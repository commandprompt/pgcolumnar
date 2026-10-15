/*-------------------------------------------------------------------------
 *
 * columnar_row_lock.c
 *		Serialize concurrent writers of the SAME columnar row (issue #5, the
 *		UPDATE facet).
 *
 * A columnar UPDATE is delete-old plus insert-new, and a DELETE marks a bit in
 * the delete vector. Neither takes a lock on the row identity, and
 * pgcolumnar_tuple_lock does not implement row locking, so core never runs
 * EvalPlanQual. Two transactions updating the same row therefore each keep their
 * own new version: the old row is deleted once and both new rows survive, so the
 * row is duplicated and one update is lost. A heap serializes this transparently.
 *
 * The serialization happens at delete-vector FLUSH time, not per row in the
 * table-AM callbacks, so the row-identity locks can be acquired in a single
 * sorted batch. This mirrors the issue #4 discipline in delete_vector_flush_buffer
 * (list_sort before taking the chunk-group locks): every transaction acquires in
 * the same order, so no AB-BA cycle forms. Acquiring incrementally per row (in
 * scan order) would take the bucketed locks in a transaction-dependent order and
 * deadlock two transactions touching unrelated rows that share a bucket.
 *
 * The flow, gated by pgcolumnar.enable_row_update_lock:
 *   1. PgColumnarRowWriteConflict, called by pgcolumnar_tuple_update and
 *      pgcolumnar_tuple_delete BEFORE marking the old row deleted, reports a
 *      same-command self-modify (the row is already in this command's own buffer)
 *      as TM_SelfModified, so a self-join UPDATE ... FROM resolves as it does for
 *      a heap. This runs even with the GUC off; it is a deterministic correctness
 *      fix independent of concurrency.
 *   2. PgColumnarSerializeFlushRows, called from delete_vector_flush_buffer before
 *      the delete-vector upsert, sorts the flushed rows by lock key, takes a
 *      transaction-scoped advisory lock on each (all held to commit), then re-reads
 *      the LATEST committed state (GetLatestSnapshot, not the caller snapshot,
 *      which PgColumnarCatalogSnapshot only copies) and raises a retryable
 *      serialization_failure if any of these rows was committed-deleted by a
 *      concurrent transaction. The lock waits out the competitor; the committed
 *      re-read decides whether a conflict actually happened, so a hash-bucket
 *      collision can only add waiting, never a false failure.
 *
 * A conflict is a row whose COMMITTED group still EXISTS and whose bit is set. A
 * row whose group is gone (retired by a concurrent rewrite) is left to the
 * flush-time Phase F3b check in PgColumnarUpsertDeleteVector, which raises its own
 * "row group was compacted concurrently" error; it is also the read-your-writes
 * case for a row this transaction just inserted, which is not a conflict.
 *
 * The loser gets one retryable serialization_failure at every isolation level,
 * matching a heap at REPEATABLE READ and what columnar already gave at
 * SERIALIZABLE. READ COMMITTED is stricter than a heap: the loser retries rather
 * than re-applying via EvalPlanQual. See docs/limitations.md.
 *
 * Written fresh for pgColumnar. It reuses no upstream source; it derives from the
 * issue #5 design analysis and the public PostgreSQL API. See PROVENANCE.md.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "access/tableam.h"
#include "access/xact.h"
#include "miscadmin.h"
#include "storage/lock.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"

#include "columnar_delete_vector.h"

/*
 * GUCs. enable_row_update_lock is USERSET so a session can turn the serialization
 * off. row_lock_buckets is POSTMASTER because the bucket count is part of the
 * advisory lock tag: two backends racing the same row must compute the same
 * bucket, which they only do when they agree on this value (as unique_lock_buckets).
 */
bool		pgcolumnar_enable_row_update_lock = true;
int			pgcolumnar_row_lock_buckets = 1024;

/*
 * row_identity_lock_key
 *		Mix (storage id, row number) into a 64-bit advisory-lock key, with the
 *		same FNV-1a plus splitmix64 finalizer as delete_vector_chunk_lock_key and
 *		the unique-key hash. A collision only makes two unrelated rows serialize
 *		needlessly; the exact-row committed re-read is what decides a real conflict.
 */
static uint64
row_identity_lock_key(uint64 storageId, uint64 rowNumber)
{
	uint64		h = UINT64CONST(1469598103934665603);	/* FNV-1a offset basis */

	h = (h ^ storageId) * UINT64CONST(1099511628211);
	h = (h ^ rowNumber) * UINT64CONST(1099511628211);

	h ^= h >> 33;
	h *= UINT64CONST(0xff51afd7ed558ccd);
	h ^= h >> 33;
	h *= UINT64CONST(0xc4ceb9fe1a85ec53);
	h ^= h >> 33;

	return h;
}

/*
 * PgColumnarRowWriteConflict
 *		Self-modify guard for pgcolumnar_tuple_update and pgcolumnar_tuple_delete,
 *		run before the write marks the old row deleted. Returns true when the write
 *		must not proceed; then *result is TM_SelfModified and tmfd is filled.
 *		Returns false when the write may proceed. The row-identity locking and the
 *		committed-conflict check happen later, in PgColumnarSerializeFlushRows at
 *		flush time, so they can be taken in one sorted batch.
 */
bool
PgColumnarRowWriteConflict(Relation rel, ItemPointer otid, CommandId cid,
						   bool wait, TM_FailureData *tmfd, TM_Result *result)
{
	uint64		rowNumber = PgColumnarItemPointerToRowNumber(otid);

	(void) wait;

	/*
	 * Same command modifying the same row twice (a self-join UPDATE ... FROM): the
	 * old row is already marked deleted in this command's own buffer. Report it as
	 * self-modified so core resolves it as it does for a heap, rather than writing
	 * a second new version. cmax == the current command id makes core treat it as
	 * already handled by this command. This runs even when the row-lock GUC is off.
	 */
	if (PgColumnarDeleteVectorBufferedDeleted(rel, rowNumber))
	{
		ItemPointerCopy(otid, &tmfd->ctid);
		tmfd->xmax = GetCurrentTransactionId();
		tmfd->cmax = cid;
		*result = TM_SelfModified;
		return true;
	}

	return false;
}

/*
 * one { lock bucket, row number } pair. Sorted by BUCKET, which is the actual
 * lock resource (SET_LOCKTAG_ADVISORY takes the bucket, not the raw key). Sorting
 * by the raw key would not order the buckets consistently, because bucket =
 * key % numBuckets and modulo is not order-preserving, so two transactions could
 * take the same two buckets in opposite order and deadlock. Ordering by the bucket
 * makes every transaction acquire shared buckets ascending, so no cycle forms.
 */
typedef struct RowLockEntry
{
	uint32		bucket;
	uint64		rowNumber;
} RowLockEntry;

static int
rowlockentry_cmp(const void *a, const void *b)
{
	const RowLockEntry *ra = (const RowLockEntry *) a;
	const RowLockEntry *rb = (const RowLockEntry *) b;

	if (ra->bucket < rb->bucket)
		return -1;
	if (ra->bucket > rb->bucket)
		return 1;
	if (ra->rowNumber < rb->rowNumber)
		return -1;
	if (ra->rowNumber > rb->rowNumber)
		return 1;
	return 0;
}

/* a committed row group plus its merged delete mask, for the batch conflict check */
typedef struct CommittedGroup
{
	uint64		firstRowNumber;
	uint64		rowCount;
	char	   *mask;
	uint32		maskLen;
} CommittedGroup;

/*
 * committed_deleted_in
 *		Is rowNumber deleted in the committed groups array (sorted by
 *		firstRowNumber)? True only when a covering group EXISTS and the bit is set;
 *		a missing group is not a conflict (left to Phase F3b / read-your-writes).
 */
static bool
committed_deleted_in(CommittedGroup *groups, int ngroups, uint64 rowNumber)
{
	int			lo = 0;
	int			hi = ngroups - 1;

	while (lo <= hi)
	{
		int			mid = (lo + hi) / 2;
		CommittedGroup *g = &groups[mid];

		if (rowNumber < g->firstRowNumber)
			hi = mid - 1;
		else if (rowNumber >= g->firstRowNumber + g->rowCount)
			lo = mid + 1;
		else
		{
			uint64		bit = rowNumber - g->firstRowNumber;

			return (g->mask != NULL && (bit >> 3) < g->maskLen &&
					(g->mask[bit >> 3] & (1 << (bit & 7))) != 0);
		}
	}
	return false;				/* no committed group covers it: not our conflict */
}

static int
committedgroup_cmp(const void *a, const void *b)
{
	const CommittedGroup *ga = (const CommittedGroup *) a;
	const CommittedGroup *gb = (const CommittedGroup *) b;

	if (ga->firstRowNumber < gb->firstRowNumber)
		return -1;
	if (ga->firstRowNumber > gb->firstRowNumber)
		return 1;
	return 0;
}

/*
 * PgColumnarSerializeFlushRows
 *		Serialize the rows about to be flushed as deleted (the delete half of every
 *		UPDATE and every DELETE in this flush) against concurrent writers of the
 *		same rows. Acquire a transaction-scoped advisory lock on each row IN SORTED
 *		KEY ORDER (deadlock-free, as every transaction acquires in the same order),
 *		then raise a retryable serialization_failure if any of these rows was
 *		committed-deleted by another transaction. Called from
 *		delete_vector_flush_buffer before the delete-vector upsert, so it runs
 *		inside the same flush that already serializes the chunk-group writes.
 */
void
PgColumnarSerializeFlushRows(uint64 storageId, const uint64 *rows, int nrows)
{
	MemoryContext tmp;
	MemoryContext old;
	RowLockEntry *ents;
	uint32		numBuckets;
	Snapshot	meta;
	List	   *rgList;
	CommittedGroup *groups;
	int			ngroups;
	int			i;
	ListCell   *lc;
	bool		conflict = false;

	if (!pgcolumnar_enable_row_update_lock || nrows <= 0)
		return;

	tmp = AllocSetContextCreate(CurrentMemoryContext,
								"columnar row serialize",
								ALLOCSET_SMALL_SIZES);
	old = MemoryContextSwitchTo(tmp);

	/*
	 * Compute each row's bucket (the actual lock resource) and sort by it, so the
	 * whole batch, and every other transaction's batch, acquires shared buckets in
	 * the same ascending order.
	 */
	numBuckets = (uint32) Max(1, pgcolumnar_row_lock_buckets);
	ents = (RowLockEntry *) palloc(sizeof(RowLockEntry) * nrows);
	for (i = 0; i < nrows; i++)
	{
		uint64		key = row_identity_lock_key(storageId, rows[i]);

		ents[i].bucket = (uint32) (key % numBuckets);
		ents[i].rowNumber = rows[i];
	}
	qsort(ents, nrows, sizeof(RowLockEntry), rowlockentry_cmp);

	for (i = 0; i < nrows; i++)
	{
		LOCKTAG		tag;

		/* a repeated bucket is already held; LockAcquire makes it a no-op */
		SET_LOCKTAG_ADVISORY(tag, MyDatabaseId,
							 (uint32) storageId, ents[i].bucket,
							 PGCOLUMNAR_LOCKCLASS_ROW_IDENTITY);
		(void) LockAcquire(&tag, ExclusiveLock, false /* transaction lock */ ,
						   false /* wait */ );
	}

	/*
	 * With the locks held, read the latest committed groups and delete masks once,
	 * then classify every flushed row against them. GetLatestSnapshot (pushed
	 * active, as PG18 asserts for a catalog scan) sees a delete a concurrent
	 * transaction committed after our own snapshot; our own not-yet-flushed deletes
	 * are in the buffer, not this committed view, so they are not self-conflicts.
	 */
	PushActiveSnapshot(GetLatestSnapshot());
	meta = PgColumnarCatalogSnapshot(GetActiveSnapshot());
	rgList = PgColumnarReadRowGroupList(storageId, meta);
	ngroups = list_length(rgList);
	groups = (CommittedGroup *) palloc0(sizeof(CommittedGroup) * Max(ngroups, 1));

	i = 0;
	foreach(lc, rgList)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);
		List	   *maskList = PgColumnarReadDeleteVectorList(storageId,
															  rg->groupNumber,
															  meta);
		uint32		want = (uint32) ((rg->rowCount + 7) / 8);
		ListCell   *mc;

		groups[i].firstRowNumber = rg->firstRowNumber;
		groups[i].rowCount = rg->rowCount;
		foreach(mc, maskList)
		{
			DeleteVectorMetadata *rm = (DeleteVectorMetadata *) lfirst(mc);
			uint32		b;

			if (rm->bitmap == NULL || rm->bitmapLen == 0)
				continue;
			if (groups[i].mask == NULL)
			{
				groups[i].mask = (char *) palloc0(want > 0 ? want : 1);
				groups[i].maskLen = want;
			}
			for (b = 0; b < rm->bitmapLen && b < want; b++)
				groups[i].mask[b] |= rm->bitmap[b];
		}
		i++;
	}
	if (ngroups > 1)
		qsort(groups, ngroups, sizeof(CommittedGroup), committedgroup_cmp);

	for (i = 0; i < nrows; i++)
	{
		if (committed_deleted_in(groups, ngroups, ents[i].rowNumber))
		{
			conflict = true;
			break;
		}
	}

	PopActiveSnapshot();
	MemoryContextSwitchTo(old);
	MemoryContextDelete(tmp);

	if (conflict)
		ereport(ERROR,
				(errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
				 errmsg("columnar: could not serialize access due to concurrent update or delete of the same row"),
				 errhint("Retry the transaction.")));
}

/*
 * PgColumnarSerializeRowNow
 *		Serialize one row on its identity NOW, during the UPDATE, rather than at
 *		the flush (#1421). Take the row's bucket lock, waiting for a concurrent
 *		writer of the row to finish, then raise the same retryable
 *		serialization_failure if that writer committed the row as deleted.
 *
 *		WHY NOT WAIT FOR THE FLUSH. An UPDATE of a table with a unique index takes
 *		the new version's unique-key lock before the flush. When a concurrent
 *		transaction updated the same row, its new version holds that key: the
 *		loser waited on it, and when the winner committed the executor's unique
 *		check raised unique_violation (23505), a duplicate the user never wrote,
 *		before the flush that raises 40001 was reached. docs/limitations.md
 *		promises 40001 and says the serialization "needs no unique index".
 *
 *		Called only for a relation with a unique index, so a table without one
 *		keeps the batched, sorted acquisition at flush. The flush still runs and
 *		re-takes this lock, which the lock manager makes a no-op. Taking locks in
 *		row order during the statement can deadlock two multi-row UPDATEs that
 *		reach shared rows in opposite orders (40P01, also retryable), as heap can.
 *
 *		ONE GROUP, NOT THE LIST. The batch reads every group's committed mask; per
 *		row that would be O(groups). The covering group comes through the fetch
 *		path's memo, and only its delete vector is read.
 */
void
PgColumnarSerializeRowNow(Relation rel, uint64 rowNumber)
{
	uint64		storageId;
	uint32		numBuckets;
	LOCKTAG		tag;
	Snapshot	meta;
	NativeRowGroupMetadata *rg;
	bool		deleted = false;
	MemoryContext tmp;
	MemoryContext old;

	if (!pgcolumnar_enable_row_update_lock)
		return;

	storageId = PgColumnarStorageId(rel);
	numBuckets = (uint32) Max(1, pgcolumnar_row_lock_buckets);
	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId, (uint32) storageId,
						 (uint32) (row_identity_lock_key(storageId, rowNumber) % numBuckets),
						 PGCOLUMNAR_LOCKCLASS_ROW_IDENTITY);
	(void) LockAcquire(&tag, ExclusiveLock, false /* transaction lock */ ,
					   false /* wait */ );

	/*
	 * With the lock held, the latest committed state: a delete committed after
	 * this statement's snapshot is visible here. This transaction's own deletes
	 * are in its buffer, not in this committed view, so they are not conflicts.
	 *
	 * The reads go in a context of their own, deleted before returning. This runs
	 * once per updated row, and in the caller's context each call would keep a copy
	 * of the group's delete bitmap until the statement ends: 402 MB for 25,000 rows
	 * of a 50,000-row group (OffgridwithJD, review of #1422).
	 */
	tmp = AllocSetContextCreate(CurrentMemoryContext,
								"pgcolumnar row serialization check",
								ALLOCSET_SMALL_SIZES);
	old = MemoryContextSwitchTo(tmp);
	PushActiveSnapshot(GetLatestSnapshot());
	meta = PgColumnarCatalogSnapshot(GetActiveSnapshot());
	rg = PgColumnarLookupRowGroup(storageId, meta, rowNumber);
	if (rg != NULL)
	{
		List	   *maskList = PgColumnarReadDeleteVectorList(storageId,
															  rg->groupNumber, meta);
		uint64		bit = rowNumber - rg->firstRowNumber;
		ListCell   *mc;

		foreach(mc, maskList)
		{
			DeleteVectorMetadata *rm = (DeleteVectorMetadata *) lfirst(mc);

			if (rm->bitmap != NULL && (bit >> 3) < rm->bitmapLen &&
				(rm->bitmap[bit >> 3] & (1 << (bit & 7))) != 0)
			{
				deleted = true;
				break;
			}
		}
	}
	PopActiveSnapshot();
	MemoryContextSwitchTo(old);
	MemoryContextDelete(tmp);

	if (deleted)
		ereport(ERROR,
				(errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
				 errmsg("columnar: could not serialize access due to concurrent update or delete of the same row"),
				 errhint("Retry the transaction.")));
}

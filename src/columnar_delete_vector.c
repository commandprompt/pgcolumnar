/*-------------------------------------------------------------------------
 *
 * pgcolumnar_delete_vector.c
 *		Delete and update marking for pgColumnar (spec 7.5, 9). Deletes do not
 *		rewrite stripes; instead a bit is set in the columnar.delete_vector entry for
 *		the affected chunk group. Update is delete-plus-insert, so it also uses
 *		this path for the old row.
 *
 * Marks are accumulated in a per-(sub)transaction in-memory buffer and applied
 * to the catalog in a single pass at flush time. Buffering is required because
 * many rows in one chunk group are typically deleted by a single command, and
 * a shared catalog tuple must not be heap-updated more than once per command.
 * The buffer is flushed at scan start of the same relation (read-your-writes)
 * and at transaction pre-commit, and discarded on rollback and on the rollback
 * of the subtransaction that made the marks.
 *
 * Independent MIT implementation built from
 * design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md and the public PostgreSQL API only.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_delete_vector.h"
#include "access/xact.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"

/* one chunk group's accumulated delete bits */
typedef struct DeleteVectorChunkBuffer
{
	uint64		stripeId;
	int			chunkId;
	uint64		startRowNumber;
	uint64		endRowNumber;
	uint64		rowCount;
	char	   *mask;			/* maskLen bytes */
	uint32		maskLen;
} DeleteVectorChunkBuffer;

/* pending delete marks for one storage id under one subtransaction */
typedef struct DeleteVectorBuffer
{
	Oid			relid;
	uint64		storageId;
	SubTransactionId subid;
	List	   *chunks;			/* list of DeleteVectorChunkBuffer* */
	List	   *rowGroupCache;	/* cached NativeRowGroupMetadata* for resolution */

	/*
	 * Last lookup results. A delete arrives in row-number order, so the previous
	 * answer is almost always the right one; without these, each row rescans both
	 * lists from the head, and the entry it wants is the one most recently
	 * appended to the tail.
	 */
	NativeRowGroupMetadata *lastGroup;
	DeleteVectorChunkBuffer *lastChunk;
} DeleteVectorBuffer;

static MemoryContext PgColumnarDeleteVectorContext = NULL;
static List *PgColumnarDeleteVectorBuffers = NIL;

static DeleteVectorBuffer *delete_vector_get_buffer(Relation rel, uint64 storageId);
static NativeRowGroupMetadata *delete_vector_find_row_group(DeleteVectorBuffer *buf,
													  uint64 rowNumber);
static DeleteVectorChunkBuffer *delete_vector_get_chunk(DeleteVectorBuffer *buf,
											 uint64 stripeId, int chunkId,
											 uint64 startRowNumber,
											 uint64 endRowNumber,
											 uint64 rowCount);
static void delete_vector_flush_buffer(DeleteVectorBuffer *buf);

/*
 * delete_vector_chunk_cmp
 *		Total order over chunk-group buffers by (stripeId, chunkId,
 *		startRowNumber). Flushing in this order makes every transaction acquire
 *		the per-chunk-group locks (pgcolumnar_metadata.c) in the same global
 *		order, so two concurrent deleters cannot form an AB-BA deadlock cycle.
 */
static int
delete_vector_chunk_cmp(const ListCell *a, const ListCell *b)
{
	const DeleteVectorChunkBuffer *ca = (const DeleteVectorChunkBuffer *) lfirst(a);
	const DeleteVectorChunkBuffer *cb = (const DeleteVectorChunkBuffer *) lfirst(b);

	if (ca->stripeId != cb->stripeId)
		return ca->stripeId < cb->stripeId ? -1 : 1;
	if (ca->chunkId != cb->chunkId)
		return ca->chunkId < cb->chunkId ? -1 : 1;
	if (ca->startRowNumber != cb->startRowNumber)
		return ca->startRowNumber < cb->startRowNumber ? -1 : 1;
	return 0;
}

/*
 * delete_vector_get_buffer
 *		Find or create the delete buffer for a storage id under the current
 *		subtransaction.
 */
static DeleteVectorBuffer *
delete_vector_get_buffer(Relation rel, uint64 storageId)
{
	SubTransactionId subid = GetCurrentSubTransactionId();
	ListCell   *lc;
	MemoryContext oldContext;
	DeleteVectorBuffer *buf;

	foreach(lc, PgColumnarDeleteVectorBuffers)
	{
		buf = (DeleteVectorBuffer *) lfirst(lc);
		if (buf->storageId == storageId && buf->subid == subid)
			return buf;
	}

	if (PgColumnarDeleteVectorContext == NULL)
		PgColumnarDeleteVectorContext = AllocSetContextCreate(TopTransactionContext,
													   "columnar delete vector",
													   ALLOCSET_DEFAULT_SIZES);

	oldContext = MemoryContextSwitchTo(PgColumnarDeleteVectorContext);
	buf = palloc0(sizeof(DeleteVectorBuffer));
	buf->relid = RelationGetRelid(rel);
	buf->storageId = storageId;
	buf->subid = subid;
	buf->chunks = NIL;
	buf->rowGroupCache = NIL;
	buf->lastChunk = NULL;
	buf->lastGroup = NULL;
	PgColumnarDeleteVectorBuffers = lappend(PgColumnarDeleteVectorBuffers, buf);
	MemoryContextSwitchTo(oldContext);

	return buf;
}


/*
 * delete_vector_find_row_group
 *		Native (PGCN v1): return the row group that contains rowNumber,
 *		rebuilding the cache from the catalog on a miss.
 */
static NativeRowGroupMetadata *
delete_vector_find_row_group(DeleteVectorBuffer *buf, uint64 rowNumber)
{
	ListCell   *lc;
	int			attempt;

	if (buf->lastGroup != NULL &&
		rowNumber >= buf->lastGroup->firstRowNumber &&
		rowNumber < buf->lastGroup->firstRowNumber + buf->lastGroup->rowCount)
		return buf->lastGroup;

	for (attempt = 0; attempt < 2; attempt++)
	{
		foreach(lc, buf->rowGroupCache)
		{
			NativeRowGroupMetadata *g = (NativeRowGroupMetadata *) lfirst(lc);

			if (rowNumber >= g->firstRowNumber &&
				rowNumber < g->firstRowNumber + g->rowCount)
			{
				buf->lastGroup = g;
				return g;
			}
		}

		if (attempt == 0)
		{
			MemoryContext oldContext =
				MemoryContextSwitchTo(PgColumnarDeleteVectorContext);
			Snapshot	snap = PgColumnarCatalogSnapshot(GetActiveSnapshot());

			buf->rowGroupCache = PgColumnarReadRowGroupList(buf->storageId, snap);
			buf->lastGroup = NULL;	/* points into the list just replaced */
			MemoryContextSwitchTo(oldContext);
		}
	}

	return NULL;
}

/*
 * delete_vector_get_chunk
 *		Find or create the chunk-group delete buffer for a chunk group.
 */
static DeleteVectorChunkBuffer *
delete_vector_get_chunk(DeleteVectorBuffer *buf, uint64 stripeId, int chunkId,
				  uint64 startRowNumber, uint64 endRowNumber, uint64 rowCount)
{
	ListCell   *lc;
	MemoryContext oldContext;
	DeleteVectorChunkBuffer *chunk;

	chunk = buf->lastChunk;
	if (chunk != NULL && chunk->stripeId == stripeId &&
		chunk->chunkId == chunkId && chunk->startRowNumber == startRowNumber)
		return chunk;

	foreach(lc, buf->chunks)
	{
		chunk = (DeleteVectorChunkBuffer *) lfirst(lc);
		if (chunk->stripeId == stripeId && chunk->chunkId == chunkId &&
			chunk->startRowNumber == startRowNumber)
		{
			buf->lastChunk = chunk;
			return chunk;
		}
	}

	oldContext = MemoryContextSwitchTo(PgColumnarDeleteVectorContext);
	chunk = palloc0(sizeof(DeleteVectorChunkBuffer));
	chunk->stripeId = stripeId;
	chunk->chunkId = chunkId;
	chunk->startRowNumber = startRowNumber;
	chunk->endRowNumber = endRowNumber;
	chunk->rowCount = rowCount;
	chunk->maskLen = (uint32) ((rowCount + 7) / 8);
	chunk->mask = palloc0(chunk->maskLen);
	buf->chunks = lappend(buf->chunks, chunk);
	buf->lastChunk = chunk;
	MemoryContextSwitchTo(oldContext);

	return chunk;
}

/*
 * PgColumnarMarkRowDeleted
 *		Record that the row with the given 1-based row number is deleted, by
 *		setting its bit in the in-memory delete buffer for its chunk group.
 *		The mark targets the whole enclosing row group as one bitmap (chunk id
 *		0), sized to the row group's rowCount, found via
 *		delete_vector_find_row_group using its firstRowNumber and rowCount.
 */
void
PgColumnarMarkRowDeleted(Relation rel, uint64 rowNumber)
{
	uint64		storageId = PgColumnarStorageId(rel);
	DeleteVectorBuffer *buf = delete_vector_get_buffer(rel, storageId);
	uint64		startRowNumber;
	uint64		endRowNumber;
	uint64		bitIndex;
	DeleteVectorChunkBuffer *chunk;
	NativeRowGroupMetadata *rg = delete_vector_find_row_group(buf, rowNumber);

	/*
	 * The whole row group is one bitmap (chunk id 0), sized to its row count. The
	 * vestigial stripe_id/chunk_id/start/end columns and group-number re-keying
	 * are pending cleanup (see design/PHASE_F_PLAN.md, F1).
	 */
	if (rg == NULL)
		elog(ERROR,
			 "columnar: cannot delete row " UINT64_FORMAT
			 ": no row group covers it", rowNumber);

	startRowNumber = rg->firstRowNumber;
	endRowNumber = startRowNumber + rg->rowCount - 1;
	chunk = delete_vector_get_chunk(buf, rg->groupNumber, 0,
							  startRowNumber, endRowNumber, rg->rowCount);
	bitIndex = rowNumber - startRowNumber;
	chunk->mask[bitIndex >> 3] |= (char) (1 << (bitIndex & 7));

	/*
	 * The deleted row makes its block not all-visible; clear any VM bit so an
	 * index-only scan never skips the fetch for a block with a dead row (gap 28).
	 * A no-op unless a prior vacuum had marked the block visible.
	 */
	PgColumnarVMClearForRow(rel, rowNumber);
}

/*
 * PgColumnarDeleteVectorBufferedDeleted
 *		True when the row is marked deleted in an in-memory row-mask buffer that
 *		has not yet been flushed to the catalog. The unique/primary-key check runs
 *		as part of an insert (or the insert half of an update) and fetches a
 *		conflicting row through PgColumnarReadRowByNumber before the delete of the
 *		old row is flushed; consulting the buffer here lets a same-key UPDATE (the
 *		old row is buffered-deleted) proceed. Checks every buffer for the relation,
 *		across subtransactions.
 */
bool
PgColumnarDeleteVectorBufferedDeleted(Relation rel, uint64 rowNumber)
{
	Oid			relid = RelationGetRelid(rel);
	ListCell   *lc;

	foreach(lc, PgColumnarDeleteVectorBuffers)
	{
		DeleteVectorBuffer *buf = (DeleteVectorBuffer *) lfirst(lc);
		ListCell   *cc;

		if (buf->relid != relid)
			continue;

		foreach(cc, buf->chunks)
		{
			DeleteVectorChunkBuffer *chunk = (DeleteVectorChunkBuffer *) lfirst(cc);
			uint64		bitIndex;

			if (rowNumber < chunk->startRowNumber ||
				rowNumber > chunk->endRowNumber)
				continue;
			bitIndex = rowNumber - chunk->startRowNumber;
			if (dv_row_deleted(chunk->mask, chunk->maskLen, bitIndex))
				return true;
		}
	}

	return false;
}

/*
 * delete_vector_flush_buffer
 *		Apply one buffer's accumulated marks to the catalog and empty it. Each
 *		chunk group is upserted exactly once, so no catalog tuple is updated
 *		more than once in this command.
 */
static void
delete_vector_flush_buffer(DeleteVectorBuffer *buf)
{
	ListCell   *lc;
	bool		pushedSnapshot = false;

	if (buf->chunks == NIL)
		return;

	/* deterministic lock-acquisition order across transactions (issue #4) */
	list_sort(buf->chunks, delete_vector_chunk_cmp);

	if (!ActiveSnapshotSet())
	{
		PushActiveSnapshot(GetTransactionSnapshot());
		pushedSnapshot = true;
	}

	/*
	 * issue #5: before writing the delete marks, serialize the rows being flushed
	 * as deleted against concurrent writers of the same rows, taking their
	 * row-identity locks in one sorted batch (deadlock-free) and raising a
	 * serialization_failure on a committed conflict. Collect the row numbers from
	 * the sorted chunks first, so the batch is ordered the same way for every
	 * transaction, exactly like the chunk-group locks below.
	 */
	{
		uint64		nrows = 0;
		uint64	   *rows;
		uint64		ri = 0;
		ListCell   *cc;

		foreach(cc, buf->chunks)
		{
			DeleteVectorChunkBuffer *chunk = (DeleteVectorChunkBuffer *) lfirst(cc);
			uint32		i;
			int			bit;

			for (i = 0; i < chunk->maskLen; i++)
				for (bit = 0; bit < 8; bit++)
					if (chunk->mask[i] & (1 << bit))
						nrows++;
		}
		if (nrows > 0)
		{
			rows = (uint64 *) palloc(sizeof(uint64) * nrows);
			foreach(cc, buf->chunks)
			{
				DeleteVectorChunkBuffer *chunk = (DeleteVectorChunkBuffer *) lfirst(cc);
				uint32		i;
				int			bit;

				for (i = 0; i < chunk->maskLen; i++)
					for (bit = 0; bit < 8; bit++)
						if (chunk->mask[i] & (1 << bit))
							rows[ri++] = chunk->startRowNumber + (uint64) (i * 8 + bit);
			}
			PgColumnarSerializeFlushRows(buf->storageId, rows, (int) nrows);
			pfree(rows);
		}
	}

	foreach(lc, buf->chunks)
	{
		DeleteVectorChunkBuffer *chunk = (DeleteVectorChunkBuffer *) lfirst(lc);
		DeleteVectorMetadata rm;
		uint32		i;
		int			bit;
		int			deleted = 0;

		for (i = 0; i < chunk->maskLen; i++)
			for (bit = 0; bit < 8; bit++)
				if (chunk->mask[i] & (1 << bit))
					deleted++;

		rm.groupNumber = chunk->stripeId;
		rm.deletedCount = deleted;
		rm.bitmap = chunk->mask;
		rm.bitmapLen = chunk->maskLen;

		PgColumnarUpsertDeleteVector(buf->storageId, &rm);
	}

	if (pushedSnapshot)
		PopActiveSnapshot();

	buf->chunks = NIL;
	buf->rowGroupCache = NIL;
	buf->lastChunk = NULL;
	buf->lastGroup = NULL;
}

/*
 * PgColumnarFlushDeleteVectorForRelation
 *		Flush pending delete marks for one relation. Called at scan start so a
 *		delete made earlier in this transaction is visible to a later scan.
 */
void
PgColumnarFlushDeleteVectorForRelation(Relation rel)
{
	Oid			relid = RelationGetRelid(rel);
	ListCell   *lc;

	foreach(lc, PgColumnarDeleteVectorBuffers)
	{
		DeleteVectorBuffer *buf = (DeleteVectorBuffer *) lfirst(lc);

		if (buf->relid == relid)
			delete_vector_flush_buffer(buf);
	}
}

/*
 * PgColumnarFlushAllDeleteVectors
 *		Flush every pending delete buffer. Called at transaction pre-commit.
 */
void
PgColumnarFlushAllDeleteVectors(void)
{
	ListCell   *lc;

	foreach(lc, PgColumnarDeleteVectorBuffers)
		delete_vector_flush_buffer((DeleteVectorBuffer *) lfirst(lc));
}

/*
 * PgColumnarDiscardAllDeleteVectors
 *		Forget all pending delete buffers (transaction end).
 */
void
PgColumnarDiscardAllDeleteVectors(void)
{
	PgColumnarDeleteVectorBuffers = NIL;
	PgColumnarDeleteVectorContext = NULL;
}

/*
 * PgColumnarDeleteVectorDiscardSubXact
 *		Drop delete buffers made in an aborting subtransaction. The catalog
 *		rows they would have produced were never written (or, if a scan flushed
 *		them, are made invisible by the subtransaction abort itself).
 */
void
PgColumnarDeleteVectorDiscardSubXact(SubTransactionId subid)
{
	List	   *kept = NIL;
	ListCell   *lc;
	MemoryContext oldContext;

	if (PgColumnarDeleteVectorBuffers == NIL)
		return;

	oldContext = MemoryContextSwitchTo(PgColumnarDeleteVectorContext);
	foreach(lc, PgColumnarDeleteVectorBuffers)
	{
		DeleteVectorBuffer *buf = (DeleteVectorBuffer *) lfirst(lc);

		if (buf->subid != subid)
			kept = lappend(kept, buf);
	}
	MemoryContextSwitchTo(oldContext);

	PgColumnarDeleteVectorBuffers = kept;
}

/*
 * PgColumnarDeleteVectorPromoteSubXact
 *		On subtransaction commit, reassign its delete buffers to the parent so
 *		they survive until the parent resolves.
 */
void
PgColumnarDeleteVectorPromoteSubXact(SubTransactionId subid, SubTransactionId parent)
{
	ListCell   *lc;

	foreach(lc, PgColumnarDeleteVectorBuffers)
	{
		DeleteVectorBuffer *buf = (DeleteVectorBuffer *) lfirst(lc);

		if (buf->subid == subid)
			buf->subid = parent;
	}
}

/*
 * PgColumnarGroupDeletedCount
 *		How many of this row group's rows are deleted, under the given catalog
 *		snapshot. Bits past the group's row count are ignored.
 *
 *		This walks the group's mask the same way the reader does when it builds
 *		one (spec 7.5), which is why it lives beside that code rather than being
 *		reimplemented at each caller.
 *
 *		It does NOT do so to avoid double-counting. An earlier version of this
 *		comment said a group can have several delete_vector rows whose bitmaps
 *		overlap, so summing deletedCount would count a row deleted twice. The
 *		catalog forbids it: delete_vector carries a unique index on
 *		(storage_id, group_number), so there is at most one row per group and
 *		nothing to OR together. The planner estimate now sums deleted_count over
 *		the storage in one scan for exactly that reason.
 */
uint64
PgColumnarGroupDeletedCount(uint64 storageId, NativeRowGroupMetadata *rg,
						  Snapshot snapshot)
{
	uint32		want = (uint32) ((rg->rowCount + 7) / 8);
	char	   *mask;
	List	   *rml;
	ListCell   *mc;
	uint64		deleted = 0;
	uint32		b;

	rml = PgColumnarReadDeleteVectorList(storageId, rg->groupNumber, snapshot);
	if (rml == NIL)
		return 0;

	mask = palloc0(want > 0 ? want : 1);
	foreach(mc, rml)
	{
		DeleteVectorMetadata *rm = (DeleteVectorMetadata *) lfirst(mc);

		if (rm->bitmap == NULL || rm->bitmapLen == 0)
			continue;
		for (b = 0; b < rm->bitmapLen && b < want; b++)
			mask[b] |= rm->bitmap[b];
	}

	for (b = 0; b < want; b++)
	{
		uint64		base = (uint64) b * 8;
		int			i;

		for (i = 0; i < 8; i++)
			if (base + i < rg->rowCount && ((mask[b] >> i) & 1))
				deleted++;
	}

	pfree(mask);
	return deleted;
}

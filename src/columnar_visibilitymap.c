/*-------------------------------------------------------------------------
 *
 * pgcolumnar_visibilitymap.c
 *		Visibility-map fork maintenance for pgColumnar index-only scans
 *		(gap 28 direction 1, phase 1: the load-bearing prototype).
 *
 *		PostgreSQL's index-only-scan executor (nodeIndexonlyscan.c) is
 *		hard-wired to read the table's visibility-map fork via VM_ALL_VISIBLE /
 *		visibilitymap_get_status; there is no table-AM hook to substitute a
 *		different all-visible source. So to make the executor skip the columnar
 *		fetch for an all-visible range, pgColumnar must maintain a real VM fork
 *		on its relation, keyed by the same synthetic block numbers its TIDs use
 *		(block = rowNumber / MaxHeapTuplesPerPage).
 *
 *		The catch: those TIDs are synthetic (columnar data is not stored as heap
 *		pages at those blocks), so the stock write path visibilitymap_set() --
 *		which requires the heap buffer and sets PD_ALL_VISIBLE on it, and whose
 *		signature differs across majors -- cannot be used. This file writes the
 *		VM bit directly: pin/extend the VM page with the stock visibilitymap_pin,
 *		set the all-visible bit using the (stable since 9.6) two-bits-per-block
 *		layout, WAL-log the whole page for crash safety, and leave reads to the
 *		stock visibilitymap_get_status. This decouples the write from any heap
 *		page and from the per-version visibilitymap_set signature.
 *
 *		Phase 1 exposes pgcolumnar.vm_selftest(rel, blk) to prove empirically that
 *		a bit written here is read back by visibilitymap_get_status on a columnar
 *		relation. Later phases wire set-in-vacuum and clear-on-write.
 *
 * Independent MIT implementation from the public PostgreSQL API and the
 * documented visibility-map on-disk layout.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_metadata.h"
#include "fmgr.h"
#include "columnar_compat.h"

#include "access/relation.h"
#include "access/table.h"
#include "access/visibilitymap.h"
#include "access/xloginsert.h"
#include "miscadmin.h"
#include "pgstat.h"
#include "storage/bufmgr.h"
#include "storage/bufpage.h"
#include "storage/latch.h"
#include "storage/procarray.h"
#include "utils/rel.h"
#include "utils/wait_event.h"

PG_FUNCTION_INFO_V1(pgcolumnar_vm_selftest);
PG_FUNCTION_INFO_V1(pgcolumnar_vm_is_visible);

/*
 * Visibility-map on-disk layout. These mirror the private macros in
 * src/backend/access/heap/visibilitymap.c; the two-bits-per-heap-block layout
 * has been stable since PostgreSQL 9.6, and pgcolumnar.vm_selftest verifies at
 * run time that a bit written with this layout is read back by the backend's
 * own visibilitymap_get_status (which uses the real macros), so any divergence
 * would surface immediately in the matrix rather than silently.
 */
#define COLUMNAR_VM_MAPSIZE			(BLCKSZ - MAXALIGN(SizeOfPageHeaderData))
#define COLUMNAR_VM_BITS_PER_BLOCK	2
#define COLUMNAR_VM_BLOCKS_PER_BYTE	(BITS_PER_BYTE / COLUMNAR_VM_BITS_PER_BLOCK)
#define COLUMNAR_VM_BLOCKS_PER_PAGE	(COLUMNAR_VM_MAPSIZE * COLUMNAR_VM_BLOCKS_PER_BYTE)
#define COLUMNAR_VM_TO_MAPBYTE(x) \
	(((x) % COLUMNAR_VM_BLOCKS_PER_PAGE) / COLUMNAR_VM_BLOCKS_PER_BYTE)
#define COLUMNAR_VM_TO_OFFSET(x) \
	(((x) % COLUMNAR_VM_BLOCKS_PER_BYTE) * COLUMNAR_VM_BITS_PER_BLOCK)

/*
 * PgColumnarVMSetVisible
 *		Mark the synthetic block `blk` all-visible in the relation's VM fork,
 *		WAL-logged. Idempotent: a no-op if the bit is already set. This writes
 *		only the VM fork -- there is no heap page to flag -- which is why it does
 *		not go through visibilitymap_set().
 *
 *		Returns true when this call flipped the bit from 0 to 1, false when it
 *		was already set. The test and the set happen under the VM page's
 *		exclusive lock, the lock a writer's clear takes too (#2170).
 */
static bool
PgColumnarVMSetVisible(Relation rel, BlockNumber blk)
{
	Buffer		vmbuf = InvalidBuffer;
	Page		page;
	char	   *map;
	uint32		mapByte = COLUMNAR_VM_TO_MAPBYTE(blk);
	uint8		mapBit = (uint8) (VISIBILITYMAP_ALL_VISIBLE << COLUMNAR_VM_TO_OFFSET(blk));
	bool		flipped = false;

	/* pin (extending the VM fork as needed) the page that holds blk's bit */
	visibilitymap_pin(rel, blk, &vmbuf);
	LockBuffer(vmbuf, BUFFER_LOCK_EXCLUSIVE);

	page = BufferGetPage(vmbuf);
	map = PageGetContents(page);

	if (!(map[mapByte] & mapBit))
	{
		START_CRIT_SECTION();
		map[mapByte] |= mapBit;
		MarkBufferDirty(vmbuf);
		if (RelationNeedsWAL(rel))
		{
			XLogRecPtr	recptr = log_newpage_buffer(vmbuf, false);

			PageSetLSN(page, recptr);
		}
		END_CRIT_SECTION();
		flipped = true;
	}

	LockBuffer(vmbuf, BUFFER_LOCK_UNLOCK);
	ReleaseBuffer(vmbuf);
	return flipped;
}

/*
 * PgColumnarVMClearVisible
 *		Clear the all-visible (and all-frozen) bits for `blk`, WAL-logged. Used
 *		by write paths so a modified range is never reported all-visible.
 */
static void
PgColumnarVMClearVisible(Relation rel, BlockNumber blk)
{
	Buffer		vmbuf = InvalidBuffer;
	Page		page;
	char	   *map;
	uint32		mapByte = COLUMNAR_VM_TO_MAPBYTE(blk);
	uint8		mapBits = (uint8) ((VISIBILITYMAP_ALL_VISIBLE | VISIBILITYMAP_ALL_FROZEN)
								   << COLUMNAR_VM_TO_OFFSET(blk));

	/* nothing to clear if the VM fork does not yet cover blk */
	if (visibilitymap_get_status(rel, blk, &vmbuf) == 0)
	{
		if (BufferIsValid(vmbuf))
			ReleaseBuffer(vmbuf);
		return;
	}

	LockBuffer(vmbuf, BUFFER_LOCK_EXCLUSIVE);
	page = BufferGetPage(vmbuf);
	map = PageGetContents(page);

	if (map[mapByte] & mapBits)
	{
		START_CRIT_SECTION();
		map[mapByte] &= ~mapBits;
		MarkBufferDirty(vmbuf);
		if (RelationNeedsWAL(rel))
		{
			XLogRecPtr	recptr = log_newpage_buffer(vmbuf, false);

			PageSetLSN(page, recptr);
		}
		END_CRIT_SECTION();
	}

	LockBuffer(vmbuf, BUFFER_LOCK_UNLOCK);
	ReleaseBuffer(vmbuf);
}

/*
 * PgColumnarVMClearForRow
 *		Clear the all-visible bit for the synthetic block that holds `rowNumber`.
 *		The block is computed the same way PgColumnarRowNumberToItemPointer derives
 *		a TID block, so it matches the block the index-only-scan executor probes.
 *		Called by every write path (insert/delete/update) so a modified block is
 *		never left all-visible. Cheap when no bit is set (a short-circuit read).
 */
void
PgColumnarVMClearForRow(Relation rel, uint64 rowNumber)
{
	BlockNumber blk = (BlockNumber) (rowNumber / COLUMNAR_VALID_ITEMPOINTER_OFFSETS);

	PgColumnarVMClearVisible(rel, blk);
}

/*
 * PgColumnarVMClearForRowRange
 *		Clear the all-visible bit for every synthetic block covering
 *		[firstRowNumber, firstRowNumber + rowCount). Used when a whole live
 *		row group is retired (pgcolumnar.expire) so an index-only scan cannot
 *		keep answering from the VM after the group's catalog rows are gone.
 */
void
PgColumnarVMClearForRowRange(Relation rel, uint64 firstRowNumber, uint64 rowCount)
{
	uint64		last;
	BlockNumber b0;
	BlockNumber b1;
	BlockNumber blk;

	if (rowCount == 0)
		return;

	last = firstRowNumber + rowCount - 1;
	b0 = (BlockNumber) (firstRowNumber / COLUMNAR_VALID_ITEMPOINTER_OFFSETS);
	b1 = (BlockNumber) (last / COLUMNAR_VALID_ITEMPOINTER_OFFSETS);
	for (blk = b0; blk <= b1; blk++)
		PgColumnarVMClearVisible(rel, blk);
}

/*
 * PgColumnarVMIsVisible
 *		True if `blk` is marked all-visible in the VM fork. Thin wrapper over the
 *		stock reader (the same call the index-only-scan executor makes).
 */
static bool
PgColumnarVMIsVisible(Relation rel, BlockNumber blk)
{
	Buffer		vmbuf = InvalidBuffer;
	uint8		status = visibilitymap_get_status(rel, blk, &vmbuf);

	if (BufferIsValid(vmbuf))
		ReleaseBuffer(vmbuf);
	return (status & VISIBILITYMAP_ALL_VISIBLE) != 0;
}

/* qsort: order row ranges by first row number */
static int
rowrange_cmp(const void *a, const void *b)
{
	uint64		fa = ((const PgColumnarRowRange *) a)->firstRowNumber;
	uint64		fb = ((const PgColumnarRowRange *) b)->firstRowNumber;

	if (fa < fb)
		return -1;
	if (fa > fb)
		return 1;
	return 0;
}

/*
 * vm_all_visible_groups
 *		The row groups PgColumnarComputeAllVisibleGroups finds all-visible, as
 *		an array sorted by first row number. Sets *ngroups.
 */
static PgColumnarRowRange *
vm_all_visible_groups(uint64 storageId, TransactionId oldestXmin, int *ngroups)
{
	List	   *groups = PgColumnarComputeAllVisibleGroups(storageId, oldestXmin);
	int			n = list_length(groups);
	PgColumnarRowRange *arr;
	ListCell   *lc;
	int			i = 0;

	*ngroups = n;
	if (n == 0)
		return NULL;

	arr = palloc(sizeof(PgColumnarRowRange) * n);
	foreach(lc, groups)
		arr[i++] = *(PgColumnarRowRange *) lfirst(lc);
	qsort(arr, n, sizeof(PgColumnarRowRange), rowrange_cmp);
	return arr;
}

/*
 * vm_set_hold
 *		Dev/test pause: wait `ms`, interruptibly, reporting `wait_event_info`.
 *		Zero (the default of both GUCs that drive it) returns at once.
 *		pgcolumnar.vm_set_hold_ms pauses between the computation and the set
 *		(#2059) and reports Extension; pgcolumnar.vm_set_release_hold_ms
 *		pauses before VACUUM releases each row group it locked (#2268) and
 *		reports Timeout/PgSleep, so a test can tell the two pauses apart in
 *		pg_stat_activity.
 */
static void
vm_set_hold(int ms, uint32 wait_event_info)
{
	long		remaining = (long) ms;

	while (remaining > 0)
	{
		long		slice = Min(remaining, 1000L);
		int			rc;

		rc = WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
					   slice, wait_event_info);
		if (rc & WL_LATCH_SET)
			ResetLatch(MyLatch);
		CHECK_FOR_INTERRUPTS();
		remaining -= slice;
	}
}

/*
 * vm_take_group
 *		Lock one row group for the set and judge it again under the lock.
 *		True when the lock was free and the group still has no delete,
 *		committed or in progress. *held says whether the lock is now ours.
 *		The lock is taken without waiting: a writer that holds it has flushed
 *		marks into the group, so the group is not all-visible anyway, and
 *		VACUUM must not queue behind a transaction that may stay open.
 */
static bool
vm_take_group(uint64 storageId, const PgColumnarRowRange *g, bool *held)
{
	*held = PgColumnarConditionalLockChunkGroup(storageId, g->groupNumber);
	if (!*held)
		return false;
	return !PgColumnarGroupHasDeleteDirty(storageId, g->groupNumber);
}

/* give back a row group vm_take_group locked */
static void
vm_release_group(uint64 storageId, const PgColumnarRowRange *g, bool held)
{
	if (!held)
		return;
	vm_set_hold(pgcolumnar_vm_set_release_hold_ms, WAIT_EVENT_PG_SLEEP);
	PgColumnarUnlockChunkGroup(storageId, g->groupNumber);
}

/*
 * PgColumnarVMSetVisibleForRelation
 *		Lazy vacuum step (gap 28 phase 3): mark all-visible row groups in the
 *		VM fork. A row group is all-visible when its catalog row precedes the
 *		oldest-xmin horizon and it has no delete, committed or in progress.
 *		The VM bit is set for every synthetic block that lies entirely within
 *		a run of contiguous all-visible groups, so a block that straddles two
 *		all-visible groups counts and one that straddles a not-all-visible
 *		neighbour does not. Runs under ShareUpdateExclusiveLock, concurrent
 *		with readers and writers.
 *
 *		The rule this keeps is heap's: once a delete is visible to some
 *		snapshot, no index-only scan under that snapshot finds its block
 *		all-visible. Heap gets it from the buffer lock. VACUUM checks the
 *		page's tuples and sets the bit under it, and a deleter clears the bit
 *		under it before its delete can commit. Here the check reads the
 *		catalog and the set writes the VM fork, so nothing made the two one
 *		step. A delete that committed between them got its bit set anyway, and
 *		an index-only scan counted the deleted row until a later pass cleared
 *		it (#2268).
 *
 *		The chunk-group lock (class 101) is the step here. Every flush of
 *		delete marks into a group takes it before it writes the group's
 *		delete_vector row, holds it to commit, and clears the VM bits of its
 *		rows after the write (delete_vector_flush_buffer). So, for each block,
 *		VACUUM locks every group covering it, judges each group again under
 *		the lock, and sets the bit only if all of them are still clean:
 *
 *		- a delete committed before VACUUM took the lock is in the catalog,
 *		  and the judgement sees it, so the bit is not set;
 *		- a writer that holds the lock has a flush in progress, so VACUUM
 *		  skips the group without waiting;
 *		- a writer that flushes later waits for VACUUM to give the lock back,
 *		  then writes its row and clears the bit before it commits. Until
 *		  then its delete is visible to no snapshot but its own, and its own
 *		  index-only scans flush first (#2169).
 *
 *		Marks still in a writer's memory are cleared the same way, by its
 *		flush. A row group can only leave the catalog under a lock that
 *		conflicts with VACUUM's (expire, recluster, compaction), so the groups
 *		of the first computation stay put. This replaces the second
 *		computation of #2059 and #2170, which cleared a bit after the fact and
 *		left the window open in between.
 *
 *		VACUUM holds only the locks of the groups covering the current block:
 *		a group is locked when the walk reaches its first block and given
 *		back once the walk passes its last. A group holds at least one row,
 *		so that is at most COLUMNAR_VALID_ITEMPOINTER_OFFSETS + 1 locks.
 *
 *		pgcolumnar.vm_set_hold_ms pauses between the computation and the
 *		walk, so a test can commit a delete in that window (#2059).
 *		pgcolumnar.vm_set_release_hold_ms pauses before each group is given
 *		back, so a test can read the VM, and try a delete, while VACUUM holds
 *		a group whose bits it has just set (#2268).
 *
 *		Returns how many ROWS the blocks found all-visible under the lock
 *		cover, set now or already set, which is what the caller needs to
 *		record the result in pg_class (#507). Rows and not blocks on purpose:
 *		these are *synthetic* blocks, rowNumber / K, and relpages counts
 *		stored pages -- on a 20,000,000-row table that is 68,730 against
 *		45,994. Handing a synthetic block count to a field core divides by
 *		relpages produces an all-visible fraction above 1 from arithmetic
 *		alone.
 */
uint64
PgColumnarVMSetVisibleForRelation(Relation rel)
{
	uint64		visibleRows = 0;
	uint64		storageId = PgColumnarStorageId(rel);
	TransactionId oldestXmin = PgColumnarOldestXmin(rel);
	uint64		K = COLUMNAR_VALID_ITEMPOINTER_OFFSETS;
	PgColumnarRowRange *arr;
	bool	   *held;
	bool	   *clean;
	int			n;
	int			i = 0;

	arr = vm_all_visible_groups(storageId, oldestXmin, &n);
	if (n == 0)
		return 0;

	held = palloc0(sizeof(bool) * n);
	clean = palloc0(sizeof(bool) * n);

	vm_set_hold(pgcolumnar_vm_set_hold_ms, PG_WAIT_EXTENSION);

	while (i < n)
	{
		uint64		lo = arr[i].firstRowNumber;
		uint64		hi = lo + arr[i].rowCount;
		BlockNumber b,
					bend;
		int			m = i + 1;
		int			taken;
		int			released;
		int			unclean = 0;

		/* merge contiguous (adjacent or overlapping) all-visible groups */
		while (m < n && arr[m].firstRowNumber <= hi)
		{
			uint64		mhi = arr[m].firstRowNumber + arr[m].rowCount;

			if (mhi > hi)
				hi = mhi;
			m++;
		}

		/*
		 * Blocks entirely within [lo, hi): ceil(lo/K) .. floor(hi/K) - 1. The
		 * window of groups this walk holds is [released, taken), and unclean
		 * counts the groups in it whose lock was busy or that hold a delete.
		 */
		b = (BlockNumber) ((lo + K - 1) / K);
		bend = (BlockNumber) (hi / K);
		taken = released = i;
		for (; b < bend; b++)
		{
			uint64		bstart = (uint64) b * K;
			uint64		bnext = bstart + K;

			/* take every group of the run that starts before the block ends */
			while (taken < m && arr[taken].firstRowNumber < bnext)
			{
				clean[taken] = vm_take_group(storageId, &arr[taken], &held[taken]);
				if (!clean[taken])
					unclean++;
				taken++;
			}

			/* give back every group that ends before the block starts */
			while (released < taken &&
				   arr[released].firstRowNumber + arr[released].rowCount <= bstart)
			{
				if (!clean[released])
					unclean--;
				vm_release_group(storageId, &arr[released], held[released]);
				released++;
			}

			if (unclean == 0)
			{
				PgColumnarVMSetVisible(rel, b);
				visibleRows += K;
			}
		}

		while (released < taken)
		{
			vm_release_group(storageId, &arr[released], held[released]);
			released++;
		}

		i = m;
	}

	pfree(held);
	pfree(clean);
	pfree(arr);
	return visibleRows;
}

/*
 * Both entry points below write to, or read, the visibility-map fork of a
 * caller-supplied relation. They are self-test helpers for the phase-1 VM work,
 * not user API, and they took an arbitrary regclass and checked nothing --
 * neither ownership, nor any ACL, nor that the relation was even columnar.
 *
 * CREATE FUNCTION grants EXECUTE to PUBLIC and the install script carried no
 * REVOKE, so any role with USAGE on the pgcolumnar schema could mark pages of
 * ANY relation all-visible, including a heap table it neither owned nor could
 * read. An index-only scan over that relation then skips the heap visibility
 * check and returns deleted rows: measured at 20,000 rows returned from a table
 * with 10,000 live ones (#558).
 *
 * PgColumnarRequireTableOwner is the check the rest of the tree already uses for
 * "you may act on this relation" -- columnar_vacuum.c and columnar_projection.c
 * call it at eleven sites. Ownership is the right bar here rather than a
 * server-file role, because these touch one named relation's storage.
 *
 * The install script also revokes EXECUTE from PUBLIC, but this check is the
 * load-bearing one: a REVOKE is undone by a later GRANT ... ON ALL FUNCTIONS IN
 * SCHEMA, and it does not protect an already installed extension until it is
 * upgraded.
 */

/*
 * pgcolumnar_vm_selftest(rel regclass, blk int) -> bool
 *		Phase-1 proof: set the all-visible bit for a synthetic block on a
 *		columnar relation, then read it back through the backend's own
 *		visibilitymap_get_status. Returns true iff the round trip succeeds,
 *		which validates that the custom VM write is layout-compatible with the
 *		reader the index-only-scan executor uses.
 */
Datum
pgcolumnar_vm_selftest(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	BlockNumber blk = (BlockNumber) PG_GETARG_INT32(1);
	Relation	rel;
	bool		before,
				after;

	PgColumnarRequireTableOwnerByOid(relid);

	if (!PgColumnarIsColumnarRelation(relid))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						get_rel_name(relid))));

	rel = table_open(relid, RowExclusiveLock);

	before = PgColumnarVMIsVisible(rel, blk);
	PgColumnarVMSetVisible(rel, blk);
	after = PgColumnarVMIsVisible(rel, blk);

	table_close(rel, RowExclusiveLock);

	/* success: not set before (fresh block), set after */
	PG_RETURN_BOOL(!before && after);
}

/*
 * pgcolumnar_vm_is_visible(rel regclass, blk int) -> bool
 *		Read-only probe: is the synthetic block marked all-visible in the VM
 *		fork? Used by the phase-3 tests to check that lazy vacuum sets bits for
 *		all-visible groups and clear-on-write removes them for modified ones.
 */
Datum
pgcolumnar_vm_is_visible(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	BlockNumber blk = (BlockNumber) PG_GETARG_INT32(1);
	Relation	rel;
	bool		vis;

	PgColumnarRequireTableOwnerByOid(relid);

	if (!PgColumnarIsColumnarRelation(relid))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						get_rel_name(relid))));

	rel = table_open(relid, AccessShareLock);
	vis = PgColumnarVMIsVisible(rel, blk);

	table_close(rel, AccessShareLock);
	PG_RETURN_BOOL(vis);
}

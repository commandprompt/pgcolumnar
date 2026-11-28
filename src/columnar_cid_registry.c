/*-------------------------------------------------------------------------
 *
 * columnar_cid_registry.c
 *		Which command of this transaction wrote each row and delete mark that
 *		this backend flushed to the catalog (#2247).
 *
 * Heap hides a row from the command that wrote it, and from earlier commands,
 * by comparing the row's cmin with the snapshot's curcid. A columnar row has no
 * cmin of its own: its row group is a catalog row written when the buffer is
 * flushed, under whatever command is current then, and a delete mark is a bit
 * in a bitmap that later commands merge into. So a scan cannot learn from the
 * catalog which command wrote a row or a mark of its own transaction.
 *
 * The write buffer and the delete-mark buffer record the writer's command id
 * as runs (columnar_write_state.c, columnar_delete_vector.c). When they flush,
 * they register those runs here. A fetch then hides a row or a mark that this
 * transaction wrote under a command id at or above its snapshot's curcid,
 * exactly as heap would. A row or mark not in the registry was written by
 * another transaction, or by a path that records no command id, and its
 * visibility is the catalog's alone, as before.
 *
 * Non-MVCC snapshots (dirty, self, any) see every write, as they do on heap:
 * the unique check and the trigger re-fetch depend on that.
 *
 * The registry is backend-local. A parallel worker never needs it, because
 * PostgreSQL runs no parallel plan in a command that writes, in a cursor, or
 * in a data-modifying CTE, so a worker's curcid is above every command id
 * registered here. The leader passes its highest registered command id to the
 * workers, and a worker refuses to run if that does not hold
 * (PgColumnarCidCheckWorker).
 *
 * Written fresh for pgColumnar.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_cid_registry.h"
#include "access/xact.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"

/* rows [firstRow, firstRow + nrows) of a storage, written by command `cid` */
typedef struct CidRowRun
{
	uint64		storageId;
	uint64		firstRow;
	uint64		nrows;
	CommandId	cid;
	SubTransactionId subid;		/* the subtransaction the flush ran under */
} CidRowRun;

/* the rows a flush marked deleted under command `cid`, bit i = startRow + i */
typedef struct CidMarkSet
{
	uint64		storageId;
	uint64		startRow;
	uint32		maskLen;
	char	   *mask;
	CommandId	cid;
	SubTransactionId subid;
} CidMarkSet;

static MemoryContext CidRegistryContext = NULL;
static List *CidRowRuns = NIL;
static List *CidMarkSets = NIL;
static bool CidAny = false;
static CommandId CidMax = FirstCommandId;

/* pgcolumnar.debug_registry_cid_skew: see PgColumnarCidLeaderMax */
int			pgcolumnar_debug_registry_cid_skew = 0;

static MemoryContext
cid_registry_context(void)
{
	if (CidRegistryContext == NULL)
		CidRegistryContext = AllocSetContextCreate(TopTransactionContext,
												   "columnar cid registry",
												   ALLOCSET_SMALL_SIZES);
	return CidRegistryContext;
}

static void
cid_note_max(CommandId cid)
{
	if (!CidAny || cid > CidMax)
		CidMax = cid;
	CidAny = true;
}

/*
 * PgColumnarCidVisible
 *		Can `snapshot` see a write this transaction made under command `cid`?
 *		Yes for a non-MVCC snapshot, and for a write with no recorded command.
 */
bool
PgColumnarCidVisible(Snapshot snapshot, CommandId cid)
{
	if (snapshot == NULL || !IsMVCCSnapshot(snapshot))
		return true;
	if (cid == InvalidCommandId)
		return true;
	return cid < snapshot->curcid;
}

/* can any registered write be hidden from this snapshot? */
static bool
cid_registry_may_hide(Snapshot snapshot)
{
	if (!CidAny || snapshot == NULL || !IsMVCCSnapshot(snapshot))
		return false;
	return CidMax >= snapshot->curcid;
}

void
PgColumnarCidRegisterRows(uint64 storageId, uint64 firstRow, uint64 nrows,
						  CommandId cid)
{
	MemoryContext old;
	CidRowRun  *r;

	if (nrows == 0 || cid == InvalidCommandId)
		return;
	old = MemoryContextSwitchTo(cid_registry_context());
	r = palloc(sizeof(CidRowRun));
	r->storageId = storageId;
	r->firstRow = firstRow;
	r->nrows = nrows;
	r->cid = cid;
	r->subid = GetCurrentSubTransactionId();
	CidRowRuns = lappend(CidRowRuns, r);
	MemoryContextSwitchTo(old);
	cid_note_max(cid);
}

void
PgColumnarCidRegisterMarks(uint64 storageId, uint64 startRow, const char *mask,
						   uint32 maskLen, CommandId cid)
{
	MemoryContext old;
	CidMarkSet *m;

	if (maskLen == 0 || cid == InvalidCommandId)
		return;
	old = MemoryContextSwitchTo(cid_registry_context());
	m = palloc(sizeof(CidMarkSet));
	m->storageId = storageId;
	m->startRow = startRow;
	m->maskLen = maskLen;
	m->mask = palloc(maskLen);
	memcpy(m->mask, mask, maskLen);
	m->cid = cid;
	m->subid = GetCurrentSubTransactionId();
	CidMarkSets = lappend(CidMarkSets, m);
	MemoryContextSwitchTo(old);
	cid_note_max(cid);
}

/*
 * PgColumnarCidRowHidden
 *		True when this transaction flushed the row under a command `snapshot`
 *		cannot see.
 */
bool
PgColumnarCidRowHidden(uint64 storageId, uint64 rowNumber, Snapshot snapshot)
{
	ListCell   *lc;

	if (!cid_registry_may_hide(snapshot))
		return false;
	foreach(lc, CidRowRuns)
	{
		CidRowRun  *r = (CidRowRun *) lfirst(lc);

		if (r->storageId == storageId && rowNumber >= r->firstRow &&
			rowNumber - r->firstRow < r->nrows)
			return !PgColumnarCidVisible(snapshot, r->cid);
	}
	return false;
}

/*
 * PgColumnarCidMarkHidden
 *		True when this transaction flushed a delete mark for the row under a
 *		command `snapshot` cannot see, so the row still reads as live to it. A
 *		transaction marks a row at most once, so the first match decides.
 */
bool
PgColumnarCidMarkHidden(uint64 storageId, uint64 rowNumber, Snapshot snapshot)
{
	ListCell   *lc;

	if (!cid_registry_may_hide(snapshot))
		return false;
	foreach(lc, CidMarkSets)
	{
		CidMarkSet *m = (CidMarkSet *) lfirst(lc);
		uint64		bit;

		if (m->storageId != storageId || rowNumber < m->startRow)
			continue;
		bit = rowNumber - m->startRow;
		if (bit >= (uint64) m->maskLen * 8)
			continue;
		if (dv_row_deleted(m->mask, m->maskLen, bit))
			return !PgColumnarCidVisible(snapshot, m->cid);
	}
	return false;
}

/*
 * PgColumnarCidLeaderMax
 *		The highest command id registered, for a parallel leader to hand to its
 *		workers; *any is false when nothing is registered.
 *
 *		pgcolumnar.debug_registry_cid_skew, when above zero, reports the
 *		current command id plus the skew instead. It exists so a test can show
 *		that a worker refuses a leader whose registry it would need.
 */
CommandId
PgColumnarCidLeaderMax(bool *any)
{
	if (pgcolumnar_debug_registry_cid_skew > 0)
	{
		*any = true;
		return GetCurrentCommandId(false) + (CommandId) pgcolumnar_debug_registry_cid_skew;
	}
	*any = CidAny;
	return CidMax;
}

/*
 * PgColumnarCidCheckWorker
 *		In a parallel worker: refuse to scan when the leader registered a write
 *		under a command id this worker's snapshot cannot see. The worker has no
 *		copy of the registry, so it would show such a write.
 */
void
PgColumnarCidCheckWorker(bool leaderAny, CommandId leaderMax, Snapshot snapshot)
{
	if (!leaderAny || snapshot == NULL || !IsMVCCSnapshot(snapshot))
		return;
	if (leaderMax >= snapshot->curcid)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("columnar: a parallel worker cannot hide this transaction's own writes"),
				 errdetail("The leader wrote under command %u, and the worker's snapshot sees commands below %u.",
						   leaderMax, snapshot->curcid)));
}

/* a subtransaction aborted: its flushes are undone, so forget them */
void
PgColumnarCidRegistryDiscardSubXact(SubTransactionId subid)
{
	List	   *keptRows = NIL;
	List	   *keptMarks = NIL;
	ListCell   *lc;
	MemoryContext old;

	if (CidRegistryContext == NULL)
		return;
	old = MemoryContextSwitchTo(CidRegistryContext);
	CidAny = false;
	CidMax = FirstCommandId;
	foreach(lc, CidRowRuns)
	{
		CidRowRun  *r = (CidRowRun *) lfirst(lc);

		if (r->subid == subid)
			continue;
		keptRows = lappend(keptRows, r);
		cid_note_max(r->cid);
	}
	foreach(lc, CidMarkSets)
	{
		CidMarkSet *m = (CidMarkSet *) lfirst(lc);

		if (m->subid == subid)
			continue;
		keptMarks = lappend(keptMarks, m);
		cid_note_max(m->cid);
	}
	MemoryContextSwitchTo(old);
	CidRowRuns = keptRows;
	CidMarkSets = keptMarks;
}

/* a subtransaction committed: its flushes now belong to the parent */
void
PgColumnarCidRegistryPromoteSubXact(SubTransactionId subid, SubTransactionId parent)
{
	ListCell   *lc;

	foreach(lc, CidRowRuns)
	{
		CidRowRun  *r = (CidRowRun *) lfirst(lc);

		if (r->subid == subid)
			r->subid = parent;
	}
	foreach(lc, CidMarkSets)
	{
		CidMarkSet *m = (CidMarkSet *) lfirst(lc);

		if (m->subid == subid)
			m->subid = parent;
	}
}

/* transaction end: the memory goes with TopTransactionContext */
void
PgColumnarCidRegistryReset(void)
{
	CidRegistryContext = NULL;
	CidRowRuns = NIL;
	CidMarkSets = NIL;
	CidAny = false;
	CidMax = FirstCommandId;
}

/*
 * What a parallel leader hands its workers (#2247): whether it registered any
 * own write, and the highest command id it registered under.
 */
typedef struct PgColumnarParallelCid
{
	bool		any;
	CommandId	max;
} PgColumnarParallelCid;

Size
PgColumnarParallelCidSize(void)
{
	return MAXALIGN(sizeof(PgColumnarParallelCid));
}

/* in the leader, while it sets up the parallel scan's shared memory */
void
PgColumnarParallelCidStore(void *at)
{
	PgColumnarParallelCid *pc = (PgColumnarParallelCid *) at;

	pc->max = PgColumnarCidLeaderMax(&pc->any);
}

/* in a worker, before it scans */
void
PgColumnarParallelCidCheck(const void *at, Snapshot snapshot)
{
	const PgColumnarParallelCid *pc = (const PgColumnarParallelCid *) at;

	PgColumnarCidCheckWorker(pc->any, pc->max, snapshot);
}

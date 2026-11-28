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
 * (columnar_write_state.c, columnar_delete_vector.c). When they flush, they
 * register here which rows and which marks each command wrote. A fetch then
 * hides a row or a mark that this transaction wrote under a command id at or
 * above its snapshot's curcid, exactly as heap would. A row or mark not in the
 * registry was written by another transaction, or by a path that records no
 * command id, and its visibility is the catalog's alone, as before.
 *
 * Non-MVCC snapshots (dirty, self, any) see every write, as they do on heap:
 * the unique check and the trigger re-fetch depend on that.
 *
 * A mark is stored as the rows it marked, not as its row group's bitmap: a
 * single-row DELETE in a loop registers a few bytes, not a group-sized mask
 * (review of #2269). A dense set is kept as a bitmap over the span it covers,
 * whichever is smaller.
 *
 * PARALLEL WORKERS GET A COPY. A STABLE function's query runs under its
 * caller's snapshot and may run in parallel, so a worker can need to hide the
 * caller's writes (review of #2269). Before workers can start -- at the start
 * of a plan that may run in parallel, and again when a parallel scan sets up
 * its shared memory -- the leader serializes the registry into a DSM segment
 * and publishes the segment's handle in a shared-memory slot of its own. A
 * worker reads its lock-group leader's slot and loads the copy before it
 * consults the registry, so it filters exactly as the leader does. The slots
 * need shared_preload_libraries, which pgColumnar requires; without it nothing
 * is published and a worker sees an empty registry.
 *
 * Written fresh for pgColumnar.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_cid_registry.h"
#include "access/parallel.h"
#include "access/twophase.h"
#include "access/xact.h"
#include "miscadmin.h"
#include "storage/dsm.h"
#include "storage/ipc.h"
#include "storage/proc.h"
#include "storage/shmem.h"
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

/*
 * The rows one flush marked deleted under command `cid`, all at or above `base`
 * and below base + span. Dense: a bitmap of span bits, bit i = base + i.
 * Otherwise: n sorted offsets from base.
 */
typedef struct CidMarkSet
{
	uint64		storageId;
	uint64		base;
	uint32		span;
	uint32		n;
	bool		dense;
	CommandId	cid;
	SubTransactionId subid;
	char	   *bits;			/* dense: (span + 7) / 8 bytes */
	uint32	   *offsets;		/* sparse: n entries */
} CidMarkSet;

static MemoryContext CidRegistryContext = NULL;
static List *CidRowRuns = NIL;
static List *CidMarkSets = NIL;
static bool CidAny = false;
static CommandId CidMax = FirstCommandId;
static bool CidWorkerLoaded = false;

/* one slot per PGPROC: the DSM handle of that backend's published registry */
typedef struct CidSlot
{
	dsm_handle	handle;
	uint32		len;
} CidSlot;

typedef struct CidShared
{
	int			nslots;
	CidSlot		slots[FLEXIBLE_ARRAY_MEMBER];
} CidShared;

static CidShared *CidSharedState = NULL;
static shmem_request_hook_type prev_shmem_request_hook = NULL;
static shmem_startup_hook_type prev_shmem_startup_hook = NULL;

static void cid_worker_load(void);

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
	if (snapshot == NULL || !IsMVCCSnapshot(snapshot))
		return false;
	if (IsParallelWorker() && !CidWorkerLoaded)
		cid_worker_load();
	if (!CidAny)
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

/* build a mark set from n sorted row numbers; in the current memory context */
static CidMarkSet *
cid_mark_set_make(uint64 storageId, const uint64 *rows, uint32 n, CommandId cid,
				  SubTransactionId subid)
{
	CidMarkSet *m = palloc0(sizeof(CidMarkSet));
	uint64		span = rows[n - 1] - rows[0] + 1;
	uint32		i;

	m->storageId = storageId;
	m->base = rows[0];
	m->n = n;
	m->cid = cid;
	m->subid = subid;
	Assert(span <= PG_UINT32_MAX);
	m->span = (uint32) span;
	m->dense = ((span + 7) / 8) < (uint64) n * sizeof(uint32);
	if (m->dense)
	{
		m->bits = palloc0((span + 7) / 8);
		for (i = 0; i < n; i++)
		{
			uint64		off = rows[i] - m->base;

			m->bits[off >> 3] |= (char) (1 << (off & 7));
		}
	}
	else
	{
		m->offsets = palloc(sizeof(uint32) * n);
		for (i = 0; i < n; i++)
			m->offsets[i] = (uint32) (rows[i] - m->base);
	}
	return m;
}

/*
 * PgColumnarCidRegisterMarkRows
 *		Register that command `cid` marked these rows deleted. `rows` holds n
 *		row numbers in ascending order, all of one row group.
 */
void
PgColumnarCidRegisterMarkRows(uint64 storageId, const uint64 *rows, uint32 n,
							  CommandId cid)
{
	MemoryContext old;

	if (n == 0 || cid == InvalidCommandId)
		return;
	old = MemoryContextSwitchTo(cid_registry_context());
	CidMarkSets = lappend(CidMarkSets,
						  cid_mark_set_make(storageId, rows, n, cid,
											GetCurrentSubTransactionId()));
	MemoryContextSwitchTo(old);
	cid_note_max(cid);
}

static bool
cid_mark_set_has(const CidMarkSet *m, uint64 rowNumber)
{
	uint64		off;
	uint32		lo,
				hi;

	if (rowNumber < m->base || rowNumber - m->base >= m->span)
		return false;
	off = rowNumber - m->base;
	if (m->dense)
		return (m->bits[off >> 3] >> (off & 7)) & 1;
	lo = 0;
	hi = m->n;
	while (lo < hi)
	{
		uint32		mid = lo + (hi - lo) / 2;

		if (m->offsets[mid] < off)
			lo = mid + 1;
		else
			hi = mid;
	}
	return lo < m->n && m->offsets[lo] == off;
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

		if (m->storageId == storageId && cid_mark_set_has(m, rowNumber))
			return !PgColumnarCidVisible(snapshot, m->cid);
	}
	return false;
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
		{
			pfree(r);
			continue;
		}
		keptRows = lappend(keptRows, r);
		cid_note_max(r->cid);
	}
	foreach(lc, CidMarkSets)
	{
		CidMarkSet *m = (CidMarkSet *) lfirst(lc);

		if (m->subid == subid)
		{
			if (m->bits)
				pfree(m->bits);
			if (m->offsets)
				pfree(m->offsets);
			pfree(m);
			continue;
		}
		keptMarks = lappend(keptMarks, m);
		cid_note_max(m->cid);
	}
	list_free(CidRowRuns);
	list_free(CidMarkSets);
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
	CidWorkerLoaded = false;
}

/* -------------------------------------------------------------------------
 * the copy for parallel workers
 * ------------------------------------------------------------------------- */

static int
cid_slot_count(void)
{
	return MaxBackends + NUM_AUXILIARY_PROCS + max_prepared_xacts;
}

static Size
cid_shared_size(void)
{
	return add_size(offsetof(CidShared, slots),
					mul_size(sizeof(CidSlot), cid_slot_count()));
}

static void
cid_shmem_request(void)
{
	if (prev_shmem_request_hook)
		prev_shmem_request_hook();
	RequestAddinShmemSpace(cid_shared_size());
}

static void
cid_shmem_startup(void)
{
	bool		found;

	if (prev_shmem_startup_hook)
		prev_shmem_startup_hook();
	LWLockAcquire(AddinShmemInitLock, LW_EXCLUSIVE);
	CidSharedState = ShmemInitStruct("pgcolumnar cid registry", cid_shared_size(), &found);
	if (!found)
	{
		int			i;

		CidSharedState->nslots = cid_slot_count();
		for (i = 0; i < CidSharedState->nslots; i++)
		{
			CidSharedState->slots[i].handle = DSM_HANDLE_INVALID;
			CidSharedState->slots[i].len = 0;
		}
	}
	LWLockRelease(AddinShmemInitLock);
}

/* _PG_init, under shared_preload_libraries */
void
PgColumnarCidRegistryShmemRegister(void)
{
	prev_shmem_request_hook = shmem_request_hook;
	shmem_request_hook = cid_shmem_request;
	prev_shmem_startup_hook = shmem_startup_hook;
	shmem_startup_hook = cid_shmem_startup;
}

static int
cid_slot_of(PGPROC *proc)
{
	int			i;

	if (CidSharedState == NULL || proc == NULL)
		return -1;
	i = (int) (proc - ProcGlobal->allProcs);
	return (i >= 0 && i < CidSharedState->nslots) ? i : -1;
}

/* append helpers for the serialized form */
static char *
cid_put(char *p, const void *v, Size n)
{
	memcpy(p, v, n);
	return p + n;
}

static const char *
cid_get(const char *p, void *v, Size n)
{
	memcpy(v, p, n);
	return p + n;
}

static Size
cid_mark_payload(const CidMarkSet *m)
{
	return m->dense ? (m->span + 7) / 8 : sizeof(uint32) * m->n;
}

/*
 * PgColumnarCidRegistryPublish
 *		In a parallel leader: put a copy of the registry where this backend's
 *		workers will find it. An empty registry publishes no segment.
 */
void
PgColumnarCidRegistryPublish(void)
{
	int			slot = cid_slot_of(MyProc);
	Size		len;
	ListCell   *lc;
	dsm_segment *seg;
	char	   *p;
	uint32		nr = (uint32) list_length(CidRowRuns);
	uint32		nm = (uint32) list_length(CidMarkSets);

	if (slot < 0 || IsParallelWorker())
		return;
	if (nr == 0 && nm == 0)
	{
		CidSharedState->slots[slot].handle = DSM_HANDLE_INVALID;
		pg_write_barrier();
		return;
	}

	len = 2 * sizeof(uint32);
	len += (Size) nr * (3 * sizeof(uint64) + sizeof(CommandId));
	foreach(lc, CidMarkSets)
	{
		CidMarkSet *m = (CidMarkSet *) lfirst(lc);

		len += 2 * sizeof(uint64) + sizeof(CommandId) + 2 * sizeof(uint32) + 1 +
			cid_mark_payload(m);
	}

	/* lives to the end of this query, which the workers do not outlive */
	seg = dsm_create(len, 0);
	p = dsm_segment_address(seg);
	p = cid_put(p, &nr, sizeof(nr));
	p = cid_put(p, &nm, sizeof(nm));
	foreach(lc, CidRowRuns)
	{
		CidRowRun  *r = (CidRowRun *) lfirst(lc);

		p = cid_put(p, &r->storageId, sizeof(uint64));
		p = cid_put(p, &r->firstRow, sizeof(uint64));
		p = cid_put(p, &r->nrows, sizeof(uint64));
		p = cid_put(p, &r->cid, sizeof(CommandId));
	}
	foreach(lc, CidMarkSets)
	{
		CidMarkSet *m = (CidMarkSet *) lfirst(lc);
		char		dense = m->dense ? 1 : 0;

		p = cid_put(p, &m->storageId, sizeof(uint64));
		p = cid_put(p, &m->base, sizeof(uint64));
		p = cid_put(p, &m->cid, sizeof(CommandId));
		p = cid_put(p, &m->span, sizeof(uint32));
		p = cid_put(p, &m->n, sizeof(uint32));
		p = cid_put(p, &dense, 1);
		p = cid_put(p, m->dense ? (void *) m->bits : (void *) m->offsets,
					cid_mark_payload(m));
	}
	Assert(p == (char *) dsm_segment_address(seg) + len);

	CidSharedState->slots[slot].len = (uint32) len;
	pg_write_barrier();
	CidSharedState->slots[slot].handle = dsm_segment_handle(seg);
}

/* in a parallel worker: load the leader's copy, once */
static void
cid_worker_load(void)
{
	int			slot = cid_slot_of(MyProc->lockGroupLeader);
	dsm_handle	h;
	dsm_segment *seg;
	const char *p;
	uint32		nr,
				nm,
				i;
	MemoryContext old;

	CidWorkerLoaded = true;
	if (slot < 0)
		return;
	h = CidSharedState->slots[slot].handle;
	pg_read_barrier();
	if (h == DSM_HANDLE_INVALID)
		return;
	seg = dsm_attach(h);
	if (seg == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("columnar: a parallel worker could not attach the leader's own-write registry")));

	old = MemoryContextSwitchTo(cid_registry_context());
	p = dsm_segment_address(seg);
	p = cid_get(p, &nr, sizeof(nr));
	p = cid_get(p, &nm, sizeof(nm));
	for (i = 0; i < nr; i++)
	{
		CidRowRun  *r = palloc(sizeof(CidRowRun));

		p = cid_get(p, &r->storageId, sizeof(uint64));
		p = cid_get(p, &r->firstRow, sizeof(uint64));
		p = cid_get(p, &r->nrows, sizeof(uint64));
		p = cid_get(p, &r->cid, sizeof(CommandId));
		r->subid = InvalidSubTransactionId;
		CidRowRuns = lappend(CidRowRuns, r);
		cid_note_max(r->cid);
	}
	for (i = 0; i < nm; i++)
	{
		CidMarkSet *m = palloc0(sizeof(CidMarkSet));
		char		dense;
		Size		payload;

		p = cid_get(p, &m->storageId, sizeof(uint64));
		p = cid_get(p, &m->base, sizeof(uint64));
		p = cid_get(p, &m->cid, sizeof(CommandId));
		p = cid_get(p, &m->span, sizeof(uint32));
		p = cid_get(p, &m->n, sizeof(uint32));
		p = cid_get(p, &dense, 1);
		m->dense = (dense != 0);
		m->subid = InvalidSubTransactionId;
		payload = cid_mark_payload(m);
		if (m->dense)
		{
			m->bits = palloc(payload);
			p = cid_get(p, m->bits, payload);
		}
		else
		{
			m->offsets = palloc(payload);
			p = cid_get(p, m->offsets, payload);
		}
		CidMarkSets = lappend(CidMarkSets, m);
		cid_note_max(m->cid);
	}
	MemoryContextSwitchTo(old);
	dsm_detach(seg);
}

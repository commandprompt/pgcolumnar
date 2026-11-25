/*-------------------------------------------------------------------------
 * columnar_autovacuum.c
 *		A background worker that runs the maintenance autovacuum cannot reach
 *		(#415): the columnar space-reclaim and re-clustering that live in
 *		extension functions rather than table-AM callbacks.
 *
 * Shape mirrors core autovacuum: a launcher (registered from _PG_init, so it
 * needs shared_preload_libraries -- which pgColumnar already requires) wakes on
 * a naptime and starts one short-lived worker per database; each worker asks
 * pgcolumnar.maintenance_due(rel) which columnar tables want attention and runs
 * the recommended verb.
 *
 * TWO INVARIANTS make this safe to run unattended:
 *
 * 1. Only the ShareUpdateExclusiveLock verbs -- pgcolumnar.compact_rewrite,
 *    pgcolumnar.recluster (now self-gating, #415 part A) and, when
 *    pgcolumnar.autovacuum_expire is on, pgcolumnar.expire (#1290) -- are ever
 *    called.
 *    Never vacuum()/vacuum_sorted()/cluster(), which take AccessExclusiveLock.
 *    SUEL does not block readers or ordinary writers, so the daemon cannot block
 *    production by construction.
 *
 * 2. Autovacuum's yield: the worker sets PROC_IS_AUTOVACUUM, so when a backend
 *    queues for a lock that conflicts with the worker's SUEL, core's lock
 *    manager cancels the worker -- the maintenance op takes a query-cancel,
 *    releases its lock, the user's statement proceeds, and the worker moves to
 *    the next table. Each op runs in its own transaction inside PG_TRY, so a
 *    cancel aborts only that op.
 *
 * Off by default (pgcolumnar.autovacuum = off): a new autonomous-maintenance
 * daemon earns its keep opt-in, and even on it only runs the non-blocking verbs.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <unistd.h>

#include "access/genam.h"
#include "access/heapam.h"
#include "access/table.h"
#include "access/xact.h"
#include "catalog/pg_database.h"
#include "executor/spi.h"
#include "miscadmin.h"
#include "pgstat.h"
#include "postmaster/bgworker.h"
#include "postmaster/interrupt.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "storage/lmgr.h"
#include "storage/proc.h"
#include "storage/procarray.h"
#include "tcop/tcopprot.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"

#include "columnar.h"
#include "columnar_curve.h"

/* GUCs (defined in columnar_tableam.c _PG_init, declared in columnar.h) */
extern bool pgcolumnar_autovacuum;
extern int	pgcolumnar_autovacuum_naptime;
extern double pgcolumnar_autovacuum_compact_threshold;
extern double pgcolumnar_autovacuum_recluster_threshold;

PGDLLEXPORT void pgcolumnar_av_launcher_main(Datum arg);
PGDLLEXPORT void pgcolumnar_av_worker_main(Datum arg);

/*
 * Register the launcher. Called from _PG_init only when
 * process_shared_preload_libraries_in_progress -- a server run without preload
 * loses the daemon and nothing else.
 */
void
PgColumnarAutovacuumRegister(void)
{
	BackgroundWorker bw;

	memset(&bw, 0, sizeof(bw));
	bw.bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION;
	bw.bgw_start_time = BgWorkerStart_RecoveryFinished;
	/* a crashed launcher restarts after a minute, never in a tight loop */
	bw.bgw_restart_time = 60;
	strlcpy(bw.bgw_library_name, "pgcolumnar", BGW_MAXLEN);
	strlcpy(bw.bgw_function_name, "pgcolumnar_av_launcher_main", BGW_MAXLEN);
	strlcpy(bw.bgw_name, "pgcolumnar autovacuum launcher", BGW_MAXLEN);
	strlcpy(bw.bgw_type, "pgcolumnar autovacuum launcher", BGW_MAXLEN);
	bw.bgw_main_arg = (Datum) 0;
	bw.bgw_notify_pid = 0;
	RegisterBackgroundWorker(&bw);
}

/* Collect the OIDs of databases that accept connections and are not templates. */
static List *
av_database_list(void)
{
	List	   *dbs = NIL;
	Relation	rel;
	TableScanDesc scan;
	HeapTuple	tup;

	StartTransactionCommand();
	rel = table_open(DatabaseRelationId, AccessShareLock);
	scan = table_beginscan_catalog(rel, 0, NULL);
	while ((tup = heap_getnext(scan, ForwardScanDirection)) != NULL)
	{
		Form_pg_database db = (Form_pg_database) GETSTRUCT(tup);

		if (db->datallowconn && !db->datistemplate)
		{
			MemoryContext old = MemoryContextSwitchTo(TopMemoryContext);

#if PG_VERSION_NUM >= 120000
			dbs = lappend_oid(dbs, db->oid);
#else
			dbs = lappend_oid(dbs, HeapTupleGetOid(tup));
#endif
			MemoryContextSwitchTo(old);
		}
	}
	table_endscan(scan);
	table_close(rel, AccessShareLock);
	CommitTransactionCommand();
	return dbs;
}

/* Start one worker for `dbid` and wait for it to finish. */
static void
av_run_worker_for_db(Oid dbid)
{
	BackgroundWorker bw;
	BackgroundWorkerHandle *handle;
	BgwHandleStatus status;
	pid_t		pid;

	memset(&bw, 0, sizeof(bw));
	bw.bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION;
	bw.bgw_start_time = BgWorkerStart_RecoveryFinished;
	bw.bgw_restart_time = BGW_NEVER_RESTART;		/* one-shot per naptime */
	strlcpy(bw.bgw_library_name, "pgcolumnar", BGW_MAXLEN);
	strlcpy(bw.bgw_function_name, "pgcolumnar_av_worker_main", BGW_MAXLEN);
	strlcpy(bw.bgw_name, "pgcolumnar autovacuum worker", BGW_MAXLEN);
	strlcpy(bw.bgw_type, "pgcolumnar autovacuum worker", BGW_MAXLEN);
	bw.bgw_main_arg = (Datum) 0;
	memcpy(bw.bgw_extra, &dbid, sizeof(Oid));
	bw.bgw_notify_pid = MyProcPid;

	if (!RegisterDynamicBackgroundWorker(&bw, &handle))
	{
		/* worker slots exhausted this cycle; try again next naptime */
		elog(DEBUG1, "pgcolumnar autovacuum: no worker slot for database %u", dbid);
		return;
	}
	status = WaitForBackgroundWorkerStartup(handle, &pid);
	if (status != BGWH_STARTED)
		return;
	(void) WaitForBackgroundWorkerShutdown(handle);
}

/*
 * The launcher. Connects to no database (shared-catalog access only), and on
 * each naptime -- when enabled -- runs one worker per database in turn.
 */
void
pgcolumnar_av_launcher_main(Datum arg)
{
	pqsignal(SIGHUP, SignalHandlerForConfigReload);
	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();
	BackgroundWorkerInitializeConnection(NULL, NULL, 0);

	for (;;)
	{
		int			rc;

		rc = WaitLatch(MyLatch,
					   WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
					   (long) pgcolumnar_autovacuum_naptime * 1000L,
					   PG_WAIT_EXTENSION);
		if (rc & WL_LATCH_SET)
			ResetLatch(MyLatch);
		CHECK_FOR_INTERRUPTS();		/* SIGTERM via die() exits here */

		if (ConfigReloadPending)
		{
			ConfigReloadPending = false;
			ProcessConfigFile(PGC_SIGHUP);
		}

		if (!pgcolumnar_autovacuum)
			continue;

		{
			List	   *dbs = av_database_list();
			ListCell   *lc;

			foreach(lc, dbs)
			{
				CHECK_FOR_INTERRUPTS();
				if (!pgcolumnar_autovacuum)
					break;
				av_run_worker_for_db(lfirst_oid(lc));
			}
			list_free(dbs);
		}
	}
}

/* Mark this worker as autovacuum, so a lock waiter can cancel it (the yield). */
static void
av_mark_as_autovacuum(void)
{
#ifdef PROC_IS_AUTOVACUUM
	LWLockAcquire(ProcArrayLock, LW_EXCLUSIVE);
	MyProc->statusFlags |= PROC_IS_AUTOVACUUM;
	ProcGlobal->statusFlags[MyProc->pgxactoff] = MyProc->statusFlags;
	LWLockRelease(ProcArrayLock);
#endif
}

/*
 * Run pgcolumnar.expire on one table that declares a retention (#1290), in its
 * own transaction inside PG_TRY.
 *
 * ITS OWN TRANSACTION, NOT av_maintain_one's: expire deletes rows and can fail
 * on its own account -- the declared retention column was dropped, or its type
 * changed -- and a failure here must not roll back a compact_rewrite that was
 * due on the same table. It runs FIRST, so a compaction does not rewrite row
 * groups that are about to be retired.
 *
 * Only reached when pgcolumnar.autovacuum_expire is on. That switch is off by
 * default (the owner's decision on #1290): turning the daemon on must not
 * start deleting rows by itself.
 */
static void
av_expire_one(const char *qualname)
{
	StartTransactionCommand();
	PushActiveSnapshot(GetTransactionSnapshot());
	SetCurrentStatementStartTimestamp();

	PG_TRY();
	{
		char		q[512];
		int64		retired = 0;

		if (SPI_connect() != SPI_OK_CONNECT)
			elog(ERROR, "pgcolumnar autovacuum: SPI_connect failed");

		snprintf(q, sizeof(q), "SELECT pgcolumnar.expire(%s)",
				 quote_literal_cstr(qualname));
		if (SPI_execute(q, false, 1) == SPI_OK_SELECT && SPI_processed == 1)
		{
			bool		isnull;
			Datum		d = SPI_getbinval(SPI_tuptable->vals[0],
										 SPI_tuptable->tupdesc, 1, &isnull);

			if (!isnull)
				retired = DatumGetInt64(d);
		}
		/*
		 * LOG only when something was retired: a line per retention table per
		 * pass, forever, is noise an administrator learns to ignore
		 * (OffgridwithJD on #1307). av_maintain_one logs only a verb that ran.
		 */
		elog(retired > 0 ? LOG : DEBUG1,
			 "pgcolumnar autovacuum: expire %s retired " INT64_FORMAT " row groups",
			 qualname, retired);

		SPI_finish();
		PopActiveSnapshot();
		CommitTransactionCommand();
	}
	PG_CATCH();
	{
		MemoryContext ecxt = MemoryContextSwitchTo(TopMemoryContext);
		ErrorData  *ed = CopyErrorData();

		elog(LOG, "pgcolumnar autovacuum: skipped expire on %s: %s", qualname, ed->message);
		FreeErrorData(ed);
		MemoryContextSwitchTo(ecxt);

		FlushErrorState();
		AbortCurrentTransaction();
	}
	PG_END_TRY();
}

/*
 * Run the due maintenance verbs on one table, all in one transaction inside
 * PG_TRY: a query-cancel (the lock yield) or any error aborts just this table's
 * pass and the caller continues to the next table. compact_rewrite keeps the
 * storage-row lock to commit, so a recluster after it in this transaction can
 * deadlock with a writer (#2194).
 */
static void
av_maintain_one(const char *qualname)
{
	StartTransactionCommand();
	PushActiveSnapshot(GetTransactionSnapshot());
	SetCurrentStatementStartTimestamp();

	PG_TRY();
	{
		bool		compactDue = false;
		bool		reclusterDue = false;
		char	   *sortKey = NULL;
		char	   *sortedKind = NULL;
		const char *reclusterVerb;
		Oid			argtypes[3] = {REGCLASSOID, FLOAT8OID, FLOAT8OID};
		Datum		argvals[3];
		char		q[512];

		argvals[0] = DirectFunctionCall1(regclassin, CStringGetDatum(qualname));
		argvals[1] = Float8GetDatum(pgcolumnar_autovacuum_compact_threshold);
		argvals[2] = Float8GetDatum(pgcolumnar_autovacuum_recluster_threshold);

		if (SPI_connect() != SPI_OK_CONNECT)
			elog(ERROR, "pgcolumnar autovacuum: SPI_connect failed");

		/*
		 * sorted_kind comes from sort_status, alongside the verdict, because the
		 * daemon has to dispatch on THE CURVE THE TABLE IS ON (#889). Reading it
		 * here rather than adding an output column to maintenance_due keeps that
		 * function's signature -- and so the upgrade path -- unchanged; both are
		 * SECURITY DEFINER reports over the same storage row, and this worker is
		 * a superuser, so neither adds an access question.
		 */
		if (SPI_execute_with_args(
				"SELECT m.compact_rewrite_due, m.recluster_due, "
				"       array_to_string(m.sort_key, ','), s.sorted_kind "
				"FROM pgcolumnar.maintenance_due($1, $2, $3) m, "
				"     pgcolumnar.sort_status($1) s",
				3, argtypes, argvals, NULL, true, 1) == SPI_OK_SELECT &&
			SPI_processed == 1)
		{
			bool		isnull;
			Datum		d;

			d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull);
			compactDue = !isnull && DatumGetBool(d);
			d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 2, &isnull);
			reclusterDue = !isnull && DatumGetBool(d);
			d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 3, &isnull);
			if (!isnull)
				sortKey = pstrdup(TextDatumGetCString(d));
			d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 4, &isnull);
			if (!isnull)
				sortedKind = pstrdup(TextDatumGetCString(d));
		}

		/* compact_rewrite: online space reclaim (SUEL) */
		if (compactDue)
		{
			snprintf(q, sizeof(q),
					 "SELECT pgcolumnar.compact_rewrite(%s, %g)",
					 quote_literal_cstr(qualname),
					 pgcolumnar_autovacuum_compact_threshold);
			(void) SPI_execute(q, false, 0);
			elog(LOG, "pgcolumnar autovacuum: compact_rewrite %s", qualname);
		}

		/*
		 * recluster: only when the table has a recorded/declared key (else the
		 * daemon cannot know the columns). The self-gating recluster (#415) is
		 * a no-op when nothing decayed, so calling it here is cheap when the
		 * gate was optimistic.
		 */
		if (reclusterDue && sortKey != NULL && sortKey[0] != '\0')
		{
			/*
			 * DISPATCH ON THE RECORDED KIND (#889). This line hard-coded
			 * pgcolumnar.recluster, so a Hilbert table was reclustered by the
			 * Z-order verb and came back relabelled 'zorder' -- the daemon
			 * converting a layout the user chose, on a timer, with nothing in
			 * the log to say so. Plain recluster() now maintains the recorded
			 * curve on a matching key, so this is belt and braces; it is written
			 * out anyway because "the daemon preserves the curve" should be
			 * readable HERE, at the call the ruling is about, and not depend on
			 * a rule two modules away.
			 */
			reclusterVerb =
				(sortedKind != NULL &&
				 strcmp(sortedKind, COLUMNAR_CURVE_HILBERT) == 0)
				? "pgcolumnar.recluster_hilbert" : "pgcolumnar.recluster";

			snprintf(q, sizeof(q),
					 "SELECT %s(%s, VARIADIC string_to_array(%s, ',')::name[])",
					 reclusterVerb,
					 quote_literal_cstr(qualname),
					 quote_literal_cstr(sortKey));
			(void) SPI_execute(q, false, 0);
			elog(LOG, "pgcolumnar autovacuum: recluster %s by (%s) on the %s curve",
				 qualname, sortKey,
				 sortedKind != NULL ? sortedKind : COLUMNAR_CURVE_ZORDER);
		}

		SPI_finish();
		PopActiveSnapshot();
		CommitTransactionCommand();
	}
	PG_CATCH();
	{
		/*
		 * A cancel (the lock yield) or any error: roll this op back and keep
		 * going. The where-it-failed line is what an administrator acts on.
		 */
		MemoryContext ecxt = MemoryContextSwitchTo(TopMemoryContext);
		ErrorData  *ed = CopyErrorData();

		elog(LOG, "pgcolumnar autovacuum: skipped %s: %s", qualname, ed->message);
		FreeErrorData(ed);
		MemoryContextSwitchTo(ecxt);

		FlushErrorState();
		AbortCurrentTransaction();
	}
	PG_END_TRY();
}

/*
 * The per-database worker. Connects, marks itself autovacuum, enumerates
 * columnar tables, and maintains each one.
 */
void
pgcolumnar_av_worker_main(Datum arg)
{
	Oid			dbid;
	uint32		conn_flags = 0;
	List	   *tables = NIL;
	List	   *hasTtl = NIL;
	ListCell   *lc;
	ListCell   *lt;

	memcpy(&dbid, MyBgworkerEntry->bgw_extra, sizeof(Oid));

	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();

#if PG_VERSION_NUM >= 170000
	conn_flags |= BGWORKER_BYPASS_ROLELOGINCHECK;
#endif
	/* InvalidOid role: the bootstrap superuser, like a real autovacuum worker */
	BackgroundWorkerInitializeConnectionByOid(dbid, InvalidOid, conn_flags);

	av_mark_as_autovacuum();

	/*
	 * Snapshot the columnar-table list once, in its own transaction, with
	 * whether each declares a retention. Both halves must be set, as
	 * PgColumnarReadTtl reads it: a half declaration is no retention, and
	 * calling expire on it would only log an error every pass.
	 */
	StartTransactionCommand();
	PushActiveSnapshot(GetTransactionSnapshot());
	if (SPI_connect() == SPI_OK_CONNECT)
	{
		int			ret = SPI_execute(
			"SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname), "
			"       COALESCE(o.ttl_column IS NOT NULL AND o.ttl_interval IS NOT NULL, false) "
			"FROM pg_class c "
			"JOIN pg_am a ON a.oid = c.relam "
			"JOIN pg_namespace n ON n.oid = c.relnamespace "
			"LEFT JOIN pgcolumnar.options o ON o.regclass = c.oid "
			"WHERE a.amname = 'pgcolumnar' AND c.relkind = 'r' "
			"ORDER BY c.oid",
			true, 0);
		if (ret == SPI_OK_SELECT)
		{
			uint64		i;

			for (i = 0; i < SPI_processed; i++)
			{
				bool		isnull;
				Datum		d = SPI_getbinval(SPI_tuptable->vals[i],
											 SPI_tuptable->tupdesc, 1, &isnull);

				if (!isnull)
				{
					bool		ttlnull;
					Datum		t = SPI_getbinval(SPI_tuptable->vals[i],
												 SPI_tuptable->tupdesc, 2, &ttlnull);
					MemoryContext old = MemoryContextSwitchTo(TopMemoryContext);

					tables = lappend(tables, pstrdup(TextDatumGetCString(d)));
					hasTtl = lappend_int(hasTtl, !ttlnull && DatumGetBool(t));
					MemoryContextSwitchTo(old);
				}
			}
		}
		SPI_finish();
	}
	PopActiveSnapshot();
	CommitTransactionCommand();

	forboth(lc, tables, lt, hasTtl)
	{
		CHECK_FOR_INTERRUPTS();
		if (pgcolumnar_autovacuum_expire && lfirst_int(lt))
			av_expire_one((const char *) lfirst(lc));
		CHECK_FOR_INTERRUPTS();
		av_maintain_one((const char *) lfirst(lc));
	}
	/* worker exits; the launcher starts a fresh one next naptime */
}

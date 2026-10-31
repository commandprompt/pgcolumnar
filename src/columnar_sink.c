/*-------------------------------------------------------------------------
 * columnar_sink.c
 *		Local byte sink for the export writers (#394 step 1).
 *
 * Fixes the two defects the write-path mapping found (PR #604): all 17 fwrite
 * sites ignored their return values (a disk-full mid-export was detected only
 * if the final flush failed), and serial exports wrote directly to the final
 * path, leaving a partial file there on error. Every write is checked here,
 * at the call; the bytes go to <path>.tmp.<pid>; and the final name appears
 * only through finish()'s durable rename, so a reader never sees a partial
 * file under the name it trusts - the same appears-whole-or-not-at-all
 * property the future remote sink gets from multipart completion.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <unistd.h>

#include "access/xact.h"
#include "lib/ilist.h"
#include "miscadmin.h"
#include "storage/fd.h"
#include "utils/memutils.h"
#include "storage/ipc.h"

#include "columnar_objstore.h"
#include "columnar_sink.h"

int			pgcolumnar_sink_fail_after = -1;	/* dev injection; see header */
int			pgcolumnar_export_group_bytes = PGC_EXPORT_GROUP_BYTES_MAX;	/* #1811 */

struct PqSink
{
	/* local */
	FILE	   *f;
	char	   *finalPath;
	char	   *tmpPath;
	int64		written;
	/* remote (#394): NULL api => local sink */
	const PgColumnarObjStoreApi *api;
	PgColumnarObjSink *rsink;
	dlist_node	openNode;		/* on sink_open_remote while rsink is live */
};

/*
 * Remote sinks with an upload in progress (#1722). A FATAL exit (SIGTERM, as
 * pexport_cleanup sends each parallel_export worker on cancel, or
 * pg_terminate_backend) calls proc_exit without unwinding, so the writers'
 * PG_CATCH never reaches PgColumnarSinkAbort and the multipart upload stays
 * open on the store, billed until a lifecycle rule reaps it. A
 * before_shmem_exit callback aborts whatever is still on this list. It is
 * registered after InitPostgres's ShutdownPostgres, so it runs first, while
 * the transaction and the module's handles are still intact. Transaction end
 * empties the list: every writer finishes or aborts its sink inside its own
 * statement, so an entry still listed then belongs to memory being released.
 */
static dlist_head sink_open_remote = DLIST_STATIC_INIT(sink_open_remote);
static bool sink_exit_registered = false;

static void
sink_abort_open_at_exit(int code, Datum arg)
{
	dlist_mutable_iter it;

	/*
	 * The module's abort never raises through PG_TRY, but under proc_exit an
	 * ERROR is promoted to FATAL, which no CATCH sees. A failed DELETE then
	 * ends this callback with one more FATAL line; shmem_exit has already
	 * popped it, so the remaining callbacks still run.
	 */
	dlist_foreach_modify(it, &sink_open_remote)
	{
		PqSink	   *snk = dlist_container(PqSink, openNode, it.cur);

		dlist_delete(&snk->openNode);
		snk->api->sink_abort(snk->rsink);
		snk->rsink = NULL;
	}
}

static void
sink_xact_callback(XactEvent event, void *arg)
{
	switch (event)
	{
		case XACT_EVENT_COMMIT:
		case XACT_EVENT_PARALLEL_COMMIT:
		case XACT_EVENT_ABORT:
		case XACT_EVENT_PARALLEL_ABORT:
		case XACT_EVENT_PREPARE:
			dlist_init(&sink_open_remote);
			break;
		default:
			break;
	}
}

PqSink *
PgColumnarSinkOpenLocal(const char *path)
{
	PqSink	   *snk = (PqSink *) palloc0(sizeof(PqSink));

	snk->finalPath = pstrdup(path);
	snk->tmpPath = psprintf("%s.tmp.%d", path, MyProcPid);
	snk->f = AllocateFile(snk->tmpPath, PG_BINARY_W);
	if (snk->f == NULL)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not open file \"%s\" for writing: %m",
						snk->tmpPath)));
	return snk;
}

/*
 * The seam the exporters call (#394): a local path keeps step 1's
 * temp-and-rename exactly; a remote URL routes through the object-store
 * module, which is loaded on first use and gives the SAME
 * nothing-visible-before-finish property from multipart completion.
 */
PqSink *
PgColumnarSinkOpen(const char *path)
{
	if (PgColumnarPathIsRemote(path))
	{
		const PgColumnarObjStoreApi *api = PgColumnarObjStoreGet();
		PqSink	   *snk;

		if (api == NULL)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar: writing \"%s\" requires the object-store module",
							path),
					 errdetail("Object storage support is a separate library, "
							   "pgcolumnar_objstore, which is not installed.")));
		if (!api->handles_url(path))
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar: writing \"%s\" is not supported", path),
					 errdetail("The installed object-store module handles no "
							   "such URL scheme.")));
		snk = (PqSink *) palloc0(sizeof(PqSink));
		snk->finalPath = pstrdup(path);
		snk->api = api;
		snk->rsink = api->sink_create(path, NULL);
		if (!sink_exit_registered)
		{
			before_shmem_exit(sink_abort_open_at_exit, (Datum) 0);
			RegisterXactCallback(sink_xact_callback, NULL);
			sink_exit_registered = true;
		}
		dlist_push_head(&sink_open_remote, &snk->openNode);
		return snk;
	}
	return PgColumnarSinkOpenLocal(path);
}

void
PgColumnarSinkWrite(PqSink *snk, const void *buf, size_t n)
{
	size_t		wrote;

	if (n == 0)
		return;

	/*
	 * Fault injection (dev) is shared: it fails the SAME way for both sinks,
	 * so the remote abort path is exercised by the same GUC the local one is.
	 */
	if (pgcolumnar_sink_fail_after >= 0 &&
		snk->written + (int64) n > (int64) pgcolumnar_sink_fail_after)
		ereport(ERROR,
				(errcode(ERRCODE_DISK_FULL),
				 errmsg("columnar: export write failed (injected fault)"),
				 errdetail("The export had written %lld bytes.",
						   (long long) snk->written)));

	if (snk->api != NULL)
		snk->api->sink_write(snk->rsink, buf, n);
	else
	{
		wrote = fwrite(buf, 1, n, snk->f);
		if (wrote != n)
		{
			/*
			 * A short fwrite leaves the real cause in errno via the stream
			 * error; a clean short count with no error is still a failed
			 * write, reported as the device-full condition it usually is.
			 */
			if (errno == 0)
				errno = ENOSPC;
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not write to \"%s\": %m", snk->tmpPath),
					 errdetail("The export had written %lld bytes.",
							   (long long) snk->written)));
		}
	}
	snk->written += (int64) n;
}

void
PgColumnarSinkFinish(PqSink *snk)
{
	FILE	   *f;

	if (snk->api != NULL)
	{
		snk->api->sink_finish(snk->rsink);
		/* only now: a complete that raised leaves it listed for the abort */
		dlist_delete(&snk->openNode);
		snk->rsink = NULL;
		return;
	}
	f = snk->f;
	snk->f = NULL;
	if (fflush(f) != 0 || pg_fsync(fileno(f)) != 0)
	{
		int			save_errno = errno;

		FreeFile(f);
		errno = save_errno;
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not flush \"%s\": %m", snk->tmpPath)));
	}
	if (FreeFile(f) != 0)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not write file \"%s\": %m", snk->tmpPath)));

	/* fsyncs the new name and its directory; raises on failure */
	durable_rename(snk->tmpPath, snk->finalPath, ERROR);
}

void
PgColumnarSinkAbort(PqSink *snk)
{
	if (snk == NULL)
		return;
	if (snk->api != NULL)
	{
		if (snk->rsink != NULL)
		{
			dlist_delete(&snk->openNode);
			snk->api->sink_abort(snk->rsink);
			snk->rsink = NULL;
		}
		return;
	}
	if (snk->f != NULL)
	{
		FreeFile(snk->f);
		snk->f = NULL;
	}
	(void) unlink(snk->tmpPath);
}

/*
 * PgColumnarCleanupAndRethrow
 *		Called from a PG_CATCH. The error being handled is copied out and the
 *		error stack emptied BEFORE the cleanup runs, so whatever the cleanup
 *		raises and swallows (the module's best-effort DELETEs flush the error
 *		stack, and a second cancel lands exactly there, #1741) cannot take the
 *		handled error with it. An error the cleanup does not swallow ends the
 *		cleanup early and is dropped: the error to report is the one being
 *		handled. That one is then re-thrown, as PG_RE_THROW would have.
 */
void
PgColumnarCleanupAndRethrow(void (*cleanup) (void *arg), void *arg)
{
	/* in a transaction for every caller; the copy only needs to outlive this */
	MemoryContext keep = TopTransactionContext ? TopTransactionContext : TopMemoryContext;
	ErrorData  *edata;

	MemoryContextSwitchTo(keep);
	edata = CopyErrorData();
	FlushErrorState();
	PG_TRY();
	{
		cleanup(arg);
	}
	PG_CATCH();
	{
		MemoryContextSwitchTo(keep);
		FlushErrorState();
	}
	PG_END_TRY();
	ReThrowError(edata);
}

static void
sink_abort_cleanup(void *arg)
{
	PgColumnarSinkAbort((PqSink *) arg);
}

void
PgColumnarSinkAbortAndRethrow(PqSink *snk)
{
	PgColumnarCleanupAndRethrow(sink_abort_cleanup, snk);
}

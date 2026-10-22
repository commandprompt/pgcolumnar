/*-------------------------------------------------------------------------
 * columnar_sink.h
 *		The byte sink behind the export writers (#394 step 1).
 *
 * Mirrors the read side's PqSource seam: every export byte goes through ONE
 * checked write, so "no fwrite remains in the export files" becomes a suite
 * check instead of a hand-maintained census (the 13-vs-17 lesson, PR #604).
 * The invariant both implementations share: NOTHING is ever visible at the
 * final name before finish() returns. Locally that is write-to-temp plus
 * durable rename; the remote implementation (later, in the objstore module)
 * gets the same property from multipart completion.
 *-------------------------------------------------------------------------
 */
#ifndef COLUMNAR_SINK_H
#define COLUMNAR_SINK_H

#include "postgres.h"

typedef struct PqSink PqSink;

/* Open a sink for `path`: local (temp-and-rename) or remote (#394) by scheme. */
extern PqSink *PgColumnarSinkOpen(const char *path);

/* The local implementation; PgColumnarSinkOpen dispatches to it for a path. */
extern PqSink *PgColumnarSinkOpenLocal(const char *path);

/* Append exactly n bytes; a short write is an error AT THE CALL (#394). */
extern void PgColumnarSinkWrite(PqSink *snk, const void *buf, size_t n);

/* Commit: flush, fsync, durable rename to the final name. */
extern void PgColumnarSinkFinish(PqSink *snk);

/* Error-path cleanup: close and unlink the temp file. Never raises. */
extern void PgColumnarSinkAbort(PqSink *snk);

/*
 * From inside a PG_CATCH: run `cleanup`, then re-throw the error being handled
 * (#1741). Cleanup that can raise (a cancel during a remote DELETE) must not run
 * while that error is still on the error stack: the best-effort catch that
 * swallows the inner error empties the whole stack, and the re-throw then
 * reports "errstart was not called". Never returns.
 */
extern void PgColumnarCleanupAndRethrow(void (*cleanup) (void *arg), void *arg);

/* PgColumnarSinkAbort through PgColumnarCleanupAndRethrow. Never returns. */
extern void PgColumnarSinkAbortAndRethrow(PqSink *snk);

/*
 * Dev fault injection: the sink fails (as ENOSPC would) once total bytes
 * written would exceed this. -1 (default) disables. It drives the SAME error
 * path a real short write takes, so the suite's failure arms exercise the
 * code that a full disk reaches, chosen to land inside write_record_batch
 * (the helper the 13-site census missed).
 */
extern int	pgcolumnar_sink_fail_after;

#endif							/* COLUMNAR_SINK_H */

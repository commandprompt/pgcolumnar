/*-------------------------------------------------------------------------
 * columnar_objstore.h
 *		The ABI between pgColumnar and its object-store module (#393).
 *
 * Object-store support lives in a SEPARATE, non-preloaded shared library. The
 * reason is measured rather than stylistic: pgColumnar loads through
 * shared_preload_libraries, so anything it links is mapped into the postmaster
 * and inherited by every backend through fork, whether or not any query ever
 * reads a remote file. libcurl alone resolves to 30 shared objects against this
 * extension's 4, including two TLS implementations, and an OpenSSL-linked client
 * brings a TLS stack into a postmaster that may have none.
 *
 * PostgreSQL made the same call for its own libcurl dependency: configure
 * default off, a separate shared library, runtime dlopen behind a frozen ABI,
 * and a separate distribution package. This follows that shape.
 *
 * The module is loaded with load_external_function on the first read of a remote
 * path and never before. A build or an install without it is fully functional for
 * local files.
 *
 * A missing module reports the remote path as unsupported. That needs a PG_TRY in
 * the loader and not just signalNotFound = false, because signalNotFound
 * suppresses a missing SYMBOL while a missing LIBRARY is raised earlier, by
 * internal_load_library, before the symbol lookup happens.
 *
 * VERSIONING. Bump PGCOLUMNAR_OBJSTORE_ABI whenever the meaning or the order of
 * anything below changes. The loader refuses a mismatch, because the failure it
 * prevents is a wrong function called through a stale pointer.
 *-------------------------------------------------------------------------
 */
#ifndef COLUMNAR_OBJSTORE_H
#define COLUMNAR_OBJSTORE_H

#include "postgres.h"

#define PGCOLUMNAR_OBJSTORE_ABI 7

/* An open remote object (read) / upload (write). Module owns both. */
typedef struct PgColumnarObjHandle PgColumnarObjHandle;
typedef struct PgColumnarObjSink PgColumnarObjSink;

/*
 * The result of a one-shot HTTP request (#388 phase 7, ABI v5). `status` is the
 * HTTP status line code. `body` is the palloc'd response body in the caller's
 * memory context (NUL-terminated for convenience; `body_len` is the true byte
 * length, which may be 0 with body != NULL).
 */
typedef struct PgColumnarHttpResult
{
	int			status;
	char	   *body;
	int64		body_len;
} PgColumnarHttpResult;

/*
 * Connection configuration resolved from the FDW catalogs (#393 M4). NULL
 * means "no catalog config": endpoint/region/credentials come from the
 * ambient environment, allowed - the function-API paths, which are gated by
 * pg_read_server_files and have no server object to hang a mapping on. A
 * non-NULL config with allow_ambient=false and no credential triple makes the
 * module refuse (SQLSTATE 28000) rather than fall back to the postmaster's
 * identity: ambient is a privilege, not a default.
 */
typedef struct PgColumnarObjStoreConfig
{
	const char *endpoint;		/* NULL: AWS_ENDPOINT_URL */
	const char *region;			/* NULL: AWS_REGION / AWS_DEFAULT_REGION */
	const char *akid;			/* credential triple; akid+secret together */
	const char *secret;
	const char *token;			/* optional session token */
	bool		allow_ambient;	/* may fall back to the environment */
} PgColumnarObjStoreConfig;

typedef struct PgColumnarObjStoreApi
{
	int			abi_version;	/* must equal PGCOLUMNAR_OBJSTORE_ABI */

	/*
	 * Does this module handle `url`? Called before open so the reader can report
	 * an unsupported scheme without a connection attempt.
	 */
	bool		(*handles_url) (const char *url);

	/*
	 * Open `url` and report its size. Raises on failure, like every other read
	 * path in this extension. `len` is required: the Parquet footer is located
	 * from the end of the object, so a source that cannot report a length cannot
	 * be read. `cfg` may be NULL (see PgColumnarObjStoreConfig).
	 */
	PgColumnarObjHandle *(*open) (const char *url,
								  const PgColumnarObjStoreConfig *cfg,
								  int64 *len);

	/*
	 * Read exactly `n` bytes at `off`. The caller has already bounded the range
	 * against the length reported by open. A short read is an error, not a
	 * partial success, because the reader has no way to make progress from one.
	 */
	void		(*read) (PgColumnarObjHandle *h, int64 off, void *buf, size_t n);

	void		(*close) (PgColumnarObjHandle *h);

	/*
	 * Write side (#394). sink_create opens an upload for `url`; sink_write
	 * appends (the module buffers into >= part-size chunks and starts a
	 * multipart upload when the total crosses one part); sink_finish commits
	 * (a single PUT for a small object, CompleteMultipartUpload otherwise) and
	 * is the ONLY point the object becomes visible at its final name;
	 * sink_abort tears down without publishing and never raises, so it is safe
	 * from a PG_CATCH. delete_object removes a completed object by key (the
	 * parallel dispatcher's remote cleanup); it is best-effort and never raises,
	 * like a local unlink, so it too is safe from the dispatcher's error-cleanup
	 * path. sink_create/sink_write/sink_finish raise on failure; sink_abort and
	 * delete_object do not. cfg may be NULL (the export function paths).
	 */
	PgColumnarObjSink *(*sink_create) (const char *url,
									   const PgColumnarObjStoreConfig *cfg);
	void		(*sink_write) (PgColumnarObjSink *s, const void *buf, size_t n);
	void		(*sink_finish) (PgColumnarObjSink *s);
	void		(*sink_abort) (PgColumnarObjSink *s);
	void		(*delete_object) (const char *url,
								  const PgColumnarObjStoreConfig *cfg);

	/*
	 * List the objects under a prefix (#619). `url` is s3://bucket/prefix; the
	 * prefix may be empty (list the whole bucket) or name a folder. Returns a
	 * palloc'd array of `*nkeys` cstrings in the current memory context, each a
	 * full s3://bucket/key URL, sorted ascending, so the caller wraps them into
	 * the same file list a local directory walk produces. `*nkeys` may be 0.
	 * Raises on a transport or a listing-parse failure. The module owns the
	 * paging (ListObjectsV2 continuation tokens) and the hostile-input XML
	 * parse behind this ABI, the isolation the frozen boundary exists for. cfg
	 * may be NULL (the function-API paths).
	 */
	char	  **(*list_objects) (const char *url,
								 const PgColumnarObjStoreConfig *cfg,
								 int *nkeys);

	/*
	 * A one-shot HTTP(S) request (#388 phase 7). `url` must be http:// or
	 * https:// (an s3:// URL is refused: this is the general transport, not the
	 * object path). `method` is "GET"/"POST"/etc. `header_lines` are full
	 * "Name: value" lines (no trailing CRLF); the module adds Host, User-Agent,
	 * Connection, and Content-Length, and refuses any header line carrying a CR
	 * or LF (request-splitting guard). This entry does NOT sign: authentication
	 * is whatever the caller places in `header_lines` (e.g. an Authorization
	 * header). The response body is read up to `max_response` bytes; a larger
	 * advertised or streamed body raises (ERRCODE_PROGRAM_LIMIT_EXCEEDED). The
	 * request goes through the same connect path as every other entry, so the
	 * endpoint allow-list and the link-local/instance-metadata refusal apply
	 * unchanged. 4xx/5xx statuses are RETURNED (not raised) so the caller maps
	 * them; a transport, allow-list, or size failure raises. Runs through the
	 * same nonblocking wait loop, so it is cancellable.
	 */
	PgColumnarHttpResult (*http_request) (const char *url,
										  const char *method,
										  const char *const *header_lines,
										  int nheaders,
										  const char *body, int64 body_len,
										  int64 max_response);

	/*
	 * Multipart uploads in progress (#1743, ABI v6). A worker learns its
	 * upload id only from the answer to CreateMultipartUpload, so a worker that
	 * ends while waiting for that answer leaves an upload nobody holds the id
	 * of. The parallel export's dispatcher finds those by listing.
	 *
	 * list_uploads lists the uploads in progress under `url`, an
	 * s3://bucket/prefix or gs://bucket/prefix (ListMultipartUploads, paged by
	 * the module; gs:// since #1854). It returns a palloc'd array of
	 * `*nuploads` keys, each a full bucket/key URL in the lowercase scheme of
	 * `url`, and sets `*upload_ids` to a parallel array of upload ids, both in
	 * the current memory context. It raises on a transport or parse failure.
	 *
	 * abort_upload aborts one upload (AbortMultipartUpload, DELETE ?uploadId=)
	 * and returns the HTTP status; a status is not an error. It raises on a
	 * transport failure.
	 *
	 * Both take `timeout_ms`: when it is above 0, every wait in the call ends by
	 * then, and the call raises once that much time has passed, whether or not
	 * interrupts are held. 0 means the module's ordinary waits. cfg may be NULL.
	 */
	char	  **(*list_uploads) (const char *url,
								 const PgColumnarObjStoreConfig *cfg,
								 int timeout_ms,
								 char ***upload_ids,
								 int *nuploads);
	int			(*abort_upload) (const char *url,
								 const char *upload_id,
								 const PgColumnarObjStoreConfig *cfg,
								 int timeout_ms);

	/*
	 * The endpoint allow-list check alone (#1780, ABI v7). It resolves the
	 * endpoint `url` would connect to, exactly as a write does, and raises
	 * 42501 when pgcolumnar.objstore_allowed_endpoints, as this backend sees
	 * it, does not list that endpoint. It sends no request and opens no
	 * connection. `url` is s3://bucket/prefix, gs://bucket/prefix or an
	 * http(s) URL; the prefix may be empty. The parallel export's dispatcher
	 * calls it so a refused endpoint fails before any worker starts. cfg may
	 * be NULL.
	 */
	void		(*check_endpoint) (const char *url,
								   const PgColumnarObjStoreConfig *cfg);
} PgColumnarObjStoreApi;

/*
 * The module's single exported entry point. Returns a pointer to a static API
 * struct owned by the module.
 */
typedef const PgColumnarObjStoreApi *(*PgColumnarObjStoreInitFn) (void);

/*
 * Resolve the module, or return NULL when it is not installed. Cached after the
 * first call, including the negative result: a missing module is a property of
 * the installation and will not appear mid-session.
 */
extern const PgColumnarObjStoreApi *PgColumnarObjStoreGet(void);

/* Does `path` look like a remote URL at all? Cheap, no module load. */
extern bool PgColumnarPathIsRemote(const char *path);

/*
 * Open a local regular file for reading with no stat-before-open race: opens
 * O_NONBLOCK so a FIFO cannot block the open(2), fstats the held fd, refuses a
 * non-regular file (DATA_CORRUPTED), then clears O_NONBLOCK. Returns a transient
 * fd (close with CloseTransientFile). *size_out, if non-NULL, receives the size.
 * The caller must have already excluded remote paths (PgColumnarPathIsRemote).
 */
extern int PgColumnarOpenLocalRegularFile(const char *path, off_t *size_out);

/*
 * Read exactly n bytes from fd into buf (handling short reads and EINTR).
 * Returns true on success, false on EOF-before-n or error (errno set).
 */
extern bool PgColumnarReadExact(int fd, void *buf, size_t n);

/*
 * Slurp a whole local regular file into a palloc'd buffer via the race-free
 * opener above. Refuses a file larger than maxlen (PROGRAM_LIMIT_EXCEEDED, with
 * `what` naming the object, e.g. "Iceberg manifest"). *outlen receives the size.
 */
extern char *PgColumnarSlurpLocalRegularFile(const char *path, int64 maxlen,
											 const char *what, int64 *outlen);

#endif							/* COLUMNAR_OBJSTORE_H */

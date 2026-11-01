/*-------------------------------------------------------------------------
 * columnar_objstore.c
 *		Loader for the object-store module (#393).
 *
 * The module is a separate, non-preloaded shared library. See
 * columnar_objstore.h for why. This file is the only thing in the main library
 * that knows it exists, and it never loads it until a remote path is read.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "fmgr.h"
#include "utils/elog.h"
#include "miscadmin.h"
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "storage/fd.h"

#include "columnar.h"
#include "columnar_objstore.h"

/*
 * Cached across the session, INCLUDING the negative result. Whether the module is
 * installed is a property of the installation, so a miss will not become a hit
 * mid-session, and retrying a dlopen per read would be a per-read cost for
 * something that cannot change.
 */
static const PgColumnarObjStoreApi *objstore_api = NULL;
static bool objstore_tried = false;

/* The module's statement-end entry point, or NULL (see PgColumnarObjStoreStatementEnd). */
static void (*objstore_statement_end) (void) = NULL;

/*
 * Is the module file actually there? Resolves $libdir the way the server does,
 * because the SQLSTATE cannot distinguish a missing library from a broken one.
 */
static bool
objstore_module_present(void)
{
	char		libdir[MAXPGPATH];
	char		path[MAXPGPATH];
	struct stat st;

	get_pkglib_path(my_exec_path, libdir);
	snprintf(path, sizeof(path), "%s/pgcolumnar_objstore%s", libdir, DLSUFFIX);
	return stat(path, &st) == 0;
}

bool
PgColumnarPathIsRemote(const char *path)
{
	static const char *const schemes[] = {"s3://", "gs://", "az://", "https://",
	"http://", NULL};
	int			i;

	if (path == NULL)
		return false;
	for (i = 0; schemes[i] != NULL; i++)
		if (pg_strncasecmp(path, schemes[i], strlen(schemes[i])) == 0)
			return true;
	return false;
}

/*
 * s3a:// and s3n:// are the Hadoop names for an S3 store. Spark and Hive write
 * them into table metadata, and users copy them out of Hadoop configuration.
 * The scheme is matched in any case, and only at the start of the path: a
 * local path that holds "s3a://" further on is a local path.
 */
bool
PgColumnarPathIsS3Alias(const char *path)
{
	return path != NULL &&
		(pg_strncasecmp(path, "s3a://", 6) == 0 ||
		 pg_strncasecmp(path, "s3n://", 6) == 0);
}

/*
 * The s3:// form of an s3a:// or s3n:// path, palloc'd, and any other path
 * unchanged. The readers call this before anything else looks at the path:
 * the Iceberg reader for every path it reads (#1825), and the Parquet readers
 * (read_parquet, parquet_schema, import_parquet and the pgcolumnar_parquet
 * FDW) for the path the user gave (#1930). So routing, listing, globbing,
 * request signing and endpoint resolution all see s3://, and the object-store
 * module, which handles s3://, gs:// and http(s):// only, never sees the alias.
 *
 * PgColumnarPathIsRemote does not list the aliases, because the exports call it
 * too and write s3:// only: an export refuses an alias by name instead
 * (PgColumnarRefuseS3AliasWrite). An error raised after the rewrite names the
 * s3:// URL, which is the one that was signed and requested.
 */
const char *
PgColumnarS3Alias(const char *path)
{
	if (PgColumnarPathIsS3Alias(path))
		return psprintf("s3://%s", path + 6);
	return path;
}

/*
 * Refuse an s3a:// or s3n:// path in an export (#1930). The readers read an
 * alias as s3://, but the exports write only the schemes PgColumnarPathIsRemote
 * lists, as the Iceberg reader, which reads the aliases, writes nothing. Before
 * this an alias reached the local write path and failed with 58P01 "could not
 * open file "s3a://..." for writing". The hint names the s3:// URL to use.
 */
void
PgColumnarRefuseS3AliasWrite(const char *funcname, const char *path)
{
	if (PgColumnarPathIsS3Alias(path))
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: %s does not write \"%s\"", funcname, path),
				 errdetail("s3a:// and s3n:// URLs are read as s3://, but not written."),
				 errhint("Write to \"%s\" instead.", PgColumnarS3Alias(path))));
}

/*
 * Refuse a remote URL in a function that reads local files only (#1886).
 * import_arrow, file_split_offsets and parallel_copy open their path with
 * open(2), so an s3:// or http:// URL used to fail with ENOENT ("could not
 * open file "s3://...""), which says the object does not exist when the real
 * reason is that these functions never read object storage. Each calls this
 * before it opens the file, so the refusal is the same 0A000 the readers raise
 * for a scheme they cannot read, and it names the function. import_arrow and
 * parallel_copy call it after their INSERT and RLS checks on the target, so a
 * caller who may not write the target gets 42501 first, and before they check
 * the target's kind, so a heap target gets the refusal. read_avro_manifest and
 * read_manifest_list call it too (#1913), after their pg_read_server_files gate.
 *
 * It also refuses s3a:// and s3n://, in any case (#1918). Those are the Hadoop
 * names for S3, which the Iceberg and Parquet readers accept (#1825, #1930), so
 * a user may well pass one here. PgColumnarPathIsRemote does not list them (see
 * PgColumnarS3Alias), so this refusal names them itself.
 */
void
PgColumnarRefuseRemotePath(const char *funcname, const char *path)
{
	if (PgColumnarPathIsRemote(path) || PgColumnarPathIsS3Alias(path))
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: %s does not read \"%s\"", funcname, path),
				 errhint("%s reads local files only. Use a local filesystem path.",
						 funcname)));
}

/*
 * Choose the config for one object (#1820). The Iceberg REST spec scopes each
 * vended StorageCredential to "a storage location prefix where the credential
 * is relevant", so the choice is made per object URL, not once per table: a
 * catalog may vend one key for metadata/ and another for data/.
 *
 * The prefix is compared with the URL pgColumnar is about to read, byte for
 * byte, as the bytes the credential will sign. A URL no prefix covers gets the
 * fallback (the catalog's flat config). With no fallback the read is refused:
 * once a catalog vends credentials for a table, none of its files is read with
 * the server's ambient identity.
 */
const PgColumnarObjStoreConfig *
PgColumnarObjStoreConfigFor(const PgColumnarObjStoreConfig *cfg, const char *url)
{
	const PgColumnarObjStoreCredSet *cs;
	const PgColumnarObjStoreConfig *best = NULL;
	size_t		bestlen = 0;
	size_t		ulen;
	int			i;

	if (cfg == NULL || cfg->byprefix == NULL)
		return cfg;
	cs = cfg->byprefix;
	ulen = strlen(url);
	for (i = 0; i < cs->n; i++)
	{
		size_t		plen = strlen(cs->prefix[i]);

		if (plen <= ulen && strncmp(url, cs->prefix[i], plen) == 0 &&
			(best == NULL || plen > bestlen))
		{
			best = cs->cfg[i];
			bestlen = plen;
		}
	}
	if (best != NULL)
		return best;
	if (cs->fallback != NULL)
		return cs->fallback;
	ereport(ERROR,
			(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
			 errmsg("columnar: no vended storage credential covers \"%s\"", url),
			 errdetail("The REST catalog vended credentials for %d location prefix(es), and none is a prefix of this file. pgColumnar does not read the file with the server's own AWS_* credentials instead.",
					   cs->n),
			 errhint("Have the catalog vend a credential whose prefix covers the whole table location.")));
	return NULL;				/* keep the compiler quiet */
}

/*
 * Open a LOCAL regular file for reading with no stat-before-open race.
 *
 * AllocateFile()/fopen("rb") on a FIFO blocks in open(2) until a writer appears;
 * because our signal handlers use SA_RESTART the blocked open resumes across
 * SIGINT/SIGALRM, so neither a query cancel nor statement_timeout gets the
 * backend back (a cancel-resistant availability DoS, #644/#686). A plain stat()
 * before the open avoids the block but races: a local principal who can write the
 * directory can swap the checked regular file for a FIFO between the stat and the
 * open, re-introducing the block.
 *
 * So open once, O_NONBLOCK (a FIFO then returns immediately instead of blocking),
 * fstat the fd we hold -- the file we checked is the file we opened -- refuse a
 * non-regular file, then clear O_NONBLOCK for the read. This is the same pattern
 * as pcopy_open_regular_file, and it is the single opener every local Iceberg,
 * Avro, Parquet, and Arrow read reaches. The DATA_CORRUPTED code matches the
 * malformed-metadata refusals the read path already raises.
 *
 * A regular file (or a symlink to one; the open follows it) is allowed. A path
 * that does not exist is left as the open's errno-by-name error. Remote paths are
 * excluded by the caller (they are not filesystem objects).
 */
int
PgColumnarOpenLocalRegularFile(const char *path, off_t *size_out)
{
	int			fd;
	int			flags;
	struct stat st;

	fd = OpenTransientFile(path, O_RDONLY | O_NONBLOCK | PG_BINARY);
	if (fd < 0)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not open file \"%s\" for reading: %m", path)));
	if (fstat(fd, &st) != 0)
	{
		int			save_errno = errno;

		CloseTransientFile(fd);
		errno = save_errno;
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not stat file \"%s\": %m", path)));
	}
	if (!S_ISREG(st.st_mode))
	{
		CloseTransientFile(fd);
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("columnar: \"%s\" is not a regular file", path)));
	}
	flags = fcntl(fd, F_GETFL);
	if (flags == -1 || fcntl(fd, F_SETFL, flags & ~O_NONBLOCK) == -1)
	{
		int			save_errno = errno;

		CloseTransientFile(fd);
		errno = save_errno;
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not clear nonblocking mode on file \"%s\": %m", path)));
	}
	if (size_out != NULL)
		*size_out = st.st_size;
	return fd;
}

/* Read exactly n bytes; true on success, false on early EOF or error (errno set). */
bool
PgColumnarReadExact(int fd, void *buf, size_t n)
{
	char	   *p = (char *) buf;
	size_t		done = 0;

	while (done < n)
	{
		ssize_t		r;

		CHECK_FOR_INTERRUPTS();
		r = read(fd, p + done, n - done);
		if (r < 0)
		{
			if (errno == EINTR)
				continue;
			return false;
		}
		if (r == 0)
			return false;		/* EOF before n */
		done += (size_t) r;
	}
	return true;
}

/* Slurp a whole local regular file into a palloc'd buffer via the race-free opener. */
char *
PgColumnarSlurpLocalRegularFile(const char *path, int64 maxlen,
								const char *what, int64 *outlen)
{
	off_t		sz = 0;
	int			fd = PgColumnarOpenLocalRegularFile(path, &sz);
	int64		flen = (int64) sz;
	char	   *buf;

	if (flen < 0 || flen > maxlen)
	{
		CloseTransientFile(fd);
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("columnar: %s \"%s\" is too large", what, path)));
	}
	/* flen + 1 and a trailing NUL so text callers can treat it as a cstring;
	 * binary callers use *outlen and ignore the extra byte. */
	buf = (char *) palloc(flen + 1);
	if (flen > 0 && !PgColumnarReadExact(fd, buf, (size_t) flen))
	{
		int			save_errno = errno;

		CloseTransientFile(fd);
		errno = save_errno;
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not read file \"%s\": %m", path)));
	}
	CloseTransientFile(fd);
	buf[flen] = '\0';
	*outlen = flen;
	return buf;
}

const PgColumnarObjStoreApi *
PgColumnarObjStoreGet(void)
{
	PgColumnarObjStoreInitFn init;
	const PgColumnarObjStoreApi *api;
	MemoryContext loadcxt;

	if (objstore_tried)
		return objstore_api;

	/*
	 * signalNotFound = false suppresses a missing SYMBOL. It does NOT suppress a
	 * missing LIBRARY: internal_load_library raises before the symbol lookup
	 * happens, so that argument never gets a say and the caller sees
	 * 'could not access file "pgcolumnar_objstore"'. An installation without the
	 * module is a supported configuration, not an error, so catch it.
	 *
	 * Only the load is inside the PG_TRY, so nothing else is swallowed.
	 *
	 * objstore_tried is set AFTER the attempt, not before. Setting it first looks
	 * equivalent and is not: the ereport unwinds past the assignment while the
	 * static keeps its new value, so the FIRST remote read of a session reported
	 * the raw load failure and every later one reported the documented message.
	 * Two identical queries in one session gave two different errors, and the
	 * second was the plausible-looking one.
	 */
	loadcxt = CurrentMemoryContext;
	init = NULL;
	PG_TRY();
	{
		init = (PgColumnarObjStoreInitFn)
			load_external_function("$libdir/pgcolumnar_objstore",
								   "pgcolumnar_objstore_init", false, NULL);
	}
	PG_CATCH();
	{
		MemoryContext ecxt = MemoryContextSwitchTo(loadcxt);

		/*
		 * "Not installed" and "installed but broken" are different situations and
		 * only the first is supported. Swallowing both reports a truncated
		 * library or a permission problem as an unsupported scheme, which sends
		 * the operator looking in the wrong place for a fault that is theirs.
		 *
		 * The SQLSTATE cannot tell them apart, which is the trap here and the
		 * reason the first attempt at this was wrong. internal_load_library uses
		 * errcode_for_file_access() for BOTH the stat failure and the dlopen
		 * failure, so a missing file and "file too short" both arrive as
		 * ERRCODE_UNDEFINED_FILE (58P01). Measured, not assumed.
		 *
		 * So ask the filesystem instead. Absent means not installed; present
		 * means the load failed for a reason the operator needs to see, and the
		 * original error is re-raised with its own message intact.
		 */
		if (objstore_module_present())
		{
			MemoryContextSwitchTo(ecxt);
			PG_RE_THROW();
		}

		MemoryContextSwitchTo(ecxt);
		FlushErrorState();
		init = NULL;
	}
	PG_END_TRY();

	/*
	 * The flag is set on each return path below, never once up here, and nothing
	 * that can raise sits between an assignment and its return.
	 *
	 * This is the same trap as the one above, one step later. init() is a call
	 * into a separately built library: it can raise, and if the flag were already
	 * set the unwind would leave the verdict cached as "tried, nothing found", so
	 * the first remote read of a session would report init()'s error and every
	 * later one would report "requires the object-store module" — the same two
	 * identical queries, two different errors, the plausible one second. It is
	 * latent while init() only returns a pointer to a static struct. It stops
	 * being latent the moment the module has anything to set up, which is the
	 * commit after this one.
	 */
	if (init == NULL)
	{
		objstore_tried = true;
		return NULL;
	}

	api = init();

	/*
	 * Refuse a mismatch rather than calling through it. A stale module and a new
	 * main library agree on the symbol name and disagree on the struct, and the
	 * failure that produces is a wrong function through a valid-looking pointer.
	 */
	if (api == NULL || api->abi_version != PGCOLUMNAR_OBJSTORE_ABI)
	{
		ereport(WARNING,
				(errmsg("columnar: ignoring object-store module with ABI version %d",
						api ? api->abi_version : -1),
				 errdetail("This build expects ABI version %d.",
						   PGCOLUMNAR_OBJSTORE_ABI),
				 errhint("Reinstall pgcolumnar_objstore from the same build as "
						 "pgcolumnar.")));
		objstore_tried = true;
		return NULL;
	}

	/*
	 * The statement-end entry point (#1907 second review) is found by name,
	 * not through the API struct, so a module built without it still loads:
	 * signalNotFound = false leaves the pointer NULL. The library is already
	 * loaded, so this lookup cannot fail for a missing file.
	 */
	objstore_statement_end = (void (*) (void))
		load_external_function("$libdir/pgcolumnar_objstore",
							   "pgcolumnar_objstore_statement_end", false, NULL);

	objstore_api = api;
	objstore_tried = true;
	return objstore_api;
}

/*
 * A top-level statement ended: tell the module, if it is loaded, so the
 * object versions it pinned for the statement are dropped (#1907 second
 * review). Never loads the module and never raises.
 */
void
PgColumnarObjStoreStatementEnd(void)
{
	if (objstore_api != NULL && objstore_statement_end != NULL)
		objstore_statement_end();
}

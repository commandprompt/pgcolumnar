/*-------------------------------------------------------------------------
 *
 * columnar_iceberg.c
 *		A read-only, filesystem-backed Apache Iceberg catalog reader
 *		(#388 phase 3). Two entry points:
 *
 *		iceberg_current_snapshot	resolves the metadata pointer: given a
 *									table's metadata.json, reports the current
 *									snapshot the table declares (phase 3a).
 *
 *		iceberg_data_files			chains that snapshot through its manifest
 *									list and manifests to the live data files at
 *									the current snapshot, rebasing the recorded
 *									absolute paths onto the table's actual
 *									location and refusing loudly if the snapshot
 *									carries any delete files (phase 3b).
 *
 *		Metadata is pure JSON, parsed through the server's jsonb reader (the
 *		same way columnar_avro.c reads the schema embedded in a manifest); the
 *		manifest list and manifests are Avro, decoded by columnar_avro.c.
 *
 * Written fresh for pgColumnar from the public Apache Iceberg table spec.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "catalog/pg_authid_d.h"
#include "catalog/pg_collation_d.h"
#include "catalog/pg_type_d.h"
#include "fmgr.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "storage/fd.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "executor/tuptable.h"
#include "utils/datum.h"
#include "utils/jsonb.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/numeric.h"
#include "utils/tuplestore.h"
#include "utils/typcache.h"

#include "columnar_avro.h"
#include "columnar_puffin.h"
#include "columnar_iceberg.h"
#include "columnar_objstore.h"
#include "columnar_parquet_reader.h"

/*
 * A table metadata.json is small (schemas, snapshot log, partition specs). Cap
 * the read so a mis-pointed path cannot slurp an arbitrary large server file.
 */
#define ICE_MAX_METADATA ((int64) 64 * 1024 * 1024)

/* the current_snapshot result, kept in one place so the C and SQL agree */
#define ICE_SNAP_NCOLS 7
/* the data_files result columns */
#define ICE_FILE_NCOLS 4

/*
 * Slurp a whole object from object storage into a palloc'd buffer of *outlen
 * bytes (an extra NUL is always appended past the end, so a text caller can use
 * the buffer as a C string without copying). Used for the metadata.json,
 * manifest list, manifests, and Puffin files of a table in object storage. The
 * config is NULL: endpoint and credentials come from the ambient environment,
 * matching the read_parquet function API, which is gated by pg_read_server_files
 * (the endpoint itself is checked against objstore_allowed_endpoints inside the
 * module). The metadata cap bounds the object as for a local file.
 */
static uint8 *
ice_slurp_remote(const char *url, int64 *outlen,
				 const PgColumnarObjStoreConfig *cfg)
{
	const PgColumnarObjStoreApi *api = PgColumnarObjStoreGet();
	PgColumnarObjHandle *h;
	int64		len;
	uint8	   *volatile buf = NULL;

	if (api == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("iceberg: reading \"%s\" requires the object-store module",
						url),
				 errdetail("Object storage support is a separate library, "
						   "pgcolumnar_objstore, which is not installed."),
				 errhint("Install the pgcolumnar object-store package, or use a "
						 "local filesystem path.")));
	if (!api->handles_url(url))
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("iceberg: reading \"%s\" is not supported", url),
				 errdetail("The installed object-store module handles no such URL scheme.")));

	h = api->open(url, cfg, &len);	/* raises on failure; len is required */
	/* the handle is module-owned and not freed on transaction abort (the module
	 * uses explicit caller cleanup, like its sink API), so close it on any raise
	 * between open and the normal close -- a too-large check, palloc, or a
	 * read that raises on a short read / transport error -- or it leaks an fd */
	PG_TRY();
	{
		if (len < 0 || len > ICE_MAX_METADATA)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("columnar: Iceberg object \"%s\" is too large", url)));
		buf = (uint8 *) palloc(Max(len, 1) + 1);
		if (len > 0)
			api->read(h, 0, buf, (size_t) len);	/* short read raises */
	}
	PG_CATCH();
	{
		api->close(h);
		PG_RE_THROW();
	}
	PG_END_TRY();
	api->close(h);
	buf[len] = '\0';
	*outlen = len;
	return (uint8 *) buf;
}

/*
 * slurp a whole (small) file into a palloc'd, NUL-terminated string so jsonb_in
 * can parse it. A local path is read with AllocateFile; a remote URL (s3://,
 * http(s)://) through the object-store module.
 */
static char *
ice_slurp_text(const char *path, const PgColumnarObjStoreConfig *cfg)
{
	int64		flen;

	if (PgColumnarPathIsRemote(path))
		return (char *) ice_slurp_remote(path, &flen, cfg);

	/* Race-free local open (rejects a FIFO without a stat-before-open window),
	 * NUL-terminated so the JSON parser can treat it as a cstring. */
	return PgColumnarSlurpLocalRegularFile(path, ICE_MAX_METADATA,
										   "Iceberg metadata", &flen);
}

/* slurp a whole binary file (a manifest list, manifest, or Puffin file) into a
 * palloc'd buffer. Local via AllocateFile; remote via the object-store module. */
static uint8 *
ice_slurp_bin(const char *path, int64 *outlen,
			  const PgColumnarObjStoreConfig *cfg)
{
	if (PgColumnarPathIsRemote(path))
		return ice_slurp_remote(path, outlen, cfg);

	/* Race-free local open; see PgColumnarOpenLocalRegularFile. */
	return (uint8 *) PgColumnarSlurpLocalRegularFile(path, ICE_MAX_METADATA,
													 "Iceberg manifest", outlen);
}

/*
 * the SRF preamble: privilege gate, result-set checks, tupdesc, and the
 * per-query-context tuplestore (created in the per-query, not per-call,
 * context; the per-call context frees before the executor drains the store).
 */
static Tuplestorestate *
ice_srf_begin(FunctionCallInfo fcinfo, TupleDesc *tupdesc)
{
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Tuplestorestate *tupstore;
	MemoryContext oldcxt;

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a server file")));
	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo) ||
		(rsinfo->allowedModes & SFRM_Materialize) == 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in a context that cannot accept a set")));
	if (get_call_result_type(fcinfo, NULL, tupdesc) != TYPEFUNC_COMPOSITE)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("function returning record called in a context that cannot accept it")));

	oldcxt = MemoryContextSwitchTo(rsinfo->econtext->ecxt_per_query_memory);
	tupstore = tuplestore_begin_heap(false, false, work_mem);
	rsinfo->returnMode = SFRM_Materialize;
	rsinfo->setResult = tupstore;
	rsinfo->setDesc = CreateTupleDescCopy(*tupdesc);
	MemoryContextSwitchTo(oldcxt);
	return tupstore;
}

/* a JSON object field by key, or NULL when the container is not an object or
 * has no such key */
static JsonbValue *
ice_field(JsonbContainer *c, const char *key)
{
	if (c == NULL || !JsonContainerIsObject(c))
		return NULL;
	return getKeyJsonValueFromContainer(c, key, (int) strlen(key),
										palloc(sizeof(JsonbValue)));
}

/*
 * Interpret a JSON number as an int64; false if v is not a (non-null) number.
 * Every number this reader takes from metadata.json is an integer in the
 * Iceberg spec. numeric_int8 rounds, so a fraction such as 1.4 used to read as
 * 1 and could name another field (#1824). Refuse a fraction instead. A number
 * outside int64 is refused by numeric_int8 itself.
 */
static bool
ice_num_int64(JsonbValue *v, int64 *out)
{
	Datum		num;

	if (v == NULL || v->type != jbvNumeric)
		return false;
	num = NumericGetDatum(v->val.numeric);
	*out = DatumGetInt64(DirectFunctionCall1(numeric_int8, num));
	if (DatumGetInt32(DirectFunctionCall2(numeric_cmp, num,
										  NumericGetDatum(int64_to_numeric(*out)))) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: metadata holds the non-integer number %s where an integer is required",
						DatumGetCString(DirectFunctionCall1(numeric_out, num)))));
	return true;
}

/*
 * A schema field's "id" as an int, or -1 when it has none. A field id is an
 * int in the Iceberg spec. A larger one used to truncate, so 2^32 + 1 read as
 * field 1 and the column returned another field's values (#1824).
 */
static int64
ice_schema_field_id(JsonbContainer *f, const char *name, int namelen)
{
	int64		fid;

	if (!ice_num_int64(ice_field(f, "id"), &fid))
		return -1;
	if (fid < 0 || fid > PG_INT32_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: schema field \"%.*s\" has a field id out of range: " INT64_FORMAT,
						namelen, name, fid)));
	return fid;
}

/* a JSON string field as a palloc'd text Datum, or set the null flag */
static Datum
ice_str_field(JsonbContainer *c, const char *key, bool *isnull)
{
	JsonbValue *v = ice_field(c, key);

	if (v == NULL || v->type != jbvString)
	{
		*isnull = true;
		return (Datum) 0;
	}
	*isnull = false;
	return PointerGetDatum(cstring_to_text_with_len(v->val.string.val,
													v->val.string.len));
}

/* a required JSON string field as a palloc'd cstring; raises if absent */
static char *
ice_str_required(JsonbContainer *c, const char *key, const char *path)
{
	JsonbValue *v = ice_field(c, key);

	if (v == NULL || v->type != jbvString)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: \"%s\" is missing the required \"%s\" field",
						path, key)));
	return pnstrdup(v->val.string.val, v->val.string.len);
}

/*
 * Resolve the current snapshot object from a parsed metadata.json root. Returns
 * the snapshot's container, or NULL when the table declares no current snapshot
 * (a legal, empty table). Raises when the file is not Iceberg table metadata, or
 * when the declared current snapshot is absent from the snapshots array. On a
 * non-NULL return *cur holds the current-snapshot-id. Shared by both entry
 * points so they resolve identically.
 */
static JsonbContainer *
ice_current_snapshot(JsonbContainer *root, const char *path, int64 *cur)
{
	JsonbValue *fv;
	JsonbValue *csid;
	JsonbValue *snaps;
	JsonbContainer *arr;
	int64		fmtver;
	uint32		i,
				n;

	if (!JsonContainerIsObject(root))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: metadata \"%s\" is not a JSON object", path)));
	/* a minimal is-this-Iceberg gate so a random JSON file is rejected, not
	 * silently read as an empty table */
	fv = ice_field(root, "format-version");
	if (fv == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: \"%s\" has no format-version; not a table metadata file",
						path)));
	/* the spec says a reader must refuse a format-version it does not know */
	if (!ice_num_int64(fv, &fmtver))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: \"%s\" has a non-numeric format-version",
						path)));
	if (fmtver < 1 || fmtver > 3)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("iceberg: \"%s\" has an unsupported format-version " INT64_FORMAT,
						path, fmtver),
				 errdetail("This reader supports format-version 1, 2 and 3.")));

	csid = ice_field(root, "current-snapshot-id");
	/* absent, JSON null, or -1: a table with no current snapshot */
	if (csid == NULL || csid->type == jbvNull)
		return NULL;
	/* a string here used to read as "no snapshot": an empty table (#1824) */
	if (!ice_num_int64(csid, cur))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: \"%s\" has a current-snapshot-id that is not an integer",
						path)));
	if (*cur < 0)
		return NULL;

	snaps = ice_field(root, "snapshots");
	if (snaps == NULL || snaps->type != jbvBinary ||
		!JsonContainerIsArray(snaps->val.binary.data))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: \"%s\" declares a current snapshot but has no snapshots array",
						path)));

	arr = snaps->val.binary.data;
	n = JsonContainerSize(arr);
	for (i = 0; i < n; i++)
	{
		JsonbValue *s = getIthJsonbValueFromContainer(arr, i);
		JsonbContainer *sc;
		JsonbValue *v;
		int64		sid;

		/* cheap hygiene for a scan over an untrusted (if capped) snapshot list */
		CHECK_FOR_INTERRUPTS();

		if (s == NULL || s->type != jbvBinary)
			continue;
		sc = s->val.binary.data;
		v = ice_field(sc, "snapshot-id");
		/* the resolution: only the snapshot the table names as current */
		if (ice_num_int64(v, &sid) && sid == *cur)
			return sc;
	}

	/* the table named a current snapshot that its own snapshots list omits */
	ereport(ERROR,
			(errcode(ERRCODE_DATA_CORRUPTED),
			 errmsg("iceberg: current-snapshot-id " INT64_FORMAT " in \"%s\" is not present in the snapshots array",
					*cur, path)));
	return NULL;				/* unreachable */
}

/* the directory portion of a path (everything before the last '/'), palloc'd */
static char *
ice_dirname(const char *path)
{
	const char *slash = strrchr(path, '/');

	if (slash == NULL)
		return pstrdup(".");
	if (slash == path)			/* the root "/" */
		return pstrdup("/");
	return pnstrdup(path, slash - path);
}

/*
 * The table's actual location on disk, derived from where its metadata.json
 * sits: Iceberg's filesystem layout puts metadata at <location>/metadata/<file>,
 * so the location is the parent of the directory holding metadata_path.
 */
static char *
ice_actual_location(const char *metadata_path)
{
	char	   *metadir = ice_dirname(metadata_path);

	return ice_dirname(metadir);
}

/* strip a leading "file://" scheme from a recorded root, in place-ish. A remote
 * scheme (s3://, http(s)://) is left intact, so a recorded remote key keeps its
 * scheme and the same containment logic applies to its key portion. */
static const char *
ice_strip_scheme(const char *path)
{
	if (strncmp(path, "file://", 7) == 0)
		return path + 7;
	return path;
}

/* does a path segment sequence contain a ".." component? For object storage a
 * key is literal (no symlinks, no realpath), so a recorded ".." cannot be
 * collapsed the way canonicalize_path does for a local path; reject it, so a
 * recorded key cannot reference outside the table prefix on a server that
 * normalizes "..". */
static bool
ice_has_dotdot(const char *s)
{
	const char *p = s;

	while (p != NULL && *p != '\0')
	{
		if (p[0] == '.' && p[1] == '.' &&
			(p[2] == '\0' || p[2] == '/') &&
			(p == s || p[-1] == '/'))
			return true;
		p = strchr(p, '/');
		if (p != NULL)
			p++;
	}
	return false;
}

/* one hex digit -> value, or false if not a hex digit */
static bool
ice_hexval(char c, int *out)
{
	if (c >= '0' && c <= '9')
	{
		*out = c - '0';
		return true;
	}
	if (c >= 'a' && c <= 'f')
	{
		*out = c - 'a' + 10;
		return true;
	}
	if (c >= 'A' && c <= 'F')
	{
		*out = c - 'A' + 10;
		return true;
	}
	return false;
}

/* Percent-decode one pass of `s` into a fresh buffer. A "%HH" with two hex
 * digits becomes the decoded byte; anything else is copied verbatim. Sets
 * *changed if any escape was decoded. */
static char *
ice_percent_decode_once(const char *s, bool *changed, bool *sawnul)
{
	size_t		len = strlen(s);
	char	   *out = palloc(len + 1);
	char	   *w = out;
	const char *p = s;
	int			hi,
				lo;

	*changed = false;
	while (*p != '\0')
	{
		if (p[0] == '%' && p[1] != '\0' && p[2] != '\0' &&
			ice_hexval(p[1], &hi) && ice_hexval(p[2], &lo))
		{
			if (((hi << 4) | lo) == 0)
				*sawnul = true;
			*w++ = (char) ((hi << 4) | lo);
			p += 3;
			*changed = true;
		}
		else
			*w++ = *p++;
	}
	*w = '\0';
	return out;
}

/* Would percent-decoding reveal a ".." segment that ice_has_dotdot cannot see
 * in the literal bytes? A remote key is sent to the object store verbatim, and
 * an origin or reverse proxy on the http(s):// transport may percent-decode it
 * before serving a file -- so ".." can be smuggled past ice_has_dotdot as
 * "%2e%2e", and a separator as "%2f", reconstituting a traversal only after the
 * bytes leave us. Model what such a server would see: decode to a fixed point (a
 * decoder may run more than once, so "%252e" -> "%2e" -> ".") and check each
 * decoded form for a ".." segment. We still send the original bytes; this only
 * decides whether to refuse. A literal '%' that does not decode toward a dot
 * segment (a legitimate, if rare, key) is left alone. Each decoding pass that
 * changes anything strictly shortens the string, so this terminates. */
static bool
ice_has_encoded_dotdot(const char *s)
{
	char	   *cur = pstrdup(s);
	bool		changed;
	bool		sawnul = false;

	for (;;)
	{
		cur = ice_percent_decode_once(cur, &changed, &sawnul);

		/*
		 * An encoded NUL truncates every check below this point. The next pass
		 * measures with strlen and ice_has_dotdot walks with strchr, so both
		 * stop at that byte and never reach the "../" behind it: a key of
		 * "%00%2e%2e/%2e%2e/etc/hostname" decoded to a NUL followed by the
		 * traversal, and this function returned false. The literal-".." guard
		 * beside it sees no ".." either, because the segments are encoded, so
		 * the path passed BOTH containment checks and was fetched.
		 *
		 * A NUL has no place in an object key or a path, and nothing downstream
		 * can carry one safely, so refuse on the encoded NUL itself rather than
		 * try to scan past it. Refusing is the same answer this function gives
		 * any other smuggled traversal.
		 */
		if (sawnul)
			return true;
		if (!changed)
			return false;		/* nothing left to decode; no hidden ".." */
		if (ice_has_dotdot(cur))
			return true;
	}
}

/*
 * Rebase a path recorded in the table onto the table's actual location, and
 * refuse anything that would escape that location. This is the arbitrary-file
 * -read boundary: `recorded_path` comes from an untrusted metadata.json, so a
 * byte-prefix match is not enough -- `<location>/../../etc/passwd` shares the
 * prefix, and `<location>EVIL/x` shares the bytes but is a sibling. So:
 *
 *   1. require the recorded path to sit under `recorded_root` on a PATH
 *      boundary (the next byte is '/' or end), which rejects the sibling;
 *   2. re-root onto `actual_root`, then canonicalize the result and re-check
 *      containment, which collapses any ".." and rejects a traversal escape.
 *
 * `actual_root` is passed already canonicalized so the containment test compares
 * like with like.
 */
static char *
ice_rebase(const char *recorded_root, const char *actual_root,
		   const char *recorded_path, const char *what, const char *mdpath)
{
	const char *p;
	size_t		rlen = strlen(recorded_root);
	size_t		alen;
	char	   *cand;

	/*
	 * A recorded path (a manifest, data, or delete path taken from the untrusted
	 * manifest/manifest-list) must not be null. The manifest schema is embedded
	 * and author-controlled, so a hostile writer can declare a path field as
	 * union[null,string] and encode null; without this guard ice_strip_scheme
	 * would strncmp(NULL) and crash the backend (#644 "refuse malformed, do not
	 * crash"; #691). This also covers a null path exposed by any other means.
	 */
	if (recorded_path == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: a %s path in \"%s\" is null", what, mdpath)));
	p = ice_strip_scheme(recorded_path);

	/* tolerate a trailing slash on the recorded location */
	while (rlen > 1 && recorded_root[rlen - 1] == '/')
		rlen--;

	if (strncmp(p, recorded_root, rlen) != 0 ||
		(p[rlen] != '\0' && p[rlen] != '/'))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: %s path \"%s\" from \"%s\" is not under the table location \"%s\"",
						what, recorded_path, mdpath, recorded_root)));

	cand = psprintf("%s%s", actual_root, p + rlen);

	/* Object storage: the actual root is a URL (s3://...). canonicalize_path
	 * would collapse the "://" double slash and corrupt the scheme, and there
	 * is no realpath/symlink notion, so the containment is purely lexical: the
	 * key stays under the table prefix (enforced by the recorded-root boundary
	 * above) and carries no ".." segment that could walk out of it -- neither a
	 * literal ".." nor one a downstream http(s) origin/proxy would reconstitute
	 * by percent-decoding the key ("%2e%2e", "..%2f"). */
	if (PgColumnarPathIsRemote(actual_root))
	{
		if (ice_has_dotdot(p + rlen) || ice_has_encoded_dotdot(p + rlen))
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("iceberg: %s path \"%s\" from \"%s\" escapes the table location \"%s\"",
							what, recorded_path, mdpath, actual_root)));
		return cand;
	}

	canonicalize_path(cand);	/* collapse any ".." / "." / "//" segments */

	alen = strlen(actual_root);
	if (strncmp(cand, actual_root, alen) != 0 ||
		(cand[alen] != '\0' && cand[alen] != '/'))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: %s path \"%s\" from \"%s\" escapes the table location \"%s\"",
						what, recorded_path, mdpath, actual_root)));
	return cand;
}

/*
 * The rebase to use for a path we are about to OPEN. ice_rebase's lexical
 * canonicalization collapses ".." but is symlink-blind: a symlink placed under
 * the location by whoever wrote the (untrusted) table would pass the lexical
 * containment check and then be followed out of the location on open. So resolve
 * the rebased path with realpath() -- which collapses symlinks as well as ".."
 * -- and re-check containment against the (also realpath'd) actual root before
 * handing it to the file reader. A legitimately relocated table never symlinks
 * out of its own directory, so honest tables resolve under the actual root and
 * are unaffected. `actual_root_real` must already be realpath'd by the caller.
 */
static char *
ice_open_path(const char *recorded_root, const char *actual_root_real,
			  const char *recorded_path, const char *what, const char *mdpath)
{
	char	   *cand = ice_rebase(recorded_root, actual_root_real,
								  recorded_path, what, mdpath);
	char	   *real;
	size_t		alen;
	char	   *out;

	/* object storage has no symlinks/realpath; ice_rebase already did the full
	 * (lexical) containment for a remote path, so the rebased URL is safe to
	 * open as is */
	if (PgColumnarPathIsRemote(actual_root_real))
		return cand;

	real = realpath(cand, NULL);
	if (real == NULL)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("iceberg: could not resolve %s \"%s\" from \"%s\": %m",
						what, recorded_path, mdpath)));

	alen = strlen(actual_root_real);
	if (strncmp(real, actual_root_real, alen) != 0 ||
		(real[alen] != '\0' && real[alen] != '/'))
	{
		char	   *escaped = pstrdup(real);

		free(real);
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: %s path \"%s\" from \"%s\" resolves outside the table location \"%s\"",
						what, recorded_path, mdpath, actual_root_real),
				 errdetail("Resolved to \"%s\".", escaped)));
	}
	out = pstrdup(real);
	free(real);
	return out;
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_current_snapshot);

/*
 * pgcolumnar.iceberg_current_snapshot(metadata_path text)
 *
 * Parse an Iceberg table metadata.json and return the current snapshot it
 * declares: the snapshot whose "snapshot-id" equals "current-snapshot-id".
 * Zero rows when the table has no current snapshot (the field is absent, JSON
 * null, or -1). Raises when the file is not Iceberg table metadata, or when
 * the declared current snapshot is not present in the snapshots array.
 */
Datum
pgcolumnar_iceberg_current_snapshot(PG_FUNCTION_ARGS)
{
	char	   *path = text_to_cstring(PG_GETARG_TEXT_PP(0));
	TupleDesc	tupdesc;
	Tuplestorestate *tupstore;
	char	   *json;
	Jsonb	   *jb;
	JsonbContainer *sc;
	JsonbValue *v;
	int64		cur;
	int64		num;
	Datum		values[ICE_SNAP_NCOLS];
	bool		nulls[ICE_SNAP_NCOLS];

	tupstore = ice_srf_begin(fcinfo, &tupdesc);

	json = ice_slurp_text(path, NULL);	/* introspection: ambient creds */
	/* jsonb_in raises a clean 22P02 on malformed JSON */
	jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in, CStringGetDatum(json)));

	sc = ice_current_snapshot(&jb->root, path, &cur);
	if (sc == NULL)
		return (Datum) 0;		/* no current snapshot -> zero rows */

	memset(nulls, false, sizeof(nulls));

	values[0] = Int64GetDatum(cur);							/* snapshot_id */

	v = ice_field(sc, "parent-snapshot-id");				/* optional */
	if (v != NULL && v->type != jbvNull && ice_num_int64(v, &num))
		values[1] = Int64GetDatum(num);
	else
		nulls[1] = true;

	v = ice_field(sc, "sequence-number");					/* 0 for v1 */
	values[2] = Int64GetDatum(ice_num_int64(v, &num) ? num : 0);

	v = ice_field(sc, "timestamp-ms");
	if (ice_num_int64(v, &num))
		values[3] = Int64GetDatum(num);
	else
		nulls[3] = true;

	/* operation lives under the summary object */
	v = ice_field(sc, "summary");
	if (v != NULL && v->type == jbvBinary)
		values[4] = ice_str_field(v->val.binary.data, "operation", &nulls[4]);
	else
		nulls[4] = true;

	values[5] = ice_str_field(sc, "manifest-list", &nulls[5]);

	v = ice_field(sc, "schema-id");							/* optional */
	if (ice_num_int64(v, &num))
		values[6] = Int32GetDatum((int32) num);
	else
		nulls[6] = true;

	tuplestore_putvalues(tupstore, tupdesc, values, nulls);
	return (Datum) 0;
}

/*
 * Callback invoked once per live entry of the current snapshot, with the entry
 * and the roots needed to rebase its recorded path. The entry's `content`
 * distinguishes data (0) from position-delete (1) files, and its
 * `sequence_number` has been resolved (inheriting the manifest's when the entry
 * left it null). Shared by iceberg_data_files (lists data files) and iceberg_scan
 * (reads them and applies deletes).
 */
typedef void (*IceDataFileCb) (void *ctx, PgColumnarAvroManifestEntry *e,
							   const PgColumnarAvroManifestFile *mf,
							   const char *recorded_root, const char *actual_root,
							   const char *mdpath, int64 snapshot_id);

/*
 * Walk the current snapshot's live entries and hand each to `cb`. Resolves the
 * snapshot from an already-parsed metadata.json `root` (so the file is read and
 * parsed once per call), reads its manifest list and each manifest (opening both
 * through ice_open_path's path boundary).
 *
 * When `collect_deletes` is false (the lister), any delete file or removed entry
 * is refused loudly (0A000). When true (the scan), position-delete (content 1)
 * and equality-delete (content 2) entries are passed to `cb` alongside data
 * entries (content 0), and removed entries (status 2) are skipped. Does nothing
 * when there is no current snapshot.
 */
static void
ice_walk_data_files(const char *path, JsonbContainer *root,
					bool collect_deletes, IceDataFileCb cb, void *ctx,
					const PgColumnarObjStoreConfig *cfg)
{
	JsonbContainer *sc;
	int64		cur;
	const char *recorded_root;
	char	   *actual_root;
	char	   *ml_path;
	uint8	   *mlbuf;
	int64		mllen;
	PgColumnarAvroManifestFile *mfs;
	int			nmf;
	int			mi;

	sc = ice_current_snapshot(root, path, &cur);
	if (sc == NULL)
		return;					/* no current snapshot -> no data files */

	/* the recorded table root, and where the table actually sits now. For a
	 * local table the actual root is resolved with realpath once here so the
	 * files we open can be re-checked for containment against a symlink-resolved
	 * form. For a table in object storage there is no realpath/symlink notion:
	 * the actual root is the location derived lexically from the metadata URL,
	 * and containment is the lexical key-prefix check in ice_rebase. */
	recorded_root = ice_strip_scheme(ice_str_required(root, "location", path));
	if (PgColumnarPathIsRemote(path))
		actual_root = ice_actual_location(path);
	else
	{
		char	   *raw = ice_actual_location(path);
		char	   *real = realpath(raw, NULL);

		if (real == NULL)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("iceberg: could not resolve the table location of \"%s\": %m",
							path)));
		actual_root = pstrdup(real);
		free(real);
	}

	ml_path = ice_open_path(recorded_root, actual_root,
							ice_str_required(sc, "manifest-list", path),
							"manifest-list", path);
	mlbuf = ice_slurp_bin(ml_path, &mllen, cfg);
	mfs = PgColumnarAvroReadManifestList(mlbuf, mllen, &nmf);

	for (mi = 0; mi < nmf; mi++)
	{
		char	   *m_path;
		uint8	   *mbuf;
		int64		mlen;
		PgColumnarAvroManifestEntry *entries;
		int			ne;
		int			ei;

		CHECK_FOR_INTERRUPTS();

		/* the lister refuses a delete manifest without even opening it */
		if (mfs[mi].content != 0 && !collect_deletes)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("iceberg: snapshot " INT64_FORMAT " has delete files; reading tables with deletes is not supported",
							cur),
					 errdetail("Manifest \"%s\" has content %d (a delete manifest).",
							   mfs[mi].manifest_path, mfs[mi].content)));

		m_path = ice_open_path(recorded_root, actual_root, mfs[mi].manifest_path,
							   "manifest", path);
		mbuf = ice_slurp_bin(m_path, &mlen, cfg);
		entries = PgColumnarAvroReadManifest(mbuf, mlen, &ne);

		for (ei = 0; ei < ne; ei++)
		{
			PgColumnarAvroManifestEntry *e = &entries[ei];

			CHECK_FOR_INTERRUPTS();

			if (!collect_deletes)
			{
				/* the lister refuses any delete file or removed entry */
				if (e->content != 0 || e->status == 2)
					ereport(ERROR,
							(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
							 errmsg("iceberg: snapshot " INT64_FORMAT " has delete files; reading tables with deletes is not supported",
									cur),
							 errdetail("Entry \"%s\" has content %d, status %d.",
									   e->file_path, e->content, e->status)));
			}
			else
			{
				/* the scan skips removed entries; both delete kinds are
				 * collected and applied by the caller */
				if (e->status == 2)
					continue;
			}

			/* a manifest entry with no data-file path is corrupt metadata; refuse
			 * it here so neither consumer dereferences a NULL file_path (the scan
			 * pstrdup's it, the lister rebases it, the error path below prints it) */
			if (e->file_path == NULL)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: a manifest entry in snapshot " INT64_FORMAT " has no data file path",
								cur)));

			/* resolve the data sequence number. A manifest whose entry schema
			 * has no sequence-number column at all is v1-shaped: the spec
			 * defaults every file's sequence number to 0, for every status.
			 * In a v2 manifest (the column exists) a null is inherited from
			 * the manifest ONLY for ADDED (status 1) entries; an EXISTING (0)
			 * or DELETED (2) entry must carry it explicitly, so a null there
			 * is corrupt metadata, not an inheritance -- inheriting the
			 * current (too-new) manifest number could wrongly keep a row a
			 * delete should have removed. */
			if (!e->has_sequence_number)
			{
				if (!e->has_sequence_field)
				{
					e->sequence_number = 0;
					e->has_sequence_number = true;
				}
				else if (e->status != 1)
					ereport(ERROR,
							(errcode(ERRCODE_DATA_CORRUPTED),
							 errmsg("iceberg: a status-%d manifest entry for \"%s\" has no sequence number",
									e->status, e->file_path)));
				else if (!mfs[mi].has_sequence_number)
					ereport(ERROR,
							(errcode(ERRCODE_DATA_CORRUPTED),
							 errmsg("iceberg: manifest \"%s\" has a null sequence number that an added entry for \"%s\" must inherit",
									mfs[mi].manifest_path, e->file_path)));
				else
				{
					e->sequence_number = mfs[mi].sequence_number;
					e->has_sequence_number = true;
				}
			}

			cb(ctx, e, &mfs[mi], recorded_root, actual_root, path, cur);
		}

		pfree(mbuf);
	}

	pfree(mlbuf);
}

/* iceberg_data_files callback: emit one row (path, format, record count,
 * partition) per data-file entry. The path is lexically rebased (the row is
 * reported, not opened here). */
typedef struct IceListCtx
{
	Tuplestorestate *tupstore;
	TupleDesc	tupdesc;
}			IceListCtx;

static void
ice_list_cb(void *ctx, PgColumnarAvroManifestEntry *e,
			const PgColumnarAvroManifestFile *mf,
			const char *recorded_root, const char *actual_root,
			const char *mdpath, int64 snapshot_id)
{
	IceListCtx *c = (IceListCtx *) ctx;
	Datum		values[ICE_FILE_NCOLS];
	bool		nulls[ICE_FILE_NCOLS];

	(void) mf;
	(void) snapshot_id;
	memset(nulls, false, sizeof(nulls));
	values[0] = PointerGetDatum(cstring_to_text(
								ice_rebase(recorded_root, actual_root,
										   e->file_path, "data-file", mdpath)));
	if (e->file_format != NULL)
		values[1] = PointerGetDatum(cstring_to_text(e->file_format));
	else
		nulls[1] = true;
	values[2] = Int64GetDatum(e->record_count);
	if (e->partition != NULL)
		values[3] = PointerGetDatum(cstring_to_text(e->partition));
	else
		nulls[3] = true;

	tuplestore_putvalues(c->tupstore, c->tupdesc, values, nulls);
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_data_files);

/*
 * pgcolumnar.iceberg_data_files(metadata_path text)
 *
 * List the live data files of the current snapshot: resolve the snapshot, read
 * its manifest list, then each manifest, and emit one row per data-file entry
 * (path, format, record count, partition). The recorded absolute paths are
 * rebased onto the table's actual location, so a relocated table reads. Zero
 * rows for a table with no current snapshot.
 *
 * Deletes are refused, not ignored: a snapshot that carries any delete manifest
 * (manifest_file.content != 0) or any delete/removed entry (data_file.content
 * != 0, or a DELETED status) raises feature_not_supported (0A000). A reader that
 * silently dropped deletes would return rows the table says are gone.
 */
Datum
pgcolumnar_iceberg_data_files(PG_FUNCTION_ARGS)
{
	char	   *path = text_to_cstring(PG_GETARG_TEXT_PP(0));
	IceListCtx	ctx;
	Jsonb	   *jb;

	ctx.tupstore = ice_srf_begin(fcinfo, &ctx.tupdesc);
	jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
											CStringGetDatum(ice_slurp_text(path, NULL))));
	ice_walk_data_files(path, &jb->root, false, ice_list_cb, &ctx, NULL);
	return (Datum) 0;
}

/* Sum the live data files' record counts for a row estimate. Delete files are
 * ignored, so the count is a slight overestimate on a table with deletes, which
 * is the safe direction for a planner cardinality. collect_deletes is true so a
 * table that carries deletes is estimated rather than refused. */
static void
ice_estimate_cb(void *ctx, PgColumnarAvroManifestEntry *e,
				const PgColumnarAvroManifestFile *mf,
				const char *recorded_root, const char *actual_root,
				const char *mdpath, int64 snapshot_id)
{
	(void) mf;
	(void) recorded_root;
	(void) actual_root;
	(void) mdpath;
	(void) snapshot_id;
	if (e->content == 0)		/* a data file; deletes do not add rows */
		*(int64 *) ctx += e->record_count;
}

/*
 * A planner row estimate for the current snapshot: the sum of the live data
 * files' record counts, read from the manifests. Zero for a table with no
 * current snapshot. Used by the foreign-data wrapper's GetForeignRelSize in
 * place of a constant guess.
 */
int64
PgColumnarIcebergEstimateRows(const char *path)
{
	Jsonb	   *jb;
	int64		total = 0;

	jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
											CStringGetDatum(ice_slurp_text(path, NULL))));
	ice_walk_data_files(path, &jb->root, true, ice_estimate_cb, &total, NULL);
	return total;
}

/*
 * Locate the "fields" array of the table's current schema in a parsed
 * metadata.json: the entry of "schemas" whose "schema-id" equals
 * "current-schema-id", or the v1 single top-level "schema" object.
 */
static JsonbContainer *
ice_current_schema_fields(JsonbContainer *root, const char *path)
{
	JsonbValue *schemas = ice_field(root, "schemas");
	JsonbValue *curid = ice_field(root, "current-schema-id");
	JsonbValue *fields = NULL;
	int64		want;

	if (schemas != NULL && schemas->type == jbvBinary &&
		JsonContainerIsArray(schemas->val.binary.data) &&
		ice_num_int64(curid, &want))
	{
		JsonbContainer *arr = schemas->val.binary.data;
		uint32		ns = JsonContainerSize(arr);
		uint32		k;
		bool		matched = false;

		for (k = 0; k < ns; k++)
		{
			JsonbValue *s = getIthJsonbValueFromContainer(arr, k);
			int64		sid;

			if (s == NULL || s->type != jbvBinary)
				continue;
			if (ice_num_int64(ice_field(s->val.binary.data, "schema-id"), &sid) &&
				sid == want)
			{
				matched = true;
				fields = ice_field(s->val.binary.data, "fields");
				break;
			}
		}

		/*
		 * The table named a current schema its own "schemas" array omits.
		 * Refuse it as corruption rather than silently resolving to the
		 * deprecated top-level "schema", which would bind every column through a
		 * stale schema and misproject each row. Mirrors ice_current_snapshot's
		 * treatment of a dangling current-snapshot-id (#644).
		 */
		if (!matched)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: current-schema-id " INT64_FORMAT " in \"%s\" is not present in the schemas array",
							want, path)));
	}
	else						/* v1: a single top-level "schema" object */
	{
		JsonbValue *sch = ice_field(root, "schema");

		if (sch != NULL && sch->type == jbvBinary)
			fields = ice_field(sch->val.binary.data, "fields");
	}
	if (fields == NULL || fields->type != jbvBinary ||
		!JsonContainerIsArray(fields->val.binary.data))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: \"%s\" has no current schema fields", path)));
	return fields->val.binary.data;
}

/*
 * Build the field id to bind each output column to, by matching the output
 * column name to a field in the table's current schema. Returns a palloc'd array
 * of one id per non-dropped attribute, in attribute order; *nout is its length.
 * A column name absent from the schema is an error. Name matching is exact and
 * case-sensitive against the schema (quote a mixed-case name in the column
 * definition list to preserve its case).
 */
static int *
ice_field_ids_for_columns(JsonbContainer *root, TupleDesc tupdesc,
						  const char *path, int *nout)
{
	JsonbContainer *fieldsc = ice_current_schema_fields(root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	int			natts = tupdesc->natts;
	int		   *ids;
	int			outn = 0;
	int			i;

	for (i = 0; i < natts; i++)
		if (!TupleDescAttr(tupdesc, i)->attisdropped)
			outn++;
	ids = (int *) palloc(sizeof(int) * Max(outn, 1));
	*nout = 0;

	for (i = 0; i < natts; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);
		const char *nm;
		int			found = -1;
		uint32		j;

		if (att->attisdropped)
			continue;
		nm = NameStr(att->attname);
		for (j = 0; j < nf; j++)
		{
			JsonbValue *f = getIthJsonbValueFromContainer(fieldsc, j);
			JsonbValue *fn;
			int64		fid;

			if (f == NULL || f->type != jbvBinary)
				continue;
			fn = ice_field(f->val.binary.data, "name");
			if (fn == NULL || fn->type != jbvString)
				continue;
			if ((int) strlen(nm) == fn->val.string.len &&
				strncmp(nm, fn->val.string.val, fn->val.string.len) == 0)
			{
				fid = ice_schema_field_id(f->val.binary.data,
										  fn->val.string.val, fn->val.string.len);
				if (fid >= 0)
					found = (int) fid;
				break;
			}
		}
		if (found < 0)
			ereport(ERROR,
					(errcode(ERRCODE_UNDEFINED_COLUMN),
					 errmsg("iceberg: output column \"%s\" is not a field in the table's current schema",
							nm)));
		ids[(*nout)++] = found;
	}
	return ids;
}

/* find the schema field id of a column by name, or -1 */
static int64
ice_field_id_by_name(JsonbContainer *fieldsc, uint32 nf, const char *nm)
{
	uint32		j;

	for (j = 0; j < nf; j++)
	{
		JsonbValue *sf = getIthJsonbValueFromContainer(fieldsc, j);
		JsonbValue *sfn;

		if (sf == NULL || sf->type != jbvBinary)
			continue;
		sfn = ice_field(sf->val.binary.data, "name");
		if (sfn == NULL || sfn->type != jbvString)
			continue;
		if ((int) strlen(nm) == sfn->val.string.len &&
			strncmp(nm, sfn->val.string.val, sfn->val.string.len) == 0)
			return ice_schema_field_id(sf->val.binary.data,
									   sfn->val.string.val, sfn->val.string.len);
	}
	return -1;
}

/*
 * Whether the current-schema field named `nm` is a timestamp of the OTHER kind
 * than the PostgreSQL column `typid` (timestamp vs timestamptz, any unit). The
 * reader converts such a column's rows as PostgreSQL's casts do, in the session
 * zone (#1706), but the writer computed its temporal partition cells in the
 * source's own kind, so those cells do not bound the converted rows.
 */
static bool
ice_field_timestamp_kind_differs(JsonbContainer *fieldsc, uint32 nf,
								 const char *nm, Oid typid)
{
	uint32		j;

	if (typid != TIMESTAMPOID && typid != TIMESTAMPTZOID)
		return false;
	for (j = 0; j < nf; j++)
	{
		JsonbValue *sf = getIthJsonbValueFromContainer(fieldsc, j);
		JsonbValue *v;
		bool		src_tz;

		if (sf == NULL || sf->type != jbvBinary)
			continue;
		v = ice_field(sf->val.binary.data, "name");
		if (v == NULL || v->type != jbvString ||
			(int) strlen(nm) != v->val.string.len ||
			strncmp(nm, v->val.string.val, v->val.string.len) != 0)
			continue;
		v = ice_field(sf->val.binary.data, "type");
		if (v == NULL || v->type != jbvString)
			return false;
		if (v->val.string.len >= 11 &&
			strncmp(v->val.string.val, "timestamptz", 11) == 0)
			src_tz = true;
		else if (v->val.string.len >= 9 &&
				 strncmp(v->val.string.val, "timestamp", 9) == 0)
			src_tz = false;
		else
			return false;
		return src_tz != (typid == TIMESTAMPTZOID);
	}
	return false;
}

/*
 * Map the current partition spec's IDENTITY fields to foreign-table columns, for
 * FDW pruning. On return *out_attno[k] is the 1-based attno of the table column
 * an identity partition field at partition-tuple position k maps to (0 when the
 * field is a non-identity transform, or its source column is not in the table);
 * *out_npos is the number of partition fields in the current spec; *out_specid
 * is the current (default) spec id. A file whose own spec_id differs from
 * *out_specid must not be pruned with this map (partition evolution). The caller
 * frees nothing (palloc'd in the current context). Reads the metadata once.
 */
void
PgColumnarIcebergIdentityPartMap(const char *path,
								 const PgColumnarObjStoreConfig *cfg,
								 TupleDesc tupdesc,
								 int **out_attno, int *out_npos, int32 *out_specid)
{
	char	   *json = ice_slurp_text(path, cfg);
	Jsonb	   *jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
													   CStringGetDatum(json)));
	JsonbContainer *root = &jb->root;
	JsonbContainer *fieldsc = ice_current_schema_fields(root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	JsonbValue *dv;
	JsonbValue *specs;
	JsonbContainer *specfields = NULL;
	uint32		nspec = 0;
	uint32		k;
	int64		defspec = 0;
	int		   *attno;

	*out_attno = NULL;
	*out_npos = 0;
	*out_specid = 0;

	dv = ice_field(root, "default-spec-id");
	if (dv != NULL)
		(void) ice_num_int64(dv, &defspec);
	*out_specid = (int32) defspec;

	specs = ice_field(root, "partition-specs");
	if (specs != NULL && specs->type == jbvBinary &&
		JsonContainerIsArray(specs->val.binary.data))
	{
		uint32		ns = JsonContainerSize(specs->val.binary.data);
		uint32		i;

		for (i = 0; i < ns; i++)
		{
			JsonbValue *sp = getIthJsonbValueFromContainer(specs->val.binary.data, i);
			JsonbValue *sid;
			JsonbValue *flds;
			int64		sv;

			if (sp == NULL || sp->type != jbvBinary)
				continue;
			sid = ice_field(sp->val.binary.data, "spec-id");
			if (sid == NULL || !ice_num_int64(sid, &sv) || sv != defspec)
				continue;
			flds = ice_field(sp->val.binary.data, "fields");
			if (flds != NULL && flds->type == jbvBinary &&
				JsonContainerIsArray(flds->val.binary.data))
			{
				specfields = flds->val.binary.data;
				nspec = JsonContainerSize(specfields);
			}
			break;
		}
	}
	if (specfields == NULL || nspec == 0)
		return;					/* unpartitioned: nothing to prune */

	attno = (int *) palloc0(sizeof(int) * nspec);
	for (k = 0; k < nspec; k++)
	{
		JsonbValue *f = getIthJsonbValueFromContainer(specfields, k);
		JsonbValue *tr;
		JsonbValue *src;
		int64		srcid;
		int			a;

		if (f == NULL || f->type != jbvBinary)
			continue;
		tr = ice_field(f->val.binary.data, "transform");
		if (tr == NULL || tr->type != jbvString ||
			tr->val.string.len != 8 ||
			strncmp(tr->val.string.val, "identity", 8) != 0)
			continue;			/* only identity is prunable in this increment */
		src = ice_field(f->val.binary.data, "source-id");
		if (src == NULL || !ice_num_int64(src, &srcid))
			continue;
		for (a = 0; a < tupdesc->natts; a++)
		{
			Form_pg_attribute att = TupleDescAttr(tupdesc, a);

			if (att->attisdropped)
				continue;
			if (ice_field_id_by_name(fieldsc, nf, NameStr(att->attname)) == srcid)
			{
				attno[k] = a + 1;
				break;
			}
		}
	}
	*out_attno = attno;
	*out_npos = (int) nspec;
}

/*
 * Map the current partition spec's BUCKET[N] fields to foreign-table columns, for
 * FDW bucket pruning. For each bucket field: *out_pos[i] its partition-tuple
 * position, *out_attno[i] the source column's attno (0 if absent), *out_n[i] the
 * bucket count N. *out_count is the number of bucket fields. A file with a
 * different spec_id must not be pruned with this map (the caller checks spec id).
 */
void
PgColumnarIcebergBucketMap(const char *path, const PgColumnarObjStoreConfig *cfg,
						   TupleDesc tupdesc, int **out_pos, int **out_attno,
						   int **out_n, int *out_count, int32 *out_specid)
{
	char	   *json = ice_slurp_text(path, cfg);
	Jsonb	   *jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
													   CStringGetDatum(json)));
	JsonbContainer *root = &jb->root;
	JsonbContainer *fieldsc = ice_current_schema_fields(root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	JsonbValue *dv;
	JsonbValue *specs;
	JsonbContainer *specfields = NULL;
	uint32		nspec = 0;
	uint32		k;
	int64		defspec = 0;
	int		   *pos;
	int		   *attno;
	int		   *nn;
	int			cnt = 0;

	*out_pos = NULL;
	*out_attno = NULL;
	*out_n = NULL;
	*out_count = 0;
	*out_specid = 0;

	dv = ice_field(root, "default-spec-id");
	if (dv != NULL)
		(void) ice_num_int64(dv, &defspec);
	*out_specid = (int32) defspec;

	specs = ice_field(root, "partition-specs");
	if (specs != NULL && specs->type == jbvBinary &&
		JsonContainerIsArray(specs->val.binary.data))
	{
		uint32		ns = JsonContainerSize(specs->val.binary.data);
		uint32		i;

		for (i = 0; i < ns; i++)
		{
			JsonbValue *sp = getIthJsonbValueFromContainer(specs->val.binary.data, i);
			JsonbValue *sid;
			JsonbValue *flds;
			int64		sv;

			if (sp == NULL || sp->type != jbvBinary)
				continue;
			sid = ice_field(sp->val.binary.data, "spec-id");
			if (sid == NULL || !ice_num_int64(sid, &sv) || sv != defspec)
				continue;
			flds = ice_field(sp->val.binary.data, "fields");
			if (flds != NULL && flds->type == jbvBinary &&
				JsonContainerIsArray(flds->val.binary.data))
			{
				specfields = flds->val.binary.data;
				nspec = JsonContainerSize(specfields);
			}
			break;
		}
	}
	if (specfields == NULL || nspec == 0)
		return;

	pos = (int *) palloc(sizeof(int) * nspec);
	attno = (int *) palloc(sizeof(int) * nspec);
	nn = (int *) palloc(sizeof(int) * nspec);
	for (k = 0; k < nspec; k++)
	{
		JsonbValue *f = getIthJsonbValueFromContainer(specfields, k);
		JsonbValue *tr;
		JsonbValue *src;
		int64		srcid;
		int			N;
		char	   *ts;
		int			a;

		if (f == NULL || f->type != jbvBinary)
			continue;
		tr = ice_field(f->val.binary.data, "transform");
		if (tr == NULL || tr->type != jbvString)
			continue;
		/* the transform name for a bucket is "bucket[N]" */
		if (tr->val.string.len < 8 ||
			strncmp(tr->val.string.val, "bucket[", 7) != 0)
			continue;
		ts = pnstrdup(tr->val.string.val, tr->val.string.len);
		N = atoi(ts + 7);		/* the digits after "bucket[" */
		if (N <= 0)
			continue;
		src = ice_field(f->val.binary.data, "source-id");
		if (src == NULL || !ice_num_int64(src, &srcid))
			continue;
		for (a = 0; a < tupdesc->natts; a++)
		{
			Form_pg_attribute att = TupleDescAttr(tupdesc, a);

			if (att->attisdropped)
				continue;
			if (ice_field_id_by_name(fieldsc, nf, NameStr(att->attname)) == srcid)
			{
				pos[cnt] = (int) k;
				attno[cnt] = a + 1;
				nn[cnt] = N;
				cnt++;
				break;
			}
		}
	}
	*out_pos = pos;
	*out_attno = attno;
	*out_n = nn;
	*out_count = cnt;
}

/*
 * Map the current spec's TRUNCATE[W] fields to columns, for FDW truncate
 * pruning. Same shape as the bucket map: per truncate field its partition-tuple
 * position (*out_pos), source column attno (*out_attno), width W (*out_w);
 * *out_count fields; *out_specid the current spec id.
 */
void
PgColumnarIcebergTruncateMap(const char *path, const PgColumnarObjStoreConfig *cfg,
							 TupleDesc tupdesc, int **out_pos, int **out_attno,
							 int **out_w, int *out_count, int32 *out_specid)
{
	char	   *json = ice_slurp_text(path, cfg);
	Jsonb	   *jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
													   CStringGetDatum(json)));
	JsonbContainer *root = &jb->root;
	JsonbContainer *fieldsc = ice_current_schema_fields(root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	JsonbValue *dv;
	JsonbValue *specs;
	JsonbContainer *specfields = NULL;
	uint32		nspec = 0;
	uint32		k;
	int64		defspec = 0;
	int		   *pos;
	int		   *attno;
	int		   *ww;
	int			cnt = 0;

	*out_pos = NULL;
	*out_attno = NULL;
	*out_w = NULL;
	*out_count = 0;
	*out_specid = 0;

	dv = ice_field(root, "default-spec-id");
	if (dv != NULL)
		(void) ice_num_int64(dv, &defspec);
	*out_specid = (int32) defspec;

	specs = ice_field(root, "partition-specs");
	if (specs != NULL && specs->type == jbvBinary &&
		JsonContainerIsArray(specs->val.binary.data))
	{
		uint32		ns = JsonContainerSize(specs->val.binary.data);
		uint32		i;

		for (i = 0; i < ns; i++)
		{
			JsonbValue *sp = getIthJsonbValueFromContainer(specs->val.binary.data, i);
			JsonbValue *sid;
			JsonbValue *flds;
			int64		sv;

			if (sp == NULL || sp->type != jbvBinary)
				continue;
			sid = ice_field(sp->val.binary.data, "spec-id");
			if (sid == NULL || !ice_num_int64(sid, &sv) || sv != defspec)
				continue;
			flds = ice_field(sp->val.binary.data, "fields");
			if (flds != NULL && flds->type == jbvBinary &&
				JsonContainerIsArray(flds->val.binary.data))
			{
				specfields = flds->val.binary.data;
				nspec = JsonContainerSize(specfields);
			}
			break;
		}
	}
	if (specfields == NULL || nspec == 0)
		return;

	pos = (int *) palloc(sizeof(int) * nspec);
	attno = (int *) palloc(sizeof(int) * nspec);
	ww = (int *) palloc(sizeof(int) * nspec);
	for (k = 0; k < nspec; k++)
	{
		JsonbValue *f = getIthJsonbValueFromContainer(specfields, k);
		JsonbValue *tr;
		JsonbValue *src;
		int64		srcid;
		int			W;
		char	   *ts;
		int			a;

		if (f == NULL || f->type != jbvBinary)
			continue;
		tr = ice_field(f->val.binary.data, "transform");
		if (tr == NULL || tr->type != jbvString ||
			tr->val.string.len < 10 ||
			strncmp(tr->val.string.val, "truncate[", 9) != 0)
			continue;
		ts = pnstrdup(tr->val.string.val, tr->val.string.len);
		W = atoi(ts + 9);		/* the digits after "truncate[" */
		if (W <= 0)
			continue;
		src = ice_field(f->val.binary.data, "source-id");
		if (src == NULL || !ice_num_int64(src, &srcid))
			continue;
		for (a = 0; a < tupdesc->natts; a++)
		{
			Form_pg_attribute att = TupleDescAttr(tupdesc, a);

			if (att->attisdropped)
				continue;
			if (ice_field_id_by_name(fieldsc, nf, NameStr(att->attname)) == srcid)
			{
				pos[cnt] = (int) k;
				attno[cnt] = a + 1;
				ww[cnt] = W;
				cnt++;
				break;
			}
		}
	}
	*out_pos = pos;
	*out_attno = attno;
	*out_w = ww;
	*out_count = cnt;
}

/*
 * Map the current spec's day() fields to columns for FDW temporal pruning: per
 * day() field its partition-tuple position (*out_pos) and source column attno
 * (*out_attno); *out_count fields; *out_specid the current spec id. The stored
 * cell is the day as an int (days since the 1970 epoch), one date per file.
 */
void
PgColumnarIcebergDayMap(const char *path, const PgColumnarObjStoreConfig *cfg,
						TupleDesc tupdesc, int **out_pos, int **out_attno,
						int *out_count, int32 *out_specid)
{
	char	   *json = ice_slurp_text(path, cfg);
	Jsonb	   *jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
													   CStringGetDatum(json)));
	JsonbContainer *root = &jb->root;
	JsonbContainer *fieldsc = ice_current_schema_fields(root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	JsonbValue *dv;
	JsonbValue *specs;
	JsonbContainer *specfields = NULL;
	uint32		nspec = 0;
	uint32		k;
	int64		defspec = 0;
	int		   *pos;
	int		   *attno;
	int			cnt = 0;

	*out_pos = NULL;
	*out_attno = NULL;
	*out_count = 0;
	*out_specid = 0;

	dv = ice_field(root, "default-spec-id");
	if (dv != NULL)
		(void) ice_num_int64(dv, &defspec);
	*out_specid = (int32) defspec;

	specs = ice_field(root, "partition-specs");
	if (specs != NULL && specs->type == jbvBinary &&
		JsonContainerIsArray(specs->val.binary.data))
	{
		uint32		ns = JsonContainerSize(specs->val.binary.data);
		uint32		i;

		for (i = 0; i < ns; i++)
		{
			JsonbValue *sp = getIthJsonbValueFromContainer(specs->val.binary.data, i);
			JsonbValue *sid;
			JsonbValue *flds;
			int64		sv;

			if (sp == NULL || sp->type != jbvBinary)
				continue;
			sid = ice_field(sp->val.binary.data, "spec-id");
			if (sid == NULL || !ice_num_int64(sid, &sv) || sv != defspec)
				continue;
			flds = ice_field(sp->val.binary.data, "fields");
			if (flds != NULL && flds->type == jbvBinary &&
				JsonContainerIsArray(flds->val.binary.data))
			{
				specfields = flds->val.binary.data;
				nspec = JsonContainerSize(specfields);
			}
			break;
		}
	}
	if (specfields == NULL || nspec == 0)
		return;

	pos = (int *) palloc(sizeof(int) * nspec);
	attno = (int *) palloc(sizeof(int) * nspec);
	for (k = 0; k < nspec; k++)
	{
		JsonbValue *f = getIthJsonbValueFromContainer(specfields, k);
		JsonbValue *tr;
		JsonbValue *src;
		int64		srcid;
		int			a;

		if (f == NULL || f->type != jbvBinary)
			continue;
		tr = ice_field(f->val.binary.data, "transform");
		if (tr == NULL || tr->type != jbvString ||
			tr->val.string.len != 3 ||
			strncmp(tr->val.string.val, "day", 3) != 0)
			continue;
		src = ice_field(f->val.binary.data, "source-id");
		if (src == NULL || !ice_num_int64(src, &srcid))
			continue;
		for (a = 0; a < tupdesc->natts; a++)
		{
			Form_pg_attribute att = TupleDescAttr(tupdesc, a);

			if (att->attisdropped)
				continue;
			if (ice_field_id_by_name(fieldsc, nf, NameStr(att->attname)) == srcid)
			{
				pos[cnt] = (int) k;
				attno[cnt] = a + 1;
				cnt++;
				break;
			}
		}
	}
	*out_pos = pos;
	*out_attno = attno;
	*out_count = cnt;
}

/*
 * Like PgColumnarIcebergDayMap, but for an arbitrary order-preserving temporal
 * transform named `transform` ("year", "month", "day", or "hour"). Returns the
 * default spec's partition-cell positions and 1-based source attnos for the
 * fields carrying that transform. The FDW routes each mapped field by its source
 * column type: the coarse temporal compiler handles the timestamp cases, the
 * exact day() compiler handles a day() on a date. A field whose source is a
 * timestamp of the other kind than its column (timestamp vs timestamptz) is left
 * out, so nothing prunes on it: its cells are in the source's kind, its rows are
 * converted in the session zone (#1706).
 */
void
PgColumnarIcebergTemporalMap(const char *path, const PgColumnarObjStoreConfig *cfg,
							 TupleDesc tupdesc, const char *transform,
							 int **out_pos, int **out_attno, int *out_count,
							 int32 *out_specid)
{
	char	   *json = ice_slurp_text(path, cfg);
	Jsonb	   *jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
													   CStringGetDatum(json)));
	JsonbContainer *root = &jb->root;
	JsonbContainer *fieldsc = ice_current_schema_fields(root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	int			trlen = (int) strlen(transform);
	JsonbValue *dv;
	JsonbValue *specs;
	JsonbContainer *specfields = NULL;
	uint32		nspec = 0;
	uint32		k;
	int64		defspec = 0;
	int		   *pos;
	int		   *attno;
	int			cnt = 0;

	*out_pos = NULL;
	*out_attno = NULL;
	*out_count = 0;
	*out_specid = 0;

	dv = ice_field(root, "default-spec-id");
	if (dv != NULL)
		(void) ice_num_int64(dv, &defspec);
	*out_specid = (int32) defspec;

	specs = ice_field(root, "partition-specs");
	if (specs != NULL && specs->type == jbvBinary &&
		JsonContainerIsArray(specs->val.binary.data))
	{
		uint32		ns = JsonContainerSize(specs->val.binary.data);
		uint32		i;

		for (i = 0; i < ns; i++)
		{
			JsonbValue *sp = getIthJsonbValueFromContainer(specs->val.binary.data, i);
			JsonbValue *sid;
			JsonbValue *flds;
			int64		sv;

			if (sp == NULL || sp->type != jbvBinary)
				continue;
			sid = ice_field(sp->val.binary.data, "spec-id");
			if (sid == NULL || !ice_num_int64(sid, &sv) || sv != defspec)
				continue;
			flds = ice_field(sp->val.binary.data, "fields");
			if (flds != NULL && flds->type == jbvBinary &&
				JsonContainerIsArray(flds->val.binary.data))
			{
				specfields = flds->val.binary.data;
				nspec = JsonContainerSize(specfields);
			}
			break;
		}
	}
	if (specfields == NULL || nspec == 0)
		return;

	pos = (int *) palloc(sizeof(int) * nspec);
	attno = (int *) palloc(sizeof(int) * nspec);
	for (k = 0; k < nspec; k++)
	{
		JsonbValue *f = getIthJsonbValueFromContainer(specfields, k);
		JsonbValue *tr;
		JsonbValue *src;
		int64		srcid;
		int			a;

		if (f == NULL || f->type != jbvBinary)
			continue;
		tr = ice_field(f->val.binary.data, "transform");
		if (tr == NULL || tr->type != jbvString ||
			tr->val.string.len != trlen ||
			strncmp(tr->val.string.val, transform, trlen) != 0)
			continue;
		src = ice_field(f->val.binary.data, "source-id");
		if (src == NULL || !ice_num_int64(src, &srcid))
			continue;
		for (a = 0; a < tupdesc->natts; a++)
		{
			Form_pg_attribute att = TupleDescAttr(tupdesc, a);

			if (att->attisdropped)
				continue;
			if (ice_field_id_by_name(fieldsc, nf, NameStr(att->attname)) == srcid)
			{
				/* the cells are in the source's kind, the rows in the column's */
				if (ice_field_timestamp_kind_differs(fieldsc, nf,
													 NameStr(att->attname),
													 att->atttypid))
					break;
				pos[cnt] = (int) k;
				attno[cnt] = a + 1;
				cnt++;
				break;
			}
		}
	}
	*out_pos = pos;
	*out_attno = attno;
	*out_count = cnt;
}

/*
 * Return, for each column of `tupdesc`, the current-schema field id of the
 * column with that name (0 when the table has no such column), for FDW metrics
 * pruning that keys a data file's bounds by field id. palloc'd, length natts.
 */
int *
PgColumnarIcebergColumnFieldIds(const char *path,
								const PgColumnarObjStoreConfig *cfg,
								TupleDesc tupdesc)
{
	char	   *json = ice_slurp_text(path, cfg);
	Jsonb	   *jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
													   CStringGetDatum(json)));
	JsonbContainer *fieldsc = ice_current_schema_fields(&jb->root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	int		   *out = (int *) palloc0(sizeof(int) * Max(tupdesc->natts, 1));
	int			a;

	for (a = 0; a < tupdesc->natts; a++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, a);
		int64		fid;

		if (att->attisdropped)
			continue;
		fid = ice_field_id_by_name(fieldsc, nf, NameStr(att->attname));
		if (fid > 0 && fid <= PG_INT32_MAX)
			out[a] = (int) fid;
	}
	return out;
}

/* A name mapping has one entry per top-level column plus a few aliases; a real
 * schema is well under this. The metadata.json is capped at 64 MB, so without a
 * bound a crafted mapping could carry millions of names -- refuse rather than
 * spend the memory and time on it. */
#define ICE_MAX_NAME_MAPPING 100000

/* qsort comparator over char* by string value, for the uniqueness check */
static int
ice_cmp_namep(const void *a, const void *b)
{
	return strcmp(*(const char *const *) a, *(const char *const *) b);
}

/*
 * Parse the table's schema.name-mapping.default property into a flat (name ->
 * field id) table, for reading data files that carry no field ids. The property
 * lives at metadata root `properties` and its value is a JSON *string* holding
 * an array of {names: string[], field-id: int, fields: [...]} objects. Only the
 * top-level entries are flattened (the reader binds flat scalars; nested
 * `fields` are ignored), emitting one (name, id) pair per name. A name that
 * appears twice is corrupt metadata (the spec requires names to be unique).
 * Returns palloc'd parallel arrays; *nout is 0 when the property is absent, so
 * the caller preserves the no-mapping refusal for an id-less file.
 */
static void
ice_name_mapping(JsonbContainer *root, const char *path,
				 char ***names_out, int **ids_out, int *nout)
{
	JsonbValue *props = ice_field(root, "properties");
	JsonbValue *nm;
	Jsonb	   *arrjb;
	JsonbContainer *arr;
	uint32		ne;
	uint32		k;
	char	  **names;
	int		   *ids;
	int			n = 0;
	int			cap;
	MemoryContext callerCxt = CurrentMemoryContext;
	MemoryContext entryCxt;

	*names_out = NULL;
	*ids_out = NULL;
	*nout = 0;

	if (props == NULL || props->type != jbvBinary)
		return;
	nm = ice_field(props->val.binary.data, "schema.name-mapping.default");
	if (nm == NULL || nm->type != jbvString)
		return;

	/* the property value is itself a JSON document (a string), re-parsed here */
	arrjb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
											   CStringGetDatum(pnstrdup(nm->val.string.val,
																		nm->val.string.len))));
	if (!JsonContainerIsArray(&arrjb->root))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: \"%s\" schema.name-mapping.default is not a JSON array",
						path)));
	arr = &arrjb->root;
	ne = JsonContainerSize(arr);

	/*
	 * Size the FIRST allocation by the cap, not by the element count the file
	 * declares (#711).
	 *
	 * ne is attacker-controlled and is not an upper bound on the name count
	 * anyway -- one entry carries a list of names, so n can exceed ne -- which
	 * is why the array grows by doubling below. It is only an opening guess,
	 * and taking it from the file meant a document of N tiny entries asked for
	 * 12N bytes up front: about four bytes of allocation per byte of input,
	 * from a path whose only other bound is the 64 MB metadata slurp several
	 * layers away, which protects a different thing and could change without
	 * this being reconsidered.
	 *
	 * ICE_MAX_NAME_MAPPING is the real ceiling: the loop below refuses the
	 * document once n reaches it, so n can never exceed it and any capacity
	 * beyond it could never be used. The doubling may still overshoot the cap
	 * by up to one step, which is bounded and not worth a second clamp.
	 */
	cap = Max(Min(ne, (uint32) ICE_MAX_NAME_MAPPING), 1);
	names = (char **) palloc(sizeof(char *) * cap);
	ids = (int *) palloc(sizeof(int) * cap);

	/*
	 * Per-entry scratch, reclaimed each iteration (#731).
	 *
	 * Walking one entry allocates a JsonbValue per getIthJsonbValueFromContainer
	 * and per ice_field, and none of it is needed once the entry's names have
	 * been copied out. Left in the caller's context it accumulated for the whole
	 * document: measured at ~100 bytes an entry, about 78 MB over an 800000-entry
	 * mapping, held until the call returned.
	 *
	 * This is the smaller half of what #731 was filed for and worth saying so:
	 * the other 87% of that document's cost is core's jsonb_in parsing the
	 * property string, which this code does not control. An unprivileged session
	 * can reach a larger version of it with a plain `::jsonb` cast.
	 *
	 * The parsed document itself (arrjb) stays in the caller's context, so the
	 * containers these JsonbValues point INTO are untouched by the reset; only
	 * the per-entry wrappers are reclaimed. Retained names are copied in the
	 * caller's context explicitly, below.
	 */
	entryCxt = AllocSetContextCreate(callerCxt,
									 "iceberg name-mapping entry",
									 ALLOCSET_SMALL_SIZES);

	for (k = 0; k < ne; k++)
	{
		JsonbValue *ent;
		int64		fid;
		JsonbValue *namesv;
		JsonbContainer *narr;
		uint32		nn;
		uint32		m;

		/*
		 * Reset at the TOP, not after the body. Every `continue` below would
		 * otherwise skip the reclaim, and those are the common paths: an entry
		 * with no field-id is exactly the shape that made this measurable.
		 */
		MemoryContextReset(entryCxt);
		MemoryContextSwitchTo(entryCxt);

		ent = getIthJsonbValueFromContainer(arr, k);

		CHECK_FOR_INTERRUPTS();
		if (ent == NULL || ent->type != jbvBinary)
			continue;
		/* an entry without a field-id is an unmapped column; skip it */
		if (!ice_num_int64(ice_field(ent->val.binary.data, "field-id"), &fid))
			continue;
		/* a field id beyond int32 would truncate and could alias a real id */
		if (fid < 0 || fid > PG_INT32_MAX)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: \"%s\" schema.name-mapping.default has a field-id out of range: " INT64_FORMAT,
							path, fid)));
		namesv = ice_field(ent->val.binary.data, "names");
		if (namesv == NULL || namesv->type != jbvBinary ||
			!JsonContainerIsArray(namesv->val.binary.data))
			continue;
		narr = namesv->val.binary.data;
		nn = JsonContainerSize(narr);
		for (m = 0; m < nn; m++)
		{
			JsonbValue *nv = getIthJsonbValueFromContainer(narr, m);
			char	   *nstr;

			if (nv == NULL || nv->type != jbvString)
				continue;
			/* the retained name must outlive the per-entry reset */
			MemoryContextSwitchTo(callerCxt);
			nstr = pnstrdup(nv->val.string.val, nv->val.string.len);
			if (n >= ICE_MAX_NAME_MAPPING)
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("iceberg: \"%s\" schema.name-mapping.default has more than %d names",
								path, ICE_MAX_NAME_MAPPING)));
			if (n == cap)
			{
				cap *= 2;
				names = (char **) repalloc(names, sizeof(char *) * cap);
				ids = (int *) repalloc(ids, sizeof(int) * cap);
			}
			names[n] = nstr;
			ids[n] = (int) fid;
			n++;
			MemoryContextSwitchTo(entryCxt);
		}
	}

	/*
	 * Leave the scratch context before anything below allocates in it, and
	 * before deleting it. A `continue` can exit the loop body while it is still
	 * current, so this restore is unconditional rather than paired with a switch
	 * inside the loop.
	 */
	MemoryContextSwitchTo(callerCxt);
	MemoryContextDelete(entryCxt);

	/* the spec requires names to be unique. Detect a duplicate by sorting a
	 * copy of the name pointers and scanning adjacent pairs -- O(n log n), not
	 * the O(n^2) of an each-against-all scan over an attacker-sized mapping. */
	if (n > 1)
	{
		char	  **sorted = (char **) palloc(sizeof(char *) * n);
		int			i;

		memcpy(sorted, names, sizeof(char *) * n);
		qsort(sorted, n, sizeof(char *), ice_cmp_namep);
		for (i = 1; i < n; i++)
		{
			CHECK_FOR_INTERRUPTS();
			if (strcmp(sorted[i - 1], sorted[i]) == 0)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: name \"%s\" appears more than once in \"%s\" schema.name-mapping.default",
								sorted[i], path)));
		}
		pfree(sorted);
	}

	*names_out = names;
	*ids_out = ids;
	*nout = n;
}

/* the reserved Iceberg field ids of a position-delete file's two columns */
#define ICE_POSDEL_PATH_ID 2147483546
#define ICE_POSDEL_POS_ID  2147483545

/* one collected manifest entry: a data file or a delete file of either kind */
typedef struct IceEntry
{
	char	   *file_path;		/* the path as recorded (rebased at read time) */
	char	   *file_format;	/* for the "only PARQUET" check on data files */
	int64		seq;			/* resolved data sequence number */
	int32	   *eq_ids;			/* content 2: the field ids defining equality */
	int			neq_ids;
	int32		spec_id;		/* the enclosing manifest's partition spec id */
	bool		has_spec_id;	/* false when the manifest list omitted it */
	char	   *ref_data_file;	/* v3 DV: the one data file it targets */
	int64		content_offset; /* v3 DV: blob offset in the Puffin file */
	bool		has_content_offset;
	int64		content_size;	/* v3 DV: blob length */
	bool		has_content_size;
	int64		record_count;	/* for a DV, its cardinality per the spec */
	PgColumnarAvroPartCell *part_cells; /* typed partition tuple, or NULL */
	int			npart_cells;
	PgColumnarAvroBound *lower_bounds;	/* per-field-id column bounds, or NULL */
	int			nlower;
	PgColumnarAvroBound *upper_bounds;
	int			nupper;
}			IceEntry;

/* deep-copy a bound map into the current memory context */
static void
ice_copy_bounds(const PgColumnarAvroBound *src, int nsrc,
				PgColumnarAvroBound **out, int *nout)
{
	int			i;

	*out = NULL;
	*nout = 0;
	if (nsrc <= 0)
		return;
	*out = (PgColumnarAvroBound *) palloc0(sizeof(PgColumnarAvroBound) * nsrc);
	*nout = nsrc;
	for (i = 0; i < nsrc; i++)
	{
		(*out)[i] = src[i];
		if (src[i].bytes != NULL)
		{
			(*out)[i].bytes = (char *) palloc(Max(src[i].blen, 1));
			if (src[i].blen > 0)
				memcpy((*out)[i].bytes, src[i].bytes, src[i].blen);
		}
	}
}

/* deep-copy a partition tuple into the current memory context */
static void
ice_copy_part_cells(const PgColumnarAvroPartCell *src, int nsrc,
					PgColumnarAvroPartCell **out, int *nout)
{
	int			i;

	*out = NULL;
	*nout = 0;
	if (nsrc <= 0)
		return;
	*out = (PgColumnarAvroPartCell *)
		palloc0(sizeof(PgColumnarAvroPartCell) * nsrc);
	*nout = nsrc;
	for (i = 0; i < nsrc; i++)
	{
		(*out)[i] = src[i];
		if (src[i].is_bytes && src[i].bytes != NULL)
		{
			(*out)[i].bytes = (char *) palloc(src[i].blen);
			memcpy((*out)[i].bytes, src[i].bytes, src[i].blen);
		}
	}
}

/* are two partition tuples exactly equal? Caller must ensure both are fully
 * comparable (no float/double/unhandled cell); an incomparable cell here is a
 * programming error, treated as not-equal defensively. */
static bool
ice_part_cells_equal(const PgColumnarAvroPartCell *a, int na,
					 const PgColumnarAvroPartCell *b, int nb)
{
	int			i;

	if (na != nb)
		return false;
	for (i = 0; i < na; i++)
	{
		if (!a[i].comparable || !b[i].comparable)
			return false;
		if (a[i].isnull != b[i].isnull)
			return false;
		if (a[i].isnull)
			continue;
		if (a[i].is_bytes != b[i].is_bytes)
			return false;
		if (a[i].is_bytes)
		{
			if (a[i].blen != b[i].blen ||
				memcmp(a[i].bytes, b[i].bytes, a[i].blen) != 0)
				return false;
		}
		else if (a[i].ival != b[i].ival)
			return false;
	}
	return true;
}

/* does a partition tuple contain a cell the reader cannot compare exactly? */
static bool
ice_part_incomparable(const PgColumnarAvroPartCell *cells, int n)
{
	int			i;

	for (i = 0; i < n; i++)
		if (!cells[i].comparable)
			return true;
	return false;
}

/* one position-delete row: drop row `pos` of data file `dpath`, if this delete's
 * sequence number is greater than that data file's */
typedef struct IcePosDel
{
	char	   *dpath;
	int64		pos;
	int64		seq;
}			IcePosDel;

/* iceberg_scan collects the current snapshot's entries first (data and
 * position-delete files), then reads the data files applying the deletes. */
typedef struct IceScanCtx
{
	Tuplestorestate *tupstore;
	TupleDesc	tupdesc;
	TupleTableSlot *slot;
	const int  *field_ids;
	int			nfield;
	char	  **nm_names;		/* name mapping: id-less file columns by name */
	int		   *nm_ids;
	int			nm_count;
	MemoryContext filectx;
	char	   *recorded_root;
	char	   *actual_root;
	char	   *mdpath;
	const PgColumnarObjStoreConfig *cfg;	/* vended storage creds, or NULL */
	const bool *needTop;		/* projection: which output columns to decode, or NULL for all */
	List	   *data;			/* IceEntry* content 0 */
	List	   *posdel;			/* IceEntry* content 1 */
	List	   *eqdel;			/* IceEntry* content 2 */
}			IceScanCtx;

static void
ice_scan_cb(void *ctx, PgColumnarAvroManifestEntry *e,
			const PgColumnarAvroManifestFile *mf,
			const char *recorded_root, const char *actual_root,
			const char *mdpath, int64 snapshot_id)
{
	IceScanCtx *c = (IceScanCtx *) ctx;
	IceEntry   *ie = (IceEntry *) palloc0(sizeof(IceEntry));

	(void) snapshot_id;
	if (c->recorded_root == NULL)
	{
		c->recorded_root = pstrdup(recorded_root);
		c->actual_root = pstrdup(actual_root);
		c->mdpath = pstrdup(mdpath);
	}
	ie->file_path = pstrdup(e->file_path);
	ie->file_format = e->file_format ? pstrdup(e->file_format) : NULL;
	ie->seq = e->sequence_number;
	ie->spec_id = mf->partition_spec_id;
	ie->has_spec_id = mf->has_partition_spec_id;
	ie->ref_data_file = e->referenced_data_file
		? pstrdup(e->referenced_data_file) : NULL;
	ie->content_offset = e->content_offset;
	ie->has_content_offset = e->has_content_offset;
	ie->content_size = e->content_size_in_bytes;
	ie->has_content_size = e->has_content_size;
	ie->record_count = e->record_count;
	ice_copy_part_cells(e->part_cells, e->npart_cells,
						&ie->part_cells, &ie->npart_cells);
	ice_copy_bounds(e->lower_bounds, e->nlower, &ie->lower_bounds, &ie->nlower);
	ice_copy_bounds(e->upper_bounds, e->nupper, &ie->upper_bounds, &ie->nupper);
	if (e->nequality_ids > 0)
	{
		ie->eq_ids = (int32 *) palloc(e->nequality_ids * sizeof(int32));
		memcpy(ie->eq_ids, e->equality_ids, e->nequality_ids * sizeof(int32));
		ie->neq_ids = e->nequality_ids;
	}
	if (e->content == 0)
		c->data = lappend(c->data, ie);
	else if (e->content == 1)
		c->posdel = lappend(c->posdel, ie);
	else
		c->eqdel = lappend(c->eqdel, ie);
}

/*
 * Read every collected position-delete file into a flat list of (dpath, pos,
 * seq). Each file is opened through the path boundary and read by the reserved
 * field ids, so column names/order in the file do not matter.
 */
static List *
ice_read_pos_deletes(IceScanCtx *c)
{
	List	   *out = NIL;
	ListCell   *lc;
	TupleDesc	dtd;
	TupleTableSlot *wslot;
	TupleTableSlot *rslot;
	MemoryContext filectx;
	int			fids[2];

	if (c->posdel == NIL)
		return NIL;

	dtd = CreateTemplateTupleDesc(2);
	TupleDescInitEntry(dtd, 1, "file_path", TEXTOID, -1, 0);
	TupleDescInitEntry(dtd, 2, "pos", INT8OID, -1, 0);
#if PG_VERSION_NUM >= 190000
	/* PG19 asserts firstNonCachedOffsetAttr >= 0 on a manually built tupdesc;
	 * BlessTupleDesc does not compute it, TupleDescFinalize does */
	TupleDescFinalize(dtd);
#endif
	dtd = BlessTupleDesc(dtd);
	wslot = MakeSingleTupleTableSlot(dtd, &TTSOpsVirtual);
	rslot = MakeSingleTupleTableSlot(dtd, &TTSOpsMinimalTuple);
	fids[0] = ICE_POSDEL_PATH_ID;
	fids[1] = ICE_POSDEL_POS_ID;

	/* each delete file's Parquet footer + metadata (which pq_read_file_into
	 * leaves for its caller to reclaim) live in this scratch context, reset per
	 * file so an upsert-heavy table's many delete files do not accumulate
	 * O(files) footers. The (dpath, pos, seq) rows we keep are allocated in the
	 * outer context so they survive the reset. */
	filectx = AllocSetContextCreate(CurrentMemoryContext,
									"pgcolumnar iceberg_scan posdel",
									ALLOCSET_DEFAULT_SIZES);

	foreach(lc, c->posdel)
	{
		IceEntry   *pd = (IceEntry *) lfirst(lc);
		char	   *safe;
		Tuplestorestate *ts;
		MemoryContext old;

		/* puffin entries are v3 deletion vectors, read by ice_read_dvs */
		if (pd->file_format != NULL &&
			pg_strcasecmp(pd->file_format, "PUFFIN") == 0)
			continue;
		/* v2 position deletes are Parquet; any other delete-file format is
		 * refused here with a clear cause rather than a parse error */
		if (pd->file_format != NULL && strcmp(pd->file_format, "PARQUET") != 0)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("iceberg: position-delete file \"%s\" has format %s; only PARQUET position deletes are supported",
							pd->file_path, pd->file_format)));

		old = MemoryContextSwitchTo(filectx);
		safe = ice_open_path(c->recorded_root, c->actual_root,
							 pd->file_path, "position-delete", c->mdpath);
		ts = tuplestore_begin_heap(false, false, work_mem);
		(void) PgColumnarReadParquetByFieldId(safe, dtd, fids, 2, ts, wslot, NULL, 0,
											  c->cfg);
		while (tuplestore_gettupleslot(ts, true, false, rslot))
		{
			bool		n1,
						n2;
			Datum		d1 = slot_getattr(rslot, 1, &n1);
			Datum		d2 = slot_getattr(rslot, 2, &n2);
			int64		pos;
			IcePosDel  *r;

			/*
			 * Per the Iceberg spec a position-delete row's file_path (field id
			 * 2147483546) and pos (2147483545) are both REQUIRED. A null in
			 * either is corruption: silently dropping the row (the old
			 * "if (!n1 && !n2)" with no else) leaves a row the delete was meant
			 * to remove -- a phantom row -- with no diagnostic (#644).
			 */
			if (n1 || n2)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: position-delete file \"%s\" has a null %s",
								pd->file_path, n1 ? "file_path" : "pos")));

			/*
			 * pos is a non-negative row ordinal; 0 (the first row) is valid. A
			 * negative pos names no row and, cast to uint64 at apply time, would
			 * become a huge ordinal that silently matches nothing -- refuse it
			 * as corruption rather than swallow the delete (#644).
			 */
			pos = DatumGetInt64(d2);
			if (pos < 0)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: position-delete file \"%s\" has a negative pos " INT64_FORMAT,
								pd->file_path, pos)));

			/* keep the row in the outer context, not the per-file scratch */
			MemoryContextSwitchTo(old);
			r = (IcePosDel *) palloc(sizeof(IcePosDel));
			r->dpath = text_to_cstring(DatumGetTextPP(d1));
			r->pos = pos;
			r->seq = pd->seq;
			out = lappend(out, r);
			MemoryContextSwitchTo(filectx);
		}
		tuplestore_end(ts);
		MemoryContextSwitchTo(old);
		MemoryContextReset(filectx);
	}
	MemoryContextDelete(filectx);
	ExecDropSingleTupleTableSlot(wslot);
	ExecDropSingleTupleTableSlot(rslot);
	return out;
}

/* one decoded v3 deletion vector: the sorted ordinals to drop from `dpath`,
 * when this DV's data sequence number is at or above that data file's */
typedef struct IceDvDel
{
	char	   *dpath;			/* the referenced data file, as recorded */
	int64		seq;
	uint64	   *pos;			/* ascending ordinals */
	int64		npos;
}			IceDvDel;

/*
 * Read every collected puffin-format delete entry (a v3 deletion vector) into
 * a list of IceDvDel. Validates each entry: deletion vectors exist only from
 * format-version 3 (0A000 below that), referenced_data_file and the blob
 * offset/length are required (XX001), at most one DV may reference a data
 * file in a snapshot (the spec leaves violations undefined and allows readers
 * to raise -- we raise, XX001), and the entry's record_count must equal the
 * decoded cardinality (the spec defines it as exactly that). The Puffin file
 * is opened through the path boundary and slurped whole (the metadata cap
 * bounds it); columnar_puffin.c validates the container, the manifest/footer
 * offset cross-check, the blob framing, and the CRC. The per-file buffer
 * lives in a scratch context reset per file; the decoded ordinal arrays are
 * allocated in the calling context.
 */
static List *
ice_read_dvs(IceScanCtx *c, int64 format_version)
{
	List	   *out = NIL;
	List	   *seen = NIL;
	ListCell   *lc;
	MemoryContext filectx;

	filectx = AllocSetContextCreate(CurrentMemoryContext,
									"pgcolumnar iceberg_scan dv",
									ALLOCSET_DEFAULT_SIZES);

	foreach(lc, c->posdel)
	{
		IceEntry   *pd = (IceEntry *) lfirst(lc);
		IceDvDel   *dv;
		ListCell   *lc2;
		char	   *safe;
		uint8	   *buf;
		int64		len;
		MemoryContext old;

		if (pd->file_format == NULL ||
			pg_strcasecmp(pd->file_format, "PUFFIN") != 0)
			continue;

		if (format_version < 3)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("iceberg: \"%s\" is a deletion vector, which format-version " INT64_FORMAT " does not support",
							pd->file_path, format_version),
					 errdetail("Deletion vectors exist from format-version 3.")));
		if (pd->ref_data_file == NULL)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: the deletion vector in \"%s\" has no referenced_data_file",
							pd->file_path)));
		if (!pd->has_content_offset || !pd->has_content_size ||
			pd->content_offset < 0 || pd->content_size <= 0)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: the deletion vector in \"%s\" has no content offset or size",
							pd->file_path)));
		/* the per-file merge matches on the scheme-stripped path, so the
		 * one-DV-per-data-file check must too, or an aliased pair (one with a
		 * file:// scheme, one without) would slip past and be unioned */
		foreach(lc2, seen)
			if (strcmp(ice_strip_scheme((const char *) lfirst(lc2)),
					   ice_strip_scheme(pd->ref_data_file)) == 0)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: two deletion vectors reference data file \"%s\"",
								pd->ref_data_file),
						 errdetail("A snapshot may carry at most one deletion vector per data file.")));
		seen = lappend(seen, pd->ref_data_file);

		/* Slurp AND decode in the per-file scratch context; the Puffin footer
		 * (a payload copy plus its parsed jsonb, up to the metadata cap) must
		 * not survive per DV entry -- a snapshot with many DVs, or many DVs
		 * sharing one large-footer Puffin file, would otherwise retain
		 * O(entries * footer) in the query context. Only the decoded ordinal
		 * array is copied out to survive the reset. */
		old = MemoryContextSwitchTo(filectx);
		safe = ice_open_path(c->recorded_root, c->actual_root,
							 pd->file_path, "deletion-vector", c->mdpath);
		buf = ice_slurp_bin(safe, &len, c->cfg);
		{
			uint64	   *scratch_pos;
			int64		scratch_n;

			PgColumnarPuffinReadDeletionVector(buf, len,
											   pd->content_offset,
											   pd->content_size,
											   pd->ref_data_file,
											   pd->file_path,
											   &scratch_pos, &scratch_n);
			/* record_count is defined as the DV's cardinality */
			if (scratch_n != pd->record_count)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: the deletion vector in \"%s\" names " INT64_FORMAT " positions but its manifest entry records " INT64_FORMAT,
								pd->file_path, scratch_n, pd->record_count)));
			MemoryContextSwitchTo(old);
			dv = (IceDvDel *) palloc(sizeof(IceDvDel));
			dv->dpath = pstrdup(pd->ref_data_file);
			dv->seq = pd->seq;
			dv->npos = scratch_n;
			dv->pos = scratch_n > 0
				? (uint64 *) palloc(scratch_n * sizeof(uint64)) : NULL;
			if (scratch_n > 0)
				memcpy(dv->pos, scratch_pos, scratch_n * sizeof(uint64));
		}
		out = lappend(out, dv);
		MemoryContextReset(filectx);
	}
	MemoryContextDelete(filectx);
	list_free(seen);
	return out;
}

/*
 * Is partition spec `spec_id` a partitioned spec? Resolved against the
 * metadata's "partition-specs"; a spec id the metadata does not define leaves
 * an equality delete's scope undecidable, which is corrupt metadata (XX001).
 */
static bool
ice_spec_is_partitioned(JsonbContainer *root, int32 spec_id, const char *path)
{
	JsonbValue *specs = ice_field(root, "partition-specs");

	if (specs != NULL && specs->type == jbvBinary &&
		JsonContainerIsArray(specs->val.binary.data))
	{
		JsonbContainer *arr = specs->val.binary.data;
		uint32		ns = JsonContainerSize(arr);
		uint32		k;

		for (k = 0; k < ns; k++)
		{
			JsonbValue *sp = getIthJsonbValueFromContainer(arr, k);
			int64		sid;
			JsonbValue *fields;

			if (sp == NULL || sp->type != jbvBinary)
				continue;
			if (!ice_num_int64(ice_field(sp->val.binary.data, "spec-id"), &sid) ||
				sid != spec_id)
				continue;
			/* a matched spec whose required "fields" is missing or mistyped
			 * cannot prove the spec unpartitioned; defaulting to unpartitioned
			 * would silently globalize a possibly partition-scoped delete */
			fields = ice_field(sp->val.binary.data, "fields");
			if (fields == NULL || fields->type != jbvBinary ||
				!JsonContainerIsArray(fields->val.binary.data))
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: partition spec %d in \"%s\" has no fields array",
								spec_id, path)));
			return JsonContainerSize(fields->val.binary.data) > 0;
		}
	}
	ereport(ERROR,
			(errcode(ERRCODE_DATA_CORRUPTED),
			 errmsg("iceberg: \"%s\" does not define partition spec %d, which an equality delete's scope depends on",
					path, spec_id)));
	return false;				/* unreachable */
}

/*
 * Resolve an equality-delete column: find field id `fid` in the table's current
 * schema and map its Iceberg type to the Postgres type it is read and compared
 * as. The supported primitives cover the types the Parquet reader can project;
 * anything else is refused loudly (0A000) BEFORE any file is opened -- never
 * silently unapplied. *fname gets the schema field name (for the tupdesc and
 * error messages).
 */
static Oid
ice_eq_col_type(JsonbContainer *root, int32 fid, const char *path,
				const char **fname)
{
	JsonbContainer *fieldsc = ice_current_schema_fields(root, path);
	uint32		nf = JsonContainerSize(fieldsc);
	uint32		j;

	for (j = 0; j < nf; j++)
	{
		JsonbValue *f = getIthJsonbValueFromContainer(fieldsc, j);
		int64		id;
		JsonbValue *fn;
		JsonbValue *ft;
		char	   *tname;

		if (f == NULL || f->type != jbvBinary)
			continue;
		if (!ice_num_int64(ice_field(f->val.binary.data, "id"), &id) ||
			id != fid)
			continue;

		fn = ice_field(f->val.binary.data, "name");
		*fname = (fn != NULL && fn->type == jbvString)
			? pnstrdup(fn->val.string.val, fn->val.string.len)
			: psprintf("field_%d", fid);
		ft = ice_field(f->val.binary.data, "type");
		if (ft == NULL || ft->type != jbvString)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("iceberg: equality delete on field \"%s\" of a non-primitive type is not supported",
							*fname)));
		tname = pnstrdup(ft->val.string.val, ft->val.string.len);
		if (strcmp(tname, "int") == 0)
			return INT4OID;
		if (strcmp(tname, "long") == 0)
			return INT8OID;
		if (strcmp(tname, "string") == 0)
			return TEXTOID;
		if (strcmp(tname, "boolean") == 0)
			return BOOLOID;
		if (strcmp(tname, "date") == 0)
			return DATEOID;
		/* float/double are forbidden as equality-delete columns by the spec;
		 * everything else simply has no mapping yet */
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("iceberg: equality delete on field \"%s\" of type \"%s\" is not supported",
						*fname, tname)));
	}
	ereport(ERROR,
			(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
			 errmsg("iceberg: equality delete field id %d is not a top-level field of the table's current schema; equality deletes on dropped or nested columns are not supported",
					fid)));
	return InvalidOid;			/* unreachable */
}

/* one equality-delete row: the delete-column values, in the file's id order */
typedef struct IceEqRow
{
	Datum	   *vals;
	bool	   *nulls;
}			IceEqRow;

/* one equality-delete file, read into memory: its sequence number, the columns
 * that define the match, and every delete row */
typedef struct IceEqDel
{
	int64		seq;
	int			nids;
	int32	   *ids;
	Oid		   *types;
	int16	   *typlen;
	bool	   *typbyval;
	List	   *rows;			/* IceEqRow* */
	bool		partitioned;	/* delete written under a partitioned spec */
	int32		spec_id;		/* its partition spec id (partitioned only) */
	PgColumnarAvroPartCell *part_cells; /* its partition tuple (partitioned) */
	int			npart_cells;
}			IceEqDel;

/*
 * Read every applicable equality-delete file into memory. A delete whose
 * sequence number exceeds no data file's (seq <= min_data_seq) can never apply
 * -- the strict-< rule -- and is skipped entirely, unread and unvalidated, per
 * the spec (it has no effect on the scan). Each applicable entry is validated
 * (equality_ids present, PARQUET, a present partition_spec_id resolving to an
 * unpartitioned spec -- a partition-scoped equality delete applied globally
 * would over-delete, so it is refused), each equality column is mapped to a
 * Postgres type via the current schema, and the file is read projected to
 * exactly those columns by field id. Rows are kept as Datum arrays in the
 * calling context; the per-file Parquet footer/metadata live in a scratch
 * context reset per file (the 4a memory rule).
 */
static List *
ice_read_eq_deletes(IceScanCtx *c, JsonbContainer *root, const char *path,
					int64 min_data_seq)
{
	List	   *out = NIL;
	ListCell   *lc;
	MemoryContext filectx;

	if (c->eqdel == NIL)
		return NIL;

	filectx = AllocSetContextCreate(CurrentMemoryContext,
									"pgcolumnar iceberg_scan eqdel",
									ALLOCSET_DEFAULT_SIZES);

	foreach(lc, c->eqdel)
	{
		IceEntry   *ed = (IceEntry *) lfirst(lc);
		IceEqDel   *E;
		TupleDesc	dtd;
		TupleTableSlot *wslot;
		TupleTableSlot *rslot;
		Tuplestorestate *ts;
		char	   *safe;
		MemoryContext old;
		int			i;

		/* never applicable: no data file is strictly older than this delete */
		if (ed->seq <= min_data_seq)
			continue;

		/* the spec requires equality_ids when content = 2 */
		if (ed->neq_ids == 0)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: equality delete file \"%s\" has no equality_ids",
							ed->file_path)));
		if (ed->file_format != NULL && strcmp(ed->file_format, "PARQUET") != 0)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("iceberg: equality-delete file \"%s\" has format %s; only PARQUET equality deletes are supported",
							ed->file_path, ed->file_format)));
		/* the manifest list must say which partition spec the delete was
		 * written under; without it the scope is undecidable, and defaulting
		 * to "unpartitioned" would silently globalize a scoped delete */
		if (!ed->has_spec_id)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: the manifest list carries no partition_spec_id for equality delete file \"%s\"",
							ed->file_path)));

		E = (IceEqDel *) palloc0(sizeof(IceEqDel));
		E->seq = ed->seq;
		E->spec_id = ed->spec_id;
		E->partitioned = ice_spec_is_partitioned(root, ed->spec_id, path);
		if (E->partitioned)
		{
			/* a partition-scoped delete applies where its (spec id, partition
			 * tuple) equals a data file's; we need a comparable tuple. A delete
			 * whose spec id or values match no data file simply applies to
			 * nothing -- the pass-2 filter yields that no-op, exactly as the
			 * spec requires (a partitioned delete never crosses spec ids), so
			 * no cross-spec refusal is needed or correct. */
			if (ed->npart_cells == 0 ||
				ice_part_incomparable(ed->part_cells, ed->npart_cells))
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("iceberg: partition-scoped equality delete file \"%s\" has a partition tuple this reader cannot compare",
								ed->file_path),
						 errdetail("A float, double, or otherwise uncomparable partition value is not supported.")));
			ice_copy_part_cells(ed->part_cells, ed->npart_cells,
								&E->part_cells, &E->npart_cells);
		}
		E->nids = ed->neq_ids;
		E->ids = (int32 *) palloc(E->nids * sizeof(int32));
		memcpy(E->ids, ed->eq_ids, E->nids * sizeof(int32));
		E->types = (Oid *) palloc(E->nids * sizeof(Oid));
		E->typlen = (int16 *) palloc(E->nids * sizeof(int16));
		E->typbyval = (bool *) palloc(E->nids * sizeof(bool));
		E->rows = NIL;

		old = MemoryContextSwitchTo(filectx);
		dtd = CreateTemplateTupleDesc(E->nids);
		for (i = 0; i < E->nids; i++)
		{
			const char *fname = NULL;

			E->types[i] = ice_eq_col_type(root, E->ids[i], path, &fname);
			get_typlenbyval(E->types[i], &E->typlen[i], &E->typbyval[i]);
			TupleDescInitEntry(dtd, i + 1, fname, E->types[i], -1, 0);
		}
#if PG_VERSION_NUM >= 190000
		/* PG19 asserts firstNonCachedOffsetAttr >= 0 on a manually built
		 * tupdesc; BlessTupleDesc does not compute it, TupleDescFinalize does */
		TupleDescFinalize(dtd);
#endif
		dtd = BlessTupleDesc(dtd);
		wslot = MakeSingleTupleTableSlot(dtd, &TTSOpsVirtual);
		rslot = MakeSingleTupleTableSlot(dtd, &TTSOpsMinimalTuple);

		safe = ice_open_path(c->recorded_root, c->actual_root,
							 ed->file_path, "equality-delete", c->mdpath);
		ts = tuplestore_begin_heap(false, false, work_mem);
		(void) PgColumnarReadParquetByFieldId(safe, dtd, (const int *) E->ids,
											  E->nids, ts, wslot, NULL, 0, c->cfg);
		while (tuplestore_gettupleslot(ts, true, false, rslot))
		{
			IceEqRow   *row;

			/* keep the row in the outer context, not the per-file scratch */
			MemoryContextSwitchTo(old);
			row = (IceEqRow *) palloc(sizeof(IceEqRow));
			row->vals = (Datum *) palloc0(E->nids * sizeof(Datum));
			row->nulls = (bool *) palloc(E->nids * sizeof(bool));
			for (i = 0; i < E->nids; i++)
			{
				Datum		d = slot_getattr(rslot, i + 1, &row->nulls[i]);

				if (!row->nulls[i])
					row->vals[i] = datumCopy(d, E->typbyval[i], E->typlen[i]);
			}
			E->rows = lappend(E->rows, row);
			MemoryContextSwitchTo(filectx);
		}
		tuplestore_end(ts);
		ExecDropSingleTupleTableSlot(wslot);
		ExecDropSingleTupleTableSlot(rslot);
		MemoryContextSwitchTo(old);
		MemoryContextReset(filectx);
		out = lappend(out, E);
	}
	MemoryContextDelete(filectx);
	return out;
}

/* ascending qsort comparator over uint64 file ordinals */
static int
ice_cmp_u64(const void *a, const void *b)
{
	uint64		x = *(const uint64 *) a;
	uint64		y = *(const uint64 *) b;

	return (x < y) ? -1 : (x > y) ? 1 : 0;
}

/*
 * Does a data row (dv/dn, indexed by union column position) match ANY row of
 * equality-delete file E? A delete row matches when ALL of E's equality columns
 * are equal; a null delete value matches a null data value (IS NULL semantics,
 * per the spec), and only a null. mape maps E's column index to the union
 * position the probe read that column into.
 */
static bool
ice_eq_file_matches(IceEqDel *E, const int *mape, const Datum *dv,
					const bool *dn, TypeCacheEntry **utc)
{
	ListCell   *lc;

	foreach(lc, E->rows)
	{
		IceEqRow   *row = (IceEqRow *) lfirst(lc);
		bool		all = true;
		int			i;

		for (i = 0; i < E->nids; i++)
		{
			int			k = mape[i];

			if (dn[k] != row->nulls[i])
			{
				all = false;
				break;
			}
			if (!dn[k] &&
				!DatumGetBool(FunctionCall2Coll(&utc[k]->eq_opr_finfo,
												DEFAULT_COLLATION_OID,
												dv[k], row->vals[i])))
			{
				all = false;
				break;
			}
		}
		if (all)
			return true;
	}
	return false;
}

/*
 * The equality-delete PROBE pass over one data file: read only the union of the
 * eligible delete files' equality columns (projected by field id, so this is a
 * whole-file subset read whose tuplestore row index IS the file ordinal), test
 * each row against each eligible delete file, and append matching ordinals to
 * the caller's skip set (skip/nskip/cap, grown in the caller's memory
 * context). Returns the file's total row count, which the caller cross-checks
 * against the second (full) read. Scratch allocations live in c->filectx,
 * reset before returning.
 */
static int64
ice_eq_probe(IceScanCtx *c, IceEntry *d, List *elig,
			 uint64 **skip, int *nskip, int *cap)
{
	int			ne = list_length(elig);
	int			tot = 0;
	int			nuni = 0;
	int		   *uids;
	Oid		   *utypes;
	int		  **map;
	TypeCacheEntry **utc;
	TupleDesc	ptd;
	TupleTableSlot *wslot;
	TupleTableSlot *rslot;
	Tuplestorestate *ts;
	Datum	   *dv;
	bool	   *dn;
	char	   *safe;
	MemoryContext old;
	ListCell   *lc;
	int			e;
	int			k;
	uint64		ord = 0;

	/* everything below except the skip-set growth is per-data-file scratch,
	 * reclaimed by the reset at the end */
	old = MemoryContextSwitchTo(c->filectx);

	/* the union of the eligible files' equality columns, deduplicated, with a
	 * per-file map from its column index to the union position */
	foreach(lc, elig)
		tot += ((IceEqDel *) lfirst(lc))->nids;
	uids = (int *) palloc(tot * sizeof(int));
	utypes = (Oid *) palloc(tot * sizeof(Oid));
	map = (int **) palloc(ne * sizeof(int *));
	e = 0;
	foreach(lc, elig)
	{
		IceEqDel   *E = (IceEqDel *) lfirst(lc);
		int			i;

		map[e] = (int *) palloc(E->nids * sizeof(int));
		for (i = 0; i < E->nids; i++)
		{
			for (k = 0; k < nuni; k++)
				if (uids[k] == E->ids[i])
					break;
			if (k == nuni)
			{
				uids[nuni] = E->ids[i];
				utypes[nuni] = E->types[i];
				nuni++;
			}
			/* both resolved via the same current-schema lookup */
			Assert(utypes[k] == E->types[i]);
			map[e][i] = k;
		}
		e++;
	}
	utc = (TypeCacheEntry **) palloc(nuni * sizeof(TypeCacheEntry *));
	for (k = 0; k < nuni; k++)
	{
		utc[k] = lookup_type_cache(utypes[k], TYPECACHE_EQ_OPR_FINFO);
		if (!OidIsValid(utc[k]->eq_opr_finfo.fn_oid))
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("iceberg: equality delete column with field id %d has no equality operator",
							uids[k])));
	}

	ptd = CreateTemplateTupleDesc(nuni);
	for (k = 0; k < nuni; k++)
		TupleDescInitEntry(ptd, k + 1, psprintf("eq%d", uids[k]),
						   utypes[k], -1, 0);
#if PG_VERSION_NUM >= 190000
	TupleDescFinalize(ptd);
#endif
	ptd = BlessTupleDesc(ptd);
	wslot = MakeSingleTupleTableSlot(ptd, &TTSOpsVirtual);
	rslot = MakeSingleTupleTableSlot(ptd, &TTSOpsMinimalTuple);
	safe = ice_open_path(c->recorded_root, c->actual_root, d->file_path,
						 "data-file", c->mdpath);
	ts = tuplestore_begin_heap(false, false, work_mem);
	(void) PgColumnarReadParquetByFieldId(safe, ptd, uids, nuni, ts, wslot,
										  NULL, 0, c->cfg);
	dv = (Datum *) palloc(nuni * sizeof(Datum));
	dn = (bool *) palloc(nuni * sizeof(bool));
	while (tuplestore_gettupleslot(ts, true, false, rslot))
	{
		bool		matched = false;

		CHECK_FOR_INTERRUPTS();
		for (k = 0; k < nuni; k++)
			dv[k] = slot_getattr(rslot, k + 1, &dn[k]);
		e = 0;
		foreach(lc, elig)
		{
			if (ice_eq_file_matches((IceEqDel *) lfirst(lc), map[e], dv, dn, utc))
			{
				matched = true;
				break;
			}
			e++;
		}
		if (matched)
		{
			/* grow the skip set in the caller's context, not the scratch */
			MemoryContextSwitchTo(old);
			if (*nskip == *cap)
			{
				*cap = *cap ? *cap * 2 : 16;
				*skip = (*skip == NULL)
					? (uint64 *) palloc(*cap * sizeof(uint64))
					: (uint64 *) repalloc(*skip, *cap * sizeof(uint64));
			}
			(*skip)[(*nskip)++] = ord;
			MemoryContextSwitchTo(c->filectx);
		}
		ord++;
	}
	tuplestore_end(ts);
	ExecDropSingleTupleTableSlot(wslot);
	ExecDropSingleTupleTableSlot(rslot);
	MemoryContextSwitchTo(old);
	MemoryContextReset(c->filectx);
	return (int64) ord;
}

/*
 * The body of iceberg_scan, factored (#388 phase 7) so the REST catalog client
 * reads a table it resolved by name through the very same path. `path` is a
 * metadata.json location (local or remote), `tupdesc` the caller's column
 * definition list; the rows land in a materialize-mode tuplestore on `rsinfo`.
 */
int64
PgColumnarIcebergScanCore(const char *path, TupleDesc tupdesc,
						  const PgColumnarObjStoreConfig *cfg,
						  Tuplestorestate *tupstore,
						  PgColumnarIceFileFilter filter, void *filterarg,
						  const bool *needTop)
{
	IceScanCtx	ctx;
	char	   *json;
	Jsonb	   *jb;
	int		   *field_ids;
	int			nfield;
	List	   *posdels;
	List	   *dvdels;
	List	   *eqdels;
	ListCell   *lc;
	int64		pruned = 0;

	ctx.cfg = cfg;				/* vended storage creds for every file, or NULL */
	ctx.needTop = needTop;

	/* map output column names -> field ids via the table's current schema */
	json = ice_slurp_text(path, cfg);
	jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_in, CStringGetDatum(json)));
	field_ids = ice_field_ids_for_columns(&jb->root, tupdesc, path, &nfield);
	ice_name_mapping(&jb->root, path, &ctx.nm_names, &ctx.nm_ids, &ctx.nm_count);

	ctx.tupstore = tupstore;
	ctx.tupdesc = tupdesc;
	ctx.slot = MakeSingleTupleTableSlot(tupdesc, &TTSOpsVirtual);
	ctx.field_ids = field_ids;
	ctx.nfield = nfield;
	ctx.filectx = AllocSetContextCreate(CurrentMemoryContext,
										"pgcolumnar iceberg_scan file",
										ALLOCSET_DEFAULT_SIZES);
	ctx.recorded_root = NULL;
	ctx.actual_root = NULL;
	ctx.mdpath = NULL;
	ctx.data = NIL;
	ctx.posdel = NIL;
	ctx.eqdel = NIL;

	/* pass 1: collect the snapshot's data and delete entries (reuse the
	 * metadata already parsed for the schema, so the file is read once) */
	ice_walk_data_files(path, &jb->root, true, ice_scan_cb, &ctx, cfg);

	/* read the position-delete files once into (dpath, pos, seq) rows, and the
	 * applicable equality-delete files once into per-file delete-row sets
	 * (both kinds are snapshot-global; equality deletes are validated against
	 * the metadata -- equality_ids present, a present partition_spec_id naming
	 * an unpartitioned spec, mappable column types -- while a delete no data
	 * file is strictly older than is skipped as having no effect) */
	posdels = ice_read_pos_deletes(&ctx);
	{
		int64		fmtver = 0;
		int64		min_data_seq = PG_INT64_MAX;

		/* the format-version VALUE gates v3 deletion vectors; its presence
		 * was already required when the snapshot was resolved */
		if (!ice_num_int64(ice_field(&jb->root, "format-version"), &fmtver))
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: \"%s\" has a non-numeric format-version",
							path)));
		dvdels = ice_read_dvs(&ctx, fmtver);
		foreach(lc, ctx.data)
			min_data_seq = Min(min_data_seq, ((IceEntry *) lfirst(lc))->seq);
		eqdels = ice_read_eq_deletes(&ctx, &jb->root, path, min_data_seq);
	}

	/* pass 2: read each data file, skipping the positions its deletes drop */
	foreach(lc, ctx.data)
	{
		IceEntry   *d = (IceEntry *) lfirst(lc);
		const char *dp = ice_strip_scheme(d->file_path);
		uint64	   *skip = NULL;
		int			nskip = 0;
		int			cap = 0;
		List	   *elig = NIL;
		int64		probe_rows = -1;
		int64		returned;
		char	   *safe;
		MemoryContext old;
		ListCell   *lc2;

		/* pruning: a caller-supplied filter may skip a whole data file whose
		 * partition value or column bounds cannot match the scan's predicate
		 * (the FDW). Skipping a file never changes correctness -- a false-negative
		 * just reads a file that would have returned no rows -- so the filter is
		 * only ever an optimization, never a source of wrong results. */
		if (filter != NULL)
		{
			PgColumnarIceFileMeta meta;

			meta.cells = d->part_cells;
			meta.ncells = d->npart_cells;
			meta.spec_id = d->spec_id;
			meta.lower = d->lower_bounds;
			meta.nlower = d->nlower;
			meta.upper = d->upper_bounds;
			meta.nupper = d->nupper;
			if (filter(filterarg, &meta))
			{
				pruned++;
				continue;
			}
		}

		if (d->file_format != NULL && strcmp(d->file_format, "PARQUET") != 0)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("iceberg: data file \"%s\" has format %s; only PARQUET is supported",
							d->file_path, d->file_format)));

		/* deletion vectors first: a DV applies under the same <= sequence rule
		 * as position-delete files (data_seq <= dv_seq, so the comparison is
		 * >=), and an applicable DV SUPERSEDES position-delete files for its
		 * data file -- the spec's scope rule applies a position-delete file
		 * only when no DV must be applied, because a writer adding a DV must
		 * fold all existing position deletes into it. */
		{
			bool		dv_applies = false;

			foreach(lc2, dvdels)
			{
				IceDvDel   *dv = (IceDvDel *) lfirst(lc2);

				if (dv->seq >= d->seq &&
					strcmp(ice_strip_scheme(dv->dpath), dp) == 0)
				{
					dv_applies = true;
					if (dv->npos > 0)
					{
						while (nskip + dv->npos > cap)
							cap = cap ? cap * 2 : 16;
						skip = (skip == NULL)
							? (uint64 *) palloc(cap * sizeof(uint64))
							: (uint64 *) repalloc(skip, cap * sizeof(uint64));
						memcpy(skip + nskip, dv->pos,
							   dv->npos * sizeof(uint64));
						nskip += (int) dv->npos;
					}
				}
			}

			/* gather the ordinals to drop: position deletes that target this
			 * file and whose data sequence number is >= the data file's. The
			 * spec rule is data_seq <= delete_seq (a position delete applies
			 * to data written in the same commit or earlier), so the
			 * comparison is >=, not > -- an equal sequence number (a
			 * single-commit upsert) still applies. Equality deletes use
			 * strict < instead. Skipped entirely when a DV applies. */
			if (!dv_applies)
				foreach(lc2, posdels)
				{
					IcePosDel  *pd = (IcePosDel *) lfirst(lc2);

					if (pd->seq >= d->seq &&
						strcmp(ice_strip_scheme(pd->dpath), dp) == 0)
					{
						if (nskip == cap)
						{
							cap = cap ? cap * 2 : 16;
							skip = (skip == NULL)
								? (uint64 *) palloc(cap * sizeof(uint64))
								: (uint64 *) repalloc(skip, cap * sizeof(uint64));
						}
						skip[nskip++] = (uint64) pd->pos;
					}
				}
		}

		/* equality deletes: STRICTLY newer than the data file only (spec rule
		 * data_seq < delete_seq -- the opposite boundary from position deletes;
		 * an equality delete never touches data from its own commit). Eligible
		 * files drive a probe pass over this data file's equality columns that
		 * appends the matching ordinals to the same skip set. */
		foreach(lc2, eqdels)
		{
			IceEqDel   *E = (IceEqDel *) lfirst(lc2);

			if (E->seq <= d->seq)
				continue;
			/* an unpartitioned-spec equality delete is global (4b); a
			 * partition-scoped one applies only to a data file of the same
			 * spec id whose partition tuple equals the delete's */
			if (E->partitioned)
			{
				/* the data file's spec id decides whether a partition-scoped
				 * delete applies to it; a missing one (corrupt metadata --
				 * partition_spec_id is required) would default to 0 and could
				 * silently exclude a delete that should apply (under-delete),
				 * the mirror of the delete-side check that already refuses. */
				if (!d->has_spec_id)
					ereport(ERROR,
							(errcode(ERRCODE_DATA_CORRUPTED),
							 errmsg("iceberg: data file \"%s\" carries no partition_spec_id, which a partition-scoped equality delete's scope depends on",
									d->file_path)));
				if (E->spec_id != d->spec_id)
					continue;	/* a different partition spec */
				if (ice_part_incomparable(d->part_cells, d->npart_cells))
					ereport(ERROR,
							(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
							 errmsg("iceberg: data file \"%s\" has a partition tuple this reader cannot compare against a partition-scoped equality delete",
									d->file_path)));
				if (!ice_part_cells_equal(E->part_cells, E->npart_cells,
										  d->part_cells, d->npart_cells))
					continue;	/* a different partition value */
			}
			elig = lappend(elig, E);
		}
		if (elig != NIL)
			probe_rows = ice_eq_probe(&ctx, d, elig, &skip, &nskip, &cap);

		if (nskip > 1)			/* the reader wants the set sorted and unique */
		{
			int			w = 1;
			int			k;

			qsort(skip, nskip, sizeof(uint64), ice_cmp_u64);
			for (k = 1; k < nskip; k++)
				if (skip[k] != skip[w - 1])
					skip[w++] = skip[k];
			nskip = w;
		}

		/* resolve and read inside the per-file context so the opened path string
		 * and the reader's footer/metadata are reclaimed by the reset, not left
		 * to accumulate one-per-data-file across the scan */
		old = MemoryContextSwitchTo(ctx.filectx);
		safe = ice_open_path(ctx.recorded_root, ctx.actual_root, d->file_path,
							 "data-file", ctx.mdpath);
		returned = PgColumnarReadParquetByFieldIdNM(safe, ctx.tupdesc, ctx.field_ids,
													ctx.nfield,
													(const char *const *) ctx.nm_names,
													ctx.nm_ids, ctx.nm_count,
													ctx.tupstore, ctx.slot,
													skip, nskip, ctx.needTop, ctx.cfg);
		MemoryContextSwitchTo(old);
		MemoryContextReset(ctx.filectx);

		/* the probe and the full read walked the same file: the full read must
		 * return exactly the probe's row count minus the in-range skips, or the
		 * two passes disagreed on ordinals */
		if (probe_rows >= 0)
		{
			int64		expect = probe_rows;
			int			k;

			for (k = 0; k < nskip; k++)
				if (skip[k] < (uint64) probe_rows)
					expect--;
			if (returned != expect)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: data file \"%s\" returned " INT64_FORMAT " rows where " INT64_FORMAT " were expected after deletes",
								d->file_path, returned, expect)));
		}
		if (skip != NULL)
			pfree(skip);
		list_free(elig);
	}

	MemoryContextDelete(ctx.filectx);
	ExecDropSingleTupleTableSlot(ctx.slot);
	return pruned;
}

/*
 * The body of iceberg_scan: read the whole table (no pruning) into a
 * materialize-mode tuplestore on rsinfo. Thin wrapper over the filtered core.
 */
void
PgColumnarIcebergScanInto(const char *path, TupleDesc tupdesc,
						  ReturnSetInfo *rsinfo,
						  const PgColumnarObjStoreConfig *cfg)
{
	MemoryContext oldcxt;
	TupleDesc	outdesc;
	Tuplestorestate *tupstore;

	oldcxt = MemoryContextSwitchTo(rsinfo->econtext->ecxt_per_query_memory);
	outdesc = CreateTupleDescCopy(tupdesc);
	tupstore = tuplestore_begin_heap(true, false, work_mem);
	rsinfo->returnMode = SFRM_Materialize;
	rsinfo->setResult = tupstore;
	rsinfo->setDesc = outdesc;
	MemoryContextSwitchTo(oldcxt);

	(void) PgColumnarIcebergScanCore(path, outdesc, cfg, tupstore, NULL, NULL, NULL);
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_scan);

/*
 * pgcolumnar.iceberg_scan(metadata_path text) returns setof record
 *
 * Read an Apache Iceberg table at its current snapshot. The caller supplies a
 * column definition list; each output column name is resolved to a field id via
 * the table's current schema, and every live data file is read projected by
 * those ids -- so a data file written before a column rename still reads, since
 * Iceberg selects columns by id, not name or position.
 *
 * Row-level deletes are applied, each kind under its own spec rule:
 *
 * - Position deletes drop the listed row ordinals of the data file they name,
 *   when the data file's data sequence number is less than OR EQUAL TO the
 *   delete's (a position delete applies to data written in the same commit or
 *   earlier, so the same-sequence upsert case counts).
 * - v3 deletion vectors (Puffin files) are position deletes in bitmap form:
 *   the same <= sequence rule, scoped to their referenced_data_file. An
 *   applicable DV SUPERSEDES position-delete files for its data file (the
 *   spec's scope rule; the writer folded their deletes into it). At most one
 *   DV may reference a data file; deletion vectors are refused below
 *   format-version 3.
 * - Equality deletes drop every data row whose equality_ids column values match
 *   a delete row (all columns equal; null matches null), when the data file's
 *   sequence number is STRICTLY LESS THAN the delete's (an equality delete
 *   never touches same-commit data). An unpartitioned-spec equality delete is
 *   global; a partition-scoped one applies only to a data file whose partition
 *   (spec id and tuple) equals the delete's, comparing the stored transformed
 *   partition tuples (phase 5). A partition-scoped delete matching no data
 *   file's spec, or carrying an uncomparable partition value, is refused.
 *
 * Only PARQUET files are read. Superuser / pg_read_server_files;
 * materialize-mode SRF.
 */
Datum
pgcolumnar_iceberg_scan(PG_FUNCTION_ARGS)
{
	char	   *path = text_to_cstring(PG_GETARG_TEXT_PP(0));
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	TupleDesc	tupdesc;

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a server file")));
	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo) ||
		(rsinfo->allowedModes & SFRM_Materialize) == 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in a context that cannot accept a set")));
	if (get_call_result_type(fcinfo, NULL, &tupdesc) != TYPEFUNC_COMPOSITE)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.iceberg_scan requires a column definition list"),
				 errhint("Call it as SELECT * FROM pgcolumnar.iceberg_scan(path) AS t(col1 type1, ...).")));

	PgColumnarIcebergScanInto(path, tupdesc, rsinfo, NULL);
	return (Datum) 0;
}

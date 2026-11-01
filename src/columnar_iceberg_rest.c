/*-------------------------------------------------------------------------
 * columnar_iceberg_rest.c
 *		Read-only Apache Iceberg REST Catalog client (#388 phase 7).
 *
 * Resolves a table NAMED by a catalog (catalog URI + namespace + table) into
 * its current metadata location, which the reader (already remote-capable since
 * phase 6) then reads. This file owns only the REST protocol and its security
 * envelope; the HTTP(S) transport, TLS verification, the endpoint allow-list,
 * and the link-local/instance-metadata refusal all live in the object-store
 * module, reached through the ABI v5 http_request entry -- so no second TLS
 * stack enters the preloaded postmaster.
 *
 * Authentication is a static bearer token read from the SERVER PROCESS
 * ENVIRONMENT (PGCOLUMNAR_ICEBERG_REST_TOKEN), never a SQL argument: a token in
 * a function argument would land in pg_stat_activity and the statement log. The
 * per-catalog FOREIGN SERVER + USER MAPPING credential model, vended storage
 * credentials, OAuth2 token exchange, and the warehouse server option are
 * implemented (issue #656); secrets live only on the USER MAPPING.
 *
 * See design/ISSUE_388_PHASE7_REST_CATALOG.md.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/reloptions.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_foreign_server.h"
#include "catalog/pg_type_d.h"
#include "catalog/pg_user_mapping.h"
#include "commands/defrem.h"
#include "common/hashfn.h"
#include "fmgr.h"
#include "foreign/foreign.h"
#include "lib/stringinfo.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "utils/acl.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/hsearch.h"
#include "utils/jsonb.h"
#include "utils/syscache.h"
#include "utils/tuplestore.h"

#include "funcapi.h"
#include "columnar_iceberg.h"
#include "columnar_objstore.h"
#include "columnar_iceberg_rest.h"

/*
 * Set up a materialize-mode SRF returning a single text column, and return its
 * tuplestore. Used by the listing functions, which return SETOF text (a scalar
 * return type InitMaterializedSRF does not build a tupdesc for).
 */
static Tuplestorestate *
rest_text_srf_setup(ReturnSetInfo *rsinfo, TupleDesc *tdout)
{
	Tuplestorestate *ts;
	TupleDesc	td;
	MemoryContext oldcxt;

	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo) ||
		(rsinfo->allowedModes & SFRM_Materialize) == 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in a context that cannot accept a set")));

	oldcxt = MemoryContextSwitchTo(rsinfo->econtext->ecxt_per_query_memory);
	td = CreateTemplateTupleDesc(1);
	TupleDescInitEntry(td, 1, "name", TEXTOID, -1, 0);
	ts = tuplestore_begin_heap(false, false, work_mem);
	rsinfo->returnMode = SFRM_Materialize;
	rsinfo->setResult = ts;
	rsinfo->setDesc = td;
	MemoryContextSwitchTo(oldcxt);
	*tdout = td;
	return ts;
}

/*
 * A catalog reply is small (a metadata-location plus, for now, an ignored inline
 * metadata document). 64 MiB matches the metadata-slurp cap and refuses a
 * hostile or runaway catalog before it can exhaust backend memory.
 */
#define REST_MAX_RESPONSE	((int64) 64 * 1024 * 1024)

/* The environment variable holding the static bearer token, or none. */
#define REST_TOKEN_ENV		"PGCOLUMNAR_ICEBERG_REST_TOKEN"

/* Resolve the object-store module, or raise: the REST client needs its HTTP. */
static const PgColumnarObjStoreApi *
rest_objstore(void)
{
	const PgColumnarObjStoreApi *api = PgColumnarObjStoreGet();

	if (api == NULL || api->http_request == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("iceberg: the object-store module is required for a REST catalog"),
				 errhint("Install the pgcolumnar_objstore module.")));
	return api;
}

/*
 * Resolve a FOREIGN SERVER (pgcolumnar_iceberg_catalog) into its catalog_uri and
 * the current role's bearer token. The role must hold USAGE on the server, as
 * dblink requires before it uses a named server's mapping (#1819): without the
 * check, REVOKE USAGE did not stop a role using its own mapping, nor a PUBLIC
 * one. The token comes from the role's USER MAPPING (or the PUBLIC mapping),
 * which lives in pg_user_mapping and is not world-readable. When the mapping
 * carries no token, fall back to the environment token only if ambient use is
 * allowed (a superuser, or a mapping that set credentials_required 'false');
 * otherwise refuse (28000). *out_token may be NULL for an anonymous catalog.
 */
static char *rest_oauth_token(const char *catalog_uri, const char *token_uri,
							  const char *client_id, const char *client_secret,
							  const char *scope);

static void
rest_resolve_server(const char *name, char **out_uri, char **out_token,
					char **out_warehouse)
{
	ForeignServer *srv = GetForeignServerByName(name, false);
	ListCell   *lc;
	char	   *uri = NULL;
	char	   *warehouse = NULL;
	char	   *token = NULL;
	char	   *oauthId = NULL;
	char	   *oauthSecret = NULL;
	char	   *oauthScope = NULL;
	char	   *oauthUri = NULL;
	bool		credRequired = true;
	HeapTuple	tp;
	AclResult	aclresult;

	/* before any mapping is read or any request is made */
#if PG_VERSION_NUM >= 160000
	aclresult = object_aclcheck(ForeignServerRelationId, srv->serverid,
								GetUserId(), ACL_USAGE);
#else
	aclresult = pg_foreign_server_aclcheck(srv->serverid, GetUserId(), ACL_USAGE);
#endif
	if (aclresult != ACLCHECK_OK)
		aclcheck_error(aclresult, OBJECT_FOREIGN_SERVER, srv->servername);

	foreach(lc, srv->options)
	{
		DefElem    *d = (DefElem *) lfirst(lc);

		if (strcmp(d->defname, "catalog_uri") == 0)
			uri = defGetString(d);
		else if (strcmp(d->defname, "warehouse") == 0)
			warehouse = defGetString(d);
	}
	if (uri == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_FDW_OPTION_NAME_NOT_FOUND),
				 errmsg("iceberg: server \"%s\" has no \"catalog_uri\" option", name)));

	/* the current role's mapping, else the PUBLIC (InvalidOid) mapping */
	tp = SearchSysCache2(USERMAPPINGUSERSERVER,
						 ObjectIdGetDatum(GetUserId()),
						 ObjectIdGetDatum(srv->serverid));
	if (!HeapTupleIsValid(tp))
		tp = SearchSysCache2(USERMAPPINGUSERSERVER,
							 ObjectIdGetDatum(InvalidOid),
							 ObjectIdGetDatum(srv->serverid));
	if (HeapTupleIsValid(tp))
	{
		Datum		datum;
		bool		isnull;

		datum = SysCacheGetAttr(USERMAPPINGUSERSERVER, tp,
								Anum_pg_user_mapping_umoptions, &isnull);
		if (!isnull)
		{
			List	   *umopts = untransformRelOptions(datum);
			ListCell   *l2;

			foreach(l2, umopts)
			{
				DefElem    *d = (DefElem *) lfirst(l2);

				if (strcmp(d->defname, "token") == 0)
					token = pstrdup(defGetString(d));
				else if (strcmp(d->defname, "oauth_client_id") == 0)
					oauthId = pstrdup(defGetString(d));
				else if (strcmp(d->defname, "oauth_client_secret") == 0)
					oauthSecret = pstrdup(defGetString(d));
				else if (strcmp(d->defname, "oauth_scope") == 0)
					oauthScope = pstrdup(defGetString(d));
				else if (strcmp(d->defname, "oauth_token_uri") == 0)
					oauthUri = pstrdup(defGetString(d));
				else if (strcmp(d->defname, "credentials_required") == 0)
					credRequired = defGetBoolean(d);
			}
		}
		ReleaseSysCache(tp);
	}

	/* OAuth2 client-credentials: a half credential (one of id/secret) is refused
	 * before any request, whether or not a static token is also set (#1794); a
	 * complete pair mints the bearer when no static token. */
	if ((oauthId == NULL) != (oauthSecret == NULL))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
				 errmsg("iceberg: incomplete OAuth2 credential for REST catalog server \"%s\"",
						name),
				 errhint("Set both oauth_client_id and oauth_client_secret on the "
						 "USER MAPPING.")));
	if (token == NULL && oauthId != NULL)
		token = rest_oauth_token(uri, oauthUri, oauthId, oauthSecret, oauthScope);

	if (token == NULL)
	{
		bool		ambientOk = superuser() || !credRequired;

		if (ambientOk)
		{
			const char *env = getenv(REST_TOKEN_ENV);

			if (env != NULL && env[0] != '\0')
				token = pstrdup(env);
		}
		else
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
					 errmsg("iceberg: no bearer token for REST catalog server \"%s\"",
							name),
					 errhint("Create a USER MAPPING with a token option for this role, "
							 "or set credentials_required 'false' on it.")));
	}

	*out_uri = pstrdup(uri);
	*out_token = token;
	if (out_warehouse != NULL)
		*out_warehouse = (warehouse != NULL) ? pstrdup(warehouse) : NULL;
}

/*
 * Resolve the first function argument, which is EITHER a catalog URI (http(s)://,
 * with the token from the environment, the phase-7 form) OR a FOREIGN SERVER name
 * (with the URI and token from the catalog). A server name can never look like a
 * URL, so the two are unambiguous.
 */
static void
rest_resolve(const char *arg0, char **out_uri, char **out_token,
			 char **out_warehouse)
{
	if (pg_strncasecmp(arg0, "http://", 7) == 0 ||
		pg_strncasecmp(arg0, "https://", 8) == 0)
	{
		const char *env = getenv(REST_TOKEN_ENV);

		*out_uri = pstrdup(arg0);
		*out_token = (env != NULL && env[0] != '\0') ? pstrdup(env) : NULL;
		if (out_warehouse != NULL)
			*out_warehouse = NULL;	/* a bare URI carries no warehouse */
	}
	else
		rest_resolve_server(arg0, out_uri, out_token, out_warehouse);
}

/* Append `s` percent-encoded (RFC 3986 unreserved kept), for one path segment. */
static void
rest_pct_encode(StringInfo out, const char *s)
{
	static const char hex[] = "0123456789ABCDEF";
	const unsigned char *p;

	for (p = (const unsigned char *) s; *p != '\0'; p++)
	{
		unsigned char c = *p;

		if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
			(c >= '0' && c <= '9') ||
			c == '-' || c == '.' || c == '_' || c == '~')
			appendStringInfoChar(out, (char) c);
		else
		{
			appendStringInfoChar(out, '%');
			appendStringInfoChar(out, hex[c >> 4]);
			appendStringInfoChar(out, hex[c & 0x0f]);
		}
	}
}

/*
 * What GET /v1/config tells a client about the catalog's paths (#1793).
 *   base  "/v1/", or "/v1/<prefix>/" when the catalog has a prefix. Every
 *         resource path but /v1/config starts with it.
 *   sep   the namespace separator, percent-encoded byte by byte ("%1F" by
 *         default, "%2E" on the Apache Iceberg 1.12 REST catalog).
 */
typedef struct RestCatalogConfig
{
	char	   *base;
	char	   *sep;
	char	   *rawsep;			/* sep decoded: the bytes a level must not hold */
} RestCatalogConfig;

/* The REST spec's default namespace separator, the unit separator 0x1F. */
#define REST_DEFAULT_SEPARATOR	"%1F"

/*
 * The byte length of the character at p in the database encoding, never past
 * the string's end. A level is text in the database encoding: in a LATIN1
 * database "é" is the one byte E9, so a UTF-8 length would step over the next
 * two bytes (#1865).
 */
static int
rest_char_len(const char *p)
{
	int			len = pg_mblen(p);	/* every major has it; clamped below */
	int			n = 0;

	while (n < len && p[n] != '\0')
		n++;
	return n;
}

/*
 * Whether `level` holds the separator `rawsep`. The separator matches only as
 * whole characters of the database encoding: it must start and end on a
 * character boundary. In a UTF-8 database, a one-byte separator at or above
 * 0x80 thus never matches part of a multibyte character (#1865).
 */
static bool
rest_level_holds_separator(const char *level, const char *rawsep)
{
	int			seplen = (int) strlen(rawsep);
	const char *p;

	for (p = level; *p != '\0'; p += rest_char_len(p))
	{
		int			covered = 0;

		if (strncmp(p, rawsep, seplen) != 0)
			continue;
		while (covered < seplen)
			covered += rest_char_len(p + covered);
		if (covered == seplen)
			return true;
	}
	return false;
}

/*
 * The first level in `levels` that holds the catalog's separator, or NULL. The
 * catalog splits a namespace on its separator, so it would read such a level
 * as two levels (#1865). The Apache Iceberg REST catalog's separator is ".".
 */
static const char *
rest_unnameable_level(List *levels, const RestCatalogConfig *cc)
{
	ListCell   *lc;

	foreach(lc, levels)
	{
		if (rest_level_holds_separator((const char *) lfirst(lc), cc->rawsep))
			return (const char *) lfirst(lc);
	}
	return NULL;
}

/*
 * A level as an error message shows it: each control byte (0x1F is the default
 * separator) becomes \xNN, so it never reaches the log or the client raw.
 */
static char *
rest_level_for_message(const char *level)
{
	StringInfoData out;
	const char *p;

	initStringInfo(&out);
	for (p = level; *p != '\0'; p++)
	{
		unsigned char c = (unsigned char) *p;

		if (c < 0x20 || c == 0x7F)
			appendStringInfo(&out, "\\x%02X", c);
		else
			appendStringInfoChar(&out, (char) c);
	}
	return out.data;
}

/*
 * Append a namespace, given as a List of level strings, to a resource path. The
 * levels are joined with the catalog's separator (already encoded). Each level
 * is percent-encoded, so a level bearing '/', '.', or a control byte cannot
 * alter the request path. A level that holds the separator is refused: the
 * catalog would read it as a different namespace (#1865).
 */
static void
rest_append_levels(StringInfo out, List *levels, const RestCatalogConfig *cc)
{
	ListCell   *lc;
	const char *bad = rest_unnameable_level(levels, cc);

	if (bad != NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: the namespace level \"%s\" contains the catalog's namespace separator",
						rest_level_for_message(bad)),
				 errdetail("The catalog joins namespace levels with %s, so it would read this level as more than one.",
						   cc->sep)));
	foreach(lc, levels)
	{
		if (lc != list_head(levels))
			appendStringInfoString(out, cc->sep);
		rest_pct_encode(out, (const char *) lfirst(lc));
	}
}

/*
 * Refuse an empty namespace level before any request names it (#1891). The
 * catalog would otherwise be asked about the empty level and answer 404, so
 * the caller saw "no such namespace" instead of the wrong argument.
 */
static void
rest_check_no_empty_level(List *levels)
{
	ListCell   *lc;
	int			i = 0;

	foreach(lc, levels)
	{
		i++;
		if (((const char *) lfirst(lc))[0] == '\0')
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("iceberg: namespace level %d of %d is empty",
							i, list_length(levels)),
					 errdetail("Every namespace level needs at least one character. "
							   "In the text form, a leading, trailing or doubled dot makes an empty level.")));
	}
}

/* Split a dot-separated namespace, as a caller gives it, into its levels. */
static List *
rest_split_namespace(const char *ns)
{
	List	   *levels = NIL;
	const char *start = ns;
	const char *p;

	for (p = ns;; p++)
	{
		if (*p == '.' || *p == '\0')
		{
			levels = lappend(levels, pnstrdup(start, p - start));
			if (*p == '\0')
				break;
			start = p + 1;
		}
	}
	rest_check_no_empty_level(levels);
	return levels;
}

/*
 * The levels of a namespace given as a text[] (#1865): each element is one
 * level, as given, so a level may hold a dot. The array must have one
 * dimension, at least one element, no NULL element and no empty element.
 */
static List *
rest_array_levels(ArrayType *arr)
{
	Datum	   *elems;
	bool	   *nulls;
	int			n;
	int			i;
	List	   *levels = NIL;

	if (ARR_NDIM(arr) > 1)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: a namespace array must have one dimension, not %d",
						ARR_NDIM(arr))));
	deconstruct_array(arr, TEXTOID, -1, false, TYPALIGN_INT, &elems, &nulls, &n);
	if (n == 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: a namespace array must hold at least one level")));
	for (i = 0; i < n; i++)
	{
		if (nulls[i])
			ereport(ERROR,
					(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
					 errmsg("iceberg: a namespace level must not be NULL"),
					 errdetail("Element %d of the namespace array is NULL.", i + 1)));
		levels = lappend(levels, TextDatumGetCString(elems[i]));
	}
	rest_check_no_empty_level(levels);
	return levels;
}

/*
 * The namespace argument of a REST function: a dot-separated text, split on
 * every dot, or a text[] of levels (#1865).
 */
static List *
rest_namespace_arg(FunctionCallInfo fcinfo, int argno, bool is_array)
{
	if (is_array)
		return rest_array_levels(PG_GETARG_ARRAYTYPE_P(argno));
	return rest_split_namespace(text_to_cstring(PG_GETARG_TEXT_PP(argno)));
}

static int
rest_hexval(char c)
{
	if (c >= '0' && c <= '9')
		return c - '0';
	if (c >= 'a' && c <= 'f')
		return c - 'a' + 10;
	if (c >= 'A' && c <= 'F')
		return c - 'A' + 10;
	return -1;
}

/*
 * Turn the namespace-separator a catalog advertises into the form the client
 * puts in a path. The catalog sends it percent-encoded ("%1F", "%2E"), but a
 * plain character is allowed too. Decode it, then encode every byte as %XX, so
 * a hostile value can never put a '/', '?' or '#' into the request.
 */
static char *
rest_encode_separator(const char *advertised, char **raw)
{
	static const char hex[] = "0123456789ABCDEF";
	StringInfoData out;
	StringInfoData dec;
	const char *p;

	initStringInfo(&out);
	initStringInfo(&dec);
	for (p = advertised; *p != '\0'; p++)
	{
		int			c = (unsigned char) *p;

		if (*p == '%')
		{
			int			hi = rest_hexval(p[1]);
			int			lo = (hi < 0) ? -1 : rest_hexval(p[2]);

			if (hi < 0 || lo < 0)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("iceberg: the REST catalog advertised an invalid namespace-separator \"%s\"",
								advertised)));
			c = hi * 16 + lo;
			p += 2;
		}
		if (c == 0)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: the REST catalog advertised an invalid namespace-separator \"%s\"",
							advertised)));
		appendStringInfoChar(&out, '%');
		appendStringInfoChar(&out, hex[c >> 4]);
		appendStringInfoChar(&out, hex[c & 0x0f]);
		appendStringInfoChar(&dec, (char) c);
	}
	*raw = dec.data;
	if (out.len == 0)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog advertised an empty namespace-separator")));
	return out.data;
}

/*
 * Append a catalog prefix as path segments. A prefix may hold '/', such as
 * "catalogs/main": split it there and encode each piece on its own, as
 * pyiceberg and the Java client send it. A leading or trailing '/' is dropped.
 * An empty, "." or ".." piece is refused: the prefix comes from the catalog.
 */
static void
rest_append_prefix(StringInfo out, const char *prefix)
{
	const char *p = prefix;
	const char *end;

	while (*p == '/')
		p++;
	end = p + strlen(p);
	while (end > p && end[-1] == '/')
		end--;
	while (p < end)
	{
		const char *slash = memchr(p, '/', end - p);
		const char *stop = (slash != NULL) ? slash : end;
		char	   *piece = pnstrdup(p, stop - p);

		if (piece[0] == '\0' || strcmp(piece, ".") == 0 || strcmp(piece, "..") == 0)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: the REST catalog advertised an invalid prefix \"%s\"",
							prefix)));
		rest_pct_encode(out, piece);
		appendStringInfoChar(out, '/');
		pfree(piece);
		p = (slash != NULL) ? slash + 1 : end;
	}
}

/* Get a required string field from a jsonb object, or raise. */
static char *
rest_json_string(JsonbContainer *c, const char *key, const char *what)
{
	JsonbValue	vbuf;
	JsonbValue *v;

	if (c == NULL || !JsonContainerIsObject(c))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog %s is not a JSON object", what)));
	v = getKeyJsonValueFromContainer(c, key, (int) strlen(key), &vbuf);
	if (v == NULL || v->type != jbvString)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog %s is missing the \"%s\" field",
						what, key)));
	return pnstrdup(v->val.string.val, v->val.string.len);
}

/* Optional string field: NULL when absent, raises only on a wrong type. */
static char *
rest_json_string_opt(JsonbContainer *c, const char *key)
{
	JsonbValue	vbuf;
	JsonbValue *v;

	if (c == NULL || !JsonContainerIsObject(c))
		return NULL;
	v = getKeyJsonValueFromContainer(c, key, (int) strlen(key), &vbuf);
	if (v == NULL || v->type != jbvString)
		return NULL;
	return pnstrdup(v->val.string.val, v->val.string.len);
}

/*
 * The catalog's ErrorModel from a non-200 body, {"error":{"message","type"}}:
 * *msg and *type are palloc'd strings, or NULL when absent (#1820). A body that
 * is not JSON is not an error of its own here: the caller still reports the
 * status. The message is the catalog's text, so it goes in an errdetail, cut to
 * REST_ERROR_MSG_MAX bytes on a character boundary.
 */
#define REST_ERROR_MSG_MAX 512
static void
rest_error_model(const PgColumnarHttpResult *r, char **msg, char **type)
{
	MemoryContext oldcxt = CurrentMemoryContext;
	Jsonb	   *volatile doc = NULL;

	*msg = NULL;
	*type = NULL;
	if (r->body == NULL || r->body_len <= 0 || r->body[0] != '{' ||
		strlen(r->body) != (size_t) r->body_len)
		return;
	PG_TRY();
	{
		doc = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
												 CStringGetDatum(r->body)));
	}
	PG_CATCH();
	{
		ErrorData  *edata;

		/*
		 * Not JSON: drop the parse error, and the status alone is reported.
		 * jsonb_in raises a parse error in the data-exception class (22xxx) and
		 * holds no resource, so no subtransaction is needed to drop it. Any
		 * other error (stack depth, out of memory, a cancel) is not about the
		 * body, so it rises unchanged (#1820 review).
		 */
		MemoryContextSwitchTo(oldcxt);
		edata = CopyErrorData();
		if (ERRCODE_TO_CATEGORY(edata->sqlerrcode) != ERRCODE_DATA_EXCEPTION)
		{
			FreeErrorData(edata);
			PG_RE_THROW();
		}
		FreeErrorData(edata);
		FlushErrorState();
		doc = NULL;
	}
	PG_END_TRY();
	if (doc != NULL && JsonContainerIsObject(&doc->root))
	{
		JsonbValue	vbuf;
		JsonbValue *ev = getKeyJsonValueFromContainer(&doc->root, "error", 5, &vbuf);

		if (ev != NULL && ev->type == jbvBinary &&
			JsonContainerIsObject(ev->val.binary.data))
		{
			*msg = rest_json_string_opt(ev->val.binary.data, "message");
			*type = rest_json_string_opt(ev->val.binary.data, "type");
		}
	}
	if (*msg != NULL && strlen(*msg) > REST_ERROR_MSG_MAX)
		(*msg)[pg_mbcliplen(*msg, strlen(*msg), REST_ERROR_MSG_MAX)] = '\0';
}

/*
 * GET catalog_uri + resource_path, sending the bearer token when set. Maps the
 * transport-level statuses the catalog can return to clean SQLSTATEs; returns
 * the parsed JSON body of a 200 as a Jsonb, or raises. `what` labels errors.
 */
static Jsonb *
rest_get_json(const char *catalog_uri, const char *resource_path,
			  const char *what, const char *token)
{
	const PgColumnarObjStoreApi *api = rest_objstore();
	StringInfoData url;
	const char *headers[2];
	int			nheaders = 0;
	char	   *authhdr = NULL;
	char	   *emsg = NULL;
	char	   *etype = NULL;
	PgColumnarHttpResult r;
	Datum		jd;

	initStringInfo(&url);
	appendStringInfoString(&url, catalog_uri);
	/* one '/' between the base and the resource path */
	while (url.len > 0 && url.data[url.len - 1] == '/')
		url.data[--url.len] = '\0';
	appendStringInfoString(&url, resource_path);

	headers[nheaders++] = "Accept: application/json";
	if (token != NULL && token[0] != '\0')
	{
		authhdr = psprintf("Authorization: Bearer %s", token);
		headers[nheaders++] = authhdr;
	}

	r = api->http_request(url.data, "GET", headers, nheaders,
						  NULL, 0, REST_MAX_RESPONSE);
	if (authhdr != NULL)
		pfree(authhdr);

	if (r.status != 200)
		rest_error_model(&r, &emsg, &etype);
	if (r.status == 401 || r.status == 403)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
				 errmsg("iceberg: the REST catalog refused the request (HTTP %d)",
						r.status),
				 emsg ? errdetail("The catalog said: %s", emsg) : 0,
				 errhint("Provide the catalog's bearer token via a USER MAPPING "
						 "or the %s server environment variable.", REST_TOKEN_ENV)));
	/* the ErrorModel type tells a missing namespace from a missing table */
	if (r.status == 404 && etype != NULL &&
		strcmp(etype, "NoSuchNamespaceException") == 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_SCHEMA_NAME),
				 errmsg("iceberg: the REST catalog has no such namespace (HTTP 404)"),
				 emsg ? errdetail("The catalog said: %s", emsg) : 0));
	if (r.status == 404)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_TABLE),
				 errmsg("iceberg: the REST catalog has no such %s (HTTP 404)", what),
				 emsg ? errdetail("The catalog said: %s", emsg) : 0));
	if (r.status != 200)
		ereport(ERROR,
				(errcode(ERRCODE_PROTOCOL_VIOLATION),
				 errmsg("iceberg: the REST catalog returned HTTP %d for the %s",
						r.status, what),
				 emsg ? errdetail("The catalog said: %s", emsg) : 0));
	if (r.body == NULL || r.body_len == 0)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog returned an empty %s", what)));

	jd = DirectFunctionCall1(jsonb_in, CStringGetDatum(r.body));
	return DatumGetJsonbP(jd);
}

/*
 * POST a form body to an absolute URL and return the parsed JSON reply. Used for
 * the OAuth2 token endpoint. No Authorization header is sent (this call obtains
 * the bearer). The allow-list, link-local refusal, and header guards of the
 * transport apply as for any request. A 401/403 is the catalog rejecting the
 * client credentials, mapped to 28000 with no secret in the message.
 */
static Jsonb *
rest_post_form(const char *url, const char *body, const char *what)
{
	const PgColumnarObjStoreApi *api = rest_objstore();
	const char *headers[2];
	int			nheaders = 0;
	PgColumnarHttpResult r;
	Datum		jd;

	headers[nheaders++] = "Accept: application/json";
	headers[nheaders++] = "Content-Type: application/x-www-form-urlencoded";

	r = api->http_request(url, "POST", headers, nheaders,
						  body, (int) strlen(body), REST_MAX_RESPONSE);

	if (r.status == 400 || r.status == 401 || r.status == 403)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
				 errmsg("iceberg: the REST catalog refused the OAuth2 %s (HTTP %d)",
						what, r.status),
				 errhint("Check oauth_client_id and oauth_client_secret on the "
						 "USER MAPPING.")));
	if (r.status != 200)
		ereport(ERROR,
				(errcode(ERRCODE_PROTOCOL_VIOLATION),
				 errmsg("iceberg: the REST catalog returned HTTP %d for the OAuth2 %s",
						r.status, what)));
	if (r.body == NULL || r.body_len == 0)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog returned an empty OAuth2 %s", what)));

	jd = DirectFunctionCall1(jsonb_in, CStringGetDatum(r.body));
	return DatumGetJsonbP(jd);
}

/*
 * Mint a bearer token by the OAuth2 client-credentials grant. POSTs
 * grant_type=client_credentials with the client id/secret (and optional scope),
 * each percent-encoded, to the token endpoint: `token_uri` when given, else
 * {catalog}/v1/oauth/tokens. The secret travels in the body, never a query
 * string, and is not logged. Returns the palloc'd access_token.
 */
static char *
rest_oauth_token(const char *catalog_uri, const char *token_uri,
				 const char *client_id, const char *client_secret,
				 const char *scope)
{
	StringInfoData url;
	StringInfoData body;
	Jsonb	   *reply;

	initStringInfo(&url);
	if (token_uri != NULL && token_uri[0] != '\0')
		appendStringInfoString(&url, token_uri);
	else
	{
		appendStringInfoString(&url, catalog_uri);
		while (url.len > 0 && url.data[url.len - 1] == '/')
			url.data[--url.len] = '\0';
		appendStringInfoString(&url, "/v1/oauth/tokens");
	}

	initStringInfo(&body);
	appendStringInfoString(&body, "grant_type=client_credentials");
	appendStringInfoString(&body, "&client_id=");
	rest_pct_encode(&body, client_id);
	appendStringInfoString(&body, "&client_secret=");
	rest_pct_encode(&body, client_secret);
	if (scope != NULL && scope[0] != '\0')
	{
		appendStringInfoString(&body, "&scope=");
		rest_pct_encode(&body, scope);
	}

	reply = rest_post_form(url.data, body.data, "token exchange");
	if (!JsonContainerIsObject(&reply->root))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the OAuth2 token response is not a JSON object")));
	return rest_json_string(&reply->root, "access_token", "OAuth2 token response");
}

/*
 * A config property: "overrides" first, then a top-level key, then "defaults"
 * (the REST spec: overrides win over the client, defaults lose to it). NULL
 * when no level sets it as a string.
 */
static char *
rest_config_value(JsonbContainer *root, const char *key)
{
	char	   *v = NULL;
	JsonbValue	vbuf;
	JsonbValue *ov;

	ov = getKeyJsonValueFromContainer(root, "overrides", 9, &vbuf);
	if (ov != NULL && ov->type == jbvBinary)
		v = rest_json_string_opt(ov->val.binary.data, key);
	if (v == NULL)
		v = rest_json_string_opt(root, key);
	if (v == NULL)
	{
		JsonbValue *dv = getKeyJsonValueFromContainer(root, "defaults", 8, &vbuf);

		if (dv != NULL && dv->type == jbvBinary)
			v = rest_json_string_opt(dv->val.binary.data, key);
	}
	return v;
}

/*
 * GET /v1/config and fill *cc with the path base and the namespace separator.
 * Every resource call goes through this, so the list functions see the same
 * prefix and separator as loadTable (#1793).
 */
static void
rest_catalog_config(const char *catalog_uri, const char *token,
					const char *warehouse, RestCatalogConfig *cc)
{
	Jsonb	   *cfg;
	char	   *prefix;
	char	   *sep;
	StringInfoData cfgpath;
	StringInfoData base;

	if (pg_strncasecmp(catalog_uri, "http://", 7) != 0 &&
		pg_strncasecmp(catalog_uri, "https://", 8) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: a REST catalog URI must be http:// or https://")));

	/* A warehouse option selects a warehouse on a multi-warehouse catalog and is
	 * sent as the ?warehouse= query parameter (Iceberg REST spec). */
	initStringInfo(&cfgpath);
	appendStringInfoString(&cfgpath, "/v1/config");
	if (warehouse != NULL && warehouse[0] != '\0')
	{
		appendStringInfoString(&cfgpath, "?warehouse=");
		rest_pct_encode(&cfgpath, warehouse);
	}
	cfg = rest_get_json(catalog_uri, cfgpath.data, "config", token);
	/*
	 * The config body is untrusted (a compromised or hostile allow-listed
	 * catalog). getKeyJsonValueFromContainer Asserts object-ness before its
	 * count<=0 early-out and then walks children as key/value pairs, so a
	 * non-object root ([], a scalar) is an assert crash on a debug build and an
	 * out-of-bounds read otherwise. Refuse it before any key lookup.
	 */
	if (!JsonContainerIsObject(&cfg->root))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog config response is not a JSON object")));
	prefix = rest_config_value(&cfg->root, "prefix");
	initStringInfo(&base);
	appendStringInfoString(&base, "/v1/");
	if (prefix != NULL)
		rest_append_prefix(&base, prefix);
	cc->base = base.data;

	sep = rest_config_value(&cfg->root, "namespace-separator");
	cc->sep = rest_encode_separator(sep != NULL ? sep : REST_DEFAULT_SEPARATOR,
									&cc->rawsep);
}

/*
 * Call GET /v1/config (to learn the prefix and separator) then loadTable, and
 * return the whole loadTable JSON document. *out_location is set to its
 * metadata-location.
 */
static Jsonb *
rest_load_table_doc(const char *catalog_uri, List *levels, const char *table,
					const char *token, const char *warehouse, char **out_location)
{
	RestCatalogConfig cc;
	Jsonb	   *lt;
	StringInfoData rp;

	rest_catalog_config(catalog_uri, token, warehouse, &cc);
	initStringInfo(&rp);
	appendStringInfoString(&rp, cc.base);
	appendStringInfoString(&rp, "namespaces/");
	rest_append_levels(&rp, levels, &cc);
	appendStringInfoString(&rp, "/tables/");
	rest_pct_encode(&rp, table);

	lt = rest_get_json(catalog_uri, rp.data, "table", token);
	if (out_location != NULL)
		*out_location = rest_json_string(&lt->root, "metadata-location",
										 "loadTable response");
	return lt;
}

/*
 * One vended credential config as an object-store config, or NULL when it holds
 * no s3.access-key-id and s3.secret-access-key (it vends no usable credential).
 * allow_ambient is false: the module must use these keys, never the server's.
 */
static PgColumnarObjStoreConfig *
rest_cred_cfg(JsonbContainer *c)
{
	char	   *akid = rest_json_string_opt(c, "s3.access-key-id");
	char	   *secret = rest_json_string_opt(c, "s3.secret-access-key");
	PgColumnarObjStoreConfig *cfg;

	if (akid == NULL || secret == NULL)
		return NULL;
	cfg = (PgColumnarObjStoreConfig *) palloc0(sizeof(PgColumnarObjStoreConfig));
	cfg->akid = akid;
	cfg->secret = secret;
	cfg->token = rest_json_string_opt(c, "s3.session-token");
	cfg->region = rest_json_string_opt(c, "s3.region");
	if (cfg->region == NULL)
		cfg->region = rest_json_string_opt(c, "client.region");
	cfg->endpoint = rest_json_string_opt(c, "s3.endpoint");
	cfg->allow_ambient = false;
	cfg->vended = true;			/* a refused key's hint names the catalog (#1796) */
	return cfg;
}

/*
 * Build an object-store config from the storage credentials a loadTable reply
 * vends, or NULL when it vends none (the read then uses the ambient
 * environment, as for iceberg_scan).
 *
 * With no usable `storage-credentials` entry, the flat `config` signs every
 * file. Otherwise the result carries every entry, and the reader chooses one
 * per file (#1820): the entry whose `prefix` is the longest match for that
 * file's URL, else the flat config, else it refuses the file. An entry or a
 * flat config without both S3 keys vends nothing and is ignored. Once a catalog
 * vends credentials, no file is read silently with the server's ambient
 * identity.
 */
static PgColumnarObjStoreConfig *
rest_vended_cfg(JsonbContainer *lt)
{
	JsonbValue	vbuf;
	JsonbValue *sc;
	JsonbValue *fv;
	PgColumnarObjStoreConfig *flat = NULL;
	PgColumnarObjStoreCredSet *cs;
	PgColumnarObjStoreConfig *cfg;

	if (lt == NULL || !JsonContainerIsObject(lt))
		return NULL;

	fv = getKeyJsonValueFromContainer(lt, "config", 6, &vbuf);
	if (fv != NULL && fv->type == jbvBinary &&
		JsonContainerIsObject(fv->val.binary.data))
		flat = rest_cred_cfg(fv->val.binary.data);

	cs = (PgColumnarObjStoreCredSet *) palloc0(sizeof(PgColumnarObjStoreCredSet));
	sc = getKeyJsonValueFromContainer(lt, "storage-credentials", 19, &vbuf);
	if (sc != NULL && sc->type == jbvBinary &&
		JsonContainerIsArray(sc->val.binary.data))
	{
		uint32		n = JsonContainerSize(sc->val.binary.data);
		uint32		i;

		cs->prefix = (const char **) palloc0(sizeof(char *) * Max(n, 1));
		cs->cfg = (const PgColumnarObjStoreConfig **)
			palloc0(sizeof(PgColumnarObjStoreConfig *) * Max(n, 1));
		for (i = 0; i < n; i++)
		{
			JsonbValue *e = getIthJsonbValueFromContainer(sc->val.binary.data, i);
			JsonbValue	pbuf,
						cbuf;
			JsonbValue *pv;
			JsonbValue *cv;
			PgColumnarObjStoreConfig *ec;

			if (e == NULL || e->type != jbvBinary ||
				!JsonContainerIsObject(e->val.binary.data))
				continue;
			pv = getKeyJsonValueFromContainer(e->val.binary.data, "prefix", 6, &pbuf);
			cv = getKeyJsonValueFromContainer(e->val.binary.data, "config", 6, &cbuf);
			if (pv == NULL || pv->type != jbvString ||
				cv == NULL || cv->type != jbvBinary ||
				!JsonContainerIsObject(cv->val.binary.data))
				continue;
			ec = rest_cred_cfg(cv->val.binary.data);
			if (ec == NULL)
				continue;
			cs->prefix[cs->n] = pnstrdup(pv->val.string.val, pv->val.string.len);
			cs->cfg[cs->n] = ec;
			cs->n++;
		}
	}

	if (cs->n == 0)
		return flat;			/* NULL here means the ambient environment */

	/*
	 * No credential triple and allow_ambient false: if this config ever reached
	 * the module unresolved, the module would refuse it (28000).
	 */
	cs->fallback = flat;
	cfg = (PgColumnarObjStoreConfig *) palloc0(sizeof(PgColumnarObjStoreConfig));
	cfg->allow_ambient = false;
	cfg->byprefix = cs;
	return cfg;
}

/*
 * Resolve the current metadata-location of catalog_uri's ns.table.
 */
char *
PgColumnarIcebergRestLoadTableLocation(const char *catalog_uri,
									   const char *ns, const char *table,
									   const char *token, const char *warehouse)
{
	char	   *loc;

	(void) rest_load_table_doc(catalog_uri, rest_split_namespace(ns), table,
							   token, warehouse, &loc);
	return loc;
}

/* Every REST entry point reads an external service: superuser or this role. */
static void
rest_check_privilege(void)
{
	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a REST catalog")));
}

/*
 * iceberg_rest_table_location, with the namespace as a dot-separated text or
 * as a text[] of levels (#1865).
 */
static Datum
rest_table_location(FunctionCallInfo fcinfo, bool ns_is_array)
{
	char	   *arg0;
	List	   *levels;
	char	   *table;
	char	   *catalog_uri;
	char	   *token;
	char	   *warehouse;
	char	   *loc;

	rest_check_privilege();
	arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	levels = rest_namespace_arg(fcinfo, 1, ns_is_array);
	table = text_to_cstring(PG_GETARG_TEXT_PP(2));
	rest_resolve(arg0, &catalog_uri, &token, &warehouse);
	(void) rest_load_table_doc(catalog_uri, levels, table, token, warehouse, &loc);
	PG_RETURN_TEXT_P(cstring_to_text(loc));
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_table_location);

/*
 * SQL: iceberg_rest_table_location(catalog_uri text, namespace text,
 * table_name text) RETURNS text. Superuser / pg_read_server_files, like every
 * other external read in this extension.
 */
Datum
pgcolumnar_iceberg_rest_table_location(PG_FUNCTION_ARGS)
{
	return rest_table_location(fcinfo, false);
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_table_location_levels);

/* SQL: iceberg_rest_table_location(catalog_uri text, namespace text[], table_name text). */
Datum
pgcolumnar_iceberg_rest_table_location_levels(PG_FUNCTION_ARGS)
{
	return rest_table_location(fcinfo, true);
}

/*
 * iceberg_rest_scan: resolve the table's current metadata-location through the
 * catalog, then read it through the very same path as iceberg_scan (a resolved
 * remote location reads because the reader is remote-capable). Superuser /
 * pg_read_server_files, materialize-mode SRF, column definition list required.
 */
static Datum
rest_scan(FunctionCallInfo fcinfo, bool ns_is_array)
{
	char	   *arg0;
	List	   *levels;
	char	   *table;
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	TupleDesc	tupdesc;
	char	   *catalog_uri;
	char	   *token;
	char	   *warehouse;
	Jsonb	   *lt;
	char	   *loc;
	PgColumnarObjStoreConfig *cfg;

	rest_check_privilege();
	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo) ||
		(rsinfo->allowedModes & SFRM_Materialize) == 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in a context that cannot accept a set")));
	if (get_call_result_type(fcinfo, NULL, &tupdesc) != TYPEFUNC_COMPOSITE)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.iceberg_rest_scan requires a column definition list"),
				 errhint("Call it as SELECT * FROM pgcolumnar.iceberg_rest_scan(uri, ns, tbl) AS t(col1 type1, ...).")));

	arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	levels = rest_namespace_arg(fcinfo, 1, ns_is_array);
	table = text_to_cstring(PG_GETARG_TEXT_PP(2));
	rest_resolve(arg0, &catalog_uri, &token, &warehouse);
	/* one loadTable: the metadata-location AND the vended storage creds (if any) */
	lt = rest_load_table_doc(catalog_uri, levels, table, token, warehouse, &loc);
	cfg = rest_vended_cfg(&lt->root);
	/*
	 * A metadata-location is a URI. The reader's remote path handles s3:// and
	 * http(s)://; a file:// location is a local path, and the local read path
	 * opens a bare filename, so strip the scheme here.
	 */
	if (pg_strncasecmp(loc, "file://", 7) == 0)
		loc += 7;
	PgColumnarIcebergScanInto(loc, tupdesc, rsinfo, cfg, "pgcolumnar.iceberg_rest_scan");
	return (Datum) 0;
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_scan);

/* SQL: iceberg_rest_scan(catalog_uri, namespace text, table_name) RETURNS SETOF record. */
Datum
pgcolumnar_iceberg_rest_scan(PG_FUNCTION_ARGS)
{
	return rest_scan(fcinfo, false);
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_scan_levels);

/* SQL: iceberg_rest_scan(catalog_uri, namespace text[], table_name) RETURNS SETOF record. */
Datum
pgcolumnar_iceberg_rest_scan_levels(PG_FUNCTION_ARGS)
{
	return rest_scan(fcinfo, true);
}

/*
 * Bounds on a list walk, so a broken or hostile catalog cannot keep a backend
 * busy for ever. A real catalog stays far below both.
 */
#define REST_MAX_PAGES				100000
#define REST_MAX_NAMESPACE_DEPTH	64

/*
 * A set of strings, for "seen before?" questions about values a catalog sends.
 * Keyed by a 64-bit hash; each entry keeps every string with that hash, so a
 * collision never makes two different strings look equal.
 */
typedef struct RestStrSetEntry
{
	uint64		hash;			/* the key; must be first */
	List	   *strs;
} RestStrSetEntry;

/*
 * Is `s` in *set? With `add`, also put it there. *set starts NULL and is made
 * on the first add, in the current memory context.
 */
static bool
rest_strset_lookup(HTAB **set, const char *s, bool add)
{
	uint64		h = hash_bytes_extended((const unsigned char *) s, (int) strlen(s), 0);
	RestStrSetEntry *e;
	bool		found;
	ListCell   *lc;

	if (*set == NULL)
	{
		HASHCTL		ctl;

		if (!add)
			return false;
		memset(&ctl, 0, sizeof(ctl));
		ctl.keysize = sizeof(uint64);
		ctl.entrysize = sizeof(RestStrSetEntry);
		ctl.hcxt = CurrentMemoryContext;
		*set = hash_create("pgcolumnar REST string set", 64, &ctl,
						   HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	}
	e = (RestStrSetEntry *) hash_search(*set, &h, add ? HASH_ENTER : HASH_FIND, &found);
	if (!found)
	{
		if (e != NULL)
			e->strs = list_make1(pstrdup(s));
		return false;
	}
	foreach(lc, e->strs)
	{
		if (strcmp((const char *) lfirst(lc), s) == 0)
			return true;
	}
	if (add)
		e->strs = lappend(e->strs, pstrdup(s));
	return false;
}

/*
 * GET a list resource and return the elements of its `key` array, following
 * next-page-token across pages (the REST spec's pageToken query parameter).
 * `path` may already hold a query string. Each element is a JsonbValue that
 * points into its page's document. `what` labels errors and the object check.
 */
static List *
rest_get_list(const char *catalog_uri, const char *path, const char *key,
			  const char *what, const char *token)
{
	List	   *items = NIL;
	char	   *pagetoken = NULL;
	int			pages = 0;
	HTAB	   *seen = NULL;	/* every token sent so far */

	for (;;)
	{
		StringInfoData rp;
		Jsonb	   *doc;
		JsonbValue	vbuf;
		JsonbValue *arr;
		char	   *next;

		CHECK_FOR_INTERRUPTS();
		if (++pages > REST_MAX_PAGES)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("iceberg: the REST catalog %s has more than %d pages",
							what, REST_MAX_PAGES)));
		initStringInfo(&rp);
		appendStringInfoString(&rp, path);
		if (pagetoken != NULL)
		{
			appendStringInfoString(&rp, strchr(path, '?') ? "&pageToken=" : "?pageToken=");
			rest_pct_encode(&rp, pagetoken);
		}
		doc = rest_get_json(catalog_uri, rp.data, what, token);
		if (!JsonContainerIsObject(&doc->root))
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: the REST catalog %s response is not a JSON object",
							what)));
		arr = getKeyJsonValueFromContainer(&doc->root, key, (int) strlen(key), &vbuf);
		if (arr != NULL && arr->type == jbvBinary &&
			JsonContainerIsArray(arr->val.binary.data))
		{
			uint32		n = JsonContainerSize(arr->val.binary.data);
			uint32		i;

			for (i = 0; i < n; i++)
			{
				JsonbValue *e = getIthJsonbValueFromContainer(arr->val.binary.data, i);

				if (e != NULL)
					items = lappend(items, e);
			}
		}

		/* a null, absent or empty token ends the list */
		next = rest_json_string_opt(&doc->root, "next-page-token");
		if (next == NULL || next[0] == '\0')
			break;
		/* any earlier token, not only the last one: an A, B, A cycle never ends */
		if (rest_strset_lookup(&seen, next, true))
			ereport(ERROR,
					(errcode(ERRCODE_PROTOCOL_VIOLATION),
					 errmsg("iceberg: the REST catalog returned the same page token twice for the %s",
							what)));
		pagetoken = next;
	}
	return items;
}

/*
 * The levels of one namespace element (a JSON array of strings) as a List, or
 * NIL when the element is not such an array.
 */
static List *
rest_namespace_levels(JsonbValue *lv)
{
	List	   *levels = NIL;
	uint32		nlev;
	uint32		j;

	if (lv == NULL || lv->type != jbvBinary ||
		!JsonContainerIsArray(lv->val.binary.data))
		return NIL;
	nlev = JsonContainerSize(lv->val.binary.data);
	for (j = 0; j < nlev; j++)
	{
		JsonbValue *pv = getIthJsonbValueFromContainer(lv->val.binary.data, j);

		if (pv == NULL || pv->type != jbvString)
			return NIL;
		levels = lappend(levels, pnstrdup(pv->val.string.val, pv->val.string.len));
	}
	return levels;
}

/*
 * A key for a namespace's levels that no two different namespaces share: each
 * level with its length in front, so ["a.b"] and ["a","b"] differ.
 */
static char *
rest_levels_key(List *levels)
{
	StringInfoData k;
	ListCell   *lc;

	initStringInfo(&k);
	foreach(lc, levels)
	{
		const char *l = (const char *) lfirst(lc);

		appendStringInfo(&k, "%zu:%s", strlen(l), l);
	}
	return k.data;
}

/*
 * Emit every namespace under `parent` (NIL: the top level), then walk into
 * each one with ?parent=. The REST spec lists only the direct children of
 * `parent`, so a nested namespace appears only through this walk (#1793).
 *
 * Below the top level, a child must extend its parent by exactly one level;
 * anything else (such as a catalog that ignores ?parent= and repeats the top
 * level) is skipped, so the walk always goes deeper and ends.
 *
 * Some catalogs list nested namespaces at the top level too. Each one is
 * emitted there, as before #1793, but not walked from there, and *topnested
 * remembers it so the walk below does not emit it a second time. The walk
 * still goes into it, to find its own children.
 *
 * Each namespace is emitted as its dot-joined levels (text), or with
 * `as_array` as a text[] of its levels, which keeps a level that holds a dot
 * apart from two levels (#1865). A level that holds the catalog's separator
 * is emitted but not walked into: no request can name it.
 */
static void
rest_walk_namespaces(const char *catalog_uri, const char *token,
					 RestCatalogConfig *cc, List *parent, HTAB **topnested,
					 bool as_array, Tuplestorestate *ts, TupleDesc td)
{
	StringInfoData rp;
	List	   *items;
	ListCell   *lc;
	int			depth = list_length(parent);

	/* a namespace REST_MAX_NAMESPACE_DEPTH levels deep is still walked */
	if (depth > REST_MAX_NAMESPACE_DEPTH)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("iceberg: the REST catalog nests namespaces deeper than %d levels",
						REST_MAX_NAMESPACE_DEPTH)));
	check_stack_depth();

	initStringInfo(&rp);
	appendStringInfoString(&rp, cc->base);
	appendStringInfoString(&rp, "namespaces");
	if (parent != NIL)
	{
		appendStringInfoString(&rp, "?parent=");
		rest_append_levels(&rp, parent, cc);
	}
	items = rest_get_list(catalog_uri, rp.data, "namespaces", "namespaces", token);

	if (parent == NIL)
	{
		foreach(lc, items)
		{
			List	   *levels = rest_namespace_levels((JsonbValue *) lfirst(lc));

			if (list_length(levels) > 1)
				rest_strset_lookup(topnested, rest_levels_key(levels), true);
		}
	}

	foreach(lc, items)
	{
		List	   *levels = rest_namespace_levels((JsonbValue *) lfirst(lc));
		StringInfoData name;
		ListCell   *l2;
		Datum		values[1];
		bool		nulls[1] = {false};

		if (levels == NIL)
			continue;
		if (parent != NIL)
		{
			bool		extends = (list_length(levels) == depth + 1);
			int			k = 0;

			foreach(l2, parent)
			{
				if (!extends)
					break;
				extends = strcmp((const char *) lfirst(l2),
								 (const char *) list_nth(levels, k++)) == 0;
			}
			if (!extends)
				continue;
		}

		/* emitted already when the top level listed it */
		if (parent == NIL ||
			!rest_strset_lookup(topnested, rest_levels_key(levels), false))
		{
			if (as_array)
			{
				Datum	   *elems = palloc(sizeof(Datum) * list_length(levels));
				int			k = 0;

				foreach(l2, levels)
					elems[k++] = CStringGetTextDatum((const char *) lfirst(l2));
				values[0] = PointerGetDatum(construct_array(elems, k, TEXTOID, -1,
															false, TYPALIGN_INT));
				tuplestore_putvalues(ts, td, values, nulls);
			}
			else
			{
				/* dot-join the levels */
				initStringInfo(&name);
				foreach(l2, levels)
				{
					if (name.len > 0)
						appendStringInfoChar(&name, '.');
					appendStringInfoString(&name, (const char *) lfirst(l2));
				}
				values[0] = PointerGetDatum(cstring_to_text_with_len(name.data, name.len));
				tuplestore_putvalues(ts, td, values, nulls);
				pfree(name.data);
			}
		}

		/* a nested namespace on the top-level list is walked from its parent */
		if (parent == NIL && list_length(levels) > 1)
			continue;
		/* a level the catalog cannot be asked about has no children we can list */
		if (rest_unnameable_level(levels, cc) != NULL)
			continue;
		rest_walk_namespaces(catalog_uri, token, cc, levels, topnested, as_array,
							 ts, td);
	}
}

/* iceberg_rest_namespaces (text rows) and iceberg_rest_namespace_levels (text[]) */
static Datum
rest_namespaces(FunctionCallInfo fcinfo, bool as_array)
{
	char	   *arg0;
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Tuplestorestate *ts;
	TupleDesc	td;
	char	   *catalog_uri;
	char	   *token;
	char	   *warehouse;
	RestCatalogConfig cc;
	HTAB	   *topnested = NULL;

	rest_check_privilege();
	arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	ts = rest_text_srf_setup(rsinfo, &td);
	if (as_array)
		TupleDescInitEntry(td, 1, "namespace", TEXTARRAYOID, -1, 0);

	rest_resolve(arg0, &catalog_uri, &token, &warehouse);
	rest_catalog_config(catalog_uri, token, warehouse, &cc);
	rest_walk_namespaces(catalog_uri, token, &cc, NIL, &topnested, as_array, ts, td);
	return (Datum) 0;
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_namespaces);

/*
 * SQL: iceberg_rest_namespaces(catalog_uri) RETURNS SETOF text. One row per
 * namespace, at every level; a multi-level namespace is dot-joined.
 */
Datum
pgcolumnar_iceberg_rest_namespaces(PG_FUNCTION_ARGS)
{
	return rest_namespaces(fcinfo, false);
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_namespace_levels);

/*
 * SQL: iceberg_rest_namespace_levels(catalog_uri) RETURNS SETOF text[]. The
 * same namespaces, each as a text[] of its levels (#1865). A row passes back
 * unchanged as the namespace argument of the other REST functions, except a
 * row with a level that holds the catalog's separator: they refuse it (22023).
 */
Datum
pgcolumnar_iceberg_rest_namespace_levels(PG_FUNCTION_ARGS)
{
	return rest_namespaces(fcinfo, true);
}

/*
 * iceberg_rest_tables: one row per table name in the namespace, given as a
 * dot-separated text or as a text[] of levels (#1865).
 */
static Datum
rest_tables(FunctionCallInfo fcinfo, bool ns_is_array)
{
	char	   *arg0;
	List	   *levels;
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Tuplestorestate *ts;
	TupleDesc	td;
	char	   *catalog_uri;
	char	   *token;
	char	   *warehouse;
	RestCatalogConfig cc;
	StringInfoData rp;
	List	   *items;
	ListCell   *lc;

	rest_check_privilege();
	arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	levels = rest_namespace_arg(fcinfo, 1, ns_is_array);
	ts = rest_text_srf_setup(rsinfo, &td);

	rest_resolve(arg0, &catalog_uri, &token, &warehouse);
	rest_catalog_config(catalog_uri, token, warehouse, &cc);
	initStringInfo(&rp);
	appendStringInfoString(&rp, cc.base);
	appendStringInfoString(&rp, "namespaces/");
	rest_append_levels(&rp, levels, &cc);
	appendStringInfoString(&rp, "/tables");
	items = rest_get_list(catalog_uri, rp.data, "identifiers", "table list", token);

	foreach(lc, items)
	{
		JsonbValue *e = (JsonbValue *) lfirst(lc);
		JsonbValue	nbuf;
		JsonbValue *nm;
		Datum		values[1];
		bool		nulls[1] = {false};

		if (e->type != jbvBinary || !JsonContainerIsObject(e->val.binary.data))
			continue;
		nm = getKeyJsonValueFromContainer(e->val.binary.data, "name", 4, &nbuf);
		if (nm == NULL || nm->type != jbvString)
			continue;
		values[0] = PointerGetDatum(cstring_to_text_with_len(nm->val.string.val,
															 nm->val.string.len));
		tuplestore_putvalues(ts, td, values, nulls);
	}
	return (Datum) 0;
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_tables);

/* SQL: iceberg_rest_tables(catalog_uri, namespace text) RETURNS SETOF text. */
Datum
pgcolumnar_iceberg_rest_tables(PG_FUNCTION_ARGS)
{
	return rest_tables(fcinfo, false);
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_tables_levels);

/* SQL: iceberg_rest_tables(catalog_uri, namespace text[]) RETURNS SETOF text. */
Datum
pgcolumnar_iceberg_rest_tables_levels(PG_FUNCTION_ARGS)
{
	return rest_tables(fcinfo, true);
}

/* -------------------------------------------------------------------------
 * The pgcolumnar_iceberg_catalog foreign-data wrapper (#656). It creates no
 * foreign tables -- a catalog is a service endpoint plus a per-role token -- so
 * the wrapper is validator-only. A CREATE SERVER under it holds catalog_uri (a
 * non-secret, world-readable option); a CREATE USER MAPPING holds the bearer
 * token (a secret, which pg_user_mapping keeps role-private).
 * ------------------------------------------------------------------------- */

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_catalog_validator);

Datum
pgcolumnar_iceberg_catalog_validator(PG_FUNCTION_ARGS)
{
	List	   *options = untransformRelOptions(PG_GETARG_DATUM(0));
	Oid			catalog = PG_GETARG_OID(1);
	ListCell   *lc;

	foreach(lc, options)
	{
		DefElem    *def = (DefElem *) lfirst(lc);

		if (catalog == ForeignServerRelationId)
		{
			/* srvoptions is world-readable: only non-secret options belong here.
			 * catalog_uri is the endpoint; warehouse selects a warehouse on a
			 * multi-warehouse catalog and is sent on GET /v1/config. */
			if (strcmp(def->defname, "catalog_uri") != 0 &&
				strcmp(def->defname, "warehouse") != 0)
				ereport(ERROR,
						(errcode(ERRCODE_FDW_INVALID_OPTION_NAME),
						 errmsg("invalid option \"%s\" for a pgcolumnar_iceberg_catalog server",
								def->defname),
						 errhint("Valid server options: catalog_uri, warehouse. Secrets go on a USER MAPPING.")));
		}
		else if (catalog == UserMappingRelationId)
		{
			/* secrets live only here (pg_user_mapping is role-protected):
			 * the static bearer token, or the OAuth2 client-credentials inputs */
			if (strcmp(def->defname, "token") == 0 ||
				strcmp(def->defname, "oauth_client_id") == 0 ||
				strcmp(def->defname, "oauth_client_secret") == 0 ||
				strcmp(def->defname, "oauth_scope") == 0 ||
				strcmp(def->defname, "oauth_token_uri") == 0)
				 /* a credential input */ ;
			else if (strcmp(def->defname, "credentials_required") == 0)
			{
				(void) defGetBoolean(def);
				if (!defGetBoolean(def) && !superuser())
					ereport(ERROR,
							(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
							 errmsg("only a superuser may set credentials_required 'false'")));
			}
			else
				ereport(ERROR,
						(errcode(ERRCODE_FDW_INVALID_OPTION_NAME),
						 errmsg("invalid option \"%s\" for a pgcolumnar_iceberg_catalog user mapping",
								def->defname),
						 errhint("Valid user-mapping options: token, oauth_client_id, "
								 "oauth_client_secret, oauth_scope, oauth_token_uri, "
								 "credentials_required.")));
		}
		else if (def->defname != NULL)
			ereport(ERROR,
					(errcode(ERRCODE_FDW_INVALID_OPTION_NAME),
					 errmsg("invalid option \"%s\" for pgcolumnar_iceberg_catalog",
							def->defname)));
	}
	PG_RETURN_VOID();
}

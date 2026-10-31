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
#include "fmgr.h"
#include "foreign/foreign.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "utils/acl.h"
#include "utils/builtins.h"
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
 * the current role's bearer token. The token comes from the role's USER MAPPING
 * (or the PUBLIC mapping), which lives in pg_user_mapping and is not
 * world-readable. When the mapping carries no token, fall back to the
 * environment token only if ambient use is allowed (a superuser, or a mapping
 * that set credentials_required 'false'); otherwise refuse (28000). *out_token
 * may be NULL for an anonymous catalog.
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
 * Append a namespace to a resource path. The REST wire form joins multi-level
 * namespace parts with the unit separator 0x1F ("%1F" encoded); a caller passes
 * the levels dot-separated. Each level is percent-encoded, so a level bearing
 * '/', '.', or a control byte cannot alter the request path.
 */
static void
rest_append_namespace(StringInfo out, const char *ns)
{
	const char *start = ns;
	const char *p;

	for (p = ns;; p++)
	{
		if (*p == '.' || *p == '\0')
		{
			char	   *level = pnstrdup(start, p - start);

			rest_pct_encode(out, level);
			pfree(level);
			if (*p == '\0')
				break;
			appendStringInfoString(out, "%1F");
			start = p + 1;
		}
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

	if (r.status == 401 || r.status == 403)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
				 errmsg("iceberg: the REST catalog refused the request (HTTP %d)",
						r.status),
				 errhint("Provide the catalog's bearer token via a USER MAPPING "
						 "or the %s server environment variable.", REST_TOKEN_ENV)));
	if (r.status == 404)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_TABLE),
				 errmsg("iceberg: the REST catalog has no such %s (HTTP 404)", what)));
	if (r.status != 200)
		ereport(ERROR,
				(errcode(ERRCODE_PROTOCOL_VIOLATION),
				 errmsg("iceberg: the REST catalog returned HTTP %d for the %s",
						r.status, what)));
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
 * Call GET /v1/config (to learn any path prefix) then loadTable, and return the
 * whole loadTable JSON document. *out_location is set to its metadata-location.
 */
static Jsonb *
rest_load_table_doc(const char *catalog_uri, const char *ns, const char *table,
					const char *token, const char *warehouse, char **out_location)
{
	Jsonb	   *cfg;
	char	   *prefix;
	Jsonb	   *lt;
	StringInfoData rp;
	StringInfoData cfgpath;

	if (pg_strncasecmp(catalog_uri, "http://", 7) != 0 &&
		pg_strncasecmp(catalog_uri, "https://", 8) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("iceberg: a REST catalog URI must be http:// or https://")));

	/* config: {overrides, defaults}; a prefix, if any, is spliced after /v1/.
	 * A warehouse option selects a warehouse on a multi-warehouse catalog and is
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
	prefix = rest_json_string_opt(&cfg->root, "prefix");
	if (prefix == NULL)
	{
		JsonbValue	vbuf;
		JsonbValue *ov = getKeyJsonValueFromContainer(&cfg->root, "overrides", 9,
													  &vbuf);

		if (ov != NULL && ov->type == jbvBinary)
			prefix = rest_json_string_opt(ov->val.binary.data, "prefix");
	}

	initStringInfo(&rp);
	appendStringInfoString(&rp, "/v1/");
	if (prefix != NULL && prefix[0] != '\0')
	{
		rest_pct_encode(&rp, prefix);
		appendStringInfoChar(&rp, '/');
	}
	appendStringInfoString(&rp, "namespaces/");
	rest_append_namespace(&rp, ns);
	appendStringInfoString(&rp, "/tables/");
	rest_pct_encode(&rp, table);

	lt = rest_get_json(catalog_uri, rp.data, "table", token);
	if (out_location != NULL)
		*out_location = rest_json_string(&lt->root, "metadata-location",
										 "loadTable response");
	return lt;
}

/*
 * Build an object-store config from the storage credentials a loadTable reply
 * vends, or NULL when it vends none (the caller then falls back to ambient). A
 * `storage-credentials` array is preferred, choosing the entry whose `prefix` is
 * the longest match for `metadata_location`; otherwise the flat `config` object.
 * allow_ambient is false: once a catalog vends credentials a read must use them,
 * never silently the server's ambient identity.
 */
static PgColumnarObjStoreConfig *
rest_vended_cfg(JsonbContainer *lt, const char *metadata_location)
{
	JsonbContainer *chosen = NULL;
	int			bestlen = -1;
	JsonbValue	vbuf;
	JsonbValue *sc;
	char	   *akid;
	char	   *secret;
	PgColumnarObjStoreConfig *cfg;

	if (lt == NULL || !JsonContainerIsObject(lt))
		return NULL;

	sc = getKeyJsonValueFromContainer(lt, "storage-credentials", 19, &vbuf);
	if (sc != NULL && sc->type == jbvBinary &&
		JsonContainerIsArray(sc->val.binary.data))
	{
		uint32		n = JsonContainerSize(sc->val.binary.data);
		uint32		i;
		int			mloclen = (int) strlen(metadata_location);

		for (i = 0; i < n; i++)
		{
			JsonbValue *e = getIthJsonbValueFromContainer(sc->val.binary.data, i);
			JsonbValue	pbuf,
						cbuf;
			JsonbValue *pv;
			JsonbValue *cv;

			if (e == NULL || e->type != jbvBinary ||
				!JsonContainerIsObject(e->val.binary.data))
				continue;
			pv = getKeyJsonValueFromContainer(e->val.binary.data, "prefix", 6, &pbuf);
			cv = getKeyJsonValueFromContainer(e->val.binary.data, "config", 6, &cbuf);
			if (pv == NULL || pv->type != jbvString ||
				cv == NULL || cv->type != jbvBinary ||
				!JsonContainerIsObject(cv->val.binary.data))
				continue;
			if (pv->val.string.len <= mloclen &&
				strncmp(metadata_location, pv->val.string.val,
						pv->val.string.len) == 0 &&
				pv->val.string.len > bestlen)
			{
				bestlen = pv->val.string.len;
				chosen = cv->val.binary.data;
			}
		}
	}

	if (chosen == NULL)
	{
		JsonbValue *cv = getKeyJsonValueFromContainer(lt, "config", 6, &vbuf);

		if (cv != NULL && cv->type == jbvBinary &&
			JsonContainerIsObject(cv->val.binary.data))
			chosen = cv->val.binary.data;
	}
	if (chosen == NULL)
		return NULL;

	akid = rest_json_string_opt(chosen, "s3.access-key-id");
	secret = rest_json_string_opt(chosen, "s3.secret-access-key");
	if (akid == NULL || secret == NULL)
		return NULL;			/* no usable vended creds -> ambient fallback */

	cfg = (PgColumnarObjStoreConfig *) palloc0(sizeof(PgColumnarObjStoreConfig));
	cfg->akid = akid;
	cfg->secret = secret;
	cfg->token = rest_json_string_opt(chosen, "s3.session-token");
	cfg->region = rest_json_string_opt(chosen, "s3.region");
	if (cfg->region == NULL)
		cfg->region = rest_json_string_opt(chosen, "client.region");
	cfg->endpoint = rest_json_string_opt(chosen, "s3.endpoint");
	cfg->allow_ambient = false;
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

	(void) rest_load_table_doc(catalog_uri, ns, table, token, warehouse, &loc);
	return loc;
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
	char	   *arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	char	   *ns = text_to_cstring(PG_GETARG_TEXT_PP(1));
	char	   *table = text_to_cstring(PG_GETARG_TEXT_PP(2));
	char	   *catalog_uri;
	char	   *token;
	char	   *warehouse;
	char	   *loc;

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a REST catalog")));

	rest_resolve(arg0, &catalog_uri, &token, &warehouse);
	loc = PgColumnarIcebergRestLoadTableLocation(catalog_uri, ns, table, token,
												 warehouse);
	PG_RETURN_TEXT_P(cstring_to_text(loc));
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_scan);

/*
 * SQL: iceberg_rest_scan(catalog_uri, namespace, table_name) RETURNS SETOF
 * record. Resolve the table's current metadata-location through the catalog,
 * then read it through the very same path as iceberg_scan (a resolved remote
 * location reads because the reader is remote-capable). Superuser /
 * pg_read_server_files, materialize-mode SRF, column definition list required.
 */
Datum
pgcolumnar_iceberg_rest_scan(PG_FUNCTION_ARGS)
{
	char	   *arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	char	   *ns = text_to_cstring(PG_GETARG_TEXT_PP(1));
	char	   *table = text_to_cstring(PG_GETARG_TEXT_PP(2));
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	TupleDesc	tupdesc;
	char	   *catalog_uri;
	char	   *token;
	char	   *warehouse;
	Jsonb	   *lt;
	char	   *loc;
	PgColumnarObjStoreConfig *cfg;

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a REST catalog")));
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

	rest_resolve(arg0, &catalog_uri, &token, &warehouse);
	/* one loadTable: the metadata-location AND the vended storage creds (if any) */
	lt = rest_load_table_doc(catalog_uri, ns, table, token, warehouse, &loc);
	cfg = rest_vended_cfg(&lt->root, loc);
	/*
	 * A metadata-location is a URI. The reader's remote path handles s3:// and
	 * http(s)://; a file:// location is a local path, and the local read path
	 * opens a bare filename, so strip the scheme here.
	 */
	if (pg_strncasecmp(loc, "file://", 7) == 0)
		loc += 7;
	PgColumnarIcebergScanInto(loc, tupdesc, rsinfo, cfg);
	return (Datum) 0;
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_namespaces);

/*
 * SQL: iceberg_rest_namespaces(catalog_uri) RETURNS SETOF text. One row per
 * namespace; a multi-level namespace is dot-joined.
 */
Datum
pgcolumnar_iceberg_rest_namespaces(PG_FUNCTION_ARGS)
{
	char	   *arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Tuplestorestate *ts;
	TupleDesc	td;
	char	   *catalog_uri;
	char	   *token;
	Jsonb	   *doc;
	JsonbValue	vbuf;
	JsonbValue *nsv;
	uint32		n;
	uint32		i;

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a REST catalog")));
	ts = rest_text_srf_setup(rsinfo, &td);

	rest_resolve(arg0, &catalog_uri, &token, NULL);
	doc = rest_get_json(catalog_uri, "/v1/namespaces", "namespaces", token);
	if (!JsonContainerIsObject(&doc->root))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog namespaces response is not a JSON object")));
	nsv = getKeyJsonValueFromContainer(&doc->root, "namespaces", 10, &vbuf);
	if (nsv == NULL || nsv->type != jbvBinary ||
		!JsonContainerIsArray(nsv->val.binary.data))
		return (Datum) 0;		/* no namespaces */

	/* each element is itself an array of level strings; dot-join them */
	n = JsonContainerSize(nsv->val.binary.data);
	for (i = 0; i < n; i++)
	{
		JsonbValue *lv = getIthJsonbValueFromContainer(nsv->val.binary.data, i);
		StringInfoData name;
		uint32		nlev;
		uint32		j;
		Datum		values[1];
		bool		nulls[1] = {false};

		if (lv == NULL || lv->type != jbvBinary ||
			!JsonContainerIsArray(lv->val.binary.data))
			continue;
		initStringInfo(&name);
		nlev = JsonContainerSize(lv->val.binary.data);
		for (j = 0; j < nlev; j++)
		{
			JsonbValue *pv = getIthJsonbValueFromContainer(lv->val.binary.data, j);

			if (pv == NULL || pv->type != jbvString)
				continue;
			if (name.len > 0)
				appendStringInfoChar(&name, '.');
			appendBinaryStringInfo(&name, pv->val.string.val, pv->val.string.len);
		}
		values[0] = PointerGetDatum(cstring_to_text_with_len(name.data, name.len));
		tuplestore_putvalues(ts, td, values, nulls);
		pfree(name.data);
	}
	return (Datum) 0;
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_rest_tables);

/*
 * SQL: iceberg_rest_tables(catalog_uri, namespace) RETURNS SETOF text. One row
 * per table name in the namespace.
 */
Datum
pgcolumnar_iceberg_rest_tables(PG_FUNCTION_ARGS)
{
	char	   *arg0 = text_to_cstring(PG_GETARG_TEXT_PP(0));
	char	   *ns = text_to_cstring(PG_GETARG_TEXT_PP(1));
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Tuplestorestate *ts;
	TupleDesc	td;
	char	   *catalog_uri;
	char	   *token;
	StringInfoData rp;
	Jsonb	   *doc;
	JsonbValue	vbuf;
	JsonbValue *idv;
	uint32		n;
	uint32		i;

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a REST catalog")));
	ts = rest_text_srf_setup(rsinfo, &td);

	rest_resolve(arg0, &catalog_uri, &token, NULL);
	initStringInfo(&rp);
	appendStringInfoString(&rp, "/v1/namespaces/");
	rest_append_namespace(&rp, ns);
	appendStringInfoString(&rp, "/tables");
	doc = rest_get_json(catalog_uri, rp.data, "table list", token);
	if (!JsonContainerIsObject(&doc->root))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: the REST catalog table-list response is not a JSON object")));
	idv = getKeyJsonValueFromContainer(&doc->root, "identifiers", 11, &vbuf);
	if (idv == NULL || idv->type != jbvBinary ||
		!JsonContainerIsArray(idv->val.binary.data))
		return (Datum) 0;		/* no tables */

	n = JsonContainerSize(idv->val.binary.data);
	for (i = 0; i < n; i++)
	{
		JsonbValue *e = getIthJsonbValueFromContainer(idv->val.binary.data, i);
		JsonbValue	nbuf;
		JsonbValue *nm;
		Datum		values[1];
		bool		nulls[1] = {false};

		if (e == NULL || e->type != jbvBinary ||
			!JsonContainerIsObject(e->val.binary.data))
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

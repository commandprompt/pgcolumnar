/*-------------------------------------------------------------------------
 *
 * columnar_avro.c
 *		A targeted Avro object-container-file reader for Iceberg manifests
 *		(#388 step 1). See columnar_avro.h.
 *
 * It decodes the subset of Avro that Iceberg manifests use, against the schema
 * embedded in the file (the `avro.schema` header metadata), so a v3 manifest --
 * which adds fields -- reads structurally rather than as garbage. The reader
 * wears the columnar_thrift.c bounds discipline: a {buf,len,pos,error} cursor
 * with overflow-safe `n > len - pos` bounds, and check_stack_depth() in the
 * recursive schema and value descent, because this is a decoder over bytes an
 * outside writer produced.
 *
 * Written fresh for pgColumnar from the public Apache Avro specification.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <zlib.h>

#include "catalog/pg_authid_d.h"
#include "fmgr.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "storage/fd.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/jsonb.h"
#include "utils/tuplestore.h"

#include "columnar_avro.h"
#include "columnar_objstore.h"

/* Caps so a hostile manifest cannot exhaust memory or spin the backend. */
#define AV_MAX_FILE			((int64) 256 * 1024 * 1024)
#define AV_MAX_BLOCK		((int64) 256 * 1024 * 1024)	/* one block, decompressed */
#define AV_MAX_TOTAL		((int64) 1024 * 1024 * 1024)	/* all blocks, decompressed */
#define AV_MAX_OBJECTS		((int64) 50 * 1000 * 1000)
#define AV_MAX_SCHEMA		((int64) 4 * 1024 * 1024)
#define AV_SYNC_LEN			16

/* -------------------------------------------------------- bounded reader */

typedef struct AvReader
{
	const uint8 *buf;
	int64		len;
	int64		pos;
	bool		error;
} AvReader;

/* raw unsigned varint (LEB128), bounded, with an overflow guard */
static uint64
av_varint(AvReader *r)
{
	uint64		v = 0;
	int			shift = 0;

	while (r->pos < r->len)
	{
		uint8		b = r->buf[r->pos++];

		v |= (uint64) (b & 0x7f) << shift;
		if ((b & 0x80) == 0)
			return v;
		shift += 7;
		if (shift > 63)
			break;
	}
	r->error = true;
	return 0;
}

/* zigzag long/int */
static int64
av_long(AvReader *r)
{
	uint64		u = av_varint(r);

	return (int64) (u >> 1) ^ -(int64) (u & 1);
}

/* advance n bytes, bounds-checked (overflow-safe); returns the start or NULL */
static const uint8 *
av_take(AvReader *r, int64 n)
{
	const uint8 *p;

	if (r->error || n < 0 || n > r->len - r->pos)
	{
		r->error = true;
		return NULL;
	}
	p = r->buf + r->pos;
	r->pos += n;
	return p;
}

/* a length-prefixed bytes/string: pointer into the buffer + length */
static const uint8 *
av_bytes(AvReader *r, int64 *outlen)
{
	int64		n = av_long(r);
	const uint8 *p = av_take(r, n);

	*outlen = r->error ? 0 : n;
	return p;
}

/* a UTF-8 string as a palloc'd cstring, or NULL on error */
static char *
av_cstring(AvReader *r)
{
	int64		n;
	const uint8 *p = av_bytes(r, &n);

	if (r->error || p == NULL)
		return NULL;
	return pnstrdup((const char *) p, n);
}

/* --------------------------------------------------------- schema tree */

typedef enum AvKind
{
	AV_NULL, AV_BOOL, AV_INT, AV_LONG, AV_FLOAT, AV_DOUBLE,
	AV_BYTES, AV_STRING, AV_ENUM, AV_FIXED, AV_RECORD, AV_ARRAY, AV_MAP, AV_UNION
} AvKind;

typedef struct AvSchema AvSchema;
typedef struct AvField
{
	char	   *name;
	AvSchema   *type;
} AvField;

struct AvSchema
{
	AvKind		kind;
	int			n;				/* RECORD fields / UNION branches */
	AvField    *fields;			/* RECORD */
	AvSchema  **branch;			/* UNION */
	AvSchema   *items;			/* ARRAY items / MAP values */
	int			fixedsize;		/* FIXED */
};

static AvSchema *av_schema_build(JsonbValue *tv);

static AvSchema *
av_prim(AvKind k)
{
	AvSchema   *s = (AvSchema *) palloc0(sizeof(AvSchema));

	s->kind = k;
	return s;
}

/* a primitive Avro type name -> kind, or -1 for unknown */
static int
av_prim_kind(const char *name, int len)
{
	if (len == 4 && strncmp(name, "null", 4) == 0)
		return AV_NULL;
	if (len == 7 && strncmp(name, "boolean", 7) == 0)
		return AV_BOOL;
	if (len == 3 && strncmp(name, "int", 3) == 0)
		return AV_INT;
	if (len == 4 && strncmp(name, "long", 4) == 0)
		return AV_LONG;
	if (len == 5 && strncmp(name, "float", 5) == 0)
		return AV_FLOAT;
	if (len == 6 && strncmp(name, "double", 6) == 0)
		return AV_DOUBLE;
	if (len == 5 && strncmp(name, "bytes", 5) == 0)
		return AV_BYTES;
	if (len == 6 && strncmp(name, "string", 6) == 0)
		return AV_STRING;
	return -1;
}

/* JsonbValue for a container's object field by key, or NULL */
static JsonbValue *
av_jb_field(JsonbValue *obj, const char *key)
{
	/* A jsonb binary container is an object OR an array; getKeyJsonValueFromContainer
	 * asserts object-ness and walks an object pair-stride, so an array container
	 * (e.g. a "fields":[[...]] element) must be rejected here, not passed through. */
	if (obj->type != jbvBinary || !JsonContainerIsObject(obj->val.binary.data))
		return NULL;
	return getKeyJsonValueFromContainer(obj->val.binary.data, key,
										(int) strlen(key),
										palloc(sizeof(JsonbValue)));
}

static void
av_schema_err(void)
{
	ereport(ERROR,
			(errcode(ERRCODE_DATA_CORRUPTED),
			 errmsg("columnar: unsupported or malformed Avro schema in manifest")));
}

/* build the schema tree from a JSON "type" value (string | array | object) */
static AvSchema *
av_schema_build(JsonbValue *tv)
{
	check_stack_depth();

	if (tv->type == jbvString)
	{
		int			k = av_prim_kind(tv->val.string.val, tv->val.string.len);

		if (k < 0)
			av_schema_err();	/* a named-type reference we do not resolve */
		return av_prim((AvKind) k);
	}
	if (tv->type != jbvBinary)
		av_schema_err();

	/* a union is a JSON array of types */
	if (JsonContainerIsArray(tv->val.binary.data))
	{
		JsonbContainer *c = tv->val.binary.data;
		int			cnt = JsonContainerSize(c);
		AvSchema   *s = av_prim(AV_UNION);
		int			i;

		s->n = cnt;
		s->branch = (AvSchema **) palloc0(sizeof(AvSchema *) * Max(cnt, 1));
		for (i = 0; i < cnt; i++)
		{
			JsonbValue *e = getIthJsonbValueFromContainer(c, i);

			if (e == NULL)
				av_schema_err();
			s->branch[i] = av_schema_build(e);
		}
		return s;
	}

	/* otherwise an object: dispatch on its "type" */
	{
		JsonbValue *t = av_jb_field(tv, "type");
		int			k;

		if (t == NULL || t->type != jbvString)
			av_schema_err();
		k = av_prim_kind(t->val.string.val, t->val.string.len);
		if (k >= 0)
			return av_prim((AvKind) k);		/* {"type":"string", ...} */

		if (t->val.string.len == 6 && strncmp(t->val.string.val, "record", 6) == 0)
		{
			JsonbValue *fields = av_jb_field(tv, "fields");
			AvSchema   *s = av_prim(AV_RECORD);
			int			cnt,
						i;

			if (fields == NULL || fields->type != jbvBinary ||
				!JsonContainerIsArray(fields->val.binary.data))
				av_schema_err();
			cnt = JsonContainerSize(fields->val.binary.data);
			s->n = cnt;
			s->fields = (AvField *) palloc0(sizeof(AvField) * Max(cnt, 1));
			for (i = 0; i < cnt; i++)
			{
				JsonbValue *fe = getIthJsonbValueFromContainer(fields->val.binary.data, i);
				JsonbValue *nm,
						   *ty;

				if (fe == NULL)
					av_schema_err();
				nm = av_jb_field(fe, "name");
				ty = av_jb_field(fe, "type");
				if (nm == NULL || nm->type != jbvString || ty == NULL)
					av_schema_err();
				s->fields[i].name = pnstrdup(nm->val.string.val, nm->val.string.len);
				s->fields[i].type = av_schema_build(ty);
			}
			return s;
		}
		if (t->val.string.len == 5 && strncmp(t->val.string.val, "array", 5) == 0)
		{
			JsonbValue *items = av_jb_field(tv, "items");
			AvSchema   *s = av_prim(AV_ARRAY);

			if (items == NULL)
				av_schema_err();
			s->items = av_schema_build(items);
			return s;
		}
		if (t->val.string.len == 3 && strncmp(t->val.string.val, "map", 3) == 0)
		{
			JsonbValue *vals = av_jb_field(tv, "values");
			AvSchema   *s = av_prim(AV_MAP);

			if (vals == NULL)
				av_schema_err();
			s->items = av_schema_build(vals);
			return s;
		}
		if (t->val.string.len == 4 && strncmp(t->val.string.val, "enum", 4) == 0)
			return av_prim(AV_ENUM);
		if (t->val.string.len == 5 && strncmp(t->val.string.val, "fixed", 5) == 0)
		{
			JsonbValue *sz = av_jb_field(tv, "size");
			AvSchema   *s = av_prim(AV_FIXED);

			if (sz == NULL || sz->type != jbvNumeric)
				av_schema_err();
			s->fixedsize = DatumGetInt32(DirectFunctionCall1(numeric_int4,
															 NumericGetDatum(sz->val.numeric)));
			if (s->fixedsize < 0)
				av_schema_err();
			return s;
		}
	}
	av_schema_err();
	return NULL;				/* unreachable */
}

/* --------------------------------------------------- schema-driven decode */

/* decode-and-discard one value of schema `s`, advancing the cursor */
static void
av_skip(AvReader *r, AvSchema *s)
{
	int64		cnt;

	check_stack_depth();

	/*
	 * Per-element interrupt check. The AV_ARRAY/AV_MAP arms below check once per
	 * block, before the count is read, but a single block declares a count up to
	 * AV_MAX_OBJECTS (50,000,000) and AV_NULL consumes zero bytes, so the inner
	 * element loop can run 50M no-op skips with nothing to yield on. Because every
	 * element (and every record field) is one av_skip call, a check here bounds all
	 * of them uniformly. av_skip only ever skips unconsumed metadata fields, never
	 * the row data, so this is off the hot path.
	 */
	CHECK_FOR_INTERRUPTS();
	if (r->error)
		return;
	switch (s->kind)
	{
		case AV_NULL:
			break;
		case AV_BOOL:
			(void) av_take(r, 1);
			break;
		case AV_INT:
		case AV_LONG:
		case AV_ENUM:
			(void) av_long(r);
			break;
		case AV_FLOAT:
			(void) av_take(r, 4);
			break;
		case AV_DOUBLE:
			(void) av_take(r, 8);
			break;
		case AV_BYTES:
		case AV_STRING:
			{
				int64		n;

				(void) av_bytes(r, &n);
			}
			break;
		case AV_FIXED:
			(void) av_take(r, s->fixedsize);
			break;
		case AV_RECORD:
			{
				int			i;

				for (i = 0; i < s->n && !r->error; i++)
					av_skip(r, s->fields[i].type);
			}
			break;
		case AV_UNION:
			{
				int64		idx = av_long(r);

				if (r->error || idx < 0 || idx >= s->n)
				{
					r->error = true;
					return;
				}
				av_skip(r, s->branch[idx]);
			}
			break;
		case AV_ARRAY:
			while (!r->error)
			{
				int64		i;

				CHECK_FOR_INTERRUPTS();
				cnt = av_long(r);
				if (cnt == 0)
					break;
				if (cnt < 0)
				{
					/* also catches INT64_MIN, whose negation is signed-overflow UB */
					if (cnt < -AV_MAX_OBJECTS)
					{
						r->error = true;
						break;
					}
					cnt = -cnt;
					(void) av_long(r);	/* block byte size, unused */
				}
				if (cnt > AV_MAX_OBJECTS)
				{
					r->error = true;
					break;
				}
				for (i = 0; i < cnt && !r->error; i++)
					av_skip(r, s->items);
			}
			break;
		case AV_MAP:
			while (!r->error)
			{
				int64		i;

				CHECK_FOR_INTERRUPTS();
				cnt = av_long(r);
				if (cnt == 0)
					break;
				if (cnt < 0)
				{
					if (cnt < -AV_MAX_OBJECTS)	/* INT64_MIN negate is UB */
					{
						r->error = true;
						break;
					}
					cnt = -cnt;
					(void) av_long(r);
				}
				if (cnt > AV_MAX_OBJECTS)
				{
					r->error = true;
					break;
				}
				for (i = 0; i < cnt && !r->error; i++)
				{
					int64		kl;

					(void) av_bytes(r, &kl);	/* key string */
					av_skip(r, s->items);		/* value */
				}
			}
			break;
	}
}

/* if `s` is a union of [null, X], return X and report which branch is null;
 * otherwise return s. Used to read a possibly-nullable scalar field. */
static AvSchema *
av_unwrap_union(AvReader *r, AvSchema *s, bool *is_null)
{
	*is_null = false;
	if (s->kind != AV_UNION)
		return s;
	{
		int64		idx = av_long(r);

		if (r->error || idx < 0 || idx >= s->n)
		{
			r->error = true;
			return s;
		}
		if (s->branch[idx]->kind == AV_NULL)
			*is_null = true;
		return s->branch[idx];
	}
}

/*
 * Decode a partition struct (a record of scalar-ish fields) in ONE pass into
 * both the display string (name=value,...) the lister surfaces AND a typed cell
 * per field for exact-equality comparison (partition-scoped equality deletes).
 * The cells are stored on the entry; the display string is returned.
 */
static char *
av_decode_partition(AvReader *r, AvSchema *s, PgColumnarAvroManifestEntry *e)
{
	StringInfoData out;
	int			i;

	if (s->kind != AV_RECORD)
	{
		av_skip(r, s);
		return NULL;
	}
	e->part_cells = (PgColumnarAvroPartCell *)
		palloc0(sizeof(PgColumnarAvroPartCell) * Max(s->n, 1));
	e->npart_cells = s->n;
	initStringInfo(&out);
	for (i = 0; i < s->n && !r->error; i++)
	{
		PgColumnarAvroPartCell *c = &e->part_cells[i];
		bool		isnull;
		AvSchema   *t = av_unwrap_union(r, s->fields[i].type, &isnull);
		char	   *disp;

		if (i > 0)
			appendStringInfoChar(&out, ',');
		if (r->error)
			break;
		if (isnull)
		{
			c->isnull = true;
			c->comparable = true;	/* null compares equal only to null */
			appendStringInfo(&out, "%s=", s->fields[i].name);
			continue;
		}
		switch (t->kind)
		{
			case AV_INT:
			case AV_LONG:
				c->ival = av_long(r);
				c->comparable = true;
				disp = psprintf(INT64_FORMAT, c->ival);
				break;
			case AV_BOOL:
				{
					const uint8 *p = av_take(r, 1);

					c->ival = (p && *p) ? 1 : 0;
					c->comparable = true;
					disp = pstrdup(c->ival ? "true" : "false");
					break;
				}
			case AV_STRING:
			case AV_BYTES:
				{
					int64		n;
					const uint8 *p = av_bytes(r, &n);

					if (r->error || p == NULL)
					{
						disp = pstrdup("");
						break;
					}
					c->is_bytes = true;
					c->comparable = true;
					c->bytes = (char *) palloc(n);
					memcpy(c->bytes, p, n);
					c->blen = (int) n;
					disp = pnstrdup((const char *) p, n);
					break;
				}
			default:
				/* float/double/fixed/other: cannot compare for exact equality */
				av_skip(r, t);
				c->comparable = false;
				disp = pstrdup("?");
				break;
		}
		appendStringInfo(&out, "%s=%s", s->fields[i].name, disp ? disp : "");
	}
	return out.data;
}

/* read a nullable/plain string field */
static char *
av_read_string(AvReader *r, AvSchema *s)
{
	bool		isnull;
	AvSchema   *t = av_unwrap_union(r, s, &isnull);

	if (r->error || isnull)
		return NULL;
	if (t->kind != AV_STRING && t->kind != AV_BYTES)
	{
		av_skip(r, t);
		return NULL;
	}
	return av_cstring(r);
}

/* read a nullable/plain int or long field */
static int64
av_read_long(AvReader *r, AvSchema *s, bool *have)
{
	bool		isnull;
	AvSchema   *t = av_unwrap_union(r, s, &isnull);

	*have = false;
	if (r->error || isnull)
		return 0;
	if (t->kind != AV_INT && t->kind != AV_LONG)
	{
		av_skip(r, t);
		return 0;
	}
	*have = true;
	return av_long(r);
}

/*
 * read an int field kept as int32 (an enum: content, status). av_read_long
 * returns the whole varint, and a plain cast wrapped 4294967296 to 0, a valid
 * value that then passed the reader's range check (#1823). A value outside
 * int32 is refused here as corrupt metadata (XX001), naming the value and,
 * when it is already decoded, the path. `what` is "manifest entry" or
 * "manifest", as in the reader's own range-check messages. `havep`, when not
 * NULL, says whether the field was present (partition_spec_id keeps that; a
 * manifest list's partition_spec_id at 4294967296 read as spec 0, #1878).
 */
static int32
av_read_int32_field(AvReader *r, AvSchema *s, const char *what,
					const char *path, const char *field, bool *havep)
{
	bool		have;
	int64		v = av_read_long(r, s, &have);

	if (havep != NULL)
		*havep = have;

	if (v < PG_INT32_MIN || v > PG_INT32_MAX)
	{
		if (path != NULL)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("iceberg: %s \"%s\" has invalid %s %lld",
							what, path, field, (long long) v)));
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("iceberg: %s has invalid %s %lld",
						what, field, (long long) v)));
	}
	return (int32) v;
}

/* a data_file's equality_ids names a handful of columns; anything larger than
 * this is a hostile manifest, not a real table */
#define AV_MAX_EQUALITY_IDS 10000

/* read a nullable array<int> field (equality_ids) into a palloc'd int32 array.
 * The array block protocol (negative count = block byte size follows) and its
 * caps mirror av_skip's AV_ARRAY arm. Null, absent, or mistyped decodes as an
 * empty array (*nout 0), which the caller treats as "not present". */
static void
av_read_int_array(AvReader *r, AvSchema *s, int32 **out, int *nout)
{
	bool		isnull;
	AvSchema   *t = av_unwrap_union(r, s, &isnull);
	int32	   *vals = NULL;
	int			n = 0;
	int			cap = 0;

	*out = NULL;
	*nout = 0;
	if (r->error || isnull)
		return;
	if (t->kind != AV_ARRAY ||
		(t->items->kind != AV_INT && t->items->kind != AV_LONG))
	{
		av_skip(r, t);
		return;
	}
	while (!r->error)
	{
		int64		cnt;
		int64		i;

		CHECK_FOR_INTERRUPTS();
		cnt = av_long(r);
		if (cnt == 0)
			break;
		if (cnt < 0)
		{
			/* also catches INT64_MIN, whose negation is signed-overflow UB */
			if (cnt < -AV_MAX_EQUALITY_IDS)
			{
				r->error = true;
				break;
			}
			cnt = -cnt;
			(void) av_long(r);	/* block byte size, unused */
		}
		if (cnt > AV_MAX_EQUALITY_IDS || n + cnt > AV_MAX_EQUALITY_IDS)
		{
			r->error = true;
			break;
		}
		for (i = 0; i < cnt && !r->error; i++)
		{
			int64		v = av_long(r);

			/* a field id beyond int32 is malformed; truncating it would alias
			 * a different (possibly real) column and key the delete wrongly */
			if (v < 0 || v > PG_INT32_MAX)
			{
				r->error = true;
				break;
			}
			if (n == cap)
			{
				cap = cap ? cap * 2 : 8;
				vals = (vals == NULL)
					? (int32 *) palloc(cap * sizeof(int32))
					: (int32 *) repalloc(vals, cap * sizeof(int32));
			}
			vals[n++] = (int32) v;
		}
	}
	if (r->error)
		return;
	*out = vals;
	*nout = n;
}

#define AV_MAX_BOUNDS 8192
#define AV_MAX_BOUND_LEN 65536

/*
 * Decode a lower_bounds / upper_bounds field: Iceberg's map<int,bytes> is an
 * Avro array of {key:int, value:bytes} records (Avro maps need string keys), so
 * read it as a block array of key/value records, keeping the field id and a copy
 * of the single-value binary. Absent, null, or mistyped decodes as empty.
 */
static void
av_read_bound_map(AvReader *r, AvSchema *s,
				  PgColumnarAvroBound **out, int *nout)
{
	bool		isnull;
	AvSchema   *t = av_unwrap_union(r, s, &isnull);
	AvSchema   *rec;
	PgColumnarAvroBound *b = NULL;
	int			n = 0;
	int			cap = 0;

	*out = NULL;
	*nout = 0;
	if (r->error || isnull)
		return;
	if (t->kind != AV_ARRAY || t->items->kind != AV_RECORD)
	{
		av_skip(r, t);
		return;
	}
	rec = t->items;

	while (!r->error)
	{
		int64		cnt;
		int64		i;

		CHECK_FOR_INTERRUPTS();
		cnt = av_long(r);
		if (cnt == 0)
			break;
		if (cnt < 0)
		{
			if (cnt < -AV_MAX_BOUNDS)
			{
				r->error = true;
				break;
			}
			cnt = -cnt;
			(void) av_long(r);	/* block byte size, unused */
		}
		if (cnt > AV_MAX_BOUNDS || n + cnt > AV_MAX_BOUNDS)
		{
			r->error = true;
			break;
		}
		for (i = 0; i < cnt && !r->error; i++)
		{
			int32		key = 0;
			char	   *val = NULL;
			int			vlen = 0;
			bool		haskey = false;
			bool		hasval = false;
			int			j;

			for (j = 0; j < rec->n && !r->error; j++)
			{
				const char *fn = rec->fields[j].name;
				AvSchema   *fty = rec->fields[j].type;

				if (strcmp(fn, "key") == 0)
				{
					bool		have;
					int64		k = av_read_long(r, fty, &have);

					if (r->error || k < PG_INT32_MIN || k > PG_INT32_MAX)
					{
						r->error = true;
						break;
					}
					key = (int32) k;
					haskey = true;
				}
				else if (strcmp(fn, "value") == 0)
				{
					bool		vnull;
					AvSchema   *vt = av_unwrap_union(r, fty, &vnull);

					if (r->error)
						break;
					if (vnull)
						continue;
					if (vt->kind != AV_BYTES && vt->kind != AV_STRING)
					{
						av_skip(r, vt);
						continue;
					}
					{
						int64		len = av_long(r);
						const uint8 *p;

						if (r->error || len < 0 || len > AV_MAX_BOUND_LEN)
						{
							r->error = true;
							break;
						}
						p = av_take(r, len);
						if (r->error)
							break;
						val = (char *) palloc(Max(len, 1));
						if (len > 0)
							memcpy(val, p, len);
						vlen = (int) len;
						hasval = true;
					}
				}
				else
					av_skip(r, fty);
			}
			if (r->error)
				break;
			if (haskey && hasval)
			{
				if (n == cap)
				{
					cap = cap ? cap * 2 : 8;
					b = (b == NULL)
						? (PgColumnarAvroBound *) palloc(cap * sizeof(*b))
						: (PgColumnarAvroBound *) repalloc(b, cap * sizeof(*b));
				}
				b[n].field_id = key;
				b[n].bytes = val;
				b[n].blen = vlen;
				n++;
			}
		}
	}
	if (r->error)
		return;
	*out = b;
	*nout = n;
}

/* decode one data_file record into the projected fields */
static void
av_decode_data_file(AvReader *r, AvSchema *s, PgColumnarAvroManifestEntry *e)
{
	int			i;

	if (s->kind != AV_RECORD)
	{
		r->error = true;
		return;
	}
	for (i = 0; i < s->n && !r->error; i++)
	{
		const char *nm = s->fields[i].name;
		AvSchema   *ft = s->fields[i].type;
		bool		have;

		if (strcmp(nm, "file_path") == 0)
			e->file_path = av_read_string(r, ft);
		else if (strcmp(nm, "file_format") == 0)
			e->file_format = av_read_string(r, ft);
		else if (strcmp(nm, "content") == 0)
			e->content = av_read_int32_field(r, ft, "manifest entry",
											 e->file_path, "content", NULL);
		else if (strcmp(nm, "record_count") == 0)
			e->record_count = av_read_long(r, ft, &have);
		else if (strcmp(nm, "file_size_in_bytes") == 0)
			e->file_size_in_bytes = av_read_long(r, ft, &have);
		else if (strcmp(nm, "partition") == 0)
		{
			bool		isnull;
			AvSchema   *pt = av_unwrap_union(r, ft, &isnull);

			if (!r->error && !isnull)
				e->partition = av_decode_partition(r, pt, e);
		}
		else if (strcmp(nm, "equality_ids") == 0)
			av_read_int_array(r, ft, &e->equality_ids, &e->nequality_ids);
		else if (strcmp(nm, "lower_bounds") == 0)
			av_read_bound_map(r, ft, &e->lower_bounds, &e->nlower);
		else if (strcmp(nm, "upper_bounds") == 0)
			av_read_bound_map(r, ft, &e->upper_bounds, &e->nupper);
		else if (strcmp(nm, "referenced_data_file") == 0)
			e->referenced_data_file = av_read_string(r, ft);
		else if (strcmp(nm, "content_offset") == 0)
			e->content_offset = av_read_long(r, ft, &e->has_content_offset);
		else if (strcmp(nm, "content_size_in_bytes") == 0)
			e->content_size_in_bytes = av_read_long(r, ft, &e->has_content_size);
		else
			av_skip(r, ft);		/* every other field: advance past it */
	}
}

/* decode one manifest_entry record into an entry */
static void
av_decode_entry(AvReader *r, AvSchema *s, PgColumnarAvroManifestEntry *e)
{
	int			i;

	if (s->kind != AV_RECORD)
	{
		r->error = true;
		return;
	}
	for (i = 0; i < s->n && !r->error; i++)
	{
		const char *nm = s->fields[i].name;
		AvSchema   *ft = s->fields[i].type;
		bool		have;

		if (strcmp(nm, "status") == 0)
			e->status = av_read_int32_field(r, ft, "manifest entry",
											e->file_path, "status", NULL);
		/* the data sequence number; "sequence_number" in v2 manifests as
		 * pyiceberg writes them, "data_sequence_number" in the newer spec name.
		 * Nullable: null means inherit the manifest's sequence number. */
		else if (strcmp(nm, "sequence_number") == 0 ||
				 strcmp(nm, "data_sequence_number") == 0)
		{
			e->has_sequence_field = true;
			e->sequence_number = av_read_long(r, ft, &have);
			e->has_sequence_number = have;
		}
		else if (strcmp(nm, "data_file") == 0)
		{
			bool		isnull;
			AvSchema   *dt = av_unwrap_union(r, ft, &isnull);

			if (!r->error && !isnull)
				av_decode_data_file(r, dt, e);
		}
		else
			av_skip(r, ft);
	}
}

/* decode one manifest_file record (a manifest-list entry) into a projection */
static void
av_decode_manifest_file(AvReader *r, AvSchema *s, PgColumnarAvroManifestFile *e)
{
	int			i;

	if (s->kind != AV_RECORD)
	{
		r->error = true;
		return;
	}
	for (i = 0; i < s->n && !r->error; i++)
	{
		const char *nm = s->fields[i].name;
		AvSchema   *ft = s->fields[i].type;
		bool		have;

		if (strcmp(nm, "manifest_path") == 0)
			e->manifest_path = av_read_string(r, ft);
		else if (strcmp(nm, "manifest_length") == 0)
			e->manifest_length = av_read_long(r, ft, &have);
		else if (strcmp(nm, "partition_spec_id") == 0)
		{
			e->partition_spec_id = av_read_int32_field(r, ft, "manifest",
													   e->manifest_path,
													   "partition_spec_id",
													   &have);
			e->has_partition_spec_id = have;
		}
		else if (strcmp(nm, "content") == 0)
			e->content = av_read_int32_field(r, ft, "manifest",
											 e->manifest_path, "content", NULL);
		else if (strcmp(nm, "sequence_number") == 0)
		{
			e->sequence_number = av_read_long(r, ft, &have);
			e->has_sequence_number = have;
		}
		else if (strcmp(nm, "min_sequence_number") == 0)
		{
			e->min_sequence_number = av_read_long(r, ft, &have);
			e->has_min_sequence_number = have;
		}
		else if (strcmp(nm, "added_snapshot_id") == 0)
			e->added_snapshot_id = av_read_long(r, ft, &have);
		else if (strcmp(nm, "added_files_count") == 0)
			e->added_files_count = (int32) av_read_long(r, ft, &have);
		else if (strcmp(nm, "existing_files_count") == 0)
			e->existing_files_count = (int32) av_read_long(r, ft, &have);
		else if (strcmp(nm, "deleted_files_count") == 0)
			e->deleted_files_count = (int32) av_read_long(r, ft, &have);
		else if (strcmp(nm, "added_rows_count") == 0)
			e->added_rows_count = av_read_long(r, ft, &have);
		else if (strcmp(nm, "existing_rows_count") == 0)
			e->existing_rows_count = av_read_long(r, ft, &have);
		else if (strcmp(nm, "deleted_rows_count") == 0)
			e->deleted_rows_count = av_read_long(r, ft, &have);
		else
			av_skip(r, ft);
	}
}

/* ---------------------------------------------------- object-container file */

/* decode the header metadata map (Avro map<string,bytes>) into schema + codec */
static void
av_read_metadata(AvReader *r, char **schema_json, char **codec)
{
	*schema_json = NULL;
	*codec = NULL;
	while (!r->error)
	{
		int64		cnt = av_long(r);
		int64		i;

		CHECK_FOR_INTERRUPTS();
		if (cnt == 0)
			break;
		if (cnt < 0)
		{
			if (cnt < -AV_MAX_OBJECTS)	/* INT64_MIN negate is UB */
			{
				r->error = true;
				return;
			}
			cnt = -cnt;
			(void) av_long(r);	/* block byte size */
		}
		if (cnt > AV_MAX_OBJECTS)
		{
			r->error = true;
			return;
		}
		for (i = 0; i < cnt && !r->error; i++)
		{
			char	   *key = av_cstring(r);
			int64		vlen;
			const uint8 *vp = av_bytes(r, &vlen);

			if (r->error || key == NULL)
				return;
			if (strcmp(key, "avro.schema") == 0)
			{
				if (vlen > AV_MAX_SCHEMA)
				{
					r->error = true;
					return;
				}
				*schema_json = pnstrdup((const char *) vp, vlen);
			}
			else if (strcmp(key, "avro.codec") == 0)
				*codec = pnstrdup((const char *) vp, vlen);
		}
	}
}

/* raw-DEFLATE (RFC 1951) inflate of the Avro "deflate" codec into a palloc'd
 * buffer; bounded by AV_MAX_BLOCK. */
static uint8 *
av_inflate(const uint8 *in, int64 inlen, int64 *outlen)
{
	z_stream	zs;
	int64		cap = Max(inlen * 4, 4096);
	uint8	   *out;
	int			rc;

	if (cap > AV_MAX_BLOCK)
		cap = AV_MAX_BLOCK;
	out = (uint8 *) palloc(cap);
	memset(&zs, 0, sizeof(zs));
	if (inflateInit2(&zs, -15) != Z_OK)		/* raw deflate, no zlib header */
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("columnar: could not initialize the Avro deflate codec")));
	zs.next_in = (Bytef *) in;
	zs.avail_in = (uInt) inlen;
	zs.next_out = out;
	zs.avail_out = (uInt) cap;
	for (;;)
	{
		rc = inflate(&zs, Z_NO_FLUSH);
		if (rc == Z_STREAM_END)
			break;
		if (rc != Z_OK && rc != Z_BUF_ERROR)
		{
			inflateEnd(&zs);
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("columnar: corrupt Avro deflate block")));
		}
		if (zs.avail_out == 0)
		{
			int64		used = cap;

			if (cap >= AV_MAX_BLOCK)
			{
				inflateEnd(&zs);
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("columnar: Avro block exceeds %lld bytes",
								(long long) AV_MAX_BLOCK)));
			}
			cap = Min(cap * 2, AV_MAX_BLOCK);
			out = (uint8 *) repalloc(out, cap);
			zs.next_out = out + used;
			zs.avail_out = (uInt) (cap - used);
		}
		else if (rc == Z_BUF_ERROR)
		{
			/* no progress possible and not at stream end: truncated input */
			inflateEnd(&zs);
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("columnar: truncated Avro deflate block")));
		}
	}
	*outlen = (int64) zs.total_out;
	inflateEnd(&zs);
	return out;
}

/*
 * Read an Avro object-container file, decoding each record with `decode` into a
 * fresh element of an array of `elemsize`-byte elements. Shared by the manifest
 * and manifest-list readers: only the per-record projection differs; the OCF
 * framing, deflate codec, caps and sync-marker checks are identical and live
 * here, in one place. Returns the palloc'd array; *nout is the count.
 */
static void *
av_read_ocf(const uint8 *buf, int64 len, Size elemsize,
			void (*decode) (AvReader *br, AvSchema *schema, void *elem),
			int *nout)
{
	AvReader	r = {buf, len, 0, false};
	char	   *schema_json;
	char	   *codec;
	const uint8 *sync;
	uint8		syncmark[AV_SYNC_LEN];
	AvSchema   *schema;
	JsonbValue	rootv;
	Jsonb	   *j;
	char	   *out = NULL;
	int			nalloc = 0;
	int			n = 0;

	/* magic Obj\x01 */
	{
		const uint8 *m = av_take(&r, 4);

		if (m == NULL || m[0] != 'O' || m[1] != 'b' || m[2] != 'j' || m[3] != 1)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("columnar: not an Avro object-container file (bad magic)")));
	}

	av_read_metadata(&r, &schema_json, &codec);
	sync = av_take(&r, AV_SYNC_LEN);
	if (r.error || schema_json == NULL || sync == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("columnar: malformed Avro header")));
	memcpy(syncmark, sync, AV_SYNC_LEN);

	if (codec != NULL && strcmp(codec, "null") != 0 &&
		strcmp(codec, "deflate") != 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED)),
				errmsg("columnar: Avro codec \"%s\" is not supported yet", codec));

	/* parse the embedded schema (jsonb_in raises on bad JSON, a clean ERROR) */
	j = DatumGetJsonbP(DirectFunctionCall1(jsonb_in, CStringGetDatum(schema_json)));
	rootv.type = jbvBinary;
	rootv.val.binary.data = &j->root;
	rootv.val.binary.len = VARSIZE(j);
	schema = av_schema_build(&rootv);
	if (schema->kind != AV_RECORD)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("columnar: Avro manifest schema is not a record")));

	/* data blocks */
	{
	int64		total_dec = 0;		/* decompressed bytes across all blocks */

	while (r.pos < r.len && !r.error)
	{
		int64		count = av_long(&r);
		int64		bsize = av_long(&r);
		const uint8 *block = av_take(&r, bsize);
		const uint8 *bsync;
		uint8	   *dfree = NULL;	/* this block's decompressed buffer, freed below */
		AvReader	br;
		int64		i;

		CHECK_FOR_INTERRUPTS();

		if (r.error || block == NULL || count < 0 || count > AV_MAX_OBJECTS)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("columnar: malformed Avro data block")));

		if (codec != NULL && strcmp(codec, "deflate") == 0)
		{
			int64		dlen;

			dfree = av_inflate(block, bsize, &dlen);
			/*
			 * A cumulative decompressed-bytes cap. A count==0 block decodes no
			 * entries yet still inflates, so it bypasses the per-entry cap; many
			 * such blocks in a small compressed file are a deflate zip bomb. The
			 * buffer is freed at the end of this iteration, so peak retained
			 * memory is one block, but the total inflation still needs a bound.
			 */
			total_dec += dlen;
			if (total_dec > AV_MAX_TOTAL)
			{
				pfree(dfree);
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("columnar: Avro manifest decompresses past %lld bytes",
								(long long) AV_MAX_TOTAL)));
			}
			br.buf = dfree;
			br.len = dlen;
		}
		else
		{
			br.buf = block;
			br.len = bsize;
		}
		br.pos = 0;
		br.error = false;

		if ((int64) n + count > AV_MAX_OBJECTS)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("columnar: Avro manifest exceeds %lld entries",
							(long long) AV_MAX_OBJECTS)));
		if (n + count > nalloc)
		{
			nalloc = (int) Max((int64) (n + count), (int64) 16);
			out = out == NULL
				? (char *) palloc0(elemsize * nalloc)
				: (char *) repalloc(out, elemsize * nalloc);
			memset(out + (Size) n * elemsize, 0, (Size) (nalloc - n) * elemsize);
		}
		for (i = 0; i < count; i++)
		{
			if ((i & 0xFFF) == 0)
				CHECK_FOR_INTERRUPTS();
			decode(&br, schema, out + (Size) n * elemsize);
			if (br.error)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("columnar: corrupt Avro manifest entry")));
			n++;
		}

		if (dfree != NULL)
			pfree(dfree);		/* peak retained memory stays one block */

		/* the block's trailing sync marker must match the header's */
		bsync = av_take(&r, AV_SYNC_LEN);
		if (r.error || bsync == NULL ||
			memcmp(bsync, syncmark, AV_SYNC_LEN) != 0)
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("columnar: Avro block sync marker mismatch")));
	}
	}

	*nout = n;
	return out;
}

/* thin callbacks so av_read_ocf can decode either record type */
static void
av_entry_cb(AvReader *br, AvSchema *s, void *e)
{
	av_decode_entry(br, s, (PgColumnarAvroManifestEntry *) e);
}

static void
av_manifest_file_cb(AvReader *br, AvSchema *s, void *e)
{
	av_decode_manifest_file(br, s, (PgColumnarAvroManifestFile *) e);
}

PgColumnarAvroManifestEntry *
PgColumnarAvroReadManifest(const uint8 *buf, int64 len, int *nout)
{
	return (PgColumnarAvroManifestEntry *)
		av_read_ocf(buf, len, sizeof(PgColumnarAvroManifestEntry),
					av_entry_cb, nout);
}

PgColumnarAvroManifestFile *
PgColumnarAvroReadManifestList(const uint8 *buf, int64 len, int *nout)
{
	return (PgColumnarAvroManifestFile *)
		av_read_ocf(buf, len, sizeof(PgColumnarAvroManifestFile),
					av_manifest_file_cb, nout);
}

/* ------------------------------------------------------ SQL introspection */

/* slurp a whole (small) local Avro file into a palloc'd buffer */
static uint8 *
av_slurp_file(const char *path, int64 *outlen)
{
	uint8	   *buf;

	/* Race-free local open (rejects a FIFO without a stat-before-open window). */
	buf = (uint8 *) PgColumnarSlurpLocalRegularFile(path, AV_MAX_FILE,
													"Avro file", outlen);
	return buf;
}

/* the shared SRF preamble: privilege gate, result-set checks, tupdesc, and the
 * per-query-context tuplestore (the per-call context frees before the executor
 * drains it -- ASAN caught a heap-use-after-free in tuplestore_gettuple). */
static Tuplestorestate *
av_srf_begin(FunctionCallInfo fcinfo, TupleDesc *tupdesc)
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

PG_FUNCTION_INFO_V1(pgcolumnar_read_avro_manifest);

Datum
pgcolumnar_read_avro_manifest(PG_FUNCTION_ARGS)
{
	char	   *path = text_to_cstring(PG_GETARG_TEXT_PP(0));
	TupleDesc	tupdesc;
	Tuplestorestate *tupstore;
	int64		flen;
	uint8	   *buf;
	PgColumnarAvroManifestEntry *entries;
	int			n,
				i;

	tupstore = av_srf_begin(fcinfo, &tupdesc);
	buf = av_slurp_file(path, &flen);
	entries = PgColumnarAvroReadManifest(buf, flen, &n);

	for (i = 0; i < n; i++)
	{
		Datum		values[8];
		bool		nulls[8] = {false, false, false, false, false, false, false, false};
		PgColumnarAvroManifestEntry *e = &entries[i];

		values[0] = Int32GetDatum(e->status);
		values[1] = Int32GetDatum(e->content);
		if (e->file_path)
			values[2] = CStringGetTextDatum(e->file_path);
		else
			nulls[2] = true;
		if (e->file_format)
			values[3] = CStringGetTextDatum(e->file_format);
		else
			nulls[3] = true;
		values[4] = Int64GetDatum(e->record_count);
		values[5] = Int64GetDatum(e->file_size_in_bytes);
		if (e->partition)
			values[6] = CStringGetTextDatum(e->partition);
		else
			nulls[6] = true;
		/* the data sequence number, NULL when the entry inherits it */
		if (e->has_sequence_number)
			values[7] = Int64GetDatum(e->sequence_number);
		else
			nulls[7] = true;
		tuplestore_putvalues(tupstore, tupdesc, values, nulls);
	}
	return (Datum) 0;
}

PG_FUNCTION_INFO_V1(pgcolumnar_read_manifest_list);

Datum
pgcolumnar_read_manifest_list(PG_FUNCTION_ARGS)
{
	char	   *path = text_to_cstring(PG_GETARG_TEXT_PP(0));
	TupleDesc	tupdesc;
	Tuplestorestate *tupstore;
	int64		flen;
	uint8	   *buf;
	PgColumnarAvroManifestFile *files;
	int			n,
				i;

	tupstore = av_srf_begin(fcinfo, &tupdesc);
	buf = av_slurp_file(path, &flen);
	files = PgColumnarAvroReadManifestList(buf, flen, &n);

	for (i = 0; i < n; i++)
	{
		Datum		values[13];
		bool		nulls[13] = {false};
		PgColumnarAvroManifestFile *e = &files[i];

		if (e->manifest_path)
			values[0] = CStringGetTextDatum(e->manifest_path);
		else
			nulls[0] = true;
		values[1] = Int64GetDatum(e->manifest_length);
		values[2] = Int32GetDatum(e->content);
		values[3] = Int32GetDatum(e->partition_spec_id);
		values[4] = Int32GetDatum(e->added_files_count);
		values[5] = Int32GetDatum(e->existing_files_count);
		values[6] = Int32GetDatum(e->deleted_files_count);
		values[7] = Int64GetDatum(e->added_rows_count);
		values[8] = Int64GetDatum(e->existing_rows_count);
		values[9] = Int64GetDatum(e->deleted_rows_count);
		if (e->has_sequence_number)
			values[10] = Int64GetDatum(e->sequence_number);
		else
			nulls[10] = true;
		if (e->has_min_sequence_number)
			values[11] = Int64GetDatum(e->min_sequence_number);
		else
			nulls[11] = true;
		values[12] = Int64GetDatum(e->added_snapshot_id);
		tuplestore_putvalues(tupstore, tupdesc, values, nulls);
	}
	return (Datum) 0;
}

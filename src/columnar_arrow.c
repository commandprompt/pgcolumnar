/*-------------------------------------------------------------------------
 *
 * pgcolumnar_arrow.c
 *		Arrow IPC stream export for pgColumnar (gap 27, piece 1).
 *
 *		pgcolumnar.export_arrow(rel regclass, path text) writes a columnar table
 *		to an Apache Arrow IPC *stream* file: a Schema message, one RecordBatch
 *		message per ARROW_BATCH_ROWS rows, and an end-of-stream marker. The
 *		writer is self-contained -- it hand-builds the FlatBuffers metadata and
 *		the record-batch body buffers, so there is no libarrow/libparquet build
 *		or run-time dependency. Rows are read in physical order via the scalar
 *		reader; deleted rows are skipped by the reader.
 *
 *		Type mapping: int2/4/8, float4/8, bool, text/varchar (Utf8), bytea
 *		(Binary), date/time/timestamp/timestamptz, uuid, numeric, and json/jsonb;
 *		1-D arrays (List) and composites (Struct) are also exported. A scalar type
 *		with no mapping is rejected. Little-endian hosts only (the Arrow body
 *		mirrors native scalar bytes).
 *
 * Independent MIT implementation built from the Apache Arrow columnar format
 * and IPC specifications (Schema.fbs, Message.fbs, encapsulated message format)
 * and the public PostgreSQL API only.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"
#include "columnar_flatbuffers.h"
#include "columnar_objstore.h"
#include "columnar_sink.h"

#include "fmgr.h"
#include "funcapi.h"
#include "access/htup_details.h"
#include "access/relation.h"
#include "access/table.h"
#include "access/tableam.h"
#include "access/xact.h"
#include "catalog/pg_type.h"
#include "common/int.h"
#include "executor/tuptable.h"
#include "lib/stringinfo.h"
#include "catalog/pg_authid_d.h"
#include "miscadmin.h"
#include "utils/acl.h"
#include "storage/fd.h"
#include "utils/builtins.h"
#include "utils/date.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/numeric.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/array.h"
#include "utils/timestamp.h"
#include "utils/typcache.h"
#include "utils/uuid.h"

PG_FUNCTION_INFO_V1(pgcolumnar_export_arrow);
PG_FUNCTION_INFO_V1(pgcolumnar_import_arrow);

/* one RecordBatch per this many rows */
#define ARROW_BATCH_ROWS 16384

/* PostgreSQL epoch (2000-01-01) to Unix epoch (1970-01-01) offsets */
#define PG_TO_UNIX_DAYS		((int64) (POSTGRES_EPOCH_JDATE - UNIX_EPOCH_JDATE))
#define PG_TO_UNIX_USECS	(PG_TO_UNIX_DAYS * USECS_PER_DAY)

/* Arrow Type union tags (Schema.fbs) */
#define ARROW_TYPE_Int			2
#define ARROW_TYPE_FloatingPoint 3
#define ARROW_TYPE_Binary		4
#define ARROW_TYPE_Utf8			5
#define ARROW_TYPE_Bool			6
#define ARROW_TYPE_Decimal		7
#define ARROW_TYPE_Date			8
#define ARROW_TYPE_Time			9
#define ARROW_TYPE_Timestamp	10

/*
 * Arrow DateUnit and TimeUnit (Schema.fbs).
 *
 * A FlatBuffers writer omits any field equal to its schema default, and two of
 * these defaults are not zero: Date.unit defaults to MILLISECOND, and Time.unit
 * to MILLISECOND with bitWidth 32. pyarrow therefore writes date64 and time32[ms]
 * with no unit field at all. Reading an absent field as 0 is what made a date64
 * file decode as a day count (#864).
 */
#define ARROW_DU_DAY		0
#define ARROW_DU_MILLI		1		/* Date.unit default */
#define ARROW_TU_SECOND		0		/* Timestamp.unit default */
#define ARROW_TU_MILLI		1		/* Time.unit default */
#define ARROW_TU_MICRO		2
#define ARROW_TU_NANO		3

/* milliseconds in a day, for the date64 carrier */
#define ARROW_MSECS_PER_DAY	INT64CONST(86400000)
#define ARROW_TYPE_List			12
#define ARROW_TYPE_Struct		13
#define ARROW_TYPE_FixedSizeBinary 15
/* Arrow MessageHeader union tags (Message.fbs) */
#define ARROW_MSG_Schema		1
#define ARROW_MSG_DictionaryBatch 2
#define ARROW_MSG_RecordBatch	3
/* MetadataVersion V5 */
#define ARROW_METADATA_V5		4

/* Column kind -> how we lay out its Arrow buffers */
typedef enum ArrowKind
{
	A_INT16,
	A_INT32,
	A_INT64,
	A_FLOAT32,
	A_FLOAT64,
	A_BOOL,
	A_UTF8,
	A_BINARY,
	A_DATE32,					/* date -> Date32 (days from Unix epoch) */
	A_TIME64,					/* time -> Time64[us] */
	A_TIMESTAMP,				/* timestamp -> Timestamp[us], no zone */
	A_TIMESTAMPTZ,				/* timestamptz -> Timestamp[us], zone "UTC" */
	A_UUID,						/* uuid -> FixedSizeBinary(16) */
	A_DECIMAL128,				/* numeric(p,s) -> Decimal128(p,s) */
	A_LIST,						/* 1-D array -> List<element> (gap 27) */
	A_STRUCT					/* composite -> Struct<fields> (gap 27) */
}			ArrowKind;

/* Parse a numeric value (via its text form) into a 128-bit unscaled integer at
 * the given scale. Returns false for NaN/Infinity, which a decimal cannot hold.
 * The stored value already carries scale s, so padding suffices; the defensive
 * truncation branch never fires for a validly scaled numeric. */
static bool
numeric_to_int128(Datum numd, int scale, __int128 *out)
{
	char	   *s = DatumGetCString(DirectFunctionCall1(numeric_out, numd));
	char	   *p = s;
	bool		neg = false;
	bool		seenDot = false;
	int			fracDigits = 0;
	__int128	acc = 0;

	if (*p == '-')
	{
		neg = true;
		p++;
	}
	else if (*p == '+')
		p++;

	for (; *p; p++)
	{
		if (*p == '.')
		{
			seenDot = true;
			continue;
		}
		if (*p < '0' || *p > '9')	/* NaN, Infinity: not representable */
		{
			pfree(s);
			return false;
		}
		acc = acc * 10 + (*p - '0');
		if (seenDot)
			fracDigits++;
	}
	while (fracDigits < scale)
	{
		acc *= 10;
		fracDigits++;
	}
	while (fracDigits > scale)
	{
		acc /= 10;
		fracDigits--;
	}
	*out = neg ? -acc : acc;
	pfree(s);
	return true;
}

/* ---- Arrow Type table for one column; returns tag via *typetag ---- */
static uint32
pgc_fb_arrow_type(FBB *b, ArrowKind kind, int precision, int scale, uint8 *typetag)
{
	switch (kind)
	{
		case A_INT16:
		case A_INT32:
		case A_INT64:
			{
				int32		bits = (kind == A_INT16) ? 16 : (kind == A_INT32) ? 32 : 64;

				pgc_fb_start(b, 2);
				pgc_fb_add_i32(b, 0, bits, 0);	/* bitWidth */
				pgc_fb_add_bool(b, 1, true, false); /* is_signed */
				*typetag = ARROW_TYPE_Int;
				return pgc_fb_end(b);
			}
		case A_FLOAT32:
		case A_FLOAT64:
			pgc_fb_start(b, 1);
			pgc_fb_add_i16(b, 0, (kind == A_FLOAT32) ? 1 : 2, 0);	/* SINGLE/DOUBLE */
			*typetag = ARROW_TYPE_FloatingPoint;
			return pgc_fb_end(b);
		case A_BOOL:
			pgc_fb_start(b, 0);
			*typetag = ARROW_TYPE_Bool;
			return pgc_fb_end(b);
		case A_UTF8:
			pgc_fb_start(b, 0);
			*typetag = ARROW_TYPE_Utf8;
			return pgc_fb_end(b);
		case A_BINARY:
			pgc_fb_start(b, 0);
			*typetag = ARROW_TYPE_Binary;
			return pgc_fb_end(b);
		case A_DATE32:
			/* Date { unit: DateUnit = MILLISECOND (1) }; want DAY (0) */
			pgc_fb_start(b, 1);
			pgc_fb_add_i16(b, 0, 0, 1);
			*typetag = ARROW_TYPE_Date;
			return pgc_fb_end(b);
		case A_TIME64:
			/* Time { unit: TimeUnit = MILLISECOND (1); bitWidth: int = 32 } */
			pgc_fb_start(b, 2);
			pgc_fb_add_i16(b, 0, 2, 1);		/* MICROSECOND */
			pgc_fb_add_i32(b, 1, 64, 32);
			*typetag = ARROW_TYPE_Time;
			return pgc_fb_end(b);
		case A_TIMESTAMP:
		case A_TIMESTAMPTZ:
			{
				uint32		tzOff = 0;

				if (kind == A_TIMESTAMPTZ)
					tzOff = pgc_fb_create_string(b, "UTC");
				/* Timestamp { unit: TimeUnit = SECOND (0); timezone: string } */
				pgc_fb_start(b, 2);
				pgc_fb_add_i16(b, 0, 2, 0);		/* MICROSECOND */
				pgc_fb_add_offset(b, 1, tzOff);
				*typetag = ARROW_TYPE_Timestamp;
				return pgc_fb_end(b);
			}
		case A_UUID:
			/* FixedSizeBinary { byteWidth: int } */
			pgc_fb_start(b, 1);
			pgc_fb_add_i32(b, 0, 16, 0);
			*typetag = ARROW_TYPE_FixedSizeBinary;
			return pgc_fb_end(b);
		case A_DECIMAL128:
			/* Decimal { precision: int; scale: int; bitWidth: int = 128 } */
			pgc_fb_start(b, 3);
			pgc_fb_add_i32(b, 0, precision, 0);
			pgc_fb_add_i32(b, 1, scale, 0);
			*typetag = ARROW_TYPE_Decimal;
			return pgc_fb_end(b);
		case A_LIST:
			/* List {} -- the element type is carried in the Field's children */
			pgc_fb_start(b, 0);
			*typetag = ARROW_TYPE_List;
			return pgc_fb_end(b);
		case A_STRUCT:
			/* Struct_ {} -- the field types are the Field's children */
			pgc_fb_start(b, 0);
			*typetag = ARROW_TYPE_Struct;
			return pgc_fb_end(b);
	}
	*typetag = 0;
	return 0;					/* unreachable */
}

/* -------------------------------------------------------------------------
 * Per-column accumulator for one RecordBatch. Values for null slots are still
 * written (zeros / no advance) as Arrow requires; validity records nullness.
 * ------------------------------------------------------------------------- */
typedef struct ArrowCol
{
	char	   *name;
	ArrowKind	kind;
	int			width;			/* fixed-width byte size, 0 otherwise */
	int			precision;		/* decimal precision (A_DECIMAL128) */
	int			scale;			/* decimal scale (A_DECIMAL128) */
	bool		convertText;	/* A_UTF8 fallback needs the type output fn */
	FmgrInfo	outFinfo;		/* output function (when convertText) */
	StringInfoData valid;		/* 1 byte per row: 1 valid, 0 null */
	int64		nullCount;
	StringInfoData fixed;		/* fixed-width raw values */
	StringInfoData boolvals;	/* 1 byte per row (A_BOOL) */
	StringInfoData vardata;		/* concatenated var-length bytes */
	StringInfoData offs;		/* int32 running offsets, n+1 entries; A_LIST reuses
								 * this as the list offsets into the child */
	int32		running;

	/*
	 * Nested types (gap 27). A_LIST has one child (the element accumulator) and
	 * uses offs/running as its list offsets; A_STRUCT has nchildren field
	 * children and only a validity buffer of its own. Scalars have nchildren 0.
	 */
	struct ArrowCol *children;
	int			nchildren;
	Oid			elemtype;		/* A_LIST element type oid (for deconstruct) */
	int16		elemlen;
	bool		elembyval;
	char		elemalign;
}			ArrowCol;

static void
arrowcol_reset(ArrowCol *c)
{
	resetStringInfo(&c->valid);
	resetStringInfo(&c->fixed);
	resetStringInfo(&c->boolvals);
	resetStringInfo(&c->vardata);
	resetStringInfo(&c->offs);
	c->nullCount = 0;
	c->running = 0;
	if (c->kind == A_UTF8 || c->kind == A_BINARY || c->kind == A_LIST)
	{
		int32		z = 0;

		appendBinaryStringInfo(&c->offs, (char *) &z, 4);	/* offsets[0] = 0 */
	}
	{
		int			i;

		for (i = 0; i < c->nchildren; i++)
			arrowcol_reset(&c->children[i]);
	}
}

static ArrowKind
arrow_kind_for_type(Oid typid, int32 typmod, int *width, int *precision, int *scale)
{
	*precision = 0;
	*scale = 0;
	switch (typid)
	{
		case INT2OID:
			*width = 2;
			return A_INT16;
		case INT4OID:
			*width = 4;
			return A_INT32;
		case INT8OID:
			*width = 8;
			return A_INT64;
		case FLOAT4OID:
			*width = 4;
			return A_FLOAT32;
		case FLOAT8OID:
			*width = 8;
			return A_FLOAT64;
		case BOOLOID:
			*width = 0;
			return A_BOOL;
		case TEXTOID:
		case VARCHAROID:
		case JSONOID:
		case JSONBOID:
			*width = 0;
			return A_UTF8;
		case BYTEAOID:
			*width = 0;
			return A_BINARY;
		case DATEOID:
			*width = 4;
			return A_DATE32;
		case TIMEOID:
			*width = 8;
			return A_TIME64;
		case TIMESTAMPOID:
			*width = 8;
			return A_TIMESTAMP;
		case TIMESTAMPTZOID:
			*width = 8;
			return A_TIMESTAMPTZ;
		case UUIDOID:
			*width = 16;
			return A_UUID;
		case NUMERICOID:
			/* numeric(p,s) with p<=38 and 0<=s<=p -> Decimal128; otherwise
			 * (unconstrained, over-precision) fall back to text. */
			if (typmod >= (int32) VARHDRSZ)
			{
				int32		tmp = typmod - VARHDRSZ;
				int			p = (tmp >> 16) & 0xffff;
				int			s = tmp & 0xffff;

				if (p >= 1 && p <= 38 && s >= 0 && s <= p)
				{
					*width = 16;
					*precision = p;
					*scale = s;
					return A_DECIMAL128;
				}
			}
			*width = 0;
			return A_UTF8;		/* text fallback */
		default:
			*width = -1;
			return A_INT32;		/* caller checks width == -1 */
	}
}

/*
 * arrowcol_init
 *		Build an ArrowCol (recursively for nested types) for a column of type
 *		(typid, typmod). A 1-D array becomes A_LIST with one element child; a
 *		named composite becomes A_STRUCT with one child per live field; anything
 *		else is a scalar. Sets *ok = false if any (leaf) type is unsupported.
 */
static void
arrowcol_init(ArrowCol *c, const char *name, Oid typid, int32 typmod, bool *ok)
{
	Oid			elemtype;

	memset(c, 0, sizeof(*c));
	c->name = name ? pstrdup(name) : NULL;
	initStringInfo(&c->valid);
	initStringInfo(&c->fixed);
	initStringInfo(&c->boolvals);
	initStringInfo(&c->vardata);
	initStringInfo(&c->offs);

	elemtype = get_element_type(typid);
	if (OidIsValid(elemtype))
	{
		c->kind = A_LIST;
		c->elemtype = elemtype;
		get_typlenbyvalalign(elemtype, &c->elemlen, &c->elembyval, &c->elemalign);
		c->nchildren = 1;
		c->children = (ArrowCol *) palloc0(sizeof(ArrowCol));
		/* arrays do not carry the element typmod, so pass -1 */
		arrowcol_init(&c->children[0], "item", elemtype, -1, ok);
		return;
	}

	if (get_typtype(typid) == TYPTYPE_COMPOSITE)
	{
		TupleDesc	td = lookup_rowtype_tupdesc(typid, typmod);
		int			a;
		int			live = 0;
		int			fi = 0;

		for (a = 0; a < td->natts; a++)
			if (!TupleDescAttr(td, a)->attisdropped)
				live++;

		c->kind = A_STRUCT;
		c->nchildren = live;
		c->children = (ArrowCol *) palloc0(sizeof(ArrowCol) * Max(live, 1));
		for (a = 0; a < td->natts; a++)
		{
			Form_pg_attribute fa = TupleDescAttr(td, a);

			if (fa->attisdropped)
				continue;
			arrowcol_init(&c->children[fi++], NameStr(fa->attname),
						  fa->atttypid, fa->atttypmod, ok);
		}
		ReleaseTupleDesc(td);
		return;
	}

	/* scalar leaf */
	{
		int			width;

		c->kind = arrow_kind_for_type(typid, typmod, &width,
									  &c->precision, &c->scale);
		c->width = width;
		if (width < 0)
		{
			*ok = false;
			return;
		}
		c->convertText = (c->kind == A_UTF8 &&
						  (typid == NUMERICOID || typid == JSONBOID));
		if (c->convertText)
		{
			Oid			outfunc;
			bool		isvarlena;

			getTypeOutputInfo(typid, &outfunc, &isvarlena);
			fmgr_info(outfunc, &c->outFinfo);
		}
	}
}

/* append one row's value for a column */
static void
arrowcol_append(ArrowCol *c, Datum d, bool isnull)
{
	char		one = 1,
				zero = 0;
	__int128	dec = 0;

	/*
	 * A few types can carry values with no target representation (±infinity
	 * dates/timestamps, NaN/Infinity numerics). Fold those to null, computing
	 * the decimal here so a non-finite value is detected before validity is
	 * written.
	 */
	if (!isnull)
	{
		switch (c->kind)
		{
			case A_DATE32:
				if (DATE_NOT_FINITE(DatumGetDateADT(d)))
					isnull = true;
				break;
			case A_TIMESTAMP:
			case A_TIMESTAMPTZ:
				if (TIMESTAMP_NOT_FINITE(DatumGetTimestamp(d)))
					isnull = true;
				break;
			case A_DECIMAL128:
				if (!numeric_to_int128(d, c->scale, &dec))
					isnull = true;
				break;
			default:
				break;
		}
	}

	appendStringInfoChar(&c->valid, isnull ? zero : one);
	if (isnull)
		c->nullCount++;

	switch (c->kind)
	{
		case A_INT16:
			{
				int16		v = isnull ? 0 : DatumGetInt16(d);

				appendBinaryStringInfo(&c->fixed, (char *) &v, 2);
				break;
			}
		case A_INT32:
			{
				int32		v = isnull ? 0 : DatumGetInt32(d);

				appendBinaryStringInfo(&c->fixed, (char *) &v, 4);
				break;
			}
		case A_INT64:
			{
				int64		v = isnull ? 0 : DatumGetInt64(d);

				appendBinaryStringInfo(&c->fixed, (char *) &v, 8);
				break;
			}
		case A_FLOAT32:
			{
				float4		v = isnull ? 0 : DatumGetFloat4(d);

				appendBinaryStringInfo(&c->fixed, (char *) &v, 4);
				break;
			}
		case A_FLOAT64:
			{
				float8		v = isnull ? 0 : DatumGetFloat8(d);

				appendBinaryStringInfo(&c->fixed, (char *) &v, 8);
				break;
			}
		case A_DATE32:
			{
				int32		v = isnull ? 0 :
					(int32) (DatumGetDateADT(d) + PG_TO_UNIX_DAYS);

				appendBinaryStringInfo(&c->fixed, (char *) &v, 4);
				break;
			}
		case A_TIME64:
			{
				int64		v = isnull ? 0 : (int64) DatumGetTimeADT(d);

				appendBinaryStringInfo(&c->fixed, (char *) &v, 8);
				break;
			}
		case A_TIMESTAMP:
		case A_TIMESTAMPTZ:
			{
				int64		v = isnull ? 0 :
					(int64) DatumGetTimestamp(d) + PG_TO_UNIX_USECS;

				appendBinaryStringInfo(&c->fixed, (char *) &v, 8);
				break;
			}
		case A_UUID:
			{
				static const char zeros[UUID_LEN] = {0};

				if (isnull)
					appendBinaryStringInfo(&c->fixed, zeros, UUID_LEN);
				else
					appendBinaryStringInfo(&c->fixed,
										   (char *) DatumGetUUIDP(d)->data, UUID_LEN);
				break;
			}
		case A_DECIMAL128:
			{
				char		buf[16];

				if (isnull)
					memset(buf, 0, 16);
				else
					memcpy(buf, &dec, 16);	/* little-endian two's complement */
				appendBinaryStringInfo(&c->fixed, buf, 16);
				break;
			}
		case A_BOOL:
			appendStringInfoChar(&c->boolvals,
								 (!isnull && DatumGetBool(d)) ? 1 : 0);
			break;
		case A_UTF8:
		case A_BINARY:
			{
				if (!isnull)
				{
					if (c->convertText)
					{
						char	   *str = OutputFunctionCall(&c->outFinfo, d);
						int			len = (int) strlen(str);

						if (len > 0)
							appendBinaryStringInfo(&c->vardata, str, len);
						c->running += len;
						pfree(str);
					}
					else
					{
						struct varlena *v = PG_DETOAST_DATUM_PACKED(d);
						int			len = VARSIZE_ANY_EXHDR(v);

						appendBinaryStringInfo(&c->vardata, VARDATA_ANY(v), len);
						c->running += len;
					}
				}
				appendBinaryStringInfo(&c->offs, (char *) &c->running, 4);
				break;
			}
		case A_LIST:
			{
				/* flatten the array's elements into the child; the list offset is
				 * the running element count (a NULL or empty list adds none) */
				if (!isnull)
				{
					ArrayType  *arr = DatumGetArrayTypeP(d);
					Datum	   *elems;
					bool	   *enulls;
					int			nelems;
					int			k;

					if (ARR_NDIM(arr) > 1)
						ereport(ERROR,
								(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
								 errmsg("columnar.export_arrow does not support multi-dimensional arrays")));

					deconstruct_array(arr, c->elemtype, c->elemlen,
									  c->elembyval, c->elemalign,
									  &elems, &enulls, &nelems);
					for (k = 0; k < nelems; k++)
						arrowcol_append(&c->children[0], elems[k], enulls[k]);
					c->running += nelems;
				}
				appendBinaryStringInfo(&c->offs, (char *) &c->running, 4);
				break;
			}
		case A_STRUCT:
			{
				int			k;

				if (isnull)
				{
					/* a null struct still contributes one (null) value per child */
					for (k = 0; k < c->nchildren; k++)
						arrowcol_append(&c->children[k], (Datum) 0, true);
				}
				else
				{
					HeapTupleHeader th = DatumGetHeapTupleHeader(d);
					Oid			tupType = HeapTupleHeaderGetTypeId(th);
					int32		tupTypmod = HeapTupleHeaderGetTypMod(th);
					TupleDesc	td = lookup_rowtype_tupdesc(tupType, tupTypmod);
					HeapTupleData tmptup;
					Datum	   *av = palloc(sizeof(Datum) * td->natts);
					bool	   *an = palloc(sizeof(bool) * td->natts);
					int			a;
					int			fi = 0;

					tmptup.t_len = HeapTupleHeaderGetDatumLength(th);
					ItemPointerSetInvalid(&tmptup.t_self);
					tmptup.t_tableOid = InvalidOid;
					tmptup.t_data = th;
					heap_deform_tuple(&tmptup, td, av, an);
					for (a = 0; a < td->natts; a++)
					{
						if (TupleDescAttr(td, a)->attisdropped)
							continue;
						arrowcol_append(&c->children[fi], av[a], an[a]);
						fi++;
					}
					ReleaseTupleDesc(td);
				}
				break;
			}
	}
}

/* append `body` with one buffer of `bytes`/`len`, 8-padded; record Buffer meta */
static void
body_add_buffer(StringInfo body, int64 *offsets, int64 *lengths, int *nbuf,
				const char *bytes, int64 len)
{
	static const char zeros[8] = {0};
	int64		pad;

	offsets[*nbuf] = body->len;
	lengths[*nbuf] = len;
	(*nbuf)++;
	if (len > 0)
		appendBinaryStringInfo(body, bytes, len);
	pad = (8 - (body->len % 8)) % 8;
	if (pad > 0)
		appendBinaryStringInfo(body, zeros, pad);
}

/* number of Arrow FieldNodes an ArrowCol contributes (itself plus descendants) */
static int
arrow_count_nodes(ArrowCol *c)
{
	int			n = 1;
	int			i;

	for (i = 0; i < c->nchildren; i++)
		n += arrow_count_nodes(&c->children[i]);
	return n;
}

/* number of Arrow buffers an ArrowCol contributes (itself plus descendants) */
static int
arrow_count_buffers(ArrowCol *c)
{
	int			n;
	int			i;

	switch (c->kind)
	{
		case A_UTF8:
		case A_BINARY:
			n = 3;				/* validity, offsets, data */
			break;
		case A_LIST:
			n = 2;				/* validity, offsets */
			break;
		case A_STRUCT:
			n = 1;				/* validity only */
			break;
		default:
			n = 2;				/* validity, values (fixed or bool bits) */
			break;
	}
	for (i = 0; i < c->nchildren; i++)
		n += arrow_count_buffers(&c->children[i]);
	return n;
}

/* append this node's FieldNode {length, null_count}, pre-order over children */
static void
arrow_emit_nodes(ArrowCol *c, int64 *nodeLen, int64 *nodeNull, int *nn)
{
	int			i;

	nodeLen[*nn] = c->valid.len;	/* one validity byte was appended per value */
	nodeNull[*nn] = c->nullCount;
	(*nn)++;
	for (i = 0; i < c->nchildren; i++)
		arrow_emit_nodes(&c->children[i], nodeLen, nodeNull, nn);
}

/* append this node's buffers to body (pre-order), packing validity into a bitmap */
static void
arrow_emit_buffers(StringInfo body, ArrowCol *c,
				   int64 *bufOff, int64 *bufLen, int *nb)
{
	int			nvals = c->valid.len;
	int			vlen = (nvals + 7) / 8;
	char	   *validbits = palloc0(Max(vlen, 1));
	int			r;
	int			i;

	for (r = 0; r < nvals; r++)
		if (c->valid.data[r])
			validbits[r >> 3] |= (1 << (r & 7));
	body_add_buffer(body, bufOff, bufLen, nb, validbits, vlen);

	switch (c->kind)
	{
		case A_BOOL:
			{
				char	   *bits = palloc0(Max(vlen, 1));

				for (r = 0; r < nvals; r++)
					if (c->boolvals.data[r])
						bits[r >> 3] |= (1 << (r & 7));
				body_add_buffer(body, bufOff, bufLen, nb, bits, vlen);
				break;
			}
		case A_UTF8:
		case A_BINARY:
			body_add_buffer(body, bufOff, bufLen, nb, c->offs.data, c->offs.len);
			body_add_buffer(body, bufOff, bufLen, nb, c->vardata.data, c->vardata.len);
			break;
		case A_LIST:
			body_add_buffer(body, bufOff, bufLen, nb, c->offs.data, c->offs.len);
			break;
		case A_STRUCT:
			break;				/* validity only; children carry the data */
		default:
			body_add_buffer(body, bufOff, bufLen, nb, c->fixed.data, c->fixed.len);
			break;
	}

	for (i = 0; i < c->nchildren; i++)
		arrow_emit_buffers(body, &c->children[i], bufOff, bufLen, nb);
}

/* build a schema Field (recursively for nested types) and return its offset */
static uint32
arrow_build_field(FBB *b, ArrowCol *c)
{
	uint32		nameOff = pgc_fb_create_string(b, c->name ? c->name : "");
	uint8		typetag;
	uint32		typeOff;
	uint32		childrenVec = 0;
	uint32	   *childOff = NULL;
	int			i;

	if (c->nchildren > 0)
	{
		childOff = palloc(sizeof(uint32) * c->nchildren);
		for (i = 0; i < c->nchildren; i++)
			childOff[i] = arrow_build_field(b, &c->children[i]);
		pgc_fb_start_vector(b, 4, c->nchildren, 4);
		for (i = c->nchildren - 1; i >= 0; i--)
			pgc_fb_push_uoffset(b, childOff[i]);
		childrenVec = pgc_fb_end_vector(b, c->nchildren);
	}

	typeOff = pgc_fb_arrow_type(b, c->kind, c->precision, c->scale, &typetag);

	pgc_fb_start(b, 7);
	pgc_fb_add_offset(b, 0, nameOff);	/* name */
	pgc_fb_add_bool(b, 1, true, false); /* nullable */
	pgc_fb_add_u8(b, 2, typetag, 0);	/* type_type */
	pgc_fb_add_offset(b, 3, typeOff);	/* type */
	if (c->nchildren > 0)
		pgc_fb_add_offset(b, 5, childrenVec);	/* children (Field slot 5) */
	return pgc_fb_end(b);
}

/* build one RecordBatch (metadata + body) and write it */
static void
write_record_batch(PqSink *snk, ArrowCol *cols, int ncols, int64 nrows)
{
	StringInfoData body;
	FBB			b;
	int64	   *nodeLen;
	int64	   *nodeNull;
	int64	   *bufOff;
	int64	   *bufLen;
	int			totalNodes = 0;
	int			totalBufs = 0;
	int			nnodes = 0,
				nbuf = 0;
	int			i;
	uint32		nodesVec,
				bufsVec,
				rbOff,
				msgOff;
	uint32		cont = 0xFFFFFFFF;
	uint32		metaLen;
	uint32		metaPad;

	/* size the node/buffer arrays for the whole (possibly nested) field tree */
	for (i = 0; i < ncols; i++)
	{
		totalNodes += arrow_count_nodes(&cols[i]);
		totalBufs += arrow_count_buffers(&cols[i]);
	}
	nodeLen = palloc(sizeof(int64) * Max(totalNodes, 1));
	nodeNull = palloc(sizeof(int64) * Max(totalNodes, 1));
	bufOff = palloc(sizeof(int64) * Max(totalBufs, 1));
	bufLen = palloc(sizeof(int64) * Max(totalBufs, 1));

	initStringInfo(&body);

	/* nodes and buffers are each laid out pre-order across all fields */
	for (i = 0; i < ncols; i++)
	{
		arrow_emit_nodes(&cols[i], nodeLen, nodeNull, &nnodes);
		arrow_emit_buffers(&body, &cols[i], bufOff, bufLen, &nbuf);
	}

	/* ---- RecordBatch metadata flatbuffer ---- */
	pgc_fb_init(&b);

	/* nodes vector: [FieldNode{length,null_count}] structs, 16B/8-align */
	pgc_fb_start_vector(&b, 16, nnodes, 8);
	for (i = nnodes - 1; i >= 0; i--)
	{
		pgc_fb_prep(&b, 8, 0);
		pgc_fb_place(&b, &nodeNull[i], 8);	/* null_count (higher) */
		pgc_fb_place(&b, &nodeLen[i], 8);	/* length (lower) */
	}
	nodesVec = pgc_fb_end_vector(&b, nnodes);

	/* buffers vector: [Buffer{offset,length}] structs */
	pgc_fb_start_vector(&b, 16, nbuf, 8);
	for (i = nbuf - 1; i >= 0; i--)
	{
		pgc_fb_prep(&b, 8, 0);
		pgc_fb_place(&b, &bufLen[i], 8); /* length (higher) */
		pgc_fb_place(&b, &bufOff[i], 8); /* offset (lower) */
	}
	bufsVec = pgc_fb_end_vector(&b, nbuf);

	pgc_fb_start(&b, 4);
	pgc_fb_add_i64(&b, 0, nrows, 0);
	pgc_fb_add_offset(&b, 1, nodesVec);
	pgc_fb_add_offset(&b, 2, bufsVec);
	rbOff = pgc_fb_end(&b);

	pgc_fb_start(&b, 5);
	pgc_fb_add_i16(&b, 0, ARROW_METADATA_V5, 0);
	pgc_fb_add_u8(&b, 1, ARROW_MSG_RecordBatch, 0);
	pgc_fb_add_offset(&b, 2, rbOff);
	pgc_fb_add_i64(&b, 3, body.len, 0); /* bodyLength */
	msgOff = pgc_fb_end(&b);
	pgc_fb_finish(&b, msgOff);

	/* ---- write encapsulated message ---- */
	metaLen = b.tail;
	metaPad = (8 - (metaLen % 8)) % 8;
	{
		uint32		metaLenField = metaLen + metaPad;
		static const char zeros[8] = {0};

		PgColumnarSinkWrite(snk, &cont, 4);
		PgColumnarSinkWrite(snk, &metaLenField, 4);
		PgColumnarSinkWrite(snk, b.buf + b.cap - b.tail, metaLen);
		if (metaPad)
			PgColumnarSinkWrite(snk, zeros, metaPad);
		if (body.len > 0)
			PgColumnarSinkWrite(snk, body.data, body.len);
	}

	pfree(b.buf);
	pfree(body.data);
}

/*
 * pgcolumnar_export_arrow
 *		SQL: pgcolumnar.export_arrow(rel regclass, path text) -> bigint.
 *		Write a columnar table to an Arrow IPC stream file; returns the number
 *		of rows written.
 */
Datum
pgcolumnar_export_arrow(PG_FUNCTION_ARGS)
{
	Oid			relid;
	text	   *pathText;
	char	   *path;
	Relation	rel;
	TupleDesc	tupdesc;
	int			ncols;
	ArrowCol   *cols;
	Snapshot	snapshot;
	PgColumnarReadState *readState;
	Datum	   *values;
	bool	   *nulls;
	uint64		rowNumber;
	int64		total = 0;
	int64		batchRows = 0;
	PqSink	   *snk;
	FBB			b;
	int			i;
	uint32		vec,
				schemaOff,
				msgOff;
	uint32	   *fieldOff;
	MemoryContext batchCtx,
				oldCtx;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("relation and path must not be null")));

	if (!has_privs_of_role(GetUserId(), ROLE_PG_WRITE_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_write_server_files role to write a server file")));

	relid = PG_GETARG_OID(0);
	pathText = PG_GETARG_TEXT_PP(1);
	path = text_to_cstring(pathText);

	/*
	 * SELECT on the source, before the file is opened (#559). The server-file
	 * role above governs writing a file; it says nothing about reading this
	 * relation, so without this a pg_write_server_files member could dump any
	 * columnar table it cannot SELECT. The parallel twin already checks this
	 * (columnar_parallel_export.c:471).
	 */
	{
		AclResult	ac = pg_class_aclcheck(relid, GetUserId(), ACL_SELECT);

		if (ac != ACLCHECK_OK)
			aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));
	}

	/* RLS after the ACL check, matching core's ordering (#563). */
	PgColumnarRequireNoRowSecurity(relid);

	rel = table_open(relid, AccessShareLock);
	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, AccessShareLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	tupdesc = RelationGetDescr(rel);
	ncols = tupdesc->natts;
	if (ncols > 16)
	{
		table_close(rel, AccessShareLock);
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar.export_arrow supports at most 16 columns")));
	}

	cols = palloc0(sizeof(ArrowCol) * ncols);
	for (i = 0; i < ncols; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);
		bool		ok = true;

		if (att->attisdropped)
		{
			table_close(rel, AccessShareLock);
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar.export_arrow does not support dropped columns")));
		}
		/* build the (possibly nested) accumulator tree for this column */
		arrowcol_init(&cols[i], NameStr(att->attname),
					  att->atttypid, att->atttypmod, &ok);
		if (!ok)
		{
			table_close(rel, AccessShareLock);
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("column \"%s\" has type %s, which columnar.export_arrow does not support",
							NameStr(att->attname),
							format_type_be(att->atttypid))));
		}
		arrowcol_reset(&cols[i]);
	}

	snk = PgColumnarSinkOpen(path);
	PG_TRY();
	{

	/* ---- Schema message ---- */
	fieldOff = palloc(sizeof(uint32) * ncols);
	pgc_fb_init(&b);
	for (i = 0; i < ncols; i++)
		fieldOff[i] = arrow_build_field(&b, &cols[i]);
	pgc_fb_start_vector(&b, 4, ncols, 4);
	for (i = ncols - 1; i >= 0; i--)
		pgc_fb_push_uoffset(&b, fieldOff[i]);
	vec = pgc_fb_end_vector(&b, ncols);

	pgc_fb_start(&b, 4);
	/* endianness Little=0 is the default, so omit slot 0 */
	pgc_fb_add_offset(&b, 1, vec);	/* fields */
	schemaOff = pgc_fb_end(&b);

	pgc_fb_start(&b, 5);
	pgc_fb_add_i16(&b, 0, ARROW_METADATA_V5, 0);
	pgc_fb_add_u8(&b, 1, ARROW_MSG_Schema, 0);
	pgc_fb_add_offset(&b, 2, schemaOff);
	msgOff = pgc_fb_end(&b);
	pgc_fb_finish(&b, msgOff);

	{
		uint32		cont = 0xFFFFFFFF;
		uint32		metaLen = b.tail;
		uint32		metaPad = (8 - (metaLen % 8)) % 8;
		uint32		metaLenField = metaLen + metaPad;
		static const char zeros[8] = {0};

		PgColumnarSinkWrite(snk, &cont, 4);
		PgColumnarSinkWrite(snk, &metaLenField, 4);
		PgColumnarSinkWrite(snk, b.buf + b.cap - b.tail, metaLen);
		if (metaPad)
			PgColumnarSinkWrite(snk, zeros, metaPad);
	}
	pfree(b.buf);

	/* ---- RecordBatch messages ---- */
	batchCtx = AllocSetContextCreate(CurrentMemoryContext,
									 "columnar arrow batch",
									 ALLOCSET_DEFAULT_SIZES);

	values = palloc(sizeof(Datum) * ncols);
	nulls = palloc(sizeof(bool) * ncols);

	snapshot = ActiveSnapshotSet() ? GetActiveSnapshot() : GetTransactionSnapshot();
	readState = PgColumnarBeginRead(rel, snapshot, NULL, NULL, 0, NULL);

	while (PgColumnarReadNextRow(readState, values, nulls, &rowNumber))
	{
		CHECK_FOR_INTERRUPTS();
		for (i = 0; i < ncols; i++)
			arrowcol_append(&cols[i], values[i], nulls[i]);
		batchRows++;
		total++;

		if (batchRows == ARROW_BATCH_ROWS)
		{
			oldCtx = MemoryContextSwitchTo(batchCtx);
			write_record_batch(snk, cols, ncols, batchRows);
			MemoryContextSwitchTo(oldCtx);
			MemoryContextReset(batchCtx);
			for (i = 0; i < ncols; i++)
				arrowcol_reset(&cols[i]);
			batchRows = 0;
		}
	}
	PgColumnarEndRead(readState);

	if (batchRows > 0)
	{
		oldCtx = MemoryContextSwitchTo(batchCtx);
		write_record_batch(snk, cols, ncols, batchRows);
		MemoryContextSwitchTo(oldCtx);
	}

	/* ---- end-of-stream marker ---- */
	{
		uint32		cont = 0xFFFFFFFF;
		uint32		zero = 0;

		PgColumnarSinkWrite(snk, &cont, 4);
		PgColumnarSinkWrite(snk, &zero, 4);
	}

	/*
	 * Commit: the final name appears only here, whole (#394). Any failure
	 * above unwinds through the CATCH, which removes the temp file; the
	 * final path is never touched on error.
	 */
	PgColumnarSinkFinish(snk);
	}
	PG_CATCH();
	{
		PgColumnarSinkAbort(snk);
		PG_RE_THROW();
	}
	PG_END_TRY();

	table_close(rel, AccessShareLock);
	PG_RETURN_INT64(total);
}

/* =========================================================================
 * Arrow IPC stream import: pgcolumnar.import_arrow(rel regclass, path text).
 *
 * Reads an Arrow IPC *stream* file (as pgcolumnar.export_arrow writes, and as
 * pyarrow writes for non-dictionary arrays) and inserts its rows into an
 * existing columnar table whose column types match the file's schema, using
 * the reverse of the export type mapping. Uncompressed bodies only; a
 * DictionaryBatch or a compressed RecordBatch is rejected. Because it parses an
 * external file, every metadata and body read is bounds-checked and a
 * malformed file raises ERRCODE_DATA_CORRUPTED rather than reading out of
 * bounds.
 * ========================================================================= */

#define IMPORT_CORRUPT(msg) \
	ereport(ERROR, \
			(errcode(ERRCODE_DATA_CORRUPTED), \
			 errmsg("columnar: malformed Arrow IPC file: %s", (msg))))


/* ---- bounds-checked little-endian FlatBuffers reader ---- */
static uint8
fbr_u8(const uint8 *b, uint32 len, uint32 pos)
{
	if ((uint64) pos + 1 > len)
		IMPORT_CORRUPT("truncated u8");
	return b[pos];
}
static uint16
fbr_u16(const uint8 *b, uint32 len, uint32 pos)
{
	uint16		v;

	if ((uint64) pos + 2 > len)
		IMPORT_CORRUPT("truncated u16");
	memcpy(&v, b + pos, 2);
	return v;
}
static uint32
fbr_u32(const uint8 *b, uint32 len, uint32 pos)
{
	uint32		v;

	if ((uint64) pos + 4 > len)
		IMPORT_CORRUPT("truncated u32");
	memcpy(&v, b + pos, 4);
	return v;
}
static int16
fbr_i16(const uint8 *b, uint32 len, uint32 pos)
{
	return (int16) fbr_u16(b, len, pos);
}
static int32
fbr_i32(const uint8 *b, uint32 len, uint32 pos)
{
	return (int32) fbr_u32(b, len, pos);
}
static int64
fbr_i64(const uint8 *b, uint32 len, uint32 pos)
{
	int64		v;

	if ((uint64) pos + 8 > len)
		IMPORT_CORRUPT("truncated i64");
	memcpy(&v, b + pos, 8);
	return v;
}

/* absolute position of field `i` of the table at `tab`, or 0 if absent */
static uint32
pgc_fb_field(const uint8 *b, uint32 len, uint32 tab, int i)
{
	int32		soff = fbr_i32(b, len, tab);
	int64		vt = (int64) tab - soff;
	uint16		vtsize;
	uint32		slot = 4 + (uint32) i * 2;
	uint16		voff;

	if (vt < 0 || (uint64) vt + 4 > len)
		IMPORT_CORRUPT("vtable out of bounds");
	vtsize = fbr_u16(b, len, (uint32) vt);
	if (slot >= vtsize)
		return 0;
	voff = fbr_u16(b, len, (uint32) vt + slot);
	if (voff == 0)
		return 0;
	return tab + voff;
}

/* follow the uoffset stored at `pos` to the object it points at */
static uint32
pgc_fb_indirect(const uint8 *b, uint32 len, uint32 pos)
{
	return pos + fbr_u32(b, len, pos);
}

/* Build a numeric input string for a 128-bit unscaled value at the given scale
 * (reverse of numeric_to_int128). */
static char *
int128_to_numeric_cstring(__int128 v, int scale)
{
	char		digs[48];
	int			n = 0;
	int			L,
				j;
	bool		neg = v < 0;
	unsigned __int128 u = neg ? -(unsigned __int128) v : (unsigned __int128) v;
	StringInfoData s;

	if (u == 0)
		digs[n++] = '0';
	while (u > 0 && n < (int) sizeof(digs))
	{
		digs[n++] = (char) ('0' + (int) (u % 10));
		u /= 10;
	}
	/* digs[] holds least-significant digit first; emit most-significant first */
	initStringInfo(&s);
	if (neg)
		appendStringInfoChar(&s, '-');
	L = n - scale;				/* number of integer digits */
	if (L <= 0)
	{
		appendStringInfoString(&s, "0.");
		for (j = 0; j < -L; j++)
			appendStringInfoChar(&s, '0');
		for (j = 0; j < n; j++)
			appendStringInfoChar(&s, digs[n - 1 - j]);
	}
	else
	{
		for (j = 0; j < L; j++)
			appendStringInfoChar(&s, digs[n - 1 - j]);
		if (scale > 0)
		{
			appendStringInfoChar(&s, '.');
			for (j = L; j < n; j++)
				appendStringInfoChar(&s, digs[n - 1 - j]);
		}
	}
	return s.data;
}

/* Read a validity bit for row r from a bitmap buffer (empty bitmap = all valid). */
static inline bool
imp_is_null(const uint8 *body, int64 bufOff, int64 bufLen, int64 r)
{
	if (bufLen == 0)
		return false;			/* Arrow omits the bitmap when null_count == 0 */
	return ((body[bufOff + (r >> 3)] >> (r & 7)) & 1) == 0;
}

/*
 * Import field-tree node (gap 27 nested import). Mirrors the export tree: a 1-D
 * array target is A_LIST with one element child; a composite is A_STRUCT with a
 * child per live field; everything else is a scalar leaf. Buffer indices into the
 * RecordBatch's flat buffer vector are assigned once, pre-order, matching the
 * layout the exporters (and Arrow) emit: [validity] then [data] / [offsets,data]
 * for a leaf, [validity,offsets] then the child for a list, [validity] then the
 * children for a struct.
 */
typedef struct ImpNode
{
	ArrowKind	kind;
	Oid			typid;
	int			width;			/* carrier bytes IN THE FILE, not in PostgreSQL */
	int			scale;			/* decimal scale; not a temporal unit */
	int			srcUnit;		/* Arrow DateUnit/TimeUnit, -1 if the file said nothing */
	int32		atttypmod;
	bool		needsInput;
	FmgrInfo	inFinfo;
	Oid			inTypioparam;
	/* list element */
	Oid			elemtype;
	int16		elemlen;
	bool		elembyval;
	char		elemalign;
	/* struct */
	TupleDesc	structDesc;
	struct ImpNode *children;
	int			nchildren;
	/* per-schema buffer assignment */
	int			validBuf;
	int			offBuf;			/* list/utf8 offsets, else -1 */
	int			dataBuf;		/* scalar/utf8 data, else -1 */
}			ImpNode;

/* build an import node for a target column type; *ok=false if unsupported */
static void
imp_build_node(ImpNode *n, Oid typid, int32 typmod, bool *ok)
{
	Oid			elemtype;

	memset(n, 0, sizeof(*n));
	n->typid = typid;
	n->atttypmod = typmod;
	n->validBuf = n->offBuf = n->dataBuf = -1;
	n->srcUnit = -1;

	elemtype = get_element_type(typid);
	if (OidIsValid(elemtype))
	{
		n->kind = A_LIST;
		n->elemtype = elemtype;
		get_typlenbyvalalign(elemtype, &n->elemlen, &n->elembyval, &n->elemalign);
		n->nchildren = 1;
		n->children = palloc0(sizeof(ImpNode));
		imp_build_node(&n->children[0], elemtype, -1, ok);
		return;
	}
	if (get_typtype(typid) == TYPTYPE_COMPOSITE)
	{
		TupleDesc	td = lookup_rowtype_tupdesc(typid, typmod);
		int			a,
					live = 0,
					fi = 0;

		for (a = 0; a < td->natts; a++)
			if (!TupleDescAttr(td, a)->attisdropped)
				live++;
		n->kind = A_STRUCT;
		n->structDesc = CreateTupleDescCopy(td);
		n->nchildren = live;
		n->children = palloc0(sizeof(ImpNode) * Max(live, 1));
		for (a = 0; a < td->natts; a++)
		{
			Form_pg_attribute fa = TupleDescAttr(td, a);

			if (fa->attisdropped)
				continue;
			imp_build_node(&n->children[fi++], fa->atttypid, fa->atttypmod, ok);
		}
		ReleaseTupleDesc(td);
		return;
	}

	/* scalar leaf */
	{
		int			width,
					precision,
					scale;

		n->kind = arrow_kind_for_type(typid, typmod, &width, &precision, &scale);
		if (width < 0)
		{
			*ok = false;
			return;
		}
		n->width = width;
		n->scale = scale;
		n->needsInput = (n->kind == A_UTF8);
		if (n->needsInput)
		{
			Oid			infunc;

			getTypeInputInfo(typid, &infunc, &n->inTypioparam);
			fmgr_info(infunc, &n->inFinfo);
		}
	}
}

/* assign RecordBatch buffer indices to a node subtree, pre-order */
static void
imp_assign_buffers(ImpNode *n, int *bufcur)
{
	n->validBuf = (*bufcur)++;
	switch (n->kind)
	{
		case A_LIST:
			n->offBuf = (*bufcur)++;
			imp_assign_buffers(&n->children[0], bufcur);
			break;
		case A_STRUCT:
			{
				int			c;

				for (c = 0; c < n->nchildren; c++)
					imp_assign_buffers(&n->children[c], bufcur);
				break;
			}
		case A_UTF8:
		case A_BINARY:
			n->offBuf = (*bufcur)++;
			n->dataBuf = (*bufcur)++;
			break;
		case A_BOOL:
			n->dataBuf = (*bufcur)++;
			break;
		default:				/* fixed width */
			n->dataBuf = (*bufcur)++;
			break;
	}
}

/* decode a scalar leaf value at index i (caller checked non-null) */
/*
 * Convert a stored TIME/TIMESTAMP value to the microseconds PostgreSQL stores,
 * per the unit the FILE declares. Returns false if the conversion overflows.
 *
 * This mirrors pq_scale_to_usecs in columnar_parquet_reader.c, and deliberately
 * makes the same two calls: NANOS is divided rather than refused, because
 * PostgreSQL has no nanosecond timestamp and truncating yields the right instant
 * whereas reading nanoseconds as microseconds is wrong by a factor of 1000; and
 * a unit the file did not declare is read as microseconds, which is what our own
 * exporter writes. Arrow adds a SECOND unit that Parquet does not have.
 *
 * If one reader's policy changes the other must change with it.
 */
/*
 * Floor division. C division truncates toward zero, which for a negative value
 * names the unit AFTER the one it falls in: -1500 nanoseconds is 1.5us before
 * the epoch, and -1500/1000 == -1 places it 1us before instead. Every narrowing
 * here floors, so the date arm and the timestamp arm cannot disagree about which
 * day or microsecond an instant belongs to.
 */
static int64
arrow_floordiv(int64 num, int64 den)
{
	int64		q = num / den;

	if (num % den != 0 && (num < 0) != (den < 0))
		q--;
	return q;
}

static bool
arrow_scale_to_usecs(int unit, int64 v, int64 *out)
{
	switch (unit)
	{
		case ARROW_TU_SECOND:
			return !pg_mul_s64_overflow(v, INT64CONST(1000000), out);
		case ARROW_TU_MILLI:
			return !pg_mul_s64_overflow(v, INT64CONST(1000), out);
		case ARROW_TU_NANO:
			*out = arrow_floordiv(v, INT64CONST(1000));
			return true;
		case ARROW_TU_MICRO:
		default:
			*out = v;
			return true;
	}
}

/*
 * Apply what the file's Field table declares onto an import node.
 *
 * imp_build_node knows only what PostgreSQL wants. The carrier width and the
 * temporal unit live in the file and the target type does not imply either:
 * Arrow gives date32 and date64 the same type tag (8) and time32 and time64 the
 * same tag (9), so the tag alone never settles the width.
 *
 * An absent field means its FlatBuffers default, which is not zero for Date or
 * Time -- see the ARROW_DU_/ARROW_TU_ comment above.
 *
 * Recursion is driven by the node tree, which comes from the target type, so a
 * file cannot drive it deeper than the column's own nesting.
 */
static void
imp_temporal_mismatch(const char *arrowtype, Oid typid)
{
	ereport(ERROR,
			(errcode(ERRCODE_DATATYPE_MISMATCH),
			 errmsg("Arrow field is %s, which columnar.import_arrow cannot read into type %s",
					arrowtype, format_type_be(typid))));
}

static void
imp_apply_field(ImpNode *n, const uint8 *meta, uint32 metaLen, uint32 field)
{
	uint32		pos;
	uint32		tt;
	uint8		typetag;

	if (field == 0)
		return;

	pos = pgc_fb_field(meta, metaLen, field, 2);	/* type_type (u8) */
	typetag = pos ? fbr_u8(meta, metaLen, pos) : 0;
	pos = pgc_fb_field(meta, metaLen, field, 3);	/* type (offset) */
	tt = pos ? pgc_fb_indirect(meta, metaLen, pos) : 0;

	if (tt != 0)
	{
		/*
		 * Stamp a node ONLY when the file's tag matches the kind the target type
		 * gave it. n->width is both the stride and the divisor in
		 * imp_check_bounds, while every non-temporal decode arm memcpy's a size
		 * fixed by its kind. Taking a width from a tag the target does not share
		 * severs those two, and a Date-tagged field would set width 4 under an
		 * A_INT64 node that still reads 8 bytes -- passing the bounds check and
		 * reading off the end of the body. A temporal tag under a target that
		 * cannot hold it is refused by name. Leaving it alone kept a pre-existing
		 * silent corruption: measured on unpatched main, a time64 file into a date
		 * column stored 687342-02-27 and a timestamp file into one stored
		 * 2722128-09-17, both from the low 4 bytes of an 8-byte carrier. A
		 * NON-temporal tag is still left alone, so an int64 file into a timestamp
		 * column keeps working exactly as it does today.
		 */
		switch (typetag)
		{
			case ARROW_TYPE_Date:
				if (n->kind != A_DATE32)
					imp_temporal_mismatch("a date", n->typid);
				pos = pgc_fb_field(meta, metaLen, tt, 0);	/* unit (i16) */
				n->srcUnit = pos ? fbr_i16(meta, metaLen, pos) : ARROW_DU_MILLI;
				if (n->srcUnit != ARROW_DU_DAY && n->srcUnit != ARROW_DU_MILLI)
					IMPORT_CORRUPT("unknown Arrow DateUnit");
				n->width = (n->srcUnit == ARROW_DU_DAY) ? 4 : 8;
				break;
			case ARROW_TYPE_Time:
				{
					int32		bits;

					if (n->kind != A_TIME64)
						imp_temporal_mismatch("a time", n->typid);
					pos = pgc_fb_field(meta, metaLen, tt, 0);	/* unit (i16) */
					n->srcUnit = pos ? fbr_i16(meta, metaLen, pos) : ARROW_TU_MILLI;
					pos = pgc_fb_field(meta, metaLen, tt, 1);	/* bitWidth (i32) */
					bits = pos ? fbr_i32(meta, metaLen, pos) : 32;
					if (bits != 32 && bits != 64)
						IMPORT_CORRUPT("Arrow Time bitWidth is neither 32 nor 64");

					/*
					 * The spec pairs the two: Time32 is s or ms, Time64 is us or
					 * ns. Refuse a mismatched pair rather than scale a carrier by
					 * a unit that cannot belong to it.
					 */
					if (bits == 32 && n->srcUnit != ARROW_TU_SECOND &&
						n->srcUnit != ARROW_TU_MILLI)
						IMPORT_CORRUPT("Arrow Time32 unit is neither second nor millisecond");
					if (bits == 64 && n->srcUnit != ARROW_TU_MICRO &&
						n->srcUnit != ARROW_TU_NANO)
						IMPORT_CORRUPT("Arrow Time64 unit is neither microsecond nor nanosecond");
					n->width = bits / 8;
					break;
				}
			case ARROW_TYPE_Timestamp:
				if (n->kind != A_TIMESTAMP && n->kind != A_TIMESTAMPTZ)
					imp_temporal_mismatch("a timestamp", n->typid);
				pos = pgc_fb_field(meta, metaLen, tt, 0);	/* unit (i16) */
				n->srcUnit = pos ? fbr_i16(meta, metaLen, pos) : ARROW_TU_SECOND;
				if (n->srcUnit < ARROW_TU_SECOND || n->srcUnit > ARROW_TU_NANO)
					IMPORT_CORRUPT("unknown Arrow TimeUnit");
				n->width = 8;
				break;
			default:
				break;
		}
	}

	/* a list or struct carries its element types as children (Field slot 5) */
	if (n->nchildren > 0)
	{
		uint32		vec;
		uint32		cnt;
		int			i;

		pos = pgc_fb_field(meta, metaLen, field, 5);
		if (pos == 0)
			return;
		vec = pgc_fb_indirect(meta, metaLen, pos);
		cnt = fbr_u32(meta, metaLen, vec);
		if (cnt != (uint32) n->nchildren)
			IMPORT_CORRUPT("Arrow field child count does not match the target type");
		for (i = 0; i < n->nchildren; i++)
			imp_apply_field(&n->children[i], meta, metaLen,
							pgc_fb_indirect(meta, metaLen,
											vec + 4 + (uint32) i * 4));
	}
}

static Datum
imp_scalar_at(ImpNode *n, const uint8 *body, const int64 *bufOff,
			  const int64 *bufLen, int64 i)
{
	if (n->kind == A_BOOL)
	{
		int64		dOff = bufOff[n->dataBuf];

		return BoolGetDatum(((body[dOff + (i >> 3)] >> (i & 7)) & 1) != 0);
	}
	if (n->kind == A_UTF8 || n->kind == A_BINARY)
	{
		int64		oOff = bufOff[n->offBuf];
		int64		dOff = bufOff[n->dataBuf];
		int32		start,
					end,
					vlen;

		memcpy(&start, body + oOff + i * 4, 4);
		memcpy(&end, body + oOff + (i + 1) * 4, 4);
		vlen = end - start;
		if (n->kind == A_BINARY)
		{
			bytea	   *out = (bytea *) palloc(vlen + VARHDRSZ);

			SET_VARSIZE(out, vlen + VARHDRSZ);
			memcpy(VARDATA(out), body + dOff + start, vlen);
			return PointerGetDatum(out);
		}
		else if (n->needsInput)
		{
			char	   *str = palloc(vlen + 1);
			Datum		d;

			memcpy(str, body + dOff + start, vlen);
			str[vlen] = '\0';
			d = InputFunctionCall(&n->inFinfo, str, n->inTypioparam, n->atttypmod);
			pfree(str);
			return d;
		}
		else
		{
			text	   *out = (text *) palloc(vlen + VARHDRSZ);

			SET_VARSIZE(out, vlen + VARHDRSZ);
			memcpy(VARDATA(out), body + dOff + start, vlen);
			return PointerGetDatum(out);
		}
	}
	else
	{
		const uint8 *vp = body + bufOff[n->dataBuf] + i * n->width;

		switch (n->kind)
		{
			case A_INT16:
				{
					int16		v;

					memcpy(&v, vp, 2);
					return Int16GetDatum(v);
				}
			case A_INT32:
				{
					int32		v;

					memcpy(&v, vp, 4);
					return Int32GetDatum(v);
				}
			case A_INT64:
				{
					int64		v;

					memcpy(&v, vp, 8);
					return Int64GetDatum(v);
				}
			case A_FLOAT32:
				{
					float4		v;

					memcpy(&v, vp, 4);
					return Float4GetDatum(v);
				}
			case A_FLOAT64:
				{
					float8		v;

					memcpy(&v, vp, 8);
					return Float8GetDatum(v);
				}
			case A_DATE32:
				{
					int64		days;

					if (n->width == 8)
					{
						int64		ms;

						/* date64: milliseconds from the Unix epoch */
						memcpy(&ms, vp, 8);
						days = ms / ARROW_MSECS_PER_DAY;

						/*
						 * C division truncates toward zero, which names the day
						 * AFTER the one an instant before the epoch falls on.
						 * Floor instead, so every instant reports the date it is in.
						 */
						if (ms % ARROW_MSECS_PER_DAY != 0 && ms < 0)
							days--;
					}
					else
					{
						int32		v;

						memcpy(&v, vp, 4);
						days = v;
					}
					days -= PG_TO_UNIX_DAYS;
					if (!IS_VALID_DATE(days))
						ereport(ERROR,
								(errcode(ERRCODE_DATETIME_FIELD_OVERFLOW),
								 errmsg("columnar: Arrow date value out of range for type date")));
					return DateADTGetDatum((DateADT) days);
				}
			case A_TIME64:
				{
					int64		raw;
					int64		us;

					if (n->width == 4)
					{
						int32		v;

						memcpy(&v, vp, 4);
						raw = v;
					}
					else
						memcpy(&raw, vp, 8);

					/*
					 * TimeADT is microseconds since midnight; nothing else is a
					 * time.
					 *
					 * The sign is tested on the STORED value. Flooring already
					 * carries a negative count to a negative microsecond count, so
					 * this guard is redundant today and reddens no test on its own
					 * -- measured. It is kept because it does not depend on the
					 * narrowing rule: truncation toward zero would take -500ns to
					 * exactly 0, and a check on the scaled result would then store
					 * midnight for a malformed input.
					 */
					if (raw < INT64CONST(0) ||
						!arrow_scale_to_usecs(n->srcUnit, raw, &us) ||
						us > USECS_PER_DAY)
						ereport(ERROR,
								(errcode(ERRCODE_DATETIME_FIELD_OVERFLOW),
								 errmsg("columnar: Arrow time value out of range for type time")));
					return TimeADTGetDatum((TimeADT) us);
				}
			case A_TIMESTAMP:
			case A_TIMESTAMPTZ:
				{
					int64		raw;
					int64		us;
					int64		t;

					memcpy(&raw, vp, 8);
					if (!arrow_scale_to_usecs(n->srcUnit, raw, &us) ||
						pg_sub_s64_overflow(us, PG_TO_UNIX_USECS, &t) ||
						!IS_VALID_TIMESTAMP((Timestamp) t))
						ereport(ERROR,
								(errcode(ERRCODE_DATETIME_FIELD_OVERFLOW),
								 errmsg("columnar: Arrow timestamp value out of range for type %s",
										n->kind == A_TIMESTAMP ? "timestamp"
										: "timestamp with time zone")));
					return TimestampGetDatum((Timestamp) t);
				}
			case A_UUID:
				{
					pg_uuid_t  *uu = palloc(sizeof(pg_uuid_t));

					memcpy(uu->data, vp, UUID_LEN);
					return UUIDPGetDatum(uu);
				}
			case A_DECIMAL128:
				{
					__int128	v;
					char	   *str;

					memcpy(&v, vp, 16);
					str = int128_to_numeric_cstring(v, n->scale);
					return DirectFunctionCall3(numeric_in, CStringGetDatum(str),
											   ObjectIdGetDatum(InvalidOid),
											   Int32GetDatum(n->atttypmod));
				}
			default:
				ereport(ERROR,
						(errcode(ERRCODE_DATA_CORRUPTED),
						 errmsg("columnar.import_arrow: unexpected value kind")));
				return (Datum) 0;
		}
	}
}

/* reconstruct a node's value at index i (recursively for list/struct) */
static Datum
imp_value_at(ImpNode *n, const uint8 *body, const int64 *bufOff,
			 const int64 *bufLen, int64 i, bool *isnull)
{
	*isnull = imp_is_null(body, bufOff[n->validBuf], bufLen[n->validBuf], i);
	if (*isnull)
		return (Datum) 0;

	if (n->kind == A_LIST)
	{
		int64		oOff = bufOff[n->offBuf];
		int32		start,
					end,
					k,
					nelem;
		Datum	   *elems;
		bool	   *enulls;
		ArrayType  *arr;
		int			dims[1],
					lbs[1] = {1};

		memcpy(&start, body + oOff + i * 4, 4);
		memcpy(&end, body + oOff + (i + 1) * 4, 4);
		nelem = end - start;
		elems = (nelem > 0) ? palloc(sizeof(Datum) * nelem) : NULL;
		enulls = (nelem > 0) ? palloc(sizeof(bool) * nelem) : NULL;
		for (k = 0; k < nelem; k++)
			elems[k] = imp_value_at(&n->children[0], body, bufOff, bufLen,
									start + k, &enulls[k]);
		dims[0] = nelem;
		arr = construct_md_array(elems, enulls, 1, dims, lbs, n->elemtype,
								 n->elemlen, n->elembyval, n->elemalign);
		return PointerGetDatum(arr);
	}
	if (n->kind == A_STRUCT)
	{
		Datum	   *fv = palloc(sizeof(Datum) * n->structDesc->natts);
		bool	   *fn = palloc(sizeof(bool) * n->structDesc->natts);
		int			a,
					ci = 0;
		HeapTuple	tup;

		for (a = 0; a < n->structDesc->natts; a++)
		{
			if (TupleDescAttr(n->structDesc, a)->attisdropped)
			{
				fv[a] = (Datum) 0;
				fn[a] = true;
				continue;
			}
			fv[a] = imp_value_at(&n->children[ci++], body, bufOff, bufLen, i, &fn[a]);
		}
		tup = heap_form_tuple(n->structDesc, fv, fn);
		return HeapTupleGetDatum(tup);
	}
	return imp_scalar_at(n, body, bufOff, bufLen, i);
}

/*
 * imp_check_bounds
 *		Reject a record batch whose declared row count or offsets do not fit its
 *		buffers, before any value is read. The buffer (offset, length) pairs are
 *		already checked against the body length by the caller; this checks the
 *		other half, that reading `count` elements -- and, for varlena and list,
 *		the offsets that index the data -- stays inside each buffer.
 *
 *		Without it a crafted RecordBatch `length`, or a crafted offset, drives
 *		imp_scalar_at()/imp_value_at() to memcpy past a buffer and take the
 *		backend down rather than raising an ERROR (issue #214, found by
 *		test/fuzz_arrow.sh: a mutated int64 length made the row loop read row
 *		450,000 of a 200-row buffer). Every read site below reads element i for
 *		i < count, so bounding count against the buffers here bounds all of them.
 *		count is non-negative (checked below) and each size comparison is written
 *		so neither side overflows int64: the buffer-side factor is bounded well
 *		below INT64_MAX/8, and count+1 is taken in uint64. A huge declared count
 *		is rejected, never wrapped past the check (a naive "(count + 7) / 8" or
 *		"count + 1" overflowed for count near INT64_MAX and skipped the check).
 */
static void
imp_check_bounds(ImpNode *n, const uint8 *body, const int64 *bufOff,
				 const int64 *bufLen, int nbuffers, int64 count)
{
	if (count < 0)
		IMPORT_CORRUPT("negative element count");

	/* validity bitmap: count bits, omitted (length 0) when null_count is 0 */
	if (n->validBuf >= 0)
	{
		if (n->validBuf >= nbuffers)
			IMPORT_CORRUPT("validity buffer index out of range");
		if (bufLen[n->validBuf] > 0 && count > (int64) bufLen[n->validBuf] * 8)
			IMPORT_CORRUPT("validity buffer too small for the row count");
	}

	if (n->kind == A_STRUCT)
	{
		int			a,
					ci = 0;

		for (a = 0; a < n->structDesc->natts; a++)
		{
			if (TupleDescAttr(n->structDesc, a)->attisdropped)
				continue;
			imp_check_bounds(&n->children[ci++], body, bufOff, bufLen,
							 nbuffers, count);
		}
		return;
	}

	if (n->kind == A_LIST || n->kind == A_UTF8 || n->kind == A_BINARY)
	{
		int64		oOff;
		int64		k;
		int32		prev = 0;

		if (n->offBuf < 0 || n->offBuf >= nbuffers)
			IMPORT_CORRUPT("offset buffer index out of range");
		if ((uint64) count + 1 > (uint64) (bufLen[n->offBuf] / 4))	/* (count+1) int32 offsets */
			IMPORT_CORRUPT("offset buffer too small for the row count");
		oOff = bufOff[n->offBuf];
		for (k = 0; k <= count; k++)
		{
			int32		off;

			memcpy(&off, body + oOff + k * 4, 4);
			if (off < 0 || off < prev)
				IMPORT_CORRUPT("offsets are not non-negative and non-decreasing");
			prev = off;
		}
		/* prev is now the last offset: the child element count, or the data extent */
		if (n->kind == A_LIST)
			imp_check_bounds(&n->children[0], body, bufOff, bufLen, nbuffers, prev);
		else
		{
			if (n->dataBuf < 0 || n->dataBuf >= nbuffers)
				IMPORT_CORRUPT("data buffer index out of range");
			if ((int64) prev > bufLen[n->dataBuf])
				IMPORT_CORRUPT("string/binary data runs past its buffer");
		}
		return;
	}

	/* fixed-width scalar, including A_BOOL which is one bit per value */
	if (n->dataBuf < 0 || n->dataBuf >= nbuffers)
		IMPORT_CORRUPT("data buffer index out of range");
	if (n->kind == A_BOOL)
	{
		if (count > (int64) bufLen[n->dataBuf] * 8)
			IMPORT_CORRUPT("bool buffer too small for the row count");
		return;
	}
	if (n->width <= 0)
		IMPORT_CORRUPT("non-positive scalar width");
	if (count > bufLen[n->dataBuf] / n->width)
		IMPORT_CORRUPT("value buffer too small for the row count");
}

/*
 * pgcolumnar_import_arrow
 *		SQL: pgcolumnar.import_arrow(rel regclass, path text) -> bigint.
 *		Insert the rows of an Arrow IPC stream file into a columnar table;
 *		returns the number of rows inserted.
 */
Datum
pgcolumnar_import_arrow(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *path;
	Relation	rel;
	TupleDesc	tupdesc;
	int			ncols;
	ImpNode    *tops;
	int			totalBuffers = 0;
	int			fd;
	TupleTableSlot *slot;
	PgColumnarIndexInsertState *indexes;
	CommandId	cid;
	MemoryContext rowCtx;
	int64		total = 0;
	bool		sawSchema = false;
	int			i;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("relation and path must not be null")));
	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read a server file")));

	relid = PG_GETARG_OID(0);
	path = text_to_cstring(PG_GETARG_TEXT_PP(1));

	/*
	 * INSERT on the target, before the file is opened (#559). The server-file
	 * role above governs reading a file; it says nothing about writing this
	 * relation, so without this a pg_read_server_files member could insert into
	 * any columnar table it cannot INSERT into.
	 */
	{
		AclResult	ac = pg_class_aclcheck(relid, GetUserId(), ACL_INSERT);

		if (ac != ACLCHECK_OK)
			aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));
	}

	/* RLS after the ACL check, matching core's ordering (#563). */
	PgColumnarRequireNoRowSecurity(relid);

	rel = table_open(relid, RowExclusiveLock);
	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, RowExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	tupdesc = RelationGetDescr(rel);
	ncols = tupdesc->natts;

	tops = palloc0(sizeof(ImpNode) * ncols);
	for (i = 0; i < ncols; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);
		bool		ok = true;

		if (att->attisdropped)
		{
			table_close(rel, RowExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar.import_arrow does not support dropped columns")));
		}
		imp_build_node(&tops[i], att->atttypid, att->atttypmod, &ok);
		if (!ok)
		{
			table_close(rel, RowExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("column \"%s\" has type %s, which columnar.import_arrow does not support",
							NameStr(att->attname),
							format_type_be(att->atttypid))));
		}
		imp_assign_buffers(&tops[i], &totalBuffers);
	}

	/* refuse a FIFO/non-regular file before the open, same as the Iceberg and
	 * parquet read paths: fopen("rb") on a FIFO blocks in open(2) and the block
	 * survives a cancel/statement_timeout (a DoS). See #644 / #686. */
	/* Race-free local open (rejects a FIFO without a stat-before-open window);
	 * it raises on failure, and the RowExclusiveLock releases at abort. */
	fd = PgColumnarOpenLocalRegularFile(path, NULL);

	slot = table_slot_create(rel, NULL);
	cid = GetCurrentCommandId(true);
	indexes = PgColumnarRelationHasIndexes(rel)
		? PgColumnarIndexInsertBegin(rel, true) : NULL;

	/*
	 * Per-row scratch context. Reconstructing a nested value (array/composite)
	 * allocates several objects per row; without resetting, a large file would
	 * accumulate O(rows) transient memory. table_tuple_insert copies the tuple
	 * into the write state's own context, so the row context is safe to reset
	 * after each insert.
	 */
	rowCtx = AllocSetContextCreate(CurrentMemoryContext,
								   "columnar import row",
								   ALLOCSET_DEFAULT_SIZES);

	for (;;)
	{
		uint32		first;
		uint32		metaLen;
		uint8	   *meta;
		int64		bodyLength;
		uint8	   *body = NULL;
		uint32		msg,
					hdr,
					pos;
		uint8		headerType;

		/* message framing: [0xFFFFFFFF] [metaLen] or [metaLen] (legacy) */
		if (!PgColumnarReadExact(fd, &first, 4))
			break;				/* clean EOF */
		if (first == 0xFFFFFFFF)
		{
			if (!PgColumnarReadExact(fd, &metaLen, 4))
				IMPORT_CORRUPT("truncated continuation");
		}
		else
			metaLen = first;
		if (metaLen == 0)
			break;				/* end-of-stream marker */

		meta = palloc(metaLen);
		if (!PgColumnarReadExact(fd, meta, metaLen))
			IMPORT_CORRUPT("truncated metadata");

		msg = pgc_fb_indirect(meta, metaLen, 0);
		pos = pgc_fb_field(meta, metaLen, msg, 1);	/* header_type (u8) */
		headerType = pos ? fbr_u8(meta, metaLen, pos) : 0;
		pos = pgc_fb_field(meta, metaLen, msg, 2);	/* header (offset) */
		hdr = pos ? pgc_fb_indirect(meta, metaLen, pos) : 0;
		pos = pgc_fb_field(meta, metaLen, msg, 3);	/* bodyLength (i64) */
		bodyLength = pos ? fbr_i64(meta, metaLen, pos) : 0;
		if (bodyLength < 0)
			IMPORT_CORRUPT("negative body length");

		if (bodyLength > 0)
		{
			body = palloc((Size) bodyLength);
			if (!PgColumnarReadExact(fd, body, (size_t) bodyLength))
				IMPORT_CORRUPT("truncated body");
		}

		if (headerType == ARROW_MSG_Schema)
		{
			uint32		fieldsVecPos = hdr ? pgc_fb_field(meta, metaLen, hdr, 1) : 0;
			uint32		fieldsVec = fieldsVecPos ?
				pgc_fb_indirect(meta, metaLen, fieldsVecPos) : 0;
			uint32		nfields = fieldsVec ? fbr_u32(meta, metaLen, fieldsVec) : 0;

			if ((int) nfields != ncols)
				ereport(ERROR,
						(errcode(ERRCODE_DATATYPE_MISMATCH),
						 errmsg("Arrow file has %u columns, target table has %d",
								nfields, ncols)));

			/*
			 * Take the carrier width and temporal unit from the file. Before this
			 * the Schema was only counted, so every temporal column was decoded as
			 * whatever the target type happened to be (#864, #865).
			 */
			for (i = 0; i < ncols; i++)
				imp_apply_field(&tops[i], meta, metaLen,
								pgc_fb_indirect(meta, metaLen,
												fieldsVec + 4 + (uint32) i * 4));
			sawSchema = true;
		}
		else if (headerType == ARROW_MSG_RecordBatch)
		{
			int64		nrows;
			uint32		nodesVecPos,
						buffersVecPos,
						buffersVec;
			uint32		nbuffers;

			if (!sawSchema)
				IMPORT_CORRUPT("RecordBatch before Schema");
			if (!hdr)
				IMPORT_CORRUPT("missing RecordBatch header");
			if (pgc_fb_field(meta, metaLen, hdr, 3) != 0)
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("columnar.import_arrow does not support compressed Arrow bodies")));

			pos = pgc_fb_field(meta, metaLen, hdr, 0);	/* length */
			nrows = pos ? fbr_i64(meta, metaLen, pos) : 0;
			nodesVecPos = pgc_fb_field(meta, metaLen, hdr, 1);
			buffersVecPos = pgc_fb_field(meta, metaLen, hdr, 2);
			buffersVec = buffersVecPos ? pgc_fb_indirect(meta, metaLen, buffersVecPos) : 0;
			nbuffers = buffersVec ? fbr_u32(meta, metaLen, buffersVec) : 0;
			(void) nodesVecPos;

			/* read every buffer's (offset,length) once, then reconstruct each
			 * row's columns from the field tree (handles nested list/struct) */
			{
				int64	   *bufOff;
				int64	   *bufLen;
				int			b;
				int64		r;

				if ((int) nbuffers < totalBuffers)
					IMPORT_CORRUPT("too few buffers for the schema");
				bufOff = palloc(sizeof(int64) * Max(nbuffers, 1));
				bufLen = palloc(sizeof(int64) * Max(nbuffers, 1));
				for (b = 0; b < (int) nbuffers; b++)
				{
					uint32		base = buffersVec + 4 + (uint32) b * 16;

					bufOff[b] = fbr_i64(meta, metaLen, base);
					bufLen[b] = fbr_i64(meta, metaLen, base + 8);
					if (bufOff[b] < 0 || bufLen[b] < 0 ||
						(uint64) bufOff[b] + bufLen[b] > (uint64) bodyLength)
						IMPORT_CORRUPT("buffer out of range");
				}

				/*
				 * Reject a batch whose row count or offsets overrun its buffers
				 * before reading any value from them (issue #214).
				 */
				for (i = 0; i < ncols; i++)
					imp_check_bounds(&tops[i], body, bufOff, bufLen,
									 (int) nbuffers, nrows);

				for (r = 0; r < nrows; r++)
				{
					MemoryContext oldCtx;

					CHECK_FOR_INTERRUPTS();
					ExecClearTuple(slot);
					oldCtx = MemoryContextSwitchTo(rowCtx);
					for (i = 0; i < ncols; i++)
					{
						bool		isnull;

						slot->tts_values[i] = imp_value_at(&tops[i], body, bufOff,
														   bufLen, r, &isnull);
						slot->tts_isnull[i] = isnull;
					}
					ExecStoreVirtualTuple(slot);
					table_tuple_insert(rel, slot, cid, 0, NULL);

					/*
					 * table_tuple_insert writes the row and nothing else: index
					 * maintenance belongs to the executor and there is none
					 * here. Without this the imported rows are invisible to
					 * every index scan and a unique index accepts duplicates
					 * (issue #153). tts_tid carries the assigned row number.
					 */
					if (indexes != NULL)
						PgColumnarIndexInsertRow(indexes, rel, slot->tts_values,
											   slot->tts_isnull,
											   PgColumnarItemPointerToRowNumber(&slot->tts_tid));
					MemoryContextSwitchTo(oldCtx);
					MemoryContextReset(rowCtx);
					total++;
				}
			}
		}
		else if (headerType == ARROW_MSG_DictionaryBatch)
		{
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar.import_arrow does not support dictionary-encoded Arrow files")));
		}
		/* any other message type is ignored */

		if (body)
			pfree(body);
		pfree(meta);
	}

	CloseTransientFile(fd);
	MemoryContextDelete(rowCtx);
	if (indexes != NULL)
		PgColumnarIndexInsertEnd(indexes);

	ExecDropSingleTupleTableSlot(slot);
	/*
	 * NoLock, not RowExclusiveLock: the lock has to outlive this function. A
	 * deferred constraint queued by the load is fired at commit, and the
	 * trigger machinery reopens the relation with NoLock on the assumption that
	 * whoever queued the event still holds one (#168). Dropping it here aborts
	 * an assert build in relation_open and is undefined on a non-assert one.
	 * The lock is released with the transaction, as it is for an ordinary
	 * INSERT.
	 */
	table_close(rel, NoLock);
	PG_RETURN_INT64(total);
}

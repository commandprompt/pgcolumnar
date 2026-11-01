/*-------------------------------------------------------------------------
 *
 * pgcolumnar_parquet.c
 *		Parquet file export for pgColumnar (gap 27, piece 2).
 *
 *		pgcolumnar.export_parquet(rel regclass, path text) writes a columnar table
 *		to a Parquet file. The writer is self-contained -- it emits the Thrift
 *		compact-protocol metadata and PLAIN-encoded, UNCOMPRESSED data pages
 *		directly -- so there is no libparquet build or run-time dependency. Rows
 *		are read in physical order via the scalar reader; one row group is
 *		emitted per PARQUET_ROWGROUP_ROWS rows, with one DATA_PAGE per column.
 *
 *		First-slice type mapping (matches pgcolumnar.export_arrow): int2/int4 ->
 *		INT32 (int2 tagged INT_16), int8 -> INT64, float4 -> FLOAT, float8 ->
 *		DOUBLE, bool -> BOOLEAN, text/varchar -> BYTE_ARRAY (UTF8), bytea ->
 *		BYTE_ARRAY. All columns are OPTIONAL; nulls are carried in definition
 *		levels. Little-endian hosts only.
 *
 * Independent MIT implementation built from the Apache Parquet format and
 * Thrift compact-protocol specifications and the public PostgreSQL API only.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"
#include "columnar_objstore.h"
#include "columnar_parquet_format.h"
#include "columnar_sink.h"
#include "columnar_thrift.h"

/* isnan(), for the float statistics rules. PostgreSQL's own headers happen to
 * reach it on 15 through 18 and do not on 19, where the build fails outright. */
#include <math.h>

#include "fmgr.h"
#include "access/detoast.h"
#include "access/htup_details.h"
#include "access/relation.h"
#include "access/table.h"
#include "catalog/pg_type.h"
#include "lib/stringinfo.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_foreign_server.h"
#include "foreign/foreign.h"
#include "miscadmin.h"
#include "utils/acl.h"
#include "storage/fd.h"
#include "utils/builtins.h"
#include "utils/date.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/numeric.h"
#include "utils/array.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/timestamp.h"
#include "utils/typcache.h"
#include "utils/uuid.h"

PG_FUNCTION_INFO_V1(pgcolumnar_export_parquet);

#define PARQUET_ROWGROUP_ROWS 65536

/* PostgreSQL epoch (2000-01-01) to Unix epoch (1970-01-01) offsets */
#define PG_TO_UNIX_DAYS		((int64) (POSTGRES_EPOCH_JDATE - UNIX_EPOCH_JDATE))
#define PG_TO_UNIX_USECS	(PG_TO_UNIX_DAYS * USECS_PER_DAY)

typedef enum ParquetKind
{
	P_INT16,
	P_INT32,
	P_INT64,
	P_FLOAT,
	P_DOUBLE,
	P_BOOL,
	P_UTF8,
	P_BINARY,
	P_DATE,						/* date -> INT32 (days from Unix epoch) */
	P_TIME,						/* time -> INT64 TIME_MICROS */
	P_TIMESTAMP,				/* timestamp/tz -> INT64 TIMESTAMP(MICROS) */
	P_UUID,						/* uuid -> FIXED_LEN_BYTE_ARRAY(16) */
	P_DECIMAL					/* numeric(p,s) -> FIXED_LEN_BYTE_ARRAY(16) */
}			ParquetKind;

/* Parse a numeric value (via its text form) into a 128-bit unscaled integer at
 * the given scale. Returns false for NaN/Infinity, which a decimal cannot hold. */
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


/*
 * Per-row-group statistics for one leaf, i.e. what goes in ColumnMetaData's
 * Statistics (field 12). A reader skips a row group by proving its predicate
 * cannot hold anywhere in [min_value, max_value], so a wrong bound loses rows
 * silently -- the executor's recheck never sees a group that emitted nothing.
 * Everything here is therefore conservative:
 *
 *   * Bounds are written only for the fixed-width physical types whose defined
 *     sort order is the signed one we compare in: INT32, INT64, FLOAT and
 *     DOUBLE. BOOLEAN, BYTE_ARRAY and FIXED_LEN_BYTE_ARRAY get null_count and
 *     nothing else. UTF8 sorts by unsigned bytes, which is not any PostgreSQL
 *     collation, and a bound in the wrong order is worse than no bound.
 *   * Values are accumulated in PHYSICAL space, inside write_leaf_value, from
 *     the same expression that writes the data page. Deriving them a second
 *     time is how a bound comes to disagree with the column by an epoch.
 *   * A value with no Parquet representation (date and timestamp infinity, a
 *     numeric too wide for its DECIMAL) is folded to null before it gets here,
 *     so it counts as a null and cannot reach a bound.
 *   * parquet.thrift's TYPE_ORDER rules for floats: NaN is never a bound,
 *     nan_count is always written, a column whose non-null values are all NaN
 *     gets no bounds at all, and a computed zero is written -0.0 as a minimum
 *     and +0.0 as a maximum.
 */
typedef struct PqStats
{
	bool		hasBounds;		/* a non-NaN value has been seen */
	int64		nulls;			/* nulls, folded values included */
	int64		nans;			/* NaN values (float and double only) */
	int64		iMin;			/* INT32/INT64 bounds, held widened */
	int64		iMax;
	float8		fMin;			/* FLOAT/DOUBLE bounds, held widened */
	float8		fMax;
}			PqStats;

/*
 * A leaf column is one Parquet column chunk: a scalar column, an array's element,
 * or one field of a composite. Nesting is expressed through repetition and
 * definition levels (the Dremel model). For a plain scalar, max_rep is 0 and
 * max_def is 1, so the encoding is byte-identical to the flat writer.
 */
typedef struct PqLeaf
{
	const char *path[3];		/* schema path from the top column to the leaf */
	int			pathlen;
	ParquetKind kind;
	int			ptype;			/* Parquet physical type */
	int			convType;		/* ConvertedType, or -1 */
	int			tsAdjusted;		/* TIMESTAMP's isAdjustedToUTC (1 or 0), or -1 */
	int			typeLength;		/* FIXED_LEN_BYTE_ARRAY length, else 0 */
	int			precision;
	int			scale;
	bool		convertText;
	FmgrInfo	outFinfo;
	int			max_def;
	int			max_rep;
	StringInfoData defs;		/* one byte per level entry (definition level) */
	StringInfoData reps;		/* one byte per level entry (repetition level) */
	StringInfoData values;		/* PLAIN values, non-null only */
	StringInfoData boolbits;	/* 1 byte per non-null bool value */
	int64		nEntries;		/* level entries in the current row group */
	PqStats		st;				/* statistics for the current row group */
	PgcTextQueue pending;		/* this row's texts, made by pq_row_bytes */
}			PqLeaf;

/* how a top-level column shreds into leaves */
typedef enum
{
	TOP_SCALAR,
	TOP_LIST,					/* 1-D array -> LIST of one scalar element */
	TOP_STRUCT					/* composite -> group of scalar fields */
}			TopKind;

typedef struct TopColumn
{
	char	   *name;
	TopKind		tkind;
	int			firstLeaf;		/* index of the first leaf in leaves[] */
	int			nleaves;		/* 1 for scalar/list, #fields for struct */
	/* list element decode */
	Oid			elemtype;
	int16		elemlen;
	bool		elembyval;
	char		elemalign;
	/* struct */
	TupleDesc	structDesc;
	int		   *fieldLeaf;		/* [structDesc->natts] leaf index, -1 if dropped */
}			TopColumn;

typedef struct PqColMeta
{
	int64		dataPageOffset;
	int64		totalSize;		/* page header + body */
	int64		numValues;		/* rows */
	PqStats		st;				/* this group's statistics, taken at flush */
}			PqColMeta;

typedef struct PqRowGroup
{
	PqColMeta  *cols;
	int64		totalByteSize;
	int64		numRows;
}			PqRowGroup;

/*
 * Reset one leaf for the next row group. The statistics reset here rather than
 * per file: a bound left over from the previous group would describe rows that
 * are not in this one, and a file-wide bound on every chunk is a file that can
 * never be skipped.
 */
static void
pqstats_reset(PqStats *st)
{
	st->hasBounds = false;
	st->nulls = 0;
	st->nans = 0;
	st->iMin = st->iMax = 0;
	st->fMin = st->fMax = 0;
}

/* One non-null INT32/INT64 physical value, in physical space. */
static inline void
pqstats_int(PqStats *st, int64 v)
{
	if (!st->hasBounds)
	{
		st->iMin = st->iMax = v;
		st->hasBounds = true;
	}
	else
	{
		if (v < st->iMin)
			st->iMin = v;
		if (v > st->iMax)
			st->iMax = v;
	}
}

/*
 * One non-null FLOAT/DOUBLE value. A NaN is counted and discarded: parquet.thrift
 * requires that a bound, when present, be computed from non-NaN values only, so
 * that a reader may trust it for the non-NaN rows.
 */
static inline void
pqstats_float(PqStats *st, float8 v)
{
	if (isnan(v))
	{
		st->nans++;
		return;
	}
	if (!st->hasBounds)
	{
		st->fMin = st->fMax = v;
		st->hasBounds = true;
	}
	else
	{
		if (v < st->fMin)
			st->fMin = v;
		if (v > st->fMax)
			st->fMax = v;
	}
}

static void
pqleaf_reset(PqLeaf *c)
{
	resetStringInfo(&c->defs);
	resetStringInfo(&c->reps);
	resetStringInfo(&c->values);
	resetStringInfo(&c->boolbits);
	pqstats_reset(&c->st);
	c->nEntries = 0;
}

static ParquetKind
parquet_kind_for_type(Oid typid, int32 typmod, int *ptype, int *convType,
					  int *typeLength, int *precision, int *scale)
{
	*convType = -1;
	*typeLength = 0;
	*precision = 0;
	*scale = 0;
	switch (typid)
	{
		case INT2OID:
			*ptype = PQ_INT32;
			*convType = PQ_CT_INT_16;
			return P_INT16;
		case INT4OID:
			*ptype = PQ_INT32;
			return P_INT32;
		case INT8OID:
			*ptype = PQ_INT64;
			return P_INT64;
		case FLOAT4OID:
			*ptype = PQ_FLOAT;
			return P_FLOAT;
		case FLOAT8OID:
			*ptype = PQ_DOUBLE;
			return P_DOUBLE;
		case BOOLOID:
			*ptype = PQ_BOOLEAN;
			return P_BOOL;
		case TEXTOID:
		case VARCHAROID:
		case JSONOID:
		case JSONBOID:
			*ptype = PQ_BYTE_ARRAY;
			*convType = PQ_CT_UTF8;
			return P_UTF8;
		case BYTEAOID:
			*ptype = PQ_BYTE_ARRAY;
			return P_BINARY;
		case DATEOID:
			*ptype = PQ_INT32;
			*convType = PQ_CT_DATE;
			return P_DATE;
		case TIMEOID:
			*ptype = PQ_INT64;
			*convType = PQ_CT_TIME_MICROS;
			return P_TIME;
		case TIMESTAMPOID:
			/*
			 * A wall time with no zone: the TIMESTAMP logical type with
			 * isAdjustedToUTC=false, written in write_schema_leaf. NO converted
			 * type, because TIMESTAMP_MICROS means adjusted-to-UTC, and every
			 * other reader took this column for a UTC instant (#1706).
			 */
			*ptype = PQ_INT64;
			return P_TIMESTAMP;
		case TIMESTAMPTZOID:
			/* an instant: isAdjustedToUTC=true, and the legacy converted type
			 * that means the same, for readers older than logical types */
			*ptype = PQ_INT64;
			*convType = PQ_CT_TIMESTAMP_MICROS;
			return P_TIMESTAMP;
		case UUIDOID:
			*ptype = PQ_FIXED_LEN_BYTE_ARRAY;
			*typeLength = 16;
			return P_UUID;
		case NUMERICOID:
			/* numeric(p,s) with p<=38 and 0<=s<=p -> DECIMAL in a 16-byte
			 * FIXED_LEN_BYTE_ARRAY; otherwise fall back to text. */
			if (typmod >= (int32) VARHDRSZ)
			{
				int32		tmp = typmod - VARHDRSZ;
				int			p = (tmp >> 16) & 0xffff;
				int			s = tmp & 0xffff;

				if (p >= 1 && p <= 38 && s >= 0 && s <= p)
				{
					*ptype = PQ_FIXED_LEN_BYTE_ARRAY;
					*typeLength = 16;
					*convType = PQ_CT_DECIMAL;
					*precision = p;
					*scale = s;
					return P_DECIMAL;
				}
			}
			*ptype = PQ_BYTE_ARRAY;
			*convType = PQ_CT_UTF8;
			return P_UTF8;			/* text fallback */
		default:
			*ptype = -1;
			return P_INT32;
	}
}

/* whether a value has a Parquet representation (else it is folded to null) */
static bool
leaf_value_representable(PqLeaf *c, Datum d, __int128 *dec)
{
	switch (c->kind)
	{
		case P_DATE:
			return !DATE_NOT_FINITE(DatumGetDateADT(d));
		case P_TIMESTAMP:
			return !TIMESTAMP_NOT_FINITE(DatumGetTimestamp(d));
		case P_DECIMAL:
			return numeric_to_int128(d, c->scale, dec);
		default:
			return true;
	}
}

/* append one PLAIN value to a leaf's value buffer */
static void
write_leaf_value(PqLeaf *c, Datum d, __int128 dec)
{
	switch (c->kind)
	{
		case P_INT16:
			{
				int32		v = (int32) DatumGetInt16(d);

				appendBinaryStringInfo(&c->values, (char *) &v, 4);
				pqstats_int(&c->st, v);
				break;
			}
		case P_INT32:
			{
				int32		v = DatumGetInt32(d);

				appendBinaryStringInfo(&c->values, (char *) &v, 4);
				pqstats_int(&c->st, v);
				break;
			}
		case P_INT64:
			{
				int64		v = DatumGetInt64(d);

				appendBinaryStringInfo(&c->values, (char *) &v, 8);
				pqstats_int(&c->st, v);
				break;
			}
		case P_FLOAT:
			{
				float4		v = DatumGetFloat4(d);

				appendBinaryStringInfo(&c->values, (char *) &v, 4);
				pqstats_float(&c->st, (float8) v);
				break;
			}
		case P_DOUBLE:
			{
				float8		v = DatumGetFloat8(d);

				appendBinaryStringInfo(&c->values, (char *) &v, 8);
				pqstats_float(&c->st, v);
				break;
			}
		case P_DATE:
			{
				int32		v = (int32) (DatumGetDateADT(d) + PG_TO_UNIX_DAYS);

				appendBinaryStringInfo(&c->values, (char *) &v, 4);
				pqstats_int(&c->st, v);
				break;
			}
		case P_TIME:
			{
				int64		v = (int64) DatumGetTimeADT(d);

				appendBinaryStringInfo(&c->values, (char *) &v, 8);
				pqstats_int(&c->st, v);
				break;
			}
		case P_TIMESTAMP:
			{
				int64		v = (int64) DatumGetTimestamp(d) + PG_TO_UNIX_USECS;

				appendBinaryStringInfo(&c->values, (char *) &v, 8);
				pqstats_int(&c->st, v);
				break;
			}
		case P_UUID:
			appendBinaryStringInfo(&c->values,
								   (char *) DatumGetUUIDP(d)->data, UUID_LEN);
			break;
		case P_DECIMAL:
			{
				/* big-endian two's complement, 16 bytes */
				char		be[16];
				char	   *le = (char *) &dec;
				int			j;

				for (j = 0; j < 16; j++)
					be[j] = le[15 - j];
				appendBinaryStringInfo(&c->values, be, 16);
				break;
			}
		case P_BOOL:
			appendStringInfoChar(&c->boolbits, DatumGetBool(d) ? 1 : 0);
			break;
		case P_UTF8:
		case P_BINARY:
			if (c->convertText)
			{
				/*
				 * numeric/jsonb fallback: canonical text via output function,
				 * or the text pq_row_bytes already made for this row
				 */
				int			plen = 0;
				char	   *str = PgcTextQueuePop(&c->pending, d, &plen);
				int32		len = plen;

				if (str == NULL)
				{
					str = OutputFunctionCall(&c->outFinfo, d);
					len = (int32) strlen(str);
				}

				appendBinaryStringInfo(&c->values, (char *) &len, 4);
				if (len > 0)
					appendBinaryStringInfo(&c->values, str, len);
				pfree(str);
			}
			else
			{
				struct varlena *v = PG_DETOAST_DATUM_PACKED(d);
				int32		len = VARSIZE_ANY_EXHDR(v);

				appendBinaryStringInfo(&c->values, (char *) &len, 4);
				if (len > 0)
					appendBinaryStringInfo(&c->values, VARDATA_ANY(v), len);
			}
			break;
	}
}

/*
 * Append one Dremel entry to a leaf: its definition and repetition levels, and
 * the PLAIN value when present. A present value with no Parquet representation
 * is folded to the "container present, value absent" level (max_def - 1).
 */
static void
leaf_entry(PqLeaf *c, int def, int rep, bool hasValue, Datum d)
{
	__int128	dec = 0;

	if (hasValue && !leaf_value_representable(c, d, &dec))
	{
		hasValue = false;
		def = c->max_def - 1;
	}
	appendStringInfoChar(&c->defs, (char) def);
	if (c->max_rep > 0)
		appendStringInfoChar(&c->reps, (char) rep);
	c->nEntries++;
	/*
	 * The null count is taken here rather than beside the accumulators, because
	 * this is where a value that has no Parquet representation has just become a
	 * null. Counting it in write_leaf_value would miss exactly those rows, and
	 * miss them in the direction that makes the file look denser than it is.
	 */
	if (hasValue)
		write_leaf_value(c, d, dec);
	else
		c->st.nulls++;
}

/* shred one top-level column value for a row into its leaf/leaves */
static void
shred_top(TopColumn *tc, PqLeaf *leaves, Datum d, bool isnull)
{
	switch (tc->tkind)
	{
		case TOP_SCALAR:
			leaf_entry(&leaves[tc->firstLeaf], isnull ? 0 : 1, 0, !isnull, d);
			break;
		case TOP_LIST:
			{
				PqLeaf	   *leaf = &leaves[tc->firstLeaf];

				if (isnull)
					leaf_entry(leaf, 0, 0, false, (Datum) 0);
				else
				{
					ArrayType  *arr = DatumGetArrayTypeP(d);
					Datum	   *elems;
					bool	   *enulls;
					int			n;
					int			k;

					if (ARR_NDIM(arr) > 1)
						ereport(ERROR,
								(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
								 errmsg("columnar.export_parquet does not support multi-dimensional arrays")));
					deconstruct_array(arr, tc->elemtype, tc->elemlen,
									  tc->elembyval, tc->elemalign, &elems, &enulls, &n);
					if (n == 0)
						leaf_entry(leaf, 1, 0, false, (Datum) 0);	/* empty list */
					else
						for (k = 0; k < n; k++)
							leaf_entry(leaf, enulls[k] ? 2 : 3, k == 0 ? 0 : 1,
									   !enulls[k], elems[k]);
				}
				break;
			}
		case TOP_STRUCT:
			{
				Datum	   *fv = NULL;
				bool	   *fn = NULL;
				int			a;

				if (!isnull)
				{
					HeapTupleHeader th = DatumGetHeapTupleHeader(d);
					HeapTupleData tmp;

					fv = palloc(sizeof(Datum) * tc->structDesc->natts);
					fn = palloc(sizeof(bool) * tc->structDesc->natts);
					tmp.t_len = HeapTupleHeaderGetDatumLength(th);
					ItemPointerSetInvalid(&tmp.t_self);
					tmp.t_tableOid = InvalidOid;
					tmp.t_data = th;
					heap_deform_tuple(&tmp, tc->structDesc, fv, fn);
				}
				for (a = 0; a < tc->structDesc->natts; a++)
				{
					int			li = tc->fieldLeaf[a];

					if (li < 0)
						continue;	/* dropped field */
					if (isnull)
						leaf_entry(&leaves[li], 0, 0, false, (Datum) 0);
					else if (fn[a])
						leaf_entry(&leaves[li], 1, 0, false, (Datum) 0);
					else
						leaf_entry(&leaves[li], 2, 0, true, fv[a]);
				}
				break;
			}
	}
}


/*
 * Build an RLE/bit-packed hybrid level section (one bit-packed run at the given
 * bit width), prefixed with its int32 LE byte length (DataPage v1 convention).
 * With bit width 1 this is byte-identical to the flat writer's def levels.
 */
static void
build_rle_levels(StringInfo out, const uint8 *levels, int64 n, int bit_width)
{
	StringInfoData h;
	int64		ngroups = (n + 7) / 8;
	int64		nbytes = ngroups * bit_width;
	uint8	   *packed = palloc0(Max(nbytes, 1));
	int64		i;
	int32		len;

	initStringInfo(&h);
	PgColumnarThriftPutVarint(&h, (uint64) ((ngroups << 1) | 1));	/* one bit-packed run */
	for (i = 0; i < n; i++)
	{
		uint32		v = levels[i];
		int			b;

		for (b = 0; b < bit_width; b++)
			if (v & (1u << b))
			{
				int64		abs = i * bit_width + b;

				packed[abs >> 3] |= (1 << (abs & 7));
			}
	}
	appendBinaryStringInfo(&h, (char *) packed, nbytes);
	len = h.len;
	appendBinaryStringInfo(out, (char *) &len, 4);
	appendBinaryStringInfo(out, h.data, h.len);
	pfree(packed);
	pfree(h.data);
}

/* assemble the PLAIN values buffer for a leaf (from its accumulators) */
static void
build_values(StringInfo out, PqLeaf *c)
{
	if (c->kind == P_BOOL)
	{
		int64		n = c->boolbits.len;
		int64		nbytes = (n + 7) / 8;
		char	   *bits = palloc0(nbytes);
		int64		i;

		for (i = 0; i < n; i++)
			if (c->boolbits.data[i])
				bits[i >> 3] |= (1 << (i & 7));
		appendBinaryStringInfo(out, bits, nbytes);
		pfree(bits);
	}
	else
		appendBinaryStringInfo(out, c->values.data, c->values.len);
}

/* build a data page body for a leaf: [rep levels][def levels][values] */
static void
build_leaf_body(StringInfo body, PqLeaf *c)
{
	if (c->max_rep > 0)
		build_rle_levels(body, (const uint8 *) c->reps.data, c->nEntries,
						 pq_bits_for(c->max_rep));
	build_rle_levels(body, (const uint8 *) c->defs.data, c->nEntries,
					 pq_bits_for(c->max_def));
	build_values(body, c);
}

/* write a DATA_PAGE PageHeader (Thrift) for a page of body_size bytes */
static void
write_page_header(StringInfo out, int64 nrows, int32 body_size)
{
	int16		last = 0;
	int16		dlast = 0;

	/* PageHeader */
	PgColumnarThriftPutI32Field(out, &last, 1, 0);			/* type = DATA_PAGE */
	PgColumnarThriftPutI32Field(out, &last, 2, body_size); /* uncompressed_page_size */
	PgColumnarThriftPutI32Field(out, &last, 3, body_size); /* compressed_page_size */
	/* field 5: data_page_header (struct) */
	PgColumnarThriftPutField(out, &last, 5, TC_STRUCT);
	PgColumnarThriftPutI32Field(out, &dlast, 1, (int32) nrows); /* num_values */
	PgColumnarThriftPutI32Field(out, &dlast, 2, PQ_ENC_PLAIN);	/* encoding */
	PgColumnarThriftPutI32Field(out, &dlast, 3, PQ_ENC_RLE);	/* def level encoding */
	PgColumnarThriftPutI32Field(out, &dlast, 4, PQ_ENC_RLE);	/* rep level encoding */
	PgColumnarThriftPutStop(out);								/* end data_page_header */
	PgColumnarThriftPutStop(out);								/* end PageHeader */
}

/* ---- FileMetaData footer ---- */
static void
write_schema_element_root(StringInfo b, int ncols)
{
	int16		last = 0;

	PgColumnarThriftPutStringField(b, &last, 4, "schema", 6);	/* name */
	PgColumnarThriftPutI32Field(b, &last, 5, ncols);			/* num_children */
	PgColumnarThriftPutStop(b);
}

/* one leaf SchemaElement (a primitive) */
static void
write_schema_leaf(StringInfo b, const char *name, PqLeaf *leaf, int repetition)
{
	int16		last = 0;

	PgColumnarThriftPutI32Field(b, &last, 1, leaf->ptype);	/* type */
	if (leaf->ptype == PQ_FIXED_LEN_BYTE_ARRAY)
		PgColumnarThriftPutI32Field(b, &last, 2, leaf->typeLength);
	PgColumnarThriftPutI32Field(b, &last, 3, repetition);
	PgColumnarThriftPutStringField(b, &last, 4, name, (int) strlen(name));
	if (leaf->convType >= 0)
		PgColumnarThriftPutI32Field(b, &last, 6, leaf->convType);
	if (leaf->convType == PQ_CT_DECIMAL)
	{
		PgColumnarThriftPutI32Field(b, &last, 7, leaf->scale);
		PgColumnarThriftPutI32Field(b, &last, 8, leaf->precision);
	}
	if (leaf->tsAdjusted >= 0)
	{
		/*
		 * logicalType (10): LogicalType union, member TIMESTAMP (8):
		 * TimestampType { 1: isAdjustedToUTC, 2: unit TimeUnit { 2: MICROS {} } }.
		 * A bool's value is its compact-protocol field type, so it has no body.
		 */
		int16		lt = 0,
					ts = 0,
					unit = 0;

		PgColumnarThriftPutField(b, &last, 10, TC_STRUCT);
		PgColumnarThriftPutField(b, &lt, PQ_LT_TIMESTAMP, TC_STRUCT);
		PgColumnarThriftPutField(b, &ts, 1, leaf->tsAdjusted ? TC_BOOL_TRUE : TC_BOOL_FALSE);
		PgColumnarThriftPutField(b, &ts, 2, TC_STRUCT);
		PgColumnarThriftPutField(b, &unit, PQ_TUNIT_MICROS, TC_STRUCT);
		PgColumnarThriftPutStop(b);		/* MicroSeconds {} */
		PgColumnarThriftPutStop(b);		/* TimeUnit */
		PgColumnarThriftPutStop(b);		/* TimestampType */
		PgColumnarThriftPutStop(b);		/* LogicalType */
	}
	PgColumnarThriftPutStop(b);
}

/* one group SchemaElement (no physical type; has num_children) */
static void
write_schema_group(StringInfo b, const char *name, int repetition,
				   int num_children, int convType)
{
	int16		last = 0;

	PgColumnarThriftPutI32Field(b, &last, 3, repetition);
	PgColumnarThriftPutStringField(b, &last, 4, name, (int) strlen(name));
	PgColumnarThriftPutI32Field(b, &last, 5, num_children);
	if (convType >= 0)
		PgColumnarThriftPutI32Field(b, &last, 6, convType);
	PgColumnarThriftPutStop(b);
}

/* number of SchemaElements a top column contributes (excluding the root) */
static int
schema_count_for_top(TopColumn *tc)
{
	switch (tc->tkind)
	{
		case TOP_LIST:
			return 3;			/* group(LIST), group(list), element */
		case TOP_STRUCT:
			return 1 + tc->nleaves;
		default:
			return 1;
	}
}

/* emit a top column's schema subtree (pre-order) */
static void
write_top_schema(StringInfo b, TopColumn *tc, PqLeaf *leaves)
{
	switch (tc->tkind)
	{
		case TOP_SCALAR:
			write_schema_leaf(b, tc->name, &leaves[tc->firstLeaf], 1);
			break;
		case TOP_LIST:
			write_schema_group(b, tc->name, 1, 1, 3 /* LIST */);
			write_schema_group(b, "list", 2 /* REPEATED */, 1, -1);
			write_schema_leaf(b, "element", &leaves[tc->firstLeaf], 1);
			break;
		case TOP_STRUCT:
			{
				int			a;

				write_schema_group(b, tc->name, 1, tc->nleaves, -1);
				for (a = 0; a < tc->structDesc->natts; a++)
				{
					int			li = tc->fieldLeaf[a];

					if (li < 0)
						continue;
					write_schema_leaf(b,
									  NameStr(TupleDescAttr(tc->structDesc, a)->attname),
									  &leaves[li], 1);
				}
				break;
			}
	}
}

/*
 * Write ColumnMetaData's Statistics (field 12) for one column chunk.
 *
 * null_count is written for every chunk, including where it is zero:
 * parquet.thrift says a reader MUST NOT assume null_count == 0 when the field is
 * absent, so omitting it on a column with no nulls tells the reader less than
 * writing a zero does.
 *
 * Bounds go in min_value (6) and max_value (5), PLAIN-encoded at the physical
 * width. The deprecated min (2) and max (1) are not written: they are defined by
 * signed comparison alone, and a current reader that has column_orders prefers
 * the new pair anyway.
 */
static void
write_statistics(StringInfo b, PqLeaf *c, PqColMeta *m)
{
	int16		slast = 0;
	bool		isFloat = (c->ptype == PQ_FLOAT || c->ptype == PQ_DOUBLE);
	bool		ordered = (c->ptype == PQ_INT32 || c->ptype == PQ_INT64 || isFloat);
	bool		wide = (c->ptype == PQ_INT64 || c->ptype == PQ_DOUBLE);

	PgColumnarThriftPutI64Field(b, &slast, 3, m->st.nulls);		/* null_count */

	if (ordered && m->st.hasBounds)
	{
		char		lo[8];
		char		hi[8];
		int			n = wide ? 8 : 4;

		if (isFloat)
		{
			/*
			 * parquet.thrift, TYPE_ORDER: a computed maximum of either zero is
			 * written +0.0, and a computed minimum of either zero is written
			 * -0.0. C's comparison treats -0.0 == 0.0, so which zero the
			 * accumulator kept is not decided by the data; normalising here
			 * makes it not matter.
			 */
			float8		fmin = (m->st.fMin == 0.0) ? -0.0 : m->st.fMin;
			float8		fmax = (m->st.fMax == 0.0) ? 0.0 : m->st.fMax;

			if (wide)
			{
				memcpy(lo, &fmin, 8);
				memcpy(hi, &fmax, 8);
			}
			else
			{
				float4		f4lo = (float4) fmin;
				float4		f4hi = (float4) fmax;

				memcpy(lo, &f4lo, 4);
				memcpy(hi, &f4hi, 4);
			}
		}
		else if (wide)
		{
			int64		i64lo = m->st.iMin;
			int64		i64hi = m->st.iMax;

			memcpy(lo, &i64lo, 8);
			memcpy(hi, &i64hi, 8);
		}
		else
		{
			int32		i32lo = (int32) m->st.iMin;
			int32		i32hi = (int32) m->st.iMax;

			memcpy(lo, &i32lo, 4);
			memcpy(hi, &i32hi, 4);
		}

		PgColumnarThriftPutStringField(b, &slast, 5, hi, n);	/* max_value */
		PgColumnarThriftPutStringField(b, &slast, 6, lo, n);	/* min_value */
	}

	/*
	 * nan_count must be set for the floating point types even when it is zero:
	 * absent, a reader must assume NaNs may be present, which is the assumption
	 * the bounds above are written to remove.
	 */
	if (isFloat)
		PgColumnarThriftPutI64Field(b, &slast, 9, m->st.nans);

	PgColumnarThriftPutStop(b);									/* end Statistics */
}

/* one column chunk for a leaf (path_in_schema is the full leaf path) */
static void
write_column_chunk(StringInfo b, PqLeaf *c, PqColMeta *m)
{
	int16		last = 0;
	int16		mlast = 0;
	int			p;

	PgColumnarThriftPutI64Field(b, &last, 2, m->dataPageOffset);	/* file_offset */
	PgColumnarThriftPutField(b, &last, 3, TC_STRUCT);				/* meta_data */
	PgColumnarThriftPutI32Field(b, &mlast, 1, c->ptype);			/* type */
	PgColumnarThriftPutField(b, &mlast, 2, TC_LIST);				/* encodings [PLAIN, RLE] */
	PgColumnarThriftPutListHeader(b, 2, TC_I32);
	PgColumnarThriftPutZigzag32(b, PQ_ENC_PLAIN);
	PgColumnarThriftPutZigzag32(b, PQ_ENC_RLE);
	PgColumnarThriftPutField(b, &mlast, 3, TC_LIST);				/* path_in_schema */
	PgColumnarThriftPutListHeader(b, c->pathlen, TC_BINARY);
	for (p = 0; p < c->pathlen; p++)
	{
		PgColumnarThriftPutVarint(b, (uint64) strlen(c->path[p]));
		appendBinaryStringInfo(b, c->path[p], strlen(c->path[p]));
	}
	PgColumnarThriftPutI32Field(b, &mlast, 4, 0);					/* codec = UNCOMPRESSED */
	PgColumnarThriftPutI64Field(b, &mlast, 5, m->numValues);		/* num_values */
	PgColumnarThriftPutI64Field(b, &mlast, 6, m->totalSize);		/* total_uncompressed_size */
	PgColumnarThriftPutI64Field(b, &mlast, 7, m->totalSize);		/* total_compressed_size */
	PgColumnarThriftPutI64Field(b, &mlast, 9, m->dataPageOffset);	/* data_page_offset */
	PgColumnarThriftPutField(b, &mlast, 12, TC_STRUCT);				/* statistics */
	write_statistics(b, c, m);
	PgColumnarThriftPutStop(b);										/* end ColumnMetaData */
	PgColumnarThriftPutStop(b);										/* end ColumnChunk */
}

static void
write_row_group(StringInfo b, PqLeaf *leaves, int nleaves, PqRowGroup *rg)
{
	int16		last = 0;
	int			i;

	PgColumnarThriftPutField(b, &last, 1, TC_LIST);					/* columns */
	PgColumnarThriftPutListHeader(b, nleaves, TC_STRUCT);
	for (i = 0; i < nleaves; i++)
		write_column_chunk(b, &leaves[i], &rg->cols[i]);
	PgColumnarThriftPutI64Field(b, &last, 2, rg->totalByteSize);
	PgColumnarThriftPutI64Field(b, &last, 3, rg->numRows);
	PgColumnarThriftPutStop(b);
}

/* initialize a scalar leaf for a given type; *ok=false if unsupported */
static void
build_leaf_scalar(PqLeaf *leaf, Oid typid, int32 typmod,
				  int max_def, int max_rep, bool *ok)
{
	int			ptype,
				convType,
				typeLength,
				precision,
				scale;
	ParquetKind kind = parquet_kind_for_type(typid, typmod, &ptype, &convType,
											 &typeLength, &precision, &scale);

	if (ptype < 0)
	{
		*ok = false;
		return;
	}
	leaf->kind = kind;
	leaf->ptype = ptype;
	leaf->convType = convType;
	leaf->tsAdjusted = (kind == P_TIMESTAMP) ? (typid == TIMESTAMPTZOID) : -1;
	leaf->typeLength = typeLength;
	leaf->precision = precision;
	leaf->scale = scale;
	leaf->max_def = max_def;
	leaf->max_rep = max_rep;
	leaf->convertText = (kind == P_UTF8 &&
						 (typid == NUMERICOID || typid == JSONBOID));
	if (leaf->convertText)
	{
		Oid			outfunc;
		bool		isvarlena;

		getTypeOutputInfo(typid, &outfunc, &isvarlena);
		fmgr_info(outfunc, &leaf->outFinfo);
	}
	initStringInfo(&leaf->defs);
	initStringInfo(&leaf->reps);
	initStringInfo(&leaf->values);
	initStringInfo(&leaf->boolbits);
}

/* number of leaves a top column of this type contributes */
static int
count_leaves_for(Oid typid, int32 typmod)
{
	if (OidIsValid(get_element_type(typid)))
		return 1;
	if (get_typtype(typid) == TYPTYPE_COMPOSITE)
	{
		TupleDesc	td = lookup_rowtype_tupdesc(typid, typmod);
		int			a,
					live = 0;

		for (a = 0; a < td->natts; a++)
			if (!TupleDescAttr(td, a)->attisdropped)
				live++;
		ReleaseTupleDesc(td);
		return live;
	}
	return 1;
}

/* build one top column and its leaves; *ok=false if any leaf type is unsupported */
static void
build_top_column(TopColumn *tc, const char *name, Oid typid, int32 typmod,
				 PqLeaf *leaves, int *nleaves, bool *ok)
{
	Oid			elemtype = get_element_type(typid);

	tc->name = pstrdup(name);
	tc->structDesc = NULL;
	tc->fieldLeaf = NULL;

	if (OidIsValid(elemtype))
	{
		PqLeaf	   *leaf;

		tc->tkind = TOP_LIST;
		tc->firstLeaf = *nleaves;
		tc->nleaves = 1;
		tc->elemtype = elemtype;
		get_typlenbyvalalign(elemtype, &tc->elemlen, &tc->elembyval, &tc->elemalign);
		leaf = &leaves[(*nleaves)++];
		build_leaf_scalar(leaf, elemtype, -1, 3, 1, ok);
		leaf->path[0] = tc->name;
		leaf->path[1] = "list";
		leaf->path[2] = "element";
		leaf->pathlen = 3;
		return;
	}
	if (get_typtype(typid) == TYPTYPE_COMPOSITE)
	{
		TupleDesc	td = lookup_rowtype_tupdesc(typid, typmod);
		int			a;
		int			live = 0;

		tc->tkind = TOP_STRUCT;
		tc->firstLeaf = *nleaves;
		tc->structDesc = CreateTupleDescCopy(td);
		tc->fieldLeaf = palloc(sizeof(int) * td->natts);
		for (a = 0; a < td->natts; a++)
		{
			Form_pg_attribute fa = TupleDescAttr(td, a);
			PqLeaf	   *leaf;

			if (fa->attisdropped)
			{
				tc->fieldLeaf[a] = -1;
				continue;
			}
			leaf = &leaves[*nleaves];
			build_leaf_scalar(leaf, fa->atttypid, fa->atttypmod, 2, 0, ok);
			leaf->path[0] = tc->name;
			leaf->path[1] = pstrdup(NameStr(fa->attname));
			leaf->pathlen = 2;
			tc->fieldLeaf[a] = (*nleaves)++;
			live++;
		}
		tc->nleaves = live;
		ReleaseTupleDesc(td);
		return;
	}

	tc->tkind = TOP_SCALAR;
	tc->firstLeaf = *nleaves;
	tc->nleaves = 1;
	{
		PqLeaf	   *leaf = &leaves[(*nleaves)++];

		build_leaf_scalar(leaf, typid, typmod, 1, 0, ok);
		leaf->path[0] = tc->name;
		leaf->pathlen = 1;
	}
}

/*
 * PgColumnarParquetCheckExportable
 *		Ereport if rel cannot be exported to Parquet (a dropped column, or a
 *		column type the writer does not support). Lets the parallel exporter fail
 *		fast in the dispatcher, before it opens files or spawns workers.
 */
void
PgColumnarParquetCheckExportable(Relation rel)
{
	TupleDesc	tupdesc = RelationGetDescr(rel);
	int			ntop = tupdesc->natts;
	int			totalLeaves = 0;
	int			nleaves = 0;
	TopColumn  *tops;
	PqLeaf	   *leaves;
	int			i;

	for (i = 0; i < ntop; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);

		if (att->attisdropped)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar parquet export does not support dropped columns")));
		totalLeaves += count_leaves_for(att->atttypid, att->atttypmod);
	}
	tops = palloc0(sizeof(TopColumn) * ntop);
	leaves = palloc0(sizeof(PqLeaf) * Max(totalLeaves, 1));
	for (i = 0; i < ntop; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);
		bool		ok = true;

		build_top_column(&tops[i], NameStr(att->attname), att->atttypid,
						 att->atttypmod, leaves, &nleaves, &ok);
		if (!ok)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("column \"%s\" has type %s, which columnar parquet export does not support",
							NameStr(att->attname),
							format_type_be(att->atttypid))));
	}
	pfree(tops);
	pfree(leaves);
}

/*
 * pq_value_width
 *		The PLAIN bytes one non-null value of a fixed-width leaf adds: the
 *		physical width, which can be wider than the stored one (int2 is written
 *		as INT32). 0 for a variable-length leaf.
 */
static int
pq_value_width(const PqLeaf *c)
{
	switch (c->kind)
	{
		case P_BOOL:
			return 1;
		case P_INT16:
		case P_INT32:
		case P_FLOAT:
		case P_DATE:
			return 4;
		case P_INT64:
		case P_DOUBLE:
		case P_TIME:
		case P_TIMESTAMP:
			return 8;
		case P_UUID:
			return UUID_LEN;
		case P_DECIMAL:
			return 16;
		case P_UTF8:
		case P_BINARY:
			return 0;
	}
	return 0;
}

/*
 * pq_entry_bytes
 *		An upper bound on the bytes leaf_entry adds for one entry, counted in
 *		the written form (#1811, #1843 review): the level bytes, then the PLAIN
 *		value. A numeric or jsonb written as text is measured by its text, which
 *		can be far longer than the stored value. The leaf keeps that text for
 *		write_leaf_value, which takes the texts in the same order, so each value
 *		converts once.
 */
static int64
pq_entry_bytes(PqLeaf *c, bool hasValue, Datum d)
{
	int64		bytes = (c->max_rep > 0) ? 2 : 1;

	if (!hasValue)
		return bytes;
	if (pq_value_width(c) > 0)
		return bytes + pq_value_width(c);
	if (c->convertText)
	{
		char	   *str = OutputFunctionCall(&c->outFinfo, d);
		int32		len = (int32) strlen(str);

		bytes += 4 + (int64) len;
		PgcTextQueuePush(&c->pending, d, str, len);
	}
	else
		bytes += 4 + (int64) toast_raw_datum_size(d) - VARHDRSZ;
	return bytes;
}

/*
 * pq_row_bytes
 *		An upper bound on the bytes shred_top adds for one row, over all its
 *		columns (#1811, #1843 review). The writers cut a group before a row that
 *		would take the group past the byte cap, so this must not undercount.
 */
static int64
pq_row_bytes(TopColumn *tops, int ntop, PqLeaf *leaves,
			 const Datum *values, const bool *nulls)
{
	int64		bytes = 0;
	int			i;

	for (i = 0; i < ntop; i++)
	{
		TopColumn  *tc = &tops[i];

		switch (tc->tkind)
		{
			case TOP_SCALAR:
				bytes += pq_entry_bytes(&leaves[tc->firstLeaf], !nulls[i], values[i]);
				break;
			case TOP_LIST:
				{
					PqLeaf	   *leaf = &leaves[tc->firstLeaf];
					ArrayType  *arr;
					int			n;

					if (nulls[i])
					{
						bytes += 2;
						break;
					}
					arr = DatumGetArrayTypeP(values[i]);
					n = ArrayGetNItems(ARR_NDIM(arr), ARR_DIMS(arr));
					if (n == 0)
						bytes += 2;
					else if (pq_value_width(leaf) > 0)
						bytes += (int64) n * (2 + pq_value_width(leaf));
					else if (!leaf->convertText)
						bytes += (int64) n * (2 + 4) + ARR_SIZE(arr);
					else
					{
						Datum	   *elems;
						bool	   *enulls;
						int			k;

						deconstruct_array(arr, tc->elemtype, tc->elemlen,
										  tc->elembyval, tc->elemalign,
										  &elems, &enulls, &n);
						for (k = 0; k < n; k++)
							bytes += pq_entry_bytes(leaf, !enulls[k], elems[k]);
						pfree(elems);
						pfree(enulls);
					}
					if ((Pointer) arr != DatumGetPointer(values[i]))
						pfree(arr);
					break;
				}
			case TOP_STRUCT:
				{
					TupleDesc	sd = tc->structDesc;
					Datum	   *fv = NULL;
					bool	   *fn = NULL;
					int			a;

					if (!nulls[i])
					{
						HeapTupleHeader th = DatumGetHeapTupleHeader(values[i]);
						HeapTupleData tmp;

						fv = palloc(sizeof(Datum) * sd->natts);
						fn = palloc(sizeof(bool) * sd->natts);
						tmp.t_len = HeapTupleHeaderGetDatumLength(th);
						ItemPointerSetInvalid(&tmp.t_self);
						tmp.t_tableOid = InvalidOid;
						tmp.t_data = th;
						heap_deform_tuple(&tmp, sd, fv, fn);
					}
					for (a = 0; a < sd->natts; a++)
					{
						int			li = tc->fieldLeaf[a];

						if (li < 0)
							continue;
						bytes += pq_entry_bytes(&leaves[li], !nulls[i] && !fn[a],
												nulls[i] ? (Datum) 0 : fv[a]);
					}
					if (fv)
					{
						pfree(fv);
						pfree(fn);
					}
					break;
				}
		}
	}
	return bytes;
}

/*
 * pq_group_bytes
 *		The bytes the current row group has buffered, over all leaves (#1811).
 */
static int64
pq_group_bytes(const PqLeaf *leaves, int nleaves)
{
	int64		bytes = 0;
	int			i;

	for (i = 0; i < nleaves; i++)
		bytes += (int64) leaves[i].defs.len + leaves[i].reps.len +
			leaves[i].values.len + leaves[i].boolbits.len;
	return bytes;
}

/*
 * pq_flush_row_group
 *		Write the buffered rows as one row group: a data page per leaf, then
 *		record the group for the footer and reset the leaves.
 */
static void
pq_flush_row_group(PqSink *snk, PqLeaf *leaves, int nleaves, int64 groupRows,
				   int64 *offset, PqRowGroup **rgs, int *nrgs, int *rgCap,
				   MemoryContext rgCtx)
{
	PqRowGroup *rg;
	int			i;

	if (*nrgs == *rgCap)
	{
		*rgCap = *rgCap ? *rgCap * 2 : 8;
		*rgs = *rgs ? repalloc(*rgs, sizeof(PqRowGroup) * *rgCap)
			: palloc(sizeof(PqRowGroup) * *rgCap);
	}
	rg = &(*rgs)[(*nrgs)++];
	rg->cols = MemoryContextAlloc(rgCtx, sizeof(PqColMeta) * Max(nleaves, 1));
	rg->numRows = groupRows;
	rg->totalByteSize = 0;

	for (i = 0; i < nleaves; i++)
	{
		StringInfoData body;
		StringInfoData ph;
		int64		pageStart = *offset;

		initStringInfo(&body);
		build_leaf_body(&body, &leaves[i]);

		initStringInfo(&ph);
		write_page_header(&ph, leaves[i].nEntries, (int32) body.len);

		PgColumnarSinkWrite(snk, ph.data, ph.len);
		PgColumnarSinkWrite(snk, body.data, body.len);
		*offset += ph.len + body.len;

		rg->cols[i].dataPageOffset = pageStart;
		rg->cols[i].totalSize = ph.len + body.len;
		rg->cols[i].numValues = leaves[i].nEntries;
		/*
		 * Take the statistics before pqleaf_reset() below clears them. The
		 * footer is written after every row group has been flushed, so by
		 * then the leaf holds only the last group's accumulator.
		 */
		rg->cols[i].st = leaves[i].st;
		rg->totalByteSize += ph.len + body.len;

		pfree(body.data);
		pfree(ph.data);
		pqleaf_reset(&leaves[i]);
	}
}

/*
 * PgColumnarWriteParquetFile
 *		Write rel's live rows to a Parquet file at filepath, under snapshot.
 *		When restrictGroups is non-NULL, only those row groups are written (the
 *		parallel exporter gives each worker a disjoint group slice). Returns rows
 *		written. The caller owns rel (kept open) and the snapshot; on error this
 *		ereports and the resource owner releases the lock. Shared by the serial
 *		pgcolumnar_export_parquet and the parallel exporter.
 */
int64
PgColumnarWriteParquetFile(Relation rel, Snapshot snapshot, const char *filepath,
						   const uint64 *restrictGroups, int nRestrictGroups)
{
	return PgColumnarWriteParquetFileWith(rel, snapshot, filepath, restrictGroups,
										  nRestrictGroups, NULL);
}

int64
PgColumnarWriteParquetFileWith(Relation rel, Snapshot snapshot, const char *filepath,
							   const uint64 *restrictGroups, int nRestrictGroups,
							   const PgColumnarObjStoreConfig *cfg)
{
	TupleDesc	tupdesc;
	int			ntop;
	TopColumn  *tops;
	PqLeaf	   *leaves;
	int			nleaves = 0;
	int			totalLeaves = 0;
	PgColumnarReadState *readState;
	Datum	   *values;
	bool	   *nulls;
	uint64		rowNumber;
	int64		total = 0;
	int64		groupRows = 0;
	int64		offset = 0;
	PqSink	   *snk;
	int			i;
	PqRowGroup *rgs = NULL;
	int			nrgs = 0;
	int			rgCap = 0;
	MemoryContext rgCtx = CurrentMemoryContext;

	tupdesc = RelationGetDescr(rel);
	ntop = tupdesc->natts;

	/* reject dropped columns and count the leaf columns (arrays and composites
	 * expand to more than one leaf) */
	for (i = 0; i < ntop; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);

		if (att->attisdropped)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar parquet export does not support dropped columns")));
		totalLeaves += count_leaves_for(att->atttypid, att->atttypmod);
	}

	tops = palloc0(sizeof(TopColumn) * ntop);
	leaves = palloc0(sizeof(PqLeaf) * Max(totalLeaves, 1));
	for (i = 0; i < ntop; i++)
	{
		Form_pg_attribute att = TupleDescAttr(tupdesc, i);
		bool		ok = true;

		build_top_column(&tops[i], NameStr(att->attname), att->atttypid,
						 att->atttypmod, leaves, &nleaves, &ok);
		if (!ok)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("column \"%s\" has type %s, which columnar parquet export does not support",
							NameStr(att->attname),
							format_type_be(att->atttypid))));
	}

	snk = PgColumnarSinkOpenWith(filepath, cfg);
	PG_TRY();
	{
	PgColumnarSinkWrite(snk, "PAR1", 4);	/* magic header */
	offset = 4;

	values = palloc(sizeof(Datum) * ntop);
	nulls = palloc(sizeof(bool) * ntop);

	readState = PgColumnarBeginRead(rel, snapshot, NULL, NULL, 0, NULL);
	/*
	 * NULL restrictGroups means no restriction (the whole table). A non-NULL
	 * list restricts to exactly those groups, so an empty list (nRestrictGroups
	 * == 0) exports nothing. The parallel exporter always passes a non-NULL list,
	 * so a worker whose slice is empty writes zero rows rather than -- as an
	 * earlier version did -- the entire table.
	 */
	if (restrictGroups != NULL)
		PgColumnarReadRestrictToGroups(readState, restrictGroups, nRestrictGroups);

	for (;;)
	{
		bool		got = PgColumnarReadNextRow(readState, values, nulls, &rowNumber);

		if (got)
		{
			CHECK_FOR_INTERRUPTS();

			/*
			 * Cut by bytes too (#1811): start a new row group before a row
			 * that would take the buffered bytes past the cap. Each leaf
			 * buffers its values in one allocation, capped at 1 GiB. The row
			 * is counted as written, not as stored (#1843 review).
			 */
			if (groupRows > 0 &&
				pq_group_bytes(leaves, nleaves) +
				pq_row_bytes(tops, ntop, leaves, values, nulls) >
				(int64) pgcolumnar_export_group_bytes)
			{
				pq_flush_row_group(snk, leaves, nleaves, groupRows, &offset,
								   &rgs, &nrgs, &rgCap, rgCtx);
				groupRows = 0;
			}
			for (i = 0; i < ntop; i++)
				shred_top(&tops[i], leaves, values[i], nulls[i]);
			groupRows++;
			total++;
		}

		/* flush a row group when full, or at end if it holds rows */
		if ((groupRows == PARQUET_ROWGROUP_ROWS) || (!got && groupRows > 0))
		{
			pq_flush_row_group(snk, leaves, nleaves, groupRows, &offset,
							   &rgs, &nrgs, &rgCap, rgCtx);
			groupRows = 0;
		}

		if (!got)
			break;
	}
	PgColumnarEndRead(readState);

	/* ---- FileMetaData footer ---- */
	{
		StringInfoData fmd;
		int16		last = 0;
		int32		footerLen;
		int			nschema = 1;

		for (i = 0; i < ntop; i++)
			nschema += schema_count_for_top(&tops[i]);

		initStringInfo(&fmd);
		PgColumnarThriftPutI32Field(&fmd, &last, 1, 1);	/* version */
		/* schema list (2): root + the (possibly nested) elements per column */
		PgColumnarThriftPutField(&fmd, &last, 2, TC_LIST);
		PgColumnarThriftPutListHeader(&fmd, nschema, TC_STRUCT);
		write_schema_element_root(&fmd, ntop);
		for (i = 0; i < ntop; i++)
			write_top_schema(&fmd, &tops[i], leaves);
		PgColumnarThriftPutI64Field(&fmd, &last, 3, total);	/* num_rows */
		/* row_groups list (4) */
		PgColumnarThriftPutField(&fmd, &last, 4, TC_LIST);
		PgColumnarThriftPutListHeader(&fmd, nrgs, TC_STRUCT);
		for (i = 0; i < nrgs; i++)
			write_row_group(&fmd, leaves, nleaves, &rgs[i]);
		PgColumnarThriftPutStringField(&fmd, &last, 6, "pgColumnar", 10);	/* created_by */

		/*
		 * column_orders (7), one ColumnOrder per LEAF column in leaf order.
		 * Not optional in practice: parquet.thrift states that without it the
		 * meaning of min_value and max_value is undefined, and Arrow acts on
		 * that by discarding them, so a file with statistics and no
		 * column_orders skips nothing under pyarrow. The list length must equal
		 * the leaf count exactly -- Arrow raises on a mismatch, which would make
		 * the file unreadable rather than merely unskippable.
		 *
		 * TYPE_ORDER is the union's field 1, holding an empty TypeDefinedOrder:
		 * one field header, then the inner struct's stop, then the union's.
		 */
		PgColumnarThriftPutField(&fmd, &last, 7, TC_LIST);
		PgColumnarThriftPutListHeader(&fmd, nleaves, TC_STRUCT);
		for (i = 0; i < nleaves; i++)
		{
			int16		olast = 0;

			PgColumnarThriftPutField(&fmd, &olast, 1, TC_STRUCT);
			PgColumnarThriftPutStop(&fmd);	/* end TypeDefinedOrder (empty) */
			PgColumnarThriftPutStop(&fmd);	/* end ColumnOrder */
		}
		PgColumnarThriftPutStop(&fmd);

		PgColumnarSinkWrite(snk, fmd.data, fmd.len);
		footerLen = fmd.len;
		PgColumnarSinkWrite(snk, &footerLen, 4);	/* footer length, LE */
		PgColumnarSinkWrite(snk, "PAR1", 4);	/* magic footer */
		pfree(fmd.data);
	}

	/*
	 * Commit: the final name appears only here, whole (#394). Any failure
	 * above, including inside a write, unwinds through the CATCH, which
	 * removes the temp file; the final path is never touched on error.
	 */
	PgColumnarSinkFinish(snk);
	}
	PG_CATCH();
	{
		PgColumnarSinkAbortAndRethrow(snk);	/* never returns (#1741) */
	}
	PG_END_TRY();

	return total;
}

/*
 * pgcolumnar_export_parquet
 *		SQL: pgcolumnar.export_parquet(rel regclass, path text) -> bigint.
 *		Thin wrapper over PgColumnarWriteParquetFile for the whole table.
 *
 *		SQL: pgcolumnar.export_parquet(rel regclass, path text, server name)
 *		writes a remote path with that pgcolumnar_parquet server's endpoint and
 *		region and the caller's user mapping, the configuration a foreign table
 *		of that server reads with (#1795). The two-argument form writes with the
 *		server process environment.
 */
Datum
pgcolumnar_export_parquet(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *path;
	Relation	rel;
	Snapshot	snapshot;
	int64		total;
	PgColumnarObjStoreConfig cfg;
	PgColumnarObjStoreConfig *cfgp = NULL;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("relation and path must not be null")));
	if (!has_privs_of_role(GetUserId(), ROLE_PG_WRITE_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_write_server_files role to write a server file")));

	relid = PG_GETARG_OID(0);
	path = text_to_cstring(PG_GETARG_TEXT_PP(1));
	PgColumnarRefuseS3AliasWrite("export_parquet", path);

	/*
	 * The server is resolved before the file is opened, so a missing server,
	 * a missing USAGE grant or half a credential stops the call with nothing
	 * written.
	 */
	if (PG_NARGS() > 2)
	{
		ForeignServer *server;
		AclResult	ac;

		if (PG_ARGISNULL(2))
			ereport(ERROR,
					(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
					 errmsg("server must not be null")));
		server = GetForeignServerByName(NameStr(*PG_GETARG_NAME(2)), false);
		if (strcmp(GetForeignDataWrapper(server->fdwid)->fdwname,
				   "pgcolumnar_parquet") != 0)
			ereport(ERROR,
					(errcode(ERRCODE_WRONG_OBJECT_TYPE),
					 errmsg("server \"%s\" uses the %s wrapper, not pgcolumnar_parquet",
							server->servername,
							GetForeignDataWrapper(server->fdwid)->fdwname)));
#if PG_VERSION_NUM >= 160000
		ac = object_aclcheck(ForeignServerRelationId, server->serverid,
							 GetUserId(), ACL_USAGE);
#else
		ac = pg_foreign_server_aclcheck(server->serverid, GetUserId(), ACL_USAGE);
#endif
		if (ac != ACLCHECK_OK)
			aclcheck_error(ac, OBJECT_FOREIGN_SERVER, server->servername);
		cfgp = &cfg;
		PgColumnarObjStoreConfigForServer(server->serverid, cfgp);
	}

	/*
	 * SELECT on the source, before the file is opened (#559). The server-file
	 * role above governs writing a file; it does not govern reading this
	 * relation. The parallel twin already checks this
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

	snapshot = ActiveSnapshotSet() ? GetActiveSnapshot() : GetTransactionSnapshot();
	total = PgColumnarWriteParquetFileWith(rel, snapshot, path, NULL, 0, cfgp);

	table_close(rel, AccessShareLock);
	PG_RETURN_INT64(total);
}

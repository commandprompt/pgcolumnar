/*-------------------------------------------------------------------------
 *
 * pgcolumnar_encoding.c
 *		Lightweight, type-aware value-stream encodings (I1) and the
 *		compression-block abstraction they implement (I2).
 *
 * The encoding layer sits between the raw serialized value stream that the
 * writer builds (a packed sequence of PgColumnarEncodeValue outputs, spec 4) and
 * the general-purpose block codec (pgcolumnar_compression.c, spec 5). An encoding
 * is a reversible transform of the raw value-stream BYTES: encode(raw) -> a
 * smaller encoded buffer, and decode(encoded) -> the byte-identical raw stream.
 * Because decode reconstructs the exact raw stream, every downstream consumer
 * (per-value PgColumnarDecodeValue, the vectorized group decoder, the min/max
 * skip list) is unchanged; only the flush and load paths gain one step.
 *
 * The techniques come from the public column-store literature (see
 * design/IMPROVEMENT_PLAN.md): run-length encoding and frame-of-reference with
 * bit-packing [C-Store; Abadi SIGMOD 2006], and delta+bit-packing
 * [Abadi SIGMOD 2006; Zukowski et al. Super-Scalar Compression]. This file is
 * an independent implementation written from those descriptions and the public
 * PostgreSQL API only (clean-room; see PROVENANCE.md).
 *
 * On-disk: columnar.chunk records value_encoding_type per chunk, and
 * value_raw_length (the decoded raw stream length). A NULL/absent encoding type
 * (old format 2.0 chunks) is treated as NONE, so 2.0 files still read.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "miscadmin.h"

#include <math.h>

#include "catalog/pg_type.h"
#include "funcapi.h"
#include "port/pg_bitutils.h"
#include "utils/builtins.h"
#include "utils/memutils.h"
/*
 * Explicit rather than inherited: through PostgreSQL 18 this arrives
 * transitively behind funcapi.h, and on 19 it does not. The five-major matrix
 * caught it as a build failure on 19 alone.
 */
#include "utils/tuplestore.h"

/*
 * Decoding runs on bytes read back from disk. Normal pgColumnar data is
 * self-consistent, but a corrupt catalog row, bit rot, or a crafted format-2.0
 * file can present lengths, counts, widths, and dictionary codes that do not
 * match the buffer. Every decoder validates its inputs and raises this rather
 * than reading or writing out of bounds.
 */
#define DECODE_CORRUPT(msg) \
	ereport(ERROR, \
			(errcode(ERRCODE_DATA_CORRUPTED), \
			 errmsg("columnar: corrupt encoded chunk (%s)", (msg))))

/* -------------------------------------------------------------------------
 * fixed-width value load/store and bit-packing helpers
 * ------------------------------------------------------------------------- */

/*
 * Load/store a fixed-width value as a uint64. We copy the low W bytes so the
 * transform is exactly reversible on any architecture; the integer value is
 * only used to cluster/delta and never leaves this file, so its interpretation
 * (little-endian on x86) does not affect correctness of the round trip.
 */
static inline uint64
load_uint(const char *p, int w)
{
	uint64		v = 0;

	memcpy(&v, p, w);
	return v;
}

static inline void
store_uint(char *p, uint64 v, int w)
{
	memcpy(p, &v, w);
}

/* number of bits needed to represent maxval; 0 when maxval == 0 */
static int
bits_needed(uint64 maxval)
{
	int			n = 0;

	while (maxval > 0)
	{
		n++;
		maxval >>= 1;
	}
	return n;
}

/*
 * pack n values of `width` bits each (LSB-first) and append to out
 *
 * Each field is masked to `width` bits and shifted into position, then emitted
 * one output byte at a time via explicit shifts. Extracting each byte with a
 * shift (rather than memcpy of a word) keeps the on-disk layout LSB-first and
 * identical on any host endianness. A field spans at most 9 bytes (7 bits of
 * sub-byte offset + 64 bits of value), so the inner loop runs <= 9 times
 * regardless of width -- versus one iteration per bit in the naive form.
 */
static void
bitpack(const uint64 *vals, uint32 n, int width, StringInfo out)
{
	uint64		totalbits;
	uint32		nbytes;
	unsigned char *buf;
	uint64		bitpos = 0;
	uint32		i;
	uint64		mask;

	if (width == 0 || n == 0)
		return;

	totalbits = (uint64) n * (uint64) width;
	nbytes = (uint32) ((totalbits + 7) / 8);
	buf = palloc0(nbytes);
	mask = (width == 64) ? ~UINT64CONST(0) : ((UINT64CONST(1) << width) - 1);

	for (i = 0; i < n; i++)
	{
		uint64		v = vals[i] & mask;
		uint32		bytepos = (uint32) (bitpos >> 3);
		uint32		bitoff = (uint32) (bitpos & 7);
		uint64		lo = v << bitoff;
		uint64		hi = bitoff ? (v >> (64 - bitoff)) : 0;
		uint32		nb = (bitoff + (uint32) width + 7) >> 3;
		uint32		k;

		for (k = 0; k < nb; k++)
		{
			unsigned char byte = (k < 8)
				? (unsigned char) (lo >> (8 * k))
				: (unsigned char) (hi >> (8 * (k - 8)));

			buf[bytepos + k] |= byte;
		}
		bitpos += width;
	}

	appendBinaryStringInfo(out, (char *) buf, nbytes);
	pfree(buf);
}

/* inverse of bitpack: unpack n values of `width` bits each into out[]. inLen is
 * the number of bytes available at `in`; reads past it are rejected. */
/*
 * How often a per-value decode loop checks for interrupts. A vector holds up to
 * pgcolumnar.chunk_group_row_limit values, which is user-settable and unbounded,
 * so a decode of one vector is otherwise an uninterruptible stretch proportional
 * to that setting. The stride is a power of two so the test is a mask, and large
 * enough that it disappears next to the per-value work.
 */
#define COLUMNAR_DECODE_INTERRUPT_STRIDE 65536
#define COLUMNAR_DECODE_INTERRUPT(i) \
	do { \
		if (((i) & (COLUMNAR_DECODE_INTERRUPT_STRIDE - 1)) == 0) \
			CHECK_FOR_INTERRUPTS(); \
	} while (0)

static void
bitunpack(const unsigned char *in, uint32 inLen, uint32 n, int width,
		  uint64 *out)
{
	uint64		bitpos = 0;
	uint32		i;

	if (width < 0 || width > 64)
		DECODE_CORRUPT("bit width out of range");

	if (width == 0)
	{
		for (i = 0; i < n; i++)
			out[i] = 0;
		return;
	}

	/* bytes needed to hold n values of `width` bits, LSB-first */
	if (((uint64) n * (uint32) width + 7) / 8 > inLen)
		DECODE_CORRUPT("bit-packed body exceeds encoded length");

	/*
	 * Word at a time (#501). The per-bit loop this replaces ran once per BIT --
	 * a load, two shifts, a mask, a test and an or for each one -- and was the
	 * largest single frame in a profile of both scan shapes, at 21.0% of a
	 * filtered aggregate and 19.2% of a row-returning projection.
	 *
	 * A value of `width` bits starting at bit offset `shift` occupies at most
	 * 64 + 7 bits, so one unaligned 64-bit load plus at most one more byte
	 * covers it. Both are little-endian reads, which this format already
	 * requires (spec 3), and the shift-in of the high byte is guarded: it runs
	 * only when shift + width > 64, which cannot hold with shift == 0 because
	 * width is at most 64, so `64 - shift` is never a shift by 64.
	 *
	 * The fast path is used only where its 9-byte window lies inside the buffer.
	 * The tail falls back to the reference loop rather than reading past the end
	 * -- the over-read would be at most seven bytes and would usually land in the
	 * same allocation, which is exactly the kind of bug that survives every test
	 * and fails under a sanitizer on someone else's machine.
	 */
	{
		const uint64 mask = (width == 64)
			? ~UINT64CONST(0) : ((UINT64CONST(1) << width) - 1);
		uint32		nbytes = (uint32) (((uint64) n * (uint32) width + 7) / 8);

		/*
		 * How many values the wide load can serve, computed once rather than
		 * tested per value. Value i starts at byte (i * width) / 8 and needs
		 * nine bytes, so the condition is i * width <= 8 * (nbytes - 9).
		 *
		 * Hoisting this matters at small widths: a per-value bounds test cost
		 * more than the whole per-bit loop it was replacing when width was 1,
		 * measured at 1.7% slower on a monotonic bigint before the split.
		 */
		uint32		nFast = 0;

		if (nbytes >= 9)
		{
			uint64		maxIdx = (8 * (uint64) (nbytes - 9)) / (uint32) width;

			nFast = (maxIdx + 1 < (uint64) n) ? (uint32) (maxIdx + 1) : n;
		}

		for (i = 0; i < nFast; i++)
		{
			uint64		lo;
			unsigned	shift = (unsigned) (bitpos & 7);
			uint64		v;

			COLUMNAR_DECODE_INTERRUPT(i);

			memcpy(&lo, in + (bitpos >> 3), sizeof(uint64));
			v = lo >> shift;
			if (shift + (unsigned) width > 64)
				v |= (uint64) in[(bitpos >> 3) + 8] << (64 - shift);

			out[i] = v & mask;
			bitpos += width;
		}

		/*
		 * Tail: assemble only the bytes that exist, one bit at a time.
		 *
		 * Deliberately bit-indexed and therefore endian-independent, where the
		 * fast path above is not. Spec 3 already requires a little-endian host,
		 * so this is not a second contract -- but do not "simplify" this into
		 * the same word trick. The fast path is bounded by nFast precisely
		 * because the wide load would read past the encoded body here, and an
		 * array that is correct above nFast and wrong below it is far nastier to
		 * diagnose than one that is uniformly wrong.
		 */
		for (; i < n; i++)
		{
			uint64		v = 0;
			int			b;

			COLUMNAR_DECODE_INTERRUPT(i);
			for (b = 0; b < width; b++)
			{
				if ((in[(bitpos + b) >> 3] >> ((bitpos + b) & 7)) & 1)
					v |= (uint64) 1 << b;
			}
			out[i] = v;
			bitpos += width;
		}
	}
}

/* zigzag map signed<->unsigned so small-magnitude deltas pack tightly */
static inline uint64
zigzag(int64 x)
{
	return ((uint64) x << 1) ^ (uint64) (x >> 63);
}

static inline int64
unzigzag(uint64 z)
{
	return (int64) (z >> 1) ^ -(int64) (z & 1);
}

/* is this a fixed-width integer-family value we can FOR/DELTA/DOD encode?
 * Floats are excluded: their bit patterns are not integers, and they have their
 * own encoding (gorilla). Applying integer delta to float bits happens to help
 * same-exponent runs but is not the right tool. */
static inline bool
is_packable_int(Form_pg_attribute att)
{
	return att->attbyval &&
		att->atttypid != FLOAT4OID && att->atttypid != FLOAT8OID &&
		(att->attlen == 1 || att->attlen == 2 ||
		 att->attlen == 4 || att->attlen == 8);
}

/* is this a float we can Gorilla-encode? */
static inline bool
is_gorilla_float(Form_pg_attribute att)
{
	return att->attbyval &&
		(att->atttypid == FLOAT4OID || att->atttypid == FLOAT8OID);
}

/* -------------------------------------------------------------------------
 * RLE: runs of an equal fixed-width value, [uint32 count][W bytes] per run
 * ------------------------------------------------------------------------- */

static bool
encode_rle(const char *raw, uint32 rawLen, int w, uint32 n,
		   char **out, uint32 *outLen)
{
	StringInfoData buf;
	uint32		i = 0;

	initStringInfo(&buf);
	while (i < n)
	{
		const char *val = raw + (uint64) i * w;
		uint32		run = 1;

		while (i + run < n &&
			   memcmp(raw + (uint64) (i + run) * w, val, w) == 0)
			run++;

		appendBinaryStringInfo(&buf, (char *) &run, sizeof(uint32));
		appendBinaryStringInfo(&buf, val, w);
		i += run;

		/* give up early if we are not beating the raw size */
		if ((uint32) buf.len >= rawLen)
		{
			pfree(buf.data);
			return false;
		}
	}

	*out = buf.data;
	*outLen = buf.len;
	return true;
}

static char *
decode_rle(const char *enc, uint32 encLen, int w, uint32 n, uint32 rawLen,
		   MemoryContext cx)
{
	char	   *raw = MemoryContextAlloc(cx, rawLen > 0 ? rawLen : 1);
	const char *p = enc;
	const char *end = enc + encLen;
	uint32		produced = 0;

	while (produced < n)
	{
		uint32		run;

		if (p + sizeof(uint32) > end)
			DECODE_CORRUPT("RLE run header past encoded length");
		memcpy(&run, p, sizeof(uint32));
		p += sizeof(uint32);
		if (p + w > end)
			DECODE_CORRUPT("RLE run value past encoded length");

		/*
		 * The check belongs in the inner loop, not the outer one. A single run
		 * can cover the whole vector, which is the case RLE is chosen for, and
		 * the outer loop would then run once. Testing `produced` out here is also
		 * unsound on its own: it advances by run length rather than by one, so it
		 * can step over every multiple of the stride and never fire again after
		 * the first run.
		 */
		while (run-- > 0 && produced < n)
		{
			COLUMNAR_DECODE_INTERRUPT(produced);
			memcpy(raw + (uint64) produced * w, p, w);	/* produced < n; n*w == rawLen */
			produced++;
		}
		p += w;
	}
	return raw;
}

/* -------------------------------------------------------------------------
 * FOR: frame of reference + bit-packing.
 * header: [uint8 w][uint8 width][uint64 min], body: bit-packed (value - min)
 * ------------------------------------------------------------------------- */

static bool
encode_for(const char *raw, uint32 rawLen, int w, uint32 n,
		   char **out, uint32 *outLen)
{
	uint64	   *vals = palloc(sizeof(uint64) * n);
	uint64		minv;
	uint64		maxoff = 0;
	int			width;
	StringInfoData buf;
	uint32		i;

	for (i = 0; i < n; i++)
		vals[i] = load_uint(raw + (uint64) i * w, w);

	minv = vals[0];
	for (i = 1; i < n; i++)
		if (vals[i] < minv)
			minv = vals[i];
	for (i = 0; i < n; i++)
	{
		vals[i] -= minv;
		if (vals[i] > maxoff)
			maxoff = vals[i];
	}
	width = bits_needed(maxoff);

	initStringInfo(&buf);
	appendStringInfoChar(&buf, (char) w);
	appendStringInfoChar(&buf, (char) width);
	appendBinaryStringInfo(&buf, (char *) &minv, sizeof(uint64));
	bitpack(vals, n, width, &buf);
	pfree(vals);

	if ((uint32) buf.len >= rawLen)
	{
		pfree(buf.data);
		return false;
	}
	*out = buf.data;
	*outLen = buf.len;
	return true;
}

static char *
decode_for(const char *enc, uint32 encLen, uint32 n, uint32 rawLen,
		   MemoryContext cx)
{
	char	   *raw = MemoryContextAlloc(cx, rawLen > 0 ? rawLen : 1);
	int			w;
	int			width;
	uint64		minv;
	uint64	   *vals;
	uint32		i;

	if (encLen < 10)			/* [u8 w][u8 width][u64 min] */
		DECODE_CORRUPT("FOR header truncated");
	w = (unsigned char) enc[0];
	width = (unsigned char) enc[1];
	if (w != 1 && w != 2 && w != 4 && w != 8)
		DECODE_CORRUPT("FOR element width invalid");
	if ((uint64) n * (uint32) w != rawLen)
		DECODE_CORRUPT("FOR raw length mismatch");

	memcpy(&minv, enc + 2, sizeof(uint64));
	vals = palloc(sizeof(uint64) * (n > 0 ? n : 1));
	bitunpack((const unsigned char *) enc + 10, encLen - 10, n, width, vals);
	for (i = 0; i < n; i++)
	{
		COLUMNAR_DECODE_INTERRUPT(i);
		store_uint(raw + (uint64) i * w, vals[i] + minv, w);
	}
	pfree(vals);
	return raw;
}

/* -------------------------------------------------------------------------
 * DELTA: store first value, then zigzag(value[i]-value[i-1]) bit-packed.
 * header: [uint8 w][uint8 width][uint64 base], body: bit-packed zigzag deltas
 * ------------------------------------------------------------------------- */

static bool
encode_delta(const char *raw, uint32 rawLen, int w, uint32 n,
			 char **out, uint32 *outLen)
{
	uint64	   *vals = palloc(sizeof(uint64) * n);
	uint64	   *deltas;
	uint64		base;
	uint64		maxz = 0;
	int			width;
	StringInfoData buf;
	uint32		i;

	for (i = 0; i < n; i++)
		vals[i] = load_uint(raw + (uint64) i * w, w);

	base = vals[0];
	deltas = palloc(sizeof(uint64) * (n > 1 ? n - 1 : 1));
	for (i = 1; i < n; i++)
	{
		int64		d = (int64) vals[i] - (int64) vals[i - 1];
		uint64		z = zigzag(d);

		deltas[i - 1] = z;
		if (z > maxz)
			maxz = z;
	}
	width = bits_needed(maxz);

	initStringInfo(&buf);
	appendStringInfoChar(&buf, (char) w);
	appendStringInfoChar(&buf, (char) width);
	appendBinaryStringInfo(&buf, (char *) &base, sizeof(uint64));
	if (n > 1)
		bitpack(deltas, n - 1, width, &buf);
	pfree(vals);
	pfree(deltas);

	if ((uint32) buf.len >= rawLen)
	{
		pfree(buf.data);
		return false;
	}
	*out = buf.data;
	*outLen = buf.len;
	return true;
}

static char *
decode_delta(const char *enc, uint32 encLen, uint32 n, uint32 rawLen,
			 MemoryContext cx)
{
	char	   *raw = MemoryContextAlloc(cx, rawLen > 0 ? rawLen : 1);
	int			w;
	int			width;
	uint64		base;
	uint64	   *deltas;
	uint64		cur;
	uint32		i;

	if (encLen < 10)			/* [u8 w][u8 width][u64 base] */
		DECODE_CORRUPT("DELTA header truncated");
	w = (unsigned char) enc[0];
	width = (unsigned char) enc[1];
	if (w != 1 && w != 2 && w != 4 && w != 8)
		DECODE_CORRUPT("DELTA element width invalid");
	if ((uint64) n * (uint32) w != rawLen)
		DECODE_CORRUPT("DELTA raw length mismatch");

	memcpy(&base, enc + 2, sizeof(uint64));
	deltas = palloc(sizeof(uint64) * (n > 1 ? n - 1 : 1));
	if (n > 1)
		bitunpack((const unsigned char *) enc + 10, encLen - 10, n - 1, width,
				  deltas);

	cur = base;
	if (n > 0)
		store_uint(raw, cur, w);
	for (i = 1; i < n; i++)
	{
		COLUMNAR_DECODE_INTERRUPT(i);
		cur = (uint64) ((int64) cur + unzigzag(deltas[i - 1]));
		store_uint(raw + (uint64) i * w, cur, w);
	}
	pfree(deltas);
	return raw;
}

/* -------------------------------------------------------------------------
 * streaming bit writer/reader (LSB-first) for variable-width fields
 * ------------------------------------------------------------------------- */

typedef struct BitWriter
{
	StringInfoData buf;
	uint8		cur;
	int			nbits;			/* bits currently held in cur (0..7) */
} BitWriter;

static void
bw_init(BitWriter *bw)
{
	initStringInfo(&bw->buf);
	bw->cur = 0;
	bw->nbits = 0;
}

/*
 * append the low `bits` bits of v, least-significant first
 *
 * Same LSB-first stream as a per-bit loop, but bits are merged into a 64-bit
 * accumulator and whole bytes are flushed at once. The accumulator holds the
 * up-to-7 carried bits (bw->cur) in its low positions with the new bits above,
 * so each chunk stays within 64 bits: <= 7 carried + <= 57 taken.
 */
static void
bw_put(BitWriter *bw, uint64 v, int bits)
{
	if (bits <= 0)
		return;
	if (bits < 64)
		v &= (UINT64CONST(1) << bits) - 1;

	while (bits > 0)
	{
		int			take = bits < 57 ? bits : 57;
		uint64		chunk = v & ((UINT64CONST(1) << take) - 1);
		uint64		acc = (uint64) bw->cur | (chunk << bw->nbits);
		int			have = bw->nbits + take;

		while (have >= 8)
		{
			appendStringInfoChar(&bw->buf, (char) (acc & 0xff));
			acc >>= 8;
			have -= 8;
		}
		bw->cur = (uint8) (acc & 0xff);
		bw->nbits = have;

		v >>= take;
		bits -= take;
	}
}

static void
bw_flush(BitWriter *bw)
{
	if (bw->nbits > 0)
	{
		appendStringInfoChar(&bw->buf, (char) bw->cur);
		bw->cur = 0;
		bw->nbits = 0;
	}
}

typedef struct BitReader
{
	const unsigned char *data;
	uint64		nbits;			/* total readable bits (bytes * 8) */
	uint64		bitpos;
} BitReader;

static void
br_init(BitReader *br, const unsigned char *data, uint32 nbytes)
{
	br->data = data;
	br->nbits = (uint64) nbytes * 8;
	br->bitpos = 0;
}

static uint64
br_get(BitReader *br, int bits)
{
	uint64		v = 0;
	int			i;

	if (bits < 0 || bits > 64)
		DECODE_CORRUPT("bit field width out of range");
	if (br->bitpos + (uint64) bits > br->nbits)
		DECODE_CORRUPT("bit reader ran past encoded length");

	for (i = 0; i < bits; i++)
	{
		uint64		p = br->bitpos + i;

		if ((br->data[p >> 3] >> (p & 7)) & 1)
			v |= (uint64) 1 << i;
	}
	br->bitpos += bits;
	return v;
}

/*
 * leading/trailing zero counts of a value within a `total`-bit window
 *
 * pg_leftmost/rightmost_one_pos64 compile to a hardware bit-scan and require a
 * non-zero argument. clz within the window is the gap from the top window bit
 * (total-1) down to the highest set bit; ctz is simply the lowest set-bit
 * position. x < 2^total holds for every caller (total == w*8 and x is a w-byte
 * value). x == 0 keeps the old all-zeros answer.
 */
static inline int
clz_in(uint64 x, int total)
{
	if (x == 0)
		return total;
	return (total - 1) - pg_leftmost_one_pos64(x);
}

static inline int
ctz_in(uint64 x)
{
	if (x == 0)
		return 0;
	return pg_rightmost_one_pos64(x);
}

/* -------------------------------------------------------------------------
 * GORILLA: XOR-against-previous for float4/float8 (Facebook Gorilla, VLDB 2015),
 * simplified without the previous-window reuse. Per value after the first:
 *   XOR==0            -> control bit 0
 *   else              -> control bit 1, [lz][mlen-1][mlen meaningful bits]
 * where lz/len fields are 5 bits for 32-bit values, 6 bits for 64-bit.
 * ------------------------------------------------------------------------- */

static bool
encode_gorilla(const char *raw, uint32 rawLen, int w, uint32 n,
			   char **out, uint32 *outLen)
{
	int			total = w * 8;
	int			field = (w == 8) ? 6 : 5;
	BitWriter	bw;
	uint64		prev;
	uint32		i;

	bw_init(&bw);
	prev = load_uint(raw, w);
	bw_put(&bw, prev, total);	/* first value verbatim */

	for (i = 1; i < n; i++)
	{
		uint64		v = load_uint(raw + (uint64) i * w, w);
		uint64		x = v ^ prev;

		if (x == 0)
			bw_put(&bw, 0, 1);
		else
		{
			int			lz = clz_in(x, total);
			int			tz = ctz_in(x);
			int			mlen = total - lz - tz;

			bw_put(&bw, 1, 1);
			bw_put(&bw, (uint64) lz, field);
			bw_put(&bw, (uint64) (mlen - 1), field);
			bw_put(&bw, x >> tz, mlen);
		}
		prev = v;
	}
	bw_flush(&bw);

	if ((uint32) bw.buf.len >= rawLen)
	{
		pfree(bw.buf.data);
		return false;
	}
	*out = bw.buf.data;
	*outLen = bw.buf.len;
	return true;
}

static char *
decode_gorilla(const char *enc, uint32 encLen, int w, uint32 n, uint32 rawLen,
			   MemoryContext cx)
{
	char	   *raw = MemoryContextAlloc(cx, rawLen > 0 ? rawLen : 1);
	int			total = w * 8;
	int			field = (w == 8) ? 6 : 5;
	BitReader	br;
	uint64		prev;
	uint32		i;

	br_init(&br, (const unsigned char *) enc, encLen);
	prev = br_get(&br, total);
	if (n > 0)
		store_uint(raw, prev, w);

	for (i = 1; i < n; i++)
	{
		uint64		v;

		COLUMNAR_DECODE_INTERRUPT(i);
		if (br_get(&br, 1) == 0)
			v = prev;
		else
		{
			int			lz = (int) br_get(&br, field);
			int			mlen = (int) br_get(&br, field) + 1;
			int			tz = total - lz - mlen;
			uint64		meaningful;

			if (lz < 0 || mlen < 1 || mlen > total || tz < 0)
				DECODE_CORRUPT("GORILLA XOR field out of range");
			meaningful = br_get(&br, mlen);
			v = prev ^ (meaningful << tz);
		}
		store_uint(raw + (uint64) i * w, v, w);	/* i < n; n*w == rawLen */
		prev = v;
	}
	return raw;
}

/* -------------------------------------------------------------------------
 * DOD: delta-of-delta + zigzag + bit-packing, for fixed-width int-family types.
 * Regular sequences (e.g. fixed-interval timestamps) have zero second
 * differences and pack to almost nothing.
 * header: [uint8 w][uint8 width][uint64 base][int64 firstDelta]
 * body: bit-packed zigzag(delta[i]-delta[i-1]) for i >= 2
 * ------------------------------------------------------------------------- */

static bool
encode_dod(const char *raw, uint32 rawLen, int w, uint32 n,
		   char **out, uint32 *outLen)
{
	uint64	   *vals = palloc(sizeof(uint64) * n);
	uint64	   *dods;
	uint64		base;
	int64		firstDelta;
	uint64		maxz = 0;
	int			width;
	StringInfoData buf;
	uint32		i;

	for (i = 0; i < n; i++)
		vals[i] = load_uint(raw + (uint64) i * w, w);

	base = vals[0];
	firstDelta = (n > 1) ? (int64) vals[1] - (int64) vals[0] : 0;
	dods = palloc(sizeof(uint64) * (n > 2 ? n - 2 : 1));
	for (i = 2; i < n; i++)
	{
		int64		d = (int64) vals[i] - (int64) vals[i - 1];
		int64		dd = d - ((int64) vals[i - 1] - (int64) vals[i - 2]);
		uint64		z = zigzag(dd);

		dods[i - 2] = z;
		if (z > maxz)
			maxz = z;
	}
	width = bits_needed(maxz);

	initStringInfo(&buf);
	appendStringInfoChar(&buf, (char) w);
	appendStringInfoChar(&buf, (char) width);
	appendBinaryStringInfo(&buf, (char *) &base, sizeof(uint64));
	appendBinaryStringInfo(&buf, (char *) &firstDelta, sizeof(int64));
	if (n > 2)
		bitpack(dods, n - 2, width, &buf);
	pfree(vals);
	pfree(dods);

	if ((uint32) buf.len >= rawLen)
	{
		pfree(buf.data);
		return false;
	}
	*out = buf.data;
	*outLen = buf.len;
	return true;
}

static char *
decode_dod(const char *enc, uint32 encLen, uint32 n, uint32 rawLen,
		   MemoryContext cx)
{
	char	   *raw = MemoryContextAlloc(cx, rawLen > 0 ? rawLen : 1);
	int			w;
	int			width;
	uint64		base;
	int64		firstDelta;
	uint64	   *dods;
	int64		delta;
	uint64		cur;
	uint32		i;

	if (encLen < 18)			/* [u8 w][u8 width][u64 base][i64 firstDelta] */
		DECODE_CORRUPT("DOD header truncated");
	w = (unsigned char) enc[0];
	width = (unsigned char) enc[1];
	if (w != 1 && w != 2 && w != 4 && w != 8)
		DECODE_CORRUPT("DOD element width invalid");
	if ((uint64) n * (uint32) w != rawLen)
		DECODE_CORRUPT("DOD raw length mismatch");

	memcpy(&base, enc + 2, sizeof(uint64));
	memcpy(&firstDelta, enc + 10, sizeof(int64));
	dods = palloc(sizeof(uint64) * (n > 2 ? n - 2 : 1));
	if (n > 2)
		bitunpack((const unsigned char *) enc + 18, encLen - 18, n - 2, width,
				  dods);

	cur = base;
	if (n > 0)
		store_uint(raw, cur, w);
	delta = firstDelta;
	for (i = 1; i < n; i++)
	{
		COLUMNAR_DECODE_INTERRUPT(i);
		if (i >= 2)
			delta += unzigzag(dods[i - 2]);
		cur = (uint64) ((int64) cur + delta);
		store_uint(raw + (uint64) i * w, cur, w);
	}
	return raw;
}

/* -------------------------------------------------------------------------
 * ALP: the decimal scheme of Adaptive Lossless floating-Point compression, for
 * float4 and float8 (E1). Many stored doubles are decimals in disguise (a price,
 * a rate). For a chosen exponent e and factor f, round(value * 10^e * 10^-f) is
 * an integer that reconstructs the value exactly for most rows; those integers,
 * frame-of-reference plus bit-packed, are far smaller than the raw IEEE-754
 * bytes. A value that does not reconstruct byte-for-byte (a genuine real, NaN,
 * infinity, negative zero, out-of-range magnitude) is recorded as an exception:
 * its position and original bytes are stored verbatim, so the round trip is
 * always exact. e and f are chosen by sampling for the pair that minimizes the
 * estimated encoded size. Built clean-room from Afroozeh, Kuffo, Boncz, "ALP:
 * Adaptive Lossless floating-Point Compression" (SIGMOD 2023), from the paper's
 * description only.
 *
 * On-disk:
 *   [uint8 e][uint8 f][uint8 intWidth][uint8 reserved]
 *   [int64 forBase][uint32 nExc]
 *   [bit-packed FOR offsets: n values of intWidth bits, exceptions carry 0]
 *   [exceptions: nExc * (uint32 pos)(w bytes original)]
 * ------------------------------------------------------------------------- */

/* powers of ten exact as doubles; 10^e fits int64 for e <= 18 */
#define ALP_MAX_EXP 18
static const double alp_F10[ALP_MAX_EXP + 1] = {
	1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9,
	1e10, 1e11, 1e12, 1e13, 1e14, 1e15, 1e16, 1e17, 1e18
};
static const double alp_IF10[ALP_MAX_EXP + 1] = {
	1e0, 1e-1, 1e-2, 1e-3, 1e-4, 1e-5, 1e-6, 1e-7, 1e-8, 1e-9,
	1e-10, 1e-11, 1e-12, 1e-13, 1e-14, 1e-15, 1e-16, 1e-17, 1e-18
};

/*
 * alp_try
 *		Encode one value at vp (w bytes, float4 or float8) under (e, f). Returns
 *		true and sets *encOut when the value reconstructs byte-for-byte through
 *		the decimal integer; false when it must be an exception. Decode uses the
 *		identical arithmetic, so a true result here guarantees an exact round trip.
 */
static inline bool
alp_try(const char *vp, int w, int e, int f, int64 *encOut)
{
	double		d;
	double		scaled;
	int64		enc;
	double		rec;

	if (w == 8)
		memcpy(&d, vp, 8);
	else
	{
		float		ff;

		memcpy(&ff, vp, 4);
		d = (double) ff;
	}

	if (!isfinite(d))
		return false;			/* NaN / +-inf: exception */

	scaled = d * alp_F10[e] * alp_IF10[f];
	/* keep well inside the int64 range so llround cannot overflow */
	if (scaled >= 9.0e18 || scaled <= -9.0e18)
		return false;

	enc = (int64) llround(scaled);
	rec = (double) enc * alp_F10[f] * alp_IF10[e];

	if (w == 8)
	{
		if (memcmp(&rec, vp, 8) != 0)
			return false;
	}
	else
	{
		float		rf = (float) rec;

		if (memcmp(&rf, vp, 4) != 0)
			return false;
	}

	*encOut = enc;
	return true;
}

static bool
encode_alp(const char *raw, uint32 rawLen, Form_pg_attribute att, uint32 n,
		   char **out, uint32 *outLen)
{
	int			w = att->attlen;	/* 4 or 8 */
	uint32		sample = n < 64 ? n : 64;
	uint32		step = sample > 0 ? (n / sample) : 1;
	int			bestE = 0;
	int			bestF = 0;
	double		bestCost = -1;
	int			e;
	uint32		i;
	int64	   *encVals;
	bool	   *isExc;
	int64		forBase = 0;
	uint64		maxOff = 0;
	uint32		nExc = 0;
	bool		haveEnc = false;
	int			intWidth;
	uint64	   *offs;
	StringInfoData buf;

	if (step == 0)
		step = 1;

	/* choose (e, f) on a sample: minimize estimated bytes = n * offsetBits/8 plus
	 * per-exception overhead, favouring the tightest integer range */
	for (e = 0; e <= ALP_MAX_EXP; e++)
	{
		int			f;

		for (f = 0; f <= e; f++)
		{
			uint32		sEnc = 0;
			uint32		sExc = 0;
			int64		mn = 0;
			int64		mx = 0;
			bool		first = true;
			double		cost;
			int			bits;
			uint32		k;

			for (k = 0; k < n; k += step)
			{
				int64		enc;

				if (alp_try(raw + (uint64) k * w, w, e, f, &enc))
				{
					sEnc++;
					if (first)
					{
						mn = mx = enc;
						first = false;
					}
					else
					{
						if (enc < mn)
							mn = enc;
						if (enc > mx)
							mx = enc;
					}
				}
				else
					sExc++;
			}

			if (sEnc == 0)
				continue;		/* nothing encodable at this (e, f) */

			bits = bits_needed((uint64) (mx - mn));
			/* estimate over the whole vector from the sample ratio */
			cost = (double) n * bits / 8.0 +
				(double) sExc / (double) (sEnc + sExc) * (double) n *
				(double) (sizeof(uint32) + w);

			if (bestCost < 0 || cost < bestCost)
			{
				bestCost = cost;
				bestE = e;
				bestF = f;
			}
		}
	}

	if (bestCost < 0)
		return false;			/* no value is a decimal under any (e, f) */

	/* encode every value under the chosen (e, f) */
	encVals = palloc(sizeof(int64) * n);
	isExc = palloc(sizeof(bool) * n);
	for (i = 0; i < n; i++)
	{
		int64		enc;

		if (alp_try(raw + (uint64) i * w, w, bestE, bestF, &enc))
		{
			isExc[i] = false;
			encVals[i] = enc;
			if (!haveEnc)
			{
				forBase = enc;
				haveEnc = true;
			}
			else if (enc < forBase)
				forBase = enc;
		}
		else
		{
			isExc[i] = true;
			nExc++;
		}
	}

	if (!haveEnc)
	{
		pfree(encVals);
		pfree(isExc);
		return false;			/* all exceptions: ALP cannot help */
	}

	/* FOR offsets; exception positions carry offset 0 (decode overwrites them) */
	offs = palloc(sizeof(uint64) * n);
	for (i = 0; i < n; i++)
	{
		if (isExc[i])
			offs[i] = 0;
		else
		{
			uint64		off = (uint64) (encVals[i] - forBase);

			offs[i] = off;
			if (off > maxOff)
				maxOff = off;
		}
	}
	intWidth = bits_needed(maxOff);

	initStringInfo(&buf);
	{
		uint8		hdr[4];

		hdr[0] = (uint8) bestE;
		hdr[1] = (uint8) bestF;
		hdr[2] = (uint8) intWidth;
		hdr[3] = 0;
		appendBinaryStringInfo(&buf, (char *) hdr, 4);
	}
	appendBinaryStringInfo(&buf, (char *) &forBase, sizeof(int64));
	appendBinaryStringInfo(&buf, (char *) &nExc, sizeof(uint32));
	bitpack(offs, n, intWidth, &buf);
	for (i = 0; i < n; i++)
	{
		if (isExc[i])
		{
			appendBinaryStringInfo(&buf, (char *) &i, sizeof(uint32));
			appendBinaryStringInfo(&buf, raw + (uint64) i * w, w);
		}
	}

	pfree(offs);
	pfree(encVals);
	pfree(isExc);

	if ((uint32) buf.len >= rawLen)
	{
		pfree(buf.data);
		return false;
	}
	*out = buf.data;
	*outLen = buf.len;
	return true;
}

static char *
decode_alp(const char *enc, uint32 encLen, int w, uint32 n, uint32 rawLen,
		   MemoryContext cx)
{
	char	   *raw = MemoryContextAlloc(cx, rawLen > 0 ? rawLen : 1);
	const char *p = enc;
	const char *end = enc + encLen;
	int			e;
	int			f;
	int			intWidth;
	int64		forBase;
	uint32		nExc;
	uint64	   *offs;
	uint32		packedBytes;
	uint32		i;

	if (w != 4 && w != 8)
		DECODE_CORRUPT("ALP on a non-float column");
	if ((uint64) n * (uint32) w != rawLen)
		DECODE_CORRUPT("ALP raw length does not match value count");
	if (encLen < 4 + sizeof(int64) + sizeof(uint32))
		DECODE_CORRUPT("ALP header truncated");

	e = (unsigned char) p[0];
	f = (unsigned char) p[1];
	intWidth = (unsigned char) p[2];
	p += 4;
	memcpy(&forBase, p, sizeof(int64));
	p += sizeof(int64);
	memcpy(&nExc, p, sizeof(uint32));
	p += sizeof(uint32);

	if (e > ALP_MAX_EXP || f > e)
		DECODE_CORRUPT("ALP exponent out of range");
	if (intWidth > 64)
		DECODE_CORRUPT("ALP bit width out of range");

	packedBytes = (uint32) (((uint64) n * (uint32) intWidth + 7) / 8);
	if (packedBytes > (uint32) (end - p))
		DECODE_CORRUPT("ALP offsets past encoded length");

	offs = palloc(sizeof(uint64) * (n > 0 ? n : 1));
	bitunpack((const unsigned char *) p, (uint32) (end - p), n, intWidth, offs);
	p += packedBytes;

	for (i = 0; i < n; i++)
	{
		int64		v = forBase + (int64) offs[i];
		double		rec = (double) v * alp_F10[f] * alp_IF10[e];

		COLUMNAR_DECODE_INTERRUPT(i);

		if (w == 8)
			memcpy(raw + (uint64) i * 8, &rec, 8);
		else
		{
			float		rf = (float) rec;

			memcpy(raw + (uint64) i * 4, &rf, 4);
		}
	}
	pfree(offs);

	/* patch exceptions with their exact original bytes */
	for (i = 0; i < nExc; i++)
	{
		uint32		pos;

		if (p + sizeof(uint32) + w > end)
			DECODE_CORRUPT("ALP exception past encoded length");
		memcpy(&pos, p, sizeof(uint32));
		p += sizeof(uint32);
		if (pos >= n)
			DECODE_CORRUPT("ALP exception position out of range");
		memcpy(raw + (uint64) pos * w, p, w);
		p += w;
	}

	return raw;
}

/* -------------------------------------------------------------------------
 * DICT: dictionary encoding for low-cardinality columns, fixed-width or varlena.
 * This is the cardinality axis of per-chunk selection (I5): distinct values are
 * stored once and each position becomes a small bit-packed code.
 *   [uint32 nDistinct][dictionary][uint8 codeWidth][bit-packed codes]
 * dictionary is nDistinct fixed-width values, or for varlena each entry is
 * [uint32 len][len bytes]. Only attempted up to a bounded distinct count.
 * ------------------------------------------------------------------------- */

#define DICT_MAX_DISTINCT 1024

static inline uint64 pgcolumnar_fnv1a(const char *p, uint32 n);

static bool
encode_dict(const char *raw, uint32 rawLen, Form_pg_attribute att, uint32 n,
			char **out, uint32 *outLen)
{
	int			w = att->attlen;
	uint32	   *codes = palloc(sizeof(uint32) * n);
	uint32		distOff[DICT_MAX_DISTINCT];
	uint32		distLen[DICT_MAX_DISTINCT];
	int			nd = 0;
	uint64	   *codes64;
	int			codeWidth;
	StringInfoData buf;
	uint32		pos = 0;
	uint32		i;
	int			j;

	/*
	 * Open-addressing hash of value -> distinct index (stored +1; 0 == empty),
	 * so "have I seen this value?" is O(1) average instead of a memcmp scan over
	 * every prior distinct value -- the O(n^2) that showed up as ~10% of a text
	 * load in profiling (issue #155). First-seen assignment order is unchanged,
	 * so the dictionary and codes are byte-identical to the linear build. Sized a
	 * power of two above 2 * DICT_MAX_DISTINCT so the load factor stays <= 1/2.
	 */
	enum { DICT_HASH_SLOTS = 2048 };
	uint32		hslot[DICT_HASH_SLOTS];

	memset(hslot, 0, sizeof(hslot));

	for (i = 0; i < n; i++)
	{
		const char *vp = raw + pos;
		uint32		vlen = (w > 0) ? (uint32) w
			: PgColumnarVarSizeAnyUnaligned(vp);
		uint64		h = pgcolumnar_fnv1a(vp, vlen);
		uint32		s = (uint32) (h & (DICT_HASH_SLOTS - 1));
		int			code = -1;

		for (;;)
		{
			uint32		entry = hslot[s];

			if (entry == 0)
				break;			/* empty slot: value unseen; insert here below */
			j = (int) (entry - 1);
			if (distLen[j] == vlen &&
				memcmp(raw + distOff[j], vp, vlen) == 0)
			{
				code = j;
				break;
			}
			s = (s + 1) & (DICT_HASH_SLOTS - 1);	/* collision: keep probing */
		}

		if (code < 0)
		{
			if (nd >= DICT_MAX_DISTINCT)
			{
				pfree(codes);
				return false;	/* too many distinct values for a dictionary */
			}
			distOff[nd] = pos;
			distLen[nd] = vlen;
			hslot[s] = (uint32) (nd + 1);	/* record at the empty slot found */
			code = nd;
			nd++;
		}
		codes[i] = (uint32) code;
		pos += vlen;
	}

	codeWidth = bits_needed(nd > 0 ? (uint64) (nd - 1) : 0);

	initStringInfo(&buf);
	{
		uint32		ndVal = (uint32) nd;

		appendBinaryStringInfo(&buf, (char *) &ndVal, sizeof(uint32));
	}
	for (j = 0; j < nd; j++)
	{
		if (w > 0)
			appendBinaryStringInfo(&buf, raw + distOff[j], w);
		else
		{
			appendBinaryStringInfo(&buf, (char *) &distLen[j], sizeof(uint32));
			appendBinaryStringInfo(&buf, raw + distOff[j], distLen[j]);
		}
	}
	appendStringInfoChar(&buf, (char) codeWidth);

	codes64 = palloc(sizeof(uint64) * n);
	for (i = 0; i < n; i++)
		codes64[i] = codes[i];
	bitpack(codes64, n, codeWidth, &buf);
	pfree(codes64);
	pfree(codes);

	if ((uint32) buf.len >= rawLen)
	{
		pfree(buf.data);
		return false;
	}
	*out = buf.data;
	*outLen = buf.len;
	return true;
}

static char *
decode_dict(const char *enc, uint32 encLen, Form_pg_attribute att, uint32 n,
			uint32 rawLen, MemoryContext cx)
{
	int			w = att->attlen;
	char	   *raw = MemoryContextAlloc(cx, rawLen > 0 ? rawLen : 1);
	const char *p = enc;
	const char *end = enc + encLen;
	uint32		nd;
	const char **dptr;
	uint32	   *dlen;
	int			codeWidth;
	uint64	   *codes;
	uint32		pos = 0;
	uint32		i;
	uint32		j;

	if (encLen < sizeof(uint32))
		DECODE_CORRUPT("DICT header truncated");
	memcpy(&nd, p, sizeof(uint32));
	p += sizeof(uint32);

	dptr = palloc(sizeof(char *) * (nd > 0 ? nd : 1));
	dlen = palloc(sizeof(uint32) * (nd > 0 ? nd : 1));
	for (j = 0; j < nd; j++)
	{
		if (w > 0)
		{
			if (p + w > end)
				DECODE_CORRUPT("DICT fixed entry past encoded length");
			dptr[j] = p;
			dlen[j] = (uint32) w;
			p += w;
		}
		else
		{
			if (p + sizeof(uint32) > end)
				DECODE_CORRUPT("DICT varlen header past encoded length");
			memcpy(&dlen[j], p, sizeof(uint32));
			p += sizeof(uint32);
			if (dlen[j] > (uint32) (end - p))
				DECODE_CORRUPT("DICT varlen entry past encoded length");
			dptr[j] = p;
			p += dlen[j];
		}
	}

	if (p + 1 > end)
		DECODE_CORRUPT("DICT code width past encoded length");
	codeWidth = (unsigned char) *p;
	p++;

	codes = palloc(sizeof(uint64) * (n > 0 ? n : 1));
	bitunpack((const unsigned char *) p, (uint32) (end - p), n, codeWidth, codes);

	for (i = 0; i < n; i++)
	{
		uint32		code = (uint32) codes[i];

		COLUMNAR_DECODE_INTERRUPT(i);
		if (code >= nd)
			DECODE_CORRUPT("DICT code out of range");
		if (dlen[code] > rawLen - pos)	/* pos <= rawLen invariant */
			DECODE_CORRUPT("DICT output exceeds raw length");
		memcpy(raw + pos, dptr[code], dlen[code]);
		pos += dlen[code];
	}
	pfree(codes);

	/*
	 * The codes must fill the raw stream exactly. Without this, a descriptor
	 * whose value_raw_length exceeds what the codes decode to leaves the tail of
	 * the (unzeroed) raw buffer uninitialized; a varlena column then reads a
	 * length prefix out of that garbage and dereferences out of bounds. The
	 * pre-dispatch switch cannot check a DICT chunk's raw length up front (it is
	 * the sum of the entry lengths, known only here), so the check lives here,
	 * mirroring decode_fsst_shared.
	 */
	if (pos != rawLen)
		DECODE_CORRUPT("DICT output length does not match raw length");
	return raw;
}

/* -------------------------------------------------------------------------
 * FSST: static symbol-table string compression for varlena columns (E2).
 *
 * FSST replaces frequent 1-8 byte substrings of the raw value stream with
 * single-byte codes drawn from a per-chunk symbol table of up to 255 symbols.
 * A reserved escape code (255) precedes any literal byte the table cannot
 * cover, so the transform is exact and lossless for arbitrary bytes. Because
 * the raw value stream is just the concatenation of the chunk's varlena values
 * (each self-delimited by its own length header), compressing and decompressing
 * the whole stream reproduces every value byte-for-byte; no per-value bookkeeping
 * is needed here, and the reader re-splits values downstream as before.
 *
 * The symbol table is built clean-room from the published description of FSST
 * [Boncz, Neumann, Leis, "FSST: Fast Static Symbol Table compression", VLDB
 * 2020]: start from an empty table and, for a few rounds, compress a sample of
 * the corpus with the current table while counting each emitted symbol and each
 * concatenation of two adjacent symbols (capped at 8 bytes); the highest-gain
 * candidates become the next round's table. This grows symbols from single
 * bytes toward longer frequent strings and converges in a handful of rounds.
 *
 * On-disk layout:
 *   [uint8 nSym]
 *   nSym x ( [uint8 len] [len bytes] )         -- the symbol table
 *   [ code bytes to end ]                       -- 255 = escape + 1 literal byte;
 *                                                  else code c => symbol c's bytes
 * ------------------------------------------------------------------------- */

#define FSST_MAX_SYMBOLS 255	/* codes 0..254; 255 is the escape marker */
#define FSST_ESCAPE 255
#define FSST_MAX_SYMLEN 8
#define FSST_ROUNDS 4			/* enough to grow symbols to 8 bytes and refine */
#define FSST_SAMPLE_CAP 262144	/* bytes of the corpus used to build the table.
								 * Larger than a single vector on purpose: the table
								 * is built once per column chunk (E3b), so a bigger
								 * sample trains a more representative table (tighter
								 * codes) at negligible amortized cost. */
#define FSST_MIN_RAWLEN 64		/* below this, table overhead cannot pay off */
#define FSST_COUNT_SLOTS (1u << 17)	/* candidate-counting hash capacity */
#define FSST_DECODE_SLACK 8		/* headroom so a symbol is one 8-byte store (#768) */

typedef struct FsstTable
{
	int			nSym;
	uint64		symVal[FSST_MAX_SYMBOLS];	/* packed bytes, low byte first */
	uint8		symLen[FSST_MAX_SYMBOLS];	/* 1..8 */
} FsstTable;

/* (val, len) -> code lookup for longest-match, open-addressed */
typedef struct FsstLookup
{
	uint64	   *keyVal;
	uint8	   *keyLen;
	int32	   *code;			/* -1 when the slot is empty */
	uint32		mask;
} FsstLookup;

/* candidate counter cell for the build rounds */
typedef struct FsstCount
{
	uint64		val;
	uint32		count;			/* accumulated gain (sum of symbol lengths) */
	uint8		len;
	uint8		used;
} FsstCount;

/* a used counter cell, extracted for top-N selection */
typedef struct FsstCand
{
	uint32		count;
	uint64		val;
	uint8		len;
} FsstCand;

static inline uint32
fsst_hash(uint64 val, uint8 len)
{
	uint64		h = val * 0x9E3779B97F4A7C15UL;

	h ^= (uint64) len * 0x100000001B3UL;
	h ^= h >> 29;
	return (uint32) h;
}

static void
fsst_lookup_build(FsstLookup *lk, const FsstTable *t)
{
	uint32		cap = 512;
	uint32		i;
	int			s;

	while (cap < (uint32) t->nSym * 4 + 4)
		cap <<= 1;
	lk->mask = cap - 1;
	lk->keyVal = palloc(sizeof(uint64) * cap);
	lk->keyLen = palloc(sizeof(uint8) * cap);
	lk->code = palloc(sizeof(int32) * cap);
	for (i = 0; i < cap; i++)
		lk->code[i] = -1;

	for (s = 0; s < t->nSym; s++)
	{
		uint32		slot = fsst_hash(t->symVal[s], t->symLen[s]) & lk->mask;

		while (lk->code[slot] != -1)
			slot = (slot + 1) & lk->mask;
		lk->code[slot] = s;
		lk->keyVal[slot] = t->symVal[s];
		lk->keyLen[slot] = t->symLen[s];
	}
}

static void
fsst_lookup_free(FsstLookup *lk)
{
	pfree(lk->keyVal);
	pfree(lk->keyLen);
	pfree(lk->code);
}

static inline int
fsst_lookup_find(const FsstLookup *lk, uint64 val, uint8 len)
{
	uint32		slot = fsst_hash(val, len) & lk->mask;

	while (lk->code[slot] != -1)
	{
		if (lk->keyVal[slot] == val && lk->keyLen[slot] == len)
			return lk->code[slot];
		slot = (slot + 1) & lk->mask;
	}
	return -1;
}

/* longest table symbol matching the prefix at p; -1 if none (byte is escaped) */
static inline int
fsst_longest_match(const FsstLookup *lk, const unsigned char *p,
				   uint32 remaining, int *matchLen)
{
	int			maxL = remaining < FSST_MAX_SYMLEN ? (int) remaining : FSST_MAX_SYMLEN;
	int			L;

	for (L = maxL; L >= 1; L--)
	{
		uint64		v = 0;
		int			c;

		memcpy(&v, p, L);
		c = fsst_lookup_find(lk, v, (uint8) L);
		if (c >= 0)
		{
			*matchLen = L;
			return c;
		}
	}
	return -1;
}

static void
fsst_count_add(FsstCount *tbl, uint32 mask, uint32 *nUsed,
			   uint64 val, uint8 len, uint32 gain)
{
	uint32		slot = fsst_hash(val, len) & mask;

	while (tbl[slot].used)
	{
		if (tbl[slot].val == val && tbl[slot].len == len)
		{
			tbl[slot].count += gain;
			return;
		}
		slot = (slot + 1) & mask;
	}
	/* stop admitting new keys past 75% load so open addressing stays fast */
	if (*nUsed >= (mask + 1) - (mask + 1) / 4)
		return;
	tbl[slot].used = 1;
	tbl[slot].val = val;
	tbl[slot].len = len;
	tbl[slot].count = gain;
	(*nUsed)++;
}

static int
fsst_cand_cmp(const void *a, const void *b)
{
	const FsstCand *x = (const FsstCand *) a;
	const FsstCand *y = (const FsstCand *) b;

	if (x->count != y->count)
		return x->count > y->count ? -1 : 1;
	/* deterministic tie-break: longer first, then by value */
	if (x->len != y->len)
		return x->len > y->len ? -1 : 1;
	if (x->val != y->val)
		return x->val < y->val ? -1 : 1;
	return 0;
}

/*
 * Build the symbol table for a corpus by iterative gain counting: each round
 * compresses the sample with the current table, counting every emitted symbol
 * and each adjacent-symbol concatenation as a candidate, then keeps the top
 * FSST_MAX_SYMBOLS by gain. Round 0 starts from an empty table (every byte is a
 * single-byte candidate and adjacent bytes form 2-byte candidates), and each
 * later round can double symbol lengths up to the 8-byte cap.
 */
static void
build_fsst_table(const char *corpus, uint32 corpusLen, FsstTable *out)
{
	uint32		sampleLen = corpusLen < FSST_SAMPLE_CAP ? corpusLen : FSST_SAMPLE_CAP;
	uint32		slots;
	uint32		mask;
	FsstCount  *cnt;
	FsstTable	cur;
	int			round;

	/*
	 * Size the candidate hash to the sample so small vectors stay cheap: at most
	 * ~2 candidates (the symbol and one concatenation) arise per input position,
	 * so 2x the sample length, rounded to a power of two and clamped, keeps the
	 * load factor low without a fixed multi-megabyte allocation per vector. One
	 * allocation is reused (zeroed) across rounds.
	 */
	slots = 4096;
	while (slots < sampleLen * 2 && slots < FSST_COUNT_SLOTS)
		slots <<= 1;
	mask = slots - 1;
	cnt = palloc(sizeof(FsstCount) * slots);

	cur.nSym = 0;
	for (round = 0; round < FSST_ROUNDS; round++)
	{
		FsstLookup	lk;
		FsstCand   *cands;
		uint32		nUsed = 0;
		uint32		nc = 0;
		uint32		pos = 0;
		uint8		prevLen = 0;
		uint64		prevVal = 0;
		uint32		slot;
		FsstTable	next;
		int			s;

		memset(cnt, 0, sizeof(FsstCount) * slots);
		fsst_lookup_build(&lk, &cur);

		while (pos < sampleLen)
		{
			int			L = 1;
			int			c = fsst_longest_match(&lk,
											   (const unsigned char *) corpus + pos,
											   sampleLen - pos, &L);
			uint64		val;
			uint8		len;

			if (c >= 0)
			{
				val = cur.symVal[c];
				len = cur.symLen[c];
			}
			else
			{
				val = (unsigned char) corpus[pos];
				len = 1;
				L = 1;
			}

			fsst_count_add(cnt, mask, &nUsed, val, len, len);
			if (prevLen > 0 && prevLen + len <= FSST_MAX_SYMLEN)
			{
				uint64		catVal = prevVal | (val << (prevLen * 8));
				uint8		catLen = (uint8) (prevLen + len);

				fsst_count_add(cnt, mask, &nUsed, catVal, catLen, catLen);
			}
			prevVal = val;
			prevLen = len;
			pos += L;
		}
		fsst_lookup_free(&lk);

		cands = palloc(sizeof(FsstCand) * (nUsed > 0 ? nUsed : 1));
		for (slot = 0; slot < slots; slot++)
		{
			if (cnt[slot].used)
			{
				cands[nc].count = cnt[slot].count;
				cands[nc].len = cnt[slot].len;
				cands[nc].val = cnt[slot].val;
				nc++;
			}
		}

		if (nc == 0)
		{
			pfree(cands);
			cur.nSym = 0;
			break;
		}
		qsort(cands, nc, sizeof(FsstCand), fsst_cand_cmp);
		next.nSym = (int) (nc < FSST_MAX_SYMBOLS ? nc : FSST_MAX_SYMBOLS);
		for (s = 0; s < next.nSym; s++)
		{
			next.symVal[s] = cands[s].val;
			next.symLen[s] = cands[s].len;
		}
		pfree(cands);
		cur = next;
	}
	pfree(cnt);
	*out = cur;
}

/* serialize a symbol table as [uint8 nSym][ nSym x (uint8 len, len bytes) ] */
static void
fsst_serialize_table(const FsstTable *t, StringInfo out)
{
	int			s;

	appendStringInfoChar(out, (char) t->nSym);
	for (s = 0; s < t->nSym; s++)
	{
		uint64		v = t->symVal[s];

		appendStringInfoChar(out, (char) t->symLen[s]);
		appendBinaryStringInfo(out, (char *) &v, t->symLen[s]);
	}
}

/* parse a serialized symbol table; false (with a clean error) on corruption */
static void
fsst_deserialize_table(const char *p, uint32 len, FsstTable *t)
{
	const char *end = p + len;
	uint32		nSym;
	uint32		i;

	if (len < 1)
		DECODE_CORRUPT("FSST shared table truncated");
	nSym = (unsigned char) *p++;
	if (nSym > FSST_MAX_SYMBOLS)
		DECODE_CORRUPT("FSST shared table symbol count out of range");
	t->nSym = (int) nSym;
	for (i = 0; i < nSym; i++)
	{
		uint8		L;
		uint64		v = 0;

		if (p >= end)
			DECODE_CORRUPT("FSST shared table symbol length past end");
		L = (unsigned char) *p++;
		if (L < 1 || L > FSST_MAX_SYMLEN)
			DECODE_CORRUPT("FSST shared table symbol length invalid");
		if ((uint32) (end - p) < L)
			DECODE_CORRUPT("FSST shared table symbol bytes past end");
		memcpy(&v, p, L);
		t->symVal[i] = v;
		t->symLen[i] = L;
		p += L;
	}
}

/* FNV-1a over a byte range: a cheap, well-mixed 64-bit hash for the distinct
 * probe below. Our own trivial implementation of the public FNV-1a formula, used
 * only to bucket values while counting distinct ones. */
static inline uint64
pgcolumnar_fnv1a(const char *p, uint32 n)
{
	uint64		h = UINT64CONST(1469598103934665603);
	uint32		i;

	for (i = 0; i < n; i++)
	{
		h ^= (unsigned char) p[i];
		h *= UINT64CONST(1099511628211);
	}
	return h;
}

/*
 * PgColumnarFsstDictWins
 *		Cheap pre-check for the FSST table build (issue #155): would dictionary
 *		encoding win outright, making the costly FSST symbol-table build wasted
 *		work? FSST is only attempted per vector when a cheaper encoding has not
 *		already shrunk the vector well; a low-cardinality column is compressed far
 *		below that threshold by the dictionary, so the FSST table would be built
 *		(the single largest cost of a text load, ~22% in profiling) and then never
 *		used. Count distinct values over the corpus with a small open-addressing
 *		hash set and stop the instant the count exceeds the dictionary's distinct
 *		cap: at or below the cap the dictionary is viable for every 1024-row vector
 *		and wins, so skipping the build produces byte-identical storage; above it
 *		the column is a genuine FSST candidate and the caller builds the table.
 *		This is our own heuristic; FSST is the public scheme (VLDB 2020).
 */
bool
PgColumnarFsstDictWins(const char *corpus, uint32 corpusLen)
{
	/* Open-addressing set of value hashes, capacity a power of two comfortably
	 * above the distinct cap so the load factor at the early-exit stays low. */
	enum { FSST_CARD_SLOTS = 4096 };	/* > 2 * DICT_MAX_DISTINCT */
	uint64		slot[FSST_CARD_SLOTS];
	bool		used[FSST_CARD_SLOTS];
	int			distinct = 0;
	uint32		pos = 0;

	memset(used, 0, sizeof(used));
	while (pos < corpusLen)
	{
		uint32		vlen = PgColumnarVarSizeAnyUnaligned(corpus + pos);
		uint64		h;
		uint32		s;

		if (vlen == 0 || pos + vlen > corpusLen)
			return false;		/* malformed run: let the caller build normally */

		h = pgcolumnar_fnv1a(corpus + pos, vlen);
		s = (uint32) (h & (FSST_CARD_SLOTS - 1));
		while (used[s] && slot[s] != h)
			s = (s + 1) & (FSST_CARD_SLOTS - 1);
		if (!used[s])
		{
			used[s] = true;
			slot[s] = h;
			if (++distinct > DICT_MAX_DISTINCT)
				return false;	/* high cardinality: a real FSST candidate */
		}
		pos += vlen;
	}
	return true;				/* dictionary wins; the FSST build is skippable */
}

/*
 * PgColumnarFsstBuildChunkTable
 *		Build one FSST symbol table for a whole column chunk (E3b). The expensive
 *		iterative table build runs once here; the serialized table is handed back
 *		to be stored once per chunk and reused by every FSST vector.
 */
bool
PgColumnarFsstBuildChunkTable(const char *corpus, uint32 corpusLen,
							Form_pg_attribute att, char **tableOut,
							uint32 *tableLenOut)
{
	FsstTable	table;
	StringInfoData buf;

	if (att->attlen != -1)
		return false;			/* varlena only */
	if (corpusLen < FSST_MIN_RAWLEN)
		return false;

	build_fsst_table(corpus, corpusLen, &table);
	if (table.nSym == 0)
		return false;

	initStringInfo(&buf);
	fsst_serialize_table(&table, &buf);
	*tableOut = buf.data;
	*tableLenOut = (uint32) buf.len;
	return true;
}

/*
 * Encode one vector against a chunk-shared table, producing a bare code stream
 * (no inline table). The per-vector win test compares the code stream alone to
 * the raw length: the shared table's bytes are amortized across the chunk and
 * not charged here, so FSST wins per vector whenever the codes are smaller.
 *
 * That test is only meaningful once PgColumnarFsstHelpsCompressed has decided the
 * chunk should use FSST at all; it is what charges the table and what compares
 * against the compressed size actually written. See the comment on it below.
 */
static bool
encode_fsst_shared(const char *raw, uint32 rawLen, const char *table,
				   uint32 tableLen, char **out, uint32 *outLen)
{
	FsstTable	t;
	FsstLookup	lk;
	StringInfoData buf;
	uint32		pos = 0;

	if (rawLen < FSST_MIN_RAWLEN)
		return false;

	fsst_deserialize_table(table, tableLen, &t);
	if (t.nSym == 0)
		return false;
	fsst_lookup_build(&lk, &t);

	initStringInfo(&buf);
	while (pos < rawLen)
	{
		int			L = 1;
		int			c = fsst_longest_match(&lk,
										   (const unsigned char *) raw + pos,
										   rawLen - pos, &L);

		if (c >= 0)
		{
			appendStringInfoChar(&buf, (char) c);
			pos += L;
		}
		else
		{
			appendStringInfoChar(&buf, (char) FSST_ESCAPE);
			appendStringInfoChar(&buf, raw[pos]);
			pos += 1;
		}

		if ((uint32) buf.len >= rawLen)	/* not winning this vector */
		{
			fsst_lookup_free(&lk);
			pfree(buf.data);
			return false;
		}
	}
	fsst_lookup_free(&lk);

	if ((uint32) buf.len >= rawLen)
	{
		pfree(buf.data);
		return false;
	}
	*out = buf.data;
	*outLen = buf.len;
	return true;
}

/*
 * Would FSST still pay once the block compressor has had its turn?
 *
 * The per-vector win test above compares encoded lengths, but the bytes that
 * reach disk are compressed afterwards (pgcolumnar_write_state.c compresses the
 * whole encoded stream). Those are different objectives, and for some shapes
 * they disagree sharply: FSST turns highly repetitive text into a stream of
 * high-entropy codes, which is smaller than the text but far less compressible
 * than the text was. Scored uncompressed FSST wins; stored compressed it loses.
 *
 * Worse, the guard that gates this path -- only try FSST when nothing else
 * compressed the vector -- selects for exactly that case, because a vector no
 * encoding could shrink is usually one the general-purpose compressor can.
 *
 * So decide on the quantity that matters. The caller has already gathered a
 * bounded sample of the chunk's value streams to train the symbol table; run
 * both candidates through the configured codec and keep FSST only if it is
 * genuinely smaller after compression. The symbol table's own bytes are charged
 * to FSST here, which is also the only place they are charged at all.
 *
 * Cost is one FSST encode and two compressions of a sample capped at 256 kB,
 * once per column chunk, and it is repaid immediately when the answer is no:
 * the per-vector encode is then skipped for every vector in the chunk.
 */
bool
PgColumnarFsstHelpsCompressed(const char *corpus, uint32 corpusLen,
							const char *table, uint32 tableLen,
							int compressionType, int compressionLevel)
{
	char	   *codes = NULL;
	char	   *plainComp = NULL;
	char	   *codesComp = NULL;
	uint32		codesLen = 0;
	uint32		plainCompLen = 0;
	uint32		codesCompLen = 0;
	int			usedType;
	int			usedLevel;
	bool		helps;

	/*
	 * With no codec downstream the encoded length IS the stored length, so the
	 * per-vector test is already measuring the right thing.
	 */
	if (compressionType == COLUMNAR_COMPRESSION_NONE)
		return true;

	if (!encode_fsst_shared(corpus, corpusLen, table, tableLen, &codes, &codesLen))
		return false;

	PgColumnarCompressValueStream(corpus, corpusLen, compressionType,
								compressionLevel, &plainComp, &plainCompLen,
								&usedType, &usedLevel);
	PgColumnarCompressValueStream(codes, codesLen, compressionType,
								compressionLevel, &codesComp, &codesCompLen,
								&usedType, &usedLevel);

	/*
	 * Keep FSST only when the compressed win clears pgcolumnar.fsst_min_gain_percent,
	 * not on any win at all. Encoding FSST codes for every vector of the chunk is a
	 * dominant cost of a text/varlena load (issue #155), and a sub-margin win --
	 * e.g. FSST ~1% smaller after compression on numeric -- does not repay it. At
	 * 0 this is the original "any win keeps it"; the default is 5. The comparison is
	 * multiplied out rather than subtracting a percentage of plainCompLen, so it
	 * cannot underflow when plainCompLen is small.
	 */
	helps = (((uint64) codesCompLen + tableLen) * 100
			 < (uint64) plainCompLen * (uint64) (100 - pgcolumnar_fsst_min_gain_percent));

	pfree(codes);
	if (plainComp)
		pfree(plainComp);
	if (codesComp)
		pfree(codesComp);

	return helps;
}

/* decode a bare code stream against a chunk-shared serialized table (E3b) */
static char *
decode_fsst_shared(const char *enc, uint32 encLen, const char *table,
				   uint32 tableLen, uint32 rawLen, MemoryContext cx)
{
	/*
	 * FSST_DECODE_SLACK bytes of headroom past rawLen, so the decode loop can
	 * store a symbol as one unaligned 8-byte write whatever its length. The
	 * write starts at outPos, which the bounds check in the loop holds at
	 * rawLen - L or less, so it reaches at most rawLen + 7.
	 */
	char	   *raw = MemoryContextAlloc(cx, (rawLen > 0 ? rawLen : 1) +
										 FSST_DECODE_SLACK);
	const char *p = enc;
	const char *end = enc + encLen;
	const char *tp;
	const char *tend;
	uint64		symVal[FSST_MAX_SYMBOLS];	/* symbol bytes, memcpy'd in and out */
	uint8		symLen[FSST_MAX_SYMBOLS];
	uint32		nSym;
	uint32		outPos = 0;
	uint32		i;
	uint32		iter = 0;		/* decode-loop iterations, for the interrupt stride */

	/* parse the shared table (pointers reference the caller's descriptor bytes) */
	if (tableLen < 1)
		DECODE_CORRUPT("FSST shared table missing");
	tp = table;
	tend = table + tableLen;
	nSym = (unsigned char) *tp++;
	if (nSym > FSST_MAX_SYMBOLS)
		DECODE_CORRUPT("FSST shared table symbol count out of range");
	for (i = 0; i < nSym; i++)
	{
		uint8		L;

		if (tp >= tend)
			DECODE_CORRUPT("FSST shared table symbol length past end");
		L = (unsigned char) *tp++;
		if (L < 1 || L > FSST_MAX_SYMLEN)
			DECODE_CORRUPT("FSST shared table symbol length invalid");
		if ((uint32) (tend - tp) < L)
			DECODE_CORRUPT("FSST shared table symbol bytes past end");
		symLen[i] = L;
		/*
		 * Pack the symbol into a uint64 once, here, rather than copying L bytes
		 * out of the table on every occurrence in the loop. nSym is at most 255
		 * and this runs once per chunk.
		 *
		 * ENDIANNESS. This is safe on any byte order for one reason only: both
		 * ends are a byte-wise memcpy, so the bytes round-trip, and symVal[i] is
		 * NEVER INTERPRETED AS A NUMBER. Do not "clarify" this into
		 * `v |= (uint64) tp[j] << (8 * j)`. That builds a little-endian integer,
		 * is identical on x86, and is wrong on a big-endian machine, where the
		 * store below would write the symbol reversed.
		 *
		 * The L < 1 || L > FSST_MAX_SYMLEN check above is load-bearing here in a
		 * way it was not before: it is what keeps this memcpy inside the uint64.
		 */
		{
			uint64		v = 0;

			memcpy(&v, tp, L);
			symVal[i] = v;
		}
		tp += L;
	}

	/*
	 * The decode loop, and the one that needs the interrupt check (#712). The
	 * symbol-table parse above is bounded by tableLen, a few KB at most; this
	 * one is bounded by encLen, which is a whole vector's code stream.
	 *
	 * A separate iteration counter rather than a byte offset, because the stride
	 * check is a mask: `p` advances by one or two per code, so a test on
	 * (p - enc) can STEP OVER the exact multiple and never fire. Counting
	 * iterations advances by exactly one and cannot skip the boundary. Starting
	 * at zero also means the first iteration always checks, so a vector shorter
	 * than the stride is still interruptible at its start.
	 */
	while (p < end)
	{
		unsigned char c;

		COLUMNAR_DECODE_INTERRUPT(iter);
		iter++;
		c = (unsigned char) *p++;

		if (c == FSST_ESCAPE)
		{
			if (p >= end)
				DECODE_CORRUPT("FSST escape at encoded length");
			if (outPos >= rawLen)
				DECODE_CORRUPT("FSST output exceeds raw length");
			raw[outPos++] = *p++;
		}
		else
		{
			uint8		L;

			if (c >= nSym)
				DECODE_CORRUPT("FSST code out of range");
			L = symLen[c];
			if (L > rawLen - outPos)	/* outPos <= rawLen invariant */
				DECODE_CORRUPT("FSST output exceeds raw length");
			/*
			 * One fixed 8-byte store rather than a variable-length memcpy. A
			 * memcpy whose length is only known at run time cannot become a
			 * single store; this can, and the slack allocated above makes the
			 * over-write safe. Only symLen[c] bytes are kept: outPos advances
			 * by L, so the next store overwrites the remainder.
			 */
			memcpy(raw + outPos, &symVal[c], sizeof(uint64));
			outPos += L;
		}
	}

	if (outPos != rawLen)
		DECODE_CORRUPT("FSST output length does not match raw length");
	return raw;
}

/* -------------------------------------------------------------------------
 * public entry points
 * ------------------------------------------------------------------------- */

/* -------------------------------------------------------------------------
 * compression-block run iterator (I2)
 * ------------------------------------------------------------------------- */

void
PgColumnarBlockReaderInit(PgColumnarBlockReader *br, const char *raw,
						uint64 valueCount, int width)
{
	br->raw = raw;
	br->valueCount = valueCount;
	br->width = width;
	br->pos = 0;
}

bool
PgColumnarBlockNextRun(PgColumnarBlockReader *br, const char **valBytes,
					 uint64 *runLen)
{
	const char *v;
	uint64		run = 1;
	int			w = br->width;

	if (br->pos >= br->valueCount)
		return false;

	v = br->raw + br->pos * (uint64) w;
	while (br->pos + run < br->valueCount &&
		   memcmp(br->raw + (br->pos + run) * (uint64) w, v, w) == 0)
		run++;

	*valBytes = v;
	*runLen = run;
	br->pos += run;
	return true;
}

const char *
PgColumnarEncodingName(int encodingType)
{
	switch (encodingType)
	{
		case COLUMNAR_ENCODING_NONE:
			return "none";
		case COLUMNAR_ENCODING_RLE:
			return "rle";
		case COLUMNAR_ENCODING_FOR:
			return "for";
		case COLUMNAR_ENCODING_DELTA:
			return "delta";
		case COLUMNAR_ENCODING_GORILLA:
			return "gorilla";
		case COLUMNAR_ENCODING_DOD:
			return "dod";
		case COLUMNAR_ENCODING_DICT:
			return "dict";
		case COLUMNAR_ENCODING_ALP:
			return "alp";
		case COLUMNAR_ENCODING_FSST:
			return "fsst";
		default:
			return "unknown";
	}
}


/*
 * Sample-based candidate selection.
 *
 * Applying every candidate to the whole vector and keeping the smallest is
 * correct but wasteful: measured on a 6,000,000-row load, the trials that lose
 * are 9.0 s of a 20.8 s load, while the winner's own encode is 6.6 s (see
 * design/CASCADE_ENCODING_PLAN.md). Estimating on a sample and applying only the
 * best candidates recovers most of the wasted part.
 *
 * The sample is WINDOWED: a number of evenly spread windows of consecutive
 * values, rather than a prefix or a pure stride. Each shape fails differently.
 *
 * A prefix describes the head and not the tail: an id column's first values look
 * perfectly delta-encodable whether or not the rest is, and a column sorted on
 * another key has runs at the front that do not continue.
 *
 * A pure stride fixes that for the candidates that read a global property (delta,
 * delta-of-delta, frame-of-reference, Gorilla all survive it up to a scale
 * factor) but destroys the one that reads a local property. Run-length sees only
 * runs longer than the stride, so at a 10,000-row vector and a 2048-value sample
 * the stride is 4 and runs of two or three vanish entirely; raising
 * chunk_group_row_limit makes the stride, and the blind spot, proportionally
 * larger. The column would just store larger, with no error and no test failure.
 *
 * Windows keep both properties: spread across the whole vector, so the head does
 * not decide, and consecutive within a window, so runs are visible at their real
 * length and successive values are genuinely successive.
 *
 * Getting this wrong costs size, never correctness: whatever is chosen is applied
 * in full and recorded per vector, and the reader decodes what the descriptor
 * says.
 */
#define ENCODE_SAMPLE_WINDOW 64

/*
 * Below this many sampled values nothing can be ranked: every candidate writes a
 * fixed header, and against a handful of values that header alone exceeds the raw
 * size, so no candidate beats raw and the vector would be stored unencoded. A
 * setting that small therefore means the exhaustive path, not "sample tiny": the
 * failure mode of the alternative is a silently much larger table.
 */
#define ENCODE_SAMPLE_MIN 128

static char *
build_sample(const char *raw, int w, uint32 n, uint32 want, uint32 *sampleN)
{
	uint32		win = ENCODE_SAMPLE_WINDOW;
	uint32		nwin;
	uint32		i;
	uint32		taken = 0;
	char	   *sample;

	if (want == 0 || n <= want)
		return NULL;			/* nothing to gain */
	if (want < win)
		win = want;
	nwin = want / win;
	if (nwin < 2 || n <= win)
		return NULL;			/* too few windows to represent the vector */

	sample = palloc((Size) nwin * win * w);
	for (i = 0; i < nwin; i++)
	{
		/* windows evenly spread from the first value to the last full window */
		uint64		start = ((uint64) i * (n - win)) / (nwin - 1);

		memcpy(sample + (Size) taken * w, raw + (Size) start * w,
			   (Size) win * w);
		taken += win;
	}
	*sampleN = taken;
	return sample;
}

/*
 * PgColumnarEncodeChunk
 *		Choose and apply the best lightweight encoding for one chunk's raw value
 *		stream. Returns the encoding code used and sets out and outLen to the
 *		encoded buffer. When no encoding beats the raw size, returns NONE and
 *		sets out to the raw pointer itself (no copy); callers must treat it as
 *		read-only and must not free it. Encoded buffers for the other cases are
 *		palloc'd in the current memory context.
 *
 *		Selection is adaptive per chunk: each applicable encoding is measured and
 *		the smallest pre-compression result wins (each encoder bails cheaply when
 *		it cannot help). The candidate set spans the encoding axes -- runs (rle),
 *		range (for), sequences (delta, dod), cardinality (dict), floats (gorilla,
 *		alp), and shared-substring strings (fsst) -- so the choice adapts to the
 *		column's data (I5).
 *
 *		fsstTable/fsstTableLen, when non-NULL, is the chunk-shared FSST symbol
 *		table (E3b): for a varlena column the FSST candidate encodes against it,
 *		producing a bare code stream, so the table build is paid once per chunk by
 *		the caller rather than once per vector here. When NULL, FSST is not a
 *		candidate.
 */
int
PgColumnarEncodeChunk(const char *raw, uint32 rawLen, Form_pg_attribute att,
					uint64 valueCount, const char *fsstTable, uint32 fsstTableLen,
					char **out, uint32 *outLen)
{
	int			w = att->attlen;
	uint32		n = (uint32) valueCount;
	int			bestCode = COLUMNAR_ENCODING_NONE;
	char	   *bestBuf = NULL;
	uint32		bestLen = rawLen;
	char	   *buf;
	uint32		len;

	/* nothing to do for an empty (all-null) chunk */
	if (rawLen == 0 || n == 0)
	{
		*out = (char *) raw;
		*outLen = rawLen;
		return COLUMNAR_ENCODING_NONE;
	}

	/*
	 * Choose the candidates to apply. With sampling on and a chunk big enough to
	 * sample, each candidate is measured on a windowed sample and only the best
	 * two are applied to the whole chunk; two rather than one because the sample
	 * ranks closely-matched candidates unreliably, and the second application is
	 * cheap next to the three it replaces. With sampling off, or a chunk too
	 * small to sample, every candidate is applied as before.
	 */
	if (w > 0 && pgcolumnar_encoding_sample_rows >= ENCODE_SAMPLE_MIN)
	{
		uint32		sampleN = 0;
		char	   *sample = build_sample(raw, w, n,
										  (uint32) pgcolumnar_encoding_sample_rows,
										  &sampleN);

		if (sample != NULL)
		{
			uint32		sampleRawLen = sampleN * (uint32) w;
			int			cand[2] = {COLUMNAR_ENCODING_NONE, COLUMNAR_ENCODING_NONE};
			uint32		best[2] = {sampleRawLen, sampleRawLen};
			int			c;

#define SAMPLE_TRY(code, expr) \
			do { \
				if (expr) \
				{ \
					if (len < best[0]) \
					{ \
						best[1] = best[0]; cand[1] = cand[0]; \
						best[0] = len; cand[0] = (code); \
					} \
					else if (len < best[1]) \
					{ \
						best[1] = len; cand[1] = (code); \
					} \
					pfree(buf); \
				} \
			} while (0)

			SAMPLE_TRY(COLUMNAR_ENCODING_RLE,
					   encode_rle(sample, sampleRawLen, w, sampleN, &buf, &len));
			if (is_packable_int(att))
			{
				SAMPLE_TRY(COLUMNAR_ENCODING_FOR,
						   encode_for(sample, sampleRawLen, w, sampleN, &buf, &len));
				SAMPLE_TRY(COLUMNAR_ENCODING_DELTA,
						   encode_delta(sample, sampleRawLen, w, sampleN, &buf, &len));
				SAMPLE_TRY(COLUMNAR_ENCODING_DOD,
						   encode_dod(sample, sampleRawLen, w, sampleN, &buf, &len));
			}
			if (is_gorilla_float(att))
			{
				SAMPLE_TRY(COLUMNAR_ENCODING_GORILLA,
						   encode_gorilla(sample, sampleRawLen, w, sampleN, &buf, &len));
				SAMPLE_TRY(COLUMNAR_ENCODING_ALP,
						   encode_alp(sample, sampleRawLen, att, sampleN, &buf, &len));
			}
			SAMPLE_TRY(COLUMNAR_ENCODING_DICT,
					   encode_dict(sample, sampleRawLen, att, sampleN, &buf, &len));
#undef SAMPLE_TRY
			pfree(sample);

			/* apply the one or two the sample liked, to the whole chunk */
			for (c = 0; c < 2; c++)
			{
				bool		ok = false;

				switch (cand[c])
				{
					case COLUMNAR_ENCODING_RLE:
						ok = encode_rle(raw, rawLen, w, n, &buf, &len);
						break;
					case COLUMNAR_ENCODING_FOR:
						ok = encode_for(raw, rawLen, w, n, &buf, &len);
						break;
					case COLUMNAR_ENCODING_DELTA:
						ok = encode_delta(raw, rawLen, w, n, &buf, &len);
						break;
					case COLUMNAR_ENCODING_DOD:
						ok = encode_dod(raw, rawLen, w, n, &buf, &len);
						break;
					case COLUMNAR_ENCODING_GORILLA:
						ok = encode_gorilla(raw, rawLen, w, n, &buf, &len);
						break;
					case COLUMNAR_ENCODING_ALP:
						ok = encode_alp(raw, rawLen, att, n, &buf, &len);
						break;
					case COLUMNAR_ENCODING_DICT:
						ok = encode_dict(raw, rawLen, att, n, &buf, &len);
						break;
					default:
						continue;
				}
				if (ok && len < bestLen)
				{
					if (bestBuf)
						pfree(bestBuf);
					bestCode = cand[c];
					bestBuf = buf;
					bestLen = len;
				}
				else if (ok)
					pfree(buf);
			}

			*out = bestBuf ? bestBuf : (char *) raw;
			*outLen = bestLen;
			return bestCode;
		}
	}

	/* fixed-width encodings (rle/for/delta/dod/gorilla); dict handled below */
	if (w > 0)
	{
		if (encode_rle(raw, rawLen, w, n, &buf, &len) && len < bestLen)
		{
			bestCode = COLUMNAR_ENCODING_RLE;
			bestBuf = buf;
			bestLen = len;
		}

		if (is_packable_int(att))
		{
			if (encode_for(raw, rawLen, w, n, &buf, &len) && len < bestLen)
			{
				if (bestBuf)
					pfree(bestBuf);
				bestCode = COLUMNAR_ENCODING_FOR;
				bestBuf = buf;
				bestLen = len;
			}
			if (encode_delta(raw, rawLen, w, n, &buf, &len) && len < bestLen)
			{
				if (bestBuf)
					pfree(bestBuf);
				bestCode = COLUMNAR_ENCODING_DELTA;
				bestBuf = buf;
				bestLen = len;
			}
			if (encode_dod(raw, rawLen, w, n, &buf, &len) && len < bestLen)
			{
				if (bestBuf)
					pfree(bestBuf);
				bestCode = COLUMNAR_ENCODING_DOD;
				bestBuf = buf;
				bestLen = len;
			}
		}

		if (is_gorilla_float(att))
		{
			if (encode_gorilla(raw, rawLen, w, n, &buf, &len) && len < bestLen)
			{
				if (bestBuf)
					pfree(bestBuf);
				bestCode = COLUMNAR_ENCODING_GORILLA;
				bestBuf = buf;
				bestLen = len;
			}
			/* ALP (decimal) is the type-specific float scheme; gorilla stays a
			 * candidate and the smaller of the two wins per vector (spec 5.3) */
			if (encode_alp(raw, rawLen, att, n, &buf, &len) && len < bestLen)
			{
				if (bestBuf)
					pfree(bestBuf);
				bestCode = COLUMNAR_ENCODING_ALP;
				bestBuf = buf;
				bestLen = len;
			}
		}
	}

	/*
	 * Dictionary (the cardinality axis of selection) applies to any fixed-width
	 * or varlena column, and is the only lightweight encoding available for
	 * varlena (text/bytea/jsonb) low-cardinality columns. It bails cheaply when
	 * the distinct count is too high to help.
	 */
	if (w > 0 || w == -1)
	{
		if (encode_dict(raw, rawLen, att, n, &buf, &len) && len < bestLen)
		{
			if (bestBuf)
				pfree(bestBuf);
			bestCode = COLUMNAR_ENCODING_DICT;
			bestBuf = buf;
			bestLen = len;
		}
	}

	/*
	 * FSST (the substring axis) applies to varlena columns and captures shared
	 * byte patterns that dictionary cannot -- high-cardinality text/bytea where
	 * values differ but share frequent substrings. It competes with dict per
	 * vector and the smaller result wins (I5). The symbol table is built once per
	 * chunk by the caller (E3b) and passed as fsstTable; here each vector only
	 * encodes against it, so the dominant cost is not repeated per vector. It is
	 * still skipped when a cheaper encoding already compressed the vector well.
	 */
	if (w == -1 && fsstTable != NULL && bestLen > rawLen - rawLen / 4)
	{
		if (encode_fsst_shared(raw, rawLen, fsstTable, fsstTableLen, &buf, &len) &&
			len < bestLen)
		{
			if (bestBuf)
				pfree(bestBuf);
			bestCode = COLUMNAR_ENCODING_FSST;
			bestBuf = buf;
			bestLen = len;
		}
	}

	if (bestCode == COLUMNAR_ENCODING_NONE)
	{
		*out = (char *) raw;
		*outLen = rawLen;
		return COLUMNAR_ENCODING_NONE;
	}

	*out = bestBuf;
	*outLen = bestLen;
	return bestCode;
}

/*
 * PgColumnarDecodeChunk
 *		Reverse an encoding, reconstructing the byte-identical raw value stream
 *		in cx. For NONE, returns the input pointer unchanged (no copy).
 */
char *
PgColumnarDecodeChunk(const char *enc, uint32 encLen, int encodingType,
					Form_pg_attribute att, uint64 valueCount, uint32 rawLen,
					const char *fsstTable, uint32 fsstTableLen,
					MemoryContext cx)
{
	uint32		n = (uint32) valueCount;

	if (valueCount > PG_UINT32_MAX)
		DECODE_CORRUPT("value count too large");

	/*
	 * Cross-check the catalog-supplied lengths against the encoder's invariants
	 * before dispatching, so a corrupt chunk cannot drive a decoder past its
	 * buffers. The fixed-width encodings always produce exactly n * attlen raw
	 * bytes; NONE stores the raw bytes verbatim. DICT validates internally
	 * (its raw length is the sum of variable-length dictionary entries).
	 *
	 * RLE copies whole values and works at any fixed width, and the encoder
	 * chooses it for every fixed-width column: uuid (16), interval (16),
	 * timetz (12), name (64) and the like as well as 1, 2, 4 and 8. Requiring
	 * 1, 2, 4 or 8 here made every such chunk unreadable after it was written
	 * (#2061). FOR, DELTA, GORILLA and DOD load each value as an integer of at
	 * most 8 bytes, so they keep that restriction.
	 */
	switch (encodingType)
	{
		case COLUMNAR_ENCODING_RLE:
			if (att->attlen <= 0)
				DECODE_CORRUPT("fixed-width encoding on a non-fixed-width column");
			if ((uint64) n * (uint32) att->attlen != rawLen)
				DECODE_CORRUPT("raw length does not match value count");
			break;
		case COLUMNAR_ENCODING_FOR:
		case COLUMNAR_ENCODING_DELTA:
		case COLUMNAR_ENCODING_GORILLA:
		case COLUMNAR_ENCODING_DOD:
			if (att->attlen != 1 && att->attlen != 2 &&
				att->attlen != 4 && att->attlen != 8)
				DECODE_CORRUPT("fixed-width encoding on a non-fixed-width column");
			if ((uint64) n * (uint32) att->attlen != rawLen)
				DECODE_CORRUPT("raw length does not match value count");
			break;
		case COLUMNAR_ENCODING_ALP:
			if (att->attlen != 4 && att->attlen != 8)
				DECODE_CORRUPT("ALP on a non-float column");
			if ((uint64) n * (uint32) att->attlen != rawLen)
				DECODE_CORRUPT("raw length does not match value count");
			break;
		case COLUMNAR_ENCODING_NONE:
			if (encLen != rawLen)
				DECODE_CORRUPT("uncompressed length mismatch");
			break;
		default:
			break;
	}

	switch (encodingType)
	{
		case COLUMNAR_ENCODING_NONE:
			return (char *) enc;
		case COLUMNAR_ENCODING_RLE:
			return decode_rle(enc, encLen, att->attlen, n, rawLen, cx);
		case COLUMNAR_ENCODING_FOR:
			return decode_for(enc, encLen, n, rawLen, cx);
		case COLUMNAR_ENCODING_DELTA:
			return decode_delta(enc, encLen, n, rawLen, cx);
		case COLUMNAR_ENCODING_GORILLA:
			return decode_gorilla(enc, encLen, att->attlen, n, rawLen, cx);
		case COLUMNAR_ENCODING_DOD:
			return decode_dod(enc, encLen, n, rawLen, cx);
		case COLUMNAR_ENCODING_ALP:
			return decode_alp(enc, encLen, att->attlen, n, rawLen, cx);
		case COLUMNAR_ENCODING_DICT:
			return decode_dict(enc, encLen, att, n, rawLen, cx);
		case COLUMNAR_ENCODING_FSST:
			if (fsstTable == NULL || fsstTableLen == 0)
				DECODE_CORRUPT("FSST vector without a chunk-shared table");
			return decode_fsst_shared(enc, encLen, fsstTable, fsstTableLen,
									  rawLen, cx);
		default:
			ereport(ERROR,
					(errcode(ERRCODE_DATA_CORRUPTED),
					 errmsg("columnar: unknown value encoding type %d",
							encodingType)));
			return NULL;			/* keep the compiler happy */
	}
}

/* -------------------------------------------------------------------------
 * Test-only self-test of the encoding primitives (#155)
 *
 * The #155 rewrites replaced per-bit loops with word-at-a-time equivalents on
 * the promise that they emit exactly the bytes the old code emitted. A SQL suite
 * cannot check that promise directly: it can only observe what survives a round
 * trip, and it cannot reach a primitive the encoder declines to select. Two edges
 * turned out to be unreachable that way, both found by removal proofs that failed
 * to fail: the (width == 64) mask in bitpack, because packing at full width never
 * wins so the encoder never chooses it, and the zero guard in ctz_in, because
 * Gorilla encodes an unchanged value with a same-as-previous bit and never XORs
 * to zero.
 *
 * So the promise is checked here instead, against a reference implementation of
 * the algorithm each rewrite replaced. The references below are the pre-rewrite
 * code, kept deliberately naive: they are the oracle, so they are written to be
 * obviously correct rather than fast. A difference in a single byte is a
 * difference in stored data.
 *
 * Reached only through the binding the encoding suite creates; not in the shipped
 * catalog, like the other debug hooks.
 * ------------------------------------------------------------------------- */

/*
 * Reference bit unpacker: the pre-#501 per-bit loop, verbatim.
 *
 * The oracle for the word-at-a-time reader that replaced it. Kept naive on
 * purpose -- it walks one bit at a time and that is the whole point, because the
 * claim the rewrite has to make is that it is byte-identical, and an oracle that
 * shares the fast path's cleverness cannot check that.
 */
static void
ref_bitunpack(const unsigned char *in, uint32 n, int width, uint64 *out)
{
	uint64		bitpos = 0;
	uint32		i;

	if (width == 0)
	{
		for (i = 0; i < n; i++)
			out[i] = 0;
		return;
	}

	for (i = 0; i < n; i++)
	{
		uint64		v = 0;
		int			b;

		for (b = 0; b < width; b++)
		{
			if ((in[(bitpos + b) >> 3] >> ((bitpos + b) & 7)) & 1)
				v |= (uint64) 1 << b;
		}
		out[i] = v;
		bitpos += width;
	}
}

/* Reference bit packer: the pre-#285 per-bit loop. */
static void
ref_bitpack(const uint64 *vals, uint32 n, int width, StringInfo out)
{
	uint64		totalbits;
	uint32		nbytes;
	unsigned char *buf;
	uint64		bitpos = 0;
	uint32		i;

	if (width == 0 || n == 0)
		return;

	totalbits = (uint64) n * (uint64) width;
	nbytes = (uint32) ((totalbits + 7) / 8);
	buf = palloc0(nbytes);

	for (i = 0; i < n; i++)
	{
		uint64		v = vals[i];
		int			b;

		for (b = 0; b < width; b++)
		{
			if ((v >> b) & 1)
				buf[(bitpos + b) >> 3] |= (unsigned char) (1u << ((bitpos + b) & 7));
		}
		bitpos += width;
	}
	appendBinaryStringInfo(out, (char *) buf, nbytes);
	pfree(buf);
}

/* Reference bit writer: the pre-#286 per-bit loop. */
static void
ref_bw_put(BitWriter *bw, uint64 v, int bits)
{
	int			i;

	for (i = 0; i < bits; i++)
	{
		if ((v >> i) & 1)
			bw->cur |= (uint8) (1u << bw->nbits);
		bw->nbits++;
		if (bw->nbits == 8)
		{
			appendStringInfoChar(&bw->buf, (char) bw->cur);
			bw->cur = 0;
			bw->nbits = 0;
		}
	}
}

/* Reference leading-zero count within a window: the pre-#286 loop. */
static int
ref_clz_in(uint64 x, int total)
{
	int			n = 0;

	while (n < total && ((x >> (total - 1 - n)) & 1) == 0)
		n++;
	return n;
}

/*
 * Reference trailing-zero count. The pre-#286 loop had no zero guard and ran past
 * the end of the value for x == 0, so this stops at 64 rather than reproducing
 * that. The zero case is asserted separately against the documented answer.
 */
static int
ref_ctz_in(uint64 x)
{
	int			n = 0;

	while (n < 64 && ((x >> n) & 1) == 0)
		n++;
	return n;
}

/* Deterministic values (splitmix64), so a failure is reproducible. */
static inline uint64
selftest_rand(uint64 *state)
{
	uint64		z = (*state += UINT64CONST(0x9E3779B97F4A7C15));

	z = (z ^ (z >> 30)) * UINT64CONST(0xBF58476D1CE4E5B9);
	z = (z ^ (z >> 27)) * UINT64CONST(0x94D049BB133111EB);
	return z ^ (z >> 31);
}

PG_FUNCTION_INFO_V1(pgcolumnar_debug_encoding_selftest);

Datum
pgcolumnar_debug_encoding_selftest(PG_FUNCTION_ARGS)
{
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	TupleDesc	retdesc;
	Tuplestorestate *tupstore;
	MemoryContext oldContext;
	int			width;
	int			i;
	int			cases = 0;

	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo))
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("set-valued function called in context that cannot accept a set")));

	retdesc = CreateTemplateTupleDesc(1);
	TupleDescInitEntry(retdesc, 1, "result", TEXTOID, -1, 0);

	oldContext = MemoryContextSwitchTo(rsinfo->econtext->ecxt_per_query_memory);
	tupstore = tuplestore_begin_heap(true, false, work_mem);
	rsinfo->returnMode = SFRM_Materialize;
	rsinfo->setResult = tupstore;
	rsinfo->setDesc = retdesc;
	MemoryContextSwitchTo(oldContext);

#define SELFTEST_FAIL(fmt, ...) \
	do { \
		Datum		d_[1]; \
		bool		n_[1] = {false}; \
		char		m_[256]; \
		snprintf(m_, sizeof(m_), "FAIL " fmt, __VA_ARGS__); \
		d_[0] = CStringGetTextDatum(m_); \
		tuplestore_putvalues(tupstore, retdesc, d_, n_); \
	} while (0)

	/*
	 * 1. bitpack against the per-bit reference, every width from 1 to 64.
	 *
	 * Widths above roughly 32, the nine-byte field span, and the (width == 64)
	 * mask are unreachable from SQL because the encoder never selects packing
	 * there; this is the only place they run. Several value counts are used so
	 * the packing starts at every sub-byte offset.
	 */
	for (width = 1; width <= 64; width++)
	{
		static const uint32 counts[] = {1, 2, 3, 7, 8, 9, 17, 64, 129};
		uint32		ns[lengthof(counts) + 1];
		uint64		state = UINT64CONST(0x155) + (uint64) width;
		uint32		ci;

		/*
		 * One derived count per width, in addition to the fixed list (#514
		 * review). Coverage of the unpacker's two paths is a property of these
		 * counts and not of the loop: nFast is zero until the encoded body
		 * reaches nine bytes, which needs n * width >= 65, so a shorter list
		 * would exercise low widths through the TAIL only -- and the tail is the
		 * per-bit assembly, which is what ref_bitunpack does. The oracle would
		 * then be comparing the reference against itself and passing.
		 *
		 * Derived rather than listed so that trimming counts[] above cannot
		 * silently narrow this, and checked below so it cannot rot either.
		 */
		ns[lengthof(counts)] = (65 + (uint32) width - 1) / (uint32) width + 4;
		for (ci = 0; ci < lengthof(counts); ci++)
			ns[ci] = counts[ci];

		{
			uint32		dn = ns[lengthof(counts)];
			uint32		dbytes = (uint32) (((uint64) dn * (uint32) width + 7) / 8);

			if (dbytes < 9)
				SELFTEST_FAIL("width=%d: derived n=%u yields %u bytes, "
							  "which never reaches the wide load",
							  width, dn, dbytes);
		}

		for (ci = 0; ci < lengthof(ns); ci++)
		{
			uint32		n = ns[ci];
			uint64	   *vals = palloc(sizeof(uint64) * n);
			StringInfoData a;
			StringInfoData b;
			uint64		mask = (width == 64)
				? ~UINT64CONST(0) : ((UINT64CONST(1) << width) - 1);
			uint32		k;

			for (k = 0; k < n; k++)
				vals[k] = selftest_rand(&state) & mask;
			vals[0] = 0;			/* extremes as well as random */
			if (n > 1)
				vals[1] = mask;

			initStringInfo(&a);
			initStringInfo(&b);
			bitpack(vals, n, width, &a);
			ref_bitpack(vals, n, width, &b);
			cases++;

			/*
			 * And the reader (#501). The word-at-a-time unpacker must agree with
			 * the per-bit reference value for value, at every width and at every
			 * sub-byte start offset the counts above produce. This is the only
			 * place widths above ~32, the nine-byte field span and the width==64
			 * mask are reached at all.
			 */
			{
				uint64	   *got = palloc(sizeof(uint64) * n);
				uint64	   *want = palloc(sizeof(uint64) * n);
				uint32		j;

				bitunpack((const unsigned char *) a.data, (uint32) a.len,
						  n, width, got);
				ref_bitunpack((const unsigned char *) a.data, n, width, want);
				cases++;

				for (j = 0; j < n; j++)
				{
					if (got[j] != want[j])
						SELFTEST_FAIL(
							"bitunpack width=%d n=%u value %u: " UINT64_FORMAT
							" vs reference " UINT64_FORMAT,
							width, n, j, got[j], want[j]);
				}
				pfree(got);
				pfree(want);
			}

			if (a.len != b.len)
				SELFTEST_FAIL("bitpack width=%d n=%u: length %d vs reference %d",
							  width, n, a.len, b.len);
			else if (memcmp(a.data, b.data, a.len) != 0)
				SELFTEST_FAIL("bitpack width=%d n=%u: bytes differ from reference",
							  width, n);

			pfree(a.data);
			pfree(b.data);
			pfree(vals);
		}
	}

	/*
	 * 2. bw_put against the per-bit reference. The batched writer carries up to
	 * seven bits between calls and takes at most 57 at a time, so the sequence of
	 * widths matters more than any single one.
	 */
	{
		uint64		state = UINT64CONST(0xB17);
		int			round;

		for (round = 0; round < 8; round++)
		{
			BitWriter	bwa;
			BitWriter	bwb;
			int			bits;

			bw_init(&bwa);
			bw_init(&bwb);
			for (bits = 1; bits <= 64; bits++)
			{
				uint64		v = selftest_rand(&state);
				int			use = ((bits + round) % 64) + 1;

				bw_put(&bwa, v, use);
				ref_bw_put(&bwb, v, use);
			}
			cases++;
			if (bwa.buf.len != bwb.buf.len)
				SELFTEST_FAIL("bw_put round=%d: length %d vs reference %d",
							  round, bwa.buf.len, bwb.buf.len);
			else if (memcmp(bwa.buf.data, bwb.buf.data, bwa.buf.len) != 0)
				SELFTEST_FAIL("bw_put round=%d: bytes differ from reference", round);
			else if (bwa.nbits != bwb.nbits || bwa.cur != bwb.cur)
				SELFTEST_FAIL("bw_put round=%d: carry %d/%u vs reference %d/%u",
							  round, bwa.nbits, (unsigned) bwa.cur,
							  bwb.nbits, (unsigned) bwb.cur);
		}
	}

	/* 3. clz_in and ctz_in against the reference loops, over the edges a
	 * hardware bit scan gets wrong if the zero case or the window is off. */
	{
		static const int totals[] = {8, 16, 32, 64};
		uint64		state = UINT64CONST(0xC12);
		uint32		ti;

		for (ti = 0; ti < lengthof(totals); ti++)
		{
			int			total = totals[ti];
			uint64		twin = (total == 64)
				? ~UINT64CONST(0) : ((UINT64CONST(1) << total) - 1);

			for (i = 0; i < 40; i++)
			{
				uint64		x;
				int			got;
				int			want;

				switch (i)
				{
					case 0: x = 0; break;
					case 1: x = 1; break;
					case 2: x = twin; break;
					case 3: x = UINT64CONST(1) << (total - 1); break;
					default: x = selftest_rand(&state) & twin; break;
				}

				got = clz_in(x, total);
				want = ref_clz_in(x, total);
				cases++;
				if (got != want)
					SELFTEST_FAIL("clz_in(0x%llx, %d) = %d, reference %d",
								  (unsigned long long) x, total, got, want);

				got = ctz_in(x);
				want = (x == 0) ? 0 : ref_ctz_in(x);
				cases++;
				if (got != want)
					SELFTEST_FAIL("ctz_in(0x%llx) = %d, reference %d",
								  (unsigned long long) x, got, want);
			}
		}
	}

	/* How many comparisons ran, so a suite can refuse a pass that came from
	 * doing nothing, or from the backend dying part way through. */
	{
		Datum		d[1];
		bool		nulls[1] = {false};
		char		msg[64];

		snprintf(msg, sizeof(msg), "cases=%d", cases);
		d[0] = CStringGetTextDatum(msg);
		tuplestore_putvalues(tupstore, retdesc, d, nulls);
	}

#undef SELFTEST_FAIL

	return (Datum) 0;
}

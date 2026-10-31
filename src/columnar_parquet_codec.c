/*-------------------------------------------------------------------------
 *
 * pgcolumnar_parquet_codec.c
 *		Parquet data-page decompression. See pgcolumnar_parquet_codec.h.
 *
 * Written fresh for pgColumnar from the public Apache Parquet specification and
 * the public Snappy format description.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "lib/stringinfo.h"
#include "utils/memutils.h"

#include "columnar_parquet_codec.h"
#include "columnar_parquet_format.h"

#ifdef HAVE_LIBZ
#include <zlib.h>
#endif
#ifdef HAVE_LIBZSTD
#include <zstd.h>
#endif
#ifdef HAVE_LIBLZ4
#include <lz4.h>
#endif

/* -------------------------------------------------------------------------
 * Snappy raw decompression (format spec: preamble varint uncompressed length,
 * then a stream of literal and copy elements). Clean-room from the format.
 * ------------------------------------------------------------------------- */
static bool
snappy_raw_uncompress(const uint8 *in, size_t inlen, StringInfo out)
{
	size_t		pos = 0;
	uint32		ulen = 0;
	int			shift = 0;

	/* preamble: uncompressed length as a varint */
	while (pos < inlen)
	{
		uint8		b = in[pos++];

		ulen |= (uint32) (b & 0x7f) << shift;
		if ((b & 0x80) == 0)
			break;
		shift += 7;
		if (shift > 32)
			return false;
	}

	enlargeStringInfo(out, ulen);
	while (pos < inlen)
	{
		uint8		tag = in[pos++];
		int			type = tag & 0x03;

		if (type == 0)			/* literal */
		{
			uint32		len = (tag >> 2) + 1;

			if (len > 60)
			{
				int			nb = (tag >> 2) - 59;	/* 1..4 bytes of length */
				uint32		l = 0;
				int			i;

				if (pos + nb > inlen)
					return false;
				for (i = 0; i < nb; i++)
					l |= (uint32) in[pos++] << (8 * i);
				len = l + 1;
			}
			if (pos + len > inlen)
				return false;
			appendBinaryStringInfo(out, (const char *) in + pos, len);
			pos += len;
		}
		else					/* copy */
		{
			uint32		len;
			uint32		offset;
			int			i;

			if (type == 1)
			{
				len = ((tag >> 2) & 0x07) + 4;
				if (pos >= inlen)
					return false;
				offset = ((uint32) (tag >> 5) << 8) | in[pos++];
			}
			else if (type == 2)
			{
				len = (tag >> 2) + 1;
				if (pos + 2 > inlen)
					return false;
				offset = (uint32) in[pos] | ((uint32) in[pos + 1] << 8);
				pos += 2;
			}
			else				/* type == 3 */
			{
				len = (tag >> 2) + 1;
				if (pos + 4 > inlen)
					return false;
				offset = (uint32) in[pos] | ((uint32) in[pos + 1] << 8) |
					((uint32) in[pos + 2] << 16) | ((uint32) in[pos + 3] << 24);
				pos += 4;
			}
			if (offset == 0 || offset > (uint32) out->len)
				return false;
			/* copies may overlap, so copy byte by byte from the output so far */
			for (i = 0; i < (int) len; i++)
			{
				char		c = out->data[out->len - offset];

				appendBinaryStringInfo(out, &c, 1);
			}
		}
	}
	return (uint32) out->len == ulen;
}

#if defined(HAVE_LIBZ) || defined(HAVE_LIBZSTD) || defined(HAVE_LIBLZ4)
/*
 * The empty result for a page whose declared uncompressed size is 0.
 */
static bool
empty_output(StringInfo scratch, const uint8 **out, size_t *outlen)
{
	resetStringInfo(scratch);
	*out = (const uint8 *) scratch->data;
	*outlen = 0;
	return true;
}
#endif

/*
 * Decompress one Parquet page body according to its column-chunk codec.
 *
 * On success *out / *outlen point at the decompressed bytes: either straight into
 * `src` (uncompressed), or into `scratch` (any real codec). `usize` is the
 * uncompressed size from the page header, which zstd, lz4_raw and gzip require up
 * front (only Snappy self-describes its output length, via a leading varint); pass
 * the value portion's uncompressed size for a v2 data page, where the levels are
 * stored uncompressed ahead of the compressed values.
 *
 * A codec whose library was not built in, or an unknown codec id, returns false
 * so the caller raises a clean decode error rather than reading garbage.
 *
 * srclen and usize are file-declared and otherwise unvalidated (parse_page_header
 * casts a zigzag int, so a crafted header can present them as negative -- which
 * arrive here as an enormous size_t -- or absurdly large). Both are bounded to
 * MaxAllocSize up front so a crafted page yields a clean "return false" rather
 * than a generic allocation error deep in enlargeStringInfo or a decompressor.
 * Every codec that consumes usize also caps its output at it, so none can be
 * driven to allocate or inflate more than the header declares.
 *
 * A declared size of 0 gives an empty output for GZIP, ZSTD and LZ4_RAW (#1786),
 * as it already did for UNCOMPRESSED and SNAPPY. pyarrow writes the dictionary
 * page of a column that is NULL in every row with no values, so its declared size
 * is 0; refusing that page made the whole column unreadable. An empty output
 * cannot inflate into a large allocation, so the size bound loses nothing.
 */
bool
PgColumnarParquetDecompress(int codec, const uint8 *src, size_t srclen, size_t usize,
			  StringInfo scratch, const uint8 **out, size_t *outlen)
{
	if (srclen > MaxAllocSize || usize > MaxAllocSize)
		return false;

	switch (codec)
	{
		case PQC_UNCOMPRESSED:
			*out = src;
			*outlen = srclen;
			return true;

		case PQC_SNAPPY:
			/* Snappy self-describes its output length (a leading varint), which
			 * snappy_raw_uncompress bounds; usize is not consulted. */
			if (!snappy_raw_uncompress(src, srclen, scratch))
				return false;
			*out = (const uint8 *) scratch->data;
			*outlen = scratch->len;
			return true;

#ifdef HAVE_LIBZ
		case PQC_GZIP:
			{
				z_stream	zs;
				int			rc;

				if (usize == 0)
					return empty_output(scratch, out, outlen);
				/* Bound the output at the declared size, like zstd and lz4, so a
				 * small crafted page cannot inflate into a huge allocation. */
				memset(&zs, 0, sizeof(zs));
				/* 15 + 32: accept a gzip or zlib header (Parquet writes gzip) */
				if (inflateInit2(&zs, 15 + 32) != Z_OK)
					return false;
				enlargeStringInfo(scratch, (int) usize);
				zs.next_in = (Bytef *) src;
				zs.avail_in = (uInt) srclen;
				zs.next_out = (Bytef *) scratch->data;
				zs.avail_out = (uInt) usize;
				rc = inflate(&zs, Z_FINISH);
				inflateEnd(&zs);
				if (rc != Z_STREAM_END || zs.total_out != usize)
					return false;
				scratch->len = (int) usize;
				scratch->data[scratch->len] = '\0';
				*out = (const uint8 *) scratch->data;
				*outlen = scratch->len;
				return true;
			}
#endif

#ifdef HAVE_LIBZSTD
		case PQC_ZSTD:
			{
				size_t		got;

				if (usize == 0)
					return empty_output(scratch, out, outlen);
				enlargeStringInfo(scratch, (int) usize);
				got = ZSTD_decompress(scratch->data, usize, src, srclen);
				if (ZSTD_isError(got) || got != usize)
					return false;
				scratch->len = (int) got;
				*out = (const uint8 *) scratch->data;
				*outlen = scratch->len;
				return true;
			}
#endif

#ifdef HAVE_LIBLZ4
		case PQC_LZ4_RAW:
			{
				int			got;

				if (usize == 0)
					return empty_output(scratch, out, outlen);
				enlargeStringInfo(scratch, (int) usize);
				got = LZ4_decompress_safe((const char *) src, scratch->data,
										  (int) srclen, (int) usize);
				if (got < 0 || (size_t) got != usize)
					return false;
				scratch->len = got;
				*out = (const uint8 *) scratch->data;
				*outlen = scratch->len;
				return true;
			}
#endif

		default:
			return false;		/* unknown codec, or its library not built in */
	}
}

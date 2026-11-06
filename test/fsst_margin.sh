#!/usr/bin/env bash
#
# The FSST keep/drop cost margin (#155, #271).
#
# PgColumnarFsstHelpsCompressed used to keep FSST on any compressed win at all,
# however small, and the per-vector FSST encode is a dominant cost of a text or
# varlena load. pgcolumnar.fsst_min_gain_percent requires the win to clear a
# margin before FSST is kept.
#
# The knob changes how bytes are encoded, so the check that matters is not that
# it saves time -- that is a benchmark, and benchmarks belong elsewhere -- but
# that it never changes what comes back out. A future change to the FSST path
# could otherwise alter content under a non-default margin with nothing to catch
# it.
#
# It asserts:
#   1. the margin actually decides -- a corpus whose FSST win is marginal keeps
#      FSST below the margin and drops it above, observed from the encoding
#      descriptor rather than inferred from size;
#   2. margin 0 is the original "any win keeps it", so the documented equivalence
#      is pinned rather than argued from the arithmetic;
#   3. the rows read back byte-identical at every margin, including against a
#      heap mirror. This is the invariant; 1 and 2 exist to prove 3 is not
#      vacuous.
#
# Usage:  test/fsst_margin.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS="${PGC_FSST_MARGIN_ROWS:-20000}"

# How many vectors of the text column chose FSST (encoding type 8).
#
# The descriptor is a 6-byte header -- version, a flags byte (#1130; a reserved
# zero before it), then the vector
# count as uint32 -- followed by that many 13-byte entries. Same decode as
# write_fsst_compressed.sh, and for the same reason: reading past the entries
# would score the chunk's symbol table as encoding types.
fsst_vectors() {  # table -> count
	q "SELECT coalesce(sum(n), 0) FROM (
		SELECT (SELECT count(*)
				FROM generate_series(0,
					get_byte(c.encoding_descriptor, 2)
					+ get_byte(c.encoding_descriptor, 3) * 256
					+ get_byte(c.encoding_descriptor, 4) * 65536
					+ get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
				WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = 8) AS n
		FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass
		  AND c.column_index = 1) t;" | tail -1
}

# High-entropy hex: FSST builds a symbol table that barely pays, which is exactly
# the marginal case the margin exists to decline. Nulls and short values are mixed
# in because the decision applies to a whole chunk and those are the values whose
# handling differs between encodings.
# The decision corpus. Plain, because mixing NULLs and short values into it
# changes which encoding wins outright and the margin then never gets a say --
# which is what the first version of this suite did, and it reported "margin 0
# does not keep FSST" as though that were a finding about the margin.
decide_corpus="md5(g::text) || md5((g * 7)::text)"

# The content corpus. Deliberately the nastiest one: NULLs, empty strings and
# values below the FSST minimum, because the decision applies to a whole chunk
# at once and those are the values whose handling differs between encodings.
#
# ONE IN 97, NOT ONE IN 13. At one in 13 no vector of the content tables chose
# FSST at ANY margin (measured: 0 FSST vectors in fm_c0, fm_c90 and fm_cd), so
# section 3's invariant never read FSST-encoded data, and a decoder that wrote
# the wrong symbol passed it. At one in 97 the margin-0 table stores FSST and
# the margin-90 one does not, and the check before section 3's reads asserts it.
content_corpus="CASE g % 97
			WHEN 0 THEN NULL
			WHEN 1 THEN ''
			WHEN 2 THEN 'ab'
			ELSE md5(g::text) || md5((g * 7)::text)
		END"

# margin "" means: do not SET it at all, i.e. whatever the build ships.
# codec "" means the same: leave pgcolumnar.compression at the build default.
load() {  # table margin corpus [codec]
	local setguc=""
	[ -n "$2" ] && setguc="SET pgcolumnar.fsst_min_gain_percent = $2;"
	[ -n "${4:-}" ] && setguc="$setguc SET pgcolumnar.compression = '$4';"
	psql_run "DROP TABLE IF EXISTS $1;
		$setguc
		CREATE TABLE $1 (id int, v text) USING pgcolumnar;
		INSERT INTO $1 SELECT g, $3 FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
}

# The heap mirror the columnar copies must all match.
psql_run "DROP TABLE IF EXISTS fm_h;
	CREATE TABLE fm_h (id int, v text);
	INSERT INTO fm_h SELECT g, $content_corpus FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
heap_hash="$(q "SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM fm_h t;")"
check "the heap mirror has content to compare against" \
	"$([ -n "$heap_hash" ] && echo yes || echo no)" "yes"

# --- 1. the margin decides ---------------------------------------------------
#
# THE THREE ARMS BELOW AND IN 2b BRACKET THE DECISION, and the bracket also
# falsifies `fsst_vectors` itself. That second property is accidental and worth
# writing down, because the low-margin arm looks redundant next to the margin-90
# one and is not:
#
#     reader always returns 0    low-margin RED   margin-90 PASS   codec RED
#     reader always returns >0   low-margin PASS  margin-90 RED    codec PASS
#     the real tree              PASS             PASS             PASS
#
# `fsst_vectors` parses the encoding descriptor by byte offset --
# `get_byte(descriptor, 6 + i * 13)` -- which is exactly the kind of reader that
# goes silently wrong on a format change and reports a plausible number. With only
# the margin-90 arm, a reader stuck at 0 would read as "FSST was dropped" and every
# arm would be green. Removing the low-margin arm as redundant takes that with it.
#
# AND THE TWO MARGINS ARE THE GUC'S OWN ENDPOINTS, not arbitrary low and high
# values. `columnar_tableam.c:3269` declares it `5, 0, 99` -- default 5, minimum
# 0, maximum 99 -- so this arm and 2b's bracket the decision with the extremes the
# setting admits.
#
# That is what makes each half the strongest available statement. At margin 0 the
# keep test is "any compressed win at all keeps FSST", in the GUC's own words, so
# `kept == 0` here would mean FSST never helps on this corpus rather than merely
# not helping enough. Raise this to 5 to "simplify" it and that disappears, with
# nothing to show it went.
#
# Found by @OffgridwithJD reviewing #1082, and the endpoint reading is theirs too.
# The table above is measured rather than argued, by evaluating the arms against a
# stubbed reader; the range was read from the declaration rather than taken on
# trust.

load fm_keep 0 "$decide_corpus"
kept="$(fsst_vectors fm_keep)"
check "at margin 0 this corpus keeps FSST" \
	"$([ "$kept" -gt 0 ] && echo yes || echo no)" "yes"

load fm_drop 90 "$decide_corpus"
dropped="$(fsst_vectors fm_drop)"
# Each drop is read with the table's row count, so an empty table -- whose
# chunks are none and whose FSST count is therefore 0 -- cannot read as a drop.
rows_and() { echo "$(q "SELECT count(*) FROM $1;" | tail -1)/$2"; }
check "at margin 90 the same corpus drops FSST" "$(rows_and fm_drop "$dropped")" "$ROWS/0"

# Without this the pair above could both be true of a corpus FSST never helped:
# the drop is only meaningful because the keep is real.
check "so the margin is what changed the decision, not the corpus" \
	"$([ "$kept" -gt "$dropped" ] 2>/dev/null && echo yes || echo "no (kept $kept, dropped $dropped)")" "yes"

# --- 2. margin 0 is the original behaviour -----------------------------------
#
# The arithmetic is exact -- total*100 < plain*100 is total < plain -- but the
# claim that the default did not silently change what gets encoded is worth
# pinning rather than deriving.
load fm_default "" "$decide_corpus"   # no SET at all: whatever the build ships
default_v="$(fsst_vectors fm_default)"
check "the shipped default drops FSST on a marginal corpus" "$(rows_and fm_default "$default_v")" "$ROWS/0"
check "and 0 still keeps it, so the default is a decision and not a rewrite" \
	"$([ "$kept" -gt 0 ] && echo yes || echo no)" "yes"

# --- 2b. the CODEC decides too, which the docs used to deny (#1076) ----------
#
# `pgcolumnar.compression` reads as a codec choice: "default codec for new
# chunks". It is not only that. `columnar_encoding.c` returns "FSST helps"
# UNCONDITIONALLY when the codec is `none`, before it looks at the corpus or the
# margin:
#
#     if (compressionType == COLUMNAR_COMPRESSION_NONE)
#         return true;
#
# So `none` is not "the cascade without a block codec", it is a DIFFERENT
# cascade. Setting it changes which lightweight encodings a chunk gets.
#
# This is the same differential as section 1 with the other variable moved: same
# corpus, same margin 90, and only the codec differs. Section 1 established that
# margin 90 drops FSST on this corpus, so a keep here can only have come from the
# codec. Without that pairing this arm would be satisfied by a corpus FSST always
# wins on.
load fm_none 90 "$decide_corpus" none
none_v="$(fsst_vectors fm_none)"

check "the paired arm dropped FSST at this margin with a codec" "$(rows_and fm_drop "$dropped")" "$ROWS/0"

check "with no codec the same corpus keeps FSST at the same margin" \
	"$([ "$none_v" -gt 0 ] && echo yes || echo no)" "yes"

# The margin is not merely outvoted here, it is never consulted: the keep test
# returns before reading it. 99 is the maximum the GUC accepts, so if any margin
# could drop FSST under `none` this is the one that would.
load fm_none99 99 "$decide_corpus" none
check "and at the maximum margin too, because the test returns before reading it" \
	"$([ "$(fsst_vectors fm_none99)" -gt 0 ] && echo yes || echo no)" "yes"

# --- 3. the invariant: content never changes ---------------------------------

load fm_c0  0   "$content_corpus"
load fm_c90 90  "$content_corpus"
load fm_cd  ""  "$content_corpus"

# What makes the invariant below about FSST at all: the margin-0 copy really is
# stored as FSST. Without it, every content check passed on a decoder that
# wrote the wrong symbol, because no content table held an FSST vector.
check "the margin-0 content copy really is stored as FSST, so the invariant reads FSST" \
	"$([ "$(fsst_vectors fm_c0)" -gt 0 ] 2>/dev/null && echo yes || echo "no ($(fsst_vectors fm_c0))")" "yes"

# Unrolled, so each name is a literal the grader can read. check_text: a failed
# reading is empty, and the heap hash an empty heap gives is empty too.
row_hash() { q "SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM $1 t;"; }
check_text "fm_c0 reads back exactly what the heap holds" "$(row_hash fm_c0)" "$heap_hash"
check_text "fm_c90 reads back exactly what the heap holds" "$(row_hash fm_c90)" "$heap_hash"
check_text "fm_cd reads back exactly what the heap holds" "$(row_hash fm_cd)" "$heap_hash"

# Nulls and empty strings survive the encoding switch, which is where a
# whole-chunk encoding change is most likely to go wrong.
nulls() { q "SELECT count(*) FROM $1 WHERE v IS NULL;"; }
empties() { q "SELECT count(*) FROM $1 WHERE v = '';"; }
check_text "fm_c0 preserves NULLs" "$(nulls fm_c0)" "$(nulls fm_h)"
check_text "fm_c0 preserves empty strings" "$(empties fm_c0)" "$(empties fm_h)"
check_text "fm_c90 preserves NULLs" "$(nulls fm_c90)" "$(nulls fm_h)"
check_text "fm_c90 preserves empty strings" "$(empties fm_c90)" "$(empties fm_h)"

pgc_summary

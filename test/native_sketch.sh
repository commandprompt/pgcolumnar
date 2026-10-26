#!/usr/bin/env bash
#
# pgColumnar: per-row-group distinct-count sketches, the catalog and the writer
# (#1309 phase 1).
#
# A column named in set_options(sketch_columns => ...) gets one HyperLogLog sketch
# per row group in pgcolumnar.sketch: 2048 one-byte registers, filled from the
# type's extended hash (HASHEXTENDED_PROC, seed 0) of each non-NULL value. Phase 2
# adds the aggregate that reads them; this suite holds what the writer must get
# right before anything reads a sketch:
#
#   - only the named columns get sketches, and a table naming none pays nothing;
#   - the option refuses what cannot be sketched: an unknown column, a type with
#     no extended hash, a nondeterministic collation;
#   - NULLs are not hashed, so a group of one value sets one register and an
#     all-NULL group sets none;
#   - the registers of a fixed group are PINNED. They are stored on disk and
#     merged across row groups written by different builds, so a change to the
#     hash or the register rule must redden here rather than merge silently;
#   - a parallel flush writes the same sketch as a serial one;
#   - a projection's storage gets no sketch, and TRUNCATE, DROP and a rewrite
#     leave no sketch behind for a row group that no longer exists.
#
# Usage:  test/native_sketch.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

sqlstate() {	# sqlstate SQL -> a five-character SQLSTATE, noerror, or PROBE_UNREACHABLE
	local out st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=0 \
		-c "$1" -c "SELECT 'PGC_PROBE_OK';" 2>&1)"
	case "$out" in
		*PGC_PROBE_OK*) ;;
		*) echo PROBE_UNREACHABLE; return ;;
	esac
	st="$(printf '%s\n' "$out" | sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' | head -1)"
	if [ -n "$st" ]; then printf '%s\n' "$st"; else echo noerror; fi
}

# sketch rows of a table's base storage, optionally for one column (0-based index)
# THE BASE STORAGE ONLY: a projection's storage row carries the table's oid too, so
# a join on relation_oid would count the projection's row groups as the table's.
sketches() {	# table [column_index]
	q "SELECT count(*) FROM pgcolumnar.sketch k
		WHERE k.storage_id = pgcolumnar.get_storage_id('$1') ${2:+AND k.column_index = $2};"
}
groups() {	# table
	q "SELECT count(*) FROM pgcolumnar.row_group r
		WHERE r.storage_id = pgcolumnar.get_storage_id('$1');"
}
# non-zero registers of the sketch of one row group, column 0
nonzero() {	# table group_number
	q "SELECT count(*) FROM pgcolumnar.sketch k
		JOIN pgcolumnar.storage s ON s.storage_id = k.storage_id,
		     generate_series(0, length(k.registers) - 1) i
		WHERE s.relation_oid = '$1'::regclass AND k.column_index = 0
		  AND k.group_number = $2 AND get_byte(k.registers, i) <> 0;"
}

# --- 1. the option -----------------------------------------------------------

psql_run "CREATE TABLE sk (a bigint, b text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('sk', stripe_row_limit => 1000);" >/dev/null 2>&1
check "set_options accepts sketch_columns naming a column" \
	"$(sqlstate "SELECT pgcolumnar.set_options('sk', sketch_columns => ARRAY['a'])")" "noerror"
check "and the options row records it" \
	"$(q "SELECT sketch_columns FROM pgcolumnar.options WHERE regclass = 'sk'::regclass;")" "{a}"
check "an unknown column is refused" \
	"$(sqlstate "SELECT pgcolumnar.set_options('sk', sketch_columns => ARRAY['nosuch'])")" "42703"
psql_run "CREATE TABLE skp (p point) USING pgcolumnar;" >/dev/null 2>&1
check "a column whose type has no extended hash is refused" \
	"$(sqlstate "SELECT pgcolumnar.set_options('skp', sketch_columns => ARRAY['p'])")" "0A000"
psql_run "CREATE COLLATION IF NOT EXISTS sk_ci (provider = icu, locale = 'und-u-ks-level2', deterministic = false);" >/dev/null 2>&1
if [ "$(q "SELECT count(*) FROM pg_collation WHERE collname = 'sk_ci';")" = 1 ]; then
	psql_run "CREATE TABLE skc (t text COLLATE sk_ci) USING pgcolumnar;" >/dev/null 2>&1
	check "a column with a nondeterministic collation is refused" \
		"$(sqlstate "SELECT pgcolumnar.set_options('skc', sketch_columns => ARRAY['t'])")" "0A000"
else
	check_unrunnable "a column with a nondeterministic collation is refused" MISSING_DEPENDENCY \
		"this server has no ICU, so no nondeterministic collation can be created"
fi
check "the refusals left the recorded option alone" \
	"$(q "SELECT sketch_columns FROM pgcolumnar.options WHERE regclass = 'sk'::regclass;")" "{a}"

# --- 2. the writer -----------------------------------------------------------

psql_run "INSERT INTO sk SELECT g, 'v' || g FROM generate_series(1, 3000) g;" >/dev/null 2>&1
check "premise: the insert wrote three row groups" "$(groups sk)" "3"
check "each row group gets one sketch for the named column" "$(sketches sk 0)" "3"
check "a column not named gets no sketch" "$(sketches sk 1)" "0"
check "each sketch holds 2048 registers" \
	"$(q "SELECT string_agg(DISTINCT length(k.registers)::text, ',') FROM pgcolumnar.sketch k
		JOIN pgcolumnar.storage s ON s.storage_id = k.storage_id
		WHERE s.relation_oid = 'sk'::regclass;")" "2048"

psql_run "CREATE TABLE skn (a bigint) USING pgcolumnar;
	INSERT INTO skn SELECT g FROM generate_series(1, 3000) g;" >/dev/null 2>&1
check "a table that names no column has no sketch rows" "$(sketches skn) over $(groups skn) groups" "0 over 1 groups"

psql_run "CREATE TABLE sk1 (a bigint) USING pgcolumnar;
	SELECT pgcolumnar.set_options('sk1', stripe_row_limit => 1000, sketch_columns => ARRAY['a']);
	INSERT INTO sk1 SELECT 42 FROM generate_series(1, 1000);
	INSERT INTO sk1 SELECT NULL FROM generate_series(1, 1000);" >/dev/null 2>&1
check "one group of a single value and one of NULLs was written" \
	"$(q "SELECT string_agg(r.row_count::text, ',' ORDER BY r.group_number) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
		WHERE s.relation_oid = 'sk1'::regclass;")" "1000,1000"
check "a row group of one distinct value sets exactly one register" \
	"$(nonzero sk1 "$(q "SELECT min(r.group_number) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id WHERE s.relation_oid = 'sk1'::regclass;")")" "1"
check "an all-NULL row group sets no register" \
	"$(nonzero sk1 "$(q "SELECT max(r.group_number) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id WHERE s.relation_oid = 'sk1'::regclass;")")" "0"

# THE PIN. Values 1..1000 of a bigint column, one row group. The digest is of the
# register bytes, so the hash, the seed, the register index (the hash's low 11
# bits) and the rank rule are all in it.
psql_run "CREATE TABLE skpin (a bigint) USING pgcolumnar;
	SELECT pgcolumnar.set_options('skpin', sketch_columns => ARRAY['a']);
	INSERT INTO skpin SELECT g FROM generate_series(1, 1000) g;" >/dev/null 2>&1
check "the sketch of a fixed row group is pinned" \
	"$(q "SELECT md5(k.registers) FROM pgcolumnar.sketch k
		JOIN pgcolumnar.storage s ON s.storage_id = k.storage_id
		WHERE s.relation_oid = 'skpin'::regclass;")" "ce25d8b59b9f5a5752c42116b4ff9212"

# THE RULE, RECOMPUTED. The same registers built in SQL from core's
# hashint8extended: the low 11 bits pick the register, and its value is one more
# than the trailing zeros of the remaining 53 bits (54 when they are all zero).
# The pin above holds the bytes still; this holds them to the stated rule.
check "the registers follow the stated rule, recomputed in SQL" \
	"$(q "SELECT md5(k.registers) FROM pgcolumnar.sketch k
		WHERE k.storage_id = pgcolumnar.get_storage_id('skpin');")" \
	"$(q "WITH h AS (SELECT hashint8extended(g::bigint, 0) AS h FROM generate_series(1, 1000) g), r AS (SELECT (h & 2047)::int AS idx, (h >> 11) & 9007199254740991 AS w FROM h), k AS (SELECT idx, max(CASE WHEN w = 0 THEN 54 ELSE round(log(2, (w & -w)::numeric))::int + 1 END) AS rank FROM r GROUP BY idx) SELECT md5(decode(string_agg(lpad(to_hex(coalesce(k.rank, 0)), 2, '0'), '' ORDER BY i), 'hex')) FROM generate_series(0, 2047) i LEFT JOIN k ON k.idx = i")"

# A SEPARATE TRANSACTION for the create and the insert, and two columns: the writer
# goes parallel only for natts >= 2 and a relation not new in the current transaction.
# The DEBUG1 line is the evidence a worker flushed a column; without it this check
# would compare two serial sketches.
psql_run "CREATE TABLE skpar (a bigint, b bigint) USING pgcolumnar;" >/dev/null 2>&1
psql_run "SELECT pgcolumnar.set_options('skpar', sketch_columns => ARRAY['a']);" >/dev/null 2>&1
_par="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-c "SET pgcolumnar.parallel_flush = on; SET client_min_messages = debug1;
		INSERT INTO skpar SELECT g, g FROM generate_series(1, 1000) g;" 2>&1)"
check "premise: the parallel insert flushed columns in a worker" \
	"$(printf '%s\n' "$_par" | grep -cE 'parallel_flush: [1-9][0-9]* of 2 columns by [1-9]')" "1"
check "a parallel flush writes the same sketch as a serial one" \
	"$(q "SELECT md5(k.registers) FROM pgcolumnar.sketch k
		JOIN pgcolumnar.storage s ON s.storage_id = k.storage_id
		WHERE s.relation_oid = 'skpar'::regclass AND k.column_index = 0;")" "ce25d8b59b9f5a5752c42116b4ff9212"

# --- 3. reset, projections, cleanup ------------------------------------------

psql_run "SELECT pgcolumnar.add_projection('sk', 'skproj', ARRAY['a']);" >/dev/null 2>&1
psql_run "INSERT INTO sk SELECT g, 'p' || g FROM generate_series(1, 1000) g;" >/dev/null 2>&1
check "premise: the projection's storage holds row groups" \
	"$(q "SELECT CASE WHEN count(*) > 0 THEN 'some' ELSE 'none' END FROM pgcolumnar.row_group
		WHERE storage_id IN (SELECT proj_storage_id FROM pgcolumnar.projection WHERE name = 'skproj');")" "some"
check "a projection's storage gets no sketch" \
	"$(q "SELECT count(*) FROM pgcolumnar.sketch
		WHERE storage_id IN (SELECT proj_storage_id FROM pgcolumnar.projection WHERE name = 'skproj');")" "0"

psql_run "SELECT pgcolumnar.vacuum('sk');" >/dev/null 2>&1
check "a rewrite leaves one sketch per surviving row group" "$(sketches sk 0) for $(groups sk) groups" "$(groups sk) for $(groups sk) groups"

# compact_rewrite retires each rewritten group one at a time (PgColumnarRetireGroup),
# where vacuum above drops the whole old storage: two different cleanup paths.
_rw="$(q "DELETE FROM sk WHERE a % 3 = 0; SELECT pgcolumnar.compact_rewrite('sk');" | tail -1)"
check "premise: compact_rewrite rewrote row groups" \
	"$([ "${_rw:-0}" -gt 0 ] 2>/dev/null && echo yes || echo "no (${_rw:-<none>})")" "yes"
check "compact_rewrite leaves one sketch per surviving row group" \
	"$(sketches sk 0) for $(groups sk) groups" "$(groups sk) for $(groups sk) groups"

psql_run "SELECT pgcolumnar.reset_options('sk', sketch_columns => true);" >/dev/null 2>&1
check "reset_options clears sketch_columns" \
	"$(q "SELECT coalesce(sketch_columns::text, 'NULL') FROM pgcolumnar.options WHERE regclass = 'sk'::regclass;")" "NULL"
_before="$(sketches sk 0)"
_gbefore="$(groups sk)"
psql_run "INSERT INTO sk SELECT g, 'w' || g FROM generate_series(1, 1000) g;" >/dev/null 2>&1
check "premise: the insert after the reset wrote a row group" "$(( $(groups sk) - ${_gbefore:-0} ))" "1"
check "a row group written after the reset gets no sketch" \
	"$(( $(sketches sk 0) - ${_before:-0} )) new over ${_before:-none} before" "0 new over $_before before"

_sid="$(q "SELECT storage_id FROM pgcolumnar.storage WHERE relation_oid = 'sk1'::regclass;")"
psql_run "TRUNCATE sk1;" >/dev/null 2>&1
check "TRUNCATE leaves no sketch for the old storage" \
	"$(q "SELECT count(*) FROM pgcolumnar.sketch WHERE storage_id = $_sid;")" "0"
_sid="$(q "SELECT storage_id FROM pgcolumnar.storage WHERE relation_oid = 'skpin'::regclass;")"
psql_run "DROP TABLE skpin;" >/dev/null 2>&1
check "DROP TABLE leaves no sketch behind" \
	"$(q "SELECT count(*) FROM pgcolumnar.sketch WHERE storage_id = $_sid;")" "0"

# THE INVARIANT, over every storage: a sketch row whose row group no longer exists is
# one some cleanup path forgot. Counting per table could not see the old storage a
# rewrite left behind.
check "no sketch outlives its row group" \
	"$(q "SELECT count(*) FROM pgcolumnar.sketch k WHERE NOT EXISTS (SELECT 1 FROM pgcolumnar.row_group r
		WHERE r.storage_id = k.storage_id AND r.group_number = k.group_number);")" "0"

pgc_summary

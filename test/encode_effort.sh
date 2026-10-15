#!/usr/bin/env bash
#
# pgColumnar: the per-table encode_effort option (#155).
#
# encode_effort = fast skips the FSST substring search -- the symbol-table
# build, the whole-corpus "does it help" decision, and the per-vector encode.
# That is where a text column's write cost lives: measured on 1,000,000 rows,
# one text column, loads run 1.2x to 5.7x faster without it.
#
# THE CHECKS DO NOT TIME ANYTHING. A wall-clock assertion on an encoder is the
# kind that fails on a busy box and gets believed anyway; this suite discriminates
# on stored bytes, which is deterministic. Skipping FSST is observable because
# the bytes change, and it changes them in a known direction: FSST is only ever
# chosen when it makes a vector smaller, so "fast" is never smaller than "full".
#
# THE SHAPE IS THE WHOLE DESIGN OF THIS FILE. On most text, FSST is computed and
# then loses, so full and fast store byte-for-byte identical data and any check
# comparing them passes without testing anything. Of seven shapes measured, five
# were identical. This file uses md5(g) -- 32 hex chars, high cardinality,
# shared alphabet -- because it is one of the two where FSST actually wins, and
# so one of the two where the option is observable at all.
#
# Usage:  test/encode_effort.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Hold fsst_min_gain_percent at 0 for the whole suite, deliberately.
#
# This file compares encode_effort=fast against full, and that comparison only
# says anything on a shape where FSST is actually chosen. md5 text is such a
# shape, but only marginally: FSST wins it by roughly 2 percent, which is exactly
# the sub-margin win the shipped default of 5 declines (#155, #271). At the
# default, FSST is dropped for both arms, they store identical bytes, and the
# suite fails reporting equal sizes. It did, on all five majors, the first time
# that default landed.
#
# Pinning it states the dependency rather than inheriting it: the option under
# test here is encode_effort, so the margin is held where FSST stays in play
# instead of being left to a default that is free to move. The margin's own
# behaviour is covered by fsst_margin.sh.
#
# Set on the database rather than per statement because psql_run opens its own
# connection per call, so a SET would not outlive one, and every load in this
# file needs it, not only the first pair.
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.fsst_min_gain_percent = 0;" >/dev/null 2>&1

ROWS=${PGC_EFFORT_ROWS:-200000}

stored() {	# byte-accurate stored size; pg_total_relation_size is page-granular
	q "SELECT coalesce(sum(page_length), 0) FROM pgcolumnar.column_chunk
		WHERE storage_id = pgcolumnar.get_storage_id('$1');" | tail -1
}

# --- 1. the option is validated and stored ------------------------------------

psql_run "DROP TABLE IF EXISTS ef_a; CREATE TABLE ef_a (v text) USING pgcolumnar;" >/dev/null

bad="$(psql_run "SELECT pgcolumnar.set_options('ef_a', encode_effort => 'turbo');" 2>&1 || true)"
check "an unknown encode_effort is refused" \
	"$(case "$bad" in *'unknown columnar encode_effort'*) echo yes ;; *) echo "no ($bad)" ;; esac)" \
	"yes"

# A rejected value must not be stored. If the exception left a row behind, the
# next writer would read it and the refusal would be cosmetic.
check "and the refused value was not stored" \
	"$(q "SELECT count(*) FROM pgcolumnar.options
		WHERE regclass = 'ef_a'::regclass AND encode_effort IS NOT NULL;" | tail -1)" "0"

psql_run "SELECT pgcolumnar.set_options('ef_a', encode_effort => 'fast');" >/dev/null
check "a valid encode_effort is stored" \
	"$(q "SELECT encode_effort FROM pgcolumnar.options WHERE regclass = 'ef_a'::regclass;" | tail -1)" \
	"fast"

# The compression option's own guard (#1341), its sibling. Without it an unknown
# name is stored, and the C reader maps a name it does not know to -1 and falls
# back to the instance default: the table silently compresses as something else.
psql_run "SELECT pgcolumnar.set_options('ef_a', compression => 'zstd');" >/dev/null
badc="$(psql_run "SELECT pgcolumnar.set_options('ef_a', compression => 'brotli');" 2>&1 || true)"
check "an unknown compression name is refused" \
	"$(case "$badc" in *'unknown columnar compression "brotli"'*) echo yes ;; *) echo "no ($badc)" ;; esac)" \
	"yes"
check "and the compression set before it is still the one stored" \
	"$(q "SELECT compression FROM pgcolumnar.options WHERE regclass = 'ef_a'::regclass;" | tail -1)" \
	"zstd"

# --- 2. it changes what the writer does ---------------------------------------

psql_run "DROP TABLE IF EXISTS ef_full; DROP TABLE IF EXISTS ef_fast;
	CREATE TABLE ef_full (v text) USING pgcolumnar;
	CREATE TABLE ef_fast (v text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ef_fast', encode_effort => 'fast');" >/dev/null

psql_run "INSERT INTO ef_full SELECT md5(g::text) FROM generate_series(1, $ROWS) g;" >/dev/null
psql_run "INSERT INTO ef_fast SELECT md5(g::text) FROM generate_series(1, $ROWS) g;" >/dev/null

full_sz="$(stored ef_full)"
fast_sz="$(stored ef_fast)"

# The observable effect. If this ever reports equal, either the option stopped
# reaching the writer or the shape stopped being one where FSST wins -- and the
# next check tells you which.
check "fast stores more than full on a shape where FSST wins" \
	"$([ "${fast_sz:-0}" -gt "${full_sz:-0}" ] && echo yes || echo "no ($fast_sz vs $full_sz)")" \
	"yes"

# Guards the premise of the check above rather than the fix: on a shape where
# FSST loses, both arms are identical and the comparison proves nothing.
psql_run "DROP TABLE IF EXISTS ef_flat; DROP TABLE IF EXISTS ef_flatf;
	CREATE TABLE ef_flat (v text) USING pgcolumnar;
	CREATE TABLE ef_flatf (v text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ef_flatf', encode_effort => 'fast');" >/dev/null
psql_run "INSERT INTO ef_flat SELECT repeat('a', 8) FROM generate_series(1, $ROWS) g;" >/dev/null
psql_run "INSERT INTO ef_flatf SELECT repeat('a', 8) FROM generate_series(1, $ROWS) g;" >/dev/null
check "and is identical where FSST does not win" \
	"$(stored ef_flat)" "$(stored ef_flatf)"

# --- 3. it changes cost only, never content -----------------------------------

check "the rows are identical either way" \
	"$(q "SELECT md5(string_agg(v, ',' ORDER BY v)) FROM ef_fast;" | tail -1)" \
	"$(q "SELECT md5(string_agg(v, ',' ORDER BY v)) FROM ef_full;" | tail -1)"

check "and a filtered read agrees" \
	"$(q "SELECT count(*) FROM ef_fast WHERE v LIKE 'a%';" | tail -1)" \
	"$(q "SELECT count(*) FROM ef_full WHERE v LIKE 'a%';" | tail -1)"

# A fast-written chunk must be readable through the ordinary fetch path too,
# since it carries no shared symbol table where a full-written one does.
psql_run "CREATE INDEX ef_fast_v ON ef_fast (v);" >/dev/null
probe="$(q "SELECT md5('12345');" | tail -1)"
check "and an index fetch finds a fast-written row" \
	"$(q "SET enable_seqscan = off; SET pgcolumnar.enable_custom_scan = off;
		SELECT count(*) FROM ef_fast WHERE v = '$probe';" | tail -1)" "1"

# --- 4. the default is unchanged ----------------------------------------------

# The default must keep today's effort: a storage format that quietly got worse
# at compressing between releases is a bad trade even when it is faster.
psql_run "DROP TABLE IF EXISTS ef_def; CREATE TABLE ef_def (v text) USING pgcolumnar;" >/dev/null
psql_run "INSERT INTO ef_def SELECT md5(g::text) FROM generate_series(1, $ROWS) g;" >/dev/null
check "a table with no option set matches full" "$(stored ef_def)" "$full_sz"

psql_run "SELECT pgcolumnar.reset_options('ef_fast', encode_effort => true);" >/dev/null
check "reset_options clears it" \
	"$(q "SELECT coalesce(encode_effort, 'null') FROM pgcolumnar.options
		WHERE regclass = 'ef_fast'::regclass;" | tail -1)" "null"

pgc_summary

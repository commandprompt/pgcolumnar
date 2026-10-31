#!/usr/bin/env bash
#
# pgColumnar Parquet decode-hardening suite (Phase G).
#
# The Parquet reader binds several PostgreSQL types to a wider physical type,
# because Parquet has no narrower one: int2 and date ride on INT32, time and the
# timestamps on INT64. A value outside the target type's range must raise, not
# wrap into a different value. Separately, the row-group column-chunk list is
# indexed by the schema's leaf count, so a row group that carries a different
# number of chunks must be rejected at parse time rather than read out of bounds.
#
# This complements test/corruption.sh (native catalog fields) and
# test/hardening.sh (native on-disk bytes); neither covers Parquet input.
# Run on an assert-enabled build so an out-of-bounds access trips an assertion
# rather than passing silently.
#
# HOW A REFUSAL IS READ. Four of the five range refusals share 22008, so each check
# wants its guard's own message ("value out of range for type <T>") as well as the
# SQLSTATE. Every reading first waits for the server, and survival also reads the
# server log's count of backends killed by a signal: SELECT 1 answers again once
# crash recovery has finished, so it alone passes a crash (#1341).
#
# HOW AN IN-RANGE BIND IS READ. Each "still reads" check compares the decoded values
# with the ones the writer stored, not a row count: a decoder one day or one
# microsecond off returned the right count and passed (#1341). The no-wrap check
# reads the out-of-range file itself, which must refuse; it used to count a wrapped
# value in the in-range file, which never holds one, so it could not fail.
#
# Usage:  test/native_parquet_hardening.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; Parquet hardening suite needs it"
fi

W="$PGC_WORKDIR"

wait_up() { local _i; for _i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && return; sleep 0.5; done; }
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since BEFORE
	local _up _now
	wait_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"; return; fi
	if [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1; return; fi
	echo "no ([$_up], $(( _now - $1 )) backend crashes)"
}
# qw SQL -> q's answer, after the server answers: a crash in an earlier arm must
# fail only that arm, not every reading made during recovery.
qw() { wait_up; q "$1"; }
CRASHES_BEFORE="$(crashes)"
psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# errfull QUERY -> psql's whole output for QUERY (ERROR and DETAIL lines included)
errfull() {
	wait_up
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1
}

# errs QUERY -> "OK" when the statement raises, "NO ERROR: <rows>" when it does not
errs() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1)"
	case "$out" in
		*ERROR*) echo "OK" ;;
		*) echo "NO ERROR: $out" ;;
	esac
}

# ---- narrowing: out-of-range values must raise, not wrap --------------------
python3 "$(dirname "${BASH_SOURCE[0]}")/craft_parquet_hardening.py" "$W"

psql_run "CREATE FOREIGN TABLE f_i2_bad   (c int2)      SERVER pq OPTIONS (path '$W/i2_bad.parquet');"
psql_run "CREATE FOREIGN TABLE f_i2_ok    (c int2)      SERVER pq OPTIONS (path '$W/i2_ok.parquet');"
psql_run "CREATE FOREIGN TABLE f_time_bad (c time)      SERVER pq OPTIONS (path '$W/time_bad.parquet');"
psql_run "CREATE FOREIGN TABLE f_time_ok  (c time)      SERVER pq OPTIONS (path '$W/time_ok.parquet');"
psql_run "CREATE FOREIGN TABLE f_date_bad (c date)      SERVER pq OPTIONS (path '$W/date_bad.parquet');"
psql_run "CREATE FOREIGN TABLE f_date_ovf (c date)     SERVER pq OPTIONS (path '$W/date_ovf.parquet');"
psql_run "CREATE FOREIGN TABLE f_date_ok  (c date)      SERVER pq OPTIONS (path '$W/date_ok.parquet');"
psql_run "CREATE FOREIGN TABLE f_ts_bad   (c timestamp) SERVER pq OPTIONS (path '$W/ts_bad.parquet');"
psql_run "CREATE FOREIGN TABLE f_ts_ok    (c timestamp) SERVER pq OPTIONS (path '$W/ts_ok.parquet');"

# Pin the SQLSTATE rather than accepting any error, so an unrelated failure
# cannot pass these: 22003 numeric_value_out_of_range, 22008 datetime_field_overflow.
psql_run "CREATE FUNCTION pgc_try(q text) RETURNS text LANGUAGE plpgsql AS \$\$
          BEGIN EXECUTE q; RETURN 'NO ERROR';
          EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE || ': ' || SQLERRM; END \$\$;"
# pgc_value(q) -> the query's one value as text, or its SQLSTATE when it raises
psql_run "CREATE FUNCTION pgc_value(q text) RETURNS text LANGUAGE plpgsql AS \$\$
          DECLARE r text;
          BEGIN EXECUTE q INTO r; RETURN coalesce(r, '<null>');
          EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE; END \$\$;"
# refusal SQL -> "SQLSTATE: message" of the error SQL raised, or NO ERROR
sqlstate() { qw "SELECT pgc_try(\$q\$$1\$q\$);"; }

check "int2 out of range -> 22003"        "$(sqlstate 'SELECT * FROM f_i2_bad')"   "22003: value out of range for type smallint"
check "time out of day -> 22008"          "$(sqlstate 'SELECT * FROM f_time_bad')" "22008: value out of range for type time"
check "date invalid -> 22008"             "$(sqlstate 'SELECT * FROM f_date_bad')" "22008: value out of range for type date"
check "date offset overflow -> 22008"     "$(sqlstate 'SELECT * FROM f_date_ovf')" "22008: value out of range for type date"
check "timestamp overflow -> 22008"       "$(sqlstate 'SELECT * FROM f_ts_bad')"   "22008: value out of range for type timestamp"

# the legitimate binds must still work: the check must not disable them
check "int2 in range still reads"      "$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_i2_ok;")" "-30000 0 30000"
check "time in range still reads"      "$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_time_ok;")" "00:00:00 01:00:00"
check "date in range still reads"      "$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_date_ok;")" "1970-01-01 2022-01-08"
check "timestamp in range still reads" "$(qw "SELECT string_agg(c::text, ',' ORDER BY c) FROM f_ts_ok;")" "1970-01-01 00:00:00,2023-11-14 22:13:20"

# no wrapped value may ever be observable: the out-of-range file itself must refuse
# rather than answer. Counting -25536 in f_i2_ok, which never holds 40000, could not fail.
check "no wrapped -25536 appears" \
	"$(qw "SELECT pgc_value(\$q\$SELECT count(*) FROM f_i2_bad WHERE c = (-25536)::int2\$q\$);")" "22003"

# ---- the same decoder is shared by read_parquet and import_parquet ----------
check "read_parquet also raises 22003" \
	"$(sqlstate "SELECT * FROM pgcolumnar.read_parquet('$W/i2_bad.parquet') AS t(c int2)")" "22003: value out of range for type smallint"
check "read_parquet in range still reads" \
	"$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM pgcolumnar.read_parquet('$W/i2_ok.parquet') AS t(c int2);")" "-30000 0 30000"

psql_run "CREATE TABLE imp_bad (c int2);"
check "import_parquet also raises 22003" \
	"$(sqlstate "SELECT pgcolumnar.import_parquet('imp_bad'::regclass, '$W/i2_bad.parquet')")" "22003: value out of range for type smallint"
check_num "import_parquet left no wrapped rows" "$(qw 'SELECT count(*) FROM imp_bad;')" "0"

# ---- malformed row-group chunk list ----------------------------------------
# The crafter cut the column-chunk list of a valid two-column file so its row group
# carries fewer chunks than the schema has leaves (short_chunks.parquet). The reader
# indexes chunks[] by the schema leaf count, so this must be rejected at parse time.
psql_run "CREATE FOREIGN TABLE f_short (a int4, b int4) SERVER pq OPTIONS (path '$W/short_chunks.parquet');"
# Must be the specific malformed-row-group rejection. Before the guard this read
# chunks[1] out of bounds and failed incidentally, e.g. "invalid memory alloc
# request size", an out-of-bounds read that merely tripped an allocator check.
#
# Five guards raise "has a malformed row group", so the message alone let another one
# stand in: with the chunk-count guard removed, a later guard rejected the file and
# this passed 18/18 (#1341). The verdict wants THIS guard's detail.
short_out="$(errfull 'SELECT * FROM f_short;')"
case "$short_out" in
	*"has a malformed row group"*"carries 1 column chunks, but the schema has 2 leaf columns"*)
		short_verdict="CLEAN REJECT" ;;
	*) short_verdict="$(printf '%s' "$short_out" | grep -m1 -E '^(ERROR|DETAIL):' || echo "NO ERROR")" ;;
esac
check "short chunk list rejected with a specific error" "$short_verdict" "CLEAN REJECT"
check "backend survived short chunk list" "$(survived "$CRASHES_BEFORE")" "1"
# The validation gates the read paths, not parsing, so the diagnostic function
# still works on exactly the file one would be diagnosing.
check "parquet_schema still describes a bad chunk list" \
	"$(qw "SELECT count(*) FROM pgcolumnar.parquet_schema('$W/short_chunks.parquet');")" "2"
check "valid two-column file still reads" \
	"$(qw "SELECT string_agg(a || '|' || b, ' ' ORDER BY a) FROM pgcolumnar.read_parquet('$W/two.parquet') AS t(a int4, b int4);")" "1|4 2|5 3|6"


# ---- malformed footer Thrift (#1822) ----------------------------------------
# The crafter decoded a good 3000-row file's footer and wrote variants of it. A
# field header with an invalid type nibble was skipped as zero bytes, so the parser
# lost its place and could lose row_groups or a num_rows: 0 or 2000 rows came back
# with no error. Each refusal is read as "SQLSTATE: message / detail" with the file
# path masked, so the reading names the guard that fired. Each read is
# "count/sum(id)", so a silent wrong answer shows its numbers.
psql_run "CREATE FUNCTION pgc_err(q text) RETURNS text LANGUAGE plpgsql AS \$\$
          DECLARE r text; d text;
          BEGIN EXECUTE q INTO r; RETURN 'NO ERROR: ' || coalesce(r, '<null>');
          EXCEPTION WHEN OTHERS THEN
            GET STACKED DIAGNOSTICS d = PG_EXCEPTION_DETAIL;
            RETURN SQLSTATE || ': ' || regexp_replace(SQLERRM, '\"[^\"]*\"', '\"<file>\"')
                   || coalesce(' / ' || nullif(d, ''), ''); END \$\$;"
# footer_read FILE -> "count/sum(id)" of the file, or the refusal
footer_read() {
	qw "SELECT pgc_err(\$q\$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pgcolumnar.read_parquet('$W/$1.parquet') AS t(id bigint, name text)\$q\$);"
}
FOOTER_BEFORE="$(crashes)"
UNPARSED='0A000: could not parse "<file>" (unsupported or corrupt Parquet metadata)'
check "footer: the good file still reads"            "$(footer_read footer_good)"      "NO ERROR: 3000/4498500"
check "footer: the re-encoded good file still reads" "$(footer_read footer_reencoded)" "NO ERROR: 3000/4498500"
check "footer: an invalid type nibble on row_groups is refused" "$(footer_read nibble_row_groups)" "$UNPARSED"
check "footer: an invalid type nibble on a num_rows is refused" "$(footer_read nibble_num_rows)"   "$UNPARSED"
# Every field header in the footer, each set to type 15 in its own copy. The
# reading names each copy that read without an error, with its answer.
NSWEEP="$(cat "$W/nibble_sweep.count" 2>/dev/null)"
check "footer: every invalid type nibble on a field header is refused" \
	"$(qw "SELECT count(*) || ' of $NSWEEP read without an error' || coalesce(': ' || string_agg(i || '=' || v, ' ' ORDER BY i), '')
	       FROM (SELECT i, pgc_err(format(\$f\$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pgcolumnar.read_parquet(%L) AS t(id bigint, name text)\$f\$,
	                                     '$W/nibble_sweep_' || lpad(i::text, 4, '0') || '.parquet')) AS v
	             FROM generate_series(0, ${NSWEEP:-0} - 1) AS i) s
	      WHERE v LIKE 'NO ERROR%';")" \
	"0 of $NSWEEP read without an error"
check "footer: no row_groups field is refused" "$(footer_read no_row_groups)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer has no row_groups field.'
check "footer: a row group with no num_rows is refused" "$(footer_read no_rg_num_rows)" \
	'XX001: Parquet file "<file>" has a malformed row group / Row group 2 has no num_rows field.'
check "footer: no file num_rows is refused" "$(footer_read no_file_num_rows)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer has no num_rows field.'
check "footer: a file num_rows that differs from its row groups is refused" "$(footer_read file_num_rows_2999)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer declares num_rows 2999, but its row groups hold 3000 rows.'
check "footer: an unknown map field is skipped whole"     "$(footer_read unknown_map_field)"       "NO ERROR: 3000/4498500"
check "footer: an unknown list of bools is skipped whole" "$(footer_read unknown_bool_list_field)" "NO ERROR: 3000/4498500"
check "backend survived the malformed footers" "$(survived "$FOOTER_BEFORE")" "1"

pgc_summary

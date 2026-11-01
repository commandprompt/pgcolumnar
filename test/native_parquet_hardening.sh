#!/usr/bin/env bash
#
# pgColumnar Parquet decode-hardening suite (Phase G).
#
# The Parquet reader binds several PostgreSQL types to a wider physical type,
# because Parquet has no narrower one: int2 and date ride on INT32, time and the
# timestamps on INT64. A value outside the target type's range must raise, not
# wrap into a different value. An unsigned column (UINT_32, UINT_64) is the same
# rule from the other side: its upper half has the sign bit set, so it must read
# as the stored value into bigint or numeric, and refuse a narrower type, never
# come back negative (#1814). Separately, the row-group column-chunk list is
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
# HOW A STRING CELL IS READ (#1821). A Parquet STRING is UTF-8 by the spec, and the
# reader used to turn its bytes into text unchecked, so a file holding invalid
# UTF-8 or an embedded NUL returned text the server would never accept from COPY.
# These arms run in a database created as UTF8, because the check is against the
# server encoding and a SQL_ASCII cluster (initdb under LANG=C) accepts any byte
# but NUL. Each refusal wants 22021 and the reader's message, which names the
# column and the first bad bytes, as core's own message does.
#
# A FOOTER THAT DOES NOT MATCH ITS PAGES (#1855, #1852). A column whose footer
# says REQUIRED, while its pages carry definition levels, used to read the levels
# as values. So did a repetition_type that is no valid value. Both are refused now,
# and so is a list group marked OPTIONAL whose pages carry repetition levels.
#
# INT96 VALUES THAT ARE NOT TIMESTAMPS (#1870). Nanoseconds outside the day, a
# Julian day before timestamp's range, and a page shorter than its num_values
# are each refused; a value refusal names both parts of the INT96 value.
#
# A PAGE WHOSE SIZE IS NOT ITS HEADER'S (#1910), AND A NEGATIVE num_children
# (#1921). A Snappy page that decompresses to a size other than its header's is
# refused, as GZIP, ZSTD and LZ4_RAW pages already were. A schema group whose
# num_children is negative is refused, by parquet_schema as well.
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
# The second argument is the committed Iceberg warehouse the string arms copy.
python3 "$(dirname "${BASH_SOURCE[0]}")/craft_parquet_hardening.py" "$W" \
	"$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg/warehouse"

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

# ---- unsigned integers: read as unsigned, never as a wrapped negative (#1814) ----
# pyarrow writes uint32 as INT32 and uint64 as INT64, marked UINT_32/UINT_64. The
# reader used to drop the mark, so 2^31 read as -2147483648 and 2^64-1 as -1, with
# no error, through the very types parquet_schema advised. A wider target (bigint
# for UINT_32, numeric for UINT_64) must now read the stored value, and a narrower
# one must refuse with 22003 rather than wrap.
check "premise: pyarrow reads the unsigned file's values unsigned" \
	"$(python3 -c "import sys, pyarrow.parquet as pq; t = pq.read_table(sys.argv[1]); print(' '.join(str(v) for c in ('u32', 'u64') for v in t.column(c).to_pylist()))" "$W/unsigned.parquet" 2>&1)" \
	"1 2147483648 4294967295 1 9223372036854775808 18446744073709551615"
psql_run "CREATE FOREIGN TABLE f_u32_i8  (c int8)    SERVER pq OPTIONS (path '$W/u32.parquet');"
psql_run "CREATE FOREIGN TABLE f_u32_i4  (c int4)    SERVER pq OPTIONS (path '$W/u32.parquet');"
psql_run "CREATE FOREIGN TABLE f_u64_num (c numeric) SERVER pq OPTIONS (path '$W/u64.parquet');"
psql_run "CREATE FOREIGN TABLE f_u64_i8  (c int8)    SERVER pq OPTIONS (path '$W/u64.parquet');"
psql_run "CREATE FOREIGN TABLE f_u32max_i2 (c int2)  SERVER pq OPTIONS (path '$W/u32_max.parquet');"

check "parquet_schema advises a type that holds every unsigned value" \
	"$(qw "SELECT string_agg(column_name || ' ' || data_type, '|' ORDER BY column_name) FROM pgcolumnar.parquet_schema('$W/unsigned.parquet');")" \
	"u16 integer|u32 bigint|u64 numeric(20,0)|u8 integer"
check "UINT_32 reads into bigint as unsigned" \
	"$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_u32_i8;")" "1 2147483648 4294967295"
check "UINT_64 reads into numeric as unsigned" \
	"$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_u64_num;")" "1 9223372036854775808 18446744073709551615"
check "UINT_32 into bigint: WHERE c > 0 counts every row" \
	"$(qw "SELECT count(*) FROM f_u32_i8 WHERE c > 0;")" "3"
check "UINT_32 at or above 2^31 into integer -> 22003" \
	"$(sqlstate 'SELECT * FROM f_u32_i4')" "22003: value out of range for type integer"
check "UINT_64 at or above 2^63 into bigint -> 22003" \
	"$(sqlstate 'SELECT * FROM f_u64_i8')" "22003: value out of range for type bigint"
check "UINT_32 2^32-1 into smallint -> 22003, not -1" \
	"$(sqlstate 'SELECT * FROM f_u32max_i2')" "22003: value out of range for type smallint"
# the DETAIL names the value the file stores, not its signed reading
u64_detail="$(errfull 'SELECT * FROM f_u64_i8;')"
case "$u64_detail" in
	*"DETAIL:  The Parquet file stores 9223372036854775808, which cannot be represented as bigint."*)
		u64_detail="names 9223372036854775808" ;;
	*) u64_detail="$(printf '%s' "$u64_detail" | grep -m1 -E '^(ERROR|DETAIL):' || echo "NO ERROR")" ;;
esac
check "the UINT_64 refusal names the stored value" "$u64_detail" "names 9223372036854775808"
check "read_parquet reads every unsigned width at its advised type" \
	"$(qw "SELECT string_agg(u8 || '|' || u16 || '|' || u32 || '|' || u64, ' ' ORDER BY u64) FROM pgcolumnar.read_parquet('$W/unsigned.parquet') AS t(u8 int, u16 int, u32 bigint, u64 numeric);")" \
	"1|1|1|1 200|40000|2147483648|9223372036854775808 255|65535|4294967295|18446744073709551615"
check "read_parquet UINT_64 into bigint -> 22003" \
	"$(sqlstate "SELECT * FROM pgcolumnar.read_parquet('$W/u64.parquet') AS t(c bigint)")" "22003: value out of range for type bigint"
# pyarrow writes both spellings of the mark; the crafter keeps one in each of these
check "UINT_32 marked only by its LogicalType reads into bigint as unsigned" \
	"$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM pgcolumnar.read_parquet('$W/u32_logical_only.parquet') AS t(c bigint);")" "1 2147483648 4294967295"
check "UINT_32 marked only by its ConvertedType reads into bigint as unsigned" \
	"$(qw "SELECT string_agg(c::text, ' ' ORDER BY c) FROM pgcolumnar.read_parquet('$W/u32_converted_only.parquet') AS t(c bigint);")" "1 2147483648 4294967295"
psql_run "CREATE TABLE imp_u (u8 int, u16 int, u32 bigint, u64 numeric);"
psql_run "CREATE TABLE imp_u32_i4 (c int4);"
check "import_parquet reads UINT_32 into bigint and UINT_64 into numeric" \
	"$(qw "SELECT pgc_value(\$q\$SELECT pgcolumnar.import_parquet('imp_u'::regclass, '$W/unsigned.parquet')\$q\$);"):$(qw "SELECT string_agg(u32 || '|' || u64, ' ' ORDER BY u64) FROM imp_u;")" \
	"3:1|1 2147483648|9223372036854775808 4294967295|18446744073709551615"
check "import_parquet UINT_32 into integer -> 22003" \
	"$(sqlstate "SELECT pgcolumnar.import_parquet('imp_u32_i4'::regclass, '$W/u32.parquet')")" "22003: value out of range for type integer"

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

# ---- a repetition_type that does not match the pages (#1855) ---------------
# The crafter changed the `id` column's repetition_type in the footer: REQUIRED in
# rep_required.parquet, and 7, which is no repetition_type, in rep_invalid.parquet.
# The pages still carry definition levels. The reader used to decode those levels
# as values and return 3000/3019995 instead of 3000/4498500, with no error. The
# REQUIRED file is refused by its level histogram, which no longer fits the schema;
# the invalid value is refused when the footer is read.
# rep_verdict SQL DETAIL SQLSTATE -> "CLEAN REJECT" when SQL raised SQLSTATE with
# DETAIL, else the ERROR and DETAIL lines it raised (the ERROR line carries its
# SQLSTATE), or NO ERROR and the rows it returned.
rep_verdict() {
	local out
	wait_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c '\set VERBOSITY verbose' -c "$1" 2>&1)"
	case "$out" in
		*"ERROR:  $3:"*"DETAIL:  $2"*) echo "CLEAN REJECT" ;;
		*ERROR*) printf '%s' "$out" | grep -E '^(ERROR|DETAIL):' | tr '\n' ' ' ;;
		*) echo "NO ERROR: $out" ;;
	esac
}
REP_SUM="SELECT count(*) || '/' || sum(id) FROM pgcolumnar.read_parquet"
REP_HIST='Row group 0 column 0 ("id") has a definition level histogram of 2 entries, but the schema gives it a maximum definition level of 0.'
REP_ENUM='Schema element "id" has repetition_type 7; a valid one is 0 (REQUIRED), 1 (OPTIONAL) or 2 (REPEATED).'
check "the unchanged repetition file reads 3000/4498500" \
	"$(qw "$REP_SUM('$W/rep_base.parquet') AS t(id bigint, name text);")" "3000/4498500"
check "repetition_type REQUIRED over optional pages is refused" \
	"$(rep_verdict "$REP_SUM('$W/rep_required.parquet') AS t(id bigint, name text);" "$REP_HIST" XX001)" \
	"CLEAN REJECT"
check "repetition_type that is not 0, 1 or 2 is refused" \
	"$(rep_verdict "$REP_SUM('$W/rep_invalid.parquet') AS t(id bigint, name text);" "$REP_ENUM" XX001)" \
	"CLEAN REJECT"
psql_run "CREATE TABLE imp_rep (id bigint, name text);"
check "import_parquet refuses repetition_type REQUIRED over optional pages" \
	"$(rep_verdict "SELECT pgcolumnar.import_parquet('imp_rep'::regclass, '$W/rep_required.parquet');" "$REP_HIST" XX001)" \
	"CLEAN REJECT"

# ---- a repetition_type that does not match the repetition levels (#1855) ---
# lst_optional.parquet is lst_base.parquet, a list<int64> column, with its `list`
# group marked OPTIONAL instead of REPEATED. The definition levels still fit the
# schema; the chunk's repetition level histogram of 2 entries does not, since the
# schema now gives `element` a maximum repetition level of 0. The base file reads,
# so a check that refuses every repetition histogram fails here.
LST_HIST='Row group 0 column 0 ("element") has a repetition level histogram of 2 entries, but the schema gives it a maximum repetition level of 0.'
LST_AGG="SELECT string_agg(coalesce(l::text, 'null'), ' ' ORDER BY l NULLS LAST) FROM pgcolumnar.read_parquet"
check "the unchanged list file reads {} {1,2} {3} {4,5,6} null" \
	"$(qw "$LST_AGG('$W/lst_base.parquet') AS t(l bigint[]);")" "{} {1,2} {3} {4,5,6} null"
check "a list group marked OPTIONAL over repeated pages is refused" \
	"$(rep_verdict "$LST_AGG('$W/lst_optional.parquet') AS t(l bigint[]);" "$LST_HIST" XX001)" \
	"CLEAN REJECT"
check "a list group marked OPTIONAL over repeated pages is refused into a scalar column" \
	"$(rep_verdict "SELECT count(*) || '/' || count(l) FROM pgcolumnar.read_parquet('$W/lst_optional.parquet') AS t(l bigint);" "$LST_HIST" XX001)" \
	"CLEAN REJECT"

# ---- INT96 values that are not timestamps (#1870) ---------------------------
# The crafter patched the second of three values in a REQUIRED, PLAIN INT96
# column: nanoseconds of one whole day, nanoseconds of -1, and Julian day -1.
# No valid INT96 value has any of them. Each read wants 22008 and a DETAIL that
# names both parts. count(t) needs no output function, so only the reader can
# raise. i96_short.parquet says its page of 3 values is 24 bytes long, not 36:
# the third value lies past the page's end, and must not be read from there.
I96_CNT="SELECT count(*) || '/' || count(t) FROM pgcolumnar.read_parquet"
check "the unpatched REQUIRED INT96 file reads its 3 values" \
	"$(qw "$I96_CNT('$W/i96_req.parquet') AS t(t timestamptz);")" "3/3"
check "INT96 nanoseconds of a whole day are refused" \
	"$(rep_verdict "$I96_CNT('$W/i96_nanos_day.parquet') AS t(t timestamptz);" \
		'The Parquet file stores an INT96 timestamp of Julian day 2461246 and 86400000000000 nanoseconds, which cannot be represented as timestamp with time zone.' 22008)" \
	"CLEAN REJECT"
check "INT96 nanoseconds of -1 are refused" \
	"$(rep_verdict "$I96_CNT('$W/i96_nanos_neg.parquet') AS t(t timestamptz);" \
		'The Parquet file stores an INT96 timestamp of Julian day 2461246 and -1 nanoseconds, which cannot be represented as timestamp with time zone.' 22008)" \
	"CLEAN REJECT"
check "INT96 Julian day -1 is refused" \
	"$(rep_verdict "$I96_CNT('$W/i96_jday_neg.parquet') AS t(t timestamptz);" \
		'The Parquet file stores an INT96 timestamp of Julian day -1 and 123456789 nanoseconds, which cannot be represented as timestamp with time zone.' 22008)" \
	"CLEAN REJECT"
check "an INT96 page shorter than its num_values is refused" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c '\set VERBOSITY verbose' -c "$I96_CNT('$W/i96_short.parquet') AS t(t timestamptz);" 2>&1 |
		grep -E '^(ERROR|DETAIL):|^[0-9]+/[0-9]+$' | tr '\n' ' ')" \
	"ERROR:  XX001: could not decode Parquet column 0 in row group 0 "

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
          EXCEPTION WHEN OTHERS OR query_canceled THEN
            GET STACKED DIAGNOSTICS d = PG_EXCEPTION_DETAIL;
            RETURN SQLSTATE || ': ' || regexp_replace(SQLERRM, '\"[^\"]*\"', '\"<file>\"')
                   || coalesce(' / ' || nullif(d, ''), ''); END \$\$;"
# footer_read FILE -> "count/sum(id)" of the file, or the refusal
footer_read() {
	qw "SELECT pgc_err(\$q\$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pgcolumnar.read_parquet('$W/$1.parquet') AS t(id bigint, name text)\$q\$);"
}
# pgc_timed Q -> pgc_err's reading, then "within 3 s" or "after N ms"
psql_run "CREATE FUNCTION pgc_timed(q text) RETURNS text LANGUAGE plpgsql AS \$\$
          DECLARE t0 timestamptz := clock_timestamp(); v text; ms bigint;
          BEGIN v := pgc_err(q);
            ms := (extract(epoch FROM clock_timestamp() - t0) * 1000)::bigint;
            RETURN v || CASE WHEN ms < 3000 THEN ' within 3 s' ELSE ' after ' || ms || ' ms' END; END \$\$;"
# footer_timed FILE -> footer_read's reading with the time it took
footer_timed() {
	qw "SELECT pgc_timed(\$q\$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pgcolumnar.read_parquet('$W/$1.parquet') AS t(id bigint, name text)\$q\$);"
}
# footer_capped FILE -> footer_timed's reading under a 5 s statement_timeout, so a
# skip that loops billions of times reads as 57014 after about 5000 ms
footer_capped() {
	PGOPTIONS='-c statement_timeout=5s' footer_timed "$1"
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
# The same headers set to type 13 (UUID). The skip steps over 16 bytes, so a copy
# may read, but only with the good file's answer. The reading names each copy
# that read a different answer without an error.
check "footer: no type-13 (UUID) nibble on a field header reads a wrong answer" \
	"$(qw "SELECT count(*) || ' of $NSWEEP read a wrong answer' || coalesce(': ' || string_agg(i || '=' || v, ' ' ORDER BY i), '')
	       FROM (SELECT i, pgc_err(format(\$f\$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pgcolumnar.read_parquet(%L) AS t(id bigint, name text)\$f\$,
	                                     '$W/uuid_sweep_' || lpad(i::text, 4, '0') || '.parquet')) AS v
	             FROM generate_series(0, ${NSWEEP:-0} - 1) AS i) s
	      WHERE v LIKE 'NO ERROR%' AND v <> 'NO ERROR: 3000/4498500';")" \
	"0 of $NSWEEP read a wrong answer"
check "footer: no row_groups field is refused" "$(footer_read no_row_groups)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer has no row_groups field.'
check "footer: a row group with no num_rows is refused" "$(footer_read no_rg_num_rows)" \
	'XX001: Parquet file "<file>" has a malformed row group / Row group 2 has no num_rows field.'
check "footer: no file num_rows is refused" "$(footer_read no_file_num_rows)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer has no num_rows field.'
check "footer: a file num_rows that differs from its row groups is refused" "$(footer_read file_num_rows_2999)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer declares num_rows 2999, but its row groups hold 3000 rows.'
# The other direction loses rows: row group 3 says 999, the footer says 3000. A
# num_rows below a column's value count is legal (a repeated column), so the total
# check is the only guard. Checking only "total > num_rows" read 2999 rows.
check "footer: a row group num_rows below the footer's total is refused" "$(footer_read rg3_999)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer declares num_rows 3000, but its row groups hold 2999 rows.'
check "footer: a file num_rows above its row groups' total is refused" "$(footer_read file_num_rows_3001)" \
	'XX001: Parquet file "<file>" has a malformed footer / The footer declares num_rows 3001, but its row groups hold 3000 rows.'
# A type-0 nibble with a field-id delta is not STOP (only the byte 0x00 is). Read as
# STOP, the footer ended before row_groups and a later check refused it instead.
check "footer: a type-0 nibble with a field-id delta is refused as corrupt" "$(footer_read nibble0_row_groups)" "$UNPARSED"
check "footer: an unknown map field is skipped whole"     "$(footer_read unknown_map_field)"       "NO ERROR: 3000/4498500"
check "footer: an unknown list of bools is skipped whole" "$(footer_read unknown_bool_list_field)" "NO ERROR: 3000/4498500"
# Type 13 is UUID: a 16-byte value. parquet.thrift has none, but the skip must
# step over one whole, as it does any other unknown field.
check "footer: an unknown UUID field is skipped whole"      "$(footer_read unknown_uuid_field)"      "NO ERROR: 3000/4498500"
check "footer: an unknown list of UUIDs is skipped whole"   "$(footer_read unknown_uuid_list_field)" "NO ERROR: 3000/4498500"
# A list element of a type that is not a compact type has no known size. The
# skip refuses it; skipped as zero bytes, the list vanished and the file read.
check "footer: a list whose element type is not a compact type is refused" "$(footer_read list_of_type_14)" "$UNPARSED"
# The same refusal on a list of 4294967295 elements of type 15. Skipped as zero
# bytes, each element passed and the loop ran until the 5 s timeout.
check "footer: a huge list whose element type is not a compact type is refused at once" \
	"$(footer_capped list_elem15_huge)" "$UNPARSED within 3 s"
# A list of 4294967295 bytes or doubles that runs off the footer's end. The skip
# refuses the first element that does not fit. Stepping past the end without an
# error ran every element first: seconds of work from a few bytes of footer.
check "footer: a list of bytes that runs off the footer is refused at once" \
	"$(footer_timed byte_list_off_end)" "$UNPARSED within 3 s"
check "footer: a list of doubles that runs off the footer is refused at once" \
	"$(footer_timed double_list_off_end)" "$UNPARSED within 3 s"
# The `id` schema element without its name, a required field. A changed byte that
# ends the element early leaves the rest of the footer read as something else.
check "footer: a schema element with no name is refused" "$(footer_read no_schema_name)" "$UNPARSED"
# The `id` element's repetition_type set to 2^32, and the level histograms dropped.
# A cast to int read 2^32 as 0 (REQUIRED), and the definition levels then read as
# values. The file without histograms, and nothing else changed, still reads.
check "footer: the file without level histograms still reads" "$(footer_read rep_nohist)" "NO ERROR: 3000/4498500"
check "footer: a repetition_type of 2^32 is refused, not read as REQUIRED" \
	"$(footer_read rep_wide_nohist)" \
	'XX001: Parquet file "<file>" has a malformed schema / Schema element "id" has repetition_type 4294967296; a valid one is 0 (REQUIRED), 1 (OPTIONAL) or 2 (REPEATED).'
# A group whose num_children is -1 (#1921). The schema walk read it as a group
# with no children, and parquet_schema stopped there: it returned the columns
# before it and dropped the rest, with no error. The reading is the row count and
# the column names, or the refusal.
# schema_read FILE -> "N rows: names" of parquet_schema, or the refusal
schema_read() {
	qw "SELECT pgc_err(\$q\$SELECT count(*) || ' rows: ' || string_agg(column_name, ' ') FROM pgcolumnar.parquet_schema('$W/$1.parquet')\$q\$);"
}
check "footer: the unpatched struct file's schema has its three columns" \
	"$(schema_read grp_base)" "NO ERROR: 3 rows: a s b"
check "footer: a negative num_children is refused by parquet_schema" \
	"$(schema_read grp_neg_children)" \
	'XX001: Parquet file "<file>" has a malformed schema / Schema element "s" has num_children -1; a valid one is 0 or more.'
# A count wider than int (#1921). A plain cast to int keeps the low 32 bits, so
# 2^32 + 1 and -(2^32) + 1 would both read as 1, a valid count.
check "footer: a num_children of 2^32 + 1 is refused by parquet_schema" \
	"$(schema_read grp_wide_children)" \
	'XX001: Parquet file "<file>" has a malformed schema / Schema element "s" has num_children 4294967297; a valid one is 0 or more.'
check "footer: a num_children of -(2^32) + 1 is refused by parquet_schema" \
	"$(schema_read grp_neg_wide_children)" \
	'XX001: Parquet file "<file>" has a malformed schema / Schema element "s" has num_children -4294967295; a valid one is 0 or more.'
check "backend survived the malformed footers" "$(survived "$FOOTER_BEFORE")" "1"

# ---- a page whose decompressed length is not its header's (#1910) -----------
# Each copy changes only the page header's uncompressed_page_size. The SNAPPY
# reader trusted the length inside the Snappy stream and never compared it with
# the header, so a corrupt page read without an error: the nightly fuzz found a
# wide64.parquet mutant that read 50 rows with a different answer, which pyarrow
# refuses. The reading is "count/sum(id)", or the refusal.
# page_read FILE -> "count/sum(id)" of the file, or the refusal
page_read() {
	qw "SELECT pgc_err(\$q\$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pgcolumnar.read_parquet('$W/$1.parquet') AS t(id bigint)\$q\$);"
}
PAGE_REFUSED='XX001: could not decode Parquet column 0 in row group 0'
check "page: the unpatched SNAPPY v1 file reads" "$(page_read page_snappy_v1)" "NO ERROR: 100/5050"
check "page: the unpatched SNAPPY v2 file reads" "$(page_read page_snappy_v2)" "NO ERROR: 100/5050"
check "page: a SNAPPY v1 page shorter than its header's size is refused" \
	"$(page_read page_snappy_v1_long)" "$PAGE_REFUSED"
check "page: a SNAPPY v1 page longer than its header's size is refused" \
	"$(page_read page_snappy_v1_short)" "$PAGE_REFUSED"
check "page: a SNAPPY v2 page whose values are shorter than its header's size is refused" \
	"$(page_read page_snappy_v2_long)" "$PAGE_REFUSED"
# An uncompressed page copies no bytes, so its header's size cannot change what
# is read. pyarrow reads this file, and so does pgColumnar.
check "page: an uncompressed page whose header size differs still reads, as in pyarrow" \
	"$(page_read page_none_v1_long)" "NO ERROR: 100/5050"
# An UNCOMPRESSED v2 page that says is_compressed=true, so its values go through
# the codec. A header size below the page's level bytes changes nothing that is
# read, and pyarrow reads both copies.
check "page: an UNCOMPRESSED v2 page marked compressed reads" \
	"$(page_read page_none_v2_c)" "NO ERROR: 100/5050"
check "page: an UNCOMPRESSED v2 page whose header size is below its level bytes reads, as in pyarrow" \
	"$(page_read page_none_v2_c_short)" "NO ERROR: 100/5050"

# ---- a page header field of the wrong Thrift type (#1935) -------------------
# The page reader read each page header field by its id and never looked at its
# Thrift type, so a corrupt type nibble read without an error. The fuzzer found a
# DataPageHeader encoding field (i32) retyped to BOOL_TRUE and to UUID; a change
# to the page's values then read as a wrong answer, where pyarrow refuses the
# header. pyarrow refuses every copy in the first three checks.
check "page: a data page encoding field of Thrift type BOOL_TRUE is refused" \
	"$(page_read page_enc_bool)" "$PAGE_REFUSED"
check "page: a data page encoding field of Thrift type UUID is refused" \
	"$(page_read page_enc_uuid)" "$PAGE_REFUSED"
# page_sweep PREFIX COUNT FILTER -> "N of COUNT <what>", then each copy's answer
page_sweep() {
	qw "SELECT count(*) || ' of $2 $4' || coalesce(': ' || string_agg(i || '=' || v, ' ' ORDER BY i), '')
	       FROM (SELECT i, pgc_err(format(\$f\$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pgcolumnar.read_parquet(%L) AS t(id bigint)\$f\$,
	                                     '$W/$1' || lpad(i::text, $5, '0') || '.parquet')) AS v
	             FROM generate_series(0, ${2:-0} - 1) AS i) s
	      WHERE $3;"
}
PTSWEEP="$(cat "$W/page_type_sweep.count" 2>/dev/null)"
check "page: every i32 or struct page header field of another Thrift type is refused" \
	"$(page_sweep page_type_sweep_ "$PTSWEEP" "v LIKE 'NO ERROR%'" "read without an error" 4)" \
	"0 of $PTSWEEP read without an error"
# is_compressed is optional, so pyarrow skips it when its type is not bool and
# keeps the default, true. A copy may read, but only with the right answer.
PBSWEEP="$(cat "$W/page_bool_sweep.count" 2>/dev/null)"
check "page: no v2 is_compressed field of another Thrift type reads a wrong answer" \
	"$(page_sweep page_bool_sweep_ "$PBSWEEP" "v LIKE 'NO ERROR%' AND v <> 'NO ERROR: 100/5050'" "read a wrong answer" 2)" \
	"0 of $PBSWEEP read a wrong answer"
check "page: a v2 is_compressed field of type LIST is skipped and reads, as in pyarrow" \
	"$(page_read page_bool_sweep_06)" "NO ERROR: 100/5050"

# ---- string cells that are not valid in the server encoding (#1821) --------
# A database created as UTF8 whatever the cluster's encoding (see the header).
psql_admin "CREATE DATABASE pqh_utf8 TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C';"
U8() { PGC_DB=pqh_utf8 "$@"; }
U8 psql_run "CREATE EXTENSION pgcolumnar;
	CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;
	CREATE FUNCTION pgc_try(q text) RETURNS text LANGUAGE plpgsql AS \$\$
	BEGIN EXECUTE q; RETURN 'NO ERROR';
	EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE || ': ' || SQLERRM; END \$\$;
	CREATE FOREIGN TABLE f_str_dict (id bigint, name text) SERVER pq
		OPTIONS (path '$W/str_dict_bad.parquet');
	CREATE TABLE imp_str (id bigint, name text);
	CREATE TABLE heap_str (id bigint, name text) USING heap;"
check "premise: the string arms run in a UTF8 database" \
	"$(U8 qw "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database();")" "UTF8"

ENC_MSG='22021: invalid byte sequence for encoding "UTF8" in Parquet column "name"'
check "read_parquet PLAIN string not UTF-8 -> 22021 naming the column" \
	"$(U8 sqlstate "SELECT * FROM pgcolumnar.read_parquet('$W/str_plain_bad.parquet') AS t(id bigint, name text)")" \
	"$ENC_MSG: 0xff"
check "read_parquet dictionary string not UTF-8 -> 22021 naming the column" \
	"$(U8 sqlstate "SELECT * FROM pgcolumnar.read_parquet('$W/str_dict_bad.parquet') AS t(id bigint, name text)")" \
	"$ENC_MSG: 0xc0 0xaf"
check "read_parquet string with an embedded NUL -> 22021" \
	"$(U8 sqlstate "SELECT * FROM pgcolumnar.read_parquet('$W/str_nul.parquet') AS t(id bigint, name text)")" \
	"$ENC_MSG: 0x00"
check "varchar output column is checked too -> 22021" \
	"$(U8 sqlstate "SELECT * FROM pgcolumnar.read_parquet('$W/str_plain_bad.parquet') AS t(id bigint, name varchar)")" \
	"$ENC_MSG: 0xff"
check "parquet foreign table string not UTF-8 -> 22021" \
	"$(U8 sqlstate "SELECT * FROM f_str_dict")" "$ENC_MSG: 0xc0 0xaf"
check "import_parquet string not UTF-8 -> 22021" \
	"$(U8 sqlstate "SELECT pgcolumnar.import_parquet('imp_str'::regclass, '$W/str_plain_bad.parquet')")" \
	"$ENC_MSG: 0xff"
check_num "import_parquet stored no row of the invalid file" "$(U8 qw 'SELECT count(*) FROM imp_str;')" "0"
U8 sqlstate "INSERT INTO heap_str SELECT * FROM pgcolumnar.read_parquet('$W/str_nul.parquet') AS t(id bigint, name text)" >/dev/null
check_num "INSERT SELECT read_parquet stored no row of the invalid file" "$(U8 qw 'SELECT count(*) FROM heap_str;')" "0"

# valid multibyte UTF-8, PLAIN and dictionary: the check must not refuse it. The
# octet lengths say the bytes arrived whole (caf\u00e9 is 5 bytes, \u65e5\u672c 6).
check "valid multibyte UTF-8 still reads (PLAIN)" \
	"$(U8 qw "SELECT string_agg(name || ':' || octet_length(name), ' ' ORDER BY id)
	          FROM pgcolumnar.read_parquet('$W/str_ok.parquet') AS t(id bigint, name text);")" \
	"ok:2 caf$(printf '\303\251'):5 $(printf '\346\227\245\346\234\254'):6"
check "valid multibyte UTF-8 still reads (dictionary)" \
	"$(U8 qw "SELECT string_agg(name, ' ' ORDER BY id)
	          FROM pgcolumnar.read_parquet('$W/str_ok_dict.parquet') AS t(id bigint, name text);")" \
	"caf$(printf '\303\251') caf$(printf '\303\251') caf$(printf '\303\251') ok"

# Iceberg: iceberg_scan reads its data files through the same decoder.
ICE_BAD_MD="$(ls "$W"/ice_bad/db/events/metadata/*.metadata.json 2>/dev/null | sort | tail -1)"
check "iceberg_scan data file string not UTF-8 -> 22021 naming the column" \
	"$(U8 sqlstate "SELECT * FROM pgcolumnar.iceberg_scan('$ICE_BAD_MD') AS t(id bigint, region text)")" \
	'22021: invalid byte sequence for encoding "UTF8" in Parquet column "region": 0xff'
# The Iceberg FDW prunes a file by its manifest partition value before opening it.
# A value that is not valid text is undecidable, so the file must be read, not
# pruned: compared as raw bytes, 0xff 0xfe is not 'eu', and the eu rows vanished.
ICE_PART_MD="$(ls "$W"/ice_part/db/events/metadata/*.metadata.json 2>/dev/null | sort | tail -1)"
U8 psql_run "CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg;
	CREATE FOREIGN TABLE ev_part (id bigint, region text, amount int)
		SERVER ice OPTIONS (metadata_path '$ICE_PART_MD');"
check "the patched-manifest table reads all five rows" \
	"$(U8 qw "SELECT string_agg(id || region, ',' ORDER BY id) FROM ev_part;")" "1eu,2eu,3us,4us,5us"
check "iceberg FDW reads, not prunes, a file whose partition value is not valid text" \
	"$(U8 qw "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev_part WHERE region = 'eu';")" "1,2"
check "backend survived the string arms" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary

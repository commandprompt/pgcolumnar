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
# and so is a footer whose row groups do not add up to its num_rows.
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
# rep_verdict SQL DETAIL -> "CLEAN REJECT" when SQL raised with DETAIL, else the
# ERROR and DETAIL lines it raised, or NO ERROR and the rows it returned.
rep_verdict() {
	local out
	out="$(errfull "$1")"
	case "$out" in
		*"$2"*) echo "CLEAN REJECT" ;;
		*ERROR*) printf '%s' "$out" | grep -E '^(ERROR|DETAIL):' | tr '\n' ' ' ;;
		*) echo "NO ERROR: $out" ;;
	esac
}
REP_SUM="SELECT count(*) || '/' || sum(id) FROM pgcolumnar.read_parquet"
REP_HIST='Row group 0 column 0 ("id") has a definition level histogram of 2 entries, but the schema gives it a maximum definition level of 0.'
REP_ENUM='Schema element "id" has repetition_type 7; a valid one is 0 (REQUIRED), 1 (OPTIONAL) or 2 (REPEATED).'
check "premise: the unchanged repetition file reads 3000/4498500" \
	"$(qw "$REP_SUM('$W/rep_base.parquet') AS t(id bigint, name text);")" "3000/4498500"
check "repetition_type REQUIRED over optional pages is refused" \
	"$(rep_verdict "$REP_SUM('$W/rep_required.parquet') AS t(id bigint, name text);" "$REP_HIST")" \
	"CLEAN REJECT"
check "repetition_type that is not 0, 1 or 2 is refused" \
	"$(rep_verdict "$REP_SUM('$W/rep_invalid.parquet') AS t(id bigint, name text);" "$REP_ENUM")" \
	"CLEAN REJECT"
psql_run "CREATE TABLE imp_rep (id bigint, name text);"
check "import_parquet refuses repetition_type REQUIRED over optional pages" \
	"$(rep_verdict "SELECT pgcolumnar.import_parquet('imp_rep'::regclass, '$W/rep_required.parquet');" "$REP_HIST")" \
	"CLEAN REJECT"

# The row groups of rows_short.parquet hold 3000 rows, but its footer says 2000.
REP_ROWS='The row groups hold 3000 rows, but the file declares num_rows 2000.'
check "a footer num_rows the row groups do not add up to is refused" \
	"$(rep_verdict "$REP_SUM('$W/rows_short.parquet') AS t(id bigint, name text);" "$REP_ROWS")" \
	"CLEAN REJECT"

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
check "premise: the patched-manifest table reads all five rows" \
	"$(U8 qw "SELECT string_agg(id || region, ',' ORDER BY id) FROM ev_part;")" "1eu,2eu,3us,4us,5us"
check "iceberg FDW reads, not prunes, a file whose partition value is not valid text" \
	"$(U8 qw "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev_part WHERE region = 'eu';")" "1,2"
check "backend survived the string arms" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary

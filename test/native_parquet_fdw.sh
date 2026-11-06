#!/usr/bin/env bash
#
# pgColumnar Parquet foreign-data wrapper (Phase G). The pgcolumnar_parquet FDW
# exposes a single Parquet file as a foreign table, binding its column definitions
# against the file like read_parquet's column list and producing rows through the
# same scan core. This suite exports a columnar table, reads it back through a
# foreign table, and asserts identity with the source across full scan, projection,
# predicate (executor-applied), self-join (rescan), and a nested-type table. It also
# checks EXPLAIN shows a Foreign Scan and the option validator/scan errors.
#
# Usage:  test/native_parquet_fdw.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

wait_up() { local _i; for _i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && return; sleep 0.5; done; }

# refusal SQL REGEX LABEL -> "<SQLSTATE> (LABEL)" when SQL raised an error whose
# message matches REGEX, "<SQLSTATE>: <message>" for any other error, "no refusal:
# the connection was lost" on a crash, or "no error". These arms used to accept ANY
# psql failure: with the scan's missing-path guard removed the backend crashed, and
# with the short-file guard removed a later guard refused instead, and main passed
# both. Each arm now names its own guard.
refusal_of() {	# stdin: psql output -> the refusal reading
	local re="$1" label="$2" line state msg
	local out; out="$(cat)"
	line="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): /\1|/p' <<<"$out" | head -1)"
	if [ -z "$line" ]; then
		grep -qE 'server closed the connection|connection to server' <<<"$out" \
			&& echo "no refusal: the connection was lost" || echo "no error ($(head -1 <<<"$out"))"
		return
	fi
	state="${line%%|*}"; msg="${line#*|}"
	if grep -qE -- "$re" <<<"$msg"; then echo "$state ($label)"; else echo "$state: $msg"; fi
}
refusal() {
	wait_up
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | refusal_of "$2" "$3"
\\set VERBOSITY verbose
$1;
SQLEOF
}
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
# explain_field PLAN NAME -> the integer after "NAME: ", or "no reading" when absent
explain_field() {
	local v; v="$(sed -n "s/.*$2: \([0-9][0-9]*\).*/\1/p" <<<"$1" | head -1)"
	[ -n "$v" ] && echo "$v" || echo "no reading"
}

psql_run "CREATE SERVER pqsrv FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# ---- scalar foreign table ---------------------------------------------------
SCALAR="$PGC_WORKDIR/scalar.parquet"
scalar_cols="c_i2 int2, c_i4 int4, c_i8 int8, c_f4 float4, c_f8 float8, c_bool bool,
             c_text text, c_bytea bytea, c_date date, c_time time, c_ts timestamp"

# THE ORACLE IS A HEAP TABLE WRITTEN FROM THE GENERATOR, not the columnar table the
# file was exported from: the comparisons then hold the file to the rows that were
# meant, not to whatever the columnar scan returned on the way out.
GEN="SELECT (g%100)::int2, g, g::int8*1000, (g/2.0)::float4, (g/4.0)::float8, (g%2=0),
	       'row'||g, decode(lpad(to_hex(g),8,'0'),'hex'), date '2020-01-01' + g,
	       time '00:00:00' + (g||' seconds')::interval,
	       timestamp '2020-01-01' + (g||' minutes')::interval
	FROM generate_series(1, 5000) g"
psql_run "CREATE TABLE s ($scalar_cols) USING pgcolumnar;"
psql_run "INSERT INTO s $GEN;"
psql_run "INSERT INTO s VALUES (NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);"
psql_run "CREATE TABLE h ($scalar_cols) USING heap;"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO h VALUES (NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);"
check "scalar export rows" "$(q "SELECT pgcolumnar.export_parquet('s', '$SCALAR');")" "5001"

psql_run "CREATE FOREIGN TABLE fs ($scalar_cols) SERVER pqsrv OPTIONS (path '$SCALAR');"

check "full scan: fdw == source" \
	"$(pgc_set_hash "SELECT * FROM fs")" "$(pgc_set_hash "SELECT * FROM h")"
check_num "fdw row count" "$(q "SELECT count(*) FROM fs")" "5001"

# projection (executor discards other columns)
check "projection: fdw == source" \
	"$(pgc_set_hash "SELECT c_text FROM fs")" "$(pgc_set_hash "SELECT c_text FROM h")"

# predicate applied by the executor
check "predicate: fdw == source" \
	"$(pgc_set_hash "SELECT c_i4, c_text FROM fs WHERE c_i4 > 2500 AND c_bool")" \
	"$(pgc_set_hash "SELECT c_i4, c_text FROM h  WHERE c_i4 > 2500 AND c_bool")"

# self-join forces the scan to be re-executed (ReScan); result must match source
psql_run "SET enable_hashjoin=off; SET enable_mergejoin=off;"
check_num "nested-loop self-join (rescan): fdw == source" \
	"$(q "SELECT count(*) FROM fs a JOIN fs b ON a.c_i4 = b.c_i4 WHERE a.c_i4 < 200")" \
	"$(q "SELECT count(*) FROM h  a JOIN h  b ON a.c_i4 = b.c_i4 WHERE a.c_i4 < 200")"
psql_run "RESET enable_hashjoin; RESET enable_mergejoin;"

# the plan is a Foreign Scan
check "EXPLAIN shows Foreign Scan" \
	"$(q "EXPLAIN (COSTS OFF) SELECT * FROM fs" | grep -c 'Foreign Scan')" "1"

# ---- pushdown: projection and row-group skipping ---------------------------
# Both used to go unmeasured: every check compared answers, which an executor
# recheck and a discarding projection keep right, so a scan that decoded every
# column, or skipped no row group, passed. EXPLAIN ANALYZE reports both.
PROJ="$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT c_text FROM fs")"
check "a one-column query reads one of the eleven columns" \
	"$(explain_field "$PROJ" 'Columns Read')/$(explain_field "$PROJ" 'Columns Total')" "1/11"

# 200,000 rows export as four 65536-row groups, ascending in id, so id > 196608
# can only match the last group.
SKF="$PGC_WORKDIR/skip.parquet"
psql_run "CREATE TABLE sk (id int8) USING pgcolumnar;"
psql_run "INSERT INTO sk SELECT g FROM generate_series(1, 200000) g;"
check_num "skip export rows" "$(q "SELECT pgcolumnar.export_parquet('sk', '$SKF');")" "200000"
psql_run "CREATE FOREIGN TABLE fsk (id int8) SERVER pqsrv OPTIONS (path '$SKF');"
SKP="$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM fsk WHERE id > 196608::int8")"
check "the skip file has four row groups" "$(explain_field "$SKP" 'Row Groups')" "4"
check "a selective predicate skips the three row groups it cannot match" \
	"$(explain_field "$SKP" 'Row Groups Skipped')" "3"
# The same query the plan above measured, int8 constant and all: a plain 196608 is an
# int4 constant, a cross-type comparison that skips nothing (#1712), so the count was
# read from a full scan and stayed right whatever skipping did (#1711 review).
check_num "and the skipped scan still returns every matching row" \
	"$(q "SELECT count(*) FROM fsk WHERE id > 196608::int8")" "3392"

# ---- nested foreign table (int[], text[], composite) ------------------------
psql_run "CREATE TYPE fdw_npc AS (x int, y text);"
psql_run "CREATE TABLE np (id int, ia int[], ta text[], c fdw_npc) USING pgcolumnar;"
psql_run "INSERT INTO np
	SELECT g,
	       CASE WHEN g%11=0 THEN NULL WHEN g%7=0 THEN '{}'::int[] ELSE ARRAY[g,g+1] END,
	       CASE WHEN g%13=0 THEN NULL ELSE ARRAY['a'||g,'b'||g] END,
	       CASE WHEN g%5=0 THEN NULL ELSE ROW(g,'c'||g)::fdw_npc END
	FROM generate_series(1, 3000) g;"
NESTED="$PGC_WORKDIR/nested.parquet"
check "nested export rows" "$(q "SELECT pgcolumnar.export_parquet('np', '$NESTED');")" "3000"
psql_run "CREATE FOREIGN TABLE fnp (id int, ia int[], ta text[], c fdw_npc)
	SERVER pqsrv OPTIONS (path '$NESTED');"
check "nested full scan: fdw == source" \
	"$(pgc_set_hash "SELECT * FROM fnp")" "$(pgc_set_hash "SELECT * FROM np")"

# ---- option validation + scan errors ----------------------------------------
CRASHES_BEFORE="$(crashes)"
check "reject invalid table option (HV00D)" \
	"$(refusal "CREATE FOREIGN TABLE fbad ($scalar_cols) SERVER pqsrv OPTIONS (path '$SCALAR', bogus 'x')" \
	   'invalid option "bogus"' 'invalid option')" "HV00D (invalid option)"
check "reject empty path option (HV00D)" \
	"$(refusal "CREATE FOREIGN TABLE fempty ($scalar_cols) SERVER pqsrv OPTIONS (path '')" \
	   '"path" option cannot be empty' 'empty path')" "HV00D (empty path)"

# a foreign table with no path errors at scan time
psql_run "CREATE FOREIGN TABLE fnopath ($scalar_cols) SERVER pqsrv;"
check "scan without path option errors (HV00J)" \
	"$(refusal "SELECT * FROM fnopath" 'foreign table "fnopath" has no "path" option' 'no path')" "HV00J (no path)"

# a path that is not a Parquet file errors at scan time: 11 bytes, too short to be one
wait_up
printf 'not parquet' > "$PGC_WORKDIR/bad.dat"
chmod 644 "$PGC_WORKDIR/bad.dat"
wait_up
psql_run "CREATE FOREIGN TABLE fbadfile ($scalar_cols) SERVER pqsrv OPTIONS (path '$PGC_WORKDIR/bad.dat');"
check "scan of non-parquet file errors (XX001)" \
	"$(refusal "SELECT * FROM fbadfile" 'bad\.dat" is not a Parquet file$' 'too short')" "XX001 (too short)"

# a declared type incompatible with the file column errors at scan time
wait_up
psql_run "CREATE FOREIGN TABLE fwrongtype (c_i2 int2, c_i4 text, c_i8 int8, c_f4 float4,
	c_f8 float8, c_bool bool, c_text text, c_bytea bytea, c_date date, c_time time,
	c_ts timestamp) SERVER pqsrv OPTIONS (path '$SCALAR');"
check "scan with incompatible declared type errors (42804)" \
	"$(refusal "SELECT * FROM fwrongtype" 'physical type is not compatible with target column "c_i4"' 'type mismatch')" "42804 (type mismatch)"

# ---- a declared typmod applies to every value (#1790) -------------------------
# A foreign table's column types carry their typmods, and the scan returns each
# value as that type: numeric(p,s) rounds to scale s or refuses an overflow (22003),
# and varchar(n) refuses a longer value (22001) unless the excess is spaces.
wait_up
psql_run "CREATE TABLE ftm_src (d numeric(10,3), s text) USING pgcolumnar;"
psql_run "INSERT INTO ftm_src VALUES (12345.678, 'hello');"
psql_run "CREATE TABLE ftm_fitsrc (d numeric(10,3), s text) USING pgcolumnar;"
psql_run "INSERT INTO ftm_fitsrc VALUES (1.005, 'ab'), (2.5, 'abc  ');"
FTM="$PGC_WORKDIR/ftm.parquet"
FTM_FIT="$PGC_WORKDIR/ftm_fit.parquet"
q "SELECT pgcolumnar.export_parquet('ftm_src', '$FTM');" >/dev/null
q "SELECT pgcolumnar.export_parquet('ftm_fitsrc', '$FTM_FIT');" >/dev/null
psql_run "CREATE FOREIGN TABLE ftm_num (d numeric(5,2), s text) SERVER pqsrv OPTIONS (path '$FTM');"
psql_run "CREATE FOREIGN TABLE ftm_fit (d numeric(5,2), s text) SERVER pqsrv OPTIONS (path '$FTM_FIT');"
psql_run "CREATE FOREIGN TABLE ftm_str (d numeric(10,3), s varchar(3)) SERVER pqsrv OPTIONS (path '$FTM');"
check "typmod: a numeric(5,2) foreign column refuses 12345.678 (22003)" \
	"$(refusal "SELECT * FROM ftm_num" 'numeric field overflow' 'overflow')" "22003 (overflow)"
check "typmod: a numeric(5,2) foreign column rounds 1.005 and 2.500 to scale 2" \
	"$(q "SELECT string_agg(d::text, ',' ORDER BY d) FROM ftm_fit")" \
	"1.01,2.50"
check "typmod: a varchar(3) foreign column refuses 'hello' (22001)" \
	"$(refusal "SELECT * FROM ftm_str" 'value too long for type character varying\(3\)' 'too long')" \
	"22001 (too long)"

check "backend still up after the refusals" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary

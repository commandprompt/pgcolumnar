#!/usr/bin/env bash
#
# pgColumnar external-Parquet read (Phase G). pgcolumnar.read_parquet(path) streams
# a server-side Parquet file's rows in place, with a caller-supplied column
# definition list. This suite is the round-trip differential: it exports a columnar
# table (scalars, then nested int[]/text[]/composite), reads the file back with
# read_parquet, and asserts the rows are the rows that were generated. It also checks
# the argument-validation errors (no column list, wrong count/type, not Parquet) and
# the function's own pg_read_server_files gate.
#
# THE ORACLE IS THE GENERATOR, NOT THE TABLE (#1341). The source table s is read
# through the columnar scan twice: once by export_parquet, to write the file, and
# once by the old comparison, as "the source". A scan that decoded one value wrong
# (int4 777 read as 778) wrote 778 into the file AND read 778 as the source, so the
# round trip agreed with itself: measured, main passed 15/15 under that mutant. Each
# comparison now hashes the generator's own rows, built in SQL, never through s.
#
# A REFUSAL IS ITS OWN GUARD'S (#1341). Each refusal used to count any psql failure,
# a crash included. Two of them share SQLSTATE 42804 (the leaf count and the physical
# type), so each now requires its SQLSTATE and its own message, waits for the server
# first, and the refusals end with a survival check that reads the server log's count
# of killed backends.
#
# Usage:  test/native_read_parquet.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Backends killed by a signal, from the server log: a number, or nothing when the log
# can't be read, so that a blind count never reads as "no crashes".
rp_crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
# wait (bounded) until the server answers again, so a crash in one arm fails only
# that arm and the survival check, not every later reading
rp_wait_up() {
	local _i
	for _i in $(seq 1 40); do
		[ "$(q "SELECT 1;" 2>/dev/null)" = 1 ] && return 0
		sleep 0.5
	done
	return 1
}
# refusal_of SQL PHRASE LABEL -> "<SQLSTATE><LABEL>" when SQL raised with PHRASE in its
# message, the bare SQLSTATE when the message lacks it, or what happened instead.
refusal_of() {
	local out state
	rp_wait_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	if [ -z "$state" ]; then echo "no error ($(head -1 <<<"$out"))"
	elif grep -qF "$2" <<<"$out"; then echo "$state$3"
	else echo "$state"; fi
}
# every reading waits for the server first, so a crash fails only its own reading
rp_q() { rp_wait_up; q "$1"; }
rp_hash() { rp_wait_up; pgc_set_hash "$1"; }
RP_CRASHES_BEFORE="$(rp_crashes)"

# ---- scalar round-trip ------------------------------------------------------
# The shared decoder supports the pq_want_phys type set (int/float/bool/text/bytea/
# date/time/timestamp). uuid and numeric(DECIMAL) are reported by parquet_schema but
# not yet decodable by import/read_parquet -- a shared-decoder follow-up.
SCALAR="$PGC_WORKDIR/scalar.parquet"
scalar_cols="c_i2 int2, c_i4 int4, c_i8 int8, c_f4 float4, c_f8 float8, c_bool bool,
             c_text text, c_bytea bytea, c_date date, c_time time, c_ts timestamp"
# the generator: 5000 rows, then an all-NULL row so nullability round-trips
GEN_SCALAR="SELECT (g%100)::int2, g, g::int8*1000, (g/2.0)::float4, (g/4.0)::float8, (g%2=0),
	       'row'||g, decode(lpad(to_hex(g),8,'0'),'hex'), date '2020-01-01' + g,
	       time '00:00:00' + (g||' seconds')::interval,
	       timestamp '2020-01-01' + (g||' minutes')::interval
	FROM generate_series(1, 5000) g
	UNION ALL SELECT NULL::int2, NULL::int4, NULL::int8, NULL::float4, NULL::float8,
	       NULL::bool, NULL::text, NULL::bytea, NULL::date, NULL::time, NULL::timestamp"

psql_run "CREATE TABLE s ($scalar_cols) USING pgcolumnar;"
psql_run "INSERT INTO s $GEN_SCALAR;"

check "scalar export rows" "$(rp_q "SELECT pgcolumnar.export_parquet('s', '$SCALAR');")" "5001"

# read the file back and hash-compare (order-independent) against the generated rows
rp_scalar() { rp_hash "SELECT * FROM pgcolumnar.read_parquet('$SCALAR')
	AS t($scalar_cols)"; }
check "scalar round-trip: read_parquet == the generated rows" \
	"$(rp_scalar)" "$(rp_hash "$GEN_SCALAR")"
check "scalar read_parquet row count" \
	"$(rp_q "SELECT count(*) FROM pgcolumnar.read_parquet('$SCALAR') AS t($scalar_cols)")" "5001"

# a projection over the function still returns the projected column faithfully
check "projected column round-trips" \
	"$(rp_hash "SELECT c_text FROM pgcolumnar.read_parquet('$SCALAR') AS t($scalar_cols)")" \
	"$(rp_hash "SELECT 'row'||g FROM generate_series(1, 5000) g UNION ALL SELECT NULL::text")"

# ---- nested round-trip (int[], text[], composite) ---------------------------
psql_run "CREATE TYPE rp_npc AS (x int, y text);"
GEN_NESTED="SELECT g,
	       CASE WHEN g%11=0 THEN NULL WHEN g%7=0 THEN '{}'::int[]
	            ELSE ARRAY[g, g+1, g+2] END,
	       CASE WHEN g%13=0 THEN NULL ELSE ARRAY['a'||g, 'b'||g] END,
	       CASE WHEN g%5=0 THEN NULL ELSE ROW(g, 'c'||g)::rp_npc END
	FROM generate_series(1, 3000) g"
psql_run "CREATE TABLE np (id int, ia int[], ta text[], c rp_npc) USING pgcolumnar;"
psql_run "INSERT INTO np $GEN_NESTED;"
NESTED="$PGC_WORKDIR/nested.parquet"
check "nested export rows" "$(rp_q "SELECT pgcolumnar.export_parquet('np', '$NESTED');")" "3000"

nested_cols="id int, ia int[], ta text[], c rp_npc"
check "nested round-trip: read_parquet == the generated rows" \
	"$(rp_hash "SELECT * FROM pgcolumnar.read_parquet('$NESTED') AS t($nested_cols)")" \
	"$(rp_hash "$GEN_NESTED")"

# ---- argument validation ----------------------------------------------------
# no column definition list: the record-returning function has no shape. The parser
# refuses this before read_parquet runs (its own guard is unreachable from SQL), so
# this pins PostgreSQL's 42601, not a pgColumnar guard.
check "premise: reject missing column definition list" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$SCALAR')" \
	   "a column definition list is required" " (no column list)")" \
	"42601 (no column list)"

# too few columns declared (the file has 11 leaves)
check "reject fewer columns than file" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$SCALAR') AS t(c_i2 int2)" \
	   "Parquet file has 11 leaf columns, target table expands to 1" " by the leaf-count guard")" \
	"42804 by the leaf-count guard"

# incompatible declared type (c_i4 is INT32, not text)
check "reject incompatible declared type" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$SCALAR')
	 AS t(c_i2 int2, c_i4 text, c_i8 int8, c_f4 float4, c_f8 float8, c_bool bool,
	      c_text text, c_bytea bytea, c_date date, c_time time, c_ts timestamp)" \
	   "physical type is not compatible with target column \"c_i4\"" " by the physical-type guard")" \
	"42804 by the physical-type guard"

# not a Parquet file
printf 'not parquet' > "$PGC_WORKDIR/bad.dat"
chmod 644 "$PGC_WORKDIR/bad.dat"
check "reject non-parquet file" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$PGC_WORKDIR/bad.dat') AS t(a int)" \
	   "is not a Parquet file" " by the magic check")" \
	"XX001 by the magic check"

# ---- the function's own file gate ---------------------------------------------
# The role holds USAGE on the schema, so what refuses it is read_parquet's own
# pg_read_server_files check, not the schema (which would also say 42501).
psql_run "DROP ROLE IF EXISTS t_rp_none;"
psql_run "DROP ROLE IF EXISTS t_rp_files;"
psql_run "CREATE ROLE t_rp_none NOSUPERUSER LOGIN;"
psql_run "CREATE ROLE t_rp_files NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_rp_none, t_rp_files;"
psql_run "GRANT pg_read_server_files TO t_rp_files;"
check "a role without pg_read_server_files is refused by read_parquet's own gate" \
	"$(refusal_of "SET ROLE t_rp_none; SELECT count(*) FROM pgcolumnar.read_parquet('$SCALAR') AS t($scalar_cols)" \
	   "must be superuser or a member of the pg_read_server_files role to read a server file" \
	   " by the file gate")" \
	"42501 by the file gate"
check_num "and a member of pg_read_server_files reads every row" \
	"$(rp_q "SET ROLE t_rp_files; SELECT count(*) FROM pgcolumnar.read_parquet('$SCALAR') AS t($scalar_cols);" | tail -1)" "5001"

# Answering a query is not enough: a fresh connection answers again once crash recovery
# has finished, so the check also requires that no backend has died since the start.
rp_wait_up
rp_up="$(rp_q "SELECT 'alive';")"
rp_now="$(rp_crashes)"
check "backend still up after the refusals" \
	"$(if ! [[ "$RP_CRASHES_BEFORE" =~ ^[0-9]+$ && "$rp_now" =~ ^[0-9]+$ ]]; then
		echo "no (crash count unreadable)"
	elif [ "$rp_up" = alive ] && [ "$rp_now" = "$RP_CRASHES_BEFORE" ]; then
		echo alive
	else
		echo "no ([$rp_up], $(( rp_now - RP_CRASHES_BEFORE )) backend crashes)"
	fi)" "alive"

# ---- import parity: read_parquet == import_parquet of the same file ---------
psql_run "CREATE TABLE imp ($scalar_cols) USING pgcolumnar;"
check "import parity rows" "$(rp_q "SELECT pgcolumnar.import_parquet('imp', '$SCALAR');")" "5001"
check "read_parquet == import_parquet" \
	"$(rp_scalar)" "$(rp_hash "SELECT * FROM imp")"

# ---- a declared typmod applies to every value (#1790) -------------------------
# read_parquet returns each value as the declared column type, typmod included, as
# PostgreSQL's input functions do for json_to_record: numeric(p,s) rounds to scale s
# and refuses an overflow (22003), and varchar(n) refuses a longer value (22001)
# unless the excess is spaces, which it drops. CREATE TABLE AS and INSERT ... SELECT
# take the function's typmod as already applied, so nothing else checks it.
psql_run "CREATE TABLE tm_big (d numeric(10,3), s text) USING pgcolumnar;"
psql_run "INSERT INTO tm_big VALUES (12345.678, 'hello'), (1.005, 'ab');"
psql_run "CREATE TABLE tm_fit (d numeric(10,3), s text) USING pgcolumnar;"
psql_run "INSERT INTO tm_fit VALUES (1.005, 'ab'), (2.5, 'abc  ');"
TM_BIG="$PGC_WORKDIR/tm_big.parquet"
TM_FIT="$PGC_WORKDIR/tm_fit.parquet"
rp_q "SELECT pgcolumnar.export_parquet('tm_big', '$TM_BIG');" >/dev/null
rp_q "SELECT pgcolumnar.export_parquet('tm_fit', '$TM_FIT');" >/dev/null
check "typmod: a declared typmod that holds every value reads them unchanged" \
	"$(rp_q "SELECT string_agg(d || '|' || s, ',' ORDER BY d)
	   FROM pgcolumnar.read_parquet('$TM_BIG') AS x(d numeric(10,3), s varchar(5));")" \
	"1.005|ab,12345.678|hello"
check "typmod: numeric(5,2) refuses 12345.678 with numeric field overflow" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$TM_BIG') AS x(d numeric(5,2), s text)" \
	   "numeric field overflow" " (overflow)")" \
	"22003 (overflow)"
check "typmod: numeric(5,2) rounds 1.005 and 2.500 to the declared scale" \
	"$(rp_q "SELECT string_agg(d::text, ',' ORDER BY d)
	   FROM pgcolumnar.read_parquet('$TM_FIT') AS x(d numeric(5,2), s text);")" \
	"1.01,2.50"
check "typmod: varchar(3) refuses 'hello' with value too long" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$TM_BIG') AS x(d numeric(10,3), s varchar(3))" \
	   "value too long for type character varying(3)" " (too long)")" \
	"22001 (too long)"
check "typmod: varchar(3) drops the excess trailing spaces of 'abc  '" \
	"$(rp_q "SELECT string_agg(s || ':' || octet_length(s), ',' ORDER BY s)
	   FROM pgcolumnar.read_parquet('$TM_FIT') AS x(d numeric(10,3), s varchar(3));")" \
	"ab:2,abc:3"
check "typmod: CREATE TABLE AS of an overflowing value fails with numeric field overflow" \
	"$(refusal_of "CREATE TABLE tm_ctas AS SELECT * FROM pgcolumnar.read_parquet('$TM_BIG')
	   AS x(d numeric(5,2), s varchar(3))" "numeric field overflow" " (overflow)")" \
	"22003 (overflow)"
check "typmod: the failed CREATE TABLE AS left no table" \
	"$(rp_q "SELECT coalesce(to_regclass('tm_ctas')::text, 'none');")" "none"
psql_run "CREATE TABLE tm_heap (d numeric(5,2), s varchar(3));"
psql_run "INSERT INTO tm_heap SELECT * FROM pgcolumnar.read_parquet('$TM_FIT')
          AS x(d numeric(5,2), s varchar(3));"
check "typmod: INSERT ... SELECT into a numeric(5,2) heap table stores the rounded values" \
	"$(rp_q "SELECT string_agg(d || '|' || s, ',' ORDER BY d) FROM tm_heap;")" \
	"1.01|ab,2.50|abc"
psql_run "CREATE TABLE tm_imp (d numeric(5,2), s varchar(3)) USING pgcolumnar;"
rp_q "SELECT pgcolumnar.import_parquet('tm_imp', '$TM_FIT');" >/dev/null
check "typmod: import_parquet into a numeric(5,2) table stores the rounded values" \
	"$(rp_q "SELECT string_agg(d || '|' || s, ',' ORDER BY d) FROM tm_imp;")" \
	"1.01|ab,2.50|abc"
# the nested file's ta holds 'a1' .. 'a3000', and c.y holds 'c1' .. 'c3000'
check "typmod: an array element typmod applies, varchar(3)[] refuses 'a1000'" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$NESTED')
	   AS t(id int, ia int[], ta varchar(3)[], c rp_npc)" \
	   "value too long for type character varying(3)" " (too long)")" \
	"22001 (too long)"
psql_run "CREATE TYPE rp_npc3 AS (x int, y varchar(3));"
check "typmod: a composite field typmod applies, y varchar(3) refuses 'c1000'" \
	"$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$NESTED')
	   AS t(id int, ia int[], ta text[], c rp_npc3)" \
	   "value too long for type character varying(3)" " (too long)")" \
	"22001 (too long)"

pgc_summary

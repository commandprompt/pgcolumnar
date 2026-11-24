#!/usr/bin/env bash
#
# pgColumnar nested-type Parquet import round-trip (gap 27): export a table with
# array and composite columns to Parquet, import it back into a fresh table, and
# assert the two tables are identical (arrays reconstructed from the LIST
# repetition/definition levels, composites from the group field leaves, including
# NULL arrays/elements/structs and empty arrays). A second case imports a nested
# Parquet file written by pyarrow, to prove interop beyond our own writer.
#
# Usage:  test/parquet_nested_import.sh [PG_CONFIG]
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

PF="$PGC_WORKDIR/ni.parquet"
psql_run "CREATE TYPE pnic AS (x int, y text);"
psql_run "CREATE TABLE pna (id int, ia int[], ta text[], c pnic) USING pgcolumnar;"
psql_run "INSERT INTO pna SELECT g,
    CASE WHEN g % 10 = 0 THEN NULL
         WHEN g % 7 = 0 THEN '{}'::int[]
         ELSE ARRAY[g, NULL, g + 3] END,
    ARRAY['a' || g, 'b' || g],
    CASE WHEN g % 5 = 0 THEN NULL ELSE ROW(g * 2, 'c' || g)::pnic END
  FROM generate_series(1, 5000) g;"

check "export rows" "$(q "SELECT pgcolumnar.export_parquet('pna', '$PF');")" "5000"
psql_run "CREATE TABLE pna2 (id int, ia int[], ta text[], c pnic) USING pgcolumnar;"
check "import rows" "$(q "SELECT pgcolumnar.import_parquet('pna2', '$PF');")" "5000"

# the reconstructed table must equal the original (canonical text of every column)
SEL="SELECT id, ia::text, ta::text, c::text FROM"
check "nested import round-trips (arrays + composite)" \
	"$(pgc_set_hash "$SEL pna2")" \
	"$(pgc_set_hash "$SEL pna")"
# check_num: under check two failed counts were both empty and agreed, so with both
# tables missing this passed (#1341).
check_num "import count matches" "$(q 'SELECT count(*) FROM pna2;')" "$(q 'SELECT count(*) FROM pna;')"

# after a delete on the source, a fresh export+import still round-trips the live set
psql_run "DELETE FROM pna WHERE id BETWEEN 1000 AND 2000;"
psql_run "SELECT pgcolumnar.export_parquet('pna', '$PF');"
psql_run "TRUNCATE pna2;"
psql_run "SELECT pgcolumnar.import_parquet('pna2', '$PF');"
check "round-trips after delete" \
	"$(pgc_set_hash "$SEL pna2")" \
	"$(pgc_set_hash "$SEL pna")"

# ---------------------------------------------------------------------------
# WHY NOT `pgc_skip`, which 25 pyarrow suites use (#1215). It is terminal, and
# only two of these seven arms need pyarrow. Measured on pg18a:
#
#     with pyarrow     7 passed + 0 skipped = 7
#     without pyarrow  5 passed + 2 skipped = 7
#
# Same name set both ways, 7 records either way, and rc=0 without it. The two
# that need the reference writer decline by name below. #2079 added four more
# pyarrow arms at the end, which decline the same way. Measured on PG 18.6:
#
#     with pyarrow     11 passed + 0 skipped = 11
#     without pyarrow   5 passed + 6 skipped = 11
#
# The verifier's review of #2146 added four more, which decline the same way:
#
#     with pyarrow     15 passed + 0 skipped = 15
#     without pyarrow   5 passed + 10 skipped = 15
# pyarrow-written nested Parquet file: list<int32> and struct<x:int,y:string>.
# This proves we read the Dremel levels the reference writer produces, not just
# our own exporter's byte layout.
# ---------------------------------------------------------------------------
PYF="$PGC_WORKDIR/pyarrow_nested.parquet"
if python3 - "$PYF" <<'PY'
import sys
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception:
    sys.exit(3)
ids = list(range(1, 201))
ia = [None if g % 10 == 0 else ([] if g % 7 == 0 else [g, None, g + 3]) for g in ids]
c = [None if g % 5 == 0 else {"x": g * 2, "y": "c" + str(g)} for g in ids]
t = pa.table({
    "id": pa.array(ids, pa.int32()),
    "ia": pa.array(ia, pa.list_(pa.int32())),
    "c":  pa.array(c, pa.struct([("x", pa.int32()), ("y", pa.string())])),
})
pq.write_table(t, sys.argv[1])
PY
then
	psql_run "CREATE TABLE pna3 (id int, ia int[], c pnic) USING pgcolumnar;"
	check "pyarrow import rows" "$(q "SELECT pgcolumnar.import_parquet('pna3', '$PYF');")" "200"
	# build the same expected set in the heap oracle for comparison
	psql_run "CREATE TABLE pna3_o (id int, ia int[], c pnic);"
	psql_run "INSERT INTO pna3_o SELECT g,
	    CASE WHEN g % 10 = 0 THEN NULL
	         WHEN g % 7 = 0 THEN '{}'::int[]
	         ELSE ARRAY[g, NULL, g + 3] END,
	    CASE WHEN g % 5 = 0 THEN NULL ELSE ROW(g * 2, 'c' || g)::pnic END
	  FROM generate_series(1, 200) g;"
	SEL3="SELECT id, ia::text, c::text FROM"
	check "pyarrow nested file matches oracle" \
		"$(pgc_set_hash "$SEL3 pna3")" \
		"$(pgc_set_hash "$SEL3 pna3_o")"
else
	rc=$?
	if [ "$rc" = 3 ]; then
		# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#1185); a single
		# summary name leaves both of these out of the recorded set.
		for _pni_arm in "pyarrow import rows" "pyarrow nested file matches oracle"
		do
			check_skip "$_pni_arm" \
				"SKIP: $_pni_arm (pyarrow not available for the reference writer)" \
				"pyarrow not available"
		done
	else
		echo "FAIL: pyarrow nested file generation errored (rc=$rc)"
		PGC_FAIL=1
	fi
fi

# ---------------------------------------------------------------------------
# Definition levels that depend on the schema shape (#2079, #1916). The reader
# used fixed levels: def 0 was a NULL list or struct and def 1 an empty list.
# That holds only when the list or struct is OPTIONAL with no OPTIONAL parent.
# A REQUIRED list, a REQUIRED struct, or a list inside a nullable struct moves
# the levels, and the reader returned wrong values with no error. pyarrow wrote
# both files; each check compares with the values pyarrow reads back.
# ---------------------------------------------------------------------------
LVL_STL="$PGC_WORKDIR/lvl_stl.parquet"
LVL_REQ="$PGC_WORKDIR/lvl_req.parquet"
LVL_SUB="$PGC_WORKDIR/lvl_sub.parquet"
LVL_FLAT="$PGC_WORKDIR/lvl_flat.parquet"
LVL_ARMS=("list in a nullable struct reads like pyarrow"
	"required lists and struct read like pyarrow"
	"required lists and struct import like pyarrow"
	"required lists and struct read like pyarrow through the FDW"
	"list in a nullable struct imports like pyarrow"
	"list in a nullable struct reads like pyarrow through the FDW"
	"a NULL sub-struct keeps its sibling field"
	"a composite over top-level columns is refused")
# refused SQL -> "refused: not inside one struct", "succeeded", or the other error.
lvl_refusal() {
	local out
	if out="$(psql_run "$1" 2>&1)"; then
		echo "succeeded"
		return
	fi
	case "$out" in
	*"binds Parquet columns that are not inside one struct"*) echo "refused: not inside one struct" ;;
	*) echo "other error: $(printf '%s' "$out" | head -n 1)" ;;
	esac
}
if python3 - "$LVL_STL" "$LVL_REQ" "$LVL_SUB" "$LVL_FLAT" <<'PY'
import sys
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception:
    sys.exit(3)
stl = pa.array([{"a": 1, "l": [1, 2]}, {"a": None, "l": None},
                {"a": 2, "l": []}, {"a": 3, "l": [None]}],
               pa.struct([("a", pa.int32()), ("l", pa.list_(pa.int64()))]))
pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3], pa.int32()), "stl": stl}), sys.argv[1])
s = pa.schema([
    pa.field("id", pa.int32(), nullable=False),
    pa.field("rl", pa.list_(pa.field("item", pa.int32(), nullable=True)), nullable=False),
    pa.field("rlr", pa.list_(pa.field("item", pa.int32(), nullable=False)), nullable=False),
    pa.field("s", pa.struct([pa.field("a", pa.int32()), pa.field("b", pa.string())]), nullable=False),
])
pq.write_table(pa.Table.from_pylist([
    {"id": 0, "rl": [], "rlr": [], "s": {"a": None, "b": "x"}},
    {"id": 1, "rl": [None], "rlr": [5], "s": {"a": 5, "b": "y"}},
    {"id": 2, "rl": [1, None], "rlr": [1, 2], "s": {"a": None, "b": None}},
], schema=s), sys.argv[2])
# A struct holding a field and a nullable sub-struct. A composite (a, b) binds
# s.a and s.inner.b, two leaves at different depths. A NULL inner must leave a.
sub = pa.array([{"a": 1, "inner": {"b": 2}}, {"a": 3, "inner": None}, None,
                {"a": None, "inner": {"b": 4}}],
               pa.struct([("a", pa.int32()), ("inner", pa.struct([("b", pa.int32())]))]))
pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3], pa.int32()), "s": sub}), sys.argv[3])
# Flat top-level columns only: the file holds no struct for a composite.
pq.write_table(pa.table({"id": pa.array([0, 1], pa.int32()),
                         "a": pa.array([1, None], pa.int32()),
                         "b": pa.array([None, 2], pa.int32())}), sys.argv[4])
PY
then
	psql_run "CREATE TYPE lvl_ab AS (a int, b text);"
	# One line per file: each row as id:value, NULL spelled out.
	REQ_ROWS="string_agg(id || ':' || coalesce(rl::text, 'NULL') || ':' || coalesce(rlr::text, 'NULL') || ':' || coalesce(s::text, 'NULL'), ' ' ORDER BY id)"
	REQ_WANT='0:{}:{}:(,x) 1:{NULL}:{5}:(5,y) 2:{1,NULL}:{1,2}:(,)'
	check "list in a nullable struct reads like pyarrow" \
		"$(q "SELECT string_agg(id || ':' || coalesce(sa::text, 'NULL') || ':' || coalesce(sl::text, 'NULL'), ' ' ORDER BY id)
		      FROM pgcolumnar.read_parquet('$LVL_STL') AS t(id int, sa int, sl bigint[]);")" \
		'0:1:{1,2} 1:NULL:NULL 2:2:{} 3:3:{NULL}'
	check "required lists and struct read like pyarrow" \
		"$(q "SELECT $REQ_ROWS FROM pgcolumnar.read_parquet('$LVL_REQ') AS t(id int, rl int[], rlr int[], s lvl_ab);")" \
		"$REQ_WANT"
	psql_run "CREATE TABLE lvl_heap (id int, rl int[], rlr int[], s lvl_ab);"
	psql_run "SELECT pgcolumnar.import_parquet('lvl_heap', '$LVL_REQ');"
	check "required lists and struct import like pyarrow" \
		"$(q "SELECT $REQ_ROWS FROM lvl_heap;")" "$REQ_WANT"
	psql_run "CREATE SERVER lvl_srv FOREIGN DATA WRAPPER pgcolumnar_parquet;"
	psql_run "CREATE FOREIGN TABLE lvl_f (id int, rl int[], rlr int[], s lvl_ab)
		SERVER lvl_srv OPTIONS (path '$LVL_REQ');"
	check "required lists and struct read like pyarrow through the FDW" \
		"$(q "SELECT $REQ_ROWS FROM lvl_f;")" "$REQ_WANT"
	# The list inside a nullable struct, through the other two read paths.
	STL_ROWS="string_agg(id || ':' || coalesce(sa::text, 'NULL') || ':' || coalesce(sl::text, 'NULL'), ' ' ORDER BY id)"
	psql_run "CREATE TABLE lvl_stl_heap (id int, sa int, sl bigint[]);"
	psql_run "SELECT pgcolumnar.import_parquet('lvl_stl_heap', '$LVL_STL');"
	check "list in a nullable struct imports like pyarrow" \
		"$(q "SELECT $STL_ROWS FROM lvl_stl_heap;")" \
		'0:1:{1,2} 1:NULL:NULL 2:2:{} 3:3:{NULL}'
	psql_run "CREATE FOREIGN TABLE lvl_stl_f (id int, sa int, sl bigint[])
		SERVER lvl_srv OPTIONS (path '$LVL_STL');"
	check "list in a nullable struct reads like pyarrow through the FDW" \
		"$(q "SELECT $STL_ROWS FROM lvl_stl_f;")" \
		'0:1:{1,2} 1:NULL:NULL 2:2:{} 3:3:{NULL}'
	# A composite whose fields sit at different depths of one struct. The struct
	# is NULL only when s is; a NULL inner is a NULL field b, not a NULL row.
	psql_run "CREATE TYPE lvl_ii AS (a int, b int);"
	check "a NULL sub-struct keeps its sibling field" \
		"$(q "SELECT string_agg(id || ':' || coalesce(s::text, 'NULL'), ' ' ORDER BY id)
		      FROM pgcolumnar.read_parquet('$LVL_SUB') AS t(id int, s lvl_ii);")" \
		'0:(1,2) 1:(3,) 2:NULL 3:(,4)'
	# A composite over two top-level columns stands for no struct in the file, so
	# the reader cannot tell when it is NULL. It must refuse, not guess.
	check "a composite over top-level columns is refused" \
		"$(lvl_refusal "SELECT * FROM pgcolumnar.read_parquet('$LVL_FLAT') AS t(id int, c lvl_ii);")" \
		"refused: not inside one struct"
else
	rc=$?
	if [ "$rc" = 3 ]; then
		for _lvl_arm in "${LVL_ARMS[@]}"
		do
			check_skip "$_lvl_arm" \
				"SKIP: $_lvl_arm (pyarrow not available for the reference writer)" \
				"pyarrow not available"
		done
	else
		echo "FAIL: pyarrow definition-level file generation errored (rc=$rc)"
		PGC_FAIL=1
	fi
fi

pgc_summary

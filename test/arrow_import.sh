#!/usr/bin/env bash
#
# pgColumnar Arrow IPC import suite.
#
# pgcolumnar.import_arrow(rel, path) inserts the rows of an Arrow IPC stream file
# into an existing columnar table, the reverse of pgcolumnar.export_arrow. This
# suite covers three things:
#   1. Round trip: export a mixed-type columnar table, import it into a fresh
#      columnar table, and assert the two are identical (differential oracle).
#   2. Foreign file: read a file written by pyarrow (not by pgColumnar) and
#      assert the values arrive correctly.
#   3. The documented lossy mappings (non-finite date/timestamp and NaN numeric
#      export as null) and the error cases.
#
# Requires pyarrow to build the foreign file and to cross-check; if it is not
# importable the suite skips with a note.
#
# Usage:  test/arrow_import.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Run one statement under a HARD wall-clock cap. A cancel-resistant open (a FIFO)
# cannot be ended by statement_timeout, so the external timeout is the only
# reliable detector. Prints the 5-char SQLSTATE, or HANG when psql was KILLed.
sqlstate_or_hang() {
	local out rc
	out="$(timeout -s KILL 5 env PATH="$PGC_BINDIR:$PATH" psql \
		-h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY sqlstate
$1;
SQLEOF
)"
	rc=$?
	if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then echo HANG; return; fi
	printf '%s\n' "$out" | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
fifo_release() { exec 9<>"$1" 2>/dev/null; exec 9>&- 2>/dev/null; }

have_pyarrow=1
python3 -c 'import pyarrow' 2>/dev/null || have_pyarrow=0

expect_error() {
	local label="$1" sql="$2"
	if psql_run "$sql" >/dev/null 2>&1; then
		check "$label (expected error)" "succeeded" "error"
	else
		check "$label" "error" "error"
	fi
}

ARROW="$PGC_WORKDIR/rt.arrows"

# --- 1. round trip through pgColumnar's own writer ---------------------------
echo "-- round trip: export then import"
# 16 columns: the maximum pgcolumnar.export_arrow supports.
psql_run "CREATE TABLE ri_src (id int, c int8, d float4, e float8, f bool,
          g text, h bytea, dt date, tm time, ts timestamp, tz timestamptz,
          u uuid, num numeric(20,4), numun numeric, j json, jb jsonb) USING pgcolumnar;"
psql_run "INSERT INTO ri_src
  SELECT s, s::int8*7, (s::float4/3), (s::float8/9), (s%2=0),
         'row'||s, decode(lpad(to_hex(s),4,'0'),'hex'),
         DATE '2000-01-01' + s, TIME '00:00:00' + (s || ' seconds')::interval,
         TIMESTAMP '2001-01-01' + (s||' minutes')::interval,
         TIMESTAMPTZ '2001-01-01 00:00:00+00' + (s||' minutes')::interval,
         ('00000000-0000-0000-0000-' || lpad(to_hex(s),12,'0'))::uuid,
         (s::numeric/100)::numeric(20,4), (s::numeric/7),
         json_build_object('n', s), jsonb_build_object('n', s)
  FROM generate_series(1, 3000) s;"
# a few explicit NULL rows and float NaN/Inf (these round trip exactly)
psql_run "INSERT INTO ri_src (id, d, e) VALUES
  (900001, NULL, NULL),
  (900002, 'NaN'::float4, 'Infinity'::float8),
  (900003, '-Infinity'::float4, 'NaN'::float8);"

src_rows="$(q "SELECT count(*) FROM ri_src;")"
w="$(q "SELECT pgcolumnar.export_arrow('ri_src', '$ARROW');")"
check "export rows" "$w" "$src_rows"

psql_run "CREATE TABLE ri_dst (LIKE ri_src) USING pgcolumnar;"
n="$(q "SELECT pgcolumnar.import_arrow('ri_dst', '$ARROW');")"
check "import rows" "$n" "$src_rows"

h_src="$(pgc_set_hash "SELECT * FROM ri_src")"
h_dst="$(pgc_set_hash "SELECT * FROM ri_dst")"
check "round trip identical" "$h_dst" "$h_src"

# --- 2. a file written by pyarrow (foreign producer) ------------------------
if [ "$have_pyarrow" = 1 ]; then
	echo "-- import a pyarrow-written file"
	PYF="$PGC_WORKDIR/foreign.arrows"
	python3 - "$PYF" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
t = pa.table({
    'a': pa.array([1, 2, None, 4], pa.int64()),
    'b': pa.array([1.5, None, 3.5, 4.5], pa.float64()),
    'c': pa.array(['x', 'y', 'z', None], pa.string()),
})
with ipc.new_stream(pa.OSFile(sys.argv[1], 'wb'), t.schema) as w:
    w.write_table(t)
PY
	psql_run "CREATE TABLE ri_for (a bigint, b float8, c text) USING pgcolumnar;"
	nf="$(q "SELECT pgcolumnar.import_arrow('ri_for', '$PYF');")"
	check "pyarrow import rows" "$nf" "4"
	vals="$(q "SELECT string_agg(coalesce(a::text,'-')||'/'||coalesce(b::text,'-')||'/'||coalesce(c,'-'), ',' ORDER BY a NULLS LAST) FROM ri_for;")"
	check "pyarrow values" "$vals" "1/1.5/x,2/-/y,4/4.5/-,-/3.5/z"
else
	echo "-- pyarrow not available; skipping foreign-file import"
fi

# --- 3. documented lossy mapping: non-finite -> null ------------------------
echo "-- non-finite date/timestamp and NaN numeric import as null"
LOSSY="$PGC_WORKDIR/lossy.arrows"
psql_run "CREATE TABLE ri_nf (id int, dt date, ts timestamp, num numeric(10,2)) USING pgcolumnar;"
psql_run "INSERT INTO ri_nf VALUES (1, 'infinity', '-infinity', 'NaN'), (2, '2020-01-01', '2020-01-01 00:00', 1.25);"
psql_run "SELECT pgcolumnar.export_arrow('ri_nf', '$LOSSY');"
psql_run "CREATE TABLE ri_nf2 (LIKE ri_nf) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.import_arrow('ri_nf2', '$LOSSY');"
nulls="$(q "SELECT count(*) FROM ri_nf2 WHERE id=1 AND dt IS NULL AND ts IS NULL AND num IS NULL;")"
check "non-finite imported as null" "$nulls" "1"
keep="$(q "SELECT count(*) FROM ri_nf2 WHERE id=2 AND dt='2020-01-01' AND num=1.25;")"
check "finite row preserved" "$keep" "1"

# --- 4. error cases ---------------------------------------------------------
echo "-- argument validation"
psql_run "CREATE TABLE ri_heap (a bigint, b float8, c text) USING heap;"
expect_error "reject non-columnar target" "SELECT pgcolumnar.import_arrow('ri_heap', '$ARROW');"
psql_run "CREATE TABLE ri_wrong (a int) USING pgcolumnar;"
expect_error "reject column-count mismatch" "SELECT pgcolumnar.import_arrow('ri_wrong', '$ARROW');"

if [ "$have_pyarrow" = 1 ]; then
	DICTF="$PGC_WORKDIR/dict.arrows"
	python3 - "$DICTF" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
arr = pa.array(['a', 'b', 'a', 'b']).dictionary_encode()
t = pa.table({'c': arr})
with ipc.new_stream(pa.OSFile(sys.argv[1], 'wb'), t.schema) as w:
    w.write_table(t)
PY
	psql_run "CREATE TABLE ri_d (c text) USING pgcolumnar;"
	expect_error "reject dictionary-encoded file" "SELECT pgcolumnar.import_arrow('ri_d', '$DICTF');"

	# Arrow temporal values span the integer carrier's full range, while
	# PostgreSQL's date, time, and timestamp types have narrower valid ranges.
	# Import must reject those values instead of overflowing or storing an
	# invalid internal Datum.
	python3 - "$PGC_WORKDIR" <<'PY'
import os, struct, sys, pyarrow as pa, pyarrow.ipc as ipc
out = sys.argv[1]
cases = {
    'date_oob.arrows': (pa.date32(), struct.pack('<i', -(2**31))),
    'time_oob.arrows': (pa.time64('us'), struct.pack('<q', -1)),
    'timestamp_oob.arrows': (pa.timestamp('us'), struct.pack('<q', -(2**63))),
}
for name, (typ, raw) in cases.items():
    arr = pa.Array.from_buffers(typ, 1, [None, pa.py_buffer(raw)])
    table = pa.table({'v': arr})
    with ipc.new_stream(pa.OSFile(os.path.join(out, name), 'wb'), table.schema) as w:
        w.write_table(table)
PY
	psql_run "CREATE TABLE ri_date_oob (v date) USING pgcolumnar;
	          CREATE TABLE ri_time_oob (v time) USING pgcolumnar;
	          CREATE TABLE ri_timestamp_oob (v timestamp) USING pgcolumnar;"
	expect_error "reject out-of-range Arrow date" \
		"SELECT pgcolumnar.import_arrow('ri_date_oob', '$PGC_WORKDIR/date_oob.arrows');"
	expect_error "reject out-of-range Arrow time" \
		"SELECT pgcolumnar.import_arrow('ri_time_oob', '$PGC_WORKDIR/time_oob.arrows');"
	expect_error "reject overflowing Arrow timestamp" \
		"SELECT pgcolumnar.import_arrow('ri_timestamp_oob', '$PGC_WORKDIR/timestamp_oob.arrows');"
fi

IXFILE="$PGC_WORKDIR/ix_roundtrip.arrows"

# ---------------------------------------------------------------------------
# Import into a table that already has indexes (issue #153).
#
# table_tuple_insert writes the row and nothing else: index maintenance is the
# executor's job, and an importer calling it directly has to do that itself.
# When it did not, the rows landed in the table and in no index, so an index
# scan returned nothing while a sequential scan returned everything, and a
# unique index accepted the same key twice. Nothing errored either time.
#
# The checks below compare the two access paths against each other on the same
# predicate, which is the shape that catches it: a count that only ever uses one
# path passes whether or not the index was maintained.
# ---------------------------------------------------------------------------

psql_run "CREATE TABLE ix_src (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO ix_src SELECT g, 'v' || g FROM generate_series(1, 3000) g;"
psql_run "SELECT pgcolumnar.export_arrow('ix_src', '$IXFILE');"

psql_run "CREATE TABLE ix_tgt (id int, v text) USING pgcolumnar;"
psql_run "CREATE INDEX ix_tgt_id ON ix_tgt (id);"
psql_run "SELECT pgcolumnar.import_arrow('ix_tgt', '$IXFILE');"

# q echoes one line per statement, so take the last: the count, not the SETs.
#
# enable_seqscan does not discourage a custom scan, so turning it off on its own
# leaves the columnar scan the cheapest path and the index is never consulted.
# That made an earlier version of this check pass against a build with index
# maintenance deliberately removed. pgcolumnar.enable_custom_scan is what takes
# the columnar path out of the running, and the plan is asserted below so a
# future costing change cannot quietly put it back.
IDX_SETUP="SET enable_seqscan = off; SET pgcolumnar.enable_custom_scan = off;"
idx_count() {  # force an index scan
	q "$IDX_SETUP
	   SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199;" | tail -1
}
idx_plan_is_index_scan() {
	q "$IDX_SETUP
	   EXPLAIN (COSTS OFF) SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199;" \
		| grep -qi 'Index.*Scan' && echo yes || echo no
}
seq_count() {  # force a sequential scan
	q "SET enable_indexscan = off; SET enable_bitmapscan = off;
	   SET enable_indexonlyscan = off;
	   SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199;" | tail -1
}

check "the index-scan check really uses the index" "$(idx_plan_is_index_scan)" "yes"
check "import into an indexed table: both scans agree" "$(idx_count)" "$(seq_count)"
check "import into an indexed table: the rows are there" "$(seq_count)" "100"
check "import into an indexed table: a point lookup finds its row" \
	"$(q "$IDX_SETUP
	      SELECT count(*) FROM ix_tgt WHERE id = 2222;" | tail -1)" "1"

# A unique index must police the second import rather than take it.
psql_run "CREATE TABLE ix_uq (id int, v text) USING pgcolumnar;"
psql_run "CREATE UNIQUE INDEX ix_uq_id ON ix_uq (id);"
psql_run "SELECT pgcolumnar.import_arrow('ix_uq', '$IXFILE');"
expect_error "importing the same keys twice raises unique_violation" \
	"SELECT pgcolumnar.import_arrow('ix_uq', '$IXFILE');"
check "the failed second import left the row count alone" \
	"$(q 'SELECT count(*) FROM ix_uq;')" "3000"

# A partial index must receive only the rows its predicate covers.
psql_run "CREATE TABLE ix_part (id int, v text) USING pgcolumnar;"
psql_run "CREATE INDEX ix_part_id ON ix_part (id) WHERE id <= 500;"
psql_run "SELECT pgcolumnar.import_arrow('ix_part', '$IXFILE');"
check "partial index covers its rows after import" \
	"$(q "$IDX_SETUP
	      SELECT count(*) FROM ix_part WHERE id BETWEEN 100 AND 199;" | tail -1)" "100"

# --- #686/#644: import_arrow must refuse a FIFO, not hang the backend ---------
# fopen("rb") on a FIFO blocks in open(2); with SA_RESTART the block survives a
# cancel/statement_timeout. RED (unguarded) is a wall-clock HANG; GREEN is XX001.
echo "-- FIFO refusal"
psql_run "CREATE TABLE ia_fifo (a int) USING pgcolumnar;"
IAFIFO="$PGC_WORKDIR/ia.fifo"; mkfifo "$IAFIFO"
check "import_arrow on a FIFO is refused, not a hang (XX001)" \
	"$(sqlstate_or_hang "SELECT pgcolumnar.import_arrow('ia_fifo', '$IAFIFO')")" \
	"XX001"; fifo_release "$IAFIFO"
check "backend still up after the import_arrow FIFO refusal" "$(q 'SELECT 1')" "1"

pgc_summary

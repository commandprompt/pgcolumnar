#!/usr/bin/env bash
#
# pgColumnar Parquet import suite (gap 27). pgcolumnar.import_parquet(rel, path)
# reads a Parquet file (self-contained: Thrift metadata, Snappy, PLAIN and
# dictionary encodings, DATA_PAGE v1 and v2) and inserts its rows into a target
# table. pyarrow writes the reference files (its default snappy + dictionary +
# data-page-v1, and an explicit data-page-v2 run); the imported columnar table is
# compared against a heap oracle loaded from the identical data.
#
# Requires pyarrow; skips with a note if it is not importable.
#
# Usage:  test/parquet_import.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; skipping Parquet import verification"
fi

expect_error() {
	local label="$1" sql="$2"
	if psql_run "$sql" >/dev/null 2>&1; then
		check "$label (expected error)" "succeeded" "error"
	else
		check "$label" "error" "error"
	fi
}

# Generate identical data as a Parquet file (given page version) and a CSV.
gen_py="$PGC_WORKDIR/gen.py"
cat > "$gen_py" <<'PY'
import sys, csv, datetime
import pyarrow as pa, pyarrow.parquet as pq
pv = sys.argv[1]            # '1.0' or '2.0'
pf = sys.argv[2]; cf = sys.argv[3]
N = 20000
ids=[]; big=[]; f4=[]; f8=[]; s=[]; bl=[]; dt=[]; ts=[]; by=[]
base = datetime.date(2020,1,1); bts = datetime.datetime(2020,1,1)
for g in range(1, N+1):
    ids.append(g)
    big.append(None if g%11==0 else g*100000)
    f4.append(g*0.5)                       # exact in binary32
    f8.append(g*0.25)
    s.append(None if g%13==0 else 'v%d'%g)
    bl.append(None if g%7==0 else (g%2==0))
    dt.append(base + datetime.timedelta(days=g))
    ts.append(bts + datetime.timedelta(seconds=g))
    by.append(None if g%17==0 else bytes([g&0xff, (g>>8)&0xff]))
t = pa.table({
    'id': pa.array(ids, pa.int32()),
    'big': pa.array(big, pa.int64()),
    'f4': pa.array(f4, pa.float32()),
    'f8': pa.array(f8, pa.float64()),
    's': pa.array(s, pa.string()),
    'bl': pa.array(bl, pa.bool_()),
    'dt': pa.array(dt, pa.date32()),
    'ts': pa.array(ts, pa.timestamp('us')),
    'by': pa.array(by, pa.binary()),
})
pq.write_table(t, pf, compression='snappy', use_dictionary=True,
               data_page_version=pv)
with open(cf,'w',newline='') as fh:
    w=csv.writer(fh)
    for i in range(N):
        w.writerow([
            ids[i],
            r'\N' if big[i] is None else big[i],
            f4[i], f8[i],
            r'\N' if s[i] is None else s[i],
            r'\N' if bl[i] is None else ('t' if bl[i] else 'f'),
            dt[i].isoformat(),
            ts[i].isoformat(sep=' '),
            r'\N' if by[i] is None else ('\\x'+by[i].hex()),
        ])
PY

SCHEMA="id int, big bigint, f4 real, f8 double precision, s text, bl bool, dt date, ts timestamp, by bytea"
SEL="SELECT id, big, f4, f8, s, bl, dt, ts, encode(by,'hex') FROM"

run_case() {
	local label="$1" pv="$2"
	local pfile="$PGC_WORKDIR/d_$pv.parquet" cfile="$PGC_WORKDIR/d_$pv.csv"

	python3 "$gen_py" "$pv" "$pfile" "$cfile"

	psql_run "DROP TABLE IF EXISTS pc; DROP TABLE IF EXISTS po;"
	psql_run "CREATE TABLE pc ($SCHEMA) USING pgcolumnar;"
	psql_run "CREATE TABLE po ($SCHEMA) USING heap;"
	psql_run "COPY po FROM '$cfile' WITH (FORMAT csv, NULL E'\\\\N');"

	local n
	n="$(q "SELECT pgcolumnar.import_parquet('pc', '$pfile');")"
	check "$label rows imported" "$n" "20000"
	check "$label row count matches oracle" "$(q 'SELECT count(*) FROM pc;')" "$(q 'SELECT count(*) FROM po;')"
	check "$label contents match heap oracle" \
		"$(pgc_set_hash "$SEL pc")" \
		"$(pgc_set_hash "$SEL po")"
}

echo "-- Parquet import: snappy + dictionary, data page v1 (pyarrow default)"
run_case "v1" "1.0"

echo "-- Parquet import: snappy + dictionary, data page v2"
run_case "v2" "2.0"

echo "-- Parquet import: uncompressed + no dictionary"
cat > "$PGC_WORKDIR/gen2.py" <<'PY'
import sys
import pyarrow as pa, pyarrow.parquet as pq
pf=sys.argv[1]
t=pa.table({'id':pa.array(list(range(1,5001)),pa.int32()),
            's':pa.array(['x%d'%g for g in range(1,5001)],pa.string())})
pq.write_table(t, pf, compression='none', use_dictionary=False)
PY
python3 "$PGC_WORKDIR/gen2.py" "$PGC_WORKDIR/plain.parquet"
psql_run "CREATE TABLE pp (id int, s text) USING pgcolumnar;"
check "plain import rows" "$(q "SELECT pgcolumnar.import_parquet('pp', '$PGC_WORKDIR/plain.parquet');")" "5000"
check "plain import sum(id)" "$(q 'SELECT sum(id) FROM pp;')" "12502500"
check "plain import first s" "$(q "SELECT s FROM pp WHERE id = 42;")" "x42"

echo "-- argument validation"
expect_error "reject non-parquet file" "SELECT pgcolumnar.import_parquet('pp', '$gen_py');"
psql_run "CREATE TABLE pmismatch (id int, s text, extra int) USING pgcolumnar;"
expect_error "reject column-count mismatch" "SELECT pgcolumnar.import_parquet('pmismatch', '$PGC_WORKDIR/plain.parquet');"

IXFILE="$PGC_WORKDIR/ix_roundtrip.parquet"

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
psql_run "SELECT pgcolumnar.export_parquet('ix_src', '$IXFILE');"

psql_run "CREATE TABLE ix_tgt (id int, v text) USING pgcolumnar;"
psql_run "CREATE INDEX ix_tgt_id ON ix_tgt (id);"
psql_run "SELECT pgcolumnar.import_parquet('ix_tgt', '$IXFILE');"

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
	# grep -c on a captured value, not a pipe into grep -q; see lib.sh's
	# pgc_is_columnar_scan for the mechanism and the measurement.
	local _plan
	_plan="$(q "$IDX_SETUP
	   EXPLAIN (COSTS OFF) SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199;")"
	[ "$(grep -ci 'Index.*Scan' <<<"$_plan" || true)" -ne 0 ] && echo yes || echo no
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
psql_run "SELECT pgcolumnar.import_parquet('ix_uq', '$IXFILE');"
expect_error "importing the same keys twice raises unique_violation" \
	"SELECT pgcolumnar.import_parquet('ix_uq', '$IXFILE');"
check "the failed second import left the row count alone" \
	"$(q 'SELECT count(*) FROM ix_uq;')" "3000"

# A partial index must receive only the rows its predicate covers.
psql_run "CREATE TABLE ix_part (id int, v text) USING pgcolumnar;"
psql_run "CREATE INDEX ix_part_id ON ix_part (id) WHERE id <= 500;"
psql_run "SELECT pgcolumnar.import_parquet('ix_part', '$IXFILE');"
check "partial index covers its rows after import" \
	"$(q "$IDX_SETUP
	      SELECT count(*) FROM ix_part WHERE id BETWEEN 100 AND 199;" | tail -1)" "100"

# ---- a PARTITIONED columnar parent must be refused, not segfault (#1263) -----
#
# import_parquet never asks whether the relation is columnar. Every other
# regclass entry point resolves PgColumnarIsColumnarRelation; the file holding
# this one uses it zero times, so it was not among #1259's 31 call sites and
# #1261's predicate fix cannot reach it. Handed a partitioned parent -- relam
# pgcolumnar, relfilenode 0 -- it dereferences its way to SIGSEGV. Measured on
# main 00d3529c: signal 11, and on a build WITHOUT asserts too, so unlike
# #1259's family this takes the cluster down on a production build rather than
# returning a wrong answer.
#
# THE FILE MUST EXIST BEFORE THE CALL. A sweep that reused one fixture across
# entry points recorded this as "survived rc=1", because export had been
# refused on the parent and the import failed at the OPEN before reaching the
# crash. An arm that skips the export step passes against the unfixed build.
#
# AND "IS REFUSED" IS NOT THE PROPERTY. A crashed connection also returns
# non-zero, so expect_error alone is satisfied by the bug itself. The liveness
# check from a fresh backend is what separates a refusal from an abort.
psql_run "DROP TABLE IF EXISTS pq_parent CASCADE;
	CREATE TABLE pq_parent (id int, v text) PARTITION BY RANGE (id);
	ALTER TABLE pq_parent SET ACCESS METHOD pgcolumnar;
	CREATE TABLE pq_leaf PARTITION OF pq_parent FOR VALUES FROM (0) TO (100);
	INSERT INTO pq_parent SELECT g, 'r' || g FROM generate_series(0, 49) g;"

check "premise: the parent is partitioned and carries the columnar access method" \
	"$(q "SELECT c.relkind::text || am.amname::text
	      FROM pg_class c JOIN pg_am am ON am.oid = c.relam
	      WHERE c.relname = 'pq_parent';")" "ppgcolumnar"
check_num "premise: so it has no storage of its own" \
	"$(q "SELECT relfilenode FROM pg_class WHERE relname = 'pq_parent';")" "0"

PQFILE="$PGC_WORKDIR/pq_leaf.parquet"
psql_run "SELECT pgcolumnar.export_parquet('pq_leaf', '$PQFILE');" >/dev/null 2>&1
check "premise: a parquet file exists, so the import reaches the relation" \
	"$([ -s "$PQFILE" ] && echo yes || echo no)" "yes"

# TWO CONTROLS, and the HEAP one is the reason this guard is about storage and
# not about being columnar. import_parquet deliberately accepts a heap target --
# native_parquet_units imports into a plain `CREATE TABLE imp_ms (t timestamp)`
# -- which is where it differs from import_arrow. A first version of this fix
# guarded on PgColumnarIsColumnarRelation, rejected heap targets with 42809 and
# reddened four suites; the arm set at the time had a columnar control and no
# heap one, so it could not see that.
check_num "control: importing that file into the LEAF still works" \
	"$(q "SELECT pgcolumnar.import_parquet('pq_leaf', '$PQFILE');")" "50"

psql_run "DROP TABLE IF EXISTS pq_heap;
	CREATE TABLE pq_heap (id int, v text);"
check_num "control: a HEAP target still imports, so the guard is about storage" \
	"$(q "SELECT pgcolumnar.import_parquet('pq_heap', '$PQFILE');")" "50"

expect_error "importing into the partitioned parent is refused" \
	"SELECT pgcolumnar.import_parquet('pq_parent', '$PQFILE');"
# `q`, not `psql_run`: psql_run runs -q with no -At and prints nothing, so this
# arm returned empty on a LIVE cluster and could never pass. It read as a
# correct red against the unfixed build and would have stayed red against the
# fix -- a discriminator that discriminates nothing.
check "and the cluster is still up, so the refusal was not an abort" \
	"$(q "SELECT 1;")" "1"

pgc_summary

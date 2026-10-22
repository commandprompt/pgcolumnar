#!/usr/bin/env bash
#
# pgColumnar audit regression test. Each block below reproduces a concrete
# defect found during the bug audit and asserts the fixed behaviour. Every
# check here fails against the pre-fix code and passes after the fix.
#
#   1. ADD COLUMN on a table with existing data. A stripe written before the
#      column existed has no chunk for it; the reader must yield the column's
#      missing value (NULL, or the constant default) instead of erroring with
#      "missing chunk for attr N". Covers sequential scan, projection, the
#      vectorized aggregate path, and index fetch.
#   2. Chunk-group skipping under a mismatched collation. The stored per-chunk
#      min/max are ordered under the column's own collation; a predicate whose
#      comparison uses a different collation (an explicit COLLATE) must not
#      drive skipping, or matching rows are wrongly dropped. Results must be
#      identical whether or not qual pushdown is enabled.
#   3. Per-table option bounds. set_options must reject an
#      out-of-range chunk_group_row_limit / stripe_row_limit / compression_level
#      rather than store it: a zero chunk_group_row_limit records a stripe with
#      chunk_row_count = 0 and makes delete/update/fetch divide by zero.
#   5. ALTER TABLE ADD COLUMN while a write buffer is already open in the same
#      transaction. The buffer snapshots the tuple descriptor when it opens and
#      nothing invalidates it, so every value written into the new column after
#      the ALTER was silently dropped and the column read back as its missing
#      value. Covers INSERT and UPDATE, with and without a default.
#   6. A same-statement duplicate against a unique index must be caught rather
#      than spin. The second row is inserted while the first is still buffered
#      and unflushed, so the uniqueness check fetches a row that has no row
#      group yet; if that fetch cannot answer, _bt_doinsert retries forever.
#      Cheap standalone cover for what unique_conc scenario 7 exercises.
#   4. CREATE INDEX must not leak a relation reference. A parallel index build
#      opens a TableScanDesc per participant through pgcolumnar_scan_begin (which
#      takes a relation reference); the index_build_range_scan callback owns that
#      scan and must end it, or each participant leaks a reference that surfaces
#      at transaction commit as "resource was not closed: relation". The callback
#      must also read through the participant's claimed scan, so every live row is
#      indexed exactly once rather than once per participant.
#
# Builds and installs the extension, spins up a throwaway PostgreSQL cluster as
# the postgres OS user, and exercises the cases above. Written fresh for
# pgColumnar; it does not reuse any upstream test file or expected-output file.
#
# Usage:
#   test/audit.sh [PG_CONFIG]
#
# PG_CONFIG defaults to /usr/local/pg17/bin/pg_config. Run as a user that may
# "runuser -u postgres" (e.g. root) when the current user is not postgres.

set -euo pipefail

# set -e aborts this suite at the first unguarded failure, and the verdict block
# is at the bottom. So an abort ends the run non-zero having printed neither a
# FAIL line nor AUDIT TEST FAILED, and every check below the abort silently did
# not run (#418).
#
# Observed: a CREATE TABLE carrying COLLATE "en_US" on a box where that locale
# is not generated killed this suite partway through. The runner recorded
# audit=FAIL, correctly, and the output showed five PASS lines, one ERROR, and
# no verdict at all, which reads like a suite that stopped for no reason.
#
# The trap says which it was. It does not change the exit status, and it does
# not turn an abort into a pass; it stops an abort from being mistaken for a
# short run that went fine.
AUDIT_REACHED_END=0
# THE NOTICE LIVES IN cleanup() BELOW, NOT IN A trap OF ITS OWN. It was a trap here,
# and `trap cleanup EXIT` further down replaced it, so it never printed: with
# set_options failing at entry the suite stopped after the partitioned arm, 13 checks
# unrecorded, and the log ended on the server's error with no notice at all.

# lib.sh for the check vocabulary (#965). It sources portlib.sh itself
# (lib.sh:110), so this is a superset of what was here, and its top level is
# assignments and function definitions only, so sourcing it starts nothing.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PG_CONFIG="${1:-/usr/local/pg17/bin/pg_config}"

# WHICH MAJOR THIS SUITE RAN ON (#1121, the wider half of #1109). `pgc_record`
# writes `${PGC_MAJOR:-unknown}`, and PGC_MAJOR is set inside `pgc_setup` -- which
# this suite does not call, deliberately. Without this line every record it emits
# says `unknown`, and a ledger row claiming `unknown` matches no run, so none of
# these checks could ever be seeded or matched again.
#
# The runner passes the pg_config as $1 to EVERY suite, including those that need
# no cluster, so it is available here. `pgc_major_of` returns empty on a path it
# cannot run, which degrades to exactly today's `unknown` rather than to a WRONG
# major -- a guessed major would seed a row claiming a major the check was never
# observed on, which is worse than saying nothing.
PGC_MAJOR="$(pgc_major_of "$PG_CONFIG")"
BINDIR="$("$PG_CONFIG" --bindir)"
PORT="${PGC_PORT:-$(pgc_pick_port)}"
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WORKDIR="$(mktemp -d /tmp/pgcolumnar-audit.XXXXXX)"
PGDATA="$WORKDIR/data"
LOGFILE="$WORKDIR/server.log"

echo "== pgColumnar audit test =="
echo "PG_CONFIG=$PG_CONFIG"
echo "workdir=$WORKDIR"

# The matrix runner installs the extension once per version and sets
# PGC_SKIP_BUILD; skip the redundant per-suite build+install then, which also
# avoids racing a concurrent suite's install into the same lib dir.
if [ -z "${PGC_SKIP_BUILD:-}" ]; then
	# THE HARNESS BUILDER, NOT A HAND-ROLLED make (#1220). Three guarantees come
	# with it and none of them were here: it asks the OBJECTS which major built
	# them (#1219), it keeps the build stamp (#536), and it refuses to run when
	# the build or the install failed instead of reporting checks against the
	# previously installed .so -- which the two discarded exit statuses below it
	# used to do silently.
	pgc_build_and_install "$SRCDIR" "$PG_CONFIG" "$PGC_MAJOR" || exit 1
fi

if [ "$(id -u)" = "0" ]; then
	RUNPG=(runuser -u postgres --)
	chown -R postgres "$WORKDIR"
else
	RUNPG=(env)
fi

run_pg() { "${RUNPG[@]}" env PATH="$BINDIR:$PATH" bash -lc "$1"; }

cleanup() {
	local rc=$?
	if [ "$AUDIT_REACHED_END" = 0 ]; then
		echo; echo "AUDIT TEST ABORTED (exit $rc) before reaching its verdict."
		echo "The checks after the failing command did not run."
	fi
	run_pg "pg_ctl -D '$PGDATA' stop -m immediate -w" >/dev/null 2>&1 || true
	rm -rf "$WORKDIR"
	return $rc
}
trap cleanup EXIT

echo "-- initdb"
run_pg "initdb -D '$PGDATA' -A trust" >/dev/null 2>&1
run_pg "echo \"port=$PORT\" >> '$PGDATA/postgresql.conf'"
run_pg "echo \"shared_preload_libraries='pgcolumnar'\" >> '$PGDATA/postgresql.conf'"
echo "-- start"
run_pg "pg_ctl -D '$PGDATA' -l '$LOGFILE' start -w" >/dev/null
run_pg "createdb -p $PORT audit"

PSQL="psql -p $PORT -d audit -qAtX -v ON_ERROR_STOP=1"
q() { run_pg "$PSQL -c \"$1\""; }

# LIB.SH'S check, NOT A LOCAL ONE (#1341). This suite used to define its own
# check() and call pgc_record from it, to keep the measured value on every PASS
# line. Nothing else here needs a local recorder, and a suite that calls
# pgc_record directly cannot have a pytest twin (test_compare_to_bash's seed
# arm), so it records through lib.sh like the rest of the corpus. A FAIL line still
# carries both sides; a PASS line carries the name.

# expect_error NAME SQL MESSAGE -- PASS when SQL is refused WITH MESSAGE.
#
# ANY error used to pass. Measured on PG17 with set_options failing at entry for a
# reason that is none of its guards (a division by zero): all five refusal arms below
# passed, while the two that already read the reason went red. MESSAGE is the guard's
# own text, so a refusal must come from the guard the arm names.
#
# The name stays `expect_error`: selftest 270 recognises an out-of-range set_options
# literal as deliberate only inside this helper.
expect_error() {
	local name="$1" sql="$2" want="$3" out
	local got
	if out="$(run_pg "$PSQL -c \"$sql\"" 2>&1)"; then
		got="statement unexpectedly succeeded"
	elif [ -z "$want" ]; then
		got="no refusal message to require (harness)"
	else
		case "$out" in
			*"$want"*) got="rejected" ;;
			*) got="refused for another reason: $(printf '%s\n' "$out" | head -1)" ;;
		esac
	fi
	check "$name" "$got" "rejected"
}

q "CREATE EXTENSION pgcolumnar;" >/dev/null

# ---------------------------------------------------------------------------
# 1. ADD COLUMN after data exists must not break reads (missing-value support).
# ---------------------------------------------------------------------------
q "CREATE TABLE ac (a int) USING pgcolumnar;" >/dev/null
q "INSERT INTO ac SELECT g FROM generate_series(1,5) g;" >/dev/null
q "ALTER TABLE ac ADD COLUMN b int;" >/dev/null
q "ALTER TABLE ac ADD COLUMN c int DEFAULT 42;" >/dev/null
q "ALTER TABLE ac ADD COLUMN d text DEFAULT 'hi';" >/dev/null

# old rows: b is NULL, c is the constant default 42, d is 'hi'
check "add-column nullable is null" \
	"$(q "SELECT count(*) FROM ac WHERE b IS NULL;")" "5"
check "add-column const default" \
	"$(q "SELECT string_agg(DISTINCT c::text, ',') FROM ac;")" "42"
check "add-column text default" \
	"$(q "SELECT string_agg(DISTINCT d, ',') FROM ac;")" "hi"

# projection of only the added columns
check "add-column projected only" \
	"$(q "SELECT c || '/' || d FROM ac ORDER BY a LIMIT 1;")" "42/hi"

# new rows carry real values, mixed with old rows
q "INSERT INTO ac VALUES (6, 7, 8, 'new');" >/dev/null
check "add-column mixed rows" \
	"$(q "SELECT a||':'||coalesce(b::text,'-')||':'||c||':'||d FROM ac WHERE a IN (5,6) ORDER BY a;" | tr '\n' ' ')" \
	"5:-:42:hi 6:7:8:new "

# vectorized aggregate over the added columns
check "add-column aggregate" \
	"$(q "SELECT sum(c) || '/' || count(d) || '/' || count(b) FROM ac;")" "218/6/1"

# index scan over an added column
#
# FORCED THROUGH THE INDEX, AND ASSERTED (#1339). enable_seqscan alone leaves the
# columnar custom scan on, so this and "create index rows indexed once" below could
# be answered without reading the index they name. Both lookups now turn the
# custom scan and bitmap scans off (a bitmap scan would also collapse a duplicate
# index entry), and assert an Index Scan or Index Only Scan through their index.
IDXONLY="SET enable_seqscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off;"
reads_index() {	# reads_index INDEX QUERY -> yes, or the plan's scan line
	local plan
	plan="$(q "$IDXONLY EXPLAIN (COSTS OFF) $2")"
	case "$plan" in
		*"Index Scan using $1 "*|*"Index Only Scan using $1 "*) echo yes ;;
		*) echo "no: $(grep -m1 'Scan' <<<"$plan" | sed 's/^ *//')" ;;
	esac
}
q "CREATE INDEX ac_c_idx ON ac(c);" >/dev/null
check "premise: the add-column lookup reads ac_c_idx" \
	"$(reads_index ac_c_idx "SELECT count(*) FROM ac WHERE c=42")" "yes"
check "add-column index scan" \
	"$(q "$IDXONLY SELECT count(*) FROM ac WHERE c=42;" | tail -1)" "5"
q "DROP TABLE ac;" >/dev/null

# ---------------------------------------------------------------------------
# 2. Chunk-group skipping must respect the predicate collation.
# ---------------------------------------------------------------------------
q "SET pgcolumnar.chunk_group_row_limit=100; SET pgcolumnar.stripe_row_limit=1000;
   CREATE TABLE col (t text COLLATE \\\"en_US\\\") USING pgcolumnar;" >/dev/null
# all values uppercase, so each group's min/max are uppercase letters
q "INSERT INTO col SELECT chr(65 + (g%20)) || lpad(g::text,5,'0')
     FROM generate_series(1,300) g;" >/dev/null

# under C collation every uppercase letter is < 'a'; all 300 rows match.
# With the bug, en_US-ordered min/max wrongly skip every group -> 0 rows.
ON="$(q "SET pgcolumnar.enable_qual_pushdown=on;  SELECT count(*) FROM col WHERE t < 'a' COLLATE \\\"C\\\";")"
OFF="$(q "SET pgcolumnar.enable_qual_pushdown=off; SELECT count(*) FROM col WHERE t < 'a' COLLATE \\\"C\\\";")"
check "collation pushdown == no-pushdown" "$ON" "$OFF"
check "collation result correct"          "$ON" "300"

# a same-collation predicate is still pushed down (skipping preserved)
PUSHED="$(q "EXPLAIN (COSTS off) SELECT t FROM col WHERE t < 'A00001';" \
	| grep -c 'Pushed-Down Filters: 1' || true)"
check "same-collation still pushes" "$PUSHED" "1"
q "DROP TABLE col;" >/dev/null

# ---------------------------------------------------------------------------
# 3. Per-table option bounds are validated (no divide-by-zero from limit 0).
# ---------------------------------------------------------------------------
q "CREATE TABLE opt (a int) USING pgcolumnar;" >/dev/null
expect_error "reject chunk_group_row_limit 0" \
	"SELECT pgcolumnar.set_options('opt'::regclass, chunk_group_row_limit => 0);" \
	"chunk_group_row_limit must be at least 100"
expect_error "reject stripe_row_limit 5" \
	"SELECT pgcolumnar.set_options('opt'::regclass, stripe_row_limit => 5);" \
	"stripe_row_limit must be at least 1000"
expect_error "reject compression_level 99" \
	"SELECT pgcolumnar.set_options('opt'::regclass, compression_level => 99);" \
	"compression_level must be between 1 and 22"

# A relation that is not columnar must be refused rather than recorded. The drop
# hook that clears pgcolumnar.options fires only for columnar relations, so a row
# stored for a heap table outlives the table and is left keyed to a dangling oid,
# which a later relation reusing that oid inherits. Measured before the guard, on
# one cluster: the heap table's row survived DROP TABLE and regclass then rendered
# as the bare oid, while the identical sequence on a columnar table cleaned up.
q "CREATE TABLE opt_heap (a int) USING heap;" >/dev/null
expect_error "reject set_options on a heap table" \
	"SELECT pgcolumnar.set_options('opt_heap'::regclass, chunk_group_row_limit => 1000);" \
	"is not a columnar table"
# expect_error now requires the guard's message too; this check reads the full
# reason the same way, and stays as its own record. Captured and then matched: piping an erroring psql into
# grep reports on the pipeline rather than on the message.
# `|| true` because this file runs under `set -e` and the call is EXPECTED to
# fail: without it the assignment takes the non-zero status and aborts the suite
# right here, which is what it did the first time. expect_error above survives
# only because it sits inside an `if`.
opt_heap_err="$(run_pg "$PSQL -c \"SELECT pgcolumnar.set_options('opt_heap'::regclass, chunk_group_row_limit => 1000);\"" 2>&1 || true)"
check "the refusal says the relation is not columnar" \
	"$(case "$opt_heap_err" in *"is not a columnar table"*) echo yes ;; *) echo "no [$opt_heap_err]" ;; esac)" \
	"yes"
check "nothing was recorded for the heap table" \
	"$(q "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt_heap'::regclass;")" "0"

# The SQLSTATE, not the message text. The C paths raise this same sentence with
# ERRCODE_WRONG_OBJECT_TYPE (42809), so a caller that keys on SQLSTATE -- which
# is what this project's own privilege suites do, deliberately -- has to get the
# same answer whichever path refuses it. plpgsql's RAISE EXCEPTION defaults to
# P0001 unless an ERRCODE is given, and the guard shipped without one.
#
# VERBOSITY=verbose makes psql print `ERROR:  <sqlstate>: <message>`, which is
# read here instead of a DO block: this suite passes SQL through `bash -lc`, and
# a dollar-quoted block nested in that is the quoting trap the tree already has
# scars from.
sqlstate() {	# sqlstate <sql> -> the SQLSTATE of the failure, or empty
	local out
	out="$(run_pg "$PSQL -v ON_ERROR_STOP=0 -v VERBOSITY=verbose -c \"$1\"" 2>&1 || true)"
	printf '%s\n' "$out" | grep -oE '^ERROR:  [0-9A-Z]{5}' | awk '{print $2}' | head -1
}
check "premise: the SQLSTATE probe reads a known code" \
	"$(sqlstate 'SELECT 1/0;')" "22012"
check "set_options on a non-columnar relation raises 42809, not P0001" \
	"$(sqlstate "SELECT pgcolumnar.set_options('opt_heap'::regclass, chunk_group_row_limit => 1000);")" \
	"42809"
q "DROP TABLE opt_heap;" >/dev/null

# A PARTITIONED parent is the same leak through a different door, and the access
# method alone does not close it. From PG17 a partitioned table may carry an
# access method, so `relam = pgcolumnar` admits a parent that has no storage of
# its own, that the writer never writes, and whose options row the drop hook will
# never clear: the hook returns early for anything that is not RELKIND_RELATION
# (columnar_tableam.c). Measured on 17.6 before the relkind test was added:
# accepted, one row recorded, and the row still present after DROP TABLE keyed to
# the dropped oid. PG16 and earlier refuse `PARTITION BY ... USING pgcolumnar`
# outright (checked on 16.14), so the arm is gated rather than skipped silently.
audit_srv="$(q 'SHOW server_version_num')"
if [ "$audit_srv" -ge 170000 ]; then
	q "CREATE TABLE opt_part (id int, v text) PARTITION BY RANGE (id) USING pgcolumnar;" >/dev/null
	# relkind is type "char", so it needs the cast: without it the concatenation
	# raises `operator is not unique: "char" || unknown`, the premise returns empty,
	# and the arm below would be believed on no evidence.
	check "premise: this major really lets a partitioned parent carry the AM" \
		"$(q "SELECT c.relkind::text || '/' || a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam WHERE c.relname = 'opt_part';")" \
		"p/pgcolumnar"
	expect_error "reject set_options on a partitioned parent" \
		"SELECT pgcolumnar.set_options('opt_part'::regclass, chunk_group_row_limit => 1000);" \
		"is not a columnar table"
	check "nothing was recorded for the partitioned parent" \
		"$(q "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt_part'::regclass;")" "0"
	# And the leaf, which is an ordinary table, must still be accepted -- the guard
	# has to refuse the parent without refusing the thing that actually stores rows.
	q "CREATE TABLE opt_part_1 PARTITION OF opt_part FOR VALUES FROM (0) TO (100);" >/dev/null
	# `|| true`: a set_options that fails here is what the next check reports, by name.
	# Under set -e it used to end the suite instead, with every later check unrecorded.
	q "SELECT pgcolumnar.set_options('opt_part_1'::regclass, chunk_group_row_limit => 1000);" >/dev/null || true
	check "the partition itself is still accepted" \
		"$(q "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt_part_1'::regclass;")" "1"
	q "DROP TABLE opt_part;" >/dev/null
else
	# ONE SKIP FOR THE BLOCK, not one per gated check, which is the shape
	# unique_conc.sh:546 already uses for its own version gate. Naming each of the
	# four checks here would make the count major-invariant and would also duplicate
	# four check names in a branch that never runs them, so they would drift. The
	# real fix for comparing counts across majors is a major dimension in the
	# ledger, which is #432's problem and not this file's.
	check_skip "the partitioned-parent arm" \
		"SKIP  the partitioned-parent arm (PG$((audit_srv / 10000)) refuses PARTITION BY ... USING pgcolumnar)" \
		"PG$((audit_srv / 10000)) refuses PARTITION BY ... USING pgcolumnar"
fi

# valid values are accepted, and delete/update work (no divide-by-zero)
# `|| true` for the same reason: "is recorded" below names a failed call.
q "SELECT pgcolumnar.set_options('opt'::regclass,
     chunk_group_row_limit => 1000, stripe_row_limit => 2000, compression_level => 9);" >/dev/null || true
q "INSERT INTO opt SELECT g FROM generate_series(1,10) g;" >/dev/null
q "DELETE FROM opt WHERE a=5;" >/dev/null
check "valid options delete works" "$(q "SELECT count(*) FROM opt;")" "9"
# The positive control for the refusal above: the same call, on a columnar table,
# is accepted AND recorded. Without this the deny arm could pass because
# set_options rejects everything.
check "the same call on a columnar table is recorded" \
	"$(q "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt'::regclass;")" "1"
q "DROP TABLE opt;" >/dev/null

# ---------------------------------------------------------------------------
# 4. CREATE INDEX must not leak a relation reference (parallel index build).
# ---------------------------------------------------------------------------
# parallel_workers on the table plus min_parallel_table_scan_size=0 forces a
# parallel build regardless of the table's estimated size; a small stripe limit
# gives several stripes. Before the fix each build participant leaked a
# reference to the table, logged as "resource was not closed: relation", and
# re-scanned the whole table so rows were indexed once per participant.
q "SET pgcolumnar.stripe_row_limit=10000;
   CREATE TABLE idxleak (a int, b int) USING pgcolumnar WITH (parallel_workers=2);" >/dev/null
q "INSERT INTO idxleak SELECT g, g%100 FROM generate_series(1,50000) g;" >/dev/null
# THE BUILD'S OUTCOME IS RECORDED, NOT DISCARDED. It was `>/dev/null 2>&1 || true`.
# Measured on PG17 with a parallel build participant raising an error: CREATE INDEX
# failed, no index existed, and "no relation leak" passed (nothing was built, so
# nothing leaked) and "rows indexed once" passed (50000, counted by a Seq Scan).
# Only the plan check went red, and it was labelled a premise.
idx_out="$(run_pg "$PSQL -c \"SET max_parallel_maintenance_workers=2; SET min_parallel_table_scan_size=0;
   CREATE INDEX idxleak_a_idx ON idxleak(a);\"" 2>&1)" && idx_rc=0 || idx_rc=$?
check "the parallel CREATE INDEX completed" \
	"$([ "$idx_rc" = 0 ] && echo yes || echo "no: $(printf '%s\n' "$idx_out" | head -1)")" "yes"

# the server logfile must carry no resource-leak warning
LEAKS="$(run_pg "grep -c 'resource was not closed' '$LOGFILE'" || true)"
check "create index no relation leak" "$LEAKS" "0"

# and every row is indexed exactly once (no duplicate TIDs from a per-worker rescan)
# NOT A PREMISE: a failed parallel build reddens it (#1339). And the count is read
# only when that plan reads the index, so a Seq Scan cannot answer for it.
idx_plan="$(reads_index idxleak_a_idx "SELECT count(*) FROM idxleak WHERE a>0")"
check "the indexed-once count reads idxleak_a_idx" "$idx_plan" "yes"
check "create index rows indexed once" \
	"$([ "$idx_plan" = yes ] && q "$IDXONLY SELECT count(*) FROM idxleak WHERE a>0;" | tail -1 || echo "not read through idxleak_a_idx")" "50000"
q "DROP TABLE idxleak;" >/dev/null

# ---------------------------------------------------------------------------
# 5. ALTER TABLE ADD COLUMN with a write buffer already open in the same
#    transaction. Writes to the new column must survive.
# ---------------------------------------------------------------------------
# The buffer is keyed by (relation, subtransaction) and snapshots the descriptor
# when it opens; nothing invalidates it on DDL. A write before the ALTER opens
# it with the old column count, so without the fix every value written into the
# added column afterwards is dropped on the floor and the column reads back as
# its missing value, with no error.

q "CREATE TABLE wsalter (id int, v text) USING pgcolumnar;" >/dev/null
q "BEGIN;
   INSERT INTO wsalter VALUES (0,'x');
   ALTER TABLE wsalter ADD COLUMN w int DEFAULT 7;
   INSERT INTO wsalter VALUES (1,'a',99);
   COMMIT;" >/dev/null
check "add-column mid-transaction keeps the inserted value" \
	"$(q "SELECT string_agg(DISTINCT coalesce(w::text,'NULL'), ',' ORDER BY coalesce(w::text,'NULL')) FROM wsalter;")" \
	"7,99"

q "CREATE TABLE wsalter2 (id int, v text) USING pgcolumnar;" >/dev/null
q "BEGIN;
   INSERT INTO wsalter2 VALUES (0,'x');
   ALTER TABLE wsalter2 ADD COLUMN w int;
   INSERT INTO wsalter2 VALUES (1,'a',99);
   COMMIT;" >/dev/null
check "add-column without a default keeps the inserted value" \
	"$(q "SELECT count(*) FROM wsalter2 WHERE w = 99;")" "1"

q "CREATE TABLE wsalter3 (id int, v text) USING pgcolumnar;" >/dev/null
q "BEGIN;
   INSERT INTO wsalter3 VALUES (0,'x');
   ALTER TABLE wsalter3 ADD COLUMN w int DEFAULT 7;
   UPDATE wsalter3 SET w = 55;
   COMMIT;" >/dev/null
check "add-column mid-transaction keeps a later UPDATE" \
	"$(q "SELECT string_agg(DISTINCT w::text, ',') FROM wsalter3;")" "55"

q "DROP TABLE wsalter; DROP TABLE wsalter2; DROP TABLE wsalter3;" >/dev/null
# 6. Same-statement duplicate against a unique index: caught, not spun on.
# ---------------------------------------------------------------------------
# The second row is inserted while the first is still buffered and unflushed, so
# the uniqueness check fetches a row number no row group covers yet. If that
# fetch cannot answer, _bt_doinsert retries the same index entry forever, which
# shows up as a backend at 100% CPU rather than as an error. The timeout is the
# assertion: without it a regression hangs this suite instead of failing it.

q "CREATE TABLE ss_dup (k int) USING pgcolumnar;" >/dev/null
q "CREATE UNIQUE INDEX ss_dup_uidx ON ss_dup (k);" >/dev/null
ss_out="$(run_pg "timeout 60 psql -p $PORT -d audit -qAtX -c \"INSERT INTO ss_dup SELECT 7 FROM generate_series(1,2);\" 2>&1" || true)"
check "same-statement duplicate is rejected, not spun on" \
	"$(case "$ss_out" in
		*"duplicate key value"*) echo rejected ;;
		"") echo "TIMED OUT (uniqueness check did not terminate)" ;;
		*) echo "$ss_out" | head -1 ;;
	esac)" "rejected"
check "same-statement duplicate inserted nothing" \
	"$(q "SELECT count(*) FROM ss_dup;")" "0"
q "DROP TABLE ss_dup;" >/dev/null

echo
AUDIT_REACHED_END=1
# The verdict is lib.sh's, from the counters every check updates. pgc_summary adds
# the accounting line the matrix reconciles on, which this suite never printed.
if [ "${PGC_FAIL:-0}" = "0" ]; then
	echo "AUDIT TEST PASSED"
else
	echo "AUDIT TEST FAILED"
	echo "---- server log tail ----"
	run_pg "tail -30 '$LOGFILE'" || true
fi
pgc_summary

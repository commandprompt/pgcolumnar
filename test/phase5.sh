#!/usr/bin/env bash
#
# pgColumnar phase 5 test: planner integration and vacuum. Covers the columnar
# custom scan (EXPLAIN shows it for a plain SELECT), qual pushdown driving
# chunk-group skipping (a filtered scan reads fewer chunk groups than an
# unfiltered one, while results are identical whether or not pushdown is on),
# column projection pushed into the scan, per-table options (compression,
# compression level, chunk-group row limit) taking effect for later writes and
# resetting to the instance default, vacuum combining stripes and reclaiming
# space from row-mask-deleted rows while returning correct rows (including with
# an index, which is rebuilt), and plan stability (no parallel sequential scan;
# a clean fallback to a sequential scan when the custom scan is disabled).
#
# Builds and installs the extension, spins up a throwaway PostgreSQL cluster as
# the postgres OS user, and exercises the phase 5 feature set. Written fresh for
# pgColumnar; it does not reuse any upstream test file or expected-output file.
#
# Usage:
#   test/phase5.sh [PG_CONFIG]
#
# PG_CONFIG defaults to /usr/local/pg17/bin/pg_config. Run as a user that may
# "runuser -u postgres" (e.g. root) when the current user is not postgres.

set -euo pipefail

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

WORKDIR="$(mktemp -d /tmp/pgcolumnar-phase5.XXXXXX)"
PGDATA="$WORKDIR/data"
LOGFILE="$WORKDIR/server.log"

echo "== pgColumnar phase 5 test =="
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
	run_pg "pg_ctl -D '$PGDATA' stop -m immediate -w" >/dev/null 2>&1 || true
	rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "-- initdb"
run_pg "initdb -D '$PGDATA' -A trust" >/dev/null 2>&1
run_pg "echo \"port=$PORT\" >> '$PGDATA/postgresql.conf'"
run_pg "echo \"shared_preload_libraries='pgcolumnar'\" >> '$PGDATA/postgresql.conf'"
echo "-- start"
run_pg "pg_ctl -D '$PGDATA' -l '$LOGFILE' start -w" >/dev/null
run_pg "createdb -p $PORT p5"

# -qAtX: quiet, unaligned, tuples-only, no psqlrc, so a value is exactly output.
PSQL="psql -p $PORT -d p5 -qAtX -v ON_ERROR_STOP=1"
q() { run_pg "$PSQL -c \"$1\""; }

fail=0
# RECORDS RATHER THAN ONLY PRINTING (#965). This suite emitted human PASS lines
# and no RESULT records, so every mechanism built on the record vocabulary -- the
# ledger, the census, checks_never_observed_red, the red-observation record,
# duplicate-name detection -- was blind to all of them. The ledger did not merely
# return nothing on this log, it REFUSED it: "no RESULT records, so there is
# nothing to reconcile".
#
# `pgc_record` takes the DISPLAY whole, so the human output below is byte-for-byte
# what it was. NOT lib.sh's own `check`: that composes its own display and would
# drop the `: $got` suffix, which is the measured value rather than a label.
#
# `fail` is still set, so this suite's exit logic is untouched. Its verdict line
# stays for the same reason: under `set -euo pipefail` a failing command aborts the
# suite, and the verdict is what distinguishes finished from stopped.
#
# The `checks run:` line is READ BY NOTHING YET. The matrix gates reconciliation on
# the ACCOUNTING line via `pgc_log_shows_accounting`, and this suite emits none
# because it emits no accounting line. The line is still correct and wanted --
# it is the total the records reconcile against -- so the remaining step is a gate
# flip rather than new work (@jdatcmd, #969 review).
check() {
	local name="$1" got="$2" want="$3"
	if [ "$got" = "$want" ]; then
		pgc_record PASS "$name" "PASS  $name: $got"
	else
		pgc_record FAIL "$name" "FAIL  $name: got [$got] want [$want]"
		fail=1
	fi
}

# assert an EXPLAIN plan contains / omits text
assert_plan() {
	# $1 name, $2 setup+query, $3 must-contain, $4 must-NOT-contain (or "")
	local name="$1" sql="$2" want="$3" notwant="${4:-}"
	local plan
	plan="$(run_pg "$PSQL -c \"$sql\"")"
	if grep -q "$want" <<<"$plan" && { [ -z "$notwant" ] || ! grep -q "$notwant" <<<"$plan"; }; then
		pgc_record PASS "$name" "PASS  $name"
	else
		pgc_record FAIL "$name" "FAIL  $name: plan was:
$(echo "$plan" | sed 's/^/        /')"
		fail=1
	fi
}

# run an EXPLAIN and echo the integer trailing a given label line, or empty when
# the label is absent (kept from aborting the script under pipefail).
explain_num() {
	# $1 = full EXPLAIN sql, $2 = label to grep
	run_pg "$PSQL -c \"$1\"" | grep -F "$2" | grep -oE '[0-9]+$' | head -1 || true
}

q "CREATE EXTENSION pgcolumnar;" >/dev/null

# ---------------------------------------------------------------------------
# The custom scan is chosen for a plain SELECT on a columnar table.
# ---------------------------------------------------------------------------
echo "-- custom scan path"
q "CREATE TABLE t (a int, b text, c int) USING pgcolumnar;" >/dev/null
# One row group per 10000 rows, so 50000 rows form five row groups and a narrow
# range predicate skips whole row groups by their zone-map min/max.
q "SELECT pgcolumnar.set_options('t', stripe_row_limit => 10000);" >/dev/null
q "INSERT INTO t SELECT g, 'r'||g, g%7 FROM generate_series(1,50000) g;" >/dev/null
assert_plan "plain select uses custom scan" \
	"EXPLAIN (COSTS OFF) SELECT * FROM t;" "Custom Scan (PgColumnarScan)" "Seq Scan"
assert_plan "filtered select uses custom scan" \
	"EXPLAIN (COSTS OFF) SELECT a FROM t WHERE a = 12345;" \
	"Custom Scan (PgColumnarScan)" "Seq Scan"

# ---------------------------------------------------------------------------
# Qual pushdown: a filtered scan skips chunk groups the min/max rule out.
# Default chunk_group_row_limit is 10000, so 50000 rows form 5 chunk groups; a
# narrow range hits exactly one.
#
# An unfiltered count(*) or sum(a) is answered from zone-map metadata (the
# covering-count and zone-map-aggregate fast paths) and never scans chunk groups,
# so it emits no chunk-group counters. A filtered aggregate declines the metadata
# path and scans, so the read/skip counters are checked on filtered queries.
# ---------------------------------------------------------------------------
echo "-- chunk-group skipping from pushed-down quals"
EA="EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)"
# unfiltered count(*) and sum(a): answered from metadata, no chunk-group scan
cover_lines="$(run_pg "$PSQL -c \"$EA SELECT count(*) FROM t;\"" | grep -c 'Chunk Groups Total' || true)"
check "covering count(*) skips the scan" "$cover_lines" "0"
sum_lines="$(run_pg "$PSQL -c \"$EA SELECT sum(a) FROM t;\"" | grep -c 'Chunk Groups Total' || true)"
check "unfiltered sum answered from metadata" "$sum_lines" "0"
# a narrow range scans and skips all but one group; verified via sum(a) and
# filtered count(*)
skip_f="$(explain_num "$EA SELECT sum(a) FROM t WHERE a BETWEEN 12000 AND 12100;" 'Chunk Groups Removed by Filter')"
read_f="$(explain_num "$EA SELECT sum(a) FROM t WHERE a BETWEEN 12000 AND 12100;" 'Chunk Groups Read')"
check "filtered reads one group" "$read_f" "1"
check "filtered skips four groups" "$skip_f" "4"
cnt_read_f="$(explain_num "$EA SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100;" 'Chunk Groups Read')"
check "filtered count(*) also scans one group" "$cnt_read_f" "1"

# ---------------------------------------------------------------------------
# Skipping must never change results: the filtered query returns the same rows
# with pushdown on and with pushdown off.
# ---------------------------------------------------------------------------
echo "-- results identical with pushdown on vs off"
on_cnt="$(q "SET pgcolumnar.enable_qual_pushdown=on;  SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100;")"
off_cnt="$(q "SET pgcolumnar.enable_qual_pushdown=off; SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100;")"
check "pushdown-on count" "$on_cnt" "101"
check "pushdown matches off" "$on_cnt" "$off_cnt"
on_sum="$(q "SET pgcolumnar.enable_qual_pushdown=on;  SELECT sum(a) FROM t WHERE a > 49990;")"
off_sum="$(q "SET pgcolumnar.enable_qual_pushdown=off; SELECT sum(a) FROM t WHERE a > 49990;")"
check "pushdown-on sum matches off" "$on_sum" "$off_sum"
# equality on a low-cardinality column still returns every match, not just one group
check "equality full result" "$(q "SELECT count(*) FROM t WHERE c = 3;")" "$(q "SET pgcolumnar.enable_qual_pushdown=off; SELECT count(*) FROM t WHERE c = 3;")"

# ---------------------------------------------------------------------------
# Column projection: only referenced columns are read. Reported by EXPLAIN.
# ---------------------------------------------------------------------------
echo "-- column projection pushdown"
proj_one="$(explain_num "EXPLAIN (COSTS OFF) SELECT a FROM t WHERE a = 5;" 'Projected Columns')"
tot_cols="$(explain_num "EXPLAIN (COSTS OFF) SELECT a FROM t WHERE a = 5;" 'Total Columns')"
proj_star="$(explain_num "EXPLAIN (COSTS OFF) SELECT * FROM t;" 'Projected Columns')"
check "projects one column" "$proj_one" "1"
check "table has three columns" "$tot_cols" "3"
check "select star projects all" "$proj_star" "3"
# two referenced columns -> two projected
proj_two="$(explain_num "EXPLAIN (COSTS OFF) SELECT a, c FROM t WHERE a = 5;" 'Projected Columns')"
check "projects two columns" "$proj_two" "2"

# ---------------------------------------------------------------------------
# Per-table options take effect for subsequent writes.
# ---------------------------------------------------------------------------
echo "-- per-table options"
sid() { q "SELECT pgcolumnar.get_storage_id('$1');"; }

# default compression is zstd (block codec 3); overriding to none stores 0
q "CREATE TABLE o_def (a int, b text) USING pgcolumnar;" >/dev/null
q "INSERT INTO o_def SELECT g, repeat('x',200) FROM generate_series(1,20000) g;" >/dev/null
check "default uses zstd" \
	"$(q "SELECT max(block_codec) FROM pgcolumnar.column_chunk WHERE storage_id=pgcolumnar.get_storage_id('o_def');")" "3"

q "CREATE TABLE o_none (a int, b text) USING pgcolumnar;" >/dev/null
q "SELECT pgcolumnar.set_options('o_none', compression => 'none');" >/dev/null
q "INSERT INTO o_none SELECT g, repeat('x',200) FROM generate_series(1,20000) g;" >/dev/null
check "option none disables compression" \
	"$(q "SELECT max(block_codec) FROM pgcolumnar.column_chunk WHERE storage_id=pgcolumnar.get_storage_id('o_none');")" "0"

# chunk_group_row_limit option changes how many vectors a row group holds
q "CREATE TABLE o_cg (a int) USING pgcolumnar;" >/dev/null
q "SELECT pgcolumnar.set_options('o_cg', chunk_group_row_limit => 1000);" >/dev/null
q "INSERT INTO o_cg SELECT g FROM generate_series(1,5000) g;" >/dev/null
check "chunk group limit applied" \
	"$(q "SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id=pgcolumnar.get_storage_id('o_cg') AND vector_index >= 0 AND column_index = 0;")" "5"

# reset returns an option to the instance default
q "SELECT pgcolumnar.set_options('o_reset', chunk_group_row_limit => 1000);" 2>/dev/null || true
q "CREATE TABLE o_reset (a int) USING pgcolumnar;" >/dev/null
q "SELECT pgcolumnar.set_options('o_reset', chunk_group_row_limit => 1000);" >/dev/null
q "SELECT pgcolumnar.reset_options('o_reset', chunk_group_row_limit => true);" >/dev/null
q "INSERT INTO o_reset SELECT g FROM generate_series(1,5000) g;" >/dev/null
check "reset restores default limit" \
	"$(q "SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id=pgcolumnar.get_storage_id('o_reset') AND vector_index >= 0 AND column_index = 0;")" "1"

# ---- set_options and reset_options must reach the SAME verdict (#1265) -------
#
# reset_options had no guard at all -- no relam test, no relkind test, just the
# UPDATE -- so it reported success for a heap table and for a partitioned parent
# while set_options refused the identical relation. Nothing is corrupted by
# that: it reports success for something that could not have applied, which is
# the harder half to notice.
#
# THE PROPERTY IS PARITY, NOT A RELKIND LIST, and that is deliberate. It was
# written while the matview question was open, so that settling it either way
# would not invalidate the arm -- "widening one then has to widen the other to
# keep this green" was the prediction, and that is what happened.
#
# SETTLED 2026-09-25 (jd, #1265): a columnar MATERIALIZED VIEW MAY hold options,
# and the drop hook was widened in the same change so the row is cleaned up
# after one. Before that, set_options refused a matview while compact,
# vacuum_sorted and stats all accepted it, and an options row planted on a
# matview outlived the relation on all five majors.
#
# The parity arms below stay exactly as they were. What is ADDED is a verdict
# arm, because parity alone is now too weak: both entry points refusing a
# matview would still "agree", which is the state this change exists to end.
#
# "reset_options must refuse what set_options refuses" would be the same claim
# stated as a direction, and it quietly assumes set_options is the correct one.
# Symmetry does not.
p5_set_verdict() {	# p5_set_verdict REL -> accepted|refused
	q "SELECT pgcolumnar.set_options('$1', chunk_group_row_limit => 1000);" \
		>/dev/null 2>&1 && echo accepted || echo refused
}
p5_reset_verdict() {	# p5_reset_verdict REL -> accepted|refused
	q "SELECT pgcolumnar.reset_options('$1', chunk_group_row_limit => true);" \
		>/dev/null 2>&1 && echo accepted || echo refused
}
p5_agree() {	# p5_agree REL -> agree | set=X reset=Y
	local s r
	s="$(p5_set_verdict "$1")"
	r="$(p5_reset_verdict "$1")"
	[ "$s" = "$r" ] && echo agree || echo "set=$s reset=$r"
}

q "CREATE TABLE o_par_col (a int) USING pgcolumnar;" >/dev/null
q "CREATE TABLE o_par_heap (a int);" >/dev/null
q "CREATE TABLE o_par_part (a int) PARTITION BY RANGE (a);" >/dev/null
q "CREATE TABLE o_par_mvsrc (a int);" >/dev/null
q "INSERT INTO o_par_mvsrc VALUES (1),(2);" >/dev/null
q "CREATE MATERIALIZED VIEW o_par_mv USING pgcolumnar AS SELECT a FROM o_par_mvsrc;" >/dev/null

check "premise: the columnar fixture is relkind r on the columnar access method" \
	"$(q "SELECT c.relkind::text || am.amname::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam WHERE c.relname = 'o_par_col';")" \
	"rpgcolumnar"
check "premise: the heap fixture is relkind r and is NOT columnar" \
	"$(q "SELECT c.relkind::text || (am.amname <> 'pgcolumnar')::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam WHERE c.relname = 'o_par_heap';")" \
	"rtrue"
check "premise: the partitioned fixture is relkind p with no storage" \
	"$(q "SELECT c.relkind::text || c.relfilenode::text FROM pg_class c WHERE c.relname = 'o_par_part';")" \
	"p0"
check "premise: the matview fixture is relkind m on the columnar access method" \
	"$(q "SELECT c.relkind::text || am.amname::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam WHERE c.relname = 'o_par_mv';")" \
	"mpgcolumnar"

# THE CONTROL. Without it "agree" is satisfied by a build where both functions
# refuse everything, and the three parity arms below would all pass on it.
check "control: both entry points accept an ordinary columnar table" \
	"$(p5_set_verdict o_par_col)/$(p5_reset_verdict o_par_col)" "accepted/accepted"

check "set_options and reset_options agree about a columnar table" \
	"$(p5_agree o_par_col)" "agree"
check "set_options and reset_options agree about a heap table" \
	"$(p5_agree o_par_heap)" "agree"
check "set_options and reset_options agree about a partitioned parent" \
	"$(p5_agree o_par_part)" "agree"
check "set_options and reset_options agree about a columnar matview" \
	"$(p5_agree o_par_mv)" "agree"

# The verdict arm. Parity is satisfied by both refusing, which is precisely the
# behaviour #1265 changed, so the direction has to be named once it is settled.
check "both entry points accept a columnar matview (#1265, settled)" \
	"$(p5_set_verdict o_par_mv)/$(p5_reset_verdict o_par_mv)" "accepted/accepted"

# And the partitioned parent must STILL be refused. Widening to matviews is not
# widening to everything without storage: this is the arm that says so.
check "control: a partitioned parent is still refused by both" \
	"$(p5_set_verdict o_par_part)/$(p5_reset_verdict o_par_part)" "refused/refused"

# ---------------------------------------------------------------------------
# Vacuum: combine small stripes and reclaim deleted rows, returning correct
# data. stripe_row_limit=1000 makes 5 stripes; after deleting half and vacuum
# (with the limit reset), the live rows compact into a single stripe.
# ---------------------------------------------------------------------------
echo "-- vacuum compaction and space reclaim"
q "CREATE TABLE v (a int, b text) USING pgcolumnar;" >/dev/null
q "SELECT pgcolumnar.set_options('v', stripe_row_limit => 1000);" >/dev/null
q "INSERT INTO v SELECT g, 'v'||g FROM generate_series(1,5000) g;" >/dev/null
check "v starts with five stripes" "$(q "SELECT count(*) FROM pgcolumnar.stats('v');")" "5"
q "DELETE FROM v WHERE a % 2 = 0;" >/dev/null
check "v live rows after delete" "$(q "SELECT count(*) FROM v;")" "2500"
check "v deleted rows tracked" "$(q "SELECT sum(deletedrows) FROM pgcolumnar.stats('v');")" "2500"
q "SELECT pgcolumnar.reset_options('v', stripe_row_limit => true);" >/dev/null
q "SELECT pgcolumnar.vacuum('v');" >/dev/null
check "v compacts to one stripe" "$(q "SELECT count(*) FROM pgcolumnar.stats('v');")" "1"
check "v no deleted rows after vacuum" "$(q "SELECT COALESCE(sum(deletedrows),0) FROM pgcolumnar.stats('v');")" "0"
check "v rows correct after vacuum" "$(q "SELECT count(*) FROM v;")" "2500"
check "v sum correct after vacuum" "$(q "SELECT sum(a) FROM v;")" "$(q "SELECT sum(a) FROM (SELECT generate_series(1,5000) a) s WHERE a % 2 = 1;")"
check "v value intact after vacuum" "$(q "SELECT b FROM v WHERE a = 4999;")" "v4999"

# vacuum rebuilds indexes so index scans stay correct after renumbering
echo "-- vacuum rebuilds indexes"
q "CREATE TABLE vi (a int, b text) USING pgcolumnar;" >/dev/null
q "INSERT INTO vi SELECT g, 'i'||g FROM generate_series(1,20000) g;" >/dev/null
q "CREATE INDEX vi_a_idx ON vi (a);" >/dev/null
q "DELETE FROM vi WHERE a BETWEEN 100 AND 5000;" >/dev/null
q "SELECT pgcolumnar.vacuum('vi');" >/dev/null
check "vi count after vacuum" "$(q "SELECT count(*) FROM vi;")" "15099"
check "vi index point after vacuum" "$(q "SET enable_seqscan=off; SELECT b FROM vi WHERE a = 12345;")" "i12345"
check "vi index sees deletion after vacuum" "$(q "SET enable_seqscan=off; SELECT count(*) FROM vi WHERE a BETWEEN 100 AND 5000;")" "0"
check "vi index survivor after vacuum" "$(q "SET enable_seqscan=off; SELECT count(*) FROM vi WHERE a BETWEEN 1 AND 99;")" "99"

# vacuum_full across a schema
q "SELECT pgcolumnar.vacuum_full('public');" >/dev/null
check "vacuum_full keeps data" "$(q "SELECT count(*) FROM t;")" "50000"

# ---------------------------------------------------------------------------
# Plan stability: at default settings a columnar scan is the serial custom scan
# (no parallel sequential scan, and no Gather). Parallel columnar scans are now
# supported but only chosen when the planner deems them worthwhile; they are
# covered by test/parallel.sh.
# ---------------------------------------------------------------------------
echo "-- default plan is the serial custom scan"
assert_plan "columnar default uses custom scan, not seqscan" \
	"EXPLAIN (COSTS OFF) SELECT count(*) FROM t;" \
	"Custom Scan (PgColumnarScan)" "Seq Scan"
assert_plan "columnar default plan is not parallel" \
	"EXPLAIN (COSTS OFF) SELECT count(*) FROM t;" \
	"Custom Scan (PgColumnarScan)" "Gather"

# ---------------------------------------------------------------------------
# Disabling the custom scan falls back cleanly to a sequential scan.
# ---------------------------------------------------------------------------
echo "-- custom scan can be disabled"
assert_plan "disable custom scan -> seq scan" \
	"SET pgcolumnar.enable_custom_scan=off; EXPLAIN (COSTS OFF) SELECT count(*) FROM t;" \
	"Seq Scan" "Custom Scan"
check "seq-scan fallback returns correct rows" \
	"$(q "SET pgcolumnar.enable_custom_scan=off; SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100;")" "101"

echo
if [ "$fail" = "0" ]; then
	echo "checks run: $PGC_CHECKS"
	echo "PHASE 5 TEST PASSED"
else
	echo "checks run: $PGC_CHECKS"
	echo "PHASE 5 TEST FAILED"
	echo "---- server log tail ----"
	run_pg "tail -60 '$LOGFILE'" || true
fi
exit $fail

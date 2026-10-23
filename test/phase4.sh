#!/usr/bin/env bash
#
# pgColumnar phase 4 test: indexes and constraints. Covers btree and hash index
# build and scan over a columnar table, index fetch by item pointer respecting
# the delete vector, unique/primary-key constraint enforcement (across statements and
# within a single statement), NOT NULL and CHECK constraints, heap<->columnar
# conversion round-trips, and confirmation that ordinary index scans work (with
# the index-only-scan fallback verified when the feature is turned off; the
# feature itself is covered by index_only.sh).
#
# Builds and installs the extension, spins up a throwaway PostgreSQL cluster as
# the postgres OS user, and exercises the phase 4 feature set. Written fresh for
# pgColumnar; it does not reuse any upstream test file or expected-output file.
#
# Usage:
#   test/phase4.sh [PG_CONFIG]
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

WORKDIR="$(mktemp -d /tmp/pgcolumnar-phase4.XXXXXX)"
PGDATA="$WORKDIR/data"
LOGFILE="$WORKDIR/server.log"

echo "== pgColumnar phase 4 test =="
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
run_pg "createdb -p $PORT p4"

# -qAt: quiet, unaligned, tuples-only, so a value is exactly the query output.
PSQL="psql -p $PORT -d p4 -qAt -v ON_ERROR_STOP=1"
q() { run_pg "$PSQL -c \"$1\""; }
qq() { run_pg "$PSQL -c \"$1\"" | tr '\n' ',' ; }
# A READ THROUGH THE INDEX, NOT BESIDE IT (#1341). With only enable_seqscan off the
# planner answers a point lookup with the columnar custom scan, which never calls the
# table AM's index fetch, and a count with an index-only scan. Measured on 17: every
# point read below planned as Custom Scan (PgColumnarScan), and an index fetch that
# handed back the NEXT row's values passed all of them. The custom scan and the
# index-only scan are turned off here, as assert_plan turns them off, so each of
# these reads goes through pgcolumnar_index_fetch_tuple; index_only.sh covers the
# index-only scan itself.
IDX="SET enable_seqscan=off; SET pgcolumnar.enable_custom_scan=off; SET pgcolumnar.enable_index_only_scan=off;"
iq() { q "$IDX $1"; }

fail=0
# RECORDS RATHER THAN ONLY PRINTING (#965). This suite emitted human PASS lines
# and no RESULT records, so every mechanism built on the record vocabulary -- the
# ledger, the census, checks_never_observed_red, the red-observation record,
# duplicate-name detection -- was blind to all of them. The ledger did not merely
# return nothing on this log, it REFUSED it: "no RESULT records, so there is
# nothing to reconcile".
#
# THROUGH pgc_pass AND pgc_fail, NOT pgc_record (#1341). A suite that calls the
# primitive directly can have no pytest twin (test_compare_to_bash.py's
# extractor arm), and this one now has one. A failure still shows the measured
# value; a pass shows the name, as lib.sh's own `check` does.
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
		pgc_pass "$name"
	else
		pgc_fail "$name" "got [$got] want [$want]"
		fail=1
	fi
}

# assert a query succeeds and its plan (with seqscan disabled) contains/omits text
assert_plan() {
	# $1 name, $2 query, $3 must-contain, $4 must-NOT-contain
	local name="$1" sql="$2" want="$3" notwant="$4"
	local plan
	# Force the index scan: disabling only seqscan is not enough, because the
	# columnar custom scan replaces the sequential-scan path and would otherwise
	# be costed below the index scan; turning it off makes the planner pick the
	# index scan so the plan shape can be asserted.
	plan="$(run_pg "$PSQL -c \"$IDX EXPLAIN (COSTS OFF) $sql\"")"
	if grep -q "$want" <<<"$plan" && ! grep -q "$notwant" <<<"$plan"; then
		pgc_pass "$name"
		echo "        $(echo "$plan" | grep -E 'Scan' | head -1 | sed 's/^ *//')"
	else
		# The plan dump is part of the display, so the record line lands after the
		# whole thing rather than between the header and the dump. A red run's output
		# stays byte-identical that way, which is the property the green runs already
		# have.
		pgc_fail "$name" "plan was:
$(echo "$plan" | sed 's/^/        /')"
		fail=1
	fi
}

# assert a statement is rejected WITH THIS SQLSTATE (#1341). Any failure used to
# pass: a unique check that raised an internal error instead of 23505 passed both
# duplicate arms, and a backend that died on the duplicate passed as "rejected".
expect_fail() {
	local name="$1" want="$2" sql="$3" out got
	out="$(run_pg "$PSQL -v VERBOSITY=verbose -c \"$sql\"" 2>&1)" && got="succeeded" ||
		got="$({ grep -oE '^ERROR:  [0-9A-Z]{5}:' <<<"$out" || true; } | head -1 | cut -c9-13)"
	if [ "$got" = "$want" ]; then
		pgc_pass "$name"
	else
		pgc_fail "$name" "got [${got:-no SQLSTATE: $(head -1 <<<"$out")}] want [$want]"
		fail=1
	fi
}

q "CREATE EXTENSION pgcolumnar;" >/dev/null

# ---------------------------------------------------------------------------
# Btree index: build over a columnar table, then point and range index scans.
# ---------------------------------------------------------------------------
echo "-- btree index build and scan"
q "CREATE TABLE bt (a int, b text) USING pgcolumnar;" >/dev/null
q "INSERT INTO bt SELECT g, 'r'||g FROM generate_series(1,20000) g;" >/dev/null
q "CREATE INDEX bt_a_idx ON bt (a);" >/dev/null
assert_plan "btree plan is index scan" "SELECT b FROM bt WHERE a = 12345;" \
	"Index Scan" "Index Only Scan"
check "btree point"       "$(iq 'SELECT b FROM bt WHERE a = 12345;')" "r12345"
check "btree range count"  "$(iq 'SELECT count(*) FROM bt WHERE a BETWEEN 100 AND 199;')" "100"
check "btree range first"  "$(iq 'SELECT b FROM bt WHERE a BETWEEN 100 AND 199 ORDER BY a LIMIT 1;')" "r100"
check "btree miss"         "$(iq 'SELECT count(*) FROM bt WHERE a = 999999;')" "0"

# ---------------------------------------------------------------------------
# Hash index: equality lookups.
# ---------------------------------------------------------------------------
echo "-- hash index build and equality lookup"
q "CREATE TABLE ht (a int, b text) USING pgcolumnar;" >/dev/null
q "INSERT INTO ht SELECT g, 'h'||g FROM generate_series(1,20000) g;" >/dev/null
q "CREATE INDEX ht_a_hash ON ht USING hash (a);" >/dev/null
assert_plan "hash plan is index scan" "SELECT b FROM ht WHERE a = 5000;" \
	"Index Scan" "Index Only Scan"
check "hash point"    "$(iq 'SELECT b FROM ht WHERE a = 5000;')" "h5000"
check "hash point2"   "$(iq 'SELECT b FROM ht WHERE a = 17777;')" "h17777"
check "hash miss"     "$(iq 'SELECT count(*) FROM ht WHERE a = 999999;')" "0"

# ---------------------------------------------------------------------------
# Unique / primary key: reject duplicates across statements and within one.
# ---------------------------------------------------------------------------
echo "-- unique / primary key enforcement"
q "CREATE TABLE uq (id int PRIMARY KEY, v text) USING pgcolumnar;" >/dev/null
q "INSERT INTO uq VALUES (1,'a'),(2,'b'),(3,'c');" >/dev/null
check "uq initial"          "$(q 'SELECT count(*) FROM uq;')" "3"
expect_fail "uq cross-stmt dup"   23505 "INSERT INTO uq VALUES (2, 'dup');"
expect_fail "uq within-stmt dup"  23505 "INSERT INTO uq VALUES (10,'x'),(10,'y');"
check "uq unchanged"        "$(q 'SELECT count(*) FROM uq;')" "3"
# a non-conflicting insert still works, and is found by the index afterwards
q "INSERT INTO uq VALUES (4,'d');" >/dev/null
check "uq new row"          "$(iq 'SELECT v FROM uq WHERE id = 4;')" "d"
check "uq final count"      "$(q 'SELECT count(*) FROM uq;')" "4"
# a standalone UNIQUE index (not a PK) behaves the same
q "CREATE TABLE uq2 (id int, v text) USING pgcolumnar;" >/dev/null
q "CREATE UNIQUE INDEX uq2_id ON uq2 (id);" >/dev/null
q "INSERT INTO uq2 SELECT g, 'u'||g FROM generate_series(1,5000) g;" >/dev/null
expect_fail "uq2 dup"       23505 "INSERT INTO uq2 VALUES (2500, 'dup');"
check "uq2 count"           "$(q 'SELECT count(*) FROM uq2;')" "5000"

# ---------------------------------------------------------------------------
# NOT NULL and CHECK constraints (driven by the executor through the AM).
# ---------------------------------------------------------------------------
echo "-- not null and check constraints"
q "CREATE TABLE ck (a int NOT NULL, b int CHECK (b > 0)) USING pgcolumnar;" >/dev/null
q "INSERT INTO ck VALUES (1, 10), (2, 20);" >/dev/null
expect_fail "not null reject"  23502 "INSERT INTO ck VALUES (NULL, 5);"
expect_fail "check reject"     23514 "INSERT INTO ck VALUES (3, -1);"
check "ck unchanged"        "$(q 'SELECT count(*) FROM ck;')" "2"

# ---------------------------------------------------------------------------
# Index scan does not return rows marked deleted in the delete vector.
# ---------------------------------------------------------------------------
echo "-- index scan skips row-mask-deleted rows"
q "CREATE TABLE di (a int, b text) USING pgcolumnar;" >/dev/null
q "INSERT INTO di SELECT g, 'd'||g FROM generate_series(1,20000) g;" >/dev/null
q "CREATE INDEX di_a_idx ON di (a);" >/dev/null
q "DELETE FROM di WHERE a BETWEEN 5000 AND 5100;" >/dev/null
assert_plan "deleted range plan is index scan" "SELECT count(*) FROM di WHERE a BETWEEN 4900 AND 5200;" \
	"Index Scan" "Index Only Scan"
check "deleted point"      "$(iq 'SELECT count(*) FROM di WHERE a = 5050;')" "0"
check "deleted range"      "$(iq 'SELECT count(*) FROM di WHERE a BETWEEN 5000 AND 5100;')" "0"
check "survivors around"   "$(iq 'SELECT count(*) FROM di WHERE a BETWEEN 4900 AND 5200;')" "200"
check "neighbor present"   "$(iq 'SELECT b FROM di WHERE a = 4999;')" "d4999"
# update then index-fetch the new value; the old key must be gone via the index
q "UPDATE di SET a = a + 1000000 WHERE a = 8000;" >/dev/null
check "updated old gone"   "$(iq 'SELECT count(*) FROM di WHERE a = 8000;')" "0"
check "updated new found"  "$(iq 'SELECT b FROM di WHERE a = 1008000;')" "d8000"

# ---------------------------------------------------------------------------
# Heap <-> columnar conversion round-trips row counts and values.
# ---------------------------------------------------------------------------
echo "-- heap to columnar and back"
q "CREATE TABLE conv (a int, b text);" >/dev/null
q "INSERT INTO conv SELECT g, 'v'||g FROM generate_series(1,10000) g;" >/dev/null
check "conv starts heap"   "$(q "SELECT amname FROM pg_am m JOIN pg_class c ON c.relam=m.oid WHERE c.relname='conv';")" "heap"
q "SELECT pgcolumnar.alter_table_set_access_method('conv', 'pgcolumnar');" >/dev/null
check "conv now columnar"  "$(q "SELECT amname FROM pg_am m JOIN pg_class c ON c.relam=m.oid WHERE c.relname='conv';")" "pgcolumnar"
check "conv col count"     "$(q 'SELECT count(*) FROM conv;')" "10000"
check "conv col value"     "$(q 'SELECT b FROM conv WHERE a = 7777;')" "v7777"
check "conv col sum"       "$(q 'SELECT sum(a) FROM conv;')" "50005000"
q "SELECT pgcolumnar.alter_table_set_access_method('conv','heap');" >/dev/null
check "conv back heap"     "$(q "SELECT amname FROM pg_am m JOIN pg_class c ON c.relam=m.oid WHERE c.relname='conv';")" "heap"
check "conv heap count"    "$(q 'SELECT count(*) FROM conv;')" "10000"
check "conv heap value"    "$(q 'SELECT b FROM conv WHERE a = 7777;')" "v7777"
check "conv heap sum"      "$(q 'SELECT sum(a) FROM conv;')" "50005000"

# ---------------------------------------------------------------------------
# Index-only scans are a real feature now (gap 28, pgcolumnar.enable_index_only_scan,
# on by default) and are exercised comprehensively in index_only.sh. Here we only
# confirm the fallback path: with the GUC off the planner builds a plain Index
# Scan for a covering query, and that scan returns the correct value.
# ---------------------------------------------------------------------------
echo "-- index-only scan can be turned off (plain index scan fallback)"
q "CREATE TABLE ios (a int, b int) USING pgcolumnar;" >/dev/null
q "INSERT INTO ios SELECT g, g*2 FROM generate_series(1,20000) g;" >/dev/null
q "CREATE INDEX ios_a_idx ON ios (a);" >/dev/null
# With index-only scans disabled, a covering query falls back to an Index Scan.
# The custom scan is turned off so the planner picks the index scan (see assert_plan).
iosoff_plan="$(run_pg "$PSQL -c \"SET pgcolumnar.enable_index_only_scan=off; SET enable_seqscan=off; SET pgcolumnar.enable_custom_scan=off; EXPLAIN (COSTS OFF) SELECT a FROM ios WHERE a = 100;\"")"
if grep -q "Index Scan" <<<"$iosoff_plan" && ! grep -q "Index Only Scan" <<<"$iosoff_plan"; then
	pgc_pass "IOS off: plain index scan"
else
	pgc_fail "IOS off: plain index scan" "plan was:
$(echo "$iosoff_plan" | sed 's/^/        /')"; fail=1
fi
check "covering value"    "$(q 'SET enable_seqscan=off; SELECT a FROM ios WHERE a = 100;')" "100"
# a full-table scan is still available when index/bitmap scans are disabled.
# As of phase 5 the columnar full scan is the custom scan (PgColumnarScan); with
# the custom scan disabled it is a plain sequential scan. Either is acceptable.
assert_plan_seq() {
	local plan
	plan="$(run_pg "$PSQL -c \"SET enable_indexscan=off; SET enable_bitmapscan=off; EXPLAIN (COSTS OFF) SELECT * FROM ios WHERE a = 100;\"")"
	if grep -qE "Seq Scan|Custom Scan \(PgColumnarScan\)" <<<"$plan"; then
		pgc_pass "full-table scan available"
	else
		pgc_fail "full-table scan available" "$plan"; fail=1
	fi
}
assert_plan_seq

echo
if [ "$fail" = "0" ]; then
	echo "checks run: $PGC_CHECKS"
	echo "PHASE 4 TEST PASSED"
else
	echo "checks run: $PGC_CHECKS"
	echo "PHASE 4 TEST FAILED"
	echo "---- server log tail ----"
	run_pg "tail -60 '$LOGFILE'" || true
fi
exit $fail

#!/usr/bin/env bash
#
# pgColumnar phase 1 smoke test.
#
# Builds and installs the extension, spins up a throwaway PostgreSQL cluster
# as the postgres OS user, exercises create/insert/scan/drop on a columnar
# table, and checks the results. Written fresh for pgColumnar; it does not
# reuse any upstream test file or expected-output file.
#
# Usage:
#   test/smoke.sh [PG_CONFIG]
#
# PG_CONFIG defaults to /usr/local/pg17/bin/pg_config. Run as a user that may
# "runuser -u postgres" (e.g. root) when the current user is not postgres.

set -euo pipefail

# lib.sh for the check vocabulary (#965). It sources portlib.sh itself
# (lib.sh:110), so this is a superset of what was here. Its top level is
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

# A scratch area the postgres user can read and write.
WORKDIR="$(mktemp -d /tmp/pgcolumnar-smoke.XXXXXX)"
PGDATA="$WORKDIR/data"
LOGFILE="$WORKDIR/server.log"

echo "== pgColumnar smoke test =="
echo "PG_CONFIG=$PG_CONFIG"
echo "workdir=$WORKDIR"

# ---- build and install -----------------------------------------------------
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

# ---- decide how to run cluster commands ------------------------------------
# pg_regress and initdb cannot run as root; use the postgres user if we are
# root, otherwise run directly.
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

# ---- start a throwaway cluster ---------------------------------------------
echo "-- initdb"
run_pg "initdb -D '$PGDATA' -A trust" >/dev/null 2>&1
run_pg "echo \"port=$PORT\" >> '$PGDATA/postgresql.conf'"
# Preload the library so the drop-time metadata cleanup hook is installed in
# every backend (the canonical deployment for a table-AM extension).
run_pg "echo \"shared_preload_libraries='pgcolumnar'\" >> '$PGDATA/postgresql.conf'"
echo "-- start"
run_pg "pg_ctl -D '$PGDATA' -l '$LOGFILE' start -w" >/dev/null
run_pg "createdb -p $PORT smoke"

PSQL="psql -p $PORT -d smoke -At -v ON_ERROR_STOP=1"

# ---- exercise the access method --------------------------------------------
echo "-- running smoke SQL"
run_pg "$PSQL -c \"CREATE EXTENSION pgcolumnar;\"" >/dev/null
run_pg "$PSQL -c \"CREATE TABLE t (a int, b text) USING pgcolumnar;\"" >/dev/null
run_pg "$PSQL -c \"INSERT INTO t SELECT g, g::text FROM generate_series(1, 100000) g;\"" >/dev/null

TOTAL="$(run_pg "$PSQL -c \"SELECT count(*) FROM t;\"")"
FILTERED="$(run_pg "$PSQL -c \"SELECT count(*) FROM t WHERE a < 50;\"")"
FIRST3="$(run_pg "$PSQL -c \"SELECT a || '|' || b FROM t ORDER BY a LIMIT 3;\"" | tr '\n' ',')"
NULLCOUNT="$(run_pg "$PSQL -c \"SELECT count(*) FROM t WHERE b IS NULL;\"")"

# a table with a null value round-trips
run_pg "$PSQL -c \"CREATE TABLE n (a int, b text) USING pgcolumnar;\"" >/dev/null
run_pg "$PSQL -c \"INSERT INTO n VALUES (1,'x'),(2,NULL),(3,'z');\"" >/dev/null
NROWS="$(run_pg "$PSQL -c \"SELECT count(*) FROM n;\"")"
NNULL="$(run_pg "$PSQL -c \"SELECT count(*) FROM n WHERE b IS NULL;\"")"
NVAL="$(run_pg "$PSQL -c \"SELECT b FROM n WHERE a = 3;\"")"

run_pg "$PSQL -c \"DROP TABLE t;\"" >/dev/null
run_pg "$PSQL -c \"DROP TABLE n;\"" >/dev/null

# metadata rows are cleaned up on drop
ORPHANS="$(run_pg "$PSQL -c \"SELECT count(*) FROM pgcolumnar.row_group;\"")"

# ---- check -----------------------------------------------------------------
fail=0
# RECORDS RATHER THAN ONLY PRINTING (#965). This suite emitted nine human PASS
# lines and no RESULT records, so every mechanism built on the record vocabulary --
# the ledger, the census, checks_never_observed_red, the red-observation record,
# duplicate-name detection -- was blind to all nine. Measured: 0 RESULT lines.
#
# `fail` is still set, so this suite's own exit logic is untouched --
# the verdict line it prints is load-bearing until `checks run:` reconciles, and
# removing it in the same step would make the suite report less than it did before.
#
# lib.sh's own `check` (#1341). This suite used to define its own `check` around
# pgc_record, to keep a `: $got` suffix on PASS lines. A PASS line's value is the
# wanted value, which the call already states, and a FAIL line carries both either
# way. A suite-local recorder is also what the pytest twin's grading cannot read
# (test_compare_to_bash's seed arm), so the shared one is used and `fail` is
# taken from lib.sh's own PGC_FAIL below.
#
# AND THE `checks run:` LINE BELOW IS READ BY NOTHING YET, which is worth saying
# so the next conversion does not add it believing it wired something up
# (@jdatcmd, #969 review). The matrix decides whether to reconcile a suite by
# looking for the ACCOUNTING line -- `run_all_versions.sh` calls
# `pgc_log_shows_accounting`, which greps for
# `accounting: N passed + N failed + N unrunnable + N skipped = N` -- and this
# suite emits none, because it emits no accounting line. Measured: 0 accounting
# lines here against 1 in any suite that does.
#
# The line is still correct and still wanted: it is the total the records
# reconcile against, and `pgc_reconcile_records` returns 0 on this log when driven
# directly. So the remaining step is a gate flip rather than new work -- once
# whatever replaces the verdict line emits the accounting line, reconciliation
# starts working for all ten of these suites with no further change to them.

check "count(*)"            "$TOTAL"    "100000"
check "count where a<50"   "$FILTERED" "49"
check "order by a limit 3" "$FIRST3"   "1|1,2|2,3|3,"
check "no nulls in t.b"    "$NULLCOUNT" "0"
check "null table rows"    "$NROWS"    "3"
check "null table nulls"   "$NNULL"    "1"
check "null table value"   "$NVAL"     "z"
check "orphan stripes"     "$ORPHANS"  "0"

# Phase D1: the native format catalog tables exist (empty until the native
# writer, Phase D2). Confirms CREATE EXTENSION created the additive catalog.
NATIVE_TABLES="$(run_pg "$PSQL -c \"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='pgcolumnar' AND c.relkind='r' AND c.relname IN ('storage','row_group','column_chunk','zone_map');\"")"
check "native catalog tables" "$NATIVE_TABLES" "4"
fail="$PGC_FAIL"

echo
if [ "$fail" = "0" ]; then
	echo "checks run: $PGC_CHECKS"
	echo "SMOKE TEST PASSED"
else
	echo "checks run: $PGC_CHECKS"
	echo "SMOKE TEST FAILED"
	echo "---- server log tail ----"
	run_pg "tail -30 '$LOGFILE'" || true
fi
exit $fail

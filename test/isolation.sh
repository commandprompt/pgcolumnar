#!/usr/bin/env bash
#
# pgColumnar deterministic MVCC isolation tests. Runs PostgreSQL's
# pg_isolation_regress over the specs in test/isolation/specs against a throwaway
# cluster, pinning the concurrency hazards of the online compaction paths (Phase
# F3) with exact, repeatable interleavings and automatic blocking detection --
# stronger than the randomized concurrent stress suites for the precise race
# windows. Specs:
#   delete_vs_rewrite   -- a DELETE racing compact_rewrite of its group aborts
#                          with a serialization failure (H1), never lost.
#   old_snapshot_compact-- an old snapshot keeps seeing rows through a concurrent
#                          delete + compact_rewrite (H3).
#
# Usage:  test/isolation.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUTDIR="$SRCDIR/test/isolation"
PGXS="$("$PGC_PG_CONFIG" --pgxs)"
ISO="$(dirname "$PGXS")/../test/isolation/pg_isolation_regress"

if [ ! -x "$ISO" ]; then
	# pg_isolation_regress is a build artifact under src/test/isolation; a
	# packaged server-dev (as on a CI runner) does not ship it, so this suite
	# would otherwise turn green without running any of the race specs -- a silent
	# loss of exactly the coverage it exists for. Where the binary is expected to
	# be present (CI, release gate), set PGC_REQUIRE_ISOLATION to make its absence
	# a hard failure instead of a skip. Any non-empty value other than "0" arms
	# it (1, true, yes, ...), so a truthy-looking value cannot silently disarm the
	# guard -- the failure direction is safe.
	if [ -n "${PGC_REQUIRE_ISOLATION:-}" ] && [ "${PGC_REQUIRE_ISOLATION}" != 0 ]; then
		echo "FAIL  pg_isolation_regress not found at $ISO and PGC_REQUIRE_ISOLATION is set"
		echo "      install it (built under src/test/isolation) or unset PGC_REQUIRE_ISOLATION"
		exit 1
	fi
	echo "pg_isolation_regress not found at $ISO; SKIP (set PGC_REQUIRE_ISOLATION=1 to make this fatal)"
	pgc_summary
	exit 0
fi

SPECS="delete_vs_rewrite old_snapshot_compact compact_vs_reader recluster_vs_delete truncate_vs_reader truncate_vs_writer truncate_vs_truncate"
OUTDIR="$PGC_WORKDIR/iso_out"
mkdir -p "$OUTDIR"

# Run against the already-running throwaway cluster (extension preloaded and
# created by pgc_setup). pg_isolation_regress compares each permutation's output
# to test/isolation/expected/<spec>.out and writes a regression.diffs on mismatch.
#
# ONE CHECK PER SPEC. This was one check over the whole run, so a fault in one
# race window read exactly like a fault in all seven, and no ledger row could say
# which window a mutation broke. Each spec's verdict is read from the runner's
# own line for it -- "test NAME ... ok" on PG15, "ok N - NAME" from PG16 -- and a
# spec with no line at all is "not run", never ok.
ISO_LOG="$OUTDIR/runner.log"
PGHOST=127.0.0.1 PGPORT="$PGC_PORT" PGUSER=postgres \
	"$ISO" --inputdir="$INPUTDIR" --outputdir="$OUTDIR" \
		--bindir="$PGC_BINDIR" --use-existing --dbname="$PGC_DB" $SPECS > "$ISO_LOG" 2>&1
iso_rc=$?
cat "$ISO_LOG"
if [ "$iso_rc" -ne 0 ]; then
	echo "---- regression.diffs ----"
	sed 's/^/    /' "$OUTDIR/regression.diffs" 2>/dev/null | head -80
fi
spec_verdict() {  # spec -> ok | failed | not run
	if [ "$(grep -cE "^ok +[0-9]+ +- +$1( |\$)|^test $1 +\.\.\. ok" "$ISO_LOG")" -ge 1 ]; then echo ok
	elif [ "$(grep -cE "^not ok +[0-9]+ +- +$1( |\$)|^test $1 +\.\.\. FAILED" "$ISO_LOG")" -ge 1 ]; then echo failed
	else echo "not run"; fi
}
check "isolation spec delete_vs_rewrite" "$(spec_verdict delete_vs_rewrite)" "ok"
check "isolation spec old_snapshot_compact" "$(spec_verdict old_snapshot_compact)" "ok"
check "isolation spec compact_vs_reader" "$(spec_verdict compact_vs_reader)" "ok"
check "isolation spec recluster_vs_delete" "$(spec_verdict recluster_vs_delete)" "ok"
check "isolation spec truncate_vs_reader" "$(spec_verdict truncate_vs_reader)" "ok"
check "isolation spec truncate_vs_writer" "$(spec_verdict truncate_vs_writer)" "ok"
check "isolation spec truncate_vs_truncate" "$(spec_verdict truncate_vs_truncate)" "ok"

pgc_summary

#!/usr/bin/env bash
#
# pgColumnar online recluster ordered extent (#311).
#
# pgcolumnar.recluster reorders a relation under ShareUpdateExclusiveLock, so
# another session can insert while it works. Its output groups and the concurrent
# inserter's groups both take numbers above the ones it retires, and afterwards
# nothing about a group says which wrote it. That is why the rewrite reports the
# stripe ids it reserved, and why the run stops at the first live group it did
# not write.
#
# Two cheaper designs were rejected and this suite is what would catch either of
# them being adopted:
#
#   - marking through the highest live group number: counts the concurrent
#     inserter's group as ordered;
#   - marking by the row-number range the rewrite wrote: reservations interleave,
#     so a concurrent reservation lands strictly inside that range.
#
# Both over-claim, and over-claiming is the unsafe direction: it leaves a decayed
# table looking ordered, which costs every query against it.
#
# Usage:  test/recluster_extent.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

raw() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1
}

ss() { raw "SELECT $1 FROM pgcolumnar.sort_status('t');"; }

# Large enough that the rewrite takes long enough for an insert to land inside
# it. Small groups so there are many of them to reorder.
BASE=1500000
raw "CREATE TABLE t (id int, k int, v text) USING pgcolumnar;" >/dev/null
raw "SELECT pgcolumnar.set_options('t', stripe_row_limit => 20000, chunk_group_row_limit => 2048);" >/dev/null
raw "INSERT INTO t SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g FROM generate_series(1, $BASE) g;" >/dev/null

check "the table loaded" "$(raw 'SELECT count(*) FROM t;')" "$BASE"

# ------------------------------------------------------- the concurrent case

# Start the online rewrite, then insert and delete from a second session while it runs.
DELETED=1000
# pgcolumnar.maintenance_hold_ms (dev/test) holds the rewrite after it has listed
# its work set and before it locks those groups and reads, so the insert lands inside the rewrite by
# construction rather than by scheduling luck: a two-second sleep lost the race
# on a loaded CI runner.
PGOPTIONS='-c pgcolumnar.maintenance_hold_ms=5000' \
	raw "SELECT pgcolumnar.recluster('t', 'id', 'k');" >/tmp/pgc_recluster_out.$$ 2>&1 &
RECL_PID=$!

# Wait until the rewrite sits in its hold, asked of the server.
recl_count() {
	raw "SELECT count(*) FROM pg_stat_activity
	WHERE state = 'active' AND query LIKE 'SELECT pgcolumnar.recluster(''t''%' $1;"
}
RECL_HELD=0
for _ in $(seq 1 120); do
	RECL_HELD="$(recl_count "AND wait_event_type = 'Extension'")"
	[ "$RECL_HELD" = 1 ] && break
	kill -0 "$RECL_PID" 2>/dev/null || break
	sleep 0.5
done
raw "INSERT INTO t SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g FROM generate_series($((BASE + 1)), $((BASE + 50000))) g;" >/dev/null
# And a DELETE in the same window (#1633). The hold sits between the rewrite's
# group listing and its read snapshot, so these rows are in its work set but are
# already gone when it reads: a rewrite that read under the listing's snapshot
# would write them back.
raw "DELETE FROM t WHERE id <= $DELETED;" >/dev/null
# Asked again after the insert and the delete have committed: the rewrite must
# still be running.
RECL_AFTER="$(recl_count "")"

wait $RECL_PID

# The premise. If the insert committed after the rewrite had already finished,
# nothing concurrent happened and the rest of this suite proves nothing.
check "the insert ran while the rewrite was still running" \
	"$(if [ "$RECL_HELD" != 1 ]; then echo "no (the rewrite never reached its hold: $RECL_HELD)"
	   elif [ "$RECL_AFTER" != 1 ]; then echo "no (the rewrite had ended when the insert committed: $RECL_AFTER)"
	   else echo yes; fi)" "yes"
# The real premise, and the one the wall-clock comparison above cannot establish
# (#342). recluster returns the number of groups it retired, which is exactly the
# set it read. The base load is BASE rows at stripe_row_limit, so a rewrite that
# excluded the concurrent insert retires exactly that many groups; if the insert
# landed in its work set it retires more, and it then legitimately ordered those
# rows. Without this, that case fails the property check below and looks like a
# product defect instead of an unmet precondition.
RETIRED="$(cat /tmp/pgc_recluster_out.$$)"
rm -f /tmp/pgc_recluster_out.$$
check "the rewrite reported groups reclustered" \
	"$( [ "$RETIRED" -gt 0 ] 2>/dev/null && echo yes || echo no )" "yes"
check "premise: the concurrent insert was not in the rewrite's work set" \
	"$RETIRED" "$((BASE / 20000))"

check "every row is present afterwards" "$(raw 'SELECT count(*) FROM t;')" "$((BASE + 50000 - DELETED))"
# #1633: the rewrite listed its groups, then read under a later snapshot, and
# retired only the groups it listed. A row inserted in between sat in a group it
# did not list: it was rewritten AND kept. The total above can hide that against
# a lost row, so the duplicates are counted on their own.
check "no row was rewritten and also kept" \
	"$(raw 'SELECT count(*) - count(DISTINCT id) FROM t;')" "0"
check "a row deleted during the rewrite stays deleted" \
	"$(raw "SELECT count(*) FROM t WHERE id <= $DELETED;")" "0"

SORTED="$(ss sorted_rows)"
APPENDED="$(ss appended_rows)"
echo "      base $BASE rows, 50000 inserted during the rewrite; sorted $SORTED, appended $APPENDED"

# The property. The rewrite reordered the rows that existed when it started. It
# cannot have ordered the ones inserted while it ran, so the run must not claim
# more rows than it began with. A mark taken from the highest live group number
# would claim them.
# A run that recorded nothing claims nothing too: 0 sorted rows used to pass here.
# The rewrite reordered the base, so the run must claim some of it and no more.
check "the run does not claim rows written during the rewrite" \
	"$(if ! pgc_is_number "$SORTED"; then echo "no reading ($SORTED)"
	   elif [ "$SORTED" -lt 1 ]; then echo "no (the run claims no rows at all)"
	   elif [ "$SORTED" -le "$BASE" ]; then echo yes
	   else echo "no ($SORTED sorted, more than the $BASE the rewrite read)"; fi)" "yes"

# And the concurrent rows must be visible as decay rather than silently dropped
# from both counts.
check "the counts still cover every stored row" \
	"$(raw "SELECT (sorted_rows + appended_rows = (SELECT sum(rowcount) FROM pgcolumnar.stats('t')))::text
			FROM pgcolumnar.sort_status('t');")" "true"
check "the concurrent rows are reported as appended" \
	"$( [ "$APPENDED" -gt 0 ] && echo yes || echo no )" "yes"

# -------------------------------- the id-drawn-below case, deterministically

# The defect in #342: a concurrent writer draws its stripe id when it buffers its
# first row, so a writer that starts before the rewrite owns an id BELOW every id
# the rewrite draws. The rewrite's own ids stay consecutive, so a mark that is a
# bare upper bound sweeps that foreign group underneath it and counts unordered
# rows as ordered.
#
# The concurrent section above only hits this when the scheduler cooperates, which
# is why it failed on CI and not locally. This reproduces it with no timing at all:
# the writer holds its transaction open, so the ordering is forced rather than
# raced.
#
# The insert is deliberately smaller than stripe_row_limit. It draws its stripe id
# while buffering, but does not flush until commit -- so it does not hold the
# per-storage advisory lock that a flush takes to transaction end, and the rewrite
# is free to run to completion in the meantime.
raw "DROP TABLE IF EXISTS d;" >/dev/null
raw "CREATE TABLE d (id int, k int, v text) USING pgcolumnar;" >/dev/null
raw "SELECT pgcolumnar.set_options('d', stripe_row_limit => 20000, chunk_group_row_limit => 2048);" >/dev/null
raw "INSERT INTO d SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g FROM generate_series(1, 100000) g;" >/dev/null

# Session B: draw a stripe id, then hold the transaction open across the rewrite.
raw "BEGIN;
     INSERT INTO d SELECT g, 1, 'late' || g FROM generate_series(900001, 905000) g;
     SELECT pg_sleep(12);
     COMMIT;" >/dev/null &
HOLD_PID=$!
sleep 3          # let B buffer its first row, which is when its id is drawn

raw "SELECT pgcolumnar.recluster('d', 'id', 'k');" >/dev/null
wait $HOLD_PID   # B commits now, writing its group at an id below the rewrite's

D_SORTED="$(raw "SELECT sorted_rows FROM pgcolumnar.sort_status('d');")"
D_APPEND="$(raw "SELECT appended_rows FROM pgcolumnar.sort_status('d');")"
echo "      id-drawn-below: sorted $D_SORTED, appended $D_APPEND (100000 ordered + 5000 concurrent)"

# The rewrite ordered the 100000 rows that existed. The 5000 written by the held
# transaction were never read by it, so they must not be counted as ordered no
# matter where their group number falls.
check "a group numbered below the run is not counted as ordered" "$D_SORTED" "100000"
check "the rows written below the run are reported as appended" "$D_APPEND" "5000"
check "the id-drawn-below counts still cover every stored row" \
	"$(raw "SELECT (sorted_rows + appended_rows = (SELECT sum(rowcount) FROM pgcolumnar.stats('d')))::text
			FROM pgcolumnar.sort_status('d');")" "true"

# ------------------------------- a projection must not read as foreign (#345)

# The rewrite's projection fan-out writes through its own write state but draws
# stripe ids from THIS relation's counter, and records its groups under the
# projection's own storage id. So its ids interleave with the rewrite's own and
# are invisible in the base relation's group list -- exactly what a foreign
# reservation looks like. Treating them as foreign truncated the ordered run at
# the first projection flush, so a table that had just been fully reclustered
# reported almost all of itself as decayed.
#
# Both arms are identical except for the projection, so the projection is
# established as the cause rather than assumed.
# prj ARM [PROJECTION] -> "SORTED APPENDED PROJECTIONS" after a full recluster of a
# 200000-row table, with a projection added first when asked. PROJECTIONS is how many
# projections named p_ARM the catalog holds after add_projection, or "-" when none was
# asked for. One literal set of names per arm.
prj() {
	local p
	raw "DROP TABLE IF EXISTS pr_$1;" >/dev/null
	raw "CREATE TABLE pr_$1 (id int, k int, v text) USING pgcolumnar;" >/dev/null
	raw "SELECT pgcolumnar.set_options('pr_$1', stripe_row_limit => 20000);" >/dev/null
	raw "INSERT INTO pr_$1 SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g
	     FROM generate_series(1, 200000) g;" >/dev/null
	if [ -n "${2:-}" ]; then
		local e
		e="$(raw "SELECT pgcolumnar.add_projection('pr_$1', 'p_$1', ARRAY['k','id'], ARRAY['k']);")"
		case "$e" in *ERROR*) echo "      add_projection failed: $e" >&2;; esac
		# Read back from the catalog, whatever add_projection printed. One word, so a
		# failed read cannot shift the fields read after it.
		p="$(raw "SELECT count(*) FROM pgcolumnar.projection
			WHERE name = 'p_$1' AND projection_id > 0;")"
		p="${p//[[:space:]]/_}"
	else
		p=-
	fi
	raw "SELECT pgcolumnar.recluster('pr_$1', 'id', 'k');" >/dev/null
	echo "$(raw "SELECT sorted_rows FROM pgcolumnar.sort_status('pr_$1');") $(raw "SELECT appended_rows FROM pgcolumnar.sort_status('pr_$1');") ${p:-no_reading}"
}
read -r S A _ <<<"$(prj noproj)"
echo "      noproj: sorted $S, appended $A"
check "noproj: a full recluster claims every row" "$S" "200000"
check "noproj: a full recluster leaves nothing appended" "$A" "0"
read -r S A P <<<"$(prj withproj yes)"
echo "      withproj: sorted $S, appended $A"
# Without the projection, withproj is a second noproj arm, and its two checks pass
# whatever #345's merge does. add_projection's error only reached stderr.
check "withproj: add_projection created the projection" "$P" "1"
check "withproj: a full recluster claims every row" "$S" "200000"
check "withproj: a full recluster leaves nothing appended" "$A" "0"

# ---------------------------------------------------- the uncontended case

# With no concurrent writer the rewrite orders everything, so the run covers the
# whole relation. This is the control: without it, a mark that always came back
# empty would satisfy every assertion above.
raw "SELECT pgcolumnar.recluster('t', 'id', 'k');" >/dev/null
check "an uncontended rewrite claims the whole relation" \
	"$(ss appended_rows)" "0"
check "an uncontended rewrite counts every row as sorted" \
	"$(ss sorted_rows)" "$((BASE + 50000 - DELETED))"

pgc_summary

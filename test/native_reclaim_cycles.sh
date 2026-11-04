#!/usr/bin/env bash
#
# pgColumnar physical reclaim, repeated compact_rewrite cycles (Phase F).
#
# Regression guard for the free-space allocator self-conflict fixed in PR #84:
# PgColumnarAllocateFreeSpace consumed a free_space row without a
# CommandCounterIncrement, so a compact_rewrite that wrote MORE THAN ONE group in
# one command (many allocations) re-selected the just-consumed row and died with
# "tuple already updated by self". It only fires once reusable free space exists
# (the second compaction onward), so single-cycle and single-group tests miss it.
# native_reclaim.sh did not catch it because recluster advances the command
# counter between groups; compact_rewrite does not. This suite uses several small
# groups and repeated compact_rewrite so each command allocates several blocks
# from the free list.
#
# It asserts: every compact_rewrite past the first (i.e. with free space present)
# returns a group count instead of erroring, the live set always matches a heap
# mirror, and the file reaches a steady state instead of growing per cycle.
#
# Usage:  test/native_reclaim_cycles.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail

# COALESCING OFF, OR THIS SUITE CANNOT REACH THE DEFECT IT GUARDS (#1138).
# Delete the #84 fix, rebuild, and the old fixture reported 12 passed + 0 failed,
# arm for arm -- including `compact_rewrite cycle N returns a count (no
# self-conflict)`, the arm named after the defect. Found by @OffgridwithJD.
#
# THE OPERATIVE PROPERTY IS THE GUC, NOT A FRAGMENTED FREE LIST, and the first
# version of this comment said the opposite. `reclaim_coalesce` does two things:
# it merges adjacent freed ranges, AND it carries its own CommandCounterIncrement
# on the free path (columnar_metadata.c:792, `if (pgcolumnar_reclaim_coalesce)`).
# That second one does the visibility work the #84 fix would otherwise do, so
# with coalescing on the defect is masked however fragmented the list is.
#
# Isolated by freeing ALTERNATE whole groups, which fragments by non-adjacency
# and leaves the GUC at its default. On the same mutated .so:
#
#     alternate groups, coalesce=on    free list 15, rewrote 15    CLEAN
#     contiguous block, coalesce=off   free list 18, rewrote 12    tuple already
#                                                                  updated by self
#
# Fifteen fragments with coalescing on does not reach it. So coalesce=off is
# necessary and sufficient, and the free-list count is a property of the fixture
# rather than the thing that arms the suite. @OffgridwithJD separated the two
# factors; the first version of this comment would have told the next maintainer
# to preserve the wrong one.
#
# IN THE CLUSTER CONFIG, NOT A `SET`. Every psql_run here is its own session, so a
# SET would last exactly one statement and the writing session would not have it.
PGC_EXTRA_CONF="${PGC_EXTRA_CONF:-}
pgcolumnar.reclaim_coalesce=off"
export PGC_EXTRA_CONF
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# 30,000 rows in groups of 1,000, then a CONTIGUOUS block of whole groups freed
# at once. That is what puts many separate reusable ranges on the free list; a
# rotating slice frees a little from every group and coalesces back to one range.
ROWS=30000
GROUP=1000
DEL_LO=6001
DEL_HI=24000
GEN="SELECT g AS id, (g % 100) AS v, md5(g::text) AS payload FROM generate_series(1, $ROWS) g"
psql_run "CREATE TABLE h (id int, v int, payload text) WITH (autovacuum_enabled = false);"
psql_run "CREATE TABLE n (id int, v int, payload text) USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => $GROUP, chunk_group_row_limit => $GROUP);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

fsize() { q "SELECT pg_relation_size('n');"; }
hash_n() { pgc_set_hash 'SELECT id, v, payload FROM n'; }
hash_h() { pgc_set_hash 'SELECT id, v, payload FROM h'; }

free_rows() { q "SELECT count(*) FROM pgcolumnar.free_space
                 WHERE storage_id = pgcolumnar.get_storage_id('n');"; }

# SEVERAL GROUPS, OR ONE COMMAND CANNOT ALLOCATE TWICE. Read back from the
# catalog rather than assumed from the option that asked for it.
_groups="$(q "SELECT count(*) FROM pgcolumnar.storage s
              JOIN pgcolumnar.row_group rg USING (storage_id)
              WHERE s.relation_oid = 'n'::regclass;")"
check "premise: the table has several row groups to rewrite" \
	"$([ "${_groups:-0}" -ge 2 ] && echo "many ($_groups)" || echo "TOO FEW ($_groups)")" \
	"many ($_groups)"

# Free a large CONTIGUOUS block of whole groups and compact, which is what puts
# many separate reusable ranges on the free list.
#
# WAIT FOR THE HORIZON (#1979). compact retires a dead group only when no
# transaction in the database is older than the DELETE. With one call, a snapshot
# held by another session left the free list empty, and the cycles below then
# started from no reusable space. So compact runs again until it has retired all
# 18 groups, for up to 30 s, and the count is checked. The fixture tables have
# autovacuum off, because an autovacuum would retire the groups itself.
psql_run "DELETE FROM h WHERE id BETWEEN $DEL_LO AND $DEL_HI;"
psql_run "DELETE FROM n WHERE id BETWEEN $DEL_LO AND $DEL_HI;"
_dead=$(( (DEL_HI - DEL_LO + 1) / GROUP ))
_retired="$(pgc_retry_past_horizon "$_dead" "SELECT pgcolumnar.compact('n');")"
_free="$(free_rows)"
echo "  (compact retired/calls: $_retired, of $_dead)"
check_num "compact retired every dead group" "${_retired%/*}" "$_dead"

# THE PRECONDITION FOR #84, ASSERTED RATHER THAN HOPED FOR, AND IT IS THE GUC.
# Read back from the SERVER, because the value is set in the cluster config: a
# conf line that stops taking effect leaves every arm below passing on a build
# with the fix removed, which is the state this suite was in before #1138.
check "premise: coalescing is off, which is what lets this suite reach #84" \
	"$(q "SHOW pgcolumnar.reclaim_coalesce;")" "off"

# The free list's shape, PRINTED rather than asserted. It is a property of the
# fixture and not what arms the suite: 15 fragments with coalescing ON do not
# reach the defect.
echo "  (free_space rows after the block delete: $_free)"

check "initial parity" "$(hash_n)" "$(hash_h)"

# Repeated {delete a rotating slice, compact_rewrite}. No inserts, so the only
# writes are compaction's and the file must plateau once reuse kicks in.
declare -a sizes
for r in 1 2 3 4 5; do
	psql_run "DELETE FROM h WHERE id % 8 = $((r % 8));"
	psql_run "DELETE FROM n WHERE id % 8 = $((r % 8));"
	# capture stderr: the pre-fix bug surfaced as an ERROR here, which would
	# otherwise be swallowed and leave rw empty.
	rw="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "SELECT pgcolumnar.compact_rewrite('n', 0.02);" 2>&1)"
	# a healthy call returns an integer group count; the bug returned an ERROR line
	check "compact_rewrite cycle $r returns a count (no self-conflict)" \
		"$(grep -Eq '^[0-9]+$' <<<"$rw" && echo ok || echo "bad:$rw")" "ok"
	# THE COUNT IS READ, NOT ONLY ITS SHAPE (#1339). Zero is a count, so a
	# compact_rewrite that returned 0 without rewriting anything passed the arm above,
	# and so did every other arm here: parity holds while deletes stay masked, and a
	# file nothing writes never grows. Each cycle deletes one row in eight from every
	# group, which every group's 0.02 threshold admits, and #84 needs more than one
	# group rewritten in one command to reach the defect.
	check "compact_rewrite cycle $r rewrites several groups in one command, the shape #84 needs" \
		"$(grep -Eq '^[0-9]+$' <<<"$rw" && [ "$rw" -ge 2 ] && echo "several" || echo "only:$rw")" "several"
	check "parity after compact_rewrite cycle $r" "$(hash_n)" "$(hash_h)"
	sizes[$r]="$(fsize)"
done
echo "  (file sizes by cycle: ${sizes[*]})"

# Cycles 2..5 reuse the prior cycle's frees; the file must not grow past cycle 2.
check "file reaches steady state (cycle 5 <= cycle 2)" \
	"$([ "${sizes[5]}" -le "${sizes[2]}" ] && echo yes \
		|| echo "no (cycle 5 size ${sizes[5]}, cycle 2 size ${sizes[2]})")" "yes"

pgc_summary

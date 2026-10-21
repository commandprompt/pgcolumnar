#!/usr/bin/env bash
#
# pgColumnar eager rewrites record the ordering they applied (#758).
#
# pgcolumnar.storage.sorted_by and sorted_kind exist (#415) to say WHAT ordering
# the last rewrite left behind, so that a later reader can tell a lexicographic
# run from a Z-order run without guessing. Only the online recluster wrote them;
# both eager rewrites reached the catalog through record_sorted_extent(), which
# passed NIL and NULL, so an eagerly sorted table and an eagerly Z-ordered table
# were indistinguishable in the catalog.
#
# They are not the same layout. Z-order over two or more columns is not a sort
# on any one of them, and sort_status falls back to the DECLARED options.sort_by
# when storage.sorted_by is NULL -- so a Z-ordered table reported "fully sorted,
# no tail, on key {k}" while its physical order held hundreds of inversions on
# k. Anything reading that to decide an ordering (#751 pathkeys) would be
# silently wrong on LIMIT and on merge joins.
#
# THE FIXTURE MUST USE A TWO-COLUMN KEY. Single-column Z-order IS lexicographic
# order, so a one-column fixture cannot tell the two apart and every arm below
# would pass by construction. The inversion counts asserted below are what keeps
# that honest: they fail if the fixture ever stops discriminating. They are arms,
# not premises: they read the order pgcolumnar wrote, so a rewrite that sorts
# descending, or a Z-order key on the lead column only, reddens them and nothing
# else (measured). The same holds for the sort_status readings of tail and group
# counts: each one is the only check a broken count reddens.
#
# Usage:  test/eager_ordering_record.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# 5000 rows at 500 rows a chunk group and 1000 a stripe gives 5 groups: enough
# that an appended tail would be countable, few enough to read.
mk() {	# mk TABLE -- identical data in every arm, {k} declared as the sort_by
	psql_run "CREATE TABLE $1 (id int, k int, j int) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
	psql_run "INSERT INTO $1 SELECT g, (g*7919)%5000, g%13 FROM generate_series(1,5000) g;"
	psql_run "SELECT pgcolumnar.set_options('$1', sort_by => ARRAY['k']::name[]);"
}
skind() { q "SELECT coalesce(sorted_kind::text,'<NULL>') FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('$1');"; }
sby()   { q "SELECT coalesce(sorted_by::text,'<NULL>')   FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('$1');"; }
skey()  { q "SELECT coalesce(sort_key::text,'<NULL>') FROM pgcolumnar.sort_status('$1');"; }
# The same fact through the REPORTER rather than the catalog (#761). storage
# carries no GRANT and is superuser-only, so a table's owner could read the kind
# nowhere until sort_status could express it.
skindst() { q "SELECT coalesce(sorted_kind::text,'<NULL>') FROM pgcolumnar.sort_status('$1');"; }
# Inversions in the order the scan actually returns rows. This is the physical
# fact every other arm is about, and it is taken from the data, never assumed.
inv()   { q "SELECT count(*) FROM (SELECT k, lag(k) OVER () AS p FROM $1) s WHERE p > k;"; }
# differs A B WORD -- WORD when A and B are both READ and differ. A failed reading
# is empty, and an empty one differs from anything: with one side unread, the
# distinguishable and moved checks below passed (measured).
differs() {
	if [ -z "$1" ] || [ -z "$2" ]; then echo "unread [$1|$2]"
	elif [ "$1" != "$2" ]; then echo "$3"
	else echo "same [$1]"; fi
}

# ------------------------------------------------- eager lexicographic rewrite

mk lex
psql_run "SELECT pgcolumnar.vacuum_sorted('lex', 'k');"

check_num "the lexicographic rewrite really ordered the rows on k" "$(inv lex)" "0"
check "it left no unsorted tail" \
	"$(q "SELECT appended_groups FROM pgcolumnar.sort_status('lex');")" "0"
check_text "eager vacuum_sorted records the kind it applied" "$(skind lex)" "lexicographic"
check_text "and sort_status reports that kind to the table's owner (#761)" \
	"$(skindst lex)" "lexicographic"
check_text "eager vacuum_sorted records the key it applied" "$(sby lex)" "{k}"

# ------------------------------------------------------ eager Z-order rewrite

mk zo
psql_run "SELECT pgcolumnar.cluster('zo', 'k', 'j');"

# The fact the whole issue rests on: this table is NOT in k order, even
# though {k} is its declared sort_by and the run covers every group. If this
# ever reports 0 the fixture has stopped discriminating and every arm below is
# vacuous -- see the two-column note at the top.
check "the Z-ordered table is NOT ordered on k" \
	"$([ "$(inv zo)" -gt 0 ] && echo yes || echo no)" "yes"
check "it too reports a full run with no tail" \
	"$(q "SELECT appended_groups FROM pgcolumnar.sort_status('zo');")" "0"
check_text "eager cluster records the kind it applied" "$(skind zo)" "zorder"
check_text "and sort_status reports THAT kind, not the lexicographic one (#761)" \
	"$(skindst zo)" "zorder"
check_text "eager cluster records the key it applied" "$(sby zo)" "{k,j}"

# The consequence, stated as the discrimination it buys: two tables that report
# the same run extent and the same declared key must no longer look the same.
check "the two eager layouts are now distinguishable in the catalog" \
	"$(differs "$(skind lex)" "$(skind zo)" yes)" "yes"
check_text "sort_status reports the APPLIED key on the Z-ordered table, not the declared {k}" \
	"$(skey zo)" "{k,j}"
check_text "sort_status still reports {k} on the lexicographically sorted table" \
	"$(skey lex)" "{k}"

# The point of #761, stated as the discrimination it buys. sort_key is {k} on one
# table and {k,j} on the other here, but a one-column Z-order would report the
# same key as a sort, and the reader could still not tell them apart. Only the
# kind can.
check "the two layouts are distinguishable through sort_status alone (#761)" \
	"$(differs "$(skindst lex)" "$(skindst zo)" yes)" "yes"

# ------------------------------------------- the self-gate the record unblocks
#
# #415's gate skips a full reorg when the relation is already exactly the
# Z-order run over the requested columns. It requires sorted_kind = 'zorder',
# so after an eager cluster it could never fire and recluster rewrote all five
# groups of an already-clustered table.

# A return of 0 is NOT evidence that the gate fired. It is also what an empty
# relation, a single group, an early return on some unrelated condition, or a
# return value simply not wired to the gate would produce. So the arms below
# corroborate with the LAYOUT: the exact set of (group_number, file_offset)
# rows in pgcolumnar.row_group. A gate that fired rewrote nothing and that set
# is byte-identical; a fall-through retires every group and writes new ones, so
# the set always moves. The return value is asserted too, as the secondary.
layout() { q "SELECT coalesce(md5(string_agg(group_number || ':' || file_offset, ',' ORDER BY group_number)), 'EMPTY') FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('$1');"; }

mk gate
psql_run "SELECT pgcolumnar.cluster('gate', 'k', 'j');"
# So that "nothing changed" cannot be true because there was nothing there: the
# fixture must have live rows in more than one group.
check "the gate fixture has more than one group" \
	"$([ "$(q "SELECT total_groups FROM pgcolumnar.sort_status('gate');")" -gt 1 ] && echo yes || echo no)" "yes"
check_num "premise: and live rows in them" "$(q 'SELECT count(*) FROM gate;')" "5000"

GATE_BEFORE="$(layout gate)"
check "recluster on the key the eager cluster already applied does nothing" \
	"$(q "SELECT pgcolumnar.recluster('gate', 'k', 'j');")" "0"
check_text "and it rewrote no group: the physical layout is unchanged" \
	"$(layout gate)" "$GATE_BEFORE"

# Removal proof for that arm: the gate must DISCRIMINATE, not always skip. A
# different key on the same table has to fall through to the full rewrite, or
# both arms above would be satisfied by a gate that never looks at anything.
check "control: recluster on a DIFFERENT key still rewrites every group" \
	"$(q "SELECT pgcolumnar.recluster('gate', 'j', 'k');")" "5"
check "control: and that rewrite really moved the layout" \
	"$(differs "$(layout gate)" "$GATE_BEFORE" moved)" "moved"

# The other half of the gate condition: nothing appended past the run. One row
# inserted after the cluster must defeat it, on the same key.
mk tailgate
psql_run "SELECT pgcolumnar.cluster('tailgate', 'k', 'j');"
psql_run "INSERT INTO tailgate VALUES (90001, 42, 1);"
# Read AFTER the insert, just before the recluster: read before it, the insert's
# own new group moved the layout, and "moved" passed with recluster skipping
# (measured, with the gate blind to the tail).
TAIL_BEFORE="$(layout tailgate)"
check "the insert appended past the recorded run" \
	"$([ "$(q "SELECT appended_groups FROM pgcolumnar.sort_status('tailgate');")" -gt 0 ] && echo yes || echo no)" "yes"
check "control: recluster with an appended tail does not skip" \
	"$([ "$(q "SELECT pgcolumnar.recluster('tailgate', 'k', 'j');")" -gt 0 ] && echo yes || echo no)" "yes"
check "control: and it moved the tailgate layout" \
	"$(differs "$(layout tailgate)" "$TAIL_BEFORE" moved)" "moved"

# And the mirror: a lexicographic run is not a Z-order run, so recluster must
# not skip it however the key matches.
mk lexgate
psql_run "SELECT pgcolumnar.vacuum_sorted('lexgate', 'k');"
LEXGATE_BEFORE="$(layout lexgate)"
check "control: recluster does not skip a LEXICOGRAPHIC run of the same lead column" \
	"$(q "SELECT pgcolumnar.recluster('lexgate', 'k');")" "5"
check "control: and it moved the lexgate layout" \
	"$(differs "$(layout lexgate)" "$LEXGATE_BEFORE" moved)" "moved"

# ------------------------------------------------- an unsorted rewrite records nothing

mk un
psql_run "SELECT pgcolumnar.vacuum_sorted('un', 'k');"
check_text "the mark is set before the unsorted rewrite" "$(skind un)" "lexicographic"
psql_run "SELECT pgcolumnar.vacuum('un');"
check_text "an unsorted rewrite claims no ordering" "$(skind un)" "<NULL>"
check_text "and sort_status reports no kind either, rather than a stale one (#761)" \
	"$(skindst un)" "<NULL>"
check_text "and records no key" "$(sby un)" "<NULL>"

# --------------------------------------------- the declared key is what is applied

mk dec
# No explicit columns: vacuum_sorted falls back to the declared sort_by.
psql_run "SELECT pgcolumnar.vacuum_sorted('dec');"
check_text "vacuum_sorted with no columns records the DECLARED key it applied" \
	"$(sby dec)" "{k}"
check_text "and records it as a lexicographic run" "$(skind dec)" "lexicographic"

pgc_summary

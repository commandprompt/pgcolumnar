#!/usr/bin/env bash
#
# pgColumnar: EXPLAIN must report the pushdown the scan performs, not the
# pushdown the planner offered it (#191).
#
# pgcolumnar_enable_qual_pushdown gates pgcolumnar_build_predicates in
# PgColumnarBeginRead, so with the setting off the reader builds no predicates and
# skips no chunk groups. cstate->nScanKeys is the planner's count and does not
# move, so EXPLAIN reported the same "Columnar Pushed-Down Filters: 1" either
# way -- telling someone who had just turned the setting off to test a theory
# that it had not taken effect.
#
# The controls below are the point of the file. Reporting 0 is easy to fake: a
# fix that reported 0 unconditionally, or that broke pushdown outright, would
# pass the headline check. So each reported number is paired with an observable
# consequence in the same plan -- rows removed by the executor's filter, and
# chunk groups skipped by the reader.
#
# Usage:  test/pushdown_report.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_PUSHDOWN_ROWS:-200000}

# One matching row in a table many chunk groups wide, so that skipping has
# something substantial to skip and its absence is equally visible.
psql_run "DROP TABLE IF EXISTS pdr;
	CREATE TABLE pdr (id int, k int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('pdr', chunk_group_row_limit => 1000);" >/dev/null
psql_run "INSERT INTO pdr SELECT g, g FROM generate_series(1, $ROWS) g;" >/dev/null

# EXPLAIN ANALYZE of the same query under each setting. COSTS/TIMING off so the
# only things that vary are the counters we are asserting on.
plan() {
	q "SET pgcolumnar.enable_qual_pushdown = $1;
	   EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT * FROM pdr WHERE id = 1;"
}

field() { echo "$1" | grep -oE "$2: [0-9]+" | head -1 | grep -oE '[0-9]+$'; }

# Is this plan the scalar columnar custom scan, and not something else?
#
# "Columnar Projected Columns" is that node's own marker: the vectorized
# aggregate node does not report it, and no other node reports it at all. A
# positive grep for the node's marker is a stronger test than an absence test,
# because a plan that fell back to a seq scan has no Columnar lines to be absent.
is_scalar_scan() {
	grep -q 'Columnar Projected Columns' <<<"$1" && echo yes || echo no
}

on="$(plan on)"
off="$(plan off)"

# --- 0. the node, before any number read from it --------------------------------

# Every check below reads a counter out of these two plans and believes it. If
# the planner ever stops choosing the columnar custom scan here -- a costing
# change, a new path, a GUC default -- those counters go missing or come from
# somewhere else, and a suite that only compared numbers would report a fix
# where there was a plan change. This has to fail before anything is read.
check "the plan under test is a columnar custom scan" "$(is_scalar_scan "$on")" "yes"

# The setting under test must not be changing which node runs; if it did, every
# comparison between the two plans below would be comparing different things.
check "and it is still one with pushdown off" "$(is_scalar_scan "$off")" "yes"

# --- 1. the reported number follows the setting ---------------------------------

check "pushdown on reports a pushed-down filter" \
	"$(field "$on" 'Columnar Pushed-Down Filters')" "1"

check "pushdown off reports none" \
	"$(field "$off" 'Columnar Pushed-Down Filters')" "0"

# --- 2. and the report matches what the scan actually did -----------------------

# With pushdown on the reader skips chunk groups; with it off it reads them all.
# This is what makes the reported 0 truthful rather than merely different.
skipped_on="$(field "$on" 'Columnar Chunk Groups Removed by Filter')"
skipped_off="$(field "$off" 'Columnar Chunk Groups Removed by Filter')"

check "pushdown on skips chunk groups" \
	"$([ "${skipped_on:-0}" -gt 0 ] && echo yes || echo no)" "yes"

check "pushdown off skips none" "${skipped_off:-unset}" "0"

# --- 3. the setting really is doing something (not just being reported) ---------

# The executor's own filter has to remove everything the reader did not skip. If
# a "fix" disabled pushdown rather than reporting it honestly, this check would
# not notice -- but check 3 above would, which is why both are here.
removed_on="$(field "$on" 'Rows Removed by Filter')"
removed_off="$(field "$off" 'Rows Removed by Filter')"

# A plan that ran may filter nothing at all, so a missing line on the on side
# reads as 0. An empty on plan is a failed read, not 0: it used to pass here.
check "the executor filters far more rows with pushdown off" \
	"$(if [ -z "$on" ]; then echo "no reading (the pushdown-on plan is empty)"
	   elif [ "${removed_off:-0}" -gt "${removed_on:-0}" ] 2>/dev/null; then echo yes
	   else echo "no (off removed [$removed_off], on removed [$removed_on])"; fi)" "yes"

check "pushdown off examines every row" "${removed_off:-unset}" "$((ROWS - 1))"

# --- 4. the query still returns the right answer either way ---------------------

# A pushdown that skipped a group it should have read would show up here and
# nowhere else in this file.
check "the answer is unchanged with pushdown on" \
	"$(q "SET pgcolumnar.enable_qual_pushdown = on;
		SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pdr WHERE id BETWEEN 5000 AND 5100;" | tail -1)" \
	"101/510050"

check "the answer is unchanged with pushdown off" \
	"$(q "SET pgcolumnar.enable_qual_pushdown = off;
		SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pdr WHERE id BETWEEN 5000 AND 5100;" | tail -1)" \
	"101/510050"

# --- 5. more than one qual is counted, so the number is a count not a flag ------

two="$(q "SET pgcolumnar.enable_qual_pushdown = on;
	EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	SELECT * FROM pdr WHERE id > 100 AND id < 200;")"

check "two quals report two pushed-down filters" \
	"$(field "$two" 'Columnar Pushed-Down Filters')" "2"

# --- 6. a filter that is pushed down but cannot exclude anything (#479) ---------
#
# "Columnar Pushed-Down Filters" counts the scan keys the reader was HANDED.
# The reader then converts each into a skip predicate and can drop one, and a
# dropped key excludes no chunk group at all while the line above still reports
# it as pushed down. That is how #477 stayed invisible for a year: a bigint
# column against a bare integer literal reported
#
#     Columnar Pushed-Down Filters: 1
#     Columnar Chunk Groups Removed by Filter: 0
#
# which reads as "pushdown works, this predicate is just not selective" and
# actually meant "the predicate was never usable". test/zonemap_cost.sh sat in
# exactly that state for its whole life.
#
# "Columnar Usable Skip Predicates" is the second number: how many predicates the
# reader built and can exclude with. The two together say which case you are in.
#
# THE UNUSABLE ARM IS CONSTRUCTED, AND HAS TO BE. This suite first used a domain
# column, which was unusable because of a defect (#483) rather than by nature.
# That defect is fixed, so a domain now prunes exactly as its base type does and
# is no use here. Nothing in core replaces it: asked directly, the catalog has NO
# cross-type btree operator lacking an ordering proc for its pair, which the
# premise below asserts rather than assumes.
#
# So the condition is built on purpose, out of one operator and one opfamily
# entry, and it reproduces precisely the shape #477 had in production: a
# cross-type btree operator whose family has no BTORDER proc for the pair, so
# pgcolumnar_clause_to_scankey builds a key and pgcolumnar_make_predicates drops
# it.
#
# Two details are load-bearing and were each found by the fixture failing:
#
#   The right-hand type must be NON-COLLATABLE. With `text` on the right, the
#   operator's inputcollid is the default collation while a bigint column's
#   attcollation is 0, and clause_to_scankey refuses the clause on that mismatch
#   before any key is built. Both counters then read 0 and there is nothing to
#   tell apart. `oid` has no collation, so the clause survives that guard.
#
#   The function must NOT be inlinable. Written in SQL it was inlined, and the
#   qual the planner handed the scan was the built-in `>` against a bigint
#   constant, which prunes perfectly well. The EXPLAIN said
#   `Filter: (dz.b > '190000'::bigint)` and the arm quietly tested nothing.
#   plpgsql is not inlined.
psql_run "DROP TABLE IF EXISTS pdr_u;
	CREATE TABLE pdr_u (plain int, b bigint) USING pgcolumnar;
	SELECT pgcolumnar.set_options('pdr_u', stripe_row_limit => 10000);" >/dev/null
psql_run "INSERT INTO pdr_u SELECT g, g FROM generate_series(1, $ROWS) g;" >/dev/null

cat > "$PGC_SQLDIR/pdr_unusable.sql" <<'SQL'
CREATE FUNCTION pdr_gt_oid(bigint, oid) RETURNS boolean
  LANGUAGE plpgsql IMMUTABLE AS $b$ BEGIN RETURN $1 > ($2::bigint); END $b$;
CREATE OPERATOR >>> (LEFTARG = bigint, RIGHTARG = oid, FUNCTION = pdr_gt_oid);
ALTER OPERATOR FAMILY integer_ops USING btree ADD OPERATOR 5 >>> (bigint, oid);
SQL
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -f "$PGC_SQLDIR/pdr_unusable.sql" >/dev/null 2>&1

# The premise the whole section rests on: the family really has no ordering proc
# for this pair, which is WHY the reader must drop the key. Without this, "0
# usable" could be any other cause and the section would be naming the wrong one.
check "premise: integer_ops has no ordering proc for (bigint, oid)" \
	"$(q "SELECT count(*) FROM pg_amproc ap
		JOIN pg_opfamily f ON f.oid = ap.amprocfamily
		WHERE f.opfname = 'integer_ops'
		  AND ap.amproclefttype = 'int8'::regtype
		  AND ap.amprocrighttype = 'oid'::regtype
		  AND ap.amprocnum = 1")" "0"

# And the reason this had to be constructed at all. If core ever ships a
# cross-type btree operator without an ordering proc, an ordinary fixture exists
# again and this section should use it instead of the operator above.
check "premise: and core has no such pair of its own, which is why this is built" \
	"$(q "SELECT count(*) FROM pg_amop ao
		JOIN pg_am am ON am.oid = ao.amopmethod AND am.amname = 'btree'
		WHERE ao.amoplefttype <> ao.amoprighttype
		  AND ao.amopstrategy BETWEEN 1 AND 5
		  AND ao.amopfamily <> (SELECT oid FROM pg_opfamily WHERE opfname = 'integer_ops'
		                        AND opfmethod = am.oid)
		  AND NOT EXISTS (SELECT 1 FROM pg_amproc ap
		                   WHERE ap.amprocfamily = ao.amopfamily
		                     AND ap.amproclefttype = ao.amoplefttype
		                     AND ap.amprocrighttype = ao.amoprighttype
		                     AND ap.amprocnum = 1)")" "0"

uplan() {  # uplan <qual>
	q "SET pgcolumnar.enable_qual_pushdown = on;
	   EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM pdr_u WHERE $1;"
}

usable="$(uplan "plain > $((ROWS - 10000))")"
unusable="$(uplan "b >>> $((ROWS - 10000))::oid")"

# The node again, before any number is read out of either plan.
check "the usable arm is a columnar custom scan" "$(is_scalar_scan "$usable")" "yes"
check "the unusable arm is a columnar custom scan" "$(is_scalar_scan "$unusable")" "yes"

# The premise that makes the rest mean something: these two arms really do prune
# differently. Without it, "1 usable" against "0 usable" could be two labels on
# identical behaviour.
check "the usable arm removes chunk groups" \
	"$([ "$(field "$usable" 'Columnar Chunk Groups Removed by Filter')" -gt 0 ] \
		&& echo yes || echo no)" "yes"
check "the unusable arm removes none" \
	"$(field "$unusable" 'Columnar Chunk Groups Removed by Filter')" "0"

# A dropped predicate must cost speed and nothing else. The executor re-applies
# the qual, so the answer is the same either way, and if it were not this suite
# would be reporting a correctness bug as a reporting one.
check "and the unusable arm still returns the right answer" \
	"$(q "SELECT count(*) FROM pdr_u WHERE b >>> $((ROWS - 10000))::oid;")" "10000"

# Both are reported as pushed down. This is the defect: the old line alone cannot
# tell these two plans apart.
check "both arms report the filter as pushed down" \
	"$(field "$usable" 'Columnar Pushed-Down Filters')/$(field "$unusable" 'Columnar Pushed-Down Filters')" \
	"1/1"

# And the new line does tell them apart.
check "the usable arm reports one usable skip predicate" \
	"$(field "$usable" 'Columnar Usable Skip Predicates')" "1"
check "the unusable arm reports none" \
	"$(field "$unusable" 'Columnar Usable Skip Predicates')" "0"

# With pushdown off the reader builds no predicates at all, so the new line must
# follow the setting too, or it would report a capability the run did not have,
# which is the #191 complaint about the old line.
check "pushdown off reports no usable skip predicates" \
	"$(field "$off" 'Columnar Usable Skip Predicates')" "0"

# --- 7. the two vectorized aggregate nodes report it too (#479) -----------------
#
# "Columnar Pushed-Down Filters" is printed by three nodes, not one: the scalar
# scan above, and both vectorized aggregate nodes, which fill it from
# PgColumnarCountConvertibleQuals -- the same built-key count, with the same gap
# under it. Leaving two of the three unfixed would make one line of plan text
# mean two different things depending on which node ran.
#
# Both nodes are opt-in, so each arm asserts its own node fired FIRST. Without
# that, a query the node quietly declines falls back to core Agg over the scalar
# scan, which reports the new line correctly, so the check would pass while
# testing the node it was written for not at all.
aggplan() {  # aggplan <guc> <sql>
	q "SET pgcolumnar.enable_qual_pushdown = on;
	   SET $1 = on;
	   EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $2;"
}
has() { case "$1" in *"$2"*) echo yes ;; *) echo no ;; esac; }

UAGG=pgcolumnar.enable_ungrouped_vector_agg
u_usable="$(aggplan $UAGG "SELECT count(*), sum(b) FROM pdr_u WHERE plain > $((ROWS - 10000))")"
u_unusable="$(aggplan $UAGG "SELECT count(*), sum(b) FROM pdr_u WHERE b >>> $((ROWS - 10000))::oid")"

check "the ungrouped vectorized aggregate node ran (usable arm)" \
	"$(has "$u_usable" 'Columnar Vectorized Aggregates')" "yes"
check "the ungrouped vectorized aggregate node ran (unusable arm)" \
	"$(has "$u_unusable" 'Columnar Vectorized Aggregates')" "yes"

check "ungrouped: the usable arm removes chunk groups" \
	"$([ "$(field "$u_usable" 'Columnar Chunk Groups Removed by Filter')" -gt 0 ] \
		&& echo yes || echo no)" "yes"
check "ungrouped: the unusable arm removes none" \
	"$(field "$u_unusable" 'Columnar Chunk Groups Removed by Filter')" "0"

# ---- plain EXPLAIN must report the count too (#726) -------------------------
# Every arm in this file so far uses EXPLAIN ANALYZE, and that is exactly how
# this went unnoticed. The ungrouped node assigns nscankeys and npreds AFTER its
# EXEC_FLAG_EXPLAIN_ONLY return, so under ANALYZE the numbers are right and under
# a plain EXPLAIN they are still their palloc0 zero -- whatever the truth is.
#
# It is the #602 shape on a different line: a plain-EXPLAIN value that was never
# computed. `Columnar Pushed-Down Filters` is a line people read to decide
# whether pushdown is working, and a hard zero reads as "pushdown is not
# happening" rather than "this number was not filled in". It is also the line a
# pushdown measurement pins its premise on, and a premise that is always zero
# cannot fail for the right reason.
#
# The GROUPED node has always done this correctly, so the two vectorized nodes
# disagreed about the same label -- which is what #493 exists to prevent.
plainplan() {  # plainplan <guc> <sql>
	q "SET pgcolumnar.enable_qual_pushdown = on;
	   SET $1 = on;
	   EXPLAIN (COSTS OFF) $2;"
}
PDR_Q="SELECT count(*), sum(b) FROM pdr_u WHERE plain > $((ROWS - 10000))"
u_plain="$(plainplan $UAGG "$PDR_Q")"

check "the plain EXPLAIN arm is the ungrouped vectorized aggregate" \
	"$(has "$u_plain" 'Columnar Vectorized Aggregates')" "yes"
# The premise that makes a zero meaningful: under ANALYZE the number is NOT zero,
# so a zero on the plain plan is a value that was never filled in rather than an
# honest report of no pushdown.
check "the same query under ANALYZE reports a non-zero count" \
	"$([ "$(field "$u_usable" 'Columnar Pushed-Down Filters')" -gt 0 ] && echo yes || echo no)" "yes"

check_num "plain EXPLAIN reports the ungrouped node's pushed-down filters (#726)" \
	"$(field "$u_plain" 'Columnar Pushed-Down Filters')" \
	"$(field "$u_usable" 'Columnar Pushed-Down Filters')"
check_num "plain EXPLAIN reports the ungrouped node's vector predicates (#726)" \
	"$(field "$u_plain" 'Columnar Vector Predicates')" \
	"$(field "$u_usable" 'Columnar Vector Predicates')"

# And the two vectorized nodes must agree with each other on a plain plan, which
# is the property that was actually broken: one label, one quantity, every node.
# The same shape the grouped arms below already use, so the node is genuinely
# planned. Written first with a GROUP BY on a column this table does not have,
# which errored, reported "no such node", and SKIPPED -- a check that proves
# nothing while looking like coverage. The node assertion below is what turns
# that into a failure instead of a shrug.
g_plain="$(plainplan pgcolumnar.enable_group_vectorization \
	"SELECT b, count(*) FROM pdr_u WHERE plain > $((ROWS - 10000)) GROUP BY 1")"
check "the grouped node IS planned for the cross-node arm" \
	"$(has "$g_plain" 'Columnar Vectorized Group Keys')" "yes"
check_num "plain EXPLAIN: the grouped node agrees with the ungrouped one (#493, #726)" \
	"$(field "$g_plain" 'Columnar Pushed-Down Filters')" \
	"$(field "$u_plain" 'Columnar Pushed-Down Filters')"

# One label, one quantity, on every node (#493).
#
# "Columnar Pushed-Down Filters" used to come from PgColumnarCountConvertibleQuals
# on these two nodes -- quals convertible to a VECTOR predicate -- and from
# pgcolumnar_clause_to_scankey on the scalar node. The two tests do not accept the
# same clauses, so the same line reported two different quantities depending on a
# plan choice the reader did not make: "Pushed-Down Filters went from 1 to 0" read
# as a pushdown regression and could mean the planner switched to a vectorized
# aggregate, which is usually a speedup.
#
# The remedy is #479's, which this issue is a repeat of: add the second number,
# do not redefine the first. All three nodes now report scan keys under the old
# label, and the vector-predicate count has its own line where it applies.
#
# The `>>>` operator below is the discriminator: it builds a scan key and is not
# convertible to a vector predicate, so before the fix the scalar node said 1 and
# these two said 0. Asserting the three agree is the whole of this issue.
#
# The practical consequence here is that the constructed operator is not
# convertible to a vector predicate either, so this arm reads 0 on BOTH counters
# and cannot discriminate on these nodes the way it does on the scalar scan.
# Asserted as what it is rather than dressed up as a comparison that works.
check "ungrouped: the usable arm reports one of each, agreeing" \
	"$(field "$u_usable" 'Columnar Pushed-Down Filters')/$(field "$u_usable" 'Columnar Usable Skip Predicates')" \
	"1/1"
check "ungrouped: and the unusable arm builds no skip predicate" \
	"$(field "$u_unusable" 'Columnar Usable Skip Predicates')" "0"

GAGG=pgcolumnar.enable_group_vectorization
g_usable="$(aggplan $GAGG "SELECT b, count(*) FROM pdr_u WHERE plain > $((ROWS - 10000)) GROUP BY 1")"
g_unusable="$(aggplan $GAGG "SELECT plain, count(*) FROM pdr_u WHERE b >>> $((ROWS - 10000))::oid GROUP BY 1")"

# "Columnar Vectorized Group Keys" is this node's own marker; no other node emits
# it, so a positive grep proves the node rather than an absence a fallback would
# also satisfy.
check "the grouped vectorized aggregate node ran (usable arm)" \
	"$(has "$g_usable" 'Columnar Vectorized Group Keys')" "yes"
check "the grouped vectorized aggregate node ran (unusable arm)" \
	"$(has "$g_unusable" 'Columnar Vectorized Group Keys')" "yes"

check "grouped: the usable arm removes chunk groups" \
	"$([ "$(field "$g_usable" 'Columnar Chunk Groups Removed by Filter')" -gt 0 ] \
		&& echo yes || echo no)" "yes"
check "grouped: the unusable arm removes none" \
	"$(field "$g_unusable" 'Columnar Chunk Groups Removed by Filter')" "0"

# Same caveat as the ungrouped node above: this label is a different quantity
# here, so the arms assert agreement on the usable side and an absent skip
# predicate on the unusable one, which is what is actually true.
check "grouped: the usable arm reports one of each, agreeing" \
	"$(field "$g_usable" 'Columnar Pushed-Down Filters')/$(field "$g_usable" 'Columnar Usable Skip Predicates')" \
	"1/1"
check "grouped: and the unusable arm builds no skip predicate" \
	"$(field "$g_unusable" 'Columnar Usable Skip Predicates')" "0"

# ---- one emitter per shared statistic (#495) --------------------------------
#
# Five of these lines are printed by every scan node -- the scalar custom scan and
# the two vectorized aggregates -- and each used to print its own copy. #484 had
# to add "Usable Skip Predicates" in three places for that reason, and its own
# rationale says why it could not do fewer: "fixing one would leave a line of plan
# text meaning two different things depending on which node ran."
#
# Unifying three call sites into one is only durable if a FOURTH is detectable.
# Otherwise the next node grows its own copy, the suite stays green, and the three
# reappear exactly as they arose. So this asserts the count at the source, which
# is the only place a new caller is visible before it has drifted.
#
# A grep over source rather than a behavioural check on purpose: the failure being
# guarded is "someone wrote a second emitter", which is a property of the code and
# not of any plan. The behavioural half -- that the nodes agree -- is the checks
# above.
PGC_SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"
check "exactly one emitter for Columnar Pushed-Down Filters (#495)" \
	"$(grep -rho 'ExplainPropertyInteger("Columnar Pushed-Down Filters"' "$PGC_SRC_DIR" | wc -l | tr -d ' ')" \
	"1"
check "exactly one emitter for Columnar Usable Skip Predicates (#495)" \
	"$(grep -rho 'ExplainPropertyInteger("Columnar Usable Skip Predicates"' "$PGC_SRC_DIR" | wc -l | tr -d ' ')" \
	"1"
check "exactly one emitter for Columnar Chunk Groups Total (#495)" \
	"$(grep -rho 'ExplainPropertyInteger("Columnar Chunk Groups Total"' "$PGC_SRC_DIR" | wc -l | tr -d ' ')" \
	"1"
check "exactly one emitter for Columnar Chunk Groups Read (#495)" \
	"$(grep -rho 'ExplainPropertyInteger("Columnar Chunk Groups Read"' "$PGC_SRC_DIR" | wc -l | tr -d ' ')" \
	"1"
check "exactly one emitter for Columnar Chunk Groups Removed by Filter (#495)" \
	"$(grep -rho 'ExplainPropertyInteger("Columnar Chunk Groups Removed by Filter"' "$PGC_SRC_DIR" | wc -l | tr -d ' ')" \
	"1"

# The three nodes, same table, same predicate, same label.
pdr_scalar="$(q "SET pgcolumnar.enable_qual_pushdown = on;
	SET pgcolumnar.enable_ungrouped_vector_agg = off;
	SET pgcolumnar.enable_group_vectorization = off;
	EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	SELECT count(*), sum(b) FROM pdr_u WHERE b >>> $((ROWS - 10000))::oid;")"

check "the scalar arm really is the scalar scan (#493)" \
	"$(has "$pdr_scalar" 'Columnar Projected Columns')" "yes"

check_num "scalar and ungrouped agree on Pushed-Down Filters (#493)" \
	"$(field "$u_unusable" 'Columnar Pushed-Down Filters')" \
	"$(field "$pdr_scalar" 'Columnar Pushed-Down Filters')"
check_num "scalar and grouped agree on Pushed-Down Filters (#493)" \
	"$(field "$g_unusable" 'Columnar Pushed-Down Filters')" \
	"$(field "$pdr_scalar" 'Columnar Pushed-Down Filters')"

# and the quantity that used to be printed under that label still exists, under
# its own name, on the nodes it applies to -- otherwise this trades one
# asymmetry for a loss of information.
check "the ungrouped node reports its vector-predicate count separately (#493)" \
	"$(has "$u_unusable" 'Columnar Vector Predicates')" "yes"
check "and it is the count that used to be mislabelled (#493)" \
	"$(field "$u_unusable" 'Columnar Vector Predicates')" "0"

check "exactly one emitter for Columnar Vector Predicates (#495)" \
	"$(grep -rho "ExplainPropertyInteger(\"Columnar Vector Predicates\"" "$PGC_SRC_DIR" | wc -l | tr -d ' ')" \
	"1"

pgc_summary

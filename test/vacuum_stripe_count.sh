#!/usr/bin/env bash
#
# pgColumnar: pgcolumnar.vacuum's stripe_count is refused, not silently ignored (#560).
#
# docs/sql-reference.md documented `stripe_count` as bounding how many row groups
# are combined in one call. The C function reads PG_GETARG_OID(0) and nothing
# else, and passes a literal 0 to pgcolumnar_compact_relation, so the argument
# has never done anything.
#
# HONOURING IT IS NOT AN OPTION, and that is the whole reason this fix refuses
# rather than implements. pgcolumnar_compact_relation's second parameter is
# nsortkeys, not a bound, and its body dereferences sortAtts[i]. More seriously,
# it swaps the whole relation to a NEW RELFILENODE, so a partial rewrite of some
# row groups would leave the rest behind and destroy them. A bounded compaction
# is a different algorithm, not a parameter.
#
# pgcolumnar.compact_rewrite(rel, min_deleted_fraction, max_groups) already does
# bounded work: its maxGroups reaches pgcolumnar_rewrite_partial_groups. So the
# refusal can name a function that actually works, which is what makes an ERROR
# defensible rather than merely honest.
#
# vacuum_full carries the same argument and PASSES IT THROUGH to
# pgcolumnar.vacuum, so it inherits the refusal without its own change. That
# propagation is asserted here, because it is the only thing making the second
# instance safe.
#
# Usage:  test/vacuum_stripe_count.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# NGROUPS, not GROUPS. GROUPS is a bash BUILT-IN array holding the invoking
# user's group ids, so the assignment is silently discarded and the name expands
# to the first gid (0 for root in the test container). That made `seq 1` empty,
# so the fixture built NOTHING, and every premise below compared 0 with 0 and
# PASSED. A suite that measured an empty table and reported success.
#
# stripe_row_limit also has a floor of 1000: set_options raises below it, on
# stderr, which a suite reading only stdout never sees.
NGROUPS=6
ROWS_PER=1000
STRIPE_LIMIT=100000

build_fixture() {
	psql_run "DROP TABLE IF EXISTS vsc;"
	psql_run "CREATE TABLE vsc (id int, v text) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('vsc', stripe_row_limit => $STRIPE_LIMIT, chunk_group_row_limit => $ROWS_PER);"
	for _i in $(seq 1 $NGROUPS); do
		psql_run "INSERT INTO vsc SELECT g, 'v'||g FROM generate_series(1,$ROWS_PER) g;"
	done
}
groups_now() { q "SELECT count(*) FROM pgcolumnar.stats('vsc');"; }
rows_now()   { q "SELECT count(*) FROM vsc;"; }

err_of() {  # err_of <sql> -> the ERROR line, connfail, or the literal noerror
	#
	# -U postgres is not optional and its absence is why this helper had to be
	# rewritten. Without it psql connects as the OS user, which is not a role
	# here, so every call failed at connect. psql reports that as a LOWERCASE
	# "psql: error:", which the ERROR: case below does not match, so the helper
	# returned "noerror" and a call that never reached the server read as a
	# successful one. Every arm using it was green for the wrong reason.
	#
	# So connect the way lib.sh does, and give a connection failure its OWN token
	# rather than letting it fall through to the success branch. An arm can then
	# never pass because the server was unreachable.
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "$1" 2>&1)"
	case "$out" in
		*"psql: error"*|*"could not connect"*|*"FATAL:"*) echo connfail ;;
		*ERROR:*) printf '%s\n' "$out" | grep -m1 'ERROR:' ;;
		*)        echo noerror ;;
	esac
}

# refused_by_us <sql> -> "refused" only for pgcolumnar's own stripe_count refusal,
# "accepted" when the call succeeds, or the other error. A refusal read as "anything
# but noerror" passed on any error: aimed at a missing table, the call failed with
# `relation "vsc_missing" does not exist` and read as the refusal.
refused_by_us() {
	local e
	e="$(err_of "$1")"
	case "$e" in
		*"does not support a stripe_count bound"*) echo refused ;;
		noerror) echo accepted ;;
		*) echo "other error: $e" ;;
	esac
}

# The helper is itself a claim, so prove it can say all three things before any
# arm depends on it. Without this, the rewrite above could have been wrong in a
# new way and every arm would still have looked fine.
check "premise: err_of reports a clean call"      "$(err_of 'SELECT 1;')" "noerror"
check "premise: err_of reports a real error"      "$(err_of 'SELECT 1/0;' | grep -c 'ERROR:')" "1"
check "premise: err_of cannot mistake an unreachable server for success" \
	"$(PGC_PORT=1 err_of 'SELECT 1;')" "connfail"

# Guard against the failure that produced the comment above: if the fixture
# constants are zero, every arm compares 0 with 0 and passes on an empty table.
check "premise: the fixture constants are non-zero, so the arms can fail" \
	"$([ "${NGROUPS:-0}" -gt 1 ] && [ "${ROWS_PER:-0}" -ge 1000 ] && echo sane || echo "NGROUPS=$NGROUPS ROWS_PER=$ROWS_PER")" \
	"sane"
# And that the groups are COMBINABLE. Six groups of exactly stripe_row_limit rows
# are already full, so a correct vacuum leaves all six and an arm expecting one
# fails against working code. The fixture must leave headroom, or it measures the
# limit rather than the compaction.
check "premise: the whole fixture fits in one stripe, so combining is possible" \
	"$([ "$(( NGROUPS * ROWS_PER ))" -le "$STRIPE_LIMIT" ] && echo combinable || echo "capped at $STRIPE_LIMIT")" \
	"combinable"

build_fixture

# ---- premises ---------------------------------------------------------------
#
# The fixture must be able to DISTINGUISH bounded from unbounded work, or every
# arm below is satisfied by a table that was never going to be rewritten anyway.
check_num "premise: the fixture really has several row groups to combine" \
	"$(groups_now)" "$NGROUPS"
check_num "premise: the fixture has the rows it should" \
	"$(rows_now)" "$(( NGROUPS * ROWS_PER ))"


# ---- the default form must keep working -------------------------------------
#
# Without these the fix could pass by refusing everything, which is not a fix.
check "the one-argument form still vacuums" "$(err_of "SELECT pgcolumnar.vacuum('vsc');")" "noerror"
check_num "and it combined the groups, so it did the work" "$(groups_now)" "1"
check_num "and it preserved every row" "$(rows_now)" "$(( NGROUPS * ROWS_PER ))"

build_fixture
check "the explicit default 0 is still accepted" \
	"$(err_of "SELECT pgcolumnar.vacuum('vsc', 0);")" "noerror"
check_num "and 0 also did the work" "$(groups_now)" "1"

# ---- the refusal ------------------------------------------------------------
#
# A non-default stripe_count must be refused rather than accepted and ignored.
# On unfixed code these calls SUCCEED, which is the defect.
build_fixture
check "a non-default stripe_count is refused rather than ignored" \
	"$(refused_by_us "SELECT pgcolumnar.vacuum('vsc', 4);")" "refused"
# The hint is a separate line of psql output, so it is read from the FULL error
# block rather than from err_of, which deliberately returns only the ERROR line.
# A refusal that does not name a working alternative is a dead end for the user.
full_err="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "SELECT pgcolumnar.vacuum('vsc', 4);" 2>&1)"
check "and the refusal names the function that does bound the work" \
	"$(printf '%s' "$full_err" | grep -c 'compact_rewrite')" "1"
check "and it is offered as a HINT, where psql shows it to the user" \
	"$(printf '%s' "$full_err" | grep -c '^HINT:')" "1"

# The refusal must not have done the work anyway. A message is not a behaviour:
# if the rewrite ran before the argument was inspected, the table would already
# be compacted and only the error would differ.
# NGROUPS, not a count read before the call: an empty rebuild read 0 both times
check_num "the refused call did no work: the groups are untouched" "$(groups_now)" "$NGROUPS"
check_num "and no rows were lost to it" "$(rows_now)" "$(( NGROUPS * ROWS_PER ))"

# ---- the second instance, inherited rather than fixed separately ------------
#
# vacuum_full(schema, sleep_time, stripe_count) passes stripe_count straight to
# pgcolumnar.vacuum, so it must inherit the refusal. If that propagation ever
# stops, this arm is the only thing that would notice.
check "vacuum_full inherits the refusal for a non-default stripe_count" \
	"$(refused_by_us "SELECT pgcolumnar.vacuum_full('public', 0.0, 4);")" "refused"
check_num "the refused vacuum_full did no work either" "$(groups_now)" "$NGROUPS"
check "vacuum_full's own default form still works" \
	"$(err_of "SELECT pgcolumnar.vacuum_full('public', 0.0);")" "noerror"
check_num "and it combined the groups" "$(groups_now)" "1"

# ---- the documented alternative must actually bound work --------------------
#
# The errhint points callers at compact_rewrite. If that did not bound anything
# either, the refusal would be trading one false promise for another.
build_fixture
psql_run "DELETE FROM vsc WHERE id % 2 = 0;"
# THE WORK, NOT THE GROUP COUNT. Each rewritten group is replaced by one new group, so
# the table holds six groups whether compact_rewrite rewrote two, all six or none, and
# the old "left more than one group" check passed all three (measured: with the bound
# ignored, and with the rewrite made a no-op). Every group holds deleted rows going in,
# so the groups that still hold some are the ones it did not rewrite.
cr_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "SELECT pgcolumnar.compact_rewrite('vsc', 0.1, 2);" 2>&1)"
check "premise: compact_rewrite's bounded form is accepted" \
	"$(case "$cr_out" in *ERROR:*) printf '%s\n' "$cr_out" | grep -m1 'ERROR:' ;; *) echo noerror ;; esac)" "noerror"
check "the named alternative rewrote exactly its bound and left the other groups alone" \
	"rewrote ${cr_out:-nothing} of $NGROUPS groups, $(q "SELECT count(*) FROM pgcolumnar.stats('vsc') WHERE deletedrows > 0;") still hold deleted rows" \
	"rewrote 2 of $NGROUPS groups, $(( NGROUPS - 2 )) still hold deleted rows"

pgc_summary

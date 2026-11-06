#!/usr/bin/env bash
#
# The planner's chosen plan must not be catastrophically worse than one it
# declined (issues #433, #434).
#
# Every other suite asks whether a plan is CORRECT. None asks whether it is the
# one a reasonable cost model would pick. That gap is how #434 survived: the
# planner prices a columnar index scan at 31,502 and the custom scan at 69,204,
# then chooses the index scan, which is 12.4x slower. Both plans return the right
# answer, so every existing check passes.
#
# WHAT THIS ASSERTS
#
# For each query: run the planner's own choice, then force each alternative, and
# fail when the choice is more than PLAN_BOUND times slower than the best
# alternative. It is a ratio between two plans in the same run on the same box,
# so it does not depend on how fast the machine is.
#
# WHY THE BOUND IS LOOSE
#
# 3x. The point is not to police the cost model, which is allowed to be wrong.
# It is to catch the case where it is wrong by orders of magnitude, which is what
# #434 is. A tight bound here would flake on a shared runner and teach people to
# ignore it.
#
# WHY TIMING RATHER THAN BUFFERS
#
# Buffers would be exact, and they are the right tool when the question is "did
# this read less". Here the question is "is the chosen plan much slower", and
# slower is what the user experiences. The bound is loose enough to survive
# timing noise.
#
# PGC_SKIP_TIMING
#
# The ratio goes through check_ratio_needs_quiet_machine, which lives in lib.sh and owns the
# decision. This file does NOT branch on the flag to decide whether to assert.
#
# Two earlier versions got this wrong in the same way. The first only SAID the
# flag was wired and used check_ratio throughout, so it did nothing. The second
# branched on the flag here and called check_timing with two empty strings, which
# is the "" vs "" compare lib.sh forbids, and which would have PASSED the suite's
# central assertion had the two copies of the condition ever disagreed.
#
# Whether to MEASURE is still this file's business, because that is a cost
# decision rather than an assertion: PGC_MEASURING below gates both the fixture
# size and the execution of the timed queries.
#
# Usage:  test/planner_choice_quality.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

PLAN_BOUND=${PGC_PLAN_BOUND:-3}
# The only read of PGC_SKIP_TIMING in this file, and it governs COST alone: how
# big a fixture to build, and whether to execute the timed queries. Whether to
# assert a ratio is check_ratio_needs_quiet_machine's decision, in lib.sh.
PGC_MEASURING=1
[ "${PGC_SKIP_TIMING:-0}" = 1 ] && PGC_MEASURING=0
ROWS=${PGC_PLAN_ROWS:-200000}
# 200,000 wide rows is 12.8M md5() calls and ~205 MB, and none of it is needed to
# read an EXPLAIN. When only the plan shapes are asserted, build a fixture sized
# for that. Shortening the statement timeout did not address this cost; the
# review asked about the suite's cost per major and this is where it lives.
[ "$PGC_MEASURING" = 1 ] || ROWS=${PGC_PLAN_ROWS:-20000}
# The finding is "23x slower", not "slower than two minutes". A long timeout only
# buys a longer wait before the same verdict, on every major, for as long as the
# bug exists.
PLAN_TIMEOUT=${PGC_PLAN_TIMEOUT:-20}


# The shape #433 and #434 are about: wide incompressible rows, an index on a
# correlated key. The payload must not compress, or the row group stays under the
# fetch cache cap and the effect disappears. Correlating on both g and the inner
# series is what makes every row and every block differ.
psql_run "CREATE TABLE pq (k bigint, tag text, payload bytea) USING pgcolumnar;
          INSERT INTO pq
          SELECT g, 'tag' || (g % 5),
                 decode((SELECT string_agg(md5(g::text || s::text), '')
                           FROM generate_series(1,64) s), 'hex')
          FROM generate_series(1,$ROWS) g;
          CREATE INDEX pq_k ON pq (k);
          ANALYZE pq;" >/dev/null

check "fixture rows" "$(q 'SELECT count(*) FROM pq')" "$ROWS"
SZ=$(q "SELECT pg_total_relation_size('pq')")
check "premise: the payload did not compress, so the fetch path is exercised" \
	"$([ "$SZ" -gt $(( ROWS * 700 )) ] && echo yes || echo "no ($(( SZ / ROWS )) bytes per row)")" "yes"
check "premise: the index key is correlated, which is the case that misprices" \
	"$(q "SELECT CASE WHEN correlation > 0.9 THEN 'yes' ELSE 'no (' || correlation || ')' END
	        FROM pg_stats WHERE tablename='pq' AND attname='k'")" "yes"

# Time a query under a given setting, and report which scan node ran.
#
# A FAILED RUN IS NOT A FAST ONE. psql prints "Time:" after a statement that
# errored as well as after one that succeeded, so this used to read a plan that
# raised an error in 2 ms as a plan that answered in 2 ms, and the ratio below
# passed with the chosen plan unable to run at all (measured: a custom scan that
# raises at execution left the suite PASSED). psql's exit status under
# ON_ERROR_STOP is the signal now, and the statement-timeout cancel is told
# apart by its own message rather than by any line containing "timeout".
run_plan() {  # run_plan <settings> <sql> -> "<node>\t<ms | TIMEOUT | ERROR (msg) | empty>"
	local node ms out rc
	node=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atq -c "$1" -c "EXPLAIN (COSTS OFF) $2" 2>&1 |
		grep -oE 'Index Only Scan|Index Scan|Bitmap Heap Scan|Custom Scan \([A-Za-z]+\)|Seq Scan' | head -1)
	# No execution under PGC_SKIP_TIMING. The plan name above comes from EXPLAIN,
	# so every premise still holds; only the wall clock is unavailable.
	if [ "$PGC_MEASURING" = 0 ]; then
		printf '%s\t\n' "${node:-unknown}"
		return
	fi
	# Warm first, then measure. The three plans are timed in a fixed order, so
	# without this the chosen plan pays a cold cache and both alternatives run
	# warm against a 3x bound -- the ordering alone could push a healthy ratio
	# over it. The discarded run also populates shared buffers for the timed one.
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atq -c "SET statement_timeout='${PLAN_TIMEOUT}s';" -c "$1" -c "$2" >/dev/null 2>&1
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atq -c "SET statement_timeout='${PLAN_TIMEOUT}s';" -c "$1" -c '\timing on' -c "$2" 2>&1)
	rc=$?
	if [ "$rc" != 0 ]; then
		if grep -q 'canceling statement due to statement timeout' <<<"$out"; then
			printf '%s\tTIMEOUT\n' "${node:-unknown}"
		else
			out=$(grep -m1 -E 'ERROR|FATAL|error' <<<"$out" | tr '\t' ' ')
			printf '%s\tERROR (%s)\n' "${node:-unknown}" "${out:-psql exit $rc}"
		fi
		return
	fi
	ms=$(grep -oE 'Time: [0-9.]+ ms' <<<"$out" | tail -1 | grep -oE '[0-9.]+')
	# Tab-delimited, because a node name contains spaces: "Custom Scan
	# (PgColumnarScan)" read back through a space-split gives node="Custom" and
	# ms="Scan (PgColumnarScan) 23.380". The first version of this file did that
	# and compared two garbage strings while reporting a pass.
	printf '%s\t%s\n' "${node:-unknown}" "${ms:-}"
}

# The planner's choice against the best alternative it declined.
#
# THE CHECK NAMES CARRY THE LABEL AND NOTHING ELSE. They used to carry the bound,
# the timeout and the node that won the comparison, so the same check had one
# name when it passed ("... alternative (Index Scan)") and another when it failed
# ("... alternative (Custom Scan (PgColumnarScan))") -- a red that no green run
# had ever named. Those values are in the display line now, not the name.
choice_vs_best() {  # choice_vs_best <label> <sql>
	local label="$1" sql="$2"
	local cnode cms best bnode n1 m1 n2 m2 i cand_n cand_m slowalt failalt

	IFS=$'\t' read -r cnode cms <<<"$(run_plan "" "$sql")"
	IFS=$'\t' read -r n1 m1 <<<"$(run_plan "SET enable_indexscan=off; SET enable_bitmapscan=off;" "$sql")"
	IFS=$'\t' read -r n2 m2 <<<"$(run_plan "SET pgcolumnar.enable_custom_scan=off; SET enable_seqscan=off;" "$sql")"

	echo "      $label: chose $cnode=${cms:-?}ms; alternatives $n1=${m1:-?}ms $n2=${m2:-?}ms"

	# A plan we could not name is a plan we did not measure. The first version of
	# this file reported "unknown" for an Index Only Scan, because "Index Scan"
	# does not match it, and then compared its time to another unknown.
	check "premise: [$label] the chosen plan was identified" \
		"$([ "$cnode" != unknown ] && echo yes || echo "no (grep matched no scan node)")" "yes"

	# The alternatives must really be different plans. If forcing changed
	# nothing, there is no choice to judge and the check below would compare a
	# plan with itself. An arm whose EXPLAIN named no scan node is not a
	# different plan; it is an unidentified one.
	#
	# Not a premise (#1339): it is a product check. With pgcolumnar.enable_custom_scan
	# ignored by the planner, every arm runs the custom scan and this is the one
	# check that goes red -- 2026-10-22, PG17, both twins.
	check "[$label] forcing produced a different plan" \
		"$( { [ "$n1" != "$cnode" ] && [ "$n1" != unknown ]; } ||
		    { [ "$n2" != "$cnode" ] && [ "$n2" != unknown ]; } &&
		    echo yes || echo "no (chosen $cnode, forced $n1 and $n2)")" "yes"

	# Everything below this line has a wall clock in it. An absent timing is the
	# signal, not a second reading of the flag: when nothing was executed there is
	# no measurement to judge, and check_timing / check_ratio_needs_quiet_machine
	# decide whether that is a skip or a failure. If the flag is set they announce
	# a skip; if it is not, the empty reading fails loudly.
	#
	# The chosen plan must have RUN before it can be compared. A timeout, an error
	# and a missing timing line are each a failure here, by name, and not a number.
	check_timing "[$label] the chosen plan ran to completion" \
		"$(pgc_is_number "$cms" && echo yes || echo "no (${cms:-no timing reported})")" "yes"
	if ! pgc_is_number "$cms"; then
		check_ratio_needs_quiet_machine "[$label] the chosen plan is within the bound of the best alternative" \
			"$cms" "" "$PLAN_BOUND"
		return
	fi

	best=""; bnode=""; slowalt=""; failalt=""
	# Node and time kept in separate variables rather than re-split from a string.
	for i in 1 2; do
		eval "cand_n=\$n$i; cand_m=\$m$i"
		# An arm whose forcing did not change the plan is the CHOSEN plan run
		# again, not an alternative. The premise above only requires ONE arm to
		# differ, so without this the minimum could be the chosen plan's own warm
		# re-run and the ratio would measure cache warmth.
		[ "$cand_n" = "$cnode" ] && continue
		# An alternative that timed out is not a missing alternative. Remember it:
		# the two look identical to the ratio below (neither yields a number) but
		# they are opposite results, and the report has to say which happened.
		case "${cand_m:-}" in
			TIMEOUT) slowalt="$cand_n"; continue ;;
			# An alternative that FAILED is neither fast nor absent. Read as a
			# time it made the chosen plan look slow; read as absent it made
			# "nothing could be forced" pass. It is its own failure.
			ERROR*) failalt="$cand_n ($cand_m)"; continue ;;
		esac
		pgc_is_number "${cand_m:-}" || continue
		if [ -z "$best" ] || [ "$(awk -v a="$cand_m" -v b="$best" 'BEGIN{print (a<b)?1:0}')" = 1 ]; then
			best="$cand_m"; bnode="$cand_n"
		fi
	done
	if [ -n "$failalt" ]; then
		check "[$label] the chosen plan is within the bound of the best alternative" \
			"no (the forced alternative $failalt failed, so there is nothing to compare)" "yes"
		return
	fi
	# No alternative could be forced.
	#
	# This was a failing premise, and that was wrong in the direction that matters.
	# Once #434 is fixed the custom scan is priced correctly, wins outright, and
	# forcing yields nothing to compare against -- so the EXPECTED state after the
	# fix made the suite red. Reported by the reviewer, who ran it against the fix
	# rather than against the bug.
	#
	# Treating it as a plain pass is the other wrong answer: after the fix that is
	# the normal case for these shapes, so the suite would fall silent on exactly
	# what it exists to watch. There is no ratio to assert, so assert the outcome
	# instead. If nothing can be forced away from the chosen plan, the plan that
	# won had better be ours.
	if [ -z "$best" ]; then
		# Two ways to arrive here, and they are not the same result.
		#
		# An alternative was forced and TIMED OUT. That is the strongest outcome
		# the suite can produce -- the declined plan is at least PLAN_TIMEOUT
		# against the chosen plan's milliseconds -- but it yields no number, so
		# the ratio above cannot express it. Reporting it as "nothing could be
		# forced" reads as "there was nothing to compare", which is the opposite
		# of what happened, and hides that the ratio bound went untested.
		if [ -n "$slowalt" ]; then
			echo "      [$label] the forced alternative ($slowalt) did not finish in ${PLAN_TIMEOUT}s"
			check "[$label] a forced alternative did not finish, so the chosen plan must be the custom scan" \
				"$(case "$cnode" in 'Custom Scan'*) echo yes ;; *) echo "no ($cnode)" ;; esac)" "yes"
			return
		fi
		# Genuinely nothing different could be forced. There is no ratio to
		# assert, so assert the outcome: the plan that won had better be ours.
		check "[$label] nothing could be forced, so the chosen plan must be the custom scan" \
			"$(case "$cnode" in 'Custom Scan'*) echo yes ;; *) echo "no ($cnode)" ;; esac)" "yes"
		return
	fi

	echo "      [$label] best alternative $bnode, bound ${PLAN_BOUND}x"
	check_ratio_needs_quiet_machine "[$label] the chosen plan is within the bound of the best alternative" \
		"$cms" "$best" "$PLAN_BOUND"
}

echo "   planner choice against the best declined alternative, bound ${PLAN_BOUND}x"
choice_vs_best "range scan with aggregate" \
	"SELECT tag, count(*) FROM pq WHERE k > $(( ROWS * 4 / 5 )) GROUP BY tag"
choice_vs_best "wide range, narrow projection" \
	"SELECT count(*) FROM pq WHERE k > $(( ROWS / 2 ))"

pgc_summary

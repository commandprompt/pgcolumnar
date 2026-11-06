#!/usr/bin/env bash
#
# pgColumnar #403 item 1: preimage rewriting for monotonic functions.
#
# A zone map holds `ts`. A predicate on `date_trunc('day', ts)` is about a
# FUNCTION of ts, so nothing can exclude a chunk group from it and the scan
# reads everything. Rewritten to a range on ts it prunes with the machinery that
# already exists.
#
# Measured on this fixture before the rewrite: the explicit range reads 2 of 50
# chunk groups, the date_trunc form reads all 50, and both return the same
# 10,000 rows. That 25x is the whole prize, and it is what the work arm below
# measures.
#
# THE ANSWERS CANNOT SEE THIS CHANGE. A predicate rewrite that prunes correctly
# and one that does not prune at all return identical rows, so every oracle arm
# here is there to catch a rewrite that prunes WRONGLY, and the work arm is the
# only thing that goes red when the rewrite stops happening.
#
# The three shapes that must DECLINE are as important as the one that must work,
# and each has its own arm:
#
#   * a NON-TRUNCATED constant. `date_trunc('day', ts) = '2024-02-01 12:00'`
#     matches nothing, because date_trunc never returns 12:00. The code declines
#     it, and that decline is DEFENCE IN DEPTH rather than a correctness
#     requirement -- a distinction established by removing the guard and
#     watching this suite stay green, including the arm below. Pruning to a
#     wrong range cannot return wrong rows while the keys are conservative and
#     the executor re-applies the clause, and the derived range is never
#     NARROWER than the true matching set, which here is empty and so contained
#     in every range. It becomes load-bearing the moment these keys are marked
#     exact, because the batch fold then uses them as its only row filter
#     (#715) with no recheck behind it.
#   * a timestamptz column. date_trunc(text, timestamptz) truncates in the
#     session TimeZone, so a key frozen at plan time is wrong if TimeZone
#     changes. Excluded deliberately; the arm pins that it stays excluded.
#   * a non-constant unit, which cannot be inverted at plan time at all.
#
# Usage:  test/preimage_rewrite.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1
}
q1() { q "$1" | tail -1; }

# Backends killed by a signal, from the server log: a number, or nothing when the log
# cannot be read. A fresh connection answers again after crash recovery, so "SELECT 1"
# alone passed with a backend SIGKILLed just before it (#1341, as avro_manifest found
# in #1576). The survival check also requires that this count has not moved.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q1 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}
CRASHES_BEFORE="$(crashes)"

# One day per ~10000 rows and a 10000-row group limit, so a day is about one
# chunk group and pruning has something to remove. NULLs and the extremes are in
# the data because they are where a rewrite most easily admits a row it should
# not.
psql_run "CREATE TABLE pre_c(ts timestamp, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('pre_c', stripe_row_limit => 10000);"
psql_run "INSERT INTO pre_c SELECT timestamp '2024-01-01' + (g / 10000) * interval '1 day'
                                   + (g % 10000) * interval '1 second', g
          FROM generate_series(1,500000) g;"
psql_run "INSERT INTO pre_c VALUES (NULL, -1), ('infinity', -2), ('-infinity', -3);"
# THE MIRRORS ARE BUILT FROM THE GENERATORS, never copied from the columnar table:
# a copy agrees with whatever the columnar reader returned, so with a slice of
# pre_c corrupted before the copy every heap comparison here passed, 86 of 86
# (#1341). The same holds for pre_edgeh and pre_tzh below.
psql_run "CREATE TABLE pre_h(ts timestamp, v int);"
psql_run "INSERT INTO pre_h SELECT timestamp '2024-01-01' + (g / 10000) * interval '1 day'
                                   + (g % 10000) * interval '1 second', g
          FROM generate_series(1,500000) g;"
psql_run "INSERT INTO pre_h VALUES (NULL, -1), ('infinity', -2), ('-infinity', -3);"
psql_run "ANALYZE pre_c; ANALYZE pre_h;"

check_num "premise: the fixture spans many chunk groups" \
	"$(q1 "SELECT (count(*) > 10)::int FROM pgcolumnar.row_group
	       WHERE storage_id = pgcolumnar.get_storage_id('pre_c'::regclass);")" 1

removed() {	# removed QUERY -> chunk groups removed by filter, or empty
	q "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF) $1" \
		| grep 'Columnar Chunk Groups Removed by Filter' | grep -oE '[0-9]+' | head -1
}

DAY="timestamp '2024-02-01'"
RANGE="SELECT count(*) FROM pre_c WHERE ts >= $DAY AND ts < $DAY + interval '1 day'"
TRUNC="SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = $DAY"

# The reference: the explicit range already prunes. Without this the work arm
# below cannot tell "the rewrite did not fire" from "there was nothing to prune".
ref="$(removed "$RANGE")"
check_num "the explicit range prunes, so there IS something to remove" \
	"$([ -n "$ref" ] && [ "$ref" -gt 0 ] && echo 1 || echo 0)" 1
echo "      explicit range removed $ref chunk groups"

# ---- the work arm: the whole point of the change ---------------------------
got="$(removed "$TRUNC")"
echo "      date_trunc form removed ${got:-unset} chunk groups"
check_num "date_trunc('day', ts) = const prunes chunk groups (#403)" \
	"$([ -n "$got" ] && [ "$got" -gt 0 ] && echo 1 || echo 0)" 1
# and it must prune as WELL as the explicit range, not merely more than zero
check_num "...and prunes as well as the equivalent explicit range" \
	"$got" "$ref"

# ---- the answers, which cannot see the change and are here for wrongness ----
check_num "the rewritten predicate returns the range's answer" \
	"$(q1 "$TRUNC;")" "$(q1 "$RANGE;")"
check_num "and the heap mirror's answer" \
	"$(q1 "$TRUNC;")" \
	"$(q1 "SELECT count(*) FROM pre_h WHERE date_trunc('day', ts) = $DAY;")"
check_text "the actual ROWS match the heap, not just the count" \
	"$(q "SELECT v FROM pre_c WHERE date_trunc('day', ts) = $DAY ORDER BY v" | md5sum)" \
	"$(q "SELECT v FROM pre_h WHERE date_trunc('day', ts) = $DAY ORDER BY v" | md5sum)"

# ---- the ordered operators (#403 item 1 residue) ---------------------------
#
# #739 inverted equality only. Monotonicity is what makes the ORDERED operators
# invertible too, and they are the shape a time-series filter actually takes:
# `date_trunc('day', ts) >= X` reads every chunk group today while the range it
# is equivalent to reads two.
#
# Each arm names the explicit range it is equivalent to and asserts against THAT
# and against the heap. Deriving the expected answer here rather than hardcoding
# it is deliberate: the fixture carries NULL, 'infinity' and '-infinity' rows, and
# the heap mirror is the only oracle that gets those right without me restating
# date_trunc's semantics in the test.
ord_arm() {	# ord_arm <label> <trunc-predicate> <equivalent-range-predicate>
	local label="$1" tp="$2" rp="$3"
	local r g
	r="$(removed "SELECT count(*) FROM pre_c WHERE $rp")"
	g="$(removed "SELECT count(*) FROM pre_c WHERE $tp")"
	check_num "the range for [$label] prunes, so there is something to remove" \
		"$([ -n "$r" ] && [ "$r" -gt 0 ] && echo 1 || echo 0)" 1
	check_num "[$label] prunes chunk groups" \
		"$([ -n "$g" ] && [ "$g" -gt 0 ] && echo 1 || echo 0)" 1
	check_num "[$label] prunes as well as the equivalent explicit range" "$g" "$r"
	check_num "[$label] returns the range's answer" \
		"$(q1 "SELECT count(*) FROM pre_c WHERE $tp;")" \
		"$(q1 "SELECT count(*) FROM pre_c WHERE $rp;")"
	check_num "[$label] returns the heap's answer" \
		"$(q1 "SELECT count(*) FROM pre_c WHERE $tp;")" \
		"$(q1 "SELECT count(*) FROM pre_h WHERE $tp;")"
	check_text "[$label] returns the heap's ROWS, not just its count" \
		"$(q "SELECT v FROM pre_c WHERE $tp ORDER BY v" | md5sum)" \
		"$(q "SELECT v FROM pre_h WHERE $tp ORDER BY v" | md5sum)"
}

NEXT="timestamp '2024-02-02'"
ord_arm "ts_trunc >= c"  "date_trunc('day', ts) >= $DAY"  "ts >= $DAY"
ord_arm "ts_trunc > c"   "date_trunc('day', ts) > $DAY"   "ts >= $NEXT"
ord_arm "ts_trunc < c"   "date_trunc('day', ts) < $DAY"   "ts < $DAY"
ord_arm "ts_trunc <= c"  "date_trunc('day', ts) <= $DAY"  "ts < $NEXT"

# The operand order is swapped. For equality that is a no-op; for these it
# COMMUTES the strategy, and reusing the unswapped one inverts the bound and
# drops rows. `c < f(ts)` is `f(ts) > c`, not `f(ts) < c`.
ord_arm "c < ts_trunc (reversed)"  "$DAY < date_trunc('day', ts)"  "ts >= $NEXT"
ord_arm "c >= ts_trunc (reversed)" "$DAY >= date_trunc('day', ts)" "ts < $NEXT"

# A constant that is not itself truncated. Under equality this is unsatisfiable
# and #739 declines it. Under the ordered operators it is SATISFIABLE, and the
# bound moves to the next boundary because date_trunc only ever returns truncated
# values. Treating it the way equality treats it would lose every row in the
# partial bucket.
HALF="timestamp '2024-02-01 12:00'"
ord_arm "ts_trunc >= c, c not truncated" "date_trunc('day', ts) >= $HALF" "ts >= $NEXT"
ord_arm "ts_trunc < c, c not truncated"  "date_trunc('day', ts) < $HALF"  "ts < $NEXT"

# ---- near the end of the timestamp range, the rewrite must DECLINE ---------
#
# hi = lo + step raises `timestamp out of range` within one step of the maximum,
# and an ereport in the planner fails the whole query instead of declining to
# prune. This was reachable on main before the ordered operators existed:
# `date_trunc('year', ts) = timestamp '294276-01-01'` answered ERROR on a
# columnar table while the heap returned the row. Inverting the ordered
# operators reaches the same arithmetic from four more predicates.
#
# A separate fixture, because putting a near-maximum row in pre_c would move
# every count and premise above it.
psql_run "CREATE TABLE pre_edge(ts timestamp, v int) USING pgcolumnar;"
psql_run "INSERT INTO pre_edge SELECT timestamp '2024-01-01' + g * interval '1 day', g
          FROM generate_series(1,1000) g;"
psql_run "INSERT INTO pre_edge VALUES ('294276-12-31 23:00', -1), ('infinity', -2),
          ('-infinity', -3);"
psql_run "CREATE TABLE pre_edgeh(ts timestamp, v int);"
psql_run "INSERT INTO pre_edgeh SELECT timestamp '2024-01-01' + g * interval '1 day', g
          FROM generate_series(1,1000) g;"
psql_run "INSERT INTO pre_edgeh VALUES ('294276-12-31 23:00', -1), ('infinity', -2),
          ('-infinity', -3);"
MAXY="timestamp '294276-01-01'"
edge_arm() {	# edge_arm <label> <predicate>
	check_num "[$1] answers instead of raising, and agrees with the heap" \
		"$(q1 "SELECT count(*) FROM pre_edge WHERE $2;")" \
		"$(q1 "SELECT count(*) FROM pre_edgeh WHERE $2;")"
}
# 2, not 1: the near-maximum row AND 'infinity' both satisfy this.
check_num "premise: the near-maximum row is really there" \
	"$(q1 "SELECT count(*) FROM pre_edgeh WHERE ts > timestamp '294276-01-01';")" 2
edge_arm "year = max"  "date_trunc('year', ts) = $MAXY"
edge_arm "year >= max" "date_trunc('year', ts) >= $MAXY"
edge_arm "year > max"  "date_trunc('year', ts) > $MAXY"
edge_arm "year <= max" "date_trunc('year', ts) <= $MAXY"
edge_arm "year < max"  "date_trunc('year', ts) < $MAXY"
# Infinity is exempt from the near-maximum decline: interval arithmetic saturates
# there rather than overflowing, so those predicates still rewrite and still
# prune.
edge_arm "day >= infinity" "date_trunc('day', ts) >= timestamp 'infinity'"
edge_arm "day < infinity"  "date_trunc('day', ts) < timestamp 'infinity'"
# EQUALITY on an infinity is the case that exemption did not cover. The ordered
# operators emit ONE key, so lo == hi is harmless to them and the two arms above
# stayed green. Equality emits the bounded interval `k >= lo AND k < hi`, and
# saturation makes hi equal lo, so the interval is empty and excludes the very
# row that matches. The suite carried infinity arms, and carried equality arms,
# and never their intersection.
edge_arm "day = infinity"  "date_trunc('day', ts) = timestamp 'infinity'"
edge_arm "day = -infinity" "date_trunc('day', ts) = timestamp '-infinity'"
edge_arm "hour = infinity" "date_trunc('hour', ts) = timestamp 'infinity'"
edge_arm "year = -infinity" "date_trunc('year', ts) = timestamp '-infinity'"
# The premise those four rest on: both infinities are in the fixture, so a
# columnar count of 0 is a lost row rather than an empty fixture.
check_num "premise: the fixture holds one +infinity row" \
	"$(q1 "SELECT count(*) FROM pre_edgeh WHERE ts = timestamp 'infinity';")" 1
check_num "premise: the fixture holds one -infinity row" \
	"$(q1 "SELECT count(*) FROM pre_edgeh WHERE ts = timestamp '-infinity';")" 1

# ---- the shapes that must DECLINE ------------------------------------------
# A non-truncated constant is unsatisfiable, and these arms assert the ANSWER,
# which is all they can assert: with conservative keys and an executor recheck,
# a rewrite that turned this into a range would still return zero rows. They do
# not, and cannot, show that the decline is load-bearing -- removing the guard
# leaves them green. They are the arms that would catch it if the keys were ever
# marked exact, which is when the decline starts protecting an answer.
NOTRUNC="SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = timestamp '2024-02-01 12:00'"
check_num "a non-truncated constant returns no rows" "$(q1 "$NOTRUNC;")" 0
check_num "...and the heap agrees it is unsatisfiable" \
	"$(q1 "SELECT count(*) FROM pre_h WHERE date_trunc('day', ts) = timestamp '2024-02-01 12:00';")" 0

psql_run "CREATE TABLE pre_tz(ts timestamptz, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('pre_tz', stripe_row_limit => 10000);"
psql_run "INSERT INTO pre_tz SELECT timestamptz '2024-01-01+00' + (g / 10000) * interval '1 day'
                                    + (g % 10000) * interval '1 second', g
          FROM generate_series(1,200000) g;"
psql_run "CREATE TABLE pre_tzh(ts timestamptz, v int);"
psql_run "INSERT INTO pre_tzh SELECT timestamptz '2024-01-01+00' + (g / 10000) * interval '1 day'
                                    + (g % 10000) * interval '1 second', g
          FROM generate_series(1,200000) g;"
# timestamptz is excluded because the truncation depends on the session
# TimeZone. Asserted as an ANSWER under two different zones rather than as an
# absence: what matters is that the result follows the zone, which a frozen key
# could not do.
check_num "timestamptz: correct under UTC" \
	"$(q1 "SET TimeZone='UTC'; SELECT count(*) FROM pre_tz WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+00';")" \
	"$(q1 "SET TimeZone='UTC'; SELECT count(*) FROM pre_tzh WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+00';")"
check_num "timestamptz: and still correct under a different zone" \
	"$(q1 "SET TimeZone='Asia/Tokyo'; SELECT count(*) FROM pre_tz WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+09';")" \
	"$(q1 "SET TimeZone='Asia/Tokyo'; SELECT count(*) FROM pre_tzh WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+09';")"

# A unit that is not a constant cannot be inverted at plan time.
check_num "a non-constant unit still answers correctly" \
	"$(q1 "SELECT count(*) FROM pre_c WHERE date_trunc((SELECT 'day'), ts) = $DAY;")" \
	"$(q1 "SELECT count(*) FROM pre_h WHERE date_trunc((SELECT 'day'), ts) = $DAY;")"

# ---- the keys are CONSERVATIVE, and that is load-bearing -------------------
# The derived keys prune and the executor re-checks the original clause. They
# are deliberately NOT marked exact, so the batch fold refuses them (#715),
# because the fold's only per-row filter is its scan-key loop and a key that
# expressed the clause loosely would let it count rows the clause excludes.
#
# THIS ARM CANNOT CURRENTLY TELL YOU THE MARKING IS RIGHT, and that is worth
# saying rather than leaving implied. Marking the keys exact leaves this green:
# pgcolumnar_batch_type_ok admits only int2/4/8 and float4/8, so the fold refuses
# a timestamp column on the TYPE gate before exactness is ever consulted.
# Established by mutating the marking and watching the suite pass, not assumed.
#
# So the arm pins that the fold declines -- which is what a reader of the plan
# cares about -- and no more. Conservative is still the right marking, because
# it makes the claim true independently of a type list that could widen; if it
# does widen, this arm starts discriminating and exactness needs its own proof
# over every unit and every value.
check_text "the rewritten predicate does not engage the batch fold" \
	"$(q "SET pgcolumnar.enable_ungrouped_vector_agg=on;
	      EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF)
	      SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = $DAY" |
	   grep 'Columnar Batch Fold' | grep -oE 'yes|no' | head -1)" "no"
check_num "...and still answers correctly with the fold enabled" \
	"$(q1 "SET pgcolumnar.enable_ungrouped_vector_agg=on;
	       SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = $DAY;")" \
	"$(q1 "SELECT count(*) FROM pre_h WHERE date_trunc('day', ts) = $DAY;")"

# ---- the other units in the table, each proved rather than assumed ---------
# A month is not a fixed number of seconds, so `month` and `quarter` exercise
# calendar arithmetic that `day` does not.
for u in month week quarter year; do
	case $u in
		month)   c="timestamp '2024-02-01'" ;;
		week)    c="date_trunc('week', timestamp '2024-02-01')" ;;
		quarter) c="date_trunc('quarter', timestamp '2024-02-01')" ;;
		year)    c="timestamp '2024-01-01'" ;;
	esac
	uq="SELECT count(*) FROM pre_c WHERE date_trunc('$u', ts) = $c"
	uh="SELECT count(*) FROM pre_h WHERE date_trunc('$u', ts) = $c"
	check_num "unit '$u' returns the heap's answer" "$(q1 "$uq;")" "$(q1 "$uh;")"
	# and the answer is not trivially zero, or the arm proves nothing
	check_num "unit '$u': premise: it matches some rows" \
		"$([ "$(q1 "$uh;")" -gt 0 ] && echo 1 || echo 0)" 1
done

# A unit the table does not carry must DECLINE and still be correct.
check_num "an unlisted unit (millennium) still answers correctly" \
	"$(q1 "SELECT count(*) FROM pre_c WHERE date_trunc('millennium', ts) = date_trunc('millennium', timestamp '2024-02-01');")" \
	"$(q1 "SELECT count(*) FROM pre_h WHERE date_trunc('millennium', ts) = date_trunc('millennium', timestamp '2024-02-01');")"

check "backend still up" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary

#!/usr/bin/env bash
#
# pgColumnar interrupt correctness on the decode path (#254).
#
# The question this answers is which query a missing CHECK_FOR_INTERRUPTS in the
# columnar decode path actually breaks. For most queries the answer is none: the
# executor checks interrupts on its own loops, so an ordinary scan stays
# cancellable even with every interrupt check removed from this extension. That
# is why native_cancel.sh cannot detect such a regression, and why removing its
# skip would not have helped.
#
# The shape that does depend on the extension's own checks is one large row
# group. Loading and decoding a row group happens inside a single call into the
# access method, so no executor loop runs for its duration. The uninterruptible
# window is therefore the decode time of one group, and stripe_row_limit is a
# user-settable option, so that window is as long as a user makes it.
#
# Measured on PostgreSQL 17, cancel latency against a 50 ms statement_timeout:
#
#            row group           checks present   all checks removed
#            4M rows, 3 cols           62 ms            179 ms
#            12M rows, 3 cols          64 ms            526 ms
#            4M rows, 8 cols           66 ms            584 ms
#
# With the checks, latency is flat in the size of the group. Without them it
# tracks the group's decode time. That flatness is what this suite asserts.
#
# The assertion is a ratio between two cancel latencies measured back to back on
# the same machine in the same run, not a ratio against a data-volume baseline.
# Both readings move together under load, which is what makes it safe on shared
# hardware where the wall-clock ratio suites are not. It carries no PGC_SKIP_TIMING
# guard for that reason.
#
# Usage:  test/cancel_decode.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Wide rows, so one group takes long enough to decode that the window is
# unmistakable, without needing a row count that is slow to load.
DDL="a int, b int, c text, d text, e text, f text, h text, i text"
SEL="g, g %% 1000, 'r'||g, 'r'||g, 'r'||g, 'r'||g, 'r'||g, 'r'||g"
ROWS=4000000

# VERBOSITY verbose, so an ERROR line carries its SQLSTATE: a refusal is read by
# code AND message (#1341), never by a word grepped out of whatever came back.
raw() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -q -v VERBOSITY=verbose "$@" 2>&1
}

build() {			# build(table, stripe_row_limit)
	raw -c "CREATE TABLE $1 ($DDL) USING pgcolumnar;" >/dev/null
	raw -c "SELECT pgcolumnar.set_options('$1', stripe_row_limit => $2, chunk_group_row_limit => 10000);" >/dev/null
	raw -c "INSERT INTO $1 SELECT $(printf "$SEL") FROM generate_series(1, $ROWS) g;" >/dev/null
}

# The predicate matches nothing and is not pushed down, so the scan decodes the
# whole relation and hands no tuple to the executor while doing it.
#
# Every column is named on purpose. The reader decodes only the columns a query
# references (#338), so a predicate over one column would leave the other seven
# unread and the scan would finish before there was anything to cancel -- the
# premise check below would then fail, correctly, because nothing was being
# tested. Each conjunct is an expression rather than a comparison to a constant
# so that none of them is pushed down to the zone maps and used to skip groups.
PRED="(a % 7) = 999 AND (b % 7) = 999 AND length(c) < 0 AND length(d) < 0 AND length(e) < 0 AND length(f) < 0 AND length(h) < 0 AND length(i) < 0"
QUERY() { echo "SELECT count(*) FROM $1 WHERE $PRED"; }

# What a statement_timeout cancel prints, exactly: the code and the message. It used
# to be any output containing "canceling statement" (#1341).
CANCELLED="ERROR:  57014: canceling statement due to statement timeout"

# Cancel latency: milliseconds from issuing the statement to the error coming
# back, or what came back instead when the statement was not cancelled.
#
# The best of three readings, not the average. Scheduling noise on a shared
# runner only ever adds latency, so the minimum is the reading least polluted by
# it, and a missing interrupt check raises the floor rather than the spread. An
# average would let one descheduled run widen the ratio on its own.
cancel_ms() {
	local t="$1" c d out best= ms i
	for i in 1 2 3; do
		c=$(date +%s%N)
		out="$(raw -c "SET statement_timeout = 100; $(QUERY "$t")")"
		d=$(date +%s%N)
		grep -qxF "$CANCELLED" <<<"$out" || { echo "not cancelled: $(head -1 <<<"$out")"; return; }
		ms=$(( (d - c) / 1000000 ))
		[ -z "$best" ] || [ "$ms" -lt "$best" ] && best="$ms"
	done
	echo "$best"
}
is_ms() { [[ "$1" =~ ^[0-9]+$ ]]; }

groups() { raw -c "SELECT count(*) FROM pgcolumnar.stats('$1');"; }

# Backends killed so far, from the server log. A fresh connection's SELECT 1
# answers again once crash recovery is over, so it cannot say whether a cancel
# took a backend down (#1341); the log can.
crashes() {
	# grep -c prints 0 itself when nothing matches, and then exits 1
	pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null || true
}

build many 20000            # many small groups
build one   40000000        # a single group holding everything

check "the many-group table has many groups" \
	"$( [ "$(groups many)" -gt 50 ] && echo yes || echo no )" "yes"
check "the one-group table has exactly one group" "$(groups one)" "1"

# The scan must outlast the timeout, or nothing is being tested. Measured
# uncancelled, with no timeout set.
t0=$(date +%s%N); raw -c "$(QUERY one);" >/dev/null; t1=$(date +%s%N)
FULL=$(( (t1 - t0) / 1000000 ))
check "the scan outlasts the timeout, so a cancel is possible" \
	"$( [ "$FULL" -gt 300 ] && echo yes || echo no )" "yes"

CRASHES_BEFORE="$(crashes)"
MANY_MS="$(cancel_ms many)"
ONE_MS="$(cancel_ms one)"

echo "      uncancelled scan ${FULL} ms; cancel latency: many groups ${MANY_MS} ms, one group ${ONE_MS} ms"

check "a scan of many small groups is cancelled" \
	"$(is_ms "$MANY_MS" && echo yes || echo "$MANY_MS")" "yes"
check "a scan of one large group is cancelled" \
	"$(is_ms "$ONE_MS" && echo yes || echo "$ONE_MS")" "yes"

# The property. Decoding one large group must not make a statement wait longer
# for its cancel than decoding many small ones. Three times, measured back to
# back on the same machine, is loose enough to absorb scheduling noise and far
# below the factor a missing check produces.
check "cancel latency does not grow with row group size" \
	"$( is_ms "$ONE_MS" && is_ms "$MANY_MS" \
		&& [ "$ONE_MS" -le $((MANY_MS * 3 + 50)) ] && echo yes \
		|| echo "no (one group: $ONE_MS; many groups: $MANY_MS; in ms)" )" "yes"

# An absolute ceiling as well, so the ratio cannot be satisfied by both readings
# drifting upward together.
check "the cancel arrives within a generous absolute bound" \
	"$( is_ms "$ONE_MS" && [ "$ONE_MS" -lt 2000 ] && echo yes || echo "no (one group: $ONE_MS; in ms)" )" "yes"

# AFTER THE CANCEL, IN THE SAME SESSION (#1341). These three used to open a new
# psql each, so they read a backend that had never been cancelled: with a flag the
# group load sets and a cancel leaves set, the next scan in that backend refused
# (or aborted), and main passed 10 of 10. One session now takes the cancel and
# then answers all three; psql runs every -c in turn and does not stop at the
# cancel's ERROR.
SESSION="$(raw -c "SET statement_timeout = 100" -c "$(QUERY one)" -c "RESET statement_timeout" \
	-c "SELECT 'usable:' || 1" -c "SELECT 'rows:' || count(*) || '|' || sum(a) FROM one" \
	-c "SELECT 'answer:' || count(*) FROM one WHERE $PRED")"
after() {	# label -> its value from the session, or what the session printed instead
	if ! grep -qxF "$CANCELLED" <<<"$SESSION"; then
		echo "the session's scan was not cancelled: $(head -1 <<<"$SESSION")"
	elif grep -q "^$1:" <<<"$SESSION"; then
		sed -n "s/^$1://p" <<<"$SESSION"
	else
		echo "no $1 line: $( (grep '^ERROR' <<<"$SESSION" | grep -vxF "$CANCELLED"; echo "(no other error)") | head -1)"
	fi
}
check "the session is usable after the cancel" "$(after usable)" "1"
# rows reads a column's sum as well as the count, so the data is checked and not
# only how many rows there are. It is the predicate's scan below that loads the
# group again in this session; under the stale-flag mutant it is the one that fails.
check "the table still reads correctly after the cancel" "$(after rows)" "$ROWS|$(( ROWS * (ROWS + 1) / 2 ))"
check "the cancelled predicate returns its true answer when allowed to finish" "$(after answer)" "0"

# No backend died on the way, read from the server log once the server answers
# again, so a crash that recovery has already hidden still counts (#1341).
for i in $(seq 1 60); do [ "$(raw -c 'SELECT 1')" = "1" ] && break; sleep 0.5; done
check "no backend crashed while the scans were cancelled" \
	"$(( $(crashes) - CRASHES_BEFORE ))" "0"

pgc_summary

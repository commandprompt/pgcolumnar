#!/usr/bin/env bash
#
# A scan must not answer a backward fetch with forward rows.
#
# pgcolumnar_scan_getnextslot takes a ScanDirection and never read it: every
# call advanced forward. heap_getnextslot passes direction through to
# heapgettup, which walks the other way. So a scrollable cursor over a columnar
# relation, on a plan that reaches the table AM rather than the custom scan,
# silently returned the wrong rows:
#
#     FETCH FORWARD 5   then   FETCH BACKWARD 3
#     heap:     1 2 3 4 5  then  4 3 2
#     columnar: 1 2 3 4 5  then  6 7 8
#
# and FETCH LAST returned nothing at all where the heap returned row 20.
#
# A true backward scan over native storage is a feature, not a bug fix: the
# reader walks row groups and decodes vectors forwards. What this pins is that
# the AM never INVENTS an answer. Forward is unchanged, no-movement fetches
# nothing, and a backward fetch raises 0A000 rather than returning rows that were
# never asked for.
#
# The arms assert SQLSTATE rather than message text. A grep for "not supported"
# is also satisfied by a dozen unrelated errors, and by a connection failure.
#
# Usage:  test/scan_direction.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

make_pair "id int, v text"
load_pair "SELECT g, 'row' || g FROM generate_series(1,20) g"
check_num "premise: the pair holds the same row count" \
	"$(q 'SELECT count(*) FROM t_col')" "$(q 'SELECT count(*) FROM t_heap')"

cur() {  # cur <table> <guc-statement> <cursor-body>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$2" -c "BEGIN; DECLARE c SCROLL CURSOR FOR SELECT id FROM $1; $3 CLOSE c; COMMIT;" 2>&1
}
# psql prints the SQLSTATE ahead of the message under VERBOSITY verbose, which
# is how these arms assert the code rather than the text. A grep for wording is
# also satisfied by an unrelated error or by a failed connection.
state_of() {  # state_of <table> <guc-statement> <cursor-body>
	local out rc code
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -v VERBOSITY=verbose \
		-c "\\set VERBOSITY verbose" -c "$2" \
		-c "BEGIN; DECLARE c SCROLL CURSOR FOR SELECT id FROM $1; $3 CLOSE c; COMMIT;" 2>&1)"
	rc=$?
	# "psql:...: ERROR:  0A000: message"  ->  0A000
	code="$(printf '%s\n' "$out" | sed -n 's/.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\):.*/\1/p' | head -1)"
	# 00000 only when psql ran the statements and exited 0: a failed connection
	# prints no ERROR line either, and used to read 00000, so both controls passed.
	if [ -n "$code" ]; then printf '%s\n' "$code"
	elif [ "$rc" -eq 0 ]; then printf '00000\n'
	else printf 'no reading (psql rc %s: %s)\n' "$rc" "$(printf '%s' "$out" | head -1)"; fi
}

# same_rows HEAP_OUTPUT COL_OUTPUT -> matches, DIFFERS, or "no reading" when either
# side is empty or an error. Two identical connection errors used to read "matches".
same_rows() {
	if [ -z "$1" ] || [ -z "$2" ] || grep -qi 'error' <<<"$1$2"; then
		echo "no reading (heap [$(head -1 <<<"$1")], columnar [$(head -1 <<<"$2")])"
	elif [ "$1" = "$2" ]; then echo matches
	else echo DIFFERS; fi
}

FWDBACK="FETCH FORWARD 5 FROM c; FETCH BACKWARD 3 FROM c;"
FWDONLY="FETCH FORWARD 5 FROM c;"

# ---- at shipped defaults the custom scan handles the cursor ----------------
h_def="$(cur t_heap 'SELECT 1;' "$FWDBACK")"
c_def="$(cur t_col  'SELECT 1;' "$FWDBACK")"
check "at shipped defaults the cursor matches heap" "$(same_rows "$h_def" "$c_def")" "matches"
[ "$h_def" != "$c_def" ] && { echo "-- heap:     $(printf '%s' "$h_def" | tr '\n' ' ')";
                              echo "-- columnar: $(printf '%s' "$c_def" | tr '\n' ' ')"; }

# ---- with the custom scan off, the table AM is reached ---------------------
OFF="SET pgcolumnar.enable_custom_scan=off;"
plan_off="$(q "SET pgcolumnar.enable_custom_scan=off; EXPLAIN (COSTS OFF) SELECT id FROM t_col;" | tr '\n' ' ')"
check "with the custom scan off the plan really is a Seq Scan on the AM" \
	"$(case "$plan_off" in *"Seq Scan"*) echo yes ;; *) echo "no: $plan_off" ;; esac)" "yes"

# forward-only must be unchanged
check "forward-only fetch still matches heap through the AM" \
	"$(same_rows "$(cur t_heap "$OFF" "$FWDONLY")" "$(cur t_col "$OFF" "$FWDONLY")")" "matches"

# a backward fetch must REFUSE, not invent rows
st_back="$(state_of t_col "$OFF" "FETCH FORWARD 5 FROM c; FETCH BACKWARD 3 FROM c;")"
echo "-- backward fetch SQLSTATE through the AM: ${st_back:-<none>}"
check "a backward fetch through the AM raises 0A000 rather than answering" "$st_back" "0A000"

st_last="$(state_of t_col "$OFF" "FETCH LAST FROM c;")"
echo "-- FETCH LAST SQLSTATE through the AM: ${st_last:-<none>}"
check "FETCH LAST through the AM raises 0A000 rather than returning nothing" "$st_last" "0A000"

# and the heap, unchanged, must NOT raise
st_heap="$(state_of t_heap "$OFF" "FETCH FORWARD 5 FROM c; FETCH BACKWARD 3 FROM c;")"
check "control: the same cursor on a heap table raises nothing" "$st_heap" "00000"

# control: the probe can distinguish states at all
st_ok="$(state_of t_col "$OFF" "$FWDONLY")"
check "control: a forward-only fetch reports success, so the probe is not stuck on 0A000" \
	"$st_ok" "00000"

pgc_summary

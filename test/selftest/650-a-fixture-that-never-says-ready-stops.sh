# ---- a fixture that never says READY stops the suite (#2165) -----------------
#
# pgc_await_ready waits for a fixture's READY line and, when none comes, ends the
# suite with a FATAL naming the fixture. iceberg_rest_scan and iceberg_rest_vended
# used to wait 5 s and carry on, so their arms talked to whatever answered on the
# port: in CI another suite's REST fixture, which gave HTTP 401. Those suites
# reach pgc_await_ready only on the path where READY arrives, so nothing there
# would notice a FATAL branch that returns 0 (review of #2208). Each arm here runs
# it in a subshell, against a real process, and reads its status and its words.
#
# The never-READY arm uses PGC_READY_WAIT=1, so it waits one second, not 30. The
# exit arm waits up to 20 and must be done well inside that: it stops when the
# fixture's process is gone, rather than waiting the whole limit out.

_ar_tmp="$(mktemp -d)"
_ar_run() {	# _ar_run WAIT OUT PID WHAT -> pgc_await_ready's output, then rc=N
	local _o _rc
	_o="$(PGC_READY_WAIT="$1"; pgc_await_ready "$2" "$3" "$4" 2>&1)"
	_rc=$?
	printf '%s rc=%s' "$_o" "$_rc"
}

# (a) The fixture exits without READY.
bash -c 'echo "bind: Address already in use"; exit 3' > "$_ar_tmp/exits.out" 2>&1 &
_ar_pid=$!
_ar_t0=$SECONDS
_ar_got="$(_ar_run 20 "$_ar_tmp/exits.out" "$_ar_pid" "test fixture on port 1")"
_ar_took=$(( SECONDS - _ar_t0 ))
check_text "a fixture that exits without READY stops the suite with a FATAL naming it" \
	"$_ar_got" "FATAL: the test fixture on port 1 did not start (exited): bind: Address already in use  rc=1"
check_text "and the wait ends when the fixture exits, not at the limit" \
	"$([ "$_ar_took" -lt 10 ] && echo early || echo "after ${_ar_took} s of 20")" "early"

# (b) The fixture says READY late, well inside the limit.
( sleep 1; echo READY; sleep 30 ) > "$_ar_tmp/late.out" 2>&1 &
_ar_pid=$!
check_text "a fixture that says READY late is waited for" \
	"$(_ar_run 10 "$_ar_tmp/late.out" "$_ar_pid" "late fixture")" " rc=0"
kill "$_ar_pid" 2>/dev/null
wait "$_ar_pid" 2>/dev/null

# (c) The fixture runs and never says READY.
sleep 30 > "$_ar_tmp/silent.out" 2>&1 &
_ar_pid=$!
check_text "a fixture that never says READY stops the suite with a FATAL naming it" \
	"$(_ar_run 1 "$_ar_tmp/silent.out" "$_ar_pid" "silent fixture")" \
	"FATAL: the silent fixture did not start (still running with no READY after 1 s):  rc=1"
kill "$_ar_pid" 2>/dev/null
wait "$_ar_pid" 2>/dev/null

rm -rf "$_ar_tmp"
unset -f _ar_run
unset _ar_tmp _ar_pid _ar_t0 _ar_got _ar_took

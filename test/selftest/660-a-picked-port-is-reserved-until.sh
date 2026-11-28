# ---- a picked port is reserved until it is bound (#2165) ---------------------
#
# pgc_pick_free_port proves a port free and returns it, and the caller binds it
# later. run_all_versions runs suites in parallel, so two suites whose walks met
# at one free port were both given it: in CI iceberg_rest_scan and
# iceberg_rest_vended both got 31732, and the scan's arms got HTTP 401 from the
# vended suite's REST fixture. The picker now keeps a reservation protocol, which
# portlib.sh states above pgc_port_reserve_dir: a lock per user, a file per
# reserved port naming the picker's pid and the time, and a lifetime.
#
# THESE ARMS DRIVE THE SHELL PICKER ONLY. The pytest picker keeps the same
# protocol, and its arms are in test_runshape.py, with the same shapes; neither
# harness runs the other's picker (CONTEXT.md, "Tests"). Where an arm needs
# "another picker", it plays one by the protocol: it takes the lock itself, or
# writes a reservation file of its own.
#
# Each picker is a separate bash, as a suite is, and reserves in a directory of
# this part's own, so nothing here sees or leaves a reservation in the real one.
# The collision arm starts every picker from one seed and binds nothing until all
# have picked, which holds the probe-to-bind gap open; PGC_PORT_PICK_DELAY=1
# also holds each pick's probe-to-reservation gap open, so that without the lock
# the pickers would all probe the port free before any reserved it.

_rp_tmp="$(mktemp -d)"
_rp_lib="$PGC_TESTDIR/portlib.sh"

_rp_pick() {	# _rp_pick DIR SEED [DELAY] -> "PID PORT" of one picker, or its error
	local _o _rc
	_o="$(PGC_PORT_RESERVE_DIR="$1" PGC_PORT_PICK_DELAY="${3:-}" bash -c \
		'. "$1"; echo "$$"; pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" "$2"' \
		pick "$_rp_lib" "$2" 2>&1)"
	_rc=$?
	if [ "$_rc" -eq 0 ]; then printf '%s\n' "$_o" | tr '\n' ' ' | sed 's/ $//'; else printf 'rc=%s: %s' "$_rc" "$_o"; fi
}

_rp_port() {	# _rp_port "PID PORT" -> PORT, or the whole text when it is no pick
	case "$1" in
		*' '*) printf '%s' "${1##* }" ;;
		*) printf '%s' "$1" ;;
	esac
}

_rp_bind_all() {	# _rp_bind_all PORT... -> "bound" per port bound at once, or why not
	python3 -c '
import socket, sys
held, got = [], []
for p in sys.argv[1:]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.append(s)
    try:
        s.bind(("127.0.0.1", int(p)))
        got.append("bound")
    except (OSError, ValueError) as e:
        got.append(f"{p}: {e}")
print(" ".join(got))
' "$@"
}

# (a) Four suites pick at once from one seed.
for _rp_i in 0 1; do
	_rp_pick "$_rp_tmp/alone$_rp_i" 7 1 > "$_rp_tmp/alone$_rp_i.out" &
done
wait
_rp_alone="$(_rp_port "$(cat "$_rp_tmp/alone0.out")") $(_rp_port "$(cat "$_rp_tmp/alone1.out")")"
check "premise: two shell pickers that share no reservations take one port" \
	"$(printf '%s\n' $_rp_alone | sort -u | wc -l | tr -d ' ')" "1"

for _rp_i in 0 1 2 3; do
	_rp_pick "$_rp_tmp/four" 7 1 > "$_rp_tmp/four$_rp_i.out" &
done
wait
_rp_four=""
for _rp_i in 0 1 2 3; do
	_rp_four="$_rp_four $(_rp_port "$(cat "$_rp_tmp/four$_rp_i.out")")"
done
check "four shell suites picking from one seed at once get four ports" \
	"$(printf '%s\n' $_rp_four | sort -u | wc -l | tr -d ' ') ($_rp_four )" "4 ($_rp_four )"
check_text "and each shell suite binds the port it was given" \
	"$(_rp_bind_all $_rp_four)" "bound bound bound bound"

# (b) Another picker holds the lock, and reserves the port this pick would take
# just before it lets go. The pick must wait for it, then skip that port.
_rp_first="$(_rp_port "$(_rp_pick "$_rp_tmp/lonely" 54)")"
check "premise: the shell picker alone takes a port" \
	"$(case "$_rp_first" in ''|*[!0-9]*) echo "$_rp_first" ;; *) echo "a port" ;; esac)" "a port"
mkdir -p "$_rp_tmp/held"
(
	flock 9
	: > "$_rp_tmp/held.locked"
	sleep 3
	printf '%s %s\n' "$BASHPID" "$(date +%s)" > "$_rp_tmp/held/$_rp_first"
) 9>>"$_rp_tmp/held/lock" &
_rp_holder=$!
for _rp_i in $(seq 1 50); do [ -e "$_rp_tmp/held.locked" ] && break; sleep 0.1; done
_rp_t0=$SECONDS
_rp_got="$(_rp_port "$(_rp_pick "$_rp_tmp/held" 54)")"
_rp_took=$(( SECONDS - _rp_t0 ))
wait "$_rp_holder" 2>/dev/null
check_text "a pick waits while another picker holds the lock, and skips the port it reserved" \
	"$([ "$_rp_took" -ge 2 ] && echo waited || echo "returned after $_rp_took s"), $(
		[ "$_rp_got" != "$_rp_first" ] && [ -n "$_rp_got" ] && [ -z "${_rp_got//[0-9]/}" ] \
			&& echo moved || echo "took $_rp_got")" \
	"waited, moved"

# (c) The reservation a pick leaves names the picker's pid and the time.
_rp_pp="$(_rp_pick "$_rp_tmp/named" 54)"
_rp_now=$(date +%s)
_rp_res="$_rp_tmp/named/$(_rp_port "$_rp_pp")"
if [ -f "$_rp_res" ]; then
	read -r _rp_owner _rp_then _ < "$_rp_res"
	_rp_mt="$(stat -c %Y "$_rp_res")"
	_rp_named="pid $([ "$_rp_owner" = "${_rp_pp%% *}" ] && echo ok || echo "$_rp_owner not ${_rp_pp%% *}")"
	_rp_named="$_rp_named, time $(_rp_d=$(( ${_rp_then:-0} - _rp_now )); [ "${_rp_d#-}" -le 5 ] && echo ok || echo "$_rp_then not ~$_rp_now")"
	_rp_named="$_rp_named, mtime $(_rp_d=$(( _rp_mt - _rp_now )); [ "${_rp_d#-}" -le 5 ] && echo ok || echo "$_rp_mt not ~$_rp_now")"
else
	_rp_named="no reservation file for [$_rp_pp]"
fi
check_text "a pick reserves its port in a file that names the picker's pid and the time" \
	"$_rp_named" "pid ok, time ok, mtime ok"

# (d) The default directory, which the pytest picker states too.
check_text "with no PGC_PORT_RESERVE_DIR, a pick reserves in pgcolumnar-ports-<uid> under TMPDIR, or under /tmp" \
	"$(env -u PGC_PORT_RESERVE_DIR -u TMPDIR bash -c '. "$1"; pgc_port_reserve_dir' d "$_rp_lib") | $(
		env -u PGC_PORT_RESERVE_DIR TMPDIR="$_rp_tmp" bash -c '. "$1"; pgc_port_reserve_dir' d "$_rp_lib")" \
	"/tmp/pgcolumnar-ports-$(id -u) | $_rp_tmp/pgcolumnar-ports-$(id -u)"

# (e) How long a reservation holds: while its picker runs, at any age, and for
# 120 s once it has gone. Each arm learns the port the picker takes alone,
# rewrites that port's reservation with an owner and an age, and asks again in
# the same directory. The gone owner is pid_max + 1, which no process can have;
# 60 s and 121 s sit either side of the limit with room for a slow second.
_rp_repick() {	# _rp_repick OWNER AGE -> same | moved | the picker's error
	local _d="$_rp_tmp/ttl-$1-$2" _p1 _p2 _then
	_p1="$(_rp_port "$(_rp_pick "$_d" 54)")"
	case "$_p1" in ''|*[!0-9]*) printf '%s' "$_p1"; return ;; esac
	_then=$(( $(date +%s) - $2 ))
	printf '%s %s\n' "$1" "$_then" > "$_d/$_p1"
	touch -d "@$_then" "$_d/$_p1"
	_p2="$(_rp_port "$(_rp_pick "$_d" 54)")"
	case "$_p2" in
		"$_p1") printf same ;;
		''|*[!0-9]*) printf '%s' "$_p2" ;;
		*) printf moved ;;
	esac
}
_rp_dead=$(( $(cat /proc/sys/kernel/pid_max) + 1 ))
check_text "a reservation 60 s old whose picker has gone blocks the pick" \
	"$(_rp_repick "$_rp_dead" 60)" "moved"
check_text "a reservation 121 s old whose picker has gone does not block the pick" \
	"$(_rp_repick "$_rp_dead" 121)" "same"
check_text "a reservation an hour old whose picker still runs blocks the pick" \
	"$(_rp_repick "$$" 3600)" "moved"

rm -rf "$_rp_tmp"
unset -f _rp_pick _rp_port _rp_bind_all _rp_repick
unset _rp_tmp _rp_lib _rp_i _rp_alone _rp_four _rp_first _rp_holder _rp_t0 _rp_got _rp_took \
	_rp_pp _rp_now _rp_res _rp_owner _rp_then _rp_mt _rp_named _rp_dead

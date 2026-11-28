# ---- the port walk must WRAP, not walk off the ceiling (#548) ---------------
#
# pgc_pick_free_port seeds a base from the PID and scans forward. It used to
# `break` at hi, so a seed near the ceiling got a truncated scan: the failure
# needs the ports at the top of the band to be BUSY, and then the walk gives up
# with the whole band free beneath it. About 1 replication run in 2000, which
# across five majors is roughly 1 CI run in 400, presenting as an unattributable
# red that moved between majors.
#
# The band must be made busy to test this. A first version of these checks asked
# the picker for a port with the band EMPTY, where the old code also succeeds --
# they passed with the no-wrap walk restored, which is to say they tested
# nothing. The stub is the whole point.

_w=$(( PGC_AUX_PORT_HI - PGC_AUX_PORT_LO ))
check "premise: the auxiliary band has a width to wrap within" \
	"$([ "$_w" -gt 400 ] && echo yes || echo no)" "yes"

_realfree=$(declare -f pgc_port_free)

# The picks below reserve what the stubs call free (#2165), so they reserve in a
# directory of their own. In the shared one a concurrent run's reservation of
# lo+500 would move the far pick, and these would hold ports nothing binds.
_rd_was="${PGC_PORT_RESERVE_DIR-unset}"
PGC_PORT_RESERVE_DIR="$(mktemp -d)"
export PGC_PORT_RESERVE_DIR

# Every port in the TOP 400 is busy; everything below is free. A walk that stops
# at hi finds nothing from a base inside that region. A walk that wraps lands in
# the free part below.
pgc_port_free() { [ "$1" -lt $(( PGC_AUX_PORT_HI - 400 )) ]; }
check "premise: the stub really does refuse the top of the band" \
	"$(pgc_port_free $(( PGC_AUX_PORT_HI - 1 )) && echo free || echo busy)" "busy"
check "premise: and really does allow the bottom" \
	"$(pgc_port_free "$PGC_AUX_PORT_LO" && echo free || echo busy)" "free"

_top="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" $(( _w - 1 )) || echo NONE)"
check "a seed at the ceiling wraps past a busy top and still finds a port" \
	"$([ "$_top" != "NONE" ] && [ -n "$_top" ] && echo yes || echo no)" "yes"
check "and the port it found is below the busy region, which is where wrapping lands" \
	"$([ "$_top" != "NONE" ] && [ "$_top" -lt $(( PGC_AUX_PORT_HI - 400 )) ] && [ "$_top" -ge "$PGC_AUX_PORT_LO" ] && echo yes || echo no)" \
	"yes"

# A band with every port busy must report itself full rather than spin. The
# bound is half the fix; without it the wrap turns a hard failure into a hang,
# which is worse than the failure it replaces.
pgc_port_free() { return 1; }
_none="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" 5 || echo NONE)"
check "an entirely busy band reports itself full and terminates" "$_none" "NONE"

# The caller says "no free port for the restore cluster in [lo,hi)". That names
# the WHOLE band, so the picker has to have swept the whole band before it may
# say nothing is free. It used to stop after 300 probes, which meant a free port
# 500 away from the base was reported as a full band -- #548's own defect
# surviving inside #548's fix, and #537's defect in the message.
#
# One free port, deliberately further from the base than the old 300 bound.
pgc_port_free() { [ "$1" = "$(( PGC_AUX_PORT_LO + 500 ))" ]; }
check "premise: the stub frees exactly one port, 500 past the floor" \
	"$(pgc_port_free $(( PGC_AUX_PORT_LO + 500 )) && echo free || echo busy)/$(pgc_port_free $(( PGC_AUX_PORT_LO + 200 )) && echo free || echo busy)" \
	"free/busy"
_far="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" 0 || echo NONE)"
check "a free port beyond the old 300-probe bound is still found" \
	"$_far" "$(( PGC_AUX_PORT_LO + 500 ))"

eval "$_realfree"
rm -rf "$PGC_PORT_RESERVE_DIR"
if [ "$_rd_was" = unset ]; then unset PGC_PORT_RESERVE_DIR; else PGC_PORT_RESERVE_DIR="$_rd_was"; fi
check "premise: the real prober was restored, or every check after this lies" \
	"$(pgc_port_free 1 && echo probing || echo stubbed)" "probing"



#!/usr/bin/env bash
#
# One definition of the port a run starts on.
#
# Every suite stands up its own throwaway cluster, so every suite needs a port,
# and each one used to carry a different fixed default: 54321 for smoke, 54322
# for phase2, and so on. Distinct within a run, which is what made the matrix
# work, and identical across runs, which made two runs on one machine contend by
# construction rather than by accident.
#
# #184 fixed that for lib.sh and for the matrix runner, and explained the cost
# better than this comment can: the collision surfaces as a wall of
# `database "regress" already exists` with no named check failing, which reads
# exactly like a real failure and was twice misread as one. It did not reach the
# ten suites that carry their own harness instead of using lib.sh, which is what
# this file is for -- they were still starting on 54321 through 54327.
#
# The retry in lib.sh stays. The point is that a default should not guarantee the
# collision it then has to recover from.
#
# run_all_versions.sh keeps its own copy of this arithmetic rather than sourcing
# this file, and that is deliberate. It re-executes itself from a private copy in
# /tmp so that editing the driver cannot corrupt a run in progress (#184), and at
# that point it is detached from the tree: a tree-relative source would be
# exactly the fragility the re-exec exists to remove. Two copies of one
# expression is the smaller cost. If the range here changes, change it there.
#
# Written fresh for pgColumnar.

# The port this run starts on. Derived from the pid, matching #184's derivation
# in lib.sh and run_all_versions.sh exactly, so there is one range and not two.
# PGC_RUN_OWNER is set by the matrix runner when it re-executes itself, so a
# re-executed driver keeps the port block its parent picked rather than drawing
# a second one.
#
# The range stops well short of 65535 on purpose: the matrix walks upward from
# its base once per suite per major, several hundred ports, and a base above the
# ceiling once made every cluster fail with `invalid port number: "66009"`,
# which presents as a broken tree rather than as a bad constant.
#
# It must also stop short of the EPHEMERAL range, which is the harder constraint
# and was got wrong for a long time. On this box:
#
#     /proc/sys/net/ipv4/ip_local_port_range  ->  32768   60999
#
# The band was 40000-59999, entirely inside that. An outbound connection -- psql
# reaching the primary, pg_basebackup, a walsender -- can be assigned any free
# port in the ephemeral range as its LOCAL port, including the one a cluster is
# about to bind. Probing that the port is free does not help: the probe and the
# bind are different instants, and the kernel is free to hand the port out in
# between.
#
# That is not theoretical. It is the intermittent replication failure this project
# chased across many matrices:
#
#     could not bind IPv4 address "127.0.0.1": Address already in use
#     HINT: Is another postmaster already running on port 33500?
#
# with nothing listening on that port before or after. It moved between majors
# every run, because which connection lands on which port is a race, and it left
# no evidence behind, because the ephemeral socket that won closed moments later.
#
# So the band is derived from the floor rather than hardcoded, and sits entirely
# below it. A port below the ephemeral floor is never handed to an outbound
# connection, so "free when probed" and "still free at bind" are the same fact.
pgc_ephemeral_floor() {
	local lo
	lo="$(awk '{print $1}' /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null)"
	case "$lo" in
		''|*[!0-9]*) lo=32768 ;;
	esac
	printf '%s\n' "$lo"
}

# Two disjoint bands below the floor.
#
#   PGC_PORT_*      the suites' own clusters. The matrix walks upward from a base
#                   in this band, one port per suite per major.
#   PGC_AUX_PORT_*  extra clusters a single suite stands up beyond its own -- the
#                   replication standby and its restore cluster. Separate so that
#                   the matrix's walk cannot wander into a port a suite is about
#                   to use, which the free-probe would then have to catch rather
#                   than the layout preventing.
#
# ip_local_port_range's low value is a sysctl. It is 32768 here and on
# ubuntu-latest, but an image is free to set it lower, and a band derived from a
# floor that leaves no room beneath it would be tiny, empty, or negative. That
# must fail loudly: silently falling back into the ephemeral range would reinstate
# exactly the race this file exists to remove, and a picker that starves is worse
# than one that says why.
PGC_EPH_FLOOR="$(pgc_ephemeral_floor)"

# Below this there is no room for two bands plus the matrix's walk.
PGC_MIN_FLOOR=20000

if [ "$PGC_EPH_FLOOR" -lt "$PGC_MIN_FLOOR" ]; then
	echo "pgcolumnar: ephemeral floor $PGC_EPH_FLOOR too low to carve a private port band" >&2
	echo "  /proc/sys/net/ipv4/ip_local_port_range starts at $PGC_EPH_FLOOR; this needs" >&2
	echo "  at least $PGC_MIN_FLOOR so cluster ports can sit below the range the kernel" >&2
	echo "  assigns to outbound connections. Raise it:" >&2
	echo "      sysctl -w net.ipv4.ip_local_port_range=\"32768 60999\"" >&2
	echo "  Running inside the ephemeral range makes clusters lose ports between the" >&2
	echo "  free-check and the bind, intermittently and with no evidence left behind." >&2
	exit 1
fi

# Auxiliary band: the 2000 ports just under the floor, less a 1000-port guard.
PGC_AUX_PORT_HI=$(( PGC_EPH_FLOOR - 1000 ))
PGC_AUX_PORT_LO=$(( PGC_AUX_PORT_HI - 2000 ))

# Main band: from 10000 up to the auxiliary band, less the room the matrix needs
# to walk upward from its base.
PGC_PORT_LO=10000
PGC_PORT_HI=$(( PGC_AUX_PORT_LO - 200 ))
# Room for one port per suite per major, with slack. The matrix asserts its actual
# demand against this rather than trusting the constant (see run_all_versions.sh).
PGC_PORT_WALK=1500

# Kept for callers that only need "the top of the safe range".
PGC_PORT_CEIL="$PGC_PORT_HI"

pgc_pick_port() {
	local span
	span=$(( PGC_PORT_HI - PGC_PORT_LO - PGC_PORT_WALK ))
	if [ "$span" -lt 1000 ]; then
		echo "pgcolumnar: safe port band [$PGC_PORT_LO,$PGC_PORT_HI) too narrow" >&2
		echo "  ephemeral floor is $PGC_EPH_FLOOR; need room for $PGC_PORT_WALK ports of walk" >&2
		exit 1
	fi
	printf '%s\n' "$(( PGC_PORT_LO + (${PGC_RUN_OWNER:-$$} % span) ))"
}

# True when nothing is accepting connections on the given port.
#
# Lives here rather than in lib.sh because the band and the probe are one idea:
# below the ephemeral floor a port that probes free is still free at bind time,
# and a harness that has the band without the probe is only half-protected.
# Suites that carry their own harness (pg_upgrade, unique_conc, concurrency,
# harness_selftest) source this file and get both.
pgc_port_free() {
	! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# A free port from a band, verified rather than assumed. Callers pass the band so
# a suite standing up extra clusters can draw from AUX while the matrix walks MAIN.
pgc_pick_free_port() {
	local lo="$1" hi="$2" seed="${3:-$$}" width base span i p

	width=$(( hi - lo ))
	[ "$width" -le 0 ] && return 1
	base=$(( lo + (seed % width) ))

	# The walk WRAPS to lo, and is bounded by the band width (#548).
	#
	# It used to run `seq base base+300` and `break` at hi, so a seed landing
	# near the ceiling got a truncated scan and a seed landing on hi-1 got a scan
	# of one port. The caller then reported the band full while the whole band
	# beneath it was free. Resizing the band does not fix that: the failure needs
	# a base within one port of the ceiling, which stays a fixed fraction of the
	# width whatever the width is.
	#
	# The bound matters as much as the wrap. Without it a genuinely full band
	# spins forever rather than reporting itself full.
	#
	# The bound is the WHOLE BAND, not a 300-probe budget. A budget reintroduces
	# the defect it was meant to fix one layer up: the caller reports "no free
	# port in [lo,hi)", naming 2000 ports, on the strength of 300 probes, so a
	# free port 500 away from the base reads as a full band. Sweeping the band is
	# what makes that message true, which is #537's rule applied here.
	#
	# It costs nothing that matters. The picker returns on the first free port, so
	# a full sweep happens only when the band really is full, which is the failure
	# path. Measured on the bench container: 2000 probes take 856 ms, against
	# 191 ms for 300, and neither is paid on the success path.
	span=$width
	for i in $(seq 0 $(( span - 1 ))); do
		p=$(( lo + ((base - lo + i) % width) ))
		if pgc_port_free "$p"; then
			printf '%s\n' "$p"
			return 0
		fi
	done
	return 1
}

# ---- a lock taken by the caller does not reach this run's servers (#1915) ----
#
# Loaded by every suite that starts a cluster, so this is the one place that
# covers them all. A run started as `flock <lock> bash test/x.sh` inherits the
# lock's descriptor, and so does every process it starts, a postmaster
# included. A postmaster that outlived the command then held the lock: the
# reviewer of #1906 found five runs of one major waiting 25 minutes on a
# cluster nobody had stopped.
#
# Closing our copy does not release the lock. flock(1) keeps its own copy
# open until the command ends, so runs stay serialised; our children just do
# not get one. This is what `flock -o` does, applied whether or not the caller
# remembered it.
#
# Only a lock that a LIVE OTHER process took is closed: then that process
# still holds its own copy. fdinfo's lock line names the pid that took the
# lock. `exec 8>f; flock 8` takes it through a flock(1) child that has already
# exited, so there our copy may be the only one, and closing it would release
# the lock. Measured: closing on "not our pid" alone released exactly that.
# $BASHPID, not $$: in a subshell $$ names the parent.
pgc_drop_inherited_locks() {
	local f n taker
	for f in /proc/"$BASHPID"/fdinfo/[0-9]*; do
		n="${f##*/}"
		[ "$n" -gt 2 ] 2>/dev/null || continue
		# The list includes the descriptor the glob itself read the directory
		# through, which is closed by now, so a read can fail. Under set -e and
		# pipefail that failure ended the caller's shell (review of #1928).
		taker="$(sed -nE 's/^lock:.*FLOCK +[A-Z]+ +[A-Z]+ +([0-9]+) .*/\1/p' "$f" 2>/dev/null | head -1)" || taker=""
		[ -n "$taker" ] && [ "$taker" != "$BASHPID" ] && [ -d "/proc/$taker" ] || continue
		eval "exec $n>&-"
	done
}
pgc_drop_inherited_locks

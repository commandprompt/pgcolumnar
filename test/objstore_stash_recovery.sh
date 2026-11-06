#!/usr/bin/env bash
#
# Recovery from a stash left behind by an interrupted objstore_module run (#393).
#
# objstore_module.sh moves the installed module aside to reach the absent and the
# broken paths. An interrupt between a move and its restore leaves a
# pgcolumnar_objstore.so.probe or .away in pkglibdir, and that suite's start guard
# then refuses to run.
#
# Refusing is right when the stash is the only surviving copy of the module:
# continuing would overwrite it with the run's 19-byte stand-in and destroy the
# installation. Refusing FOREVER is not. Every matrix leg runs `make install`, so
# the ordinary leftover is debris sitting beside a perfectly good module, and the
# suite stays red on that major until a person notices and moves a file by hand.
#
# It is not hypothetical. On 2026-08-06 a .probe from 15:09 turned PG17 red in a
# five-major matrix while 119 other suites passed, and the run before it had been
# interrupted hours earlier. The guard's own advice -- `mv "$stash" "$MOD"` -- was
# by then wrong as well: make install had already restored the module, so
# following it would have replaced a freshly built module with a stale one.
#
# What separates the two cases is not whether a stash is PRESENT. It is whether
# the module beside it is VALID. Presence alone cannot tell debris from the only
# real copy, and an interrupt inside objstore_module's broken-module arm leaves a
# 19-byte stand-in installed with the real module at .away -- deleting the stash
# there is the destructive move the guard exists to prevent.
#
# Tested at the only seam that matters: run the suite, then look at its exit
# status and at what it left in pkglibdir. Nothing here reaches inside the guard,
# so the guard can be rewritten without touching this file.
#
# Usage:  test/objstore_stash_recovery.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PG_CONFIG="${1:-/usr/local/pg17/bin/pg_config}"

# WHICH MAJOR THIS SUITE RAN ON (#1121, the wider half of #1109). `pgc_record`
# writes `${PGC_MAJOR:-unknown}`, and PGC_MAJOR is set inside `pgc_setup` -- which
# this suite does not call, deliberately. Without this line every record it emits
# says `unknown`, and a ledger row claiming `unknown` matches no run, so none of
# these checks could ever be seeded or matched again.
#
# The runner passes the pg_config as $1 to EVERY suite, including those that need
# no cluster, so it is available here. `pgc_major_of` returns empty on a path it
# cannot run, which degrades to exactly today's `unknown` rather than to a WRONG
# major -- a guessed major would seed a row claiming a major the check was never
# observed on, which is worse than saying nothing.
PGC_MAJOR="$(pgc_major_of "$PG_CONFIG")"
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE="$SRCDIR/test/objstore_module.sh"

# nm decides "valid module" for every check below. Without it the discriminator
# silently answers "not valid" for everything, which would make the destructive
# branch look correct.
pgc_require_tools nm || { pgc_summary; exit 1; }

echo "== pgColumnar test: $(basename "$0") =="
echo "PG_CONFIG=$PG_CONFIG"

# No cluster of our own: this suite asserts on files and on another suite's exit
# status, and objstore_module.sh stands up its own. It does need the module
# installed, so build once here and let each invocation skip it.
if [ -z "${PGC_SKIP_BUILD:-}" ]; then
	# THE HARNESS BUILDER, NOT A HAND-ROLLED make (#1220). It asks the OBJECTS
	# which major built them (#1219) and keeps the build stamp (#536), neither of
	# which the two makes below it had. This suite's own vocabulary is kept: a
	# build that did not happen must be REPORTED here, not merely exited on,
	# because the accounting line is what says nothing measured the guard.
	pgc_build_and_install "$SRCDIR" "$PG_CONFIG" "$PGC_MAJOR" || {
		pgc_fail "the build or install failed, so nothing below measures the guard"
		pgc_summary
	}
fi

LIBDIR="$("$PG_CONFIG" --pkglibdir)"
MOD="$LIBDIR/pgcolumnar_objstore.so"

# The discriminator under test, computed here independently of the suite's copy
# of it. A module is valid when it defines the entry point, which is the same
# question objstore_module.sh asks with nm at its positive control.
mod_is_valid() {
	[ -e "$1" ] || return 1
	[ "$(nm -D --defined-only "$1" 2>/dev/null | grep -c ' T pgcolumnar_objstore_init')" -ge 1 ]
}

# Safety net, and deliberately NOT the logic under test: a pristine copy taken
# before anything moves. If the fix is wrong and leaves the installation broken,
# this restores it from that copy rather than from a rule this suite is asserting.
# Without it, a failing run of this suite leaves the box in exactly the state it
# exists to recover from, and poisons every later run on this major.
SAFE=""
cleanup() {
	rm -f "$MOD.probe" "$MOD.away"
	if [ -n "$SAFE" ] && [ -e "$SAFE" ]; then
		mod_is_valid "$MOD" || cp -p "$SAFE" "$MOD"
		rm -f "$SAFE"
	fi
}
trap cleanup EXIT INT TERM

if ! mod_is_valid "$MOD"; then
	check_skip "stash recovery" "SKIP  no valid module installed at $MOD, so there is no state to arrange" "no valid module installed at $MOD"
	pgc_summary
fi
SAFE="$(mktemp /tmp/pgc-objstore-safe.XXXXXX)"
cp -p "$MOD" "$SAFE"

# EVERY SLICE STARTS FROM THE PRISTINE COPY (#1341), not from whatever the slice
# before it left. Chained, a presence-only guard that deleted the only copy in the
# second slice left nothing to move in the third, so the third slice reported its
# own premise rather than the defect it exists for, and which arm caught what
# depended on the order the slices were written in.
arrange_pristine() {
	rm -f "$MOD.probe" "$MOD.away"
	cp -p "$SAFE" "$MOD"
}

# ---- debris beside a good installation must not stop the suite ----------------
#
# The PG17 case, exactly: a stash left by an interrupted run, and a valid module
# reinstalled beside it by the next `make install`.
#
# THE DEBRIS IS A DIFFERENT MODULE, NOT A COPY (#1341). On PG17 the stash was a
# STALE build and the installed module a fresh one, which is why restoring the
# stash was the wrong advice. A byte-identical copy cannot show that: a guard with
# no debris branch at all, restoring every stash over the installed module, passed
# every check here, because restoring the copy changed nothing. Trailing bytes keep
# it a valid module to nm and to dlopen while making it a different file.
#
# The check this replaces, "and it did not stop at the start guard", counted the
# words 'interrupted mid-move' in the suite's output. That was the refusal's text
# before #393's fix and nothing has printed it since, so it read 0 whatever the
# guard did: with the guard turned back into a refusal, every other check in this
# slice went red and it stayed green. Whether the suite stopped is the exit status
# above it, so it is not replaced by another count of words.
arrange_pristine
check "premise: the installed module is valid before this slice arranges anything" \
	"$(mod_is_valid "$MOD" && echo yes || echo no)" "yes"

cp -p "$MOD" "$MOD.probe"
printf 'stale' >> "$MOD.probe"
check "premise: a debris stash really is in place for the run below" \
	"$([ -e "$MOD.probe" ] && echo yes || echo no)" "yes"
check "premise: and the debris is a valid module that differs from the installed one" \
	"$(mod_is_valid "$MOD.probe" && ! cmp -s "$MOD.probe" "$MOD" && echo yes || echo no)" "yes"

out="$(PGC_SKIP_BUILD=1 PGC_PORT="$(pgc_pick_port)" bash "$SUITE" "$PG_CONFIG" 2>&1)"
rc=$?

check_num "the suite runs to completion with debris beside a valid module" "$rc" "0"
check "and the installed module is still the one that was there, not the debris" \
	"$(cmp -s "$MOD" "$SAFE" && echo same || echo replaced)" "same"
check "the debris is gone afterwards" \
	"$([ -e "$MOD.probe" ] && echo present || echo absent)" "absent"
check "and the module left installed is still valid" \
	"$(mod_is_valid "$MOD" && echo yes || echo no)" "yes"

# ---- a stash that is the only surviving copy must be restored, not refused ----
#
# The interrupted state itself: the module moved aside and nothing put back. The
# suite must not proceed past this by treating the stash as debris -- there is no
# module to fall back on -- and it must not leave the box needing a human either.
#
# AT .probe, NOT .away (#1341). The guard walks both names, and the third slice
# below already parks the real module at .away. With both restore slices at .away,
# a guard that walked .away alone passed every check here, while the interrupt
# objstore_module.sh's own restore_module comment describes -- the real module at
# .probe and nothing at its path -- went unrecovered.
arrange_pristine
mv "$MOD" "$MOD.probe"
check "premise: nothing is installed at the module's path for this slice" \
	"$([ -e "$MOD" ] && echo present || echo absent)" "absent"
check "premise: and the stash beside it is the real module" \
	"$(mod_is_valid "$MOD.probe" && echo yes || echo no)" "yes"

out="$(PGC_SKIP_BUILD=1 PGC_PORT="$(pgc_pick_port)" bash "$SUITE" "$PG_CONFIG" 2>&1)"
rc=$?

check_num "the suite recovers the only surviving copy and runs to completion" "$rc" "0"
check "the module is installed again afterwards, and valid" \
	"$(mod_is_valid "$MOD" && echo yes || echo no)" "yes"
check "and the stash is not left behind for the next run to trip on" \
	"$([ -e "$MOD.probe" ] && echo present || echo absent)" "absent"

# ---- a stand-in installed over the real module is the destructive case --------
#
# An interrupt inside objstore_module's broken-module arm leaves this exact state:
# 19 bytes of text installed AS the module, with the real one at .away. A stash
# rule written on presence alone deletes .away here and destroys the installation,
# and it looks correct in every other arrangement. This is the arm that says the
# discriminator has to be validity.
arrange_pristine
mv "$MOD" "$MOD.away"
printf 'not a shared object' > "$MOD"
check_num "premise: a 19-byte stand-in really is installed as the module" \
	"$(stat -c %s "$MOD" 2>/dev/null)" "19"
check "premise: which is not a module" \
	"$(mod_is_valid "$MOD" && echo yes || echo no)" "no"
check "premise: and the real module is the thing parked at the stash" \
	"$(mod_is_valid "$MOD.away" && echo yes || echo no)" "yes"

out="$(PGC_SKIP_BUILD=1 PGC_PORT="$(pgc_pick_port)" bash "$SUITE" "$PG_CONFIG" 2>&1)"
rc=$?

check_num "the suite replaces the stand-in with the real module and completes" "$rc" "0"
check "the real module survived, rather than being deleted with the stash" \
	"$(mod_is_valid "$MOD" && echo yes || echo no)" "yes"
check "and the stash is cleared" \
	"$([ -e "$MOD.away" ] && echo present || echo absent)" "absent"

pgc_summary

# ---- a hand rebuild must leave the harness able to verify freshness (#1230) --
#
# `rebuild.sh` is the script this repo tells a developer to run for a clean
# rebuild, and it is careful: it cleans with the right PG_CONFIG, deletes the
# installed artifacts first so a failed install cannot leave the old library
# loadable, and verifies every undefined symbol resolves against the target
# postgres. What it did not do was write the harness's stamps, because it never
# sourced lib.sh.
#
# SO THE FRESHNESS GATE ACCUSED A CORRECT BINARY. Driven on the container:
#
#   A. harness-built, build skipped   -- source: 9cea7e4a926b matches the binary
#   B. edit a .c, then rebuild.sh     rc=0, .so built from the edited source
#   C. the same suite, build skipped  FATAL: the binary under test was not built
#                                     from this source (refusing to report checks
#                                     about code that is not installed)
#
# Both halves of that refusal are false: the binary WAS built from that source one
# step earlier, and the code IS installed. The reader is sent to the compiler and
# to a list of 66 files when the answer is that a stamp did not move. It cost
# three runs the last time it happened, two of them spent deleting object files.
#
# A guard that refuses correct work gets switched off, and the rule goes with it.
#
# THE TOOL KEEPS ITS OWN BUILD. Routing it through `pgc_build_and_install` would
# lose the parallel `-j` build, the compiler-warning gate that mirrors the matrix,
# and the error extraction from the build log -- none of which the harness builder
# has. Only the RECORD was missing, so only the record is added.

# PREMISE: the tool installs, which is what makes a missing record dangerous.
_hr_installs="$(sed 's/#.*//' "$PGC_TESTDIR/rebuild.sh" 2>/dev/null |
	grep -cE 'make[^|;&]*install')"
check_num "premise: the hand rebuild tool installs, so a stale record is possible" \
	"$(if [ "$_hr_installs" -ge 1 ]; then echo 1; else echo 0; fi)" "1"

# AND that one place knows how to write the record, so the arm below is not
# asking for a second copy of the stamp format.
check_num "premise: lib.sh has one recorder for what was installed" \
	"$(sed 's/#.*//' "$PGC_TESTDIR/lib.sh" | grep -c '^pgc_record_source_stamp()')" "1"

# COMMENTS STRIPPED (#1222): a comment naming the recorder must not satisfy this.
_hr_records="$(sed 's/#.*//' "$PGC_TESTDIR/rebuild.sh" 2>/dev/null |
	grep -cE 'pgc_record_source_stamp|pgc_build_and_install')"
check_num "the hand rebuild tool records what it installed rather than only naming it" \
	"$(if [ "$_hr_records" -ge 1 ]; then echo 1; else echo 0; fi)" "1"

# THE RECORDER REFUSES RATHER THAN RECORDING SOMETHING PLAUSIBLE (#1232 review).
# Called with nothing it used to write `./.pgc_source_stamp.0.nolibd41` into the
# CURRENT DIRECTORY -- keyed by the md5 of an empty pkglibdir, `d41` being the
# front of d41d8cd98f00, the md5 of nothing -- holding a fingerprint of the
# current directory rather than of any source. A believable record in the wrong
# place is worse than no record, because the freshness gate reads it.
check_num "the recorder refuses when it is not told what was installed" \
	"$(pgc_record_source_stamp >/dev/null 2>&1; echo $?)" "1"

# AND WROTE NOTHING WHILE REFUSING, checked from a directory of its own so a
# stray file cannot be confused with one already there.
_hr_cwd="$(mktemp -d)"
check_num "and writes no stamp into the directory it was called from" \
	"$(cd "$_hr_cwd" && pgc_record_source_stamp >/dev/null 2>&1; ls -A "$_hr_cwd" | wc -l)" "0"
rm -rf "$_hr_cwd"

# ---- driven, against a pg_config shim so nothing reaches a real prefix -------
#
# The arm above is a spelling check and would pass on a call that writes the
# wrong thing. This runs the tool for real and then asks the FRESHNESS GATE, which
# is the thing that was refusing.
#
# NOTHING HERE TOUCHES SHARED STATE. The shim answers --pkglibdir and --sharedir
# with a temporary directory and passes every other question through, so the
# install lands in the temp prefix; the tree is a copy; and the stamp path is
# keyed by the md5 of pkglibdir, so it cannot collide with a real one. While a
# matrix holds a major, an install into its prefix is a write to shared state that
# `harness_selftest` itself reports as a stale .so -- this part must not be the
# thing that causes it.
_hr_tmp="$(mktemp -d)"
mkdir -p "$_hr_tmp/prefix/lib" "$_hr_tmp/prefix/share/extension" "$_hr_tmp/tree"
{
	echo '#!/bin/bash'
	echo 'for a in "$@"; do'
	echo '	case "$a" in'
	printf '\t\t--pkglibdir) echo "%s/prefix/lib"; exit 0 ;;\n' "$_hr_tmp"
	printf '\t\t--sharedir)  echo "%s/prefix/share"; exit 0 ;;\n' "$_hr_tmp"
	echo '	esac'
	echo 'done'
	printf 'exec %s "$@"\n' "$PGC_SELFTEST_PG_CONFIG"
} > "$_hr_tmp/pg_config"
chmod +x "$_hr_tmp/pg_config"
tar cf - --exclude=.git -C "$PGC_TESTDIR/.." . 2>/dev/null | tar xf - -C "$_hr_tmp/tree" 2>/dev/null

# THE SHIM MUST ACTUALLY REDIRECT, or the arm below measures the live prefix and
# passes for the wrong reason -- and would have installed over it to do so.
check_text "premise: the shim redirects the install away from the real prefix" \
	"$(if [ "$("$_hr_tmp/pg_config" --pkglibdir)" = "$_hr_tmp/prefix/lib" ] &&
		[ "$("$_hr_tmp/pg_config" --bindir)" = "$("$PGC_SELFTEST_PG_CONFIG" --bindir)" ];
	then echo redirected; else echo passthrough; fi)" "redirected"

# AND THAT NO STAMP EXISTS YET for this prefix, so what the arm reads afterwards
# was written by the run and not copied in with the tree.
# AND THE LIVE PREFIX IS WATCHED WHILE THE RUN HAPPENS (@jdatcmd, review). The
# day the shim stops redirecting is the day this arm installs over the real .so
# and the run still looks clean.
#
# MTIME, NOT DIGEST. The tree here is a copy of the tree under test and the build
# is byte-reproducible, so an install that DID land on the live prefix would write
# the same bytes and leave the digest identical: a control that cannot fail. The
# mtime moves whether or not the content does, and that is the whole signal.
_hr_live_so="$("$PGC_SELFTEST_PG_CONFIG" --pkglibdir)/pgcolumnar.so"
_hr_live_before="$(stat -c %Y "$_hr_live_so" 2>/dev/null)"

_hr_stamp="$(pgc_source_stamp_path "$_hr_tmp/tree" "$_hr_tmp/pg_config")"
check_text "premise: no stamp for this prefix before the rebuild" \
	"$(if [ -e "$_hr_stamp" ]; then echo present; else echo absent; fi)" "absent"

# BOTH DECISIONS ARE FUNCTIONS so they can be driven from here rather than only
# by a box that happens to break (#1248). The branch that matters is the one a
# healthy machine never takes, which is exactly the branch that had never run
# until aarch64 took it.
_hr_dependents() {	# _hr_dependents RC -> run|skip
	[ "${1:-}" = 0 ] && echo run || echo skip
}
_hr_diagnose() {	# _hr_diagnose LOGFILE -> the tail, or WHY there is no tail
	# NEVER SILENT. The first version returned early on `[ -s ]`, so the
	# 2026-09-24 nightly -- which failed with an EMPTY rebuild.log on aarch64 --
	# printed no diagnosis at all. An absent log and an empty one are different
	# failures: one means the redirect never opened, the other means rebuild.sh
	# exited before its first echo. Naming which one costs a line and saves a
	# night.
	local _f="${1:-}"
	if [ ! -f "$_f" ]; then
		echo "      ---- rebuild.log is MISSING: the redirect never created it ----"
		return 0
	fi
	if [ ! -s "$_f" ]; then
		echo "      ---- rebuild.log is EMPTY (0 bytes): rebuild.sh exited before its first echo ----"
		return 0
	fi
	echo "      ---- rebuild.log, last 20 lines ----"
	tail -20 "$_f" | sed 's/^/      /'
}

"$PGC_TESTDIR/rebuild.sh" "$_hr_tmp/pg_config" "$_hr_tmp/tree" >"$_hr_tmp/rebuild.log" 2>&1
_hr_rc=$?
check_num "premise: the hand rebuild itself succeeded" "$_hr_rc" "0"

# ---- the premise must GATE, and the failure must SAY SOMETHING (#1248) ------
#
# The 2026-09-24 nightly failed here on aarch64 with `got [1] want [0]`, and that
# was the entire diagnosis: the rebuild's output is captured to a file this part
# deletes on the way out, so the cause died with the workdir. The three arms
# below the premise then ran anyway and reported PASS.
#
# They were not vacuous that night, because rebuild.sh had in fact written its
# stamps before failing -- it writes them at step 3b and the symbol check that
# rejected the build comes after. That is luck rather than design. Had the BUILD
# failed, the same three arms would have read an absent stamp and reported on a
# rebuild that never happened.
#
# So both decisions are exposed as functions and judged, the way part 190 judges
# pgc_build_needs_clean, rather than being written inline where nothing can drive
# the branch that only a broken box takes.
check_text "premise: the gating decision is exposed to be judged" \
	"$(type -t _hr_dependents)" "function"
check_text "premise: the diagnosis is exposed too" \
	"$(type -t _hr_diagnose)" "function"

# ---- and the SYMBOL EXEMPTION is exposed too, for the same reason (#1248) ----
#
# The 2026-09-25 nightly finally printed rebuild.sh's diagnosis, and the cause
# was the symbol check itself: `__stack_chk_guard`, reported as evidence of a
# mislinked major on a build that had just logged `build: OK (0 warnings)`.
#
# THE ARM DRIVES THE DECISION, NOT THE SYMBOL, and that is the whole point.
# `__stack_chk_guard` cannot be reached on x86_64: the canary lives at %fs:0x28
# and the name never appears, so a fix verified on this architecture is
# indistinguishable from a no-op. Measured on x86_64 -- our .so leaves
# `__stack_chk_fail` undefined and libc exports it, while `__stack_chk_guard` is
# exported by neither libc nor the loader and is never referenced.
#
# Literals run the same on every architecture. The end-to-end behaviour on
# aarch64 is NOT covered by these arms and the next nightly is what covers it.
check_text "premise: the symbol exemption is exposed to be judged" \
	"$(type -t pgc_symbol_is_toolchain)" "function"

check_text "the stack canary object is exempt, whatever the architecture calls it" \
	"$(pgc_symbol_is_toolchain __stack_chk_guard)" "yes"
check_text "and so are the ITM and gmon hooks this check always ignored" \
	"$(pgc_symbol_is_toolchain __gmon_start__)/$(pgc_symbol_is_toolchain _ITM_deregisterTMCloneTable)" \
	"yes/yes"

# THE ARM THAT STOPS THE EXEMPTION SWALLOWING ITS OWN SUBJECT. A predicate that
# answered `yes` to everything would satisfy the three above and disarm the
# check entirely, which is the failure this whole issue is an instance of.
check_text "a PostgreSQL symbol is NOT exempt, so the check still has a subject" \
	"$(pgc_symbol_is_toolchain ExecInitNode)" "no"
check_text "and neither is one of ours" \
	"$(pgc_symbol_is_toolchain PgColumnarIsColumnarRelation)" "no"

# The version suffix is stripped before the comparison, so the exemption has to
# see through it too -- `__cxa_finalize@GLIBC_2.2.5` is the form nm prints.
check_text "the exemption sees through an @GLIBC version suffix" \
	"$(pgc_symbol_is_toolchain '__cxa_finalize@GLIBC_2.2.5')" "yes"

check_text "a rebuild that succeeded runs the arms that read its stamp" \
	"$(_hr_dependents 0)" "run"
check_text "a rebuild that failed skips them rather than reading a stamp it did not write" \
	"$(_hr_dependents 1)" "skip"
check_text "and any other status skips too, rather than being read as success" \
	"$(_hr_dependents "")" "skip"

_hr_dtmp="$(mktemp -d)"
printf 'rebuild: UNRESOLVED SYMBOLS against PostgreSQL 18\n__aarch64_ldadd4_acq_rel\n' \
	> "$_hr_dtmp/full.log"
: > "$_hr_dtmp/empty.log"
check_num "a log with content produces a diagnosis to print" \
	"$([ -n "$(_hr_diagnose "$_hr_dtmp/full.log")" ] && echo 1 || echo 0)" "1"
check_num "and the diagnosis carries the failing line, not just a status" \
	"$(_hr_diagnose "$_hr_dtmp/full.log" | grep -c 'UNRESOLVED SYMBOLS')" "1"
# THESE ARMS USED TO DEMAND SILENCE, AND THE SILENCE WAS THE DEFECT (#1248).
# The 2026-09-24 nightly failed the premise on aarch64 with an EMPTY rebuild.log.
# `[ -s ]` returned early, so the whole diagnosis was nothing at all -- the exact
# outcome this part exists to prevent, reached through the arm that required it.
# A diagnosis that can be silent is not a diagnosis. When there is no tail to
# print it must say so, and say which of the two causes it found, because
# "rebuild.sh wrote no reason" and "the log was never created" are different
# failures and merging them costs the next reader the night.
check_num "an empty log still produces a diagnosis, because silence is the bug" \
	"$([ -n "$(_hr_diagnose "$_hr_dtmp/empty.log")" ] && echo 1 || echo 0)" "1"
check_num "and it names the log as empty rather than printing a bare banner" \
	"$(_hr_diagnose "$_hr_dtmp/empty.log" | grep -c 'rebuild.log is EMPTY')" "1"
check_num "a log that is not there also produces a diagnosis" \
	"$([ -n "$(_hr_diagnose "$_hr_dtmp/nope.log" 2>/dev/null)" ] && echo 1 || echo 0)" "1"
check_num "and it distinguishes missing from empty, so the two causes do not merge" \
	"$(_hr_diagnose "$_hr_dtmp/nope.log" 2>/dev/null | grep -c 'rebuild.log is MISSING')" "1"
rm -rf "$_hr_dtmp"

# AND THE REAL SCRIPT MUST LEAVE SOMETHING TO DIAGNOSE. A gate that prints a log
# is worth nothing if the log is empty on the path that matters, so this drives
# rebuild.sh's cheapest failure -- a pg_config that is not there -- and asserts it
# wrote a reason rather than only exiting non-zero.
_hr_fail_log="$(mktemp)"
"$PGC_TESTDIR/rebuild.sh" /nonexistent/pg_config "$_hr_tmp/tree" >"$_hr_fail_log" 2>&1
_hr_fail_rc=$?
check_num "premise: a rebuild with no pg_config fails" \
	"$([ "$_hr_fail_rc" != 0 ] && echo 1 || echo 0)" "1"
check_num "and it writes a reason, so there is something for the gate to print" \
	"$(grep -c '^rebuild: no such pg_config' "$_hr_fail_log")" "1"
rm -f "$_hr_fail_log"

if [ "$(_hr_dependents "$_hr_rc")" = run ]; then
	check_text "and the freshness gate then reads the tree as built from this source" \
		"$(pgc_freshness_verdict \
			"$(head -1 "$_hr_stamp" 2>/dev/null)" \
			"$(pgc_source_fingerprint "$_hr_tmp/tree")")" "fresh"

	# THE SAME READ AGAINST A FINGERPRINT THAT IS NOT THIS TREE'S, so the arm
	# above is known to distinguish rather than to answer "fresh" whatever it is
	# given.
	check_text "and it reads a different source as stale rather than fresh" \
		"$(pgc_freshness_verdict "$(head -1 "$_hr_stamp" 2>/dev/null)" deadbeefdead)" "stale"

	check_text "and the installed library of the running major was never touched" \
		"$(if [ "$(stat -c %Y "$_hr_live_so" 2>/dev/null)" = "$_hr_live_before" ];
		then echo untouched; else echo overwritten; fi)" "untouched"
else
	# THE ONLY DIAGNOSIS THERE IS. rebuild.sh's output goes to a file this part
	# deletes on the way out, so without this the run reports `got [1] want [0]`
	# and the cause leaves with the workdir -- which is how the aarch64 failure
	# arrived undiagnosable (#1248).
	_hr_diagnose "$_hr_tmp/rebuild.log"
	for _hr_arm in \
		"and the freshness gate then reads the tree as built from this source" \
		"and it reads a different source as stale rather than fresh" \
		"and the installed library of the running major was never touched"; do
		check_skip "$_hr_arm" \
			"SKIP  $_hr_arm (the hand rebuild failed, so there is no stamp to read)" \
			"the hand rebuild failed"
	done
fi

rm -rf "$_hr_tmp"

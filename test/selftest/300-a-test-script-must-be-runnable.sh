# ---- a test script must be runnable the way it is documented (#852, #856) ----
#
# Every document that tells a reader how to run a script spells it as a command:
# `test/temporal.sh /path/to/pg_config`, `PGC_RUN_UPGRADE=1
# test/run_all_versions.sh`, `bench/run_bench_fsst.sh /path/to/pg_config`. 103 of
# the 262 top-level scripts under test/ were mode 100644 at the base #853 landed
# on, so 30 of those documented invocations died with `Permission denied` before
# running a single statement.
#
# THE MATRIX NEVER SAW IT, AND THAT IS THE POINT. ci.yml and nightly.yml both
# call `bash test/run_all_versions.sh`, and the runner launches each suite as
# `bash "$builddir/test/${s}.sh"`. An interpreter named on the command line does
# not consult the execute bit, so a suite's own mode is invisible to every green
# run in this project's history. Those runs are honest. The gap is between the
# documentation and a shell, and only a check that reads the MODE can stand in it.
#
# THE RULE IS A BICONDITIONAL: A SHEBANG AND THE EXECUTE BIT GO TOGETHER.
#
#   A. A file that declares an interpreter must be executable. It has said it is
#      meant to be run, and a mode that forbids running it contradicts the file's
#      own first line. This is #852's defect.
#   B. A file that is executable must declare an interpreter. Otherwise the mode
#      promises something the file cannot deliver: what would `./foo.py` even run
#      under?
#   C. And a script a document names as a bare command must be executable,
#      whatever its first line says. This is the only rule anchored on prose, and
#      it is here to close the hole B leaves: delete a documented command's
#      shebang AND its bit and A and B both fall silent, because the file is then
#      internally consistent and still broken for the reader.
#
# A file with NEITHER a shebang nor the bit passes, and that is deliberate. It is
# a fragment meant to be sourced, and it is the only self-consistent way to say
# so. #856 is why this is spelled out: an earlier revision of this part flagged
# every file without a shebang, which left a sourced fragment no way to be
# correct -- dropping the shebang moved it from one red to another. The header
# then documented "drop its shebang and say why" as the way out, and that way out
# did not exist. bench/cb_guards.sh is the file that proved it: sourced by
# bench/run_clickbench.sh and test/bench_guards.sh, its header saying "Sourced,
# not executed" since the day it was written, and unfixable under the old rule.
#
# WHY THE POPULATION IS "EVERY DIRECTORY THAT HOLDS A DOCUMENTED ENTRY POINT".
# Today that is test/ and bench/. #852 was found in test/ and fixed there, and
# bench/ was left out -- while docs/benchmarks.md names five bench/ scripts as
# bare commands. All five are 100755 today, so all five work: correct by habit,
# with nothing checking it, which is exactly what test/ was before #852. A
# directory that documents an entry point earns the same guard as the one that
# already had it.
#
# AND THE BICONDITIONAL REMOVED THE NEED FOR EXEMPTIONS, WHICH IS THE REAL
# SIMPLIFICATION HERE. The old rule flagged every file without a shebang, so the
# two directories full of sourced fragments and data had to be pruned by path or
# they would have reddened the sweep wholesale. Under A and B a fragment with
# neither a shebang nor the bit is CORRECT, so nothing needs excusing:
#
#   test/selftest/   31 scripts, every one already ok -- no shebang, no bit
#   test/fixtures/   14 .sh/.py, one ok and thirteen that #856 gave the bit
#
# Measured, both directories, before the prune was removed. Nothing is excluded
# now, so nothing is concealed: the thirteen fixture host tools that the old
# exemption hid in a state this rule calls wrong are inside the population and
# the suite would redden if any of them lost its bit again.

_tsm_root="$(cd "$PGC_TESTDIR/.." && pwd)"

_tsm_has_shebang() {	# _tsm_has_shebang FILE
	# grep -c on a captured line, not a pipe into grep -q; see selftest 080.
	[ "$(head -1 "$1" 2>/dev/null | grep -c '^#!' || true)" != 0 ]
}

# ok      -- shebang and bit, or neither: internally consistent
# noexec  -- declares an interpreter and cannot be run                 (rule A)
# nodecl  -- can be run and declares no interpreter                    (rule B)
_tsm_verdict() {	# _tsm_verdict FILE -> ok | noexec | nodecl
	if _tsm_has_shebang "$1"; then
		[ -x "$1" ] && echo ok || echo noexec
	else
		[ -x "$1" ] && echo nodecl || echo ok
	fi
}

# ---- controls: all four corners of the biconditional ------------------------
#
# A sweep that has never fired is indistinguishable from a tree that is already
# clean. These pin every verdict before the sweep below is believed.

_tsm_c="$PGC_WORKDIR/tsm_ctl"; mkdir -p "$_tsm_c"
printf '#!/usr/bin/env bash\nexit 0\n' > "$_tsm_c/sh_noexec.sh"; chmod 644 "$_tsm_c/sh_noexec.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$_tsm_c/sh_exec.sh";   chmod 755 "$_tsm_c/sh_exec.sh"
printf 'import sys\n'                  > "$_tsm_c/bare_exec.py"; chmod 755 "$_tsm_c/bare_exec.py"
printf 'import sys\n'                  > "$_tsm_c/bare_noexec.py"; chmod 644 "$_tsm_c/bare_noexec.py"

check "control: an interpreter declared without the bit is caught" \
	"$(_tsm_verdict "$_tsm_c/sh_noexec.sh")" "noexec"
check "control: and the same file with the bit is not" \
	"$(_tsm_verdict "$_tsm_c/sh_exec.sh")" "ok"
check "control: the bit without an interpreter is caught too" \
	"$(_tsm_verdict "$_tsm_c/bare_exec.py")" "nodecl"
check "control: and a sourced fragment, with neither, is correct" \
	"$(_tsm_verdict "$_tsm_c/bare_noexec.py")" "ok"

# The bit has to survive the copy, or this reads a mode the repository does not
# have: run_all_versions.sh stages the tree with `cp -a` and the matrix runs
# these parts out of that staged copy.
cp -a "$_tsm_c/sh_exec.sh" "$_tsm_c/copied.sh"
check "control: cp -a preserves the execute bit, so a staged tree reads the same" \
	"$(_tsm_verdict "$_tsm_c/copied.sh")" "ok"

# ---- the population ---------------------------------------------------------
#
# Every .sh and .py under a directory that holds a documented entry point, at
# any depth, and nothing is excluded. There is no prune and no name list.

_tsm_dirs=("$_tsm_root/test" "$_tsm_root/bench")

_tsm_scripts=()
while IFS= read -r _tsm_f; do
	_tsm_scripts+=("$_tsm_f")
done < <(find "${_tsm_dirs[@]}" -type f \( -name '*.sh' -o -name '*.py' \) \
	-print 2>/dev/null | sort)

# ---- premises ---------------------------------------------------------------

check "premise: the sweep reads a population of scripts, not an empty find" \
	"$([ "${#_tsm_scripts[@]}" -ge 200 ] && echo enough || echo "${#_tsm_scripts[@]}")" "enough"

# The reverse of what this premise used to assert. Both directories are swept
# now, and saying so out loud is what stops the prune quietly coming back.
_tsm_parts="$(printf '%s\n' "${_tsm_scripts[@]}" | grep -c '/test/selftest/')"
check "premise: the sourced parts are inside the population, not pruned" \
	"$([ "$_tsm_parts" -ge 25 ] && echo yes || echo "no ($_tsm_parts under test/selftest/)")" "yes"

_tsm_tools="$(printf '%s\n' "${_tsm_scripts[@]}" | grep -c '/test/fixtures/')"
check "premise: and so are the fixture host tools" \
	"$([ "$_tsm_tools" -ge 10 ] && echo yes || echo "no ($_tsm_tools under test/fixtures/)")" "yes"

check "premise: a runnable script one level down is inside the population" \
	"$(printf '%s\n' "${_tsm_scripts[@]}" | grep -c '/pbt/run\.sh$')" "1"

# #856's whole point: bench/ is swept now, and the premise says so by naming a
# file that only a bench/ sweep can reach.
check "premise: and bench/ is in the population" \
	"$(printf '%s\n' "${_tsm_scripts[@]}" | grep -c '/bench/run_bench\.sh$')" "1"

# ---- the sweep: rules A and B ----------------------------------------------

_tsm_noexec=""; _tsm_noexec_n=0
_tsm_nodecl=""; _tsm_nodecl_n=0
for _tsm_f in "${_tsm_scripts[@]}"; do
	case "$(_tsm_verdict "$_tsm_f")" in
		noexec)
			_tsm_noexec_n=$((_tsm_noexec_n + 1))
			[ "$_tsm_noexec_n" -le 5 ] && _tsm_noexec="$_tsm_noexec ${_tsm_f#"$_tsm_root"/}"
			;;
		nodecl)
			_tsm_nodecl_n=$((_tsm_nodecl_n + 1))
			[ "$_tsm_nodecl_n" -le 5 ] && _tsm_nodecl="$_tsm_nodecl ${_tsm_f#"$_tsm_root"/}"
			;;
	esac
done

# The count leads the message and at most five names follow it: a real failure is
# one or two files and wants naming, while the 103 this was written against would
# otherwise print an unreadable line.
_tsm_fmt() {	# _tsm_fmt N NAMES
	[ "$1" -eq 0 ] && { echo "[]"; return; }
	echo "[$1:$2]"
}

check "every script that declares an interpreter is executable" \
	"$(_tsm_fmt "$_tsm_noexec_n" "$_tsm_noexec")" "[]"

check "and every executable script declares one" \
	"$(_tsm_fmt "$_tsm_nodecl_n" "$_tsm_nodecl")" "[]"

# ---- rule C: what the documents actually tell a reader to type --------------
#
# Anchored on prose, and that is a deliberate exception rather than a lapse. A
# documentation-derived list is the wrong POPULATION -- delete a line and the
# sweep silently narrows -- which is why A and B are anchored on the files. As an
# additional check it cannot create a false green for either of them, and it is
# the only one that states #852's defect directly: the command in the manual
# does not run.
#
# Over-inclusive on purpose, and more so than an earlier draft of this comment
# claimed. EVERY `test/x.sh` or `bench/x.sh` in a document counts -- prose or
# fenced, and including one an interpreter already precedes. The leading
# character class admits a space, so `bash test/smoke.sh` in docs/testing.md is
# counted like any other, which is the one occurrence of that shape today. That
# is fine and it is deliberate: a reader who sees a path may well type it
# without the interpreter, and the cost of a false positive is one execute bit
# on a file that is an entry point anyway. The draft said the extraction skipped
# interpreter-prefixed paths; it never did, and a comment describing a filter
# the code does not implement is the defect this file exists to prevent.
#
# TWO THINGS ARE ASSERTED, NOT ONE. A document may name a script that does not
# exist, and a reader who types it gets "No such file or directory" rather than
# "Permission denied" -- #852's defect wearing a different error message, and C
# is the check whose entire justification is that the command in the manual does
# not run. So a named path must EXIST as well as be executable. An earlier draft
# skipped a missing path with `[ -f ] || continue`, commented "a document may
# name a path that moved", and that skip was a silent hole rather than a
# decision. All 83 named paths resolve today, so this is a guard rather than a
# fix.
#
# WHAT IS STILL ACCEPTED, with the number rather than a shrug: deleting one line
# that names one script narrows C by one and stays green, because the count
# premise below has 43 of slack over its floor. C is additive over A and B, so a
# one-script narrowing weakens C alone and cannot make either sweep pass
# falsely. The narrowing that WOULD matter -- a whole document going, taking a
# directory's coverage with it -- is what the per-directory premise catches.

_tsm_docs=()
while IFS= read -r _tsm_d; do
	_tsm_docs+=("$_tsm_d")
done < <(find "$_tsm_root/docs" -maxdepth 1 -name '*.md' -print 2>/dev/null | sort)
[ -f "$_tsm_root/README.md" ] && _tsm_docs+=("$_tsm_root/README.md")

_tsm_named="$(grep -hoE '(^|[^/[:alnum:]_.-])(test|bench)/[A-Za-z0-9_./-]+\.(sh|py)' \
		"${_tsm_docs[@]}" 2>/dev/null \
	| grep -oE '(test|bench)/[A-Za-z0-9_./-]+\.(sh|py)' | sort -u)"

check_num "premise: the documents name a population of commands, not none" \
	"$([ "$(printf '%s\n' "$_tsm_named" | grep -c .)" -ge 40 ] && echo 1 || echo 0)" "1"

# A TOTAL IS THE WRONG PREMISE FOR THIS CHECK, and a floor over it hides the
# narrowing that matters. Measured on this tree: the documents name 83 scripts,
# 76 under test/ and 7 under bench/. Delete docs/benchmarks.md and the bench/
# half goes to ZERO while the total is still 75 -- comfortably over any floor,
# so the premise stays green and C silently stops covering the directory #856
# added it for. That is #853's own objection to a prose-derived population,
# arriving where it actually bites.
#
# So the premise is per-directory, and it is the population's definition turned
# into an assertion: a directory is swept BECAUSE it holds documented entry
# points, so the documents must still name one. A directory added to the sweep
# with nothing documented in it reddens here and says so.
_tsm_uncovered=""
for _tsm_d in "${_tsm_dirs[@]}"; do
	_tsm_b="$(basename "$_tsm_d")"
	# grep -c on a here-string, not `printf | grep -q`; see selftest 080 and the
	# note in 350. The piped form reports a present name as absent under load.
	[ "$(grep -c "^$_tsm_b/" <<<"$_tsm_named" || true)" -ne 0 ] \
		|| _tsm_uncovered="$_tsm_uncovered $_tsm_b"
done
check "premise: and they name at least one command in every swept directory" \
	"[${_tsm_uncovered}]" "[]"

_tsm_docbad=""; _tsm_docbad_n=0
_tsm_docgone=""; _tsm_docgone_n=0
while IFS= read -r _tsm_s; do
	[ -n "$_tsm_s" ] || continue
	if [ ! -e "$_tsm_root/$_tsm_s" ]; then
		_tsm_docgone_n=$((_tsm_docgone_n + 1))
		[ "$_tsm_docgone_n" -le 5 ] && _tsm_docgone="$_tsm_docgone $_tsm_s"
	elif [ ! -x "$_tsm_root/$_tsm_s" ]; then
		_tsm_docbad_n=$((_tsm_docbad_n + 1))
		[ "$_tsm_docbad_n" -le 5 ] && _tsm_docbad="$_tsm_docbad $_tsm_s"
	fi
done <<< "$_tsm_named"

check "and every script a document names exists" \
	"$(_tsm_fmt "$_tsm_docgone_n" "$_tsm_docgone")" "[]"

check "and every script a document names is executable" \
	"$(_tsm_fmt "$_tsm_docbad_n" "$_tsm_docbad")" "[]"

unset _tsm_root _tsm_scripts _tsm_dirs _tsm_docs _tsm_named _tsm_f _tsm_d _tsm_s
unset _tsm_uncovered _tsm_b
unset _tsm_noexec _tsm_noexec_n _tsm_nodecl _tsm_nodecl_n _tsm_docbad _tsm_docbad_n _tsm_c
unset _tsm_docgone _tsm_docgone_n
unset -f _tsm_has_shebang _tsm_verdict _tsm_fmt

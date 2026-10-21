# ---- an unrunnable record must name the check it stands in for --------------
#
# #1040. `check_unrunnable NAME REASON DETAIL` gives one check the honesty
# `pgc_skip` gives a whole suite: the check did not run, and the reader is told
# WHICH. That only works if the record carries the name the check uses when it
# DOES run. Where the two spellings differ the property has TWO ledger keys and
# which one appears depends on runtime state -- in `hilbert_locality` it depended
# on whether that box's two partitions came out different that day, so the key
# was a function of the data.
#
# THIS IS PART 470's PROPERTY ONE HELPER OVER. 470 asks whether a skip LOOP still
# names the arms its sibling branch emits. This asks whether an unrunnable record
# names a check the same file asserts anywhere.
#
# WHY A GUARD AND NOT JUST THE RENAME. The convention is already near-universal
# and that is exactly what makes a lapse invisible: 23 of 25 call sites carry the
# runnable name (`hilbert_cluster` 9 of 9, `projection_rewrite` 11 of 11), so a
# reader skimming a new refusal branch sees nothing unusual in a shorter name.
# Both halves of a two-branch site are rarely read together.
#
# `check_skip` IS NOT SWEPT and that is a finding rather than an omission. A
# skipped arm has no runnable counterpart by construction -- the name IS the arm
# -- so 21 of its 23 call sites have no twin and always will. Sweeping it would
# report 21 mismatches that are all correct code.
#
# THE POSITIVE CONTROL BELOW IS THE POINT OF THE PART. A sweep that matched
# nothing reports the same zero as a corpus in agreement, and this guard's steady
# state is zero, so nothing else would ever distinguish the two. The fixture arms
# drive the real tool over a file whose answer is known.

_un_py="$TESTDIR/../.github/scripts/unrunnable-arm-names.py"

check "premise: the sweep tool is present" \
	"$([ -r "$_un_py" ] && echo yes || echo no)" "yes"

_un_out="$(python3 "$_un_py" "$TESTDIR" --show-emitters 2>&1)" || _un_out="TOOL FAILED: $_un_out"

_un_sites=$(printf '%s\n' "$_un_out"    | sed -n 's/^sites \([0-9]*\)$/\1/p')
_un_dyn=$(printf '%s\n' "$_un_out"      | sed -n 's/^dynamic \([0-9]*\)$/\1/p')
_un_cmp=$(printf '%s\n' "$_un_out"      | sed -n 's/^compared \([0-9]*\)$/\1/p')
_un_ref=$(printf '%s\n' "$_un_out"      | sed -n 's/^refusal //p')
_un_twins=$(printf '%s\n' "$_un_out"    | sed -n 's/^twins //p')
_un_bad="$(printf '%s\n' "$_un_out"     | sed -n 's/^MISMATCH //p' | tr '\n' ' ' | sed 's/ $//')"

echo "-- unrunnable names: $_un_sites sites, $_un_cmp compared, $_un_dyn dynamic"

# THE POPULATION IS THE PREMISE, twice. A sweep that found nothing and a corpus in
# agreement both print zero mismatches, and so does a sweep that found sites and
# compared none of them.
check "premise: the sweep found unrunnable sites at all" \
	"$([ "${_un_sites:-0}" -ge 20 ] && echo yes || echo "only ${_un_sites:-none}")" "yes"
check "premise: and it compared them rather than writing them all off as dynamic" \
	"$([ "${_un_cmp:-0}" -ge 20 ] && echo yes || echo "only ${_un_cmp:-none}")" "yes"

# THREE PINS, each on a value that was WRONG in a draft of the tool, so each is a
# regression test rather than a restatement of the code.
#
# The refusal set decides what gets swept. A new refusal emitter must be looked at
# rather than silently sweep or silently not sweep. `arms_unrunnable` was here,
# wrapping `check_unrunnable` and reading its names from a list, until hilbert_curve's
# port (#1341) wrote it out as loops at its call sites; `check_unrunnable` is now the
# only one, and a new list-taking wrapper reddens this arm by name.
check_text "the refusal emitters are the ones the tree defines" \
	"$_un_ref" "check_unrunnable:1"

# pgc_skip records `pgc_record FAIL "$2"`: its first quoted argument is the
# CAPABILITY, not the name. Reading argument one takes `arrow` where the check is
# called `arrow support is present` -- the bash-side mirror of the name-position
# defect #1036 and #1038 closed on the python side, across 22 call sites.
check "pgc_skip's name is read from argument TWO, not argument one" \
	"$(printf '%s\n' "$_un_twins" | tr ' ' '\n' | grep -c '^pgc_skip:2$')" "1"

# pgc_pass was missing from two hand-written runnable lists, and its absence made
# projection_rewrite.sh report a false orphan: the twin of its unrunnable name is
# recorded by pgc_pass and not by any check_* helper.
check "pgc_pass and pgc_fail are twins, because a check can be recorded through them" \
	"$(printf '%s\n' "$_un_twins" | tr ' ' '\n' | grep -c '^pgc_\(pass\|fail\):1$')" "2"

# ---- the positive control ---------------------------------------------------
#
# Driven over a fixture whose answer is known, because this guard's steady state
# is zero mismatches and a broken sweep reports zero too.
_un_fx="$(mktemp -d)"
cp "$TESTDIR/lib.sh" "$_un_fx/lib.sh"

cat > "$_un_fx/offender.sh" <<'PGC_FX_BAD'
check_num "the property, said one way" "$a" "$b"
check_unrunnable "the property, said another way" UNMET_PRECONDITION "no fixture"
PGC_FX_BAD

cat > "$_un_fx/control.sh" <<'PGC_FX_GOOD'
check_num "the property, said one way" "$a" "$b"
check_unrunnable "the property, said one way" UNMET_PRECONDITION "no fixture"
PGC_FX_GOOD

# A SECOND FIXTURE DIRECTORY holding only the agreeing file, so the clean exit code
# is measured on a corpus that HAS a refusal site rather than on one with nothing to
# find -- those two report the same 0 and only one of them is evidence.
_un_fxok="$(mktemp -d)"
cp "$TESTDIR/lib.sh" "$_un_fxok/lib.sh"
cp "$_un_fx/control.sh" "$_un_fxok/control.sh"

# NO PIPE ON EITHER RUN. `$?` after a pipeline is the LAST stage's, which is how the
# missing exit code first read as present (@jdatcmd).
_un_fxout="$(python3 "$_un_py" "$_un_fx" 2>&1)"; _un_fxrc=$?
_un_okout="$(python3 "$_un_py" "$_un_fxok" 2>&1)"; _un_okrc=$?

check "the sweep REPORTS a name the file records only in its refusal branch" \
	"$(printf '%s\n' "$_un_fxout" | grep -c '^MISMATCH offender.sh:2 the property, said another way$')" "1"
check "and it stays silent on the same file with the names in agreement" \
	"$(printf '%s\n' "$_un_fxout" | grep -c '^MISMATCH control.sh')" "0"

# THE EXIT CODE IS THE VERDICT, pinned in both directions. It was a flat 0 in the
# first version of the tool: two MISMATCH lines printed and success reported. This
# part gates on the parsed output and so was never fooled, which is exactly why the
# hazard needs its own arm -- the next caller is the one that trusts `$?`.
check "the sweep EXITS non-zero when it reports a mismatch" \
	"$([ "$_un_fxrc" -ne 0 ] && echo yes || echo "no, rc=$_un_fxrc")" "yes"
check "and exits zero on a corpus that has refusal sites and no mismatch" \
	"$_un_okrc" "0"
check "premise: that clean run had a refusal site to be silent ABOUT" \
	"$(printf '%s\n' "$_un_okout" | sed -n 's/^compared \([0-9]*\)$/\1/p')" "1"

rm -rf "$_un_fx" "$_un_fxok"

# ---- the tree ---------------------------------------------------------------

check "every unrunnable record names a check its own suite asserts (#1040)" \
	"$_un_bad" ""

unset _un_py _un_out _un_sites _un_dyn _un_cmp _un_ref _un_twins _un_bad
unset _un_fx _un_fxok _un_fxout _un_okout _un_fxrc _un_okrc

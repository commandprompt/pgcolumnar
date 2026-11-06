# A preflight that built nothing must not report PASSED.
#
# test/build_all_versions.sh answers "does this compile on every major". Its
# verdict reads only `failed`, and only a build that RUNS and FAILS sets it. A
# pg_config that is not executable takes the `continue` above that, so a run
# where every major was skipped reaches the end with failed=0 and prints
# PASSED, exit 0, having invoked no compiler at all.
#
# FOUND THE EXPENSIVE WAY, 2026-08-28 (#809). The defaults are /usr/local/pg15,
# pg16, pg17, pgsql and pg19. The audit container has none of them -- every
# assert build there is a-suffixed, pg15a through pg19a -- so the no-argument
# form can never build anything on that machine. It was recorded as a green
# five-major preflight for a pull request and caught only by reading the body
# above the verdict:
#
#   SKIP  /usr/local/pg15/bin/pg_config      (not executable)     [x5]
#   build_all_versions.sh: PASSED
#
# WHY A COUNT RATHER THAN A STRICTER DEFAULT LIST. Hard-coding this box's paths
# would move the problem to the next machine. The verdict is what is wrong: it
# collapses "built five" and "built none" into one word. Its sibling
# run_all_versions.sh already prints `versions run: N of M configured`, and that
# line is the whole fix -- a reader sees the denominator, and zero is refused.
#
# SCOPE. This pins the all-skip case only. Whether a PARTIAL run (three of five
# present) should fail or warn is a judgement about how people run this, and is
# deliberately not decided here.

_bav_src="$PGC_TESTDIR/build_all_versions.sh"

# ---- and it must leave no object tree behind, on a box with no pg_config -----
#
# #1219. The script's LAST act is a clean, and its comment says why: "Leave no
# object tree behind from whichever major happened to be last: the next build
# against a different major would link objects compiled for this one." It was
# written as
#
#     make -C "$SRCDIR" clean >/dev/null 2>&1 || true
#
# with no PG_CONFIG and its failure swallowed. PGXS resolves `pg_config` from
# PATH, so on a box with no packaged one this make fails, the `|| true` eats it,
# and the tree keeps the LAST major's objects while the script still prints
# PASSED. Measured, same tree, pg18a then pg19a:
#
#     normal PATH              built 2 of 2  PASSED   objects left:  0
#     PATH with no pg_config   built 2 of 2  PASSED   objects left: 36
#
# The consequence is bounded -- #1221's DWARF provenance catches the foreign
# objects on the next suite and cleans them -- so this costs a rebuild rather
# than a wrong install. What it does not cost is nothing, and the comment
# promises something it does not always do.
#
# THE SWEEP DOES NOT DEPEND ON pg_config AT ALL, and that is the fix rather than
# passing one: objects live in src/ AND objstore/, a `make clean` needs PGXS
# loaded to do anything, and a clean whose failure is swallowed cannot be
# distinguished from one that worked. So it removes by find and then VERIFIES,
# and the verdict is a value this part can judge.
eval "$(sed -n '/^pgc_bav_tree_has_objects()/,/^}/p' "$PGC_TESTDIR/build_all_versions.sh")"
eval "$(sed -n '/^pgc_bav_clean_tree()/,/^}/p' "$PGC_TESTDIR/build_all_versions.sh")"
check_text "premise: the tree cleaner is exposed to be judged" \
	"$(type -t pgc_bav_clean_tree)" "function"
check_text "premise: and the leftover detector it reads is exposed too" \
	"$(type -t pgc_bav_tree_has_objects)" "function"

_bav_ct="$(mktemp -d)"
mkdir -p "$_bav_ct/src" "$_bav_ct/objstore"
: > "$_bav_ct/src/columnar.o"
: > "$_bav_ct/src/columnar.bc"
: > "$_bav_ct/objstore/columnar_objstore_module.o"
: > "$_bav_ct/pgcolumnar.so"
: > "$_bav_ct/src/keep.c"

# THE FIXTURE MUST ACTUALLY HOLD OBJECTS, or every arm below passes by measuring
# an empty directory.
check_num "premise: the fixture holds objects in both source directories" \
	"$(find "$_bav_ct" \( -name '*.o' -o -name '*.bc' -o -name '*.so' \) | wc -l)" "4"

# THE DETECTOR, JUDGED BEFORE ANYTHING IS SWEPT. Folded inside the cleaner it
# was unreachable -- a cleaner that always answered `clean` reddened none of
# these arms, measured. Driven directly it has a killer.
check_text "a tree holding objects is detected as holding them" \
	"$(pgc_bav_tree_has_objects "$_bav_ct")" "yes"
check_text "and a directory that is not there holds none" \
	"$(pgc_bav_tree_has_objects "$_bav_ct/nope")" "no"

check_text "a tree full of objects is reported clean after the sweep" \
	"$(pgc_bav_clean_tree "$_bav_ct")" "clean"
check_num "and the objects are gone, including the ones outside src/" \
	"$(find "$_bav_ct" \( -name '*.o' -o -name '*.bc' -o -name '*.so' \) | wc -l)" "0"
check_num "and it left the source alone" \
	"$([ -f "$_bav_ct/src/keep.c" ] && echo 1 || echo 0)" "1"
check_text "and the detector now says the tree holds none" \
	"$(pgc_bav_tree_has_objects "$_bav_ct")" "no"

# AND THE CLEANER MUST REPORT WHAT THE DETECTOR TELLS IT, which needs the
# detector STUBBED rather than a fixture. A sweep that fails is what makes the
# cleaner's verdict load-bearing, and it cannot be staged here: the suite runs as
# root, so chmod does not stop an unlink, and anything `find -type f -delete` can
# remove the `-type f` re-check cannot see either. Measured before this arm
# existed -- a cleaner hard-wired to `echo clean` passed every other arm in this
# part. Stubbing the one function it consults is what gives that mutation a
# killer.
pgc_bav_tree_has_objects() { echo yes; }
check_text "the cleaner says dirty when the detector reports objects left behind" \
	"$(pgc_bav_clean_tree "$_bav_ct")" "dirty"
pgc_bav_tree_has_objects() { echo no; }
check_text "and clean when it reports none" \
	"$(pgc_bav_clean_tree "$_bav_ct")" "clean"

# THE STUB IS PUT BACK, and that is asserted rather than assumed: an unrestored
# stub makes every later arm measure a function this part wrote.
eval "$(sed -n '/^pgc_bav_tree_has_objects()/,/^}/p' "$PGC_TESTDIR/build_all_versions.sh")"
: > "$_bav_ct/src/restored.o"
check_text "the real detector is back, and sees a real object" \
	"$(pgc_bav_tree_has_objects "$_bav_ct")" "yes"
rm -f "$_bav_ct/src/restored.o"

# THE #1219 CASE ITSELF: no pg_config is given and none need be.
check_text "a tree with nothing to remove is clean, not an error" \
	"$(pgc_bav_clean_tree "$_bav_ct")" "clean"
check_text "and a directory that is not there is dirty rather than silently clean" \
	"$(pgc_bav_clean_tree "$_bav_ct/nope")" "dirty"
check_text "and no argument at all is dirty rather than sweeping the cwd" \
	"$(pgc_bav_clean_tree)" "dirty"
rm -rf "$_bav_ct"

# AND THE SCRIPT MUST ASK IT, not merely define it. A correct cleaner that
# nothing calls leaves the defect exactly where it was, and the arms above would
# all still pass. Comments are stripped first: this file's own prose quotes the
# call, and part 190 already paid for that lesson once.
_bav_code="$(sed 's/#.*//' "$PGC_TESTDIR/build_all_versions.sh")"
check_num "the script asks the cleaner rather than merely defining it" \
	"$(printf '%s\n' "$_bav_code" | grep -c 'pgc_bav_clean_tree "')" "1"
check_num "and the old swallow-everything form is gone" \
	"$(printf '%s\n' "$_bav_code" | grep -cE 'make -C "\$SRCDIR" clean >/dev/null 2>&1 \|\| true')" "0"

# Run a COPY, in a scratch tree, because the real script ends with
# `make -C "$SRCDIR" clean` and SRCDIR is derived from the script's own
# location. Run in place, this selftest would wipe the object tree of the very
# build the surrounding suite is testing. In the scratch tree that make has no
# Makefile to find and fails into the script's own `|| true`.
_bav_dir="$(mktemp -d)"
mkdir -p "$_bav_dir/test"
cp "$_bav_src" "$_bav_dir/test/build_all_versions.sh"

# Three pg_config paths that certainly do not exist, so every major skips.
_bav_out="$(bash "$_bav_dir/test/build_all_versions.sh" \
	/nonexistent-a/bin/pg_config /nonexistent-b/bin/pg_config \
	/nonexistent-c/bin/pg_config 2>&1)"
_bav_rc=$?
rm -rf "$_bav_dir"

# PREMISE. The arm has to actually be the all-skip case. If a path above ever
# existed, or the skip line were reworded, every assertion below would be
# testing a run that did something, and would pass or fail for the wrong reason.
check "premise: the probe run skipped every major" \
	"$(printf '%s\n' "$_bav_out" | grep -c 'SKIP')" "3"
check "premise: and it built none of them" \
	"$(printf '%s\n' "$_bav_out" | grep -cE '^[[:space:]]*OK')" "0"

# The three things a verdict on zero builds must do.
check "a preflight that built nothing says how many it built" \
	"$(printf '%s\n' "$_bav_out" | grep -cE 'built 0 of 3')" "1"
check "a preflight that built nothing does not report PASSED" \
	"$(printf '%s\n' "$_bav_out" | grep -c 'build_all_versions.sh: PASSED')" "0"
check "a preflight that built nothing exits non-zero" \
	"$([ "$_bav_rc" -ne 0 ] && echo nonzero || echo "zero")" "nonzero"

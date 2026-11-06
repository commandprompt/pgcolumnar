# ---- deciding "is this a checkout" must ask git, not stat .git (#1224) -------
#
# A linked worktree's `.git` is a regular FILE containing `gitdir: <path>`, not a
# directory. So does a tree staged with the one-line gitfile devloop.sh writes
# into its build dir, and so does an agent's copy staged across a container
# boundary. In all three git answers perfectly and `[ -d "$SRCDIR/.git" ]` is
# false.
#
# extension_upgrade.sh decided that way in two places and skipped its default
# path in every one of those environments -- including "the audit container,
# worktrees of a clone", which selftest 310 names as one of the five
# environments this harness runs in. A supported environment could not run that
# suite.
#
# THE TREE ALREADY HAD THE RIGHT TEST, TWO DIRECTORIES AWAY. devloop.sh uses
# `rev-parse --absolute-git-dir` and says why: "SRC may itself be a linked
# worktree, where .git is a FILE and that path is not a git directory."
# docs_style.sh and native_upgrade_converge.sh use rev-parse too. The defect was
# one file against three, not a missing idiom.
#
# THIS ARM IS A CLASS GUARD, NOT A LIST. It asks whether any suite decides the
# question by stat-ing `.git`, which is derived from the text rather than
# enumerated -- so a fourth file adopting the wrong form is caught without
# anyone maintaining a roster.

# PREMISE FIRST: the sweep must be able to match something. A pattern that
# cannot match reports zero, and zero is exactly the answer this arm wants --
# which is how a guard for wrong patterns ships with a wrong pattern.
_ck_mentions="$(sed 's/#.*//' "$PGC_TESTDIR"/*.sh 2>/dev/null | grep -c '\.git')"
check_num "premise: the sweep sees .git mentioned in the suites at all" \
	"$(if [ "$_ck_mentions" -ge 1 ]; then echo 1; else echo 0; fi)" "1"

# AND that the correct idiom exists, so arm two is not asserting an empty world.
_ck_right="$(grep -lE 'rev-parse --(git-dir|absolute-git-dir|is-inside-work-tree)' \
	"$PGC_TESTDIR"/*.sh 2>/dev/null | wc -l)"
check_num "premise: some suite already asks git whether it can answer" \
	"$(if [ "$_ck_right" -ge 1 ]; then echo 1; else echo 0; fi)" "1"

# THE PATTERN SHIPPED IN #1224 MISSED THREE OF EIGHT FORMS (#1227), all for one
# reason: it allowed the optional quote only BEFORE the variable, so a quote
# closing before the slash -- `[ -d "$SRCDIR"/.git ]` -- did not match. Measured
# by @OffgridwithJD and reproduced here, 5 of 10 caught. It also missed a nested
# path, `"$WORK/$name/.git"`.
#
# A guard that under-reports is worse than none: it answers the question with a
# number that looks like coverage. The pattern now spans from the test operator
# to `.git` and requires a non-word character after it, so `.gitignore`,
# `.gitattributes` and `.github` cannot satisfy it.
_ck_pat='(\[|test)[[:space:]]+(!?[[:space:]]*)-d[[:space:]]+[^]]*\.git([^A-Za-z0-9_-]|$)'

# AND IT READS COMMANDS, NOT LINES (@OffgridwithJD, review). bash continues a
# command after a trailing backslash, so
#
#     if [ -d \
#         "$SRCDIR/.git" ]; then
#
# is ONE command spelled over two lines, and a line-at-a-time sweep sees neither
# half. Live count today is zero, so this is latent like the false positives
# were; it is closed for the same reason, and because under-reporting is the
# defect this whole part is about. Parts 260 and 460 already fold with this sed,
# so it is the house form rather than a new idea.
#
# A BACKSLASH FOLD CLOSES THE CLASS HERE, not merely the common case, and that
# is measured rather than assumed. bash also continues after a trailing `&&`,
# `||` or `|` with no backslash, and every sweep in this tree folds on `\` only,
# which has cost two sessions an else-less count before now. It cannot hide THIS
# pattern, because those operators join COMMANDS and not a command's words:
#
#     [ -d                       `[: missing ']'` at runtime. Broken code,
#       "/tmp/.git" ]            not a continuation; nothing to find.
#     [ -d "/tmp" ] &&           1 unfolded. The test is whole on its own line.
#       [ -d "/tmp/.git" ]
#     [ -d \                     0 unfolded, 1 folded.
#       "/tmp/.git" ]
#     if [ -d "$(dirname \       0 unfolded, 1 folded.
#        "/tmp/x")/.git" ]
#
# Driven, all four parsed and run. Splitting `[`'s operands needs a backslash,
# so for a pattern anchored on `[` or `test` plus `-d` this fold is complete.
# Measured by @OffgridwithJD and reproduced here.
#
# COMMENTS FIRST, THEN THE FOLD. The other order joins a comment to the code
# line below it and strips both, which loses real code rather than gaining it.
_ck_fold() {	# stdin -> comments removed, continued lines joined
	sed 's/#.*//' | sed -e :a -e '/\\$/N; s/\\\n//; ta'
}
_ck_stat="$(cat "$PGC_TESTDIR"/*.sh "$PGC_TESTDIR"/selftest/*.sh 2>/dev/null |
	_ck_fold | grep -cE "$_ck_pat")"
check_num "no suite decides it is a checkout by stat-ing .git as a directory" \
	"$_ck_stat" "0"

# THE BEHAVIOUR THE ARM ABOVE IS ABOUT, driven rather than described, so the
# rule is not just a spelling preference. A gitfile is what every one of the
# three environments produces.
# STATED WITH stat, NOT PROBED WITH THE FORBIDDEN FORM. The first version of
# this arm demonstrated the point by writing `[ -d "$_ck_dir/.git" ]` -- and the
# sweep above then counted its own demonstration and stayed red after both real
# sites were fixed. A guard whose own text satisfies its pattern is the defect
# it exists to catch, one level up.
# THESE TWO ARE PREMISES, NOT ARMS (#1236). They assert what `printf >` just
# made, so no change to this repository can redden them: that is POSIX, not
# pgcolumnar. They stay because they make the gitfile concrete beside the sweep
# that depends on it, and the sweep is the arm. Named as arms, they were counted
# as coverage they cannot give.
_ck_dir="$(mktemp -d)"
printf 'gitdir: %s\n' "$_ck_dir/nowhere" > "$_ck_dir/.git"
check_text "premise: the gitfile fixture is a regular file, not a directory" \
	"$(stat -c %F "$_ck_dir/.git" 2>/dev/null)" "regular file"
check_text "premise: the gitfile fixture exists, so a reader must not skip it" \
	"$(if test -e "$_ck_dir/.git"; then echo present; else echo absent; fi)" "present"
rm -rf "$_ck_dir"

# THE PATTERN MUST MATCH WHAT IT CLAIMS TO, driven over every spelling either of
# us has written or found. Assembled from a fragment rather than written out,
# because the sweep above reads THIS FILE: spelling the forms literally would
# make the guard count its own test data, which is the defect it exists to catch
# and which the first version of this part committed.
_ck_op='-d'
_ck_forms="$(printf '%s\n' \
	"if [ $_ck_op \"\$SRCDIR/.git\" ]; then" \
	"if [ ! $_ck_op \"\$SRCDIR/.git\" ]; then" \
	"if [ $_ck_op \"\${SRCDIR}/.git\" ]; then" \
	"if [ $_ck_op \"\$SRCDIR\"/.git ]; then" \
	"test $_ck_op \"\$SRCDIR\"/.git" \
	"if [ $_ck_op \"\$src_dir\"/.git ]; then" \
	"if [ $_ck_op \$SRCDIR/.git ]; then" \
	"if [ $_ck_op .git ]; then" \
	"if [ $_ck_op \"\$WORK/\$name/.git\" ]; then" \
	"[ $_ck_op \"\$d\"/.git ] && echo yes")"
_ck_hit="$(printf '%s\n' "$_ck_forms" | grep -cE "$_ck_pat")"
check_num "premise: ten spellings were built to test the pattern against" \
	"$(printf '%s\n' "$_ck_forms" | grep -c .)" "10"
check_num "the sweep matches every spelling that stats .git as a directory" \
	"$_ck_hit" "10"

# AND MUST NOT MATCH WHAT IT DOES NOT CLAIM. `\.git` with nothing after it is a
# prefix of `.gitignore`, `.gitattributes` and `.github`, so the shipped pattern
# refused three spellings that are correct.
#
# LATENT, NOT LIVE, and the distinction is the whole reason to say the number:
# the false-positive count over every `test/*.sh` and `test/selftest/*.sh` is
# ZERO for the shipped pattern and zero for this one. No suite reads those three
# names with `-d` today. It is fixed anyway because a guard that refuses correct
# code gets switched off, and the rule goes with it -- the cost arrives with the
# first suite that reads `.gitignore`, and it arrives as a red build nobody can
# explain.
#
# EVERY FORM HERE USES THE SAME OPERATOR AS THE ONES ABOVE except where the
# operator is the point. The first draft of this arm spelled `.gitattributes`
# with `-f`, so the boundary it exists to test was never reached: the form was
# excluded for having the wrong operator, and the arm would have passed with the
# boundary deleted.
_ck_ok="$(printf '%s\n' \
	"if [ $_ck_op \"\$SRCDIR/.gitignore\" ]; then" \
	"if [ $_ck_op \"\$SRCDIR/.gitattributes\" ]; then" \
	"if [ $_ck_op \"\$SRCDIR/.github\" ]; then" \
	"[ -f \"\$SRCDIR/.git\" ] || echo no" \
	"[ $_ck_op \"\$SRCDIR/subdir\" ] && git -C \"\$SRCDIR\" status")"
check_num "premise: five legitimate spellings were built to test against" \
	"$(printf '%s\n' "$_ck_ok" | grep -c .)" "5"
check_num "and matches none of .gitignore, .gitattributes, .github or a plain dir" \
	"$(printf '%s\n' "$_ck_ok" | grep -cE "$_ck_pat")" "0"

# AND THE BOUNDARY IS WHAT DOES THE WORK, measured rather than asserted. Naming
# the change that would make the arm above fail is not the same as making it:
# the same five forms against the shipped pattern, which ended at `.git` with
# nothing after it, flag the three that merely START with it.
#
# DERIVED FROM THE PATTERN, NOT COPIED (@OffgridwithJD, review). A second
# hand-written copy is a drift site: edit the boundary in one and the control
# measures a pattern nobody ships, and it does so GREEN. Stripping the boundary
# off the real pattern cannot drift, and it fails SAFE -- change the boundary
# and the strip becomes a no-op, the control keeps the boundary, and the arm
# below measures 0 where it wants 3. Driven with the boundary changed to
# `([^A-Za-z0-9]|$)`: the derived control flags 0 and this arm goes red, where
# a hand-written copy stays green.
_ck_nb="${_ck_pat%'([^A-Za-z0-9_-]|$)'}"
check_text "stripping the boundary actually changed the pattern" \
	"$(if [ "$_ck_nb" = "$_ck_pat" ]; then echo no-op; else echo stripped; fi)" "stripped"
check_num "without the trailing boundary the same pattern flags three of them" \
	"$(printf '%s\n' "$_ck_ok" | grep -cE "$_ck_nb")" "3"

# THE FOLD, DRIVEN BOTH WAYS. The second arm is the change that would make the
# first fail, made rather than named: the same two lines are invisible without
# it.
_ck_cont="$(printf '%s\n' \
	"if [ $_ck_op \\" \
	"	\"\$SRCDIR/.git\" ]; then")"
check_num "premise: the continuation fixture really is two lines" \
	"$(printf '%s\n' "$_ck_cont" | grep -c .)" "2"
check_num "a test split over a line continuation is not invisible to the sweep" \
	"$(printf '%s\n' "$_ck_cont" | _ck_fold | grep -cE "$_ck_pat")" "1"
check_num "and unfolded it is invisible, so the fold is what finds it" \
	"$(printf '%s\n' "$_ck_cont" | sed 's/#.*//' | grep -cE "$_ck_pat")" "0"

# ---- and "its own work tree" is not "inside some work tree" (#1228) ---------
#
# rev-parse searches UPWARD, so a copy with no git presence of its own that sits
# inside any repository answers yes to --absolute-git-dir. `git clone --shared`
# then takes the path literally rather than walking up and dies with
# `fatal: repository does not exist`, so a SKIP carrying instructions became a
# FATAL about a missing repository (@OffgridwithJD).
#
# The reader is EVALLED OUT of extension_upgrade.sh rather than restated, per
# selftest 320: a part that recomputes a rule tests the world instead of the
# code.
_ck_eu="$PGC_TESTDIR/extension_upgrade.sh"
check_num "the suite defines the work-tree reader this part evals" \
	"$(grep -c '^pgc_eu_own_repo()' "$_ck_eu")" "1"
eval "$(sed -n '/^pgc_eu_own_repo()/,/^}/p' "$_ck_eu")"
check "it is callable once evalled" \
	"$(type -t pgc_eu_own_repo)" "function"

# A directory whose only git presence is a gitfile IS its own work tree. That is
# devloop's build dir and every tree an agent stages across a container
# boundary, so accepting it is the whole point of #1224.
_ck_gf="$(mktemp -d)"
printf 'gitdir: %s\n' "$(git -C "$PGC_SRCDIR" rev-parse --absolute-git-dir 2>/dev/null)" \
	> "$_ck_gf/.git"
check_text "a gitfile directory is its own work tree" \
	"$(pgc_eu_own_repo "$_ck_gf" && echo own || echo not-own)" "own"

# A directory NESTED in a repository is not, however loudly rev-parse answers.
_ck_nest="$(mktemp -d)"
git -C "$_ck_nest" init -q . 2>/dev/null
mkdir -p "$_ck_nest/inner"
check_text "premise: rev-parse does answer for the nested directory" \
	"$(git -C "$_ck_nest/inner" rev-parse --absolute-git-dir >/dev/null 2>&1 && echo answers || echo silent)" \
	"answers"
check_text "but a directory inside another repository is NOT its own work tree" \
	"$(pgc_eu_own_repo "$_ck_nest/inner" && echo own || echo not-own)" "not-own"
rm -rf "$_ck_gf" "$_ck_nest"

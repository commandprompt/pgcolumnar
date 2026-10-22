# ---- an arm's failure must say what it measured (#1164) ---------------------
#
#     check "..." "$([ "$WF" -le $((NF * 2)) ] && echo yes || echo no)" "yes"
#
# Two measured numbers, reduced to `yes`/`no` before the comparison. The failure
# reads `got no want yes`, which is the word FAILED spelled twice. A reader who
# hits that red at 3am learns nothing about WF, NF, or how far apart they were.
#
# WHAT IT COST, measured rather than imagined. A PG 15 leg reddened one arm of
# `sorted_pathkeys`, and two sessions spent an afternoon unable to say whether the
# ORACLE (a six-buffer spurious gap) or the CONTROL (a fourteen-buffer collapse)
# had failed -- different defects with different owners. Four further experiments
# were aimed at a target whose identity was unknown.
#
# AND THE SHELL SIDE IS WHERE THE LESSON LANDS HARDEST, because the shell suite
# for that very property had ALWAYS carried its numbers --
#
#     no (on=$PB_NOORDER_ON off=$PB_NOORDER_OFF over $PB_GROUPS groups)
#
# -- and the pytest PORT dropped them. Two of the four pairs repaired under #1164
# were port regressions of exactly that shape. `compare_to_bash` grades the check
# NAME, so both halves graded `missing: 0` while one printed its measurement and
# the other printed `got 0 want 1`. Name parity does not preserve diagnostics.
#
# WHY THIS IS A LIST AND NOT A ZERO. The pytest corpus reaches zero and its twin
# asserts zero. This corpus holds 77 such arms across 52 suites, and a rule that
# went red on arrival with 77 offenders would be switched off within a week. So
# the debt is TRACKED, in `test/lossy_arms.tsv`, and the gate refuses BOTH
# directions: an unlisted offender fails by name, and a listed arm that has since
# been repaired fails until its row goes. The list may only shrink, and it does so
# by construction rather than by anyone maintaining a number.
#
# The asymmetry between the two halves is deliberate. The corpora are in different
# states, and a guard that asserted zero over both would claim a property this
# tree does not have.
#
# THE RULE. An `$([ TEST ] && echo A || echo B)` handed to a recorder is refused
# -- and so is `$([ TEST ] && echo A)` with no else, which is WORSE: it reports
# `got [] want [ok]`, losing the measurement and the verdict together.
# when both hold:
#
#   (a) the failing set has MORE THAN ONE MEMBER, so the boolean does not say
#       which state was reached; AND
#   (b) NEITHER echo carries a value, so the message cannot recover it.
#
# Determinate, and deliberately out of scope: unary file and string tests
# (`-f`, `-n`, `-z` and the rest), equality (`=`, `!=`, `-eq`, `-ne`), and
# nothing-versus-something (`-gt 0`, `-ge 1`) where exactly one value fails.
#
# THOSE CARVE-OUTS ARE NOT TIDINESS, THEY WERE MEASURED. Three drafts of this
# sweep over-reported, each caught by reading what it SELECTED rather than by
# trusting the count: `-f "$CB"` flagged as lossy (a file test names its own
# failure), `-n "$emsg"` flagged twice after `n` and `z` were dropped from the
# unary set in a rewrite, and equality flagged before it was excluded. The counts
# went 77, then 94, then 55. **A rule that fails toward MORE findings is the
# direction that gets published**, so the carve-outs are driven below rather than
# described.
#
# THE SCOPE IS A SHAPE, NOT A DIRECTORY, and saying only the directory once cost
# 26 arms (#1174). The selector below reads two spellings of a verdict:
#
#   $( [ TEST ] && echo A || echo B )        a test built into the shell
#   $( awk ... print (COND) ? A : B ... )    a verdict computed by awk
#
# The second was invisible until #1174, so `lossy_arms.tsv` read as the debt of
# the shell corpus when it was the debt of one spelling.
#
# THE POPULATION, WITH ITS BUCKETS SUMMING TO IT. Measured over test/*.sh and
# test/selftest/*.sh at f1c3b7a:
#
#     awk-valued recorder arms           136
#       awk as an extractor               57   concludes nothing
#       a branch carries a value          35   correct by the rule
#       both branches constant            44
#         excused by the carve-out        20   nothing-versus-something, equality
#         LOSSY                           24   still unrepaired
#       inputs 136 == sum of buckets 136
#
# THE PARTITION MOVED WITH A REPAIR, AND NOTHING WOULD HAVE NOTICED. It read
# 57/33/46 (20 excused, 26 lossy) until parallel_scan_cost's two "io-kept"
# arms were repaired: each moved from "both constant and lossy" into "a branch
# carries a value", so carries went 33 -> 35, both-constant 46 -> 44 and LOSSY
# 26 -> 24. The TOTAL does not move, because a repaired arm is still an
# awk-valued recorder arm. The tsv count is derived from the rows and follows a
# repair on its own; THESE NUMBERS ARE TYPED PROSE and no check reads them, so
# a repair that leaves them alone leaves the file describing a tree that no
# longer exists. Re-derive them in the commit that repairs an arm.
#
# THE FOURTH BUCKET IS THE ONE THAT MATTERS AND I PUBLISHED THIS WITHOUT IT.
# The first version of this note read "57, 33 and 26", which sums to 116 against
# a stated 136: the 20 the carve-out EXCUSES had been dropped and the 26 was
# labelled "both branches constant" when it means "both constant AND lossy".
# A reader given three buckets cannot tell whether an arm left the population
# because the rule excused it or because the sweep never saw it -- which is the
# distinction this whole file is about. Caught by @OffgridwithJD, against the
# repository rule that a list-derived claim prints `inputs == sum(buckets)`
# beside it. That identity is now printed above, where it can be checked.
#
# STILL OUT OF SCOPE, and named rather than left to be rediscovered:
#
#   A VERDICT COMPUTED INTO A VARIABLE and checked on a later line --
#   `hit="$(awk ... ? 1 : 0)"` then `check "..." "$hit" "1"` -- the same defect
#   with the arm two statements from its name. Naming it needs dataflow this
#   sweep does not do, and guessing from the nearest preceding name charges it to
#   the wrong check: that is measured, not feared, in the matcher below.
#
#   A VERDICT RENDERED BY if/else RATHER THAN A TERNARY --
#   `BEGIN { if (a <= b*2) print "yes"; else print "no" }` -- which is
#   both-constant and throws its operands away, and which the matcher does not
#   read. Zero live sites, swept with continuations folded; the two non-ternary
#   near-misses are printf extractors. Reported by @OffgridwithJD and left open
#   deliberately: the argument that closed `any()` on #1166 was that an author
#   told "your min arm is refused" reaches for it, and nobody rewrites a ternary
#   as if/else to dodge a guard.
#
#   A VERDICT RENDERED BY AN AWK AT THE END OF A PIPE --
#   `$(cmd | awk '{print ($1 <= $2 * 2) ? "yes" : "no"}')` -- which is
#   both-constant, throws its operands away, and which the matcher does not
#   read, because the matcher requires `$(awk` and a pipe puts something else
#   in that position. Reported by @OffgridwithJD; the numbers here are a second
#   sweep, run against a lift of `_lam_sweep` that was validated FIRST by
#   reproducing the shipped list exactly -- 103 found, 103 tracked, both `comm`
#   directions empty -- so what follows is about the matcher and not about a
#   copy of it.
#
#   SEVEN arms render a verdict this way. FIVE are invisible to the sweep, and
#   are outside the 136 above because that population requires `$(awk` too:
#
#       cost_written_geometry.sh            ($1 >= 1)
#       logical_decoding_cdc_recipe.sh      ($1 > 0)
#       native_fetch_interrupt.sh   (x2)    ($1 >= 1)
#       pg_dump_roundtrip.sh                ($1 > 0)
#
#   THE OTHER TWO ARE EXAMINED BY ACCIDENT, and that is the part worth knowing.
#   Both are in `decode_interrupts.sh`, and their FIRST pipe stage is itself a
#   `$(awk ...`, which satisfies the matcher's test; the greedy strip to the
#   last `print` then reads the second stage's condition, correctly. Rewrite
#   that first stage as `grep` and the arm leaves the sweep without its verdict
#   changing at all. Membership, so the two claims can be checked separately:
#   7 piped arms == 2 inside the 136 + 5 outside it. (#1341 retired those two:
#   decode_interrupts.sh now compares the count its own `in_loop` returns, so the
#   piped arms are the five outside.)
#
#   ZERO ARE LOSSY TODAY: all seven conditions are `> 0` or `>= 1`, which the
#   carve-out excuses. Proven rather than eyeballed, by putting four arms
#   through the lifted sweep --
#
#       a  piped awk,       excused condition    not flagged   right either way
#       b  substituted awk, same condition       not flagged   carve-out works
#       c  piped awk,       LOSSY condition      NOT FLAGGED   <- the gap
#       d  substituted awk, same LOSSY condition flagged       <- the control
#
#   -- where c and d carry the same condition and the same two constant
#   branches, so the pipe is the whole difference, and d flagging is what says
#   the probe can speak at all. A fifth arm in the `decode_interrupts` shape,
#   with a lossy second stage, IS flagged: that is what "by accident" was
#   measured with rather than asserted.
#
#   RECORDED, NOT CLOSED, for the reason given just above: widening `\$\(awk`
#   to "an awk that is the last stage of the substitution" would cover both
#   spellings and would add zero rows today, and nobody rewrites a substitution
#   as a pipe to dodge a guard.
#
# SCOPED TO test/*.sh AND test/selftest/*.sh, EXCEPT THIS FILE. The first draft
# excluded the whole `selftest/` directory on the grounds that "this file's own
# explanation of the rule, and the control fixture below, both contain the shape
# they exist to describe". That is exactly right for 540 and **it is not a
# property of the directory** -- @OffgridwithJD pointed at
# `410-a-check-must-have-been-red.sh`, an ordinary part carrying a genuinely lossy
# arm by this rule: two computed line numbers compared with `-lt`, both discarded,
# where the comment above it records that the real failure was "definition at line
# 1128, called at 762" -- precisely the two numbers `got [after] want [before]`
# throws away.
#
# Selftest 260's mistake was sweeping the ENFORCER for instances of the rule. The
# enforcer is one file, so one file is what the exclusion covers. 080 records the
# same correction twice, in the other direction: its directory scoping "fell out
# of writing a glob" rather than being decided, and bench/ then held six instances
# it never looked at.

_lam_list="$PGC_TESTDIR/lossy_arms.tsv"

# THE CONTROL RUNS FIRST, because a sweep that matches nothing reports the same
# clean result as a corpus with nothing to find -- the `d41d8cd98f00` shape, where
# an extractor that finds nothing hashes identically on both sides. A planted
# fixture says the matcher can still see the thing it is looking for.
_lam_probe="$PGC_WORKDIR/lam_probe.sh"
cat > "$_lam_probe" <<'PROBE'
check "a threshold whose operands vanish" \
	"$([ "$WF" -le $((NF * 2)) ] && echo yes || echo no)" "yes"
check "a count against a floor" \
	"$([ "${SEEN:-0}" -ge 7 ] && echo yes || echo no)" "yes"
check "determinate: nothing versus something" \
	"$([ "$rows" -gt 0 ] && echo yes || echo no)" "yes"
check "determinate: a file either exists or does not" \
	"$([ -f "$CB" ] && echo yes || echo no)" "yes"
check "determinate: a string is empty or it is not" \
	"$([ -n "$emsg" ] && echo yes || echo no)" "yes"
check "carried: the branch names the number" \
	"$([ "$n" -ge 5 ] && echo yes || echo "no (found $n)")" "yes"
PROBE

# THE SHELL if/then/else FIXTURE (#1255), with its own carve-out controls and a
# HELPER PLACED BELOW AN INNOCENT ARM -- the shape that proves the `[` rule's
# same-line recorder conjunct, since without it the helper is charged upward.
_lam_probe_ifs="$PGC_WORKDIR/lam_probe_ifs.sh"
cat > "$_lam_probe_ifs" <<'PROBE'
check_num "an if/then/else verdict whose operand vanishes" \
	"$(if [ "$sj" -ge 2 ]; then echo 1; else echo 0; fi)" "1"
check_num "determinate: an if/then/else against a floor of one" \
	"$(if [ "$rows" -ge 1 ]; then echo 1; else echo 0; fi)" "1"
check_num "determinate: an if/then/else on a file test" \
	"$(if [ -f "$CB" ]; then echo 1; else echo 0; fi)" "1"
check_text "carried: the if/then/else branch names the number" \
	"$(if [ "$n" -ge 5 ]; then echo yes; else echo "no (found $n)"; fi)" "yes"
check_text "innocent: this arm carries its own value" \
	"$(printf '%s' "$probed")" "yes"
margin() {
	echo "$([ "$1" -gt "$2" ] && echo over || echo under)"
}
PROBE

_lam_sweep() {  # _lam_sweep FILE -> one "suite<TAB>name" per lossy arm
	awk '
		function determinate(t) {
			# ANCHORED to the start of the test. Searching anywhere matched
			# `grep -c .` inside a command substitution as a unary file test --
			# `c` is in the class -- and silently excused five real offenders.
			if (t ~ /^[[:space:]]*!?[[:space:]]*-[fdersxwLhpSbcgkuGnz][[:space:]]/) return 1
			if (t ~ /(^|[[:space:]])(-eq|-ne|!=|==|=)([[:space:]]|$)/) return 1
			if (t ~ /-(gt|ge)[[:space:]]+"?[01]"?[[:space:]]*$/) return 1
			return 0
		}
		# The same carve-outs the `[` rule uses, written in awk syntax.
		#
		# EXACTLY ONE COMPARISON. `(s>0 && p>0)` ends in `p>0` and would read as
		# nothing-versus-something, while hiding WHICH of two costs went
		# non-positive -- the arm #1172 repaired by hand. A condition carrying
		# more than one comparison is a compound and stays in scope.
		function awk_determinate(c,   n, i) {
			sub(/[[:space:]]*\)*[[:space:]]*$/, "", c)
			n = gsub(/<=|>=|==|!=|<|>/, "&", c)
			if (n != 1) return 0
			if (c ~ /(==|!=)/) return 1
			if (c ~ />[[:space:]]*0$/) return 1
			if (c ~ />=[[:space:]]*1$/) return 1
			return 0
		}
		# CONTINUATIONS ARE FOLDED FIRST. The corpus wraps these arms across
		# lines, and a line-by-line reader sees `&& echo "smaller"` without the
		# `|| echo "UNCHANGED ($ROWS_AFTER of $ROWS_BEFORE)"` that follows it --
		# so four arms that DO carry their values read as though they discard
		# them. Measured: 65 sites before folding, 61 after.
		{
			line = $0
			while (line ~ /\\$/ && (getline nxt) > 0) {
				sub(/\\$/, " ", line)
				line = line nxt
			}
		}
		# A WHOLE-LINE COMMENT IS NOT CODE (#1255). A comment QUOTING the
		# anti-pattern supplied a real match, and the missing recorder conjunct
		# below then gave it the name of whatever arm preceded it -- so a file
		# was refused for a shape it only DESCRIBED. It takes both defects; each
		# alone is inert. Found by @jdatcmd, who had written such a comment.
		#
		# WHOLE-LINE ONLY, AND DELIBERATELY NOT `sub(/#.*/, "")`. 214 check names
		# in this corpus contain a `#` -- `(#355 premise)`, `(#1164)` -- and
		# stripping from the first one truncates the NAME the sweep reports,
		# which trades a false positive for a corpus of mangled rows. A `#` in
		# the middle of a line may be inside a string; a `#` at the start of one
		# cannot be.
		line ~ /^[[:space:]]*#/ { next }
		# The recorder call carries the NAME in its first quoted argument.
		line ~ /(^|[[:space:]])(check|check_num|check_text|check_ratio|pgc_fail|check_skip)[[:space:]]+"/ {
			nm = line
			sub(/^[^"]*"/, "", nm); sub(/".*$/, "", nm)
			name = nm
		}
		# AN AWK-RENDERED VERDICT, same rule and a different spelling (#1174).
		#
		# THE BRANCHES DECIDE, NOT THE PROGRAM. The `[` arm below excuses any arm
		# whose branches contain `$`, because a branch that interpolates carries
		# its value. That test cannot be reused here: an awk program written in
		# DOUBLE quotes interpolates in its CONDITION -- `print ($LO * 8 <= $HI)
		# ? 1 : 0` -- and still throws both operands away. So the two branches are
		# matched as literals, and anything concatenated onto one fails the match
		# and is treated as carrying.
		#
		# A BARE NUMBER IS A CONSTANT TOO. `? 1 : 0` renders `got 0 want 1`, which
		# is the exact symptom #1164 is named for, and a first pass over this
		# corpus keyed on quoted branches alone and reported zero of them.
		# THE RECORDER MUST BE ON THIS LINE. `name` holds the LAST name seen, so
		# a rule that fires on any line would charge an arm to whatever check
		# preceded it. Measured: without this, `test/lib.sh:1347` -- an `if` in a
		# helper -- was reported as `lib: $name`, and three `bloom_hit="$(awk
		# ... ? 1 : 0)"` ASSIGNMENTS in native_join_runtime_filter were charged to
		# unrelated checks. 4 of 32 findings were manufactured that way.
		#
		# A BOOLEAN ASSIGNED TO A VARIABLE AND CHECKED LATER IS THE SAME DEFECT
		# and is deliberately OUT OF SCOPE here: the arm is two statements apart
		# from its name, so naming it needs dataflow this sweep does not do.
		# Recorded rather than mis-attributed.
		line ~ /(^|[[:space:]])(check|check_num|check_text|check_ratio|pgc_fail|check_skip)[[:space:]]+"/ &&
		line ~ /\$\(awk/ {
			t = line
			gsub(/\\"/, "\"", t)   # an awk program in double quotes escapes its own
			if (match(t, /\?[[:space:]]*("[^"]*"|[0-9]+(\.[0-9]+)?)[[:space:]]*:[[:space:]]*("[^"]*"|[0-9]+(\.[0-9]+)?)[[:space:]]*[})]/)) {
				cond = substr(t, 1, RSTART - 1)
				sub(/^.*print[[:space:]]*/, "", cond)
				if (!awk_determinate(cond) && name != "") print suite "\t" name
			}
		}
		# THE SAME SAME-LINE CONJUNCT THE awk RULE CARRIES (#1255). Without it
		# this rule fires on any line and then uses `name`, the LAST name seen,
		# so a helper holding this shape is charged to whichever recorder arm
		# precedes it. The comment above the awk rule explains exactly this and
		# was applied to one rule of the two. Adding it here found nothing on
		# the corpus -- 101 before, 101 after, both comm directions empty -- so
		# it closes a shape rather than removing a live wrong name.
		line ~ /(^|[[:space:]])(check|check_num|check_text|check_ratio|pgc_fail|check_skip)[[:space:]]+"/ &&
		line ~ /\$\([[:space:]]*\[[^]]*\][[:space:]]*&&[[:space:]]*echo/ {
			body = line
			sub(/^.*\$\([[:space:]]*\[/, "", body)
			test = body; sub(/\].*$/, "", test)
			arms = body; sub(/^[^]]*\][[:space:]]*/, "", arms)
			# (b) a branch that interpolates carries the value already.
			if (arms ~ /\$/) next
			if (determinate(test)) next
			if (name != "") print suite "\t" name
		}
		# THE SHELL if/then/else SPELLING (#1255). The two rules above both
		# require `&&`, so `$(if [ COND ]; then echo 1; else echo 0; fi)` passed
		# through untouched -- both branches constant, the operand discarded, and
		# `got [0] want [1]` printed on failure, which is the symptom #1164 is
		# named for. Five live arms were lossy this way and none was tracked.
		#
		# NOT one of the gaps this file already records: all three of those are
		# awk -- a verdict in a variable, an awk `if/else` inside BEGIN, and an
		# awk at the end of a pipe. Both awk gaps were left open on the stated
		# ground of "ZERO ARE LOSSY TODAY", which was right for them and is not
		# available here.
		#
		# THE SAME TWO CARVE-OUTS as the `[` rule, for the same reasons: a
		# branch that interpolates carries its value already, and a determinate
		# test names its own subject.
		line ~ /(^|[[:space:]])(check|check_num|check_text|check_ratio|pgc_fail|check_skip)[[:space:]]+"/ &&
		line ~ /\$\([[:space:]]*if[[:space:]]*\[/ {
			ib = line
			sub(/^.*\$\([[:space:]]*if[[:space:]]*\[/, "", ib)
			itest = ib; sub(/\].*$/, "", itest)
			iarms = ib; sub(/^[^]]*\][[:space:]]*;?[[:space:]]*/, "", iarms)
			sub(/[[:space:]]*fi[[:space:]]*\).*$/, "", iarms)
			if (iarms !~ /then[[:space:]]*echo/) next
			if (iarms !~ /else[[:space:]]*echo/) next
			if (iarms ~ /\$/) next
			if (determinate(itest)) next
			if (name != "") print suite "\t" name
		}
	' suite="$(basename "${1%.sh}")" "$1"
}

# AND THE SAME RULE OVER AN AWK-RENDERED VERDICT (#1174). A separate fixture, so
# the two controls above keep their numbers and this family is counted on its own.
#
# THE INTERPOLATION RULE CANNOT BE REUSED WHOLESALE. The `[` matcher excuses an
# arm whose branches contain `$`, because a branch that interpolates carries its
# value. An awk program written in DOUBLE quotes carries `$LO` in its CONDITION
# and constants in its branches -- `print ($LO * 8 <= $HI) ? 1 : 0` -- so a test
# for `$` anywhere excuses the whole family. The branches are what must be read.
_lam_probe_awk="$PGC_WORKDIR/lam_probe_awk.sh"
cat > "$_lam_probe_awk" <<'PROBE'
check "an awk verdict whose operands vanish" \
	"$(awk -v a="$WF" -v b="$NF" 'BEGIN{ print (a <= b * 2) ? "yes" : "no" }')" "yes"
check "an awk verdict rendered as a number" \
	"$(awk "BEGIN{print ($LO * 8 <= $HI) ? 1 : 0}")" "1"
check "determinate: an awk count against nothing" \
	"$(awk -v n="$SEEN" 'BEGIN{ print (n > 0) ? "some" : "none" }')" "some"
check "carried: the awk branch names the number" \
	"$(awk -v n="$n" 'BEGIN{ print (n >= 5) ? "yes" : "no (found " n ")" }')" "yes"
PROBE

_lam_probe_hits="$(_lam_sweep "$_lam_probe" | wc -l | tr -d ' ')"
check "control: the sweep sees a threshold arm and a floor arm" \
	"$_lam_probe_hits" "2"

# THE CARVE-OUTS, DRIVEN. Without these the rule refuses most of the corpus, and
# a rule that refuses everything is argued with rather than kept.
_lam_probe_names="$(_lam_sweep "$_lam_probe" | cut -f2 | sort | tr '\n' ';')"
check_text "control: a determinate or carrying arm is not swept up" \
	"$_lam_probe_names" "a count against a floor;a threshold whose operands vanish;"

_lam_awk_hits="$(_lam_sweep "$_lam_probe_awk" | wc -l | tr -d ' ')"
check "control: the sweep sees an awk verdict that discards its operands" \
	"$_lam_awk_hits" "2"

_lam_awk_names="$(_lam_sweep "$_lam_probe_awk" | cut -f2 | sort | tr '\n' ';')"
check_text "control: a determinate or carrying awk arm is not swept up" \
	"$_lam_awk_names" "an awk verdict rendered as a number;an awk verdict whose operands vanish;"

_lam_ifs_hits="$(_lam_sweep "$_lam_probe_ifs" | wc -l | tr -d ' ')"
check "control: the sweep sees a shell if/then/else that discards its operand" \
	"$_lam_ifs_hits" "1"

# AND NAMES IT, which is the half a count cannot check. The innocent arm sits
# directly above a helper holding the `&&` shape, so a sweep without the
# same-line conjunct reports TWO names here and one of them is a real arm that
# is not lossy. Naming rather than counting is what distinguishes those.
_lam_ifs_names="$(_lam_sweep "$_lam_probe_ifs" | cut -f2 | sort | tr '\n' ';')"
check_text "control: the carve-outs hold and the innocent arm above the helper is not named" \
	"$_lam_ifs_names" "an if/then/else verdict whose operand vanishes;"

# A COMMENT DESCRIBES THE SHAPE, IT DOES NOT COMMIT IT (#1255). A file was
# refused for an anti-pattern it only WROTE ABOUT, and the name it was refused
# under belonged to an innocent arm above the comment.
#
# IT MUST QUOTE A WHOLE RECORDER CALL, NOT A FRAGMENT, and the first version of
# this fixture quoted a fragment -- which made both arms below VACUOUS. A
# fragment carries no recorder call on its line, so the same-line conjunct added
# above already refuses it whether or not comments are skipped: the arms passed
# with the rule and without it, and mutation C reddened NOTHING on five majors.
# Measured, once the fixture was fixed:
#
#     fragment comment     0 hits with the rule, 0 without   <- conjunct blocks it
#     whole-call comment   0 hits with the rule, 1 without   <- what the rule is for
#
# And the name the unguarded sweep invents is `a quoted arm that only exists in
# prose` -- not a mis-attributed real arm but one that exists nowhere, which is
# the worse of the two failures.
_lam_cmt="$PGC_WORKDIR/lam_probe_cmt.sh"
cat > "$_lam_cmt" <<'PROBE'
check_text "innocent: an arm that carries its own value" \
	"$(printf '%s' "$probed")" "yes"
# check_num "a quoted arm that only exists in prose" \
# 	"$([ "$a" -lt "$b" ] && echo 1 || echo 0)" "1"
PROBE
check_num "premise: the comment fixture quotes a whole recorder call, not a fragment" \
	"$(grep -cE '^[[:space:]]*#[[:space:]]*check_num[[:space:]]+"' "$_lam_cmt")" "1"
check_num "a comment quoting an arm is not an instance of one" \
	"$(_lam_sweep "$_lam_cmt" | wc -l | tr -d ' ')" "0"

# The corpus, against the tracked debt. Both directions, in one pass.
_lam_found="$PGC_WORKDIR/lam_found.tsv"
: > "$_lam_found"
for _lam_f in "$PGC_TESTDIR"/*.sh "$PGC_TESTDIR"/selftest/*.sh; do
	# This file only: it describes the shape and plants one in its probe.
	case "$_lam_f" in *540-an-arm-must-carry-its-measurement.sh) continue ;; esac
	_lam_sweep "$_lam_f" >> "$_lam_found"
done
sort -u -o "$_lam_found" "$_lam_found"

_lam_tracked="$PGC_WORKDIR/lam_tracked.tsv"
grep -v '^#' "$_lam_list" | grep -v '^[[:space:]]*$' | sort -u > "$_lam_tracked"

# A SWEEP THAT READ NOTHING REPORTS THE SAME CLEAN RESULT AS A CLEAN CORPUS, so
# assert it read the corpus before either comparison is believed.
check "premise: the sweep read the corpus and classified arms in it" \
	"$([ "$(wc -l < "$_lam_found")" -ge 20 ] && echo yes || echo "no ($(wc -l < "$_lam_found"))")" \
	"yes"

_lam_new="$(comm -23 "$_lam_found" "$_lam_tracked" | sed 's/\t/: /' | tr '\n' ';')"
check_text "a new arm that cannot say what it measured is refused by name" \
	"${_lam_new:-none}" "none"

_lam_stale="$(comm -13 "$_lam_found" "$_lam_tracked" | sed 's/\t/: /' | tr '\n' ';')"
check_text "a repaired arm must have its row removed, so the list only shrinks" \
	"${_lam_stale:-none}" "none"

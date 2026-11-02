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
# nothing-versus-something (`-gt 0`, `-ge 1`, and the mirrored `0 -lt`, `1 -le`)
# where exactly one value fails. A compound test, joined by `-a`, `-o`, `&&` or
# `||`, is never excused (#1989).
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
#   $( [ TEST ] && echo A || echo B )        a test built into the shell,
#                                            also as [[ TEST ]] or test TEST
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
		# HIDE WHAT IS QUOTED OR SUBSTITUTED (#1986). Returns s with every
		# character inside quotes, a `$(...)` or a backtick replaced by `_`,
		# so the result has the same length and its positions match s. A
		# substitution may hold quotes and parentheses of its own, and may
		# nest, so a regular expression cannot find where it ends. KNOWN
		# LIMIT: the `x)` of a `case` inside `$(...)` closes it early. No arm
		# in the tree holds one.
		function mask(s,   n, i, ch, d, q, bt, out) {
			n = length(s); out = ""; d = 0; q[0] = ""; bt = 0
			for (i = 1; i <= n; i++) {
				ch = substr(s, i, 1)
				if (q[d] == "\047") {
					if (ch == "\047") q[d] = ""
					out = out "_"; continue
				}
				if (ch == "\\" && i < n) {
					if (d > 0 || q[d] != "" || bt) out = out "__"
					else out = out substr(s, i, 2)
					i++; continue
				}
				if (bt) { if (ch == "`") bt = 0; out = out "_"; continue }
				if (ch == "`") { bt = 1; out = out "_"; continue }
				if (ch == "$" && substr(s, i + 1, 1) == "(") {
					d++; q[d] = ""; out = out "__"; i++; continue
				}
				if (q[d] == "\"") {
					if (ch == "\"") q[d] = ""
					out = out "_"; continue
				}
				if (ch == "\"" || ch == "\047") { q[d] = ch; out = out "_"; continue }
				if (d > 0 && ch == "(") { d++; q[d] = ""; out = out "_"; continue }
				if (d > 0 && ch == ")") { d--; out = out "_"; continue }
				out = out ((d > 0 || q[0] != "") ? "_" : ch)
			}
			return out
		}
		function determinate(t,   c) {
			# A COMPOUND IS NEVER EXCUSED (#1989). `[ "$b" -le "$c" -a "$a"
			# -gt 0 ]` ends in `-gt 0`, and an equality half matched from any
			# position, so both read as determinate while `no` hid which half
			# failed. The awk rule below refuses more than one comparison for
			# the same reason. Quotes and command substitutions are hidden
			# first: the `|| true` of `"$(grep -c "(x)" || true)"` and the
			# `-o` of `grep -o` do not join two tests. `&&` and `||` need no
			# spaces around them, because `[[ A&&B ]]` is a compound too.
			c = mask(t)
			if (c ~ /&&|\|\|/) return 0
			# A JOINING `-a` OR `-o` HAS A TEST AFTER IT. In `[ "$arg" = -a ]`
			# the `-a` is the right-hand operand of `=`, not a join.
			if (c ~ /(^|[[:space:]])-[ao][[:space:]]+[^[:space:]]/) return 0
			# ANCHORED to the start of the test. Searching anywhere matched
			# `grep -c .` inside a command substitution as a unary file test --
			# `c` is in the class -- and silently excused five real offenders.
			if (t ~ /^[[:space:]]*!?[[:space:]]*-[fdersxwLhpSbcgkuGnz][[:space:]]/) return 1
			if (t ~ /(^|[[:space:]])(-eq|-ne|!=|==|=)([[:space:]]|$)/) return 1
			# OF THE -gt AND -ge FORMS, ONLY `-gt 0` AND `-ge 1` (#1980). The
			# class used to be `-(gt|ge) [01]`. That also excused `-gt 1`, which
			# fails on 0 AND on 1, so its `no` does not say which, and `-ge 0`,
			# which a count always passes. The awk carve-out reads `> 0` and
			# `>= 1` only, and now the two agree. The end anchor keeps `-gt 05`
			# and `-ge 10` out (#1989).
			if (t ~ /-gt[[:space:]]+"?0"?[[:space:]]*$/) return 1
			if (t ~ /-ge[[:space:]]+"?1"?[[:space:]]*$/) return 1
			# THE SAME TWO, MIRRORED (#1986). `[ 0 -lt "$x" ]` is `-gt 0` and
			# `[ 1 -le "$x" ]` is `-ge 1`. The start anchor keeps `[ 10 -lt
			# "$x" ]` out. `-lt 1` and `-le 0` stay refused: they fail on every
			# count above 0, so `no` does not say which. The pytest twin refuses
			# `r <= 0` for the same reason.
			if (t ~ /^[[:space:]]*"?0"?[[:space:]]+-lt[[:space:]]/) return 1
			if (t ~ /^[[:space:]]*"?1"?[[:space:]]+-le[[:space:]]/) return 1
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
		# ONE REDIRECT PATTERN, used by every rule that strips one (#1980,
		# #1989). A target is a quoted string, which may hold a space, or a
		# word that stops at a space, `&`, `|`, `;` or `)`.
		BEGIN {
			RD = "((&|[0-9]*)[<>]+&?[[:space:]]*(\"[^\"]*\"[^[:space:]&|;)]*|[^[:space:]&|;)]*)[[:space:]]*)*"
		}
		# WHERE THE LAST MATCH OF re STARTS IN s, or 0. The rules take the LAST
		# `$([`, so `$([ "$(wc -l < f)" -ge 20 ] ...)` reads the outer test.
		function last_at(s, re,   p, off) {
			p = 0; off = 0
			while (match(s, re)) {
				p = off + RSTART
				off = p
				s = substr(s, RSTART + 1)
			}
			return p
		}
		# SPLIT ONE TEST FROM WHAT FOLLOWS IT (#1986). s starts at the test.
		# Sets T to the text the test compares and REST to the text after it.
		# `test` ends at the first `&`, `|`, `;`, `<` or `>` that is not
		# quoted or inside a substitution, so `test "$(wc -l < f)" -ge 20`
		# is read whole. A trailing file descriptor such as the `2` of
		# `2>x` goes back to REST. `[` and `[[` still end at the first raw
		# `]`, which hides seven live lossy arms (#2002). KNOWN LIMIT: a
		# redirect between the operands, as in `test "$a" -le 2>x "$b"`,
		# hides the arm. No arm in the tree holds one.
		function split_test(s) {
			sub(/^[[:space:]]*/, "", s)
			if (match(s, /^\[\[[^]]*\]\]/)) {
				T = substr(s, 3, RLENGTH - 4); REST = substr(s, RLENGTH + 1); return 1
			}
			if (match(s, /^\[[^]]*\]/)) {
				T = substr(s, 2, RLENGTH - 2); REST = substr(s, RLENGTH + 1); return 1
			}
			if (match(mask(s), /^test[[:space:]][^&|;<>]*/)) {
				T = substr(s, 5, RLENGTH - 4); REST = substr(s, RLENGTH + 1)
				if (REST ~ /^[<>]/ && match(T, /[[:space:]][0-9]+$/)) {
					REST = substr(T, RSTART + 1) REST; T = substr(T, 1, RSTART)
				}
				return 1
			}
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
		# A REDIRECT MAY SIT BETWEEN THE TEST AND THE `&&` (#1980). The rule
		# required `&&` straight after the `]`, so `[ ... ] 2>/dev/null && echo
		# yes || echo no` was never examined. The redirect may be `2>x`, `2> x`,
		# `2>&1`, `>&2`, `&>x` or `2>"a b"`. It is stripped from the arms too, so
		# a `2>"$LOG"` is not read as a branch that carries a value.
		#
		# THREE SPELLINGS OF THE TEST (#1986): `[ ... ]`, `[[ ... ]]` and
		# `test ...`. Only the first was read, so the other two hid a lossy arm.
		(p = last_at(line, "\\$\\([[:space:]]*(\\[|test[[:space:]])")) {
			arms = ""
			if (split_test(substr(line, p + 2))) {
				arms = REST
				sub("^[[:space:]]*" RD, "", arms)
			}
			if (arms ~ /^&&[[:space:]]*echo/) {
				# (b) a branch that interpolates carries the value already.
				if (arms ~ /\$/) next
				if (determinate(T)) next
				if (name != "") print suite "\t" name
			}
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
		# test names its own subject. THE SAME THREE SPELLINGS too (#1986).
		line ~ /(^|[[:space:]])(check|check_num|check_text|check_ratio|pgc_fail|check_skip)[[:space:]]+"/ &&
		(p = last_at(line, "\\$\\([[:space:]]*if[[:space:]]+(\\[|test[[:space:]])")) {
			ib = substr(line, p + 2)
			sub(/^[[:space:]]*if[[:space:]]+/, "", ib)
			if (!split_test(ib)) next
			# THE REDIRECT IS STRIPPED HERE TOO (#1980), by the same pattern as
			# the `[` rule. Without it, `if [ ... ] 2>"$LOG"; then echo 1; else
			# echo 0; fi` reads as carrying its value, because of the `$`.
			iarms = REST
			sub("^[[:space:]]*" RD, "", iarms)
			sub(/^;?[[:space:]]*/, "", iarms)
			sub(/[[:space:]]*fi[[:space:]]*\).*$/, "", iarms)
			if (iarms !~ /then[[:space:]]*echo/) next
			if (iarms !~ /else[[:space:]]*echo/) next
			if (iarms ~ /\$/) next
			if (determinate(T)) next
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

# A REDIRECT AFTER THE TEST, AND A FLOOR OF TWO (#1980). Two arms that print
# only `no` on failure were not seen:
#
#   - `[ ... ] 2>/dev/null && echo yes || echo no`. The `[` rule required `&&`
#     straight after the `]`, so any redirect in between hid the arm.
#   - `[ "$x" -gt 1 ]`. The carve-out excused `-gt 0`, `-gt 1`, `-ge 0` and
#     `-ge 1`. It now excuses only `-gt 0` and `-ge 1` of these. `-gt 1`
#     fails on 0 and on 1, so `no` does not say which. The awk carve-out
#     already reads `> 0` and `>= 1` only.
#
# The determinate and carrying arms behind a redirect are the controls: a
# redirect must not make the carve-outs stop working. A redirect into a
# variable (`2>"$LOG"`) must not excuse an arm, in the `[` rule or in the
# if/then/else rule. `-ge 0` must not be excused.
#
# EVERY PIECE OF THE REDIRECT PATTERN IS PINNED (#1995 review round 3). A
# mechanical sweep weakened and over-applied each quantifier, anchor,
# alternation branch and class member of the regular expressions in this part.
# The arms from `joined to an unspaced and` down to `an unspaced redirect`
# are the ones that sweep found missing: without each, one change to the
# pattern stayed green. The three `innocent:` arms are a test piped into
# `cat`, whose `&&` reads cat's status and not the test's.
_lam_probe_redir="$PGC_WORKDIR/lam_probe_redir.sh"
cat > "$_lam_probe_redir" <<'PROBE'
check "a threshold behind a stderr redirect" \
	"$([ "$a" -le "$b" ] 2>/dev/null && echo yes || echo no)" "yes"
check "a threshold behind two redirects" \
	"$([ "$a" -le "$b" ] >/dev/null 2>&1 && echo yes || echo no)" "yes"
check "a count above one" \
	"$([ "$x" -gt 1 ] && echo yes || echo no)" "yes"
check "a count above one behind a redirect" \
	"$([ "$x" -gt 1 ] 2>/dev/null && echo yes || echo no)" "yes"
check "determinate: nothing versus something behind a redirect" \
	"$([ "$rows" -gt 0 ] 2>/dev/null && echo yes || echo no)" "yes"
check "determinate: a floor of one behind a redirect" \
	"$([ "$rows" -ge 1 ] 2>/dev/null && echo yes || echo no)" "yes"
check "carried: a branch behind a redirect names the number" \
	"$([ "$n" -ge 5 ] 2>/dev/null && echo yes || echo "no (found $n)")" "yes"
check "a threshold behind a redirect into a variable" \
	"$([ "$a" -le "$b" ] 2>"$LOG" && echo yes || echo no)" "yes"
check "a threshold behind a dup and a redirect into a variable" \
	"$([ "$a" -le "$b" ] 2>&1 >"$LOG" && echo yes || echo no)" "yes"
check "a floor of zero" \
	"$([ "$x" -ge 0 ] && echo yes || echo no)" "yes"
check "a threshold behind a spaced redirect" \
	"$([ "$a" -le "$b" ] 2> /dev/null && echo yes || echo no)" "yes"
check "a threshold behind an and-redirect" \
	"$([ "$a" -le "$b" ] &>/dev/null && echo yes || echo no)" "yes"
check "a threshold behind an and-redirect into a variable" \
	"$([ "$a" -le "$b" ] &>"$LOG" && echo yes || echo no)" "yes"
check "a threshold behind a spaced redirect into a variable" \
	"$([ "$a" -le "$b" ] 2> "$LOG" && echo yes || echo no)" "yes"
check_num "an if/then/else threshold behind a redirect into a variable" \
	"$(if [ "$a" -le "$b" ] 2>"$LOG"; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold behind an and-redirect into a variable" \
	"$(if [ "$a" -le "$b" ] &>"$LOG"; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold behind a spaced redirect into a variable" \
	"$(if [ "$a" -le "$b" ] 2> "$LOG"; then echo 1; else echo 0; fi)" "1"
check_num "carried: an if/then/else behind a redirect names the number" \
	"$(if [ "$a" -le "$b" ] 2>"$LOG"; then echo 1; else echo "0 (a=$a)"; fi)" "1"
check "a threshold behind a redirect joined to an unspaced and" \
	"$([ "$a" -le "$b" ] 2>/dev/null&& echo yes || echo no)" "yes"
check "a threshold behind a quoted redirect joined to an unspaced and" \
	"$([ "$a" -le "$b" ] 2>"$LOG"&& echo yes || echo no)" "yes"
check "a threshold joined to an unspaced echo" \
	"$([ "$a" -le "$b" ] &&echo yes || echo no)" "yes"
check "a threshold behind an input redirect" \
	"$([ "$a" -le "$b" ] </dev/null && echo yes || echo no)" "yes"
check "a threshold behind a spaced appending redirect into a variable" \
	"$([ "$a" -le "$b" ] 2>> "$LOG" && echo yes || echo no)" "yes"
check "innocent: a threshold piped into cat behind a redirect" \
	"$([ "$a" -le "$b" ] 2>/dev/null|cat && echo yes || echo no)" "yes"
check "innocent: a threshold piped into cat behind a quoted redirect" \
	"$([ "$a" -le "$b" ] 2>"$LOG"|cat && echo yes || echo no)" "yes"
check "a threshold with two spaces before the bracket" \
	"$(  [ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check_num "an if/then/else threshold behind a redirect joined to an unspaced then" \
	"$(if [ "$a" -le "$b" ] 2>/dev/null;then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold behind a quoted redirect joined to an unspaced then" \
	"$(if [ "$a" -le "$b" ] 2>"$LOG";then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold with a space before the if" \
	"$( if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold with two spaces after the if" \
	"$(if  [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold behind a doubly spaced redirect into a variable" \
	"$(if [ "$a" -le "$b" ]  2>"$LOG"; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else test-builtin threshold behind an unspaced redirect into a variable" \
	"$(if test "$a" -le "$b">"$LOG"; then echo 1; else echo 0; fi)" "1"
PROBE
# SORTED IN THE C LOCALE. A locale-aware sort ignores spaces, so it puts
# `an if/then/else ...` before `a threshold ...` and the order would depend on
# the runner. Measured: as root on PG15 the names came out in the other order.
_lam_redir_names="$(_lam_sweep "$_lam_probe_redir" | cut -f2 | LC_ALL=C sort | tr '\n' ';')"
check_text "control: the sweep sees an arm behind a redirect and a floor of two" \
	"$_lam_redir_names" "a count above one;a count above one behind a redirect;a floor of zero;a threshold behind a dup and a redirect into a variable;a threshold behind a quoted redirect joined to an unspaced and;a threshold behind a redirect into a variable;a threshold behind a redirect joined to an unspaced and;a threshold behind a spaced appending redirect into a variable;a threshold behind a spaced redirect;a threshold behind a spaced redirect into a variable;a threshold behind a stderr redirect;a threshold behind an and-redirect;a threshold behind an and-redirect into a variable;a threshold behind an input redirect;a threshold behind two redirects;a threshold joined to an unspaced echo;a threshold with two spaces before the bracket;an if/then/else test-builtin threshold behind an unspaced redirect into a variable;an if/then/else threshold behind a doubly spaced redirect into a variable;an if/then/else threshold behind a quoted redirect joined to an unspaced then;an if/then/else threshold behind a redirect into a variable;an if/then/else threshold behind a redirect joined to an unspaced then;an if/then/else threshold behind a spaced redirect into a variable;an if/then/else threshold behind an and-redirect into a variable;an if/then/else threshold with a space before the if;an if/then/else threshold with two spaces after the if;"

# THREE MORE SPELLINGS OF A TEST (#1986). The rules read only `[ ... ]`, so a
# lossy arm written as `[[ ... ]]` or with the `test` builtin was never seen.
# A mirrored test was not read either: `[ 0 -lt "$x" ]` says the same as
# `[ "$x" -gt 0 ]`, and `[ 1 -le "$x" ]` the same as `[ "$x" -ge 1 ]`, but both
# were refused. The mirror is excused only with exactly 0 or 1 on the left, so
# `[ 10 -lt "$x" ]` and `[ 11 -le "$x" ]` stay refused.
#
# A `test` ends at the first `&`, `|`, `;`, `<` or `>` outside quotes and
# substitutions. It used to end at the first one anywhere, so
# `test "$(wc -l < "$f")" -ge 20 && echo yes || echo no` was never examined
# (#1995 review).
#
# `-lt 1` and `-le 0` stay refused on purpose. They pass on 0 and fail on every
# count above it, so `no` does not say which count. The pytest twin refuses
# `r <= 0` for the same reason.
#
# The arms from `a mirrored nothing versus something with doubled spaces` to
# the end pin the mirrored excuses, the `test` end and the file descriptor
# that `2>x` hands back to the redirect, as the mutant sweep above found
# missing (#1995 review round 3).
_lam_probe_forms="$PGC_WORKDIR/lam_probe_forms.sh"
cat > "$_lam_probe_forms" <<'PROBE'
check "a double-bracket threshold" \
	"$([[ "$a" -le "$b" ]] && echo yes || echo no)" "yes"
check "a double-bracket threshold behind a redirect into a variable" \
	"$([[ "$a" -le "$b" ]] 2>"$LOG" && echo yes || echo no)" "yes"
check "a test-builtin threshold" \
	"$(test "$a" -le "$b" && echo yes || echo no)" "yes"
check "a test-builtin threshold behind a redirect" \
	"$(test "$a" -le "$b" 2>/dev/null && echo yes || echo no)" "yes"
check_num "an if/then/else double-bracket threshold" \
	"$(if [[ "$a" -le "$b" ]]; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else test-builtin threshold" \
	"$(if test "$a" -le "$b"; then echo 1; else echo 0; fi)" "1"
check "nothing expected and a count found" \
	"$([ "$x" -lt 1 ] && echo yes || echo no)" "yes"
check "at most zero" \
	"$([ "$x" -le 0 ] && echo yes || echo no)" "yes"
check "a mirrored count above one" \
	"$([ 1 -lt "$x" ] && echo yes || echo no)" "yes"
check "a mirrored count above ten" \
	"$([ 10 -lt "$x" ] && echo yes || echo no)" "yes"
check "a mirrored floor of eleven" \
	"$([ 11 -le "$x" ] && echo yes || echo no)" "yes"
check "determinate: a double-bracket count against nothing" \
	"$([[ "$rows" -gt 0 ]] && echo yes || echo no)" "yes"
check "determinate: a test-builtin file test" \
	"$(test -f "$CB" && echo yes || echo no)" "yes"
check "determinate: a mirrored nothing versus something" \
	"$([ 0 -lt "$x" ] && echo yes || echo no)" "yes"
check "determinate: a mirrored floor of one" \
	"$([ 1 -le "$x" ] && echo yes || echo no)" "yes"
check "determinate: a mirrored double-bracket nothing versus something" \
	"$([[ 0 -lt $x ]] && echo yes || echo no)" "yes"
check_num "determinate: an if/then/else test-builtin floor of one" \
	"$(if test "$rows" -ge 1; then echo 1; else echo 0; fi)" "1"
check "carried: a double-bracket branch names the number" \
	"$([[ "$n" -ge 5 ]] && echo yes || echo "no (found $n)")" "yes"
check "carried: a test-builtin branch names the number" \
	"$(test "$n" -ge 5 && echo yes || echo "no (found $n)")" "yes"
check "determinate: a test-builtin count against nothing behind a redirect" \
	"$(test "$rows" -gt 0 2>/dev/null && echo yes || echo no)" "yes"
check "a test-builtin floor over a count read through a redirect" \
	"$(test "$(wc -l < "$f")" -ge 20 && echo yes || echo no)" "yes"
check "a test-builtin threshold over a pipeline that may fail" \
	"$(test "$(grep -c x "$f" || true)" -le 3 && echo yes || echo no)" "yes"
check "determinate: a test-builtin count from a pipeline that may fail" \
	"$(test "$(grep -c x "$f" || true)" -gt 0 && echo yes || echo no)" "yes"
check "a test-builtin floor over a count of an escaped alternation" \
	"$(test "$(grep -c "a\|b" "$f")" -ge 20 && echo yes || echo no)" "yes"
check "determinate: a test-builtin count against a quoted nothing" \
	"$(test "$rows" -gt "0" && echo yes || echo no)" "yes"
check "determinate: a mirrored nothing versus something with doubled spaces" \
	"$([  0  -lt "$x" ] && echo yes || echo no)" "yes"
check "determinate: a mirrored floor of one with doubled spaces" \
	"$([  1  -le "$x" ] && echo yes || echo no)" "yes"
check "determinate: a mirrored quoted nothing versus something" \
	"$([ "0" -lt "$x" ] && echo yes || echo no)" "yes"
check "determinate: a mirrored quoted floor of one" \
	"$([ "1" -le "$x" ] && echo yes || echo no)" "yes"
check "determinate: a test-builtin count against nothing behind an input redirect" \
	"$(test "$rows" -gt 0 </dev/null && echo yes || echo no)" "yes"
check "determinate: a test-builtin count against nothing behind a numbered input redirect" \
	"$(test "$rows" -gt 0 0</dev/null && echo yes || echo no)" "yes"
check "determinate: a test-builtin count against nothing behind a two-digit descriptor" \
	"$(test "$rows" -gt 0 10>/dev/null && echo yes || echo no)" "yes"
check_num "determinate: an if/then/else test-builtin floor of one with its output redirected" \
	"$(if test "$rows" -ge 1; then echo 1; else echo 0; fi 2>/dev/null)" "1"
check "a test-builtin threshold against a number behind a redirect" \
	"$(test "$a" -le 3 2>/dev/null && echo yes || echo no)" "yes"
check "a test-builtin threshold whose branch holds brackets" \
	"$(test "$a" -le "$b" && echo yes || echo "no [[x]]")" "yes"
check "innocent: a test-builtin threshold piped into cat" \
	"$(test "$a" -le "$b" | cat && echo yes || echo no)" "yes"
PROBE
_lam_forms_names="$(_lam_sweep "$_lam_probe_forms" | cut -f2 | LC_ALL=C sort | tr '\n' ';')"
check_text "control: the sweep reads double brackets, the test builtin and mirrored tests" \
	"$_lam_forms_names" "a double-bracket threshold;a double-bracket threshold behind a redirect into a variable;a mirrored count above one;a mirrored count above ten;a mirrored floor of eleven;a test-builtin floor over a count of an escaped alternation;a test-builtin floor over a count read through a redirect;a test-builtin threshold;a test-builtin threshold against a number behind a redirect;a test-builtin threshold behind a redirect;a test-builtin threshold over a pipeline that may fail;a test-builtin threshold whose branch holds brackets;an if/then/else double-bracket threshold;an if/then/else test-builtin threshold;at most zero;nothing expected and a count found;"

# A COMPOUND TEST IS NOT EXCUSED, AND THE EXCUSES ARE ANCHORED (#1989).
#
# `[ "$a" -gt 0 -a "$b" -le "$c" ]` fails in more than one way, and `no` does
# not say which half failed. The `-gt 0` excuse was anchored to the end of the
# test, which kept this one out, but nothing pinned the anchor. And the anchor
# alone was not enough: `[ "$b" -le "$c" -a "$a" -gt 0 ]` ends in `-gt 0` and
# was excused. An equality half excused a compound from any position. So a test
# that joins two tests with `-a`, `-o`, `&&` or `||` is now never excused, as the
# awk rule already refuses a condition with more than one comparison.
#
# THE ANCHORS STILL MATTER, and two arms pin them. Without the end anchor,
# `-gt 05` reads as `-gt 0` and `-ge 10` reads as `-ge 1`.
#
# A REDIRECT TARGET IN QUOTES MAY HOLD A SPACE. `2>"$d/a b"` hid the arm: the
# target stopped at the space and `&&` was never reached. In the if/then/else
# form, the half after the space was read as a branch.
#
# CONTROLS KEEP THE COMPOUND RULE HONEST. A command substitution inside the
# test is not a compound: the `|| true` of `"$(grep -c x || true)"` and the `-o`
# of `grep -o` belong to the substitution. Without that, three live arms in
# native_vecskip were refused. The substitution may nest, hold a quoted `)` or a
# subshell, or be written in backticks. An operand such as `-only` or `x-a` is
# not `-o` or `-a`, and nor is the `-a` of `[ "$arg" = -a ]`, which has no test
# after it. A carrying compound is still not refused.
#
# `&&` AND `||` NEED NO SPACES. `[[ "$b" -le 3&&"$a" -gt 0 ]]` is a compound in
# bash, so it is refused like the spaced form (#1995 review).
#
# THE TEST AFTER A JOIN MAY BE ONE WORD, AND MORE THAN ONE SPACE MAY COME
# BEFORE IT. `[ "$a" = x -a "$b" ]` and `-a  "$b"` are compounds. Two fixtures
# pin both, so the join rule's `[[:space:]]+` and its one-word lookahead
# cannot be weakened while this part stays green (#1995 review round 3).
_lam_probe_compound="$PGC_WORKDIR/lam_probe_compound.sh"
cat > "$_lam_probe_compound" <<'PROBE'
check "a compound whose first half is nothing versus something" \
	"$([ "$a" -gt 0 -a "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a compound whose last half is nothing versus something" \
	"$([ "$b" -le "$c" -a "$a" -gt 0 ] && echo yes || echo no)" "yes"
check "a compound with an equality half" \
	"$([ "$a" -eq 1 -o "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a double-bracket compound whose last half is a floor of one" \
	"$([[ "$b" -le "$c" && "$a" -ge 1 ]] && echo yes || echo no)" "yes"
check "a threshold with a leading zero" \
	"$([ "$x" -gt 05 ] && echo yes || echo no)" "yes"
check "a floor of ten" \
	"$([ "$x" -ge 10 ] && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a quoted path with a space" \
	"$([ "$a" -le "$b" ] 2>"$d/a b" && echo yes || echo no)" "yes"
check_num "an if/then/else threshold behind a redirect into a quoted path with a space" \
	"$(if [ "$a" -le "$b" ] 2>"/tmp/a $d"; then echo 1; else echo 0; fi)" "1"
check "determinate: nothing versus something behind a redirect into a quoted path with a space" \
	"$([ "$rows" -gt 0 ] 2>"$d/a b" && echo yes || echo no)" "yes"
check "determinate: a count from a pipeline that may fail is not a compound" \
	"$([ "$(grep -c x "$f" || true)" != 0 ] && echo yes || echo no)" "yes"
check "determinate: a grep option inside the test is not a compound" \
	"$([ "$(grep -o x "$f" | wc -l)" -gt 0 ] && echo yes || echo no)" "yes"
check "carried: a compound whose branch names every operand" \
	"$([ "$a" -gt 0 -a "$b" -le "$c" ] && echo yes || echo "no (a=$a b=$b c=$c)")" "yes"
check "determinate: a nested substitution whose pipeline may fail is not a compound" \
	"$([ "$(grep -c "$(cat "$f")" "$g" || true)" -gt 0 ] && echo yes || echo no)" "yes"
check "a double-bracket compound joined by or" \
	"$([[ "$a" -eq 1 || "$b" -le "$c" ]] && echo yes || echo no)" "yes"
check "determinate: an operand that starts with -o is not a compound" \
	"$([ "$arg" = -only ] && echo yes || echo no)" "yes"
check "determinate: an operand that ends in -a is not a compound" \
	"$([ "$arg" = x-a ] && echo yes || echo no)" "yes"
check "a mirrored floor of zero" \
	"$([ 0 -le "$x" ] && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a quoted path with a space and a suffix" \
	"$([ "$a" -le "$b" ] 2>"$d/a b".log && echo yes || echo no)" "yes"
check "a double-bracket compound joined by an unspaced and" \
	"$([[ "$b" -le 3&&"$a" -gt 0 ]] && echo yes || echo no)" "yes"
check "determinate: a substitution that holds a quoted parenthesis is not a compound" \
	"$([ "$(grep -c "x)" "$f" || true)" -gt 0 ] && echo yes || echo no)" "yes"
check "determinate: a substitution that holds a subshell is not a compound" \
	"$([ $( (grep -c x "$f") || true) -gt 0 ] && echo yes || echo no)" "yes"
check "determinate: a backtick substitution is not a compound" \
	"$([ `grep -c x "$f" || true` -gt 0 ] && echo yes || echo no)" "yes"
check "determinate: a substitution that holds a single-quoted parenthesis is not a compound" \
	"$([ $(grep -c 'x)' "$f" || true) -gt 0 ] && echo yes || echo no)" "yes"
check "a double-bracket compound after a single-quoted operand" \
	"$([[ "$a" = 'x' || "$b" -le 3 ]] && echo yes || echo no)" "yes"
check "determinate: a substitution that holds an escaped quote is not a compound" \
	"$([ "$(grep -c "x\")" "$f" || true)" -gt 0 ] && echo yes || echo no)" "yes"
check "a double-bracket compound after a backtick substitution" \
	"$([[ `grep -c x "$f"` = 1 || "$b" -le 3 ]] && echo yes || echo no)" "yes"
check "determinate: an equality with the word -a is not a compound" \
	"$([ "$arg" = -a ] && echo yes || echo no)" "yes"
check "determinate: an equality with the word -o is not a compound" \
	"$([ "$arg" = -o ] && echo yes || echo no)" "yes"
check "determinate: a left operand that ends in -a is not a compound" \
	"$([ x-a = "$arg" ] && echo yes || echo no)" "yes"
check "a compound whose last half is a one-word test" \
	"$([ "$a" = x -a "$b" ] && echo yes || echo no)" "yes"
check "a compound joined by a double-spaced and" \
	"$([ "$a" = x -a  "$b" -le 3 ] && echo yes || echo no)" "yes"
PROBE
_lam_compound_names="$(_lam_sweep "$_lam_probe_compound" | cut -f2 | LC_ALL=C sort | tr '\n' ';')"
check_text "control: a compound, a padded floor and a quoted redirect target are not excused" \
	"$_lam_compound_names" "a compound joined by a double-spaced and;a compound whose first half is nothing versus something;a compound whose last half is a one-word test;a compound whose last half is nothing versus something;a compound with an equality half;a double-bracket compound after a backtick substitution;a double-bracket compound after a single-quoted operand;a double-bracket compound joined by an unspaced and;a double-bracket compound joined by or;a double-bracket compound whose last half is a floor of one;a floor of ten;a mirrored floor of zero;a threshold behind a redirect into a quoted path with a space;a threshold behind a redirect into a quoted path with a space and a suffix;a threshold with a leading zero;an if/then/else threshold behind a redirect into a quoted path with a space;"

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

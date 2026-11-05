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
# (a first branch written with `printf` counts as `echo`, #2011)
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
# `||`, is never excused (#1989). Nor are two tests chained outside the
# brackets, as in `[ A ] && [ B ] && echo yes || echo no` (#1997).
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

# CONTINUATIONS ARE FOLDED FIRST, by a stage of their own. The corpus wraps
# arms across lines, and a line-by-line reader sees `&& echo "smaller"`
# without the `|| echo "UNCHANGED ($ROWS_AFTER of $ROWS_BEFORE)"` that
# follows it. So four arms that DO carry their values read as though they
# discard them. Measured: 65 sites before folding, 61 after.
#
# A line that ends in `\` is joined to the next. A line whose CODE ends in
# `&&` or `||` INSIDE A `$(` THAT IS STILL OPEN is joined to the next too, as
# bash does, with a space between (#1997). Code means the line without its
# comment, with quoted text hidden. So `echo x # note ||` and `note="a &&`
# are not joined: bash does not continue either one.
#
# ONLY INSIDE AN OPEN `$(` (#2022 review, round 2). An arm split after `&&`
# always leaves its `$(` open, so that is the only place a join is needed.
# Joining after a top-level `&&` hid or renamed arms that main read: the
# rules take the name from the first `"` on a line, the arm from the last
# `$([`, and "carries a value" from a `$` after it. So `[ -n "$X" ] &&` lent
# the arm below it the name `$X`, and an arm whose line ended in `&&` was
# hidden by a carrying arm, an assignment or an `echo "$a"` on the next line.
# code_of reports the depth of `$(` it ended at in CD, and the join stops
# when CD is 0. A `(` or `)` outside a `$(` does not change the depth, so a
# subshell or a `case` branch is not read as an open substitution.
#
# THE OPEN `$(` MUST BE THE ARM'S (#2022 review, round 3). A line can hold a
# whole arm and then open a second `$(` that ends in `&&` or `||`, as in
# `check "x" "$([ … ] && echo yes || echo no)" "yes"; got=$(f &&`. That open
# `$(` is not the arm's. Joining the next line there let a `$([` on it take
# the arm's place, so the arm main read was hidden. code_of sets CC when a
# `$(` closed back to the top level earlier on the line, and the join stops
# when CC is set. A chain on such a line is then read one line at a time, as
# the base read it.
#
# INSIDE THE `$(`, THE JOIN READS COMMENTS AS BASH DOES. A comment line is
# skipped, and a comment at the end of a joined line is cut off before the
# next line is added. A comment that quoted an arm was refused before
# (#1255), and a comment line used to stop the join, which hid the rest of a
# chained arm. THE NEXT LINE IS NOT JOINED WHEN IT HOLDS A RECORDER CALL, by
# the same pattern the rules use to find one. That call is an arm of its own
# and keeps its own name. The join needs to look one line ahead, which a
# single awk with `getline` cannot undo, so this stage reads the whole file
# first.
_lam_join() {  # _lam_join FILE -> FILE with its continued lines folded
	awk '
		# THE CODE OF s: its comment cut off, and every character inside
		# quotes or backticks replaced by `_`, one for one, so the code is
		# as long as the part of s before the comment. A `$(...)` stays
		# code, so the `&&` that ends `"$([ "$a" -gt 0 ] &&` still counts. A
		# `#` starts a comment only at the start of a word, so `$#` and
		# `${#x}` do not. It sets CD to the depth of `$(` still open where
		# the code ends, and CC to 1 when a `$(` closed back to depth 0
		# before that.
		function code_of(s,   n, i, ch, d, q, bt, out, cl) {
			n = length(s); out = ""; d = 0; q[0] = ""; bt = 0; cl = 0
			for (i = 1; i <= n; i++) {
				ch = substr(s, i, 1)
				if (q[d] == "\047") {
					if (ch == "\047") q[d] = ""
					out = out "_"; continue
				}
				if (ch == "\\" && i < n) { out = out "__"; i++; continue }
				if (bt) { if (ch == "`") bt = 0; out = out "_"; continue }
				if (ch == "`") { bt = 1; out = out "_"; continue }
				if (ch == "$" && substr(s, i + 1, 1) == "(") {
					d++; q[d] = ""; out = out "$("; i++; continue
				}
				if (q[d] == "\"") {
					if (ch == "\"") q[d] = ""
					out = out "_"; continue
				}
				if (ch == "\"" || ch == "\047") { q[d] = ch; out = out "_"; continue }
				if (ch == "#" && (out == "" || substr(out, length(out), 1) ~ /[[:space:];&|()]/)) { CD = d; CC = cl; return out }
				if (d > 0 && ch == "(") { d++; q[d] = "" }
				if (d > 0 && ch == ")") { d--; if (d == 0) cl = 1 }
				out = out ch
			}
			CD = d; CC = cl; return out
		}
		{ L[++n] = $0 }
		END {
			for (i = 1; i <= n; ) {
				line = L[i++]
				while (i <= n) {
					if (line ~ /\\$/) {
						sub(/\\$/, " ", line); line = line L[i++]; continue
					}
					c = code_of(line)
					if (c !~ /(&&|\|\|)[[:space:]]*$/ || CD == 0 || CC) break
					if (L[i] ~ /^[[:space:]]*#/) { i++; continue }
					if (L[i] ~ /(^|[[:space:]])(check|check_num|check_text|check_ratio|pgc_fail|check_skip)[[:space:]]+"/) break
					line = substr(line, 1, length(c)) " " L[i++]
				}
				print line
			}
		}
	' "$1"
}

_lam_sweep() {  # _lam_sweep FILE -> one "suite<TAB>name" per lossy arm
	_lam_join "$1" | awk '
		# HIDE WHAT IS QUOTED OR SUBSTITUTED (#1986). Returns s with every
		# character inside quotes, a `$(...)` or a backtick replaced by `_`,
		# so the result has the same length and its positions match s. A
		# substitution may hold quotes and parentheses of its own, and may
		# nest, so a regular expression cannot find where it ends. KNOWN
		# LIMIT: the `x)` of a `case` inside `$(...)` closes it early. No arm
		# in the tree holds one.
		#
		# It also sets SHUT to the position of the first `)` that closes every
		# open substitution, or to 0 if none does. The `[` rule uses it to find
		# where the arm ends (#2012).
		function mask(s,   n, i, ch, d, q, bt, out) {
			n = length(s); out = ""; d = 0; q[0] = ""; bt = 0; SHUT = 0
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
				if (d > 0 && ch == ")") {
					d--; if (d == 0 && !SHUT) SHUT = i
					out = out "_"; continue
				}
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
			# NOR IS THE FIRST WORD A JOIN WHEN `=`, `!=` OR `==` FOLLOWS IT.
			# In `[ -a = "$arg" ]` the `-a` is the left-hand operand (#2007).
			# Only that first word is dropped, so the later `-a` of
			# `[ -a = x -a "$b" -le 3 ]` is still read as a join.
			sub(/^[[:space:]]*-[ao][[:space:]]+(=|!=|==)[[:space:]]/, "", c)
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
		# #1989). A target is any run of bare characters and quoted parts, in
		# double or single quotes, and a quoted part may hold a space. A bare
		# character is anything but a space, `&`, `|`, `;`, `)` or a quote.
		# The operator may end in `&` (`2>&1`) or in `|` (the noclobber
		# `>|x`). Single quotes and `>|` hid the arm before (#1995 review
		# round 4). So did a bare or quoted part joined to a second quoted
		# part that holds a space, as in `2>x"y z"`, and an ANSI-C quoted
		# target. The target used to be one quoted part with a bare suffix,
		# or one bare word (#2019).
		BEGIN {
			RD = "((&|[0-9]*)[<>]+[&|]?[[:space:]]*([^[:space:]&|;)\"\047]|\"[^\"]*\"|\047[^\047]*\047)*[[:space:]]*)*"
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
		# HIDE THE REGEX AFTER `=~` IN A MASKED `[[ ]]` (#2027). m is what
		# mask() returned. An unquoted regex such as `^[0-9]+$` holds a `]`,
		# and the test ended there, so the rest of a compound such as
		# `[[ "$a" -le "$b" && "$a" =~ ^[0-9]+$ ]]` was never examined. Bash
		# reads the regex as one word, so every character of it is replaced
		# by `_`, as mask() does. The word ends at a blank, but not at one
		# inside `(` and `)`, and a `\` takes the next character with it.
		# A `&&` glued to the regex is hidden with it, which changes no
		# verdict: T is cut from s, not from the mask. Quoted parts are
		# already hidden. `=~` counts only as a word of its own. KNOWN
		# LIMIT, and no arm in the tree holds it: `=~` as an operand, as in
		# `[[ "$op" == =~ ]]`, is read as the operator too, so the `]]`
		# after it is hidden and the arm is not examined.
		function rx_hide(m,   n, i, j, d, ch) {
			n = length(m)
			for (i = 2; i < n; i++) {
				if (substr(m, i, 2) != "=~") continue
				if (substr(m, i - 1, 1) !~ /[[:space:]]/) continue
				if (substr(m, i + 2, 1) !~ /[[:space:]]/) continue
				for (j = i + 2; j <= n && substr(m, j, 1) ~ /[[:space:]]/; j++) ;
				for (d = 0; j <= n; j++) {
					ch = substr(m, j, 1)
					if (ch == "\\" && j < n) {
						m = substr(m, 1, j - 1) "__" substr(m, j + 2); j++; continue
					}
					if (ch == "(") d++
					else if (ch == ")" && d) d--
					else if (!d && ch ~ /[[:space:]]/) break
					m = substr(m, 1, j - 1) "_" substr(m, j + 1)
				}
			}
			return m
		}
		# SPLIT ONE TEST FROM WHAT FOLLOWS IT (#1986). s starts at the test.
		# Sets T to the text the test compares and REST to the text after it.
		# `test` ends at the first `&`, `|`, `;`, `<` or `>` that is not
		# quoted or inside a substitution, so `test "$(wc -l < f)" -ge 20`
		# is read whole. A trailing file descriptor such as the `2` of
		# `2>x` goes back to REST. `[` and `[[` end at the first `]` that is
		# not quoted or inside a substitution, so `[ "${sizes[5]}" -le 3 ]`
		# is read whole. They ended at the first `]` anywhere, which hid
		# seven live lossy arms in six suites (#2002). KNOWN LIMITS, and no
		# arm in the tree holds either: a redirect between the operands, as
		# in `test "$a" -le 2>x "$b"`, hides the arm; and an unquoted array
		# element such as `[ ${s[5]} -le 3 ]`, or a glob such as
		# `[[ $c == [[:digit:]] ]]`, still ends the test early.
		# In `[[ ]]`, the regex after `=~` is hidden too (#2027), by rx_hide.
		function split_test(s,   m) {
			sub(/^[[:space:]]*/, "", s)
			m = mask(s)
			if (m ~ /^\[\[/ && match(rx_hide(m), /^\[\[[^]]*\]\]/)) {
				T = substr(s, 3, RLENGTH - 4); REST = substr(s, RLENGTH + 1); return 1
			}
			if (match(m, /^\[[^]]*\]/)) {
				T = substr(s, 2, RLENGTH - 2); REST = substr(s, RLENGTH + 1); return 1
			}
			if (match(m, /^test[[:space:]][^&|;<>]*/)) {
				T = substr(s, 5, RLENGTH - 4); REST = substr(s, RLENGTH + 1)
				if (REST ~ /^[<>]/ && match(T, /[[:space:]][0-9]+$/)) {
					REST = substr(T, RSTART + 1) REST; T = substr(T, 1, RSTART)
				}
				return 1
			}
			return 0
		}
		# WHAT FOLLOWS A TEST, WITH ITS REDIRECTS AND ANY TESTS CHAINED AFTER
		# IT TAKEN OFF (#1997). `[ A ] && [ B ] && echo yes || echo no` is a
		# compound too: `no` does not say which test failed. The rules read
		# only the first test, so the text after it began `&& [`, not `&&
		# echo`, and the arm was never examined. In the if/then/else form the
		# second test was read as part of the branches, and its `$` excused
		# the arm. A test chained by `&&` or `||`, in any of the three
		# spellings, is now taken off, and CHAINED says one was. Returns the
		# rest, which starts at the branches.
		function after_test(a) {
			CHAINED = 0
			sub("^[[:space:]]*" RD, "", a)
			while (a ~ /^(&&|\|\|)[[:space:]]*(\[|test[[:space:]])/ && split_test(substr(a, 3))) {
				CHAINED = 1; a = REST
				sub("^[[:space:]]*" RD, "", a)
			}
			return a
		}
		# Each record is one line as _lam_join folded it.
		{ line = $0 }
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
		# `2>&1`, `>&2`, `&>x`, `>|x`, or a quoted target with a space in
		# double or single quotes, as in `2>"a b"`. It is stripped
		# from the arms too, so a `2>"$LOG"` is not read as a branch that
		# carries a value.
		#
		# KNOWN LIMITS. No arm in the tree has any of these shapes.
		#   - A branch that ends in a bare dollar sign, as in `|| echo no$)`,
		#     prints a literal dollar sign and carries no value, but that `$`
		#     still excuses the arm. Main has the same limit. The redirect
		#     probe pins it as a `known limit:` arm, which also pins where the
		#     cut ends: one character short and the arm is refused (#2020
		#     review).
		#   - `[ ""0 -lt "$x" ]` means `[ 0 -lt "$x" ]` but is refused, because
		#     the mirrored excuse allows at most one quote on each side.
		#   - A chained test after a `!`, as in `[ A ] && ! [ B ] && echo
		#     yes || echo no`, is not read, so the arm is not examined.
		#   - A line is joined to the next only after `\`, `&&` or `||`. An
		#     arm split after a `|` or inside quotes is read as two lines.
		#   - _lam_join reads one line at a time for quotes and does not know
		#     here-docs. So a line in the middle of a multi-line string, or in
		#     a here-doc body, that holds an open `$(` and ends in `&&` or
		#     `||` is still joined to the next line, unless that line holds a
		#     recorder call. Main joined no line after `&&` or `||` at all.
		#   - A line where a `$(` closed back to the top level before the
		#     open one is not joined, so a chain split after `&&` on it, as in
		#     `check "rows $(n)" "$([ … ] &&`, is read one line at a time.
		#     The base read every chain that way.
		#   - Inside an open `$(`, a recorder call whose name is not a quoted
		#     string, as in `check $v`, does not stop the join, so its arm is
		#     reported under the name of the line above. A recorder call glued
		#     to a `;` or `{` is not read as one, here or by the rules.
		#   - A trailing `\` is joined before the comment is cut, so `# c \`
		#     pulls in the next line. Main has the same behaviour.
		#   - A redirect into a substitution, as in `2>$(mktemp)`, hides the
		#     arm: the target stops at the `)`. Main has the same limit.
		#
		# THREE SPELLINGS OF THE TEST (#1986): `[ ... ]`, `[[ ... ]]` and
		# `test ...`. Only the first was read, so the other two hid a lossy arm.
		(p = last_at(line, "\\$\\([[:space:]]*(\\[|test[[:space:]])")) {
			arms = ""
			if (split_test(substr(line, p + 2))) {
				T1 = T; arms = after_test(REST)
				# THE ARM ENDS WHERE ITS SUBSTITUTION CLOSES (#2012). A `$` in a
				# later argument, as in `... || echo no)" "$(want)"`, is not in a
				# branch, so it must not excuse the arm. The text after the test
				# is one level deep, so mask() is handed it behind a `$(` and
				# reports where that level closes. A `)` in quotes or in a
				# subshell does not close it. A line that never closes it, such
				# as an arm that goes on after `||` on the next line, is kept.
				# The cut runs after after_test(), which calls mask() itself
				# and so resets SHUT, and before the branches are read.
				mask("$(" arms)
				if (SHUT) arms = substr(arms, 1, SHUT - 3)
			}
			# A BRANCH MAY BE `printf` AS WELL AS `echo` (#2011). `&& printf yes
			# || echo no` prints only `no` on failure, the same as `echo`. A
			# `printf` that prints a `$` carries its value like an `echo` does.
			if (arms ~ /^&&[[:space:]]*(echo|printf)/) {
				# (b) a branch that interpolates carries the value already.
				if (arms ~ /\$/) next
				if (!CHAINED && determinate(T1)) next
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
			T1 = T; iarms = after_test(REST)
			sub(/^;?[[:space:]]*/, "", iarms)
			sub(/[[:space:]]*fi[[:space:]]*\).*$/, "", iarms)
			# EITHER BRANCH MAY BE `printf` (#2011), as in the `[` rule.
			if (iarms !~ /then[[:space:]]*(echo|printf)/) next
			if (iarms !~ /else[[:space:]]*(echo|printf)/) next
			if (iarms ~ /\$/) next
			if (!CHAINED && determinate(T1)) next
			if (name != "") print suite "\t" name
		}
	' suite="$(basename "${1%.sh}")"
}

# AND THE SAME RULE OVER AN AWK-RENDERED VERDICT (#1174). A separate fixture, so
# the two controls above keep their numbers and this family is counted on its own.
#
# THE INTERPOLATION RULE CANNOT BE REUSED WHOLESALE. The `[` matcher excuses an
# arm whose branches contain `$`, because a branch that interpolates carries its
# value. An awk program written in DOUBLE quotes carries `$LO` in its CONDITION
# and constants in its branches -- `print ($LO * 8 <= $HI) ? 1 : 0` -- so a test
# for `$` anywhere excuses the whole family. The branches are what must be read.
#
# THE ARMS FROM `determinate: an awk exit status against nothing` TO THE END
# PIN THE OLDER PATTERNS (#2006). A mutant sweep weakened and widened each
# piece of the regular expressions that read an awk verdict: the comparison
# count, the `> 0` and `>= 1` excuses, the strip after `print`, both branches
# of the ternary and the recorder names. 121 changes to the older patterns of
# this sweep stayed green. Each arm here makes at least one of them fail a
# control. The `not this shape:` arm is a helper whose name only ends in
# `check`, so it is not a recorder. KNOWN LIMIT: a branch written as a number
# with no digit before or after its point, such as `.5` or `1.`, is read as
# carrying a value. No arm in the tree holds one.
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
check "determinate: an awk exit status against nothing" \
	"$(awk -v n="$SEEN" 'BEGIN{ exit n > 0 ? 0 : 1 }')" "1"
check "determinate: an awk count against nothing in double parentheses" \
	"$(awk -v n="$SEEN" 'BEGIN{ print ((n > 0)) ? "some" : "none" }')" "1"
check "determinate: an awk count against nothing joined to its question mark" \
	"$(awk -v n="$SEEN" 'BEGIN{ print (n > 0)? "some" : "none" }')" "1"
check "determinate: an awk count against nothing with two spaces before its question mark" \
	"$(awk -v n="$SEEN" 'BEGIN{ print (n > 0)  ? "some" : "none" }')" "1"
check "determinate: an awk inequality" \
	"$(awk -v s="$S" 'BEGIN{ print (s != "") ? "set" : "unset" }')" "1"
check "an awk compound whose first half is a less-than" \
	"$(awk -v a="$A" -v b="$B" -v n="$N" 'BEGIN{ print (a < b && n > 0) ? 1 : 0 }')" "1"
check "determinate: an awk floor of one" \
	"$(awk -v n="$SEEN" 'BEGIN{ print (n >= 1) ? "some" : "none" }')" "1"
check "determinate: an awk floor of one written without a space" \
	"$(awk -v n="$SEEN" 'BEGIN{ print (n >=1) ? "some" : "none" }')" "1"
check "determinate: an awk floor of one with two spaces" \
	"$(awk -v n="$SEEN" 'BEGIN{ print (n >=  1) ? "some" : "none" }')" "1"
check "an awk verdict whose question mark is joined to its branch" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ?"yes" : "no" }')" "1"
check "an awk verdict with two spaces after its question mark" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ?  "yes" : "no" }')" "1"
check "an awk verdict with two empty branches" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? "" : "" }')" "1"
check "an awk verdict rendered as two-digit numbers" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 10 : 25 }')" "1"
check "an awk verdict rendered as decimals" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1.25 : 0.75 }')" "1"
check "an awk verdict whose colon is joined to its first branch" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1: 0 }')" "1"
check "an awk verdict with two spaces around its colon" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1  :  0 }')" "1"
check "an awk verdict whose colon is joined to its second branch" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1 :0 }')" "1"
check "an awk verdict that ends in a parenthesis" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print ((a <= b) ? 1 : 0) }')" "1"
check "determinate: an awk count against nothing with no space after print" \
	"$(awk -v n="$SEEN" 'BEGIN{ print(n>0) ? "some" : "none" }')" "1"
check "determinate: an awk count against nothing whose name holds a less-than, a < b" \
	"$(awk -v n="$SEEN" 'BEGIN{ print(n > 0) ? "some" : "none" }')" "1"
check_ratio "an awk verdict recorded by check_ratio" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1 : 0 }')" "1"
pgc_fail "an awk verdict recorded by pgc_fail" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1 : 0 }')" "1"
check_skip "an awk verdict recorded by check_skip" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1 : 0 }')" "1"
precheck "not this shape: an awk verdict handed to a helper whose name ends in check" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1 : 0 }')" "1"
check  "an awk verdict recorded with two spaces before its name" \
	"$(awk -v a="$A" -v b="$B" 'BEGIN{ print (a <= b) ? 1 : 0 }')" "1"
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
	"$_lam_awk_hits" "16"

_lam_awk_names="$(_lam_sweep "$_lam_probe_awk" | cut -f2 | LC_ALL=C sort | tr '\n' ';')"
check_text "control: a determinate or carrying awk arm is not swept up" \
	"$_lam_awk_names" "an awk compound whose first half is a less-than;an awk verdict recorded by check_ratio;an awk verdict recorded by check_skip;an awk verdict recorded by pgc_fail;an awk verdict recorded with two spaces before its name;an awk verdict rendered as a number;an awk verdict rendered as decimals;an awk verdict rendered as two-digit numbers;an awk verdict that ends in a parenthesis;an awk verdict whose colon is joined to its first branch;an awk verdict whose colon is joined to its second branch;an awk verdict whose operands vanish;an awk verdict whose question mark is joined to its branch;an awk verdict with two empty branches;an awk verdict with two spaces after its question mark;an awk verdict with two spaces around its colon;"

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
# EVERY PIECE OF THE REDIRECT PATTERN IS PINNED (#1995 review rounds 3, 4
# and 5). A mechanical sweep weakened and over-applied each quantifier, anchor,
# alternation branch, class member and literal of the regular expressions this
# change added. The arms from `joined to an unspaced and` to the end are the
# ones that sweep found missing: without each, one change to a pattern stayed
# green. Some of them hold tabs, because the rules accept `[[:space:]]` on
# purpose and nothing else pinned that.
#
# THE ARMS FROM `a threshold behind a redirect into a word joined to a
# single-quoted part with a space` TO THE END are targets made of more than one
# part, where a later part is quoted and holds a space, and an ANSI-C quoted
# target. Each hid the arm, because the target stopped at the space (#2019).
#
# THE ARM ENDS WHERE ITS SUBSTITUTION CLOSES (#2012). A `$` in a later
# argument, as in `"$(want)"`, or straight after the `)` in the same argument,
# is not in a branch, so it does not excuse the arm. An arm that goes on after
# `||` on the next line is still read. The three new `carried:` arms hold a
# quoted `(`, a subshell, and a `$n` that ends the else branch, so the cut can
# stop neither early nor one character short. The `known limit:` arm ends its
# else branch in a bare `$`, which bash prints as a literal: the arm carries no
# value but is excused, as on main. It pins that the cut keeps the last
# character before the `)` (#2020 review).
#
# The `not this shape:` arms (four here, ten in the next probe) are not this
# rule's shape: a test piped into `cat` or `echo`, or sent to the background.
# In the two `| cat` arms the `&&` reads `cat`'s status. In the `|&` and `&`
# arms `echo` runs whatever the test says. Either way the test's result is
# lost entirely, but this sweep does not read them.
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
check "not this shape: a threshold piped into cat behind a redirect" \
	"$([ "$a" -le "$b" ] 2>/dev/null|cat && echo yes || echo no)" "yes"
check "not this shape: a threshold piped into cat behind a quoted redirect" \
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
check "a threshold spaced with tabs" \
	"$(	[ "$a" -le "$b" ]	2>	/dev/null	>&2	&&	echo yes || echo no)" "yes"
check "a threshold whose no branch redirects" \
	"$([ "$a" -le "$b" ] && echo yes || echo no 2>/dev/null)" "yes"
check "a threshold behind a two-digit descriptor" \
	"$([ "$a" -le "$b" ] 10>/dev/null && echo yes || echo no)" "yes"
check "a threshold behind a redirect with two spaces before its target" \
	"$([ "$a" -le "$b" ] 2>  /dev/null && echo yes || echo no)" "yes"
check "a threshold behind a redirect with two spaces before the and" \
	"$([ "$a" -le "$b" ] 2>/dev/null  && echo yes || echo no)" "yes"
check "a threshold with two spaces before the echo" \
	"$([ "$a" -le "$b" ] &&  echo yes || echo no)" "yes"
check "a threshold behind a redirect into a bare variable" \
	"$([ "$a" -le "$b" ] 2>$LOG && echo yes || echo no)" "yes"
check "a threshold behind a dup joined to an unspaced and" \
	"$([ "$a" -le "$b" ] 2>&1&& echo yes || echo no)" "yes"
check "a threshold behind a redirect into a path with an underscore" \
	"$([ "$a" -le "$b" ] 2>/tmp/err_log && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a quoted path with a space, an underscore and a digit" \
	"$([ "$a" -le "$b" ] 2>"a b_1.log" && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a word with a quoted suffix" \
	"$([ "$a" -le "$b" ] 2>x"y" && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a quoted path that holds an and and angle brackets" \
	"$([ "$a" -le "$b" ] 2>"a b&<>" && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a quoted path with a space and two suffixes" \
	"$([ "$a" -le "$b" ] 2>"a b"_1"c" && echo yes || echo no)" "yes"
check "not this shape: a threshold piped with its stderr into echo" \
	"$([ "$a" -le "$b" ] |& echo yes || echo no)" "yes"
check "not this shape: a threshold sent to the background before echo" \
	"$([ "$a" -le "$b" ] & echo yes || echo no)" "yes"
check_num "an if/then/else threshold spaced with tabs" \
	"$(	if	[ "$a" -le "$b" ]	2>"$LOG"; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold with two spaces before the if" \
	"$(  if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold with an elif" \
	"$(if [ "$a" -le "$b" ]; then echo 1; elif [ -f x ]; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold behind an unspaced redirect into a variable" \
	"$(if [ "$a" -le "$b" ]>"$LOG"; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold before a later substitution that holds an if" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "$(cd /; if [ -d x ]; then echo 1; fi)" "1"
check_num "an if/then/else threshold before a negated if" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "$(if ! [ -d x ]; then echo 1; fi)" "1"
check_num "an if/then/else threshold before an if on a helper whose name starts with test" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "$(if test_ok; then echo 1; fi)" "1"
check_num "an if/then/else threshold before a comment that holds the word if" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1" # if this fails, see #1164
check "a threshold behind a redirect into a single-quoted path with a space" \
	"$([ "$a" -le "$b" ] 2>'a b' && echo yes || echo no)" "yes"
check "a threshold behind a noclobber redirect" \
	"$([ "$a" -le "$b" ] >|x && echo yes || echo no)" "yes"
check_num "an if/then/else threshold behind a redirect into a single-quoted path with a space" \
	"$(if [ "$a" -le "$b" ] 2>'a b'; then echo 1; else echo 0; fi)" "1"
check "a threshold behind a single-quoted redirect joined to an unspaced and" \
	"$([ "$a" -le "$b" ] 2>'a b'&& echo yes || echo no)" "yes"
check_num "an if/then/else threshold behind a single-quoted redirect joined to an unspaced then" \
	"$(if [ "$a" -le "$b" ] 2>'a b';then echo 1; else echo 0; fi)" "1"
check "a threshold behind a redirect into a single-quoted path with a space and a suffix" \
	"$([ "$a" -le "$b" ] 2>'a b'x && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a single-quoted path that holds an and and angle brackets" \
	"$([ "$a" -le "$b" ] 2>'a b&<>' && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a single-quoted path with a space and a double-quoted suffix" \
	"$([ "$a" -le "$b" ] 2>'a b'"c" && echo yes || echo no)" "yes"
check "a threshold before a later argument that holds a substitution" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "$(want)"
check "carried: a branch with a quoted parenthesis before the number" \
	"$([ "$n" -ge 5 ] && echo yes || echo "no (found)" "$n")" "yes"
check "carried: a branch with a subshell before the number" \
	"$([ "$n" -ge 5 ] && echo yes || { (echo no); echo "$n"; })" "yes"
check "a threshold whose else branch is on the next line" \
	"$([ "$a" -le "$b" ] && echo yes ||
	echo no)" "yes"
check "carried: a branch that ends in the number" \
	"$([ "$n" -ge 5 ] && echo yes || echo no $n)" "yes"
check "a threshold whose substitution is followed by a variable in the same argument" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)$x" "yes"
check "known limit: a branch that ends in a literal dollar sign" \
	"$([ "$a" -le "$b" ] && echo yes || echo no$)" "yes"
check "a threshold behind a redirect into a word joined to a single-quoted part with a space" \
	"$([ "$a" -le "$b" ] 2>x'y z' && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a word joined to a double-quoted part with a space" \
	"$([ "$a" -le "$b" ] 2>x"y z" && echo yes || echo no)" "yes"
check "a threshold behind a redirect into an ANSI-C quoted path with a space" \
	"$([ "$a" -le "$b" ] 2>$'a b' && echo yes || echo no)" "yes"
check "a threshold behind a redirect into two single-quoted parts, the second with a space" \
	"$([ "$a" -le "$b" ] >'a''b c' && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a single-quoted part joined to a double-quoted part with a space" \
	"$([ "$a" -le "$b" ] 2>'a b'"c d" && echo yes || echo no)" "yes"
check_num "an if/then/else threshold behind a redirect into an ANSI-C quoted path with a space" \
	"$(if [ "$a" -le "$b" ] 2>$'a b'; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold behind a redirect into a word joined to a double-quoted part with a space" \
	"$(if [ "$a" -le "$b" ] 2>x"y $z"; then echo 1; else echo 0; fi)" "1"
check "a threshold behind a redirect into a path that holds the digits four and seven" \
	"$([ "$a" -le "$b" ] 2>/tmp/x47 && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a single-quoted path that holds the digits four and seven" \
	"$([ "$a" -le "$b" ] 2>'a 47' && echo yes || echo no)" "yes"
check "a threshold behind a redirect into a single-quoted path, before a single-quoted want" \
	"$([ "$a" -le "$b" ] 2>'a b' && echo yes || echo no)" 'yes'
check "a threshold behind a redirect into a single-quoted path that holds a dollar, an underscore and a double quote" \
	"$([ "$a" -le "$b" ] 2>'$d_"b' && echo yes || echo no)" "yes"
PROBE
# SORTED IN THE C LOCALE. A locale-aware sort ignores spaces, so it puts
# `an if/then/else ...` before `a threshold ...` and the order would depend on
# the runner. Measured: as root on PG15 the names came out in the other order.
_lam_redir_names="$(_lam_sweep "$_lam_probe_redir" | cut -f2 | LC_ALL=C sort | tr '\n' ';')"
check_text "control: the sweep sees an arm behind a redirect and a floor of two" \
	"$_lam_redir_names" "a count above one;a count above one behind a redirect;a floor of zero;a threshold before a later argument that holds a substitution;a threshold behind a dup and a redirect into a variable;a threshold behind a dup joined to an unspaced and;a threshold behind a noclobber redirect;a threshold behind a quoted redirect joined to an unspaced and;a threshold behind a redirect into a bare variable;a threshold behind a redirect into a path that holds the digits four and seven;a threshold behind a redirect into a path with an underscore;a threshold behind a redirect into a quoted path that holds an and and angle brackets;a threshold behind a redirect into a quoted path with a space and two suffixes;a threshold behind a redirect into a quoted path with a space, an underscore and a digit;a threshold behind a redirect into a single-quoted part joined to a double-quoted part with a space;a threshold behind a redirect into a single-quoted path that holds a dollar, an underscore and a double quote;a threshold behind a redirect into a single-quoted path that holds an and and angle brackets;a threshold behind a redirect into a single-quoted path that holds the digits four and seven;a threshold behind a redirect into a single-quoted path with a space;a threshold behind a redirect into a single-quoted path with a space and a double-quoted suffix;a threshold behind a redirect into a single-quoted path with a space and a suffix;a threshold behind a redirect into a single-quoted path, before a single-quoted want;a threshold behind a redirect into a variable;a threshold behind a redirect into a word joined to a double-quoted part with a space;a threshold behind a redirect into a word joined to a single-quoted part with a space;a threshold behind a redirect into a word with a quoted suffix;a threshold behind a redirect into an ANSI-C quoted path with a space;a threshold behind a redirect into two single-quoted parts, the second with a space;a threshold behind a redirect joined to an unspaced and;a threshold behind a redirect with two spaces before its target;a threshold behind a redirect with two spaces before the and;a threshold behind a single-quoted redirect joined to an unspaced and;a threshold behind a spaced appending redirect into a variable;a threshold behind a spaced redirect;a threshold behind a spaced redirect into a variable;a threshold behind a stderr redirect;a threshold behind a two-digit descriptor;a threshold behind an and-redirect;a threshold behind an and-redirect into a variable;a threshold behind an input redirect;a threshold behind two redirects;a threshold joined to an unspaced echo;a threshold spaced with tabs;a threshold whose else branch is on the next line;a threshold whose no branch redirects;a threshold whose substitution is followed by a variable in the same argument;a threshold with two spaces before the bracket;a threshold with two spaces before the echo;an if/then/else test-builtin threshold behind an unspaced redirect into a variable;an if/then/else threshold before a comment that holds the word if;an if/then/else threshold before a later substitution that holds an if;an if/then/else threshold before a negated if;an if/then/else threshold before an if on a helper whose name starts with test;an if/then/else threshold behind a doubly spaced redirect into a variable;an if/then/else threshold behind a quoted redirect joined to an unspaced then;an if/then/else threshold behind a redirect into a single-quoted path with a space;an if/then/else threshold behind a redirect into a variable;an if/then/else threshold behind a redirect into a word joined to a double-quoted part with a space;an if/then/else threshold behind a redirect into an ANSI-C quoted path with a space;an if/then/else threshold behind a redirect joined to an unspaced then;an if/then/else threshold behind a single-quoted redirect joined to an unspaced then;an if/then/else threshold behind a spaced redirect into a variable;an if/then/else threshold behind an and-redirect into a variable;an if/then/else threshold behind an unspaced redirect into a variable;an if/then/else threshold spaced with tabs;an if/then/else threshold with a space before the if;an if/then/else threshold with an elif;an if/then/else threshold with two spaces after the if;an if/then/else threshold with two spaces before the if;"

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
# the end pin the mirrored excuses, the `test` end, the `[[ ]]` end and the
# file descriptor that `2>x` hands back to the redirect, as the mutant sweep
# above found missing (#1995 review rounds 3 and 4).
#
# A `]` INSIDE QUOTES OR A SUBSTITUTION DOES NOT END THE TEST (#2002). The
# arms from `a threshold over array elements whose subscripts hold a bracket`
# to the end hold one, as in `"${s[5]}"` or `"$(grep -c '[x]' "$f")"`. The test
# ended at that `]`, so the rest of it read as the text after the test and the
# arm was never examined. Seven live arms in six suites hid this way.
#
# THE ARMS FROM `determinate: a writable-file test` TO `a threshold behind a
# redirect joined to the bracket`, AND THOSE AFTER THE BRACKET ARMS, PIN THE
# OLDER PATTERNS (#2006): every unary flag, spacing and tabs in the excuses, a
# quoted floor, each recorder name in the `[` and if/then/else rules, an
# indented recorder, and the strip of what follows `fi)`. A mutant sweep found
# each piece could change while every control stayed green.
#
# A BRANCH MAY BE WRITTEN WITH `printf` (#2011). `&& printf yes || echo no`
# prints only `no` on failure, as `echo` does, and so does an if/then/else whose
# then or else branch is `printf`. A `printf` that prints a value carries it,
# as `echo "$x"` does, so the two `carried:` printf arms stay excused. The seven
# `not this shape:` arms run `printenv`, `cat out.log` or `true`, which the rule
# does not read: they pin that only `echo` and `printf` are. Their branches hold
# no `$`, so the carried-value excuse cannot hide a widened rule (#2020 review). The arms with two spaces or a tab after
# `then` and `else`, and the one with `&& true` before `then`, pin how the
# if/then/else rule finds its branches.
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
check "not this shape: a test-builtin threshold piped into cat" \
	"$(test "$a" -le "$b" | cat && echo yes || echo no)" "yes"
check "a test-builtin threshold with a tab after test" \
	"$(test	"$a" -le "$b" && echo yes || echo no)" "yes"
check_num "an if/then/else test-builtin threshold with a tab after test" \
	"$(if test	"$a" -le "$b"; then echo 1; else echo 0; fi)" "1"
check "a test-builtin floor over a count of a bracket class" \
	"$(test "$(grep -c "[ab]" "$f")" -ge 20 && echo yes || echo no)" "yes"
check "determinate: a test-builtin count against nothing behind a tab and a redirect" \
	"$(test "$rows" -gt 0	2>/dev/null && echo yes || echo no)" "yes"
check "determinate: a mirrored nothing versus something spaced with tabs" \
	"$([	0	-lt	"$x"	] && echo yes || echo no)" "yes"
check "determinate: a mirrored floor of one spaced with tabs" \
	"$([	1	-le	"$x"	] && echo yes || echo no)" "yes"
check "a mirrored count above one with a leading zero" \
	"$([ 01 -lt "$x" ] && echo yes || echo no)" "yes"
check "a mirrored count above five with a leading zero" \
	"$([ 05 -lt "$x" ] && echo yes || echo no)" "yes"
check "a mirrored at most one" \
	"$([ 1 -ge "$x" ] && echo yes || echo no)" "yes"
check "a mirrored negative count" \
	"$([ 0 -gt "$x" ] && echo yes || echo no)" "yes"
check "a test-builtin threshold against a quoted zero glued to a digit" \
	"$(test "$n" -gt "0"2>/dev/null && echo yes || echo no)" "yes"
check "a double-bracket floor over a variable with an underscore" \
	"$([[ "$n_rows" -ge 20 ]] && echo yes || echo no)" "yes"
check "a double-bracket floor over a count read through a redirect" \
	"$([[ "$(wc -l < "$f")" -ge 20 ]] && echo yes || echo no)" "yes"
check "a double-bracket string order" \
	"$([[ "$a" > "$b" ]] && echo yes || echo no)" "yes"
check "a floor over a count whose stderr is joined" \
	"$([ "$(grep -c x "$f" 2>&1)" -ge 20 ] && echo yes || echo no)" "yes"
check "a test-builtin threshold over a bare variable joined to an unspaced and" \
	"$(test $n -le 3&& echo yes || echo no)" "yes"
check "a test-builtin threshold against a number joined to an unspaced and" \
	"$(test "$a" -le 3&& echo yes || echo no)" "yes"
check "a floor over a count read through a redirect whose branch holds brackets" \
	"$([ "$(wc -l < "$f")" -ge 20 ] && echo yes || echo "no [x]")" "yes"
check "a test-builtin threshold behind a dup and an input redirect" \
	"$(test "$a" -le "$b" 2>&1 <"$f" && echo yes || echo no)" "yes"
check "a threshold whose first branch is printf" \
	"$([ "$a" -le "$b" ] && printf yes || echo no)" "yes"
check_num "an if/then/else threshold whose then branch is printf" \
	"$(if [ "$a" -le "$b" ]; then printf 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold whose else branch is printf" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else printf 0; fi)" "1"
check "carried: a printf branch prints the value" \
	"$([ "$a" -le "$b" ] && printf '%s' "$a" || echo no)" "yes"
check_num "carried: an if/then/else printf branch prints the value" \
	"$(if [ "$a" -le "$b" ]; then printf '%s' "$a"; else echo 0; fi)" "1"
check "not this shape: a threshold whose first branch is printenv" \
	"$([ "$a" -le "$b" ] && printenv X || echo no)" "yes"
check_num "not this shape: an if/then/else threshold whose then branch is printenv" \
	"$(if [ "$a" -le "$b" ]; then printenv X; else echo 0; fi)" "1"
check_num "not this shape: an if/then/else threshold whose else branch is printenv" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else printenv Y; fi)" "1"
check_num "an if/then/else threshold with two spaces after then and after else" \
	"$(if [ "$a" -le "$b" ]; then  echo 1; else  echo 0; fi)" "1"
check_num "an if/then/else threshold with tabs after then and after else" \
	"$(if [ "$a" -le "$b" ]; then	echo 1; else	echo 0; fi)" "1"
check_num "an if/then/else threshold with another command before then" \
	"$(if [ "$a" -le "$b" ] && true; then echo 1; else echo 0; fi)" "1"
check "not this shape: a threshold whose first branch is cat" \
	"$([ "$a" -le "$b" ] && cat out.log || echo no)" "yes"
check "not this shape: a threshold whose first branch is true" \
	"$([ "$a" -le "$b" ] && true || echo no)" "yes"
check_num "not this shape: an if/then/else threshold whose then branch is cat" \
	"$(if [ "$a" -le "$b" ]; then cat out.log; else echo 0; fi)" "1"
check_num "not this shape: an if/then/else threshold whose else branch is cat" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else cat out.log; fi)" "1"
check "a threshold over array elements whose subscripts hold a bracket" \
	"$([ "${s[5]}" -le "${s[2]}" ] && echo yes || echo no)" "yes"
check "a double-bracket threshold over array elements whose subscripts hold a bracket" \
	"$([[ "${s[5]}" -le "${s[2]}" ]] && echo yes || echo no)" "yes"
check_num "an if/then/else threshold over array elements whose subscripts hold a bracket" \
	"$(if [ "${s[5]}" -le "${s[2]}" ]; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else double-bracket threshold over array elements whose subscripts hold a bracket" \
	"$(if [[ "${s[5]}" -le "${s[2]}" ]]; then echo 1; else echo 0; fi)" "1"
check "a floor over a quoted count of a bracket class" \
	"$([ "$(grep -c '[[:space:]]' "$f")" -ge 2 ] && echo yes || echo no)" "yes"
check "a floor over an unquoted count of a bracket class" \
	"$([ $(grep -c '[x]' "$f") -ge 2 ] && echo yes || echo no)" "yes"
check "a floor over a backtick count of a bracket class" \
	"$([ `grep -c '[x]' "$f"` -ge 2 ] && echo yes || echo no)" "yes"
check "a double-bracket floor over a count of a bracket class" \
	"$([[ "$(grep -c '[^_[:alnum:]]' "$f")" -ge 3 ]] && echo yes || echo no)" "yes"
check "determinate: a count against nothing over an array element whose subscript holds a bracket" \
	"$([ "${s[1]}" -gt 0 ] && echo yes || echo no)" "yes"
check "carried: a branch names array elements whose subscripts hold a bracket" \
	"$([ "${s[5]}" -le "${s[2]}" ] && echo yes || echo "no (${s[5]} > ${s[2]})")" "yes"
check "determinate: a writable-file test" \
	"$([ -w "$f" ] && echo yes || echo no)" "yes"
check "determinate: a symbolic-link test" \
	"$([ -h "$f" ] && echo yes || echo no)" "yes"
check "determinate: a named-pipe test" \
	"$([ -p "$f" ] && echo yes || echo no)" "yes"
check "determinate: a socket test" \
	"$([ -S "$f" ] && echo yes || echo no)" "yes"
check "determinate: a block-device test" \
	"$([ -b "$f" ] && echo yes || echo no)" "yes"
check "determinate: a character-device test" \
	"$([ -c "$f" ] && echo yes || echo no)" "yes"
check "determinate: a set-group-id test" \
	"$([ -g "$f" ] && echo yes || echo no)" "yes"
check "determinate: a sticky-bit test" \
	"$([ -k "$f" ] && echo yes || echo no)" "yes"
check "determinate: a set-user-id test" \
	"$([ -u "$f" ] && echo yes || echo no)" "yes"
check "determinate: a group-owner test" \
	"$([ -G "$f" ] && echo yes || echo no)" "yes"
check "determinate: a negated file test with two spaces before the bang" \
	"$([  ! -f "$f" ] && echo yes || echo no)" "yes"
check "determinate: a negated file test with two spaces after the bang" \
	"$([ !  -f "$f" ] && echo yes || echo no)" "yes"
check "determinate: a double-bracket equality written with two equals signs" \
	"$([[ "$a" == "$b" ]] && echo yes || echo no)" "yes"
check "determinate: a count against nothing with two spaces before the zero" \
	"$([ "$rows" -gt  0 ] && echo yes || echo no)" "yes"
check "determinate: a count against nothing with two spaces before the bracket" \
	"$([ "$rows" -gt 0  ] && echo yes || echo no)" "yes"
check "determinate: a test-builtin count against nothing joined to an unspaced and" \
	"$(test "$rows" -gt 0&& echo yes || echo no)" "yes"
check "determinate: a floor of one with two spaces before the one" \
	"$([ "$rows" -ge  1 ] && echo yes || echo no)" "yes"
check "determinate: a quoted floor of one" \
	"$([ "$rows" -ge "1" ] && echo yes || echo no)" "yes"
check "determinate: a double-bracket quoted floor of one" \
	"$([[ "$rows" -ge "1" ]] && echo yes || echo no)" "yes"
check_text "a threshold recorded by check_text" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check_ratio "a threshold recorded by check_ratio" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
pgc_fail "a threshold recorded by pgc_fail" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check_skip "a threshold recorded by check_skip" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check  "a threshold recorded with two spaces before its name" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check "an if/then/else threshold recorded by check" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_text "an if/then/else threshold recorded by check_text" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_ratio "an if/then/else threshold recorded by check_ratio" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
pgc_fail "an if/then/else threshold recorded by pgc_fail" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_skip "an if/then/else threshold recorded by check_skip" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_num  "an if/then/else threshold recorded with two spaces before its name" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
	check_num "an if/then/else threshold recorded from an indented line" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check_num "an if/then/else threshold with two spaces before each echo" \
	"$(if [ "$a" -le "$b" ]; then  echo 1; else  echo 0; fi)" "1"
check_num "an if/then/else threshold joined to fi, before a later substitution" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0;fi)" "$(want)"
check_num "an if/then/else threshold with a space after fi, before a later substitution" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi )" "$(want)"
check_num "carried: an if/then/else whose carrying branch is joined to fi" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo "0 ($a)";fi)" "1"
check "a threshold behind a redirect joined to the bracket" \
	"$([ "$a" -le "$b" ]>/dev/null && echo yes || echo no)" "yes"
precheck "not this shape: a threshold handed to a helper whose name ends in check" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
precheck "not this shape: an if/then/else threshold handed to a helper whose name ends in check" \
	"$(if [ "$a" -le "$b" ]; then echo 1; else echo 0; fi)" "1"
check "a threshold over bare variables" \
	"$([ $a -le $b ] && echo yes || echo no)" "yes"
check "a double-bracket threshold over bare variables" \
	"$([[ $a -le $b ]] && echo yes || echo no)" "yes"
check "a double-bracket string order by less-than" \
	"$([[ "$a" < "$b" ]] && echo yes || echo no)" "yes"
check "a string order by an escaped less-than" \
	"$([ "$a" \< "$b" ] && echo yes || echo no)" "yes"
check "a string order by an escaped greater-than" \
	"$([ "$a" \> "$b" ] && echo yes || echo no)" "yes"
check "determinate: a negated file test spaced with tabs" \
	"$([	!	-f	"$f"	] && echo yes || echo no)" "yes"
check "determinate: an equality spaced with tabs" \
	"$([	"$a"	=	"$b"	] && echo yes || echo no)" "yes"
check "determinate: a numeric equality" \
	"$([ "$n" -eq 3 ] && echo yes || echo no)" "yes"
check "determinate: a count against nothing with a tab before the zero" \
	"$([ "$rows" -gt	0 ] && echo yes || echo no)" "yes"
check "determinate: a floor of one with a tab before the one" \
	"$([ "$rows" -ge	1 ] && echo yes || echo no)" "yes"
check "determinate: a floor of one with a tab before the bracket" \
	"$([ "$rows" -ge 1	] && echo yes || echo no)" "yes"
check "determinate: a floor of one with two spaces before the bracket" \
	"$([ "$rows" -ge 1  ] && echo yes || echo no)" "yes"
check_num "determinate: an if/then/else test-builtin string test with an elif" \
	"$(if test -n "$e"; then echo 1; elif [ -f x ]; then echo 1; else echo 0; fi)" "1"
check "a threshold with a redirect inside the brackets" \
	"$([ "$a" -le "$b" 2>&1 ] && echo yes || echo no)" "yes"
PROBE
_lam_forms_names="$(_lam_sweep "$_lam_probe_forms" | cut -f2 | LC_ALL=C sort | tr '\n' ';')"
check_text "control: the sweep reads double brackets, the test builtin and mirrored tests" \
	"$_lam_forms_names" "a double-bracket floor over a count of a bracket class;a double-bracket floor over a count read through a redirect;a double-bracket floor over a variable with an underscore;a double-bracket string order;a double-bracket string order by less-than;a double-bracket threshold;a double-bracket threshold behind a redirect into a variable;a double-bracket threshold over array elements whose subscripts hold a bracket;a double-bracket threshold over bare variables;a floor over a backtick count of a bracket class;a floor over a count read through a redirect whose branch holds brackets;a floor over a count whose stderr is joined;a floor over a quoted count of a bracket class;a floor over an unquoted count of a bracket class;a mirrored at most one;a mirrored count above five with a leading zero;a mirrored count above one;a mirrored count above one with a leading zero;a mirrored count above ten;a mirrored floor of eleven;a mirrored negative count;a string order by an escaped greater-than;a string order by an escaped less-than;a test-builtin floor over a count of a bracket class;a test-builtin floor over a count of an escaped alternation;a test-builtin floor over a count read through a redirect;a test-builtin threshold;a test-builtin threshold against a number behind a redirect;a test-builtin threshold against a number joined to an unspaced and;a test-builtin threshold against a quoted zero glued to a digit;a test-builtin threshold behind a dup and an input redirect;a test-builtin threshold behind a redirect;a test-builtin threshold over a bare variable joined to an unspaced and;a test-builtin threshold over a pipeline that may fail;a test-builtin threshold whose branch holds brackets;a test-builtin threshold with a tab after test;a threshold behind a redirect joined to the bracket;a threshold over array elements whose subscripts hold a bracket;a threshold over bare variables;a threshold recorded by check_ratio;a threshold recorded by check_skip;a threshold recorded by check_text;a threshold recorded by pgc_fail;a threshold recorded with two spaces before its name;a threshold whose first branch is printf;a threshold with a redirect inside the brackets;an if/then/else double-bracket threshold;an if/then/else double-bracket threshold over array elements whose subscripts hold a bracket;an if/then/else test-builtin threshold;an if/then/else test-builtin threshold with a tab after test;an if/then/else threshold joined to fi, before a later substitution;an if/then/else threshold over array elements whose subscripts hold a bracket;an if/then/else threshold recorded by check;an if/then/else threshold recorded by check_ratio;an if/then/else threshold recorded by check_skip;an if/then/else threshold recorded by check_text;an if/then/else threshold recorded by pgc_fail;an if/then/else threshold recorded from an indented line;an if/then/else threshold recorded with two spaces before its name;an if/then/else threshold whose else branch is printf;an if/then/else threshold whose then branch is printf;an if/then/else threshold with a space after fi, before a later substitution;an if/then/else threshold with another command before then;an if/then/else threshold with tabs after then and after else;an if/then/else threshold with two spaces after then and after else;an if/then/else threshold with two spaces before each echo;at most zero;nothing expected and a count found;"

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
#
# THE FIRST WORD MAY BE `-a` OR `-o` WHEN AN EQUALITY FOLLOWS IT (#2007).
# `[ -a = "$arg" ]` compares the word `-a`, as `[ "$arg" = -a ]` does, with
# `=`, `!=` or `==`, and any spaces or tabs around it. A later `-a` is still a
# join, so `[ -a = x -a "$b" -le 3 ]` and its `==` twin are refused, and so is
# `[ "$b" -le 3 -o -a = "$c" ]`, where that `-a` is not the first word.
# A first `-a` that is the unary file test, as in `[ -a "$f" -a "$x" = y ]`,
# is not dropped, so that compound is refused too.
#
# TWO TESTS CHAINED OUTSIDE THE BRACKETS ARE A COMPOUND TOO (#1997).
# `[ "$b" -le "$c" ] && [ "$a" -gt 0 ] && echo yes || echo no` was never
# examined: the text after the first test began `&& [`, not `&& echo`. In the
# if/then/else form the second test was read as part of the branches, and its
# `$` excused the arm. The chain may use `&&` or `||`, any of the three
# spellings, redirects, tabs, no spaces, or a line break after `&&`. A line
# that ends in `&&` or `||` is joined to the next, as bash does, so a carrying
# branch on the next line still counts. A comment that ends in `||` is not
# joined, or it would swallow the arm below it.
#
# THE JOIN MUST NOT HIDE OR RENAME AN ARM THAT MAIN READ (#2022 review). The
# arms from the one after `foo &&` near the end of this probe back to the
# here-doc arm pin _lam_join:
#   - a trailing comment or an open string that ends in `||` or `&&` is not
#     code, so the line is not joined;
#   - a line whose `&&` or `||` is at the top level, not inside an open `$(`,
#     is not joined, so the arm on it or on the next line keeps its name and
#     its verdict: after `[ -n "$X" ] &&`, `start_x "$p" &&`, a subshell, a
#     `case` branch or another arm, and before an unnamed arm, an assignment
#     or an `echo` of the operand (round 2, S3 to T4). An arm whose
#     substitution is not quoted closes its `$(` too (round 3, Y6 and Y7);
#   - a line that holds a whole arm and then opens a second `$(` ending in
#     `&&` or `||` is not joined either, with or without a subshell inside
#     it, so the arm keeps its verdict (round 3, Y8 to Y10);
#   - a top-level `&&` followed by a comment is not joined either: the
#     `echo` below a check whose `and` is followed by a comment stays
#     unnamed (round 4);
#   - a `&&` or `||` inside quotes at the end of a line is not code, even
#     inside an open `$(` (round 3, Y3);
#   - `#` starts a comment only at the start of a word, after a space, `;`,
#     `&`, `|`, `(` or `)`, so `$#`, `${#s}`, `a#b` and `"$d"#y` hold no
#     comment, and a comment line in column 0 is a comment even when it
#     opens a `$(`. The three `not this shape:` arms whose comment is glued
#     to a `;`, a `(` or a `)` pin those three characters (round 4);
#   - inside an open `$(`, a comment line is skipped and a trailing comment
#     is cut, so neither stops the chain nor lends it a value, and a quoted
#     or backticked `#` or an escaped quote is not a comment or a quote.
#     The pair of tests chained across a comment line that quotes a
#     recorder pins the skip for an indented comment line (round 4);
#   - inside an open `$(`, a line that holds a recorder call is read alone,
#     under its own name, for each of the six recorders, unindented or after
#     a `{`; a helper whose name only ends in `check` is joined.
# Each arm makes one mutant of _lam_join fail this control. The round-4 arms
# have no `$(` that closes earlier on their line, so the CC stop does not end
# the join before the comment rules are read. The older arms that do have
# one, such as the thresholds beside a substitution whose comment is glued
# to a `;`, `(` or `)`, now pin the CC stop instead (round 4). Three mutants
# stay green and are equivalent: a `\` escape at the end of the line (the
# `\` join runs first), `check"` as a recorder (bash reads one word), and a
# join with no space (`&&` and `||` end a word).
#
# THE JOIN RULE MUST NOT OVER-APPLY EITHER. `[ "$x"-a = "$y" ]`, `[ 5-a = "$x"
# ]`, `[ $a = 0 ]` and `test "$a" = -a10>x` hold no join, and a redirect
# inside `[ ]` is not a `&&`. The `determinate:` arms just before the
# #2007 arms pin that (#1995 review round 4).
#
# AN UNQUOTED REGEX AFTER `=~` DOES NOT END A `[[ ]]` (#2027). `^[0-9]+$`
# holds a `]`, and the test ended there, so the compound
# `[[ "$a" -le "$b" && "$a" =~ ^[0-9]+$ ]]` was never examined. The arms from
# `a compound with a regex bracket class` to the one whose last operand ends
# in `=~` hold a class that starts with `]`, a negated `[^]]`, a POSIX class,
# a group with `|` or a blank, a quoted part and an escaped blank in the
# regex, the regex before the `&&`, and the if/then/else form. A lone regex
# match is refused, as `[[ "$a" =~ $re ]]` already was. The word `=~` in
# `[ ]` is an operand and hides nothing there, and `x=~` is not `=~`. The
# two `known limit:` arms pin that only the word after `=~` is read: a glob
# after `==` that holds `]]`, even one that starts with `=~`, still ends the
# test early, so the compound after it is not examined.
#
# THE ARMS AFTER THE CHAINED ONES PIN THE LINE JOIN AND THE CHAIN RULE, as a
# mutant sweep found missing (#2006, #1997): a backslash inside the first
# half of a continued arm, a comment that ends in `||` at any indent, a line
# sent to the background or ending in `&& cmd` above an arm, and a test piped
# into a second test, which is not a chain.
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
check "a compound with a regex bracket class" \
	"$([[ "$a" -le "$b" && "$a" =~ ^[0-9]+$ ]] && echo yes || echo no)" "yes"
check "a compound with a regex bracket class that starts with a close bracket" \
	"$([[ "$a" -le "$b" && "$a" =~ ^[]a]+$ ]] && echo yes || echo no)" "yes"
check "a compound with a negated regex bracket class of a close bracket" \
	"$([[ "$a" -le "$b" && "$a" =~ ^[^]]+$ ]] && echo yes || echo no)" "yes"
check "a compound with a regex POSIX class" \
	"$([[ "$a" -le "$b" && "$a" =~ ^[[:digit:]]+$ ]] && echo yes || echo no)" "yes"
check "a compound with a regex group, an alternation and a bracket class" \
	"$([[ "$a" -le "$b" && "$a" =~ ^(x|[0-9])+$ ]] && echo yes || echo no)" "yes"
check "a compound with a regex group that holds a space and a bracket class" \
	"$([[ "$a" -le "$b" && "$a" =~ ^(x [0-9])+$ ]] && echo yes || echo no)" "yes"
check "a compound with a regex whose quoted part holds a space" \
	"$([[ "$a" -le "$b" && "$a" =~ ^"x y"[0-9]+$ ]] && echo yes || echo no)" "yes"
check "a compound with a regex that holds an escaped space" \
	"$([[ "$a" -le "$b" && "$a" =~ ^x\ [0-9]+$ ]] && echo yes || echo no)" "yes"
check "a compound whose regex bracket class comes before the and" \
	"$([[ "$a" =~ ^[0-9]+$ && "$a" -le "$b" ]] && echo yes || echo no)" "yes"
check "a regex bracket class" \
	"$([[ "$a" =~ ^[0-9]+$ ]] && echo yes || echo no)" "yes"
check "a regex in a variable" \
	"$([[ "$a" =~ $re ]] && echo yes || echo no)" "yes"
check_num "an if/then/else compound with a regex bracket class" \
	"$(if [[ "$a" -le "$b" && "$a" =~ ^[0-9]+$ ]]; then echo 1; else echo 0; fi)" "1"
check "carried: a compound with a regex bracket class whose branch names the operands" \
	"$([[ "$a" -le "$b" && "$a" =~ ^[0-9]+$ ]] && echo yes || echo "no (a=$a b=$b)")" "yes"
check "determinate: an equality with the word =~ on the right" \
	"$([[ "$op" == =~ ]] && echo yes || echo no)" "yes"
check "a compound whose last operand is the word =~ in single brackets" \
	"$([ "$a" -le "$b" -a "$c" != =~ ] && echo yes || echo no)" "yes"
check "a double-bracket compound whose last operand ends in =~" \
	"$([[ "$a" -le "$b" && "$c" == x=~ ]] && echo yes || echo no)" "yes"
check "known limit: a double-bracket compound after a glob that holds a POSIX class" \
	"$([[ "$c" == [[:digit:]] && "$a" -le "$b" ]] && echo yes || echo no)" "yes"
check "known limit: a double-bracket compound after a glob that starts with =~" \
	"$([[ "$c" == =~[[:digit:]] && "$a" -le "$b" ]] && echo yes || echo no)" "yes"
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
check "a compound joined by a tab-separated and" \
	"$([ "$a" = x	-a	"$b" ] && echo yes || echo no)" "yes"
check "determinate: an equality whose quoted left operand ends in -a" \
	"$([ "$x"-a = "$y" ] && echo yes || echo no)" "yes"
check "determinate: an equality whose numeric left operand ends in -a" \
	"$([ 5-a = "$x" ] && echo yes || echo no)" "yes"
check "determinate: an equality over a bare variable whose name starts with a" \
	"$([ $a = 0 ] && echo yes || echo no)" "yes"
check "determinate: an equality with a redirect inside the brackets" \
	"$([ "$a" = "$b" 2>&1 ] && echo yes || echo no)" "yes"
check "determinate: an equality whose right operand is a word that starts with -a glued to a redirect" \
	"$(test "$a" = -a10>/dev/null && echo yes || echo no)" "yes"
check "a test-builtin compound whose last operand is glued to a redirect" \
	"$(test "$a" -le "$b" -a -f x2>"$LOG" && echo yes || echo no)" "yes"
check "determinate: an equality with the word -a on the left is not a compound" \
	"$([ -a = "$arg" ] && echo yes || echo no)" "yes"
check "determinate: an inequality with the word -o on the left is not a compound" \
	"$([ -o != "$arg" ] && echo yes || echo no)" "yes"
check "determinate: a double-equals equality with the word -a on the left is not a compound" \
	"$([ -a == "$arg" ] && echo yes || echo no)" "yes"
check "determinate: an equality with the word -o on the left after two spaces is not a compound" \
	"$([  -o = "$arg" ] && echo yes || echo no)" "yes"
check "determinate: an equality with the word -a on the left spaced with tabs is not a compound" \
	"$([	-a	=	"$arg" ] && echo yes || echo no)" "yes"
check "a compound after an equality with the word -a on the left" \
	"$([ -a = x -a "$b" -le 3 ] && echo yes || echo no)" "yes"
check "a compound whose last half is an equality with the word -a on the left" \
	"$([ "$b" -le 3 -o -a = "$c" ] && echo yes || echo no)" "yes"
check "a compound after a double-equals equality with the word -a on the left" \
	"$([ -a == x -a "$b" -le 3 ] && echo yes || echo no)" "yes"
check "determinate: an inequality with the word -o on the left and two spaces after it" \
	"$([ -o  != "$arg" ] && echo yes || echo no)" "yes"
check "a compound of a file test and an equality" \
	"$([ -a "$f" -a "$x" = y ] && echo yes || echo no)" "yes"
check "a pair of tests chained by and" \
	"$([ "$b" -le "$c" ] && [ "$a" -gt 0 ] && echo yes || echo no)" "yes"
check_num "an if/then/else pair of tests chained by and" \
	"$(if [ "$b" -le "$c" ] && [ "$a" -gt 0 ]; then echo 1; else echo 0; fi)" "1"
check "a pair of tests chained by or" \
	"$([ "$b" -le "$c" ] || [ "$a" -gt 0 ] && echo yes || echo no)" "yes"
check_num "an if/then/else pair of tests chained by or" \
	"$(if [ "$b" -le "$c" ] || [ "$a" -gt 0 ]; then echo 1; else echo 0; fi)" "1"
check "a pair of determinate tests chained by and" \
	"$([ -f "$f" ] && [ "$a" -gt 0 ] && echo yes || echo no)" "yes"
check "a file test chained to a test-builtin count against nothing" \
	"$([ -f "$f" ] && test "$a" -gt 0 && echo yes || echo no)" "yes"
check "a test-builtin count against nothing chained to a test builtin by or" \
	"$(test "$a" -gt 0 || test "$b" -gt 0 && echo yes || echo no)" "yes"
check "a pair of double-bracket tests chained by and" \
	"$([[ "$b" -le "$c" ]] && [[ "$a" -gt 0 ]] && echo yes || echo no)" "yes"
check "a chain of three tests" \
	"$([ "$a" -gt 0 ] && [ "$b" -gt 0 ] && [ "$c" -gt 0 ] && echo yes || echo no)" "yes"
check "a pair of tests chained behind a redirect" \
	"$([ "$a" -gt 0 ] 2>"$LOG" && [ "$b" -gt 0 ] 2>/dev/null && echo yes || echo no)" "yes"
check "a pair of tests chained by an unspaced and" \
	"$([ "$a" -gt 0 ]&&[ "$b" -gt 0 ]&&echo yes || echo no)" "yes"
check_num "an if/then/else pair of tests chained by an unspaced and" \
	"$(if [ "$a" -gt 0 ]&&[ "$b" -gt 0 ];then echo 1; else echo 0; fi)" "1"
check "a pair of tests chained with tabs" \
	"$([ "$a" -gt 0 ]	&&	[ "$b" -gt 0 ]	&&	echo yes || echo no)" "yes"
check "carried: a pair of chained tests whose branch names every operand" \
	"$([ "$b" -le "$c" ] && [ "$a" -gt 0 ] && echo yes || echo "no (a=$a b=$b c=$c)")" "yes"
check_num "carried: an if/then/else pair of chained tests whose branch names every operand" \
	"$(if [ "$b" -le "$c" ] && [ "$a" -gt 0 ]; then echo 1; else echo "0 (a=$a b=$b c=$c)"; fi)" "1"
check "not this shape: a test chained to a helper whose name starts with test" \
	"$([ "$a" -gt 0 ] && test_ok && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after and" \
	"$([ "$a" -gt 0 ] &&
		[ "$b" -gt 0 ] && echo yes || echo no)" "yes"
check "carried: a threshold whose carrying branch follows a line break after or" \
	"$([ "$a" -le "$b" ] && echo yes ||
		echo "no ($a > $b)")" "yes"
check "carried: a threshold whose carrying branch follows a line break after or and a tab" \
	"$([ "$a" -le "$b" ] && echo yes ||	
		echo "no ($a > $b)")" "yes"
# a comment that ends in or ||
check "a threshold below a comment that ends in or" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check "a pair of tests chained with a redirect joined to the second" \
	"$([ "$a" -gt 0 ] && [ "$b" -gt 0 ]>/dev/null && echo yes || echo no)" "yes"
check "a floor over a count of an escaped alternation across a continuation" \
	"$([ "$(grep -c "a\|b" "$f")" -ge 20 ] \
		&& echo yes || echo no)" "yes"
check "a single-bracket test chained to a double-bracket test" \
	"$([ "$a" -gt 0 ] && [[ "$b" -gt 0 ]] && echo yes || echo no)" "yes"
check "a test-builtin threshold chained to a file test" \
	"$(test "$a" -le "$b" && [ -f "$f" ] && echo yes || echo no)" "yes"
check "not this shape: a test sent to the background before a chain" \
	"$([ "$a" -le "$b" ] & [ "$c" -gt 0 ] && [ "$d" -gt 0 ] && echo yes || echo no)" "yes"
check "determinate: a substitution that holds an escaped quote across a continuation is not a compound" \
	"$([ "$(grep -c "x\")" "$f" || true)" -gt 0 ] \
		&& echo yes || echo no)" "yes"
	# an indented comment that ends in or ||
check "a threshold below an indented comment that ends in or" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
	# check "not this shape: an indented comment that quotes an arm" \
	# 	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check "not this shape: a test piped with its stderr into a second test" \
	"$([ "$a" -le "$b" ] |& [ "$c" -gt 0 ] && echo yes || echo no)" "yes"
check "not this shape: a test piped into a second test" \
	"$([ "$a" -le "$b" ] | [ "$c" -gt 0 ] && echo yes || echo no)" "yes"
check "a file test chained to a test builtin by a tab" \
	"$([ -f "$f" ] && test	"$a" -gt 0 && echo yes || echo no)" "yes"
		# a doubly indented comment that ends in or ||
check "a threshold below a doubly indented comment that ends in or" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
"$srv" -D "$d" &
check "a threshold below a line sent to the background" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
cd "$d" && pwd
check "a threshold below a line that ends in an and and a command" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check "carried: a threshold whose carrying branch follows a line break after or and two tabs" \
	"$([ "$a" -le "$b" ] && echo yes ||		
		echo "no ($a > $b)")" "yes"
check "not this shape: a test piped with its stderr into a test builtin" \
	"$([ "$a" -le "$b" ] |& test "$c" -gt 0 && echo yes || echo no)" "yes"
check "a pair of tests chained with two spaces before the and" \
	"$([ "$a" -gt 0 ] && [ "$b" -gt 0 ]  && echo yes || echo no)" "yes"
echo "x" # a trailing comment that ends in or ||
check "a threshold below a trailing comment that ends in or" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
[ -n "$X" ] &&
	check "a threshold guarded by the line above, which ends in and" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
start_x "$p" &&
check "a threshold on the line after a command that ends in and" \
	"$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check "a threshold whose line ends in and before a carrying arm" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" &&
	check "carried: an arm on the line after one that ends in and" "$([ "$c" -le "$d" ] && echo yes || echo "no ($c $d)")" "yes"
cat <<EOF
a here-doc body line that ends in and &&
check "a threshold on the here-doc body line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
EOF
note="a string whose first line ends in and &&
check "a threshold on the line after a string whose first line ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
the string ends here"
check "a threshold whose line ends in a comment that ends in or" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" # a note ||
	echo "the next line names $a"
check "a threshold whose line opens a string that ends the line in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; note="a string &&
	that names $a"
[ -n "$X" ] &&
	{ check "a threshold in a brace group on the line after a test that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; }
check "a threshold whose line ends in and before an arm with no name" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" &&
	check $v "$([ "$c" -le "$d" ] && echo yes || echo "no $c")" "yes"
check "a threshold whose line ends in and before a carrying assignment" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" &&
	ok="$([ "$c" -le "$d" ] && echo "yes $c" || echo no)"
check "a threshold whose line ends in and before an echo of its operand" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" &&
	echo "passed with $a"
check "a threshold whose line ends in or before an echo of its operand" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" ||
	echo "failed with $a"
check "a threshold before a backtick that holds a hash and ends the line in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; v=`date # x` &&
	echo "$a"
check "a threshold before a word with a hash inside that ends the line in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; echo a#b &&
	echo "$a"
check "a threshold before a nested subshell that ends the line in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; ( (true) ) &&
	echo "$a"
[ $# -gt 0 ] && check "a threshold after a count of arguments on a line that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" &&
	echo "$a"
n=${#arr[@]}; check "a threshold after an array length on a line that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" &&
	echo "$a"
check "a threshold before a quoted word glued to a hash that ends the line in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; echo "x"#y &&
	echo "$a"
check "a pair of tests chained across a line break whose second line ends in a comment" \
	"$([ -f "$a" ] &&
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes" # (#1997)
check "a pair of tests chained across a line break and a comment line" \
	"$([ -f "$a" ] &&
		# the file first
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break and an unindented comment line" \
	"$([ -f "$a" ] &&
# the file first
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a comment" \
	"$([ -f "$a" ] && # the file first, then [ x ] && echo yes || echo "no $a"
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a comment glued to the and" \
	"$([ -f "$a" ] &&# the file first, then [ x ] && echo yes || echo "no $a"
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a comment glued to the or" \
	"$([ -f "$a" ] ||# no file, then [ x ] && echo yes || echo "no $a"
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "carried: an arm that holds a comment line that quotes an arm" \
	"$(start_x "$a" &&
		# check "not this shape: a comment inside an arm that quotes an arm" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
		check "carried: an inner arm" "$([ "$a" -le "$b" ] && echo yes || echo "no $a")" "yes")" "yes"
check "a pair of tests chained across a line break after a path with an escaped quote" \
	"$([ -f don\'t ] &&
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a backtick that holds a comment" \
	"$([ `echo 1 #x` -le "$c" ] &&
		[ "$a" -gt 0 ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a count of arguments" \
	"$([ $# -le "$c" ] &&
		[ "$a" -gt 0 ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a string length" \
	"$([ ${#s} -le "$c" ] &&
		[ "$a" -gt 0 ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a quoted path glued to a hash" \
	"$([ -f "$d"#y ] &&
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a pair of tests chained across a line break after a path with a hash inside" \
	"$([ -f a#b ] &&
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes"
check "a threshold beside a substitution whose comment is glued to a semicolon" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; x="$(true;# a note &&
	echo "$([ "$c" -le "$d" ] && echo yes || echo "no $c")")"
check "a threshold beside a substitution whose comment is glued to an open parenthesis" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; x="$( (# a note &&
	echo "$([ "$c" -le "$d" ] && echo yes || echo "no $c")") )"
check "a threshold beside a substitution whose comment is glued to a close parenthesis" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; x="$( (true)# a note &&
	echo "$([ "$c" -le "$d" ] && echo yes || echo "no $c")")"
# a note that opens a substitution: "$(start_x &&
x=1 \
check "a threshold below a comment that opens a substitution and a continued line" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
( cd "$d" &&
	x=1 \
	check "a threshold in a subshell on the line after a command that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" )
case "$m" in a) [ -n "$X" ] &&
	x=1 \
	check "a threshold in a case branch on the line after a test that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes" ;; esac
check "a pair of tests chained across a line break before a trailing comment that ends in and" \
	"$([ -f "$a" ] &&
		[ "$b" -le "$c" ] && echo yes || echo no)" "yes" && # a note
	x=1 \
	check "a threshold after a line whose comment follows an and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
	check "a threshold recorded inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
	check_num "a threshold recorded by check_num inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo 1 || echo 0)" "1")" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
	check_text "a threshold recorded by check_text inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
	check_ratio "a threshold recorded by check_ratio inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
	pgc_fail "a threshold recorded by pgc_fail inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
	check_skip "a threshold recorded by check_skip inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
check "a threshold recorded unindented inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "carried: an outer arm" "$(start_x "$p" &&
	{ check "a threshold in a brace group inside a substitution on the line after one that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; })" "yes"
check "a threshold inside a substitution before a helper whose name ends in check" "$(start_x "$p" &&
	precheck "x" "$([ "$a" -le "$b" ] && echo yes || echo no)")" "yes"
check "a threshold inside a substitution before a helper whose name ends in underscore check" "$(start_x "$p" &&
	pre_check "x" "$([ "$a" -le "$b" ] && echo yes || echo no)")" "yes"
check "a threshold inside a substitution before a recorder whose name is a variable" "$(start_x "$p" &&
	check $v "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "a threshold whose substitution holds a comment line that quotes a carrying test before a recorder" "$([ "$a" -le "$b" ] && echo yes || echo no) $(start_x "$p" &&
	# was "$([ "$a" -le "$b" ] && echo yes || echo "no $a")"
	check "carried: an inner arm" "$([ "$a" -le "$b" ] && echo yes || echo "no $a")" "yes")" "yes"
check "a threshold inside a substitution before a recorder glued to a semicolon" "$(start_x "$p" &&
	x=1;check "x" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes")" "yes"
check "a threshold before a substitution on its line that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; got=$(wait_for x &&
	echo "$([ "$c" -gt 0 ] && echo "up $c" || echo down)")
check "a threshold before a substitution on its line that ends in or" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; r=$(f ||
	g "$([ "$c" -gt 0 ] && echo "up $c" || echo down)")
check "a threshold before a substitution with a subshell on its line that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"; w=$( (true) &&
	check $v "$([ "$c" -le "$d" ] && echo yes || echo "no $c")" "yes")
check "an unquoted threshold whose line ends in and before an arm with no name" $([ "$a" -le "$b" ] && echo yes || echo no) yes &&
	check $v "$([ "$c" -le "$d" ] && echo yes || echo "no $c")" "yes"
check "an unquoted threshold whose line ends in and before a carrying assignment" $([ "$a" -le "$b" ] && echo yes || echo no) yes &&
	ok="$([ "$c" -le "$d" ] && echo "yes $c" || echo no)"
check "a threshold whose quoted branch ends its line in or" "$([ "$a" -le "$b" ] && echo "yes ||
$(echo no)")" "yes"
check "not this shape: an echo below a check whose and is followed by a comment" "yes" "yes" && # a note
	echo "$([ "$a" -le "$b" ] && echo yes || echo no)"
check "not this shape: a nameless check below a comment glued to a semicolon" "$(true;# a note &&
	check $v "$([ "$c" -le "$d" ] && echo yes || echo no)" "yes")" "yes"
check "not this shape: a nameless check below a comment glued to an open parenthesis" "$( (# a note &&
	check $v "$([ "$c" -le "$d" ] && echo yes || echo no)" "yes") )" "yes"
check "not this shape: a nameless check below a comment glued to a close parenthesis" "$( (true)# a note &&
	check $v "$([ "$c" -le "$d" ] && echo yes || echo no)" "yes")" "yes"
check "a pair of tests chained across a line break and a comment line that quotes a recorder" "$([ -f "$a" ] &&
	# check "x" "$([ "$a" -le "$b" ] && echo "yes $a" || echo no)" "yes"
	[ "$a" -le "$b" ] && echo yes || echo no)" "yes"
foo &&
# check "not this shape: a comment below a line that ends in and" "$([ "$a" -le "$b" ] && echo yes || echo no)" "yes"
bar
PROBE
_lam_compound_names="$(_lam_sweep "$_lam_probe_compound" | cut -f2 | LC_ALL=C sort | tr '\n' ';')"
check_text "control: a compound, a padded floor and a quoted redirect target are not excused" \
	"$_lam_compound_names" "a chain of three tests;a compound after a double-equals equality with the word -a on the left;a compound after an equality with the word -a on the left;a compound joined by a double-spaced and;a compound joined by a tab-separated and;a compound of a file test and an equality;a compound whose first half is nothing versus something;a compound whose last half is a one-word test;a compound whose last half is an equality with the word -a on the left;a compound whose last half is nothing versus something;a compound whose last operand is the word =~ in single brackets;a compound whose regex bracket class comes before the and;a compound with a negated regex bracket class of a close bracket;a compound with a regex POSIX class;a compound with a regex bracket class;a compound with a regex bracket class that starts with a close bracket;a compound with a regex group that holds a space and a bracket class;a compound with a regex group, an alternation and a bracket class;a compound with a regex that holds an escaped space;a compound with a regex whose quoted part holds a space;a compound with an equality half;a double-bracket compound after a backtick substitution;a double-bracket compound after a single-quoted operand;a double-bracket compound joined by an unspaced and;a double-bracket compound joined by or;a double-bracket compound whose last half is a floor of one;a double-bracket compound whose last operand ends in =~;a file test chained to a test builtin by a tab;a file test chained to a test-builtin count against nothing;a floor of ten;a floor over a count of an escaped alternation across a continuation;a mirrored floor of zero;a pair of determinate tests chained by and;a pair of double-bracket tests chained by and;a pair of tests chained across a line break after a backtick that holds a comment;a pair of tests chained across a line break after a comment;a pair of tests chained across a line break after a comment glued to the and;a pair of tests chained across a line break after a comment glued to the or;a pair of tests chained across a line break after a count of arguments;a pair of tests chained across a line break after a path with a hash inside;a pair of tests chained across a line break after a path with an escaped quote;a pair of tests chained across a line break after a quoted path glued to a hash;a pair of tests chained across a line break after a string length;a pair of tests chained across a line break after and;a pair of tests chained across a line break and a comment line;a pair of tests chained across a line break and a comment line that quotes a recorder;a pair of tests chained across a line break and an unindented comment line;a pair of tests chained across a line break before a trailing comment that ends in and;a pair of tests chained across a line break whose second line ends in a comment;a pair of tests chained behind a redirect;a pair of tests chained by an unspaced and;a pair of tests chained by and;a pair of tests chained by or;a pair of tests chained with a redirect joined to the second;a pair of tests chained with tabs;a pair of tests chained with two spaces before the and;a regex bracket class;a regex in a variable;a single-bracket test chained to a double-bracket test;a test-builtin compound whose last operand is glued to a redirect;a test-builtin count against nothing chained to a test builtin by or;a test-builtin threshold chained to a file test;a threshold after a count of arguments on a line that ends in and;a threshold after a line whose comment follows an and;a threshold after an array length on a line that ends in and;a threshold before a backtick that holds a hash and ends the line in and;a threshold before a nested subshell that ends the line in and;a threshold before a quoted word glued to a hash that ends the line in and;a threshold before a substitution on its line that ends in and;a threshold before a substitution on its line that ends in or;a threshold before a substitution with a subshell on its line that ends in and;a threshold before a word with a hash inside that ends the line in and;a threshold behind a redirect into a quoted path with a space;a threshold behind a redirect into a quoted path with a space and a suffix;a threshold below a comment that ends in or;a threshold below a comment that opens a substitution and a continued line;a threshold below a doubly indented comment that ends in or;a threshold below a line sent to the background;a threshold below a line that ends in an and and a command;a threshold below a trailing comment that ends in or;a threshold below an indented comment that ends in or;a threshold beside a substitution whose comment is glued to a close parenthesis;a threshold beside a substitution whose comment is glued to a semicolon;a threshold beside a substitution whose comment is glued to an open parenthesis;a threshold guarded by the line above, which ends in and;a threshold in a brace group inside a substitution on the line after one that ends in and;a threshold in a brace group on the line after a test that ends in and;a threshold in a case branch on the line after a test that ends in and;a threshold in a subshell on the line after a command that ends in and;a threshold inside a substitution before a helper whose name ends in check;a threshold inside a substitution before a helper whose name ends in underscore check;a threshold inside a substitution before a recorder glued to a semicolon;a threshold inside a substitution before a recorder whose name is a variable;a threshold on the here-doc body line after one that ends in and;a threshold on the line after a command that ends in and;a threshold on the line after a string whose first line ends in and;a threshold recorded by check_num inside a substitution on the line after one that ends in and;a threshold recorded by check_ratio inside a substitution on the line after one that ends in and;a threshold recorded by check_skip inside a substitution on the line after one that ends in and;a threshold recorded by check_text inside a substitution on the line after one that ends in and;a threshold recorded by pgc_fail inside a substitution on the line after one that ends in and;a threshold recorded inside a substitution on the line after one that ends in and;a threshold recorded unindented inside a substitution on the line after one that ends in and;a threshold whose line ends in a comment that ends in or;a threshold whose line ends in and before a carrying arm;a threshold whose line ends in and before a carrying assignment;a threshold whose line ends in and before an arm with no name;a threshold whose line ends in and before an echo of its operand;a threshold whose line ends in or before an echo of its operand;a threshold whose line opens a string that ends the line in and;a threshold whose quoted branch ends its line in or;a threshold whose substitution holds a comment line that quotes a carrying test before a recorder;a threshold with a leading zero;an if/then/else compound with a regex bracket class;an if/then/else pair of tests chained by an unspaced and;an if/then/else pair of tests chained by and;an if/then/else pair of tests chained by or;an if/then/else threshold behind a redirect into a quoted path with a space;an unquoted threshold whose line ends in and before a carrying assignment;an unquoted threshold whose line ends in and before an arm with no name;"

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
# ONE COLLATION FOR sort AND comm. Under en_US.UTF-8, comm called the lists
# unsorted and named arms that were tracked, so a red run printed wrong names.
LC_ALL=C sort -u -o "$_lam_found" "$_lam_found"

_lam_tracked="$PGC_WORKDIR/lam_tracked.tsv"
grep -v '^#' "$_lam_list" | grep -v '^[[:space:]]*$' | LC_ALL=C sort -u > "$_lam_tracked"

# A SWEEP THAT READ NOTHING REPORTS THE SAME CLEAN RESULT AS A CLEAN CORPUS, so
# assert it read the corpus before either comparison is believed.
check "premise: the sweep read the corpus and classified arms in it" \
	"$([ "$(wc -l < "$_lam_found")" -ge 20 ] && echo yes || echo "no ($(wc -l < "$_lam_found"))")" \
	"yes"

_lam_new="$(LC_ALL=C comm -23 "$_lam_found" "$_lam_tracked" | sed 's/\t/: /' | tr '\n' ';')"
check_text "a new arm that cannot say what it measured is refused by name" \
	"${_lam_new:-none}" "none"

_lam_stale="$(LC_ALL=C comm -13 "$_lam_found" "$_lam_tracked" | sed 's/\t/: /' | tr '\n' ';')"
check_text "a repaired arm must have its row removed, so the list only shrinks" \
	"${_lam_stale:-none}" "none"

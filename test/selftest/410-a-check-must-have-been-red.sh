# ---- a check must have been seen red, or be counted as debt -----------------
#
# Nothing recorded whether a check had ever been red. That is the gap that let 39
# checks across 35 suites ship unable to fail, three of them inside this very
# suite. The gate answered "did anything print FAIL" and had never answered
# "could anything print FAIL".
#
# WHAT THIS RECORDS, AND WHAT IT DOES NOT. It records that a named check WAS
# OBSERVED RED in a recorded run. It does NOT claim the check is proven able to
# fail: that needs a named mutation applied deliberately, and conflating the two
# would put a claim in the ledger that nothing measured.
#
# THE FIRST DESIGN DEADLOCKED AND THE SECOND DOES NOT. Bounding
# `checks_never_observed_red` means every added check breaks the gate, because a
# new check enters as `never` -- so the only way to land one was to raise a number
# the design said may only fall. It shipped at 614 rows, 614 never, ceiling 614.
# It is now a CENSUS, asserted to match the ledger; the CEILING is
# `suites_not_covered`, which adding a check does not move.
#
# WHAT THE GATE REFUSES is a check the committed ledger has never seen. Existing
# checks are grandfathered; a new one is named, and regenerating the ledger is the
# INTENDED fix rather than a forbidden edit.
# ---------------------------------------------------------------------------

_led="$PGC_TESTDIR/pgc_ledger.py"
_ledger="$PGC_TESTDIR/check_ledger.tsv"
_budget="$PGC_TESTDIR/check_ledger_budget.txt"

check "premise: the ledger tool exists" "$([ -f "$_led" ] && echo yes || echo no)" "yes"
check "premise: the ledger itself is a tracked file, not a variable" \
	"$([ -f "$_ledger" ] && echo yes || echo no)" "yes"
check "premise: the budget is a tracked file too" \
	"$([ -f "$_budget" ] && echo yes || echo no)" "yes"

_lw="$PGC_WORKDIR/ledger"; mkdir -p "$_lw"
_led_run() { python3 "$_led" "$@" 2>&1; }
_led_rc()  { python3 "$_led" "$@" >/dev/null 2>&1; echo $?; }

printf 'RESULT\tdemo\tpart1\tfirst check\tPASS\t\nRESULT\tdemo\tpart1\tsecond check\tPASS\t\nchecks run: 2\n' > "$_lw/green.log"
printf 'RESULT\tdemo\tpart1\tfirst check\tFAIL\t\nRESULT\tdemo\tpart1\tsecond check\tPASS\t\nchecks run: 2\n' > "$_lw/red.log"
printf 'demo\n' > "$_lw/registered"
printf 'suites_not_covered 0\n' > "$_lw/budget.txt"

# ---- fail closed. Every one of these returned rc=0 before -------------------
#
# read_records ignored unreadable files, empty ones and short records, so a gate
# over a NONEXISTENT log reported success. An integrity failure that reads as a
# clean run is worse than no gate, because it certifies. Reported by @linuxhikerpm.

: > "$_lw/empty.log"
printf 'RESULT\tdemo\tpart1\tname\n' > "$_lw/short.log"
: > "$_lw/l.tsv"
check "a gate over a nonexistent log is an integrity failure, not a pass" \
	"$(_led_rc gate --ledger "$_lw/l.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/nope.log")" "2"
check "an empty log is one too, because there is nothing to reconcile" \
	"$(_led_rc gate --ledger "$_lw/l.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/empty.log")" "2"
check "and a record missing its verdict" \
	"$(_led_rc gate --ledger "$_lw/l.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/short.log")" "2"
check "each says what was wrong with the input" \
	"$(_led_run gate --ledger "$_lw/l.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/short.log" \
		| grep -c 'a record has 5 fields')" "1"

# ---- the count is not the schema, on INGESTION as well as in the runner ------
#
# read_records accepted `len(f) >= 5`, so a record missing its reason, a verdict
# outside pgc_record's vocabulary, an empty check name, and one record against
# `checks run: 2` all merged at rc=0 -- the ledger absorbing as evidence a log
# that does not parse. The runner reconciles the suite it just ran; this
# reconciles a log handed to the ledger, possibly from another machine.
# Reported by @linuxhikerpm on #918.
printf 'RESULT\tdemo\tpart1\ta name\tBOGUS\t\nchecks run: 1\n' > "$_lw/bogus.log"
printf 'RESULT\tdemo\tpart1\t\tPASS\t\nchecks run: 1\n'        > "$_lw/noname.log"
printf 'RESULT\tdemo\tpart1\ta name\tPASS\t\nchecks run: 2\n'  > "$_lw/miscount.log"

check "a verdict pgc_record cannot emit is an integrity failure" \
	"$(_led_rc merge --ledger "$_lw/v.tsv" --date 2026-09-10 "$_lw/bogus.log")" "2"
check "and it names the verdict, so the author knows which record" \
	"$(_led_run merge --ledger "$_lw/v.tsv" --date 2026-09-10 "$_lw/bogus.log" | grep -c "'BOGUS'")" "1"
check "an empty check name is one too, because it names no check" \
	"$(_led_rc merge --ledger "$_lw/v.tsv" --date 2026-09-10 "$_lw/noname.log")" "2"
check "and a log that does not reconcile with its own checks run:" \
	"$(_led_rc merge --ledger "$_lw/v.tsv" --date 2026-09-10 "$_lw/miscount.log")" "2"

# THE CONTROL, because four arms all saying 2 prove nothing if the tool has
# started refusing everything.
check "control: a well-formed log still merges" \
	"$(_led_rc merge --ledger "$_lw/v.tsv" --date 2026-09-10 "$_lw/green.log")" "0"

# ---- last red may only move forward -----------------------------------------
#
# It was a plain assignment: merging an older log rewrote a recent observation
# with an older one, and merging an undated log replaced a real date with
# `unknown`. A free-form --date was accepted verbatim, so a typo became an
# observation date the ledger treated as authoritative.
: > "$_lw/date.tsv"
_led_run merge --reds-are-real --ledger "$_lw/date.tsv" --date 2026-09-10 "$_lw/red.log" >/dev/null
_led_run merge --reds-are-real --ledger "$_lw/date.tsv" --date 2026-09-01 "$_lw/red.log" >/dev/null
check "an older observation does not overwrite a newer one" \
	"$(awk -F'\t' '$3=="first check"{print $4}' "$_lw/date.tsv")" "2026-09-10"
_led_run merge --reds-are-real --ledger "$_lw/date.tsv" --date 2026-09-20 "$_lw/red.log" >/dev/null
check "and a newer one does" \
	"$(awk -F'\t' '$3=="first check"{print $4}' "$_lw/date.tsv")" "2026-09-20"
_led_run merge --reds-are-real --ledger "$_lw/date.tsv" "$_lw/red.log" >/dev/null
check "and an undated merge does not erase a known date" \
	"$(awk -F'\t' '$3=="first check"{print $4}' "$_lw/date.tsv")" "2026-09-20"
check "a date that is not a date is refused rather than stored" \
	"$(_led_rc merge --ledger "$_lw/date.tsv" --date not-a-date "$_lw/red.log")" "2"

# ---- a mutation names ONE check ---------------------------------------------
#
# A run that mutates one thing can redden several: the target, plus whatever
# depended on it. Attributing --mutation to every failure records collateral
# damage as evidence that the mutation kills that check.
printf 'RESULT\tdemo\tpart1\tthe target\tFAIL\t\nRESULT\tdemo\tpart1\tcollateral\tFAIL\t\nchecks run: 2\n' \
	> "$_lw/twofail.log"
check "--mutation across two failing checks in one run is refused" \
	"$(_led_rc merge --ledger "$_lw/m2.tsv" --date 2026-09-10 --mutation M "$_lw/twofail.log")" "2"
check "and the refusal names how many failed, so the author can narrow the run" \
	"$(_led_run merge --ledger "$_lw/m2.tsv" --date 2026-09-10 --mutation M "$_lw/twofail.log" \
		| grep -c '2 checks failed')" "1"
check "control: the same log merges with a different reason, so the refusal above is --mutation-across-two-checks and not the log" \
	"$(_led_rc merge --reds-are-real --ledger "$_lw/m2.tsv" --date 2026-09-10 "$_lw/twofail.log")" "0"

# The three must be distinguishable from a REAL refusal, or fail-closed just
# renames every outcome.
check "a real refusal is a different status from an integrity failure" \
	"$(_led_rc gate --ledger "$_lw/l.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/green.log")" "1"

# --registered is required. Skipping it silently is how a gate reports success
# for a question it never asked.
check "the gate refuses to run without the registered suite list" \
	"$(_led_rc gate --ledger "$_lw/l.tsv" --budget "$_lw/budget.txt" "$_lw/green.log")" "2"

# ---- the census: a green run records debt and never a red observation -------

: > "$_lw/ledger.tsv"
_led_run merge --ledger "$_lw/ledger.tsv" --date 2026-09-10 "$_lw/green.log" >/dev/null
check "merging a green run records both checks" "$(grep -c . "$_lw/ledger.tsv")" "2"
check "and records neither as ever having been red" \
	"$(cut -f4 "$_lw/ledger.tsv" | sort -u | tr '\n' ' ')" "never "
check "every row has five fields and no trailing tab" \
	"$(awk -F'\t' 'NF!=5' "$_lw/ledger.tsv" | grep -c . || true)" "0"
check "and an empty mutation is a placeholder, not an empty last field" \
	"$(grep -cP '\t$' "$_lw/ledger.tsv" || true)" "0"

_led_run merge --reds-are-real --ledger "$_lw/ledger.tsv" --date 2026-09-10 "$_lw/red.log" >/dev/null
check "a check observed red gains the date it was seen" \
	"$(awk -F'\t' '$3=="first check"{print $4}' "$_lw/ledger.tsv")" "2026-09-10"
check "and one that stayed green keeps its debt" \
	"$(awk -F'\t' '$3=="second check"{print $4}' "$_lw/ledger.tsv")" "never"
_led_run merge --ledger "$_lw/ledger.tsv" --date 2026-09-11 "$_lw/green.log" >/dev/null
check "a later green run does not erase an observation" \
	"$(awk -F'\t' '$3=="first check"{print $4}' "$_lw/ledger.tsv")" "2026-09-10"

# ---- the mutation column ACCUMULATES ----------------------------------------
#
# Last-write-wins records the most recent attack rather than the catalogue the
# column exists to become, which defeats its stated purpose rather than limiting
# it. And one --mutation value copied across several logs attributes a deliberate
# change to failures it had nothing to do with. Both reported by @linuxhikerpm.

: > "$_lw/mut.tsv"
_led_run merge --ledger "$_lw/mut.tsv" --date 2026-09-10 --mutation 'SAOP limit 128 -> 0' "$_lw/red.log" >/dev/null
check "a named mutation is recorded against the check that reddened" \
	"$(awk -F'\t' '$3=="first check"{print $5}' "$_lw/mut.tsv")" "SAOP limit 128 -> 0"
check "and not against one that stayed green" \
	"$(awk -F'\t' '$3=="second check"{print $5}' "$_lw/mut.tsv")" "-"
_led_run merge --ledger "$_lw/mut.tsv" --date 2026-09-10 --mutation 'bloom neutered' "$_lw/red.log" >/dev/null
check "a second mutation ACCUMULATES rather than replacing the first" \
	"$(awk -F'\t' '$3=="first check"{print $5}' "$_lw/mut.tsv")" "SAOP limit 128 -> 0;bloom neutered"
check "one --mutation cannot be attributed across several runs at once" \
	"$(_led_rc merge --ledger "$_lw/mut.tsv" --date 2026-09-10 --mutation X "$_lw/red.log" "$_lw/green.log")" "2"

# ---- two runs of a check are not a duplicate of it --------------------------
#
# Merging the logs first cannot tell "the same check in two runs" from "the same
# name twice in one run", and reported the first as the second.

: > "$_lw/dup.tsv"
check "the same check in two logs is two runs, not a duplicate" \
	"$(_led_run merge --ledger "$_lw/dup.tsv" --date 2026-09-10 "$_lw/green.log" "$_lw/green.log" | grep -c 'duplicate')" "0"
printf 'RESULT\tdemo\tpart1\tsame\tPASS\t\nRESULT\tdemo\tpart1\tsame\tFAIL\t\nchecks run: 2\n' > "$_lw/twice.log"
: > "$_lw/dup2.tsv"
check "the same name twice in ONE log is a duplicate, and is named" \
	"$(_led_run merge --reds-are-real --ledger "$_lw/dup2.tsv" --date 2026-09-10 "$_lw/twice.log" \
		| grep -c 'duplicate check name in one run, so one ledger row covers 2: demo	part1	same')" "1"

# ---- renames, grouped by part and scanned against ONE run -------------------
#
# A global positional pairing misses a real rename whenever unrelated movement in
# another part shifts the ordering. And given a before-log and an after-log
# together, the vanished name is present in the union and nothing appears to have
# gone -- a scan that silently finds nothing is worse than one that refuses.

: > "$_lw/ren.tsv"
printf 'RESULT\tdemo\tpart1\tthe old name\tFAIL\t\nRESULT\tdemo\tpart1\ta stable check\tPASS\t\nchecks run: 2\n' > "$_lw/before.log"
printf 'RESULT\tdemo\tpart1\tthe new name\tPASS\t\nRESULT\tdemo\tpart1\ta stable check\tPASS\t\nchecks run: 2\n' > "$_lw/after.log"
_led_run merge --reds-are-real --ledger "$_lw/ren.tsv" --date 2026-09-01 "$_lw/before.log" >/dev/null
check "premise: the check has history before the rename" \
	"$(awk -F'\t' '$3=="the old name"{print $4}' "$_lw/ren.tsv")" "2026-09-01"
check "a name that appeared while another disappeared is reported as a rename" \
	"$(_led_run rename-scan --ledger "$_lw/ren.tsv" "$_lw/after.log" \
		| grep -c 'possible rename: the old name -> the new name')" "1"
check "and the history it is about to lose travels with it" \
	"$(_led_run rename-scan --ledger "$_lw/ren.tsv" "$_lw/after.log" | grep -c 'last red 2026-09-01')" "1"
check "the stable check is not reported" \
	"$(_led_run rename-scan --ledger "$_lw/ren.tsv" "$_lw/after.log" | grep -c 'a stable check')" "0"
check "a before-log and an after-log together are refused, not silently empty" \
	"$(_led_rc rename-scan --ledger "$_lw/ren.tsv" "$_lw/before.log" "$_lw/after.log")" "2"

# Movement in ANOTHER part must not consume this part's pairing. That is what a
# global positional zip gets wrong, and it fails silently.
: > "$_lw/ren2.tsv"
printf 'RESULT\tdemo\tpartA\told A\tPASS\t\nRESULT\tdemo\tpartB\tstable B\tPASS\t\nchecks run: 2\n' > "$_lw/b2.log"
printf 'RESULT\tdemo\tpartA\tnew A\tPASS\t\nRESULT\tdemo\tpartB\tstable B\tPASS\t\nRESULT\tdemo\tpartB\tadded B\tPASS\t\nchecks run: 3\n' > "$_lw/a2.log"
_led_run merge --ledger "$_lw/ren2.tsv" --date 2026-09-10 "$_lw/b2.log" >/dev/null
check "a rename in one part survives an addition in another" \
	"$(_led_run rename-scan --ledger "$_lw/ren2.tsv" "$_lw/a2.log" \
		| grep -c 'possible rename: old A -> new A')" "1"
check "and the addition in the other part is not called a rename" \
	"$(_led_run rename-scan --ledger "$_lw/ren2.tsv" "$_lw/a2.log" | grep -c 'added B')" "0"

# A check merely added, or merely removed, is not a rename.
printf 'RESULT\tdemo\tpart1\tthe old name\tPASS\t\nRESULT\tdemo\tpart1\ta stable check\tPASS\t\nRESULT\tdemo\tpart1\tbrand new\tPASS\t\nchecks run: 3\n' > "$_lw/added.log"
check "a check merely added is not reported as a rename" \
	"$(_led_run rename-scan --ledger "$_lw/ren.tsv" "$_lw/added.log" | grep -c 'possible rename')" "0"
printf 'RESULT\tdemo\tpart1\ta stable check\tPASS\t\nchecks run: 1\n' > "$_lw/removed.log"
check "nor is one merely removed" \
	"$(_led_run rename-scan --ledger "$_lw/ren.tsv" "$_lw/removed.log" | grep -c 'possible rename')" "0"

# ---- the gate refuses a check the ledger has never seen ---------------------

: > "$_lw/g.tsv"
_led_run merge --ledger "$_lw/g.tsv" --date 2026-09-10 "$_lw/green.log" >/dev/null
printf 'suites_not_covered 0\n' > "$_lw/gb.txt"
check "a run whose checks are all ledgered passes the gate" \
	"$(_led_rc gate --ledger "$_lw/g.tsv" --budget "$_lw/gb.txt" --registered "$_lw/registered" "$_lw/green.log")" "0"
printf 'RESULT\tdemo\tpart1\tfirst check\tPASS\t\nRESULT\tdemo\tpart1\tsecond check\tPASS\t\nRESULT\tdemo\tpart1\tbrand new\tPASS\t\nchecks run: 3\n' > "$_lw/new.log"
check "a check the ledger has never seen is refused" \
	"$(_led_rc gate --ledger "$_lw/g.tsv" --budget "$_lw/gb.txt" --registered "$_lw/registered" "$_lw/new.log")" "1"
check "and it is named, so the author knows which one" \
	"$(_led_run gate --ledger "$_lw/g.tsv" --budget "$_lw/gb.txt" --registered "$_lw/registered" "$_lw/new.log" \
		| grep -c 'not in the ledger: demo	part1	brand new')" "1"
check "and the message says how to fix it, because regenerating is the intended action" \
	"$(_led_run gate --ledger "$_lw/g.tsv" --budget "$_lw/gb.txt" --registered "$_lw/registered" "$_lw/new.log" \
		| grep -c 'Regenerate it with')" "1"

# THE DEADLOCK THAT SHIPPED, as its own arm. Adding a check must not require an
# edit the design forbids.
_led_run merge --ledger "$_lw/g.tsv" --date 2026-09-10 "$_lw/new.log" >/dev/null
check "regenerating the ledger lets the new check through" \
	"$(_led_rc gate --ledger "$_lw/g.tsv" --budget "$_lw/gb.txt" --registered "$_lw/registered" "$_lw/new.log")" "0"
check "and it entered as debt, not as an observation nothing made" \
	"$(awk -F'\t' '$3=="brand new"{print $4}' "$_lw/g.tsv")" "never"

# ---- the ceiling is monotone, mechanically ----------------------------------
#
# The file says the ceiling may only fall. Without this the sentence is prose:
# raising the number passed. Measured against a prior value from git rather than
# taken on trust.

check "the ceiling refuses being exceeded" \
	"$(printf 'suites_not_covered 0\n' > "$_lw/gb0.txt"
	   printf 'other\ndemo\n' > "$_lw/reg2"
	   _led_rc gate --ledger "$_lw/g.tsv" --budget "$_lw/gb0.txt" --registered "$_lw/reg2" "$_lw/new.log")" "1"

_lg="$_lw/repo"; rm -rf "$_lg"; mkdir -p "$_lg"
( cd "$_lg" && git init -q . && git config user.email t@t && git config user.name t
  printf 'suites_not_covered 5\n' > b.txt && git add b.txt && git commit -qm base ) >/dev/null 2>&1
check "premise: the scratch repo has a prior ceiling committed" \
	"$(cd "$_lg" && git show HEAD:b.txt | grep -c 'suites_not_covered 5')" "1"
printf 'suites_not_covered 9\n' > "$_lg/b.txt"
check "raising the ceiling above its committed value is refused" \
	"$(cd "$_lg" && _led_rc gate --ledger "$_lw/g.tsv" --budget b.txt \
		--registered "$_lw/registered" --against HEAD "$_lw/new.log")" "1"
check "and the refusal names both values" \
	"$(cd "$_lg" && _led_run gate --ledger "$_lw/g.tsv" --budget b.txt \
		--registered "$_lw/registered" --against HEAD "$_lw/new.log" \
		| grep -c 'was raised from 5 to 9')" "1"
printf 'suites_not_covered 3\n' > "$_lg/b.txt"
check "lowering it is allowed, which is the direction the burn-down goes" \
	"$(cd "$_lg" && _led_rc gate --ledger "$_lw/g.tsv" --budget b.txt \
		--registered "$_lw/registered" --against HEAD "$_lw/new.log")" "0"

# ---- and the RUNNER must invoke it ------------------------------------------
#
# A gate nothing runs is a comment, which is selftest 350's phrasing about its own
# subject. Nothing in the repository called this tool: zero references in
# .github/, zero in the runner. Reported by @linuxhikerpm and by OffgridwithJD
# independently.

check "the runner invokes the ledger gate" \
	"$(grep -c 'pgc_ledger.py" gate' "$_rv")" "1"
# ---- and the logs outlive the build directory -------------------------------
#
# The gate runs, then `rm -rf "$builddir"` runs, and CI's collection step globs
# the build directory AFTER the loop has finished -- so it searched a path that
# had already been removed and collected nothing. The ledger is fed by merging
# real logs, and a CI red is exactly the run that first records a check going
# red, so deleting them meant CI could never feed the thing it gates.
# Reported by @linuxhikerpm on #918.
check "the runner keeps the logs somewhere that outlives the build directory" \
	"$(grep -c 'cp -p "\$_l" "\$_logkeep' "$_rv")" "1"
check "and it copies them BEFORE removing the build directory, which is the only order that works" \
	"$([ "$(grep -n 'cp -p "\$_l" "\$_logkeep' "$_rv" | cut -d: -f1)" -lt \
	    "$(grep -n 'rm -rf "\$builddir"' "$_rv" | tail -1 | cut -d: -f1)" ] && echo before || echo after)" "before"
check "and CI collects from the retained path rather than the deleted one" \
	"$(grep -c '/tmp/pgcolumnar-logs/\*\.log' "$PGC_SRCDIR/.github/workflows/ci.yml")" "1"

check "and it runs before the build directory is removed, which is the only place it can" \
	"$([ "$(grep -n 'pgc_ledger.py" gate' "$_rv" | cut -d: -f1)" -lt \
	    "$(grep -n 'rm -rf "\$builddir"' "$_rv" | tail -1 | cut -d: -f1)" ] && echo before || echo after)" "before"

# ---- the committed files agree ----------------------------------------------

_l_total="$(grep -c . "$_ledger" || true)"
_l_red="$(awk -F'\t' '$4!="never"' "$_ledger" | grep -c . || true)"
_l_never="$(awk -F'\t' '$4=="never"' "$_ledger" | grep -c . || true)"
echo "  ledger: inputs=$_l_total | observed red=$_l_red, never=$_l_never | sum=$((_l_red + _l_never))"
check "the ledger partitions into observed and never" "$((_l_red + _l_never))" "$_l_total"
check "premise: the ledger is not empty, so the partition means something" \
	"$([ "$_l_total" -gt 0 ] && echo yes || echo no)" "yes"
check "every committed row has five fields" \
	"$(awk -F'\t' 'NF!=5' "$_ledger" | grep -c . || true)" "0"
check "and none of them ends in a tab" "$(grep -cP '\t$' "$_ledger" || true)" "0"
check "the committed census matches the committed ledger" \
	"$(sed -n 's/^checks_never_observed_red //p' "$_budget")" "$_l_never"
check "the budget names a ceiling and a census, and says which is which" \
	"$(grep -cE '^(suites_not_covered|checks_never_observed_red) [0-9]+$' "$_budget")" "2"

# ---- the gate cannot refuse a check in a suite it has never seen -------------
#
# The suite restriction is the MEANING of suites_not_covered, not a softening of
# the refusal. Without it the gate refuses every check of all 250 uncovered
# suites and reddens the whole matrix on its first run -- a gate somebody turns
# off within the week, which is the failure this issue family exists to prevent.
#
# It tightens on its own as suites are seeded, and the ceiling forces that
# direction.

printf 'RESULT\tother\tpartX\tsomething\tPASS\t\nchecks run: 1\n' > "$_lw/othersuite.log"
printf 'demo\nother\n' > "$_lw/reg_both"
printf 'suites_not_covered 1\n' > "$_lw/gb1.txt"
check "a check in an UNCOVERED suite is not refused" \
	"$(_led_rc gate --ledger "$_lw/g.tsv" --budget "$_lw/gb1.txt" --registered "$_lw/reg_both" "$_lw/othersuite.log")" "0"
check "but that suite is counted as not covered, which is the debt" \
	"$(_led_run gate --ledger "$_lw/g.tsv" --budget "$_lw/gb1.txt" --registered "$_lw/reg_both" "$_lw/othersuite.log" \
		| grep -c 'not covered=1')" "1"

# And once the suite IS covered, a new check in it is refused again -- the
# restriction tightens rather than exempting the suite forever.
_led_run merge --ledger "$_lw/g.tsv" --date 2026-09-10 "$_lw/othersuite.log" >/dev/null
printf 'RESULT\tother\tpartX\tsomething\tPASS\t\nRESULT\tother\tpartX\tnewly added\tPASS\t\nchecks run: 2\n' > "$_lw/other2.log"
printf 'suites_not_covered 0\n' > "$_lw/gb2.txt"
check "once the suite is covered, a new check in it IS refused" \
	"$(_led_rc gate --ledger "$_lw/g.tsv" --budget "$_lw/gb2.txt" --registered "$_lw/reg_both" "$_lw/other2.log")" "1"
check "and it is the new one that is named, not the one already ledgered" \
	"$(_led_run gate --ledger "$_lw/g.tsv" --budget "$_lw/gb2.txt" --registered "$_lw/reg_both" "$_lw/other2.log" \
		| grep -c 'not in the ledger: other	partX	newly added')" "1"

# ---- the monotone check, in the REAL tree, with the REAL path ---------------
#
# The scratch-repo arms above prove the TOOL. They do not prove the WIRING, and
# the two came apart exactly the way the gate-nothing-invokes finding did one
# level down. OffgridwithJD measured it on the shipped form:
#
#   the runner's exact invocation, no --against    rc=0   the raise is not refused
#   --against HEAD, absolute path                  rc=0   "no prior ceiling to compare"
#   --against HEAD, repo-relative path             rc=1   correctly refused
#
# The middle line is the dangerous one: asked to compare, unable to compare, and
# it printed a note that reads like a pass. `git show REF:PATH` needs a
# repo-relative path, and the runner passes an absolute one inside a copied build
# directory.
#
# So the tool resolves the path itself, and every failure to resolve it is an
# ERROR rather than a shrug. These arms use the REAL budget at its real path.

_mono_budget="$PGC_TESTDIR/check_ledger_budget.txt"
_mono_reg="$_lw/mono_reg"
cut -f1 "$_ledger" | sort -u > "$_mono_reg"
_mono_log="$_lw/mono.log"
awk -F'\t' 'NR<=2 {printf "RESULT\t%s\t%s\t%s\tPASS\t\n", $1, $2, $3}' "$_ledger" > "$_mono_log"
# A log states its own count. read_records reconciles the two, so a fixture
# without this line is not a log the ledger will accept -- which is the point.
printf 'checks run: %s\n' "$(grep -c '^RESULT' "$_mono_log")" >> "$_mono_log"

check "premise: the real budget is inside a git repository" \
	"$(git -C "$PGC_TESTDIR" rev-parse --show-toplevel >/dev/null 2>&1 && echo yes || echo no)" "yes"
check "premise: the fixture log names checks the real ledger already knows" \
	"$(_led_rc gate --ledger "$_ledger" --budget "$_mono_budget" --registered "$_mono_reg" "$_mono_log")" "0"

# The absolute path the runner passes must WORK, not fail open.
check "an absolute budget path resolves against git rather than shrugging" \
	"$(_led_run gate --ledger "$_ledger" --budget "$_mono_budget" --registered "$_mono_reg" \
		--against HEAD "$_mono_log" | grep -c 'ceiling against HEAD')" "1"
check "and the comparison passes when the ceiling did not rise" \
	"$(_led_rc gate --ledger "$_ledger" --budget "$_mono_budget" --registered "$_mono_reg" \
		--against HEAD "$_mono_log")" "0"

# A raise in the real tracked file, at its real path, must redden.
_mono_raised="$PGC_TESTDIR/check_ledger_budget.txt.raised"
sed 's/^suites_not_covered [0-9]*$/suites_not_covered 9999/' "$_mono_budget" > "$_mono_raised"
check "premise: the raised copy really does carry a higher ceiling" \
	"$(sed -n 's/^suites_not_covered //p' "$_mono_raised")" "9999"
check "premise: and it is a file git has never seen, which is the case that used to fail open" \
	"$(git -C "$PGC_TESTDIR" show "HEAD:test/check_ledger_budget.txt.raised" >/dev/null 2>&1 && echo tracked || echo untracked)" "untracked"
# A FILE THAT DOES NOT EXIST AT THE PRIOR HAS NO CEILING TO VIOLATE, so it is a
# NOTE rather than an error. Introducing the budget is not raising it.
#
# The first version made it an error and the gate caught its own bootstrap the
# first time it ran in CI: #925's base is #923's branch, where the budget does not
# exist because this change adds it, so `auto` resolved the base correctly, found
# no prior, failed closed, and reddened the matrix. A PR introducing the file could
# never pass its own gate.
#
# It is not a hole: deleting the budget on a branch and re-adding it higher does
# not reach here, because the file still exists at the prior and the comparison
# happens. Only a genuinely new file gets the note.
check "a budget that does not exist at the prior is a note, not a refusal" \
	"$(_led_rc gate --ledger "$_ledger" --budget "$_mono_raised" --registered "$_mono_reg" \
		--against HEAD "$_mono_log")" "0"
check "and it says the change introduces the file rather than raising anything" \
	"$(_led_run gate --ledger "$_ledger" --budget "$_mono_raised" --registered "$_mono_reg" \
		--against HEAD "$_mono_log" | grep -c 'this change introduces it')" "1"
rm -f "$_mono_raised"

# And the raise itself, on the tracked path, by rewriting it in place and putting
# it back byte-exact.
_mono_orig="$_lw/budget.orig"
cp "$_mono_budget" "$_mono_orig"
sed -i 's/^suites_not_covered [0-9]*$/suites_not_covered 9999/' "$_mono_budget"
_mono_rc="$(_led_rc gate --ledger "$_ledger" --budget "$_mono_budget" --registered "$_mono_reg" \
	--against HEAD "$_mono_log")"
_mono_out="$(_led_run gate --ledger "$_ledger" --budget "$_mono_budget" --registered "$_mono_reg" \
	--against HEAD "$_mono_log" | grep -c 'was raised from')"
cp "$_mono_orig" "$_mono_budget"
check "raising the ceiling in the tracked file is refused" "$_mono_rc" "1"
check "and the refusal names the raise" "$_mono_out" "1"
check "premise: the budget was restored byte-exact" \
	"$(cmp -s "$_mono_orig" "$_mono_budget" && echo same || echo CHANGED)" "same"

# ---- and the RUNNER must pass --against, or none of the above is wired -------

# The ref is a variable, chosen above the call and pinned by the three arms at
# the end of this part. What matters here is that the call site passes one at all:
# without --against the monotone block never runs, which is how the tool was right
# and the wiring was not.
check "the runner passes --against to the gate" \
	"$(grep -A5 'pgc_ledger.py" gate' "$_rv" | grep -c -- '--against')" "1"



# ---- the prior is resolved, never named ------------------------------------
#
# `origin` is not a fixed thing. In a contributor's clone it is their FORK, and
# OffgridwithJD measured theirs 446 commits behind upstream. Comparing the ceiling
# against a stale main makes this check WEAKER rather than falsely red -- the
# ceiling may only fall, so an older main carries a higher one, and a raise passes
# whenever the stale prior is high enough.
#
# It fails open while printing a line that reads like the enforcement happened.
# That is the same shape as the absolute-path bug this part already carries, with
# "compared against the wrong thing" in place of "could not compare".
#
# So the tool resolves it: GITHUB_BASE_REF in CI, which names the PR's target and
# IS the prior by definition; the local main's configured upstream outside CI,
# which is the per-clone answer to "which main is mine". Neither available is an
# ERROR, because a fallback that enforces less while saying so is still a gate
# enforcing less.

check "the runner asks the tool to resolve the prior rather than naming one" \
	"$(grep -A5 'pgc_ledger.py" gate' "$_rv" | grep -c -- '--against auto')" "1"
check "and the runner names no remote at that call site" \
	"$(grep -A6 'pgc_ledger.py" gate' "$_rv" | grep -c 'origin/main')" "0"

_res="$_lw/resolve"; rm -rf "$_res"; mkdir -p "$_res"
( cd "$_res" && git init -q . && git config user.email t@t && git config user.name t
  printf 'suites_not_covered 7\n' > b.txt && git add b.txt && git commit -qm base ) >/dev/null 2>&1
printf 'suites_not_covered 7\n' > "$_res/b.txt"

check "premise: the scratch repo has a committed ceiling and no upstream" \
	"$(cd "$_res" && git rev-parse --abbrev-ref main@{upstream} 2>/dev/null || echo none)" "none"
check "auto with no base ref and no upstream is an integrity failure" \
	"$(cd "$_res" && env -u GITHUB_BASE_REF python3 "$_led" gate --ledger "$_lw/g.tsv" \
		--budget b.txt --registered "$_lw/registered" --against auto "$_lw/new.log" \
		>/dev/null 2>&1; echo $?)" "2"
check "and it says why, rather than falling back to something weaker" \
	"$(cd "$_res" && env -u GITHUB_BASE_REF python3 "$_led" gate --ledger "$_lw/g.tsv" \
		--budget b.txt --registered "$_lw/registered" --against auto "$_lw/new.log" 2>&1 \
		| grep -c 'no trustworthy prior ceiling')" "1"

# GITHUB_BASE_REF names a branch whose ref must actually exist. A CI checkout that
# did not fetch the base is an error the workflow fixes, not one the author works
# around.
check "auto refuses when GITHUB_BASE_REF names a ref that is not here" \
	"$(cd "$_res" && GITHUB_BASE_REF=nosuchbranch python3 "$_led" gate --ledger "$_lw/g.tsv" \
		--budget b.txt --registered "$_lw/registered" --against auto "$_lw/new.log" 2>&1 \
		| grep -ci 'the checkout needs to fetch the base branch')" "1"

# And when it IS here, that is the ref used -- named in the output, so a reader
# can see which prior the comparison actually made.
( cd "$_res" && git branch -q basebranch && git update-ref refs/remotes/origin/basebranch \
	"$(git rev-parse HEAD)" ) >/dev/null 2>&1
check "auto uses the base ref when it resolves, and names it" \
	"$(cd "$_res" && GITHUB_BASE_REF=basebranch python3 "$_led" gate --ledger "$_lw/g.tsv" \
		--budget b.txt --registered "$_lw/registered" --against auto "$_lw/new.log" 2>&1 \
		| grep -c 'ceiling against refs/remotes/origin/basebranch')" "1"
printf 'suites_not_covered 99\n' > "$_res/b.txt"
check "and a raise against that base ref is refused" \
	"$(cd "$_res" && GITHUB_BASE_REF=basebranch python3 "$_led" gate --ledger "$_lw/g.tsv" \
		--budget b.txt --registered "$_lw/registered" --against auto "$_lw/new.log" 2>&1 \
		| grep -c 'was raised from 7 to 99')" "1"

# ---- and CI must fetch that base, or the gate stops the run ----------------

_ci="$PGC_SRCDIR/.github/workflows/ci.yml"
check "premise: the workflow file is where this part thinks it is" \
	"$([ -f "$_ci" ] && echo yes || echo no)" "yes"
check "the suites job fetches the PR base for the ceiling comparison" \
	"$(grep -c 'fetch the PR base, for the ledger ceiling comparison' "$_ci")" "1"
check "and only when there is a base, so a push build does not fail on it" \
	"$(grep -A1 'fetch the PR base, for the ledger ceiling comparison' "$_ci" \
		| grep -c "github.base_ref != ''")" "1"

# ---- and how far behind the prior is, printed beside it ---------------------
#
# `main@{upstream}` is the per-clone answer to "which main is mine", and in a
# contributor's setup it resolves to their FORK. OffgridwithJD measured theirs 446
# commits behind upstream -- and `git push -u origin main` is what sets that
# config, so `auto` outside CI lands on exactly the ref the hardcoded version did.
# The route changed; the destination did not.
#
# There is no better ref to pick that does not guess, so the weakness is made
# VISIBLE rather than removed. Naming the ref told a reader WHICH prior was used;
# it did not tell them what the comparison was worth. The direction still fails
# open: an older main carries a higher ceiling, so a raise passes whenever the
# stale prior is high enough.
#
# Their suggestion, and it costs nothing when the number is 0.

_dist="$_lw/dist"; rm -rf "$_dist"; mkdir -p "$_dist"
( cd "$_dist" && git init -q . && git config user.email t@t && git config user.name t
  printf 'suites_not_covered 7\n' > b.txt && git add b.txt && git commit -qm base
  git branch -q oldbase
  for i in 1 2 3; do echo "x$i" > f; git add f; git commit -qm "c$i"; done ) >/dev/null 2>&1
printf 's\tp\ta\tnever\t-\n' > "$_dist/led"
printf 'RESULT\ts\tp\ta\tPASS\t\nchecks run: 1\n' > "$_dist/log"
printf 's\n' > "$_dist/reg"

check "premise: the scratch prior really is three commits behind" \
	"$(cd "$_dist" && git rev-list --count oldbase..HEAD)" "3"
check "a stale prior is named WITH its distance from HEAD" \
	"$(cd "$_dist" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against oldbase log 2>&1 | grep -c 'ceiling against oldbase (3 commits behind HEAD)')" "1"
check "and a level prior carries no distance, so zero is silent" \
	"$(cd "$_dist" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against HEAD log 2>&1 | grep -c 'ceiling against HEAD: ')" "1"
check "the distance travels with a refusal too, not only with a pass" \
	"$(cd "$_dist" && sed -i 's/^suites_not_covered 7$/suites_not_covered 99/' b.txt
	   python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against oldbase log 2>&1 | grep -c 'against oldbase (3 commits behind HEAD)')" "1"

# ---- the runner must not collapse the two failure kinds ---------------------
#
# The gate distinguishes rc=1, a real refusal whose fix is to regenerate the
# ledger, from rc=2, the gate unable to do its job at all. The runner reported
# both as "has a check the ledger has never seen", which sends the reader at a
# repair that cannot help -- and printed it three lines below the gate's own
# "new this run=0", which says the opposite. Reported by OffgridwithJD from the
# CI log of this very branch.

# The block is EXTRACTED and tested, not counted inside a -A window. A window's
# size is a fact about formatting: the first version of these arms measured 6, 8
# and 12 lines, and every one of them went wrong the moment the call site gained
# a comment. Selftest 320 already takes this approach with the runner's classifier.
# Comments stripped. The block's own explanation quotes the sentences these arms
# count, so an unstripped extraction counts the documentation as a second
# occurrence -- selftest 080's control problem, which this session has now met
# five times in five different files.
_ledcase="$(sed -n '/^\t\t_led_rc=\$?$/,/^\t\tesac$/p' "$_rv" | grep -vE '^[[:space:]]*#')"
check "premise: the gate's status block was found in the runner" \
	"$(printf '%s' "$_ledcase" | grep -c 'case "\$_led_rc" in')" "1"
check "the runner captures the gate's status rather than only its success" \
	"$(grep -c '_led_rc=\$?' "$_rv")" "1"
check "a refusal keeps the regenerate-the-ledger wording" \
	"$(printf '%s' "$_ledcase" | grep -c 'has a check the ledger has never seen')" "1"
check "an integrity failure says regenerating will not help" \
	"$(printf '%s' "$_ledcase" | grep -c 'will not help')" "1"
check "and it is a different sentence from the refusal, not the same one twice" \
	"$(printf '%s' "$_ledcase" | grep -c 'could not run the ledger gate at all')" "1"
check "a clean status says nothing and does not fail the major" \
	"$(printf '%s' "$_ledcase" | grep -cE '^[[:space:]]+0\)[[:space:]]*;;[[:space:]]*$')" "1"
check "both failure arms fail the major" \
	"$(printf '%s' "$_ledcase" | grep -c 'verfail=1')" "2"

# ---- an unresolvable ref is not the bootstrap case --------------------------
#
# The three states are the point of this part, and one path had collapsed two of
# them. The ref-resolution check lived inside the `auto` resolver, so it covered
# the production call site and nothing else: an EXPLICIT ref that did not resolve
# fell through to the file-absent branch and was reported as rc=0, with a message
# asserting "this change introduces it" about a ref that does not exist -- one
# clause after saying the distance from HEAD was unknown. The code knew it could
# not resolve the ref and contradicted itself inside one sentence.
#
# Unreachable from the runner, which always passes `auto`. A trap for these arms,
# for anyone driving the tool by hand, and for whoever later passes a concrete ref
# because `auto` was inconvenient. Scoped exactly that way by OffgridwithJD, who
# checked the production path first rather than leading with the headline.

_rr="$_lw/refres"; rm -rf "$_rr"; mkdir -p "$_rr"
( cd "$_rr" && git init -q . && git config user.email t@t && git config user.name t
  printf 'suites_not_covered 7\n' > b.txt && git add b.txt && git commit -qm base
  git branch -q hasbudget
  git rm -q b.txt && git commit -qm "a commit without the budget"
  git branch -q nobudget
  git checkout -q hasbudget ) >/dev/null 2>&1
printf 's\tp\ta\tnever\t-\n' > "$_rr/led"
printf 'RESULT\ts\tp\ta\tPASS\t\nchecks run: 1\n' > "$_rr/log"
printf 's\n' > "$_rr/reg"

check "premise: the hasbudget branch carries the budget" \
	"$(cd "$_rr" && git show hasbudget:b.txt >/dev/null 2>&1 && echo yes || echo no)" "yes"
check "premise: and the nobudget branch does not, which is the bootstrap shape" \
	"$(cd "$_rr" && git show nobudget:b.txt >/dev/null 2>&1 && echo yes || echo no)" "no"

check "a ref that does not resolve is an integrity failure, not a bootstrap" \
	"$(cd "$_rr" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against refs/heads/no-such-ref-xyz log >/dev/null 2>&1; echo $?)" "2"
check "and it says the ref does not resolve, rather than claiming the file is new" \
	"$(cd "$_rr" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against refs/heads/no-such-ref-xyz log 2>&1 | grep -c 'that ref does not resolve here')" "1"
check "and never says a change introduces a file at a ref that is not there" \
	"$(cd "$_rr" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against refs/heads/no-such-ref-xyz log 2>&1 | grep -c 'this change introduces it')" "0"

# The bootstrap case must still be the bootstrap case: a ref that EXISTS, without
# the file. Without this arm the fix above is satisfied by refusing everything.
check "a ref that exists without the budget is still the bootstrap case" \
	"$(cd "$_rr" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against nobudget log >/dev/null 2>&1; echo $?)" "0"
check "and it is that case that says the change introduces the file" \
	"$(cd "$_rr" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against nobudget log 2>&1 | grep -c 'this change introduces it')" "1"

# And a ref that exists WITH the file still compares, so the third state is intact.
check "a ref that exists with the budget still compares" \
	"$(cd "$_rr" && python3 "$_led" gate --ledger led --budget b.txt --registered reg \
		--against hasbudget log 2>&1 | grep -c 'ceiling against hasbudget')" "1"

# ---- a red needs a reason (#946) --------------------------------------------
#
# `merge` already refuses a log that does not RECONCILE, and that is not the
# property that matters: both logs that poisoned this ledger on the day it landed
# reconciled. One was 827 records against `checks run: 827` with fifteen checks red
# because the tree was copied without `.git`; the other was a single FAIL from an
# unfinished change. A rule about reconciliation would have caught neither.
#
# An environment red and a real regression are IDENTICAL in the log, so the caller
# has to say which it is rather than the tool guessing.

_r946="$PGC_WORKDIR/r946"; mkdir -p "$_r946"
printf 'RESULT\tdemo\tp\tthe check\tFAIL\t\nRESULT\tdemo\tp\tother\tPASS\t\nchecks run: 2\n' > "$_r946/red.log"
printf 'RESULT\tdemo\tp\tthe check\tPASS\t\nRESULT\tdemo\tp\tother\tPASS\t\nchecks run: 2\n' > "$_r946/green.log"

: > "$_r946/a.tsv"
check "a log carrying a FAIL is refused when no reason is given" \
	"$(_led_rc merge --ledger "$_r946/a.tsv" --date 2026-09-10 "$_r946/red.log")" "2"
check "and the refusal names the check that reddened" \
	"$(_led_run merge --ledger "$_r946/a.tsv" --date 2026-09-10 "$_r946/red.log" \
	   | grep -c 'the check')" "1"
# NOTHING HALF-APPLIED. A refusal that had already written rows would leave the
# ledger holding the very observation it just declined to accept.
check "and nothing is written, so a refused merge is not half-applied" \
	"$(wc -c < "$_r946/a.tsv" | tr -d ' ')" "0"

# BOTH WAYS OF SAYING WHY MUST STILL WORK. Refusing reds outright would refuse a
# genuine CI red, which is the most valuable row this ledger can hold and has no
# mutation to name.
: > "$_r946/b.tsv"
check "a deliberate break says so with --mutation" \
	"$(_led_rc merge --ledger "$_r946/b.tsv" --date 2026-09-10 --mutation M "$_r946/red.log")" "0"
: > "$_r946/c.tsv"
check "and a genuine observation says so with --reds-are-real" \
	"$(_led_rc merge --ledger "$_r946/c.tsv" --date 2026-09-10 --reds-are-real "$_r946/red.log")" "0"

# THE CONTROL, without which every arm above passes on a tool that refuses
# everything it is handed.
: > "$_r946/d.tsv"
check "control: an all-PASS log still merges with no flag at all" \
	"$(_led_rc merge --ledger "$_r946/d.tsv" --date 2026-09-10 "$_r946/green.log")" "0"
unset _r946

# ---- the gate must refuse a census that contradicts its ledger (#952) --------
#
# `gate` prints `ledger census: rows=N | never observed red=N` and does not
# compare that N to the budget. A six-row ledger with a budget claiming 2 is
# a four-row lie; today it is rc=0. The composed-tree case @OffgridwithJD
# measured was 775 rows against a budget of 769, same shape.
#
# The census is not a ceiling. The refusal is only that the two numbers
# describe the same file and disagree.

_r952="$PGC_WORKDIR/r952"; mkdir -p "$_r952"
: > "$_r952/l.tsv"
: > "$_r952/g.log"
i=0
while [ "$i" -lt 6 ]; do
	printf 'demo\tq\trow-%s\tnever\t-\n' "$i" >> "$_r952/l.tsv"
	printf 'RESULT\tdemo\tq\trow-%s\tPASS\t\n' "$i" >> "$_r952/g.log"
	i=$((i + 1))
done
printf 'checks run: 6\n' >> "$_r952/g.log"
printf 'demo\n' > "$_r952/reg"
printf 'suites_not_covered 0\nchecks_never_observed_red 2\n' > "$_r952/lie.txt"
printf 'suites_not_covered 0\nchecks_never_observed_red 6\n' > "$_r952/ok.txt"

check "a budget that understates the ledger census is refused" \
	"$(_led_rc gate --ledger "$_r952/l.tsv" --budget "$_r952/lie.txt" --registered "$_r952/reg" "$_r952/g.log")" "1"
check "and the refusal names both values the budget and the ledger hold" \
	"$(_led_run gate --ledger "$_r952/l.tsv" --budget "$_r952/lie.txt" --registered "$_r952/reg" "$_r952/g.log" \
		| grep -c 'budget states 2, ledger has 6')" "1"
check "control: the same ledger passes when the census matches" \
	"$(_led_rc gate --ledger "$_r952/l.tsv" --budget "$_r952/ok.txt" --registered "$_r952/reg" "$_r952/g.log")" "0"
unset _r952

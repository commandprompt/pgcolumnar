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

printf 'RESULT\tdemo\tpart1\tfirst check\tPASS\t18\t\nRESULT\tdemo\tpart1\tsecond check\tPASS\t18\t\nchecks run: 2\n' > "$_lw/green.log"
printf 'RESULT\tdemo\tpart1\tfirst check\tFAIL\t18\t\nRESULT\tdemo\tpart1\tsecond check\tPASS\t18\t\nchecks run: 2\n' > "$_lw/red.log"
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
		| grep -c 'a record has 6 fields')" "1"

# ---- the count is not the schema, on INGESTION as well as in the runner ------
#
# read_records accepted `len(f) >= 5`, so a record missing its reason, a verdict
# outside pgc_record's vocabulary, an empty check name, and one record against
# `checks run: 2` all merged at rc=0 -- the ledger absorbing as evidence a log
# that does not parse. The runner reconciles the suite it just ran; this
# reconciles a log handed to the ledger, possibly from another machine.
# Reported by @linuxhikerpm on #918.
printf 'RESULT\tdemo\tpart1\ta name\tBOGUS\t18\t\nchecks run: 1\n' > "$_lw/bogus.log"
printf 'RESULT\tdemo\tpart1\t\tPASS\t18\t\nchecks run: 1\n'        > "$_lw/noname.log"
printf 'RESULT\tdemo\tpart1\ta name\tPASS\t18\t\nchecks run: 2\n'  > "$_lw/miscount.log"

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
	"$(awk -F'\t' '$3=="first check"{print $5}' "$_lw/date.tsv")" "2026-09-10"
_led_run merge --reds-are-real --ledger "$_lw/date.tsv" --date 2026-09-20 "$_lw/red.log" >/dev/null
check "and a newer one does" \
	"$(awk -F'\t' '$3=="first check"{print $5}' "$_lw/date.tsv")" "2026-09-20"
_led_run merge --reds-are-real --ledger "$_lw/date.tsv" "$_lw/red.log" >/dev/null
check "and an undated merge does not erase a known date" \
	"$(awk -F'\t' '$3=="first check"{print $5}' "$_lw/date.tsv")" "2026-09-20"
check "a date that is not a date is refused rather than stored" \
	"$(_led_rc merge --ledger "$_lw/date.tsv" --date not-a-date "$_lw/red.log")" "2"

# ---- a mutation names ONE check ---------------------------------------------
#
# A run that mutates one thing can redden several: the target, plus whatever
# depended on it. Attributing --mutation to every failure records collateral
# damage as evidence that the mutation kills that check.
printf 'RESULT\tdemo\tpart1\tthe target\tFAIL\t18\t\nRESULT\tdemo\tpart1\tcollateral\tFAIL\t18\t\nchecks run: 2\n' \
	> "$_lw/twofail.log"
check "--mutation across two failing checks in one run is refused" \
	"$(_led_rc merge --ledger "$_lw/m2.tsv" --date 2026-09-10 --mutation M "$_lw/twofail.log")" "2"
check "and the refusal names how many failed, so the author can narrow the run" \
	"$(_led_run merge --ledger "$_lw/m2.tsv" --date 2026-09-10 --mutation M "$_lw/twofail.log" \
		| grep -c '2 checks failed')" "1"
check "control: the same log merges with a different reason, so the refusal above is --mutation-across-two-checks and not the log" \
	"$(_led_rc merge --reds-are-real --ledger "$_lw/m2.tsv" --date 2026-09-10 "$_lw/twofail.log")" "0"

# ---- ...unless the caller NAMES them (#1014) --------------------------------
#
# A MUTATION WITH TWO GENUINE TARGETS IS ORDINARY. #1008 is the live case:
# reverting the `enable_join_runtime_filter` boot value reddens both `join runtime
# filter defaults on` and `default plan has runtime coordinator`. Neither is
# collateral -- both read the default directly, which is why one change kills both.
#
# The tool could not tell that from "one target and one bystander", and resolved
# the ambiguity by recording NOTHING: the only permitted merge was
# `--reds-are-real`, which writes `-` in the mutation column. So the catalogue this
# column exists to become could never hold the entry it most exists for -- the
# mutation that tells a reader WHICH CHECKS SHARE A CAUSE.
#
# The caller asserts the attribution, exactly as `--reds-are-real` makes them
# assert that a red is real. A red not named is still collateral and still refused.
#
# EVERY REFUSAL ARM BELOW GREPS ITS MESSAGE, not just the status. `--target` did
# not exist before this change, so argparse exited 2 for an unknown flag -- and an
# arm asserting only `rc=2` would have passed against the absent feature, which is
# the shape this repository keeps finding.
check "naming both failing checks as targets permits the merge" \
	"$(_led_rc merge --ledger "$_lw/m3.tsv" --date 2026-09-10 --mutation M \
		--target 'the target' --target 'collateral' "$_lw/twofail.log")" "0"
check "and the mutation is recorded against both of them" \
	"$(awk -F'\t' '$6=="M"' "$_lw/m3.tsv" | grep -c .)" "2"

check "naming only one of two failing checks is still refused" \
	"$(_led_rc merge --ledger "$_lw/m4.tsv" --date 2026-09-10 --mutation M \
		--target 'the target' "$_lw/twofail.log")" "2"
check "and the refusal names the check that was not claimed as a target" \
	"$(_led_run merge --ledger "$_lw/m4.tsv" --date 2026-09-10 --mutation M \
		--target 'the target' "$_lw/twofail.log" \
		| grep -c 'failed but was not named as a target')" "1"

# THE OTHER DIRECTION. A target that did not redden means the author believes the
# mutation kills a check and it does not. That is a finding about the mutation,
# not a row to write down.
check "a target that did not fail is refused, because the claim is wrong" \
	"$(_led_rc merge --ledger "$_lw/m5.tsv" --date 2026-09-10 --mutation M \
		--target 'the target' --target 'collateral' --target 'never ran' \
		"$_lw/twofail.log")" "2"
check "and that refusal names the target that stayed green" \
	"$(_led_run merge --ledger "$_lw/m5.tsv" --date 2026-09-10 --mutation M \
		--target 'the target' --target 'collateral' --target 'never ran' \
		"$_lw/twofail.log" | grep -c 'named as a target but did not fail')" "1"

check "--target without --mutation is refused, because it attributes nothing" \
	"$(_led_rc merge --ledger "$_lw/m6.tsv" --date 2026-09-10 \
		--target 'the target' "$_lw/twofail.log")" "2"
check "and that refusal says --target needs --mutation" \
	"$(_led_run merge --ledger "$_lw/m6.tsv" --date 2026-09-10 \
		--target 'the target' "$_lw/twofail.log" \
		| grep -c 'target names what a mutation killed')" "1"

# EVERY EXISTING CALLER. One red and no --target is the shape every merge in this
# tree uses today, and it must not have moved.
check "control: one red and no --target still merges" \
	"$(_led_rc merge --ledger "$_lw/m7.tsv" --date 2026-09-10 --mutation M "$_lw/red.log")" "0"
check "control: and still records the mutation against the check that reddened" \
	"$(awk -F'\t' '$3=="first check"{print $6}' "$_lw/m7.tsv")" "M"

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
	"$(cut -f5 "$_lw/ledger.tsv" | sort -u | tr '\n' ' ')" "never "
check "every row has six fields and no trailing tab" \
	"$(awk -F'\t' 'NF!=6' "$_lw/ledger.tsv" | grep -c . || true)" "0"
check "and an empty mutation is a placeholder, not an empty last field" \
	"$(grep -cP '\t$' "$_lw/ledger.tsv" || true)" "0"

_led_run merge --reds-are-real --ledger "$_lw/ledger.tsv" --date 2026-09-10 "$_lw/red.log" >/dev/null
check "a check observed red gains the date it was seen" \
	"$(awk -F'\t' '$3=="first check"{print $5}' "$_lw/ledger.tsv")" "2026-09-10"
check "and one that stayed green keeps its debt" \
	"$(awk -F'\t' '$3=="second check"{print $5}' "$_lw/ledger.tsv")" "never"
_led_run merge --ledger "$_lw/ledger.tsv" --date 2026-09-11 "$_lw/green.log" >/dev/null
check "a later green run does not erase an observation" \
	"$(awk -F'\t' '$3=="first check"{print $5}' "$_lw/ledger.tsv")" "2026-09-10"

# ---- the mutation column ACCUMULATES ----------------------------------------
#
# Last-write-wins records the most recent attack rather than the catalogue the
# column exists to become, which defeats its stated purpose rather than limiting
# it. And one --mutation value copied across several logs attributes a deliberate
# change to failures it had nothing to do with. Both reported by @linuxhikerpm.

: > "$_lw/mut.tsv"
_led_run merge --ledger "$_lw/mut.tsv" --date 2026-09-10 --mutation 'SAOP limit 128 -> 0' "$_lw/red.log" >/dev/null
check "a named mutation is recorded against the check that reddened" \
	"$(awk -F'\t' '$3=="first check"{print $6}' "$_lw/mut.tsv")" "SAOP limit 128 -> 0"
check "and not against one that stayed green" \
	"$(awk -F'\t' '$3=="second check"{print $6}' "$_lw/mut.tsv")" "-"
_led_run merge --ledger "$_lw/mut.tsv" --date 2026-09-10 --mutation 'bloom neutered' "$_lw/red.log" >/dev/null
check "a second mutation ACCUMULATES rather than replacing the first" \
	"$(awk -F'\t' '$3=="first check"{print $6}' "$_lw/mut.tsv")" "SAOP limit 128 -> 0;bloom neutered"
check "one --mutation cannot be attributed across several runs at once" \
	"$(_led_rc merge --ledger "$_lw/mut.tsv" --date 2026-09-10 --mutation X "$_lw/red.log" "$_lw/green.log")" "2"

# ---- the majors a row CLAIMS, which is a set and not part of the key (#1010) --
#
# A check's existence depends on the major: analyze_differential.sh:61 emits ONE
# record on PG15-17 and a suite's worth on PG18+, and fk_referencing.sh:287 emits
# DIFFERENT CHECK NAMES in its two branches. The ledger has to record WHERE a check
# exists. It does not follow that the major belongs in the KEY: measured on a full
# matrix at 4d7c75ae, 6367 of 6472 checks are identical on PG15 and PG18, so a
# (major, check) key would hold 6472 x 5 rows to express 105 keys' worth.
#
# THE SET ACCUMULATES, for the reason the mutation column above does. A plain
# assignment would make merging a PG15 log after a PG18 log say the check stopped
# existing on 18, and the order somebody merges logs in is not a fact about the code.

printf 'RESULT	demo	part1	both majors	PASS	18	
RESULT	demo	part1	pg18 only	PASS	18	
checks run: 2
' > "$_lw/m18.log"
printf 'RESULT	demo	part1	both majors	PASS	15	
checks run: 1
' > "$_lw/m15.log"
printf 'RESULT	demo	part1	both majors	PASS	unknown	
checks run: 1
' > "$_lw/munk.log"

: > "$_lw/maj.tsv"
_led_run merge --ledger "$_lw/maj.tsv" --date 2026-09-12 "$_lw/m18.log" >/dev/null
check "a check seen once claims the one major it was seen on" 	"$(awk -F'	' '$3=="pg18 only"{print $4}' "$_lw/maj.tsv")" "18"

_led_run merge --ledger "$_lw/maj.tsv" --date 2026-09-12 "$_lw/m15.log" >/dev/null
check "a second major is ADDED to the set, sorted, not written over the first" 	"$(awk -F'	' '$3=="both majors"{print $4}' "$_lw/maj.tsv")" "15;18"
check "and a check the second run never mentioned keeps the majors it claimed" 	"$(awk -F'	' '$3=="pg18 only"{print $4}' "$_lw/maj.tsv")" "18"
check "two checks stay two rows whatever the majors, because the key is not the major" 	"$(grep -c . "$_lw/maj.tsv")" "2"

# `unknown` is a token in the set like any other, and a real case: PGC_MAJOR is set in
# pgc_setup, and 14 suites need no cluster so never call it -- 544 of 6753 records on a
# full pg18 matrix. This suite is NOT one of them: 10 of its 46 parts call pgc_setup and
# they share one shell, so its records name the major.
_led_run merge --ledger "$_lw/maj.tsv" --date 2026-09-12 "$_lw/munk.log" >/dev/null
check "a harness that named no major adds its own token rather than a number" 	"$(awk -F'	' '$3=="both majors"{print $4}' "$_lw/maj.tsv")" "15;18;unknown"

# A row whose majors field is garbage is refused, in the LEDGER as well as in a log.
# A ledger is hand-edited far more often than a log is generated.
printf 'demo	part1	c	eighteen	never	-
' > "$_lw/badmaj.tsv"
check "a ledger row naming a major that is not a major is an integrity failure" 	"$(_led_rc orphan-scan --ledger "$_lw/badmaj.tsv" "$_lw/m18.log")" "2"
printf 'demo	part1	c		never	-
' > "$_lw/nomaj.tsv"
check "and a row naming NO major says nothing about where its check exists" 	"$(_led_rc orphan-scan --ledger "$_lw/nomaj.tsv" "$_lw/m18.log")" "2"
check "control: the same row with a real major is read without complaint" 	"$(printf 'demo	part1	both majors	18	never	-
demo	part1	pg18 only	18	never	-
' > "$_lw/okmaj.tsv"
	   _led_rc orphan-scan --ledger "$_lw/okmaj.tsv" "$_lw/m18.log")" "0"

# ---- a run speaks only for the majors a row claims ---------------------------
#
# THE DIRECTION THE MISSING DIMENSION BROKE, and it is orphan-scan rather than the
# gate: the gate refuses a check in the LOG the ledger has not seen, and a PG18-only
# check does not appear in a PG15 log, so it stayed correct by never being asked.
#
# Until now it was saved only by the SKIP rule, and that was luck.
# analyze_differential emits a check_skip on PG15-17 so its part was unprunable;
# fk_referencing:287 emits `check` in its older-major branch and has no SKIP at all,
# so once that suite is seeded a PG15 run would have called its two PG17+ checks
# deleted. THIS FIXTURE CARRIES NO SKIP, or the arm proves the wrong mechanism.

printf 'demo	part1	both majors	15;18	never	-
demo	part1	pg18 only	18	never	-
demo	part1	pg15 only	15	never	-
' > "$_lw/scope.tsv"
printf 'RESULT	demo	part1	both majors	PASS	15	
RESULT	demo	part1	pg15 only	PASS	15	
checks run: 2
' > "$_lw/s15.log"
_scope="$(_led_run orphan-scan --ledger "$_lw/scope.tsv" "$_lw/s15.log")"
check "a PG15 run names no orphan, having seen every check the ledger claims for 15" 	"$(printf '%s
' "$_scope" | grep -c 'orphan:' || true)" "0"
check "and the PG18-only row is one it cannot speak about, not one that vanished" 	"$(printf '%s
' "$_scope" | grep -c 'not checked: 1 row' || true)" "1"
check "and that is not a failure, because a run sees ONE major" 	"$(_led_rc orphan-scan --ledger "$_lw/scope.tsv" "$_lw/s15.log")" "0"

# THE CONTROL. Without it the three arms above prove only that nothing is ever an
# orphan. Same major, really gone.
printf 'RESULT	demo	part1	both majors	PASS	15	
checks run: 1
' > "$_lw/s15b.log"
check "control: a check the ledger claims for 15 that a PG15 run did not emit IS an orphan" 	"$(_led_run orphan-scan --ledger "$_lw/scope.tsv" "$_lw/s15b.log" | grep -c 'orphan: demo	part1	pg15 only' || true)" "1"

# ---- a log must be able to say WHICH TREE it came from (#1073) ---------------
#
# `orphan-scan` reports a ledger row that no record in its own part matches. A row
# whose check was ADDED AFTER the log was written produces exactly that signal, and
# nothing in a RESULT record dates it against a tree. So a stale log and a genuinely
# deleted check are indistinguishable, and the tool cannot close the gap from inside.
#
# Measured when it was filed: replaying a log from one tree against the ledger one
# commit later reported 2 orphans, and both were checks that tree had just GAINED.
# One step from filing a defect in a tool merged an hour earlier.
#
# The harness ALREADY writes `-- source: <fingerprint>` into every log, from the one
# implementation in test/pgc_fingerprint.py. The tool now reads it and the caller
# says what it expects.
#
# OPT-IN, deliberately. A hand caller may not know the build its log came from, and a
# flag that refused every hand invocation is a flag nobody passes. Today's runner is
# safe by CONSTRUCTION -- it passes the logs from the run it just finished -- and
# that is an argument for making the guarantee explicit, not for assuming the next
# caller inherits it.
printf -- '-- source: aaaaaaaaaaaa matches the binary under test\nRESULT\tdemo\tpart1\tc\tPASS\t18\t\nchecks run: 1\n' > "$_lw/fp_a.log"
printf -- '-- source: bbbbbbbbbbbb matches the binary under test\nRESULT\tdemo\tpart1\tc\tPASS\t18\t\nchecks run: 1\n' > "$_lw/fp_b.log"
printf 'RESULT\tdemo\tpart1\tc\tPASS\t18\t\nchecks run: 1\n' > "$_lw/fp_none.log"
printf 'demo\tpart1\tc\t18\tnever\t-\n' > "$_lw/fp.tsv"

check "control: without --expect-source a log from any tree is read, so this is opt-in" \
	"$(_led_rc orphan-scan --ledger "$_lw/fp.tsv" "$_lw/fp_b.log")" "0"
check "a log whose source fingerprint is the expected one is read" \
	"$(_led_rc orphan-scan --ledger "$_lw/fp.tsv" --expect-source aaaaaaaaaaaa "$_lw/fp_a.log")" "0"
check "a log from ANOTHER tree is refused rather than read as evidence" \
	"$(_led_rc orphan-scan --ledger "$_lw/fp.tsv" --expect-source aaaaaaaaaaaa "$_lw/fp_b.log")" "2"
check "and the refusal names the fingerprint the log carries, so a reader can tell which tree" \
	"$(_led_run orphan-scan --ledger "$_lw/fp.tsv" --expect-source aaaaaaaaaaaa "$_lw/fp_b.log" \
		| grep -c 'bbbbbbbbbbbb')" "1"

# THE FLAG MUST NOT PASS VACUOUSLY. A log naming no fingerprint at all satisfies
# "does not disagree", which is how an opt-in check reports success having asked
# nothing -- the shape #1032 and #965 both turned out to be.
check "a log naming NO source fingerprint is refused when one is expected" \
	"$(_led_rc orphan-scan --ledger "$_lw/fp.tsv" --expect-source aaaaaaaaaaaa "$_lw/fp_none.log")" "2"
check "and that refusal says the log names none, rather than that it disagrees" \
	"$(_led_run orphan-scan --ledger "$_lw/fp.tsv" --expect-source aaaaaaaaaaaa "$_lw/fp_none.log" \
		| grep -c 'names no source fingerprint')" "1"

# MERGE TOO, and it matters MORE there. orphan-scan misreporting a stale log is
# recoverable by looking again; a stale log stamped into the ledger persists.
check "merge refuses a log from another tree the same way" \
	"$(_led_rc merge --ledger "$_lw/fpm.tsv" --date 2026-09-18 --reds-are-real \
		--expect-source aaaaaaaaaaaa "$_lw/fp_b.log")" "2"
check "control: and merges it when the fingerprint is the expected one" \
	"$(_led_rc merge --ledger "$_lw/fpm.tsv" --date 2026-09-18 --reds-are-real \
		--expect-source aaaaaaaaaaaa "$_lw/fp_a.log")" "0"

# AND THE RUNNER PASSES IT, or the flag exists and nothing uses it -- which is the
# state orphan-scan itself was in until #983: written, tested, and unable to fire on
# anybody's change.
check "the runner tells orphan-scan which tree the logs came from" \
	"$(grep -A8 'pgc_ledger.py" orphan-scan' "$_rv" | grep -c -- '--expect-source')" "1"

# AND AN EMPTY EXPECTATION MUST NOT PASS FOR ONE (#1073, reported by jdatcmd).
#
# `pgc_source_fingerprint` returns EMPTY with status 0 on both its failure paths, so
# a box without python3 would have the runner pass `--expect-source ""` -- and the
# tool's opt-in rule then skips the check. "Does not disagree" satisfying a guard,
# moved from the log to the expectation, which is the thing the flag exists to
# refuse one level out.
check "the runner refuses the scan when it cannot name the tree" \
	"$(grep -A14 '_orph_fp=' "$_rv" | grep -c 'could not be computed')" "1"
check "and the scan does not run at all without an expectation, so it cannot report clean" \
	"$(grep -c 'for _orph_log in \${_orph_fp:+\$_led_logs}' "$_rv")" "1"
# THE PREMISE FOR BOTH: the empty return is real, not imagined.
check "premise: pgc_source_fingerprint really does return empty with status 0" \
	"$( . "$PGC_TESTDIR/lib.sh" 2>/dev/null
	   v="$(pgc_source_fingerprint /nonexistent/tree 2>/dev/null)"; rc=$?
	   printf 'value=[%s] rc=%s' "$v" "$rc" )" "value=[] rc=0"

# ---- and the RUNNER stamps every log, not the suites (#1073) -----------------
#
# The first version of this leaned on `pgc_setup` to write `-- source:`, and TWENTY
# suites in this tree never call it -- they carry their own harness, deliberately,
# which is the population #1109 exists for. Fourteen registered suites therefore
# produced logs that could not say which tree they came from, and CI refused the
# whole major:
#
#     audit  concurrency  decode_interrupts  hilbert_curve  objstore_stash_recovery
#     phase2  phase3  phase4  phase5  phase6  smoke  unique_conc  update_conc
#     wal_envelope
#
# "Safe by construction" was true of PROVENANCE and not of STAMPING, and those are
# different properties. Caught by @jdatcmd off CI, not by me.
#
# The runner owns every log, so it stamps every log: one site, 258 suites, nothing
# asked of any suite. THE ORDER IS LOAD-BEARING -- a suite that also stamps writes a
# second line, and the tool reads the FIRST, so the runner's is the one answer.
check "the runner defines one stamping helper rather than repeating the line" \
	"$(grep -c '^pgc_stamp_log()' "$_rv")" "1"
# AND DEFINES IT BEFORE IT CALLS IT. bash reads top to bottom, so a helper defined
# below its call site is `command not found` at run time -- and the suite loop still
# runs, so every log in the batch arrives unstamped while the run looks normal.
# Measured: the first version put the definition at line 1128 and called it at 762,
# and the matrix printed `pgc_stamp_log: command not found` once per suite.
check "and defines it BEFORE the first call, since bash reads top to bottom" \
	"$([ "$(grep -n '^pgc_stamp_log()' "$_rv" | cut -d: -f1)" \
	     -lt "$(grep -n 'pgc_stamp_log "' "$_rv" | sed -n 2p | cut -d: -f1)" ] \
	   && echo before || echo after)" "before"
check "and stamps at every site that writes a suite log" \
	"$(grep -c 'pgc_stamp_log "\$builddir/\${s}\.log"' "$_rv")" "3"
# THE APPEND IS THE OTHER HALF. A suite redirected with > would truncate the stamp
# the line above just wrote, and the log would arrive unstamped with nothing saying
# so -- the failure this replaces, reintroduced by a redirection operator.
check "and every suite log is APPENDED to, so the stamp survives the run" \
	"$(grep -c 'bash "\$builddir/test/\${s}\.sh" "\$pgc" >"\$builddir/\${s}\.log"' "$_rv")" "0"
check "premise: and those writes exist at all, in the appending form" \
	"$(grep -c 'bash "\$builddir/test/\${s}\.sh" "\$pgc" >>"\$builddir/\${s}\.log"' "$_rv")" "2"

# THE HELPER'S OWN BEHAVIOUR, exercised rather than read: it must write the line a
# log carries, and must write NOTHING when it has no fingerprint -- a stamp saying
# `-- source: ` would satisfy the format and name no tree.
_st_d="$PGC_WORKDIR/stamp"; rm -rf "$_st_d"; mkdir -p "$_st_d"
( eval "$(sed -n '/^pgc_stamp_log()/,/^}/p' "$_rv")"
  pgc_stamp_log "$_st_d/a.log" c9e65b1b35ba
  pgc_stamp_log "$_st_d/b.log" "" )
check "the helper writes a source line a reader can parse" \
	"$(grep -cE '^-- source: [0-9a-f]{6,64}\b' "$_st_d/a.log" 2>/dev/null || true)" "1"
check "and writes nothing at all when it has no fingerprint to write" \
	"$([ -e "$_st_d/b.log" ] && echo "a file was created" || echo none)" "none"

# A row claiming BOTH majors is checked on BOTH: a stronger claim held to both tests,
# which is the point of storing a set rather than one major per row.
printf 'RESULT	demo	part1	pg18 only	PASS	18	
checks run: 1
' > "$_lw/s18.log"
check "a row claiming 15;18 is an orphan on a PG18 run that did not emit it" 	"$(_led_run orphan-scan --ledger "$_lw/scope.tsv" "$_lw/s18.log" | grep -c 'orphan: demo	part1	both majors' || true)" "1"

# ---- the gate cannot refuse a check on a major it holds no rows for ----------
#
# The same argument as suites_not_covered, one dimension over: it cannot refuse a
# new check in a suite it has never seen, and a major it has never seen is the
# identical problem. Adding PG20 would otherwise redden every check at once, which
# is a gate somebody turns off.

printf 'demo	part1	known check	18	never	-
' > "$_lw/cov.tsv"
printf 'RESULT	demo	part1	known check	PASS	20	
RESULT	demo	part1	brand new	PASS	20	
checks run: 2
' > "$_lw/g20.log"
_g20="$(_led_run gate --ledger "$_lw/cov.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/g20.log")"
check "a major the ledger holds no row for refuses nothing, as an uncovered suite does" 	"$(printf '%s
' "$_g20" | grep -c 'not in the ledger' || true)" "0"
check "and the gate says out loud that it covered no rows for that major" 	"$(printf '%s
' "$_g20" | grep -c 'holds no row for major 20' || true)" "1"
check "so a first run on a new major is not a failure" 	"$(_led_rc gate --ledger "$_lw/cov.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/g20.log")" "0"

# THE CONTROL: on a major it HAS seen, a new check is still refused.
printf 'RESULT	demo	part1	known check	PASS	18	
RESULT	demo	part1	brand new	PASS	18	
checks run: 2
' > "$_lw/g18.log"
check "control: on a covered major a new check is named and refused" 	"$(_led_rc gate --ledger "$_lw/cov.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/g18.log")" "1"

# And a KNOWN check seen on a major its row does not claim is refused too: the row
# is a claim about where the check exists, so widening it is a ledger edit.
printf 'demo	part1	known check	18	never	-
demo	part1	other	15	never	-
' > "$_lw/cov2.tsv"
printf 'RESULT	demo	part1	known check	PASS	15	
RESULT	demo	part1	other	PASS	15	
checks run: 2
' > "$_lw/g15.log"
check "a known check on a major its row does not claim is refused, because the row is a claim" 	"$(_led_rc gate --ledger "$_lw/cov2.tsv" --budget "$_lw/budget.txt" --registered "$_lw/registered" "$_lw/g15.log")" "1"

# ---- two runs of a check are not a duplicate of it --------------------------
#
# Merging the logs first cannot tell "the same check in two runs" from "the same
# name twice in one run", and reported the first as the second.

: > "$_lw/dup.tsv"
check "the same check in two logs is two runs, not a duplicate" \
	"$(_led_run merge --ledger "$_lw/dup.tsv" --date 2026-09-10 "$_lw/green.log" "$_lw/green.log" | grep -c 'duplicate')" "0"
printf 'RESULT\tdemo\tpart1\tsame\tPASS\t18\t\nRESULT\tdemo\tpart1\tsame\tFAIL\t18\t\nchecks run: 2\n' > "$_lw/twice.log"
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
printf 'RESULT\tdemo\tpart1\tthe old name\tFAIL\t18\t\nRESULT\tdemo\tpart1\ta stable check\tPASS\t18\t\nchecks run: 2\n' > "$_lw/before.log"
printf 'RESULT\tdemo\tpart1\tthe new name\tPASS\t18\t\nRESULT\tdemo\tpart1\ta stable check\tPASS\t18\t\nchecks run: 2\n' > "$_lw/after.log"
_led_run merge --reds-are-real --ledger "$_lw/ren.tsv" --date 2026-09-01 "$_lw/before.log" >/dev/null
check "premise: the check has history before the rename" \
	"$(awk -F'\t' '$3=="the old name"{print $5}' "$_lw/ren.tsv")" "2026-09-01"
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
printf 'RESULT\tdemo\tpartA\told A\tPASS\t18\t\nRESULT\tdemo\tpartB\tstable B\tPASS\t18\t\nchecks run: 2\n' > "$_lw/b2.log"
printf 'RESULT\tdemo\tpartA\tnew A\tPASS\t18\t\nRESULT\tdemo\tpartB\tstable B\tPASS\t18\t\nRESULT\tdemo\tpartB\tadded B\tPASS\t18\t\nchecks run: 3\n' > "$_lw/a2.log"
_led_run merge --ledger "$_lw/ren2.tsv" --date 2026-09-10 "$_lw/b2.log" >/dev/null
check "a rename in one part survives an addition in another" \
	"$(_led_run rename-scan --ledger "$_lw/ren2.tsv" "$_lw/a2.log" \
		| grep -c 'possible rename: old A -> new A')" "1"
check "and the addition in the other part is not called a rename" \
	"$(_led_run rename-scan --ledger "$_lw/ren2.tsv" "$_lw/a2.log" | grep -c 'added B')" "0"

# A check merely added, or merely removed, is not a rename.
printf 'RESULT\tdemo\tpart1\tthe old name\tPASS\t18\t\nRESULT\tdemo\tpart1\ta stable check\tPASS\t18\t\nRESULT\tdemo\tpart1\tbrand new\tPASS\t18\t\nchecks run: 3\n' > "$_lw/added.log"
check "a check merely added is not reported as a rename" \
	"$(_led_run rename-scan --ledger "$_lw/ren.tsv" "$_lw/added.log" | grep -c 'possible rename')" "0"
printf 'RESULT\tdemo\tpart1\ta stable check\tPASS\t18\t\nchecks run: 1\n' > "$_lw/removed.log"
check "nor is one merely removed" \
	"$(_led_run rename-scan --ledger "$_lw/ren.tsv" "$_lw/removed.log" | grep -c 'possible rename')" "0"

# ---- and what it IS, which nothing asked until #983 -------------------------
#
# `rename-scan` pairs an appearance with a disappearance. An UNPAIRED
# disappearance -- a check deleted, or renamed in a run where nothing appeared --
# printed as `vanished=N` and refused nothing. Measured on the committed ledger:
# two rows named checks that no longer existed, the census counted both, and the
# gate returned 0 on every run while the note scrolled past.
#
# SCOPED TO THE PARTS THE RUN CONTAINS, and the scope is REPORTED rather than
# assumed. A one-suite log has nothing to say about another suite's rows, and
# counting those as present is how a guard manufactures the confidence that the
# ledger was checked -- the shape of the whole issue family.

: > "$_lw/orph.tsv"
printf 'RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\nRESULT\tdemo\tpart1\tgone tomorrow\tPASS\t18\t\nRESULT\tdemo\tpartZ\telsewhere\tPASS\t18\t\nchecks run: 3\n' > "$_lw/o_before.log"
printf 'RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\nchecks run: 1\n' > "$_lw/o_after.log"
_led_run merge --ledger "$_lw/orph.tsv" --date 2026-09-01 "$_lw/o_before.log" >/dev/null
check "premise: the ledger holds all three rows before the scan" \
	"$(wc -l < "$_lw/orph.tsv" | tr -d ' ')" "3"
check "premise: and the run the scan is given holds only one of them" \
	"$(grep -c '^RESULT' "$_lw/o_after.log")" "1"

check "a ledger row no record in its own part matches is named an orphan" \
	"$(_led_run orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_after.log" \
		| grep -c 'orphan: demo	part1	gone tomorrow')" "1"
check "and it is REFUSED, not merely printed" \
	"$(_led_rc orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_after.log")" "1"
check "the check the run still emits is not called an orphan" \
	"$(_led_run orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_after.log" | grep -c 'still here')" "0"

# The half that decides whether this guard is honest. A row in a part the run does
# not contain is NOT an orphan -- the run cannot speak about it -- and saying so
# out loud is the difference between a scope and a blind spot.
check "a row in a part the run does not contain is not called an orphan" \
	"$(_led_run orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_after.log" | grep -c 'elsewhere')" "0"
check "and the scan states how many rows it could not speak about" \
	"$(_led_run orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_after.log" | grep -c 'not checked=1')" "1"
check "a run that emits every row in its parts is clean" \
	"$(_led_rc orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_before.log")" "0"
check "a before-log and an after-log together are refused, as rename-scan refuses them" \
	"$(_led_rc orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_before.log" "$_lw/o_after.log")" "2"

# ---- pruning: historyless rows may go, a catalogue entry may not ------------
#
# The catalogue of what has been seen red is the thing this ledger exists to be.
# Dropping an entry because the name moved is the exact loss `rename-scan` was
# written to prevent, so prune REFUSES on history rather than asking nicely.

cp "$_lw/orph.tsv" "$_lw/prune.tsv"
check "premise: the orphan about to be pruned carries no history" \
	"$(awk -F'\t' '$3=="gone tomorrow"{print $5}' "$_lw/prune.tsv")" "never"
check "--prune removes a historyless orphan and says which" \
	"$(_led_run orphan-scan --prune --ledger "$_lw/prune.tsv" "$_lw/o_after.log" \
		| grep -c 'pruned: demo	part1	gone tomorrow')" "1"
check "and the ledger is one row shorter afterwards" \
	"$(wc -l < "$_lw/prune.tsv" | tr -d ' ')" "2"
check "control: the row in the part the run never mentioned SURVIVES the prune" \
	"$(awk -F'\t' '$3=="elsewhere"' "$_lw/prune.tsv" | wc -l | tr -d ' ')" "1"
check "and a prune with nothing left to remove is clean, not an error" \
	"$(_led_rc orphan-scan --prune --ledger "$_lw/prune.tsv" "$_lw/o_after.log")" "0"

# An orphan that has been seen red is the one case where deleting the row loses
# something no run can recreate.
: > "$_lw/hist.tsv"
printf 'RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\nRESULT\tdemo\tpart1\tgone tomorrow\tFAIL\t18\t\nchecks run: 2\n' > "$_lw/h_before.log"
_led_run merge --reds-are-real --mutation "drop the guard" --ledger "$_lw/hist.tsv" \
	--date 2026-09-01 "$_lw/h_before.log" >/dev/null
check "premise: the orphan now carries a date and a mutation" \
	"$(awk -F'\t' '$3=="gone tomorrow"{print $5"/"$6}' "$_lw/hist.tsv")" "2026-09-01/drop the guard"
check "an orphan carrying history is reported as carrying it" \
	"$(_led_run orphan-scan --ledger "$_lw/hist.tsv" "$_lw/o_after.log" \
		| grep -c 'ORPHAN CARRYING HISTORY')" "1"
check "and the history it would lose is printed with it" \
	"$(_led_run orphan-scan --ledger "$_lw/hist.tsv" "$_lw/o_after.log" \
		| grep -c 'last red 2026-09-01')" "1"
check "--prune REFUSES the whole prune when any orphan carries history" \
	"$(_led_rc orphan-scan --prune --ledger "$_lw/hist.tsv" "$_lw/o_after.log")" "2"
check "premise: and the refusal removed NOTHING -- the row is still there" \
	"$(awk -F'\t' '$3=="gone tomorrow"' "$_lw/hist.tsv" | wc -l | tr -d ' ')" "1"
check "the refusal says why, rather than only that it refused" \
	"$(_led_run orphan-scan --prune --ledger "$_lw/hist.tsv" "$_lw/o_after.log" \
		| grep -c 'the catalogue is what this ledger is for')" "1"

# ---- a part the run SKIPPED is not a part the run can speak about ------------
#
# The first version of this deleted a suite. One SKIP record put the part in
# `parts`, so every other row of that suite became an orphan, and `--prune` removed
# them while reporting `not checked=0` and rc=0 -- the most confident output the
# tool can produce. `not checked` protects a part the run does not contain at all;
# a part CONTAINED BUT SKIPPED WHOLESALE fell in the gap between the two.
#
# Reported by @pgcolumnar-9b reviewing this change, on a three-row fixture for
# `analyze_differential`, whose run on PG17 is a single SKIP. Nine suites skip
# wholesale on PG17 and `suites_not_covered` is 250, so seeding any one of them
# would have armed it.
#
# THE RULE IS BROADER THAN THAT CASE DELIBERATELY: a SKIP anywhere in the part
# means some arm did not run, so the run cannot tell "this row's check was deleted"
# from "this row's check was skipped under a name that does not match it" -- #994's
# defect at suite granularity. One skipped timing check therefore blocks pruning
# that whole part, and that is the direction a deleting command should err in.

: > "$_lw/skp.tsv"
printf 'RESULT\tdemo\tpart1\tarm one\tPASS\t18\t\nRESULT\tdemo\tpart1\tarm two\tPASS\t18\t\nRESULT\tdemo\tpart1\tthe whole thing\tPASS\t18\t\nchecks run: 3\n' > "$_lw/sk_full.log"
printf 'RESULT\tdemo\tpart1\tthe whole thing\tSKIP\t18\tno fixture on this box\nchecks run: 1\n' > "$_lw/sk_skipped.log"
_led_run merge --ledger "$_lw/skp.tsv" --date 2026-09-01 "$_lw/sk_full.log" >/dev/null
check "premise: the ledger holds all three of that part's rows" \
	"$(wc -l < "$_lw/skp.tsv" | tr -d ' ')" "3"
check "premise: and the skipped run emits exactly one of them, as a SKIP" \
	"$(awk -F'\t' '$5=="SKIP"' "$_lw/sk_skipped.log" | wc -l | tr -d ' ')" "1"

check "a row in a part that SKIPPED is reported as unprunable, not as an orphan" \
	"$(_led_run orphan-scan --ledger "$_lw/skp.tsv" "$_lw/sk_skipped.log" \
		| grep -c 'unprunable: 2 row(s)')" "1"
check "and the summary keeps the two apart, so a zero orphan count is not a clean bill" \
	"$(_led_run orphan-scan --ledger "$_lw/skp.tsv" "$_lw/sk_skipped.log" \
		| grep -c 'orphans=0 (0 carrying history), unprunable=2')" "1"
check "it is still a finding, so the scan does not return success" \
	"$(_led_rc orphan-scan --ledger "$_lw/skp.tsv" "$_lw/sk_skipped.log")" "1"

_led_run orphan-scan --prune --ledger "$_lw/skp.tsv" "$_lw/sk_skipped.log" >/dev/null
check "--prune removes NOTHING from a part that skipped" \
	"$(wc -l < "$_lw/skp.tsv" | tr -d ' ')" "3"
check "and it says so rather than declining silently" \
	"$(_led_run orphan-scan --prune --ledger "$_lw/skp.tsv" "$_lw/sk_skipped.log" \
		| grep -c 'the run did not exercise those checks')" "1"

# CONTROL. Without this the arms above are satisfied by a tool that refuses to prune
# anything at all, which is the failure mode of every over-broad guard.
printf 'RESULT\tdemo\tpart1\tthe whole thing\tPASS\t18\t\nchecks run: 1\n' > "$_lw/sk_pass.log"
check "control: the same two rows ARE pruned when that part's record is a PASS" \
	"$(_led_run orphan-scan --prune --ledger "$_lw/skp.tsv" "$_lw/sk_pass.log" \
		| grep -c 'removed 2 row(s)')" "1"
check "control: and the ledger really is shorter afterwards" \
	"$(wc -l < "$_lw/skp.tsv" | tr -d ' ')" "1"

# Every row must land in exactly one of the four categories, or the classification
# itself lost one -- which is the failure this whole tool exists to report.
check "the four categories account for every ledger row" \
	"$(_led_run orphan-scan --ledger "$_lw/orph.tsv" "$_lw/o_after.log" \
		| grep -c 'classification lost rows')" "0"

# ---- and the exit code cannot say "done" when nothing was done --------------
#
# The first version returned 0 from `--prune` whenever it pruned nothing, including
# when everything it found was unprunable. A caller that scans, sees 1, re-runs with
# `--prune` and sees 0 reads "it pruned them" -- when nothing was pruned and nothing
# could be. Prose covers a human; a script sees only the code. Reported by @jdatcmd
# in review.

: > "$_lw/rc.tsv"
_led_run merge --ledger "$_lw/rc.tsv" --date 2026-09-01 "$_lw/sk_full.log" >/dev/null
check "premise: that part has rows the skipped run cannot speak for" \
	"$(wc -l < "$_lw/rc.tsv" | tr -d ' ')" "3"
check "the scan reports a finding on a skipped part" \
	"$(_led_rc orphan-scan --ledger "$_lw/rc.tsv" "$_lw/sk_skipped.log")" "1"
check "and --prune does NOT turn that 1 into a 0, because it pruned nothing" \
	"$(_led_rc orphan-scan --prune --ledger "$_lw/rc.tsv" "$_lw/sk_skipped.log")" "1"
check "premise: and it really pruned nothing -- the rows are all still there" \
	"$(wc -l < "$_lw/rc.tsv" | tr -d ' ')" "3"
# CONTROL: 0 still means 0. A prune with nothing outstanding must report success, or
# the code says "work remains" forever and nobody can use it in a script either.
check "control: a prune that leaves nothing outstanding returns 0" \
	"$(_led_rc orphan-scan --prune --ledger "$_lw/rc.tsv" "$_lw/sk_pass.log")" "0"
check "control: and that one did prune, so 0 is not a refusal in disguise" \
	"$(wc -l < "$_lw/rc.tsv" | tr -d ' ')" "1"

# ---- --orphans-only: a QUESTION, not a change of severity (#983, #1015) ------
#
# The default question is "is anything outstanding in the parts this run claimed",
# and a skipped part IS outstanding: the run held those rows and failed to speak for
# them. That is why rc=1 covers it and why the three arms above are right on the
# merits rather than only by precedent -- `not checked` is silence by construction
# and OUT of scope, `unprunable` is a gap in a part the run claimed and IN it.
#
# A GATE NEEDS THE NARROWER QUESTION. `run_all_versions.sh` must refuse a ledger row
# whose check no longer exists (#983) and must NOT refuse a box where a part skipped,
# because a skip is box-dependent and not a deletion. Those share rc=1 by design, so
# the flag changes WHAT IS ASKED rather than what a code means: with it, rc=1 is
# orphans and nothing else. rc=1 never acquires a second meaning.
_oo_pre="$(wc -l < "$_lw/rc.tsv" | tr -d ' ')"
check "premise: the fixture is back to a state with rows to speak about" \
	"$([ "$_oo_pre" -ge 1 ] && echo yes || echo no)" "yes"
: > "$_lw/oo.tsv"
_led_run merge --ledger "$_lw/oo.tsv" --date 2026-09-01 "$_lw/sk_full.log" >/dev/null
check "premise: the skipped-part fixture still yields a finding by default" \
	"$(_led_rc orphan-scan --ledger "$_lw/oo.tsv" "$_lw/sk_skipped.log")" "1"
check "a skipped part is NOT an orphan, so --orphans-only returns 0" \
	"$(_led_rc orphan-scan --orphans-only --ledger "$_lw/oo.tsv" "$_lw/sk_skipped.log")" "0"
check "and it still REPORTS the skipped part, so the gate cannot silence it" \
	"$(_led_run orphan-scan --orphans-only --ledger "$_lw/oo.tsv" "$_lw/sk_skipped.log" \
	   | grep -c 'unprunable:')" "1"
check "control: a REAL orphan still returns 1 under --orphans-only" \
	"$(_led_rc orphan-scan --orphans-only --ledger "$_lw/orph.tsv" "$_lw/o_after.log")" "1"
check "control: and a clean run still returns 0 under it" \
	"$(_led_rc orphan-scan --orphans-only --ledger "$_lw/orph.tsv" "$_lw/o_before.log")" "0"

# ---- the WIRING, not the tool: the runner's reduction over N logs ------------
#
# The arms above prove the TOOL. They say nothing about run_all_versions.sh, and
# the first version of that wiring was wrong in a way none of them could see: it
# reduced N per-log statuses with `|| _orph_fail=$?`, which OVERWRITES, so the
# operator was told about whichever log failed LAST. Measured with a stub, both
# orders: orphan-then-toolfail reported "could not run the orphan scan" and hid a
# real orphan; the reverse hid the broken tool. The verdict was right both times
# and the DIAGNOSIS was wrong half the time -- the same defect the gate's own
# comment says it fixed, which is how I know a comment does not transfer.
#
# Caught by @OffgridwithJD, who also measured that the obvious repair is worse:
# `|| { [ "$?" -gt "$_orph_fail" ] && _orph_fail=$?; }` yields 0 for EVERY input,
# because `$?` inside the braces is the `[` test, so it reports CLEAN.
#
# EVALS THE RUNNER'S OWN TEXT, the way link 5 of part 330 does, because an arm
# that re-derives the rule tests the world instead of the code.
_orw="$PGC_TESTDIR/run_all_versions.sh"
_orw_txt="$(awk '/^\t\t\tcase "\$_orph_rc" in$/{f=1} f{print} f&&/^\t\t\tesac$/{exit}' "$_orw")"
check "premise: the runner's orphan reduction was extracted, not an empty range" \
	"$(printf '%s\n' "$_orw_txt" | grep -c '_orph_orphan=1')" "1"

for _ord in "1 2" "2 1"; do
	_orph_orphan=0; _orph_broken=0
	for _orph_rc in $_ord; do eval "$_orw_txt"; done
	check "both conditions survive the reduction whatever order they arrive in ($_ord)" \
		"$_orph_orphan$_orph_broken" "11"
done
# CONTROL: it does not simply set both flags for everything.
_orph_orphan=0; _orph_broken=0
for _orph_rc in 0 0 0; do eval "$_orw_txt"; done
check "control: all-clean logs leave both flags down" "$_orph_orphan$_orph_broken" "00"
_orph_orphan=0; _orph_broken=0
for _orph_rc in 0 1 0; do eval "$_orw_txt"; done
check "control: one orphan among clean logs raises only the orphan flag" \
	"$_orph_orphan$_orph_broken" "10"

# WHY THIS REPORTS AND DOES NOT GATE -- pinned to the PRECONDITION, not to one
# instance of it.
#
# This arm used to grep `340` for `check_skip "the unreadable-source refusal"`: one
# skip standing in for three named arms, so on a box with no non-root user three
# committed rows had no record and were not removed checks. #998 removed that line,
# which SATISFIED the premise rather than breaking it -- and the arm as written would
# then have failed. Worse, it would have failed in `main`: the two PRs compose with a
# conflict only in the budget file, so nothing would have presented a marker to read.
# Found by @pgcolumnar-9b, by composing the merge rather than reasoning about it.
#
# THE PRECONDITION IS NOT "340 HAS THAT LINE". It is that somewhere in the corpus a
# skip still cannot be matched to the arms it stands in for -- while that holds, an
# absent record does not reliably mean a removed check, and a gate refusing on absence
# would redden a correct run. #998's sweep is the authority on that number, so this
# reads it instead of re-deriving it: a second implementation of one count is how two
# numbers come to disagree, which is the defect this file exists to catch.
#
# AND IT RETIRES ITSELF. The day `interpolated` and `armless` both reach zero this arm
# fails, and the fix for that failure is to arm the gate -- which is the direction the
# whole issue wants to go, stated as a check rather than as a comment somebody has to
# remember to re-read.
_orph_tool="$PGC_TESTDIR/../.github/scripts/skip-loop-arms.py"
check "premise: the skip-loop sweep is present, so its counts can be read" \
	"$([ -r "$_orph_tool" ] && echo yes || echo no)" "yes"
_orph_sweep="$(python3 "$_orph_tool" "$PGC_TESTDIR" 2>&1)" || _orph_sweep="TOOL FAILED"
check "premise: and it reported all four of its categories, so a zero is a measurement" \
	"$(printf '%s\n' "$_orph_sweep" | grep -cE '^(loops|compared|interpolated|armless) [0-9]+$')" "4"
_orph_i="$(printf '%s\n' "$_orph_sweep" | sed -n 's/^interpolated \([0-9]*\)$/\1/p')"
_orph_a="$(printf '%s\n' "$_orph_sweep" | sed -n 's/^armless \([0-9]*\)$/\1/p')"
check "the orphan scan stays a REPORT while any skip loop cannot be compared to its arms" \
	"$({ [ "${_orph_i:-0}" -gt 0 ] || [ "${_orph_a:-0}" -gt 0 ]; } && echo "not yet armable" || echo "armable: arm the gate")" \
	"not yet armable"
unset _orph_tool _orph_sweep _orph_i _orph_a
check "premise: and the two arms it stands in for are still named in the ledger" \
	"$(grep -cP '^harness_selftest\t340-the-binary-must-be-built-from\tpremise: the unprivileged' \
		"$_ledger")" "2"

# ---- the gate refuses a check the ledger has never seen ---------------------

: > "$_lw/g.tsv"
_led_run merge --ledger "$_lw/g.tsv" --date 2026-09-10 "$_lw/green.log" >/dev/null
printf 'suites_not_covered 0\n' > "$_lw/gb.txt"
check "a run whose checks are all ledgered passes the gate" \
	"$(_led_rc gate --ledger "$_lw/g.tsv" --budget "$_lw/gb.txt" --registered "$_lw/registered" "$_lw/green.log")" "0"
printf 'RESULT\tdemo\tpart1\tfirst check\tPASS\t18\t\nRESULT\tdemo\tpart1\tsecond check\tPASS\t18\t\nRESULT\tdemo\tpart1\tbrand new\tPASS\t18\t\nchecks run: 3\n' > "$_lw/new.log"
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
	"$(awk -F'\t' '$3=="brand new"{print $5}' "$_lw/g.tsv")" "never"

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

# ---- and EVERY JOB THAT RUNS THE MATRIX, not just the one that was fixed -----
#
# The arm above names ci.yml. nightly.yml has the same step and was never named,
# so it went on globbing only the build directory this runner deletes, and
# collected nothing on three consecutive reds. That is how #1248's aarch64
# failure stayed undiagnosable even after #1253 made its diagnosis
# unconditional: the diagnosis is printed into a per-suite log that the nightly
# never collected.
#
# SCOPED TO THE JOB, NOT THE FILE, and that distinction is the arm. nightly.yml
# carries TWO collection steps: the matrix job's, and the sanitizer gate's. The
# sanitizer job runs test/run_san.sh, never run_all_versions.sh, so it never
# writes /tmp/pgcolumnar-logs and is right not to read it. A file-scoped grep is
# satisfied by whichever step happens to carry the path and would pass this file
# while the matrix job's step was still wrong -- which is the defect, not a
# hypothetical. Named by @OffgridwithJD.
#
# The property is therefore: a job that INVOKES the matrix must collect from the
# path the matrix retains. Derived, so a second such job is covered on the day
# it is added rather than the day somebody remembers it.
_cl_pop=""
_cl_missing=""
for _cl_wf in "$PGC_SRCDIR"/.github/workflows/*.yml; do
	for _cl_job in $(awk '/^  [a-z][a-z0-9_-]*:$/{gsub(/[ :]/,"");print}' "$_cl_wf"); do
		_cl_body="$(awk -v j="  ${_cl_job}:" '$0==j{f=1;next} /^  [a-z][a-z0-9_-]*:$/{f=0} f' "$_cl_wf")"
		# A `case`, never a captured string piped into an early-exit reader.
		# Part 080 caught exactly that shape in the
		# first version of this loop: under pipefail the early-exiting reader
		# makes the writer take EPIPE, and the pipeline reports the pattern
		# ABSENT even when the body contains it -- which here would name a job
		# as missing the path while it has it.
		case "$_cl_body" in
			*"bash test/run_all_versions.sh"*) ;;
			*) continue ;;
		esac
		_cl_pop="$_cl_pop ${_cl_wf##*/}:${_cl_job}"
		case "$_cl_body" in
			*"/tmp/pgcolumnar-logs/*.log"*) ;;
			*) _cl_missing="$_cl_missing ${_cl_wf##*/}:${_cl_job}" ;;
		esac
	done
done
check_num "premise: more than one job runs the suite matrix, so this arm has a population" \
	"$(set -- $_cl_pop; [ $# -ge 2 ] && echo 1 || echo 0)" "1"
check "and every job that runs the suite matrix collects from the path it retains" \
	"$(set -- $_cl_missing; [ $# -eq 0 ] && echo none || echo "$*")" "none"

check "and it runs before the build directory is removed, which is the only place it can" \
	"$([ "$(grep -n 'pgc_ledger.py" gate' "$_rv" | cut -d: -f1)" -lt \
	    "$(grep -n 'rm -rf "\$builddir"' "$_rv" | tail -1 | cut -d: -f1)" ] && echo before || echo after)" "before"

# ---- the committed files agree ----------------------------------------------

_l_total="$(grep -c . "$_ledger" || true)"
_l_red="$(awk -F'\t' '$5!="never"' "$_ledger" | grep -c . || true)"
_l_never="$(awk -F'\t' '$5=="never"' "$_ledger" | grep -c . || true)"
echo "  ledger: inputs=$_l_total | observed red=$_l_red, never=$_l_never | sum=$((_l_red + _l_never))"
check "the ledger partitions into observed and never" "$((_l_red + _l_never))" "$_l_total"
check "premise: the ledger is not empty, so the partition means something" \
	"$([ "$_l_total" -gt 0 ] && echo yes || echo no)" "yes"
check "every committed row has six fields" \
	"$(awk -F'\t' 'NF!=6' "$_ledger" | grep -c . || true)" "0"
check "and none of them ends in a tab" "$(grep -cP '\t$' "$_ledger" || true)" "0"
check "the committed census matches the committed ledger" \
	"$(sed -n 's/^checks_never_observed_red //p' "$_budget")" "$_l_never"
check "the budget names a ceiling and a census, and says which is which" \
	"$(grep -cE '^(suites_not_covered|checks_never_observed_red) [0-9]+$' "$_budget")" "2"


# ---- #952: the census is printed AND compared -------------------------------
#
# `gate` printed `ledger census: rows=N` and never compared it to the budget's
# `checks_never_observed_red`. rc=0 on a twenty-row ledger claiming 5 was measured.
# Reporting is not enforcing.
#
# The case it catches is a MERGE, not a PR: two PRs each rewrite the census from the
# same base, so the composed tree keeps whichever side won the conflict while the
# ledger takes both sets of rows. So the comparison must need NO prior, and nothing
# in this block passes --against.
_cw="$PGC_WORKDIR/census952"; mkdir -p "$_cw"
printf 'demo\n' > "$_cw/reg"
: > "$_cw/led"
_led_run merge --ledger "$_cw/led" --date 2026-09-10 "$_lw/green.log" >/dev/null
check "premise: the fixture ledger holds two rows" \
	"$(grep -c . "$_cw/led" || true)" "2"
check "premise: both are never, so this ledger's census is two" \
	"$(awk -F'\t' '$5=="never"' "$_cw/led" | grep -c . || true)" "2"

printf 'suites_not_covered 0\nchecks_never_observed_red 2\n' > "$_cw/ok.txt"
check "a census that matches the ledger passes" \
	"$(_led_rc gate --ledger "$_cw/led" --budget "$_cw/ok.txt" --registered "$_cw/reg" "$_lw/green.log")" "0"
check "and the gate prints that it compared them" \
	"$(_led_run gate --ledger "$_cw/led" --budget "$_cw/ok.txt" --registered "$_cw/reg" "$_lw/green.log" \
	  | grep -c 'census stated 2, ledger holds 2')" "1"
check "and the census line it always printed is still there" \
	"$(_led_run gate --ledger "$_cw/led" --budget "$_cw/ok.txt" --registered "$_cw/reg" "$_lw/green.log" \
	  | grep -c 'ledger census: rows=2')" "1"

printf 'suites_not_covered 0\nchecks_never_observed_red 1\n' > "$_cw/low.txt"
check "a census that understates the ledger is refused" \
	"$(_led_rc gate --ledger "$_cw/led" --budget "$_cw/low.txt" --registered "$_cw/reg" "$_lw/green.log")" "1"
check "and the refusal quotes the claim beside the measurement" \
	"$(_led_run gate --ledger "$_cw/led" --budget "$_cw/low.txt" --registered "$_cw/reg" "$_lw/green.log" \
	  | grep -c 'checks_never_observed_red 1, the ledger holds 2')" "1"

# NOT A CEILING: a ceiling refuses a rise, and bounding this number deadlocks. What
# is refused is a contradiction, so overstating is refused too.
printf 'suites_not_covered 0\nchecks_never_observed_red 3\n' > "$_cw/high.txt"
check "a census that overstates the ledger is refused too, because this is not a ceiling" \
	"$(_led_rc gate --ledger "$_cw/led" --budget "$_cw/high.txt" --registered "$_cw/reg" "$_lw/green.log")" "1"

# Absence is not a contradiction. Every other gate fixture in this part states only
# suites_not_covered, so refusing here would redden arms testing other things; the
# committed budget is held to naming both by the arm above.
printf 'suites_not_covered 0\n' > "$_cw/absent.txt"
check "a budget stating no census is not refused, because absence is not a contradiction" \
	"$(_led_rc gate --ledger "$_cw/led" --budget "$_cw/absent.txt" --registered "$_cw/reg" "$_lw/green.log")" "0"
check "but the gate says so, so the skip is visible rather than silent" \
	"$(_led_run gate --ledger "$_cw/led" --budget "$_cw/absent.txt" --registered "$_cw/reg" "$_lw/green.log" \
	  | grep -c 'names no checks_never_observed_red')" "1"

# ---- the gate cannot refuse a check in a suite it has never seen -------------
#
# The suite restriction is the MEANING of suites_not_covered, not a softening of
# the refusal. Without it the gate refuses every check of every uncovered
# suites and reddens the whole matrix on its first run -- a gate somebody turns
# off within the week, which is the failure this issue family exists to prevent.
#
# It tightens on its own as suites are seeded, and the ceiling forces that
# direction.

printf 'RESULT\tother\tpartX\tsomething\tPASS\t18\t\nchecks run: 1\n' > "$_lw/othersuite.log"
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
printf 'RESULT\tother\tpartX\tsomething\tPASS\t18\t\nRESULT\tother\tpartX\tnewly added\tPASS\t18\t\nchecks run: 2\n' > "$_lw/other2.log"
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
awk -F'\t' 'NR<=2 {printf "RESULT\t%s\t%s\t%s\tPASS\t18\t\n", $1, $2, $3}' "$_ledger" > "$_mono_log"
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

# THE INVARIANT IS UNCHANGED AND THE LITERAL MOVED (#1104). The runner now passes
# `$_led_against`, which a `case` immediately above restricts to `auto` or
# `parent`. Both are POLICIES the tool resolves; neither is a ref the caller
# names, so the staleness this section refuses is still refused. The old form
# pinned the literal string `--against auto`, which could not express "a policy
# chosen from a closed set" and so failed a runner that still honours the rule.
#
# What must stay true is that no REF reaches that call site. The arm below checks
# the closed set, and the `origin/main` arm after it is unchanged and still the
# one that catches a remote name.
check "the runner asks the tool to resolve the prior rather than naming one" \
	"$(grep -A5 'pgc_ledger.py" gate' "$_rv" | grep -c -- '--against "\$_led_against"')" "1"

check "and the prior policy is chosen from a closed set, not taken as a ref" \
	"$(grep -cE '^[[:space:]]+auto\|parent\)' "$_rv")" "1"
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
printf 's\tp\ta\t18\tnever\t-\n' > "$_dist/led"
printf 'RESULT\ts\tp\ta\tPASS\t18\t\nchecks run: 1\n' > "$_dist/log"
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
printf 's\tp\ta\t18\tnever\t-\n' > "$_rr/led"
printf 'RESULT\ts\tp\ta\tPASS\t18\t\nchecks run: 1\n' > "$_rr/log"
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
printf 'RESULT\tdemo\tp\tthe check\tFAIL\t18\t\nRESULT\tdemo\tp\tother\tPASS\t18\t\nchecks run: 2\n' > "$_r946/red.log"
printf 'RESULT\tdemo\tp\tthe check\tPASS\t18\t\nRESULT\tdemo\tp\tother\tPASS\t18\t\nchecks run: 2\n' > "$_r946/green.log"

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

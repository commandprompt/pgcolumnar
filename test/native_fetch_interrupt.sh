#!/usr/bin/env bash
#
# pgColumnar fetch-path interrupt guard (#212).
#
# pgcolumnar_fetch_row is reached once per candidate item pointer by
# _bt_check_unique() during a unique INSERT, and each such call reads the
# row-group list out of the catalog (the unique check runs under SnapshotDirty,
# which #709's memo never serves). Before #212 that path had no CHECK_FOR_INTERRUPTS --
# the three checks already in columnar_reader.c are all on the scan/decode path,
# which a unique liveness check never enters -- so a unique INSERT whose conflict
# check did a lot of fetch work could not be cancelled and never noticed
# postmaster death. A backend spun there at 100% CPU for three days, outliving
# its cluster. The fix is one CHECK_FOR_INTERRUPTS at the top of the function.
#
# This check is structural, and on purpose. The behavioural proof -- with the
# guard the fetch loop cancels under a small statement_timeout (57014), without
# it the SAME statement runs to completion ignoring the timeout -- is real and is
# in the PR, but it is not a stable CI assertion: the loop only overruns a small
# timeout when the per-fetch catalog scan is paid enough times, the fixture that
# arranges that is O(dups^2) to build (each set-up delete pays the same scan), and
# the loop's exact length still shifts with build flags and catalog state enough
# to cross a 10 ms line either way. Pinning the guard's presence and placement is
# stable and is the actual regression: if someone removes it, this fails.
#
# Usage:  test/native_fetch_interrupt.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

# The body of a function in columnar_reader.c, from its definition to its closing
# brace, with its comments blanked, multi-line ones included: a guard commented out
# of the code is not a guard, and every check below passed with the call written
# as /* CHECK_FOR_INTERRUPTS(); */ (#1341). Each line keeps its place, so the
# placement check still compares line numbers of the code itself.
fn_body() {
	awk -v fn="$1" '$0 ~ "^" fn "\\(" {p=1} p{print} p&&/^}/{exit}' "$SRC/columnar_reader.c" |
		perl -0777 -pe 's{/\*.*?\*/}{ my $c = $&; $c =~ tr/\n//cd; $c }gse; s{//[^\n]*}{}g'
}
body="$(fn_body pgcolumnar_fetch_row)"

# The guard has to be in this function, not merely in the file. The interrupt
# checks elsewhere in columnar_reader.c are on the scan/decode path, which the
# unique liveness fetch never enters, so counting the file would pass vacuously.
check "pgcolumnar_fetch_row carries an interrupt check" \
	"$(printf '%s\n' "$body" | grep -c 'CHECK_FOR_INTERRUPTS' | awk '{print ($1>=1)?"yes":"no"}')" \
	"yes"

# And it has to run before the row-group resolution it guards, or the fetch
# does its work before ever reaching a cancellation point -- which is the state
# #212 was in. Since #709 the per-fetch catalog read lives behind
# pgcolumnar_lookup_row_group (memoized, but a miss or an uncached snapshot
# still reads the catalog), so that call is the guarded work; assert the
# check precedes it, and separately that the lookup helper is really where
# the catalog read moved, so this arm cannot rot into comparing against a
# call that no longer does the work.
cfi_line="$(printf '%s\n' "$body" | grep -n 'CHECK_FOR_INTERRUPTS' | head -1 | cut -d: -f1)"
# match the CALL, with its paren; comments are already blanked, so a comment
# that names it cannot satisfy a placement check
rgl_line="$(printf '%s\n' "$body" | grep -n 'pgcolumnar_lookup_row_group(' | head -1 | cut -d: -f1)"
check "the interrupt check precedes the row-group resolution" \
	"$( [ -n "$cfi_line" ] && [ -n "$rgl_line" ] && [ "$cfi_line" -lt "$rgl_line" ] && echo yes || echo no )" \
	"yes"

lookup_body="$(fn_body pgcolumnar_lookup_row_group)"
check "the row-group resolution is where the catalog read lives" \
	"$(printf '%s\n' "$lookup_body" | grep -c 'PgColumnarReadRowGroupList' | awk '{print ($1>=1)?"yes":"no"}')" \
	"yes"

pgc_summary

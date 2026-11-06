#!/usr/bin/env bash
#
# pgColumnar fetch-by-row-number with a column projection (issue #157).
#
# Fetching a row by number decoded every column of its row group whatever the
# caller wanted. Two entry points now exist beside it: one that decodes only a
# given set of columns, and one that answers visibility and decodes nothing.
#
# Neither has a SQL surface of its own, so this exercises them through the two
# callers that use them: pgcolumnar.read_projection, which needs the base row
# only for visibility, and pgcolumnar.reconstruct_via_projection, which needs
# exactly the columns the projection does not carry.
#
# Four things are asserted.
#
# 1. The output is unchanged. This is the whole risk of the change: a projection
#    that decodes too few columns returns a null where a value belongs, and
#    nothing raises. Checked against the same query with the projection dropped,
#    which reads the base table directly, so the oracle does not share the code
#    under test.
#
# 2. Visibility still comes from the base row. The liveness entry point stops
#    before decoding anything, so a delete that it failed to see would show up as
#    a row that should have disappeared and did not.
#
# 3. The empty-set case is right. When a projection covers every column the
#    computed set comes out empty, and a Bitmapset cannot tell empty from NULL.
#    The first version of this API read NULL as "every column", so that case
#    asked for the opposite of what it meant -- invisibly, because decoding
#    everything still returns the right answer. The set now says what it means,
#    which changes behaviour on that path, so the path is checked.
#
# 4. The call sites are the new ones. A wide fixture throughout, because on a
#    narrow table decoding one column against forty is not observable.
#
# Usage:  test/native_fetch_projection.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_FETCHPROJ_ROWS:-20000}
NCOLS=${PGC_FETCHPROJ_COLS:-40}

cols=""; sel=""
for i in $(seq 1 "$NCOLS"); do
	cols="$cols, c$i bigint"
	sel="$sel, g * $i"
done

psql_run "DROP TABLE IF EXISTS fp_w;
	CREATE TABLE fp_w (id int$cols) USING pgcolumnar;
	INSERT INTO fp_w SELECT g$sel FROM generate_series(1, $ROWS) g;" >/dev/null

# A literal name, so the ledger and the twin's grader can read it. The
# all-covering projection below names c1..c40, so NCOLS is 40 in practice.
check "the wide fixture has 41 columns" \
	"$(q "SELECT count(*) FROM information_schema.columns
		WHERE table_name = 'fp_w';")" "$((NCOLS + 1))"

psql_run "SELECT pgcolumnar.add_projection('fp_w', 'fp_p',
	ARRAY['id','c1'], ARRAY['id']);" >/dev/null

# --- 1. reconstruct returns the same rows as reading the table -----------------

# reconstruct_via_projection reads the covered columns from the projection and
# the rest from the base row, which is the projected-fetch path. The oracle is
# the base table itself, which does not go through that path at all.
mismatch="$(q "WITH viaproj AS (
		SELECT pgcolumnar.reconstruct_via_projection('fp_w','fp_p') AS r
	), direct AS (
		SELECT id::text || '|' || c1::text || '|' || c2::text || '|' ||
		       c${NCOLS}::text AS d
		FROM fp_w
	)
	SELECT count(*) FROM viaproj
	WHERE split_part(r, '|', 1) || '|' || split_part(r, '|', 2) || '|' ||
	      split_part(r, '|', 3) || '|' || split_part(r, '|', $((NCOLS + 1)))
	      NOT IN (SELECT d FROM direct);")"

check "every reconstructed row matches the base table" "$mismatch" "0"

# --- 2. visibility still comes from the base row ------------------------------

before="$(q "SELECT count(*) FROM pgcolumnar.read_projection('fp_w','fp_p');")"
check "the projection reads every row to start with" "$before" "$ROWS"

psql_run "DELETE FROM fp_w WHERE id <= 100;" >/dev/null

after="$(q "SELECT count(*) FROM pgcolumnar.read_projection('fp_w','fp_p');")"
check "a deleted row stops being read through the projection" \
	"$after" "$((ROWS - 100))"

# the same must hold for the reconstruct path, which uses the projected fetch
rafter="$(q "SELECT count(*) FROM pgcolumnar.reconstruct_via_projection('fp_w','fp_p');")"
check "and stops being reconstructed too" "$rafter" "$((ROWS - 100))"

# a value from an uncovered column is still right after the delete, so the
# projection set was not narrowed by one column too many
check "an uncovered column still reads correctly after a delete" \
	"$(q "SELECT split_part(r, '|', $((NCOLS + 1)))
		FROM pgcolumnar.reconstruct_via_projection('fp_w','fp_p') AS r
		WHERE split_part(r, '|', 1) = '101';")" \
	"$(q "SELECT c${NCOLS}::text FROM fp_w WHERE id = 101;")"

# --- 3. a projection that covers every column ---------------------------------

# The set of columns to read from the base row is computed, and here it comes out
# EMPTY, because the projection carries all of them. That is the case the old
# "NULL means every column" convention got backwards: an empty computed set is
# indistinguishable from NULL, so it asked for every column instead of none. It
# still returned the right answer, which is why it was invisible -- it just did
# the whole decode this change exists to avoid.
#
# The fix makes the set say what it means, so this path now decodes nothing. That
# is a real behaviour change on a live path, so it is checked rather than assumed:
# every value here has to come from the projection and still be right.
psql_run "DROP TABLE IF EXISTS fp_all;
	CREATE TABLE fp_all (id int$cols) USING pgcolumnar;
	INSERT INTO fp_all SELECT g$sel FROM generate_series(1, 2000) g;" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('fp_all', 'fp_ap',
	ARRAY['id', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7', 'c8', 'c9', 'c10', 'c11', 'c12', 'c13', 'c14', 'c15', 'c16', 'c17', 'c18', 'c19', 'c20', 'c21', 'c22', 'c23', 'c24', 'c25', 'c26', 'c27', 'c28', 'c29', 'c30', 'c31', 'c32', 'c33', 'c34', 'c35', 'c36', 'c37', 'c38', 'c39', 'c40'], ARRAY['id']);" >/dev/null

check "the all-covering projection reconstructs every row" \
	"$(q "SELECT count(*) FROM pgcolumnar.reconstruct_via_projection('fp_all','fp_ap');")" \
	"2000"

check "and its values are right with nothing decoded from the base" \
	"$(q "SELECT split_part(r, '|', 2) || '/' || split_part(r, '|', $((NCOLS + 1)))
		FROM pgcolumnar.reconstruct_via_projection('fp_all','fp_ap') AS r
		WHERE split_part(r, '|', 1) = '77';")" \
	"$(q "SELECT c1::text || '/' || c${NCOLS}::text FROM fp_all WHERE id = 77;")"

# --- 4. the entry points are the ones being used ------------------------------

# The timing difference is real but modest, because the decoded-group cache
# already amortises the decode across a group, so it is not asserted here. These
# pin the call sites instead: a revert to the full-decode entry point would pass
# every check above while giving back what the change was for.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

# Every source arm below reads CODE, not prose. The index-deletion arm used to
# pass on a comment: pgcolumnar_index_delete_tuples stopped asking any row
# whether it is live (it vouches for nothing, #1341), and the only
# PgColumnarRowIsLive left in it was the comment explaining why. String literals
# are kept, so a "/*" inside one is not read as a comment.
code_of() {	# code_of FILE -> FILE with its C comments blanked, line count kept
	python3 -c '
import re, sys
s = open(sys.argv[1]).read()
pat = re.compile(r"\"(?:\\.|[^\"\\])*\"|\x27(?:\\.|[^\x27\\])*\x27|/\*.*?\*/|//[^\n]*", re.S)
sys.stdout.write(pat.sub(lambda m: m.group(0) if m.group(0)[0] in "\"\x27" else "\n" * m.group(0).count("\n"), s))
' "$1"
}

# THE ARM NAMED "DECODES NOTHING" COULD NOT SEE A DECODE. Both of these counted a
# CALL SITE, with its argument text, and pinned it at a literal 1 -- so they
# asserted that a particular call is still written the way it was written, which is
# not the property either name claims. Measured: plant the regression these arms
# exist to catch, a full `PgColumnarReadRowByNumber` beside the liveness check in
# the visibility path, and BOTH arms stay green, because the call they count is
# still there and the decode added next to it is invisible to them.
#
# The property is a ZERO, not a one. There are three entry points and only one of
# them decodes every column:
#
#     PgColumnarRowIsLive            answers visibility, decodes nothing
#     PgColumnarReadRowByNumberCols  decodes a given set of columns
#     PgColumnarReadRowByNumber      decodes EVERY column -- the thing to stay out
#
# So assert the file never calls the full decode. A want-zero count is also the
# shape a second honest caller cannot break: more correct callers of the two narrow
# entry points move nothing, while any full decode moves it off zero. That is the
# opposite failure direction from a count pinned at 1, which reddens on correct
# additions and stays green on wrong ones.
#
# The two premises keep the zero from being vacuous: a file that called NOTHING
# would also report zero full decodes.
_np_code="$(code_of "$SRC/columnar_projection.c")"
_np_live="$(grep -c 'PgColumnarRowIsLive(' <<<"$_np_code")"
_np_cols="$(grep -c 'PgColumnarReadRowByNumberCols(' <<<"$_np_code")"
_np_full="$(grep -c 'PgColumnarReadRowByNumber(' <<<"$_np_code")"

check "premise: the visibility-only entry point is called at all" \
	"$([ "${_np_live:-0}" -ge 1 ] && echo yes || echo no)" "yes"

check "premise: the column-set entry point is called at all" \
	"$([ "${_np_cols:-0}" -ge 1 ] && echo yes || echo no)" "yes"

# `PgColumnarReadRowByNumberCols(` does not match `PgColumnarReadRowByNumber(`, so
# the narrow caller is not counted as a full decode.
#
# THIS PREMISE DOCUMENTS THE INTENT; IT IS NOT WHAT PROVIDES THE GUARANTEE, and
# the first version of this comment said it was. A broken prefix relationship
# cannot pass silently, because the other two arms already contradict each other
# under it: if `Cols(` matched the wide pattern then every narrow call would be
# counted twice, so `full >= cols`, and `cols >= 1` with `full == 0` is a
# contradiction. The arm reddens rather than hiding.
#
#     cols=1, prefix intact   -> full=0   arm PASS
#     cols=1, prefix broken   -> full=1   arm RED
#     cols=3, prefix broken   -> full=3   arm RED
#
# Kept because a reader should not have to derive that, and because it names the
# assumption a future rename would break. Correction from @OffgridwithJD's review.
check "premise: the column-set caller is not counted as a full decode" \
	"$(printf 'PgColumnarReadRowByNumberCols(a, b)\n' | grep -c 'PgColumnarReadRowByNumber(')" "0"

check "neither caller decodes every column: no full decode in the projection path" \
	"$_np_full full decode(s)" "0 full decode(s)"

# Scoped to the function rather than counting a string across the file: the
# string appears legitimately elsewhere now that the index fetch also asks only
# for liveness, and a whole-file count turned that correct second use into a
# failure.
#
# The first arm was "index deletion asks whether the row is live". It stopped
# being true when the function stopped asking (an answer from this backend's
# snapshot erased index entries of rows live to everyone else), and it stayed
# green on the comment that says so. The property now is the absence: a
# per-row liveness test here is the regression, so it is what reddens.
deltuples="$(code_of "$SRC/columnar_tableam.c" | awk '/^pgcolumnar_index_delete_tuples\(/,/^}/')"

check "premise: the index deletion function was extracted, not blank" \
	"$(case "$deltuples" in *ndeltids*) echo yes ;; *) echo "extracted=<${deltuples:-empty}>" ;; esac)" "yes"

check "index deletion asks no row whether it is live: it vouches for nothing" \
	"$(case "$deltuples" in *PgColumnarRowIsLive*) echo "no (asks per row)" ;; *) echo yes ;; esac)" "yes"

check "and does not decode the row to find out" \
	"$(case "$deltuples" in *PgColumnarReadRowByNumber*) echo "no (still decodes)" ;;
		*) echo yes ;; esac)" "yes"

# and the convention that made an empty set mean its opposite stays gone: the
# worker takes an explicit flag, so "every column" cannot be spelled as a set.
#
# PIN THE PROPERTY, NOT THE CALLER COUNT. Both arms here counted the guarded
# form across the file and compared it against a literal 1, which asserts how
# many honest callers exist rather than that every caller is honest. That is the
# same defect the `deltuples` comment 15 lines above records, left in place in
# two arms after being fixed in one, and it fired again the moment a second
# correct caller arrived (#1077's coalescing read, which takes the flag and
# tests it exactly as the convention demands, and was failed for it).
#
# The property is that EVERY membership test in the needed-set consults the flag
# first. Honest callers move both counts together; an unguarded test moves only
# the total. Both numbers are printed in the arm so the comparison is the
# reconciliation rather than a bare verdict.
_afc_code="$(code_of "$SRC/columnar_reader.c")"
_afc_tests="$(grep -c 'bms_is_member(c, needed)' <<<"$_afc_code")"
_afc_guarded="$(grep -c '!allColumns && !bms_is_member(c, needed)' <<<"$_afc_code")"
_afc_flags="$(grep -c 'bool allColumns' <<<"$_afc_code")"

check "premise: there is a needed-set membership test to guard" \
	"$([ "${_afc_tests:-0}" -ge 1 ] && echo yes || echo no)" "yes"

check "premise: the explicit all-columns flag is declared" \
	"$([ "${_afc_flags:-0}" -ge 1 ] && echo yes || echo no)" "yes"

# A LITERAL MATCH CANNOT TELL "written differently" FROM "written wrongly", and
# both readings of a mismatch are live. Reversed operands, a renamed variable, a
# `pgindent` wrap across two lines, or the positive form all redden this pin while
# the tree is correct -- and this repository already has a guard that failed for
# not joining line continuations. Failing closed is the right direction, but the
# message has to say what the two readings are or the next reader spends the
# afternoon hunting a caller that does not exist.
#
# Emitted only on mismatch, and BEFORE the check, so the check's NAME stays the
# key the ledger records.
if [ "$_afc_guarded" != "$_afc_tests" ]; then
	echo "  note: $_afc_tests needed-set membership test(s) in columnar_reader.c," \
		"$_afc_guarded of them guarded."
	echo "  note: either a test was added without the flag, OR a correct test is" \
		"written in a form this literal match does not recognise -- reversed" \
		"operands, a wrapped line, a renamed variable, the positive form."
	echo "  note: read the sites before assuming the first."
fi

check "every needed-set membership test consults that flag rather than a null set" \
	"guarded $_afc_guarded of $_afc_tests" "guarded $_afc_tests of $_afc_tests"

pgc_summary

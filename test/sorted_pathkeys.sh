#!/usr/bin/env bash
#
# pgColumnar ordered paths on a physically sorted table (#751).
#
# pgcolumnar.vacuum_sorted physically orders a relation, and until this change
# the columnar scan told the planner nothing about it: every pathkeys field in
# the tree was NIL, so ORDER BY on a table that was already in that order still
# planned a Sort over the whole scan, and ORDER BY ... LIMIT n could not stop
# early.
#
# THE FAILURE MODE THIS SUITE EXISTS FOR IS SILENT WRONGNESS, NOT A MISSING
# SPEED-UP. A scan that claims an ordering the rows are not in produces wrong
# answers for LIMIT and for merge joins, and no correctness test on unordered
# data would notice, because the planner would put a Sort above it anyway. So
# the arms are in two groups and the first group is the important one:
#
#   REFUSAL arms   -- every shape where the rows are NOT in the claimed order
#                     must plan a Sort, AND must return the same rows as a heap
#                     table holding identical data. These pass trivially when
#                     no pathkeys exist at all, so they are proved by the
#                     over-claiming mutation described in the PR, not by being
#                     green here.
#   CLAIM arms     -- the shapes where the ordering is real must lose the Sort.
#                     These are what goes red without the change.
#
# Every arm that asserts a plan also asserts the ANSWER against a heap oracle
# with the same rows, because a plan check alone cannot see a wrong result and
# an answer check alone cannot see that the Sort was never removed.
#
# Usage:  test/sorted_pathkeys.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# pgcolumnar.parallel_copy prepares one transaction per worker, and
# max_prepared_transactions cannot be raised without a restart, so it is set
# before the cluster starts rather than with a SET that would silently not take.
# The parallel_copy arm is the only place the invalidation has to cross a
# process boundary; without this it errors out and the arm reads as a failure
# of the product rather than of the fixture.
export PGC_EXTRA_CONF="max_prepared_transactions=8"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

pgc_check_ordered_oracle

# Does the plan for this query contain a Sort (or Incremental Sort) node?
sorts() {	# sorts QUERY -> yes|no
	# grep -c on a captured value, not a pipe into grep -q; see lib.sh's
	# pgc_is_columnar_scan for the mechanism and the measurement.
	local _plan
	_plan="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (COSTS OFF) $1" 2>/dev/null)"
	[ "$(grep -cE '^ *(->)? *(Incremental )?Sort' <<<"$_plan" || true)" -ne 0 ] \
		&& echo yes || echo no
}
inv() {		# inversions on the lead column in the order the scan returns rows
	q "SELECT count(*) FROM (SELECT $2, lag($2) OVER () AS p FROM $1) s WHERE p > $2;"
}
ss() { q "SELECT $2 FROM pgcolumnar.sort_status('$1');"; }
# rows that break (k ASC NULLS LAST, j ASC) in scan order, the order vacuum_sorted
# ('c', 'k', 'j') promises (#1339). inv cannot see it: `p > k` is NULL when either
# side is, so NULLs sorted first count nothing, and it never reads j, so ties on k in
# any j order count nothing. Measured: with NULLS FIRST, and with the sort on k alone,
# the fixture check that used inv stayed green.
order_breaks() {
	q "SELECT count(*) FROM (SELECT k, j, lag(k) OVER () AS pk, lag(j) OVER () AS pj, row_number() OVER () AS rn FROM $1) s WHERE rn > 1 AND ((pk IS NULL AND k IS NOT NULL) OR pk > k OR (pk IS NOT DISTINCT FROM k AND pj > j));"
}

# ---------------------------------------------------------------- the fixture
#
# A columnar table and a heap table holding identical rows. Every answer arm
# compares the two, so a claimed ordering that reorders or drops rows is caught
# by the answer and not only by the plan.
#
# k has duplicates and NULLs on purpose: NULLS LAST is part of what the claim
# says, and a tie on k is where a wrong secondary order would show.
psql_run "CREATE TABLE h (id int, k int, j int, t text) USING heap;"
psql_run "INSERT INTO h SELECT g, CASE WHEN g % 97 = 0 THEN NULL ELSE (g*7919)%500 END, g%13, 'v'||g FROM generate_series(1,20000) g;"
psql_run "CREATE TABLE c (id int, k int, j int, t text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('c', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO c SELECT * FROM h;"
psql_run "SELECT pgcolumnar.vacuum_sorted('c', 'k', 'j');"

check_num "the fixture is physically ordered on (k, j), NULLs last" "$(order_breaks c)" "0"
check "with no unsorted tail" "$(ss c appended_groups)" "0"
check "recorded as a lexicographic run" \
	"$(q "SELECT sorted_kind FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('c');")" \
	"lexicographic"
check "on the key it was given" \
	"$(q "SELECT sorted_by::text FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('c');")" \
	"{k,j}"
check "premise: the fixture has NULLs in the sort column" \
	"$([ "$(q 'SELECT count(*) FROM c WHERE k IS NULL;')" -gt 0 ] && echo yes || echo no)" "yes"
check "premise: and ties on it" \
	"$([ "$(q 'SELECT count(*) FROM (SELECT k FROM c WHERE k IS NOT NULL GROUP BY k HAVING count(*) > 1) s;')" -gt 0 ] && echo yes || echo no)" "yes"
check "premise: the columnar table is read by the columnar scan" \
	"$(pgc_is_columnar_scan 'SELECT k FROM c ORDER BY k')" "yes"

# ansp LABEL HEAP COLUMNAR QUERY-with-%T -- the columnar answer must equal the
# heap answer, ROW ORDER INCLUDED (pgc_seq_hash, not pgc_set_hash). A set
# comparison cannot fail on order, which is the only thing a wrong pathkey
# claim breaks.
ansp() {
	local label="$1" ht="$2" ct="$3" tmpl="$4"
	check_text "$label" "$(pgc_seq_hash "${tmpl//%T/$ct}")" "$(pgc_seq_hash "${tmpl//%T/$ht}")"
}
ans() { ansp "$1" h c "$2"; }

# ============================================================== CLAIM arms
#
# The ordering is real on these shapes, so the Sort must be gone. Each is
# paired with the answer, because losing the Sort is only correct if the rows
# still come back in that order.

check "ORDER BY the sort key plans no Sort" "$(sorts 'SELECT k FROM c ORDER BY k')" "no"
ans   "and returns the same rows in the same order as heap" \
	'SELECT id, k, j FROM %T ORDER BY k, j, id'

check "ORDER BY the full key plans no Sort" "$(sorts 'SELECT k, j FROM c ORDER BY k, j')" "no"
check "ORDER BY the key prefix plans no Sort" "$(sorts 'SELECT k FROM c ORDER BY k ASC NULLS LAST')" "no"

check "ORDER BY k LIMIT plans no Sort" "$(sorts 'SELECT k FROM c ORDER BY k LIMIT 10')" "no"
ans   "and LIMIT returns the same first rows as heap" \
	'SELECT k, j FROM %T ORDER BY k NULLS LAST, j LIMIT 10'
ans   "and a larger LIMIT does too" \
	'SELECT k, j, id FROM %T ORDER BY k NULLS LAST, j, id LIMIT 500'

# A constant leading key is SKIPPED and the prefix continues, mirroring core's
# build_index_pathkeys. Every row the scan returns has k = 5, so within that
# restriction the rows are ordered by j and ORDER BY j is satisfied by the run
# on (k,j). Without the skip-and-continue this would end the prefix at k and
# plan a Sort. The bare "ORDER BY j" refusal arm below is its control: j alone,
# with no equality on k, is NOT an order the rows are in.
check "premise: the equality really selects rows, so the arm is not empty" \
	"$([ "$(q 'SELECT count(*) FROM c WHERE k = 5;')" -gt 1 ] && echo yes || echo no)" "yes"
check "a constant leading key is skipped, so ORDER BY the next key plans no Sort" \
	"$(sorts 'SELECT j FROM c WHERE k = 5 ORDER BY j')" "no"
ansp  "and it answers in j order, matching heap" h c \
	'SELECT j, id FROM %T WHERE k = 5 ORDER BY j, id'

check "MIN over the sort key plans no Sort" "$(sorts 'SELECT k FROM c WHERE k IS NOT NULL ORDER BY k LIMIT 1')" "no"
ans   "and the first row matches heap" \
	'SELECT k FROM %T WHERE k IS NOT NULL ORDER BY k LIMIT 1'

# ============================================================= REFUSAL arms
#
# Each of these is a shape where the physical order does NOT satisfy the
# requested order. A Sort must remain. These arms are green with no feature at
# all, which is exactly why the PR proves them by over-claiming instead.

check "REFUSE: DESC is not the order the rows are in" \
	"$(sorts 'SELECT k FROM c ORDER BY k DESC')" "yes"
ans   "and DESC still answers correctly" 'SELECT k, j, id FROM %T ORDER BY k DESC NULLS FIRST, j DESC, id DESC LIMIT 200'

check "REFUSE: NULLS FIRST is not the null placement the rows are in" \
	"$(sorts 'SELECT k FROM c ORDER BY k NULLS FIRST')" "yes"
ans   "and NULLS FIRST still answers correctly" 'SELECT k, id FROM %T ORDER BY k NULLS FIRST, id LIMIT 300'

check "REFUSE: a non-prefix of the key is not an order the rows are in" \
	"$(sorts 'SELECT j FROM c ORDER BY j')" "yes"
ans   "and a non-prefix still answers correctly" 'SELECT j, id FROM %T ORDER BY j, id LIMIT 300'

check "REFUSE: a column that is not in the key at all" \
	"$(sorts 'SELECT id FROM c ORDER BY id')" "yes"
ans   "and a non-key column still answers correctly" 'SELECT id FROM %T ORDER BY id LIMIT 300'

check "REFUSE: the key columns in the wrong order" \
	"$(sorts 'SELECT k, j FROM c ORDER BY j, k')" "yes"

# --- an unsorted tail: the run is still ordered, the relation is not ---------

psql_run "CREATE TABLE tailc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('tailc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO tailc SELECT * FROM c;"
psql_run "SELECT pgcolumnar.vacuum_sorted('tailc', 'k', 'j');"
psql_run "CREATE TABLE tailh (LIKE h) USING heap;"
psql_run "INSERT INTO tailh SELECT * FROM tailc;"
# Rows whose k values fall BELOW the run's minimum, so a scan that returned the
# run first and the tail afterwards would give a wrong LIMIT answer rather than
# merely an unordered one.
psql_run "INSERT INTO tailc SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g;"
psql_run "INSERT INTO tailh SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g;"

check "premise: the tail really appended past the run" \
	"$([ "$(ss tailc appended_groups)" -gt 0 ] && echo yes || echo no)" "yes"
check "premise: and the relation is no longer in k order" \
	"$([ "$(inv tailc k)" -gt 0 ] && echo yes || echo no)" "yes"
# Read from the HEAP twin, not from tailc. min() over the columnar table is
# itself a candidate for the ordered path, so a premise taken there would be
# measuring the thing under test: an over-claiming build answered it wrongly.
check "premise: the tail holds values below the run's minimum" \
	"$(q 'SELECT (min(k) < 0)::text FROM tailh;')" "true"
check "REFUSE: a run with an appended tail is not an ordered relation" \
	"$(sorts 'SELECT k FROM tailc ORDER BY k')" "yes"
# The arm that would catch a wrong claim as a WRONG ANSWER rather than a slow
# plan: with the tail below the run, the first ten rows of a claimed order are
# not the first ten rows.
ansp  "and ORDER BY k LIMIT still returns the true first rows" tailh tailc \
	'SELECT k, id FROM %T ORDER BY k NULLS LAST, id LIMIT 10'
ansp  "and the whole ordered result matches heap with the tail appended" tailh tailc \
	'SELECT k, j, id FROM %T ORDER BY k NULLS LAST, j, id'

# --- a Z-order run: an order, but not a sort on any one column --------------

psql_run "CREATE TABLE zc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('zc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO zc SELECT * FROM h WHERE k IS NOT NULL;"
psql_run "SELECT pgcolumnar.set_options('zc', sort_by => ARRAY['k','j']::name[]);"
psql_run "SELECT pgcolumnar.cluster('zc', 'k', 'j');"
psql_run "CREATE TABLE zh (LIKE h) USING heap;"
psql_run "INSERT INTO zh SELECT * FROM zc;"

check "the Z-ordered table records a full run with no tail" \
	"$(ss zc appended_groups)" "0"
check "recorded as a zorder run, not lexicographic" \
	"$(q "SELECT sorted_kind FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('zc');")" \
	"zorder"
check "premise: and it is NOT in k order" \
	"$([ "$(inv zc k)" -gt 0 ] && echo yes || echo no)" "yes"
check "REFUSE: a Z-order run is not a sort on its lead column" \
	"$(sorts 'SELECT k FROM zc ORDER BY k')" "yes"
ansp  "and the Z-order run still answers correctly" zh zc \
	'SELECT k, j, id FROM %T ORDER BY k, j, id LIMIT 300'

# --- an unsorted relation ---------------------------------------------------

psql_run "CREATE TABLE uc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('uc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO uc SELECT * FROM h;"
psql_run "SELECT pgcolumnar.set_options('uc', sort_by => ARRAY['k','j']::name[]);"
check "premise: an unsorted relation records no kind" \
	"$(q "SELECT coalesce(sorted_kind,'<NULL>') FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('uc');")" \
	"<NULL>"
check "premise: even though sort_status reports the declared key" "$(ss uc sort_key)" "{k,j}"
check "REFUSE: a DECLARED sort key is an intention, not a layout" \
	"$(sorts 'SELECT k FROM uc ORDER BY k')" "yes"

# --- an unsorted rewrite retracts the claim ---------------------------------

psql_run "CREATE TABLE rc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('rc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO rc SELECT * FROM h;"
psql_run "SELECT pgcolumnar.vacuum_sorted('rc', 'k', 'j');"
# "The claim is live" is an arm, not a premise (#1339, #1236): a vacuum_sorted
# that records no claim reddens it, here and in the four checks like it below.
check "the claim is live before the unsorted rewrite" \
	"$(sorts 'SELECT k FROM rc ORDER BY k')" "no"
psql_run "SELECT pgcolumnar.vacuum('rc');"
check "REFUSE: an unsorted vacuum retracts the ordered path" \
	"$(sorts 'SELECT k FROM rc ORDER BY k')" "yes"

# --- a collatable sort key is refused, and here is the wrong answer it saves --
#
# Only the column NAMES are recorded, so nothing at plan time can tell whether
# the collation the rewrite sorted under is still the column's collation. And
# it can change with no rewrite at all: ALTER COLUMN k TYPE text COLLATE X on a
# column that is already text needs no transformation, so PostgreSQL updates
# pg_attribute and leaves every stored row where it is.
#
# The values are chosen so the two collations DISAGREE: in C, 'B' (0x42) sorts
# before 'a' (0x61), and in en_US it does not. Without that the arm cannot fail.

psql_run "CREATE TABLE colh (id int, k text COLLATE \"C\") USING heap;"
psql_run "INSERT INTO colh SELECT g, (ARRAY['aB','Ab','aa','AA','Ba','bA','_x','Zz'])[1+(g%8)] || g FROM generate_series(1,4000) g;"
psql_run "CREATE TABLE colc (id int, k text COLLATE \"C\") USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('colc', stripe_row_limit => 1000, chunk_group_row_limit => 250);"
psql_run "INSERT INTO colc SELECT * FROM colh;"
psql_run "SELECT pgcolumnar.vacuum_sorted('colc', 'k');"

check "the rewrite recorded a lexicographic run on the text column" \
	"$(q "SELECT sorted_kind FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('colc');")" \
	"lexicographic"
check "with no tail, so only the collation stands between it and a claim" \
	"$(ss colc appended_groups)" "0"
check "REFUSE: a collatable sort column is not claimed, whatever its collation" \
	"$(sorts 'SELECT k FROM colc ORDER BY k')" "yes"
ansp  "and it answers in C order, matching heap" colh colc \
	'SELECT k, id FROM %T ORDER BY k, id'

# The demonstration of WHY: a collation-only ALTER changes the ordering the
# column asks for without rewriting a single row. It needs two collations that
# DISAGREE, and a server that has one is not guaranteed, so this half degrades
# to a named skip. The refusal itself is asserted above, on COLLATE "C", which
# every server has.
ALTCOLL="$(q "SELECT collname FROM pg_collation WHERE collname IN ('en_US.utf8','en_US.UTF-8','en_US','und-x-icu') ORDER BY 1 LIMIT 1;")"
if [ -z "$ALTCOLL" ] || \
   [ "$(q "SELECT (min(k) COLLATE \"C\") = (SELECT min(k COLLATE \"$ALTCOLL\") FROM colh) FROM colh;" 2>/dev/null)" != "f" ]; then
	# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994). Seven arms sat behind one
	# skip named for none of them, so a reader could not tell which seven did not
	# run. Two of them go through `ansp`, which records under its first argument --
	# they are arms like any other and were missed by a sweep that looked only for
	# `check`.
	for _sp_n in "premise: C and the alternate collation really disagree on this data" \
			"premise: the collation ALTER rewrote nothing (same storage id)" \
			"so the run is still recorded as lexicographic" \
			"premise: and the column's collation really did change" \
			"REFUSE: the order the rows are in is no longer the order the column asks for" \
			"and ORDER BY k LIMIT returns the new collation's first rows, matching heap" \
			"and the whole ordered result matches heap under the new collation"; do
		check_skip "$_sp_n" \
			"SKIP  $_sp_n (this server has no collation that disagrees with C on ASCII)" \
			"this server has no suitable collation"
	done
	echo "      disagrees with C on ASCII, so the arm could not fail and is not run."
	echo "      The refusal it demonstrates is asserted above on COLLATE \"C\"."
else
	check "premise: C and the alternate collation really disagree on this data" \
		"$([ "$(q "SELECT k FROM colh ORDER BY k COLLATE \"C\" LIMIT 1;")" \
		 != "$(q "SELECT k FROM colh ORDER BY k COLLATE \"$ALTCOLL\" LIMIT 1;")" ] && echo yes || echo no)" "yes"

	SID_BEFORE_ALTER="$(storage_id_of colc)"
	psql_run "ALTER TABLE colc ALTER COLUMN k TYPE text COLLATE \"$ALTCOLL\";"
	psql_run "ALTER TABLE colh ALTER COLUMN k TYPE text COLLATE \"$ALTCOLL\";"
	check "premise: the collation ALTER rewrote nothing (same storage id)" \
		"$([ "$SID_BEFORE_ALTER" = "$(storage_id_of colc)" ] && echo same || echo rewritten)" "same"
	check "so the run is still recorded as lexicographic" \
		"$(q "SELECT sorted_kind FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('colc');")" \
		"lexicographic"
	check "premise: and the column's collation really did change" \
		"$(q "SELECT collname FROM pg_collation WHERE oid = (SELECT attcollation FROM pg_attribute WHERE attrelid = 'colc'::regclass AND attname = 'k');")" \
		"$ALTCOLL"
	check "REFUSE: the order the rows are in is no longer the order the column asks for" \
		"$(sorts 'SELECT k FROM colc ORDER BY k')" "yes"
	# Without the refusal this returned the C order, AA1003|AA1011|AA1019, where
	# the answer is aa10|aa1002|AA1003. A wrong answer from a plan with no Sort.
	ansp  "and ORDER BY k LIMIT returns the new collation's first rows, matching heap" colh colc \
		'SELECT k, id FROM %T ORDER BY k, id LIMIT 3'
	ansp  "and the whole ordered result matches heap under the new collation" colh colc \
		'SELECT k, id FROM %T ORDER BY k, id'
fi

# --- which types the collation refusal actually covers -----------------------
#
# The refusal is `OidIsValid(att->attcollation)`, and the claim is that this is
# EXACT for "has an ordering that can change under us". A type where
# attcollation is InvalidOid and the ordering can still change would be a wrong
# answer the refusal does not reach. These arms pin the reason for each family
# rather than the reasoning, because the reasoning is what would rot.

psql_run "CREATE DOMAIN dom_t AS text COLLATE \"C\";"
psql_run "CREATE TABLE t_dom (id int, k dom_t) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('t_dom', stripe_row_limit => 1000, chunk_group_row_limit => 250);"
psql_run "INSERT INTO t_dom SELECT g, ('v' || g)::dom_t FROM generate_series(1,2000) g;"
psql_run "SELECT pgcolumnar.vacuum_sorted('t_dom', 'k');"
check "REFUSE: a DOMAIN over text carries the base type's collation" \
	"$(sorts 'SELECT k FROM t_dom ORDER BY k')" "yes"

psql_run "CREATE TABLE t_arr (id int, k text[]) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('t_arr', stripe_row_limit => 1000, chunk_group_row_limit => 250);"
psql_run "INSERT INTO t_arr SELECT g, ARRAY['v' || g] FROM generate_series(1,2000) g;"
psql_run "SELECT pgcolumnar.vacuum_sorted('t_arr', 'k');"
check "REFUSE: an ARRAY of a collatable type is collatable" \
	"$(sorts 'SELECT k FROM t_arr ORDER BY k')" "yes"

# A COMPOSITE has attcollation InvalidOid while comparing by its FIELD
# collations, which looks like a hole. It is closed by PostgreSQL, not by this
# extension: a composite's attribute cannot be altered while any column uses the
# type. The arm asserts the refusal, so if that ever stops being true this goes
# red rather than going quietly wrong.
psql_run "CREATE TYPE comp_t AS (a text COLLATE \"C\", b int);"
psql_run "CREATE TABLE t_comp (id int, k comp_t) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('t_comp', stripe_row_limit => 1000, chunk_group_row_limit => 250);"
psql_run "INSERT INTO t_comp SELECT g, ROW('v' || g, g)::comp_t FROM generate_series(1,2000) g;"
psql_run "SELECT pgcolumnar.vacuum_sorted('t_comp', 'k');"
check "premise: a composite column's attcollation is InvalidOid, so it IS claimed" \
	"$(q "SELECT (attcollation = 0)::text FROM pg_attribute WHERE attrelid = 't_comp'::regclass AND attname = 'k';")" \
	"true"
check "and it is claimed" "$(sorts 'SELECT k FROM t_comp ORDER BY k')" "no"
check_text "PostgreSQL refuses to change a composite's field collation while a column uses it" \
	"$(psql_run "ALTER TYPE comp_t ALTER ATTRIBUTE a TYPE text COLLATE \"C\" CASCADE;" 2>&1 | grep -oE 'cannot alter type' | head -1)" \
	"cannot alter type"

# An ENUM also has attcollation InvalidOid. ALTER TYPE ... ADD VALUE ... BEFORE
# slots a new value in without renumbering the existing ones, and the new value
# cannot be in already-stored rows, so the stored order survives.
psql_run "CREATE TYPE enum_t AS ENUM ('b','d','f');"
psql_run "CREATE TABLE t_enum (id int, k enum_t) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('t_enum', stripe_row_limit => 1000, chunk_group_row_limit => 250);"
psql_run "INSERT INTO t_enum SELECT g, (ARRAY['b','d','f'])[1+(g%3)]::enum_t FROM generate_series(1,2000) g;"
psql_run "CREATE TABLE t_enum_h (id int, k enum_t) USING heap;"
psql_run "INSERT INTO t_enum_h SELECT * FROM t_enum;"
psql_run "SELECT pgcolumnar.vacuum_sorted('t_enum', 'k');"
check "an enum sort key is claimed" "$(sorts 'SELECT k FROM t_enum ORDER BY k')" "no"
psql_run "ALTER TYPE enum_t ADD VALUE 'a' BEFORE 'b';"
check "an enum ADD VALUE ... BEFORE does not renumber the values already stored" \
	"$(sorts 'SELECT k FROM t_enum ORDER BY k')" "no"
ansp  "and the ordered answer still matches heap" t_enum_h t_enum \
	'SELECT k, id FROM %T ORDER BY k, id'

# --- a rewrite that a type change forces retracts the claim -----------------

psql_run "CREATE TABLE atc (id int, k int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('atc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO atc SELECT g, (g*7919)%500 FROM generate_series(1,20000) g;"
psql_run "SELECT pgcolumnar.vacuum_sorted('atc', 'k');"
check "the claim is live before the type change" \
	"$(sorts 'SELECT k FROM atc ORDER BY k')" "no"
ATC_SID="$(storage_id_of atc)"
psql_run "ALTER TABLE atc ALTER COLUMN k TYPE text;"
check "premise: a type change DID rewrite the storage" \
	"$([ "$ATC_SID" = "$(storage_id_of atc)" ] && echo same || echo rewritten)" "rewritten"
check "REFUSE: integer order is not text order, and the rewrite dropped the mark" \
	"$(sorts 'SELECT k FROM atc ORDER BY k')" "yes"

# --- an UPDATE lands outside the run, so the claim lapses -------------------

psql_run "CREATE TABLE upc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('upc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO upc SELECT * FROM h;"
psql_run "SELECT pgcolumnar.vacuum_sorted('upc', 'k', 'j');"
check "the claim is live before the update" \
	"$(sorts 'SELECT k FROM upc ORDER BY k')" "no"
psql_run "UPDATE upc SET k = -1 WHERE id = 1;"
check "premise: the new row version appended past the run" \
	"$([ "$(ss upc appended_groups)" -gt 0 ] && echo yes || echo no)" "yes"
check "REFUSE: one updated row is a row outside the run" \
	"$(sorts 'SELECT k FROM upc ORDER BY k')" "yes"
check "and ORDER BY k LIMIT 1 finds the updated row" \
	"$(q 'SELECT k FROM upc ORDER BY k LIMIT 1;')" "-1"

# --- a column rename: the mark follows it, so the claim survives (#778) ------
#
# This arm used to assert the opposite, and it was right to at the time: nothing
# maintained the mark, so after a rename the recorded name stopped resolving and
# the pathkey code refused to claim an ordering. #778 made the mark follow the
# rename, because a rename does not move data -- the rows really are still
# ordered by whichever column now carries the name -- so the claim is now both
# available and TRUE, and the needless Sort goes away.
#
# The refusal still has to exist for a name that genuinely cannot be resolved,
# which is the second half below.

psql_run "CREATE TABLE rnc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('rnc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO rnc SELECT * FROM h;"
psql_run "SELECT pgcolumnar.vacuum_sorted('rnc', 'k', 'j');"
check "the claim is live before the rename" \
	"$(sorts 'SELECT k FROM rnc ORDER BY k')" "no"
psql_run "ALTER TABLE rnc RENAME COLUMN k TO kk;"
check "the recorded key FOLLOWS the rename (#778)" \
	"$(q "SELECT sorted_by::text FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('rnc');")" \
	"{kk,j}"
check "so the claim survives the rename instead of being refused" \
	"$(sorts 'SELECT kk FROM rnc ORDER BY kk')" "no"
# ...and the claim is not merely available, it is true: no Sort AND the rows
# really do come out ordered. A plan with no Sort over unordered rows is the
# wrong answer, which is the whole risk of claiming a pathkey.
check "and the rows really are ordered by the renamed column (no Sort AND correct)" \
	"$(q "SELECT count(*) FROM (SELECT kk < lag(kk) OVER () AS d FROM rnc) z WHERE d;")" "0"

# a name that truly cannot be resolved must still refuse
psql_run "CREATE TABLE rnd (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('rnd', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO rnd SELECT * FROM h;"
psql_run "SELECT pgcolumnar.vacuum_sorted('rnd', 'k', 'j');"
# Drop the FIRST key column, not the second. Dropping the second leaves {k} as a
# resolvable prefix, and a prefix of a sort key is a sound claim -- ORDER BY k
# legitimately needs no Sort, so that shape cannot test a refusal at all. With
# the first column gone nothing about the remaining order can be claimed: j is
# ordered only WITHIN equal k, so claiming j alone would be a wrong answer.
psql_run "ALTER TABLE rnd DROP COLUMN k;"
check "the recorded key still names the dropped column" \
	"$(q "SELECT sorted_by::text FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('rnd');")" \
	"{k,j}"
check "premise: and j alone really is NOT ordered, so a claim on it would be wrong" \
	"$([ "$(q "SELECT count(*) FROM (SELECT j < lag(j) OVER () AS d FROM rnd) z WHERE d;")" -gt 0 ] && echo unordered || echo ordered)" \
	"unordered"
check "REFUSE: a recorded name that no longer resolves is not a claim" \
	"$(sorts 'SELECT j FROM rnd ORDER BY j')" "yes"

# --- the GUC is the escape hatch and it works both ways ---------------------

# sorts() runs one statement, so the SET has to travel with the connection.
sorts_off() {
	# grep -c on a captured value, not a pipe into grep -q; see lib.sh's
	# pgc_is_columnar_scan for the mechanism and the measurement.
	local _plan
	_plan="$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="-c pgcolumnar.enable_sorted_pathkeys=off" \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "EXPLAIN (COSTS OFF) $1" 2>/dev/null)"
	[ "$(grep -cE '^ *(->)? *(Incremental )?Sort' <<<"$_plan" || true)" -ne 0 ] \
		&& echo yes || echo no
}
check "the claim is live with the GUC on" "$(sorts 'SELECT k FROM c ORDER BY k')" "no"
check "control: pgcolumnar.enable_sorted_pathkeys = off restores the Sort" \
	"$(sorts_off 'SELECT k FROM c ORDER BY k')" "yes"

# --- a cached plan must not outlive the ordering it was planned on ----------
#
# The pathkeys come from the DATA, not from a catalog object the plan cache
# tracks. A plan prepared while the relation was fully ordered would keep
# claiming that order after an INSERT appended a tail, and answer ORDER BY ...
# LIMIT from the run alone. Nothing in the plan cache invalidates on an append.

psql_run "CREATE TABLE pc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('pc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO pc SELECT * FROM h;"
psql_run "SELECT pgcolumnar.vacuum_sorted('pc', 'k', 'j');"
# One session: prepare and execute enough times that a generic plan is chosen
# and cached, then append a tail below the run, then execute again.
PLAN_SQL="$PGC_SQLDIR/cachedplan.sql"
cat > "$PLAN_SQL" <<'SQL'
PREPARE p AS SELECT k FROM pc ORDER BY k NULLS LAST LIMIT 5;
EXECUTE p \g /dev/null
EXECUTE p \g /dev/null
EXECUTE p \g /dev/null
EXECUTE p \g /dev/null
EXECUTE p \g /dev/null
EXECUTE p \g /dev/null
INSERT INTO pc SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g;
EXECUTE p;
SQL
CACHED="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -f "$PLAN_SQL" 2>&1 | tail -5 | tr '\n' ',' | sed 's/,$//')"
check_text "a cached ordered plan sees rows appended after it was planned" \
	"$CACHED" "-600,-599,-598,-597,-596"

# --- a query that cannot use an ordering must not pay to find one out -------
#
# Deciding whether to claim reads the whole row-group list, which grows with the
# relation. A query with no ORDER BY and no mergejoinable clause would have had
# any claim truncated to NIL at the end anyway, so the read is pure waste, and
# it is per plan.
#
# The oracle is a COUNT of the rows planning reads from pgcolumnar.row_group, not
# of buffers and not a duration (#1203). The decision reads the row-group list
# through row_group_pkey. This arm used to count every shared buffer planning
# touched, and most of those belong to PostgreSQL's own catalogs, whose cache
# state differs from one backend to the next: it failed in CI on both arms, in
# both directions, and on one commit red then green. It was never reproduced on a
# quiet box, so the cause of that noise is not known. The noise was in buffers
# this arm does not care about.
#
# pg_stat_get_xact_tuples_returned reads THIS transaction's own counts, so no
# other backend and no autovacuum can add to them, and a plain EXPLAIN plans
# without executing. Heap and index are summed so the count survives a change of
# access path. Measured on PG17, twelve readings each, with and without three
# sessions running DDL alongside: 1000 for both settings with no ORDER BY, 2000
# against 1000 with one, every reading identical.
grouprows() {	# grouprows GUC QUERY -> row_group rows read while planning QUERY
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "SET pgcolumnar.enable_sorted_pathkeys=$1" -c "BEGIN" \
		-c "EXPLAIN (COSTS OFF) $2" \
		-c "SELECT pg_stat_get_xact_tuples_returned('pgcolumnar.row_group'::regclass)
		     + pg_stat_get_xact_tuples_returned('pgcolumnar.row_group_pkey'::regclass)" \
		-c "ROLLBACK" 2>/dev/null | grep -E '^[0-9]+$' | tail -1
}
psql_run "CREATE TABLE pb (id int, k int, j int) USING pgcolumnar;"
# The row_group catalog holds one row per STRIPE, so stripe_row_limit is what
# sets how many rows the plan-time read walks, not chunk_group_row_limit. It has
# a floor of 1000, so the group count comes from the ROW count: a million rows
# for a thousand groups. A hundred-group fixture put the unguarded delta at +6,
# too close to the two-buffer noise floor for the arm to discriminate; at a
# thousand groups it is +44.
psql_run "SELECT pgcolumnar.set_options('pb', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO pb SELECT g, ((g::bigint*7919)%1000000)::int, g%17 FROM generate_series(1,1000000) g;"
psql_run "SELECT pgcolumnar.vacuum_sorted('pb', 'k', 'j');"
psql_run "ANALYZE pb;"
PB_GROUPS="$(ss pb total_groups)"
check "premise: the fixture has many groups, so a per-group read would show" \
	"$([ "$PB_GROUPS" -ge 900 ] && echo yes || echo "no ($PB_GROUPS)")" "yes"
check_text "and it is marked, so the claim is not refused at condition 1" \
	"$(q "SELECT sorted_kind FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('pb');")" \
	"lexicographic"
PB_NOORDER_ON="$(grouprows on  'SELECT count(*) FROM pb WHERE j = 3')"
PB_NOORDER_OFF="$(grouprows off 'SELECT count(*) FROM pb WHERE j = 3')"
check "premise: planning reads the group list at all, so a zero below is not an instrument that cannot see it" \
	"$([ "${PB_NOORDER_OFF:-0}" -ge "$PB_GROUPS" ] && echo yes || echo "no (${PB_NOORDER_OFF:-empty} over $PB_GROUPS groups)")" "yes"
# Equality, not a tolerance: the count is this backend's own rows from one
# catalog, so nothing but the planner's own reads can move it. With the guard
# removed it read 2000 against 1000 here, one whole extra pass over the list.
echo "-- group-list rows read while planning, no ORDER BY: on=$PB_NOORDER_ON off=$PB_NOORDER_OFF over $PB_GROUPS groups"
check "a query with no ORDER BY does not read the group list to decide" \
	"$([ "$PB_NOORDER_ON" = "$PB_NOORDER_OFF" ] && echo yes || echo "no (on=$PB_NOORDER_ON off=$PB_NOORDER_OFF over $PB_GROUPS groups)")" "yes"
# The control that stops the arm above from being satisfied by a function that
# never reads anything: the query that CAN use the ordering must still read the
# list, which costs at least one row per group.
PB_ORDER_ON="$(grouprows on  'SELECT k FROM pb ORDER BY k LIMIT 10')"
PB_ORDER_OFF="$(grouprows off 'SELECT k FROM pb ORDER BY k LIMIT 10')"
echo "-- group-list rows read while planning, ORDER BY: on=$PB_ORDER_ON off=$PB_ORDER_OFF (needs at least $PB_GROUPS more)"
check "control: a query that CAN use the ordering does read to decide" \
	"$([ $(( ${PB_ORDER_ON:-0} - ${PB_ORDER_OFF:-0} )) -ge "$PB_GROUPS" ] && echo yes || echo "no ($PB_ORDER_ON vs $PB_ORDER_OFF over $PB_GROUPS groups)")" "yes"

# --- the two OTHER paths this hook could reach, which must claim nothing -----
#
# Three columnar paths are offered for a base relation and only the serial one
# may carry this claim. A projection is a different physical layout with its own
# sort key (pgcolumnar.projection.sort_key), unrelated to storage.sorted_by, so
# base-table pathkeys reaching a projection path would be a wrong answer with no
# Sort in the plan. And pathkeys on a PARTIAL path describe one worker's output,
# which a plain Gather interleaves.

psql_run "CREATE TABLE prc (LIKE c) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('prc', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO prc SELECT * FROM h;"
psql_run "SELECT pgcolumnar.vacuum_sorted('prc', 'k', 'j');"
psql_run "SELECT pgcolumnar.add_projection('prc', 'p_on_j', ARRAY['k','j'], ARRAY['j']);"
psql_run "CREATE TABLE prc_h (LIKE h) USING heap;"
psql_run "INSERT INTO prc_h SELECT * FROM h;"
# sort_key is stored as attnums; j is attnum 3, so a projection sorted on {3} is
# sorted on a column that is NOT the base relation's lead sort column.
check "premise: the projection exists and is sorted on a DIFFERENT key" \
	"$(q "SELECT sort_key::text FROM pgcolumnar.projection WHERE projection_id > 0 AND storage_id = pgcolumnar.get_storage_id('prc');")" \
	"{3}"
check "the base relation still records its own lexicographic run on {k,j}" \
	"$(q "SELECT sorted_by::text FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('prc');")" \
	"{k,j}"
ansp  "a query the projection can serve still answers in the requested order" prc_h prc \
	'SELECT k, j FROM %T WHERE j = 3 ORDER BY k, j'
ansp  "and with a LIMIT, which is where a borrowed claim would show" prc_h prc \
	'SELECT k, j FROM %T WHERE j = 3 ORDER BY k, j LIMIT 10'

# --- parallel: the claim must not survive into a plan that interleaves --------

check "premise: the fixture is columnar and ordered" "$(inv c k)" "0"
par() {	# run with parallelism on, since pgc_setup pins the gather workers to 0
	env PATH="$PGC_BINDIR:$PATH" \
		PGOPTIONS="-c max_parallel_workers_per_gather=4 -c parallel_setup_cost=0 -c parallel_tuple_cost=0 -c min_parallel_table_scan_size=0" \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -c "$1" 2>&1
}
PARPLAN="$(par "EXPLAIN (COSTS OFF) SELECT k, j FROM c ORDER BY k, j LIMIT 20")"
# AN UNORDERED SCAN OF THE SAME TABLE MUST PLAN A GATHER (#1339). This used to check
# only that EXPLAIN printed something, which any plan does; with no parallel path
# for the table at all, the arm below then read "ok" because no Gather existed. A
# check a repository change can redden is an arm, not a premise (#1236).
check "parallelism is actually available for this table in that session" \
	"$(par "EXPLAIN (COSTS OFF) SELECT k, j FROM c WHERE j >= 0" | grep -cE '^ *(->)? *Gather')" "1"
# grep -c, never grep -q: a reader that exits early takes the writer down with
# EPIPE under pipefail and reports the pattern ABSENT whatever the string held
# (selftest 080). -c consumes all of its input, so the count is the answer.
PAR_BARE_GATHER=$(printf '%s\n' "$PARPLAN" | grep -cE '^ *(->)? *Gather$' || true)
PAR_ORDER_KEEPER=$(printf '%s\n' "$PARPLAN" | grep -cE 'Gather Merge|(Incremental )?Sort' || true)
check "a plain Gather never sits above a scan claiming an order" \
	"$([ "$PAR_BARE_GATHER" -gt 0 ] && [ "$PAR_ORDER_KEEPER" -eq 0 ] && echo bad || echo ok)" "ok"
check_text "and the parallel answer matches the serial one" \
	"$(par "SELECT string_agg(k || ':' || j, ',') FROM (SELECT k, j FROM c ORDER BY k, j LIMIT 20) s")" \
	"$(q "SELECT string_agg(k || ':' || j, ',') FROM (SELECT k, j FROM c ORDER BY k, j LIMIT 20) s;")"

# --- every write path that can append a group must retract a cached plan -----
#
# The invalidation lives in one flush function. That is the single caller of
# PgColumnarInsertRowGroupRow today, but a writer that grew its own path would
# bypass it silently, so each way of appending gets its own arm rather than
# trusting the funnel. The two opt-in GUCs are included because a suite at
# defaults never exercises them and a gap there would be invisible.

cached_first5() {	# cached_first5 TABLE EXTRA_GUCS APPEND_SQL
	local tbl="$1" gucs="$2" appendsql="$3"
	local f="$PGC_SQLDIR/cp.$tbl.sql"
	# The LAST statement must be EXECUTE p, not an equivalent ad-hoc SELECT.
	# It was an ad-hoc SELECT first, and every arm below stayed green with the
	# invalidation disabled: a fresh query is planned from scratch, so it can
	# never observe a stale plan. The whole point is to re-run the CACHED one.
	{
		echo "PREPARE p AS SELECT k FROM $tbl ORDER BY k NULLS LAST LIMIT 5;"
		for _ in 1 2 3 4 5 6; do echo "EXECUTE p \\g /dev/null"; done
		echo "$appendsql"
		echo "EXECUTE p;"
	} > "$f"
	env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="$gucs" \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -f "$f" 2>&1 \
		| tail -5 | paste -sd,
}
mk_sorted() {	# mk_sorted TABLE
	psql_run "CREATE TABLE $1 (id int, k int, j int, t text) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
	psql_run "INSERT INTO $1 SELECT * FROM h;"
	psql_run "SELECT pgcolumnar.vacuum_sorted('$1', 'k', 'j');"
}
# The appended rows carry k = -600..-1, all below the run, so a plan still
# claiming the old order answers 0,0,0,0,0 instead of -600,-599,-598,-597,-596.
WANT="-600,-599,-598,-597,-596"

mk_sorted w_ins
check_text "cached plan retracted by a plain INSERT" \
	"$(cached_first5 w_ins "" "INSERT INTO w_ins SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g;")" "$WANT"

mk_sorted w_isel
psql_run "CREATE TABLE feed AS SELECT g AS id, -g AS k, g%13 AS j, 'x'||g AS t FROM generate_series(1,600) g;"
check_text "cached plan retracted by INSERT ... SELECT from another table" \
	"$(cached_first5 w_isel "" "INSERT INTO w_isel SELECT * FROM feed;")" "$WANT"

mk_sorted w_copy
COPYF="$PGC_WORKDIR/feed.csv"
psql_run "COPY (SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g) TO '$COPYF' WITH (FORMAT csv);"
check_text "cached plan retracted by COPY" \
	"$(cached_first5 w_copy "" "COPY w_copy FROM '$COPYF' WITH (FORMAT csv);")" "$WANT"

# pgcolumnar.bulk_parallel_writer is deliberately NOT an arm here. It does not
# change the write path: its only consulted site is
# PgColumnarInsertNativeStorageRow, where it skips an advisory lock when the
# storage row already exists, and pgcolumnar.parallel_copy sets it in its loader
# sessions. An arm for it would be this plain-INSERT arm wearing a GUC, and
# would read as coverage it does not buy. The path it looks like it covers is
# parallel_copy, which has its own arm below.

# parallel_flush has FOUR conjuncts in its gate (columnar_write_state.c), two of
# which fail silently in ordinary fixture shapes: a table created in the same
# transaction as the insert takes the serial path, and so does anything narrower
# than two columns. So the dispatch line it emits at DEBUG1 is asserted as the
# premise; without it this arm is a plain INSERT wearing a GUC.
# The dispatch premise runs on its OWN table. Taken on w_pflush it appended a
# tail before the plan was ever prepared, so the relation had no ordered path to
# retract and the arm passed with the invalidation disabled -- vacuous for the
# second time, in the same suite, for a different reason.
mk_sorted w_pflush_probe
PFLUSH_DISPATCH="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -c "SET client_min_messages=debug1;" -c "SET pgcolumnar.parallel_flush=on;" \
	-c "INSERT INTO w_pflush_probe SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g;" 2>&1 |
	grep -oE 'parallel_flush dispatch: .*-> (parallel|serial)' | tail -1 | sed 's/^.*-> //')"
check_text "premise: parallel_flush dispatches parallel on exactly this shape" \
	"$PFLUSH_DISPATCH" "parallel"
mk_sorted w_pflush
check_text "cached plan retracted with pgcolumnar.parallel_flush on" \
	"$(cached_first5 w_pflush "-c pgcolumnar.parallel_flush=on" \
		"INSERT INTO w_pflush SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g;")" "$WANT"

# pgcolumnar.parallel_copy is the one write path where separate BACKENDS flush
# groups in their own transactions, so it is the only one where the invalidation
# has to cross a process boundary. It needs N+2 worker slots: eight loaders
# against eight stock slots load ZERO rows, which is a vacuous arm that reads as
# a pass, so the row count is asserted before anything else.
mk_sorted w_pcopy
PCOPYF="$PGC_WORKDIR/pcopy.txt"
psql_run "COPY (SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g) TO '$PCOPYF';"
PCOPY_ROWS="$(q "SELECT pgcolumnar.parallel_copy('w_pcopy', '$PCOPYF', 2);")"
check_num "premise: parallel_copy actually loaded its rows (worker slots sufficed)" \
	"$PCOPY_ROWS" "600"
check "premise: and they appended past the run" \
	"$([ "$(ss w_pcopy appended_groups)" -gt 0 ] && echo yes || echo no)" "yes"
# The load already happened, so this arm prepares AFTER it and appends again
# through parallel_copy, which is the cross-backend case.
mk_sorted w_pcopy2
check_text "cached plan retracted by pgcolumnar.parallel_copy" \
	"$(cached_first5 w_pcopy2 "" \
		"SELECT pgcolumnar.parallel_copy('w_pcopy2', '$PCOPYF', 2);")" "$WANT"

# --- the invariant the whole invalidation gate rests on ---------------------
#
# The gate is "the storage has a mark AND this group falls outside it". It goes
# quiet if a new group can land INSIDE a stale mark. Group numbers are monotonic
# within a storage, and TRUNCATE does restart them at 1 -- but it also makes a
# NEW storage, whose mark is NULL. THE MARK AND THE GROUP NUMBERING LIVE IN THE
# SAME STORAGE ROW, so numbering can only reset together with a mark that resets
# to NULL. This arm exists because that invariant is invisible: anything that
# reused a storage id, or reset numbering within one, would silence the gate and
# bring stale ordered plans back with no other test noticing.

mk_sorted trunc
TRUNC_SID="$(storage_id_of trunc)"
check "the mark is set and numbering starts at 1 before the truncate" \
	"$(q "SELECT (sorted_from = min(group_number))::text FROM pgcolumnar.row_group, pgcolumnar.storage WHERE pgcolumnar.storage.storage_id = pgcolumnar.get_storage_id('trunc') AND pgcolumnar.row_group.storage_id = pgcolumnar.storage.storage_id GROUP BY sorted_from;")" \
	"true"
psql_run "TRUNCATE trunc;"
psql_run "INSERT INTO trunc SELECT * FROM h;"
check "TRUNCATE restarts group numbering" \
	"$(q "SELECT (min(group_number) = 1)::text FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('trunc');")" \
	"true"
check "but in a NEW storage, so the mark it could collide with is gone" \
	"$([ "$TRUNC_SID" != "$(storage_id_of trunc)" ] && echo new || echo reused)" "new"
check_text "and that new storage claims no ordering" \
	"$(q "SELECT coalesce(sorted_kind,'<NULL>') FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('trunc');")" \
	"<NULL>"
check "REFUSE: so a restarted group number cannot land inside a live mark" \
	"$(sorts 'SELECT k FROM trunc ORDER BY k')" "yes"

# CTAS creates a new relation, so there is no earlier plan to retract; the arm
# is that the fresh relation claims nothing, having never been ordered.
psql_run "CREATE TABLE w_ctas USING pgcolumnar AS SELECT * FROM h;"
check "a CTAS relation was never ordered, so it claims nothing" \
	"$(sorts 'SELECT k FROM w_ctas ORDER BY k')" "yes"

# --- a reclaiming rewrite retracts the claim even though the rows stay ordered
#
# Group numbers are monotonic: a reclaiming rewrite writes ABOVE the mark rather
# than reusing numbers inside it, so the run no longer covers every group and
# the claim lapses while the data is still in order. That is conservative and
# deliberate, and this arm exists so a later optimisation cannot quietly remove
# the conservatism without a red.

mk_sorted rec
psql_run "DELETE FROM rec WHERE id % 2 = 0;"
psql_run "SELECT pgcolumnar.compact_rewrite('rec');"
check "premise: the reclaiming rewrite moved every group above the mark" \
	"$([ "$(ss rec sorted_groups)" -eq 0 ] && echo yes || echo no)" "yes"
check "premise: and the rows are still physically in k order" "$(inv rec k)" "0"
check "REFUSE: a run that no longer covers every group is not a claim" \
	"$(sorts 'SELECT k FROM rec ORDER BY k')" "yes"

pgc_summary

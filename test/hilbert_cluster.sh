#!/usr/bin/env bash
#
# pgColumnar Hilbert clustering: the SQL surface, the recorded kind, the
# self-gate and the daemon (issue #889, SQL half).
#
# WHAT THIS SUITE IS FOR
#
# test/hilbert_curve.sh pins the CURVE, in C, by its mathematical properties and
# by frozen bytes. Nothing in it goes through SQL, so nothing in it can tell
# whether the curve is ever REACHED from a user statement, whether the table
# remembers that it was laid on that curve, or whether the maintenance daemon
# preserves the choice. This file is that half and only that half; it does not
# re-test the encoder.
#
# THE SURFACE, AND WHY IT IS TWO NEW VERBS RATHER THAN A PARAMETER
#
#	pgcolumnar.cluster_hilbert(regclass, VARIADIC name[])
#	pgcolumnar.recluster_hilbert(regclass, VARIADIC name[])
#
# PostgreSQL refuses to extend the existing signature in either direction: a
# defaulted parameter cannot precede a VARIADIC one, and VARIADIC must be last.
# An array-plus-kind overload breaks the documented cluster('t','a','b') call
# style with "function ... is not unique". Both measured on 18.4. The surface is
# settled; these arms pin it rather than reopen it.
#
# THE OWNER RULING (2026-09-09, recorded on #889): THE CURVE IS STICKY
#
# sorted_kind is the table's DECLARED INTENT, not a property of each call:
#
#	- the daemon dispatches on the recorded kind, so a Hilbert table stays
#	  Hilbert;
#	- plain recluster() on a Hilbert table WITH A MATCHING KEY is a no-op, not
#	  a silent conversion to Z-order;
#	- switching curves requires naming the other verb explicitly;
#	- vacuum_sorted must not clobber a Hilbert table. Its gate
#	  (vacuum_sorted_gate_is_noop, src/columnar_vacuum.c) refuses anything that
#	  is not exactly 'lexicographic', so TODAY it would rewrite the table and
#	  relabel it.
#
# S6 IS THE ONE ARM WHOSE INTENDED VALUE IS A READING RATHER THAN A QUOTE. The
# ruling says vacuum_sorted "must not clobber"; it does not say in so many words
# whether the correct behaviour is a no-op or an honest relabel. This suite pins
# the no-op, because the parenthetical names "rewrite AND relabel" as the defect
# and a relabel alone is honest labelling. If the owner meant the other thing,
# S6 is the arm to change and it is deliberately isolated so that it can be.
#
# WHY EACH ARM IS SHAPED THE WAY IT IS
#
#	- SQLSTATE, never message text. A grep for "permission denied" is also
#	  satisfied by a login FATAL, a missing function (42883), a bad argument
#	  (22023) or a transaction-block refusal (25001). Only aclcheck_error
#	  produces 42501, and a role that cannot open a session carries no SQLSTATE
#	  at all -- so the session premise is asserted first.
#	- AND "no error" MUST MEAN THE SERVER ANSWERED. sqlstate() reports a state
#	  only when it finds one, so a probe that never reached the server used to
#	  read as success and satisfied its own removal proof. Every probe now
#	  carries a sentinel statement behind the one under test: no sentinel in the
#	  output is PROBE_UNREACHABLE, not noerror, and a NOLOGIN role proves the
#	  probe can still say so.
#	- A CALL MADE WITH psql_run IS NOT AN ASSERTION. This suite runs under
#	  `set -uo pipefail` with no -e, so a verb that RAISES leaves no trace: the
#	  fixture is simply not rewritten, and "the verb left the table alone" is
#	  byte-identical to "the verb threw". A vacuum_sorted that raises on every
#	  Hilbert table scored as the pinned no-op and all of S6 went green
#	  (measured, 2026-09-09). So every maintenance verb here is called through
#	  hrun(), which asserts the SQLSTATE, or through q(), whose empty result on
#	  an error is rejected by check_num.
#	- pgc_set_hash IS ORDER-BLIND BY DESIGN. A parity-only "it only reorders"
#	  arm therefore passes on a table nothing touched, so S2 carries the
#	  order-SENSITIVE half beside it and neither half stands alone.
#	- AND AN "IT MOVED" ARM MUST NOT BE SATISFIED BY A MEASUREMENT NOBODY TOOK.
#	  pgc_seq_hash returns a unique QUERY_ERROR.$seq when its query cannot run
#	  and EMPTY when the relation has no rows; physlayout returns NO_LAYOUT when
#	  the relation has no stripes. Those sentinels stop two FAILED measurements
#	  comparing EQUAL (#418) -- and they are unequal to every baseline, so they
#	  satisfy an inequality arm outright. Thirteen arms were of that shape; all
#	  of them now go through changed()/differs(), which report UNMEASURED rather
#	  than "moved" when either side is a sentinel.
#	- physlayout() IS BLIND TO AN EAGER REWRITE, AND THAT IS MEASURED HERE
#	  RATHER THAN ASSUMED. The eager verbs (cluster, cluster_hilbert,
#	  vacuum_sorted) swap the relfilenode and rewrite into a fresh file, so the
#	  (stripeid, fileoffset, rowcount) multiset comes back byte-identical: at
#	  20 stripes and at 1, vacuum_sorted and cluster both left the digest
#	  unchanged while the row order moved (measured on PG17.10, 2026-09-08).
#	  So physlayout is the right corroboration for the ONLINE recluster gate,
#	  where retired groups and new file offsets do move it -- and it is the
#	  WRONG instrument for S2 and S6, which use the order digest and carry a
#	  control that pins this blindness on a verb that exists today.
#	- A RETURN OF 0 IS NOT EVIDENCE THE GATE FIRED, AND NEITHER IS AN UNCHANGED
#	  LAYOUT BESIDE IT. Both are the same observation -- nothing happened -- and
#	  "the function does not exist" is the strongest instance of it: with the
#	  verb absent, "the physical layout is byte-identical, so the gate fired"
#	  printed PASS, and so did all three arms of (a) against a stub defined as
#	  `BEGIN RETURN 0; END` (both measured, 2026-09-09). A gate is proved by a
#	  POSITIVE CONTROL ON THE SAME TABLE: S4(a) appends a tail to the very table
#	  that was just gated, requires the SAME call to do work, and then requires
#	  the gate to close again. "This verb can rewrite this table and chose not
#	  to" is what "the gate fired" means; nothing weaker distinguishes a gate
#	  from a dead verb.
#	- S7 DRIVES THE DAEMON. Calling recluster() by hand and reasoning about
#	  what the daemon would do answers the neighbouring question. The daemon
#	  hard-codes its recluster call at src/columnar_autovacuum.c:283-291, and
#	  that line is the subject of the ruling, so the daemon has to run.
#	- AND THE DAEMON'S REFERENCE MUST BE FROZEN WHILE IT RUNS. "av_hi came to
#	  match the hand-driven hilbert twin" is also satisfied by the daemon
#	  reclustering the TWIN, which is what happened when a stubbed
#	  recluster_hilbert left the twin's tail unfolded and therefore due
#	  (measured). The twin's digest and appended count are captured before the
#	  daemon is enabled and asserted unchanged after, and the daemon's own LOG
#	  LINE names the dispatch, so the actor is read rather than inferred.
#
# WHAT DEFENDS THE CURVE, AND WHAT DOES NOT
#
# There is no SQL exposure of cluster_hilbert_transpose, so no arm here can
# compare a verb's output against the pinned encoder; that would need a
# test-only SQL helper in the install script, which is #889's own change to
# make. What this suite CAN do is refuse to accept a relabelled Z-order
# implementation, and four arms carry that and no others:
#
#	S3  "three different physical orders (hilbert vs zorder)" -- the EAGER verb
#	S4  "(d) ... is NOT the ZORDER rewrite over (b,a)"        -- the ONLINE verb
#	S7  "premise: the two references really are two different layouts"
#	S7  "and it is NOT the zorder layout"                     -- the DAEMON
#
# Measured on PG17.10, 2026-09-09, against a shim whose two verbs delegate to
# the Z-order verbs and then write sorted_kind='hilbert': exactly those four
# reddened for the curve, and 162 of 181 arms passed. S5 in particular is fully
# green on such a shim BY CONSTRUCTION -- over one column both curves are the
# identity -- so S5 buys the surface and the recorded identity, never the curve,
# and must not be counted as evidence of Hilbertness.
#
# THE THREE PARAGRAPHS THAT STOOD HERE WERE CARRIED OUT AND LEFT BEHIND (#1043).
# They said this suite was red on purpose, that neither verb existed, and that it
# was deliberately absent from the matrix. All three were true when written. The
# commit they named, `4b66555`, did every one of the things they instructed, and
# the paragraphs stayed:
#
#     cluster_hilbert, recluster_hilbert   defined in the install script
#     registered                           test/run_all_versions.sh
#     green                                in the matrix, on all five majors
#
# So a reader was told the suite is red on purpose, and it is green; that the
# verbs do not exist, and they ship; and that registering it is future work, and
# it is registered. A stale instruction is worse than a stale fact, because it
# tells the next person to undo what was done.
#
# WHAT SURVIVES IS THE ONE SENTENCE THAT IS STILL TRUE, and it is the trap the
# original paragraphs existed to name: a shim that renames the Z-order verbs and
# writes sorted_kind='hilbert' reddens only four arms and passes 162 of 181. S5
# is green on such a shim BY CONSTRUCTION, because over one column both curves
# are the identity. S5 therefore buys the surface and the recorded identity, and
# never the curve. Do not count it as evidence of Hilbertness.
#
# Usage:  test/hilbert_cluster.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail

# The daemon is left OFF here and enabled only inside S7, by ALTER SYSTEM. A
# daemon running for the whole suite would be free to recluster the gate
# fixtures between the "before" and "after" reads of S4, which reads as a gate
# that did not fire.
#
# All four pgcolumnar.autovacuum GUCs are PGC_SIGHUP (src/columnar_tableam.c:
# 3262, 3272, 3281, 3290), so none of them NEEDS to be here. They are set in the
# config file because this suite wants them stable for the whole run and
# identical for the launcher, its workers and every psql session -- one place to
# read them from, and no window in which a fixture is built under one threshold
# and measured under another.
#
# max_parallel_workers_per_gather=0 is not a tuning choice. Every scanorder()
# digest below is a claim about PHYSICAL layout, and a parallel plan interleaves
# workers' output independently of the layout, so a parallel scan would make the
# digest a claim about scheduling.
export PGC_EXTRA_CONF="pgcolumnar.autovacuum_naptime=2
pgcolumnar.autovacuum_compact_threshold=0.2
pgcolumnar.autovacuum_recluster_threshold=0.05
max_parallel_workers_per_gather=0"

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# The row GROUP is the stripe, so stripe_row_limit is what decides how many
# groups a fixture has -- chunk_group_row_limit sizes the vector inside one.
# 20,000 rows at 1,000 to a stripe is 20 groups, which is what makes "the layout
# did not move" a statement about something rather than about a single group
# that could not move. Both values are at set_options' floor (stripe >= 1000,
# chunk_group >= 100); a value below it RAISES, and a suite that discards the
# error then measures a fixture built on the defaults. So the group count is
# ASSERTED by every fixture generator below rather than assumed from the call.
SR=1000
CG=500

# ---- the instruments -------------------------------------------------------

# WHAT COUNTS AS A MEASUREMENT. The digest helpers deliberately return sentinels
# rather than the empty string, so two failed reads cannot compare equal and pass
# an equality arm (#418). The same sentinels satisfy an INEQUALITY arm outright:
# QUERY_ERROR.7 is not equal to any baseline, so "the layout moved" passes on a
# query that never ran. EMPTY is the same hazard from the other side -- a verb
# that destroys every row produces it, and "moved" is then true and useless.
# Nothing in this suite ever measures a legitimately empty relation.
pgc_measured() {	# pgc_measured VALUE -> 0 when VALUE is a real measurement
	case "$1" in
		'' | QUERY_ERROR.* | EMPTY | NO_LAYOUT) return 1 ;;
	esac
	return 0
}

# changed BEFORE AFTER -> moved | unchanged | UNMEASURED[...]
# differs A B           -> different | IDENTICAL | UNMEASURED[...]
# Every inequality arm in this file goes through one of these two.
changed() {
	pgc_measured "$1" || { printf 'UNMEASURED[before=%s]\n' "$1"; return; }
	pgc_measured "$2" || { printf 'UNMEASURED[after=%s]\n' "$2"; return; }
	if [ "$1" != "$2" ]; then echo moved; else echo unchanged; fi
}
differs() {
	pgc_measured "$1" || { printf 'UNMEASURED[a=%s]\n' "$1"; return; }
	pgc_measured "$2" || { printf 'UNMEASURED[b=%s]\n' "$2"; return; }
	if [ "$1" != "$2" ]; then echo different; else echo IDENTICAL; fi
}

# A falsifiable PHYSICAL signal for "was this rewritten": the per-group
# (stripeid, fileoffset, rowcount) multiset from stats(). This is
# recluster_gate.sh's physlayout(), with one deliberate difference -- a relation
# with no stripes, or a query that could not run, yields NO_LAYOUT rather than
# the empty string, so it cannot read as a layout that moved.
# relfilenode does NOT move on any recluster path (pgcolumnar rewrites inside its
# own storage), so it could never falsify a rewrite.
physlayout() {
	local d
	d="$(q "SELECT md5(string_agg(stripeid::text||':'||fileoffset::text||':'||rowcount::text, ',' ORDER BY stripeid)) FROM pgcolumnar.stats('$1');")"
	printf '%s\n' "${d:-NO_LAYOUT}"
}

# The recorded kind, from the CATALOG. pgcolumnar.storage carries no GRANT and
# is superuser-only (measured: a plain owner gets "permission denied for table
# storage").
skind()   { q "SELECT coalesce(sorted_kind::text,'<NULL>') FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('$1');"; }
# The same fact through the REPORTER, which is the only route a table's own
# owner has. Reading only the catalog tests the catalog and not the reporter --
# and reading the reporter AS THE SUPERUSER tests neither, because q() is
# hard-wired to -U postgres. So S3 hands three fixtures to a plain role and
# reads them back as that role; qrole() is how.
skindst() { q "SELECT coalesce(sorted_kind::text,'<NULL>') FROM pgcolumnar.sort_status('$1');"; }
skey()    { q "SELECT coalesce(sort_key::text,'<NULL>') FROM pgcolumnar.sort_status('$1');"; }
appended(){ q "SELECT appended_groups FROM pgcolumnar.sort_status('$1');"; }
groups()  { q "SELECT total_groups FROM pgcolumnar.sort_status('$1');"; }

# A scalar read AS A NAMED ROLE. q() is -U postgres and always will be; this is
# the only way an arm may claim to have measured what a table's OWNER can see.
qrole() {	# qrole ROLE SQL
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -At -c "$2" 2>/dev/null || true
}
skindst_as() { qrole "$1" "SELECT coalesce(sorted_kind::text,'<NULL>') FROM pgcolumnar.sort_status('$2');"; }

# The order the scan actually returns rows in. pgc_seq_hash keeps the query's own
# output order (ORDER BY row_number() OVER ()), so this can fail on a reordering;
# pgc_set_hash cannot, by design, which is why both appear in S2.
scanorder() { pgc_seq_hash "SELECT * FROM $1"; }
setof()     { pgc_set_hash "SELECT * FROM $1"; }

# The SQLSTATE a statement raises, as a value, for a named role.
#
# VERBOSITY is set with -v and NOT with -c '\set ...': psql takes a different
# code path for a -c argument beginning with a backslash, which is how a sibling
# suite ended up with deny arms that could never go green. The state is read out
# of the ERROR line rather than from a bare line of its own, because psql
# prefixes it. "noerror" rather than the empty string, so a statement that
# SUCCEEDED can never compare equal to one whose state could not be read.
#
# AND A SENTINEL STATEMENT BEHIND IT. Without one, "no ERROR line was found" is
# also what a probe that never reached the server produces, so `noerror` was
# satisfied by a psql that could not connect -- including the control that
# exists to prove the probe can report success. ON_ERROR_STOP=0 means the
# sentinel still runs after the statement under test raised (measured: the state
# and PGC_PROBE_OK both appear), so its ABSENCE means the session, not the
# statement, is what failed.
sqlstate() {	# sqlstate ROLE SQL -> a five-character SQLSTATE, noerror, or PROBE_UNREACHABLE
	local role="$1" sql="$2" out st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$role" \
		-d "$PGC_DB" -At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=0 \
		-c "$sql" -c "SELECT 'PGC_PROBE_OK';" 2>&1)"
	case "$out" in
		*PGC_PROBE_OK*) ;;
		*) echo PROBE_UNREACHABLE; return ;;
	esac
	st="$(printf '%s\n' "$out" | sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' | head -1)"
	if [ -n "$st" ]; then printf '%s\n' "$st"; else echo noerror; fi
}

# Run a maintenance verb AS AN ASSERTION. psql_run's exit status is checked
# nowhere in this tree and this suite cannot afford that: "the verb left the
# table alone" and "the verb raised" produce identical fixtures.
hrun() {	# hrun WHAT SQL
	check_text "$1 ran without raising" "$(sqlstate postgres "$2")" "noerror"
}

# The premise behind every scanorder() comparison below. Without it those arms
# could pass by construction.
pgc_check_ordered_oracle

# And the digests must be reading the columnar layout, not a parallel plan's
# interleaving of it.
check_text "premise: parallelism is off, so a scan order is a fact about the layout" \
	"$(q 'SHOW max_parallel_workers_per_gather;')" "0"

# =============================================================================
# S1  THE SURFACE AND ITS REFUSALS, BY SQLSTATE
# =============================================================================
#
# Four refusals, for each of the two new verbs, each compared against the state
# the ESTABLISHED verb raises on the identical input and read from the same
# probe. Comparing against the sibling rather than only against a literal is
# what stops the two surfaces drifting apart: a change to cluster()'s refusal
# that is not made to cluster_hilbert()'s reddens here.

psql_run "CREATE TABLE sur (c1 int, c2 int, c3 int, c4 int, c5 int,
                            c6 int, c7 int, c8 int, c9 int, txt text) USING pgcolumnar;"
psql_run "INSERT INTO sur SELECT g,g,g,g,g,g,g,g,g,'t'||g FROM generate_series(1,2000) g;"
check_num "premise: the refusal fixture holds rows, so a refusal is not an empty-table artifact" \
	"$(q 'SELECT count(*) FROM sur;')" "2000"

psql_run "DROP ROLE IF EXISTS h_other;"
psql_run "CREATE ROLE h_other NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO h_other;"
# The DROP above is itself an unchecked psql_run, so a leftover h_other from a
# previous run -- one that owned objects, and so could not be dropped -- would
# be reused with whatever grants it had. lib.sh initdbs a fresh cluster per run,
# which is why this has never bitten; asserted rather than trusted.
check_num "premise: h_other owns nothing, so it is this run's role and not a leftover" \
	"$(q "SELECT count(*) FROM pg_class WHERE relowner = 'h_other'::regrole;")" "0"

# A deny arm is evidence only if the call reached the code that denies it, and a
# login FATAL carries no SQLSTATE at all. The role must be able to open a
# session, or the 42501 arms measure connectivity.
check_text "premise: h_other can open a session, so its refusals are refusals and not login failures" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U h_other -d "$PGC_DB" -At -c 'SELECT 1;' 2>&1 | head -1)" \
	"1"
check_text "premise: h_other does not own the table" \
	"$(q "SELECT pg_get_userbyid(relowner) = 'h_other' FROM pg_class WHERE oid = 'sur'::regclass;")" "f"

# None of the four clustering verbs is REVOKEd from PUBLIC (unlike
# read_projection and friends), so EXECUTE is not the layer that refuses here.
# Asserted rather than assumed: if a future revision adds a REVOKE, the 42501
# below stops attributing to the C owner check and this premise says so.
check_text "premise: h_other holds EXECUTE on all four verbs, so a 42501 below can only be the owner check" \
	"$(q "SELECT count(*) FILTER (WHERE has_function_privilege('h_other', p.oid, 'EXECUTE'))
	          || '/' || count(*)
	      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	      WHERE n.nspname = 'pgcolumnar'
	        AND p.proname IN ('cluster','recluster','cluster_hilbert','recluster_hilbert');")" \
	"4/4"

# The four inputs. Held in variables so the hilbert verb and its sibling are
# given byte-identical arguments; two hand-written call sites are how a "same
# input" comparison stops being one.
ARGS_OK="'sur','c1','c2'"
ARGS_TEXT="'sur','txt'"
ARGS_NINE="'sur','c1','c2','c3','c4','c5','c6','c7','c8','c9'"
# Zero columns is spelled with an explicit empty array. Bare
# pgcolumnar.cluster('sur') does not resolve at all -- measured 42883, not
# 22023 -- so it would test PostgreSQL's function resolution rather than the
# verb's own argument check, and the two new verbs would "agree" with the old
# ones about an error neither of them raised.
ARGS_ZERO="'sur', VARIADIC ARRAY[]::name[]"

# case | role | args | expected SQLSTATE | what it is
#   42501  aclcheck_error(ACLCHECK_NOT_OWNER)
#   0A000  ERRCODE_FEATURE_NOT_SUPPORTED, an unsupported clustering type
#   22023  ERRCODE_INVALID_PARAMETER_VALUE, too many / no clustering columns
for _case in \
	"non-owner|h_other|$ARGS_OK|42501" \
	"a text clustering column|postgres|$ARGS_TEXT|0A000" \
	"nine clustering columns|postgres|$ARGS_NINE|22023" \
	"zero clustering columns|postgres|$ARGS_ZERO|22023"
do
	_what="${_case%%|*}"; _rest="${_case#*|}"
	_role="${_rest%%|*}"; _rest="${_rest#*|}"
	_args="${_rest%%|*}"; _want="${_rest##*|}"

	for _pair in "cluster|cluster_hilbert" "recluster|recluster_hilbert"; do
		_old="${_pair%%|*}"; _new="${_pair##*|}"
		_sold="$(sqlstate "$_role" "SELECT pgcolumnar.$_old($_args);")"
		_snew="$(sqlstate "$_role" "SELECT pgcolumnar.$_new($_args);")"
		check_text "$_old on $_what raises $_want (the probe reads the right state)" \
			"$_sold" "$_want"
		check_text "$_new on $_what raises $_want" \
			"$_snew" "$_want"
		# "THE ONE $_old RAISES", so it has to be a raised SQLSTATE. Two verbs that both
		# ran clean, or both went unreachable, used to agree here under a name that
		# says two refusals were compared (#1339 :301).
		case "$_sold" in
			[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]) _cmp="$_snew" ;;
			*) _cmp="no comparison: $_old raised nothing ($_sold)" ;;
		esac
		check_text "and $_new's state on $_what is the one $_old raises on the identical input" \
			"$_cmp" "$_sold"
	done
done

# The removal proof for the whole block, in both directions. The probe must be
# able to report success, or every arm above is satisfied by an instrument that
# always finds an error -- AND it must be able to report that it never reached
# the server, or "success" is what an unreachable probe says and the first
# control proves nothing. A NOLOGIN role is refused at connection time and
# carries no SQLSTATE, which is exactly the shape the sentinel exists to catch.
check_text "control: the probe reports noerror when the owner makes a call that works" \
	"$(sqlstate postgres "SELECT pgcolumnar.cluster('sur','c1','c2');")" "noerror"
psql_run "DROP ROLE IF EXISTS h_nologin;"
psql_run "CREATE ROLE h_nologin NOSUPERUSER NOLOGIN;"
check_text "control: and it reports PROBE_UNREACHABLE when it cannot open a session, so noerror means the server answered" \
	"$(sqlstate h_nologin "SELECT 1;")" "PROBE_UNREACHABLE"

# =============================================================================
# S2  IT ONLY REORDERS
# =============================================================================
#
# Against a heap mirror. The set hash is order-blind ON PURPOSE, so it is the
# right instrument for "the rows are the same rows" and the WRONG one for "the
# rows moved". Both halves are here because a parity-only arm passes on a table
# that was never touched.

psql_run "CREATE TABLE s2h (id int, x int, y int, pad text);"
psql_run "CREATE TABLE s2c (id int, x int, y int, pad text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('s2c', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO s2h SELECT g, ((g::bigint*7919)%200)::int, ((g::bigint*104729)%200)::int, 'p'||(g%97)
          FROM generate_series(1,40960) g;"
psql_run "INSERT INTO s2c SELECT * FROM s2h;"

S2_HEAP="$(pgc_set_hash 'SELECT id, x, y, pad FROM s2h')"
S2_SET_BEFORE="$(pgc_set_hash 'SELECT id, x, y, pad FROM s2c')"
S2_SEQ_BEFORE="$(scanorder s2c)"
S2_SEQ_AGAIN="$(scanorder s2c)"

check_num "premise: set_options took, so s2c is 20 groups and not one default-sized group" \
	"$(groups s2c)" "20"
check_text "premise: before clustering, the columnar table already matches its heap mirror as a SET" \
	"$S2_SET_BEFORE" "$S2_HEAP"
check_text "premise: the order-sensitive digest is STABLE across two reads, so a later change is a change" \
	"$S2_SEQ_AGAIN" "$S2_SEQ_BEFORE"
check "premise: the plan being digested is the columnar custom scan, not a fallback" \
	"$(pgc_is_columnar_scan 'SELECT * FROM s2c')" "yes"

hrun "cluster_hilbert('s2c','x','y')" "SELECT pgcolumnar.cluster_hilbert('s2c', 'x', 'y');"

check_text "cluster_hilbert preserves the row SET exactly (unchanged from before)" \
	"$(pgc_set_hash 'SELECT id, x, y, pad FROM s2c')" "$S2_SET_BEFORE"
check_text "and the row set still equals the heap mirror's" \
	"$(pgc_set_hash 'SELECT id, x, y, pad FROM s2c')" "$S2_HEAP"
check "cluster_hilbert MOVED the rows: the order-sensitive digest changed" \
	"$(changed "$S2_SEQ_BEFORE" "$(scanorder s2c)")" "moved"
check_num "and no row was lost or gained" "$(q 'SELECT count(*) FROM s2c;')" "40960"

# THE INSTRUMENT'S LIMIT, PINNED ON A VERB THAT ALREADY EXISTS.
#
# physlayout() is recluster_gate.sh's digest and it is the right corroboration
# for the ONLINE gate in S4. It cannot see an EAGER rewrite: the eager path
# writes a fresh file and reproduces the same (stripeid, fileoffset, rowcount)
# multiset, so the digest is byte-identical either side of a rewrite that
# reordered every row. Measured on plain cluster() below rather than asserted
# about cluster_hilbert(), so the control stands on code that ships today and
# cannot be perturbed by whatever #889 lands.
#
# WHAT THIS SECTION NO LONGER ASSERTS, AND WHY. It used to also require s2c's
# OWN layout digest to be unchanged across cluster_hilbert. That is not a
# property of the eager path; file_offset is the compressed size of the
# preceding groups, so reproducing the geometry is a data-dependent accident of
# this fixture. A correct Hilbert implementation that packs groups differently
# would redden it for no defect. The control below is the honest form of the
# same fact, because it is measured on a verb #889 cannot change.
psql_run "CREATE TABLE s2ctl (id int, x int, y int, pad text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('s2ctl', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO s2ctl SELECT * FROM s2h;"
S2CTL_PHYS="$(physlayout s2ctl)"
S2CTL_SEQ="$(scanorder s2ctl)"
hrun "cluster('s2ctl','x','y')" "SELECT pgcolumnar.cluster('s2ctl', 'x', 'y');"
check "control: an eager cluster() DOES reorder the rows" \
	"$(changed "$S2CTL_SEQ" "$(scanorder s2ctl)")" "moved"
check "control: and physlayout cannot see it, which is why the order digest is S2's instrument" \
	"$(changed "$S2CTL_PHYS" "$(physlayout s2ctl)")" "unchanged"

# =============================================================================
# S3  THE KIND IS RECORDED, AND IT IS DISTINGUISHABLE
# =============================================================================
#
# Three tables from ONE fixture generator, so the only difference between them
# is the verb that rewrote them. Read BOTH ways: pgcolumnar.storage is the
# catalog and is superuser-only, pgcolumnar.sort_status is the reporter and is
# the only route a table's own owner has. Reading one tests one -- and reading
# both AS THE SUPERUSER tests one, which is why the three fixtures are handed to
# a plain role and the reporter is read back as that role.

mk3() {
	psql_run "CREATE TABLE $1 (id int, k int, j int) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', stripe_row_limit => $SR, chunk_group_row_limit => $CG);"
	psql_run "INSERT INTO $1 SELECT g, (g*7919)%5000, g%13 FROM generate_series(1,5000) g;"
	check_num "premise: set_options took on $1, so it is 5 groups and not one default-sized group" \
		"$(groups "$1")" "5"
}

psql_run "DROP ROLE IF EXISTS h_owner;"
psql_run "CREATE ROLE h_owner NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO h_owner;"
check_text "premise: h_owner can open a session, so a read as h_owner is a read" \
	"$(qrole h_owner 'SELECT 1;')" "1"
check_text "premise: h_owner is NOT a superuser, or reading 'as the owner' reads as the superuser again" \
	"$(q "SELECT rolsuper::text FROM pg_roles WHERE rolname = 'h_owner';")" "false"
check_text "premise: and the catalog really is closed to it, so sort_status is the only route it has" \
	"$(sqlstate h_owner 'SELECT count(*) FROM pgcolumnar.storage;')" "42501"

mk3 s3lex; S3LEX_SEQ0="$(scanorder s3lex)"; hrun "vacuum_sorted('s3lex','k')"    "SELECT pgcolumnar.vacuum_sorted('s3lex', 'k');"
mk3 s3zo;  S3ZO_SEQ0="$(scanorder s3zo)";   hrun "cluster('s3zo','k','j')"       "SELECT pgcolumnar.cluster('s3zo', 'k', 'j');"
mk3 s3hi;  S3HI_SEQ0="$(scanorder s3hi)";   hrun "cluster_hilbert('s3hi','k','j')" "SELECT pgcolumnar.cluster_hilbert('s3hi', 'k', 'j');"

# THE PREMISE THE PHYSICAL-ORDER ARMS BELOW STAND ON, AND THE GATE ON THEM.
# "s3hi's order differs from s3zo's" is also true of an s3hi nobody rewrote: it
# is still in insert order, which differs from both of the tables that WERE
# rewritten -- both arms printed PASS, green, on a tree with no cluster_hilbert
# in it (measured). Each leg is anchored against its OWN pre-clustering
# baseline, and the comparison is recorded UNRUN unless both of its legs moved.
S3LEX_MOVED="$(changed "$S3LEX_SEQ0" "$(scanorder s3lex)")"
S3ZO_MOVED="$(changed "$S3ZO_SEQ0" "$(scanorder s3zo)")"
S3HI_MOVED="$(changed "$S3HI_SEQ0" "$(scanorder s3hi)")"
check "vacuum_sorted moved s3lex from its own baseline" "$S3LEX_MOVED" "moved"
check "cluster moved s3zo from its own baseline" "$S3ZO_MOVED" "moved"
check "cluster_hilbert moved s3hi from its own baseline" "$S3HI_MOVED" "moved"
check "premise: the plan being digested for s3hi is the columnar custom scan too" \
	"$(pgc_is_columnar_scan 'SELECT * FROM s3hi')" "yes"

psql_run "ALTER TABLE s3hi OWNER TO h_owner;"
psql_run "ALTER TABLE s3lex OWNER TO h_owner;"
psql_run "ALTER TABLE s3zo OWNER TO h_owner;"
check_text "premise: the three fixtures are now owned by h_owner, so the reads below are the owner's" \
	"$(q "SELECT count(*) FROM pg_class WHERE relowner = 'h_owner'::regrole AND relname IN ('s3hi','s3lex','s3zo');")" "3"

check_text "cluster_hilbert records the kind in pgcolumnar.storage" "$(skind s3hi)" "hilbert"
check_text "and pgcolumnar.sort_status reports that same kind to the table's own (non-superuser) owner" \
	"$(skindst_as h_owner s3hi)" "hilbert"
check_text "and it records the key it applied" "$(skey s3hi)" "{k,j}"

# The discrimination this buys. Three tables built from one fixture must be told
# apart by sort_status ALONE, without reading the superuser-only catalog.
#
# ASSERTED AS A NAMED SET, NOT PAIRWISE. Three pairwise `!=` arms are each
# satisfied by a NULL on one side -- all three passed, green, on a tree where
# cluster_hilbert did not exist and s3hi's kind was <NULL> (measured). They can
# say which pair collapsed; they cannot say the three kinds are the three kinds.
# This one can, and it is strictly stronger than all three of them together.
check_text "the owner alone can tell the three apart: the kinds ARE hilbert, lexicographic and zorder, with no NULL standing in for one" \
	"$(printf '%s\n' "$(skindst_as h_owner s3hi)" "$(skindst_as h_owner s3lex)" "$(skindst_as h_owner s3zo)" | sort | tr '\n' '|')" \
	"hilbert|lexicographic|zorder|"

# And the three layouts really are three layouts, not one relabelled three
# times. Without this, "distinguishable" could be true of a catalog column that
# nothing physical stands behind -- which is exactly what a relabelled Z-order
# implementation is. THIS IS ONE OF THE THREE ARMS IN THE FILE THAT REFUSE ONE.
if [ "$S3HI_MOVED" = "moved" ] && [ "$S3ZO_MOVED" = "moved" ]; then
	check "the three kinds stand for three different physical orders (hilbert vs zorder)" \
		"$(differs "$(scanorder s3hi)" "$(scanorder s3zo)")" "different"
else
	check_unrunnable "the three kinds stand for three different physical orders (hilbert vs zorder)" \
		UNMET_PRECONDITION \
		"a leg was never rewritten (s3hi=[$S3HI_MOVED], s3zo=[$S3ZO_MOVED]), so 'a different order' would only be insert order"
fi
if [ "$S3HI_MOVED" = "moved" ] && [ "$S3LEX_MOVED" = "moved" ]; then
	check "the three kinds stand for three different physical orders (hilbert vs lexicographic)" \
		"$(differs "$(scanorder s3hi)" "$(scanorder s3lex)")" "different"
else
	check_unrunnable "the three kinds stand for three different physical orders (hilbert vs lexicographic)" \
		UNMET_PRECONDITION \
		"a leg was never rewritten (s3hi=[$S3HI_MOVED], s3lex=[$S3LEX_MOVED]), so 'a different order' would only be insert order"
fi

# =============================================================================
# S4  THE SELF-GATE, IN EVERY DIRECTION, CORROBORATED BY A POSITIVE CONTROL
# =============================================================================
#
# There are FIVE directions, not four, and the fifth is the one the daemon
# depends on: same kind, same key, WITH AN APPENDED TAIL must NOT gate. A gate
# keyed on kind and key alone satisfies every other arm here -- (a) 0, (b) >0,
# (c) 0, (d) >0 -- and breaks the daemon outright. Measured against a shim whose
# gate ignored the tail: NO ARM IN S4 COULD SEE IT, and the only arm that
# reddened for it was a FIXTURE PREMISE in S7 ("the hand-driven hilbert
# reference folded its tail back in: got [5] want [0]"). A defect in the gate
# must redden a gate arm, not read as a bad fixture.
# So (a) does not stop at "it returned 0": it appends a tail to the very table
# it just gated, requires the SAME call to do work, and then requires the gate
# to close again.
#
# physlayout is carried beside every return value, but only where it can
# corroborate: an unchanged layout beside a 0 return is the same observation
# twice, so on the no-op side it is asserted only AFTER the return value has
# been confirmed to be the number 0, and recorded UNRUN otherwise.

mk4() {
	psql_run "CREATE TABLE $1 (a int, b int, pad text) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', stripe_row_limit => $SR, chunk_group_row_limit => $CG);"
	psql_run "INSERT INTO $1 SELECT (g*2654435761)::bigint % 1000, g, md5(g::text)
	          FROM generate_series(1,20000) g;"
	check_num "premise: set_options took on $1, so it is 20 groups and 'nothing changed' cannot be true because there was nothing there" \
		"$(groups "$1")" "20"
}
decay4() {	# the 25% appended tail every decay fixture gets
	psql_run "INSERT INTO $1 SELECT (g*2654435761)::bigint % 1000, g, md5(g::text)
	          FROM generate_series(1,5000) g;"
}

# ---- (a) recluster_hilbert on an already-hilbert table, same key ------------
mk4 s4a
hrun "cluster_hilbert('s4a','a','b')" "SELECT pgcolumnar.cluster_hilbert('s4a','a','b');"
check_num "the eager rewrite left no appended tail on s4a" "$(appended s4a)" "0"
S4A_PHYS="$(physlayout s4a)"
S4A_RET="$(q "SELECT pgcolumnar.recluster_hilbert('s4a','a','b');")"
check_num "(a) recluster_hilbert on an already-hilbert table with the same key reclusters 0 groups" \
	"$S4A_RET" "0"
if [ "$S4A_RET" = "0" ]; then
	check_text "(a) and the physical layout is byte-identical" "$(physlayout s4a)" "$S4A_PHYS"
else
	check_unrunnable "(a) and the physical layout is byte-identical" UNMET_PRECONDITION \
		"the call did not return 0 (returned [$S4A_RET]), so an unchanged layout would not be about the gate"
fi
check_text "(a) and the kind is untouched" "$(skind s4a)" "hilbert"

# THE POSITIVE CONTROL, ON THE SAME TABLE AND THROUGH THE SAME CALL. Without it,
# "it returned 0 and nothing moved" is equally true of a verb that does nothing
# at all -- a stub defined as `BEGIN RETURN 0; END` passed all three arms above.
# It is also the fifth direction: a Hilbert table with an appended tail is
# exactly what the daemon hands this verb, and a gate that skips it is a gate
# that disables the daemon.
decay4 s4a
S4A_SET="$(setof s4a)"
S4A_PHYS2="$(physlayout s4a)"
check "premise: s4a now carries an appended tail for the gate to let through" \
	"$([ "$(appended s4a)" -gt 0 ] 2>/dev/null && echo decayed || echo clean)" "decayed"
S4A_RET2="$(q "SELECT pgcolumnar.recluster_hilbert('s4a','a','b');")"
check "(a2) THE SAME CALL ON THE SAME TABLE does work once a tail is appended, so the 0 above was a gate and not a dead verb (>0)" \
	"$([ "$S4A_RET2" -gt 0 ] 2>/dev/null && echo yes || echo no)" "yes"
check "(a2) and that rewrite moved the layout" \
	"$(changed "$S4A_PHYS2" "$(physlayout s4a)")" "moved"
check_num "(a2) and it folded the appended tail back in" "$(appended s4a)" "0"
check_text "(a2) and it is still a hilbert table on the same key" "$(skind s4a)/$(skey s4a)" "hilbert/{a,b}"
check_num "(a2) and no row was lost" "$(q 'SELECT count(*) FROM s4a;')" "25000"
check_text "(a2) and the rows are the same rows, only reordered" "$(setof s4a)" "$S4A_SET"

S4A_PHYS3="$(physlayout s4a)"
S4A_RET3="$(q "SELECT pgcolumnar.recluster_hilbert('s4a','a','b');")"
check_num "(a3) and the gate closes again on the refolded table" "$S4A_RET3" "0"
if [ "$S4A_RET3" = "0" ]; then
	check_text "(a3) and the layout is byte-identical again" "$(physlayout s4a)" "$S4A_PHYS3"
else
	check_unrunnable "(a3) and the layout is byte-identical again" UNMET_PRECONDITION \
		"the call did not return 0 (returned [$S4A_RET3]), so an unchanged layout would not be about the gate"
fi

# ---- (b) recluster_hilbert on a 'zorder' table over the same columns --------
mk4 s4b
hrun "cluster('s4b','a','b')" "SELECT pgcolumnar.cluster('s4b','a','b');"
check_text "s4b is a zorder table over exactly (a,b)" "$(skind s4b)/$(skey s4b)" "zorder/{a,b}"
S4B_PHYS="$(physlayout s4b)"
S4B_SET="$(setof s4b)"
check "(b) recluster_hilbert on a zorder table over the same columns does real work (>0)" \
	"$([ "$(q "SELECT pgcolumnar.recluster_hilbert('s4b','a','b');")" -gt 0 ] 2>/dev/null && echo yes || echo no)" "yes"
check "(b) and the physical layout moved" \
	"$(changed "$S4B_PHYS" "$(physlayout s4b)")" "moved"
# THE LABEL IS ASSERTED WITH THE BYTES. On its own this arm was green in a run
# where its own two siblings were red -- a shim that wrote 'hilbert' into the
# catalog and rewrote nothing at all satisfied it (measured).
check_text "(b) and the recorded kind became hilbert IN THE SAME CALL THAT MOVED THE LAYOUT" \
	"$(skind s4b)/$(changed "$S4B_PHYS" "$(physlayout s4b)")" "hilbert/moved"
check_num "(b) and no row was lost" "$(q 'SELECT count(*) FROM s4b;')" "20000"
check_text "(b) and the rows are the same rows, only reordered" "$(setof s4b)" "$S4B_SET"

# ---- (c) plain recluster on a HILBERT table with a matching key -------------
# THE RULING. The curve is sticky, so this is a no-op and NOT a conversion back
# to Z-order. Today's gate compares strcmp(skind, "zorder") == 0
# (src/columnar_vacuum.c:646), so it falls through and rewrites.
mk4 s4c
hrun "cluster_hilbert('s4c','a','b')" "SELECT pgcolumnar.cluster_hilbert('s4c','a','b');"
S4C_PHYS="$(physlayout s4c)"
S4C_RET="$(q "SELECT pgcolumnar.recluster('s4c','a','b');")"
check_num "(c) plain recluster on a hilbert table with a matching key reclusters 0 groups" \
	"$S4C_RET" "0"
if [ "$S4C_RET" = "0" ]; then
	check_text "(c) and the physical layout is byte-identical: it did not convert the table" \
		"$(physlayout s4c)" "$S4C_PHYS"
else
	check_unrunnable "(c) and the physical layout is byte-identical: it did not convert the table" \
		UNMET_PRECONDITION \
		"the call did not return 0 (returned [$S4C_RET]), so an unchanged layout would not be about the gate"
fi
check_text "(c) and the table is still hilbert, not relabelled zorder" "$(skind s4c)" "hilbert"
check_num "(c) and no row was lost" "$(q 'SELECT count(*) FROM s4c;')" "20000"

# ---- (d) a DIFFERENT key rewrites in both directions ------------------------
# The removal proof for (a) and (c): a gate that never looks at anything
# satisfies both. It must still DISCRIMINATE.
#
# The two twins are built and laid on the curve FIRST, and asserted identical,
# so that the last arm in this block is a comparison of two curves over one key
# on one dataset rather than a comparison of two histories. THAT ARM IS THE
# ONLINE VERB'S ONLY CURVE DEFENCE IN THIS FILE.
mk4 s4d1
hrun "cluster_hilbert('s4d1','a','b')" "SELECT pgcolumnar.cluster_hilbert('s4d1','a','b');"
mk4 s4d2
hrun "cluster_hilbert('s4d2','a','b')" "SELECT pgcolumnar.cluster_hilbert('s4d2','a','b');"
S4D1_PHYS="$(physlayout s4d1)"
S4D2_PHYS="$(physlayout s4d2)"
check_text "premise: the two (d) twins are byte-identical before either is reclustered on the new key" \
	"$(scanorder s4d1)" "$(scanorder s4d2)"

S4D1_RET="$(q "SELECT pgcolumnar.recluster_hilbert('s4d1','b','a');")"
S4D1_MOVED="$([ "$S4D1_RET" -gt 0 ] 2>/dev/null && echo yes || echo no)"
check "(d) recluster_hilbert over DIFFERENT columns still rewrites (>0)" "$S4D1_MOVED" "yes"
check "(d) and that rewrite moved s4d1's layout" \
	"$(changed "$S4D1_PHYS" "$(physlayout s4d1)")" "moved"
check_text "(d) and it is still a hilbert table, now on the new key" "$(skind s4d1)/$(skey s4d1)" "hilbert/{b,a}"
check_num "(d) and no row was lost from s4d1" "$(q 'SELECT count(*) FROM s4d1;')" "20000"

S4D2_RET="$(q "SELECT pgcolumnar.recluster('s4d2','b','a');")"
S4D2_MOVED="$([ "$S4D2_RET" -gt 0 ] 2>/dev/null && echo yes || echo no)"
check "(d) plain recluster over DIFFERENT columns rewrites a hilbert table (>0)" "$S4D2_MOVED" "yes"
check "(d) and that rewrite moved s4d2's layout" \
	"$(changed "$S4D2_PHYS" "$(physlayout s4d2)")" "moved"
check_text "(d) and naming the plain verb with a NEW key is the explicit switch back to zorder" \
	"$(skind s4d2)/$(skey s4d2)" "zorder/{b,a}"
check_num "(d) and no row was lost from s4d2" "$(q 'SELECT count(*) FROM s4d2;')" "20000"

# THE ONLINE VERB'S CURVE DEFENCE, and it is gated on both rewrites having
# happened. Untouched, s4d1 is still on the (a,b) layout while s4d2 is on the
# (b,a) one, so "they differ" is satisfied by recluster_hilbert doing nothing --
# which is what it does today, and the arm printed PASS on it (measured).
if [ "$S4D1_MOVED" = "yes" ] && [ "$S4D2_MOVED" = "yes" ]; then
	check "(d) and the HILBERT rewrite over (b,a) is NOT the ZORDER rewrite over (b,a) on the identical data" \
		"$(differs "$(scanorder s4d1)" "$(scanorder s4d2)")" "different"
else
	check_unrunnable "(d) and the HILBERT rewrite over (b,a) is NOT the ZORDER rewrite over (b,a) on the identical data" \
		UNMET_PRECONDITION \
		"a rewrite did not happen (s4d1=[$S4D1_RET], s4d2=[$S4D2_RET]), so the two layouts are not the two curves"
fi

# =============================================================================
# S5  ncols == 1 IS THE IDENTITY, THROUGH SQL
# =============================================================================
#
# Over one column the Hilbert index and the Morton index are both the identity,
# so the two verbs must produce the SAME physical order while recording
# DIFFERENT kinds. Each side is also compared against its OWN pre-cluster
# baseline: two calls that both no-opped would compare equal to each other and
# the arm would pass on nothing.
#
# THIS SECTION BUYS SURFACE AND IDENTITY, NEVER THE CURVE. Over one column a
# relabelled Z-order implementation is INDISTINGUISHABLE from a correct one --
# all of S5 is green on one, by construction (measured). Do not read a green S5
# as evidence of Hilbertness; S3, S4(d) and S7 carry that.

mk5() {
	psql_run "CREATE TABLE $1 (a int, b int, pad text) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', stripe_row_limit => $SR, chunk_group_row_limit => $CG);"
	psql_run "INSERT INTO $1 SELECT (g*2654435761)::bigint % 1000, g, md5(g::text)
	          FROM generate_series(1,20000) g;"
	check_num "premise: set_options took on $1, so it is 20 groups" "$(groups "$1")" "20"
}
mk5 s5hi
mk5 s5zo
S5HI_BEFORE="$(scanorder s5hi)"
S5ZO_BEFORE="$(scanorder s5zo)"
S5HI_SET="$(setof s5hi)"
S5ZO_SET="$(setof s5zo)"
check_text "premise: the two single-column fixtures start identical" "$S5HI_BEFORE" "$S5ZO_BEFORE"
check "premise: the plan being digested for s5hi is the columnar custom scan too" \
	"$(pgc_is_columnar_scan 'SELECT * FROM s5hi')" "yes"

hrun "cluster_hilbert('s5hi','a')" "SELECT pgcolumnar.cluster_hilbert('s5hi','a');"
hrun "cluster('s5zo','a')"         "SELECT pgcolumnar.cluster('s5zo','a');"

check "cluster_hilbert changed s5hi's order from its own baseline" \
	"$(changed "$S5HI_BEFORE" "$(scanorder s5hi)")" "moved"
check "cluster changed s5zo's order from its own baseline" \
	"$(changed "$S5ZO_BEFORE" "$(scanorder s5zo)")" "moved"
# AND BOTH STILL HOLD THEIR ROWS. Two verbs that record their kind and then
# EMPTY the table satisfy both premises above -- the emptied digest differs from
# every baseline, so both read "moved", and then they compare equal to each
# other and the identity arm passes too.
check_num "premise: and s5hi still holds every row it started with" "$(q 'SELECT count(*) FROM s5hi;')" "20000"
check_num "premise: and s5zo still holds every row it started with" "$(q 'SELECT count(*) FROM s5zo;')" "20000"
check_text "premise: and s5hi's rows are the same rows, only reordered" "$(setof s5hi)" "$S5HI_SET"
check_text "premise: and s5zo's rows are the same rows, only reordered" "$(setof s5zo)" "$S5ZO_SET"

check_text "over ONE column the two curves are the identity: the physical order is the same" \
	"$(scanorder s5hi)" "$(scanorder s5zo)"
# The bare inequality arm that used to sit here is gone: <NULL> != 'zorder'
# passed it on a tree with no cluster_hilbert at all. The named pair below is
# strictly stronger and cannot be satisfied by a kind that was never written.
check_text "and each records its own verb's kind" "$(skind s5hi)/$(skind s5zo)" "hilbert/zorder"

# =============================================================================
# S6  vacuum_sorted MUST NOT CLOBBER A HILBERT TABLE
# =============================================================================
#
# See the header: the pinned value here is a READING of the ruling, and this is
# the arm to change if the owner meant an honest relabel rather than a no-op.
#
# THE CALL IS ASSERTED, NOT MADE. A vacuum_sorted that RAISES on every Hilbert
# table leaves the order and the kind untouched, which is byte-identical to the
# no-op this section pins -- all of S6 was green on exactly that shim (measured,
# 2026-09-09). hrun's SQLSTATE arm is what tells the two apart.

mk4 s6t
hrun "cluster_hilbert('s6t','a','b')" "SELECT pgcolumnar.cluster_hilbert('s6t','a','b');"
check_text "s6t is a hilbert table before vacuum_sorted touches it" "$(skind s6t)" "hilbert"
check "premise: the plan being digested for s6t is the columnar custom scan too" \
	"$(pgc_is_columnar_scan 'SELECT * FROM s6t')" "yes"
# THE ORDER DIGEST IS THE INSTRUMENT HERE, NOT physlayout. vacuum_sorted is an
# EAGER verb, and an eager rewrite reproduces the stripe geometry exactly, so
# the layout digest reads "unchanged" whether the table was clobbered or left
# alone -- a check that cannot fail. S2's control measures that directly.
S6_SEQ="$(scanorder s6t)"
hrun "vacuum_sorted('s6t','a') on a hilbert table" "SELECT pgcolumnar.vacuum_sorted('s6t','a');"
check_text "vacuum_sorted leaves a hilbert table's physical ORDER identical: it did not re-sort it" \
	"$(scanorder s6t)" "$S6_SEQ"
check_text "and leaves the recorded kind hilbert, not relabelled lexicographic" "$(skind s6t)" "hilbert"
check_text "and the reporter agrees with the catalog, so the two do not disagree about it" "$(skindst s6t)" "hilbert"
check_num "and no row was lost" "$(q 'SELECT count(*) FROM s6t;')" "20000"

# The removal proof. Without this the arms above are satisfied by a
# vacuum_sorted that no-ops on EVERYTHING, which is a worse defect than the one
# they guard.
mk4 s6c
S6C_SEQ="$(scanorder s6c)"
hrun "vacuum_sorted('s6c','a') on a table with no recorded kind" "SELECT pgcolumnar.vacuum_sorted('s6c','a');"
check "control: vacuum_sorted still REORDERS a table with no recorded kind" \
	"$(changed "$S6C_SEQ" "$(scanorder s6c)")" "moved"
check_num "control: and the rows really are ascending on a afterwards" \
	"$(q "SELECT count(*) FROM (SELECT a < lag(a) OVER () AS d FROM s6c) z WHERE d;")" "0"
check_text "control: and it still records its own kind there" "$(skind s6c)" "lexicographic"

# =============================================================================
# S7  THE DAEMON PRESERVES THE CURVE
# =============================================================================
#
# This is the arm the ruling exists for, so it DRIVES THE DAEMON. The daemon
# builds its own call at src/columnar_autovacuum.c:283-291 and hard-codes
# pgcolumnar.recluster; calling recluster() from the suite and reasoning about
# it would measure the neighbouring question.
#
# Three twins from one fixture, with identical appended decay:
#
#	av_hi   hilbert, left decayed -- the DAEMON's subject
#	av_ref  hilbert, reclustered BY HAND with recluster_hilbert -- what a
#	        Hilbert rewrite of this data produces
#	av_zo   zorder,  reclustered BY HAND with recluster -- the control that
#	        makes "the layout is a Hilbert layout" a discrimination rather
#	        than a restatement of "something ran"
#
# The two hand-driven twins are reclustered BEFORE the daemon is enabled, so
# they have no appended tail left and the daemon has no reason to touch them.
# THAT IS A PREMISE, NOT A GUARANTEE: when recluster_hilbert was a stub, av_ref
# kept its tail, stayed due, and the daemon reclustered IT as well -- the two
# converged on the same Z-order layout and "av_hi matches the hilbert twin"
# passed on it (measured). So both references are FROZEN across the daemon
# window and asserted unchanged afterwards.
#
# AND THE DAEMON HAS TWO DISPATCHES. compact_rewrite would also fold the tail,
# and it records 'zorder' (src/columnar_vacuum.c:1576), so "the tail folded"
# alone cannot name which one ran. compact_rewrite_due is asserted false, and
# the daemon's own log line is read for the dispatch that did the work.

check_num "premise: the maintenance launcher is running" \
	"$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'pgcolumnar autovacuum launcher';")" "1"
check_text "premise: the daemon is OFF while the fixtures are built" "$(q "SHOW pgcolumnar.autovacuum;")" "off"

mk7() {
	psql_run "CREATE TABLE $1 (a int, b int, pad text) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', stripe_row_limit => $SR, chunk_group_row_limit => $CG);"
	psql_run "INSERT INTO $1 SELECT (g*2654435761)::bigint % 1000, g, md5(g::text)
	          FROM generate_series(1,20000) g;"
	check_num "premise: set_options took on $1, so it is 20 groups" "$(groups "$1")" "20"
}

mk7 av_hi;  hrun "cluster_hilbert('av_hi','a','b')"  "SELECT pgcolumnar.cluster_hilbert('av_hi','a','b');"
mk7 av_ref; hrun "cluster_hilbert('av_ref','a','b')" "SELECT pgcolumnar.cluster_hilbert('av_ref','a','b');"
mk7 av_zo;  hrun "cluster('av_zo','a','b')"          "SELECT pgcolumnar.cluster('av_zo','a','b');"
# av_zd: a ZORDER table left decayed for the daemon (#1339 :1362). Every table the
# daemon handled here used to be a hilbert one, so a daemon that reclustered
# everything on the hilbert curve -- converting a layout the user chose, on a
# timer -- passed the whole section.
mk7 av_zd;  hrun "cluster('av_zd','a','b')"          "SELECT pgcolumnar.cluster('av_zd','a','b');"
decay4 av_hi; decay4 av_ref; decay4 av_zo; decay4 av_zd

check_text "av_hi is a hilbert table with appended decay for the daemon to find" \
	"$(skind av_hi)/$([ "$(appended av_hi)" -gt 0 ] 2>/dev/null && echo decayed || echo clean)" \
	"hilbert/decayed"
check_text "and the key the daemon will read off it is the one it was clustered by" \
	"$(skey av_hi)" "{a,b}"
# BOTH halves of the daemon's dispatch, so the recluster is its only reason to
# touch av_hi. Without the compaction half, a compact_rewrite that folded the
# tail and relabelled the table zorder would pass the arm below and redden THE
# RULING, and the reader would attribute both to the recluster path.
check_text "the daemon agrees a recluster is due, and that a compaction is NOT" \
	"$(q "SELECT recluster_due::text || '/' || compact_rewrite_due::text FROM pgcolumnar.maintenance_due('av_hi');")" \
	"true/false"
check "premise: the plan being digested for av_hi is the columnar custom scan too" \
	"$(pgc_is_columnar_scan 'SELECT * FROM av_hi')" "yes"
check_text "av_zd is a zorder table with appended decay for the daemon to find" \
	"$(skind av_zd)/$([ "$(appended av_zd)" -gt 0 ] 2>/dev/null && echo decayed || echo clean)" \
	"zorder/decayed"
check_text "and the daemon agrees av_zd's recluster is due, and that a compaction is NOT" \
	"$(q "SELECT recluster_due::text || '/' || compact_rewrite_due::text FROM pgcolumnar.maintenance_due('av_zd');")" \
	"true/false"

# The two references, driven by hand while the daemon is still off.
hrun "recluster_hilbert('av_ref','a','b')" "SELECT pgcolumnar.recluster_hilbert('av_ref','a','b');"
hrun "recluster('av_zo','a','b')"          "SELECT pgcolumnar.recluster('av_zo','a','b');"
REF_FOLDED="$(appended av_ref)"
ZO_FOLDED="$(appended av_zo)"
check_num "the hand-driven hilbert reference folded its tail back in" "$REF_FOLDED" "0"
check_num "the hand-driven zorder control folded its tail back in" "$ZO_FOLDED" "0"
# Gated for the same reason S3's and S4(d)'s comparisons are: an av_ref that was
# never reclustered still carries its tail, so it differs from av_zo for a
# reason that has nothing to do with the curve, and the premise passes on it.
if [ "$REF_FOLDED" = "0" ] && [ "$ZO_FOLDED" = "0" ]; then
	check "the two references really are two different layouts, so the arm below discriminates" \
		"$(differs "$(scanorder av_ref)" "$(scanorder av_zo)")" "different"
else
	check_unrunnable "the two references really are two different layouts, so the arm below discriminates" \
		UNMET_PRECONDITION \
		"a hand-driven reference did not fold its tail (av_ref=[$REF_FOLDED], av_zo=[$ZO_FOLDED]), so the two layouts are not the two rewrites"
fi
# AND av_hi MUST NOT ALREADY EQUAL av_ref. Without this the "the daemon produced
# a Hilbert layout" arm below is satisfied by a daemon that did nothing at all:
# two twins nobody touched compare equal, and the arm passes on nothing. av_hi
# still carries its decayed tail here, so it must differ. THE ARM BELOW IS
# GATED ON THIS RESULT rather than merely preceded by it -- a premise that reds
# while the arm it guards still prints PASS leaves the flagship green on a tree
# with no Hilbert code in it, which is exactly what happened.
S7_PRE="$(differs "$(scanorder av_hi)" "$(scanorder av_ref)")"
check "av_hi does NOT yet match the hilbert reference, so matching it later is a change" \
	"$S7_PRE" "different"

# THE REFERENCES ARE FROZEN HERE. Everything below compares against these
# values, not against a fresh read, so a daemon that rewrites a reference during
# its window cannot make the comparison true by moving the target.
REF_SEQ="$(scanorder av_ref)"
REF_APP="$(appended av_ref)"
ZO_SEQ="$(scanorder av_zo)"
AV_HI_SET="$(setof av_hi)"

psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum = on;"
q "SELECT pg_reload_conf();" >/dev/null
check_text "the daemon is now ON" "$(q "SHOW pgcolumnar.autovacuum;")" "on"

AV_AFTER="$(appended av_hi)"
ZD_AFTER="$(appended av_zd)"
for _ in $(seq 1 15); do
	sleep 2
	AV_AFTER="$(appended av_hi)"
	ZD_AFTER="$(appended av_zd)"
	[ "${AV_AFTER:-1}" = "0" ] && [ "${ZD_AFTER:-1}" = "0" ] && break
done
check_num "the daemon reclustered av_hi (its appended tail folded to 0)" "$AV_AFTER" "0"

# WHO DID IT, read from the daemon's own log rather than inferred from a
# counter. src/columnar_autovacuum.c:291 emits the recluster line at LOG and
# :275 the compact_rewrite line; both land in this suite's own server log.
AV_RECLOG="$(grep -c 'pgcolumnar autovacuum: recluster public\.av_hi ' "$PGC_LOGFILE")"
AV_CMPLOG="$(grep -c 'pgcolumnar autovacuum: compact_rewrite public\.av_hi' "$PGC_LOGFILE")"
AV_REFLOG="$(grep -c 'pgcolumnar autovacuum: \(recluster\|compact_rewrite\) public\.av_\(ref\|zo\)' "$PGC_LOGFILE")"
check "and the daemon's own log names the dispatch that did it: recluster on av_hi" \
	"$([ "$AV_RECLOG" -ge 1 ] 2>/dev/null && echo yes || echo no)" "yes"
check_num "and no compaction dispatch touched av_hi, so the recluster path is the only candidate" \
	"$AV_CMPLOG" "0"
check_num "and the daemon never touched either reference, so they are still the twins they were" \
	"$AV_REFLOG" "0"
check_text "and the hilbert reference is byte-for-byte what it was before the daemon ran" \
	"$(scanorder av_ref)" "$REF_SEQ"
check_num "and it still has no appended tail, so nothing rewrote it behind the comparison" \
	"$(appended av_ref)" "$REF_APP"

check_text "THE RULING: the daemon left the table hilbert, it did not convert it to zorder" \
	"$(skind av_hi)" "hilbert"
check_text "and sort_status agrees with the catalog about it" "$(skindst av_hi)" "hilbert"

# THE OTHER HALF OF THE DISPATCH: a decayed zorder table is reclustered too, and on
# its own curve. A daemon that sent every table to recluster_hilbert folded
# av_zd's tail just as well, so the curve and key are what tell the two apart.
check_num "the daemon reclustered the decayed zorder table av_zd too (its appended tail folded to 0)" \
	"$ZD_AFTER" "0"
check_text "and it kept av_zd on the zorder curve and its key: a non-hilbert table is not converted" \
	"$(skind av_zd)/$(skey av_zd)" "zorder/{a,b}"

if [ "$S7_PRE" = "different" ] && [ "$REF_FOLDED" = "0" ]; then
	check_text "and the layout the daemon produced IS a Hilbert layout: identical to the hand-driven hilbert twin" \
		"$(scanorder av_hi)" "$REF_SEQ"
	check "and it is NOT the zorder layout, which is what a hard-coded recluster would have left" \
		"$(differs "$(scanorder av_hi)" "$ZO_SEQ")" "different"
else
	check_unrunnable "and the layout the daemon produced IS a Hilbert layout: identical to the hand-driven hilbert twin" \
		UNMET_PRECONDITION \
		"av_hi already matched the reference, or the reference was never rebuilt (pre=[$S7_PRE], av_ref tail=[$REF_FOLDED]), so the comparison cannot be a change"
	check_unrunnable "and it is NOT the zorder layout, which is what a hard-coded recluster would have left" \
		UNMET_PRECONDITION \
		"av_hi already matched the reference, or the reference was never rebuilt (pre=[$S7_PRE], av_ref tail=[$REF_FOLDED]), so this discriminates nothing"
fi
check_num "and no row was lost in the process" "$(q 'SELECT count(*) FROM av_hi;')" "25000"
check_text "and the rows are the same rows, only reordered" "$(setof av_hi)" "$AV_HI_SET"

psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum = off;"
q "SELECT pg_reload_conf();" >/dev/null
sleep 1
check_text "and the daemon is OFF again, so the suite ends on the invariant it opened with" \
	"$(q "SHOW pgcolumnar.autovacuum;")" "off"

# =============================================================================
# S8  THE ENUMERATIONS REACT
# =============================================================================
#
# test/entry_point_privilege.sh enumerates C entry points from pg_proc.prosrc
# and cross-checks that set against the install script's
# AS 'MODULE_PATHNAME','<symbol>' clauses. Two new functions must appear in
# BOTH, and the two sets must stay identical.
#
# THE PROJECT RULE, restated because it has been lost three times: RESOLVE THE
# C SYMBOL FROM THE AS CLAUSE, NEVER BY DERIVING pgcolumnar_<sqlname>.
# get_storage_id (-> pgcolumnar_relation_storageid) and columnar_handler
# (-> pgcolumnar_handler) both break that convention and have been dropped by
# three separate enumerations that derived the name. So nothing below asserts
# that cluster_hilbert's symbol IS 'pgcolumnar_cluster_hilbert'. It asserts that
# whatever prosrc names is also declared in the script -- which is true whatever
# the implementer calls it.

_hc_root="$(dirname "${BASH_SOURCE[0]}")/.."
_hc_ver="$(sed -n "s/^default_version *= *'\(.*\)'.*/\1/p" "$_hc_root/pgcolumnar.control")"
SQLFILE="$_hc_root/pgcolumnar--$_hc_ver.sql"
check "premise: the install script derived from default_version exists" \
	"$([ -r "$SQLFILE" ] && echo yes || echo "missing: $SQLFILE")" "yes"

# Comments blanked first: a doc comment naming a symbol is not a declaration,
# and counting one is how a census comes out wrong in the other direction.
#
# The symbol class is [A-Za-z0-9_] and the separator swallows a tab. Every one
# of the 42 MODULE_PATHNAME symbols in the current script is lowercase with no
# digit, so the narrower class was correct today and would have gone RED, loudly
# but at the wrong address, on the first symbol carrying a digit.
src_syms="$(sed 's,--.*,,' "$SQLFILE" \
	| tr '\n' ' ' \
	| grep -o "AS 'MODULE_PATHNAME'[,[:space:]]*'[A-Za-z0-9_]*'" \
	| grep -o "'[A-Za-z0-9_]*'$" | tr -d "'" | sort -u | tr '\n' ' ')"
cat_syms="$(q "SELECT string_agg(DISTINCT p.prosrc, ' ' ORDER BY p.prosrc)
               FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'pgcolumnar'
                 AND p.prolang = (SELECT oid FROM pg_language WHERE lanname = 'c');")"
cat_syms="$(printf '%s\n' $cat_syms | sort -u | tr '\n' ' ')"

check "premise: the install script declares MODULE_PATHNAME symbols at all" \
	"$([ "$(printf '%s\n' $src_syms | grep -c .)" -ge 20 ] && echo yes || echo no)" "yes"
# THE TWO LISTS ARE COMPARED, NOT A TOKEN ABOUT THEM (#1339): "same" was also what
# two empty lists produced, and check_text refuses an empty side. A failure now
# names the symbols rather than saying "differs".
check_text "the catalog and the install script still agree on the symbol set" \
	"$(printf '%s\n' $cat_syms | sort -u | tr '\n' ' ' | sed 's/ $//')" \
	"$(printf '%s\n' $src_syms | sort -u | tr '\n' ' ' | sed 's/ $//')"

for fn in cluster_hilbert recluster_hilbert; do
	check_num "pgcolumnar.$fn is installed, exactly once" \
		"$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
		      WHERE n.nspname = 'pgcolumnar' AND p.proname = '$fn';")" "1"

	# From prosrc. The server resolved it; no naming convention is involved.
	_sym="$(q "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	           WHERE n.nspname = 'pgcolumnar' AND p.proname = '$fn'
	             AND p.prolang = (SELECT oid FROM pg_language WHERE lanname = 'c');")"
	check "$fn is a C function and the catalog names its symbol" \
		"$([ -n "$_sym" ] && echo yes || echo "no symbol")" "yes"
	check "and the symbol the CATALOG names for $fn is declared in the install script's AS clause" \
		"$(case " $src_syms " in *" ${_sym:-__none__} "*) echo declared ;; *) echo "MISSING: ${_sym:-<none>}" ;; esac)" \
		"declared"
done

# The surface itself, compared against the sibling verb rather than retyped: the
# argument types, the variadic element type and the return type must match the
# established verb the new one shadows.
sigof() {	# sigof FN -> argtypes | variadic element type | return type
	q "SELECT p.proargtypes::text || '|' || p.provariadic::text || '|' || p.prorettype::text
	   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	   WHERE n.nspname = 'pgcolumnar' AND p.proname = '$1';"
}
check_text "cluster_hilbert has exactly cluster's signature (args, VARIADIC element, return type)" \
	"$(sigof cluster_hilbert)" "$(sigof cluster)"
check_text "recluster_hilbert has exactly recluster's signature (args, VARIADIC element, return type)" \
	"$(sigof recluster_hilbert)" "$(sigof recluster)"

pgc_summary

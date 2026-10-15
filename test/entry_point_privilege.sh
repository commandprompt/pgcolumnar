#!/usr/bin/env bash
#
# pgColumnar: every relation-taking entry point has a decided privilege bar (#569).
#
# This suite exists because three separate coverage scans in this tree each lost
# members of the class they were written to enumerate, and each reported success
# while doing it:
#
#   - test/server_file_privilege.sh scans for functions declaring a file-path
#     argument, so it cannot see any relation-taking function without one.
#   - the projection suite has no coverage arm at all.
#   - two enumerations written on 2026-08-10, independently, both derived the C
#     symbol as pgcolumnar_<sqlname> and therefore silently dropped
#     get_storage_id (-> pgcolumnar_relation_storageid) and columnar_handler
#     (-> pgcolumnar_handler). Nothing errored. The lists just came out shorter.
#
# Three design choices follow from those failures, and they are the whole point
# of this file:
#
#   1. ENUMERATE FROM THE CATALOG, NOT THE SCRIPT. pg_proc.prosrc IS the C
#      symbol, resolved by the server with no parsing, so no naming convention
#      can be broken. The install script is then cross-checked against it as a
#      set, which catches a function declared but not installed, or installed
#      under a different symbol.
#   2. BUCKET ON THE TYPE, NOT ON A PARAMETER NAME. "Takes a relation" is
#      proargtypes[0] = 'regclass'::regtype. Nine of these functions name that
#      parameter tablename or target rather than rel, and several put it on a
#      continuation line, so every name-based or line-oriented regex drops most
#      of the class.
#   3. ASSERT SQLSTATE, NOT ERROR TEXT. A deny arm that greps for "permission
#      denied" is satisfied by a login FATAL, a missing function (42883), a bad
#      argument (22023), or a transaction-block refusal (25001). Only
#      aclcheck_error produces 42501.
#
# Usage:  test/entry_point_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Derive the base install script from default_version, so a version bump that
# renames pgcolumnar--<ver>.sql does not silently point this at a missing file
# (which reads as zero declared symbols, not as a failure of the thing under test).
_epp_root="$(dirname "${BASH_SOURCE[0]}")/.."
_epp_ver="$(sed -n "s/^default_version *= *'\(.*\)'.*/\1/p" "$_epp_root/pgcolumnar.control")"
SQLFILE="$_epp_root/pgcolumnar--$_epp_ver.sql"

# Functions that take no relation and therefore have no relation-level bar to
# decide. Written out by name on purpose: adding a new no-relation function
# fails the coverage arm until somebody names it here and says why.
#
#   columnar_handler       returns the table AM routine to core, no arguments
#   parquet_fdw_handler    returns the FDW routine to core, no arguments
#   parquet_fdw_validator  validates an option list, no relation
#   parquet_schema         reads a FILE, gated by the server-file role (#559)
#   read_parquet           reads a FILE, gated by the server-file role (#559)
#   file_split_offsets     reads a FILE, gated by the server-file role (#559)
#   iceberg_current_snapshot reads a metadata.json FILE, server-file role (#388)
#   iceberg_data_files     reads an Iceberg table's FILES, server-file role (#388)
#   iceberg_scan           reads an Iceberg table's FILES, server-file role (#388)
#   read_avro_manifest     reads an Avro FILE, gated by the server-file role (#388)
#   iceberg_rest_table_location  reads an external REST catalog, server-file role (#388 phase 7)
#   iceberg_rest_scan      reads a REST-cataloged table's FILES, server-file role (#388 phase 7)
#   iceberg_rest_namespaces / iceberg_rest_tables  list a REST catalog, server-file role (#388 phase 7)
#   read_manifest_list     reads an Avro FILE, gated by the server-file role (#388)
#   row_tid                maps a row number to a TID by arithmetic (#1294): it
#                          takes no relation and reads nothing. A version that
#                          took a relation would need the SELECT check the others have.
#   rollup_record_dependencies  a TRIGGER function (#1376): PostgreSQL refuses to call it
#                          directly, and it runs only on writes to pgcolumnar.rollup,
#                          whose row-level security asks that the writer own the
#                          view; it checks SELECT on the source itself (rollup.sh).
#
# Kept on ONE line and matched after whitespace normalisation. A multi-line list
# here is matched by `case " $EXEMPT " in *" $fn "*`, and the name sitting at a
# line end is followed by a newline rather than a space, so it silently fails to
# match and reads as an unbucketed function. That is not hypothetical: it is what
# the first run of this suite reported for parquet_fdw_validator.
EXEMPT="columnar_handler parquet_fdw_handler parquet_fdw_validator iceberg_fdw_handler iceberg_fdw_validator iceberg_catalog_fdw_validator parquet_schema read_parquet file_split_offsets iceberg_current_snapshot iceberg_data_files iceberg_scan iceberg_rest_table_location iceberg_rest_scan iceberg_rest_namespaces iceberg_rest_tables read_avro_manifest read_manifest_list row_tid rollup_record_dependencies"
EXEMPT="$(echo $EXEMPT)"

# Relation-taking entry points with NO privilege check of any kind on main.
# PINNED AS THE WRONG VALUE, deliberately, per CONTEXT.md: when a fix lands this
# suite goes red and forces the list to shrink on purpose, rather than a comment
# nobody re-reads. Each carries the issue that owns it.
# EMPTY, and that is the point of the list rather than an oversight.
#
# #558 and #562 landed first (d0d7309, 77b530d), then #559 and #566 (819ce21), and
# each landing turned the pins for those functions RED and forced this line to
# shrink. Every relation-taking C entry point is now guarded, and each one is
# asserted behaviourally below rather than trusted.
#
# A name added back here is a claim that something is ungated, and it needs an
# issue number beside it.
KNOWN_UNGUARDED=""

# Functions that ACT on relations without declaring one as their first argument,
# so `proargtypes[0] = 'regclass'` cannot see them. Named explicitly, because the
# alternative is to let them fall out of the population the way the language
# filter did.
#
# This is the blind spot of the type-based rule this suite argues for, and it is
# worth stating plainly rather than hiding: bucketing on the declared type beats
# bucketing on a parameter NAME, and it still misses a function that takes text
# and resolves a relation itself.
#
#   alter_table_set_access_method(t text, method text)
#       plpgsql, and its first statement is `rel regclass := t::regclass`. It is
#       relation-taking in every sense except the declaration.
#   vacuum_full(schema name, sleep_time real, stripe_count int)
#       schema-scoped: it loops over the tables in a schema, so its bar is
#       whatever the per-table path it calls enforces, not a bar of its own. It
#       also carries the same phantom `stripe_count` argument as
#       pgcolumnar.vacuum (#560).
#   create_rollup(rollup name, source regclass, ...)
#       plpgsql; its relation is the SECOND argument (#1294). It runs as the
#       caller and reads the source's storage id through get_storage_id, which
#       is guarded (below), so a caller that cannot SELECT the source is refused
#       with 42501 at creation. That is asserted in rollup.sh and
#       test_rollup.py, not here.
#
# The first two are recorded here rather than resolved; neither is claimed
# guarded.
RELATION_BY_OTHER_MEANS="alter_table_set_access_method vacuum_full create_rollup"
RELATION_BY_OTHER_MEANS="$(echo $RELATION_BY_OTHER_MEANS)"

# Relation-taking entry points that are NOT C functions. Their bar is not a
# pg_class_aclcheck in a C body at all: they are SQL/plpgsql and read the
# pgcolumnar catalog tables directly, so what governs them is the ACL on those
# tables. That is a different mechanism and a different defect (#560), and it is
# named here rather than filtered out of the population, so the coverage arm
# balances over the real class.
CATALOG_GOVERNED="analyze rebuild_projections reset_options set_options sort_status stats"
CATALOG_GOVERNED="$(echo $CATALOG_GOVERNED)"

# Relation-taking entry points that are supposed to REFUSE an unprivileged
# caller, asserted behaviourally rather than believed.
# #1386 added the four after get_storage_id: each had no arm anywhere that calls it as
# a caller its bar should stop (cluster_hilbert and recluster_hilbert share cluster's
# owner check in code, which is a reason to expect a refusal, not an assertion of one).
GUARDED_BEHAVIOURAL="read_projection reconstruct_via_projection vm_is_visible vm_selftest get_storage_id cluster_hilbert recluster_hilbert truncate require_caller_select"
GUARDED_BEHAVIOURAL="$(echo $GUARDED_BEHAVIOURAL)"

# ---- the enumeration, from the catalog -------------------------------------
#
# prosrc is the C symbol. proargtypes[0] is the first argument's type oid. Both
# come from the server, so neither can be lost to a naming convention or a line
# break in the install script.

installed="$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'pgcolumnar' AND p.prolang = (SELECT oid FROM pg_language WHERE lanname = 'c');")"
symbols="$(q "SELECT string_agg(DISTINCT p.prosrc, ' ' ORDER BY p.prosrc)
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'pgcolumnar' AND p.prolang = (SELECT oid FROM pg_language WHERE lanname = 'c');")"
# NO LANGUAGE FILTER HERE, deliberately. An earlier version of this file scoped
# the population with prolang = 'c', which silently excluded six relation-taking
# functions written in sql/plpgsql (analyze, rebuild_projections, reset_options,
# set_options, sort_status, stats). The coverage arm still balanced, because it
# balanced over the truncated population. That is the same disease this file
# indicts the older scans for, moved from name-derivation to a language filter,
# and it was found by review rather than by the suite. The language filter
# survives only where prosrc is read AS a C symbol, above.
relfns="$(q "SELECT string_agg(DISTINCT p.proname, ' ' ORDER BY p.proname)
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'pgcolumnar'
               AND p.proargtypes[0] = 'regclass'::regtype;")"
allfns="$(q "SELECT string_agg(DISTINCT p.proname, ' ' ORDER BY p.proname)
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'pgcolumnar';")"

# ---- premises: the enumeration is real -------------------------------------
#
# An empty or tiny enumeration would satisfy every assertion below by having
# nothing to check. This is the floor that stops a broken query reading as a
# clean bill of health.
check "premise: the extension is installed and C entry points were found" \
	"$([ "${installed:-0}" -ge 20 ] && echo yes || echo no)" "yes"
check "premise: at least one relation-taking entry point was found" \
	"$([ -n "$relfns" ] && echo yes || echo no)" "yes"

# The catalog and the install script must agree as SETS. A function declared in
# the script but not installed, or installed under a different symbol, is a hole
# in the enumeration rather than a fact about the code.
#
# Comments are blanked first: a doc comment mentioning pgcolumnar.storage is not
# a declaration, and counting one is how a census comes out wrong in the other
# direction.
src_syms="$(sed 's,--.*,,' "$SQLFILE" \
	| tr '\n' ' ' \
	| grep -o "AS 'MODULE_PATHNAME'[, ]*'[a-z_]*'" \
	| grep -o "'[a-z_]*'$" | tr -d "'" | sort -u | tr '\n' ' ')"
cat_syms="$(printf '%s\n' $symbols | sort -u | tr '\n' ' ')"
check "premise: the install script declares MODULE_PATHNAME symbols at all" \
	"$([ "$(printf '%s\n' $src_syms | grep -c .)" -ge 20 ] && echo yes || echo no)" "yes"
check "the catalog and the install script agree on the symbol set" \
	"$(diff <(printf '%s\n' $src_syms) <(printf '%s\n' $cat_syms) >/dev/null && echo same || echo differs)" \
	"same"

# ---- the coverage assertion ------------------------------------------------
#
# Every C entry point lands in exactly one bucket, and the buckets sum to the
# input. This is the line that makes the rest a fact rather than a list.

exempt_seen=0 rel_seen=0 other_seen=0 unbucketed=""
for fn in $allfns; do
	case " $EXEMPT " in *" $fn "*) exempt_seen=$((exempt_seen + 1)); continue ;; esac
	case " $relfns " in *" $fn "*) rel_seen=$((rel_seen + 1)); continue ;; esac
	case " $RELATION_BY_OTHER_MEANS " in *" $fn "*) other_seen=$((other_seen + 1)); continue ;; esac
	unbucketed="$unbucketed $fn"
done

# The non-C relation-taking functions must all be inside the relation-taking
# population, or the CATALOG_GOVERNED list has gone stale against the catalog.
missing_cg=""
for fn in $CATALOG_GOVERNED; do
	case " $relfns " in *" $fn "*) ;; *) missing_cg="$missing_cg $fn" ;; esac
done
check "every catalog-governed name is still a relation-taking entry point" \
	"$([ -z "${missing_cg// /}" ] && echo none || echo "$missing_cg")" "none"
n_all="$(printf '%s\n' $allfns | grep -c .)"

check_num "coverage: inputs == sum(buckets)" \
	"$n_all" "$(( exempt_seen + rel_seen + other_seen + $(printf '%s\n' $unbucketed | grep -c .) ))"
check_num "the relation-by-other-means class is the three that are known" "$other_seen" "3"
check "coverage: no entry point is unbucketed" \
	"$([ -z "${unbucketed// /}" ] && echo none || echo "$unbucketed")" "none"

# The EXEMPT list is an escape hatch, so it needs a guard of its own. Without
# this, adding a real relation-taking function to EXEMPT removes it from the
# population and NOTHING fails: the coverage arm still balances, because the
# function was counted as exempt rather than dropped. Proven by mutation on
# 2026-08-10 -- inserting `vacuum` into EXEMPT produced a fully green run, which
# is the same "a check too tight to fail" shape this suite exists to prevent.
#
# An exempt name that takes a relation is a contradiction: exempt means "has no
# relation-level bar to decide", and proargtypes says otherwise.
overclaimed=""
for fn in $EXEMPT; do
	case " $relfns " in *" $fn "*) overclaimed="$overclaimed $fn" ;; esac
done
check "no exempt name actually takes a relation" \
	"$([ -z "${overclaimed// /}" ] && echo none || echo "$overclaimed")" "none"

# Every relation-taking function splits into pinned-as-unguarded or
# claimed-guarded. The obvious loop here is a TRAP and the first version of this
# file fell into it: asserting that each function is "pinned or claimed-guarded"
# is true by construction, because claimed-guarded is the else branch. It passed
# for every function including one the whitespace bug had already mis-sorted.
#
# So assert things that CAN be false instead.
#
# 1. Every pinned name still exists as a relation-taking entry point. A pin for a
#    function that was renamed or dropped is a stale pin that silently stops
#    guarding anything.
stale=""
for fn in $KNOWN_UNGUARDED; do
	case " $relfns " in *" $fn "*) ;; *) stale="$stale $fn" ;; esac
done
check "no pinned name is stale (all still exist and take a relation)" \
	"$([ -z "${stale// /}" ] && echo none || echo "$stale")" "none"

# 2. The split is exhaustive and the two sides sum to the population, printed
#    from the data rather than retyped.
pinned_n=0 guarded_n=0 catalog_n=0
for fn in $relfns; do
	case " $CATALOG_GOVERNED " in *" $fn "*) catalog_n=$((catalog_n + 1)); continue ;; esac
	case " $KNOWN_UNGUARDED " in
		*" $fn "*) pinned_n=$((pinned_n + 1)) ;;
		*)         guarded_n=$((guarded_n + 1)) ;;
	esac
done
n_rel="$(printf '%s\n' $relfns | grep -c .)"
check_num "coverage: relation-taking == pinned + claimed-guarded + catalog-governed" \
	"$n_rel" "$(( pinned_n + guarded_n + catalog_n ))"
check_num "the catalog-governed class is the six from #560" "$catalog_n" "6"

# 3. The pinned count is exactly the size of the known class. This is the arm
#    that reddens when a fix lands, which is the entire purpose of the pins: the
#    engineer who closes one must come here and remove the name.
check_num "no relation-taking C entry point is still ungated" "$pinned_n" "0"

# ---- every claimed-guarded entry point names where its bar is asserted (#1386) ----
#
# "Claimed-guarded" was the else branch: every relation-taking name that is neither
# catalog-governed nor pinned. A new relation-taking function with no check at all
# landed there and nothing reddened (measured: zz_mutant_leak(regclass) left this
# suite 31/31). So the claim is now written down, name by name, with the suite whose
# arms call it as a caller the bar should stop. A name with no entry is red, and so
# is an entry whose suite does not exist or never calls the function, or that names
# a function this population no longer has.
BAR_ASSERTED_IN="
add_projection:vacuum_lock_privilege
drop_projection:vacuum_lock_privilege
vacuum:vacuum_lock_privilege
vacuum_sorted:vacuum_lock_privilege
cluster:vacuum_lock_privilege
recluster:vacuum_lock_privilege
compact:vacuum_lock_privilege
compact_rewrite:vacuum_lock_privilege
export_arrow:import_export_privilege
export_parquet:import_export_privilege
import_arrow:import_export_privilege
import_parquet:import_export_privilege
parallel_copy:import_export_privilege
parallel_export_parquet:import_export_privilege
expire:ttl_expire
column_extent:column_extent
maintenance_due:maintenance_due
refresh_rollup:rollup
rebase_rollup:rollup
rollup_source_groups:rollup
rollup_source_retired:rollup
tier_partition:tier_partition
partition_create_ahead:partition_lifecycle
"
# Relation-taking, with no bar to assert, each for its reason:
#   partition_drop_candidates, partition_drop_plan  return what pg_inherits and the
#     partition bounds already tell any role, and the plan is text: the statements
#     it lists run as whoever runs them, under core's own privilege checks.
NO_BAR_NEEDED="partition_drop_candidates partition_drop_plan"
bar_home() { awk -v f="$1" -F: '$1 == f { print $2 }' <<<"$BAR_ASSERTED_IN"; }
unhomed="" badhome="" stalehome=""
for fn in $relfns; do
	case " $CATALOG_GOVERNED $KNOWN_UNGUARDED $NO_BAR_NEEDED " in *" $fn "*) continue ;; esac
	# The behavioural arms below are this suite's own home for the names they call.
	case " $GUARDED_BEHAVIOURAL " in *" $fn "*) continue ;; esac
	home="$(bar_home "$fn")"
	if [ -z "$home" ]; then unhomed="$unhomed $fn"; continue; fi
	f="$(dirname "${BASH_SOURCE[0]}")/$home.sh"
	[ -r "$f" ] && grep -q "pgcolumnar\.$fn(" "$f" || badhome="$badhome $fn:$home"
done
for fn in $(awk -F: 'NF == 2 { print $1 }' <<<"$BAR_ASSERTED_IN") $NO_BAR_NEEDED; do
	case " $relfns " in *" $fn "*) ;; *) stalehome="$stalehome $fn" ;; esac
done
check "every claimed-guarded entry point names the suite that asserts its bar (#1386)" \
	"$([ -z "${unhomed// /}" ] && echo none || echo "$unhomed")" "none"
check "and each named suite exists and calls that entry point" \
	"$([ -z "${badhome// /}" ] && echo none || echo "$badhome")" "none"
check "and no entry names a function that is not a relation-taking entry point" \
	"$([ -z "${stalehome// /}" ] && echo none || echo "$stalehome")" "none"

# THE COUNT ABOVE IS NOT ENOUGH, AND THIS SUITE PROVED IT ON ITSELF.
#
# When #558 and #562 merged, four of the nine names here became genuinely
# guarded, and this file stayed FULLY GREEN. The arm counts names in a list I
# maintain, so it measures my intent rather than the world: a fix can land and
# the list can go stale with nothing to say so. That is the same "measure the
# work, never the intent" failure this suite exists to catch, built into the
# suite itself.
#
# The arms below are the correction. Each makes the call and reads the SQLSTATE,
# so a fix landing turns the relevant arm red on its own, whatever the list says.

# ---- the pins, which are assertions of the WRONG value ----------------------
#
# Each of these is a live defect on main. When its fix lands, the function stops
# being reachable without privilege, this arm goes RED, and whoever fixed it must
# remove the name from KNOWN_UNGUARDED. That is the intended behaviour and not a
# failure of this suite.
#
# The arm is behavioural, not a list lookup: it calls the function as a role with
# schema USAGE and nothing on the relation, and asserts the SQLSTATE. 42501 is
# produced only by aclcheck_error, so no unrelated error can satisfy it, and a
# function that is genuinely ungated returns something that is not 42501 at all.
psql_run "CREATE TABLE ep_target (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO ep_target SELECT g, 'v'||g FROM generate_series(1,10) g;"
psql_run "REVOKE ALL ON ep_target FROM PUBLIC;"
psql_run "DROP ROLE IF EXISTS t_ep;"
psql_run "CREATE ROLE t_ep NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_ep;"

as_ep() {  # as_ep <sql> -> the SQLSTATE, or the literal noerror
	#
	# VERBOSITY is set with -v, NOT with -c "\\set ...". psql treats a -c argument
	# beginning with a backslash as a meta-command and takes a different code
	# path; that is how a sibling suite ended up with deny arms that could never
	# go green. -v sets the same psql variable with no meta-command involved.
	#
	# The SQLSTATE is then read from the ERROR line rather than from a bare line
	# of its own, because psql prefixes it. The first version of this helper
	# required a line matching ^[0-9A-Z]{5}$ and therefore returned empty for
	# every real error, including one the server log showed it had raised.
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_ep -d "$PGC_DB" \
		-At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=0 -c "$1" 2>&1)"
	# Extract once into a variable rather than twice through a pipeline whose
	# STATUS is the answer: `... | grep -q .` reports "no sqlstate" whenever the
	# writer takes EPIPE, and this function's answer is a SQLSTATE.
	local _sqlstate
	_sqlstate="$(sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' <<<"$out" | head -1)"
	if [ -n "$_sqlstate" ]; then printf '%s\n' "$_sqlstate"; else echo noerror; fi
}

# The premise that makes every SQLSTATE arm below mean anything: this role can
# open a session. A deny arm is evidence only if the call reached the code that
# denies it, and a login FATAL carries no SQLSTATE at all.
check "premise: t_ep can open a session" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_ep -d "$PGC_DB" -At -c 'SELECT 1;' 2>&1 | head -1)" \
	"1"
check "premise: t_ep cannot read the table by any ordinary route" \
	"$(as_ep 'SELECT count(*) FROM ep_target;')" "42501"

# get_storage_id takes exactly one regclass and nothing else, so its call needs
# no argument vector to be invented. It is the pin this slice proves
# behaviourally; the remaining eight need their own valid argument vectors and
# are covered by the bucketing arm above until those are written (#569).
# A projection is needed before the projection entry points can be called at all,
# and the REACHED premise below is what makes each deny arm mean anything: the
# owner making the identical call must succeed, or a refusal is indistinguishable
# from a broken call.
psql_run "SELECT pgcolumnar.add_projection('ep_target','p1',ARRAY['id'],ARRAY['id']);"
psql_run "SELECT pgcolumnar.rebuild_projections('ep_target');"

call_for() {  # call_for <fn> -> a valid argument vector for that function
	case "$1" in
		read_projection|reconstruct_via_projection) echo "SELECT pgcolumnar.$1('ep_target','p1');" ;;
		vm_is_visible|vm_selftest)                  echo "SELECT pgcolumnar.$1('ep_target',0);" ;;
		get_storage_id|truncate|require_caller_select) echo "SELECT pgcolumnar.$1('ep_target');" ;;
		cluster_hilbert|recluster_hilbert)          echo "SELECT pgcolumnar.$1('ep_target', 'id');" ;;
		export_arrow|export_parquet|import_arrow|import_parquet)
			echo "SELECT pgcolumnar.$1('ep_target','/nonexistent/dir/x');" ;;
		*) echo "" ;;
	esac
}

# THE ROLE MUST CLEAR THE SQL LAYER FIRST, OR THIS PROVES NOTHING.
#
# #562's fix ships two layers: a REVOKE of EXECUTE from PUBLIC, and a C-level
# pg_class_aclcheck. Both raise 42501. A role holding only schema USAGE is
# stopped by the REVOKE, so the SQLSTATE is identical whether or not the C check
# exists, and deleting the C guard leaves this suite fully green.
#
# That is not hypothetical. It was measured on 2026-08-10: both guards were
# removed from src/columnar_projection.c, the extension rebuilt, and this file
# reported 22 of 22 passing.
#
# Granting EXECUTE puts the role PAST the SQL layer on purpose, so the only thing
# left that can refuse it is the C check. Now 42501 attributes to one layer.
#
# On every overload, by OID (#1387 review): a grant on a hard-coded signature, with
# its error discarded, left the role without EXECUTE when a declaration changed, and
# the premise below then reddened for the fixture's reason rather than the product's.
for fn in $GUARDED_BEHAVIOURAL; do
	while IFS= read -r sig; do
		[ -n "$sig" ] && psql_run "GRANT EXECUTE ON FUNCTION $sig TO t_ep;"
	done <<<"$(q "SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	              WHERE n.nspname = 'pgcolumnar' AND p.proname = '$fn';")"
done
check "premise: t_ep now holds EXECUTE, so a refusal below can only be the C check" \
	"$(q "SELECT bool_and(has_function_privilege('t_ep', p.oid, 'EXECUTE'))
	      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	      WHERE n.nspname='pgcolumnar' AND p.proname IN ('read_projection','reconstruct_via_projection','vm_is_visible','vm_selftest',
	                                   'cluster_hilbert','recluster_hilbert','truncate','require_caller_select');")" \
	"t"

for fn in $GUARDED_BEHAVIOURAL; do
	sql="$(call_for "$fn")"
	# A check, not a premise (#1236; #1387 review): a function refusing even its owner
	# is a product change. Read by psql's exit status: q ends in "|| true", so
	# "q ... && echo ok" printed ok whatever the call did.
	check "the owner can call $fn, so a refusal below is a refusal" \
		"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
			-X -At -v ON_ERROR_STOP=1 -c "$sql" >/dev/null 2>&1 && echo ok || echo broken)" "ok"
	check "$fn refuses an unprivileged caller with 42501" \
		"$(as_ep "$sql")" "42501"
done

# These four gate on a server-file role BEFORE any table check, so the role must
# hold both or it is refused by that outer layer and never reaches the bar under
# test. Removing this grant while restructuring is exactly how the arms below
# started reporting "must be superuser or a member of the pg_write_server_files
# role", which is a refusal by the wrong layer and proves nothing.
psql_run "GRANT pg_read_server_files, pg_write_server_files TO t_ep;"
check "premise: t_ep holds the server-file roles, so it reaches the table check" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_ep -d "$PGC_DB" -At \
		-c "SELECT pg_has_role('t_ep','pg_read_server_files','member');" 2>&1 | head -1)" "t"

# The four import/export siblings, now GUARDED by #559 (merge 819ce21).
#
# They gate on a server-file role before any table check, so the role is given
# both roles above. A path that cannot exist still discriminates: an unguarded
# function runs past the absent check and dies at the file layer, a guarded one
# refuses first. These arms asserted "reached-the-file-layer" until #559 landed,
# at which point they reddened and named themselves, which is what a pin is for.
for fn in export_arrow export_parquet import_arrow import_parquet; do
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_ep -d "$PGC_DB" -At \
		-c "SELECT pgcolumnar.$fn('ep_target','/nonexistent/dir/x');" 2>&1 | grep -m1 'ERROR:')"
	case "$out" in
		*"permission denied for table"*) state=refused ;;
		*"could not open file"*)         state=reached-the-file-layer ;;
		*)                               state="unexpected: $out" ;;
	esac
	check "$fn refuses a caller with no privilege before touching the file" \
		"$state" "refused"
done

# get_storage_id is covered by the GUARDED_BEHAVIOURAL loop above, which asserts
# 42501 from a caller holding nothing. It was the last member of the ungated
# class (#566) and landed with #559 in the same merge.

pgc_summary

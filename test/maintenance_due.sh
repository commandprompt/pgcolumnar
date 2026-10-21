#!/usr/bin/env bash
#
# pgColumnar #415: pgcolumnar.maintenance_due(rel) — the policy report a cron
# job or an operator consults to decide whether an online maintenance verb is
# worth running, as a pure function of table statistics (stats() + sort_status()).
# No daemon, no lock, no rewrite: it only reports.
#
# The thresholds are PARAMETERS with measured defaults (compact 0.2, recluster
# 0.05 — the knee and the smallest damaging decay measured on #415), not GUCs:
# the pgcolumnar GUC prefix is reserved (MarkGUCPrefixReserved), so an
# unregistered pgcolumnar.* GUC is rejected, and a report you call is better
# configured at the call site than globally.
#
# The load-bearing correctness point, asserted below: sort_status() reports a
# NEVER-ORDERED table as entirely "appended" (it has no sorted run), so a naive
# appended_groups > 0 gate would recommend reclustering a table that has no
# ordering to restore. maintenance_due gates recluster on a sorted run existing
# (sorted_groups > 0), which a never-ordered table does not have.
#
# Usage:  test/maintenance_due.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

CG=1000		# small chunk groups so several groups form per fixture

mk() {  # mk <table>  -- columnar table with small chunk groups
	psql_run "CREATE TABLE $1 (ts int, v int, pad text) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', chunk_group_row_limit => $CG);"
}
gen() {  # gen <table> <n>
	psql_run "INSERT INTO $1 SELECT g, g % 100, md5(g::text) FROM generate_series(1, $2) g;"
}

# A single scalar column out of maintenance_due for <table>, default thresholds.
md() {  # md <table> <column>
	q "SELECT $2 FROM pgcolumnar.maintenance_due('$1')"
}
# Same, with explicit thresholds.
mdp() {  # mdp <table> <compact_frac> <recl_frac> <column>
	q "SELECT $4 FROM pgcolumnar.maintenance_due('$1', $2, $3)"
}

# ---- fixture 1: clean, ordered, no deletes, no appends ----------------------
mk md_clean
gen md_clean 20000
psql_run "SELECT pgcolumnar.vacuum_sorted('md_clean', 'ts');"
check_num "md_clean has no deleted rows" \
	"$(q "SELECT COALESCE(sum(deletedrows),0) FROM pgcolumnar.stats('md_clean')")" "0"
check_num "md_clean is fully in its sorted run (0 appended groups)" \
	"$(q "SELECT appended_groups FROM pgcolumnar.sort_status('md_clean')")" "0"
check "md_clean has a sorted run" \
	"$(q "SELECT sorted_groups > 0 FROM pgcolumnar.sort_status('md_clean')")" "t"
check "clean: compact_rewrite not due" "$(md md_clean compact_rewrite_due)" "f"
check "clean: recluster not due"        "$(md md_clean recluster_due)"       "f"
check "clean: recommendation is null"   "$(md md_clean "recommendation IS NULL")" "t"

# ---- fixture 2: 40% deleted, never ordered ---------------------------------
mk md_deleted
gen md_deleted 20000
psql_run "DELETE FROM md_deleted WHERE (ts * 2654435761)::bigint % 100 < 40;"
psql_run "SELECT pgcolumnar.compact('md_deleted');"   # retire fully-dead (none: scattered)
# The fraction the DELETE's predicate removes, read by core alone from the
# generator: maintenance_due sums stats(), so an oracle taken from stats() moved
# with it, and a stats() that over-counted deleted rows by 10% passed (#1597
# review).
CORE_DF="$(q "SELECT round(avg((((g * 2654435761)::bigint % 100) < 40)::int), 2) FROM generate_series(1, 20000) g")"
check "premise: md_deleted deleted fraction ~0.40" "$CORE_DF" "0.40"
# check_text: with both readings failing, two empty readings agreed as "" == "".
check_text "delete: maintenance_due reports the same deleted_fraction" \
	"$(q "SELECT round(deleted_fraction::numeric,2) FROM pgcolumnar.maintenance_due('md_deleted')")" "$CORE_DF"
check "delete: compact_rewrite due (0.40 >= 0.2 default)" "$(md md_deleted compact_rewrite_due)" "t"
check "delete: recluster NOT due (table was never ordered)" "$(md md_deleted recluster_due)" "f"
check "delete: recommendation names compact_rewrite only" \
	"$(md md_deleted recommendation)" "compact_rewrite"
# the threshold is a live parameter, not a baked constant:
check "delete: raising the compact threshold above 0.40 makes it not due" \
	"$(mdp md_deleted 0.5 0.05 compact_rewrite_due)" "f"

# ---- fixture 3: ordered, then 25% appended (real clustering decay) ---------
mk md_appended
gen md_appended 20000
psql_run "SELECT pgcolumnar.vacuum_sorted('md_appended', 'ts');"
gen md_appended 5000     # appends outside the run
check "md_appended has appended groups" \
	"$(q "SELECT appended_groups > 0 FROM pgcolumnar.sort_status('md_appended')")" "t"
check "md_appended still has a sorted run" \
	"$(q "SELECT sorted_groups > 0 FROM pgcolumnar.sort_status('md_appended')")" "t"
check "appended: recluster due" "$(md md_appended recluster_due)" "t"
check "appended: compact_rewrite not due (no deletes)" "$(md md_appended compact_rewrite_due)" "f"
check "appended: recommendation names recluster only" "$(md md_appended recommendation)" "recluster"
check "appended: raising the recluster threshold above the fraction makes it not due" \
	"$(mdp md_appended 0.2 0.99 recluster_due)" "f"

# ---- fixture 4: NEVER ordered, two batches (the guard) ----------------------
# sort_status reports every group "appended" here because there is no run. A
# gate on appended_groups alone would wrongly recommend recluster; the
# sorted-run guard (sorted_groups > 0) must suppress it. The driver also proves
# this guard load-bearing by removal.
mk md_neverord
gen md_neverord 20000
gen md_neverord 5000
check "never-ordered table has NO sorted run" \
	"$(q "SELECT sorted_groups = 0 FROM pgcolumnar.sort_status('md_neverord')")" "t"
check "yet sort_status still counts appended groups (> 0)" \
	"$(q "SELECT appended_groups > 0 FROM pgcolumnar.sort_status('md_neverord')")" "t"
check "GUARD: recluster NOT due on a never-ordered table" \
	"$(md md_neverord recluster_due)" "f"
check "GUARD: recommendation is null on a never-ordered table" \
	"$(md md_neverord "recommendation IS NULL")" "t"

# ---- fixture 5: both due ----------------------------------------------------
mk md_both
gen md_both 20000
psql_run "SELECT pgcolumnar.vacuum_sorted('md_both', 'ts');"
psql_run "DELETE FROM md_both WHERE (ts * 2654435761)::bigint % 100 < 40;"
gen md_both 5000
check "both: compact_rewrite due" "$(md md_both compact_rewrite_due)" "t"
check "both: recluster due"        "$(md md_both recluster_due)"       "t"
check "both: recommendation names both verbs" \
	"$(md md_both recommendation)" "compact_rewrite, recluster"

# ---- privilege: a positive control AND a deny arm ---------------------------
# A deny arm alone is vacuous -- a function that refuses EVERYONE passes it,
# which is exactly how an invoker-rights bug (sort_status reads an internal
# catalog ordinary roles cannot) hid here. Test both, and assert the deny on
# SQLSTATE (42501), not a "permission denied" grep a login FATAL also matches.
sqlstate_as() {  # sqlstate_as <role> <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
$2;
SQLEOF
}
scalar_as() {  # scalar_as <role> <sql> -- value as <role>, empty on error
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -qtA -c "$2" 2>/dev/null | tail -1
}
psql_run "CREATE ROLE md_reader LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO md_reader;"
psql_run "GRANT SELECT ON md_clean TO md_reader;"   # SELECT on the table, nothing on the catalogs
psql_run "CREATE ROLE md_none LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO md_none;"
# Positive control: a SELECT-holder gets a verdict. Reds on invoker rights
# (42501 on pgcolumnar.row_group inside sort_status).
check "positive: a SELECT-holder gets a verdict, not a false deny" \
	"$(scalar_as md_reader "SELECT compact_rewrite_due FROM pgcolumnar.maintenance_due('md_clean')")" "f"
# Deny: a role without SELECT on the relation is refused, 42501 specifically.
check "deny: a role without SELECT on the relation is refused (42501)" \
	"$(sqlstate_as md_none "SELECT compact_rewrite_due FROM pgcolumnar.maintenance_due('md_clean')")" "42501"

pgc_summary

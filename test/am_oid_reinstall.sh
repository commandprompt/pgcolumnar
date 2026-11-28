#!/usr/bin/env bash
#
# A columnar table made after DROP EXTENSION + CREATE EXTENSION is recognised as
# columnar, by the session that ran the reinstall and by a session that looked
# the access method up before it (#2176).
#
# The library is preloaded and outlives the extension, so the reinstall gives the
# pgcolumnar access method a new oid in a backend that already holds the old one.
# PgColumnarIsColumnarRelation cached that oid in a static and never dropped it,
# and the index-only-scan planner hook kept a second copy. In such a backend every
# columnar table made after the reinstall looked like a heap: no custom scan,
# get_storage_id and vacuum_sorted refused with 42809, the RENAME COLUMN hook left
# the declared sort key on the old name, the DROP COLUMN hook left the ordering
# mark naming a dropped column, and pgcolumnar.enable_index_only_scan = off no
# longer kept index-only scans off it.
#
# Two scenarios, with the same checks:
#   self  -- one session warms the cache, reinstalls the extension, then uses a
#            new table.
#   other -- one session warms the cache, a second session reinstalls the
#            extension, then the first session uses a new table.
#
# Session S is one psql process reading one script, so every statement in it runs
# in one backend. The statements another session runs (the reinstall in "other",
# and the vacuum_sorted that sets the ordering mark) run through psql's \! in a
# psql of their own, in order, and with lock_timeout and statement_timeout so no
# step can wait unbounded. Each value S reports is one "key=value" line. Refusals
# come back as "ERROR:  SQLSTATE message" from a plpgsql wrapper, not as a psql
# failure.
#
# The pytest twin is test/pytest/test_am_oid_reinstall.py, same check names.
#
# Usage:  test/am_oid_reinstall.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

crashes() {
	# grep -c prints 0 itself when nothing matches, and then exits 1
	pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null || true
}
crashes_before="$(crashes)"

# The helpers live in public, which DROP EXTENSION does not touch.
#   aor_try(q) -- q's single value as text, NULL as 'NULL', or the error
#   aor_do(q)  -- 'ok' once q ran, or the error
#   aor_scan(q) -- the first plan line naming a relation, without indentation
psql_run "CREATE FUNCTION public.aor_try(q text) RETURNS text LANGUAGE plpgsql AS \$\$
DECLARE r text;
BEGIN
	EXECUTE q INTO r;
	RETURN coalesce(r, 'NULL');
EXCEPTION WHEN OTHERS THEN
	RETURN 'ERROR:  ' || SQLSTATE || ' ' || SQLERRM;
END \$\$;
CREATE FUNCTION public.aor_do(q text) RETURNS text LANGUAGE plpgsql AS \$\$
BEGIN
	EXECUTE q;
	RETURN 'ok';
EXCEPTION WHEN OTHERS THEN
	RETURN 'ERROR:  ' || SQLSTATE || ' ' || SQLERRM;
END \$\$;
CREATE FUNCTION public.aor_scan(q text) RETURNS text LANGUAGE plpgsql AS \$\$
DECLARE l text;
BEGIN
	FOR l IN EXECUTE 'EXPLAIN (COSTS OFF) ' || q LOOP
		IF l ~ ' on ' THEN
			RETURN btrim(regexp_replace(l, '^[ >-]*', ''));
		END IF;
	END LOOP;
	RETURN 'no scan';
END \$\$;"

# a second session, as \! runs it from inside S
OTHER="env PATH=$PGC_BINDIR:\$PATH PGOPTIONS='-c lock_timeout=30s -c statement_timeout=60s' psql -X -q -At -h 127.0.0.1 -p $PGC_PORT -U postgres -d $PGC_DB"

# Only the planner settings that leave an index scan as the one plan. The custom
# scan goes too, or it outbids the index scan and the IOS hook is never asked.
IOS_ON="SET pgcolumnar.enable_index_only_scan = off; SET pgcolumnar.enable_custom_scan = off;
SET enable_seqscan = off; SET enable_bitmapscan = off;"
IOS_OFF="RESET pgcolumnar.enable_index_only_scan; RESET pgcolumnar.enable_custom_scan;
RESET enable_seqscan; RESET enable_bitmapscan;"

# scenario TAG REINSTALL_SQL: run session S's script; its key=value lines land in
# $PGC_WORKDIR/TAG.out. TAG is also the new table's name.
scenario() {
	local t="$1" reinstall="$2" f="$PGC_WORKDIR/$1.sql"
	cat > "$f" <<SQL
SET lock_timeout = '30s';
SET statement_timeout = '60s';
CREATE TABLE ${t}_warm (a int) USING pgcolumnar;
CREATE INDEX ${t}_warm_a ON ${t}_warm (a);
SELECT 'warm=' || aor_try(\$q\$SELECT (pgcolumnar.get_storage_id('${t}_warm') IS NOT NULL)::text\$q\$);
$IOS_ON
SELECT 'iosw=' || aor_scan('SELECT a FROM ${t}_warm WHERE a = 1');
$IOS_OFF
SELECT 'old=' || oid FROM pg_am WHERE amname = 'pgcolumnar';
$reinstall
SELECT 'new=' || oid FROM pg_am WHERE amname = 'pgcolumnar';
CREATE TABLE ${t} (a int, b int, c int) USING pgcolumnar;
INSERT INTO ${t} SELECT g, 101 - g, g % 7 FROM generate_series(1, 100) g;
UPDATE ${t} SET b = b + 1000 WHERE a <= 10;
DELETE FROM ${t} WHERE a > 90;
CREATE INDEX ${t}_a ON ${t} (a);
SELECT 'rows=' || count(*) || '|' || sum(a) || '|' || sum(b) FROM ${t};
SELECT 'scan=' || aor_scan('SELECT b FROM ${t} WHERE b > 5');
SELECT 'sid=' || aor_try(\$q\$SELECT (pgcolumnar.get_storage_id('${t}') IS NOT NULL)::text\$q\$);
$IOS_ON
SELECT 'ios=' || aor_scan('SELECT a FROM ${t} WHERE a = 5');
$IOS_OFF
SELECT 'vs=' || aor_do(\$q\$SELECT pgcolumnar.vacuum_sorted('${t}', 'a')\$q\$);
SELECT pgcolumnar.set_options('${t}', sort_by => ARRAY['c']);
ALTER TABLE ${t} RENAME COLUMN c TO c2;
SELECT 'ren=' || coalesce(sort_by::text, 'NULL') FROM pgcolumnar.options WHERE regclass = '${t}'::regclass;
\\! $OTHER -c "SELECT pgcolumnar.vacuum_sorted('${t}', 'b')"
SELECT 'mark=' || coalesce(sorted_by::text, 'NULL') || '/' || coalesce(sorted_kind, 'NULL')
  FROM pgcolumnar.storage WHERE relation_oid = '${t}'::regclass;
ALTER TABLE ${t} DROP COLUMN b;
SELECT 'drop=' || coalesce(sorted_by::text, 'NULL') || '/' || coalesce(sorted_kind, 'NULL')
  FROM pgcolumnar.storage WHERE relation_oid = '${t}'::regclass;
SQL
	env PATH="$PGC_BINDIR:$PATH" psql -X -At -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -f "$f" > "$PGC_WORKDIR/$t.out" 2>&1
}
# val TAG KEY: the value S reported for KEY, empty when it reported none
val() { sed -n "s/^$2=//p" "$PGC_WORKDIR/$1.out" | head -1; }
changed() { local o n; o="$(val "$1" old)"; n="$(val "$1" new)"
	if [ -n "$o" ] && [ -n "$n" ] && [ "$o" != "$n" ]; then echo yes; else echo "no ($o -> $n)"; fi; }

scenario self "DROP EXTENSION pgcolumnar CASCADE;
CREATE EXTENSION pgcolumnar;"
scenario other "\\! $OTHER -c 'DROP EXTENSION pgcolumnar CASCADE' -c 'CREATE EXTENSION pgcolumnar'"

echo "-- self: the session that reinstalled"
check "premise: self -- the session looked the access method up before the reinstall" \
	"$(val self warm)" "true"
check "premise: self -- the session planned through the index-only-scan hook before the reinstall" \
	"$(val self iosw)" "Index Scan using self_warm_a on self_warm"
check "premise: self -- the reinstall gave the access method a new oid" "$(changed self)" "yes"
check "self: the new table's rows after INSERT, UPDATE and DELETE" "$(val self rows)" "90|4095|14995"
check "self: the new table is read by the columnar custom scan (#2176)" \
	"$(val self scan)" "Custom Scan (PgColumnarScan) on self"
check "self: get_storage_id recognises the new table (#2176)" "$(val self sid)" "true"
check "self: enable_index_only_scan = off keeps index-only scans off the new table (#2176)" \
	"$(val self ios)" "Index Scan using self_a on self"
check "self: vacuum_sorted accepts the new table (#2176)" "$(val self vs)" "ok"
check "self: RENAME COLUMN renames the declared sort key (#2176)" "$(val self ren)" "{c2}"
check "premise: self -- another session's vacuum_sorted set the ordering mark" \
	"$(val self mark)" "{b}/lexicographic"
check "self: DROP COLUMN clears the ordering mark (#2176)" "$(val self drop)" "NULL/NULL"

echo "-- other: a session that cached the oid before another session reinstalled"
check "premise: other -- the session looked the access method up before the reinstall" \
	"$(val other warm)" "true"
check "premise: other -- the session planned through the index-only-scan hook before the reinstall" \
	"$(val other iosw)" "Index Scan using other_warm_a on other_warm"
check "premise: other -- the reinstall gave the access method a new oid" "$(changed other)" "yes"
check "other: the new table's rows after INSERT, UPDATE and DELETE" "$(val other rows)" "90|4095|14995"
check "other: the new table is read by the columnar custom scan (#2176)" \
	"$(val other scan)" "Custom Scan (PgColumnarScan) on other"
check "other: get_storage_id recognises the new table (#2176)" "$(val other sid)" "true"
check "other: enable_index_only_scan = off keeps index-only scans off the new table (#2176)" \
	"$(val other ios)" "Index Scan using other_a on other"
check "other: vacuum_sorted accepts the new table (#2176)" "$(val other vs)" "ok"
check "other: RENAME COLUMN renames the declared sort key (#2176)" "$(val other ren)" "{c2}"
check "premise: other -- another session's vacuum_sorted set the ordering mark" \
	"$(val other mark)" "{b}/lexicographic"
check "other: DROP COLUMN clears the ordering mark (#2176)" "$(val other drop)" "NULL/NULL"

check "no backend terminated by signal during the scenarios" "$(( $(crashes) - crashes_before ))" "0"

pgc_summary

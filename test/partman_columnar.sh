#!/usr/bin/env bash
#
# pg_partman manages columnar partitions -- with one setting, and not without it (#1291).
#
# The owner's question on #1291 was whether pg_partman works with columnar
# partitions on every major; if it does, we document AND TEST that. Measured with
# pg_partman 5.4.1 on PG 15..19, and asserted here:
#
#   OUT OF THE BOX, every partition pg_partman makes is HEAP -- even on 17+, where
#   the parent itself carries USING pgcolumnar. pg_partman does not take the access
#   method from the parent, and switching its template table does not help either.
#
#   WITH default_table_access_method = pgcolumnar where its maintenance runs, every
#   partition it makes is columnar, the rows land there, and retention drops
#   partitions leaving no pgcolumnar catalog row behind.
#
#   run_maintenance holds ACCESS EXCLUSIVE on the parent. That is pg_partman's
#   behaviour, not ours; it is asserted because docs/how-to.md tells users so, and a
#   pg_partman that changed it should turn this red rather than leave the doc wrong.
#
# The first check pins the trap rather than the recipe, on purpose: it is the reason
# the documentation exists, and a pg_partman that started honouring the parent's
# access method should be noticed, not silently make the warning stale.
#
# CHECKS NO CHANGE TO THIS REPOSITORY CAN REDDEN ARE PREMISES (the owner's rule,
# #1236). Which access method a partition gets, whether pg_partman makes a DEFAULT
# partition, and which lock its maintenance holds are core's and pg_partman's
# behaviour. They are still asserted -- a pg_partman release that changes one
# turns it red, and the document with it -- but as premises, not as arms of this
# repository. The arms are what pgcolumnar decides: what a scan returns, and what
# the drop hook leaves in the catalogs.
#
# pg_partman is an external extension. A box without it FAILS this suite (lib.sh's
# rule for every capability but pyarrow); set PGC_ALLOW_MISSING_PG_PARTMAN=1 to make
# that a named skip. CI installs postgresql-N-partman from PGDG.
#
# Usage:  test/partman_columnar.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
if [ "$(q "SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_partman';")" != 1 ]; then
	pgc_skip pg_partman "pg_partman is not installed for this major"
fi
q "CREATE SCHEMA partman; CREATE EXTENSION pg_partman SCHEMA partman;" >/dev/null
check_text "premise: pg_partman is installed in this database" \
	"$(q "SELECT count(*) FROM pg_extension WHERE extname = 'pg_partman';")" "1"
echo "-- pg_partman $(q "SELECT extversion FROM pg_extension WHERE extname = 'pg_partman';")"

# am_counts PARENT -> "am=n am=n" over the partitions pg_partman made
am_counts() {
	q "SELECT coalesce(string_agg(amname || '=' || n, ' ' ORDER BY amname), 'none')
	   FROM (SELECT a.amname, count(*) n FROM pg_inherits i
	           JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_am a ON a.oid = c.relam
	          WHERE i.inhparent = '$1'::regclass GROUP BY 1) x;"
}
# On 17+ a partitioned parent can carry USING; give it to them where it can be
# given, so the first arm shows pg_partman ignores even that.
using=""; [ "$PGC_MAJOR" -ge 17 ] && using="USING pgcolumnar"

# ---- 1. the trap: no setting, heap partitions -------------------------------
q "CREATE TABLE plain_m (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts) $using;" >/dev/null
q "SELECT partman.create_parent('public.plain_m', 'ts', '1 day', p_premake := 2,
     p_start_partition := (date_trunc('day', now()) - interval '2 days')::text);" >/dev/null
check "premise: without default_table_access_method pg_partman makes heap partitions" \
	"$(am_counts plain_m | sed -E 's/=[0-9]+//g')" "heap"

# ---- 2. the recipe -----------------------------------------------------------
q "ALTER DATABASE \"$PGC_DB\" SET default_table_access_method = 'pgcolumnar';" >/dev/null
q "CREATE TABLE ts_m (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts) $using;" >/dev/null
q "SELECT partman.create_parent('public.ts_m', 'ts', '1 day', p_premake := 3,
     p_start_partition := (date_trunc('day', now()) - interval '40 days')::text);" >/dev/null
check_num "premise: pg_partman creates a DEFAULT partition, as docs/how-to.md says" \
	"$(q "SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
	      WHERE i.inhparent = 'ts_m'::regclass
	        AND pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT';")" "1"
check "premise: with default_table_access_method every partition pg_partman makes is columnar" \
	"$(am_counts ts_m)" "pgcolumnar=$(q "SELECT count(*) FROM pg_inherits WHERE inhparent = 'ts_m'::regclass;")"

q "CREATE TABLE ts_h (ts timestamptz, v int) USING heap;" >/dev/null
for t in ts_m ts_h; do
	q "INSERT INTO $t SELECT date_trunc('day', now()) - interval '40 days' + g * interval '17 min', g
	   FROM generate_series(1, 3000) g;" >/dev/null
done
check_num "the rows went in" "$(q "SELECT count(*) FROM ts_m;")" "3000"
check "and the rows land in columnar partitions" \
	"$(q "SELECT string_agg(DISTINCT a.amname, ',') FROM ts_m t
	      JOIN pg_class c ON c.oid = t.tableoid JOIN pg_am a ON a.oid = c.relam;")" "pgcolumnar"
# Every row, in order, hashed: an unfiltered count/sum/min/max is answered from
# zone maps and would not exercise the scan at all.
check "and read back as heap does" \
	"$(q "SELECT md5(string_agg(ts::text || ':' || v, ',' ORDER BY ts, v)) FROM ts_m;")" \
	"$(q "SELECT md5(string_agg(ts::text || ':' || v, ',' ORDER BY ts, v)) FROM ts_h;")"

# ---- 3. maintenance: ahead, retention, and the parent lock ------------------
q "UPDATE partman.part_config SET premake = 6, retention = '20 days',
     retention_keep_table = false WHERE parent_table = 'public.ts_m';" >/dev/null
st_before="$(q "SELECT count(*) FROM pgcolumnar.storage;")"
kids_before="$(q "SELECT count(*) FROM pg_inherits WHERE inhparent = 'ts_m'::regclass;")"
lock="$(q "BEGIN; SELECT partman.run_maintenance('public.ts_m');
	SELECT coalesce(string_agg(DISTINCT mode, ','), 'none') FROM pg_locks
	 WHERE relation = 'ts_m'::regclass AND pid = pg_backend_pid(); COMMIT;" | grep -vE '^(BEGIN|COMMIT)$|^$' | tail -1)"
kids_after="$(q "SELECT count(*) FROM pg_inherits WHERE inhparent = 'ts_m'::regclass;")"
echo "-- partitions $kids_before -> $kids_after, storage rows $st_before -> $(q "SELECT count(*) FROM pgcolumnar.storage;")"
dropped=$((kids_before - kids_after))
check "premise: retention dropped partitions" \
	"$([ "$dropped" -ge 1 ] && echo dropped || echo "dropped $dropped")" "dropped"
check "premise: the partitions maintenance creates ahead are columnar too" \
	"$(am_counts ts_m)" "pgcolumnar=$kids_after"
check_num "retention leaves no pgcolumnar storage row behind" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage s
	      WHERE NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = s.relation_oid);")" "0"
check_num "and no row-group row behind" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group r
	      WHERE NOT EXISTS (SELECT 1 FROM pgcolumnar.storage s WHERE s.storage_id = r.storage_id);")" "0"
check "premise: run_maintenance holds an exclusive lock on the parent, as docs/how-to.md says" \
	"$lock" "AccessExclusiveLock"

pgc_summary

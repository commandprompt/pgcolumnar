#!/usr/bin/env bash
#
# Twelve metadata scans must not cost more because the database holds other
# columnar tables (#1207).
#
# THE BUG. Twelve systable_beginscan calls in columnar_metadata.c passed
# InvalidOid on a key that an existing index starts with, so each one read its
# whole catalog to answer a question about one table:
#
#     free_space              storage_id   :AllocateFreeSpace, :TrailingFreeSpaceSafe,
#                                          :DeleteFreeSpaceAtOrAbove
#     row_group               storage_id,  row_group_exists -- EVERY DELETE
#                             group_number
#     storage                 storage_id   three in InsertNativeStorageRow,
#                                          one in SetSortedExtent
#     projection_declaration  rel          list, rename column, drop one, drop all
#
# The pgcolumnar catalogs are SHARED by every columnar table in the database,
# so each of those reads was charged for every other table's rows. Measured on
# a database of 300 projected columnar tables, a one-row DELETE read 35 row_group
# buffers; through the index, 11.
#
# THE FIX ROUTES EACH ONE THROUGH pgcolumnar_scan_index_oid(), which probes only
# when the catalog is at least pgcolumnar.index_min_blocks pages (#1213, #1217).
#
# WHAT IS ASSERTED, AND WHY IT IS A PATH AND NOT A COST. catalog_delete_index.sh
# explains why that suite asserts buffers and never seq_scan: a build that
# DECLINES a probe on a small catalog is correct, and a path assertion fires on
# it. That objection holds only where declining can be right. Here every arm
# runs after 400 other projected tables have put each target catalog AT OR ABOVE
# the threshold, and a premise asserts it against the live setting. On such a
# catalog the resolver never declines, so a sequential scan of it during the
# operation is one of these sites still reading whole -- the defect, and
# nothing else.
#
# TWO COST MEASURES WERE TRIED FIRST, AND A REVERT SURVIVED BOTH.
#
#   default against index_min_blocks = huge. A revert passed four arms of
#   nine. The setting also governs the scans converted BEFORE this change
#   (#1213, #1237), so forcing it made those read whole too, and the forced
#   reading was worse than the unfixed code's. It measured the setting.
#
#   growth from an empty database to the populated one. A probe costs about
#   three buffers where a one-page catalog costs one, and vacuum_sorted makes
#   28 storage scans, so the FIXED code grew by more than the pages added.
#   Separating the two needs catalogs near a hundred pages.
#
# The buffers are printed beside each arm, as diagnosis.
#
# Usage:  test/catalog_scan_index.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# pgcolumnar.parallel_copy commits its loaders by two-phase commit and runs them
# as background workers; both settings are PGC_POSTMASTER (parallel_copy.sh).
export PGC_EXTRA_CONF=$'max_prepared_transactions=8\nmax_worker_processes=16'
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"



# at_least GOT FLOOR -- GOT when it falls short of FLOOR, else FLOOR. An arm
# comparing this against FLOOR passes exactly when GOT >= FLOOR, and a failing
# one reports the number it measured rather than a bare 0 (#1164, selftest
# part 540). Defined above the first recorder call; catalog_plan_index.sh says
# why the placement matters.
at_least() { [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
pages_of() { q "SELECT pg_relation_size('pgcolumnar.$1') / 8192;"; }
min_blocks="$(q "SHOW pgcolumnar.index_min_blocks;")"
echo "-- pgcolumnar.index_min_blocks=$(q "SHOW pgcolumnar.index_min_blocks;")  (the shipped default this run measures)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# make_target TABLE NAME -- one measured table, for the arm called NAME: seven 1000-row groups and a
# projection over both columns, shaped so every operation below has work to do:
#
#     group 0   retired               a free range, so the rewrite can reuse one
#     group 5   every other row dead  a group compact_rewrite will write
#     group 6   retired               a TRAILING free range, so truncate() has a
#                                     tail to remove
#
# With TARGET_PROJ=0 the projection is left off, for the end-truncation arm
# alone: a projection is its own storage IN THE SAME FILE, written after the
# base's groups, so its live data sits behind group 6 and there is no tail to
# reclaim. Measured: with the projection truncate() returns 0 and both free
# ranges stay; without it, 3 blocks and one.
TARGET_PROJ=1
make_target() {
	q "CREATE TABLE $1 (a int, b text) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('$1', stripe_row_limit => 1000);
	   INSERT INTO $1 SELECT g, md5(g::text) FROM generate_series(1,7000) g;
	   DELETE FROM $1 WHERE a <= 1000 OR a > 6000;" >/dev/null
	[ "$TARGET_PROJ" = 1 ] &&
		q "SELECT pgcolumnar.add_projection('$1', 'p', '{a,b}', '{a}');" >/dev/null
	# ITS OWN STATEMENT, AND THAT IS LOAD-BEARING. One q is one psql -c, which
	# runs as one transaction, and compact() cannot retire a group whose rows
	# were deleted by a transaction that has not committed. In one q with the
	# DELETE it retired nothing and every free_space arm measured an empty
	# table's free list: the premises below are what caught it.
	q "SELECT pgcolumnar.compact('$1');" >/dev/null
	q "DELETE FROM $1 WHERE a BETWEEN 5001 AND 6000 AND a % 2 = 0;" >/dev/null
	check_num "premise: the $2 target holds two free ranges, one of them trailing" \
		"$(q "SELECT count(*) FROM pgcolumnar.free_space WHERE storage_id = pgcolumnar.get_storage_id('$1');")" "2"
}

# work CATALOG MIN_BLOCKS SQL -- "buffers seq_scan idx_scan" for CATALOG while
# SQL ran with index_min_blocks at MIN_BLOCKS. Buffers are heap AND index. SQL may hold several statements
# separated by ";;".
#
# ONE SESSION, ONE -c PER STATEMENT, and both halves matter. The SET has to be
# in the session that runs the statement. But one -c holding several statements
# is ONE implicit transaction, and pgcolumnar.truncate() refuses to run inside a
# transaction block -- it raised, q() discarded the error, and the arm read
# zero buffers for an operation that never ran. Separate -c arguments run in
# one session as separate transactions.
work() {
	local -a cmds=(-c "SET pgcolumnar.index_min_blocks = $2")
	local rest="$3" stmt
	while [ -n "$rest" ]; do
		stmt="${rest%%;;*}"
		cmds+=(-c "$stmt")
		[ "$stmt" = "$rest" ] && rest="" || rest="${rest#*;;}"
	done
	q "SELECT pg_stat_reset();" >/dev/null
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At "${cmds[@]}" >/dev/null 2>&1
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT (coalesce(io.heap_blks_read + io.heap_blks_hit
			+ coalesce(io.idx_blks_read,0) + coalesce(io.idx_blks_hit,0), 0))::text
			|| ' ' || coalesce(st.seq_scan,0)::text || ' ' || coalesce(st.idx_scan,0)::text
		FROM pg_statio_all_tables io JOIN pg_stat_all_tables st USING (relid)
		WHERE io.schemaname = 'pgcolumnar' AND io.relname = '$1';"
}

# arm NAME CATALOG SQL EFFECT WANT -- run SQL (@T is the table) on a fresh
# target and assert it did not read CATALOG whole.
#
# EFFECT IS A PREMISE, AND IT IS NOT OPTIONAL. q() discards errors, so a
# statement that failed outright still leaves a reading behind, and "no
# sequential scan" is exactly what a statement that never ran reports. Both
# kinds of silent failure happened while this file was written (a compaction
# that could not retire, a truncate() that refused a transaction block), and
# this premise and the next are what caught each.
N_ARM=0
N_MEASURED=0	# arms whose catalog statistics came back as numbers
arm() {
	local name="$1" cat="$2" tmpl="$3" effect="$4" want="$5"
	N_ARM=$((N_ARM + 1))
	local t="arm${N_ARM}" r bufs seq idx
	make_target "$t" "$name"
	r="$(work "$cat" "$min_blocks" "${tmpl//@T/$t}")"
	read -r bufs seq idx <<<"$r"
	pgc_is_number "$seq" && pgc_is_number "$idx" && N_MEASURED=$((N_MEASURED + 1))
	echo "-- $name: pgcolumnar.$cat buffers=$bufs seq_scan=$seq idx_scan=$idx"
	# Not premises (#1339): each operation, and the catalog access it makes, is
	# pgcolumnar's own, so a change to it reddens these -- with compact_rewrite a no-op,
	# "an online rewrite took effect" went red.
	check_text "$name took effect" "$(q "${effect//@T/$t}")" "$want"
	check_num "$name reached pgcolumnar.$cat" \
		"$(at_least "$((${seq:-0} + ${idx:-0}))" 1)" "1"
	check_num "$name does not read pgcolumnar.$cat whole" "${seq:-x}" "0"
}

# ---------------------------------------------------------------------------
# The population: other columnar tables in the same catalogs
# ---------------------------------------------------------------------------
#
# Each noise table holds four 1000-row groups, a projection, and two retired
# groups that are NOT adjacent (0 and 2), so each leaves two free_space rows
# rather than one coalesced one. That is what puts every target catalog above
# the threshold: a contiguous delete coalesces, and a scattered one retires
# nothing and leaves free_space empty -- the fixture that hid this catalog on
# #1207.
NOISE=400
G="$PGC_WORKDIR/noise.sql"
: > "$G"
for i in $(seq 1 $NOISE); do
	cat >> "$G" <<SQL
CREATE TABLE noise_$i (a int, b text) USING pgcolumnar;
SELECT pgcolumnar.set_options('noise_$i', stripe_row_limit => 1000);
INSERT INTO noise_$i SELECT g, md5(g::text) FROM generate_series(1,4000) g;
SELECT pgcolumnar.add_projection('noise_$i', 'p', '{a}', '{a}');
DELETE FROM noise_$i WHERE (a - 1) / 1000 IN (0, 2);
SELECT pgcolumnar.compact('noise_$i');
SQL
done
psql_file "$G" >/dev/null

# EACH PREMISE IS THE CONDITION ITS ARMS NEED. A catalog still below the
# threshold in the big database is read whole by design (#1213), it grows by
# its pages, and the arm would fail for a reason that says nothing about the
# code.
for c in storage row_group free_space projection_declaration; do
	p="$(pages_of "$c")"
	echo "-- pgcolumnar.$c: $p pages"
	check_num "premise: pgcolumnar.$c is at least index_min_blocks pages" \
		"$(at_least "$p" "$min_blocks")" "$min_blocks"
done

# ---------------------------------------------------------------------------
# The arms, one per operation that reaches a converted site
# ---------------------------------------------------------------------------

# 7000 rows, minus groups 0 and 6, minus half of group 5: 4500 live.
arm "an insert into an existing table" storage \
	"INSERT INTO @T SELECT g, 'x' FROM generate_series(7001,7100) g;" \
	"SELECT count(*) FROM @T;" "4600"
# THE SAME FUNCTION, TWO MORE OF ITS THREE SCANS. A table created in the same
# transaction takes the unlocked existence check (:2115 on be31874); a
# parallel_copy loader, which sets pgcolumnar_bulk_parallel_writer, takes the
# one before it (:2077). Each is its own arm because each is its own scan.
arm "an insert into a table created in the same transaction" storage \
	"BEGIN; CREATE TABLE @T_new (a int, b text) USING pgcolumnar; INSERT INTO @T_new SELECT g, 'x' FROM generate_series(1,100) g; COMMIT;" \
	"SELECT count(*) FROM @T_new;" "100"
PCDIR="$PGC_WORKDIR/pcopy"
mkdir -p "$PCDIR"; chmod 777 "$PCDIR"
if [ "$(id -u)" = "0" ]; then chown postgres "$PCDIR"; fi
PCF="$PCDIR/load.txt"
q "COPY (SELECT g, 'p' || g FROM generate_series(10001,12000) g) TO '$PCF' WITH (FORMAT text);" >/dev/null
arm "a parallel bulk load" storage \
	"SELECT pgcolumnar.parallel_copy('@T', '$PCF', 2);" \
	"SELECT count(*) FROM @T;" "6500"
arm "a one-row delete" row_group \
	"DELETE FROM @T WHERE a = 1500;" \
	"SELECT count(*) FROM @T;" "4499"
arm "an online rewrite" free_space \
	"SELECT pgcolumnar.compact_rewrite('@T', 0.2, 0);" \
	"SELECT sum(deletedrows) FROM pgcolumnar.stats('@T');" "0"
# Two free ranges before (groups 0 and 6); the trailing one is removed.
TARGET_PROJ=0
arm "an end truncation" free_space \
	"SET pgcolumnar.enable_end_truncation = on;;SELECT pgcolumnar.truncate('@T');" \
	"SELECT count(*) FROM pgcolumnar.free_space WHERE storage_id = pgcolumnar.get_storage_id('@T');" "1"
TARGET_PROJ=1
arm "a sorted rewrite" storage \
	"SELECT pgcolumnar.vacuum_sorted('@T', 'a');" \
	"SELECT sorted_through IS NOT NULL FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('@T');" "t"
arm "renaming a projected column" projection_declaration \
	"ALTER TABLE @T RENAME COLUMN b TO bb;" \
	"SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = '@T'::regclass AND columns::text LIKE '%bb%';" "1"
arm "truncating a projected table" projection_declaration \
	"TRUNCATE @T;" \
	"SELECT count(*) FROM @T;" "0"
arm "dropping a projection" projection_declaration \
	"SELECT pgcolumnar.drop_projection('@T', 'p');" \
	"SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = '@T'::regclass;" "0"
arm "dropping a projected table" projection_declaration \
	"DROP TABLE @T;" \
	"SELECT to_regclass('@T') IS NULL;" "t"

# The arms are eleven calls of one function; a refactor that dropped one would
# leave every remaining arm green.
# Arms MEASURED, not arms called (#1339): N_ARM counted calls, so an arm whose
# statistics never came back still made eleven.
check_num "premise: eleven operations are measured" "$N_MEASURED" "11"

pgc_summary

#!/usr/bin/env bash
#
# pg_dump / restore round-trip for a columnar table: the basic "can I get my
# data back out" guarantee, which nothing else in the suite covered. Builds a
# USING pgcolumnar table with data, a per-table option, and an index; then
# exercises BOTH dump paths and restores each into a fresh database (restoring
# over the live one collides on the extension's own schema):
#
#   - plain SQL       (pg_dump -f ...   -> psql -f)
#   - custom archive  (pg_dump -Fc ...  -> pg_restore), the format most real
#                     backups use, which drives a different restore code path
#
# For each it asserts the row count, a content checksum, the access method, and
# that the surviving index actually answers a point lookup (not merely that a
# catalog row exists). Restore success is judged by the client's EXIT CODE
# (ON_ERROR_STOP / pg_restore --exit-on-error), not by grepping for the word
# "ERROR", which is localised.
#
# Per-table state lives in extension-owned catalogs, which pg_dump only emits when
# the extension registers them:
#   - options (pgcolumnar.options, regclass-keyed) IS registered (#248/#258) and
#     is asserted to survive below.
#   - projections (pgcolumnar.projection, storage_id-keyed) cannot be carried that
#     way -- restoring the rows would point at storage that no longer exists -- and
#     nothing re-emits add_projection() at dump time yet, so they are lost. The
#     check below pins that loss (#266): it goes red the day a fix lands, exactly
#     as the options check did before #258.
#
set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

DUMP_SQL="$PGC_WORKDIR/rt.sql"
DUMP_FC="$PGC_WORKDIR/rt.dump"
run() { env PATH="$PGC_BINDIR:$PATH" "$@"; }
on() { run psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$1" -At -c "$2"; }

# --- build: data + an option + an index ------------------------------------
psql_run "CREATE TABLE dt (id bigint, kind int, payload text) USING pgcolumnar;" >/dev/null
psql_run "SELECT pgcolumnar.set_options('dt', encode_effort => 'fast');" >/dev/null
psql_run "INSERT INTO dt SELECT g, g % 7, 'p'||g FROM generate_series(1,50000) g;" >/dev/null
psql_run "CREATE INDEX dt_id_idx ON dt (id);" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('dt', 'dt_proj', ARRAY['id','payload'], ARRAY['id']);" >/dev/null

# #288: a table whose declared sort_by names columns that sit AFTER a dropped
# column, so a plain pg_dump renumbers their attnums on restore (the dropped
# column is not re-emitted). sort_by stores column NAMES, so it survives and
# still resolves; a stored attnum would silently sort the restored table on the
# wrong column. Verified in verify() for both dump formats.
psql_run "CREATE TABLE ds (id bigint, doomed int, host text, ts timestamptz) USING pgcolumnar;" >/dev/null
psql_run "INSERT INTO ds (id, host, ts) SELECT g, 'h'||(g%20), '2024-01-01'::timestamptz + (g%100)*interval '1 minute' FROM generate_series(1,20000) g;" >/dev/null
psql_run "ALTER TABLE ds DROP COLUMN doomed;" >/dev/null
psql_run "SELECT pgcolumnar.set_options('ds', sort_by => ARRAY['host','ts']);" >/dev/null

sum_sql="SELECT coalesce(sum(hashtextextended(id::text||'|'||kind::text||'|'||payload, 0)), 0) FROM dt"
am_sql="SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam WHERE c.relname = 'dt'"
# A scalar subquery, so the answer is defined whether or not a row exists. The
# plain form returned zero rows after a restore -- pg_dump does not emit the
# options row at all -- and coalesce cannot rescue a missing row, so the result
# was empty rather than '<none>' and the pinned assertion below read as a
# different bug than the one it is pinning.
opt_sql="SELECT coalesce((SELECT encode_effort FROM pgcolumnar.options WHERE regclass = 'dt'::regclass), '<none>')"
# Force the planner off seqscan so the point lookup exercises the restored index;
# the returned value proves both the data and the index survived and work.
idx_sql="SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT id FROM dt WHERE id = 12345"
# Declared projections for this table, keyed by its (restore-regenerated) storage id.
proj_sql="SELECT count(*) FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('dt') AND projection_id > 0"

before_count="$(q "SELECT count(*) FROM dt;")"
before_sum="$(q "$sum_sql;")"
before_opt="$(q "$opt_sql;")"
before_proj="$(q "$proj_sql;")"
check "the base table has a projection to preserve" "$before_proj" "1"

decl_sql="SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'dt'::regclass"
before_decl="$(q "$decl_sql;")"
check "and a declaration recorded for it" "$before_decl" "1"

# Dropping a projection must forget its declaration as well. If it did not, a
# later rebuild would resurrect a projection the operator deliberately removed,
# which is a worse failure than the one #266 fixes.
psql_run "SELECT pgcolumnar.add_projection('dt', 'dt_tmp', ARRAY['id'], ARRAY['id']);" >/dev/null 2>&1
check "a second projection is declared" \
	"$(q "$decl_sql;")" "2"
psql_run "SELECT pgcolumnar.drop_projection('dt', 'dt_tmp');" >/dev/null 2>&1
check "and dropping it forgets the declaration, so a rebuild cannot resurrect it" \
	"$(q "$decl_sql;")" "1"
check "a rebuild now builds nothing" \
	"$(q "SELECT pgcolumnar.rebuild_projections();")" "0"

verify() {  # label db
	local label="$1" db="$2" ao
	check "$label: row count survives"           "$(on "$db" "SELECT count(*) FROM dt;")" "$before_count"
	check "$label: row data survives (checksum)" "$(on "$db" "$sum_sql;")" "$before_sum"
	check "$label: access method is pgcolumnar"  "$(on "$db" "$am_sql;")" "pgcolumnar"
	# tail -1: the two SET statements each emit a command tag before the result.
	# The plan premise is what makes this arm about the INDEX (#1341): the columnar
	# scan is not disabled by enable_seqscan, and with the index dropped from the
	# restored database the same lookup still answered 12345.
	check "$label: premise: the point lookup takes the restored index" \
		"$(on "$db" "SET enable_seqscan=off; SET enable_bitmapscan=off; EXPLAIN (COSTS off) SELECT id FROM dt WHERE id = 12345;" | grep -c 'Index.* using dt_id_idx')" "1"
	check "$label: restored index answers a point lookup" "$(on "$db" "$idx_sql;" | tail -1)" "12345"
	# Per-table options survive the round trip (#248). They did not until the
	# extension registered pgcolumnar.options with pg_extension_config_dump:
	# rows in an extension's own tables are not dumped unless the extension asks,
	# so a restored table silently reverted to default limits, compression and
	# encode_effort.
	#
	# This check was pinned to the broken behaviour while #248 was open, which is
	# what turned it red the moment the fix landed and forced this line to be
	# updated deliberately rather than a green run hiding the change.
	check "$label: per-table options survive" \
		"$(on "$db" "$opt_sql;")" "$before_opt"
	# The projection STORAGE still cannot be carried: pgcolumnar.projection is
	# keyed by a storage id that a restore reassigns, so restoring those rows
	# would point at storage that does not exist. That half of #266 is unchanged
	# and is still pinned here.
	check "$label: projection storage is NOT carried by pg_dump (see #266)" \
		"$(on "$db" "$proj_sql;")" "0"
	# #288: the declared sort_by (stored as NAMES) survives even though ds had a
	# dropped column that renumbered attnums on restore, and the zero-arg apply
	# resolves the restored names and runs. A stored attnum would have pointed at
	# the wrong or a missing column here.
	check "$label: declared sort_by survives restore" \
		"$(on "$db" "SELECT sort_by::text FROM pgcolumnar.options WHERE regclass='ds'::regclass;")" "{host,ts}"
	# APPLIES means the rows end up in (host, ts) order, not merely that the call
	# returns (#1341): the row count alone is 20000 whichever columns it sorted on.
	# Inversions are counted in scan order, with a premise that there are some
	# before the call, so a no-op cannot pass.
	inversions="SELECT count(*) FROM (SELECT host, ts, lag(host) OVER () AS lh, lag(ts) OVER () AS lt FROM ds) x WHERE (lh, lt) > (host, ts)"
	check "$label: premise: the restored ds is not yet in its sort_by order" \
		"$(on "$db" "SELECT ($inversions) > 0;")" "t"
	check "$label: restored sort_by resolves and applies" \
		"$(on "$db" "SELECT pgcolumnar.vacuum_sorted('ds'); SELECT count(*) || '|' || ($inversions) FROM ds;" 2>/dev/null | tail -1)" "20000|0"

	# The DECLARATION is carried, which is the half #266 fixed. It is keyed by
	# regclass and stores column names, so config_dump can restore it, and the
	# intent survives even though the data does not.
	check "$label: the projection declaration IS carried" \
		"$(on "$db" "$decl_sql;")" "$before_decl"

	# And it is recoverable rather than merely recorded. Without this the
	# declaration would be a note about something the operator still cannot get
	# back, which is not a fix.
	check "$label: rebuild_projections() reports it rebuilt one" \
		"$(on "$db" "SELECT pgcolumnar.rebuild_projections();")" "1"
	check "$label: and the projection exists again after the rebuild" \
		"$(on "$db" "$proj_sql;")" "$before_proj"
	check "$label: and it reads the same rows as the base table" \
		"$(on "$db" "SELECT count(*) FROM pgcolumnar.read_projection('dt','dt_proj');")" \
		"$(on "$db" "SELECT count(*) FROM dt;")"
	# Running it twice must build nothing, or a routine re-run would duplicate work.
	check "$label: a second rebuild is a no-op" \
		"$(on "$db" "SELECT pgcolumnar.rebuild_projections();")" "0"
	on postgres "DROP DATABASE IF EXISTS $db;" >/dev/null 2>&1
}

freshdb() { on postgres "DROP DATABASE IF EXISTS $1;" >/dev/null 2>&1; on postgres "CREATE DATABASE $1;" >/dev/null; }

# --- plain SQL format ------------------------------------------------------
run pg_dump -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -f "$DUMP_SQL"; rc=$?
check "plain: pg_dump exited 0" "$rc" "0"
check "plain: dump recreates the table on the columnar AM" \
	"$(grep -cE "USING pgcolumnar|default_table_access_method = pgcolumnar" "$DUMP_SQL" | awk '{print ($1>0)?"yes":"no"}')" "yes"
freshdb "${PGC_DB}_p"
run psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "${PGC_DB}_p" -v ON_ERROR_STOP=1 -q -f "$DUMP_SQL" >/dev/null 2>&1; rc=$?
check "plain: restore exited 0 (ON_ERROR_STOP)" "$rc" "0"
verify plain "${PGC_DB}_p"

# --- custom archive format -------------------------------------------------
run pg_dump -Fc -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -f "$DUMP_FC"; rc=$?
check "custom: pg_dump -Fc exited 0" "$rc" "0"
freshdb "${PGC_DB}_c"
run pg_restore --exit-on-error -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "${PGC_DB}_c" "$DUMP_FC" >/dev/null 2>&1; rc=$?
check "custom: pg_restore exited 0 (--exit-on-error)" "$rc" "0"
verify custom "${PGC_DB}_c"

pgc_summary

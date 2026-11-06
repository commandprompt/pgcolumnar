#!/usr/bin/env bash
#
# docs/configuration.md is the reference for every pgcolumnar.* setting: its name,
# type, default, and the one condition a reader must know before setting it
# (needs superuser, settable only at server start, reloadable). This suite holds
# the document to the server, both ways, so a setting added, renamed, re-defaulted
# or moved to another context without the reference following it turns red
# (#1341 item 42). It also sets the two settings no test had touched: a role that
# is not a superuser is refused enable_end_truncation (item 57), and
# compression_level reaches what zstd stores (item 83).
#
# Usage:  test/settings_reference.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

DOC="$PGC_SRCDIR/docs/configuration.md"

# doc_rows -> "name|default|condition" for each settings-table row, where condition
# is what the row's text says: superuser, postmaster (only at server start),
# sighup (reloadable), or user.
doc_rows() {
	awk -F' [|] ' '/^[|] `pgcolumnar\./ {
		name = $1; sub(/^[|] `/, "", name); sub(/`.*/, "", name)
		def = $3; gsub(/`/, "", def); sub(/ *\(empty\)/, "", def); if (def == "'"''"'") def = ""
		desc = $0; cond = "user"
		if (desc ~ /[Ss]uperuser/) cond = "superuser"
		else if (desc ~ /only at server start/) cond = "postmaster"
		else if (desc ~ /Reloadable/) cond = "sighup"
		print name "|" def "|" cond
	}' "$DOC" | LC_ALL=C sort -t'|' -k1,1
}
# server_rows [public] -> the same shape from pg_settings. With "public", a setting
# the server marks NOT_IN_SAMPLE is left out: those are developer hooks and internal
# switches (rollup_pause, tier_fault_after, bulk_parallel_writer), which the reference
# may document but need not.
server_rows() {
	local only=""
	[ "${1:-}" = public ] && only="AND NOT ('NOT_IN_SAMPLE' = ANY (pg_settings_get_flags(name)))"
	q "SELECT name || '|' || boot_val || '|' || context FROM pg_settings
	   WHERE name LIKE 'pgcolumnar.%' $only ORDER BY name COLLATE \"C\";"
}
listed() {	# stdin lines -> one space-separated line, or "none" when there are none
	local x; x="$(tr '\n' ' ' | sed 's/ *$//')"; echo "${x:-none}"
}
# comm compares WHOLE lines, so each side of a two-field comparison is sorted again
# after its cut: rows sorted on the name alone put `autovacuum|` after
# `autovacuum_compact_threshold|` in C order, and comm then pairs the wrong lines
# and names settings that are correct (OffgridwithJD, review of #1414).
docd="$(doc_rows)"
srv="$(server_rows)"
pub="$(server_rows public)"
# the server's rows for the settings the reference documents, to compare field by field
srv_docd="$(LC_ALL=C join -t'|' -o 2.1,2.2,2.3 <(cut -d'|' -f1 <<<"$docd") <(cat <<<"$srv"))"

_ndoc="$(grep -c . <<<"$docd")"
check "the reference documents at least thirty settings, so the comparisons below read something" \
	"$([ "$_ndoc" -ge 30 ] && echo yes || echo "no ($_ndoc rows parsed from $DOC)")" "yes"
check "every setting the server defines is documented, the developer hooks aside" \
	"$(LC_ALL=C comm -13 <(cut -d'|' -f1 <<<"$docd") <(cut -d'|' -f1 <<<"$pub") | listed)" "none"
check "every documented setting exists on the server" \
	"$(LC_ALL=C comm -23 <(cut -d'|' -f1 <<<"$docd") <(cut -d'|' -f1 <<<"$srv") | listed)" "none"
check "each documented default is the server's" \
	"$(LC_ALL=C comm -3 <(cut -d'|' -f1,2 <<<"$docd" | LC_ALL=C sort) <(cut -d'|' -f1,2 <<<"$srv_docd" | LC_ALL=C sort) | tr -d '\t' | listed)" "none"
check "each documented condition is the server's context for the setting" \
	"$(LC_ALL=C comm -3 <(cut -d'|' -f1,3 <<<"$docd" | LC_ALL=C sort) <(cut -d'|' -f1,3 <<<"$srv_docd" | LC_ALL=C sort) | tr -d '\t' | listed)" "none"

# ---- enable_end_truncation needs superuser (item 57) -------------------------------
psql_run "DROP ROLE IF EXISTS sr_plain; CREATE ROLE sr_plain LOGIN NOSUPERUSER;"
as_plain() {	# as_plain SQL -> ok, the SQLSTATE it fails with, or noconnect
	local out rc st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U sr_plain -d "$PGC_DB" \
		-X -qtA -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)"
	rc=$?
	st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then echo "$st"; elif [ "$rc" = 0 ]; then echo ok; else echo noconnect; fi
}
check "a role that is not a superuser cannot set enable_end_truncation (42501)" \
	"$(as_plain "SET pgcolumnar.enable_end_truncation = on")" "42501"
check "and a superuser can" \
	"$(q "SET pgcolumnar.enable_end_truncation = on; SHOW pgcolumnar.enable_end_truncation;" | tail -1)" "on"

# ---- compression_level reaches what zstd stores (item 83) ---------------------------
# The same rows written at level 1 and at level 19: zstd's higher level must store
# less. A build without zstd cannot run this.
if [ "$(q "SELECT 'zstd' = ANY (enumvals) FROM pg_settings WHERE name = 'pgcolumnar.compression'")" != t ]; then
	pgc_skip zstd "this build has no zstd codec"
fi
for lvl in 1 19; do
	psql_run "SET pgcolumnar.compression = zstd; SET pgcolumnar.compression_level = $lvl;
	          CREATE TABLE sr_l$lvl (t text) USING pgcolumnar;
	          INSERT INTO sr_l$lvl SELECT string_agg(md5((g * 7 + k)::text), ' ')
	            FROM generate_series(1, 20000) g, generate_series(1, 3) k GROUP BY g;"
done
check "compression_level reaches the zstd codec: level 19 stores less than level 1" \
	"$(q "SELECT CASE WHEN pg_relation_size('sr_l19') < pg_relation_size('sr_l1') THEN 'less'
	              ELSE pg_relation_size('sr_l19') || ' vs ' || pg_relation_size('sr_l1') END")" "less"

pgc_summary

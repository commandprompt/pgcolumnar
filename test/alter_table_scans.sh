#!/usr/bin/env bash
#
# ALTER TABLE forms that read every existing row through the access method (#1341):
# SET NOT NULL, ADD PRIMARY KEY, VALIDATE CONSTRAINT, and the two that rewrite the
# table, ALTER COLUMN TYPE and ADD COLUMN with a volatile default. No test ran them.
#
# A columnar table keeps a deleted row in its file until compaction, so the question
# each form has to answer is the same: does it see the live rows, and only those?
# Each form runs on a columnar table and on heap with the same rows. First a row
# violates it, and both must refuse it with the same SQLSTATE. Then that row is
# deleted, and both must accept it: a deleted NULL, duplicate or negative value
# that the scan still saw would refuse it. Each rewrite must keep the live rows and
# leave the deleted ones gone.
#
# Usage:  test/alter_table_scans.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

outcome() {	# outcome SQL -> ok, or the SQLSTATE the statement failed with
	local out st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -qtA -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)"
	st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out")"
	echo "${st:-ok}"
}
both() {	# both SQL -> "<columnar outcome>|<heap outcome>", %t standing for the table
	echo "$(outcome "${1//%t/at_pgcolumnar}")|$(outcome "${1//%t/at_heap}")"
}
answers() {	# answers SQL -> "<columnar answer>|<heap answer>", %t standing for the table
	echo "$(q "${1//%t/at_pgcolumnar}" | tail -1)|$(q "${1//%t/at_heap}" | tail -1)"
}

# 3000 ordinary rows; then a NULL in v, a second id 1, and a negative v.
for am in pgcolumnar heap; do
	psql_run "CREATE TABLE at_$am (id int, v int, w text) USING $am;
	          INSERT INTO at_$am SELECT g, g, 'x' FROM generate_series(1, 3000) g;
	          INSERT INTO at_$am VALUES (5000, NULL, 'null'), (1, -1, 'dup');"
done

# ---- each form refuses the row that violates it ----------------------------------
check "SET NOT NULL is refused over a NULL (23502), as on heap" \
	"$(both "ALTER TABLE %t ALTER COLUMN v SET NOT NULL")" "23502|23502"
check "ADD PRIMARY KEY is refused over a duplicate (23505), as on heap" \
	"$(both "ALTER TABLE %t ADD PRIMARY KEY (id)")" "23505|23505"
check "a CHECK added NOT VALID is accepted, as on heap" \
	"$(both "ALTER TABLE %t ADD CONSTRAINT v_pos CHECK (v > 0) NOT VALID")" "ok|ok"
check "and VALIDATE CONSTRAINT is refused over the row that breaks it (23514), as on heap" \
	"$(both "ALTER TABLE %t VALIDATE CONSTRAINT v_pos")" "23514|23514"

# ---- once those rows are deleted, each form accepts --------------------------------
psql_run "DELETE FROM at_pgcolumnar WHERE v IS NULL OR v < 0; DELETE FROM at_heap WHERE v IS NULL OR v < 0;"
check "the deleted rows are gone from both tables, which hold the same rows" \
	"$(answers "SELECT count(*) || '/' || sum(v) FROM %t")" "3000/4501500|3000/4501500"
check "SET NOT NULL succeeds once the NULL row is deleted, as on heap" \
	"$(both "ALTER TABLE %t ALTER COLUMN v SET NOT NULL")" "ok|ok"
check "ADD PRIMARY KEY succeeds once the duplicate is deleted, as on heap" \
	"$(both "ALTER TABLE %t ADD PRIMARY KEY (id)")" "ok|ok"
check "VALIDATE CONSTRAINT succeeds once the breaking row is deleted, as on heap" \
	"$(both "ALTER TABLE %t VALIDATE CONSTRAINT v_pos")" "ok|ok"
check "and the constraint is recorded as validated" \
	"$(answers "SELECT convalidated FROM pg_constraint WHERE conrelid = '%t'::regclass AND conname = 'v_pos'")" "t|t"

# ---- the two rewrites keep the live rows and only those ----------------------------
check "ALTER COLUMN TYPE rewrites the table, as on heap" \
	"$(both "ALTER TABLE %t ALTER COLUMN v TYPE bigint")" "ok|ok"
check "and the rewrite kept the live rows and not the deleted ones" \
	"$(answers "SELECT count(*) || '/' || sum(v) || '/' || (SELECT format_type(atttypid, NULL) FROM pg_attribute WHERE attrelid = '%t'::regclass AND attname = 'v') FROM %t")" "3000/4501500/bigint|3000/4501500/bigint"
check "ADD COLUMN with a volatile default rewrites the table, as on heap" \
	"$(both "ALTER TABLE %t ADD COLUMN z float8 DEFAULT random()")" "ok|ok"
check "and that rewrite gave every live row a value and brought back no deleted one" \
	"$(answers "SELECT count(*) || '/' || count(z) || '/' || count(*) FILTER (WHERE v IS NULL OR v < 0) FROM %t")" \
	"3000/3000/0|3000/3000/0"
IDX="SET enable_seqscan = off; SET enable_bitmapscan = off;"
check "the lookup of id 1 is an index scan on the primary key" \
	"$(for t in at_pgcolumnar at_heap; do q "$IDX EXPLAIN (COSTS OFF) SELECT count(*) FROM $t WHERE id = 1" | grep -c "Index.* Scan using ${t}_pkey"; done | paste -sd'|')" "1|1"
check "the primary key still finds id 1 once, through the index, after both rewrites" \
	"$(answers "$IDX SELECT count(*) || '/' || min(w) FROM %t WHERE id = 1")" "1/x|1/x"

pgc_summary

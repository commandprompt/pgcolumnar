#!/usr/bin/env bash
#
# pgColumnar temporal-constraint coverage.
#
# PostgreSQL 18 adds WITHOUT OVERLAPS primary keys/unique constraints; 19beta2 had
# UPDATE ... FOR PORTION OF, which 19beta4 removed (#1337). Both run through the index and constraint machinery
# pgColumnar integrates with, so a columnar table must enforce them exactly as a
# heap table does. This suite builds a heap/columnar pair with a WITHOUT OVERLAPS
# primary key and asserts identical behavior: non-overlapping rows accepted,
# overlapping rows rejected, and (where the grammar has it) a FOR PORTION OF update
# produces the same result set. WITHOUT OVERLAPS needs a GiST index over the scalar key part,
# so btree_gist must be available; the suite skips with a note if it is not.
#
# btree_gist is CONTRIB. A PGDG package ships it with postgresql-N, so CI has it. A
# source build configured without contrib does not, and this suite then asserts nothing.
# It reported PASSED while doing so until #447, and the local five-major matrix was
# claiming temporal coverage on every major while running it on one.
#
# Since #448 a missing btree_gist FAILS rather than skips, because the box cannot gate
# this suite and a pass would claim coverage nobody has. Build and install
# contrib/btree_gist against each source tree, or set PGC_ALLOW_MISSING_BTREE_GIST=1 to
# run knowingly without it, which reports SKIPPED and keeps the loss visible.
#
# A skip on 15, 16 and 17 is different and is correct: WITHOUT OVERLAPS does not exist
# there, so there is nothing to gate.
#
# Usage:  test/temporal.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

major="$("$PGC_PG_CONFIG" --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"

if [ "$major" -lt 18 ]; then
	echo "-- temporal constraints: skipped (PostgreSQL < 18)"
	pgc_summary
	exit 0
fi

if ! psql_run "CREATE EXTENSION IF NOT EXISTS btree_gist;" >/dev/null 2>&1; then
	pgc_skip btree_gist "btree_gist not available; temporal constraints need it"
fi

# expect_ok / expect_err run the same statement against heap and columnar and
# assert both agree (both succeed, or both reject).
both() {
	local label="$1" want="$2" sql_h="$3" sql_c="$4" rh rc
	if psql_run "$sql_h" >/dev/null 2>&1; then rh=ok; else rh=err; fi
	if psql_run "$sql_c" >/dev/null 2>&1; then rc=ok; else rc=err; fi
	check "$label (heap)" "$rh" "$want"
	check "$label (columnar)" "$rc" "$want"
}

echo "-- WITHOUT OVERLAPS primary key"
psql_run "CREATE TABLE tt_heap (id int, valid daterange,
          PRIMARY KEY (id, valid WITHOUT OVERLAPS)) USING heap;"
psql_run "CREATE TABLE tt_col (id int, valid daterange,
          PRIMARY KEY (id, valid WITHOUT OVERLAPS)) USING pgcolumnar;"

both "non-overlapping insert accepted" ok \
	"INSERT INTO tt_heap VALUES (1,'[2020-01-01,2020-06-01)'),(1,'[2020-06-01,2021-01-01)'),(2,'[2020-01-01,2021-01-01)');" \
	"INSERT INTO tt_col  VALUES (1,'[2020-01-01,2020-06-01)'),(1,'[2020-06-01,2021-01-01)'),(2,'[2020-01-01,2021-01-01)');"

both "overlapping insert rejected" err \
	"INSERT INTO tt_heap VALUES (1,'[2020-03-01,2020-09-01)');" \
	"INSERT INTO tt_col  VALUES (1,'[2020-03-01,2020-09-01)');"

th="$(pgc_set_hash "SELECT id, valid FROM tt_heap")"
tc="$(pgc_set_hash "SELECT id, valid FROM tt_col")"
check "temporal PK contents match" "$tc" "$th"

# --- FOR PORTION OF -----------------------------------------------------------
# Gated on the server's GRAMMAR, not its major (#1337). 19beta2 had UPDATE ... FOR
# PORTION OF; 19beta4 removed it (no "portion" keyword in its kwlist.h). A version
# gate ran these arms on a server that cannot parse them and failed both storage
# types alike. The probe parses the clause and plans nothing that writes.
fpo_probe="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate \
	-c "EXPLAIN UPDATE tt_heap FOR PORTION OF valid FROM '2020-03-01' TO '2020-05-01' SET id = id WHERE false;" 2>&1 \
	| sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1)"
if [ -z "$fpo_probe" ]; then
	echo "-- UPDATE ... FOR PORTION OF"
	# Spelled out rather than through both(), so these arms are named literally and
	# the skip loop below can be held to exactly them (#994's skip-loop-arms.py).
	if psql_run "UPDATE tt_heap FOR PORTION OF valid FROM '2020-03-01' TO '2020-05-01' SET id = 100 WHERE id = 2;" >/dev/null 2>&1; then rh=ok; else rh=err; fi
	if psql_run "UPDATE tt_col  FOR PORTION OF valid FROM '2020-03-01' TO '2020-05-01' SET id = 100 WHERE id = 2;" >/dev/null 2>&1; then rc=ok; else rc=err; fi
	check "for portion of update applied (heap)" "$rh" "ok"
	check "for portion of update applied (columnar)" "$rc" "ok"

	# The tables agreeing is not enough: two updates that both failed leave them
	# agreeing too, and this arm passed that way on 19beta4 (#1337). The portion must
	# also be visible, under its new key, in the columnar table.
	ph="$(pgc_set_hash "SELECT id, valid FROM tt_heap")"
	pc="$(pgc_set_hash "SELECT id, valid FROM tt_col")"
	check "for portion of result matches" \
		"$pc|$(q "SELECT coalesce(string_agg(valid::text, ',' ORDER BY valid), 'none') FROM tt_col WHERE id = 100;")" \
		"$ph|[2020-03-01,2020-05-01)"
else
	for a in "for portion of update applied (heap)" "for portion of update applied (columnar)" \
	         "for portion of result matches"; do
		check_skip "$a" "SKIP  $a (this server's grammar has no UPDATE ... FOR PORTION OF: $fpo_probe)" \
			"this server's grammar has no UPDATE ... FOR PORTION OF ($fpo_probe)"
	done
fi

pgc_summary

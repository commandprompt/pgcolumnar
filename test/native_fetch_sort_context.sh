#!/usr/bin/env bash
#
# pgColumnar deferred-decode must not pfree in the caller's memory context (#720).
#
# A columnar index scan returns a DEFERRED slot: the row's address is stored and
# its columns are decoded lazily, when something asks for them. One asker is a
# sort. A merge join sorts its input, and tuplesort materialises each slot with
# copy_minimal_tuple while ITS OWN memory context is current. On PostgreSQL 17
# that context is a bump context (the bump allocator is new in 17), and a bump
# context raises "pfree is not supported by the bump memory allocator" on any
# pfree.
#
# The deferred decode pfreed there twice: the Bitmapset of needed columns (built
# with bms_add_member, freed with bms_free), and, on a storage's first fetch, the
# format-version check's catalog scan, which frees a btree search stack. Either
# aborts a plain query a planner can choose on its own -- a Merge Join over a Sort
# over an Index Scan.
#
# The fix routes both through a pfree-supporting context; the decoded values are
# unaffected because they are copied into the caller's context, where palloc is
# legal. This suite drives that plan and asserts the answer matches a heap mirror.
# It reddens on PostgreSQL 17+ before the fix (the query errors, so no value is
# returned and the check fails against the numeric oracle) and is a no-op guard on
# 15/16, which have no bump allocator and never erred.
#
# The reproducing shape is a two-key merge condition whose keys are a leading
# by-VALUE column and a by-REFERENCE column (matching the issue's variants): the
# leading int becomes tuplesort's datum1 while the by-reference key forces the
# tuple to be materialised through the deferred slot. A single sort key is the
# control that already worked and must keep working.
#
# Usage:  test/native_fetch_sort_context.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Force the plan the bug needs: Merge Join over Sort over Index Scan, with the
# columnar custom scan off so the fetch goes through the table-AM index path.
FORCE="SET enable_seqscan=off; SET enable_bitmapscan=off;
       SET pgcolumnar.enable_custom_scan=off;
       SET enable_nestloop=off; SET enable_hashjoin=off;"

psql_run "DROP TABLE IF EXISTS f; DROP TABLE IF EXISTS h;
          SET pgcolumnar.stripe_row_limit=2000;
          CREATE TABLE f (id int, v text) USING pgcolumnar;
          INSERT INTO f SELECT g, 'v'||g FROM generate_series(1,100000) g;
          CREATE INDEX f_id ON f (id);
          CREATE TABLE h (id int, v text);
          INSERT INTO h SELECT g, 'v'||g FROM generate_series(1,2000) g;"

# Scalar value under a given SQL. The forced GUCs and the query share one session
# (the GUCs must apply to the query), so psql echoes a "SET" tag per GUC ahead of
# the value; the value is the last line. On error psql prints no value line, so
# the last line is a "SET" tag, which fails the numeric check rather than crashing
# the suite -- an errored arm reads as a red, which is what the bug does on 17+.
jv() { q "$FORCE $1" | tail -1; }

# premise: the plan really is a Merge Join over a Sort over an Index Scan on f,
# or the regression is not being exercised.
PLAN="$(q "$FORCE EXPLAIN (COSTS OFF) SELECT count(*) FROM h JOIN f ON f.id=h.id AND f.v=h.v;")"
check "premise: plan is a merge join" \
	"$(grep -qi 'Merge Join' <<<"$PLAN" && echo yes || echo no)" yes
# The Sort is what the bug needs: tuplesort materialises the deferred slot in its own
# (bump, on 17+) context. Without a Sort directly over the f_id index scan the path
# under test never runs, and the #720 arms below pass on a reverted fix: with an
# index on (id, v) the merge join reads pre-ordered rows and needs no Sort, and the
# old grep (any "Index Scan using f_id") passed it, 7/7 with the fix half-reverted.
# So a Sort (an Incremental Sort on 18+, where the index already orders id) must sit
# directly over THIS index (f_id, matched whole) on f.
check "premise: the columnar side is an index scan under a sort" \
	"$(n=$(grep -A3 -E '^[[:space:]]*->[[:space:]]+(Incremental )?Sort$' <<<"$PLAN" |
		grep -cE 'Index Scan using f_id on f( |$)'); [ "${n:-0}" -gt 0 ] && echo yes || echo no)" yes

# The regression: a two-key merge cond, leading by-value int + by-reference text.
# All 2000 rows of h match f, so the count is 2000 and the id-sum is 2001000.
check "two-key merge join count is right (#720)" \
	"$(jv "SELECT count(*) FROM h JOIN f ON f.id=h.id AND f.v=h.v;")" "2000"
check "two-key merge join sum(id) is right (#720)" \
	"$(jv "SELECT sum(f.id) FROM h JOIN f ON f.id=h.id AND f.v=h.v;")" "2001000"

# a third key and a residual filter, to widen the decoded prefix past two columns.
check "three-key merge cond with a filter (#720)" \
	"$(jv "SELECT count(*) FROM h JOIN f ON f.id=h.id AND f.v=h.v AND f.id<1500;")" "1499"

# controls that already worked before the fix (a single sort key never defers a
# by-reference second key through the sort): they must stay correct, so the fix
# did not disturb the working paths.
check "control: single int key still right" \
	"$(jv "SELECT count(*) FROM h JOIN f ON f.id=h.id;")" "2000"
check "control: single text key still right" \
	"$(jv "SELECT count(*) FROM h JOIN f ON f.v=h.v;")" "2000"

pgc_summary

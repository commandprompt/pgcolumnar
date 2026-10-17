#!/usr/bin/env bash
#
# pgColumnar: writing a TOASTED varlena is correct (#445 detoast-once).
#
# The writer flattens each varlena value once per row and reuses the flat Datum
# for the encoder, the bloom hash and the min/max comparisons, rather than
# letting each of those detoast the value independently (for a compressed value,
# each is a full decompression -- ~11% of a large-text load, #445's write-path
# detoast_attr). This suite guards the CORRECTNESS of that flatten-once path,
# which the other write suites do not reach because they use short, non-toasted
# values. The perf win is a timing, not a check here; the removal proof for the
# optimisation is the load time, recorded on the PR.
#
# The source is a HEAP table, so its wide values are stored pglz-compressed and
# reading it back hands the columnar writer already-toasted varlenas -- the exact
# input the flatten-once path exists for. The heap mirror is the oracle.
#
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# big is ~3.2 kB and compressible, so heap stores it toasted (pglz). small is a
# non-toasted varlena, so both branches of the flatten (copy vs no-copy) run.
# pglz named, not defaulted (#1341): PG19's default_toast_compression is lz4, and the
# premise below read [lz4] there
psql_run "CREATE TABLE src (k int, big text COMPRESSION pglz, small text);"
psql_run "INSERT INTO src SELECT g, repeat(md5((g % 777)::text), 100) || chr(65 + (g % 26)), (g % 50)::text FROM generate_series(1, 60000) g;"
psql_run "CREATE TABLE h (k int, big text, small text);"
psql_run "CREATE TABLE c (k int, big text, small text) USING pgcolumnar;"
# six row groups (#1341): under the default limit all 60000 rows were one group, so
# no zone map or bloom could prune a group at all
psql_run "SELECT pgcolumnar.set_options('c', stripe_row_limit => 10000);"
psql_run "INSERT INTO h SELECT * FROM src;"
psql_run "INSERT INTO c SELECT * FROM src;"

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atqc "$1" 2>/dev/null
}

# If this ever reads non-pglz, the fixture stopped toasting and the suite is no
# longer exercising the path it exists for.
check_text "premise: the source values are toasted (pglz)" \
	"$(q "SELECT pg_column_compression(big) FROM src LIMIT 1")" "pglz"

# The value bytes must survive the flatten-once path exactly.
check_text "toasted values are byte-identical to the heap mirror" \
	"$(q "SELECT md5(string_agg(big, '|' ORDER BY k)) FROM c")" \
	"$(q "SELECT md5(string_agg(big, '|' ORDER BY k)) FROM h")"

# min/max is built from the flattened value; a range prune must match heap. The
# range selects rows (#1341): every value starts with a hex digit, so the old
# "big > repeat('z', 10)" selected none, and a zone map that pruned every group
# still matched heap's 0.
check "premise: each range selects some rows and not all" \
	"$(q "SELECT bool_and(n > 0 AND n < 60000) FROM (SELECT count(*) FILTER (WHERE big >= 'e') AS n FROM h UNION ALL SELECT count(*) FILTER (WHERE big < '1') FROM h) s")" "t"
check_num "the min/max zone map is correct (range count matches heap)" \
	"$(q "SELECT count(*) FROM c WHERE big >= 'e'")" \
	"$(q "SELECT count(*) FROM h WHERE big >= 'e'")"
# an upper bound prunes by the minimum, which the range above never asks; below
# a typical first value, so a minimum left too high skips matching groups (#1521 review)
check_num "the min/max zone map is correct below a bound (range count matches heap)" \
	"$(q "SELECT count(*) FROM c WHERE big < '1'")" \
	"$(q "SELECT count(*) FROM h WHERE big < '1'")"

# the bloom hash is taken from the flattened value; equality must match heap. The
# probe is the value itself (#1341): compared with a scalar subquery, the scan saw an
# InitPlan parameter, pushed no skip predicate and never asked the bloom.
BIGV="$(q "SELECT big FROM src WHERE k = 12345")"
check "the equality probe reaches the bloom (a usable skip predicate)" \
	"$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM c WHERE big = '$BIGV'" | grep -c 'Columnar Usable Skip Predicates: 1')" "1"
check_num "equality on a toasted value (bloom + recheck) matches heap" \
	"$(q "SELECT count(*) FROM c WHERE big = '$BIGV'")" \
	"$(q "SELECT count(*) FROM h WHERE big = '$BIGV'")"

# the short non-toasted column must be unaffected.
check_text "the non-toasted column is byte-identical too" \
	"$(q "SELECT md5(string_agg(small, '|' ORDER BY k)) FROM c")" \
	"$(q "SELECT md5(string_agg(small, '|' ORDER BY k)) FROM h")"

pgc_summary

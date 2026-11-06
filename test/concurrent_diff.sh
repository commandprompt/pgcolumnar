#!/usr/bin/env bash
#
# pgColumnar concurrent-DML differential suite.
#
# Several backends concurrently run the same INSERT/DELETE/UPDATE workload,
# partitioned into disjoint id ranges, against both a heap table and a columnar
# table. Because the ranges are disjoint the final logical state is deterministic
# regardless of interleaving, so the columnar table must end byte-for-byte equal
# to the heap oracle. This exercises concurrent writers to one columnar relation
# (stripe/row-number reservation, the delete vector, and index maintenance) with the
# format 2.1 encoding and bloom-filter write paths active.
#
# Usage:  test/concurrent_diff.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

make_pair "id int, cat int, v bigint, txt text, price float8"
q "SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 500, stripe_row_limit => 2000);" >/dev/null

WORKERS=6
PER=3000

echo "-- launching $WORKERS concurrent workers"
pids=()
for w in $(seq 0 $((WORKERS - 1))); do
	lo=$((w * PER + 1))
	hi=$(((w + 1) * PER))
	sql="
		INSERT INTO t_heap SELECT g, g%5, g*3, md5(g::text), ((g*37)%100000)*0.01 FROM generate_series($lo,$hi) g;
		INSERT INTO t_col  SELECT g, g%5, g*3, md5(g::text), ((g*37)%100000)*0.01 FROM generate_series($lo,$hi) g;
		DELETE FROM t_heap WHERE id BETWEEN $lo AND $hi AND id % 7 = 0;
		DELETE FROM t_col  WHERE id BETWEEN $lo AND $hi AND id % 7 = 0;
		UPDATE t_heap SET v = v + 1 WHERE id BETWEEN $lo AND $hi AND id % 5 = 0;
		UPDATE t_col  SET v = v + 1 WHERE id BETWEEN $lo AND $hi AND id % 5 = 0;"
	(
		date +%s%N > "$PGC_WORKDIR/worker.$w.start"
		env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -v ON_ERROR_STOP=1 -q -c "$sql" \
			>/dev/null 2>>"$PGC_WORKDIR/worker.$w.err"
		rc=$?
		date +%s%N > "$PGC_WORKDIR/worker.$w.end"
		exit $rc
	) &
	pids+=($!)
done

fail_workers=0
for pid in "${pids[@]}"; do
	wait "$pid" || fail_workers=1
done
check "all workers succeeded" "$fail_workers" "0"

# THE WORKERS MUST OVERLAP, or this is a serial differential with extra steps. Every
# worker starts before any finishes: the latest start precedes the earliest end
# (measured on PG17: all six inside the first 6 ms, the first done at about 90 ms).
overlap="$(cat "$PGC_WORKDIR"/worker.*.start "$PGC_WORKDIR"/worker.*.end 2>/dev/null | wc -l)"
if [ "$overlap" != "$((2 * WORKERS))" ]; then
	overlap="no timing for every worker ($overlap of $((2 * WORKERS)) stamps)"
else
	overlap="$(awk 'FNR==1 && FILENAME ~ /start$/ { if ($1 > ls) ls = $1 }
		FNR==1 && FILENAME ~ /end$/ { if (ee == "" || $1 < ee) ee = $1 }
		END { print (ls < ee) ? "yes" : "no (the last start came after the first end)" }' \
		"$PGC_WORKDIR"/worker.*.start "$PGC_WORKDIR"/worker.*.end)"
fi
check "premise: every worker started before any worker finished" "$overlap" "yes"

# The columnar table must match the heap oracle exactly after concurrent DML.
# The whole-row and price checks cover the ALP-encoded decimal column flushed
# under concurrent writers.
diff_query "concurrent whole-row" "SELECT * FROM %T"
diff_query "concurrent count/sum" "SELECT count(*), sum(v), count(distinct cat) FROM %T"
# min/max/count are order-independent; a float sum is not (addition is not
# associative and the scan orders differ), so it is not compared here. The
# whole-row check above already proves every price value round-trips exactly.
diff_query "concurrent price agg" "SELECT min(price), max(price), count(price) FROM %T"
diff_query "concurrent bloom eq"  "SELECT count(*) FROM %T WHERE v = 900"
diff_query "concurrent range"     "SELECT count(*) FROM %T WHERE id BETWEEN 4000 AND 12000"
check "row count sane" "$([ "$(q 'SELECT count(*) FROM t_col;')" -gt 0 ] && echo ok)" "ok"

pgc_summary

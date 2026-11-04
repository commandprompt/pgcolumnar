#!/usr/bin/env bash
#
# pgColumnar Phase F scale validation. Registered in run_all_versions.sh (#2018),
# so the per-PR matrix runs it on PG 17 and 18 and the nightly on 15 to 18 on
# x86_64 and on 18 on aarch64, at the default SCALE_N. Until #2018 it was opt-in
# and no job ran it. Its pytest twin is test/pytest/test_native_scale.py, which
# the pytest-cluster jobs run at SCALE_N=200000. Exercises online compaction, rewrite,
# reclustering, and physical page reclaim at a large row count and asserts
# correctness against a heap mirror plus a bounded, steady-state file under
# repeated compaction. The small matrix suites prove logic on a handful of
# groups; this proves the
# mutation/clustering/reclaim machinery holds at scale and across many cycles
# (no O(rows) blowups, and reclaim actually reaches a plateau rather than
# growing without bound). Uses order-independent aggregates for parity (a full
# set-hash of half a million rows is needless here).
#
# It was this suite (at SCALE_N=200000, repeated compaction) that surfaced the
# free-space allocator's missing CommandCounterIncrement: without it a second
# allocation in one compaction command re-selected the just-consumed free_space
# row and failed with "tuple already updated by self". The checks that each
# recluster and compact_rewrite returned a group count are the regression guard.
# The plateau check alone is not: a failed call rolls back, the file stops
# changing, and a file that does not change passes it.
#
# Usage:  SCALE_N=500000 test/native_scale.sh [PG_CONFIG]
#         (SCALE_N=6000000 for a full-scale run; expect the recluster to be slow.)
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N="${SCALE_N:-500000}"
# compute the products in bigint so large g does not overflow int32
GEN="SELECT g AS id, (g % 1000)::int AS a, (g::bigint * 7) AS b,
       ((g::bigint * 7919) % 100000)::int AS c
  FROM generate_series(1, $N) g"

# order-independent fingerprint of the live set (same query on both tables)
FP="SELECT count(*), sum(a)::bigint, sum(b)::bigint, min(c), max(c),
           bit_xor(('x' || substr(md5(id||'|'||a||'|'||b||'|'||c), 1, 16))::bit(64)::bigint)"
fp_n() { q "$FP FROM n;"; }
fp_h() { q "$FP FROM h;"; }
fsize() { q "SELECT pg_relation_size('n');"; }
# Run one maintenance call and echo its result, OR its error text. q throws the
# error away, so a failed recluster or compact_rewrite read as an empty result
# and no check saw it (OffgridwithJD, review of #2021). The failed call rolls
# back, so the parity checks still pass and the file stops changing, which the
# plateau check reads as success.
maint() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=1 -c "$1" 2>&1 || true
}
# is_count RESULT: "yes" when the call returned a group count, else the whole
# result, which is the error text when the call failed.
is_count() {
	pgc_is_number "$1" && echo yes || echo "no: [$1]"
}

echo "-- loading $N rows into heap + columnar"
psql_run "CREATE TABLE h (id int, a int, b bigint, c int);"
psql_run "CREATE TABLE n (id int, a int, b bigint, c int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 50000, chunk_group_row_limit => 10000);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"
psql_run "CREATE INDEX n_id ON n (id);"

check "bulk load count" "$(q 'SELECT count(*) FROM n;')" "$N"
check "bulk load parity" "$(fp_n)" "$(fp_h)"

# ---- correctness under mutation / clustering -------------------------------
# Delete ~25%, then online rewrite of the partially-deleted groups.
psql_run "DELETE FROM h WHERE id % 4 = 0;"
psql_run "DELETE FROM n WHERE id % 4 = 0;"
check "parity after delete" "$(fp_n)" "$(fp_h)"
rew="$(maint "SELECT pgcolumnar.compact_rewrite('n', 0.1);")"
echo "  (compact_rewrite rewrote $rew groups)"
check "compact_rewrite returned a group count" "$(is_count "$rew")" "yes"
check "parity after compact_rewrite" "$(fp_n)" "$(fp_h)"
# online index maintenance: rewritten rows carry new row numbers and must have
# fresh index entries; an indexed range lookup must match the heap exactly.
check "indexed lookup after rewrite" \
	"$(q "SELECT count(*) FROM n WHERE id BETWEEN 1 AND $((N/2));")" \
	"$(q "SELECT count(*) FROM h WHERE id BETWEEN 1 AND $((N/2));")"

# Online recluster by c; results unchanged. (This is the slow step at scale.)
rec="$(maint "SELECT pgcolumnar.recluster('n', 'c');")"
echo "  (recluster processed $rec groups)"
check "recluster returned a group count" "$(is_count "$rec")" "yes"
check "parity after recluster" "$(fp_n)" "$(fp_h)"

# ---- physical reclaim: steady state across repeated compaction -------------
# Rounds of {delete a rotating 1/8 slice, compact_rewrite} with NO inserts, so
# the only writes are compaction's. After the first round establishes a
# generation, the file must PLATEAU: each round reuses the prior round's freed
# space instead of extending. A growing file here means reuse regressed.
#
# Round 4 deletes id % 8 = 5, not 4 (#2024). Every id with id % 8 = 4 also has
# id % 4 = 0, and those rows went in the delete above. So round 4 deleted no
# rows, rewrote nothing, and the steady-state check compared round 3 with round
# 2. A mutant that turned reuse off from round 4 on passed all 20 checks. Each
# round's delete is now counted: it must remove all (N - s) / 8 + 1 rows of its
# slice from each table.
sizes=()
SLICES=(1 2 3 5)
for r in 1 2 3 4; do
	s="${SLICES[r-1]}"
	want_del=$(( (N - s) / 8 + 1 ))
	for t in h n; do
		del="$(q "WITH d AS (DELETE FROM $t WHERE id % 8 = $s RETURNING 1) SELECT count(*) FROM d;")"
		check_num "reclaim round $r deletes its whole id % 8 slice from $t" "$del" "$want_del"
	done
	# WAIT FOR THE HORIZON FIRST (#2010). A rewrite reuses a freed range only
	# when no transaction is older than the one that freed it. A snapshot another
	# session held through the rounds made every rewrite append. The reuse code
	# worked, but the horizon kept it from running, and the file grew 3792896 ->
	# 5054464 -> 6045696. So rounds 2 to 4, the rounds the steady-state check
	# reads, wait until nothing holds the horizon at the newest range freed in n,
	# and the count is checked. Round 1 does not wait: its rewrite reuses what the
	# recluster freed, and no check compares its size.
	if [ "$r" -ge 2 ]; then
		blockers="$(pgc_wait_freed_range_past_horizon n)"
		echo "  (round $r horizon blockers/calls before the rewrite: $blockers)"
		check_num "round $r: no transaction older than the newest freed range was open before the rewrite" \
			"${blockers%/*}" "0"
	fi
	rw="$(maint "SELECT pgcolumnar.compact_rewrite('n', 0.02);")"
	check "reclaim round $r: compact_rewrite returned a group count" "$(is_count "$rw")" "yes"
	check "parity after reclaim round $r" "$(fp_n)" "$(fp_h)"
	sizes+=("$(fsize)")
	echo "  (round $r file size: ${sizes[-1]})"
done
# Rounds 2..4 each delete a slice and reuse earlier frees; the file must not
# grow past round 2's size.
check "reclaim reached steady state (round 4 <= round 2)" \
	"$([ "${sizes[3]}" -le "${sizes[1]}" ] && echo yes ||
	   echo "no (round 2 ${sizes[1]}, round 4 ${sizes[3]})")" "yes"

pgc_summary

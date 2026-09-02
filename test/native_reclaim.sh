#!/usr/bin/env bash
#
# pgColumnar physical page reclaim (Phase F free-list). Retiring a row group
# records its data byte range in pgcolumnar.free_space; an online compaction
# (which holds ShareUpdateExclusiveLock and is self-serialized) then reserves from
# a freed range instead of advancing the file highwater, once the oldest-xmin
# horizon has passed the freeing transaction. So repeated online compaction reuses
# space and the relation file plateaus instead of growing every cycle. Plain
# inserts always append (they never race a reuse). This suite proves that repeated
# recluster keeps the file bounded (reuse), that free_space is populated and
# consumed, and that the reused blocks hold correct data (parity with a heap
# mirror).
#
# It also proves the compaction threshold guards, in BOTH directions, for both
# entry points: pgcolumnar.compact_rewrite() and pgcolumnar.maintenance_due(),
# which is the gate the autovacuum daemon consults before it ever calls
# compact_rewrite (see the block near the end).
#
# Usage:  test/native_reclaim.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Print the 5-char SQLSTATE of a statement, OK when it succeeded, HANG when it
# did not finish inside the cap, or ERR_NO_SQLSTATE_rcN when psql failed without
# reporting one (a dead server, a bad connection) -- which must never read as OK.
#
# Every deny arm below asserts a SQLSTATE rather than "it failed". "It failed" is
# satisfied by a typo, a missing function, a wrong-arity call and 1/0 alike; the
# controls below assert exactly that discrimination. Modelled on
# test/arrow_import.sh's sqlstate_or_hang.
sqlstate() {
	local out rc st
	out="$(timeout -s KILL 120 env PATH="$PGC_BINDIR:$PATH" psql \
		-h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY sqlstate
$1;
SQLEOF
)"
	rc=$?
	if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then echo HANG; return; fi
	st="$(printf '%s\n' "$out" | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1)"
	if [ -n "$st" ]; then printf '%s\n' "$st"; return; fi
	if [ "$rc" -ne 0 ]; then echo "ERR_NO_SQLSTATE_rc$rc"; return; fi
	echo OK
}

GEN="SELECT g, g AS v, md5(g::text) AS payload FROM generate_series(1, 6000) g"

psql_run "CREATE TABLE h (id int, v int, payload text);"
psql_run "CREATE TABLE n (id int, v int, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

fsize() { q "SELECT pg_relation_size('n');"; }
freerows() { q "SELECT count(*) FROM pgcolumnar.free_space WHERE storage_id = pgcolumnar.get_storage_id('n');"; }

size1="$(fsize)"
check "initial data present" "$(q 'SELECT count(*) FROM n;')" "6000"
check "no free space yet" "$(freerows)" "0"

# First online recluster: retires the original groups (freed) and writes new ones.
# With no reusable space yet it appends, so the file grows and free_space fills.
psql_run "SELECT pgcolumnar.recluster('n', 'id');"
size2="$(fsize)"
check "free space recorded after first recluster" \
	"$([ "$(freerows)" -gt 0 ] && echo yes || echo no)" "yes"
check "data intact after first recluster" \
	"$(q 'SELECT count(*) FROM n;')" "6000"

# Repeated reclusters must REUSE the previous cycle's freed space (its freeing
# transaction has committed and the horizon has passed it), so the file plateaus
# instead of growing every cycle. Without reuse it would grow by ~size1 each time.
for i in 1 2 3 4; do
	psql_run "SELECT pgcolumnar.recluster('n', 'id');"
done
size3="$(fsize)"
echo "  (file size: initial=$size1 after-1-recluster=$size2 after-5-reclusters=$size3; free_space rows=$(freerows))"

check "repeated recluster reuses space (file plateaus)" \
	"$([ "$size3" -le "$((size2 + size1))" ] && echo yes || echo no)" "yes"
check "free space is being reused (bounded, not growing per cycle)" \
	"$([ "$(freerows)" -gt 0 ] && echo yes || echo no)" "yes"

# The reused blocks hold correct data.
check "reused-block data matches heap mirror" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM h ORDER BY id')"

# Reuse survives a delete + rewrite cycle too, with correct data.
psql_run "DELETE FROM h WHERE id % 4 = 0;"
psql_run "DELETE FROM n WHERE id % 4 = 0;"

# ---- compact_rewrite's threshold guard, both directions (#860) --------------
#
# NaN passes both ordinary C range comparisons (`< 0.0` and `> 1.0` are each
# false for NaN). Refuse it explicitly, or the candidate predicate is false for
# every group and compaction silently does no work despite an accepted threshold.
# 22023 is ERRCODE_INVALID_PARAMETER_VALUE, the code the guard raises.

# Controls. These four calls are exactly what an "it errored" assertion cannot
# tell apart from the guard firing, so each one is pinned to its own SQLSTATE.
# If any of them ever returns 22023, the deny arms below prove nothing.
check "control: a missing function is 42883, not the guard's 22023" \
	"$(sqlstate "SELECT pgcolumnar.no_such_function(1)")" "42883"
check "control: a wrong-arity compact_rewrite is 42883, not 22023" \
	"$(sqlstate "SELECT pgcolumnar.compact_rewrite('n', 0.5, 1, 'x')")" "42883"
check "control: a null table name is 22004, not 22023" \
	"$(sqlstate "SELECT pgcolumnar.compact_rewrite(NULL, 0.5)")" "22004"
check "control: division by zero is 22012, not 22023" \
	"$(sqlstate "SELECT 1/0")" "22012"

check "compact_rewrite rejects a NaN threshold with 22023" \
	"$(sqlstate "SELECT pgcolumnar.compact_rewrite('n', 'NaN'::float8)")" "22023"
check "compact_rewrite rejects a threshold above 1 with 22023" \
	"$(sqlstate "SELECT pgcolumnar.compact_rewrite('n', 2.0)")" "22023"
check "compact_rewrite rejects a negative threshold with 22023" \
	"$(sqlstate "SELECT pgcolumnar.compact_rewrite('n', -1.0)")" "22023"

# The other direction, which is how a bounds fix usually breaks. Nothing that
# was here could see the guard becoming OVER-broad: the only call at 0.0 was a
# bare `psql_run "SELECT pgcolumnar.compact_rewrite('n', 0.0);"` whose exit
# status nothing read, so rejecting the legal 0.0 reached nothing but the server
# log ("ERROR:  min_deleted_fraction must be a number between 0 and 1").
# Measured: with `minFrac < 0.0` changed to `minFrac <= 0.0`, this suite fails
# exactly one arm -- the 0.0 arm below -- and the other 32 pass. 0.0 and 1.0 are
# legal and sit exactly ON the boundary, so these two arms redden if either half
# of the comparison goes strict.
check "compact_rewrite ACCEPTS the boundary threshold 0.0" \
	"$(sqlstate "SELECT pgcolumnar.compact_rewrite('n', 0.0)")" "OK"
check "compact_rewrite ACCEPTS the boundary threshold 1.0" \
	"$(sqlstate "SELECT pgcolumnar.compact_rewrite('n', 1.0)")" "OK"

psql_run "SELECT pgcolumnar.recluster('n', 'id');"
check "data correct after delete + compact_rewrite + recluster" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM h ORDER BY id')"
check "row count correct after cycle" "$(q 'SELECT count(*) FROM n;')" "$(q 'SELECT count(*) FROM h;')"

# ---- maintenance_due(): the gate consulted BEFORE compact_rewrite (#860) ----
#
# pgcolumnar.maintenance_due() is what the autovacuum daemon reads to decide
# whether to call compact_rewrite at all, and it validated nothing. Measured on
# exactly this fixture (20000 rows, 10000 deleted) with the guard removed, beside
# what compact_rewrite answered for the same value:
#   threshold      compact_rewrite_due   recommendation     compact_rewrite()
#   0.2            t                     compact_rewrite    OK
#   NaN            f                     (none)             22023
#   2.0            f                     (none)             22023
#   -1.0           t                     compact_rewrite    22023
#   NULL           (null)                (none)             OK (NULL means 0.2)
# NaN and 2.0 suppress the work for good. -1.0 is the worst of them: it is not a
# suppressed report but a permanent, self-renewing rewrite of every columnar
# table, because `fraction >= -1` is true whatever the table's real state.
# NULL is the NaN case again, because the daemon reads a NULL verdict as
# "not due" (SPI_getbinval isnull).
#
# Both thresholds are validated because both are thresholds.
psql_run "CREATE TABLE nd (id int, v int, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('nd', stripe_row_limit => 1000, chunk_group_row_limit => 1000);"
psql_run "INSERT INTO nd SELECT g, g, md5(g::text) FROM generate_series(1, 20000) g;"
psql_run "DELETE FROM nd WHERE id % 2 = 0;"

# Premise, GATED not printed: every arm below reads a verdict about a table that
# is 50% deleted. If it is not that table, no verdict from it means anything, so
# refuse to report one.
md_total="$(q "SELECT total_rows FROM pgcolumnar.maintenance_due('nd', 0.2, 0.05)")"
md_del="$(q "SELECT deleted_rows FROM pgcolumnar.maintenance_due('nd', 0.2, 0.05)")"
check_num "premise: nd holds 20000 rows" "$md_total" "20000"
check_num "premise: 10000 of nd's rows are deleted" "$md_del" "10000"
if [ "$md_total" != "20000" ] || [ "$md_del" != "10000" ]; then
	echo "FATAL: nd is not the 50%-deleted fixture these arms describe" \
		"(total_rows=[$md_total] deleted_rows=[$md_del]); refusing to report a verdict from it"
	pgc_summary
	exit 1
fi

# A legal threshold still answers, and answers correctly: 0.5 deleted >= 0.2.
check "maintenance_due(0.2) on a 50%-deleted table is still due" \
	"$(q "SELECT compact_rewrite_due FROM pgcolumnar.maintenance_due('nd', 0.2, 0.05)")" "t"
check "maintenance_due(0.6) on a 50%-deleted table is not due" \
	"$(q "SELECT compact_rewrite_due FROM pgcolumnar.maintenance_due('nd', 0.6, 0.05)")" "f"

check "maintenance_due rejects a NaN compact threshold with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 'NaN'::float8, 0.05)")" "22023"
check "maintenance_due rejects a compact threshold above 1 with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 2.0, 0.05)")" "22023"
check "maintenance_due rejects a negative compact threshold with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', -1.0, 0.05)")" "22023"
check "maintenance_due rejects a NULL compact threshold with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', NULL::float8, 0.05)")" "22023"

check "maintenance_due rejects a NaN recluster threshold with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 0.2, 'NaN'::float8)")" "22023"
check "maintenance_due rejects a recluster threshold above 1 with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 0.2, 2.0)")" "22023"
check "maintenance_due rejects a negative recluster threshold with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 0.2, -1.0)")" "22023"
check "maintenance_due rejects a NULL recluster threshold with 22023" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 0.2, NULL::float8)")" "22023"

# The new guard must not be over-broad either. 0.0 and 1.0 are legal thresholds
# on both parameters and sit exactly on the boundary.
check "maintenance_due ACCEPTS the boundary thresholds 0.0, 0.0" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 0.0, 0.0)")" "OK"
check "maintenance_due ACCEPTS the boundary thresholds 1.0, 1.0" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd', 1.0, 1.0)")" "OK"
# ... and the defaults, which the daemon uses, still resolve.
check "maintenance_due ACCEPTS its own defaults" \
	"$(sqlstate "SELECT * FROM pgcolumnar.maintenance_due('nd')")" "OK"

pgc_summary

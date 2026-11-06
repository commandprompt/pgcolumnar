#!/usr/bin/env bash
#
# pgColumnar Z-order clustering (Phase F2). pgcolumnar.cluster(table, cols)
# physically reorders rows by the Z-order (Morton) space-filling curve over
# several columns, so the per-group min/max zone maps of ALL clustered columns
# tighten at once. A multi-column box predicate then skips far more row groups
# than in insert order, where the columns are scattered. Clustering only reorders
# storage, so results are identical to a heap mirror. This suite proves parity,
# the skipping improvement, single-column clustering, and a clean error on an
# unsupported clustering type. It is the EAGER reorg (AccessExclusiveLock); the
# online incremental path is Phase F3.
#
# Usage:  test/native_cluster.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# 40960 rows in 20 row groups (2048 each). x and y are scattered in insert order
# via coprime multiplicative hashes, so before clustering each group's x and y
# ranges span most of [0,200) and a small 2D box skips nothing. Z-order
# clustering co-locates nearby (x,y).
GEN="SELECT g,
       ((g::bigint * 7919) % 200)::int   AS x,
       ((g::bigint * 104729) % 200)::int AS y,
       'p' || (g % 97)                    AS payload
  FROM generate_series(1, 40960) g"

psql_run "CREATE TABLE h (id int, x int, y int, payload text);"
psql_run "CREATE TABLE n (id int, x int, y int, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

# Count of row groups skipped by the zone maps for a query, from EXPLAIN ANALYZE.
# Nothing when the plan could not be read, which the checks below read as no
# reading rather than as 0 skipped.
skipped() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null \
		| grep 'Columnar Chunk Groups Removed by Filter' | grep -oE '[0-9]+' | head -1
}
# more_and_most BEFORE AFTER -> yes when AFTER > BEFORE and AFTER >= 10, both readings
# numbers. An unread count used to default to 0, which a "more than before" reading
# then beat for free.
more_and_most() {
	if ! [[ "$1" =~ ^[0-9]+$ && "$2" =~ ^[0-9]+$ ]]; then echo "no reading (before=[$1] after=[$2])"
	elif [ "$2" -gt "$1" ] && [ "$2" -ge 10 ]; then echo yes
	else echo "no (before=$1 after=$2 of 20)"; fi
}
more_than() {	# more_than BEFORE AFTER -> yes when both are numbers and AFTER > BEFORE
	if ! [[ "$1" =~ ^[0-9]+$ && "$2" =~ ^[0-9]+$ ]]; then echo "no reading (before=[$1] after=[$2])"
	elif [ "$2" -gt "$1" ]; then echo yes; else echo "no (before=$1 after=$2)"; fi
}
at_least_10() {	# at_least_10 N -> yes when N is a number of at least 10
	if ! [[ "$1" =~ ^[0-9]+$ ]]; then echo "no reading [$1]"
	elif [ "$1" -ge 10 ]; then echo yes; else echo "no ($1 of 20)"; fi
}

# refusal SQL PHRASE -> the SQLSTATE when SQL is refused with PHRASE in its message;
# "SQLSTATE (another refusal: MESSAGE)" when a different guard refused it; "no
# error" when it ran; nothing when the server could not be asked. The refusal
# checks used to accept any failure of psql, and a crash is one: with the
# unsupported-type refusal replaced by abort(), both passed.
refusal() {
	local out st msg
	await_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then
		case "$msg" in *"$2"*) echo "$st" ;; *) echo "$st (another refusal: $msg)" ;; esac
	elif grep -q 'could not connect\|server closed the connection\|terminating connection' <<<"$out"; then
		:
	else
		echo "no error"
	fi
}
# await_up -> wait, at most 60 s, until the server answers, so a refusal that killed
# a backend reds its own arm and the survival check, not the arms after it.
await_up() {
	local i
	for i in $(seq 1 120); do
		[ "$(q 'SELECT 1')" = 1 ] && return 0
		sleep 0.5
	done
}
# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	await_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}

BOX="x BETWEEN 20 AND 40 AND y BETWEEN 20 AND 40"

check "row count" "$(q 'SELECT count(*) FROM n;')" "40960"
check "box parity (pre-cluster)" \
	"$(pgc_set_hash "SELECT id, x, y, payload FROM n WHERE $BOX")" \
	"$(pgc_set_hash "SELECT id, x, y, payload FROM h WHERE $BOX")"

# The node, before any counter read out of it: the skip counts below are
# reported by the columnar custom scan and by nothing else.
check "the plan under test is a columnar custom scan" \
	"$(pgc_is_columnar_scan "SELECT id FROM n WHERE $BOX")" "yes"

before="$(skipped "SELECT id FROM n WHERE $BOX")"

# Cluster by (x, y) -- the eager Z-order reorg.
psql_run "SELECT pgcolumnar.cluster('n', 'x', 'y');"

check "row count after cluster" "$(q 'SELECT count(*) FROM n;')" "40960"
check "box parity (post-cluster)" \
	"$(pgc_set_hash "SELECT id, x, y, payload FROM n WHERE $BOX")" \
	"$(pgc_set_hash "SELECT id, x, y, payload FROM h WHERE $BOX")"
check "full-table parity after cluster" \
	"$(pgc_set_hash 'SELECT id, x, y, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, x, y, payload FROM h ORDER BY id')"

after="$(skipped "SELECT id FROM n WHERE $BOX")"

echo "  (chunk groups skipped for the box: before=$before after=$after of 20)"
# Z-order tightens both columns' zone maps, so the box skips strictly more groups
# after clustering, and skips most of the 20 groups.
check "clustering increases 2D skipping" "$(more_than "$before" "$after")" "yes"
check "clustering skips most groups" "$(at_least_10 "$after")" "yes"

# Single-column clustering works and preserves results. The (x, y) clustering
# above scattered id across the groups, so an id range skips almost nothing now;
# clustering on id alone must bring it back. Parity alone held with cluster(n, id)
# doing nothing at all: on main it passed with single-column clustering a no-op.
IDR="id BETWEEN 1 AND 2048"
id_before="$(skipped "SELECT id FROM n WHERE $IDR")"
psql_run "SELECT pgcolumnar.cluster('n', 'id');"
id_after="$(skipped "SELECT id FROM n WHERE $IDR")"
echo "  (chunk groups skipped for the id range: before=$id_before after=$id_after of 20)"
check "single-column clustering lets an id range skip most groups" \
	"$(more_and_most "$id_before" "$id_after")" "yes"
check "single-column cluster parity" \
	"$(pgc_set_hash 'SELECT id, x, y, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, x, y, payload FROM h ORDER BY id')"

# An unsupported clustering type (text) must be refused by its own guard, not crash,
# and a non-existent column likewise. Each reads the guard's SQLSTATE and words.
crashes_before="$(crashes)"
check "unsupported type errors cleanly" \
	"$(refusal "SELECT pgcolumnar.cluster('n', 'payload')" 'column "payload" of type text cannot be used as a clustering key')" "0A000"
check "missing column errors cleanly" \
	"$(refusal "SELECT pgcolumnar.cluster('n', 'nope')" 'column "nope" does not exist in table "n"')" "42703"
check "backend still up after the refusals" "$(survived "$crashes_before")" "1"

pgc_summary

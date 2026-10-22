#!/usr/bin/env bash
#
# pgColumnar declarative sort_by clustering key (#288). set_options(t, sort_by =>
# ARRAY[...]) declares a persisted physical sort key; vacuum_sorted(t) with no
# explicit columns applies it (the PostgreSQL CLUSTER model: remember the key,
# re-apply on demand). Unlike the numeric-only Z-order cluster(), the sorted
# rewrite handles any btree-orderable column, TEXT included, so an equality
# predicate on a text segment key skips chunk groups once the table is sorted.
# This suite proves: heap-parity, that sorting drives group skipping, the
# declared-key round-trip, set/reset, multi-column keys, and clean errors for a
# stale/dropped key, a bad column, a virtual generated column, and no-key.
#
# THE PHYSICAL ORDER IS READ, not inferred. Group skipping on host was the only
# evidence of a sort, and a sort that kept hosts together but in the wrong order
# (descending, or on host alone with ts unordered) skipped just as well. The rows
# are now read in scan order and every adjacent pair is compared on (host, ts).
#
# A REFUSAL READS ITS OWN GUARD: the SQLSTATE and a phrase of the guard's message.
# "psql failed" was enough before, and a backend that crashed in place of the
# refusal failed psql too. Survival reads the server log's count of backends
# killed by a signal, and every reading after a refusal first waits for the server.
# Written fresh for pgColumnar.
#
# Usage:  test/native_sort_by.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# 40960 rows, 50 text hosts scattered in insert order (h000..h049 cycled), so
# before sorting each 1024-row chunk group holds all 50 hosts and its host
# min/max spans the whole range -- an equality on one host skips nothing. Small
# stripe/chunk limits so many groups form.
GEN="SELECT g AS id,
       'h' || lpad((g % 50)::text, 3, '0') AS host,
       ('2024-01-01'::timestamptz + ((g % 1440) * interval '1 minute')) AS ts,
       'p' || (g % 7) AS payload
  FROM generate_series(1, 40960) g"

psql_run "CREATE TABLE h (id int, host text, ts timestamptz, payload text);"
psql_run "CREATE TABLE n (id int, host text, ts timestamptz, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

# chunk groups removed by the zone maps for a query, from EXPLAIN ANALYZE; empty
# when the plan could not be read or printed no such line
skipped() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null \
		| grep 'Columnar Chunk Groups Removed by Filter' | grep -oE '[0-9]+' | head -1
}
# wait, at most 60 s, until the server answers again (after a crash's recovery)
await_up() {
	local i
	for i in $(seq 1 120); do
		[ "$(q 'SELECT 1')" = 1 ] && return 0
		sleep 0.5
	done
	return 1
}
# refusal_of SQL PHRASE -> "<SQLSTATE> <PHRASE>" when SQL raised with PHRASE in its
# message, "<SQLSTATE> other: <first line>" for a different refusal, "no error" when
# it ran, or "no (<first line>)" when the connection was lost (a crash)
refusal_of() {
	local out state first
	await_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$1" 2>&1)" && { echo "no error"; return; }
	state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	first="$(head -1 <<<"$out")"
	if [ -z "$state" ]; then echo "no ($first)"
	elif grep -qF "$2" <<<"$out"; then echo "$state $2"
	else echo "$state other: $first"; fi
}
# ran_ok SQL -> "ok" when SQL ran to the end, or what happened instead
ran_ok() {
	local out
	await_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 -c "$1" 2>&1)" && { echo ok; return; }
	echo "no ($(head -1 <<<"$out"))"
}
crashes() {	# -> backends killed by a signal so far, or nothing when the log is unreadable
	local n
	[ -r "$PGC_LOGFILE" ] || return 0
	n="$(grep -c 'terminated by signal' "$PGC_LOGFILE" 2>/dev/null)"	# prints 0, exit 1, on no match
	printf '%s' "$n"
}
survived() {	# survived BEFORE -> 1, or why not
	local up now
	await_up
	up="$(q 'SELECT 1')"; now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ ]] || ! [[ "$now" =~ ^[0-9]+$ ]]; then
		echo "no (crash count unreadable)"
	elif [ "$up" = 1 ] && [ "$now" = "$1" ]; then echo 1
	else echo "no ([$up], $((now - $1)) backend crashes)"; fi
}
# the scan-order pairs that break (host, ts) ascending; a number, or empty on failure
INVERSIONS="SELECT count(*) FROM (SELECT host, ts, lag(host) OVER () AS ph, lag(ts) OVER () AS pt
              FROM (SELECT host, ts FROM n OFFSET 0) s) x
             WHERE ph IS NOT NULL AND (ph, pt) > (host, ts)"
# the (host, ts) sequence in scan order, as one digest
LAYOUT="SELECT md5(string_agg(host || '|' || ts::text, ',')) FROM (SELECT host, ts FROM n OFFSET 0) s"

EQ="host = 'h007'"

check "row count" "$(q 'SELECT count(*) FROM n;')" "40960"
check "the plan under test is a columnar custom scan" \
	"$(pgc_is_columnar_scan "SELECT id FROM n WHERE $EQ")" "yes"
check "equality parity (pre-sort)" \
	"$(pgc_set_hash "SELECT id, host, ts, payload FROM n WHERE $EQ")" \
	"$(pgc_set_hash "SELECT id, host, ts, payload FROM h WHERE $EQ")"

# The generator cycles hosts, so the inserted order is far from (host, ts) order;
# without this the order check below could pass on a table that was never sorted.
check "premise: before sorting, the scan order does not follow (host, ts)" \
	"$(inv="$(q "$INVERSIONS")"; pgc_is_number "$inv" && [ "$inv" -gt 0 ] && echo yes || echo "no [$inv]")" "yes"

before="$(skipped "SELECT id FROM n WHERE $EQ")"

# ---- declare the key, then apply it with the zero-arg form ----
psql_run "SELECT pgcolumnar.set_options('n', sort_by => ARRAY['host','ts']);"
check "declared sort_by round-trips" \
	"$(q "SELECT sort_by::text FROM pgcolumnar.options WHERE regclass = 'n'::regclass")" \
	"{host,ts}"

psql_run "SELECT pgcolumnar.vacuum_sorted('n');"   # no columns -> use declared key

check "row count after sort" "$(q 'SELECT count(*) FROM n;')" "40960"
check "full-table parity after sort" \
	"$(pgc_set_hash 'SELECT id, host, ts, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, host, ts, payload FROM h ORDER BY id')"
check "equality parity (post-sort)" \
	"$(pgc_set_hash "SELECT id, host, ts, payload FROM n WHERE $EQ")" \
	"$(pgc_set_hash "SELECT id, host, ts, payload FROM h WHERE $EQ")"

after="$(skipped "SELECT id FROM n WHERE $EQ")"
echo "  (chunk groups skipped for $EQ: before=$before after=$after)"
check "sorting a text key enables group skipping" \
	"$(if ! pgc_is_number "$before" || ! pgc_is_number "$after"; then
		echo "no reading (before [$before], after [$after])"
	elif [ "$after" -gt "$before" ] && [ "$after" -gt 0 ]; then echo yes; else echo no; fi)" "yes"
check_num "the scan order follows the declared key (host, ts)" "$(q "$INVERSIONS")" "0"
DECLARED_LAYOUT="$(q "$LAYOUT")"

# ---- explicit columns still work and equal the declared-key result ----
psql_run "SELECT pgcolumnar.vacuum_sorted('n', 'host', 'ts');"
check "explicit-column sort parity" \
	"$(pgc_set_hash 'SELECT id, host, ts, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, host, ts, payload FROM h ORDER BY id')"
check_text "explicit columns lay the rows out as the declared key does" \
	"$(q "$LAYOUT")" "${DECLARED_LAYOUT:-no declared layout read}"

# ---- reset clears the key; zero-arg then errors ----
psql_run "SELECT pgcolumnar.reset_options('n', sort_by => true);"
check "sort_by cleared" \
	"$(q "SELECT sort_by IS NULL FROM pgcolumnar.options WHERE regclass = 'n'::regclass")" "t"
CRASHES_BEFORE="$(crashes)"
check "zero-arg errors when no key declared" \
	"$(refusal_of "SELECT pgcolumnar.vacuum_sorted('n')" "has no declared sort_by key")" \
	"22023 has no declared sort_by key"

# ---- validation errors ----
check "set_options rejects a nonexistent sort_by column" \
	"$(refusal_of "SELECT pgcolumnar.set_options('n', sort_by => ARRAY['nope'])" "cannot be used in sort_by")" \
	"P0001 cannot be used in sort_by"

# stale declared key: declare, drop the column, then the zero-arg apply must error
psql_run "SELECT pgcolumnar.set_options('n', sort_by => ARRAY['payload']);"
psql_run "ALTER TABLE n DROP COLUMN payload;"
check "zero-arg errors on a dropped declared column" \
	"$(refusal_of "SELECT pgcolumnar.vacuum_sorted('n')" "declared sort_by column \"payload\" does not exist")" \
	"42703 declared sort_by column \"payload\" does not exist"
psql_run "SELECT pgcolumnar.reset_options('n', sort_by => true);"

# ---- edge cases: empty and single-row tables sort cleanly ----
psql_run "CREATE TABLE e (id int, host text) USING pgcolumnar;"
check "empty table sorts without error" "$(ran_ok "SELECT pgcolumnar.vacuum_sorted('e', 'host')")" "ok"
psql_run "INSERT INTO e VALUES (1, 'x');"
check "single-row table sorts without error" "$(ran_ok "SELECT pgcolumnar.vacuum_sorted('e', 'host')")" "ok"
check "single-row parity" "$(q 'SELECT count(*) FROM e')" "1"

# ---- virtual generated column rejected as a key (PG18+) ----
# The major is asserted first, so an unreadable version is not mistaken for an old
# one. Before PG18 the arm is recorded as a SKIP rather than printed and left out.
if ! pgc_is_number "${PGC_MAJOR:-}"; then
	pgc_fail "could not read the server major, so the virtual arm cannot be gated" \
		"got [${PGC_MAJOR:-<none>}]"
elif [ "$PGC_MAJOR" -ge 18 ]; then
	psql_run "CREATE TABLE v (id int, k int, kv int GENERATED ALWAYS AS (k * 2) VIRTUAL) USING pgcolumnar;"
	psql_run "INSERT INTO v (id, k) SELECT g, g % 10 FROM generate_series(1, 100) g;"
	check "set_options rejects a virtual generated sort_by column" \
		"$(refusal_of "SELECT pgcolumnar.set_options('v', sort_by => ARRAY['kv'])" "cannot be used in sort_by")" \
		"P0001 cannot be used in sort_by"
	check "vacuum_sorted rejects an explicit virtual generated column" \
		"$(refusal_of "SELECT pgcolumnar.vacuum_sorted('v', 'kv')" "is a virtual generated column and cannot be a sort key")" \
		"22023 is a virtual generated column and cannot be a sort key"
else
	check_skip "virtual generated sort keys" \
		"SKIP  VIRTUAL generated columns arrived in PostgreSQL 18; this server is $PGC_MAJOR" \
		"needs VIRTUAL generated columns, PG18+"
fi

check "backend still up after the refusals" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary

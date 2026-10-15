#!/usr/bin/env bash
#
# pgcolumnar.partition_create_ahead: columnar partitions created ahead, without an
# exclusive lock on the parent (#1297).
#
# WRITTEN BEFORE THE FUNCTION EXISTS (the owner's TDD rule). The specification it
# pins, from #1297's design note 1, measured on PG 15..19:
#
#   * CREATE TABLE ... PARTITION OF takes ACCESS EXCLUSIVE on the parent, while a
#     standalone CREATE followed by ATTACH takes SHARE UPDATE EXCLUSIVE. So the
#     function must never hold more than SHARE UPDATE EXCLUSIVE on the parent.
#   * before PG 17 the parent cannot carry USING pgcolumnar, so the function sets
#     the access method on every partition itself.
#   * a DEFAULT partition rules out DETACH CONCURRENTLY for the drop half, so a
#     parent with one is refused rather than half-managed.
#
#   partition_create_ahead(parent regclass, width interval, ahead int) -> int
#
#   Makes sure the parent's partitions reach now() + ahead * width, each `width`
#   wide and contiguous with the last existing upper bound -- or, with no
#   partitions yet, starting at now() truncated to the width's unit -- and returns
#   how many it created. A second call creates nothing: the daemon calls it every
#   pass.
#
# Usage:  test/partition_lifecycle.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

at_least() { [ -z "$1" ] && return; [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
check_num "premise: the function exists" \
	"$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	      WHERE n.nspname = 'pgcolumnar' AND p.proname = 'partition_create_ahead';")" "1"

sqlstate_or() {	# sqlstate_or SQL -> the single value it returns, or the SQLSTATE it fails with
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)
	case "$out" in ERROR:*) sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1;; *) echo "$out";; esac
}
kids() { q "SELECT count(*) FROM pg_inherits WHERE inhparent = '$1'::regclass;"; }
# rc REL -> the relation as the function's messages name it: they format regclass
# under its own search_path (pg_catalog, pg_temp), so always schema-qualified.
rc() { q "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = '$1'::regclass;"; }
refusal() {	# refusal SQL -> "SQLSTATE message" of the error it raises, or "ok: VALUE"
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$1" 2>&1)
	case "$out" in ERROR:*) sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p' <<<"$out" | head -1;; *) echo "ok: $out";; esac
}
ams()  { q "SELECT coalesce(string_agg(DISTINCT a.amname, ','), 'none') FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_am a ON a.oid = c.relam
            WHERE i.inhparent = '$1'::regclass;"; }
# the partition bounds, as "from|to" lines in order
bounds() {
	q "SELECT (regexp_match(pg_get_expr(c.relpartbound, c.oid),
	           'FROM \\(''([^'']+)''\\) TO \\(''([^'']+)''\\)'))[1]
	          || '|' ||
	          (regexp_match(pg_get_expr(c.relpartbound, c.oid),
	           'FROM \\(''([^'']+)''\\) TO \\(''([^'']+)''\\)'))[2]
	   FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
	   WHERE i.inhparent = '$1'::regclass ORDER BY 1;"
}

# ---- from nothing -----------------------------------------------------------
q "CREATE TABLE tl (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
made="$(q "SELECT pgcolumnar.partition_create_ahead('tl', '1 day', 3);")"
check_num "from no partitions it creates enough to reach now plus the lead" \
	"$made" "$(q "SELECT 4;")"
check_num "the parent now has the partitions it reported" "$(kids tl)" "$made"
check "every partition it creates is columnar" "$(ams tl)" "pgcolumnar"
check "they are contiguous, each one width wide" \
	"$(bounds tl | awk -F'|' 'NR>1 && $1!=prev {gap++} {prev=$2} END {print gap+0}')|$(q "SELECT count(DISTINCT upper - lower) FROM (SELECT (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'TO \\(''([^'']+)''\\)'))[1]::timestamptz AS upper, (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'FROM \\(''([^'']+)''\\)'))[1]::timestamptz AS lower FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'tl'::regclass) b;")" \
	"0|1"
check "and they cover now() plus the lead" \
	"$(q "SELECT bool_or(now() + interval '3 days' < (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'TO \\(''([^'']+)''\\)'))[1]::timestamptz
	             AND now() + interval '3 days' >= (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'FROM \\(''([^'']+)''\\)'))[1]::timestamptz)
	      FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'tl'::regclass;")" "t"

# ---- idempotent, and it continues from the last bound ----------------------
check_num "a second call creates nothing" \
	"$(q "SELECT pgcolumnar.partition_create_ahead('tl', '1 day', 3);")" "0"
check_num "asking further ahead creates only the difference" \
	"$(q "SELECT pgcolumnar.partition_create_ahead('tl', '1 day', 5);")" "2"

# ---- rows go in, columnar ------------------------------------------------------
q "INSERT INTO tl SELECT now() + g * interval '1 hour', g FROM generate_series(0, 100) g;" >/dev/null
check_num "rows within the covered range go in" "$(q "SELECT count(*) FROM tl;")" "101"
check "and land in columnar partitions" \
	"$(q "SELECT string_agg(DISTINCT a.amname, ',') FROM tl t JOIN pg_class c ON c.oid = t.tableoid
	      JOIN pg_am a ON a.oid = c.relam;")" "pgcolumnar"

# ---- the parent lock ----------------------------------------------------------
q "CREATE TABLE tk (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
# The call and the lock reading share one transaction, and the arm requires the
# call to have RETURNED: in the red run before the function existed, the failed
# call aborted the transaction, the pg_locks query returned nothing, and an arm
# reading "no AccessExclusiveLock" passed on a function that did not exist.
out="$(q "BEGIN; SELECT 'made=' || pgcolumnar.partition_create_ahead('tk', '1 hour', 6);
	SELECT 'locks=' || coalesce(string_agg(DISTINCT mode, ',' ORDER BY mode), 'none') FROM pg_locks
	 WHERE relation = 'tk'::regclass AND pid = pg_backend_pid(); ROLLBACK;")"
tk_made="$(printf '%s\n' "$out" | sed -n 's/^made=//p')"
lock="$(printf '%s\n' "$out" | sed -n 's/^locks=//p')"
echo "-- created $tk_made; locks held on the parent: ${lock:-<unread>}"
check_num "premise: the call inside the transaction created partitions" "$(at_least "$tk_made" 1)" "1"
check "it never takes AccessExclusiveLock on the parent" \
	"$(case "$lock" in '') echo unread;; *AccessExclusiveLock*) echo "held: $lock";; *) echo none;; esac)" "none"

# ---- the session's time zone ----------------------------------------------------
# The bounds are read back as text and continued from. For a `timestamp` key (no
# zone) the text carries no offset, so reading it in one zone and writing the next
# bound in another leaves a gap or an overlap. Each arm continues from a partition
# made by hand, in a session whose zone is not UTC, and requires every partition's
# lower bound to be exactly the previous one's upper bound.
contiguous() {	# contiguous PARENT -> "ok", or the first break
	bounds "$1" | awk -F'|' 'NR>1 && $1!=prev {print "break: " prev " then " $1; bad=1; exit} {prev=$2} END {if (!bad) print "ok"}'
}
for key in timestamp timestamptz; do
	t="tz_$key"
	q "CREATE TABLE $t (ts $key NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
	q "SET TimeZone = 'America/New_York';
	   CREATE TABLE ${t}_seed PARTITION OF $t FOR VALUES
	   FROM (date_trunc('day', now() - interval '2 days')::$key) TO (date_trunc('day', now() - interval '1 day')::$key)
	   USING pgcolumnar;" >/dev/null
	tz_made="$(q "SET TimeZone = 'America/New_York'; SELECT pgcolumnar.partition_create_ahead('$t', '1 day', 2);" | tail -1)"
	check_num "premise: a $key parent continued from a hand-made partition, in a session west of UTC" \
		"$(at_least "$tz_made" 3)" "3"
	check "a $key parent's partitions are contiguous across a non-UTC session" \
		"$(q "SET TimeZone = 'America/New_York'; SELECT 1;" >/dev/null; contiguous "$t")" "ok"
	check_num "and a row at every boundary has exactly one partition to go to ($key)" \
		"$(q "SET TimeZone = 'America/New_York';
		      INSERT INTO $t SELECT (date_trunc('day', now() - interval '1 day') + g * interval '1 day')::$key, g
		      FROM generate_series(0, 2) g; SELECT count(*) FROM $t;" | tail -1)" "3"
done

# A `timestamp` key holds local wall-clock time, so "now" for it is the session's
# localtimestamp. East of UTC that is ahead of UTC's: a function that reasoned in
# UTC would stop nine hours short in Tokyo.
q "CREATE TABLE tz_tokyo (ts timestamp NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
tk2="$(q "SET TimeZone = 'Asia/Tokyo'; SELECT pgcolumnar.partition_create_ahead('tz_tokyo', '1 day', 2);" | tail -1)"
echo "-- Tokyo local now: $(q "SET TimeZone = 'Asia/Tokyo'; SELECT localtimestamp;" | tail -1); bounds: $(bounds tz_tokyo | tr '\n' ' ')"
check_num "premise: a timestamp parent was given partitions from a session east of UTC" "$(at_least "$tk2" 1)" "1"
check "a timestamp parent made east of UTC covers the local now plus the lead" \
	"$(q "SET TimeZone = 'Asia/Tokyo';
	      SELECT bool_or(localtimestamp + interval '2 days' >= (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'FROM \\(''([^'']+)''\\)'))[1]::timestamp
	                 AND localtimestamp + interval '2 days' < (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'TO \\(''([^'']+)''\\)'))[1]::timestamp)
	      FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'tz_tokyo'::regclass;" | tail -1)" "t"

check "and its partitions start on the width's boundary in the key's own wall clock" \
	"$(bounds tz_tokyo | awk -F'|' '$1 !~ / 00:00:00$/ {print "not midnight: " $1; bad=1; exit} END {if (!bad) print "ok"}')" "ok"

# ---- inputs that broke the first version (@jdatcmd, #1308 review) ---------------
# A stored generated column: LIKE without INCLUDING GENERATED made it a plain
# column, and ATTACH refused the partition.
q "CREATE TABLE gen (ts timestamptz NOT NULL, v int, w int GENERATED ALWAYS AS (v * 2) STORED)
   PARTITION BY RANGE (ts);" >/dev/null
gen_made="$(sqlstate_or "SELECT pgcolumnar.partition_create_ahead('gen', '1 day', 1);")"
check "a parent with a generated column gets its partitions, and the column stays generated" \
	"$gen_made|$(q "INSERT INTO gen VALUES (now(), 21); SELECT w FROM gen;" | tail -1)" "2|42"

# A 63-byte name, the longest an identifier holds: the 16-byte date suffix must
# survive, or the second partition's name truncates onto the first's.
LONG="p_$(printf 'x%.0s' $(seq 1 61))"
q "CREATE TABLE $LONG (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
long_made="$(sqlstate_or "SELECT pgcolumnar.partition_create_ahead('$LONG', '1 day', 2);")"
check "a parent with a 63-byte name gets partitions whose names keep their dates" \
	"$long_made|$(q "SELECT count(*) FILTER (WHERE c.relname ~ '_[0-9]{8}T[0-9]{6}\$' AND octet_length(c.relname) <= 63)
	   FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = '$LONG'::regclass;")" "3|3"

# No bound on the work: one call with a tiny width ran for minutes holding locks,
# then failed on the lock table with nothing created. Refused before any DDL.
q "CREATE TABLE many (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
t0=$(date +%s%N)
many="$(sqlstate_or "SELECT pgcolumnar.partition_create_ahead('many', '1 second', 20000);")"
many_ms=$(( ($(date +%s%N) - t0) / 1000000 ))
check "a call that would create more than 1000 partitions is refused before any DDL" \
	"$many|$(kids many)|$([ "$many_ms" -lt 5000 ] && echo prompt || echo "took $many_ms ms")" "22023|0|prompt"

# A partition bounded by MAXVALUE: the bound reader ignored it, and the first new
# partition overlapped it.
q "CREATE TABLE tail (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);
   CREATE TABLE tail_rest PARTITION OF tail FOR VALUES FROM ('2026-01-01') TO (MAXVALUE) USING pgcolumnar;" >/dev/null
check "a parent with a partition bounded by MAXVALUE is refused, and nothing is created" \
	"$(sqlstate_or "SELECT pgcolumnar.partition_create_ahead('tail', '1 day', 2);")|$(kids tail)" "55000|1"

# ---- refused before any DDL, each by its own check (#1341) ------------------------
# The MESSAGE is in the want, not just the code: with the width check gone, a zero
# or negative width never moves the counting loop on, and the 1000-partition cap
# answers with the same 22023.
q "CREATE TABLE wa (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
check "a width that is not positive, or a negative lead, is refused before any DDL" \
	"$(refusal "SELECT pgcolumnar.partition_create_ahead('wa', '0 seconds', 1);")|$(refusal "SELECT pgcolumnar.partition_create_ahead('wa', '-1 day', 1);")|$(refusal "SELECT pgcolumnar.partition_create_ahead('wa', '1 day', -1);")|$(kids wa)" \
	"22023 width must be positive and ahead not negative|22023 width must be positive and ahead not negative|22023 width must be positive and ahead not negative|0"
q "CREATE TABLE lp (ts timestamptz NOT NULL, v int) PARTITION BY LIST (ts);
   CREATE TABLE mc (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts, v);
   CREATE TABLE np (ts timestamptz NOT NULL, v int) USING pgcolumnar;" >/dev/null
check "a parent that is not RANGE-partitioned on one column is refused before any DDL" \
	"$(refusal "SELECT pgcolumnar.partition_create_ahead('lp', '1 day', 1);")|$(refusal "SELECT pgcolumnar.partition_create_ahead('mc', '1 day', 1);")|$(refusal "SELECT pgcolumnar.partition_create_ahead('np', '1 day', 1);")|$(kids lp)|$(kids mc)" \
	"55000 $(rc lp) is not RANGE-partitioned on one column|55000 $(rc mc) is not RANGE-partitioned on one column|55000 $(rc np) is not RANGE-partitioned on one column|0|0"
q "CREATE TABLE kd (d date NOT NULL, v int) PARTITION BY RANGE (d);
   CREATE TABLE ki (k bigint NOT NULL, v int) PARTITION BY RANGE (k);" >/dev/null
check "a parent partitioned on a key that is not a timestamp is refused before any DDL" \
	"$(refusal "SELECT pgcolumnar.partition_create_ahead('kd', '1 day', 1);")|$(refusal "SELECT pgcolumnar.partition_create_ahead('ki', '1 day', 1);")|$(kids kd)|$(kids ki)" \
	"55000 $(rc kd) is partitioned on date, not on a timestamp|55000 $(rc ki) is partitioned on bigint, not on a timestamp|0|0"

# ---- month and year widths (#1341) ------------------------------------------------
# date_bin cannot bin by months or years, so a width holding either starts at the
# first of the current month, and each bound is the last one plus the width: the
# calendar's month, not a fixed 30 days.
for key in timestamptz timestamp; do
	q "CREATE TABLE mo_$key (ts $key NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
	nowexpr="now()"; [ "$key" = timestamp ] && nowexpr="localtimestamp"
	check "a $key parent with a month width starts at this month's first and steps by calendar months" \
		"$(sqlstate_or "SELECT pgcolumnar.partition_create_ahead('mo_$key', '1 month', 2);")|$(q "SELECT count(*) FILTER (WHERE f = date_trunc('month', f) AND t = f + interval '1 month') || '|' || (min(f) = date_trunc('month', $nowexpr))
		   FROM (SELECT (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'FROM \(''([^'']+)''\) TO \(''([^'']+)''\)'))[1]::$key AS f,
		                (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'FROM \(''([^'']+)''\) TO \(''([^'']+)''\)'))[2]::$key AS t
		         FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'mo_$key'::regclass) b;")" \
		"3|3|true"
done
q "CREATE TABLE yr (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);" >/dev/null
check "a year width starts at this month's first and steps by calendar years" \
	"$(sqlstate_or "SELECT pgcolumnar.partition_create_ahead('yr', '1 year', 1);")|$(q "SELECT count(*) FILTER (WHERE f = date_trunc('month', f) AND t = f + interval '1 year') || '|' || (min(f) = date_trunc('month', now()))
	   FROM (SELECT (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'FROM \(''([^'']+)''\) TO \(''([^'']+)''\)'))[1]::timestamptz AS f,
	                (regexp_match(pg_get_expr(c.relpartbound, c.oid), 'FROM \(''([^'']+)''\) TO \(''([^'']+)''\)'))[2]::timestamptz AS t
	         FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'yr'::regclass) b;")" \
	"2|2|true"

# ---- refused: a DEFAULT partition -------------------------------------------------
q "CREATE TABLE td (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);
   CREATE TABLE td_default PARTITION OF td DEFAULT USING pgcolumnar;" >/dev/null
check "a parent with a DEFAULT partition is refused" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	   -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate \
	   -c "SELECT pgcolumnar.partition_create_ahead('td', '1 day', 2)" 2>&1 >/dev/null |
	   sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p')" "55000"
# Only a claim when the function exists: with it absent, nothing is created
# for any parent, and this arm passed in the red run.
check "and nothing was created for it" \
	"$(kids td)|$(q "SELECT count(*) FROM pg_proc WHERE proname = 'partition_create_ahead';")" "1|1"

# ---- the caller's own privileges (#1341) -------------------------------------
#
# partition_create_ahead is not SECURITY DEFINER, so its CREATE TABLE ... PARTITION OF
# runs as the caller, and core refuses a role that does not own the parent. Made
# SECURITY DEFINER, it would run as the extension's owner and let any caller add
# partitions, and the access-exclusive lock that comes with them, to a table it does
# not own. Both roles hold USAGE and CREATE on the schema, so ownership is all that
# separates them.
for r in pl_own pl_peer; do
	psql_run "DROP ROLE IF EXISTS $r; CREATE ROLE $r LOGIN NOSUPERUSER;
	          GRANT USAGE, CREATE ON SCHEMA public TO $r; GRANT USAGE ON SCHEMA pgcolumnar TO $r;"
done
as_role() {	# as_role ROLE SQL -> the single value SQL returns as ROLE, or the SQLSTATE it fails with
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$2" 2>&1)
	case "$out" in
		*ERROR:*|*FATAL:*) sed -n 's/^\(ERROR\|FATAL\): *\([0-9A-Z]\{5\}\).*/\2/p' <<<"$out" | head -1 ;;
		*) printf '%s\n' "$out" | tail -1 ;;
	esac
}
# The owner's control has a parent of its own (pw2), so a refusal that let the peer
# through cannot make the control fail for the peer's reason.
q "CREATE TABLE pv (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts); ALTER TABLE pv OWNER TO pl_own;
   CREATE TABLE pw2 (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts); ALTER TABLE pw2 OWNER TO pl_own;" >/dev/null
check "premise: pl_own owns the parent, and neither role is a superuser" \
	"$(q "SELECT string_agg(DISTINCT pg_get_userbyid(relowner), ',') FROM pg_class WHERE oid IN ('pv'::regclass, 'pw2'::regclass);")|$(q "SELECT count(*) FROM pg_roles WHERE rolname IN ('pl_own', 'pl_peer') AND rolsuper;")" \
	"pl_own|0"
check "premise: pl_peer can open a session" "$(as_role pl_peer "SELECT 1")" "1"
check "a role that does not own the parent is refused by its own privileges (42501), and creates nothing" \
	"$(as_role pl_peer "SELECT pgcolumnar.partition_create_ahead('pv', '1 day', 2)")|$(kids pv)" "42501|0"
check "a non-superuser owner creates partitions ahead on its own table" \
	"$(as_role pl_own "SELECT pgcolumnar.partition_create_ahead('pw2', '1 day', 2)")|$(kids pw2)" "3|3"

# ==== the drop half: partition_drop_plan (#1297) ================================
#
#   partition_drop_plan(parent regclass, cutoff timestamptz) -> SETOF text
#
# The statements, schema-qualified, that retire every partition whose whole range
# is older than cutoff, in range order: DETACH ... CONCURRENTLY, then DROP. They cannot run
# inside a function (DETACH CONCURRENTLY refuses any function, DO block or
# procedure, on every major: #1297 note 1), so the function RETURNS them and
# the caller runs each as its own top-level statement (\gexec). A partition a
# concurrent detach left pending gets DETACH ... FINALIZE instead.
plan() { q "SELECT string_agg(s, E'\n') FROM pgcolumnar.partition_drop_plan('$1', '$2') s;"; }
# The drop each partition gets: a DO block that refuses (55000) while the partition
# is still attached, so a DETACH that failed cannot be followed by a DROP that takes
# ACCESS EXCLUSIVE on the parent (jdatcmd, #1331 review).
sqlit() { local x="${1//\'/\'\'}"; printf "'%s'" "$x"; }	# a SQL string literal
gdrop() {	# gdrop 'TABLE'|'FOREIGN TABLE' QUALIFIED-NAME -> the planned statement
	# The body is a string literal, not dollar-quoted, so no name can end it.
	local body
	body="BEGIN IF EXISTS (SELECT 1 FROM pg_catalog.pg_inherits WHERE inhrelid = $(sqlit "$2")::regclass) THEN RAISE EXCEPTION '% is still attached to its parent; its DETACH did not finish', $(sqlit "$2") USING ERRCODE = '55000'; END IF; DROP $1 $2; END"
	printf 'DO %s' "$(sqlit "$body")"
}
run_plan() {	# each statement top-level, as \gexec would run it
	local line
	while IFS= read -r line; do
		[ -n "$line" ] && env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -X -q -v ON_ERROR_STOP=1 -c "$line" >/dev/null 2>&1 || echo "failed: $line"
	done <<<"$(plan "$1" "$2")"
}
q "CREATE TABLE dl (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);
   CREATE TABLE dl_03 PARTITION OF dl FOR VALUES FROM ('2020-03-01 00:00+00') TO ('2020-04-01 00:00+00') USING pgcolumnar;
   CREATE TABLE dl_02 PARTITION OF dl FOR VALUES FROM ('2020-02-01 00:00+00') TO ('2020-03-01 00:00+00') USING pgcolumnar;
   CREATE TABLE dl_04 PARTITION OF dl FOR VALUES FROM ('2020-04-01 00:00+00') TO ('2020-05-01 00:00+00') USING pgcolumnar;
   CREATE TABLE dl_01 PARTITION OF dl FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-02-01 00:00+00') USING pgcolumnar;
   INSERT INTO dl SELECT '2020-01-01 00:00+00'::timestamptz + g * interval '1 hour', g FROM generate_series(0, 2903) g;" >/dev/null
# Created out of range order, so a plan in catalog order is not a plan in range order.
check "premise: the drop fixture has several partitions, each holding rows" \
	"$(q "SELECT count(DISTINCT tableoid) FROM dl;")|$(kids dl)" "4|4"
check "the plan retires the partitions wholly before the cutoff, in range order" \
	"$(plan dl '2020-03-15 00:00+00')" \
	"$(printf '%s\n' 'ALTER TABLE public.dl DETACH PARTITION public.dl_01 CONCURRENTLY' "$(gdrop TABLE public.dl_01)" \
	                 'ALTER TABLE public.dl DETACH PARTITION public.dl_02 CONCURRENTLY' "$(gdrop TABLE public.dl_02)")"
check "a cutoff on a partition's upper bound includes it" \
	"$(q "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dl', '2020-03-01 00:00+00') s WHERE s LIKE 'DO %';")" "2"
check "a cutoff before every partition plans nothing" \
	"$(q "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dl', '2019-06-01 00:00+00');")" "0"
sids="$(q "SELECT string_agg(pgcolumnar.get_storage_id(c)::text, ',') FROM unnest(ARRAY['dl_01','dl_02']::regclass[]) c;")"
run_plan dl '2020-03-15 00:00+00'
check "running the plan leaves only the partitions it did not name" \
	"$(q "SELECT string_agg(c.relname, ',' ORDER BY c.relname) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
	      WHERE i.inhparent = 'dl'::regclass;")|$(q "SELECT count(*) FROM pg_class WHERE relname IN ('dl_01', 'dl_02');")" "dl_03,dl_04|0"
check "and every row the remaining partitions held is still there" \
	"$(q "SELECT count(*) FROM dl;")" "$(q "SELECT count(*) FROM generate_series(0, 2903) g
	   WHERE '2020-01-01 00:00+00'::timestamptz + g * interval '1 hour' >= '2020-03-01 00:00+00';")"
check "and the dropped partitions' pgcolumnar storage rows are gone" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id IN (${sids:-0});")" "0"
check "a second plan for the same cutoff is empty, and the partitions it kept are still attached" \
	"$(q "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dl', '2020-03-15 00:00+00');")|$(kids dl)" "0|2"

# The cutoff's type must be the key's (jdatcmd, #1297): a timestamp bound compared
# with a timestamptz cutoff converts in the session's zone, so the same call could
# plan differently from two sessions. A cutoff of the other type is refused
# (42804), and a matching one plans the same everywhere.
q "CREATE TABLE dw (ts timestamp NOT NULL) PARTITION BY RANGE (ts);
   CREATE TABLE dw_a PARTITION OF dw FOR VALUES FROM ('2021-01-01') TO ('2021-01-02') USING pgcolumnar;
   CREATE TABLE dw_b PARTITION OF dw FOR VALUES FROM ('2021-01-02') TO ('2021-01-03') USING pgcolumnar;" >/dev/null
plan_in() {	# plan_in ZONE PARENT CUTOFF-EXPRESSION
	env PGTZ="$1" PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-c "SELECT coalesce(string_agg(s, ';'), 'none') FROM pgcolumnar.partition_drop_plan('$2', $3) s" 2>&1
}
check "a timestamp key's plan for a timestamp cutoff is the same from sessions west and east of UTC" \
	"$(plan_in America/Los_Angeles dw "'2021-01-02 00:00'::timestamp")|$(plan_in Asia/Kolkata dw "'2021-01-02 00:00'::timestamp")" \
	"ALTER TABLE public.dw DETACH PARTITION public.dw_a CONCURRENTLY;$(gdrop TABLE public.dw_a)|ALTER TABLE public.dw DETACH PARTITION public.dw_a CONCURRENTLY;$(gdrop TABLE public.dw_a)"
check "a cutoff of the other timestamp type is refused" \
	"$(sqlstate_or "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dw', '2021-01-02 00:00+00'::timestamptz)")|$(sqlstate_or "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dl', '2020-03-01 00:00'::timestamp)")" \
	"42804|42804"
check "a NULL cutoff is refused, in both overloads" \
	"$(sqlstate_or "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dl', NULL::timestamptz)")|$(sqlstate_or "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dw', NULL::timestamp)")" \
	"22023|22023"

# Bounds at the ends of the range: MINVALUE below is listed once its upper bound
# is past, MAXVALUE above never is.
q "CREATE TABLE dm (ts timestamptz NOT NULL) PARTITION BY RANGE (ts);
   CREATE TABLE dm_lo PARTITION OF dm FOR VALUES FROM (MINVALUE) TO ('2020-01-01 00:00+00') USING pgcolumnar;
   CREATE TABLE dm_mid PARTITION OF dm FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-02-01 00:00+00') USING pgcolumnar;
   CREATE TABLE dm_hi PARTITION OF dm FOR VALUES FROM ('2020-02-01 00:00+00') TO (MAXVALUE) USING pgcolumnar;" >/dev/null
check "a partition from MINVALUE is listed once its upper bound is past, one to MAXVALUE never" \
	"$(q "SELECT string_agg(s, ';') FROM pgcolumnar.partition_drop_plan('dm', '2020-01-01 00:00+00') s WHERE s LIKE 'DO %';")|$(q "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dm', 'infinity') s WHERE s LIKE '%dm_hi%';")" \
	"$(gdrop TABLE public.dm_lo)|0"

# Names that need quoting, in a schema that needs quoting: the plan must run as
# returned (#1317 was caught by exactly this).
q "CREATE SCHEMA \"Drop Zone\";
   CREATE TABLE \"Drop Zone\".\"Metrics\" (ts timestamptz NOT NULL) PARTITION BY RANGE (ts);
   CREATE TABLE \"Drop Zone\".\"Old Part\" PARTITION OF \"Drop Zone\".\"Metrics\"
     FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-02-01 00:00+00') USING pgcolumnar;
   CREATE TABLE \"Drop Zone\".\"New Part\" PARTITION OF \"Drop Zone\".\"Metrics\"
     FOR VALUES FROM ('2020-02-01 00:00+00') TO ('2020-03-01 00:00+00') USING pgcolumnar;" >/dev/null
# Every statement must run: a DETACH that fails on a bad name still leaves the
# DROP to remove an attached partition (under ACCESS EXCLUSIVE on the parent), so
# the tables left alone cannot tell a quoting bug apart.
quoted_failures="$(run_plan '"Drop Zone"."Metrics"' '2020-02-01 00:00+00')"
check "a plan for names that need quoting runs as returned" \
	"${quoted_failures:-every statement ran}|$(q "SELECT string_agg(c.relname, ',' ORDER BY c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
	      WHERE n.nspname = 'Drop Zone' AND c.relkind IN ('r', 'p');")" "every statement ran|Metrics,New Part"

# A partition that is itself partitioned is listed whole: the concurrent detach
# takes it with its children, and DROP drops them.
q "CREATE TABLE ds (ts timestamptz NOT NULL) PARTITION BY RANGE (ts);
   CREATE TABLE ds_q1 PARTITION OF ds FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-04-01 00:00+00') PARTITION BY RANGE (ts);
   CREATE TABLE ds_q1_a PARTITION OF ds_q1 FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-02-15 00:00+00') USING pgcolumnar;
   CREATE TABLE ds_q1_b PARTITION OF ds_q1 FOR VALUES FROM ('2020-02-15 00:00+00') TO ('2020-04-01 00:00+00') USING pgcolumnar;
   CREATE TABLE ds_q2 PARTITION OF ds FOR VALUES FROM ('2020-04-01 00:00+00') TO ('2020-07-01 00:00+00') USING pgcolumnar;
   INSERT INTO ds SELECT '2020-01-01 00:00+00'::timestamptz + g * interval '1 day' FROM generate_series(0, 180) g;" >/dev/null
check "a partition that is itself partitioned is planned whole" \
	"$(plan ds '2020-04-01 00:00+00' | paste -sd';')" \
	"ALTER TABLE public.ds DETACH PARTITION public.ds_q1 CONCURRENTLY;$(gdrop TABLE public.ds_q1)"
run_plan ds '2020-04-01 00:00+00'
check "and running the plan drops it with its children" \
	"$(q "SELECT count(*) FROM pg_class WHERE relname IN ('ds_q1', 'ds_q1_a', 'ds_q1_b');")|$(q "SELECT count(*) FROM ds;")" \
	"0|$(q "SELECT count(*) FROM generate_series(0, 180) g WHERE '2020-01-01 00:00+00'::timestamptz + g * interval '1 day' >= '2020-04-01 00:00+00';")"

# A partition tier_partition moved to Parquet (#1295) is a foreign table: it is
# listed like any other, dropped with DROP FOREIGN TABLE, and its file stays.
psql_run "CREATE SERVER drop_pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"
q "CREATE TABLE dt (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);
   CREATE TABLE dt_jan PARTITION OF dt FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-02-01 00:00+00') USING pgcolumnar;
   CREATE TABLE dt_feb PARTITION OF dt FOR VALUES FROM ('2020-02-01 00:00+00') TO ('2020-03-01 00:00+00') USING pgcolumnar;
   INSERT INTO dt SELECT '2020-01-01 00:00+00'::timestamptz + g * interval '1 hour', g FROM generate_series(0, 1400) g;" >/dev/null
mkdir -p "$PGC_WORKDIR/drop" && chmod 777 "$PGC_WORKDIR/drop"
check "premise: the January partition was tiered to Parquet" \
	"$(q "SELECT pgcolumnar.tier_partition('dt_jan', 'drop_pq', '$PGC_WORKDIR/drop/dt_jan.parquet');")|$(q "SELECT relkind FROM pg_class WHERE relname = 'dt_jan';")" "744|f"
check "a tiered partition is planned with DROP FOREIGN TABLE" \
	"$(plan dt '2020-02-01 00:00+00' | paste -sd';')" \
	"ALTER TABLE public.dt DETACH PARTITION public.dt_jan CONCURRENTLY;$(gdrop 'FOREIGN TABLE' public.dt_jan)"
run_plan dt '2020-02-01 00:00+00'
check "and running it drops the foreign table and leaves its Parquet file" \
	"$(q "SELECT count(*) FROM pg_class WHERE relname = 'dt_jan';")|$([ -s "$PGC_WORKDIR/drop/dt_jan.parquet" ] && echo file-kept || echo file-gone)" "0|file-kept"

# An interrupted concurrent detach leaves the partition pending: a transaction
# holding an older snapshot makes the second phase wait, and a statement timeout
# cancels it there.
q "CREATE TABLE dp (ts timestamptz NOT NULL) PARTITION BY RANGE (ts);
   CREATE TABLE dp_1 PARTITION OF dp FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-02-01 00:00+00') USING pgcolumnar;
   CREATE TABLE dp_2 PARTITION OF dp FOR VALUES FROM ('2020-02-01 00:00+00') TO ('2020-03-01 00:00+00') USING pgcolumnar;" >/dev/null
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -q \
	-c "BEGIN ISOLATION LEVEL REPEATABLE READ" -c "SELECT count(*) FROM dp" -c "SELECT pg_sleep(8)" -c "COMMIT" >/dev/null 2>&1 &
holder=$!
sleep 1
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -q \
	-c "SET statement_timeout = '2s'" -c "ALTER TABLE dp DETACH PARTITION dp_1 CONCURRENTLY" >/dev/null 2>&1
wait "$holder"
check "premise: the interrupted detach left the partition pending" \
	"$(q "SELECT inhdetachpending FROM pg_inherits WHERE inhrelid = 'dp_1'::regclass;")" "t"
check "a pending detach is finalised, not started again" \
	"$(q "SELECT string_agg(s, ';') FROM pgcolumnar.partition_drop_plan('dp', '2020-02-01 00:00+00') s;")" \
	"ALTER TABLE public.dp DETACH PARTITION public.dp_1 FINALIZE;$(gdrop TABLE public.dp_1)"
run_plan dp '2020-02-01 00:00+00'
check "and running it drops the pending partition" \
	"$(q "SELECT count(*) FROM pg_class WHERE relname = 'dp_1';")|$(kids dp)" "0|1"

# A DETACH that fails -- here a lock_timeout while another session holds the
# parent -- must not be followed by a DROP of the still-attached partition, which
# would take ACCESS EXCLUSIVE on the parent and block every query on it. \gexec
# carries on after an error unless ON_ERROR_STOP is set, so the plan itself has
# to be safe. Each statement's SQLSTATE, in order.
q "CREATE TABLE df (ts timestamptz NOT NULL) PARTITION BY RANGE (ts);
   CREATE TABLE df_1 PARTITION OF df FOR VALUES FROM ('2020-01-01 00:00+00') TO ('2020-02-01 00:00+00') USING pgcolumnar;
   CREATE TABLE df_2 PARTITION OF df FOR VALUES FROM ('2020-02-01 00:00+00') TO ('2020-03-01 00:00+00') USING pgcolumnar;" >/dev/null
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -q \
	-c "BEGIN" -c "LOCK TABLE df IN SHARE UPDATE EXCLUSIVE MODE" -c "SELECT pg_sleep(4)" -c "COMMIT" >/dev/null 2>&1 &
holder=$!
sleep 1
stmt_state() {	# stmt_state SQL [PGOPTIONS] -> ok, or the SQLSTATE it failed with
	local out
	out="$(env PGOPTIONS="${2:-}" PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -X -q -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1 >/dev/null)"
	[ -z "$out" ] && echo ok || sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1
}
mapfile -t df_plan <<<"$(plan df '2020-02-01 00:00+00')"
# The DETACH times out while the holder has the parent; the holder then lets go,
# so the DROP that follows finds nothing in its way but its own guard.
states="$(stmt_state "${df_plan[0]}" '-c lock_timeout=1500')"
wait "$holder"
states="$states;$(stmt_state "${df_plan[1]}")"
check "after a DETACH that fails, the plan's DROP refuses and the partition stays attached" \
	"$states|$(kids df)|$(q "SELECT count(*) FROM pg_inherits WHERE inhrelid = 'df_1'::regclass;")" "55P03;55000|2|1"

# A partition whose name holds the DO block's own dollar-quote tag (jdatcmd, #1331
# review): the guarded drop must still be one statement that runs as returned.
q 'CREATE TABLE dq (ts timestamptz NOT NULL) PARTITION BY RANGE (ts);
   CREATE TABLE "dq$pgcolumnar_drop$1" PARTITION OF dq FOR VALUES FROM ('"'2020-01-01 00:00+00'"') TO ('"'2020-02-01 00:00+00'"') USING pgcolumnar;
   CREATE TABLE dq_2 PARTITION OF dq FOR VALUES FROM ('"'2020-02-01 00:00+00'"') TO ('"'2020-03-01 00:00+00'"') USING pgcolumnar;' >/dev/null
check "premise: a partition is named with the drop block's dollar-quote tag" \
	"$(q "SELECT count(*) FROM pg_class WHERE relname = 'dq\$pgcolumnar_drop\$1';")" "1"
dq_failures="$(run_plan dq '2020-02-01 00:00+00')"
check "a partition named with the drop block's dollar-quote tag is dropped by the plan as returned" \
	"${dq_failures:-every statement ran}|$(q "SELECT count(*) FROM pg_class WHERE relname = 'dq\$pgcolumnar_drop\$1';")|$(kids dq)" \
	"every statement ran|0|1"

# Refusals, as partition_create_ahead refuses.
check "a drop plan for a parent with a DEFAULT partition is refused" \
	"$(sqlstate_or "SELECT count(*) FROM pgcolumnar.partition_drop_plan('td', now())")" "55000"
q "CREATE TABLE dn (k int NOT NULL) PARTITION BY RANGE (k);
   CREATE TABLE dnl (k int);
   CREATE TABLE dls (ts timestamptz NOT NULL) PARTITION BY LIST (ts);
   CREATE TABLE dr2 (ts timestamptz NOT NULL, k int NOT NULL) PARTITION BY RANGE (ts, k);" >/dev/null
# Each by its own check, so the message is in the want (#1339): both answer 55000.
check "a drop plan for a parent not partitioned on a timestamp is refused" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.partition_drop_plan('dn', now())")" \
	"55000 $(rc dn) is partitioned on integer, not on a timestamp"
# The RANGE-on-one-column check had only an unpartitioned table to refuse, which
# NOT FOUND refuses alone. A LIST parent and a two-column RANGE parent, both led by
# a timestamptz, pass the key check: only strat and natts stop them.
check "a drop plan for a parent not RANGE-partitioned on one column is refused, LIST and two-column RANGE included" \
	"$(for t in dnl dls dr2; do refusal "SELECT count(*) FROM pgcolumnar.partition_drop_plan('$t', now())"; done | paste -sd'|')" \
	"55000 $(rc dnl) is not RANGE-partitioned on one column|55000 $(rc dls) is not RANGE-partitioned on one column|55000 $(rc dr2) is not RANGE-partitioned on one column"

pgc_summary

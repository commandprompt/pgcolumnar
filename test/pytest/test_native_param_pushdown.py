"""Chunk-group skipping for parameterized predicates. The pytest twin of
`test/native_param_pushdown.sh`, every check by the shell's name (#1341).

The scan-key builder accepts only a Const operand, so Begin freezes execution-stable
operands (a PARAM_EXTERN, or a subexpression with no Var, no PARAM_EXEC and no volatile
function) into Consts before building the keys. A correlated PARAM_EXEC (a nestloop
inner scan's reference to the outer row) changes per rescan and the keys are built
once, so it must NOT be frozen.

Three things the shell's first version could not see, now read in both twins:
- with the PARAM_EXEC guard removed the lower-bound correlated arm still answered
  3002, because Begin runs before any outer row sets the param and "ts >= <its
  initial value>" prunes nothing. The arm now reads the scan's own "Usable Skip
  Predicates" and asks the upper bound too, where a frozen initial value prunes all;
- every arm ran one execution per session, so a value frozen once and reused by a
  later execution passed. Two executions in one session now each count and prune
  with their own value;
- survival was a bare SELECT 1; it now also reads the server log's crash count.

An error raised while Begin freezes a stable operand must reach the client (#2065).
Begin caught it with PG_TRY and threw it away, with no subtransaction to release what
the failed call held: a statement_timeout during that evaluation was lost, the query
returned rows, and the failed call's plancache, snapshot and SPI state leaked as
WARNINGs. The functions misbehave on their FIRST call only, on a generic plan, where
the planner cannot fold "fn($1)", so that first call is the one the scan makes.

A scan that is set up but never run must not call the operand at all: an Append arm
that LIMIT cuts off, or a branch under a false one-time filter. A btree index scan
evaluates its runtime keys when it first runs, and heap per row, so both answer.
That includes a PL/pgSQL variable, whose parameter list has a fetch hook that
evaluates it. The scan freezes the operand once, at its first run, and hands a
projection scan the keys mapped onto the projection's own columns.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading. A failed query is a `query_error()`, so two failures never agree.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_param_pushdown"
CORR_GE = "SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts >= thr.lo) s"
CORR_LE = "SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts <= thr.lo) s"
GENERIC = "SET plan_cache_mode = force_generic_plan"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _await_up(dsn):
    for _ in range(60):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _session(dsn, statements):
    """Run `statements` in ONE session and return the rows of each, as text lists, or
    the failure as a query_error()."""
    _await_up(dsn)
    out = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in statements:
                cur = conn.execute(sql)
                rows = cur.fetchall() if cur.description else []
                out.append(["" if r[0] is None else str(r[0]) for r in rows])
    except psycopg.Error as exc:
        return _failed(exc)
    return out


def _value(dsn, sql):
    res = _session(dsn, [sql])
    if isinstance(res, str):
        return res
    return res[0][0] if res[0] else query_error(f"no row from {sql}")


def _field(lines, label):
    """-> the number after `label:` in an EXPLAIN, or why there is none."""
    if isinstance(lines, str):
        return lines
    for line in lines:
        m = re.search(re.escape(label) + r": (\d+)", line)
        if m:
            return m.group(1)
    return query_error(f"the plan has no {label} line")


FIRST_CALL = """CREATE FUNCTION {name}(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    DECLARE n int := coalesce(nullif(current_setting('t.n', true), ''), '0')::int + 1;
    BEGIN PERFORM set_config('t.n', n::text, false);
    IF n = 1 THEN {act}; END IF; RETURN $1; END $$"""

# boom() raises on every call. same() is PL/pgSQL so the planner cannot inline it into
# a bare parameter.
BOOM = """CREATE FUNCTION boom(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    BEGIN RAISE EXCEPTION 'boom called'; END $$"""
SAME = """CREATE FUNCTION same(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    BEGIN RETURN $1; END $$"""
# cntc() counts its calls in t.n and never raises. A function that raised would roll
# back its own count, so t.n would read 0 whether it was called or not.
CNTC = """CREATE FUNCTION cntc(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    DECLARE n int := coalesce(nullif(current_setting('t.n', true), ''), '0')::int + 1;
    BEGIN PERFORM set_config('t.n', n::text, false); RETURN $1; END $$"""
# A PL/pgSQL optional filter: with use_filter false the record r is never assigned,
# the plan's one-time filter is false, and the scan never runs.
OPT_EQ = """CREATE FUNCTION opt_eq(use_filter bool) RETURNS bigint LANGUAGE plpgsql AS $$
    DECLARE r record; c bigint;
    BEGIN
      IF use_filter THEN SELECT 39500 AS id INTO r; END IF;
      SELECT count(*) INTO c FROM t WHERE use_filter AND ts = r.id;
      RETURN c;
    END $$"""
# A PL/pgSQL variable is frozen only when the scan first runs. A plain EXPLAIN inside
# PL/pgSQL inlines the value, so auto_explain logs the generic plan the function ran.
LO_CNT = """CREATE FUNCTION lo_cnt(lo int) RETURNS bigint LANGUAGE plpgsql AS $$
    DECLARE c bigint; BEGIN SELECT count(*) INTO c FROM t WHERE ts >= lo; RETURN c; END $$"""
AUTO_EXPLAIN = ["LOAD 'auto_explain'", "SET auto_explain.log_min_duration = 0",
                "SET auto_explain.log_analyze = on", "SET auto_explain.log_timing = off",
                "SET auto_explain.log_nested_statements = on", "SET client_min_messages = log"]
PROJ_SET = ["SET pgcolumnar.enable_projection_scan = on", "SET max_parallel_workers_per_gather = 0",
            "SET pgcolumnar.enable_ungrouped_vector_agg = off",
            "SET pgcolumnar.enable_group_vectorization = off"]
PROJ_LOW = "SELECT count(*), sum(k) FROM prsc WHERE k >= 100 AND k <= same(900)"
PROJ_HIGH = "SELECT count(*), sum(k) FROM prsc WHERE k >= 19990 AND k <= same(20000)"
# A correlated subquery rescans the columnar scan once per outer row, and each rescan
# reuses the key frozen at the first run. A 300 kB text value gets its own allocation, so
# a key that still points at the freed result crashes the backend.
SAME_T = """CREATE FUNCTION same_t(text) RETURNS text LANGUAGE plpgsql STABLE AS $$
    BEGIN RETURN $1; END $$"""
# A hash join attaches its runtime range after Begin; the first run's keys must keep it.
# The hash builds on d (k 8001..8200) and probes f (20 groups of 1000). Heap answers
# 51|416925.
JOIN_SET = ["SET plan_cache_mode = force_generic_plan", "SET enable_nestloop = off",
            "SET enable_mergejoin = off", "SET max_parallel_workers_per_gather = 0"]
JOIN_Q = "PREPARE j(int) AS SELECT count(*), sum(f.k) FROM f JOIN d ON f.k = d.k WHERE f.k >= same($1)"
JOIN_EXPLAIN = "EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) EXECUTE j(8150)"
# A subquery runs the hash join once per group of d2, so each loop rescans it with a new
# runtime range. fh is a heap copy of f.
RESCAN_Q = ("PREPARE r(int) AS SELECT g.g || '|' || (SELECT count(*)::text || '|' || coalesce(sum({t}.k), 0) "
            "FROM {t} JOIN d2 ON {t}.k = d2.k AND d2.grp = g.g WHERE {t}.k >= same($1)) FROM generate_series(1, 2) g(g)")
RESCAN_WANT = "1|51|416925 2|200|3020100 1|51|416925 2|200|3020100"
TEXT_RESCAN = ("PREPARE r(text) AS SELECT g || '|' || (SELECT count(*) FROM tx WHERE s >= same_t($1) "
               "AND s <= g::text || 'z') FROM generate_series(1, 3) g")


def _transcript(dsn, statements):
    """Run `statements` in ONE session and return everything the client saw, one line
    per row, notice or raised error: "WARNING: ...", "raised: ...". The first raised
    error ends the session, as psql's -c sequence does not; nothing after it matters
    here. A raised error is the reading under test, compared with a fixed message; a
    session that cannot connect at all is a query_error()."""
    _await_up(dsn)
    lines = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: lines.append(f"{d.severity}: {d.message_primary}"))
            for sql in statements:
                try:
                    cur = conn.execute(sql)
                except psycopg.Error as exc:
                    lines.append(f"raised: {exc.diag.message_primary}")
                    break
                for row in (cur.fetchall() if cur.description else []):
                    lines.append("" if row[0] is None else str(row[0]))
    except psycopg.Error as exc:
        return _failed(exc)
    return lines


def _proj_answers(dsn):
    """-> "count|sum count|sum" for the two projection queries, as psql -At prints them."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in PROJ_SET:
                conn.execute(sql)
            return " ".join("|".join("" if v is None else str(v) for v in conn.execute(q).fetchone())
                            for q in (PROJ_LOW, PROJ_HIGH))
    except psycopg.Error as exc:
        return _failed(exc)


def _count(lines, needle):
    if isinstance(lines, str):
        return lines
    return str(sum(1 for line in lines if needle in line))


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    _await_up(dsn)
    return f"no ([{up}], {now - before} backend crashes)"


# Each boomc function counts its calls in the sequence ncalls and then raises. A sequence
# keeps its value when the query fails, so "COUNT|CALLS" shows a call even where main's
# code swallowed the error and still counted 0 (review of 5f90e108).
BOOMC = ["CREATE SEQUENCE ncalls"] + [
    f"""CREATE FUNCTION {name}(int) RETURNS {rettype} LANGUAGE plpgsql STABLE AS $$
    BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION '{name} called'; END $$"""
    for name, rettype in (("boomc", "int"), ("boomc_t", "text"), ("boomc_ts", "timestamp"),
                          ("boomc_arr", "int[]"), ("boomc_r", "tsrange"), ("boomc_tstz", "timestamptz"),
                          ("boomc_date", "date"), ("boomc_mr", "tsmultirange"),
                          ("boomc_tstzarr", "timestamptz[]"))]
# tz holds a timestamp domain column and a timestamptz column; no row matches its
# "< 2000-01-01" filters.
TZ = ["CREATE DOMAIN tsdom AS timestamp",
      "CREATE TABLE tz (tsd tsdom, tsz timestamptz) USING pgcolumnar",
      "INSERT INTO tz SELECT timestamp '2020-01-01' + g * interval '1 hour', "
      "timestamptz '2020-01-01' + g * interval '1 hour' FROM generate_series(1, 100) g",
      # trunc_k(text, int) has date_trunc's shape, a unit and a column, but is not date_trunc.
      """CREATE FUNCTION trunc_k(text, int) RETURNS timestamp LANGUAGE plpgsql IMMUTABLE AS $$
    BEGIN RETURN timestamp '2020-01-01' + $2 * interval '1 hour'; END $$"""]
NO_PARALLEL = "SET max_parallel_workers_per_gather = 0"
NONC_Q = ("SELECT collname FROM pg_collation "
          "WHERE collname IN ('en_US.utf8', 'en_US.UTF-8', 'en_US', 'und-x-icu') "
          "AND collencoding IN (-1, pg_char_to_encoding(current_setting('server_encoding'))) "
          "ORDER BY collname = 'und-x-icu', collname LIMIT 1")
# tk holds each clause shape the scan can key, 20 groups of 2000 rows. tkh is a heap copy.
TK = ["SET pgcolumnar.stripe_row_limit = 2000",
      "CREATE TABLE tk (k int, sc text COLLATE \"C\", sn text COLLATE \"{nonc}\", v varchar, tsp timestamp, "
      "r tsrange, tsz timestamptz, d date) USING pgcolumnar",
      "INSERT INTO tk SELECT g, lpad(g::text, 6, '0'), lpad(g::text, 6, '0'), lpad(g::text, 6, '0'), "
      "timestamp '2020-01-01' + g * interval '1 hour', "
      "tsrange(timestamp '2020-01-01' + g * interval '1 hour', timestamp '2020-01-01' + (g + 1) * interval '1 hour'), "
      "timestamptz '2020-01-01 00:00+00' + g * interval '1 hour', "
      "(timestamp '2020-01-01' + g * interval '1 hour')::date "
      "FROM generate_series(1, 40000) g",
      "CREATE TABLE tkh AS SELECT * FROM tk"]
# Clauses that can never be scan keys: ts < 0, tsp < '2000-01-01' and sc/sn < '' hold
# for no row, so heap never calls the operand. Each must give "0|0".
NOT_KEYABLE = (
    ("not keyable ne", "t WHERE ts < 0 AND ts <> boomc($1)"),
    ("not keyable expr", "t WHERE ts < 0 AND ts + 0 > boomc($1)"),
    ("not keyable like non-c", "tk WHERE sn < '' AND sn LIKE boomc_t($1)"),
    ("not keyable date_trunc ne", "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp) <> boomc_ts($1)"),
    ("not keyable date_trunc expr",
     "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp + interval '1 hour') = boomc_ts($1)"),
    ("not keyable date_trunc unit", "tk WHERE tsp < '2000-01-01' AND date_trunc(sc, tsp) = boomc_ts($1)"),
    ("not keyable like reversed", "tk WHERE sc < '' AND boomc_t($1) LIKE sc"),
    ("not keyable ne any", "t WHERE ts < 0 AND ts <> ANY(boomc_arr($1))"),
    ("not keyable eq all", "t WHERE ts < 0 AND ts = ALL(boomc_arr($1))"),
    ("not keyable range contains", "tk WHERE tsp < '2000-01-01' AND r @> boomc_r($1)"),
    ("not keyable other collation", "tk WHERE sn < '' AND sn > boomc_t($1) COLLATE \"C\""),
    ("not keyable abs", "t WHERE ts < 0 AND abs(ts) > boomc($1)"),
    # review of 27aa4c46: shapes a key builder declines
    ("not keyable date_trunc tstz operand", "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp) = boomc_tstz($1)"),
    ("not keyable date_trunc date operand", "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp) = boomc_date($1)"),
    ("not keyable date_trunc decade", "tk WHERE tsp < '2000-01-01' AND date_trunc('decade', tsp) = boomc_ts($1)"),
    ("not keyable date_trunc days", "tk WHERE tsp < '2000-01-01' AND date_trunc('days', tsp) = boomc_ts($1)"),
    ("not keyable date_trunc domain", "tz WHERE tsd < '2000-01-01' AND date_trunc('day', tsd) = boomc_ts($1)"),
    ("not keyable range multirange", "tk WHERE tsp < '2000-01-01' AND r && boomc_mr($1)"),
    ("not keyable date_trunc tstz column", "tz WHERE tsz < '2000-01-01' AND date_trunc('day', tsz) = boomc_tstz($1)"),
    ("not keyable two-argument function", "tk WHERE k < 0 AND trunc_k('day', k) = boomc_ts($1)"),
    # review of ff1fc62f: the reader drops a timestamp column against a timestamptz value (#1738)
    ("not keyable tstz scalar", "tk WHERE tsp < '2000-01-01' AND tsp > boomc_tstz($1)"),
    ("not keyable tstz any", "tk WHERE tsp < '2000-01-01' AND tsp = ANY(boomc_tstzarr($1))"),
    ("not keyable tstz domain column", "tz WHERE tsd < '2000-01-01' AND tsd > boomc_tstz($1)"),
    ("not keyable lt any", "tk WHERE k < 0 AND k < ANY(boomc_arr($1))"),
)
# Clauses that are keys, each with a bound parameter: (key, type, where on {t}, value).
PRUNE = (
    ("prune date_trunc", "timestamp", "{t} WHERE date_trunc('day', tsp) = $1", "'2024-07-01'"),
    ("prune like c", "text", "{t} WHERE sc LIKE $1", "'0390%'"),
    ("prune range overlap", "tsrange", "{t} WHERE r && $1", "'[2024-07-01,2024-07-02)'"),
    ("prune varchar", "text", "{t} WHERE v >= $1", "'039001'"),
    ("prune any", "int[]", "{t} WHERE k = ANY($1)", "'{39500,39600}'"),
)
# A bound parameter of a domain type, or varchar, reaches the scan inside a RelabelType;
# its frozen Const must take the relabelled type (review of ff1fc62f, attacker's blocker A).
DOMAINS = ["CREATE DOMAIN tdom AS text", "CREATE DOMAIN rdom AS tsrange"]
PRUNE3 = (
    ("prune varchar like", "varchar", "{t} WHERE sc LIKE $1", "'0390%'"),
    ("prune tdom like", "tdom", "{t} WHERE sc LIKE $1", "'0390%'"),
    ("prune tsdom date_trunc", "tsdom", "{t} WHERE date_trunc('day', tsp) = $1", "'2024-07-01'"),
    ("prune tsdom date_trunc ge", "tsdom", "{t} WHERE date_trunc('day', tsp) >= $1", "'2024-07-01'"),
    ("prune rdom overlap", "rdom", "{t} WHERE r && $1", "'[2024-07-01,2024-07-02)'"),
    ("prune date_trunc upper unit", "timestamp", "{t} WHERE date_trunc('Day', tsp) = $1", "'2024-07-01'"),
    ("prune tstz column", "timestamptz", "{t} WHERE tsz >= $1", "'2024-07-01 00:00+00'"),
    # the timestamptz decline must stay narrow (review of 164d61da)
    ("prune date operand", "date", "{t} WHERE tsp >= $1", "'2024-07-01'"),
    ("prune tstz on date column", "timestamptz", "{t} WHERE d >= $1", "'2024-07-01 00:00+00'"),
)
# prsc's rows are in random order, so its groups cannot prune; prsk holds the same rows
# in k order, so its projection's groups can.
PRSK = ["CREATE TABLE prsk (k int, payload text) USING pgcolumnar",
        "SELECT pgcolumnar.set_options('prsk', chunk_group_row_limit => 1000, stripe_row_limit => 1000)",
        "INSERT INTO prsk SELECT k, 'p' || k FROM generate_series(1, 20000) k",
        "SELECT pgcolumnar.add_projection('prsk', 'bykk', ARRAY['k'], ARRAY['k'])",
        "ANALYZE prsk"]
PRSK_EXPLAIN = ("EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) "
                "SELECT count(*), sum(k) FROM prsk WHERE k >= 100 AND k <= same(900)")
PRUNE2 = (
    ("prune date_trunc ge", "timestamp", "{t} WHERE date_trunc('day', tsp) >= $1", "'2024-07-01'"),
    ("prune range contains", "timestamp", "{t} WHERE r @> $1", "'2024-07-01 00:30'"),
)


def _nc_arm(dsn, where):
    """-> "COUNT|CALLS" for one EXECUTE of `SELECT count(*) FROM where`; COUNT is
    "raised: ..." if the query raised."""
    _session(dsn, ["SELECT setval('ncalls', 1, false)"])
    res = _transcript(dsn, [GENERIC, NO_PARALLEL, f"PREPARE nc(int) AS SELECT count(*) FROM {where}",
                            "EXECUTE nc(0)"])
    if isinstance(res, str):
        return res
    calls = _value(dsn, "SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM ncalls")
    return f"{res[-1] if res else 'no row'}|{calls}"


def _prune_arm(dsn, typ, where, value):
    """-> "REMOVED|COUNT" for `SELECT count(*) FROM where` on a generic plan."""
    res = _transcript(dsn, [GENERIC, NO_PARALLEL, f"PREPARE pa({typ}) AS SELECT count(*) FROM {where}",
                            f"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE pa({value})",
                            f"EXECUTE pa({value})"])
    if isinstance(res, str):
        return res
    return f"{_field(res, 'Chunk Groups Removed by Filter')}|{res[-1] if res else 'no row'}"


@pytest.fixture(scope="module")
def pp(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        # 40000 rows / 2000 = 20 monotonic groups; ts >= 39000 lives in the last group.
        _session(dsn, ["CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                       "SET pgcolumnar.stripe_row_limit = 2000",
                       "CREATE TABLE t (id int, ts int) USING pgcolumnar",
                       "INSERT INTO t SELECT g, g FROM generate_series(1, 40000) g",
                       "CREATE TABLE thr (lo int)",
                       "INSERT INTO thr VALUES (39000), (38000)"])
        before = _crashes(pgc_cluster)
        r = {}
        plan = _session(dsn, [GENERIC, "DEALLOCATE ALL",
                              "PREPARE p(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                              "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE p(39000)"])
        r["removed"] = _field(plan if isinstance(plan, str) else plan[-1], "Chunk Groups Removed by Filter")
        cnt = _session(dsn, [GENERIC, "DEALLOCATE ALL",
                             "PREPARE c(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                             "EXECUTE c(39000)"])
        r["prep count"] = cnt if isinstance(cnt, str) else (cnt[-1][0] if cnt[-1] else query_error("no count"))
        r["literal count"] = _value(dsn, "SELECT count(*) FROM t WHERE ts >= 39000")
        corr = _session(dsn, [f"EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) {CORR_GE}"])
        corr = corr if isinstance(corr, str) else corr[0]
        r["corr rescans"] = (corr if isinstance(corr, str) else
                             str(sum(1 for l in corr if re.search(
                                 r"Custom Scan \(PgColumnarScan\) on t \(actual rows=[\d.]+ loops=2\)", l))))
        r["corr skip preds"] = _field(corr, "Columnar Usable Skip Predicates")
        r["corr ge"] = _value(dsn, CORR_GE)
        r["corr le"] = _value(dsn, CORR_LE)
        twice = _session(dsn, [GENERIC, "DEALLOCATE ALL",
                               "PREPARE w(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                               "EXECUTE w(39000)", "EXECUTE w(38000)",
                               "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE w(38000)"])
        if isinstance(twice, str):
            r["twice count"] = r["twice removed"] = twice
        else:
            r["twice count"] = twice[4][0] if twice[4] else query_error("no second count")
            r["twice removed"] = _field(twice[5], "Chunk Groups Removed by Filter")
        _session(dsn, [FIRST_CALL.format(name="slow_first", act="PERFORM pg_sleep(2)"),
                       FIRST_CALL.format(name="fail_first", act="RAISE EXCEPTION 'call 1 fails'")])
        timeout = _transcript(dsn, [GENERIC, "PREPARE s(int) AS SELECT count(*) FROM t WHERE ts > slow_first($1)",
                                    "SET t.n = '0'", "SET statement_timeout = '300ms'", "EXECUTE s(0)"])
        r["timeout"] = _count(timeout, "raised: canceling statement due to statement timeout")
        fail = _transcript(dsn, [GENERIC, "PREPARE f(int) AS SELECT count(*) FROM t WHERE ts > fail_first($1)",
                                 "SET t.n = '0'", "EXECUTE f(0)"])
        r["fail once"] = _count(fail, "raised: call 1 fails")
        r["warnings"] = (timeout if isinstance(timeout, str) else fail if isinstance(fail, str)
                         else _count(timeout + fail, "WARNING"))
        _session(dsn, [CNTC])
        expl = _transcript(dsn, [GENERIC, "PREPARE e(int) AS SELECT count(*) FROM t WHERE ts > cntc($1)",
                                 "SET t.n = '0'", "EXPLAIN (COSTS off) EXECUTE e(0)", "SHOW t.n"])
        r["explain calls"] = expl if isinstance(expl, str) else (expl[-1] if expl else query_error("no SHOW"))
        bound = _transcript(dsn, [GENERIC, "PREPARE b(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                                  "EXPLAIN (COSTS off) EXECUTE b(39000)"])
        r["explain bound"] = _field(bound, "Columnar Pushed-Down Filters")
        # A scan that is set up but never run calls no operand; boom() always raises.
        _session(dsn, [BOOM, SAME])
        cut = _transcript(dsn, [GENERIC, "PREPARE l(int) AS SELECT 1 UNION ALL "
                                "(SELECT ts FROM t WHERE ts > boom($1)) LIMIT 1", "EXECUTE l(0)"])
        r["limit cut"] = cut if isinstance(cut, str) else (cut[-1] if cut else query_error("no row"))
        off = _transcript(dsn, [GENERIC, "PREPARE o(int, int) AS SELECT count(*) FROM t "
                                "WHERE ts > boom($1) AND $2 = 0", "EXECUTE o(0, 1)"])
        r["one-time off"] = off if isinstance(off, str) else (off[-1] if off else query_error("no row"))
        fn = _transcript(dsn, [GENERIC, "PREPARE g(int) AS SELECT count(*) FROM t WHERE ts >= same($1)",
                               "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE g(39000)"])
        r["fn removed"] = _field(fn, "Chunk Groups Removed by Filter")
        # The frozen keys are also the scan's own keys, which EXPLAIN counts: a freeze
        # that gave them to the reader alone pruned 19 groups and reported 0.
        r["fn pushed"] = _field(fn, "Columnar Pushed-Down Filters")
        # One call to freeze the operand, then one per row the Filter tests.
        once = _transcript(dsn, [GENERIC, "PREPARE n(int) AS SELECT count(*) FROM t WHERE ts >= cntc($1)",
                                 "SET t.n = '0'",
                                 "EXPLAIN (ANALYZE, COSTS off, TIMING off, SUMMARY off) EXECUTE n(39001)",
                                 "SHOW t.n"])
        if isinstance(once, str):
            r["once calls"] = r["once want"] = once
        else:
            rows = next((m.group(1) for l in once for m in [re.search(
                r"Custom Scan \(PgColumnarScan\) on t \(actual rows=(\d+)", l)] if m), None)
            removed = next((m.group(1) for l in once for m in [re.search(
                r"Rows Removed by Filter: (\d+)", l)] if m), None)
            r["once calls"] = once[-1] if once else query_error("no SHOW")
            r["once want"] = (str(1 + int(rows) + int(removed)) if rows and int(rows) > 0 and removed
                              else query_error(f"no rows or Rows Removed by Filter: [{rows}] [{removed}]"))
        _session(dsn, [OPT_EQ])
        opt = _transcript(dsn, [GENERIC, "SELECT opt_eq(true)", "SELECT opt_eq(false)"])
        r["plpgsql one-time off"] = opt if isinstance(opt, str) else " ".join(opt)
        _session(dsn, [LO_CNT])
        lo = _transcript(dsn, [GENERIC] + AUTO_EXPLAIN + ["SELECT lo_cnt(39000)"])
        r["plpgsql premise"] = _count(lo, "Filter: (ts >= $1)")
        r["plpgsql removed"] = _field(lo, "Chunk Groups Removed by Filter")
        _session(dsn, ["CREATE TABLE prsc (k int, payload text) USING pgcolumnar",
                       "SELECT pgcolumnar.set_options('prsc', chunk_group_row_limit => 1000, "
                       "stripe_row_limit => 1000)",
                       "INSERT INTO prsc SELECT k, 'p' || k FROM generate_series(1, 20000) k "
                       "ORDER BY md5(k::text)",
                       "SELECT pgcolumnar.add_projection('prsc', 'byk', ARRAY['k'], ARRAY['k'])",
                       "ANALYZE prsc"])
        pplan = _session(dsn, PROJ_SET + [f"EXPLAIN (COSTS off) {PROJ_LOW}"])
        r["proj premise"] = (pplan if isinstance(pplan, str)
                             else str(sum(1 for l in pplan[-1] if "Columnar Projection: byk" in l)))
        # Heap answers 801|400500 (k 100..900) and 11|219945 (k 19990..20000).
        r["proj answers"] = _proj_answers(dsn)
        # The keys frozen at the first run obey pgcolumnar.enable_qual_pushdown = off, as
        # Begin's keys do; with pushdown on the same query removes 19 ("fn removed").
        qoff = _transcript(dsn, [GENERIC, "SET pgcolumnar.enable_qual_pushdown = off",
                                 "PREPARE q(int) AS SELECT count(*) FROM t WHERE ts >= same($1)",
                                 "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE q(39000)"])
        r["qual pushdown off removed"] = _field(qoff, "Chunk Groups Removed by Filter")
        # Each key removes groups the other keeps: ts >= 2001 removes group 1 and ts <= 4000
        # removes groups 3 to 20. Testing only the first key removes 1, only the last 18.
        two = _transcript(dsn, [GENERIC, "PREPARE k(int, int) AS SELECT count(*) FROM t "
                                "WHERE ts >= same($1) AND ts <= same($2)",
                                "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE k(2001, 4000)"])
        r["two keys removed"] = _field(two, "Chunk Groups Removed by Filter")
        _session(dsn, ["SET pgcolumnar.stripe_row_limit = 2000",
                       "CREATE TABLE tx (s text) USING pgcolumnar",
                       "INSERT INTO tx SELECT lpad(g::text, 6, '0') FROM generate_series(1, 40000) g",
                       SAME_T])
        # 1655 rows of tx are >= '038345' followed by 'x's.
        rescan = _transcript(dsn, [GENERIC, TEXT_RESCAN, "EXECUTE r('038345' || repeat('x', 300000))"])
        r["text rescan"] = rescan if isinstance(rescan, str) else " ".join(rescan)
        # A varchar parameter on a text column reaches the scan inside a RelabelType;
        # Begin sees through it, so a plain EXPLAIN EXECUTE reports it pushed down.
        bv = _transcript(dsn, [GENERIC, "PREPARE bv(varchar) AS SELECT count(*) FROM tx WHERE s >= $1",
                               "EXPLAIN (COSTS off) EXECUTE bv('039000')"])
        r["explain bound varchar"] = _field(bv, "Columnar Pushed-Down Filters")
        # A domain parameter on an int column is wrapped in a RelabelType too.
        _session(dsn, ["CREATE DOMAIN dint AS int"])
        bd = _transcript(dsn, [GENERIC, "PREPARE d(dint) AS SELECT count(*) FROM t WHERE ts >= $1",
                               "EXPLAIN (COSTS off) EXECUTE d(39000)"])
        r["explain bound domain"] = _field(bd, "Columnar Pushed-Down Filters")
        # A NULL bound parameter is frozen as NULL and builds no key. Frozen as a
        # non-null Const, an int NULL became "ts >= 0" and a text NULL crashed the backend.
        ni = _transcript(dsn, [GENERIC, "PREPARE ni(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                               "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE ni(NULL)"])
        r["null int pushed"] = _field(ni, "Columnar Pushed-Down Filters")
        nt = _transcript(dsn, [GENERIC, "PREPARE nt(text) AS SELECT count(*) FROM tx WHERE s >= $1",
                               "EXECUTE nt(NULL)"])
        r["null text count"] = nt if isinstance(nt, str) else (nt[-1] if nt else query_error("no row"))
        r["null text survived"] = _survived(dsn, pgc_cluster, before)
        _session(dsn, ["SET pgcolumnar.stripe_row_limit = 1000",
                       "CREATE TABLE d (k int)", "INSERT INTO d SELECT generate_series(8001, 8200)",
                       "CREATE TABLE f (k int, p text) USING pgcolumnar",
                       "INSERT INTO f SELECT g, 'p' || g FROM generate_series(1, 20000) g",
                       "ANALYZE d", "ANALYZE f"])
        _await_up(dsn)
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                for sql in JOIN_SET + [JOIN_Q]:
                    conn.execute(sql)
                r["rr answer"] = "|".join(str(v) for v in conn.execute("EXECUTE j(8150)").fetchone())
        except psycopg.Error as exc:
            r["rr answer"] = _failed(exc)
        rr_on = _transcript(dsn, JOIN_SET + [JOIN_Q, JOIN_EXPLAIN])
        r["rr skip preds"] = _field(rr_on, "Columnar Usable Skip Predicates")
        r["rr removed"] = _field(rr_on, "Runtime Filter Groups Removed")
        r["rr survived"] = _survived(dsn, pgc_cluster, before)
        rr_off = _transcript(dsn, JOIN_SET + ["SET pgcolumnar.enable_qual_pushdown = off", JOIN_Q, JOIN_EXPLAIN])
        r["rr off skip preds"] = _field(rr_off, "Columnar Usable Skip Predicates")
        r["rr off groups read"] = _field(rr_off, "Columnar Chunk Groups Read")
        r["rr off survived"] = _survived(dsn, pgc_cluster, before)
        _session(dsn, ["CREATE TABLE d2 (grp int, k int)",
                       "INSERT INTO d2 SELECT 1, g FROM generate_series(8001, 8200) g",
                       "INSERT INTO d2 SELECT 2, g FROM generate_series(15001, 15200) g",
                       "CREATE TABLE fh AS SELECT * FROM f", "ANALYZE d2", "ANALYZE fh"])
        for key, tbl in (("rescan heap", "fh"), ("rescan", "f")):
            res = _transcript(dsn, JOIN_SET + [RESCAN_Q.format(t=tbl), "EXECUTE r(8150)", "EXECUTE r(8150)"])
            r[key] = res if isinstance(res, str) else " ".join(res)
        r["rescan survived"] = _survived(dsn, pgc_cluster, before)
        # A NULL a stable function returns at the first run is frozen as NULL as well.
        nfi = _transcript(dsn, [GENERIC, "PREPARE nfi(int) AS SELECT count(*) FROM t WHERE ts >= same($1)",
                                "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE nfi(NULL)"])
        r["fn null int pushed"] = _field(nfi, "Columnar Pushed-Down Filters")
        nf = _transcript(dsn, [GENERIC, "PREPARE nf(text) AS SELECT count(*) FROM tx WHERE s >= same_t($1)",
                               "EXECUTE nf(NULL)"])
        r["fn null text count"] = nf if isinstance(nf, str) else (nf[-1] if nf else query_error("no row"))
        r["fn null survived"] = _survived(dsn, pgc_cluster, before)
        # Only an operand whose clause could become a scan key is evaluated.
        nonc = _value(dsn, NONC_Q)
        r["nonc premise"] = _value(dsn, f"SELECT ('a' < 'B' COLLATE \"{nonc}\")::text || '|' || "
                                        "('a' < 'B' COLLATE \"C\")::text")
        _session(dsn, BOOMC + [sql.format(nonc=nonc) for sql in TK] + TZ)
        for key, where in NOT_KEYABLE:
            r[key] = _nc_arm(dsn, where)
        heap = []
        for key, typ, where, value in PRUNE:
            r[key] = _prune_arm(dsn, typ, where.format(t="tk"), value)
            hres = _transcript(dsn, [GENERIC, f"PREPARE ha({typ}) AS SELECT count(*) FROM {where.format(t='tkh')}",
                                     f"EXECUTE ha({value})"])
            heap.append(hres if isinstance(hres, str) else (hres[-1] if hres else "no row"))
        r["prune heap"] = " ".join(heap)
        heap = []
        for key, typ, where, value in PRUNE2:
            r[key] = _prune_arm(dsn, typ, where.format(t="tk"), value)
            hres = _transcript(dsn, [GENERIC, f"PREPARE ha({typ}) AS SELECT count(*) FROM {where.format(t='tkh')}",
                                     f"EXECUTE ha({value})"])
            heap.append(hres if isinstance(hres, str) else (hres[-1] if hres else "no row"))
        r["prune heap 2"] = " ".join(heap)
        _session(dsn, DOMAINS)
        heap = []
        for key, typ, where, value in PRUNE3:
            r[key] = _prune_arm(dsn, typ, where.format(t="tk"), value)
            hres = _transcript(dsn, [GENERIC, f"PREPARE ha({typ}) AS SELECT count(*) FROM {where.format(t='tkh')}",
                                     f"EXECUTE ha({value})"])
            heap.append(hres if isinstance(hres, str) else (hres[-1] if hres else "no row"))
        r["prune heap 3"] = " ".join(heap)
        # The projection and rescan arms check answers; these check that the keys frozen
        # at the first run still prune there.
        _session(dsn, PRSK)
        pk = _transcript(dsn, PROJ_SET + [PRSK_EXPLAIN])
        if isinstance(pk, str):
            r["proj removed"] = pk
        else:
            proj = "bykk" if any("Columnar Projection: bykk" in line for line in pk) else "no projection"
            r["proj removed"] = f"{proj}|{_field(pk, 'Chunk Groups Removed by Filter')}"
        # "Chunk Groups Read|Removed by Filter|Runtime Filter Groups Removed" for
        # f.k >= same(19500), a key that removes more than either loop's runtime range.
        rs = _transcript(dsn, JOIN_SET + [RESCAN_Q.format(t="f"),
                                          "EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) EXECUTE r(19500)"])
        r["rescan removed"] = rs if isinstance(rs, str) else "|".join(
            _field([line], label) for line in rs
            for label in ("Columnar Chunk Groups Read", "Chunk Groups Removed by Filter",
                          "Runtime Filter Groups Removed") if label in line)
        r["survived"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_param_pushdown_a_generic_plan_parameterized_qual_prunes(pp, expect):
    expect.text(pp["removed"], "19",
                "a generic-plan parameterized qual prunes chunk groups (19 of 20 removed)")


def test_param_pushdown_the_parameterized_count_matches_the_literal(pp, expect):
    expect.text(pp["prep count"], "1001", "parameterized count matches the literal count")


def test_param_pushdown_the_literal_count_is_the_expected_value(pp, expect):
    expect.text(pp["literal count"], "1001", "literal count is the expected value")


def test_param_pushdown_the_correlated_arm_rescans_the_columnar_scan(pp, expect):
    expect.text(pp["corr rescans"], "1", "the correlated arm rescans the columnar scan once per outer row")


def test_param_pushdown_a_correlated_param_exec_builds_no_skip_predicate(pp, expect):
    expect.text(pp["corr skip preds"], "0", "a correlated PARAM_EXEC builds no skip predicate")


def test_param_pushdown_a_correlated_param_exec_is_not_mis_pruned(pp, expect):
    expect.text(pp["corr ge"], "3002", "a correlated PARAM_EXEC is not mis-pruned (results stay correct)")


def test_param_pushdown_a_correlated_upper_bound_is_not_mis_pruned(pp, expect):
    expect.text(pp["corr le"], "77000", "a correlated PARAM_EXEC upper bound is not mis-pruned either")


def test_param_pushdown_a_second_execution_counts_with_its_own_value(pp, expect):
    expect.text(pp["twice count"], "2001", "a second execution in the same session counts with its own value")


def test_param_pushdown_a_second_execution_prunes_with_its_own_value(pp, expect):
    expect.text(pp["twice removed"], "18", "and prunes with its own value too (18 of 20 removed)")


def test_param_pushdown_a_statement_timeout_at_scan_start_cancels(pp, expect):
    expect.text(pp["timeout"], "1",
                "a statement_timeout during scan start-up cancels the columnar query (#2065)")


def test_param_pushdown_a_stable_operand_that_raises_once_reaches_the_client(pp, expect):
    expect.text(pp["fail once"], "1",
                "a stable operand that raises once reaches the client, as on heap (#2065)")


def test_param_pushdown_no_failed_query_leaves_a_warning(pp, expect):
    expect.text(pp["warnings"], "0",
                "neither failed query leaves a resource or SPI-stack WARNING (#2065)")


def test_param_pushdown_a_plain_explain_does_not_call_a_stable_operand(pp, expect):
    expect.text(pp["explain calls"], "0", "a plain EXPLAIN does not call a stable operand (#2065)")


def test_param_pushdown_a_plain_explain_execute_still_pushes_down_a_bound_param(pp, expect):
    expect.text(pp["explain bound"], "1",
                "a plain EXPLAIN EXECUTE still pushes down a bound parameter (#2065)")


def test_param_pushdown_a_scan_that_limit_cuts_off_calls_no_stable_operand(pp, expect):
    expect.text(pp["limit cut"], "1",
                "a columnar scan that LIMIT cuts off never calls its stable operand (#2065)")


def test_param_pushdown_a_scan_under_a_false_one_time_filter_calls_no_stable_operand(pp, expect):
    expect.text(pp["one-time off"], "0",
                "a columnar scan under a false one-time filter never calls its stable operand (#2065)")


def test_param_pushdown_a_stable_function_operand_still_prunes_when_the_scan_runs(pp, expect):
    expect.text(pp["fn removed"], "19",
                "a stable function operand still prunes when the scan runs (19 of 20 removed) (#2065)")


def test_param_pushdown_the_frozen_key_counts_as_pushed_down(pp, expect):
    expect.text(pp["fn pushed"], "1",
                "and the same EXPLAIN ANALYZE counts the frozen key as pushed down (#2065)")


def test_param_pushdown_a_stable_operand_is_called_once_to_freeze_then_once_per_row(pp, expect):
    expect.text(pp["once calls"], pp["once want"],
                "the scan calls a stable operand once to freeze it, then once per row the Filter tests (#2065)")


def test_param_pushdown_a_plpgsql_record_field_under_a_false_one_time_filter_is_not_fetched(pp, expect):
    expect.text(pp["plpgsql one-time off"], "1 0",
                "a PL/pgSQL record field under a false one-time filter is not fetched at scan set-up (#2065)")


def test_param_pushdown_auto_explain_logs_the_plan_a_plpgsql_function_ran(pp, expect):
    expect.text(pp["plpgsql premise"], "1",
                "auto_explain logs the plan a PL/pgSQL function ran, with its parameter (premise) (#2065)")


def test_param_pushdown_a_plpgsql_variable_on_a_generic_plan_prunes_when_the_scan_runs(pp, expect):
    expect.text(pp["plpgsql removed"], "19",
                "a PL/pgSQL variable on a generic plan prunes when the scan runs (19 of 20 removed) (#2065)")


def test_param_pushdown_the_projection_arm_uses_the_projection(pp, expect):
    expect.text(pp["proj premise"], "1", "the projection arm's scan uses the projection (premise) (#2065)")


def test_param_pushdown_a_projection_scan_with_a_first_run_key_counts_what_heap_counts(pp, expect):
    expect.text(pp["proj answers"], "801|400500 11|219945",
                "a projection scan with a key frozen at the first run counts what heap counts (#2065)")


def test_param_pushdown_a_first_run_key_obeys_qual_pushdown_off(pp, expect):
    expect.text(pp["qual pushdown off removed"], "0",
                "with pgcolumnar.enable_qual_pushdown off, a key frozen at the first run prunes nothing (#2065)")


def test_param_pushdown_two_first_run_keys_both_prune(pp, expect):
    expect.text(pp["two keys removed"], "19",
                "two keys frozen at the first run both prune (19 of 20 removed) (#2065)")


def test_param_pushdown_a_long_text_first_run_key_survives_each_rescan(pp, expect):
    expect.text(pp["text rescan"], "1|1655 2|1655 3|1655",
                "a long text key frozen at the first run survives each rescan (#2065)")


def test_param_pushdown_a_plain_explain_execute_still_pushes_down_a_bound_varchar_param(pp, expect):
    expect.text(pp["explain bound varchar"], "1",
                "a plain EXPLAIN EXECUTE still pushes down a bound varchar parameter (#2065)")


def test_param_pushdown_a_plain_explain_execute_still_pushes_down_a_bound_domain_param(pp, expect):
    expect.text(pp["explain bound domain"], "1",
                "a plain EXPLAIN EXECUTE still pushes down a bound domain parameter (#2065)")


def test_param_pushdown_a_null_bound_int_param_builds_no_pushed_down_filter(pp, expect):
    expect.text(pp["null int pushed"], "0",
                "a NULL bound int parameter on a generic plan builds no pushed-down filter (#2065)")


def test_param_pushdown_a_null_bound_text_param_counts_zero(pp, expect):
    expect.text(pp["null text count"], "0",
                "a NULL bound text parameter on a generic plan counts 0 (#2065)")


def test_param_pushdown_the_backend_is_alive_after_the_null_text_param(pp, expect):
    expect.text(pp["null text survived"], "1",
                "the backend is alive after the NULL text parameter (#2065)")


def test_param_pushdown_a_first_run_key_under_a_runtime_range_counts_what_heap_counts(pp, expect):
    expect.text(pp["rr answer"], "51|416925",
                "a key frozen at the first run under a hash join's runtime range counts what heap counts (#2065)")


def test_param_pushdown_the_scan_keeps_the_runtime_range_after_the_frozen_key(pp, expect):
    expect.text(pp["rr skip preds"], "3",
                "the scan keeps the runtime range after the frozen key: three skip predicates (#2065)")


def test_param_pushdown_the_kept_runtime_range_still_removes_groups(pp, expect):
    expect.text(pp["rr removed"], "11",
                "and the kept runtime range still removes groups (11 removed) (#2065)")


def test_param_pushdown_the_backend_is_alive_after_the_runtime_range_arm(pp, expect):
    expect.text(pp["rr survived"], "1", "the backend is alive after the runtime-range arm (#2065)")


def test_param_pushdown_with_pushdown_off_the_scan_keeps_the_runtime_range_alone(pp, expect):
    expect.text(pp["rr off skip preds"], "2",
                "with pushdown off, the scan keeps the runtime range alone: two skip predicates (#2065)")


def test_param_pushdown_with_pushdown_off_the_runtime_range_still_reads_one_group(pp, expect):
    expect.text(pp["rr off groups read"], "1",
                "with pushdown off, the runtime range still reads one chunk group (#2065)")


def test_param_pushdown_the_backend_is_alive_after_the_runtime_range_arm_with_pushdown_off(pp, expect):
    expect.text(pp["rr off survived"], "1",
                "the backend is alive after the runtime-range arm with pushdown off (#2065)")


def test_param_pushdown_the_heap_copy_answers_the_hash_join_rescan(pp, expect):
    expect.text(pp["rescan heap"], RESCAN_WANT, "the heap copy answers the hash-join rescan (premise) (#2065)")


def test_param_pushdown_a_hash_join_rescan_counts_what_heap_counts_in_each_loop(pp, expect):
    expect.text(pp["rescan"], RESCAN_WANT,
                "a hash-join rescan with a key frozen at the first run counts what heap counts in each loop (#2065)")


def test_param_pushdown_the_backend_is_alive_after_the_hash_join_rescan(pp, expect):
    expect.text(pp["rescan survived"], "1", "the backend is alive after the hash-join rescan (#2065)")


def test_param_pushdown_a_null_from_a_stable_function_on_an_int_column_builds_no_filter(pp, expect):
    expect.text(pp["fn null int pushed"], "0",
                "a NULL from a stable function on an int column builds no pushed-down filter (#2065)")


def test_param_pushdown_a_null_from_a_stable_function_on_a_text_column_counts_zero(pp, expect):
    expect.text(pp["fn null text count"], "0", "a NULL from a stable function on a text column counts 0 (#2065)")


def test_param_pushdown_the_backend_is_alive_after_the_null_from_a_stable_function(pp, expect):
    expect.text(pp["fn null survived"], "1",
                "the backend is alive after the NULL from a stable function (#2065)")


def test_param_pushdown_a_stable_operand_of_a_clause_with_no_btree_strategy_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable ne"], "0|0",
                "a stable operand of a clause with no btree strategy (<>) is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_compared_with_a_column_expression_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable expr"], "0|0",
                "a stable operand compared with an expression of the column is not evaluated (#2065)")


def test_param_pushdown_the_premise_collation_is_not_c(pp, expect):
    expect.text(pp["nonc premise"], "true|false",
                "premise: sn's collation orders a lower-case letter before an upper-case one, which C does not (#2065)")


def test_param_pushdown_a_stable_operand_of_like_on_a_column_whose_collation_is_not_c_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable like non-c"], "0|0",
                "a stable operand of LIKE on a column whose collation is not C is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_date_trunc_with_no_btree_strategy_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc ne"], "0|0",
                "a stable operand of date_trunc with no btree strategy (<>) is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_date_trunc_over_a_column_expression_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc expr"], "0|0",
                "a stable operand of date_trunc over an expression of the column is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_date_trunc_with_a_unit_from_a_column_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc unit"], "0|0",
                "a stable operand of date_trunc with a unit read from a column is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_on_the_column_side_of_like_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable like reversed"], "0|0",
                "a stable operand on the column side of LIKE is not evaluated (#2065)")


def test_param_pushdown_a_stable_array_operand_of_ne_any_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable ne any"], "0|0",
                "a stable array operand of <> ANY is not evaluated (#2065)")


def test_param_pushdown_a_stable_array_operand_of_eq_all_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable eq all"], "0|0",
                "a stable array operand of = ALL is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_range_contains_range_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable range contains"], "0|0",
                "a stable operand of range @> range is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_under_another_collation_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable other collation"], "0|0",
                "a stable operand compared under a collation that is not the column's is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_compared_with_another_function_of_the_column_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable abs"], "0|0",
                "a stable operand compared with a function of the column other than date_trunc is not evaluated (#2065)")


def test_param_pushdown_the_heap_copy_answers_the_five_pruning_queries(pp, expect):
    expect.text(pp["prune heap"], "24 100 24 1000 2",
                "the heap copy answers the five pruning queries (premise) (#2065)")


def test_param_pushdown_a_bound_param_compared_with_date_trunc_prunes(pp, expect):
    expect.text(pp["prune date_trunc"], "19|24",
                "a bound parameter compared with date_trunc of a timestamp column prunes (#2065)")


def test_param_pushdown_a_bound_param_in_like_on_a_c_column_prunes(pp, expect):
    expect.text(pp["prune like c"], "19|100",
                "a bound parameter in LIKE on a C-collated column prunes (#2065)")


def test_param_pushdown_a_bound_param_that_a_range_column_overlaps_prunes(pp, expect):
    expect.text(pp["prune range overlap"], "19|24",
                "a bound parameter that a range column overlaps prunes (#2065)")


def test_param_pushdown_a_bound_text_param_compared_with_a_varchar_column_prunes(pp, expect):
    expect.text(pp["prune varchar"], "19|1000",
                "a bound text parameter compared with a varchar column prunes (#2065)")


def test_param_pushdown_a_bound_array_param_of_eq_any_prunes(pp, expect):
    expect.text(pp["prune any"], "19|2",
                "a bound array parameter of = ANY prunes (#2065)")


def test_param_pushdown_a_stable_timestamptz_operand_compared_with_date_trunc_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc tstz operand"], "0|0",
                "a stable timestamptz operand compared with date_trunc of a timestamp column is not evaluated (#2065)")


def test_param_pushdown_a_stable_date_operand_compared_with_date_trunc_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc date operand"], "0|0",
                "a stable date operand compared with date_trunc of a timestamp column is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_date_trunc_to_a_decade_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc decade"], "0|0",
                "a stable operand of date_trunc to a decade is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_date_trunc_to_a_plural_unit_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc days"], "0|0",
                "a stable operand of date_trunc to a plural unit (days) is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_date_trunc_of_a_domain_column_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc domain"], "0|0",
                "a stable operand of date_trunc of a timestamp domain column is not evaluated (#2065)")


def test_param_pushdown_a_stable_multirange_operand_a_range_column_overlaps_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable range multirange"], "0|0",
                "a stable multirange operand that a range column overlaps is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_of_date_trunc_of_a_timestamptz_column_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable date_trunc tstz column"], "0|0",
                "a stable operand of date_trunc of a timestamptz column is not evaluated (#2065)")


def test_param_pushdown_a_stable_operand_compared_with_a_two_argument_function_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable two-argument function"], "0|0",
                "a stable operand compared with a two-argument function of the column other than date_trunc is not evaluated (#2065)")


def test_param_pushdown_a_stable_array_operand_of_lt_any_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable lt any"], "0|0",
                "a stable array operand of < ANY is not evaluated (#2065)")


def test_param_pushdown_the_heap_copy_answers_the_date_trunc_ge_and_range_contains_queries(pp, expect):
    expect.text(pp["prune heap 2"], "569 1",
                "the heap copy answers the date_trunc >= and range @> pruning queries (premise) (#2065)")


def test_param_pushdown_a_bound_param_that_date_trunc_is_ge_prunes(pp, expect):
    expect.text(pp["prune date_trunc ge"], "19|569",
                "a bound parameter that date_trunc of a timestamp column is >= prunes (#2065)")


def test_param_pushdown_a_bound_param_that_a_range_column_contains_prunes(pp, expect):
    expect.text(pp["prune range contains"], "19|1",
                "a bound parameter that a range column contains prunes (#2065)")


def test_param_pushdown_a_stable_timestamptz_operand_compared_with_a_timestamp_column_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable tstz scalar"], "0|0",
                "a stable timestamptz operand compared with a timestamp column is not evaluated (#2065)")


def test_param_pushdown_a_stable_timestamptz_array_operand_of_eq_any_on_a_timestamp_column_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable tstz any"], "0|0",
                "a stable timestamptz array operand of = ANY on a timestamp column is not evaluated (#2065)")


def test_param_pushdown_a_stable_timestamptz_operand_compared_with_a_timestamp_domain_column_is_not_evaluated(pp, expect):
    expect.text(pp["not keyable tstz domain column"], "0|0",
                "a stable timestamptz operand compared with a timestamp domain column is not evaluated (#2065)")


def test_param_pushdown_the_heap_copy_answers_the_bound_domain_and_varchar_queries(pp, expect):
    expect.text(pp["prune heap 3"], "100 100 24 569 24 24 569 569 569",
                "the heap copy answers the bound domain and varchar pruning queries (premise) (#2065)")


def test_param_pushdown_a_bound_varchar_param_in_like_on_a_c_column_prunes(pp, expect):
    expect.text(pp["prune varchar like"], "19|100",
                "a bound varchar parameter in LIKE on a C-collated column prunes (#2065)")


def test_param_pushdown_a_bound_text_domain_param_in_like_on_a_c_column_prunes(pp, expect):
    expect.text(pp["prune tdom like"], "19|100",
                "a bound text domain parameter in LIKE on a C-collated column prunes (#2065)")


def test_param_pushdown_a_bound_timestamp_domain_param_compared_with_date_trunc_prunes(pp, expect):
    expect.text(pp["prune tsdom date_trunc"], "19|24",
                "a bound timestamp domain parameter compared with date_trunc prunes (#2065)")


def test_param_pushdown_a_bound_timestamp_domain_param_that_date_trunc_is_ge_prunes(pp, expect):
    expect.text(pp["prune tsdom date_trunc ge"], "19|569",
                "a bound timestamp domain parameter that date_trunc is >= prunes (#2065)")


def test_param_pushdown_a_bound_range_domain_param_that_a_range_column_overlaps_prunes(pp, expect):
    expect.text(pp["prune rdom overlap"], "19|24",
                "a bound range domain parameter that a range column overlaps prunes (#2065)")


def test_param_pushdown_a_bound_param_compared_with_date_trunc_to_an_upper_case_unit_prunes(pp, expect):
    expect.text(pp["prune date_trunc upper unit"], "19|24",
                "a bound parameter compared with date_trunc to an upper-case unit (Day) prunes (#2065)")


def test_param_pushdown_a_bound_timestamptz_param_compared_with_a_timestamptz_column_prunes(pp, expect):
    expect.text(pp["prune tstz column"], "19|569",
                "a bound timestamptz parameter compared with a timestamptz column prunes (#2065)")


def test_param_pushdown_a_bound_date_param_compared_with_a_timestamp_column_prunes(pp, expect):
    expect.text(pp["prune date operand"], "19|569",
                "a bound date parameter compared with a timestamp column prunes (#2065)")


def test_param_pushdown_a_bound_timestamptz_param_compared_with_a_date_column_prunes(pp, expect):
    expect.text(pp["prune tstz on date column"], "19|569",
                "a bound timestamptz parameter compared with a date column prunes (#2065)")


def test_param_pushdown_a_projection_scan_with_a_first_run_key_removes_groups(pp, expect):
    expect.text(pp["proj removed"], "bykk|19",
                "a projection scan with a key frozen at the first run removes groups (#2065)")


def test_param_pushdown_a_hash_join_rescan_with_a_first_run_key_removes_groups_in_each_loop(pp, expect):
    expect.text(pp["rescan removed"], "0|40|1",
                "a hash-join rescan with a key frozen at the first run removes groups in each loop (#2065)")


def test_param_pushdown_the_backend_is_alive(pp, expect):
    expect.text(pp["survived"], "1", "backend alive")

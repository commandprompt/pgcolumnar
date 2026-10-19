"""The columnar scan is priced for what the zone maps let it skip (#434). The pytest twin
of `test/zonemap_cost.sh`, every check by the shell's name (#1341).

The cost model used to quote a full scan whatever the zone maps excluded, so an index
scan priced at its best case won: priced 12,407 and ran 500 ms, against a columnar scan
priced 17,326 that ran 43 ms. What is asserted is the plan and the cost relation, never
wall clock. One table holds three keys over the same values: `seq` ascends with physical
order and prunes; `scat` is scattered and prunes nothing, the control that a blanket
discount would fail; and `saw` is uncorrelated globally but tight inside each row group,
so it prunes as hard as `seq` while its correlation says otherwise (#461). Pruning is
measured from the executor's own counter before any price is compared (#477).

ON A PRIVATE DATABASE, because pg_stats is read by table name, built once by a module
fixture that takes every reading in the shell's order; each test asserts one, so one red
cannot stop the next (#1339). Each reading's SETs share its connection with its
statement, as the shell's psql calls do. A failed query is a `query_error()`, refused on
either side by name.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_zonemap_cost"
ROWS = 200000
CUT = ROWS * 8 // 10
QUERY = "SELECT tag, count(*) FROM zc WHERE {col} > %d GROUP BY tag" % CUT
NODE = re.compile(r"Custom Scan \(PgColumnarScan\)|Index Scan|Seq Scan")
COST = re.compile(r"cost=[0-9.]+\.\.([0-9.]+)")
REMOVED = re.compile(r"Columnar Chunk Groups Removed by Filter: ([0-9]+)")
SERIAL = "SET max_parallel_workers_per_gather = 0"
NO_INDEX = ("SET enable_indexscan = off", "SET enable_bitmapscan = off")
INDEX_ONLY = ("SET pgcolumnar.enable_custom_scan = off", "SET enable_seqscan = off",
              "SET enable_bitmapscan = off")


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rows(dsn, *sqls):
    """-> the last statement's rows, first column as text, on a connection of its own,
    or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in sqls:
                cur = conn.execute(sql)
            return ["" if r[0] is None else str(r[0]) for r in cur]
    except psycopg.Error as exc:
        return _failed(exc)


def _value(dsn, *sqls):
    rows = _rows(dsn, *sqls)
    return rows if isinstance(rows, str) else (rows[0] if rows else "")


def _first(dsn, pattern, *sqls):
    """-> the first match of `pattern` in the last statement's output (its group if it
    has one), "" when nothing matches, or the failed reading."""
    lines = _rows(dsn, *sqls)
    if isinstance(lines, str):
        return lines
    for line in lines:
        m = pattern.search(line)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return ""


def _num(v):
    """-> v as a float, or None when it is not a number."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _count(v):
    """-> v as an int when it is one, otherwise v itself (a failed or empty reading)."""
    return int(v) if isinstance(v, str) and v.isdigit() else v


def _flag(v, test):
    """-> 1 when v is a number and test(v) holds, 0 when it is a number that does not,
    and v itself, a failed reading or an empty one, otherwise."""
    if isinstance(v, str) and v.startswith("QUERY_ERROR"):
        return v
    n = _num(v)
    return v if n is None else (1 if test(n) else 0)


@pytest.fixture(scope="module")
def zc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            for sql in ("CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                        "CREATE TABLE zc (seq bigint, scat bigint, saw bigint, tag int, pad text) "
                        "USING pgcolumnar",
                        "SELECT pgcolumnar.set_options('zc', stripe_row_limit => 10000)",
                        f"INSERT INTO zc SELECT g, ((g * 7919) % {ROWS}), "
                        "((((g - 1) / 10000) * 9) % 20) * 10000 + ((g * 7919) % 10000), "
                        f"g % 5, md5(g::text) FROM generate_series(1, {ROWS}) g",
                        "CREATE INDEX zc_seq ON zc (seq)",
                        "CREATE INDEX zc_scat ON zc (scat)",
                        "CREATE INDEX zc_saw ON zc (saw)",
                        "ANALYZE zc"):
                try:
                    c.execute(sql)
                except psycopg.Error as exc:
                    print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")

        def corr(col, places):
            return _value(dsn, f"SELECT round(abs(correlation)::numeric, {places}) FROM pg_stats "
                               f"WHERE tablename = 'zc' AND attname = '{col}'")

        def node(col):
            return _first(dsn, NODE, SERIAL, f"EXPLAIN (COSTS OFF) {QUERY.format(col=col)}")

        def cost(col, sets):
            return _first(dsn, COST, SERIAL, *sets, f"EXPLAIN {QUERY.format(col=col)}")

        def removed(col):
            return _first(dsn, REMOVED, SERIAL, *NO_INDEX,
                          "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                          + QUERY.format(col=col))

        r = {"rows": _value(dsn, "SELECT count(*) FROM zc"),
             "corr seq": corr("seq", 2), "corr scat": corr("scat", 2)}
        print(f"-- |correlation|: seq = {r['corr seq']}, scat = {r['corr scat']}")
        r["seq removed"], r["scat removed"] = removed("seq"), removed("scat")
        print(f"-- groups removed: seq = {r['seq removed']}, scat = {r['scat removed']}")
        r["seq node"] = node("seq")
        r["seq col"], r["seq idx"] = cost("seq", NO_INDEX), cost("seq", INDEX_ONLY)
        print(f"-- correlated: chosen={r['seq node']}  columnar={r['seq col']}  index={r['seq idx']}")
        r["scat col"] = cost("scat", NO_INDEX)
        print(f"-- scattered: columnar={r['scat col']}")
        r["corr saw"], r["saw removed"] = corr("saw", 4), removed("saw")
        print(f"-- sawtooth: |correlation| = {r['corr saw']}, groups removed = {r['saw removed']}")
        r["saw node"] = node("saw")
        r["saw col"], r["saw idx"] = cost("saw", NO_INDEX), cost("saw", INDEX_ONLY)
        print(f"-- sawtooth: chosen={r['saw node']}  columnar={r['saw col']}  index={r['saw idx']}")
        agg = ("SELECT md5(string_agg(tag || ':' || n, ',' ORDER BY tag)) FROM ("
               f"SELECT tag, count(*) n FROM zc WHERE seq > {CUT} GROUP BY tag) s")
        r["md5 columnar"] = _value(dsn, agg)
        r["md5 other"] = _value(dsn, "SET pgcolumnar.enable_custom_scan = off", agg)
        r["count"] = _value(dsn, f"SELECT count(*) FROM zc WHERE seq > {CUT}")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_every_row_loaded(zc, expect):
    expect.num(_count(zc["rows"]), ROWS,
               "premise: every row loaded")


def test_the_ordered_key_really_is_correlated(zc, expect):
    expect.num(_flag(zc["corr seq"], lambda n: n > 0.9), 1, "premise: the ordered key really is correlated")


def test_the_scattered_key_really_is_not(zc, expect):
    expect.num(_flag(zc["corr scat"], lambda n: n < 0.2), 1, "premise: the scattered key really is not")


# ---- the physical premise: pruning happens (#477) ----------------------------

def test_the_correlated_arm_actually_removes_chunk_groups(zc, expect):
    expect.num(_flag(zc["seq removed"], lambda n: n > 0), 1,
               "the correlated arm actually removes chunk groups (#477)")


def test_the_scattered_arm_removes_none(zc, expect):
    expect.num(_flag(zc["scat removed"], lambda n: n == 0), 1,
               "premise: and the scattered arm removes none, so the two differ physically")


# ---- the correlated arm: pruning is real, so it must be priced -------------

def test_the_columnar_cost_is_a_measurement(zc, expect):
    expect.num(_flag(zc["seq col"], lambda n: n > 0), 1, "premise: the columnar cost is a measurement")


def test_the_index_cost_is_a_measurement(zc, expect):
    expect.num(_flag(zc["seq idx"], lambda n: n > 0), 1, "premise: the index cost is a measurement")


def test_a_correlated_restriction_picks_the_columnar_scan(zc, expect):
    expect.text(zc["seq node"], "Custom Scan (PgColumnarScan)",
                "a correlated restriction picks the columnar scan (#434)")


def test_it_is_priced_below_the_index_scan_it_used_to_lose_to(zc, expect):
    idx = _num(zc["seq idx"])
    expect.num(_flag(zc["seq col"], lambda n: idx is not None and n < idx), 1,
               "and it is priced below the index scan it used to lose to (#434)")


# ---- the control: nothing to prune, so nothing to discount ------------------

def test_the_scattered_columnar_cost_is_a_measurement(zc, expect):
    expect.num(_flag(zc["scat col"], lambda n: n > 0), 1,
               "premise: the scattered columnar cost is a measurement")


def test_an_uncorrelated_restriction_gets_no_pruning_discount(zc, expect):
    """With a margin: a bare `>` passed under a blanket discount on two equal prices
    apart by the noise in ANALYZE's sample, which is what this control must catch."""
    seq = _num(zc["seq col"])
    expect.num(_flag(zc["scat col"], lambda n: seq is not None and n > seq * 2), 1,
               "an uncorrelated restriction gets no pruning discount (#434)")


# ---- #461: locally tight, globally uncorrelated -----------------------------

def test_the_sawtooth_column_is_uncorrelated(zc, expect):
    expect.num(_flag(zc["corr saw"], lambda n: n < 0.2), 1,
               "premise: the sawtooth column is uncorrelated, like TSBS time (#391)")


def test_it_prunes_as_hard_as_the_correlated_column(zc, expect):
    seq = _num(zc["seq removed"])
    expect.num(_flag(zc["saw removed"], lambda n: seq is not None and n >= seq), 1,
               "premise: and it prunes as hard as the correlated column, measured")


def test_the_sawtooth_columnar_cost_is_a_measurement(zc, expect):
    expect.num(_flag(zc["saw col"], lambda n: n > 0), 1,
               "premise: the sawtooth columnar cost is a measurement")


def test_a_demonstrably_pruning_restriction_is_discounted(zc, expect):
    scat = _num(zc["scat col"])
    expect.num(_flag(zc["saw col"], lambda n: scat is not None and n < scat * 0.8), 1,
               "a demonstrably pruning restriction is discounted whatever its correlation (#461)")


def test_it_is_priced_close_to_the_correlated_arm(zc, expect):
    seq = _num(zc["seq col"])
    expect.num(_flag(zc["saw col"], lambda n: seq is not None and n < seq * 1.5), 1,
               "and priced close to the correlated arm it matches physically (#461)")


def test_the_planner_picks_the_columnar_scan_for_it(zc, expect):
    expect.text(zc["saw node"], "Custom Scan (PgColumnarScan)",
                "and the planner picks the columnar scan for it")


# ---- correctness, which the cost model may not change ----------------------

def test_the_correlated_query_returns_the_same_rows_either_way(zc, expect):
    expect.text(zc["md5 columnar"], zc["md5 other"],
                "the correlated query returns the same rows either way")


def test_the_correlated_query_returns_the_right_count(zc, expect):
    expect.num(_count(zc["count"]), ROWS - CUT,
               "the correlated query returns the right count")

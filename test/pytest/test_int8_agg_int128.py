"""sum() and avg() over bigint accumulate in 128 bits on the vectorized path (#785), and
the accumulator is cleared on a rescan (#786). The pytest twin of
`test/int8_agg_int128.sh`, every check by the shell's name (#1341).

Every value arm runs three ways and requires all three to agree: columnar with
pgcolumnar.enable_ungrouped_vector_agg on, columnar with it off, and a heap table.
Sums past int64 in both directions must be rebuilt from both 64-bit halves, empty and
all-NULL columns answer NULL, and numeric is unchanged. Two timing arms bound the
vectorized path at 1.10x the scalar one, unfiltered and filtered, as a ratio of two
interleaved minima of five. A LATERAL re-executes the node, and each rescan must match
the heap.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). The heap oracle is loaded from the same generator as the
columnar table, never copied out of it, so a decode fault cannot land in both. A NULL
answer reads <NULL>, never the empty string of a failure, and a rescan reading must
return its four rows.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_int8_agg_int128"
ON = "SET pgcolumnar.enable_ungrouped_vector_agg = on"
OFF = "SET pgcolumnar.enable_ungrouped_vector_agg = off"
RSGUC = (ON, "SET enable_material = off", "SET max_parallel_workers_per_gather = 0")
RSLAT = "SELECT d.i, s.c FROM rs_drv d, LATERAL (SELECT {a} c FROM {t} s2 WHERE s2.v > d.i) s ORDER BY d.i"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _rows(dsn, sql, sets=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            return conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _one(dsn, sql, sets=()):
    rows = _rows(dsn, sql, sets)
    if _failed(rows):
        return rows
    return query_error(f"no row from {sql}") if not rows else rows[0][0]


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _mk(dsn, name, expr, n, typ="bigint"):
    _run(dsn, f"DROP TABLE IF EXISTS {name}; DROP TABLE IF EXISTS {name}_h; "
              f"CREATE TABLE {name} (v {typ}) USING pgcolumnar; "
              f"INSERT INTO {name} SELECT {expr} FROM generate_series(1,{n}) g; "
              f"CREATE TABLE {name}_h (v {typ}); "
              f"INSERT INTO {name}_h SELECT {expr} FROM generate_series(1,{n}) g;")


def _trio(dsn, tbl, q):
    sel = f"SELECT coalesce(({q})::text, '<NULL>') FROM {{}}"
    on = _one(dsn, sel.format(tbl), (ON,))
    off = _one(dsn, sel.format(tbl), (OFF,))
    heap = _one(dsn, sel.format(f"{tbl}_h"))
    if any(_failed(x) for x in (on, off, heap)):
        return f"UNREAD on=[{on}] off=[{off}] heap=[{heap}]"
    return f"agree:{on}" if on == off == heap else f"DIFFER on=[{on}] off=[{off}] heap=[{heap}]"


def _vecplan(dsn, agg, tbl, where=""):
    plan = _rows(dsn, f"EXPLAIN (COSTS OFF) SELECT {agg} FROM {tbl}{where}", (ON,))
    return plan if _failed(plan) else sum(1 for r in plan if "Columnar Vectorized Aggregates" in r[0])


def _ms(dsn, setting, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SET pgcolumnar.enable_ungrouped_vector_agg = {setting}")
            t0 = time.perf_counter()
            conn.execute(sql).fetchall()
            return (time.perf_counter() - t0) * 1000.0
    except psycopg.Error:
        return None


def _min_of_five(dsn, sql):
    on = off = None
    for _ in range(5):
        x, y = _ms(dsn, "on", sql), _ms(dsn, "off", sql)
        on = x if on is None or (x is not None and x < on) else on
        off = y if off is None or (y is not None and y < off) else off
    return on, off


def _rs_rows(dsn, tbl, agg):
    rows = _rows(dsn, RSLAT.format(a=agg, t=tbl), RSGUC)
    if _failed(rows) or len(rows) != 4 or any(r[1] is None for r in rows):
        return f"not 4 rows from {tbl}: {rows}"
    return "\n".join(f"{r[0]}|{r[1]}" for r in rows)


@pytest.fixture(scope="module")
def ia(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _mk(dsn, "a8", "(g % 1000)::bigint", 200000)
        r["vec sum"], r["vec avg"] = _vecplan(dsn, "sum(v)", "a8"), _vecplan(dsn, "avg(v)", "a8")
        r["a8 sum"], r["a8 avg"] = _trio(dsn, "a8", "sum(v)"), _trio(dsn, "a8", "avg(v)")

        _mk(dsn, "n8", "CASE WHEN g % 7 = 0 THEN NULL WHEN g % 3 = 0 THEN -(g::bigint) ELSE g::bigint END", 100000)
        r["n8 shape"] = _one(dsn, "SELECT (count(*) FILTER (WHERE v IS NULL) > 0)::text||'/'||"
                                  "(count(*) FILTER (WHERE v < 0) > 0)::text FROM n8")
        for agg in ("sum", "avg"):
            r[f"n8 {agg}"] = _trio(dsn, "n8", f"{agg}(v)")
            r[f"n8 {agg} want"] = f"agree:{_one(dsn, f'SELECT {agg}(v)::text FROM n8_h')}"

        _mk(dsn, "big8", "9000000000000000007::bigint", 20000)
        r["big over"] = _one(dsn, "SELECT (sum(v) > 9223372036854775807::numeric)::text FROM big8_h")
        r["vec big"] = _vecplan(dsn, "sum(v)", "big8")
        r["big sum"], r["big avg"] = _trio(dsn, "big8", "sum(v)"), _trio(dsn, "big8", "avg(v)")
        r["big avg want"] = f"agree:{_one(dsn, 'SELECT avg(v)::text FROM big8_h')}"

        _mk(dsn, "neg8", "-9000000000000000007::bigint", 20000)
        r["neg over"] = _one(dsn, "SELECT (sum(v) < -9223372036854775807::numeric)::text FROM neg8_h")
        r["neg sum"] = _trio(dsn, "neg8", "sum(v)")

        _mk(dsn, "mix8", "CASE WHEN g % 2 = 0 THEN 9000000000000000000::bigint ELSE -8999999999999999999::bigint END", 20000)
        r["mix sum"] = _trio(dsn, "mix8", "sum(v)")
        r["mix sum want"] = f"agree:{_one(dsn, 'SELECT sum(v)::text FROM mix8_h')}"

        _mk(dsn, "e8", "g::bigint", 0)
        r["e8 sum"], r["e8 avg"] = _trio(dsn, "e8", "sum(v)"), _trio(dsn, "e8", "avg(v)")
        _mk(dsn, "z8", "NULL::bigint", 5000)
        r["z8 sum"], r["z8 avg"] = _trio(dsn, "z8", "sum(v)"), _trio(dsn, "z8", "avg(v)")

        _mk(dsn, "d8", "(g::numeric / 7)", 50000, "numeric")
        for agg in ("sum", "avg"):
            r[f"d8 {agg}"] = _trio(dsn, "d8", f"{agg}(v)")
            r[f"d8 {agg} want"] = f"agree:{_one(dsn, f'SELECT {agg}(v)::text FROM d8_h')}"

        _run(dsn, "DROP TABLE IF EXISTS perf8; CREATE TABLE perf8 (v bigint) USING pgcolumnar; "
                  "INSERT INTO perf8 SELECT (g % 1000)::bigint FROM generate_series(1,4000000) g;")
        r["perf rows"] = _one(dsn, "SELECT count(*) FROM perf8")
        r["vec perf"] = _vecplan(dsn, "sum(v)", "perf8")
        r["perf on"], r["perf off"] = _min_of_five(dsn, "SELECT sum(v) FROM perf8")

        _run(dsn, "DROP TABLE IF EXISTS perf8f; CREATE TABLE perf8f (k int, v bigint) USING pgcolumnar; "
                  "INSERT INTO perf8f SELECT g % 1000, ((g*2654435761)::bigint % 1000000007) FROM generate_series(1,4000000) g;")
        r["perff rows"] = _one(dsn, "SELECT count(*) FROM perf8f")
        r["perff distinct"] = _one(dsn, "SELECT count(DISTINCT v) FROM perf8f")
        r["vec perff"] = _vecplan(dsn, "sum(v)", "perf8f", " WHERE k < 50")
        r["perff on"], r["perff off"] = _min_of_five(dsn, "SELECT sum(v) FROM perf8f WHERE k < 50")

        _run(dsn, "CREATE TABLE rs_drv(i bigint); INSERT INTO rs_drv SELECT g FROM generate_series(1,4) g;")
        plan = _rows(dsn, "EXPLAIN (ANALYZE, TIMING OFF, SUMMARY OFF, COSTS OFF) "
                          + RSLAT.format(a="sum(s2.v)", t="a8"), RSGUC)
        lines = [] if _failed(plan) else [x[0] for x in plan]
        r["rs vec"] = plan if _failed(plan) else sum(1 for l in lines if "Columnar Vectorized Aggregates" in l)
        loops = [int(m) for l in lines for m in re.findall(r"loops=(\d+)", l)]
        r["rs loops"] = max(loops) if loops else 0
        out = _rows(dsn, RSLAT.format(a="sum(s2.v)", t="a8"), RSGUC)
        r["rs count"] = out if _failed(out) else sum(1 for x in out if x[1] is not None)
        for key, tbl, agg in (("rs sum", "a8", "sum(s2.v)"), ("rs avg", "a8", "avg(s2.v)"),
                              ("rs big", "big8", "sum(s2.v)"), ("rs count(*)", "a8", "count(*)")):
            r[key] = _rs_rows(dsn, tbl, agg)
            r[f"{key} heap"] = _rs_rows(dsn, f"{tbl}_h", agg)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _ratio(a, b, bound):
    if not (isinstance(a, float) and isinstance(b, float)) or a == 0 or b == 0:
        return query_error(f"not a measurement, so no ratio was formed: a=[{a}] b=[{b}]")
    ratio = round(a / b, 2)
    return "within" if ratio <= bound else f"{ratio:.2f}x exceeds the {bound}x bound (a={a:.1f} b={b:.1f})"


def test_the_vectorized_node_really_runs_for_sum(ia, expect):
    expect.text(str(ia["vec sum"]), "1", "premise: the vectorized node really runs for sum(v)")


def test_and_for_avg(ia, expect):
    expect.text(str(ia["vec avg"]), "1", "premise: and for avg(v)")


def test_sum_bigint_agrees_with_the_scalar_path_and_with_heap(ia, expect):
    expect.text(ia["a8 sum"], "agree:99900000", "sum(bigint) agrees with the scalar path and with heap")


def test_avg_bigint_agrees_too(ia, expect):
    expect.text(ia["a8 avg"], "agree:499.5000000000000000", "avg(bigint) agrees too")


def test_the_fixture_really_holds_nulls_and_negatives(ia, expect):
    expect.text(str(ia["n8 shape"]), "true/true", "premise: the fixture really holds NULLs and negatives")


def test_sum_over_a_column_with_nulls_and_negatives_agrees(ia, expect):
    expect.text(ia["n8 sum"], ia["n8 sum want"], "sum over a column with NULLs and negatives agrees")


def test_avg_over_the_same_agrees(ia, expect):
    expect.text(ia["n8 avg"], ia["n8 avg want"], "avg over the same agrees")


def test_the_sum_really_exceeds_what_int64_can_hold(ia, expect):
    expect.text(str(ia["big over"]), "true", "premise: the sum really exceeds what int64 can hold")


def test_the_vectorized_node_runs_on_this_table_too(ia, expect):
    expect.text(str(ia["vec big"]), "1", "premise: the vectorized node runs on this table too")


def test_a_sum_past_int64_agrees(ia, expect):
    expect.text(ia["big sum"], "agree:180000000000000000140000",
                "a sum past int64 agrees with the scalar path and with heap")


def test_and_its_avg_agrees(ia, expect):
    expect.text(ia["big avg"], ia["big avg want"], "and its avg agrees")


def test_this_sum_is_past_int64_in_the_negative_direction(ia, expect):
    expect.text(str(ia["neg over"]), "true", "premise: this sum is past int64 in the negative direction")


def test_a_large_negative_sum_agrees(ia, expect):
    expect.text(ia["neg sum"], "agree:-180000000000000000140000", "a large NEGATIVE sum agrees")


def test_mixed_signs_summing_back_into_int64_range_agree(ia, expect):
    expect.text(ia["mix sum"], ia["mix sum want"], "mixed signs summing back into int64 range agree")


def test_sum_over_an_empty_table_is_null(ia, expect):
    expect.text(ia["e8 sum"], "agree:<NULL>", "sum over an empty table is NULL, as on heap")


def test_avg_over_an_empty_table_is_null_too(ia, expect):
    expect.text(ia["e8 avg"], "agree:<NULL>", "avg over an empty table is NULL too")


def test_sum_over_an_all_null_column_is_null(ia, expect):
    expect.text(ia["z8 sum"], "agree:<NULL>", "sum over an all-NULL column is NULL")


def test_avg_over_an_all_null_column_is_null(ia, expect):
    expect.text(ia["z8 avg"], "agree:<NULL>", "avg over an all-NULL column is NULL")


def test_sum_numeric_keeps_its_scale_and_still_agrees(ia, expect):
    expect.text(ia["d8 sum"], ia["d8 sum want"], "sum(numeric) keeps its scale and still agrees")


def test_avg_numeric_too(ia, expect):
    expect.text(ia["d8 avg"], ia["d8 avg want"], "avg(numeric) too")


def test_the_timing_fixture_loaded_every_row(ia, expect):
    expect.text(str(ia["perf rows"]), "4000000", "premise: the timing fixture loaded every row")


def test_the_vectorized_node_really_runs_so_the_ratio_compares_two_paths(ia, expect):
    expect.text(str(ia["vec perf"]), "1",
                "premise: and the vectorized node really runs, so the ratio compares two paths")


def test_the_vectorized_path_is_not_slower_than_the_scalar_one(ia, expect):
    expect.text(_ratio(ia["perf on"], ia["perf off"], 1.10), "within",
                "the vectorized path is not slower than the scalar one")


def test_the_filtered_fixture_loaded_every_row(ia, expect):
    expect.text(str(ia["perff rows"]), "4000000", "premise: the filtered fixture loaded every row")


def test_its_values_are_not_low_cardinality(ia, expect):
    n = ia["perff distinct"]
    expect.text(("varied" if n > 1000000 else "collapsed") if isinstance(n, int) else n, "varied",
                "premise: its values are NOT low-cardinality, so the fold is not answered from runs")


def test_the_vectorized_node_runs_on_the_filtered_query_too(ia, expect):
    expect.text(str(ia["vec perff"]), "1", "premise: the vectorized node runs on the FILTERED query too")


def test_the_filtered_vectorized_path_is_not_slower(ia, expect):
    expect.text(_ratio(ia["perff on"], ia["perff off"], 1.10), "within",
                "the FILTERED vectorized path is not slower than the scalar one")


def test_the_rescan_shape_still_takes_the_vectorized_node(ia, expect):
    expect.text(str(ia["rs vec"]), "1", "premise: the rescan shape still takes the vectorized node")


def test_the_node_really_is_re_executed(ia, expect):
    n = ia["rs loops"]
    expect.text("rescanned" if n > 1 else f"not rescanned (loops={n})", "rescanned",
                "premise: the node really is re-executed, so a stale accumulator can show")


def test_the_rescan_query_returns_a_row_per_driver_row(ia, expect):
    expect.text(str(ia["rs count"]), "4", "premise: the rescan query returns a row per driver row, not an error")


def test_sum_bigint_across_a_rescan_matches_the_heap(ia, expect):
    expect.text(ia["rs sum"], ia["rs sum heap"], "sum(bigint) across a rescan matches the heap")


def test_avg_bigint_across_a_rescan_matches_the_heap(ia, expect):
    expect.text(ia["rs avg"], ia["rs avg heap"], "avg(bigint) across a rescan matches the heap")


def test_sum_bigint_across_a_rescan_on_the_big_fixture_matches_the_heap(ia, expect):
    expect.text(ia["rs big"], ia["rs big heap"], "sum(bigint) across a rescan on the >int64 fixture matches the heap")


def test_count_across_the_same_rescan_matches_the_heap(ia, expect):
    expect.text(ia["rs count(*)"], ia["rs count(*) heap"], "control: count(*) across the same rescan matches the heap")

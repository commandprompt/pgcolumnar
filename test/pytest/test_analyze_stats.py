"""ANALYZE collects column statistics on a columnar table (#154), and the planner prices
what they feed (#355, #362, #363, #376, #507). The pytest twin of
`test/analyze_stats.sh`, every check by the shell's name (#1341).

The sampler maps each block core chooses to the slice of its row group that the block
stands for, and offers that slice's live rows. The statistics are compared with a heap
table on identical data; the row count discriminates against cluster sampling, which
inflates reltuples by the blocks a group spans; correlation needs the sampled tuples'
item pointers. The fetch-penalty, decode-prefix and all-visible checks read plans and
costs built from those statistics.

A MISSING READING NEVER AGREES WITH ANOTHER. A crash restarts the backend and a fresh
connection answers again, so survival also requires the server log's count of killed
backends to be unchanged, and an unreadable log fails it. A statistic that is not there
is a failed query rather than a value, so two missing statistics cannot match, and every
negative plan check first requires a plan of the table it names.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). Each statement has a
connection of its own, as the shell's psql calls do.
"""
import pathlib
import re
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_analyze_stats"
SRCDIR = pathlib.Path(__file__).resolve().parents[2]
ROWS = 500000
WIDE_ROWS = 40000
O355_ROWS = 300000
O363_ROWS = 150000
IOSC_ROWS = 300000
TARGET = ROWS // 2
ORD = ["SET max_parallel_workers_per_gather = 0", "SET random_page_cost = 1.0"]
PENALTY_OFF = ["SET pgcolumnar.enable_index_fetch_penalty = off"]
INT64_MAX = 2 ** 63 - 1


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    """-> "" when the statements ran, else the error: the shell's psql_run."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())  # simple protocol: several statements, as psql -c
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql, settings=()):
    """-> the first column of the first row, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no row from {sql}") if row is None else row[0]


def _plan(dsn, sql, settings=()):
    """-> the plan as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute(sql))
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _stat(dsn, table, column, field):
    """-> the statistic as text, or a failed query when ANALYZE recorded none: a missing
    statistic is not a value, so two of them cannot agree."""
    v = _value(dsn, f"SELECT {field}::text FROM pg_stats "
                    f"WHERE tablename = '{table}' AND attname = '{column}'")
    if v is None:
        return query_error(f"no statistic: {table}.{column} {field}")
    return v


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _crashes(cluster):
    """-> how many backends the server log says died, or a failed reading when the log
    can't be read: a blind count must not read as "no crashes"."""
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _first_line(plan):
    return str(plan).splitlines()[0] if str(plan) else ""


def _uses(plan, marker):
    """-> "yes" when the plan holds `marker`, else what it was."""
    return "yes" if marker in str(plan) else f"no ({_first_line(plan)})"


def analyze_cap_ms(insert_ms):
    """The shell's pgc_analyze_cap_ms: the ceiling for ANALYZE, twenty times the insert's
    own time with a 5000 ms floor. Anything that is not a positive int64 is 0."""
    try:
        i = int(insert_ms)
    except (TypeError, ValueError):
        i = 0
    if not 0 < i <= INT64_MAX:
        i = 0
    return max(i * 20, 5000)


def _ms(dsn, sql):
    """-> (milliseconds, error) for one statement."""
    t0 = time.monotonic()
    err = _run(dsn, sql)
    return int((time.monotonic() - t0) * 1000), err


def _discarded(dsn, settings):
    """-> rows the filter threw away to return one row, or a failed query."""
    plan = _plan(dsn, "EXPLAIN (ANALYZE, COSTS off, TIMING off, SUMMARY off) "
                      f"SELECT * FROM as_c WHERE id = {TARGET}", settings)
    if plan.startswith(QUERY_ERROR):
        return plan
    if "Scan" not in plan:
        return query_error(f"no scan in the plan: {_first_line(plan)}")
    return sum(int(n) for n in re.findall(r"Rows Removed by Filter: (\d+)", plan))


def _total_cost(plan, node):
    m = re.search(re.escape(node) + r".*?\.\.([0-9.]+)", str(plan))
    return float(m.group(1)) if m else None


def _ios_gap(dsn):
    """-> (index scan) - (index only scan), the saving allvisfrac buys, or None. The
    penalty is off because it lands on T_IndexScan alone, inside the gap measured."""
    off = ["SET pgcolumnar.enable_custom_scan = off", "SET enable_seqscan = off",
           "SET enable_bitmapscan = off", "SET pgcolumnar.enable_index_fetch_penalty = off"]
    sql = "EXPLAIN SELECT k FROM iosc WHERE k BETWEEN 0 AND 200000"
    ios = _total_cost(_plan(dsn, sql, off), "Index Only Scan")
    idx = _total_cost(_plan(dsn, sql, off + ["SET enable_indexonlyscan = off"]), "Index Scan")
    return None if ios is None or idx is None else round(idx - ios, 2)


@pytest.fixture(scope="module")
def ans(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {"crashes before": _crashes(pgc_cluster)}

        # 1. the statistics agree with heap on identical data
        _run(dsn, "DROP TABLE IF EXISTS as_c; DROP TABLE IF EXISTS as_h; "
                  "CREATE TABLE as_c (id int, status text, v int, nullable int) USING pgcolumnar; "
                  "INSERT INTO as_c SELECT g, (ARRAY['new','open','shut'])[1 + (g % 3)], g % 500, "
                  f"CASE WHEN g % 4 = 0 THEN NULL ELSE g % 77 END FROM generate_series(1, {ROWS}) g; "
                  "CREATE TABLE as_h (LIKE as_c); INSERT INTO as_h SELECT * FROM as_c; "
                  "ANALYZE as_c; ANALYZE as_h;")
        r["alive"] = _value(dsn, "SELECT 'alive'")
        r["crashes after"] = _crashes(pgc_cluster)
        r["collects"] = _value(dsn, "SELECT (count(*) > 0)::text FROM pg_stats WHERE tablename = 'as_c'")
        r["null_frac"] = (_stat(dsn, "as_c", "nullable", "null_frac"), _stat(dsn, "as_h", "nullable", "null_frac"))
        for col in ("status", "v", "nullable"):
            r[f"nd {col}"] = (_stat(dsn, "as_c", col, "n_distinct"), _stat(dsn, "as_h", col, "n_distinct"))
        r["mcv"] = _value(dsn, """SELECT ((SELECT array_agg(x ORDER BY x) FROM unnest(
                (SELECT most_common_vals FROM pg_stats
                  WHERE tablename = 'as_c' AND attname = 'status')::text::text[]) x)
            = (SELECT array_agg(x ORDER BY x) FROM unnest(
                (SELECT most_common_vals FROM pg_stats
                  WHERE tablename = 'as_h' AND attname = 'status')::text::text[]) x))::text""")
        r["correlation"] = _stat(dsn, "as_c", "id", "correlation")
        try:
            r["t_self"] = (SRCDIR / "src" / "columnar_tableam.c").read_text().count(
                "tuple->t_self = slot->tts_tid")
        except OSError as exc:
            r["t_self"] = query_error(f"source unreadable: {exc}")
        _run(dsn, "ALTER TABLE as_c ALTER COLUMN id SET STATISTICS 10000; ANALYZE as_c;")
        r["id nd"] = _stat(dsn, "as_c", "id", "n_distinct")
        _run(dsn, "ALTER TABLE as_c ALTER COLUMN id SET STATISTICS -1; ANALYZE as_c;")

        # 2. the estimated row count is close to the truth
        r["reltuples"] = _value(dsn, "SELECT reltuples::bigint FROM pg_class WHERE relname = 'as_c'")

        # 3. a clustered table still estimates n_distinct
        _run(dsn, "DROP TABLE IF EXISTS as_ck; "
                  "CREATE TABLE as_ck (id int, ck int, payload text) USING pgcolumnar; "
                  "INSERT INTO as_ck SELECT g, g / 1000, repeat('p', 20) || g "
                  f"FROM generate_series(1, {ROWS}) g; ANALYZE as_ck;")
        r["true nd"] = _value(dsn, "SELECT count(DISTINCT ck) FROM as_ck")
        r["clustered nd"] = _stat(dsn, "as_ck", "ck", "n_distinct")

        # 4. a join, and the estimates a plan is built from
        _run(dsn, "DROP TABLE IF EXISTS as_dim; CREATE TABLE as_dim (ck int primary key, label text); "
                  "INSERT INTO as_dim SELECT DISTINCT ck, 'l' || ck FROM as_ck; ANALYZE as_dim;")
        r["join plan"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT count(*) FROM as_ck f "
                                    "JOIN as_dim d USING (ck) WHERE d.ck < 5")
        r["rows est"] = _value(dsn, "EXPLAIN (FORMAT JSON) SELECT * FROM as_c WHERE status = 'open'")
        _run(dsn, "CREATE INDEX IF NOT EXISTS as_c_id ON as_c (id); ANALYZE as_c;")
        r["point plan"] = _plan(dsn, f"EXPLAIN (COSTS off) SELECT * FROM as_c WHERE id = {TARGET}")
        r["discarded with"] = _discarded(dsn, [])
        r["discarded without"] = _discarded(dsn, ["SET enable_indexscan = off", "SET enable_bitmapscan = off"])

        # 5. ANALYZE on a wide table, inside a budget derived from the insert
        r["insert ms"], _ = _ms(dsn, "DROP TABLE IF EXISTS as_w; "
            "CREATE TABLE as_w (a bigint, b int, c int, d int, e timestamptz, "
            "f text, g text, h text) USING pgcolumnar; "
            "INSERT INTO as_w SELECT g, g, g % 1000, g % 7, "
            "'2020-01-01'::timestamptz + (g || ' sec')::interval, "
            "repeat('x', 200) || g, repeat('y', 200) || g, repeat('z', 200) || g "
            f"FROM generate_series(1, {WIDE_ROWS}) g;")
        r["analyze ms"], r["analyze err"] = _ms(dsn, "ANALYZE as_w;")

        # 6. the fetch penalty (#355, #362)
        _run(dsn, "DROP TABLE IF EXISTS o355; "
                  "CREATE TABLE o355 (id int, scat int, pad text) USING pgcolumnar; "
                  f"INSERT INTO o355 SELECT g, ((g::bigint * 48271) % {O355_ROWS})::int, repeat('p', 48) "
                  f"FROM generate_series(1, {O355_ROWS}) g; "
                  "CREATE INDEX o355_scat ON o355 (scat); CREATE INDEX o355_id ON o355 (id); ANALYZE o355;")
        r["plan off"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT * FROM o355 ORDER BY scat", ORD + PENALTY_OFF)
        r["plan on"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT * FROM o355 ORDER BY scat", ORD)
        r["plan cl"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT * FROM o355 ORDER BY id", ORD)
        r["plan pt"] = _plan(dsn, f"EXPLAIN (COSTS off) SELECT * FROM o355 WHERE id = {O355_ROWS // 2}", ORD)
        _run(dsn, "DROP TABLE IF EXISTS o362; "
                  "CREATE TABLE o362 (id int, h int, pad text) USING pgcolumnar; "
                  "INSERT INTO o362 SELECT g, g % 1000, repeat('q', 64) "
                  f"FROM generate_series(1, {O355_ROWS}) g; CREATE INDEX o362_h ON o362 (h); ANALYZE o362;")
        r["sel off"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT * FROM o362 WHERE h = 7", ORD + PENALTY_OFF)
        r["sel on"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT * FROM o362 WHERE h = 7", ORD)

        # 7. the decode is the attribute prefix, not the emitted columns (#363)
        _run(dsn, "DROP TABLE IF EXISTS o363; "
                  "CREATE TABLE o363 (id int, a1 text, a2 text, a3 text, a4 text, a5 text, "
                  "a6 text, a7 text, a8 text, a9 text, a10 text) USING pgcolumnar; "
                  "INSERT INTO o363 SELECT g, repeat('a',100), repeat('b',100), repeat('c',100), "
                  "repeat('d',100), repeat('e',100), repeat('f',100), repeat('g',100), "
                  "repeat('h',100), repeat('i',100), repeat('j',100) "
                  f"FROM generate_series(1, {O363_ROWS}) g; CREATE INDEX o363_id ON o363 (id); ANALYZE o363;")
        r["early"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT max(a1) FROM o363 WHERE id BETWEEN 1 AND 2000", ORD)
        r["late"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT max(a10) FROM o363 WHERE id BETWEEN 1 AND 2000", ORD)

        # 8. the penalty is bounded by a multiple of one scan (#376)
        r["lim"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT * FROM o355 ORDER BY scat LIMIT 10", ORD)
        r["nolim"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT * FROM o355 ORDER BY scat", ORD)

        # 9. a vacuumed table is priced with its all-visible pages (#507)
        _run(dsn, "DROP TABLE IF EXISTS iosc; "
                  "CREATE TABLE iosc (id int, k int, pad text) USING pgcolumnar; "
                  "INSERT INTO iosc SELECT g, ((g::bigint * 48271) % 1000000)::int, "
                  f"repeat(md5(g::text), 4) FROM generate_series(1, {IOSC_ROWS}) g; "
                  "CREATE INDEX iosc_k ON iosc (k); ANALYZE iosc;")
        _run(dsn, "VACUUM iosc;")
        r["allvisible"] = _value(dsn, "SELECT (relallvisible > 0)::text FROM pg_class WHERE relname = 'iosc'")
        r["range rows"] = _value(dsn, "SELECT count(*) FROM iosc WHERE k BETWEEN 0 AND 200000")
        r["ios plan"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT k FROM iosc WHERE k BETWEEN 0 AND 200000")
        r["all plan"] = _plan(dsn, "EXPLAIN (COSTS off) SELECT k FROM iosc WHERE k BETWEEN 0 AND 1000000")
        r["gap a"] = _ios_gap(dsn)
        # grown, and NOT analyzed or vacuumed, outside the predicate
        _run(dsn, "INSERT INTO iosc SELECT g, 200001 + ((g::bigint * 48271) % 799999)::int, "
                  "repeat(md5(g::text), 4) FROM generate_series(300001, 340000) g;")
        r["gap b"] = _ios_gap(dsn)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_server_survived_analyze(ans, expect):
    before, after = ans["crashes before"], ans["crashes after"]
    if not isinstance(before, int) or not isinstance(after, int):
        got = f"no (crash count unreadable: {before} / {after})"
    elif ans["alive"] == "alive" and after == before:
        got = "alive"
    else:
        got = f"no ([{ans['alive']}], {after - before} backend crashes)"
    expect.text(got, "alive", "the server survived ANALYZE")


def test_analyze_now_collects_statistics_at_all(ans, expect):
    expect.text(ans["collects"], "true", "ANALYZE now collects statistics at all")


def test_null_frac_matches_heap(ans, expect):
    c, h = (_float(v) for v in ans["null_frac"])
    if c is None or h is None:
        got = f"no reading ({ans['null_frac'][0]} vs {ans['null_frac'][1]})"
    else:
        got = "yes" if abs(c - h) < 0.02 else f"no ({c} vs {h})"
    expect.text(got, "yes", "null_frac matches heap within 0.02")


def test_n_distinct_on_status_matches_heap(ans, expect):
    expect.text(*ans["nd status"], "n_distinct on status matches heap")


def test_n_distinct_on_v_matches_heap(ans, expect):
    expect.text(*ans["nd v"], "n_distinct on v matches heap")


def test_n_distinct_on_nullable_matches_heap(ans, expect):
    expect.text(*ans["nd nullable"], "n_distinct on nullable matches heap")


def test_most_common_vals_holds_the_same_set_as_heap(ans, expect):
    expect.text(ans["mcv"], "true", "most_common_vals holds the same set as heap")


def test_correlation_on_an_ascending_column_is_near_1(ans, expect):
    c = _float(ans["correlation"])
    got = "yes" if c is not None and c > 0.9 else f"no ({ans['correlation']})"
    expect.text(got, "yes", "correlation on an ascending column is near 1")


def test_the_sampled_tuples_carry_a_valid_item_pointer(ans, expect):
    expect.num(ans["t_self"], 1, "the sampled tuples carry a valid item pointer")


def test_no_row_is_offered_to_the_sampler_twice(ans, expect):
    expect.text(ans["id nd"], "-1", "no row is offered to the sampler twice")


def test_reltuples_is_within_2_percent_of_the_true_row_count(ans, expect):
    e = ans["reltuples"]
    got = ("yes" if isinstance(e, int) and 0.98 < e / ROWS < 1.02 else f"no ({e} vs {ROWS})")
    expect.text(got, "yes", "reltuples is within 2% of the true row count")


def test_n_distinct_on_a_clustered_column_is_within_10_percent(ans, expect):
    g, t = _float(ans["clustered nd"]), ans["true nd"]
    if g is None or not isinstance(t, int) or t == 0:
        got = f"no reading ({ans['clustered nd']} vs {t})"
    elif g < 0:
        got = f"no (negative: {g})"
    else:
        got = "yes" if 0.9 < g / t < 1.1 else f"no ({g} vs {t})"
    expect.text(got, "yes", "n_distinct on a clustered column is within 10% of the truth")


def test_the_join_plans_as_a_join_rather_than_degenerating(ans, expect):
    n = len(re.findall(r"Nested Loop|Hash Join|Merge Join", str(ans["join plan"])))
    expect.text("yes" if n >= 1 else f"no ({_first_line(ans['join plan'])})", "yes",
                "the join plans as a join rather than degenerating")


def test_a_selective_equality_is_estimated_from_the_data(ans, expect):
    try:
        e = ans["rows est"][0]["Plan"]["Plan Rows"]
    except (TypeError, KeyError, IndexError):
        e = None
    got = ("yes" if isinstance(e, (int, float)) and 0.2 < e / ROWS < 0.5
           else f"no ({ans['rows est'] if e is None else e} of {ROWS})")
    expect.text(got, "yes", "a selective equality is estimated from the data, not the 0.5% default")


def test_a_point_lookup_still_uses_the_index_after_analyze(ans, expect):
    p = str(ans["point plan"])
    got = "yes" if re.search(r"Index (Only )?Scan|Bitmap Heap Scan", p) else f"no ({_first_line(p)})"
    expect.text(got, "yes", "a point lookup on an indexed column still uses the index after ANALYZE")


def test_having_an_index_available_saves_the_point_lookup_real_work(ans, expect):
    a, b = ans["discarded with"], ans["discarded without"]
    if not isinstance(a, int) or not isinstance(b, int):
        got = f"no reading ({a} with the index, {b} without)"
    else:
        got = "yes" if b > 0 and a < b / 2 else f"no ({a} with the index, {b} without)"
    expect.text(got, "yes", "having an index available saves the point lookup real work")


def test_the_cap_is_exposed_as_a_function(expect):
    expect.num(int(callable(analyze_cap_ms)), 1, "premise: the cap is exposed as a function, not written inline")


def test_a_fast_machine_still_gets_the_floor(expect):
    expect.num(analyze_cap_ms(1), 5000, "a fast machine still gets the floor, not a bound that shrinks with it")


def test_a_slow_machine_gets_a_budget_proportional_to_its_insert(expect):
    expect.num(analyze_cap_ms(1000), 20000, "and a slow one gets a budget proportional to its own insert")


def test_the_floor_and_the_derived_bound_meet(expect):
    expect.num(analyze_cap_ms(250), 5000, "the floor and the derived bound meet where they should")


def test_one_millisecond_past_the_floor_the_derived_bound_takes_over(expect):
    expect.num(analyze_cap_ms(251), 5020, "and one millisecond past it the derived bound takes over")


def test_a_zero_insert_time_still_yields_the_floor(expect):
    expect.num(analyze_cap_ms(0), 5000, "a missing or zero insert time still yields the floor, not zero")


def test_a_malformed_insert_time_yields_the_floor(expect):
    expect.num(analyze_cap_ms("12abc"), 5000,
               "a malformed insert time yields the floor rather than an arithmetic error")


def test_an_insert_time_past_int64_yields_the_floor(expect):
    expect.num(analyze_cap_ms("99999999999999999999"), 5000,
               "and an insert time past int64 yields the floor rather than a cap that cannot fail")


def test_analyze_on_a_wide_table_completes_inside_its_cap(ans, expect):
    an, cap = ans["analyze ms"], analyze_cap_ms(ans["insert ms"])
    if ans["analyze err"]:
        got = f"no (ANALYZE failed after {an} ms: {ans['analyze err']})"
    else:
        got = "yes" if an < cap else f"no ({an}ms against a {cap}ms cap)"
    expect.text(got, "yes", "ANALYZE on a wide table completes well inside its derived cap")


def test_without_the_penalty_an_unclustered_order_by_takes_the_index(ans, expect):
    expect.text(_uses(ans["plan off"], "Index Scan using o355_scat"), "yes",
                "without the fetch penalty an unclustered ORDER BY takes the index (#355 premise)")


def test_the_penalty_makes_an_unclustered_order_by_sort(ans, expect):
    p = str(ans["plan on"])
    got = ("yes" if "Sort" in p and "Index Scan using o355_scat" not in p else f"no ({_first_line(p)})")
    expect.text(got, "yes", "the fetch penalty makes an unclustered ORDER BY sort rather than fetch per row (#355)")


def test_the_penalty_leaves_a_clustered_order_by_on_its_index(ans, expect):
    expect.text(_uses(ans["plan cl"], "Index Scan using o355_id"), "yes",
                "the fetch penalty leaves a clustered ORDER BY on its index (#355 must not over-fire)")


def test_the_penalty_leaves_a_selective_point_lookup_on_the_index(ans, expect):
    p = str(ans["plan pt"])
    got = "yes" if re.search(r"Index (Only )?Scan|Bitmap Heap Scan", p) else f"no ({_first_line(p)})"
    expect.text(got, "yes", "the fetch penalty leaves a selective point lookup on the index (#355 vs #171)")


def test_without_the_penalty_a_scattered_condition_takes_the_index(ans, expect):
    expect.text(_uses(ans["sel off"], "Index Scan using o362_h"), "yes",
                "without the penalty a selective scattered condition takes the index (#362 premise)")


def test_the_penalty_is_applied_before_the_columnar_path_is_offered(ans, expect):
    p = str(ans["sel on"])
    got = ("yes" if "Custom Scan (PgColumnarScan)" in p and "Index Scan using o362_h" not in p
           else f"no ({_first_line(p)})")
    expect.text(got, "yes",
                "the penalty is applied before the columnar path is offered, so it can still win (#362)")


def test_an_early_columns_short_decode_prefix_stays_on_the_index(ans, expect):
    expect.text(_uses(ans["early"], "Index Scan using o363_id"), "yes",
                "an early column's short decode prefix leaves it on the index (#363)")


def test_a_late_columns_wide_decode_prefix_costs_it_off_the_index(ans, expect):
    p = str(ans["late"])
    got = ("yes" if re.search(r" on o363( |$)", p, re.M) and "Index Scan using o363_id" not in p
           else f"no ({_first_line(p)})")
    expect.text(got, "yes", "a late column's wide decode prefix costs it off the index (#363)")


def test_a_small_limit_still_reaches_the_index(ans, expect):
    expect.text(_uses(ans["lim"], "Index Scan using o355_scat"), "yes",
                "a small LIMIT still reaches the index through the fetch penalty (#376)")


def test_the_bound_does_not_disable_the_penalty_for_a_full_read(ans, expect):
    p = str(ans["nolim"])
    got = ("yes" if "Sort" in p and "Index Scan using o355_scat" not in p else f"no ({_first_line(p)})")
    expect.text(got, "yes", "the bound does not disable the penalty for a full ordered read (#376)")


def test_vacuum_recorded_all_visible_pages(ans, expect):
    expect.text(ans["allvisible"], "true", "premise: VACUUM recorded all-visible pages to price with (#507)")


def test_the_selective_range_has_its_rows(ans, expect):
    expect.num(ans["range rows"], 60003, "premise: the selective range has its rows (#507)")


def test_a_vacuumed_columnar_table_can_reach_an_index_only_scan(ans, expect):
    expect.text(_uses(ans["ios plan"], "Index Only Scan using iosc_k"), "yes",
                "a vacuumed columnar table can reach an index-only scan (#507)")


def test_the_all_visible_fraction_does_not_hand_every_query_to_the_index(ans, expect):
    expect.text(_uses(ans["all plan"], "Custom Scan (PgColumnarScan)"), "yes",
                "the all-visible fraction does not hand every query to the index (#507)")


def test_both_all_visible_estimates_were_taken(ans, expect):
    a, b = ans["gap a"], ans["gap b"]
    expect.text("yes" if a is not None and b is not None else f"no (a={a} b={b})", "yes",
                "premise: both estimates were taken (#507)")


def test_the_all_visible_pages_buy_the_index_only_scan_a_saving(ans, expect):
    a = ans["gap a"]
    got = (f"no reading (a=[{a}])" if a is None
           else "yes" if a > 0 else f"no (index scan minus index-only scan is {a})")
    expect.text(got, "yes", "the all-visible pages buy the index-only scan a saving (#507)")


def test_growth_without_vacuum_does_not_inflate_the_saving(ans, expect):
    a, b = ans["gap a"], ans["gap b"]
    if a is None or b is None:
        got = f"no reading (a=[{a}] b=[{b}])"
    else:
        tol = round(a * 1.05, 2)
        got = "yes" if b <= tol else f"no (grown {b} above vacuumed {a}, tolerance {tol})"
    expect.text(got, "yes", "growth without vacuum does not inflate the all-visible saving (#507)")

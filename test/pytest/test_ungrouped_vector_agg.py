"""The ungrouped vectorized aggregate returns what core Agg returns (#289). The pytest twin
of `test/ungrouped_vector_agg.sh`, every check by the shell's name (#1341).

`pgcolumnar.enable_ungrouped_vector_agg` routes an ungrouped aggregate that a zone map
cannot answer (one with a WHERE filter, or sum/avg over int8 or float) to a single-pass
scan-fold node. Asserted: the node runs with the setting on and not off; it returns
byte for byte what core Agg returns, across types, NULLs, filters, empty and all-NULL
inputs, and after deletes; it prunes the row groups the row path prunes (#349); a varlena
or by-reference column falls back off the batch fold (#423); a clause with no exact scan
key falls back (#715); and sum/avg over numeric are refused whole (#785).

WHAT MAKES A COMPARISON COUNT. An on/off pair reads "same (VALUE)" only when both sides
returned a value; an error or an empty result on either side reads "no reading". Every
fixture is held to its row count first, because over an empty table every aggregate here
agrees with itself: with t's INSERT made to load nothing, the shell passed 56 of 56. A
plan is read as "no vectorized node" only when there is a plan, since a failed EXPLAIN
shows no node either.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order, with serial
execution pinned in both arms (a parallel core Agg sums floats in another order).
"""
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_ungrouped_vector_agg"
GUC = "pgcolumnar.enable_ungrouped_vector_agg"
NOPAR = "SET max_parallel_workers_per_gather = 0"
VNODE = "Columnar Vectorized Aggregates"

# Each on/off pair: the shell's name, its query, and whether it runs after the deletes.
AB = [
    ("filtered float sum :: on==off", "SELECT sum(v)::text FROM t WHERE k > 500"),
    ("filtered float avg :: on==off", "SELECT avg(v)::text FROM t WHERE k > 500"),
    ("q6-like count+avg filter :: on==off", "SELECT count(*)||'/'||coalesce(avg(v)::text,'z') FROM t WHERE v > 0"),
    ("filtered int8 sum :: on==off", "SELECT sum(i8)::text FROM t WHERE k < 250"),
    ("filtered int8 avg :: on==off", "SELECT avg(i8)::text FROM t WHERE k < 250"),
    ("filtered numeric sum :: on==off", "SELECT sum(n)::text FROM t WHERE k <> 3"),
    ("filtered numeric avg :: on==off", "SELECT avg(n)::text FROM t WHERE k <> 3"),
    ("filtered int sum/avg/cnt :: on==off", "SELECT sum(i4)||'/'||avg(i4)::text||'/'||count(i4) FROM t WHERE k >= 100"),
    ("filtered min/max float :: on==off",
     "SELECT coalesce(min(v)::text,'z')||'/'||coalesce(max(v)::text,'z') FROM t WHERE k > 900"),
    ("filtered count(*) :: on==off", "SELECT count(*) FROM t WHERE k BETWEEN 100 AND 200"),
    ("unfiltered int8 sum :: on==off", "SELECT sum(i8)::text FROM t"),
    ("unfiltered float sum :: on==off", "SELECT sum(v)::text FROM t"),
    ("unfiltered float avg :: on==off", "SELECT avg(v)::text FROM t"),
    ("unfiltered numeric avg :: on==off", "SELECT avg(n)::text FROM t"),
    ("all rows filtered out :: on==off",
     "SELECT count(*)||'/'||coalesce(sum(v)::text,'z')||'/'||coalesce(avg(v)::text,'z') FROM t WHERE k > 100000"),
    ("avg over a null-heavy col :: on==off", "SELECT coalesce(avg(v)::text,'z') FROM t WHERE k % 50 = 0"),
    ("count(*) no filter (metadata) :: on==off", "SELECT count(*) FROM t"),
    ("min/max no filter (metadata) :: on==off", "SELECT min(v)::text||'/'||max(v)::text FROM t"),
    ("sum(int4) no filter (metadata) :: on==off", "SELECT sum(i4)::text FROM t"),
]
AB_AFTER_DELETES = [
    ("filtered avg after deletes :: on==off", "SELECT coalesce(avg(v)::text,'z') FROM t WHERE k > 500"),
    ("count(*) filter after deletes :: on==off", "SELECT count(*) FROM t WHERE k > 500"),
]
AB_CLUSTERED = [
    ("clustered filtered agg :: on==off",
     "SELECT count(*)::text || '|' || coalesce(sum(y)::text,'z') FROM cl WHERE x > 490000"),
]
ALL_NULL = [
    ("all-null float sum on==off", "SELECT coalesce(sum(v)::text,'NULL') FROM t_allnull WHERE k > 2"),
    ("all-null float avg on==off", "SELECT coalesce(avg(v)::text,'NULL') FROM t_allnull WHERE k > 2"),
]
VARLENA = [
    ("a varlena shape agrees with heap: count(*) FROM vfold WHERE s LIKE '%9%'",
     "SELECT count(*) FROM vfold WHERE s LIKE '%9%'"),
    ("a varlena shape agrees with heap: count(*) FROM vfold WHERE i > 100 AND s LIKE '%9%'",
     "SELECT count(*) FROM vfold WHERE i > 100 AND s LIKE '%9%'"),
    ("a varlena shape agrees with heap: sum(i) FROM vfold WHERE s LIKE '%9%'",
     "SELECT sum(i) FROM vfold WHERE s LIKE '%9%'"),
    ("a varlena shape agrees with heap: count(*) FROM vfold WHERE s = 'abc7'",
     "SELECT count(*) FROM vfold WHERE s = 'abc7'"),
    ("a varlena shape agrees with heap: count(*) FROM vfold WHERE length(s) > 4",
     "SELECT count(*) FROM vfold WHERE length(s) > 4"),
    ("a varlena shape agrees with heap: count(uid) FROM vfold WHERE i > 100",
     "SELECT count(uid) FROM vfold WHERE i > 100"),
    ("a varlena shape agrees with heap: count(nm) FROM vfold WHERE i > 100",
     "SELECT count(nm) FROM vfold WHERE i > 100"),
    ("issue #715: count(*) WHERE i <> 100 agrees with heap", "SELECT count(*) FROM vfold WHERE i <> 100"),
    ("issue #715: sum(i) WHERE i <> 100 agrees with heap", "SELECT sum(i) FROM vfold WHERE i <> 100"),
    ("control: i > 100 (exact key) still folds and agrees with heap", "SELECT count(*) FROM vfold WHERE i > 100"),
]
FOLD = [
    ("a fixed-width filter still uses the batch fold", "SELECT count(*) FROM vfold WHERE i > 100", "yes"),
    ("a varlena filter falls back, and EXPLAIN says so", "SELECT count(*) FROM vfold WHERE s LIKE '%9%'", "no"),
    ("a fixed-width BY-REFERENCE column also falls back (uuid)", "SELECT count(uid) FROM vfold WHERE i > 100", "no"),
    ("and name, which is 64 bytes by reference", "SELECT count(nm) FROM vfold WHERE i > 100", "no"),
    ("issue #715: the no-scan-key op (<>) falls back off the fold", "SELECT count(*) FROM vfold WHERE i <> 100", "no"),
]
NODES = [
    ("premise: the node DOES run for float8, so a 0 below means refusal and not absence",
     "SELECT sum(d) FROM vnumc WHERE i > 5", 1),
    ("sum(numeric) is refused from the scan-fold path", "SELECT sum(c) FROM vnumc WHERE i > 5", 0),
    ("avg(numeric) too", "SELECT avg(c) FROM vnumc WHERE i > 5", 0),
    ("and a MIXED query is refused whole, because classification is all or nothing",
     "SELECT sum(c), sum(d) FROM vnumc WHERE i > 5", 0),
]
REFUSED = [
    ("refused numeric still matches the heap oracle [sum(c)]", "sum(c)"),
    ("refused numeric still matches the heap oracle [avg(c)]", "avg(c)"),
    ("refused numeric still matches the heap oracle [sum(c)::text||'/'||sum(d)::text]",
     "sum(c)::text||'/'||sum(d)::text"),
]


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- step failed: {str(exc).splitlines()[0]}")


def _val(dsn, sql, settings=()):
    """-> the first column as text ("" for NULL or no row), or a failed query."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


def _count(dsn, table):
    v = _val(dsn, f"SELECT count(*) FROM {table}")
    return int(v) if v.isdigit() else v


def _plan(dsn, sql, settings=()):
    """-> the plan text, or a failed query."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS OFF) {sql}"))
    except psycopg.Error as exc:
        return _failed(exc)


def _guc(on):
    return [NOPAR, f"SET {GUC} = {'on' if on else 'off'}"]


def _ab(dsn, sql):
    """-> "same (VALUE)", both values, or "no reading" when either side failed or is empty."""
    off, on = _val(dsn, sql, _guc(False)), _val(dsn, sql, _guc(True))
    if not on or not off or on.startswith(QUERY_ERROR) or off.startswith(QUERY_ERROR):
        return f"no reading: on [{on}] off [{off}]"
    return f"same ({on})" if on == off else f"differ: on [{on}] off [{off}]"


def _same(reading):
    return "same" if reading.startswith("same (") else reading


def _has(plan, marker):
    return "yes" if re.search(marker, plan, re.I) else "no"


def _fold(dsn, sql):
    """-> "yes"/"no" from "Columnar Batch Fold: ...", or "" when the plan has no such line."""
    m = re.search(r"Columnar Batch Fold: ([a-z]+)", _plan(dsn, sql, [f"SET {GUC} = on"]))
    return m.group(1) if m else ""


def _vnode(dsn, sql):
    """-> how many vectorized nodes the plan has, or the plan when it has no Aggregate at
    all: a failed EXPLAIN shows no node, which is the refusal these checks want."""
    plan = _plan(dsn, sql, [f"SET {GUC} = on"])
    if "Aggregate" not in plan:
        return f"no plan: {plan.splitlines()[0] if plan else ''}"
    return plan.count(VNODE)


def _removed(dsn, on):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in _guc(on):
                conn.execute(s)
            plan = "\n".join(r[0] for r in conn.execute(
                "EXPLAIN (ANALYZE, TIMING OFF, SUMMARY OFF, COSTS OFF) "
                "SELECT count(*), sum(y) FROM cl WHERE x > 490000"))
    except psycopg.Error as exc:
        return _failed(exc)
    m = re.search(r"Removed by Filter: (\d+)", plan, re.I)
    return int(m.group(1)) if m else f"no reading: {plan.splitlines()[0] if plan else ''}"


@pytest.fixture(scope="module")
def uv(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _run(dsn, """CREATE TABLE t (id int, k int, i4 int, i8 bigint, v float8, n numeric, txt text)
                       USING pgcolumnar;
                     SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 2000);
                     INSERT INTO t
                     SELECT g, g % 1000, (g % 7) - 3, (g::bigint * 1000003),
                            CASE WHEN g % 50 = 0 THEN NULL
                                 ELSE ((g % 13) - 6)::float8 * (10.0 ^ (g % 5)) END,
                            CASE WHEN g % 37 = 0 THEN NULL ELSE (g % 100)::numeric / 7 END,
                            'r' || g
                     FROM generate_series(1, 60000) g""")
        r["t rows"] = _count(dsn, "t")
        shape = "SELECT count(*), sum(v), avg(v) FROM t WHERE v > 0"
        r["plan on"], r["plan off"] = _plan(dsn, shape, _guc(True)), _plan(dsn, shape, _guc(False))
        r["plan minmax"] = _plan(dsn, "SELECT min(v), max(v) FROM t WHERE k > 5", _guc(True))
        for name, sql in AB:
            r[name] = _ab(dsn, sql)
        _run(dsn, """CREATE TABLE t_allnull (id int, k int, v float8) USING pgcolumnar;
                     INSERT INTO t_allnull SELECT g, g%10, NULL FROM generate_series(1,5000) g""")
        r["allnull rows"] = _count(dsn, "t_allnull")
        for name, sql in ALL_NULL:
            r[name] = _ab(dsn, sql)
        _run(dsn, "DELETE FROM t WHERE id IN (5, 2005, 40001)")
        for name, sql in AB_AFTER_DELETES:
            r[name] = _ab(dsn, sql)

        _run(dsn, """CREATE TABLE cl (x float8, y float8) USING pgcolumnar;
                     SELECT pgcolumnar.set_options('cl'::regclass, stripe_row_limit => 10000);
                     INSERT INTO cl SELECT g::float8, (g % 100)::float8
                         FROM generate_series(1, 500000) g ORDER BY g""")
        r["removed on"], r["removed off"] = _removed(dsn, True), _removed(dsn, False)
        for name, sql in AB_CLUSTERED:
            r[name] = _ab(dsn, sql)
        r["up"] = _val(dsn, "SELECT 1")

        _run(dsn, """CREATE TABLE vfold (i int, s text, uid uuid, nm name) USING pgcolumnar;
                     INSERT INTO vfold SELECT g, 'abc'||g, gen_random_uuid(), ('n'||g)::name
                       FROM generate_series(1,200000) g;
                     CREATE TABLE vfoldh (i int, s text, uid uuid, nm name);
                     INSERT INTO vfoldh SELECT * FROM vfold""")
        r["uuid"] = _val(dsn, "SELECT attlen||'/'||attbyval FROM pg_attribute "
                              "WHERE attrelid='vfold'::regclass AND attname='uid'")
        r["vfold rows"] = (f"{_val(dsn, 'SELECT count(*) FROM vfold')}/"
                           f"{_val(dsn, 'SELECT count(*) FROM vfoldh')}")
        r["vfold am"] = _val(dsn, "SELECT amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam "
                                  "WHERE c.relname='vfold'")
        for name, sql in VARLENA:
            r[name] = (_val(dsn, sql, [f"SET {GUC} = on"]), _val(dsn, sql.replace("vfold", "vfoldh")))
        for name, sql, _ in FOLD:
            r[name] = _fold(dsn, sql)

        _run(dsn, """CREATE TABLE vnum (c numeric, d float8, i int);
                     INSERT INTO vnum SELECT (g%997)::numeric, (g%997)::float8, g FROM generate_series(1,20000) g;
                     CREATE TABLE vnumc (LIKE vnum) USING pgcolumnar;
                     INSERT INTO vnumc SELECT * FROM vnum""")
        r["vnum rows"] = (f"{_val(dsn, 'SELECT count(*) FROM vnum')}/"
                          f"{_val(dsn, 'SELECT count(*) FROM vnumc')}")
        for name, sql, _ in NODES:
            r[name] = _vnode(dsn, sql)
        for name, e in REFUSED:
            r[name] = (_val(dsn, f"SELECT ({e})::text FROM vnumc", [f"SET {GUC} = on"]),
                       _val(dsn, f"SELECT ({e})::text FROM vnum"))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- the fixture, and the premise that the new path runs ---------------------

def test_the_fixture_holds_its_rows(uv, expect):
    expect.num(uv["t rows"], 60000, "premise: the fixture holds its 60000 rows")


def test_the_node_is_used_with_the_setting_on(uv, expect):
    expect.text(_has(uv["plan on"], VNODE), "yes", "premise: vectorized agg node used when GUC on")


def test_core_agg_runs_with_the_setting_off(uv, expect):
    p = uv["plan off"]
    got = "vectorized" if _has(p, VNODE) == "yes" else ("core Agg" if "Aggregate" in p else
                                                       f"no plan: {p.splitlines()[0] if p else ''}")
    expect.text(got, "core Agg", "premise: core Agg (no vectorized node) when GUC off")


def test_the_filter_is_pushed_into_the_scan(uv, expect):
    expect.text(_has(uv["plan on"], "Columnar Pushed-Down Filters"), "yes",
                "premise: the filter is pushed into the scan (EXPLAIN shows it)")


def test_an_eligible_shape_uses_the_batch_fold(uv, expect):
    expect.text(_has(uv["plan on"], "Batch Fold: yes"), "yes", "premise: eligible shape uses the batch fold")


def test_min_max_falls_back_off_the_batch_fold(uv, expect):
    expect.text(_has(uv["plan minmax"], "Batch Fold: no"), "yes", "premise: min/max falls back off the batch fold")


# ---- the new path returns what core Agg returns -------------------------------

@pytest.mark.parametrize("name,sql", AB)
def test_the_fold_matches_core_agg(uv, name, sql, expect):
    expect.text(_same(uv[name]), "same", name)


def test_the_all_null_fixture_holds_its_rows(uv, expect):
    expect.num(uv["allnull rows"], 5000, "premise: the all-null fixture holds its 5000 rows")


@pytest.mark.parametrize("name,sql", ALL_NULL)
def test_an_all_null_column_matches_core_agg(uv, name, sql, expect):
    expect.text(uv[name], "same (NULL)", name)


@pytest.mark.parametrize("name,sql", AB_AFTER_DELETES)
def test_the_fold_matches_core_agg_after_deletes(uv, name, sql, expect):
    expect.text(_same(uv[name]), "same", name)


# ---- the fold prunes row groups (#349 item 2) ----------------------------------

def test_the_fold_prunes_groups_on_a_clustered_filter(uv, expect):
    n = uv["removed on"]
    got = ("yes" if n > 0 else f"no ({n})") if isinstance(n, int) else n
    expect.text(got, "yes", "fold prunes groups on a clustered filter (removed>0)")


def test_the_fold_prunes_the_same_groups_as_the_row_path(uv, expect):
    expect.num(uv["removed on"], uv["removed off"], "fold prunes the same groups the row path does")


@pytest.mark.parametrize("name,sql", AB_CLUSTERED)
def test_the_clustered_fold_matches_core_agg(uv, name, sql, expect):
    expect.text(_same(uv[name]), "same", name)


def test_the_server_is_still_up(uv, expect):
    expect.text(uv["up"], "1", "server still up")


# ---- varlena and by-reference columns fall back (#423), and #715 ----------------

def test_uuid_is_fixed_width_but_not_by_value(uv, expect):
    expect.text(uv["uuid"], "16/false", "premise: uuid is fixed width but NOT by value, which is the whole point")


def test_the_varlena_fixture_holds_its_rows(uv, expect):
    expect.text(uv["vfold rows"], "200000/200000", "premise: the varlena fixture holds its 200000 rows, in both tables")


def test_the_varlena_fixture_is_columnar(uv, expect):
    expect.text(uv["vfold am"], "pgcolumnar", "premise: the varlena fixture really is columnar")


@pytest.mark.parametrize("name,sql", VARLENA)
def test_a_shape_agrees_with_heap(uv, name, sql, expect):
    got, want = uv[name]
    expect.text(got, want, name)


@pytest.mark.parametrize("name,sql,want", FOLD)
def test_the_batch_fold_decision(uv, name, sql, want, expect):
    expect.text(f"Columnar Batch Fold: {uv[name]}" if uv[name] else "", f"Columnar Batch Fold: {want}", name)


# ---- numeric is refused from this path (#785) ----------------------------------

def test_the_numeric_fixture_holds_its_rows(uv, expect):
    expect.text(uv["vnum rows"], "20000/20000", "premise: the numeric fixture holds its 20000 rows, in both tables")


@pytest.mark.parametrize("name,sql,want", NODES)
def test_the_vectorized_node_count(uv, name, sql, want, expect):
    expect.num(uv[name], want, name)


@pytest.mark.parametrize("name,expr", REFUSED)
def test_a_refused_numeric_aggregate_matches_heap(uv, name, expr, expect):
    got, want = uv[name]
    expect.text(got, want, name)

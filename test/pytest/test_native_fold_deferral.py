"""Fold-path payload deferral, with the work measured (#405 step 2). The pytest twin of
`test/native_fold_deferral.sh`, every check by the shell's name (#1341).

On the batch-fold path the payload (non-key) columns of a row group are materialized
only for rows that pass the scan keys, gated per group: defer unless the survival seen
so far exceeds half, the first group optimistic. EXPLAIN ANALYZE reports the work as
`Columnar Fold Payload Loads` (on a deferred group, survivors times payload width) and
the gate as `Columnar Fold Deferred Groups: X of Y`.

The answers are compared three ways: the fold, the row path (the vectorized aggregate
off) and a heap mirror. THE MIRROR IS WRITTEN FROM THE GENERATOR, not copied from the
columnar table: a copy reads through the scan under test, and with the writer storing
one value off by one the shell passed every check (#1339).

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next. A failed query is a
`query_error()`, so two failures never compare equal, and a count that could not be
read is never defaulted to 0.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_fold_deferral"
GUC = "SET pgcolumnar.enable_ungrouped_vector_agg = on"
ROW = "SET pgcolumnar.enable_ungrouped_vector_agg = off"
NP = 4                      # payload columns
ROWS = 120000
QSEL = "SELECT sum(p1), sum(p2), sum(p3), sum(p4) FROM fd_t WHERE q <= 1"
QHI = "SELECT sum(p1), sum(p2), sum(p3), sum(p4) FROM fd_t WHERE q <= 99"
GEN = f"SELECT 1 + g % 100, g, g*2, g*3, g*4 FROM generate_series(1,{ROWS}) g"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _row(dsn, sql, setting=None):
    """-> the first row's columns joined by '|', as psql -At prints them, or the failure
    as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if setting:
                conn.execute(setting)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    if row is None:
        return query_error(f"no row from {sql}")
    return "|".join("" if c is None else str(c) for c in row)


def _int(dsn, sql):
    """-> the single value as an int, or why there is none."""
    v = _row(dsn, sql)
    if v.lstrip("-").isdigit():
        return int(v)
    return v if v.startswith("QUERY_ERROR") else query_error(f"not a number from {sql}: [{v}]")


def _plan_line(dsn, label, sql):
    """-> the EXPLAIN ANALYZE line carrying `label` under this suite's GUC, stripped, or
    a query_error() when the plan failed or has no such line."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(GUC)
            lines = [r[0] for r in conn.execute(
                f"EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF) {sql}").fetchall()]
    except psycopg.Error as exc:
        return _failed(exc)
    for line in lines:
        if label in line:
            return line.strip()
    return query_error(f"the plan has no {label} line")


def _trailing_int(line):
    m = re.search(r"(\d+)$", line) if isinstance(line, str) and not line.startswith("QUERY_ERROR") else None
    return int(m.group(1)) if m else query_error(f"no number at the end of [{line}]")


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


@pytest.fixture(scope="module")
def fd(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE fd_t (q int4, p1 float8, p2 float8, p3 float8, p4 float8) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('fd_t', stripe_row_limit => 20000)")
        _run(dsn, f"INSERT INTO fd_t {GEN}")
        _run(dsn, "CREATE TABLE fd_heap (q int4, p1 float8, p2 float8, p3 float8, p4 float8)")
        _run(dsn, f"INSERT INTO fd_heap {GEN}")
        r = {}
        fold = _plan_line(dsn, "Columnar Batch Fold", QSEL)
        r["takes fold"] = (fold if fold.startswith("QUERY_ERROR")
                           else (re.search(r"yes|no", fold) or [""])[0] or f"no yes/no in [{fold}]")
        r["ngroups"] = _int(dsn, "SELECT count(*) FROM pgcolumnar.stats('fd_t'::regclass)")
        r["surv"] = _int(dsn, "SELECT count(*) FROM fd_t WHERE q <= 1")
        r["cand"] = _int(dsn, "SELECT count(*) FROM fd_t")
        r["fold sel"] = _row(dsn, QSEL, GUC)
        r["row sel"] = _row(dsn, QSEL, ROW)
        r["heap sel"] = _row(dsn, "SELECT sum(p1), sum(p2), sum(p3), sum(p4) FROM fd_heap WHERE q <= 1")
        r["fold hi"] = _row(dsn, QHI, GUC)
        r["heap hi"] = _row(dsn, "SELECT sum(p1), sum(p2), sum(p3), sum(p4) FROM fd_heap WHERE q <= 99")
        r["loads line"] = _plan_line(dsn, "Columnar Fold Payload Loads", QSEL)
        r["defer line"] = _plan_line(dsn, "Columnar Fold Deferred Groups", QSEL)
        r["defer hi line"] = _plan_line(dsn, "Columnar Fold Deferred Groups", QHI)
        r["loads hi line"] = _plan_line(dsn, "Columnar Fold Payload Loads", QHI)
        r["surv hi"] = _int(dsn, "SELECT count(*) FROM fd_t WHERE q <= 99")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _of(line):
    """-> 'X of Y' from a Deferred Groups line, or why there is none."""
    m = re.search(r"(\d+) of (\d+)", line) if not line.startswith("QUERY_ERROR") else None
    return m if m else None


def test_fold_deferral_the_selective_query_takes_the_fold(fd, expect):
    expect.text(fd["takes fold"], "yes", "the selective query takes the fold (ANALYZE, post-#602)")


def test_fold_deferral_the_selective_predicate_is_selective(fd, expect):
    s, c = fd["surv"], fd["cand"]
    got = ("yes" if s * 50 <= c else f"no ({s} of {c})") if isinstance(s, int) and isinstance(c, int) \
        else f"no reading ({s}, {c})"
    expect.text(got, "yes", "premise: the selective predicate is genuinely selective (<= 2%)")


def test_fold_deferral_deferred_fold_equals_row_path(fd, expect):
    expect.text(fd["fold sel"], fd["row sel"], "deferred fold == row path (selective)")


def test_fold_deferral_deferred_fold_equals_heap_mirror(fd, expect):
    expect.text(fd["fold sel"], fd["heap sel"], "deferred fold == heap mirror (selective)")


def test_fold_deferral_high_survival_fold_equals_heap_mirror(fd, expect):
    expect.text(fd["fold hi"], fd["heap hi"], "fold == heap mirror (high survival)")


def test_fold_deferral_the_loads_counter_exists(fd, expect):
    line = fd["loads line"]
    expect.text("no" if line.startswith("QUERY_ERROR") else "yes", "yes",
                "the loads counter exists under ANALYZE")


def test_fold_deferral_the_predicate_has_survivors(fd, expect):
    s = fd["surv"]
    expect.text("yes" if isinstance(s, int) and s > 0 else f"no ({s})", "yes",
                "premise: the predicate has survivors (the identity is not 0 == 0)")


def test_fold_deferral_loads_equal_survivors_times_payload(fd, expect):
    s = fd["surv"]
    want = s * NP if isinstance(s, int) else s
    expect.num(_trailing_int(fd["loads line"]), want,
               "deferred: loads == survivors x payload width (the identity)")


def test_fold_deferral_explain_group_total_matches_stats(fd, expect):
    m = _of(fd["defer line"])
    got = int(m.group(2)) if m else query_error(f"no 'X of Y' in [{fd['defer line']}]")
    expect.num(got, fd["ngroups"], "EXPLAIN group total == stats() group count")


def test_fold_deferral_the_fixture_spans_several_groups(fd, expect):
    m = _of(fd["defer line"])
    expect.text("yes" if m and int(m.group(2)) >= 3 else f"no ({fd['defer line']})", "yes",
                "the fixture spans several groups")


def test_fold_deferral_every_group_deferred_at_one_percent(fd, expect):
    m = _of(fd["defer line"])
    got = f"{m.group(1)} of {m.group(2)}" if m else query_error(f"no 'X of Y' in [{fd['defer line']}]")
    want = f"{m.group(2)} of {m.group(2)}" if m else "all of them"
    expect.text(got, want, "deferred: every group deferred at 1% survival")


def test_fold_deferral_only_the_first_group_defers_at_high_survival(fd, expect):
    m = _of(fd["defer hi line"])
    got = int(m.group(1)) if m else query_error(f"no 'X of Y' in [{fd['defer hi line']}]")
    expect.num(got, 1, "adaptive: only the optimistic first group deferred at 99% survival")


def test_fold_deferral_eager_groups_load_more_than_survivors(fd, expect):
    loads, s = _trailing_int(fd["loads hi line"]), fd["surv hi"]
    got = (("yes" if loads > s * NP * 99 // 100 else f"no ({loads} loads, {s} survivors)")
           if isinstance(loads, int) and isinstance(s, int) else f"no reading ({loads}, {s})")
    expect.text(got, "yes", "adaptive: eager groups load more than survivors alone (work measured, not assumed)")

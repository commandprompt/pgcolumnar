"""The batch fold must refuse a group whose decode skipped vectors (#512). The pytest twin
of `test/native_fold_skipguard.sh`, every check by the shell's name (#1341).

#452 phase 1b-i taught decode to skip the vectors the zone maps rule out, and taught the
fold to honour the same mask. What this file proves is that the fold is REACHABLE for a
shape that skips vectors and that it accounts for the skipping it performs, on its own
plan (#542): 60,000 rows in one group of 59 1024-row vectors, and a range inside one of
them, so the fold must decode exactly 1 and skip exactly 58. Correctness of the fold over
an undecoded hole is native_vecdecode's, whose poison check needs an assert build.

The two checks that read pgcolumnar's own behaviour (the fold decodes only the vector it
needs; the query reaches the fold) are checks, not premises: a decoder that never skips
reddens the first and a fold that refuses every shape reddens the second (#1339). The
scalar arm is compared with the arithmetic oracle, not with the fold's reading, so two
failed readings cannot agree.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, and a failed query is a `query_error()`.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_fold_skipguard"
ROWS = 60000
GROUP_VECS = 59
Q = "SELECT count(*), sum(v) FROM t WHERE k BETWEEN 30000 AND 30100"
SET_ON = ("SET max_parallel_workers_per_gather = 0",
          "SET pgcolumnar.enable_ungrouped_vector_agg = on")
SET_OFF = ("SET max_parallel_workers_per_gather = 0",
           "SET pgcolumnar.enable_ungrouped_vector_agg = off")
WANT = f"101|{(30000 + 30100) * 101 // 2}"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does: a failed
    step shows up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _rows(dsn, sets, sql):
    """-> the result rows, each as its columns joined by "|" like psql -At, after the
    session settings; or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return _failed(exc)
    return ["|".join("" if c is None else str(c) for c in r) for r in rows]


def _field(plan, label):
    """-> the first integer after `label` in the plan, or a failure that names it."""
    if not isinstance(plan, list):
        return plan
    for line in plan:
        m = re.search(re.escape(label) + r": (\d+)", line)
        if m:
            return m.group(1)
    return query_error(f"no '{label}' line in the fold arm's plan")


@pytest.fixture(scope="module")
def fs(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE t (k int, v int) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 60000, "
                  "chunk_group_row_limit => 1024)")
        _run(dsn, f"INSERT INTO t SELECT g, g FROM generate_series(1, {ROWS}) g")
        _run(dsn, "ANALYZE t")
        r = {}
        plan = _rows(dsn, SET_ON, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {Q}")
        r["decoded"] = _field(plan, "Columnar Vectors Decoded")
        r["skipped"] = _field(plan, "Columnar Vectors Skipped")
        if r["decoded"].isdigit() and r["skipped"].isdigit():
            r["sum"] = str(int(r["decoded"]) + int(r["skipped"]))
        else:
            r["sum"] = query_error(f"decoded [{r['decoded']}] and skipped [{r['skipped']}] are not both numbers")
        if isinstance(plan, list):
            fold = [m.group(0) for ln in plan for m in [re.search(r"Columnar Batch Fold: [a-z]+", ln)] if m]
            r["fold"] = fold[0] if fold else query_error("no 'Columnar Batch Fold' line in the fold arm's plan")
        else:
            r["fold"] = plan
        on = _rows(dsn, SET_ON, Q)
        r["on"] = on[-1] if isinstance(on, list) and on else on if not isinstance(on, list) else query_error("no row from the fold arm")
        off = _rows(dsn, SET_OFF, Q)
        r["off"] = off[-1] if isinstance(off, list) and off else off if not isinstance(off, list) else query_error("no row from the scalar arm")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_fold_arm_decodes_only_the_vector_it_needs(fs, expect):
    expect.text(fs["decoded"], "1", "the fold arm decodes only the vector it needs")


def test_the_fold_arm_reports_the_vectors_it_skipped(fs, expect):
    expect.text(fs["skipped"], "58", "and reports the 58 it skipped, on its own plan (#542)")


def test_decoded_plus_skipped_is_the_groups_vectors(fs, expect):
    expect.text(fs["sum"], str(GROUP_VECS), "so decoded plus skipped is the group's vectors, on the FOLD arm")


def test_the_query_reaches_the_batch_fold(fs, expect):
    expect.text(fs["fold"], "Columnar Batch Fold: yes", "the query reaches the batch fold")


def test_the_fold_answers_correctly_while_decode_skips(fs, expect):
    expect.text(fs["on"], WANT, "the fold answers correctly while decode skips 58 of 59 vectors")


def test_the_scalar_arm_agrees_with_the_oracle(fs, expect):
    expect.text(fs["off"], WANT, "and the scalar arm agrees")

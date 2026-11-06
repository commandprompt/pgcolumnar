"""Deferred decode must not pfree in the caller's memory context (#720). The pytest twin
of `test/native_fetch_sort_context.sh`, every check by the shell's name (#1341).

A columnar index scan returns a deferred slot, decoded when something asks for its
columns. A merge join sorts its input, and tuplesort materialises each slot while its
own memory context is current: on PostgreSQL 17+ a bump context, which raises on any
pfree. The deferred decode used to pfree there twice (the needed-column Bitmapset and
the first fetch's format-version catalog scan). Asserted under the plan the bug needs,
forced as the shell forces it: a Merge Join whose columnar side is a Sort (an Incremental
Sort on 18+) DIRECTLY over an Index Scan using f_id on f. A two-key and a three-key merge condition return the
numbers the generator gives, and two single-key controls stay right. On 15/16 there is
no bump allocator and the checks are a guard that the fix changed nothing.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). A reading that fails is a failed reading, never a value.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_fetch_sort_context"
FORCE = ("SET enable_seqscan=off", "SET enable_bitmapscan=off",
         "SET pgcolumnar.enable_custom_scan=off",
         "SET enable_nestloop=off", "SET enable_hashjoin=off")
TWOKEY = "FROM h JOIN f ON f.id=h.id AND f.v=h.v"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _rows(dsn, sql):
    """-> every row's first column under the forced plan, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in FORCE:
                conn.execute(s)
            return [str(r[0]) for r in conn.execute(sql).fetchall()]
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _forced(dsn, sql):
    """-> the one value `sql` returns under the forced plan, as the shell's jv reads it."""
    rows = _rows(dsn, sql)
    if isinstance(rows, str):
        return rows
    return rows[0] if len(rows) == 1 and rows[0] != "None" else query_error(f"no single value from {sql}")


def _sort_over_f_id(plan):
    """-> yes when a Sort sits directly over the Index Scan using f_id on f, as the shell's
    grep -A3 reads it; the failure when the plan could not be read."""
    if isinstance(plan, str):
        return plan
    lines = plan
    for i, line in enumerate(lines):
        if re.match(r"^\s*->\s+(Incremental )?Sort$", line):
            if any(re.search(r"Index Scan using f_id on f( |$)", l) for l in lines[i + 1:i + 4]):
                return "yes"
    return "no"


@pytest.fixture(scope="module")
def nfsc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "SET pgcolumnar.stripe_row_limit=2000; "
                  "CREATE TABLE f (id int, v text) USING pgcolumnar; "
                  "INSERT INTO f SELECT g, 'v'||g FROM generate_series(1,100000) g; "
                  "CREATE INDEX f_id ON f (id); "
                  "CREATE TABLE h (id int, v text); "
                  "INSERT INTO h SELECT g, 'v'||g FROM generate_series(1,2000) g;")
        plan = _rows(dsn, f"EXPLAIN (COSTS OFF) SELECT count(*) {TWOKEY}")
        yield {
            "merge join": plan if isinstance(plan, str) else
                          ("yes" if any("merge join" in l.lower() for l in plan) else "no"),
            "sort over scan": _sort_over_f_id(plan),
            "two-key count": _forced(dsn, f"SELECT count(*) {TWOKEY}"),
            "two-key sum": _forced(dsn, f"SELECT sum(f.id) {TWOKEY}"),
            "three-key": _forced(dsn, f"SELECT count(*) {TWOKEY} AND f.id<1500"),
            "int key": _forced(dsn, "SELECT count(*) FROM h JOIN f ON f.id=h.id"),
            "text key": _forced(dsn, "SELECT count(*) FROM h JOIN f ON f.v=h.v"),
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_nfsc_the_plan_is_a_merge_join(nfsc, expect):
    expect.text(nfsc["merge join"], "yes", "premise: plan is a merge join")


def test_nfsc_the_columnar_side_is_an_index_scan_under_a_sort(nfsc, expect):
    expect.text(nfsc["sort over scan"], "yes", "premise: the columnar side is an index scan under a sort")


def test_nfsc_the_two_key_merge_join_count_is_right(nfsc, expect):
    expect.text(nfsc["two-key count"], "2000", "two-key merge join count is right (#720)")


def test_nfsc_the_two_key_merge_join_sum_is_right(nfsc, expect):
    expect.text(nfsc["two-key sum"], "2001000", "two-key merge join sum(id) is right (#720)")


def test_nfsc_a_three_key_merge_cond_with_a_filter_is_right(nfsc, expect):
    expect.text(nfsc["three-key"], "1499", "three-key merge cond with a filter (#720)")


def test_nfsc_a_single_int_key_is_still_right(nfsc, expect):
    expect.text(nfsc["int key"], "2000", "control: single int key still right")


def test_nfsc_a_single_text_key_is_still_right(nfsc, expect):
    expect.text(nfsc["text key"], "2000", "control: single text key still right")

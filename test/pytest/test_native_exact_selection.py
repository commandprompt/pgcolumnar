"""Exact selection feeds back into the skip vector (#452 phase 1b-ii). The pytest twin
of `test/native_exact_selection.sh`, every check by the shell's name (#1341).

A predicate that matches no row while lying inside every vector's min/max is ruled out
by no zone map, so phase 1b-ii evaluates the qual column's values and marks a vector
with no matching row as skipped: the payload columns are then never decoded. Asserted
on a 32-vector table where z holds only multiples of ten: for z BETWEEN 51 AND 59 every
vector is ruled out by value, none by zone map, and only the qual column is decoded;
the all-matching control decodes every column of every vector and rules out none; the
equality form is pruned whole by the bloom filter; and selective, partial, on-bound
and all-matching queries return what a heap mirror built from the same generator
returns.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). A reading that fails is a failed reading, never a value.
"""
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_native_exact_selection"
ROWS = 32768
VECTORS = 32            # ROWS / 1024
NCOLS = 6               # z plus five payload columns
GEN = (f"SELECT (g % 100) * 10, g, g + 1, g + 2, g + 3, g + 4 "
       f"FROM generate_series(1, {ROWS}) g")
ZEROQ = "SELECT * FROM n WHERE z BETWEEN 51 AND 59"
ALLQ = "SELECT * FROM n WHERE z >= 0"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else str(row[0])


def _set_hash(dsn, query):
    """-> the shell's pgc_set_hash: md5 of the sorted row texts, EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _explain(dsn, query):
    """-> the EXPLAIN ANALYZE text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                                + query).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join(r[0] for r in rows)


def _counter(plan, label):
    """-> the shell's counter_in: the first number on the line naming `label`, as text,
    or a failed reading when the plan failed or has no such line."""
    if plan.startswith(QUERY_ERROR):
        return plan
    m = re.search(re.escape(label) + r"\D*(\d+)", plan)
    return m.group(1) if m else query_error(f"no '{label}' line in the plan")


def _has(plan, label):
    if plan.startswith(QUERY_ERROR):
        return plan
    return "yes" if label in plan else "no"


@pytest.fixture(scope="module")
def nes(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE h (z int, p1 int, p2 int, p3 int, p4 int, p5 int)")
        _run(dsn, "CREATE TABLE n (z int, p1 int, p2 int, p3 int, p4 int, p5 int) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('n', stripe_row_limit => 65536, chunk_group_row_limit => 1024)")
        _run(dsn, f"INSERT INTO h {GEN}")
        _run(dsn, f"INSERT INTO n {GEN}")
        zero, allq = _explain(dsn, ZEROQ), _explain(dsn, ALLQ)
        skipped = _counter(zero, "Columnar Vectors Skipped")
        byvalue = _counter(zero, "Columnar Vectors Ruled Out by Value")
        if skipped.isdigit() and byvalue.isdigit():
            zonemaps = str(int(skipped) - int(byvalue))
        else:
            zonemaps = f"no reading (skipped [{skipped}], by value [{byvalue}])"
        yield {
            "scan": _has(zero, "Columnar Projected Columns"),
            "groups": _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group "
                                  "WHERE storage_id = pgcolumnar.get_storage_id('n')"),
            "zero rows": _value(dsn, "SELECT count(*) FROM h WHERE z BETWEEN 51 AND 59"),
            "all rows": _value(dsn, "SELECT count(*) FROM n WHERE z >= 0"),
            "zone maps": zonemaps,
            "groups pruned": _counter(zero, "Columnar Chunk Groups Removed by Filter"),
            "reports decodes": _has(allq, "Columnar Vector Decodes"),
            "control decodes": _counter(allq, "Columnar Vector Decodes"),
            "ruled out": byvalue,
            "zero decodes": _counter(zero, "Columnar Vector Decodes"),
            "control ruled out": _counter(allq, "Columnar Vectors Ruled Out by Value"),
            "zero returns": _value(dsn, "SELECT count(*) FROM n WHERE z BETWEEN 51 AND 59"),
            "equality pruned": _counter(_explain(dsn, "SELECT * FROM n WHERE z = 51"),
                                        "Columnar Chunk Groups Removed by Filter"),
            "selective": (_set_hash(dsn, "SELECT * FROM n WHERE z = 50"),
                          _set_hash(dsn, "SELECT * FROM h WHERE z = 50")),
            "partial": (_set_hash(dsn, "SELECT * FROM n WHERE z BETWEEN 100 AND 220"),
                        _set_hash(dsn, "SELECT * FROM h WHERE z BETWEEN 100 AND 220")),
            "on bounds": (_set_hash(dsn, "SELECT * FROM n WHERE z BETWEEN 990 AND 990"),
                          _set_hash(dsn, "SELECT * FROM h WHERE z BETWEEN 990 AND 990")),
            "control": (_value(dsn, "SELECT count(*) || '|' || sum(p1) FROM n WHERE z >= 0"),
                        _value(dsn, "SELECT count(*) || '|' || sum(p1) FROM h WHERE z >= 0")),
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_nes_the_zero_match_plan_is_a_columnar_custom_scan(nes, expect):
    expect.text(nes["scan"], "yes", "the zero-match plan is a columnar custom scan")


def test_nes_one_row_group(nes, expect):
    expect.text(nes["groups"], "1", "one row group, so every count below is one group's")


def test_nes_the_predicate_matches_no_row(nes, expect):
    expect.text(nes["zero rows"], "0", "premise: the predicate really matches no row")


def test_nes_the_control_matches_every_row(nes, expect):
    expect.text(nes["all rows"], str(ROWS), "and the control matches every row")


def test_nes_zone_maps_rule_out_nothing(nes, expect):
    expect.text(nes["zone maps"], "0", "ZONE MAPS RULE OUT NOTHING, which is what makes this 1b-ii")


def test_nes_no_whole_row_group_is_pruned(nes, expect):
    expect.text(nes["groups pruned"], "0", "no whole row group is pruned either")


def test_nes_explain_reports_vector_decodes(nes, expect):
    expect.text(nes["reports decodes"], "yes", "EXPLAIN reports Columnar Vector Decodes")


def test_nes_the_control_decodes_every_column_of_every_vector(nes, expect):
    expect.text(nes["control decodes"], str(VECTORS * NCOLS),
                "the control decodes every column of every vector")


def test_nes_exact_selection_rules_out_every_vector(nes, expect):
    expect.text(nes["ruled out"], str(VECTORS),
                "exact selection rules out every vector, none of them by zone map")


def test_nes_a_zero_matching_predicate_decodes_only_the_qual_column(nes, expect):
    expect.text(nes["zero decodes"], str(VECTORS),
                "a zero-matching predicate decodes the qual column and no payload column")


def test_nes_the_control_rules_out_no_vector_by_value(nes, expect):
    expect.text(nes["control ruled out"], "0", "and the control rules out no vector by value")


def test_nes_the_zero_matching_query_returns_nothing(nes, expect):
    expect.text(nes["zero returns"], "0", "the zero-matching query returns nothing")


def test_nes_the_equality_form_is_pruned_by_bloom(nes, expect):
    expect.text(nes["equality pruned"], "1", "the EQUALITY form is pruned by bloom and reaches no decode")


def test_nes_a_selective_matching_predicate_returns_the_right_rows(nes, expect):
    expect.text(nes["selective"][0], nes["selective"][1],
                "a selective matching predicate still returns the right rows")


def test_nes_a_partially_matching_range_returns_the_right_rows(nes, expect):
    expect.text(nes["partial"][0], nes["partial"][1], "and a partially matching range does too")


def test_nes_a_range_matching_only_on_its_bounds_returns_them(nes, expect):
    expect.text(nes["on bounds"][0], nes["on bounds"][1],
                "a range whose only matches lie on its bounds still returns them")


def test_nes_the_control_returns_every_row(nes, expect):
    expect.text(nes["control"][0], nes["control"][1], "the control returns every row")

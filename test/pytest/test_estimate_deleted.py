"""The planner's row estimate follows deletes on a columnar table. The pytest twin of
`test/estimate_deleted.sh`, every check by the shell's name (#1341).

After deleting 99% of a 100,000-row table and analysing it, EXPLAIN must estimate the
live 1,000 rows, as heap does, rather than the physical occupancy. And with reltuples
left stale -- a delete with no ANALYZE after it, on a table of several row groups --
the estimate must still follow the deletes, while an undeleted table of the same shape
estimates its full size.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). A reading that is not a number is a failed reading, never
zero.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_estimate_deleted"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _plan_rows(dsn, sql):
    """-> the top plan node's rows= estimate, or a failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS ON) {sql}"))
    except psycopg.Error as exc:
        return query_error(_message(exc))
    m = re.search(r"rows=(\d+)", plan)
    return int(m.group(1)) if m else query_error("no rows= in the plan")


def _within(v, target, slack, ok, bad):
    if not isinstance(v, int):
        return query_error(f"no reading: {v}")
    return ok if abs(v - target) <= slack else f"{bad} {v}"


def _at_least(v, floor, ok, bad):
    if not isinstance(v, int):
        return query_error(f"no reading: {v}")
    return ok if v >= floor else f"{bad} {v}"


@pytest.fixture(scope="module")
def ed(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _run(dsn, "CREATE TABLE ed_c (id int, v text) USING pgcolumnar; CREATE TABLE ed_h (id int, v text); "
                  "INSERT INTO ed_c SELECT g, 'x' || g FROM generate_series(1, 100000) g; "
                  "INSERT INTO ed_h SELECT g, 'x' || g FROM generate_series(1, 100000) g;")
        _run(dsn, "ANALYZE ed_c; ANALYZE ed_h;")
        r["before rows"] = _value(dsn, "SELECT count(*) FROM ed_c")
        r["before est"] = _plan_rows(dsn, "SELECT * FROM ed_c")
        _run(dsn, "DELETE FROM ed_c WHERE id > 1000; DELETE FROM ed_h WHERE id > 1000;")
        _run(dsn, "ANALYZE ed_c; ANALYZE ed_h;")
        r["live rows"] = _value(dsn, "SELECT count(*) FROM ed_c")
        r["reltuples"] = _value(dsn, "SELECT reltuples::bigint FROM pg_class WHERE relname = 'ed_c'")
        r["after est"] = _plan_rows(dsn, "SELECT * FROM ed_c")
        r["heap est"] = _plan_rows(dsn, "SELECT * FROM ed_h")

        _run(dsn, "CREATE TABLE ed_m (id int, v text) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('ed_m', stripe_row_limit => 20000); "
                  "INSERT INTO ed_m SELECT g, 'x' || g FROM generate_series(1, 100000) g;")
        _run(dsn, "ANALYZE ed_m;")
        r["groups"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group rg JOIN pgcolumnar.storage s "
                                  "ON s.storage_id = rg.storage_id WHERE s.relation_oid = 'ed_m'::regclass")
        r["stale reltuples"] = _value(dsn, "SELECT reltuples::bigint FROM pg_class WHERE relname = 'ed_m'")
        _run(dsn, "DELETE FROM ed_m WHERE id > 60000;")
        r["stale est"] = _plan_rows(dsn, "SELECT * FROM ed_m")
        r["stale live"] = _value(dsn, "SELECT count(*) FROM ed_m")

        _run(dsn, "CREATE TABLE ed_n (id int, v text) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('ed_n', stripe_row_limit => 20000); "
                  "INSERT INTO ed_n SELECT g, 'x' || g FROM generate_series(1, 100000) g;")
        r["undeleted est"] = _plan_rows(dsn, "SELECT * FROM ed_n")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_columnar_holds_its_rows_before_the_delete(ed, expect):
    expect.text(str(ed["before rows"]), "100000", "premise: columnar holds 100000 rows before delete")


def test_the_estimate_before_the_delete_is_the_full_table(ed, expect):
    expect.text(_at_least(ed["before est"], 90000, "full", "low"), "full",
                "the estimate before delete is the full table")


def test_a_thousand_live_rows_remain(ed, expect):
    expect.text(str(ed["live rows"]), "1000", "premise: 1000 live rows remain")


def test_analyze_recorded_the_live_count_in_reltuples(ed, expect):
    expect.text(_within(ed["reltuples"], 1000, 50, "close", "off"), "close",
                "ANALYZE recorded the live count in reltuples")


def test_the_estimate_after_the_delete_is_the_live_count(ed, expect):
    v = ed["after est"]
    got = ("live" if v == 1000 else f"stale {v}") if isinstance(v, int) else query_error(f"no reading: {v}")
    expect.text(got, "live", "the planner estimate after delete is the live count, not the physical occupancy")


def test_heap_is_near_the_live_count_too(ed, expect):
    expect.text(_within(ed["heap est"], 1000, 50, "close", "off"), "close",
                "control: heap is also near the live count, so the probe reads EXPLAIN")


def test_the_fixture_spans_several_row_groups(ed, expect):
    expect.text(_at_least(ed["groups"], 3, "several", "only"), "several",
                "premise: the fixture spans several row groups, not one")


def test_reltuples_is_full_before_the_delete(ed, expect):
    expect.text(_at_least(ed["stale reltuples"], 90000, "full", "low"), "full",
                "reltuples is 100000 before the delete")


def test_the_estimate_follows_the_deletes_with_a_stale_reltuples(ed, expect):
    expect.text(_within(ed["stale est"], 60000, 5000, "live", "wrong"), "live",
                "the estimate follows the deletes with a stale reltuples and several groups")


def test_the_live_count_really_is_60000(ed, expect):
    expect.text(str(ed["stale live"]), "60000", "control: and the live count really is 60000")


def test_an_undeleted_table_estimates_its_full_size(ed, expect):
    expect.text(_at_least(ed["undeleted est"], 90000, "full", "low"), "full",
                "control: an undeleted table of the same shape estimates its full size")

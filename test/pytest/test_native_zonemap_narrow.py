"""Group skipping reads only the predicate columns' zone maps (#314). The pytest twin of
`test/native_zonemap_narrow.sh`, every check by the shell's name (#1341).

pgcolumnar_native_group_can_match fetches the zone map of each column that carries a
predicate, not the whole group's. A one-predicate scan therefore reads about the same
number of pgcolumnar.zone_map tuples on a 30-column table as on a 2-column one; the
instrument is pg_stat_all_tables.idx_tup_fetch for zone_map, flushed with
pg_stat_force_next_flush, read exactly as the shell reads it (one session per step).

PRUNING IS READ, NOT INFERRED (#1341). The answer to `c1 > 19000` alone could not show
a group was skipped: a scan that read the zone maps but never excluded a group, or never
consulted them, returned the same 1000 rows, and main passed all four checks under
each. The scan's own "Columnar Chunk Groups Removed by Filter" must be 9 of the ten
groups, on both tables.

ON A PRIVATE DATABASE. Every reading opens its own connection and returns a failure as
a `query_error()`, so two failed reads never compare equal and an unread count is never
defaulted to 0.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_zonemap_narrow"
WCOLS = ", ".join(f"c{i} int" for i in range(1, 31))
WVALS = ", ".join("g" for _ in range(1, 31))


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rows(dsn, sql):
    """-> every row's first column as text, or the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            cur = conn.execute(sql)
            return [str(r[0]) for r in cur.fetchall()] if cur.description else []
    except psycopg.Error as exc:
        return _failed(exc)


def _int(dsn, sql):
    """-> the single value as an int, or why there is none."""
    rows = _rows(dsn, sql)
    if isinstance(rows, str):
        return rows
    if len(rows) == 1 and rows[0].isdigit():
        return int(rows[0])
    return query_error(f"not a number from {sql}: {rows}")


def _zm_fetch(dsn, table):
    """-> zone_map tuples fetched by a one-predicate scan of TABLE, as the shell's zm_fetch."""
    for sql in ("SELECT pg_stat_reset()",
                f"SELECT count(*) FROM {table} WHERE c1 >= 0",
                "SELECT pg_stat_force_next_flush()"):
        r = _rows(dsn, sql)
        if isinstance(r, str):
            return r
    return _int(dsn, "SELECT coalesce(idx_tup_fetch, 0) FROM pg_stat_all_tables "
                     "WHERE relname = 'zone_map' AND schemaname = 'pgcolumnar'")


def _removed(dsn, table, pred):
    """-> the scan's "Columnar Chunk Groups Removed by Filter", or why there is none."""
    plan = _rows(dsn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                      f"SELECT * FROM {table} WHERE {pred}")
    if isinstance(plan, str):
        return plan
    m = re.search(r"Columnar Chunk Groups Removed by Filter: (\d+)", "\n".join(plan))
    return int(m.group(1)) if m else query_error(f"no removed-groups line for {table}: {plan[:1]}")


def _scales(wf, nf):
    """-> "yes" when the wide read is within twice the narrow, as the shell's arithmetic."""
    if not (isinstance(wf, int) and isinstance(nf, int)):
        return query_error(f"no reading (wide [{wf}], narrow [{nf}])")
    return "yes" if wf <= nf * 2 else "no"


@pytest.fixture(scope="module")
def zn(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        setup = _rows(dsn, f"""CREATE EXTENSION IF NOT EXISTS pgcolumnar;
            SET pgcolumnar.stripe_row_limit = 2000;
            CREATE TABLE w ({WCOLS}) USING pgcolumnar;
            INSERT INTO w SELECT {WVALS} FROM generate_series(1, 20000) g;
            CREATE TABLE n (c1 int, c2 int) USING pgcolumnar;
            INSERT INTO n SELECT g, g FROM generate_series(1, 20000) g""")
        wf = _zm_fetch(dsn, "w")
        nf = _zm_fetch(dsn, "n")
        yield {
            "setup": setup,
            "scales": _scales(wf, nf),
            "wide": _int(dsn, "SELECT count(*) FROM w WHERE c1 >= 0"),
            "narrow": _int(dsn, "SELECT count(*) FROM n WHERE c1 >= 0"),
            "prunes": _int(dsn, "SELECT count(*) FROM w WHERE c1 > 19000"),
            "removed w": _removed(dsn, "w", "c1 > 19000"),
            "removed n": _removed(dsn, "n", "c1 > 19000"),
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_zone_map_reads_scale_with_predicates_not_table_width(zn, expect):
    expect.text(zn["scales"], "yes",
                "zone_map reads scale with predicates, not table width (wide ~= narrow)")


def test_the_wide_scan_result_is_correct(zn, expect):
    expect.num(zn["wide"], 20000, "wide scan result is correct")


def test_the_narrow_scan_result_is_correct(zn, expect):
    expect.num(zn["narrow"], 20000, "narrow scan result is correct")


def test_a_pruning_predicate_still_prunes_on_the_wide_table(zn, expect):
    expect.num(zn["prunes"], 1000, "a pruning predicate still prunes on the wide table")


def test_the_zone_map_removes_nine_groups_on_the_wide_table(zn, expect):
    expect.num(zn["removed w"], 9,
               "the zone map removes the nine groups c1 > 19000 cannot match, on the wide table")


def test_the_zone_map_removes_nine_groups_on_the_narrow_table(zn, expect):
    expect.num(zn["removed n"], 9, "and on the narrow table")

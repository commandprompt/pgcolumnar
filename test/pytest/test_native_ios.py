"""Native index-only scan (Phase D6c, #507, #1341). The pytest twin of
`test/native_ios.sh`, every check by the shell's name.

After VACUUM marks a native table's all-visible row groups in the visibility map, an
index-only scan over an all-visible range skips the fetch (Heap Fetches: 0) and returns
a heap mirror's rows. VACUUM also records the all-visible pages in pg_class, in pages
and not the VM's synthetic blocks, since core prices an index-only scan by
relallvisible / relpages. A delete clears the VM bit, so the scan falls back to the
fetch there and never returns a deleted row. The custom scan, seqscan and bitmap scans
are off for the database, as the shell sets them.
"""
import re

import psycopg
import pytest

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
RANGE = "SELECT id FROM {} WHERE id BETWEEN 1000 AND 5000"


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


def _hash(c, sql):
    return _one(c, HASH.format(sql))


def _plan(c, sql, analyze=False):
    opts = "ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF" if analyze else "COSTS OFF"
    return "\n".join(r[0] for r in c.execute(f"EXPLAIN ({opts}) {sql}"))


def _heap_fetches(plan):
    m = re.search(r"Heap Fetches: (\d+)", plan)
    return int(m.group(1)) if m else None


def _flag(c, rel, expr):
    return _one(c, f"SELECT {expr} FROM pg_class WHERE relname = '{rel}'")


@pytest.fixture(scope="module")
def ios(pgc_cluster):
    db = "pgc_ios"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
        for s in ("pgcolumnar.enable_index_only_scan = on", "pgcolumnar.enable_custom_scan = off",
                  "enable_seqscan = off", "enable_bitmapscan = off"):
            a.execute(f"ALTER DATABASE {db} SET {s}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            # one row group, so an interior range maps to blocks wholly inside it
            c.execute("CREATE TABLE ioh (id int, v text)")
            c.execute("CREATE TABLE ios (id int, v text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('ios', stripe_row_limit => 16384)")
            c.execute("INSERT INTO ioh SELECT g, 'r'||g FROM generate_series(1, 8000) g")
            c.execute("INSERT INTO ios SELECT g, 'r'||g FROM generate_series(1, 8000) g")
            c.execute("CREATE INDEX ios_id ON ios (id)")
            c.execute("VACUUM ios")
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_an_all_visible_range_needs_no_fetch_and_vacuum_records_it(ios, expect):
    c = ios
    expect.text(_one(c, "SELECT count(*) FROM ios"), "8000", "row count")
    expect.num(_plan(c, RANGE.format("ios")).count("Index Only Scan"), 1, "index-only scan chosen")
    # as text: a plan with no Heap Fetches line is a red under this name, not a harness error
    expect.text(str(_heap_fetches(_plan(c, RANGE.format("ios"), analyze=True))), "0",
                "all-visible interior: zero heap fetches")
    expect.text(_hash(c, RANGE.format("ios")), _hash(c, RANGE.format("ioh")), "index-only results match heap (all-visible)")

    # relallvisible prices the scan; the heap mirror is the oracle, not a constant
    c.execute("VACUUM ioh")
    expect.text(_flag(c, "ioh", "relallvisible > 0"), "True",
                "premise: core records all-visible pages for the heap mirror (#507 oracle)")
    expect.text(_flag(c, "ios", "relallvisible > 0"), "True", "VACUUM records the all-visible pages in pg_class (#507)")
    expect.text(_flag(c, "ios", "relpages > 0 AND reltuples > 0"), "True",
                "premise: the relation has pages and rows to be a fraction of (#507)")
    # a synthetic block count would satisfy "> 0" and exceed relpages
    expect.text(_flag(c, "ios", "relallvisible <= relpages"), "True",
                "the all-visible page count cannot exceed the relation (#507)")
    expect.text(_one(c, "SELECT (SELECT relallvisible::float8 / relpages FROM pg_class WHERE relname = 'ios') "
                        ">= 0.5 * (SELECT relallvisible::float8 / relpages FROM pg_class WHERE relname = 'ioh')"),
                "True", "the all-visible fraction is most of the relation, as the heap's is (#507)")


def test_a_delete_clears_the_bit_and_is_never_returned(ios, expect):
    c = ios
    c.execute("DELETE FROM ios WHERE id BETWEEN 2000 AND 2200")
    c.execute("DELETE FROM ioh WHERE id BETWEEN 2000 AND 2200")
    expect.text(_hash(c, RANGE.format("ios")), _hash(c, RANGE.format("ioh")), "index-only results match heap after delete")
    fetched = _heap_fetches(_plan(c, RANGE.format("ios"), analyze=True)) or 0
    expect.text("yes" if fetched > 0 else "no", "yes", "after delete: some heap fetches occur")

"""A rewrite reads each row group once, not once per row (#196). The pytest twin of
`test/rewrite_group_scan.sh`, every check by the shell's name (#1341).

rewrite_one_group used to fetch every row by number, and each fetch decoded the whole
group whenever the group's decoded form exceeded the fetch cache cap: a group 3.5% over
the cap took longer than two minutes to rewrite. The fixture here crosses the cap (three
columns, one varlena, 200,000 rows with a third deleted). Asserted: compact_rewrite
rewrites it within a generous statement_timeout, keeps every surviving row exactly,
agrees with a heap mirror, still rewrites a group under the cap correctly, and leaves
the rows reachable through an index built before it, through a plan that reads that
index.

WHAT A REWRITE READING IS: "finished" only when compact_rewrite returned a positive
count of rewritten groups, "rewrote nothing", or the error it raised. A timeout was the
one failure counted before, so a rewrite that did nothing at all "finished", and every
check passed with compact_rewrite returning 0 untouched. The fixture is held to its
survivors, since every comparison agrees over an empty table.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_rewrite_group_scan"
ROWS = 200000
LIMIT = "60s"
DIGEST = "SELECT count(*) || '/' || md5(string_agg(id || ':' || k || ':' || v, ',' ORDER BY id)) FROM rgs"
SMALL = "SELECT count(*) || '/' || coalesce(sum(id), 0) FROM rgs_s"
PROBE = ROWS // 2 + 1


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, *sqls):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                for sql in sqls:
                    conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0]}")

    def q(self, sql, settings=()):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                for s in settings:
                    conn.execute(s)
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return "" if row is None or row[0] is None else str(row[0])

    def plan(self, sql, settings=()):
        """-> the whole EXPLAIN (COSTS OFF) text of sql, or its error as a value."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                for s in settings:
                    conn.execute(s)
                rows = conn.execute(f"EXPLAIN (COSTS OFF) {sql}").fetchall()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return "\n".join(r[0] for r in rows)

    def rewrite(self, table):
        """-> "finished", "rewrote nothing", or "no reading: <error>"."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(f"SET statement_timeout = '{LIMIT}'")
                n = conn.execute(f"SELECT pgcolumnar.compact_rewrite('{table}', 0.2)").fetchone()[0]
        except psycopg.Error as exc:
            return f"no reading: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
        return "finished" if n and n > 0 else "rewrote nothing"


@pytest.fixture(scope="module")
def rg(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        d.run("CREATE TABLE rgs (id int, k int, v text) USING pgcolumnar",
              f"INSERT INTO rgs SELECT g, g % 100, repeat('m', 80) || g FROM generate_series(1, {ROWS}) g",
              "DELETE FROM rgs WHERE id % 3 = 0",
              "CREATE TABLE rgs_h (id int, k int, v text)",
              f"INSERT INTO rgs_h SELECT g, g % 100, repeat('m', 80) || g FROM generate_series(1, {ROWS}) g "
              "WHERE g % 3 <> 0")
        r["survivors"] = d.q("SELECT count(*) FROM rgs")
        # Built before the rewrite, so the rewrite must maintain it: built afterwards,
        # it was fresh over the rewritten table and a rewrite inserting no index
        # entries passed every check.
        d.run("CREATE INDEX rgs_id ON rgs (id)")
        r["before"] = d.q(DIGEST)
        r["big"] = d.rewrite("rgs")
        r["after"] = d.q(DIGEST)
        r["mirror"] = d.q("SELECT count(*) FROM rgs r FULL JOIN rgs_h h USING (id) "
                          "WHERE r.v IS DISTINCT FROM h.v OR r.k IS DISTINCT FROM h.k")
        d.run("CREATE TABLE rgs_s (id int, k int, v text) USING pgcolumnar",
              "INSERT INTO rgs_s SELECT g, g % 100, repeat('m', 80) || g FROM generate_series(1, 20000) g",
              "DELETE FROM rgs_s WHERE id % 3 = 0")
        small_before = d.q(SMALL)
        small = d.rewrite("rgs_s")
        # Pairs, not one string: a failed read stays a failed-query value the comparison
        # refuses, instead of text inside a string that two failures could make agree.
        r["small"], r["small want"] = (small, d.q(SMALL)), ("finished", small_before)
        lookup = ["SET enable_seqscan = off", "SET pgcolumnar.enable_custom_scan = off"]
        r["index plan"] = d.plan(f"SELECT v FROM rgs WHERE id = {PROBE}", lookup)
        r["index"] = d.q(f"SELECT v FROM rgs WHERE id = {PROBE}", lookup)
        r["index want"] = d.q(f"SELECT v FROM rgs_h WHERE id = {PROBE}")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _num(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def test_the_fixture_holds_its_survivors(rg, expect):
    expect.num(_num(rg["survivors"]), ROWS - ROWS // 3,
               "premise: the fixture holds the rows that survive the delete")


def test_a_rewrite_over_the_cache_cap_finishes(rg, expect):
    expect.text(rg["big"], "finished", "a rewrite of a group over the cache cap finishes")


def test_the_rewrite_preserved_every_surviving_row(rg, expect):
    expect.text(rg["after"], rg["before"], "the rewrite preserved every surviving row")


def test_the_rewrite_agrees_with_a_heap_mirror(rg, expect):
    expect.text(rg["mirror"], "0", "and it agrees with a heap mirror")


def test_a_group_under_the_cap_still_rewrites_correctly(rg, expect):
    expect.text(rg["small"], rg["small want"], "a group under the cap still rewrites correctly")


def test_the_index_lookup_reads_the_index(rg, expect):
    expect.contains(rg["index plan"], "rgs_id", "the index lookup's plan reads rgs_id")


def test_an_index_lookup_finds_a_rewritten_row(rg, expect):
    expect.text(rg["index"], rg["index want"], "an index lookup finds a rewritten row")

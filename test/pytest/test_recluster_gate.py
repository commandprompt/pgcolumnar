"""pgcolumnar.recluster() skips its rewrite when the table is already this key's run with
nothing appended (#415). The pytest twin of `test/recluster_gate.sh`, every check by the
shell's name (#1341).

recluster returns the number of row groups it rewrote. Asserted: the first recluster
does work and records the key; repeating it on an unchanged table rewrites nothing and
leaves the layout byte-identical; an append makes it work again and fold the tail into
the run; a new key re-clusters and is recorded; and the no-op calls left the row set
exactly as the generator wrote it.

THE ROW-SET CHECK reads a heap mirror built from the generator, not one copied from the
table under test: a copy agrees with whatever the reclusters did, and with a row
corrupted before the copy the shell's old form passed. A layout comparison needs both
readings, since two failed reads agreed.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_recluster_gate"
CG = 1000
ROWSET = "SELECT count(*)||'/'||sum(a)||'/'||sum(b)||'/'||md5(string_agg(pad,',' ORDER BY b)) FROM {t}"
LAYOUT = ("SELECT md5(string_agg(stripeid::text||':'||fileoffset::text||':'||rowcount::text, ',' "
          "ORDER BY stripeid)) FROM pgcolumnar.stats('rg_t')")


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0]}")

    def q(self, sql):
        """-> the first column as psql -At prints it, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        if row is None or row[0] is None:
            return ""
        return ("t" if row[0] else "f") if isinstance(row[0], bool) else str(row[0])

    def recluster(self, key):
        v = self.q(f"SELECT pgcolumnar.recluster('rg_t','{key}')")
        return int(v) if v.isdigit() else v


def _work(n):
    return ("yes" if n > 0 else "no") if isinstance(n, int) else f"no ({n})"


@pytest.fixture(scope="module")
def rc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        d.run("CREATE TABLE rg_t (a int, b int, pad text) USING pgcolumnar")
        d.run(f"SELECT pgcolumnar.set_options('rg_t', chunk_group_row_limit => {CG})")
        d.run("INSERT INTO rg_t SELECT (g*2654435761)::bigint % 1000, g, md5(g::text) FROM generate_series(1,20000) g")
        r["first"] = d.recluster("a")
        r["key"] = d.q("SELECT sort_key::text FROM pgcolumnar.sort_status('rg_t')")
        r["appended"] = d.q("SELECT appended_groups FROM pgcolumnar.sort_status('rg_t')")
        before = d.q(LAYOUT)
        r["second"] = d.recluster("a")
        after = d.q(LAYOUT)
        ok = all(v and not v.startswith(QUERY_ERROR) for v in (before, after))
        r["layout"] = ("same" if before == after else "rewritten") if ok \
            else f"no reading: [{before}] -> [{after}]"
        r["third"] = d.recluster("a")
        d.run("INSERT INTO rg_t SELECT (g*2654435761)::bigint % 1000, g, md5(g::text) FROM generate_series(1,5000) g")
        r["tail"] = d.q("SELECT appended_groups > 0 FROM pgcolumnar.sort_status('rg_t')")
        r["after append"] = d.recluster("a")
        r["folded"] = d.q("SELECT appended_groups FROM pgcolumnar.sort_status('rg_t')")
        r["new key"] = d.recluster("b")
        r["key b"] = d.q("SELECT sort_key::text FROM pgcolumnar.sort_status('rg_t')")
        r["repeat b"] = d.recluster("b")
        # the mirror comes from the generator: g = 1..20000, then 1..5000 again
        d.run("CREATE TABLE rg_h AS "
              "SELECT ((g*2654435761)::bigint % 1000)::int AS a, g AS b, md5(g::text) AS pad "
              "FROM generate_series(1,20000) g UNION ALL "
              "SELECT ((g*2654435761)::bigint % 1000)::int, g, md5(g::text) FROM generate_series(1,5000) g")
        r["rowset"], r["mirror"] = d.q(ROWSET.format(t="rg_t")), d.q(ROWSET.format(t="rg_h"))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# Not premises (#1339): the next three read recluster's own work, its recorded key (#415)
# and its recorded extent (#311), and a one-line change to recluster reddens each.
def test_the_first_gate_recluster_does_real_work(rc, expect):
    expect.text(_work(rc["first"]), "yes", "the first recluster does real work (>0 groups)")


def test_sort_status_reports_the_clustering_key(rc, expect):
    expect.text(rc["key"], "{a}", "sort_status now reports the clustering key (was NULL before #415)")


def test_the_recluster_run_covers_the_whole_relation(rc, expect):
    expect.text(rc["appended"], "0", "the run covers the whole relation (0 appended groups)")


def test_a_repeat_on_an_unchanged_table_reclusters_nothing(rc, expect):
    expect.num(rc["second"], 0, "recluster by the same key on an unchanged table reclusters 0 groups")


def test_the_layout_is_byte_identical(rc, expect):
    expect.text(rc["layout"], "same", "and it leaves the physical layout byte-identical (group offsets unmoved)")


def test_a_third_identical_recluster_is_a_no_op(rc, expect):
    expect.num(rc["third"], 0, "a third identical recluster is also a no-op")


def test_the_insert_appended_unsorted_groups(rc, expect):
    expect.text(rc["tail"], "t", "premise: the insert appended unsorted groups")


def test_a_recluster_after_appends_does_work(rc, expect):
    expect.text(_work(rc["after append"]), "yes", "recluster after appends does real work again (>0)")


def test_recluster_folds_the_tail_back_into_the_run(rc, expect):
    expect.text(rc["folded"], "0", "and it folds the new data back into the run (0 appended after)")


def test_a_recluster_by_a_new_key_does_work(rc, expect):
    expect.text(_work(rc["new key"]), "yes", "recluster by a NEW key is NOT a no-op -- it re-clusters (>0)")


def test_sort_status_reports_the_new_key(rc, expect):
    expect.text(rc["key b"], "{b}", "and sort_status now reports the new key")


def test_a_repeat_by_the_new_key_is_a_no_op(rc, expect):
    expect.num(rc["repeat b"], 0, "a repeat recluster by the new key is a no-op")


def test_the_no_op_reclusters_preserved_the_row_set(rc, expect):
    expect.text(rc["rowset"], rc["mirror"], "no-op recluster preserved the exact row set (columnar == heap mirror)")

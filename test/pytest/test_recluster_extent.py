"""An online recluster records only the extent it actually rewrote (#311, #342). The pytest
twin of `test/recluster_extent.sh`, every check by the shell's name (#1341).

The online recluster runs while other sessions write. Its run must cover the groups it
wrote and nothing else: not rows inserted while it ran, and not a group whose id another
writer drew below the rewrite's. Asserted, with the races played out in threads as the
shell plays them in background jobs: the concurrent insert commits during the rewrite,
every row survives, the run claims some of the base and no more, the concurrent rows are
reported as appended, a group numbered below the run is not counted as ordered, a full
recluster with and without a projection claims every row (the projection read back from
the catalog, so a failed add_projection cannot turn the projection arm into a second
arm without one), and an uncontended rewrite claims the whole relation.

A run that recorded nothing claims nothing, so "does not claim rows written during the
rewrite" needs at least one sorted row; 0 used to pass it.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_recluster_extent"
BASE = 1500000


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql.encode())  # simple protocol, as psql -c: several statements
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return ""

    def q(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        if row is None or row[0] is None:
            return ""
        return ("true" if row[0] else "false") if isinstance(row[0], bool) else str(row[0])

    def n(self, sql):
        v = self.q(sql)
        return int(v) if v.lstrip("-").isdigit() else v

    def ss(self, col, table="t"):
        return self.n(f"SELECT {col} FROM pgcolumnar.sort_status('{table}')")


def _prj(d, arm, projection=False):
    """-> (sorted, appended, projections): projections is how many projections named
    p_ARM the catalog holds after add_projection, as a value (a failed read included),
    or None when none was asked for."""
    d.run(f"DROP TABLE IF EXISTS pr_{arm}")
    d.run(f"CREATE TABLE pr_{arm} (id int, k int, v text) USING pgcolumnar")
    d.run(f"SELECT pgcolumnar.set_options('pr_{arm}', stripe_row_limit => 20000)")
    d.run(f"INSERT INTO pr_{arm} SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g FROM generate_series(1, 200000) g")
    made = None
    if projection:
        d.run(f"SELECT pgcolumnar.add_projection('pr_{arm}', 'p_{arm}', ARRAY['k','id'], ARRAY['k'])")
        # read back from the catalog: add_projection's own result was discarded
        made = d.n(f"SELECT count(*) FROM pgcolumnar.projection WHERE name = 'p_{arm}' AND projection_id > 0")
    d.run(f"SELECT pgcolumnar.recluster('pr_{arm}', 'id', 'k')")
    return d.ss("sorted_rows", f"pr_{arm}"), d.ss("appended_rows", f"pr_{arm}"), made


@pytest.fixture(scope="module")
def re_(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        d.run("CREATE TABLE t (id int, k int, v text) USING pgcolumnar")
        d.run("SELECT pgcolumnar.set_options('t', stripe_row_limit => 20000, chunk_group_row_limit => 2048)")
        d.run(f"INSERT INTO t SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g FROM generate_series(1, {BASE}) g")
        r["loaded"] = d.n("SELECT count(*) FROM t")

        # the rewrite in one session, an insert in another two seconds in
        out = {}

        def recluster():
            out["retired"] = d.n("SELECT pgcolumnar.recluster('t', 'id', 'k')")
        th = threading.Thread(target=recluster)
        th.start()
        time.sleep(2)
        # asked of the server, before the insert: the rewrite's end comes moments after
        # the insert commits, so nothing read afterwards can order the two
        active = d.n("SELECT count(*) FROM pg_stat_activity WHERE state = 'active' "
                     "AND query LIKE 'SELECT pgcolumnar.recluster(''t''%'")
        d.run(f"INSERT INTO t SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g "
              f"FROM generate_series({BASE + 1}, {BASE + 50000}) g")
        th.join()
        r["during"] = {1: "yes", 0: "no (the rewrite was not running when the insert began)"}.get(
            active, f"no reading ({active})")
        r["retired"] = out["retired"]
        r["rows"] = d.n("SELECT count(*) FROM t")
        r["sorted"], r["appended"] = d.ss("sorted_rows"), d.ss("appended_rows")
        r["cover"] = d.q("SELECT (sorted_rows + appended_rows = (SELECT sum(rowcount) FROM pgcolumnar.stats('t')))::text "
                         "FROM pgcolumnar.sort_status('t')")

        # a writer draws its group id before the rewrite and commits after it
        d.run("DROP TABLE IF EXISTS d")
        d.run("CREATE TABLE d (id int, k int, v text) USING pgcolumnar")
        d.run("SELECT pgcolumnar.set_options('d', stripe_row_limit => 20000, chunk_group_row_limit => 2048)")
        d.run("INSERT INTO d SELECT g, ((g::bigint * 7919) % 100000)::int, 'v' || g FROM generate_series(1, 100000) g")
        hold = threading.Thread(target=d.run, args=(
            "BEGIN; INSERT INTO d SELECT g, 1, 'late' || g FROM generate_series(900001, 905000) g; "
            "SELECT pg_sleep(12); COMMIT;",))
        hold.start()
        time.sleep(3)
        d.run("SELECT pgcolumnar.recluster('d', 'id', 'k')")
        hold.join()
        r["d sorted"], r["d appended"] = d.ss("sorted_rows", "d"), d.ss("appended_rows", "d")
        r["d cover"] = d.q("SELECT (sorted_rows + appended_rows = (SELECT sum(rowcount) FROM pgcolumnar.stats('d')))::text "
                           "FROM pgcolumnar.sort_status('d')")

        r["noproj"] = _prj(d, "noproj")
        r["withproj"] = _prj(d, "withproj", projection=True)

        d.run("SELECT pgcolumnar.recluster('t', 'id', 'k')")
        r["final appended"], r["final sorted"] = d.ss("appended_rows"), d.ss("sorted_rows")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_table_loaded(re_, expect):
    expect.num(re_["loaded"], BASE, "the table loaded")


def test_the_insert_ran_during_the_rewrite(re_, expect):
    expect.text(re_["during"], "yes", "the insert ran while the rewrite was still running")


def test_the_rewrite_reported_groups_reclustered(re_, expect):
    n = re_["retired"]
    expect.text(("yes" if n > 0 else "no") if isinstance(n, int) else f"no ({n})", "yes",
                "the rewrite reported groups reclustered")


def test_the_concurrent_insert_was_not_in_the_work_set(re_, expect):
    expect.num(re_["retired"], BASE // 20000, "premise: the concurrent insert was not in the rewrite's work set")


def test_every_row_is_present_afterwards(re_, expect):
    expect.num(re_["rows"], BASE + 50000, "every row is present afterwards")


def test_the_run_does_not_claim_rows_written_during_the_rewrite(re_, expect):
    s = re_["sorted"]
    if not isinstance(s, int):
        got = f"no reading ({s})"
    elif s < 1:
        got = "no (the run claims no rows at all)"
    else:
        got = "yes" if s <= BASE else f"no ({s} sorted, more than the {BASE} the rewrite read)"
    expect.text(got, "yes", "the run does not claim rows written during the rewrite")


def test_the_counts_still_cover_every_stored_row(re_, expect):
    expect.text(re_["cover"], "true", "the counts still cover every stored row")


def test_the_concurrent_rows_are_reported_as_appended(re_, expect):
    a = re_["appended"]
    expect.text(("yes" if a > 0 else "no") if isinstance(a, int) else f"no ({a})", "yes",
                "the concurrent rows are reported as appended")


def test_a_group_numbered_below_the_run_is_not_ordered(re_, expect):
    expect.num(re_["d sorted"], 100000, "a group numbered below the run is not counted as ordered")


def test_the_rows_below_the_run_are_appended(re_, expect):
    expect.num(re_["d appended"], 5000, "the rows written below the run are reported as appended")


def test_the_id_drawn_below_counts_cover_every_row(re_, expect):
    expect.text(re_["d cover"], "true", "the id-drawn-below counts still cover every stored row")


def test_noproj_a_full_recluster_claims_every_row(re_, expect):
    expect.num(re_["noproj"][0], 200000, "noproj: a full recluster claims every row")


def test_noproj_a_full_recluster_leaves_nothing_appended(re_, expect):
    expect.num(re_["noproj"][1], 0, "noproj: a full recluster leaves nothing appended")


def test_withproj_add_projection_created_the_projection(re_, expect):
    # without it, withproj is a second noproj arm and passes whatever #345's merge does
    expect.num(re_["withproj"][2], 1, "withproj: add_projection created the projection")


def test_withproj_a_full_recluster_claims_every_row(re_, expect):
    expect.num(re_["withproj"][0], 200000, "withproj: a full recluster claims every row")


def test_withproj_a_full_recluster_leaves_nothing_appended(re_, expect):
    expect.num(re_["withproj"][1], 0, "withproj: a full recluster leaves nothing appended")


def test_an_uncontended_rewrite_claims_the_whole_relation(re_, expect):
    expect.num(re_["final appended"], 0, "an uncontended rewrite claims the whole relation")


def test_an_uncontended_rewrite_counts_every_row_as_sorted(re_, expect):
    expect.num(re_["final sorted"], BASE + 50000, "an uncontended rewrite counts every row as sorted")

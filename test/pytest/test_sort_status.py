"""pgcolumnar.sort_status() reports how much of an ordered table is still in its ordered
run (#301). The pytest twin of `test/sort_status.sh`, every check by the shell's name
(#1341).

sort_status splits a table's row groups into the sorted run, recorded by the last
vacuum_sorted, cluster or recluster, and the groups appended since. Asserted through one
table's life: unsorted, freshly sorted, with an appended tail, re-sorted, rewritten
unsorted by vacuum(), Z-ordered by cluster() and recluster(), with groups retired by
compact_rewrite, and after a delete; plus an empty table, the declared key, and the
storage rows that a rewrite and a DROP leave behind.

WHAT A READING IS. Each value is the first column as text, or a query_error(), so two
failed reads never agree. The three checks that compare one sort_status reading with
another (every group sorted, the extent unmoved after an insert and after a delete) are
numeric, so with sort_status raising on a sorted table they fail rather than compare ""
with "". The rewrite into new storage needs both storage ids.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_sort_status"


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

    def n(self, sql):
        v = self.q(sql)
        return int(v) if v.lstrip("-").isdigit() else v

    def ss(self, col, table="t"):
        return self.q(f"SELECT {col} FROM pgcolumnar.sort_status('{table}')")

    def ssn(self, col):
        v = self.ss(col)
        return int(v) if v.lstrip("-").isdigit() else v

    def sid(self):
        return self.q("SELECT pgcolumnar.get_storage_id('t')")


def _cmp(a, b, op, yes, no):
    if not isinstance(a, int) or not isinstance(b, int):
        return f"{no} ({a}, {b})"
    return yes if op(a, b) else f"{no} ({a}, {b})"


@pytest.fixture(scope="module")
def st(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        d.run("CREATE TABLE t (id int, k int, v text) USING pgcolumnar")
        d.run("SELECT pgcolumnar.set_options('t', stripe_row_limit => 1000, chunk_group_row_limit => 500)")
        d.run("INSERT INTO t SELECT g, (g * 7919) % 5000, 'v' || g FROM generate_series(1, 5000) g")
        for k in ("total_groups", "sorted_groups", "appended_groups", "sorted_rows", "appended_rows"):
            r[f"unsorted {k}"] = d.ss(k)
        r["no key"] = d.ss("sort_key IS NULL")

        sid_before = d.sid()
        d.run("SELECT pgcolumnar.vacuum_sorted('t', 'k')")
        sid_after = d.sid()
        ok = all(v and not v.startswith(QUERY_ERROR) for v in (sid_before, sid_after))
        r["new storage"] = ("yes" if sid_before != sid_after else "no") if ok \
            else f"no reading: storage [{sid_before}] -> [{sid_after}]"
        r["rows survived"] = d.q("SELECT count(*) FROM t")
        r["ordered"] = d.q("SELECT count(*) FROM (SELECT k, lag(k) OVER () AS p FROM t) s WHERE p > k")
        total_sorted = d.ssn("total_groups")
        r["fresh sorted groups"], r["total sorted"] = d.ssn("sorted_groups"), total_sorted
        for k in ("appended_groups", "sorted_rows", "appended_rows"):
            r[f"fresh {k}"] = d.ss(k)

        d.run("INSERT INTO t SELECT g, (g * 7919) % 5000, 'v' || g FROM generate_series(5001, 7500) g")
        r["extent after insert"] = d.ssn("sorted_groups")
        for k in ("appended_groups", "appended_rows", "sorted_rows"):
            r[f"tail {k}"] = d.ss(k)
        r["totals"] = d.q("SELECT (sorted_groups + appended_groups = total_groups)::text FROM pgcolumnar.sort_status('t')")
        r["no longer ordered"] = d.q("SELECT (count(*) > 0)::text FROM (SELECT k, lag(k) OVER () AS p FROM t) s WHERE p > k")

        d.run("SELECT pgcolumnar.vacuum_sorted('t', 'k')")
        for k in ("appended_groups", "sorted_rows", "appended_rows"):
            r[f"resort {k}"] = d.ss(k)
        d.run("SELECT pgcolumnar.vacuum('t')")
        for k in ("sorted_groups", "sorted_rows", "appended_rows"):
            r[f"vacuum {k}"] = d.ss(k)
        d.run("SELECT pgcolumnar.cluster('t', 'id', 'k')")
        r["cluster appended_groups"], r["cluster sorted_rows"] = d.ss("appended_groups"), d.ss("sorted_rows")
        d.run("SELECT pgcolumnar.vacuum('t')")
        r["vacuum again sorted_groups"] = d.ss("sorted_groups")
        d.run("SELECT pgcolumnar.recluster('t', 'id', 'k')")
        r["recluster sorted_groups"] = d.ssn("sorted_groups")
        for k in ("appended_groups", "sorted_rows", "appended_rows"):
            r[f"recluster {k}"] = d.ss(k)
        d.run("INSERT INTO t SELECT g, (g * 7919) % 5000, 'v' || g FROM generate_series(7501, 8500) g")
        r["post-recluster appended_rows"], r["post-recluster sorted_rows"] = d.ss("appended_rows"), d.ss("sorted_rows")

        d.run("SELECT pgcolumnar.vacuum_sorted('t', 'k')")
        run_groups = d.ssn("sorted_groups")
        d.run("DELETE FROM t WHERE k < 400")
        rewritten = d.n("SELECT pgcolumnar.compact_rewrite('t', 0.1, 0)")
        r["rewritten"] = rewritten
        r["run lost"] = _cmp(d.ssn("sorted_groups"), run_groups, lambda a, b: a < b, "yes", "no")
        r["replacement appended"] = _cmp(d.ssn("appended_groups"), rewritten, lambda a, b: a >= b, "yes", "no")
        r["totals still"] = d.q("SELECT (sorted_groups + appended_groups = total_groups)::text "
                                "FROM pgcolumnar.sort_status('t')")
        d.run("SELECT pgcolumnar.set_options('t', sort_by => ARRAY['k'])")
        r["declared key"] = d.ss("array_to_string(sort_key, ',')")

        d.run("CREATE TABLE e (id int) USING pgcolumnar")
        for k in ("total_groups", "sorted_groups", "appended_rows"):
            r[f"empty {k}"] = d.ss(k, "e")

        d.run("SELECT pgcolumnar.vacuum_sorted('t', 'k')")
        before_del = d.ssn("sorted_groups")
        before_rows = d.n("SELECT count(*) FROM t")
        doomed = d.n("SELECT count(*) FROM t WHERE id % 10 = 0")
        d.run("DELETE FROM t WHERE id % 10 = 0")
        r["doomed"] = doomed
        r["extent after delete"], r["before delete"] = d.ssn("sorted_groups"), before_del
        r["delete appended_groups"] = d.ss("appended_groups")
        r["live after delete"] = d.n("SELECT count(*) FROM t")
        r["live expected"] = before_rows - doomed if isinstance(before_rows, int) and isinstance(doomed, int) \
            else query_error(f"no counts: {before_rows} / {doomed}")
        r["mark covers"] = d.q("SELECT (st.sorted_through = (SELECT max(rg.group_number) FROM pgcolumnar.row_group rg "
                               "WHERE rg.storage_id = st.storage_id))::text FROM pgcolumnar.storage st "
                               "WHERE st.storage_id = pgcolumnar.get_storage_id('t')")
        r["abandoned gone"] = d.q(f"SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid_before or 'NULL::bigint'}") \
            if ok else query_error("no storage id before the sort")
        sid_now = d.sid()
        d.run("DROP TABLE t")
        r["dropped gone"] = d.q(f"SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid_now}") \
            if sid_now and not sid_now.startswith(QUERY_ERROR) else query_error("no storage id before the drop")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- an unsorted table ---------------------------------------------------------

def test_groups_written(st, expect):
    expect.text(st["unsorted total_groups"], "5", "groups written")


def test_an_unsorted_table_reports_no_sorted_groups(st, expect):
    expect.text(st["unsorted sorted_groups"], "0", "unsorted table reports no sorted groups")


def test_an_unsorted_table_reports_every_group_appended(st, expect):
    expect.text(st["unsorted appended_groups"], "5", "unsorted table reports every group appended")


def test_an_unsorted_table_reports_no_sorted_rows(st, expect):
    expect.text(st["unsorted sorted_rows"], "0", "unsorted table reports no sorted rows")


def test_an_unsorted_table_reports_every_row_appended(st, expect):
    expect.text(st["unsorted appended_rows"], "5000", "unsorted table reports every row appended")


def test_no_declared_sort_key_yet(st, expect):
    expect.text(st["no key"], "t", "no declared sort key yet")


# ---- a fresh sort --------------------------------------------------------------

def test_the_sort_rewrote_into_a_new_storage(st, expect):
    expect.text(st["new storage"], "yes", "the sort rewrote into a new storage")


def test_rows_survived_the_sort(st, expect):
    expect.text(st["rows survived"], "5000", "rows survived the sort")


def test_the_table_is_physically_ordered_on_k(st, expect):
    expect.text(st["ordered"], "0", "the table is physically ordered on k")


def test_a_fresh_sort_reports_every_group_sorted(st, expect):
    expect.num(st["fresh sorted groups"], st["total sorted"], "a fresh sort reports every group sorted")


def test_a_fresh_sort_reports_no_decay(st, expect):
    expect.text(st["fresh appended_groups"], "0", "a fresh sort reports no decay")


def test_a_fresh_sort_reports_every_row_sorted(st, expect):
    expect.text(st["fresh sorted_rows"], "5000", "a fresh sort reports every row sorted")


def test_a_fresh_sort_reports_no_appended_rows(st, expect):
    expect.text(st["fresh appended_rows"], "0", "a fresh sort reports no appended rows")


# ---- an appended tail ------------------------------------------------------------

def test_the_sorted_extent_did_not_move(st, expect):
    expect.num(st["extent after insert"], st["total sorted"], "the sorted extent did not move")


def test_appended_groups_appeared(st, expect):
    expect.text(st["tail appended_groups"], "3", "appended groups appeared")


def test_appended_rows_are_counted(st, expect):
    expect.text(st["tail appended_rows"], "2500", "appended rows are counted")


def test_sorted_rows_are_unchanged(st, expect):
    expect.text(st["tail sorted_rows"], "5000", "sorted rows are unchanged")


def test_the_totals_add_up(st, expect):
    expect.text(st["totals"], "true", "the totals add up")


def test_the_table_is_no_longer_fully_ordered(st, expect):
    expect.text(st["no longer ordered"], "true", "the table is no longer fully ordered")


# ---- re-sorted, rewritten unsorted, clustered, reclustered ----------------------------

def test_re_sorting_clears_the_appended_tail(st, expect):
    expect.text(st["resort appended_groups"], "0", "re-sorting clears the appended tail")


def test_re_sorting_counts_every_row_as_sorted(st, expect):
    expect.text(st["resort sorted_rows"], "7500", "re-sorting counts every row as sorted")


def test_re_sorting_reports_no_appended_rows(st, expect):
    expect.text(st["resort appended_rows"], "0", "re-sorting reports no appended rows")


def test_an_unsorted_rewrite_reports_no_sorted_groups(st, expect):
    expect.text(st["vacuum sorted_groups"], "0", "an unsorted rewrite reports no sorted groups")


def test_an_unsorted_rewrite_reports_no_sorted_rows(st, expect):
    expect.text(st["vacuum sorted_rows"], "0", "an unsorted rewrite reports no sorted rows")


def test_an_unsorted_rewrite_reports_every_row_appended(st, expect):
    expect.text(st["vacuum appended_rows"], "7500", "an unsorted rewrite reports every row appended")


def test_cluster_reports_no_decay(st, expect):
    expect.text(st["cluster appended_groups"], "0", "cluster reports no decay")


def test_cluster_counts_every_row_as_sorted(st, expect):
    expect.text(st["cluster sorted_rows"], "7500", "cluster counts every row as sorted")


def test_the_unsorted_rewrite_cleared_the_mark_again(st, expect):
    expect.text(st["vacuum again sorted_groups"], "0", "the unsorted rewrite cleared the mark again")


def test_recluster_records_its_extent(st, expect):
    v = st["recluster sorted_groups"]
    expect.text(("yes" if v > 0 else f"no ({v})") if isinstance(v, int) else f"no ({v})", "yes",
                "recluster records its extent")


def test_recluster_reports_no_decay(st, expect):
    expect.text(st["recluster appended_groups"], "0", "recluster reports no decay")


def test_recluster_counts_every_row_as_sorted(st, expect):
    expect.text(st["recluster sorted_rows"], "7500", "recluster counts every row as sorted")


def test_recluster_reports_no_appended_rows(st, expect):
    expect.text(st["recluster appended_rows"], "0", "recluster reports no appended rows")


def test_rows_inserted_after_recluster_are_appended(st, expect):
    expect.text(st["post-recluster appended_rows"], "1000", "rows inserted after recluster are appended")


def test_the_reclustered_rows_are_still_counted_as_sorted(st, expect):
    expect.text(st["post-recluster sorted_rows"], "7500", "the reclustered rows are still counted as sorted")


# ---- groups retired by compact_rewrite -------------------------------------------

def test_a_group_was_rewritten_out_of_the_run(st, expect):
    v = st["rewritten"]
    expect.text(("yes" if v > 0 else f"no ({v})") if isinstance(v, int) else f"no ({v})", "yes",
                "a group was rewritten out of the run")


def test_the_run_lost_the_retired_groups(st, expect):
    expect.text(st["run lost"], "yes", "the run lost the retired groups")


def test_the_replacement_counts_as_appended(st, expect):
    expect.text(st["replacement appended"], "yes", "the replacement counts as appended")


def test_the_totals_still_add_up(st, expect):
    expect.text(st["totals still"], "true", "the totals still add up")


def test_the_declared_sort_key_is_reported(st, expect):
    expect.text(st["declared key"], "k", "the declared sort key is reported")


# ---- an empty table ----------------------------------------------------------------

def test_an_empty_table_reports_no_groups(st, expect):
    expect.text(st["empty total_groups"], "0", "an empty table reports no groups")


def test_an_empty_table_reports_no_sorted_groups(st, expect):
    expect.text(st["empty sorted_groups"], "0", "an empty table reports no sorted groups")


def test_an_empty_table_reports_no_appended_rows(st, expect):
    expect.text(st["empty appended_rows"], "0", "an empty table reports no appended rows")


# ---- a delete, and the storage rows left behind -----------------------------------------

def test_the_delete_removed_rows(st, expect):
    v = st["doomed"]
    expect.text(("yes" if v > 0 else f"no ({v})") if isinstance(v, int) else f"no ({v})", "yes",
                "the delete removed rows")


def test_a_delete_leaves_the_sorted_extent_alone(st, expect):
    expect.num(st["extent after delete"], st["before delete"], "a delete leaves the sorted extent alone")


def test_a_delete_leaves_no_appended_tail(st, expect):
    expect.text(st["delete appended_groups"], "0", "a delete leaves no appended tail")


def test_live_rows_dropped_by_the_deleted_count(st, expect):
    expect.num(st["live after delete"], st["live expected"], "live rows dropped by the deleted count")


def test_the_mark_covers_the_runs_last_group(st, expect):
    expect.text(st["mark covers"], "true", "the mark covers the run's last group")


def test_the_storage_the_sort_abandoned_is_gone(st, expect):
    expect.text(st["abandoned gone"], "0", "the storage the sort abandoned is gone")


def test_dropping_the_table_removes_the_storage_row(st, expect):
    expect.text(st["dropped gone"], "0", "dropping the table removes the storage row")

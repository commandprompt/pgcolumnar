"""A table's ordering mark follows a column rename (#778, #779). The pytest twin of
`test/sorted_mark_rename.sh`, every check by the shell's name (#1341).

pgcolumnar.storage records the key a table is physically ordered on, by column NAME, and
two things trust it: vacuum_sorted's self-gate (#760), which skips a rewrite when the
mark already names the requested key, and the planner, which drops a Sort the mark says
is unneeded. Swap two columns' names and an unmaintained mark names the wrong column:
the gate skips work it must do, and ORDER BY returns rows out of order. Asserted: the
mark follows a rename, on a plain table, a partition (renamed through its parent), and
an inheritance child; an unrelated rename leaves the storage row alone; the planner
sorts again after a swap; the declared sort_by key follows too; and recluster's mark.

HOW A CALL IS READ. A vacuum_sorted call is read by the table's storage id: "noop",
"rewrote", "call failed: ..." when the call errored, or "no reading" when a storage id
is missing. A failed call moves nothing, so it used to read as a fired gate, and a
missing id after the call read as a rewrite. A plan with no scan in it is "no plan",
never "no Sort". Every fixture is held to its row count, since an empty table is in
every order at once.

ON A PRIVATE DATABASE, with max_parallel_workers_per_gather = 0 as the shell's cluster
runs, built once by a module fixture in the shell's order.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_sorted_mark_rename"
LOAD = "SELECT (g*7919::bigint)%2000, (g*104729::bigint)%2000 FROM generate_series(1,20000) g"


class _Db:
    def __init__(self, dsn):
        self.dsn, self.err = dsn, ""

    def run(self, sql):
        """-> "" when `sql` ran, else its error."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return ""

    def q(self, sql):
        """-> the first column as text ("" for NULL), or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return "" if row is None or row[0] is None else str(row[0])

    def num(self, sql):
        v = self.q(sql)
        return int(v) if v.lstrip("-").isdigit() else v

    def descents(self, t, col):
        return self.num(f"SELECT count(*) FROM (SELECT {col} < lag({col}) OVER () AS d FROM {t}) z WHERE d")

    def order(self, t, col):
        """-> "ordered" or "unordered", by the count of descents in storage order."""
        d = self.descents(t, col)
        return ("ordered" if d == 0 else "unordered") if isinstance(d, int) else f"no reading ({d})"

    def mark(self, t):
        return self.q("SELECT COALESCE(s.sorted_by::text,'-')||'/'||COALESCE(s.sorted_kind,'-') "
                      f"FROM pgcolumnar.storage s WHERE s.storage_id = pgcolumnar.get_storage_id('{t}')")

    def storid(self, t):
        return self.q(f"SELECT pgcolumnar.get_storage_id('{t}')")

    def xmin(self, t):
        return self.q("SELECT xmin::text FROM pgcolumnar.storage "
                      f"WHERE storage_id = pgcolumnar.get_storage_id('{t}')")

    def vsort(self, t, *cols):
        self.err = self.run(f"SELECT pgcolumnar.vacuum_sorted('{t}'" + "".join(f", '{c}'" for c in cols) + ")")

    def moved(self, t, s0):
        s1 = self.storid(t)
        if self.err:
            return f"call failed: {self.err}"
        if not s0 or not s1 or s0.startswith(QUERY_ERROR) or s1.startswith(QUERY_ERROR):
            return f"no reading: storage [{s0}] -> [{s1}]"
        return "noop" if s0 == s1 else "rewrote"

    def sorts(self, sql):
        """-> "yes" when the plan has a Sort node, "no" when it has none, "no plan" when the
        EXPLAIN failed: a failed EXPLAIN has no Sort in it either."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                plan = [r[0] for r in conn.execute(f"EXPLAIN (COSTS OFF) {sql}")]
        except psycopg.Error:
            return "no plan"
        if not any("Scan" in line for line in plan):
            return "no plan"
        return "yes" if any(line.lstrip(" ->").startswith(("Sort", "Incremental Sort")) for line in plan) else "no"

    def rows(self, t):
        return self.num(f"SELECT count(*) FROM {t}")

    def mk(self, t):
        for sql in [f"DROP TABLE IF EXISTS {t}", f"CREATE TABLE {t} (a int, b int) USING pgcolumnar",
                    f"INSERT INTO {t} {LOAD}"]:
            self.run(sql)

    def mkz(self, t):
        for sql in [f"DROP TABLE IF EXISTS {t}", f"CREATE TABLE {t} (a int, b int, c int) USING pgcolumnar",
                    f"SELECT pgcolumnar.set_options('{t}', chunk_group_row_limit => 1000)",
                    f"INSERT INTO {t} SELECT (g*7919::bigint)%2000, (g*104729::bigint)%2000, g "
                    "FROM generate_series(1,20000) g"]:
            self.run(sql)

    def swap(self, t, x, y):
        for sql in [f"ALTER TABLE {t} RENAME COLUMN {x} TO tmp_swap", f"ALTER TABLE {t} RENAME COLUMN {y} TO {x}",
                    f"ALTER TABLE {t} RENAME COLUMN tmp_swap TO {y}"]:
            self.run(sql)


@pytest.fixture(scope="module")
def sm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"ALTER DATABASE {DB} SET max_parallel_workers_per_gather = 0")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}

        # control: the gate fires on an unrenamed table
        d.mk("vr_ctl")
        r["vr_ctl rows"] = d.rows("vr_ctl")
        r["ctl unordered"] = d.order("vr_ctl", "a")
        d.vsort("vr_ctl", "a")
        r["ctl descents"] = d.descents("vr_ctl", "a")
        s = d.storid("vr_ctl")
        d.vsort("vr_ctl", "a")
        r["ctl noop"] = d.moved("vr_ctl", s)

        # swap two columns' names under a sorted table
        d.mk("vr_t")
        r["vr_t rows"] = d.rows("vr_t")
        d.vsort("vr_t", "a")
        r["t by a"], r["t by b"], r["t mark"] = d.order("vr_t", "a"), d.order("vr_t", "b"), d.mark("vr_t")
        d.swap("vr_t", "a", "b")
        r["t swapped b"], r["t swapped a"] = d.order("vr_t", "b"), d.order("vr_t", "a")
        r["t mark after"] = d.mark("vr_t")
        s = d.storid("vr_t")
        d.vsort("vr_t", "a")
        r["t point"] = d.moved("vr_t", s)
        r["t descents"] = d.descents("vr_t", "a")
        s = d.storid("vr_t")
        d.vsort("vr_t", "a")
        r["t gate"] = d.moved("vr_t", s)

        # an unrelated rename
        d.mk("vr_o")
        r["vr_o rows"] = d.rows("vr_o")
        d.run("ALTER TABLE vr_o ADD COLUMN spare int")
        d.vsort("vr_o", "a")
        m, x0 = d.mark("vr_o"), d.xmin("vr_o")
        d.run("ALTER TABLE vr_o RENAME COLUMN spare TO spare2")
        r["o mark"], r["o mark before"] = d.mark("vr_o"), m
        r["o xmin"], r["o xmin before"] = d.xmin("vr_o"), x0
        d.run("ALTER TABLE vr_o RENAME COLUMN a TO a2")
        x1 = d.xmin("vr_o")
        d.run("ALTER TABLE vr_o RENAME COLUMN a2 TO a")
        ok = all(v and not v.startswith(QUERY_ERROR) for v in (x0, x1))
        r["o xmin moves"] = ("moved" if x1 != x0 else "stuck") if ok else f"no reading: xmin [{x0}] -> [{x1}]"
        s = d.storid("vr_o")
        d.vsort("vr_o", "a")
        r["o gate"] = d.moved("vr_o", s)

        # a partition, renamed through its parent
        for sql in ["CREATE TABLE prt (k int, j int) PARTITION BY RANGE (k)",
                    "CREATE TABLE prt1 PARTITION OF prt FOR VALUES FROM (0) TO (100000) USING pgcolumnar",
                    f"INSERT INTO prt {LOAD}"]:
            d.run(sql)
        r["prt rows"] = d.rows("prt1")
        d.vsort("prt1", "k")
        r["prt sorted"] = f"{d.order('prt1', 'k')}/{d.mark('prt1')}"
        r["prt parent am"] = d.q("SELECT COALESCE((SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid=c.relam "
                                 "WHERE c.relname='prt'),'none')")
        d.swap("prt", "k", "j")
        r["prt j"], r["prt k"], r["prt mark"] = d.order("prt1", "j"), d.order("prt1", "k"), d.mark("prt1")
        s = d.storid("prt1")
        d.vsort("prt1", "k")
        r["prt work"] = d.moved("prt1", s)
        r["prt descents"] = d.descents("prt1", "k")

        # an inheritance child, renamed through its parent
        for sql in ["CREATE TABLE inp (k int, j int)", "CREATE TABLE inc (LIKE inp) INHERITS (inp) USING pgcolumnar",
                    f"INSERT INTO inc {LOAD}"]:
            d.run(sql)
        d.vsort("inc", "k")
        r["inc sorted"] = f"{d.order('inc', 'k')}/{d.rows('inc')}"
        d.swap("inp", "k", "j")
        r["inc mark"] = d.mark("inc")
        s = d.storid("inc")
        d.vsort("inc", "k")
        r["inc work"] = d.moved("inc", s)

        # the planner trusts the mark
        for sql in ["CREATE TABLE wp (k int, j int) PARTITION BY RANGE (k)",
                    "CREATE TABLE wp1 PARTITION OF wp FOR VALUES FROM (0) TO (100000) USING pgcolumnar",
                    f"INSERT INTO wp {LOAD}"]:
            d.run(sql)
        d.vsort("wp1", "k")
        r["wp before"] = d.sorts("SELECT k FROM wp1 ORDER BY k")
        d.swap("wp", "k", "j")
        r["wp after"] = d.sorts("SELECT k FROM wp1 ORDER BY k")
        r["wp answer"] = d.q("SELECT count(*) FILTER (WHERE d) || ' descents/' || count(*) || ' rows' FROM "
                             "(SELECT k < lag(k) OVER () AS d FROM (SELECT k FROM wp1 ORDER BY k) x) z")
        r["wp j"] = d.sorts("SELECT j FROM wp1 ORDER BY j")

        # the declared sort_by key
        d.mk("sb")
        r["sb rows"] = d.rows("sb")
        d.run("SELECT pgcolumnar.set_options('sb', sort_by => ARRAY['a'])")
        d.vsort("sb")
        r["sb ordered"] = d.order("sb", "a")
        d.swap("sb", "a", "b")
        r["sb declared"] = d.q("SELECT sort_by::text FROM pgcolumnar.options WHERE regclass = 'sb'::regclass")
        r["sb mark"] = d.q("SELECT sorted_by::text FROM pgcolumnar.storage "
                           "WHERE storage_id = pgcolumnar.get_storage_id('sb')")
        s = d.storid("sb")
        d.vsort("sb")
        r["sb noop"] = d.moved("sb", s)

        # recluster's mark
        d.mkz("rr_ctl")
        r["rr_ctl rows"] = d.rows("rr_ctl")
        r["rr first"] = d.num("SELECT pgcolumnar.recluster('rr_ctl','a','b')")
        r["rr repeat"] = d.num("SELECT pgcolumnar.recluster('rr_ctl','a','b')")
        d.mkz("rr_t")
        r["rr_t rows"] = d.rows("rr_t")
        d.run("SELECT pgcolumnar.recluster('rr_t','a','b')")
        r["rr mark"] = d.mark("rr_t")
        d.run("ALTER TABLE rr_t RENAME COLUMN b TO b_old")
        d.run("ALTER TABLE rr_t RENAME COLUMN c TO b")
        r["rr mark after"] = d.mark("rr_t")
        r["rr after"] = d.num("SELECT pgcolumnar.recluster('rr_t','a','b')")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _positive(v, yes, no):
    return (yes if v > 0 else no) if isinstance(v, int) else f"no reading ({v})"


# ---- control: the gate fires -----------------------------------------------------

def test_vr_ctl_loaded_every_row(sm, expect):
    expect.num(sm["vr_ctl rows"], 20000, "premise: vr_ctl loaded every row (an overflowed INSERT would make an empty table look like a fired gate)")


def test_before_sorting_a_is_not_in_order(sm, expect):
    expect.text(sm["ctl unordered"], "unordered", "premise: before sorting, a is not in order")


def test_vacuum_sorted_really_ordered_it(sm, expect):
    expect.num(sm["ctl descents"], 0, "premise: vacuum_sorted really ordered it by a")


def test_a_second_identical_call_is_a_no_op(sm, expect):
    expect.text(sm["ctl noop"], "noop", "CONTROL: a second identical call is a no-op, so the gate does fire")


# ---- a swap of two column names -----------------------------------------------------

def test_vr_t_loaded_every_row(sm, expect):
    expect.num(sm["vr_t rows"], 20000, "premise: vr_t loaded every row (an overflowed INSERT would make an empty table look like a fired gate)")


def test_vr_t_is_ordered_by_a(sm, expect):
    expect.text(sm["t by a"], "ordered", "premise: vr_t is ordered by a")


def test_vr_t_is_not_ordered_by_b(sm, expect):
    expect.text(sm["t by b"], "unordered", "premise: and NOT by b")


def test_the_mark_records_a(sm, expect):
    expect.text(sm["t mark"], "{a}/lexicographic", "premise: the mark records key {a}")


def test_after_the_swap_b_is_ordered(sm, expect):
    expect.text(sm["t swapped b"], "ordered", "premise: after the swap the ordered column is the one now named b")


def test_after_the_swap_a_is_not_ordered(sm, expect):
    expect.text(sm["t swapped a"], "unordered", "premise: and the column now named a is NOT ordered")


def test_the_mark_follows_the_rename(sm, expect):
    expect.text(sm["t mark after"], "{b}/lexicographic",
                "the mark FOLLOWS the rename, so it still names the ordered column")


def test_vacuum_sorted_after_the_swap_does_the_work(sm, expect):
    expect.text(sm["t point"], "rewrote", "THE POINT: vacuum_sorted('a') after the swap does the work")


def test_the_table_is_ordered_by_a_afterwards(sm, expect):
    expect.num(sm["t descents"], 0, "and the table really is ordered by a afterwards")


def test_the_gate_still_fires_for_the_right_key(sm, expect):
    expect.text(sm["t gate"], "noop", "the gate still fires for the right key after all that")


# ---- an unrelated rename ---------------------------------------------------------------

def test_vr_o_loaded_every_row(sm, expect):
    expect.num(sm["vr_o rows"], 20000, "premise: vr_o loaded every row (an overflowed INSERT would make an empty table look like a fired gate)")


def test_an_unrelated_rename_leaves_the_mark(sm, expect):
    expect.text(sm["o mark"], sm["o mark before"], "renaming an unrelated column does not disturb the mark")


def test_an_unrelated_rename_does_not_rewrite_the_storage_row(sm, expect):
    expect.text(sm["o xmin"], sm["o xmin before"], "and does not rewrite the storage row at all (xmin unchanged)")


def test_xmin_can_move(sm, expect):
    expect.text(sm["o xmin moves"], "moved",
                "premise: xmin CAN move, so the arm above is not comparing two constants")


def test_the_gate_still_fires_after_an_unrelated_rename(sm, expect):
    expect.text(sm["o gate"], "noop", "and the gate still fires")


# ---- a partition -------------------------------------------------------------------------

def test_the_partition_holds_every_row(sm, expect):
    expect.num(sm["prt rows"], 20000, "premise: the partition holds every row")


def test_the_partition_is_ordered_and_marked(sm, expect):
    expect.text(sm["prt sorted"], "ordered/{k}/lexicographic",
                "premise: the partition is ordered by k and its mark says so")


def test_the_parent_is_not_columnar(sm, expect):
    expect.text(sm["prt parent am"], "none",
                "premise: the parent is NOT itself columnar, so gating on it would skip the child")


def test_after_the_swap_the_partitions_ordered_column_is_j(sm, expect):
    expect.text(sm["prt j"], "ordered", "premise: after the swap the partition's ordered column is named j")


def test_after_the_swap_the_partitions_k_is_not_ordered(sm, expect):
    expect.text(sm["prt k"], "unordered", "premise: and the column now named k is NOT ordered")


def test_the_partitions_mark_follows_a_rename_on_the_parent(sm, expect):
    expect.text(sm["prt mark"], "{j}/lexicographic", "the PARTITION's mark follows a rename issued on the parent")


def test_vacuum_sorted_on_the_partition_does_the_work(sm, expect):
    expect.text(sm["prt work"], "rewrote", "so vacuum_sorted on the partition does the work")


def test_the_partition_is_ordered_by_k_afterwards(sm, expect):
    expect.num(sm["prt descents"], 0, "and the partition really is ordered by k afterwards")


# ---- an inheritance child -------------------------------------------------------------

def test_the_inheritance_child_is_ordered(sm, expect):
    expect.text(sm["inc sorted"], "ordered/20000", "premise: the inheritance child is ordered by k")


def test_the_childs_mark_follows_a_rename_on_the_parent(sm, expect):
    expect.text(sm["inc mark"], "{j}/lexicographic", "the CHILD's mark follows a rename issued on the parent")


def test_vacuum_sorted_on_the_child_does_the_work(sm, expect):
    expect.text(sm["inc work"], "rewrote", "so vacuum_sorted on the child does the work")


# ---- the planner -------------------------------------------------------------------------

def test_with_the_mark_true_order_by_needs_no_sort(sm, expect):
    expect.text(sm["wp before"], "no", "premise: with the mark true, ORDER BY k needs no Sort")


def test_after_the_swap_order_by_plans_a_sort(sm, expect):
    expect.text(sm["wp after"], "yes", "after the swap, ORDER BY on the now-unordered column plans a Sort")


def test_the_rows_come_back_ordered(sm, expect):
    expect.text(sm["wp answer"], "0 descents/20000 rows", "THE WRONG-ANSWER ARM: and the rows really do come back ordered")


def test_order_by_on_the_ordered_column_needs_no_sort(sm, expect):
    expect.text(sm["wp j"], "no", "and ORDER BY on the column that IS ordered still needs no Sort")


# ---- the declared key ------------------------------------------------------------------

def test_sb_loaded_every_row(sm, expect):
    expect.num(sm["sb rows"], 20000, "premise: sb loaded every row (an overflowed INSERT would make an empty table look like a fired gate)")


def test_a_bare_vacuum_sorted_used_the_declared_key(sm, expect):
    expect.text(sm["sb ordered"], "ordered", "premise: a bare vacuum_sorted() used the declared key")


def test_the_declared_key_follows_the_rename(sm, expect):
    expect.text(sm["sb declared"], "{b}", "the DECLARED key follows the rename too")


def test_the_two_catalogs_still_agree(sm, expect):
    expect.text(sm["sb declared"], sm["sb mark"], "so the two catalogs still agree after the rename")


def test_a_bare_vacuum_sorted_is_a_no_op(sm, expect):
    expect.text(sm["sb noop"], "noop", "and a bare vacuum_sorted() is a no-op, not a rewrite on a different key")


# ---- recluster ---------------------------------------------------------------------------

def test_rr_ctl_loaded_every_row(sm, expect):
    expect.num(sm["rr_ctl rows"], 20000, "premise: rr_ctl loaded every row")


def test_the_first_recluster_does_real_work(sm, expect):
    expect.text(_positive(sm["rr first"], "yes", "no"), "yes", "premise: the first recluster does real work")


def test_a_repeat_recluster_is_a_no_op(sm, expect):
    expect.num(sm["rr repeat"], 0, "CONTROL: a repeat recluster on the same key is a no-op, so the gate fires")


def test_rr_t_loaded_every_row(sm, expect):
    expect.num(sm["rr_t rows"], 20000, "premise: rr_t loaded every row")


def test_the_mark_records_a_zorder_run(sm, expect):
    expect.text(sm["rr mark"], "{a,b}/zorder", "premise: the mark records {a,b} as zorder")


def test_the_mark_follows_both_renames(sm, expect):
    expect.text(sm["rr mark after"], "{a,b_old}/zorder", "the mark follows both renames")


def test_recluster_on_the_new_key_is_not_skipped(sm, expect):
    expect.text(_positive(sm["rr after"], "rewrote", "SKIPPED (gate fired wrongly)"), "rewrote",
                "recluster on the NEW (a,b) is not skipped as already done")

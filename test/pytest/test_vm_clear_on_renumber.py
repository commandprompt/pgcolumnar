"""The visibility map is cleared wherever live rows are renumbered. The pytest twin of
`test/vm_clear_on_renumber.sh`, every check by the shell's name (#1341).

`compact_rewrite` (rewrite_one_group) and `recluster` (recluster_online) retire a row
group and give its live rows new row numbers, so each must clear the all-visible bits
over the OLD numbers. Otherwise an index-only scan answers from the index for a TID
whose group no longer exists. The instrument is `pgcolumnar.vm_is_visible(rel, blk)`,
which reads the fork through the call the index-only-scan executor makes;
`relallvisible` is a VACUUM statistic and does not move when a bit is cleared.

Part 1 vacuums, deletes only the FRONT of group 3, and asserts over the group's LATER
block, which only the rewrite can clear; a group the rewrite does not touch is the
control. Part 2 has no in-table control, because recluster renumbers every group, so a
second relation is the control: it catches a clear that escapes its relation, not an
over-broad range.

ON A PRIVATE DATABASE, so another worker's snapshot cannot hold back VACUUM's
all-visible marking, built once by a module fixture in the shell's order; each test
asserts one reading. Every "cleared" reading is the transition `before->after` and
wants `t->f`: a VACUUM that set no bit leaves every block `f`, and an after-only
reading passed as cleared. The shell's geometry premises, which exit rather than
record, refuse the fixture here, so every test errors rather than passes.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_vm_clear_on_renumber"
SPAN = 291  # row numbers per synthetic block: MaxHeapTuplesPerPage at BLCKSZ 8192


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    """-> the first column of the first row as text, on a connection of its own, or the
    failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    if row is None or row[0] is None:
        return ""
    return {True: "t", False: "f"}.get(row[0], str(row[0])) if isinstance(row[0], bool) else str(row[0])


def _run(dsn, *sqls):
    """Statements in one session, as the shell's psql_run does; a failure is printed and
    leaves the readings to show it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in sqls:
                conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")


def _premise(ok, why):
    if not ok:
        pytest.fail(f"PREMISE FAILED: {why}", pytrace=False)


def _int(v, what):
    _premise(v.isdigit(), f"{what} unreadable [{v}]")
    return int(v)


def _blk(row):
    return row // SPAN


@pytest.fixture(scope="module")
def vm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        vis = lambda rel, blk: _value(dsn, f"SELECT pgcolumnar.vm_is_visible('{rel}', {blk})")
        geo = ("SELECT {c} FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('{t}')"
               " AND group_number = {g}")
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _premise(_value(dsn, "SELECT current_setting('block_size')") == "8192",
                 "block_size is not 8192; the block arithmetic is written for it")
        r = {}

        # ---- Part 1: rewrite_one_group, through compact_rewrite ----
        _run(dsn, "CREATE TABLE cw (id int, v int) USING pgcolumnar",
             "SELECT pgcolumnar.set_options('cw', stripe_row_limit => 2048, chunk_group_row_limit => 1024)",
             "INSERT INTO cw SELECT g, g FROM generate_series(1, 20480) g",
             "CREATE INDEX cw_id ON cw (id)", "VACUUM cw")
        g3_first = _int(_value(dsn, geo.format(c="first_row_number", t="cw", g=3)), "group 3's first row")
        g3_count = _int(_value(dsn, geo.format(c="row_count", t="cw", g=3)), "group 3's row count")
        g5_first = _int(_value(dsn, geo.format(c="first_row_number", t="cw", g=5)), "group 5's first row")
        front_end = g3_first + 600
        target = _blk(g3_first + g3_count - 1 - 100)  # late in group 3, no deleted row
        front = _blk(g3_first)                        # early in group 3, deleted rows
        ctrl = _blk(g5_first + 100)                   # group 5, untouched throughout
        _premise(target > _blk(front_end), f"target block {target} is inside the deleted range")

        front_v0 = vis("cw", front)
        r["late set"] = vis("cw", target)
        r["ctrl set"] = vis("cw", ctrl)
        _run(dsn, f"DELETE FROM cw WHERE id BETWEEN {g3_first} AND {front_end}")
        r["delete cleared"] = f"{front_v0}->{vis('cw', front)}"
        target_v1 = vis("cw", target)
        r["late kept"] = target_v1
        r["ctrl kept"] = vis("cw", ctrl)
        r["rewrote"] = _value(dsn, "SELECT pgcolumnar.compact_rewrite('cw', 0.2)")
        r["group 3 gone"] = _value(dsn, geo.format(c="count(*)", t="cw", g=3))
        r["rewrite cleared"] = f"{target_v1}->{vis('cw', target)}"
        r["rewrite ctrl"] = vis("cw", ctrl)

        # ---- Part 2: recluster_online, through recluster ----
        for t in ("rc", "rcctl"):
            _run(dsn, f"CREATE TABLE {t} (id int, x int, y int) USING pgcolumnar",
                 f"SELECT pgcolumnar.set_options('{t}', stripe_row_limit => 2048, chunk_group_row_limit => 1024)",
                 f"INSERT INTO {t} SELECT g, ((g::bigint*7919)%200)::int, ((g::bigint*104729)%200)::int "
                 "FROM generate_series(1, 20480) g",
                 f"VACUUM {t}")
        span = ("SELECT {f} FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('rc')")
        rc_first = _int(_value(dsn, span.format(f="min(first_row_number)")), "rc's first row")
        rc_last = _int(_value(dsn, span.format(f="max(first_row_number + row_count - 1)")), "rc's last row")
        # three whole blocks in from each end: the end blocks are partly covered, read f
        # on a correct tree, and could not fail
        early, late = _blk(rc_first) + 3, _blk(rc_last) - 3
        _premise(early < late and early * SPAN >= rc_first and (late + 1) * SPAN - 1 <= rc_last,
                 f"blocks {early}/{late} are not wholly inside rows {rc_first}..{rc_last}")
        early_v0, late_v0 = vis("rc", early), vis("rc", late)
        r["rc early set"], r["rc late set"] = early_v0, late_v0
        r["rcctl set"] = vis("rcctl", early)
        groups = _value(dsn, "SELECT pgcolumnar.recluster('rc', 'x', 'y')")
        r["reclustered"] = int(groups) if groups.isdigit() else groups
        r["rc early cleared"] = f"{early_v0}->{vis('rc', early)}"
        r["rc late cleared"] = f"{late_v0}->{vis('rc', late)}"
        r["rcctl early"] = vis("rcctl", early)
        r["rcctl late"] = vis("rcctl", late)
        print(f"-- blocks: cw target={target} front={front} ctrl={ctrl}; rc early={early} late={late}")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- Part 1: rewrite_one_group ----------------------------------------------

def test_vacuum_set_all_visible_on_group_3s_late_block(vm, expect):
    expect.text(vm["late set"], "t", "premise: VACUUM set all-visible on group 3's late block")


def test_and_on_the_untouched_control_groups_block(vm, expect):
    expect.text(vm["ctrl set"], "t", "premise: and on the untouched control group's block")


def test_the_delete_cleared_the_block_holding_the_deleted_rows(vm, expect):
    expect.text(vm["delete cleared"], "t->f", "the delete cleared the block holding the deleted rows")


def test_but_left_group_3s_late_block_all_visible(vm, expect):
    expect.text(vm["late kept"], "t", "premise: but left group 3's late block all-visible")


def test_and_left_the_control_block_all_visible(vm, expect):
    expect.text(vm["ctrl kept"], "t", "premise: and left the control block all-visible")


def test_compact_rewrite_rewrote_exactly_the_one_qualifying_group(vm, expect):
    expect.text(vm["rewrote"], "1", "compact_rewrite rewrote exactly the one qualifying group")


def test_group_3_is_gone_from_the_catalog(vm, expect):
    expect.text(vm["group 3 gone"], "0", "premise: group 3 is gone from the catalog, so its rows were renumbered")


def test_rewrite_one_group_cleared_the_retired_groups_late_block(vm, expect):
    expect.text(vm["rewrite cleared"], "t->f", "rewrite_one_group cleared the retired group's late block (#877)")


def test_a_group_the_rewrite_did_not_touch_keeps_its_all_visible_bit(vm, expect):
    expect.text(vm["rewrite ctrl"], "t", "control: a group the rewrite did not touch keeps its all-visible bit")


# ---- Part 2: recluster_online -----------------------------------------------

def test_vacuum_set_all_visible_early_in_rc(vm, expect):
    expect.text(vm["rc early set"], "t", "premise: VACUUM set all-visible early in rc")


def test_and_late_in_rc(vm, expect):
    expect.text(vm["rc late set"], "t", "premise: and late in rc")


def test_and_on_the_control_relation(vm, expect):
    expect.text(vm["rcctl set"], "t", "premise: and on the control relation")


def test_recluster_reported_reclustering_groups(vm, expect):
    expect.at_least(vm["reclustered"], 1, "recluster reported reclustering groups")


def test_recluster_online_cleared_the_old_numbers_early_block(vm, expect):
    expect.text(vm["rc early cleared"], "t->f", "recluster_online cleared the old numbers' early block (#877)")


def test_recluster_online_cleared_the_old_numbers_late_block(vm, expect):
    expect.text(vm["rc late cleared"], "t->f", "recluster_online cleared the old numbers' late block (#877)")


def test_a_relation_that_was_not_reclustered_keeps_its_all_visible_bits(vm, expect):
    expect.text(vm["rcctl early"], "t", "control: a relation that was not reclustered keeps its all-visible bits")


def test_and_its_late_block_too(vm, expect):
    expect.text(vm["rcctl late"], "t", "control: and its late block too")

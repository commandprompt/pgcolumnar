"""Retiring a row group must not cost more because other columnar tables exist.

`delete_group_rows()` opens its catalog from a `const char *tableName`
PARAMETER, and `PgColumnarDeleteGroupMetadata` calls it five times, for
`delete_vector`, `column_chunk`, `zone_map`, `bloom` and `row_group`. One
`systable_beginscan` in the source was therefore five sequential reads per
retired group at run time. The `pgcolumnar` metadata catalogs are SHARED by
every columnar table in the database, so each read was charged for every other
table's rows.

THE FIX IS NOT "USE THE INDEX". It is "ask the catalog how big it is, and use
the index when that is the cheaper read". A probe is a btree descent plus a heap
fetch plus two catcache lookups, which on a catalog of a few pages is MORE work
than reading the whole thing, and far less on a large one. Both sizes occur in
one installation, because the catalogs are shared -- so these arms measure at
three catalog sizes rather than one, and the claim at each is the one that size
can carry.

WHAT THEY ASSERT. The WORK: buffers served out of the six catalogs, heap and
index, from `pg_statio_all_tables`. Never the access path. An earlier version
asserted `seq_scan == 0` and `idx_scan >= 1` per catalog; run against the build
that chooses by catalog size -- cheaper at every size measured -- it failed 13
of 21 arms, the same 13 a full revert reddens. A guard that fires on correct
code gets switched off.

NO CONSTANT ANYWHERE. Every buffer count is compared against another taken in
the same run from the same build, because the numbers are not the same on every
major: the same fixture reads 848 buffers on PG17, 845 on PG15 and 895 on PG19.
`pgcolumnar.index_min_blocks` decides the path -- 0 probes every catalog, a very
large value reads every one whole -- so the same work is measured two or three
ways and the readings are compared to each other.

Independent of `test/catalog_delete_index.sh`: different tables, different row
counts, different group sizes, different retention pattern (this half retires
the LAST half of its groups, the shell half retires every other one), a smaller
deep fixture, and this half reads the work per catalog and sums in Python where
the shell half sums in SQL.

`pg_stat_reset()` is database-wide. Tests run serially within a worker, so
nothing else is counting during these tests, and this file must not be run
concurrently with another that reads statistics.
"""

# Phase 0 is small ON PURPOSE. The claim it carries -- that probing every
# catalog costs more than choosing -- is worth 284 parts per thousand at six
# catalog pages, 151 at nine and 31 at eighteen, against a drift of 3 to 10
# throughout. A bigger fixture hides a real effect.
TINY_GROUPS = 12
BIG_GROUPS = 45
GROUP = 1000

DEEP_ROWS = 150000
DEEP_GROUP = 1024

# The vacuum test needs a BIGGER deep table than the compaction test does, and
# that is not padding. Its claim is the difference between reading `row_group`
# whole and fetching one small table's rows from it, so it needs `row_group`
# itself above the threshold. In a private database with only this test's
# tables in it, 150,000 rows leaves `row_group` at two pages -- below the
# setting -- and BOTH paths then read it whole and the arm reports 0. Measured:
# that is exactly how it failed once the fixture was corrected.
VAC_DEEP_ROWS = 600000

PROBE_ALWAYS = 0
READ_WHOLE = 2147483647

# HOW FAR APART TWO READINGS MUST BE BEFORE THIS FILE CALLS THE DIFFERENCE A
# RESULT, in parts per thousand of the reading they are compared against.
#
# MEASURED, NOT CHOSEN, AND THE FIRST TWO ATTEMPTS WERE BOTH WRONG. Every claim
# compares two compactions of two different tables run one after another, and
# compaction WRITES to `row_group` and `free_space`, so the next compaction
# reads more of them. Two readings from identical code drift apart.
#
#   A floor of ONE BUFFER let a full revert through: fifteen arms of sixteen
#   passed against code with the fix removed, carried by 2 to 8 buffers.
#
#   A floor of TEN PARTS PER THOUSAND let it through too. Under a full revert
#   the drift reached 14 to 16 -- above the floor -- and two arms of eighteen
#   reddened. Worse, the two arms it was meant to protect were worth only 22 and
#   26, so they sat inside the drift: they measured the sequence, not the fix.
#
# The floor is 100, and each claim is made where it is worth several times that.
# Measured, this build against the three mutations, in parts per thousand:
#
#     arm                    real   revert   probe-always   default replaced
#     P1 (6 pages)            284        0             12        284 (passes)
#     A1 (22 pages)           237       -1             -2         -1
#     B1 (76 pages)          1823        0             -1          0
#     growth (A to B)         960        1             22          1
#     vacuum                  714        0              0          0
#
#     measured drift            -      1-7           2-18        1-7
#
# Every claim clears the floor by at least 2.4x; every mutation falls at least
# 4.5x below it. Two arms were DELETED rather than rescued; what a fixture
# cannot measure, this file does not assert.
FLOOR_PERMILLE = 100


CATALOGS = (
    "bloom",
    "column_chunk",
    "delete_vector",
    "free_space",
    "row_group",
    "zone_map",
)


# `pgc_own_db` moved to conftest.py when a second file needed it (#1217). A
# guarantee kept in one suite has to be re-fitted to every other that needs it,
# and this one is about a trap that is invisible from inside the file it bites.


def _columnar_relations(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
            "WHERE a.amname = 'pgcolumnar'"
        )
        return cur.fetchone()[0]


def _permille(part, whole):
    """`part` as thousandths of `whole`; 0 for a whole of 0, which fails."""
    return (part * 1000) // whole if whole > 0 else 0


def _build_kind(conn):
    """Which build kind this run measured, printed rather than asserted.

    Two of the eight converted scan sites are in
    `PgColumnarCheckFreeSpaceNoOverlap`, which is assert-only. On a release
    build they do not execute, so every arm here is a WEAKER claim there: it
    says nothing about those two sites rather than clearing them.

    The probe run that closed the account for #1207 was on a release build and
    reported the compaction path fully clean while the assert-enabled suite
    still showed a scan on each of two catalogs. Nothing in the measurement
    said which build it was, so the zero read as an answer rather than a
    partial one.

    Printed and NOT made an arm: it records the condition the run happened in,
    and breaking the code under test cannot change it.
    """
    with conn.cursor() as cur:
        cur.execute("SHOW debug_assertions")
        assertions = cur.fetchone()[0]
        cur.execute("SHOW pgcolumnar.index_min_blocks")
        return assertions, cur.fetchone()[0]


def _work(conn):
    """Buffers the six catalogs have served, heap AND index, per catalog.

    Index blocks are counted because the question is total work: a probe that
    read only index pages would otherwise look free, which is the error this
    file exists to avoid.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relname, "
            "  coalesce(heap_blks_read,0) + coalesce(heap_blks_hit,0) "
            "+ coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0) "
            "FROM pg_statio_all_tables "
            "WHERE schemaname = 'pgcolumnar' AND relname = ANY(%s)",
            (list(CATALOGS),),
        )
        return {r[0]: int(r[1]) for r in cur.fetchall()}


def _catpages(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(sum(pg_relation_size('pgcolumnar.' || relname) / 8192), 0) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'pgcolumnar' AND relname = ANY(%s)",
            (list(CATALOGS),),
        )
        return int(cur.fetchone()[0])


def _groups_of(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.row_group r "
            "JOIN pgcolumnar.storage s USING (storage_id) "
            "WHERE s.relation_oid = %s::regclass::oid",
            (table,),
        )
        return cur.fetchone()[0]


def _make_target(conn, table, groups):
    """A measurement table: `groups` groups, the last half emptied.

    One per measurement. A compaction retires its groups once, so a second
    reading of the same table would measure a compaction that found nothing
    left to do -- which reports a small number for the same reason a fast one
    does.
    """
    rows = groups * GROUP
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (k bigint, tag int) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options(%s, stripe_row_limit => %s)", (table, GROUP)
        )
        cur.execute(
            f"INSERT INTO {table} SELECT g, g % 7 FROM generate_series(1,{rows}) g"
        )
        cur.execute(f"DELETE FROM {table} WHERE k > {(groups // 2) * GROUP}")


def _compact_work(conn, table, min_blocks=None):
    """Total catalog buffers the compaction of `table` cost, and the breakdown.

    FLUSH BEFORE THE RESET, NOT ONLY AFTER. This harness holds ONE connection
    for the whole file, so the writes that built the fixture leave pending
    statistics in this backend that `pg_stat_reset()` does not clear -- they
    flush afterwards and land on top of the reading. The shell twin cannot
    reach this state: it runs every statement in a fresh backend, which flushes
    on exit before the next one starts.

    Measured, on an otherwise identical single-session fixture:
        reset with pending stats   row_group idx=36 seq=15
        flush BEFORE reset         row_group idx=32 seq=0
    The fifteen were the test's own DELETE, one scan per retired group,
    arriving after the counter had been zeroed.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT pg_stat_reset()")
        if min_blocks is not None:
            cur.execute(f"SET pgcolumnar.index_min_blocks = {min_blocks}")
        cur.execute("SELECT pgcolumnar.compact(%s)", (table,))
        cur.execute("RESET pgcolumnar.index_min_blocks")
        cur.execute("SELECT pg_stat_force_next_flush()")
    per_catalog = _work(conn)
    return sum(per_catalog.values()), per_catalog


def _phase(conn, expect, name, prefix, groups, other, other_label, carried=0):
    """Measure a phase: a control and the default, then `other`.

    THE CONTROL IS COMPACTED FIRST, NEXT TO THE DEFAULT. Drift accumulates with
    distance, so a control three steps from the default measures three steps of
    it and condemns a claim exposed to one. It did: placed last, one phase
    reported 32 parts per thousand of noise against a margin of 31.

    Returns (default, other, pages).
    """
    tables = [f"{prefix}_control", f"{prefix}_default", f"{prefix}_other"]
    for t in tables:
        _make_target(conn, t, groups)
    pages = _catpages(conn)
    expect.num(
        sum(_groups_of(conn, t) for t in tables),
        groups * 3,
        f"premise: the three phase {name} targets are the same fixture",
    )
    # THE PREMISE THAT WOULD HAVE CAUGHT THE FIXTURE DEFECT. Phase 0's claim is
    # about a catalog of a few pages, and it reports a smaller margin rather
    # than an error when the catalogs are large -- 65 parts per thousand
    # instead of 223, which reads as a weak result and not as a broken fixture.
    # Counting the relations says which it is.
    expect.num(
        _columnar_relations(conn) - carried,
        3,
        f"premise: the phase {name} catalogs hold only this file's tables",
    )

    control, _ = _compact_work(conn, tables[0])
    default, by_cat = _compact_work(conn, tables[1])
    other_work, other_cat = _compact_work(conn, tables[2], other)
    noise = _permille(abs(control - default), default)
    margin = _permille(other_work - default, default)

    print(f"-- phase {name}  catalog pages={pages}  work: control={control} "
          f"default={default} {other_label}={other_work}")
    print(f"-- phase {name}  permille vs the default: {other_label}={margin} noise={noise}")
    print(f"--   by catalog  default={by_cat}")
    print(f"--   by catalog  {other_label}={other_cat}")

    # DERIVED FROM `groups`, NOT ASSUMED EVEN. _make_target empties everything
    # past the halfway row, so an odd group count retires the larger half:
    # at 45 groups it is 23 that go and 22 that stay, and a premise written as
    # `groups // 2` fails on the fixture rather than on the code.
    expect.num(
        groups - _groups_of(conn, tables[1]),
        groups - groups // 2,
        f"premise: the phase {name} compaction retired the emptied groups",
    )
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {tables[1]}")
        expect.num(
            cur.fetchone()[0],
            (groups // 2) * GROUP,
            f"premise: the phase {name} compaction kept every surviving row",
        )
    expect.num(
        len(by_cat),
        len(CATALOGS),
        f"premise: the phase {name} reading covers every catalog the arms name",
    )
    expect.at_least(
        default, 1, f"premise: the phase {name} reading measured something"
    )
    # THE ARMS ARE ONLY AS GOOD AS THIS ONE. A third identical table is
    # compacted at the SAME setting as the measured one, so the two readings
    # differ only by where they sit in the sequence. If that ever approaches the
    # floor the claims are asserted against, the claims stop meaning anything.
    expect.at_least(
        FLOOR_PERMILLE - noise,
        1,
        f"premise: two phase {name} compactions at the same setting "
        "agree well inside the floor",
    )
    return default, other_work, pages, margin


def test_retiring_a_group_costs_no_more_for_a_bigger_database(pgc_own_db, expect):
    conn = pgc_own_db
    assertions, min_blocks = _build_kind(conn)
    print(f"-- debug_assertions={assertions} "
          "(off = the two assert-only sites did not run)")
    print(f"-- pgcolumnar.index_min_blocks={min_blocks} "
          "(the shipped default this run measures)")

    # NO SEPARATE NOISE TABLE. Each phase already holds three storages, so no
    # arm can pass on a catalog that happens to hold only one.
    _d0, _p0, _pages0, margin0 = _phase(
        conn, expect, "0", "ret0", TINY_GROUPS, PROBE_ALWAYS, "probe-always"
    )
    expect.at_least(
        margin0,
        FLOOR_PERMILLE,
        "P1 with a few catalog pages the default does less work than probing every one",
    )

    da, _sa, pages_a, margin_a = _phase(
        conn, expect, "A", "retA", BIG_GROUPS, READ_WHOLE, "read-whole", carried=3
    )
    expect.at_least(
        margin_a,
        FLOOR_PERMILLE,
        "A1 with more catalog pages the default does less work than reading every one whole",
    )
    sa = _sa

    # ONE DEEP TABLE, NOT MANY SHALLOW ONES. What makes a sequential read
    # expensive is catalog PAGES, not how many tables share the catalogs;
    # reaching this size with one-group tables took a thousand of them.
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE retire_deep (k bigint, a int, b int, c text) USING pgcolumnar"
        )
        cur.execute(
            "SELECT pgcolumnar.set_options('retire_deep', stripe_row_limit => %s)",
            (DEEP_GROUP,),
        )
        cur.execute(
            "INSERT INTO retire_deep "
            f"SELECT g, g % 7, g % 13, 'x' || g FROM generate_series(1,{DEEP_ROWS}) g"
        )

    db, sb, pages_b, margin_b = _phase(
        conn, expect, "B", "retB", BIG_GROUPS, READ_WHOLE, "read-whole", carried=7
    )

    # THE PREMISE THIS EXPERIMENT NEEDS MOST. The growth arm compares two
    # readings taken over catalogs that are supposed to differ in size. If the
    # deep table never landed, both phases measure the same fixture and the arm
    # passes while proving nothing -- and that is not hypothetical: the sweep
    # that chose the shipped default first produced a clean table across seven
    # database sizes in which the noise had been eaten by shell quoting. Every
    # row was secretly the same database, and the only thing that said so was
    # this quantity, flat where it should have grown eightfold.
    expect.at_least(
        pages_b - pages_a,
        1,
        "premise: the deep table grew the catalogs it is there to grow",
    )
    expect.at_least(
        margin_b,
        FLOOR_PERMILLE,
        "B1 with large catalogs the default does far less work than reading every one whole",
    )

    # THE INVARIANT THE ISSUE IS ABOUT, written down as its own arm rather than
    # left for a reader to compose out of A1 and B1. It is the sentence the bug
    # report would use: retiring a group must not cost more because other tables
    # exist.
    growth_default = db - da
    growth_scan = sb - sa
    growth_margin = _permille(growth_scan - growth_default, growth_scan)
    print(f"-- growth from phase A to phase B: default={growth_default} "
          f"read-whole={growth_scan} permille={growth_margin}")
    expect.at_least(
        growth_margin,
        FLOOR_PERMILLE,
        "the default's cost grows far less with the database than reading whole does",
    )


def test_vacuum_reads_less_of_row_group_than_reading_it_whole(pgc_own_db, expect):
    """The vacuum path reaches a row_group read the compaction path does not.

    `PgColumnarVMSetVisibleForRelation` calls
    `PgColumnarComputeAllVisibleGroups`, and nothing in the test above reaches
    it. Probing every scan site during a compaction shows that function never
    fires, so without this test a change to it would ride along on arms that
    could not fail if it were reverted.

    NO POSITIONAL CONFOUND, unlike every arm in the test above, because a VACUUM
    is repeatable where a compaction is not. All three readings come from ONE
    table and the only thing that differs is the setting.

    AND IT IS VACUUMED SMALL, ON PURPOSE. The size check is worth the difference
    between reading `row_group` whole and fetching the vacuumed table's own rows
    from it, so the gap widens as the catalog grows and narrows as the VACUUMED
    table grows. An earlier draft vacuumed a thirty-group table and the arm swung
    between 200 and 750 parts per thousand from run to run on a base of ten
    buffers.

    The premise reads `delete_vector`, a DIFFERENT catalog from the one the arm
    is about, so it cannot be satisfied by whatever makes it pass.
    `relallvisible` is the obvious premise and is the wrong quantity: it stays 0
    on this fixture however many times the table is vacuumed. So do
    `vacuum_count` and `last_vacuum`, which this table access method's vacuum
    does not report through at all.
    """
    conn = pgc_own_db
    assertions, min_blocks = _build_kind(conn)
    print(f"-- debug_assertions={assertions} index_min_blocks={min_blocks}")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE vac_deep (k bigint, a int, b int, c text) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('vac_deep', stripe_row_limit => %s)",
            (DEEP_GROUP,),
        )
        cur.execute(
            "INSERT INTO vac_deep "
            f"SELECT g, g % 7, g % 13, 'x' || g FROM generate_series(1,{VAC_DEEP_ROWS}) g"
        )
        cur.execute("CREATE TABLE vac_small (k bigint) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('vac_small', stripe_row_limit => %s)", (GROUP,)
        )
        cur.execute("INSERT INTO vac_small SELECT g FROM generate_series(1,3000) g")
        cur.execute("DELETE FROM vac_small WHERE k % 3 = 0")

        cur.execute("SELECT count(*) FROM vac_small")
        expect.at_least(cur.fetchone()[0], 1, "premise: the vacuumed table holds rows")
        cur.execute(
            "SELECT pg_relation_size('pgcolumnar.row_group') / 8192"
        )
        rg_pages = int(cur.fetchone()[0])
    print(f"-- row_group pages={rg_pages}, threshold={min_blocks}")
    # THE PREMISE THE ARM BELOW CANNOT DO WITHOUT, and it is derived from the
    # setting rather than typed. Below the threshold the default declines the
    # probe and reads `row_group` whole -- which is what the other reading does
    # too, so both come back equal and the arm reports 0. That reads as "the fix
    # is gone" and means "the fixture is too small".
    expect.at_least(
        rg_pages - int(min_blocks),
        1,
        "premise: row_group is larger than the threshold, so the two paths differ",
    )

    def vacuum_work(min_blocks=None):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_stat_force_next_flush()")
            cur.execute("SELECT pg_stat_reset()")
            if min_blocks is not None:
                cur.execute(f"SET pgcolumnar.index_min_blocks = {min_blocks}")
            cur.execute("VACUUM vac_small")
            cur.execute("RESET pgcolumnar.index_min_blocks")
            cur.execute("SELECT pg_stat_force_next_flush()")
        return _work(conn)

    w_default = vacuum_work()
    w_control = vacuum_work()
    w_scan = vacuum_work(READ_WHOLE)
    v_default = w_default.get("row_group", 0)
    v_control = w_control.get("row_group", 0)
    v_scan = w_scan.get("row_group", 0)
    noise = _permille(abs(v_control - v_default), v_default)
    margin = _permille(v_scan - v_default, v_default)
    print(f"-- VACUUM row_group work: default={v_default} control={v_control} "
          f"read-whole={v_scan} permille={margin} noise={noise}")

    expect.at_least(
        w_default.get("delete_vector", 0),
        1,
        "premise: the vacuum walked this table's groups",
    )
    expect.at_least(
        FLOOR_PERMILLE - noise,
        1,
        "premise: two vacuums of the same table at the same setting agree well inside the floor",
    )
    expect.at_least(
        margin,
        FLOOR_PERMILLE,
        "the vacuum's default does less row_group work than reading it whole",
    )


# ---- the DROP path: delete_rows_by_storage_id, seven catalogs (#1207) -------
#
# The shell twin's section explains the subject: everything above drives
# `delete_group_rows` (columnar_metadata.c:745), the retire path, while the
# seven-catalog sweep is `delete_rows_by_storage_id` (:1919), reached only from
# PgColumnarDeleteMetadata on DROP and TRUNCATE. Neither half covered it.
#
# MEASURED HERE, NOT BORROWED. This file opens its own database, builds its own
# fixture and reads pg_statio itself. Parallel in what it asserts, independent
# in what it calls -- lifting the shell helpers so both could drive them would
# make the extraction the dependency.
DROP_CATALOGS = tuple(CATALOGS) + ("storage",)


def _drop_work(conn, table, min_blocks=None):
    """Buffers the seven catalogs served while `table` was dropped.

    A DROP cannot be repeated, so each reading needs its own identically built
    table.
    """
    with conn.cursor() as cur:
        # FLUSH BEFORE THE RESET, for the reason _compact_work records: this
        # harness holds ONE connection, so the writes that built the fixture
        # leave pending statistics that pg_stat_reset() does not clear and that
        # land on top of the reading. Omitting it here read -988 permille --
        # the first DROP absorbing the whole fixture build -- against +125 for
        # the shell twin, which gets a fresh backend per statement.
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT pg_stat_reset()")
        if min_blocks is not None:
            cur.execute(f"SET pgcolumnar.index_min_blocks = {min_blocks}")
        cur.execute(f"DROP TABLE {table}")
        cur.execute("RESET pgcolumnar.index_min_blocks")
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute(
            "SELECT coalesce(sum("
            "  coalesce(heap_blks_read,0) + coalesce(heap_blks_hit,0) "
            "+ coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0)), 0) "
            "FROM pg_statio_all_tables "
            "WHERE schemaname = 'pgcolumnar' AND relname = ANY(%s)",
            (list(DROP_CATALOGS),),
        )
        return int(cur.fetchone()[0])


def test_dropping_a_table_reads_less_of_the_catalogs_than_reading_them_whole(
    pgc_own_db, expect
):
    """The DROP sweep must use the index once the catalogs are worth one.

    WORK, NOT THE ACCESS PATH, for the reason in this file's header: an arm
    asserting `seq_scan = 0` fails against a build that makes the drop cheaper
    some other way. And an arm asserting only that the rows were deleted passes
    with InvalidOid, because a sequential scan deletes them just as correctly --
    that is the vacuous version this test exists instead of.
    """
    conn = pgc_own_db
    # MORE NEIGHBOURS THAN THE SHELL TWIN NEEDS, and the reason is the fixture
    # rather than the property. `pgc_own_db` gives this file a private database,
    # so the catalogs hold only what this test builds -- where the shell twin
    # shares a cluster with everything before it. At 24 fill tables the margin
    # read 53 permille here against 901 there, both correct measurements of
    # different databases.
    for i in range(1, 61):
        _make_target(conn, f"drp_fill_{i}", 6)
    for t in ("drp_default", "drp_whole"):
        _make_target(conn, t, 6)

    expect.at_least(
        _catpages(conn), 3,
        "premise: the drop fixture grew the catalogs it is there to grow",
    )
    expect.at_least(
        _groups_of(conn, "drp_default"), 1,
        "premise: the table about to be dropped owns catalog rows",
    )

    d_default = _drop_work(conn, "drp_default")
    d_whole = _drop_work(conn, "drp_whole", 2147483647)
    print(f"-- drop  catalog pages={_catpages(conn)} "
          f"default={d_default} read-whole={d_whole}")
    expect.at_least(
        _permille(d_whole - d_default, d_default), FLOOR_PERMILLE,
        "the drop's default does less catalog work than reading them whole",
    )

    # THE OTHER SIDE. Below the threshold the default declines the probe, so
    # forcing one must cost MORE. A `<=` form would pass on a build with the
    # size check removed -- the two readings are then equal -- which is the same
    # vacuity as asserting only that the rows were deleted.
    for t in ("drp_small_a", "drp_small_b"):
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {t} (id int) USING pgcolumnar")
            cur.execute(f"INSERT INTO {t} SELECT g FROM generate_series(1,50) g")
    s_default = _drop_work(conn, "drp_small_a")
    s_probe = _drop_work(conn, "drp_small_b", 0)
    print(f"-- drop small  default={s_default} probe-always={s_probe}")
    expect.at_least(
        _permille(s_probe - s_default, s_default), FLOOR_PERMILLE,
        "with few catalog pages the drop's default does less work than probing every one",
    )

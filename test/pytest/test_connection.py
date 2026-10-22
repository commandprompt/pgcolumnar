"""The cluster fixture and the direct connection, tested before they exist.

The requirement is a direct typed connection. These tests assert on Python types
coming back from the server, which is the thing `psql -At` cannot give: it returns
the string "100" and leaves every conversion to the reader.
"""

import decimal
import pathlib
import signal

import pgc_vacuity


def test_cluster_fixture_gives_a_typed_connection(pgc_conn, expect):
    """count(*) must arrive as an int, not as text."""
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM (SELECT generate_series(1,7)) s")
        value = cur.fetchone()[0]
    expect.num(value, 7, "count arrives as a number")
    expect.text(type(value).__name__, "int", "and its Python type is int")


def test_the_extension_is_installed_and_columnar(pgc_conn, expect):
    """The loaded extension is the version THIS CHECKOUT declares.

    The expected version is read from `pgcolumnar.control` rather than written
    here. A hardcoded "1.0-alpha3" is wrong the moment a release cycle opens, and
    on 2026-09-09 it became wrong: #899 moved the tree to 1.0-alpha4 and this arm
    would have failed for a reason that has nothing to do with what it tests.

    It is also the wrong ASSERTION. A constant tests that the extension is a
    particular version; reading the control file tests that the extension is the
    one this source tree describes, which is the property the arm is named after
    and the one that catches a foreign install.
    """
    from pgc_cluster import control_default_version

    srcdir = pathlib.Path(__file__).resolve().parents[2]
    want = control_default_version((srcdir / "pgcolumnar.control").read_text())
    expect.text(bool(want), True,
                "PREMISE the checkout declares a version to compare against")

    with pgc_conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'pgcolumnar'")
        row = cur.fetchone()
    expect.rows([row[0]] if row else [], [want],
                f"the loaded extension is this tree's {want}")
    # AND COLUMNAR (#1339): the name says so, and the version alone does not. A table
    # created USING pgcolumnar must carry that access method.
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE amprobe (id int) USING pgcolumnar")
        cur.execute("SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
                    "WHERE c.oid = 'amprobe'::regclass")
        am = cur.fetchone()
    expect.text(am[0] if am else "no access method", "pgcolumnar",
                "and a table created USING pgcolumnar is a columnar table")


def test_a_columnar_table_round_trips_with_real_types(pgc_conn, expect):
    """Typed results end to end, including the types psql flattens to text."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE t (id int, n numeric, f float8, b bytea, a int[]) USING pgcolumnar")
        cur.execute("INSERT INTO t VALUES (1, 1.5, 2.5, '\\x00ff', ARRAY[1,2])")
        cur.execute("SELECT id, n, f, b, a FROM t")
        row = cur.fetchone()
    expect.num(row[0], 1, "int column")
    expect.text(type(row[1]).__name__, "Decimal", "numeric arrives as Decimal")
    expect.num(row[1], decimal.Decimal("1.5"), "and holds its exact value")
    expect.text(type(row[2]).__name__, "float", "float8 arrives as float")
    expect.text(row[3].hex(), "00ff", "bytea arrives as bytes")
    expect.rows(row[4], [1, 2], "array arrives as a list")


def _plan(conn, sql, gucs=()):
    with conn.cursor() as cur:
        for g in gucs:
            cur.execute(g)
        cur.execute(f"EXPLAIN (FORMAT JSON, COSTS OFF) {sql}")
        return cur.fetchone()[0]


def test_the_plan_shows_a_columnar_scan(pgc_conn, expect):
    """EXPLAIN FORMAT JSON arrives parsed, and the SCAN is identified by its marker.

    By the marker, not by the provider name. See the next test for why.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE p (id int, a int) USING pgcolumnar")
        cur.execute("INSERT INTO p SELECT g, g%10 FROM generate_series(1,500) g")
    plan = _plan(pgc_conn, "SELECT count(*) FROM p WHERE a > 5")
    expect.text(type(plan).__name__, "list", "the plan arrives as parsed Python")
    expect.plan_marker(plan, "Columnar Projected Columns",
                       name="the columnar scan ran")


def test_the_provider_name_does_not_identify_a_scan(pgc_conn, expect):
    """Pin the trap: provider equality is WIDER than pgc_is_columnar_scan.

    Measured. `columnar_vector.c:806` assigns the aggregate node
    `&pgcolumnar_scan_methods`, whose CustomName is `PgColumnarScan`, so every
    pgcolumnar node reports that provider. With the ungrouped vector aggregate
    engaged the plan is a SINGLE Custom Scan node carrying `Columnar Vectorized
    Aggregates` and NO `Columnar Projected Columns` -- the aggregate absorbed the
    scan. A test asserting the provider would say "there is a columnar scan" about
    a plan that has none, which is what `pgc_is_columnar_scan` refuses to say.

    This test exists so that reverting to the provider predicate reddens here.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE vp (id int, a int) USING pgcolumnar")
        cur.execute("INSERT INTO vp SELECT g, g%100 FROM generate_series(1,20000) g")
        cur.execute("ANALYZE vp")
    plan = _plan(pgc_conn, "SELECT count(*) FROM vp",
                 ("SET pgcolumnar.enable_vectorization = on",
                  "SET pgcolumnar.enable_ungrouped_vector_agg = on"))

    # The premise: the vectorized aggregate must actually have engaged, or the rest
    # of this test is about an ordinary plan and proves nothing.
    # Not a premise (#1339): pgcolumnar's planner engages it, and a source change that
    # stops it planning the ungrouped aggregate reddens this.
    expect.plan_marker(plan, "Columnar Vectorized Aggregates",
                       name="the vector aggregate engaged")
    # The provider still matches, which is the trap.
    expect.plan_node(plan, provider="PgColumnarScan",
                     name="the provider matches even with no scan node")
    # And the scan marker is absent, which is what makes the provider wrong here.
    expect.plan_marker(plan, "Columnar Projected Columns", absent=True,
                       name="but no columnar SCAN marker is present")


def test_each_test_gets_its_own_schema(pgc_conn, expect):
    """Isolation without a cluster per test: a private schema, first on the path."""
    with pgc_conn.cursor() as cur:
        cur.execute("SHOW search_path")
        path = cur.fetchone()[0]
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
    # PRIVATE MEANS NOTHING OF ANOTHER TEST'S IS THERE (#1339). The name prefix alone held
    # for one schema every test shared, and the tests above this one create t and vp.
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = current_schema()")
        others = cur.fetchone()[0]
    expect.text("pgc_test_ and empty" if schema.startswith("pgc_test_") and others == 0
                else f"{schema} holding {others} relations", "pgc_test_ and empty",
                "the schema is test-private")
    expect.text(path.split(",")[0].strip().startswith("pgc_test_"), True,
                "and it is first on the search path")



def test_a_killed_run_can_still_reach_its_teardown(pgc_cluster, expect):
    """SIGTERM must reach the fixture's `finally`, or a killed run leaks its
    cluster (#1170).

    Python does not run `finally` blocks when the DEFAULT SIGTERM disposition
    terminates the process, so the teardown was unreachable for every interrupted
    run -- and interrupting a run is normal. Measured before the handler existed:
    21 orphaned postmasters and ~2.5 GB of datadirs across two containers, the
    oldest 33 hours.

    THIS ASSERTS THE WIRING, NOT THE HELPER. Calling `_raise_system_exit`
    directly proves only that a two-line function raises; what was broken is that
    nothing installed it. So the arm reads the disposition that is actually in
    effect while a cluster is alive, which is the state a TERM would arrive in.

    It cannot deliver a real SIGTERM to its own process without ending the run,
    so the end-to-end proof lives in the commit: a run killed with TERM leaves 0
    postmasters and no datadir with the handler, and 1 postmaster with the
    datadir still on disk without it. SIGKILL is uncatchable and still leaks,
    which is recorded rather than fixed.
    """
    import conftest

    handler = signal.getsignal(signal.SIGTERM)
    expect.text(getattr(handler, "__name__", repr(handler)), "_raise_system_exit",
                "a SIGTERM arriving during a run reaches a handler, not the default")
    expect.text(repr(handler is conftest._raise_system_exit), "True",
                "and it is the conftest handler, not some other module's")

    # The exit status a shell reports for a signalled process, so `$?` reads 143
    # whichever way the run ended and nothing downstream learns a new number.
    raised = None
    try:
        handler(signal.SIGTERM, None)
    except SystemExit as exc:
        raised = exc.code
    expect.num(raised if raised is not None else -1, 143,
               "and it raises SystemExit(143), which is what runs the finally")

def test_the_worker_owns_its_own_cluster(pgc_cluster, expect):
    """Under xdist each worker must own a cluster, not share one.

    Two workers installing into one pkglibdir race, and a shared cluster lets one
    test see another's tables.

    This used to assert `port == PORT_BASE + slot`, an injective formula, so that
    distinctness was a property rather than a hope. The formula is gone: the
    harness now WALKS to a free port, because a fixed base is only a claim about
    probability and this harness was broken by exactly that -- 54600 sat inside
    the kernel's ephemeral range and something else was holding it
    (@jdatcmd, #897 review).

    So the assertions moved to the properties that survive a walk, starting with
    the one that was actually false. `port < ephemeral floor` is the invariant
    the old constant violated, and it is checked against the floor READ from the
    kernel rather than a number written here.

    Distinctness is now held by the walk starting each worker at its own offset
    plus `is_ours()`, which fails loudly if the server answering is not the one
    this worker started. A guard that says NO is what makes the yes mean
    something; the arm below drives that guard to False on purpose.
    """
    from pgc_cluster import aux_band, read_ephemeral_floor

    floor = read_ephemeral_floor()
    lo, hi = aux_band(floor)
    wid = pgc_cluster.worker_id

    expect.at_least(pgc_cluster.port, lo,
                    f"worker {wid} sits at or above the AUX band floor {lo}")
    expect.at_least(hi - pgc_cluster.port, 1,
                    f"and below the AUX band ceiling {hi}")
    expect.at_least(floor - pgc_cluster.port, 1,
                    f"and below the kernel's ephemeral floor {floor}, which is "
                    "the invariant the old constant broke")
    expect.text(pgc_cluster.is_ours(), True,
                "and the server answering there runs from our datadir")
    # AND THE DATADIR IS THIS WORKER'S (#1339). Ports and is_ours() hold for a cluster
    # two workers shared; make_cluster roots each worker's tree at pgc-pytest-<slot>-.
    slot = 0 if wid in (None, "master") else int(str(wid).lstrip("gw") or 0)
    expect.text("yes" if pgc_cluster.datadir.parent.name.startswith(f"pgc-pytest-{slot}-")
                else f"no ({pgc_cluster.datadir})", "yes",
                f"and its data directory was made for worker {wid}")


def test_the_cluster_refuses_a_foreign_server(pgc_cluster, expect):
    """The identity guard must be able to say NO, not just say yes.

    pg_ctl -w proves only that SOMETHING answers on the port. lib.sh added this
    check because a foreign cluster answering would let every later assertion run
    against the wrong server. A guard that has never returned False is not known to
    work, so this points it at a datadir that is not ours and requires a False.
    """
    import pathlib

    from pgc_cluster import Cluster

    impostor = Cluster(pgc_cluster.pg_config, pgc_cluster.worker_id,
                       pathlib.Path("/tmp/definitely-not-our-datadir"),
                       pgc_cluster.port)
    expect.text(impostor.is_ours(), False,
                "a server whose datadir differs is refused")

def test_the_connection_the_tests_use_is_watched(pgc_conn, expect, request):
    """Writes through this fixture reach the zero-row guard.

    THIS IS THE HALF THE DRIVER-FREE ARMS CANNOT PROVE. test_writes_wrote_rows.py
    exercises the classifier and the refusal against stub cursors, which says
    nothing about whether the connection the tests actually use is wrapped at all.
    Proving a function and proving its call site are two proofs, and the second is
    the one that goes missing: #917's pytest twin tested the reconciler's body and
    left the runner's CALL to it uncovered, so deleting the call kept that half
    green while the shell half went red.

    BOTH PATHS, because the corpus uses both. 24 sites call `conn.execute` and 42
    call `cur.execute` on a cursor the connection handed out, so a proxy that
    watched only the connection would leave most of the corpus unwatched.
    """
    writes = pgc_vacuity._WRITES.setdefault(request.node.nodeid, [])
    writes.clear()

    pgc_conn.execute("CREATE TABLE watched (i int) USING pgcolumnar")
    expect.num(len(writes), 0, "DDL carries no row count, so it is not a write")

    cur = pgc_conn.execute("INSERT INTO watched SELECT g FROM generate_series(1,3) g")
    expect.num(len(writes), 1, "a write through conn.execute is seen")
    expect.num(writes[-1].count, 3, "with the count the server reported")
    expect.text(writes[-1].tag, "INSERT", "and the command tag it reported")
    expect.wrote(cur, 3, "and expect.wrote reads the same count back")

    with pgc_conn.cursor() as c:
        c.execute("INSERT INTO watched SELECT g FROM generate_series(1,2) g")
        expect.num(len(writes), 2, "a write through a handed-out cursor is seen too")
        expect.wrote(c, 2, "and its count is the one the server reported")

    # A zero-row write through the real driver, which is the mode itself. Naming the
    # zero is what keeps this test passing; without the name the guard would fail it,
    # and that refusal is pinned in test_writes_wrote_rows.py where it can be caught.
    empty = pgc_conn.execute("INSERT INTO watched SELECT g FROM generate_series(1,3) g "
                             "WHERE false")
    expect.num(len(writes), 3, "the empty write is recorded like any other")
    expect.wrote(empty, 0, "and INSERT ... WHERE false wrote no rows, deliberately")

def test_the_acknowledgement_is_by_cursor_against_the_real_driver(pgc_conn, expect,
                                                                  request):
    """The stamp must land on the object the CALLER holds, not the one psycopg owns.

    THE DRIVER-FREE ARMS CANNOT SEE THIS, and that is why it belongs here. They stamp
    a stub, and a stub accepts a new attribute; a real `psycopg.Cursor` raises
    AttributeError, so the stamp was swallowed by its own `except` on every real
    write and `wrote()` silently fell back to matching on `(tag, count)`. The arms
    proved the identity mechanism on an object that differs from the real one in
    exactly the respect under test -- which is the hazard the `_WatchedCursor`
    docstring names, arriving in the arms that were supposed to guard it. Found by
    @jdatcmd on #432.

    It matters because the two fixes rest on each other: the absolute ordinal is only
    trustworthy when the acknowledgement is by identity, so with the stamp swallowed
    the refusal named the wrong statement in precisely the case the ordinal was added
    for.

    TWO INDISTINGUISHABLE WRITES, because that is the only case where identity and
    value matching can disagree. Both are named, so the test passes; the assertion is
    about WHICH one each call acknowledged.
    """
    writes = pgc_vacuity._WRITES.setdefault(request.node.nodeid, [])
    writes.clear()

    pgc_conn.execute("CREATE TABLE twin (i int) USING pgcolumnar")
    first = pgc_conn.execute("INSERT INTO twin SELECT 1 WHERE false")
    second = pgc_conn.execute("INSERT INTO twin SELECT 1 WHERE false")
    expect.num(len(writes), 2, "two indistinguishable zero-row writes are recorded")
    expect.text(f"{writes[0].tag}/{writes[0].count} {writes[1].tag}/{writes[1].count}",
                "INSERT/0 INSERT/0", "premise: the two are indistinguishable by value")

    expect.text(type(getattr(second, "_pgc_write", None)).__name__, "_Write",
                "the cursor the caller received carries the write it ran")
    expect.num(getattr(second, "_pgc_write").ordinal, 2,
               "and it is the SECOND write, by its absolute ordinal")

    expect.wrote(second, 0, "naming the second write")
    expect.num(int(writes[1].acknowledged), 1, "acknowledges the second")
    expect.num(int(writes[0].acknowledged), 0, "and leaves the first unnamed")

    expect.wrote(first, 0, "naming the first as well, so nothing is left unnamed")
    expect.num(int(writes[0].acknowledged), 1, "which acknowledges the first")

    # BOTH STAMP SITES, because there are two and one probe pins neither. The writes
    # above went through `conn.execute`; a cursor the connection hands out is a second
    # path with its own stamp, and removing that one alone left this arm green --
    # found by mutating it, not by reading it. The existing wiring arm uses the cursor
    # path but with DISTINGUISHABLE counts, so value matching finds the right write
    # there whether or not the stamp lands.
    writes.clear()
    with pgc_conn.cursor() as cur:
        cur.execute("INSERT INTO twin SELECT 1 WHERE false")
        expect.num(len(writes), 1, "a cursor-path write is recorded")
        expect.text(type(getattr(cur, "_pgc_write", None)).__name__, "_Write",
                    "and the handed-out cursor carries the write it ran")
        cur.execute("INSERT INTO twin SELECT 1 WHERE false")
        expect.num(len(writes), 2, "and so is its indistinguishable twin")
        expect.num(getattr(cur, "_pgc_write").ordinal, 2,
                   "the cursor now carries the SECOND write, not the first")
        expect.wrote(cur, 0, "naming what the cursor last ran")
        expect.num(int(writes[1].acknowledged), 1, "acknowledges the second write")
        expect.num(int(writes[0].acknowledged), 0, "and leaves the first unnamed")
        expect.wrote(first, 0, "so name the first explicitly too")


def test_setting_autocommit_on_the_connection_the_tests_use_is_refused(pgc_conn, expect):
    """The wiring arm for #1420: the stub arms in test_writes_wrote_rows.py prove the
    wrapper refuses, and this proves `pgc_conn` IS that wrapper, against the real
    driver. Before the fix the assignment succeeded on the wrapper and the server
    went on committing every statement.
    """
    try:
        pgc_conn.autocommit = False
        outcome = "set"
    except AttributeError as exc:
        outcome = f"refused: {exc}"
    expect.text(outcome.split(":")[0], "refused",
                "setting autocommit on pgc_conn is refused, not silently ignored")
    expect.text(str(pgc_conn.autocommit), "True", "and pgc_conn is still in autocommit")


def test_the_block_compression_default_is_pinned_to_its_measurement(pgc_conn, expect):
    """#890: zstd:3 stays the default until a measurement says otherwise.

    The roadmap proposed making this default depend on a storage tier, on the
    premise that block compression trades CPU for I/O. Phase 1 refuted the premise
    ON THIS ENGINE: our cascade runs lightweight encodings first, so zstd works on
    already-reduced bytes and its remaining cost is small against the memory traffic
    it removes. Measured on PG 17.10, 400k rows, minimum of 12, arms round-robined,
    and the none/zstd ranges do not overlap on any shape:

        shape   marginal bytes saved   scan time vs none
        rep                   +76.6%   0.92   (faster)
        rand                   -1.7%   1.14   (slower -- incompressible)
        mix                   +12.0%   0.92   (faster)

    Warm cache, which understates the saving ON THE SHAPES THAT COMPRESS: no
    physical read happens on either arm, so a cold run adds an I/O term proportional
    to bytes read and `rep` and `mix` can only widen.

    NOT SO ON `rand`, AND THE DIFFERENCE MATTERS. There zstd writes 148,076 MORE
    bytes than none, so cold makes it worse on both terms at once -- more bytes to
    read AND the same decompression CPU -- and C(cold) > 1.14. An earlier version of
    this comment said a cold measurement "can only move further toward compression",
    full stop. That is true only where compression reduces bytes, and stating it
    unconditionally gave away the stronger argument: the incompressible shape
    degrading cold is a SECOND, independent reason for the per-table override rather
    than a global change. Reported by @OffgridwithJD.

    This arm is a pin, not a discovery. It exists so that changing the default is a
    deliberate act with a new measurement attached, rather than a line edit nobody
    has to defend. Incompressible data is a real cost and the answer there is the
    per-table override, not a different global default.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("SHOW pgcolumnar.compression")
        method = cur.fetchone()[0]
        cur.execute("SHOW pgcolumnar.compression_level")
        level = cur.fetchone()[0]
    expect.text(method, "zstd", "the block compression default is zstd (#890 phase 1)")
    expect.num(int(level), 3, "and its level is 3")

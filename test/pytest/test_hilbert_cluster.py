"""Port of test/hilbert_cluster.sh: the Hilbert clustering SQL surface (#889, #432).

WHAT THIS PORTS, AND WHAT IT DELIBERATELY DOES NOT. The bash suite pins the SQL
surface of `pgcolumnar.cluster_hilbert` and `recluster_hilbert`, the recorded
`sorted_kind`, the self-gate and the daemon. `test/hilbert_curve.sh` pins the
CURVE itself in C, and neither file re-tests the other. This port keeps that
division: nothing here compares a verb's output against the pinned encoder,
because there is no SQL exposure of `cluster_hilbert_transpose` to compare
against.

THE ONE PLACE THIS PORT ASSERTS THE PROPERTY MORE DIRECTLY THAN THE ORIGINAL.
The bash `sqlstate()` runs psql with a SENTINEL statement behind the one under
test, because psql cannot otherwise distinguish "the statement raised no error"
from "the probe never reached the server" -- both produce no ERROR line, and a
probe that could not connect therefore read as success. That is a limitation of
reading a subprocess's text, not a property of PostgreSQL. psycopg separates the
two at the type level: a connection that cannot be opened raises
`OperationalError` from `connect()`, and a statement that raises carries
`.sqlstate` on the exception. So `_sqlstate` below returns PROBE_UNREACHABLE from
the connect failure itself rather than from the absence of a sentinel in stdout.

THE PROPERTY IS UNCHANGED AND BOTH CONTROLS ARE KEPT. "noerror means the server
answered" still needs proving, so the NOLOGIN control is ported as-is: a role
that cannot open a session must produce PROBE_UNREACHABLE and not noerror. A
port that dropped it because "psycopg makes that impossible" would be asserting
its own implementation rather than the property.

SQLSTATE, NEVER MESSAGE TEXT, for the reason the bash header gives: a grep for
"permission denied" is also satisfied by a login FATAL, a missing function
(42883), a bad argument (22023) or a transaction-block refusal (25001). Only
aclcheck_error produces 42501.
"""

import re

import pytest

# The row GROUP is the stripe, so stripe_row_limit decides how many groups a
# fixture has. Both values are at set_options' floor (stripe >= 1000,
# chunk_group >= 100); a value below it RAISES, and a suite that discards the
# error then measures a fixture built on the defaults. Every fixture generator
# ASSERTS its group count rather than assuming it from the call.
SR = 1000
CG = 500

# S1's four inputs, held in one place so each new verb and its established
# sibling are given byte-identical arguments. Two hand-written call sites is how
# a "same input" comparison stops being one.
ARGS_OK = "'sur','c1','c2'"
ARGS_TEXT = "'sur','txt'"
ARGS_NINE = "'sur','c1','c2','c3','c4','c5','c6','c7','c8','c9'"
# Zero columns is spelled with an explicit empty array. Bare
# pgcolumnar.cluster('sur') does not resolve at all -- 42883, not 22023 -- so it
# would test PostgreSQL's function resolution rather than the verb's own
# argument check, and the two new verbs would "agree" with the old ones about an
# error neither of them raised.
ARGS_ZERO = "'sur', VARIADIC ARRAY[]::name[]"

#   42501  aclcheck_error(ACLCHECK_NOT_OWNER)
#   0A000  ERRCODE_FEATURE_NOT_SUPPORTED, an unsupported clustering type
#   22023  ERRCODE_INVALID_PARAMETER_VALUE, too many / no clustering columns
S1_CASES = [
    ("non-owner", "h_other", ARGS_OK, "42501"),
    ("a text clustering column", "postgres", ARGS_TEXT, "0A000"),
    ("nine clustering columns", "postgres", ARGS_NINE, "22023"),
    ("zero clustering columns", "postgres", ARGS_ZERO, "22023"),
]
S1_PAIRS = [("cluster", "cluster_hilbert"), ("recluster", "recluster_hilbert")]

UNREACHABLE = "PROBE_UNREACHABLE"
NOERROR = "noerror"

# The port's fixture lives in its own schema; the bash suite's lives in `public`.
# That difference is not cosmetic for S1 -- see `_sqlstate` and the schema-USAGE
# premise beside the EXECUTE one.
SCHEMA = "pgc_hilbert_cluster"


def _sqlstate(cluster, role, sql):
    """-> the five-character SQLSTATE, `noerror`, or PROBE_UNREACHABLE.

    THE THREE OUTCOMES ARE DISTINGUISHED BY TYPE, not by scraping text. A
    session that cannot be opened raises OperationalError from connect(); a
    statement that raises carries .sqlstate; anything else answered. The bash
    original needs a sentinel statement to tell the first from the third, and
    that sentinel is the reason this function exists at all -- see the module
    docstring.

    A raised exception with NO sqlstate is reported as PROBE_UNREACHABLE rather
    than as an error state, because a state that cannot be read is not evidence
    about which check refused.
    """
    import psycopg

    # THE SEARCH PATH IS SET IN THE CONNECTION OPTIONS, NOT AS A STATEMENT. The
    # probe must see the fixture schema or every call raises 42P01 and the arms
    # measure name resolution; and a `SET` issued afterwards is one more
    # statement that can fail without the probe noticing.
    try:
        conn = psycopg.connect(cluster.dsn(), user=role, autocommit=True,
                               options=f"-c search_path={SCHEMA},public")
    except psycopg.OperationalError:
        return UNREACHABLE
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(sql)
            except psycopg.Error as exc:
                return exc.sqlstate or UNREACHABLE
        return NOERROR
    finally:
        conn.close()


def _scalar(conn, sql):
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


@pytest.fixture(scope="module")
def hc(pgc_cluster):
    """The module's one connection, its schema, and the settings every arm needs.

    MODULE SCOPE for the reason `test_hilbert_locality.py` states: the corpus is
    built once, and the connection carries settings the arms depend on. Each arm
    builds its own tables on it.
    """
    import psycopg

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    conn.execute(f'CREATE SCHEMA "{SCHEMA}"')
    conn.execute(f'SET search_path TO "{SCHEMA}", public')
    # Every digest in this module is a claim about PHYSICAL layout, and a
    # parallel plan interleaves workers' output independently of the layout, so
    # a parallel scan would make the digest a claim about scheduling.
    conn.execute("SET max_parallel_workers_per_gather = 0")
    yield conn
    conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    conn.close()


@pytest.fixture(scope="module")
def surface(hc):
    """S1's fixture: one columnar table and the two roles the refusals need.

    MODULE SCOPE for the reason `test_hilbert_locality.py` states: the corpus is
    built once and the connection carries settings every arm depends on. Roles
    are CLUSTER-GLOBAL rather than schema-local, so they are dropped on the way
    in as well as out -- a leftover `h_other` that owned objects would be reused
    with whatever grants it had, and the premise below asserts it owns nothing
    rather than trusting that lib.sh's fresh initdb makes it impossible.
    """
    conn = hc
    schema = SCHEMA
    try:
        conn.execute(
            "CREATE TABLE sur (c1 int, c2 int, c3 int, c4 int, c5 int,"
            "                  c6 int, c7 int, c8 int, c9 int, txt text)"
            " USING pgcolumnar")
        conn.execute(
            "INSERT INTO sur SELECT g,g,g,g,g,g,g,g,g,'t'||g"
            " FROM generate_series(1,2000) g")

        for role, opts in (("h_other", "NOSUPERUSER LOGIN"),
                           ("h_nologin", "NOSUPERUSER NOLOGIN")):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} {opts}")
        conn.execute("GRANT USAGE ON SCHEMA pgcolumnar TO h_other")
        # AND USAGE ON THE FIXTURE'S OWN SCHEMA, which the bash suite does not
        # need because its fixture sits in `public`. Without it the probe's
        # 42501 would be the SCHEMA check rather than the owner check -- the same
        # state from a different refusal, satisfying the arm for the wrong
        # reason. The premise below asserts the grant took, so this cannot
        # silently become the thing being measured.
        conn.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO h_other')

        yield conn
    finally:
        # NOTHING IS SWALLOWED HERE. The obvious teardown wraps each DROP in
        # `except Exception: pass`, and the vacuity layer refuses that on sight
        # -- correctly: a role that cannot be dropped because it still OWNS
        # something is the leftover this fixture's own premise asserts against,
        # and hiding it would let the next run reuse the role with whatever
        # grants it had. The schema is dropped BEFORE the roles because a role
        # cannot be dropped while it owns objects in it, which is the ordering
        # that makes the un-caught DROP ROLE safe rather than the try/except.
        conn.execute("DROP TABLE IF EXISTS sur")
        for role in ("h_other", "h_nologin"):
            conn.execute(f"DROP OWNED BY {role}")
            conn.execute(f"DROP ROLE {role}")


# =============================================================================
# S0  THE PREMISES EVERY DIGEST BELOW STANDS ON
# =============================================================================
#
# The bash suite gets these from `pgc_check_ordered_oracle` in lib.sh plus one
# check of its own. The three oracle premises are INVISIBLE to
# `compare_to_bash.py` -- it reads only the suite file, and their names live in
# `lib.sh` -- so porting them buys no parity credit and is done because the arms
# below need them: without an order-SENSITIVE digest, S5's identity arm passes on
# two tables nobody reordered.


def test_the_two_digests_are_the_instruments_this_file_thinks_they_are(hc, expect):
    """The port of `pgc_check_ordered_oracle`, on this module's own helpers.

    `_seq` must notice a reordering and `_set` must not. One fixture, two
    orderings, both digests read from each -- so neither claim rests on how the
    helper is written.
    """
    hc.execute("CREATE TEMP TABLE o_src (n int)")
    hc.execute("INSERT INTO o_src SELECT g FROM generate_series(1,500) g")
    hc.execute("CREATE TEMP VIEW o_asc AS SELECT n FROM o_src ORDER BY n")
    hc.execute("CREATE TEMP VIEW o_desc AS SELECT n FROM o_src ORDER BY n DESC")

    expect.text(_differs(_seq(hc, "o_asc"), _seq(hc, "o_desc")), "different",
                "premise: the ordered oracle is order-sensitive")
    expect.text(_seq(hc, "o_asc"), _seq(hc, "o_asc"),
                "premise: the ordered oracle agrees with itself")
    expect.text(_set(hc, "o_asc"), _set(hc, "o_desc"),
                "control: the set oracle is order-blind by design")

    hc.execute("DROP VIEW o_asc, o_desc")
    hc.execute("DROP TABLE o_src")


def test_parallelism_is_off_so_a_scan_order_is_a_fact_about_the_layout(hc, expect):
    """Every digest in this module is a claim about PHYSICAL layout.

    A parallel plan interleaves workers' output independently of the layout, so a
    parallel scan would make every digest a claim about scheduling. The fixture
    SETs this; nothing until now read it back.
    """
    expect.text(_scalar(hc, "SHOW max_parallel_workers_per_gather"), "0",
                "premise: parallelism is off, so a scan order is a fact about the "
                "layout")


# =============================================================================
# S1  THE SURFACE AND ITS REFUSALS, BY SQLSTATE
# =============================================================================


def test_the_refusal_fixture_and_its_roles_are_this_runs(surface, expect, pgc_cluster):
    """The premises every 42501 below rests on.

    A DENY ARM IS EVIDENCE ONLY IF THE CALL REACHED THE CODE THAT DENIES IT. A
    login FATAL carries no SQLSTATE at all, so `h_other` must be able to open a
    session or the 42501 arms measure connectivity. And none of the four verbs
    is REVOKEd from PUBLIC, so EXECUTE is not the layer that refuses -- asserted
    rather than assumed, because a future REVOKE would stop a 42501 attributing
    to the C owner check.
    """
    expect.num(_scalar(surface, "SELECT count(*) FROM sur"), 2000,
               "premise: the refusal fixture holds rows, so a refusal is not an "
               "empty-table artifact")
    expect.num(_scalar(surface,
                       "SELECT count(*) FROM pg_class "
                       "WHERE relowner = 'h_other'::regrole"), 0,
               "premise: h_other owns nothing, so it is this run's role and not a leftover")
    expect.text(_sqlstate(pgc_cluster, "h_other", "SELECT 1"), NOERROR,
                "premise: h_other can open a session, so its refusals are refusals "
                "and not login failures")
    expect.text(str(_scalar(surface,
                            "SELECT pg_get_userbyid(relowner) = 'h_other' "
                            "FROM pg_class WHERE oid = 'sur'::regclass")), "False",
                "premise: h_other does not own the table")
    expect.text(_scalar(surface, """
        SELECT count(*) FILTER (WHERE has_function_privilege('h_other', p.oid, 'EXECUTE'))
               || '/' || count(*)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'pgcolumnar'
           AND p.proname IN ('cluster','recluster','cluster_hilbert','recluster_hilbert')
        """), "4/4",
        "premise: h_other holds EXECUTE on all four verbs, so a 42501 below can "
        "only be the owner check")
    expect.text(str(_scalar(surface,
                            f"SELECT has_schema_privilege('h_other', '{SCHEMA}', 'USAGE')")),
                "True",
                "premise: and h_other holds USAGE on the fixture schema, so a 42501 "
                "is not the schema check")


def _s1(cluster, role, args, old, new):
    """-> (the SQLSTATE the established verb raises, the one the new verb raises)."""
    return (_sqlstate(cluster, role, f"SELECT pgcolumnar.{old}({args});"),
            _sqlstate(cluster, role, f"SELECT pgcolumnar.{new}({args});"))


# bash S1's loop: four inputs against each of the two verb pairs, ONE CHECK PER TEST.
# The three checks of an input used to share a test, and the layer's assertions
# raise, so when the first went red the comparison after it was never evaluated and
# the twin reported fewer reds than the shell for the same run (#1339 :301).
@pytest.mark.parametrize("what,role,args,want", S1_CASES,
                         ids=[c[0].replace(" ", "-") for c in S1_CASES])
@pytest.mark.parametrize("old,new", S1_PAIRS, ids=[p[0] for p in S1_PAIRS])
def test_each_established_verb_refuses_the_input(
        surface, expect, pgc_cluster, what, role, args, want, old, new):
    """The established verb's own refusal: pgcolumnar code, so not a premise."""
    s_old, _ = _s1(pgc_cluster, role, args, old, new)
    expect.text(s_old, want,
                f"{old} on {what} raises {want} (the probe reads the right state)")


@pytest.mark.parametrize("what,role,args,want", S1_CASES,
                         ids=[c[0].replace(" ", "-") for c in S1_CASES])
@pytest.mark.parametrize("old,new", S1_PAIRS, ids=[p[0] for p in S1_PAIRS])
def test_each_new_verb_refuses_what_its_sibling_refuses(
        surface, expect, pgc_cluster, what, role, args, want, old, new):
    """COMPARED AGAINST THE SIBLING, not only against a literal: see the next test."""
    _, s_new = _s1(pgc_cluster, role, args, old, new)
    expect.text(s_new, want, f"{new} on {what} raises {want}")


@pytest.mark.parametrize("what,role,args,want", S1_CASES,
                         ids=[c[0].replace(" ", "-") for c in S1_CASES])
@pytest.mark.parametrize("old,new", S1_PAIRS, ids=[p[0] for p in S1_PAIRS])
def test_each_new_verbs_state_is_the_one_its_sibling_raises(
        surface, expect, pgc_cluster, what, role, args, want, old, new):
    """A change to `cluster()`'s refusal that is not made to `cluster_hilbert()`'s
    reddens this arm even when both literal arms still pass.

    "THE ONE {old} RAISES", so it has to be a raised SQLSTATE. Two verbs that both
    ran clean, or both went unreachable, used to agree here under a name that says
    two refusals were compared (#1339 :301): with the shared column cap removed,
    cluster and cluster_hilbert both returned noerror on nine columns and this
    passed.
    """
    s_old, s_new = _s1(pgc_cluster, role, args, old, new)
    cmp = s_new if re.fullmatch(r"[0-9A-Z]{5}", str(s_old)) else \
        f"no comparison: {old} raised nothing ({s_old})"
    expect.text(cmp, s_old,
                f"and {new}'s state on {what} is the one {old} raises on the identical input")


def test_the_probe_can_report_both_success_and_an_unreachable_session(
        surface, expect, pgc_cluster):
    """The removal proof for the whole block, in both directions.

    The probe must be able to report SUCCESS, or every arm above is satisfied by
    an instrument that always finds an error -- and it must be able to report
    that it never reached the server, or "success" is what an unreachable probe
    says and the first control proves nothing. A NOLOGIN role is refused at
    connection time and carries no SQLSTATE, which is exactly the shape the
    second control exists to catch.
    """
    expect.text(_sqlstate(pgc_cluster, "postgres",
                          "SELECT pgcolumnar.cluster('sur','c1','c2');"), NOERROR,
                "control: the probe reports noerror when the owner makes a call that works")
    expect.text(_sqlstate(pgc_cluster, "h_nologin", "SELECT 1;"), UNREACHABLE,
                "control: and it reports PROBE_UNREACHABLE when it cannot open a "
                "session, so noerror means the server answered")


# =============================================================================
# S2  IT ONLY REORDERS
# =============================================================================
#
# Against a heap mirror. The set hash is order-blind ON PURPOSE, so it is the
# right instrument for "the rows are the same rows" and the WRONG one for "the
# rows moved". Both halves are here because a parity-only arm passes on a table
# that was never touched.

# Order-blind, exactly like `pgc_set_hash` in test/lib.sh.
#
# THE SENTINEL IS SHAPED LIKE THE ONE THE LAYER ALREADY REFUSES, following
# test_hilbert_locality.py: `expect.hash` rejects a value beginning QUERY_ERROR,
# while the bash suite's five-character `EMPTY` is not empty to the layer's
# `_empty()` and would pass an equality arm comparing two dead reads.
SET_HASH_SQL = """
SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)),
                'QUERY_ERROR.empty-relation')
FROM (SELECT r::text AS t FROM {table} r) s
"""

# Order-SENSITIVE, like `pgc_seq_hash`: the scan's own output order is kept, so
# this can fail on a reordering where the set hash cannot. Both appear in S2
# because a parity-only arm passes on a table nothing touched.
SEQ_HASH_SQL = """
SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY n)),
                'QUERY_ERROR.empty-relation')
FROM (SELECT r::text AS t, row_number() OVER () AS n FROM {table} r) s
"""

# The per-group (stripeid, fileoffset, rowcount) multiset: a falsifiable
# PHYSICAL signal for "was this rewritten". A relation with no stripes yields a
# sentinel rather than NULL, so it cannot read as a layout that moved.
PHYSLAYOUT_SQL = """
SELECT coalesce(
         md5(string_agg(stripeid::text||':'||fileoffset::text||':'||rowcount::text,
                        ',' ORDER BY stripeid)),
         'QUERY_ERROR.no-layout')
FROM pgcolumnar.stats(%s)
"""

MEASURED_SENTINEL = "QUERY_ERROR"


def _measured(value):
    """-> True when VALUE is a real measurement rather than a dead read.

    The digest helpers return sentinels rather than NULL so that two FAILED
    reads cannot compare equal and pass an equality arm (#418). The same
    sentinels satisfy an INEQUALITY arm outright -- a sentinel is unequal to any
    baseline, so "the layout moved" would pass on a query that never ran -- which
    is why every inequality arm here goes through `_changed` or `_differs`.
    """
    return bool(value) and not str(value).startswith(MEASURED_SENTINEL)


def _changed(before, after):
    """-> moved | unchanged | UNMEASURED[...], never a bare boolean."""
    if not _measured(before):
        return f"UNMEASURED[before={before}]"
    if not _measured(after):
        return f"UNMEASURED[after={after}]"
    return "moved" if before != after else "unchanged"


def _differs(a, b):
    """-> different | IDENTICAL | UNMEASURED[...]."""
    if not _measured(a):
        return f"UNMEASURED[a={a}]"
    if not _measured(b):
        return f"UNMEASURED[b={b}]"
    return "different" if a != b else "IDENTICAL"


def _seq(conn, table):
    return _scalar(conn, SEQ_HASH_SQL.format(table=table))


def _set(conn, table):
    return _scalar(conn, SET_HASH_SQL.format(table=table))


def _physlayout(conn, table):
    with conn.cursor() as cur:
        cur.execute(PHYSLAYOUT_SQL, (table,))
        row = cur.fetchone()
    return row[0] if row else "QUERY_ERROR.no-layout"


def _groups(conn, table):
    return _scalar(conn, f"SELECT total_groups FROM pgcolumnar.sort_status('{table}')")


def _verb(cluster, expect, what, sql):
    """Run a maintenance verb AS AN ASSERTION, the port of bash `hrun`.

    A CALL MADE WITHOUT CHECKING IS NOT AN ASSERTION. The bash suite runs under
    `set -uo pipefail` with no `-e`, so a verb that RAISES leaves no trace: the
    fixture is simply not rewritten, and "the verb left the table alone" is
    byte-identical to "the verb threw". A vacuum_sorted that raised on every
    Hilbert table once scored as the pinned no-op and a whole arm went green.
    psycopg would raise here rather than pass silently, but the arm is kept
    because the PROPERTY -- this verb ran and did not raise -- is what the
    fixtures downstream depend on, and an exception would name the test rather
    than the verb.
    """
    expect.text(_sqlstate(cluster, "postgres", sql), NOERROR,
                f"{what} ran without raising")


def _is_columnar_scan(conn, sql):
    plan = _scalar(conn, f"EXPLAIN (FORMAT JSON) {sql}")
    return "yes" if "Columnar" in str(plan) else "no"


@pytest.fixture(scope="module")
def s2(hc):
    """A heap mirror and its columnar twin, both loaded from the same rows.

    THE MIRROR IS MATERIALISED ONCE and the columnar table loads FROM it, rather
    than both evaluating the generator: two independent evaluations is how the
    two sides come to hold different rows.
    """
    hc.execute("CREATE TABLE s2h (id int, x int, y int, pad text)")
    hc.execute("CREATE TABLE s2c (id int, x int, y int, pad text) USING pgcolumnar")
    hc.execute("SELECT pgcolumnar.set_options('s2c', stripe_row_limit => 2048,"
               " chunk_group_row_limit => 1024)")
    hc.execute("INSERT INTO s2h SELECT g, ((g::bigint*7919)%200)::int,"
               " ((g::bigint*104729)%200)::int, 'p'||(g%97)"
               " FROM generate_series(1,40960) g")
    hc.execute("INSERT INTO s2c SELECT * FROM s2h")
    state = {
        "heap": _set(hc, "s2h"),
        "set_before": _set(hc, "s2c"),
        "seq_before": _seq(hc, "s2c"),
        "seq_again": _seq(hc, "s2c"),
    }
    yield hc, state
    hc.execute("DROP TABLE IF EXISTS s2h, s2c, s2ctl")


def test_the_reorder_fixture_is_measurable_before_anything_moves(s2, expect):
    """S2's four premises. Without them the arms below pass by construction."""
    conn, st = s2
    expect.num(_groups(conn, "s2c"), 20,
               "premise: set_options took, so s2c is 20 groups and not one "
               "default-sized group")
    expect.text(st["set_before"], st["heap"],
                "premise: before clustering, the columnar table already matches its "
                "heap mirror as a SET")
    expect.text(st["seq_again"], st["seq_before"],
                "premise: the order-sensitive digest is STABLE across two reads, so a "
                "later change is a change")
    expect.text(_is_columnar_scan(conn, "SELECT * FROM s2c"), "yes",
                "premise: the plan being digested is the columnar custom scan, not a "
                "fallback")


def test_cluster_hilbert_reorders_without_changing_the_row_set(s2, expect, pgc_cluster):
    """The two halves together: same rows, different order.

    NEITHER HALF STANDS ALONE. The set hash is order-blind by design, so a
    parity-only arm passes on a table nothing touched; the order digest alone
    would not notice a verb that dropped and re-inserted different rows.
    """
    conn, st = s2
    _verb(pgc_cluster, expect, "cluster_hilbert('s2c','x','y')",
          "SELECT pgcolumnar.cluster_hilbert('s2c', 'x', 'y');")

    expect.text(_set(conn, "s2c"), st["set_before"],
                "cluster_hilbert preserves the row SET exactly (unchanged from before)")
    expect.text(_set(conn, "s2c"), st["heap"],
                "and the row set still equals the heap mirror's")
    expect.text(_changed(st["seq_before"], _seq(conn, "s2c")), "moved",
                "cluster_hilbert MOVED the rows: the order-sensitive digest changed")
    expect.num(_scalar(conn, "SELECT count(*) FROM s2c"), 40960,
               "and no row was lost or gained")


def test_physlayout_is_blind_to_an_eager_rewrite(s2, expect, pgc_cluster):
    """THE INSTRUMENT'S LIMIT, PINNED ON A VERB THAT ALREADY EXISTS.

    `physlayout` is the right corroboration for the ONLINE gate in S4 and the
    WRONG instrument here: the eager path writes a fresh file and reproduces the
    same (stripeid, fileoffset, rowcount) multiset, so the digest is identical
    either side of a rewrite that reordered every row.

    MEASURED ON plain `cluster()` rather than asserted about `cluster_hilbert()`,
    so the control stands on code that ships today and cannot be perturbed by
    whatever #889 lands.
    """
    conn, _ = s2
    conn.execute("CREATE TABLE s2ctl (id int, x int, y int, pad text) USING pgcolumnar")
    conn.execute("SELECT pgcolumnar.set_options('s2ctl', stripe_row_limit => 2048,"
                 " chunk_group_row_limit => 1024)")
    conn.execute("INSERT INTO s2ctl SELECT * FROM s2h")
    phys_before = _physlayout(conn, "s2ctl")
    seq_before = _seq(conn, "s2ctl")

    _verb(pgc_cluster, expect, "cluster('s2ctl','x','y')",
          "SELECT pgcolumnar.cluster('s2ctl', 'x', 'y');")

    expect.text(_changed(seq_before, _seq(conn, "s2ctl")), "moved",
                "control: an eager cluster() DOES reorder the rows")
    expect.text(_changed(phys_before, _physlayout(conn, "s2ctl")), "unchanged",
                "control: and physlayout cannot see it, which is why the order digest "
                "is S2's instrument")


# =============================================================================
# S3  THE KIND IS RECORDED, AND IT IS DISTINGUISHABLE
# =============================================================================
#
# Three tables from ONE fixture generator, so the only difference between them
# is the verb that rewrote them. Read BOTH ways: `pgcolumnar.storage` is the
# catalog and is superuser-only, `pgcolumnar.sort_status` is the reporter and is
# the only route a table's own owner has. Reading one tests one -- and reading
# both AS THE SUPERUSER tests one, which is why the three fixtures are handed to
# a plain role and the reporter is read back as that role.


def _scalar_as(cluster, role, sql):
    """A scalar read AS A NAMED ROLE.

    The module's own connection is the superuser's and always will be; this is
    the only way an arm may claim to have measured what a table's OWNER can see.
    """
    import psycopg

    try:
        conn = psycopg.connect(cluster.dsn(), user=role, autocommit=True,
                               options=f"-c search_path={SCHEMA},public")
    except psycopg.OperationalError:
        return UNREACHABLE
    try:
        row = conn.execute(sql).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _skind_as(cluster, role, table):
    return _scalar_as(cluster, role,
                      "SELECT coalesce(sorted_kind::text,'<NULL>')"
                      f" FROM pgcolumnar.sort_status('{table}')")


@pytest.fixture(scope="module")
def s3(hc, pgc_cluster):
    """Three tables, one generator, one verb each -- then handed to a plain role.

    EACH LEG IS ANCHORED AGAINST ITS OWN PRE-CLUSTERING BASELINE. "s3hi's order
    differs from s3zo's" is also true of an s3hi nobody rewrote: it is still in
    insert order, which differs from both tables that WERE rewritten. Both such
    arms printed PASS on a tree with no cluster_hilbert in it, so the `moved`
    verdicts are captured here and the comparisons below are recorded UNRUN
    unless both of their legs moved.
    """
    state = {}
    hc.execute("DROP ROLE IF EXISTS h_owner")
    hc.execute("CREATE ROLE h_owner NOSUPERUSER LOGIN")
    hc.execute("GRANT USAGE ON SCHEMA pgcolumnar TO h_owner")
    hc.execute(f'GRANT USAGE ON SCHEMA "{SCHEMA}" TO h_owner')

    for table, what, sql in (
            ("s3lex", "vacuum_sorted('s3lex','k')",
             "SELECT pgcolumnar.vacuum_sorted('s3lex', 'k');"),
            ("s3zo", "cluster('s3zo','k','j')",
             "SELECT pgcolumnar.cluster('s3zo', 'k', 'j');"),
            ("s3hi", "cluster_hilbert('s3hi','k','j')",
             "SELECT pgcolumnar.cluster_hilbert('s3hi', 'k', 'j');")):
        hc.execute(f"CREATE TABLE {table} (id int, k int, j int) USING pgcolumnar")
        hc.execute(f"SELECT pgcolumnar.set_options('{table}',"
                   f" stripe_row_limit => {SR}, chunk_group_row_limit => {CG})")
        hc.execute(f"INSERT INTO {table} SELECT g, (g*7919)%5000, g%13"
                   " FROM generate_series(1,5000) g")
        state[table] = {"groups": _groups(hc, table), "seq0": _seq(hc, table),
                        "what": what, "sql": sql}

    for table in ("s3lex", "s3zo", "s3hi"):
        state[table]["state"] = _sqlstate(pgc_cluster, "postgres", state[table]["sql"])
        state[table]["moved"] = _changed(state[table]["seq0"], _seq(hc, table))

    for table in ("s3hi", "s3lex", "s3zo"):
        hc.execute(f"ALTER TABLE {table} OWNER TO h_owner")
    state["owned"] = _scalar(hc, "SELECT count(*) FROM pg_class WHERE relowner ="
                                 " 'h_owner'::regrole AND relname IN"
                                 " ('s3hi','s3lex','s3zo')")
    yield hc, state
    hc.execute("DROP OWNED BY h_owner")
    hc.execute("DROP ROLE h_owner")


def test_the_three_fixtures_and_the_owner_role_are_measurable(s3, expect, pgc_cluster):
    """S3's premises: the geometry, the role, and that each verb actually ran."""
    conn, st = s3
    for table in ("s3lex", "s3zo", "s3hi"):
        expect.num(st[table]["groups"], 5,
                   f"premise: set_options took on {table}, so it is 5 groups and not "
                   "one default-sized group")
    expect.text(str(_scalar_as(pgc_cluster, "h_owner", "SELECT 1")), "1",
                "premise: h_owner can open a session, so a read as h_owner is a read")
    expect.text(str(_scalar(conn, "SELECT rolsuper::text FROM pg_roles"
                                  " WHERE rolname = 'h_owner'")), "false",
                "premise: h_owner is NOT a superuser, or reading 'as the owner' reads "
                "as the superuser again")
    expect.text(_sqlstate(pgc_cluster, "h_owner",
                          "SELECT count(*) FROM pgcolumnar.storage;"), "42501",
                "premise: and the catalog really is closed to it, so sort_status is "
                "the only route it has")
    for table in ("s3lex", "s3zo", "s3hi"):
        expect.text(st[table]["state"], NOERROR,
                    f"{st[table]['what']} ran without raising")


def test_each_verb_moved_its_own_table_from_its_own_baseline(s3, expect):
    """Without these, "different from each other" is satisfied by insert order."""
    conn, st = s3
    expect.text(st["s3lex"]["moved"], "moved",
                "vacuum_sorted moved s3lex from its own baseline")
    expect.text(st["s3zo"]["moved"], "moved",
                "cluster moved s3zo from its own baseline")
    expect.text(st["s3hi"]["moved"], "moved",
                "cluster_hilbert moved s3hi from its own baseline")
    expect.text(_is_columnar_scan(conn, "SELECT * FROM s3hi"), "yes",
                "premise: the plan being digested for s3hi is the columnar custom "
                "scan too")
    expect.num(st["owned"], 3,
               "premise: the three fixtures are now owned by h_owner, so the reads "
               "below are the owner's")


def test_the_kind_is_recorded_and_the_owner_can_read_it(s3, expect, pgc_cluster):
    """Both routes: the superuser-only catalog and the owner's reporter."""
    conn, _ = s3
    expect.text(_scalar(conn, "SELECT coalesce(sorted_kind::text,'<NULL>')"
                              " FROM pgcolumnar.storage WHERE storage_id ="
                              " pgcolumnar.get_storage_id('s3hi')"), "hilbert",
                "cluster_hilbert records the kind in pgcolumnar.storage")
    expect.text(_skind_as(pgc_cluster, "h_owner", "s3hi"), "hilbert",
                "and pgcolumnar.sort_status reports that same kind to the table's own "
                "(non-superuser) owner")
    expect.text(_scalar(conn, "SELECT coalesce(sort_key::text,'<NULL>')"
                              " FROM pgcolumnar.sort_status('s3hi')"), "{k,j}",
                "and it records the key it applied")


def test_the_owner_alone_can_tell_the_three_kinds_apart(s3, expect, pgc_cluster):
    """ASSERTED AS A NAMED SET, NOT PAIRWISE.

    Three pairwise `!=` arms are each satisfied by a NULL on one side -- all
    three passed on a tree where cluster_hilbert did not exist and s3hi's kind
    was <NULL>. They can say which pair collapsed; they cannot say the three
    kinds ARE the three kinds. This one can, and it is strictly stronger than
    all three together.
    """
    kinds = sorted(_skind_as(pgc_cluster, "h_owner", t)
                   for t in ("s3hi", "s3lex", "s3zo"))
    expect.text("|".join(kinds) + "|", "hilbert|lexicographic|zorder|",
                "the owner alone can tell the three apart: the kinds ARE hilbert, "
                "lexicographic and zorder, with no NULL standing in for one")


# SPELLED OUT, for the same reason the signature pair is: the bash side writes
# these two literally, so an f-string templates to `(hilbert vs {})` and matches
# neither. Both arms ran and passed while reading as MISSING.
_KIND_PAIRS = [
    ("s3zo", "the three kinds stand for three different physical orders "
             "(hilbert vs zorder)"),
    ("s3lex", "the three kinds stand for three different physical orders "
              "(hilbert vs lexicographic)"),
]


@pytest.mark.parametrize("other,name", _KIND_PAIRS, ids=[p[0] for p in _KIND_PAIRS])
def test_the_three_kinds_stand_for_three_physical_orders(s3, expect, other, name):
    """ONE OF THE ARMS IN THIS FILE THAT REFUSE A RELABELLED Z-ORDER.

    Without it, "distinguishable" could be true of a catalog column with nothing
    physical behind it -- which is exactly what a relabelled implementation is.

    UNRUN, NOT PASS, WHEN A LEG NEVER MOVED. The comparison is only about the
    curve if both sides were actually rewritten; otherwise "a different order"
    is insert order. Each case is its own test so that two refusals cannot
    collapse onto one `UNMET_PRECONDITION` record.
    """
    conn, st = s3
    if st["s3hi"]["moved"] != "moved" or st[other]["moved"] != "moved":
        expect.cannot_run(
            "UNMET_PRECONDITION",
            f"a leg was never rewritten (s3hi=[{st['s3hi']['moved']}], "
            f"{other}=[{st[other]['moved']}]), so 'a different order' would only be "
            "insert order")
        return
    expect.text(_differs(_seq(conn, "s3hi"), _seq(conn, other)), "different", name)


# =============================================================================
# S4  THE SELF-GATE, IN EVERY DIRECTION, CORROBORATED BY A POSITIVE CONTROL
# =============================================================================
#
# There are FIVE directions, not four, and the fifth is the one the daemon
# depends on: same kind, same key, WITH AN APPENDED TAIL must NOT gate. A gate
# keyed on kind and key alone satisfies every other arm here -- (a) 0, (b) >0,
# (c) 0, (d) >0 -- and breaks the daemon outright. Measured against a shim whose
# gate ignored the tail: NO ARM IN S4 COULD SEE IT, and the only arm that
# reddened was a FIXTURE PREMISE in S7. A defect in the gate must redden a gate
# arm, not read as a bad fixture. So (a) does not stop at "it returned 0": it
# appends a tail to the very table it just gated, requires the SAME call to do
# work, and then requires the gate to close again.
#
# physlayout is carried beside every return value, but only where it can
# CORROBORATE: an unchanged layout beside a 0 return is the same observation
# twice, so on the no-op side it is asserted only after the return has been
# confirmed to be 0, and recorded UNRUN otherwise.

S4_ROWS = 20000
S4_TAIL = 5000
S4_GROUPS = 20


def _appended(conn, table):
    return _scalar(conn, f"SELECT appended_groups FROM pgcolumnar.sort_status('{table}')")


def _skind(conn, table):
    return _scalar(conn, "SELECT coalesce(sorted_kind::text,'<NULL>') FROM"
                         f" pgcolumnar.storage WHERE storage_id ="
                         f" pgcolumnar.get_storage_id('{table}')")


def _skey(conn, table):
    return _scalar(conn, "SELECT coalesce(sort_key::text,'<NULL>') FROM"
                         f" pgcolumnar.sort_status('{table}')")


def _mk4(conn, table):
    """One S4 fixture. Its group count is ASSERTED by the caller, not assumed."""
    conn.execute(f"CREATE TABLE {table} (a int, b int, pad text) USING pgcolumnar")
    conn.execute(f"SELECT pgcolumnar.set_options('{table}',"
                 f" stripe_row_limit => {SR}, chunk_group_row_limit => {CG})")
    conn.execute(f"INSERT INTO {table} SELECT (g*2654435761)::bigint % 1000, g,"
                 f" md5(g::text) FROM generate_series(1,{S4_ROWS}) g")
    return _groups(conn, table)


def _decay4(conn, table):
    """The 25% appended tail every decay fixture gets."""
    conn.execute(f"INSERT INTO {table} SELECT (g*2654435761)::bigint % 1000, g,"
                 f" md5(g::text) FROM generate_series(1,{S4_TAIL}) g")


@pytest.fixture(scope="module")
def s4a(hc, pgc_cluster):
    """(a) an already-hilbert table, same key -- then the same call with a tail."""
    st = {"groups": _mk4(hc, "s4a")}
    st["ran"] = _sqlstate(pgc_cluster, "postgres",
                          "SELECT pgcolumnar.cluster_hilbert('s4a','a','b');")
    st["appended0"] = _appended(hc, "s4a")
    st["phys1"] = _physlayout(hc, "s4a")
    st["ret1"] = _scalar(hc, "SELECT pgcolumnar.recluster_hilbert('s4a','a','b')")
    st["phys1_after"] = _physlayout(hc, "s4a")
    st["kind1"] = _skind(hc, "s4a")

    _decay4(hc, "s4a")
    st["appended_tail"] = _appended(hc, "s4a")
    st["set2"] = _set(hc, "s4a")
    st["phys2"] = _physlayout(hc, "s4a")
    st["ret2"] = _scalar(hc, "SELECT pgcolumnar.recluster_hilbert('s4a','a','b')")
    st["phys2_after"] = _physlayout(hc, "s4a")

    st["phys3"] = _physlayout(hc, "s4a")
    st["ret3"] = _scalar(hc, "SELECT pgcolumnar.recluster_hilbert('s4a','a','b')")
    st["phys3_after"] = _physlayout(hc, "s4a")
    yield hc, st
    hc.execute("DROP TABLE IF EXISTS s4a")


def test_the_gate_is_closed_on_an_already_hilbert_table(s4a, expect):
    conn, st = s4a
    expect.num(st["groups"], S4_GROUPS,
               "premise: set_options took on s4a, so it is 20 groups and 'nothing "
               "changed' cannot be true because there was nothing there")
    expect.text(st["ran"], NOERROR,
                "cluster_hilbert('s4a','a','b') ran without raising")
    expect.num(st["appended0"], 0,
               "the eager rewrite left no appended tail on s4a")
    expect.num(st["ret1"], 0,
               "(a) recluster_hilbert on an already-hilbert table with the same key "
               "reclusters 0 groups")
    expect.text(st["kind1"], "hilbert", "(a) and the kind is untouched")


def test_the_gated_call_left_the_layout_byte_identical(s4a, expect):
    """UNRUN unless the return really was 0.

    An unchanged layout beside a non-zero return says nothing about the gate --
    it is the same observation as "nothing happened", which is what a dead verb
    also produces. Its own test so this refusal cannot share a record with
    another.
    """
    conn, st = s4a
    if st["ret1"] != 0:
        expect.cannot_run("UNMET_PRECONDITION",
                          f"the call did not return 0 (returned [{st['ret1']}]), so an "
                          "unchanged layout would not be about the gate")
        return
    expect.text(st["phys1_after"], st["phys1"],
                "(a) and the physical layout is byte-identical")


def test_s4a_carries_an_appended_tail_for_the_gate(s4a, expect):
    conn, st = s4a
    expect.text("decayed" if (st["appended_tail"] or 0) > 0 else "clean", "decayed",
                "premise: s4a now carries an appended tail for the gate to let through")


# (a2), ONE CHECK PER TEST (#1339 :854). These seven shared a test, and the layer's
# assertions raise, so once the first went red the rest were never evaluated: with
# recluster_hilbert a no-op, the shell named "(a2) and it folded the appended tail
# back in" and the twin did not.


def test_the_same_call_does_work_once_a_tail_is_appended(s4a, expect):
    """THE POSITIVE CONTROL, ON THE SAME TABLE AND THROUGH THE SAME CALL.

    Without it, "it returned 0 and nothing moved" is equally true of a verb that
    does nothing at all -- a stub defined as `BEGIN RETURN 0; END` passed all
    three arms above. It is also the fifth direction: a Hilbert table with an
    appended tail is exactly what the daemon hands this verb, and a gate that
    skips it is a gate that disables the daemon.
    """
    conn, st = s4a
    # `(x or 0) > 0` maps BOTH None and 0 to "no": a verb that returned no count and
    # a verb that returned zero are different failures and this said neither.
    expect.text("yes" if (st["ret2"] or 0) > 0 else f"no (returned {st['ret2']!r})", "yes",
                "(a2) THE SAME CALL ON THE SAME TABLE does work once a tail is "
                "appended, so the 0 above was a gate and not a dead verb (>0)")


def test_the_a2_rewrite_moved_the_layout(s4a, expect):
    conn, st = s4a
    expect.text(_changed(st["phys2"], st["phys2_after"]), "moved",
                "(a2) and that rewrite moved the layout")


def test_the_a2_rewrite_folded_the_appended_tail_back_in(s4a, expect):
    conn, st = s4a
    expect.num(_appended(conn, "s4a"), 0,
               "(a2) and it folded the appended tail back in")


def test_after_the_a2_rewrite_it_is_still_hilbert_on_the_same_key(s4a, expect):
    conn, st = s4a
    expect.text(f"{_skind(conn, 's4a')}/{_skey(conn, 's4a')}", "hilbert/{a,b}",
                "(a2) and it is still a hilbert table on the same key")


def test_the_a2_rewrite_lost_no_row(s4a, expect):
    conn, st = s4a
    expect.num(_scalar(conn, "SELECT count(*) FROM s4a"), S4_ROWS + S4_TAIL,
               "(a2) and no row was lost")


def test_the_a2_rewrite_kept_the_same_rows_only_reordered(s4a, expect):
    conn, st = s4a
    expect.text(_set(conn, "s4a"), st["set2"],
                "(a2) and the rows are the same rows, only reordered")

def test_the_gate_closes_again_on_the_refolded_table(s4a, expect):
    conn, st = s4a
    expect.num(st["ret3"], 0, "(a3) and the gate closes again on the refolded table")


def test_the_refolded_layout_is_byte_identical_again(s4a, expect):
    conn, st = s4a
    if st["ret3"] != 0:
        expect.cannot_run("UNMET_PRECONDITION",
                          f"the call did not return 0 (returned [{st['ret3']}]), so an "
                          "unchanged layout would not be about the gate")
        return
    expect.text(st["phys3_after"], st["phys3"],
                "(a3) and the layout is byte-identical again")


@pytest.fixture(scope="module")
def s4b(hc, pgc_cluster):
    """(b) a zorder table over the same columns: the gate must NOT hold."""
    st = {"groups": _mk4(hc, "s4b")}
    st["ran"] = _sqlstate(pgc_cluster, "postgres",
                          "SELECT pgcolumnar.cluster('s4b','a','b');")
    st["kind_before"] = f"{_skind(hc, 's4b')}/{_skey(hc, 's4b')}"
    st["phys"] = _physlayout(hc, "s4b")
    st["set"] = _set(hc, "s4b")
    st["ret"] = _scalar(hc, "SELECT pgcolumnar.recluster_hilbert('s4b','a','b')")
    st["phys_after"] = _physlayout(hc, "s4b")
    st["kind_after"] = _skind(hc, "s4b")
    yield hc, st
    hc.execute("DROP TABLE IF EXISTS s4b")


def test_the_gate_opens_for_a_zorder_table_over_the_same_columns(s4b, expect):
    """THE LABEL IS ASSERTED WITH THE BYTES.

    On its own the kind arm was green in a run where its two siblings were red:
    a shim that wrote 'hilbert' into the catalog and rewrote nothing at all
    satisfied it. So the kind and the layout verdict are one value.
    """
    conn, st = s4b
    expect.num(st["groups"], S4_GROUPS,
               "premise: set_options took on s4b, so it is 20 groups and 'nothing "
               "changed' cannot be true because there was nothing there")
    expect.text(st["ran"], NOERROR, "cluster('s4b','a','b') ran without raising")
    expect.text(st["kind_before"], "zorder/{a,b}",
                "s4b is a zorder table over exactly (a,b)")
    expect.text("yes" if (st["ret"] or 0) > 0 else f"no (returned {st['ret']!r})", "yes",
                "(b) recluster_hilbert on a zorder table over the same columns does "
                "real work (>0)")
    expect.text(_changed(st["phys"], st["phys_after"]), "moved",
                "(b) and the physical layout moved")
    expect.text(f"{st['kind_after']}/{_changed(st['phys'], st['phys_after'])}",
                "hilbert/moved",
                "(b) and the recorded kind became hilbert IN THE SAME CALL THAT MOVED "
                "THE LAYOUT")
    expect.num(_scalar(conn, "SELECT count(*) FROM s4b"), S4_ROWS,
               "(b) and no row was lost")
    expect.text(_set(conn, "s4b"), st["set"],
                "(b) and the rows are the same rows, only reordered")


@pytest.fixture(scope="module")
def s4c(hc, pgc_cluster):
    """(c) THE RULING: the curve is sticky, so plain recluster is a no-op here."""
    st = {"groups": _mk4(hc, "s4c")}
    st["ran"] = _sqlstate(pgc_cluster, "postgres",
                          "SELECT pgcolumnar.cluster_hilbert('s4c','a','b');")
    st["phys"] = _physlayout(hc, "s4c")
    st["ret"] = _scalar(hc, "SELECT pgcolumnar.recluster('s4c','a','b')")
    st["phys_after"] = _physlayout(hc, "s4c")
    yield hc, st
    hc.execute("DROP TABLE IF EXISTS s4c")


def test_plain_recluster_is_a_noop_on_a_hilbert_table(s4c, expect):
    conn, st = s4c
    expect.num(st["groups"], S4_GROUPS,
               "premise: set_options took on s4c, so it is 20 groups and 'nothing "
               "changed' cannot be true because there was nothing there")
    expect.text(st["ran"], NOERROR,
                "cluster_hilbert('s4c','a','b') ran without raising")
    expect.num(st["ret"], 0,
               "(c) plain recluster on a hilbert table with a matching key reclusters "
               "0 groups")
    expect.text(_skind(conn, "s4c"), "hilbert",
                "(c) and the table is still hilbert, not relabelled zorder")
    expect.num(_scalar(conn, "SELECT count(*) FROM s4c"), S4_ROWS,
               "(c) and no row was lost")


def test_the_noop_recluster_left_the_layout_byte_identical(s4c, expect):
    conn, st = s4c
    if st["ret"] != 0:
        expect.cannot_run("UNMET_PRECONDITION",
                          f"the call did not return 0 (returned [{st['ret']}]), so an "
                          "unchanged layout would not be about the gate")
        return
    expect.text(st["phys_after"], st["phys"],
                "(c) and the physical layout is byte-identical: it did not convert "
                "the table")


@pytest.fixture(scope="module")
def s4d(hc, pgc_cluster):
    """(d) the removal proof for (a) and (c): the gate must DISCRIMINATE.

    The twins are built and laid on the curve FIRST and asserted identical, so
    the last arm compares two curves over one key on one dataset rather than two
    histories.
    """
    st = {}
    for t in ("s4d1", "s4d2"):
        st[t] = {"groups": _mk4(hc, t)}
        st[t]["ran"] = _sqlstate(pgc_cluster, "postgres",
                                 f"SELECT pgcolumnar.cluster_hilbert('{t}','a','b');")
    st["seq1"] = _seq(hc, "s4d1")
    st["seq2"] = _seq(hc, "s4d2")
    st["s4d1"]["phys"] = _physlayout(hc, "s4d1")
    st["s4d2"]["phys"] = _physlayout(hc, "s4d2")

    st["s4d1"]["ret"] = _scalar(hc, "SELECT pgcolumnar.recluster_hilbert('s4d1','b','a')")
    st["s4d1"]["phys_after"] = _physlayout(hc, "s4d1")
    st["s4d2"]["ret"] = _scalar(hc, "SELECT pgcolumnar.recluster('s4d2','b','a')")
    st["s4d2"]["phys_after"] = _physlayout(hc, "s4d2")
    yield hc, st
    hc.execute("DROP TABLE IF EXISTS s4d1, s4d2")


def test_a_different_key_rewrites_in_both_directions(s4d, expect):
    conn, st = s4d
    for t in ("s4d1", "s4d2"):
        expect.num(st[t]["groups"], S4_GROUPS,
                   f"premise: set_options took on {t}, so it is 20 groups and "
                   "'nothing changed' cannot be true because there was nothing there")
        expect.text(st[t]["ran"], NOERROR,
                    f"cluster_hilbert('{t}','a','b') ran without raising")
    expect.text(st["seq1"], st["seq2"],
                "premise: the two (d) twins are byte-identical before either is "
                "reclustered on the new key")

    expect.text("yes" if (st["s4d1"]["ret"] or 0) > 0
                else f"no (returned {st['s4d1']['ret']!r})", "yes",
                "(d) recluster_hilbert over DIFFERENT columns still rewrites (>0)")
    expect.text(_changed(st["s4d1"]["phys"], st["s4d1"]["phys_after"]), "moved",
                "(d) and that rewrite moved s4d1's layout")
    expect.text(f"{_skind(conn, 's4d1')}/{_skey(conn, 's4d1')}", "hilbert/{b,a}",
                "(d) and it is still a hilbert table, now on the new key")
    expect.num(_scalar(conn, "SELECT count(*) FROM s4d1"), S4_ROWS,
               "(d) and no row was lost from s4d1")

    expect.text("yes" if (st["s4d2"]["ret"] or 0) > 0
                else f"no (returned {st['s4d2']['ret']!r})", "yes",
                "(d) plain recluster over DIFFERENT columns rewrites a hilbert table (>0)")
    expect.text(_changed(st["s4d2"]["phys"], st["s4d2"]["phys_after"]), "moved",
                "(d) and that rewrite moved s4d2's layout")
    expect.text(f"{_skind(conn, 's4d2')}/{_skey(conn, 's4d2')}", "zorder/{b,a}",
                "(d) and naming the plain verb with a NEW key is the explicit switch "
                "back to zorder")
    expect.num(_scalar(conn, "SELECT count(*) FROM s4d2"), S4_ROWS,
               "(d) and no row was lost from s4d2")


def test_the_online_hilbert_rewrite_is_not_the_zorder_rewrite(s4d, expect):
    """THE ONLINE VERB'S ONLY CURVE DEFENCE IN THIS FILE, and it is gated.

    Untouched, s4d1 is still on the (a,b) layout while s4d2 is on the (b,a) one,
    so "they differ" is satisfied by recluster_hilbert doing nothing -- which is
    what it did before #889, and the arm printed PASS on it.
    """
    conn, st = s4d
    d1 = "yes" if (st["s4d1"]["ret"] or 0) > 0 else "no"
    d2 = "yes" if (st["s4d2"]["ret"] or 0) > 0 else "no"
    if d1 != "yes" or d2 != "yes":
        expect.cannot_run(
            "UNMET_PRECONDITION",
            f"a rewrite did not happen (s4d1=[{st['s4d1']['ret']}], "
            f"s4d2=[{st['s4d2']['ret']}]), so the two layouts are not the two curves")
        return
    expect.text(_differs(_seq(conn, "s4d1"), _seq(conn, "s4d2")), "different",
                "(d) and the HILBERT rewrite over (b,a) is NOT the ZORDER rewrite over "
                "(b,a) on the identical data")


# =============================================================================
# S5  ncols == 1 IS THE IDENTITY, THROUGH SQL
# =============================================================================
#
# Over one column the Hilbert index and the Morton index are both the identity,
# so the two verbs must produce the SAME physical order while recording
# DIFFERENT kinds. Each side is also compared against its OWN pre-cluster
# baseline: two calls that both no-opped would compare equal to each other and
# the arm would pass on nothing.
#
# THIS SECTION BUYS SURFACE AND IDENTITY, NEVER THE CURVE. Over one column a
# relabelled Z-order implementation is INDISTINGUISHABLE from a correct one --
# all of S5 is green on one, by construction. Do not read a green S5 as evidence
# of Hilbertness; S3, S4(d) and S7 carry that.


def _mk5(conn, table):
    conn.execute(f"CREATE TABLE {table} (a int, b int, pad text) USING pgcolumnar")
    conn.execute(f"SELECT pgcolumnar.set_options('{table}',"
                 f" stripe_row_limit => {SR}, chunk_group_row_limit => {CG})")
    conn.execute(f"INSERT INTO {table} SELECT (g*2654435761)::bigint % 1000, g,"
                 f" md5(g::text) FROM generate_series(1,{S4_ROWS}) g")
    return _groups(conn, table)


@pytest.fixture(scope="module")
def s5(hc, pgc_cluster):
    st = {}
    for t in ("s5hi", "s5zo"):
        st[t] = {"groups": _mk5(hc, t)}
    st["s5hi"]["before"] = _seq(hc, "s5hi")
    st["s5zo"]["before"] = _seq(hc, "s5zo")
    st["s5hi"]["set"] = _set(hc, "s5hi")
    st["s5zo"]["set"] = _set(hc, "s5zo")
    st["scan_ok"] = _is_columnar_scan(hc, "SELECT * FROM s5hi")
    st["s5hi"]["ran"] = _sqlstate(pgc_cluster, "postgres",
                                  "SELECT pgcolumnar.cluster_hilbert('s5hi','a');")
    st["s5zo"]["ran"] = _sqlstate(pgc_cluster, "postgres",
                                  "SELECT pgcolumnar.cluster('s5zo','a');")
    yield hc, st
    hc.execute("DROP TABLE IF EXISTS s5hi, s5zo")


def test_over_one_column_both_curves_are_the_identity(s5, expect):
    """AND BOTH STILL HOLD THEIR ROWS.

    Two verbs that record their kind and then EMPTY the table satisfy both
    `moved` premises -- an emptied digest differs from every baseline -- and then
    compare equal to each other, so the identity arm passes too. The row-count
    and set premises are what stop that.
    """
    conn, st = s5
    for t in ("s5hi", "s5zo"):
        expect.num(st[t]["groups"], S4_GROUPS,
                   f"premise: set_options took on {t}, so it is 20 groups")
    expect.text(st["s5hi"]["before"], st["s5zo"]["before"],
                "premise: the two single-column fixtures start identical")
    expect.text(st["scan_ok"], "yes",
                "premise: the plan being digested for s5hi is the columnar custom "
                "scan too")
    expect.text(st["s5hi"]["ran"], NOERROR,
                "cluster_hilbert('s5hi','a') ran without raising")
    expect.text(st["s5zo"]["ran"], NOERROR,
                "cluster('s5zo','a') ran without raising")

    expect.text(_changed(st["s5hi"]["before"], _seq(conn, "s5hi")), "moved",
                "cluster_hilbert changed s5hi's order from its own baseline")
    expect.text(_changed(st["s5zo"]["before"], _seq(conn, "s5zo")), "moved",
                "cluster changed s5zo's order from its own baseline")
    expect.num(_scalar(conn, "SELECT count(*) FROM s5hi"), S4_ROWS,
               "premise: and s5hi still holds every row it started with")
    expect.num(_scalar(conn, "SELECT count(*) FROM s5zo"), S4_ROWS,
               "premise: and s5zo still holds every row it started with")
    expect.text(_set(conn, "s5hi"), st["s5hi"]["set"],
                "premise: and s5hi's rows are the same rows, only reordered")
    expect.text(_set(conn, "s5zo"), st["s5zo"]["set"],
                "premise: and s5zo's rows are the same rows, only reordered")

    expect.text(_seq(conn, "s5hi"), _seq(conn, "s5zo"),
                "over ONE column the two curves are the identity: the physical order "
                "is the same")
    # The bare inequality arm that used to sit here is gone: <NULL> != 'zorder'
    # passed it on a tree with no cluster_hilbert at all. The named pair is
    # strictly stronger and cannot be satisfied by a kind that was never written.
    expect.text(f"{_skind(conn, 's5hi')}/{_skind(conn, 's5zo')}", "hilbert/zorder",
                "and each records its own verb's kind")


# =============================================================================
# S6  vacuum_sorted MUST NOT CLOBBER A HILBERT TABLE
# =============================================================================
#
# The pinned value here is a READING of the owner's ruling, and this is the arm
# to change if the owner meant an honest relabel rather than a no-op.
#
# THE CALL IS ASSERTED, NOT MADE. A vacuum_sorted that RAISES on every Hilbert
# table leaves the order and the kind untouched, which is byte-identical to the
# no-op this section pins -- all of S6 was green on exactly that shim. The
# SQLSTATE arm is what tells the two apart.


@pytest.fixture(scope="module")
def s6(hc, pgc_cluster):
    st = {"t_groups": _mk4(hc, "s6t")}
    st["t_clustered"] = _sqlstate(pgc_cluster, "postgres",
                                  "SELECT pgcolumnar.cluster_hilbert('s6t','a','b');")
    st["t_kind_before"] = _skind(hc, "s6t")
    st["scan_ok"] = _is_columnar_scan(hc, "SELECT * FROM s6t")
    # THE ORDER DIGEST IS THE INSTRUMENT HERE, NOT physlayout. vacuum_sorted is
    # an EAGER verb, and an eager rewrite reproduces the stripe geometry exactly,
    # so the layout digest reads "unchanged" whether the table was clobbered or
    # left alone -- a check that cannot fail. S2's control measures that directly.
    st["t_seq"] = _seq(hc, "s6t")
    st["t_vacuumed"] = _sqlstate(pgc_cluster, "postgres",
                                 "SELECT pgcolumnar.vacuum_sorted('s6t','a');")

    st["c_groups"] = _mk4(hc, "s6c")
    st["c_seq"] = _seq(hc, "s6c")
    st["c_vacuumed"] = _sqlstate(pgc_cluster, "postgres",
                                 "SELECT pgcolumnar.vacuum_sorted('s6c','a');")
    yield hc, st
    hc.execute("DROP TABLE IF EXISTS s6t, s6c")


def test_vacuum_sorted_leaves_a_hilbert_table_alone(s6, expect):
    conn, st = s6
    expect.num(st["t_groups"], S4_GROUPS,
               "premise: set_options took on s6t, so it is 20 groups and 'nothing "
               "changed' cannot be true because there was nothing there")
    expect.text(st["t_clustered"], NOERROR,
                "cluster_hilbert('s6t','a','b') ran without raising")
    expect.text(st["t_kind_before"], "hilbert",
                "s6t is a hilbert table before vacuum_sorted touches it")
    expect.text(st["scan_ok"], "yes",
                "premise: the plan being digested for s6t is the columnar custom "
                "scan too")
    expect.text(st["t_vacuumed"], NOERROR,
                "premise: vacuum_sorted('s6t','a') on a hilbert table ran without "
                "raising")

    expect.text(_seq(conn, "s6t"), st["t_seq"],
                "vacuum_sorted leaves a hilbert table's physical ORDER identical: it "
                "did not re-sort it")
    expect.text(_skind(conn, "s6t"), "hilbert",
                "and leaves the recorded kind hilbert, not relabelled lexicographic")
    expect.text(_scalar(conn, "SELECT coalesce(sorted_kind::text,'<NULL>') FROM"
                              " pgcolumnar.sort_status('s6t')"), "hilbert",
                "and the reporter agrees with the catalog, so the two do not disagree "
                "about it")
    expect.num(_scalar(conn, "SELECT count(*) FROM s6t"), S4_ROWS,
               "and no row was lost")


def test_vacuum_sorted_still_works_on_a_table_with_no_recorded_kind(s6, expect):
    """The removal proof. Without it the arms above are satisfied by a
    vacuum_sorted that no-ops on EVERYTHING, which is a worse defect than the
    one they guard.
    """
    conn, st = s6
    expect.num(st["c_groups"], S4_GROUPS,
               "premise: set_options took on s6c, so it is 20 groups and 'nothing "
               "changed' cannot be true because there was nothing there")
    expect.text(st["c_vacuumed"], NOERROR,
                "vacuum_sorted('s6c','a') on a table with no recorded kind "
                "ran without raising")
    expect.text(_changed(st["c_seq"], _seq(conn, "s6c")), "moved",
                "control: vacuum_sorted still REORDERS a table with no recorded kind")
    expect.num(_scalar(conn, "SELECT count(*) FROM (SELECT a < lag(a) OVER () AS d"
                             " FROM s6c) z WHERE d"), 0,
               "control: and the rows really are ascending on a afterwards")
    expect.text(_skind(conn, "s6c"), "lexicographic",
                "control: and it still records its own kind there")


# =============================================================================
# S7  THE DAEMON PRESERVES THE CURVE
# =============================================================================
#
# This is the arm the ruling exists for, so it DRIVES THE DAEMON. The daemon
# builds its own call at src/columnar_autovacuum.c:283-291 and hard-codes
# `pgcolumnar.recluster`; calling recluster() from the test and reasoning about
# what the daemon would do answers the neighbouring question.
#
# Three twins from one fixture, with identical appended decay:
#
#   av_hi   hilbert, left decayed -- the DAEMON's subject
#   av_ref  hilbert, reclustered BY HAND -- what a Hilbert rewrite produces
#   av_zo   zorder,  reclustered BY HAND -- the control that makes "the layout
#           is a Hilbert layout" a discrimination rather than a restatement
#
# THE REFERENCES ARE FROZEN ACROSS THE DAEMON'S WINDOW. "av_hi came to match the
# hilbert twin" is also satisfied by the daemon reclustering the TWIN, which is
# what happened when a stubbed recluster_hilbert left av_ref's tail unfolded and
# therefore due. Their digests are captured before the daemon is enabled and
# asserted unchanged after, and the daemon's own LOG LINE names the dispatch, so
# the actor is read rather than inferred.
#
# WHERE THIS PORT DIFFERS FROM THE BASH SUITE, AND IT IS NOT COSMETIC. The bash
# suite hands its naptime and thresholds to the server through PGC_EXTRA_CONF
# before `pgc_setup`, so they are in postgresql.conf for the launcher, its
# workers and every session. `pgc_cluster.py` hard-codes its own conf and has no
# such hook. All four `pgcolumnar.autovacuum*` GUCs are PGC_SIGHUP
# (src/columnar_tableam.c:3262, 3272, 3281, 3290), so this fixture sets them by
# ALTER SYSTEM and reloads -- which reaches the launcher and its workers the same
# way. What it does NOT reproduce is the bash suite's guarantee that no fixture
# was ever built under a different threshold: here the tables are created first
# and the thresholds set before the daemon is turned on, so the window exists and
# is empty rather than impossible.

S7_ROWS = 20000
S7_TAIL = 5000
# The daemon's window: 15 polls of 2s against a 2s naptime. The MARGIN is the
# thing to watch rather than the bound -- see the poll counter in the fixture.
S7_POLLS = 15
S7_NAP = 2
# The bound the margin arm uses, kept separate from S7_POLLS so the window and the
# bound are not one number. Observed count: 1 on each of five IDLE majors, which is
# a single point rather than a spread. See the CHANGELOG entry for what that does
# and does not support.
S7_MARGIN = 5


def _mk7(conn, table):
    conn.execute(f"CREATE TABLE {table} (a int, b int, pad text) USING pgcolumnar")
    conn.execute(f"SELECT pgcolumnar.set_options('{table}',"
                 f" stripe_row_limit => {SR}, chunk_group_row_limit => {CG})")
    conn.execute(f"INSERT INTO {table} SELECT (g*2654435761)::bigint % 1000, g,"
                 f" md5(g::text) FROM generate_series(1,{S7_ROWS}) g")
    return _groups(conn, table)


def _show_fresh(cluster, guc):
    """Read a GUC on a NEW session, because a reload is not a read.

    `pg_reload_conf()` signals the postmaster; a backend that is already open
    absorbs the change at its next command boundary, and this module deliberately
    runs every statement through ONE connection. So `SHOW` on that connection can
    return the pre-reload value: measured here, `SHOW pgcolumnar.autovacuum`
    answered `off` immediately after `ALTER SYSTEM SET ... = on` and a reload, and
    the arm failed for a reason that had nothing to do with the daemon.

    The bash suite never meets this because every `q` is a fresh psql, which reads
    the current config at startup. A new session is therefore the port of what the
    original gets for free -- not a workaround, the same semantics.
    """
    import psycopg

    conn = psycopg.connect(cluster.dsn(), autocommit=True)
    try:
        row = conn.execute(f"SHOW {guc}").fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _logfile(cluster):
    return cluster.datadir / "server.log"


def _log_hits(cluster, pattern):
    """-> how many lines of the server log match, by regex.

    THE ACTOR IS READ, NOT INFERRED. "the tail folded" is also what
    compact_rewrite does, and it records 'zorder', so a counter alone cannot name
    which dispatch ran. src/columnar_autovacuum.c:291 emits the recluster line
    and :275 the compact_rewrite line, both at LOG, and both land here.
    """
    import re

    path = _logfile(cluster)
    if not path.exists():
        return -1
    return sum(1 for line in path.read_text(errors="replace").splitlines()
               if re.search(pattern, line))


@pytest.fixture(scope="module")
def s7(hc, pgc_cluster):
    import time

    st = {}
    # The thresholds the bash suite puts in PGC_EXTRA_CONF. SIGHUP, so a reload
    # reaches the launcher and its workers.
    for guc, val in (("pgcolumnar.autovacuum_naptime", "2"),
                     ("pgcolumnar.autovacuum_compact_threshold", "0.2"),
                     ("pgcolumnar.autovacuum_recluster_threshold", "0.05")):
        hc.execute(f"ALTER SYSTEM SET {guc} = {val}")
    hc.execute("ALTER SYSTEM SET pgcolumnar.autovacuum = off")
    hc.execute("SELECT pg_reload_conf()")

    st["launcher"] = _scalar(hc, "SELECT count(*) FROM pg_stat_activity WHERE"
                                 " backend_type = 'pgcolumnar autovacuum launcher'")
    st["off_before"] = _show_fresh(pgc_cluster, "pgcolumnar.autovacuum")
    # THE THRESHOLDS ARE ASSERTED, NOT SET AND HOPED FOR, and the reason is a
    # CLASS rather than this fixture's detail: WHEREVER A PORT REPLACES A
    # STRUCTURAL GUARANTEE WITH A PROCEDURAL ONE, IT OWES AN ARM THE ORIGINAL
    # DOES NOT NEED. The bash suite cannot run with these unset -- PGC_EXTRA_CONF
    # puts them in postgresql.conf before the postmaster starts, so the failure
    # is not available to it. ALTER SYSTEM plus a reload can silently not take.
    # THE ORIGINAL GETS THIS STRUCTURALLY AND THE PORT HAS TO ASSERT IT; that is
    # the price of the route, not an incidental variation, and the arm is not
    # redundant with this comment.
    #
    # With naptime at its default the daemon still eventually acts, the poll
    # below still sees the tail fold, and every arm in S7 passes while the values
    # this fixture claims to have put in force never were. Raised by @jdatcmd.
    #
    # `compare_to_bash.py` CANNOT SEE THIS CLASS: a structural precondition emits
    # no check name, so the parity tool reports no MISSING for it however widely
    # its extractor reads.
    st["gucs"] = "/".join(str(_show_fresh(pgc_cluster, g)) for g in (
        "pgcolumnar.autovacuum_naptime",
        "pgcolumnar.autovacuum_compact_threshold",
        "pgcolumnar.autovacuum_recluster_threshold"))

    # av_zd: a ZORDER table left decayed for the daemon (#1339 :1362). Every table the
    # daemon handled here used to be a hilbert one, so a daemon that reclustered
    # everything on the hilbert curve passed the whole section.
    for t, verb in (("av_hi", "cluster_hilbert"), ("av_ref", "cluster_hilbert"),
                    ("av_zo", "cluster"), ("av_zd", "cluster")):
        st[t] = {"groups": _mk7(hc, t)}
        st[t]["ran"] = _sqlstate(pgc_cluster, "postgres",
                                 f"SELECT pgcolumnar.{verb}('{t}','a','b');")
    for t in ("av_hi", "av_ref", "av_zo", "av_zd"):
        hc.execute(f"INSERT INTO {t} SELECT (g*2654435761)::bigint % 1000, g,"
                   f" md5(g::text) FROM generate_series(1,{S7_TAIL}) g")

    st["hi_decayed"] = (f"{_skind(hc, 'av_hi')}/"
                        f"{'decayed' if (_appended(hc, 'av_hi') or 0) > 0 else 'clean'}")
    st["hi_key"] = _skey(hc, "av_hi")
    st["zd_decayed"] = (f"{_skind(hc, 'av_zd')}/"
                        f"{'decayed' if (_appended(hc, 'av_zd') or 0) > 0 else 'clean'}")
    st["zd_due"] = _scalar(hc, "SELECT recluster_due::text || '/' ||"
                               " compact_rewrite_due::text FROM"
                               " pgcolumnar.maintenance_due('av_zd')")
    st["due"] = _scalar(hc, "SELECT recluster_due::text || '/' ||"
                            " compact_rewrite_due::text FROM"
                            " pgcolumnar.maintenance_due('av_hi')")
    st["scan_ok"] = _is_columnar_scan(hc, "SELECT * FROM av_hi")

    st["ref_ran"] = _sqlstate(pgc_cluster, "postgres",
                              "SELECT pgcolumnar.recluster_hilbert('av_ref','a','b');")
    st["zo_ran"] = _sqlstate(pgc_cluster, "postgres",
                             "SELECT pgcolumnar.recluster('av_zo','a','b');")
    st["ref_folded"] = _appended(hc, "av_ref")
    st["zo_folded"] = _appended(hc, "av_zo")
    st["refs_differ"] = _differs(_seq(hc, "av_ref"), _seq(hc, "av_zo"))
    st["pre"] = _differs(_seq(hc, "av_hi"), _seq(hc, "av_ref"))

    # FROZEN HERE. Everything below compares against these, not a fresh read, so
    # a daemon that rewrites a reference cannot make a comparison true by moving
    # the target.
    st["ref_seq"] = _seq(hc, "av_ref")
    st["ref_app"] = _appended(hc, "av_ref")
    st["zo_seq"] = _seq(hc, "av_zo")
    st["hi_set"] = _set(hc, "av_hi")

    hc.execute("ALTER SYSTEM SET pgcolumnar.autovacuum = on")
    hc.execute("SELECT pg_reload_conf()")
    st["on"] = _show_fresh(pgc_cluster, "pgcolumnar.autovacuum")

    # THE POLL COUNT IS RECORDED, NOT JUST THE OUTCOME. A fixture one poll from
    # its margin and a fixture fourteen from it produce identical greens, and only
    # the count distinguishes them -- so a drift toward the bound is invisible
    # unless the number is printed. Raised by @jdatcmd, who could not run this half
    # and said so rather than offering an opinion about it.
    st["after"] = _appended(hc, "av_hi")
    st["zd_after"] = _appended(hc, "av_zd")
    st["polls"] = 0
    for _ in range(S7_POLLS):
        time.sleep(S7_NAP)
        st["polls"] += 1
        st["after"] = _appended(hc, "av_hi")
        st["zd_after"] = _appended(hc, "av_zd")
        if st["after"] == 0 and st["zd_after"] == 0:
            break
    print(f"-- the daemon folded av_hi's tail after {st['polls']} poll(s) "
          f"of {S7_POLLS} at {S7_NAP}s")

    # THE SCHEMA IS THIS MODULE'S, NOT `public`. The bash suite builds in the
    # database's default schema and its patterns say `public.av_hi`; copying them
    # verbatim made every log arm read zero while the daemon had demonstrably
    # run -- the tail folded inside one naptime. Caught by the arm on its first
    # run, which is what the arm is for.
    st["reclog"] = _log_hits(
        pgc_cluster, rf"pgcolumnar autovacuum: recluster {SCHEMA}\.av_hi ")
    st["cmplog"] = _log_hits(
        pgc_cluster, rf"pgcolumnar autovacuum: compact_rewrite {SCHEMA}\.av_hi")
    st["reflog"] = _log_hits(
        pgc_cluster,
        rf"pgcolumnar autovacuum: (recluster|compact_rewrite) {SCHEMA}\.av_(ref|zo)")
    # A PATTERN THAT MATCHES NOTHING AND A DAEMON THAT DID NOTHING READ ALIKE.
    # The log must contain the daemon's own marker at all, or all three counts
    # above are zero for a reason that has nothing to do with which dispatch ran.
    st["anylog"] = _log_hits(pgc_cluster, r"pgcolumnar autovacuum: ")

    st["ruling"] = _skind(hc, "av_hi")
    st["ruling_reporter"] = _scalar(hc, "SELECT coalesce(sorted_kind::text,'<NULL>')"
                                        " FROM pgcolumnar.sort_status('av_hi')")
    st["hi_seq_after"] = _seq(hc, "av_hi")
    st["ref_seq_after"] = _seq(hc, "av_ref")
    st["ref_app_after"] = _appended(hc, "av_ref")
    st["rows"] = _scalar(hc, "SELECT count(*) FROM av_hi")
    st["zd_kind_key"] = f"{_skind(hc, 'av_zd')}/{_skey(hc, 'av_zd')}"
    st["hi_set_after"] = _set(hc, "av_hi")

    hc.execute("ALTER SYSTEM SET pgcolumnar.autovacuum = off")
    hc.execute("SELECT pg_reload_conf()")
    time.sleep(1)
    st["off_after"] = _show_fresh(pgc_cluster, "pgcolumnar.autovacuum")
    yield hc, st
    for guc in ("pgcolumnar.autovacuum", "pgcolumnar.autovacuum_naptime",
                "pgcolumnar.autovacuum_compact_threshold",
                "pgcolumnar.autovacuum_recluster_threshold"):
        hc.execute(f"ALTER SYSTEM RESET {guc}")
    hc.execute("SELECT pg_reload_conf()")
    hc.execute("DROP TABLE IF EXISTS av_hi, av_ref, av_zo, av_zd")


def test_the_daemon_fixtures_are_built_with_the_daemon_off(s7, expect):
    conn, st = s7
    expect.num(st["launcher"], 1, "premise: the maintenance launcher is running")
    expect.text(st["off_before"], "off",
                "premise: the daemon is OFF while the fixtures are built")
    # `2s`, not `2`: SHOW returns the unit. The arm caught that on its first
    # run, which is the cheapest possible demonstration that it reads the server
    # rather than restating the ALTER SYSTEM above it.
    expect.text(st["gucs"], "2s/0.2/0.05",
                "premise: and the naptime and both thresholds are the values this "
                "fixture set, read back from the server rather than assumed")
    for t in ("av_hi", "av_ref", "av_zo", "av_zd"):
        expect.num(st[t]["groups"], S4_GROUPS,
                   f"premise: set_options took on {t}, so it is 20 groups")
    expect.text(st["av_hi"]["ran"], NOERROR,
                "cluster_hilbert('av_hi','a','b') ran without raising")
    expect.text(st["av_ref"]["ran"], NOERROR,
                "cluster_hilbert('av_ref','a','b') ran without raising")
    expect.text(st["av_zo"]["ran"], NOERROR,
                "cluster('av_zo','a','b') ran without raising")
    expect.text(st["av_zd"]["ran"], NOERROR,
                "cluster('av_zd','a','b') ran without raising")
    expect.text(st["hi_decayed"], "hilbert/decayed",
                "av_hi is a hilbert table with appended decay for the daemon "
                "to find")
    expect.text(st["hi_key"], "{a,b}",
                "and the key the daemon will read off it is the one it was "
                "clustered by")
    # BOTH halves of the dispatch, so the recluster is its only reason to touch
    # av_hi: a compact_rewrite that folded the tail and relabelled the table
    # zorder would pass the arm below and redden THE RULING, and a reader would
    # attribute both to the recluster path.
    expect.text(st["due"], "true/false",
                "the daemon agrees a recluster is due, and that a compaction "
                "is NOT")
    expect.text(st["scan_ok"], "yes",
                "premise: the plan being digested for av_hi is the columnar custom "
                "scan too")


def test_the_zorder_table_is_decayed_and_due_for_the_daemon(s7, expect):
    conn, st = s7
    expect.text(st["zd_decayed"], "zorder/decayed",
                "av_zd is a zorder table with appended decay for the daemon to find")
    expect.text(st["zd_due"], "true/false",
                "and the daemon agrees av_zd's recluster is due, and that a compaction is NOT")


def test_the_daemon_reclusters_a_decayed_zorder_table_on_its_own_curve(s7, expect):
    """THE OTHER HALF OF THE DISPATCH (#1339 :1362). A daemon that sent every table to
    recluster_hilbert folded av_zd's tail just as well, so the curve and key are what
    tell the two apart."""
    conn, st = s7
    expect.num(st["zd_after"], 0,
               "the daemon reclustered the decayed zorder table av_zd too (its appended "
               "tail folded to 0)")
    expect.text(st["zd_kind_key"], "zorder/{a,b}",
                "and it kept av_zd on the zorder curve and its key: a non-hilbert table is "
                "not converted")


def test_the_two_hand_driven_references_are_built_and_folded(s7, expect):
    conn, st = s7
    expect.text(st["ref_ran"], NOERROR,
                "recluster_hilbert('av_ref','a','b') ran without raising")
    expect.text(st["zo_ran"], NOERROR,
                "recluster('av_zo','a','b') ran without raising")
    expect.num(st["ref_folded"], 0,
               "the hand-driven hilbert reference folded its tail back in")
    expect.num(st["zo_folded"], 0,
               "the hand-driven zorder control folded its tail back in")
    expect.text(st["pre"], "different",
                "av_hi does NOT yet match the hilbert reference, so matching "
                "it later is a change")


def test_the_two_references_are_two_different_layouts(s7, expect):
    """Gated for the reason S3's and S4(d)'s comparisons are: an av_ref that was
    never reclustered still carries its tail, so it differs from av_zo for a
    reason that has nothing to do with the curve.
    """
    conn, st = s7
    if st["ref_folded"] != 0 or st["zo_folded"] != 0:
        expect.cannot_run(
            "UNMET_PRECONDITION",
            f"a hand-driven reference did not fold its tail (av_ref=[{st['ref_folded']}], "
            f"av_zo=[{st['zo_folded']}]), so the two layouts are not the two rewrites")
        return
    expect.text(st["refs_differ"], "different",
                "the two references really are two different layouts, so the "
                "arm below discriminates")


def test_the_daemon_reclustered_the_decayed_hilbert_table(s7, expect):
    conn, st = s7
    expect.text(st["on"], "on", "the daemon is now ON")
    expect.num(st["after"], 0,
               "the daemon reclustered av_hi (its appended tail folded to 0)")
    # NOT a pin on the exact count, which would be flaky on a loaded box: a bound
    # comfortably inside the loop, so drift toward the margin reddens here while
    # the arm above still passes. MEASURED AT 1 ON FIVE IDLE MAJORS, which is one
    # value five times rather than a distribution -- the loop never waited, so
    # nothing in those runs exercised the waiting. 5 is four above the only value
    # ever observed and no loaded measurement exists; see the CHANGELOG entry.
    expect.text("inside" if st["polls"] <= S7_MARGIN else f"took {st['polls']} polls",
                "inside",
                "and it did so well inside the poll window, so this fixture is not "
                "sitting on its margin")
    expect.text("yes" if st["anylog"] >= 1 else "no", "yes",
                "premise: the server log carries the daemon's own dispatch lines at "
                "all, so a zero below is about which dispatch and not about the log")
    expect.text("yes" if st["reclog"] >= 1 else "no", "yes",
                "and the daemon's own log names the dispatch that did it: recluster "
                "on av_hi")
    expect.num(st["cmplog"], 0,
               "and no compaction dispatch touched av_hi, so the recluster path is "
               "the only candidate")
    expect.num(st["reflog"], 0,
               "and the daemon never touched either reference, so they are still the "
               "twins they were")
    expect.text(st["ref_seq_after"], st["ref_seq"],
                "and the hilbert reference is byte-for-byte what it was before the "
                "daemon ran")
    expect.num(st["ref_app_after"], st["ref_app"],
               "and it still has no appended tail, so nothing rewrote it behind the "
               "comparison")
    expect.text(st["ruling"], "hilbert",
                "THE RULING: the daemon left the table hilbert, it did not convert it "
                "to zorder")
    expect.text(st["ruling_reporter"], "hilbert",
                "and sort_status agrees with the catalog about it")
    expect.num(st["rows"], S7_ROWS + S7_TAIL, "and no row was lost in the process")
    expect.text(st["hi_set_after"], st["hi_set"],
                "and the rows are the same rows, only reordered")
    expect.text(st["off_after"], "off",
                "and the daemon is OFF again, so the suite ends on the invariant it "
                "opened with")


def test_the_layout_the_daemon_produced_is_a_hilbert_layout(s7, expect):
    conn, st = s7
    if st["pre"] != "different" or st["ref_folded"] != 0:
        expect.cannot_run(
            "UNMET_PRECONDITION",
            f"av_hi already matched the reference, or the reference was never rebuilt "
            f"(pre=[{st['pre']}], av_ref tail=[{st['ref_folded']}]), so the comparison "
            "cannot be a change")
        return
    expect.text(st["hi_seq_after"], st["ref_seq"],
                "and the layout the daemon produced IS a Hilbert layout: identical to "
                "the hand-driven hilbert twin")


def test_the_daemons_layout_is_not_the_zorder_layout(s7, expect):
    conn, st = s7
    if st["pre"] != "different" or st["ref_folded"] != 0:
        expect.cannot_run(
            "UNMET_PRECONDITION",
            f"av_hi already matched the reference, or the reference was never rebuilt "
            f"(pre=[{st['pre']}], av_ref tail=[{st['ref_folded']}]), so this "
            "discriminates nothing")
        return
    expect.text(_differs(st["hi_seq_after"], st["zo_seq"]), "different",
                "and it is NOT the zorder layout, which is what a hard-coded recluster "
                "would have left")


# =============================================================================
# S8  THE ENUMERATIONS REACT
# =============================================================================
#
# THE PROJECT RULE, restated because it has been lost three times: RESOLVE THE C
# SYMBOL FROM THE AS CLAUSE, NEVER BY DERIVING `pgcolumnar_<sqlname>`.
# `get_storage_id` (-> pgcolumnar_relation_storageid) and `columnar_handler`
# (-> pgcolumnar_handler) both break that convention and have been dropped by
# three separate enumerations that derived the name. So nothing below asserts
# that cluster_hilbert's symbol IS 'pgcolumnar_cluster_hilbert'. It asserts that
# whatever prosrc names is also declared in the script.


@pytest.fixture(scope="module")
def s8(hc):
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    control = (root / "pgcolumnar.control").read_text()
    version = re.search(r"^default_version *= *'(.*)'", control, re.M).group(1)
    sqlfile = root / f"pgcolumnar--{version}.sql"
    st = {"sqlfile": sqlfile, "exists": sqlfile.is_file()}
    if st["exists"]:
        # Comments blanked first: a doc comment naming a symbol is not a
        # declaration, and counting one is how a census comes out wrong in the
        # other direction.
        text = re.sub(r"--.*", "", sqlfile.read_text())
        st["src"] = sorted(set(re.findall(
            r"AS 'MODULE_PATHNAME'[,\s]*'([A-Za-z0-9_]*)'", text)))
    else:
        st["src"] = []
    rows = hc.execute(
        "SELECT DISTINCT p.prosrc FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'pgcolumnar' AND p.prolang ="
        " (SELECT oid FROM pg_language WHERE lanname = 'c')").fetchall()
    st["cat"] = sorted({r[0] for r in rows})
    return hc, st


def test_the_install_script_and_the_catalog_agree_on_the_symbol_set(s8, expect):
    conn, st = s8
    expect.text("yes" if st["exists"] else f"missing: {st['sqlfile']}", "yes",
                "premise: the install script derived from default_version exists")
    expect.text("yes" if len(st["src"]) >= 20
                else f"no ({len(st['src'])} symbols declared)", "yes",
                "premise: the install script declares MODULE_PATHNAME symbols at all")
    # THE TWO LISTS ARE COMPARED, NOT A TOKEN ABOUT THEM (#1339 cross-file 7): "same"
    # was also what two empty lists produced, and a failure now names the symbols
    # that differ rather than saying "differs".
    expect.text(" ".join(st["cat"]), " ".join(st["src"]),
                "the catalog and the install script still agree on the symbol set")


@pytest.mark.parametrize("fn", ["cluster_hilbert", "recluster_hilbert"])
def test_each_new_verb_is_installed_and_its_symbol_declared(s8, expect, fn):
    conn, st = s8
    expect.num(_scalar(conn, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                             " ON n.oid = p.pronamespace WHERE n.nspname ="
                             f" 'pgcolumnar' AND p.proname = '{fn}'"), 1,
               f"pgcolumnar.{fn} is installed, exactly once")
    sym = _scalar(conn, "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON"
                        " n.oid = p.pronamespace WHERE n.nspname = 'pgcolumnar'"
                        f" AND p.proname = '{fn}' AND p.prolang = (SELECT oid FROM"
                        " pg_language WHERE lanname = 'c')")
    expect.text("yes" if sym else "no symbol", "yes",
                f"{fn} is a C function and the catalog names its symbol")
    expect.text("declared" if sym in st["src"] else f"MISSING: {sym or '<none>'}",
                "declared",
                f"and the symbol the CATALOG names for {fn} is declared in the install "
                "script's AS clause")


# THE NAMES ARE SPELLED OUT rather than built from the pair. The bash side writes
# these two literally, so an f-string here templates to `{} has exactly {}'s ...`
# and matches neither -- the arms ran, passed, and read as MISSING.
_SIG_PAIRS = [
    ("cluster_hilbert", "cluster",
     "cluster_hilbert has exactly cluster's signature (args, VARIADIC element, "
     "return type)"),
    ("recluster_hilbert", "recluster",
     "recluster_hilbert has exactly recluster's signature (args, VARIADIC element, "
     "return type)"),
]


@pytest.mark.parametrize("new,old,name", _SIG_PAIRS, ids=[p[0] for p in _SIG_PAIRS])
def test_each_new_verb_has_its_siblings_signature(s8, expect, new, old, name):
    """Compared against the sibling verb rather than retyped: the argument types,
    the variadic element type and the return type must match the established verb
    the new one shadows.
    """
    conn, _ = s8

    def sigof(name):
        return _scalar(conn, "SELECT p.proargtypes::text || '|' ||"
                             " p.provariadic::text || '|' || p.prorettype::text"
                             " FROM pg_proc p JOIN pg_namespace n ON n.oid ="
                             f" p.pronamespace WHERE n.nspname = 'pgcolumnar'"
                             f" AND p.proname = '{name}'")

    expect.text(sigof(new), sigof(old), name)

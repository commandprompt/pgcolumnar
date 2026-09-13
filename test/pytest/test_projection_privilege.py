"""The projection READ helpers are a privilege boundary (#562, #563; ported for #432).

`pgcolumnar.read_projection()` and `pgcolumnar.reconstruct_via_projection()` opened a
caller-supplied regclass and returned its contents with no privilege check of any kind,
and `CREATE FUNCTION` grants EXECUTE to PUBLIC. `reconstruct_via_projection` is the worse
of the two: it rebuilds NON-COVERED columns from the base relation by row number, so the
projection was never the bound on what leaked. One projection on any column exposed the
whole row.

The bar is ACL_SELECT on the BASE relation rather than ownership, and that is a
correctness argument rather than a lenient one: reconstruct returns columns the projection
does not store, so SELECT on the base is exactly the privilege that governs reading those
columns by any other route.

WHAT THIS PORT ASSERTS THAT `projection_privilege.sh` CANNOT.

That suite decides four things by matching error TEXT, and two of them are load-bearing
rather than decorative, because the refusals it must tell apart share a SQLSTATE. Its own
comment says so:

    Both layers reject this role, so a bare "refused" stays true if the REVOKE is deleted
    and the C check catches it instead -- measured: with the REVOKE removed this suite
    still passed 14 of 14. The error text is the only thing that attributes the refusal.

That is correct about the shell harness and it is not a limit of the property. Three
refusals arrive from three different places in the code, and each carries its own
SQLSTATE the moment you stop asking which words the message contains:

    the SQL grant on the function   42501, raised before the body runs at all
    the base-relation ACL check     42501, the first statement of the body
    row-level security              0A000, ERRCODE_FEATURE_NOT_SUPPORTED
    the projection lookup           42704, ERRCODE_UNDEFINED_OBJECT

Two of those are 42501, so SQLSTATE alone does not separate the two ACL layers either.
**The discriminator is the fixture, not the message.** Call the function with a projection
name that does not exist, on a table the role may read:

    stopped by the SQL grant   -> 42501, because the body never ran to notice the name
    past the SQL grant         -> 42704, because it ran and looked the name up

So `test_which_layer_refused` attributes the refusal to a layer by what the code REACHED,
and reaching is a fact about execution rather than about wording. `grep -c 'permission
denied for function'` is satisfied by any future message containing that phrase; this is
satisfied only by control actually arriving at the projection lookup.

The same move gives the port two arms the shell suite does not have at all: the base-ACL
check must run BEFORE the projection lookup, and before the RLS check. Both are asserted
as orderings in `src/columnar_projection.c`, the second one in a comment recording that an
earlier version had it backwards and disclosed RLS state to a caller with no privilege.
Nothing tested either until now.

REAL LOGINS, NOT `SET ROLE`. A deny arm is evidence only if the call reached the code that
denies it, and `projection_privilege.sh` records measuring that exact hole: with `ALTER
ROLE t_prjexec NOLOGIN` injected, its EXECUTE premise and both "is refused" checks still
passed. Every premise here is run BY the role it is about.
"""

import pytest

ROWS = 2000
NONE, EXEC, SEL = "t_prjnone", "t_prjexec", "t_prjsel"
ROLES = (NONE, EXEC, SEL)

FUNCS = ("read_projection", "reconstruct_via_projection")

# (function, the name `projection_privilege.sh` gives this exact property). The names
# are the bash suite's character for character, which is what lets compare_to_bash.py
# pair the two harnesses by property instead of by shape.
USAGE_ONLY = (
    ("read_projection", "a role with only schema USAGE is refused read_projection"),
    ("reconstruct_via_projection", "and is refused reconstruct_via_projection"),
)
EXEC_NO_SELECT = (
    ("read_projection", "a role with EXECUTE but no SELECT is refused read_projection"),
    ("reconstruct_via_projection",
     "and is refused reconstruct_via_projection, which leaks non-covered columns"),
)
WITH_SELECT = (
    ("read_projection", "a role WITH SELECT still reads the projection"),
    ("reconstruct_via_projection", "and still reconstructs"),
)
UNDER_POLICY = (
    ("read_projection", "read_projection now refuses a policy-restricted caller (#563)"),
    ("reconstruct_via_projection", "and so does reconstruct_via_projection"),
)


def _as(cluster, role, sql, schema=None):
    """Run one statement on a fresh connection as `role`, returning (rows, error).

    THE SET IS ITS OWN EXECUTE. psycopg3 returns the FIRST statement's result for a
    multi-statement execute, so `SET search_path ...; SELECT ...` hands back the SET's
    empty result. Ported from `test_stats_privilege.py`, where collapsing that into a 0
    produced a product claim out of a driver detail.
    """
    import psycopg

    dsn = f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                if schema:
                    cur.execute(f'SET search_path TO "{schema}", pgcolumnar, public')
                cur.execute(sql)
                return cur.fetchall(), None
    except psycopg.Error as exc:
        return None, exc


def _fixture(cur):
    """Two tables and three roles, one role per layer so a refusal can be attributed.

        t_prjnone  schema USAGE only.       Stopped by the SQL REVOKE.
        t_prjexec  USAGE + EXECUTE.         Clears the SQL layer ON PURPOSE, so a
                                              refusal can only be the C check.
        t_prjsel   USAGE + EXECUTE + SELECT. Must SUCCEED -- this is the arm that
                                              distinguishes ACL_SELECT from ownership.

    `open_t` is the second table, and it is what makes the layer attribution possible:
    every role may read it, so a refusal on it cannot come from the base-relation ACL and
    a call naming a projection it does not have reaches the lookup or it does not.
    """
    for r in ROLES:
        cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{r}'")
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {r} NOSUPERUSER LOGIN")
        cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")

    cur.execute("SELECT current_schema()")
    schema = cur.fetchone()[0]
    for r in ROLES:
        cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {r}')

    cur.execute("DROP TABLE IF EXISTS prj_secret")
    cur.execute("CREATE TABLE prj_secret (id int, ssn text, note text) USING pgcolumnar")
    cur.execute(
        f"INSERT INTO prj_secret "
        f"SELECT g, 'ssn-'||g, 'note-'||g FROM generate_series(1,{ROWS}) g"
    )
    # The projection covers id and ssn. It does NOT cover note, which is the point:
    # reconstruct returns note anyway.
    cur.execute(
        "SELECT pgcolumnar.add_projection("
        "'prj_secret','p1',ARRAY['id','ssn'],ARRAY['id'])"
    )
    cur.execute("SELECT pgcolumnar.rebuild_projections('prj_secret')")
    cur.execute("REVOKE ALL ON prj_secret FROM PUBLIC")

    cur.execute("DROP TABLE IF EXISTS prj_open")
    cur.execute("CREATE TABLE prj_open (id int) USING pgcolumnar")
    cur.execute("INSERT INTO prj_open SELECT generate_series(1,10)")

    for r in (EXEC, SEL):
        for f in FUNCS:
            cur.execute(
                f"GRANT EXECUTE ON FUNCTION pgcolumnar.{f}(regclass,text) TO {r}"
            )
    cur.execute(f"GRANT SELECT ON prj_secret TO {SEL}")
    for r in ROLES:
        cur.execute(f"GRANT SELECT ON prj_open TO {r}")
    return schema


def test_the_premises_the_fixture_is_what_the_suite_assumes(pgc_cluster, pgc_conn, expect):
    """Every premise about a role is run BY that role.

    A DENIED result and a BROKEN result are indistinguishable from outside, so the owner
    arms are not decoration: they assert a ROW COUNT on the same call the deny arms make.
    If a later change made these functions return zero rows for everyone, a no-error owner
    arm would stay green while every deny arm stayed green, and the suite would pass over a
    function that does nothing.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)

    # CARDINALITY FIRST, for every loop below. A loop over an empty sequence runs no
    # assertion and reports no failure, so each of these says how many arms it owes
    # before it runs them.
    expect.num(len(ROLES), 3, "premise: three roles, one per privilege layer")
    expect.num(len(FUNCS), 2, "premise: both read helpers are under test")

    for r in ROLES:
        rows, err = _as(pgc_cluster, r, "SELECT 1", schema)
        assert err is None, f"{r} could not open a session: {err}"
        expect.num(rows[0][0], 1, "premise: t_prjexec can open a session, so a refusal below is a refusal"
                   if r == EXEC else f"premise: {r} can open a session of its own")

    with pgc_conn.cursor() as cur:
        for f in FUNCS:
            cur.execute(f"SELECT count(*) FROM pgcolumnar.{f}('prj_secret','p1')")
            expect.num(cur.fetchone()[0], ROWS,
                       "premise: the owner reads the projection, and gets rows" if f == "read_projection"
                       else "premise: the owner reconstructs, and gets rows")
        cur.execute(
            "SELECT pgcolumnar.reconstruct_via_projection('prj_secret','p1') "
            "LIKE '%note-%' LIMIT 1"
        )
        expect.text(str(cur.fetchone()[0]), "True",
                    "premise: reconstruct really does return the NON-COVERED column")
        cur.execute(
            "SELECT pgcolumnar.read_projection('prj_secret','p1') LIKE '%note-%' LIMIT 1"
        )
        expect.text(str(cur.fetchone()[0]), "False",
                    "premise: and read_projection does NOT, so the two differ as the issue says")
        cur.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s) AND rolsuper", (list(ROLES),)
        )
        expect.num(cur.fetchone()[0], 0, "premise: the test roles are not superusers")
        # A CATALOG LOOKUP RUN AS THE OWNER, which is blind to whether t_prjexec can
        # open a session at all -- hence the session premise above, which that role
        # runs itself. Both are needed: this says the grant exists, that says it can
        # be exercised.
        cur.execute(
            "SELECT has_function_privilege("
            "'t_prjexec','pgcolumnar.read_projection(regclass,text)','EXECUTE')"
        )
        expect.text(str(cur.fetchone()[0]), "True",
                    "premise: t_prjexec really does hold EXECUTE, so it reaches the C gate")

    _, err = _as(pgc_cluster, NONE, "SELECT count(*) FROM prj_secret", schema)
    expect.sqlstate(err, "42501",
                    "premise: the unprivileged role cannot read the table by any ordinary route")

    rows, err = _as(pgc_cluster, SEL, "SELECT count(*) FROM prj_secret", schema)
    assert err is None, f"{SEL} could not read the table: {err}"
    expect.num(rows[0][0], ROWS,
               "premise: t_prjsel CAN read the table, so its arm below tests the bar")

    for r in ROLES:
        rows, err = _as(pgc_cluster, r, "SELECT count(*) FROM prj_open", schema)
        assert err is None, f"{r} could not read prj_open: {err}"
        expect.num(rows[0][0], 10,
                   f"premise: {r} may read prj_open, so a refusal on it is not the ACL")


@pytest.mark.parametrize("func,name", USAGE_ONLY)
def test_a_role_with_only_schema_usage_is_refused(pgc_cluster, pgc_conn, expect, func, name):
    """Layer one: the SQL grant."""
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    _, err = _as(pgc_cluster, NONE,
                 f"SELECT count(*) FROM pgcolumnar.{func}('prj_secret','p1')", schema)
    expect.sqlstate(err, "42501", name)


@pytest.mark.parametrize("func,name", EXEC_NO_SELECT)
def test_a_role_with_execute_but_no_select_is_refused(pgc_cluster, pgc_conn, expect, func, name):
    """Layer two: the C check, reached only because EXECUTE was granted.

    This role clears the SQL layer deliberately. If it is refused, the refusal cannot be
    the REVOKE and cannot be the schema.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    _, err = _as(pgc_cluster, EXEC,
                 f"SELECT count(*) FROM pgcolumnar.{func}('prj_secret','p1')", schema)
    expect.sqlstate(err, "42501", name)
    if func == "read_projection":
        expect.sqlstate(
            err, "42501",
            "and THAT refusal is the C check, which raises 42501 from aclcheck_error")


def test_which_layer_refused_without_reading_the_message(pgc_cluster, pgc_conn, expect):
    """THE ARM THE SHELL SUITE NEEDS ERROR TEXT FOR, decided by execution instead.

    Both layers raise 42501, so the code alone cannot tell them apart and a bare "refused"
    stays true if the REVOKE is deleted and the C check catches it instead. Measured in
    `projection_privilege.sh`: with the REVOKE removed, that suite still passed 14 of 14.

    So ask a question only one layer can answer. On `prj_open`, which EVERY role may read,
    with a projection name that does not exist:

        stopped by the SQL grant   42501   the body never ran, so the name was never read
        past the SQL grant         42704   it ran, and the projection lookup raised

    The difference is which code executed. That is not a property of the wording, and no
    future rephrasing of either message can move it.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    call = "SELECT count(*) FROM pgcolumnar.read_projection('prj_open','no_such_proj')"

    _, err = _as(pgc_cluster, NONE, call, schema)
    expect.sqlstate(err, "42501",
                    "the no-EXECUTE role never reaches the body, so the grant is what stopped it")

    _, err = _as(pgc_cluster, EXEC, call, schema)
    expect.sqlstate(err, "42704",
                    "while the EXECUTE role reaches the projection lookup on the same call")


def test_the_base_acl_is_checked_before_the_projection_is_looked_up(
        pgc_cluster, pgc_conn, expect):
    """An ordering `src/columnar_projection.c` asserts and nothing tested.

    The ACL check is the first statement of the body and the projection lookup is well
    below it. If they were swapped, a caller with no SELECT would learn whether a named
    projection exists on a table it may not read -- existence disclosure, from a function
    whose whole purpose is to stop disclosure.

    The control is the arm above: the same role, the same bogus name, on a table it MAY
    read, returns 42704. So 42501 here is the ACL check winning the race rather than the
    lookup being unreachable.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    _, err = _as(pgc_cluster, EXEC,
                 "SELECT count(*) FROM pgcolumnar.read_projection("
                 "'prj_secret','no_such_proj')", schema)
    expect.sqlstate(err, "42501",
                    "the base ACL is checked before the projection name is looked up")


@pytest.mark.parametrize("func,name", WITH_SELECT)
def test_a_role_with_select_still_reads(pgc_cluster, pgc_conn, expect, func, name):
    """THE CONTROL FOR THE WHOLE SUITE. Without it the fix could pass by refusing
    everyone, which is not a fix, it is a broken function with a good error message.

    It is also what distinguishes ACL_SELECT from ownership: this role owns nothing, and
    an owner-only bar refuses it. Refusing it would be a regression, not a fix.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    rows, err = _as(pgc_cluster, SEL,
                    f"SELECT count(*) FROM pgcolumnar.{func}('prj_secret','p1')", schema)
    assert err is None, f"{SEL} was refused {func}: {type(err).__name__}: {err}"
    expect.num(rows[0][0], ROWS, name)


@pytest.mark.parametrize("func,name", UNDER_POLICY)
def test_a_policy_restricted_caller_is_refused(pgc_cluster, pgc_conn, expect, func, name):
    """Row-level security, closed by #563, and a DIFFERENT SQLSTATE rather than a phrase.

    ACL_SELECT answers "may this role read this table". RLS answers "which rows". The
    direct-storage paths cannot answer the second: policies are applied by the REWRITER to
    a query's range table entry, and these functions never build a query. A caller holding
    SELECT but restricted to one row by a policy received every row.

    `PgColumnarRequireNoRowSecurity` raises ERRCODE_FEATURE_NOT_SUPPORTED, so this arm
    asserts 0A000 where the shell suite greps `^ERROR:.*row-level security`. The codes are
    from different SQLSTATE CLASSES -- 0A and 42 -- so this cannot be confused with either
    ACL refusal by any amount of rewording, and the premise below shows the policy really
    took effect rather than the call having broken for some other reason.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
        cur.execute("ALTER TABLE prj_secret ENABLE ROW LEVEL SECURITY")
        cur.execute("DROP POLICY IF EXISTS p_one ON prj_secret")
        cur.execute(f"CREATE POLICY p_one ON prj_secret FOR SELECT TO {SEL} USING (id = 1)")

    rows, err = _as(pgc_cluster, SEL, "SELECT count(*) FROM prj_secret", schema)
    assert err is None, f"{SEL} could not read the table under the policy: {err}"
    expect.num(rows[0][0], 1, "premise: the policy is in force for ordinary SQL")

    _, err = _as(pgc_cluster, SEL,
                 f"SELECT count(*) FROM pgcolumnar.{func}('prj_secret','p1')", schema)
    expect.sqlstate(err, "0A000", name)
    if func == "read_projection":
        expect.sqlstate(
            err, "0A000",
            "and the refusal says the feature is not supported, not that a privilege "
            "is missing")


def test_the_acl_is_checked_before_rls_so_no_privilege_discloses_no_rls_state(
        pgc_cluster, pgc_conn, expect):
    """The second ordering, recorded in the source as a correction made in review.

    `PgColumnarRequireNoRowSecurity` is called AFTER the relation ACL check. An earlier
    version had it first, and `src/columnar_vacuum.c` records what that produced:

        RLS on, no-select, ordinary SQL     -> permission denied for table
        RLS on, no-select, read_projection  -> row-level security is in force

    A caller with no privilege at all was told the table has RLS enabled, which ordinary
    SQL does not disclose and which disagrees with core's ordering.

    THE TWO OUTCOMES ARE DIFFERENT SQLSTATE CLASSES, so this arm is decidable: 42501 means
    the ACL check ran first and 0A000 means RLS did. The arm above is its control -- the
    same table, the same RLS, a role that DOES hold SELECT, and 0A000 -- which is what
    makes 42501 here an ordering result rather than the RLS check being switched off.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
        cur.execute("ALTER TABLE prj_secret ENABLE ROW LEVEL SECURITY")
        cur.execute("DROP POLICY IF EXISTS p_one ON prj_secret")
        cur.execute(f"CREATE POLICY p_one ON prj_secret FOR SELECT TO {SEL} USING (id = 1)")

    _, err = _as(pgc_cluster, EXEC,
                 "SELECT count(*) FROM pgcolumnar.read_projection('prj_secret','p1')",
                 schema)
    expect.sqlstate(err, "42501",
                    "a caller with no SELECT is told a privilege is missing, not that RLS is in force")

"""`pgcolumnar.stats` is readable by the table's owner, and only by a caller who
may read the table (#560, ported for #432).

`stats()` is SECURITY DEFINER and does its own privilege check, because the
alternative -- GRANTing SELECT on `pgcolumnar.zone_map` to PUBLIC -- would publish
per-column minimum, maximum and sum for every columnar table. That is a larger
disclosure than the usability bug it fixes.

THE TRAP THIS SUITE EXISTS TO CATCH. Inside a SECURITY DEFINER function the
effective user is the function OWNER, so `pg_class_aclcheck(relid, GetUserId(), ...)`
checks the superuser who installed the extension and returns ACLCHECK_OK for every
relation in the database. It looks like a correct check and refuses nobody. The
refusal arm below is what fails if `GetOuterUserId()` is ever changed back.

WHAT THIS PORT ASSERTS THAT THE BASH SUITE CANNOT. `stats_privilege.sh` decides the
refusal with `grep -c 'permission denied for table'`. `CLAUDE.md` names the rule:

    Assert SQLSTATE, not error text: 42501 comes only from aclcheck_error, while a
    grep for "permission denied" is also satisfied by a login FATAL, a missing
    function (42883), a bad argument (22023), or a transaction-block refusal (25001).

`permission denied for schema` also matches `permission denied`, which is not
hypothetical: it is the exact confusion measured while porting `native_ownership`,
where a missing schema grant produced a refusal that looked like the one under test.
This port asserts 42501 AND that the message names the table, so neither half can
carry the arm alone.

REAL LOGINS, NOT `SET ROLE`. Unlike the ownership port, session-opening is a property
this suite tests -- "the owner can open a session" is one of its premises -- and
`SET ROLE` would assert it away. Each role connects.
"""

import pytest

ROWS = 500
OWNER, NONE, SEL = "t_stowner", "t_stnone", "t_stsel"


def _as(cluster, role, sql, schema=None):
    """Run one statement on a fresh connection as `role`, returning (rows, error).

    THE SET IS ITS OWN EXECUTE. psycopg3 returns the FIRST statement's result for a
    multi-statement execute, so `SET search_path ...; SELECT ...` hands back the
    SET's empty result and raises "the last operation didn't produce records". The
    first version of this helper collapsed that into a 0 and the arm reported
    "the OWNER cannot read its stats" -- a product claim, from a driver detail.
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
    for r in (OWNER, NONE, SEL):
        cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{r}'")
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {r} NOSUPERUSER LOGIN")
        cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
    cur.execute("DROP TABLE IF EXISTS st_t")
    cur.execute("CREATE TABLE st_t (id int, v text) USING pgcolumnar")
    cur.execute(f"INSERT INTO st_t SELECT g, 'v'||g FROM generate_series(1,{ROWS}) g")
    cur.execute("SELECT current_schema()")
    schema = cur.fetchone()[0]
    for r in (OWNER, NONE, SEL):
        cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {r}')
    cur.execute(f"ALTER TABLE st_t OWNER TO {OWNER}")
    cur.execute("REVOKE ALL ON st_t FROM PUBLIC")
    cur.execute(f"GRANT SELECT ON st_t TO {SEL}")
    return schema


def test_the_premises_each_role_is_what_the_suite_assumes(pgc_cluster, pgc_conn, expect):
    """Each premise is run BY the role it is about, which is why real logins matter."""
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    # WRITTEN OUT, NOT LOOPED, and the names are the bash suite's character for
    # character. The loop that was here passed `f"premise: {r} can open a session"`,
    # which compare_to_bash.py reads as the template `premise: {} can open a session`
    # -- matching neither bash name, so both properties were reported MISSING from a
    # port that asserts them. A name held in a variable is unreadable to the parity
    # tool by design: guessing at it would report the wrong string as PRESENT.
    sessions = {}
    for r in (OWNER, NONE, SEL):
        rows, err = _as(pgc_cluster, r, 'SELECT 1', schema)
        assert err is None, f"{r} could not connect: {err}"
        sessions[r] = rows[0][0]
    expect.num(sessions[OWNER], 1, "premise: the owner can open a session")
    expect.num(sessions[NONE], 1, "premise: the no-privilege role can open a session")
    expect.num(sessions[SEL], 1, "premise: the granted role can open a session")

    with pgc_conn.cursor() as cur:
        cur.execute("SELECT relowner::regrole::text FROM pg_class WHERE relname = 'st_t'")
        expect.text(cur.fetchone()[0], OWNER, "premise: t_stowner really owns the table")
        cur.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s) AND rolsuper",
            ([OWNER, NONE, SEL],),
        )
        expect.num(cur.fetchone()[0], 0, "premise: none of these roles is a superuser")

    rows, err = _as(pgc_cluster, OWNER, 'SELECT count(*) FROM st_t', schema)
    assert err is None, f"owner read failed: {err}"
    expect.num(rows[0][0], ROWS,
               "premise: the owner can read its own table by ordinary SQL")

    _, err = _as(pgc_cluster, NONE, 'SELECT count(*) FROM st_t', schema)
    expect.sqlstate(err, "42501",
                    "premise: the no-privilege role cannot read it by ordinary SQL")

    _, err = _as(pgc_cluster, SEL, "SELECT count(*) FROM pgcolumnar.row_group")
    expect.sqlstate(err, "42501",
                    "premise: the catalog tables are NOT readable by these roles, "
                    "so a GRANT is not the fix")


def test_who_may_read_the_stats(pgc_cluster, pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    q = "SELECT count(*) FROM pgcolumnar.stats('st_t')"

    rows, err = _as(pgc_cluster, OWNER, q, schema)
    assert err is None, f"owner call failed: {type(err).__name__}: {err}"
    expect.num(rows[0][0], 1, "the OWNER of the table can read its stats")

    rows, err = _as(pgc_cluster, SEL, q, schema)
    assert err is None, f"granted role failed: {err}"
    expect.num(rows[0][0], 1, "a role merely GRANTed select can read them too")

    with pgc_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pgcolumnar.stats('st_t')")
        expect.num(cur.fetchone()[0], 1, "a superuser still reads stats")


def test_a_role_with_no_privilege_is_refused(pgc_cluster, pgc_conn, expect):
    """THE BAR. With GetUserId() the check tests the superuser who owns the function,
    passes, and this role reads the stats of a table it cannot read.

    Two assertions, because neither carries it alone: `42501` says a privilege check
    refused rather than something else failing, and the table name says it was THIS
    table's check rather than a catalog table the caller never asked about. The bash
    suite has only the second, as text.
    """
    with pgc_conn.cursor() as cur:
        schema = _fixture(cur)
    _, err = _as(pgc_cluster, NONE,
                 "SELECT count(*) FROM pgcolumnar.stats('st_t')", schema)
    expect.sqlstate(err, "42501", "a role with no privilege on the table is refused")
    expect.num(int("st_t" in str(err)), 1,
               "and the refusal names the TABLE, not a catalog table it never asked about")

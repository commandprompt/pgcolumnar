"""pgcolumnar.sort_status() answers whoever may SELECT the table, and refuses everyone
else by naming the table (#608). The pytest twin of `test/sort_status_privilege.sh`,
every check by the shell's name (#1341).

sort_status reads pgcolumnar's internal catalogs, which carry no GRANT. As an
invoker-rights function it false-denied a table's own owner on their own table. It is
SECURITY DEFINER now, and require_caller_select gates the real caller, so definer rights
do not widen who may read it. Asserted, as three non-superuser roles (the owner, a role
granted SELECT, and a role with nothing): the owner and the grantee read one row and the
owner sees its sorted run; the role with nothing is refused, and the refusal names the
table rather than a catalog it never asked about; a superuser still reads it.

ON A PRIVATE DATABASE, with the three roles created for this module and dropped after.
A call made as a role reads the first column of its first row, or the first line of
its failure as "SQLSTATE <code>: <message>", so a refusal is matched on its message as
in the shell.
"""
import psycopg
import pytest

DB = "pgc_sort_status_privilege"
ROWS = 500
ROLES = ("t_ssowner", "t_ssnone", "t_sssel")


def _as(dsn, role, sql):
    """-> the first column of the first row as text, or the failure as "SQLSTATE <code>:
    <message>", which the refusal checks match on as the shell greps psql's output."""
    try:
        with psycopg.connect(dsn.replace("user=postgres", f"user={role}"), autocommit=True,
                             connect_timeout=5) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return f"SQLSTATE {exc.sqlstate}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
    return "" if row is None or row[0] is None else str(row[0])


def _refused(reading, text):
    """-> 1 when the reading is a failure carrying `text`, else 0, as the shell's grep -c."""
    return 1 if reading.startswith("SQLSTATE ") and text in reading else 0


@pytest.fixture(scope="module")
def ss(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        for r in ROLES:
            a.execute(f"DROP ROLE IF EXISTS {r}")
        a.execute(f"CREATE DATABASE {DB}")
        for r in ROLES:
            a.execute(f"CREATE ROLE {r} NOSUPERUSER LOGIN")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            for r in ROLES:
                conn.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
            conn.execute("CREATE TABLE ss_t (id int, ts int) USING pgcolumnar")
            conn.execute(f"INSERT INTO ss_t SELECT g, g FROM generate_series(1,{ROWS}) g")
            conn.execute("SELECT pgcolumnar.vacuum_sorted('ss_t','ts')")
            conn.execute("ALTER TABLE ss_t OWNER TO t_ssowner")
            conn.execute("REVOKE ALL ON ss_t FROM PUBLIC")
            conn.execute("GRANT SELECT ON ss_t TO t_sssel")
            conn.execute("CREATE TABLE ss_h (id int)")   # a heap table, readable by no test role
            conn.execute("REVOKE ALL ON ss_h FROM PUBLIC")
            owner = conn.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='ss_t'").fetchone()[0]
            nosuper = conn.execute("SELECT bool_and(NOT rolsuper) FROM pg_roles WHERE rolname LIKE 't_ss%'").fetchone()[0]
            superuser = conn.execute("SELECT count(*) FROM pgcolumnar.sort_status('ss_t')").fetchone()[0]
        status = "SELECT count(*) FROM pgcolumnar.sort_status('ss_t')"
        none_status = _as(dsn, "t_ssnone", status)
        yield {
            "owner session": _as(dsn, "t_ssowner", "SELECT 1"),
            "none session": _as(dsn, "t_ssnone", "SELECT 1"),
            "owner": owner,
            "nosuper": "t" if nosuper else "f",
            "owner reads": _as(dsn, "t_ssowner", "SELECT count(*) FROM ss_t"),
            "none reads": _refused(_as(dsn, "t_ssnone", "SELECT count(*) FROM ss_t"), "permission denied"),
            "catalog": _refused(_as(dsn, "t_sssel", "SELECT count(*) FROM pgcolumnar.row_group"), "permission denied"),
            "owner status": _as(dsn, "t_ssowner", status),
            "sel status": _as(dsn, "t_sssel", status),
            "owner run": _as(dsn, "t_ssowner", "SELECT (sorted_groups > 0)::text FROM pgcolumnar.sort_status('ss_t')"),
            "none refused": _refused(none_status, "permission denied for table"),
            "none names table": _refused(none_status, "ss_t"),
            "super status": superuser,
            "none heap": _refused(_as(dsn, "t_ssnone", "SELECT count(*) FROM pgcolumnar.sort_status('ss_h')"),
                                  "permission denied for table ss_h"),
            "super heap": _refused(_as(dsn, "postgres", "SELECT count(*) FROM pgcolumnar.sort_status('ss_h')"),
                                   'relation "ss_h" is not a columnar table'),
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            for r in ROLES:
                a.execute(f"DROP ROLE IF EXISTS {r}")


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def test_the_owner_can_open_a_session(ss, expect):
    expect.text(ss["owner session"], "1", "premise: the owner can open a session")


def test_the_no_privilege_role_can_open_a_session(ss, expect):
    expect.text(ss["none session"], "1", "premise: the no-privilege role can open a session")


def test_the_owner_really_owns_the_table(ss, expect):
    expect.text(ss["owner"], "t_ssowner", "premise: t_ssowner really owns the table")


def test_none_of_the_roles_is_a_superuser(ss, expect):
    expect.text(ss["nosuper"], "t", "premise: none of these roles is a superuser")


def test_the_owner_can_read_its_table(ss, expect):
    expect.text(ss["owner reads"], str(ROWS), "premise: the owner can read its own table by ordinary SQL")


def test_the_no_privilege_role_cannot_read_the_table(ss, expect):
    expect.num(ss["none reads"], 1, "premise: the no-privilege role cannot read it by ordinary SQL")


def test_the_catalogs_are_not_readable(ss, expect):
    expect.num(ss["catalog"], 1,
               "premise: the catalog tables are NOT readable by these roles, so a GRANT is not the fix")


def test_the_owner_can_read_its_sort_status(ss, expect):
    expect.num(_int(ss["owner status"]), 1, "the OWNER of the table can read its sort status")


def test_a_grantee_can_read_the_sort_status(ss, expect):
    expect.num(_int(ss["sel status"]), 1, "a role merely GRANTed select can read it too")


def test_the_owner_sees_its_sorted_run(ss, expect):
    expect.text(ss["owner run"], "true", "the owner sees its sorted run, not a false empty")


def test_a_role_with_no_privilege_is_refused_its_sort_status(ss, expect):
    expect.num(ss["none refused"], 1, "a role with no privilege on the table is refused")


def test_the_refusal_names_the_table(ss, expect):
    expect.num(ss["none names table"], 1, "and the refusal names the TABLE, not a catalog table it never asked about")


def test_a_superuser_still_reads_the_sort_status(ss, expect):
    expect.num(ss["super status"], 1, "a superuser still reads sort status")


def test_a_heap_table_is_refused_for_privilege_before_its_kind_is_read(ss, expect):
    """get_storage_id checks the DEFINER inside this SECURITY DEFINER function, so only
    the explicit caller check refuses, and only while it runs first (#1657 review)."""
    expect.num(ss["none heap"], 1,
               "a role with no privilege on a heap table is refused for privilege, not told it is not columnar")


def test_a_superuser_is_told_a_heap_table_is_not_columnar(ss, expect):
    expect.num(ss["super heap"], 1, "a superuser asking about a heap table is told it is not columnar")

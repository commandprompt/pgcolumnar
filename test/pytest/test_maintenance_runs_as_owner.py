"""A maintenance verb runs the table's own code as the table's owner (#2053).

An online rewrite moves live rows into fresh groups and puts each one back into
every index itself (`src/columnar_index.c`, the rewrite route). That evaluates
each index expression and each partial-index predicate, which are functions the
TABLE OWNER chose. They ran as whoever called the verb. The maintenance daemon
calls `compact_rewrite`, `recluster` and `expire` as the bootstrap superuser, and
a superuser may call any verb on any table, so an unprivileged owner's function
ran with superuser rights. That is the hole core closed for VACUUM, CLUSTER,
REINDEX and CREATE INDEX (CVE-2022-1552): `index_build` and `cluster_rel` switch
to the owner with SECURITY_RESTRICTED_OPERATION, on every supported major.

The property is measured, not inferred. `mallory` owns the table and is nothing
else. Her function is the expression of one index and the predicate of another,
and it raises a NOTICE naming `current_user` and whether the operation is
security-restricted. A superuser runs each verb, and every notice the verb raises
must name mallory, restricted. A premise per verb: her function ran at least once,
so an empty set of users cannot pass.

The verbs that rebuild through core (vacuum, vacuum_sorted, cluster) already
switched for the index build. They are here so that every row-rewriting verb is
held to the same answer, whichever route it takes. compact and expire are not:
they retire whole groups and evaluate no index code (measured: zero notices).
"""

import psycopg
import pytest

ROLE = "pgc_mallory"
TAG = "pgc2053"

# Each verb, with arguments valid for the owner, in an order the fixture survives.
VERBS = [
    "compact_rewrite('m', 0.0)",
    "recluster('m', 'id')",
    "recluster_hilbert('m', 'id', 'v')",
    "vacuum('m')",
    "vacuum_sorted('m', 'id')",
    "cluster('m', 'id')",
    "cluster_hilbert('m', 'id', 'v')",
]


def _fixture(cur):
    """mallory's table, with her function in an index expression and a predicate.

    Rows land in three groups and a third of each group is deleted, so every
    rewrite verb has a partial group to move and recluster has groups to merge.
    The role is cluster-global and the tests run in parallel, so it
    is created idempotently and never dropped.
    """
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
    if cur.fetchone() is None:
        cur.execute(f"CREATE ROLE {ROLE} NOSUPERUSER")
    cur.execute("SELECT current_schema()")
    schema = cur.fetchone()[0]
    cur.execute(f'GRANT USAGE ON SCHEMA pgcolumnar, "{schema}" TO {ROLE}')
    cur.execute("DROP TABLE IF EXISTS m")
    # The probe for a security-restricted operation: setting `role` is refused
    # inside one. The block always ends in an exception, so the setting is rolled
    # back whichever way it went.
    cur.execute(
        "CREATE OR REPLACE FUNCTION seen(x int) RETURNS int IMMUTABLE LANGUAGE plpgsql AS $$"
        " DECLARE why text; BEGIN"
        "  BEGIN PERFORM pg_catalog.set_config('role', current_user, true);"
        "   RAISE EXCEPTION 'unrestricted';"
        "  EXCEPTION WHEN OTHERS THEN why := SQLERRM; END;"
        f" RAISE NOTICE '{TAG} user=% restricted=%', current_user,"
        "   CASE WHEN why LIKE '%security-restricted%' THEN 'yes' ELSE 'no' END;"
        " RETURN x; END $$")
    cur.execute(f"ALTER FUNCTION seen(int) OWNER TO {ROLE}")
    cur.execute("CREATE TABLE m (id int, v int) USING pgcolumnar")
    cur.execute(
        "SELECT pgcolumnar.set_options('m', stripe_row_limit => 1000,"
        " chunk_group_row_limit => 1000)")
    cur.execute(
        "INSERT INTO m SELECT g, (g * 7919) % 3000 FROM generate_series(1, 3000) g")
    cur.execute("CREATE INDEX m_expr ON m ((seen(v)))")
    cur.execute("CREATE INDEX m_part ON m (id) WHERE seen(id) > 0")
    cur.execute("DELETE FROM m WHERE id % 3 = 0")
    cur.execute(f"ALTER TABLE m OWNER TO {ROLE}")


def _run_as_superuser(conn, call):
    """The users and restriction states the verb's evaluations of seen() reported."""
    notices = []
    conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    # One transaction, so the identity read after the verb is the one the verb
    # left behind: a commit does not reset it, only an abort does.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
        caller, caller_is_super = cur.fetchone()
        cur.execute("SELECT current_setting('search_path')")
        path = cur.fetchone()[0]
        cur.execute(f"SELECT pgcolumnar.{call}")
        cur.execute("SELECT current_user, current_setting('search_path')")
        after, path_after = cur.fetchone()
    seen = [n.split(" ", 1)[1] for n in notices if n.startswith(TAG + " ")]
    return caller, caller_is_super, (after, path_after), seen, path


@pytest.mark.parametrize("call", VERBS, ids=lambda c: c.split("(")[0])
def test_a_superuser_runs_the_owners_index_code_as_the_owner(pgc_conn, expect, call):
    fn = call.split("(")[0]
    with pgc_conn.cursor() as cur:
        _fixture(cur)
    caller, caller_is_super, (after, path_after), seen, path = _run_as_superuser(pgc_conn, call)
    expect.text(repr(caller_is_super), "True", f"premise: the caller of {fn} is a superuser")
    expect.text(after, caller, f"{fn}: the caller is the current user again once it returns")
    expect.text(path_after, path, f"{fn}: the caller's search_path is back once it returns")
    expect.at_least(len(seen), 1, f"premise: {fn} evaluated mallory's index code")
    expect.text(" ".join(sorted(set(seen))), f"user={ROLE} restricted=yes",
                f"{fn}: every evaluation ran as the owner, security-restricted")

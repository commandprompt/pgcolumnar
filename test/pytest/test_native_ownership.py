"""Every maintenance and DDL function refuses a non-owner (#432, port of native_ownership.sh).

The maintenance functions rewrite data, reclaim space, or take strong locks --
`truncate` takes AccessExclusiveLock -- so they are owner-only, like VACUUM and
CLUSTER. The ownership check sits immediately after the relation is opened and
confirmed columnar, before any work.

TWO THINGS THIS PORT ASSERTS THAT THE BASH SUITE CANNOT.

**The SQLSTATE, not the message.** `native_ownership.sh` greps the output for
`must be owner`. `CLAUDE.md` states the rule this breaks: "Assert SQLSTATE, not
error text: 42501 comes only from `aclcheck_error`." The refusal here is
`aclcheck_error(ACLCHECK_NOT_OWNER, OBJECT_TABLE, ...)` at
`src/columnar_vacuum.c:189` and `:205`, which is `ERRCODE_INSUFFICIENT_PRIVILEGE`.
A text grep passes whatever code the server attached, so the day one of these
refusals is raised as 22023 or 0A000 the bash suite stays green and every client
that switches on SQLSTATE breaks.

**It does not conflate refusal with login.** The bash suite runs each call through
a separate `psql` as a role that must be able to connect. If that role could not
log in, the grep finds no `must be owner` and the arm fails -- for a reason that
has nothing to do with ownership. `SET ROLE` changes the effective user for
permission checks without involving authentication, so what is measured here is
the ownership check and nothing else.

A third arm states the ordering the bash suite's comment asserts in prose: the
check fires BEFORE the work, so a non-owner is refused for a projection that does
not exist rather than told it is missing.
"""

import psycopg
import pytest

# Every maintenance and DDL entry point, with an argument list that would be valid
# for the owner. The names are the bash suite's, character for character, so
# compare_to_bash.py can diff the two by property.
OWNER_ONLY = [
    "compact('n')",
    "compact_rewrite('n', 0.0)",
    "recluster('n', 'id')",
    "vacuum('n')",
    "vacuum_sorted('n', 'id')",
    "cluster('n', 'id')",
    "truncate('n')",
    "add_projection('n', 'p', ARRAY['id','v'])",
    "drop_projection('n', 'p')",
]


def _fixture(cur):
    """Table, role, and enough grants that ONLY ownership can refuse alice.

    THE SCHEMA GRANT IS NOT HOUSEKEEPING, and what it protects against is not what
    I first wrote. `pgc_conn` puts each test in a private schema. Measured by
    removing the grant: alice gets `42P01 relation "n" does not exist`, because an
    unqualified name resolves through `search_path` and a schema she cannot use is
    simply skipped. So the refusal arms FAIL rather than falsely pass, and the
    premise arm's value here is that it fails FIRST and names reachability instead
    of leaving `42501 != 42P01` to be interpreted.

    The false-pass case is real but narrower: a QUALIFIED reference into a schema
    without USAGE raises `42501 permission denied for schema`, which is the
    ownership refusal's own SQLSTATE from a different check. These calls pass the
    table as an unqualified string, so they land on the 42P01 side today -- and the
    premise arm is what keeps that from being an assumption.

    The role is cluster-global and these tests run in parallel under xdist, so it is
    created idempotently and never dropped: `DROP ROLE` fails with
    DependentObjectsStillExist once it holds a grant, and dropping a role another
    worker is using is worse than leaving it.
    """
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'alice'")
    if cur.fetchone() is None:
        cur.execute("CREATE ROLE alice NOSUPERUSER")
    cur.execute("GRANT USAGE ON SCHEMA pgcolumnar TO alice")
    cur.execute("SELECT current_schema()")
    schema = cur.fetchone()[0]
    cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO alice')
    cur.execute("DROP TABLE IF EXISTS n")
    cur.execute("CREATE TABLE n (id int, v int) USING pgcolumnar")
    cur.execute(
        "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000,"
        " chunk_group_row_limit => 1000)"
    )
    cur.execute("INSERT INTO n SELECT g, g FROM generate_series(1, 3000) g")
    cur.execute("GRANT SELECT ON n TO alice")


def _alice_can_read(cur):
    """The premise every refusal arm rests on: alice reaches the table."""
    cur.execute("SET ROLE alice")
    try:
        cur.execute("SELECT count(*) FROM n")
        return cur.fetchone()[0]
    finally:
        cur.execute("RESET ROLE")


@pytest.mark.parametrize("call", OWNER_ONLY, ids=lambda c: c.split("(")[0])
def test_non_owner_is_refused_with_42501(pgc_conn, expect, call):
    """One arm per function, named as the bash suite names it."""
    fn = call.split("(")[0]
    with pgc_conn.cursor() as cur:
        _fixture(cur)
        reachable = _alice_can_read(cur)
        cur.execute("SET ROLE alice")
        try:
            with pytest.raises(Exception) as excinfo:
                cur.execute(f"SELECT pgcolumnar.{call}")
        finally:
            cur.execute("RESET ROLE")
    expect.num(reachable, 3000,
               f"premise: alice reaches the table, so a 42501 is about ownership: {fn}")
    expect.sqlstate(excinfo.value, "42501", f"non-owner refused: {fn}")


# The order the owner runs them in: a projection must exist before it is dropped, and
# truncate empties the table, so it goes last.
OWNER_ORDER = [
    "compact('n')",
    "compact_rewrite('n', 0.0)",
    "recluster('n', 'id')",
    "vacuum('n')",
    "vacuum_sorted('n', 'id')",
    "cluster('n', 'id')",
    "add_projection('n', 'p', ARRAY['id','v'])",
    "drop_projection('n', 'p')",
    "truncate('n')",
]


def test_the_owner_is_allowed(pgc_conn, expect):
    """The control. A gate that refused everyone would satisfy every arm above.

    THE OWNER IS NOT A SUPERUSER (#1339). It used to be the connecting role,
    `postgres`, and the owner check passes every superuser. So the control proved a
    superuser is allowed, and an ownership gate rewritten as `superuser()` refused
    every real owner while every arm here stayed green. `bob` owns the table and is
    nothing else, and he runs all nine functions, not two.
    """
    with pgc_conn.cursor() as cur:
        _fixture(cur)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'bob'")
        if cur.fetchone() is None:
            cur.execute("CREATE ROLE bob NOSUPERUSER")
        cur.execute("GRANT USAGE ON SCHEMA pgcolumnar TO bob")
        cur.execute("SELECT current_schema()")
        cur.execute(f'GRANT USAGE ON SCHEMA "{cur.fetchone()[0]}" TO bob')
        cur.execute("ALTER TABLE n OWNER TO bob")
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'bob'")
        bob_super = cur.fetchone()[0]
        cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'n'::regclass")
        owner = cur.fetchone()[0]
        outcomes = {}
        counts = {}
        cur.execute("SET ROLE bob")
        try:
            for call in OWNER_ORDER:
                fn = call.split("(")[0]
                try:
                    cur.execute(f"SELECT pgcolumnar.{call}")
                    outcomes[fn] = "allowed"
                except psycopg.Error as e:
                    outcomes[fn] = e.sqlstate or type(e).__name__
                cur.execute("SELECT count(*) FROM n")
                counts[fn] = cur.fetchone()[0]
        finally:
            cur.execute("RESET ROLE")
    expect.text(f"{owner}|{bob_super}", "bob|False",
                "premise: the table's owner is bob, who is not a superuser")
    # The verdict AND the rows: a refusal leaves 3000 rows too, so the count alone
    # cannot tell an allowed call from a refused one.
    expect.text(f"{outcomes['compact']}|{counts['compact']}", "allowed|3000",
                "owner compact allowed")
    expect.text(f"{outcomes['recluster']}|{counts['recluster']}", "allowed|3000",
                "owner recluster allowed")
    expect.num(len(outcomes), 9, "premise: the owner ran all nine maintenance functions")
    for call in OWNER_ORDER:
        fn = call.split("(")[0]
        expect.text(outcomes[fn], "allowed", f"owner allowed: {fn}")


def test_the_check_fires_before_the_work(pgc_conn, expect):
    """The ordering the bash suite states in prose and does not assert.

    `drop_projection` on a projection that does not exist. A non-owner must still get
    42501 rather than a missing-object error, which is what "the check sits right
    after the relation is opened, before any work" means.
    """
    with pgc_conn.cursor() as cur:
        _fixture(cur)
        reachable = _alice_can_read(cur)
        cur.execute("SET ROLE alice")
        try:
            with pytest.raises(Exception) as excinfo:
                cur.execute("SELECT pgcolumnar.drop_projection('n', 'no_such_projection')")
        finally:
            cur.execute("RESET ROLE")
    expect.num(reachable, 3000, "premise: alice reaches the table")
    expect.sqlstate(excinfo.value, "42501",
                    "ownership is checked before the projection is looked up")

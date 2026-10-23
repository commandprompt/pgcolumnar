"""DROP COLUMN must not invalidate a materialized projection (#432, port of
projection_drop_column.sh).

Projections are extension metadata rather than `pg_depend` objects, so PostgreSQL
accepted `DROP COLUMN` even when a projection stored that column. Dropping its sort key
left the projection's attnum pointing at a dropped `pg_attribute` row, and the next
INSERT failed in `lookup_type_cache` with "type with OID 0 does not exist" -- the table
became unwritable. Until projections can join `DROP ... CASCADE`, the dependent DROP is
refused at its public DDL boundary.

**SQLSTATE, never the message.** `2BP01` is `dependent_objects_still_exist` and `42501`
is `insufficient_privilege`; both are typed fields rather than text the server is free
to reword. The bash suite already does this, by parsing `VERBOSITY verbose` output with
`sed`. The port reads `exc.sqlstate` off the raised error, which is the same claim
without a regex over a message.

**SET ROLE, not a login role, and the two precedents disagree so the choice is stated.**
`test_projection_privilege.py` uses real logins because there the ACL layers ARE the
subject: a deny arm must prove the call reached the code that denies it, and it recorded
`SET ROLE` arms still passing with `NOLOGIN` injected. `test_native_ownership.py` uses
`SET ROLE` for the opposite reason: when only ownership is under test, requiring the role
to log in adds a way for the arm to fail that has nothing to do with the property. This
suite is the second kind. Nothing here asserts anything about connecting, so the role
never needs to.

**The schema grant is load-bearing**, and it is the trap `test_native_ownership.py`
records. `pgc_conn` puts each test in a private schema. Without USAGE on it the
non-owner gets `42P01 relation "pdc" does not exist`, because an unqualified name
resolves through `search_path` and a schema the role cannot use is simply skipped -- so
the refusal arms would fail against the wrong SQLSTATE rather than pass falsely. The
premise below fails first and names reachability instead of leaving `42501 != 42P01` to
be interpreted.

THE TWO NON-OWNER REFUSALS ARE COMPARED TO EACH OTHER, in both suites (#1339). The arms
exist to show that a stranger cannot tell a projected column from an unprojected one, so
the property -- these two are INDISTINGUISHABLE -- is asserted directly rather than
implied by two separate constants, and it is asserted before them, so it can fail.

**The lock is tested by contention.** The ownership check runs before the lock and again
after it, and both raise 42501, so a SQLSTATE alone cannot tell which one refused. While
the owner holds SHARE on the table, only the unlocked check refuses a stranger at once.
"""

import psycopg

NOBODY = "pdc_nobody"


def _err(cur, sql):
    """-> the raised error, or None if the statement succeeded.

    `pgc_conn` is autocommit, so a failed statement is its own aborted transaction and
    the next one starts clean. No savepoint is needed and none is taken.
    """
    try:
        cur.execute(sql)
        return None
    except psycopg.Error as exc:
        return exc


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def _as_nobody(cur, sql):
    """Run one statement as the non-owner and come back, whatever happened."""
    cur.execute(f"SET ROLE {NOBODY}")
    try:
        return _err(cur, sql)
    finally:
        cur.execute("RESET ROLE")


def test_drop_column_is_refused_while_a_projection_depends_on_it(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pdc (id int, payload text, sort_key int) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.add_projection('pdc', 'by_sort', "
            "ARRAY['id','sort_key'], ARRAY['sort_key'])"
        )
        cur.execute(
            "INSERT INTO pdc SELECT g, 'row-' || g, g % 10 FROM generate_series(1,1000) g"
        )

        # The role is cluster-global and these tests run in parallel under xdist, so it
        # is created idempotently and never dropped: DROP ROLE fails once it holds a
        # grant, and dropping a role another worker is using is worse than leaving it.
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (NOBODY,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {NOBODY} NOSUPERUSER")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {NOBODY}')

        # EXTRA, and it is what stops the two arms below passing for the wrong reason.
        #
        # THE PROPERTY IS RESOLUTION, NOT READABILITY, and my first version of this
        # asserted the wrong one. USAGE on the schema lets the name resolve; SELECT is a
        # separate grant the non-owner does not have, so a read comes back 42501 and an
        # arm demanding success fails on a correctly configured fixture. What matters
        # here is only that the name is not invisible: 42P01 would mean `search_path`
        # skipped the schema, and then the DROP arms below would be asserting 42501
        # against a table the role cannot see rather than against ownership.
        seen = _as_nobody(cur, "SELECT 1 FROM pdc LIMIT 1")
        expect.text("resolved" if seen is None or seen.sqlstate != "42P01"
                    else "unresolved (42P01)",
                    "resolved",
                    "premise: the non-owner can resolve the table, so a refusal is about ownership")

        # --- the stranger learns nothing ------------------------------------
        proj_err = _as_nobody(cur, "ALTER TABLE pdc DROP COLUMN sort_key")
        plain_err = _as_nobody(cur, "ALTER TABLE pdc DROP COLUMN payload")

        # The property is that the two are INDISTINGUISHABLE, not that each happens to
        # equal a constant, so it is compared FIRST. Placed after the two constant arms
        # it could never run red: `expect` stops at the first failure, and if both
        # constants hold the two are equal. With both ownership checks deleted, the
        # stranger gets 2BP01 for the projected column and 42501 for the other, and
        # this is the check that names it (#1339).
        expect.text(proj_err.sqlstate if proj_err else "no error",
                    plain_err.sqlstate if plain_err else "no error",
                    "and the two refusals are the same error, so neither leaks")
        expect.sqlstate(proj_err, "42501",
                        "a non-owner learns nothing from a projected column")
        expect.sqlstate(plain_err, "42501",
                        "the same non-owner error is returned for an unprojected column")

        # THE LOCK HALF OF THE PROPERTY (#1339). The ownership check runs twice: once
        # with no lock, then again after ShareUpdateExclusiveLock. The arms above
        # cannot tell them apart, because both raise 42501. Contention can: while the
        # owner holds SHARE on pdc, which conflicts with ShareUpdateExclusiveLock, a
        # stranger refused by the unlocked check gets 42501 at once, and one that
        # reaches the lock first waits out its lock_timeout (55P03).
        holder = psycopg.connect(pgc_conn.info.dsn, autocommit=False)
        try:
            holder.execute(f'SET search_path TO "{schema}"')
            holder.execute("LOCK TABLE pdc IN SHARE MODE")
            held = _one(cur, "SELECT count(*) FROM pg_locks WHERE relation = 'pdc'::regclass "
                             "AND mode = 'ShareLock' AND granted")
            expect.num(held, 1, "premise: the owner holds SHARE on pdc before the stranger asks")
            cur.execute("SET lock_timeout = '2s'")
            try:
                waited_err = _as_nobody(cur, "ALTER TABLE pdc DROP COLUMN sort_key")
            finally:
                cur.execute("RESET lock_timeout")
            expect.sqlstate(waited_err, "42501",
                            "a non-owner is refused before it waits for the table lock")
        finally:
            holder.rollback()
            holder.close()

        # --- the owner is refused as a dependency ---------------------------
        #
        # Red before the fix: PostgreSQL accepts this, and the next INSERT reaches the
        # projection writer with a type OID of zero.
        expect.sqlstate(_err(cur, "ALTER TABLE pdc DROP COLUMN sort_key"), "2BP01",
                        "dropping a projected column is refused as a dependency")

        cur.execute("INSERT INTO pdc VALUES (1001, 'still-writable', 1)")
        expect.num(_one(cur, "SELECT count(*) FROM pdc"), 1001,
                   "the rejected DDL leaves the table writable")
        expect.num(
            _one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('pdc','by_sort')"),
            1001,
            "and the projection still receives the row",
        )

        # A column outside every projection remains ordinary DDL.
        cur.execute("ALTER TABLE pdc DROP COLUMN payload")
        cur.execute("INSERT INTO pdc VALUES (1002, 2)")
        expect.num(_one(cur, "SELECT count(*) FROM pdc"), 1002,
                   "dropping an unrelated column remains allowed")

        # --- a partitioned parent recurses into columnar partitions ---------
        #
        # The parent has no storage of its own, but DROP COLUMN recurses, so the
        # dependency check has to walk the same hierarchy.
        cur.execute("CREATE TABLE pdc_parent (id int, sort_key int) PARTITION BY RANGE (id)")
        cur.execute(
            "CREATE TABLE pdc_child PARTITION OF pdc_parent "
            "FOR VALUES FROM (0) TO (100) USING pgcolumnar"
        )
        cur.execute(
            "SELECT pgcolumnar.add_projection('pdc_child', 'child_sort', "
            "ARRAY['id','sort_key'], ARRAY['sort_key'])"
        )
        cur.execute("INSERT INTO pdc_parent VALUES (1, 1)")

        expect.sqlstate(_err(cur, "ALTER TABLE pdc_parent DROP COLUMN sort_key"), "2BP01",
                        "a parent DROP sees projections on columnar partitions")

        cur.execute("INSERT INTO pdc_parent VALUES (2, 2)")
        expect.num(_one(cur, "SELECT count(*) FROM pdc_parent"), 2,
                   "the rejected parent DDL leaves its partition writable")

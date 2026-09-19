"""pgColumnar's internal advisory locks must not be reachable from SQL (#430).

`locktag_field4` says which advisory lock space a tag belongs to, and PostgreSQL's own
functions own exactly two values: 1 for the int8 key form, 2 for the two-int4 form. A
lock the extension takes in class 1 or 2 shares a space with anything a user can take
from SQL, so a user lock on the same tag blocks a columnar insert forever — which is
what #430 was.

Independent of test/advisory_lock_class.sh: same public seam (`pg_locks` and the two
`pg_advisory_*` forms), own observations. Assertion names match the shell suite so the
two can be compared by name, not by importing each other.

NO SLEEPS AND NO POLLING, WHICH IS THE WHOLE DIFFERENCE. The shell suite backgrounds a
`psql` running `pg_sleep(30)`, then polls `pg_locks` up to sixty times waiting for the
lock to appear, and again waiting for it to go. It also has to `pg_terminate_backend`
rather than kill the client, and says why: killing the client leaves the server inside
`pg_sleep()` still holding the transaction, and every later check then blocks on our own
lock in both arms -- "which is how the first version of this file reported the same
result with and without the fix".

None of that is needed with real connections. A second connection's `INSERT` returns
when the lock is held, so the next statement can read `pg_locks` with no window at all;
closing the connection ends the transaction, so the lock is gone with no window either.
Every wait in the original is an artefact of driving the database through a shell.

THE SKIP IS PER TEST, SO THE SQL-FORM ARM IS ITS OWN TEST. `cannot_run` declares the
whole test unrunnable, while the shell suite's `check_skip` skips one check and carries
on. Splitting it keeps the scope honest -- the same reason `test_iceberg_fdw` refuses per
test rather than per check -- and lets the duplicate-key arm run regardless.
"""
import psycopg

# The two field4 values an application can produce, from lockfuncs.c.
SQL_REACHABLE = (1, 2)
PROBE_KEY = 900001


def _one(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    return row[0] if row else None


def _fixture(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE u (k int, v text) USING pgcolumnar")
        cur.execute("CREATE UNIQUE INDEX u_k ON u (k)")
        cur.execute("INSERT INTO u SELECT g, 'v'||g FROM generate_series(1,100) g")
        cur.execute("SELECT current_schema()")
        return cur.fetchone()[0]


def _holder(cluster, schema):
    """A second session, in the same schema, with its own open transaction."""
    conn = psycopg.connect(cluster.dsn(), autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{schema}", public')
    return conn


def _advisory_tags(conn):
    """-> EVERY advisory lock another session holds, as (classid, objid, field4).

    ALL OF THEM, NOT THE HIGHEST field4, and that difference is the whole test. The
    shell suite takes `ORDER BY objsubid DESC LIMIT 1` as "the lock the insert took".
    An insert takes more than one: `PGCOLUMNAR_LOCKCLASS_STORAGE_ROW` (102) as well as
    `PGCOLUMNAR_LOCKCLASS_UNIQUE_KEY` (103). The maximum is the unique-key lock only
    while the unique-key lock is the highest-numbered one -- which stops being true in
    exactly the case the suite exists to catch.

    Measured, with the unique-key class put back to 2 (the #430 defect) and the object
    forced to rebuild:

        the lock it took: classid=2 objid=1410065408 field4=102

    The maximum is now 102, the STORAGE_ROW lock, which is still unreachable -- so the
    suite reports the property holding while the lock under test sits in the SQL space.
    Asserting over the whole set has no such blind spot, and is the stronger claim
    anyway: NO lock an insert takes may be SQL-reachable.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT classid, objid, objsubid FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted "
                    "AND pid <> pg_backend_pid() "
                    "ORDER BY objsubid DESC")
        return cur.fetchall()


def _others_holding(conn):
    return _one(conn, "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                      "AND pid <> pg_backend_pid()")


def test_the_lock_an_insert_takes_is_not_in_a_sql_reachable_class(
        pgc_cluster, pgc_conn, expect):
    schema = _fixture(pgc_conn)
    expect.text(_one(pgc_conn, "SHOW pgcolumnar.enable_unique_insert_lock"), "on",
                "premise: the lock is enabled, or nothing below proves anything")

    other = _holder(pgc_cluster, schema)
    try:
        with other.cursor() as cur:
            cur.execute(f"INSERT INTO u VALUES ({PROBE_KEY}, 'probe')")
        tags = _advisory_tags(pgc_conn)
        expect.text("yes" if tags else "no (pg_locks showed nothing)", "yes",
                    "premise: the insert took an advisory lock we can see")
        print(f"-- the locks it took: {tags}")
        bad = [t for t in tags if t[2] in SQL_REACHABLE]
        expect.text(
            "unreachable" if not bad else f"reachable {bad}",
            "unreachable",
            "the lock an insert takes is not in a SQL-reachable class")
    finally:
        other.rollback()
        other.close()

    # NO POLLING: the rollback ended the transaction that held it, so by the time the
    # close returns there is nothing left to wait for.
    expect.num(_others_holding(pgc_conn), 0,
               "premise: the discovering session is gone and holds nothing")


def test_a_user_advisory_lock_on_that_tag_does_not_block_an_insert(
        pgc_cluster, pgc_conn, expect):
    schema = _fixture(pgc_conn)

    discoverer = _holder(pgc_cluster, schema)
    try:
        with discoverer.cursor() as cur:
            cur.execute(f"INSERT INTO u VALUES ({PROBE_KEY}, 'probe')")
        tags = _advisory_tags(pgc_conn)
    finally:
        discoverer.rollback()
        discoverer.close()

    # EVERY tag, for the reason in `_advisory_tags`: contending for only the
    # highest-numbered one contends for the wrong lock the moment the unique-key
    # class regresses into the SQL space, which is the case this arm is about.
    addressable = [(c, o) for c, o, _f in tags
                   if -2147483648 <= c <= 2147483647 and -2147483648 <= o <= 2147483647]
    tag = tags[0] if tags else None

    if not tag:
        expect.cannot_run("UNMET_PRECONDITION",
                          "no advisory lock was visible in pg_locks, so there is no "
                          "tag for a user session to contend for",
                          name="the SQL form of the advisory lock")
        return
    if not addressable:
        expect.cannot_run(
            "UNMET_PRECONDITION",
            f"none of {tags} fits in two int4s, and pg_advisory_xact_lock's "
            f"two-int4 form cannot address a tag it cannot express",
            name="the SQL form of the advisory lock")
        return

    # A user takes that EXACT tag through the two-int4 form, which is field4 = 2.
    # Before the fix the extension's lock was also field4 = 2, so this took the same
    # tag and the insert below waited forever.
    holder = _holder(pgc_cluster, schema)
    try:
        with holder.cursor() as cur:
            for classid, objid in addressable:
                cur.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)",
                            (classid, objid))
        held = _one(pgc_conn,
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted "
                    "AND objsubid = 2 AND (classid, objid) IN "
                    "(SELECT * FROM unnest(%s::bigint[], %s::bigint[]))",
                    ([c for c, _o in addressable], [o for _c, o in addressable]))
        expect.at_least(held, 1,
                        "premise: the other session really holds that exact tag in class 2")

        verdict = "ok"
        try:
            with pgc_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '10s'")
                cur.execute(f"INSERT INTO u VALUES ({PROBE_KEY}, 'new')")
        except psycopg.errors.QueryCanceled:
            verdict = "BLOCKED by the user lock"
        except psycopg.Error as exc:
            verdict = f"ERROR: {str(exc).splitlines()[0]}"
        finally:
            with pgc_conn.cursor() as cur:
                cur.execute("RESET statement_timeout")
        expect.text(verdict, "ok",
                    "a user advisory lock on that tag does not block a columnar insert")
    finally:
        holder.rollback()
        holder.close()


def test_a_duplicate_key_is_still_rejected(pgc_conn, expect):
    """Removing the collision by removing the lock satisfies everything above and
    silently gives back issue #5, so the lock's own job is asserted separately."""
    _fixture(pgc_conn)
    with pgc_conn.cursor() as cur:
        cur.execute(f"INSERT INTO u VALUES ({PROBE_KEY}, 'first')")
    try:
        with pgc_conn.cursor() as cur:
            cur.execute(f"INSERT INTO u VALUES ({PROBE_KEY}, 'dup')")
        got = "NOT rejected"
    except psycopg.Error as exc:
        msg = str(exc).lower()
        got = ("rejected" if "duplicate key" in msg or "unique constraint" in msg
               else f"NOT rejected: {str(exc).splitlines()[0]}")
    expect.text(got, "rejected", "a duplicate key is still rejected")

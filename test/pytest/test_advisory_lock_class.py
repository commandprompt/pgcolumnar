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

from pgc_vacuity import query_error

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
    shell suite reports the property holding while the lock under test sits in the SQL
    space. Measured under that mutation, with the object forced to rebuild:

        advisory_lock_class.sh    7 passed + 0 failed        PASSED
        this file                 TWO arms red, .so 0ab2accb47ef -> 29a7f55eeb71

    TWO, NOT ONE, AND I ORIGINALLY CLAIMED ONE (#1157). I wrote that the contention arm
    "inherits the error" -- reasoned, not run. @jdatcmd ran it. Both
    `the lock an insert takes is not in a SQL-reachable class` and
    `a user advisory lock on that tag does not block a columnar insert` go red, because
    the discovery FEEDS the contention arm: one wrong premise disables both, and both
    come back when the premise is fixed. Two arms moving together with the defect is
    worse than one vacuous arm, since both keep looking like evidence.

    Asserting over the whole set has no such blind spot, and is the stronger claim
    anyway: NO lock an insert takes may be SQL-reachable.

    REPRODUCING THAT MUTATION NEEDS A CLEAN BUILD (#1158). Changing this `#define`
    rebuilds nothing on its own: PGXS emits no header dependencies unless PostgreSQL
    was configured with `--enable-depend`, and it was not here. The run then measures a
    stale object and reports a clean pass that reads exactly like "not load-bearing",
    and the `.so` hash not moving is the only tell.

    `rm -f src/columnar_unique.o` IS NOT ENOUGH, AND THIS COMMENT SAID IT WAS. Measured
    on PG17 from a clean tree, mutating the header each time:

        rm -f obj; make                 0 files compiled, object still MISSING,
                                        .so f7eac4d19c7f4762 -- UNCHANGED
        rm -f obj; touch the .c; make   2 files compiled, .so 812d9b3c5c9ddd4c
        make clean; make                .so 812d9b3c5c9ddd4c

    `make` does not rebuild a deleted object listed in OBJS, so the first line leaves
    exactly the stale library the caveat is warning about. The last two agree on the
    byte, so touching the source is a measured-equivalent shortcut to a full clean --
    but `make clean` is the one to reach for, because it does not depend on knowing
    which object the header reached.
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


def _lock_classes(cluster, schema, setting):
    """-> the advisory classes one insert into u takes, read from its own session."""
    conn = psycopg.connect(cluster.dsn(), autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}", public')
            cur.execute(f"SET pgcolumnar.enable_unique_insert_lock = {setting}")
            cur.execute("INSERT INTO u VALUES (900003, 'classes')")
            cur.execute("SELECT DISTINCT objsubid FROM pg_locks "
                        "WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
            return {r[0] for r in cur.fetchall()}
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])
    finally:
        conn.rollback()
        conn.close()


def test_an_insert_takes_the_unique_key_lock_only_while_it_is_enabled(
        pgc_cluster, pgc_conn, expect):
    """The lock under test is TAKEN at all. Every other arm reads whatever locks an
    insert takes, and an insert also takes the storage-row lock (102), so with the
    unique-key lock never taken the premise still saw a lock, the class arm still read
    "unreachable", and the duplicate arm still passed (#1339). The setting is the
    control rather than the class number, so a renumbered class still counts."""
    schema = _fixture(pgc_conn)
    on = _lock_classes(pgc_cluster, schema, "on")
    off = _lock_classes(pgc_cluster, schema, "off")
    # Both readings must hold a lock, since the storage-row lock is taken either
    # way: an empty control would make every class "extra" (found by jdatcmd on #1565).
    if not isinstance(on, set) or not isinstance(off, set) or not on or not off:
        got = query_error(f"no reading: on {on}, off {off}")
    else:
        got = "taken" if on - off else f"not taken (on {sorted(on)}, off {sorted(off)})"
    expect.text(got, "taken",
                "an insert takes the unique-key lock, and only while enable_unique_insert_lock is on")


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
        # EVERY addressable tag, as the shell asks (#1339). At least one was satisfied by a
        # holder that took only the first tag, and the insert below then contended for a
        # lock nobody was holding.
        expect.text("yes" if held == len(addressable)
                    else f"no (holds {held} of {len(addressable)})", "yes",
                    "premise: the other session really holds that exact tag in class 2")

        verdict = "ok"
        try:
            with pgc_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '10s'")
                cur.execute(f"INSERT INTO u VALUES ({PROBE_KEY}, 'new')")
        except psycopg.errors.QueryCanceled:
            verdict = "BLOCKED by the user lock"
        except psycopg.Error as exc:
            verdict = query_error(str(exc).splitlines()[0])
        finally:
            with pgc_conn.cursor() as cur:
                cur.execute("RESET statement_timeout")
        expect.text(verdict, "ok",
                    "a user advisory lock on that tag does not block a columnar insert")
    finally:
        holder.rollback()
        holder.close()


def test_a_duplicate_key_is_still_rejected(pgc_conn, expect):
    """A duplicate is rejected. This is NOT the lock's own job asserted: with the lock
    never taken a duplicate is still rejected, and measured on PG17 even a concurrent
    one waits on the first insert and gets 23505, because the btree's uniqueness check
    waits on the winner (#1339). The lock's presence is the arm above that compares
    the setting on and off."""
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


def test_the_row_identity_lock_and_its_settings(pgc_cluster, pgc_conn, expect):
    """An UPDATE serialises on its row through an advisory lock in class 104; the
    session setting enable_row_update_lock turns that off, and row_lock_buckets is
    part of the lock tag, so it is fixed at server start. A bulk UPDATE holds one lock
    per bucket, not one per row (#1341 item 16)."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rl (id int, v int) USING pgcolumnar")
        cur.execute("INSERT INTO rl SELECT g, g FROM generate_series(1, 100) g")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]

    def row_locks_while_updating(setting):
        holder = psycopg.connect(pgc_cluster.dsn(), autocommit=False)
        try:
            with holder.cursor() as hc:
                hc.execute(f'SET search_path TO "{schema}"')
                hc.execute(f"SET LOCAL pgcolumnar.enable_row_update_lock = {setting}")
                hc.execute("UPDATE rl SET v = v + 1 WHERE id = 7")
            with pgc_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                            " AND objsubid = 104 AND pid <> pg_backend_pid()")
                return cur.fetchone()[0]
        finally:
            holder.rollback()
            holder.close()

    on = row_locks_while_updating("on")
    expect.text("held" if on >= 1 else "none", "held",
                "an UPDATE holds a row-identity lock (class 104) while enable_row_update_lock is on")
    expect.num(row_locks_while_updating("off"), 0, "and holds none with enable_row_update_lock off")
    with pgc_conn.cursor() as cur:
        try:
            cur.execute("SET pgcolumnar.row_lock_buckets = 7")
            refused = "ok"
        except psycopg.Error as e:
            refused = e.sqlstate
        cur.execute("SELECT context || '|' || setting FROM pg_settings WHERE name = 'pgcolumnar.row_lock_buckets'")
        ctx = cur.fetchone()[0]
    expect.text(f"{refused}|{ctx}", "55P02|postmaster|1024",
                "row_lock_buckets is fixed at server start, so a session cannot change it (55P02)")

    # The documented bound: row_lock_buckets bounds the row locks a transaction holds.
    # One transaction updates 200000 rows, then counts its own class-104 locks.
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rlb (id int, v int) USING pgcolumnar")
        cur.execute("INSERT INTO rlb SELECT g, g FROM generate_series(1, 200000) g")
    bulk = psycopg.connect(pgc_cluster.dsn(), autocommit=False)
    try:
        with bulk.cursor() as bc:
            bc.execute(f'SET search_path TO "{schema}"')
            try:
                bc.execute("UPDATE rlb SET v = v + 1")
                bc.execute("SELECT count(*) || '|' || current_setting('pgcolumnar.row_lock_buckets') FROM pg_locks"
                           " WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND objsubid = 104")
                held = bc.fetchone()[0]
                bulk.commit()
            except psycopg.Error as e:
                held = f"ERROR: {e.sqlstate}"
                bulk.rollback()
    finally:
        bulk.close()
    expect.text(held, "1024|1024",
                "a bulk UPDATE of 200000 rows holds one row-identity lock per bucket, row_lock_buckets of them")
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT count(*) || '|' || sum(v - id) FROM rlb")
        expect.text(cur.fetchone()[0], "200000|200000", "and the UPDATE committed every row")


def _int4(x):
    """An oid as the int4 with the same 32 bits, which is how lockfuncs.c puts a
    two-int4 key into the tag."""
    return x - 4294967296 if x > 2147483647 else x


def test_the_row_identity_lock_cannot_be_reached_from_sql(pgc_cluster, pgc_conn, expect):
    """#1341 item 56: the INSERT arms ask this of the unique-key and storage-row locks,
    and nothing asked it of class 104. Every advisory lock an open UPDATE holds is read;
    none may be in field 1 or 2. A second session then takes each of those exact tags
    through pg_advisory_xact_lock(int4, int4), and an UPDATE of the same row must not
    wait on it."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rl (id int, v int) USING pgcolumnar")
        cur.execute("INSERT INTO rl SELECT g, g FROM generate_series(1, 100) g")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]

    updater = _holder(pgc_cluster, schema)
    try:
        with updater.cursor() as uc:
            uc.execute("UPDATE rl SET v = v + 1 WHERE id = 7")
            uc.execute("SELECT pg_backend_pid()")
            upid = uc.fetchone()[0]
        with pgc_conn.cursor() as cur:
            cur.execute("SELECT classid, objid, objsubid FROM pg_locks "
                        "WHERE locktype = 'advisory' AND granted AND pid = %s", (upid,))
            tags = cur.fetchall()
    finally:
        updater.rollback()
        updater.close()

    reachable = [t for t in tags if t[2] in SQL_REACHABLE]
    expect.text("unknown (no advisory lock seen)" if not tags
                else (f"reachable: {reachable}" if reachable else "unreachable"),
                "unreachable", "the locks an UPDATE takes are not in a SQL-reachable class")

    user = _holder(pgc_cluster, schema)
    try:
        with user.cursor() as hc:
            for classid, objid, _f in tags:
                hc.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)",
                           (_int4(classid), _int4(objid)))
            hc.execute("SELECT pg_backend_pid()")
            hpid = hc.fetchone()[0]
        expect.num(_one(pgc_conn, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                                  "AND granted AND objsubid = 2 AND pid = %s", (hpid,)),
                   len(tags),
                   "premise: the other session holds every one of those tags through the SQL functions")
        verdict = "ok"
        try:
            with pgc_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '10s'")
                cur.execute("UPDATE rl SET v = v + 1 WHERE id = 7")
        except psycopg.errors.QueryCanceled:
            verdict = "BLOCKED by the user lock"
        except psycopg.Error as exc:
            verdict = query_error(str(exc).splitlines()[0])
        finally:
            with pgc_conn.cursor() as cur:
                cur.execute("RESET statement_timeout")
        expect.text(verdict, "ok", "a user advisory lock on those tags does not block a columnar UPDATE")
    finally:
        user.rollback()
        user.close()


def test_bulk_parallel_writer_set_by_hand_still_serialises_a_first_write(
        pgc_cluster, pgc_conn, expect):
    """#1341 item 55: the reference says setting bulk_parallel_writer by hand is safe,
    because the loaders' skip of the storage-row creation lock fires only once the
    storage row is committed. Two sessions with it on each make the first write to a
    committed, empty table; the second must wait for the first and then land its row.
    A skip that did not wait for the row to be committed lets both create it, and the
    second fails on storage_pkey."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE bw (id int) USING pgcolumnar")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]

    first = _holder(pgc_cluster, schema)
    second = _holder(pgc_cluster, schema)
    second.commit()
    second.autocommit = True
    try:
        with first.cursor() as fc:
            fc.execute("SET LOCAL pgcolumnar.bulk_parallel_writer = on")
            fc.execute("INSERT INTO bw VALUES (1)")
        result = {}

        def write_second():
            try:
                with second.cursor() as sc:
                    sc.execute("SET pgcolumnar.bulk_parallel_writer = on")
                    sc.execute("SET statement_timeout = '30s'")
                    sc.execute("INSERT INTO bw VALUES (2)")
                result["verdict"] = "ok"
            except psycopg.Error as exc:
                result["verdict"] = query_error(str(exc).splitlines()[0])

        import threading
        import time
        t = threading.Thread(target=write_second)
        t.start()
        # The second write must be waiting on the first, not finished, before the
        # first commits: otherwise the arm measures two writes in sequence.
        waited = "no"
        for _ in range(100):
            if _one(pgc_conn, "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                              "AND query LIKE 'INSERT INTO bw%%'"):
                waited = "yes"
                break
            time.sleep(0.1)
        first.commit()
        t.join(60)
        expect.text(waited, "yes", "premise: the second first write waited on the first")
        expect.text(f"{result.get('verdict', 'still running')}|"
                    f"{_one(pgc_conn, 'SELECT count(*) FROM bw')}", "ok|2",
                    "with bulk_parallel_writer set by hand, a second first write waits for the first and both rows land")
    finally:
        first.rollback()
        first.close()
        second.close()

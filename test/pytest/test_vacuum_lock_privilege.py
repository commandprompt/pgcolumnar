"""Maintenance entry points refuse a non-owner BEFORE taking their lock (#749): the
pytest twin of `test/vacuum_lock_privilege.sh` (#1341). Every check carries the
shell's name.

A superuser session holds ACCESS EXCLUSIVE on `victim` while a non-owner calls each
entry point with lock_timeout 4s. Refused by ownership first is 42501 at once; checking
ownership after the lock request is a lock wait that ends in 55P03. Only contention
separates the two orderings: without it both end in 42501.

The tables live in this test's schema, and the role is granted USAGE on it, so a
42501 is never "permission denied for schema" passing for the owner check.
"""
import threading
import time

import pytest

UNPRIV = "t_unpriv_vlp_py"


def _login(cluster, role, schema):
    import psycopg
    c = psycopg.connect(f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres", autocommit=True)
    c.execute(f'SET search_path TO "{schema}", pgcolumnar, public')
    return c


def _outcome(cluster, schema, sql):
    """-> owner | locktimeout | other:<state> | success, for `sql` run as the non-owner."""
    import psycopg
    with _login(cluster, UNPRIV, schema) as c:
        try:
            c.execute("SET lock_timeout = '4s'")
            c.execute(sql)
            return "success"
        except psycopg.Error as e:
            if e.sqlstate == "42501":
                return "owner"
            if e.sqlstate == "55P03":
                return "locktimeout"
            return f"other:{e.sqlstate}"


def _heap_outcome(cluster, schema, sql):
    """-> owner | typedisclosed | other:<state>, for a heap relation the caller does not own."""
    import psycopg
    with _login(cluster, UNPRIV, schema) as c:
        try:
            c.execute(sql)
            return "other:none"
        except psycopg.Error as e:
            if e.sqlstate == "42501":
                return "owner"
            if "is not a columnar table" in (e.diag.message_primary or ""):
                return "typedisclosed"
            return f"other:{e.sqlstate}"


def test_maintenance_refuses_a_non_owner_before_its_lock(pgc_cluster, pgc_conn, expect):
    import psycopg
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        cur.execute("CREATE TABLE victim (id int, v int) USING pgcolumnar")
        cur.execute("INSERT INTO victim SELECT g, g FROM generate_series(1,2000) g")
        cur.execute("REVOKE ALL ON victim FROM PUBLIC")
        cur.execute(f"DO $$ BEGIN CREATE ROLE {UNPRIV} NOSUPERUSER LOGIN; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {UNPRIV}")
        cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {UNPRIV}')
        cur.execute("CREATE TABLE heapvictim (id int)")
        cur.execute("REVOKE ALL ON heapvictim FROM PUBLIC")

    # ---- premises
    with _login(pgc_cluster, UNPRIV, schema) as c:
        expect.num(c.execute("SELECT 1").fetchone()[0], 1, "premise: the caller can open a session")
    with pgc_conn.cursor() as cur:
        cur.execute(f"SELECT pg_get_userbyid(relowner) = '{UNPRIV}' FROM pg_class WHERE oid = 'victim'::regclass")
        expect.text(str(cur.fetchone()[0]), "False", "premise: the caller is not the owner of victim")
        cur.execute("SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam WHERE c.oid = 'victim'::regclass")
        expect.text(cur.fetchone()[0], "pgcolumnar", "premise: victim is a columnar table")

    # ---- an ACCESS EXCLUSIVE lock on victim, held on a connection of its own
    holder = psycopg.connect(pgc_cluster.dsn(), autocommit=False)
    try:
        with holder.cursor() as hc:
            hc.execute(f'SET search_path TO "{schema}"')
            hc.execute("LOCK TABLE victim IN ACCESS EXCLUSIVE MODE")
        held = 0
        for _ in range(40):
            with pgc_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks WHERE relation = 'victim'::regclass"
                            " AND mode = 'AccessExclusiveLock' AND granted")
                held = cur.fetchone()[0]
            if held >= 1:
                break
            time.sleep(0.25)
        expect.num(held, 1, "premise: an AccessExclusiveLock on victim is held before the probes")

        # ---- the deny arms: refused by ownership, not by a lock wait
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.vacuum('victim'::regclass)"), "owner",
                    "vacuum refuses a non-owner before taking the exclusive lock")
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.vacuum_sorted('victim'::regclass, 'id')"),
                    "owner", "vacuum_sorted refuses a non-owner before taking the exclusive lock")
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.cluster('victim'::regclass, 'id')"), "owner",
                    "cluster refuses a non-owner before taking the exclusive lock")
        expect.text(_outcome(pgc_cluster, schema,
                             "SELECT pgcolumnar.add_projection('victim'::regclass, 'p1', ARRAY['id'])"), "owner",
                    "add_projection refuses a non-owner before taking its lock (#749)")
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.drop_projection('victim'::regclass, 'p1')"),
                    "owner", "drop_projection refuses a non-owner before taking its lock (#749)")
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.recluster('victim'::regclass, 'id')"), "owner",
                    "recluster refuses a non-owner before taking its lock (#749)")
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.compact_rewrite('victim'::regclass, 0.2)"),
                    "owner", "compact_rewrite refuses a non-owner before taking its lock (#749)")
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.compact('victim'::regclass)"), "owner",
                    "compact refuses a non-owner before taking its lock (#749)")
        expect.text(_outcome(pgc_cluster, schema, "SELECT pgcolumnar.truncate('victim'::regclass)"), "owner",
                    "truncate refuses a non-owner before taking its lock (#1384)")

        # ---- a non-owner learns only that it is not the owner
        expect.text(_heap_outcome(pgc_cluster, schema,
                                  "SELECT pgcolumnar.drop_projection('heapvictim'::regclass, 'p1')"), "owner",
                    "drop_projection tells a non-owner nothing about the relation type (#749)")
        expect.text(_heap_outcome(pgc_cluster, schema, "SELECT pgcolumnar.compact('heapvictim'::regclass)"), "owner",
                    "compact does the same, which is the behaviour drop_projection now matches")
        expect.text(_heap_outcome(pgc_cluster, schema, "SELECT pgcolumnar.truncate('heapvictim'::regclass)"), "owner",
                    "truncate tells a non-owner nothing about the relation type (#1384)")

        # WATCHED WHILE A REFUSED PROBE RUNS: asked after every probe had exited this
        # could not fail, because a timed-out request leaves the queue with its session
        # (#1381 review). A caller refused before its lock is gone at once; one that
        # queued is seen waiting for its whole lock_timeout.
        t = threading.Thread(target=_outcome, args=(pgc_cluster, schema, "SELECT pgcolumnar.vacuum('victim'::regclass)"))
        t.start()
        queued_max = 0
        while t.is_alive():
            with pgc_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
                            f" WHERE l.relation = 'victim'::regclass AND a.usename = '{UNPRIV}' AND NOT l.granted")
                queued_max = max(queued_max, cur.fetchone()[0])
            time.sleep(0.1)
        t.join()
        expect.num(queued_max, 0, "the refused caller never queued a lock request on victim, watched while it ran")
        with pgc_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE relation = 'victim'::regclass"
                        " AND mode = 'AccessExclusiveLock' AND granted")
            expect.num(cur.fetchone()[0], 1, "premise: the lock is still held after the last probe")
    finally:
        holder.rollback()
        holder.close()

    # ---- allow arms: the owner is not broken (uncontended)
    def owner_ok(*sqls):
        try:
            with pgc_conn.cursor() as cur:
                for s in sqls:
                    cur.execute(s)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"
    expect.text(owner_ok("SELECT pgcolumnar.vacuum('victim'::regclass)"), "ok",
                "the owner can still vacuum the table")
    expect.text(owner_ok("SELECT pgcolumnar.compact('victim'::regclass)"), "ok",
                "the owner can still compact the table (#749 control)")
    expect.text(owner_ok("SELECT pgcolumnar.add_projection('victim'::regclass, 'p1', ARRAY['id'])",
                         "SELECT pgcolumnar.drop_projection('victim'::regclass, 'p1')"), "ok",
                "the owner can still add and drop a projection (#749 control)")
    # truncate's own answer, a block count, so a call that never ran cannot satisfy it
    try:
        with pgc_conn.cursor() as cur:
            cur.execute("SELECT pgcolumnar.truncate('victim'::regclass)")
            blocks = cur.fetchone()[0]
        counted = "counted" if isinstance(blocks, int) and blocks >= 0 else f"none:{blocks!r}"
    except psycopg.Error as e:
        counted = f"none:{e.sqlstate}"
    expect.text(counted, "counted",
                "the owner can still truncate the table, and is told how many blocks it returned (#1384 control)")

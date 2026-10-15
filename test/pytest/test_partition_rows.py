"""Rows that cross columnar partitions, and keys enforced through a partitioned
parent (#1341, #1421). The pytest twin of `test/partition_rows.sh`, every check by
the shell's name.

A row-moving UPDATE is a delete in one partition and an insert in another. A key on
the parent is one unique index per partition. Each arm runs on a parent with two
columnar partitions and on one with two heap partitions, and heap is the answer.

THE RACES are ordered by events, not sleeps. A gate connection holds an advisory
lock. The holder writes, then queues for that lock. The contender starts and must be
seen waiting on a lock; only then does the gate let go and the holder commit. A
second session moving a row the first is moving, with a primary key on the parent,
used to fail with 23505 on columnar, where heap fails with 40001 (#1421).
"""
import threading
import time

import psycopg

AMS = ("pgcolumnar", "heap")
CONTENT = ("SELECT count(*) || '/' || md5(string_agg(id || ':' || k || ':' || v, ',' ORDER BY id)) "
           "FROM %p")
SPLIT = "SELECT (SELECT count(*) FROM %p_1) || '/' || (SELECT count(*) FROM %p_2)"
IDX = ("SET enable_seqscan = off", "SET enable_bitmapscan = off")


def _connect(cluster, schema, name):
    conn = psycopg.connect(cluster.dsn(), autocommit=True)
    conn.execute(f'SET search_path TO "{schema}", public')
    conn.execute(f"SET application_name = '{name}'")
    return conn


def _outcome(conn, sql):
    try:
        conn.execute(sql)
        return "ok"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no sqlstate: {exc}"


def _value(conn, sql, setup=()):
    with conn.cursor() as cur:
        for s in setup:
            cur.execute(s)
        cur.execute(sql)
        got = cur.fetchone()[0]
        for s in setup:
            cur.execute(s.replace("SET ", "RESET ").split(" =")[0])
    return got


def _both(conn, sql):
    return "|".join(_outcome(conn, sql.replace("%p", f"pr_{am}")) for am in AMS)


def _answers(conn, sql, setup=()):
    return "|".join(str(_value(conn, sql.replace("%p", f"pr_{am}"), setup)) for am in AMS)


def _race(cluster, schema, conn, holder_sql, contender_sql, count_sql):
    """-> '<contender waited>|<contender outcome>|<rows matching afterwards>'."""
    gate = _connect(cluster, schema, "pr_gate")
    holder = _connect(cluster, schema, "pr_holder")
    contender = _connect(cluster, schema, "pr_contender")
    result = {}
    try:
        gate.execute("SELECT pg_advisory_lock(1341)")
        holder.execute("BEGIN")
        holder.execute(holder_sql)

        def hold():
            holder.execute("SELECT pg_advisory_lock_shared(1341)")
            holder.execute("COMMIT")

        def contend():
            contender.execute("SET statement_timeout = '60s'")
            result["out"] = _outcome(contender, contender_sql)

        th = threading.Thread(target=hold)
        th.start()
        tc = threading.Thread(target=contend)
        tc.start()
        waited = "no"
        for _ in range(300):
            if _value(conn, "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'pr_contender' "
                            "AND wait_event_type = 'Lock'") == 1:
                waited = "yes"
                break
            time.sleep(0.1)
        gate.execute("SELECT pg_advisory_unlock(1341)")
        th.join(60)
        tc.join(60)
        return f"{waited}|{result.get('out', 'still running')}|{_value(conn, count_sql)}"
    finally:
        for c in (contender, holder, gate):
            c.close()


def _both_race(cluster, schema, conn, holder_sql, contender_sql, key):
    return "  ".join(_race(cluster, schema, conn, holder_sql.replace("%p", f"pr_{am}"),
                           contender_sql.replace("%p", f"pr_{am}"),
                           f"SELECT count(*) FROM pr_{am} WHERE {key}") for am in AMS)


def test_rows_cross_columnar_partitions_and_keys_hold_through_the_parent(pgc_cluster, pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        for am in AMS:
            cur.execute(f"CREATE TABLE pr_{am} (id int PRIMARY KEY, k int, v text) PARTITION BY RANGE (id)")
            cur.execute(f"CREATE TABLE pr_{am}_1 PARTITION OF pr_{am} FOR VALUES FROM (0) TO (1000) USING {am}")
            cur.execute(f"CREATE TABLE pr_{am}_2 PARTITION OF pr_{am} FOR VALUES FROM (1000) TO (3000) USING {am}")
            cur.execute(f"INSERT INTO pr_{am} SELECT g, g % 7, 'r' || g FROM generate_series(1, 999) g")

    expect.text(_both(pgc_conn, "UPDATE %p SET id = id + 1000 WHERE id < 50"), "ok|ok",
                "an UPDATE moving 49 rows to the other partition succeeds, as on heap")
    expect.text(_answers(pgc_conn, SPLIT), "950/49|950/49", "and each partition holds what heap's does")
    c, h = _answers(pgc_conn, CONTENT).split("|")
    expect.text("same" if c == h else f"{c}|{h}", "same", "and the parent holds the same rows as heap's")
    expect.text(_answers(pgc_conn, "SELECT count(*) FILTER (WHERE id = 1001) || '/' || "
                                   "count(*) FILTER (WHERE id = 1) FROM %p WHERE id IN (1, 1001)", IDX),
                "1/0|1/0", "the primary key finds a moved row at its new key, and not at its old one, as on heap")

    expect.text(_both(pgc_conn, "INSERT INTO %p VALUES (500, 0, 'dup')"), "23505|23505",
                "a duplicate key inserted through the parent is refused (23505), as on heap")
    expect.text(_both(pgc_conn, "INSERT INTO %p VALUES (1, 0, 'again')"), "ok|ok",
                "a key a moved row gave up can be used again, as on heap")
    expect.text(_both(pgc_conn, "INSERT INTO %p VALUES (1001, 0, 'dup')"), "23505|23505",
                "a duplicate of a moved row's new key is refused in its new partition (23505), as on heap")

    expect.text(_both_race(pgc_cluster, schema, pgc_conn, "INSERT INTO %p VALUES (1500, 0, 'a')",
                           "INSERT INTO %p VALUES (1500, 0, 'b')", "id = 1500"),
                "yes|23505|1  yes|23505|1",
                "a second insert of the same new key through the parent waits, then is refused (23505), and one row is left, as on heap")
    expect.text(_both_race(pgc_cluster, schema, pgc_conn, "UPDATE %p SET id = id + 1000 WHERE id = 60",
                           "UPDATE %p SET id = id + 1000 WHERE id = 60", "id IN (60, 1060)"),
                "yes|40001|1  yes|40001|1",
                "a second session moving a row the first is moving waits, then fails with 40001, and the row exists once, as on heap")
    expect.text(_both_race(pgc_cluster, schema, pgc_conn, "UPDATE %p SET id = id + 1000 WHERE id = 70",
                           "UPDATE %p SET v = 'changed' WHERE id = 70", "id IN (70, 1070) AND v <> 'changed'"),
                "yes|40001|1  yes|40001|1",
                "an UPDATE in place of a row another session is moving waits, then fails with 40001, as on heap")
    c, h = _answers(pgc_conn, CONTENT).split("|")
    expect.text("same" if c == h else f"{c}|{h}", "same", "and after the races the parents hold the same rows")


PM_MEM = """CREATE FUNCTION pm_mem(at text, id int) RETURNS int LANGUAGE plpgsql AS $$
BEGIN
    IF id IN (1, 49999) THEN
        INSERT INTO pm_mem SELECT at, id, sum(total_bytes) FROM pg_backend_memory_contexts;
    END IF;
    RETURN 0;
END $$"""


def _pm_build(conn, name):
    """A keyed parent whose first partition holds 1..50000 in one row group, evens deleted."""
    conn.execute(f"CREATE TABLE {name} (id int PRIMARY KEY, v int) PARTITION BY RANGE (id)")
    conn.execute(f"CREATE TABLE {name}_1 PARTITION OF {name} FOR VALUES FROM (0) TO (100000) USING pgcolumnar")
    conn.execute(f"CREATE TABLE {name}_2 PARTITION OF {name} FOR VALUES FROM (100000) TO (300000) USING pgcolumnar")
    conn.execute(f"INSERT INTO {name} SELECT g, 0 FROM generate_series(1, 50000) g")
    conn.execute(f"DELETE FROM {name} WHERE id % 2 = 0")


def _marks(conn, name):
    return _value(conn, f"SELECT count(*) || '|' || (SELECT count(*) FROM pgcolumnar.delete_vector "
                        f"WHERE storage_id = pgcolumnar.get_storage_id('{name}_1')) FROM {name}")


def _bounded(conn, at):
    readings, mb = conn.execute(
        "SELECT count(*), (max(bytes) FILTER (WHERE id = 49999) - max(bytes) FILTER (WHERE id = 1)) / 1048576 "
        "FROM pm_mem WHERE at = %s", (at,)).fetchone()
    print(f"  {at}: {readings} readings, growth {mb} MB")
    return "yes" if readings == 2 and mb is not None and mb < 32 else f"no (readings|MB: {readings}|{mb})"


def test_a_keyed_update_keeps_its_memory_bounded(pgc_conn, expect):
    """On a keyed table each updated or moved row is checked at once against the
    committed delete marks of its row group (#1421). Kept in the statement's memory,
    those reads cost one copy of the group's delete bitmap per row: 402 MB for
    25,000 rows of a 50,000-row group (OffgridwithJD, review of #1422). pm_mem records
    the backend's context bytes at the first and the last row an UPDATE reaches.
    """
    pgc_conn.execute("CREATE TABLE pm_mem (at text, id int, bytes bigint)")
    pgc_conn.execute(PM_MEM)
    _pm_build(pgc_conn, "pmu")
    _pm_build(pgc_conn, "pmm")
    expect.text(f"{_marks(pgc_conn, 'pmu')} {_marks(pgc_conn, 'pmm')}", "25000|1 25000|1",
                "each keyed table's group carries delete marks before its UPDATE")
    pgc_conn.execute("UPDATE pmu SET v = v + 1 + pm_mem('update', id)")
    expect.text(_bounded(pgc_conn, "update"), "yes",
                "an UPDATE of 25,000 keyed rows keeps under 32 MB of memory between its first and last row")
    pgc_conn.execute("UPDATE pmm SET id = id + 100000 + pm_mem('move', id)")
    expect.text(_bounded(pgc_conn, "move"), "yes",
                "an UPDATE moving 25,000 keyed rows to the other partition keeps under 32 MB likewise")
    expect.text(_value(pgc_conn, "SELECT (SELECT sum(v) FROM pmu) || '/' || (SELECT count(*) FROM pmm_1) "
                                 "|| '/' || (SELECT count(*) FROM pmm_2)"), "25000/0/25000",
                "and each UPDATE reached every row once")

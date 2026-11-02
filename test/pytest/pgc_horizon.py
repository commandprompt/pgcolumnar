"""Wait for the oldest-xmin horizon before reading what compact or truncate freed.

The pytest twin of `pgc_retry_past_horizon` and `pgc_horizon_holders` in
test/lib.sh (#1979, #1981).

compact retires a dead row group only when no transaction in the database is older
than the DELETE. truncate removes a freed range only when no transaction is older
than the compact that freed it. While such a transaction is open, either one returns
0 and the check after it measures nothing. An autoanalyze, or a session another test
left open, is enough. So `retry_past_horizon` runs the statement again every 0.5 s,
up to 60 calls (30 s), until the counts it returns add up to the number wanted.

When the total is still short, it prints what holds the horizon back, so a red
names the holder. pytest shows that output beside the failed check. The caller's
check reports the total itself.
"""
import time

CALLS = 60
PAUSE = 0.5

HOLDERS = """
SELECT 'session pid=' || pid || ' backend_type=' || coalesce(backend_type, '-')
       || ' database=' || coalesce(datname, '-') || ' state=' || coalesce(state, '-')
       || ' backend_xid=' || coalesce(backend_xid::text, '-')
       || ' backend_xmin=' || coalesce(backend_xmin::text, '-')
       || ' xact_start=' || coalesce(xact_start::text, '-')
       || ' query=' || translate(left(coalesce(query, ''), 200), E'\\n\\r\\t', '   ')
  FROM pg_stat_activity
 WHERE pid <> pg_backend_pid() AND (backend_xmin IS NOT NULL OR backend_xid IS NOT NULL)
UNION ALL
SELECT 'prepared transaction gid=' || gid || ' transaction=' || transaction
       || ' database=' || database || ' prepared=' || prepared
  FROM pg_prepared_xacts
UNION ALL
SELECT 'replication slot ' || slot_name || ' xmin=' || coalesce(xmin::text, '-')
       || ' catalog_xmin=' || coalesce(catalog_xmin::text, '-')
  FROM pg_replication_slots WHERE xmin IS NOT NULL OR catalog_xmin IS NOT NULL
"""


def horizon_holders(conn):
    """One line for each session, prepared transaction or replication slot that
    can hold the horizon back, or ["none found"]. Leaves out this session."""
    with conn.cursor() as cur:
        cur.execute(HOLDERS)
        rows = [r[0] for r in cur.fetchall()]
    return rows or ["none found"]


def retry_past_horizon(conn, sql, want, calls=CALLS, pause=PAUSE):
    """Run `sql`, which returns one integer, until the counts add up to `want`.

    -> (total, calls made). An error from `sql` raises at once, so it does not
    read as a held horizon.
    """
    total = made = 0
    with conn.cursor() as cur:
        while total < want and made < calls:
            if made:
                time.sleep(pause)
            cur.execute(sql)
            total += cur.fetchone()[0]
            made += 1
    if total < want:
        print(f"-- [{sql}] returned {total} of {want} in {made} calls. "
              "What holds the horizon back:")
        for line in horizon_holders(conn):
            print(f"--   {line}")
    return total, made


# The twin of `pgc_freed_range_blockers` in test/lib.sh (#1988, #1992). truncate's
# own snapshot has as its xmin the oldest transaction ID running in any database.
# So a session with an xid, or a prepared transaction, holds truncate back from any
# database. A snapshot with no xid holds it only from this database, or from a
# session with no database such as a walsender. Replication slots count too. xid
# has no ordering operator, so age() compares: the older xid has the larger age, and
# an xid equal to freed_xid holds it too. With no freed range the query returns
# NULL, because a count would read 0 without measuring anything.
FREED_RANGE_BLOCKERS = """
WITH f AS (SELECT min(age(freed_xid::text::xid)) AS a FROM pgcolumnar.free_space
            WHERE storage_id = pgcolumnar.get_storage_id(%s) AND freed_xid >= 3)
SELECT CASE WHEN f.a IS NOT NULL THEN
         (SELECT count(*) FROM pg_stat_activity
           WHERE pid <> pg_backend_pid()
             AND (age(backend_xid) >= f.a
                  OR ((datname = current_database() OR datid IS NULL)
                      AND age(backend_xmin) >= f.a)))
       + (SELECT count(*) FROM pg_prepared_xacts WHERE age(transaction) >= f.a)
       + (SELECT count(*) FROM pg_replication_slots WHERE age(xmin) >= f.a)
       END
  FROM f
"""


def freed_range_blockers(conn, table):
    """How many sessions, prepared transactions and replication slots hold the
    horizon at or before the newest range compact freed in `table` (see above).
    truncate removes a freed range only when the horizon has passed the transaction
    that freed it. None when `table` has no freed range. A backend running VACUUM is
    counted although PostgreSQL skips it; that only makes a wait longer."""
    with conn.cursor() as cur:
        cur.execute(FREED_RANGE_BLOCKERS, (table,))
        return cur.fetchone()[0]


def wait_freed_range_past_horizon(conn, table, calls=CALLS, pause=PAUSE):
    """Wait until nothing holds the horizon at or before the newest range compact
    freed in `table` (#1988). For a check that a truncate does NOT shrink the file:
    while the horizon holds, truncate leaves the file alone for that reason alone.

    -> (blockers, calls made). When blockers is not 0, what holds the horizon is
    printed. blockers is None, after one call, when `table` has no freed range
    (#1992), so a check that wants 0 fails.
    """
    n = made = 0
    while made < calls:
        if made:
            time.sleep(pause)
        n = freed_range_blockers(conn, table)
        made += 1
        if n is None:
            print(f"-- no count for {table}: it has no freed range")
            return None, made
        if n == 0:
            break
    if n != 0:
        print(f"-- the newest range freed in {table} is still held after {made} calls "
              f"({n}). What holds the horizon back:")
        for line in horizon_holders(conn):
            print(f"--   {line}")
    return n, made

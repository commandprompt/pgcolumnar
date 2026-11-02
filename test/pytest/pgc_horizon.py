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

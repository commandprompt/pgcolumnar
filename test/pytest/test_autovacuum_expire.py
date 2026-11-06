"""The maintenance daemon runs `pgcolumnar.expire` only when told to (#1290).

`pgcolumnar.autovacuum_expire` is off by default -- the owner's decision on #1290 --
because expire deletes rows, and turning the daemon on must not start deleting them
by itself. With both switches on, each pass expires every table that declares a
retention, in its own transaction, before compaction.

Independent of `test/autovacuum_expire.sh`: its own schema, its own connection, its
own SQL and its own polling. Neither file reads the other. Check names match so the
pair can be graded by name.

WHERE THIS PORT DIFFERS, AND WHY IT IS NOT COSMETIC. The shell suite starts its
server with the daemon already on and a 2s naptime in postgresql.conf. This corpus
shares one cluster per worker, so the settings arrive by ALTER SYSTEM and a reload,
and are read back from a fresh session before anything depends on them. And because
the cluster is shared, every setting this module changes is reset at teardown:
the daemon visits every database, so leaving `autovacuum_expire` on would expire
other modules' retention tables.
"""

import re
import time

import pytest

SCHEMA = "pgc_avexp"
GROUP = 1000
NAP = 2
POLL_S = 40
# Every daemon setting this module touches, so teardown can put each one back.
TOUCHED = ("pgcolumnar.autovacuum", "pgcolumnar.autovacuum_expire",
           "pgcolumnar.autovacuum_naptime", "pgcolumnar.autovacuum_compact_threshold",
           "pgcolumnar.maintenance_hold_ms", "deadlock_timeout")


def _one(conn, sql):
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def _fresh(cluster, sql):
    """One value from a NEW session: a reload is absorbed at session start, so the
    module's long-lived connection can report the value from before it."""
    import psycopg

    c = psycopg.connect(cluster.dsn(), autocommit=True)
    try:
        return _one(c, sql)
    finally:
        c.close()


def _sqlstate(conn, sql):
    """-> the SQLSTATE `sql` raises, or 'noerror'."""
    import psycopg

    try:
        conn.execute(sql)
    except psycopg.Error as e:
        return e.sqlstate or "unreadable"
    return "noerror"


def _log_count(cluster, pattern):
    path = cluster.datadir / "server.log"
    if not path.exists():
        return -1
    return sum(1 for line in path.read_text(errors="replace").splitlines()
               if re.search(pattern, line))


def _until(fn, want, seconds=POLL_S):
    """Re-read `fn()` once a second until it returns `want`; -> the last reading."""
    got = fn()
    deadline = time.monotonic() + seconds
    while got != want and time.monotonic() < deadline:
        time.sleep(1)
        got = fn()
    return got


def _reload(conn, **gucs):
    for k, v in gucs.items():
        conn.execute(f"ALTER SYSTEM SET {k.replace('__', '.')} = {v}")
    conn.execute("SELECT pg_reload_conf()")


@pytest.fixture(scope="module")
def ax(pgc_cluster):
    import psycopg

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    conn.execute(f'CREATE SCHEMA "{SCHEMA}"')
    conn.execute(f'SET search_path TO "{SCHEMA}", public')
    yield conn
    # The hold first, so the daemon is not sitting in a 30s expire when the schema
    # is dropped; then every other setting, so no later module meets this daemon.
    # Each reset on its own: on a build without the switch, its RESET raises, and
    # one raise must not leave the settings after it in force for later modules.
    for g in TOUCHED:
        try:
            conn.execute(f"ALTER SYSTEM RESET {g}")
        except psycopg.Error:
            pass
    conn.execute("SELECT pg_reload_conf()")
    conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    conn.close()


def _aged(conn, t):
    return _one(conn, f"SELECT count(*) FROM {t} WHERE ts < now() - interval '1 day'")


def _fresh_rows(conn, t):
    return _one(conn, f"SELECT count(*) FROM {t} WHERE ts >= now() - interval '1 day'")


def _dead(conn, t):
    return _one(conn, f"SELECT COALESCE(sum(deletedrows), 0) FROM pgcolumnar.stats('{t}')")


def _ttl_table(conn, t, fresh=GROUP // 2):
    """Two whole row groups dated 40 days back, then `fresh` rows dated now."""
    conn.execute(f"CREATE TABLE {t} (ts timestamptz, v int) USING pgcolumnar")
    conn.execute(f"SELECT pgcolumnar.set_options('{t}', chunk_group_row_limit => {GROUP},"
                 f" stripe_row_limit => {GROUP}, ttl_column => 'ts', ttl_interval => '1 day')")
    conn.execute(f"INSERT INTO {t} SELECT now() - interval '40 days' + g * interval '1 minute', g"
                 f" FROM generate_series(1, {2 * GROUP}) g")
    conn.execute(f"INSERT INTO {t} SELECT now() + g * interval '1 minute', g"
                 f" FROM generate_series(1, {fresh}) g")


def _deleted_heavy(conn, t, extra_cols=""):
    conn.execute(f"CREATE TABLE {t} (id int{extra_cols}, v int) USING pgcolumnar")
    conn.execute(f"SELECT pgcolumnar.set_options('{t}', chunk_group_row_limit => {GROUP})")


@pytest.fixture(scope="module")
def run(ax, pgc_cluster):
    """The whole sequence, in the order the shell suite runs it, recorded once."""
    st = {}
    _reload(ax, pgcolumnar__autovacuum="on", pgcolumnar__autovacuum_naptime=NAP,
            pgcolumnar__autovacuum_compact_threshold=0.1, deadlock_timeout="'1s'")
    st["launcher"] = _one(ax, "SELECT count(*) FROM pg_stat_activity"
                              " WHERE backend_type = 'pgcolumnar autovacuum launcher'")
    st["switches"] = _fresh(pgc_cluster, "SELECT current_setting('pgcolumnar.autovacuum')"
                                         " || '/' || current_setting('pgcolumnar.autovacuum_expire', true)")

    _ttl_table(ax, "ex_copy")
    st["manual"] = _one(ax, "SELECT pgcolumnar.expire('ex_copy')")

    # OFF: the daemon is running passes -- it compacts this -- and expires nothing.
    _ttl_table(ax, "ex_off")
    _deleted_heavy(ax, "ex_busy")
    ax.execute("INSERT INTO ex_busy SELECT g, g FROM generate_series(1, 20000) g")
    ax.execute("DELETE FROM ex_busy WHERE (id * 2654435761)::bigint % 100 < 40")
    st["busy_dead"] = _until(lambda: _dead(ax, "ex_busy"), 0)
    time.sleep(2 * NAP)
    st["off_aged"] = _aged(ax, "ex_off")

    # ON
    ax.execute("CREATE TABLE ex_none (ts timestamptz, v int) USING pgcolumnar")
    ax.execute(f"SELECT pgcolumnar.set_options('ex_none', chunk_group_row_limit => {GROUP},"
               f" stripe_row_limit => {GROUP})")
    ax.execute(f"INSERT INTO ex_none SELECT now() - interval '40 days' + g * interval '1 minute', g"
               f" FROM generate_series(1, {2 * GROUP}) g")
    _reload(ax, pgcolumnar__autovacuum_expire="on")
    st["on_aged"] = _until(lambda: _aged(ax, "ex_off"), 0)
    st["on_fresh"] = _fresh_rows(ax, "ex_off")
    st["none_aged"] = _aged(ax, "ex_none")
    st["logged_retired"] = _log_count(pgc_cluster,
                                      rf"pgcolumnar autovacuum: expire .*\b{SCHEMA}\.ex_off retired 2 row groups")
    st["none_skipped"] = _log_count(pgc_cluster, rf"pgcolumnar autovacuum: skipped .*\bex_none\b")
    # More passes find nothing left in ex_off; those must not write at LOG.
    time.sleep(2 * NAP + 1)
    st["zero_logged"] = _log_count(pgc_cluster,
                                   rf"pgcolumnar autovacuum: expire .*\b{SCHEMA}\.ex_off retired 0 row groups")

    # isolate: built with expire off, so neither verb runs until both are set.
    _reload(ax, pgcolumnar__autovacuum_expire="off")
    time.sleep(NAP + 1)
    _deleted_heavy(ax, "ex_bad", extra_cols=", ts timestamptz")
    ax.execute("SELECT pgcolumnar.set_options('ex_bad', ttl_column => 'ts', ttl_interval => '1 day')")
    ax.execute("INSERT INTO ex_bad SELECT g, now(), g FROM generate_series(1, 20000) g")
    ax.execute("DELETE FROM ex_bad WHERE (id * 2654435761)::bigint % 100 < 40")
    ax.execute("ALTER TABLE ex_bad DROP COLUMN ts")
    st["bad_state"] = _sqlstate(ax, "SELECT pgcolumnar.expire('ex_bad')")
    _reload(ax, pgcolumnar__autovacuum_expire="on")
    st["bad_dead"] = _until(lambda: _dead(ax, "ex_bad"), 0)

    # yield: ex_hold has a retention and nothing else due, so a lock the daemon
    # worker holds on it is expire's. The other retention tables go first, because
    # with the hold on the worker spends 30s inside expire on each of them.
    ax.execute("DROP TABLE ex_copy, ex_off, ex_bad")
    _reload(ax, pgcolumnar__maintenance_hold_ms=30000)
    # One transaction, so the listing worker sees all three in creation order:
    # ex_hold (held), ex_gone (dropped during the hold), ex_after (still reached).
    with ax.transaction():
        for t in ("ex_hold", "ex_gone", "ex_after"):
            _ttl_table(ax, t)
    st["held"] = _until(lambda: _one(
        ax, "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
            " WHERE l.relation = 'ex_hold'::regclass AND l.mode = 'ShareUpdateExclusiveLock'"
            " AND l.granted AND a.backend_type = 'pgcolumnar autovacuum worker'"), 1)

    import psycopg

    ax.execute("DROP TABLE ex_gone")
    t0 = time.monotonic()
    lk = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    try:
        lk.execute(f'SET search_path TO "{SCHEMA}", public')
        lk.execute("SET statement_timeout = '15s'")
        try:
            with lk.transaction():
                lk.execute("LOCK TABLE ex_hold IN ACCESS EXCLUSIVE MODE")
            st["lock_state"] = "granted"
        except psycopg.Error as e:
            st["lock_state"] = e.sqlstate or "unreadable"
    finally:
        lk.close()
    st["lock_s"] = time.monotonic() - t0
    time.sleep(1)
    st["yield_logged"] = _log_count(pgc_cluster,
                                    r"pgcolumnar autovacuum: skipped .*\bex_hold\b.*canceling statement")
    st["after_aged"] = _until(lambda: _aged(ax, "ex_after"), 0, seconds=45)
    st["gone_skipped"] = _log_count(pgc_cluster, r"pgcolumnar autovacuum: skipped expire on .*\bex_gone\b")
    _reload(ax, pgcolumnar__maintenance_hold_ms=0)
    print(f"-- yield: lock {st['lock_state']} after {st['lock_s']:.1f}s;"
          f" busy_dead={st['busy_dead']} on_aged={st['on_aged']} bad_dead={st['bad_dead']}")
    return st


def test_the_switch_exists_and_is_off_by_default(run, expect):
    # Not a premise (#1339): the launcher is pgcolumnar's own, so a repository change
    # reddens this -- with it never registered. The same holds below for the daemon's
    # compaction pass and expire's refusal on a missing TTL column.
    expect.num(run["launcher"], 1, "the maintenance launcher is running")
    expect.text(run["switches"], "on/off",
                "the daemon's expire switch exists and is OFF by default")
    expect.num(run["manual"], 2,
               "a manual expire on the same fixture retires its two aged groups")


def test_with_the_switch_off_the_daemon_expires_nothing(run, expect):
    expect.num(run["busy_dead"], 0,
               "the daemon compacted a deleted-heavy table, so it was running passes")
    expect.num(run["off_aged"], 2 * GROUP,
               "OFF: with autovacuum_expire off the daemon retired nothing")


def test_with_the_switch_on_the_daemon_expires_only_what_aged(run, expect):
    expect.num(run["on_aged"], 0, "ON: the daemon expired the aged-out rows without being asked")
    expect.num(run["on_fresh"], GROUP // 2, "ON: no row inside the retention was lost")
    expect.num(run["none_aged"], 2 * GROUP, "ON: a table with no retention keeps every row")
    expect.text("yes" if run["logged_retired"] >= 1 else "no", "yes",
                "ON: the daemon logged what it retired")
    expect.num(run["none_skipped"], 0,
               "ON: a table with no retention is never named in a skipped line")
    expect.num(run["zero_logged"], 0, "a pass that retires nothing logs nothing at LOG for that table")


def test_a_failing_expire_does_not_undo_the_compaction(run, expect):
    expect.text(run["bad_state"], "42703",
                "expire on a table whose TTL column is gone fails with undefined_column")
    expect.num(run["bad_dead"], 0,
               "isolate: a failing expire does not roll back the compaction due on the same table")


def test_the_daemon_yields_the_lock_it_holds_inside_expire(run, expect):
    expect.num(run["held"], 1,
               "the daemon holds ShareUpdateExclusiveLock inside expire (the hold window is open)")
    expect.text(run["lock_state"], "granted",
                "yield: the AccessExclusive lock on a table in expire was granted")
    expect.text("fast" if run["lock_s"] <= 8 else f"slow({run['lock_s']:.0f}s)", "fast",
                "yield: and it was granted fast (<= 8s, far under the 30s hold)")
    expect.text("yes" if run["yield_logged"] >= 1 else "no", "yes",
                "yield: the daemon logged cancelling expire")


def test_a_table_dropped_mid_pass_is_skipped_and_the_pass_goes_on(run, expect):
    expect.num(run["after_aged"], 0, "dropped: and the pass goes on to expire the next table")
    expect.text("yes" if run["gone_skipped"] >= 1 else "no", "yes",
                "dropped: a table dropped mid-pass is logged as skipped")


def test_the_daemon_cuts_a_date_retention_at_its_databases_time_zone(pgc_own_db, pgc_cluster, expect):
    """Which TimeZone the daemon's expire uses for a date retention (#1341). how-to.md
    said the server's; measured, it follows the server's, including a reloaded change,
    and a database-level ALTER DATABASE ... SET TimeZone overrides it, as for any
    session connecting to that database. Two zones 25 hours apart, never on the same
    date. One row group per day, so the first surviving day IS the daemon's cutoff.

    A private database, so the database-level setting reaches no other module, and every
    setting this changes is reset before it returns: the daemon visits every database."""
    conn = pgc_own_db
    db = _one(conn, "SELECT current_database()")
    touched = ("timezone", "pgcolumnar.autovacuum", "pgcolumnar.autovacuum_expire", "pgcolumnar.autovacuum_naptime")

    def cut(zone):
        return _one(conn, f"SELECT ((now() AT TIME ZONE '{zone}')::date - 1)::text")

    def expire(on):
        _reload(conn, pgcolumnar__autovacuum_expire="on" if on else "off")
        if not on:
            time.sleep(5)

    def mk(t):
        conn.execute(f"CREATE TABLE {t} (d date, v int) USING pgcolumnar")
        conn.execute(f"SELECT pgcolumnar.set_options('{t}', chunk_group_row_limit => 1000,"
                     " stripe_row_limit => 1000, ttl_column => 'd', ttl_interval => '1 day')")
        for k in range(-6, 7):
            conn.execute(f"INSERT INTO {t} SELECT (now() AT TIME ZONE 'UTC')::date + {k}, g"
                         " FROM generate_series(1, 100) g")

    def first(t, zone, start):
        """'match' if the first surviving day is the zone's cutoff, read just before and
        just after the daemon's passes (a zone's midnight may fall between)."""
        before = cut(zone)
        expire(True)
        got, last, same = None, None, 0
        for _ in range(60):
            got = _one(conn, f"SELECT min(d)::text FROM {t}")
            if got == last and got != start:
                same += 1
                if same >= 8:
                    break
            else:
                same = 0
            last = got
            time.sleep(1)
        after = cut(zone)
        expire(False)
        return "match" if got in (before, after) else f"{got} (want {before} or {after})"

    try:
        _reload(conn, pgcolumnar__autovacuum="on", pgcolumnar__autovacuum_naptime=NAP,
                pgcolumnar__autovacuum_expire="off", timezone="'Pacific/Kiritimati'")
        time.sleep(3)
        mk("tz_k")
        start = _one(conn, "SELECT min(d)::text FROM tz_k")
        groups = _one(conn, "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('tz_k')")
        expect.text(f"{'apart' if cut('Pacific/Kiritimati') != cut('Pacific/Pago_Pago') else 'same'}|{groups}",
                    "apart|13", "premise: the two zones are on different dates, so every arm below can tell them apart")
        expect.text(first("tz_k", "Pacific/Kiritimati", start), "match",
                    "the daemon cuts a date retention at the server's TimeZone")
        _reload(conn, timezone="'Pacific/Pago_Pago'")
        time.sleep(3)
        mk("tz_p")
        expect.text(first("tz_p", "Pacific/Pago_Pago", start), "match",
                    "and follows a change to it once the configuration is reloaded")
        conn.execute(f'ALTER DATABASE "{db}" SET timezone = \'Pacific/Kiritimati\'')
        mk("tz_d")
        expect.text(first("tz_d", "Pacific/Kiritimati", start), "match",
                    "a database-level TimeZone overrides the server's for the daemon too")
    finally:
        conn.execute(f'ALTER DATABASE "{db}" RESET timezone')
        for g in touched:
            conn.execute(f"ALTER SYSTEM RESET {g}")
        conn.execute("SELECT pg_reload_conf()")

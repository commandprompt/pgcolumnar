"""The maintenance daemon compacts and reclusters on its own only while it is on (#415).
The pytest twin of `test/autovacuum.sh`, every check by the shell's name (#1341).

With `pgcolumnar.autovacuum` off, a table with dead rows above the compaction
threshold is left alone across several naptimes; turned on, the daemon compacts it to
no dead rows without changing its survivors, and folds a reclustered table's appended
groups back into its run; turned off again, a new deleted table is left alone.

THE SETTINGS ARRIVE BY ALTER SYSTEM AND A RELOAD. The shell starts its own server with
them in postgresql.conf; this corpus shares one cluster per worker, so each is set,
read back from a fresh session, and reset at teardown: the daemon visits every
database, and leaving it on would compact other modules' tables. "Off by default" is
the setting's boot value, which a shared cluster cannot have changed.

A READING IS NEVER A DEFAULT. "Left alone" needs dead rows to leave and two readings
that agree, so a table that lost its deletes, or a failed read, cannot pass as it.
"""
import time

import psycopg
import pytest

from pgc_vacuity import query_error

SCHEMA = "pgc_autovacuum"
CG = 1000
NAP = 2
POLL_S = 30
TOUCHED = ("pgcolumnar.autovacuum", "pgcolumnar.autovacuum_naptime",
           "pgcolumnar.autovacuum_compact_threshold", "pgcolumnar.autovacuum_recluster_threshold")
SURVIVORS = "SELECT count(*) FROM generate_series(1, 20000) g WHERE NOT ((g * 2654435761)::bigint % 100 < 40)"


def _one(conn, sql):
    try:
        row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])
    return query_error(f"no row from {sql}") if row is None else row[0]


def _fresh(cluster, sql):
    """One value from a NEW session: a reload is absorbed at session start."""
    with psycopg.connect(cluster.dsn(), autocommit=True) as c:
        return _one(c, sql)


def _reload(conn, **gucs):
    for k, v in gucs.items():
        conn.execute(f"ALTER SYSTEM SET {k.replace('__', '.')} = {v}")
    conn.execute("SELECT pg_reload_conf()")


def _dead(conn, t):
    """-> the table's deleted rows as an int (sum() is numeric), or a failed reading."""
    return _one(conn, f"SELECT COALESCE(sum(deletedrows), 0)::bigint FROM pgcolumnar.stats('{SCHEMA}.{t}')")


def _until(fn, want, seconds=POLL_S):
    got = fn()
    deadline = time.monotonic() + seconds
    while got != want and time.monotonic() < deadline:
        time.sleep(1)
        got = fn()
    return got


def _mk_deleted(conn, t):
    """20k rows, 40% scattered-deleted, which is above the 0.1 compaction threshold."""
    conn.execute(f"CREATE TABLE {t} (id int, v int, pad text) USING pgcolumnar")
    conn.execute(f"SELECT pgcolumnar.set_options('{t}', chunk_group_row_limit => {CG})")
    conn.execute(f"INSERT INTO {t} SELECT g, g % 100, md5(g::text) FROM generate_series(1, 20000) g")
    conn.execute(f"DELETE FROM {t} WHERE (id * 2654435761)::bigint % 100 < 40")
    conn.execute(f"SELECT pgcolumnar.compact('{t}')")


def _left_alone(before, after):
    """-> "unchanged" when a table that had dead rows still has the same number."""
    if not isinstance(before, int) or not isinstance(after, int):
        return query_error(f"no reading: [{before}] -> [{after}]")
    if before <= 0:
        return "no reading (no dead rows to leave alone)"
    return "unchanged" if before == after else f"changed: {before} -> {after}"


@pytest.fixture(scope="module")
def av(pgc_cluster):
    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    conn.execute(f'CREATE SCHEMA "{SCHEMA}"')
    conn.execute(f'SET search_path TO "{SCHEMA}", public')
    r = {}
    try:
        _reload(conn, pgcolumnar__autovacuum="off", pgcolumnar__autovacuum_naptime=NAP,
                pgcolumnar__autovacuum_compact_threshold=0.1, pgcolumnar__autovacuum_recluster_threshold=0.05)
        r["launcher"] = _fresh(pgc_cluster, "SELECT count(*) FROM pg_stat_activity "
                                            "WHERE backend_type = 'pgcolumnar autovacuum launcher'")
        r["default"] = _fresh(pgc_cluster, "SELECT boot_val FROM pg_settings WHERE name = 'pgcolumnar.autovacuum'")

        _mk_deleted(conn, "av_off")
        r["off before"] = _dead(conn, "av_off")
        time.sleep(4 * NAP)  # several naptimes; the daemon is off
        r["off after"] = _dead(conn, "av_off")

        _mk_deleted(conn, "av_on")
        r["on before"] = _dead(conn, "av_on")
        _reload(conn, pgcolumnar__autovacuum="on")
        r["on"] = _fresh(pgc_cluster, "SHOW pgcolumnar.autovacuum")
        r["on after"] = _until(lambda: _dead(conn, "av_on"), 0)
        r["survivors"] = _one(conn, "SELECT count(*) FROM av_on")
        r["survivors want"] = _one(conn, SURVIVORS)

        conn.execute(f"CREATE TABLE av_rc (ts int, v int) USING pgcolumnar")
        conn.execute(f"SELECT pgcolumnar.set_options('av_rc', chunk_group_row_limit => {CG})")
        conn.execute("INSERT INTO av_rc SELECT g, g % 100 FROM generate_series(1, 20000) g")
        conn.execute("SELECT pgcolumnar.recluster('av_rc', 'ts')")
        conn.execute("INSERT INTO av_rc SELECT g, g % 100 FROM generate_series(1, 5000) g")
        r["rc premise"] = _one(conn, "SELECT (sort_key IS NOT NULL AND appended_groups > 0)::text "
                                     "FROM pgcolumnar.sort_status('av_rc')")
        r["rc after"] = _until(lambda: _one(conn, "SELECT appended_groups FROM pgcolumnar.sort_status('av_rc')"), 0)

        _reload(conn, pgcolumnar__autovacuum="off")
        time.sleep(1)
        _mk_deleted(conn, "av_off2")
        r["off2 before"] = _dead(conn, "av_off2")
        time.sleep(4 * NAP)
        r["off2 after"] = _dead(conn, "av_off2")
        yield r
    finally:
        for g in TOUCHED:
            try:
                conn.execute(f"ALTER SYSTEM RESET {g}")
            except psycopg.Error:
                pass
        conn.execute("SELECT pg_reload_conf()")
        conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        conn.close()


def test_the_maintenance_launcher_is_running_for_the_daemon(av, expect):
    expect.text(str(av["launcher"]), "1", "the maintenance launcher is running")


def test_the_daemon_is_off_by_default(av, expect):
    expect.text(str(av["default"]), "off", "the daemon is OFF by default")


def test_av_off_starts_with_dead_rows_above_the_threshold(av, expect):
    b = av["off before"]
    expect.text("yes" if isinstance(b, int) and b > 4000 else f"no ({b})", "yes",
                "premise: av_off starts with dead rows above the threshold")


def test_off_the_daemon_did_not_touch_the_table(av, expect):
    expect.text(_left_alone(av["off before"], av["off after"]), "unchanged",
                "OFF: the daemon did not touch the table (dead rows unchanged)")


def test_av_on_starts_with_dead_rows_above_the_threshold(av, expect):
    b = av["on before"]
    expect.text("yes" if isinstance(b, int) and b > 4000 else f"no ({b})", "yes",
                "premise: av_on starts with dead rows above the threshold")


def test_the_daemon_is_now_on(av, expect):
    expect.text(str(av["on"]), "on", "the daemon is now ON")


def test_on_the_daemon_compacted_the_deleted_heavy_table(av, expect):
    expect.text(str(av["on after"]), "0", "ON: the daemon compacted the deleted-heavy table (dead rows now 0)")


def test_on_the_surviving_rows_are_unchanged(av, expect):
    expect.text(str(av["survivors"]), str(av["survivors want"]), "ON: the surviving rows are unchanged")


def test_av_rc_has_a_recorded_key_and_appended_decay(av, expect):
    expect.text(str(av["rc premise"]), "true", "premise: av_rc has a recorded key and appended decay")


def test_on_the_daemon_reclustered_the_decayed_table(av, expect):
    expect.text(str(av["rc after"]), "0",
                "ON: the daemon reclustered the decayed table (appended groups folded to 0)")


def test_off_again_a_new_deleted_table_is_left_alone(av, expect):
    expect.text(_left_alone(av["off2 before"], av["off2 after"]), "unchanged",
                "OFF-again: a new deleted table is left alone after disabling")

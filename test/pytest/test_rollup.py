"""Incremental rollups, phase 1: append-only (#1294).

A rollup is a view over a partials table; refresh_rollup folds the rows of the
row groups appended since the last refresh, in ONE statement, so the group list,
the folded rows and the recorded groups share a snapshot. The design, its eight
points and the owner's decisions are on #1294.

THE ORACLE is a full recompute of the same GROUP BY, every time. Exactness was
fixed by the design so it cannot be loosened here: count, integer sum, min and
max exact; avg of an integer column exact too (numeric partials); avg of
float8 within a relative 1e-12. This file uses an integer avg, so it is exact.

Two things the shell twin found while the code was written, held here too: a
lateral per-group subquery pulled up into a join read every row group (a TID
range is built only from a relation's own restrictions), and every arm run as a
superuser bypassed the row-level security on the rollup catalogs, so an owner
and a stranger are tested as ordinary roles.

ON A PRIVATE DATABASE (`pgc_own_db`): the roles and pg_stat_reset. Independent
of `test/rollup.sh`: its own table, 15-minute buckets, aggregates (count, sum,
max, avg of an integer), zones, row counts and roles.
"""

import threading
import time

import psycopg

ORIGIN = "2025-12-29 06:00:00+00"
AGG_FUNCS = "{count,sum,max,avg}"
AGG_COLS = "{*,reading,temp,reading}"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _state(conn, sql):
    try:
        return str(_one(conn, sql))
    except psycopg.Error as e:
        return e.sqlstate or "?"


def _load(conn, lo, hi, start):
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO feed SELECT '{start}'::timestamptz + (g - {lo}) * interval '23 s',"
                    f" g % 11, (g * 31337) % 65521, ((g * 2654435761) % 4093) / 7.0"
                    f" FROM generate_series({lo}::bigint, {hi}) g")


RECOMPUTE = (f"SELECT date_bin('15 minutes', at, '{ORIGIN}') AS bucket, sensor, count(*) AS count,"
             " sum(reading)::numeric AS sum_reading, max(temp) AS max_temp, avg(reading) AS avg_reading"
             " FROM feed GROUP BY 1, 2")


def _digest(conn, rel_sql):
    # concat_ws over coalesce: a plain || turns a row with any NULL into NULL, and
    # string_agg drops it, so NULL keys would be compared by their absence.
    return _one(conn, "SELECT count(*) || '|' || md5(string_agg(concat_ws(',', coalesce(bucket::text, '~'),"
                      " coalesce(sensor::text, '~'), count, coalesce(sum_reading::text, '~'),"
                      " coalesce(max_temp::text, '~'), coalesce(avg_reading::text, '~')), ';'"
                      f" ORDER BY bucket NULLS FIRST, sensor NULLS FIRST)) FROM ({rel_sql}) x")


def _matches(conn, rollup="gauge_15m"):
    return _digest(conn, f"SELECT * FROM {rollup}")


def _oracle(conn):
    return _digest(conn, RECOMPUTE)


def _refresh(conn, rollup="gauge_15m"):
    return _state(conn, f"SELECT pgcolumnar.refresh_rollup('{rollup}')")


def _folds(conn, rollup="gauge_15m"):
    # "folded", or the SQLSTATE: a count of five digits would read as one.
    try:
        _one(conn, f"SELECT pgcolumnar.refresh_rollup('{rollup}')")
        return "folded"
    except psycopg.Error as e:
        return e.sqlstate or "?"


def test_rollup_phase_one(pgc_own_db, pgc_cluster, expect):
    conn = pgc_own_db
    dsn = pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()"))
    expect.num(_one(conn, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                          " WHERE n.nspname = 'pgcolumnar'"
                          " AND p.proname IN ('create_rollup', 'refresh_rollup', 'rebase_rollup')"), 3,
               "premise: the rollup functions exist")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE feed (at timestamptz, sensor int, reading bigint, temp float8) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('feed', stripe_row_limit => 3000)")
    _load(conn, 1, 24000, "2026-02-01 00:00:00+00")
    expect.num(_one(conn, "SELECT count(*) FROM feed"), 24000, "premise: the source holds every row")

    with conn.cursor() as cur:
        cur.execute(f"SELECT pgcolumnar.create_rollup('gauge_15m', 'feed', 'at', '15 minutes', '{ORIGIN}',"
                    f" '{{sensor}}', '{AGG_FUNCS}', '{AGG_COLS}')")
    expect.text(_one(conn, "SELECT string_agg(attname, ',' ORDER BY attnum) FROM pg_attribute"
                           " WHERE attrelid = to_regclass('gauge_15m') AND attnum > 0 AND NOT attisdropped"),
                "bucket,sensor,count,sum_reading,max_temp,avg_reading",
                "the rollup table exists with the documented columns")

    expect.text(_refresh(conn), "24000", "the first refresh folds every row")
    expect.text(_matches(conn), _oracle(conn), "and after the first refresh the rollup equals a full recompute")
    expect.text(_refresh(conn), "0", "a refresh with nothing new folds nothing")
    expect.text(_matches(conn), _oracle(conn), "and leaves the rollup as it was")

    _load(conn, 24001, 27000, "2026-02-08 00:00:00+00")
    _load(conn, 27001, 27900, "2026-02-01 03:00:00+00")      # late: old buckets
    expect.text(_refresh(conn), "3900",
                "a refresh after appends folds exactly the appended rows, late ones included")
    expect.text(_matches(conn), _oracle(conn), "and the rollup still equals a full recompute")

    # Rows committed while a refresh runs are the next refresh's.
    _load(conn, 27901, 28300, "2026-02-09 00:00:00+00")
    out = {}

    def paused():
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SET pgcolumnar.rollup_pause = on")
            out["folded"] = c.execute("SELECT pgcolumnar.refresh_rollup('gauge_15m')").fetchone()[0]
    th = threading.Thread(target=paused)
    th.start()
    time.sleep(0.8)
    _load(conn, 28301, 28520, "2026-02-09 05:00:00+00")
    th.join(60)
    expect.num(out.get("folded"), 400, "the paused refresh folded what was there before it began")
    expect.text(_refresh(conn), "220", "the next refresh folds the rows committed during the last one, once")
    expect.text(_matches(conn), _oracle(conn), "and after the refresh that ran beside an insert the rollup equals a full recompute")

    # Two refreshes at once.
    _load(conn, 28521, 30020, "2026-02-10 00:00:00+00")
    got = []

    def refresher():
        with psycopg.connect(dsn, autocommit=True) as c:
            got.append(c.execute("SELECT pgcolumnar.refresh_rollup('gauge_15m')").fetchone()[0])
    ts = [threading.Thread(target=refresher) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    expect.text(" ".join(str(x) for x in sorted(got)), "0 1500",
                "two refreshes started together fold the new rows once between them")
    expect.text(_matches(conn), _oracle(conn), "and after two refreshes started together the rollup equals a full recompute")

    # Zones.
    for zone, name in (("UTC", "z_utc"), ("America/St_Johns", "z_stjohns")):
        with conn.cursor() as cur:
            cur.execute(f"SET TimeZone = '{zone}'")
            cur.execute(f"SELECT pgcolumnar.create_rollup('{name}', 'feed', 'at', '15 minutes', '{ORIGIN}',"
                        f" '{{sensor}}', '{AGG_FUNCS}', '{AGG_COLS}')")
            cur.execute(f"SELECT pgcolumnar.refresh_rollup('{name}')")
            cur.execute("SET TimeZone = 'UTC'")
    expect.text(_matches(conn, "z_utc"), _matches(conn, "z_stjohns"),
                "refreshes from sessions in different time zones build the same rollup")
    expect.num(_one(conn, "SELECT count(*) FROM z_utc"), _one(conn, f"SELECT count(*) FROM ({RECOMPUTE}) z"),
               "the zone rollups hold a row per key of the recompute")
    expect.at_least(_one(conn, "SELECT count(*) FROM z_utc"), 1, "and that is not zero")

    # Cost, on a source of real size.
    _load(conn, 100001, 260000, "2026-03-01 00:00:00+00")
    expect.text(_refresh(conn), "160000", "a refresh folds a bulk append")
    expect.text(_matches(conn), _oracle(conn), "and after the bulk append the rollup equals a full recompute")
    _load(conn, 30021, 30820, "2026-02-11 00:00:00+00")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT pg_stat_reset()")
        cur.execute("SELECT pgcolumnar.refresh_rollup('gauge_15m')")
        cur.execute("SELECT pg_stat_force_next_flush()")
    read = _one(conn, "SELECT coalesce(heap_blks_read, 0) + coalesce(heap_blks_hit, 0)"
                      " FROM pg_statio_user_tables WHERE relname = 'feed'")
    pages = _one(conn, "SELECT pg_relation_size('feed') / 8192")
    print(f"-- refresh after an 800-row append read {read} of {pages} blocks")
    expect.at_least(read, 1, "the refresh after a small append read the source at all")
    expect.at_least(pages - 5 * read if read else 0, 1,
                    "a refresh after a small append reads under a fifth of the source")
    expect.text(_matches(conn), _oracle(conn), "and after the small append the rollup equals a full recompute")

    # A rewrite of the source, and rebase.
    def identity():
        return _one(conn, "SELECT pgcolumnar.get_storage_id('feed') || ':' || string_agg(group_number::text, ','"
                          " ORDER BY group_number) FROM pgcolumnar.row_group"
                          " WHERE storage_id = pgcolumnar.get_storage_id('feed')")
    before = identity()
    with conn.cursor() as cur:
        cur.execute("SELECT pgcolumnar.vacuum('feed')")
    expect.text("renumbered" if identity() != before else "same", "renumbered",
                "pgcolumnar.vacuum gave the source's row groups new identities")
    vb = _matches(conn)
    expect.at_least(_one(conn, "SELECT count(*) FROM gauge_15m"), 1, "the rollup holds rows before the rewrite")
    expect.text(f"{_refresh(conn)}|{_matches(conn)}", f"55000|{vb}",
                "a refresh after the source was rewritten is refused, naming rebase_rollup")
    # Not through _state: a SQLSTATE such as 42883 is all digits and would read as a count.
    try:
        rebased = _one(conn, "SELECT pgcolumnar.rebase_rollup('gauge_15m')")
    except psycopg.Error:
        rebased = -1
    expect.at_least(rebased, 1, "rebase_rollup records the source's current row groups")
    expect.text(_matches(conn), vb, "and leaves the rollup's rows alone")
    _load(conn, 30821, 31420, "2026-02-12 00:00:00+00")
    expect.text(_refresh(conn), "600", "after a rebase the next refresh folds exactly the appended rows")
    expect.text(_matches(conn), _oracle(conn), "and after the rebase the rollup equals a full recompute")

    # Phase 2: a delete, update or rewrite makes the buckets of the changed groups
    # dirty; the refresh recomputes them and leaves every other partial alone (a
    # recomputed partial is a new row, so a new ctid). The groups were last
    # recorded by rebase_rollup, so this also needs rebase to record time ranges.
    far = "bucket >= '2026-02-04 00:00:00+00'"     # past every group the delete reaches
    at_far = ("SELECT count(*) || '|' || md5(string_agg(ctid::text, ',' ORDER BY ctid))"
              f" FROM gauge_15m__partials WHERE {far}")
    far_before = _one(conn, at_far)
    expect.at_least(_one(conn, f"SELECT count(*) FROM gauge_15m__partials WHERE {far}"), 1,
                    "the rollup holds partials the delete below cannot reach")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM feed WHERE sensor = 4 AND at < '2026-02-01 01:00:00+00'")
    expect.text(_folds(conn), "folded",
                "a refresh after a delete is not refused")
    expect.text(_matches(conn), _oracle(conn), "and after a delete the rollup equals a full recompute")
    expect.text(_one(conn, at_far), far_before,
                "and the refresh rewrote no partial outside the changed groups' time range")
    expect.text(_refresh(conn), "0", "and the next refresh, with nothing changed since, recomputes nothing")

    with conn.cursor() as cur:
        cur.execute("UPDATE feed SET at = at + interval '3 days 7 hours'"
                    " WHERE at >= '2026-02-02 08:00:00+00' AND at < '2026-02-02 08:10:00+00'")
    _refresh(conn)
    expect.text(_matches(conn), _oracle(conn),
                "after an update that moves rows to another bucket the rollup equals a full recompute")

    # A dirty bucket and a new group in one refresh: the recompute already counts
    # the new rows in the dirty bucket. 150 rows an hour apart outrun any group.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM feed WHERE sensor = 2 AND at >= '2026-02-02 20:00:00+00'"
                    " AND at < '2026-02-02 20:15:00+00'")
        cur.execute("INSERT INTO feed SELECT '2026-02-02 20:05:00+00'::timestamptz + g * interval '1 hour',"
                    " 2, g * 3, g / 7.0 FROM generate_series(0, 149) g")
    _refresh(conn)
    expect.text(_matches(conn), _oracle(conn),
                "after a delete and an append that straddles its bucket, a refresh counts the new rows once")

    with conn.cursor() as cur:
        cur.execute("DELETE FROM feed WHERE at >= '2026-02-05 00:00:00+00' AND at < '2026-02-05 09:00:00+00'")
    try:
        rewrote = _one(conn, "SELECT pgcolumnar.compact_rewrite('feed', 0.02)")
    except psycopg.Error:
        rewrote = -1
    expect.at_least(rewrote, 1, "compact_rewrite rewrote a row group")
    _refresh(conn)
    expect.text(_matches(conn), _oracle(conn), "after compact_rewrite the rollup equals a full recompute")
    expect.text(_refresh(conn), "0", "and the refresh after the rewrite recomputes nothing")

    # The TID range of each row group comes from the access method's own mapping
    # (row_tid), so it holds exactly the group's rows at any block size.
    bad, groups = conn.execute(
        "SELECT count(*) FILTER (WHERE got <> want), count(*) FROM ("
        " SELECT g.row_count - g.deleted_count AS want,"
        "  (SELECT count(*) FROM feed WHERE ctid >= pgcolumnar.row_tid(g.first_row_number)"
        "     AND ctid <= pgcolumnar.row_tid(g.first_row_number + g.row_count - 1)) AS got"
        " FROM pgcolumnar.rollup_source_groups('feed') g) x").fetchone()
    expect.text(f"{bad}|{groups}",
                f"0|{_one(conn, 'SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id(' + chr(39) + 'feed' + chr(39) + ')')}",
                "each row group's TID range holds exactly that group's rows")

    # NULL keys and NULL timestamps (#1319 review): keys match with IS NOT
    # DISTINCT FROM, and a NULL ts is a NULL bucket.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE holes (at timestamptz, sensor int, reading bigint, temp float8) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('holes', stripe_row_limit => 1000)")
        cur.execute("INSERT INTO holes SELECT CASE WHEN g % 13 = 0 THEN NULL ELSE '2026-07-01'::timestamptz"
                    " + g * interval '47 s' END, CASE WHEN g % 6 = 0 THEN NULL ELSE g % 4 END, g, g / 4.0"
                    " FROM generate_series(1, 2600) g")
        cur.execute(f"SELECT pgcolumnar.create_rollup('holes_15m', 'holes', 'at', '15 minutes', '{ORIGIN}',"
                    f" '{{sensor}}', '{AGG_FUNCS}', '{AGG_COLS}')")
        cur.execute("SELECT pgcolumnar.refresh_rollup('holes_15m')")
        cur.execute("INSERT INTO holes SELECT CASE WHEN g % 13 = 0 THEN NULL ELSE '2026-07-01'::timestamptz"
                    " + g * interval '47 s' END, CASE WHEN g % 6 = 0 THEN NULL ELSE g % 4 END, g, g / 4.0"
                    " FROM generate_series(2601, 3900) g")
    expect.text(_refresh(conn, "holes_15m"), "1300", "the second refresh of the NULL-key rollup folded the append")
    holes_recompute = RECOMPUTE.replace("FROM feed", "FROM holes")
    nulls = f"SELECT count(*) FILTER (WHERE sensor IS NULL) || ' ' || count(*) FILTER (WHERE bucket IS NULL) FROM ({{}}) z"
    expect.text(f"{_digest(conn, 'SELECT * FROM holes_15m')}|{_one(conn, nulls.format('SELECT * FROM holes_15m'))}",
                f"{_digest(conn, holes_recompute)}|{_one(conn, nulls.format(holes_recompute))}",
                "a rollup with NULL keys and NULL timestamps equals a full recompute")
    expect.at_least(_one(conn, f"SELECT count(*) FROM ({holes_recompute}) z WHERE sensor IS NULL OR bucket IS NULL"), 2,
                    "premise: and the fixture has NULL-key and NULL-bucket groups")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM holes WHERE at IS NULL AND sensor = 1")
    _refresh(conn, "holes_15m")
    expect.text(_digest(conn, "SELECT * FROM holes_15m"), _digest(conn, holes_recompute),
                "after a delete of rows whose ts is NULL the NULL-bucket rollup equals a full recompute")

    # Retention keeps history: groups expire retires (pgcolumnar.retired_group,
    # #1314) leave the snapshot, and their buckets keep what they folded.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE aged (at timestamptz, reading int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('aged', stripe_row_limit => 1000)")
        cur.execute("INSERT INTO aged SELECT now() - interval '90 days' + g * interval '45 s', g % 97"
                    " FROM generate_series(1, 3200) g")
        cur.execute("INSERT INTO aged SELECT now() - interval '3 days' + g * interval '20 s', g % 89"
                    " FROM generate_series(1, 1600) g")
        cur.execute(f"SELECT pgcolumnar.create_rollup('aged_hourly', 'aged', 'at', '1 hour', '{ORIGIN}', '{{}}',"
                    " '{count,max}', '{*,reading}')")
    expect.text(_refresh(conn, "aged_hourly"), "4800", "the history rollup folded every row")
    hist = "SELECT md5(string_agg(bucket || ':' || count || ':' || max_reading, ',' ORDER BY bucket)) FROM aged_hourly"
    before_hist = _one(conn, hist)
    old_buckets = _one(conn, "SELECT count(*) FROM aged_hourly WHERE bucket < now() - interval '60 days'")
    expect.at_least(old_buckets, 1, "the history rollup holds old buckets")
    with conn.cursor() as cur:
        cur.execute("SELECT pgcolumnar.set_options('aged', ttl_column => 'at', ttl_interval => '14 days')")
    expect.at_least(_one(conn, "SELECT pgcolumnar.expire('aged')"), 1, "expire retired the old row groups")
    expect.text(f"{_refresh(conn, 'aged_hourly')}|{_one(conn, hist)}", f"0|{before_hist}",
                "a refresh after retention folds nothing, refuses nothing, and keeps every bucket")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.rollup_group rg JOIN pgcolumnar.retired_group t"
                          " ON t.group_number = rg.group_number AND t.storage_id = pgcolumnar.get_storage_id('aged')"
                          " WHERE rg.rollup = 'aged_hourly'::regclass"), 0,
               "and the rollup no longer records the groups retention retired")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO aged SELECT now() - interval '5 hours' + g * interval '3 s', g % 83"
                    " FROM generate_series(1, 700) g")
    expect.text(_refresh(conn, "aged_hourly"), "700", "and after retention an append still folds")
    recent = _one(conn, "SELECT md5(string_agg(bucket || ':' || count || ':' || max_reading, ',' ORDER BY bucket))"
                        " FROM aged_hourly WHERE bucket >= now() - interval '14 days'")
    recomputed = _one(conn, f"SELECT md5(string_agg(b || ':' || n || ':' || m, ',' ORDER BY b)) FROM"
                            f" (SELECT date_bin('1 hour', at, '{ORIGIN}') b, count(*) n, max(reading) m FROM aged"
                            " WHERE at >= now() - interval '14 days' GROUP BY 1) x WHERE b >= now() - interval '14 days'")
    still_old = _one(conn, "SELECT count(*) FROM aged_hourly WHERE bucket < now() - interval '60 days'")
    expect.text(f"{recent}|{still_old}", f"{recomputed}|{old_buckets}",
                "and the recent buckets equal a recompute, while the expired ones keep their history")

    # History and dirty buckets: a bucket that also holds a retired group's rows
    # cannot be recomputed, so that refresh is refused and changes nothing; a
    # delete whose buckets hold no history folds. One group per INSERT: OLD
    # retires, KEPT shares OLD's hour H and reaches past the cutoff, LATE is
    # days later.
    h = f"date_bin('1 hour', now() - interval '20 days', '{ORIGIN}')"

    def layer(table):
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {table} (at timestamptz, reading int) USING pgcolumnar")
            cur.execute(f"SELECT pgcolumnar.set_options('{table}', stripe_row_limit => 5000)")
            cur.execute(f"INSERT INTO {table} SELECT {h} + g * interval '7 s', g FROM generate_series(1, 300) g")
            cur.execute(f"INSERT INTO {table} SELECT {h} + interval '40 min' + g * interval '10 min', g"
                        " FROM generate_series(1, 1000) g")
            cur.execute(f"INSERT INTO {table} SELECT {h} + interval '12 days' + g * interval '1 min', g"
                        " FROM generate_series(1, 900) g")
            cur.execute(f"SELECT pgcolumnar.create_rollup('{table}_hourly', '{table}', 'at', '1 hour', '{ORIGIN}',"
                        " '{}', '{count,max}', '{*,reading}')")
            cur.execute(f"SELECT pgcolumnar.refresh_rollup('{table}_hourly')")
            cur.execute(f"SELECT pgcolumnar.set_options('{table}', ttl_column => 'at', ttl_interval => '14 days')")
            cur.execute(f"SELECT pgcolumnar.expire('{table}')")
    layer("layered")
    expect.text(_one(conn, f"SELECT count(*) FILTER (WHERE at < {h} + interval '40 min') || '|'"
                           f" || count(*) FILTER (WHERE at >= {h} + interval '40 min' AND at < {h} + interval '1 hour')"
                           " FROM layered"), "0|1",
                "expire retired the group in bucket D alone and kept the one that shares it")
    lq = "SELECT md5(string_agg(bucket || ':' || count || ':' || max_reading, ',' ORDER BY bucket)) FROM layered_hourly"
    lr = (f"SELECT md5(string_agg(b || ':' || n || ':' || m, ',' ORDER BY b)) FROM (SELECT date_bin('1 hour', at,"
          f" '{ORIGIN}') b, count(*) n, max(reading) m FROM layered GROUP BY 1) x")
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM layered WHERE at >= {h} + interval '12 days 5 hours' AND reading % 3 = 0")
    expect.text(f"{_folds(conn, 'layered_hourly')}|{_one(conn, f'{lq} WHERE bucket > {h}')}"
                f"|{_one(conn, f'SELECT count || chr(58) || max_reading FROM layered_hourly WHERE bucket = {h}')}",
                f"folded|{_one(conn, f'{lr} WHERE b > {h}')}|301:300",
                "a delete in buckets with no history, next to retired ones, folds: "
                "those buckets equal a recompute and D keeps its history")
    before_layered = _one(conn, lq)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM layered WHERE at >= {h} + interval '2 hours' AND at < {h} + interval '3 hours'")
    expect.text(f"{_refresh(conn, 'layered_hourly')}|{_one(conn, lq)}", f"0A000|{before_layered}",
                "a delete in a bucket that holds retired history is refused, and the rollup is unchanged")

    # No refresh between expire and the delete: the same refresh retires the
    # history that blocks it.
    layer("relayered")
    rq = lq.replace("layered_hourly", "relayered_hourly")
    before_re = _one(conn, rq)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM relayered WHERE at >= {h} + interval '2 hours' AND at < {h} + interval '3 hours'")
    expect.text(f"{_refresh(conn, 'relayered_hourly')}|{_one(conn, rq)}", f"0A000|{before_re}",
                "a delete in a bucket whose history the same refresh retires is refused, and the rollup is unchanged")

    # Rows deleted from a group expire then retires (found on #1328): they cannot
    # be recomputed, and history would keep counting them, so the refresh refuses.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE thinned (at timestamptz, reading int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('thinned', stripe_row_limit => 2000)")
        cur.execute("INSERT INTO thinned SELECT now() - interval '45 days' + g * interval '2 s', g"
                    " FROM generate_series(1, 1500) g")
        cur.execute("INSERT INTO thinned SELECT now() - interval '2 days' + g * interval '2 s', g"
                    " FROM generate_series(1, 1500) g")
        cur.execute(f"SELECT pgcolumnar.create_rollup('thinned_hourly', 'thinned', 'at', '1 hour', '{ORIGIN}',"
                    " '{}', '{count,max}', '{*,reading}')")
        cur.execute("SELECT pgcolumnar.refresh_rollup('thinned_hourly')")
        cur.execute("DELETE FROM thinned WHERE at < now() - interval '14 days' AND reading % 5 = 0")
        cur.execute("SELECT pgcolumnar.set_options('thinned', ttl_column => 'at', ttl_interval => '14 days')")
    expect.num(_one(conn, "SELECT pgcolumnar.expire('thinned')"), 1,
               "expire retired the group the delete thinned")
    tq = "SELECT string_agg(bucket || ':' || count || ':' || max_reading, ',' ORDER BY bucket) FROM thinned_hourly"
    before_thin = _one(conn, tq)
    expect.text(f"{_refresh(conn, 'thinned_hourly')}|{_one(conn, tq)}", f"0A000|{before_thin}",
                "a delete in a group that expire then retires is refused, and the rollup is unchanged")

    # A day is 24 hours to date_bin in every zone. Europe/Berlin moves its clocks
    # at 01:00 UTC on 2026-03-29, inside the bucket [03-28 06:00, 03-29 06:00) UTC
    # (ORIGIN is at 06:00), so that bucket is 23 hours long in Berlin. It must be
    # recomputed to its end: the rows run to 05:40 UTC. (Rows ending at 23:40 UTC
    # first, a bucket the switch is not in, left this arm green under the mutation.)
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE springs (at timestamptz, reading int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('springs', stripe_row_limit => 4000)")
        cur.execute("INSERT INTO springs SELECT '2026-03-27 06:00:00+00'::timestamptz + g * interval '4 min', g"
                    " FROM generate_series(1, 715) g")
        cur.execute(f"SELECT pgcolumnar.create_rollup('springs_daily', 'springs', 'at', '1 day', '{ORIGIN}',"
                    " '{}', '{count,max}', '{*,reading}')")
        cur.execute("SELECT pgcolumnar.refresh_rollup('springs_daily')")
        cur.execute("DELETE FROM springs WHERE reading = 2")
        cur.execute("SET TimeZone = 'Europe/Berlin'")
        cur.execute("SELECT pgcolumnar.refresh_rollup('springs_daily')")
        cur.execute("SET TimeZone = 'UTC'")
    expect.text(_one(conn, "SELECT string_agg(bucket || ':' || count || ':' || max_reading, ',' ORDER BY bucket)"
                           " FROM springs_daily"),
                _one(conn, f"SELECT string_agg(b || ':' || n || ':' || m, ',' ORDER BY b) FROM (SELECT"
                           f" date_bin('1 day', at, '{ORIGIN}') b, count(*) n, max(reading) m FROM springs GROUP BY 1) x"),
                "a refresh in a zone with a DST change recomputes a whole day bucket")

    # Ordinary roles: row-level security on the rollup catalogs is what is tested.
    with conn.cursor() as cur:
        for r in ("rp_owner", "rp_other"):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {r}; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            cur.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {r}")
            cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")

    def as_role(role, sql):
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"SET ROLE {role}")
            try:
                row = c.execute(sql).fetchone()
                return str(row[0]) if row else ""
            except psycopg.Error as e:
                return e.sqlstate or "?"
    as_role("rp_owner", "CREATE TABLE meter (at timestamptz, kwh int) USING pgcolumnar")
    as_role("rp_owner", "INSERT INTO meter SELECT '2026-04-01'::timestamptz + g * interval '7 min', g % 50"
                        " FROM generate_series(1, 3000) g")
    expect.text(as_role("rp_owner", f"SELECT pgcolumnar.create_rollup('meter_daily', 'meter', 'at', '1 day',"
                                    f" '{ORIGIN}', '{{}}', '{{count,max}}', '{{*,kwh}}')"),
                "meter_daily", "a table's owner, not a superuser, creates a rollup of it")
    expect.text(as_role("rp_owner", "SELECT pgcolumnar.refresh_rollup('meter_daily')"), "3000", "and refreshes it")
    expect.text(as_role("rp_owner", "SELECT string_agg(bucket || ':' || count || ':' || max_kwh, ' ' ORDER BY bucket)"
                                    " FROM meter_daily"),
                _one(conn, f"SELECT string_agg(b || ':' || n || ':' || m, ' ' ORDER BY b) FROM"
                           f" (SELECT date_bin('1 day', at, '{ORIGIN}') b, count(*) n, max(kwh) m FROM meter GROUP BY 1) x"),
                "and reads what a recompute gives")
    # THE WRITE SIDE AND THE DEFINER HELPERS (#1341). The policies have USING only,
    # so they also check an INSERT's new row; an UPDATE or DELETE with no WHERE reads
    # nothing, so only the policy's USING keeps it off another owner's rows. Asked
    # before the read-side arms, so a write that got through is what fails.
    rg_sql = "SELECT count(*) FROM pgcolumnar.rollup_group WHERE rollup = 'meter_daily'::regclass"
    rg_own = _one(conn, rg_sql)
    expect.text("some" if rg_own > 0 else f"none ({rg_own})", "some",
                "premise: the owner's rollup has group rows another role could reach")
    expect.text(as_role("rp_other", "INSERT INTO pgcolumnar.rollup_group VALUES"
                                    " ('public.meter_daily'::regclass, 999999, 1, 0, NULL, NULL, false)"),
                "42501", "another role cannot add a group row to the owner's rollup (42501)")
    as_role("rp_other", "UPDATE pgcolumnar.rollup SET history_has_null = true")
    as_role("rp_other", "DELETE FROM pgcolumnar.rollup_group")
    has_null = _one(conn, "SELECT history_has_null FROM pgcolumnar.rollup"
                          " WHERE rollup = 'meter_daily'::regclass")
    expect.text(f"{has_null}|{_one(conn, rg_sql)}", f"False|{rg_own}",
                "and its unqualified UPDATE and DELETE reach none of the owner's catalog rows")
    # rollup_source_groups and rollup_source_retired run as their definer, so the
    # catalogs they read refuse no one; the caller's SELECT on the source is checked
    # by require_caller_select against the OUTER user.
    own_groups = as_role("rp_owner", "SELECT (count(*) > 0)::text FROM pgcolumnar.rollup_source_groups('meter')")
    own_retired = as_role("rp_owner", "SELECT count(*) >= 0 FROM pgcolumnar.rollup_source_retired('meter')")
    expect.text(f"{own_groups}|{own_retired}", "true|True",
                "the owner reads its source's groups through the definer helpers")
    expect.text(as_role("rp_other", "SELECT count(*) FROM pgcolumnar.rollup_source_groups('public.meter')"), "42501",
                "another role cannot read the source's groups through rollup_source_groups (42501)")
    expect.text(as_role("rp_other", "SELECT count(*) FROM pgcolumnar.rollup_source_retired('public.meter')"), "42501",
                "nor its retired groups through rollup_source_retired (42501)")
    expect.text(f"{as_role('rp_other', 'SELECT count(*) FROM pgcolumnar.rollup')} "
                f"{as_role('rp_other', 'SELECT count(*) FROM pgcolumnar.rollup_group')}", "0 0",
                "another role sees none of its rollup catalog rows")
    expect.text(as_role("rp_other", "SELECT pgcolumnar.refresh_rollup('public.meter_daily')"), "42704",
                "and cannot refresh it")
    # create_rollup reads the source's storage id through get_storage_id, which
    # refuses a caller that cannot SELECT the table: refused at creation.
    expect.text(as_role("rp_other", f"SELECT pgcolumnar.create_rollup('other_daily', 'public.meter', 'at', '1 day',"
                                    f" '{ORIGIN}', '{{}}', '{{count}}', '{{*}}')"),
                "42501", "a role that cannot read the source cannot create a rollup of it")

    # access_method: a heap rollup folds and matches the same way.
    with conn.cursor() as cur:
        cur.execute(f"SELECT pgcolumnar.create_rollup('heap_15m', 'feed', 'at', '15 minutes', '{ORIGIN}',"
                    f" '{{sensor}}', '{AGG_FUNCS}', '{AGG_COLS}', 'heap')")
    am = _one(conn, "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam"
                    " WHERE c.oid = 'heap_15m__partials'::regclass")
    folded = _refresh(conn, "heap_15m")
    expect.text(f"{am}|{folded.isdigit() and int(folded) > 0}|{_matches(conn, 'heap_15m')}",
                f"heap|True|{_oracle(conn)}",
                "a rollup kept in a heap table refreshes and equals a full recompute")

    # REPEATABLE READ is refused: its snapshot would predate the refresh's lock.
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        try:
            c.execute("SELECT pgcolumnar.refresh_rollup('z_utc')")
            rr = "ok"
        except psycopg.Error as e:
            rr = e.sqlstate
        c.execute("ROLLBACK")
    expect.text(rr, "25000", "a refresh at REPEATABLE READ is refused")

    # Refused at creation.
    def refused(name, width, funcs, cols):
        return _state(conn, f"SELECT pgcolumnar.create_rollup('{name}', 'feed', 'at', '{width}', '{ORIGIN}',"
                            f" '{{}}', '{funcs}', '{cols}')")
    expect.text(refused("q_m", "2 months", "{count}", "{*}"), "22023", "a width with months is refused")
    expect.text(refused("q_p", "1 hour", "{stddev}", "{temp}"), "22023", "an aggregate that does not combine is refused")
    # More columns than functions, so only the length check can catch it.
    expect.text(refused("q_l", "1 hour", "{count}", "{*,temp}"), "22023",
                "aggregate functions and columns of different lengths are refused")
    # Under min, which has no "cannot be folded" check to catch it first.
    expect.text(refused("q_c", "1 hour", "{min}", "{humidity}"), "22023",
                "an aggregate column the source does not have is refused")
    expect.text(refused("q_s", "1 hour", "{max}", "{*}"), "22023", "* with anything but count is refused")
    # The rest of create_rollup's refusals, and rebase_rollup's (#1341). A timestamp
    # (no zone) source column would be bucketed in whatever zone the refresh runs in.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE feed_lt (at timestamp, temp float8) USING pgcolumnar")
    expect.text(_state(conn, f"SELECT pgcolumnar.create_rollup('q_t', 'feed_lt', 'at', '1 hour', '{ORIGIN}',"
                             f" '{{}}', '{{count}}', '{{*}}')"),
                "22023", "a ts_column that is not timestamptz is refused")
    expect.text(_state(conn, f"SELECT pgcolumnar.create_rollup('q_g', 'feed', 'at', '1 hour', '{ORIGIN}',"
                             f" '{{humidity}}', '{{count}}', '{{*}}')"),
                "22023", "a group_by column the source does not have is refused")
    expect.text(refused("q_f", "1 hour", "{sum}", "{at}"), "22023",
                "a sum over a column that cannot be folded is refused")
    expect.text(_state(conn, "SELECT pgcolumnar.rebase_rollup('feed')"), "42704",
                "rebase_rollup of a table that is not a rollup is refused (42704)")
    expect.num(_one(conn, "SELECT count(*) FROM pg_class WHERE relname IN ('q_m', 'q_p', 'q_l', 'q_c', 'q_s', 'q_t', 'q_g', 'q_f')"), 0,
               "and no refused rollup left a table behind")


def test_a_rollup_and_its_source_are_dropped_together(pgc_conn, pgc_cluster, expect):
    """The catalog held the source and the view as bare OIDs (#1376). DROP TABLE of the
    source succeeded and left the rollup unrefreshable for good (42P01 on the dead OID,
    or a reused OID folded silently); DROP VIEW left the partials table and the catalog
    rows behind. Now the view depends on the source as a view over it would, so the drop
    is refused and CASCADE takes the rollup; and dropping the view takes the rest of it."""
    conn = pgc_conn

    def ddl(sql):
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"

    def left(oid, name):
        return _one(conn, f"SELECT (SELECT count(*) FROM pg_class WHERE relname IN ('{name}', '{name}__partials'))"
                          f" || '|' || (SELECT count(*) FROM pgcolumnar.rollup WHERE rollup::oid = {oid})"
                          f" || '|' || (SELECT count(*) FROM pgcolumnar.rollup_group WHERE rollup::oid = {oid})")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE lsrc (ts timestamptz, v int) USING pgcolumnar")
        cur.execute(f"INSERT INTO lsrc SELECT '{ORIGIN}'::timestamptz + g * interval '7 minutes', g"
                    " FROM generate_series(1, 500) g")
        for lr in ("lr_a", "lr_b"):
            cur.execute(f"SELECT pgcolumnar.create_rollup('{lr}', 'lsrc', 'ts', '1 hour', '{ORIGIN}',"
                        " '{}', '{count}', '{*}')")
            cur.execute(f"SELECT pgcolumnar.refresh_rollup('{lr}')")
    a_oid = _one(conn, "SELECT 'lr_a'::regclass::oid")
    b_oid = _one(conn, "SELECT 'lr_b'::regclass::oid")

    sums = _one(conn, "SELECT (SELECT sum(count) FROM lr_a) || '|' || (SELECT sum(count) FROM lr_b)")
    a_parts = "|".join(left(a_oid, "lr_a").split("|")[:2])
    expect.text(f"{sums}|{a_parts}", "500|500|2|1",
                "both rollups over the source refreshed, so each has a view, partials and catalog rows")
    expect.text(f"{ddl('DROP VIEW lr_b')}|{left(b_oid, 'lr_b')}", "ok|0|0|0",
                "DROP VIEW of a rollup takes its partials and its catalog rows with it (#1376)")
    expect.text(f"{ddl('DROP TABLE lsrc')}|{_one(conn, 'SELECT count(*) FROM pg_class WHERE relname = ' + chr(39) + 'lsrc' + chr(39))}",
                "2BP01|1", "DROP TABLE of a rollup's source is refused while the rollup exists (2BP01, #1376)")
    expect.text(f"{ddl('DROP TABLE lsrc CASCADE')}|{left(a_oid, 'lr_a')}", "ok|0|0|0",
                "and DROP TABLE ... CASCADE takes the rollup's view, partials and catalog rows with it (#1376)")

    # The trigger that records those dependencies writes to pg_depend for whoever writes
    # a rollup row, and the catalog's row-level security only asks that they own the
    # view. So it asks what a view over the source would: SELECT on the source.
    schema = _one(conn, "SELECT current_schema()")
    with conn.cursor() as cur:
        cur.execute("DO $$ BEGIN CREATE ROLE lr_stranger_py LOGIN; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        cur.execute(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO lr_stranger_py')
        cur.execute("GRANT USAGE ON SCHEMA pgcolumnar TO lr_stranger_py")
        cur.execute("CREATE TABLE lr_victim (ts timestamptz, v int) USING pgcolumnar")
        cur.execute("REVOKE ALL ON lr_victim FROM PUBLIC")
    db = _one(conn, "SELECT current_database()")
    try:
        with psycopg.connect(f"host=127.0.0.1 port={pgc_cluster.port} user=lr_stranger_py dbname={db}",
                             autocommit=False) as c:
            c.execute(f'SET search_path TO "{schema}", public')
            c.execute("CREATE TABLE lr_mine (bucket timestamptz) USING pgcolumnar")
            c.execute("CREATE VIEW lr_my_view AS SELECT bucket FROM lr_mine")
            c.execute("INSERT INTO pgcolumnar.rollup (rollup, partials, source, source_storage, source_oid, ts_column,"
                      " width, origin, group_by, agg_functions, agg_columns) VALUES ('lr_my_view', 'lr_mine',"
                      f" 'lr_victim', 0, 0, 'ts', '1 hour', '{ORIGIN}', '{{}}', '{{count}}', '{{*}}')")
            c.commit()
        tie = "ok"
    except psycopg.Error as e:
        tie = e.sqlstate or "?"
    expect.text(f"{tie}|{ddl('DROP TABLE lr_victim')}", "42501|ok",
                "a role cannot tie a rollup row to a table it cannot read (42501, #1376)")

    # The partials go with the view by an AUTO dependency, and a drop deletes AUTO
    # dependents with no ownership check. So the trigger requires the writer to own
    # the partials too, and the rollup to be a view and the partials a table.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE lr_others (bucket timestamptz) USING pgcolumnar")
        cur.execute("GRANT SELECT ON lr_others TO lr_stranger_py")
        cur.execute("CREATE TABLE lr_src2 (ts timestamptz, v int) USING pgcolumnar")
        cur.execute("GRANT SELECT ON lr_src2 TO lr_stranger_py")

    def row(view, partials):
        return ("INSERT INTO pgcolumnar.rollup (rollup, partials, source, source_storage, source_oid, ts_column,"
                f" width, origin, group_by, agg_functions, agg_columns) VALUES ('{view}', '{partials}', 'lr_src2', 0,"
                f" 0, 'ts', '1 hour', '{ORIGIN}', '{{}}', '{{count}}', '{{*}}')")

    def as_stranger(*stmts):
        try:
            with psycopg.connect(f"host=127.0.0.1 port={pgc_cluster.port} user=lr_stranger_py dbname={db}",
                                 autocommit=False) as c:
                c.execute(f'SET search_path TO "{schema}", public')
                for st in stmts:
                    c.execute(st)
                c.commit()
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"

    named = as_stranger("CREATE TABLE lr_p2 (bucket timestamptz) USING pgcolumnar",
                        "CREATE VIEW lr_v2 AS SELECT bucket FROM lr_p2", row("lr_v2", "lr_others"))
    dropped = as_stranger("DROP VIEW IF EXISTS lr_v2")
    survives = _one(conn, "SELECT count(*) FROM pg_class WHERE relname = 'lr_others'")
    expect.text(f"{named}|{dropped}|{survives}", "42501|ok|1",
                "a role cannot name another role's table as its rollup's partials (42501), and that table survives its view's drop (#1376)")
    expect.text(as_stranger("CREATE TABLE lr_notview (bucket timestamptz) USING pgcolumnar",
                            "CREATE TABLE lr_p4 (bucket timestamptz) USING pgcolumnar", row("lr_notview", "lr_p4")),
                "42809", "a rollup row whose rollup is not a view is refused (42809, #1376)")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE lr_p5 (bucket timestamptz) USING pgcolumnar")
        cur.execute("CREATE VIEW lr_v5 AS SELECT bucket FROM lr_p5")
        cur.execute("CREATE TABLE lr_p6 (bucket timestamptz) USING pgcolumnar")
        cur.execute(row("lr_v5", "lr_p5"))
    expect.text(ddl("UPDATE pgcolumnar.rollup SET partials = 'lr_p6' WHERE rollup = 'lr_v5'::regclass"), "0A000",
                "a rollup row's view, partials and source cannot be changed once written (0A000, #1376)")


def test_rollup_dump_and_restore(pgc_cluster, tmp_path, expect):
    """A DUMP AND RESTORE (#1377). The definition is dumped, so the rollup exists
    after a restore; the row groups it folded are not, as they name storage a restore
    replaces. Neither the database OID nor the storage id tells a restored source from
    the recorded one: storageid_seq restarts with the extension, so the first columnar
    table of a new database gets the same storage id, and a refresh would fold every
    restored row into the restored partials a second time. The OID does: the dump
    writes the source as a name and its recorded OID as a number.

    Its own databases and role. The partials are heap and the source is its database's
    only columnar table, so the restored source gets the recorded storage id: the case
    a storage id alone does not catch."""
    import subprocess

    src_db, dst_db = "pgc_rq_src", "pgc_rq_dst"
    dump = tmp_path / "rollup-1377.sql"
    bindir = pgc_cluster.bindir
    conn_args = ["-h", "127.0.0.1", "-p", str(pgc_cluster.port), "-U", "postgres"]

    def admin(db, sql):
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            row = c.execute(sql).fetchone() if sql.lstrip().upper().startswith("SELECT") else c.execute(sql)
            return str(row[0]) if isinstance(row, tuple) else None

    def owner(db, sql):
        """(value, sqlstate, hint) of SQL run as the rollup's owner, not a superuser."""
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("SET ROLE rq_owner")
            try:
                row = c.execute(sql).fetchone()
                return (str(row[0]) if row else "", "ok", None)
            except psycopg.Error as e:
                return (None, e.sqlstate or "?", e.diag.message_hint)

    def restore(db):
        p = subprocess.run([f"{bindir}/psql", *conn_args, "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                            "-f", str(dump)], capture_output=True, text=True)
        return "0" if p.returncode == 0 else \
            f"{p.returncode} " + next((l for l in p.stderr.splitlines() if "ERROR" in l), p.stderr[:200])

    recompute = ("SELECT count(*) || '|' || md5(string_agg(b || ':' || n || ':' || m, ' ' ORDER BY b)) FROM"
                 " (SELECT date_bin('30 minutes', at, '2026-01-01') b, count(*) n, max(level) m"
                 " FROM rq.tank GROUP BY 1) x")
    rollup = ("SELECT count(*) || '|' || md5(string_agg(bucket || ':' || count || ':' || max_level, ' '"
              " ORDER BY bucket)) FROM rq.tank_30m")

    admin("postgres", "DO $$ BEGIN CREATE ROLE rq_owner; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    for db in (src_db, dst_db):
        admin("postgres", f"DROP DATABASE IF EXISTS {db}")
    try:
        admin("postgres", f"CREATE DATABASE {src_db}")
        admin(src_db, "CREATE EXTENSION pgcolumnar")
        admin(src_db, "GRANT USAGE ON SCHEMA pgcolumnar TO rq_owner")
        admin(src_db, "CREATE SCHEMA rq AUTHORIZATION rq_owner")
        owner(src_db, "CREATE TABLE rq.tank (at timestamptz, level int) USING pgcolumnar")
        owner(src_db, "INSERT INTO rq.tank SELECT '2026-07-01'::timestamptz + g * interval '50 s', (g * 7) % 1000"
                      " FROM generate_series(1, 4000) g")
        owner(src_db, "SELECT pgcolumnar.create_rollup('tank_30m', 'rq.tank', 'at', '30 minutes', '2026-01-01',"
                      " '{}', '{count,max}', '{*,level}', 'heap')")
        expect.text(owner(src_db, "SELECT pgcolumnar.refresh_rollup('rq.tank_30m')")[0], "4000",
                    "the rollup to be dumped folds its source")
        sid = admin(src_db, "SELECT pgcolumnar.get_storage_id('rq.tank')")
        p = subprocess.run([f"{bindir}/pg_dump", *conn_args, "-d", src_db, "-f", str(dump)],
                           capture_output=True, text=True)
        expect.text(str(p.returncode), "0", "the database holding a rollup dumps")

        # Into another database.
        admin("postgres", f"CREATE DATABASE {dst_db}")
        expect.text(restore(dst_db), "0", "the dump restores into another database")
        expect.text(admin(dst_db, "SELECT count(*) || '|' || pgcolumnar.get_storage_id('rq.tank') FROM rq.tank"),
                    f"4000|{sid}", "the restored source holds the dumped rows under the storage id recorded for the old one")
        expect.text(admin(dst_db, "SELECT coalesce((SELECT source::text || ' ' || ts_column || ' ' || width || ' '"
                                  " || agg_functions::text || ' ' || agg_columns::text FROM pgcolumnar.rollup"
                                  " WHERE rollup = 'rq.tank_30m'::regclass), 'none')"),
                    "rq.tank at 00:30:00 {count,max} {*,level}", "a restore keeps the rollup's definition")
        before = admin(dst_db, rollup)
        expect.text(before, admin(dst_db, recompute),
                    "the restored rollup reads as a recompute of the restored source")
        _, st, hint = owner(dst_db, "SELECT pgcolumnar.refresh_rollup('rq.tank_30m')")
        expect.text(f"{st}|{admin(dst_db, rollup)}", f"55000|{before}",
                    "after a restore into another database its owner's refresh is refused (55000), the rollup untouched")
        expect.text("rebase_rollup" if hint and "rebase_rollup" in hint else f"hint: {hint}", "rebase_rollup",
                    "and the refusal names rebase_rollup")
        owner(dst_db, "SELECT pgcolumnar.rebase_rollup('rq.tank_30m')")
        expect.text(owner(dst_db, "SELECT pgcolumnar.refresh_rollup('rq.tank_30m')")[0], "0",
                    "after rebase_rollup a refresh folds nothing")
        owner(dst_db, "INSERT INTO rq.tank SELECT '2026-07-01'::timestamptz + g * interval '50 s', (g * 7) % 1000"
                      " FROM generate_series(4001, 4600) g")
        expect.text(owner(dst_db, "SELECT pgcolumnar.refresh_rollup('rq.tank_30m')")[0], "600",
                    "and then folds exactly the rows appended after the restore")
        expect.text(admin(dst_db, rollup), admin(dst_db, recompute), "and the restored rollup equals a full recompute")
        # The restore wrote the catalog row back, and with it the view's dependency on
        # the restored source (#1376).
        expect.text(f"{owner(dst_db, 'DROP TABLE rq.tank')[1]}|{admin(dst_db, 'SELECT count(*) FROM rq.tank')}",
                    "2BP01|4600", "after a restore the rollup's source cannot be dropped while the rollup exists (2BP01)")

        # Into its own database after DROP EXTENSION: the same cluster and database
        # OID, and the sequence restarted, so again the recorded storage id.
        admin(src_db, "DROP EXTENSION pgcolumnar CASCADE")
        # The schema is no member of the extension, so it outlives it; the dump creates it.
        admin(src_db, "DROP SCHEMA pgcolumnar CASCADE")
        admin(src_db, "DROP SCHEMA rq CASCADE")
        expect.text(f"{restore(src_db)}|"
                    + str(admin(src_db, "SELECT count(*) || '|' || pgcolumnar.get_storage_id('rq.tank') FROM rq.tank")),
                    f"0|4000|{sid}",
                    "the dump restores into its own database after DROP EXTENSION, again under the recorded storage id")
        before = admin(src_db, rollup)
        _, st, _ = owner(src_db, "SELECT pgcolumnar.refresh_rollup('rq.tank_30m')")
        expect.text(f"{st}|{admin(src_db, rollup)}", f"55000|{before}",
                    "after a restore into the same database its refresh is refused (55000), the rollup untouched")
    finally:
        for db in (src_db, dst_db):
            admin("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_row_tid_refuses_a_row_number_below_one(pgc_conn, expect):
    """Row numbers start at 1 (#1341). LAST IN THE FILE, AND A TEST OF ITS OWN: without
    the floor, row_tid(0) crashed the backend (measured on PG17), which would take the
    checks after it in the same test down with it."""
    def state(n):
        try:
            _one(pgc_conn, f"SELECT pgcolumnar.row_tid({n})")
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"

    expect.text(f"{state(0)}|{state(-1)}|{state(1)}", "22003|22003|ok",
                "row_tid refuses a row number below 1 (22003), and row 1 is accepted")

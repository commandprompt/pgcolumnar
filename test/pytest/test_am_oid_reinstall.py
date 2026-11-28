"""A columnar table made after DROP EXTENSION + CREATE EXTENSION is recognised (#2176).
The pytest twin of `test/am_oid_reinstall.sh`, every check by the shell's name.

The library is preloaded and outlives the extension, so the reinstall gives the
pgcolumnar access method a new oid in a backend that already holds the old one.
PgColumnarIsColumnarRelation cached that oid in a static and never dropped it, and the
index-only-scan planner hook kept a second copy. In such a backend every columnar table
made after the reinstall looked like a heap: no custom scan, get_storage_id and
vacuum_sorted refused with 42809, the RENAME COLUMN hook left the declared sort key on
the old name, the DROP COLUMN hook left the ordering mark naming a dropped column, and
pgcolumnar.enable_index_only_scan = off no longer kept index-only scans off it.

Two scenarios, with the same checks. "self": one connection warms the cache, reinstalls
the extension, then uses a new table. "other": one connection warms the cache, a second
connection reinstalls the extension, then the first uses a new table. Every statement of
a scenario's session runs on one psycopg connection, so in one backend. The reinstall in
"other" and the vacuum_sorted that sets the ordering mark run on connections of their
own, in order. Every connection sets lock_timeout and statement_timeout, so no step can
wait unbounded. A refusal is read as a failed-query value (pgc_vacuity.query_error)
carrying its SQLSTATE and message, which every expect helper refuses by name.

ON A PRIVATE DATABASE, built once by a module fixture; each test asserts one reading.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_am_oid_reinstall"
TIMEOUTS = ("SET lock_timeout = '30s'", "SET statement_timeout = '60s'")
IOS_ON = ("SET pgcolumnar.enable_index_only_scan = off", "SET pgcolumnar.enable_custom_scan = off",
          "SET enable_seqscan = off", "SET enable_bitmapscan = off")
IOS_OFF = ("RESET pgcolumnar.enable_index_only_scan", "RESET pgcolumnar.enable_custom_scan",
           "RESET enable_seqscan", "RESET enable_bitmapscan")


def _connect(dsn):
    c = psycopg.connect(dsn, autocommit=True)
    for sql in TIMEOUTS:
        c.execute(sql)
    return c


def _value(c, sql):
    """sql's single value as text, NULL as 'NULL', or a failed-query value carrying
    the SQLSTATE and message."""
    try:
        row = c.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(f"{exc.sqlstate} {exc.diag.message_primary}")
    return "NULL" if row is None or row[0] is None else str(row[0])


def _do(c, sql):
    """'ok' once sql ran, or a failed-query value carrying the SQLSTATE and message."""
    try:
        c.execute(sql)
    except psycopg.Error as exc:
        return query_error(f"{exc.sqlstate} {exc.diag.message_primary}")
    return "ok"


def _scan(c, sql):
    """The first plan line that names a relation, without its indentation and arrow."""
    for (line,) in c.execute("EXPLAIN (COSTS OFF) " + sql).fetchall():
        if " on " in line:
            return line.strip().lstrip("->").strip()
    return "no scan"


def _other(dsn, *sqls):
    with _connect(dsn) as o:
        for sql in sqls:
            o.execute(sql)


def _scenario(dsn, t, reinstall_here):
    r = {}
    with _connect(dsn) as s:
        s.execute(f"CREATE TABLE {t}_warm (a int) USING pgcolumnar")
        s.execute(f"CREATE INDEX {t}_warm_a ON {t}_warm (a)")
        r["warm"] = _value(s, f"SELECT (pgcolumnar.get_storage_id('{t}_warm') IS NOT NULL)::text")
        for sql in IOS_ON:
            s.execute(sql)
        r["iosw"] = _scan(s, f"SELECT a FROM {t}_warm WHERE a = 1")
        for sql in IOS_OFF:
            s.execute(sql)
        r["old"] = _value(s, "SELECT oid FROM pg_am WHERE amname = 'pgcolumnar'")
        reinstall = ("DROP EXTENSION pgcolumnar CASCADE", "CREATE EXTENSION pgcolumnar")
        if reinstall_here:
            for sql in reinstall:
                s.execute(sql)
        else:
            _other(dsn, *reinstall)
        r["new"] = _value(s, "SELECT oid FROM pg_am WHERE amname = 'pgcolumnar'")
        for sql in (f"CREATE TABLE {t} (a int, b int, c int) USING pgcolumnar",
                    f"INSERT INTO {t} SELECT g, 101 - g, g % 7 FROM generate_series(1, 100) g",
                    f"UPDATE {t} SET b = b + 1000 WHERE a <= 10",
                    f"DELETE FROM {t} WHERE a > 90",
                    f"CREATE INDEX {t}_a ON {t} (a)"):
            s.execute(sql)
        r["rows"] = _value(s, f"SELECT count(*) || '|' || sum(a) || '|' || sum(b) FROM {t}")
        r["scan"] = _scan(s, f"SELECT b FROM {t} WHERE b > 5")
        r["sid"] = _value(s, f"SELECT (pgcolumnar.get_storage_id('{t}') IS NOT NULL)::text")
        for sql in IOS_ON:
            s.execute(sql)
        r["ios"] = _scan(s, f"SELECT a FROM {t} WHERE a = 5")
        for sql in IOS_OFF:
            s.execute(sql)
        r["vs"] = _do(s, f"SELECT pgcolumnar.vacuum_sorted('{t}', 'a')")
        s.execute(f"SELECT pgcolumnar.set_options('{t}', sort_by => ARRAY['c'])")
        s.execute(f"ALTER TABLE {t} RENAME COLUMN c TO c2")
        r["ren"] = _value(s, f"SELECT sort_by::text FROM pgcolumnar.options WHERE regclass = '{t}'::regclass")
        _other(dsn, f"SELECT pgcolumnar.vacuum_sorted('{t}', 'b')")
        mark = (f"SELECT coalesce(sorted_by::text, 'NULL') || '/' || coalesce(sorted_kind, 'NULL') "
                f"FROM pgcolumnar.storage WHERE relation_oid = '{t}'::regclass")
        r["mark"] = _value(s, mark)
        s.execute(f"ALTER TABLE {t} DROP COLUMN b")
        r["drop"] = _value(s, mark)
    o, n = r["old"], r["new"]
    r["changed"] = "yes" if o.isdigit() and n.isdigit() and o != n else f"no ({o} -> {n})"
    return r


def _crashes(pgc_cluster):
    return (pgc_cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")


@pytest.fixture(scope="module")
def ar(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _other(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        before = _crashes(pgc_cluster)
        r = {"self": _scenario(dsn, "self", True), "other": _scenario(dsn, "other", False)}
        r["crashes"] = _crashes(pgc_cluster) - before
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_self_premise_warmed(ar, expect):
    expect.text(ar["self"]["warm"], "true",
                "premise: self -- the session looked the access method up before the reinstall")


def test_self_premise_ios_hook_warmed(ar, expect):
    expect.text(ar["self"]["iosw"], "Index Scan using self_warm_a on self_warm",
                "premise: self -- the session planned through the index-only-scan hook before the reinstall")


def test_self_premise_new_am_oid(ar, expect):
    expect.text(ar["self"]["changed"], "yes",
                "premise: self -- the reinstall gave the access method a new oid")


def test_self_rows(ar, expect):
    expect.text(ar["self"]["rows"], "90|4095|14995",
                "self: the new table's rows after INSERT, UPDATE and DELETE")


def test_self_custom_scan(ar, expect):
    expect.text(ar["self"]["scan"], "Custom Scan (PgColumnarScan) on self",
                "self: the new table is read by the columnar custom scan (#2176)")


def test_self_get_storage_id(ar, expect):
    expect.text(ar["self"]["sid"], "true", "self: get_storage_id recognises the new table (#2176)")


def test_self_index_only_scan_off(ar, expect):
    expect.text(ar["self"]["ios"], "Index Scan using self_a on self",
                "self: enable_index_only_scan = off keeps index-only scans off the new table (#2176)")


def test_self_vacuum_sorted(ar, expect):
    expect.text(ar["self"]["vs"], "ok", "self: vacuum_sorted accepts the new table (#2176)")


def test_self_rename_sort_key(ar, expect):
    expect.text(ar["self"]["ren"], "{c2}", "self: RENAME COLUMN renames the declared sort key (#2176)")


def test_self_premise_mark_set(ar, expect):
    expect.text(ar["self"]["mark"], "{b}/lexicographic",
                "premise: self -- another session's vacuum_sorted set the ordering mark")


def test_self_drop_column_clears_mark(ar, expect):
    expect.text(ar["self"]["drop"], "NULL/NULL", "self: DROP COLUMN clears the ordering mark (#2176)")


def test_other_premise_warmed(ar, expect):
    expect.text(ar["other"]["warm"], "true",
                "premise: other -- the session looked the access method up before the reinstall")


def test_other_premise_ios_hook_warmed(ar, expect):
    expect.text(ar["other"]["iosw"], "Index Scan using other_warm_a on other_warm",
                "premise: other -- the session planned through the index-only-scan hook before the reinstall")


def test_other_premise_new_am_oid(ar, expect):
    expect.text(ar["other"]["changed"], "yes",
                "premise: other -- the reinstall gave the access method a new oid")


def test_other_rows(ar, expect):
    expect.text(ar["other"]["rows"], "90|4095|14995",
                "other: the new table's rows after INSERT, UPDATE and DELETE")


def test_other_custom_scan(ar, expect):
    expect.text(ar["other"]["scan"], "Custom Scan (PgColumnarScan) on other",
                "other: the new table is read by the columnar custom scan (#2176)")


def test_other_get_storage_id(ar, expect):
    expect.text(ar["other"]["sid"], "true", "other: get_storage_id recognises the new table (#2176)")


def test_other_index_only_scan_off(ar, expect):
    expect.text(ar["other"]["ios"], "Index Scan using other_a on other",
                "other: enable_index_only_scan = off keeps index-only scans off the new table (#2176)")


def test_other_vacuum_sorted(ar, expect):
    expect.text(ar["other"]["vs"], "ok", "other: vacuum_sorted accepts the new table (#2176)")


def test_other_rename_sort_key(ar, expect):
    expect.text(ar["other"]["ren"], "{c2}", "other: RENAME COLUMN renames the declared sort key (#2176)")


def test_other_premise_mark_set(ar, expect):
    expect.text(ar["other"]["mark"], "{b}/lexicographic",
                "premise: other -- another session's vacuum_sorted set the ordering mark")


def test_other_drop_column_clears_mark(ar, expect):
    expect.text(ar["other"]["drop"], "NULL/NULL", "other: DROP COLUMN clears the ordering mark (#2176)")


def test_no_backend_crashed_in_the_scenarios(ar, expect):
    expect.num(ar["crashes"], 0, "no backend terminated by signal during the scenarios")

"""The declarative sort_by clustering key (#288). The pytest twin of
`test/native_sort_by.sh`, every check by the shell's name (#1341).

set_options(t, sort_by => ARRAY[...]) declares a persisted physical sort key, and
vacuum_sorted(t) with no columns applies it. Asserted: heap parity before and after
the sort, that sorting a text key enables chunk-group skipping, that the rows really
lie in (host, ts) order afterwards, that explicit columns lay them out the same way,
the key's round-trip and reset, clean refusals for no key, a bad column, a dropped
declared column and (PG18+) a virtual generated column, and that the backend survives
every refusal.

THE PHYSICAL ORDER IS READ, not inferred: the rows are read in scan order and every
adjacent pair is compared on (host, ts). Group skipping on host was the only evidence
before, and a sort that kept hosts together in the wrong order skipped just as well.

A REFUSAL READS ITS OWN GUARD: the SQLSTATE and a phrase of the guard's message, as
"<SQLSTATE> <phrase>". A backend that crashed in place of the refusal used to count as
refused. Survival reads the server log's count of backends killed by a signal, and
each reading after a refusal first waits for the server.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_sort_by"
GEN = ("SELECT g AS id, 'h' || lpad((g % 50)::text, 3, '0') AS host, "
       "('2024-01-01'::timestamptz + ((g % 1440) * interval '1 minute')) AS ts, "
       "'p' || (g % 7) AS payload FROM generate_series(1, 40960) g")
EQ = "host = 'h007'"
INVERSIONS = ("SELECT count(*) FROM (SELECT host, ts, lag(host) OVER () AS ph, lag(ts) OVER () AS pt "
              "FROM (SELECT host, ts FROM n OFFSET 0) s) x "
              "WHERE ph IS NOT NULL AND (ph, pt) > (host, ts)")
LAYOUT = "SELECT md5(string_agg(host || '|' || ts::text, ',')) FROM (SELECT host, ts FROM n OFFSET 0) s"
SERIAL = "SET max_parallel_workers_per_gather = 0"


def _message(exc):
    diag = getattr(exc, "diag", None)
    primary = diag.message_primary if diag is not None else None
    return primary or (str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(SERIAL)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else str(row[0])


def _await_up(dsn):
    """Wait, at most 60 s, until the server answers, as the shell's await_up."""
    for _ in range(120):
        if _value(dsn, "SELECT 1") == "1":
            return
        time.sleep(0.5)


def _set_hash(dsn, query):
    """-> the shell's pgc_set_hash: md5 of the sorted row texts, EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _plan(dsn, query, analyze):
    opts = "ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF" if analyze else "COSTS OFF"
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(SERIAL)
            rows = conn.execute(f"EXPLAIN ({opts}) {query}").fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join(r[0] for r in rows)


def _skipped(dsn, query):
    """-> chunk groups removed by the zone maps as text, or a value no number equals."""
    plan = _plan(dsn, query, analyze=True)
    m = re.search(r"Columnar Chunk Groups Removed by Filter: (\d+)", plan)
    return m.group(1) if m else ""


def _refusal(dsn, sql, phrase):
    """-> "<SQLSTATE> <phrase>" when sql raised with phrase in its message,
    "<SQLSTATE> other: <message>" for a different refusal, "no error" when it ran,
    or "no (<message>)" when the connection was lost."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return f"no ({_message(exc)})"
        if phrase in str(exc):
            return f"{exc.sqlstate} {phrase}"
        return f"{exc.sqlstate} other: {_message(exc)}"
    return "no error"


def _ran_ok(dsn, sql):
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return f"no ({_message(exc)})"
    return "ok"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    _await_up(dsn)
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


def _major(dsn):
    v = _value(dsn, "SELECT current_setting('server_version_num')::int / 10000")
    return int(v) if v.isdigit() else v


@pytest.fixture(scope="module")
def nsb(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        r = {}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE h (id int, host text, ts timestamptz, payload text)")
        _run(dsn, "CREATE TABLE n (id int, host text, ts timestamptz, payload text) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024)")
        _run(dsn, f"INSERT INTO h {GEN}")
        _run(dsn, f"INSERT INTO n {GEN}")

        r["rows"] = _value(dsn, "SELECT count(*) FROM n")
        plan = _plan(dsn, f"SELECT id FROM n WHERE {EQ}", analyze=False)
        r["scan"] = plan if plan.startswith("QUERY_ERROR") else (
            "yes" if "Columnar Projected Columns" in plan else "no")
        r["eq pre"] = (_set_hash(dsn, f"SELECT id, host, ts, payload FROM n WHERE {EQ}"),
                       _set_hash(dsn, f"SELECT id, host, ts, payload FROM h WHERE {EQ}"))
        inv = _value(dsn, INVERSIONS)
        r["unsorted"] = "yes" if inv.isdigit() and int(inv) > 0 else f"no [{inv}]"
        before = _skipped(dsn, f"SELECT id FROM n WHERE {EQ}")

        _run(dsn, "SELECT pgcolumnar.set_options('n', sort_by => ARRAY['host','ts'])")
        r["roundtrip"] = _value(dsn, "SELECT sort_by::text FROM pgcolumnar.options WHERE regclass = 'n'::regclass")
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('n')")
        r["rows after"] = _value(dsn, "SELECT count(*) FROM n")
        r["full"] = (_set_hash(dsn, "SELECT id, host, ts, payload FROM n ORDER BY id"),
                     _set_hash(dsn, "SELECT id, host, ts, payload FROM h ORDER BY id"))
        r["eq post"] = (_set_hash(dsn, f"SELECT id, host, ts, payload FROM n WHERE {EQ}"),
                        _set_hash(dsn, f"SELECT id, host, ts, payload FROM h WHERE {EQ}"))
        after = _skipped(dsn, f"SELECT id FROM n WHERE {EQ}")
        if not (before.isdigit() and after.isdigit()):
            r["skipping"] = f"no reading (before [{before}], after [{after}])"
        else:
            r["skipping"] = "yes" if int(after) > int(before) and int(after) > 0 else "no"
        r["inversions"] = _value(dsn, INVERSIONS)
        declared = _value(dsn, LAYOUT)

        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('n', 'host', 'ts')")
        r["explicit"] = (_set_hash(dsn, "SELECT id, host, ts, payload FROM n ORDER BY id"),
                         _set_hash(dsn, "SELECT id, host, ts, payload FROM h ORDER BY id"))
        r["explicit layout"] = (_value(dsn, LAYOUT), declared)

        _run(dsn, "SELECT pgcolumnar.reset_options('n', sort_by => true)")
        r["cleared"] = _value(dsn, "SELECT (sort_by IS NULL)::text FROM pgcolumnar.options "
                                   "WHERE regclass = 'n'::regclass")
        before_crash = _crashes(pgc_cluster)
        r["no key"] = _refusal(dsn, "SELECT pgcolumnar.vacuum_sorted('n')", "has no declared sort_by key")
        r["bad column"] = _refusal(dsn, "SELECT pgcolumnar.set_options('n', sort_by => ARRAY['nope'])",
                                   "cannot be used in sort_by")
        _await_up(dsn)
        _run(dsn, "SELECT pgcolumnar.set_options('n', sort_by => ARRAY['payload'])")
        _run(dsn, "ALTER TABLE n DROP COLUMN payload")
        r["dropped"] = _refusal(dsn, "SELECT pgcolumnar.vacuum_sorted('n')",
                                'declared sort_by column "payload" does not exist')
        _await_up(dsn)
        _run(dsn, "SELECT pgcolumnar.reset_options('n', sort_by => true)")

        _run(dsn, "CREATE TABLE e (id int, host text) USING pgcolumnar")
        r["empty"] = _ran_ok(dsn, "SELECT pgcolumnar.vacuum_sorted('e', 'host')")
        _run(dsn, "INSERT INTO e VALUES (1, 'x')")
        r["single"] = _ran_ok(dsn, "SELECT pgcolumnar.vacuum_sorted('e', 'host')")
        r["single rows"] = _value(dsn, "SELECT count(*) FROM e")

        r["major"] = _major(dsn)
        if isinstance(r["major"], int) and r["major"] >= 18:
            _await_up(dsn)
            _run(dsn, "CREATE TABLE v (id int, k int, kv int GENERATED ALWAYS AS (k * 2) VIRTUAL) USING pgcolumnar")
            _run(dsn, "INSERT INTO v (id, k) SELECT g, g % 10 FROM generate_series(1, 100) g")
            r["virtual set"] = _refusal(dsn, "SELECT pgcolumnar.set_options('v', sort_by => ARRAY['kv'])",
                                        "cannot be used in sort_by")
            r["virtual sort"] = _refusal(dsn, "SELECT pgcolumnar.vacuum_sorted('v', 'kv')",
                                         "is a virtual generated column and cannot be a sort key")
        r["survived"] = _survived(dsn, pgc_cluster, before_crash)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_nsb_row_count(nsb, expect):
    expect.text(nsb["rows"], "40960", "row count")


def test_nsb_the_plan_under_test_is_a_columnar_custom_scan(nsb, expect):
    expect.text(nsb["scan"], "yes", "the plan under test is a columnar custom scan")


def test_nsb_equality_parity_pre_sort(nsb, expect):
    expect.text(nsb["eq pre"][0], nsb["eq pre"][1], "equality parity (pre-sort)")


def test_nsb_the_scan_order_starts_unsorted(nsb, expect):
    expect.text(nsb["unsorted"], "yes", "premise: before sorting, the scan order does not follow (host, ts)")


def test_nsb_declared_sort_by_round_trips(nsb, expect):
    expect.text(nsb["roundtrip"], "{host,ts}", "declared sort_by round-trips")


def test_nsb_row_count_after_sort(nsb, expect):
    expect.text(nsb["rows after"], "40960", "row count after sort")


def test_nsb_full_table_parity_after_sort(nsb, expect):
    expect.text(nsb["full"][0], nsb["full"][1], "full-table parity after sort")


def test_nsb_equality_parity_post_sort(nsb, expect):
    expect.text(nsb["eq post"][0], nsb["eq post"][1], "equality parity (post-sort)")


def test_nsb_sorting_a_text_key_enables_group_skipping(nsb, expect):
    expect.text(nsb["skipping"], "yes", "sorting a text key enables group skipping")


def test_nsb_the_scan_order_follows_the_declared_key(nsb, expect):
    inv = nsb["inversions"]
    expect.num(int(inv) if inv.isdigit() else inv, 0, "the scan order follows the declared key (host, ts)")


def test_nsb_explicit_column_sort_parity(nsb, expect):
    expect.text(nsb["explicit"][0], nsb["explicit"][1], "explicit-column sort parity")


def test_nsb_explicit_columns_lay_the_rows_out_as_the_declared_key_does(nsb, expect):
    expect.text(nsb["explicit layout"][0], nsb["explicit layout"][1],
                "explicit columns lay the rows out as the declared key does")


def test_nsb_sort_by_cleared(nsb, expect):
    expect.text(nsb["cleared"], "true", "sort_by cleared")


def test_nsb_zero_arg_errors_when_no_key_declared(nsb, expect):
    expect.text(nsb["no key"], "22023 has no declared sort_by key", "zero-arg errors when no key declared")


def test_nsb_set_options_rejects_a_nonexistent_sort_by_column(nsb, expect):
    expect.text(nsb["bad column"], "P0001 cannot be used in sort_by",
                "set_options rejects a nonexistent sort_by column")


def test_nsb_zero_arg_errors_on_a_dropped_declared_column(nsb, expect):
    expect.text(nsb["dropped"], '42703 declared sort_by column "payload" does not exist',
                "zero-arg errors on a dropped declared column")


def test_nsb_empty_table_sorts_without_error(nsb, expect):
    expect.text(nsb["empty"], "ok", "empty table sorts without error")


def test_nsb_single_row_table_sorts_without_error(nsb, expect):
    expect.text(nsb["single"], "ok", "single-row table sorts without error")


def test_nsb_single_row_parity(nsb, expect):
    expect.text(nsb["single rows"], "1", "single-row parity")


def test_nsb_set_options_rejects_a_virtual_generated_sort_by_column(nsb, expect):
    m = nsb["major"]
    if not isinstance(m, int):
        expect.text(m, "readable", "could not read the server major, so the virtual arm cannot be gated")
        return
    if m < 18:
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"VIRTUAL generated columns arrived in PostgreSQL 18; this server is {m}",
                          name="virtual generated sort keys")
        return
    expect.text(nsb["virtual set"], "P0001 cannot be used in sort_by",
                "set_options rejects a virtual generated sort_by column")


def test_nsb_vacuum_sorted_rejects_an_explicit_virtual_generated_column(nsb, expect):
    m = nsb["major"]
    if not isinstance(m, int) or m < 18:
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"VIRTUAL generated columns arrived in PostgreSQL 18; this server is {m}")
        return
    expect.text(nsb["virtual sort"], "22023 is a virtual generated column and cannot be a sort key",
                "vacuum_sorted rejects an explicit virtual generated column")


def test_nsb_backend_still_up_after_the_refusals(nsb, expect):
    expect.text(nsb["survived"], "1", "backend still up after the refusals")

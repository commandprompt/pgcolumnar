"""A cancel lands inside one large row group's decode (#254). The pytest twin of
`test/cancel_decode.sh`, every check by the shell's name (#1341).

Loading and decoding a row group happens inside one call into the access method, so no
executor loop runs for its duration, and stripe_row_limit makes that window as long as a
user likes. The extension's own interrupt checks are what keep it short. With them, a
statement_timeout cancel arrives as fast on one 4,000,000-row group as on many small ones;
without the group load's per-chunk check it waits for the whole group (the shell's header
has the measurements). The property is that ratio, read back to back in the same run, plus
an absolute ceiling.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order. A reading is a
value or a query_error(), never text that stands in for one.

A CANCEL IS 57014 AND ITS MESSAGE. The shell used to take any output containing "canceling
statement" (#1341); both twins now want SQLSTATE 57014 and "canceling statement due to
statement timeout", and anything else is reported as what came back.

AFTER THE CANCEL, IN THE SAME SESSION. The shell's three follow-up checks each opened a new
psql, so they read a backend that had never been cancelled: with a flag the group load sets
and a cancel leaves set, the next scan in that backend refused or aborted and main passed 10
of 10 (#1341). One connection now takes the cancel and then answers all three.

NO BACKEND DIED. Read from the server log once the server answers again: a fresh SELECT 1
answers after crash recovery, so it cannot see a crash; the log can.
"""
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_cancel_decode"
ROWS = 4000000
DDL = "a int, b int, c text, d text, e text, f text, h text, i text"
SEL = "g, g % 1000, 'r'||g, 'r'||g, 'r'||g, 'r'||g, 'r'||g, 'r'||g"
PRED = ("(a % 7) = 999 AND (b % 7) = 999 AND length(c) < 0 AND length(d) < 0 "
        "AND length(e) < 0 AND length(f) < 0 AND length(h) < 0 AND length(i) < 0")
CANCEL_MESSAGE = "canceling statement due to statement timeout"


def _query(t):
    return f"SELECT count(*) FROM {t} WHERE {PRED}"


def _one(dsn, sql):
    """-> the first cell of sql's first row as text, or a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return str(conn.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _cancel(conn, sql):
    """Run sql under a 100 ms statement_timeout on conn -> None when it was cancelled
    (57014 and the timeout's own message), else what came back instead."""
    conn.execute("SET statement_timeout = 100")
    try:
        rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        code, msg = exc.sqlstate, getattr(exc.diag, "message_primary", None)
        if code == "57014" and msg == CANCEL_MESSAGE:
            return None
        return f"not cancelled: {code} {msg or str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
    return f"not cancelled: it returned {rows}"


def _cancel_ms(dsn, t):
    """-> the best of three cancel latencies in ms, as a string of digits, or what came back
    instead. The minimum, because scheduling noise only ever adds latency."""
    best = None
    for _ in range(3):
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                t0 = time.monotonic()
                why = _cancel(conn, _query(t))
                ms = int((time.monotonic() - t0) * 1000)
        except psycopg.Error as exc:
            return f"not cancelled: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
        if why is not None:
            return why
        best = ms if best is None else min(best, ms)
    return str(best)


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError:
        return 0


def _session(dsn):
    """One connection takes the cancel, then answers usable, rows and answer -> a dict of
    those three readings, each a value or what stood in its way."""
    out = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            why = _cancel(conn, _query("one"))
            if why is not None:
                return {k: f"the session's scan was not cancelled: {why}" for k in ("usable", "rows", "answer")}
            conn.execute("RESET statement_timeout")
            for k, sql in (("usable", "SELECT 1"), ("rows", "SELECT count(*) || '|' || sum(a) FROM one"),
                           ("answer", f"SELECT count(*) FROM one WHERE {PRED}")):
                try:
                    out[k] = str(conn.execute(sql).fetchone()[0])
                except psycopg.Error as exc:
                    out[k] = query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    except psycopg.Error as exc:
        err = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        for k in ("usable", "rows", "answer"):
            out.setdefault(k, query_error(err))
    return out


def _is_ms(v):
    return isinstance(v, str) and v.isdigit()


@pytest.fixture(scope="module")
def cd(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION pgcolumnar")
            for t, limit in (("many", 20000), ("one", 40000000)):
                conn.execute(f"CREATE TABLE {t} ({DDL}) USING pgcolumnar")
                conn.execute(f"SELECT pgcolumnar.set_options('{t}', stripe_row_limit => {limit}, "
                             "chunk_group_row_limit => 10000)")
                conn.execute(f"INSERT INTO {t} SELECT {SEL} FROM generate_series(1, {ROWS}) g")
        r = {"groups many": _one(dsn, "SELECT count(*) FROM pgcolumnar.stats('many')"),
             "groups one": _one(dsn, "SELECT count(*) FROM pgcolumnar.stats('one')")}
        t0 = time.monotonic()
        _one(dsn, _query("one"))
        r["full"] = int((time.monotonic() - t0) * 1000)
        before = _crashes(pgc_cluster)
        r["many"] = _cancel_ms(dsn, "many")
        r["one"] = _cancel_ms(dsn, "one")
        print(f"uncancelled scan {r['full']} ms; cancel latency: many groups {r['many']} ms, "
              f"one group {r['one']} ms")
        r.update(_session(dsn))
        for _ in range(60):
            if _one(dsn, "SELECT 1") == "1":
                break
            time.sleep(0.5)
        r["crashes"] = _crashes(pgc_cluster) - before
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_many_group_table_has_many_groups(cd, expect):
    n = cd["groups many"]
    expect.text("yes" if n.isdigit() and int(n) > 50 else f"no ({n} groups)", "yes",
                "the many-group table has many groups")


def test_the_one_group_table_has_exactly_one_group(cd, expect):
    expect.text(cd["groups one"], "1", "the one-group table has exactly one group")


def test_the_scan_outlasts_the_timeout(cd, expect):
    expect.text("yes" if cd["full"] > 300 else f"no (the uncancelled scan took {cd['full']} ms)", "yes",
                "the scan outlasts the timeout, so a cancel is possible")


def test_a_scan_of_many_small_groups_is_cancelled(cd, expect):
    expect.text("yes" if _is_ms(cd["many"]) else cd["many"], "yes", "a scan of many small groups is cancelled")


def test_a_scan_of_one_large_group_is_cancelled(cd, expect):
    expect.text("yes" if _is_ms(cd["one"]) else cd["one"], "yes", "a scan of one large group is cancelled")


def test_cancel_latency_does_not_grow_with_row_group_size(cd, expect):
    one, many = cd["one"], cd["many"]
    ok = _is_ms(one) and _is_ms(many) and int(one) <= int(many) * 3 + 50
    expect.text("yes" if ok else f"no (one group: {one}; many groups: {many}; in ms)", "yes",
                "cancel latency does not grow with row group size")


def test_the_cancel_arrives_within_an_absolute_bound(cd, expect):
    one = cd["one"]
    expect.text("yes" if _is_ms(one) and int(one) < 2000 else f"no (one group: {one}; in ms)", "yes",
                "the cancel arrives within a generous absolute bound")


def test_the_session_is_usable_after_the_cancel(cd, expect):
    expect.text(cd["usable"], "1", "the session is usable after the cancel")


def test_the_table_still_reads_correctly_after_the_cancel(cd, expect):
    # a column's sum as well as the count, so the data is checked, not only the row count
    expect.text(cd["rows"], f"{ROWS}|{ROWS * (ROWS + 1) // 2}", "the table still reads correctly after the cancel")


def test_the_cancelled_predicate_returns_its_true_answer(cd, expect):
    expect.text(cd["answer"], "0", "the cancelled predicate returns its true answer when allowed to finish")


def test_no_backend_crashed_while_the_scans_were_cancelled(cd, expect):
    expect.num(cd["crashes"], 0, "no backend crashed while the scans were cancelled")

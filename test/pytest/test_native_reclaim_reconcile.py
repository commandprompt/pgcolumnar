"""A reuse op drops a free_space row that overlaps a live group, and only that row
(Phase F reconcile). The pytest twin of `test/native_reclaim_reconcile.sh`, every check
by the shell's name (#1341).

A crash in end-truncation's narrow window can leave a free_space row over a live group.
PgColumnarReconcileFreeList, run at the start of compact_rewrite, compact and truncate,
drops any free row overlapping a live row-group footprint. The suite injects such a row
(freed_xid 1, so reusable) over the first live group before each of the three ops and
asserts it is gone after, the data still equals a heap mirror, and the legitimate free
ranges come through a plain compact (which does not reuse) unchanged: a reconcile that
dropped every free row passed all the other checks.

READINGS ARE VALUES: a failed query is a query_error(), so two failed reads never agree,
and the row sets are compared as an md5 over the sorted rows.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_native_reclaim_reconcile"
HASH = "SELECT md5(coalesce(string_agg(id || ':' || v, ',' ORDER BY id), '')) FROM {}"


def _message(exc):
    diag = getattr(exc, "diag", None)
    primary = diag.message_primary if diag is not None else None
    return primary or (str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


def _run(dsn, sql):
    """One statement in its own autocommit session (truncate refuses a transaction
    block). -> None, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return None


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


@pytest.fixture(scope="module")
def rr(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    dsn = pgc_cluster.dsn(dbname=DB)
    try:
        for s in ("CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                  f"ALTER DATABASE {DB} SET pgcolumnar.enable_end_truncation = on",
                  "CREATE TABLE h (id int, v text)",
                  "CREATE TABLE n (id int, v text) USING pgcolumnar",
                  "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000)",
                  "INSERT INTO h SELECT g, md5(g::text) FROM generate_series(1, 8000) g",
                  "INSERT INTO n SELECT g, md5(g::text) FROM generate_series(1, 8000) g",
                  "DELETE FROM h WHERE id % 3 = 0",
                  "DELETE FROM n WHERE id % 3 = 0"):
            _run(dsn, s)
        r = {}
        hn = lambda: _value(dsn, HASH.format("n"))
        hh = lambda: _value(dsn, HASH.format("h"))
        r["parity before"] = (hn(), hh())
        sid = _value(dsn, "SELECT pgcolumnar.get_storage_id('n')")
        first = ("SELECT {} FROM pgcolumnar.stats('n') ORDER BY fileoffset LIMIT 1")

        def inject():
            off, ln = _value(dsn, first.format("fileoffset")), _value(dsn, first.format("datalength"))
            _run(dsn, f"INSERT INTO pgcolumnar.free_space VALUES ({sid}, {off}, {ln}, 1)")

        stale = lambda: _value(dsn, "SELECT count(*) FROM pgcolumnar.free_space WHERE freed_xid = 1")
        legit = lambda: _value(dsn, "SELECT count(*) || '|' || coalesce(sum(byte_length), 0) "
                                    f"FROM pgcolumnar.free_space WHERE storage_id = {sid} AND freed_xid <> 1")

        inject()
        r["injected"] = stale()
        _run(dsn, "SELECT pgcolumnar.compact_rewrite('n', 0.1)")
        r["rewrite reconciled"] = stale()
        r["rewrite intact"] = (hn(), hh())
        r["count n"] = _value(dsn, "SELECT count(*) FROM n")
        r["count h"] = _value(dsn, "SELECT count(*) FROM h")

        inject()
        r["legit before"] = legit()
        _run(dsn, "SELECT pgcolumnar.compact('n')")
        r["compact reconciled"] = stale()
        r["legit after"] = legit()
        r["compact intact"] = (hn(), hh())

        inject()
        _run(dsn, "SELECT pgcolumnar.truncate('n')")
        r["truncate reconciled"] = stale()
        r["truncate intact"] = (hn(), hh())

        for cyc in (1, 2):
            _run(dsn, f"DELETE FROM h WHERE id % 5 = {cyc}")
            _run(dsn, f"DELETE FROM n WHERE id % 5 = {cyc}")
            _run(dsn, "SELECT pgcolumnar.compact_rewrite('n', 0.0)")
            r[f"cycle {cyc}"] = (hn(), hh())
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _legit_present(v):
    if not isinstance(v, str) or v.startswith(QUERY_ERROR) or v == "" or v.startswith("0|"):
        return f"no ({v})"
    return "yes"


def test_rr_parity_before(rr, expect):
    expect.text(rr["parity before"][0], rr["parity before"][1], "parity before")


def test_rr_stale_row_injected(rr, expect):
    expect.text(rr["injected"], "1", "stale overlapping free row injected")


def test_rr_compact_rewrite_reconciled(rr, expect):
    expect.text(rr["rewrite reconciled"], "0", "compact_rewrite reconciled the stale row")


def test_rr_data_intact_after_rewrite(rr, expect):
    expect.text(rr["rewrite intact"][0], rr["rewrite intact"][1], "data intact after reconcile + rewrite")


def test_rr_row_count_intact(rr, expect):
    expect.num(_int(rr["count n"]), _int(rr["count h"]), "row count intact")


def test_rr_a_legitimate_free_range_exists(rr, expect):
    expect.text(_legit_present(rr["legit before"]), "yes",
                "premise: a legitimate free range exists before the plain compact")


def test_rr_plain_compact_reconciled(rr, expect):
    expect.text(rr["compact reconciled"], "0", "plain compact reconciled the stale row")


def test_rr_plain_compact_kept_the_legitimate_ranges(rr, expect):
    expect.text(rr["legit after"], rr["legit before"],
                "plain compact kept the legitimate free ranges, which overlap no live group")


def test_rr_data_intact_after_compact(rr, expect):
    expect.text(rr["compact intact"][0], rr["compact intact"][1], "data intact after compact reconcile")


def test_rr_truncate_reconciled(rr, expect):
    expect.text(rr["truncate reconciled"], "0", "truncate reconciled the stale row")


def test_rr_data_intact_after_truncate(rr, expect):
    expect.text(rr["truncate intact"][0], rr["truncate intact"][1], "data intact after truncate reconcile")


def test_rr_parity_after_reuse_cycle_1(rr, expect):
    expect.text(rr["cycle 1"][0], rr["cycle 1"][1], "parity after reuse cycle 1")


def test_rr_parity_after_reuse_cycle_2(rr, expect):
    expect.text(rr["cycle 2"][0], rr["cycle 2"][1], "parity after reuse cycle 2")

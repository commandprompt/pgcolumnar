"""CREATE TABLE AS into a columnar table, with a parallel source plan (#387). The pytest
twin of `test/native_ctas.sh`, every check by the shell's name (#1341).

The storage-row writer re-checks for an existing row under an advisory lock against
GetLatestSnapshot(), which raises "cannot update SecondarySnapshot during a parallel
operation" under parallel mode, and CTAS runs its whole executor in parallel mode when
the source plan is parallel. The fix skips the lock and the fresh snapshot for a
relation this transaction created. Asserted: a parallel CTAS succeeds, writes every row
with its content and exactly one storage row; a serial CTAS and WITH NO DATA plus INSERT
keep working; and a COMMITTED empty table first-written by two sessions still
serializes on the lock.

WHAT "SUCCEEDS" READS: the statement ran to the end, "ok", or what happened instead.
The shell used to grep for the text ERROR, and a backend that crashed mid-statement
prints none, so with the creation path replaced by abort() all three passed.

THE RACE IS FORCED: writer A first-writes the table and holds its transaction open,
writer B first-writes it while A is open and is seen waiting on a lock, then A commits.
With the lock, B re-checks after A's commit and goes on; without it, B inserts a second
storage row and fails on storage_pkey. Two plain concurrent INSERTs used to stand in for
this, and a writer that dropped the lock passed every check.

Survival reads the server log's count of backends killed by a signal as well as
SELECT 1, and each reading first waits for the server, so a crash reds its own arm and
the survival check rather than every reading after it.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_ctas"
ROWS = 200000
FORCE = ("SET parallel_setup_cost=0", "SET parallel_tuple_cost=0",
         "SET min_parallel_table_scan_size=0", "SET max_parallel_workers_per_gather=2")


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


def _await_up(dsn):
    """Wait, at most 60 s, until the server answers, as the shell's await_up."""
    for _ in range(120):
        if _value(dsn, "SELECT 1") == "1":
            return
        time.sleep(0.5)


def _ran_ok(dsn, statements):
    """-> "ok" when every statement ran and the transaction committed, or what happened
    instead: a refusal with its SQLSTATE, or the connection lost."""
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            for s in statements:
                conn.execute(s)
            conn.commit()
    except psycopg.Error as exc:
        if exc.sqlstate:
            return f"refused: {exc.sqlstate} {_message(exc)}"
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


def _storage_rows(dsn, rel):
    return _value(dsn, "SELECT count(*) FROM pgcolumnar.storage s "
                       f"JOIN pg_class c ON c.oid = s.relation_oid WHERE c.relname = '{rel}'")


def _race(dsn):
    """Writer A first-writes ctas_race and holds its transaction open; writer B
    first-writes it while A is open; A commits once B waits on a lock (or has finished).
    -> (whether A was open when B started, "A <how A ended>, B <how B ended>")."""
    out = {"a": "no (never ran)", "b": "no (never ran)"}
    a_open = "0"
    try:
        a = psycopg.connect(dsn, connect_timeout=10)
    except psycopg.Error as exc:
        return query_error(_message(exc)), f"A no ({_message(exc)}), B no (never ran)"
    try:
        try:
            a.execute("INSERT INTO ctas_race SELECT g, 'w1-'||g FROM generate_series(1,20000) g")
        except psycopg.Error as exc:
            out["a"] = (f"refused: {exc.sqlstate} {_message(exc)}" if exc.sqlstate
                        else f"no ({_message(exc)})")
            return a_open, f"A {out['a']}, B {out['b']}"
        a_open = _value(dsn, "SELECT count(*) FROM pg_stat_activity "
                             f"WHERE pid = {a.info.backend_pid} AND state = 'idle in transaction'")

        def writer_b():
            out["b"] = _ran_ok(dsn, ["INSERT INTO ctas_race SELECT g, 'w2-'||g FROM generate_series(1,20000) g"])

        t = threading.Thread(target=writer_b, daemon=True)
        t.start()
        for _ in range(40):
            if not t.is_alive():
                break
            if _value(dsn, "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                           "AND query LIKE 'INSERT INTO ctas_race%w2-%'") == "1":
                break
            time.sleep(0.25)
        try:
            a.commit()
            out["a"] = "ok"
        except psycopg.Error as exc:
            out["a"] = (f"refused: {exc.sqlstate} {_message(exc)}" if exc.sqlstate
                        else f"no ({_message(exc)})")
        t.join(60)
        if t.is_alive():
            out["b"] = "no (still running after 60 s)"
    finally:
        try:
            a.close()
        except psycopg.Error:
            pass
    return a_open, f"A {out['a']}, B {out['b']}"


@pytest.fixture(scope="module")
def ct(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as adm:
        adm.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        adm.execute(f"CREATE DATABASE {DB}")
    dsn = pgc_cluster.dsn(dbname=DB)
    r = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE ctas_src AS SELECT g AS id, 'h'||(g%97) AS host, "
                         f"(g%1000)::float8 AS v FROM generate_series(1,{ROWS}) g")
        before = _crashes(pgc_cluster)
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                for s in FORCE:
                    conn.execute(s)
                plan = "\n".join(row[0] for row in conn.execute(
                    "EXPLAIN (COSTS OFF) CREATE TABLE ctas_p USING pgcolumnar AS "
                    "SELECT * FROM ctas_src ORDER BY host"))
            r["parallel plan"] = "yes" if ("Gather" in plan) else f"no ({plan.splitlines()[:2]})"
        except psycopg.Error as exc:
            r["parallel plan"] = query_error(_message(exc))

        r["parallel ok"] = _ran_ok(dsn, list(FORCE) + [
            "CREATE TABLE ctas_p USING pgcolumnar AS SELECT * FROM ctas_src ORDER BY host"])
        _await_up(dsn)
        r["parallel rows"] = _value(dsn, "SELECT count(*) FROM ctas_p")
        r["parallel content"] = _value(dsn, "SELECT count(*) FROM ctas_p p JOIN ctas_src s USING (id) "
                                            "WHERE p.host = s.host AND p.v = s.v")
        r["serial ok"] = _ran_ok(dsn, ["SET max_parallel_workers_per_gather=0",
                                       "CREATE TABLE ctas_s USING pgcolumnar AS SELECT * FROM ctas_src"])
        _await_up(dsn)
        r["serial rows"] = _value(dsn, "SELECT count(*) FROM ctas_s")
        r["nodata ok"] = _ran_ok(dsn, list(FORCE) + [
            "CREATE TABLE ctas_nd USING pgcolumnar AS SELECT * FROM ctas_src WITH NO DATA",
            "INSERT INTO ctas_nd SELECT * FROM ctas_src"])
        _await_up(dsn)
        r["nodata rows"] = _value(dsn, "SELECT count(*) FROM ctas_nd")
        r["parallel storage rows"] = _storage_rows(dsn, "ctas_p")
        r["after loads"] = _survived(dsn, pgc_cluster, before)

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE ctas_race (id int, v text) USING pgcolumnar")
        before_race = _crashes(pgc_cluster)
        r["a open"], r["writers"] = _race(dsn)
        _await_up(dsn)
        r["race storage rows"] = _storage_rows(dsn, "ctas_race")
        r["race rows"] = _value(dsn, "SELECT count(*) FROM ctas_race")
        r["after race"] = _survived(dsn, pgc_cluster, before_race)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as adm:
            adm.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_ctas_source_plan_is_parallel(ct, expect):
    expect.text(ct["parallel plan"], "yes", "premise: the CTAS source plan is parallel")


def test_ctas_parallel_into_columnar_succeeds(ct, expect):
    expect.text(ct["parallel ok"], "ok", "parallel CTAS into a columnar table succeeds (#387)")


def test_ctas_parallel_wrote_every_row(ct, expect):
    expect.text(ct["parallel rows"], str(ROWS), "parallel CTAS wrote every row (#387)")


def test_ctas_parallel_content_matches_the_source(ct, expect):
    expect.text(ct["parallel content"], str(ROWS), "parallel CTAS content matches the source (#387)")


def test_ctas_serial_still_succeeds(ct, expect):
    expect.text(ct["serial ok"], "ok", "serial CTAS still succeeds")


def test_ctas_serial_row_count(ct, expect):
    expect.text(ct["serial rows"], str(ROWS), "serial CTAS row count")


def test_ctas_with_no_data_then_insert_succeeds(ct, expect):
    expect.text(ct["nodata ok"], "ok", "WITH NO DATA then INSERT still succeeds")


def test_ctas_with_no_data_then_insert_row_count(ct, expect):
    expect.text(ct["nodata rows"], str(ROWS), "WITH NO DATA then INSERT row count")


def test_ctas_parallel_produced_one_storage_row(ct, expect):
    expect.text(ct["parallel storage rows"], "1", "the parallel CTAS produced exactly one storage row")


def test_ctas_backend_still_up_after_the_loads(ct, expect):
    expect.text(ct["after loads"], "1", "backend still up after the CTAS loads")


def test_ctas_second_writer_started_while_the_first_was_open(ct, expect):
    expect.text(ct["a open"], "1", "the second writer started while the first held its transaction open")


def test_ctas_concurrent_first_writes_both_succeed(ct, expect):
    expect.text(ct["writers"], "A ok, B ok", "concurrent first writes to a committed empty table both succeed")


def test_ctas_race_produced_one_storage_row(ct, expect):
    expect.text(ct["race storage rows"], "1", "and they produced one storage row, not two")


def test_ctas_race_every_row_present(ct, expect):
    expect.text(ct["race rows"], "40000", "and every row from both writers is present")


def test_ctas_backend_still_up_after_the_race(ct, expect):
    expect.text(ct["after race"], "1", "backend still up after the concurrent first writes")

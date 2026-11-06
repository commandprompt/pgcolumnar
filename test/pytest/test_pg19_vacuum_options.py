"""PostgreSQL 19's parallel-autovacuum storage parameter on a columnar table (#398). The
pytest twin of `test/pg19_vacuum_options.sh`, every check by the shell's name (#1341).

autovacuum_parallel_workers is accepted on a columnar table and lands in reloptions as
it does on heap, and it changes nothing: pgcolumnar's relation_vacuum does no parallel
work. Asserted: the server variable exists; the parameter is accepted and recorded on
both access methods; VACUUM, VACUUM (PARALLEL 2) and pgcolumnar.vacuum succeed with it
set and leave the rows and their content unchanged; and VACUUM VERBOSE reports on the
heap table but not on the columnar one, whose vacuum path is ours.

A STATEMENT SUCCEEDED WHEN THE DRIVER SAYS SO, not when its output lacks "ERROR": with
the server stopped, the shell's grep for ERROR read every vacuum as a success.

ONE TEST PER CHECK, each declining before PostgreSQL 19 as test_native_repack.py does, and
listed per major in expected_unrunnable.txt. One test holding all twelve stopped at the
first red, so a mutant that reddened three checks reported one. On a private database.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_pg19_vacuum_options"
ROWS = 100000


def _ran(dsn, sql, notices=None):
    """-> "ok", or "failed (...)" carrying the error, as the shell's ran()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if notices is not None:
                conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
            conn.execute(sql)
        return "ok"
    except psycopg.Error as exc:
        return f"failed ({str(exc).splitlines()[0] if str(exc) else type(exc).__name__})"


def _one(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])
    return "" if row is None or row[0] is None else str(row[0])


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


@pytest.fixture(scope="module")
def p19(pgc_cluster):
    srv = _one(pgc_cluster.dsn(), "SHOW server_version_num")
    if not srv.isdigit():
        yield {"no server": srv}
        return
    if int(srv) < 190000:
        yield {"decline": f"parallel autovacuum arrived in PostgreSQL 19; this server is {int(srv) // 10000}"}
        return
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE av_c (id int, v text) USING pgcolumnar")
            conn.execute("CREATE TABLE av_h (id int, v text)")
            conn.execute(f"INSERT INTO av_c SELECT g,'x'||g FROM generate_series(1,{ROWS}) g")
            conn.execute(f"INSERT INTO av_h SELECT g,'x'||g FROM generate_series(1,{ROWS}) g")
            conn.execute("DELETE FROM av_c WHERE id % 4 = 0")
            conn.execute("DELETE FROM av_h WHERE id % 4 = 0")
        r = {"exists": _ran(dsn, "SHOW autovacuum_max_parallel_workers")}
        for t in ("av_c", "av_h"):
            r[("accepted", t)] = _ran(dsn, f"ALTER TABLE {t} SET (autovacuum_parallel_workers = 3)")
            r[("recorded", t)] = _one(dsn, f"SELECT array_to_string(reloptions,',') FROM pg_class WHERE relname='{t}'")
        digest = "SELECT md5(string_agg(id::text||':'||v, ',' ORDER BY id)) FROM av_c"
        live_before, hash_before = _one(dsn, "SELECT count(*) FROM av_c"), _one(dsn, digest)
        r["vacuum"] = _ran(dsn, "VACUUM av_c")
        r["parallel"] = _ran(dsn, "VACUUM (PARALLEL 2) av_c")
        r["pgc vacuum"] = _ran(dsn, "SELECT pgcolumnar.vacuum('av_c')")
        r["rows"] = (_one(dsn, "SELECT count(*) FROM av_c"), live_before)
        r["content"] = (_one(dsn, digest), hash_before)
        heap_notes, col_notes = [], []
        heap_ran = _ran(dsn, "VACUUM (VERBOSE) av_h", heap_notes)
        col_ran = _ran(dsn, "VACUUM (VERBOSE) av_c", col_notes)
        r["heap verbose"] = ("yes" if heap_ran == "ok" and any("finished vacuuming" in n.lower() for n in heap_notes)
                             else f"no ({heap_ran})")
        if col_ran != "ok":
            r["col verbose"] = f"no reading (VACUUM {col_ran})"
        elif any("finished vacuuming" in n.lower() and "av_c" in n for n in col_notes):
            r["col verbose"] = "reports"
        else:
            r["col verbose"] = "silent"
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _runnable(p19, expect):
    """False after declining or failing on a server that is not 19 or did not answer."""
    if "no server" in p19:
        expect.text(p19["no server"], "a server_version_num", "the server did not answer 'SHOW server_version_num'")
        return False
    if "decline" in p19:
        expect.cannot_run("UNSUPPORTED_MAJOR", p19["decline"], name="parallel autovacuum")
        return False
    return True


def test_the_server_variable_exists(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19["exists"], "ok", "autovacuum_max_parallel_workers exists on this server")


def test_the_parameter_is_accepted_on_columnar(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19[("accepted", "av_c")], "ok", "autovacuum_parallel_workers is accepted on av_c")


def test_the_parameter_is_recorded_on_columnar(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19[("recorded", "av_c")], "autovacuum_parallel_workers=3",
                    "and it is recorded in reloptions for av_c")


def test_the_parameter_is_accepted_on_heap(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19[("accepted", "av_h")], "ok", "autovacuum_parallel_workers is accepted on av_h")


def test_the_parameter_is_recorded_on_heap(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19[("recorded", "av_h")], "autovacuum_parallel_workers=3",
                    "and it is recorded in reloptions for av_h")


def test_vacuum_with_the_parameter_succeeds(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19["vacuum"], "ok", "VACUUM on a columnar table with the parameter set succeeds")


def test_parallel_vacuum_succeeds(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19["parallel"], "ok", "VACUUM (PARALLEL 2) on a columnar table succeeds")


def test_pgcolumnar_vacuum_succeeds(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19["pgc vacuum"], "ok", "pgcolumnar.vacuum with the parameter set succeeds")


def test_the_rows_are_unchanged(p19, expect):
    if _runnable(p19, expect):
        now, before = p19["rows"]
        expect.num(_int(now), _int(before), "the rows are unchanged by any of it")


def test_the_content_is_unchanged(p19, expect):
    if _runnable(p19, expect):
        now, before = p19["content"]
        expect.text(now, before, "and the content is unchanged")


def test_vacuum_verbose_reports_on_heap(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19["heap verbose"], "yes", "control: VACUUM VERBOSE reports on the heap table")


def test_vacuum_verbose_is_silent_on_columnar(p19, expect):
    if _runnable(p19, expect):
        expect.text(p19["col verbose"], "silent",
                    "columnar VACUUM VERBOSE reports nothing, because the AM path is ours")

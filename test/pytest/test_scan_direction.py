"""A scrollable cursor over a columnar table answers backward fetches as heap does, or
refuses them; it never invents an answer. The pytest twin of `test/scan_direction.sh`,
every check by the shell's name (#1341).

The access method decodes forwards only. At shipped defaults the custom scan serves a
cursor and, since it does not claim backward support, PostgreSQL materializes under a
SCROLL cursor, so FETCH BACKWARD matches heap. With the custom scan off the cursor
reaches the access method itself, which must raise 0A000 for a backward fetch and for
FETCH LAST rather than return nothing.

HOW A FETCH IS READ. A cursor's rows are compared with heap's only when both sides
returned rows: an empty or failed side is "no reading", where two identical failures
used to read "matches". A statement reads as its SQLSTATE, "00000" when it ran, or
"no reading" when the server could not be reached, which used to read "00000".
The row counts and the plan are read the same way: a failed reading is a
query_error() value that reds its own check by name, not a fixture error that
ERRORs every test at setup (#1522).

ON A PRIVATE DATABASE, with max_parallel_workers_per_gather = 0 as the shell's cluster
runs.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_scan_direction"
OFF = "SET pgcolumnar.enable_custom_scan = off"
FWDBACK = ["FETCH FORWARD 5 FROM c", "FETCH BACKWARD 3 FROM c"]
FWDONLY = ["FETCH FORWARD 5 FROM c"]


def _connect(dsn):
    try:
        return psycopg.connect(dsn, connect_timeout=5)
    except psycopg.OperationalError as exc:
        return f"no reading ({str(exc).splitlines()[0] if str(exc) else type(exc).__name__})"


def _cursor(dsn, table, setting, fetches):
    """-> (rows, error): the ids every FETCH returned, in order, or the failure."""
    conn = _connect(dsn)
    if isinstance(conn, str):
        return None, conn
    with conn:
        try:
            if setting:
                conn.execute(setting)
            conn.execute(f"DECLARE c SCROLL CURSOR FOR SELECT id FROM {table}")
            rows = [r[0] for f in fetches for r in conn.execute(f).fetchall()]
            conn.execute("CLOSE c")
            return rows, ""
        except psycopg.Error as exc:
            return None, f"SQLSTATE {exc.sqlstate}"


def _rows(dsn, setting, sql):
    """-> every row's first column, or the failure as a query_error(), so a reading
    that fails reds its own check by name rather than erroring the fixture (#1522)."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            if setting:
                conn.execute(setting)
            return [r[0] for r in conn.execute(sql).fetchall()]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _same_rows(heap, col):
    """-> matches, DIFFERS, or "no reading" when either side is empty or failed."""
    (h, herr), (c, cerr) = heap, col
    if herr or cerr or not h or not c:
        return f"no reading (heap [{h if h else herr}], columnar [{c if c else cerr}])"
    return "matches" if h == c else f"DIFFERS (heap {h}, columnar {c})"


def _state(dsn, table, setting, fetches):
    rows, err = _cursor(dsn, table, setting, fetches)
    if err.startswith("SQLSTATE "):
        return err[len("SQLSTATE "):]
    return err or "00000"


@pytest.fixture(scope="module")
def sd(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"ALTER DATABASE {DB} SET max_parallel_workers_per_gather = 0")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE t_col (id int, v text) USING pgcolumnar")
            conn.execute("CREATE TABLE t_heap (id int, v text)")
            for t in ("t_col", "t_heap"):
                conn.execute(f"INSERT INTO {t} SELECT g, 'row' || g FROM generate_series(1,20) g")
        counts = [_rows(dsn, "", f"SELECT count(*) FROM {t}") for t in ("t_col", "t_heap")]
        plan_off = _rows(dsn, OFF, "EXPLAIN (COSTS OFF) SELECT id FROM t_col")
        yield {
            "counts": [c[0] if isinstance(c, list) and c else c for c in counts],
            "plan off": plan_off if isinstance(plan_off, str) else " ".join(plan_off),
            "default": _same_rows(_cursor(dsn, "t_heap", "", FWDBACK), _cursor(dsn, "t_col", "", FWDBACK)),
            "forward": _same_rows(_cursor(dsn, "t_heap", OFF, FWDONLY), _cursor(dsn, "t_col", OFF, FWDONLY)),
            "back": _state(dsn, "t_col", OFF, FWDBACK),
            "last": _state(dsn, "t_col", OFF, ["FETCH LAST FROM c"]),
            "heap": _state(dsn, "t_heap", OFF, FWDBACK),
            "ok": _state(dsn, "t_col", OFF, FWDONLY),
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_pair_holds_the_same_row_count(sd, expect):
    expect.num(sd["counts"][0], sd["counts"][1], "premise: the pair holds the same row count")


def test_at_shipped_defaults_the_cursor_matches_heap(sd, expect):
    expect.text(sd["default"], "matches", "at shipped defaults the cursor matches heap")


def test_with_the_custom_scan_off_the_plan_is_a_seq_scan(sd, expect):
    p = sd["plan off"]
    expect.text("yes" if "Seq Scan" in p else f"no: {p}", "yes",
                "with the custom scan off the plan really is a Seq Scan on the AM")


def test_a_forward_only_fetch_matches_heap_through_the_am(sd, expect):
    expect.text(sd["forward"], "matches", "forward-only fetch still matches heap through the AM")


def test_a_backward_fetch_through_the_am_raises_0a000(sd, expect):
    expect.text(sd["back"], "0A000", "a backward fetch through the AM raises 0A000 rather than answering")


def test_fetch_last_through_the_am_raises_0a000(sd, expect):
    expect.text(sd["last"], "0A000", "FETCH LAST through the AM raises 0A000 rather than returning nothing")


def test_the_same_cursor_on_heap_raises_nothing(sd, expect):
    expect.text(sd["heap"], "00000", "control: the same cursor on a heap table raises nothing")


def test_a_forward_only_fetch_reports_success(sd, expect):
    expect.text(sd["ok"], "00000",
                "control: a forward-only fetch reports success, so the probe is not stuck on 0A000")

"""pgColumnar is not a logical decoding source (#754). The pytest twin of
`test/logical_decoding_source.sh`, every check by the shell's name.

Logical decoding reads the WAL records of heap tuples. Columnar data reaches WAL as
full-page images, which carry no tuple structure, so no change to a columnar table is
emitted. A slot on a database holding columnar tables is not silent either: it carries
pgcolumnar's own catalog writes, which follow groups and not rows.

"Zero changes decoded" is also what a dead slot looks like, so each slot gets a heap
CONTROL written in the same window and asserted to decode. The second slot had none,
and with it created after the insert it carried nothing and every check passed.

A PRIVATE CLUSTER at wal_level=logical: the level cannot be raised without a restart,
and the shared cluster runs at the default.
"""
import pathlib
import shutil
import tempfile

import psycopg
import pytest

from pgc_vacuity import query_error

SLOT, SLOT_B = "pgc_754", "pgc_754b"


def _q(dsn, sql):
    """-> the first column of the first row as text, or query_error() carrying the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute(sql).fetchone()
            return "" if row is None or row[0] is None else str(row[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, sql):
    """-> None, or query_error() when the statement failed (the next reading then fails by name)."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql)
        return None
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


@pytest.fixture(scope="module")
def lds(pgc_cluster):
    from pgc_cluster import Cluster, pick_port

    if not (pathlib.Path(pgc_cluster.libdir) / "test_decoding.so").is_file():
        yield {"missing": f"test_decoding.so is not in {pgc_cluster.libdir}"}
        return
    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-lds-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "lds", datadir, pick_port(26))
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\nwal_level=logical\nmax_replication_slots=8\nmax_wal_senders=8\n")
        cluster.start()
        dsn = cluster.dsn()
        r = {}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r["wal_level"] = _q(dsn, "SHOW wal_level")
        _run(dsn, "CREATE TABLE heap_t (id int PRIMARY KEY, v text) USING heap")
        _run(dsn, "CREATE TABLE col_t (id int PRIMARY KEY, v text) USING pgcolumnar")
        # The slot is created before any row is written: a slot only carries later changes.
        _run(dsn, f"SELECT pg_create_logical_replication_slot('{SLOT}', 'test_decoding')")
        r["slot type"] = _q(dsn, f"SELECT slot_type FROM pg_replication_slots WHERE slot_name = '{SLOT}'")
        _run(dsn, "INSERT INTO heap_t SELECT g, 'h' || g FROM generate_series(1, 50) g")
        _run(dsn, "INSERT INTO col_t SELECT g, 'c' || g FROM generate_series(1, 50) g")
        r["heap rows"] = _int(_q(dsn, "SELECT count(*) FROM heap_t"))
        r["col rows"] = _int(_q(dsn, "SELECT count(*) FROM col_t"))
        # Drained once, so every count reads the same snapshot of the slot.
        _run(dsn, f"CREATE TABLE decoded AS SELECT data FROM pg_logical_slot_get_changes('{SLOT}', NULL, NULL)")

        def d(table, pattern):
            return _int(_q(dsn, f"SELECT count(*) FROM {table} WHERE data LIKE '%{pattern}%'"))

        r["decoded"] = _int(_q(dsn, "SELECT count(*) FROM decoded"))
        r["heap decoded"] = d("decoded", "table public.heap_t: INSERT")
        r["col decoded"] = d("decoded", "table public.col_t:")
        r["internal small"] = d("decoded", "table pgcolumnar.")
        # The second slot, with its own heap control in the same window.
        _run(dsn, "CREATE TABLE col_big (id int PRIMARY KEY, v text) USING pgcolumnar")
        _run(dsn, "CREATE TABLE heap_big (id int PRIMARY KEY, v text) USING heap")
        _run(dsn, f"SELECT pg_create_logical_replication_slot('{SLOT_B}', 'test_decoding')")
        _run(dsn, "INSERT INTO col_big SELECT g, 'c' || g FROM generate_series(1, 5000) g")
        _run(dsn, "INSERT INTO heap_big SELECT g, 'h' || g FROM generate_series(1, 5000) g")
        _run(dsn, f"CREATE TABLE decoded_big AS SELECT data FROM pg_logical_slot_get_changes('{SLOT_B}', NULL, NULL)")
        r["big rows"] = _q(dsn, "SELECT count(*) FROM col_big")
        r["heap big decoded"] = d("decoded_big", "table public.heap_big: INSERT")
        r["col big decoded"] = d("decoded_big", "table public.col_big:")
        r["internal big"] = d("decoded_big", "table pgcolumnar.")
        _run(dsn, f"SELECT pg_drop_replication_slot('{SLOT}')")
        _run(dsn, f"SELECT pg_drop_replication_slot('{SLOT_B}')")
        yield r
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def test_lds_test_decoding_is_installed(lds, expect):
    # The shell's pgc_skip records a FAIL under this name when the plugin is missing, and
    # nothing when it is there. Here the same name passes when it is there.
    expect.text("missing" if "missing" in lds else "installed", "installed",
                "test_decoding.so is not installed; build contrib/test_decoding")


def _declined(lds, expect):
    if "missing" in lds:
        expect.cannot_run("MISSING_DEPENDENCY", lds["missing"])
        return True
    return False


def test_lds_server_runs_at_wal_level_logical(lds, expect):
    if not _declined(lds, expect):
        expect.text(lds["wal_level"], "logical", "premise: the server is running at wal_level=logical")


def test_lds_slot_exists_and_is_logical(lds, expect):
    if not _declined(lds, expect):
        expect.text(lds["slot type"], "logical", "premise: the slot exists and is logical")


def test_lds_both_tables_hold_the_same_rows(lds, expect):
    if not _declined(lds, expect):
        expect.num(lds["heap rows"], lds["col rows"], "premise: both tables hold the same 50 rows")


def test_lds_slot_decoded_something(lds, expect):
    if not _declined(lds, expect):
        n = lds["decoded"]
        expect.text("yes" if isinstance(n, int) and n > 0 else f"no ({n})", "yes",
                    "premise: the slot decoded something at all")


def test_lds_heap_control_is_decoded(lds, expect):
    if not _declined(lds, expect):
        expect.num(lds["heap decoded"], 50, "CONTROL: the heap table's 50 inserts ARE decoded")


def test_lds_columnar_emits_no_change(lds, expect):
    if not _declined(lds, expect):
        expect.num(lds["col decoded"], 0, "a columnar table emits no decodable change")


def test_lds_columnar_rows_are_there(lds, expect):
    if not _declined(lds, expect):
        expect.num(lds["col rows"], 50, "although the rows are there")


def test_lds_slot_carries_internal_catalog(lds, expect):
    if not _declined(lds, expect):
        n = lds["internal small"]
        expect.text("yes" if isinstance(n, int) and n > 0 else f"no ({n})", "yes",
                    "the slot carries pgcolumnar's internal catalog instead")


def test_lds_big_table_holds_its_rows(lds, expect):
    if not _declined(lds, expect):
        expect.text(lds["big rows"], "5000", "premise: the 100x table really holds 100x the rows")


def test_lds_second_slot_heap_control_is_decoded(lds, expect):
    if not _declined(lds, expect):
        expect.num(lds["heap big decoded"], 5000, "CONTROL: the second slot decodes the heap table's 5000 inserts")


def test_lds_big_columnar_emits_no_change(lds, expect):
    if not _declined(lds, expect):
        expect.num(lds["col big decoded"], 0, "and it too emits no decodable change of its own")


def test_lds_internal_records_do_not_follow_rows(lds, expect):
    if not _declined(lds, expect):
        small, big = lds["internal small"], lds["internal big"]
        ok = isinstance(small, int) and isinstance(big, int) and 0 < big < small * 10
        expect.text("yes" if ok else f"no ({small} then {big}; the second slot must carry some)", "yes",
                    "100x the rows does not give 100x the internal records")

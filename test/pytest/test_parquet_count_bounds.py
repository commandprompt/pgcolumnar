"""File-declared Parquet counts are bounded before use (#570, #571). The pytest twin of
`test/parquet_count_bounds.sh`, every check by the shell's name (#1341).

The reader takes a column chunk's num_values and a row group's num_rows off the wire.
A num_values of 2^62 used to wrap the decode arrays' size to zero, and a num_rows past a
chunk's values walked the row loop off the decoded arrays. Using the shell's crafter,
which patches those footer varints in an honest pyarrow file, this asserts: the honest
file imports its 8 rows; each malformed file is rejected as a malformed row group whose
detail names the count, without returning phantom rows; a repeated (LIST) column's
num_rows past its record count is rejected too, while a valid three-record list is not;
and no backend dies on the way.

THE BACKEND MUST NOT HAVE DIED, not merely answer now: a fresh connection answers
again after crash recovery, so the survival checks also require the server log's count
of killed backends not to have moved since setup. A phantom-row count is any count
above the file's true one, not only the one the crafter declares.

ON A PRIVATE DATABASE. The shell skips whole without pyarrow, so every test declines by
the shell's skip name first. The crafted files live in `server_dir`.
"""
import pathlib
import re
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parquet_count_bounds"
CRAFTER = pathlib.Path(__file__).resolve().parents[1] / "craft_parquet_counts.py"
FILES = ("honest", "bignv", "bignr", "hugenr", "list_honest", "list_valid3", "list_bad")


def _crashes(cluster):
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return sum(1 for line in text.splitlines() if "terminated by signal" in line)


def _alive(cluster, dsn, before):
    """-> "1" when the server answers and no backend died since setup, as the shell's alive()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            up = str(conn.execute("SELECT 1").fetchone()[0])
    except psycopg.Error as exc:
        up = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    now = _crashes(cluster)
    if not (isinstance(before, int) and isinstance(now, int)):
        return "no (crash count unreadable)"
    if up == "1" and now == before:
        return "1"
    return f"no ([{up}], {now - before} backend crashes)"


def _import(dsn, table, coltype, path):
    """-> the imported row count as text, or "ERR: <message>" plus "\\n<detail>"."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"CREATE TABLE {table} (a {coltype}) USING pgcolumnar")
            conn.execute(f"SELECT pgcolumnar.import_parquet('{table}', '{path}')")
            return str(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except psycopg.Error as exc:
        detail = getattr(getattr(exc, "diag", None), "message_detail", None) or ""
        return f"ERR: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}\n{detail}"


def _rejected(reading):
    return "rejected" if "malformed row group" in reading.lower() else reading.splitlines()[0]


def _leak(reading, true_rows):
    return f"leaked ({reading} rows)" if reading.isdigit() and int(reading) > true_rows else "ok"


@pytest.fixture(scope="module")
def pcb(pgc_cluster, server_dir):
    try:
        import pyarrow.parquet  # noqa: F401  the crafter needs it; tests decline without
    except ImportError:
        yield None
        return
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir / "pcb"
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        crafted = subprocess.run([sys.executable, str(CRAFTER), str(w)], capture_output=True, text=True)
        for f in w.iterdir():
            f.chmod(0o644)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        before = _crashes(pgc_cluster)
        missing = [f for f in FILES if not (w / f"{f}.parquet").is_file() or (w / f"{f}.parquet").stat().st_size == 0]
        r = {"crafted": "yes" if not missing else "missing: " + " ".join(missing),
             "crafter": crafted.stderr.strip().splitlines()[-1:] if crafted.returncode else [],
             "up before": _alive(pgc_cluster, dsn, before)}
        r["honest"] = _import(dsn, "pcb", "int", w / "honest.parquet")
        r["up honest"] = _alive(pgc_cluster, dsn, before)
        r["bignv"] = _import(dsn, "pcb", "int", w / "bignv.parquet")
        r["up bignv"] = _alive(pgc_cluster, dsn, before)
        r["bignr"] = _import(dsn, "pcb", "int", w / "bignr.parquet")
        r["hugenr"] = _import(dsn, "pcb", "int", w / "hugenr.parquet")
        r["up hugenr"] = _alive(pgc_cluster, dsn, before)
        r["list honest"] = _import(dsn, "pcl", "int[]", w / "list_honest.parquet")
        r["list valid3"] = _import(dsn, "pcl", "int[]", w / "list_valid3.parquet")
        r["list bad"] = _import(dsn, "pcl", "int[]", w / "list_bad.parquet")
        r["up list"] = _alive(pgc_cluster, dsn, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def test_the_crafter_produced_the_files(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(pcb["crafted"], "yes", "premise: the crafter produced the seven files")


def test_the_server_is_up_before_any_malformed_file(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(pcb["up before"], "1", "premise: the server is up before any malformed file is read")


def test_an_honest_file_imports_its_rows(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.num(_int(pcb["honest"]), 8, "an honest file imports its rows")


def test_the_server_is_up_after_the_honest_import(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(pcb["up honest"], "1", "the server is still up after the honest import")


def test_a_wrapping_num_values_is_rejected(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(_rejected(pcb["bignv"]), "rejected", "num_values 2^62 is rejected, not imported")


def test_the_backend_survives_the_wrapping_num_values(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(pcb["up bignv"], "1", "and the backend is still up (it did not wrap-allocate and crash)")


def test_num_rows_past_the_values_is_rejected(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(_rejected(pcb["bignr"]), "rejected",
                "num_rows past the chunk's values is rejected, not emitted as phantom rows")


def test_num_rows_past_the_values_returns_no_phantom_rows(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(_leak(pcb["bignr"], 8), "ok", "and it did not silently return more rows than the file holds")


def test_a_huge_num_rows_is_rejected(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(_rejected(pcb["hugenr"]), "rejected",
                "a huge num_rows is rejected rather than walking off the allocation")


def test_the_backend_survives_the_huge_num_rows(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(pcb["up hugenr"], "1", "and the backend survived it")


def test_the_num_values_rejection_names_num_values(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.num(1 if "num_values" in pcb["bignv"] else 0, 1,
               "the num_values rejection names num_values in its detail")


def test_the_num_rows_rejection_names_num_rows(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.num(1 if "num_rows" in pcb["bignr"] else 0, 1, "the num_rows rejection names num_rows in its detail")


def test_an_honest_list_imports_one_row(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.num(_int(pcb["list honest"]), 1, "an honest one-record list imports one row")


def test_a_valid_three_record_list_imports_three_rows(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.num(_int(pcb["list valid3"]), 3, "a valid three-record list imports three rows (not over-rejected)")


def test_num_rows_past_a_lists_records_is_rejected(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(_rejected(pcb["list bad"]), "rejected",
                "num_rows past a list's record count is rejected, not emitted as phantom rows")


def test_num_rows_past_a_lists_records_returns_no_phantom_rows(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(_leak(pcb["list bad"], 1), "ok", "and it did not silently return more rows than the file's records")


def test_the_backend_survives_the_list_over_read(pcb, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet count-bounds suite needs it") is None:
        return
    expect.text(pcb["up list"], "1", "and the backend survived the list over-read")

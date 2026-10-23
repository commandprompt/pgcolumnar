"""Streaming external-Parquet reads: a file over the old 1GB palloc ceiling describes
itself, the page loop refuses crafted pages cleanly, and the FDW decodes one row group
at a time. The pytest twin of `test/native_parquet_streaming.sh`, every check by the
shell's name.

Every file is written by `test/craft_parquet_streaming.py`, the shell's own crafter,
run as a subprocess (so this module imports no pyarrow) into `server_dir`. A refusal
must be the page loop's own ("could not decode Parquet column ..."), not any error.
Every reading waits for the server first, so a crash fails only its own arm, and each
survival check also reads the server log's count of killed backends since its
section began, since SELECT 1 answers again once crash recovery has finished.

ON A PRIVATE DATABASE. Without pyarrow every test declines under the shell's skip
name; on a box that cannot build the oversized sparse file, under the shell's other
skip name, before asking for the fixture's readings.
"""
import pathlib
import re
import subprocess
import sys
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_parquet_streaming"
CRAFTER = pathlib.Path(__file__).resolve().parent.parent / "craft_parquet_streaming.py"
NO_PYARROW = "pyarrow not available; streaming suite needs it"
NO_SPARSE = "could not build the oversized sparse file"
DECODE = "could not decode Parquet column "


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _wait_up(dsn):
    for _ in range(60):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _rows(dsn, sql, sep="|"):
    """-> every row joined as the shell's q prints them (columns by `sep`, rows by
    newline), or the failure as a query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join(sep.join("" if v is None else str(v) for v in row) for row in rows)


def _exec(dsn, sql):
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return None


def _errtext(dsn, sql, timeout=None):
    """-> "ERROR:  <message>" for the error SQL raised, as psql's first ERROR line, or
    NO ERROR; TIMED OUT when a statement_timeout cancelled it; a lost connection as a
    query_error()."""
    _wait_up(dsn)
    caught = None
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if timeout:
                conn.execute(f"SET statement_timeout = '{timeout}s'")
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        caught = exc
    if caught is None:
        return "NO ERROR"
    if isinstance(caught, psycopg.errors.QueryCanceled):
        return "TIMED OUT (page loop made no progress)"
    if not caught.sqlstate:
        return query_error("no refusal: the connection was lost")
    return "ERROR:  " + (caught.diag.message_primary or "")


def _decodes(dsn, sql):
    """-> OK when SQL raised the page loop's refusal, else what it did raise."""
    out = _errtext(dsn, sql)
    return "OK" if isinstance(out, str) and out.startswith("ERROR:  " + DECODE) else out


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    """-> "1" when the server answers and no backend died since `before`."""
    _wait_up(dsn)
    up, now = _rows(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    return f"no ([{up if up == '1' else ''}], {now - before} backend crashes)"


def _decoded_for(dsn, sql):
    """-> the plan's Row Groups Decoded count, or what was read instead."""
    plan = _rows(dsn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}")
    m = re.search(r"Row Groups Decoded: (\d+)", plan) if isinstance(plan, str) else None
    return m.group(1) if m else plan


@pytest.fixture(scope="module")
def pqs(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    w = server_dir / "pqs"
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        rc = subprocess.run([sys.executable, str(CRAFTER), str(w)],
                            capture_output=True, text=True).returncode
        for f in w.glob("*.parquet"):
            f.chmod(0o644)
        r = {"sparse": rc not in (3, 4), "crafted": "built" if rc == 0 else f"build failed (exit {rc})"}
        if rc != 0:
            yield r
            return
        _exec(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        before = _crashes(pgc_cluster)
        r["schema big"] = _rows(dsn, f"SELECT count(*) FROM pgcolumnar.parquet_schema('{w}/huge_sparse.parquet')")
        r["schema cols"] = _rows(dsn, f"SELECT string_agg(column_name, ',' ORDER BY column_name) "
                                      f"FROM pgcolumnar.parquet_schema('{w}/huge_sparse.parquet')")
        r["sparse read"] = _decodes(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/huge_sparse.parquet') "
                                         f"AS t(id int, v text)")
        r["survived read"] = _survived(dsn, pgc_cluster, before)
        mid = _crashes(pgc_cluster)
        r["schema small"] = _rows(dsn, f"SELECT count(*) FROM pgcolumnar.parquet_schema('{w}/small.parquet')")
        r["read small"] = _rows(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet('{w}/small.parquet') "
                                     f"AS t(id int, v text)")
        zero = _errtext(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet('{w}/zero_pages.parquet') "
                             f"AS t(id int)", timeout=60)
        r["zero"] = "CLEAN REJECT" if isinstance(zero, str) and zero.startswith("ERROR:  " + DECODE) else zero
        r["bad size"] = _errtext(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/bad_size.parquet') AS t(id int)")
        r["bad offset"] = _errtext(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/bad_offset.parquet') "
                                        f"AS t(id int, v text)")
        r["noise"] = _decodes(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/noise_pages.parquet') AS t(id int)")
        r["two dicts"] = _errtext(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/two_dicts.parquet') AS t(id int)")
        r["v2"] = _errtext(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/v2_levels.parquet') AS t(id int)")
        r["allnull"] = _rows(dsn, f"SELECT count(*), count(c) FROM pgcolumnar.read_parquet('{w}/allnull.parquet') "
                                  f"AS t(c int)", sep=" ")
        r["survived crafted"] = _survived(dsn, pgc_cluster, mid)
        r["plain"] = _rows(dsn, f"SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('{w}/plain.parquet') "
                                f"AS t(id int)", sep=" ")
        for stmt in ("CREATE SERVER pqs FOREIGN DATA WRAPPER pgcolumnar_parquet",
                     f"CREATE FOREIGN TABLE fts (id int4, v int8) SERVER pqs "
                     f"OPTIONS (path '{w}/stream_groups.parquet')",
                     "CREATE TABLE outer2 (k int)", "INSERT INTO outer2 VALUES (1),(2)"):
            _exec(dsn, stmt)
        r["full"] = _rows(dsn, "SELECT count(*), sum(id), sum(v) FROM fts", sep=" ")
        r["dec all"] = _decoded_for(dsn, "SELECT * FROM fts")
        r["dec 10"] = _decoded_for(dsn, "SELECT * FROM fts LIMIT 10")
        r["dec 1010"] = _decoded_for(dsn, "SELECT * FROM fts LIMIT 1010")
        _wait_up(dsn)
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("SET enable_hashjoin = off")
                conn.execute("SET enable_mergejoin = off")
                r["rescan"] = str(conn.execute(
                    "SELECT count(*) FROM outer2 o JOIN fts ON fts.id = o.k").fetchone()[0])
        except psycopg.Error as exc:
            r["rescan"] = query_error(_message(exc))
        yield r
    finally:
        for f in w.glob("*.parquet"):
            f.unlink(missing_ok=True)
        _wait_up(pgc_cluster.dsn())
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _readings(request, expect):
    """-> the fixture's readings, or None after declining as the shell skips."""
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; streaming suite needs it") is None:
        return None
    r = request.getfixturevalue("pqs")
    if not r["sparse"]:
        expect.cannot_run("UNMET_PRECONDITION", "the box cannot build a 1600MB sparse file",
                          name="could not build the oversized sparse file")
        return None
    return r


def test_parquet_streaming_crafted_files_were_built(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["crafted"], "built", "crafted files were built")


def test_parquet_streaming_schema_reads_a_file_over_the_palloc_ceiling(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("schema big", r["crafted"]), "2", "parquet_schema reads a file over the 1GB palloc ceiling")


def test_parquet_streaming_schema_reports_the_oversized_files_columns(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("schema cols", r["crafted"]), "id,v",
                    "parquet_schema reports the columns of the oversized file")


def test_parquet_streaming_value_read_of_the_sparse_file_fails_cleanly(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("sparse read", r["crafted"]), "OK", "value read of the sparse file fails cleanly")


def test_parquet_streaming_backend_survived_the_failed_read(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("survived read", r["crafted"]), "1", "backend survived the failed read")


def test_parquet_streaming_schema_still_reads_a_normal_file(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("schema small", r["crafted"]), "2", "parquet_schema still reads a normal file")


def test_parquet_streaming_read_parquet_still_reads_a_normal_file(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("read small", r["crafted"]), "3", "read_parquet still reads a normal file")


def test_parquet_streaming_zero_filled_page_area_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("zero", r["crafted"]), "CLEAN REJECT",
                    "zero-filled page area is rejected, not walked byte by byte")


def test_parquet_streaming_page_size_past_eof_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("bad size", r["crafted"]), "ERROR:  could not decode Parquet column 0 in row group 0",
                    "page size past end of file is rejected by the size bound")


def test_parquet_streaming_negative_page_offset_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("bad offset", r["crafted"]), "ERROR:  could not decode Parquet column 1 in row group 0",
                    "negative page offset is rejected by the offset check")


def test_parquet_streaming_unparseable_page_header_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("noise", r["crafted"]), "OK", "unparseable page header is rejected")


def test_parquet_streaming_second_dictionary_page_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("two dicts", r["crafted"]), "ERROR:  could not decode Parquet column 0 in row group 0",
                    "a second dictionary page in a chunk is rejected")


def test_parquet_streaming_v2_level_lengths_past_the_page_are_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("v2", r["crafted"]), "ERROR:  could not decode Parquet column 0 in row group 0",
                    "v2 level lengths past the page are rejected")


def test_parquet_streaming_all_null_column_still_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("allnull", r["crafted"]), "100 0", "all-null column (empty dictionary page) still reads")


def test_parquet_streaming_backend_survived_the_crafted_page_areas(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("survived crafted", r["crafted"]), "1", "backend survived the crafted page areas")


def test_parquet_streaming_uncompressed_source_file_still_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("plain", r["crafted"]), "200 19900", "the uncompressed source file still reads")


def test_parquet_streaming_full_scan_content_is_correct(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("full", r["crafted"]), "4000 7998000 15996000", "streaming: full scan content is correct")


def test_parquet_streaming_full_scan_decodes_all_four_row_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("dec all", r["crafted"]), "4", "streaming: a full scan decodes all four row groups")


def test_parquet_streaming_limit_10_decodes_one_row_group(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("dec 10", r["crafted"]), "1",
                    "streaming: LIMIT 10 (inside group 0) decodes one row group")


def test_parquet_streaming_limit_1010_decodes_two_row_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("dec 1010", r["crafted"]), "2",
                    "streaming: LIMIT 1010 (into group 1) decodes two row groups")


def test_parquet_streaming_rescan_under_a_nested_loop_is_correct(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r.get("rescan", r["crafted"]), "2", "streaming: rescan under a nested loop is correct")

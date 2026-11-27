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
REFUSED = "ERROR:  XX001: could not decode Parquet column 0 in row group 0"


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


def _errcode(dsn, sql):
    """-> "ERROR:  <SQLSTATE>: <message>" for the error SQL raised, as psql prints it
    with VERBOSITY verbose, or NO ERROR. The page loop's refusals are documented as
    XX001, so a check on one asserts the code as well as the text."""
    _wait_up(dsn)
    caught = None
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        caught = exc
    if caught is None:
        return "NO ERROR"
    if not caught.sqlstate:
        return query_error("no refusal: the connection was lost")
    return "ERROR:  " + caught.sqlstate + ": " + (caught.diag.message_primary or "")


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
        r["empty page"] = _rows(dsn, f"SELECT string_agg(coalesce(ls::text, 'NULL'), ' ') FROM "
                                     f"pgcolumnar.read_parquet('{w}/empty_data_page.parquet') AS t(ls text[])")
        r["empty snappy"] = _rows(dsn, f"SELECT string_agg(coalesce(li::text, 'NULL'), ' ') FROM "
                                       f"pgcolumnar.read_parquet('{w}/empty_page_snappy.parquet') AS t(li bigint[])")
        for key, name in (("one empty", "one_empty_page"), ("run16", "empty_run16"),
                          ("runs 10 10", "empty_runs_10_10"), ("nobody v1 req", "nobody_v1_req"),
                          ("nobody v2", "nobody_v2"), ("index page", "index_page"),
                          ("index run16", "index_run16"), ("unknown type", "unknown_type_page"),
                          ("mix dict0 200", "mix_dict0_200"), ("mix v2full 200", "mix_v2full_200"),
                          ("v2 ok req", "v2_ok_req"), ("mix dictnv0", "mix_dictnv0"),
                          ("mix v2zero", "mix_v2zero"), ("p enc v2dict 200", "p_enc_v2dict_200"),
                          ("dict v1nv0", "dict_v1nv0"), ("p mixrun 8e 8i", "p_mixrun_8e_8i"),
                          ("a v2run16", "a_v2run16"), ("a other8x2 200", "a_other8x2_200"),
                          ("a other5x2 v2 200", "a_other5x2_v2_200"),
                          ("e oth8 r1 200", "e_oth8_r1_200"), ("e oth5 r1 v2 200", "e_oth5_r1_v2_200"),
                          ("x fid32767 v1", "x_fid32767_v1_200"), ("b unkm32768 v1", "b_unkm32768_v1_200")):
            r[key] = _rows(dsn, f"SELECT count(*), sum(id) FROM "
                                f"pgcolumnar.read_parquet('{w}/{name}.parquet') AS t(id int)", sep=" ")
        for key, name in (("empty run", "empty_page_run"), ("run17", "empty_run17"),
                          ("nobody v1 opt", "nobody_v1_opt"), ("garbage body", "garbage_body"),
                          ("index run17", "index_run17"), ("nohdr loss", "nohdr_loss"),
                          ("nv missing", "nv_missing"), ("hdr5 empty", "hdr5empty_loss"),
                          ("hdr8 empty", "hdr8empty_loss"), ("nohdr v2", "nohdr_v2_loss_req"),
                          ("negnv", "negnv"), ("nv2 missing", "nv2_missing_req"), ("dicthdr only", "dicthdr_only"),
                          ("dicthdr zero", "dicthdr_zero_loss"), ("idx negcsz", "idx_negcsz"),
                          ("p dup5 nv0", "p_dup5_nv0"), ("p dup8 nv0", "p_dup8_nv0"),
                          ("p only8 type0", "p_only8_type0"), ("p only5 type3", "p_only5_type3"),
                          ("p empty5 full8", "p_empty5_full8"), ("p mixrun 9e 8i", "p_mixrun_9e_8i"),
                          ("f rep1 v1 150", "f_rep1_v1_150"), ("f rep1 v2 150", "f_rep1_v2_150"),
                          ("a dup5 nv10 110", "a_dup5_nv10_110"), ("a dup8 nv10 110", "a_dup8_nv10_110"),
                          ("a v2run17", "a_v2run17"), ("a v2neg opt 200", "a_v2neg_opt_200"),
                          ("e r1v1 50 10 110", "e_r1v1_50_10_110"), ("e r1v2 50 10 110", "e_r1v2_50_10_110"),
                          ("x fid65537 v1", "x_fid65537_v1_150"), ("x fid65537 v2", "x_fid65537_v2_req150"),
                          ("x fidm65535 v1", "x_fidm65535_v1_150"), ("x fidm65535 v2", "x_fidm65535_v2_req150"),
                          ("x fid32769 short v1", "x_fid32769_short_v1_200"), ("x run16 dict 16", "x_run16_dict_16"),
                          ("y ph65537 type", "y_ph65537_type_150"), ("y phm65535 type", "y_phm65535_type_150"),
                          ("y ph32768 type", "y_ph32768_type_150"),
                          ("y phm32769 type", "y_phm32769_type_150"),
                          ("y ph2147483648 type", "y_ph2147483648_type_150"),
                          ("y ph4294967297 type", "y_ph4294967297_type_150"),
                          ("y phm4294967295 type", "y_phm4294967295_type_150"),
                          ("y phm4294967297 type", "y_phm4294967297_type_150"),
                          ("y ph1099511627777 type", "y_ph1099511627777_type_150"),
                          ("b unk32768 v1", "b_unk32768_v1_200"), ("b unkm32769 v1", "b_unkm32769_v1_200")):
            r[key] = _errcode(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/{name}.parquet') AS t(id int)")
        r["alt20"] = _rows(dsn, f"SELECT count(*), count(ls), sum(cardinality(ls)) FROM "
                                f"pgcolumnar.read_parquet('{w}/alt20.parquet') AS t(ls text[])", sep=" ")
        r["v2 list"] = _rows(dsn, f"SELECT count(*), count(ls), sum(cardinality(ls)), sum(ls[1]) FROM "
                                  f"pgcolumnar.read_parquet('{w}/v2_list.parquet') AS t(ls int[])", sep=" ")
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


def _got(r, key):
    """-> the reading under key. If the crafter failed, every check shows why
    instead; a key missing after a good build fails loudly (KeyError)."""
    return r[key] if r["crafted"] == "built" else r["crafted"]


def test_parquet_streaming_crafted_files_were_built(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["crafted"], "built", "crafted files were built")


def test_parquet_streaming_schema_reads_a_file_over_the_palloc_ceiling(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "schema big"), "2", "parquet_schema reads a file over the 1GB palloc ceiling")


def test_parquet_streaming_schema_reports_the_oversized_files_columns(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "schema cols"), "id,v",
                    "parquet_schema reports the columns of the oversized file")


def test_parquet_streaming_value_read_of_the_sparse_file_fails_cleanly(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "sparse read"), "OK", "value read of the sparse file fails cleanly")


def test_parquet_streaming_backend_survived_the_failed_read(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "survived read"), "1", "backend survived the failed read")


def test_parquet_streaming_schema_still_reads_a_normal_file(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "schema small"), "2", "parquet_schema still reads a normal file")


def test_parquet_streaming_read_parquet_still_reads_a_normal_file(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "read small"), "3", "read_parquet still reads a normal file")


def test_parquet_streaming_zero_filled_page_area_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "zero"), "CLEAN REJECT",
                    "zero-filled page area is rejected, not walked byte by byte")


def test_parquet_streaming_page_size_past_eof_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "bad size"), "ERROR:  could not decode Parquet column 0 in row group 0",
                    "page size past end of file is rejected by the size bound")


def test_parquet_streaming_negative_page_offset_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "bad offset"), "ERROR:  could not decode Parquet column 1 in row group 0",
                    "negative page offset is rejected by the offset check")


def test_parquet_streaming_unparseable_page_header_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "noise"), "OK", "unparseable page header is rejected")


def test_parquet_streaming_second_dictionary_page_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "two dicts"), "ERROR:  could not decode Parquet column 0 in row group 0",
                    "a second dictionary page in a chunk is rejected")


def test_parquet_streaming_v2_level_lengths_past_the_page_are_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "v2"), "ERROR:  could not decode Parquet column 0 in row group 0",
                    "v2 level lengths past the page are rejected")


def test_parquet_streaming_all_null_column_still_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "allnull"), "100 0", "all-null column (empty dictionary page) still reads")


def test_parquet_streaming_data_page_with_no_values_reads(request, expect):
    """#2077: pyarrow writes a data page with no values for a dictionary-encoded list
    column when it flushes pages often. The page loop refused every such page."""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "empty page"), "{yy} NULL",
                    "a data page with no values (pyarrow list column) reads")


def test_parquet_streaming_data_page_with_no_values_snappy_reads(request, expect):
    """#2077 acceptance: the compressed variant, a dictionary list<int64> column."""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "empty snappy"), "{1,2} NULL {3} {}",
                    "a data page with no values (snappy list<int64> column) reads")


def test_parquet_streaming_one_empty_data_page_between_data_pages_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "one empty"), "200 19900",
                    "one empty data page between two data pages reads")


def test_parquet_streaming_run_of_empty_data_pages_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "empty run"), REFUSED, "a run of 1000 empty data pages is rejected")


def test_parquet_streaming_run_of_16_empty_data_pages_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "run16"), "200 19900", "a run of 16 empty data pages reads")


def test_parquet_streaming_run_of_17_empty_data_pages_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "run17"), REFUSED, "a run of 17 empty data pages is rejected")


def test_parquet_streaming_two_runs_of_10_empty_data_pages_read(request, expect):
    """a page with values resets the run: the limit is 16 in a row, not 16 per chunk"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "runs 10 10"), "200 19900",
                    "two runs of 10 empty data pages read (the limit is per run)")


def test_parquet_streaming_empty_v1_page_without_body_reads_when_required(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "nobody v1 req"), "200 19900",
                    "an empty v1 page with no body reads on a REQUIRED column")


def test_parquet_streaming_empty_v2_page_without_body_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "nobody v2"), "200 19900", "an empty v2 page with no body reads")


def test_parquet_streaming_empty_v1_page_without_body_is_rejected_when_nullable(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "nobody v1 opt"), REFUSED,
                    "an empty v1 page with no body is rejected on a nullable column")


def test_parquet_streaming_empty_data_page_with_garbage_body_is_rejected(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "garbage body"), REFUSED,
                    "an empty data page with a garbage body is rejected")


def test_parquet_streaming_index_page_is_stepped_over(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "index page"), "200 19900",
                    "an INDEX_PAGE between data pages is stepped over")


def test_parquet_streaming_run_of_16_index_pages_reads(request, expect):
    """the run limit is the same for INDEX_PAGEs as for empty data pages: 16 read"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "index run16"), "200 19900", "a run of 16 INDEX_PAGEs reads")


def test_parquet_streaming_run_of_17_index_pages_is_rejected(request, expect):
    """an INDEX_PAGE holds no values, so it counts toward the run limit"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "index run17"), REFUSED, "a run of 17 INDEX_PAGEs is rejected")


def test_parquet_streaming_data_page_without_header_is_rejected(request, expect):
    """skipping the page would read 150 rows with ids 50..99 silently gone"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "nohdr loss"), REFUSED,
                    "a DATA_PAGE with no DataPageHeader is rejected, not skipped")


def test_parquet_streaming_data_page_header_without_num_values_is_rejected(request, expect):
    """decoding it as an empty page would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "nv missing"), REFUSED,
                    "a DataPageHeader without num_values is rejected")


def test_parquet_streaming_empty_data_page_header_struct_is_rejected(request, expect):
    """decoding it as an empty page would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "hdr5 empty"), REFUSED,
                    "an empty DataPageHeader struct is rejected")


def test_parquet_streaming_empty_data_page_header_v2_struct_is_rejected(request, expect):
    """decoding it as an empty page would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "hdr8 empty"), REFUSED,
                    "an empty DataPageHeaderV2 struct is rejected")


def test_parquet_streaming_data_page_v2_without_header_is_rejected(request, expect):
    """decoding it as an empty v1 page would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "nohdr v2"), REFUSED,
                    "a DATA_PAGE_V2 with no DataPageHeaderV2 is rejected")


def test_parquet_streaming_negative_num_values_is_rejected(request, expect):
    """without the check, the reader fails with an internal XX000 from a wrapped size"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "negnv"), REFUSED,
                    "a data page with a negative num_values is rejected")


def test_parquet_streaming_unknown_page_type_is_stepped_over(request, expect):
    """a page type no Parquet version defines holds no values; pyarrow steps over it"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "unknown type"), "200 19900", "a page of an unknown type is stepped over")


def test_parquet_streaming_dictionary_header_does_not_overwrite_data_page_count(request, expect):
    """page 2 keeps its count of 50: 150 rows are ids 0..149; a count of 0 drops ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "mix dictnv0"), "150 11175",
                    "a DictionaryPageHeader after a DataPageHeader does not overwrite its count")


def test_parquet_streaming_data_page_v2_header_does_not_overwrite_data_page_count(request, expect):
    """page 2 keeps its count of 50: 150 rows are ids 0..149; a count of 0 drops ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "mix v2zero"), "150 11175",
                    "a DataPageHeaderV2 after a DataPageHeader does not overwrite its count")


def test_parquet_streaming_data_page_count_ignores_a_dictionary_header(request, expect):
    """pyarrow uses the struct of the page's own type and reads 200 rows"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "mix dict0 200"), "200 19900",
                    "a DATA_PAGE takes its count from its DataPageHeader, not a DictionaryPageHeader")


def test_parquet_streaming_data_page_count_ignores_a_data_page_v2_header(request, expect):
    """pyarrow uses the struct of the page's own type and reads 200 rows"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "mix v2full 200"), "200 19900",
                    "a DATA_PAGE takes its count from its DataPageHeader, not a DataPageHeaderV2")


def test_parquet_streaming_data_page_v2_with_values_reads(request, expect):
    """the control for the DataPageHeaderV2 refusal below"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "v2 ok req"), "200 19900",
                    "a DATA_PAGE_V2 with values reads")


def test_parquet_streaming_data_page_v2_header_without_num_values_is_rejected(request, expect):
    """decoding it as an empty page would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "nv2 missing"), REFUSED,
                    "a DataPageHeaderV2 without num_values is rejected")


def test_parquet_streaming_data_page_with_only_a_dictionary_header_is_rejected(request, expect):
    """a dictionary page's count is not a data page's"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "dicthdr only"), REFUSED,
                    "a DATA_PAGE with only a DictionaryPageHeader is rejected")


def test_parquet_streaming_data_page_with_only_an_empty_dictionary_header_is_rejected(request, expect):
    """taking the dictionary count would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "dicthdr zero"), REFUSED,
                    "a DATA_PAGE with only an empty-count DictionaryPageHeader is rejected")


def test_parquet_streaming_index_page_with_negative_compressed_size_is_rejected(request, expect):
    """stepping over it first would re-read page 1: ids 0..49 four times"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "idx negcsz"), REFUSED,
                    "an INDEX_PAGE with a negative compressed_size is rejected")


def test_parquet_streaming_run_of_one_value_pages_reads(request, expect):
    """a page with one value resets the run of empty pages"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "alt20"), "40 20 20",
                    "a run of one-value pages reads (pyarrow list column, 41 pages)")


def test_parquet_streaming_repeated_data_page_header_is_rejected(request, expect):
    """a second DataPageHeader with num_values 0 would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p dup5 nv0"), REFUSED,
                    "a repeated DataPageHeader with num_values 0 is rejected")


def test_parquet_streaming_repeated_data_page_v2_header_is_rejected(request, expect):
    """a second DataPageHeaderV2 with num_values 0 would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p dup8 nv0"), REFUSED,
                    "a repeated DataPageHeaderV2 with num_values 0 is rejected")


def test_parquet_streaming_data_page_with_only_a_v2_header_is_rejected(request, expect):
    """a DataPageHeaderV2 is not a DATA_PAGE's own header"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p only8 type0"), REFUSED,
                    "a DATA_PAGE with only a DataPageHeaderV2 is rejected")


def test_parquet_streaming_data_page_v2_with_only_a_v1_header_is_rejected(request, expect):
    """a DataPageHeader is not a DATA_PAGE_V2's own header"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p only5 type3"), REFUSED,
                    "a DATA_PAGE_V2 with only a DataPageHeader is rejected")


def test_parquet_streaming_v2_count_does_not_stand_in_for_an_empty_v1_header(request, expect):
    """the page's own struct is empty, so the page is refused"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p empty5 full8"), REFUSED,
                    "a DATA_PAGE with an empty DataPageHeader and a full DataPageHeaderV2 is rejected")


def test_parquet_streaming_data_page_encoding_ignores_a_data_page_v2_header(request, expect):
    """the v2 struct says RLE_DICTIONARY; the page is PLAIN, as its own struct says"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p enc v2dict 200"), "200 19900",
                    "a DATA_PAGE takes its encoding from its DataPageHeader, not a DataPageHeaderV2")


def test_parquet_streaming_dictionary_count_ignores_a_data_page_header(request, expect):
    """a DataPageHeader with num_values 0 in a dictionary page's header is ignored"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "dict v1nv0"), "200 900",
                    "a dictionary page takes its count from its DictionaryPageHeader, not a DataPageHeader")


def test_parquet_streaming_empty_and_index_pages_share_one_run(request, expect):
    """9 empty data pages and 8 INDEX_PAGEs in a row are 17 pages"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p mixrun 9e 8i"), REFUSED,
                    "9 empty data pages and 8 INDEX_PAGEs in a row are rejected (one shared run)")


def test_parquet_streaming_8_empty_and_8_index_pages_read(request, expect):
    """16 pages in a row is the limit"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "p mixrun 8e 8i"), "200 19900",
                    "8 empty data pages and 8 INDEX_PAGEs in a row read")


def test_parquet_streaming_data_page_header_that_repeats_num_values_is_rejected(request, expect):
    """a second num_values 0 in the same DataPageHeader would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "f rep1 v1 150"), REFUSED,
                    "a DataPageHeader that repeats num_values is rejected")


def test_parquet_streaming_data_page_v2_header_that_repeats_num_values_is_rejected(request, expect):
    """a second num_values 0 in the same DataPageHeaderV2 would drop ids 50..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "f rep1 v2 150"), REFUSED,
                    "a DataPageHeaderV2 that repeats num_values is rejected")


def test_parquet_streaming_repeated_complete_data_page_header_is_rejected(request, expect):
    """a second, complete DataPageHeader with num_values 10 is refused, not only one with 0"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "a dup5 nv10 110"), REFUSED,
                    "a repeated complete DataPageHeader is rejected")


def test_parquet_streaming_repeated_complete_data_page_v2_header_is_rejected(request, expect):
    """a second, complete DataPageHeaderV2 with num_values 10 is refused, not only one with 0"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "a dup8 nv10 110"), REFUSED,
                    "a repeated complete DataPageHeaderV2 is rejected")


def test_parquet_streaming_run_of_16_empty_v2_pages_reads(request, expect):
    """16 empty DATA_PAGE_V2 pages in a row is the limit"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "a v2run16"), "200 19900",
                    "a run of 16 empty DATA_PAGE_V2 pages reads")


def test_parquet_streaming_run_of_17_empty_v2_pages_is_rejected(request, expect):
    """empty DATA_PAGE_V2 pages count toward the run of empty pages"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "a v2run17"), REFUSED,
                    "a run of 17 empty DATA_PAGE_V2 pages is rejected")


def test_parquet_streaming_data_page_with_two_v2_headers_besides_its_own_reads(request, expect):
    """a struct of the other version is ignored, even when it appears twice"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "a other8x2 200"), "200 19900",
                    "a DATA_PAGE with two DataPageHeaderV2 structs besides its own reads")


def test_parquet_streaming_data_page_v2_with_two_v1_headers_besides_its_own_reads(request, expect):
    """a struct of the other version is ignored, even when it appears twice"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "a other5x2 v2 200"), "200 19900",
                    "a DATA_PAGE_V2 with two DataPageHeader structs besides its own reads")


def test_parquet_streaming_data_page_v2_with_a_negative_num_values_is_rejected(request, expect):
    """the v2 twin of negnv, on a nullable column"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "a v2neg opt 200"), REFUSED,
                    "a DATA_PAGE_V2 with a negative num_values is rejected")


def test_parquet_streaming_data_page_header_that_repeats_num_values_ending_nonzero_is_rejected(request, expect):
    """num_values 50 then 10 would drop ids 60..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "e r1v1 50 10 110"), REFUSED,
                    "a DataPageHeader that repeats num_values with a nonzero last value is rejected")


def test_parquet_streaming_data_page_v2_header_that_repeats_num_values_ending_nonzero_is_rejected(request, expect):
    """num_values 50 then 10 would drop ids 60..99"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "e r1v2 50 10 110"), REFUSED,
                    "a DataPageHeaderV2 that repeats num_values with a nonzero last value is rejected")


def test_parquet_streaming_data_page_header_with_field_id_65537_is_rejected(request, expect):
    """Thrift field ids are i16; kept whole, the id hid a num_values 50"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "x fid65537 v1"), REFUSED,
                    "a DataPageHeader with field id 65537 is rejected")


def test_parquet_streaming_data_page_v2_header_with_field_id_65537_is_rejected(request, expect):
    """Thrift field ids are i16; kept whole, the id hid a num_values 50"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "x fid65537 v2"), REFUSED,
                    "a DataPageHeaderV2 with field id 65537 is rejected")


def test_parquet_streaming_data_page_header_with_field_id_minus_65535_is_rejected(request, expect):
    """Thrift field ids are i16; kept whole, the id hid a num_values 50"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "x fidm65535 v1"), REFUSED,
                    "a DataPageHeader with field id -65535 is rejected")


def test_parquet_streaming_data_page_v2_header_with_field_id_minus_65535_is_rejected(request, expect):
    """Thrift field ids are i16; kept whole, the id hid a num_values 50"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "x fidm65535 v2"), REFUSED,
                    "a DataPageHeaderV2 with field id -65535 is rejected")


def test_parquet_streaming_data_page_header_with_field_id_32769_from_a_delta_is_rejected(request, expect):
    """a short-form delta past 32767 leaves int16 too"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "x fid32769 short v1"), REFUSED,
                    "a DataPageHeader with field id 32769 from a short-form delta is rejected")


def test_parquet_streaming_data_page_header_with_unknown_field_id_32767_reads(request, expect):
    """32767 is the largest field id; an unknown field with it is skipped"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "x fid32767 v1"), "200 19900",
                    "a DataPageHeader with an unknown field id 32767 reads")


def test_parquet_streaming_data_page_whose_v2_header_repeats_num_values_reads(request, expect):
    """a struct of the other version is ignored, whatever it holds"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "e oth8 r1 200"), "200 19900",
                    "a DATA_PAGE whose DataPageHeaderV2 repeats num_values reads")


def test_parquet_streaming_data_page_v2_whose_v1_header_repeats_num_values_reads(request, expect):
    """a struct of the other version is ignored, whatever it holds"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "e oth5 r1 v2 200"), "200 19900",
                    "a DATA_PAGE_V2 whose DataPageHeader repeats num_values reads")


def test_parquet_streaming_dictionary_page_does_not_reset_the_empty_run(request, expect):
    """16 empty pages, a dictionary page and 16 more are 32 in a row"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "x run16 dict 16"), REFUSED,
                    "16 empty data pages, a dictionary page and 16 more are rejected (one run)")


def test_parquet_streaming_page_header_with_long_form_field_id_65537_is_rejected(request, expect):
    """narrowed to field 1 (the page type), the page would be stepped over and ids 50..99 lost"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y ph65537 type"), REFUSED,
                    "a PageHeader with long-form field id 65537 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_minus_65535_is_rejected(request, expect):
    """narrowed to field 1 (the page type), the page would be stepped over and ids 50..99 lost"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y phm65535 type"), REFUSED,
                    "a PageHeader with long-form field id -65535 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_32768_is_rejected(request, expect):
    """a wide id is refused, whatever width a narrowing reader would use"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y ph32768 type"), REFUSED,
                    "a PageHeader with long-form field id 32768 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_minus_32769_is_rejected(request, expect):
    """a wide id is refused, whatever width a narrowing reader would use"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y phm32769 type"), REFUSED,
                    "a PageHeader with long-form field id -32769 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_2147483648_is_rejected(request, expect):
    """a wide id is refused, whatever width a narrowing reader would use"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y ph2147483648 type"), REFUSED,
                    "a PageHeader with long-form field id 2147483648 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_4294967297_is_rejected(request, expect):
    """a wide id is refused, whatever width a narrowing reader would use"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y ph4294967297 type"), REFUSED,
                    "a PageHeader with long-form field id 4294967297 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_minus_4294967295_is_rejected(request, expect):
    """a wide id is refused, whatever width a narrowing reader would use"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y phm4294967295 type"), REFUSED,
                    "a PageHeader with long-form field id -4294967295 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_minus_4294967297_is_rejected(request, expect):
    """a wide id is refused, whatever width a narrowing reader would use"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y phm4294967297 type"), REFUSED,
                    "a PageHeader with long-form field id -4294967297 is rejected")


def test_parquet_streaming_page_header_with_long_form_field_id_1099511627777_is_rejected(request, expect):
    """a wide id is refused, whatever width a narrowing reader would use"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "y ph1099511627777 type"), REFUSED,
                    "a PageHeader with long-form field id 1099511627777 is rejected")


def test_parquet_streaming_data_page_header_with_unknown_field_id_32768_is_rejected(request, expect):
    """32768 is one past int16"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "b unk32768 v1"), REFUSED,
                    "a DataPageHeader with an unknown field id 32768 is rejected")


def test_parquet_streaming_data_page_header_with_unknown_field_id_minus_32768_reads(request, expect):
    """-32768 is the smallest int16; an unknown field with it is skipped"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "b unkm32768 v1"), "200 19900",
                    "a DataPageHeader with an unknown field id -32768 reads")


def test_parquet_streaming_data_page_header_with_unknown_field_id_minus_32769_is_rejected(request, expect):
    """-32769 is one below int16"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "b unkm32769 v1"), REFUSED,
                    "a DataPageHeader with an unknown field id -32769 is rejected")


def test_parquet_streaming_data_page_v2_on_a_list_column_reads(request, expect):
    """a v2 page of a repeated column carries a repetition-level length"""
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "v2 list"), "300 200 400 30000",
                    "a DATA_PAGE_V2 on a list column reads")


def test_parquet_streaming_backend_survived_the_crafted_page_areas(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "survived crafted"), "1", "backend survived the crafted page areas")


def test_parquet_streaming_uncompressed_source_file_still_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "plain"), "200 19900", "the uncompressed source file still reads")


def test_parquet_streaming_full_scan_content_is_correct(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "full"), "4000 7998000 15996000", "streaming: full scan content is correct")


def test_parquet_streaming_full_scan_decodes_all_four_row_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "dec all"), "4", "streaming: a full scan decodes all four row groups")


def test_parquet_streaming_limit_10_decodes_one_row_group(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "dec 10"), "1",
                    "streaming: LIMIT 10 (inside group 0) decodes one row group")


def test_parquet_streaming_limit_1010_decodes_two_row_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "dec 1010"), "2",
                    "streaming: LIMIT 1010 (into group 1) decodes two row groups")


def test_parquet_streaming_rescan_under_a_nested_loop_is_correct(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(_got(r, "rescan"), "2", "streaming: rescan under a nested loop is correct")

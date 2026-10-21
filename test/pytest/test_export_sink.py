"""The export byte sink checks every write and never shows a partial file under the
final name (#394 step 1). The pytest twin of `test/export_sink.sh`, every check by the
shell's name (#1341).

No fwrite call remains in either export file, so the count is a property of the tree.
Parquet and arrow exports of a 40,000-row table import back hash-equal to the source,
leaving no `*.tmp.*` debris. With `pgcolumnar.sink_fail_after` injecting the error a
full disk raises, each export fails 53100, nothing exists at its final path, and no
temp file remains; the arrow arm fails inside write_record_batch by arithmetic on the
measured full size. A fresh session exports again.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). The files live in `server_dir`, because the backend writes
them. Each round-trip reading carries its row count, so two empty tables cannot
hash-match, and a directory that cannot be listed is a failed reading, never "no
debris".
"""
import hashlib
import os
import pathlib

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_export_sink"
ROWS = 40000
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _counted_hash(dsn, table):
    """-> "count:md5" of a table, the shell's pgc_set_hash with its row count."""
    n = _value(dsn, f"SELECT count(*) FROM {table}")
    h = _value(dsn, HASH.format(f"SELECT * FROM {table}"))
    return f"{n}:{h}" if isinstance(n, int) and isinstance(h, str) and not _failed(h) else query_error(
        f"no counted hash of {table}: {n} / {h}")


def _sqlstate(dsn, fail_after, sql):
    """-> the SQLSTATE the export raises with the injection set in its session."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SET pgcolumnar.sink_fail_after = {fail_after}")
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


def _debris(d):
    """-> the count of *.tmp.* names under d. A directory that is missing or cannot be
    listed is a failed reading: rglob yields nothing for one, so walk and keep its errors."""
    errors = []
    found = sum(1 for _, dirs, files in os.walk(d, onerror=errors.append)
                for n in dirs + files if ".tmp." in n)
    return query_error(f"listing {d} failed: {errors[0]}") if errors else found


def _md5(path):
    try:
        return hashlib.md5(pathlib.Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _fwrites(name):
    try:
        return sum(1 for line in (SRC / name).read_text().splitlines() if "fwrite(" in line)
    except OSError as exc:
        return query_error(f"{name} unreadable: {exc}")


@pytest.fixture(scope="module")
def es(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        d = server_dir
        pqf, arf = d / "es.parquet", d / "es.arrow"
        r = {"census parquet": _fwrites("columnar_parquet.c"), "census arrow": _fwrites("columnar_arrow.c")}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # one statement per call, as the shell's psql_run: a failed load keeps its table
        _run(dsn, "CREATE TABLE es_src (id int, v int8, t text) USING pgcolumnar")
        _run(dsn, f"INSERT INTO es_src SELECT g, g*17, 'row-'||g FROM generate_series(1,{ROWS}) g")
        r["pq export"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('es_src', '{pqf}')")
        r["ar export"] = _value(dsn, f"SELECT pgcolumnar.export_arrow('es_src', '{arf}')")
        r["debris ok"] = _debris(d)

        _run(dsn, "CREATE TABLE es_back_pq (id int, v int8, t text) USING pgcolumnar")
        _run(dsn, f"SELECT pgcolumnar.import_parquet('es_back_pq', '{pqf}')")
        r["pq back"] = _counted_hash(dsn, "es_back_pq")
        _run(dsn, "CREATE TABLE es_back_ar (id int, v int8, t text) USING pgcolumnar")
        _run(dsn, f"SELECT pgcolumnar.import_arrow('es_back_ar', '{arf}')")
        r["ar back"] = _counted_hash(dsn, "es_back_ar")
        src = _value(dsn, HASH.format("SELECT * FROM es_src"))
        r["src"] = query_error(f"no hash of es_src: {src}") if _failed(src) else f"{ROWS}:{src}"

        try:
            size = os.stat(arf).st_size
        except OSError:
            size = 0
        r["arrow size"] = size
        before = _md5(pqf)
        before = before if before else "unread before the failed export"

        failp = d / "es_fail.parquet"
        r["pq inject"] = _sqlstate(dsn, 8192, f"SELECT pgcolumnar.export_parquet('es_src', '{failp}')")
        r["pq final"] = "dirty" if failp.exists() else "clean"
        state = _sqlstate(dsn, 8192, f"SELECT pgcolumnar.export_parquet('es_src', '{pqf}')")
        r["pq intact"] = f"{state}:{_md5(pqf)}"
        r["pq before"] = f"53100:{before}"
        r["pq debris"] = _debris(d)

        faila = d / "es_fail.arrow"
        r["ar inject"] = _sqlstate(dsn, size - 100, f"SELECT pgcolumnar.export_arrow('es_src', '{faila}')")
        r["ar final"] = "dirty" if faila.exists() else "clean"
        r["ar debris"] = _debris(d)

        r["again"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es2.parquet'}')")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_census_zero_fwrite_remain_in_columnar_parquet_c(es, expect):
    expect.text(str(es["census parquet"]), "0", "census: zero fwrite remain in columnar_parquet.c")


def test_census_zero_fwrite_remain_in_columnar_arrow_c(es, expect):
    expect.text(str(es["census arrow"]), "0", "census: zero fwrite remain in columnar_arrow.c")


def test_parquet_export_succeeds_through_the_sink(es, expect):
    expect.text(str(es["pq export"]), str(ROWS), "parquet export succeeds through the sink")


def test_arrow_export_succeeds_through_the_sink(es, expect):
    expect.text(str(es["ar export"]), str(ROWS), "arrow export succeeds through the sink")


def test_no_temp_debris_after_successful_exports(es, expect):
    expect.text(str(es["debris ok"]), "0", "no temp debris after successful exports")


def test_parquet_round_trip_hash_matches_the_source(es, expect):
    expect.text(es["pq back"], es["src"], "parquet round-trip hash-matches the source")


def test_arrow_round_trip_hash_matches_the_source(es, expect):
    expect.text(es["ar back"], es["src"], "arrow round-trip hash-matches the source")


def test_the_arrow_size_supports_a_mid_batch_injection_point(es, expect):
    expect.text("yes" if es["arrow size"] > 4200 else f"no ({es['arrow size']} bytes)", "yes",
                "measured arrow size supports a mid-batch injection point")


def test_inject_parquet_export_fails_as_disk_full(es, expect):
    expect.text(str(es["pq inject"]), "53100", "inject: parquet export fails as disk-full (53100)")


def test_inject_nothing_exists_at_the_parquet_final_path(es, expect):
    expect.text(es["pq final"], "clean", "inject: nothing exists at the parquet FINAL path")


def test_inject_a_failed_re_export_leaves_the_previous_parquet_file_intact(es, expect):
    expect.text(es["pq intact"], es["pq before"],
                "inject: a failed re-export leaves the previous parquet file intact")


def test_inject_no_parquet_temp_debris(es, expect):
    expect.text(str(es["pq debris"]), "0", "inject: no parquet temp debris")


def test_inject_arrow_export_fails_inside_write_record_batch(es, expect):
    expect.text(str(es["ar inject"]), "53100", "inject: arrow export fails inside write_record_batch (53100)")


def test_inject_nothing_exists_at_the_arrow_final_path(es, expect):
    expect.text(es["ar final"], "clean", "inject: nothing exists at the arrow FINAL path")


def test_inject_no_arrow_temp_debris(es, expect):
    expect.text(str(es["ar debris"]), "0", "inject: no arrow temp debris")


def test_after_reset_exports_work_again(es, expect):
    expect.text(str(es["again"]), str(ROWS), "after RESET, exports work again")

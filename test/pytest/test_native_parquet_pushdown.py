"""The Parquet FDW skips row groups its statistics prove empty (Phase G, #1341). The
pytest twin of `test/native_parquet_pushdown.sh`, every check by the shell's name.

Each row group's min/max is read, and a pushable col-op-const clause skips groups it
proves empty; the executor still rechecks every row, so a skip can only save work.
Correctness against a heap oracle (skipping never changes results), and the skips
themselves through EXPLAIN ANALYZE's "Row Groups Skipped". Then soundness: a NaN row
sits outside the statistics, so float > and >= and = 'NaN' must never skip; an
inverted interval (a footer whose min and max are swapped) must never drive a skip;
and an INT64-backed DECIMAL is skippable. Every file is written by pyarrow, an
independent writer, so the reader and the writer cannot share a misunderstanding.
"""
import datetime
import decimal
import struct

import psycopg

from pgc_vacuity import query_error

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
ANALYZE = "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM {} WHERE {}"


def _hash(cur, sql):
    """-> the shell's pgc_set_hash of `sql`, or the error: two failing queries never agree."""
    try:
        cur.execute(HASH.format(sql))
        return cur.fetchone()[0]
    except psycopg.Error as exc:
        return query_error(f"{exc.diag.message_primary} [{sql}]")


def _counter(cur, table, where, label):
    """-> the number on the plan's `label` line, or '' when there is none."""
    cur.execute(ANALYZE.format(table, where))
    for (line,) in cur.fetchall():
        if label in line:
            digits = "".join(ch if ch.isdigit() else " " for ch in line.split(label, 1)[1]).split()
            return digits[0] if digits else ""
    return ""


def _write(path, table, pq, **kw):
    pq.write_table(table, str(path), **kw)
    path.chmod(0o644)


def _ft(cur, server_dir, pa, pq):
    """200000 rows sorted by id in four groups of 50000: disjoint id ranges, with stats."""
    path = server_dir / "stats.parquet"
    n = 200000
    _write(path, pa.table({"id": pa.array(range(n), pa.int32()),
                           "val": pa.array([i * 1.5 for i in range(n)], pa.float64())}), pq, row_group_size=50000)
    assert pq.ParquetFile(str(path)).num_row_groups == 4
    cur.execute("CREATE TABLE h (id int, val float8)")
    cur.execute("INSERT INTO h SELECT g, g*1.5 FROM generate_series(0,199999) g")
    cur.execute("CREATE SERVER IF NOT EXISTS pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
    cur.execute(f"CREATE FOREIGN TABLE ft (id int4, val float8) SERVER pq OPTIONS (path '{path}')")
    return path


def test_skipping_never_changes_the_answer_and_really_skips(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet",
                               name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    with pgc_conn.cursor() as cur:
        path = _ft(cur, server_dir, pa, pq)
        expect.text(_hash(cur, "SELECT * FROM ft"), _hash(cur, "SELECT * FROM h"), "full scan == oracle")
        for pred in ("id = 60000", "id BETWEEN 60000 AND 70000", "id < 100", "id >= 199900",
                     "id > 60000 AND id < 60010", "id <= 5 OR id >= 199995", "val > 90000 AND val < 90030"):
            expect.text(_hash(cur, f"SELECT * FROM ft WHERE {pred}"), _hash(cur, f"SELECT * FROM h  WHERE {pred}"),
                        f"predicate [{pred}] == oracle")
        # THE ROW-GROUP EDGES (#1496 review): group 0 ends at 49999, group 1 starts at
        # 50000, and an inclusive bound read as exclusive skips the group holding the
        # match. Every predicate above stays clear of an edge.
        for pred in ("id >= 49999", "id > 49998", "id <= 50000", "id < 50001", "id = 49999", "id = 50000"):
            expect.text(_hash(cur, f"SELECT * FROM ft WHERE {pred}"), _hash(cur, f"SELECT * FROM h  WHERE {pred}"),
                        f"row-group edge [{pred}] == oracle")

        expect.text(_counter(cur, "ft", "id >= 0", "Row Groups:"), "4", "reports 4 row groups")
        for name, where, want in (
            ("range in one group skips 3", "id BETWEEN 60000 AND 70000", "3"),
            ("point in group 0 skips 3", "id = 123", "3"),
            ("id >= 199900 skips 3", "id >= 199900", "3"),
            ("id < 100 skips 3", "id < 100", "3"),
            # float >= and > are never pushed: Parquet stats exclude NaN, PostgreSQL sorts it last
            ("float predicate skips 2 (>= not pushed, NaN-unsafe)", "val >= 90000 AND val <= 90030", "2"),
            ("float < still skips", "val < 1.0", "3"),
            ("spanning predicate skips 0", "id >= 0 AND id < 200000", "0"),
            ("no predicate skips 0", "true", "0"),
        ):
            expect.text(_counter(cur, "ft", where, "Row Groups Skipped"), want, name)

        # read_parquet over the same file ignores the stats and reads everything
        expect.text(_hash(cur, f"SELECT * FROM pgcolumnar.read_parquet('{path}') AS t(id int4, val float8)"),
                    _hash(cur, "SELECT * FROM h"), "read_parquet same file == oracle")


def test_a_nan_row_is_never_skipped(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet",
                               name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    path = server_dir / "nan.parquet"
    vals = [float(i) for i in range(4000)]
    vals[500] = float("nan")          # one NaN, in row group 0
    _write(path, pa.table({"id": pa.array(range(4000), pa.int32()), "val": pa.array(vals, pa.float64())}),
           pq, row_group_size=1000)
    assert pq.ParquetFile(str(path)).num_row_groups == 4
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE SERVER IF NOT EXISTS pq_nan FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute(f"CREATE FOREIGN TABLE ftn (id int4, val float8) SERVER pq_nan OPTIONS (path '{path}')")
        cur.execute("CREATE TABLE hn (id int, val float8)")
        cur.execute("INSERT INTO hn SELECT g, CASE WHEN g = 500 THEN 'NaN'::float8 ELSE g END "
                    "FROM generate_series(0,3999) g")
        for pred in ("val > 1e300", "val >= 1e300", "val = 'NaN'::float8", "val > 3998", "val < 10", "val <= 10",
                     "val = 1234"):
            expect.text(_hash(cur, f"SELECT * FROM ftn WHERE {pred}"), _hash(cur, f"SELECT * FROM hn  WHERE {pred}"),
                        f"NaN-safe [{pred}] == oracle")


def test_inverted_statistics_never_drive_a_skip(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet",
                               name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    # A signed INT32 holding 1 and -1294967296, with the footer's min and max swapped:
    # the group claims min=1, max=-1294967296. This used to be a UINT_32 column, whose
    # unsigned statistics the reader decoded signed; since #1814 it decodes unsigned,
    # so that file no longer inverts and the interval is made directly.
    path = server_dir / "inverted.parquet"
    _write(path, pa.table({"c": pa.array([1, -1294967296], pa.int32())}), pq, row_group_size=2)
    raw = path.read_bytes()
    base = len(raw) - 8 - int.from_bytes(raw[-8:-4], "little")
    # each statistic is a Thrift binary: length 4, then the PLAIN little-endian value
    lo, hi = b"\x04" + struct.pack("<i", -1294967296), b"\x04" + struct.pack("<i", 1)
    foot = raw[base:-8]
    assert foot.count(lo) >= 1 and foot.count(lo) == foot.count(hi), (foot.count(lo), foot.count(hi))
    foot = foot.replace(lo, b"\0SWAP\0").replace(hi, lo).replace(b"\0SWAP\0", hi)
    path.write_bytes(raw[:base] + foot + raw[-8:])
    st = pq.ParquetFile(str(path)).metadata.row_group(0).column(0).statistics
    assert st.min == 1 and st.max == -1294967296, (st.min, st.max)
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE SERVER IF NOT EXISTS pq_inv FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute(f"CREATE FOREIGN TABLE fti (c int4) SERVER pq_inv OPTIONS (path '{path}')")
        cur.execute("CREATE TABLE hi (c int4)")
        cur.execute("INSERT INTO hi VALUES (1), (-1294967296)")
        expect.text(_hash(cur, "SELECT * FROM fti"), _hash(cur, "SELECT * FROM hi"),
                    "inverted stats: unfiltered == oracle")
        for pred in ("c = 1", "c = -1294967296", "c >= 1", "c <= -1294967296", "c < 0", "c > 0"):
            expect.text(_hash(cur, f"SELECT * FROM fti WHERE {pred}"), _hash(cur, f"SELECT * FROM hi  WHERE {pred}"),
                        f"inverted-stats [{pred}] == oracle")
        expect.text(_counter(cur, "fti", "c = 1", "Row Groups Skipped"), "0", "inverted stats skip nothing")


def test_an_int64_backed_decimal_is_skippable(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet",
                               name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    # four row groups with disjoint ranges, stored as INT64, the type the stats decode from
    path = server_dir / "dec_push.parquet"
    vals = [decimal.Decimal(i) / 100 for i in range(0, 40000)]
    _write(path, pa.table({"d": pa.array(vals, pa.decimal128(18, 2))}), pq,
           store_decimal_as_integer=True, compression="none", row_group_size=10000)
    meta = pq.ParquetFile(str(path)).metadata
    assert meta.row_group(0).column(0).physical_type == "INT64" and meta.num_row_groups == 4
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE SERVER IF NOT EXISTS pq_dec FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute(f"CREATE FOREIGN TABLE ftdec (d numeric) SERVER pq_dec OPTIONS (path '{path}')")
        # 0.00 .. 399.99 in groups of 100.00: a range inside the second excludes the other three
        expect.text(_counter(cur, "ftdec", "d BETWEEN 120.00 AND 130.00", "Row Groups Skipped"), "3",
                    "INT64-backed DECIMAL: predicate skips 3 of 4 groups")
        cur.execute("SELECT count(*) FROM ftdec WHERE d BETWEEN 120.00 AND 130.00")
        expect.num(cur.fetchone()[0], 1001, "INT64-backed DECIMAL: skipping did not drop rows")
        expect.text(_counter(cur, "ftdec", "d >= 0", "Row Groups Skipped"), "0",
                    "INT64-backed DECIMAL: unfiltered scan skips nothing")
        # a declared typmod that rounds (#1790): numeric(18,1) reads 99.95 .. 99.99, in
        # the first group whose stored max is 99.99, as 100.0, so it must not skip;
        # the file's own typmod changes nothing and keeps skipping
        cur.execute(f"CREATE FOREIGN TABLE ftdec1 (d numeric(18,1)) SERVER pq_dec OPTIONS (path '{path}')")
        cur.execute(f"CREATE FOREIGN TABLE ftdec2 (d numeric(18,2)) SERVER pq_dec OPTIONS (path '{path}')")
        cur.execute("SELECT count(*) FROM ftdec1 WHERE d = 100.0")
        expect.num(cur.fetchone()[0], 10,
                   "typmod: a numeric(18,1) column returns every row that rounds to 100.0")
        expect.text(_counter(cur, "ftdec1", "d = 100.0", "Row Groups Skipped"), "0",
                    "typmod: a numeric(18,1) column skips no group for d = 100.0")
        expect.text(_counter(cur, "ftdec2", "d BETWEEN 120.00 AND 130.00", "Row Groups Skipped"), "3",
                    "typmod: a numeric(18,2) column, the file's own typmod, still skips 3 of 4 groups")


def test_a_time_type_at_full_precision_keeps_skipping(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet",
                               name="pyarrow not available; predicate-pushdown suite needs it to write stats")
    # four groups, one per hour of 2021-01-01: 999 whole seconds, then hh:59:59.9996,
    # which timestamp(3) reads as the next hour, beyond the group's stored max (#1790)
    path = server_dir / "ts_push.parquet"
    base = datetime.datetime(2021, 1, 1)
    vals = []
    for g in range(4):
        h = base + datetime.timedelta(hours=g)
        vals += [h + datetime.timedelta(seconds=k) for k in range(999)]
        vals.append(h + datetime.timedelta(minutes=59, seconds=59, microseconds=999600))
    _write(path, pa.table({"t": pa.array(vals, pa.timestamp("us"))}), pq,
           compression="none", row_group_size=1000)
    assert pq.ParquetFile(str(path)).metadata.num_row_groups == 4
    where = "t >= TIMESTAMP '2021-01-01 03:00:00'"
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE SERVER IF NOT EXISTS pq_ts FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute(f"CREATE FOREIGN TABLE ftts6 (t timestamp(6)) SERVER pq_ts OPTIONS (path '{path}')")
        cur.execute(f"CREATE FOREIGN TABLE ftts3 (t timestamp(3)) SERVER pq_ts OPTIONS (path '{path}')")
        expect.text(_counter(cur, "ftts6", where, "Row Groups Skipped"), "3",
                    "typmod: a timestamp(6) column still skips 3 of 4 groups")
        expect.text(_counter(cur, "ftts3", where, "Row Groups Skipped"), "0",
                    "typmod: a timestamp(3) column skips no group for t >= 03:00")
        cur.execute(f"SELECT count(*) FROM ftts3 WHERE {where}")
        expect.num(cur.fetchone()[0], 1001,
                   "typmod: a timestamp(3) column returns the row that rounds up to 03:00")

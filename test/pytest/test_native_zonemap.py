"""Native zone maps, storage side (Phase D5a/D5b). The pytest twin of
`test/native_zonemap.sh`, every check by the shell's name (#1341).

A native-format table records a Small Materialized Aggregate per 1024-value vector and
per column chunk in pgcolumnar.zone_map: min, max, value_count, null_count, and a sum
for int2/int4 columns. The suite validates the catalog rows directly against a heap
mirror written from the same generator: their shape, the exact counts, the bounds'
presence, width AND VALUES, the sums, that the read path is unchanged, and that the
rows go with the table.

THE BOUNDS ARE COMPARED BY VALUE (#1341). The suite used to check only that a bound
was present and had the right width, and every column ascended, so a vector's first
value was always its minimum. A writer that never raised a max past a vector's first
value, never lowered a min, or never folded a chunk max past its first vector stored
bounds of the right width, and main passed all 18 checks under each. `v` is now a
permutation (its values, nulls and counts unchanged), and each bound is decoded and
compared with the heap's own for its group and vector.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading. Every reading opens its own connection and returns a failure as a
`query_error()`, so two failed reads never compare equal and a count that could not be
read is never defaulted to 0.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_zonemap"
GEN = """SELECT g,
       (g * 10)::bigint,
       'lbl-' || (g % 50),
       CASE WHEN g % 7 = 0 THEN NULL ELSE ((g::bigint * 7919) % 6151)::int END
  FROM generate_series(1, 6144) g"""
HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
ZM_LE = """CREATE FUNCTION zm_le(b bytea) RETURNS bigint LANGUAGE sql IMMUTABLE AS
    $$SELECT sum((get_byte(b, i)::bigint) << (8 * i))::bigint
        FROM generate_series(0, length(b) - 1) i$$"""


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _one(dsn, sql):
    """-> the first column of the first row as psql -At prints it, or the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    if row is None:
        return query_error(f"no row from {sql}")
    v = row[0]
    if v is None:
        return ""
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v)


def _int(dsn, sql):
    """-> the value as an int, or why there is none."""
    v = _one(dsn, sql)
    if v.lstrip("-").isdigit():
        return int(v)
    return v if v.startswith("QUERY_ERROR") else query_error(f"not a number from {sql}: [{v}]")


def _zq(sid, expr):
    return f"SELECT {expr} FROM pgcolumnar.zone_map WHERE storage_id = {sid}"


def _zint(dsn, sid, expr):
    """-> a zone-map reading for storage `sid`, or sid's own failure when it could not be
    read. A query over `storage_id = NULL` matches no row, and a count of bad rows over no
    rows is the 0 nine checks want (#1697 review); the shell's empty $SID is a syntax
    error there, so each of its checks fails."""
    return _int(dsn, _zq(sid, expr)) if isinstance(sid, int) else sid


def _bounds_match(dsn, sid, kind, side):
    """-> how many zone rows of id (0), k (1) and v (3) hold the heap's own bound for
    their group and vector; kind "vector" (18 rows) or "chunk" (9 rows), as the shell's
    bounds_match."""
    if not isinstance(sid, int):
        return sid
    vfilter = "vector_index = -1" if kind == "chunk" else "vector_index >= 0"
    p = side[:3]
    return _int(dsn, f"""
        WITH z AS (
            SELECT dense_rank() OVER (ORDER BY group_number) - 1 AS g, column_index AS c,
                   vector_index AS vi, zm_le({side}) AS got
              FROM pgcolumnar.zone_map
             WHERE storage_id = {sid} AND column_index IN (0, 1, 3) AND {vfilter}),
             hv AS (
            SELECT (id - 1) / 2048 AS g, ((id - 1) % 2048) / 1024 AS vi,
                   min(id) AS min0, max(id) AS max0, min(k) AS min1, max(k) AS max1,
                   min(v) AS min3, max(v) AS max3
              FROM h GROUP BY 1, 2
            UNION ALL
            SELECT (id - 1) / 2048, -1, min(id), max(id), min(k), max(k), min(v), max(v)
              FROM h GROUP BY 1)
        SELECT count(*) FROM z JOIN hv USING (g, vi)
         WHERE z.got = CASE z.c WHEN 0 THEN hv.{p}0 WHEN 1 THEN hv.{p}1 ELSE hv.{p}3 END""")


@pytest.fixture(scope="module")
def zm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE h (id int, k bigint, label text, v int)")
            conn.execute("CREATE TABLE n (id int, k bigint, label text, v int) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, "
                         "chunk_group_row_limit => 1024)")
            conn.execute(f"INSERT INTO h {GEN}")
            conn.execute(f"INSERT INTO n {GEN}")
            conn.execute(ZM_LE)
        sid = _int(dsn, "SELECT pgcolumnar.get_storage_id('n')")
        r = {
            "rows": _int(dsn, "SELECT count(*) FROM n"),
            "hash n": _one(dsn, HASH.format("SELECT id, k, label, v FROM n")),
            "hash h": _one(dsn, HASH.format("SELECT id, k, label, v FROM h")),
            "chunk rows": _zint(dsn, sid, "count(*) FILTER (WHERE vector_index = -1)"),
            "vector rows": _zint(dsn, sid, "count(*) FILTER (WHERE vector_index >= 0)"),
            "chunk cover": _zint(dsn, sid, "count(*) FILTER (WHERE vector_index = -1 "
                                                "AND value_count + null_count <> 2048)"),
            "vector cover": _zint(dsn, sid, "count(*) FILTER (WHERE vector_index >= 0 "
                                                 "AND value_count + null_count <> 1024)"),
            "v values": _zint(dsn, sid, "sum(value_count) FILTER (WHERE vector_index = -1 "
                                             "AND column_index = 3)"),
            "h v values": _int(dsn, "SELECT count(v) FROM h"),
            "v nulls": _zint(dsn, sid, "sum(null_count) FILTER (WHERE vector_index = -1 "
                                            "AND column_index = 3)"),
            "h v nulls": _int(dsn, "SELECT count(*) - count(v) FROM h"),
            "id values": _zint(dsn, sid, "sum(value_count) FILTER (WHERE vector_index = -1 "
                                              "AND column_index = 0)"),
            "id nulls": _zint(dsn, sid, "sum(null_count) FILTER (WHERE vector_index = -1 "
                                             "AND column_index = 0)"),
            "k folds": _zint(dsn, sid, """count(*) FILTER (WHERE column_index = 1 AND vector_index = -1
                AND value_count <> (SELECT sum(z2.value_count)
                                    FROM pgcolumnar.zone_map z2
                                    WHERE z2.storage_id = pgcolumnar.zone_map.storage_id
                                      AND z2.group_number = pgcolumnar.zone_map.group_number
                                      AND z2.column_index = 1 AND z2.vector_index >= 0))"""),
            "minmax missing": _zint(dsn, sid, "count(*) FILTER (WHERE minimum IS NULL "
                                                   "OR maximum IS NULL)"),
            "id width": _zint(dsn, sid, "count(*) FILTER (WHERE column_index = 0 "
                                             "AND octet_length(minimum) <> 4)"),
            "k width": _zint(dsn, sid, "count(*) FILTER (WHERE column_index = 1 "
                                            "AND octet_length(maximum) <> 8)"),
            "v not ascending": _one(dsn, """SELECT count(*) > 0 FROM (
                SELECT (id - 1) / 1024 AS vec, min(v) AS mn,
                       (array_agg(v ORDER BY id) FILTER (WHERE v IS NOT NULL))[1] AS first
                  FROM h GROUP BY 1) s WHERE first <> mn"""),
            "vector min": _bounds_match(dsn, sid, "vector", "minimum"),
            "vector max": _bounds_match(dsn, sid, "vector", "maximum"),
            "chunk min": _bounds_match(dsn, sid, "chunk", "minimum"),
            "chunk max": _bounds_match(dsn, sid, "chunk", "maximum"),
            "int4 sums": _zint(dsn, sid, "count(*) FILTER (WHERE column_index IN (0,3) "
                                              "AND value_count > 0 AND sum IS NULL)"),
            "other sums": _zint(dsn, sid, "count(*) FILTER (WHERE column_index IN (1,2) "
                                               "AND sum IS NOT NULL)"),
            "id sum": _zint(dsn, sid, "sum(sum) FILTER (WHERE column_index = 0 "
                                           "AND vector_index = -1)"),
            "h id sum": _int(dsn, "SELECT sum(id) FROM h"),
        }
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("DROP TABLE n")
            r["after drop"] = _zint(dsn, sid, "count(*)")
        except psycopg.Error as exc:
            r["after drop"] = _failed(exc)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_zonemap_row_count(zm, expect):
    expect.num(zm["rows"], 6144, "row count")


def test_zonemap_read_parity(zm, expect):
    expect.text(zm["hash n"], zm["hash h"], "read parity (no regression)")


def test_zonemap_whole_chunk_zone_rows(zm, expect):
    expect.num(zm["chunk rows"], 12, "whole-chunk zone rows")


def test_zonemap_per_vector_zone_rows(zm, expect):
    expect.num(zm["vector rows"], 24, "per-vector zone rows")


def test_zonemap_whole_chunk_rows_cover_their_group(zm, expect):
    expect.num(zm["chunk cover"], 0, "whole-chunk value+null = group rows")


def test_zonemap_each_vector_covers_1024(zm, expect):
    expect.num(zm["vector cover"], 0, "per-vector value+null = 1024")


def test_zonemap_value_count_sums_for_nullable_v(zm, expect):
    expect.num(zm["v values"], zm["h v values"], "value_count sums (v, nullable)")


def test_zonemap_null_count_sums_for_nullable_v(zm, expect):
    expect.num(zm["v nulls"], zm["h v nulls"], "null_count sums (v, nullable)")


def test_zonemap_value_count_sums_for_id(zm, expect):
    expect.num(zm["id values"], 6144, "value_count sums (id, no nulls)")


def test_zonemap_null_count_zero_for_id(zm, expect):
    expect.num(zm["id nulls"], 0, "null_count zero (id, no nulls)")


def test_zonemap_vector_value_counts_fold_into_the_chunk(zm, expect):
    expect.num(zm["k folds"], 0, "per-vector value_count folds into chunk (k)")


def test_zonemap_min_max_present_for_all_columns(zm, expect):
    expect.num(zm["minmax missing"], 0, "min/max present for all columns")


def test_zonemap_int4_min_width(zm, expect):
    expect.num(zm["id width"], 0, "id (int4) min width = 4 bytes")


def test_zonemap_int8_max_width(zm, expect):
    expect.num(zm["k width"], 0, "k (int8) max width = 8 bytes")


def test_zonemap_v_is_not_ascending_within_a_vector(zm, expect):
    expect.text(zm["v not ascending"], "t",
                "premise: v is not ascending within a vector, so a stale minimum would show")


def test_zonemap_every_vector_minimum_is_the_heaps(zm, expect):
    expect.num(zm["vector min"], 18, "every vector's minimum of id, k and v is the heap's")


def test_zonemap_every_vector_maximum_is_the_heaps(zm, expect):
    expect.num(zm["vector max"], 18, "every vector's maximum of id, k and v is the heap's")


def test_zonemap_every_chunk_minimum_is_the_heaps(zm, expect):
    expect.num(zm["chunk min"], 9, "every whole-chunk minimum of id, k and v is the heap's")


def test_zonemap_every_chunk_maximum_is_the_heaps(zm, expect):
    expect.num(zm["chunk max"], 9, "every whole-chunk maximum of id, k and v is the heap's")


def test_zonemap_sum_present_for_int4_columns(zm, expect):
    expect.num(zm["int4 sums"], 0, "sum present for int4 columns")


def test_zonemap_sum_null_for_other_columns(zm, expect):
    expect.num(zm["other sums"], 0, "sum null for non-int4 columns")


def test_zonemap_whole_chunk_sum_of_id_matches_heap(zm, expect):
    expect.num(zm["id sum"], zm["h id sum"], "whole-chunk sum(id) matches heap")


def test_zonemap_zone_maps_go_with_the_table(zm, expect):
    expect.num(zm["after drop"], 0, "zone maps cleaned on drop")

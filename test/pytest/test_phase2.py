"""Compression, projection, zone-map min/max and chunk-group filtering, plus the phase 1
regression checks: the pytest twin of `test/phase2.sh`, every check by the shell's name
(#1341).

Each codec writes 20000 rows with an identical 100-byte payload, and must round-trip
AND be the codec stored: the round-trip alone passed with the session's setting ignored
and with no block codec ever applied. A single value is too small to compress, so it is
stored with codec 0 whatever was asked. The zone maps of 25000 sorted ints in three
vectors are decoded from their little-endian bytes, and a range wholly inside the last
vector must decode one of the three, not merely return the right rows: exact selection
returns them with the zone maps ruling out nothing. Every expected value comes from the
generator, never from the columnar table.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order, because the
fallback and orphan checks read the database's whole catalog. A reading is a value or a
query_error(), never text that stands in for one.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_phase2"
CODECS = (("none", 0), ("pglz", 1), ("lz4", 2), ("zstd", 3))
ZM = ("pgcolumnar.zone_map WHERE storage_id = pgcolumnar.get_storage_id('s') "
      "AND column_index = 0 AND vector_index")
DEC = ("((get_byte(minimum,3)::bigint<<24)|(get_byte(minimum,2)<<16)"
       "|(get_byte(minimum,1)<<8)|get_byte(minimum,0))")
DECX = DEC.replace("minimum", "maximum")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v)


def _q(dsn, *sql, sep="\n", trail=""):
    """-> the last statement's rows as psql -At prints them (`sep` between rows, `trail`
    after each when given), or a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sql[:-1]:
                conn.execute(s)
            rows = conn.execute(sql[-1]).fetchall()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    if trail:
        return "".join("|".join(_cell(v) for v in r) + trail for r in rows)
    return sep.join("|".join(_cell(v) for v in r) for r in rows)


def _qq(dsn, sql):
    return _q(dsn, sql, trail=",")


def _decoded(dsn, sql):
    """-> the plan's Columnar Vectors Decoded, or a query_error()."""
    plan = _q(dsn, "SET max_parallel_workers_per_gather = 0",
              f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}")
    m = re.search(r"Columnar Vectors Decoded: (\d+)", plan)
    return m.group(1) if m else query_error(f"no Vectors Decoded line in [{plan}]")


@pytest.fixture(scope="module")
def p2(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        q = lambda *s: _q(dsn, *s)
        q("CREATE EXTENSION pgcolumnar")
        r = {}

        q("CREATE TABLE t (a int, b text) USING pgcolumnar")
        q("INSERT INTO t SELECT g, g::text FROM generate_series(1,100000) g")
        r["count(*)"] = q("SELECT count(*) FROM t")
        r["count where a<50"] = q("SELECT count(*) FROM t WHERE a < 50")
        r["order by a limit3"] = _qq(dsn, "SELECT a || CHR(124) || b FROM t ORDER BY a LIMIT 3")
        r["no nulls t.b"] = q("SELECT count(*) FROM t WHERE b IS NULL")
        q("DROP TABLE t")

        for codec, _ in CODECS:
            q("CREATE TABLE c (id int, payload text) USING pgcolumnar")
            # SET and INSERT in one session so the setting is in force at write time.
            q(f"SET pgcolumnar.compression = '{codec}'",
              "INSERT INTO c SELECT g, repeat('x',100) FROM generate_series(1,20000) g")
            r[f"{codec} count"] = q("SELECT count(*) FROM c")
            r[f"{codec} sum(id)"] = q("SELECT sum(id) FROM c")
            r[f"{codec} payload ok"] = q("SELECT payload = repeat('x',100) FROM c WHERE id = 12345")
            r[f"{codec} distinct pl"] = q("SELECT count(DISTINCT payload) FROM c")
            r[f"{codec} block codec"] = q(
                "SELECT string_agg(DISTINCT block_codec::text, ',') FROM pgcolumnar.column_chunk "
                "WHERE storage_id = pgcolumnar.get_storage_id('c') AND column_index = 1")
            q("DROP TABLE c")

        q("CREATE TABLE f (id int) USING pgcolumnar")
        q("SET pgcolumnar.compression = 'zstd'", "INSERT INTO f VALUES (42)")
        r["fallback value"] = q("SELECT id FROM f")
        r["fallback codec=0"] = q("SELECT block_codec FROM pgcolumnar.column_chunk WHERE column_index = 0")
        q("DROP TABLE f")

        q("CREATE TABLE n (a int, b text) USING pgcolumnar")
        q("SET pgcolumnar.compression = 'lz4'",
          "INSERT INTO n SELECT g, CASE WHEN g%3=0 THEN NULL ELSE repeat('y',50) END "
          "FROM generate_series(1,15000) g")
        r["null rows"] = q("SELECT count(*) FROM n")
        r["null count"] = q("SELECT count(*) FROM n WHERE b IS NULL")
        r["null value"] = q("SELECT b IS NULL FROM n WHERE a = 9")
        r["nonnull val"] = q("SELECT b = repeat('y',50) FROM n WHERE a = 8")
        q("DROP TABLE n")

        q("CREATE TABLE p (a int, b text, c int) USING pgcolumnar")
        q("SET pgcolumnar.compression = 'zstd'",
          "INSERT INTO p SELECT g, 'row'||g, g*2 FROM generate_series(1,30000) g")
        r["project a"] = q("SELECT a FROM p WHERE a = 100")
        r["project b"] = q("SELECT b FROM p WHERE a = 100")
        r["project c"] = q("SELECT c FROM p WHERE a = 100")
        r["project sum"] = q("SELECT sum(c) FROM p")
        r["project b,c"] = _qq(dsn, "SELECT b || CHR(124) || c FROM p ORDER BY a LIMIT 2")
        q("DROP TABLE p")

        q("CREATE TABLE s (id int) USING pgcolumnar")
        q("SET pgcolumnar.compression = 'zstd'", "INSERT INTO s SELECT g FROM generate_series(1,25000) g")
        r["vectors"] = q(f"SELECT count(*) FROM {ZM} >= 0")
        r["minmax present"] = q(f"SELECT count(*) FROM {ZM} >= 0 AND minimum IS NOT NULL AND maximum IS NOT NULL")
        r["v0 min=1"] = q(f"SELECT {DEC} FROM {ZM} = 0")
        r["v0 max=10000"] = q(f"SELECT {DECX} FROM {ZM} = 0")
        r["v2 max=25000"] = q(f"SELECT {DECX} FROM {ZM} = 2")
        r["between count"] = q("SELECT count(*) FROM s WHERE id BETWEEN 15000 AND 15010")
        r["range min/max"] = _qq(dsn, "SELECT min(id) || CHR(124) || max(id) FROM s WHERE id > 24990")
        r["point lookup"] = q("SELECT count(*) FROM s WHERE id = 20005")
        r["empty range"] = q("SELECT count(*) FROM s WHERE id > 30000")
        r["range decodes 1 of 3 vectors"] = _decoded(dsn, "SELECT id FROM s WHERE id > 24990")
        q("DROP TABLE s")

        r["orphan row groups"] = q("SELECT count(*) FROM pgcolumnar.row_group")
        r["orphan chunks"] = q("SELECT count(*) FROM pgcolumnar.column_chunk")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_count_star_reads_every_row(p2, expect):
    expect.text(p2["count(*)"], "100000", "count(*)")


def test_a_filtered_count_reads_the_matching_rows(p2, expect):
    expect.text(p2["count where a<50"], "49", "count where a<50")


def test_order_by_limit_returns_the_first_three_rows(p2, expect):
    expect.text(p2["order by a limit3"], "1|1,2|2,3|3,", "order by a limit3")


def test_a_column_with_no_nulls_reads_none(p2, expect):
    expect.text(p2["no nulls t.b"], "0", "no nulls t.b")


# One test per codec per check, each under the shell's name: the shell's loop names
# them "$codec count" and so on, so the twin's names are the same template.
@pytest.mark.parametrize("codec", [c for c, _ in CODECS])
def test_a_codec_round_trips_every_row(p2, expect, codec):
    expect.text(p2[f"{codec} count"], "20000", f"{codec} count")


@pytest.mark.parametrize("codec", [c for c, _ in CODECS])
def test_a_codec_round_trips_the_ids(p2, expect, codec):
    expect.text(p2[f"{codec} sum(id)"], "200010000", f"{codec} sum(id)")


@pytest.mark.parametrize("codec", [c for c, _ in CODECS])
def test_a_codec_round_trips_a_payload(p2, expect, codec):
    expect.text(p2[f"{codec} payload ok"], "t", f"{codec} payload ok")


@pytest.mark.parametrize("codec", [c for c, _ in CODECS])
def test_a_codec_round_trips_one_distinct_payload(p2, expect, codec):
    expect.text(p2[f"{codec} distinct pl"], "1", f"{codec} distinct pl")


@pytest.mark.parametrize("codec, code", CODECS)
def test_the_codec_asked_for_is_the_block_codec_stored(p2, expect, codec, code):
    expect.text(p2[f"{codec} block codec"], str(code), f"{codec} block codec")

def test_a_value_too_small_to_compress_round_trips(p2, expect):
    expect.text(p2["fallback value"], "42", "fallback value")


def test_a_value_too_small_to_compress_is_stored_with_codec_0(p2, expect):
    expect.text(p2["fallback codec=0"], "0", "fallback codec=0")


def test_nulls_round_trip_under_lz4(p2, expect):
    expect.text(p2["null rows"], "15000", "null rows")


def test_every_third_value_reads_null(p2, expect):
    expect.text(p2["null count"], "5000", "null count")


def test_a_null_reads_null(p2, expect):
    expect.text(p2["null value"], "t", "null value")


def test_a_value_beside_nulls_reads_its_value(p2, expect):
    expect.text(p2["nonnull val"], "t", "nonnull val")


def test_projecting_a_reads_a(p2, expect):
    expect.text(p2["project a"], "100", "project a")


def test_projecting_b_reads_b(p2, expect):
    expect.text(p2["project b"], "row100", "project b")


def test_projecting_c_reads_c(p2, expect):
    expect.text(p2["project c"], "200", "project c")


def test_a_sum_over_one_projected_column(p2, expect):
    expect.text(p2["project sum"], "900030000", "project sum")


def test_projecting_two_columns_in_order(p2, expect):
    expect.text(p2["project b,c"], "row1|2,row2|4,", "project b,c")


def test_three_vectors_have_zone_maps(p2, expect):
    expect.text(p2["vectors"], "3", "vectors")


def test_every_zone_map_has_a_minimum_and_maximum(p2, expect):
    expect.text(p2["minmax present"], "3", "minmax present")


def test_vector_0_minimum_is_1(p2, expect):
    expect.text(p2["v0 min=1"], "1", "v0 min=1")


def test_vector_0_maximum_is_10000(p2, expect):
    expect.text(p2["v0 max=10000"], "10000", "v0 max=10000")


def test_vector_2_maximum_is_25000(p2, expect):
    expect.text(p2["v2 max=25000"], "25000", "v2 max=25000")


def test_a_between_filter_returns_its_rows(p2, expect):
    expect.text(p2["between count"], "11", "between count")


def test_a_range_filter_returns_its_bounds(p2, expect):
    expect.text(p2["range min/max"], "24991|25000,", "range min/max")


def test_a_point_lookup_returns_one_row(p2, expect):
    expect.text(p2["point lookup"], "1", "point lookup")


def test_a_range_past_the_data_returns_none(p2, expect):
    expect.text(p2["empty range"], "0", "empty range")


def test_a_range_inside_the_last_vector_decodes_one_of_three(p2, expect):
    expect.text(p2["range decodes 1 of 3 vectors"], "1", "range decodes 1 of 3 vectors")


def test_no_row_group_outlives_its_table(p2, expect):
    expect.text(p2["orphan row groups"], "0", "orphan row groups")


def test_no_column_chunk_outlives_its_table(p2, expect):
    expect.text(p2["orphan chunks"], "0", "orphan chunks")

"""FSST is kept only where it wins after the block compressor. The pytest twin of
`test/write_fsst_compressed.sh`, every check by the shell's name (#1341).

FSST was picked per vector by comparing its code stream with the raw length, but what
reaches disk is that stream after the codec has run, and FSST turns repetitive text into
high-entropy codes that compress far worse than the text did. The writer now runs both
candidates through the codec over a bounded run of the chunk and keeps FSST only when it
is genuinely smaller. Asserted: values round-trip against a heap mirror on both outcomes;
the choice, read from the chunk descriptor, goes the right way on each shape, with an
e-mail-shaped control where FSST must be kept; and with compression off nothing changes.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). A failed query is a
`query_error()`. A round-trip reading carries its row count and a vector count says
"no vectors" for a column with none, so an empty table cannot pass as agreement.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_write_fsst_compressed"
ROWS = 300000
MIXED = "CASE g % 11 WHEN 0 THEN NULL WHEN 1 THEN '' WHEN 2 THEN 'x' ELSE 's' || g END"
SHAPES = {"short": "'s' || g",
          "email": "'user' || g || '@' || (g % 997) || '.example.com'",
          "hient": "repeat(md5(g::text), 8)"}

# How many vectors of the text column chose FSST (type 8), or "no vectors". The
# descriptor is a 6-byte header -- version, flags, then the vector count as uint32 --
# followed by that many 13-byte entries; entry i's type byte is at 6 + i*13.
COUNT = "get_byte(c.encoding_descriptor, 2) + get_byte(c.encoding_descriptor, 3) * 256 " \
        "+ get_byte(c.encoding_descriptor, 4) * 65536 + get_byte(c.encoding_descriptor, 5) * 16777216"
FSST_VECTORS = (
    "SELECT CASE WHEN coalesce(sum(total), 0) > 0 THEN sum(n)::text ELSE 'no vectors' END FROM ("
    f"SELECT {COUNT} AS total, (SELECT count(*) FROM generate_series(0, {COUNT} - 1) i "
    "WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = 8) AS n "
    "FROM pgcolumnar.column_chunk c JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id "
    "WHERE s.relation_oid = '{t}'::regclass AND c.column_index = 1) t")
VECTORS = (f"SELECT coalesce(sum({COUNT}), 0) FROM pgcolumnar.column_chunk c JOIN pgcolumnar.storage s "
           "ON s.storage_id = c.storage_id WHERE s.relation_oid = '{t}'::regclass AND c.column_index = 1")
ROUNDTRIP = ("SELECT count(*) FILTER (WHERE a.id IS NULL OR b.id IS NULL OR a.v IS DISTINCT FROM b.v) "
             "|| '/' || count(*) FROM {a} a FULL JOIN {b} b USING (id)")
NULLS = ("SELECT count(*) FILTER (WHERE v IS NULL) || '/' || count(*) FILTER (WHERE v = '') "
         "|| '/' || count(*) FROM {t}")


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    """-> the first column of the first row as text, on a connection of its own, or the
    failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
            return "" if row is None or row[0] is None else str(row[0])
    except psycopg.Error as exc:
        return _failed(exc)


def _run(dsn, *sqls):
    """Statements in one session, as the shell's psql_run does; a failure is printed
    and leaves the readings to show it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in sqls:
                conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")


def _build(dsn, table, shape, compression):
    _run(dsn, f"DROP TABLE IF EXISTS {table}", f"SET pgcolumnar.compression = {compression}",
         f"CREATE TABLE {table} (id int, v text) USING pgcolumnar",
         f"INSERT INTO {table} SELECT g, {SHAPES[shape]} FROM generate_series(1, {ROWS}) g")


def _chosen(n):
    """-> "yes" when n counts FSST vectors, "no" otherwise, or the failed reading."""
    return n if n.startswith("QUERY_ERROR") else ("yes" if n.isdigit() and int(n) > 0 else "no")


@pytest.fixture(scope="module")
def fc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # nulls, empty strings and values below the FSST minimum, mixed in. EVERY HEAP
        # MIRROR IS BUILT FROM ITS GENERATOR: a copy of the columnar table would carry a
        # consistently wrong decoder's values and round-trip as 0 mismatches.
        _run(dsn, "CREATE TABLE fc_c (id int, v text) USING pgcolumnar",
             f"INSERT INTO fc_c SELECT g, {MIXED} FROM generate_series(1, {ROWS}) g",
             "CREATE TABLE fc_h (id int, v text)",
             f"INSERT INTO fc_h SELECT g, {MIXED} FROM generate_series(1, {ROWS}) g")
        r = {"dropped": _value(dsn, ROUNDTRIP.format(a="fc_c", b="fc_h"))}
        heap = _value(dsn, NULLS.format(t="fc_h"))
        r["nulls"] = (_value(dsn, NULLS.format(t="fc_c")),
                      heap if heap.startswith("QUERY_ERROR") or heap.endswith(f"/{ROWS}")
                      else f"heap holds [{heap}], not {ROWS} rows")
        _build(dsn, "fc_e", "email", "zstd")
        _run(dsn, "DROP TABLE IF EXISTS fc_eh", "CREATE TABLE fc_eh (id int, v text)",
             f"INSERT INTO fc_eh SELECT g, {SHAPES['email']} FROM generate_series(1, {ROWS}) g")
        r["kept"] = _value(dsn, ROUNDTRIP.format(a="fc_e", b="fc_eh"))
        _build(dsn, "fc_s", "short", "zstd")
        r["short"] = _value(dsn, FSST_VECTORS.format(t="fc_s"))
        _build(dsn, "fc_hi", "hient", "zstd")
        r["hient"] = _value(dsn, FSST_VECTORS.format(t="fc_hi"))
        r["email"] = _value(dsn, FSST_VECTORS.format(t="fc_e"))
        r["email vectors"] = _value(dsn, VECTORS.format(t="fc_e"))
        for shape in ("short", "hient"):
            _build(dsn, f"fc_n_{shape}", shape, "none")
            r[f"none {shape}"] = _value(dsn, FSST_VECTORS.format(t=f"fc_n_{shape}"))
        _run(dsn, "DROP TABLE IF EXISTS fc_sh", "CREATE TABLE fc_sh (id int, v text)",
             f"INSERT INTO fc_sh SELECT g, {SHAPES['short']} FROM generate_series(1, {ROWS}) g")
        r["off"] = _value(dsn, ROUNDTRIP.format(a="fc_n_short", b="fc_sh"))
        print(f"-- FSST vectors: short={r['short']} hient={r['hient']} email={r['email']} "
              f"off short={r['none short']} off hient={r['none hient']}")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- 1. values round-trip through both outcomes -----------------------------

def test_every_value_round_trips_where_fsst_is_dropped(fc, expect):
    expect.text(fc["dropped"], f"0/{ROWS}", "every value round-trips on a shape where FSST is dropped")


def test_the_null_and_empty_values_survive(fc, expect):
    columnar, heap = fc["nulls"]
    expect.text(columnar, heap, "the null and empty values survive")


def test_every_value_round_trips_where_fsst_is_kept(fc, expect):
    expect.text(fc["kept"], f"0/{ROWS}", "every value round-trips on a shape where FSST is kept")


# ---- 2. the choice goes the right way on each shape -------------------------

def test_fsst_is_not_chosen_for_text_the_codec_compresses_better_alone(fc, expect):
    expect.text(fc["short"], "0", "FSST is not chosen for text the codec compresses better alone")


def test_fsst_is_not_chosen_where_it_cost_more_write_time_for_a_larger_file(fc, expect):
    expect.text(fc["hient"], "0", "FSST is not chosen where it cost 5.8x the write time for a larger file")


def test_fsst_is_still_chosen_where_it_genuinely_wins(fc, expect):
    """Every vector of the control, not some: a wrongly reused verdict dropped FSST in
    one stripe of two and still left some."""
    n, total = fc["email"], fc["email vectors"]
    failed = next((v for v in (n, total) if v.startswith("QUERY_ERROR")), None)
    expect.text(failed or ("yes" if _chosen(n) == "yes" and n == total else f"no ({n} of {total} vectors)"),
                "yes", "FSST is still chosen where it genuinely wins after compression")


# ---- 3. no codec, no change -------------------------------------------------

@pytest.mark.parametrize("shape", ["short", "hient"])
def test_with_compression_off_fsst_is_still_chosen(fc, shape, expect):
    expect.text(_chosen(fc[f"none {shape}"]), "yes", f"with compression off FSST is still chosen for {shape}")


def test_values_round_trip_with_compression_off(fc, expect):
    expect.text(fc["off"], f"0/{ROWS}", "values round-trip with compression off")

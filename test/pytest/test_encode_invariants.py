"""Encoding invariants for the #155 bulk-load optimisations. The pytest twin of
`test/encode_invariants.sh`, every check by the shell's name (#1341).

The C self-test compares each rewritten primitive with a reference implementation and
must report a real case count. Bit packing at every reachable width, full-range
64-bit values, Gorilla float patterns and dictionary text around the distinct cap all
round-trip against a heap mirror, with a control per section that the encoder under
test actually ran. A high-cardinality corpus selects FSST, and no vector at or below
the distinct cap does, which is the premise the FSST build skip rests on. The block
compression default is zstd at level 3.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order, with
enable_post_codec_encoding_choice off on that database as the shell sets it; each test
asserts one reading (#1339). A round-trip agrees only over a non-empty columnar hash,
and every FSST-premise table must hold its 20,000 rows, so an empty load cannot read as
"no FSST".
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_encode_invariants"
ROWS = 4096
WIDTHS = (1, 2, 3, 4, 7, 8, 9, 15, 16, 17, 24, 25, 31, 32)
GORILLA = {
    "constant": "1.5::float8",
    "zero": "0::float8",
    "tiny": "(1.0 + g * 1e-15)::float8",
    "alt": "CASE WHEN g % 2 = 0 THEN 1.0::float8 ELSE -1.0::float8 END",
    "extreme": "CASE g % 4 WHEN 0 THEN 0::float8 WHEN 1 THEN 1e308::float8 "
               "WHEN 2 THEN -1e308::float8 ELSE 5e-324::float8 END",
    "seq": "(g::float8)",
}
DISTINCT = (1, 2, 1023, 1024, 1025)
LONG = "'https://example.com/catalog/section/item-' || (g % {d}) || '-standard-edition-variant'"


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
    return query_error(f"no row from {sql}") if row is None else row[0]


def _enc_vectors(dsn, table, code):
    """Vectors of column 1 that chose encoding CODE: the shell's descriptor decode."""
    return _value(dsn, f"""SELECT coalesce(sum(n), 0)::bigint FROM (
        SELECT (SELECT count(*) FROM generate_series(0,
                    get_byte(c.encoding_descriptor, 2) + get_byte(c.encoding_descriptor, 3) * 256
                    + get_byte(c.encoding_descriptor, 4) * 65536
                    + get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
                WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = {code}) AS n
        FROM pgcolumnar.column_chunk c JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
        WHERE s.relation_oid = '{table}'::regclass AND c.column_index = 1) t""")


def _pair(dsn, name, coltype, expr):
    _run(dsn, f"DROP TABLE IF EXISTS {name}_c; DROP TABLE IF EXISTS {name}_h; "
              f"CREATE TABLE {name}_c (id int, v {coltype}) USING pgcolumnar; "
              f"CREATE TABLE {name}_h (id int, v {coltype}); "
              f"INSERT INTO {name}_c SELECT g, {expr} FROM generate_series(1, {ROWS}) g; "
              f"INSERT INTO {name}_h SELECT g, {expr} FROM generate_series(1, {ROWS}) g;")


def _agrees(dsn, name):
    q = "SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM {} t"
    a, b = _value(dsn, q.format(f"{name}_c")), _value(dsn, q.format(f"{name}_h"))
    return isinstance(a, str) and a != "" and not _failed(a) and a == b


def _chose(n):
    return ("yes" if n > 0 else "no") if isinstance(n, int) else n


@pytest.fixture(scope="module")
def ei(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"ALTER DATABASE {DB} SET pgcolumnar.enable_post_codec_encoding_choice = off")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE FUNCTION pgcolumnar.debug_encoding_selftest() RETURNS SETOF text "
                  "AS 'pgcolumnar', 'pgcolumnar_debug_encoding_selftest' LANGUAGE C")
        r = {}
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                lines = [x[0] for x in conn.execute("SELECT * FROM pgcolumnar.debug_encoding_selftest()")]
        except psycopg.Error as exc:
            lines, r["ran"] = [], query_error(_message(exc))
        r["selftest fails"] = [l for l in lines if l.startswith("FAIL")]
        if "ran" not in r:
            ran = [l[len("cases="):] for l in lines if l.startswith("cases=")]
            r["ran"] = int(ran[0]) if ran and ran[0].isdigit() else query_error(f"ran={ran[0] if ran else 'none'}")

        wfail = []
        for k in WIDTHS:
            _pair(dsn, f"bp{k}", "bigint", f"CASE WHEN g = 1 THEN ((1::bigint << {k}) - 1) ELSE (g % 997)::bigint END")
            if not _agrees(dsn, f"bp{k}"):
                wfail.append(str(k))
        r["wfail"] = wfail
        _pair(dsn, "bp64", "bigint", "(('x' || md5(g::text))::bit(64)::bigint)")
        r["bp64"] = "yes" if _agrees(dsn, "bp64") else "no"
        r["for"] = _enc_vectors(dsn, "bp31_c", 2)

        gfail = []
        for p, e in GORILLA.items():
            _pair(dsn, f"g_{p}", "float8", e)
            if not _agrees(dsn, f"g_{p}"):
                gfail.append(p)
        r["gfail"] = gfail
        r["gorilla"] = _enc_vectors(dsn, "g_tiny_c", 4)

        dfail = []
        for d in DISTINCT:
            _pair(dsn, f"d{d}", "text", f"'v' || (g % {d})")
            if not _agrees(dsn, f"d{d}"):
                dfail.append(str(d))
        r["dfail"] = dfail
        r["dict"] = _enc_vectors(dsn, "d2_c", 6)

        _run(dsn, "DROP TABLE IF EXISTS fs_hi; SET pgcolumnar.fsst_min_gain_percent = 0; "
                  "CREATE TABLE fs_hi (id int, v text) USING pgcolumnar; "
                  "INSERT INTO fs_hi SELECT g, md5(g::text) || md5((g * 7)::text) FROM generate_series(1, 20000) g;")
        r["fsst hi"] = _enc_vectors(dsn, "fs_hi", 8)
        lofail = []
        for d in (16, 256, 1000, 1024):
            for shape, e in (("short", f"'v' || (g % {d})"), ("long", LONG.format(d=d))):
                t = f"fs_{d}_{shape}"
                _run(dsn, f"DROP TABLE IF EXISTS {t}; SET pgcolumnar.fsst_min_gain_percent = 0; "
                          f"CREATE TABLE {t} (id int, v text) USING pgcolumnar; "
                          f"INSERT INTO {t} SELECT g, {e} FROM generate_series(1, 20000) g;")
                rows, n = _value(dsn, f"SELECT count(*) FROM {t}"), _enc_vectors(dsn, t, 8)
                if not (rows == 20000 and n == 0):
                    lofail.append(f"{d}/{shape}={n}(rows={rows})")
        r["lofail"] = lofail
        r["compression"] = _value(dsn, "SHOW pgcolumnar.compression")
        r["level"] = _value(dsn, "SHOW pgcolumnar.compression_level")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_encoding_primitives_match_their_reference_implementations(ei, expect):
    if not isinstance(ei["ran"], int):
        got = f"the self-test returned no case count [{ei['ran']}]"
    else:
        got = "none" if not ei["selftest fails"] else "\n".join(ei["selftest fails"][:3])
    expect.text(got, "none", "the encoding primitives match their reference implementations byte for byte")


def test_the_self_test_compared_a_meaningful_number_of_cases(ei, expect):
    n = ei["ran"]
    expect.text("yes" if isinstance(n, int) and n > 500 else f"no (ran={n})", "yes",
                "and the self-test actually compared a meaningful number of cases")


def test_every_reachable_bit_pack_width_round_trips(ei, expect):
    expect.text("none" if not ei["wfail"] else "failed: " + " ".join(ei["wfail"]), "none",
                "every reachable bit-pack width round-trips")


def test_full_range_64_bit_values_round_trip(ei, expect):
    expect.text(ei["bp64"], "yes", "full-range 64-bit values round-trip whatever encoding is chosen")


def test_the_width_fixtures_chose_frame_of_reference_packing(ei, expect):
    expect.text(_chose(ei["for"]), "yes", "and the width fixtures actually chose frame-of-reference packing")


def test_gorilla_float_patterns_round_trip(ei, expect):
    expect.text("none" if not ei["gfail"] else "failed: " + " ".join(ei["gfail"]), "none",
                "gorilla float patterns round-trip, including all-zero XOR")


def test_at_least_one_pattern_chose_gorilla(ei, expect):
    expect.text(_chose(ei["gorilla"]), "yes", "and at least one of them actually chose gorilla")


def test_dictionary_round_trips_around_the_distinct_cap(ei, expect):
    expect.text("none" if not ei["dfail"] else "failed: " + " ".join(ei["dfail"]), "none",
                "dictionary round-trips at, below and above the distinct cap")


def test_the_low_cardinality_fixture_chose_dictionary(ei, expect):
    expect.text(_chose(ei["dict"]), "yes", "and the low-cardinality fixture actually chose dictionary")


def test_a_high_cardinality_corpus_does_select_fsst(ei, expect):
    expect.text(_chose(ei["fsst hi"]), "yes", "control: a high-cardinality corpus does select FSST")


def test_no_vector_selects_fsst_at_or_below_the_distinct_cap(ei, expect):
    expect.text("none" if not ei["lofail"] else "failed: " + " ".join(ei["lofail"]), "none",
                "no vector selects FSST at or below the distinct cap, so skipping its build cannot change output")


def test_the_block_compression_default_is_zstd(ei, expect):
    expect.text(str(ei["compression"]), "zstd", "the block compression default is zstd, pinned to #890 phase 1")


def test_its_level_is_3(ei, expect):
    expect.text(str(ei["level"]), "3", "and its level is 3")

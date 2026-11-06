"""Native cascade encoding (Phase D4, E1 ALP, E2/E3b FSST, sampled selection). The
pytest twin of `test/native_encoding.sh`, every check by the shell's name (#1341).

A native table encodes each 1024-value vector with the adaptive selector and, when a
block codec is set, block-compresses the encoded region. Asserted, against heap
mirrors built from the same generators: every codec round-trips (rows, values, nulls,
a filtered scan, an aggregate); every chunk carries a real descriptor; the constant
column is ENCODED (its vectors' stored type is not NONE), not merely small, because a
block codec shrinks it on its own; ALP is chosen for a two-decimal float and FSST for
shared-substring text, with one shared FSST table per FSST chunk; sampled selection
returns what exhaustive selection returns and costs little ratio on a misleading head
or short runs; and a chunk too small to sample is still encoded, read from its stored
encoding rather than its values.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). A reading that fails is a failed reading, never a value:
two failed reads never agree.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_native_encoding"
GEN = ("SELECT g, 42::bigint, (ARRAY['cat','dog','bird'])[1 + g % 3], md5(g::text), "
       "CASE WHEN g % 7 = 0 THEN NULL ELSE g * 3 END FROM generate_series(1, 6000) g")
FGEN = ("SELECT g, 'https://www.example.com/user/' || g || '/profile?id=' || md5(g::text) "
        "|| '&ref=columnar', ('\\\\x' || md5(g::text) || md5((g+1)::text))::bytea "
        "FROM generate_series(1, 6000) g")
CODECS = ("none", "pglz", "lz4", "zstd")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, *sqls):
    """Run the statements in ONE session (so a SET reaches the INSERT after it)."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in sqls:
                conn.execute(sql)
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _num(dsn, sql):
    """-> the first column of the first row as an int, or the failure as a query_error()."""
    v = _value(dsn, sql)
    try:
        return int(v)
    except ValueError:
        return v if v.startswith(QUERY_ERROR) else query_error(f"not a number: {v}")


def _set_hash(dsn, query):
    """-> the shell's pgc_set_hash: md5 of the sorted row texts, EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _cc(table):
    return f"FROM pgcolumnar.column_chunk WHERE storage_id = pgcolumnar.get_storage_id('{table}')"


def _bad_of(dsn, table, bad, extra=""):
    """-> "bad/total" chunks: a total of 0 can never pass as none bad."""
    return _value(dsn, f"SELECT count(*) FILTER (WHERE {bad}) || '/' || count(*) {_cc(table)} {extra}")


def _yes_if(dsn, sql):
    """-> yes/no from a boolean query, or a query_error()."""
    v = _value(dsn, sql)
    return {"True": "yes", "False": "no"}.get(v, v)


def _ratio_ok(dsn, on, off):
    """-> yes when column 1's total page_length for `on` is within 1.2x of `off`'s, as
    the shell's [ a -le b*12/10 ]."""
    return _yes_if(dsn, f"SELECT (SELECT sum(page_length) {_cc(on)} AND column_index = 1) <= "
                        f"(SELECT ((sum(page_length) * 12) / 10)::bigint {_cc(off)} AND column_index = 1)")


@pytest.fixture(scope="module")
def ne(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE h (id int, k bigint, label text, rnd text, v int)", f"INSERT INTO h {GEN}")
        r = {"h hash": _set_hash(dsn, "SELECT id, k, label, rnd, v FROM h"),
             "h nulls": _num(dsn, "SELECT count(*) FROM h WHERE v IS NULL"),
             "h filt": _set_hash(dsn, "SELECT id, label FROM h WHERE k = 42 AND id BETWEEN 2000 AND 4000"),
             "h sum": _num(dsn, "SELECT sum(v) FROM h")}
        for c in CODECS:
            t = f"n_{c}"
            _run(dsn, f"CREATE TABLE {t} (id int, k bigint, label text, rnd text, v int) USING pgcolumnar",
                 f"SELECT pgcolumnar.set_options('{t}', stripe_row_limit => 1500, compression => '{c}')",
                 f"INSERT INTO {t} {GEN}")
            r[c] = {
                "rows": _num(dsn, f"SELECT count(*) FROM {t}"),
                "hash": _set_hash(dsn, f"SELECT id, k, label, rnd, v FROM {t}"),
                "nulls": _num(dsn, f"SELECT count(*) FROM {t} WHERE v IS NULL"),
                "filt": _set_hash(dsn, f"SELECT id, label FROM {t} WHERE k = 42 AND id BETWEEN 2000 AND 4000"),
                "sum": _num(dsn, f"SELECT sum(v) FROM {t}"),
                "base": _bad_of(dsn, t, "octet_length(encoding_descriptor) < 6"),
                "small": _bad_of(dsn, t, "page_length > 4000", "AND column_index = 1"),
                "raw": _bad_of(dsn, t, "get_byte(encoding_descriptor, 6) = 0", "AND column_index = 1"),
                "codec": _bad_of(dsn, t, "block_codec <> 0"),
            }

        _run(dsn, "CREATE TABLE hp (id int, price float8, real8 float8)",
             "INSERT INTO hp SELECT g, ((g * 37) % 100000) * 0.01, "
             "sqrt(g::float8) * (CASE WHEN g % 2 = 0 THEN -1 ELSE 1 END) FROM generate_series(1, 6000) g",
             "INSERT INTO hp VALUES (6001, '-0'::float8, 'NaN'::float8), "
             "(6002, 'Infinity'::float8, '-Infinity'::float8), (6003, 12.34, 0.0)")
        _run(dsn, "CREATE TABLE np (id int, price float8, real8 float8) USING pgcolumnar",
             "SELECT pgcolumnar.set_options('np', stripe_row_limit => 2048)",
             "INSERT INTO np SELECT id, price, real8 FROM hp")
        special = ("WHERE price = '-0'::float8 OR real8 = 'NaN'::float8 OR price = 'Infinity'::float8")
        r["alp hash"] = (_set_hash(dsn, "SELECT id, price, real8 FROM np"),
                         _set_hash(dsn, "SELECT id, price, real8 FROM hp"))
        r["alp special"] = (_num(dsn, f"SELECT count(*) FROM np {special}"),
                            _num(dsn, f"SELECT count(*) FROM hp {special}"))
        r["alp chosen"] = _yes_if(dsn, f"SELECT count(*) >= 1 {_cc('np')} AND column_index = 1 "
                                       "AND get_byte(encoding_descriptor, 6) = 7")

        _run(dsn, "CREATE TABLE hf (id int, url text, blob bytea)", f"INSERT INTO hf {FGEN}")
        _run(dsn, "CREATE TABLE nf (id int, url text, blob bytea) USING pgcolumnar",
             "SELECT pgcolumnar.set_options('nf', stripe_row_limit => 2048, compression => 'none')",
             f"INSERT INTO nf {FGEN}")
        r["fsst hash"] = (_set_hash(dsn, "SELECT id, url, blob FROM nf"),
                          _set_hash(dsn, "SELECT id, url, blob FROM hf"))
        r["fsst filt"] = (_set_hash(dsn, "SELECT id, url FROM nf WHERE id BETWEEN 3000 AND 3500"),
                          _set_hash(dsn, "SELECT id, url FROM hf WHERE id BETWEEN 3000 AND 3500"))
        r["fsst chosen"] = _yes_if(dsn, f"SELECT count(*) >= 1 {_cc('nf')} AND column_index = 1 "
                                        "AND get_byte(encoding_descriptor, 6) = 8")
        r["fsst shrinks"] = _yes_if(dsn, f"SELECT (SELECT max(page_length) {_cc('nf')} AND column_index = 1) "
                                         "< (SELECT max(octet_length(url)) * 2048 FROM hf)")
        r["fsst table"] = _yes_if(dsn, f"SELECT count(*) >= 1 {_cc('nf')} AND column_index = 1 "
                                       "AND get_byte(encoding_descriptor, 6) = 8 "
                                       "AND octet_length(encoding_descriptor) > 6 + (get_byte(encoding_descriptor, 2) "
                                       "+ get_byte(encoding_descriptor, 3) * 256 + get_byte(encoding_descriptor, 4) * 65536 "
                                       "+ get_byte(encoding_descriptor, 5) * 16777216) * 13 + 4")

        _run(dsn, "CREATE TABLE se_h (id bigint, k int, f float8, txt text)",
             "INSERT INTO se_h SELECT g, (g % 977)::int, (g % 313)::float8 / 7.0, 'v-' || (g % 4096) "
             "FROM generate_series(1, 60000) g")
        _run(dsn, "CREATE TABLE se_off (LIKE se_h) USING pgcolumnar",
             "SET pgcolumnar.encoding_sample_rows = 0", "INSERT INTO se_off SELECT * FROM se_h")
        _run(dsn, "CREATE TABLE se_on (LIKE se_h) USING pgcolumnar",
             "SET pgcolumnar.encoding_sample_rows = 2048", "INSERT INTO se_on SELECT * FROM se_h")
        se = {t: _set_hash(dsn, f"SELECT id, k, f, txt FROM {t}") for t in ("se_on", "se_off", "se_h")}
        r["se"] = se

        _run(dsn, "CREATE TABLE se_head_h (id bigint, v bigint)",
             "INSERT INTO se_head_h SELECT g, CASE WHEN g <= 20000 THEN 7 "
             "ELSE (g * 2654435761) % 1000000000 END FROM generate_series(1, 60000) g")
        _run(dsn, "CREATE TABLE se_head_off (LIKE se_head_h) USING pgcolumnar",
             "SET pgcolumnar.encoding_sample_rows = 0", "INSERT INTO se_head_off SELECT * FROM se_head_h")
        _run(dsn, "CREATE TABLE se_head_on (LIKE se_head_h) USING pgcolumnar",
             "SET pgcolumnar.encoding_sample_rows = 2048", "INSERT INTO se_head_on SELECT * FROM se_head_h")
        r["head ratio"] = _ratio_ok(dsn, "se_head_on", "se_head_off")
        r["head hash"] = (_set_hash(dsn, "SELECT id, v FROM se_head_on"), _set_hash(dsn, "SELECT id, v FROM se_head_h"))

        _run(dsn, "CREATE TABLE se_run_h (id bigint, v bigint)",
             "INSERT INTO se_run_h SELECT g, (g / 30) % 1000 FROM generate_series(1, 200000) g")
        _run(dsn, "CREATE TABLE se_run_off (LIKE se_run_h) USING pgcolumnar",
             "SET pgcolumnar.chunk_group_row_limit = 100000", "SET pgcolumnar.encoding_sample_rows = 0",
             "INSERT INTO se_run_off SELECT * FROM se_run_h")
        _run(dsn, "CREATE TABLE se_run_on (LIKE se_run_h) USING pgcolumnar",
             "SET pgcolumnar.chunk_group_row_limit = 100000", "SET pgcolumnar.encoding_sample_rows = 2048",
             "INSERT INTO se_run_on SELECT * FROM se_run_h")
        r["run ratio"] = _ratio_ok(dsn, "se_run_on", "se_run_off")
        r["run hash"] = (_set_hash(dsn, "SELECT id, v FROM se_run_on"), _set_hash(dsn, "SELECT id, v FROM se_run_h"))

        _run(dsn, "CREATE TABLE se_tiny (LIKE se_run_h) USING pgcolumnar",
             "SET pgcolumnar.encoding_sample_rows = 8", "INSERT INTO se_tiny SELECT * FROM se_run_h")
        r["tiny ratio"] = _ratio_ok(dsn, "se_tiny", "se_run_off")

        _run(dsn, "CREATE TABLE se_small (id bigint, v bigint) USING pgcolumnar",
             "SET pgcolumnar.encoding_sample_rows = 2048",
             "INSERT INTO se_small SELECT g, g * 3 FROM generate_series(1, 500) g")
        r["small enc"] = _bad_of(dsn, "se_small", "get_byte(encoding_descriptor, 6) = 0", "AND column_index = 1")
        r["small vals"] = _value(dsn, "SELECT count(*) || ' ' || sum(v) FROM se_small")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_none_holds_every_row(ne, expect):
    expect.num(ne["none"]["rows"], 6000, "[none] row count")


def test_none_round_trips_every_column(ne, expect):
    expect.text(ne["none"]["hash"], ne["h hash"], "[none] all columns round-trip")


def test_none_round_trips_the_nulls(ne, expect):
    expect.num(ne["none"]["nulls"], ne["h nulls"], "[none] nulls round-trip")


def test_none_filtered_scan_agrees(ne, expect):
    expect.text(ne["none"]["filt"], ne["h filt"], "[none] filtered scan")


def test_none_aggregate_agrees(ne, expect):
    expect.num(ne["none"]["sum"], ne["h sum"], "[none] aggregate over encoded column")


def test_none_carries_no_baseline_descriptor(ne, expect):
    expect.text(ne["none"]["base"], "0/20", "[none] no baseline descriptors remain")


def test_none_constant_column_is_small(ne, expect):
    expect.text(ne["none"]["small"], "0/4", "[none] constant column encoded small")


def test_none_constant_column_is_encoded_not_raw(ne, expect):
    expect.text(ne["none"]["raw"], "0/4", "[none] the constant column's vectors are encoded, not stored raw")


def test_none_uses_no_block_codec(ne, expect):
    expect.text(ne["none"]["codec"], "0/20", "[none] no block codec used")


def test_pglz_holds_every_row(ne, expect):
    expect.num(ne["pglz"]["rows"], 6000, "[pglz] row count")


def test_pglz_round_trips_every_column(ne, expect):
    expect.text(ne["pglz"]["hash"], ne["h hash"], "[pglz] all columns round-trip")


def test_pglz_round_trips_the_nulls(ne, expect):
    expect.num(ne["pglz"]["nulls"], ne["h nulls"], "[pglz] nulls round-trip")


def test_pglz_filtered_scan_agrees(ne, expect):
    expect.text(ne["pglz"]["filt"], ne["h filt"], "[pglz] filtered scan")


def test_pglz_aggregate_agrees(ne, expect):
    expect.num(ne["pglz"]["sum"], ne["h sum"], "[pglz] aggregate over encoded column")


def test_pglz_carries_no_baseline_descriptor(ne, expect):
    expect.text(ne["pglz"]["base"], "0/20", "[pglz] no baseline descriptors remain")


def test_pglz_constant_column_is_small(ne, expect):
    expect.text(ne["pglz"]["small"], "0/4", "[pglz] constant column encoded small")


def test_pglz_constant_column_is_encoded_not_raw(ne, expect):
    expect.text(ne["pglz"]["raw"], "0/4", "[pglz] the constant column's vectors are encoded, not stored raw")


def test_lz4_holds_every_row(ne, expect):
    expect.num(ne["lz4"]["rows"], 6000, "[lz4] row count")


def test_lz4_round_trips_every_column(ne, expect):
    expect.text(ne["lz4"]["hash"], ne["h hash"], "[lz4] all columns round-trip")


def test_lz4_round_trips_the_nulls(ne, expect):
    expect.num(ne["lz4"]["nulls"], ne["h nulls"], "[lz4] nulls round-trip")


def test_lz4_filtered_scan_agrees(ne, expect):
    expect.text(ne["lz4"]["filt"], ne["h filt"], "[lz4] filtered scan")


def test_lz4_aggregate_agrees(ne, expect):
    expect.num(ne["lz4"]["sum"], ne["h sum"], "[lz4] aggregate over encoded column")


def test_lz4_carries_no_baseline_descriptor(ne, expect):
    expect.text(ne["lz4"]["base"], "0/20", "[lz4] no baseline descriptors remain")


def test_lz4_constant_column_is_small(ne, expect):
    expect.text(ne["lz4"]["small"], "0/4", "[lz4] constant column encoded small")


def test_lz4_constant_column_is_encoded_not_raw(ne, expect):
    expect.text(ne["lz4"]["raw"], "0/4", "[lz4] the constant column's vectors are encoded, not stored raw")


def test_zstd_holds_every_row(ne, expect):
    expect.num(ne["zstd"]["rows"], 6000, "[zstd] row count")


def test_zstd_round_trips_every_column(ne, expect):
    expect.text(ne["zstd"]["hash"], ne["h hash"], "[zstd] all columns round-trip")


def test_zstd_round_trips_the_nulls(ne, expect):
    expect.num(ne["zstd"]["nulls"], ne["h nulls"], "[zstd] nulls round-trip")


def test_zstd_filtered_scan_agrees(ne, expect):
    expect.text(ne["zstd"]["filt"], ne["h filt"], "[zstd] filtered scan")


def test_zstd_aggregate_agrees(ne, expect):
    expect.num(ne["zstd"]["sum"], ne["h sum"], "[zstd] aggregate over encoded column")


def test_zstd_carries_no_baseline_descriptor(ne, expect):
    expect.text(ne["zstd"]["base"], "0/20", "[zstd] no baseline descriptors remain")


def test_zstd_constant_column_is_small(ne, expect):
    expect.text(ne["zstd"]["small"], "0/4", "[zstd] constant column encoded small")


def test_zstd_constant_column_is_encoded_not_raw(ne, expect):
    expect.text(ne["zstd"]["raw"], "0/4", "[zstd] the constant column's vectors are encoded, not stored raw")


def test_alp_column_round_trips_exactly(ne, expect):
    expect.text(ne["alp hash"][0], ne["alp hash"][1], "alp column round-trips exactly")


def test_alp_special_values_round_trip(ne, expect):
    expect.num(ne["alp special"][0], ne["alp special"][1], "alp special values round-trip")


def test_alp_is_chosen_for_the_decimal_column(ne, expect):
    expect.text(ne["alp chosen"], "yes", "alp chosen for the decimal column")


def test_fsst_column_round_trips_exactly(ne, expect):
    expect.text(ne["fsst hash"][0], ne["fsst hash"][1], "fsst column round-trips exactly")


def test_fsst_filtered_scan_round_trips(ne, expect):
    expect.text(ne["fsst filt"][0], ne["fsst filt"][1], "fsst filtered scan round-trips")


def test_fsst_is_chosen_for_the_shared_substring_column(ne, expect):
    expect.text(ne["fsst chosen"], "yes", "fsst chosen for the shared-substring column")


def test_fsst_shrinks_the_url_column(ne, expect):
    expect.text(ne["fsst shrinks"], "yes", "fsst shrinks the url column")


def test_fsst_stores_one_shared_table_per_chunk(ne, expect):
    expect.text(ne["fsst table"], "yes", "fsst stores one shared table per chunk (E3b)")


def test_sampling_on_matches_the_heap_oracle(ne, expect):
    expect.text(ne["se"]["se_on"], ne["se"]["se_h"], "sampling on == heap oracle")


def test_sampling_off_matches_the_heap_oracle(ne, expect):
    expect.text(ne["se"]["se_off"], ne["se"]["se_h"], "sampling off == heap oracle")


def test_sampling_changes_nothing_a_query_sees(ne, expect):
    expect.text(ne["se"]["se_on"], ne["se"]["se_off"], "sampling changes nothing a query can see")


def test_a_misleading_head_costs_little_ratio(ne, expect):
    expect.text(ne["head ratio"], "yes", "a misleading column head does not cost much ratio")


def test_a_misleading_head_reads_back_exactly(ne, expect):
    expect.text(ne["head hash"][0], ne["head hash"][1], "misleading-head column still reads back exactly")


def test_a_run_length_column_is_not_misjudged(ne, expect):
    expect.text(ne["run ratio"], "yes", "a run-length column is not misjudged by the sample")


def test_a_run_length_column_reads_back_exactly(ne, expect):
    expect.text(ne["run hash"][0], ne["run hash"][1], "run-length column reads back exactly")


def test_a_sample_below_the_floor_still_encodes(ne, expect):
    expect.text(ne["tiny ratio"], "yes", "a sample setting below the floor still encodes")


def test_a_chunk_smaller_than_the_sample_window_is_encoded(ne, expect):
    expect.text(ne["small enc"], "0/1", "a chunk smaller than the sample window still encodes")


def test_a_chunk_smaller_than_the_sample_window_reads_back(ne, expect):
    expect.text(ne["small vals"], "500 375750", "a chunk smaller than the sample window reads back exactly")

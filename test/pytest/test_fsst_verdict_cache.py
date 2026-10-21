"""The FSST keep/drop verdict is cached with an age bound (#472). The pytest twin of
`test/fsst_verdict_cache.sh`, every check by the shell's name (#1341).

A cached verdict must not change one stored byte: prose (FSST helps) and md5 (FSST
hurts) loads at pgcolumnar.fsst_verdict_reuse 16 store the same chunks as at 0, read
from the catalog rather than the relation file. A bound of one row group re-decides
every time. On a column that changes character mid-load, a bounded cache tracks the
uncached decision more closely than an unbounded one. Values match a heap loaded from
the same generator, and caching makes an md5 load at least 10% faster.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). A decline and a value comparison carry their row counts,
and a timed load must have loaded, so an empty or failed load cannot pass.
"""
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_fsst_verdict_cache"
ROWS = 600000
NGROUPS = 6
PROSE = "'the quick brown fox jumps over the lazy dog number ' || g || ' in the morning'"
MD5 = "md5(g::text)"
CHANGING = f"CASE WHEN g <= {ROWS // 2} THEN {PROSE} ELSE {MD5} END"
HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _load(dsn, tbl, expr, reuse):
    """-> elapsed ms of the INSERT, as the shell times it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            conn.execute(f"CREATE TABLE {tbl} (t text) USING pgcolumnar")
            conn.execute(f"SELECT pgcolumnar.set_options('{tbl}', stripe_row_limit => {ROWS // NGROUPS})")
    except psycopg.Error:
        return 0
    t0 = time.perf_counter()
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SET pgcolumnar.fsst_verdict_reuse = {reuse}")
            conn.execute(f"INSERT INTO {tbl} SELECT {expr} FROM generate_series(1,{ROWS}) g")
    except psycopg.Error:
        pass
    return int((time.perf_counter() - t0) * 1000)


def _fingerprint(dsn, tbl):
    return _value(dsn, "SELECT md5(string_agg(c.group_number || ':' || c.column_index || ':' || c.value_count "
                       "|| ':' || encode(c.encoding_descriptor, 'hex') || ':' || c.block_codec || ':' || "
                       "c.page_length, '|' ORDER BY c.group_number, c.column_index)) "
                       "FROM pgcolumnar.column_chunk c JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id "
                       f"WHERE s.relation_oid = '{tbl}'::regclass")


def _fsst(dsn, tbl):
    return _value(dsn, f"""SELECT coalesce(sum(n), 0)::bigint FROM (
        SELECT (SELECT count(*) FROM generate_series(0,
                    get_byte(c.encoding_descriptor, 2) + get_byte(c.encoding_descriptor, 3) * 256
                    + get_byte(c.encoding_descriptor, 4) * 65536
                    + get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
                WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = 8) AS n
        FROM pgcolumnar.column_chunk c JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
        WHERE s.relation_oid = '{tbl}'::regclass) t""")


def _rows(dsn, tbl):
    return _value(dsn, f"SELECT count(*) FROM {tbl}")


def _bytes(dsn, tbl):
    return _value(dsn, "SELECT sum(page_length)::bigint FROM pgcolumnar.column_chunk c "
                       "JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id "
                       f"WHERE s.relation_oid = '{tbl}'::regclass")


def _counted_hash(dsn, sql):
    n, h = _value(dsn, f"SELECT count(*) FROM ({sql}) _c"), _value(dsn, HASH.format(sql))
    return query_error(f"no counted hash: {n} / {h}") if _failed(n) or _failed(h) else f"{n}:{h}"


def _expected(dsn, sql):
    h = _value(dsn, HASH.format(sql))
    return h if _failed(h) else f"{ROWS}:{h}"


@pytest.fixture(scope="module")
def fv(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _load(dsn, "fv_prose_off", PROSE, 0)
        r["prose rows"] = _rows(dsn, "fv_prose_off")
        r["prose groups"] = _value(dsn, "SELECT count(DISTINCT group_number) FROM pgcolumnar.column_chunk c "
                                        "JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id "
                                        "WHERE s.relation_oid = 'fv_prose_off'::regclass")
        _load(dsn, "fv_md5_off", MD5, 0)
        r["md5 rows"] = _rows(dsn, "fv_md5_off")
        r["prose fsst"] = _fsst(dsn, "fv_prose_off")
        r["md5 off"] = f"{_rows(dsn, 'fv_md5_off')}/{_fsst(dsn, 'fv_md5_off')}"

        _load(dsn, "fv_prose_on", PROSE, 16)
        r["prose fp"] = (_fingerprint(dsn, "fv_prose_on"), _fingerprint(dsn, "fv_prose_off"))
        r["prose on fsst"] = _fsst(dsn, "fv_prose_on")
        _load(dsn, "fv_md5_on", MD5, 16)
        r["md5 fp"] = (_fingerprint(dsn, "fv_md5_on"), _fingerprint(dsn, "fv_md5_off"))
        r["md5 on"] = f"{_rows(dsn, 'fv_md5_on')}/{_fsst(dsn, 'fv_md5_on')}"

        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE fv_heap (t text)")
            c.execute(f"INSERT INTO fv_heap SELECT {PROSE} FROM generate_series(1,{ROWS}) g")
        r["values"] = _counted_hash(dsn, "SELECT t FROM fv_prose_on")
        r["values want"] = _expected(dsn, "SELECT t FROM fv_heap")

        _load(dsn, "fv_prose_one", PROSE, 1)
        r["one fp"] = (_fingerprint(dsn, "fv_prose_one"), r["prose fp"][1])

        _load(dsn, "fv_chg_off", CHANGING, 0)
        _load(dsn, "fv_chg_bounded", CHANGING, 2)
        _load(dsn, "fv_chg_unbounded", CHANGING, 1000000)
        r["chg"] = tuple(_bytes(dsn, t) for t in ("fv_chg_off", "fv_chg_bounded", "fv_chg_unbounded"))
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE fv_chg_heap (t text)")
            c.execute(f"INSERT INTO fv_chg_heap SELECT {CHANGING} FROM generate_series(1,{ROWS}) g")
        r["chg values"] = _counted_hash(dsn, "SELECT t FROM fv_chg_bounded")
        r["chg values want"] = _expected(dsn, "SELECT t FROM fv_chg_heap")

        t_off = _load(dsn, "fv_time_off", MD5, 0)
        t_on = _load(dsn, "fv_time_on", MD5, 16)
        r["time"] = (t_on, t_off, _rows(dsn, "fv_time_on"), _rows(dsn, "fv_time_off"))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _keeps(n):
    return ("yes" if n > 0 else f"no ({n})") if isinstance(n, int) else f"no ({n})"


def _distances(chg):
    off, bounded, unbounded = chg
    if not all(isinstance(v, int) for v in chg):
        return None
    return abs(bounded - off), abs(unbounded - off)


def test_the_prose_fixture_loaded(fv, expect):
    expect.text(str(fv["prose rows"]), str(ROWS), "premise: the prose fixture loaded")


def test_it_spans_several_row_groups(fv, expect):
    n = fv["prose groups"]
    expect.text("yes" if isinstance(n, int) and n >= 3 else f"no ({n})", "yes",
                "premise: and it spans several row groups, so caching has something to reuse")


def test_the_md5_fixture_loaded(fv, expect):
    expect.text(str(fv["md5 rows"]), str(ROWS), "premise: the md5 fixture loaded")


def test_the_prose_corpus_keeps_fsst(fv, expect):
    expect.text(_keeps(fv["prose fsst"]), "yes", "premise: the prose corpus KEEPS fsst, so the HELPS branch is exercised")


def test_the_md5_corpus_drops_it(fv, expect):
    expect.text(fv["md5 off"], f"{ROWS}/0", "premise: and the md5 corpus DROPS it, so the two arms are different branches")


def test_a_reused_helps_verdict_stores_byte_identical_chunks(fv, expect):
    on, off = fv["prose fp"]
    expect.text(str(on), str(off), "a reused HELPS verdict stores byte-identical chunks")


def test_and_it_still_keeps_fsst(fv, expect):
    expect.text(_keeps(fv["prose on fsst"]), "yes", "and it still keeps fsst, rather than silently dropping it")


def test_a_reused_hurts_verdict_stores_byte_identical_chunks(fv, expect):
    on, off = fv["md5 fp"]
    expect.text(str(on), str(off), "a reused HURTS verdict stores byte-identical chunks")


def test_and_it_still_declines_fsst(fv, expect):
    expect.text(fv["md5 on"], f"{ROWS}/0", "and it still declines fsst")


def test_the_cached_load_returns_the_same_values_as_heap(fv, expect):
    expect.text(str(fv["values"]), str(fv["values want"]), "the cached load returns the same values as heap")


def test_a_reuse_bound_of_one_row_group_is_byte_identical_to_no_caching(fv, expect):
    one, off = fv["one fp"]
    expect.text(str(one), str(off), "a reuse bound of one row group is byte-identical to no caching")


def test_the_three_changing_column_loads_all_produced_bytes(fv, expect):
    expect.text("yes" if _distances(fv["chg"]) is not None else "no", "yes",
                "premise: the three changing-column loads all produced bytes to compare")


def test_an_unbounded_cache_really_does_diverge(fv, expect):
    d = _distances(fv["chg"])
    expect.text("yes" if d and d[1] > 0 else "no (the fixture does not change character)", "yes",
                "an unbounded cache really does diverge on this fixture")


def test_a_bounded_cache_tracks_the_change_more_closely(fv, expect):
    d = _distances(fv["chg"])
    expect.text("yes" if d and d[0] < d[1] else f"no ({d})", "yes",
                "a bounded cache tracks the change more closely than an unbounded one")


def test_the_changing_column_still_returns_its_values(fv, expect):
    expect.text(str(fv["chg values"]), str(fv["chg values want"]), "and the changing column still returns its values")


def test_caching_the_verdict_makes_the_load_measurably_faster(fv, expect):
    t_on, t_off, rows_on, rows_off = fv["time"]
    if rows_on != ROWS or rows_off != ROWS:
        got = f"a timed load did not load ({rows_on} / {rows_off} rows)"
    else:
        got = "yes" if t_on < t_off * 90 // 100 else f"no ({t_on} vs {t_off} ms)"
    expect.text(got, "yes", "caching the verdict makes the load measurably faster")

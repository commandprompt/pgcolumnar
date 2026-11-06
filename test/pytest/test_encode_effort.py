"""The per-table encode_effort option (#155). The pytest twin of `test/encode_effort.sh`,
every check by the shell's name (#1341).

encode_effort = fast skips the FSST substring search. The option is validated and
stored, and so is its sibling, compression. On md5 text, a shape where FSST wins, fast
stores more bytes than full; on a shape where FSST loses they store the same; either
way the rows, a filtered read and an index fetch agree. A table with no option stores
what an explicit 'full' stores, and reset_options clears the option. No check times
anything: stored bytes are deterministic.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order, with
pgcolumnar.fsst_min_gain_percent held at 0 on that database as the shell holds it; each
test asserts one reading (#1339). A size is a reading only when it is a positive
number, and each content reading carries its row count against the fixture's, so two
empty or two failed readings cannot agree.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_encode_effort"
ROWS = 200000


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _run(dsn, sql):
    """-> "" on success, else the error's first line."""
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


def _stored(dsn, table):
    return _value(dsn, "SELECT coalesce(sum(page_length), 0)::bigint FROM pgcolumnar.column_chunk "
                       f"WHERE storage_id = pgcolumnar.get_storage_id('{table}')")


def _size(v, table):
    return v if isinstance(v, int) and v > 0 else query_error(f"no size of {table} read [{v}]")


def _option(dsn, column):
    v = _value(dsn, f"SELECT {column} FROM pgcolumnar.options WHERE regclass = 'ef_a'::regclass")
    return v if _failed(v) or v is None else str(v)


@pytest.fixture(scope="module")
def ef(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"ALTER DATABASE {DB} SET pgcolumnar.fsst_min_gain_percent = 0")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _run(dsn, "CREATE TABLE ef_a (v text) USING pgcolumnar")
        r["bad"] = _run(dsn, "SELECT pgcolumnar.set_options('ef_a', encode_effort => 'turbo')")
        r["bad stored"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.options "
                                      "WHERE regclass = 'ef_a'::regclass AND encode_effort IS NOT NULL")
        _run(dsn, "SELECT pgcolumnar.set_options('ef_a', encode_effort => 'fast')")
        r["stored"] = _option(dsn, "encode_effort")
        _run(dsn, "SELECT pgcolumnar.set_options('ef_a', compression => 'zstd')")
        r["badc"] = _run(dsn, "SELECT pgcolumnar.set_options('ef_a', compression => 'brotli')")
        r["comp"] = _option(dsn, "compression")

        _run(dsn, "CREATE TABLE ef_full (v text) USING pgcolumnar; CREATE TABLE ef_fast (v text) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('ef_full', encode_effort => 'full'); "
                  "SELECT pgcolumnar.set_options('ef_fast', encode_effort => 'fast');")
        for t in ("ef_full", "ef_fast"):
            _run(dsn, f"INSERT INTO {t} SELECT md5(g::text) FROM generate_series(1, {ROWS}) g")
        r["full"], r["fast"] = _stored(dsn, "ef_full"), _stored(dsn, "ef_fast")

        _run(dsn, "CREATE TABLE ef_flat (v text) USING pgcolumnar; CREATE TABLE ef_flatf (v text) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('ef_flatf', encode_effort => 'fast');")
        for t in ("ef_flat", "ef_flatf"):
            _run(dsn, f"INSERT INTO {t} SELECT repeat('a', 8) FROM generate_series(1, {ROWS}) g")
        r["flat"], r["flatf"] = _size(_stored(dsn, "ef_flat"), "ef_flat"), _size(_stored(dsn, "ef_flatf"), "ef_flatf")

        rows = "SELECT count(*) || ':' || md5(string_agg(v, ',' ORDER BY v)) FROM {}"
        like = "SELECT count(*) FILTER (WHERE v LIKE 'a%') || '/' || count(*) FROM {}"
        r["rows fast"], full = _value(dsn, rows.format("ef_fast")), _value(dsn, rows.format("ef_full"))
        r["rows full"] = full if str(full).split(":")[0] == str(ROWS) else query_error(
            f"ef_full does not hold the fixture: [{full}]")
        r["like fast"], full = _value(dsn, like.format("ef_fast")), _value(dsn, like.format("ef_full"))
        r["like full"] = full if str(full).split("/")[-1] == str(ROWS) else query_error(
            f"ef_full does not hold the fixture: [{full}]")

        _run(dsn, "CREATE INDEX ef_fast_v ON ef_fast (v)")
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("SET enable_seqscan = off")
                conn.execute("SET pgcolumnar.enable_custom_scan = off")
                r["fetch"] = conn.execute("SELECT count(*) FROM ef_fast WHERE v = md5('12345')").fetchone()[0]
        except psycopg.Error as exc:
            r["fetch"] = query_error(_message(exc))

        _run(dsn, "CREATE TABLE ef_def (v text) USING pgcolumnar; "
                  f"INSERT INTO ef_def SELECT md5(g::text) FROM generate_series(1, {ROWS}) g;")
        r["def"] = _size(_stored(dsn, "ef_def"), "ef_def")
        _run(dsn, "SELECT pgcolumnar.reset_options('ef_fast', encode_effort => true)")
        r["reset"] = _value(dsn, "SELECT coalesce(encode_effort::text, 'null') FROM pgcolumnar.options "
                                 "WHERE regclass = 'ef_fast'::regclass")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_an_unknown_encode_effort_is_refused(ef, expect):
    bad = ef["bad"]
    expect.text("yes" if "unknown columnar encode_effort" in bad else f"no ({bad})", "yes",
                "an unknown encode_effort is refused")


def test_the_refused_value_was_not_stored(ef, expect):
    expect.text(str(ef["bad stored"]), "0", "and the refused value was not stored")


def test_a_valid_encode_effort_is_stored(ef, expect):
    expect.text(str(ef["stored"]), "fast", "a valid encode_effort is stored")


def test_an_unknown_compression_name_is_refused(ef, expect):
    bad = ef["badc"]
    expect.text("yes" if 'unknown columnar compression "brotli"' in bad else f"no ({bad})", "yes",
                "an unknown compression name is refused")


def test_the_compression_set_before_it_is_still_the_one_stored(ef, expect):
    expect.text(str(ef["comp"]), "zstd", "and the compression set before it is still the one stored")


def test_fast_stores_more_than_full_where_fsst_wins(ef, expect):
    full, fast = ef["full"], ef["fast"]
    ok = isinstance(full, int) and isinstance(fast, int) and full > 0 and fast > full
    expect.text("yes" if ok else f"no ({fast} vs {full})", "yes",
                "fast stores more than full on a shape where FSST wins")


def test_and_is_identical_where_fsst_does_not_win(ef, expect):
    expect.text(str(ef["flat"]), str(ef["flatf"]), "and is identical where FSST does not win")


def test_the_rows_are_identical_either_way(ef, expect):
    expect.text(str(ef["rows fast"]), str(ef["rows full"]), "the rows are identical either way")


def test_a_filtered_read_agrees(ef, expect):
    expect.text(str(ef["like fast"]), str(ef["like full"]), "and a filtered read agrees")


def test_an_index_fetch_finds_a_fast_written_row(ef, expect):
    expect.text(str(ef["fetch"]), "1", "and an index fetch finds a fast-written row")


def test_a_table_with_no_option_set_matches_full(ef, expect):
    full = _size(ef["full"], "ef_full")
    expect.text(str(ef["def"]), str(full), "a table with no option set matches full")


def test_reset_options_clears_it(ef, expect):
    expect.text(str(ef["reset"]), "null", "reset_options clears it")

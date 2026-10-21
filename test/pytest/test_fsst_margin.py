"""The FSST keep/drop cost margin, pgcolumnar.fsst_min_gain_percent (#155, #271). The
pytest twin of `test/fsst_margin.sh`, every check by the shell's name (#1341).

On a corpus where FSST barely wins, margin 0 keeps FSST and margin 90 drops it, read
from the encoding descriptor; the shipped default drops it too. With no block codec
the keep test returns before it reads the margin, so FSST is kept even at the
maximum. And the invariant: tables loaded at margin 0, at 90 and at the default read
back exactly what a heap mirror holds, NULLs and empty strings included -- over a
corpus whose margin-0 copy really is stored as FSST, which a check asserts first.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). Each drop carries its table's row count, so an empty table
cannot read as one, and two failed readings cannot agree.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_fsst_margin"
ROWS = 20000
DECIDE = "md5(g::text) || md5((g * 7)::text)"
CONTENT = ("CASE g % 97 WHEN 0 THEN NULL WHEN 1 THEN '' WHEN 2 THEN 'ab' "
           "ELSE md5(g::text) || md5((g * 7)::text) END")


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


def _load(dsn, table, margin, corpus, codec=None):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            if margin is not None:
                conn.execute(f"SET pgcolumnar.fsst_min_gain_percent = {margin}")
            if codec is not None:
                conn.execute(f"SET pgcolumnar.compression = '{codec}'")
            conn.execute(f"CREATE TABLE {table} (id int, v text) USING pgcolumnar")
            conn.execute(f"INSERT INTO {table} SELECT g, {corpus} FROM generate_series(1, {ROWS}) g")
    except psycopg.Error:
        pass


def _fsst_vectors(dsn, table):
    return _value(dsn, f"""SELECT coalesce(sum(n), 0)::bigint FROM (
        SELECT (SELECT count(*) FROM generate_series(0,
                    get_byte(c.encoding_descriptor, 2) + get_byte(c.encoding_descriptor, 3) * 256
                    + get_byte(c.encoding_descriptor, 4) * 65536
                    + get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
                WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = 8) AS n
        FROM pgcolumnar.column_chunk c JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
        WHERE s.relation_oid = '{table}'::regclass AND c.column_index = 1) t""")


def _rows_and(dsn, table, n):
    return f"{_value(dsn, f'SELECT count(*) FROM {table}')}/{n}"


def _row_hash(dsn, table):
    return _value(dsn, f"SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM {table} t")


def _some(n):
    return ("yes" if n > 0 else "no") if isinstance(n, int) else n


@pytest.fixture(scope="module")
def fm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE fm_h (id int, v text)")
            c.execute(f"INSERT INTO fm_h SELECT g, {CONTENT} FROM generate_series(1, {ROWS}) g")
        r = {"heap": _row_hash(dsn, "fm_h")}
        _load(dsn, "fm_keep", 0, DECIDE)
        r["kept"] = _fsst_vectors(dsn, "fm_keep")
        _load(dsn, "fm_drop", 90, DECIDE)
        r["dropped"] = _fsst_vectors(dsn, "fm_drop")
        r["drop rows"] = _rows_and(dsn, "fm_drop", r["dropped"])
        _load(dsn, "fm_default", None, DECIDE)
        r["default"] = _rows_and(dsn, "fm_default", _fsst_vectors(dsn, "fm_default"))
        _load(dsn, "fm_none", 90, DECIDE, "none")
        r["none"] = _fsst_vectors(dsn, "fm_none")
        _load(dsn, "fm_none99", 99, DECIDE, "none")
        r["none99"] = _fsst_vectors(dsn, "fm_none99")
        for t, m in (("fm_c0", 0), ("fm_c90", 90), ("fm_cd", None)):
            _load(dsn, t, m, CONTENT)
        r["c0 fsst"] = _fsst_vectors(dsn, "fm_c0")
        for t in ("fm_c0", "fm_c90", "fm_cd"):
            r[f"{t} hash"] = _row_hash(dsn, t)
        for t in ("fm_c0", "fm_c90", "fm_h"):
            r[f"{t} nulls"] = _value(dsn, f"SELECT count(*) FROM {t} WHERE v IS NULL")
            r[f"{t} empties"] = _value(dsn, f"SELECT count(*) FROM {t} WHERE v = ''")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_heap_mirror_has_content_to_compare_against(fm, expect):
    expect.text("no" if _failed(fm["heap"]) else "yes", "yes", "the heap mirror has content to compare against")


def test_at_margin_0_this_corpus_keeps_fsst(fm, expect):
    expect.text(_some(fm["kept"]), "yes", "at margin 0 this corpus keeps FSST")


def test_at_margin_90_the_same_corpus_drops_fsst(fm, expect):
    expect.text(fm["drop rows"], f"{ROWS}/0", "at margin 90 the same corpus drops FSST")


def test_the_margin_is_what_changed_the_decision(fm, expect):
    k, d = fm["kept"], fm["dropped"]
    ok = isinstance(k, int) and isinstance(d, int) and k > d
    expect.text("yes" if ok else f"no (kept {k}, dropped {d})", "yes",
                "so the margin is what changed the decision, not the corpus")


def test_the_shipped_default_drops_fsst_on_a_marginal_corpus(fm, expect):
    expect.text(fm["default"], f"{ROWS}/0", "the shipped default drops FSST on a marginal corpus")


def test_and_0_still_keeps_it(fm, expect):
    expect.text(_some(fm["kept"]), "yes", "and 0 still keeps it, so the default is a decision and not a rewrite")


def test_the_paired_arm_dropped_fsst_at_this_margin_with_a_codec(fm, expect):
    expect.text(fm["drop rows"], f"{ROWS}/0", "the paired arm dropped FSST at this margin with a codec")


def test_with_no_codec_the_same_corpus_keeps_fsst_at_the_same_margin(fm, expect):
    expect.text(_some(fm["none"]), "yes", "with no codec the same corpus keeps FSST at the same margin")


def test_and_at_the_maximum_margin_too(fm, expect):
    expect.text(_some(fm["none99"]), "yes",
                "and at the maximum margin too, because the test returns before reading it")


def test_the_margin_0_content_copy_really_is_stored_as_fsst(fm, expect):
    n = fm["c0 fsst"]
    expect.text("yes" if isinstance(n, int) and n > 0 else f"no ({n})", "yes",
                "the margin-0 content copy really is stored as FSST, so the invariant reads FSST")


def test_fm_c0_reads_back_exactly_what_the_heap_holds(fm, expect):
    expect.text(str(fm["fm_c0 hash"]), str(fm["heap"]), "fm_c0 reads back exactly what the heap holds")


def test_fm_c90_reads_back_exactly_what_the_heap_holds(fm, expect):
    expect.text(str(fm["fm_c90 hash"]), str(fm["heap"]), "fm_c90 reads back exactly what the heap holds")


def test_fm_cd_reads_back_exactly_what_the_heap_holds(fm, expect):
    expect.text(str(fm["fm_cd hash"]), str(fm["heap"]), "fm_cd reads back exactly what the heap holds")


def test_fm_c0_preserves_nulls(fm, expect):
    expect.text(str(fm["fm_c0 nulls"]), str(fm["fm_h nulls"]), "fm_c0 preserves NULLs")


def test_fm_c0_preserves_empty_strings(fm, expect):
    expect.text(str(fm["fm_c0 empties"]), str(fm["fm_h empties"]), "fm_c0 preserves empty strings")


def test_fm_c90_preserves_nulls(fm, expect):
    expect.text(str(fm["fm_c90 nulls"]), str(fm["fm_h nulls"]), "fm_c90 preserves NULLs")


def test_fm_c90_preserves_empty_strings(fm, expect):
    expect.text(str(fm["fm_c90 empties"]), str(fm["fm_h empties"]), "fm_c90 preserves empty strings")

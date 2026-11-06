"""approx_count_distinct answered from the stored sketches (#1309 phase 3). The pytest
twin of `test/native_sketch_fold.sh`, every check by the shell's name.

An ungrouped, unfiltered approx_count_distinct over a column named in sketch_columns
folds each clean row group's stored sketch by a register-wise max, and reads and
hashes only a group the sketches cannot describe: one with deletes, or one with no
sketch row. A register union is exactly the registers of the union, so every answer
must EQUAL the same query with pgcolumnar.enable_custom_scan off (core's Agg over the
plain transition function). EXPLAIN ANALYZE's "Columnar Sketch Groups Folded/Scanned"
tell a fold from a scan that happens to agree.

ON A PRIVATE DATABASE, built once by a module fixture; each test asserts one reading.
"""
import hashlib
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_sketch_fold"
A = "SELECT pgcolumnar.approx_count_distinct(a) FROM f"
T = "SELECT pgcolumnar.approx_count_distinct(t) FROM f"
B = "SELECT pgcolumnar.approx_count_distinct(a) FROM fb"
W = "SELECT pgcolumnar.approx_count_distinct(a) FROM f WHERE b > 5000"
G = "SELECT b % 3, pgcolumnar.approx_count_distinct(a) FROM f GROUP BY 1 ORDER BY 1"
M = "SELECT pgcolumnar.approx_count_distinct(a), sum(b::numeric) FROM f"
C = 'SELECT pgcolumnar.approx_count_distinct(t COLLATE "C") FROM f'


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _rows(dsn, sql, plain=False):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            if plain:
                c.execute("SET pgcolumnar.enable_custom_scan = off")
            return c.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _one(dsn, sql, plain=False):
    rows = _rows(dsn, sql, plain)
    return rows if isinstance(rows, str) else (str(rows[0][0]) if rows else query_error("no row"))


def _same(a, b):
    if not (isinstance(a, str) and a.isdigit()):
        return f"not a number: [{a}] vs [{b}]"
    return "same" if a == b else f"[{a}] vs [{b}]"


def _groups_line(dsn, sql):
    plan = _rows(dsn, "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql)
    if isinstance(plan, str):
        return plan
    text = "\n".join(r[0] for r in plan)
    f = re.search(r"Columnar Sketch Groups Folded: (\d+)", text)
    s = re.search(r"Columnar Sketch Groups Scanned: (\d+)", text)
    if not f and not s:
        return "no fold"
    return f"folded {f.group(1) if f else '?'} scanned {s.group(1) if s else '?'}"


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql.encode())
    except psycopg.Error as exc:
        print(f"-- {sql[:60]}: {_message(exc)}")


@pytest.fixture(scope="module")
def nsf(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE f (a bigint, t text, b int) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('f', stripe_row_limit => 1000, sketch_columns => ARRAY['a', 't']);")
        _run(dsn, "INSERT INTO f SELECT CASE WHEN g <= 50 THEN 100000 + g WHEN g % 97 = 0 THEN NULL ELSE g % 3000 END, "
                  "CASE WHEN g % 89 = 0 THEN NULL ELSE 'k' || (g % 2500) END, g FROM generate_series(1, 10000) g")
        r = {}
        r["premise"] = _one(dsn, "SELECT count(*) || '/' || (SELECT count(*) FROM pgcolumnar.sketch "
                                 "WHERE storage_id = pgcolumnar.get_storage_id('f')) FROM pgcolumnar.row_group "
                                 "WHERE storage_id = pgcolumnar.get_storage_id('f')")
        r["clean line"] = _groups_line(dsn, A)
        r["clean"] = _same(_one(dsn, A), _one(dsn, A, plain=True))
        r["text"] = _same(_one(dsn, T), _one(dsn, T, plain=True))
        r["count"] = (_one(dsn, "SELECT count(*) || '|' || pgcolumnar.approx_count_distinct(a) FROM f"),
                      f"10000|{_one(dsn, A, plain=True)}")
        _run(dsn, "DELETE FROM f WHERE b BETWEEN 1 AND 50")
        r["deleted line"] = _groups_line(dsn, A)
        r["deleted"] = _same(_one(dsn, A), _one(dsn, A, plain=True))
        _run(dsn, "CREATE TABLE fb (a bigint) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('fb', stripe_row_limit => 1000);")
        _run(dsn, "INSERT INTO fb SELECT g % 700 FROM generate_series(1, 2000) g")
        _run(dsn, "SELECT pgcolumnar.set_options('fb', sketch_columns => ARRAY['a'])")
        _run(dsn, "INSERT INTO fb SELECT 500 + g % 700 FROM generate_series(1, 2000) g")
        r["before line"] = _groups_line(dsn, B)
        r["before"] = _same(_one(dsn, B), _one(dsn, B, plain=True))
        r["where line"] = _groups_line(dsn, W)
        r["where"] = _same(_one(dsn, W), _one(dsn, W, plain=True))
        r["group line"] = _groups_line(dsn, G)
        g1, g2 = _rows(dsn, G), _rows(dsn, G, plain=True)
        h = lambda rows: rows if isinstance(rows, str) else hashlib.md5(repr(rows).encode()).hexdigest()
        r["group"] = (h(g1), h(g2))
        r["mixed line"] = _groups_line(dsn, M)
        r["collate line"] = _groups_line(dsn, C)
        r["collate"] = _same(_one(dsn, C), _one(dsn, C, plain=True))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_every_group_carries_its_sketches(nsf, expect):
    expect.text(nsf["premise"], "10/20", "premise: ten row groups, each with a sketch for a and for t")


def test_a_clean_table_folds_every_group(nsf, expect):
    expect.text(nsf["clean line"], "folded 10 scanned 0", "on a clean table every row group is folded from its sketch")


def test_the_fold_answers_the_plain_aggregate(nsf, expect):
    expect.text(nsf["clean"], "same", "and the fold answers what the plain aggregate answers")


def test_a_text_column_folds_to_the_plain_answer(nsf, expect):
    expect.text(nsf["text"], "same", "a text column folds to the plain answer too")


def test_count_star_beside_it_folds(nsf, expect):
    got, want = nsf["count"]
    expect.text(got, want, "count(*) beside it folds and counts every row")


def test_a_group_with_deletes_is_scanned(nsf, expect):
    expect.text(nsf["deleted line"], "folded 9 scanned 1", "a row group with deletes is scanned, the others folded")


def test_the_answer_with_deletes_is_the_plain_one(nsf, expect):
    expect.text(nsf["deleted"], "same", "and the answer is the plain aggregate's over the live rows")


def test_a_group_with_no_sketch_is_scanned(nsf, expect):
    expect.text(nsf["before line"], "folded 2 scanned 2", "a row group with no sketch row is scanned, not counted as empty")


def test_the_answer_without_sketches_is_the_plain_one(nsf, expect):
    expect.text(nsf["before"], "same", "and the answer is the plain aggregate's")


def test_a_where_declines_the_fold(nsf, expect):
    expect.text(nsf["where line"], "no fold", "a WHERE declines the fold")


def test_the_where_answer_is_the_plain_one(nsf, expect):
    expect.text(nsf["where"], "same", "and still answers what the plain aggregate answers")


def test_a_group_by_declines_the_fold(nsf, expect):
    expect.text(nsf["group line"], "no fold", "a GROUP BY declines the fold")


def test_the_group_by_answer_is_the_plain_one(nsf, expect):
    got, want = nsf["group"]
    expect.text(got, want, "and answers what the plain aggregate answers")


def test_an_unanswerable_aggregate_beside_it_declines(nsf, expect):
    expect.text(nsf["mixed line"], "no fold", "an aggregate the metadata cannot answer beside it declines the fold")


def test_a_collate_override_declines_the_fold(nsf, expect):
    expect.text(nsf["collate line"], "no fold", "a COLLATE override declines the fold")


def test_the_collate_answer_is_the_plain_one(nsf, expect):
    expect.text(nsf["collate"], "same", "and the override answers what the plain aggregate answers")

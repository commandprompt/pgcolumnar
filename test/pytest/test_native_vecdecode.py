"""Decode honours the skip vector (#452 phase 1b-i). The pytest twin of
`test/native_vecdecode.sh`, every check by the shell's name (#1341).

One row group of 32 vectors x 1024 rows; id is monotonic, so each vector's zone map is
tight and a narrow range rules most of them out. The central check is that "Columnar
Vectors Decoded" moves by EXACTLY the skipped count, read from one plan. The heap mirror
is written from the generator, never copied from the columnar table.

Each counter must be READ: a failed EXPLAIN is a `query_error()`, never 0, so "decoded
plus skipped is the group's vectors" cannot pass as 0 + 0 = 0. Every comparison of two
readings refuses a failed side. The column w is NULL on every seventh row, so the fold's
use of the validity bitmap is exercised: with the bitmap ignored, the nullable aggregate
reads shifted values.

The poison check ("the fold does not count rows from a vector it never decoded") has teeth
only on assert builds, which poison a skipped vector's bytes; on other builds it is a
parity check.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading.
"""
import hashlib
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_vecdecode"
ROWS = 32768
GEN = (f"SELECT g AS id, (g % 97) AS v, CASE WHEN g % 7 = 0 THEN NULL ELSE g % 89 END AS w "
       f"FROM generate_series(1, {ROWS}) g")
NARROW = "SELECT id, v FROM n WHERE id BETWEEN 2000 AND 2050"
WIDE = f"SELECT id, v FROM n WHERE id BETWEEN 1 AND {ROWS}"
FOLD = ("SET max_parallel_workers_per_gather = 0",
        "SET pgcolumnar.enable_ungrouped_vector_agg = on")
FQ = "SELECT count(*), sum(v) FROM n WHERE id BETWEEN 2000 AND 2050"
FQN = "SELECT count(w), sum(w) FROM n WHERE id BETWEEN 2000 AND 2050"
FQ2 = "SELECT count(*), sum(v) FROM n WHERE id BETWEEN 20000 AND 20050"
POISONQ = "SELECT count(*), sum(v) FROM n WHERE id BETWEEN -2000000000 AND 3000"
EXPLAIN = "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _rows(dsn, sql, sets=()):
    """-> the rows as "|"-joined text like psql -At, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return _failed(exc)
    return ["|".join("" if c is None else str(c) for c in r) for r in rows]


def _one(dsn, sql, sets=()):
    rows = _rows(dsn, sql, sets)
    if not isinstance(rows, list):
        return rows
    return rows[-1] if rows else query_error(f"no row from {sql}")


def _set_hash(dsn, sql):
    """-> an order-free hash of the result set, as the shell's pgc_set_hash, or a failure."""
    rows = _rows(dsn, sql)
    if not isinstance(rows, list):
        return rows
    return hashlib.md5("\n".join(sorted(rows)).encode()).hexdigest()


def _has(plan, label):
    if not isinstance(plan, list):
        return plan
    return "yes" if any(label in ln for ln in plan) else "no"


def _counter(plan, label):
    """-> the integer after `label`, or a failure that names the missing line."""
    if not isinstance(plan, list):
        return plan
    for ln in plan:
        if label in ln:
            m = re.search(r"\d+", ln[ln.index(label) + len(label):])
            if m:
                return m.group(0)
    return query_error(f"no '{label}' line in the plan")


def _fold_flag(plan):
    if not isinstance(plan, list):
        return plan
    for ln in plan:
        m = re.search(r"Columnar Batch Fold: [a-z]+", ln)
        if m:
            return m.group(0)
    return query_error("no 'Columnar Batch Fold' line in the plan")


def _num(x):
    return isinstance(x, str) and x.isdigit()


@pytest.fixture(scope="module")
def vd(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE h (id int, v int, w int)")
        _run(dsn, "CREATE TABLE n (id int, v int, w int) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('n', stripe_row_limit => 65536, chunk_group_row_limit => 1024)")
        _run(dsn, f"INSERT INTO h {GEN}")
        _run(dsn, f"INSERT INTO n {GEN}")
        r = {}
        pn = _rows(dsn, EXPLAIN + NARROW)
        pw = _rows(dsn, EXPLAIN + WIDE)
        r["narrow custom"] = _has(pn, "Columnar Projected Columns")
        r["wide custom"] = _has(pw, "Columnar Projected Columns")
        r["groups"] = _one(dsn, "SELECT count(*) FROM pgcolumnar.row_group "
                                "WHERE storage_id = pgcolumnar.get_storage_id('n')")
        r["pruned"] = _counter(pn, "Columnar Chunk Groups Removed by Filter")
        skip_n = _counter(pn, "Columnar Vectors Skipped")
        skip_w = _counter(pw, "Columnar Vectors Skipped")
        r["narrow skips"] = ("yes" if int(skip_n) > 0 else "no") if _num(skip_n) else skip_n
        r["wide skips"] = skip_w
        r["has decoded"] = _has(pw, "Columnar Vectors Decoded")
        dec_w = _counter(pw, "Columnar Vectors Decoded")
        dec_n = _counter(pn, "Columnar Vectors Decoded")
        r["wide decodes"] = ("yes" if int(dec_w) > 0 else "no") if _num(dec_w) else dec_w
        r["fewer"] = (("yes" if int(dec_n) < int(dec_w) else "no") if _num(dec_n) and _num(dec_w)
                      else query_error(f"no reading [{dec_n}] [{dec_w}]"))
        r["sum"] = (str(int(dec_n) + int(skip_n)) if _num(dec_n) and _num(skip_n)
                    else query_error(f"no reading [{dec_n}] [{skip_n}]"))
        r["sum want"] = dec_w if _num(dec_w) else query_error(f"no wide reading [{dec_w}]")
        r["narrow parity"] = (_set_hash(dsn, NARROW),
                              _set_hash(dsn, "SELECT id, v FROM h WHERE id BETWEEN 2000 AND 2050"))
        r["cross parity"] = (_set_hash(dsn, "SELECT id, v FROM n WHERE id BETWEEN 3000 AND 9000"),
                             _set_hash(dsn, "SELECT id, v FROM h WHERE id BETWEEN 3000 AND 9000"))
        r["edge parity"] = (_set_hash(dsn, "SELECT id, v FROM n WHERE id BETWEEN 1025 AND 2048"),
                            _set_hash(dsn, "SELECT id, v FROM h WHERE id BETWEEN 1025 AND 2048"))
        r["has nulls"] = _one(dsn, "SELECT (count(*) - count(w) > 0)::text FROM h WHERE id BETWEEN 2000 AND 2050")
        r["null parity"] = (_set_hash(dsn, "SELECT id, w FROM n WHERE id BETWEEN 3000 AND 9000"),
                            _set_hash(dsn, "SELECT id, w FROM h WHERE id BETWEEN 3000 AND 9000"))
        r["agg"] = (_one(dsn, "SELECT count(*), sum(v) FROM n WHERE id BETWEEN 2000 AND 2050"),
                    _one(dsn, "SELECT count(*), sum(v) FROM h WHERE id BETWEEN 2000 AND 2050"))
        r["agg many"] = (_one(dsn, "SELECT count(*), sum(v), min(v), max(v) FROM n WHERE id BETWEEN 5000 AND 5200"),
                         _one(dsn, "SELECT count(*), sum(v), min(v), max(v) FROM h WHERE id BETWEEN 5000 AND 5200"))
        r["none"] = _one(dsn, "SELECT count(*) FROM n WHERE id BETWEEN 100000 AND 200000")
        r["fold reach"] = _fold_flag(_rows(dsn, EXPLAIN + FQ, FOLD))
        r["fold"] = (_one(dsn, FQ, FOLD),
                     _one(dsn, "SELECT count(*), sum(v) FROM h WHERE id BETWEEN 2000 AND 2050"))
        r["null fold reach"] = _fold_flag(_rows(dsn, EXPLAIN + FQN, FOLD))
        r["null fold"] = (_one(dsn, FQN, FOLD),
                          _one(dsn, "SELECT count(w), sum(w) FROM h WHERE id BETWEEN 2000 AND 2050"))
        r["deep reach"] = _fold_flag(_rows(dsn, EXPLAIN + FQ2, FOLD))
        r["deep"] = (_one(dsn, FQ2, FOLD),
                     _one(dsn, "SELECT count(*), sum(v) FROM h WHERE id BETWEEN 20000 AND 20050"))
        r["poison reach"] = _fold_flag(_rows(dsn, EXPLAIN + POISONQ, FOLD))
        r["fold decoded"] = _counter(_rows(dsn, EXPLAIN + POISONQ, FOLD), "Columnar Vectors Decoded")
        r["fold all"] = _counter(_rows(dsn, EXPLAIN + "SELECT count(*), sum(v) FROM n WHERE id >= 0", FOLD),
                                 "Columnar Vectors Decoded")
        r["poison"] = (_one(dsn, POISONQ, FOLD),
                       _one(dsn, "SELECT count(*), sum(v) FROM h WHERE id BETWEEN -2000000000 AND 3000"))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _n(x):
    return int(x) if _num(x) else x


def test_vecdecode_narrow_plan_is_the_custom_scan(vd, expect):
    expect.text(vd["narrow custom"], "yes", "premise: the narrow plan is a columnar custom scan")


def test_vecdecode_wide_plan_is_the_custom_scan(vd, expect):
    expect.text(vd["wide custom"], "yes", "premise: the wide plan is a columnar custom scan")


def test_vecdecode_one_row_group(vd, expect):
    expect.text(vd["groups"], "1", "premise: one row group, so every difference is per-vector")


def test_vecdecode_no_whole_group_pruned(vd, expect):
    expect.text(vd["pruned"], "0", "premise: no whole group is pruned on the narrow query")


def test_vecdecode_narrow_range_skips_vectors(vd, expect):
    expect.text(vd["narrow skips"], "yes", "the narrow range skips vectors")


def test_vecdecode_wide_range_skips_none(vd, expect):
    expect.text(vd["wide skips"], "0", "premise: the wide range skips none, so the arms really differ")


def test_vecdecode_explain_reports_vectors_decoded(vd, expect):
    expect.text(vd["has decoded"], "yes", "EXPLAIN reports Columnar Vectors Decoded")


def test_vecdecode_full_scan_decodes_something(vd, expect):
    expect.text(vd["wide decodes"], "yes", "and it is not stuck at zero when a full scan decodes everything")


def test_vecdecode_ruled_out_vector_not_decoded(vd, expect):
    expect.text(vd["fewer"], "yes", "a ruled-out vector is not decoded")


def test_vecdecode_decoded_plus_skipped_is_the_groups_vectors(vd, expect):
    expect.text(vd["sum"], vd["sum want"],
                "and EVERY ruled-out vector is: decoded plus skipped is the group's vectors")


def test_vecdecode_narrow_range_parity(vd, expect):
    expect.text(vd["narrow parity"][0], vd["narrow parity"][1], "narrow range parity with heap")


def test_vecdecode_cross_vector_parity(vd, expect):
    expect.text(vd["cross parity"][0], vd["cross parity"][1], "cross-vector range parity")


def test_vecdecode_boundary_range_parity(vd, expect):
    expect.text(vd["edge parity"][0], vd["edge parity"][1],
                "boundary range parity, a range starting exactly on a vector edge")


def test_vecdecode_nullable_column_has_nulls(vd, expect):
    expect.text(vd["has nulls"], "true", "premise: the nullable column has nulls inside the ranges read below")


def test_vecdecode_nullable_cross_vector_parity(vd, expect):
    expect.text(vd["null parity"][0], vd["null parity"][1], "cross-vector parity of a nullable column, nulls included")


def test_vecdecode_aggregate_over_skipping_range(vd, expect):
    expect.text(vd["agg"][0], vd["agg"][1], "aggregate over a skipping range matches heap")


def test_vecdecode_aggregate_over_many_skipped_vectors(vd, expect):
    expect.text(vd["agg many"][0], vd["agg many"][1],
                "aggregate over a range spanning many skipped vectors matches heap")


def test_vecdecode_range_matching_no_row(vd, expect):
    expect.num(_n(vd["none"]), 0, "a range matching no row at all is still correct")


def test_vecdecode_aggregate_reaches_the_fold(vd, expect):
    expect.text(vd["fold reach"], "Columnar Batch Fold: yes", "the aggregate really reaches the batch fold")


def test_vecdecode_fold_answers_over_skipping_range(vd, expect):
    expect.text(vd["fold"][0], vd["fold"][1], "the fold answers correctly over a range that skips vectors")


def test_vecdecode_nullable_aggregate_reaches_the_fold(vd, expect):
    expect.text(vd["null fold reach"], "Columnar Batch Fold: yes", "a nullable aggregate reaches the fold")


def test_vecdecode_fold_nullable_count_and_sum(vd, expect):
    expect.text(vd["null fold"][0], vd["null fold"][1],
                "the fold answers count and sum of a nullable column over a skipping range")


def test_vecdecode_deep_range_reaches_the_fold(vd, expect):
    expect.text(vd["deep reach"], "Columnar Batch Fold: yes", "the deep range reaches the fold too")


def test_vecdecode_deep_range_agrees_with_heap(vd, expect):
    expect.text(vd["deep"][0], vd["deep"][1], "and it agrees with heap after many leading skipped vectors")


def test_vecdecode_poison_range_reaches_the_fold(vd, expect):
    expect.text(vd["poison reach"], "Columnar Batch Fold: yes", "the poison-accepting range reaches the fold")


def test_vecdecode_fold_arm_decodes_only_what_it_needs(vd, expect):
    expect.num(_n(vd["fold decoded"]), 3, "the FOLD ARM really decodes only the vectors it needs")


def test_vecdecode_fold_counter_reads_every_vector_when_none_skipped(vd, expect):
    expect.text(vd["fold all"], "32", "control: and the same counter reads every vector when nothing is skipped")


def test_vecdecode_fold_does_not_count_undecoded_rows(vd, expect):
    expect.text(vd["poison"][0], vd["poison"][1], "the fold does not count rows from a vector it never decoded")

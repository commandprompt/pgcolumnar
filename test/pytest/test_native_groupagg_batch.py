"""The grouped vectorized aggregate folds column-at-a-time (#708, #1341). The pytest twin
of `test/native_groupagg_batch.sh`, every check by the shell's name.

Two things are proved separately. The ANSWERS: every shape agrees with a heap mirror,
and with the same rows on the path off. The WORK: "Columnar Batch Fold: yes" on the
grouped node under EXPLAIN ANALYZE, the only assertion that goes red when the fold
stops running. Eligibility both ways: a by-reference key, an expression key, min/max
and a `<>` filter each make the fold decline, and each still answers right. Then the
delete mask, -0.0 and 0.0 as one float8 group, a scan that really skips vectors, the
parallel partial fold, and a column added after some row groups (predicted yes, ran
no).
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

ANALYZE = "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF)"
PLAIN = "EXPLAIN (COSTS OFF)"
MARKER = "Columnar Vectorized Group Keys"
DDL = "k int, k8 bigint, f float8, t text, ts timestamp, d date, v int, w int"
GEN = """SELECT CASE WHEN g % 997 = 0 THEN NULL ELSE g % 8 END,
            (g % 5)::bigint,
            (g % 4)::float8,
            't' || (g % 6),
            timestamp '2024-01-01 00:00:00' + ((g % 7) * interval '1 day'),
            date '2024-03-01' + (g % 9),
            g,
            g % 100
     FROM generate_series(1,200000) g"""
SGEN = """SELECT g % 8, CASE WHEN (g / 1024) % 40 = 0 THEN 1 ELSE 0 END,
             g, g % 3, g % 5, g % 7, g % 11
      FROM generate_series(1,200000) g"""
PAR = ("max_parallel_workers_per_gather=4", "parallel_setup_cost=0", "parallel_tuple_cost=0",
       "min_parallel_table_scan_size=0", "min_parallel_index_scan_size=0",
       "pgcolumnar.enable_parallel_vector_agg=on")
Q_PLAIN = "SELECT k, count(*), sum(v) FROM %T GROUP BY k"
Q_FILT = "SELECT k, count(*), sum(v) FROM %T WHERE v < 50000 GROUP BY k"
Q_TWOKEY = "SELECT k, k8, count(*), sum(w) FROM %T GROUP BY k, k8"
Q_SKIP = "SELECT k, count(*), sum(p1) FROM %T WHERE sel = 1 GROUP BY k"
Q_SKIP4 = "SELECT k, count(*), sum(p1), sum(p2), sum(p3), sum(p4) FROM %T WHERE sel = 1 GROUP BY k"
Q_PAR = "SELECT k, count(*), sum(v) FROM gbb GROUP BY k"


def _rows(c, sql):
    """-> the rows as sorted text tuples, NULL as "\\0" so it never equals ''. A query that
    errors returns its error as the one row, so the comparison reddens by name."""
    try:
        return sorted(tuple("\0" if v is None else str(v) for v in r) for r in c.execute(sql))
    except psycopg.Error as exc:
        return [(query_error(exc.diag.message_primary),)]


def _one(c, sql):
    return c.execute(sql).fetchone()[0]


def _text(c, sql):
    """-> the rows' first column joined, or the error: a plan that errors has no fold line."""
    try:
        return "\n".join(str(r[0]) for r in c.execute(sql))
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _groupvec(c, sql):
    return "yes" if MARKER in _text(c, f"{PLAIN} {sql}") else "no"


def _fold(c, pfx, sql):
    """-> the grouped node's "Columnar Batch Fold" value, '' when the plan has none."""
    m = re.search(r"Columnar Batch Fold: (yes|no)", _text(c, f"{pfx} {sql}"))
    return m.group(1) if m else ""


def _num(plan, label):
    m = re.search(rf"{re.escape(label)}: (\d+)", plan)
    return int(m.group(1)) if m else 0


def _agree(c, tbl, tmpl):
    """-> (columnar rows, heap rows). An empty columnar side against a full heap is a red,
    and expect.rows refuses two empty sides (#418)."""
    return _rows(c, tmpl.replace("%T", tbl)), _rows(c, tmpl.replace("%T", f"{tbl}_h"))


class _Gucs:
    def __init__(self, c, settings):
        self.c, self.settings = c, settings

    def __enter__(self):
        for s in self.settings:
            self.c.execute(f"SET {s}")

    def __exit__(self, *exc):
        for s in self.settings:
            self.c.execute(f"RESET {s.split('=')[0]}")


@pytest.fixture(scope="module")
def gbb(pgc_cluster):
    """-> a connection to a database holding gbb and its heap mirror, the path on."""
    db = "pgc_gab"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
        a.execute(f"ALTER DATABASE {db} SET pgcolumnar.enable_group_vectorization = on")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute(f"CREATE TABLE gbb({DDL}) USING pgcolumnar")
            c.execute(f"INSERT INTO gbb {GEN}")
            c.execute(f"CREATE TABLE gbb_h({DDL})")
            c.execute(f"INSERT INTO gbb_h {GEN}")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_the_fold_takes_every_eligible_shape_and_answers_as_the_heap_does(gbb, expect):
    c = gbb
    expect.num(_one(c, "SELECT count(*) FROM gbb"), 200000, "premise: fixture loaded every row (columnar)")
    expect.num(_one(c, "SELECT count(*) FROM gbb_h"), 200000, "premise: fixture loaded every row (heap)")
    expect.num(_one(c, "SELECT count(*) FROM gbb WHERE k IS NULL"),
               _one(c, "SELECT count(*) FROM gbb_h WHERE k IS NULL"), "premise: the fixture really has null keys")
    expect.num(_one(c, "SELECT (count(*) > 1)::int FROM pgcolumnar.row_group "
                       "WHERE storage_id = pgcolumnar.get_storage_id('gbb'::regclass)"), 1,
               "premise: the fixture spans more than one row group")
    for label, tmpl in (
        ("plain", Q_PLAIN),
        ("filtered", Q_FILT),
        ("two keys", Q_TWOKEY),
        ("float8 key", "SELECT f, count(*), sum(v) FROM %T GROUP BY f"),
        ("count(*)", "SELECT k, count(*) FROM %T GROUP BY k"),
        ("timestamp key", "SELECT ts, count(*), sum(v) FROM %T GROUP BY ts"),
        ("date key", "SELECT d, count(*), sum(w) FROM %T GROUP BY d"),
    ):
        q = tmpl.replace("%T", "gbb")
        expect.text(_groupvec(c, q), "yes", f"{label}: the grouped node is planned")
        expect.text(_fold(c, ANALYZE, q), "yes", f"{label}: the fold ran (work done)")
        col, heap = _agree(c, "gbb", tmpl)
        expect.rows(col, heap, f"{label}: answers match the heap mirror")


def test_the_fold_declines_every_ineligible_shape_and_still_answers(gbb, expect):
    c = gbb
    for label, tmpl in (
        ("text key", "SELECT t, count(*), sum(v) FROM %T GROUP BY t"),
        ("expression key", "SELECT k + 1, count(*) FROM %T GROUP BY k + 1"),
        ("min/max", "SELECT k, min(v), max(v) FROM %T GROUP BY k"),
        ("<> filter", "SELECT k, count(*), sum(v) FROM %T WHERE v <> 5 GROUP BY k"),
    ):
        q = tmpl.replace("%T", "gbb")
        # the decline must be the fold's, not the node's absence
        expect.text(_fold(c, ANALYZE, q) if _groupvec(c, q) == "yes" else "the grouped node is not planned",
                    "no", f"{label}: the fold declined")
        col, heap = _agree(c, "gbb", tmpl)
        expect.rows(col, heap, f"{label}: answers match the heap mirror")
    # the #715 gate: `<>` builds no scan key, so a fold that took it would count excluded rows
    expect.num(_one(c, "SELECT count(*) FROM gbb WHERE v <> 5"), _one(c, "SELECT count(*) FROM gbb_h WHERE v <> 5"),
               "<> filter: the excluded rows really are excluded")
    expect.num(_one(c, "SELECT (count(*) < 200000)::int FROM gbb_h WHERE v <> 5"), 1,
               "premise: the <> filter excludes something at all")


def test_the_fold_honours_the_delete_mask(gbb, expect):
    c = gbb
    c.execute("DELETE FROM gbb WHERE v % 7 = 0")
    c.execute("DELETE FROM gbb_h WHERE v % 7 = 0")
    expect.num(_one(c, "SELECT (count(*) < 200000)::int FROM gbb"), 1, "premise: the delete removed rows")
    expect.text(_fold(c, ANALYZE, Q_PLAIN.replace("%T", "gbb")), "yes", "deletes: the fold still ran")
    col, heap = _agree(c, "gbb", Q_PLAIN)
    expect.rows(col, heap, "deletes: answers match the heap mirror")


def test_the_fold_equals_the_row_path_over_the_same_rows(gbb, expect):
    c = gbb
    off = ("pgcolumnar.enable_group_vectorization=off",)
    for label, tmpl in (
        ("toggle-differential: plain", Q_PLAIN),
        ("toggle-differential: filtered", Q_FILT),
        ("toggle-differential: two keys", Q_TWOKEY),
    ):
        q = tmpl.replace("%T", "gbb")
        with _Gucs(c, off):
            rows_off = _rows(c, q)
            node_off = _groupvec(c, q)
        expect.text(_groupvec(c, q), "yes", f"{label} [premise: node fires with the path on]")
        rows_on = _rows(c, q)
        expect.text(node_off, "no", f"{label} [premise: the off arm is not the grouped node]")
        expect.rows(rows_on, rows_off, label)


def test_float8_keys_group_by_the_types_equality_not_by_bits(gbb, expect):
    c = gbb
    c.execute("CREATE TABLE gbz(f float8, v int) USING pgcolumnar")
    c.execute("INSERT INTO gbz VALUES ('0'::float8, 1), ('-0'::float8, 2), ('0'::float8, 4), "
              "('NaN'::float8, 8), ('NaN'::float8, 16)")
    c.execute("CREATE TABLE gbz_h(f float8, v int)")
    c.execute("INSERT INTO gbz_h SELECT * FROM gbz")
    # '-0'::float8, not -0.0: an untyped -0.0 goes through numeric and arrives as +0.0
    expect.text(_one(c, "SELECT encode(float8send(f),'hex') FROM gbz WHERE v = 2"), "8000000000000000",
                "float8 zero: the fixture really stores a NEGATIVE zero")
    expect.text(_one(c, "SELECT encode(float8send(f),'hex') FROM gbz WHERE v = 1"), "0000000000000000",
                "float8 zero: and a positive one beside it")
    q = "SELECT f, sum(v) FROM gbz GROUP BY f"
    expect.text(_groupvec(c, q), "yes", "float8 zero: the grouped node is planned")
    expect.text(_fold(c, ANALYZE, q), "yes", "float8 zero: and the fold actually ran on it")
    # the rows of the folding query itself: an outer count(*) would change the plan
    expect.num(len(_rows(c, q)), len(_rows(c, "SELECT f, sum(v) FROM gbz_h GROUP BY f")),
               "float8 zero: -0.0 and 0.0 are one group, exactly as the heap says")
    expect.rows(_rows(c, q), _rows(c, "SELECT f, sum(v) FROM gbz_h GROUP BY f"),
                "float8 zero: the grouped sums match the heap")


def test_the_fold_steps_over_skipped_vectors(gbb, expect):
    c = gbb
    c.execute("CREATE TABLE gbs(k int, sel int, p1 int, p2 int, p3 int, p4 int, p5 int) USING pgcolumnar")
    c.execute("SELECT pgcolumnar.set_options('gbs', stripe_row_limit => 65536, chunk_group_row_limit => 1024)")
    c.execute(f"INSERT INTO gbs {SGEN}")
    c.execute("CREATE TABLE gbs_h(k int, sel int, p1 int, p2 int, p3 int, p4 int, p5 int)")
    c.execute(f"INSERT INTO gbs_h {SGEN}")
    plan = _text(c, f"{ANALYZE} {Q_SKIP.replace('%T', 'gbs')}")
    m = re.search(r"Columnar Batch Fold: (yes|no)", plan)
    expect.text(m.group(1) if m else "", "yes", "skipped vectors: the fold ran")
    expect.at_least(_num(plan, "Columnar Vectors Skipped"), 1, "skipped vectors: premise: the scan really skipped vectors")
    expect.at_least(_num(plan, "Columnar Chunk Groups Read"), 1, "skipped vectors: premise: a row group was still read")
    col, heap = _agree(c, "gbs", Q_SKIP)
    expect.rows(col, heap, "skipped vectors: answers match the heap mirror")
    # four payload columns, so a cursor that misaligns on any one shows as a wrong sum
    q4 = Q_SKIP4.replace("%T", "gbs")
    expect.text(_groupvec(c, q4), "yes", "skipped vectors: premise: the wide arm is still the grouped node")
    expect.text(_fold(c, ANALYZE, q4), "yes", "skipped vectors: the fold ran on the four-payload arm")
    col, heap = _agree(c, "gbs", Q_SKIP4)
    expect.rows(col, heap, "skipped vectors: and their sums match the heap mirror")


def test_the_parallel_partial_fold_equals_the_serial_one(gbb, expect):
    c = gbb
    with _Gucs(c, PAR):
        pea = _text(c, f"{ANALYZE} {Q_PAR}")
        rows_par = _rows(c, Q_PAR)
    expect.text("yes" if re.search(r"Parallel Custom Scan \(PgColumnarScan\)", pea, re.I) and MARKER in pea else "no",
                "yes", "parallel: our parallel-aware grouped node is under the Gather")
    expect.text("yes" if re.search(r"Workers Launched: [1-9]", pea, re.I) and MARKER in pea else "no",
                "yes", "parallel: workers actually launched for that node")
    m = re.search(r"Columnar Batch Fold: (yes|no)", pea)
    expect.text(m.group(1) if m else "", "yes", "parallel: the fold ran in the partial node (work done)")
    with _Gucs(c, ("max_parallel_workers_per_gather=0",)):
        rows_serial = _rows(c, Q_PAR)
    expect.rows(rows_par, rows_serial, "parallel: the answers equal the serial fold, byte for byte")
    expect.rows(rows_par, _rows(c, "SELECT k, count(*), sum(v) FROM gbb_h GROUP BY k"),
                "parallel: and they equal the heap mirror")
    # the value arm's own plan, read under the same settings: without workers it is serial against serial
    with _Gucs(c, PAR):
        own = _text(c, f"{ANALYZE} {Q_PAR}")
    expect.text("yes" if re.search(r"Workers Launched: [1-9]", own, re.I) else "no", "yes",
                "parallel: premise: the value arm's own plan launches workers")


def test_a_column_added_after_some_row_groups_runs_the_row_path(gbb, expect):
    c = gbb
    c.execute("CREATE TABLE gba(k int) USING pgcolumnar")
    c.execute("INSERT INTO gba SELECT g % 8 FROM generate_series(1,50000) g")
    c.execute("ALTER TABLE gba ADD COLUMN v int")
    c.execute("INSERT INTO gba SELECT g % 8, g FROM generate_series(1,10000) g")
    q = "SELECT k, count(*), sum(v) FROM gba GROUP BY k"
    expect.text(_fold(c, PLAIN, q), "yes", "add column: plain EXPLAIN reports the prediction")
    expect.text(_fold(c, ANALYZE, q), "no", "add column: ANALYZE reports the row-path run")
    expect.num(_one(c, "SELECT sum(s) FROM (SELECT sum(v) AS s FROM gba GROUP BY k) x"),
               _one(c, "SELECT sum(g) FROM generate_series(1,10000) g"), "add column: the answer is still right")

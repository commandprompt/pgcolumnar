"""The vectorized aggregate accepts a target list that CONTAINS aggregates, not only one
that IS them (#755). The pytest twin of `test/vector_agg_tlist_shape.sh`, every check by
the shell's name (#1341).

The ungrouped vectorized path used to require every target-list entry to be a bare
Aggref, so `count(*)::text`, `avg(a)+avg(b)` or `round(avg(a), 2)` fell off it and
scanned the whole relation. The node emits one bare aggregate per output column and a
projection above it computes the expressions, so the risk is a WRONG ANSWER: every arm
asserts the answer against a heap table holding identical rows, and the plan separately.
Shapes that must still decline -- a bare column, an unsupported aggregate, FILTER,
DISTINCT, ORDER BY inside an aggregate, an aggregate over an expression -- are asserted
to decline and to answer correctly; the node ignores a modifier it cannot represent, so
a refusal that slipped returns a plausible wrong number. Empty and all-NULL relations,
and the parallel arm on the scan-fold path, are covered too.

ON A PRIVATE DATABASE, built by a module fixture. A plan reads "vectorized", "not
vectorized", or the failed EXPLAIN as a `query_error()`, so an EXPLAIN that errors can
never read as a decline. An answer is the row list's repr, or the failure as a
`query_error()`. The grouped vectorized path is pinned off for the plan readings, as
the shell pins it, because it prints the same marker.
"""
import hashlib
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_vector_agg_tlist_shape"
MARKER = re.compile(r"Columnar Vectorized Aggregates: [1-9]")
GROUPED_OFF = "SET pgcolumnar.enable_group_vectorization = off"
SCAN_FOLD_ON = "SET pgcolumnar.enable_ungrouped_vector_agg = on"
PARALLEL = ["SET max_parallel_workers_per_gather = 4", "SET parallel_setup_cost = 0",
            "SET min_parallel_table_scan_size = 0", SCAN_FOLD_ON,
            "SET pgcolumnar.enable_parallel_vector_agg = on"]

# (name, query with %T for the table): the plan over c takes the fast path
FAST = [
    ("a bare aggregate: vectorized", "SELECT avg(a) FROM %T"),
    ("two bare aggregates: vectorized", "SELECT avg(a), avg(b) FROM %T"),
    ("count(*): vectorized", "SELECT count(*) FROM %T"),
    ("min and max: vectorized", "SELECT min(a), max(a) FROM %T"),
    ("a cast on an aggregate: vectorized", "SELECT count(*)::text FROM %T"),
    ("a sum of two aggregates: vectorized", "SELECT avg(a)+avg(b) FROM %T"),
    ("a difference of aggregates: vectorized", "SELECT max(a)-min(a) FROM %T"),
    ("an aggregate in a function: vectorized", "SELECT round(avg(a)::numeric, 2) FROM %T"),
    ("an aggregate times a const: vectorized", "SELECT avg(a)*2 FROM %T"),
    ("mixed bare and wrapped: vectorized", "SELECT count(*), count(*)::text, avg(a) FROM %T"),
    ("a constant beside aggregates: vectorized", "SELECT 1, count(*), avg(a) FROM %T"),
    ("the same aggregate twice: vectorized", "SELECT avg(a)+avg(a) FROM %T"),
    ("aggregates in both orders: vectorized", "SELECT max(a)-min(a), min(a)-max(a) FROM %T"),
    ("a CASE over aggregates: vectorized", "SELECT CASE WHEN count(*) > 0 THEN avg(a) ELSE NULL END FROM %T"),
    ("count of a nullable column wrapped: vectorized", "SELECT count(a)::text, count(*)::text FROM %T"),
    ("output order differs from aggregate order: vectorized", "SELECT max(a), min(a), count(*), avg(a) FROM %T"),
    ("an aggregate used in two different expressions: vectorized", "SELECT avg(a)+1, avg(a)*2, avg(a) FROM %T"),
]

# (name, query with %T): the answer over c equals the answer over h
ANSWER = [
    ("a bare aggregate: answer matches heap", "SELECT avg(a) FROM %T"),
    ("two bare aggregates: answer matches heap", "SELECT avg(a), avg(b) FROM %T"),
    ("count(*): answer matches heap", "SELECT count(*) FROM %T"),
    ("min and max: answer matches heap", "SELECT min(a), max(a) FROM %T"),
    ("a cast on an aggregate: answer matches heap", "SELECT count(*)::text FROM %T"),
    ("a sum of two aggregates: answer matches heap", "SELECT avg(a)+avg(b) FROM %T"),
    ("a difference of aggregates: answer matches heap", "SELECT max(a)-min(a) FROM %T"),
    ("an aggregate in a function: answer matches heap", "SELECT round(avg(a)::numeric, 2) FROM %T"),
    ("an aggregate times a const: answer matches heap", "SELECT avg(a)*2 FROM %T"),
    ("mixed bare and wrapped: answer matches heap", "SELECT count(*), count(*)::text, avg(a) FROM %T"),
    ("a constant beside aggregates: answer matches heap", "SELECT 1, count(*), avg(a) FROM %T"),
    ("the same aggregate twice: answer matches heap", "SELECT avg(a)+avg(a) FROM %T"),
    ("aggregates in both orders: answer matches heap", "SELECT max(a)-min(a), min(a)-max(a) FROM %T"),
    ("a CASE over aggregates: answer matches heap",
     "SELECT CASE WHEN count(*) > 0 THEN avg(a) ELSE NULL END FROM %T"),
    ("and the filtered wrapped answer matches heap", "SELECT round(avg(a)::numeric,2) FROM %T WHERE b < 40"),
    ("count of a nullable column wrapped: answer matches heap", "SELECT count(a)::text, count(*)::text FROM %T"),
    ("output order differs from aggregate order: answer matches heap",
     "SELECT max(a), min(a), count(*), avg(a) FROM %T"),
    ("an aggregate used in two different expressions: answer matches heap",
     "SELECT avg(a)+1, avg(a)*2, avg(a) FROM %T"),
    ("and GROUP BY still answers correctly", "SELECT a, count(*) FROM %T GROUP BY a ORDER BY a NULLS LAST"),
    ("and the unsupported aggregate still answers correctly",
     "SELECT count(*)::text, length(string_agg(t, ',')) FROM %T"),
    ("and FILTER still answers correctly", "SELECT count(*) FILTER (WHERE a > 5)::text FROM %T"),
    ("and DISTINCT still answers correctly", "SELECT count(DISTINCT b)::text FROM %T"),
    ("and FILTER inside an expression still answers correctly",
     "SELECT avg(a) FILTER (WHERE b < 50) + 1 FROM %T"),
    ("and a wrapped ORDER BY still answers correctly",
     "SELECT length(string_agg(t, ',' ORDER BY a NULLS LAST, id)) FROM %T"),
    ("and an aggregate over an expression still answers correctly", "SELECT sum(a + b) FROM %T"),
]

# (name, query over c): shapes that must still decline
REFUSE = [
    ("REFUSE: a bare column is not an ungrouped aggregate shape", "SELECT a, count(*) FROM c GROUP BY a"),
    ("REFUSE: an unsupported aggregate declines the whole target list",
     "SELECT count(*)::text, string_agg(t, ',') FROM c"),
    ("REFUSE: FILTER on a wrapped aggregate", "SELECT count(*) FILTER (WHERE a > 5)::text FROM c"),
    ("REFUSE: DISTINCT on a wrapped aggregate", "SELECT count(DISTINCT b)::text FROM c"),
    ("REFUSE: FILTER inside an expression over aggregates", "SELECT avg(a) FILTER (WHERE b < 50) + 1 FROM c"),
    ("REFUSE: ORDER BY inside an aggregate, wrapped", "SELECT length(string_agg(t, ',' ORDER BY a)) FROM c"),
    ("REFUSE: an aggregate over an expression of two columns", "SELECT sum(a + b) FROM c"),
]

# (name, query over c): a wrapped target list on the scan-fold path goes parallel
PARALLEL_ARMS = [
    ("parallel accepts a wrapped target list: avg(v)+avg(v) FROM c", "SELECT avg(v)+avg(v) FROM c"),
    ("parallel accepts a wrapped target list: round(avg(v)::numeric,3) FROM c",
     "SELECT round(avg(v)::numeric,3) FROM c"),
    ("parallel accepts a wrapped target list: avg(v)::text FROM c", "SELECT avg(v)::text FROM c"),
]
PARALLEL_ANSWERS = [
    ("and its answer matches the heap twin: avg(v)+avg(v) FROM c", "SELECT avg(v)+avg(v) FROM c"),
    ("and its answer matches the heap twin: round(avg(v)::numeric,3) FROM c",
     "SELECT round(avg(v)::numeric,3) FROM c"),
    ("and its answer matches the heap twin: avg(v)::text FROM c", "SELECT avg(v)::text FROM c"),
]


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rows(dsn, sql, settings=()):
    """-> every row of `sql` as a list of tuples, after `settings`, on a connection of its
    own, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            return conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return _failed(exc)


def _answer(dsn, sql, settings=()):
    """-> the answer as the repr of its row list, which is order-sensitive, or the
    failure as a query_error()."""
    rows = _rows(dsn, sql, settings)
    return rows if isinstance(rows, str) else repr(rows)


def _one(dsn, sql):
    rows = _rows(dsn, sql)
    return rows if isinstance(rows, str) else rows[0][0]


def _plan(dsn, sql, settings):
    """-> "vectorized" or "not vectorized", or the failed EXPLAIN as a query_error()."""
    rows = _rows(dsn, f"EXPLAIN (COSTS OFF) {sql}", settings)
    if isinstance(rows, str):
        return rows
    return "vectorized" if any(MARKER.search(r[0]) for r in rows) else "not vectorized"


def _parallel(dsn, sql):
    """-> "yes" when the plan has a Gather, a planned worker and the vectorized node,
    else what it had, or the failed EXPLAIN as a query_error()."""
    rows = _rows(dsn, f"EXPLAIN (COSTS OFF) {sql}", PARALLEL)
    if isinstance(rows, str):
        return rows
    text = "\n".join(r[0] for r in rows)
    gather = len(re.findall(r"Gather", text))
    workers = max((int(w) for w in re.findall(r"Workers Planned: (\d+)", text)), default=0)
    vec = len(MARKER.findall(text))
    return "yes" if gather and workers and vec else f"no (gather={gather} workers={workers} vec={vec})"


def _digest(rows, ordered):
    """-> an md5 of the rows, in their order or sorted."""
    lines = [repr(r) for r in rows]
    return hashlib.md5("\n".join(lines if ordered else sorted(lines)).encode()).hexdigest()


@pytest.fixture(scope="module")
def dsn(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(d, autocommit=True) as conn:
            for sql in (
                "CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                "CREATE TABLE h (id int, a int, b int, v float8, t text) USING heap",
                # NULLs and duplicate values on purpose: null handling is part of what
                # the projection must not disturb, and ties are where a wrong column shows
                "INSERT INTO h SELECT g, CASE WHEN g % 101 = 0 THEN NULL ELSE (g * 7919) % 1000 END, "
                "(g * 104729) % 97, (g % 13)::float8, 'v' || (g % 7) FROM generate_series(1, 20000) g",
                "CREATE TABLE c (LIKE h) USING pgcolumnar",
                "SELECT pgcolumnar.set_options('c', stripe_row_limit => 2000, chunk_group_row_limit => 500)",
                "INSERT INTO c SELECT * FROM h",
                "ANALYZE h", "ANALYZE c",
            ):
                try:
                    conn.execute(sql)
                except psycopg.Error as exc:
                    print(f"-- step failed: {str(exc).splitlines()[0]}")
        yield d
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- the oracle and the fixture ---------------------------------------------

def test_the_ordered_oracle_is_order_sensitive(dsn, expect):
    fwd = _rows(dsn, "SELECT g FROM generate_series(1,5) g ORDER BY g")
    rev = _rows(dsn, "SELECT g FROM generate_series(1,5) g ORDER BY g DESC")
    again = _rows(dsn, "SELECT g FROM generate_series(1,5) g ORDER BY g")
    # a failed query stays its query_error(): two of them read "same", and text() refuses one
    f, r, a = (x if isinstance(x, str) else _digest(x, True) for x in (fwd, rev, again))
    expect.text("different" if f != r else f"same ({f})", "different", "premise: the ordered oracle is order-sensitive")
    expect.text(a, f, "premise: the ordered oracle agrees with itself")
    sf, sr = (x if isinstance(x, str) else _digest(x, False) for x in (fwd, rev))
    expect.text(sr, sf, "control: the set oracle is order-blind by design")


def test_both_tables_hold_the_same_rows(dsn, expect):
    expect.num(_one(dsn, "SELECT count(*) FROM c"), _one(dsn, "SELECT count(*) FROM h"),
               "premise: both tables hold the same rows")


def test_the_sort_column_has_nulls(dsn, expect):
    expect.at_least(_one(dsn, "SELECT count(*) FROM c WHERE a IS NULL"), 1,
                    "premise: the sort column has NULLs, which the aggregates must skip")


# ---- the shapes that take the fast path, and their answers -------------------

@pytest.mark.parametrize("name,sql", FAST)
def test_the_shape_takes_the_fast_path(dsn, name, sql, expect):
    expect.text(_plan(dsn, sql.replace("%T", "c"), [GROUPED_OFF]), "vectorized", name)


@pytest.mark.parametrize("name,sql", ANSWER)
def test_the_answer_matches_heap(dsn, name, sql, expect):
    expect.text(_answer(dsn, sql.replace("%T", "c")), _answer(dsn, sql.replace("%T", "h")), name)


# ---- the scan-fold path -------------------------------------------------------

def test_the_scan_fold_guc_is_off_by_default(dsn, expect):
    expect.text(_plan(dsn, "SELECT avg(a) FROM c WHERE b < 40", [GROUPED_OFF]), "not vectorized",
                "premise: the scan-fold GUC is off by default, so a filter declines")


def test_a_bare_aggregate_takes_the_scan_fold_path_with_the_guc_on(dsn, expect):
    expect.text(_plan(dsn, "SELECT avg(a) FROM c WHERE b < 40", [SCAN_FOLD_ON]), "vectorized",
                "premise: and a BARE aggregate takes it once the GUC is on")


def test_a_wrapped_aggregate_takes_the_scan_fold_path_too(dsn, expect):
    expect.text(_plan(dsn, "SELECT round(avg(a)::numeric,2) FROM c WHERE b < 40", [SCAN_FOLD_ON]), "vectorized",
                "a wrapped aggregate takes the scan-fold path too")


# ---- the shapes that must still decline ---------------------------------------

@pytest.mark.parametrize("name,sql", REFUSE)
def test_the_shape_still_declines(dsn, name, sql, expect):
    expect.text(_plan(dsn, sql, [GROUPED_OFF]), "not vectorized", name)


# ---- empty and all-NULL --------------------------------------------------------

WRAPPED = "SELECT count(*)::text, avg(a), avg(a)+avg(b) FROM {}"


def test_empty_then_all_null_relations_answer_as_heap_does(dsn, expect):
    """One test, because the all-NULL half fills the same pair the empty half reads."""
    for sql in ("CREATE TABLE eh (LIKE h) USING heap", "CREATE TABLE ec (LIKE h) USING pgcolumnar"):
        _rows(dsn, f"{sql}; SELECT 1")
    expect.text(str(_one(dsn, "SELECT count(*) FROM ec")), "0", "premise: the empty pair really is empty")
    expect.text(_answer(dsn, WRAPPED.format("ec")), _answer(dsn, WRAPPED.format("eh")),
                "an empty relation answers the wrapped shape as heap does")
    _rows(dsn, "INSERT INTO eh SELECT g, NULL, NULL, NULL, NULL FROM generate_series(1,500) g; SELECT 1")
    _rows(dsn, "INSERT INTO ec SELECT * FROM eh; SELECT 1")
    rows, values = _one(dsn, "SELECT count(*) FROM ec"), _one(dsn, "SELECT count(a) FROM ec")
    expect.text(f"{rows} rows, {values} values", "500 rows, 0 values",
                "premise: the all-NULL pair has rows but no values")
    expect.text(_answer(dsn, WRAPPED.format("ec")), _answer(dsn, WRAPPED.format("eh")),
                "an all-NULL relation answers the wrapped shape as heap does")


# ---- the parallel arm ----------------------------------------------------------

def test_a_bare_aggregate_on_the_scan_fold_path_goes_parallel(dsn, expect):
    expect.text(_parallel(dsn, "SELECT avg(v) FROM c"), "yes",
                "premise: a BARE aggregate on the scan-fold path really goes parallel")


@pytest.mark.parametrize("name,sql", PARALLEL_ARMS)
def test_parallel_accepts_a_wrapped_target_list(dsn, name, sql, expect):
    expect.text(_parallel(dsn, sql), "yes", name)


@pytest.mark.parametrize("name,sql", PARALLEL_ANSWERS)
def test_the_parallel_answer_matches_heap(dsn, name, sql, expect):
    expect.text(_answer(dsn, sql, PARALLEL), _answer(dsn, sql.replace("FROM c", "FROM h")), name)

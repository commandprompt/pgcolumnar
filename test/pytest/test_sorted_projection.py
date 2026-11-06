"""vacuum_sorted rewrites a columnar table physically sorted on a key (gap 26, #1341). The
pytest twin of `test/sorted_projection.sh`, every check by the shell's name.

The rewrite must never change an answer: every query shape agrees with a heap oracle
before and after it, order-independently, and in order where the query orders. It
must really reorder storage: a narrow range on the sort key skips vectors it could not
skip before. Then a multi-column key, deleted rows reclaimed with the index rebuilt
(each index lookup forced with the custom scan off, and asserting it reads the index),
idempotence, and argument validation.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

SET_HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
            "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
SEQ_HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY n)), $e$EMPTY$e$) "
            "FROM (SELECT row_number() OVER () AS n, _row::text AS t FROM ({}) _row) _s")
IDXON = ("SET enable_seqscan=off", "SET enable_bitmapscan=off", "SET pgcolumnar.enable_custom_scan=off")


def _hash(c, tmpl, sql):
    try:
        return c.execute(tmpl.format(sql)).fetchone()[0]
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _pair(c, tmpl, hashing=SET_HASH):
    """-> (columnar, heap) digests of `tmpl` with %T for the table, as diff_query."""
    return _hash(c, hashing, tmpl.replace("%T", "t_col")), _hash(c, hashing, tmpl.replace("%T", "t_heap"))


def _vectors_skipped(c, sql):
    for (line,) in c.execute(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}"):
        if "Vectors Skipped" in line:
            return int(line.rsplit(" ", 1)[-1])
    return 0


def _forced(c, sql, explain=False):
    for s in IDXON:
        c.execute(s)
    try:
        if explain:
            plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {sql}"))
            return "yes" if ("Index Scan using t_col_id_idx" in plan or "Index Only Scan using t_col_id_idx" in plan) \
                else f"no: {plan.splitlines()[0] if plan else ''}"
        return _one(c, sql)
    finally:
        for g in ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan"):
            c.execute(f"RESET {g}")


def _refused(c, sql):
    """-> the SQLSTATE `sql` is refused with, or 'succeeded': the refusal must be the
    function's own, since with its columnar check gone a heap table still raised XX001."""
    try:
        c.execute(sql)
        return "succeeded"
    except psycopg.Error as exc:
        return exc.sqlstate or "?"


@pytest.fixture(scope="module")
def sp(pgc_cluster):
    db = "pgc_sortproj"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            # k scrambled against insert order, so before sorting every group spans the domain
            c.execute("CREATE TABLE t_heap (id int, k int, v bigint, t text) USING heap")
            c.execute("CREATE TABLE t_col (id int, k int, v bigint, t text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 1000)")
            c.execute("INSERT INTO t_heap SELECT g, ((g*7919)%1000), g::bigint*2, 'r'||g FROM generate_series(1,50000) g")
            c.execute("INSERT INTO t_col SELECT * FROM t_heap")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_the_sorted_rewrite_changes_no_answer_and_really_reorders(sp, expect):
    c = sp
    for label, tmpl in (("pre range", "SELECT id, v, t FROM %T WHERE k BETWEEN 100 AND 120"),
                        ("pre equality", "SELECT id FROM %T WHERE k = 500")):
        col, heap = _pair(c, tmpl)
        expect.text(col, heap, label)
    # the premise behind the ordered comparisons, as lib.sh's pgc_check_ordered_oracle
    fwd = _hash(c, SEQ_HASH, "SELECT g FROM generate_series(1,5) g ORDER BY g")
    rev = _hash(c, SEQ_HASH, "SELECT g FROM generate_series(1,5) g ORDER BY g DESC")
    again = _hash(c, SEQ_HASH, "SELECT g FROM generate_series(1,5) g ORDER BY g")
    sfwd = _hash(c, SET_HASH, "SELECT g FROM generate_series(1,5) g ORDER BY g")
    srev = _hash(c, SET_HASH, "SELECT g FROM generate_series(1,5) g ORDER BY g DESC")
    expect.text("yes" if fwd != rev else "no", "yes", "premise: the ordered oracle is order-sensitive")
    expect.text("yes" if fwd == again else "no", "yes", "premise: the ordered oracle agrees with itself")
    expect.text("yes" if sfwd == srev else "no", "yes", "control: the set oracle is order-blind by design")
    col, heap = _pair(c, "SELECT k, v FROM %T ORDER BY k, v", SEQ_HASH)
    expect.text(col, heap, "pre order-by")
    for label, tmpl in (("pre aggregate", "SELECT k, count(*), sum(v) FROM %T GROUP BY k"),
                        ("pre star", "SELECT * FROM %T WHERE id % 997 = 0")):
        col, heap = _pair(c, tmpl)
        expect.text(col, heap, label)

    narrow = "SELECT sum(v) FROM t_col WHERE k BETWEEN 100 AND 120"
    plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {narrow}"))
    expect.text("yes" if "Columnar Projected Columns" in plan else "no", "yes",
                "the plan under test is a columnar custom scan")
    before = _vectors_skipped(c, narrow)

    c.execute("SELECT pgcolumnar.vacuum_sorted('t_col', 'k')")
    for label, tmpl in (("post range", "SELECT id, v, t FROM %T WHERE k BETWEEN 100 AND 120"),
                        ("post equality", "SELECT id FROM %T WHERE k = 500")):
        col, heap = _pair(c, tmpl)
        expect.text(col, heap, label)
    col, heap = _pair(c, "SELECT k, v FROM %T ORDER BY k, v", SEQ_HASH)
    expect.text(col, heap, "post order-by")
    for label, tmpl in (("post aggregate", "SELECT k, count(*), sum(v) FROM %T GROUP BY k"),
                        ("post star", "SELECT * FROM %T WHERE id % 997 = 0")):
        col, heap = _pair(c, tmpl)
        expect.text(col, heap, label)

    after = _vectors_skipped(c, narrow)
    expect.num(before, 0, "pre-sort prunes little")
    expect.text("more" if after > before else f"removed_before={before} removed_after={after}", "more",
                "post-sort prunes more chunk groups")


def test_a_multi_key_sort_reclaims_and_rebuilds_the_index(sp, expect):
    c = sp
    c.execute("SELECT pgcolumnar.vacuum_sorted('t_col', 'k', 'v')")
    col, heap = _pair(c, "SELECT id, v FROM %T WHERE k BETWEEN 100 AND 120")
    expect.text(col, heap, "multi-key range")

    c.execute("CREATE INDEX t_col_id_idx ON t_col (id)")
    c.execute("DELETE FROM t_heap WHERE id % 2 = 0")
    c.execute("DELETE FROM t_col WHERE id % 2 = 0")
    c.execute("SELECT pgcolumnar.vacuum_sorted('t_col', 'k')")
    expect.text(_one(c, "SELECT count(*) FROM t_col"), "25000", "live count after delete+sort")
    for label, tmpl in (("count matches heap", "SELECT count(*) FROM %T"), ("sum matches heap", "SELECT sum(v) FROM %T")):
        col, heap = _pair(c, tmpl)
        expect.text(col, heap, label)
    # forced and asserted: with seqscan alone off the point lookup never read the index
    expect.text(_forced(c, "SELECT t FROM t_col WHERE id = 4999", explain=True), "yes",
                "premise: the point lookup reads t_col_id_idx")
    expect.text(_forced(c, "SELECT t FROM t_col WHERE id = 4999"), "r4999", "index point lookup after sort")
    expect.text(_forced(c, "SELECT count(*) FROM t_col WHERE id = 5000", explain=True), "yes",
                "premise: the deletion lookup reads t_col_id_idx")
    expect.text(_forced(c, "SELECT count(*) FROM t_col WHERE id = 5000"), "0", "index sees deletion after sort")

    h1 = _hash(c, SET_HASH, "SELECT * FROM t_col")
    c.execute("SELECT pgcolumnar.vacuum_sorted('t_col', 'k')")
    expect.text(_hash(c, SET_HASH, "SELECT * FROM t_col"), h1, "idempotent sorted rewrite")


def test_vacuum_sorted_refuses_what_it_cannot_sort(sp, expect):
    c = sp
    expect.text(_refused(c, "SELECT pgcolumnar.vacuum_sorted('t_heap', 'k')"), "42809", "reject non-columnar table")
    expect.text(_refused(c, "SELECT pgcolumnar.vacuum_sorted('t_col', 'nope')"), "42703", "reject unknown column")
    expect.text(_refused(c, "SELECT pgcolumnar.vacuum_sorted('t_col')"), "22023", "reject no sort columns")
    c.execute("CREATE TABLE t_js (a int, j json) USING pgcolumnar")
    c.execute("INSERT INTO t_js SELECT g, '{}'::json FROM generate_series(1,10) g")
    expect.text(_refused(c, "SELECT pgcolumnar.vacuum_sorted('t_js', 'j')"), "42883", "reject non-sortable type")

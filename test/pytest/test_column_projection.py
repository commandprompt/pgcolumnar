"""Column projection reads only the columns a query references (#338). The pytest twin of
`test/column_projection.sh`, every check by the shell's name (#1341).

A columnar table of fourteen columns and its heap copy: reading one column must cost a
fraction of reading them all, SELECT * must read what projection-off reads, and every
query must answer the same with `pgcolumnar.enable_column_projection` on and off and the
same as the heap -- through added columns, deletes, updates and a parallel scan. The
cost model must price a narrow projection below a wide one.

A COMPARISON NEEDS TWO VALUES. A side that errored or came back empty is a failed
query, so two runs failing the same way, or two empty results, cannot agree. ON A
PRIVATE DATABASE, built once by a module fixture in the shell's order (#1339).
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_column_projection"
GUC = "pgcolumnar.enable_column_projection"
SERIAL = "-c max_parallel_workers_per_gather=0"
WIDE_SQL = ("SELECT count(id), count(txt), count(pad1), count(pad2), count(pad3), "
            "count(pad4), count(pad5), count(pad6), count(pad7), count(pad8) FROM t")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _rows(dsn, sql, setting="on", options=SERIAL, extra=()):
    """-> every row of `sql` as text, joined, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=options) as conn:
            conn.execute(f"SET {GUC} = {setting}")
            for s in extra:
                conn.execute(s)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    text = "\n".join("|".join("" if v is None else str(v) for v in r) for r in rows)
    return text if text else query_error(f"no rows from {sql}")


def _agree(a, b):
    """-> "same" when both readings are values and equal, else what was wrong."""
    if a.startswith("QUERY_ERROR") or b.startswith("QUERY_ERROR"):
        return query_error(f"no reading: [{a}] and [{b}]")
    return "same" if a == b else f"differ: [{a}] and [{b}]"


def _ab(dsn, sql):
    return _agree(_rows(dsn, sql, "on"), _rows(dsn, sql, "off"))


def _oracle(dsn, sql):
    return _agree(_rows(dsn, sql % "t"), _rows(dsn, sql % "h"))


def _plan(dsn, sql, setting, analyze):
    try:
        with psycopg.connect(dsn, autocommit=True, options=SERIAL) as conn:
            conn.execute(f"SET {GUC} = {setting}")
            opts = "(ANALYZE, BUFFERS)" if analyze else ""
            return "\n".join(r[0] for r in conn.execute(f"EXPLAIN {opts} {sql}"))
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _bufs(dsn, sql, setting="on"):
    """-> the first Buffers line's hit+read, or a failed reading."""
    m = re.search(r"Buffers: shared[^)\n]*", _plan(dsn, sql, setting, True))
    if not m:
        return query_error("no Buffers line")
    return sum(int(x) for x in re.findall(r"(?:hit|read)=(\d+)", m.group(0)))


def _cost(dsn, sql):
    for line in _plan(dsn, sql, "on", False).splitlines():
        if "PgColumnar" in line:
            m = re.search(r"cost=[0-9.]+\.\.([0-9.]+)", line)
            if m:
                return float(m.group(1))
    return query_error("no PgColumnar cost")


def _ratio(a, b, bound):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or a == 0 or b == 0:
        return query_error(f"no ratio: [{a}] and [{b}]")
    return "yes" if a / b <= bound else f"no ({a / b:.2f}x against {bound}x)"


@pytest.fixture(scope="module")
def cpj(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # The heap from the generator and the columnar table from the heap: copied out
        # of t, h held whatever the reader returned and every columnar==heap check
        # compared the reader with itself (found by jdatcmd reviewing #1581).
        _run(dsn, "CREATE TABLE h (id int, k int, v float8, w bigint, txt text, alln int, "
                  "pad1 float8, pad2 float8, pad3 float8, pad4 float8, pad5 float8, pad6 float8, "
                  "pad7 float8, pad8 float8); "
                  "INSERT INTO h SELECT g, g % 1000, CASE WHEN g % 50 = 0 THEN NULL ELSE (g % 13)::float8 * 1.5 END, "
                  "g::bigint * 7, CASE WHEN g % 31 = 0 THEN NULL ELSE 'row' || g END, NULL, "
                  "random(), random(), random(), random(), random(), random(), random(), random() "
                  "FROM generate_series(1, 300000) g; CREATE TABLE t (LIKE h) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 20000); "
                  "INSERT INTO t SELECT * FROM h;")
        r = {}
        r["rows t"], r["rows h"] = _rows(dsn, "SELECT count(*) FROM t"), _rows(dsn, "SELECT count(*) FROM h")
        r["b on"], r["b off"] = _bufs(dsn, "SELECT sum(v) FROM t", "on"), _bufs(dsn, "SELECT sum(v) FROM t", "off")
        r["qual t"], r["qual h"] = (_rows(dsn, "SELECT count(*) FROM t WHERE k = 7"),
                                    _rows(dsn, "SELECT count(*) FROM h WHERE k = 7"))
        r["b all"] = _bufs(dsn, "SELECT count(*) FROM (SELECT t.* FROM t OFFSET 0) s")
        r['single column agg :: on==off'] = _ab(dsn, 'SELECT sum(v) FROM t')
        r['two columns :: on==off'] = _ab(dsn, 'SELECT sum(v), sum(w) FROM t')
        r['qual column not selected :: on==off'] = _ab(dsn, 'SELECT count(*) FROM t WHERE k < 500')
        r['qual and target disjoint :: on==off'] = _ab(dsn, 'SELECT sum(v) FROM t WHERE k < 500')
        r['varlena projected alone :: on==off'] = _ab(dsn, "SELECT md5(string_agg(txt, '|' ORDER BY id)) FROM t WHERE id <= 5000")
        r['all-NULL column :: on==off'] = _ab(dsn, 'SELECT count(alln), count(*) FROM t')
        r['count(*) :: on==off'] = _ab(dsn, 'SELECT count(*) FROM t')
        r['select star hash :: on==off'] = _ab(dsn, "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM t WHERE id <= 3000) x")
        r['whole-row var :: on==off'] = _ab(dsn, "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM t WHERE id <= 3000")
        r['ctid system column :: on==off'] = _ab(dsn, 'SELECT count(DISTINCT ctid) FROM t WHERE id <= 3000')
        r['order by unselected col :: on==off'] = _ab(dsn, "SELECT string_agg(id::text, ',') FROM (SELECT id FROM t WHERE id <= 200 ORDER BY k, id) s")
        r['min/max mixed types :: on==off'] = _ab(dsn, 'SELECT min(v), max(w), min(txt), max(id) FROM t')
        r['join on projected column :: on==off'] = _ab(dsn, 'SELECT count(*) FROM t a JOIN t b USING (id) WHERE a.id <= 2000')
        r['single column agg :: columnar==heap'] = _oracle(dsn, 'SELECT sum(v) FROM %s')
        r['qual and target disjoint :: columnar==heap'] = _oracle(dsn, 'SELECT sum(v) FROM %s WHERE k < 500')
        r['varlena alone :: columnar==heap'] = _oracle(dsn, "SELECT md5(string_agg(txt, '|' ORDER BY id)) FROM %s WHERE id <= 5000")
        r['select star hash :: columnar==heap'] = _oracle(dsn, "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM %s WHERE id <= 3000) x")
        r['all-NULL column :: columnar==heap'] = _oracle(dsn, 'SELECT count(alln), count(*) FROM %s')
        r['min/max mixed types :: columnar==heap'] = _oracle(dsn, 'SELECT min(v), max(w), min(txt), max(id) FROM %s')
        _run(dsn, "ALTER TABLE t ADD COLUMN added int DEFAULT 42; ALTER TABLE h ADD COLUMN added int DEFAULT 42;")
        r['added column alone :: on==off'] = _ab(dsn, 'SELECT sum(added), count(added) FROM t')
        r['added column alone :: columnar==heap'] = _oracle(dsn, 'SELECT sum(added), count(added) FROM %s')
        r['added column with others :: columnar==heap'] = _oracle(dsn, 'SELECT sum(added), sum(v) FROM %s')
        for tbl in ("t", "h"):
            _run(dsn, f"INSERT INTO {tbl} SELECT g, g % 1000, 1.0, g, 'new'||g, NULL, 1,2,3,4,5,6,7,8, 99 "
                      "FROM generate_series(200001, 210000) g;")
        r['added column across the boundary :: columnar==heap'] = _oracle(dsn, 'SELECT sum(added), count(added), min(added), max(added) FROM %s')
        r['added column across the boundary :: on==off'] = _ab(dsn, 'SELECT sum(added), count(added), min(added), max(added) FROM t')
        _run(dsn, "DELETE FROM t WHERE id % 97 = 0; DELETE FROM h WHERE id % 97 = 0;")
        r['after deletes, projected column :: columnar==heap'] = _oracle(dsn, 'SELECT sum(v), count(*) FROM %s')
        r['after deletes, projected column :: on==off'] = _ab(dsn, 'SELECT sum(v), count(*) FROM t')
        _run(dsn, "UPDATE t SET v = v + 1 WHERE id % 500 = 0; UPDATE h SET v = v + 1 WHERE id % 500 = 0;")
        r['after updates, projected column :: columnar==heap'] = _oracle(dsn, 'SELECT sum(v), count(*) FROM %s')
        r['after updates, select star hash :: columnar==heap'] = _oracle(dsn, "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM %s WHERE id <= 3000) x")
        par = ["SET max_parallel_workers_per_gather = 4", "SET parallel_setup_cost = 0",
               "SET parallel_tuple_cost = 0", "SET min_parallel_table_scan_size = 0"]
        r["par proj"] = _rows(dsn, "SELECT count(*), sum(w) FROM t", options="", extra=par)
        r["par proj heap"] = _rows(dsn, "SELECT count(*), sum(w) FROM h")
        r["par qual"] = _rows(dsn, "SELECT sum(v) FROM t WHERE k < 500", options="", extra=par)
        r["par qual heap"] = _rows(dsn, "SELECT sum(v) FROM h WHERE k < 500")
        r["narrow buf"], r["wide buf"] = _bufs(dsn, "SELECT count(id) FROM t"), _bufs(dsn, WIDE_SQL)
        r["narrow cost"], r["wide cost"] = _cost(dsn, "SELECT count(id) FROM t"), _cost(dsn, WIDE_SQL)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_columnar_and_heap_hold_the_same_rows(cpj, expect):
    expect.text(cpj["rows t"], cpj["rows h"], "premise: columnar and heap hold the same rows")


def test_the_unprojected_read_is_large_enough_to_measure(cpj, expect):
    b = cpj["b off"]
    expect.text("yes" if isinstance(b, int) and b > 1000 else f"no (off: [{b}])", "yes",
                "premise: the unprojected read is large enough to measure")


def test_one_column_costs_less_than_a_third_of_all(cpj, expect):
    expect.text(_ratio(cpj["b on"], cpj["b off"], 0.333), "yes",
                "one column costs less than a third of reading all of them")


def test_a_qual_only_column_still_produces_the_right_count(cpj, expect):
    expect.text(cpj["qual t"], cpj["qual h"], "qual-only column still produces the right count")


def test_select_star_reads_as_much_as_projection_off(cpj, expect):
    expect.text(_ratio(cpj["b off"], cpj["b all"], 1.25), "yes", "SELECT * reads as much as projection-off does")


def test_single_column_agg_on_off(cpj, expect):
    expect.text(cpj['single column agg :: on==off'], "same", 'single column agg :: on==off')


def test_two_columns_on_off(cpj, expect):
    expect.text(cpj['two columns :: on==off'], "same", 'two columns :: on==off')


def test_qual_column_not_selected_on_off(cpj, expect):
    expect.text(cpj['qual column not selected :: on==off'], "same", 'qual column not selected :: on==off')


def test_qual_and_target_disjoint_on_off(cpj, expect):
    expect.text(cpj['qual and target disjoint :: on==off'], "same", 'qual and target disjoint :: on==off')


def test_varlena_projected_alone_on_off(cpj, expect):
    expect.text(cpj['varlena projected alone :: on==off'], "same", 'varlena projected alone :: on==off')


def test_all_null_column_on_off(cpj, expect):
    expect.text(cpj['all-NULL column :: on==off'], "same", 'all-NULL column :: on==off')


def test_count_on_off(cpj, expect):
    expect.text(cpj['count(*) :: on==off'], "same", 'count(*) :: on==off')


def test_select_star_hash_on_off(cpj, expect):
    expect.text(cpj['select star hash :: on==off'], "same", 'select star hash :: on==off')


def test_whole_row_var_on_off(cpj, expect):
    expect.text(cpj['whole-row var :: on==off'], "same", 'whole-row var :: on==off')


def test_ctid_system_column_on_off(cpj, expect):
    expect.text(cpj['ctid system column :: on==off'], "same", 'ctid system column :: on==off')


def test_order_by_unselected_col_on_off(cpj, expect):
    expect.text(cpj['order by unselected col :: on==off'], "same", 'order by unselected col :: on==off')


def test_min_max_mixed_types_on_off(cpj, expect):
    expect.text(cpj['min/max mixed types :: on==off'], "same", 'min/max mixed types :: on==off')


def test_join_on_projected_column_on_off(cpj, expect):
    expect.text(cpj['join on projected column :: on==off'], "same", 'join on projected column :: on==off')


def test_single_column_agg_columnar_heap(cpj, expect):
    expect.text(cpj['single column agg :: columnar==heap'], "same", 'single column agg :: columnar==heap')


def test_qual_and_target_disjoint_columnar_heap(cpj, expect):
    expect.text(cpj['qual and target disjoint :: columnar==heap'], "same", 'qual and target disjoint :: columnar==heap')


def test_varlena_alone_columnar_heap(cpj, expect):
    expect.text(cpj['varlena alone :: columnar==heap'], "same", 'varlena alone :: columnar==heap')


def test_select_star_hash_columnar_heap(cpj, expect):
    expect.text(cpj['select star hash :: columnar==heap'], "same", 'select star hash :: columnar==heap')


def test_all_null_column_columnar_heap(cpj, expect):
    expect.text(cpj['all-NULL column :: columnar==heap'], "same", 'all-NULL column :: columnar==heap')


def test_min_max_mixed_types_columnar_heap(cpj, expect):
    expect.text(cpj['min/max mixed types :: columnar==heap'], "same", 'min/max mixed types :: columnar==heap')


def test_added_column_alone_on_off(cpj, expect):
    expect.text(cpj['added column alone :: on==off'], "same", 'added column alone :: on==off')


def test_added_column_alone_columnar_heap(cpj, expect):
    expect.text(cpj['added column alone :: columnar==heap'], "same", 'added column alone :: columnar==heap')


def test_added_column_with_others_columnar_heap(cpj, expect):
    expect.text(cpj['added column with others :: columnar==heap'], "same", 'added column with others :: columnar==heap')


def test_added_column_across_the_boundary_columnar_heap(cpj, expect):
    expect.text(cpj['added column across the boundary :: columnar==heap'], "same", 'added column across the boundary :: columnar==heap')


def test_added_column_across_the_boundary_on_off(cpj, expect):
    expect.text(cpj['added column across the boundary :: on==off'], "same", 'added column across the boundary :: on==off')


def test_after_deletes_projected_column_columnar_heap(cpj, expect):
    expect.text(cpj['after deletes, projected column :: columnar==heap'], "same", 'after deletes, projected column :: columnar==heap')


def test_after_deletes_projected_column_on_off(cpj, expect):
    expect.text(cpj['after deletes, projected column :: on==off'], "same", 'after deletes, projected column :: on==off')


def test_after_updates_projected_column_columnar_heap(cpj, expect):
    expect.text(cpj['after updates, projected column :: columnar==heap'], "same", 'after updates, projected column :: columnar==heap')


def test_after_updates_select_star_hash_columnar_heap(cpj, expect):
    expect.text(cpj['after updates, select star hash :: columnar==heap'], "same", 'after updates, select star hash :: columnar==heap')


def test_a_parallel_scan_of_a_projected_column_matches_the_heap(cpj, expect):
    expect.text(cpj["par proj"], cpj["par proj heap"], "parallel scan, projected column")


def test_a_parallel_scan_with_a_qual_on_an_unselected_column_matches_the_heap(cpj, expect):
    expect.text(cpj["par qual"], cpj["par qual heap"], "parallel scan, qual on unselected column")


def test_the_narrow_projection_reads_far_fewer_buffers(cpj, expect):
    n, w = cpj["narrow buf"], cpj["wide buf"]
    got = ("yes" if w > 0 and n < w * 0.5 else f"no ({n} against {w})") if isinstance(n, int) and isinstance(w, int) \
        else query_error(f"no reading: {n} / {w}")
    expect.text(got, "yes", "the narrow projection really does read far fewer buffers")


def test_both_scan_costs_are_measurements(cpj, expect):
    n, w = cpj["narrow cost"], cpj["wide cost"]
    expect.text("yes" if isinstance(n, float) and isinstance(w, float) and n > 0 and w > 0 else f"no ({n} / {w})",
                "yes", "premise: both scan costs are measurements")


def test_the_cost_model_prices_a_narrow_projection_below_a_wide_one(cpj, expect):
    n, w = cpj["narrow cost"], cpj["wide cost"]
    got = ("yes" if n < w * 0.8 else f"no ({n} against {w})") if isinstance(n, float) and isinstance(w, float) \
        else query_error(f"no reading: {n} / {w}")
    expect.text(got, "yes", "the cost model prices a narrow projection below a wide one")

"""Every rewrite rebuilds the distinct-count sketches (#1309 phase 4). The pytest twin
of `test/native_sketch_rewrite.sh`, every check by the shell's name.

Each rewrite starts from the same table: sketch_columns => a, five row groups of
1,000 rows, a third of them deleted. Afterwards the base storage must have a sketch
for every row group and no sketch row may name a row group that does not exist; the
union of its sketches must equal the registers recomputed in SQL from the live rows;
and a rewriting ALTER must keep the table's stripe_row_limit. The rewriting ALTER
forms and REFRESH MATERIALIZED VIEW fill a transient relation (pg_temp_<oid>), and
the writer looked up the transient's own OID in pgcolumnar.options and found none:
no sketches, and one row group under a limit of 1,000.
"""
import re

import psycopg

from pgc_vacuity import query_error


def _one(conn, sql):
    """-> the first column of the first row, or the failure as a query_error()."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)
    return row[0] if row else query_error("no row")


def _run(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)


def _base_table(conn, name):
    _run(conn, f"CREATE TABLE {name} (a bigint, b int) USING pgcolumnar")
    _run(conn, f"SELECT pgcolumnar.set_options('{name}', stripe_row_limit => 1000,"
               " chunk_group_row_limit => 1000, sketch_columns => ARRAY['a'])")
    _run(conn, f"INSERT INTO {name} SELECT g, g FROM generate_series(1, 5000) g")
    _run(conn, f"DELETE FROM {name} WHERE a % 3 = 0")


def _whole(conn, rel):
    """-> "yes" when every row group of the base storage has its sketch and no sketch
    row of any storage names a missing row group, else "no (groups|sketches|orphans)"."""
    shape = _one(conn, f"""SELECT (SELECT count(*) FROM pgcolumnar.row_group r
            WHERE r.storage_id = pgcolumnar.get_storage_id('{rel}'))
        || '|' || (SELECT count(*) FROM pgcolumnar.sketch k
            WHERE k.storage_id = pgcolumnar.get_storage_id('{rel}'))
        || '|' || (SELECT count(*) FROM pgcolumnar.sketch k WHERE NOT EXISTS (
            SELECT 1 FROM pgcolumnar.row_group r
            WHERE r.storage_id = k.storage_id AND r.group_number = k.group_number))""")
    g, k, o = (shape.split("|") + ["", "", ""])[:3]
    return "yes" if g.isdigit() and int(g) > 0 and g == k and o == "0" else f"no ({shape})"


def _union_is_live(conn, rel):
    """-> "t" when the union of the base storage's sketches is the live rows' registers."""
    got = _one(conn, f"""WITH u AS (
        SELECT md5(decode(string_agg(lpad(to_hex(m), 2, '0'), '' ORDER BY i), 'hex')) d
        FROM (SELECT i, coalesce(max(get_byte(k.registers, i)), 0) m
              FROM generate_series(0, 2047) i
              LEFT JOIN pgcolumnar.sketch k
                ON k.storage_id = pgcolumnar.get_storage_id('{rel}') AND k.column_index = 0
              GROUP BY i) x),
      h AS (SELECT hashint8extended(a, 0) h FROM {rel}),
      r AS (SELECT (h & 2047)::int idx, (h >> 11) & 9007199254740991 w FROM h),
      k AS (SELECT idx, max(CASE WHEN w = 0 THEN 54
            ELSE round(log(2, (w & -w)::numeric))::int + 1 END) rank FROM r GROUP BY idx),
      l AS (SELECT md5(decode(string_agg(lpad(to_hex(coalesce(k.rank, 0)), 2, '0'), ''
            ORDER BY i), 'hex')) d FROM generate_series(0, 2047) i LEFT JOIN k ON k.idx = i)
    SELECT u.d = l.d FROM u, l""")
    return {True: "t", False: "f"}.get(got, got)


def _groups(conn, rel):
    return str(_one(conn, f"SELECT count(*) FROM pgcolumnar.row_group r"
                          f" WHERE r.storage_id = pgcolumnar.get_storage_id('{rel}')"))


def _fold_counts(conn, rel):
    """-> "folded|scanned" from the plan, "none" for a line it does not print."""
    try:
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                        f"SELECT pgcolumnar.approx_count_distinct(a) FROM {rel}")
            plan = "\n".join(r[0] for r in cur.fetchall())
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)
    f = re.search(r"Columnar Sketch Groups Folded: (\d+)", plan)
    sc = re.search(r"Columnar Sketch Groups Scanned: (\d+)", plan)
    return f"{f.group(1) if f else 'none'}|{sc.group(1) if sc else 'none'}"


def _folds_all(conn, rel):
    """-> "yes" when approx_count_distinct folds every row group from its sketch and
    scans none (#1309 phase 3's EXPLAIN ANALYZE lines), else "no (folded|scanned of N)"."""
    try:
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                        f"SELECT pgcolumnar.approx_count_distinct(a) FROM {rel}")
            plan = "\n".join(r[0] for r in cur.fetchall())
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)
    f = re.search(r"Columnar Sketch Groups Folded: (\d+)", plan)
    sc = re.search(r"Columnar Sketch Groups Scanned: (\d+)", plan)
    g = _groups(conn, rel)
    if f and sc and f.group(1) == g and sc.group(1) == "0":
        return "yes"
    return f"no ({f and f.group(1)}|{sc and sc.group(1)} of {g})"


def _rewritten(conn, rel, *verbs):
    """the base table, then each statement of the rewrite"""
    _base_table(conn, rel)
    for v in verbs:
        _run(conn, v)


def test_before_any_rewrite(pgc_conn, expect):
    _base_table(pgc_conn, "tsr_ctl")
    expect.text(f"{_whole(pgc_conn, 'tsr_ctl')}|{_union_is_live(pgc_conn, 'tsr_ctl')}", "yes|f",
                "before any rewrite each row group has its sketch, and the deleted rows are still in them")
    # exact: all five groups have deletes; a plan with no fold prints neither line
    expect.text(_fold_counts(pgc_conn, "tsr_ctl"), "0|5",
                "before any rewrite approx_count_distinct scans the row groups with deletes rather than folding them")


def test_pgcolumnar_vacuum(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_vac", "SELECT pgcolumnar.vacuum('tsr_vac')")
    expect.text(_whole(pgc_conn, "tsr_vac"), "yes",
                "pgcolumnar.vacuum: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_vac"), "t",
                "pgcolumnar.vacuum: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_vac"), "yes",
                "pgcolumnar.vacuum: approx_count_distinct folds every row group from its sketch and scans none")


def test_compact_rewrite(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_cr", "SELECT pgcolumnar.compact_rewrite('tsr_cr', 0.0)")
    expect.text(_whole(pgc_conn, "tsr_cr"), "yes",
                "compact_rewrite: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_cr"), "t",
                "compact_rewrite: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_cr"), "yes",
                "compact_rewrite: approx_count_distinct folds every row group from its sketch and scans none")


def test_recluster(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_rc", "SELECT pgcolumnar.recluster('tsr_rc', 'b')")
    expect.text(_whole(pgc_conn, "tsr_rc"), "yes",
                "recluster: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_rc"), "t",
                "recluster: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_rc"), "yes",
                "recluster: approx_count_distinct folds every row group from its sketch and scans none")


def test_vacuum_sorted(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_vs", "SELECT pgcolumnar.vacuum_sorted('tsr_vs', 'b')")
    expect.text(_whole(pgc_conn, "tsr_vs"), "yes",
                "vacuum_sorted: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_vs"), "t",
                "vacuum_sorted: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_vs"), "yes",
                "vacuum_sorted: approx_count_distinct folds every row group from its sketch and scans none")


def test_truncate_and_reinsert(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_tr", "TRUNCATE tsr_tr",
               "INSERT INTO tsr_tr SELECT g, g FROM generate_series(1, 5000) g WHERE g % 3 <> 0")
    expect.text(_whole(pgc_conn, "tsr_tr"), "yes",
                "TRUNCATE and reinsert: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_tr"), "t",
                "TRUNCATE and reinsert: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_tr"), "yes",
                "TRUNCATE and reinsert: approx_count_distinct folds every row group from its sketch and scans none")


def test_alter_column_type(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_at", "ALTER TABLE tsr_at ALTER COLUMN b TYPE bigint")
    expect.text(_whole(pgc_conn, "tsr_at"), "yes",
                "ALTER COLUMN TYPE: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_at"), "t",
                "ALTER COLUMN TYPE: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_at"), "yes",
                "ALTER COLUMN TYPE: approx_count_distinct folds every row group from its sketch and scans none")
    expect.text(_groups(pgc_conn, "tsr_at"), "4",
                "ALTER COLUMN TYPE: the rewrite keeps the table's stripe_row_limit")


def test_add_column_with_a_volatile_default(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_ac", "ALTER TABLE tsr_ac ADD COLUMN c float8 DEFAULT random()")
    expect.text(_whole(pgc_conn, "tsr_ac"), "yes",
                "ADD COLUMN with a volatile default: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_ac"), "t",
                "ADD COLUMN with a volatile default: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_ac"), "yes",
                "ADD COLUMN with a volatile default: approx_count_distinct folds every row group from its sketch and scans none")
    expect.text(_groups(pgc_conn, "tsr_ac"), "4",
                "ADD COLUMN with a volatile default: the rewrite keeps the table's stripe_row_limit")


def test_set_access_method_heap_and_back(pgc_conn, expect):
    _rewritten(pgc_conn, "tsr_am", "ALTER TABLE tsr_am SET ACCESS METHOD heap",
               "ALTER TABLE tsr_am SET ACCESS METHOD pgcolumnar")
    expect.text(_whole(pgc_conn, "tsr_am"), "yes",
                "SET ACCESS METHOD heap and back: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(pgc_conn, "tsr_am"), "t",
                "SET ACCESS METHOD heap and back: the sketches describe exactly the live rows")
    expect.text(_folds_all(pgc_conn, "tsr_am"), "yes",
                "SET ACCESS METHOD heap and back: approx_count_distinct folds every row group from its sketch and scans none")
    expect.text(_groups(pgc_conn, "tsr_am"), "4",
                "SET ACCESS METHOD heap and back: the rewrite keeps the table's stripe_row_limit")


def test_alter_column_type_keeps_compression(pgc_conn, expect):
    """every option, not only the sketch: compression none came back compressed"""
    conn = pgc_conn
    _run(conn, "CREATE TABLE tsr_cz (a bigint, b text) USING pgcolumnar")
    _run(conn, "SELECT pgcolumnar.set_options('tsr_cz', compression => 'none')")
    _run(conn, "INSERT INTO tsr_cz SELECT g, repeat('x', 200) || g FROM generate_series(1, 20000) g")
    _run(conn, "ALTER TABLE tsr_cz ALTER COLUMN a TYPE numeric")
    expect.text(str(_one(conn, "SELECT string_agg(DISTINCT block_codec::text, ',') FROM pgcolumnar.column_chunk c"
                               " WHERE c.storage_id = pgcolumnar.get_storage_id('tsr_cz')")), "0",
                "ALTER COLUMN TYPE: the rewrite keeps the table's compression")


def test_refresh_materialized_view(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE tsr_src (a bigint, b int)")
    _run(conn, "INSERT INTO tsr_src SELECT g, g FROM generate_series(1, 5000) g WHERE g % 3 <> 0")
    _run(conn, "CREATE MATERIALIZED VIEW tsr_mv USING pgcolumnar AS SELECT a, b FROM tsr_src WITH NO DATA")
    _run(conn, "SELECT pgcolumnar.set_options('tsr_mv', stripe_row_limit => 1000,"
               " chunk_group_row_limit => 1000, sketch_columns => ARRAY['a'])")
    _run(conn, "REFRESH MATERIALIZED VIEW tsr_mv")
    expect.text(_whole(conn, "tsr_mv"), "yes",
                "REFRESH MATERIALIZED VIEW: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(conn, "tsr_mv"), "t",
                "REFRESH MATERIALIZED VIEW: the sketches describe exactly the live rows")
    expect.text(_groups(conn, "tsr_mv"), "4",
                "REFRESH MATERIALIZED VIEW: the refresh keeps the view's stripe_row_limit")


def test_refresh_materialized_view_concurrently(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE tsr_csrc (a bigint, b int)")
    _run(conn, "INSERT INTO tsr_csrc SELECT g, g FROM generate_series(1, 5000) g WHERE g % 3 <> 0")
    _run(conn, "CREATE MATERIALIZED VIEW tsr_cmv USING pgcolumnar AS SELECT a, b FROM tsr_csrc WITH NO DATA")
    _run(conn, "SELECT pgcolumnar.set_options('tsr_cmv', stripe_row_limit => 1000,"
               " chunk_group_row_limit => 1000, sketch_columns => ARRAY['a'])")
    _run(conn, "REFRESH MATERIALIZED VIEW tsr_cmv")
    _run(conn, "CREATE UNIQUE INDEX tsr_cmv_a ON tsr_cmv (a)")
    _run(conn, "INSERT INTO tsr_csrc SELECT g, g FROM generate_series(5001, 6000) g")
    _run(conn, "REFRESH MATERIALIZED VIEW CONCURRENTLY tsr_cmv")
    expect.text(_whole(conn, "tsr_cmv"), "yes",
                "REFRESH MATERIALIZED VIEW CONCURRENTLY: every row group has its sketch, and no sketch outlives its row group")
    expect.text(_union_is_live(conn, "tsr_cmv"), "t",
                "REFRESH MATERIALIZED VIEW CONCURRENTLY: the sketches describe exactly the live rows")


def test_a_user_table_named_like_a_transient_borrows_nothing(pgc_conn, expect):
    """its creator holds no ExclusiveLock on the relation the name points at"""
    conn = pgc_conn
    _base_table(conn, "tsr_donor")
    oid = _one(conn, "SELECT 'tsr_donor'::regclass::oid")
    _run(conn, f'CREATE TABLE "pg_temp_{oid}" USING pgcolumnar AS SELECT g::bigint AS a, g AS b'
               " FROM generate_series(1, 3000) g")
    got = _one(conn, f"""SELECT (SELECT count(*) FROM pgcolumnar.row_group r
            WHERE r.storage_id = pgcolumnar.get_storage_id('"pg_temp_{oid}"'))
        || '|' || (SELECT count(*) FROM pgcolumnar.sketch k
            WHERE k.storage_id = pgcolumnar.get_storage_id('"pg_temp_{oid}"'))""")
    expect.text(str(got), "1|0",
                "a user table named pg_temp_<oid> keeps its own defaults: one row group and no sketch")

"""A maintenance function that rolls back leaves the table as it was (#1341). The
pytest twin of `test/maintenance_rollback.sh`, every check by the shell's name.

compact_rewrite, recluster, compact, vacuum and vacuum_sorted each retire row groups,
and most write new ones. Retirement is a catalog delete, so a rollback must bring
every retired group back with the bytes it points at intact. The space a retired
group frees may be reused only once no snapshot can need it, and never by the
transaction that freed it, which can still roll back.

The table has row groups of 1000 rows, two in five rows deleted, and one group
deleted whole, so every function has work. Each runs inside a transaction, its effect
on the row groups is read there, and the transaction rolls back. The table must then
hold the same rows by content and the same row groups, and an index scan must agree.
The last arms do the same under a savepoint, commit further writes, and run every
function for real.
"""
import psycopg

ROWS = ("SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id)) "
        "FROM mr")
GROUPS = ("SELECT count(*) || '/' || coalesce(sum(row_count), 0) FROM pgcolumnar.row_group "
          "WHERE storage_id = pgcolumnar.get_storage_id('mr')")


def _one(conn, sql):
    """-> the first column of the only row, or the error's first line."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]
    except psycopg.Error as exc:
        return f"ERROR: {str(exc).splitlines()[0]}"


BY_INDEX_SETS = ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan")


def _by_index(conn, explain=False):
    # the custom scan off too, and asserted by a premise: with only seqscan off this
    # whole-table range planned as Custom Scan (PgColumnarScan) and read no index
    with conn.cursor() as cur:
        for g in BY_INDEX_SETS:
            cur.execute(f"SET {g} = off")
    try:
        if explain:
            with conn.cursor() as cur:
                cur.execute("EXPLAIN (COSTS OFF) " + ROWS + " WHERE id BETWEEN 1 AND 10000")
                return "yes" if any("Index Scan using" in r[0] for r in cur.fetchall()) else "no"
        return _one(conn, ROWS + " WHERE id BETWEEN 1 AND 10000")
    finally:
        with conn.cursor() as cur:
            for g in BY_INDEX_SETS:
                cur.execute(f"RESET {g}")


def _build_mr(conn):
    """The table, made again: 10 row groups of 1000, 5400 rows left. Each function
    gets a fresh one, so a rollback that corrupts it fails only its own arm."""
    with conn.cursor() as cur:
        cur.execute("SET pgcolumnar.stripe_row_limit = 1000")
        cur.execute("DROP TABLE IF EXISTS mr")
        cur.execute("CREATE TABLE mr (id int, v int, w text) USING pgcolumnar")
        cur.execute("INSERT INTO mr SELECT g, (g * 7919) % 10007, md5(g::text) FROM generate_series(1, 10000) g")
        cur.execute("CREATE INDEX mr_id ON mr (id)")
        cur.execute("DELETE FROM mr WHERE id % 5 IN (1, 2) OR id BETWEEN 3001 AND 4000")
        cur.execute("RESET pgcolumnar.stripe_row_limit")


def test_a_maintenance_function_that_rolls_back_leaves_the_table_as_it_was(pgc_conn, expect):
    funcs = ("compact_rewrite('mr', 0.0)", "recluster('mr', 'v')", "compact('mr')",
             "vacuum('mr')", "vacuum_sorted('mr', 'v')")
    _build_mr(pgc_conn)
    rows0, groups0 = _one(pgc_conn, ROWS), _one(pgc_conn, GROUPS)
    expect.text(f"{rows0.split('/')[0]}|{groups0}", "5400|10/10000",
                "premise: the table holds the rows it was given")
    expect.text(_by_index(pgc_conn, explain=True), "yes", "premise: the by-index reading is an Index Scan through mr_id")

    for f in funcs:
        name = f.split("(")[0]
        _build_mr(pgc_conn)
        # BEGIN and ROLLBACK as statements: pgc_conn is a watching wrapper, and an
        # autocommit flag set on it would not reach the connection, so the
        # function would commit and the rollback would do nothing.
        with pgc_conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                cur.execute(f"SELECT pgcolumnar.{f}")
                inside = _one(pgc_conn, GROUPS)
            finally:
                cur.execute("ROLLBACK")
        expect.text("changed" if inside != groups0 and "ERROR" not in inside else f"unchanged ({inside})",
                    "changed", f"{name} changed the row groups inside its transaction")
        expect.text(f"{_one(pgc_conn, ROWS)}|{_one(pgc_conn, GROUPS)}|{_by_index(pgc_conn)}",
                    f"{rows0}|{groups0}|{rows0}",
                    f"after {name} rolls back, the table holds the same rows and row groups, by scan and by index")

    _build_mr(pgc_conn)
    with pgc_conn.cursor() as cur:
        cur.execute("BEGIN")
        try:
            cur.execute("SAVEPOINT s")
            cur.execute("SELECT pgcolumnar.compact_rewrite('mr', 0.0)")
            cur.execute("SELECT pgcolumnar.recluster('mr', 'v')")
            cur.execute("ROLLBACK TO s")
            cur.execute("SET LOCAL pgcolumnar.stripe_row_limit = 1000")
            cur.execute("INSERT INTO mr SELECT g, g, 'new' FROM generate_series(20001, 21000) g")
            cur.execute("COMMIT")
        except psycopg.Error:
            cur.execute("ROLLBACK")
            raise
    rows1 = _one(pgc_conn, ROWS)
    new = _one(pgc_conn, "SELECT count(*) FILTER (WHERE w = 'new') || '/' || count(*) FROM mr")
    expect.text(f"{new}|{str(_by_index(pgc_conn)).split('/')[0]}", "1000/6400|5400",
                "after a savepoint rolls back two rewrites, the rows written later in the transaction commit beside the old ones")
    for f in ("compact_rewrite('mr', 0.0)", "recluster('mr', 'v')", "compact('mr')", "vacuum('mr')"):
        with pgc_conn.cursor() as cur:
            cur.execute(f"SELECT pgcolumnar.{f}")
    expect.text(_one(pgc_conn, ROWS), rows1, "and every function then runs for real and keeps the same rows")

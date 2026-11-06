"""Read-your-writes, DELETE, UPDATE, ROLLBACK, savepoints and temporary tables on a
columnar table, plus the phase 1/2 regression checks: the pytest twin of
`test/phase3.sh`, every check by the shell's name (#1341).

A savepoint's own discard is reached only by a statement that FAILS PARTWAY inside it:
executor end flushes a completed statement's buffers under its subtransaction, and the
abort's xmin verdict hides those (#1441). So an INSERT whose 2001st row divides by
zero, and a DELETE that fails at a = 9, are each rolled back to a savepoint, and both
must leave nothing behind, inside the transaction and after it. With the discard of
pending writes or pending deletes removed, every other check stayed green. A DELETE
whose RETURNING reads the table in a block that commits into one that aborts must see
the rows deleted so far, as heap does: with the commit not handing the read's written
marks to its parent, the abort left them attributed to a finished block and no later
read wrote them again. The
deleted range must straddle a vector boundary, read from the zone maps, as the shell
says it does.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order, because the
delete_vector and row_group checks read the database's whole catalog. Statements the
shell runs in one psql session run on one connection here. A reading is a value or a
query_error(), never text that stands in for one.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_phase3"
DECX = ("((get_byte(maximum,3)::bigint<<24)|(get_byte(maximum,2)<<16)"
        "|(get_byte(maximum,1)<<8)|get_byte(maximum,0))")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v)


def _seq(dsn, *steps):
    """Run `steps` on one session -> each row-returning statement's rows as psql -At
    prints them. A step (sql, sqlstate) may fail with exactly that SQLSTATE. Any other
    failure ends the session, and it and every reading after it are query_error()s."""
    out = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for st in steps:
                if isinstance(st, tuple):
                    sql, state = st
                    try:
                        conn.execute(sql)
                    except psycopg.Error as exc:
                        if exc.sqlstate != state:
                            raise
                    continue
                cur = conn.execute(st)
                if cur.description is not None:
                    out.append("\n".join("|".join(_cell(v) for v in r) for r in cur.fetchall()))
    except psycopg.Error as exc:
        out.append(query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__))
    return out


def _at(out, i):
    return out[i] if i < len(out) else query_error(f"no reading {i}: the session ended early")


def _q(dsn, *steps):
    return _at(_seq(dsn, *steps), 0)


def _qq(dsn, sql):
    """-> every row followed by a comma, as the shell's qq prints them."""
    v = _q(dsn, sql)
    return v if v.startswith("QUERY_ERROR") else "".join(l + "," for l in v.split("\n") if v)


@pytest.fixture(scope="module")
def p3(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        q = lambda *s: _q(dsn, *s)
        q("CREATE EXTENSION pgcolumnar")
        r = {}

        q("CREATE TABLE reg (a int, b text) USING pgcolumnar")
        q("INSERT INTO reg SELECT g, g::text FROM generate_series(1,50000) g")
        r["reg count"] = q("SELECT count(*) FROM reg")
        r["reg filter a<100"] = q("SELECT count(*) FROM reg WHERE a < 100")
        q("DROP TABLE reg")

        r["ryw count"] = q("BEGIN", "CREATE TABLE ryw (a int) USING pgcolumnar",
                           "INSERT INTO ryw SELECT g FROM generate_series(1,1000) g",
                           "SELECT count(*) FROM ryw", "COMMIT")
        r["ryw sum"] = q("BEGIN", "INSERT INTO ryw VALUES (100000)", "SELECT sum(a) FROM ryw", "COMMIT")
        r["ryw persisted"] = q("SELECT count(*) FROM ryw")
        q("DROP TABLE ryw")

        q("CREATE TABLE d (id int, v text) USING pgcolumnar")
        q("INSERT INTO d SELECT g, 'r'||g FROM generate_series(1,25000) g")
        r["the deleted range straddles vectors 0 and 1"] = q(
            f"SELECT string_agg({DECX}::text, chr(44) ORDER BY vector_index) FROM pgcolumnar.zone_map "
            "WHERE storage_id = pgcolumnar.get_storage_id('d') AND column_index = 0 AND vector_index >= 0")
        q("DELETE FROM d WHERE id BETWEEN 9995 AND 10005")
        r["delete count"] = q("SELECT count(*) FROM d")
        r["delete gone"] = q("SELECT count(*) FROM d WHERE id BETWEEN 9995 AND 10005")
        r["delete neighbors"] = _qq(dsn, "SELECT id FROM d WHERE id IN (9994,10006) ORDER BY id")
        r["delete boundaries"] = q("SELECT min(id)||chr(47)||max(id) FROM d")
        q("DELETE FROM d WHERE id > 20000")
        r["delete tail count"] = q("SELECT count(*) FROM d")
        r["delete tail max"] = q("SELECT max(id) FROM d")
        r["delete_vector rows>0"] = q("SELECT (count(*) > 0)::int FROM pgcolumnar.delete_vector")
        r["delete_vector deleted"] = q("SELECT sum(deleted_count) FROM pgcolumnar.delete_vector")
        q("DROP TABLE d")

        q("CREATE TABLE u (id int, v text) USING pgcolumnar")
        q("INSERT INTO u SELECT g, 'r'||g FROM generate_series(1,5000) g")
        q("UPDATE u SET v='changed' WHERE id = 2500")
        r["update value"] = q("SELECT v FROM u WHERE id = 2500")
        r["update one row"] = q("SELECT count(*) FROM u WHERE v = 'changed'")
        r["update count"] = q("SELECT count(*) FROM u")
        q("UPDATE u SET id = id + 100000 WHERE id <= 10")
        r["update key gone"] = q("SELECT count(*) FROM u WHERE id <= 10")
        r["update key new"] = q("SELECT count(*) FROM u WHERE id > 100000")
        r["update key total"] = q("SELECT count(*) FROM u")
        q("UPDATE u SET v = v || 'ZZ'")
        r["bulk update rows"] = q("SELECT count(*) FROM u")
        r["bulk update sfx"] = q("SELECT count(*) FROM u WHERE right(v,2) = 'ZZ'")
        q("DROP TABLE u")

        q("CREATE TABLE rb (a int) USING pgcolumnar")
        q("INSERT INTO rb SELECT g FROM generate_series(1,100) g")
        r["rb before"] = q("SELECT count(*) FROM rb")
        _seq(dsn, "BEGIN", "INSERT INTO rb SELECT g FROM generate_series(101,200) g",
             "DELETE FROM rb WHERE a <= 50", "ROLLBACK")
        r["rb after count"] = q("SELECT count(*) FROM rb")
        r["rb after range"] = q("SELECT min(a)||chr(47)||max(a) FROM rb")
        q("DROP TABLE rb")

        q("CREATE TABLE sp (a int) USING pgcolumnar")
        q("INSERT INTO sp SELECT g FROM generate_series(1,10) g")
        r["savepoint in-txn"] = q("BEGIN", "INSERT INTO sp VALUES (11)", "DELETE FROM sp WHERE a = 1",
                                  "SAVEPOINT s1", "INSERT INTO sp VALUES (12)", "DELETE FROM sp WHERE a = 2",
                                  "ROLLBACK TO s1", "SELECT string_agg(a::text, ',' ORDER BY a) FROM sp",
                                  "COMMIT")
        r["savepoint final"] = _qq(dsn, "SELECT a FROM sp ORDER BY a")
        out = _seq(dsn, "BEGIN", "SAVEPOINT s2",
                   ("INSERT INTO sp SELECT CASE WHEN g < 2100 THEN g ELSE 1 / (g - g) END "
                    "FROM generate_series(100, 3000) g", "22012"),
                   "ROLLBACK TO s2", "SELECT count(*) FROM sp WHERE a >= 100", "SAVEPOINT s3",
                   ("DELETE FROM sp WHERE a < 11 AND 1 / (a - 9) IS NOT NULL", "22012"),
                   "ROLLBACK TO s3", "SELECT string_agg(a::text, chr(44) ORDER BY a) FROM sp", "COMMIT")
        r["savepoint failed insert discarded"] = _at(out, 0)
        r["savepoint failed delete discarded"] = _at(out, 1)
        r["savepoint failed final"] = _qq(dsn, "SELECT a FROM sp ORDER BY a")
        q("DROP TABLE sp")

        q("""CREATE FUNCTION spn_rd(t text) RETURNS int LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
	BEGIN
		BEGIN
			EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
		EXCEPTION WHEN no_data_found THEN NULL;
		END;
		PERFORM 1 / 0;
	EXCEPTION WHEN division_by_zero THEN NULL;
	END;
	RETURN n;
END $$""")

        def spn(am):
            out = _seq(dsn, f"CREATE TABLE spn_{am} (id int) USING {am}",
                       f"INSERT INTO spn_{am} SELECT g FROM generate_series(1, 200) g",
                       f"WITH d AS (DELETE FROM spn_{am} WHERE id % 2 = 0 RETURNING spn_rd('spn_{am}') AS n) "
                       "SELECT sum(n) FROM d",
                       f"SELECT count(*) || chr(124) || sum(id) FROM spn_{am}")
            return next((v for v in out if v.startswith("QUERY_ERROR")), None) or "|".join(out)

        r["savepoint nested read: pgcolumnar"] = spn("pgcolumnar")
        r["savepoint nested read: heap"] = spn("heap")
        q("DROP TABLE spn_pgcolumnar, spn_heap", "DROP FUNCTION spn_rd(text)")

        r["temp survivors"] = q("CREATE TEMP TABLE t_tmp (a int, b text) USING pgcolumnar",
                                "INSERT INTO t_tmp SELECT g, g::text FROM generate_series(1,2000) g",
                                "DELETE FROM t_tmp WHERE a % 5 = 0", "UPDATE t_tmp SET b = 'u' WHERE a = 3",
                                "SELECT count(*) FROM t_tmp")
        r["temp deleted"] = q("CREATE TEMP TABLE t_tmp2 (a int) USING pgcolumnar",
                              "INSERT INTO t_tmp2 SELECT g FROM generate_series(1,2000) g",
                              "DELETE FROM t_tmp2 WHERE a % 5 = 0",
                              "SELECT count(*) FROM t_tmp2 WHERE a % 5 = 0")

        q("CREATE TABLE dur (a int) USING pgcolumnar")
        q("INSERT INTO dur SELECT g FROM generate_series(1,1000) g")
        q("DELETE FROM dur WHERE a % 2 = 0")
        r["durable survivors"] = q("SELECT count(*) FROM dur")
        r["durable odd only"] = q("SELECT count(*) FROM dur WHERE a % 2 = 0")
        q("DROP TABLE dur")
        r["delete_vector cleaned"] = q("SELECT count(*) FROM pgcolumnar.delete_vector")
        r["row group cleaned"] = q("SELECT count(*) FROM pgcolumnar.row_group")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_regression_table_counts_every_row(p3, expect):
    expect.text(p3["reg count"], "50000", "reg count")


def test_the_regression_table_filters(p3, expect):
    expect.text(p3["reg filter a<100"], "99", "reg filter a<100")


def test_a_table_created_in_the_transaction_reads_its_rows(p3, expect):
    expect.text(p3["ryw count"], "1000", "ryw count")


def test_an_insert_is_read_by_its_own_transaction(p3, expect):
    expect.text(p3["ryw sum"], "600500", "ryw sum")


def test_the_transaction_s_rows_persist(p3, expect):
    expect.text(p3["ryw persisted"], "1001", "ryw persisted")


def test_the_deleted_range_straddles_a_vector_boundary(p3, expect):
    expect.text(p3["the deleted range straddles vectors 0 and 1"], "10000,20000,25000", "the deleted range straddles vectors 0 and 1")


def test_a_delete_leaves_the_survivors(p3, expect):
    expect.text(p3["delete count"], "24989", "delete count")


def test_deleted_rows_are_gone(p3, expect):
    expect.text(p3["delete gone"], "0", "delete gone")


def test_the_deleted_range_s_neighbours_survive(p3, expect):
    expect.text(p3["delete neighbors"], "9994,10006,", "delete neighbors")


def test_the_table_s_bounds_survive(p3, expect):
    expect.text(p3["delete boundaries"], "1/25000", "delete boundaries")


def test_a_tail_delete_leaves_the_survivors(p3, expect):
    expect.text(p3["delete tail count"], "19989", "delete tail count")


def test_a_tail_delete_lowers_the_maximum(p3, expect):
    expect.text(p3["delete tail max"], "20000", "delete tail max")


def test_the_delete_vector_catalog_has_rows(p3, expect):
    expect.text(p3["delete_vector rows>0"], "1", "delete_vector rows>0")


def test_the_delete_vector_catalog_counts_every_deletion(p3, expect):
    expect.text(p3["delete_vector deleted"], "5011", "delete_vector deleted")


def test_an_update_shows_the_new_value(p3, expect):
    expect.text(p3["update value"], "changed", "update value")


def test_an_update_changes_one_row(p3, expect):
    expect.text(p3["update one row"], "1", "update one row")


def test_an_update_keeps_the_row_count(p3, expect):
    expect.text(p3["update count"], "5000", "update count")


def test_a_key_update_removes_the_old_keys(p3, expect):
    expect.text(p3["update key gone"], "0", "update key gone")


def test_a_key_update_writes_the_new_keys(p3, expect):
    expect.text(p3["update key new"], "10", "update key new")


def test_a_key_update_keeps_the_row_count(p3, expect):
    expect.text(p3["update key total"], "5000", "update key total")


def test_a_bulk_update_keeps_the_row_count(p3, expect):
    expect.text(p3["bulk update rows"], "5000", "bulk update rows")


def test_a_bulk_update_rewrites_every_row(p3, expect):
    expect.text(p3["bulk update sfx"], "5000", "bulk update sfx")


def test_the_rollback_table_starts_with_its_rows(p3, expect):
    expect.text(p3["rb before"], "100", "rb before")


def test_a_rolled_back_transaction_leaves_the_count(p3, expect):
    expect.text(p3["rb after count"], "100", "rb after count")


def test_a_rolled_back_transaction_leaves_the_range(p3, expect):
    expect.text(p3["rb after range"], "1/100", "rb after range")


def test_rollback_to_a_savepoint_keeps_the_work_before_it(p3, expect):
    expect.text(p3["savepoint in-txn"], "2,3,4,5,6,7,8,9,10,11", "savepoint in-txn")


def test_rollback_to_a_savepoint_holds_after_commit(p3, expect):
    expect.text(p3["savepoint final"], "2,3,4,5,6,7,8,9,10,11,", "savepoint final")


def test_an_insert_that_failed_inside_a_savepoint_leaves_nothing(p3, expect):
    expect.text(p3["savepoint failed insert discarded"], "0", "savepoint failed insert discarded")


def test_a_delete_that_failed_inside_a_savepoint_deletes_nothing(p3, expect):
    expect.text(p3["savepoint failed delete discarded"], "2,3,4,5,6,7,8,9,10,11", "savepoint failed delete discarded")


def test_the_failed_statements_leave_nothing_after_commit(p3, expect):
    expect.text(p3["savepoint failed final"], "2,3,4,5,6,7,8,9,10,11,", "savepoint failed final")


def test_a_read_in_a_block_that_commits_into_an_aborted_one_sees_the_deletes(p3, expect):
    expect.text(p3["savepoint nested read: pgcolumnar"], p3["savepoint nested read: heap"],
                "savepoint nested read in a delete sees its deleted rows, as on heap")


def test_a_temporary_table_keeps_its_survivors(p3, expect):
    expect.text(p3["temp survivors"], "1600", "temp survivors")


def test_a_temporary_table_s_deleted_rows_are_gone(p3, expect):
    expect.text(p3["temp deleted"], "0", "temp deleted")


def test_deletes_survive_their_transaction(p3, expect):
    expect.text(p3["durable survivors"], "500", "durable survivors")


def test_only_the_undeleted_rows_survive(p3, expect):
    expect.text(p3["durable odd only"], "0", "durable odd only")


def test_drop_removes_the_delete_vector_rows(p3, expect):
    expect.text(p3["delete_vector cleaned"], "0", "delete_vector cleaned")


def test_drop_removes_the_row_group_rows(p3, expect):
    expect.text(p3["row group cleaned"], "0", "row group cleaned")

"""TRUNCATE leaves no catalog rows behind, and keeps what it must (#1341). The pytest
twin of `test/truncate_cleanup.sh`, every check by the shell's name.

TRUNCATE installs a new relfilenode and a new storage id. It used to leave the old
storage's row_group, column_chunk, zone_map and bloom rows behind, keyed by the retired
id, so each truncate-and-reload cycle leaked one storage's worth, and a DROP after it
removed only the current, empty one. Retiring them made a cached write state stale, so
a transaction that wrote, truncated and wrote again committed into storage nothing
reads; the state is now dropped with the rows. A relation created in the current
transaction is truncated in place instead, on a different path, which must clear each
projection's storage as well as the base's.

A PRIVATE DATABASE, because the snapshot counts every pgcolumnar catalog row, and the
shared one holds every other test's.
"""
import psycopg


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _run(conn, *sqls):
    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


def _snapshot(conn):
    return _one(conn, "SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' || "
                      "(SELECT count(*) FROM pgcolumnar.projection) || '/' || "
                      "(SELECT count(*) FROM pgcolumnar.row_group) || '/' || "
                      "(SELECT count(*) FROM pgcolumnar.column_chunk) || '/' || "
                      "(SELECT count(*) FROM pgcolumnar.zone_map) || '/' || "
                      "(SELECT count(*) FROM pgcolumnar.bloom) || '/' || "
                      "(SELECT count(*) FROM pgcolumnar.delete_vector)")


def _grew(base, now):
    return "no" if base == now else "yes"


LOAD = "INSERT INTO {t} SELECT g, 'x' || g FROM generate_series(1, {n}) g"


def test_truncate_and_drop_leave_the_catalog_as_they_found_it(pgc_own_db, expect):
    conn = pgc_own_db
    base = _snapshot(conn)
    _run(conn, "CREATE TABLE tcl_plain (id int, v text) USING pgcolumnar", LOAD.format(t="tcl_plain", n=5000))
    expect.text(_grew(base, _snapshot(conn)), "yes", "insert did add metadata")
    _run(conn, "TRUNCATE tcl_plain")
    expect.text(_snapshot(conn), base, "TRUNCATE returns the catalog to the empty baseline")
    expect.num(_one(conn, "SELECT count(*) FROM tcl_plain"), 0, "TRUNCATE leaves no rows")
    for _ in range(2):
        _run(conn, LOAD.format(t="tcl_plain", n=5000), "TRUNCATE tcl_plain")
    expect.text(_snapshot(conn), base, "three truncate-and-reload cycles still sit at the baseline")
    # loaded first, so the DROP has work to do: a DROP of an empty table could not fail
    _run(conn, LOAD.format(t="tcl_plain", n=5000))
    expect.text(_grew(base, _snapshot(conn)), "yes",
                "premise: the table carries metadata again, so the DROP has work to do")
    _run(conn, "DROP TABLE tcl_plain")
    expect.text(_snapshot(conn), base, "DROP of a loaded table after TRUNCATE leaves nothing")


def test_a_projection_is_not_leaked_by_truncate(pgc_own_db, expect):
    """A projection has its own storage id. What must hold is that no projection row
    names a storage id the table no longer has, not that projection is empty."""
    conn = pgc_own_db
    base = _snapshot(conn)
    _run(conn, "CREATE TABLE tcl_proj (id int, a int, b text) USING pgcolumnar",
         "SELECT pgcolumnar.add_projection('tcl_proj', 'tcl_p', ARRAY['a', 'b'], ARRAY['a'])",
         "INSERT INTO tcl_proj SELECT g, g % 50, 'b' || g FROM generate_series(1, 5000) g")
    expect.text(_grew(base, _snapshot(conn)), "yes", "the projection did add metadata")
    _run(conn, "TRUNCATE tcl_proj")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.projection p "
                          "WHERE p.storage_id <> pgcolumnar.get_storage_id('tcl_proj'::regclass)"), 0,
               "TRUNCATE leaves no projection row under a retired storage id")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.projection_declaration "
                          "WHERE rel = 'tcl_proj'::regclass"), 1,
               "and the declaration survives the TRUNCATE, so the projection is rebuildable")
    _run(conn, "INSERT INTO tcl_proj SELECT g, g % 50, 'b' || g FROM generate_series(1, 5000) g")
    expect.text(_grew(base, _snapshot(conn)), "yes", "premise: the projected table carries metadata again before the DROP")
    _run(conn, "DROP TABLE tcl_proj")
    expect.text(_snapshot(conn), base, "DROP of a loaded projected table leaves nothing")


def test_repeated_truncates_accumulate_nothing(pgc_own_db, expect):
    """Created once outside the loop and one statement at a time, so every TRUNCATE
    takes the transactional path and any leak accumulates."""
    conn = pgc_own_db
    base = _snapshot(conn)
    _run(conn, "CREATE TABLE tcl_rep (id int, v text) USING pgcolumnar")
    for _ in range(10):
        _run(conn, LOAD.format(t="tcl_rep", n=2000), "TRUNCATE tcl_rep")
    expect.text(_snapshot(conn), base, "ten insert-and-truncate cycles on one table leave nothing")
    _run(conn, "DROP TABLE tcl_rep")
    expect.text(_snapshot(conn), base, "and dropping it afterwards still leaves nothing")


def test_insert_truncate_insert_in_one_transaction(pgc_own_db, expect):
    """The cached write state named the storage the TRUNCATE retired, so the second
    INSERT flushed into storage nothing reads, and the transaction committed an empty
    table. The status and the surviving rows are one arm: split, a commit that keeps
    nothing passes the status half, and 1..10 passes a bare count of 10."""
    conn = pgc_own_db
    _run(conn, "CREATE TABLE tc_txn (id int) USING pgcolumnar",
         "INSERT INTO tc_txn SELECT g FROM generate_series(1, 10) g")
    expect.num(_one(conn, "SELECT count(*) FROM tc_txn"), 10, "premise: the table holds its first ten rows")
    rc = 0
    try:
        _run(conn, "BEGIN", "INSERT INTO tc_txn SELECT g FROM generate_series(11, 20) g", "TRUNCATE tc_txn",
             "INSERT INTO tc_txn SELECT g FROM generate_series(21, 30) g", "COMMIT")
    except psycopg.Error:
        rc = 1
        _run(conn, "ROLLBACK")
    expect.text(f"rc={rc} " + _one(conn, "SELECT coalesce(min(id), -1) || '..' || coalesce(max(id), -1) || "
                                         "' n=' || count(*) FROM tc_txn"),
                "rc=0 21..30 n=10", "INSERT, TRUNCATE, INSERT in one transaction commits and keeps the new rows")


def test_a_rewrite_that_is_not_a_truncate_loses_nothing(pgc_own_db, expect):
    """The retire runs from relation_set_new_filelocator, which every rewrite calls."""
    conn = pgc_own_db
    _run(conn, "CREATE TABLE tc_rw (id int, v text) USING pgcolumnar",
         "INSERT INTO tc_rw SELECT g, 'v' || g FROM generate_series(1, 5000) g")
    expect.num(_one(conn, "SELECT count(*) FROM tc_rw"), 5000, "premise: five thousand rows before the rewrite")
    _run(conn, "ALTER TABLE tc_rw ALTER COLUMN v TYPE varchar(64)")
    expect.text(_one(conn, "SELECT count(*) || '/' || count(v) || '/' || max(id) FROM tc_rw"), "5000/5000/5000",
                "a full rewrite keeps every row, and they are still readable")


def test_an_in_place_truncate_clears_the_projection_too(pgc_own_db, expect):
    """A relation created in the current transaction is truncated in place, through
    nontransactional_truncate, which cleared the base storage and left the
    projection's. The write after the truncate is what makes the leak observable: it
    collided on row_group_pkey (#896)."""
    conn = pgc_own_db
    _run(conn, "CREATE TABLE tcl_inplace_pre (id int)")
    committed = "committed"
    try:
        _run(conn, "BEGIN",
             "CREATE TABLE tcl_inplace (id int, a int, b text) USING pgcolumnar",
             "INSERT INTO tcl_inplace SELECT g, g % 50, 'b' || g FROM generate_series(1, 2000) g",
             "SELECT pgcolumnar.add_projection('tcl_inplace', 'tcl_ip', ARRAY['a', 'b'], ARRAY['a'])",
             "TRUNCATE tcl_inplace",
             "INSERT INTO tcl_inplace SELECT g, g % 50, 'c' || g FROM generate_series(1, 500) g",
             "COMMIT")
    except psycopg.Error as exc:
        committed = f"rolled back ({str(exc).splitlines()[0]})"
        _run(conn, "ROLLBACK")
    # vacuous without these: a rolled-back transaction leaves no table to count
    expect.num(0 if committed == "committed" else 1, 0, "PREMISE the in-place transaction committed")
    expect.num(_one(conn, "SELECT count(*) FROM pg_class WHERE relname = 'tcl_inplace'"), 1,
               "PREMISE the table exists after it")
    if committed != "committed":
        return
    expect.num(_one(conn, "SELECT count(*) FROM tcl_inplace"), 500,
               "the rows written after the in-place truncate are all there")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.read_projection('tcl_inplace', 'tcl_ip')"), 500,
               "and the projection reads them back")
    with conn.cursor() as cur:
        cur.execute("SELECT pgcolumnar.read_projection('tcl_inplace', 'tcl_ip')")
        proj = cur.fetchall()
        cur.execute("SELECT a::text || '|' || b FROM tcl_inplace")
        base = cur.fetchall()
    expect.row_set(proj, base, "the projection agrees with the base table")
    proj_storage = _one(conn, "SELECT proj_storage_id FROM pgcolumnar.projection "
                              "WHERE storage_id = pgcolumnar.get_storage_id('tcl_inplace'::regclass) "
                              "AND projection_id > 0")
    base_storage = _one(conn, "SELECT pgcolumnar.get_storage_id('tcl_inplace'::regclass)")
    expect.text("yes" if proj_storage is not None and proj_storage != base_storage else "no", "yes",
                "PREMISE the projection has its own storage, distinct from the base")
    # one group of 500 rows, not a group NUMBER: the base's counter is reset and the
    # projection's has no metapage to reset, so the numbers differ by design
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {proj_storage}"), 1,
               "the projection storage holds exactly one row group after the truncate")
    expect.num(_one(conn, f"SELECT coalesce(sum(row_count), 0) FROM pgcolumnar.row_group "
                          f"WHERE storage_id = {proj_storage}"), 500,
               "and that group describes only the rows written after it")

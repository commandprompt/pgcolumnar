"""Lazy VACUUM retires a fully deleted row group, and a reader that runs while that
VACUUM is still open must not undo the retirement (#2263). The pytest twin of
`test/vacuum_retire_visibility.sh`, every check by the shell's name.

Core marks a lazy VACUUM with PROC_IN_VACUUM, and other backends then leave its xid
out of their snapshots. Before the fix, a reader that met a catalog row the
retirement had deleted, while the VACUUM was open, took the deleting xid for aborted
and set HEAP_XMAX_INVALID on the row, so that delete was lost when the VACUUM
committed. The row_group row survived without its column_chunk and delete_vector rows,
and the group's deleted row came back with every column NULL.

Part 1 makes the window by hand: a blocker connection holds an uncommitted free_space
row with the key the retirement inserts last, so the VACUUM waits there after its
catalog deletes, and a reader runs a statement it prepared (and planned) before the
VACUUM. Part 2 is the reader's guard: a row group with data and no column_chunk rows
is refused with XX001 on both read paths, a table with no stored column still reads, a
table that got a column after its data was written still reads by seq, index and
index-only scans (#2266, B2), and an aborted insert's index entry returns no row. Part 3: a VACUUM held in its
visibility-map step keeps PROC_IN_VACUUM, so it does not hold back another table's
retirement. Part 4 (#2266, B1): INSERT ... ON CONFLICT on a key another connection is
flushing, held by pgcolumnar.flush_hold_ms before the flush inserts its row_group row.
The upsert waits for the writer, as on main: DO NOTHING skips the row, DO UPDATE gets
main's 0A000.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading.
"""
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_vacuum_retire_visibility"

FETCH = ("SET enable_seqscan = off", "SET enable_bitmapscan = off",
         "SET enable_indexonlyscan = off", "SET pgcolumnar.enable_custom_scan = off")
SCAN = ("SET enable_indexscan = off", "SET enable_bitmapscan = off",
        "SET enable_indexonlyscan = off")


def _rows_of(tbl):
    return ("SELECT coalesce(string_agg(coalesce(id::text, 'NULL') || ':' || coalesce(pad, 'NULL'), ',' "
            f"ORDER BY id NULLS FIRST), 'none') FROM {tbl}")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _text(v):
    return ("t" if v else "f") if isinstance(v, bool) else str(v)


def _value(dsn, sql, sets=()):
    """The first row as psql -At prints it, columns joined by "|"; every row's first
    column, one per line, for EXPLAIN; a failed reading when it fails."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if sql.lstrip().upper().startswith("EXPLAIN"):
        return "\n".join(str(row[0]) for row in rows)
    if not rows or rows[0][0] is None:
        return query_error(f"no value from {sql}")
    return "|".join(_text(v) for v in rows[0])


def _refusal(dsn, sql, sets=()):
    """-> (SQLSTATE, primary message) of the error `sql` raises, or ("no error", the
    rows it returned). The refusal checks compare both against the expected error."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate, exc.diag.message_primary
    return "no error", "|".join(_text(v) for row in rows for v in row)


def _err(pair):
    state, message = pair
    return f"ERROR {state} {message}"


def _first_scan(plan):
    for line in str(plan).splitlines():
        for node in ("Index Scan", "Seq Scan", "Custom Scan", "Bitmap Heap Scan"):
            if node in line:
                return node
    return f"no scan node in: {plan}"


def _crashes(cluster):
    """-> how many backends the server log says died, or a failed reading when the log
    can't be read: a blind count must not read as "no crashes"."""
    try:
        return str((cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal"))
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _delete_blockers(dsn, tbl):
    """Sessions whose xid or xmin is not newer than the newest delete in `tbl`."""
    return _value(dsn, f"""
        WITH f AS (SELECT min(age(xmin)) AS a FROM pgcolumnar.delete_vector
                    WHERE storage_id = pgcolumnar.get_storage_id('{tbl}') AND xmin::text::bigint >= 3)
        SELECT (SELECT count(*) FROM pg_stat_activity
                 WHERE pid <> pg_backend_pid()
                   AND (age(backend_xid) >= f.a
                        OR ((datname = current_database() OR datid IS NULL) AND age(backend_xmin) >= f.a)))
             + (SELECT count(*) FROM pg_prepared_xacts WHERE age(transaction) >= f.a)
             + (SELECT count(*) FROM pg_replication_slots WHERE age(xmin) >= f.a)
          FROM f""")


def _wait_delete_past_horizon(dsn, tbl):
    n = ""
    for _ in range(60):
        n = _delete_blockers(dsn, tbl)
        if n == "0":
            break
        time.sleep(0.5)
    return n


def _poll(dsn, sql, want, tries=200):
    for _ in range(tries):
        if _value(dsn, sql) == want:
            return "yes"
        time.sleep(0.1)
    return "no"


def _part1(dsn, r):
    _run(dsn, "CREATE TABLE r (id int, pad text) USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(dsn, "CREATE TABLE rh (id int, pad text) WITH (autovacuum_enabled = false)")
    _run(dsn, "INSERT INTO r VALUES (1, 'one')")
    _run(dsn, "INSERT INTO r VALUES (2, 'two')")
    _run(dsn, "INSERT INTO rh VALUES (1, 'one'), (2, 'two')")
    _run(dsn, "CREATE INDEX r_id ON r (id)")
    _run(dsn, "DELETE FROM r WHERE id = 1")
    _run(dsn, "DELETE FROM rh WHERE id = 1")
    sid = _value(dsn, "SELECT pgcolumnar.get_storage_id('r')")
    g1 = _value(dsn, f"SELECT group_number FROM pgcolumnar.row_group WHERE storage_id = {sid} "
                     "ORDER BY first_row_number LIMIT 1")
    g1off = _value(dsn, f"SELECT file_offset FROM pgcolumnar.row_group WHERE storage_id = {sid} "
                        f"AND group_number = {g1}")
    r["groups before"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid}")
    r["g1 deleted"] = _value(dsn, f"SELECT deleted_count FROM pgcolumnar.delete_vector "
                                  f"WHERE storage_id = {sid} AND group_number = {g1}")
    r["horizon"] = _wait_delete_past_horizon(dsn, "r")

    out = {}
    r["vacuum waits"] = r["reader"] = r["vacuum done"] = "the race did not run"
    try:
        with psycopg.connect(dsn, autocommit=True) as blocker, \
                psycopg.connect(dsn, autocommit=True) as reader, \
                psycopg.connect(dsn, autocommit=True) as vac:
            blocker.execute("BEGIN")
            blocker.execute(f"INSERT INTO pgcolumnar.free_space VALUES ({sid}, {g1off}, 8192, 1)")
            # The reader plans and runs once now, before the VACUUM.
            reader.execute("SET plan_cache_mode = force_generic_plan")
            reader.execute("SET statement_timeout = '60s'")
            reader.execute("PREPARE rd(int) AS SELECT 'rd:' || id FROM r WHERE id = $1")
            reader.execute("EXECUTE rd(2)").fetchall()
            vac.execute("SET lock_timeout = '60s'")
            vac.execute("SET statement_timeout = '90s'")

            def vacuum():
                try:
                    vac.execute("VACUUM r")
                    out["vacuum"] = "VACUUM"
                except psycopg.Error as exc:
                    out["vacuum"] = query_error(_message(exc))

            t = threading.Thread(target=vacuum)
            t.start()
            r["vacuum waits"] = _poll(dsn, "SELECT count(*) FROM pg_stat_activity "
                                           "WHERE query = 'VACUUM r' AND wait_event_type = 'Lock'", "1")
            # A transaction that commits now has an xid newer than the VACUUM's, so the
            # reader's snapshot that follows covers the VACUUM's xid.
            _value(dsn, "SELECT txid_current()")
            try:
                row = reader.execute("EXECUTE rd(2)").fetchone()
                r["reader"] = "no reply" if row is None else str(row[0])
            except psycopg.Error as exc:
                r["reader"] = query_error(_message(exc))
            blocker.execute("ROLLBACK")
            t.join(20)
            r["vacuum done"] = ("no" if t.is_alive() else "yes") + " " + out.get("vacuum", "no outcome")
    except psycopg.Error as exc:
        # a failure before the race ends shows in its premises, by name
        r["vacuum waits"] = query_error(_message(exc))
    r["free listed"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.free_space "
                                   f"WHERE storage_id = {sid} AND file_offset = {g1off}")
    r["g1 row_group"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.row_group "
                                    f"WHERE storage_id = {sid} AND group_number = {g1}")
    r["seq rows"] = (_value(dsn, _rows_of("r"), SCAN), _value(dsn, _rows_of("rh")))
    r["null rows"] = _value(dsn, "SELECT count(*) FROM r WHERE id IS NULL AND pad IS NULL", SCAN)
    r["idx deleted"] = _value(dsn, _rows_of("r") + " WHERE id = 1", FETCH)
    r["idx plan"] = _first_scan(_value(dsn, "EXPLAIN (COSTS OFF) " + _rows_of("r") + " WHERE id = 1", FETCH))


def _part2(dsn, r):
    _run(dsn, "CREATE TABLE gd (id int, pad text) USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(dsn, "INSERT INTO gd VALUES (1, 'one')")
    _run(dsn, "INSERT INTO gd VALUES (2, 'two')")
    _run(dsn, "CREATE INDEX gd_id ON gd (id)")
    _run(dsn, "DELETE FROM gd WHERE id = 1")
    sid = _value(dsn, "SELECT pgcolumnar.get_storage_id('gd')")
    g1 = _value(dsn, f"SELECT group_number FROM pgcolumnar.row_group WHERE storage_id = {sid} "
                     "ORDER BY first_row_number LIMIT 1")
    # What part 1 leaves without the fix: the group's row_group row and nothing else.
    _run(dsn, f"DELETE FROM pgcolumnar.column_chunk WHERE storage_id = {sid} AND group_number = {g1}")
    _run(dsn, f"DELETE FROM pgcolumnar.delete_vector WHERE storage_id = {sid} AND group_number = {g1}")
    r["damaged"] = _value(dsn, f"""SELECT g.byte_length > 0, (SELECT count(*) FROM pgcolumnar.column_chunk c
                                      WHERE c.storage_id = g.storage_id AND c.group_number = g.group_number)
                                     FROM pgcolumnar.row_group g WHERE g.storage_id = {sid} AND g.group_number = {g1}""")
    r["gmsg"] = f'ERROR XX001 columnar row group {g1} of relation "gd" has no column chunks'
    r["damaged seq"] = _err(_refusal(dsn, _rows_of("gd"), SCAN))
    r["damaged fetch"] = _err(_refusal(dsn, _rows_of("gd") + " WHERE id = 1", FETCH))
    r["other fetch"] = _value(dsn, _rows_of("gd") + " WHERE id = 2", FETCH)

    _run(dsn, "CREATE TABLE z () USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(dsn, "INSERT INTO z DEFAULT VALUES")
    _run(dsn, "INSERT INTO z DEFAULT VALUES")
    _run(dsn, "ALTER TABLE z ADD COLUMN a int")
    _run(dsn, "CREATE INDEX z_a ON z (a)")
    zsid = _value(dsn, "SELECT pgcolumnar.get_storage_id('z')")
    r["z shape"] = _value(dsn, f"SELECT count(*), sum(byte_length), (SELECT count(*) FROM pgcolumnar.column_chunk "
                               f"WHERE storage_id = {zsid}) FROM pgcolumnar.row_group WHERE storage_id = {zsid}")
    r["z seq"] = _value(dsn, "SELECT string_agg(coalesce(a::text, 'NULL'), ',') FROM z", SCAN)
    r["z fetch"] = _value(dsn, "SELECT string_agg(coalesce(a::text, 'NULL'), ',') FROM z WHERE a IS NULL", FETCH)

    # A table that got a column after its data was written (#2266, B2).
    _run(dsn, "CREATE TABLE ac (id int, v text) USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(dsn, "INSERT INTO ac SELECT g, 'v' || g FROM generate_series(1, 1000) g")
    # the index first: a build after the ADD COLUMN reads the table, and a reader
    # that refused it would leave no index for the reads below
    _run(dsn, "CREATE INDEX ac_id ON ac (id)")
    _run(dsn, "ALTER TABLE ac ADD COLUMN w int DEFAULT 5")
    acsid = _value(dsn, "SELECT pgcolumnar.get_storage_id('ac')")
    r["ac chunks"] = _value(dsn, f"SELECT count(DISTINCT column_index) FROM pgcolumnar.column_chunk "
                                 f"WHERE storage_id = {acsid}")
    r["ac seq"] = _value(dsn, "SELECT count(*), sum(w), max(v) FROM ac", SCAN)
    r["ac fetch"] = _value(dsn, "SELECT id, v, w FROM ac WHERE id = 7", FETCH)
    ios = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off")
    plan = _value(dsn, "EXPLAIN (COSTS OFF) SELECT id FROM ac WHERE id = 7", ios)
    r["ac ios plan"] = next((n for n in ("Index Only Scan", "Index Scan", "Seq Scan", "Custom Scan", "Bitmap Heap Scan")
                             if n in str(plan).splitlines()[0]), f"no scan node in: {plan}") if str(plan) else plan
    r["ac ios"] = _value(dsn, "SELECT id FROM ac WHERE id = 7", ios)

    # An index entry whose row no row group covers: an aborted insert's.
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO gd VALUES (3, 'three')")
        conn.rollback()
    r["aborted fetch"] = _value(dsn, _rows_of("gd") + " WHERE id = 3", FETCH)


def _part3(dsn, r):
    _run(dsn, "CREATE TABLE va (id int) USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(dsn, "INSERT INTO va SELECT g FROM generate_series(1, 1000) g")
    _run(dsn, "CREATE TABLE vb (id int, pad text) USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(dsn, "INSERT INTO vb VALUES (1, 'one')")
    _run(dsn, "INSERT INTO vb VALUES (2, 'two')")
    vbsid = _value(dsn, "SELECT pgcolumnar.get_storage_id('vb')")
    held = "SELECT count(*) FROM pg_stat_activity WHERE query = 'VACUUM va' AND wait_event_type = 'Extension'"
    with psycopg.connect(dsn, autocommit=True) as vac:
        vac.execute("SET pgcolumnar.vm_set_hold_ms = 30000")
        vac.execute("SET statement_timeout = '90s'")
        pid = vac.info.backend_pid

        def vacuum():
            try:
                vac.execute("VACUUM va")
            except psycopg.Error:
                pass        # cancelled below, once the reading is taken

        t = threading.Thread(target=vacuum)
        t.start()
        r["va held"] = _poll(dsn, held, "1")
        # The delete commits after the held VACUUM took its snapshot.
        _run(dsn, "DELETE FROM vb WHERE id = 1")
        r["vb retired"] = "0"
        for _ in range(20):
            _run(dsn, "VACUUM vb")
            if _value(dsn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {vbsid}") == "1":
                r["vb retired"] = "1"
                break
            time.sleep(0.5)
        r["va still"] = "yes" if _value(dsn, held) == "1" else "no"
        _value(dsn, f"SELECT pg_cancel_backend({pid})")
        t.join(30)


def _upsert_race(dsn, r, tag, key, stmt):
    """A writer inserts (key, 'a'), held by pgcolumnar.flush_hold_ms before its
    row_group row; `stmt` runs on a second connection meanwhile. Records whether the
    writer was held, whether the upsert waited on a lock while it was, both outcomes,
    and the row stored for the key."""
    writer_sql = f"INSERT INTO u VALUES ({key}, 'a')"
    held = ("SELECT count(*) FROM pg_stat_activity WHERE query = "
            f"'{writer_sql.replace(chr(39), chr(39) * 2)}' AND wait_event_type = 'Extension'")
    out = {}
    r[f"{tag} held"] = r[f"{tag} waited"] = r[f"{tag} still"] = "the race did not run"
    try:
        with psycopg.connect(dsn, autocommit=True) as w, psycopg.connect(dsn, autocommit=True) as u:
            w.execute("SET pgcolumnar.flush_hold_ms = 15000")
            w.execute("SET statement_timeout = '60s'")
            u.execute("SET statement_timeout = '60s'")
            upid = u.info.backend_pid

            def run(key2, conn, sql):
                try:
                    cur = conn.execute(sql)
                    out[key2] = f"INSERT 0 {cur.rowcount}"
                except psycopg.Error as exc:
                    out[key2] = f"ERROR:  {exc.sqlstate}"

            tw = threading.Thread(target=run, args=("writer", w, writer_sql))
            tw.start()
            r[f"{tag} held"] = _poll(dsn, held, "1")
            tu = threading.Thread(target=run, args=("upsert", u, stmt))
            tu.start()
            r[f"{tag} waited"] = "never waited"
            for _ in range(100):
                if not tu.is_alive():
                    break
                if _value(dsn, f"SELECT wait_event_type FROM pg_stat_activity WHERE pid = {upid}") == "Lock":
                    r[f"{tag} waited"] = "Lock"
                    break
                time.sleep(0.1)
            r[f"{tag} still"] = "yes" if _value(dsn, held) == "1" else "no"
            tw.join(30)
            tu.join(30)
    except psycopg.Error as exc:
        r[f"{tag} held"] = query_error(_message(exc))
    r[f"{tag} out"] = out.get("upsert", "the upsert did not finish")
    r[f"{tag} writer"] = out.get("writer", "the writer did not finish")
    r[f"{tag} rows"] = _value(dsn, f"SELECT string_agg(id || ':' || pad, ',' ORDER BY id) FROM u WHERE id = {key}")


def _part4(dsn, r):
    _run(dsn, "CREATE TABLE u (id int, pad text) USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(dsn, "CREATE UNIQUE INDEX u_id ON u (id)")
    _run(dsn, "INSERT INTO u VALUES (0, 'z')")
    _upsert_race(dsn, r, "u1", 1, "INSERT INTO u VALUES (1, 'b') ON CONFLICT DO NOTHING")
    _upsert_race(dsn, r, "u2", 2, "INSERT INTO u VALUES (2, 'b') ON CONFLICT (id) DO UPDATE SET pad = 'c'")


@pytest.fixture(scope="module")
def vr(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {"crashes before": _crashes(pgc_cluster)}
        _part1(dsn, r)
        _part2(dsn, r)
        _part3(dsn, r)
        _part4(dsn, r)
        r["crashes after"] = _crashes(pgc_cluster)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_premise_two_row_groups(vr, expect):
    expect.text(vr["groups before"], "2", "premise: two row groups before the vacuum")


def test_premise_first_group_fully_deleted(vr, expect):
    expect.text(vr["g1 deleted"], "1", "premise: the first row group is fully deleted")


def test_premise_horizon(vr, expect):
    expect.text(vr["horizon"], "0", "premise: no transaction is older than the delete")


def test_premise_vacuum_waits(vr, expect):
    expect.text(vr["vacuum waits"], "yes",
                "premise: the vacuum waits on the blocker after deleting the group's catalog rows")


def test_premise_reader_ran(vr, expect):
    expect.text(vr["reader"], "rd:2", "premise: the reader ran while the vacuum was open")


def test_premise_vacuum_finished(vr, expect):
    expect.text(vr["vacuum done"], "yes VACUUM", "premise: the vacuum finished")


def test_premise_group_retired(vr, expect):
    expect.text(vr["free listed"], "1", "premise: the vacuum retired the group (its range is in the free list)")


def test_row_group_row_gone(vr, expect):
    expect.text(vr["g1 row_group"], "0", "the retired group's row_group row is gone")


def test_seq_scan_matches_heap(vr, expect):
    got, want = vr["seq rows"]
    expect.text(got, want, "a sequential scan returns the heap's rows")


def test_no_all_null_row(vr, expect):
    expect.text(vr["null rows"], "0", "no row reads with every column NULL")


def test_index_scan_deleted_id(vr, expect):
    expect.text(vr["idx deleted"], "none", "an index scan for the deleted id returns no row")


def test_premise_index_scan(vr, expect):
    expect.text(vr["idx plan"], "Index Scan", "premise: that query is an index scan")


def test_premise_damaged_group(vr, expect):
    expect.text(vr["damaged"], "t|0", "premise: the damaged group has data and no column chunks")


def test_damaged_seq_refused(vr, expect):
    expect.text(vr["damaged seq"], vr["gmsg"],
                "a sequential scan of a row group with no column chunks is refused (XX001)")


def test_damaged_fetch_refused(vr, expect):
    expect.text(vr["damaged fetch"], vr["gmsg"], "an index fetch of a row in that group is refused (XX001)")


def test_other_group_fetch(vr, expect):
    expect.text(vr["other fetch"], "2:two", "an index fetch of a row in another group still returns it")


def test_premise_no_column_shape(vr, expect):
    expect.text(vr["z shape"], "2|0|0", "premise: the no-column table's row groups have no chunks and no bytes")


def test_no_column_seq(vr, expect):
    expect.text(vr["z seq"], "NULL,NULL", "a sequential scan of a table with no stored column reads its rows")


def test_no_column_fetch(vr, expect):
    expect.text(vr["z fetch"], "NULL,NULL", "an index fetch from a table with no stored column reads its rows")


def test_aborted_index_entry(vr, expect):
    expect.text(vr["aborted fetch"], "none", "an index entry no row group covers returns no row")


def test_premise_va_held(vr, expect):
    expect.text(vr["va held"], "yes", "premise: the vacuum of va is held in its visibility-map step")


def test_vb_retired_while_va_held(vr, expect):
    expect.text(vr["vb retired"], "1", "a vacuum of vb retires its dead group while va's vacuum is held")


def test_premise_va_still_held(vr, expect):
    expect.text(vr["va still"], "yes", "premise: va's vacuum was still held when vb's group was retired")


def test_no_crash(vr, expect):
    expect.text(vr["crashes after"], vr["crashes before"], "no backend terminated by signal")


def test_premise_added_column_chunks(vr, expect):
    expect.text(vr["ac chunks"], "2", "premise: the table with an added column has fewer chunks than columns")


def test_added_column_seq(vr, expect):
    expect.text(vr["ac seq"], "1000|5000|v999", "a sequential scan of a table with an added column reads its rows")


def test_added_column_fetch(vr, expect):
    expect.text(vr["ac fetch"], "7|v7|5", "an index fetch from a table with an added column reads its row")


def test_premise_added_column_ios_plan(vr, expect):
    expect.text(vr["ac ios plan"], "Index Only Scan", "premise: that read is an index-only scan")


def test_added_column_ios(vr, expect):
    expect.text(vr["ac ios"], "7", "an index-only scan of a table with an added column reads its row")


def test_premise_upsert_nothing_writer_held(vr, expect):
    expect.text(vr["u1 held"], "yes", "premise: the writer of id 1 is held before its row_group row")


def test_upsert_nothing_waits(vr, expect):
    expect.text(vr["u1 waited"], "Lock", "an upsert DO NOTHING during the flush waits for the writer")


def test_premise_upsert_nothing_still_held(vr, expect):
    expect.text(vr["u1 still"], "yes", "premise: the writer of id 1 was still held while the upsert waited")


def test_upsert_nothing_skips(vr, expect):
    expect.text(vr["u1 out"], "INSERT 0 0", "the upsert DO NOTHING then skips the row, as on main")


def test_premise_upsert_nothing_writer_committed(vr, expect):
    expect.text(vr["u1 writer"], "INSERT 0 1", "premise: the writer of id 1 committed its row")


def test_upsert_nothing_row(vr, expect):
    expect.text(vr["u1 rows"], "1:a", "id 1 holds the writer's row")


def test_premise_upsert_update_writer_held(vr, expect):
    expect.text(vr["u2 held"], "yes", "premise: the writer of id 2 is held before its row_group row")


def test_upsert_update_waits(vr, expect):
    expect.text(vr["u2 waited"], "Lock", "an upsert DO UPDATE during the flush waits for the writer")


def test_premise_upsert_update_still_held(vr, expect):
    expect.text(vr["u2 still"], "yes", "premise: the writer of id 2 was still held while the upsert waited")


def test_upsert_update_0a000(vr, expect):
    expect.text(vr["u2 out"], "ERROR:  0A000", "the upsert DO UPDATE then fails with 0A000, as on main")


def test_premise_upsert_update_writer_committed(vr, expect):
    expect.text(vr["u2 writer"], "INSERT 0 1", "premise: the writer of id 2 committed its row")


def test_upsert_update_row(vr, expect):
    expect.text(vr["u2 rows"], "2:a", "id 2 holds the writer's row")

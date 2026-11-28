"""A row group lazy VACUUM retires stays retired when another session reads it meanwhile (#2264).
The pytest twin of `test/vacuum_retire_snapshot.sh`, every check by the shell's name.

Core sets PROC_IN_VACUUM for a lazy VACUUM, and GetSnapshotData leaves such a backend's
XID out of every other snapshot. pgColumnar's lazy VACUUM writes the catalog: it deletes
a retired group's row_group and delete_vector rows and inserts its free_space row. A
session whose snapshot was taken after that XID was assigned, and after a later XID
committed, found the deleting XID neither in its snapshot nor committed, took it for
aborted, and set HEAP_XMAX_INVALID on the row_group row. The hint is permanent. After the
VACUUM committed, the group was live again while its range was also in free_space, and
the next VACUUM that retired it failed with 23505 on free_space_pkey. When the reader
read only row_group, the group's deleted rows came back.

The race is ordered by locks, not by sleeps. Three single-row inserts make three row
groups and the first two are deleted. Connection L locks the second group's row_group row
FOR UPDATE and then waits on advisory lock 2264, which a holder connection takes first.
VACUUM retires the first group and then waits on L's row lock. A transaction commits
(txid_current), so the VACUUM's XID is below the next snapshot's xmax, and the reader
reads the first group's rows. The holder releases, L commits, and VACUUM finishes.

Variant "dv" reads the first group's row_group and delete_vector rows; variant "rg" reads
only row_group. Every wait is bounded: L and both VACUUMs run with statement_timeout, the
polls give up after 20 s and the threads are joined with a timeout.

ON A PRIVATE DATABASE, built once by a module fixture; each test asserts one reading.
"""
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_vacuum_retire_snapshot"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql)


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


def _poll(dsn, sql):
    for _ in range(200):
        if _value(dsn, sql) == "1":
            return "yes"
        time.sleep(0.1)
    return "no"


def _crashes(pgc_cluster):
    return (pgc_cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")


def _vacuum(dsn, tbl):
    """VACUUM tbl on its own connection; its command tag, or SQLSTATE: message."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SET statement_timeout = '60s'")
            cur = c.execute(f"VACUUM {tbl}")
            return cur.statusmessage
    except psycopg.Error as exc:
        return f"ERROR:  {exc.sqlstate}"


def _rows(dsn, tbl):
    """tbl's rows as id:pad, or ERROR:  SQLSTATE when the scan refuses (XX001 for a
    group with no column chunks, since #2263), as the shell prints it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute("SELECT string_agg(coalesce(id::text, 'NULL') || ':' || "
                            f"coalesce(pad, 'NULL'), ',' ORDER BY id) FROM {tbl}").fetchone()
    except psycopg.Error as exc:
        return f"ERROR:  {exc.sqlstate}"
    return "" if row is None or row[0] is None else str(row[0])


def _race(dsn, r, tag, tbl, read):
    """Retire two groups of tbl while `read` (with {s} and {g}) reads the first."""
    for sql in (f"CREATE TABLE {tbl} (id int, pad text) USING pgcolumnar",
                f"INSERT INTO {tbl} VALUES (1, 'p1')",
                f"INSERT INTO {tbl} VALUES (2, 'p2')",
                f"INSERT INTO {tbl} VALUES (3, 'p3')",
                f"DELETE FROM {tbl} WHERE id IN (1, 2)"):
        _run(dsn, sql)
    s = _value(dsn, f"SELECT pgcolumnar.get_storage_id('{tbl}')")
    r[f"{tag} groups"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {s}")
    g1 = _value(dsn, f"SELECT min(group_number) FROM pgcolumnar.row_group WHERE storage_id = {s}")
    g2 = _value(dsn, f"SELECT min(group_number) FROM pgcolumnar.row_group "
                     f"WHERE storage_id = {s} AND group_number > {g1}")
    r[f"{tag} free want"] = _value(
        dsn, "SELECT sum((byte_length + p - 1) / p * p) FROM pgcolumnar.row_group, "
             "(SELECT current_setting('block_size')::int - 24 AS p) z "
             f"WHERE storage_id = {s} AND group_number IN ({g1}, {g2})")
    vacq = f"VACUUM {tbl}"
    waiting = (f"SELECT count(*) FROM pg_stat_activity WHERE query = '{vacq}' "
               "AND wait_event_type = 'Lock'")
    out = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as holder, \
                psycopg.connect(dsn, autocommit=True) as locker:
            holder.execute("SELECT pg_advisory_lock(2264)")

            def lock():
                try:
                    locker.execute("SET statement_timeout = '60s'")
                    with locker.transaction():
                        locker.execute(f"SELECT 'L' FROM pgcolumnar.row_group WHERE storage_id = {s} "
                                       f"AND group_number = {g2} FOR UPDATE")
                        locker.execute("SELECT pg_advisory_lock_shared(2264)")
                except psycopg.Error as exc:
                    out["locker"] = query_error(_message(exc))

            def vacuum():
                out["vacuum"] = _vacuum(dsn, tbl)

            tl = threading.Thread(target=lock)
            tl.start()
            r[f"{tag} locked"] = _poll(dsn, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                                            "AND objid = 2264 AND NOT granted")
            tv = threading.Thread(target=vacuum)
            tv.start()
            r[f"{tag} paused"] = _poll(dsn, waiting)
            vxid = _value(dsn, f"SELECT backend_xid FROM pg_stat_activity WHERE query = '{vacq}'")
            cx = _value(dsn, "SELECT txid_current()")
            r[f"{tag} xid"] = "yes" if vxid.isdigit() and cx.isdigit() and int(vxid) < int(cx) else "no"
            r[f"{tag} seen"] = _value(dsn, read.format(s=s, g=g1))
            r[f"{tag} still"] = _value(dsn, waiting)
            holder.execute("SELECT pg_advisory_unlock(2264)")
            tv.join(90)
            tl.join(90)
    except psycopg.Error as exc:
        r.setdefault(f"{tag} paused", query_error(_message(exc)))
    r[f"{tag} vacuum"] = out.get("locker", out.get("vacuum", "VACUUM did not finish"))
    r[f"{tag} retired"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.row_group "
                                      f"WHERE storage_id = {s} AND group_number = {g1}")
    r[f"{tag} overlap"] = _value(
        dsn, "SELECT count(*) FROM pgcolumnar.free_space f JOIN pgcolumnar.row_group r "
             "ON r.storage_id = f.storage_id AND r.file_offset < f.file_offset + f.byte_length "
             "AND f.file_offset < r.file_offset + greatest(r.byte_length, 1) "
             f"WHERE f.storage_id = {s}")
    r[f"{tag} free"] = _value(dsn, "SELECT coalesce(sum(byte_length), 0) FROM pgcolumnar.free_space "
                                   f"WHERE storage_id = {s}")
    r[f"{tag} rows"] = _rows(dsn, tbl)
    r[f"{tag} again"] = _vacuum(dsn, tbl)


@pytest.fixture(scope="module")
def rs(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        before = _crashes(pgc_cluster)
        _race(dsn, r, "dv", "rv_dv",
              "SELECT (SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {s} AND group_number = {g})"
              " || '|' || (SELECT count(*) FROM pgcolumnar.delete_vector WHERE storage_id = {s} "
              "AND group_number = {g})")
        _race(dsn, r, "rg", "rv_rg",
              "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {s} AND group_number = {g}")
        r["crashes"] = _crashes(pgc_cluster) - before
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_dv_premise_three_groups(rs, expect):
    expect.text(rs["dv groups"], "3", "premise: dv -- three single-row inserts made three row groups")


def test_dv_premise_locker_waits(rs, expect):
    expect.text(rs["dv locked"], "yes", "premise: dv -- the row lock holder is waiting on the advisory lock")


def test_dv_premise_vacuum_paused(rs, expect):
    expect.text(rs["dv paused"], "yes", "premise: dv -- VACUUM is paused on the row lock inside its retirement")


def test_dv_premise_xid_below_committed(rs, expect):
    expect.text(rs["dv xid"], "yes", "premise: dv -- the paused VACUUM's XID is below a committed XID")


def test_dv_premise_reader_saw_group(rs, expect):
    expect.text(rs["dv seen"], "1|1", "premise: dv -- the reader saw the group VACUUM was retiring")


def test_dv_premise_still_paused(rs, expect):
    expect.text(rs["dv still"], "1", "premise: dv -- VACUUM was still paused after the read")


def test_dv_premise_vacuum_finished(rs, expect):
    expect.text(rs["dv vacuum"], "VACUUM", "premise: dv -- the paused VACUUM finished")


def test_dv_group_stays_retired(rs, expect):
    expect.text(rs["dv retired"], "0", "dv: the retired group stays retired after a concurrent read (#2264)")


def test_dv_no_free_live_overlap(rs, expect):
    expect.text(rs["dv overlap"], "0", "dv: no free_space range overlaps a live row group (#2264)")


def test_dv_free_space_sum(rs, expect):
    expect.text(rs["dv free"], rs["dv free want"],
                "dv: free_space holds exactly the two retired groups' pages (#2264)")


def test_dv_next_vacuum_no_23505(rs, expect):
    expect.text(rs["dv again"], "VACUUM", "dv: the next VACUUM does not fail on free_space_pkey (#2264)")


def test_dv_table_rows(rs, expect):
    expect.text(rs["dv rows"], "3:p3", "dv: the table returns only the live row")


def test_rg_premise_three_groups(rs, expect):
    expect.text(rs["rg groups"], "3", "premise: rg -- three single-row inserts made three row groups")


def test_rg_premise_locker_waits(rs, expect):
    expect.text(rs["rg locked"], "yes", "premise: rg -- the row lock holder is waiting on the advisory lock")


def test_rg_premise_vacuum_paused(rs, expect):
    expect.text(rs["rg paused"], "yes", "premise: rg -- VACUUM is paused on the row lock inside its retirement")


def test_rg_premise_xid_below_committed(rs, expect):
    expect.text(rs["rg xid"], "yes", "premise: rg -- the paused VACUUM's XID is below a committed XID")


def test_rg_premise_reader_saw_group(rs, expect):
    expect.text(rs["rg seen"], "1", "premise: rg -- the reader saw the group VACUUM was retiring")


def test_rg_premise_still_paused(rs, expect):
    expect.text(rs["rg still"], "1", "premise: rg -- VACUUM was still paused after the read")


def test_rg_premise_vacuum_finished(rs, expect):
    expect.text(rs["rg vacuum"], "VACUUM", "premise: rg -- the paused VACUUM finished")


def test_rg_group_stays_retired(rs, expect):
    expect.text(rs["rg retired"], "0", "rg: the retired group stays retired after a concurrent read (#2264)")


def test_rg_no_free_live_overlap(rs, expect):
    expect.text(rs["rg overlap"], "0", "rg: no free_space range overlaps a live row group (#2264)")


def test_rg_free_space_sum(rs, expect):
    expect.text(rs["rg free"], rs["rg free want"],
                "rg: free_space holds exactly the two retired groups' pages (#2264)")


def test_rg_table_rows(rs, expect):
    expect.text(rs["rg rows"], "3:p3", "rg: the table returns only the live row (#2264)")


def test_rg_next_vacuum_succeeds(rs, expect):
    expect.text(rs["rg again"], "VACUUM", "rg: the next VACUUM succeeds")


def test_no_backend_crashed(rs, expect):
    expect.num(rs["crashes"], 0, "no backend terminated by signal during the races")

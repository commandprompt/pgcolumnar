"""Index-only scans on a columnar table, served from the visibility-map fork (gap 28).
The pytest twin of `test/index_only.sh`, every check by the shell's name (#1341).

pgColumnar's VM writer sets a bit for a synthetic block that the backend's own
visibilitymap_get_status reads back. Lazy VACUUM marks all-visible chunk groups and a
delete clears its block; VM state never changes what a read returns. With
pgcolumnar.enable_index_only_scan off no index-only scan is planned; on, one is, and
all-visible blocks are served with zero heap fetches while a deleted range falls back
to the fetch. And an old REPEATABLE READ snapshot never sees, through an index-only
scan, rows committed after it, even after a VACUUM.
A VACUUM that runs while a DELETE or UPDATE is still inside its statement does not
leave the changed block all-visible after the writer commits (#2059), nor does one
whose set comes after a DELETE that committed during its pause
(pgcolumnar.vm_set_hold_ms).

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). The planner settings are set on that database, as the
shell sets them, so every fresh connection inherits them. Session A is a second
connection holding its snapshot open while the writer commits and vacuums.
"""
import re
import threading
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_index_only"
ANALYZE = "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


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
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else _text(row[0])


def _plan(dsn, sql, sets=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute(sql))
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _fetches(plan):
    m = None if _failed(plan) else re.search(r"Heap Fetches: (\d+)", plan)
    return m.group(1) if m else ""


def _vm_race(dsn, r, tag, tbl, dml):
    """#2059: VACUUM while `dml` is paused inside its statement, then commit it.

    The writer keeps its delete marks in memory until the statement ends. Its CTE
    waits on advisory lock 2059, which a holder connection takes first, so the
    VACUUM runs after every row is marked and before the marks reach the catalog.
    Rows 300..400 lie in synthetic block 1 (rows 291..581), inside row group 0.
    """
    vis = f"SELECT pgcolumnar.vm_is_visible('{tbl}', 1)"
    _run(dsn, f"CREATE TABLE {tbl} (id int, pad int) USING pgcolumnar; "
              f"INSERT INTO {tbl} SELECT g, g FROM generate_series(1, 20000) g; "
              f"CREATE INDEX {tbl}_id ON {tbl} (id);")
    _run(dsn, f"VACUUM {tbl}")
    r[f"race {tag} before"] = _value(dsn, vis)
    out = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as holder, \
                psycopg.connect(dsn, autocommit=True) as writer:
            holder.execute("SELECT pg_advisory_lock(2059)")

            def write():
                try:
                    writer.execute(f"WITH d AS ({dml} RETURNING 1) "
                                   "SELECT count(*), pg_advisory_lock_shared(2059) FROM d")
                except psycopg.Error as exc:
                    out["writer"] = query_error(_message(exc))

            t = threading.Thread(target=write)
            t.start()
            waiting = "no"
            for _ in range(200):
                if _value(dsn, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                               "AND objid = 2059 AND NOT granted") == "1":
                    waiting = "yes"
                    break
                time.sleep(0.1)
            _run(dsn, f"VACUUM {tbl}")
            r[f"race {tag} paused"] = waiting
            holder.execute("SELECT pg_advisory_unlock(2059)")
            t.join(60)
    except psycopg.Error as exc:
        r[f"race {tag} paused"] = query_error(_message(exc))
    if "writer" in out:
        r[f"race {tag} paused"] = out["writer"]
    r[f"race {tag} after"] = _value(dsn, vis)
    rng = f"SELECT count(*) FROM {tbl} WHERE id BETWEEN 300 AND 400"
    plan = _plan(dsn, "EXPLAIN (COSTS OFF) " + rng)
    r[f"race {tag} ios"] = (_value(dsn, rng) if not _failed(plan) and "Index Only Scan" in plan
                            else f"no index-only scan was planned: {plan}")


def _vm_mid(dsn, r):
    """#2059: a DELETE commits between VACUUM's first computation and its set.

    pgcolumnar.vm_set_hold_ms pauses the VACUUM in that window. The writer's own
    clears then run before the set, so only VACUUM's second computation can clear
    the bit. Rows 300..400 lie in synthetic block 1.
    """
    vis = "SELECT pgcolumnar.vm_is_visible('vr_mid', 1)"
    held = ("SELECT count(*) FROM pg_stat_activity "
            "WHERE query = 'VACUUM vr_mid' AND wait_event_type = 'Extension'")
    _run(dsn, "CREATE TABLE vr_mid (id int, pad int) USING pgcolumnar; "
              "INSERT INTO vr_mid SELECT g, g FROM generate_series(1, 20000) g; "
              "CREATE INDEX vr_mid_id ON vr_mid (id);")
    _run(dsn, "VACUUM vr_mid")
    r["mid before"] = _value(dsn, vis)
    out = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as vac:
            vac.execute("SET pgcolumnar.vm_set_hold_ms = 5000")

            def vacuum():
                try:
                    vac.execute("VACUUM vr_mid")
                except psycopg.Error as exc:
                    out["vacuum"] = query_error(_message(exc))

            t = threading.Thread(target=vacuum)
            t.start()
            r["mid held"] = "no"
            for _ in range(100):
                if _value(dsn, held) == "1":
                    r["mid held"] = "yes"
                    break
                time.sleep(0.1)
            err = _run(dsn, "DELETE FROM vr_mid WHERE id BETWEEN 300 AND 400")
            r["mid still"] = query_error(err) if err else ("yes" if _value(dsn, held) == "1" else "no")
            t.join(60)
    except psycopg.Error as exc:
        r["mid held"] = query_error(_message(exc))
    if "vacuum" in out:
        r["mid held"] = out["vacuum"]
    r["mid after"] = _value(dsn, vis)
    rng = "SELECT count(*) FROM vr_mid WHERE id BETWEEN 300 AND 400"
    plan = _plan(dsn, "EXPLAIN (COSTS OFF) " + rng)
    r["mid ios"] = (_value(dsn, rng) if not _failed(plan) and "Index Only Scan" in plan
                    else f"no index-only scan was planned: {plan}")


@pytest.fixture(scope="module")
def io(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _run(dsn, "CREATE TABLE t_col (id int, v int) USING pgcolumnar; "
                  "INSERT INTO t_col SELECT g, g * 2 FROM generate_series(1, 50000) g;")
        r["rows"] = _value(dsn, "SELECT count(*) FROM t_col")
        for blk in (0, 3, 50, 171):
            r[f"vm {blk}"] = _value(dsn, f"SELECT pgcolumnar.vm_selftest('t_col', {blk})")
        r["first"] = _value(dsn, "SELECT pgcolumnar.vm_selftest('t_col', 7)")
        r["second"] = _value(dsn, "SELECT pgcolumnar.vm_selftest('t_col', 7)")

        vis = "SELECT pgcolumnar.vm_is_visible('iv', {})"
        _run(dsn, "CREATE TABLE iv (id int, v int) USING pgcolumnar; "
                  "INSERT INTO iv SELECT g, g * 2 FROM generate_series(1, 50000) g;")
        r["iv rows"] = _value(dsn, "SELECT count(*) FROM iv")
        r["before 50"] = _value(dsn, vis.format(50))
        _run(dsn, "VACUUM iv")
        for b in (10, 50, 120):
            r[f"after {b}"] = _value(dsn, vis.format(b))
        _run(dsn, "DELETE FROM iv WHERE id BETWEEN 10001 AND 20000")
        r["deleted 50"] = _value(dsn, vis.format(50))
        _run(dsn, "VACUUM iv")
        for b in (50, 10, 120):
            r[f"revac {b}"] = _value(dsn, vis.format(b))
        _run(dsn, "CREATE TABLE iv_heap (id int, v int) USING heap; INSERT INTO iv_heap SELECT g, g*2 "
                  "FROM generate_series(1,50000) g WHERE g NOT BETWEEN 10001 AND 20000;")
        r["iv hash"] = (_value(dsn, HASH.format("SELECT id, v FROM iv")),
                        _value(dsn, HASH.format("SELECT id, v FROM iv_heap")))

        _run(dsn, "CREATE TABLE ios (id int, v int) USING pgcolumnar; "
                  "INSERT INTO ios SELECT g, g*3 FROM generate_series(1,50000) g; CREATE INDEX ios_id ON ios (id); "
                  "CREATE TABLE ios_h (id int, v int) USING heap; "
                  "INSERT INTO ios_h SELECT g, g*3 FROM generate_series(1,50000) g; CREATE INDEX ios_h_id ON ios_h (id);")
        r["plan off"] = _plan(dsn, "EXPLAIN (COSTS OFF) SELECT id FROM ios WHERE id BETWEEN 100 AND 2000",
                              ("SET pgcolumnar.enable_index_only_scan = off", "SET enable_seqscan = off"))
        for k, v in (("pgcolumnar.enable_index_only_scan", "on"), ("pgcolumnar.enable_custom_scan", "off"),
                     ("enable_seqscan", "off"), ("enable_bitmapscan", "off")):
            _run(dsn, f"ALTER DATABASE {DB} SET {k} = {v}")
        _run(dsn, "VACUUM ios")
        rng = "SELECT id FROM ios{} WHERE id BETWEEN 1000 AND 5000"
        r["plan on"] = _plan(dsn, "EXPLAIN (COSTS OFF) " + rng.format(""))
        r["fetches av"] = _fetches(_plan(dsn, ANALYZE + rng.format("")))
        r["av hash"] = (_value(dsn, HASH.format(rng.format(""))), _value(dsn, HASH.format(rng.format("_h"))))
        _run(dsn, "DELETE FROM ios WHERE id BETWEEN 2000 AND 2200; DELETE FROM ios_h WHERE id BETWEEN 2000 AND 2200;")
        r["del hash"] = (_value(dsn, HASH.format(rng.format(""))), _value(dsn, HASH.format(rng.format("_h"))))
        r["fetches del"] = _fetches(_plan(dsn, ANALYZE + rng.format("")))

        _run(dsn, "CREATE TABLE mv (id int, v int) USING pgcolumnar; "
                  "INSERT INTO mv SELECT g, g*3 FROM generate_series(1,10000) g; CREATE INDEX mv_id ON mv (id);")
        _run(dsn, "VACUUM mv")
        count = "SELECT count(id) FROM mv WHERE id BETWEEN 1 AND 20000"
        try:
            with psycopg.connect(dsn) as a:
                a.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
                r["snap"] = str(a.execute(count).fetchone()[0])
                _run(dsn, "INSERT INTO mv SELECT g, g*3 FROM generate_series(10001,20000) g")
                _run(dsn, "VACUUM mv")
                r["see"] = str(a.execute(count).fetchone()[0])
                r["a plan"] = "\n".join(x[0] for x in a.execute(
                    "EXPLAIN (COSTS OFF) SELECT id FROM mv WHERE id BETWEEN 1 AND 20000"))
                a.commit()
                r["final"] = str(a.execute(count).fetchone()[0])
                a.commit()
        except psycopg.Error as exc:
            for k in ("snap", "see", "a plan", "final"):
                r.setdefault(k, query_error(_message(exc)))

        for tag, tbl, dml in (("del", "vr_del", "DELETE FROM vr_del WHERE id BETWEEN 300 AND 400"),
                              ("upd", "vr_upd", "UPDATE vr_upd SET id = id + 1000000 WHERE id BETWEEN 300 AND 400")):
            _vm_race(dsn, r, tag, tbl, dml)
        _vm_mid(dsn, r)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_rows_loaded(io, expect):
    expect.text(io["rows"], "50000", "rows loaded")


def test_vm_bit_round_trips_block_0(io, expect):
    expect.text(io["vm 0"], "t", "vm bit round-trips on columnar rel (block 0)")


def test_vm_bit_round_trips_block_3(io, expect):
    expect.text(io["vm 3"], "t", "vm bit round-trips on columnar rel (block 3)")


def test_vm_bit_round_trips_block_50(io, expect):
    expect.text(io["vm 50"], "t", "vm bit round-trips on columnar rel (block 50)")


def test_vm_bit_round_trips_block_171(io, expect):
    expect.text(io["vm 171"], "t", "vm bit round-trips on columnar rel (block 171)")


def test_first_set_on_fresh_block_succeeds(io, expect):
    expect.text(io["first"], "t", "first set on fresh block succeeds")


def test_second_set_on_same_block_sees_prior_bit(io, expect):
    expect.text(io["second"], "f", "second set on same block sees prior bit")


def test_iv_row_count(io, expect):
    expect.text(io["iv rows"], "50000", "iv row count")


def test_before_vacuum_block_50_not_all_visible(io, expect):
    expect.text(io["before 50"], "f", "before vacuum: block 50 not all-visible")


def test_after_vacuum_block_10_all_visible(io, expect):
    expect.text(io["after 10"], "t", "after vacuum: block 10 all-visible")


def test_after_vacuum_block_50_all_visible(io, expect):
    expect.text(io["after 50"], "t", "after vacuum: block 50 all-visible")


def test_after_vacuum_block_120_all_visible(io, expect):
    expect.text(io["after 120"], "t", "after vacuum: block 120 all-visible")


def test_after_delete_block_50_cleared_by_write(io, expect):
    expect.text(io["deleted 50"], "f", "after delete: block 50 cleared by write")


def test_after_delete_vacuum_deleted_block_50_not_all_visible(io, expect):
    expect.text(io["revac 50"], "f", "after delete+vacuum: deleted block 50 not all-visible")


def test_after_delete_vacuum_clean_block_10_still_all_visible(io, expect):
    expect.text(io["revac 10"], "t", "after delete+vacuum: clean block 10 still all-visible")


def test_after_delete_vacuum_clean_block_120_still_all_visible(io, expect):
    expect.text(io["revac 120"], "t", "after delete+vacuum: clean block 120 still all-visible")


def test_iv_contents_match_heap_oracle_after_vacuum_delete(io, expect):
    got, want = io["iv hash"]
    expect.text(got, want, "iv contents match heap oracle after vacuum/delete")


def test_guc_off_no_index_only_scan(io, expect):
    plan = io["plan off"]
    got = (str(plan.count("Index Only Scan")) if not _failed(plan) and " on ios" in plan
           else f"no plan over ios was read: {plan}")
    expect.text(got, "0", "GUC off: no index-only scan")


def test_guc_on_index_only_scan_chosen(io, expect):
    plan = io["plan on"]
    expect.text(plan if _failed(plan) else str(plan.count("Index Only Scan")), "1",
                "GUC on: index-only scan chosen")


def test_all_visible_interior_zero_heap_fetches(io, expect):
    expect.text(io["fetches av"], "0", "all-visible interior: zero heap fetches")


def test_index_only_results_match_heap_oracle_all_visible(io, expect):
    got, want = io["av hash"]
    expect.text(got, want, "index-only results match heap oracle (all-visible)")


def test_index_only_results_match_heap_oracle_after_delete(io, expect):
    got, want = io["del hash"]
    expect.text(got, want, "index-only results match heap oracle after delete (fallback)")


def test_after_delete_some_heap_fetches_occur(io, expect):
    f = io["fetches del"]
    expect.text("yes" if f.isdigit() and int(f) > 0 else "no", "yes", "after delete: some heap fetches occur")


def test_session_a_baseline_sees_batch_1_only(io, expect):
    # Where the shell's session A times out it records this name instead; here a
    # session that failed to open its snapshot is the same event.
    if _failed(io["snap"]):
        expect.text(io["snap"], "ok", "session A opened its snapshot")
        return
    expect.text(str(io["snap"]), "10000", "session A baseline sees batch 1 only")


def test_old_snapshot_ios_does_not_see_post_snapshot_rows(io, expect):
    if _failed(io["see"]):
        expect.text(io["see"], "ok", "session A responded to post-commit query")
        return
    expect.text(str(io["see"]), "10000", "old snapshot IOS does not see post-snapshot rows")


def test_session_a_used_an_index_only_scan(io, expect):
    plan = io["a plan"]
    expect.text(plan if _failed(plan) else str(plan.count("Index Only Scan")), "1",
                "session A used an index-only scan")


def test_new_snapshot_sees_both_batches(io, expect):
    expect.text(str(io["final"]), "20000", "new snapshot sees both batches")


def test_concurrent_vacuum_during_delete_block_1_all_visible_before(io, expect):
    expect.text(io["race del before"], "t",
                "concurrent vacuum during delete: block 1 all-visible before the writer starts")


def test_concurrent_vacuum_during_delete_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["race del paused"], "yes", "concurrent vacuum during delete: vacuum ran while the writer was paused")


def test_concurrent_vacuum_during_delete_block_1_not_all_visible_after_commit(io, expect):
    expect.text(io["race del after"], "f", "concurrent vacuum during delete: block 1 not all-visible after commit")


def test_concurrent_vacuum_during_delete_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["race del ios"], "0", "concurrent vacuum during delete: index-only scan returns no deleted row")


def test_concurrent_vacuum_during_update_block_1_all_visible_before(io, expect):
    expect.text(io["race upd before"], "t",
                "concurrent vacuum during update: block 1 all-visible before the writer starts")


def test_concurrent_vacuum_during_update_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["race upd paused"], "yes", "concurrent vacuum during update: vacuum ran while the writer was paused")


def test_concurrent_vacuum_during_update_block_1_not_all_visible_after_commit(io, expect):
    expect.text(io["race upd after"], "f", "concurrent vacuum during update: block 1 not all-visible after commit")


def test_concurrent_vacuum_during_update_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["race upd ios"], "0", "concurrent vacuum during update: index-only scan returns no deleted row")


def test_delete_between_vacuum_steps_block_1_all_visible_before(io, expect):
    expect.text(io["mid before"], "t",
                "delete between vacuum's two steps: block 1 all-visible before the writer starts")


def test_delete_between_vacuum_steps_vacuum_paused_before_its_set(io, expect):
    expect.text(io["mid held"], "yes", "delete between vacuum's two steps: vacuum paused before its set")


def test_delete_between_vacuum_steps_delete_committed_while_paused(io, expect):
    expect.text(io.get("mid still", "not reached"), "yes",
                "delete between vacuum's two steps: the delete committed while vacuum was paused")


def test_delete_between_vacuum_steps_block_1_not_all_visible_after(io, expect):
    expect.text(io["mid after"], "f", "delete between vacuum's two steps: block 1 not all-visible after vacuum")


def test_delete_between_vacuum_steps_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["mid ios"], "0", "delete between vacuum's two steps: index-only scan returns no deleted row")

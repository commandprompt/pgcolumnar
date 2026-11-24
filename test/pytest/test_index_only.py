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


def _ios(dsn, sql):
    plan = _plan(dsn, "EXPLAIN (COSTS OFF) " + sql)
    return (_value(dsn, sql) if not _failed(plan) and "Index Only Scan" in plan
            else f"no index-only scan was planned: {plan}")


# Synthetic block b holds row numbers b*291 .. b*291+290, and a single INSERT numbers
# its rows 1, 2, ... in order, so id g is in block g / 291. Rows 300..400 lie in block 1;
# rows 300..600 span blocks 1 and 2 (582..600 is block 2). The 400,000-row tables span
# three row groups; TGT is the 79 blocks that hold ids 5000, 10000, ..., 395000. Block
# 1374, the last, holds id 400000; it is partial and VACUUM never sets it.
TGT = "SELECT DISTINCT g / 291 AS b FROM generate_series(5000, 395000, 5000) g"


def _vis(tbl, blk):
    return f"SELECT pgcolumnar.vm_is_visible('{tbl}', {blk})"


def _tgt_all(tbl):
    return f"SELECT bool_and(pgcolumnar.vm_is_visible('{tbl}', b)) FROM ({TGT}) t"


def _tgt_stale(tbl):
    return f"SELECT count(*) FILTER (WHERE pgcolumnar.vm_is_visible('{tbl}', b)) FROM ({TGT}) t"


def _tgt_ios(tbl):
    return (f"SELECT count(*) FROM {tbl} "
            "WHERE id = ANY (ARRAY(SELECT generate_series(5000, 395000, 5000)))")


def _load(dsn, tbl, rows):
    _run(dsn, f"CREATE TABLE {tbl} (id int, pad int) USING pgcolumnar; "
              f"INSERT INTO {tbl} SELECT g, g FROM generate_series(1, {rows}) g; "
              f"CREATE INDEX {tbl}_id ON {tbl} (id);")
    _run(dsn, f"VACUUM {tbl}")


def _vm_race(dsn, r, tag, tbl, rows, dml, before, after, ios):
    """#2059: VACUUM while `dml` is paused inside its statement, then commit it.

    The writer keeps its delete marks in memory until the statement ends. Its CTE
    waits on advisory lock 2059, which a holder connection takes first, so the
    VACUUM runs after every row is marked and before the marks reach the catalog.
    `before` and `after` are the VM queries run before the writer starts and after
    it commits; `ios` is the count read through an index-only scan.
    """
    _load(dsn, tbl, rows)
    r[f"race {tag} before"] = _value(dsn, before)
    out = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as holder, \
                psycopg.connect(dsn, autocommit=True) as writer:
            holder.execute("SELECT pg_advisory_lock(2059)")

            def write():
                try:
                    out["written"] = str(writer.execute(
                        f"WITH d AS ({dml} RETURNING 1) "
                        "SELECT count(*), pg_advisory_lock_shared(2059) FROM d").fetchone()[0])
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
    r[f"race {tag} written"] = out.get("writer", out.get("written", "writer did not finish"))
    r[f"race {tag} after"] = _value(dsn, after)
    r[f"race {tag} ios"] = _ios(dsn, ios)


def _vm_mid(dsn, r, tag, tbl, rows, dml, before, after, ios, clean=None):
    """#2059: a DELETE commits between VACUUM's first computation and its set.

    pgcolumnar.vm_set_hold_ms pauses the VACUUM in that window. The writer's own
    clears then run before the set, so only VACUUM's second computation can clear
    the bits. `before`, `after`, `ios` and the optional `clean` are queries run
    before the hold and after the VACUUM.
    """
    held = ("SELECT count(*) FROM pg_stat_activity "
            f"WHERE query = 'VACUUM {tbl}' AND wait_event_type = 'Extension'")
    _load(dsn, tbl, rows)
    r[f"{tag} before"] = _value(dsn, before)
    out = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as vac:
            vac.execute("SET pgcolumnar.vm_set_hold_ms = 5000")

            def vacuum():
                try:
                    vac.execute(f"VACUUM {tbl}")
                except psycopg.Error as exc:
                    out["vacuum"] = query_error(_message(exc))

            t = threading.Thread(target=vacuum)
            t.start()
            r[f"{tag} held"] = "no"
            for _ in range(100):
                if _value(dsn, held) == "1":
                    r[f"{tag} held"] = "yes"
                    break
                time.sleep(0.1)
            err = _run(dsn, dml)
            r[f"{tag} still"] = query_error(err) if err else ("yes" if _value(dsn, held) == "1" else "no")
            t.join(60)
    except psycopg.Error as exc:
        r[f"{tag} held"] = query_error(_message(exc))
    if "vacuum" in out:
        r[f"{tag} held"] = out["vacuum"]
    r[f"{tag} after"] = _value(dsn, after)
    if clean is not None:
        r[f"{tag} clean"] = _value(dsn, clean)
    r[f"{tag} ios"] = _ios(dsn, ios)


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

        rng = "SELECT count(*) FROM {} WHERE id BETWEEN {} AND {}"
        _vm_race(dsn, r, "del", "vr_del", 20000, "DELETE FROM vr_del WHERE id BETWEEN 300 AND 400",
                 _vis("vr_del", 1), _vis("vr_del", 1), rng.format("vr_del", 300, 400))
        _vm_race(dsn, r, "upd", "vr_upd", 20000,
                 "UPDATE vr_upd SET id = id + 1000000 WHERE id BETWEEN 300 AND 400",
                 _vis("vr_upd", 1), _vis("vr_upd", 1), rng.format("vr_upd", 300, 400))
        _vm_race(dsn, r, "del2", "vr_del2", 20000, "DELETE FROM vr_del2 WHERE id BETWEEN 300 AND 600",
                 _vis("vr_del2", 2), _vis("vr_del2", 2), rng.format("vr_del2", 582, 600))
        _vm_race(dsn, r, "upd2", "vr_upd2", 20000,
                 "UPDATE vr_upd2 SET id = id + 1000000 WHERE id BETWEEN 300 AND 600",
                 _vis("vr_upd2", 2), _vis("vr_upd2", 2), rng.format("vr_upd2", 582, 600))
        _vm_race(dsn, r, "delm", "vr_delm", 400000, "DELETE FROM vr_delm WHERE id % 5000 = 0 AND id < 400000",
                 _tgt_all("vr_delm"), _tgt_stale("vr_delm"), _tgt_ios("vr_delm"))
        _vm_mid(dsn, r, "mid", "vr_mid", 20000, "DELETE FROM vr_mid WHERE id BETWEEN 300 AND 400",
                _vis("vr_mid", 1), _vis("vr_mid", 1), rng.format("vr_mid", 300, 400))
        # Row group 2 starts at row 150001, inside block 515 (rows 149865..150155).
        # Block 514 lies wholly in the clean first group and keeps its bit.
        _vm_mid(dsn, r, "mid2", "vr_mid2", 300000, "DELETE FROM vr_mid2 WHERE id BETWEEN 150001 AND 150100",
                _vis("vr_mid2", 515), _vis("vr_mid2", 515), rng.format("vr_mid2", 150001, 150100),
                _vis("vr_mid2", 514))
        _vm_mid(dsn, r, "midm", "vr_midm", 400000, "DELETE FROM vr_midm WHERE id % 5000 = 0 AND id < 400000",
                _tgt_all("vr_midm"), _tgt_stale("vr_midm"), _tgt_ios("vr_midm"))
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


def test_concurrent_vacuum_during_delete_writer_deleted_101_rows(io, expect):
    expect.text(io["race del written"], "101", "concurrent vacuum during delete: the writer deleted 101 rows")


def test_concurrent_vacuum_during_delete_block_1_not_all_visible_after_commit(io, expect):
    expect.text(io["race del after"], "f", "concurrent vacuum during delete: block 1 not all-visible after commit")


def test_concurrent_vacuum_during_delete_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["race del ios"], "0", "concurrent vacuum during delete: index-only scan returns no deleted row")


def test_concurrent_vacuum_during_update_block_1_all_visible_before(io, expect):
    expect.text(io["race upd before"], "t",
                "concurrent vacuum during update: block 1 all-visible before the writer starts")


def test_concurrent_vacuum_during_update_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["race upd paused"], "yes", "concurrent vacuum during update: vacuum ran while the writer was paused")


def test_concurrent_vacuum_during_update_writer_updated_101_rows(io, expect):
    expect.text(io["race upd written"], "101", "concurrent vacuum during update: the writer updated 101 rows")


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


def test_concurrent_vacuum_during_two_block_delete_block_2_all_visible_before(io, expect):
    expect.text(io["race del2 before"], "t",
                "concurrent vacuum during a two-block delete: block 2 all-visible before the writer starts")


def test_concurrent_vacuum_during_two_block_delete_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["race del2 paused"], "yes",
                "concurrent vacuum during a two-block delete: vacuum ran while the writer was paused")


def test_concurrent_vacuum_during_two_block_delete_writer_deleted_301_rows(io, expect):
    expect.text(io["race del2 written"], "301",
                "concurrent vacuum during a two-block delete: the writer deleted 301 rows")


def test_concurrent_vacuum_during_two_block_delete_block_2_not_all_visible_after_commit(io, expect):
    expect.text(io["race del2 after"], "f",
                "concurrent vacuum during a two-block delete: block 2 not all-visible after commit")


def test_concurrent_vacuum_during_two_block_delete_index_only_scan_returns_no_deleted_row_of_block_2(io, expect):
    expect.text(io["race del2 ios"], "0",
                "concurrent vacuum during a two-block delete: index-only scan returns no deleted row of block 2")


def test_concurrent_vacuum_during_two_block_update_block_2_all_visible_before(io, expect):
    expect.text(io["race upd2 before"], "t",
                "concurrent vacuum during a two-block update: block 2 all-visible before the writer starts")


def test_concurrent_vacuum_during_two_block_update_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["race upd2 paused"], "yes",
                "concurrent vacuum during a two-block update: vacuum ran while the writer was paused")


def test_concurrent_vacuum_during_two_block_update_writer_updated_301_rows(io, expect):
    expect.text(io["race upd2 written"], "301",
                "concurrent vacuum during a two-block update: the writer updated 301 rows")


def test_concurrent_vacuum_during_two_block_update_block_2_not_all_visible_after_commit(io, expect):
    expect.text(io["race upd2 after"], "f",
                "concurrent vacuum during a two-block update: block 2 not all-visible after commit")


def test_concurrent_vacuum_during_two_block_update_index_only_scan_returns_no_deleted_row_of_block_2(io, expect):
    expect.text(io["race upd2 ios"], "0",
                "concurrent vacuum during a two-block update: index-only scan returns no deleted row of block 2")


def test_concurrent_vacuum_during_delete_over_three_row_groups_every_target_block_all_visible_before(io, expect):
    expect.text(io["race delm before"], "t",
                "concurrent vacuum during a delete over three row groups: every target block all-visible before the writer starts")


def test_concurrent_vacuum_during_delete_over_three_row_groups_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["race delm paused"], "yes",
                "concurrent vacuum during a delete over three row groups: vacuum ran while the writer was paused")


def test_concurrent_vacuum_during_delete_over_three_row_groups_writer_deleted_79_rows(io, expect):
    expect.text(io["race delm written"], "79",
                "concurrent vacuum during a delete over three row groups: the writer deleted 79 rows")


def test_concurrent_vacuum_during_delete_over_three_row_groups_no_target_block_all_visible_after_commit(io, expect):
    expect.text(io["race delm after"], "0",
                "concurrent vacuum during a delete over three row groups: no target block all-visible after commit")


def test_concurrent_vacuum_during_delete_over_three_row_groups_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["race delm ios"], "0",
                "concurrent vacuum during a delete over three row groups: index-only scan returns no deleted row")


def test_delete_in_second_row_group_between_vacuum_steps_block_515_all_visible_before(io, expect):
    expect.text(io["mid2 before"], "t",
                "delete in the second row group between vacuum's two steps: block 515 all-visible before the writer starts")


def test_delete_in_second_row_group_between_vacuum_steps_vacuum_paused_before_its_set(io, expect):
    expect.text(io["mid2 held"], "yes",
                "delete in the second row group between vacuum's two steps: vacuum paused before its set")


def test_delete_in_second_row_group_between_vacuum_steps_delete_committed_while_paused(io, expect):
    expect.text(io.get("mid2 still", "not reached"), "yes",
                "delete in the second row group between vacuum's two steps: the delete committed while vacuum was paused")


def test_delete_in_second_row_group_between_vacuum_steps_block_515_not_all_visible_after(io, expect):
    expect.text(io["mid2 after"], "f",
                "delete in the second row group between vacuum's two steps: block 515 not all-visible after vacuum")


def test_delete_in_second_row_group_between_vacuum_steps_clean_block_514_still_all_visible(io, expect):
    expect.text(io["mid2 clean"], "t",
                "delete in the second row group between vacuum's two steps: clean block 514 still all-visible")


def test_delete_in_second_row_group_between_vacuum_steps_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["mid2 ios"], "0",
                "delete in the second row group between vacuum's two steps: index-only scan returns no deleted row")


def test_delete_over_three_row_groups_between_vacuum_steps_every_target_block_all_visible_before(io, expect):
    expect.text(io["midm before"], "t",
                "delete over three row groups between vacuum's two steps: every target block all-visible before the writer starts")


def test_delete_over_three_row_groups_between_vacuum_steps_vacuum_paused_before_its_set(io, expect):
    expect.text(io["midm held"], "yes",
                "delete over three row groups between vacuum's two steps: vacuum paused before its set")


def test_delete_over_three_row_groups_between_vacuum_steps_delete_committed_while_paused(io, expect):
    expect.text(io.get("midm still", "not reached"), "yes",
                "delete over three row groups between vacuum's two steps: the delete committed while vacuum was paused")


def test_delete_over_three_row_groups_between_vacuum_steps_no_target_block_all_visible_after(io, expect):
    expect.text(io["midm after"], "0",
                "delete over three row groups between vacuum's two steps: no target block all-visible after vacuum")


def test_delete_over_three_row_groups_between_vacuum_steps_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["midm ios"], "0",
                "delete over three row groups between vacuum's two steps: index-only scan returns no deleted row")

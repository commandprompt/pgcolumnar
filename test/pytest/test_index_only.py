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
(pgcolumnar.vm_set_hold_ms). That VACUUM clears no bit, so clean blocks an earlier
VACUUM set keep theirs (#2170). It judges each row group again under the group's
chunk-group lock before it sets the group's blocks, so even while it runs an
index-only scan counts no row whose delete committed, a second deleter of the group
waits for it, it does not wait for an open writer, and it holds at most the groups
covering one block (#2268, pgcolumnar.vm_set_release_hold_ms).
And an AFTER trigger of a DELETE or UPDATE, reading
through an index-only scan after such a VACUUM, counts what heap counts (#2169). A DELETE that fails after such a nested read leaves
every row live in the next transaction, and a second deleter of the same rows waits for
the first and then fails with serialization_failure, the documented columnar rule.

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


def _vm_mid(dsn, r, tag, tbl, rows, dml, before, after, ios, clean=None, setup=None):
    """#2059: a DELETE commits between VACUUM's first computation and its set.

    pgcolumnar.vm_set_hold_ms pauses the VACUUM in that window. The writer's own
    clears then run before the set, so VACUUM must see the delete when it judges
    the group again under its lock (#2268). `before`, `after`, `ios` and the optional `clean` are queries run
    before the hold and after the VACUUM; the optional `setup` runs after the
    load's VACUUM, before `before`.
    """
    held = ("SELECT count(*) FROM pg_stat_activity "
            f"WHERE query = 'VACUUM {tbl}' AND wait_event_type = 'Extension'")
    _load(dsn, tbl, rows)
    if setup is not None:
        _run(dsn, setup)
    r[f"{tag} before"] = _value(dsn, before)
    out = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as vac:
            vac.execute("SET pgcolumnar.vm_set_hold_ms = 15000")

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


# #2169: the trigger function of phase 8. It counts the target ids through an
# index-only scan of the table that fired it, joined to a heap list of the ids so
# the table is not the first entry of the query's range table, and logs the count
# and whether the plan holds an Index Only Scan. With the argument 'exc' it reads
# inside an EXCEPTION block that then fails, so the block's subtransaction, which
# does not own the writer's marks, rolls back.
TR_COUNT = """CREATE FUNCTION tr_count() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    sql text := format('SELECT count(*) FROM tr_ids k JOIN %I t ON t.id = k.id', TG_TABLE_NAME);
    n bigint;
    pl text;
    ios bool := false;
BEGIN
    IF TG_ARGV[0] = 'exc' THEN
        BEGIN
            EXECUTE sql INTO n;
            PERFORM 1 / 0;
        EXCEPTION WHEN division_by_zero THEN
            NULL;
        END;
    ELSE
        EXECUTE sql INTO n;
    END IF;
    FOR pl IN EXECUTE 'EXPLAIN (COSTS OFF) ' || sql LOOP
        ios := ios OR pl LIKE '%Index Only Scan%';
    END LOOP;
    INSERT INTO tr_log VALUES (TG_TABLE_NAME, n, ios);
    RETURN NULL;
END $$"""

# Every 500th id below 20000: 39 rows in 39 blocks of one row group. The CASE
# pauses on the last target, 19500, after the 38 earlier marks, on advisory lock
# 2169, which the holder connection takes first.
PAUSE_ON_LAST = ("CASE WHEN id % 500 <> 0 OR id >= 20000 THEN false "
                 "WHEN id = 19500 THEN pg_advisory_lock_shared(2169)::text = '' ELSE true END")
TR_TGT = "SELECT DISTINCT id / 291 AS b FROM tr_ids"


def _tr_race(dsn, r, tag, tbl, am, variant, verb):
    """#2169: VACUUM while a DELETE or UPDATE of `tbl` is paused on its last target.

    Its AFTER STATEMENT trigger then fires before the writer's ExecutorEnd writes
    the marks, and counts the targets through an index-only scan. Records, under
    `tag`: the VM bits of the target blocks before the writer and after the VACUUM,
    whether the writer was seen waiting, its command tag, the trigger's count and
    plan, and the count after commit.
    """
    _run(dsn, f"CREATE TABLE {tbl} (id int, pad int) USING {am}; "
              f"INSERT INTO {tbl} SELECT g, g FROM generate_series(1, 20000) g; "
              f"CREATE INDEX {tbl}_id ON {tbl} (id);")
    _run(dsn, f"VACUUM {tbl}")
    _run(dsn, f"CREATE TRIGGER {tbl}_tr AFTER DELETE OR UPDATE ON {tbl} "
              f"FOR EACH STATEMENT EXECUTE FUNCTION tr_count('{variant}')")
    r[f"{tag} before"] = _value(dsn, f"SELECT bool_and(pgcolumnar.vm_is_visible('{tbl}', b)) FROM ({TR_TGT}) s")
    dml = (f"DELETE FROM {tbl} WHERE {PAUSE_ON_LAST}" if verb == "DELETE"
           else f"UPDATE {tbl} SET pad = -pad WHERE {PAUSE_ON_LAST}")
    out = {}
    r[f"{tag} paused"] = "no"
    try:
        with psycopg.connect(dsn, autocommit=True) as holder, \
                psycopg.connect(dsn, autocommit=True) as writer:
            holder.execute("SELECT pg_advisory_lock(2169)")
            writer.execute("SET statement_timeout = '60s'")

            def write():
                try:
                    cur = writer.execute(dml)
                    out["written"] = f"{verb} {cur.rowcount}"
                except psycopg.Error as exc:
                    out["written"] = query_error(_message(exc))

            t = threading.Thread(target=write)
            t.start()
            for _ in range(200):
                if _value(dsn, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                               "AND objid = 2169 AND NOT granted") == "1":
                    r[f"{tag} paused"] = "yes"
                    break
                time.sleep(0.1)
            _run(dsn, f"VACUUM {tbl}")
            r[f"{tag} set"] = _value(
                dsn, f"SELECT count(*) FILTER (WHERE pgcolumnar.vm_is_visible('{tbl}', b)) FROM ({TR_TGT}) s")
            holder.execute("SELECT pg_advisory_unlock(2169)")
            t.join(90)
    except psycopg.Error as exc:
        r[f"{tag} paused"] = query_error(_message(exc))
    r[f"{tag} written"] = out.get("written", "writer did not finish")
    r[f"{tag} n"] = _value(dsn, f"SELECT n FROM tr_log WHERE tbl = '{tbl}'")
    r[f"{tag} ios"] = _value(dsn, f"SELECT ios FROM tr_log WHERE tbl = '{tbl}'")
    r[f"{tag} after"] = _value(dsn, f"SELECT count(*) FROM tr_ids k JOIN {tbl} t ON t.id = k.id")


# Phase 9 (#2169, review of #2241): a DELETE whose nested statement wrote its marks
# early, at that statement's start, then fails. AB_PEEK reads the table through an
# index-only scan on row 999 and raises a NOTICE; row 1000 divides by zero.
AB_PEEK = """CREATE FUNCTION ab_peek(t text, i int) RETURNS bool LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
    EXECUTE format('SELECT count(*) FROM %I WHERE id = $1', t) INTO n USING i;
    RAISE NOTICE 'peeked';
    RETURN true;
END $$"""
AB_SEQ = ("SET enable_seqscan = on", "SET enable_indexscan = off", "SET enable_indexonlyscan = off")
AB_IDX = ("SET enable_indexonlyscan = off",)


def _load_am(dsn, tbl, am):
    _run(dsn, f"CREATE TABLE {tbl} (id int, pad int) USING {am}; "
              f"INSERT INTO {tbl} SELECT g, g FROM generate_series(1, 20000) g; "
              f"CREATE INDEX {tbl}_id ON {tbl} (id);")
    _run(dsn, f"VACUUM {tbl}")


def _ab_counts(dsn, tbl):
    return "/".join((_value(dsn, f"SELECT count(*) FROM {tbl}", AB_SEQ),
                     _value(dsn, f"SELECT count(pad) FROM {tbl} WHERE id > 0", AB_IDX),
                     _value(dsn, f"SELECT count(*) FROM {tbl} WHERE id > 0")))


def _node(plan, pattern):
    m = None if _failed(plan) else re.search(pattern, plan)
    return m.group(0) if m else f"not in plan: {plan}"


def _ab_abort(dsn, r, tag, tbl, am):
    """Run the failing DELETE, then one more transaction on the writer's connection,
    which is where marks kept past the abort would be written. Record the writer's
    NOTICE and error SQLSTATEs, the count plans, and the seq/index/index-only counts
    from fresh connections before and after a VACUUM."""
    _load_am(dsn, tbl, am)
    seen = []
    try:
        with psycopg.connect(dsn, autocommit=True) as w:
            w.add_notice_handler(lambda d: seen.append(f"NOTICE:  {d.sqlstate}"))
            w.execute("SET statement_timeout = '60s'")
            try:
                w.execute(f"DELETE FROM {tbl} WHERE CASE WHEN id > 1000 THEN false "
                          f"WHEN id = 999 THEN ab_peek('{tbl}', id) "
                          "WHEN id = 1000 THEN id / (id - 1000) = 1 ELSE true END")
                seen.append("no error")
            except psycopg.Error as exc:
                seen.append(f"ERROR:  {exc.sqlstate}")
            seen.append(w.execute("SELECT 'W_END'").fetchone()[0])
    except psycopg.Error as exc:
        seen.append(query_error(_message(exc)))
    r[f"{tag} writer"] = "|".join(seen)
    r[f"{tag} plans"] = "|".join((
        _node(_plan(dsn, f"EXPLAIN (COSTS OFF) SELECT count(*) FROM {tbl}", AB_SEQ), r"Seq Scan"),
        _node(_plan(dsn, f"EXPLAIN (COSTS OFF) SELECT count(pad) FROM {tbl} WHERE id > 0", AB_IDX),
              r"Index (Only )?Scan"),
        _node(_plan(dsn, f"EXPLAIN (COSTS OFF) SELECT count(*) FROM {tbl} WHERE id > 0"), r"Index (Only )?Scan")))
    r[f"{tag} after"] = _ab_counts(dsn, tbl)
    _run(dsn, f"VACUUM {tbl}")
    r[f"{tag} vac"] = _ab_counts(dsn, tbl)


# Phase 10 (#2169, review of #2241): a second deleter of the same rows while the
# first pauses inside a nested read on row 999, on advisory lock 2241.
CD_GATE = """CREATE FUNCTION cd_gate(t text, i int) RETURNS bool LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
    EXECUTE format('SELECT count(*) FROM %I WHERE id = $1 AND pg_advisory_lock_shared(2241)::text = %L', t, '')
        INTO n USING i;
    RETURN true;
END $$"""


def _cd_race(dsn, r, tag, tbl, am):
    """A deletes ids up to 1000 and pauses in cd_gate; B, at READ COMMITTED, deletes
    500..1500 meanwhile. Record whether A paused, whether B waited on a lock, each
    one's outcome (command tag or SQLSTATE) and the rows left."""
    _load_am(dsn, tbl, am)
    out = {}
    r[f"{tag} paused"] = "no"
    r[f"{tag} wait"] = "B never waited"

    def run(key, conn, sql, verb):
        try:
            cur = conn.execute(sql)
            out[key] = f"{verb} {cur.rowcount}"
        except psycopg.Error as exc:
            out[key] = f"ERROR:  {exc.sqlstate}"

    try:
        with psycopg.connect(dsn, autocommit=True) as holder, \
                psycopg.connect(dsn, autocommit=True) as a, \
                psycopg.connect(dsn, autocommit=True) as b:
            holder.execute("SELECT pg_advisory_lock(2241)")
            a.execute("SET statement_timeout = '60s'")
            b.execute("SET statement_timeout = '60s'")
            ta = threading.Thread(target=run, args=("a", a, f"DELETE FROM {tbl} WHERE CASE WHEN id > 1000 THEN false "
                                                              f"WHEN id = 999 THEN cd_gate('{tbl}', id) ELSE true END",
                                                    "DELETE"))
            ta.start()
            for _ in range(200):
                if _value(dsn, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                               "AND objid = 2241 AND NOT granted") == "1":
                    r[f"{tag} paused"] = "yes"
                    break
                time.sleep(0.1)
            tb = threading.Thread(target=run, args=("b", b, f"DELETE FROM {tbl} WHERE id BETWEEN 500 AND 1500",
                                                    "DELETE"))
            tb.start()
            for _ in range(200):
                if _value(dsn, "SELECT wait_event_type FROM pg_stat_activity "
                               f"WHERE query LIKE 'DELETE FROM {tbl} WHERE id BETWEEN%'") == "Lock":
                    r[f"{tag} wait"] = "Lock"
                    break
                time.sleep(0.1)
            holder.execute("SELECT pg_advisory_unlock(2241)")
            ta.join(90)
            tb.join(90)
    except psycopg.Error as exc:
        r[f"{tag} paused"] = query_error(_message(exc))
    r[f"{tag} a"] = out.get("a", "A did not finish")
    r[f"{tag} b"] = out.get("b", "B did not finish")
    r[f"{tag} left"] = _value(dsn, f"SELECT count(*) FROM {tbl}", ("SET enable_seqscan = on",))


# #2268: VACUUM judges each row group again under its chunk-group lock (class 101)
# and sets a block only while it holds every group covering it.
# pgcolumnar.vm_set_hold_ms pauses before the set (wait event Extension), and
# pgcolumnar.vm_set_release_hold_ms before each locked group is given back
# (Timeout/PgSleep), which is where the reads run.
RL_LOCKS = ("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
            "WHERE a.query = 'VACUUM {}' AND l.locktype = 'advisory' AND l.objsubid = 101 AND l.granted")


def _rl_vacuum(dsn, tbl, hold, release, out):
    """Start VACUUM `tbl` on its own connection in a thread, with the two pauses."""
    def vacuum():
        try:
            with psycopg.connect(dsn, autocommit=True) as vac:
                vac.execute(f"SET pgcolumnar.vm_set_hold_ms = {hold}")
                vac.execute(f"SET pgcolumnar.vm_set_release_hold_ms = {release}")
                vac.execute("SET statement_timeout = '120s'")
                vac.execute(f"VACUUM {tbl}")
                out["vacuum"] = "VACUUM"
        except psycopg.Error as exc:
            out["vacuum"] = f"ERROR:  {exc.sqlstate}"

    t = threading.Thread(target=vacuum)
    t.start()
    return t


def _rl_event(dsn, tbl, event):
    """'yes' once VACUUM `tbl` waits on `event`, within 15 s."""
    for _ in range(150):
        if _value(dsn, "SELECT count(*) FROM pg_stat_activity "
                       f"WHERE query = 'VACUUM {tbl}' AND wait_event = '{event}'") == "1":
            return "yes"
        time.sleep(0.1)
    return "no"


def _rl_wait_event(dsn, tbl):
    return _value(dsn, f"SELECT wait_event FROM pg_stat_activity WHERE query = 'VACUUM {tbl}'")


def _rl_delete_count(dsn, sql):
    return _value(dsn, f"WITH d AS ({sql} RETURNING 1) SELECT count(*) FROM d")


def _rl_rel(dsn, r):
    """11a: one 20,000-row group; a DELETE of ids 300..400 commits during the first
    pause. During the second pause read block 1 and the index-only count, and try a
    second DELETE of the group with a 1 s lock_timeout."""
    _load(dsn, "vr_rel", 20000)
    r["rl before"] = _value(dsn, _vis("vr_rel", 1))
    out = {}
    t = _rl_vacuum(dsn, "vr_rel", 5000, 10000, out)
    r["rl held"] = _rl_event(dsn, "vr_rel", "Extension")
    r["rl del"] = _rl_delete_count(dsn, "DELETE FROM vr_rel WHERE id BETWEEN 300 AND 400")
    r["rl still"] = _rl_wait_event(dsn, "vr_rel")
    r["rl rel"] = _rl_event(dsn, "vr_rel", "PgSleep")
    r["rl bit"] = _value(dsn, _vis("vr_rel", 1))
    r["rl ios"] = _ios(dsn, "SELECT count(*) FROM vr_rel WHERE id BETWEEN 300 AND 400")
    try:
        with psycopg.connect(dsn, autocommit=True) as w2:
            w2.execute("SET lock_timeout = 1000")
            cur = w2.execute("DELETE FROM vr_rel WHERE id = 5000")
            r["rl w2"] = f"DELETE {cur.rowcount}"
    except psycopg.Error as exc:
        r["rl w2"] = f"ERROR:  {exc.sqlstate}: {exc.diag.message_primary}"
    r["rl w2still"] = _rl_wait_event(dsn, "vr_rel")
    t.join(120)
    r["rl vout"] = out.get("vacuum", "vacuum did not finish")
    r["rl after"] = _value(dsn, _vis("vr_rel", 1))
    r["rl ios2"] = _ios(dsn, "SELECT count(*) FROM vr_rel WHERE id BETWEEN 300 AND 400 OR id = 5000")


def _rl_open(dsn, r):
    """11b: a writer's DELETE flushes during the first pause and its transaction
    stays open, holding the group's lock. VACUUM must finish meanwhile."""
    _load(dsn, "vr_open", 20000)
    r["op before"] = _value(dsn, _vis("vr_open", 1))
    out = {}
    t = _rl_vacuum(dsn, "vr_open", 5000, 0, out)
    r["op held"] = _rl_event(dsn, "vr_open", "Extension")
    try:
        with psycopg.connect(dsn, autocommit=True) as h:
            h.execute("BEGIN")
            h.execute("DELETE FROM vr_open WHERE id BETWEEN 300 AND 400")
            r["op still"] = _rl_wait_event(dsn, "vr_open")
            t.join(30)
            r["op done"] = "no" if t.is_alive() else "yes"
            r["op bit"] = _value(dsn, _vis("vr_open", 1))
            h.execute("COMMIT")
    except psycopg.Error as exc:
        r["op still"] = query_error(_message(exc))
    t.join(120)
    r["op after"] = _value(dsn, _vis("vr_open", 1))
    r["op ios"] = _ios(dsn, "SELECT count(*) FROM vr_open WHERE id BETWEEN 300 AND 400")


def _rl_strad(dsn, r):
    """11c: six contiguous 1000-row groups; block 3 straddles groups 1 and 2. The
    first three are loaded and vacuumed (blocks 1..9 set) before the other three, so
    blocks 10..19 are unset and lie in the same run, after group 2. A DELETE of id
    1100 (group 2) commits during the first pause. At the first release
    pause read block 3 and the index-only count; then record the most class-101
    locks VACUUM holds at once until it ends."""
    _run(dsn, "CREATE TABLE vr_strad (id int, pad int) USING pgcolumnar; "
              "SELECT pgcolumnar.set_options('vr_strad', stripe_row_limit => 1000); "
              "INSERT INTO vr_strad SELECT g, g FROM generate_series(1, 3000) g; "
              "CREATE INDEX vr_strad_id ON vr_strad (id);")
    _run(dsn, "VACUUM vr_strad")
    _run(dsn, "INSERT INTO vr_strad SELECT g, g FROM generate_series(3001, 6000) g")
    sd_set = ("SELECT count(*) FILTER (WHERE pgcolumnar.vm_is_visible('vr_strad', b)) "
              "FROM generate_series(1, 19) b")
    r["sd blocks"] = _value(dsn, "SELECT string_agg((ctid::text::point)[0]::int::text, '|' ORDER BY id) "
                                 "FROM vr_strad WHERE id IN (1000, 1001, 1100, 3000, 3001)")
    r["sd before"] = _value(dsn, sd_set)
    out = {}
    t = _rl_vacuum(dsn, "vr_strad", 5000, 1500, out)
    held = _rl_event(dsn, "vr_strad", "Extension")
    deleted = _rl_delete_count(dsn, "DELETE FROM vr_strad WHERE id = 1100")
    r["sd held"] = f"{held}|{deleted}|{_rl_wait_event(dsn, 'vr_strad')}"
    r["sd rel"] = _rl_event(dsn, "vr_strad", "PgSleep")
    r["sd b3"] = _value(dsn, _vis("vr_strad", 3))
    r["sd ios"] = _ios(dsn, "SELECT count(*) FROM vr_strad WHERE id = 1100")
    most = 0
    deadline = time.monotonic() + 120
    while t.is_alive() and time.monotonic() < deadline:
        n = _value(dsn, RL_LOCKS.format("vr_strad"))
        if n.isdigit():
            most = max(most, int(n))
        time.sleep(0.1)
    t.join(10)
    r["sd max"] = str(most)
    r["sd after"] = _value(dsn, sd_set)


def _rl_order(dsn, r):
    """11d: three contiguous 1000-row groups, never vacuumed. At the first release
    pause, before group 1 is given back, read blocks 1..4 and the locks held."""
    _run(dsn, "CREATE TABLE vr_order (id int, pad int) USING pgcolumnar; "
              "SELECT pgcolumnar.set_options('vr_order', stripe_row_limit => 1000); "
              "INSERT INTO vr_order SELECT g, g FROM generate_series(1, 3000) g; "
              "CREATE INDEX vr_order_id ON vr_order (id);")
    bits = ("SELECT string_agg(pgcolumnar.vm_is_visible('vr_order', b)::text, '|' ORDER BY b) "
            "FROM generate_series(1, 4) b")
    r["od before"] = _value(dsn, bits)
    out = {}
    t = _rl_vacuum(dsn, "vr_order", 0, 3000, out)
    r["od rel"] = _rl_event(dsn, "vr_order", "PgSleep")
    r["od bits"] = _value(dsn, bits)
    r["od locks"] = _value(dsn, RL_LOCKS.format("vr_order"))
    t.join(120)
    r["od after"] = _value(dsn, bits)
    stats = "SELECT relpages || '|' || relallvisible FROM pg_class WHERE relname = 'vr_order'"
    _run(dsn, "ANALYZE vr_order")
    r["od analyzed"] = _value(dsn, stats)
    _run(dsn, "VACUUM vr_order")
    r["od stats"] = _value(dsn, stats)


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
        # #2170: group 1 (rows 1..20000, blocks 1..67) is set by the load's VACUUM;
        # a second INSERT starts group 2 at row 150001, blocks 516..583, not yet set.
        # The DELETE takes blocks 1 and 5 of group 1 during the hold. VACUUM judges
        # the groups again under their locks and leaves those two unset.
        keep = "SELECT count(*) FILTER (WHERE pgcolumnar.vm_is_visible('vr_keep', b)) FROM ({}) t"
        g1 = "SELECT b FROM generate_series(1, 67) b"
        g2 = "SELECT b FROM generate_series(516, 583) b"
        dele = "id BETWEEN 300 AND 400 OR id BETWEEN 1500 AND 1600"
        _vm_mid(dsn, r, "keep", "vr_keep", 20000, f"DELETE FROM vr_keep WHERE {dele}",
                f"SELECT ({keep.format(g1)}) || '|' || ({keep.format(g2)})",
                keep.format("SELECT b FROM (VALUES (1), (5)) v(b)"),
                f"SELECT count(*) FROM vr_keep WHERE {dele}",
                keep.format("SELECT b FROM generate_series(1, 67) b WHERE b NOT IN (1, 5)"),
                "INSERT INTO vr_keep SELECT g, g FROM generate_series(20001, 40000) g")
        r["keep fresh"] = _value(dsn, keep.format(g2))
        r["keep all"] = _ios(dsn, "SELECT count(*) FROM vr_keep WHERE id BETWEEN 1 AND 40000")
        # #2257 review, B1: ids 19500..19510 lie in block 67, the last block of row
        # group 1's run (blocks 1..67); the judgement must reach it.
        _vm_mid(dsn, r, "last", "vr_last", 20000, "DELETE FROM vr_last WHERE id BETWEEN 19500 AND 19510",
                _vis("vr_last", 67), _vis("vr_last", 67), rng.format("vr_last", 19500, 19510))
        # B2: three contiguous 1000-row groups; block 3 straddles groups 1 and 2, and
        # id 1100 (group 2) is deleted before a plain VACUUM. The runs of the clean
        # groups are blocks 1..2 and 7..9; block 3, one past the first, stays unset.
        _run(dsn, "CREATE TABLE vr_edge (id int, pad int) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('vr_edge', stripe_row_limit => 1000); "
                  "INSERT INTO vr_edge SELECT g, g FROM generate_series(1, 3000) g; "
                  "CREATE INDEX vr_edge_id ON vr_edge (id); "
                  "DELETE FROM vr_edge WHERE id = 1100;")
        _run(dsn, "VACUUM vr_edge")
        r["edge blocks"] = _value(dsn, "SELECT string_agg((ctid::text::point)[0]::int::text, '|' ORDER BY id) "
                                       "FROM vr_edge WHERE id IN (1000, 1001, 1099, 1101)")
        r["edge set"] = _value(dsn, "SELECT count(*) FILTER (WHERE pgcolumnar.vm_is_visible('vr_edge', b)) "
                                    "FROM (VALUES (1), (2), (7), (8), (9)) v(b)")
        r["edge b3"] = _value(dsn, _vis("vr_edge", 3))
        r["edge ios"] = _ios(dsn, "SELECT count(*) FROM vr_edge WHERE id = 1100")
        _run(dsn, "CREATE TABLE tr_ids (id int); "
                  "INSERT INTO tr_ids SELECT g FROM generate_series(500, 19500, 500) g; "
                  "CREATE TABLE tr_log (tbl text, n bigint, ios bool);")
        _run(dsn, TR_COUNT)
        for tag, tbl, variant, verb in (("trd", "trd", "plain", "DELETE"), ("tru", "tru", "plain", "UPDATE"),
                                        ("tre", "tre", "exc", "DELETE")):
            _tr_race(dsn, r, tag + " heap", tbl + "_h", "heap", variant, verb)
            _tr_race(dsn, r, tag, tbl, "pgcolumnar", variant, verb)
        _run(dsn, AB_PEEK)
        _ab_abort(dsn, r, "ab heap", "ab_h", "heap")
        _ab_abort(dsn, r, "ab", "ab", "pgcolumnar")
        _run(dsn, CD_GATE)
        _cd_race(dsn, r, "cd heap", "cd_h", "heap")
        _cd_race(dsn, r, "cd", "cd", "pgcolumnar")
        _rl_rel(dsn, r)
        _rl_open(dsn, r)
        _rl_strad(dsn, r)
        _rl_order(dsn, r)
        _run(dsn, "CREATE ROLE rl_plain LOGIN")
        try:
            with psycopg.connect(dsn.replace("user=postgres", "user=rl_plain"), autocommit=True) as plain:
                plain.execute("SET pgcolumnar.vm_set_release_hold_ms = 1000")
                r["rl suset"] = "SET"
        except psycopg.Error as exc:
            r["rl suset"] = f"ERROR:  {exc.sqlstate}: {exc.diag.message_primary}"
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            a.execute("DROP ROLE IF EXISTS rl_plain")


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


def test_2170_premise_group_1_set_group_2_not_yet_set_before_hold(io, expect):
    expect.text(io["keep before"], "67|0",
                "#2170 premise: delete between vacuum's two steps: group 1 set and group 2 not yet set before the hold")


def test_2170_premise_vacuum_paused_before_its_set(io, expect):
    expect.text(io["keep held"], "yes",
                "#2170 premise: delete between vacuum's two steps: vacuum paused before its set")


def test_2170_premise_delete_committed_while_paused(io, expect):
    expect.text(io.get("keep still", "not reached"), "yes",
                "#2170 premise: delete between vacuum's two steps: the delete committed while vacuum was paused")


def test_2170_blocks_1_and_5_not_all_visible_after(io, expect):
    expect.text(io["keep after"], "0",
                "#2170 delete between vacuum's two steps: blocks 1 and 5 not all-visible after vacuum")


def test_2170_clean_blocks_set_before_hold_keep_their_bits(io, expect):
    expect.text(io["keep clean"], "65",
                "#2170 delete between vacuum's two steps: the 65 clean blocks set before the hold keep their bits")


def test_2170_blocks_this_vacuum_set_in_clean_group_keep_their_bits(io, expect):
    expect.text(io["keep fresh"], "68",
                "#2170 delete between vacuum's two steps: the 68 blocks this vacuum set in the clean group keep their bits")


def test_2170_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["keep ios"], "0",
                "#2170 delete between vacuum's two steps: index-only scan returns no deleted row")


def test_2170_index_only_count_of_every_id_matches_live_rows(io, expect):
    expect.text(io["keep all"], "39798",
                "#2170 delete between vacuum's two steps: index-only count of every id matches the 39798 live rows")


def test_2170_premise_last_block_67_all_visible_before(io, expect):
    expect.text(io["last before"], "t",
                "#2170 premise: delete in the last block of a run between vacuum's two steps: block 67 all-visible before the writer starts")


def test_2170_premise_last_block_vacuum_paused_before_its_set(io, expect):
    expect.text(io["last held"], "yes",
                "#2170 premise: delete in the last block of a run between vacuum's two steps: vacuum paused before its set")


def test_2170_premise_last_block_delete_committed_while_paused(io, expect):
    expect.text(io.get("last still", "not reached"), "yes",
                "#2170 premise: delete in the last block of a run between vacuum's two steps: the delete committed while vacuum was paused")


def test_2170_last_block_67_not_all_visible_after(io, expect):
    expect.text(io["last after"], "f",
                "#2170 delete in the last block of a run between vacuum's two steps: block 67 not all-visible after vacuum")


def test_2170_last_block_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["last ios"], "0",
                "#2170 delete in the last block of a run between vacuum's two steps: index-only scan returns no deleted row")


def test_2170_premise_shared_block_ids_lie_in_block_3(io, expect):
    expect.text(io["edge blocks"], "3|3|3|3",
                "#2170 premise: a delete in a block shared by two row groups: ids 1000, 1001, 1099 and 1101 all lie in block 3")


def test_2170_premise_shared_block_vacuum_set_clean_runs(io, expect):
    expect.text(io["edge set"], "5",
                "#2170 premise: a delete in a block shared by two row groups: vacuum set the five blocks of the clean groups' runs")


def test_2170_shared_block_3_not_all_visible_after(io, expect):
    expect.text(io["edge b3"], "f",
                "#2170 a delete in a block shared by two row groups: block 3 not all-visible after vacuum")


def test_2170_shared_block_index_only_scan_returns_no_deleted_row(io, expect):
    expect.text(io["edge ios"], "0",
                "#2170 a delete in a block shared by two row groups: index-only scan returns no deleted row")
def test_trigger_of_delete_during_vacuum_every_target_block_all_visible_before(io, expect):
    expect.text(io["trd before"], "t",
                "an AFTER trigger of a delete during vacuum: every target block all-visible before the writer starts")


def test_trigger_of_delete_during_vacuum_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["trd paused"], "yes", "an AFTER trigger of a delete during vacuum: vacuum ran while the writer was paused")


def test_trigger_of_delete_during_vacuum_vacuum_set_all_39_target_blocks_again(io, expect):
    expect.text(io.get("trd set", "not reached"), "39",
                "an AFTER trigger of a delete during vacuum: the vacuum set all 39 target blocks again")


def test_trigger_of_delete_during_vacuum_writer_deleted_39_rows(io, expect):
    expect.text(f"{io['trd written']}|{io['trd heap written']}", "DELETE 39|DELETE 39",
                "an AFTER trigger of a delete during vacuum: the writer deleted 39 rows, as heap did")


def test_trigger_of_delete_during_vacuum_trigger_read_through_index_only_scan(io, expect):
    expect.text(f"{io['trd ios']}|{io['trd heap ios']}", "t|t",
                "an AFTER trigger of a delete during vacuum: the trigger read through an index-only scan, as on heap")


def test_trigger_of_delete_during_vacuum_trigger_count_matches_heap(io, expect):
    expect.text(f"{io['trd n']}|{io['trd heap n']}", "0|0",
                "an AFTER trigger of a delete during vacuum: the trigger counts none of the deleted rows, as heap does")


def test_trigger_of_delete_during_vacuum_count_after_commit_matches_heap(io, expect):
    expect.text(f"{io['trd after']}|{io['trd heap after']}", "0|0",
                "an AFTER trigger of a delete during vacuum: after commit none of them is counted, as on heap")


def test_trigger_of_update_during_vacuum_every_target_block_all_visible_before(io, expect):
    expect.text(io["tru before"], "t",
                "an AFTER trigger of an update during vacuum: every target block all-visible before the writer starts")


def test_trigger_of_update_during_vacuum_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["tru paused"], "yes", "an AFTER trigger of an update during vacuum: vacuum ran while the writer was paused")


def test_trigger_of_update_during_vacuum_vacuum_set_all_39_target_blocks_again(io, expect):
    expect.text(io.get("tru set", "not reached"), "39",
                "an AFTER trigger of an update during vacuum: the vacuum set all 39 target blocks again")


def test_trigger_of_update_during_vacuum_writer_updated_39_rows(io, expect):
    expect.text(f"{io['tru written']}|{io['tru heap written']}", "UPDATE 39|UPDATE 39",
                "an AFTER trigger of an update during vacuum: the writer updated 39 rows, as heap did")


def test_trigger_of_update_during_vacuum_trigger_read_through_index_only_scan(io, expect):
    expect.text(f"{io['tru ios']}|{io['tru heap ios']}", "t|t",
                "an AFTER trigger of an update during vacuum: the trigger read through an index-only scan, as on heap")


def test_trigger_of_update_during_vacuum_trigger_count_matches_heap(io, expect):
    expect.text(f"{io['tru n']}|{io['tru heap n']}", "39|39",
                "an AFTER trigger of an update during vacuum: the trigger counts each updated row once, as heap does")


def test_trigger_of_update_during_vacuum_count_after_commit_matches_heap(io, expect):
    expect.text(f"{io['tru after']}|{io['tru heap after']}", "39|39",
                "an AFTER trigger of an update during vacuum: after commit each is counted once, as on heap")


def test_trigger_in_exception_block_during_vacuum_every_target_block_all_visible_before(io, expect):
    expect.text(io["tre before"], "t",
                "an AFTER trigger reading in an EXCEPTION block during vacuum: every target block all-visible before the writer starts")


def test_trigger_in_exception_block_during_vacuum_vacuum_ran_while_writer_paused(io, expect):
    expect.text(io["tre paused"], "yes", "an AFTER trigger reading in an EXCEPTION block during vacuum: vacuum ran while the writer was paused")


def test_trigger_in_exception_block_during_vacuum_vacuum_set_all_39_target_blocks_again(io, expect):
    expect.text(io.get("tre set", "not reached"), "39",
                "an AFTER trigger reading in an EXCEPTION block during vacuum: the vacuum set all 39 target blocks again")


def test_trigger_in_exception_block_during_vacuum_writer_deleted_39_rows(io, expect):
    expect.text(f"{io['tre written']}|{io['tre heap written']}", "DELETE 39|DELETE 39",
                "an AFTER trigger reading in an EXCEPTION block during vacuum: the writer deleted 39 rows, as heap did")


def test_trigger_in_exception_block_during_vacuum_trigger_read_through_index_only_scan(io, expect):
    expect.text(f"{io['tre ios']}|{io['tre heap ios']}", "t|t",
                "an AFTER trigger reading in an EXCEPTION block during vacuum: the trigger read through an index-only scan, as on heap")


def test_trigger_in_exception_block_during_vacuum_trigger_count_matches_heap(io, expect):
    expect.text(f"{io['tre n']}|{io['tre heap n']}", "0|0",
                "an AFTER trigger reading in an EXCEPTION block during vacuum: the trigger counts none of the deleted rows, as heap does")


def test_trigger_in_exception_block_during_vacuum_count_after_commit_matches_heap(io, expect):
    expect.text(f"{io['tre after']}|{io['tre heap after']}", "0|0",
                "an AFTER trigger reading in an EXCEPTION block during vacuum: after commit none of them is counted, as on heap")


def test_delete_that_fails_after_nested_read_writer_outcome_matches_heap(io, expect):
    expect.text(f"{io['ab writer']} || {io['ab heap writer']}",
                "NOTICE:  00000|ERROR:  22012|W_END || NOTICE:  00000|ERROR:  22012|W_END",
                "a delete that fails after a nested read: the nested read ran, the delete failed with 22012 and the next transaction ran, as on heap")


def test_delete_that_fails_after_nested_read_counts_use_three_scans(io, expect):
    expect.text(io["ab plans"], "Seq Scan|Index Scan|Index Only Scan",
                "a delete that fails after a nested read: the counts use a seq scan, an index scan and an index-only scan")


def test_delete_that_fails_after_nested_read_every_row_live_in_new_transaction(io, expect):
    expect.text(f"{io['ab after']} || {io['ab heap after']}", "20000/20000/20000 || 20000/20000/20000",
                "a delete that fails after a nested read: every row is live by each scan in a new transaction, as on heap")


def test_delete_that_fails_after_nested_read_every_row_live_after_vacuum(io, expect):
    expect.text(f"{io['ab vac']} || {io['ab heap vac']}", "20000/20000/20000 || 20000/20000/20000",
                "a delete that fails after a nested read: every row is live by each scan after vacuum, as on heap")


def test_second_deleter_during_nested_read_first_paused(io, expect):
    expect.text(io["cd paused"], "yes",
                "a second deleter during the first's nested read: the first deleter was paused inside its nested read")


def test_second_deleter_during_nested_read_waits_for_first(io, expect):
    expect.text(f"{io['cd wait']}|{io['cd heap wait']}", "Lock|Lock",
                "a second deleter during the first's nested read: the second deleter waits for the first, as on heap")


def test_second_deleter_during_nested_read_first_deletes_1000(io, expect):
    expect.text(f"{io['cd a']}|{io['cd heap a']}", "DELETE 1000|DELETE 1000",
                "a second deleter during the first's nested read: the first deleter deletes its 1000 rows, as on heap")


def test_second_deleter_during_nested_read_second_fails_40001(io, expect):
    expect.text(f"{io['cd b']}|{io['cd heap b']}", "ERROR:  40001|DELETE 500",
                "a second deleter during the first's nested read: the second then fails with serialization_failure, where heap deletes the other 500")


def test_second_deleter_during_nested_read_rows_left(io, expect):
    expect.text(f"{io['cd left']}|{io['cd heap left']}", "19000|18500",
                "a second deleter during the first's nested read: 19000 rows remain, where heap leaves 18500")


def test_2268_premise_delete_during_pause_block_1_all_visible_before(io, expect):
    expect.text(io["rl before"], "t",
                "#2268 premise: a delete during vacuum's pause: block 1 all-visible before the writer starts")


def test_2268_premise_delete_during_pause_vacuum_paused_before_set(io, expect):
    expect.text(io["rl held"], "yes", "#2268 premise: a delete during vacuum's pause: vacuum paused before its set")


def test_2268_premise_delete_during_pause_delete_committed_while_paused(io, expect):
    expect.text(f"{io['rl del']}|{io['rl still']}", "101|Extension",
                "#2268 premise: a delete during vacuum's pause: the delete of 101 rows committed while vacuum was paused")


def test_2268_premise_delete_during_pause_vacuum_paused_again_holding_group(io, expect):
    expect.text(io["rl rel"], "yes", "#2268 premise: a delete during vacuum's pause: vacuum paused again holding the row group")


def test_2268_delete_during_pause_block_1_not_all_visible_while_held(io, expect):
    expect.text(io["rl bit"], "f",
                "#2268 a delete during vacuum's pause: block 1 not all-visible while vacuum holds the group")


def test_2268_delete_during_pause_ios_counts_no_deleted_row_while_held(io, expect):
    expect.text(io["rl ios"], "0",
                "#2268 a delete during vacuum's pause: index-only scan counts no deleted row while vacuum holds the group")


def test_2268_delete_during_pause_second_delete_waits_and_times_out(io, expect):
    expect.text(io["rl w2"], "ERROR:  55P03: canceling statement due to lock timeout",
                "#2268 a delete during vacuum's pause: a second delete of the group waits for vacuum's lock and times out")


def test_2268_premise_delete_during_pause_vacuum_still_paused_when_second_gave_up(io, expect):
    expect.text(io["rl w2still"], "PgSleep",
                "#2268 premise: a delete during vacuum's pause: vacuum was still paused when the second delete gave up")


def test_2268_delete_during_pause_vacuum_finished_cleanly(io, expect):
    expect.text(io["rl vout"], "VACUUM", "#2268 a delete during vacuum's pause: vacuum finished cleanly")


def test_2268_delete_during_pause_after_vacuum(io, expect):
    expect.text(f"{io['rl after']}|{io['rl ios2']}", "f|1",
                "#2268 a delete during vacuum's pause: after vacuum block 1 is not all-visible and the index-only scan counts only id 5000")


def test_2268_premise_writer_holding_group_block_1_all_visible_before(io, expect):
    expect.text(io["op before"], "t", "#2268 premise: a writer holding the group: block 1 all-visible before the writer starts")


def test_2268_premise_writer_holding_group_delete_flushed_while_paused(io, expect):
    expect.text(f"{io['op held']}|{io['op still']}", "yes|Extension",
                "#2268 premise: a writer holding the group: its delete flushed while vacuum was paused before its set")


def test_2268_writer_holding_group_vacuum_finished_while_open(io, expect):
    expect.text(io.get("op done", "not reached"), "yes",
                "#2268 a writer holding the group: vacuum finished while the writer's transaction was open")


def test_2268_writer_holding_group_block_1_not_all_visible_while_open(io, expect):
    expect.text(io.get("op bit", "not reached"), "f",
                "#2268 a writer holding the group: block 1 not all-visible while the writer is open")


def test_2268_writer_holding_group_after_commit(io, expect):
    expect.text(f"{io['op after']}|{io['op ios']}", "f|0",
                "#2268 a writer holding the group: after the writer commits block 1 is not all-visible and the index-only scan counts no deleted row")


def test_2268_premise_shared_block_ids_lie_in_block_3(io, expect):
    expect.text(io["sd blocks"], "3|3|3|10|10",
                "#2268 premise: a delete in a block shared by two row groups during vacuum's pause: ids 1000, 1001 and 1100 lie in block 3, and 3000 and 3001 in block 10")


def test_2268_premise_shared_block_blocks_all_visible_before(io, expect):
    expect.text(io["sd before"], "9",
                "#2268 premise: a delete in a block shared by two row groups during vacuum's pause: blocks 1..9 all-visible and 10..19 not before the writer starts")


def test_2268_premise_shared_block_delete_committed_while_paused(io, expect):
    expect.text(io["sd held"], "yes|1|Extension",
                "#2268 premise: a delete in a block shared by two row groups during vacuum's pause: the delete committed while vacuum was paused")


def test_2268_premise_shared_block_vacuum_paused_again_holding_groups(io, expect):
    expect.text(io["sd rel"], "yes",
                "#2268 premise: a delete in a block shared by two row groups during vacuum's pause: vacuum paused again holding row groups")


def test_2268_shared_block_block_3_not_all_visible_while_held(io, expect):
    expect.text(io["sd b3"], "f",
                "#2268 a delete in a block shared by two row groups during vacuum's pause: block 3 not all-visible while vacuum holds the groups")


def test_2268_shared_block_ios_counts_no_row_for_1100(io, expect):
    expect.text(io["sd ios"], "0",
                "#2268 a delete in a block shared by two row groups during vacuum's pause: index-only scan counts no row for id 1100")


def test_2268_shared_block_vacuum_held_at_most_2_groups(io, expect):
    expect.text(io["sd max"], "2",
                "#2268 a delete in a block shared by two row groups during vacuum's pause: vacuum held at most the 2 groups covering one block")


def test_2268_shared_block_only_block_3_lost_its_bit(io, expect):
    expect.text(io["sd after"], "18",
                "#2268 a delete in a block shared by two row groups during vacuum's pause: after vacuum 18 of blocks 1..19, all but block 3, are all-visible")


def test_2268_premise_clean_shared_block_nothing_set_before(io, expect):
    expect.text(io["od before"], "false|false|false|false",
                "#2268 premise: a block shared by two clean row groups: blocks 1..4 not all-visible before vacuum")


def test_2268_premise_clean_shared_block_vacuum_paused_before_giving_back_group_1(io, expect):
    expect.text(io["od rel"], "yes",
                "#2268 premise: a block shared by two clean row groups: vacuum paused before giving back group 1")


def test_2268_clean_shared_block_set_while_group_1_held(io, expect):
    expect.text(io["od bits"], "true|true|true|false",
                "#2268 a block shared by two clean row groups: at that pause blocks 1..3 are set and block 4 not yet")


def test_2268_clean_shared_block_vacuum_holds_groups_1_and_2(io, expect):
    expect.text(io["od locks"], "2",
                "#2268 a block shared by two clean row groups: at that pause vacuum holds groups 1 and 2")


def test_2268_clean_shared_block_all_set_after(io, expect):
    expect.text(io["od after"], "true|true|true|true",
                "#2268 a block shared by two clean row groups: after vacuum blocks 1..4 are all-visible")


def test_2268_premise_clean_shared_block_analyze_records_vm_blocks(io, expect):
    expect.text(io["od analyzed"], "5|9",
                "#2268 premise: a block shared by two clean row groups: ANALYZE records 5 pages and the VM's 9 all-visible blocks")


def test_2268_clean_shared_block_second_vacuum_records_all_visible_pages(io, expect):
    expect.text(io["od stats"], "5|4",
                "#2268 a block shared by two clean row groups: a second vacuum that sets no bit records 4 of the 5 pages all-visible")


def test_2268_release_hold_is_superuser_only(io, expect):
    expect.text(io["rl suset"], 'ERROR:  42501: permission denied to set parameter "pgcolumnar.vm_set_release_hold_ms"',
                "#2268 a role without superuser cannot set pgcolumnar.vm_set_release_hold_ms")

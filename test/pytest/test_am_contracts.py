"""Contracts the access method owes core DML and transactions (#1341).

Each is stated in docs/ or owed by core's table-AM interface, and none was tested.
Independent of test/am_contracts.sh: same public seam, own fixtures. Check names
match the shell twin so the two compare by name.
"""

import re
import threading
import time

import psycopg


def _state(conn, sql):
    """-> "ok", or the SQLSTATE the statement raised. A refusal is decided by its code."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error as e:
        return e.sqlstate


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_insert_on_conflict(pgc_conn, expect):
    """docs/limitations.md:655-656: DO NOTHING works, DO UPDATE raises the row-lock error.

    On a table with a primary key, which is the index ON CONFLICT arbitrates on.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE oc (id int PRIMARY KEY, v text) USING pgcolumnar")
        cur.execute("INSERT INTO oc SELECT g, 'v' || g FROM generate_series(1, 10) g")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM oc"), 10,
               "premise: the conflict table holds ten rows")
    after = "SELECT count(*) || '|' || (SELECT v FROM oc WHERE id = {k}) FROM oc"
    got = _state(pgc_conn, "INSERT INTO oc VALUES (5, 'dup'), (11, 'new') ON CONFLICT DO NOTHING")
    expect.text(f"{got}|{_one(pgc_conn, after.format(k=5))}", "ok|11|v5",
                "ON CONFLICT DO NOTHING skips the duplicate and inserts the new row")
    got = _state(pgc_conn, "INSERT INTO oc VALUES (6, 'dup'), (12, 'new') ON CONFLICT (id) DO NOTHING")
    expect.text(f"{got}|{_one(pgc_conn, after.format(k=6))}", "ok|12|v6",
                "and so does ON CONFLICT (id) DO NOTHING, naming its arbiter")
    got = _state(pgc_conn, "INSERT INTO oc VALUES (7, 'upd') ON CONFLICT (id) DO UPDATE SET v = excluded.v")
    expect.text(f"{got}|{_one(pgc_conn, after.format(k=7))}", "0A000|12|v7",
                "ON CONFLICT DO UPDATE is refused as unsupported (0A000), and changes nothing")
    got = _state(pgc_conn, "INSERT INTO oc VALUES (13, 'upd') ON CONFLICT (id) DO UPDATE SET v = excluded.v")
    expect.text(f"{got}|{_one(pgc_conn, after.format(k=13))}", "ok|13|upd",
                "ON CONFLICT DO UPDATE with no conflicting row inserts it")
    got = _state(pgc_conn, "INSERT INTO oc VALUES (30, 'first'), (30, 'second') ON CONFLICT DO NOTHING")
    twice = _one(pgc_conn, "SELECT count(*) || '|' || min(v) FROM oc WHERE id = 30")
    expect.text(f"{got}|{twice}", "ok|1|first",
                "a key twice in one DO NOTHING statement is inserted once, the first time")


def test_insert_on_conflict_withdraws_a_row_a_concurrent_insert_beat(pgc_cluster, pgc_conn, expect):
    """The withdrawal path: a row free at the pre-check that conflicts once inserted.

    A second connection holds key 20 uncommitted. DO NOTHING on 20 and 21 waits, then
    must leave exactly one row 20 -- the holder's -- and its own 21.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ocr (id int PRIMARY KEY, v text) USING pgcolumnar")
        cur.execute("INSERT INTO ocr SELECT g, 'v' || g FROM generate_series(1, 10) g")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
    holder = psycopg.connect(pgc_cluster.dsn(), autocommit=False)
    try:
        with holder.cursor() as hc:
            hc.execute(f'SET search_path TO "{schema}"')
            hc.execute("INSERT INTO ocr VALUES (20, 'holder')")
        timer = threading.Timer(2.0, holder.commit)
        timer.start()
        started = time.monotonic()
        got = _state(pgc_conn, "INSERT INTO ocr VALUES (20, 'racer'), (21, 'racer') ON CONFLICT DO NOTHING")
        waited = time.monotonic() - started
        timer.join()
    finally:
        holder.close()
    expect.at_least(int(waited * 10), 10, "premise: the racer waited for the holder to commit")
    expect.text(f"{got}|" + _one(pgc_conn, "SELECT count(*) FILTER (WHERE id = 20) || '|' || "
                                           "min(v) FILTER (WHERE id = 20) || '|' || "
                                           "count(*) FILTER (WHERE id = 21) FROM ocr"),
                "ok|1|holder|1",
                "a DO NOTHING racing an uncommitted insert of the same key waits, then skips it")
    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SELECT count(*) || '|' || min(v) FROM ocr WHERE id = 20")
        idx = cur.fetchone()[0]
        cur.execute("RESET enable_seqscan")
    expect.text(idx, "1|holder", "and the unique index answers the key with that one row")


def test_insert_on_conflict_racing_sessions_leave_each_key_once(pgc_cluster, pgc_conn, expect):
    """The withdrawal itself, which a two-session arm does not reach.

    A row is withdrawn only when another session inserts the key after this one's
    pre-check and before its own insert, and no ordering of two sessions can force
    that. So eight sessions race on clock-derived keys for a few seconds. Measured:
    0 duplicate keys on three runs; with the withdrawal disabled, 407 to 507.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ocs (id bigint PRIMARY KEY, w int) USING pgcolumnar")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
    errors, attempts, stop = [], [], time.monotonic() + 5

    def worker():
        n = 0
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute(f'SET search_path TO "{schema}"')
            while time.monotonic() < stop:
                try:
                    c.execute("INSERT INTO ocs VALUES ((extract(epoch FROM clock_timestamp()) * 200)::bigint, 1)"
                              " ON CONFLICT DO NOTHING")
                except psycopg.Error as e:
                    errors.append(e.sqlstate)
                n += 1
        attempts.append(n)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = _one(pgc_conn, "SELECT count(*) FROM ocs")
    expect.text(str(rows < sum(attempts)).lower(), "true",
                "premise: the racing sessions contended for keys")
    dup = _one(pgc_conn, "SELECT count(*) - count(DISTINCT id) FROM ocs")
    expect.text(f"{dup}|{len(errors)}", "0|0",
                "sessions racing DO NOTHING on the same keys leave each key once, with no error")


def test_a_truncate_that_rolls_back_restores_every_row(pgc_conn, expect):
    """TRUNCATE swaps the table onto new storage, so the old storage's rows and catalog
    entries must come back whole if the swap does not commit (#1341)."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE tr (id int PRIMARY KEY, v int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('tr', stripe_row_limit => 1000)")
        cur.execute("INSERT INTO tr SELECT g, g * 3 FROM generate_series(1, 5000) g")
    shape = "SELECT count(*) || '|' || sum(v) FROM tr"
    before = _one(pgc_conn, shape)
    expect.text(before, "5000|37507500", "premise: the truncate table holds 5000 rows")
    # psycopg rolls the block back when psycopg.Rollback is raised inside it.
    with pgc_conn.transaction():
        with pgc_conn.cursor() as cur:
            cur.execute("TRUNCATE tr")
        raise psycopg.Rollback()
    expect.text(_one(pgc_conn, shape), before,
                "a TRUNCATE rolled back at transaction level restores every row")
    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SELECT v FROM tr WHERE id = 4321")
        by_index = cur.fetchone()[0]
        cur.execute("RESET enable_seqscan")
    expect.num(by_index, 12963, "and the primary key still finds them by index")
    with pgc_conn.transaction():
        with pgc_conn.cursor() as cur:
            cur.execute("SAVEPOINT s")
            cur.execute("TRUNCATE tr")
            cur.execute("ROLLBACK TO SAVEPOINT s")
            cur.execute("INSERT INTO tr VALUES (5001, 1)")
    expect.text(_one(pgc_conn, shape), "5001|37507501",
                "a TRUNCATE rolled back to a savepoint restores every row, and the transaction goes on")
    expect.text(_state(pgc_conn, "INSERT INTO tr VALUES (17, 0)"), "23505",
                "and the primary key still refuses a duplicate of a restored row")
    # And the rows the parent buffered before the savepoint: TRUNCATE discards the
    # cached write state, and they may still be in it, unflushed.
    with pgc_conn.transaction():
        with pgc_conn.cursor() as cur:
            cur.execute("INSERT INTO tr SELECT g, 1 FROM generate_series(6001, 8500) g")
            cur.execute("SAVEPOINT s")
            cur.execute("TRUNCATE tr")
            cur.execute("INSERT INTO tr SELECT g, 2 FROM generate_series(1, 5) g")
            cur.execute("ROLLBACK TO SAVEPOINT s")
    expect.text(_one(pgc_conn, shape), "7501|37510001",
                "rows the parent buffered before a savepoint survive a TRUNCATE rolled back to it")


def test_an_aborted_inner_block_keeps_the_outer_statements_rows(pgc_conn, expect):
    """A plpgsql EXCEPTION block is a subtransaction, and a statement inside it flushes
    every pending write at its ExecutorEnd -- the outer statement's too. If that flush
    belongs to the inner subtransaction, the block's rollback takes the outer rows with
    it (#1341). Each arm runs on heap as well; the columnar answer must be heap's."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE sx_side (id int) USING pgcolumnar")
        cur.execute("""CREATE FUNCTION sx_f(i int) RETURNS int LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        INSERT INTO sx_side VALUES (i);
        PERFORM 1 / 0;
    EXCEPTION WHEN division_by_zero THEN NULL;
    END;
    RETURN i;
END $$""")
        for am in ("pgcolumnar", "heap"):
            cur.execute(f"CREATE TABLE sx_{am} (id int) USING {am}")
            cur.execute(f"INSERT INTO sx_{am} SELECT sx_f(g) FROM generate_series(1, 5000) g")
    shape = "SELECT count(*) || '|' || sum(id) FROM sx_{}"
    expect.text(_one(pgc_conn, shape.format("pgcolumnar")), _one(pgc_conn, shape.format("heap")),
                "an outer insert survives an aborted inner block, as on heap")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM sx_side"), 0,
               "and the inner block's own rows are gone")
    with pgc_conn.cursor() as cur:
        for am in ("pgcolumnar", "heap"):
            cur.execute(f"DELETE FROM sx_{am} WHERE id % 2 = 0 AND sx_f(id) IS NOT NULL")
    expect.text(_one(pgc_conn, shape.format("pgcolumnar")), _one(pgc_conn, shape.format("heap")),
                "an outer delete survives an aborted inner block, as on heap")


def test_a_read_inside_an_aborted_inner_block_keeps_the_outer_statements_rows(pgc_conn, expect):
    """A read of the table inside the block must see the rows the outer INSERT has
    buffered so far, as heap's does, so it flushes them. That flush's catalog rows
    belonged to the block's subtransaction, and its rollback took the outer rows with
    them (#1360). The read's answer is pinned too, so a fix cannot trade the lost rows
    for a read that misses them. Every arm runs on heap as well; the columnar answer
    must be heap's. HOW: abort, nested (a block that commits into one that raises),
    and commit (the control)."""
    with pgc_conn.cursor() as cur:
        cur.execute("""CREATE FUNCTION sx_rd(i int, t text, how text) RETURNS int LANGUAGE plpgsql AS $$
DECLARE n int; c text; sq text;
BEGIN
    IF how = 'once' AND i <> 2 THEN RETURN NULL; END IF;
    BEGIN
        IF how = 'delself' THEN
            c := current_setting('pgcolumnar.enable_custom_scan'); sq := current_setting('enable_seqscan');
            PERFORM set_config('pgcolumnar.enable_custom_scan', 'off', true), set_config('enable_seqscan', 'off', true);
            EXECUTE format('DELETE FROM %s WHERE id = %s', t, i + 1);
            PERFORM set_config('pgcolumnar.enable_custom_scan', c, true), set_config('enable_seqscan', sq, true);
        END IF;
        IF how = 'nested' THEN
            BEGIN
                EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
            EXCEPTION WHEN no_data_found THEN NULL;
            END;
        ELSE
            EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
        END IF;
        IF how <> 'commit' THEN PERFORM 1 / 0; END IF;
    EXCEPTION WHEN division_by_zero THEN NULL;
    END;
    RETURN n;
END $$""")
        for how in ("abort", "nested", "commit"):
            for am in ("pgcolumnar", "heap"):
                t = f"rd_{how}_{am}"
                cur.execute(f"CREATE TABLE {t} (id int PRIMARY KEY, seen int) USING {am}")
                cur.execute(f"INSERT INTO {t} SELECT g, sx_rd(g, '{t}', '{how}') FROM generate_series(1, 1000) g")

    def shape(t):
        return _one(pgc_conn, f"SELECT count(*) || '|' || sum(id) FROM {t}")

    def seen(t):
        return str(_one(pgc_conn, f"SELECT sum(seen) FROM {t}"))

    expect.text(shape("rd_abort_pgcolumnar"), shape("rd_abort_heap"),
                "an outer insert survives a read of its own table inside an aborted inner block, as on heap")
    expect.text(seen("rd_abort_pgcolumnar"), seen("rd_abort_heap"),
                "and that read saw the rows the outer statement had inserted so far, as on heap")
    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_bitmapscan = off")
        everything = "SELECT count(*) FROM rd_abort_pgcolumnar WHERE id BETWEEN 1 AND 1000"
        plan = "\n".join(r[0] for r in cur.execute(f"EXPLAIN (COSTS OFF) {everything}").fetchall())
        found = cur.execute(everything).fetchone()[0]
        cur.execute("RESET enable_seqscan")
        cur.execute("RESET enable_bitmapscan")
    expect.text(f"{'index' if 'Index' in plan else 'other'}|{found}", "index|1000",
                "and the primary key finds every outer row by index")
    expect.text(f"{shape('rd_nested_pgcolumnar')}|{seen('rd_nested_pgcolumnar')}",
                f"{shape('rd_nested_heap')}|{seen('rd_nested_heap')}",
                "an outer insert survives a read in a nested block that commits into an aborted one, as on heap")
    expect.text(f"{shape('rd_commit_pgcolumnar')}|{seen('rd_commit_pgcolumnar')}",
                f"{shape('rd_commit_heap')}|{seen('rd_commit_heap')}",
                "a read inside an inner block that commits keeps every outer row once, as on heap")
    # The same for the delete marks an outer DELETE has buffered: the read flushes
    # them, and the block's rollback took them away, so the deleted rows came back.
    with pgc_conn.cursor() as cur:
        for am in ("pgcolumnar", "heap"):
            t = f"rd_del_{am}"
            cur.execute(f"CREATE TABLE {t} (id int, seen int) USING {am}")
            cur.execute(f"INSERT INTO {t} SELECT g, 0 FROM generate_series(1, 1000) g")
            cur.execute(f"DELETE FROM {t} WHERE id % 2 = 0 AND sx_rd(id, '{t}', 'abort') IS NOT NULL")
    expect.text(shape("rd_del_pgcolumnar"), shape("rd_del_heap"),
                "an outer delete survives a read of its own table inside an aborted inner block, as on heap")
    # The block deletes the next row by index before its read. That path writes no
    # enclosing marks first, so the block's own mark is on disk when the read writes
    # the outer marks; it must not be carried into them and written again after the abort.
    with pgc_conn.cursor() as cur:
        for am in ("pgcolumnar", "heap"):
            t = f"rd_ds_{am}"
            cur.execute(f"CREATE TABLE {t} (id int PRIMARY KEY, seen int) USING {am}")
            cur.execute(f"INSERT INTO {t} SELECT g, 0 FROM generate_series(1, 1000) g")
            cur.execute(f"DELETE FROM {t} WHERE id % 2 = 0 AND sx_rd(id, '{t}', 'delself') IS NOT NULL")
    expect.text(shape("rd_ds_pgcolumnar"), shape("rd_ds_heap"),
                "and a delete the aborted block made itself stays undone, as on heap")
    # THE COST IS LINEAR IN THE ROWS (#1403 review). Kept as one growing stripe, each
    # abort's re-write covered every earlier row: at twice the rows, about four times
    # the file. The size at 2n must stay under 2.5 times the size at n.
    with pgc_conn.cursor() as cur:
        for sz in (400, 800):
            cur.execute(f"CREATE TABLE rd_sz_{sz} (id int, seen int, pad text) USING pgcolumnar")
            cur.execute(f"INSERT INTO rd_sz_{sz} SELECT g, sx_rd(g, 'rd_sz_{sz}', 'abort'),"
                        " md5(g::text) || md5((g + 1)::text) || md5((g + 2)::text)"
                        f" FROM generate_series(1, {sz}) g")
    expect.text(_one(pgc_conn, "SELECT (SELECT count(*) FROM rd_sz_400) || '|' || (SELECT count(*) FROM rd_sz_800)"
                               " || '|' || CASE WHEN pg_relation_size('rd_sz_800') < 2.5 * pg_relation_size('rd_sz_400')"
                               " THEN 'linear' ELSE 'x' || round(pg_relation_size('rd_sz_800')::numeric"
                               " / pg_relation_size('rd_sz_400'), 1) END"),
                "400|800|linear",
                "a read in an aborted block at twice the rows writes under 2.5 times the file, with every row kept")
    # And one read costs one write, not the rest of the statement (#1403 review): eager
    # until the statement ended, one early read made every later row its own group.
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rd_once (id int, seen int, pad text) USING pgcolumnar")
        cur.execute("INSERT INTO rd_once SELECT g, sx_rd(g, 'rd_once', 'once'), md5(g::text)"
                    " FROM generate_series(1, 5000) g")
    groups = _one(pgc_conn, "SELECT count(*) FROM pgcolumnar.row_group"
                            " WHERE storage_id = pgcolumnar.get_storage_id('rd_once')")
    expect.text(f"{_one(pgc_conn, 'SELECT count(*) FROM rd_once')}|{'few' if groups <= 3 else f'{groups} groups'}",
                "5000|few", "a single read early in a 5000-row insert keeps every row in at most 3 row groups")


def test_an_insert_racing_an_uncommitted_delete_of_its_key(pgc_cluster, pgc_conn, expect):
    """Session 1 removes key k and has not committed; session 2 inserts k (#1341).

    On heap session 2 waits for session 1: a rollback leaves k taken (23505), a commit
    frees it. One row with k either way, never two.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ud (k int UNIQUE, v text) USING pgcolumnar")
        cur.execute("INSERT INTO ud VALUES (5, 'a'), (6, 'b'), (7, 'c')")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]

    def race(hold, end, insert):
        holder = psycopg.connect(pgc_cluster.dsn(), autocommit=False)
        try:
            with holder.cursor() as hc:
                hc.execute(f'SET search_path TO "{schema}"')
                hc.execute(hold)
            timer = threading.Timer(2.0, getattr(holder, end))
            timer.start()
            got = _state(pgc_conn, insert)
            timer.join()
        finally:
            holder.close()
        return got

    row = "SELECT count(*) || '|' || min(v) FROM ud WHERE k = {}"
    got = race("DELETE FROM ud WHERE k = 5", "rollback", "INSERT INTO ud VALUES (5, 'racer')")
    expect.text(f"{got}|{_one(pgc_conn, row.format(5))}", "23505|1|a",
                "an insert of a key whose delete is uncommitted waits, and fails when the delete rolls back")
    got = race("DELETE FROM ud WHERE k = 6", "commit", "INSERT INTO ud VALUES (6, 'racer')")
    expect.text(f"{got}|{_one(pgc_conn, row.format(6))}", "ok|1|racer",
                "and succeeds when the delete commits, leaving one row with the key")
    got = race("UPDATE ud SET k = 70 WHERE k = 7", "rollback", "INSERT INTO ud VALUES (7, 'racer')")
    expect.text(f"{got}|{_one(pgc_conn, row.format(7))}", "23505|1|c",
                "an insert of a key an uncommitted UPDATE moved away fails when the update rolls back")


def test_an_index_built_after_a_delete_serves_an_older_snapshot(pgc_cluster, pgc_conn, expect):
    """Session 1 holds a REPEATABLE READ snapshot from before a committed DELETE of row
    4242; then an index is built. Session 1 still sees the row, so an index scan must
    find it, as on heap, which indexes rows not yet dead to every snapshot (#1341)."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ib (id int, v int) USING pgcolumnar")
        cur.execute("INSERT INTO ib SELECT g, g FROM generate_series(1, 10000) g")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
    old = psycopg.connect(pgc_cluster.dsn(), autocommit=False)
    try:
        with old.cursor() as oc:
            oc.execute(f'SET search_path TO "{schema}"')
            oc.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            oc.execute("SELECT count(*) FROM ib")
            before = oc.fetchone()[0]
            with pgc_conn.cursor() as cur:
                cur.execute("DELETE FROM ib WHERE id = 4242")
                cur.execute("CREATE INDEX ib_id ON ib (id)")
            oc.execute("SET LOCAL enable_seqscan = off")
            oc.execute("SET LOCAL enable_bitmapscan = off")
            oc.execute("EXPLAIN (COSTS OFF) SELECT count(*) FROM ib WHERE id = 4242")
            plan = "\n".join(r[0] for r in oc.fetchall())
            oc.execute("SELECT count(*) FROM ib WHERE id = 4242")
            seen = oc.fetchone()[0]
        old.rollback()
    finally:
        old.close()
    expect.num(before, 10000, "premise: the older snapshot was taken before the delete")
    expect.text(str(_one(pgc_conn, "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ib_id'::regclass")),
                "True", "premise: the index exists and is valid")
    expect.num(len(re.findall(r"Index (Only )?Scan using ib_id", plan)), 1,
               "premise: the older snapshot's lookup is an index scan")
    expect.num(seen, 1, "an index built after a delete finds the row for an older snapshot that sees it")
    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SELECT count(*) FROM ib WHERE id = 4242")
        now = cur.fetchone()[0]
        cur.execute("RESET enable_seqscan")
        cur.execute("RESET enable_bitmapscan")
    expect.num(now, 0, "and a new snapshot does not see the deleted row by that index")

    # The cost of indexing deleted rows, pinned: they go in as not alive, so a UNIQUE
    # index still builds over a key whose only other holder is a deleted row.
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ibu (k int) USING pgcolumnar")
        cur.execute("INSERT INTO ibu VALUES (5), (6)")
        cur.execute("DELETE FROM ibu WHERE k = 5")
        cur.execute("INSERT INTO ibu VALUES (5)")
    built = _state(pgc_conn, "CREATE UNIQUE INDEX ibu_k ON ibu (k)")
    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SELECT count(*) FROM ibu WHERE k = 5")
        five = cur.fetchone()[0]
        cur.execute("RESET enable_seqscan")
    expect.text(f"{built}|{five}", "ok|1", "a unique index builds over a key whose other holder is a deleted row")


def test_reindex_reclaims_the_entries_of_rows_no_snapshot_sees(pgc_conn, expect):
    """An index build holds deleted rows only while some snapshot can still see them
    (#1407). It held every deleted row, so REINDEX, documented as what reclaims stale
    entries, reclaimed none: 9 rows in 10 deleted, and the rebuilt index kept its size
    where heap's shrank to a tenth. Heap is run alongside as the control."""
    with pgc_conn.cursor() as cur:
        for am in ("pgcolumnar", "heap"):
            cur.execute(f"CREATE TABLE ix_{am} (a int) USING {am}")
            cur.execute(f"INSERT INTO ix_{am} SELECT g FROM generate_series(1, 100000) g")
            cur.execute(f"CREATE INDEX ix_{am}_a ON ix_{am} (a)")
            cur.execute(f"DELETE FROM ix_{am} WHERE a % 10 <> 0")

    def reclaim(am):
        before = _one(pgc_conn, f"SELECT pg_relation_size('ix_{am}_a')")
        with pgc_conn.cursor() as cur:
            cur.execute(f"REINDEX INDEX ix_{am}_a")
        after = _one(pgc_conn, f"SELECT pg_relation_size('ix_{am}_a')")
        return "reclaimed" if after < before // 2 else f"kept:{before}->{after}"

    with pgc_conn.cursor() as cur:
        col, heap = reclaim("pgcolumnar"), reclaim("heap")
        cur.execute("SET enable_seqscan = off")
        found = _one(pgc_conn, "SELECT count(*) FROM ix_pgcolumnar WHERE a > 0")
        cur.execute("RESET enable_seqscan")
    expect.text(f"{col}|{heap}|{found}", "reclaimed|reclaimed|10000",
                "REINDEX after a committed delete of most rows reclaims their entries, as heap's does")


def test_the_documented_refusals_change_nothing(pgc_conn, expect):
    """Each is refused where it is chosen, as limitations.md says, and nothing is left
    changed by the attempt (#1341 items 44, 51, 77). A failed concurrent build leaves
    its index behind, marked invalid, as core does for any such failure."""
    import os
    import shutil
    import tempfile

    def state(sql):
        try:
            with pgc_conn.cursor() as cur:
                cur.execute(sql)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"

    count = "SELECT count(*) || '|' || sum(a) FROM rf"
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rf (a int) USING pgcolumnar")
        cur.execute("INSERT INTO rf SELECT g FROM generate_series(1, 5000) g")
    expect.text(f"{state('CREATE UNLOGGED TABLE rf_u (a int) USING pgcolumnar')}|"
                f"{_one(pgc_conn, 'SELECT count(*) FROM pg_class WHERE relname = ' + repr('rf_u'))}",
                "0A000|0", "CREATE UNLOGGED TABLE ... USING pgcolumnar is refused (0A000) and leaves no table")
    expect.text(f"{state('ALTER TABLE rf SET UNLOGGED')}|"
                f"{_one(pgc_conn, 'SELECT relpersistence FROM pg_class WHERE relname = ' + repr('rf'))}|"
                f"{_one(pgc_conn, count)}",
                "0A000|p|5000|12502500", "ALTER TABLE ... SET UNLOGGED is refused (0A000), and the table stays logged with its rows")

    # A tablespace directory the server's own user owns, outside any root-only parent.
    ts = tempfile.mkdtemp(prefix="pgc-rf-ts-", dir="/tmp")
    os.chmod(ts, 0o700)
    if os.geteuid() == 0:
        shutil.chown(ts, user="postgres")
    try:
        with pgc_conn.cursor() as cur:
            cur.execute(f"CREATE TABLESPACE rf_ts_py LOCATION '{ts}'")
        expect.text(f"{state('ALTER TABLE rf SET TABLESPACE rf_ts_py')}|"
                    f"{_one(pgc_conn, 'SELECT reltablespace FROM pg_class WHERE relname = ' + repr('rf'))}|"
                    f"{_one(pgc_conn, count)}",
                    "0A000|0|5000|12502500", "ALTER TABLE ... SET TABLESPACE is refused (0A000), and the table stays where it was with its rows")
    finally:
        with pgc_conn.cursor() as cur:
            cur.execute("DROP TABLESPACE IF EXISTS rf_ts_py")
        shutil.rmtree(ts, ignore_errors=True)

    expect.text(f"{state('CREATE INDEX CONCURRENTLY rf_cic ON rf (a)')}|"
                f"{_one(pgc_conn, 'SELECT indisvalid FROM pg_index WHERE indexrelid = ' + repr('rf_cic') + '::regclass')}",
                "0A000|False", "CREATE INDEX CONCURRENTLY is refused (0A000), and the index it leaves is marked invalid")

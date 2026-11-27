"""A ctid range reads only the row groups that hold it (TID range scans).

Before this, `WHERE ctid >= '(600000,0)'` on a 10M-row table matched 0 rows and
still read all 67 row groups (#1294): the access method had no scan_set_tidrange,
so the range was a per-row filter over the whole table.

THE ORACLE is the same query with `enable_tidscan = off`, which is that per-row
filter. THE COST is the table's own blocks from pg_statio, read under a decode the
zone maps cannot answer (`max(body || 'y')`: an unfiltered max is metadata-only).

The bounds that matter are the ones no row has. A block holds 291 rows
(MaxHeapTuplesPerPage), offsets 1..291, but a bound may say offset 0 or anything up
to 65535, and the executor makes `ctid < '(B,0)'` into an upper bound of
(B-1, 65535). Read without clamping, offset 400 named a row in the next block.

ON A PRIVATE DATABASE (`pgc_own_db`): `pg_stat_reset()` resets the database's
counters, and nothing else may be counting there. Independent of
`test/tid_range_scan.sh`: its own columns, row count, group size, and ranges.
"""

import psycopg

from pgc_vacuity import query_error

ROWS = 260000
GROUP = 7000


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _plan(conn, sql):
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql)
        return "\n".join(r[0] for r in cur.fetchall())


def _digest(conn, where, tidscan):
    with conn.cursor() as cur:
        cur.execute(f"SET enable_tidscan = {tidscan}")
        cur.execute("SELECT count(*), md5(string_agg(k::text || body, ',' ORDER BY k))"
                    f" FROM tids WHERE {where}")
        n, h = cur.fetchone()
        cur.execute("RESET enable_tidscan")
    return f"{n}|{h}"


def _blocks(conn, where):
    """Blocks of `tids` read while a forced decode over `where` ran."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT pg_stat_reset()")
        cur.execute(f"SELECT count(*), max(body || 'y') FROM tids WHERE {where}")
        cur.fetchall()
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT coalesce(heap_blks_read,0) + coalesce(heap_blks_hit,0)"
                    " FROM pg_statio_user_tables WHERE relname = 'tids'")
        row = cur.fetchone()
    return int(row[0]) if row else None


def _tid(conn, k):
    return _one(conn, f"SELECT ctid FROM tids WHERE k = {k}")


def _fixture(conn, expect):
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE tids (k bigint, body text) USING pgcolumnar")
        cur.execute(f"SELECT pgcolumnar.set_options('tids', stripe_row_limit => {GROUP})")
        cur.execute(f"INSERT INTO tids SELECT g, repeat(md5((g * 7)::text), 2)"
                    f" FROM generate_series(1, {ROWS}) g")
        cur.execute("ANALYZE tids")
    expect.num(_one(conn, "SELECT count(*) FROM tids"), ROWS, "premise: the table holds every row")
    groups = _one(conn, "SELECT count(*) FROM pgcolumnar.row_group"
                        " WHERE storage_id = pgcolumnar.get_storage_id('tids')")
    expect.at_least(groups, 30, "premise: the table has many row groups, so skipping can show")


def test_a_ctid_range_reads_only_its_row_groups(pgc_own_db, expect):
    conn = pgc_own_db
    _fixture(conn, expect)

    tail, a, b, last = _tid(conn, 246001), _tid(conn, 61234), _tid(conn, 98765), _tid(conn, ROWS)
    blk = int(a.strip("()").split(",")[0])
    print(f"-- ctids: 246001={tail} 61234={a} 98765={b} {ROWS}={last}")

    expect.text("yes" if "Tid Range Scan" in _plan(conn, f"SELECT k FROM tids WHERE ctid >= '{tail}'")
                else "no", "yes", "premise: a ctid range plans a TID range scan, so the arms below exercise it")

    ranges = [
        f"ctid >= '{tail}'",
        f"ctid > '{a}' AND ctid < '{b}'",
        f"ctid <= '{a}'",
        f"ctid >= '{last}'",
        f"ctid > '{last}'",
        "ctid < '(0,1)'",
        f"ctid < '({blk},0)'",
        f"ctid >= '({blk},0)' AND ctid <= '({blk + 3},0)'",
        f"ctid > '({blk},291)'",
        f"ctid >= '({blk},500)' AND ctid <= '({blk + 1},700)'",
        f"ctid <= '({blk},65535)'",
    ]
    for where in ranges:
        expect.text(_digest(conn, where, "on"), _digest(conn, where, "off"),
                    f"a ctid range returns what the per-row filter returns: {where}")

    # Rescanned with new bounds per outer row: a reader built for one range must
    # not answer the next. The plan premise is taken on the very query compared.
    lat = ("SELECT string_agg(r.i || ':' || x.n || ':' || x.h, ' ' ORDER BY r.i)"
           f" FROM (VALUES (1, '{a}'::tid, '{b}'::tid), (2, '{tail}'::tid, '{last}'::tid),"
           f"              (3, '{b}'::tid, '{a}'::tid), (4, '(0,0)'::tid, '{a}'::tid)) r(i, lo, hi),"
           " LATERAL (SELECT count(*) n, md5(string_agg(k::text, ',' ORDER BY k)) h FROM tids"
           "          WHERE ctid >= r.lo AND ctid <= r.hi) x")
    expect.text("yes" if "Tid Range Scan" in _plan(conn, lat) else "no", "yes",
                "premise: the per-range join rescans a TID range scan")
    with conn.cursor() as cur:
        on = _one(conn, lat)
        cur.execute("SET enable_tidscan = off")
        off = _one(conn, lat)
        cur.execute("RESET enable_tidscan")
    expect.text(on, off, "a TID range scan rescanned with new bounds returns what the filter returns")

    # Cursors (#1306 review). A TID range scan counts as able to scan backward, so
    # no Material goes above it: a SCROLL cursor, and a plain DECLARE the portal
    # makes scrollable, send a backward fetch to the access method. Compared,
    # fetch for fetch, with the same session under enable_tidscan = off.
    cq = f"SELECT k FROM tids WHERE ctid > '{a}' AND ctid < '{b}'"
    expect.text("yes" if "Tid Range Scan" in _plan(conn, cq) else "no", "yes",
                "premise: the cursor's query plans a TID range scan")

    def cursor_run(tidscan, kind):
        out = []
        with conn.cursor() as cur:
            cur.execute(f"SET enable_tidscan = {tidscan}")
            cur.execute("BEGIN")
            cur.execute(f"DECLARE c {kind} CURSOR FOR {cq}")
            for f in ("FETCH 3 FROM c", "FETCH LAST FROM c", "FETCH BACKWARD 4 FROM c",
                      "FETCH ABSOLUTE 2 FROM c"):
                try:
                    cur.execute(f)
                    out.append(",".join(str(r[0]) for r in cur.fetchall()))
                except psycopg.Error as e:
                    out.append(f"error {e.sqlstate}")
                    break
            cur.execute("ROLLBACK")
            cur.execute("RESET enable_tidscan")
        return " | ".join(out)
    expect.text(cursor_run("on", "SCROLL"), cursor_run("off", "SCROLL"),
                "a SCROLL cursor over a ctid range fetches backward as the filter's plan does")
    # A plain DECLARE over the filter's plan is made forward-only; over a TID
    # range scan it is made scrollable, so it must answer as a SCROLL cursor
    # does. What it may never do is reach the access method backward.
    expect.text(cursor_run("on", ""), cursor_run("off", "SCROLL"),
                "a plain cursor over a ctid range answers a backward fetch correctly")

    # A cursor whose query has a subplan: the Material's node id must not repeat
    # one of the subplan's (#1306 review); an assert build checks they are unique.
    sq = (f"SELECT k, (SELECT min(k) FROM tids) AS low FROM tids"
          f" WHERE ctid > '{a}' AND ctid < '{b}'")
    sub_plan = _plan(conn, sq)
    expect.num(int("Tid Range Scan" in sub_plan) + int("InitPlan" in sub_plan), 2,
               "premise: the subquery cursor plans a TID range scan and a subplan")

    def sub_cursor(tidscan):
        out = []
        with conn.cursor() as cur:
            cur.execute(f"SET enable_tidscan = {tidscan}")
            cur.execute("BEGIN")
            try:
                cur.execute(f"DECLARE c SCROLL CURSOR FOR {sq}")
                for f in ("FETCH LAST FROM c", "FETCH BACKWARD 3 FROM c"):
                    cur.execute(f)
                    out.append(",".join(f"{r[0]}/{r[1]}" for r in cur.fetchall()))
            except psycopg.Error as e:
                out.append(f"error {e.sqlstate}")
            cur.execute("ROLLBACK")
            cur.execute("RESET enable_tidscan")
        return " | ".join(out)
    expect.text(sub_cursor("on"), sub_cursor("off"),
                "a SCROLL cursor whose query has a subplan fetches backward as the filter's plan does")

    # Rows written earlier in the same transaction are in the range.
    def same_xact(tidscan):
        with conn.cursor() as cur:
            cur.execute(f"SET enable_tidscan = {tidscan}")
            cur.execute("BEGIN")
            cur.execute(f"INSERT INTO tids SELECT g, 'pending' FROM generate_series({ROWS + 1}, {ROWS + 650}) g")
            cur.execute("EXPLAIN SELECT k FROM tids WHERE ctid >= '" + tail + "'")
            plan = "\n".join(r[0] for r in cur.fetchall())
            cur.execute("SELECT count(*) || '|' || md5(string_agg(k::text, ',' ORDER BY k))"
                        f" FROM tids WHERE ctid >= '{tail}'")
            got = cur.fetchone()[0]
            cur.execute("ROLLBACK")
            cur.execute("RESET enable_tidscan")
        return plan, got
    plan_on, got_on = same_xact("on")
    _, got_off = same_xact("off")
    expect.text("yes" if "Tid Range Scan" in plan_on else "no", "yes",
                "premise: inside a transaction the range still plans a TID range scan")
    expect.text(got_on, got_off,
                "rows inserted earlier in the same transaction are in the range, as the filter sees them")

    whole = _blocks(conn, "true")
    part = _blocks(conn, f"ctid >= '{tail}'")
    print(f"-- blocks: whole {whole}, tail {part}")
    expect.at_least(whole, 100, "premise: reading the whole table reads its blocks")
    expect.at_least(part, 1, "premise: the tail range read the table at all")
    # The margin is whole - 5 * part: positive passes, and a failure reports by how much.
    expect.at_least(whole - 5 * part, 1,
                    "a ctid range over the last row groups reads under a fifth of the table")

    # Deletes inside the range, then compaction, which gives the surviving rows of
    # the thinned group new, higher row numbers.
    before = _tid(conn, 249000)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tids WHERE k BETWEEN 250500 AND 251900")
    expect.text(_digest(conn, f"ctid >= '{tail}'", "on"), _digest(conn, f"ctid >= '{tail}'", "off"),
                "and rows deleted inside the range stay deleted")
    with conn.cursor() as cur:
        cur.execute("SET enable_tidscan = off")
        left = _one(conn, f"SELECT count(*) FROM tids WHERE ctid >= '{tail}'")
        cur.execute("RESET enable_tidscan")
    # 246001..260000 is 14,000 rows; the delete took 1,401 of them.
    expect.num(left, 14000 - 1401, "premise: the delete removed rows from the range")
    expect.num(_one(conn, "SELECT pgcolumnar.compact_rewrite('tids', 0.1, 0)"), 1,
               "premise: compaction rewrote the group the delete thinned")
    moved = _tid(conn, 249000)
    expect.text("moved" if moved != before else f"kept {moved}", "moved",
                "premise: compaction gave the surviving rows new addresses")
    for where in (f"ctid >= '{tail}'", f"ctid >= '{moved}'", f"ctid >= '{a}' AND ctid <= '{moved}'"):
        expect.text(_digest(conn, where, "on"), _digest(conn, where, "off"),
                    f"after compaction a ctid range returns what the filter returns: {where}")


def test_a_parallel_tid_range_scan_returns_what_the_filter_returns(pgc_own_db, expect):
    conn = pgc_own_db
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    if major < 19:
        expect.cannot_run(
            "UNSUPPORTED_MAJOR",
            f"parallel TID range scans arrived in PostgreSQL 19; this server is {major}",
            name="a parallel TID range scan returns what the filter returns")
        return
    _fixture(conn, expect)
    lo, hi = _tid(conn, 40001), _tid(conn, 222222)
    where = f"ctid >= '{lo}' AND ctid <= '{hi}'"
    want = _digest(conn, where, "off")
    with conn.cursor() as cur:
        # The custom scan off: with parallelism cheap the planner prefers a parallel
        # custom scan that filters ctid per row, and this is about the AM's path.
        for s in ("parallel_setup_cost = 0", "parallel_tuple_cost = 0",
                  "min_parallel_table_scan_size = 0", "max_parallel_workers_per_gather = 2",
                  "parallel_leader_participation = off", "pgcolumnar.enable_custom_scan = off"):
            cur.execute("SET " + s)
    q = ("SELECT count(*) || '|' || md5(string_agg(k::text || body, ',' ORDER BY k))"
         f" FROM tids WHERE {where}")
    plan = _plan(conn, q)
    launched = [ln.strip() for ln in plan.splitlines() if "Workers Launched" in ln]
    expect.text(f"{'Parallel Tid Range Scan' in plan}|{launched}",
                "True|['Workers Launched: 2']",
                "premise: the range runs as a parallel TID range scan with two workers")
    expect.text(_one(conn, q), want, "a parallel TID range scan returns what the filter returns")


def test_a_tid_scan_of_a_ctid_no_row_has_reaches_no_row(pgc_conn, expect):
    """#2058. A TID scan asks the access method whether each ctid is valid.

    The AM said yes to every ctid. On '(0,0)' it then failed an assertion, which
    restarts every backend of an assert build. On an offset past the 291 a block
    holds it read another block's row: '(0,295)' was row 294, whose ctid is
    (1,4). THE ORACLE is the same table copied into heap, where every one of
    these statements reaches no row.
    """
    conn = pgc_conn

    def run(sql, rows_after=None):
        """The first column of the first row, in a transaction rolled back after.

        Rolled back so that a statement that wrongly reaches a row cannot move
        or remove it and hide the next statement's fault. `rows_after`, if
        given, is read inside the transaction and appended.
        """
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(sql)
                got = str(cur.fetchone()[0])
                if rows_after:
                    cur.execute(rows_after)
                    got += "|" + str(cur.fetchone()[0])
                cur.execute("ROLLBACK")
                return got
        except psycopg.Error as e:
            return query_error(f"{type(e).__name__}: {str(e).strip().splitlines()[0]}")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE col_t (k int, w int) USING pgcolumnar")
        cur.execute("INSERT INTO col_t SELECT g, g * 3 FROM generate_series(1, 900) g")
        cur.execute("CREATE TABLE heap_t (k int, w int) USING heap")
        cur.execute("INSERT INTO heap_t SELECT g, g * 3 FROM generate_series(1, 900) g")
    expect.text(run("SELECT k || '@' || ctid::text FROM col_t WHERE k IN (290, 291) ORDER BY k DESC"),
                "291@(1,1)", "premise: the row after offset 291 of block 0 sits at (1,1)")
    expect.text("yes" if "Tid Scan" in run("EXPLAIN SELECT k FROM col_t WHERE ctid = '(0,295)'")
                else "no", "yes", "premise: a single ctid plans a TID scan")

    # The other side of the boundary, so a fix that refused every ctid fails here.
    expect.text(run("SELECT string_agg(k::text, ',') FROM col_t WHERE ctid = '(0,291)'"), "290",
                "a TID scan of the last offset a block holds, '(0,291)', still returns its row")
    expect.text(run("SELECT string_agg(k::text, ',' ORDER BY k) FROM col_t"
                    " WHERE ctid = ANY (ARRAY['(1,4)', '(0,295)', '(0,5)']::tid[])"), "4,294",
                "a TID scan of two ctids rows have and one past a block's last offset returns those two rows")

    def both(sql, after=None):
        """(columnar, heap) for one statement. Heap first: a crash on the columnar
        side kills the session."""
        want = run(sql.format(tab="heap_t"), after and after.format(tab="heap_t"))
        return run(sql.format(tab="col_t"), after and after.format(tab="col_t")), want

    left = "SELECT count(*) || ':' || sum(w) FROM {tab} WHERE w > 0"
    for t in ("(0,0)", "(2,0)", "(0,295)", "(2,600)"):
        got, want = both(f"SELECT count(*) FROM {{tab}} WHERE ctid = '{t}'")
        expect.text(got, want, f"a TID scan of ctid '{t}', which no row has, returns no row, as on heap")
        got, want = both(f"SELECT count(*) FROM {{tab}} WHERE ctid = ANY (ARRAY['{t}', '{t}']::tid[])")
        expect.text(got, want, f"a TID scan of ctid '{t}' in an array returns no row, as on heap")
        got, want = both(f"WITH u AS (UPDATE {{tab}} SET w = -w WHERE ctid = '{t}' RETURNING 1)"
                         " SELECT count(*) FROM u", left)
        expect.text(got, want, f"UPDATE ... WHERE ctid = '{t}' changes no row, and every row is unchanged")
        got, want = both(f"WITH d AS (DELETE FROM {{tab}} WHERE ctid = '{t}' RETURNING 1)"
                         " SELECT count(*) FROM d", left)
        expect.text(got, want, f"DELETE ... WHERE ctid = '{t}' deletes no row, and every row is still there")

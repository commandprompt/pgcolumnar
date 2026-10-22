"""The by-row-number fetch resolves its row group from a memo, not a per-fetch catalog
scan (#709). The pytest twin of `test/native_fetch_group_memo.sh`, every check by the
shell's name (#1341).

pgcolumnar_fetch_row runs twice per fetched TID, and each call used to read the whole
row-group list from the catalog: about 6000 index scans of pgcolumnar.row_group for 3000
fetched rows. The memo keeps the list per (storage, command, snapshot content) and
refreshes on a miss. Asserted: the measured statement is a nested loop of index probes;
3000 fetches cost a bounded number of row_group reads and no heap scan of row_group;
scattered fetches return exact values; a group flushed earlier in the transaction is
fetchable; a group flushed under an aborted savepoint, or retired mid-statement by
compact_rewrite() or compact(), does not resurrect through a stale memo; unique checks,
an AFTER UPDATE trigger's OLD row and a fresh DELETE are unchanged; and a
same-transaction TRUNCATE then fetch sees only the new rows.

ONE SESSION, ONE TRANSACTION where the shell runs one `psql -c`: the scan counters are
transaction-local pending stats, so the plan, the baseline, the measured statement and
the after-reading share a transaction, as they do in the shell.

READINGS ARE VALUES: a failed query is a query_error(), and the heap-scan delta is
computed only from two numbers. An empty pair used to subtract to 0 and pass in the
shell, the one check green with the server stopped. Survival reads the server log's
count of backends killed by a signal as well as SELECT 1.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_fetch_group_memo"
FORCE = ("SET enable_seqscan=off", "SET enable_bitmapscan=off",
         "SET pgcolumnar.enable_custom_scan=off", "SET pgcolumnar.enable_index_fetch_penalty=off",
         "SET max_parallel_workers_per_gather=0", "SET enable_mergejoin=off", "SET enable_hashjoin=off")
FETCHQ = ("SELECT count(x.v) FROM generate_series(1,3000) g, "
          "LATERAL (SELECT v FROM f WHERE f.id = (g*97)%100000+1) x")
COUNTERS = ("SELECT pg_stat_get_xact_numscans('pgcolumnar.row_group_pkey'::regclass), "
            "pg_stat_get_xact_numscans('pgcolumnar.row_group'::regclass)")


def _message(exc):
    diag = getattr(exc, "diag", None)
    primary = diag.message_primary if diag is not None else None
    return primary or (str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _failed(exc):
    return query_error(_message(exc))


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


def _await_up(dsn):
    for _ in range(120):
        if _value(dsn, "SELECT 1") == "1":
            return
        time.sleep(0.5)


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    _await_up(dsn)
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


def _setup(dsn, statements):
    """Run fixture statements in one autocommit session. -> None, or the failure as a
    query_error() for the readings that depend on them, so a setup that fails reds
    those checks by name instead of erroring the whole module."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in statements:
                conn.execute(s)
    except psycopg.Error as exc:
        return _failed(exc)
    return None


def _int(v):
    return v if isinstance(v, int) else None


def _measured(dsn):
    """The shell's one measured session: plan, baseline, statement, after, in one
    transaction. -> dict of readings."""
    # Each reading is kept as soon as it is taken: psql prints the plan before a later
    # statement of the same -c fails, so a failure after the plan must not take the
    # plan reading with it.
    r = {}
    plan = base = count = after = None
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            for s in FORCE:
                conn.execute(s)
            plan = [row[0] for row in conn.execute("EXPLAIN (COSTS off) " + FETCHQ)]
            base = conn.execute(COUNTERS).fetchone()
            count = conn.execute(FETCHQ).fetchone()[0]
            after = conn.execute(COUNTERS).fetchone()
            conn.rollback()
    except psycopg.Error as exc:
        err = _failed(exc)
        r["plan"] = err if plan is None else None
        r["count"] = err
        r["bounded"] = f"unbounded({err})"
        r["heap"] = f"no reading ({err})"
        if plan is None:
            return r
    if plan is not None:
        nl = sum(1 for l in plan if "Nested Loop" in l)
        ix = sum(1 for l in plan if "Index Scan using f_id" in l)
        r["plan"] = f"{nl}/{ix}"
    if after is None:
        return r
    r["count"] = "1" if count == 3000 else f"0 ({count})"
    bi, ai, bs, as_ = (_int(base[0]), _int(after[0]), _int(base[1]), _int(after[1]))
    r["bounded"] = ("bounded" if bi is not None and ai is not None and ai - bi <= 20
                    else f"unbounded({base[0]} -> {after[0]})")
    r["heap"] = (str(as_ - bs) if bs is not None and as_ is not None
                 else f"no reading ({base[1]} -> {after[1]})")
    return r


def _in_tx(dsn, statements, settings=(), commit=False):
    """-> the first column of the LAST statement's first row, all in one transaction
    (the shell's single `psql -c`), or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            for s in settings:
                conn.execute(s)
            cur = None
            for s in statements:
                cur = conn.execute(s)
            row = cur.fetchone() if cur is not None and cur.description else None
            conn.commit() if commit else conn.rollback()
    except psycopg.Error as exc:
        return _failed(exc)
    if row is None:
        return ""
    return "|".join("t" if v is True else "f" if v is False else str(v) for v in row)


def _savepoint_move(dsn):
    """The cursor drained after ROLLBACK TO SAVEPOINT -> MOVE ALL's command tag."""
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            conn.execute("SET pgcolumnar.stripe_row_limit=2000")
            for s in FORCE:
                conn.execute(s)
            conn.execute("DECLARE cur CURSOR FOR SELECT v FROM f WHERE id BETWEEN 1 AND 300000 ORDER BY id")
            conn.execute("MOVE 5 FROM cur")
            conn.execute("SAVEPOINT s")
            conn.execute("INSERT INTO f SELECT 100000+g, 'a'||g FROM generate_series(1,3000) g")
            conn.execute("MOVE 5 FROM cur")
            conn.execute("ROLLBACK TO SAVEPOINT s")
            cur = conn.execute("MOVE ALL FROM cur")
            tag = cur.statusmessage
            conn.commit()
    except psycopg.Error as exc:
        return _failed(exc)
    return tag or ""


def _unique_violation(dsn):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("CREATE TABLE u (id int, v text) USING pgcolumnar")
            conn.execute("CREATE UNIQUE INDEX u_id ON u (id)")
            conn.execute("INSERT INTO u SELECT g, 'u' FROM generate_series(1,5000) g")
            conn.execute("INSERT INTO u VALUES (4999, 'dup')")
    except psycopg.Error as exc:
        return "1" if exc.sqlstate == "23505" else f"0 ({exc.sqlstate} {_message(exc)})"
    return "0 (no error)"


def _trigger_old(dsn):
    notices = []
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
            conn.execute("CREATE TABLE trg (id int, v text) USING pgcolumnar")
            conn.execute("INSERT INTO trg SELECT g, 'o'||g FROM generate_series(1,3000) g")
            conn.execute("CREATE FUNCTION trg_f() RETURNS trigger LANGUAGE plpgsql AS "
                         "$$ BEGIN RAISE NOTICE 'old=%', OLD.v; RETURN NEW; END $$")
            conn.execute("CREATE TRIGGER t_au AFTER UPDATE ON trg FOR EACH ROW EXECUTE FUNCTION trg_f()")
            conn.execute("UPDATE trg SET v = 'n' WHERE id = 2500")
    except psycopg.Error as exc:
        return _failed(exc)
    return str(sum(1 for n in notices if "old=o2500" in n))


def _truncate_then_fetch(dsn):
    """-> the three counts the shell joins with '/', each followed by '/'."""
    out = []
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            conn.execute("SET pgcolumnar.stripe_row_limit=2000")
            for s in FORCE:
                conn.execute(s)
            conn.execute("CREATE TABLE tw (id int, v text) USING pgcolumnar")
            conn.execute("INSERT INTO tw SELECT g, 'o'||g FROM generate_series(1,5000) g")
            conn.execute("CREATE INDEX tw_id ON tw (id)")
            out.append(conn.execute("SELECT count(x.v) FROM generate_series(1,50) g, "
                                    "LATERAL (SELECT v FROM tw WHERE tw.id = g) x").fetchone()[0])
            conn.execute("TRUNCATE tw")
            conn.execute("INSERT INTO tw SELECT g, 'w'||g FROM generate_series(1,100) g")
            out.append(conn.execute("SELECT count(*) FROM generate_series(1,5000) g, "
                                    "LATERAL (SELECT v FROM tw WHERE tw.id = g) x").fetchone()[0])
            out.append(conn.execute("SELECT count(x.v) FROM generate_series(1,5000) g, "
                                    "LATERAL (SELECT v FROM tw WHERE tw.id = g) x").fetchone()[0])
            conn.commit()
    except psycopg.Error as exc:
        return _failed(exc)
    return "".join(f"{n}/" for n in out)


@pytest.fixture(scope="module")
def fm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as adm:
        adm.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        adm.execute(f"CREATE DATABASE {DB}")
    dsn = pgc_cluster.dsn(dbname=DB)
    r = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("SET pgcolumnar.stripe_row_limit=2000")
            conn.execute("CREATE TABLE f (id int, v text) USING pgcolumnar")
            conn.execute("INSERT INTO f SELECT g, 'v'||g FROM generate_series(1,100000) g")
            conn.execute("CREATE INDEX f_id ON f (id)")
        before = _crashes(pgc_cluster)
        r.update(_measured(dsn))
        _await_up(dsn)
        r["scattered"] = _in_tx(dsn, [
            "SELECT count(*) FROM generate_series(1,2000) g, "
            "LATERAL (SELECT v FROM f WHERE f.id = g*50 AND f.v = 'v'||(g*50)) x"], FORCE)
        _await_up(dsn)
        r["same tx flush"] = _in_tx(dsn, [
            "SELECT count(x.v) FROM generate_series(1,50) g, LATERAL (SELECT v FROM f WHERE f.id = g) x",
            "INSERT INTO f SELECT 200000+g, 'n'||g FROM generate_series(1,5000) g",
            "SELECT count(x.v) FROM generate_series(1,50) g, LATERAL (SELECT v FROM f WHERE f.id = 200000+g) x"],
            FORCE)
        _await_up(dsn)
        r["savepoint"] = _savepoint_move(dsn)
        _await_up(dsn)
        r_setup = _setup(dsn, ["SET pgcolumnar.stripe_row_limit=2000",
                               "CREATE TABLE r (id int, v text) USING pgcolumnar",
                               "INSERT INTO r SELECT g, 'r'||g FROM generate_series(1,10000) g",
                               "CREATE INDEX r_id ON r (id)",
                               "DELETE FROM r WHERE id <= 1000"])
        r["retire"] = r_setup or _in_tx(dsn, [
            "SELECT (SELECT count(*) FROM generate_series(1001,1500) g, LATERAL (SELECT v FROM r WHERE r.id = g) x), "
            "(SELECT pgcolumnar.compact_rewrite('r'::regclass, 0.1, 0) = 1), "
            "(SELECT count(*) FROM generate_series(1001,1500) g, LATERAL (SELECT v FROM r WHERE r.id = g) x)"],
            ("SET pgcolumnar.reclaim_coalesce=off",) + FORCE, commit=True)
        _await_up(dsn)
        fd_setup = _setup(dsn, ["SET pgcolumnar.stripe_row_limit=2000",
                                "CREATE TABLE fd (id int, v text) USING pgcolumnar",
                                "INSERT INTO fd SELECT g, 'f'||g FROM generate_series(1,10000) g",
                                "CREATE INDEX fd_id ON fd (id)",
                                "DELETE FROM fd WHERE id <= 2000"])
        r["compact"] = fd_setup or _in_tx(dsn, [
            "SELECT (SELECT count(*) FROM generate_series(2001,2010) g, LATERAL (SELECT v FROM fd WHERE fd.id = g) x), "
            "(SELECT pgcolumnar.compact('fd'::regclass) >= 1), "
            "(SELECT count(*) FROM generate_series(1,2000) g, LATERAL (SELECT v FROM fd WHERE fd.id = g) x)"],
            ("SET pgcolumnar.reclaim_coalesce=off",) + FORCE, commit=True)
        _await_up(dsn)
        r["r exact"] = _value(dsn, "SELECT count(*) FROM r")
        r["unique"] = _unique_violation(dsn)
        _await_up(dsn)
        r["trigger"] = _trigger_old(dsn)
        _await_up(dsn)
        r["delete"] = _in_tx(dsn, [
            "DELETE FROM f WHERE id = 77",
            "SELECT count(x.v) FROM (VALUES (77)) t(p), LATERAL (SELECT v FROM f WHERE f.id = t.p) x"],
            FORCE, commit=True)
        _await_up(dsn)
        r["truncate"] = _truncate_then_fetch(dsn)
        r["alive"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        _setup(pgc_cluster.dsn(), [f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)"])


def test_fgm_measured_plan_is_a_nested_loop_of_index_probes(fm, expect):
    expect.text(fm["plan"], "1/1", "the measured plan is a nested loop of index probes (fetch path engaged)")


def test_fgm_the_fetch_count_is_exact(fm, expect):
    expect.text(fm["count"], "1", "the fetch count is exact (3000 rows found)")


def test_fgm_index_fetches_cost_bounded_row_group_reads(fm, expect):
    expect.text(fm["bounded"], "bounded",
                "3000 index fetches cost a bounded number of row_group reads (delta <= 20)")


def test_fgm_row_group_is_never_heap_scanned(fm, expect):
    expect.text(fm["heap"], "0", "row_group is never heap-scanned in the window (index resolution intact)")


def test_fgm_scattered_fetched_values_are_exact(fm, expect):
    expect.text(fm["scattered"], "2000", "scattered index-fetched values are exact")


def test_fgm_same_transaction_flush_then_fetch(fm, expect):
    expect.text(fm["same tx flush"], "50", "same-transaction flush then fetch sees the new rows")


def test_fgm_aborted_savepoint_rows_do_not_resurrect(fm, expect):
    expect.text(fm["savepoint"], "MOVE 99990",
                "rows flushed under an aborted savepoint do not resurrect through the cursor")


def test_fgm_same_statement_retire_neither_loses_nor_doubles(fm, expect):
    expect.text(fm["retire"], "500|t|500", "a same-statement retire neither loses nor doubles rows (500|t|500)")


def test_fgm_same_statement_compact_resurrects_nothing(fm, expect):
    expect.text(fm["compact"], "10|t|0",
                "a same-statement compact() of a fully deleted group resurrects nothing (10|t|0)")


def test_fgm_table_is_exact_after_the_rewrite(fm, expect):
    expect.text(fm["r exact"], "9000", "the table is exact after the in-statement rewrite")


def test_fgm_duplicate_still_violates_a_unique_index(fm, expect):
    expect.text(fm["unique"], "1", "a buffered + flushed duplicate still violates a unique index (23505)")


def test_fgm_after_update_trigger_sees_the_old_row(fm, expect):
    expect.text(fm["trigger"], "1", "an AFTER UPDATE trigger still sees the OLD row (SnapshotAny re-fetch)")


def test_fgm_delete_visible_to_the_next_fetch(fm, expect):
    expect.text(fm["delete"], "0", "a DELETE is visible to the very next fetch (delete vectors uncached)")


def test_fgm_same_transaction_truncate_then_fetch(fm, expect):
    expect.text(fm["truncate"], "50/100/100/", "same-transaction TRUNCATE then fetch sees only the new rows")


def test_fgm_backend_alive(fm, expect):
    expect.text(fm["alive"], "1", "backend alive")

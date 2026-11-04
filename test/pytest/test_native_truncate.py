"""Physical end-truncation (Phase F). The pytest twin of `test/native_truncate.sh`,
every check by the shell's name.

pgcolumnar.truncate() returns trailing reclaimed blocks to the OS. After a delete and a
compaction the file shrinks, the rows still match a heap mirror, a second truncate has
nothing to do, and the table stays usable: reinsert, index, indexed lookup. The GUC
turns it off. It refuses to run inside a transaction block, since its shrink is not
transactional, and a truncate followed by reinsert and compaction stays parity-clean
(the assert build's no-overlap validator runs inside compact). Concurrency is the
isolation specs' (specs/truncate_vs_*.spec).
"""
import psycopg
import pytest

from pgc_horizon import freed_range_blockers, retry_past_horizon, wait_freed_range_past_horizon
from pgc_vacuity import query_error

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
IDX_SETS = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off")
LOOKUP = "SELECT count(v) FROM n WHERE id BETWEEN 1 AND 40000"


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _size(c):
    return int(c.execute("SELECT pg_relation_size('n')").fetchone()[0])


def _hashes(c):
    """-> (columnar, heap) set digests of the rows."""
    return _one(c, HASH.format("SELECT id, v FROM n")), _one(c, HASH.format("SELECT id, v FROM h"))


def _both(c, *sqls):
    """Run each statement on the heap mirror and on the columnar table."""
    for sql in sqls:
        c.execute(sql.format(t="h"))
        c.execute(sql.format(t="n"))


def _truncated(c):
    """Blocks truncate returned, waiting for the horizon (#1979): truncate removes a
    freed range only when no transaction is older than the compact that freed it."""
    return retry_past_horizon(c, "SELECT pgcolumnar.truncate('n')", 1)[0]


def _compacted(c, want):
    """Groups compact retired, waiting for the horizon (#1979): compact retires a
    dead group only when no transaction is older than the DELETE. With one call, a
    snapshot held by another session left nothing to truncate."""
    retired, calls = retry_past_horizon(c, "SELECT pgcolumnar.compact('n')", want)
    print(f"-- compact retired {retired} of {want} groups in {calls} calls")
    return retired


@pytest.fixture(scope="module")
def nt(pgc_cluster):
    db = "pgc_ntrunc"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
        # end-truncation is opt-in (GUC default off); on for the whole database
        a.execute(f"ALTER DATABASE {db} SET pgcolumnar.enable_end_truncation = on")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            # no autovacuum on the fixture (#1979): it would retire the dead groups itself
            c.execute("CREATE TABLE h (id int, v text) WITH (autovacuum_enabled = false)")
            c.execute("CREATE TABLE n (id int, v text) USING pgcolumnar WITH (autovacuum_enabled = false)")
            c.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
            _both(c, "INSERT INTO {t} SELECT g, md5(g::text) FROM generate_series(1, 30000) g")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


# No other session may lock n when a check wants 0 (#1994). truncate returns 0, and
# leaves the file alone, when it cannot take its ACCESS EXCLUSIVE lock on n at once:
# while another session holds a lock weaker than SHARE UPDATE EXCLUSIVE on n, or
# waits for any lock on it. Then a check that wants 0 passed whatever truncate would
# have done. So each such call runs through _truncate_alone, and its lock counts are
# checked as well.
LOCKS_ON_N = """
SELECT count(*) FROM pg_locks
 WHERE locktype = 'relation' AND relation = 'n'::regclass
   AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
   AND pid <> pg_backend_pid()
"""


def _truncate_alone(c):
    """-> ("before|after", answer): the locks other sessions hold or wait for on n,
    counted just before and just after truncate on n, and what truncate returned."""
    before = _one(c, LOCKS_ON_N)
    out = _one(c, "SELECT pgcolumnar.truncate('n')")
    after = _one(c, LOCKS_ON_N)
    return f"{before}|{after}", out


def test_truncate_shrinks_the_file_and_keeps_every_row(nt, expect):
    c = nt
    expect.text(*_hashes(c), "load parity")
    # nothing has been freed yet
    locks, out = _truncate_alone(c)
    expect.text(locks, "0|0", "truncate before any delete: no other session held a lock on n before or after the call")
    expect.text(out, "0", "truncate before any delete is a no-op")

    # the trailing half deleted whole (groups 16..30), and compacted to free it
    _both(c, "DELETE FROM {t} WHERE id > 15000")
    expect.num(_compacted(c, 15), 15, "compact retired the 15 trailing groups")
    before = _size(c)
    trunc = _truncated(c)
    print(f"-- truncate returned {trunc} blocks; before-trunc={before} after={_size(c)}")
    expect.at_least(trunc, 1, "truncate returned blocks")
    expect.at_least(before - _size(c), 1, "file shrank")
    expect.text(*_hashes(c), "parity after truncate")
    expect.text(_one(c, "SELECT count(*) FROM n"), "15000", "row count after truncate")
    locks, out = _truncate_alone(c)
    expect.text(locks, "0|0", "second truncate: no other session held a lock on n before or after the call")
    expect.text(out, "0", "second truncate is a no-op")


XDB_PREMISE = """
WITH f AS (SELECT min(age(freed_xid::text::xid)) AS a FROM pgcolumnar.free_space
            WHERE storage_id = pgcolumnar.get_storage_id('n') AND freed_xid >= 3)
SELECT count(*) FROM pg_stat_activity, f
 WHERE application_name = 'nt_xdb_holder' AND datname <> current_database()
   AND age(backend_xmin) >= f.a
"""


def test_the_table_stays_usable_and_the_guc_turns_it_off(nt, pgc_cluster, expect):
    c = nt
    _both(c, "INSERT INTO {t} SELECT g, md5(g::text) FROM generate_series(30001, 40000) g")
    expect.text(*_hashes(c), "parity after post-truncate insert")
    c.execute("CREATE INDEX n_id ON n (id)")
    # forced to fetch every row through n_id, and asserted: by default this count is an
    # Index Only Scan, which stops fetching once a VACUUM marks the pages all-visible
    for s in IDX_SETS:
        c.execute(s)
    try:
        plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {LOOKUP}"))
        expect.text("yes" if "Index Scan using n_id" in plan else "no", "yes", "premise: the indexed lookup reads n_id")
        expect.text(_one(c, LOOKUP), "25000", "indexed lookup after truncate")
    finally:
        for g in ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan"):
            c.execute(f"RESET {g}")

    _both(c, "DELETE FROM {t} WHERE id > 35000")
    # a snapshot in another database (#1992): truncate's horizon for a user table
    # ignores a snapshot with no transaction ID there, so it must not count as
    # holding the freed range (a writer there does hold it: see the last test). It stays open
    # until after the GUC-on truncate, which shows that truncate is not held.
    hold = psycopg.connect(pgc_cluster.dsn(), autocommit=True, application_name="nt_xdb_holder")
    try:
        hold.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        hold.execute("SELECT 1")
        _guc_cycle(c, expect)
    finally:
        hold.close()
    expect.text(*_hashes(c), "parity after guard cycle")
    # the helper refuses a table with no freed range (#1992): with no freed_xid to
    # compare against the count would read 0, a pass that measured nothing
    c.execute("CREATE TABLE nf (id int) USING pgcolumnar WITH (autovacuum_enabled = false)")
    n, calls = wait_freed_range_past_horizon(c, "nf")
    expect.text(f"{'none' if n is None else n}/{calls}", "none/1",
                "a table with no freed range gives the horizon helper no count")


def _guc_cycle(c, expect):
    """The GUC-off and GUC-on truncate calls, with a snapshot open in another database."""
    expect.num(_compacted(c, 5), 5, "compact retired the 5 groups above id 35000")
    expect.num(int(c.execute(XDB_PREMISE).fetchone()[0]), 1,
               "premise: a snapshot older than the compact is open in another database")
    guard = _size(c)
    # wait for the horizon first (#1988): truncate also leaves the file alone while a
    # transaction older than the compact is open, and the arm then passed whatever
    # the setting said
    blockers, calls = wait_freed_range_past_horizon(c, "n")
    print(f"-- horizon blockers/calls before the GUC-off call: {blockers}/{calls}")
    expect.num(blockers, 0, "GUC off: no transaction older than the compact was open")
    # its own statement, and its answer checked: the shell once sent SET and the call
    # as one string, an implicit transaction block that truncate refuses
    # and no other session may lock n (#1994): see _truncate_alone
    c.execute("SET pgcolumnar.enable_end_truncation = off")
    try:
        locks, off = _truncate_alone(c)
    finally:
        c.execute("RESET pgcolumnar.enable_end_truncation")
    expect.text(locks, "0|0", "GUC off: no other session held a lock on n before or after the call")
    expect.text(f"{_size(c)}|{off}", f"{guard}|0", "GUC off: truncate does nothing")
    expect.at_least(_truncated(c), 1, "GUC on: truncate reclaims")
    expect.at_least(guard - _size(c), 1, "GUC on: file shrank")
    expect.num(int(c.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'nt_xdb_holder'"
                             " AND backend_xmin IS NOT NULL").fetchone()[0]), 1,
               "the other-database snapshot was still open after the GUC-on truncate")


def test_truncate_refuses_a_transaction_block_and_reuse_stays_clean(nt, expect):
    c = nt
    # a ROLLBACK must never leave the metapage highwater lowered while the
    # transactional free_space deletes roll back
    _both(c, "DELETE FROM {t} WHERE id BETWEEN 30001 AND 33000")
    expect.num(_compacted(c, 3), 3, "compact retired the 3 groups from id 30001 to 33000")
    before = _size(c)
    refused = "no"
    c.execute("BEGIN")
    try:
        c.execute("SELECT pgcolumnar.truncate('n')")
    except psycopg.Error as exc:
        if "cannot run inside a transaction block" in (exc.diag.message_primary or ""):
            refused = "yes"
    finally:
        c.execute("ROLLBACK")
    expect.text(refused, "yes", "truncate refused inside a transaction block")
    expect.num(_size(c), before, "file unchanged after refused in-txn truncate")
    expect.text(*_hashes(c), "parity after refused in-txn truncate")

    # an autocommit truncate, reinsert into the reclaimed space, then compaction: no
    # double allocation over reused blocks
    c.execute("SELECT pgcolumnar.truncate('n')")
    _both(c, "INSERT INTO {t} SELECT g, md5(g::text) FROM generate_series(41001, 43000) g",
          "DELETE FROM {t} WHERE id > 42000")
    c.execute("SELECT pgcolumnar.compact_rewrite('n', 0.0)")
    c.execute("SELECT pgcolumnar.compact('n')")
    expect.text(*_hashes(c), "parity after truncate + reinsert + compaction")


def _xw_cycle(c, upto, hold, release):
    """DELETE the rows of xw above `upto`, open a holder with `hold()`, compact, and
    read truncate's answer and the helper's count while it is open. Then end it with
    `release(holder)` and truncate again, waiting for the horizon.
    -> (truncate while held, helper count while held, blocks truncated after)."""
    c.execute(f"DELETE FROM xw WHERE id > {upto}")
    holder = hold()
    try:
        retired, calls = retry_past_horizon(c, "SELECT pgcolumnar.compact('xw')", 3)
        print(f"-- compact of xw retired/calls: {retired}/{calls}")
        held = _one(c, "SELECT pgcolumnar.truncate('xw')")
        n = freed_range_blockers(c, "xw")
        print(f"-- horizon blockers of xw: {n}")
    finally:
        release(holder)
    after, calls = retry_past_horizon(c, "SELECT pgcolumnar.truncate('xw')", 1)
    print(f"-- truncate of xw returned/calls: {after}/{calls}")
    return held, n, after


def _counted(n):
    return "yes" if n is not None and n >= 1 else f"no ({'none' if n is None else n})"


def test_the_horizon_helper_counts_what_holds_truncate(nt, pgc_cluster, expect):
    """truncate computes its horizon with its own snapshot, and that snapshot's xmin
    is the oldest transaction ID running in ANY database (#1992). So a writer or a
    prepared transaction in another database holds truncate back; only a snapshot
    with no transaction ID there does not. A snapshot in this database taken just
    before the compact holds it too: its xmin is the compact's own transaction ID."""
    c = nt
    c.execute("CREATE TABLE xw (id int) USING pgcolumnar WITH (autovacuum_enabled = false)")
    c.execute("SELECT pgcolumnar.set_options('xw', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
    c.execute("INSERT INTO xw SELECT g FROM generate_series(1, 12000) g")

    def writer():
        w = psycopg.connect(pgc_cluster.dsn(), autocommit=True, application_name="nt_xdb_writer")
        w.execute("BEGIN")
        w.execute("SELECT txid_current()")
        return w

    held, n, after = _xw_cycle(c, 9000, writer, lambda w: w.close())
    expect.text(held, "0", "premise: truncate is held while a writer in another database is open")
    expect.text(_counted(n), "yes", "the horizon helper counts a writer in another database")
    expect.at_least(after, 1, "truncate reclaims once the writer in another database ends")

    def prepared():
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as p:
            p.execute("BEGIN")
            p.execute("SELECT txid_current()")
            p.execute("PREPARE TRANSACTION 'nt_xdb_prep'")
        return "nt_xdb_prep"

    def rollback_prepared(gid):
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as p:
            p.execute(f"ROLLBACK PREPARED '{gid}'")

    held, n, after = _xw_cycle(c, 6000, prepared, rollback_prepared)
    expect.text(held, "0", "premise: truncate is held while a prepared transaction in another database is open")
    expect.text(_counted(n), "yes", "the horizon helper counts a prepared transaction in another database")
    expect.at_least(after, 1, "truncate reclaims once the prepared transaction in another database ends")

    def reader():
        r = psycopg.connect(pgc_cluster.dsn(dbname="pgc_ntrunc"), autocommit=True,
                            application_name="nt_samedb_reader")
        r.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        r.execute("SELECT 1")
        return r

    held, n, after = _xw_cycle(c, 3000, reader, lambda r: r.close())
    expect.text(held, "0", "premise: truncate is held while a snapshot from just before the compact is open")
    expect.text(_counted(n), "yes", "the horizon helper counts a snapshot from just before the compact")
    expect.at_least(after, 1, "truncate reclaims once the snapshot from just before the compact ends")

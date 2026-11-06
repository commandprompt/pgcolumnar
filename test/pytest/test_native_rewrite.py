"""Online rewrite of partially-deleted groups, and what vacuum's full rewrite must keep.

The pytest twin of `test/native_rewrite.sh`, every check by the shell's name.

pgcolumnar.compact_rewrite() rewrites groups whose deleted fraction exceeds a
threshold into fresh groups with fresh row numbers, online under
ShareUpdateExclusiveLock. Asserted: results match a heap mirror written from the
generator; the dead rows' space is reclaimed; the threshold holds from both sides (a
rewrite with nothing deleted, and a group deleted below the threshold, rewrite no
group); index scans return each live row once and point lookups are right; the
duplicate is refused by n_id_uq itself; the lock is ShareUpdateExclusiveLock, never
AccessExclusiveLock; group numbers are never reused; and pgcolumnar.vacuum rewrites
into fresh storage with every row reachable, through the index as well.

A FAILED READING IS NEVER AGREEMENT. Counts are compared as numbers, so two failed
reads no longer agree, and "the duplicate was refused" wants 23505 and n_id_uq's own
message: any failure used to count, so with the server stopped, four checks passed.

THE INDEX AFTER VACUUM IS READ OVER A NARROW RANGE. A stale index entry still fetches
a row, just the wrong one, and over the whole table the stale entries land on every
live row once, so a count and a hash through the index both matched with the rebuild
skipped. A range of eleven ids is what tells.

ONE CHECK PER TEST (#1339). One module fixture takes every reading in the shell's
order, through `_try`, and each test asserts one reading.
"""

import hashlib

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

SCHEMA = "pgc_native_rewrite"
GEN = ("SELECT g, (g % 1000) AS v, 'p' || (g % 100) AS payload "
       "FROM generate_series(1, 8192) g")
EXTRA = "SELECT g + 1000000, g % 1000, 'p' || (g % 100) FROM generate_series(1, 2000) g"
FORCE_INDEX = ("SET enable_seqscan = off", "SET enable_bitmapscan = off",
               "SET pgcolumnar.enable_custom_scan = off")
DUP = 'duplicate key value violates unique constraint "n_id_uq"'


def _message(exc):
    if exc.diag.message_primary:
        return exc.diag.message_primary
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _try(fn, *args, **kw):
    """A reading, or the failure as a query_error(), so a failed step reddens its own
    check rather than erroring the fixture and every check with it (#1522)."""
    try:
        return fn(*args, **kw)
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _one(conn, sql):
    """-> the first column of the last row as text ('' for NULL), as the shell's q."""
    rows = conn.execute(sql).fetchall()
    if not rows:
        return query_error(f"no row from {sql}")
    v = rows[-1][0]
    return "" if v is None else str(v)


def _read(conn, sql):
    return _try(_one, conn, sql)


def _set_hash(conn, sql):
    """-> an order-free hash of the rows, or a failed query: never equal to another failure."""
    def run():
        rows = sorted(repr(r) for r in conn.execute(sql).fetchall())
        return f"{len(rows)}:{hashlib.md5(chr(10).join(rows).encode()).hexdigest()}"
    return _try(run)


def _forced(dsn, sql):
    """-> sql's first column read with the index forced, on its own session, as the
    shell's idxcount/idxids."""
    def run():
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"SET search_path = {SCHEMA}, public")
            for s in FORCE_INDEX:
                c.execute(s)
            return _one(c, sql)
    return _try(run)


def _dupfails(dsn):
    """-> "yes" only when the duplicate is refused by n_id_uq (23505 and its message)."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"SET search_path = {SCHEMA}, public")
            c.execute("INSERT INTO n VALUES (4097, 0, 'x')")
    except psycopg.Error as exc:
        if exc.sqlstate == "23505" and DUP in str(exc):
            return "yes"
        return f"no ({exc.sqlstate} {_message(exc)})"
    return "no (the duplicate was inserted)"


def _locks(dsn):
    """-> "SUE|AE": the relation locks compact_rewrite holds on n inside its transaction."""
    def run():
        with psycopg.connect(dsn) as c:
            c.execute(f"SET search_path = {SCHEMA}, public")
            c.execute("SELECT pgcolumnar.compact_rewrite('n', 0.0)")
            v = _one(c, "SELECT count(*) FILTER (WHERE mode='ShareUpdateExclusiveLock')::text || '|' || "
                        "count(*) FILTER (WHERE mode='AccessExclusiveLock')::text "
                        "FROM pg_locks WHERE relation='n'::regclass AND locktype='relation'")
            c.rollback()
            return v
    return _try(run)


def _int(v):
    return int(v) if isinstance(v, str) and v.lstrip("-").isdigit() else v


def _gt_zero(v):
    v = _int(v)
    return "yes" if isinstance(v, int) and v > 0 else "no"


@pytest.fixture(scope="module")
def nr(pgc_cluster):
    dsn = pgc_cluster.dsn()
    conn = psycopg.connect(dsn, autocommit=True)
    r = {}
    try:
        _try(conn.execute, f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        _try(conn.execute, f"CREATE SCHEMA {SCHEMA}")
        _try(conn.execute, f"SET search_path = {SCHEMA}, public")
        for sql in ("CREATE TABLE h (id int, v int, payload text)",
                    "CREATE TABLE n (id int, v int, payload text) USING pgcolumnar",
                    "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1024, chunk_group_row_limit => 1024)",
                    f"INSERT INTO h {GEN}", f"INSERT INTO n {GEN}",
                    "CREATE UNIQUE INDEX n_id_uq ON n (id)", "CREATE INDEX n_v ON n (v)",
                    "DELETE FROM h WHERE id % 3 = 0", "DELETE FROM n WHERE id % 3 = 0"):
            _try(conn.execute, sql)

        def deletedrows():
            return _read(conn, "SELECT COALESCE(sum(deletedrows), 0) FROM pgcolumnar.stats('n'::regclass)")

        def counts():
            return (_read(conn, "SELECT count(*) FROM n"), _read(conn, "SELECT count(*) FROM h"))

        def parity():
            return (_set_hash(conn, "SELECT id, v, payload FROM n ORDER BY id"),
                    _set_hash(conn, "SELECT id, v, payload FROM h ORDER BY id"))

        r["count after delete"] = counts()
        r["deleted rows"] = _gt_zero(deletedrows())
        r["parity after delete"] = parity()

        r["rewrote"] = _gt_zero(_read(conn, "SELECT pgcolumnar.compact_rewrite('n', 0.2)"))
        r["reclaimed"] = deletedrows()
        r["parity after rewrite"] = parity()
        r["count after rewrite"] = counts()

        r["second rewrite"] = _read(conn, "SELECT pgcolumnar.compact_rewrite('n', 0.2)")
        _try(conn.execute, "DELETE FROM h WHERE id = 2")
        _try(conn.execute, "DELETE FROM n WHERE id = 2")
        r["under threshold"] = _read(conn, "SELECT pgcolumnar.compact_rewrite('n', 0.2)")

        r["index count"] = (_forced(dsn, "SELECT count(*) FROM n WHERE id BETWEEN 1 AND 8192"),
                            _read(conn, "SELECT count(*) FROM h"))
        r["live id"] = _read(conn, "SELECT count(*) FROM n WHERE id = 4097")
        r["deleted id"] = _read(conn, "SELECT count(*) FROM n WHERE id = 4098")
        r["dup"] = _dupfails(dsn)

        locks = _locks(dsn)
        if isinstance(locks, str) and "|" in locks and not locks.startswith(QUERY_ERROR):
            r["sue"], r["ae"] = locks.split("|", 1)
        else:
            r["sue"] = r["ae"] = locks

        sid_q = "pgcolumnar.get_storage_id('n')"
        before_max = _read(conn, f"SELECT coalesce(max(group_number), -1) FROM pgcolumnar.row_group "
                                 f"WHERE storage_id = {sid_q}")
        _try(conn.execute, "SELECT pgcolumnar.compact_rewrite('n', 0.1)")
        _try(conn.execute, f"INSERT INTO n {EXTRA}")
        _try(conn.execute, f"INSERT INTO h {EXTRA}")
        bm = _int(before_max)
        after_min_new = _read(conn, f"SELECT coalesce(min(group_number), -1) FROM pgcolumnar.row_group "
                                    f"WHERE storage_id = {sid_q} AND group_number > {bm if isinstance(bm, int) else -1}")
        r["no reuse"] = _read(conn, f"SELECT (count(*) = count(DISTINCT group_number))::text FROM pgcolumnar.row_group "
                                    f"WHERE storage_id = {sid_q}")
        r["no reuse"] = {"true": "t", "false": "f"}.get(r["no reuse"], r["no reuse"])
        am = _int(after_min_new)
        r["past earlier"] = "yes" if isinstance(am, int) and isinstance(bm, int) and am > bm else "no"

        sid_before = _read(conn, f"SELECT {sid_q}")
        _try(conn.execute, "SELECT pgcolumnar.vacuum('n')")
        sid_after = _read(conn, f"SELECT {sid_q}")
        r["fresh storage"] = ("yes" if not str(sid_before).startswith(QUERY_ERROR)
                              and not str(sid_after).startswith(QUERY_ERROR) and sid_before != sid_after else "no")
        r["count after vacuum"] = counts()
        r["index range after vacuum"] = (
            _forced(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) FROM n WHERE id BETWEEN 4090 AND 4100"),
            _read(conn, "SELECT string_agg(id::text, ',' ORDER BY id) FROM h WHERE id BETWEEN 4090 AND 4100"))
        yield r
    finally:
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        except psycopg.Error:
            pass
        conn.close()


def test_row_count_after_delete(nr, expect):
    got, want = nr["count after delete"]
    expect.num(_int(got), _int(want), "row count after delete")


def test_there_are_deleted_rows_to_reclaim(nr, expect):
    expect.text(nr["deleted rows"], "yes", "there are deleted rows to reclaim")


def test_parity_after_delete(nr, expect):
    got, want = nr["parity after delete"]
    expect.text(got, want, "parity after delete")


def test_rewrote_some_groups(nr, expect):
    expect.text(nr["rewrote"], "yes", "rewrote some groups")


def test_dead_row_space_reclaimed(nr, expect):
    expect.text(nr["reclaimed"], "0", "dead-row space reclaimed")


def test_parity_after_rewrite(nr, expect):
    got, want = nr["parity after rewrite"]
    expect.text(got, want, "parity after rewrite")


def test_row_count_after_rewrite(nr, expect):
    got, want = nr["count after rewrite"]
    expect.num(_int(got), _int(want), "row count after rewrite")


def test_a_second_rewrite_with_nothing_deleted_rewrites_no_group(nr, expect):
    expect.num(_int(nr["second rewrite"]), 0, "a second rewrite with nothing deleted rewrites no group")


def test_a_group_deleted_below_the_threshold_is_not_rewritten(nr, expect):
    expect.num(_int(nr["under threshold"]), 0, "a group deleted below the threshold is not rewritten")


def test_index_scan_returns_each_live_row_once(nr, expect):
    got, want = nr["index count"]
    expect.num(_int(got), _int(want), "index scan returns each live row once")


def test_live_id_present_via_index(nr, expect):
    expect.text(nr["live id"], "1", "live id present via index")


def test_deleted_id_absent_via_index(nr, expect):
    expect.text(nr["deleted id"], "0", "deleted id absent via index")


def test_unique_constraint_still_enforced(nr, expect):
    expect.text(nr["dup"], "yes", "unique constraint still enforced")


def test_compact_rewrite_holds_share_update_exclusive_lock(nr, expect):
    expect.text(nr["sue"], "1", "compact_rewrite holds ShareUpdateExclusiveLock")


def test_compact_rewrite_holds_no_access_exclusive_lock(nr, expect):
    expect.text(nr["ae"], "0", "compact_rewrite holds no AccessExclusiveLock")


def test_a_rewrite_plus_later_writes_reuse_no_group_number(nr, expect):
    expect.text(nr["no reuse"], "t", "a rewrite plus later writes reuse no group number")


def test_group_numbers_issued_after_a_rewrite_are_past_every_earlier_one(nr, expect):
    expect.text(nr["past earlier"], "yes", "group numbers issued after a rewrite are past every earlier one")


def test_vacuum_rewrites_into_a_fresh_storage_id(nr, expect):
    expect.text(nr["fresh storage"], "yes", "vacuum rewrites into a fresh storage id")


def test_every_live_row_still_reads_back_after_rewrite_and_vacuum(nr, expect):
    got, want = nr["count after vacuum"]
    expect.num(_int(got), _int(want), "every live row still reads back after rewrite and vacuum")


def test_an_index_range_read_after_vacuum_returns_the_rows_asked_for(nr, expect):
    got, want = nr["index range after vacuum"]
    expect.text(got, want, "an index range read after vacuum returns the rows asked for")

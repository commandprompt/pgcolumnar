"""Online compaction, the lazy path (Phase F3a). The pytest twin of
`test/native_compact.sh`, every check by the shell's name (#1341).

pgcolumnar.compact() retires row groups that are fully deleted as of the oldest-xmin
horizon, dropping their catalog rows without rewriting data and without
AccessExclusiveLock. Asserted, against a heap mirror built from the same generator:
results match after deletes and after compact; two fully deleted groups are retired
and a half-deleted one is kept; a second compact retires nothing; a group deleted
while an older REPEATABLE READ snapshot can still see it is KEPT until that snapshot
ends, and that snapshot still reads its rows; and compact holds
ShareUpdateExclusiveLock, never AccessExclusiveLock.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). A reading that fails is a failed reading, never a value.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_compact"
GEN = ("SELECT g, (g % 100) AS v, 'p' || (g % 50) AS payload "
       "FROM generate_series(1, 8192) g")
GROUPS = "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('n')"
COMPACT = "SELECT pgcolumnar.compact('n')"
GROUP6 = "SELECT count(*) FROM n WHERE id BETWEEN 6145 AND 7168"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql, options=""):
    """-> the first column of the first row, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=options) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _set_hash(dsn, query):
    """-> the shell's pgc_set_hash: md5 of the sorted row texts, EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _locks(dsn):
    """-> (ShareUpdateExclusiveLock count, AccessExclusiveLock count) on n while a
    compact's transaction is still open, or query_error()s."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("BEGIN")
            conn.execute(COMPACT)
            row = conn.execute(
                "SELECT count(*) FILTER (WHERE mode = 'ShareUpdateExclusiveLock'), "
                "count(*) FILTER (WHERE mode = 'AccessExclusiveLock') "
                "FROM pg_locks WHERE relation = 'n'::regclass AND locktype = 'relation'").fetchone()
            conn.execute("ROLLBACK")
    except psycopg.Error as exc:
        err = query_error(_message(exc))
        return err, err
    return row[0], row[1]


@pytest.fixture(scope="module")
def nc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE h (id int, v int, payload text)")
        _run(dsn, "CREATE TABLE n (id int, v int, payload text) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1024, chunk_group_row_limit => 1024)")
        _run(dsn, f"INSERT INTO h {GEN}")
        _run(dsn, f"INSERT INTO n {GEN}")
        r = {"rows": _value(dsn, "SELECT count(*) FROM n"), "groups0": _value(dsn, GROUPS)}

        for t in ("h", "n"):
            _run(dsn, f"DELETE FROM {t} WHERE id BETWEEN 2049 AND 4096")
        for t in ("h", "n"):
            _run(dsn, f"DELETE FROM {t} WHERE id BETWEEN 5121 AND 5632")
        r["parity deletes"] = (_set_hash(dsn, "SELECT id, v, payload FROM n ORDER BY id"),
                               _set_hash(dsn, "SELECT id, v, payload FROM h ORDER BY id"))

        r["retired"] = _value(dsn, COMPACT)
        r["groups1"] = _value(dsn, GROUPS)
        r["parity compact"] = (_set_hash(dsn, "SELECT id, v, payload FROM n ORDER BY id"),
                               _set_hash(dsn, "SELECT id, v, payload FROM h ORDER BY id"))
        r["live"] = (_value(dsn, "SELECT count(*) FROM n"), _value(dsn, "SELECT count(*) FROM h"))
        r["partial"] = (_value(dsn, "SELECT count(*) FROM n WHERE id BETWEEN 4097 AND 6144"),
                        _value(dsn, "SELECT count(*) FROM h WHERE id BETWEEN 4097 AND 6144"))
        r["second"] = _value(dsn, COMPACT)

        # the horizon: a REPEATABLE READ snapshot taken before group 6 is deleted
        holder = None
        try:
            holder = psycopg.connect(dsn, autocommit=True)
            holder.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            r["before"] = f"before:{holder.execute(GROUP6).fetchone()[0]}"
            r["held"] = 1
        except psycopg.Error as exc:
            r["before"] = query_error(_message(exc))
            r["held"] = query_error(_message(exc))
        for t in ("h", "n"):
            _run(dsn, f"DELETE FROM {t} WHERE id BETWEEN 6145 AND 7168")
        # bounded as in the shell: a compact that wanted a stronger lock waits on the holder
        r["held compact"] = _value(dsn, COMPACT, options="-c lock_timeout=10s")
        r["held groups"] = _value(dsn, GROUPS)
        try:
            if holder is None:
                raise psycopg.OperationalError("the holder never connected")
            r["after"] = f"after:{holder.execute(GROUP6).fetchone()[0]}"
            holder.execute("COMMIT")
        except psycopg.Error as exc:
            r["after"] = query_error(_message(exc))
        finally:
            if holder is not None:
                holder.close()
        r["released compact"] = _value(dsn, COMPACT)
        r["released groups"] = _value(dsn, GROUPS)
        r["parity held"] = (_set_hash(dsn, "SELECT id, v, payload FROM n ORDER BY id"),
                            _set_hash(dsn, "SELECT id, v, payload FROM h ORDER BY id"))

        r["suel"], r["ael"] = _locks(dsn)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_compacted_table_holds_every_row(nc, expect):
    expect.text(str(nc["rows"]), "8192", "row count")


def test_the_table_starts_in_eight_groups(nc, expect):
    expect.text(str(nc["groups0"]), "8", "initial group count")


def test_the_table_agrees_with_its_mirror_after_the_deletes(nc, expect):
    expect.text(nc["parity deletes"][0], nc["parity deletes"][1], "parity after deletes")


def test_compact_retires_the_two_fully_deleted_groups(nc, expect):
    expect.text(str(nc["retired"]), "2", "compact retired the two fully-deleted groups")


def test_the_group_count_drops_by_two(nc, expect):
    expect.text(str(nc["groups1"]), "6", "group count dropped by two")


def test_the_table_agrees_with_its_mirror_after_compact(nc, expect):
    expect.text(nc["parity compact"][0], nc["parity compact"][1], "parity after compact")


def test_the_live_row_count_survives_compact(nc, expect):
    expect.num(nc["live"][0], nc["live"][1], "live row count after compact")


def test_the_half_deleted_groups_live_rows_survive_compact(nc, expect):
    expect.num(nc["partial"][0], nc["partial"][1], "partial-group rows still present")


def test_a_second_compact_retires_nothing(nc, expect):
    expect.text(str(nc["second"]), "0", "second compact retires nothing")


def test_the_holders_snapshot_is_open(nc, expect):
    expect.text(str(nc["held"]), "1", "premise: the holder's snapshot is open")


def test_compact_keeps_a_group_an_older_snapshot_can_see(nc, expect):
    expect.num(nc["held compact"], 0, "compact keeps a group an older snapshot can still see")


def test_the_group_count_holds_while_the_snapshot_lives(nc, expect):
    expect.num(nc["held groups"], 6, "and the group count stays at six while that snapshot lives")


def test_the_older_snapshot_read_the_group_before_the_delete(nc, expect):
    expect.text(nc["before"], "before:1024", "the older snapshot read the group's rows before the delete")


def test_the_older_snapshot_still_reads_the_group_after_compact(nc, expect):
    expect.text(nc["after"], "after:1024", "and still reads all of them after the delete and the compact")


def test_compact_retires_the_group_once_the_snapshot_ends(nc, expect):
    expect.num(nc["released compact"], 1, "once that snapshot ends, compact retires the group")


def test_the_group_count_drops_to_five(nc, expect):
    expect.num(nc["released groups"], 5, "and the group count drops to five")


def test_the_table_agrees_with_its_mirror_after_the_held_compact(nc, expect):
    expect.text(nc["parity held"][0], nc["parity held"][1], "parity after the held-snapshot compact")


def test_compact_holds_share_update_exclusive_lock(nc, expect):
    expect.text(str(nc["suel"]), "1", "compact holds ShareUpdateExclusiveLock")


def test_compact_holds_no_access_exclusive_lock(nc, expect):
    expect.text(str(nc["ael"]), "0", "compact holds no AccessExclusiveLock")

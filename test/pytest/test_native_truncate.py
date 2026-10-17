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

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
IDX_SETS = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off")
LOOKUP = "SELECT count(v) FROM n WHERE id BETWEEN 1 AND 40000"


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


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
    return int(c.execute("SELECT pgcolumnar.truncate('n')").fetchone()[0])


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
            c.execute("CREATE TABLE h (id int, v text)")
            c.execute("CREATE TABLE n (id int, v text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
            _both(c, "INSERT INTO {t} SELECT g, md5(g::text) FROM generate_series(1, 30000) g")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_truncate_shrinks_the_file_and_keeps_every_row(nt, expect):
    c = nt
    expect.text(*_hashes(c), "load parity")
    # nothing has been freed yet
    expect.text(_one(c, "SELECT pgcolumnar.truncate('n')"), "0", "truncate before any delete is a no-op")

    # the trailing half deleted whole (groups 16..30), and compacted to free it
    _both(c, "DELETE FROM {t} WHERE id > 15000")
    c.execute("SELECT pgcolumnar.compact('n')")
    before = _size(c)
    trunc = _truncated(c)
    print(f"-- truncate returned {trunc} blocks; before-trunc={before} after={_size(c)}")
    expect.at_least(trunc, 1, "truncate returned blocks")
    expect.at_least(before - _size(c), 1, "file shrank")
    expect.text(*_hashes(c), "parity after truncate")
    expect.text(_one(c, "SELECT count(*) FROM n"), "15000", "row count after truncate")
    expect.text(_one(c, "SELECT pgcolumnar.truncate('n')"), "0", "second truncate is a no-op")


def test_the_table_stays_usable_and_the_guc_turns_it_off(nt, expect):
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
    c.execute("SELECT pgcolumnar.compact('n')")
    guard = _size(c)
    # its own statement, and its answer checked: the shell once sent SET and the call
    # as one string, an implicit transaction block that truncate refuses
    c.execute("SET pgcolumnar.enable_end_truncation = off")
    try:
        off = _one(c, "SELECT pgcolumnar.truncate('n')")
    finally:
        c.execute("RESET pgcolumnar.enable_end_truncation")
    expect.text(f"{_size(c)}|{off}", f"{guard}|0", "GUC off: truncate does nothing")
    expect.at_least(_truncated(c), 1, "GUC on: truncate reclaims")
    expect.at_least(guard - _size(c), 1, "GUC on: file shrank")
    expect.text(*_hashes(c), "parity after guard cycle")


def test_truncate_refuses_a_transaction_block_and_reuse_stays_clean(nt, expect):
    c = nt
    # a ROLLBACK must never leave the metapage highwater lowered while the
    # transactional free_space deletes roll back
    _both(c, "DELETE FROM {t} WHERE id BETWEEN 30001 AND 33000")
    c.execute("SELECT pgcolumnar.compact('n')")
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

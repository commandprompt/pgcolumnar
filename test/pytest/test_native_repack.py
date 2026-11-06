"""REPACK on a columnar table is refused, naming what the user typed, and leaves the
table unharmed; pgcolumnar.vacuum() is the alternative that works (#399, #1341). The
pytest twin of `test/native_repack.sh`, every check by the shell's name. PostgreSQL 19
and later only.

REPACK replaces CLUSTER and VACUUM FULL in 19 and dispatches through
relation_copy_for_cluster, which pgColumnar registers as a stub that raises. So every
spelling errors, the message names REPACK, and the table keeps its rows, content,
options and access method. REPACK (CONCURRENTLY) needs two fixtures: without an
identity index core refuses it before the access method is reached, and with one heap
succeeds while columnar is refused (by core's heap-only check from 19beta4, by ours
before). The alternative must reclaim, not only return: with vacuum() returning before
it rewrote anything every arm stayed green, so the deleted rows are counted before and
after.

A PRIVATE CLUSTER at wal_level=logical, which the concurrent form needs and the session
cluster does not have.
"""
import pathlib
import shutil
import tempfile

import psycopg

ROWS = 20000
HASH = "SELECT md5(string_agg(id::text||':'||v, ',' ORDER BY id)) FROM {}"
OPTS = ("SELECT stripe_row_limit || '/' || compression FROM pgcolumnar.options o "
        "JOIN pg_class c ON c.oid = o.regclass WHERE c.relname = 'rp'")
DEAD = ("SELECT coalesce(sum(deleted_count), 0) FROM pgcolumnar.delete_vector "
        "WHERE storage_id = pgcolumnar.get_storage_id('rp')")


def _one(conn, sql):
    return conn.execute(sql).fetchone()[0]


def _err(conn, sql):
    """-> the error's primary message and detail, or '' when the statement succeeded."""
    try:
        conn.execute(sql)
        return ""
    except psycopg.Error as exc:
        return f"{exc.diag.message_primary} {exc.diag.message_detail or ''}"


def test_repack_on_a_columnar_table(pgc_cluster, expect):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-repack-"))
    work.chmod(0o777)
    (work / "data").mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "repack", work / "data", pick_port(23))
    try:
        cluster.initdb()
        with open(cluster.datadir / "postgresql.conf", "a") as fh:
            fh.write("wal_level=logical\nmax_locks_per_transaction=1024\nmax_wal_senders=10\n"
                     "max_replication_slots=10\n")
        cluster.start()
        with psycopg.connect(cluster.dsn(), autocommit=True) as conn:
            srv = str(_one(conn, "SHOW server_version_num"))
            if not srv.isdigit():
                # a cluster that stopped talking, not an older major (the shell's pgc_fail)
                expect.text(srv, "a server_version_num", "the server did not answer 'SHOW server_version_num'")
            major = int(srv) // 10000
            if major < 19:
                expect.cannot_run("UNSUPPORTED_MAJOR", f"REPACK arrived in PostgreSQL 19; this server is {major}",
                                  name="REPACK")
                return
            for sql in ("CREATE EXTENSION pgcolumnar",
                        "CREATE TABLE rp (id int, v text) USING pgcolumnar",
                        "SELECT pgcolumnar.set_options('rp', stripe_row_limit => 5000, compression => 'zstd')",
                        f"INSERT INTO rp SELECT g, 'x'||g FROM generate_series(1,{ROWS}) g",
                        "DELETE FROM rp WHERE id % 3 = 0",
                        "CREATE INDEX rp_id ON rp (id)",
                        "CREATE TABLE rp_heap (id int, v text)",
                        f"INSERT INTO rp_heap SELECT g, 'x'||g FROM generate_series(1,{ROWS}) g",
                        "CREATE INDEX rp_heap_id ON rp_heap (id)"):
                conn.execute(sql)
            live, hash_before, opts_before = _one(conn, "SELECT count(*) FROM rp"), _one(conn, HASH.format("rp")), \
                _one(conn, OPTS)
            # asserted here, before any REPACK can touch the table
            expect.num(_one(conn, DEAD), ROWS // 3, "premise: the table holds deleted rows for the alternative to reclaim")

            # every spelling errors, and the message names the command
            e = _err(conn, "REPACK rp")
            expect.text("yes" if e else "no (succeeded)", "yes", "REPACK on a columnar table errors (#399)")
            expect.text("yes" if "REPACK" in e else f"no ({e})", "yes",
                        "and the error names REPACK, which is what the user typed (#399)")
            for sql, name in (("REPACK rp USING INDEX rp_id", "REPACK ... USING INDEX errors the same way"),
                              ("REPACK (VERBOSE) rp", "REPACK (VERBOSE) errors the same way"),
                              ("CLUSTER rp USING rp_id", "CLUSTER errors the same way"),
                              ("VACUUM FULL rp", "VACUUM FULL errors the same way")):
                expect.text("yes" if "not supported" in _err(conn, sql) else "no", "yes", name)

            # REPACK (CONCURRENTLY): without an identity index core refuses it first
            nopk = _err(conn, "REPACK (CONCURRENTLY) rp")
            expect.text("no (ours: %s)" % nopk if "not supported" in nopk
                        else "yes" if "cannot execute REPACK" in nopk else f"no ({nopk})", "yes",
                        "without an identity index it is refused by PostgreSQL, not by us")
            for sql in ("CREATE TABLE rp_pk (id int PRIMARY KEY, v text) USING pgcolumnar",
                        f"INSERT INTO rp_pk SELECT g, 'x'||g FROM generate_series(1,{ROWS}) g",
                        "CREATE TABLE rp_pk_heap (id int PRIMARY KEY, v text)",
                        f"INSERT INTO rp_pk_heap SELECT g, 'x'||g FROM generate_series(1,{ROWS}) g"):
                conn.execute(sql)
            pk_rows, pk_hash = _one(conn, "SELECT count(*) FROM rp_pk"), _one(conn, HASH.format("rp_pk"))
            expect.text(_err(conn, "REPACK (CONCURRENTLY) rp_pk_heap") or "succeeded", "succeeded",
                        "control: with an identity index, REPACK (CONCURRENTLY) succeeds on heap")
            # from 19beta4 core refuses a non-heap AM first (#1337); earlier betas reach ours
            c_pk = _err(conn, "REPACK (CONCURRENTLY) rp_pk")
            expect.text("yes" if ("not supported yet" in c_pk or 'only supported for the "heap" access method' in c_pk)
                        else f"no ({c_pk or 'succeeded'})", "yes",
                        "and on a columnar table it is refused: by core's heap-only check from 19beta4, else by ours")
            expect.num(_one(conn, "SELECT count(*) FROM rp_pk"), pk_rows,
                       "the columnar table is undamaged after the refused concurrent repack: rows")
            expect.text(_one(conn, HASH.format("rp_pk")), pk_hash,
                        "the columnar table is undamaged after the refused concurrent repack: content")

            expect.text(_err(conn, "REPACK rp_heap") or "succeeded", "succeeded",
                        "control: REPACK works on a heap table on this build")

            # a failed rewrite must leave the table untouched
            expect.num(_one(conn, "SELECT count(*) FROM rp"), live, "the columnar table is unharmed: row count")
            expect.text(_one(conn, HASH.format("rp")), hash_before, "the columnar table is unharmed: content hash")
            expect.text(_one(conn, OPTS), opts_before, "the columnar table is unharmed: storage options")
            expect.text(_one(conn, "SELECT amname::text FROM pg_am a JOIN pg_class c ON c.relam = a.oid "
                                   "WHERE c.relname = 'rp'"), "pgcolumnar", "the columnar table is unharmed: access method")

            # the supported route reclaims, not only returns: the dead rows counted above go
            expect.text(_err(conn, "SELECT pgcolumnar.vacuum('rp')") or "succeeded", "succeeded",
                        "pgcolumnar.vacuum is the supported alternative and succeeds")
            expect.num(_one(conn, DEAD), 0, "and it reclaimed the deleted rows")
            expect.num(_one(conn, "SELECT count(*) FROM rp"), live, "and it kept every live row")
            expect.text(_one(conn, HASH.format("rp")), hash_before, "and the content is still identical")
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)

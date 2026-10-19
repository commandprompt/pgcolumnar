"""import_parquet refuses a partitioned columnar parent rather than segfaulting (#1263,
#1341). Part of the pytest twin of `test/parquet_import.sh`: these arms only, by the
shell's names, and the pair is declared INCOMPLETE.

import_parquet never asked whether its target had storage. Handed a partitioned parent,
relam pgcolumnar and relfilenode 0, it dereferenced its way to SIGSEGV, on a release
build too. The file must exist before the call, or the import fails at the OPEN and
never reaches the relation. A heap target must still import: the guard is about
storage, not about being columnar. A refusal is 42809, and the crash is read from the
server log, since a dropped connection is an error too and a later one succeeds.
"""
import psycopg

from pgc_vacuity import query_error


def _crashes(pgc_cluster):
    """-> how many backends the server log says died, or a failed reading when the
    log can't be read: a blind count must not read as "no crashes"."""
    try:
        return (pgc_cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _since(pgc_cluster, before):
    """-> backends died since `before`, or the failed reading when either count is
    one: a blind count must not read as "no crashes"."""
    now = _crashes(pgc_cluster)
    for v in (before, now):
        if not isinstance(v, int):
            return v
    return now - before


def _one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _sqlstate(conn, sql, params=None):
    """-> 'accepted', or the SQLSTATE the statement raised."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        return "accepted"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no SQLSTATE ({str(exc).splitlines()[0]})"


def test_a_partitioned_parent_is_refused(pgc_conn, pgc_cluster, server_dir, expect):
    conn = pgc_conn
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    if major < 17:
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"a partitioned table cannot carry an access method before PostgreSQL 17; this server is {major}",
                          name="importing into the partitioned parent is refused")
        return
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE pq_parent (id int, v text) PARTITION BY RANGE (id)")
        cur.execute("ALTER TABLE pq_parent SET ACCESS METHOD pgcolumnar")
        cur.execute("CREATE TABLE pq_leaf PARTITION OF pq_parent FOR VALUES FROM (0) TO (100)")
        cur.execute("INSERT INTO pq_parent SELECT g, 'r' || g FROM generate_series(0, 49) g")
    expect.text(_one(conn, "SELECT c.relkind::text || am.amname::text FROM pg_class c "
                           "JOIN pg_am am ON am.oid = c.relam WHERE c.oid = 'pq_parent'::regclass"),
                "ppgcolumnar", "premise: the parent is partitioned and carries the columnar access method")
    expect.num(_one(conn, "SELECT relfilenode::int FROM pg_class WHERE oid = 'pq_parent'::regclass"), 0,
               "premise: so it has no storage of its own")

    path = str(server_dir / "pq_leaf.parquet")
    _sqlstate(conn, "SELECT pgcolumnar.export_parquet('pq_leaf', %s)", (path,))
    exists = (server_dir / "pq_leaf.parquet").exists() and (server_dir / "pq_leaf.parquet").stat().st_size > 0
    expect.text("yes" if exists else "no", "yes",
                "premise: a parquet file exists, so the import reaches the relation")

    expect.num(_one(conn, "SELECT pgcolumnar.import_parquet('pq_leaf', %s)", (path,)), 50,
               "control: importing that file into the LEAF still works")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE pq_heap (id int, v text)")
    expect.num(_one(conn, "SELECT pgcolumnar.import_parquet('pq_heap', %s)", (path,)), 50,
               "control: a HEAP target still imports, so the guard is about storage")

    crashes = _crashes(pgc_cluster)
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.import_parquet('pq_parent', %s)", (path,)), "42809",
                "importing into the partitioned parent is refused")
    alive = _sqlstate(conn, "SELECT 1") == "accepted"
    died = _since(pgc_cluster, crashes)
    expect.text(f"{'1' if alive else ''}|{died} backend crashes" if isinstance(died, int) else died, "1|0 backend crashes",
                "and the cluster is still up, so the refusal was not an abort")

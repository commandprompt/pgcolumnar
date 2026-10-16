"""A columnar table survives pg_dump and restore, in both formats (#1341). The pytest
twin of `test/pg_dump_roundtrip.sh`, every check by the shell's name.

A source database holds a columnar table with data, a per-table option, an index and a
projection, and a second table whose declared sort_by names columns after a dropped
one, so a restore renumbers their attnums (#288). It is dumped as plain SQL and as a
custom archive, and each is restored into a fresh database, where the rows, the access
method, the option, the declared sort_by and the projection declaration must all
survive. The projection's storage is not carried (#266) but rebuild_projections()
recovers it.

Two arms are about more than their call returning: the point lookup must take the
restored INDEX, since the columnar scan answers it too with the index gone; and the
restored sort_by must leave the rows in (host, ts) order, counted as inversions in scan
order, which is why the readers run with parallel workers off.
"""
import pathlib
import subprocess
import tempfile

import psycopg
import pytest

SRC = "pgc_dumprt"
SUM_SQL = ("SELECT coalesce(sum(hashtextextended(id::text || '|' || kind::text || '|' || payload, 0)), 0) "
           "FROM dt")
AM_SQL = ("SELECT a.amname::text FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
          "WHERE c.oid = 'dt'::regclass")
OPT_SQL = ("SELECT coalesce((SELECT encode_effort FROM pgcolumnar.options WHERE regclass = 'dt'::regclass), "
           "'<none>')")
PROJ_SQL = ("SELECT count(*) FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('dt') "
            "AND projection_id > 0")
DECL_SQL = "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'dt'::regclass"
INVERSIONS = ("SELECT count(*) FROM (SELECT host, ts, lag(host) OVER () AS lh, lag(ts) OVER () AS lt FROM ds) x "
              "WHERE (lh, lt) > (host, ts)")


def _conn(cluster, db):
    return psycopg.connect(cluster.dsn(dbname=db), autocommit=True,
                           options="-c max_parallel_workers_per_gather=0")


def _one(cluster, db, sql):
    with _conn(cluster, db) as c:
        return c.execute(sql).fetchone()[0]


def _admin(cluster, sql):
    with psycopg.connect(cluster.dsn(), autocommit=True) as c:
        c.execute(sql)


def _tool(cluster, name, *args):
    return subprocess.run([str(pathlib.Path(cluster.bindir) / name), "-h", "127.0.0.1", "-p", str(cluster.port),
                           "-U", "postgres", *args], capture_output=True, text=True).returncode


@pytest.fixture(scope="module")
def src(pgc_cluster):
    """-> (cluster, the values the source holds before any dump)."""
    _admin(pgc_cluster, f"DROP DATABASE IF EXISTS {SRC}")
    _admin(pgc_cluster, f"CREATE DATABASE {SRC}")
    try:
        with _conn(pgc_cluster, SRC) as c:
            for sql in ("CREATE EXTENSION pgcolumnar",
                        "CREATE TABLE dt (id bigint, kind int, payload text) USING pgcolumnar",
                        "SELECT pgcolumnar.set_options('dt', encode_effort => 'fast')",
                        "INSERT INTO dt SELECT g, g % 7, 'p' || g FROM generate_series(1, 50000) g",
                        "CREATE INDEX dt_id_idx ON dt (id)",
                        "SELECT pgcolumnar.add_projection('dt', 'dt_proj', ARRAY['id', 'payload'], ARRAY['id'])",
                        # #288: sort_by names columns that sit after a dropped one
                        "CREATE TABLE ds (id bigint, doomed int, host text, ts timestamptz) USING pgcolumnar",
                        "INSERT INTO ds (id, host, ts) SELECT g, 'h' || (g % 20), "
                        "'2024-01-01'::timestamptz + (g % 100) * interval '1 minute' FROM generate_series(1, 20000) g",
                        "ALTER TABLE ds DROP COLUMN doomed",
                        "SELECT pgcolumnar.set_options('ds', sort_by => ARRAY['host', 'ts'])"):
                c.execute(sql)
        before = {k: _one(pgc_cluster, SRC, sql) for k, sql in
                  (("count", "SELECT count(*) FROM dt"), ("sum", SUM_SQL), ("opt", OPT_SQL),
                   ("proj", PROJ_SQL), ("decl", DECL_SQL))}
        yield pgc_cluster, before
    finally:
        _admin(pgc_cluster, f"DROP DATABASE IF EXISTS {SRC} WITH (FORCE)")


def test_dropping_a_projection_forgets_its_declaration(src, expect):
    cluster, before = src
    expect.num(before["proj"], 1, "the base table has a projection to preserve")
    expect.num(before["decl"], 1, "and a declaration recorded for it")
    # If DROP kept the declaration, a rebuild would resurrect what the operator removed.
    with _conn(cluster, SRC) as c:
        c.execute("SELECT pgcolumnar.add_projection('dt', 'dt_tmp', ARRAY['id'], ARRAY['id'])")
    expect.num(_one(cluster, SRC, DECL_SQL), 2, "a second projection is declared")
    with _conn(cluster, SRC) as c:
        c.execute("SELECT pgcolumnar.drop_projection('dt', 'dt_tmp')")
    expect.num(_one(cluster, SRC, DECL_SQL), 1,
               "and dropping it forgets the declaration, so a rebuild cannot resurrect it")
    expect.num(_one(cluster, SRC, "SELECT pgcolumnar.rebuild_projections()"), 0, "a rebuild now builds nothing")


def _verify(cluster, db, label, before, expect):
    expect.num(_one(cluster, db, "SELECT count(*) FROM dt"), before["count"], f"{label}: row count survives")
    expect.num(_one(cluster, db, SUM_SQL), before["sum"], f"{label}: row data survives (checksum)")
    expect.text(_one(cluster, db, AM_SQL), "pgcolumnar", f"{label}: access method is pgcolumnar")
    with _conn(cluster, db) as c:
        c.execute("SET enable_seqscan = off")
        c.execute("SET enable_bitmapscan = off")
        plan = "\n".join(r[0] for r in c.execute("EXPLAIN (COSTS off) SELECT id FROM dt WHERE id = 12345"))
        expect.num(sum(1 for line in plan.splitlines() if "Index" in line and "using dt_id_idx" in line), 1,
                   f"{label}: premise: the point lookup takes the restored index")
        expect.num(c.execute("SELECT id FROM dt WHERE id = 12345").fetchone()[0], 12345,
                   f"{label}: restored index answers a point lookup")
    expect.text(_one(cluster, db, OPT_SQL), before["opt"], f"{label}: per-table options survive")
    # #266: the projection's storage is keyed by a storage id a restore reassigns
    expect.num(_one(cluster, db, PROJ_SQL), 0, f"{label}: projection storage is NOT carried by pg_dump (see #266)")
    expect.text(_one(cluster, db, "SELECT sort_by::text FROM pgcolumnar.options WHERE regclass = 'ds'::regclass"),
                "{host,ts}", f"{label}: declared sort_by survives restore")
    expect.text(str(_one(cluster, db, f"SELECT ({INVERSIONS}) > 0")), "True",
                f"{label}: premise: the restored ds is not yet in its sort_by order")
    with _conn(cluster, db) as c:
        c.execute("SELECT pgcolumnar.vacuum_sorted('ds')")
        applied = c.execute(f"SELECT count(*) || '|' || ({INVERSIONS}) FROM ds").fetchone()[0]
    expect.text(applied, "20000|0", f"{label}: restored sort_by resolves and applies")
    expect.num(_one(cluster, db, DECL_SQL), before["decl"], f"{label}: the projection declaration IS carried")
    expect.num(_one(cluster, db, "SELECT pgcolumnar.rebuild_projections()"), 1,
               f"{label}: rebuild_projections() reports it rebuilt one")
    expect.num(_one(cluster, db, PROJ_SQL), before["proj"], f"{label}: and the projection exists again after the rebuild")
    expect.num(_one(cluster, db, "SELECT count(*) FROM pgcolumnar.read_projection('dt', 'dt_proj')"),
               _one(cluster, db, "SELECT count(*) FROM dt"), f"{label}: and it reads the same rows as the base table")
    expect.num(_one(cluster, db, "SELECT pgcolumnar.rebuild_projections()"), 0, f"{label}: a second rebuild is a no-op")


def test_a_plain_dump_round_trips(src, expect):
    cluster, before = src
    db = f"{SRC}_p"
    with tempfile.TemporaryDirectory() as d:
        path = str(pathlib.Path(d) / "rt.sql")
        expect.num(_tool(cluster, "pg_dump", "-d", SRC, "-f", path), 0, "plain: pg_dump exited 0")
        text = pathlib.Path(path).read_text()
        expect.text("yes" if ("USING pgcolumnar" in text or "default_table_access_method = pgcolumnar" in text)
                    else "no", "yes", "plain: dump recreates the table on the columnar AM")
        _admin(cluster, f"DROP DATABASE IF EXISTS {db}")
        _admin(cluster, f"CREATE DATABASE {db}")
        try:
            expect.num(_tool(cluster, "psql", "-d", db, "-v", "ON_ERROR_STOP=1", "-q", "-f", path), 0,
                       "plain: restore exited 0 (ON_ERROR_STOP)")
            _verify(cluster, db, "plain", before, expect)
        finally:
            _admin(cluster, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_a_custom_archive_round_trips(src, expect):
    cluster, before = src
    db = f"{SRC}_c"
    with tempfile.TemporaryDirectory() as d:
        path = str(pathlib.Path(d) / "rt.dump")
        expect.num(_tool(cluster, "pg_dump", "-Fc", "-d", SRC, "-f", path), 0, "custom: pg_dump -Fc exited 0")
        _admin(cluster, f"DROP DATABASE IF EXISTS {db}")
        _admin(cluster, f"CREATE DATABASE {db}")
        try:
            expect.num(_tool(cluster, "pg_restore", "--exit-on-error", "-d", db, path), 0,
                       "custom: pg_restore exited 0 (--exit-on-error)")
            _verify(cluster, db, "custom", before, expect)
        finally:
            _admin(cluster, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")

"""The foreign-data wrappers' PLANNING path reads no server file for a role the scan
would refuse (the Iceberg gate; #330 for Parquet).

A planner estimate is visible through EXPLAIN. A wrapper that reads the file at plan
time for anyone tells a role without pg_read_server_files how many rows a server-side
table holds, and whether a path exists at all. Independent of
test/fdw_plan_privilege.sh: own fixtures, own driver; check names match.
"""

import os
import pathlib
import re
import shutil
import stat

import psycopg

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = HERE.parent / "fixtures" / "iceberg" / "warehouse" / "db" / "events"


def _open(root):
    """Readable and writable by the server process, every parent included."""
    for parent in [root] + list(root.parents):
        try:
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IROTH | stat.S_IXOTH)
        except (PermissionError, FileNotFoundError):
            break
    for where, _dirs, files in os.walk(root):
        os.chmod(where, 0o777)
        for f in files:
            os.chmod(os.path.join(where, f), 0o644)


def _est(conn, sql, role=None):
    """-> the top node's row estimate as text, or the SQLSTATE if EXPLAIN failed."""
    with conn.cursor() as cur:
        try:
            if role:
                cur.execute(f"SET ROLE {role}")
            cur.execute("EXPLAIN " + sql)
            plan = "\n".join(r[0] for r in cur.fetchall())
            return re.search(r"rows=(\d+)", plan).group(1)
        except psycopg.Error as e:
            return e.sqlstate
        finally:
            cur.execute("RESET ROLE")


def _state(conn, sql, role):
    with conn.cursor() as cur:
        try:
            cur.execute(f"SET ROLE {role}")
            cur.execute(sql)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate
        finally:
            cur.execute("RESET ROLE")


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_fdw_planning_reads_no_server_file_for_an_unprivileged_role(pgc_conn, tmp_path, expect):
    if not any((FIXTURE / "data").rglob("*.parquet")):
        expect.cannot_run("MISSING_DEPENDENCY", "the committed Iceberg warehouse is not in this tree",
                          name="iceberg warehouse data files are missing")
        return
    stage = tmp_path / "fpp"
    shutil.copytree(FIXTURE, stage / "events")
    _open(stage)
    md = sorted((stage / "events" / "metadata").glob("*.metadata.json"))[-1]
    pq = stage / "fp.parquet"
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fp_reader'")
        if cur.fetchone() is None:
            cur.execute("CREATE ROLE fp_reader NOSUPERUSER")
        cur.execute("SELECT current_schema()")
        cur.execute(f'GRANT USAGE ON SCHEMA "{cur.fetchone()[0]}" TO fp_reader')
        cur.execute("CREATE SERVER fp_ice FOREIGN DATA WRAPPER pgcolumnar_iceberg")
        cur.execute("CREATE FOREIGN TABLE fp_ev (id bigint, region text, amount int)"
                    f" SERVER fp_ice OPTIONS (metadata_path '{md}')")
        cur.execute("CREATE FOREIGN TABLE fp_gone (id bigint, region text, amount int)"
                    f" SERVER fp_ice OPTIONS (metadata_path '{stage}/no/such/v1.metadata.json')")
        cur.execute("CREATE TABLE fp_src (id int, v text) USING pgcolumnar")
        cur.execute("INSERT INTO fp_src SELECT g, md5(g::text) || md5((g * 7)::text)"
                    " FROM generate_series(1, 20000) g")
        cur.execute(f"SELECT pgcolumnar.export_parquet('fp_src', '{pq}')")
        cur.execute("CREATE SERVER fp_pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute(f"CREATE FOREIGN TABLE fp_pqt (id int, v text) SERVER fp_pq OPTIONS (path '{pq}')")
        cur.execute("GRANT SELECT ON fp_ev, fp_gone, fp_pqt TO fp_reader")

    expect.text(str(_one(pgc_conn, f"SELECT (pg_stat_file('{pq}')).size > 0")), "True",
                "premise: the Parquet file was written")
    expect.text(str(_one(pgc_conn, "SELECT pg_has_role('fp_reader', 'pg_read_server_files', 'USAGE')")),
                "False", "premise: the reader is not a member of pg_read_server_files")
    rows = str(_one(pgc_conn, "SELECT count(*) FROM fp_ev"))

    expect.text(_est(pgc_conn, "SELECT * FROM fp_ev"), rows,
                "a privileged planner estimates the Iceberg table from its manifests")
    expect.text(_est(pgc_conn, "SELECT * FROM fp_ev", "fp_reader"), "1000",
                "an unprivileged planner gets the flat estimate, not the Iceberg table's row count")
    expect.text(_est(pgc_conn, "SELECT * FROM fp_gone", "fp_reader"), "1000",
                "and a metadata_path that does not exist plans the same, so planning probes nothing")
    expect.text(_state(pgc_conn, "SELECT count(*) FROM fp_ev", "fp_reader"), "42501",
                "the unprivileged Iceberg scan is still refused (42501)")

    priv = _est(pgc_conn, "SELECT * FROM fp_pqt")
    expect.text("sized" if priv.isdigit() and priv != "1000" else f"flat ({priv})", "sized",
                "a privileged planner estimates the Parquet table from the file's size")
    expect.text(_est(pgc_conn, "SELECT * FROM fp_pqt", "fp_reader"), "1000",
                "an unprivileged planner gets the flat Parquet estimate")

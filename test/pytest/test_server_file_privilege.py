"""Every function that touches a server file is gated by pg_read_server_files or
pg_write_server_files, as COPY is (#560 and after). The pytest twin of
`test/server_file_privilege.sh`, every check by the shell's name (#1341).

Each of 21 entry points is called twice: as `t_none`, which holds neither server-file
role and must be refused by the gate its direction needs, and as `t_priv`, which holds
both and must get past it (the call then fails on the missing file, which is fine).
Then every function in the install script that takes a path or filename must be one of
those entry points, so a new file function cannot ship without a gate test.

HOW A CALL IS READ: "refused" when the reply names the gate role, "passed" when it does
not, and "no reading" when the role never took effect. Without that last step a failed
SET ROLE named no gate, and with it failing for the privileged role all 21 "gets past
the gate" checks passed.

ON A PRIVATE DATABASE, with the two roles created for the module and dropped after.
"""
import pathlib
import re

import psycopg
import pytest

DB = "pgc_server_file_privilege"
ROLES = ("t_none", "t_priv")
NOPE = "/tmp/pgc_sfp_does_not_exist.parquet"

ENTRIES = [  # label, the server-file role it must be gated by, the call
    ("import_parquet", "pg_read_server_files", "SELECT pgcolumnar.import_parquet('imp_target'::regclass, '{nope}')"),
    ("read_parquet", "pg_read_server_files", "SELECT * FROM pgcolumnar.read_parquet('{nope}') AS t(id int)"),
    ("parquet_schema", "pg_read_server_files", "SELECT * FROM pgcolumnar.parquet_schema('{nope}')"),
    ("import_arrow", "pg_read_server_files", "SELECT pgcolumnar.import_arrow('imp_target'::regclass, '{nope}')"),
    ("file_split_offsets", "pg_read_server_files", "SELECT pgcolumnar.file_split_offsets('{nope}', 2)"),
    ("parallel_copy", "pg_read_server_files", "SELECT pgcolumnar.parallel_copy('imp_target'::regclass, '{nope}', 2)"),
    ("parquet_fdw_scan", "pg_read_server_files", "SELECT * FROM ft"),
    ("read_avro_manifest", "pg_read_server_files", "SELECT * FROM pgcolumnar.read_avro_manifest('{nope}')"),
    ("read_manifest_list", "pg_read_server_files", "SELECT * FROM pgcolumnar.read_manifest_list('{nope}')"),
    ("iceberg_current_snapshot", "pg_read_server_files", "SELECT * FROM pgcolumnar.iceberg_current_snapshot('{nope}')"),
    ("iceberg_data_files", "pg_read_server_files", "SELECT * FROM pgcolumnar.iceberg_data_files('{nope}')"),
    ("iceberg_scan", "pg_read_server_files", "SELECT * FROM pgcolumnar.iceberg_scan('{nope}') AS t(id bigint)"),
    ("export_parquet", "pg_write_server_files", "SELECT pgcolumnar.export_parquet('imp_target'::regclass, '{out}.parquet')"),
    ("export_arrow", "pg_write_server_files", "SELECT pgcolumnar.export_arrow('imp_target'::regclass, '{out}.arrow')"),
    ("parallel_export_parquet", "pg_write_server_files", "SELECT pgcolumnar.parallel_export_parquet('imp_target'::regclass, '{outdir}', 2)"),
    ("tier_partition", "pg_write_server_files", "SELECT pgcolumnar.tier_partition('sfp_p1'::regclass, 'pqsrv', '{out}.parquet')"),
    ("iceberg_rest_table_location", "pg_read_server_files", "SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:1', 'db', 't')"),
    ("iceberg_rest_scan", "pg_read_server_files", "SELECT * FROM pgcolumnar.iceberg_rest_scan('http://127.0.0.1:1', 'db', 't') AS t(id bigint)"),
    ("iceberg_rest_namespaces", "pg_read_server_files", "SELECT * FROM pgcolumnar.iceberg_rest_namespaces('http://127.0.0.1:1')"),
    ("iceberg_rest_tables", "pg_read_server_files", "SELECT * FROM pgcolumnar.iceberg_rest_tables('http://127.0.0.1:1', 'db')"),
    ("iceberg_fdw_scan", "pg_read_server_files", "SELECT * FROM ift"),
]
REFUSED = [  # name, label: the role with nothing is refused by the gate
    ("no role is refused by the pg_read_server_files gate: import_parquet", "import_parquet"),
    ("no role is refused by the pg_read_server_files gate: read_parquet", "read_parquet"),
    ("no role is refused by the pg_read_server_files gate: parquet_schema", "parquet_schema"),
    ("no role is refused by the pg_read_server_files gate: import_arrow", "import_arrow"),
    ("no role is refused by the pg_read_server_files gate: file_split_offsets", "file_split_offsets"),
    ("no role is refused by the pg_read_server_files gate: parallel_copy", "parallel_copy"),
    ("no role is refused by the pg_read_server_files gate: parquet_fdw_scan", "parquet_fdw_scan"),
    ("no role is refused by the pg_read_server_files gate: read_avro_manifest", "read_avro_manifest"),
    ("no role is refused by the pg_read_server_files gate: read_manifest_list", "read_manifest_list"),
    ("no role is refused by the pg_read_server_files gate: iceberg_current_snapshot", "iceberg_current_snapshot"),
    ("no role is refused by the pg_read_server_files gate: iceberg_data_files", "iceberg_data_files"),
    ("no role is refused by the pg_read_server_files gate: iceberg_scan", "iceberg_scan"),
    ("no role is refused by the pg_write_server_files gate: export_parquet", "export_parquet"),
    ("no role is refused by the pg_write_server_files gate: export_arrow", "export_arrow"),
    ("no role is refused by the pg_write_server_files gate: parallel_export_parquet", "parallel_export_parquet"),
    ("no role is refused by the pg_write_server_files gate: tier_partition", "tier_partition"),
    ("no role is refused by the pg_read_server_files gate: iceberg_rest_table_location", "iceberg_rest_table_location"),
    ("no role is refused by the pg_read_server_files gate: iceberg_rest_scan", "iceberg_rest_scan"),
    ("no role is refused by the pg_read_server_files gate: iceberg_rest_namespaces", "iceberg_rest_namespaces"),
    ("no role is refused by the pg_read_server_files gate: iceberg_rest_tables", "iceberg_rest_tables"),
    ("no role is refused by the pg_read_server_files gate: iceberg_fdw_scan", "iceberg_fdw_scan"),
]
PASSED = [  # name, label: the role holding both server-file roles gets past it
    ("the pg_read_server_files role gets past the gate: import_parquet", "import_parquet"),
    ("the pg_read_server_files role gets past the gate: read_parquet", "read_parquet"),
    ("the pg_read_server_files role gets past the gate: parquet_schema", "parquet_schema"),
    ("the pg_read_server_files role gets past the gate: import_arrow", "import_arrow"),
    ("the pg_read_server_files role gets past the gate: file_split_offsets", "file_split_offsets"),
    ("the pg_read_server_files role gets past the gate: parallel_copy", "parallel_copy"),
    ("the pg_read_server_files role gets past the gate: parquet_fdw_scan", "parquet_fdw_scan"),
    ("the pg_read_server_files role gets past the gate: read_avro_manifest", "read_avro_manifest"),
    ("the pg_read_server_files role gets past the gate: read_manifest_list", "read_manifest_list"),
    ("the pg_read_server_files role gets past the gate: iceberg_current_snapshot", "iceberg_current_snapshot"),
    ("the pg_read_server_files role gets past the gate: iceberg_data_files", "iceberg_data_files"),
    ("the pg_read_server_files role gets past the gate: iceberg_scan", "iceberg_scan"),
    ("the pg_write_server_files role gets past the gate: export_parquet", "export_parquet"),
    ("the pg_write_server_files role gets past the gate: export_arrow", "export_arrow"),
    ("the pg_write_server_files role gets past the gate: parallel_export_parquet", "parallel_export_parquet"),
    ("the pg_write_server_files role gets past the gate: tier_partition", "tier_partition"),
    ("the pg_read_server_files role gets past the gate: iceberg_rest_table_location", "iceberg_rest_table_location"),
    ("the pg_read_server_files role gets past the gate: iceberg_rest_scan", "iceberg_rest_scan"),
    ("the pg_read_server_files role gets past the gate: iceberg_rest_namespaces", "iceberg_rest_namespaces"),
    ("the pg_read_server_files role gets past the gate: iceberg_rest_tables", "iceberg_rest_tables"),
    ("the pg_read_server_files role gets past the gate: iceberg_fdw_scan", "iceberg_fdw_scan"),
]
COVERED = [  # name, function
    ("server-file function is covered by the boundary test: export_arrow", "export_arrow"),
    ("server-file function is covered by the boundary test: export_parquet", "export_parquet"),
    ("server-file function is covered by the boundary test: file_split_offsets", "file_split_offsets"),
    ("server-file function is covered by the boundary test: iceberg_current_snapshot", "iceberg_current_snapshot"),
    ("server-file function is covered by the boundary test: iceberg_data_files", "iceberg_data_files"),
    ("server-file function is covered by the boundary test: iceberg_scan", "iceberg_scan"),
    ("server-file function is covered by the boundary test: import_arrow", "import_arrow"),
    ("server-file function is covered by the boundary test: import_parquet", "import_parquet"),
    ("server-file function is covered by the boundary test: parallel_copy", "parallel_copy"),
    ("server-file function is covered by the boundary test: parallel_export_parquet", "parallel_export_parquet"),
    ("server-file function is covered by the boundary test: parquet_schema", "parquet_schema"),
    ("server-file function is covered by the boundary test: read_avro_manifest", "read_avro_manifest"),
    ("server-file function is covered by the boundary test: read_manifest_list", "read_manifest_list"),
    ("server-file function is covered by the boundary test: read_parquet", "read_parquet"),
    ("server-file function is covered by the boundary test: tier_partition", "tier_partition"),
]
KNOWN_FILE_FUNCTIONS = "export_arrow export_parquet file_split_offsets iceberg_current_snapshot iceberg_data_files iceberg_scan import_arrow import_parquet parallel_copy parallel_export_parquet parquet_schema read_avro_manifest read_manifest_list read_parquet tier_partition".split()


def _discovered():
    """-> the pgcolumnar functions the install script defines with a path or filename
    argument, read as the shell's awk reads them."""
    control = (pathlib.Path(__file__).resolve().parent.parent.parent / "pgcolumnar.control").read_text()
    ver = re.search(r"default_version = '([^']+)'", control).group(1)
    text = (pathlib.Path(__file__).resolve().parent.parent.parent / f"pgcolumnar--{ver}.sql").read_text()
    found, buf, cap = set(), "", False
    for line in text.splitlines():
        if re.search(r"CREATE FUNCTION pgcolumnar\.", line):
            cap, buf = True, ""
        if cap:
            buf += " " + line
            if ")" in buf:
                if "path text" in buf or "filename text" in buf:
                    found.add(re.search(r"pgcolumnar\.([a-z_]+)", buf).group(1))
                cap = False
    return found


def _gate(dsn, role, gate, sql):
    """-> "refused", "passed", or "no reading" when `role` never took effect."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        try:
            conn.execute(f"SET ROLE {role}")
            if conn.execute("SELECT current_user").fetchone()[0] != role:
                return f"no reading (SET ROLE {role} did not take)"
        except psycopg.Error as exc:
            return f"no reading (SET ROLE {role} did not take: {str(exc).splitlines()[0]})"
        try:
            conn.execute(sql).fetchall()
            reply = ""
        except psycopg.Error as exc:
            reply = str(exc)
    return "refused" if gate.lower() in reply.lower() else "passed"


@pytest.fixture(scope="module")
def sf(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        for r in ROLES:
            a.execute(f"DROP ROLE IF EXISTS {r}")
        a.execute(f"CREATE DATABASE {DB}")
        for r in ROLES:
            a.execute(f"CREATE ROLE {r} NOSUPERUSER")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in ["CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                        "CREATE TABLE imp_target (id int, v text) USING pgcolumnar",
                        "GRANT USAGE ON SCHEMA pgcolumnar TO t_none",
                        "GRANT USAGE ON SCHEMA pgcolumnar TO t_priv",
                        "GRANT pg_read_server_files, pg_write_server_files TO t_priv",
                        "GRANT SELECT, INSERT ON imp_target TO t_priv",
                        "CREATE SERVER pqsrv FOREIGN DATA WRAPPER pgcolumnar_parquet",
                        f"CREATE FOREIGN TABLE ft (id int, v text) SERVER pqsrv OPTIONS (path '{NOPE}')",
                        "GRANT SELECT ON ft TO t_none, t_priv",
                        "CREATE SERVER icesrv FOREIGN DATA WRAPPER pgcolumnar_iceberg",
                        f"CREATE FOREIGN TABLE ift (id bigint) SERVER icesrv OPTIONS (metadata_path '{NOPE}')",
                        "GRANT SELECT ON ift TO t_none, t_priv",
                        "CREATE TABLE sfp_parent (k int) PARTITION BY RANGE (k)",
                        "CREATE TABLE sfp_p1 PARTITION OF sfp_parent FOR VALUES FROM (0) TO (10) USING pgcolumnar",
                        "INSERT INTO sfp_parent VALUES (1)",
                        "GRANT SELECT, UPDATE ON sfp_p1 TO t_none, t_priv"]:
                conn.execute(sql)
        out, outdir = str(server_dir / "sfp_out"), str(server_dir / "sfp_dir")
        r = {}
        for label, gate, sql in ENTRIES:
            call = sql.format(nope=NOPE, out=out, outdir=outdir)
            r[("none", label)] = _gate(dsn, "t_none", gate, call)
            for p in (out + ".parquet", out + ".arrow"):
                pathlib.Path(p).unlink(missing_ok=True)
            r[("priv", label)] = _gate(dsn, "t_priv", gate, call)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            for r in ROLES:
                a.execute(f"DROP ROLE IF EXISTS {r}")


@pytest.mark.parametrize("name,label", REFUSED)
def test_the_role_with_nothing_is_refused_by_the_gate(sf, name, label, expect):
    expect.text(sf[("none", label)], "refused", name)


@pytest.mark.parametrize("name,label", PASSED)
def test_the_privileged_role_gets_past_the_gate(sf, name, label, expect):
    expect.text(sf[("priv", label)], "passed", name)


@pytest.mark.parametrize("name,fn", COVERED)
def test_a_file_function_is_covered_by_the_boundary_test(name, fn, expect):
    labels = {label for label, _, _ in ENTRIES}
    got = "yes" if fn in _discovered() and fn in labels else \
        ("no (not a file function in the install script)" if fn not in _discovered() else "no (no entry point)")
    expect.text(got, "yes", name)


def test_the_install_script_defines_no_unnamed_file_function(expect):
    extra = sorted(_discovered() - set(KNOWN_FILE_FUNCTIONS))
    expect.text(" ".join(extra) or "none", "none",
                "the install script defines no file function this suite does not name")


def test_the_coverage_check_found_the_file_functions(expect):
    n = len(_discovered())
    expect.text("yes" if n >= 7 else f"no ({n} found)", "yes",
                "coverage check found the file functions (not an empty scan)")

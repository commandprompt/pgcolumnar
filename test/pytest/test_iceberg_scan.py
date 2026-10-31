"""pgcolumnar.iceberg_scan reads an Iceberg table at its current snapshot, resolving
columns by field id (#388 phase 3c). The pytest twin of `test/iceberg_scan.sh`, every
check by the shell's name (#1341).

The warehouse is relocated so its recorded root differs from where it lies. The scan
reads the current snapshot's five rows, projects and reorders by field id, reads a
subset, and still reads a column the schema renamed, because it selects by id and not
by name; a name the schema lacks is refused with 42703. A role with USAGE on the
schema but without pg_read_server_files is refused with 42501, and a non-superuser
holding that role reads every row.

ON A PRIVATE DATABASE, with the warehouse copied into `server_dir`, because the
backend reads it. The two roles log in on their own, as the shell's do, and are
dropped at teardown. Where the fixture is missing the first test fails under the
shell's pgc_skip message, as pgc_skip does, and the rest decline unnamed.
"""
import json
import pathlib
import shutil
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_scan"
WH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse"
VARIANTS = WH.parent / "gen_iceberg_variants.py"
ROWS5 = "1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50"
SNAPCOLS = ("snapshot_id || '|' || coalesce(parent_snapshot_id::text, '-') || '|' || "
            "sequence_number || '|' || manifest_list")
UNPRIV, READER = "pgc_icescan_unpriv", "pgc_icescan_reader"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _rows(dsn, sql, user=None, port=None):
    target = dsn if user is None else f"host=127.0.0.1 port={port} user={user} dbname={DB}"
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            return [r[0] for r in conn.execute(sql)]
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _sqlstate(dsn, sql, user=None, port=None):
    target = dsn if user is None else f"host=127.0.0.1 port={port} user={user} dbname={DB}"
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


def _refusal(dsn, sql, phrase, label):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, else
    "<SQLSTATE>: <message>", or "no error"."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or _message(exc)
        return exc.sqlstate + label if phrase in msg else f"{exc.sqlstate}: {msg}"
    return "no error"


def _variants(dest):
    """-> {name: path} from gen_iceberg_variants.py, or {} with the failure under "error"."""
    run = subprocess.run([sys.executable, str(VARIANTS), str(dest)],
                         capture_output=True, text=True)
    out = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line)
    if run.returncode != 0:
        out["error"] = (run.stdout + run.stderr).strip()
    return out


def _lines(v):
    return v if isinstance(v, str) else "\n".join(str(x) for x in v)


@pytest.fixture(scope="module")
def isc(pgc_cluster, server_dir):
    if not list(WH.glob("db/events/data/*/*.parquet")):
        yield None
        return
    dest = server_dir / "relocated"
    shutil.copytree(WH / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    md = sorted((dest / "db" / "events" / "metadata").glob("*.metadata.json"))[-1]
    var = _variants(dest)
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)

    def vpath(name):
        return var.get(name, f"no variant {name}: {var.get('error', var)}")
    df = sorted((dest / "db" / "events" / "data").glob("*/*.parquet"))[0]
    m = json.loads(md.read_text())
    sch = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
    for f in sch["fields"]:
        if f["id"] == 3:
            f["name"] = "amt2"
    ren = md.parent / "renamed.metadata.json"
    ren.write_text(json.dumps(m))
    ren.chmod(0o666)
    admin = pgc_cluster.dsn()
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        for role in (UNPRIV, READER):
            a.execute(f"DROP ROLE IF EXISTS {role}")
            a.execute(f"CREATE ROLE {role} LOGIN")
        a.execute(f"GRANT pg_read_server_files TO {READER}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {UNPRIV}, {READER}")
        scan = "pgcolumnar.iceberg_scan('{}')"
        r = {
            "ids": _rows(dsn, f"SELECT string_agg(field_id::text, ',' ORDER BY field_id) "
                              f"FROM pgcolumnar.parquet_schema('{df}') WHERE field_id IS NOT NULL"),
            "full": _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM {scan.format(md)} "
                               "AS t(id bigint, region text, amount int) ORDER BY id"),
            "reorder": _rows(dsn, f"SELECT amount || '|' || id FROM {scan.format(md)} AS t(amount int, id bigint) "
                                  "ORDER BY id"),
            "subset": _rows(dsn, f"SELECT string_agg(region, ',' ORDER BY region) FROM {scan.format(md)} AS t(region text)"),
            "renamed": _rows(dsn, f"SELECT string_agg(amt2::text, ',' ORDER BY amt2) FROM {scan.format(ren)} AS t(amt2 int)"),
            "nosuch": _sqlstate(dsn, f"SELECT * FROM {scan.format(md)} AS t(nosuch int)"),
            "plain snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{md}')"),
            "gz_infix snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_infix')}')"),
            "gz_suffix snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_suffix')}')"),
            "gz_bomb": _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_bomb')}')",
                                "inflates past", " past the cap"),
            "gz_trunc": _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_trunc')}')",
                                 "gzip", " naming gzip"),
            "gz_multi snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_multi')}')"),
            "gz_padded snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_padded')}')"),
            "az": _refusal(dsn, "SELECT * FROM pgcolumnar.iceberg_current_snapshot('az://b/t/metadata/v1.metadata.json')",
                           'reading "az://', " remote refusal"),
            "unpriv": _sqlstate(dsn, f"SELECT * FROM {scan.format(md)} AS t(id bigint)", UNPRIV, pgc_cluster.port),
            "reader": _rows(dsn, f"SELECT count(*) FROM {scan.format(md)} AS t(id bigint)", READER, pgc_cluster.port),
        }
        for name in ("gz_infix", "gz_suffix", "lower", "mixed"):
            r[name + " rows"] = _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM "
                                           f"{scan.format(vpath(name))} AS t(id bigint, region text, amount int) "
                                           "ORDER BY id")
        yield r
    finally:
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            for role in (UNPRIV, READER):
                a.execute(f"DROP ROLE IF EXISTS {role}")


def _declined(isc, expect):
    if isc is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the iceberg warehouse fixture is missing")
    return True


def test_the_data_file_carries_field_ids(isc, expect):
    if isc is None:
        expect.text("missing", "present", "iceberg warehouse data files are missing")
        return
    expect.require_module("json", name="python3 is needed")
    expect.text(_lines(isc["ids"]), "1,2,3", "premise: the data file carries field ids (id=1,region=2,amount=3)")


def test_iceberg_scan_reads_the_current_snapshots_rows(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["full"]), "1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50",
                "iceberg_scan reads the current snapshot's rows")


def test_iceberg_scan_projects_and_reorders_by_field_id(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["reorder"]), "10|1\n20|2\n30|3\n40|4\n50|5",
                "iceberg_scan projects and reorders by field id (amount, id)")


def test_iceberg_scan_reads_a_subset(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["subset"]), "eu,eu,us,us,us", "iceberg_scan reads a subset (region only)")


def test_a_renamed_column_still_reads_by_field_id(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["renamed"]), "10,20,30,40,50",
                "a column renamed in the schema still reads by field id (amt2 = amount)")


def test_an_output_column_not_in_the_schema_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["nosuch"]), "42703", "an output column not in the schema is refused (42703)")


def test_an_unprivileged_role_is_refused_the_scan(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["unpriv"]), "42501", "an unprivileged role is refused (42501)")


def test_a_pg_read_server_files_member_may_scan(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["reader"]), "5", "a pg_read_server_files member who is not superuser may scan")


def _plain_snapshot(isc):
    return _lines(isc["plain snap"]) or "no snapshot from the plain file"


def test_iceberg_current_snapshot_reads_a_gz_metadata_json_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_infix snap"]), _plain_snapshot(isc),
                "iceberg_current_snapshot reads a .gz.metadata.json file as it reads the plain file")


def test_iceberg_current_snapshot_reads_a_metadata_json_gz_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_suffix snap"]), _plain_snapshot(isc),
                "iceberg_current_snapshot reads a .metadata.json.gz file as it reads the plain file")


def test_iceberg_scan_reads_a_table_whose_metadata_is_gz_metadata_json(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_infix rows"]), ROWS5,
                "iceberg_scan reads the rows of a table whose metadata is .gz.metadata.json")


def test_iceberg_scan_reads_a_table_whose_metadata_is_metadata_json_gz(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_suffix rows"]), ROWS5,
                "iceberg_scan reads the rows of a table whose metadata is .metadata.json.gz")


def test_a_gzip_metadata_file_past_the_cap_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["gz_bomb"]), "54000 past the cap",
                "a gzip metadata file that inflates past the 64 MiB cap is refused (54000)")


def test_a_cut_short_gzip_metadata_file_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["gz_trunc"]), "XX001 naming gzip",
                "a cut-short gzip metadata file is refused as corrupt gzip (XX001)")


def test_iceberg_current_snapshot_reads_a_two_member_gzip_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_multi snap"]), _plain_snapshot(isc),
                "iceberg_current_snapshot reads a gzip metadata file of two members as it reads the plain file")


def test_iceberg_current_snapshot_reads_a_zero_padded_gzip_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_padded snap"]), _plain_snapshot(isc),
                "iceberg_current_snapshot reads a gzip metadata file followed by zero padding")


def test_an_az_metadata_path_is_refused_as_remote(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["az"]), "0A000 remote refusal",
                "an az:// metadata path is refused as a remote URL it cannot read (0A000)")


def test_iceberg_scan_reads_a_lowercase_parquet_file_format(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["lower rows"]), ROWS5,
                "iceberg_scan reads a data file whose file_format is lowercase parquet")


def test_iceberg_scan_reads_mixed_s3_and_s3a_paths(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["mixed rows"]), ROWS5,
                "iceberg_scan reads a table whose location is s3:// and whose recorded paths are s3a://")

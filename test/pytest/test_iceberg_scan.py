"""pgcolumnar.iceberg_scan reads an Iceberg table at its current snapshot, resolving
columns by field id (#388 phase 3c). The pytest twin of `test/iceberg_scan.sh`, every
check by the shell's name (#1341).

The warehouse is relocated so its recorded root differs from where it lies. The scan
reads the current snapshot's five rows, projects and reorders by field id, reads a
subset, and still reads a column the schema renamed, because it selects by id and not
by name; a name the schema lacks is refused with 42703. A struct, list or map column
declared with a scalar type is refused with 0A000, naming the column and its kind,
through iceberg_scan and through a pgcolumnar_iceberg foreign table, instead of
reading as null on every row (#1788). After an int -> long, float -> double or
decimal precision promotion (#1789), files written before it read as the current
schema's types: alone, mixed with newer files, under equality deletes on the promoted
column, and through a pgcolumnar_iceberg foreign table. A narrower declared type is
still refused with 42804, as are a date column declared bigint and positional
read_parquet's bigint over INT32, while read_parquet by field id widens. A role with
USAGE on the schema but without pg_read_server_files is refused with 42501, and a
non-superuser holding that role reads every row.

ON A PRIVATE DATABASE, with the warehouse copied into `server_dir`, because the
backend reads it. The two roles log in on their own, as the shell's do, and are
dropped at teardown. Where the fixture is missing the first test fails under the
shell's pgc_skip message, as pgc_skip does, and the rest decline unnamed.
"""
import json
import pathlib
import re
import shutil

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_scan"
WH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse"
WHP = WH.parent / "warehouse_promo"
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


def _refused_by(dsn, sql, phrase):
    """-> the SQLSTATE when `sql` is refused with `phrase` in its message, "SQLSTATE
    (another refusal: MESSAGE)" for any other refusal, "no error" when it ran."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return exc.sqlstate if phrase in msg else f"{exc.sqlstate} (another refusal: {msg})"
    return "no error"


def _sqlstate(dsn, sql, user=None, port=None):
    target = dsn if user is None else f"host=127.0.0.1 port={port} user={user} dbname={DB}"
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


def _refusal(dsn, sql, regex, label):
    """-> "<SQLSTATE> (<label>)" when the statement raised an error whose primary
    message matches `regex`, "<SQLSTATE>: <message>" for any other error, or
    "no error: <rows>" with what it read instead."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return f"{exc.sqlstate} ({label})" if re.search(regex, msg) else f"{exc.sqlstate}: {msg}"
    return "no error: " + " ".join("|".join("" if v is None else str(v) for v in r) for r in rows)


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
    df = sorted((dest / "db" / "events" / "data").glob("*/*.parquet"))[0]
    m = json.loads(md.read_text())
    sch = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
    for f in sch["fields"]:
        if f["id"] == 3:
            f["name"] = "amt2"
    ren = md.parent / "renamed.metadata.json"
    ren.write_text(json.dumps(m))
    ren.chmod(0o666)
    # #1788: the current schema also gets a struct, a list and a map, added after
    # the data files were written, as the shell's nested.metadata.json does
    m = json.loads(md.read_text())
    sch = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
    sch["fields"] += [
        {"id": 4, "name": "st", "required": False, "type": {"type": "struct", "fields": [
            {"id": 5, "name": "a", "required": False, "type": "int"}]}},
        {"id": 6, "name": "li", "required": False, "type": {
            "type": "list", "element-id": 7, "element": "int", "element-required": False}},
        {"id": 8, "name": "mp", "required": False, "type": {
            "type": "map", "key-id": 9, "key": "string", "value-id": 10, "value": "long",
            "value-required": False}},
    ]
    m["last-column-id"] = 10
    nest = md.parent / "nested.metadata.json"
    nest.write_text(json.dumps(m))
    nest.chmod(0o666)
    pdest = server_dir / "promo"
    shutil.copytree(WHP / "db", pdest / "db")
    for p in [pdest, *pdest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    pmd = pdest / "db" / "t" / "metadata"
    pdata = pdest / "db" / "t" / "data"
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
            c.execute("CREATE SERVER icescan_nest FOREIGN DATA WRAPPER pgcolumnar_iceberg")
            c.execute("CREATE FOREIGN TABLE icescan_fnest (id bigint, st text) "
                      f"SERVER icescan_nest OPTIONS (metadata_path '{nest}')")
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
            "nest scalar": _rows(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {scan.format(nest)} "
                                      "AS t(id bigint)"),
            "nest struct": _refusal(dsn, f"SELECT * FROM {scan.format(nest)} AS t(id bigint, st text)",
                                    r'output column "st" is a struct field', "struct"),
            "nest list": _refusal(dsn, f"SELECT * FROM {scan.format(nest)} AS t(id bigint, li int4)",
                                  r'output column "li" is a list field', "list"),
            "nest map": _refusal(dsn, f"SELECT * FROM {scan.format(nest)} AS t(id bigint, mp int8)",
                                 r'output column "mp" is a map field', "map"),
            "fdw struct": _refusal(dsn, "SELECT * FROM icescan_fnest",
                                   r'output column "st" is a struct field', "struct"),
            "unpriv": _sqlstate(dsn, f"SELECT * FROM {scan.format(md)} AS t(id bigint)", UNPRIV, pgc_cluster.port),
            "reader": _rows(dsn, f"SELECT count(*) FROM {scan.format(md)} AS t(id bigint)", READER, pgc_cluster.port),
        }
        # type promotion (#1789): warehouse_promo, see gen_promotion_fixture.py
        prow = "id || '|' || k || '|' || f || '|' || d"
        pcols = "AS t(id bigint, k bigint, f double precision, d numeric(20,2))"
        old_md, mixed_md, eq_md = (pmd / f"{n}.metadata.json" for n in ("old", "mixed", "eq"))
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE SERVER ice_promo FOREIGN DATA WRAPPER pgcolumnar_iceberg")
            c.execute("CREATE FOREIGN TABLE promo_mixed (id bigint, k bigint, f double precision, d numeric(20,2)) "
                      f"SERVER ice_promo OPTIONS (metadata_path '{mixed_md}')")
            c.execute("CREATE FOREIGN TABLE promo_eq (id bigint, k bigint) "
                      f"SERVER ice_promo OPTIONS (metadata_path '{eq_md}')")
        r.update({
            "promo types": _rows(dsn, "SELECT string_agg(f || ':' || column_name || ':' || data_type, ',' "
                                      "ORDER BY f, field_id) FROM (VALUES ('old'), ('old2'), ('new')) AS v(f), "
                                      f"LATERAL pgcolumnar.parquet_schema('{pdata}/' || f || '.parquet') "
                                      "WHERE field_id BETWEEN 2 AND 4"),
            "promo long": _rows(dsn, "SELECT string_agg(id || ':' || k, ',' ORDER BY id) "
                                     f"FROM {scan.format(old_md)} AS t(id bigint, k bigint)"),
            "promo double": _rows(dsn, "SELECT string_agg(f::text, ',' ORDER BY id) "
                                       f"FROM {scan.format(old_md)} AS t(id bigint, f double precision)"),
            "promo decimal": _rows(dsn, "SELECT string_agg(d::text, ',' ORDER BY id) "
                                        f"FROM {scan.format(old_md)} AS t(id bigint, d numeric(20,2))"),
            "promo mixed": _rows(dsn, f"SELECT {prow} FROM {scan.format(mixed_md)} {pcols} ORDER BY id"),
            "promo eq": _rows(dsn, "SELECT string_agg(id || ':' || k, ',' ORDER BY id) "
                                   f"FROM {scan.format(eq_md)} AS t(id bigint, k bigint)"),
            "promo eq unprojected": _rows(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) "
                                               f"FROM {scan.format(eq_md)} AS t(id bigint)"),
            "promo fdw mixed": _rows(dsn, f"SELECT {prow} FROM promo_mixed ORDER BY id"),
            "promo fdw eq": _rows(dsn, "SELECT string_agg(id || ':' || k, ',' ORDER BY id) FROM promo_eq"),
            "promo narrow int": _refused_by(dsn, f"SELECT * FROM {scan.format(mixed_md)} AS t(k int)",
                                            'has a physical type incompatible with output column "k"'),
            "promo narrow real": _refused_by(dsn, f"SELECT * FROM {scan.format(mixed_md)} AS t(f real)",
                                             'has a physical type incompatible with output column "f"'),
            "promo date": _refused_by(dsn, f"SELECT * FROM {scan.format(old_md)} AS t(dt bigint)",
                                      'has a physical type incompatible with output column "dt"'),
            "promo by id": _rows(dsn, "SELECT string_agg(k::text, ',' ORDER BY k) FROM "
                                      f"pgcolumnar.read_parquet('{pdata}/old2.parquet', ARRAY[2]) AS t(k bigint)"),
            "promo positional": _refused_by(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{pdata}/old2.parquet') "
                                                 "AS t(id bigint, k bigint, f real, d numeric(10,2), dt date)",
                                            'physical type is not compatible with target column "k"'),
        })
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


def test_iceberg_scan_reads_the_scalars_beside_nested_fields(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["nest scalar"]), "1,2,3,4,5",
                "iceberg_scan still reads the scalar columns beside nested fields")


def test_iceberg_scan_refuses_a_struct_column_declared_text(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["nest struct"], "0A000 (struct)", "iceberg_scan refuses a struct column declared text (0A000)")


def test_iceberg_scan_refuses_a_list_column_declared_int4(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["nest list"], "0A000 (list)", "iceberg_scan refuses a list column declared int4 (0A000)")


def test_iceberg_scan_refuses_a_map_column_declared_int8(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["nest map"], "0A000 (map)", "iceberg_scan refuses a map column declared int8 (0A000)")


def test_an_iceberg_foreign_table_refuses_a_struct_column_declared_text(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["fdw struct"], "0A000 (struct)",
                "a pgcolumnar_iceberg foreign table refuses a struct column declared text (0A000)")


OLD_ROWS = ("1|-2147483648|1.5|1.25\n2|-1|-2.25|-3.50\n3|0|0.5|0.00\n4|2|1024.75|99999999.99\n"
            "5|2147483647|3.4028234663852886e+38|-0.01\n6|7|-0.125|10.10\n7|2|2|2.00")
NEW_ROWS = "8|5000000000|3.25|123456789012345678.90\n9|2|0.1|-1.00"


def test_the_promotion_fixture_stores_the_old_and_new_physical_types(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo types"]),
                "new:k:bigint,new:f:double precision,new:d:numeric(20,2),old:k:integer,old:f:real,"
                "old:d:numeric(10,2),old2:k:integer,old2:f:real,old2:d:numeric(10,2)",
                "premise: the old files store k, f, d as INT32, FLOAT and decimal(10,2), "
                "the new file as INT64, DOUBLE and decimal(20,2)")


def test_a_promoted_int_column_reads_old_files_as_bigint(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo long"]), "1:-2147483648,2:-1,3:0,4:2,5:2147483647,6:7,7:2",
                "a promoted int -> long column reads the pre-promotion files as bigint")


def test_a_promoted_float_column_reads_old_files_as_double(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo double"]), "1.5,-2.25,0.5,1024.75,3.4028234663852886e+38,-0.125,2",
                "a promoted float -> double column reads the pre-promotion files as double precision")


def test_a_promoted_decimal_column_reads_old_files(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo decimal"]), "1.25,-3.50,0.00,99999999.99,-0.01,10.10,2.00",
                "a promoted decimal(10,2) -> decimal(20,2) column reads the pre-promotion files")


def test_old_and_new_files_bind_to_the_promoted_types(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo mixed"]), OLD_ROWS + "\n" + NEW_ROWS,
                "old and new files both bind to the promoted types in one snapshot")


def test_an_equality_delete_on_a_promoted_column_applies(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo eq"]), "1:-2147483648,2:-1,5:2147483647,6:7,8:5000000000",
                "an equality delete on a promoted column applies to old and new files")


def test_an_equality_delete_on_an_unprojected_promoted_column_applies(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo eq unprojected"]), "1,2,5,6,8",
                "an equality delete on a promoted column applies when that column is not projected")


def test_a_foreign_table_reads_the_promoted_types(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo fdw mixed"]), OLD_ROWS + "\n" + NEW_ROWS,
                "a pgcolumnar_iceberg foreign table reads old and new files as the promoted types")


def test_a_foreign_table_applies_an_equality_delete_on_a_promoted_column(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo fdw eq"]), "1:-2147483648,2:-1,5:2147483647,6:7,8:5000000000",
                "a pgcolumnar_iceberg foreign table applies an equality delete on a promoted column")


def test_a_column_declared_int_over_an_int64_file_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo narrow int"]), "42804",
                "a column declared int over a post-promotion INT64 file is refused (42804)")


def test_a_column_declared_real_over_a_double_file_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo narrow real"]), "42804",
                "a column declared real over a post-promotion DOUBLE file is refused (42804)")


def test_a_date_column_declared_bigint_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo date"]), "42804", "a date column declared bigint is refused, not widened (42804)")


def test_read_parquet_by_field_id_widens_an_int32_column(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo by id"]), "2,7", "read_parquet by field id reads an INT32 column as bigint")


def test_positional_read_parquet_keeps_its_exact_type_map(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo positional"]), "42804",
                "positional read_parquet still refuses bigint over an INT32 column (42804)")

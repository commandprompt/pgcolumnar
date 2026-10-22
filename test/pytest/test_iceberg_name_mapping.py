"""Iceberg name mapping binds an id-less data file by name (#388). The pytest twin of
`test/iceberg_name_mapping.sh`, every check by the shell's name (#1341).

An Iceberg data file written outside Iceberg carries no Parquet field ids; iceberg_scan
reads it by binding its columns by name through the table's schema.name-mapping.default
property. The fixture is hand-crafted (test/fixtures/iceberg/warehouse_nm): a real
id-less pyarrow file in three row groups, and one metadata file per arm.

VALUES ARE READ AGAINST THE FIXTURE'S OWN ROWS, every row: a check that read the first
row passed with text columns read as null past the first row group. A REFUSAL reads as
its SQLSTATE. A SURVIVAL check also wants the server log's count of killed backends
unchanged since its section began, because a fresh connection answers again once crash
recovery is over. The SOURCE check reads the file with its comments blanked out, so a
comment quoting the line does not count. Every reading is a value: a failed query is a
query_error() that reds its own check by name.

ON A PRIVATE DATABASE, with the fixtures in `server_dir`. Where the fixture is missing
the first test fails under the shell's pgc_skip message, as pgc_skip does, and the rest
decline.
"""
import json
import pathlib
import re
import shutil
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_name_mapping"
WHN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse_nm"
ICE_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "columnar_iceberg.c"
COLS = "AS t(id bigint, region text, amount int)"
CAP = 100000
ROWS_ALL = "1|eu|10,2|eu|20,3|us|30,4|us|40,5|us|50"
REGIONS_ALL = "1:eu,2:eu,3:us,4:us,5:us"
CAP_LINE = "cap = Max(Min(ne, (uint32) ICE_MAX_NAME_MAPPING), 1)"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _one(dsn, sql):
    """-> the first column of the first row as text ("" for none or NULL), or the
    failed query as query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


def _refusal(dsn, sql):
    """-> (SQLSTATE, the error's message, detail and hint joined), ("", "") when the
    statement ran, or a query_error() pair when the server could not be reached."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.OperationalError as exc:
        if exc.sqlstate is None:
            e = query_error(_message(exc))
            return e, e
        d = exc.diag
        return exc.sqlstate, " ".join(x for x in (d.message_primary, d.message_detail, d.message_hint) if x)
    except psycopg.Error as exc:
        d = exc.diag
        return exc.sqlstate or "", " ".join(x for x in (d.message_primary, d.message_detail, d.message_hint) if x)
    return "", ""


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    up, now = _one(dsn, "SELECT 1"), _crashes(cluster)
    if up != "1":
        # after a crash, let recovery finish before the next section reads, so its
        # checks red on their own arms and not as collateral of this one
        for _ in range(60):
            if _one(dsn, "SELECT 1") == "1":
                break
            time.sleep(1)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


def _code_lines(path, text):
    """-> how many lines of PATH, its comments blanked out, contain TEXT, as grep -c
    counts them; a query_error() when the file cannot be read."""
    try:
        s = path.read_text()
    except OSError as exc:
        return query_error(f"source unreadable: {exc}")
    s = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", " ", s, flags=re.S))
    return str(sum(1 for line in s.splitlines() if text in line))


def _write_caps(mdir):
    """nmcap_at: the real mapping plus one entry of CAP - 3 filler names, exactly the
    cap; nmcap_over: one name more."""
    base = json.loads((mdir / "nmapply.metadata.json").read_text())
    real = json.loads(base["properties"]["schema.name-mapping.default"])
    for tag, extra in (("nmcap_at", CAP - 3), ("nmcap_over", CAP - 2)):
        md = json.loads(json.dumps(base))
        md["properties"]["schema.name-mapping.default"] = json.dumps(
            real + [{"field-id": 1, "names": ["f%d" % i for i in range(extra)]}])
        p = mdir / f"{tag}.metadata.json"
        p.write_text(json.dumps(md))
        p.chmod(0o666)


@pytest.fixture(scope="module")
def nmap(pgc_cluster, server_dir):
    if not (WHN / "db" / "t" / "metadata" / "nmapply.metadata.json").exists():
        yield None
        return
    dest = server_dir / "nmap"
    shutil.copytree(WHN / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    mdir = dest / "db" / "t" / "metadata"
    data = dest / "db" / "t" / "data" / "data.parquet"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")

        def scan(md, cols=COLS):
            return f"pgcolumnar.iceberg_scan('{mdir}/{md}.metadata.json') {cols}"

        r = {"no ids": _one(dsn, f"SELECT count(*) FROM pgcolumnar.parquet_schema('{data}') "
                                 f"WHERE field_id IS NOT NULL")}

        before = _crashes(pgc_cluster)
        r["apply count"] = _one(dsn, f"SELECT count(*) FROM {scan('nmapply')}")
        r["apply values"] = _one(dsn, f"SELECT string_agg(id || '|' || region || '|' || amount, ',' ORDER BY id) "
                                      f"FROM {scan('nmapply')}")
        r["rename"] = _one(dsn, f"SELECT string_agg(ident::text, ',' ORDER BY ident) "
                                f"FROM {scan('nmrename', 'AS t(ident bigint, region text, amount int)')}")
        r["alias"] = _one(dsn, f"SELECT count(*) FROM {scan('nmalias')} WHERE id IN (1,2,3,4,5)")
        r["subset"] = _one(dsn, f"SELECT string_agg(amount::text, ',' ORDER BY amount) "
                                f"FROM {scan('nmsubset', 'AS t(id bigint, amount int)')}")
        r["up reads"] = _survived(dsn, pgc_cluster, before)

        before = _crashes(pgc_cluster)
        r["nomap"] = _refusal(dsn, f"SELECT * FROM {scan('nmnomap')}")
        r["dup"] = _refusal(dsn, f"SELECT * FROM {scan('nmdup')}")
        r["bigid"] = _refusal(dsn, f"SELECT * FROM {scan('nmbigid')}")
        r["up refusals"] = _survived(dsn, pgc_cluster, before)

        before = _crashes(pgc_cluster)
        r["missing count"] = _one(dsn, f"SELECT count(*) FROM {scan('nmmissing')}")
        r["missing regions"] = _one(dsn, f"SELECT string_agg(id || ':' || region, ',' ORDER BY id) "
                                         f"FROM {scan('nmmissing')} WHERE amount IS NULL")
        r["evolve ids"] = _one(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) "
                                    f"FROM {scan('nmevolve')} WHERE amount IS NULL")
        r["evolve regions"] = _one(dsn, f"SELECT string_agg(id || ':' || region, ',' ORDER BY id) "
                                        f"FROM {scan('nmevolve')}")
        r["up nullfill"] = _survived(dsn, pgc_cluster, before)

        before = _crashes(pgc_cluster)
        _write_caps(mdir)
        r["cap at"] = _one(dsn, f"SELECT count(*) FROM {scan('nmcap_at')}")
        r["cap over"] = _refusal(dsn, f"SELECT * FROM {scan('nmcap_over')}")
        r["cap source"] = _code_lines(ICE_SRC, CAP_LINE)
        r["up cap"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(nmap, expect):
    if nmap is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the name-mapping fixtures are missing")
    return True


def test_name_mapping_data_file_carries_no_field_ids(nmap, expect):
    if nmap is None:
        expect.text("missing", "present", "name-mapping fixtures are missing")
        return
    expect.require_module("json", name="python3 is needed")
    expect.text(nmap["no ids"], "0", "premise: the data file carries no field ids")


def test_name_mapping_reads_an_id_less_file(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["apply count"], "5", "an id-less data file reads by name mapping (all 5 rows)")


def test_name_mapping_values_are_correct(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["apply values"], ROWS_ALL, "the mapped values are correct")


def test_name_mapping_renamed_schema_column(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["rename"], "1,2,3,4,5", "a renamed schema column still reads via the mapping name")


def test_name_mapping_alias_resolves(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["alias"], "5", "an alias name in the mapping resolves the file column")


def test_name_mapping_under_projection(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["subset"], "10,20,30,40,50", "name mapping applies under column projection (id, amount)")


def test_name_mapping_backend_up_after_reads(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["up reads"], "1", "backend still up after the name-mapping reads")


def test_name_mapping_no_mapping_is_refused(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["nomap"][0], "22023", "an id-less file with no name mapping is refused (22023)")


def test_name_mapping_refusal_names_the_property(nmap, expect):
    if _declined(nmap, expect):
        return
    text = nmap["nomap"][1]
    expect.text("1" if "schema.name-mapping.default" in text else f"0 ({text})", "1",
                "...and the refusal names schema.name-mapping.default")


def test_name_mapping_duplicate_name_is_refused(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["dup"][0], "XX001", "a duplicate name in the mapping is refused (XX001)")


def test_name_mapping_big_field_id_is_refused(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["bigid"][0], "XX001", "a mapping field-id beyond int32 is refused (XX001)")


def test_name_mapping_backend_up_after_refusals(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["up refusals"], "1", "backend still up after the name-mapping refusals")


def test_name_mapping_unmapped_column_reads_null(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["missing count"], "5", "a projected column absent from the mapping reads null, not an error")


def test_name_mapping_unmapped_null_mapped_real(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["missing regions"], REGIONS_ALL, "the unmapped column is null and the mapped columns are real")


def test_name_mapping_added_column_reads_null(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["evolve ids"], "1,2,3,4,5",
                "a column added after the file was written reads null (schema evolution)")


def test_name_mapping_evolved_columns_are_real(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["evolve regions"], REGIONS_ALL, "...and its other columns still carry real data")


def test_name_mapping_backend_up_after_null_fill(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["up nullfill"], "1", "backend still up after the null-fill reads")


def test_name_mapping_at_the_cap_is_accepted(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["cap at"], "5", "a mapping with exactly the cap in names is accepted")


def test_name_mapping_over_the_cap_is_refused(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["cap over"][0], "54000", "one name over the cap is refused")


def test_name_mapping_cap_refusal_names_the_cap(nmap, expect):
    if _declined(nmap, expect):
        return
    text = nmap["cap over"][1]
    expect.text("1" if f"more than {CAP} names" in text else f"0 ({text})", "1", "...and the refusal names the cap")


def test_name_mapping_opening_capacity_is_bounded(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["cap source"], "1", "the opening capacity is bounded by the cap, not by the declared count")


def test_name_mapping_backend_up_after_cap(nmap, expect):
    if _declined(nmap, expect):
        return
    expect.text(nmap["up cap"], "1", "backend still up after the cap reads")

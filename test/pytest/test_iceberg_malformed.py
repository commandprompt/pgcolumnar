"""The Iceberg reader refuses crafted metadata, manifests and manifest lists cleanly,
never crashing or hanging the backend (#388 phase 4a, #644). The pytest twin of
`test/iceberg_malformed.sh`, every check by the shell's name (#1341).

The hand-crafted fixtures under test/fixtures/iceberg/warehouse_malformed are copied
into `server_dir`, so the recorded root differs from the actual one and every read
goes through the same path rebasing the honest read path uses. Each refusal is read
as "<SQLSTATE> (<label>)" only when the error's primary message is the refusal under
test: every arm refuses with XX001, and so do later parse steps, so a FIFO read as an
empty file passed on its SQLSTATE alone with the regular-file guard removed. A FIFO
read that is still running after 5s is a HANG; its write end is then opened, so the
backend is released. A manifest enum value outside the spec (data_file.content,
manifest_entry.status, manifest_file.content) is refused in the scan and the lister,
naming the value, against a two-file control that reads ten rows (#1823).

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED. A fresh connection answers
again after crash recovery, so every survival check also reads the server log's count
of killed backends since the fixture began, and an unreadable log fails it.

ON A PRIVATE DATABASE; each test asserts one reading (#1339).
"""
import json
import os
import pathlib
import re
import shutil
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_malformed"
WHM = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse_malformed"
NEEDED = ("nullpath/db/t/metadata/t.metadata.json", "nullseq_ml/ml.avro", "nullminseq_ml/ml.avro",
          "nullmanifestpath/db/t/metadata/t.metadata.json", "danglingschema/db/t/metadata/t.metadata.json",
          "enum_ctl/db/t/metadata/t.metadata.json")
# the refusal each arm wants, matched against the primary message as the shell does
SCHEMA = (r"unsupported or malformed Avro schema in manifest", "malformed schema")
NOPATH = (r"has no data file path", "no data file path")
NULLMP = (r"a manifest path in .* is null", "null manifest path")
FIFO_REFUSAL = (r"is not a regular file", "not a regular file")
NULLSEQ = (r"has a null sequence number that an added entry", "null sequence to inherit")
DANGLING = (r"is not present in the schemas array", "dangling schema id")
# #1824: malformed numbers in metadata.json
EVENTS = WHM.parent / "warehouse" / "db"
EVENTS_MD = "00001-6808fe8c-acd3-4fbf-9fbe-1ac447a16b3c.metadata.json"
FIDRANGE = (r'schema field "amount" has a field id out of range: 4294967297', "field id out of range")
NONINT = (r"holds the non-integer number 1.4 where an integer is required", "non-integer number")
FV99 = (r"has an unsupported format-version 99", "unsupported format-version")
FV4 = (r"has an unsupported format-version 4", "unsupported format-version")
FV0 = (r"has an unsupported format-version 0", "unsupported format-version")
CSIDX = (r"has a current-snapshot-id that is not an integer", "non-integer current-snapshot-id")
OLD3 = (r'manifest entry ".*/old\.parquet" has invalid content 3$', "invalid content 3")
OLDM1 = (r'manifest entry ".*/old\.parquet" has invalid content -1$', "invalid content -1")
NEW3 = (r'manifest entry ".*/new\.parquet" has invalid content 3$', "invalid content 3")
STATUS3 = (r'manifest entry ".*/old\.parquet" has invalid status 3$', "invalid status 3")
MF5 = (r'manifest ".*/dm\.avro" has invalid content 5$', "invalid manifest content 5")
OLD4G = (r'manifest entry has invalid content 4294967296$', "invalid content 4294967296")
STATUS4G = (r'manifest entry has invalid status 4294967297$', "invalid status 4294967297")
MF4G = (r'manifest ".*/dm\.avro" has invalid content 4294967296$', "invalid manifest content 4294967296")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    """-> the first column of the first row as the shell's psql prints it, or the
    failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    v = row[0]
    return "t" if v is True else "f" if v is False else str(v)


def _reading(exc, want):
    """-> "<SQLSTATE> (<label>)" when the primary message is the wanted refusal,
    "<SQLSTATE>: <message>" for any other error."""
    if not exc.sqlstate:
        return "no refusal: the connection was lost"
    msg = exc.diag.message_primary or ""
    regex, label = want
    return f"{exc.sqlstate} ({label})" if re.search(regex, msg) else f"{exc.sqlstate}: {msg}"


def _refusal(dsn, sql, want):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return _reading(exc, want)
    return "no error"


def _refusal_or_hang(dsn, sql, fifo):
    """-> the refusal reading, or HANG when the statement is still running after 5s;
    a hung backend is released by opening the FIFO's write end."""
    result = {}

    def run():
        result["v"] = _refusal(dsn, sql, FIFO_REFUSAL)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        t.join(10)
        return "HANG"
    return result.get("v", query_error("the statement left no outcome"))


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    """-> "1" when the server answers and no backend died since `before`, read just
    before the arm ran; after a crash, waits out the recovery so the arms after it
    are not collateral."""
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    for _ in range(60):
        if _value(dsn, "SELECT 1") == "1":
            break
        time.sleep(0.5)
    return f"no ([{up if up == '1' else ''}], {now - before} backend crashes)"


def _fixtures_present():
    return all((WHM / f).is_file() for f in NEEDED)


@pytest.fixture(scope="module")
def imf(pgc_cluster, server_dir):
    if not _fixtures_present():
        yield None
        return
    dest = server_dir / "malformed"
    shutil.copytree(WHM, dest)
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    fifodir = server_dir / "fifo"
    fifodir.mkdir()
    fifodir.chmod(0o777)
    fifo = fifodir / "x"
    os.mkfifo(fifo)
    os.chmod(fifo, 0o666)
    mlroot = fifodir / "tbl"
    (mlroot / "db" / "t" / "metadata").mkdir(parents=True)
    for p in [mlroot, *mlroot.rglob("*")]:
        p.chmod(0o777)
    mlfifo = mlroot / "db" / "t" / "metadata" / "ml.avro"
    os.mkfifo(mlfifo)
    os.chmod(mlfifo, 0o666)
    mdjson = mlroot / "db" / "t" / "metadata" / "t.metadata.json"
    mdjson.write_text(json.dumps({
        "format-version": 2, "location": f"file://{mlroot}/db/t",
        "current-snapshot-id": 4242, "current-schema-id": 0,
        "schemas": [{"schema-id": 0, "type": "struct", "fields": []}],
        "snapshots": [{"snapshot-id": 4242, "timestamp-ms": 0, "sequence-number": 1, "schema-id": 0,
                       "manifest-list": f"file://{mlroot}/db/t/metadata/ml.avro",
                       "summary": {"operation": "append"}}]}))
    mdjson.chmod(0o666)

    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        crashes = lambda: _crashes(pgc_cluster)  # noqa: E731
        alive = lambda b: _survived(dsn, pgc_cluster, b)  # noqa: E731
        md = lambda d: dest / d / "db" / "t" / "metadata" / "t.metadata.json"  # noqa: E731
        scan3 = "AS t(id bigint, region text, amount int)"
        r = {}
        b = crashes()
        r["array refused"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_avro_manifest('{dest}/arrayfield/evil.avro')", SCHEMA)
        r["array up"] = alive(b)
        r["array control"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.read_avro_manifest('{dest}/arrayfield/good.avro')")
        b = crashes()
        r["np lister"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_data_files('{md('nullpath')}')", NOPATH)
        r["np lister up"] = alive(b)
        b = crashes()
        r["np scan"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_scan('{md('nullpath')}') AS t(id bigint)", NOPATH)
        r["np scan up"] = alive(b)
        r["np control"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_data_files('{md('nullpath_ctl')}')")
        b = crashes()
        r["nmp"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_data_files('{md('nullmanifestpath')}')", NULLMP)
        r["nmp up"] = alive(b)
        b = crashes()
        for key, sql in (("fifo snapshot", f"SELECT * FROM pgcolumnar.iceberg_current_snapshot('{fifo}')"),
                         ("fifo lister", f"SELECT * FROM pgcolumnar.iceberg_data_files('{fifo}')"),
                         ("fifo scan", f"SELECT * FROM pgcolumnar.iceberg_scan('{fifo}') AS t(id bigint)"),
                         ("fifo manifest", f"SELECT * FROM pgcolumnar.read_avro_manifest('{fifo}')"),
                         ("fifo list", f"SELECT * FROM pgcolumnar.read_manifest_list('{fifo}')"),
                         ("fifo parquet", f"SELECT * FROM pgcolumnar.read_parquet('{fifo}') AS t(id bigint)")):
            r[key] = _refusal_or_hang(dsn, sql, fifo)
        r["fifo up"] = alive(b)
        b = crashes()
        r["fifo ml"] = _refusal_or_hang(dsn, f"SELECT * FROM pgcolumnar.iceberg_data_files('{mdjson}')", mlfifo)
        r["fifo ml up"] = alive(b)
        r["seq null"] = _value(dsn, f"SELECT sequence_number IS NULL FROM pgcolumnar.read_manifest_list('{dest}/nullseq_ml/ml.avro')")
        r["seq ctl"] = _value(dsn, f"SELECT sequence_number FROM pgcolumnar.read_manifest_list('{dest}/nullseq_ml/ml_ctl.avro')")
        r["minseq null"] = _value(dsn, f"SELECT min_sequence_number IS NULL FROM pgcolumnar.read_manifest_list('{dest}/nullminseq_ml/ml.avro')")
        r["minseq ctl"] = _value(dsn, f"SELECT min_sequence_number FROM pgcolumnar.read_manifest_list('{dest}/nullminseq_ml/ml_ctl.avro')")
        b = crashes()
        r["seq refused"] = _refusal(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{md('nullseq')}') {scan3}", NULLSEQ)
        r["seq up"] = alive(b)
        r["seq control"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{md('nullseq_ctl')}') {scan3}")
        b = crashes()
        r["dangling"] = _refusal(dsn, f"SELECT amount FROM pgcolumnar.iceberg_scan('{md('danglingschema')}') AS t(amount int)", DANGLING)
        r["dangling up"] = alive(b)
        r["dangling control"] = _value(dsn, "SELECT string_agg(amount::text, ',' ORDER BY amount) "
                                            f"FROM pgcolumnar.iceberg_scan('{md('danglingschema_ctl')}') AS t(amount int)")
        r["legacy rows"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{md('legacyschema')}') {scan3}")
        r["legacy amount"] = _value(dsn, "SELECT string_agg(amount::text, ',' ORDER BY amount) "
                                         f"FROM pgcolumnar.iceberg_scan('{md('legacyschema')}') {scan3}")
        # #1824: edited copies of the db/events fixture (id 1, region 2, amount 3)
        lax = server_dir / "lax"
        shutil.copytree(EVENTS, lax / "db")
        for p in [lax, *lax.rglob("*")]:
            p.chmod(0o777 if p.is_dir() else 0o666)
        laxmd = lax / "db" / "events" / "metadata"
        src = laxmd / EVENTS_MD

        def variant(name, edit):
            m = json.loads(src.read_text())
            edit(m)
            out = laxmd / f"{name}.metadata.json"
            out.write_text(json.dumps(m))
            out.chmod(0o666)

        def amount_id(v):
            def edit(m):
                for sch in m["schemas"]:
                    for f in sch["fields"]:
                        if f["name"] == "amount":
                            f["id"] = v
            return edit
        variant("bigid", amount_id(4294967297))
        variant("fracid", amount_id(1.4))
        variant("fv99", lambda m: m.__setitem__("format-version", 99))
        variant("csidx", lambda m: m.__setitem__("current-snapshot-id", "x"))
        variant("csidneg", lambda m: m.__setitem__("current-snapshot-id", -1))
        variant("fv4", lambda m: m.__setitem__("format-version", 4))
        variant("fv0", lambda m: m.__setitem__("format-version", 0))
        variant("csidnull", lambda m: m.__setitem__("current-snapshot-id", None))
        r["lax control"] = _value(dsn, "SELECT string_agg(amount::text, ',' ORDER BY id) "
                                       f"FROM pgcolumnar.iceberg_scan('{src}') AS t(id bigint, amount int)")
        b = crashes()
        r["lax bigid"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_scan('{laxmd}/bigid.metadata.json') "
                                       "AS t(amount bigint)", FIDRANGE)
        r["lax bigid both"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_scan('{laxmd}/bigid.metadata.json') "
                                            "AS t(id bigint, amount bigint)", FIDRANGE)
        r["lax fracid"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_scan('{laxmd}/fracid.metadata.json') "
                                        "AS t(amount bigint)", NONINT)
        r["lax fv99"] = _refusal(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{laxmd}/fv99.metadata.json') "
                                      "AS t(id bigint)", FV99)
        r["lax fv4"] = _refusal(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{laxmd}/fv4.metadata.json') "
                                     "AS t(id bigint)", FV4)
        r["lax fv0"] = _refusal(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{laxmd}/fv0.metadata.json') "
                                     "AS t(id bigint)", FV0)
        r["lax csidx"] = _refusal(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{laxmd}/csidx.metadata.json') "
                                       "AS t(id bigint)", CSIDX)
        r["lax up"] = alive(b)
        r["lax csidneg"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{laxmd}/csidneg.metadata.json') "
                                       "AS t(id bigint)")
        r["lax csidnull"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{laxmd}/csidnull.metadata.json') "
                                        "AS t(id bigint)")
        # #1823: enum_ctl with one data_file.content, manifest_entry.status or
        # manifest_file.content outside the spec
        scan = lambda d: f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{md(d)}') {scan3}"  # noqa: E731
        lst = lambda d: f"SELECT * FROM pgcolumnar.iceberg_data_files('{md(d)}')"  # noqa: E731
        r["enum ctl scan"] = _value(dsn, "SELECT count(*) || '|' || min(id) || '|' || max(id) "
                                         f"FROM pgcolumnar.iceberg_scan('{md('enum_ctl')}') {scan3}")
        r["enum ctl list"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_data_files('{md('enum_ctl')}')")
        b = crashes()
        r["old3 scan"] = _refusal(dsn, scan("content3_old"), OLD3)
        r["oldm1 scan"] = _refusal(dsn, scan("contentm1_old"), OLDM1)
        r["new3 scan"] = _refusal(dsn, scan("content3_new"), NEW3)
        r["old3 list"] = _refusal(dsn, lst("content3_old"), OLD3)
        r["status3 scan"] = _refusal(dsn, scan("status3_old"), STATUS3)
        r["status3 list"] = _refusal(dsn, lst("status3_old"), STATUS3)
        r["mf5 scan"] = _refusal(dsn, scan("mfcontent5"), MF5)
        r["mf5 list"] = _refusal(dsn, lst("mfcontent5"), MF5)
        # outside int32: a cast before the range check wrapped these to 0 and 1
        r["old4g scan"] = _refusal(dsn, scan("content4g_old"), OLD4G)
        r["status4g scan"] = _refusal(dsn, scan("status4g_old"), STATUS4G)
        r["mf4g scan"] = _refusal(dsn, scan("mfcontent4g"), MF4G)
        r["enum up"] = alive(b)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(imf, expect):
    if imf is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the malformed iceberg fixtures are missing")
    return True


def test_the_malformed_iceberg_fixtures_are_present(expect):
    """The shell skips the whole suite without the committed fixtures, under this name."""
    if not _fixtures_present():
        expect.cannot_run("MISSING_DEPENDENCY", "the committed malformed Iceberg fixtures are not in this tree",
                          name="malformed iceberg fixtures are missing")
        return
    expect.at_least((WHM / NEEDED[0]).stat().st_size, 1,
                    "premise: the committed malformed fixtures are not empty")


def test_python_reads_the_malformed_metadata(expect):
    """The shell's other skip name: it needs python3 with json."""
    try:
        import json as _json
    except ImportError:
        expect.cannot_run("MISSING_DEPENDENCY", "json is not importable", name="python3 is needed")
        return
    expect.at_least(len(_json.loads((WHM / NEEDED[4]).read_text())), 1,
                    "premise: the dangling-schema metadata parses as JSON")


def test_an_array_valued_fields_element_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["array refused"], "XX001 (malformed schema)",
                "an array-valued schema fields element is refused, not crashed (XX001)")


def test_the_backend_survives_the_malformed_schema(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["array up"], "1", "backend still up after the malformed-schema refusal")


def test_a_well_formed_field_object_still_decodes(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["array control"], "0", "control: a well-formed field object still decodes (0 entries)")


def test_the_lister_refuses_a_null_data_file_path(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["np lister"], "XX001 (no data file path)",
                "a NULL data-file path is refused in iceberg_data_files (XX001, not a crash)")


def test_the_backend_survives_the_lister_refusal(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["np lister up"], "1", "backend still up after the lister refusal")


def test_the_scan_refuses_a_null_data_file_path(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["np scan"], "XX001 (no data file path)",
                "a NULL data-file path is refused in iceberg_scan (XX001, not a crash)")


def test_the_backend_survives_the_scan_refusal(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["np scan up"], "1", "backend still up after the scan refusal")


def test_a_present_file_path_lists_one_data_file(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["np control"], "1", "control: a present file_path lists one data file")


def test_a_null_manifest_path_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["nmp"], "XX001 (null manifest path)", "a NULL manifest_path is refused, not a crash (XX001)")


def test_the_backend_survives_the_null_manifest_path(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["nmp up"], "1", "backend still up after the null-manifest_path refusal")


def test_iceberg_current_snapshot_refuses_a_fifo(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo snapshot"], "XX001 (not a regular file)",
                "iceberg_current_snapshot on a FIFO is refused, not a hang (XX001)")


def test_iceberg_data_files_refuses_a_fifo(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo lister"], "XX001 (not a regular file)",
                "iceberg_data_files on a FIFO is refused, not a hang (XX001)")


def test_iceberg_scan_refuses_a_fifo(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo scan"], "XX001 (not a regular file)", "iceberg_scan on a FIFO is refused, not a hang (XX001)")


def test_read_avro_manifest_refuses_a_fifo_as_iceberg_input(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo manifest"], "XX001 (not a regular file)",
                "read_avro_manifest on a FIFO is refused, not a hang (XX001)")


def test_read_manifest_list_refuses_a_fifo(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo list"], "XX001 (not a regular file)",
                "read_manifest_list on a FIFO is refused, not a hang (XX001)")


def test_read_parquet_refuses_a_fifo_beside_iceberg(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo parquet"], "XX001 (not a regular file)", "read_parquet on a FIFO is refused, not a hang (XX001)")


def test_the_backend_survives_the_fifo_refusals(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo up"], "1", "backend still up after the FIFO refusals")


def test_a_manifest_list_that_is_a_fifo_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo ml"], "XX001 (not a regular file)",
                "a manifest-list that is a FIFO is refused, not a hang (XX001)")


def test_the_backend_survives_the_manifest_list_fifo(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["fifo ml up"], "1", "backend still up after the manifest-list FIFO refusal")


def test_a_null_manifest_list_sequence_number_reads_null(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["seq null"], "t", "a null manifest-list sequence_number decodes as NULL, not 0")


def test_a_present_manifest_list_sequence_number_reads_its_value(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["seq ctl"], "5", "control: a present manifest-list sequence_number decodes to its value")


def test_a_null_min_sequence_number_reads_null(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["minseq null"], "t", "a null manifest-list min_sequence_number decodes as NULL, not 0 (#686)")


def test_a_present_min_sequence_number_reads_its_value(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["minseq ctl"], "7", "control: a present min_sequence_number decodes to its value (#686)")


def test_the_scan_refuses_a_null_sequence_an_added_entry_inherits(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["seq refused"], "XX001 (null sequence to inherit)",
                "iceberg_scan refuses a null manifest seq an ADDED entry must inherit (XX001, not a silent wrong row)")


def test_the_backend_survives_the_null_sequence_refusal(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["seq up"], "1", "backend still up after the null-sequence refusal")


def test_a_concrete_manifest_sequence_scans_every_row(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["seq control"], "5",
                "control: a concrete manifest seq scans all 5 rows (seq-3 delete is older than seq-5 data)")


def test_a_dangling_current_schema_id_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["dangling"], "XX001 (dangling schema id)",
                "a dangling current-schema-id is refused (XX001), not silently stale-read")


def test_the_backend_survives_the_dangling_schema_refusal(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["dangling up"], "1", "backend still up after the dangling-schema refusal")


def test_a_resolvable_current_schema_id_reads_amount(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["dangling control"], "10,20,30,40,50",
                "control: a resolvable current-schema-id reads the correct amount column")


def test_a_legacy_only_schema_table_still_reads(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["legacy rows"], "5", "control: a legacy-only-schema table still reads (5 rows)")


def test_the_legacy_table_binds_amount(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["legacy amount"], "10,20,30,40,50", "control: the legacy table binds amount correctly (10..50)")


# ---- #1824: malformed numbers in metadata.json -------------------------------
# A field id above INT32 truncated to another field's id, a fraction rounded, so
# "amount" returned the "id" column. format-version 99 read as v2, and a string
# current-snapshot-id read as an empty table.

def test_the_unedited_events_metadata_reads_amount(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax control"], "10,20,30,40,50", "control: the unedited events metadata reads amount 10..50")


def test_a_field_id_above_int32_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax bigid"], "XX001 (field id out of range)",
                "a field id above INT32 is refused (XX001), not aliased to another field")


def test_a_field_id_above_int32_is_named_beside_its_alias(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax bigid both"], "XX001 (field id out of range)",
                "a field id above INT32 is named even when its alias is also queried")


def test_a_fractional_field_id_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax fracid"], "XX001 (non-integer number)", "a fractional field id is refused (XX001), not rounded")


def test_format_version_99_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax fv99"], "0A000 (unsupported format-version)",
                "format-version 99 is refused (0A000), not read as v2")


def test_a_string_current_snapshot_id_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax csidx"], "XX001 (non-integer current-snapshot-id)",
                "a string current-snapshot-id is refused (XX001), not read as an empty table")


def test_the_backend_survives_the_malformed_number_refusals(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax up"], "1", "backend still up after the malformed-number refusals")


def test_current_snapshot_id_minus_one_still_reads_as_no_snapshot(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax csidneg"], "0",
                "control: current-snapshot-id -1 still reads as a table with no snapshot (0 rows)")


def test_format_version_4_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax fv4"], "0A000 (unsupported format-version)",
                "format-version 4 is refused (0A000), one above the newest known")


def test_format_version_0_is_refused(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax fv0"], "0A000 (unsupported format-version)",
                "format-version 0 is refused (0A000), one below the oldest known")


def test_a_null_current_snapshot_id_still_reads_as_no_snapshot(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["lax csidnull"], "0",
                "control: a null current-snapshot-id still reads as a table with no snapshot (0 rows)")


def test_two_valid_data_entries_scan_as_ten_rows(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["enum ctl scan"], "10|1|10", "control: two valid data entries scan as ten rows (ids 1..10)")


def test_two_valid_data_entries_list_two_files(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["enum ctl list"], "2", "control: two valid data entries list two data files")


def test_content_3_on_the_oldest_file_is_refused_by_the_scan(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["old3 scan"], "XX001 (invalid content 3)",
                "data_file.content 3 on the oldest file is refused by iceberg_scan (XX001), not read as 5 rows")


def test_content_minus_1_on_the_oldest_file_is_refused_by_the_scan(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["oldm1 scan"], "XX001 (invalid content -1)",
                "data_file.content -1 on the oldest file is refused by iceberg_scan (XX001), not read as 5 rows")


def test_content_3_on_the_newest_file_is_refused_naming_the_value(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["new3 scan"], "XX001 (invalid content 3)",
                "data_file.content 3 on the newest file is refused naming the value, not as an equality delete with no ids")


def test_the_lister_refuses_content_3_as_corrupt(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["old3 list"], "XX001 (invalid content 3)",
                "iceberg_data_files refuses data_file.content 3 as corrupt (XX001), not as a delete file (0A000)")


def test_status_3_is_refused_by_the_scan(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["status3 scan"], "XX001 (invalid status 3)",
                "manifest_entry.status 3 is refused by iceberg_scan (XX001), not read as a live entry")


def test_the_lister_refuses_status_3(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["status3 list"], "XX001 (invalid status 3)",
                "iceberg_data_files refuses manifest_entry.status 3 (XX001), not listed")


def test_manifest_content_5_is_refused_by_the_scan(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["mf5 scan"], "XX001 (invalid manifest content 5)",
                "manifest_file.content 5 is refused by iceberg_scan (XX001), not read as a data manifest")


def test_the_lister_refuses_manifest_content_5_as_corrupt(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["mf5 list"], "XX001 (invalid manifest content 5)",
                "iceberg_data_files refuses manifest_file.content 5 as corrupt (XX001), not as a delete manifest (0A000)")


def test_content_2_32_on_the_oldest_file_is_refused_not_wrapped(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["old4g scan"], "XX001 (invalid content 4294967296)", "data_file.content 4294967296 on the oldest file is refused by iceberg_scan (XX001), not wrapped to 0")


def test_status_2_32_plus_1_is_refused_not_wrapped(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["status4g scan"], "XX001 (invalid status 4294967297)", "manifest_entry.status 4294967297 is refused by iceberg_scan (XX001), not wrapped to 1")


def test_manifest_content_2_32_is_refused_not_wrapped(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["mf4g scan"], "XX001 (invalid manifest content 4294967296)", "manifest_file.content 4294967296 is refused by iceberg_scan (XX001), not wrapped to 0")


def test_the_backend_survives_the_enum_refusals(imf, expect):
    if _declined(imf, expect):
        return
    expect.text(imf["enum up"], "1", "backend still up after the enum refusals")

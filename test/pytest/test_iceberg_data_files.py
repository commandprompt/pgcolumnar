"""`iceberg_data_files` lists an Iceberg table's live data files, inside its location (#388).

The resolver chains a real Iceberg table (written by pyiceberg, committed under
test/fixtures/iceberg/warehouse/) from its metadata.json through the manifest list and
manifests to the live data files at the current snapshot. The answer is compared with
pyiceberg's own view, `expected_files.json`. The warehouse is committed at a recorded
root (/tmp/pgc_ice_wh) and copied elsewhere at test time, so the rebasing from the
recorded location to the actual one is exercised for real.

What it refuses:
- a manifest list outside the table's location, by four routes (a foreign absolute
  path, a `..` traversal, a sibling that shares the byte prefix, and a symlink under
  the location that targets a file outside it), with 22023 and never XX001, which
  would mean the backend opened the file;
- a snapshot whose manifest list carries a delete manifest (0A000);
- a delete ENTRY inside a data manifest (0A000), with an added and an existing entry
  as controls (#1341 item 49);
- a caller without pg_read_server_files (42501).

No available writer emits the delete shapes, so those tables are crafted Avro.

What it skips: a removed (DELETED, status 2) entry is not live, so it is neither listed
nor refused (#1791). A crafted entry shows it alone, and a real pyiceberg table after a
copy-on-write delete (test/fixtures/iceberg/warehouse_cow, gen_iceberg_cow.py) shows it
against pyiceberg's own scan planning.

THE PORT OF test/iceberg_data_files.sh, BY NAME (#1341). The two names the shell
records only when it skips ("iceberg warehouse fixture is missing", "python3 (json,
zlib) is needed") are carried as the same declines here. Independent of the shell
suite: neither file reads or runs the other.

ONE CHECK PER TEST (#1339). The `ice` module fixture stages every table once and
records every reading; each test asserts one. A failed query is a `query_error()`.
Each reading runs in a session of its own, as the shell's psql per statement does,
so a backend that dies fails its own arm and leaves the other names standing.
"""
import json
import os
import pathlib
import shutil
import stat
import time

import psycopg
import pytest

from pgc_vacuity import query_error

FX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "iceberg"
WH = FX / "warehouse"
COW = FX / "warehouse_cow"           # copy-on-write deletes, written by pyiceberg (#1791)
RECORDED = "/tmp/pgc_ice_wh"
LOC = f"file://{RECORDED}/db/t"     # the crafted tables' recorded location


# ---- crafted Avro --------------------------------------------------------------------

def _zz(n):
    u = (n << 1) ^ (n >> 63) if n < 0 else (n << 1)
    out = bytearray()
    while True:
        b = u & 0x7f
        u >>= 7
        out.append(b | 0x80 if u else b)
        if not u:
            return bytes(out)


def _s(bs):
    return _zz(len(bs)) + bs


def _ocf(schema, recs):
    sync = b"\x00" * 16
    meta = (_zz(2) + _s(b"avro.schema") + _s(json.dumps(schema).encode())
            + _s(b"avro.codec") + _s(b"null") + _zz(0))
    out = b"Obj\x01" + meta + sync
    for r in recs:
        out += _zz(1) + _zz(len(r)) + r + sync
    return out


ENTRY = {"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"}]}}]}

MFILE = {"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"}]}

# the delete-manifest list: a reduced manifest_file schema carrying exactly the fields
# the reader projects, with content declared so a delete manifest is expressible
DELETE_LIST = {"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": "long"},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"},
    {"name": "added_files_count", "type": "int"},
    {"name": "existing_files_count", "type": "int"},
    {"name": "deleted_files_count", "type": "int"},
    {"name": "added_rows_count", "type": "long"},
    {"name": "existing_rows_count", "type": "long"},
    {"name": "deleted_rows_count", "type": "long"}]}


def _metadata(manifest_list):
    return {"format-version": 2, "location": LOC, "current-snapshot-id": 1,
            "current-schema-id": 0,
            "schemas": [{"schema-id": 0, "type": "struct", "fields": [
                {"id": 1, "name": "id", "required": False, "type": "long"}]}],
            "snapshots": [{"snapshot-id": 1, "sequence-number": 1, "timestamp-ms": 0,
                           "manifest-list": manifest_list,
                           "summary": {"operation": "append"}, "schema-id": 0}]}


def _entry_table(root, status, content):
    """-> the metadata.json of a table whose one data manifest holds one entry."""
    md = root / "db" / "t" / "metadata"
    md.mkdir(parents=True)
    ent = (_zz(status) + (_zz(1) + _zz(1)) + _zz(content)
           + _s(f"{LOC}/data/f.parquet".encode()) + _s(b"PARQUET") + _zz(5) + _zz(1000))
    (md / "dm.avro").write_bytes(_ocf(ENTRY, [ent]))
    mf = (_s(f"{LOC}/metadata/dm.avro".encode()) + _zz(1000) + _zz(0)
          + _zz(0)                          # content 0: a DATA manifest
          + (_zz(1) + _zz(1)) + _zz(1) + _zz(1))
    (md / "ml.avro").write_bytes(_ocf(MFILE, [mf]))
    (md / "v1.metadata.json").write_text(json.dumps(_metadata(f"{LOC}/metadata/ml.avro")))
    return md / "v1.metadata.json"


def _delete_list_table(root):
    """-> the metadata.json of a table whose manifest list holds a delete manifest."""
    md = root / "db" / "t" / "metadata"
    md.mkdir(parents=True)
    rec = (_s(f"{LOC}/metadata/delete-m0.avro".encode()) + _zz(100) + _zz(0)
           + _zz(1)                         # content 1: the delete manifest
           + _zz(1) + _zz(1) + _zz(1)
           + _zz(1) + _zz(0) + _zz(0) + _zz(1) + _zz(0) + _zz(0))
    (md / "delete-list.avro").write_bytes(_ocf(DELETE_LIST, [rec]))
    meta = _metadata(f"{LOC}/metadata/delete-list.avro")
    meta["snapshots"][0]["summary"]["operation"] = "overwrite"
    (md / "v1.metadata.json").write_text(json.dumps(meta))
    return md / "v1.metadata.json"


def _open_to_the_server(root):
    """Make `root`, its parents and everything under it readable by the server user."""
    for parent in [root] + list(root.parents):
        try:
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IROTH | stat.S_IXOTH)
        except (PermissionError, FileNotFoundError):
            break
    for where, _dirs, files in os.walk(root):
        os.chmod(where, 0o755)
        for f in files:
            p = os.path.join(where, f)
            if not os.path.islink(p):
                os.chmod(p, 0o644)


# ---- readings ------------------------------------------------------------------------

def _failed(exc):
    """A failed reading. A backend that died carries no SQLSTATE and no primary."""
    return query_error(exc.diag.message_primary or str(exc).splitlines()[0]
                       if str(exc) else type(exc).__name__)


def _one(dsn, sql, *params):
    """One value, read in a session of its own, as the shell's `q` reads it: a backend
    that dies here fails this reading and no other."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return conn.execute(sql, params or None).fetchone()[0]
    except psycopg.Error as exc:
        return _failed(exc)


def _sqlstate(dsn, path):
    """The SQLSTATE listing `path` fails with, or "ok", in a session of its own. A
    failure with no SQLSTATE is a failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SELECT * FROM pgcolumnar.iceberg_data_files(%s)", (str(path),)).fetchall()
            return "ok"
    except psycopg.Error as exc:
        return exc.sqlstate or _failed(exc)


def _gate_refusal(dsn, path, role):
    """"42501" when `role` listing `path` is refused by the pg_read_server_files gate;
    otherwise what happened instead, or the failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SET ROLE {role}")
            conn.execute("SELECT * FROM pgcolumnar.iceberg_data_files(%s)", (str(path),)).fetchall()
            return "ok"
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return _failed(exc)
        primary = exc.diag.message_primary or ""
        return "42501" if exc.sqlstate == "42501" and "pg_read_server_files" in primary \
            else f"{exc.sqlstate}: {primary}"


def _run(dsn, *stmts):
    """Statements in a session of their own -> None, or the failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for st in stmts:
                conn.execute(st)
        return None
    except psycopg.Error as exc:
        return _failed(exc)


# The unprivileged role reaches the function: USAGE on the schema (EXECUTE it has
# through PUBLIC), or its 42501 is the schema's and the pg_read_server_files gate never
# runs. The arm requires the gate's own message, not only the SQLSTATE they share.
DROP_UNPRIV = ("DO $$BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'icedf_unpriv') "
               "THEN EXECUTE 'DROP OWNED BY icedf_unpriv'; END IF; END$$",
               "DROP ROLE IF EXISTS icedf_unpriv")
MAKE_UNPRIV = DROP_UNPRIV + ("CREATE ROLE icedf_unpriv LOGIN",
                             "GRANT USAGE ON SCHEMA pgcolumnar TO icedf_unpriv")


def _alive(pgc_cluster):
    """What the shell's `q 'SELECT 1'` reads: a NEW session still gets an answer."""
    try:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            return c.execute("SELECT 1").fetchone()[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _recovered(pgc_cluster, alive):
    """`alive` as read. If the backend did not answer, wait (up to 60s) until the
    server accepts connections again before the next reading. The shell spends that
    time building its next table, so its later arms run on a recovered server; read
    at once, they would each fail on the recovery and not on their own refusal."""
    for _ in range(240):
        if alive == 1 or _alive(pgc_cluster) == 1:
            break
        time.sleep(0.25)
    return alive


def _tampered(md, name, manifest_list):
    """A variant of the real table's metadata whose current snapshot names
    `manifest_list`, written beside it so the resolver derives the same location."""
    m = json.loads(md.read_text())
    m["snapshots"][-1]["manifest-list"] = manifest_list
    out = md.parent / f"esc_{name}.metadata.json"
    out.write_text(json.dumps(m))
    os.chmod(out, 0o644)
    return out


@pytest.fixture(scope="module")
def ice(pgc_cluster, tmp_path_factory):
    got = {"have fixture": (WH / "db" / "events" / "metadata").is_dir()}
    if not got["have fixture"]:
        yield got
        return
    root = tmp_path_factory.mktemp("iceberg_data_files")
    dest = root / "relocated"
    shutil.copytree(WH / "db", dest / "db")
    mdfiles = sorted((dest / "db" / "events" / "metadata").glob("*.metadata.json"))
    md = mdfiles[-1]
    loc = json.loads(md.read_text())["location"].replace("file://", "")
    aroot = os.path.realpath(dest / "db" / "events")

    outside = root / "outside_symlink_target"
    outside.write_text("outside\n")
    (dest / "db" / "events" / "metadata" / "sneaky.avro").symlink_to(outside)
    escapes = {
        "a": _tampered(md, "a", "file:///etc/passwd"),
        "b": _tampered(md, "b", f"file://{loc}/../../../../../../../etc/hostname"),
        "c": _tampered(md, "c", f"file://{loc}EVIL/x.avro"),
        "d": _tampered(md, "d", f"file://{loc}/metadata/sneaky.avro"),
    }
    nosnap = root / "no_current.metadata.json"
    m = json.loads(md.read_text())
    m.pop("current-snapshot-id", None)
    nosnap.write_text(json.dumps(m))
    dlist = _delete_list_table(root / "deltbl")
    entries = {k: _entry_table(root / f"entry_{k}", st, ct)
               for k, st, ct in (("ctl", 1, 0), ("kept", 0, 0), ("pos", 1, 1),
                                 ("eq", 1, 2), ("gone", 2, 0))}
    _open_to_the_server(root)

    oracle = sorted("%d|%s|%s" % (r["record_count"], r["region"], r["file_basename"])
                    for r in json.loads((WH / "expected_files.json").read_text())["data_files"])

    dsn = pgc_cluster.dsn()
    lst = "SELECT {} FROM pgcolumnar.iceberg_data_files(%s)"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            got["decoded"] = "\n".join(r[0] for r in c.execute(
                lst.format("record_count || '|' || split_part(partition,'=',2) || '|' || "
                           "(regexp_match(file_path,'[^/]+$'))[1]") + " ORDER BY 1",
                (str(md),)).fetchall())
    except psycopg.Error as exc:
        got["decoded"] = _failed(exc)
    got["oracle"] = "\n".join(oracle)
    got["parquet"] = str(_one(dsn, lst.format("bool_and(file_format = 'PARQUET')"), str(md)))
    got["rebased"] = str(_one(dsn, lst.format("bool_and(file_path LIKE %s)"),
                              aroot + "/%", str(md)))
    got["leaks"] = _one(dsn, lst.format("count(*)") + " WHERE file_path LIKE %s",
                        str(md), RECORDED + "/%")
    for k, path in escapes.items():
        got[f"escape {k}"] = _sqlstate(dsn, path)
    got["alive traversal"] = _recovered(pgc_cluster, _alive(pgc_cluster))
    got["delete list"] = _sqlstate(dsn, dlist)
    got["alive delete"] = _recovered(pgc_cluster, _alive(pgc_cluster))
    got["entry ctl"] = _one(dsn, lst.format("count(*)"), str(entries["ctl"]))
    got["entry kept"] = _one(dsn, lst.format("count(*)"), str(entries["kept"]))
    for k in ("pos", "eq"):
        got[f"entry {k}"] = _sqlstate(dsn, entries[k])
    got["entry gone"] = _one(dsn, lst.format("count(*)"), str(entries["gone"]))
    got["alive entries"] = _recovered(pgc_cluster, _alive(pgc_cluster))

    # a copy-on-write delete, relocated as the main warehouse is
    cow_oracle = json.loads((COW / "expected_files.json").read_text())["tables"]
    cow_root = root / "cow_relocated"
    shutil.copytree(COW / "db", cow_root / "db")
    _open_to_the_server(cow_root)
    cow_md = {k: cow_root / "db" / k / "metadata" / cow_oracle[k]["metadata_basename"]
              for k in ("cow", "cow_all")}
    got["cow premise"] = str(all(any(e["status"] == 2 and e["content"] == 0
                                     for e in cow_oracle[k]["entries"])
                                 for k in ("cow", "cow_all")))
    got["cow expect"] = "\n".join(sorted("%d|%s" % (r["record_count"], r["file_basename"])
                                         for r in cow_oracle["cow"]["live_files"]))
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            got["cow listed"] = "\n".join(r[0] for r in c.execute(
                lst.format("record_count || '|' || (regexp_match(file_path,'[^/]+$'))[1]")
                + " ORDER BY 1", (str(cow_md["cow"]),)).fetchall())
    except psycopg.Error as exc:
        got["cow listed"] = _failed(exc)
    got["cow all"] = _one(dsn, lst.format("count(*)"), str(cow_md["cow_all"]))
    got["no snapshot"] = _one(dsn, lst.format("count(*)"), str(nosnap))
    got["unprivileged"] = _run(dsn, *MAKE_UNPRIV) or _gate_refusal(dsn, md, "icedf_unpriv")
    try:
        yield got
    finally:
        _run(dsn, *DROP_UNPRIV)


def _need(ice, expect):
    """The shell skips the whole suite without the committed warehouse, under one
    name; each check here declines under that name instead."""
    if not ice["have fixture"]:
        expect.cannot_run("MISSING_DEPENDENCY", "the committed Iceberg warehouse is not "
                          "in this tree", name="iceberg warehouse fixture is missing")
        return False
    return True


def test_the_warehouse_fixture_is_present(ice, expect):
    """The shell's skip name, carried as the same decline."""
    if _need(ice, expect):
        expect.at_least(len([r for r in ice["oracle"].split("\n") if r]), 1,
                        "premise: the oracle lists data files, so matching it is not vacuous")


def test_python_json_and_zlib_are_available(expect):
    """The shell's other skip name. The crafted Avro here needs json; zlib is what the
    shell's helper imports."""
    try:
        import json as _json  # noqa: F401
        import zlib  # noqa: F401
    except ImportError:
        expect.cannot_run("MISSING_DEPENDENCY", "zlib is not importable",
                          name="python3 (json, zlib) is needed")
        return
    expect.text(zlib.decompress(zlib.compress(b"pgc")).decode(), "pgc",
                "premise: zlib round-trips")


# ---- the oracle and the rebasing -------------------------------------------------------

def test_the_data_files_match_pyicebergs_oracle(ice, expect):
    if _need(ice, expect):
        expect.text(ice["decoded"], ice["oracle"],
                    "data files == pyiceberg oracle (record_count|region|basename)")


def test_every_data_file_is_parquet(ice, expect):
    if _need(ice, expect):
        expect.text(ice["parquet"], "True", "every data file is PARQUET")


def test_returned_paths_are_rebased_under_the_actual_location(ice, expect):
    if _need(ice, expect):
        expect.text(ice["rebased"], "True", "returned paths are rebased under the actual location")


def test_no_returned_path_leaks_the_recorded_root(ice, expect):
    if _need(ice, expect):
        expect.num(ice["leaks"], 0, "no returned path leaks the recorded root")


# ---- a path outside the table location is refused, not read ------------------------
#
# All must be 22023 ("not under / escapes the table location"), and NOT XX001 (bad Avro
# magic): an XX001 means the backend OPENED and read the file.

# (name, reading): a literal table, so the twin grader reads its names.
ESCAPES = [
    ("a foreign absolute manifest-list is refused (22023)", "escape a"),
    ("a .. traversal manifest-list is refused, not read (22023, not XX001)", "escape b"),
    ("a sibling-prefix manifest-list is refused (22023)", "escape c"),
    ("a symlink under the location targeting outside is refused (22023, not XX001)",
     "escape d"),
]


@pytest.mark.parametrize("name, reading", ESCAPES)
def test_a_manifest_list_outside_the_location_is_refused(ice, expect, name, reading):
    if _need(ice, expect):
        expect.text(ice[reading], "22023", name)


def test_the_backend_survives_the_traversal_attempts(ice, expect):
    if _need(ice, expect):
        expect.num(ice["alive traversal"], 1, "backend still up after the traversal attempts")


# ---- deletes are refused, not ignored ------------------------------------------------

def test_a_snapshot_with_a_delete_manifest_is_refused(ice, expect):
    if _need(ice, expect):
        expect.text(ice["delete list"], "0A000",
                    "a snapshot with a delete manifest is refused (feature_not_supported 0A000)")


def test_the_backend_survives_the_delete_refusal(ice, expect):
    if _need(ice, expect):
        expect.num(ice["alive delete"], 1, "backend still up after the delete refusal")


def test_the_control_entry_is_listed(ice, expect):
    """The crafted Avro reads: one added data entry lists one file."""
    if _need(ice, expect):
        expect.num(ice["entry ctl"], 1,
                   "control: a data manifest with one added data entry lists that one file")


def test_an_existing_entry_is_listed(ice, expect):
    """Status 0 is EXISTING: a live file in a manifest a later commit rewrote or merged.
    A lister that refused every entry but ADDED would fail an ordinary table after its
    second append."""
    if _need(ice, expect):
        expect.num(ice["entry kept"], 1,
                   "an EXISTING (status 0) data entry is listed, as an ADDED one is")


ENTRY_REFUSALS = [
    ("a position-delete entry in a data manifest is refused (0A000)", "entry pos"),
    ("an equality-delete entry in a data manifest is refused (0A000)", "entry eq"),
]


@pytest.mark.parametrize("name, reading", ENTRY_REFUSALS)
def test_a_delete_entry_is_refused(ice, expect, name, reading):
    if _need(ice, expect):
        expect.text(ice[reading], "0A000", name)


def test_a_deleted_entry_is_skipped(ice, expect):
    """Status 2 is DELETED: a file the current snapshot removed. It is not live, so the
    lister neither returns it nor refuses the table over it (#1791)."""
    if _need(ice, expect):
        expect.num(ice["entry gone"], 0,
                   "a DELETED (status 2) data entry is skipped, not listed and not refused")


def test_the_backend_survives_the_entry_refusals(ice, expect):
    if _need(ice, expect):
        expect.num(ice["alive entries"], 1, "backend still up after the entry refusals")


# ---- a copy-on-write delete: the removed file is skipped (#1791) ---------------------

def test_the_copy_on_write_fixture_carries_a_deleted_entry(ice, expect):
    """The listing arms below cannot pass on a table that has no status-2 entry."""
    if _need(ice, expect):
        expect.text(ice["cow premise"], "True",
                    "premise: each copy-on-write fixture's current snapshot carries a "
                    "DELETED (status 2) data entry")


def test_a_copy_on_write_delete_lists_the_live_files(ice, expect):
    if _need(ice, expect):
        expect.text(ice["cow listed"], ice["cow expect"],
                    "after a copy-on-write delete, data files == pyiceberg plan_files "
                    "(record_count|basename)")


def test_a_copy_on_write_delete_of_every_row_lists_nothing(ice, expect):
    if _need(ice, expect):
        expect.num(ice["cow all"], 0,
                   "after a copy-on-write delete of every row, no data files are listed")


# ---- no current snapshot, and the privilege ------------------------------------------

def test_a_table_with_no_current_snapshot_yields_no_data_files(ice, expect):
    if _need(ice, expect):
        expect.num(ice["no snapshot"], 0, "a table with no current snapshot yields no data files")


def test_an_unprivileged_role_is_refused(ice, expect):
    if _need(ice, expect):
        expect.text(ice["unprivileged"], "42501", "an unprivileged role is refused (42501)")

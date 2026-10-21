"""pgcolumnar.iceberg_current_snapshot reads a real Iceberg table's metadata.json and
resolves its current snapshot (#388 phase 3a). The pytest twin of
`test/iceberg_catalog.sh`, every check by the shell's name (#1341).

The metadata is pyiceberg's, an independent writer, and the oracle is pyiceberg's own
view of the current snapshot. The reader must match it, return exactly one row with no
parent, resolve the snapshot the table NAMES (not snapshots[0], against a decoy put
first), return zero rows for a table with no current snapshot, and refuse a dangling
snapshot id (XX001), a JSON object that is not table metadata (22023) and malformed
JSON (22P02). A role with USAGE on the schema but without pg_read_server_files is
refused (42501), and a non-superuser holding that role reads the catalog.

ON A PRIVATE DATABASE, with every metadata file in `server_dir`, because the backend
reads them. The two roles log in on their own, as the shell's do, and are dropped at
teardown. Where the fixture is missing the first test fails under the shell's pgc_skip
message, as pgc_skip does, and the rest decline unnamed.
"""
import json
import pathlib

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_catalog"
FX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg"
UNPRIV, READER = "pgc_ice_unpriv", "pgc_ice_reader"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _target(dsn, user, port):
    return dsn if user is None else f"host=127.0.0.1 port={port} user={user} dbname={DB}"


def _value(dsn, sql, user=None, port=None):
    try:
        with psycopg.connect(_target(dsn, user, port), autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return ("t" if row[0] else "f") if isinstance(row[0], bool) else str(row[0])


def _sqlstate(dsn, sql, user=None, port=None):
    try:
        with psycopg.connect(_target(dsn, user, port), autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


def _write(path, obj):
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    path.chmod(0o666)
    return path


@pytest.fixture(scope="module")
def ic(pgc_cluster, server_dir):
    if not (FX / "table.metadata.json").exists():
        yield None
        return
    meta = json.loads((FX / "table.metadata.json").read_text())
    oracle = json.loads((FX / "expected_meta.json").read_text())
    md = _write(server_dir / "table.metadata.json", meta)
    two = dict(meta)
    real = meta["snapshots"][-1]
    decoy = dict(real, **{"snapshot-id": real["snapshot-id"] + 1,
                          "manifest-list": "file:///warehouse/db/events/metadata/DECOY.avro",
                          "sequence-number": (real.get("sequence-number", 0) or 0) + 99})
    two["snapshots"] = [decoy, real]
    two = _write(server_dir / "two_snap.metadata.json", two)
    nosnap = _write(server_dir / "no_current.metadata.json", {k: v for k, v in meta.items() if k != "current-snapshot-id"})
    badid = _write(server_dir / "bad_id.metadata.json", dict(meta, **{"current-snapshot-id": 999999999999999}))
    notice = _write(server_dir / "notice.json", '{"hello":"world"}')
    bad = _write(server_dir / "bad.json", "this is { not json at all")
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
        snap = "pgcolumnar.iceberg_current_snapshot('{}')"
        port = pgc_cluster.port
        yield {
            "oracle": "%d|%d|%s|%d|%s" % (oracle["snapshot_id"], oracle["sequence_number"], oracle["operation"],
                                          oracle["schema_id"], oracle["manifest_list_basename"]),
            "decoded": _value(dsn, "SELECT snapshot_id || '|' || sequence_number || '|' || operation || '|' || "
                                   "schema_id || '|' || (regexp_match(manifest_list, '[^/]+$'))[1] "
                                   f"FROM {snap.format(md)}"),
            "count": _value(dsn, f"SELECT count(*) FROM {snap.format(md)}"),
            "parent": _value(dsn, f"SELECT parent_snapshot_id IS NULL FROM {snap.format(md)}"),
            "named": _value(dsn, f"SELECT (regexp_match(manifest_list, '[^/]+$'))[1] FROM {snap.format(two)}"),
            "named want": oracle["manifest_list_basename"],
            "decoy": _value(dsn, "SELECT count(*) || '/' || count(*) FILTER (WHERE manifest_list LIKE '%DECOY%') "
                                 f"FROM {snap.format(two)}"),
            "nosnap": _value(dsn, f"SELECT count(*) FROM {snap.format(nosnap)}"),
            "badid": _sqlstate(dsn, f"SELECT * FROM {snap.format(badid)}"),
            "notice": _sqlstate(dsn, f"SELECT * FROM {snap.format(notice)}"),
            "bad": _sqlstate(dsn, f"SELECT * FROM {snap.format(bad)}"),
            "alive": _value(dsn, "SELECT 1"),
            "unpriv": _sqlstate(dsn, f"SELECT count(*) FROM {snap.format(md)}", UNPRIV, port),
            "reader": _value(dsn, f"SELECT count(*) FROM {snap.format(md)}", READER, port),
        }
    finally:
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            for role in (UNPRIV, READER):
                a.execute(f"DROP ROLE IF EXISTS {role}")


def _declined(ic, expect):
    if ic is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the iceberg metadata fixture is missing")
    return True


def test_current_snapshot_matches_the_oracle(ic, expect):
    if ic is None:
        expect.text("missing", "present", "iceberg metadata fixture is missing")
        return
    expect.require_module("json", name="python3 is needed to read the oracle")
    expect.text(ic["decoded"], ic["oracle"], "current snapshot == oracle (id|seq|op|schema|manifest-list)")


def test_exactly_one_current_snapshot_row(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(ic["count"], "1", "exactly one current snapshot row")


def test_the_first_snapshot_has_no_parent(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(ic["parent"], "t", "the first snapshot has no parent (NULL)")


def test_resolves_the_named_current_snapshot(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(ic["named"], ic["named want"], "resolves the named current snapshot, not snapshots[0]")


def test_the_decoy_snapshot_is_not_returned(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(ic["decoy"], "1/0", "the decoy snapshot is not returned")


def test_a_table_with_no_current_snapshot_returns_zero_rows(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(ic["nosnap"], "0", "a table with no current snapshot returns zero rows")


def test_a_dangling_current_snapshot_id_is_refused(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(str(ic["badid"]), "XX001", "a dangling current-snapshot-id is refused (data_corrupted XX001)")


def test_a_non_iceberg_json_object_is_refused(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(str(ic["notice"]), "22023", "a non-Iceberg JSON object is refused (invalid_parameter_value 22023)")


def test_malformed_json_is_rejected(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(str(ic["bad"]), "22P02", "malformed JSON is rejected (22P02), backend survives")


def test_backend_still_up_after_the_bad_metadata(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(ic["alive"], "1", "backend still up after the bad file")


def test_an_unprivileged_role_is_refused_the_catalog(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(str(ic["unpriv"]), "42501", "an unprivileged role is refused (42501)")


def test_a_pg_read_server_files_member_may_read_the_catalog(ic, expect):
    if _declined(ic, expect):
        return
    expect.text(ic["reader"], "1", "a pg_read_server_files member who is not superuser may read the catalog")

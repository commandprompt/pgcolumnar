"""`iceberg_data_files` refuses a delete entry inside a data manifest (#1341 item 49).

The lister refuses a snapshot with deletes in two places. A delete manifest in the
manifest LIST is refused without being opened. A DATA manifest (content 0 in the
list) can still hold an entry that must not be listed: a position or equality delete
file (entry content 1 or 2), or a removed file (status 2, DELETED). Listing a removed
file would hand a caller rows that a commit took out.

No available writer emits these shapes, so each table here is crafted Avro: the same
one-entry data manifest with one (status, content) pair. The (1, 0) control is listed,
so a refusal of the others is the entry's doing, not a fault in the crafted Avro.

This ports only the entry arms of `test/iceberg_data_files.sh`; the rest of that suite
is declared INCOMPLETE in `test_compare_to_bash.py`.
"""
import json
import os
import stat

import pytest

LOC = "file:///tmp/pgc_ice_wh/db/t"     # the recorded root the reader rebases from


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
            os.chmod(os.path.join(where, f), 0o644)


def _entry_table(root, status, content):
    """-> the metadata.json path of a table whose one data manifest holds one entry."""
    md = root / "db" / "t" / "metadata"
    md.mkdir(parents=True)
    ent = (_zz(status) + (_zz(1) + _zz(1)) + _zz(content)
           + _s(f"{LOC}/data/f.parquet".encode()) + _s(b"PARQUET") + _zz(5) + _zz(1000))
    (md / "dm.avro").write_bytes(_ocf(ENTRY, [ent]))
    mf = (_s(f"{LOC}/metadata/dm.avro".encode()) + _zz(1000) + _zz(0)
          + _zz(0)                          # content 0: a DATA manifest
          + (_zz(1) + _zz(1)) + _zz(1) + _zz(1))
    (md / "ml.avro").write_bytes(_ocf(MFILE, [mf]))
    meta = {"format-version": 2, "location": LOC, "current-snapshot-id": 1,
            "current-schema-id": 0,
            "schemas": [{"schema-id": 0, "type": "struct", "fields": [
                {"id": 1, "name": "id", "required": False, "type": "long"}]}],
            "snapshots": [{"snapshot-id": 1, "sequence-number": 1, "timestamp-ms": 0,
                           "manifest-list": f"{LOC}/metadata/ml.avro",
                           "summary": {"operation": "append"}, "schema-id": 0}]}
    (md / "v1.metadata.json").write_text(json.dumps(meta))
    _open_to_the_server(root)
    return md / "v1.metadata.json"


def _listed(conn, path):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pgcolumnar.iceberg_data_files(%s)", (str(path),))
        return cur.fetchone()[0]


def test_the_control_entry_is_listed(pgc_conn, tmp_path, expect):
    """The crafted Avro reads: one added data entry lists one file."""
    expect.num(_listed(pgc_conn, _entry_table(tmp_path / "ctl", 1, 0)), 1,
               "control: a data manifest with one added data entry lists that one file")


@pytest.mark.parametrize("status, content, name", [
    (1, 1, "a position-delete entry in a data manifest is refused (0A000)"),
    (1, 2, "an equality-delete entry in a data manifest is refused (0A000)"),
    (2, 0, "a DELETED (status 2) data entry is refused, not listed (0A000)"),
])
def test_a_delete_entry_is_refused(pgc_conn, tmp_path, expect, status, content, name):
    import psycopg

    path = _entry_table(tmp_path / "t", status, content)
    with pytest.raises(psycopg.Error) as err:
        pgc_conn.execute("SELECT * FROM pgcolumnar.iceberg_data_files(%s)", (str(path),))
    expect.sqlstate(err.value, "0A000", name)
    expect.num(int(pgc_conn.execute("SELECT 1").fetchone()[0]), 1,
               "backend still up after the entry refusals")

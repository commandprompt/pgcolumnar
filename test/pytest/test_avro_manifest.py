"""pgcolumnar reads Iceberg Avro manifests and manifest lists, and refuses what it must.
The pytest twin of `test/avro_manifest.sh`, every check by the shell's name (#1341).

`read_avro_manifest` and `read_manifest_list` decode the pyiceberg-written fixtures
under test/fixtures/iceberg, checked field by field against the JSON oracles written
beside them. A hand-built manifest pins an explicit entry sequence number; a role
without pg_read_server_files is refused; a file with no Avro magic is rejected as
corrupt; and a deflate zip-bomb is refused by the cumulative decompression cap.

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED. A fresh connection answers
again after crash recovery, so survival also reads the server log's count of killed
backends, and an unreadable log fails it. ON A PRIVATE DATABASE; each test asserts
one reading (#1339).
"""
import json
import pathlib
import zlib

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_avro_manifest"
FX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg"
ROLE = "avro_unpriv_py"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql, settings=()):
    """-> the first column of the first row, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return row[0]


def _sqlstate(dsn, sql, settings=()):
    """-> the SQLSTATE `sql` raised, or what happened instead."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "noerror"


def _refusal(dsn, sql, phrase, label):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, the bare
    SQLSTATE when the message lacks it, or what happened instead."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.sqlstate + (label if phrase in str(exc) else "")
    return "noerror"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == 1 and now == before else f"no ([{up}], {now - before} backend crashes)"


def _zz(n):
    u = ((n << 1) ^ (n >> 63)) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = u & 0x7f
        u >>= 7
        out.append(b | 0x80 if u else b)
        if not u:
            return bytes(out)


def _s(bs):
    return _zz(len(bs)) + bs


def _seq_manifest(path):
    """One manifest entry with an explicit sequence_number of 42, written by hand."""
    schema = (b'{"type":"record","name":"manifest_entry","fields":['
              b'{"name":"status","type":"int"},'
              b'{"name":"sequence_number","type":["null","long"]},'
              b'{"name":"data_file","type":{"type":"record","name":"df","fields":['
              b'{"name":"content","type":"int"},'
              b'{"name":"file_path","type":"string"},'
              b'{"name":"file_format","type":"string"},'
              b'{"name":"record_count","type":"long"},'
              b'{"name":"file_size_in_bytes","type":"long"}]}}]}')
    rec = _zz(1) + _zz(1) + _zz(42) + _zz(0) + _s(b"d.parquet") + _s(b"PARQUET") + _zz(5) + _zz(100)
    sync = b"\x00" * 16
    meta = _zz(2) + _s(b"avro.schema") + _s(schema) + _s(b"avro.codec") + _s(b"null") + _zz(0)
    path.write_bytes(b"Obj\x01" + meta + sync + _zz(1) + _zz(len(rec)) + rec + sync)


def _bomb(path):
    """Nine deflate blocks of 128 MiB of zeros each, past the 1 GiB cumulative cap."""
    sync = b"\x00" * 16
    meta = _zz(2)
    for k, v in ((b"avro.schema", b'{"type":"record","name":"x","fields":[]}'), (b"avro.codec", b"deflate")):
        meta += _zz(len(k)) + k + _zz(len(v)) + v
    meta += _zz(0)
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = co.compress(b"\x00" * (128 * 1024 * 1024)) + co.flush()
    with open(path, "wb") as f:
        f.write(b"Obj\x01" + meta + sync)
        for _ in range(9):
            f.write(_zz(0) + _zz(len(raw)) + raw + sync)


@pytest.fixture(scope="module")
def avm(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"DROP ROLE IF EXISTS {ROLE}")
        a.execute(f"CREATE ROLE {ROLE} LOGIN")

    # server_dir, not tmp_path: the backend reads these files, and where it runs as a
    # different user it cannot reach pytest's mode-700 tmp_path (found by jdatcmd
    # reviewing #1576).
    work = server_dir
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            # USAGE on the schema, so the refusal is the function's own gate, not the
            # schema's: without it, the gate removed still read 42501.
            c.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {ROLE}")
        r = {}
        m0 = work / "manifest-0.avro"
        m0.write_bytes((FX / "manifest-0.avro").read_bytes())
        m0.chmod(0o644)
        expected = json.loads((FX / "expected.json").read_text())
        r["n want"] = len(expected["entries"])
        rm = f"pgcolumnar.read_avro_manifest('{m0}')"
        r["n"] = _value(dsn, f"SELECT count(*) FROM {rm}")
        r["oracle"] = "\n".join(sorted("%d|%d|%s" % (e["record_count"], e["file_size_in_bytes"], e["partition"]["region"])
                                       for e in expected["entries"]))
        r["decoded"] = _value(dsn, "SELECT string_agg(x, chr(10) ORDER BY x COLLATE \"C\") FROM (SELECT record_count || '|' || "
                                   f"file_size_in_bytes || '|' || split_part(partition, '=', 2) AS x FROM {rm}) s")
        r["field name"] = _value(dsn, f"SELECT bool_and(partition LIKE 'region=%')::text FROM {rm}")
        r["parquet"] = _value(dsn, f"SELECT bool_and(file_path LIKE '%.parquet')::text FROM {rm}")
        r["added"] = _value(dsn, f"SELECT bool_and(status = 1 AND content = 0)::text FROM {rm}")
        r["inherited"] = _value(dsn, f"SELECT count(*) FILTER (WHERE sequence_number IS NULL) || '/' || count(*) FROM {rm}")

        seq = work / "seq.avro"
        _seq_manifest(seq)
        seq.chmod(0o644)
        r["explicit"] = _value(dsn, f"SELECT sequence_number FROM pgcolumnar.read_avro_manifest('{seq}')")

        ml = work / "manifest-list.avro"
        ml.write_bytes((FX / "manifest-list.avro").read_bytes())
        ml.chmod(0o644)
        lst = json.loads((FX / "expected_list.json").read_text())["manifest_files"][0]
        r["list oracle"] = "|".join(str(lst[k]) for k in (
            "manifest_length", "content", "partition_spec_id", "added_files_count", "existing_files_count",
            "deleted_files_count", "added_rows_count", "existing_rows_count", "deleted_rows_count",
            "sequence_number", "min_sequence_number", "added_snapshot_id"))
        rl = f"pgcolumnar.read_manifest_list('{ml}')"
        r["list n"] = _value(dsn, f"SELECT count(*) FROM {rl}")
        r["list entry"] = _value(dsn, "SELECT manifest_length || '|' || content || '|' || partition_spec_id || '|' || "
                                      "added_files_count || '|' || existing_files_count || '|' || deleted_files_count "
                                      "|| '|' || added_rows_count || '|' || existing_rows_count || '|' || "
                                      "deleted_rows_count || '|' || sequence_number || '|' || min_sequence_number "
                                      f"|| '|' || added_snapshot_id FROM {rl}")
        r["list path want"] = lst["manifest_path_basename"]
        r["list path"] = _value(dsn, f"SELECT (regexp_match(manifest_path, '[^/]+$'))[1] FROM {rl}")

        r["unpriv"] = _refusal(pgc_cluster.dsn(dbname=DB).replace("user=postgres", f"user={ROLE}"),
                               f"SELECT count(*) FROM {rm}", "pg_read_server_files",
                               " by the pg_read_server_files gate")
        before = _crashes(pgc_cluster)
        bad = work / "notavro.bin"
        bad.write_bytes(b"this is not avro at all, not even close, just some bytes")
        bad.chmod(0o644)
        r["bad magic"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_avro_manifest('{bad}')",
                                  "bad magic", " (bad magic)")
        r["after bad"] = _survived(dsn, pgc_cluster, before)
        r["parquet enum"] = _value(dsn, f"SELECT bool_and(file_format = 'PARQUET')::text FROM {rm}")
        bomb = work / "bomb.avro"
        _bomb(bomb)
        bomb.chmod(0o644)
        r["bomb"] = _sqlstate(dsn, f"SELECT count(*) FROM pgcolumnar.read_avro_manifest('{bomb}')",
                              ["SET statement_timeout = '60s'"])
        r["after bomb"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            a.execute(f"DROP ROLE IF EXISTS {ROLE}")


def test_decoded_entry_count_equals_the_oracle(avm, expect):
    expect.text(str(avm["n"]), str(avm["n want"]), "decoded entry count == oracle")


def test_decoded_entries_equal_the_oracle(avm, expect):
    expect.text(avm["decoded"], avm["oracle"], "decoded entries == oracle (record_count|file_size|region)")


def test_the_partition_renders_the_schemas_field_name(avm, expect):
    expect.text(avm["field name"], "true", "partition renders the schema's field name")


def test_every_entrys_file_path_ends_in_parquet(avm, expect):
    expect.text(avm["parquet"], "true", "every entry's file_path ends in .parquet")


def test_all_entries_are_added_data_files(avm, expect):
    expect.text(avm["added"], "true", "all entries are ADDED data files (status=1, content=0)")


def test_the_committed_entries_inherit_their_sequence_number(avm, expect):
    expect.text(avm["inherited"], f"{avm['n want']}/{avm['n want']}",
                "the committed entries have a NULL (inherited) sequence_number")


def test_an_explicit_entry_sequence_number_is_decoded(avm, expect):
    expect.text(str(avm["explicit"]), "42", "an explicit entry sequence_number is decoded (42)")


def test_the_manifest_list_entry_count_equals_the_oracle(avm, expect):
    expect.text(str(avm["list n"]), "1", "manifest-list entry count == oracle (1)")


def test_the_manifest_list_entry_equals_the_oracle(avm, expect):
    expect.text(avm["list entry"], avm["list oracle"], "manifest-list entry == oracle (all scalar fields)")


def test_the_manifest_list_path_basename_equals_the_oracle(avm, expect):
    expect.text(avm["list path"], avm["list path want"], "manifest-list manifest_path basename == oracle")


def test_an_unprivileged_role_is_refused_the_manifest(avm, expect):
    expect.text(avm["unpriv"], "42501 by the pg_read_server_files gate", "an unprivileged role is refused (42501)")


def test_a_non_avro_file_is_rejected(avm, expect):
    expect.text(avm["bad magic"], "XX001 (bad magic)", "a non-Avro file is rejected (bad magic), backend survives")


def test_the_backend_is_still_up_after_the_bad_file(avm, expect):
    expect.text(avm["after bad"], "1", "backend still up after the bad file")


def test_file_format_decodes_to_the_iceberg_enum_name(avm, expect):
    expect.text(avm["parquet enum"], "true", "file_format decodes to the Iceberg enum name")


def test_a_deflate_zip_bomb_is_refused_by_the_cap(avm, expect):
    expect.text(avm["bomb"], "54000", "a deflate zip-bomb is refused by the cumulative cap (54000)")


def test_the_backend_is_still_up_after_the_zip_bomb(avm, expect):
    expect.text(avm["after bomb"], "1", "backend still up after the zip-bomb")


def test_the_manifest_fixtures_are_present(expect):
    """The shell skips the whole suite without the committed fixtures, under this name."""
    if not (FX / "manifest-0.avro").is_file():
        expect.cannot_run("MISSING_DEPENDENCY", "the committed Iceberg manifest fixtures are not in this tree",
                          name="iceberg manifest fixtures are missing")
        return
    expect.at_least((FX / "manifest-0.avro").stat().st_size, 1,
                    "premise: the committed manifest fixture is not empty")


def test_python_can_read_the_oracle(expect):
    """The shell's other skip name: it reads the oracle with python3's json."""
    try:
        import json as _json
    except ImportError:
        expect.cannot_run("MISSING_DEPENDENCY", "json is not importable",
                          name="python3 is needed to read the oracle")
        return
    expect.at_least(len(_json.loads((FX / "expected.json").read_text())["entries"]), 1,
                    "premise: the oracle lists manifest entries, so matching it is not vacuous")

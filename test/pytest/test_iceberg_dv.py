"""Iceberg v3 deletion vectors (#388 phase 4c): iceberg_scan reads a table whose current
snapshot carries Puffin deletion vectors and drops the rows they name. The pytest twin
of `test/iceberg_dv.sh`, every check by the shell's name (#1341).

The fixtures are hand-crafted (test/fixtures/iceberg/warehouse_del): pyroaring bitmaps
in hand-built Puffin files, hand-encoded manifests, exact sequence numbers, and an
oracle cross-checked against pyiceberg and DuckDB. The warehouse is relocated so its
recorded root differs from where it lies.

Asserted: eight snapshots read the oracle's surviving ids (the <= sequence boundary,
supersede per data file, run, bitset and high-bucket containers, two blobs in one
file); thirteen corrupt or unsupported snapshots are refused, each by its own guard;
and no backend dies on the way.

HOW A REFUSAL IS READ. Several guards raise XX001, so a refusal counts only when its
message is that guard's own: with the manifest/footer offset match removed, a later
XX001 refused the same file and the arm, reading the SQLSTATE alone, still passed. A
refusal reads as its SQLSTATE, "<SQLSTATE> (another refusal: ...)", "no error", or a
query_error() when the server could not be asked.

HOW SURVIVAL IS READ. A fresh connection answers again after crash recovery, so
"SELECT 1" alone passed with a backend killed by a refusal arm. Each survival check
also requires that the server log's count of backends terminated by a signal has not
moved since its section began.

ON A PRIVATE DATABASE, with the warehouse copied into `server_dir`, because the backend
reads it. Where the fixture is missing the first test fails under the shell's pgc_skip
message, and the rest decline.
"""
import json
import pathlib
import shutil
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_dv"
FX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg"
WHD = FX / "warehouse_del"
COLS = "AS t(id bigint, region text, amount int)"
APPLIED = ("dvapply", "dvnoapply", "dvsupersede", "dvother", "dvwide", "dvrun", "dvbitset", "dvtwo")
REFUSED = {
    "dvdup": "two deletion vectors reference data file",
    "dvbadcrc": "fails its CRC-32 check",
    "dvbadmagic": "has a bad magic sequence",
    "dvoffmismatch": "has no blob matching the manifest's offset",
    "dvnoref": "has no referenced_data_file",
    "dvbadcount": "positions but its manifest entry records",
    "dvcompressed": "declares a compression codec",
    "dvflags": "has a compressed Puffin footer",
    "dvv2": "is a deletion vector, which format-version",
    "dvescape": "is not under the table location",
}
AUDIT = {
    "dvbigoff": "lies outside the file's blob region",
    "dvrunoverflow": "overflows its 16-bit range",
    "dvdupscheme": "two deletion vectors reference data file",
}


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else row[0]


def _await_up(dsn):
    """Wait, at most 60 s, until the server answers, as the shell's await_up: a refusal
    that kills a backend sends the server through crash recovery, and the next readings
    would otherwise red as collateral, by timing. survived() still counts the crash."""
    for _ in range(120):
        if _value(dsn, "SELECT 1") == 1:
            return
        time.sleep(0.5)


def _refusal(dsn, sql, phrase):
    """-> the SQLSTATE when sql is refused with phrase in its primary message, the
    SQLSTATE with the other guard's message, "no error", or query_error()."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return exc.sqlstate if phrase in msg else f"{exc.sqlstate} (another refusal: {msg})"
    return "no error"


def _names(dsn, sql, phrase):
    """-> "1" when sql's refusal message carries phrase, "0" when it does not, as the
    shell's grep -c over errmsg_of; query_error() when the server could not be asked."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return "1" if phrase in (exc.diag.message_primary or "") else "0"
    return "0"


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


@pytest.fixture(scope="module")
def dv(pgc_cluster, server_dir):
    if not (WHD / "db" / "t" / "metadata" / "dvapply.metadata.json").is_file():
        yield None
        return
    dest = server_dir / "dv"
    shutil.copytree(WHD / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    mdir = dest / "db" / "t" / "metadata"
    oracle = json.loads((WHD / "expected_deletes.json").read_text())["dv_surviving"]

    def scan(name):
        return f"SELECT * FROM pgcolumnar.iceberg_scan('{mdir / (name + '.metadata.json')}') {COLS}"

    admin = pgc_cluster.dsn()
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {"oracle": {k: ",".join(str(i) for i in v) for k, v in oracle.items()}}
        # the shell's order, section by section, each with its own crash baseline
        before = _crashes(pgc_cluster)
        for name in APPLIED:
            r[name] = _value(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM "
                                  f"pgcolumnar.iceberg_scan('{mdir / (name + '.metadata.json')}') {COLS}")
        r["up after applications"] = _survived(dsn, pgc_cluster, before)
        before = _crashes(pgc_cluster)
        for name, phrase in REFUSED.items():
            r[name] = _refusal(dsn, scan(name), phrase)
            if name == "dvflags":
                r["dvflags names"] = _names(dsn, scan(name), "compressed")
            if name == "dvv2":
                r["dvv2 names"] = _names(dsn, scan(name), "format-version")
        r["up after refusals"] = _survived(dsn, pgc_cluster, before)
        before = _crashes(pgc_cluster)
        for name, phrase in AUDIT.items():
            r[name] = _refusal(dsn, scan(name), phrase)
        r["up after audit"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(dv, expect):
    if dv is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the deletion-vector fixtures are missing")
    return True


def test_a_deletion_vector_at_an_equal_sequence_number_applies(dv, expect):
    if dv is None:
        expect.text("missing", "present", "deletion-vector fixtures are missing")
        return
    expect.require_module("json", name="python3 is needed")
    expect.text(str(dv["dvapply"]), dv["oracle"]["dvapply"],
                "a deletion vector at an equal sequence number applies (survivors == oracle)")


def test_a_deletion_vector_older_than_the_data_does_not_apply(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvnoapply"]), dv["oracle"]["dvnoapply"], "a deletion vector older than the data does not apply")


def test_an_applicable_dv_supersedes_position_delete_files(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvsupersede"]), dv["oracle"]["dvsupersede"],
                "an applicable DV supersedes position-delete files for its data file")


def test_dv_supersede_is_per_data_file_not_global(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvother"]), dv["oracle"]["dvother"], "supersede is per data file, not global")


def test_a_dv_with_run_containers_and_a_high_bucket_decodes(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvwide"]), dv["oracle"]["dvwide"], "a DV with run containers and a high-bucket position decodes")


def test_a_run_optimized_dv_decodes(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvrun"]), dv["oracle"]["dvrun"], "a run-optimized DV (cookie 12347) decodes")


def test_a_bitset_container_dv_decodes(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvbitset"]), dv["oracle"]["dvbitset"], "a bitset-container DV decodes")


def test_two_dv_blobs_in_one_puffin_file_each_apply_to_their_own_file(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvtwo"]), dv["oracle"]["dvtwo"],
                "two DV blobs in one Puffin file each apply to their own data file")


def test_the_backend_survives_the_dv_applications(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["up after applications"]), "1", "backend still up after the DV applications")


def test_two_dv_entries_for_one_data_file_are_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvdup"]), "XX001", "two DV entries for one data file are refused (XX001)")


def test_a_corrupted_dv_crc_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvbadcrc"]), "XX001", "a corrupted DV CRC is refused (XX001)")


def test_a_wrong_dv_blob_magic_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvbadmagic"]), "XX001", "a wrong DV blob magic is refused (XX001)")


def test_a_manifest_footer_offset_mismatch_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvoffmismatch"]), "XX001", "a manifest/footer offset mismatch is refused (XX001)")


def test_a_dv_entry_without_referenced_data_file_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvnoref"]), "XX001", "a DV entry without referenced_data_file is refused (XX001)")


def test_a_record_count_disagreeing_with_the_dv_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvbadcount"]), "XX001",
                "a record_count that disagrees with the DV cardinality is refused (XX001)")


def test_a_dv_blob_declaring_a_codec_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvcompressed"]), "XX001", "a DV blob declaring a compression codec is refused (XX001)")


def test_a_compressed_puffin_footer_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvflags"]), "0A000", "a compressed Puffin footer is refused (0A000)")


def test_the_footer_refusal_names_the_compressed_footer(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvflags names"]), "1", "...and the refusal names the compressed footer")


def test_a_dv_in_a_format_version_2_table_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvv2"]), "0A000", "a deletion vector in a format-version 2 table is refused (0A000)")


def test_the_v2_refusal_names_the_format_version(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvv2 names"]), "1", "...and the refusal names the format version")


def test_a_puffin_path_outside_the_table_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvescape"]), "22023", "a Puffin path outside the table location is refused (22023)")


def test_the_backend_survives_the_dv_refusals(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["up after refusals"]), "1", "backend still up after the DV refusals")


def test_a_dv_whose_offset_plus_size_overflows_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvbigoff"]), "XX001", "a DV whose offset+size overflows int64 is refused, not read (XX001)")


def test_a_run_container_overflowing_its_range_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvrunoverflow"]), "XX001", "a run container overflowing its 16-bit range is refused (XX001)")


def test_an_aliased_duplicate_dv_is_refused(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["dvdupscheme"]), "XX001", "an aliased duplicate DV (scheme vs no scheme) is refused (XX001)")


def test_the_backend_survives_the_audit_arms(dv, expect):
    if _declined(dv, expect):
        return
    expect.text(str(dv["up after audit"]), "1", "backend still up after the audit arms")

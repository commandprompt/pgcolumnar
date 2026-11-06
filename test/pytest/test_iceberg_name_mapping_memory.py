"""The Iceberg name-mapping parse does not allocate per entry (#731). The pytest twin of
`test/iceberg_name_mapping_memory.sh`, every check by the shell's name (#1341).

The claim is the EXCESS over core's own cost, as a slope: the peak resident memory of
a scan over a metadata file whose name mapping is padded with N field-id-less entries,
less the peak of core's jsonb_in parsing the byte-identical mapping string alone, at
two sizes. pgcolumnar's share must stay under 30 bytes an entry, and the padded file
must still bind every row with the same values.

Each peak is VmHWM from /proc for the backend of a connection opened for that one
statement, before and after it -- monotone, so the peak cannot be missed. A statement
that errored has no peak, so a scan that never ran cannot read as one that allocated
nothing. ON A PRIVATE DATABASE, with the fixtures in `server_dir`. Where the fixture
is missing the first test fails under the shell's pgc_skip message, as pgc_skip does,
and the rest decline unnamed.
"""
import json
import pathlib
import shutil

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_name_mapping_memory"
WHN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse_nm"
SMALL, LARGE = 200000, 800000
COLS = "AS t(id bigint, region text, amount int)"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _hwm(pid):
    try:
        for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def _peak(dsn, sql):
    """-> kB the backend's peak rose by over SQL, or None when it cannot be read or
    the statement errored."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            base = _hwm(pid)
            conn.execute(sql).fetchall()
            pk = _hwm(pid)
    except psycopg.Error:
        return None
    return pk - base if base is not None and pk is not None else None


def _build(mdir, n):
    md = json.loads((mdir / "nmapply.metadata.json").read_text())
    real = json.loads(md["properties"]["schema.name-mapping.default"])
    nm = json.dumps(real + [{} for _ in range(n)])
    md["properties"]["schema.name-mapping.default"] = nm
    for name, text in (("pad.metadata.json", json.dumps(md)), ("mapping.json", nm)):
        (mdir / name).write_text(text)
        (mdir / name).chmod(0o666)


def _measure(dsn, mdir, n):
    _build(mdir, n)
    core = _peak(dsn, f"SELECT length(pg_read_file('{mdir}/mapping.json')::jsonb::text)")
    scan = _peak(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{mdir}/pad.metadata.json') {COLS}")
    return None if core is None or scan is None else (core, scan, scan - core)


def _values(dsn, md):
    """The scan's rows as id:region, or the failed query. A NULL region makes the
    aggregate NULL, read as "" -- an empty side that reds by name, as the shell's."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            v = conn.execute(f"SELECT string_agg(id || ':' || region, ',' ORDER BY id) "
                             f"FROM pgcolumnar.iceberg_scan('{md}') {COLS}").fetchone()[0]
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if v is None else v


@pytest.fixture(scope="module")
def nm(pgc_cluster, server_dir):
    if not (WHN / "db" / "t" / "metadata" / "nmapply.metadata.json").exists():
        yield None
        return
    dest = server_dir / "nmmem"
    shutil.copytree(WHN / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    mdir = dest / "db" / "t" / "metadata"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {"small": _measure(dsn, mdir, SMALL), "large": _measure(dsn, mdir, LARGE)}
        for k in ("small", "large"):
            print(f"-- {k}: core_floor/scan/overhead kB = {r[k]}")
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                r["binds"] = c.execute(f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{mdir}/pad.metadata.json') "
                                       f"{COLS}").fetchone()[0]
        except psycopg.Error as exc:
            r["binds"] = query_error(_message(exc))
        r["values"] = _values(dsn, mdir / "pad.metadata.json")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(nm, expect):
    if nm is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the name-mapping fixtures are missing")
    return True


def test_the_small_arm_produced_three_readings(nm, expect):
    if nm is None:
        expect.text("missing", "present", "name-mapping fixtures are missing")
        return
    expect.require_module("json", name="python3 is needed")
    expect.text("1" if nm["small"] is not None else "0 (no readings)", "1", "the small arm produced three readings")


def test_the_large_arm_produced_three_readings(nm, expect):
    if _declined(nm, expect):
        return
    expect.text("1" if nm["large"] is not None else "0 (no readings)", "1", "the large arm produced three readings")


def test_cores_own_parse_is_the_larger_part(nm, expect):
    if _declined(nm, expect):
        return
    big = nm["large"]
    expect.text("1" if big is not None and big[0] > big[2] else f"0 (core floor and overhead, kB: {big})", "1",
                "core's own parse is the larger part of the scan's peak")


def test_the_two_sizes_give_different_peaks(nm, expect):
    if _declined(nm, expect):
        return
    s, big = nm["small"], nm["large"]
    expect.text("1" if s is not None and big is not None and big[1] > s[1] * 2 else f"0 (scan peaks: small {s} large {big})", "1",
                "the two sizes give different peaks")


def test_the_name_mapping_parse_does_not_allocate_per_entry(nm, expect):
    if _declined(nm, expect):
        return
    s, big = nm["small"], nm["large"]
    if s is None or big is None:
        got = f"0 (no slope, from readings {s} {big})"
    else:
        per_entry = int(((big[2] - s[2]) * 1024) / (LARGE - SMALL))
        print(f"-- pgcolumnar-attributable memory per mapping entry: {per_entry} bytes")
        got = "1" if per_entry < 30 else f"0 ({per_entry} bytes an entry, from readings {s} {big})"
    expect.text(got, "1", "the name-mapping parse does not allocate per entry (#731)")


def test_the_padded_mapping_still_binds_every_row(nm, expect):
    if _declined(nm, expect):
        return
    expect.text(str(nm["binds"]), "5", "the padded mapping still binds every row")


def test_the_mapped_values_are_still_correct(nm, expect):
    if _declined(nm, expect):
        return
    # the fixture's own rows (data.parquet), not a second read through the same code
    expect.text(nm["values"], "1:eu,2:eu,3:us,4:us,5:us", "and the mapped values are still correct")

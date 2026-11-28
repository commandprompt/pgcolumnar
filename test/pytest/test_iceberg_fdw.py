"""The Iceberg foreign-data wrapper: partition and metrics pruning (#388, #432).

`iceberg_scan` is a bare SRF that receives no predicate. The FDW gives Iceberg a
predicate-bearing scan node and prunes whole data files whose partition value cannot
match a qual, reading the file's partition value out of the manifest before opening
it.

PRUNING IS ONLY AN OPTIMIZATION, so every pruning arm here is paired with a
correctness arm. That pairing is the whole design of the bash suite this ports, and
it is the reason a pruning bug is dangerous: a scan that prunes too much returns
FEWER ROWS, not a slower plan, and the row count alone cannot tell you which happened
because the correct answer to many of these predicates is also small. The oracle is
`iceberg_scan` of the same table under the same predicate -- it cannot prune, so it
cannot over-prune.

WHY THE FIXTURES ARE COPIED RATHER THAN READ IN PLACE. The server reads these files
as the `postgres` user. A mode-700 directory anywhere above the fixture makes every
file under it unreadable, and the failure surfaces as a missing file rather than as a
permission error. The stage below copies into the test's own directory and opens the
path, which is also what keeps a test from writing into the source tree.
"""
import os
import pathlib
import re
import shutil
import stat

import pytest

SRCDIR = pathlib.Path(__file__).resolve().parents[2]

# THE FIXTURE DATA IS SHARED INPUT, NOT THE OTHER HARNESS. `test/fixtures/` holds
# committed Iceberg warehouses that both harnesses read. Reading them is not the
# cross-harness CALL the independence rule forbids -- nothing here sources, invokes or
# imports `test/*.sh`, and this file would behave identically if the bash suite were
# deleted. It is named here because this is the first pytest file to read the
# directory at all.
FIXTURES = SRCDIR / "test" / "fixtures" / "iceberg"

NO_MARKER = "NO_FILES_PRUNED_MARKER"

# Each warehouse: the fixture subtree, the table under it, and the column list the
# foreign table declares. Data written by pyiceberg, so a same-oracle read is also a
# cross-check that our C transform agrees with Iceberg's.
WAREHOUSES = {
    "ev":  ("warehouse",            "events",  "id bigint, region text, amount int"),
    "evd": ("warehouse_datepart",   "byday",   "id bigint, dt date, amount int"),
    "evb": ("warehouse_bucket",     "byid",    "id bigint, val text, amount int"),
    "evs": ("warehouse_bucket_text", "bystr",  "id bigint, s text"),
    "evt": ("warehouse_trunc",      "bytr",    "id bigint, amount int"),
    "evy": ("warehouse_day",        "byday",   "id bigint, dt date"),
    "ty":  ("warehouse_temporal",   "byyear",   "id bigint, ts timestamp"),
    "tm":  ("warehouse_temporal",   "bymonth",  "id bigint, ts timestamp"),
    "td":  ("warehouse_temporal",   "bydayts",  "id bigint, ts timestamp"),
    "th":  ("warehouse_temporal",   "byhour",   "id bigint, ts timestamp"),
    "yd":  ("warehouse_temporal_xt", "byyear_d",   "id bigint, dt date"),
    "mo":  ("warehouse_temporal_xt", "bymonth_d",  "id bigint, dt date"),
    "yz":  ("warehouse_temporal_xt", "byyear_tz",  "id bigint, tt timestamptz"),
    "mz":  ("warehouse_temporal_xt", "bymonth_tz", "id bigint, tt timestamptz"),
    "dz":  ("warehouse_temporal_xt", "byday_tz",   "id bigint, tt timestamptz"),
    "hz":  ("warehouse_temporal_xt", "byhour_tz",  "id bigint, tt timestamptz"),
    # the same two hour() tables declared as the OTHER timestamp type (#1706)
    "hw":  ("warehouse_temporal",   "byhour",     "id bigint, ts timestamptz"),
    "hn":  ("warehouse_temporal_xt", "byhour_tz",  "id bigint, tt timestamp"),
    # declared typmods the reader applies to every value (#1790)
    "ev_v1": ("warehouse",          "events",       "id bigint, region varchar(1), amount int"),
    "pad2":  ("warehouse_typmod",   "byregion_pad", "id bigint, region varchar(2)"),
    "frac0": ("warehouse_typmod",   "byhour_frac",  "id bigint, ts timestamp(0)"),
    "bkt2":  ("warehouse_typmod",   "byregion_bucket", "id bigint, region varchar(2)"),
    "th6":   ("warehouse_temporal", "byhour",       "id bigint, ts timestamp(6)"),
    # decimals stored as Parquet INT32/INT64, declared int or bigint (#2086)
    "dec9i":  ("warehouse_decimal", "dec9",        "id bigint, d int"),
    "dec9b":  ("warehouse_decimal", "dec9",        "id bigint, d bigint"),
    "dec18b": ("warehouse_decimal", "dec18",       "id bigint, d bigint"),
    "dec9k":  ("warehouse_decimal", "dec9_bucket", "id bigint, d int"),
    "dec9kb": ("warehouse_decimal", "dec9_bucket", "id bigint, d bigint"),
    "dpfx":   ("warehouse_decimal", "pfx",         "id bigint, d_x bigint, d int"),
    "boolt":  ("warehouse_decimal", "boolt",       "id bigint, b bool"),
    "booli":  ("warehouse_decimal", "booli",       "id bigint, b bool"),
    "dpfx2":  ("warehouse_decimal", "pfx2",        "id bigint, d bigint, d_x int"),
    "booll":  ("warehouse_decimal", "booll",       "id bigint, b bool"),
    "booli2": ("warehouse_decimal", "booli",       "id bigint, b smallint"),
    "boolti": ("warehouse_decimal", "boolt",       "id bigint, b int"),
    "boolts": ("warehouse_decimal", "boolt",       "id bigint, b smallint"),
    "byint":  ("warehouse_decimal", "byint",       "k int"),
    "byintt": ("warehouse_decimal", "byint",       "k text"),
    "bib":    ("warehouse_decimal", "booli",       "id bigint, b bigint"),
    "byb":    ("warehouse_decimal", "byint",       "k bigint"),
    "byn":    ("warehouse_decimal", "byint",       "k name"),
    "two":    ("warehouse_decimal", "two",         "k int, d numeric"),
    # column types the reader refuses, whatever a filter prunes (#2190). A fourth
    # element names the metadata version to read instead of the latest.
    "evi":    ("warehouse",          "events",  "id int, region text, amount int"),
    "evsm":   ("warehouse",          "events",  "id smallint, region text, amount int"),
    "evri":   ("warehouse",          "events",  "id bigint, region int, amount int"),
    "prom":   ("warehouse_decimal",  "prom",    "id bigint, r text, v int"),
    "promb":  ("warehouse_decimal",  "prom",    "id bigint, r text, v bigint"),
    "prom1":  ("warehouse_decimal",  "prom",    "id bigint, r text, v int", "00001-"),
    "prom2":  ("warehouse_decimal",  "prom",    "id bigint, r text, v int", "00002-"),
    "boolpi": ("warehouse_decimal",  "boolp",   "id bigint, b int"),
    "boolpb": ("warehouse_decimal",  "boolp",   "id bigint, b bool"),
    "timepi": ("warehouse_decimal",  "timep",   "id bigint, t int"),
    "timepb": ("warehouse_decimal",  "timep",   "id bigint, t bigint"),
    "tyd":    ("warehouse_temporal", "byyear",  "id bigint, ts date"),
    "tzd":    ("warehouse_temporal_xt", "byyear_tz", "id bigint, tt date"),
    "dts":    ("warehouse_day",      "byday",   "id bigint, dt timestamp"),
    "mdz":    ("warehouse_temporal_xt", "bymonth_d", "id bigint, dt timestamptz"),
    "evd8":   ("warehouse_datepart", "byday",   "id bigint, dt bigint, amount int"),
    "byv":    ("warehouse_decimal",  "byint",   "k varchar"),
}


def _open_to_the_server(root):
    """Make `root` and everything under it readable by the postgres user.

    AND EVERY PARENT, which is the half that is easy to miss: one mode-700 directory
    above the tree makes the whole tree unreadable however open its own modes are.
    """
    for parent in [root] + list(root.parents):
        try:
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IROTH | stat.S_IXOTH)
        except (PermissionError, FileNotFoundError):
            break
    for where, dirs, files in os.walk(root):
        os.chmod(where, 0o755)
        for f in files:
            os.chmod(os.path.join(where, f), 0o644)


@pytest.fixture(scope="module")
def ice(pgc_cluster, tmp_path_factory):
    """-> (connection, {alias: metadata_path}) with every available warehouse staged.

    MODULE-SCOPED because staging copies several warehouses and creates one server;
    every test here reads and none writes, so a shared fixture cannot leak state
    between them. A warehouse the tree does not carry is simply absent from the dict,
    and the tests that need it refuse by name rather than passing vacuously.
    """
    import psycopg

    stage = tmp_path_factory.mktemp("iceberg")
    meta = {}
    for alias, spec in WAREHOUSES.items():
        wh, table = spec[0], spec[1]
        version = spec[3] if len(spec) > 3 else ""
        src = FIXTURES / wh / "db" / table
        if not (src / "metadata").is_dir():
            continue
        if not any((src / "data").rglob("*.parquet")):
            continue
        dest = stage / alias / table
        shutil.copytree(src, dest)
        found = sorted((dest / "metadata").glob(version + "*.metadata.json"))
        if found:
            meta[alias] = found[-1]
    _open_to_the_server(stage)

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    with conn.cursor() as cur:
        cur.execute("CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg")
        for alias, path in meta.items():
            cols = WAREHOUSES[alias][2]
            cur.execute(f"CREATE FOREIGN TABLE {alias} ({cols}) "
                        f"SERVER ice OPTIONS (metadata_path '{path}')")
    yield conn, meta
    conn.close()


def _one(conn, sql):
    """-> the single scalar the query returns, as text, or '' for NULL."""
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return "" if row is None or row[0] is None else str(row[0])


def _ids(conn, rel, where):
    """-> the matching ids, comma-joined in id order."""
    return _one(conn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {rel} "
                      f"WHERE {where}")


def _scan_ids(conn, meta_path, cols, where):
    """-> the same ids from `iceberg_scan`, WHICH CANNOT PRUNE.

    That is the whole point of the oracle: the SRF receives no predicate and opens
    every file, so it cannot over-prune. An FDW answer that matches it under the same
    WHERE is right for the right reason.
    """
    return _one(conn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM "
                      f"pgcolumnar.iceberg_scan('{meta_path}') AS t({cols}) "
                      f"WHERE {where}")


def _files_pruned(plan):
    """-> the count EXPLAIN reported, or NO_MARKER when it reported none.

    A MISSING MARKER IS NOT ZERO. "Pruned nothing" and "did not say" are the same
    number and opposite facts, so returning 0 for the second would make every pruning
    arm below pass vacuously the day the FDW stopped reporting. Split out from
    `_pruned` so an arm can reach it without a server: the sentinel is a guard, and a
    guard nothing exercises is the shape of defect this corpus keeps paying for.
    """
    found = re.search(r"Files Pruned: (\d+)", plan)
    return int(found.group(1)) if found else NO_MARKER


def _pruned(conn, sql):
    """-> how many files EXPLAIN ANALYZE says were pruned, or NO_MARKER."""
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql)
        plan = "\n".join(r[0] for r in cur.fetchall())
    return _files_pruned(plan)


def test_a_plan_with_no_pruning_marker_is_not_read_as_zero(expect):
    """THE SENTINEL, EXERCISED. Needs no server, which is why it can exist at all.

    Every pruning arm here compares against a number. If a plan that never mentions
    `Files Pruned` read as 0, an arm expecting 0 would pass while the FDW reported
    nothing at all -- and `no predicate prunes no files` is exactly such an arm. So
    the failure this protects against is not hypothetical: it is the first arm in
    the file.
    """
    expect.num(_files_pruned("Foreign Scan on ev\n  Files Pruned: 3"), 3,
               "a plan that reports pruning is read as that number")
    expect.num(_files_pruned("Foreign Scan on ev\n  Files Pruned: 0"), 0,
               "and a plan reporting zero is read as zero")
    expect.text(_files_pruned("Foreign Scan on ev\n  rows=5 loops=1"), NO_MARKER,
                "but a plan with no marker at all is NOT read as zero, because "
                "'pruned nothing' and 'did not say' are opposite facts")

# The name the bash suite's gate records when the committed warehouse is absent:
#
#     iceberg_fdw.sh:26   || pgc_skip fixture "iceberg warehouse data files are missing"
#
# Carried verbatim so the two harnesses state the same property. Before #1131
# `cannot_run` recorded under its reason CODE and this name could not be emitted at
# all, which is why this pair was the tree's only declared-INCOMPLETE one.
# It is written at the call site rather than held in a constant: `compare_to_bash`
# reads the port's SOURCE and resolves string LITERALS, so a name behind a module
# constant is a name the grader cannot see. Measured -- with the constant, this pair
# still reported `missing: 2`.


def _need(expect, meta, *aliases):
    """-> True when every warehouse is staged; otherwise records ONE refusal.

    Per test, because a refusal is recorded per TEST rather than per check: two
    refusals inside one test would collapse onto a single record and the second
    property would leave no trace at all.
    """
    missing = [a for a in aliases if a not in meta]
    if missing:
        expect.cannot_run("UNMET_PRECONDITION",
                          f"the {', '.join(missing)} warehouse fixture is not in this "
                          f"tree, so the transform it covers cannot be exercised",
                          name="iceberg warehouse data files are missing")
        return False
    return True


# ---------------------------------------------------------------- identity partition
#
# warehouse/db/events is identity-partitioned by region: eu holds ids 1,2 and us holds
# 3,4,5. One data file per region, so a predicate on `region` can drop a whole file
# without opening it.

def test_the_fdw_reads_the_whole_table(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "ev"):
        return
    expect.text(_one(conn, "SELECT string_agg(id||'|'||region||'|'||amount, E'\\n' "
                           "ORDER BY id) FROM ev"),
                "1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50",
                "the FDW reads the whole table (5 rows, no predicate)")
    expect.num(_pruned(conn, "SELECT * FROM ev"), 0, "no predicate prunes no files")


def test_a_partition_predicate_returns_iceberg_scans_rows(ice, expect):
    """THE SAME ORACLE, which is what makes a pruning arm mean anything.

    `iceberg_scan` opens every file and filters afterwards, so it cannot over-prune.
    An FDW answer that matches it under the same predicate is right for the right
    reason; a row count alone would not distinguish "pruned correctly" from "pruned a
    file that held matching rows", because both answers are small.
    """
    conn, meta = ice
    if not _need(expect, meta, "ev"):
        return
    fdw = _one(conn, "SELECT string_agg(id||'|'||region||'|'||amount, E'\\n' "
                     "ORDER BY id) FROM ev WHERE region='eu'")
    expect.text(fdw, "1|eu|10\n2|eu|20", "region='eu' returns the eu rows")
    scan = _one(conn, "SELECT string_agg(id||'|'||region||'|'||amount, E'\\n' ORDER BY id) "
                      f"FROM pgcolumnar.iceberg_scan('{meta['ev']}') "
                      "AS t(id bigint, region text, amount int) WHERE region='eu'")
    expect.text(fdw, scan,
                "the FDW matches iceberg_scan under the same predicate (same oracle)")


def test_a_partition_predicate_prunes_the_other_file(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "ev"):
        return
    expect.num(_pruned(conn, "SELECT * FROM ev WHERE region='eu'"), 1,
               "region='eu' prunes the us file (Files Pruned: 1)")
    expect.num(_pruned(conn, "SELECT * FROM ev WHERE region='us'"), 1,
               "region='us' prunes the eu file (Files Pruned: 1)")
    expect.text(_ids(conn, "ev", "region='us'"), "3,4,5",
                "region='us' returns the three us rows")


def test_a_value_in_no_partition_prunes_everything(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "ev"):
        return
    expect.num(_pruned(conn, "SELECT * FROM ev WHERE region='zz'"), 2,
               "region='zz' prunes both files (Files Pruned: 2)")
    expect.num(int(_one(conn, "SELECT count(*) FROM ev WHERE region='zz'")), 0,
               "region='zz' returns no rows")


# ------------------------------------------------------------------ metrics pruning
#
# `amount` is NOT a partition column, so these prune on the file's own min/max
# recorded in the manifest. Bounds: the eu file is [10,20], the us file [30,50].

METRICS_PRUNE = [
    ("amount > 25", 1, "amount > 25 prunes the eu file by metrics (Files Pruned: 1)"),
    ("amount < 15", 1, "amount < 15 prunes the us file by metrics (Files Pruned: 1)"),
    ("amount = 30", 1, "amount = 30 prunes the eu file, keeps us (Files Pruned: 1)"),
]

METRICS_ROWS = [
    ("amount > 25", "3,4,5", "amount > 25 still returns the right rows"),
    ("amount < 15", "1", "amount < 15 returns only id 1"),
    ("amount = 30", "3", "amount = 30 returns id 3"),
]


# THE NAME COLUMN IS CALLED `name`, and the pruning and rows arms are separate tests
# for that reason rather than for a testing reason. `compare_to_bash.py` resolves a
# parametrize column only when it is literally called `name`, so one test carrying
# `name_prune` and `name_rows` contributes NO names to the grader and every bash
# property it covers is reported MISSING -- measured at 42 before this split. That is
# #1045 class 2, and splitting is how a port stays one-for-one while still
# parametrising.
@pytest.mark.parametrize("where,files,name", METRICS_PRUNE)
def test_a_non_partition_column_prunes_by_file_metrics(ice, expect, where, files, name):
    conn, meta = ice
    if not _need(expect, meta, "ev"):
        return
    expect.num(_pruned(conn, f"SELECT * FROM ev WHERE {where}"), files, name)


@pytest.mark.parametrize("where,ids,name", METRICS_ROWS)
def test_a_metrics_pruned_scan_still_returns_its_rows(ice, expect, where, ids, name):
    conn, meta = ice
    if not _need(expect, meta, "ev"):
        return
    expect.text(_ids(conn, "ev", where), ids, name)


def test_a_value_outside_every_files_metrics_prunes_all(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "ev"):
        return
    expect.num(_pruned(conn, "SELECT * FROM ev WHERE amount = 100"), 2,
               "amount = 100 prunes both files by metrics (Files Pruned: 2)")
    expect.num(int(_one(conn, "SELECT count(*) FROM ev WHERE amount = 100")), 0,
               "amount = 100 returns no rows")
    expect.text(_ids(conn, "ev", "amount >= 30"),
                _scan_ids(conn, meta["ev"], "id bigint, region text, amount int",
                          "amount >= 30"),
                "a metrics-pruned query matches iceberg_scan (same oracle)")


# --------------------------------------------------------- a DATE identity partition
#
# THE #660 BUG, which is why this section is a refusal rather than a pruning arm. The
# FDW cannot convert a date partition cell, so it must read the file IN FULL and let
# the recheck qual filter. The wrong behaviour is to NULL-fill and prune, which drops
# rows -- and the symptom is an empty answer, not an error.

def test_a_date_partition_is_not_over_pruned(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "evd"):
        return
    expect.num(int(_one(conn, "SELECT count(*) FROM evd")), 4,
               "a date-partitioned FDW reads the whole table (4 rows)")
    got = _ids(conn, "evd", "dt=DATE '2020-01-01'")
    expect.text(got, "1,2",
                "dt='2020-01-01' returns its rows, not over-pruned to zero")
    expect.text(got, _scan_ids(conn, meta["evd"], "id bigint, dt date, amount int",
                               "dt=DATE '2020-01-01'"),
                "the date FDW matches iceberg_scan under the same predicate")
    expect.num(_pruned(conn, "SELECT * FROM evd WHERE dt=DATE '2020-01-01'"), 0,
               "a date partition prunes nothing in this release (Files Pruned: 0)")


# ------------------------------------------------------------ bucket[N] partitioning
#
# warehouse_bucket/db/byid is partitioned by bucket[8](id). pyiceberg wrote the
# buckets (1->4, 4->6, 3->3), so a green same-oracle read proves OUR murmur3 and
# bucket arithmetic agree with Iceberg's -- a disagreement would keep the wrong file
# and answer from it. 8 rows across 5 files (buckets 1,3,4,6,7).

BUCKET_PRUNE = [
    (1, "id = 1 keeps only its bucket file (Files Pruned: 4)"),
    (4, "id = 4 keeps only its bucket file (Files Pruned: 4)"),
    # id=5 is in bucket 7. METRICS ALONE would keep bucket-3, whose id range [3,7]
    # contains 5; bucket pruning drops it too. So this probe DISTINGUISHES the two
    # mechanisms, which is why its expected count is 4 and not 3.
    (5, "id = 5 bucket-prunes past metrics (Files Pruned: 4)"),
]

# id = 1 compares the value too, as the shell twin does (#1339): an id alone passes a
# read that keeps the right row and returns the wrong data in it.
BUCKET_ROWS = [
    (1, "1|v1", "id = 1 returns row 1"),
    (4, "4", "id = 4 returns row 4"),
    (5, "5", "id = 5 returns row 5"),
]


@pytest.mark.parametrize("probe,name", BUCKET_PRUNE)
def test_a_bucket_partition_keeps_only_the_matching_bucket(ice, expect, probe, name):
    conn, meta = ice
    if not _need(expect, meta, "evb"):
        return
    expect.num(_pruned(conn, f"SELECT * FROM evb WHERE id = {probe}"), 4, name)


@pytest.mark.parametrize("probe,ids,name", BUCKET_ROWS)
def test_a_bucket_pruned_equality_returns_its_row(ice, expect, probe, ids, name):
    conn, meta = ice
    if not _need(expect, meta, "evb"):
        return
    got = (_one(conn, f"SELECT string_agg(id::text || '|' || val, ',' ORDER BY id) FROM evb WHERE id = {probe}")
           if "|" in ids else _ids(conn, "evb", f"id = {probe}"))
    expect.text(got, ids, name)


def test_the_bucket_table_reads_whole_and_matches_the_murmur3_oracle(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "evb"):
        return
    expect.num(int(_one(conn, "SELECT count(*) FROM evb")), 8,
               "the bucket FDW reads the whole table (8 rows)")
    expect.text(_ids(conn, "evb", "id = 3"),
                _scan_ids(conn, meta["evb"], "id bigint, val text, amount int", "id = 3"),
                "a bucket-pruned equality matches iceberg_scan (murmur3 oracle)")
    expect.text(_ids(conn, "evb", "id = 3"), "3", "id = 3 returns row 3")


def test_a_range_predicate_cannot_bucket_prune(ice, expect):
    """THE HASH DESTROYS ORDER, so `id > 4` says nothing about which bucket to keep.

    Metrics pruning still applies to `id`'s own min/max, and two files hold only
    values <= 4, so the right answer is 2 -- not 4, and not 0. An implementation that
    bucket-pruned a range predicate would drop files holding matching rows.
    """
    conn, meta = ice
    if not _need(expect, meta, "evb"):
        return
    expect.num(_pruned(conn, "SELECT * FROM evb WHERE id > 4"), 2,
               "id > 4 prunes by metrics not bucket, correctly (Files Pruned: 2)")
    expect.text(_ids(conn, "evb", "id > 4"), "5,6,7,8",
                "id > 4 still returns the right rows")


# ------------------------------------- bucket[N] on a string, and the collation (#1818)
#
# warehouse_bucket_text/db/bystr is bucket[16](s), one row per file: 1 abc, 2 ABC,
# 3 Abc, 4 aBc, 5 x, 6 y, in buckets 10, 5, 0, 7, 11, 6. Iceberg hashes the UTF-8
# BYTES, so under a case-insensitive collation four rows equal 'ABC' while sitting in
# four buckets. Pruning by bucket('ABC') there skips three files of matching rows and
# returns one row where iceberg_scan returns four -- no error, just fewer rows. So
# the wrapper must read every file under a nondeterministic collation, and must keep
# pruning under a deterministic one, or the guard has merely switched text off.

def test_a_text_bucket_prunes_under_a_deterministic_collation(ice, expect):
    """THE CONTROL THE GUARD MUST NOT BREAK. The default collation and "C" are both
    deterministic: equal means byte-equal there, so bucket('ABC') names the only file
    that can hold a match. A guard that refused every collatable qual would pass the
    nondeterministic arms below and red these.
    """
    conn, meta = ice
    if not _need(expect, meta, "evs"):
        return
    expect.num(int(_one(conn, "SELECT count(*) FROM evs")), 6,
               "text bucket: the FDW reads the whole table (6 rows)")
    expect.num(_pruned(conn, "SELECT * FROM evs WHERE s = 'ABC'"), 5,
               "text bucket: s = 'ABC' under the default collation prunes 5 files")
    expect.text(_ids(conn, "evs", "s = 'ABC'"), "2",
                "text bucket: s = 'ABC' under the default collation returns row 2")
    expect.num(_pruned(conn, "SELECT * FROM evs WHERE s = 'ABC' COLLATE \"C\""), 5,
               "text bucket: s = 'ABC' under the deterministic C collation still prunes 5 files")


def _ice_ci(expect, conn, meta):
    """-> True once the ICU collation `ice_ci` and the foreign table `evs_ci`, whose
    column is declared with it, exist; otherwise records ONE refusal and -> False.

    Idempotent, so each test below can call it: `cannot_run` declines a whole test,
    while the shell suite's `check_skip` declines only this block, so every test that
    needs the collation refuses on its own.
    """
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE COLLATION IF NOT EXISTS ice_ci (provider = icu, "
                        "locale = 'und-u-ks-level2', deterministic = false)")
            cur.execute(f"CREATE FOREIGN TABLE IF NOT EXISTS evs_ci "
                        f"(id bigint, s text COLLATE ice_ci) SERVER ice "
                        f"OPTIONS (metadata_path '{meta['evs']}')")
    except psycopg.Error:
        expect.cannot_run(
            "MISSING_DEPENDENCY",
            "a case-insensitive collation needs a nondeterministic ICU collation, "
            "and a server built without ICU cannot create one",
            name="text bucket: the nondeterministic collation case")
        return False
    return True


def test_a_nondeterministic_collation_on_the_qual_does_not_bucket_prune(ice, expect):
    """`s = 'ABC' COLLATE ice_ci`: four rows match, in four buckets. The oracle is
    iceberg_scan, which receives no predicate and so cannot prune."""
    conn, meta = ice
    if not _need(expect, meta, "evs") or not _ice_ci(expect, conn, meta):
        return
    expect.text(_ids(conn, "evs", "s = 'ABC' COLLATE ice_ci"), "1,2,3,4",
                "text bucket: s = 'ABC' COLLATE ice_ci returns all four case variants")
    expect.text(
        _one(conn, "SELECT string_agg(id::text || '|' || s, ',' ORDER BY id) FROM evs "
                   "WHERE s = 'ABC' COLLATE ice_ci"),
        _one(conn, f"SELECT string_agg(id::text || '|' || s, ',' ORDER BY id) FROM "
                   f"pgcolumnar.iceberg_scan('{meta['evs']}') AS t(id bigint, s text) "
                   f"WHERE s = 'ABC' COLLATE ice_ci"),
        "text bucket: s = 'ABC' COLLATE ice_ci matches iceberg_scan (same oracle)")
    expect.num(_pruned(conn, "SELECT * FROM evs WHERE s = 'ABC' COLLATE ice_ci"), 0,
               "text bucket: s = 'ABC' COLLATE ice_ci prunes no file (Files Pruned: 0)")


def test_a_nondeterministic_collation_on_the_column_does_not_bucket_prune(ice, expect):
    """The column is declared `COLLATE ice_ci` and the query names no collation. It
    reaches the scan only as the comparison's input collation, so a guard that read
    anything else -- the literal, or the Var -- would see nothing to refuse here, or
    refuse the wrong thing in the arm above."""
    conn, meta = ice
    if not _need(expect, meta, "evs") or not _ice_ci(expect, conn, meta):
        return
    expect.text(_ids(conn, "evs_ci", "s = 'ABC'"), "1,2,3,4",
                "text bucket: a column declared COLLATE ice_ci returns all four case variants")
    expect.num(_pruned(conn, "SELECT * FROM evs_ci WHERE s = 'ABC'"), 0,
               "text bucket: a column declared COLLATE ice_ci prunes no file (Files Pruned: 0)")


def test_an_identity_partition_prunes_under_the_quals_own_collation(ice, expect):
    """Identity pruning evaluates the qual itself, against the file's partition value,
    under the qual's own collation. So 'EU' COLLATE ice_ci keeps the eu file and prunes
    only the us one: the collation that breaks bucket pruning is honoured here."""
    conn, meta = ice
    if not _need(expect, meta, "evs", "ev") or not _ice_ci(expect, conn, meta):
        return
    expect.text(_ids(conn, "ev", "region = 'EU' COLLATE ice_ci"), "1,2",
                "identity: region = 'EU' COLLATE ice_ci returns the eu rows")
    expect.num(_pruned(conn, "SELECT * FROM ev WHERE region = 'EU' COLLATE ice_ci"), 1,
               "identity: region = 'EU' COLLATE ice_ci prunes only the us file (Files Pruned: 1)")


# ----------------------------------------------------------- truncate[W] partitioning
#
# warehouse_trunc/db/bytr is truncate[100](amount) with COLUMN METRICS DISABLED, so
# truncate is the only mechanism left -- metrics would otherwise subsume it and the
# arms would pass without the transform working at all. Files: amount_tr=0 (ids 1,2),
# =100 (3,4), =200 (5).

TRUNC_PRUNE = [
    ("amount < 100", "amount < 100 truncate-prunes 2 files"),
    ("amount = 130", "amount = 130 truncate-prunes 2 files"),
]

TRUNC_ROWS = [
    ("amount < 100", "1,2", "amount < 100 returns ids 1,2"),
    ("amount = 130", "3", "amount = 130 returns id 3"),
]


@pytest.mark.parametrize("where,name", TRUNC_PRUNE)
def test_a_truncate_partition_prunes_by_range(ice, expect, where, name):
    conn, meta = ice
    if not _need(expect, meta, "evt"):
        return
    expect.num(_pruned(conn, f"SELECT * FROM evt WHERE {where}"), 2, name)


@pytest.mark.parametrize("where,ids,name", TRUNC_ROWS)
def test_a_truncate_pruned_scan_still_returns_its_rows(ice, expect, where, ids, name):
    conn, meta = ice
    if not _need(expect, meta, "evt"):
        return
    expect.text(_ids(conn, "evt", where), ids, name)


def test_the_truncate_table_reads_whole_and_matches_the_oracle(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "evt"):
        return
    expect.num(int(_one(conn, "SELECT count(*) FROM evt")), 5,
               "the truncate FDW reads the whole table (5 rows)")
    expect.num(_pruned(conn, "SELECT * FROM evt WHERE amount >= 200"), 2,
               "amount >= 200 truncate-prunes 2 files")
    expect.text(_ids(conn, "evt", "amount >= 200"),
                _scan_ids(conn, meta["evt"], "id bigint, amount int", "amount >= 200"),
                "a truncate-pruned query matches iceberg_scan (same oracle)")


# ------------------------------------------------------------- day() on a DATE column
#
# warehouse_day/db/byday is day(dt), metrics disabled. The stored cell is ICEBERG DAYS
# counted from 1970 and a PostgreSQL date is counted from 2000, so the FDW adds 10957.
# A wrong offset drops the wrong file and still returns a plausible small answer,
# which is why the same-oracle read is the cross-check rather than the row count.

DAY_PRUNE = [
    ("dt = DATE '2020-01-02'", "dt = 2020-01-02 day-prunes 2 files"),
    ("dt < DATE '2020-01-02'", "dt < 2020-01-02 day-prunes 2 files"),
]

DAY_ROWS = [
    ("dt = DATE '2020-01-02'", "2", "dt = 2020-01-02 returns id 2"),
    ("dt < DATE '2020-01-02'", "1", "dt < 2020-01-02 returns id 1"),
]


@pytest.mark.parametrize("where,name", DAY_PRUNE)
def test_a_day_partition_on_a_date_prunes(ice, expect, where, name):
    conn, meta = ice
    if not _need(expect, meta, "evy"):
        return
    expect.num(_pruned(conn, f"SELECT * FROM evy WHERE {where}"), 2, name)


@pytest.mark.parametrize("where,ids,name", DAY_ROWS)
def test_a_day_pruned_scan_still_returns_its_rows(ice, expect, where, ids, name):
    conn, meta = ice
    if not _need(expect, meta, "evy"):
        return
    expect.text(_ids(conn, "evy", where), ids, name)


def test_the_day_table_reads_whole_and_crosschecks_the_epoch(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "evy"):
        return
    expect.num(int(_one(conn, "SELECT count(*) FROM evy")), 3,
               "the day() FDW reads the whole table (3 rows)")
    expect.num(_pruned(conn, "SELECT * FROM evy WHERE dt >= DATE '2020-02-01'"), 2,
               "dt >= 2020-02-01 day-prunes 2 files")
    expect.text(_ids(conn, "evy", "dt >= DATE '2020-02-01'"),
                _scan_ids(conn, meta["evy"], "id bigint, dt date",
                          "dt >= DATE '2020-02-01'"),
                "a day()-pruned query matches iceberg_scan (epoch cross-check)")


# ------------------------------------------- coarse temporal transforms on a TIMESTAMP
#
# warehouse_temporal has four tables partitioned by year(ts), month(ts), day(ts) and
# hour(ts), all with column metrics DISABLED so the transform is the sole mechanism.
#
# EVERY BUCKET HERE IS COARSE: it spans a range of values, so at the boundary bucket
# the file MUST be read. The `>` and `>=` probes below are chosen so the constant's
# own bucket still holds a matching row -- an implementation applying an exact [V,V]
# rule would prune that file and lose the row. That is the failure these arms exist
# for, and it shows as a short answer rather than an error, which is why each is
# paired with the oracle. pyiceberg-core wrote the buckets (year 2020->50, month
# 2021-06->617, day 2021-03-01->18687, hour ->448488).

# THE TABLES ARE LITERAL, not derived from one another by a comprehension. The
# grader resolves a parametrize column through `ast.literal_eval`, which reads a
# literal list and not a comprehension over one -- measured, a comprehension left all
# twenty of these reported MISSING while the arms ran and passed. Two tables with the
# same left-hand columns is the cost of being readable, and the drift between them is
# caught by the pruning arm and the oracle arm disagreeing about which relation they
# are querying.
TEMPORAL_PRUNE = [
    ("ty", "ts > TIMESTAMP '2021-01-01 00:00:00'", 1,
     "ts > 2021-01-01 year-prunes the 2020 file (Files Pruned: 1)"),
    ("tm", "ts >= TIMESTAMP '2021-02-01 00:00:00'", 1,
     "ts >= 2021-02-01 month-prunes the Jan file (Files Pruned: 1)"),
    ("td", "ts >= TIMESTAMP '2021-03-02 00:00:00'", 1,
     "ts >= 2021-03-02 day-prunes the Mar-01 file (Files Pruned: 1)"),
    ("th", "ts >= TIMESTAMP '2021-03-01 01:00:00'", 1,
     "ts >= 2021-03-01 01:00 hour-prunes the 00h file (Files Pruned: 1)"),
    ("yd", "dt > DATE '2021-01-01'", 1,
     "year()-on-date prunes the 2020 file (Files Pruned: 1)"),
    ("mo", "dt >= DATE '2021-02-01'", 1,
     "month()-on-date prunes the Jan file (Files Pruned: 1)"),
    ("yz", "tt > TIMESTAMPTZ '2021-01-01 00:00:00+00'", 1,
     "year()-on-timestamptz prunes the 2020 file (Files Pruned: 1)"),
    ("mz", "tt >= TIMESTAMPTZ '2021-02-01 00:00:00+00'", 1,
     "month()-on-timestamptz prunes the Jan file (Files Pruned: 1)"),
    ("dz", "tt >= TIMESTAMPTZ '2021-03-02 00:00:00+00'", 1,
     "day()-on-timestamptz prunes the Mar-01 file (Files Pruned: 1)"),
    ("hz", "tt >= TIMESTAMPTZ '2021-03-01 01:00:00+00'", 1,
     "hour()-on-timestamptz prunes the 00h file (Files Pruned: 1)"),
]

TEMPORAL_ORACLE = [
    ("ty", "ts > TIMESTAMP '2021-01-01 00:00:00'", "id bigint, ts timestamp",
     "year(): boundary read matches iceberg_scan (no over-prune)"),
    ("tm", "ts >= TIMESTAMP '2021-02-01 00:00:00'", "id bigint, ts timestamp",
     "month(): boundary read matches iceberg_scan"),
    ("td", "ts >= TIMESTAMP '2021-03-02 00:00:00'", "id bigint, ts timestamp",
     "day()-on-timestamp: boundary read matches iceberg_scan"),
    ("th", "ts >= TIMESTAMP '2021-03-01 01:00:00'", "id bigint, ts timestamp",
     "hour(): boundary read matches iceberg_scan"),
    ("yd", "dt > DATE '2021-01-01'", "id bigint, dt date",
     "year()-on-date: boundary read matches iceberg_scan"),
    ("mo", "dt >= DATE '2021-02-01'", "id bigint, dt date",
     "month()-on-date: boundary read matches iceberg_scan"),
    ("yz", "tt > TIMESTAMPTZ '2021-01-01 00:00:00+00'", "id bigint, tt timestamptz",
     "year()-on-timestamptz: boundary read matches iceberg_scan"),
    ("mz", "tt >= TIMESTAMPTZ '2021-02-01 00:00:00+00'", "id bigint, tt timestamptz",
     "month()-on-timestamptz: boundary read matches iceberg_scan"),
    ("dz", "tt >= TIMESTAMPTZ '2021-03-02 00:00:00+00'", "id bigint, tt timestamptz",
     "day()-on-timestamptz: boundary read matches iceberg_scan"),
    ("hz", "tt >= TIMESTAMPTZ '2021-03-01 01:00:00+00'", "id bigint, tt timestamptz",
     "hour()-on-timestamptz: boundary read matches iceberg_scan"),
]


@pytest.mark.parametrize("rel,where,files,name", TEMPORAL_PRUNE,
                         ids=[t[0] for t in TEMPORAL_PRUNE])
def test_a_coarse_temporal_transform_prunes(ice, expect, rel, where, files, name):
    conn, meta = ice
    if not _need(expect, meta, rel):
        return
    expect.num(_pruned(conn, f"SELECT * FROM {rel} WHERE {where}"), files, name)


@pytest.mark.parametrize("rel,where,cols,name", TEMPORAL_ORACLE,
                         ids=[t[0] for t in TEMPORAL_ORACLE])
def test_a_coarse_temporal_transform_does_not_over_prune(
        ice, expect, rel, where, cols, name):
    """THE ORACLE IS THE ARM, not the pruning count.

    A count says a file was dropped. Only the comparison says the dropped file held
    nothing that matched -- and over-pruning shows as a SHORT ANSWER, never as an
    error, so a count-only arm is green on the defect these exist for.
    """
    conn, meta = ice
    if not _need(expect, meta, rel):
        return
    expect.text(_ids(conn, rel, where), _scan_ids(conn, meta[rel], cols, where), name)


def test_the_two_temporal_tables_cover_the_same_cases(expect):
    """The cost of writing both tables out is that they can drift. This is the arm
    that notices: same relations, same predicates, same order."""
    expect.text(", ".join(f"{r}:{w}" for r, w, _f, _n in TEMPORAL_PRUNE),
                ", ".join(f"{r}:{w}" for r, w, _c, _n in TEMPORAL_ORACLE),
                "the pruning table and the oracle table name the same cases in the "
                "same order")


def test_the_year_table_reads_whole_and_keeps_the_boundary_file(ice, expect):
    """The boundary case stated on its own, because it is the one an exact-[V,V] rule
    gets wrong: `ts > 2021-01-01` must KEEP the 2021 file, whose bucket is the
    constant's own bucket, and that file holds ids 2 and 3."""
    conn, meta = ice
    if not _need(expect, meta, "ty"):
        return
    expect.num(int(_one(conn, "SELECT count(*) FROM ty")), 3,
               "the year() FDW reads the whole table (3 rows)")
    expect.text(_ids(conn, "ty", "ts > TIMESTAMP '2021-01-01 00:00:00'"), "2,3",
                "ts > 2021-01-01 keeps the boundary-year file, returns ids 2,3")


def test_a_year_equality_prunes_to_one_file(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "ty"):
        return
    expect.num(_pruned(conn, "SELECT * FROM ty WHERE ts = TIMESTAMP '2021-06-01 00:00:00'"),
               2, "ts = 2021-06-01 year-prunes to one file (Files Pruned: 2)")
    expect.text(_ids(conn, "ty", "ts = TIMESTAMP '2021-06-01 00:00:00'"), "2",
                "ts = 2021-06-01 returns id 2")


def test_a_year_on_date_equality_prunes_to_one_file(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "yd"):
        return
    expect.num(_pruned(conn, "SELECT * FROM yd WHERE dt = DATE '2021-06-01'"), 2,
               "year()-on-date equality prunes to one file (Files Pruned: 2)")
    expect.text(_ids(conn, "yd", "dt = DATE '2021-06-01'"), "2",
                "year()-on-date equality returns id 2")


# ------------------------------- day() and hour() pruning near the largest timestamp
#
# day() and hour() move the constant from the PostgreSQL epoch (2000) to the Iceberg
# one (1970) by adding 30 years of microseconds. Past 294247-01-10 04:00:54.775807
# that sum overflows int64. Before #2230 it wrapped to a large negative bucket, so
# `ts < constant` pruned every file and returned NO ROWS where iceberg_scan returned
# three. Each table holds three rows in 2021. The last safe microsecond must still
# prune every file: the guard is exactly the overflow, not a margin below it.
#
# Literal tables, for the reason TEMPORAL_PRUNE gives: the grader reads literals.

EPOCH_EDGE_ORACLE = [
    ("td", "ts < TIMESTAMP '294276-12-31 23:59:59.999999'", "id bigint, ts timestamp",
     "day()-on-timestamp: ts < the largest timestamp matches iceberg_scan (#2230)"),
    ("th", "ts < TIMESTAMP '294276-12-31 23:59:59.999999'", "id bigint, ts timestamp",
     "hour()-on-timestamp: ts < the largest timestamp matches iceberg_scan (#2230)"),
    ("dz", "tt < TIMESTAMPTZ '294276-12-31 23:59:59.999999+00'", "id bigint, tt timestamptz",
     "day()-on-timestamptz: tt < the largest timestamptz matches iceberg_scan (#2230)"),
    ("hz", "tt < TIMESTAMPTZ '294276-12-31 23:59:59.999999+00'", "id bigint, tt timestamptz",
     "hour()-on-timestamptz: tt < the largest timestamptz matches iceberg_scan (#2230)"),
]

EPOCH_EDGE_PRUNE = [
    ("td", "ts <= TIMESTAMP '294247-01-10 04:00:54.775808'", 0,
     "day()-on-timestamp: ts <= the first overflowing microsecond prunes no file "
     "(Files Pruned: 0) (#2230)"),
    ("td", "ts > TIMESTAMP '294247-01-10 04:00:54.775807'", 3,
     "day()-on-timestamp: ts > the last safe microsecond still prunes every file "
     "(Files Pruned: 3) (#2230)"),
    ("th", "ts <= TIMESTAMP '294247-01-10 04:00:54.775808'", 0,
     "hour()-on-timestamp: ts <= the first overflowing microsecond prunes no file "
     "(Files Pruned: 0) (#2230)"),
    ("th", "ts > TIMESTAMP '294247-01-10 04:00:54.775807'", 3,
     "hour()-on-timestamp: ts > the last safe microsecond still prunes every file "
     "(Files Pruned: 3) (#2230)"),
    ("dz", "tt <= TIMESTAMPTZ '294247-01-10 04:00:54.775808+00'", 0,
     "day()-on-timestamptz: tt <= the first overflowing microsecond prunes no file "
     "(Files Pruned: 0) (#2230)"),
    ("dz", "tt > TIMESTAMPTZ '294247-01-10 04:00:54.775807+00'", 3,
     "day()-on-timestamptz: tt > the last safe microsecond still prunes every file "
     "(Files Pruned: 3) (#2230)"),
    ("hz", "tt <= TIMESTAMPTZ '294247-01-10 04:00:54.775808+00'", 0,
     "hour()-on-timestamptz: tt <= the first overflowing microsecond prunes no file "
     "(Files Pruned: 0) (#2230)"),
    ("hz", "tt > TIMESTAMPTZ '294247-01-10 04:00:54.775807+00'", 3,
     "hour()-on-timestamptz: tt > the last safe microsecond still prunes every file "
     "(Files Pruned: 3) (#2230)"),
]


@pytest.mark.parametrize("rel,where,cols,name", EPOCH_EDGE_ORACLE,
                         ids=[t[0] for t in EPOCH_EDGE_ORACLE])
def test_a_constant_past_the_epoch_shift_limit_does_not_prune_every_file(
        ice, expect, rel, where, cols, name):
    """#2230: the wrapped bucket shows as an EMPTY answer, never as an error."""
    conn, meta = ice
    if not _need(expect, meta, rel):
        return
    expect.text(_ids(conn, rel, where), _scan_ids(conn, meta[rel], cols, where), name)


@pytest.mark.parametrize("rel,where,files,name", EPOCH_EDGE_PRUNE,
                         ids=[f"{t[0]}-{t[2]}" for t in EPOCH_EDGE_PRUNE])
def test_day_and_hour_pruning_stop_exactly_at_the_epoch_shift_limit(
        ice, expect, rel, where, files, name):
    """#2230: one microsecond past the limit prunes nothing; the last safe one still
    prunes all three files, so the guard is not a margin that gives up early."""
    conn, meta = ice
    if not _need(expect, meta, rel):
        return
    expect.num(_pruned(conn, f"SELECT * FROM {rel} WHERE {where}"), files, name)


def test_a_constant_on_the_left_past_the_epoch_shift_limit_keeps_every_row(ice, expect):
    """#2230: the same overflow with the operator commuted (`const > ts`)."""
    conn, meta = ice
    if not _need(expect, meta, "td"):
        return
    expect.text(_ids(conn, "td", "TIMESTAMP '294276-12-31 23:59:59.999999' > ts"), "1,2,3",
                "day()-on-timestamp: the largest timestamp > ts, constant on the left, "
                "returns ids 1,2,3 (#2230)")


# ------------------------------- a temporal transform read into the OTHER timestamp type
#
# The reader converts a naive (timestamp) source read into timestamptz, and a
# timestamptz source read into timestamp, as PostgreSQL's casts do in the session zone
# (#1706). The writer computed each partition cell in the source's own kind, so a cell
# does not bound the converted rows and the FDW must not prune on it. In New York,
# byhour's naive 2021-03-01 00:00, 01:00, 05:00 are 05:00Z, 06:00Z and 10:00Z; byhour_tz's
# 00:00Z, 01:00Z, 05:00Z are the wall times 2021-02-28 19:00, 20:00 and 2021-03-01 00:00.
# Measured before the guard: the FDW returned no rows where iceberg_scan returned two.

def _in_new_york(conn, read):
    """-> read(), with the shared connection's zone set to New York for that read only."""
    with conn.cursor() as cur:
        cur.execute("SET TimeZone = 'America/New_York'")
    try:
        return read()
    finally:
        with conn.cursor() as cur:
            cur.execute("RESET TimeZone")


def test_an_hour_partition_over_a_naive_source_read_as_timestamptz_keeps_its_rows(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "hw"):
        return
    where = "ts >= TIMESTAMPTZ '2021-03-01 06:00:00+00'"
    got, want = _in_new_york(conn, lambda: (
        _ids(conn, "hw", where),
        _scan_ids(conn, meta["hw"], "id bigint, ts timestamptz", where)))
    expect.text(got, want,
                "hour() on a naive source read as timestamptz in New York matches iceberg_scan")


def test_an_hour_partition_over_a_timestamptz_source_read_as_timestamp_keeps_its_rows(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "hn"):
        return
    where = "tt <= TIMESTAMP '2021-02-28 20:00:00'"
    got, want = _in_new_york(conn, lambda: (
        _ids(conn, "hn", where),
        _scan_ids(conn, meta["hn"], "id bigint, tt timestamp", where)))
    expect.text(got, want,
                "hour() on a timestamptz source read as timestamp in New York matches iceberg_scan")


# ------------------------------------------------- a declared typmod, and pruning (#1790)
#
# The foreign table returns each value as its declared type, typmod included. A typmod
# can change a value: varchar(2) reads the stored 'eu  ' as 'eu', and timestamp(0)
# reads 00:59:59.7 as 01:00:00. The manifest's partition value is the stored one, so
# a file pruned on it could hold a row the query must return. warehouse_typmod
# (fixtures/iceberg/gen_iceberg_typmod.py) holds those two values, each in its own
# partition.

def test_a_varchar_1_iceberg_foreign_column_refuses_a_longer_value(ice, expect):
    import psycopg
    conn, meta = ice
    if not _need(expect, meta, "ev_v1"):
        return
    try:
        got = "no error (" + _one(conn, "SELECT string_agg(region, ',' ORDER BY region) FROM ev_v1") + ")"
    except psycopg.Error as e:
        got = e.sqlstate
    expect.text(got, "22001", "typmod: a varchar(1) Iceberg foreign column refuses 'eu' (22001)")


def test_a_varchar_2_identity_partition_keeps_a_row_stored_with_trailing_spaces(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "pad2"):
        return
    expect.text(_ids(conn, "pad2", "region = 'eu'") or "none", "1",
                "typmod: region = 'eu' on varchar(2) returns id 1, stored as 'eu  '")


def test_a_timestamp_0_hour_partition_keeps_a_row_that_rounds_into_range(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "frac0"):
        return
    expect.text(_ids(conn, "frac0", "ts >= TIMESTAMP '2021-03-01 01:00:00'") or "none", "1,2",
                "typmod: ts >= 01:00 on timestamp(0) returns id 1, stored at 00:59:59.7, and id 2")


def test_a_varchar_2_bucket_partition_keeps_a_row_stored_with_trailing_spaces(ice, expect):
    """'eu  ' (id 1) and 'eu' (id 3) sit in different buckets but both read 'eu'. Today
    the planner casts a varchar column to text for "=", so the qual never reaches
    bucket pruning; the typmod guard in ice_fdw_bucket_quals keeps id 1 if that changes."""
    conn, meta = ice
    if not _need(expect, meta, "bkt2"):
        return
    expect.text(_ids(conn, "bkt2", "region = 'eu'") or "none", "1,3",
                "typmod: region = 'eu' on a bucket-partitioned varchar(2) returns ids 1 and 3")


def test_a_timestamp_6_hour_partition_still_prunes(ice, expect):
    """Microsecond precision changes no value, so the column keeps hour pruning."""
    conn, meta = ice
    if not _need(expect, meta, "th6"):
        return
    expect.num(_pruned(conn, "SELECT * FROM th6 WHERE ts >= TIMESTAMP '2021-03-01 01:00:00'"), 1,
               "typmod: ts >= 01:00 on timestamp(6) still hour-prunes the 00h file (Files Pruned: 1)")


# ------------------------------------- a decimal declared int or bigint, and pruning (#2086)
#
# Spark stores a decimal of precision 18 or less as a Parquet INT32 or INT64 holding
# the unscaled value, and the reader lets the foreign table declare it int or bigint.
# Iceberg writes a decimal's bounds as big-endian bytes, where an integer's are
# little-endian, and its bucket[N] hashes the decimal's bytes, not a long. So neither
# the bounds nor the bucket of a decimal can be read as an integer's.
# warehouse_decimal (fixtures/iceberg/gen_iceberg_decimal.py) holds the unscaled
# values 10000000 (id 1) and 20000000 (id 2) in dec9 (decimal(9,2) as INT32), dec18
# (decimal(18,2) as INT64) and dec9_bucket (bucket[8](d), one file per row).

def _sqlstate(conn, sql):
    """-> the SQLSTATE the query raises, or 'no error (<rows>)'."""
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return f"no error ({cur.fetchall()!r})"
    except psycopg.Error as e:
        return e.sqlstate


def test_a_decimal_9_read_as_int_is_not_pruned_on_its_bounds(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "dec9i"):
        return
    expect.text(_one(conn, "SELECT string_agg(id||'|'||d, ',' ORDER BY id) FROM dec9i"),
                "1|10000000,2|20000000",
                "decimal: dec9 read as int returns both unscaled values (premise)")
    fdw = _ids(conn, "dec9i", "d = 20000000") or "none"
    expect.text(fdw, "2", "decimal: d = 20000000 on decimal(9,2) read as int returns id 2")
    expect.text(fdw, _scan_ids(conn, meta["dec9i"], "id bigint, d int", "d = 20000000") or "none",
                "decimal: d = 20000000 on decimal(9,2) read as int matches iceberg_scan")
    expect.num(int(_one(conn, "SELECT count(*) FROM dec9i WHERE d > 15000000")), 1,
               "decimal: count where d > 15000000 on decimal(9,2) read as int is 1")


def test_a_decimal_18_read_as_bigint_is_not_pruned_on_its_bounds(ice, expect):
    """The most serious case: the declaration is one the full scan accepts."""
    conn, meta = ice
    if not _need(expect, meta, "dec18b"):
        return
    expect.text(_ids(conn, "dec18b", "d = 20000000") or "none", "2",
                "decimal: d = 20000000 on decimal(18,2) read as bigint returns id 2")
    expect.text(_ids(conn, "dec18b", "d >= 15000000") or "none", "2",
                "decimal: d >= 15000000 on decimal(18,2) read as bigint returns id 2")


def test_a_refused_decimal_declaration_is_refused_with_a_filter_too(ice, expect):
    """bigint over an INT32 decimal is refused (42804). A filter must not turn the
    refusal into an empty answer by pruning every file before one is opened."""
    conn, meta = ice
    if not _need(expect, meta, "dec9b"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM dec9b"), "42804",
                "decimal: a full scan of decimal(9,2) read as bigint is refused (42804)")
    expect.text(_sqlstate(conn, "SELECT * FROM dec9b WHERE d = 20000000"), "42804",
                "decimal: a filtered scan of decimal(9,2) read as bigint is refused the same way, not empty")


def test_a_bucket_partition_on_a_decimal_read_as_int_is_not_pruned(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "dec9k"):
        return
    expect.text(_ids(conn, "dec9k", "d = 10000000") or "none", "1",
                "decimal: d = 10000000 on bucket[8](decimal) read as int returns id 1")
    expect.text(_ids(conn, "dec9k", "d = 20000000") or "none", "2",
                "decimal: d = 20000000 on bucket[8](decimal) read as int returns id 2")


# dec9kb reads the bucket source as bigint, which the reader refuses. The bucket map must
# not hash 20000000 as a long and prune both files, which returned 0 rows and no error.

def test_a_bigint_column_over_a_decimal_bucket_is_refused_with_a_filter_too(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "dec9kb"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM dec9kb WHERE d = 20000000"), "42804",
                "decimal: d = 20000000 on bucket[8](decimal) read as bigint is refused (42804), not empty")


def test_the_long_column_beside_a_decimal_still_prunes(ice, expect):
    """The fix stops pruning on a decimal's bounds, not min/max pruning as a whole."""
    conn, meta = ice
    if not _need(expect, meta, "dec9i"):
        return
    expect.num(_pruned(conn, "SELECT * FROM dec9i WHERE id = 5"), 1,
               "decimal: id = 5 on the long column of dec9 still prunes its file (Files Pruned: 1)")


def test_the_field_type_lookup_matches_the_whole_field_name(ice, expect):
    """pfx puts the long field d_x before the decimal d. A lookup that matched the
    column name as a prefix would take d for a long and prune on its bounds."""
    conn, meta = ice
    if not _need(expect, meta, "dpfx"):
        return
    expect.text(_ids(conn, "dpfx", "d = 20000000") or "none", "2",
                "decimal: d = 20000000 on a decimal after a long field whose name it prefixes returns id 2")


def test_the_field_type_lookup_does_not_match_a_field_that_prefixes_the_column(ice, expect):
    """pfx2 puts the long field d before the decimal d_x. A lookup that compared only
    the field name's length would take d_x for a long and prune on its bounds."""
    conn, meta = ice
    if not _need(expect, meta, "dpfx2"):
        return
    expect.text(_ids(conn, "dpfx2", "d_x = 20000000") or "none", "2",
                "decimal: d_x = 20000000 on a decimal after a long field whose name prefixes it returns id 2")


# ---------------------------------- a bool column prunes on bounds only over an Iceberg boolean
#
# boolt is an Iceberg boolean: ids 1, 2 with b false in one file, ids 3, 4 with b true
# in another. booli is an Iceberg int (5 to 8) declared bool, which the reader refuses.
# The tests use b > false and b < true: the planner folds b = true to a bare
# "Filter: b", which never reaches the bounds check.

def test_a_bool_column_over_an_iceberg_boolean_prunes_on_its_bounds(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "boolt"):
        return
    expect.num(_pruned(conn, "SELECT * FROM boolt WHERE b > false"), 1,
               "bool: b > false on an Iceberg boolean prunes the all-false file (Files Pruned: 1)")
    expect.text(_ids(conn, "boolt", "b > false") or "none", "3,4",
                "bool: b > false on an Iceberg boolean returns ids 3 and 4")


def test_a_bool_column_over_an_iceberg_int_is_refused_with_a_filter_too(ice, expect):
    """The bounds of an Iceberg int are not a boolean's. Reading them as one prunes
    the file, and the refused declaration returns an empty answer instead."""
    conn, meta = ice
    if not _need(expect, meta, "booli"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM booli"), "42804",
                "bool: a full scan of an Iceberg int declared bool is refused (42804)")
    expect.text(_sqlstate(conn, "SELECT * FROM booli WHERE b < true"), "42804",
                "bool: b < true on an Iceberg int declared bool is refused the same way, not empty")


def test_a_bool_column_over_an_iceberg_long_is_refused_with_a_filter_too(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "booll"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM booll WHERE b < true"), "42804",
                "bool: b < true on an Iceberg long declared bool is refused (42804), not empty")


# ------------------------------- an integer column prunes on bounds only over an Iceberg int or long
#
# booli2 reads the Iceberg int of booli as smallint. boolti reads the Iceberg boolean of
# boolt as int, which the reader refuses.

def test_a_smallint_column_over_an_iceberg_int_prunes_on_its_bounds(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "booli2"):
        return
    expect.num(_pruned(conn, "SELECT * FROM booli2 WHERE b > 8"), 1,
               "int: b > 8 on a smallint over an Iceberg int prunes its file (Files Pruned: 1)")
    expect.text(_ids(conn, "booli2", "b = 8") or "none", "8",
                "int: b = 8 on a smallint over an Iceberg int returns id 8")


def test_an_int_column_over_an_iceberg_boolean_is_refused_with_a_filter_too(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "boolti"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM boolti"), "42804",
                "int: a full scan of an Iceberg boolean declared int is refused (42804)")
    expect.text(_sqlstate(conn, "SELECT * FROM boolti WHERE b = 5"), "42804",
                "int: b = 5 on an Iceberg boolean declared int is refused the same way, not empty")


# boolts reads the same Iceberg boolean as smallint. The int2 arm must check the Iceberg
# type too, or b = 5 prunes both files on the boolean's bounds and returns 0 rows.

def test_a_smallint_column_over_an_iceberg_boolean_is_refused_with_a_filter_too(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "boolts"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM boolts WHERE b = 5"), "42804",
                "int: b = 5 on a smallint over an Iceberg boolean is refused (42804), not empty")


# bib reads booli's Iceberg int as bigint. The int8 arm must accept an Iceberg int, not
# only a long, or b > 8 loses the bounds pruning main has.

def test_a_bigint_column_over_an_iceberg_int_prunes_on_its_bounds(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "bib"):
        return
    expect.num(_pruned(conn, "SELECT * FROM bib WHERE b > 8"), 1,
               "int: b > 8 on a bigint over an Iceberg int prunes its file (Files Pruned: 1)")


# ------------------------------------------------------- bucket pruning on an Iceberg int source
#
# byint: k int, bucket[8](k), k = 1 to 8, written by pyiceberg into five files. Iceberg
# hashes an int as an 8-byte long. k = 5 is in bucket 7 with k = 8. Bounds alone prune
# only 3 files: the bucket 3 file holds k = 3 and 7, so its bounds contain 5.

def test_bucket_pruning_works_on_an_iceberg_int_source(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "byint"):
        return
    expect.num(_pruned(conn, "SELECT * FROM byint WHERE k = 5"), 4,
               "bucket: k = 5 on bucket[8] of an Iceberg int prunes 4 of 5 files (Files Pruned: 4)")
    expect.text(_one(conn, "SELECT string_agg(k::text, ',' ORDER BY k) FROM byint WHERE k = 5")
                or "none", "5",
                "bucket: k = 5 on bucket[8] of an Iceberg int returns 5")


# byb reads the same Iceberg int as bigint. The bucket gate must accept a bigint over an
# Iceberg int, or k = 5 loses the bucket pruning main has.

def test_bucket_pruning_works_for_a_bigint_column_over_an_iceberg_int(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "byb"):
        return
    expect.num(_pruned(conn, "SELECT * FROM byb WHERE k = 5"), 4,
               "bucket: k = 5 on a bigint over bucket[8] of an Iceberg int prunes 4 of 5 files (Files Pruned: 4)")


# byintt reads the same Iceberg int as text. The reader refuses a full scan (42804). The
# bucket map must not hash 'a' as an int's bucket and prune every file, which returned 0
# rows and no error.

def test_a_text_column_over_an_iceberg_int_is_refused_with_a_filter_too(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "byintt"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM byintt WHERE k = 'a'"), "42804",
                "bucket: k = 'a' on a text column over an Iceberg int is refused (42804), not empty")


# byn reads the same Iceberg int as name, which the reader refuses. k = 'a'::text uses the
# name = text operator, so the bucket map sees a text constant. The gate must refuse a name
# column, or every file is pruned and the query returns 0 rows and no error.

def test_a_name_column_over_an_iceberg_int_bucket_is_refused_with_a_filter_too(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "byn"):
        return
    expect.text(_sqlstate(conn, "SELECT * FROM byn WHERE k = 'a'::text"), "42804",
                "bucket: k = 'a' on a name column over an Iceberg int is refused (42804), not empty")


# two: k int, d decimal(9,2), partitioned by bucket[2](d) then bucket[4](k), k = 1 to 16
# with d = 1.00, written by pyiceberg into four files. The gate skips the decimal's bucket
# field. The bucket map must still keep k's field: k = 5 prunes 3 files by bucket, but
# only 1 by bounds.

def test_bucket_pruning_keeps_a_field_after_a_gated_decimal_bucket_field(ice, expect):
    conn, meta = ice
    if not _need(expect, meta, "two"):
        return
    expect.num(_pruned(conn, "SELECT * FROM two WHERE k = 5"), 3,
               "bucket: k = 5 after a gated bucket[2](decimal) field prunes 3 of 4 files (Files Pruned: 3)")
    expect.text(_one(conn, "SELECT string_agg(k::text || ':' || d::text, ',' ORDER BY k) "
                           "FROM two WHERE k = 5") or "none", "5:1.00",
                "bucket: k = 5 after a gated bucket[2](decimal) field returns 5 with d 1.00")


# ------------------------------------ a filter does not decide a column type refusal (#2190)
#
# The reader refuses a column type per data file: an int over a file that stores the
# field as Parquet INT64, say. The FDW prunes files on their partition values and bounds
# before it opens them, so a filter that pruned every refused file used to return rows,
# or nothing, where the same scan without the filter was refused. The FDW now checks each
# column's type against the Iceberg field's current type before it prunes anything.
#
# events: id long, region (identity: eu ids 1-2, us ids 3-5), amount int with bounds eu
# [10,20], us [30,50]. prom: id long, r string (identity), v int promoted to long; d0
# (1, a, -5) and d1 (2, b, 7) store v as INT32, d2 (3, c, 9) as INT64; prom2 reads the
# metadata version with the promotion and only the INT32 files, prom1 the one before the
# promotion. boolp: identity on a boolean, ids 1-2 false, 3-4 true. timep: identity on a
# time, ids 1-2 at 01:00, id 3 at 02:00 (cells in microseconds).

# the type each Iceberg type's refusal hint tells the user to declare
DECLARE_AS = {"long": "bigint", "string": "text", "boolean": "boolean", "time": "time",
              "timestamp": "timestamp", "timestamptz": "timestamp with time zone",
              "date": "date", "int": "integer"}


def _refusal(conn, sql, rel, col, typ, icetype):
    """-> '42804' when `sql` is refused with the up-front message for that column and
    the hint to declare it as DECLARE_AS[icetype], 'SQLSTATE (another refusal:
    MESSAGE / HINT)' for any other error, and 'no error (<rows>)' when it ran."""
    import psycopg
    want = (f'column "{col}" of foreign table "{rel}" has type {typ}, '
            f"which cannot read the Iceberg type {icetype}")
    want_hint = f"Declare the column as {DECLARE_AS[icetype]}."
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return f"no error ({cur.fetchall()!r})"
    except psycopg.Error as e:
        msg = e.diag.message_primary
        hint = e.diag.message_hint
        if msg == want and hint == want_hint:
            return e.sqlstate
        return f"{e.sqlstate} (another refusal: {msg} / {hint})"


FILTER_REFUSALS = [
    ("SELECT * FROM evi", "evi", "id", "integer", "long",
     "filter refusal: a full scan of an Iceberg long declared int is refused (42804)"),
    ("SELECT count(*) FROM evi WHERE region = 'zz'", "evi", "id", "integer", "long",
     "filter refusal: region = 'zz' (partition prunes every file) on an int over a long is refused (42804)"),
    ("SELECT * FROM evi WHERE region = 'eu'", "evi", "id", "integer", "long",
     "filter refusal: region = 'eu' (partition prunes one file) on an int over a long is refused (42804)"),
    ("SELECT count(*) FROM evi WHERE amount > 100", "evi", "id", "integer", "long",
     "filter refusal: amount > 100 (bounds prune every file) on an int over a long is refused (42804)"),
    ("SELECT * FROM evi WHERE amount > 25", "evi", "id", "integer", "long",
     "filter refusal: amount > 25 (bounds prune one file) on an int over a long is refused (42804)"),
    ("SELECT * FROM evi WHERE amount > 0", "evi", "id", "integer", "long",
     "filter refusal: amount > 0 (prunes no file) on an int over a long is refused (42804)"),
    ("SELECT count(*) FROM evi WHERE id = 99", "evi", "id", "integer", "long",
     "filter refusal: id = 99 (the refused column's own bounds prune every file) is refused (42804)"),
    ("SELECT count(*) FROM evsm WHERE region = 'zz'", "evsm", "id", "smallint", "long",
     "filter refusal: region = 'zz' on a smallint over a long is refused (42804)"),
    ("SELECT * FROM evri", "evri", "region", "integer", "string",
     "filter refusal: a full scan of an Iceberg string declared int is refused (42804)"),
    ("SELECT * FROM prom", "prom", "v", "integer", "long",
     "filter refusal: a full scan of a promoted long declared int is refused (42804)"),
    ("SELECT * FROM prom WHERE v = -5", "prom", "v", "integer", "long",
     "filter refusal: v = -5 (bounds prune the INT64 file) on a promoted long declared int is refused (42804)"),
    ("SELECT * FROM prom WHERE r = 'a'", "prom", "v", "integer", "long",
     "filter refusal: r = 'a' (partition prunes the INT64 file) on a promoted long declared int is refused (42804)"),
    ("SELECT count(*) FROM prom WHERE v > 100", "prom", "v", "integer", "long",
     "filter refusal: v > 100 (bounds prune every file) on a promoted long declared int is refused (42804)"),
    ("SELECT * FROM prom WHERE r = 'c'", "prom", "v", "integer", "long",
     "filter refusal: r = 'c' (reads only the INT64 file) on a promoted long declared int is refused (42804)"),
    ("SELECT * FROM prom2", "prom2", "v", "integer", "long",
     "filter refusal: a promoted long declared int is refused while every file is still INT32 (42804)"),
    ("SELECT * FROM boolpi", "boolpi", "b", "integer", "boolean",
     "filter refusal: a full scan of a boolean identity partition declared int is refused (42804)"),
    ("SELECT count(*) FROM boolpi WHERE b = 5", "boolpi", "b", "integer", "boolean",
     "filter refusal: b = 5 (partition prunes every file) on a boolean declared int is refused (42804)"),
    ("SELECT * FROM boolpi WHERE b = 1", "boolpi", "b", "integer", "boolean",
     "filter refusal: b = 1 (partition prunes one file) on a boolean declared int is refused (42804)"),
    ("SELECT * FROM timepi", "timepi", "t", "integer", "time",
     "filter refusal: a full scan of a time identity partition declared int is refused (42804)"),
    ("SELECT count(*) FROM timepi WHERE t = 5", "timepi", "t", "integer", "time",
     "filter refusal: t = 5 (partition prunes every file) on a time declared int is refused (42804)"),
    ("SELECT count(*) FROM tyd WHERE ts < DATE '1900-01-01'", "tyd", "ts", "date", "timestamp",
     "filter refusal: ts < 1900-01-01 (year prunes every file) on a date over an Iceberg timestamp is refused (42804)"),
    ("SELECT count(*) FROM tzd WHERE tt < DATE '1900-01-01'", "tzd", "tt", "date", "timestamptz",
     "filter refusal: tt < 1900-01-01 (year prunes every file) on a date over an Iceberg timestamptz is refused (42804)"),
    ("SELECT count(*) FROM dts WHERE dt < TIMESTAMP '1900-01-01'", "dts", "dt",
     "timestamp without time zone", "date",
     "filter refusal: dt < 1900-01-01 (day prunes every file) on a timestamp over an Iceberg date is refused (42804)"),
    ("SELECT count(*) FROM mdz WHERE dt < TIMESTAMPTZ '1900-01-01 00:00+00'", "mdz", "dt",
     "timestamp with time zone", "date",
     "filter refusal: dt < 1900-01-01 (month prunes every file) on a timestamptz over an Iceberg date is refused (42804)"),
    ("SELECT count(*) FROM evd8 WHERE dt = 5", "evd8", "dt", "bigint", "date",
     "filter refusal: dt = 5 (partition prunes every file) on a bigint over an Iceberg date is refused (42804)"),
    ("SELECT * FROM booli WHERE b < true", "booli", "b", "boolean", "int",
     "filter refusal: b < true on a bool over an Iceberg int is refused up front (42804)"),
    ("SELECT * FROM byintt WHERE k = 'a'", "byintt", "k", "text", "int",
     "filter refusal: k = 'a' on a text column over an Iceberg int is refused up front (42804)"),
    ("SELECT * FROM byv", "byv", "k", "character varying", "int",
     "filter refusal: a full scan of an Iceberg int declared varchar is refused up front (42804)"),
]


@pytest.mark.parametrize("sql,rel,col,typ,icetype,name", FILTER_REFUSALS)
def test_a_filter_does_not_hide_a_column_type_refusal(ice, expect, sql, rel, col, typ,
                                                       icetype, name):
    conn, meta = ice
    if not _need(expect, meta, rel):
        return
    expect.text(_refusal(conn, sql, rel, col, typ, icetype), "42804", name)


def _rows(conn, rel, expr):
    """-> the id-ordered answer, 'none' for no rows, or 'error SQLSTATE' when the
    query is refused, so a control that is wrongly refused fails by its name."""
    import psycopg
    try:
        return _one(conn, f"SELECT string_agg({expr}, ',' ORDER BY id) FROM {rel}") or "none"
    except psycopg.Error as e:
        return f"error {e.sqlstate}"


def _pruned_or_refused(conn, sql):
    """-> _pruned's count, or -1 when the query is refused, so a control that is
    wrongly refused fails by its name (no plan prunes a negative number of files)."""
    import psycopg
    try:
        return _pruned(conn, sql)
    except psycopg.Error:
        return -1


FILTER_REFUSAL_CONTROL_ROWS = [
    ("ev", "ev WHERE id = 3", "id::text", "3",
     "filter refusal: control: id = 3 on a bigint over the long returns id 3"),
    ("bib", "bib WHERE b = 8", "id::text", "8",
     "filter refusal: control: b = 8 on a bigint over an Iceberg int returns id 8"),
    ("prom1", "prom1", "id || '|' || r || '|' || v", "1|a|-5,2|b|7",
     "filter refusal: control: before the promotion an int column reads both INT32 files"),
    ("promb", "promb", "id || '|' || r || '|' || v", "1|a|-5,2|b|7,3|c|9",
     "filter refusal: control: a bigint over the promoted long reads all three files"),
    ("promb", "promb WHERE v = -5", "id::text", "1",
     "filter refusal: control: v = -5 on the bigint returns id 1"),
    ("promb", "promb WHERE r = 'c'", "id || '|' || v", "3|9",
     "filter refusal: control: r = 'c' on the bigint returns id 3 from the INT64 file"),
    ("boolpb", "boolpb WHERE NOT b", "id::text", "1,2",
     "filter refusal: control: NOT b on the boolean partition returns ids 1 and 2"),
    ("timepb", "timepb WHERE t = 3600000000", "id::text", "1,2",
     "filter refusal: control: a bigint over the time partition returns ids 1 and 2 for 01:00"),
]


@pytest.mark.parametrize("alias,rel,expr,rows,name", FILTER_REFUSAL_CONTROL_ROWS)
def test_a_column_type_the_reader_accepts_still_returns_its_rows(ice, expect, alias, rel,
                                                                 expr, rows, name):
    conn, meta = ice
    if not _need(expect, meta, alias):
        return
    expect.text(_rows(conn, rel, expr), rows, name)


FILTER_REFUSAL_CONTROL_PRUNED = [
    ("ev", "SELECT * FROM ev WHERE id = 3", 1,
     "filter refusal: control: id = 3 on a bigint over the long prunes the eu file (Files Pruned: 1)"),
    ("ev", "SELECT * FROM ev WHERE amount > 100", 2,
     "filter refusal: control: amount > 100 on the bigint table prunes both files (Files Pruned: 2)"),
    ("bib", "SELECT * FROM bib WHERE b > 8", 1,
     "filter refusal: control: b > 8 on a bigint over an Iceberg int prunes its file (Files Pruned: 1)"),
    ("prom1", "SELECT * FROM prom1 WHERE v = 7", 1,
     "filter refusal: control: v = 7 before the promotion prunes the other file (Files Pruned: 1)"),
    ("promb", "SELECT * FROM promb WHERE v = -5", 2,
     "filter refusal: control: v = -5 on the bigint prunes the other two files (Files Pruned: 2)"),
    ("promb", "SELECT * FROM promb WHERE r = 'c'", 2,
     "filter refusal: control: r = 'c' on the bigint prunes the two INT32 files (Files Pruned: 2)"),
    ("boolpb", "SELECT * FROM boolpb WHERE NOT b", 1,
     "filter refusal: control: NOT b on the boolean partition prunes the true file (Files Pruned: 1)"),
    ("timepb", "SELECT * FROM timepb WHERE t = 3600000000", 1,
     "filter refusal: control: t = 01:00 in microseconds on the bigint prunes the 02:00 file (Files Pruned: 1)"),
]


@pytest.mark.parametrize("alias,sql,files,name", FILTER_REFUSAL_CONTROL_PRUNED)
def test_a_column_type_the_reader_accepts_still_prunes(ice, expect, alias, sql, files, name):
    conn, meta = ice
    if not _need(expect, meta, alias):
        return
    expect.num(_pruned_or_refused(conn, sql), files, name)


# ------------------------------------------------------------------- option validation

def test_an_unknown_table_option_is_refused(ice, expect):
    """The validator, asserted by SQLSTATE rather than by message text.

    `HV00D` is `FDW_INVALID_OPTION_NAME`. A grep on the wording would pass on a
    server that changed the phrasing and fail on one that translated it.
    """
    import psycopg
    conn, meta = ice
    state = "noerror"
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE FOREIGN TABLE bad (id bigint) "
                        "SERVER ice OPTIONS (nosuch 'x')")
    except psycopg.Error as e:
        state = e.sqlstate
    expect.text(state, "HV00D",
                "an unknown table option is refused (FDW_INVALID_OPTION_NAME)")

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
    for alias, (wh, table, _cols) in WAREHOUSES.items():
        src = FIXTURES / wh / "db" / table
        if not (src / "metadata").is_dir():
            continue
        if not any((src / "data").rglob("*.parquet")):
            continue
        dest = stage / alias / table
        shutil.copytree(src, dest)
        found = sorted((dest / "metadata").glob("*.metadata.json"))
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

"""Metadata aggregates against a table with deletes answer right, and stay cheap (#149).

The pytest twin of `test/native_agg_deletes.sh`, every check by the shell's name.

An ungrouped, unfiltered aggregate over a native table is answered from row group
metadata. A zone map describes every row written into its group, deleted ones
included, so a group with deletes cannot be folded from its zone map; the decision
is made per group. Three things are asserted: the answers match a heap mirror over
four delete patterns; count(*) with one row deleted stays far under a full scan;
and min/max with one dirty group stays far under a read of everything.

THE MIRROR IS WRITTEN FROM THE GENERATOR, and a premise per stage checks it holds
exactly the rows the generator says survive. Without that, a delete that never ran
left every "after ..." arm comparing two untouched tables.

A FAILED READING IS NEVER AGREEMENT. An empty aggregate on both sides used to read
"same", and a failed timing read "yes": with the server gone, the four
differentials passed, and with the measured query failing both timing arms did.

ONE CHECK PER TEST (#1339). One module fixture takes every reading in the order the
shell does, through `_try`, and each test asserts one reading.
"""

import os
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

ROWS = int(os.environ.get("PGC_AGGDEL_ROWS", "400000"))
SCHEMA = "pgc_agg_deletes"
AGGS = ("count(*)", "count(v)", "count(s)", "sum(v)", "avg(v)", "min(v)", "max(v)",
        "min(w)", "max(w)", "min(s)", "max(s)")
NUMBER = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _try(fn, *args, **kw):
    """A reading, or the failure as a query_error(), so a failed step reddens its own
    check rather than erroring the fixture and every check with it (#1522)."""
    try:
        return fn(*args, **kw)
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary or type(exc).__name__)


def _one(conn, sql):
    """-> the first column of the last row as text ('' for NULL), as the shell's q."""
    rows = conn.execute(sql).fetchall()
    if not rows:
        return query_error(f"no row from {sql}")
    v = rows[-1][0]
    return "" if v is None else str(v)


def _read(conn, sql):
    return _try(_one, conn, sql)


def _differential(conn):
    """-> "same", or the aggregates that differ or could not be read, as the shell."""
    bad = ""
    for agg in AGGS:
        cv, hv = _read(conn, f"SELECT {agg} FROM ad_c"), _read(conn, f"SELECT {agg} FROM ad_h")
        if cv == "" or hv == "" or cv.startswith(QUERY_ERROR) or hv.startswith(QUERY_ERROR):
            bad += f" {agg}(no reading: columnar=[{cv}] heap=[{hv}])"
        elif cv != hv:
            bad += f" {agg}(columnar={cv} heap={hv})"
    return bad.strip() or "same"


def _survivors(conn, cond):
    return _read(conn, f"SELECT count(*) FROM generate_series(1, {ROWS}) g WHERE {cond}")


def _ms(conn, sql, setting=None):
    """Best of three, timed inside the server, as the shell's ms()."""
    def run():
        if setting:
            conn.execute(setting)
        try:
            return _one(conn, f"SELECT ad_ms($q${sql}$q$, 3)")
        finally:
            if setting:
                conn.execute("RESET pgcolumnar.enable_vectorization")
    return _try(run)


def _faster(measured, reference, divisor, unit):
    """The shell's awk verdict: a reading that is not a number is no reading, never fast."""
    m, r = str(measured), str(reference)
    shown = f"({m} {unit} {r})"
    if not NUMBER.match(m) or not NUMBER.match(r):
        return f"no reading {shown}"
    return "yes" if float(r) > 0 and float(m) < float(r) / divisor else f"no {shown}"


def _txt(v):
    """A reading for expect.text: a value as text, a failure passed through."""
    return v if isinstance(v, str) else str(v)


@pytest.fixture(scope="module")
def ad(pgc_cluster):
    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    r = {}
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"SET search_path = {SCHEMA}, public")
        gen = f"SELECT g, g % 1000, g * 7, 'r' || g FROM generate_series(1, {ROWS}) g"
        for sql in ("CREATE TABLE ad_c (id int, v int, w bigint, s text) USING pgcolumnar",
                    f"INSERT INTO ad_c {gen}",
                    "CREATE TABLE ad_h (id int, v int, w bigint, s text)",
                    f"INSERT INTO ad_h {gen}"):
            _try(conn.execute, sql)

        # --- 1. the answers are right ----------------------------------------------
        r["mirror"] = (_read(conn, "SELECT count(*) FROM ad_h"), _survivors(conn, "true"))
        r["diff none"] = _differential(conn)

        for t in ("ad_c", "ad_h"):
            _try(conn.execute, f"DELETE FROM {t} WHERE id = {ROWS // 2}")
        r["mirror one"] = (_read(conn, "SELECT count(*) FROM ad_h"),
                           _survivors(conn, f"g <> {ROWS // 2}"))
        r["diff one"] = _differential(conn)

        for t in ("ad_c", "ad_h"):
            _try(conn.execute, f"DELETE FROM {t} WHERE id BETWEEN 1 AND {ROWS // 10}")
        r["mirror tenth"] = (_read(conn, "SELECT count(*) FROM ad_h"),
                             _survivors(conn, f"g <> {ROWS // 2} AND g > {ROWS // 10}"))
        r["diff tenth"] = _differential(conn)

        for t in ("ad_c", "ad_h"):
            _try(conn.execute, f"DELETE FROM {t} WHERE id % 7 = 0")
        r["mirror seventh"] = (_read(conn, "SELECT count(*) FROM ad_h"),
                               _survivors(conn, f"g <> {ROWS // 2} AND g > {ROWS // 10} AND g % 7 <> 0"))
        r["diff seventh"] = _differential(conn)

        # --- 2. count(*) no longer cares about a delete ------------------------------
        for sql in ("CREATE TABLE ad_t (id int, v int) USING pgcolumnar",
                    "SELECT pgcolumnar.set_options('ad_t', stripe_row_limit => 40000)",
                    f"INSERT INTO ad_t SELECT g, g % 1000 FROM generate_series(1, {ROWS}) g"):
            _try(conn.execute, sql)
        r["groups"] = _read(conn, "SELECT count(*) FROM pgcolumnar.row_group r"
                            " JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id"
                            " WHERE s.relation_oid = 'ad_t'::regclass")
        _try(conn.execute, """CREATE OR REPLACE FUNCTION ad_ms(qry text, n int) RETURNS numeric AS $$
            DECLARE t0 timestamptz; best numeric; cur numeric; i int;
            BEGIN
                EXECUTE qry;
                FOR i IN 1..n LOOP
                    t0 := clock_timestamp();
                    EXECUTE qry;
                    cur := extract(epoch FROM clock_timestamp() - t0) * 1000;
                    IF best IS NULL OR cur < best THEN best := cur; END IF;
                END LOOP;
                RETURN round(best, 4);
            END $$ LANGUAGE plpgsql""")
        _ms(conn, "SELECT count(*) FROM ad_t")           # the shell's clean timing, printed only
        _try(conn.execute, f"DELETE FROM ad_t WHERE id = {ROWS // 2}")
        dirty = _ms(conn, "SELECT count(*) FROM ad_t")
        r["timing count"] = (_read(conn, "SELECT count(*) FROM ad_t"),
                             _survivors(conn, f"g <> {ROWS // 2}"))
        scan = _ms(conn, "SELECT count(*) FROM ad_t", "SET pgcolumnar.enable_vectorization = off")
        r["count fast"] = _faster(dirty, scan, 4, "against a")

        # --- 3. only the groups with deletes are read --------------------------------
        mm = _ms(conn, "SELECT min(v), max(v) FROM ad_t")
        full = _ms(conn, "SELECT min(v), max(v) FROM ad_t", "SET pgcolumnar.enable_vectorization = off")
        r["minmax fast"] = _faster(mm, full, 2, "vs")

        plan = _try(lambda: conn.execute("EXPLAIN (COSTS off) SELECT count(*) FROM ad_t").fetchone()[0])
        r["plan"] = ("yes" if isinstance(plan, str) and "Custom Scan (PgColumnarScan)" in plan
                     else f"no ({plan})")
        yield r
    finally:
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        except psycopg.Error:
            pass
        conn.close()


def test_the_heap_mirror_holds_every_generated_row(ad, expect):
    expect.text(_txt(ad["mirror"][0]), _txt(ad["mirror"][1]),
                "premise: the heap mirror holds every generated row")


def test_aggregates_match_heap_with_nothing_deleted(ad, expect):
    expect.text(ad["diff none"], "same", "aggregates match heap with nothing deleted")


def test_the_heap_mirror_lost_exactly_the_one_deleted_row(ad, expect):
    expect.text(_txt(ad["mirror one"][0]), _txt(ad["mirror one"][1]),
                "premise: the heap mirror lost exactly the one deleted row")


def test_aggregates_match_heap_after_one_row_is_deleted(ad, expect):
    expect.text(ad["diff one"], "same", "aggregates match heap after one row is deleted")


def test_the_heap_mirror_lost_exactly_the_contiguous_tenth(ad, expect):
    expect.text(_txt(ad["mirror tenth"][0]), _txt(ad["mirror tenth"][1]),
                "premise: the heap mirror lost exactly the contiguous tenth")


def test_aggregates_match_heap_after_a_contiguous_tenth_is_deleted(ad, expect):
    expect.text(ad["diff tenth"], "same", "aggregates match heap after a contiguous tenth is deleted")


def test_the_heap_mirror_lost_exactly_every_seventh_row_as_well(ad, expect):
    expect.text(_txt(ad["mirror seventh"][0]), _txt(ad["mirror seventh"][1]),
                "premise: the heap mirror lost exactly every seventh row as well")


def test_aggregates_match_heap_after_every_group_is_dirtied(ad, expect):
    expect.text(ad["diff seventh"], "same", "aggregates match heap after every group is dirtied")


def test_the_timing_fixture_has_the_row_groups_its_margin_assumes(ad, expect):
    expect.text(_txt(ad["groups"]), "10", "the timing fixture has the row groups its margin assumes")


def test_the_timing_table_counts_the_row_its_one_delete_removed(ad, expect):
    expect.text(_txt(ad["timing count"][0]), _txt(ad["timing count"][1]),
                "the timing table counts the row its one delete removed")


def test_one_deleted_row_does_not_send_count_back_to_a_full_scan(ad, expect):
    expect.text(ad["count fast"], "yes", "one deleted row does not send count(*) back to a full scan")


def test_a_dirty_group_is_scanned_without_scanning_the_clean_ones(ad, expect):
    expect.text(ad["minmax fast"], "yes", "a dirty group is scanned without scanning the clean ones")


def test_the_metadata_aggregate_path_is_still_chosen_with_a_row_deleted(ad, expect):
    expect.text(ad["plan"], "yes", "the metadata aggregate path is still chosen with a row deleted")

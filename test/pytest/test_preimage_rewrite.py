"""A predicate on date_trunc(unit, ts) is rewritten to a range on ts, so the zone maps
can prune it (#403 item 1). The pytest twin of `test/preimage_rewrite.sh`, every check
by the shell's name (#1341).

The answers cannot see this change: a rewrite that prunes correctly and one that does
not prune return the same rows. So the work checks compare the chunk groups each form
removes with the explicit range it is equivalent to, for equality and for the four
ordered operators (both operand orders, and constants that are not truncated), and the
answer checks are there to catch a rewrite that prunes WRONGLY: against the range and
against heap mirrors, by count and by rows. Near the end of the timestamp range, and on
the infinities, the rewrite must answer rather than raise. The shapes that must decline
(a non-truncated constant under equality, timestamptz, a non-constant unit, an unlisted
unit) must still answer as heap does. The keys stay conservative, so the batch fold
declines them.

THE MIRRORS COME FROM THE GENERATORS, never from the columnar tables: a copy agrees with
whatever the reader returned. And the server must not have lost a backend since setup,
because a fresh connection answers again after crash recovery.

ON A PRIVATE DATABASE, with serial planning, as the shell's cluster runs.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_preimage_rewrite"
DAY = "timestamp '2024-02-01'"
NEXT = "timestamp '2024-02-02'"
HALF = "timestamp '2024-02-01 12:00'"
MAXY = "timestamp '294276-01-01'"
GEN_C = ("SELECT timestamp '2024-01-01' + (g / 10000) * interval '1 day'"
         " + (g % 10000) * interval '1 second', g FROM generate_series(1,500000) g")
GEN_EDGE = "SELECT timestamp '2024-01-01' + g * interval '1 day', g FROM generate_series(1,1000) g"
GEN_TZ = ("SELECT timestamptz '2024-01-01+00' + (g / 10000) * interval '1 day'"
          " + (g % 10000) * interval '1 second', g FROM generate_series(1,200000) g")

ORD_ARMS = [
    ("ts_trunc >= c", f"date_trunc('day', ts) >= {DAY}", f"ts >= {DAY}"),
    ("ts_trunc > c", f"date_trunc('day', ts) > {DAY}", f"ts >= {NEXT}"),
    ("ts_trunc < c", f"date_trunc('day', ts) < {DAY}", f"ts < {DAY}"),
    ("ts_trunc <= c", f"date_trunc('day', ts) <= {DAY}", f"ts < {NEXT}"),
    ("c < ts_trunc (reversed)", f"{DAY} < date_trunc('day', ts)", f"ts >= {NEXT}"),
    ("c >= ts_trunc (reversed)", f"{DAY} >= date_trunc('day', ts)", f"ts < {NEXT}"),
    ("ts_trunc >= c, c not truncated", f"date_trunc('day', ts) >= {HALF}", f"ts >= {NEXT}"),
    ("ts_trunc < c, c not truncated", f"date_trunc('day', ts) < {HALF}", f"ts < {NEXT}"),
]
EDGE_ARMS = [
    ("year = max", f"date_trunc('year', ts) = {MAXY}"),
    ("year >= max", f"date_trunc('year', ts) >= {MAXY}"),
    ("year > max", f"date_trunc('year', ts) > {MAXY}"),
    ("year <= max", f"date_trunc('year', ts) <= {MAXY}"),
    ("year < max", f"date_trunc('year', ts) < {MAXY}"),
    ("day >= infinity", "date_trunc('day', ts) >= timestamp 'infinity'"),
    ("day < infinity", "date_trunc('day', ts) < timestamp 'infinity'"),
    ("day = infinity", "date_trunc('day', ts) = timestamp 'infinity'"),
    ("day = -infinity", "date_trunc('day', ts) = timestamp '-infinity'"),
    ("hour = infinity", "date_trunc('hour', ts) = timestamp 'infinity'"),
    ("year = -infinity", "date_trunc('year', ts) = timestamp '-infinity'"),
]
UNITS = [
    ("month", "timestamp '2024-02-01'"),
    ("week", "date_trunc('week', timestamp '2024-02-01')"),
    ("quarter", "date_trunc('quarter', timestamp '2024-02-01')"),
    ("year", "timestamp '2024-01-01'"),
]


def _one(dsn, sql, settings=()):
    """-> the first column of the first row as text, or query_error() carrying the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET max_parallel_workers_per_gather = 0")
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])
    return "" if row is None or row[0] is None else str(row[0])


def _rows(dsn, sql):
    """-> the column as one text, row per line, or query_error() carrying the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET max_parallel_workers_per_gather = 0")
            return "\n".join(str(r[0]) for r in conn.execute(sql))
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _plan(dsn, sql, settings=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET max_parallel_workers_per_gather = 0")
            for s in settings:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute(
                f"EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF) {sql}"))
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _removed(dsn, sql):
    """-> chunk groups removed by filter, as an int, or a reading that says why not."""
    plan = _plan(dsn, sql)
    m = re.search(r"Columnar Chunk Groups Removed by Filter: (\d+)", plan)
    return int(m.group(1)) if m else f"no removed-groups line ({plan.splitlines()[0] if plan else 'empty plan'})"


def _int(v):
    return int(v) if isinstance(v, str) and re.fullmatch(r"-?\d+", v) else v


def _prunes(v):
    """1 when a removed-groups reading is a positive count, else 0, as the shell's test."""
    return 1 if isinstance(v, int) and v > 0 else 0


def _crashes(cluster):
    """-> backends killed by a signal, from the server log, or a reading that says why not."""
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return sum(1 for line in text.splitlines() if "terminated by signal" in line)


@pytest.fixture(scope="module")
def pr(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE pre_c(ts timestamp, v int) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('pre_c', stripe_row_limit => 10000)")
            conn.execute(f"INSERT INTO pre_c {GEN_C}")
            conn.execute("INSERT INTO pre_c VALUES (NULL, -1), ('infinity', -2), ('-infinity', -3)")
            conn.execute("CREATE TABLE pre_h(ts timestamp, v int)")
            conn.execute(f"INSERT INTO pre_h {GEN_C}")
            conn.execute("INSERT INTO pre_h VALUES (NULL, -1), ('infinity', -2), ('-infinity', -3)")
            conn.execute("ANALYZE pre_c")
            conn.execute("ANALYZE pre_h")
            conn.execute("CREATE TABLE pre_edge(ts timestamp, v int) USING pgcolumnar")
            conn.execute(f"INSERT INTO pre_edge {GEN_EDGE}")
            conn.execute("INSERT INTO pre_edge VALUES ('294276-12-31 23:00', -1), ('infinity', -2), ('-infinity', -3)")
            conn.execute("CREATE TABLE pre_edgeh(ts timestamp, v int)")
            conn.execute(f"INSERT INTO pre_edgeh {GEN_EDGE}")
            conn.execute("INSERT INTO pre_edgeh VALUES ('294276-12-31 23:00', -1), ('infinity', -2), ('-infinity', -3)")
            conn.execute("CREATE TABLE pre_tz(ts timestamptz, v int) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('pre_tz', stripe_row_limit => 10000)")
            conn.execute(f"INSERT INTO pre_tz {GEN_TZ}")
            conn.execute("CREATE TABLE pre_tzh(ts timestamptz, v int)")
            conn.execute(f"INSERT INTO pre_tzh {GEN_TZ}")
        crashes_before = _crashes(pgc_cluster)

        r = {"groups": _one(dsn, "SELECT (count(*) > 10)::int FROM pgcolumnar.row_group "
                                 "WHERE storage_id = pgcolumnar.get_storage_id('pre_c'::regclass)")}
        rng = f"SELECT count(*) FROM pre_c WHERE ts >= {DAY} AND ts < {DAY} + interval '1 day'"
        trunc = f"SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = {DAY}"
        r["ref"] = _removed(dsn, rng)
        r["got"] = _removed(dsn, trunc)
        r["trunc"] = _one(dsn, trunc)
        r["range"] = _one(dsn, rng)
        r["heap trunc"] = _one(dsn, f"SELECT count(*) FROM pre_h WHERE date_trunc('day', ts) = {DAY}")
        r["rows"] = (_rows(dsn, f"SELECT v FROM pre_c WHERE date_trunc('day', ts) = {DAY} ORDER BY v"),
                     _rows(dsn, f"SELECT v FROM pre_h WHERE date_trunc('day', ts) = {DAY} ORDER BY v"))

        for label, tp, rp in ORD_ARMS:
            r[("ord", label)] = {
                "r": _removed(dsn, f"SELECT count(*) FROM pre_c WHERE {rp}"),
                "g": _removed(dsn, f"SELECT count(*) FROM pre_c WHERE {tp}"),
                "col": _one(dsn, f"SELECT count(*) FROM pre_c WHERE {tp}"),
                "range": _one(dsn, f"SELECT count(*) FROM pre_c WHERE {rp}"),
                "heap": _one(dsn, f"SELECT count(*) FROM pre_h WHERE {tp}"),
                "rows": (_rows(dsn, f"SELECT v FROM pre_c WHERE {tp} ORDER BY v"),
                         _rows(dsn, f"SELECT v FROM pre_h WHERE {tp} ORDER BY v")),
            }

        r["near max"] = _one(dsn, "SELECT count(*) FROM pre_edgeh WHERE ts > timestamp '294276-01-01'")
        for label, p in EDGE_ARMS:
            r[("edge", label)] = (_one(dsn, f"SELECT count(*) FROM pre_edge WHERE {p}"),
                                  _one(dsn, f"SELECT count(*) FROM pre_edgeh WHERE {p}"))
        r["+inf"] = _one(dsn, "SELECT count(*) FROM pre_edgeh WHERE ts = timestamp 'infinity'")
        r["-inf"] = _one(dsn, "SELECT count(*) FROM pre_edgeh WHERE ts = timestamp '-infinity'")

        r["notrunc"] = _one(dsn, "SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = timestamp '2024-02-01 12:00'")
        r["notrunc heap"] = _one(dsn, "SELECT count(*) FROM pre_h WHERE date_trunc('day', ts) = timestamp '2024-02-01 12:00'")
        utc = ("SET TimeZone='UTC'",)
        tokyo = ("SET TimeZone='Asia/Tokyo'",)
        r["tz utc"] = (_one(dsn, "SELECT count(*) FROM pre_tz WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+00'", utc),
                       _one(dsn, "SELECT count(*) FROM pre_tzh WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+00'", utc))
        r["tz tokyo"] = (_one(dsn, "SELECT count(*) FROM pre_tz WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+09'", tokyo),
                         _one(dsn, "SELECT count(*) FROM pre_tzh WHERE date_trunc('day', ts) = timestamptz '2024-02-01 00:00+09'", tokyo))
        r["nonconst"] = (_one(dsn, f"SELECT count(*) FROM pre_c WHERE date_trunc((SELECT 'day'), ts) = {DAY}"),
                         _one(dsn, f"SELECT count(*) FROM pre_h WHERE date_trunc((SELECT 'day'), ts) = {DAY}"))

        fold = ("SET pgcolumnar.enable_ungrouped_vector_agg=on",)
        fplan = _plan(dsn, f"SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = {DAY}", fold)
        m = re.search(r"Columnar Batch Fold: (yes|no)", fplan)
        r["fold"] = m.group(1) if m else ""
        r["fold answer"] = (_one(dsn, f"SELECT count(*) FROM pre_c WHERE date_trunc('day', ts) = {DAY}", fold),
                            _one(dsn, f"SELECT count(*) FROM pre_h WHERE date_trunc('day', ts) = {DAY}"))

        for u, c in UNITS:
            r[("unit", u)] = (_one(dsn, f"SELECT count(*) FROM pre_c WHERE date_trunc('{u}', ts) = {c}"),
                              _one(dsn, f"SELECT count(*) FROM pre_h WHERE date_trunc('{u}', ts) = {c}"))
        mil = "date_trunc('millennium', ts) = date_trunc('millennium', timestamp '2024-02-01')"
        r["millennium"] = (_one(dsn, f"SELECT count(*) FROM pre_c WHERE {mil}"),
                           _one(dsn, f"SELECT count(*) FROM pre_h WHERE {mil}"))

        up = _one(dsn, "SELECT 1")
        now = _crashes(pgc_cluster)
        if not (isinstance(crashes_before, int) and isinstance(now, int)):
            r["survived"] = "no (crash count unreadable)"
        elif up == "1" and now == crashes_before:
            r["survived"] = "1"
        else:
            r["survived"] = f"no ([{up}], {now - crashes_before} backend crashes)"
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_fixture_spans_many_chunk_groups(pr, expect):
    expect.num(_int(pr["groups"]), 1, "premise: the fixture spans many chunk groups")


def test_the_explicit_range_prunes(pr, expect):
    expect.num(_prunes(pr["ref"]), 1, "the explicit range prunes, so there IS something to remove")


def test_date_trunc_equality_prunes(pr, expect):
    expect.num(_prunes(pr["got"]), 1, "date_trunc('day', ts) = const prunes chunk groups (#403)")


def test_date_trunc_equality_prunes_as_well_as_the_range(pr, expect):
    expect.num(pr["got"], pr["ref"], "...and prunes as well as the equivalent explicit range")


def test_the_rewritten_predicate_returns_the_ranges_answer(pr, expect):
    expect.num(_int(pr["trunc"]), _int(pr["range"]), "the rewritten predicate returns the range's answer")


def test_the_rewritten_predicate_returns_the_heaps_answer(pr, expect):
    expect.num(_int(pr["trunc"]), _int(pr["heap trunc"]), "and the heap mirror's answer")


def test_the_rewritten_predicate_returns_the_heaps_rows(pr, expect):
    col, heap = pr["rows"]
    expect.text(col, heap, "the actual ROWS match the heap, not just the count")


@pytest.mark.parametrize("label", [a[0] for a in ORD_ARMS])
def test_the_range_for_an_ordered_arm_prunes(pr, expect, label):
    expect.num(_prunes(pr[("ord", label)]["r"]), 1,
               f"the range for [{label}] prunes, so there is something to remove")


@pytest.mark.parametrize("label", [a[0] for a in ORD_ARMS])
def test_an_ordered_arm_prunes(pr, expect, label):
    expect.num(_prunes(pr[("ord", label)]["g"]), 1, f"[{label}] prunes chunk groups")


@pytest.mark.parametrize("label", [a[0] for a in ORD_ARMS])
def test_an_ordered_arm_prunes_as_well_as_the_range(pr, expect, label):
    a = pr[("ord", label)]
    expect.num(a["g"], a["r"], f"[{label}] prunes as well as the equivalent explicit range")


@pytest.mark.parametrize("label", [a[0] for a in ORD_ARMS])
def test_an_ordered_arm_returns_the_ranges_answer(pr, expect, label):
    a = pr[("ord", label)]
    expect.num(_int(a["col"]), _int(a["range"]), f"[{label}] returns the range's answer")


@pytest.mark.parametrize("label", [a[0] for a in ORD_ARMS])
def test_an_ordered_arm_returns_the_heaps_answer(pr, expect, label):
    a = pr[("ord", label)]
    expect.num(_int(a["col"]), _int(a["heap"]), f"[{label}] returns the heap's answer")


@pytest.mark.parametrize("label", [a[0] for a in ORD_ARMS])
def test_an_ordered_arm_returns_the_heaps_rows(pr, expect, label):
    col, heap = pr[("ord", label)]["rows"]
    expect.text(col, heap, f"[{label}] returns the heap's ROWS, not just its count")


def test_the_near_maximum_row_is_there(pr, expect):
    expect.num(_int(pr["near max"]), 2, "premise: the near-maximum row is really there")


@pytest.mark.parametrize("label", [a[0] for a in EDGE_ARMS])
def test_an_edge_arm_answers_as_heap_does(pr, expect, label):
    col, heap = pr[("edge", label)]
    expect.num(_int(col), _int(heap), f"[{label}] answers instead of raising, and agrees with the heap")


def test_the_fixture_holds_one_positive_infinity(pr, expect):
    expect.num(_int(pr["+inf"]), 1, "premise: the fixture holds one +infinity row")


def test_the_fixture_holds_one_negative_infinity(pr, expect):
    expect.num(_int(pr["-inf"]), 1, "premise: the fixture holds one -infinity row")


def test_a_non_truncated_constant_returns_no_rows(pr, expect):
    expect.num(_int(pr["notrunc"]), 0, "a non-truncated constant returns no rows")


def test_the_heap_agrees_it_is_unsatisfiable(pr, expect):
    expect.num(_int(pr["notrunc heap"]), 0, "...and the heap agrees it is unsatisfiable")


def test_timestamptz_is_correct_under_utc(pr, expect):
    col, heap = pr["tz utc"]
    expect.num(_int(col), _int(heap), "timestamptz: correct under UTC")


def test_timestamptz_is_correct_under_another_zone(pr, expect):
    col, heap = pr["tz tokyo"]
    expect.num(_int(col), _int(heap), "timestamptz: and still correct under a different zone")


def test_a_non_constant_unit_answers_correctly(pr, expect):
    col, heap = pr["nonconst"]
    expect.num(_int(col), _int(heap), "a non-constant unit still answers correctly")


def test_the_rewritten_predicate_does_not_engage_the_batch_fold(pr, expect):
    expect.text(pr["fold"], "no", "the rewritten predicate does not engage the batch fold")


def test_the_rewritten_predicate_answers_with_the_fold_enabled(pr, expect):
    col, heap = pr["fold answer"]
    expect.num(_int(col), _int(heap), "...and still answers correctly with the fold enabled")


@pytest.mark.parametrize("unit", [u[0] for u in UNITS])
def test_a_unit_returns_the_heaps_answer(pr, expect, unit):
    col, heap = pr[("unit", unit)]
    expect.num(_int(col), _int(heap), f"unit '{unit}' returns the heap's answer")


@pytest.mark.parametrize("unit", [u[0] for u in UNITS])
def test_a_unit_matches_some_rows(pr, expect, unit):
    _col, heap = pr[("unit", unit)]
    expect.num(1 if isinstance(_int(heap), int) and _int(heap) > 0 else 0, 1,
               f"unit '{unit}': premise: it matches some rows")


def test_an_unlisted_unit_answers_correctly(pr, expect):
    col, heap = pr["millennium"]
    expect.num(_int(col), _int(heap), "an unlisted unit (millennium) still answers correctly")


def test_the_backend_is_still_up(pr, expect):
    expect.text(pr["survived"], "1", "backend still up")

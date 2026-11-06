"""The planner's zone-map sample is one-based, complete, and per-column. The pytest twin
of `test/zonemap_estimate_sample.sh`, every check by the shell's name (#1341).

pgcolumnar_zonemap_survival prices a restricted columnar scan by sampling row groups
and asking the reader's own skip predicates how many survive. The stride used to run
over [0, ngroups), and group numbers start at 1, so the first probe was spent on a group
that cannot exist and the newest group was never probed: the same predicate was priced
by where its groups sat. And each probe fetched every column's zone-map rows to read
one column's, so the estimator's reads scaled with table width.

The instrument is pg_stat_all_tables.idx_tup_fetch for pgcolumnar.zone_map, split into
planning (EXPLAIN, which runs the estimator and no executor) and execution. Each reading
resets the counters, runs its statement on a connection of its own, and forces that
connection's statistics out with pg_stat_force_next_flush() before another connection
reads them. The estimator's own session is read from its DEBUG1 report.

ON A PRIVATE DATABASE, because pg_stat_reset() resets the whole database's counters,
built once by a module fixture that takes every reading in the shell's order; each test
asserts one, so one red cannot stop the next (#1339). A failed query is a
`query_error()`, refused on either side by name.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_zonemap_estimate_sample"
ESTIMATE = re.compile(r"zone map estimate: probes=([0-9]+) opens=([0-9]+)")
ROWS = re.compile(r"PgColumnarScan.*rows=([0-9]*)")


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rows(dsn, *sqls):
    """-> the last statement's rows, first column as text, on a connection of its own,
    or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in sqls:
                cur = conn.execute(sql)
            return ["" if r[0] is None else str(r[0]) for r in cur]
    except psycopg.Error as exc:
        return _failed(exc)


def _value(dsn, *sqls):
    rows = _rows(dsn, *sqls)
    return rows if isinstance(rows, str) else (rows[0] if rows else "")


def _zm(dsn, sql):
    """-> zone_map index tuples fetched by ONE statement, or the failed reading."""
    reset = _value(dsn, "SELECT pg_stat_reset()")
    if reset != "":
        return reset
    flushed = _value(dsn, sql, "SELECT pg_stat_force_next_flush()")
    if flushed != "":
        return flushed
    got = _value(dsn, "SELECT coalesce(idx_tup_fetch, 0) FROM pg_stat_all_tables "
                      "WHERE relname = 'zone_map' AND schemaname = 'pgcolumnar'")
    return int(got) if got.isdigit() else got


def _cost(dsn, table, where, *settings):
    """-> the Custom Scan's total cost on `table`, "" when the plan has none, or the
    failed reading. `settings` run first, in the same session."""
    plan = _rows(dsn, *settings, f"EXPLAIN SELECT count(*) FROM {table} WHERE {where}")
    if isinstance(plan, str):
        return plan
    line = re.compile(r"Custom Scan \(PgColumnarScan\) on " + table +
                      r"  \(cost=[0-9.]*\.\.([0-9.]*) ")
    return "\n".join(m.group(1) for m in map(line.search, plan) if m)


def _scanrows(dsn, where):
    plan = _rows(dsn, f"EXPLAIN SELECT count(*) FROM w WHERE {where}")
    if isinstance(plan, str):
        return plan
    return "\n".join(m.group(1) for m in map(ROWS.search, plan) if m)


def _estimate(dsn):
    """-> (probes, opens) from the last estimator report of one EXPLAIN, or ("", "")."""
    reports = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: reports.append(d.message_primary or ""))
            conn.execute("SET client_min_messages = debug1")
            conn.execute("EXPLAIN SELECT count(*) FROM w WHERE c1 >= 0").fetchall()
    except psycopg.Error as exc:
        return _failed(exc), _failed(exc)
    found = [m for m in map(ESTIMATE.search, reports) if m]
    return (found[-1].group(1), found[-1].group(2)) if found else ("", "")


@pytest.fixture(scope="module")
def zes(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        wcols = ", ".join(f"c{i} int" for i in range(1, 31))
        wvals = ", ".join("g" for _ in range(1, 31))
        with psycopg.connect(dsn, autocommit=True) as c:
            for sql in ("CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                        f"CREATE TABLE w ({wcols}) USING pgcolumnar",
                        "CREATE TABLE n (c1 int, c2 int) USING pgcolumnar",
                        "SELECT pgcolumnar.set_options('w'::regclass, stripe_row_limit => 2000)",
                        "SELECT pgcolumnar.set_options('n'::regclass, stripe_row_limit => 2000)",
                        f"INSERT INTO w SELECT {wvals} FROM generate_series(1, 20000) g",
                        "INSERT INTO n SELECT g, g FROM generate_series(1, 20000) g"):
                try:
                    c.execute(sql)
                except psycopg.Error as exc:
                    print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")

        def sid(t):
            return _value(dsn, f"SELECT pgcolumnar.get_storage_id('{t}'::regclass)")

        r = {"w rows": _value(dsn, "SELECT count(*) FROM w"),
             "n rows": _value(dsn, "SELECT count(*) FROM n"),
             "w option": _value(dsn, "SELECT stripe_row_limit FROM pgcolumnar.options "
                                     "WHERE regclass = 'w'::regclass"),
             "w groups": _value(dsn, "SELECT count(DISTINCT group_number) || '/' || "
                                     "min(group_number) || '/' || max(group_number) "
                                     f"FROM pgcolumnar.zone_map WHERE storage_id = {sid('w')}"),
             "whole chunk first": _value(dsn, "SELECT min(vector_index) FROM pgcolumnar.zone_map "
                                              f"WHERE storage_id = {sid('w')} AND group_number = 1 "
                                              "AND column_index = 0")}
        wp = _zm(dsn, "EXPLAIN SELECT count(*) FROM w WHERE c1 >= 0")
        wf = _zm(dsn, "SELECT count(*) FROM w WHERE c1 >= 0")
        np_ = _zm(dsn, "EXPLAIN SELECT count(*) FROM n WHERE c1 >= 0")
        nf = _zm(dsn, "SELECT count(*) FROM n WHERE c1 >= 0")
        print(f"-- zone_map idx_tup_fetch, w (30 col): planning={wp} full={wf}")
        print(f"-- zone_map idx_tup_fetch, n ( 2 col): planning={np_} full={nf}")
        # An executor reading is the difference of two counts, so either one failing
        # fails it: carrying only the full count read 40 = 40 with planning failed.
        r.update({"wp": wp, "np": np_,
                  "w exec": wp if isinstance(wp, str) else wf if isinstance(wf, str) else wf - wp,
                  "n exec": np_ if isinstance(np_, str) else nf if isinstance(nf, str) else nf - np_})
        r["probes"], r["opens"] = _estimate(dsn)
        print(f"-- the planner's zone-map session: probes={r['probes']} opens={r['opens']}")

        for k, where in (("c1 > 17000", "c1 > 17000"), ("c1 <= 4000", "c1 <= 4000"),
                         ("c1 > 8000", "c1 > 8000"), ("c1 <= 12000", "c1 <= 12000")):
            r[f"count {k}"] = _value(dsn, f"SELECT count(*) FROM w WHERE {where}")
        r["hi"], r["lo"] = _cost(dsn, "w", "c1 > 8000"), _cost(dsn, "w", "c1 <= 12000")
        r["top"], r["bot"] = _cost(dsn, "w", "c1 > 17000"), _cost(dsn, "w", "c1 <= 4000")
        print(f"-- Custom Scan total cost, six of ten groups: newest={r['hi']} oldest={r['lo']}")
        print(f"-- Custom Scan total cost, two of ten groups: newest={r['top']} oldest={r['bot']}")
        r["mirror rows"] = (_scanrows(dsn, "c1 > 8000"), _scanrows(dsn, "c1 <= 12000"))
        r["narrow rows"] = (_scanrows(dsn, "c1 > 17000"), _scanrows(dsn, "c1 <= 4000"))
        r["none"], r["last"] = _cost(dsn, "w", "c1 > 99999"), _cost(dsn, "w", "c1 > 19000")
        r["count last"] = _value(dsn, "SELECT count(*) FROM w WHERE c1 > 19000")
        r["count none"] = _value(dsn, "SELECT count(*) FROM w WHERE c1 > 99999")

        r["gsess"] = _value(dsn, "CREATE TABLE gsess (c1 int, c2 int) USING pgcolumnar",
                            "SET pgcolumnar.stripe_row_limit = 2000",
                            "INSERT INTO gsess SELECT g, g FROM generate_series(1, 20000) g",
                            "SELECT 'loaded'")
        r["gsess option"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = "
                                        "'gsess'::regclass AND stripe_row_limit IS NOT NULL")
        r["gsess groups"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group "
                                        f"WHERE storage_id = {sid('gsess')}")
        serial = "SET max_parallel_workers_per_gather = 0"
        r["def"] = _cost(dsn, "gsess", "c1 <= 4000", serial, "SET pgcolumnar.stripe_row_limit = 150000")
        r["wrt"] = _cost(dsn, "gsess", "c1 <= 4000", serial, "SET pgcolumnar.stripe_row_limit = 2000")
        print(f"-- gsess cost, planned at the default GUC={r['def']} ; at the written 2000={r['wrt']}")
        r["discounted"] = _cost(dsn, "gsess", "c1 <= 4000", serial)
        r["full"] = _cost(dsn, "gsess", "c1 >= 0", serial)
        r["gsess count"] = _value(dsn, "SELECT count(*) FROM gsess WHERE c1 <= 4000")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- premises: the fixture is the shape every number below assumes ----------

def test_w_loaded_20000_rows(zes, expect):
    expect.text(zes["w rows"], "20000", "w loaded 20000 rows")


def test_n_loaded_20000_rows(zes, expect):
    expect.text(zes["n rows"], "20000", "n loaded 20000 rows")


def test_w_carries_the_per_table_stripe_row_limit(zes, expect):
    expect.text(zes["w option"], "2000", "w carries the per-table stripe_row_limit, not the session GUC")


def test_w_has_10_row_groups_numbered_1_to_10(zes, expect):
    expect.text(zes["w groups"], "10/1/10", "w has 10 row groups numbered 1..10, so there is no group 0")


def test_the_whole_chunk_row_sorts_first(zes, expect):
    expect.text(zes["whole chunk first"], "-1",
                "the whole-chunk row sorts first, so one fetch ends a per-column probe")


# ---- the sample: complete, and one column wide ------------------------------

def test_planning_probes_the_zone_map_at_all(zes, expect):
    wp = zes["wp"]
    expect.text(wp if isinstance(wp, str) else ("yes" if wp > 0 else "no"), "yes",
                "planning probes the zone map at all")


def test_census_planning_reads_one_column_across_all_10_groups_w(zes, expect):
    expect.num(zes["wp"], 10, "CENSUS: planning reads one column across all 10 groups (w)")


def test_census_planning_reads_one_column_across_all_10_groups_n(zes, expect):
    expect.num(zes["np"], 10, "CENSUS: planning reads one column across all 10 groups (n)")


def test_width_the_planners_reads_do_not_scale_with_table_width(zes, expect):
    expect.num(zes["wp"], zes["np"], "WIDTH: the planner's zone-map reads do not scale with table width")


def test_the_executors_reads_stay_width_independent_too(zes, expect):
    expect.num(zes["w exec"], zes["n exec"], "the executor's reads stay width-independent too")


# ---- the estimator's own session -------------------------------------------

def test_the_estimator_reports_a_session_of_its_own(zes, expect):
    p = zes["probes"]
    expect.text(p if p.startswith("QUERY_ERROR") else ("yes" if p else "no"), "yes",
                "the estimator reports a session of its own, distinct from a scan's")


def test_it_probes_once_per_group_across_all_10_groups(zes, expect):
    expect.text(zes["probes"], "10", "it probes once per group across all 10 groups")


def test_it_opens_zone_map_once_for_the_whole_sample(zes, expect):
    expect.text(zes["opens"], "1", "and opens zone_map once for the whole sample, not once per probe")


# ---- the consequence: position must not change the price --------------------

@pytest.mark.parametrize("where, want, name", [
    ("c1 > 17000", "3000", "c1 > 17000 matches 3000 rows"),
    ("c1 <= 4000", "4000", "c1 <= 4000 matches 4000 rows"),
    ("c1 > 8000", "12000", "c1 > 8000 matches 12000 rows"),
    ("c1 <= 12000", "12000", "c1 <= 12000 matches 12000 rows"),
])
def test_a_predicate_matches_its_rows(zes, where, want, name, expect):
    expect.text(zes[f"count {where}"], want, name)


def test_a_cost_was_extracted(zes, expect):
    costs = [zes[k] for k in ("hi", "lo", "top", "bot")]
    failed = [c for c in costs if c.startswith("QUERY_ERROR")]
    expect.text(failed[0] if failed else ("yes" if all(costs) else "no"), "yes",
                "a cost was extracted, so the checks below are not comparing two blanks")


def test_the_mirror_pair_shares_a_row_estimate(zes, expect):
    newest, oldest = zes["mirror rows"]
    expect.text(newest, oldest, "the mirror pair shares a row estimate, so only survival differs")


def test_the_narrow_pair_shares_a_row_estimate(zes, expect):
    newest, oldest = zes["narrow rows"]
    expect.text(newest, oldest, "the narrow pair shares a row estimate, so only survival differs")


def test_mirror_the_six_newest_groups_cost_what_the_six_oldest_cost(zes, expect):
    expect.text(zes["hi"], zes["lo"], "MIRROR: the six newest groups cost what the six oldest cost")


def test_narrow_the_two_newest_groups_cost_what_the_two_oldest_cost(zes, expect):
    expect.text(zes["top"], zes["bot"], "NARROW: the two newest groups cost what the two oldest cost")


def test_a_predicate_matching_nothing_is_floored(zes, expect):
    none = zes["none"]
    expect.text(none if none.startswith("QUERY_ERROR") else
                ("yes" if (_num(none) or 0) >= 1 else f"no ({none or 'no cost'})"), "yes",
                "a predicate matching nothing is floored, not priced at zero")


def test_matching_only_the_last_group_costs_the_one_group_share(zes, expect):
    expect.text(zes["last"], zes["none"], "matching only the last group costs the same one-group share")


def test_a_pruning_predicate_still_returns_the_right_rows(zes, expect):
    expect.text(zes["count last"], "1000", "a pruning predicate still returns the right rows")


def test_a_predicate_matching_nothing_still_returns_no_rows(zes, expect):
    expect.text(zes["count none"], "0", "a predicate matching nothing still returns no rows")


# ---- the estimate must not move with the planning session's GUC (#817) ------

def test_gsess_carries_no_per_table_option(zes, expect):
    expect.text(zes["gsess option"], "0",
                "PREMISE: gsess carries NO per-table option, so the GUC is the only other input")


def test_gsess_was_written_at_2000(zes, expect):
    expect.text(zes["gsess groups"], "10", "PREMISE: and it really was written at 2000, so it holds 10 groups")


def test_a_cost_was_extracted_from_both_arms(zes, expect):
    d, w = zes["def"], zes["wrt"]
    failed = [c for c in (d, w) if c.startswith("QUERY_ERROR")]
    expect.text(failed[0] if failed else ("yes" if d and w else f"def={d} wrt={w}"), "yes",
                "a cost was extracted from both arms")


def test_the_estimate_does_not_move_with_the_planning_sessions_guc(zes, expect):
    expect.text(zes["def"], zes["wrt"], "the estimate does not move with the planning session's GUC (#817)")


def test_the_discount_is_real(zes, expect):
    d, f = zes["discounted"], zes["full"]
    failed = [c for c in (d, f) if c.startswith("QUERY_ERROR")]
    below = _num(d) is not None and _num(f) is not None and _num(d) < _num(f)
    expect.text(failed[0] if failed else ("yes" if below else f"no ({d} vs full {f})"), "yes",
                "and the discount is real, not just stable (2 of 10 groups priced below a full scan)")


def test_gsess_reads_back_correctly(zes, expect):
    expect.text(zes["gsess count"], "4000", "gsess reads back correctly")

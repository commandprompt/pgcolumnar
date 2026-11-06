"""The parallel-scan numbers in docs/limitations.md rest on the narrow query reading EVERY
chunk group. The pytest twin of `test/doc_parallel_premise.sh`, every check by the
shell's name (#1341).

The filter column is extracted from the page itself, as the shell extracts it, so the
page cannot drift from the query being checked. On the page's fixture shape, the
documented query must read every chunk group the catalog holds, filtering the ordered
column instead must prune, the documented column must not be the ordered one, and it
must cost more. Nothing reads a clock.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). Every plan is read with max_parallel_workers_per_gather =
0. Where the shell stops -- no filter column on the page, so nothing after it runs; the
page naming the ordered column, so the cost block does not -- those tests decline
unnamed, because the shell records nothing for them.
"""
import pathlib
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_doc_parallel_premise"
ROWS = 200000
DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "limitations.md"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _number(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _extract():
    """-> (query, column) from the page, by the shell's own two seds."""
    try:
        text = DOC.read_text()
    except OSError:
        return "", ""
    line = next((l for l in text.splitlines() if "The narrow query is" in l), "")
    m = re.match(r".*`(SELECT[^`]*)`", line)
    query = m.group(1) if m else ""
    m = re.match(r".*WHERE\s*([a-zA-Z_][a-zA-Z0-9_]*)", query)
    return query, (m.group(1) if m else "")


def _plan(dsn, col, analyze):
    """-> the plan lines for a 25% scan on COL, or a failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            p25 = conn.execute(f"SELECT percentile_disc(0.25) WITHIN GROUP (ORDER BY {col}) FROM c753").fetchone()[0]
            conn.execute("SET max_parallel_workers_per_gather = 0")
            opts = "(ANALYZE, TIMING OFF) " if analyze else ""
            return [r[0] for r in conn.execute(f"EXPLAIN {opts}SELECT sel, a, b FROM c753 WHERE {col} <= {p25}")]
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _counter(lines, label):
    if _failed(lines):
        return ""
    for line in lines:
        m = re.search(rf"Chunk Groups {label}: (\d+)", line)
        if m:
            return int(m.group(1))
    return ""


def _scan_cost(lines):
    if _failed(lines):
        return ""
    for line in lines:
        if "PgColumnarScan" in line:
            m = re.search(r"cost=[0-9.]+\.\.([0-9.]+)", line)
            if m:
                return float(m.group(1))
    return ""


@pytest.fixture(scope="module")
def dp(pgc_cluster):
    query, col = _extract()
    r = {"query": query, "col": col}
    if not col:
        yield r
        return
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
                conn.execute("SET pgcolumnar.stripe_row_limit = 20000")
                conn.execute("SET pgcolumnar.chunk_group_row_limit = 10000")
                conn.execute("CREATE TABLE c753 (sel int4, a int4, b int4) USING pgcolumnar")
                conn.execute(f"INSERT INTO c753 SELECT g, hashint4(g), hashint4(g+1) FROM generate_series(1, {ROWS}) g")
                conn.execute("ANALYZE c753")
        except psycopg.Error:
            pass
        r["groups"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s "
                                  "USING (storage_id) WHERE s.relation_oid = 'c753'::regclass")
        for key, c in (("doc", col), ("sel", "sel")):
            plain = _plan(dsn, c, analyze=False)
            r[f"{key} scans"] = plain if _failed(plain) else sum(1 for l in plain if "PgColumnarScan" in l)
            r[f"{key} cost"] = _scan_cost(plain)
            analyzed = _plan(dsn, c, analyze=True)
            r[f"{key} read"], r[f"{key} tot"] = _counter(analyzed, "Read"), _counter(analyzed, "Total")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _stopped(dp, expect):
    """True where the shell stops: no filter column on the page. Declined unnamed."""
    if dp["col"]:
        return False
    expect.cannot_run("UNMET_PRECONDITION", "the page names no narrow query with a filter column, "
                                          "so the shell stops before its fixture")
    return True


def test_the_page_still_names_a_narrow_query(dp, expect):
    expect.text("yes" if dp["query"] else "no", "yes", "the page still names a narrow query")


def test_that_query_has_an_extractable_filter_column(dp, expect):
    expect.text("yes" if dp["col"] else "no", "yes", "and that query has an extractable filter column")


def test_the_fixture_holds_several_row_groups(dp, expect):
    if _stopped(dp, expect):
        return
    n = dp["groups"]
    expect.text(("many" if n >= 5 else str(n)) if _number(n) else n, "many",
                "the fixture holds several row groups, so pruning can be seen")


def test_the_documented_columns_plan_is_one_columnar_scan(dp, expect):
    if _stopped(dp, expect):
        return
    expect.text(str(dp["doc scans"]), "1", "the documented column's plan is exactly one columnar scan node")


def test_the_sel_plan_is_one_columnar_scan(dp, expect):
    if _stopped(dp, expect):
        return
    expect.text(str(dp["sel scans"]), "1", "the sel plan is exactly one columnar scan node")


def test_the_group_counters_were_read(dp, expect):
    if _stopped(dp, expect):
        return
    vals = [dp[k] for k in ("doc read", "doc tot", "sel read", "sel tot")]
    got = "yes" if all(_number(v) for v in vals) else f"doc={vals[0]}/{vals[1]} sel={vals[2]}/{vals[3]}"
    expect.text(got, "yes", "the group counters were read, not blank")


def test_the_documented_narrow_query_reads_every_chunk_group(dp, expect):
    if _stopped(dp, expect):
        return
    g = dp["groups"]
    read = dp["doc read"] if _number(dp["doc read"]) else "<no-read-counter>"
    tot = dp["doc tot"] if _number(dp["doc tot"]) else "<no-total-counter>"
    want = f"{g}/{g}" if _number(g) else f"<no catalog count [{g}]>"
    expect.text(f"{read}/{tot}", want, "the documented narrow query reads every chunk group")


def test_filtering_the_ordered_column_prunes(dp, expect):
    if _stopped(dp, expect):
        return
    sr, st = dp["sel read"], dp["sel tot"]
    got = "prunes" if _number(sr) and _number(st) and sr < st else f"{sr} of {st}"
    expect.text(got, "prunes", "and filtering the ordered column instead prunes some away")


def test_the_documented_column_is_not_the_ordered_one(dp, expect):
    if _stopped(dp, expect):
        return
    expect.text("yes" if dp["col"] != "sel" else "no, the page names sel", "yes",
                "the documented column is not the one stored in order")


def _no_cost_block(dp, expect):
    """True where the shell skips its cost block: the page names sel. Declined unnamed."""
    if _stopped(dp, expect):
        return True
    if dp["col"] != "sel":
        return False
    expect.cannot_run("UNMET_PRECONDITION", "the page names the ordered column, so there is no cost to compare")
    return True


def test_both_costs_were_extracted(dp, expect):
    if _no_cost_block(dp, expect):
        return
    dc, sc = dp["doc cost"], dp["sel cost"]
    expect.text("yes" if dc != "" and sc != "" else f"dc={dc} sc={sc}", "yes",
                "both costs were extracted, not blank")


def test_the_documented_column_is_costed_above_the_ordered_one(dp, expect):
    if _no_cost_block(dp, expect):
        return
    dc, sc = dp["doc cost"], dp["sel cost"]
    if not (isinstance(dc, float) and isinstance(sc, float)):
        got = f"unread [{dc}|{sc}]"
    else:
        got = "yes" if dc > sc else f"no ({dc} vs {sc})"
    expect.text(got, "yes", "and the documented column is costed above the ordered one")

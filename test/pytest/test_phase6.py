"""Vectorized aggregates and scans against the scalar path and a heap oracle: the
pytest twin of `test/phase6.sh`, every check by the shell's name (#1341).

A 50,000-row columnar table spans five chunk groups, so the vectorized path iterates
several groups and a filter has groups to skip; a second table holds NULLs in
independent patterns. Each query runs with pgcolumnar.enable_vectorization on, with
it off, and against the heap oracle, and all three must agree, and equal the known
value where the shell names one.

ON AGAINST OFF IS NOT AN ORACLE. Both read the same columnar decode, and 21 of the
39 queries plan identically with vectorization on and off, so that comparison is of
a path with itself. The oracle is schema `oracle`: heap tables of the same names,
filled by the same generator statements and the same DELETE, never copied from the
columnar tables. Each query runs there unchanged under its search_path.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). Values are read in PostgreSQL's text format, columns
joined by "|" and rows by newlines, as psql -qAt prints them. The shell's timing
block records no check and is not ported.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_phase6"
T = ("CREATE TABLE {s}t (id int, s smallint, big bigint, f float8, n numeric, label text){am}; "
     "INSERT INTO {s}t SELECT g, (g%1000)::smallint, g::bigint, g*0.5, (g||'.75')::numeric, 'row'||g "
     "FROM generate_series(1,50000) g")
NT = ("CREATE TABLE {s}nt (a int, code int, txt text){am}; "
      "INSERT INTO {s}nt SELECT CASE WHEN g % 5 = 0 THEN NULL ELSE g END, "
      "CASE WHEN g % 3 = 0 THEN NULL ELSE g % 10 END, "
      "CASE WHEN g % 7 = 0 THEN NULL ELSE 'v'||g END FROM generate_series(1,30000) g")
ON = ("SET pgcolumnar.enable_vectorization = on",)
OFF = ("SET pgcolumnar.enable_vectorization = off",)
HEAP = ("SET search_path = oracle",)

# (name, query, the shell's known value or None), in the shell's order
QUERIES = {
    "count(*)": ("SELECT count(*) FROM t", "50000"),
    "count(id)": ("SELECT count(id) FROM t", "50000"),
    "sum(id)": ("SELECT sum(id) FROM t", "1250025000"),
    "sum(smallint)": ("SELECT sum(s) FROM t", None),
    "avg(id)": ("SELECT avg(id) FROM t", None),
    "min(id),max(id)": ("SELECT min(id), max(id) FROM t", "1|50000"),
    "min/max text": ("SELECT min(label), max(label) FROM t", None),
    "several at once": ("SELECT count(*), sum(id), avg(id), min(id), max(id) FROM t", None),
    "count filtered": ("SELECT count(*) FROM t WHERE id BETWEEN 12000 AND 12100", "101"),
    "sum filtered": ("SELECT sum(id) FROM t WHERE id > 49990", None),
    "avg filtered": ("SELECT avg(id) FROM t WHERE id <= 25000", None),
    "min/max filtered": ("SELECT min(id), max(id) FROM t WHERE id BETWEEN 30000 AND 40000", "30000|40000"),
    "filter other col": ("SELECT sum(id) FROM t WHERE s = 3", None),
    "multi-predicate": ("SELECT count(*), sum(id) FROM t WHERE id > 100 AND id < 200", "99|14850"),
    "full table scan": ("SELECT md5(string_agg(id||'|'||label||'|'||big::text, ',' ORDER BY id)) FROM t", None),
    "filtered projected scan": ("SELECT md5(string_agg(id||'|'||label, ',' ORDER BY id)) FROM t WHERE id BETWEEN 12000 AND 12500", None),
    "count(*) with nulls": ("SELECT count(*) FROM nt", "30000"),
    "count(a) skips nulls": ("SELECT count(a) FROM nt", "24000"),
    "sum(a) ignores nulls": ("SELECT sum(a) FROM nt", None),
    "avg(a) ignores nulls": ("SELECT avg(a) FROM nt", None),
    "min/max(a) with nulls": ("SELECT min(a), max(a) FROM nt", "1|29999"),
    "min/max(txt) nulls": ("SELECT min(txt), max(txt) FROM nt", None),
    "count(code) nulls": ("SELECT count(code) FROM nt", "20000"),
    "filter excludes nulls": ("SELECT count(*), sum(a) FROM nt WHERE a > 100", None),
    "count empty result": ("SELECT count(*) FROM nt WHERE a < 0", "0"),
    "sum empty is null": ("SELECT COALESCE(sum(a)::text,'NULL') FROM nt WHERE a < 0", "NULL"),
    "min empty is null": ("SELECT COALESCE(min(a)::text,'NULL') FROM nt WHERE a < 0", "NULL"),
    "sum(bigint)->numeric": ("SELECT sum(big) FROM t", "1250025000"),
    "avg(bigint)->numeric": ("SELECT avg(big) FROM t", None),
    "sum(numeric)": ("SELECT sum(n) FROM t", None),
    "avg(float8)": ("SELECT round(avg(f)::numeric,4) FROM t", None),
    "sum(float8)": ("SELECT sum(f) FROM t", None),
    "ordered-set agg": ("SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY id) FROM t", "25000.5"),
    "string agg (fallback)": ("SELECT length(string_agg(label,',')) > 0 FROM t", "t"),
    "group by (fallback)": ("SELECT s, count(*) FROM t WHERE s < 3 GROUP BY s ORDER BY s", None),
    "count distinct": ("SELECT count(DISTINCT s) FROM t", "1000"),
    "count after delete": ("SELECT count(*) FROM t", "25000"),
    "sum after delete": ("SELECT sum(id) FROM t", None),
    "scan after delete": ("SELECT md5(string_agg(id::text, ',' ORDER BY id)) FROM t WHERE id < 1000", None),
}
NULLS, DELETES = "count(*) with nulls", "count after delete"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _text(dsn, sql, sets=()):
    """-> the result as psql -qAt prints it, or a failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            cur = conn.execute(sql)
            res = cur.pgresult
            rows = ["|".join("" if res.get_value(i, j) is None else res.get_value(i, j).decode()
                             for j in range(res.nfields)) for i in range(res.ntuples)]
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join(rows) if rows else query_error(f"no rows from {sql}")


def _three(dsn, sql):
    return tuple(_text(dsn, sql, sets) for sets in (ON, OFF, HEAP))


@pytest.fixture(scope="module")
def p6(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        r = {}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar; CREATE SCHEMA oracle")
        _run(dsn, T.format(s="", am=" USING pgcolumnar"))
        _run(dsn, T.format(s="oracle.", am=""))
        r["row count"] = _text(dsn, "SELECT count(*) FROM t")
        r["vectors formed"] = _text(dsn, "SELECT count(*) FROM pgcolumnar.zone_map WHERE "
                                         "storage_id = pgcolumnar.get_storage_id('t') "
                                         "AND vector_index >= 0 AND column_index = 0")
        for name, (sql, _) in QUERIES.items():
            if name == NULLS:
                _run(dsn, NT.format(s="", am=" USING pgcolumnar"))
                _run(dsn, NT.format(s="oracle.", am=""))
            if name == DELETES:
                _run(dsn, "DELETE FROM t WHERE id % 2 = 0")
                _run(dsn, "DELETE FROM oracle.t WHERE id % 2 = 0")
            r[name] = _three(dsn, sql)
            if name == "filtered projected scan":
                r["filtered scan exact value"] = _text(dsn, "SELECT label FROM t WHERE id = 12345")
                r["filtered scan exact count"] = _text(
                    dsn, "SELECT count(*) FROM (SELECT id FROM t WHERE id BETWEEN 12000 AND 12500) x")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _agree(p6, name):
    """-> (got, want): vectorized | scalar | heap, against the known value three
    times, or the heap's reading three times where the shell names none."""
    on, off, heap = p6[name]
    want = QUERIES[name][1] or heap
    return f"{on} || {off} || {heap}", f"{want} || {want} || {want}"


def test_t_row_count(p6, expect):
    expect.text(p6["row count"], "50000", "row count")


def test_vectors_formed(p6, expect):
    expect.text(p6["vectors formed"], "5", "vectors formed")


def test_t_count_star(p6, expect):
    expect.text(*_agree(p6, "count(*)"), "count(*)")


def test_count_id(p6, expect):
    expect.text(*_agree(p6, "count(id)"), "count(id)")


def test_sum_id(p6, expect):
    expect.text(*_agree(p6, "sum(id)"), "sum(id)")


def test_sum_smallint(p6, expect):
    expect.text(*_agree(p6, "sum(smallint)"), "sum(smallint)")


def test_avg_id(p6, expect):
    expect.text(*_agree(p6, "avg(id)"), "avg(id)")


def test_min_id_max_id(p6, expect):
    expect.text(*_agree(p6, "min(id),max(id)"), "min(id),max(id)")


def test_min_max_text(p6, expect):
    expect.text(*_agree(p6, "min/max text"), "min/max text")


def test_several_at_once(p6, expect):
    expect.text(*_agree(p6, "several at once"), "several at once")


def test_count_filtered(p6, expect):
    expect.text(*_agree(p6, "count filtered"), "count filtered")


def test_sum_filtered(p6, expect):
    expect.text(*_agree(p6, "sum filtered"), "sum filtered")


def test_avg_filtered(p6, expect):
    expect.text(*_agree(p6, "avg filtered"), "avg filtered")


def test_min_max_filtered(p6, expect):
    expect.text(*_agree(p6, "min/max filtered"), "min/max filtered")


def test_filter_other_col(p6, expect):
    expect.text(*_agree(p6, "filter other col"), "filter other col")


def test_multi_predicate(p6, expect):
    expect.text(*_agree(p6, "multi-predicate"), "multi-predicate")


def test_full_table_scan(p6, expect):
    expect.text(*_agree(p6, "full table scan"), "full table scan")


def test_filtered_projected_scan(p6, expect):
    expect.text(*_agree(p6, "filtered projected scan"), "filtered projected scan")


def test_filtered_scan_exact_value(p6, expect):
    expect.text(p6["filtered scan exact value"], "row12345", "filtered scan exact value")


def test_filtered_scan_exact_count(p6, expect):
    expect.text(p6["filtered scan exact count"], "501", "filtered scan exact count")


def test_count_with_nulls(p6, expect):
    expect.text(*_agree(p6, "count(*) with nulls"), "count(*) with nulls")


def test_count_a_skips_nulls(p6, expect):
    expect.text(*_agree(p6, "count(a) skips nulls"), "count(a) skips nulls")


def test_sum_a_ignores_nulls(p6, expect):
    expect.text(*_agree(p6, "sum(a) ignores nulls"), "sum(a) ignores nulls")


def test_avg_a_ignores_nulls(p6, expect):
    expect.text(*_agree(p6, "avg(a) ignores nulls"), "avg(a) ignores nulls")


def test_min_max_a_with_nulls(p6, expect):
    expect.text(*_agree(p6, "min/max(a) with nulls"), "min/max(a) with nulls")


def test_min_max_txt_nulls(p6, expect):
    expect.text(*_agree(p6, "min/max(txt) nulls"), "min/max(txt) nulls")


def test_count_code_nulls(p6, expect):
    expect.text(*_agree(p6, "count(code) nulls"), "count(code) nulls")


def test_filter_excludes_nulls(p6, expect):
    expect.text(*_agree(p6, "filter excludes nulls"), "filter excludes nulls")


def test_count_empty_result(p6, expect):
    expect.text(*_agree(p6, "count empty result"), "count empty result")


def test_sum_empty_is_null(p6, expect):
    expect.text(*_agree(p6, "sum empty is null"), "sum empty is null")


def test_min_empty_is_null(p6, expect):
    expect.text(*_agree(p6, "min empty is null"), "min empty is null")


def test_sum_bigint_numeric(p6, expect):
    expect.text(*_agree(p6, "sum(bigint)->numeric"), "sum(bigint)->numeric")


def test_avg_bigint_numeric(p6, expect):
    expect.text(*_agree(p6, "avg(bigint)->numeric"), "avg(bigint)->numeric")


def test_sum_numeric(p6, expect):
    expect.text(*_agree(p6, "sum(numeric)"), "sum(numeric)")


def test_avg_float8(p6, expect):
    expect.text(*_agree(p6, "avg(float8)"), "avg(float8)")


def test_sum_float8(p6, expect):
    expect.text(*_agree(p6, "sum(float8)"), "sum(float8)")


def test_ordered_set_agg(p6, expect):
    expect.text(*_agree(p6, "ordered-set agg"), "ordered-set agg")


def test_string_agg_fallback(p6, expect):
    expect.text(*_agree(p6, "string agg (fallback)"), "string agg (fallback)")


def test_group_by_fallback(p6, expect):
    expect.text(*_agree(p6, "group by (fallback)"), "group by (fallback)")


def test_count_distinct(p6, expect):
    expect.text(*_agree(p6, "count distinct"), "count distinct")


def test_count_after_delete(p6, expect):
    expect.text(*_agree(p6, "count after delete"), "count after delete")


def test_sum_after_delete(p6, expect):
    expect.text(*_agree(p6, "sum after delete"), "sum after delete")


def test_scan_after_delete(p6, expect):
    expect.text(*_agree(p6, "scan after delete"), "scan after delete")

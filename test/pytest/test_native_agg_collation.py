"""min and max under a COLLATE override answer under that collation (#1771). The pytest
twin of `test/native_agg_collation.sh`, every check by the shell's name.

The grouped node and the ungrouped vectorized aggregate compared min/max under the
column's collation and ignored the Aggref's input collation: on an ICU column,
min(t COLLATE "C") answered in ICU order. Every group mixes 'a...' and 'B...', which the
two collations order differently, and a heap mirror is the oracle. A control holds that
min/max under the column's own collation still take the grouped node.

ON A PRIVATE DATABASE; it declines as MISSING_DEPENDENCY where the server has no ICU.
"""
import hashlib

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_agg_collation"
NAMES = ("premise: the groups mix values the two collations order differently",
         "control: min and max under the column's own collation take the grouped node",
         "and answer what heap answers",
         'grouped min and max under a C-collation override answer what heap answers',
         'ungrouped min and max under a C-collation override answer what heap answers')
OWN = "SELECT k, min(t), max(t) FROM {} GROUP BY k"
OVR = 'SELECT k, min(t COLLATE "C"), max(t COLLATE "C") FROM {} GROUP BY k'
UNG = 'SELECT min(t COLLATE "C") || \'|\' || max(t COLLATE "C") FROM {}'


def _rows(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            return c.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _srt(dsn, sql):
    rows = _rows(dsn, sql)
    if isinstance(rows, str):
        return rows
    text = "\n".join(sorted("|".join(str(v) for v in r) for r in rows)) + "\n"
    return hashlib.md5(text.encode()).hexdigest()


def _one(dsn, sql):
    rows = _rows(dsn, sql)
    return rows if isinstance(rows, str) else (str(rows[0][0]) if rows else query_error("no row"))


@pytest.fixture(scope="module")
def nac(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            icu = c.execute("SELECT count(*) FROM pg_collation WHERE collname = 'en-x-icu'").fetchone()[0]
        if icu != 1:
            yield None
            return
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(b'CREATE TABLE cx (k int, t text COLLATE "en-x-icu") USING pgcolumnar; '
                      b"INSERT INTO cx SELECT g % 49, CASE WHEN g % 2 = 0 THEN 'a' || g ELSE 'B' || g END "
                      b"FROM generate_series(1, 5000) g; CREATE TABLE cxh AS SELECT * FROM cx; ANALYZE cx;")
        r = {}
        r["premise"] = _one(dsn, 'SELECT count(*) FROM (SELECT k FROM cxh GROUP BY k HAVING min(t) <> '
                                 'min(t COLLATE "C") AND max(t) <> max(t COLLATE "C")) s')
        plan = _rows(dsn, "EXPLAIN (COSTS OFF) " + OWN.format("cx"))
        r["control plan"] = plan if isinstance(plan, str) else \
            str(sum("Columnar Vectorized Group Keys" in p[0] for p in plan))
        r["own"] = (_srt(dsn, OWN.format("cx")), _srt(dsn, OWN.format("cxh")))
        r["grouped"] = (_srt(dsn, OVR.format("cx")), _srt(dsn, OVR.format("cxh")))
        r["ungrouped"] = (_one(dsn, UNG.format("cx")), _one(dsn, UNG.format("cxh")))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _icu(nac, expect, name):
    if nac is None:
        expect.cannot_run("MISSING_DEPENDENCY", "this server has no ICU collation en-x-icu", name=name)
        return False
    return True


def test_the_groups_mix_both_orderings(nac, expect):
    if _icu(nac, expect, "premise: the groups mix values the two collations order differently"):
        expect.text(nac["premise"], "49", "premise: the groups mix values the two collations order differently")


def test_the_columns_own_collation_takes_the_grouped_node(nac, expect):
    if _icu(nac, expect, "control: min and max under the column's own collation take the grouped node"):
        expect.text(nac["control plan"], "1",
                    "control: min and max under the column's own collation take the grouped node")


def test_the_columns_own_collation_answers_like_heap(nac, expect):
    if _icu(nac, expect, "and answer what heap answers"):
        got, want = nac["own"]
        expect.text(got, want, "and answer what heap answers")


def test_grouped_min_max_under_collate_c_answer_like_heap(nac, expect):
    if _icu(nac, expect, 'grouped min and max under a C-collation override answer what heap answers'):
        got, want = nac["grouped"]
        expect.text(got, want, 'grouped min and max under a C-collation override answer what heap answers')


def test_ungrouped_min_max_under_collate_c_answer_like_heap(nac, expect):
    if _icu(nac, expect, 'ungrouped min and max under a C-collation override answer what heap answers'):
        got, want = nac["ungrouped"]
        expect.text(got, want, 'ungrouped min and max under a C-collation override answer what heap answers')

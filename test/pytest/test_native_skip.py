"""A type binary-coercible to its opclass's type prunes row groups (#1433).

A comparison on a cidr column resolves to inet's operator, and a varchar column's to
text's, with the column relabelled. The reader received a key with a column of cidr
(or varchar) and an argument of inet (or text), called it cross-type, and asked the
opfamily for an ordering proc over the pair. network_ops and text_ops register one
only for (inet, inet) and (text, text), so the key was dropped and the column pruned
nothing, while EXPLAIN still counted the filter as pushed down.

inet and text hold the same values and are the controls: the fixture prunes when the
key is kept. Each arm asserts heap's answer before it asserts pruning, because using
the wrong comparison would give a wrong answer, not only a slow one.

This ports only the #1433 arms of `test/native_skip.sh`; the rest of that suite is
declared INCOMPLETE in `test_compare_to_bash.py`. Independent of the shell arms:
different names, values and row count.
"""
import re

import pytest

REMOVED = re.compile(r"Columnar Chunk Groups Removed by Filter: (\d+)")


@pytest.fixture
def tables(pgc_conn):
    pgc_conn.execute("CREATE DOMAIN skp_cidr AS cidr")
    pgc_conn.execute("CREATE DOMAIN skp_vc AS varchar(16)")
    cols = "id int, ip inet, cd cidr, dcd skp_cidr, t text, vc varchar(16), dvc skp_vc"
    pgc_conn.execute(f"CREATE TABLE skh ({cols})")
    pgc_conn.execute(f"CREATE TABLE skn ({cols}) USING pgcolumnar")
    pgc_conn.execute("SELECT pgcolumnar.set_options('skn', stripe_row_limit => 2000, "
                     "chunk_group_row_limit => 1000)")
    gen = ("SELECT g, a::inet, a::cidr, a::cidr, s, s, s FROM "
           "(SELECT g, '172.16.' || (g / 200) || '.' || (g % 200) AS a, "
           "'k' || lpad(g::text, 5, '0') AS s FROM generate_series(1, 18000) g) x")
    pgc_conn.execute(f"INSERT INTO skh {gen}")
    pgc_conn.execute(f"INSERT INTO skn {gen}")
    return pgc_conn


def _removed(conn, sql):
    plan = "\n".join(r[0] for r in conn.execute(
        "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql).fetchall())
    m = REMOVED.search(plan)
    return int(m.group(1)) if m else 0


def _ids(conn, table, where):
    return conn.execute(f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {table} "
                        f"WHERE {where}").fetchone()[0]


IP_RANGE = "BETWEEN '172.16.30.0' AND '172.16.30.120'"
S_RANGE = "BETWEEN 'k06000' AND 'k06120'"


@pytest.mark.parametrize("col, what, pred", [
    ("ip", "inet", IP_RANGE),
    ("cd", "cidr", IP_RANGE),
    ("dcd", "a domain over cidr", IP_RANGE),
    ("t", "text", S_RANGE),
    ("vc", "varchar", S_RANGE),
    ("dvc", "a domain over varchar", S_RANGE),
])
def test_a_range_on_a_coercible_type_prunes(tables, expect, col, what, pred):
    heap = _ids(tables, "skh", f"{col} {pred}")
    expect.num(len(heap.split(",")) if heap else 0, 121,
               f"premise: heap's range on {what} holds 121 rows")
    expect.text(_ids(tables, "skn", f"{col} {pred}"), heap,
                f"a range on {what} returns the same rows as heap (#1433)")
    expect.num(min(_removed(tables, f"SELECT id FROM skn WHERE {col} {pred}"), 1), 1,
               f"and a range on {what} skips groups (#1433)")


@pytest.mark.parametrize("col, what, val", [
    ("cd", "cidr", "'172.16.55.9'"),
    ("vc", "varchar", "'k11009'"),
])
def test_equality_on_a_coercible_type_prunes(tables, expect, col, what, val):
    """Equality also takes the bloom probe, which hashes the constant with the
    column's hash proc: the same proc for both types of each pair."""
    expect.text(_ids(tables, "skn", f"{col} = {val}"), "11009",
                f"equality on {what} returns the same rows as heap (#1433)")
    expect.text(_ids(tables, "skh", f"{col} = {val}"), "11009",
                f"premise: heap's equality on {what} finds row 11009")
    expect.num(min(_removed(tables, f"SELECT id FROM skn WHERE {col} = {val}"), 1), 1,
               f"and equality on {what} skips groups (#1433)")

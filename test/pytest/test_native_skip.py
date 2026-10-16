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

The rest of `test/native_skip.sh` is ported below by name (#1341): scalar ranges and
equality, including a bigint column against an integer literal (#477) and domains
(#483); an anchored LIKE as a range under the C collation and its refusals (#426); and
a columnar partition taking the custom scan (#436). Independent of the shell arms:
different table names and values where the names allow.
"""
import json
import re

import psycopg
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


# ---- the rest of native_skip.sh, by name (#1341) ------------------------------------

def _one(conn, sql):
    return conn.execute(sql).fetchone()[0]


def _rows(conn, sql):
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def _skips(conn, sql):
    return "yes" if _removed(conn, sql) > 0 else "no"


@pytest.fixture
def scalar(pgc_conn):
    """20480 rows in 2048-row groups, ten groups with contiguous id ranges; d4 and d8
    are domains over int and bigint holding id and k (#483)."""
    c = pgc_conn
    c.execute("CREATE DOMAIN nsq_acct4 AS int")
    c.execute("CREATE DOMAIN nsq_acct8 AS bigint")
    cols = "id int, k bigint, label text, d4 nsq_acct4, d8 nsq_acct8"
    c.execute(f"CREATE TABLE sqh ({cols})")
    c.execute(f"CREATE TABLE sqn ({cols}) USING pgcolumnar")
    c.execute("SELECT pgcolumnar.set_options('sqn', stripe_row_limit => 2048, "
              "chunk_group_row_limit => 1024)")
    gen = ("SELECT g, (g * 10)::bigint, 'lbl-' || (g % 100), g::nsq_acct4, (g * 10)::nsq_acct8 "
           "FROM generate_series(1, 20480) g")
    c.execute(f"INSERT INTO sqh {gen}")
    c.execute(f"INSERT INTO sqn {gen}")
    return c


def test_scalar_ranges_and_equality_prune_and_agree(scalar, expect):
    c = scalar
    plan = c.execute("EXPLAIN (COSTS OFF, FORMAT JSON) "
                     "SELECT id FROM sqn WHERE id BETWEEN 5000 AND 5100").fetchone()[0]
    expect.plan_marker(plan if isinstance(plan, list) else json.loads(plan),
                       "Columnar Projected Columns",
                       name="the plan under test is a columnar custom scan")
    expect.num(_one(c, "SELECT count(*) FROM sqn"), 20480, "row count")
    expect.num(_one(c, "SELECT count(*) FROM sqn"), _one(c, "SELECT count(*) FROM sqh"),
               "no-predicate scan returns all rows")

    expect.row_set(_rows(c, "SELECT id, k, label FROM sqn WHERE id BETWEEN 5000 AND 5100"),
                   _rows(c, "SELECT id, k, label FROM sqh WHERE id BETWEEN 5000 AND 5100"),
                   "range result parity")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE id BETWEEN 5000 AND 5100"), "yes",
                "range skips groups")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE id = 12345"),
               _one(c, "SELECT count(*) FROM sqh WHERE id = 12345"), "equality result parity")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE id = 12345"), "yes",
                "equality skips groups")
    expect.row_set(_rows(c, "SELECT id FROM sqn WHERE k BETWEEN 100000::bigint AND 101000::bigint"),
                   _rows(c, "SELECT id FROM sqh WHERE k BETWEEN 100000::bigint AND 101000::bigint"),
                   "bigint range parity")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE k BETWEEN 100000::bigint AND 101000::bigint"), "yes",
                "bigint range skips groups")
    # the same predicate without the casts (#477): parity before skipping, because the
    # risk in an int8 column against an int4 constant is a wrong answer
    expect.row_set(_rows(c, "SELECT id FROM sqn WHERE k BETWEEN 100000 AND 101000"),
                   _rows(c, "SELECT id FROM sqh WHERE k BETWEEN 100000 AND 101000"),
                   "bigint vs integer literal returns the same rows as heap (#477)")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE k BETWEEN 100000 AND 101000"), "yes",
                "and it skips groups, which the cast version already did (#477)")
    # a domain prunes like its base type (#483), parity first
    expect.row_set(_rows(c, "SELECT id FROM sqn WHERE d4 BETWEEN 5000 AND 5100"),
                   _rows(c, "SELECT id FROM sqh WHERE d4 BETWEEN 5000 AND 5100"),
                   "domain over int returns the same rows as heap (#483)")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE d4 BETWEEN 5000 AND 5100"), "yes",
                "and it skips groups, as the same predicate on int does (#483)")
    expect.row_set(_rows(c, "SELECT id FROM sqn WHERE d8 BETWEEN 100000 AND 101000"),
                   _rows(c, "SELECT id FROM sqh WHERE d8 BETWEEN 100000 AND 101000"),
                   "domain over bigint returns the same rows as heap (#483)")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE d8 BETWEEN 100000 AND 101000"), "yes",
                "and it skips groups too (#483)")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE d4 > 15000"), "yes",
                "one-sided domain predicate skips groups (#483)")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE d4 > 15000"),
               _one(c, "SELECT count(*) FROM sqh WHERE d4 > 15000"), "one-sided domain parity")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE d4 = 12345"), "yes",
                "domain equality skips groups (#483)")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE d4 = 12345"),
               _one(c, "SELECT count(*) FROM sqh WHERE d4 = 12345"), "domain equality parity")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE d4 = 20481"), 0,
               "domain equality on an absent value returns nothing")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE d4 = 20480"), 1,
               "domain equality on a present boundary value finds it")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE k > 150000"), "yes",
                "one-sided bigint vs integer literal skips groups (#477)")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE k > 150000"),
               _one(c, "SELECT count(*) FROM sqh WHERE k > 150000"), "one-sided parity")
    expect.text(_skips(c, "SELECT id FROM sqn WHERE k = 123450"), "yes",
                "bigint equality vs integer literal skips groups (#477)")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE k = 123450"),
               _one(c, "SELECT count(*) FROM sqh WHERE k = 123450"), "bigint equality parity")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE k = 123451"), 0,
               "bigint equality on an absent value returns nothing")
    expect.num(_removed(c, "SELECT id FROM sqn WHERE id > 0"), 0,
               "non-selective scan skips nothing")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE id > 0"),
               _one(c, "SELECT count(*) FROM sqh WHERE id > 0"), "non-selective parity")
    expect.num(_one(c, "SELECT count(*) FROM sqn WHERE id > 1000000"), 0,
               "out-of-range returns nothing")


LGEN = "SELECT 'k' || lpad(g::text, 8, '0') FROM generate_series(1, 20480) g"


def _text_pair(c, h, n, collate):
    c.execute(f"CREATE TABLE {h} (t text{collate})")
    c.execute(f"CREATE TABLE {n} (t text{collate}) USING pgcolumnar")
    c.execute(f"SELECT pgcolumnar.set_options('{n}', stripe_row_limit => 2048, "
              "chunk_group_row_limit => 1024)")
    c.execute(f"INSERT INTO {h} {LGEN}")
    c.execute(f"INSERT INTO {n} {LGEN}")


def test_an_anchored_like_prunes_under_c_and_only_then(pgc_conn, expect):
    """An anchored LIKE is a range under the C collation (#426). Parity beside every
    pruning claim, because "did not prune" and "returned the right rows" differ."""
    c = pgc_conn
    _text_pair(c, "lkh", "lkn", ' COLLATE "C"')

    expect.text(_skips(c, "SELECT t FROM lkn WHERE t >= 'k00005000' AND t < 'k00005001'"),
                "yes", "premise: the equivalent hand-written range prunes (#426)")
    expect.row_set(_rows(c, "SELECT t FROM lkn WHERE t LIKE 'k00005000%'"),
                   _rows(c, "SELECT t FROM lkh WHERE t LIKE 'k00005000%'"),
                   "anchored LIKE result parity (#426)")
    expect.text(_skips(c, "SELECT t FROM lkn WHERE t LIKE 'k00005000%'"), "yes",
                "anchored LIKE prunes groups (#426)")
    expect.text(_skips(c, "SELECT t FROM lkn WHERE t LIKE '%5000%'"), "no",
                "unanchored LIKE does not prune (#426)")
    expect.row_set(_rows(c, "SELECT t FROM lkn WHERE t LIKE '%5000%'"),
                   _rows(c, "SELECT t FROM lkh WHERE t LIKE '%5000%'"),
                   "unanchored LIKE result parity (#426)")
    expect.row_set(_rows(c, "SELECT t FROM lkn WHERE t LIKE 'k0000\\%50%'"),
                   _rows(c, "SELECT t FROM lkh WHERE t LIKE 'k0000\\%50%'"),
                   "escaped-wildcard LIKE result parity (#426)",
                   allow_empty="no row matches the escaped wildcard; parity is the claim")
    expect.row_set(_rows(c, "SELECT t FROM lkn WHERE t LIKE 'k0000500_'"),
                   _rows(c, "SELECT t FROM lkh WHERE t LIKE 'k0000500_'"),
                   "trailing _ wildcard result parity (#426)")
    expect.text(_skips(c, "SELECT t FROM lkn WHERE t LIKE 'k0000500_'"), "yes",
                "trailing _ wildcard still prunes on its prefix (#426)")
    expect.row_set(_rows(c, "SELECT t FROM lkn WHERE t LIKE 'k0000____'"),
                   _rows(c, "SELECT t FROM lkh WHERE t LIKE 'k0000____'"),
                   "swallowed _ wildcards would prune away every match (#426)")
    expect.row_set(_rows(c, "SELECT t FROM lkn WHERE t LIKE 'k00000001%'"),
                   _rows(c, "SELECT t FROM lkh WHERE t LIKE 'k00000001%'"),
                   "low-end prefix parity (#426)")
    expect.text(_skips(c, "SELECT t FROM lkn WHERE t LIKE 'k00000001%'"), "yes",
                "low-end prefix prunes, which needs the upper bound (#426)")

    # the default collation of this cluster is not C, so the bound must not be derived
    _text_pair(c, "ldh", "ldn", "")
    expect.text(_skips(c, "SELECT t FROM ldn WHERE t >= 'k00005000' AND t < 'k00005001'"),
                "yes", "premise: the range prunes on the default-collation table too (#426)")
    expect.text(_skips(c, "SELECT t FROM ldn WHERE t LIKE 'k00005000%'"), "no",
                "default collation that is not C is refused (#426)")
    expect.row_set(_rows(c, "SELECT t FROM ldn WHERE t LIKE 'k00005000%'"),
                   _rows(c, "SELECT t FROM ldh WHERE t LIKE 'k00005000%'"),
                   "default-collation LIKE result parity (#426)")


def test_a_default_collation_in_a_locale_c_database_derives_the_bounds(pgc_cluster, expect):
    """A default-collation column in a --locale=C database is byte ordering, so the
    bound is sound and must be derived (#426)."""
    db = "pgc_nsk_cdb"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        try:
            a.execute(f"CREATE DATABASE {db} LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0")
        except psycopg.Error as exc:
            expect.cannot_run("MISSING_DEPENDENCY",
                              f"a --locale=C database could not be created: {exc}")
            return
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute("CREATE TABLE nc (t text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('nc', stripe_row_limit => 2048, "
                      "chunk_group_row_limit => 1024)")
            c.execute(f"INSERT INTO nc {LGEN}")
            plan = "\n".join(r[0] for r in c.execute(
                "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                "SELECT t FROM nc WHERE t LIKE 'k00005000%'").fetchall())
            m = re.search(r"Usable Skip Predicates: (\d+)", plan)
            expect.text(m.group(1) if m else f"absent: {plan[:200]}", "2",
                        "a default-collation column in a --locale=C database derives "
                        "the bounds (#426)")
            expect.num(_one(c, "SELECT count(*) FROM nc WHERE t LIKE 'k00005000%'"), 1,
                       "and it returns the right rows (#426)")
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_an_anchored_like_does_not_prune_under_a_non_c_collation(pgc_conn, expect):
    """The refusal, on a collation that is genuinely not byte ordering (#426). The
    collation is discovered, as the shell does, because which ones work is a property
    of the build and the host."""
    c = pgc_conn
    found = None
    for coll in ("en_US.utf8", "en_US.UTF-8", "unicode", "pg_unicode_fast"):
        try:
            with c.transaction():
                ok = c.execute(f"SELECT 'x' LIKE 'x' COLLATE \"{coll}\", "
                               f"('b' < 'a' COLLATE \"{coll}\")").fetchone()
        except psycopg.Error:
            continue
        if ok == (True, False):
            found = coll
            break
    if found is None:
        expect.cannot_run("MISSING_DEPENDENCY",
                          "no usable non-C collation on this build")
        return
    _text_pair(c, "luh", "lun", f' COLLATE "{found}"')
    expect.text(_skips(c, "SELECT t FROM lun WHERE t >= 'k00005000' AND t < 'k00005001'"),
                "yes", "premise: a range still prunes under a non-C collation (#426)")
    expect.text(_skips(c, "SELECT t FROM lun WHERE t LIKE 'k00005000%'"), "no",
                "anchored LIKE does not prune under a non-C collation (#426)")
    expect.row_set(_rows(c, "SELECT t FROM lun WHERE t LIKE 'k00005000%'"),
                   _rows(c, "SELECT t FROM luh WHERE t LIKE 'k00005000%'"),
                   "non-C collation LIKE result parity (#426)")


def test_a_columnar_partition_takes_the_custom_scan_and_prunes(pgc_conn, expect):
    """A partition is RELOPT_OTHER_MEMBER_REL, and set_rel_pathlist_hook used to return
    early for it, so a partitioned columnar table lost the custom scan (#436)."""
    c = pgc_conn
    c.execute("CREATE TABLE ptp_h (id int, k int, label text)")
    c.execute("CREATE TABLE ptp (id int, k int, label text) PARTITION BY RANGE (id)")
    c.execute("CREATE TABLE ptp_1 PARTITION OF ptp FOR VALUES FROM (1) TO (10241) USING pgcolumnar")
    c.execute("CREATE TABLE ptp_2 PARTITION OF ptp FOR VALUES FROM (10241) TO (20481) USING pgcolumnar")
    for p in ("ptp_1", "ptp_2"):
        c.execute(f"SELECT pgcolumnar.set_options('{p}', stripe_row_limit => 2048, "
                  "chunk_group_row_limit => 1024)")
    gen = "SELECT g, g * 10, 'lbl-' || (g % 100) FROM generate_series(1, 20480) g"
    c.execute(f"INSERT INTO ptp_h {gen}")
    c.execute(f"INSERT INTO ptp {gen}")
    expect.num(_one(c, "SELECT count(*) FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
                       "WHERE c.relname IN ('ptp_1','ptp_2') AND a.amname = 'pgcolumnar'"), 2,
               "premise: both partitions are columnar (#436)")
    expect.row_set(_rows(c, "SELECT id, k, label FROM ptp WHERE id BETWEEN 5000 AND 5100"),
                   _rows(c, "SELECT id, k, label FROM ptp_h WHERE id BETWEEN 5000 AND 5100"),
                   "a partitioned columnar table returns the same rows as heap (#436)")
    plan = "\n".join(r[0] for r in c.execute(
        "EXPLAIN (COSTS OFF) SELECT id FROM ptp WHERE id BETWEEN 5000 AND 5100").fetchall())
    expect.num(plan.count("Custom Scan (PgColumnarScan)"), 1,
               "a partition takes the columnar custom scan (#436)")
    expect.text(_skips(c, "SELECT id FROM ptp WHERE id BETWEEN 5000 AND 5100"), "yes",
                "and it prunes row groups (#436)")

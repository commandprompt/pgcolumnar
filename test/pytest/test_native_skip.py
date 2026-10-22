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

ONE CHECK PER TEST (#1339). Module fixtures build each data set once; every reading
is read-only after that, so each test runs one query pair and asserts one thing, and
a red cannot stop the next check from running. Every reading goes through `_try`, so
an error is a named red rather than an unnamed test ERROR, and each failure is its own
`query_error()`, so two failed readings never agree (#1527).
"""
import json
import re

import psycopg
import pytest
from pgc_vacuity import QUERY_ERROR, query_error

REMOVED = re.compile(r"Columnar Chunk Groups Removed by Filter: (\d+)")


def _try(fn, *args, **kw):
    """A reading, or the error that replaced it, so a failing query is a named red."""
    try:
        return fn(*args, **kw)
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _num(v, what=""):
    """A count for expect.num, or the failed reading itself, printed: expect.num refuses
    it by the check's name. A -1 stand-in made two failed counts agree (#1527)."""
    if not isinstance(v, int):
        print(f"-- {what or 'reading'}: not a count: {v}")
    return v


def _rowsv(v):
    """Rows for expect.row_set, or one row carrying the error, so it mismatches by name."""
    return v if isinstance(v, list) else [(str(v),)]


def _removed_q(conn, sql):
    """Chunk groups the zone maps removed, or -1 when the plan reports no counter at
    all: a plan that lost the columnar scan must not read as "removed nothing", or
    every refusal arm passes on it (#1510 review)."""
    plan = "\n".join(r[0] for r in conn.execute(
        "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql).fetchall())
    m = REMOVED.search(plan)
    return int(m.group(1)) if m else -1


def _removed(conn, sql):
    return _num(_try(_removed_q, conn, sql), sql)


def _one(conn, sql):
    return conn.execute(sql).fetchone()[0]


def _rows(conn, sql):
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def _ids(conn, table, where):
    return str(_try(lambda: conn.execute(f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {table} "
                                         f"WHERE {where}").fetchone()[0]))


def _schema_conn(pgc_cluster, schema):
    """A connection whose private schema is first on the search_path, as pgc_conn
    gives a test, but for a module fixture."""
    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute(f"SET search_path = {schema}, public")
    return conn


def _drop(conn, schema):
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        conn.close()


# ---- a type binary-coercible to its opclass's type prunes (#1433) -------------------

@pytest.fixture(scope="module")
def tables(pgc_cluster):
    c = _schema_conn(pgc_cluster, "pgc_nsk_coerce")
    try:
        c.execute("CREATE DOMAIN skp_cidr AS cidr")
        c.execute("CREATE DOMAIN skp_vc AS varchar(16)")
        cols = "id int, ip inet, cd cidr, dcd skp_cidr, t text, vc varchar(16), dvc skp_vc"
        c.execute(f"CREATE TABLE skh ({cols})")
        c.execute(f"CREATE TABLE skn ({cols}) USING pgcolumnar")
        c.execute("SELECT pgcolumnar.set_options('skn', stripe_row_limit => 2000, "
                  "chunk_group_row_limit => 1000)")
        gen = ("SELECT g, a::inet, a::cidr, a::cidr, s, s, s FROM "
               "(SELECT g, '172.16.' || (g / 200) || '.' || (g % 200) AS a, "
               "'k' || lpad(g::text, 5, '0') AS s FROM generate_series(1, 18000) g) x")
        _try(c.execute, f"INSERT INTO skh {gen}")
        _try(c.execute, f"INSERT INTO skn {gen}")
        yield c
    finally:
        _drop(c, "pgc_nsk_coerce")


IP_RANGE = "BETWEEN '172.16.30.0' AND '172.16.30.120'"
S_RANGE = "BETWEEN 'k06000' AND 'k06120'"
COERCE_RANGES = [
    ("ip", "inet", IP_RANGE),
    ("cd", "cidr", IP_RANGE),
    ("dcd", "a domain over cidr", IP_RANGE),
    ("t", "text", S_RANGE),
    ("vc", "varchar", S_RANGE),
    ("dvc", "a domain over varchar", S_RANGE),
]


@pytest.mark.parametrize("col, what, pred", COERCE_RANGES)
def test_heaps_range_on_a_coercible_type_holds_121_rows(tables, expect, col, what, pred):
    heap = _ids(tables, "skh", f"{col} {pred}")
    expect.num(len(heap.split(",")) if heap and not heap.startswith(QUERY_ERROR) else 0, 121,
               f"premise: heap's range on {what} holds 121 rows")


@pytest.mark.parametrize("col, what, pred", COERCE_RANGES)
def test_a_range_on_a_coercible_type_returns_heaps_rows(tables, expect, col, what, pred):
    expect.text(_ids(tables, "skn", f"{col} {pred}"), _ids(tables, "skh", f"{col} {pred}"),
                f"a range on {what} returns the same rows as heap (#1433)")


@pytest.mark.parametrize("col, what, pred", COERCE_RANGES)
def test_a_range_on_a_coercible_type_skips_groups(tables, expect, col, what, pred):
    expect.at_least(_removed(tables, f"SELECT id FROM skn WHERE {col} {pred}"), 1,
                    f"and a range on {what} skips groups (#1433)")


COERCE_EQ = [
    ("cd", "cidr", "'172.16.55.9'"),
    ("vc", "varchar", "'k11009'"),
]


@pytest.mark.parametrize("col, what, val", COERCE_EQ)
def test_equality_on_a_coercible_type_returns_heaps_rows(tables, expect, col, what, val):
    """Equality also takes the bloom probe, which hashes the constant with the
    column's hash proc: the same proc for both types of each pair."""
    expect.text(_ids(tables, "skn", f"{col} = {val}"), "11009",
                f"equality on {what} returns the same rows as heap (#1433)")


@pytest.mark.parametrize("col, what, val", COERCE_EQ)
def test_heaps_equality_on_a_coercible_type_finds_the_row(tables, expect, col, what, val):
    expect.text(_ids(tables, "skh", f"{col} = {val}"), "11009",
                f"premise: heap's equality on {what} finds row 11009")


@pytest.mark.parametrize("col, what, val", COERCE_EQ)
def test_equality_on_a_coercible_type_skips_groups(tables, expect, col, what, val):
    expect.at_least(_removed(tables, f"SELECT id FROM skn WHERE {col} = {val}"), 1,
                    f"and equality on {what} skips groups (#1433)")


# ---- the rest of native_skip.sh, by name (#1341) ------------------------------------

@pytest.fixture(scope="module")
def scalar(pgc_cluster):
    """20480 rows in 2048-row groups, ten groups with contiguous id ranges; d4 and d8
    are domains over int and bigint holding id and k (#483)."""
    c = _schema_conn(pgc_cluster, "pgc_nsk_scalar")
    try:
        c.execute("CREATE DOMAIN nsq_acct4 AS int")
        c.execute("CREATE DOMAIN nsq_acct8 AS bigint")
        cols = "id int, k bigint, label text, d4 nsq_acct4, d8 nsq_acct8"
        c.execute(f"CREATE TABLE sqh ({cols})")
        c.execute(f"CREATE TABLE sqn ({cols}) USING pgcolumnar")
        c.execute("SELECT pgcolumnar.set_options('sqn', stripe_row_limit => 2048, "
                  "chunk_group_row_limit => 1024)")
        gen = ("SELECT g, (g * 10)::bigint, 'lbl-' || (g % 100), g::nsq_acct4, (g * 10)::nsq_acct8 "
               "FROM generate_series(1, 20480) g")
        _try(c.execute, f"INSERT INTO sqh {gen}")
        _try(c.execute, f"INSERT INTO sqn {gen}")
        yield c
    finally:
        _drop(c, "pgc_nsk_scalar")


def test_the_scalar_plan_is_a_columnar_custom_scan(scalar, expect):
    plan = _try(lambda: scalar.execute("EXPLAIN (COSTS OFF, FORMAT JSON) "
                                       "SELECT id FROM sqn WHERE id BETWEEN 5000 AND 5100").fetchone()[0])
    if isinstance(plan, str) and plan.startswith(QUERY_ERROR):
        plan = [{"Plan": {"Node Type": plan}}]
    expect.plan_marker(plan if isinstance(plan, list) else json.loads(plan),
                       "Columnar Projected Columns",
                       name="the plan under test is a columnar custom scan")


# (name, select list, where): the columnar rows equal heap's
SCALAR_ROWS = [
    ("range result parity", "id, k, label", "id BETWEEN 5000 AND 5100"),
    ("bigint range parity", "id", "k BETWEEN 100000::bigint AND 101000::bigint"),
    # the same predicate without the casts (#477): parity before skipping, because the
    # risk in an int8 column against an int4 constant is a wrong answer
    ("bigint vs integer literal returns the same rows as heap (#477)", "id", "k BETWEEN 100000 AND 101000"),
    # a domain prunes like its base type (#483), parity first
    ("domain over int returns the same rows as heap (#483)", "id", "d4 BETWEEN 5000 AND 5100"),
    ("domain over bigint returns the same rows as heap (#483)", "id", "d8 BETWEEN 100000 AND 101000"),
]


@pytest.mark.parametrize("name, cols, where", SCALAR_ROWS)
def test_scalar_rows_agree_with_heap(scalar, expect, name, cols, where):
    expect.row_set(_rowsv(_try(_rows, scalar, f"SELECT {cols} FROM sqn WHERE {where}")),
                   _rowsv(_try(_rows, scalar, f"SELECT {cols} FROM sqh WHERE {where}")), name)


# (name, where): the columnar count equals heap's
SCALAR_COUNTS = [
    ("no-predicate scan returns all rows", "true"),
    ("equality result parity", "id = 12345"),
    ("one-sided domain parity", "d4 > 15000"),
    ("domain equality parity", "d4 = 12345"),
    ("one-sided parity", "k > 150000"),
    ("bigint equality parity", "k = 123450"),
    ("non-selective parity", "id > 0"),
]


@pytest.mark.parametrize("name, where", SCALAR_COUNTS)
def test_scalar_counts_agree_with_heap(scalar, expect, name, where):
    expect.num(_num(_try(_one, scalar, f"SELECT count(*) FROM sqn WHERE {where}"), name),
               _num(_try(_one, scalar, f"SELECT count(*) FROM sqh WHERE {where}"), name), name)


# (name, where, want): the columnar count is a known number
SCALAR_KNOWN = [
    ("row count", "true", 20480),
    ("domain equality on an absent value returns nothing", "d4 = 20481", 0),
    ("domain equality on a present boundary value finds it", "d4 = 20480", 1),
    ("bigint equality on an absent value returns nothing", "k = 123451", 0),
    ("out-of-range returns nothing", "id > 1000000", 0),
]


@pytest.mark.parametrize("name, where, want", SCALAR_KNOWN)
def test_scalar_counts_are_known(scalar, expect, name, where, want):
    expect.num(_num(_try(_one, scalar, f"SELECT count(*) FROM sqn WHERE {where}"), name), want, name)


# (name, where): the zone maps remove at least one group
SCALAR_SKIPS = [
    ("range skips groups", "id BETWEEN 5000 AND 5100"),
    ("equality skips groups", "id = 12345"),
    ("bigint range skips groups", "k BETWEEN 100000::bigint AND 101000::bigint"),
    ("and it skips groups, which the cast version already did (#477)", "k BETWEEN 100000 AND 101000"),
    ("and it skips groups, as the same predicate on int does (#483)", "d4 BETWEEN 5000 AND 5100"),
    ("and it skips groups too (#483)", "d8 BETWEEN 100000 AND 101000"),
    ("one-sided domain predicate skips groups (#483)", "d4 > 15000"),
    ("domain equality skips groups (#483)", "d4 = 12345"),
    ("one-sided bigint vs integer literal skips groups (#477)", "k > 150000"),
    ("bigint equality vs integer literal skips groups (#477)", "k = 123450"),
]


@pytest.mark.parametrize("name, where", SCALAR_SKIPS)
def test_a_scalar_predicate_skips_groups(scalar, expect, name, where):
    expect.at_least(_removed(scalar, f"SELECT id FROM sqn WHERE {where}"), 1, name)


def test_a_non_selective_scan_skips_nothing(scalar, expect):
    expect.num(_removed(scalar, "SELECT id FROM sqn WHERE id > 0"), 0, "non-selective scan skips nothing")


# ---- an anchored LIKE is a range under the C collation (#426) -----------------------

LGEN = "SELECT 'k' || lpad(g::text, 8, '0') FROM generate_series(1, 20480) g"


def _text_pair(c, h, n, collate):
    c.execute(f"CREATE TABLE {h} (t text{collate})")
    c.execute(f"CREATE TABLE {n} (t text{collate}) USING pgcolumnar")
    c.execute(f"SELECT pgcolumnar.set_options('{n}', stripe_row_limit => 2048, "
              "chunk_group_row_limit => 1024)")
    _try(c.execute, f"INSERT INTO {h} {LGEN}")
    _try(c.execute, f"INSERT INTO {n} {LGEN}")


@pytest.fixture(scope="module")
def like(pgc_cluster):
    """lkh/lkn under COLLATE "C", and ldh/ldn under this cluster's default collation,
    which is not C, so the bound must not be derived there."""
    c = _schema_conn(pgc_cluster, "pgc_nsk_like")
    try:
        _text_pair(c, "lkh", "lkn", ' COLLATE "C"')
        _text_pair(c, "ldh", "ldn", "")
        yield c
    finally:
        _drop(c, "pgc_nsk_like")


# (name, table, where): the zone maps remove at least one group. Parity sits beside
# every pruning claim, because "did not prune" and "returned the right rows" differ.
LIKE_SKIPS = [
    ("premise: the equivalent hand-written range prunes (#426)", "lkn", "t >= 'k00005000' AND t < 'k00005001'"),
    ("anchored LIKE prunes groups (#426)", "lkn", "t LIKE 'k00005000%'"),
    ("trailing _ wildcard still prunes on its prefix (#426)", "lkn", "t LIKE 'k0000500_'"),
    ("low-end prefix prunes, which needs the upper bound (#426)", "lkn", "t LIKE 'k00000001%'"),
    ("premise: the range prunes on the default-collation table too (#426)", "ldn",
     "t >= 'k00005000' AND t < 'k00005001'"),
]


@pytest.mark.parametrize("name, table, where", LIKE_SKIPS)
def test_a_like_or_range_prunes(like, expect, name, table, where):
    expect.at_least(_removed(like, f"SELECT t FROM {table} WHERE {where}"), 1, name)


# (name, table, where): the zone maps remove nothing
LIKE_NO_SKIP = [
    ("unanchored LIKE does not prune (#426)", "lkn", "t LIKE '%5000%'"),
    ("default collation that is not C is refused (#426)", "ldn", "t LIKE 'k00005000%'"),
]


@pytest.mark.parametrize("name, table, where", LIKE_NO_SKIP)
def test_a_like_does_not_prune(like, expect, name, table, where):
    expect.num(_removed(like, f"SELECT t FROM {table} WHERE {where}"), 0, name)


# (name, columnar table, heap table, where, allow_empty): the rows equal heap's
LIKE_ROWS = [
    ("anchored LIKE result parity (#426)", "lkn", "lkh", "t LIKE 'k00005000%'", None),
    ("unanchored LIKE result parity (#426)", "lkn", "lkh", "t LIKE '%5000%'", None),
    ("escaped-wildcard LIKE result parity (#426)", "lkn", "lkh", "t LIKE 'k0000\\%50%'",
     "no row matches the escaped wildcard; parity is the claim"),
    ("trailing _ wildcard result parity (#426)", "lkn", "lkh", "t LIKE 'k0000500_'", None),
    ("swallowed _ wildcards would prune away every match (#426)", "lkn", "lkh", "t LIKE 'k0000____'", None),
    ("low-end prefix parity (#426)", "lkn", "lkh", "t LIKE 'k00000001%'", None),
    ("default-collation LIKE result parity (#426)", "ldn", "ldh", "t LIKE 'k00005000%'", None),
]


@pytest.mark.parametrize("name, n, h, where, allow_empty", LIKE_ROWS)
def test_a_like_returns_heaps_rows(like, expect, name, n, h, where, allow_empty):
    expect.row_set(_rowsv(_try(_rows, like, f"SELECT t FROM {n} WHERE {where}")),
                   _rowsv(_try(_rows, like, f"SELECT t FROM {h} WHERE {where}")), name,
                   allow_empty=allow_empty)


@pytest.fixture(scope="module")
def locale_c(pgc_cluster):
    """A default-collation column in a --locale=C database is byte ordering, so the
    bound is sound and must be derived (#426). -> the readings, or the reason no such
    database could be made."""
    db = "pgc_nsk_cdb"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        try:
            a.execute(f"CREATE DATABASE {db} LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0")
        except (psycopg.errors.InvalidParameterValue, psycopg.errors.FeatureNotSupported) as exc:
            yield {"declined": f"a --locale=C database could not be created: {exc}"}
            return
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute("CREATE TABLE nc (t text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('nc', stripe_row_limit => 2048, "
                      "chunk_group_row_limit => 1024)")
            _try(c.execute, f"INSERT INTO nc {LGEN}")
            plan = str(_try(lambda: "\n".join(r[0] for r in c.execute(
                "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                "SELECT t FROM nc WHERE t LIKE 'k00005000%'").fetchall())))
            m = re.search(r"Usable Skip Predicates: (\d+)", plan)
            yield {"usable": m.group(1) if m else f"absent: {plan[:200]}",
                   "count": _try(_one, c, "SELECT count(*) FROM nc WHERE t LIKE 'k00005000%'")}
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_a_default_collation_in_a_locale_c_database_derives_the_bounds(locale_c, expect):
    if "declined" in locale_c:
        expect.cannot_run("MISSING_DEPENDENCY", locale_c["declined"])
        return
    expect.text(locale_c["usable"], "2",
                "a default-collation column in a --locale=C database derives the bounds (#426)")


def test_and_it_returns_the_right_rows(locale_c, expect):
    if "declined" in locale_c:
        expect.cannot_run("MISSING_DEPENDENCY", locale_c["declined"])
        return
    expect.num(_num(locale_c["count"], "count"), 1, "and it returns the right rows (#426)")


@pytest.fixture(scope="module")
def non_c(pgc_cluster):
    """The refusal, on a collation that is genuinely not byte ordering (#426). The
    collation is discovered, as the shell does, because which ones work is a property
    of the build and the host. -> (conn, None) or (None, the reason)."""
    c = _schema_conn(pgc_cluster, "pgc_nsk_nonc")
    try:
        found = None
        for coll in ("en_US.utf8", "en_US.UTF-8", "unicode", "pg_unicode_fast"):
            try:
                with c.transaction():
                    ok = c.execute(f"SELECT 'x' LIKE 'x' COLLATE \"{coll}\", "
                                   f"('b' < 'a' COLLATE \"{coll}\")").fetchone()
            except (psycopg.errors.UndefinedObject, psycopg.errors.FeatureNotSupported):
                continue
            if ok == (True, False):
                found = coll
                break
        if found is None:
            yield None, "no usable non-C collation on this build"
        else:
            _text_pair(c, "luh", "lun", f' COLLATE "{found}"')
            yield c, None
    finally:
        _drop(c, "pgc_nsk_nonc")


def _non_c(non_c, expect):
    c, reason = non_c
    if c is None:
        expect.cannot_run("MISSING_DEPENDENCY", reason)
    return c


def test_a_range_still_prunes_under_a_non_c_collation(non_c, expect):
    c = _non_c(non_c, expect)
    if c is not None:
        expect.at_least(_removed(c, "SELECT t FROM lun WHERE t >= 'k00005000' AND t < 'k00005001'"), 1,
                        "premise: a range still prunes under a non-C collation (#426)")


def test_an_anchored_like_does_not_prune_under_a_non_c_collation(non_c, expect):
    c = _non_c(non_c, expect)
    if c is not None:
        expect.num(_removed(c, "SELECT t FROM lun WHERE t LIKE 'k00005000%'"), 0,
                   "anchored LIKE does not prune under a non-C collation (#426)")


def test_a_non_c_collation_like_returns_heaps_rows(non_c, expect):
    c = _non_c(non_c, expect)
    if c is not None:
        expect.row_set(_rowsv(_try(_rows, c, "SELECT t FROM lun WHERE t LIKE 'k00005000%'")),
                       _rowsv(_try(_rows, c, "SELECT t FROM luh WHERE t LIKE 'k00005000%'")),
                       "non-C collation LIKE result parity (#426)")


# ---- a columnar partition takes the custom scan (#436) ------------------------------

@pytest.fixture(scope="module")
def part(pgc_cluster):
    """A partition is RELOPT_OTHER_MEMBER_REL, and set_rel_pathlist_hook used to return
    early for it, so a partitioned columnar table lost the custom scan (#436)."""
    c = _schema_conn(pgc_cluster, "pgc_nsk_part")
    try:
        c.execute("CREATE TABLE ptp_h (id int, k int, label text)")
        c.execute("CREATE TABLE ptp (id int, k int, label text) PARTITION BY RANGE (id)")
        c.execute("CREATE TABLE ptp_1 PARTITION OF ptp FOR VALUES FROM (1) TO (10241) USING pgcolumnar")
        c.execute("CREATE TABLE ptp_2 PARTITION OF ptp FOR VALUES FROM (10241) TO (20481) USING pgcolumnar")
        for p in ("ptp_1", "ptp_2"):
            c.execute(f"SELECT pgcolumnar.set_options('{p}', stripe_row_limit => 2048, "
                      "chunk_group_row_limit => 1024)")
        gen = "SELECT g, g * 10, 'lbl-' || (g % 100) FROM generate_series(1, 20480) g"
        _try(c.execute, f"INSERT INTO ptp_h {gen}")
        _try(c.execute, f"INSERT INTO ptp {gen}")
        yield c
    finally:
        _drop(c, "pgc_nsk_part")


def test_both_partitions_are_columnar(part, expect):
    expect.num(_num(_try(_one, part, "SELECT count(*) FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
                                     "WHERE c.relname IN ('ptp_1','ptp_2') AND a.amname = 'pgcolumnar' "
                                     "AND c.relnamespace = current_schema()::regnamespace"), "partitions"), 2,
               "premise: both partitions are columnar (#436)")


def test_a_partitioned_columnar_table_returns_heaps_rows(part, expect):
    expect.row_set(_rowsv(_try(_rows, part, "SELECT id, k, label FROM ptp WHERE id BETWEEN 5000 AND 5100")),
                   _rowsv(_try(_rows, part, "SELECT id, k, label FROM ptp_h WHERE id BETWEEN 5000 AND 5100")),
                   "a partitioned columnar table returns the same rows as heap (#436)")


def test_a_partition_takes_the_columnar_custom_scan(part, expect):
    plan = str(_try(lambda: "\n".join(r[0] for r in part.execute(
        "EXPLAIN (COSTS OFF) SELECT id FROM ptp WHERE id BETWEEN 5000 AND 5100").fetchall())))
    expect.num(plan.count("Custom Scan (PgColumnarScan)"), 1,
               "a partition takes the columnar custom scan (#436)")


def test_a_partition_prunes_row_groups(part, expect):
    expect.at_least(_removed(part, "SELECT id FROM ptp WHERE id BETWEEN 5000 AND 5100"), 1,
                    "and it prunes row groups (#436)")


# ---- a timestamp column against a timestamptz constant (#1738) ----------------------
TZ_GEN = """SELECT g, v, v::timestamptz FROM (SELECT g, CASE
    WHEN g <= 2048 THEN '2024-03-01 00:00'::timestamp + g * interval '1 minute'
    WHEN g <= 4096 THEN '2024-03-10 01:00'::timestamp + ((g - 2049) % 121) * interval '1 minute'
    WHEN g <= 6144 THEN '2024-03-10 02:30'::timestamp + ((g - 4097) % 61) * interval '1 minute'
    ELSE '2024-11-03 00:00'::timestamp + ((g - 6145) % 181) * interval '1 minute' END AS v
  FROM generate_series(1, 8192) g) x"""

GAP_ARMS = [(">", "'2024-03-10 07:15+00'"), (">=", "'2024-03-10 07:30+00'"), ("=", "'2024-03-10 07:30+00'"),
            ("<", "'2024-03-10 07:15+00'"), ("<=", "'2024-03-10 07:14+00'")]


@pytest.fixture(scope="module")
def zone(pgc_cluster):
    """The cross-type proc for (timestamp, timestamptz) turns a group's min/max wall
    time into an instant in the session zone, and across a spring-forward gap that is
    not order-preserving: in New York 02:30 on 2024-03-10 is 07:30Z, 03:00 is 07:00Z.
    Four chunk groups of 2048 (two vectors each): early March, the gap hour with max 03:00, 02:30..03:30 with
    min 02:30, and the fall-back hour (#1738)."""
    c = _schema_conn(pgc_cluster, "pgc_nsk_zone")
    try:
        c.execute("SET timezone = 'America/New_York'")
        c.execute("CREATE TABLE hz (id int, ts timestamp, tz timestamptz)")
        c.execute("CREATE TABLE nz (id int, ts timestamp, tz timestamptz) USING pgcolumnar")
        c.execute("SELECT pgcolumnar.set_options('nz', stripe_row_limit => 2048, chunk_group_row_limit => 1024)")
        _try(c.execute, f"INSERT INTO hz {TZ_GEN}")
        _try(c.execute, f"INSERT INTO nz {TZ_GEN}")
        yield c
    finally:
        _drop(c, "pgc_nsk_zone")


@pytest.mark.parametrize("op,val", GAP_ARMS, ids=[o for o, _ in GAP_ARMS])
def test_heap_finds_rows_across_the_gap(zone, expect, op, val):
    n = _num(_try(_one, zone, f"SELECT count(*) FROM hz WHERE ts {op} {val}::timestamptz"), "heap rows")
    expect.text("yes" if isinstance(n, int) and n > 0 else f"no ({n})", "yes",
                f"premise: heap finds rows for ts {op} a timestamptz across the gap (#1738)")


@pytest.mark.parametrize("op,val", GAP_ARMS, ids=[o for o, _ in GAP_ARMS])
def test_a_timestamptz_constant_across_the_gap_returns_heaps_rows(zone, expect, op, val):
    expect.row_set(_rowsv(_try(_rows, zone, f"SELECT id FROM nz WHERE ts {op} {val}::timestamptz")),
                   _rowsv(_try(_rows, zone, f"SELECT id FROM hz WHERE ts {op} {val}::timestamptz")),
                   f"ts {op} a timestamptz constant across a spring-forward gap returns heap's rows (#1738)")


def test_across_the_fall_back_hour_returns_heaps_rows(zone, expect):
    w = "ts > '2024-11-03 05:30+00'::timestamptz"
    expect.row_set(_rowsv(_try(_rows, zone, f"SELECT id FROM nz WHERE {w}")),
                   _rowsv(_try(_rows, zone, f"SELECT id FROM hz WHERE {w}")),
                   "across the fall-back hour, ts > a timestamptz constant returns heap's rows (#1738)")


def test_the_reverse_pair_in_the_gap_returns_heaps_rows(zone, expect):
    w = "tz > '2024-03-10 02:30'::timestamp"
    expect.row_set(_rowsv(_try(_rows, zone, f"SELECT id FROM nz WHERE {w}")),
                   _rowsv(_try(_rows, zone, f"SELECT id FROM hz WHERE {w}")),
                   "the reverse pair, tz > a timestamp constant in the gap, returns heap's rows (#1738)")


def test_a_same_type_constant_still_skips(zone, expect):
    expect.at_least(_removed(zone, "SELECT id FROM nz WHERE ts > '2024-03-05'::timestamp"), 1,
                    "a timestamp column against a timestamp constant still skips groups (#1738)")


def test_the_reverse_pair_still_skips(zone, expect):
    expect.at_least(_removed(zone, "SELECT id FROM nz WHERE tz > '2024-03-05'::timestamp"), 1,
                    "the reverse pair, tz against a timestamp constant, still skips groups (#1738)")

"""The pytest twin of `test/inheritance.sh`, every check by the shell's name (#432).

A legacy INHERITS parent must plan as an Append of its children, not as a custom scan
or a vectorized aggregate of the parent's own storage: PgColumnarSetRelPathlist and
the ungrouped vector-aggregate path both run on the inheritance appendrel
(RELKIND_RELATION with rte->inh set), and a scan added there reads only the parent's
file. Measured before the fix: parent 1 row, child 5000, count(*) from the parent 1
where the heap mirror said 5001. A declarative partition is a different RTE and must
still get the custom scan (#436). The heap tree beside the columnar one is the oracle.

A partitioned parent carrying the columnar access method has no storage, and is
refused rather than read; a columnar materialized view is still columnar (#1259,
#1341).

From PostgreSQL 17 a partitioned table may carry relam = pgcolumnar as the default for
its future partitions. It has no storage of its own, relfilenode 0, and
PgColumnarIsColumnarRelation used to answer "yes" from relam alone. Every caller that
then read storage handed smgropen a zero relfilenumber: an abort on an assert build,
and on a release build a failure to open ".../0/0", 58P01. So a refusal here is
42809 (wrong_object_type), not any error, and liveness is read from the server log,
since a crashed cluster restarts and a later connection succeeds.
"""
import psycopg
import pytest

from pgc_vacuity import query_error


def _crashes(pgc_cluster):
    """-> how many backends the server log says died, or a failed reading when the
    log can't be read: a blind count must not read as "no crashes"."""
    try:
        return (pgc_cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _since(pgc_cluster, before):
    """-> backends died since `before`, or the failed reading when either count is
    one: a blind count must not read as "no crashes"."""
    now = _crashes(pgc_cluster)
    for v in (before, now):
        if not isinstance(v, int):
            return v
    return now - before


def _sqlstate(conn, sql):
    """-> 'accepted', or the SQLSTATE the statement raised."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "accepted"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no SQLSTATE ({str(exc).splitlines()[0]})"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _first_line(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _q(conn, sql):
    """-> the first column of the first row, or a failed reading."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    except psycopg.Error as exc:
        return query_error(_first_line(exc))
    return row[0] if row else query_error("no row")


def _rows(conn, sql):
    """-> every row, or a failed reading."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except psycopg.Error as exc:
        return query_error(_first_line(exc))


def _count_in_plan(conn, sql, needle):
    """-> how many lines of the statement's plan contain `needle`, as the shell's grep -c."""
    plan = _rows(conn, f"EXPLAIN (COSTS OFF) {sql}")
    return plan if isinstance(plan, str) else sum(needle in r[0] for r in plan)


TREE_DB = "pgc_inheritance_tree"


@pytest.fixture(scope="module")
def tree(pgc_cluster):
    """Every reading the inheritance-tree arms assert, in the shell's order: a heap
    INHERITS tree as the oracle beside a columnar one, a one-row parent and child,
    then a 4991-row child, then a declarative partition. One test per check reads it,
    so a red on one arm does not hide the next."""
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {TREE_DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {TREE_DB}")
    r = {}
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=TREE_DB), autocommit=True) as conn:
            def build(*stmts):
                try:
                    for sql in stmts:
                        conn.execute(sql)
                except psycopg.Error as exc:
                    print(f"-- fixture build failed: {_first_line(exc)}")

            build("CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                  "CREATE TABLE inh_h (id int, v text)",
                  "CREATE TABLE inh_hc () INHERITS (inh_h)",
                  "INSERT INTO inh_h VALUES (1, 'parent')",
                  "INSERT INTO inh_hc VALUES (2, 'child')",
                  "CREATE TABLE inh_p (id int, v text) USING pgcolumnar",
                  "CREATE TABLE inh_c () INHERITS (inh_p) USING pgcolumnar",
                  "INSERT INTO inh_p VALUES (1, 'parent')",
                  "INSERT INTO inh_c VALUES (2, 'child')")
            r["heap"] = _q(conn, "SELECT count(*) FROM inh_h")
            r["columnar"] = _q(conn, "SELECT count(*) FROM inh_p")
            r["only"] = _q(conn, "SELECT count(*) FROM ONLY inh_p")
            child = _rows(conn, "SELECT v FROM inh_p WHERE id = 2")
            # no row is the bug's answer, so it is a reading to fail on, not a failed one
            r["child"] = child if isinstance(child, str) else (child[0][0] if child else "(no row)")
            r["ids"] = (_rows(conn, "SELECT id, v FROM inh_p"), _rows(conn, "SELECT id, v FROM inh_h"))
            r["star_appends"] = _count_in_plan(conn, "SELECT * FROM inh_p", "Append")
            r["count_appends"] = _count_in_plan(conn, "SELECT count(*) FROM inh_p", "Append")

            # A cheap parent and a large child is the shape that made the custom scan
            # win: the appendrel is costed from the parent's empty-looking storage.
            build("INSERT INTO inh_c SELECT g, 'c' || g FROM generate_series(10, 5000) g",
                  "INSERT INTO inh_hc SELECT g, 'c' || g FROM generate_series(10, 5000) g",
                  "ANALYZE inh_p", "ANALYZE inh_c", "ANALYZE inh_h", "ANALYZE inh_hc")
            r["large"] = (_q(conn, "SELECT count(*) FROM inh_p"), _q(conn, "SELECT count(*) FROM inh_h"))
            r["filtered"] = (_rows(conn, "SELECT id FROM inh_p WHERE id > 100"),
                             _rows(conn, "SELECT id FROM inh_h WHERE id > 100"))
            build("SET pgcolumnar.enable_ungrouped_vector_agg = on")
            vec = _q(conn, "SELECT count(*) FROM inh_p")
            build("RESET pgcolumnar.enable_ungrouped_vector_agg")
            r["vector"] = (vec, _q(conn, "SELECT count(*) FROM inh_h"))

            # Declarative partitions were the #436 case: the custom scan MUST still
            # fire on the partition, or the refusal of rte->inh would put it back on
            # a seqscan.
            build("CREATE TABLE inh_prt (id int, v text) PARTITION BY RANGE (id)",
                  "CREATE TABLE inh_prt1 PARTITION OF inh_prt FOR VALUES FROM (0) TO (100000)"
                  " USING pgcolumnar",
                  "INSERT INTO inh_prt SELECT g, 'p' || g FROM generate_series(1, 2000) g")
            r["partitioned"] = _q(conn, "SELECT count(*) FROM inh_prt")
            r["partition_scans"] = _count_in_plan(conn, "SELECT * FROM inh_prt",
                                                  "Custom Scan (PgColumnarScan)")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {TREE_DB} WITH (FORCE)")


def test_the_heap_parent_includes_its_child(tree, expect):
    expect.num(tree["heap"], 2, "INHERITS parent includes the child (heap oracle)")


def test_the_columnar_parent_includes_its_child(tree, expect):
    expect.num(tree["columnar"], 2, "INHERITS parent includes the child (columnar)")


def test_only_the_parent_is_just_the_parent(tree, expect):
    expect.num(tree["only"], 1, "ONLY parent is still just the parent")


def test_the_child_is_visible_through_the_parent(tree, expect):
    expect.text(tree["child"], "child", "the child is visible through the parent")


def test_the_row_identities_match_the_heap(tree, expect):
    got, want = tree["ids"]
    expect.row_set(got, want, "row identities match the heap")


def test_select_star_from_the_parent_plans_an_append(tree, expect):
    expect.num(tree["star_appends"], 1, "SELECT * from an INHERITS parent plans an Append")


def test_count_from_the_parent_plans_an_append(tree, expect):
    expect.num(tree["count_appends"], 1, "count(*) from an INHERITS parent plans an Append")


def test_a_large_child_is_visible_through_the_parent(tree, expect):
    got, want = tree["large"]
    expect.num(got, want, "a large child is still visible through the parent")


def test_the_filtered_rows_match_the_heap(tree, expect):
    got, want = tree["filtered"]
    expect.row_set(got, want, "filtered rows match the heap")


def test_ungrouped_vector_aggregation_sees_the_child_rows(tree, expect):
    got, want = tree["vector"]
    expect.num(got, want, "ungrouped vector aggregation also sees the child rows")


def test_a_partitioned_parent_returns_every_row(tree, expect):
    expect.num(tree["partitioned"], 2000, "a partitioned parent still returns every row")


def test_the_partition_is_still_a_pgcolumnar_scan(tree, expect):
    expect.num(tree["partition_scans"], 1, "and the partition is still a PgColumnarScan (#436)")


def test_a_partitioned_parent_is_refused_and_its_leaf_is_not(pgc_conn, pgc_cluster, expect):
    conn = pgc_conn
    vnum = _one(conn, "SHOW server_version_num")
    expect.text("a number" if vnum.isdigit() else f"not a number: {vnum!r}", "a number",
                "premise: the server major is known, so the PG17 floor can be applied")
    major = int(vnum) // 10000
    if major < 17:
        # PostgreSQL 15 and 16 refuse PARTITION BY ... USING outright, so the fixture
        # cannot exist and every "is refused" arm would pass on a missing relation.
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"a partitioned table cannot carry an access method before PostgreSQL 17; this server is {major}",
                          name="get_storage_id on the parent is refused rather than reading storage")
        return
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE inh_pp (a int, b text) PARTITION BY RANGE (a) USING pgcolumnar")
        cur.execute("CREATE TABLE inh_pp1 PARTITION OF inh_pp FOR VALUES FROM (0) TO (100)")
        cur.execute("INSERT INTO inh_pp SELECT g, 'x' || g FROM generate_series(0, 99) g")
    expect.text(_one(conn, "SELECT am.amname::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                           "WHERE c.oid = 'inh_pp'::regclass"), "pgcolumnar",
                "premise: the parent carries the columnar access method")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'inh_pp'::regclass"), "p",
                "premise: and it is partitioned")
    expect.num(_one(conn, "SELECT relfilenode::int FROM pg_class WHERE oid = 'inh_pp'::regclass"), 0,
               "premise: so it has no storage of its own")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'inh_pp1'::regclass"), "r",
                "premise: while its leaf is an ordinary relation")

    crashes = _crashes(pgc_cluster)

    def up():
        alive = _sqlstate(conn, "SELECT 1") == "accepted"
        crashed = _since(pgc_cluster, crashes)
        if not isinstance(crashed, int):
            return crashed
        return "up" if alive and crashed == 0 else f"DOWN ({crashed} backend crashes)"

    expect.text(_sqlstate(conn, "SELECT pgcolumnar.get_storage_id('inh_pp'::regclass)"), "42809",
                "get_storage_id on the parent is refused rather than reading storage")
    expect.text(up(), "up", "and the cluster is still up after it")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.add_projection('inh_pp'::regclass, 'p1', ARRAY['a'])"),
                "42809", "add_projection on the parent is refused rather than aborting the backend")
    expect.text(up(), "up", "and the cluster is still up after that too")

    # Without these a function that refuses everything passes the arms above.
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.get_storage_id('inh_pp1'::regclass)"), "accepted",
                "control: get_storage_id on the LEAF still returns an id")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.add_projection('inh_pp1'::regclass, 'p1', ARRAY['a'])"),
                "accepted", "control: add_projection on the LEAF still works")


def test_a_columnar_materialized_view_is_still_columnar(pgc_conn, expect):
    """The arm against the obvious wrong fix: relkind == RELKIND_RELATION would exclude
    a matview too, and a matview can be columnar. RELKIND_HAS_STORAGE admits it."""
    conn = pgc_conn
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE inh_src (a int)")
        cur.execute("INSERT INTO inh_src SELECT generate_series(1, 10)")
        cur.execute("CREATE MATERIALIZED VIEW inh_mv USING pgcolumnar AS SELECT * FROM inh_src")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'inh_mv'::regclass"), "m",
                "premise: a materialized view is relkind m")
    expect.text(_one(conn, "SELECT am.amname::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                           "WHERE c.oid = 'inh_mv'::regclass"), "pgcolumnar",
                "premise: and it carries the columnar access method")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.get_storage_id('inh_mv'::regclass)"), "accepted",
                "a columnar materialized view is still recognised as columnar")
    expect.num(_one(conn, "SELECT count(*) FROM inh_mv"), 10, "and it still returns its rows")

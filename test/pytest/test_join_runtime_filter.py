"""Pytest twin of native_join_runtime_filter.sh."""
import re


def _has_runtime_filter(c):
    with c.cursor() as x:
        x.execute(
            "SELECT current_setting($g$pgcolumnar.enable_join_runtime_filter$g$, true)"
        )
        return x.fetchone()[0] is not None


def _plan(c, sql, on=None):
    with c.cursor() as x:
        if on is not None and _has_runtime_filter(c):
            x.execute(
                "SET pgcolumnar.enable_join_runtime_filter="
                + ("on" if on else "off")
            )
        x.execute("SET max_parallel_workers_per_gather=0")
        x.execute("SET enable_nestloop=off")
        x.execute("SET enable_mergejoin=off")
        x.execute("EXPLAIN(ANALYZE,TIMING off,SUMMARY off)" + sql)
        return "\n".join(r[0] for r in x.fetchall())


def _v(p, n):
    m = re.search(re.escape(n) + r": ([0-9]+)", p)
    return int(m.group(1)) if m else -1


def test_join_runtime_filter_defaults_on(pgc_conn, expect):
    """A serial inner Hash Join wraps without SET once the GUC defaults on (#752).

    Public seam: SHOW and EXPLAIN. Independently of native_join_runtime_filter.sh,
    this session does not SET the GUC. Clustered skip is already measured elsewhere.
    """
    with pgc_conn.cursor() as c:
        c.execute("SHOW pgcolumnar.enable_join_runtime_filter")
        got = c.fetchone()[0]
    expect.text(got, "on", "join runtime filter defaults on")
    with pgc_conn.cursor() as c:
        c.execute(
            """
            CREATE TABLE dim_def(k int);
            INSERT INTO dim_def VALUES (1);
            CREATE TABLE fact_def(k int) USING pgcolumnar;
            INSERT INTO fact_def VALUES (1), (2);
            ANALYZE dim_def;
            ANALYZE fact_def
            """
        )
    sql = "SELECT count(*) FROM fact_def JOIN dim_def ON fact_def.k = dim_def.k"
    plan = _plan(pgc_conn, sql)
    expect.num(plan.count("Hash Join"), 1, "default plan retains core Hash Join")
    expect.num(
        plan.count("Columnar Runtime Filter Coordinator"),
        1,
        "default plan has runtime coordinator",
    )


def test_serial_join_runtime_filter(pgc_conn, expect):
    with pgc_conn.cursor() as c:
        c.execute(
            """
            CREATE TABLE d(k int);
            INSERT INTO d SELECT g FROM generate_series(8001,8200) g;
            INSERT INTO d VALUES(8100),(NULL);
            CREATE TABLE f(k int,p text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$f$t$, stripe_row_limit => 1000);
            INSERT INTO f SELECT g, repeat(md5(g::text), 8)
            FROM generate_series(1,20000) g;
            CREATE TABLE h AS SELECT * FROM f;
            ANALYZE d;
            ANALYZE f
            """
        )
    sql = "SELECT count(*),sum(f.k),sum(length(f.p))FROM f JOIN d ON f.k=d.k"
    b = _plan(pgc_conn, sql, False)
    p = _plan(pgc_conn, sql, True)
    expect.num(b.count("Hash Join"), 1, "baseline core Hash Join")
    expect.num(_v(b, "Columnar Chunk Groups Read"), 20, "baseline reads all groups")
    expect.num(p.count("Columnar Runtime Filter Coordinator"), 1, "plan has runtime coordinator")
    expect.num(p.count("Columnar Runtime Filter Build Tap"), 1, "plan has build tap")
    expect.num(p.count("Hash Join"), 1, "plan retains core Hash Join")
    expect.num(_v(p, "Runtime Filter Build Rows"), 201, "build rows omit NULL")
    expect.num(p.count("Runtime Filter Ready: true"), 1, "filter ready before scan")
    expect.num(_v(p, "Runtime Filter Groups Removed"), 19, "clustered groups removed")
    expect.num(_v(p, "Columnar Chunk Groups Read"), 1, "clustered reads fewer groups")
    with pgc_conn.cursor() as c:
        if _has_runtime_filter(pgc_conn):
            c.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        c.execute(sql)
        a = c.fetchone()
        if _has_runtime_filter(pgc_conn):
            c.execute("SET pgcolumnar.enable_join_runtime_filter=off")
        c.execute(sql)
        o = c.fetchone()
        c.execute(
            """
            SELECT count(*),sum(h.k),sum(length(h.p))
            FROM h JOIN d ON h.k=d.k
            """
        )
        h = c.fetchone()
    expect.rows([a], [o], "runtime answer equals off")
    expect.rows([a], [h], "runtime answer equals heap")
    for name, s in [
        ("LEFT refusal", "SELECT count(*)FROM f LEFT JOIN d ON f.k=d.k"),
        ("SEMI refusal", "SELECT count(*)FROM f WHERE EXISTS(SELECT 1 FROM d WHERE d.k=f.k)"),
        ("ANTI refusal", "SELECT count(*)FROM f WHERE NOT EXISTS(SELECT 1 FROM d WHERE d.k=f.k)"),
        ("CROSS refusal", "SELECT count(*)FROM f CROSS JOIN d"),
    ]:
        expect.num(
            _plan(pgc_conn, s, True).count("Columnar Runtime Filter Coordinator"),
            0,
            name,
        )


def _json_nodes(plan):
    if isinstance(plan, list):
        for item in plan:
            yield from _json_nodes(item)
        return
    if not isinstance(plan, dict):
        return
    yield plan
    child = plan.get("Plan")
    if isinstance(child, dict):
        yield from _json_nodes(child)
    for child in plan.get("Plans") or []:
        yield from _json_nodes(child)


def _json_field(plan, key):
    for node in _json_nodes(plan):
        if key in node:
            return node[key]
    return None


def test_scattered_join_runtime_bloom(pgc_conn, expect):
    """Scattered keys keep every group; Bloom must reject non-matching rows."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dimb(k int);
            INSERT INTO dimb SELECT 80 + 100 * g FROM generate_series(0, 199) g;
            CREATE TABLE factb(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$factb$t$, stripe_row_limit => 1000);
            INSERT INTO factb SELECT g, repeat(md5(g::text), 8)
            FROM generate_series(1, 20000) g;
            CREATE TABLE heapb AS SELECT * FROM factb;
            ANALYZE dimb;
            ANALYZE factb
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        sql = """
            SELECT count(*), sum(factb.k), sum(length(factb.payload))
            FROM factb JOIN dimb ON factb.k = dimb.k
        """
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), sum(heapb.k), sum(length(heapb.payload))
            FROM heapb JOIN dimb ON heapb.k = dimb.k
            """
        )
        heap = cur.fetchone()

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="scattered plan uses the runtime coordinator",
    )
    expect.num(
        _json_field(plan, "Runtime Filter Groups Removed"),
        0,
        "scattered hull cannot drop a group",
    )
    expect.num(
        _json_field(plan, "Columnar Chunk Groups Read"),
        20,
        "scattered still reads every group",
    )
    expect.plan_marker(
        plan,
        "Runtime Filter Rows Rejected",
        name="scattered plan reports dedicated bloom rejects",
    )
    expect.at_least(
        _json_field(plan, "Runtime Filter Rows Rejected"),
        15000,
        "scattered bloom rejects most non-matches",
    )
    expect.rows([got], [heap], "scattered answer equals heap")


def _removed_or_zero(plan):
    removed = _json_field(plan, "Runtime Filter Groups Removed")
    return 0 if removed is None else removed


def test_cross_type_int4_int8_bloom(pgc_conn, expect):
    """int4 fact vs int8 dim: hash both sides, do not claim an interval."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dim8(k bigint);
            INSERT INTO dim8 SELECT g FROM generate_series(7900, 8099) g;
            CREATE TABLE fact4(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$fact4$t$, stripe_row_limit => 1000);
            INSERT INTO fact4 SELECT g, repeat(md5(g::text), 8)
            FROM generate_series(1, 20000) g;
            CREATE TABLE heap4 AS SELECT * FROM fact4;
            ANALYZE dim8;
            ANALYZE fact4
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        sql = """
            SELECT count(*), sum(fact4.k), sum(length(fact4.payload))
            FROM fact4 JOIN dim8 ON fact4.k = dim8.k
        """
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), sum(heap4.k), sum(length(heap4.payload))
            FROM heap4 JOIN dim8 ON heap4.k = dim8.k
            """
        )
        heap = cur.fetchone()

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="int4/int8 plan uses the runtime coordinator",
    )
    expect.num(
        _removed_or_zero(plan),
        0,
        "int4/int8 interval stays off",
    )
    expect.at_least(
        _json_field(plan, "Runtime Filter Rows Rejected") or 0,
        15000,
        "int4/int8 bloom rejects most non-matches",
    )
    expect.rows([got], [heap], "int4/int8 answer equals heap")


def test_collation_mismatch_bloom_only(pgc_conn, expect):
    """Mismatched operator collation must not order; Bloom may still reject."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dimc(k text COLLATE "C");
            INSERT INTO dimc SELECT (15600 + g)::text FROM generate_series(0, 199) g;
            CREATE TABLE factc(k text COLLATE "C", payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$factc$t$, stripe_row_limit => 1000);
            INSERT INTO factc SELECT g::text, repeat(md5(g::text), 8)
            FROM generate_series(1, 20000) g;
            CREATE TABLE heapc(k text COLLATE "C", payload text);
            INSERT INTO heapc SELECT g::text, repeat(md5(g::text), 8)
            FROM generate_series(1, 20000) g;
            ANALYZE dimc;
            ANALYZE factc
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        # A FIXTURE THE INTERVAL COULD PRUNE (#1339): 200 keys "15600".."15799" leave
        # every group whose keys start with 2-9 outside the C-order hull, and the
        # matching-collation control proves an interval removes them. The old keys
        # spanned "10060".."9960", which overlaps every group, so the "stays off" arm
        # passed with the collation gate removed. Two hundred keys, because under ten
        # build rows no Bloom filter is built and the Bloom arm would measure nothing.
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) "
            "SELECT count(*) FROM factc JOIN dimc ON factc.k = dimc.k"
        )
        matched_plan = cur.fetchone()[0]
        sql = """
            SELECT count(*), sum(length(factc.k)), sum(length(factc.payload))
            FROM factc JOIN dimc ON factc.k = dimc.k COLLATE "POSIX"
        """
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), sum(length(heapc.k)), sum(length(heapc.payload))
            FROM heapc JOIN dimc ON heapc.k = dimc.k COLLATE "POSIX"
            """
        )
        heap = cur.fetchone()

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="collation-mismatch plan uses the runtime coordinator",
    )
    expect.at_least(
        _removed_or_zero(matched_plan),
        1,
        "with matching collations the same fixture's interval removes row groups",
    )
    expect.num(
        _removed_or_zero(plan),
        0,
        "collation-mismatch interval stays off",
    )
    expect.at_least(
        _json_field(plan, "Runtime Filter Rows Rejected") or 0,
        15000,
        "collation-mismatch bloom rejects most non-matches",
    )
    expect.rows([got], [heap], "collation-mismatch answer equals heap")


def test_runtime_filter_rebuilds_on_lateral_rescan(pgc_conn, expect):
    """A correlated LATERAL must rebuild the filter for each outer parameter."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dimr(k int);
            INSERT INTO dimr SELECT 90 + 100 * g FROM generate_series(0, 199) g;
            CREATE TABLE factr(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$factr$t$, stripe_row_limit => 1000);
            INSERT INTO factr SELECT g, repeat(md5(g::text), 8)
            FROM generate_series(1, 20000) g;
            CREATE TABLE heapr AS SELECT * FROM factr;
            ANALYZE dimr;
            ANALYZE factr
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        sql = """
            SELECT v.x, s.c
            FROM (VALUES (10),(7000)) v(x)
            CROSS JOIN LATERAL (
              SELECT count(*) c
              FROM factr JOIN dimr ON factr.k = dimr.k
              WHERE dimr.k > v.x
            ) s
            ORDER BY 1
        """
        cur.execute(sql)
        got = cur.fetchall()
        cur.execute(
            """
            SELECT v.x, s.c
            FROM (VALUES (10),(7000)) v(x)
            CROSS JOIN LATERAL (
              SELECT count(*) c
              FROM heapr JOIN dimr ON heapr.k = dimr.k
              WHERE dimr.k > v.x
            ) s
            ORDER BY 1
            """
        )
        heap = cur.fetchall()
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="lateral rescan still uses the runtime coordinator",
    )
    expect.rows(got, heap, "lateral rescan answers equal heap")


def test_saturated_build_disables_bloom(pgc_conn, expect):
    """A build side past the #467 cap must disable Bloom rather than saturate."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dims(k int);
            INSERT INTO dims SELECT g FROM generate_series(1, 225000) g;
            CREATE TABLE facts(k int) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$facts$t$, stripe_row_limit => 1000);
            INSERT INTO facts SELECT g FROM generate_series(1, 310000) g;
            CREATE TABLE heaps AS SELECT * FROM facts;
            ANALYZE dims;
            ANALYZE facts
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        sql = """
            SELECT count(*), sum(facts.k)
            FROM facts JOIN dims ON facts.k = dims.k
        """
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), sum(heaps.k)
            FROM heaps JOIN dims ON heaps.k = dims.k
            """
        )
        heap = cur.fetchone()

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="saturated plan uses the runtime coordinator",
    )
    bloom = _json_field(plan, "Runtime Filter Bloom")
    expect.num(
        1 if bloom is False else 0,
        1,
        "saturated bloom is disabled",
    )
    expect.rows([got], [heap], "saturated answer equals heap")


def _relation_names(plan):
    names = []
    for node in _json_nodes(plan):
        name = node.get("Relation Name")
        if name:
            names.append(name)
    return names


def test_three_table_join_order_unchanged(pgc_conn, expect):
    """Wrapping the columnar-outer hash join must not reorder the other inputs."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE a3(k int);
            INSERT INTO a3 SELECT g FROM generate_series(1, 150) g;
            CREATE TABLE b3(k int, extra int);
            INSERT INTO b3 SELECT g, g + 3 FROM generate_series(1, 150) g;
            CREATE TABLE f3(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$f3$t$, stripe_row_limit => 1000);
            INSERT INTO f3 SELECT g, repeat(md5(g::text), 4)
            FROM generate_series(1, 4000) g;
            ANALYZE a3;
            ANALYZE b3;
            ANALYZE f3
            """
        )
        sql = """
            SELECT count(*), sum(f3.k), sum(b3.extra)
            FROM f3 JOIN a3 ON f3.k = a3.k JOIN b3 ON a3.k = b3.k
        """
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("EXPLAIN (FORMAT JSON, COSTS OFF) " + sql)
        on_plan = cur.fetchone()[0]
        cur.execute(sql)
        on_rows = cur.fetchone()
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=off")
        cur.execute("EXPLAIN (FORMAT JSON, COSTS OFF) " + sql)
        off_plan = cur.fetchone()[0]
        cur.execute(sql)
        off_rows = cur.fetchone()

    expect.plan_node(
        on_plan,
        provider="Columnar Runtime Filter Coordinator",
        name="3-table plan uses the runtime coordinator",
    )
    expect.rows(
        _relation_names(on_plan),
        _relation_names(off_plan),
        "3-table relation order matches filter-off",
    )
    expect.rows([on_rows], [off_rows], "3-table answer matches filter-off")


def test_empty_build_runtime_filter(pgc_conn, expect):
    """An empty dimension still uses the coordinator and returns no join rows."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dim0(k int);
            CREATE TABLE factz(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$factz$t$, stripe_row_limit => 1000);
            INSERT INTO factz SELECT g, repeat(md5(g::text), 4)
            FROM generate_series(1, 8000) g;
            CREATE TABLE heap0 AS SELECT * FROM factz;
            ANALYZE dim0;
            ANALYZE factz
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        sql = """
            SELECT count(*), sum(factz.k)
            FROM factz JOIN dim0 ON factz.k = dim0.k
        """
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), sum(heap0.k)
            FROM heap0 JOIN dim0 ON heap0.k = dim0.k
            """
        )
        heap = cur.fetchone()

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="empty-build plan uses the runtime coordinator",
    )
    expect.rows([got], [heap], "empty-build answer equals heap")


def test_projection_outer_is_not_wrapped(pgc_conn, expect):
    """A covering projection scan must stay an unwrapped Hash Join outer."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dim_pr(k int);
            INSERT INTO dim_pr SELECT g FROM generate_series(10, 180) g;
            CREATE TABLE factp(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$factp$t$, stripe_row_limit => 1000);
            INSERT INTO factp SELECT g, repeat(md5(g::text), 4)
            FROM generate_series(1, 6000) g ORDER BY md5((g + 9)::text);
            SELECT pgcolumnar.add_projection(
                $t$factp$t$, $n$pk$n$, ARRAY['k','payload'], ARRAY['k']);
            CREATE TABLE heapp AS SELECT * FROM factp;
            ANALYZE dim_pr;
            ANALYZE factp
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        sql = """
            SELECT count(*), sum(factp.k)
            FROM factp JOIN dim_pr ON factp.k = dim_pr.k
            WHERE factp.k BETWEEN 10 AND 180
        """
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), sum(heapp.k)
            FROM heapp JOIN dim_pr ON heapp.k = dim_pr.k
            WHERE heapp.k BETWEEN 10 AND 180
            """
        )
        heap = cur.fetchone()

    expect.plan_marker(
        plan,
        "Columnar Projection",
        name="projection scan is chosen",
    )
    expect.num(
        sum(
            1
            for n in _json_nodes(plan)
            if n.get("Custom Plan Provider")
            == "Columnar Runtime Filter Coordinator"
        ),
        0,
        "projection outer is not wrapped",
    )
    expect.rows([got], [heap], "projection join equals heap")


def test_early_limit_matches_heap(pgc_conn, expect):
    """LIMIT must shut the coordinator down and still match a heap control."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE diml(k int);
            INSERT INTO diml SELECT g FROM generate_series(400, 500) g;
            CREATE TABLE factl(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$factl$t$, stripe_row_limit => 1000);
            INSERT INTO factl SELECT g, repeat(md5(g::text), 4)
            FROM generate_series(1, 3000) g;
            CREATE TABLE heapl AS SELECT * FROM factl;
            ANALYZE diml;
            ANALYZE factl
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        sql = """
            SELECT factl.k
            FROM factl JOIN diml ON factl.k = diml.k
            ORDER BY factl.k
            LIMIT 7
        """
        cur.execute(sql)
        got = cur.fetchall()
        cur.execute(
            """
            SELECT heapl.k
            FROM heapl JOIN diml ON heapl.k = diml.k
            ORDER BY heapl.k
            LIMIT 7
            """
        )
        heap = cur.fetchall()
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="early limit still uses the runtime coordinator",
    )
    expect.rows(got, heap, "early limit equals heap")

def test_fact_qual_with_late_mat_off_matches_heap(pgc_conn, expect):
    """A non-key fact qual with late materialization off must still match heap."""
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dimq(k int);
            INSERT INTO dimq SELECT g FROM generate_series(9100, 9249) g;
            CREATE TABLE factq(k int, n int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$factq$t$, stripe_row_limit => 1000);
            INSERT INTO factq SELECT g, g, repeat(md5(g::text), 3)
            FROM generate_series(1, 16000) g;
            CREATE TABLE heapq AS SELECT * FROM factq;
            ANALYZE dimq;
            ANALYZE factq
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET pgcolumnar.enable_late_materialization=off")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET enable_nestloop=off")
        cur.execute("SET enable_mergejoin=off")
        sql = """
            SELECT count(*), sum(factq.k), sum(factq.n)
            FROM factq JOIN dimq ON factq.k = dimq.k
            WHERE factq.n > 80
        """
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql
        )
        plan = cur.fetchone()[0]
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), sum(heapq.k), sum(heapq.n)
            FROM heapq JOIN dimq ON heapq.k = dimq.k
            WHERE heapq.n > 80
            """
        )
        heap = cur.fetchone()

    expect.plan_node(
        plan,
        provider="Columnar Runtime Filter Coordinator",
        name="late-mat-off fact qual still uses the runtime coordinator",
    )
    expect.rows([got], [heap], "late-mat-off fact qual equals heap")


def test_a_gating_qual_over_the_join_matches_heap(pgc_conn, expect):
    """A WHERE that names no column puts a gating Result over the join (#1437).

    The coordinator expected its child to be the Hash Join, so the Result made
    every such query fail, EXPLAIN included: an ERROR on a release build, and a
    failed cast assertion that aborted the backend on an assert build. A true
    gate must give heap's rows and a false one none, over a heap and a columnar
    dimension. The premises show the same join with no gate takes the
    coordinator. Independently of native_join_runtime_filter.sh: different
    names, key range and row count.
    """
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dgh(k int);
            INSERT INTO dgh SELECT g FROM generate_series(6400, 6549) g;
            CREATE TABLE dgc(k int) USING pgcolumnar;
            INSERT INTO dgc SELECT g FROM generate_series(6400, 6549) g;
            CREATE TABLE fg(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$fg$t$, stripe_row_limit => 1000);
            INSERT INTO fg SELECT g, repeat(md5(g::text), 3)
            FROM generate_series(1, 13000) g;
            CREATE TABLE hg AS SELECT * FROM fg;
            ANALYZE dgh;
            ANALYZE dgc;
            ANALYZE fg
            """
        )
    for dim, kind in (("dgh", "heap"), ("dgc", "columnar")):
        sql = (f"SELECT count(*), sum(fg.k), sum(length(fg.payload)) "
               f"FROM fg JOIN {dim} ON fg.k = {dim}.k")
        expect.num(
            _plan(pgc_conn, sql, on=True).count("Columnar Runtime Filter Coordinator"), 1,
            f"premise: with no gate, the {kind}-dimension join has the coordinator")
        with pgc_conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM hg JOIN {dim} ON hg.k = {dim}.k WHERE (SELECT true)")
            expect.num(cur.fetchone()[0], 150,
                       f"premise: heap's answer under a true gate over a {kind} dimension holds 150 rows")
        for gate in ("false", "true"):
            with pgc_conn.cursor() as cur:
                cur.execute(f"{sql} WHERE (SELECT {gate})")
                got = cur.fetchone()
                cur.execute(
                    f"SELECT count(*), sum(hg.k), sum(length(hg.payload)) "
                    f"FROM hg JOIN {dim} ON hg.k = {dim}.k WHERE (SELECT {gate})")
                heap = cur.fetchone()
            expect.rows([got], [heap],
                        f"a (SELECT {gate}) gate over a {kind} dimension equals heap")


def test_an_empty_build_reads_no_fact_group(pgc_conn, expect):
    """An empty build from a large dimension filtered to nothing reads no fact group (#1447).

    With a large dimension the hash join fetches its first outer row before it builds,
    so the fact scan runs. The coordinator has found the build empty, and an inner join
    with an empty build has no rows, so the fact table must not be read. It was read
    whole, every fact row rejected one at a time. Independently of
    native_join_runtime_filter.sh: different names, sizes and filter.
    """
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dbig(k int, w int);
            INSERT INTO dbig SELECT g, -g FROM generate_series(1, 150000) g;
            CREATE TABLE fz(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$fz$t$, stripe_row_limit => 1000);
            INSERT INTO fz SELECT g, repeat(md5(g::text), 3) FROM generate_series(1, 6000) g;
            CREATE TABLE hz(k int, payload text);
            INSERT INTO hz SELECT g, repeat(md5(g::text), 3) FROM generate_series(1, 6000) g;
            ANALYZE dbig;
            ANALYZE fz
            """
        )
    sql = ("SELECT count(*), sum(fz.k) FROM fz JOIN dbig ON fz.k = dbig.k "
           "WHERE dbig.w > 0")
    plan = _plan(pgc_conn, sql, on=True)
    expect.num(plan.count("Columnar Runtime Filter Coordinator"), 1,
               "premise: the large-dimension empty build runs under the coordinator")
    expect.num(_v(plan, "Runtime Filter Build Rows"), 0, "premise: and its build is empty")
    expect.num(_v(plan, "Columnar Chunk Groups Read"), 0, "an empty build reads no fact chunk group")
    with pgc_conn.cursor() as cur:
        cur.execute(sql)
        got = cur.fetchone()
        cur.execute("SELECT count(*), sum(hz.k) FROM hz JOIN dbig ON hz.k = dbig.k WHERE dbig.w > 0")
        heap = cur.fetchone()
    expect.rows([got], [heap], "an empty build from a large dimension answers as heap")


def test_an_empty_build_on_one_rescan_does_not_empty_the_next(pgc_conn, expect):
    """A LATERAL whose first outer row builds nothing and second builds every key (#1447).

    An empty build now ends the join at once, so state left over from the first
    execution would answer the second with no rows. Independently of
    native_join_runtime_filter.sh: different names and keys.
    """
    with pgc_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE dre(k int);
            INSERT INTO dre SELECT 40 + 100 * g FROM generate_series(0, 149) g;
            CREATE TABLE fre(k int, payload text) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$fre$t$, stripe_row_limit => 1000);
            INSERT INTO fre SELECT g, repeat(md5(g::text), 4) FROM generate_series(1, 16000) g;
            CREATE TABLE hre(k int, payload text);
            INSERT INTO hre SELECT g, repeat(md5(g::text), 4) FROM generate_series(1, 16000) g;
            ANALYZE dre;
            ANALYZE fre
            """
        )
        if _has_runtime_filter(pgc_conn):
            cur.execute("SET pgcolumnar.enable_join_runtime_filter=on")
        cur.execute("SET max_parallel_workers_per_gather=0")
        lateral = """
            SELECT v.x, s.c FROM (VALUES (88888),(0)) v(x)
            CROSS JOIN LATERAL (
              SELECT count(*) c FROM {t} JOIN dre ON {t}.k = dre.k WHERE dre.k > v.x
            ) s ORDER BY 1
        """
        cur.execute("EXPLAIN (COSTS OFF) " + lateral.format(t="fre"))
        plan = "\n".join(r[0] for r in cur.fetchall())
        cur.execute(lateral.format(t="fre"))
        got = cur.fetchall()
        cur.execute(lateral.format(t="hre"))
        heap = cur.fetchall()
    expect.num(plan.count("Columnar Runtime Filter Coordinator"), 1,
               "premise: the empty-then-full rescan runs under the coordinator")
    expect.text(f"{heap[0][0]}|{heap[0][1]}", "0|150",
                "premise: heap's empty-then-full rescan answer holds the 150 keys")
    expect.rows(got, heap, "an empty build on one rescan does not empty the next")

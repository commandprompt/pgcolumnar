"""Ungrouped vectorized aggregate over a unique-key inner join (#752).

Public seams: EXPLAIN marker ``Columnar Vectorized Aggregates`` and the SQL
answer. Independently of native_join_vector_agg.sh, this session builds its
own fact, unique dimension, duplicate dimension, and heap twins.
"""


def _exec(cur, sql):
    cur.execute(sql)
    if cur.description is None:
        return None
    return cur.fetchall()


def _plan_and_value(cur, sql):
    cur.execute("SET max_parallel_workers_per_gather=0")
    cur.execute("SET enable_nestloop=off")
    cur.execute("SET enable_mergejoin=off")
    cur.execute("EXPLAIN (COSTS OFF) " + sql)
    plan = "\n".join(r[0] for r in cur.fetchall())
    cur.execute(sql)
    return plan, cur.fetchone()


def test_unique_join_keeps_the_ungrouped_fold(pgc_conn, expect):
    """A unique-key inner join is a fact-table filter, so the fold can run.

    Public seam: EXPLAIN and the SQL answer. The GUC-off plan is core Agg.
    """
    with pgc_conn.cursor() as c:
        _exec(
            c,
            """
            CREATE TABLE dim_u(k int PRIMARY KEY);
            INSERT INTO dim_u SELECT g FROM generate_series(1,25) g;
            CREATE TABLE fact_u(k int, m float8) USING pgcolumnar;
            SELECT pgcolumnar.set_options($t$fact_u$t$, stripe_row_limit => 1000);
            INSERT INTO fact_u SELECT 1 + (g % 50), (g % 17)::float8
              FROM generate_series(1,5000) g;
            CREATE TABLE heap_u (k int, m float8) USING heap;
            INSERT INTO heap_u SELECT * FROM fact_u;
            ANALYZE dim_u;
            ANALYZE fact_u
            """,
        )
        sql = (
            "SELECT count(*), coalesce(sum(fact_u.m)::text,'z') "
            "FROM fact_u JOIN dim_u ON fact_u.k = dim_u.k"
        )
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=on")
        plan_on, on = _plan_and_value(c, sql)
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=off")
        plan_off, off = _plan_and_value(c, sql)
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=on")
        c.execute(
            "SELECT count(*), coalesce(sum(heap_u.m)::text,'z') "
            "FROM heap_u JOIN dim_u ON heap_u.k = dim_u.k"
        )
        heap = c.fetchone()
    expect.num(plan_on.count("Columnar Vectorized Aggregates"), 1,
               "unique join uses vectorized agg when GUC on")
    expect.num(plan_off.count("Columnar Vectorized Aggregates"), 0,
               "unique join uses core Agg when GUC off")
    expect.rows([on], [off], "unique join fold answer equals GUC off")
    expect.rows([on], [heap], "unique join fold answer equals heap")


def test_duplicate_dim_keys_refuse_the_join_fold(pgc_conn, expect):
    """Duplicate dimension keys would multiply fact rows. The fold must refuse.

    Public seam: EXPLAIN has no vectorized agg node, and the answer still
    matches a heap twin of the same join.
    """
    with pgc_conn.cursor() as c:
        _exec(
            c,
            """
            CREATE TABLE dim_d(k int);
            INSERT INTO dim_d SELECT g FROM generate_series(1,50) g;
            INSERT INTO dim_d SELECT g FROM generate_series(1,50) g;
            CREATE TABLE fact_d(k int, m float8) USING pgcolumnar;
            INSERT INTO fact_d SELECT 1 + (g % 50), 1::float8
              FROM generate_series(1,100) g;
            CREATE TABLE heap_d (k int, m float8) USING heap;
            INSERT INTO heap_d SELECT * FROM fact_d;
            ANALYZE dim_d;
            ANALYZE fact_d
            """,
        )
        sql = (
            "SELECT count(*), coalesce(sum(fact_d.m)::text,'z') "
            "FROM fact_d JOIN dim_d ON fact_d.k = dim_d.k"
        )
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=on")
        plan, got = _plan_and_value(c, sql)
        c.execute(
            "SELECT count(*), coalesce(sum(heap_d.m)::text,'z') "
            "FROM heap_d JOIN dim_d ON heap_d.k = dim_d.k"
        )
        heap = c.fetchone()
    expect.num(plan.count("Columnar Vectorized Aggregates"), 0,
               "duplicate dim keys refuse the join fold")
    expect.rows([got], [heap], "duplicate dim keys answer equals heap")


def test_left_join_refuses_the_join_fold(pgc_conn, expect):
    """A LEFT join is not a fact-table filter. Core Agg stays in charge."""
    with pgc_conn.cursor() as c:
        _exec(
            c,
            """
            CREATE TABLE dim_l(k int PRIMARY KEY);
            INSERT INTO dim_l VALUES (1);
            CREATE TABLE fact_l(k int, m float8) USING pgcolumnar;
            INSERT INTO fact_l VALUES (1, 1.0), (2, 2.0);
            ANALYZE dim_l;
            ANALYZE fact_l
            """,
        )
        sql = (
            "SELECT count(*), coalesce(sum(dim_l.k)::text,'z') "
            "FROM fact_l LEFT JOIN dim_l ON fact_l.k = dim_l.k"
        )
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=on")
        plan, got = _plan_and_value(c, sql)
        c.execute(
            """
            CREATE TABLE heap_l (k int, m float8) USING heap;
            INSERT INTO heap_l VALUES (1, 1.0), (2, 2.0)
            """
        )
        c.execute(
            "SELECT count(*), coalesce(sum(dim_l.k)::text,'z') "
            "FROM heap_l LEFT JOIN dim_l ON heap_l.k = dim_l.k"
        )
        heap = c.fetchone()
    expect.num(plan.count("Columnar Vectorized Aggregates"), 0,
               "LEFT join refuses the join fold")
    expect.rows([got], [heap], "LEFT join answer equals heap")


def test_extra_join_filter_refuses_the_fold(pgc_conn, expect):
    """A Join Filter besides the hash clause is not a fact-table filter.

    Public seam: EXPLAIN has no vectorized agg node, and the sum matches a
    heap twin. Independently of native_join_vector_agg.sh.
    """
    with pgc_conn.cursor() as c:
        _exec(
            c,
            """
            CREATE TABLE dim_jf(k int PRIMARY KEY, t int);
            INSERT INTO dim_jf SELECT g, 12 FROM generate_series(1,30) g;
            CREATE TABLE fact_jf(k int, v int) USING pgcolumnar;
            INSERT INTO fact_jf SELECT 1 + (g % 30), g % 30
              FROM generate_series(1,1200) g;
            CREATE TABLE heap_jf (k int, v int) USING heap;
            INSERT INTO heap_jf SELECT * FROM fact_jf;
            ANALYZE dim_jf;
            ANALYZE fact_jf
            """,
        )
        sql = (
            "SELECT coalesce(sum(fact_jf.v)::text,'z') "
            "FROM fact_jf JOIN dim_jf "
            "ON fact_jf.k = dim_jf.k AND fact_jf.v > dim_jf.t"
        )
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=on")
        plan_on, on = _plan_and_value(c, sql)
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=off")
        _plan_off, off = _plan_and_value(c, sql)
        c.execute(
            "SELECT coalesce(sum(heap_jf.v)::text,'z') "
            "FROM heap_jf JOIN dim_jf "
            "ON heap_jf.k = dim_jf.k AND heap_jf.v > dim_jf.t"
        )
        heap = c.fetchone()
    expect.num(plan_on.count("Columnar Vectorized Aggregates"), 0,
               "extra join filter refuses the join fold")
    expect.rows([on], [off], "extra join filter answer equals GUC off")
    expect.rows([on], [heap], "extra join filter answer equals heap")


def test_inequality_join_filter_refuses_the_fold(pgc_conn, expect):
    """A non-equi join clause is the same kind of extra Join Filter."""
    with pgc_conn.cursor() as c:
        _exec(
            c,
            """
            CREATE TABLE dim_ne(k int PRIMARY KEY, t int);
            INSERT INTO dim_ne SELECT g, 4 FROM generate_series(1,8) g;
            CREATE TABLE fact_ne(k int, v int) USING pgcolumnar;
            INSERT INTO fact_ne SELECT 1 + (g % 8), g % 6
              FROM generate_series(1,240) g;
            CREATE TABLE heap_ne (k int, v int) USING heap;
            INSERT INTO heap_ne SELECT * FROM fact_ne;
            ANALYZE dim_ne;
            ANALYZE fact_ne
            """,
        )
        sql = (
            "SELECT coalesce(sum(fact_ne.v)::text,'z') "
            "FROM fact_ne JOIN dim_ne "
            "ON fact_ne.k = dim_ne.k AND fact_ne.v <> dim_ne.t"
        )
        c.execute("SET pgcolumnar.enable_ungrouped_vector_agg=on")
        plan, got = _plan_and_value(c, sql)
        c.execute(
            "SELECT coalesce(sum(heap_ne.v)::text,'z') "
            "FROM heap_ne JOIN dim_ne "
            "ON heap_ne.k = dim_ne.k AND heap_ne.v <> dim_ne.t"
        )
        heap = c.fetchone()
    expect.num(plan.count("Columnar Vectorized Aggregates"), 0,
               "inequality join filter refuses the join fold")
    expect.rows([got], [heap], "inequality join filter answer equals heap")

"""The ungrouped vectorized aggregate survives an inner join onto a UNIQUE dimension,
and refuses every join that is not a plain filter of the fact table (#752, #1341). The
pytest twin of `test/native_join_vector_agg.sh`, every check by the shell's name.

A star-schema inner Hash Join onto a unique dimension only filters the fact table, so
the fold can drain the dimension's keys and still answer exactly. A dimension with
duplicate keys would multiply fact rows, a LEFT join keeps unmatched ones, and a Join
Filter besides the hash clause is not a membership test: each must refuse the fold,
and every answer must equal the heap mirror's. Plans are read with parallel workers
off and nested-loop and merge joins disabled, so the join is a hash join, as in the
shell.
"""
import psycopg

GUC = "pgcolumnar.enable_ungrouped_vector_agg"
MARKER = "Columnar Vectorized Aggregates"


def _exec(conn, *sqls):
    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


def _row(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return "|".join(str(v) for v in cur.fetchone())


def _with(conn, guc, sql):
    """-> the plan's text for 'EXPLAIN ...', else the first row, under `guc`, hash joins only."""
    _exec(conn, "SET max_parallel_workers_per_gather = 0", "SET enable_nestloop = off",
          "SET enable_mergejoin = off", f"SET {GUC} = {guc}")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            if sql.startswith("EXPLAIN"):
                return "\n".join(r[0] for r in cur.fetchall())
            return "|".join(str(v) for v in cur.fetchone())
    finally:
        _exec(conn, "RESET max_parallel_workers_per_gather", "RESET enable_nestloop",
              "RESET enable_mergejoin", f"RESET {GUC}")


def _folds(conn, sql):
    return _with(conn, "on", f"EXPLAIN (COSTS OFF) {sql}").count(MARKER)


def test_the_join_fold(pgc_conn, expect):
    conn = pgc_conn
    _exec(conn, "CREATE TABLE dim_u(k int PRIMARY KEY)",
          "INSERT INTO dim_u SELECT g FROM generate_series(1,25) g",
          "CREATE TABLE fact_u(k int, m float8) USING pgcolumnar",
          "SELECT pgcolumnar.set_options('fact_u', stripe_row_limit => 1000)",
          "INSERT INTO fact_u SELECT 1 + (g % 50), (g % 17)::float8 FROM generate_series(1,5000) g",
          "CREATE TABLE heap_u (k int, m float8) USING heap", "INSERT INTO heap_u SELECT * FROM fact_u",
          "ANALYZE dim_u", "ANALYZE fact_u")
    sqlu = "SELECT count(*), coalesce(sum(fact_u.m)::text,'z') FROM fact_u JOIN dim_u ON fact_u.k = dim_u.k"
    expect.num(_folds(conn, sqlu), 1, "unique join uses vectorized agg when GUC on")
    expect.num(_with(conn, "off", f"EXPLAIN (COSTS OFF) {sqlu}").count(MARKER), 0, "unique join uses core Agg when GUC off")
    expect.text(_with(conn, "on", sqlu), _with(conn, "off", sqlu), "unique join fold answer equals GUC off")
    expect.text(_with(conn, "on", sqlu), _row(conn, "SELECT count(*), coalesce(sum(heap_u.m)::text,'z') "
                                                    "FROM heap_u JOIN dim_u ON heap_u.k = dim_u.k"),
                "unique join fold answer equals heap")

    _exec(conn, "CREATE TABLE dim_d(k int)", "INSERT INTO dim_d SELECT g FROM generate_series(1,50) g",
          "INSERT INTO dim_d SELECT g FROM generate_series(1,50) g",
          "CREATE TABLE fact_d(k int, m float8) USING pgcolumnar",
          "INSERT INTO fact_d SELECT 1 + (g % 50), 1::float8 FROM generate_series(1,100) g",
          "CREATE TABLE heap_d (k int, m float8) USING heap", "INSERT INTO heap_d SELECT * FROM fact_d",
          "ANALYZE dim_d", "ANALYZE fact_d")
    sqld = "SELECT count(*), coalesce(sum(fact_d.m)::text,'z') FROM fact_d JOIN dim_d ON fact_d.k = dim_d.k"
    expect.num(_folds(conn, sqld), 0, "duplicate dim keys refuse the join fold")
    expect.text(_with(conn, "on", sqld), _row(conn, "SELECT count(*), coalesce(sum(heap_d.m)::text,'z') "
                                                    "FROM heap_d JOIN dim_d ON heap_d.k = dim_d.k"),
                "duplicate dim keys answer equals heap")

    _exec(conn, "CREATE TABLE dim_l(k int PRIMARY KEY)", "INSERT INTO dim_l VALUES (1)",
          "CREATE TABLE fact_l(k int, m float8) USING pgcolumnar", "INSERT INTO fact_l VALUES (1, 1.0), (2, 2.0)",
          "CREATE TABLE heap_l (k int, m float8) USING heap", "INSERT INTO heap_l VALUES (1, 1.0), (2, 2.0)",
          "ANALYZE dim_l", "ANALYZE fact_l")
    sqll = "SELECT count(*), coalesce(sum(dim_l.k)::text,'z') FROM fact_l LEFT JOIN dim_l ON fact_l.k = dim_l.k"
    expect.num(_folds(conn, sqll), 0, "LEFT join refuses the join fold")
    expect.text(_with(conn, "on", sqll), _row(conn, "SELECT count(*), coalesce(sum(dim_l.k)::text,'z') "
                                                    "FROM heap_l LEFT JOIN dim_l ON heap_l.k = dim_l.k"),
                "LEFT join answer equals heap")

    # a Join Filter besides the hash clause is not a membership test
    _exec(conn, "CREATE TABLE dim_xf(k int PRIMARY KEY, t int)",
          "INSERT INTO dim_xf SELECT g, 20 FROM generate_series(1,40) g",
          "CREATE TABLE fact_xf(k int, v int) USING pgcolumnar",
          "INSERT INTO fact_xf SELECT 1 + (g % 40), g % 40 FROM generate_series(1,2000) g",
          "CREATE TABLE heap_xf (k int, v int) USING heap", "INSERT INTO heap_xf SELECT * FROM fact_xf",
          "ANALYZE dim_xf", "ANALYZE fact_xf")
    sqlxf = ("SELECT coalesce(sum(fact_xf.v)::text,'z') FROM fact_xf JOIN dim_xf "
             "ON fact_xf.k = dim_xf.k AND fact_xf.v > dim_xf.t")
    expect.num(_folds(conn, sqlxf), 0, "extra join filter refuses the join fold")
    expect.text(_with(conn, "on", sqlxf), _with(conn, "off", sqlxf), "extra join filter answer equals GUC off")
    expect.text(_with(conn, "on", sqlxf), _row(conn, "SELECT coalesce(sum(heap_xf.v)::text,'z') FROM heap_xf "
                                                     "JOIN dim_xf ON heap_xf.k = dim_xf.k AND heap_xf.v > dim_xf.t"),
                "extra join filter answer equals heap")

    _exec(conn, "CREATE TABLE dim_xn(k int PRIMARY KEY, t int)",
          "INSERT INTO dim_xn SELECT g, 7 FROM generate_series(1,12) g",
          "CREATE TABLE fact_xn(k int, v int) USING pgcolumnar",
          "INSERT INTO fact_xn SELECT 1 + (g % 12), g % 9 FROM generate_series(1,360) g",
          "CREATE TABLE heap_xn (k int, v int) USING heap", "INSERT INTO heap_xn SELECT * FROM fact_xn",
          "ANALYZE dim_xn", "ANALYZE fact_xn")
    sqlxn = ("SELECT coalesce(sum(fact_xn.v)::text,'z') FROM fact_xn JOIN dim_xn "
             "ON fact_xn.k = dim_xn.k AND fact_xn.v <> dim_xn.t")
    expect.num(_folds(conn, sqlxn), 0, "inequality join filter refuses the join fold")
    expect.text(_with(conn, "on", sqlxn), _row(conn, "SELECT coalesce(sum(heap_xn.v)::text,'z') FROM heap_xn "
                                                     "JOIN dim_xn ON heap_xn.k = dim_xn.k AND heap_xn.v <> dim_xn.t"),
                "inequality join filter answer equals heap")

    # The fold compares keys with the type's default equality (int4eq here). A join
    # clause with another equality operator must refuse the fold (#2066). ##= says two
    # ints are equal when their absolute values are, so fact keys 1,-1,2,-2,3 match
    # dimension keys 1,2 in 4 of every 5 rows under ##=, and in 2 of every 5 under
    # int4eq. The functions are PL/pgSQL so the planner cannot inline them away, and
    # the unique index under abs_ops makes the dimension unique.
    for name, body in (("abs_eq", "abs($1) = abs($2)"), ("abs_lt", "abs($1) < abs($2)"),
                       ("abs_le", "abs($1) <= abs($2)"), ("abs_gt", "abs($1) > abs($2)"),
                       ("abs_ge", "abs($1) >= abs($2)")):
        _exec(conn, f"CREATE FUNCTION {name}(int, int) RETURNS bool IMMUTABLE STRICT LANGUAGE plpgsql "
                    f"AS $f$BEGIN RETURN {body}; END$f$")
    _exec(conn, "CREATE FUNCTION abs_cmp(int, int) RETURNS int IMMUTABLE STRICT LANGUAGE plpgsql "
                "AS $f$BEGIN RETURN btint4cmp(abs($1), abs($2)); END$f$",
          "CREATE FUNCTION abs_hash(int) RETURNS int IMMUTABLE STRICT LANGUAGE plpgsql "
          "AS $f$BEGIN RETURN hashint4(abs($1)); END$f$",
          "CREATE OPERATOR ##= (leftarg = int, rightarg = int, function = abs_eq, "
          "commutator = ##=, hashes, merges, restrict = eqsel, join = eqjoinsel)",
          "CREATE OPERATOR ##< (leftarg = int, rightarg = int, function = abs_lt)",
          "CREATE OPERATOR ##<= (leftarg = int, rightarg = int, function = abs_le)",
          "CREATE OPERATOR ##> (leftarg = int, rightarg = int, function = abs_gt)",
          "CREATE OPERATOR ##>= (leftarg = int, rightarg = int, function = abs_ge)",
          "CREATE OPERATOR CLASS abs_ops FOR TYPE int USING btree AS OPERATOR 1 ##<, OPERATOR 2 ##<=, "
          "OPERATOR 3 ##=, OPERATOR 4 ##>=, OPERATOR 5 ##>, FUNCTION 1 abs_cmp(int, int)",
          "CREATE OPERATOR CLASS abs_ops FOR TYPE int USING hash AS OPERATOR 1 ##=, FUNCTION 1 abs_hash(int)",
          "CREATE TABLE dim_abs(k int)", "INSERT INTO dim_abs VALUES (1), (2)",
          "CREATE UNIQUE INDEX ON dim_abs (k abs_ops)",
          "CREATE TABLE fact_abs(v int, m int) USING pgcolumnar",
          "INSERT INTO fact_abs SELECT (ARRAY[1,-1,2,-2,3])[1 + g % 5], g FROM generate_series(1,1000) g",
          "CREATE TABLE heap_abs (v int, m int) USING heap", "INSERT INTO heap_abs SELECT * FROM fact_abs",
          "ANALYZE dim_abs", "ANALYZE fact_abs")
    sqlab = ("SELECT count(*), coalesce(sum(fact_abs.m)::text,'z') FROM fact_abs "
             "JOIN dim_abs ON fact_abs.v ##= dim_abs.k")
    expect.num(_with(conn, "on", f"EXPLAIN (COSTS OFF) {sqlab}").count("Columnar Join Fold: yes"), 0,
               "non-default equality operator refuses the join fold")
    expect.text(_with(conn, "on", sqlab), _row(conn, "SELECT count(*), coalesce(sum(heap_abs.m)::text,'z') "
                                                     "FROM heap_abs JOIN dim_abs ON heap_abs.v ##= dim_abs.k"),
                "non-default equality operator answer equals heap")


def test_a_join_clause_under_another_collation_refuses_the_fold(pgc_conn, expect):
    """The fold compares keys with the fact column's own equality and collation (#2066).

    A join clause under another collation must refuse the fold, or the answer follows
    the wrong collation. Fact keys 'a','A','b','B','c' meet dimension keys 'a','b': a
    case-insensitive comparison matches 4 of every 5 fact rows, a byte comparison 2.
    Both directions are covered: a case-insensitive dimension against a plain fact
    column, and a byte clause over a case-insensitive fact column. One collation on
    both sides, plain or case-insensitive, is the control and must still fold.

    ITS OWN TEST, because `cannot_run` declares a whole test unrunnable when the
    server has no ICU, and the checks above need none.
    """
    conn = pgc_conn
    try:
        _exec(conn, "CREATE COLLATION ci "
                    "(provider = icu, locale = 'und-u-ks-level2', deterministic = false)")
    except psycopg.Error:
        expect.cannot_run(
            "MISSING_DEPENDENCY",
            "a case-insensitive collation needs a non-deterministic ICU collation, "
            "and a server built without ICU cannot create one",
            name="a join clause under another collation refuses the fold")
        return
    _exec(conn, "CREATE TABLE dim_tx(k text PRIMARY KEY)", "INSERT INTO dim_tx VALUES ('a'), ('b')",
          "CREATE TABLE dim_ci(k text COLLATE ci PRIMARY KEY)", "INSERT INTO dim_ci VALUES ('a'), ('b')",
          'CREATE TABLE dim_cb(k text COLLATE "C" PRIMARY KEY)', "INSERT INTO dim_cb VALUES ('a'), ('b')",
          "CREATE TABLE fact_tx(k text, m int) USING pgcolumnar",
          "INSERT INTO fact_tx SELECT (ARRAY['a','A','b','B','c'])[1 + g % 5], g FROM generate_series(1,1000) g",
          "CREATE TABLE heap_tx (k text, m int) USING heap", "INSERT INTO heap_tx SELECT * FROM fact_tx",
          "CREATE TABLE fact_tci(k text COLLATE ci, m int) USING pgcolumnar",
          "INSERT INTO fact_tci SELECT * FROM fact_tx",
          "CREATE TABLE heap_tci (k text COLLATE ci, m int) USING heap",
          "INSERT INTO heap_tci SELECT * FROM fact_tx",
          "ANALYZE dim_tx", "ANALYZE dim_ci", "ANALYZE dim_cb", "ANALYZE fact_tx", "ANALYZE fact_tci")
    fold = "Columnar Join Fold: yes"

    sqltx = "SELECT count(*), coalesce(sum(fact_tx.m)::text,'z') FROM fact_tx JOIN dim_tx ON fact_tx.k = dim_tx.k"
    expect.num(_with(conn, "on", f"EXPLAIN (COSTS OFF) {sqltx}").count(fold), 1,
               "text key under one collation keeps the join fold")
    expect.text(_with(conn, "on", sqltx), _row(conn, "SELECT count(*), coalesce(sum(heap_tx.m)::text,'z') "
                                                     "FROM heap_tx JOIN dim_tx ON heap_tx.k = dim_tx.k"),
                "text key under one collation answer equals heap")

    # One case-insensitive collation on both sides is a second control. The fold
    # compares and hashes under the fact column's collation, so it must still fold
    # here, through the nondeterministic equality and hash path.
    sqltci = "SELECT count(*), coalesce(sum(fact_tci.m)::text,'z') FROM fact_tci JOIN dim_ci ON fact_tci.k = dim_ci.k"
    expect.num(_with(conn, "on", f"EXPLAIN (COSTS OFF) {sqltci}").count(fold), 1,
               "case-insensitive key under one collation keeps the join fold")
    expect.text(_with(conn, "on", sqltci), _row(conn, "SELECT count(*), coalesce(sum(heap_tci.m)::text,'z') "
                                                      "FROM heap_tci JOIN dim_ci ON heap_tci.k = dim_ci.k"),
                "case-insensitive key under one collation answer equals heap")

    sqlci = "SELECT count(*), coalesce(sum(fact_tx.m)::text,'z') FROM fact_tx JOIN dim_ci ON fact_tx.k = dim_ci.k"
    expect.text(_with(conn, "on", sqlci), _row(conn, "SELECT count(*), coalesce(sum(heap_tx.m)::text,'z') "
                                                     "FROM heap_tx JOIN dim_ci ON heap_tx.k = dim_ci.k"),
                "case-insensitive join clause answer equals heap")
    expect.num(_with(conn, "on", f"EXPLAIN (COSTS OFF) {sqlci}").count(fold), 0,
               "case-insensitive join clause refuses the join fold")

    sqlcb = ("SELECT count(*), coalesce(sum(fact_tci.m)::text,'z') FROM fact_tci "
             'JOIN dim_cb ON fact_tci.k COLLATE "C" = dim_cb.k')
    expect.text(_with(conn, "on", sqlcb), _row(conn, "SELECT count(*), coalesce(sum(heap_tci.m)::text,'z') "
                                                     'FROM heap_tci JOIN dim_cb ON heap_tci.k COLLATE "C" = dim_cb.k'),
                "byte join clause over a case-insensitive column answer equals heap")
    expect.num(_with(conn, "on", f"EXPLAIN (COSTS OFF) {sqlcb}").count(fold), 0,
               "byte join clause over a case-insensitive column refuses the join fold")

def test_a_fold_rescan_drains_the_new_dimension_keys(pgc_conn, expect):
    """Each rescan of the join fold joins against that rescan's dimension keys
    (the sibling of #2062). The pytest twin of the shell's rescan arms, with its
    own names, keys and outer values.

    A correlated subquery or LATERAL join reruns the fold for each outer row. The
    fold read the dimension inside its key context and reset that context on
    rescan, which freed the dimension scan's state, so the second rescan crashed
    the backend. The DISTINCT arm puts a HashAggregate under the fold; the fold
    holds it in custom_ps, which ExecReScan does not tell about a changed
    parameter, so it returned its first rows again. Each arm runs under default
    join settings and with hash joins forced. The outer values grow, so a stale
    or empty key table cannot match heap.
    """
    conn = pgc_conn
    _exec(conn, "CREATE TABLE dim_rs(k int PRIMARY KEY)",
          "INSERT INTO dim_rs SELECT g FROM generate_series(1,30) g",
          "CREATE TABLE dimh_rs(k int)",
          "INSERT INTO dimh_rs SELECT g FROM generate_series(1,30) g",
          "CREATE TABLE fact_rs(k int, m float8) USING pgcolumnar",
          "INSERT INTO fact_rs SELECT 1 + (g % 30), 1::float8 FROM generate_series(1,6000) g",
          "CREATE TABLE heap_rs (k int, m float8) USING heap", "INSERT INTO heap_rs SELECT * FROM fact_rs",
          "ANALYZE dim_rs", "ANALYZE dimh_rs", "ANALYZE fact_rs")
    outer = "(VALUES (4), (10), (25)) v(lim)"
    arms = {
        "scalar": (f"SELECT v.lim, (SELECT count(*) FROM {{t}} JOIN dim_rs ON {{t}}.k = dim_rs.k "
                   f"WHERE dim_rs.k < v.lim) FROM {outer} ORDER BY 1"),
        "LATERAL": (f"SELECT v.lim, x.c FROM {outer}, LATERAL (SELECT count(*) c FROM {{t}} "
                    f"JOIN dim_rs ON {{t}}.k = dim_rs.k WHERE dim_rs.k < v.lim) x ORDER BY 1"),
        "distinct": (f"SELECT v.lim, (SELECT count(*) FROM {{t}} JOIN (SELECT DISTINCT k FROM dimh_rs "
                     f"WHERE k < v.lim) d ON {{t}}.k = d.k) FROM {outer} ORDER BY 1"),
    }
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute(f"SET {GUC} = on")
        for name, sql in arms.items():
            cur.execute(sql.format(t="heap_rs"))
            heap = cur.fetchall()
            expect.text(" ".join(f"{lim}:{c}" for lim, c in heap), "4:600 10:1800 25:4800",
                        f"premise: heap's {name} rescan answer grows with each outer value")
            for setting, allowed in (("default", "on"), ("hash-only", "off")):
                cur.execute(f"SET enable_nestloop = {allowed}")
                cur.execute(f"SET enable_mergejoin = {allowed}")
                cur.execute("EXPLAIN (COSTS OFF) " + sql.format(t="fact_rs"))
                plan = "\n".join(r[0] for r in cur.fetchall())
                expect.num(plan.count("Columnar Join Fold: yes"), 1,
                           f"premise: the {setting} {name} rescan uses the join fold")
                cur.execute(sql.format(t="fact_rs"))
                expect.rows(cur.fetchall(), heap,
                            f"each {setting} {name} fold rescan drains its own dimension keys")

"""The index-fetch penalty sizes its row-group decode by the table's OPTION (#806).

`pgcolumnar_index_fetch_penalty` prices the row-group decode a per-row index fetch
forces, and that decode is one row group, so its size is the limit in force for the
table -- the per-table `stripe_row_limit` option when set. Reading only the session
GUC prices every table alike however its owner declared it.

The property is a RESPONSE, not a number: move the per-table option and the estimate
must move. Asserting a cost value would pin this box's cost constants instead.

Independent of test/native_index_fetch_stripe_cost.sh: same public seam (EXPLAIN of
an index-forced range scan), own fixture, own observations. Assertion names match the
shell suite so the two can be compared by name, not by importing each other.

THE RANGE IS 1,000 ROWS HERE AND 100,000 IN THE SHELL TWIN, AND THAT IS THE POINT.
Measured on this fixture, PG18 assert build, `enable_seqscan=off`, total cost of the
top node:

        rows matched   limit=2,000              limit=150,000
             1,000     Index Scan     51.70     Index Scan      808.98
            20,000     Index Scan    735.26     Index Scan     1752.26
            50,000     Index Scan   1827.50     Custom Scan    2004.50
           100,000     Index Scan   3657.00     Custom Scan    2004.50

At the shell twin's 100,000 rows the second arm is not an index scan at all, so the
two numbers it compares are the prices of two DIFFERENT plans. They differ, and the
check passes, but a plan flip is exactly what a changed penalty can cause -- so that
comparison cannot separate "the penalty re-priced this fetch" from "the penalty
pushed the planner onto another node". Below 20,000 rows both arms stay on the index
and the difference is a difference in the price of one plan shape, which is the
claim. The port asserts the shape before comparing the prices.

AND THE EFFECT IS THE PENALTY'S. Four cells, same fixture, 1,000 rows:

        penalty on     51.70   808.98      responds to the option
        penalty off    35.20    35.20      identical

so the arm below is not merely observing that two costs differ; the term that
carries the option into the price is named and its removal flattens both arms onto
the same number. That control is asserted, not just quoted.
"""

RANGE = 1000
SMALL, LARGE = 2000, 150000


def _top(conn, *, penalty="on"):
    """-> (node type, total cost, plan) for the index-forced range scan."""
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute(f"SET pgcolumnar.enable_index_fetch_penalty = {penalty}")
        cur.execute(
            "EXPLAIN (FORMAT JSON, COSTS ON) "
            f"SELECT * FROM ct WHERE id BETWEEN 1 AND {RANGE}"
        )
        plan = cur.fetchone()[0]
    top = plan[0]["Plan"]
    return top["Node Type"], top["Total Cost"], plan


def _set_limit(conn, rows):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pgcolumnar.set_options('ct', stripe_row_limit => %s)", (rows,)
        )


def test_native_index_fetch_stripe_cost(pgc_conn, expect):
    n = 200000
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ct (id int, v int) USING pgcolumnar")
        cur.execute(f"INSERT INTO ct SELECT g, g FROM generate_series(1, {n}) g")
        cur.execute("CREATE INDEX ct_id ON ct (id)")
        cur.execute("ANALYZE ct")
        cur.execute("SELECT count(*) FROM ct")
        expect.num(cur.fetchone()[0], n, "premise: the table holds all 200000 rows")

    _set_limit(pgc_conn, SMALL)
    type1, c1, plan1 = _top(pgc_conn)
    _set_limit(pgc_conn, LARGE)
    type2, c2, _ = _top(pgc_conn)
    print(f"-- index-scan total cost: stripe_row_limit={SMALL} -> {c1} ; "
          f"={LARGE} -> {c2}")
    print(f"-- top node: {type1} then {type2}")

    # A COST IS A NUMBER OR THE REST OF THIS FILE IS ABOUT NOTHING. The shell twin
    # asks whether its `sed` produced digits; here the value arrives typed from
    # FORMAT JSON, so the question is whether it is a real estimate rather than a
    # zero. A floor of 1 refuses a missing or zero cost without pinning any of this
    # box's cost constants. NAMED FOR WHAT IT MEASURES (#1339): one cost cannot show
    # a response to the option, so the name no longer claims one. The response is
    # the `expect.differ` arm below.
    expect.at_least(c1, 1, "the index-fetch cost is a number of at least 1")

    # SAME SHAPE ON BOTH ARMS, ASSERTED BEFORE THE PRICES ARE COMPARED. Arms, not
    # premises (#1339, #1236): the planner's choice is repository behaviour. See the
    # module docstring: two costs also differ when the plan flipped, and then the
    # comparison is about node selection rather than about the penalty's arithmetic.
    expect.text(type2, type1, "both arms priced the same plan shape")
    expect.plan_node(
        plan1, node_type="Index Scan",
        name="the priced plan is the per-row index fetch the penalty prices",
    )

    expect.differ(
        c1, c2,
        "changing the per-table stripe_row_limit changes the estimated cost",
    )

    # THE ATTRIBUTION CELL. Without it this file shows only that two costs differ
    # while one option moved, and any other term reading that option would satisfy
    # it equally. With the penalty off the same two arms must collapse onto one
    # number -- that is what names the penalty as the term carrying the option.
    _set_limit(pgc_conn, SMALL)
    _, off_small, _ = _top(pgc_conn, penalty="off")
    _set_limit(pgc_conn, LARGE)
    _, off_large, _ = _top(pgc_conn, penalty="off")
    print(f"-- penalty off: {SMALL} -> {off_small} ; {LARGE} -> {off_large}")
    expect.num(
        off_small, off_large,
        "with the fetch penalty off the option reaches no other cost term",
    )

    with pgc_conn.cursor() as cur:
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1, "backend alive")

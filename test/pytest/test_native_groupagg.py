"""The grouped vectorized aggregate must answer exactly what core answers (#289).

The grouped path fires for `SELECT <keys>, agg(col) ... [WHERE ...] GROUP BY <keys>`
over one columnar relation. It reads each surviving row, rechecks the full WHERE,
evaluates the group keys, and scatters the row into an open-addressing hash table
whose per-group accumulators fold in scan order.

TWO ORACLES, AND NEITHER IS THE OTHER'S SUBSTITUTE.

  * A heap mirror holds the same rows, so exact aggregates must agree byte for byte.
    It cannot judge float summation order, because heap and columnar do not scan in
    the same order and float addition is not associative.
  * A toggle differential runs the same query over the SAME columnar rows with the
    path off and on. Both arms read in the same order, so even float sums must be
    identical -- which is what validates the order-preserving accumulators, and what
    the heap mirror cannot do.

EVERY COMPARISON CARRIES A NODE PREMISE, and that is the whole reason the file is
shaped this way. A query the node quietly rejects runs the scalar Agg in BOTH arms,
so the comparison is two identical runs agreeing with each other. That is how
`sum(real)` returning 0 got through an earlier version of the shell suite: rounding
an aggregate in the select list makes the output an expression over an aggregate,
which the node rejects, and the check went green against a broken accumulator.

INDEPENDENT OF `test/native_groupagg.sh`. Same public seams -- the plan, and the rows
each query returns -- and nothing else shared:

  * The shell greps `EXPLAIN` text for the marker line. This file reads
    `EXPLAIN (FORMAT JSON)` and asks whether any node carries the PROPERTY
    `Columnar Vectorized Group Keys`, through `expect.plan_marker`, which refuses an
    empty plan rather than reading it as "the node is absent".
  * The shell compares `md5(string_agg(...))` computed by the server. This file
    compares sorted row tuples in Python, so a disagreement prints the rows rather
    than two unequal hashes.
  * The GUC is set on the SESSION here. The shell sets it on the DATABASE because
    every `q()` opens a new connection; this file holds one, so the setting cannot
    be lost between a premise and the query it is a premise for.

THE COLLATION ARMS ARE THEIR OWN TEST, and that is not tidiness. `check_skip` skips
one check; `cannot_run` declares a whole TEST unrunnable. Putting the ICU-gated pair
in the same function as anything else would make the refusal wider than the shell
suite's, and a refusal that takes unrelated arms with it is a worse report than the
one it replaces.
"""
import json
import re
import psycopg

from pgc_vacuity import query_error

ROWS = 20000
MARKER = "Columnar Vectorized Group Keys"

PAIR_COLUMNS = """g       int,
    ts      timestamp,
    host    text,
    region  text,
    i2      smallint,
    i4      int,
    i8      bigint,
    f4      real,
    f8      double precision,
    num     numeric"""

# Interleaved NULLs in both keys and measures: a NULL host or ts must form its own
# group, exactly as GROUP BY does. The `%` are the server's modulo and reach it
# literally -- psycopg substitutes only when parameters are passed, and none are.
PAIR_BODY = f"""SELECT g,
        CASE WHEN g % 331 = 0 THEN NULL
             ELSE timestamp '2024-01-01 00:00:00' + (g * interval '25 seconds') END,
        CASE WHEN g % 197 = 0 THEN NULL ELSE 'host_' || (g % 50) END,
        'region_' || (g % 4),
        CASE WHEN g % 11 = 0 THEN NULL ELSE (g % 97 - 48)::smallint END,
        CASE WHEN g %  7 = 0 THEN NULL ELSE (g * 7 - 3) END,
        CASE WHEN g %  5 = 0 THEN NULL ELSE (g::bigint * 1000003 - 5) END,
        CASE WHEN g %  6 = 0 THEN NULL ELSE (g * 1.5)::real END,
        CASE WHEN g %  9 = 0 THEN NULL ELSE (g::float8 * 0.125 - 3.5) END,
        CASE WHEN g %  8 = 0 THEN NULL ELSE (g * 0.01)::numeric(12,4) END
    FROM generate_series(1, {ROWS}) g"""

EXACT = ("count(*), count(i4), count(host), "
         "sum(i2), sum(i4), sum(i8), sum(num), "
         "min(f8), max(f8), min(host), max(host), min(ts), max(ts), min(i8), max(i8)")

# The accumulators the heap mirror cannot judge: float summation order differs
# between the two storage types, so these are compared off-versus-on instead.
FLOATAGG = ("sum(f4), sum(f8), sum(num), "
            "avg(i2), avg(i4), avg(i8), avg(f4), avg(f8), avg(num)")

Q_KEYHOST = "SELECT host, count(*), sum(i4) FROM t_col GROUP BY host"
Q_KEYHOUR = ("SELECT date_trunc('hour', ts) h, count(*) FROM t_col "
             "GROUP BY date_trunc('hour', ts)")
Q_Q4 = ("SELECT date_trunc('hour', ts) h, host, avg(f8), count(*) "
        "FROM t_col WHERE ts >= timestamp '2024-01-01 02:00:00' "
        "AND ts < timestamp '2024-01-01 06:00:00' "
        "GROUP BY date_trunc('hour', ts), host")


def _exec(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)


def _rows(conn, sql):
    """-> the result as sorted tuples of text.

    Text, because the two storage types can return the same value in different
    Python types for `numeric` and the comparison is about the VALUE. Sorted,
    because neither GROUP BY nor either scan promises an order.

    NULL IS "\\0", NOT "" (#1339). Mapped to "", a NULL and an empty string compared
    equal, so a node returning '' for min(host) over the NULL-host group matched
    heap's NULL. PostgreSQL text cannot hold a NUL byte, so nothing stored can
    collide with it. The shell twin hashes `_row::text`, which tells them apart too.
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return sorted(tuple("\0" if v is None else str(v) for v in row)
                      for row in cur.fetchall())


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _plan(conn, sql):
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (COSTS OFF, FORMAT JSON) {sql}")
        return cur.fetchone()[0]


def _estimate(conn, sql):
    """-> the top node's row estimate, which is what a group bound moves."""
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (COSTS ON, FORMAT JSON) {sql}")
        return cur.fetchone()[0][0]["Plan"]["Plan Rows"]


def _top_cost(conn, sql):
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (COSTS ON, FORMAT JSON) {sql}")
        return cur.fetchone()[0][0]["Plan"]["Total Cost"]


def _groupvec(conn, on):
    _exec(conn, f"SET pgcolumnar.enable_group_vectorization = {'on' if on else 'off'}")


def _make_pair(expect, conn):
    """Build the heap/columnar pair, and assert it is the pair this file assumes.

    THE ROW COUNT IS ASSERTED, not assumed, and it is not decoration. Two things
    downstream are arithmetic over it: the cost test states 450.00 =
    cpu_operator_cost x 20,000 rows x 9 extra aggregates, and the group shapes
    (50 hosts, 4 regions, ~14 hours of timestamps) are counts modulo this size. A
    fixture that loaded short would leave both describing a table that does not
    exist, while every oracle comparison still agreed with itself.

    Both halves are counted, not one, because the mirror needs them EQUAL. The
    layer already refuses an empty result compared with an empty result, so this
    does not close that hole -- it makes a short load name the fixture rather than
    surface three screens later as a cost that does not match its own docstring.
    """
    _exec(conn, f"CREATE TABLE t_heap ({PAIR_COLUMNS}) USING heap")
    _exec(conn, f"CREATE TABLE t_col ({PAIR_COLUMNS}) USING pgcolumnar")
    # Small row groups, so grouping spans many groups and vectors.
    _exec(conn, "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000)")
    _exec(conn, f"INSERT INTO t_heap {PAIR_BODY}")
    # From the heap table, not from the generator again: the two tables must hold
    # the same rows for the mirror to be an oracle rather than a second sample.
    _exec(conn, "INSERT INTO t_col SELECT * FROM t_heap")
    heap = _one(conn, "SELECT count(*) FROM t_heap")
    col = _one(conn, "SELECT count(*) FROM t_col")
    expect.text(f"{heap}/{col}", f"{ROWS}/{ROWS}",
                "the pair loaded in full, so the mirror compares equal populations")
    _groupvec(conn, True)


def _oracle(expect, conn, label, template):
    """The columnar form against the heap mirror, with the node asserted first.

    `label` is passed through rather than spelled at the call, exactly as the shell
    helper passes `$label`: the concrete labels are built by the caller in both
    harnesses, so neither side publishes them as static names.
    """
    columnar = template.replace("%T", "t_col")
    expect.plan_marker(_plan(conn, columnar), MARKER, name=f"{label} [node fires]")
    expect.rows(_rows(conn, columnar), _rows(conn, template.replace("%T", "t_heap")),
                label)


def _toggle(expect, conn, label, query):
    """The same query over the same columnar rows, path off versus on.

    The node premise comes first for the reason the module docstring gives: without
    it a query the node rejects runs the scalar Agg in both arms and the comparison
    is one plan agreeing with itself.
    """
    _groupvec(conn, True)
    expect.plan_marker(_plan(conn, query), MARKER, name=f"{label} [node fires]")
    _groupvec(conn, False)
    off = _rows(conn, query)
    _groupvec(conn, True)
    expect.rows(_rows(conn, query), off, label)


# ---- the node is chosen for the shapes it supports ---------------------------

def test_the_grouped_node_is_chosen_for_the_shapes_it_supports(pgc_conn, expect):
    _make_pair(expect, pgc_conn)
    expect.plan_marker(_plan(pgc_conn, Q_KEYHOST), MARKER,
                       name="plan: GROUP BY text key uses grouped node")
    expect.plan_marker(_plan(pgc_conn, Q_KEYHOUR), MARKER,
                       name="plan: GROUP BY hour expr uses grouped node")
    expect.plan_marker(_plan(pgc_conn, Q_Q4), MARKER,
                       name="plan: q4-shape (WHERE+2 keys) uses grouped node")
    _groupvec(pgc_conn, False)
    expect.plan_marker(_plan(pgc_conn, Q_KEYHOST), MARKER, absent=True,
                       name="plan: off -> ordinary Agg, not grouped node")


# ---- the heap mirror ---------------------------------------------------------

def test_the_grouped_path_agrees_with_a_heap_mirror(pgc_conn, expect):
    _make_pair(expect, pgc_conn)
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY host",
            f"SELECT host, {EXACT} FROM %T GROUP BY host")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY hour",
            f"SELECT date_trunc('hour', ts), {EXACT} FROM %T "
            "GROUP BY date_trunc('hour', ts)")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY hour, host (q4/q5 shape)",
            f"SELECT date_trunc('hour', ts), host, {EXACT} FROM %T "
            "GROUP BY date_trunc('hour', ts), host")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY region, host (two text keys)",
            f"SELECT region, host, {EXACT} FROM %T GROUP BY region, host")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY integer expr key",
            f"SELECT (g % 7), {EXACT} FROM %T GROUP BY (g % 7)")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY smallint column key",
            "SELECT i2, count(*), sum(i4), min(f8), max(f8) FROM %T GROUP BY i2")
    # A WHERE that both prunes groups and needs a residual recheck.
    _oracle(expect, pgc_conn, "oracle exact: WHERE range + GROUP BY hour, host",
            f"SELECT date_trunc('hour', ts), host, {EXACT} FROM %T "
            "WHERE ts >= timestamp '2024-01-01 02:00:00' "
            "AND ts < timestamp '2024-01-01 06:00:00' "
            "GROUP BY date_trunc('hour', ts), host")
    _oracle(expect, pgc_conn, "oracle exact: WHERE on measure + GROUP BY host",
            f"SELECT host, {EXACT} FROM %T "
            "WHERE i4 > 40000 AND host IS NOT NULL GROUP BY host")
    # The NULL keys are already in the fixture (g%197, g%331); this asserts the
    # NULL group folds the way GROUP BY does rather than being dropped.
    _oracle(expect, pgc_conn, "oracle exact: NULL ts group folds like heap",
            "SELECT date_trunc('hour', ts), count(*), sum(i4) FROM %T "
            "GROUP BY date_trunc('hour', ts)")


def test_the_mirror_still_agrees_after_deletes_and_an_added_column(pgc_conn, expect):
    _make_pair(expect, pgc_conn)
    _exec(pgc_conn, "DELETE FROM t_heap WHERE g % 17 = 0")
    _exec(pgc_conn, "DELETE FROM t_col  WHERE g % 17 = 0")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY host after deletes",
            f"SELECT host, {EXACT} FROM %T GROUP BY host")
    _toggle(expect, pgc_conn, "toggle exact: GROUP BY host after deletes",
            f"SELECT host, {EXACT} FROM t_col GROUP BY host")

    _exec(pgc_conn, "ALTER TABLE t_heap ADD COLUMN extra int DEFAULT 7")
    _exec(pgc_conn, "ALTER TABLE t_col  ADD COLUMN extra int DEFAULT 7")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY host, added column",
            "SELECT host, count(*), sum(extra), sum(i4) FROM %T GROUP BY host")
    _oracle(expect, pgc_conn, "oracle exact: GROUP BY added column",
            "SELECT extra, count(*), sum(i4) FROM %T GROUP BY extra")


# ---- the toggle differential -------------------------------------------------

def test_the_accumulators_fold_identically_with_the_path_off_and_on(pgc_conn, expect):
    _make_pair(expect, pgc_conn)
    _toggle(expect, pgc_conn, "toggle float/avg: GROUP BY host",
            f"SELECT host, {FLOATAGG} FROM t_col GROUP BY host")
    _toggle(expect, pgc_conn, "toggle float/avg: GROUP BY hour, host",
            f"SELECT date_trunc('hour', ts), host, {FLOATAGG} FROM t_col "
            "GROUP BY date_trunc('hour', ts), host")
    _toggle(expect, pgc_conn, "toggle float/avg: WHERE + GROUP BY region, host",
            f"SELECT region, host, {FLOATAGG} FROM t_col "
            "WHERE f8 IS NOT NULL GROUP BY region, host")
    _toggle(expect, pgc_conn, "toggle exact: GROUP BY host",
            f"SELECT host, {EXACT} FROM t_col GROUP BY host")
    _toggle(expect, pgc_conn, "toggle exact: GROUP BY hour, host",
            f"SELECT date_trunc('hour', ts), host, {EXACT} FROM t_col "
            "GROUP BY date_trunc('hour', ts), host")
    _toggle(expect, pgc_conn, "toggle exact: GROUP BY region, host, WHERE",
            f"SELECT region, host, {EXACT} FROM t_col "
            "WHERE i8 IS NOT NULL GROUP BY region, host")


def test_min_max_keep_the_display_scale_core_keeps(pgc_conn, expect):
    """Numeric values equal by value and different in display scale (1.0 vs 1.00).

    Core's larger/smaller keep the LATER value on a tie, so the answer depends on
    fold order and the heap mirror cannot judge it -- the two storage types do not
    scan in the same order. A toggle differential can: both arms read the same
    columnar rows in the same order.
    """
    _exec(pgc_conn, "CREATE TABLE tie_col (k int, n numeric) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO tie_col VALUES "
                    "(1, 1.0), (1, 1.00), (1, 1.000), (1, 0.5), (1, 0.50), "
                    "(2, 5.5), (2, 5.50), (2, 5.500)")
    _toggle(expect, pgc_conn, "toggle min/max numeric display-scale tie",
            "SELECT k, min(n), max(n) FROM tie_col GROUP BY k")


def test_a_lone_negative_zero_keeps_its_sign(pgc_conn, expect):
    """Core's sum assigns the first value directly, so a sum of -0.0 prints '-0'.

    Folding it into +0.0 would drop the sign, and the only oracle that can see that
    is one reading the same rows in the same order.
    """
    _exec(pgc_conn, "CREATE TABLE z_col (k int, r real, d float8) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO z_col VALUES (1,'-0','-0'), (2,'-0','-0'), (2,'-0','-0')")
    _toggle(expect, pgc_conn, "toggle sum signed-zero (-0 preserved)",
            "SELECT k, sum(r), sum(d) FROM z_col GROUP BY k")


# ---- shapes the node must decline, still answering correctly -----------------

def test_a_key_well_under_the_gate_takes_the_node(pgc_conn, expect):
    """Since #1292 phase 2 the node is taken only for an estimate of at most a tenth
    of pgcolumnar.groupagg_max_groups, so the runtime cap is reached only through a
    wrong estimate; test_on_by_default_and_gated_on_the_estimate proves that path.
    (Before the gate this forced the node under a cap of 100 with hashagg and sort
    off; the gate now declines that plan, which is the point of it.)
    """
    _make_pair(expect, pgc_conn)
    expect.plan_marker(_plan(pgc_conn, "SELECT g, count(*) FROM t_col GROUP BY g"),
                       MARKER, name="plan: the node is chosen for a key well under the gate")

    # Named at the call rather than through `_oracle`, and without a second node
    # premise, because the shell suite spends a bare `diff_query` here: the node was
    # asserted three lines above and asserting it again would publish a premise the
    # original does not have.
    expect.rows(
        _rows(pgc_conn, "SELECT g, count(*), sum(i4) FROM t_col GROUP BY g"),
        _rows(pgc_conn, "SELECT g, count(*), sum(i4) FROM t_heap GROUP BY g"),
        "oracle exact: default cap runs the high-cardinality key")


def test_an_output_built_on_a_key_falls_back_and_still_answers(pgc_conn, expect):
    _make_pair(expect, pgc_conn)
    expect.plan_marker(
        _plan(pgc_conn, "SELECT upper(host), count(*) FROM t_col GROUP BY host"),
        MARKER, absent=True, name="plan: f(key) output falls back")
    # The fallback must still be RIGHT, which is the half a plan assertion cannot
    # reach. Named through the helper's own label, as the shell suite names it.
    expect.rows(
        _rows(pgc_conn, "SELECT upper(host), count(*), sum(i4) FROM t_col GROUP BY host"),
        _rows(pgc_conn, "SELECT upper(host), count(*), sum(i4) FROM t_heap GROUP BY host"),
        "oracle exact: f(key) output still correct")

    expect.plan_marker(_plan(pgc_conn, "SELECT host FROM t_col GROUP BY host"),
                       MARKER, absent=True,
                       name="plan: GROUP BY with no aggregate falls back")


def test_a_non_deterministic_collation_key_falls_back(pgc_conn, expect):
    """A byte-equality hash cannot implement a case-insensitive collation.

    The keys decouple case from the suffix -- case comes from g%2, the suffix from
    (g/2)%10 -- so for every suffix 0..9 both 'host'N and 'HOST'N exist: twenty
    byte-distinct keys that case-fold to ten. A level-2 collation yields ten groups;
    a byte-equality path would yield twenty. The fixture therefore fails if the
    fallback ever stops happening, which a fixture whose case tracked its suffix
    would not.

    ITS OWN TEST, because `cannot_run` declares a whole test unrunnable while the
    shell suite's `check_skip` declines one check. Anything else in this function
    would be taken down with the refusal.
    """
    try:
        _exec(pgc_conn, "CREATE COLLATION ci "
                        "(provider = icu, locale = 'und-u-ks-level2', deterministic = false)")
    except psycopg.Error:
        expect.cannot_run(
            "MISSING_DEPENDENCY",
            "a case-insensitive collation needs a non-deterministic ICU collation, "
            "and a server built without ICU cannot create one",
            # At the call, not behind a constant: the grader resolves string
            # literals at the call site.
            name="the non-deterministic collation case")
        return

    _exec(pgc_conn, "CREATE TABLE t_ci (k text COLLATE ci, v int) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO t_ci SELECT "
                    "(CASE WHEN g % 2 = 0 THEN 'host' ELSE 'HOST' END) || ((g / 2) % 10), g "
                    "FROM generate_series(1, 5000) g")
    _groupvec(pgc_conn, True)
    # THE QUERY THE PLAN ARM ASKS ABOUT (#1339). The answer used to come from
    # `SELECT count(*) FROM (SELECT k FROM t_ci GROUP BY k) s`: a GROUP BY with no
    # aggregate, which always falls back, so both sides were core and a node grouping
    # by bytes could not change it. The rows of `k, count(*) ... GROUP BY k` are
    # counted here instead, against the 10 the name states and the case-folded count.
    # BEFORE the plan arm, so a node that takes the query AND groups by bytes shows
    # the wrong answer rather than stopping at the plan.
    groups = len(_rows(pgc_conn, "SELECT k, count(*) FROM t_ci GROUP BY k"))
    folded = _one(pgc_conn, "SELECT count(DISTINCT lower(k)) FROM t_ci")
    expect.text(f"{groups}|{folded}", "10|10",
                "answer: non-deterministic collation grouping is case-insensitive (10 groups)")
    expect.plan_marker(_plan(pgc_conn, "SELECT k, count(*) FROM t_ci GROUP BY k"),
                       MARKER, absent=True,
                       name="plan: non-deterministic collation key falls back")


# ---- the two reproduced wrong-answer blockers --------------------------------

def test_sum_real_matches_heap_rather_than_returning_zero(pgc_conn, expect):
    """Blocker 1: a float8 Datum handed to a float4 slot made sum(real) return 0.

    The values are integer-valued reals, so the sum is exact whatever the fold
    order and the heap mirror can judge it directly. The nonzero arm is not
    redundant: a mirror comparison is satisfied by both sides returning 0, and 0
    was the defect.
    """
    _exec(pgc_conn, "CREATE TABLE r_col (h int, r real, d float8) USING pgcolumnar")
    _exec(pgc_conn, "CREATE TABLE r_heap (h int, r real, d float8)")
    _exec(pgc_conn, "INSERT INTO r_heap SELECT g % 4, (g % 7)::real, (g % 7)::float8 "
                    "FROM generate_series(1, 4000) g")
    _exec(pgc_conn, "INSERT INTO r_col SELECT * FROM r_heap")
    _groupvec(pgc_conn, True)

    expect.plan_marker(_plan(pgc_conn, "SELECT h, sum(r) FROM r_col GROUP BY h"),
                       MARKER, name="regress B1: sum(real) fires the node")
    expect.rows(_rows(pgc_conn, "SELECT h, sum(r), sum(d) FROM r_col GROUP BY h"),
                _rows(pgc_conn, "SELECT h, sum(r), sum(d) FROM r_heap GROUP BY h"),
                "regress B1: sum(real) matches heap, not 0")
    zeroes = _rows(pgc_conn, "SELECT bool_and(s <> 0) FROM "
                             "(SELECT sum(r) s FROM r_col GROUP BY h) x")
    expect.rows(zeroes, [("True",)], "regress B1: sum(real) is nonzero")


def test_a_float_sum_that_overflows_raises_as_core_does(pgc_conn, expect):
    """Two finite float8s, or two finite reals, whose sum is infinite raise 22003 in
    core's float8pl and float4pl (#1341 item 50). The node's accumulators must raise
    it too rather than return Infinity. Judged by SQLSTATE, against core with the
    node turned off."""
    _exec(pgc_conn, "CREATE TABLE ovs_col (k int, d float8, r real) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO ovs_col VALUES (1, 1e308, 3e38), (1, 1e308, 3e38)")

    def state(on, sql):
        _groupvec(pgc_conn, on)
        try:
            _one(pgc_conn, sql)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"

    _groupvec(pgc_conn, True)
    fires = "|".join("yes" if MARKER in str(_plan(pgc_conn, f"SELECT k, sum({c}) FROM ovs_col GROUP BY k")) else "no"
                     for c in ("d", "r"))
    expect.text(fires, "yes|yes", "sum(float8) and sum(real) overflow: node fires")
    got = "|".join(state(on, f"SELECT k, sum({c}) FROM ovs_col GROUP BY k")
                   for on in (True, False) for c in ("d", "r"))
    _groupvec(pgc_conn, True)
    expect.text(got, "22003|22003|22003|22003", "sum(float8) and sum(real) overflow raise 22003, as core does")


def test_a_gating_where_is_honoured_rather_than_dropped(pgc_conn, expect):
    """Blocker 2: a pseudoconstant one-time filter was dropped, so a false gate
    wrongly returned rows. The node cannot honour a one-time filter, so it must
    fall back -- and either way the answer must match heap."""
    _make_pair(expect, pgc_conn)
    expect.plan_marker(
        _plan(pgc_conn, "SELECT host, count(*) FROM t_col WHERE (SELECT false) GROUP BY host"),
        MARKER, absent=True, name="regress B2: gating WHERE falls back (not the node)")
    expect.rows(
        _rows(pgc_conn, "SELECT host, count(*) FROM t_col WHERE (SELECT false) GROUP BY host"),
        _rows(pgc_conn, "SELECT host, count(*) FROM t_heap WHERE (SELECT false) GROUP BY host"),
        "regress B2: gating WHERE (SELECT false) returns no rows",
        allow_empty="a false one-time filter returns no rows, and that IS the property")
    expect.rows(
        _rows(pgc_conn, "SELECT host, count(*), sum(i4) FROM t_col WHERE (SELECT true) GROUP BY host"),
        _rows(pgc_conn, "SELECT host, count(*), sum(i4) FROM t_heap WHERE (SELECT true) GROUP BY host"),
        "regress B2: gating WHERE (SELECT true) is a no-op")


def test_avg_float8_overflows_the_way_core_overflows(pgc_conn, expect):
    """Core's float accumulator keeps Youngs-Cramer Sxx and raises on finite inputs
    whose sum of squares overflows even where the running sum stays finite. The node
    must raise too, and a row comparison cannot compare two errors."""
    _exec(pgc_conn, "CREATE TABLE ov_col (k int, d float8) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO ov_col VALUES (1, -1e308), (1, 1e308)")
    _groupvec(pgc_conn, True)
    expect.plan_marker(_plan(pgc_conn, "SELECT k, avg(d) FROM ov_col GROUP BY k"),
                       MARKER, name="avg(float8) overflow: node fires")
    raised = "no"
    try:
        _rows(pgc_conn, "SELECT k, avg(d) FROM ov_col GROUP BY k")
    except psycopg.Error as exc:
        raised = "yes" if "out of range" in str(exc).lower() else f"no [{exc}]"
    expect.text(raised, "yes", "avg(float8) overflow errors like core")


# ---- degenerate inputs -------------------------------------------------------

def test_degenerate_inputs_produce_no_groups(pgc_conn, expect):
    _make_pair(expect, pgc_conn)
    expect.rows(
        _rows(pgc_conn, "SELECT host, count(*) FROM t_col WHERE g < 0 GROUP BY host"),
        _rows(pgc_conn, "SELECT host, count(*) FROM t_heap WHERE g < 0 GROUP BY host"),
        "oracle exact: all rows filtered out -> 0 groups",
        allow_empty="a predicate matching nothing yields no groups, which IS the property")

    _exec(pgc_conn, "CREATE TABLE t_empty (k text, v int) USING pgcolumnar")
    expect.num(
        len(_rows(pgc_conn, "SELECT k, count(*) FROM t_empty GROUP BY k")), 0,
        "empty table: grouped scan yields 0 rows")


# ---- the path pays for the folding it does (#349) ----------------------------

def test_the_path_pays_for_the_folding_it_does(pgc_conn, expect):
    """The grouped path priced itself at the scan cost plus a fixed bump.

    Every competing plan pays a per-row aggregation cost and this one paid none, so
    it won by construction -- including against a parallel plan measured 1.9x faster
    than itself on a full-scan GROUP BY. The property asserted is the one that was
    observably wrong and that does not depend on the planner choosing any particular
    plan at fixture scale: the estimate must respond to how much folding the node
    will do.

    TWO THINGS ARE HELD CONSTANT HERE THAT THE SHELL SUITE VARIES, and the removal
    proof is what found the difference (#1162).

    `native_groupagg.sh` asks ten aggregates over ten DIFFERENT columns to cost more
    than one over one, and a scan projecting ten columns costs more than a scan
    projecting one whatever the folding charge is. Measured against a build whose
    folding charge no longer scales with the aggregate count -- the exact defect
    #349 fixed:

        distinct columns, 1 agg    350.50   350.50    (control, mutant)
        distinct columns, 10 aggs 1253.01   803.01

    803.01 is still greater than 350.50, so the arm stays green against the defect.
    The 452.51 that survives is the scan's projection cost, not folding.

    Ten aggregates over the SAME column hold the projection fixed, and the path is
    forced so both arms really are the grouped node rather than whatever the
    planner prefers once the charge is applied:

        one column, 1 agg          350.50   350.50
        one column, 10 aggs        800.50   350.50

    450.00 is cpu_operator_cost x 20000 rows x 9 extra aggregates, exactly the term
    under test, and it is the whole of the difference.
    """
    _exec(pgc_conn, "CREATE TABLE t_cost (host text, a int, b int, c int, d int, "
                    "e int, f int, g2 int, h int, i int, j int) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO t_cost SELECT 'h' || (n % 50), n, n, n, n, n, n, n, n, n, n "
                    f"FROM generate_series(1, {ROWS}) n")
    _exec(pgc_conn, "ANALYZE t_cost")
    # 450.00 in the docstring above is cpu_operator_cost x THIS NUMBER x 9. Asserted,
    # because a short load would leave the whole explanation describing another table.
    expect.num(_one(pgc_conn, "SELECT count(*) FROM t_cost"), ROWS,
               "premise: the cost fixture holds the rows the 450.00 is computed from")
    _groupvec(pgc_conn, True)
    # The only grouping path left, so both arms are the node under test and
    # neither is core's Agg wearing its cost.
    _exec(pgc_conn, "SET enable_hashagg = off")
    _exec(pgc_conn, "SET enable_sort = off")

    one_q = "SELECT host, avg(a) FROM t_cost GROUP BY host"
    ten_q = f"SELECT host, {','.join(['avg(a)'] * 10)} FROM t_cost GROUP BY host"
    # The premise the two costs below are costs OF. Without it a planner that
    # stopped choosing the node would have the arms comparing core's Agg with
    # itself, which is monotone in the aggregate count for reasons of its own.
    expect.plan_marker(_plan(pgc_conn, one_q), MARKER,
                       name="premise: one aggregate is costed as the grouped node")
    expect.plan_marker(_plan(pgc_conn, ten_q), MARKER,
                       name="premise: and so is ten, so the two costs are comparable")

    one, ten = _top_cost(pgc_conn, one_q), _top_cost(pgc_conn, ten_q)
    print(f"-- grouped path cost: 1 aggregate {one}, 10 aggregates {ten}")
    # A cost EXPLAIN did not produce, or produced as zero, makes the comparison
    # below meaningless -- and `0 > 0` is a clean-looking false.
    expect.text("yes" if one and ten else f"no [1={one!r} 10={ten!r}]", "yes",
                "premise: the grouped path is costed at all (non-empty estimate)")
    expect.text("yes" if ten > one else f"no [1={one} 10={ten}]", "yes",
                "ten aggregates cost more than one (#349)")


# ---- the group-estimate bound (#369) -----------------------------------------

GB_ROWS = 300000
# 300000 * 2160us = 648s = 10.8 minutes, so 11 minute buckets plus the two partials
# the bound allows.
GB_WINDOW = "WHERE ts >= '2026-01-01' AND ts < '2026-01-01 00:10:48'"


def _gb_session(conn):
    _groupvec(conn, True)
    _exec(conn, "SET pgcolumnar.enable_parallel_vector_agg = on")
    _exec(conn, "SET max_parallel_workers_per_gather = 4")


def test_the_group_estimate_bound_is_accurate_and_narrow(pgc_conn, expect):
    """`estimate_num_groups` cannot see through a function.

    For `date_trunc('minute', ts)` it falls back to the timestamp column's
    ndistinct, which is near the row count -- 19,996,000 against 720 actual on the
    20M fixture. That number is charged twice on the parallel arm, by the Gather and
    by the Finalize, and not at all on the serial node, so the serial node won by
    construction on shapes where the arm is four times faster.

    Three things are asserted and the middle one is the one that matters: the bound
    is ACCURATE rather than merely smaller, an INFORMED estimate is untouched, and a
    query with no Const bound on the key gets no bound from us.
    """
    _exec(pgc_conn, "CREATE TABLE gb (ts timestamptz, host text, v float8) "
                    "USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO gb SELECT "
                    "'2026-01-01'::timestamptz + (g * interval '2160 microseconds'), "
                    "'h'||(g % 4000), random()*100 "
                    f"FROM generate_series(1,{GB_ROWS}) g")
    _exec(pgc_conn, "ANALYZE gb")
    _gb_session(pgc_conn)

    bound = _estimate(pgc_conn, "SELECT date_trunc('minute', ts) b, avg(v) FROM gb "
                                f"{GB_WINDOW} GROUP BY b")
    expect.num(bound, 12, "the bound is accurate for a truncating time key, not just smaller")

    # An accurate bound is worthless if it is not a bound. Counted, not reasoned.
    actual = int(_rows(pgc_conn,
                       "SELECT count(*) FROM (SELECT date_trunc('minute', ts) "
                       f"FROM gb {GB_WINDOW} GROUP BY 1) z")[0][0])
    print(f"-- true group count in the window: {actual}")
    # AGAINST THE ESTIMATE MEASURED, not the literal the arm above expects (#1339): a
    # bound that fell below the true count failed only the arm above, and this one,
    # comparing with 12, still said yes.
    expect.text("yes" if actual <= bound else f"no [{actual} groups over a bound of {bound}]", "yes",
                "and it is an UPPER bound: the true group count is at or below it")

    # A plain Var has statistics, so examine_variable reports informed and the
    # substitution must not run at all.
    plain = _estimate(pgc_conn, "SELECT host, avg(v) FROM gb GROUP BY host")
    print(f"-- informed estimate for a plain column: {plain}")
    expect.text("yes" if 3900 <= plain <= 4100 else f"no [{plain}]", "yes",
                "an informed estimate (plain column) is not replaced")

    # A MISSING END OF THE RANGE COMES FROM THE ZONE MAPS (#1292). This used to assert
    # that a key with no Const bound gets no bound at all. The owner chose on #1292 to
    # gate the grouped path on this estimate, and a gate cannot use one that is 60x to
    # 3600x high (measured there), while a dashboard query bounds only one side. Each
    # case asserts both directions: at least the true count, because under-estimating
    # is the harmful direction, and at most four times it.
    def within(sql):
        est = _estimate(pgc_conn, sql)
        act = int(_rows(pgc_conn, f"SELECT count(*) FROM ({sql}) z")[0][0])
        ok = act >= 1 and act <= est <= 4 * act
        return "ok" if ok else f"estimate {est} for {act} groups"

    expect.text(within("SELECT date_trunc('minute', ts) b, avg(v) FROM gb GROUP BY b"),
                "ok", "with no time bound at all, a truncating key is bounded from the zone maps")
    expect.text(within("SELECT date_trunc('minute', ts) b, avg(v) FROM gb "
                       "WHERE ts >= '2026-01-01' GROUP BY b"),
                "ok", "with only a lower time bound, a truncating key is still bounded")
    expect.text(within("SELECT date_bin('1 minute', ts, timestamptz '2026-01-01') b, avg(v) "
                       f"FROM gb {GB_WINDOW} GROUP BY b"),
                "ok", "a date_bin key is bounded as a truncating key is")
    expect.text(within("SELECT date_bin('5 minutes', ts, timestamptz '2026-01-01') b, avg(v) "
                       "FROM gb GROUP BY b"),
                "ok", "and a date_bin key with no time bound is bounded from the zone maps")

    # A user function with date_trunc's SHAPE must not get date_trunc's BOUND.
    # PL/pgSQL because an SQL function would be inlined and lose the shape. Matching
    # on shape alone estimated this at 722 against 499,999 actual -- a 692x
    # UNDER-estimate, the direction that under-prices the arm and under-sizes the
    # Finalize's hash table.
    _exec(pgc_conn, "CREATE FUNCTION gb_hi_card(t text, s timestamptz) RETURNS text AS "
                    "$$ BEGIN RETURN t || md5(s::text); END $$ LANGUAGE plpgsql IMMUTABLE")
    lookalike = _estimate(pgc_conn, "SELECT gb_hi_card('minute', ts) b, avg(v) FROM gb "
                                    f"{GB_WINDOW} GROUP BY b")
    expect.text("yes" if lookalike > 1000 else f"no [{lookalike}]", "yes",
                "a lookalike function with the same signature gets NO bound")

    nontime = _estimate(pgc_conn, "SELECT upper(host) b, avg(v) FROM gb GROUP BY b")
    expect.text("yes" if nontime > 100 else f"no [{nontime}]", "yes",
                "a non-time expression key gets no bound either")


def test_the_bound_reads_the_zone_maps_in_one_catalog_scan(pgc_conn, pgc_cluster, expect):
    """The range costs one catalog scan, not one per row group (#1304 review).

    The first version probed zone_map_pkey once per group, on every plan: 109 -> 313
    planning buffers at 67 groups, about 3 per group. Counted as the planning
    transaction's own scans, which no other session can move, on the table AND its
    index, because a catalog index scan counts on the index. No WHERE, so the
    filter's survival estimate, which samples zone maps on purpose, is not counted.
    """
    _exec(pgc_conn, "CREATE TABLE gbz (ts timestamptz, v float8) USING pgcolumnar")
    _exec(pgc_conn, "SELECT pgcolumnar.set_options('gbz', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 1000)")
    _exec(pgc_conn, "INSERT INTO gbz SELECT '2026-01-01'::timestamptz + g * interval '1 second', g "
                    "FROM generate_series(1,50000) g")
    _exec(pgc_conn, "ANALYZE gbz")
    _gb_session(pgc_conn)

    groups = int(_one(pgc_conn, "SELECT count(*) FROM pgcolumnar.row_group "
                                "WHERE storage_id = pgcolumnar.get_storage_id('gbz')"))
    # The fallback is what this measures: since #1304 option 5 a complete column
    # extent bounds the range with no zone_map scan (test_column_extent.py asserts
    # 0), and without one the zone-map read must stay one scan.
    _exec(pgc_conn, "DELETE FROM pgcolumnar.column_extent "
                    "WHERE storage_id = pgcolumnar.get_storage_id('gbz')")
    # ON A FRESH CONNECTION. The "xact" counters are this backend's pending stats,
    # which are flushed to shared memory at most about once a second, not at
    # transaction end -- so in pgc_conn they still held the INSERT's own zone_map
    # reads (the column extent's first recompute, at its commit), and read 3 for a
    # plan that scanned once. The shell twin starts a new psql, so it never saw them.
    with psycopg.connect(pgc_cluster.dsn(dbname=_one(pgc_conn, "SELECT current_database()"))) as m:
        _exec(m, "SELECT set_config('search_path', %s, false)" % (
            "'" + _one(pgc_conn, "SELECT current_setting('search_path')").replace("'", "''") + "'"))
        _gb_session(m)
        _exec(m, "EXPLAIN SELECT date_trunc('minute', ts) b, avg(v) FROM gbz GROUP BY b")
        scans = int(_one(m,
                         "SELECT pg_stat_get_xact_numscans('pgcolumnar.zone_map'::regclass) "
                         "+ pg_stat_get_xact_numscans('pgcolumnar.zone_map_pkey'::regclass)"))
        m.rollback()
    print(f"-- {groups} row groups, {scans} zone_map scans while planning")
    expect.at_least(groups, 40,
                    "premise: the fixture has many row groups, so one scan and one per group differ")
    expect.num(scans, 1, "planning a bucketed GROUP BY scans zone_map once, not once per row group")


def test_a_mixed_timestamp_predicate_never_narrows_the_bound(pgc_conn, expect):
    """PostgreSQL has cross-type operators, so a `timestamp` Const compared against a
    `timestamptz` column stays bare and reaches the extraction instead of being
    wrapped in a cast. Both are int64 microseconds on different scales -- wall clock
    against UTC -- so under a non-UTC TimeZone the range would be computed across the
    offset. The session TimeZone is set to one with a non-zero offset deliberately:
    under UTC the two scales coincide and the arm cannot fail.
    """
    _exec(pgc_conn, "CREATE TABLE gbm (ts timestamptz, v float8) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO gbm SELECT "
                    "'2026-01-01 00:00:00+00'::timestamptz + (g * interval '86400 microseconds'), "
                    "random() FROM generate_series(1,200000) g")
    _exec(pgc_conn, "ANALYZE gbm")
    _exec(pgc_conn, "SET TimeZone='Asia/Kolkata'")
    _gb_session(pgc_conn)

    # THE PROPERTY THE MIXED-TYPE RULE PROTECTS, stated directly (#1292). This used to
    # assert "gets NO bound", the observable sign that the mixed predicate was unused.
    # With the zone maps supplying a range there is a bound either way, so the arm now
    # asserts that a wall-clock window read as UTC never narrows the estimate below the
    # truth. 04:00-08:00 Kolkata is 22:30-02:30 UTC, 150 minutes of this data; read as
    # UTC it would clip the data to 04:00-04:48, about 50.
    mixed_sql = ("SELECT date_trunc('minute',ts) b, avg(v) FROM gbm "
                 "WHERE ts >= timestamp '2026-01-01 04:00' "
                 "AND ts < timestamp '2026-01-01 08:00' GROUP BY b")
    mixed = _estimate(pgc_conn, mixed_sql)
    mixed_actual = int(_rows(pgc_conn, f"SELECT count(*) FROM ({mixed_sql}) z")[0][0])
    matching = _estimate(pgc_conn,
                         "SELECT date_trunc('minute',ts) b, avg(v) FROM gbm "
                         "WHERE ts >= timestamptz '2026-01-01 00:00+00' "
                         "AND ts < timestamptz '2026-01-01 12:00+00' GROUP BY b")
    print(f"-- mixed-type predicate {mixed} for {mixed_actual} groups, "
          f"matching-type predicate {matching}")
    expect.at_least(mixed_actual, 100, "premise: the mixed-type window holds data")
    expect.text("yes" if mixed >= mixed_actual
                else f"no (estimate {mixed} for {mixed_actual} groups)", "yes",
                "a mixed timestamp/timestamptz predicate never narrows the estimate below the true count")
    expect.text("yes" if matching < 5000 else f"no [{matching}]", "yes",
                "while a matching-type predicate still gets one")


# ---- on by default, gated on an upper bound (#1292 phase 2) -----------------

def _gate_only(conn, cap):
    """Hash aggregation and sorting off, so the grouped path wins whenever the gate
    offers it: the arms below test the gate, not the cost model. A declined query
    still runs, on the disabled plan."""
    _exec(conn, "SET enable_hashagg = off")
    _exec(conn, "SET enable_sort = off")
    _exec(conn, f"SET pgcolumnar.groupagg_max_groups = {cap}")


def _gate_reset(conn):
    for name in ("enable_hashagg", "enable_sort", "pgcolumnar.groupagg_max_groups"):
        _exec(conn, f"RESET {name}")


def _under_cap(conn, cap, sql):
    _gate_only(conn, cap)
    try:
        return _plan(conn, sql)
    finally:
        _gate_reset(conn)


def _groups_or_error(conn, cap, sql):
    """The number of groups the query returns under that cap, or its error. The query
    runs itself: wrapped in count(*), the planner drops the unused aggregates and the
    node refuses that shape, so a wrapper would measure a different plan."""
    _gate_only(conn, cap)
    try:
        return str(len(_rows(conn, sql)))
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)
    finally:
        _gate_reset(conn)


def test_on_by_default_and_gated_on_an_upper_bound(pgc_conn, expect):
    """The grouped path is on by default, and taken only when a TRUE UPPER BOUND on
    the group count (the live row count, or the time buckets the zone maps allow)
    is within groupagg_max_groups. A gate on the estimate let a skewed key through
    and errored (@OffgridwithJD on #1330); a bound never does.

    Independent of the shell suite's section 10: its own generators; the numbers the
    names state are shared.
    """
    conn = pgc_conn
    expect.text(_one(conn, "SELECT boot_val || '|' || setting FROM pg_settings"
                           " WHERE name = 'pgcolumnar.enable_group_vectorization'"), "on|on",
                "the grouped path is on by default")
    gen = ("SELECT '2026-05-01 00:00:00+00'::timestamptz + g * interval '648 ms',"
           " ((g * 7) % 200000)::int, (g % 777)::int FROM generate_series(1, 400000) g")
    for name, am in (("gate_c", "pgcolumnar"), ("gate_hp", "heap")):
        _exec(conn, f"CREATE TABLE {name} (ts timestamptz, k int, i int) USING {am}")
        _exec(conn, f"INSERT INTO {name} {gen}")
        _exec(conn, f"ANALYZE {name}")
    bucket = ("SELECT date_bin('5 minutes', ts, '2026-01-01 00:00:00+00') b, count(*), sum(i)"
              " FROM gate_c GROUP BY b")
    expect.plan_marker(_plan(conn, bucket), MARKER,
                       name="by default a five-minute bucket query takes the grouped path")
    expect.plan_marker(_under_cap(conn, 100000, bucket), MARKER,
                       name="a five-minute bucket query over more rows than the cap still takes the grouped path")
    high = "SELECT k, count(*), sum(i) FROM {} GROUP BY k"
    expect.plan_marker(_under_cap(conn, 100000, high.format("gate_c")), MARKER, absent=True,
                       name="a plain key over more rows than the cap is declined")
    _exec(conn, "SET pgcolumnar.groupagg_max_groups = 100000")
    declined = _rows(conn, high.format("gate_c"))
    _exec(conn, "RESET pgcolumnar.groupagg_max_groups")
    expect.rows(declined, _rows(conn, high.format("gate_hp")),
                "and the declined query answers what heap answers")
    expect.plan_marker(_under_cap(conn, 10000000, high.format("gate_c")), MARKER,
                       name="raising groupagg_max_groups to 10,000,000 lets the same query take the grouped path")

    # 95% of 800,000 rows on key 0 and the rest unique: 40,001 groups, which a fresh
    # ANALYZE puts far lower. Under a cap of 30,000 the row count declines it.
    _exec(conn, "CREATE TABLE gate_skew (k bigint, v int) USING pgcolumnar")
    _exec(conn, "INSERT INTO gate_skew SELECT CASE WHEN g % 20 = 7 THEN g * 3 ELSE 0 END, g"
                " FROM generate_series(1, 800000) g")
    _exec(conn, "ANALYZE gate_skew")
    nd = _one(conn, "SELECT n_distinct FROM pg_stats WHERE tablename = 'gate_skew' AND attname = 'k'")
    expect.text("below" if nd is not None and 0 <= nd <= 3000 else str(nd), "below",
                "premise: ANALYZE estimates the skewed key's 40,001 groups below 3,000")
    skq = "SELECT k, count(*), sum(v) FROM gate_skew GROUP BY k"
    taken = MARKER in str(_under_cap(conn, 30000, skq))
    expect.text(f"{1 if taken else 0}|{_groups_or_error(conn, 30000, skq)}", "0|40001",
                "a skewed key whose estimate is far too low is declined under a cap of 30,000, and answers every group")


def _plan_text(conn, sql):
    return json.dumps(_one(conn, f"EXPLAIN (COSTS OFF, FORMAT JSON) {sql}"))


def _cached_under(conn, name, sql, cap):
    """PREPARE name AS sql, plan it at a first EXECUTE under the default cap, then
    EXECUTE it again under `cap`. -> its rows, as _rows returns them, or the error."""
    _exec(conn, f"PREPARE {name} AS {sql}")
    try:
        _rows(conn, f"EXECUTE {name}")
        _exec(conn, f"SET pgcolumnar.groupagg_max_groups = {cap}")
        return _rows(conn, f"EXECUTE {name}")
    except psycopg.Error as exc:
        return [(query_error(exc.diag.message_primary),)]
    finally:
        _exec(conn, "RESET pgcolumnar.groupagg_max_groups")
        _exec(conn, f"DEALLOCATE {name}")


def test_past_the_cap_the_node_hands_off_to_a_core_finalize(pgc_conn, expect):
    """#1300: a grouped plan whose aggregates all have a partial state is a Partial
    node under a core Finalize, so a plan that outlives its bound (the cap lowered
    after planning, or the table grown past it) flushes its table at the cap
    instead of erroring, and the Finalize combines the duplicates. An aggregate
    with no partial state keeps the serial node, and the runtime cap as its
    backstop. The shell's section 11; this file's own rows.
    """
    conn = pgc_conn
    gen = ("SELECT '2026-06-01 00:00:00+00'::timestamptz + g * interval '512 ms',"
           " ((g * 13) % 200000)::int, (g % 641)::int FROM generate_series(1, 400000) g")
    for name, am in (("ho_c", "pgcolumnar"), ("ho_h", "heap")):
        _exec(conn, f"CREATE TABLE {name} (ts timestamptz, k int, i int) USING {am}")
        _exec(conn, f"INSERT INTO {name} {gen}")
        _exec(conn, f"ANALYZE {name}")
    high = "SELECT k, count(*), sum(i) FROM {} GROUP BY k"

    _exec(conn, f"PREPARE gk AS {high.format('ho_c')}")
    plan = _plan_text(conn, "EXECUTE gk")
    expect.text(str(int('"Finalize"' in plan and '"Hashed"' in plan) + int(MARKER in plan)), "2",
                "a cached plan made under the default cap is a Partial grouped node under a core Finalize")
    # a run that errors at the cap reports no flush line, so the arm reads red, by name
    try:
        _rows(conn, "EXECUTE gk")
        _exec(conn, "SET pgcolumnar.groupagg_max_groups = 1000")
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF, FORMAT JSON) EXECUTE gk")
            analyzed = json.dumps(cur.fetchone()[0])
    except psycopg.Error as exc:
        analyzed = query_error(exc.diag.message_primary)
    _exec(conn, "RESET pgcolumnar.groupagg_max_groups")
    _exec(conn, "DEALLOCATE gk")
    flushes = re.search(r'"Columnar Group Table Flushes": (\d+)', analyzed)
    expect.text("yes" if flushes and int(flushes.group(1)) > 0 else f"no ({flushes and flushes.group(1)})",
                "yes", "and its EXPLAIN ANALYZE reports the group table flushed at the cap")
    expect.rows(_cached_under(conn, "gk", high.format("ho_c"), 1000), _rows(conn, high.format("ho_h")),
                "a cached plan run under a lower cap hands off at the cap and answers what heap answers")
    minmax = "SELECT k, min(i), max(i), count(i) FROM {} GROUP BY k"
    expect.rows(_cached_under(conn, "gm", minmax.format("ho_c"), 1000), _rows(conn, minmax.format("ho_h")),
                "min and max hand off too: a cached min/max plan under a lower cap answers what heap answers")

    # The table grown past the cap under a cached plan: 400 keys plan under a cap of
    # 1,000, then 6,000 more commit before the plan runs again.
    for name, am in (("ho_gc", "pgcolumnar"), ("ho_gh", "heap")):
        _exec(conn, f"CREATE TABLE {name} (k int, i int) USING {am}")
        _exec(conn, f"INSERT INTO {name} SELECT g, g * 3 FROM generate_series(1, 400) g")
    _exec(conn, "SET pgcolumnar.groupagg_max_groups = 1000")
    try:
        _exec(conn, "PREPARE gg AS SELECT k, count(*), sum(i) FROM ho_gc GROUP BY k")
        _rows(conn, "EXECUTE gg")
        for name in ("ho_gc", "ho_gh"):
            _exec(conn, f"INSERT INTO {name} SELECT g, g * 3 FROM generate_series(401, 6400) g")
        still = MARKER in _plan_text(conn, "EXECUTE gg")
        try:
            grown = _rows(conn, "EXECUTE gg")
        except psycopg.Error as exc:
            grown = [(query_error(exc.diag.message_primary),)]
    finally:
        _exec(conn, "RESET pgcolumnar.groupagg_max_groups")
        _exec(conn, "DEALLOCATE gg")
    expect.text(str(int(still)), "1", "premise: the cached plan is still the grouped node after the table grew")
    expect.rows(grown, _rows(conn, "SELECT k, count(*), sum(i) FROM ho_gh GROUP BY k"),
                "a table grown past the cap under a cached plan hands off and answers what heap answers")

    # sum(int8) is a numeric sum, with no partial state the node emits.
    _exec(conn, "CREATE TABLE ho_b (k int, b bigint) USING pgcolumnar")
    _exec(conn, "INSERT INTO ho_b SELECT g % 200000, g FROM generate_series(1, 400000) g")
    _exec(conn, "ANALYZE ho_b")
    sum8 = "SELECT k, sum(b) FROM ho_b GROUP BY k"
    _exec(conn, f"PREPARE gk8 AS {sum8}")
    plan8 = _plan_text(conn, "EXECUTE gk8")
    got = "no error"
    try:
        _rows(conn, "EXECUTE gk8")
        _exec(conn, "SET pgcolumnar.groupagg_max_groups = 1000")
        _rows(conn, "EXECUTE gk8")
    except psycopg.Error as exc:
        d = exc.diag
        got = ("1" if "exceeded pgcolumnar.groupagg_max_groups (1000)" in (d.message_primary or "") else "0") + "|" + \
              ("1" if (d.message_hint or "").startswith("Raise pgcolumnar.groupagg_max_groups") else "0")
    _exec(conn, "RESET pgcolumnar.groupagg_max_groups")
    _exec(conn, "DEALLOCATE gk8")
    expect.text(f"{int(MARKER in plan8)}|{int(chr(34) + 'Finalize' + chr(34) in plan8)}|{got}", "1|0|1|1",
                "a cached plan whose aggregates cannot hand off still stops at the runtime cap, with its hint")


def _planned_under_cap(conn, name, sql, cap=1000):
    """-> (1 when the cached plan is the grouped node, its rows under `cap`), planned at a
    first EXECUTE under the default cap. The plan is read too, so a query the node
    declined cannot pass by matching heap."""
    _exec(conn, f"PREPARE {name} AS {sql}")
    try:
        planned = int(MARKER in _plan_text(conn, f"EXECUTE {name}"))
    finally:
        _exec(conn, f"DEALLOCATE {name}")
    return planned, _cached_under(conn, name, sql, cap)


def test_min_and_max_hand_off_over_float8_nan(pgc_conn, expect):
    """#1764 review: min and max across a flush where order is not byte order. NaN
    sorts above every number. 2,999 groups of ten rows, each in two runs of five on
    either side of a flush, so the node compares within a run and the Finalize
    combines the runs: with one row per flush the node never compared two values."""
    conn = pgc_conn
    for name, am in (("hmf_c", "pgcolumnar"), ("hmf_h", "heap")):
        _exec(conn, f"CREATE TABLE {name} (k int, f float8) USING {am}")
        _exec(conn, f"INSERT INTO {name} SELECT (g / 5) % 2999, CASE WHEN g % 7 = 0 THEN 'NaN'::float8"
                    " ELSE g::float8 END FROM generate_series(1, 29990) g")
    _exec(conn, "ANALYZE hmf_c")
    q = "SELECT k, min(f), max(f), count(f) FROM {} GROUP BY k"
    planned, got = _planned_under_cap(conn, "gmf", q.format("hmf_c"))
    expect.text(str(planned), "1", "premise: the float8 min/max plan is the grouped node")
    expect.rows(got, _rows(conn, q.format("hmf_h")),
                "min and max hand off over float8 NaN: a cached plan under a lower cap answers what heap answers")


def test_min_and_max_hand_off_over_text_under_an_icu_collation(pgc_conn, expect):
    """#1764 review: the same over text under en-x-icu, where 'a' < 'B', the reverse of
    byte order. ITS OWN TEST, because it declines whole on a server without ICU."""
    conn = pgc_conn
    if _one(conn, "SELECT count(*) FROM pg_collation WHERE collname = 'en-x-icu'") != 1:
        expect.cannot_run(
            "MISSING_DEPENDENCY", "this server has no ICU collation en-x-icu",
            name="min and max hand off over text under an ICU collation: a cached plan under a lower cap answers what heap answers")
        return
    for name, am in (("hmt_c", "pgcolumnar"), ("hmt_h", "heap")):
        _exec(conn, f'CREATE TABLE {name} (k int, t text COLLATE "en-x-icu") USING {am}')
        _exec(conn, f"INSERT INTO {name} SELECT (g / 5) % 2999, CASE WHEN g % 2 = 0 THEN 'a' || g"
                    " ELSE 'B' || g END FROM generate_series(1, 29990) g")
    _exec(conn, "ANALYZE hmt_c")
    q = "SELECT k, min(t), max(t) FROM {} GROUP BY k"
    planned, got = _planned_under_cap(conn, "gmt", q.format("hmt_c"))
    expect.text(str(planned), "1", "premise: the ICU text min/max plan is the grouped node")
    expect.rows(got, _rows(conn, q.format("hmt_h")),
                "min and max hand off over text under an ICU collation: a cached plan under a lower cap answers what heap answers")


def _peak_kb(conn, table):
    """-> the node's reported peak memory in kB under a cap of 1,000, or a query_error."""
    sql = f"SELECT k, count(*), sum(i) FROM {table} GROUP BY k"
    _exec(conn, f"PREPARE pm AS {sql}")
    try:
        _rows(conn, "EXECUTE pm")
        _exec(conn, "SET pgcolumnar.groupagg_max_groups = 1000")
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF, FORMAT JSON) EXECUTE pm")
            plan = json.dumps(cur.fetchone()[0])
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)
    finally:
        _exec(conn, "RESET pgcolumnar.groupagg_max_groups")
        _exec(conn, "DEALLOCATE pm")
    m = re.search(r'"Columnar Group Table Peak Memory": (\d+)', plan)
    return int(m.group(1)) if m else None


def test_the_cap_bounds_the_nodes_memory(pgc_conn, expect):
    """#1764 review: the answers are the same whether a flush frees what the table held
    or keeps it, so the node's reported peak is read. Under a cap of 1,000, ten times
    the distinct keys must not take much more: a flush that kept its keys and states
    held 1.4 GB more at 2M keys, with every answer right."""
    conn = pgc_conn
    for name, n in (("hmem_a", 20000), ("hmem_b", 200000)):
        _exec(conn, f"CREATE TABLE {name} (k text, i int) USING pgcolumnar")
        _exec(conn, f"INSERT INTO {name} SELECT md5(g::text) || md5((g * 7)::text), g"
                    f" FROM generate_series(1, {n}) g")
        _exec(conn, f"ANALYZE {name}")
    a, b = _peak_kb(conn, "hmem_a"), _peak_kb(conn, "hmem_b")
    ok = isinstance(a, int) and isinstance(b, int) and a > 0 and b <= 2 * a
    expect.text("yes" if ok else f"no ({a} kB at 20,000 keys, {b} kB at 200,000)", "yes",
                "under a cap of 1,000, ten times the distinct keys keep the node's peak memory within twice")


def test_a_null_key_counts_as_a_group_in_the_bucket_bound(pgc_conn, expect):
    """@OffgridwithJD's shape on #1330: three minute buckets fill the bound exactly
    (lo 00:00:59, hi 00:02:00: floor(61 s / 60 s) + 2 = 3), and NULL rows add a
    fourth group. Under a cap of 3, a bound without the NULL group takes the path
    and errors. This file's own rows: other second offsets, 40 rows a group.
    """
    conn = pgc_conn
    _exec(conn, "CREATE TABLE nul_b (ts timestamptz, v int) USING pgcolumnar")
    for ts in ("'2026-03-01 10:00:59+00'", "'2026-03-01 10:01:05+00'", "'2026-03-01 10:02:00+00'", "NULL"):
        _exec(conn, f"INSERT INTO nul_b SELECT {ts}, g FROM generate_series(1, 40) g")
    # DECLINED, NOT ONLY ANSWERED (#1339). The answer alone held for a node that took
    # the query and did not stop at its cap, so the plan is read too.
    q = "SELECT date_trunc('minute', ts) b, count(*) FROM nul_b GROUP BY b"
    # the plan comes back as EXPLAIN's JSON, a list of dicts, so search its text: a
    # membership test on the list itself is always false
    taken = 1 if MARKER in json.dumps(_under_cap(conn, 3, q)) else 0
    expect.text(f"{taken}|{_groups_or_error(conn, 3, q)}", "0|4",
                "a NULL key counts as a group in the bucket bound: four groups under a cap of 3 are declined and all answered")

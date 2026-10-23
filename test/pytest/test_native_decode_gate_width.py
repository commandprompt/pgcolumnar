"""Phase-2 decode gating runs only on a projection wide enough to pay for it (#595,
#1341). The pytest twin of `test/native_decode_gate_width.sh`, every check by the
shell's name.

#452 phase 2 evaluates an unprunable qual per vector so a vector no row passes skips
its payload decode; it pays only when there is payload to spare. So it runs only when
the projection has at least pgcolumnar.qual_skipvec_min_payload_cols non-qual columns
(default 20). The policy is pinned by moving the threshold across a fixed projection:
a wide SELECT * (24 payload columns) gates at the default and stops once the setting is
above 24, and gates again at exactly 24 (the >= boundary); a narrow three-column
projection does not gate at the default and does once the setting is 0. "Columnar
Vector Decodes" is the observable: VECTORS when gated, every column of every vector
when not. The shell sets the threshold per database because each helper opens a new
connection; this side holds one connection and sets it for the session.
"""
import re

import psycopg

ROWS = 32768
VECTORS = 32
PCOLS = 24
NCOLS_WIDE = PCOLS + 1
NARROW_NCOLS = 3
SURVIVING_VECTORS = 1
DEFAULT_GUC = 20

COLDEFS = ", ".join(f"p{i} int" for i in range(1, PCOLS + 1))
COLVALS = ", ".join(f"g + {i - 1}" for i in range(1, PCOLS + 1))
WIDE_NOMATCH = "SELECT * FROM nw WHERE tag LIKE '%zzzzz%'"
WIDE_NEEDLE = "SELECT * FROM nw WHERE tag LIKE '%needle%'"
WIDE_CONTROL = "SELECT * FROM nw WHERE tag LIKE '%row%'"
NARROW_NOMATCH = "SELECT tag, p1, p2 FROM nw WHERE tag LIKE '%zzzzz%'"
NARROW_CONTROL = "SELECT tag, p1, p2 FROM nw WHERE tag LIKE '%row%'"
NARROW_NEEDLE = "SELECT tag, p1, p2 FROM nw WHERE tag LIKE '%needle%'"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _show_state(conn, name):
    """-> the SQLSTATE SHOW raised, or 'shown'. 42704 is undefined_object."""
    try:
        _one(conn, f"SHOW {name}")
        return "shown"
    except psycopg.Error as exc:
        return exc.sqlstate or "no SQLSTATE"


def _plan(conn, sql):
    return "\n".join(r[0] for r in _rows(conn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}"))


def _counter(plan, label):
    for line in plan.splitlines():
        if label in line:
            m = re.search(r"\d+", line[line.index(label) + len(label):])
            return int(m.group(0)) if m else None
    return None


def _has(plan, label):
    return "yes" if label in plan else "no"


def _set_gate(conn, v):
    """A REFUSED SET IS LEFT FOR THE PREMISE THAT READS IT BACK (#1339). The shell's
    ALTER DATABASE fails quietly and its premise reads what stuck; raising here ended
    the test unnamed when the GUC's range lost the value (0, or the width)."""
    with conn.cursor() as cur:
        try:
            cur.execute(f"SET pgcolumnar.qual_skipvec_min_payload_cols = {v}")
        except psycopg.Error:
            pass


def _reset_gate(conn):
    with conn.cursor() as cur:
        cur.execute("RESET pgcolumnar.qual_skipvec_min_payload_cols")


def _gate_now(conn):
    return int(_one(conn, "SHOW pgcolumnar.qual_skipvec_min_payload_cols"))


def test_the_width_gate(pgc_conn, expect):
    conn = pgc_conn
    gen = (f"SELECT 'row' || CASE WHEN g <= 1024 THEN 'needle' ELSE 'miss' END, {COLVALS} "
           f"FROM generate_series(1, {ROWS}) g")
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE hw (tag text, {COLDEFS})")
        cur.execute(f"CREATE TABLE nw (tag text, {COLDEFS}) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('nw', stripe_row_limit => 65536, chunk_group_row_limit => 1024)")
        cur.execute(f"INSERT INTO hw {gen}")
        cur.execute(f"INSERT INTO nw {gen}")

    expect.num(_gate_now(conn), DEFAULT_GUC,
               f"the width-gate GUC exists and defaults to the shipped {DEFAULT_GUC}")
    expect.text(_show_state(conn, "pgcolumnar.qual_skipvec_probe_vecs"), "42704",
                "premise: the rejected probe GUC qual_skipvec_probe_vecs was NOT shipped")
    expect.text(_show_state(conn, "pgcolumnar.qual_skipvec_min_skip_pct"), "42704",
                "premise: the rejected probe GUC qual_skipvec_min_skip_pct was NOT shipped")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.row_group "
                          "WHERE storage_id = pgcolumnar.get_storage_id('nw')"), 1,
               "one row group, so every count below is one group's")

    wide_needle = _plan(conn, WIDE_NEEDLE)
    narrow_needle = _plan(conn, NARROW_NEEDLE)
    narrow_nomatch = _plan(conn, NARROW_NOMATCH)
    expect.text(_has(wide_needle, "Columnar Projected Columns"), "yes",
                "the wide plan is a columnar custom scan with projection")
    expect.text(_has(narrow_nomatch, "Columnar Projected Columns"), "yes",
                "the narrow plan is a columnar custom scan with projection")
    expect.num(_counter(wide_needle, "Columnar Usable Skip Predicates"), 0,
               "the LIKE is unprunable (no usable skip predicate)")
    expect.num(_counter(wide_needle, "Columnar Chunk Groups Removed by Filter"), 0,
               "no whole row group is pruned")
    expect.num(_counter(wide_needle, "Columnar Vectors Ruled Out by Value"), 0,
               "no vector is ruled out by VALUE (that is 1b-ii's path, not phase 2)")
    expect.text(_has(wide_needle, "Columnar Rows Filtered Before Materialization"), "yes",
                "the wide query takes the late-materialization path")
    expect.text(_has(narrow_needle, "Columnar Rows Filtered Before Materialization"), "yes",
                "the narrow query takes the late-materialization path")
    expect.text(_has(narrow_nomatch, "Columnar Vector Decodes"), "yes",
                "EXPLAIN reports Columnar Vector Decodes (the observable)")
    # The fixture's shape, read from the heap mirror the same generator filled (#1339):
    # read from the columnar table, a one-line reader change reddened these premises.
    expect.num(_one(conn, "SELECT count(*) FROM hw WHERE tag LIKE '%needle%'"), 1024,
               "premise: the needle matches exactly one vector's worth of rows")
    expect.num(_one(conn, "SELECT count(*) FROM hw WHERE tag LIKE '%zzzzz%'"), 0,
               "premise: the nomatch predicate matches no row")
    expect.num(_one(conn, "SELECT count(*) FROM hw WHERE tag LIKE '%row%'"), ROWS,
               "premise: the control matches every row")

    # arm 1: at the shipped default, wide gates and narrow does not
    expect.num(_gate_now(conn), DEFAULT_GUC, "still at the default before arm 1")
    wide_nomatch = _plan(conn, WIDE_NOMATCH)
    expect.num(_counter(wide_nomatch, "Columnar Vector Decodes"), VECTORS,
               "wide, default: a zero-match LIKE decodes the qual column and NO payload -> gated")
    expect.num(_counter(wide_nomatch, "Columnar Vectors Skipped"), VECTORS,
               "wide, default: and reports the payload vectors skipped")
    wide_needle_d = _plan(conn, WIDE_NEEDLE)
    expect.num(_counter(wide_needle_d, "Columnar Vector Decodes"), VECTORS + (NCOLS_WIDE - 1) * SURVIVING_VECTORS,
               "wide, default: a one-vector LIKE decodes the qual column plus payload for that vector only")
    expect.num(_counter(wide_needle_d, "Columnar Vectors Skipped"), VECTORS - SURVIVING_VECTORS,
               "wide, default: skips exactly the vectors holding no match")
    expect.num(_counter(_plan(conn, NARROW_NOMATCH), "Columnar Vector Decodes"), VECTORS * NARROW_NCOLS,
               "narrow, default: a zero-match LIKE decodes EVERY projected column of every vector -> NOT gated")

    # arm 2: the gate off, and the same narrow projection gates
    _set_gate(conn, 0)
    expect.num(_gate_now(conn), 0, "the width gate is now disabled (0)")
    expect.num(_counter(_plan(conn, NARROW_NOMATCH), "Columnar Vector Decodes"), VECTORS,
               "narrow, gate off (GUC=0): the SAME narrow projection now gates -> qual column alone")
    expect.num(_counter(_plan(conn, WIDE_CONTROL), "Columnar Vector Decodes"), VECTORS * NCOLS_WIDE,
               "control, gate forced on: a matches-everything wide qual skips no vector (full decode)")
    expect.num(_counter(_plan(conn, NARROW_CONTROL), "Columnar Vector Decodes"), VECTORS * NARROW_NCOLS,
               "control, gate forced on: a matches-everything narrow qual skips no vector (full decode)")

    # arm 3: above the wide projection's width it stops; at exactly its width it gates
    _set_gate(conn, NCOLS_WIDE)
    expect.num(_gate_now(conn), NCOLS_WIDE, "the width threshold is now above the projection width")
    expect.num(_counter(_plan(conn, WIDE_NOMATCH), "Columnar Vector Decodes"), VECTORS * NCOLS_WIDE,
               "wide, gate raised above width: the SAME wide SELECT * stops gating -> full decode")
    _set_gate(conn, PCOLS)
    expect.num(_gate_now(conn), PCOLS, "the width threshold now equals the projection width")
    expect.num(_counter(_plan(conn, WIDE_NOMATCH), "Columnar Vector Decodes"), VECTORS,
               "wide, gate AT the width (>= boundary): gates again -> qual column alone")

    # correctness: the gate never changes a result
    _reset_gate(conn)
    expect.num(_gate_now(conn), DEFAULT_GUC, "back to the shipped default")
    gated = _rows(conn, WIDE_NEEDLE)
    expect.row_set(gated, _rows(conn, "SELECT * FROM hw WHERE tag LIKE '%needle%'"),
                   "correctness: wide needle, gate ON, matches the heap oracle")
    _set_gate(conn, NCOLS_WIDE)
    expect.num(_gate_now(conn), NCOLS_WIDE, "gate suppressed for the invariance arm")
    expect.row_set(_rows(conn, WIDE_NEEDLE), gated,
                   "correctness: wide needle, gate OFF, returns the identical rows (output-invariant)")
    expect.row_set(_rows(conn, NARROW_NEEDLE), _rows(conn, "SELECT tag, p1, p2 FROM hw WHERE tag LIKE '%needle%'"),
                   "correctness: narrow needle matches the heap either way")
    expect.row_set(_rows(conn, "SELECT * FROM nw"), _rows(conn, "SELECT * FROM hw"),
                   "correctness: the full scan matches the heap")
    _reset_gate(conn)

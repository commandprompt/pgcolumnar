"""An unprunable qual gates payload-column decode per vector (#452 phase 2, #1341).
The pytest twin of `test/native_decode_gating.sh`, every check by the shell's name.

A leading-wildcard LIKE is never a skip predicate, so the reader cannot rule a vector
out by value; phase 2 evaluates the executor qual per 1024-row vector between the two
decode passes, and a vector no row passes is never decoded for its payload columns.
"Columnar Vector Decodes" counts (vector, column) decodes, so the saving is exact
arithmetic. The filter counters must stay exact too: a rejection is counted once,
inside a surviving vector as well as a skipped one, and a deleted row is not counted at
all. The width gate (#595) is set off for this session, as the shell pins it off for
its cluster, so the five-column projection still gates.
"""
import re

ROWS = 32768
VECTORS = 32            # ROWS / 1024
NCOLS = 6               # tag plus five payload columns
SURVIVING_VECTORS = 1   # the needle lives only in the first vector
MATCHES = 1024
MROWS = 49152


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _run(conn, *sqls):
    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


def _plan(conn, sql):
    return "\n".join(r[0] for r in _rows(conn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}"))


def _counter(plan, label):
    """-> the first integer on the plan's line holding label, or None."""
    for line in plan.splitlines():
        if label in line:
            m = re.search(r"\d+", line[line.index(label) + len(label):]) or re.search(r"\d+", line)
            return int(m.group(0)) if m else None
    return None


def _has(plan, label):
    return "yes" if label in plan else "no"


NEEDLEQ = "SELECT * FROM n WHERE tag LIKE '%needle%'"
NOMATCHQ = "SELECT * FROM n WHERE tag LIKE '%zzzzz%'"
CONTROLQ = "SELECT * FROM n WHERE tag LIKE '%row%'"
HALFQ = "SELECT * FROM n WHERE tag LIKE '%needle%' AND p1 % 2 = 0"


def test_an_unprunable_qual_gates_payload_decode(pgc_conn, expect):
    conn = pgc_conn
    gen = (f"SELECT 'row' || CASE WHEN g <= 1024 THEN 'needle' ELSE 'miss' END, "
           f"g, g + 1, g + 2, g + 3, g + 4 FROM generate_series(1, {ROWS}) g")
    _run(conn, "SET pgcolumnar.qual_skipvec_min_payload_cols = 0",
         "CREATE TABLE h (tag text, p1 int, p2 int, p3 int, p4 int, p5 int)",
         "CREATE TABLE n (tag text, p1 int, p2 int, p3 int, p4 int, p5 int) USING pgcolumnar",
         "SELECT pgcolumnar.set_options('n', stripe_row_limit => 65536, chunk_group_row_limit => 1024)",
         f"INSERT INTO h {gen}", f"INSERT INTO n {gen}")
    needle, nomatch, control = _plan(conn, NEEDLEQ), _plan(conn, NOMATCHQ), _plan(conn, CONTROLQ)

    expect.text(_has(needle, "Columnar Projected Columns"), "yes", "premise: the needle plan is a columnar custom scan")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.row_group "
                          "WHERE storage_id = pgcolumnar.get_storage_id('n')"), 1,
               "premise: one row group, so every count below is one group's")
    expect.text(_one(conn, "SHOW pgcolumnar.qual_skipvec_min_payload_cols"), "0",
                "premise: the width gate is pinned off, so this narrow projection still gates")
    expect.num(_counter(needle, "Columnar Usable Skip Predicates"), 0,
               "premise: the reader has no usable skip predicate for this LIKE")
    expect.num(_counter(needle, "Columnar Chunk Groups Removed by Filter"), 0, "premise: no whole row group is pruned")
    expect.num(_counter(needle, "Columnar Vectors Ruled Out by Value"), 0,
               "premise: no vector is ruled out by VALUE either (that is 1b-ii's path)")
    expect.text(_has(needle, "Columnar Rows Filtered Before Materialization"), "yes",
                "premise: the needle query takes the late-materialization path")
    expect.num(_one(conn, "SELECT count(*) FROM n WHERE tag LIKE '%needle%'"), MATCHES,
               "premise: the needle really matches one vector's worth of rows")
    expect.num(_one(conn, "SELECT count(*) FROM n WHERE tag LIKE '%zzzzz%'"), 0,
               "premise: the nomatch predicate matches no row")
    expect.num(_one(conn, "SELECT count(*) FROM n WHERE tag LIKE '%row%'"), ROWS,
               "premise: the control matches every row")

    expect.text(_has(control, "Columnar Vector Decodes"), "yes", "EXPLAIN reports Columnar Vector Decodes")
    expect.num(_counter(control, "Columnar Vector Decodes"), VECTORS * NCOLS,
               "the control decodes every column of every vector")
    expect.num(_counter(nomatch, "Columnar Vector Decodes"), VECTORS,
               "a zero-matching LIKE decodes the qual column and no payload column")
    expect.num(_counter(needle, "Columnar Vector Decodes"), VECTORS + (NCOLS - 1) * SURVIVING_VECTORS,
               "a one-vector LIKE decodes the qual column plus payload for that vector only")

    expect.num(_counter(needle, "Rows Removed by Filter"), ROWS - MATCHES,
               "the needle removes exactly the non-matching rows, counted once")
    expect.num(_counter(needle, "Columnar Rows Filtered Before Materialization"), ROWS - MATCHES,
               "and reports them all filtered before materialization")
    expect.num(_counter(needle, "Columnar Vectors Skipped"), VECTORS - SURVIVING_VECTORS,
               "the needle skips exactly the vectors holding no match")
    expect.num(_counter(nomatch, "Rows Removed by Filter"), ROWS,
               "the zero-match query removes every row by filter, counted once")
    expect.num(_counter(nomatch, "Columnar Vectors Skipped"), VECTORS, "and skips every vector")

    # Every row of the needle's surviving vector matches, so a producer that counted
    # its re-test's rejections twice reads right above; half of vector 0 passes here.
    expect.text(_one(conn, "SELECT count(*) || '|' || count(DISTINCT (p1 - 1) / 1024) FROM n "
                           "WHERE tag LIKE '%needle%' AND p1 % 2 = 0"), "512|1",
                "premise: the half-needle survives in one vector, half of its rows")
    expect.num(_counter(_plan(conn, HALFQ), "Rows Removed by Filter"), ROWS - 512,
               "rejections inside a surviving vector are counted once")

    expect.row_set(_rows(conn, NEEDLEQ), _rows(conn, "SELECT * FROM h WHERE tag LIKE '%needle%'"),
                   "the needle query returns exactly the matching rows")
    expect.num(_one(conn, "SELECT count(*) FROM n WHERE tag LIKE '%zzzzz%'"), 0, "the nomatch query returns nothing")
    expect.rows(_rows(conn, "SELECT count(*), sum(p1), sum(p5) FROM n WHERE tag LIKE '%row%'"),
                _rows(conn, "SELECT count(*), sum(p1), sum(p5) FROM h WHERE tag LIKE '%row%'"),
                "the control returns every row, values intact")


def test_deleted_rows_across_several_groups(pgc_conn, expect):
    """The evaluator reads the CURRENT group's delete mask; an earlier version read the
    previous group's, freed one load earlier. It needs more than one group, deletes,
    and the unprunable qual at once. A deleted row is not counted as filtered."""
    conn = pgc_conn
    mgen = (f"SELECT 'x' || CASE WHEN g % 4096 = 7 THEN 'needle' ELSE 'miss' END || g::text, g, g * 2 "
            f"FROM generate_series(1, {MROWS}) g")
    _run(conn, "SET pgcolumnar.qual_skipvec_min_payload_cols = 0",
         "CREATE TABLE hm (tag text, p1 int, p2 int)",
         "CREATE TABLE m (tag text, p1 int, p2 int) USING pgcolumnar",
         "SELECT pgcolumnar.set_options('m', stripe_row_limit => 8192, chunk_group_row_limit => 1024)",
         f"INSERT INTO hm {mgen}", f"INSERT INTO m {mgen}",
         "DELETE FROM hm WHERE p1 % 17 = 0",
         # the fixture's DELETE must not depend on the gating under test
         "SET pgcolumnar.qual_skipvec_min_payload_cols = 100000", "DELETE FROM m WHERE p1 % 17 = 0",
         "SET pgcolumnar.qual_skipvec_min_payload_cols = 0")
    expect.text(str(_one(conn, "SELECT count(*) > 1 FROM pgcolumnar.row_group "
                               "WHERE storage_id = pgcolumnar.get_storage_id('m')")), "True",
                "premise: the columnar table spans more than one row group")
    # count(*), not a count WHERE p1 % 17 = 0: that WHERE would run through the gating
    expect.num(_one(conn, "SELECT count(*) FROM m"), MROWS - MROWS // 17,
               "premise: rows really were deleted (so the mask is non-NULL)")
    expect.row_set(_rows(conn, "SELECT * FROM m WHERE tag LIKE '%needle%'"),
                   _rows(conn, "SELECT * FROM hm WHERE tag LIKE '%needle%'"),
                   "deletes + multiple groups + unprunable qual: columnar matches the heap")
    expect.row_set(_rows(conn, "SELECT * FROM m"), _rows(conn, "SELECT * FROM hm"),
                   "and the full scan across every group's mask matches too")
    expect.num(_counter(_plan(conn, "SELECT * FROM m WHERE tag LIKE '%needle%'"), "Rows Removed by Filter"),
               _one(conn, "SELECT count(*) FROM hm WHERE tag NOT LIKE '%needle%'"),
               "rows removed by filter across deleted rows count only live rows")

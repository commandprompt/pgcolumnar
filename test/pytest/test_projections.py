"""Port of test/projections.sh -- the multiple-projections DDL, catalog and read path.

A PROJECTION IS A SECOND COPY OF SOME COLUMNS, and every property here is about the
copy staying honest: it holds the rows the base holds, it loses the rows the base
loses, it survives a vacuum that renumbers every row underneath it, and the planner
only reads it when it can answer the whole query from it.

That makes a wrong projection a WRONG ANSWER rather than a slow one. A scan that reads
a stale projection returns rows the base no longer has, and nothing downstream
re-checks. So the suite is organised by what can make the copy diverge:

    CATALOG    what add_projection records, and what it refuses to record
    FAN-OUT    a write reaching the projection, including deletes
    RECONSTRUCT columns the projection does NOT store, fetched from the base by row
               number -- the linkage that makes a partial projection usable
    PLANNER    when a covering projection is chosen, and when it must not be
    REBUILD    vacuum compacts the base into new row numbers; the projection must
               follow
    MVCC       an old snapshot must not see rows committed after it, through a
               projection scan as much as through the base
    LIFECYCLE  a dropped table's declaration, and a projection added or dropped
               mid-transaction (#304, #875)

FOUR DELIBERATE DIFFERENCES IN MECHANISM, each asserting the same property by a
stronger means.

1.  `pgc_set_hash` becomes `expect.row_set`. The bash compares
    `md5(string_agg(t ORDER BY t))`, which is order-blind by construction; `row_set`
    is order-blind by declaration. A hash mismatch says two hashes differ, a row-set
    mismatch says which row.

2.  `expect_fail` becomes `expect.sqlstate`. The original's helper passes when the
    statement errors AT ALL, so a typo in a table name satisfies it just as well as
    the refusal it is named for. Every code here was MEASURED against this build
    rather than guessed, and they are all distinct:

        duplicate name          42710      add on heap table       42809
        unknown column          42703      drop base               22023
        empty columns           22023      drop unknown            42704
        duplicate column        42701      read_projection base    42704
        sort key not in columns 22023

    This is the one place the port is strictly stronger than its original, and it is
    worth saying which way: the names are the bash suite's, the assertions are not.

3.  The EXPLAIN grep becomes a typed JSON field. `grep -c 'Columnar Projection: pc'`
    is a substring test over text; the plan carries `"Columnar Projection": "pc"` as
    a property, so the port reads the value. For the two NEGATIVE arms it uses
    `expect.plan_marker(absent=True)`, which refuses an empty plan -- a plan that
    never arrived looks exactly like a plan with no projection, and that is the worst
    place for a silent pass.

4.  The second session is a second connection, not a psql on a fifo. The bash drives a
    background `psql -f fifo` and waits by polling its output file for a token, up to
    20 seconds. A second `psycopg` connection makes the wait unnecessary rather than
    shorter: the query returns when it returns. Its two TIMEOUT arms
    (`session A opened snapshot`, `session A responded post-commit`) exist in the
    original only to name the failure, and are carried here as the positive
    assertions they are the negative of.

ARRAYS ARE CAST TO ::text IN SQL. `psycopg` returns a PostgreSQL array as a Python
list, so `{1,2,3}` arrives as `[1, 2, 3]` and a transcribed comparison against the
bash suite's expected `{1,2,3}` fails for a reason that has nothing to do with
projections. Casting in SQL keeps both harnesses reading the same string the server
produced.
"""

import pytest

# The original's magic numbers, named once. Where the bash suite writes 5000 twice and
# 20000 four times, a changed fixture size that moves one and not the other produces an
# arm that still passes and no longer tests what it says.
CATALOG_ROWS = 100
FANOUT_ROWS = 5000
PLANNER_ROWS = 20000
STRIPE_LIMIT = 2000
MULTISTRIPE_ROWS = 7000


# --------------------------------------------------------------------------- helpers

def _one(cur, sql, params=None):
    cur.execute(sql, params)
    row = cur.fetchone()
    return None if row is None else row[0]


def _rows(cur, sql, params=None):
    """-> every row, IN THE ORDER THE SERVER RETURNED THEM.

    Not sorted here. Which comparison is wanted is the assertion's business, and
    sorting in the fetch helper is how a set claim silently becomes an ordered one or
    the reverse.
    """
    cur.execute(sql, params)
    return cur.fetchall()


def _proj(cur, sid, field, where=""):
    """The original's `proj_q`: one field of one projection row, by storage id."""
    return _one(cur, f"SELECT {field} FROM pgcolumnar.projection "
                     f"WHERE storage_id = {sid} {where}")


def _sid(cur, table):
    return _one(cur, "SELECT pgcolumnar.get_storage_id(%s)", (table,))


def _plan(cur, sql):
    """The plan as parsed JSON, so an assertion reads a typed field rather than text."""
    return _one(cur, f"EXPLAIN (COSTS OFF, FORMAT JSON) {sql}")


def _projection_in_plan(plan):
    """-> the value of the plan's `Columnar Projection` property, or None.

    The NAME, not merely the presence: the bash arm greps for `Columnar Projection:
    pc` and a port that only asserted the property exists would pass for a plan that
    chose a DIFFERENT projection. `plan_marker` deliberately tests presence of a key
    rather than equality of a value, because its usual subject is a count that varies
    -- this value is a name and does not.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "Columnar Projection" in node:
                found.append(node["Columnar Projection"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found[0] if found else None


def _read_projection(cur, table, name):
    """The projection's rows as the server renders them: columns joined by '|'."""
    return _rows(cur, "SELECT pgcolumnar.read_projection(%s, %s)", (table, name))


# ==================== CATALOG: what add_projection records
#
# The base projection is recorded LAZILY -- it does not exist until the first real
# projection is added, at which point both appear. That is why the "no rows before
# first add" arm is not a tautology: it pins that the catalog is empty rather than
# holding a base row nobody asked for.

@pytest.fixture
def cat(pgc_conn):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE p (a int, b text, c int) USING pgcolumnar")
        cur.execute("INSERT INTO p SELECT g, 'r'||g, g*2 FROM generate_series(1,%s) g",
                    (CATALOG_ROWS,))
    return pgc_conn


def test_the_catalog_is_empty_until_the_first_projection_is_added(cat, expect):
    with cat.cursor() as cur:
        expect.num(_one(cur, "SELECT count(*) FROM p"), CATALOG_ROWS,
                   "table populated")
        sid = _sid(cur, "p")
        expect.text("ok" if sid is not None else "missing", "ok",
                    "storage id resolves")
        expect.num(_proj(cur, sid, "count(*)"), 0,
                   "no projection rows before first add")


def test_the_first_add_records_the_base_and_the_new_projection(cat, expect):
    with cat.cursor() as cur:
        cur.execute("SELECT pgcolumnar.add_projection('p','p1',"
                    "ARRAY['a','c'],ARRAY['c'])")
        sid = _sid(cur, "p")
        expect.num(_proj(cur, sid, "count(*)"), 2,
                   "two rows after first add (base + p1)")

        # ::text on every array, so both harnesses compare the string the server
        # produced rather than a Python list against a brace literal.
        expect.text(_proj(cur, sid, "columns::text", "AND projection_id = 0"),
                    "{1,2,3}", "base id 0 columns are all attrs")
        expect.text(_proj(cur, sid, "sort_key::text", "AND projection_id = 0"),
                    "{}", "base id 0 sort_key empty")
        expect.text(_proj(cur, sid, "name", "AND projection_id = 0"),
                    "base", "base id 0 name")
        expect.text(str(_proj(cur, sid, "proj_storage_id = storage_id",
                              "AND projection_id = 0")), "True",
                    "base proj_storage_id == base")

        expect.num(_proj(cur, sid, "projection_id", "AND name = 'p1'"), 1,
                   "p1 id is 1")
        expect.text(_proj(cur, sid, "columns::text", "AND name = 'p1'"),
                    "{1,3}", "p1 columns")
        expect.text(_proj(cur, sid, "sort_key::text", "AND name = 'p1'"),
                    "{3}", "p1 sort_key")
        expect.text(str(_proj(cur, sid, "proj_storage_id <> storage_id",
                              "AND name = 'p1'")), "True",
                    "p1 has its own storage id")


def test_a_second_projection_may_have_no_sort_key(cat, expect):
    with cat.cursor() as cur:
        cur.execute("SELECT pgcolumnar.add_projection('p','p1',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("SELECT pgcolumnar.add_projection('p','p2',ARRAY['b'])")
        sid = _sid(cur, "p")
        expect.num(_proj(cur, sid, "projection_id", "AND name = 'p2'"), 2,
                   "p2 id is 2")
        expect.text(_proj(cur, sid, "columns::text", "AND name = 'p2'"),
                    "{2}", "p2 columns")
        expect.text(_proj(cur, sid, "sort_key::text", "AND name = 'p2'"),
                    "{}", "p2 sort_key empty")
        expect.num(_one(cur, "SELECT count(DISTINCT proj_storage_id) "
                             f"FROM pgcolumnar.projection WHERE storage_id = {sid}"),
                   3, "distinct storage ids")


# ==================== CATALOG: what it refuses, and with which code
#
# THE ORIGINAL ASSERTS ONLY THAT THE STATEMENT ERRORED. Its `expect_fail` runs the SQL
# and passes on any non-zero exit, so a misspelt table name satisfies every one of
# these eight. The codes below were measured against this build and are distinct, so
# each arm now names the refusal it is for.

REFUSALS = [
    ("duplicate name rejected", "42710",
     "SELECT pgcolumnar.add_projection('p','p1',ARRAY['a'])"),
    ("unknown column rejected", "42703",
     "SELECT pgcolumnar.add_projection('p','px',ARRAY['zzz'])"),
    ("empty columns rejected", "22023",
     "SELECT pgcolumnar.add_projection('p','pe',ARRAY[]::text[])"),
    ("duplicate column rejected", "42701",
     "SELECT pgcolumnar.add_projection('p','pd',ARRAY['a','a'])"),
    ("sort key not in columns", "22023",
     "SELECT pgcolumnar.add_projection('p','ps',ARRAY['a'],ARRAY['b'])"),
    ("drop base rejected", "22023",
     "SELECT pgcolumnar.drop_projection('p','base')"),
    ("drop unknown rejected", "42704",
     "SELECT pgcolumnar.drop_projection('p','nope')"),
]


@pytest.mark.parametrize("name,code,sql", REFUSALS,
                         ids=[r[0] for r in REFUSALS])
def test_a_bad_projection_is_refused_by_its_own_code(cat, expect, name, code, sql):
    import psycopg
    with cat.cursor() as cur:
        cur.execute("SELECT pgcolumnar.add_projection('p','p1',"
                    "ARRAY['a','c'],ARRAY['c'])")
        with pytest.raises(psycopg.Error) as exc:
            cur.execute(sql)
        expect.sqlstate(exc.value, code, name)


def test_a_projection_on_a_heap_table_is_refused(cat, expect):
    """SEPARATE, because it needs a heap table the other refusals do not, and because
    the refusal is about the ACCESS METHOD rather than about the arguments."""
    import psycopg
    with cat.cursor() as cur:
        cur.execute("CREATE TABLE h (x int)")
        with pytest.raises(psycopg.Error) as exc:
            cur.execute("SELECT pgcolumnar.add_projection('h','ph',ARRAY['x'])")
        expect.sqlstate(exc.value, "42809", "add on heap table rejected")


# ==================== DROP, and the back-fill that follows a late add

def test_drop_removes_one_projection_and_leaves_the_rest(cat, expect):
    with cat.cursor() as cur:
        cur.execute("SELECT pgcolumnar.add_projection('p','p1',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("SELECT pgcolumnar.add_projection('p','p2',ARRAY['b'])")
        cur.execute("SELECT pgcolumnar.drop_projection('p','p1')")
        sid = _sid(cur, "p")
        expect.num(_proj(cur, sid, "count(*)", "AND name = 'p1'"), 0,
                   "p1 gone after drop")
        expect.num(_proj(cur, sid, "count(*)"), 2, "base + p2 remain")
        expect.num(_one(cur, "SELECT count(*) FROM p"), CATALOG_ROWS,
                   "table still readable after DDL")


def test_a_projection_added_late_is_back_filled_from_the_existing_rows(cat, expect):
    """p2 is added AFTER the table already holds rows, so its storage must be filled
    from them. Without the back-fill it would be empty and every later fan-out arm
    would still pass."""
    with cat.cursor() as cur:
        cur.execute("SELECT pgcolumnar.add_projection('p','p2',ARRAY['b'])")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('p','p2')"),
                   CATALOG_ROWS, "back-fill: p2 populated from existing rows")
        expect.row_set(_read_projection(cur, "p", "p2"),
                       _rows(cur, "SELECT b FROM p"),
                       "back-fill: p2 matches base (b column)")


# ==================== FAN-OUT: a write reaches the projection
#
# Declared BEFORE the load, so these arms are about the write path rather than the
# back-fill above.

@pytest.fixture
def fanout(pgc_conn):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE fo (a int, b text, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('fo','fp',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("SELECT pgcolumnar.add_projection('fo','fq',ARRAY['b'])")
        cur.execute("INSERT INTO fo SELECT g, 'r'||g, (g*7)%%100 "
                    "FROM generate_series(1,%s) g", (FANOUT_ROWS,))
    return pgc_conn


def test_a_write_fans_out_to_every_projection(fanout, expect):
    with fanout.cursor() as cur:
        expect.row_set(_read_projection(cur, "fo", "fp"),
                       _rows(cur, "SELECT a::text || '|' || c::text FROM fo"),
                       "fp fan-out matches base (a,c)")
        expect.row_set(_read_projection(cur, "fo", "fq"),
                       _rows(cur, "SELECT b FROM fo"),
                       "fq fan-out matches base (b)")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('fo','fp')"),
                   _one(cur, "SELECT count(*) FROM fo"),
                   "fp row count matches base")


def test_projection_chunks_carry_skip_metadata(fanout, expect):
    """A sorted projection is only worth choosing if its chunks carry min/max, which is
    what lets the scan skip. Without it the projection is read end to end and the
    planner arm below would still pass."""
    with fanout.cursor() as cur:
        n = _one(cur,
                 "SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id = "
                 "(SELECT proj_storage_id FROM pgcolumnar.projection "
                 " WHERE storage_id = pgcolumnar.get_storage_id('fo') AND name='fp') "
                 "AND minimum IS NOT NULL")
        expect.text("yes" if n >= 1 else "no", "yes",
                    "fp chunks carry min/max skip metadata")


def test_a_delete_reaches_the_projection_through_the_base_delete_vector(fanout, expect):
    """The projection has no delete vector of its own: liveness comes from the BASE. So
    a delete that never touches the projection's storage must still remove its rows
    from every read."""
    with fanout.cursor() as cur:
        cur.execute("DELETE FROM fo WHERE a BETWEEN 1000 AND 2000")
        expect.row_set(_read_projection(cur, "fo", "fp"),
                       _rows(cur, "SELECT a::text || '|' || c::text FROM fo"),
                       "fp reflects deletes (a,c)")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('fo','fp')"),
                   _one(cur, "SELECT count(*) FROM fo"),
                   "fp count after delete matches base")


def test_fan_out_spans_more_than_one_row_group(pgc_conn, expect):
    """One row group is the case where a fan-out bug cannot show: the projection's
    row numbering only has to agree with the base ACROSS groups."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE fo2 (a int, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('fo2', stripe_row_limit => %s)",
                    (STRIPE_LIMIT,))
        cur.execute("SELECT pgcolumnar.add_projection('fo2','fp2',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("INSERT INTO fo2 SELECT g, (g*13)%%1000 "
                    "FROM generate_series(1,%s) g", (MULTISTRIPE_ROWS,))
        expect.row_set(_read_projection(cur, "fo2", "fp2"),
                       _rows(cur, "SELECT a::text || '|' || c::text FROM fo2"),
                       "fp2 multi-stripe fan-out matches base")
        groups = _one(cur,
                      "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = "
                      "(SELECT proj_storage_id FROM pgcolumnar.projection "
                      " WHERE storage_id = pgcolumnar.get_storage_id('fo2') "
                      " AND name='fp2')")
        expect.text("yes" if groups >= 2 else "no", "yes",
                    "fp2 spans multiple projection row groups")


def test_the_base_projection_cannot_be_read_by_name(fanout, expect):
    """`base` names a catalog row, not something `read_projection` addresses."""
    import psycopg
    with fanout.cursor() as cur:
        with pytest.raises(psycopg.Error) as exc:
            cur.execute("SELECT pgcolumnar.read_projection('fo','base')")
        expect.sqlstate(exc.value, "42704", "read_projection base rejected")


# ==================== RECONSTRUCT: columns the projection does not store
#
# `rp` stores (a,c) and the base has (a,b,c), so reading b means going back to the base
# BY THE PROJECTION'S STORED ROW NUMBER. That linkage is the thing under test; the
# NULLs and the delete are there because a row number that drifts shows up first where
# rows are missing or values are absent.

@pytest.fixture
def recon(pgc_conn):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rc (a int, b text, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('rc','rp',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("INSERT INTO rc SELECT g, 'r'||g, (g*7)%%100 "
                    "FROM generate_series(1,%s) g", (FANOUT_ROWS,))
    return pgc_conn


def test_columns_the_projection_lacks_are_reconstructed_from_the_base(recon, expect):
    with recon.cursor() as cur:
        expect.row_set(
            _rows(cur, "SELECT pgcolumnar.reconstruct_via_projection('rc','rp')"),
            _rows(cur, "SELECT a::text || '|' || b || '|' || c::text FROM rc"),
            "reconstruct full row matches base")


def test_reconstruction_survives_deletes_and_nulls(recon, expect):
    with recon.cursor() as cur:
        cur.execute("INSERT INTO rc VALUES (99991, NULL, NULL), (99992, 'x', NULL)")
        cur.execute("DELETE FROM rc WHERE a BETWEEN 2000 AND 3000")
        expect.row_set(
            _rows(cur, "SELECT pgcolumnar.reconstruct_via_projection('rc','rp')"),
            _rows(cur, r"SELECT coalesce(a::text,'\N') || '|' || "
                       r"coalesce(b,'\N') || '|' || coalesce(c::text,'\N') FROM rc"),
            "reconstruct matches base after delete + NULLs")
        expect.num(
            _one(cur, "SELECT count(*) FROM "
                      "pgcolumnar.reconstruct_via_projection('rc','rp')"),
            _one(cur, "SELECT count(*) FROM rc"),
            "reconstruct row count matches base")


# ==================== PLANNER: when a covering projection is chosen
#
# A HEAP TABLE HOLDING THE SAME ROWS IS THE ORACLE. Asserting only that the plan chose
# the projection says nothing about the rows it returned, and the failure this pair
# exists for is a plan that looks right over a projection that is wrong.

@pytest.fixture
def planner(pgc_conn):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ps (a int, b text, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('ps','pc',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("INSERT INTO ps SELECT g, 'r'||g, (g*7)%%1000 "
                    "FROM generate_series(1,%s) g", (PLANNER_ROWS,))
        cur.execute("CREATE TABLE ps_h (a int, b text, c int) USING heap")
        cur.execute("INSERT INTO ps_h SELECT g, 'r'||g, (g*7)%%1000 "
                    "FROM generate_series(1,%s) g", (PLANNER_ROWS,))
    return pgc_conn


COVERING = "SELECT a, c FROM ps WHERE c BETWEEN 100 AND 200"
COVERING_H = "SELECT a, c FROM ps_h WHERE c BETWEEN 100 AND 200"


def test_a_covering_sort_key_query_reads_the_projection(planner, expect):
    with planner.cursor() as cur:
        expect.text(_projection_in_plan(_plan(cur, COVERING)), "pc",
                    "projection chosen for covering + sort-key query")
        expect.row_set(_rows(cur, COVERING), _rows(cur, COVERING_H),
                       "projection-scan results match heap oracle")
        # `rows`, not `text(str(...))`. Comparing the repr of two lists reports "these
        # two strings differ" where `rows` names the differing row -- the same weakness
        # as comparing hashes, wearing a Python spelling.
        expect.rows(_rows(cur, "SELECT count(*), sum(a) FROM ps "
                               "WHERE c BETWEEN 100 AND 200"),
                    _rows(cur, "SELECT count(*), sum(a) FROM ps_h "
                               "WHERE c BETWEEN 100 AND 200"),
                    "aggregate over projection scan matches oracle")


def test_the_guc_is_an_off_switch(planner, expect):
    with planner.cursor() as cur:
        cur.execute("SET pgcolumnar.enable_projection_scan=off")
        # absent=True rather than a None check, because it refuses an EMPTY plan:
        # a plan that never arrived looks exactly like one carrying no projection.
        expect.plan_marker(_plan(cur, COVERING), "Columnar Projection", absent=True,
                           name="GUC off: no projection scan")
        cur.execute("RESET pgcolumnar.enable_projection_scan")


def test_a_query_naming_an_uncovered_column_falls_back_to_the_base(planner, expect):
    """`b` is not in `pc`, so the projection cannot answer the query and must not be
    chosen. Choosing it anyway would drop the column, not merely cost more."""
    with planner.cursor() as cur:
        expect.plan_marker(
            _plan(cur, "SELECT a, b, c FROM ps WHERE c BETWEEN 100 AND 200"),
            "Columnar Projection", absent=True,
            name="non-covering query (references b) uses the base")


def test_a_projection_scan_reflects_deletes(planner, expect):
    with planner.cursor() as cur:
        cur.execute("DELETE FROM ps   WHERE a BETWEEN 5000 AND 6000")
        cur.execute("DELETE FROM ps_h WHERE a BETWEEN 5000 AND 6000")
        expect.row_set(_rows(cur, COVERING), _rows(cur, COVERING_H),
                       "projection scan matches oracle after delete")
        expect.row_set(_rows(cur, "SELECT a, c FROM ps"),
                       _rows(cur, "SELECT a, c FROM ps_h"),
                       "full-range projection scan matches oracle")


# ==================== REBUILD: vacuum renumbers every row underneath the projection
#
# `pgcolumnar.vacuum` compacts the base into FRESH STORAGE with new row numbers. A
# projection that survived unchanged would now be keyed to numbers that mean something
# else, which is why "still exists" and "still chosen" are not enough on their own and
# every arm here is paired with the heap oracle.

@pytest.fixture
def vac(pgc_conn):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pv (a int, b text, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('pv','pvp',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("INSERT INTO pv SELECT g, 'r'||g, (g*7)%%1000 "
                    "FROM generate_series(1,%s) g", (PLANNER_ROWS,))
        cur.execute("DELETE FROM pv WHERE a BETWEEN 5000 AND 8000")
        cur.execute("CREATE TABLE pv_h (a int, b text, c int) USING heap")
        cur.execute("INSERT INTO pv_h SELECT g, 'r'||g, (g*7)%%1000 "
                    "FROM generate_series(1,%s) g WHERE g NOT BETWEEN 5000 AND 8000",
                    (PLANNER_ROWS,))
    return pgc_conn


def test_vacuum_rebuilds_the_projection_against_the_compacted_base(vac, expect):
    with vac.cursor() as cur:
        cur.execute("SELECT pgcolumnar.vacuum('pv')")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.projection "
                             "WHERE storage_id = pgcolumnar.get_storage_id('pv') "
                             "AND name='pvp'"), 1,
                   "projection survives vacuum")
        expect.row_set(_read_projection(cur, "pv", "pvp"),
                       _rows(cur, "SELECT a::text || '|' || c::text FROM pv_h"),
                       "read_projection matches base after vacuum")
        expect.text(_projection_in_plan(
                        _plan(cur, "SELECT a, c FROM pv WHERE c BETWEEN 100 AND 200")),
                    "pvp", "planner still uses projection after vacuum")
        expect.row_set(_rows(cur, "SELECT a, c FROM pv WHERE c BETWEEN 100 AND 200"),
                       _rows(cur, "SELECT a, c FROM pv_h WHERE c BETWEEN 100 AND 200"),
                       "projection-scan matches oracle after vacuum")
        expect.row_set(
            _rows(cur, "SELECT pgcolumnar.reconstruct_via_projection('pv','pvp')"),
            _rows(cur, "SELECT a::text||'|'||b||'|'||c::text FROM pv_h"),
            "reconstruct (a,b,c) matches base after vacuum")


def test_a_second_vacuum_renumbers_again_and_stays_correct(vac, expect):
    """ONCE IS NOT THE PROPERTY. A rebuild that reads the pre-vacuum numbering is right
    the first time and wrong the second, so the suite vacuums twice."""
    with vac.cursor() as cur:
        cur.execute("SELECT pgcolumnar.vacuum('pv')")
        cur.execute("DELETE FROM pv   WHERE a BETWEEN 100 AND 200")
        cur.execute("DELETE FROM pv_h WHERE a BETWEEN 100 AND 200")
        cur.execute("SELECT pgcolumnar.vacuum('pv')")
        expect.row_set(_read_projection(cur, "pv", "pvp"),
                       _rows(cur, "SELECT a::text || '|' || c::text FROM pv_h"),
                       "projection matches base after second vacuum")


# ==================== MVCC: an old snapshot, through a projection scan
#
# The projection stripe list AND the base liveness check both have to use the QUERY
# snapshot. If either used a current one, a REPEATABLE READ transaction would see rows
# committed after it -- through the projection only, which is the case no single-session
# test can reach.
#
# A SECOND CONNECTION, not a psql on a fifo. The original sends statements down a fifo
# to a background psql and polls its output file for a token, retrying 200 times at
# 0.1s. Here the second connection's query returns when it returns, so the two arms the
# original carries for the polling TIMEOUT are asserted as the positive facts they are
# the negative of.

def test_an_old_snapshot_never_sees_rows_committed_after_it(pgc_conn, expect):
    import psycopg

    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pm (a int, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('pm','pmp',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("INSERT INTO pm SELECT g, g FROM generate_series(1,10000) g")
        schema = _one(cur, "SELECT current_schema()")
        dsn = pgc_conn.info.dsn

    COUNT = "SELECT count(a) FROM pm WHERE c BETWEEN 1 AND 20000"
    a = psycopg.connect(dsn, autocommit=False)
    try:
        with a.cursor() as ac:
            # The fixture's schema is per-CONNECTION, so session A must be pointed at
            # the same one or it would read a different (absent) table.
            ac.execute(f'SET search_path TO "{schema}", public')
            ac.execute("SET pgcolumnar.enable_projection_scan = on")
            ac.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

            base = _one(ac, COUNT)
            expect.num(base, 10000, "session A opened snapshot")
            expect.num(base, 10000,
                       "session A baseline via projection sees batch 1")

            with pgc_conn.cursor() as cur:
                cur.execute("INSERT INTO pm SELECT g, g "
                            "FROM generate_series(10001,20000) g")

            after = _one(ac, COUNT)
            expect.num(after, 10000, "session A responded post-commit")
            expect.num(after, 10000,
                       "old snapshot projection scan does not see post-snapshot rows")
        a.commit()

        with a.cursor() as ac:
            expect.num(_one(ac, COUNT), 20000,
                       "new snapshot projection scan sees both batches")
    finally:
        a.close()


# ==================== LIFECYCLE: a dropped table must not orphan its declaration
#
# `pgcolumnar.projection_declaration` is keyed by regclass and is DUMPED, so a row left
# behind by a dropped table holds a regclass that no longer resolves. That is not
# confined to the dropped table: `rebuild_projections()` resolves `pd.rel` for every
# declaration, and resolving a dropped relation raises -- so one orphan used to abort
# the rebuild for every other table in the database (#304).

def test_dropping_a_table_removes_only_its_own_declaration(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        for t in ("od1", "od2"):
            cur.execute(f"CREATE TABLE {t} (id int, v text) USING pgcolumnar")
            cur.execute(f"INSERT INTO {t} SELECT g, md5(g::text) "
                        "FROM generate_series(1,500) g")
            cur.execute("SELECT pgcolumnar.add_projection(%s,%s,"
                        "ARRAY['id','v'],ARRAY['id'])", (t, t + "_p"))

        declared = ("SELECT count(*) FROM pgcolumnar.projection_declaration "
                    "WHERE rel::text IN ('od1','od2')")
        expect.num(_one(cur, declared), 2, "two declared projections to start")

        cur.execute("DROP TABLE od1")
        expect.num(_one(cur, declared), 1,
                   "DROP TABLE removes its declaration (#304)")
        expect.text(_one(cur, "SELECT name FROM pgcolumnar.projection_declaration "
                              "WHERE rel::text = 'od2'"), "od2_p",
                    "and leaves the other table's declaration alone")
        expect.num(_one(cur, "SELECT pgcolumnar.rebuild_projections()"), 0,
                   "so a rebuild still works for the rest of the database")


def test_the_rebuild_repairs_an_orphan_rather_than_aborting_on_it(pgc_conn, expect):
    """A database created by the build that shipped WITHOUT the drop hook already holds
    orphans, so removing the hook's cause is not enough -- the rebuild has to survive
    what is already on disk and clean it up."""
    with pgc_conn.cursor() as cur:
        cur.execute("INSERT INTO pgcolumnar.projection_declaration VALUES "
                    "(2147483647::oid::regclass, 'ghost', ARRAY['id'], ARRAY['id'])")
        ghost = ("SELECT count(*) FROM pgcolumnar.projection_declaration "
                 "WHERE name = 'ghost'")
        expect.num(_one(cur, ghost), 1,
                   "an orphan left by an older build is present")
        expect.num(_one(cur, "SELECT pgcolumnar.rebuild_projections()"), 0,
                   "the rebuild does not abort on it")
        expect.num(_one(cur, ghost), 0, "and it removed the orphan")


# ==================== LIFECYCLE: the mid-transaction latch (#875)
#
# `PgColumnarProjectionFanoutRow` builds the write state's projection-writer list on
# first use and LATCHES it -- including when the list comes back empty. So a write
# before `add_projection()` latches an empty list, the add back-fills the rows that
# already existed, and every later write in that transaction skips the projection with
# no error. The rows are in the base and absent from the projection, and a covering
# projection scan answers as if they were never inserted.
#
# THE LEADING WRITE IS THE WHOLE TRIGGER, so the control is the same transaction
# without it. That path already worked, and an arm that only ran the broken shape could
# not tell a fix from a change that broke both.

def test_a_projection_added_mid_transaction_receives_the_later_writes(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE nt (a int, c int) USING pgcolumnar")
        cur.execute("INSERT INTO nt SELECT g, g FROM generate_series(1,100) g")

    with pgc_conn.transaction():
        with pgc_conn.cursor() as cur:
            cur.execute("INSERT INTO nt SELECT g, g FROM generate_series(101,105) g")
            cur.execute("SELECT pgcolumnar.add_projection('nt','np',"
                        "ARRAY['a','c'],ARRAY['c'])")
            cur.execute("INSERT INTO nt SELECT g, g FROM generate_series(200,210) g")

    with pgc_conn.cursor() as cur:
        expect.num(_one(cur, "SELECT count(*) FROM nt"), 116,
                   "premise: the base table holds every committed row")
        expect.num(
            _one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('nt','np')"),
            116,
            "a projection added after a write in the same transaction gets the "
            "later rows")
        # NOT JUST THE COUNT. 116 of the WRONG rows satisfies the arm above.
        expect.row_set(_read_projection(cur, "nt", "np"),
                       _rows(cur, "SELECT a::text||'|'||c::text FROM nt"),
                       "and they are the right rows, not merely the right number")


def test_the_control_a_transaction_with_no_write_before_the_add(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ct (a int, c int) USING pgcolumnar")
        cur.execute("INSERT INTO ct SELECT g, g FROM generate_series(1,100) g")

    with pgc_conn.transaction():
        with pgc_conn.cursor() as cur:
            cur.execute("SELECT pgcolumnar.add_projection('ct','cp',"
                        "ARRAY['a','c'],ARRAY['c'])")
            cur.execute("INSERT INTO ct SELECT g, g FROM generate_series(200,210) g")

    with pgc_conn.cursor() as cur:
        expect.num(
            _one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('ct','cp')"),
            111,
            "control: with no write before add_projection the projection was always "
            "right")


def _orphan_storage(cur, table):
    """Row-group storage ids under this relation that no projection row names.

    DO NOT also exclude ids present in `pgcolumnar.storage`: a projection's storage is
    registered there too, so that filter hides exactly the row this is for.

    SCOPED TO ONE RELATION, because a database-wide count is not independent -- the
    first arm's orphan would still be there when the control runs, and the control
    would fail for the previous arm's reason while reading as if it had caught its own.
    """
    return _one(cur,
                "SELECT count(*) FROM (SELECT DISTINCT rg.storage_id "
                "  FROM pgcolumnar.row_group rg "
                "  JOIN pgcolumnar.storage s ON s.storage_id = rg.storage_id "
                f" WHERE s.relation_oid = '{table}'::regclass) x "
                "WHERE NOT EXISTS (SELECT 1 FROM pgcolumnar.projection p "
                "                  WHERE p.proj_storage_id = x.storage_id)")


def test_a_projection_dropped_mid_transaction_stops_receiving_writes(pgc_conn, expect):
    """The same latch in the other direction. Same cache, opposite sign: a writer
    cached before the drop keeps taking rows, which land in a projection storage whose
    catalog rows are already deleted and commit as an orphan."""
    import psycopg
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE dt (a int, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('dt','dp',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("INSERT INTO dt SELECT g, g FROM generate_series(1,50) g")

    with pgc_conn.transaction():
        with pgc_conn.cursor() as cur:
            cur.execute("INSERT INTO dt SELECT g, g FROM generate_series(51,55) g")
            cur.execute("SELECT pgcolumnar.drop_projection('dt','dp')")
            cur.execute("INSERT INTO dt SELECT g, g FROM generate_series(200,210) g")

    with pgc_conn.cursor() as cur:
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.projection_declaration "
                             "WHERE name = 'dp'"), 0,
                   "premise: the drop really removed the projection")
        expect.num(_one(cur, "SELECT count(*) FROM dt"), 66,
                   "and the base table still took every row")

        # The declaration going is only half. Reading it must FAIL as undefined rather
        # than return rows written to a writer the cache was still holding.
        with pytest.raises(psycopg.Error) as exc:
            cur.execute("SELECT pgcolumnar.read_projection('dt','dp')")
        expect.sqlstate(exc.value, "42704",
                        "a projection dropped mid-transaction is gone, not still "
                        "being written")

        expect.num(_orphan_storage(cur, "dt"), 0,
                   "a mid-transaction drop leaves no orphan projection storage")


def test_the_control_a_drop_in_its_own_transaction(pgc_conn, expect):
    """Pins the arm above to the CACHE rather than to drop_projection's own cleanup:
    the same drop with the transaction to itself was always 0."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE dt2 (a int, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('dt2','dp2',"
                    "ARRAY['a','c'],ARRAY['c'])")
        cur.execute("INSERT INTO dt2 SELECT g, g FROM generate_series(1,50) g")
        cur.execute("SELECT pgcolumnar.drop_projection('dt2','dp2')")
        cur.execute("INSERT INTO dt2 SELECT g, g FROM generate_series(200,300) g")
        expect.num(_orphan_storage(cur, "dt2"), 0,
                   "control: a drop in its own transaction never left one")

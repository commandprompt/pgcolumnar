"""A columnar table enforces temporal constraints exactly as a heap table does.

PostgreSQL 18 adds `WITHOUT OVERLAPS` primary keys and unique constraints; 19beta2 had
`UPDATE ... FOR PORTION OF`, which 19beta4 removed (#1337), so that part is its own test. Both run through the index and constraint machinery
pgColumnar integrates with, so the two storage types must agree: non-overlapping rows
accepted, overlapping rows rejected, and, where the grammar has it, a FOR PORTION OF
update producing the same result set.

Independent of test/temporal.sh: same public seam (the two tables' behaviour under the
same statements), own fixture, own observations. Assertion names match the shell suite
so the two can be compared by name, not by importing each other.

Row sets are compared as sorted tuples in Python rather than through `pgc_set_hash`,
which keeps the harnesses independent by construction and prints the rows that differ
rather than two unequal hashes.

THIS PAIR EXISTS BECAUSE OF #1131. `temporal.sh` gates on `btree_gist` -- `WITHOUT
OVERLAPS` needs a GiST index over the scalar key part -- and records that refusal under
a NAME. Until `expect.cannot_run` could carry a name, a port could not emit the string
and `compare_to_bash` reported it MISSING however faithful the rest was. The gate's
precondition exists on both sides, which is the case resolution 1 unblocks.

TWO REFUSALS, AND ONLY ONE OF THEM IS A PROPERTY. The version refusal below is
deliberately UNNAMED: on 15, 16 and 17 the shell suite prints a note, calls
`pgc_summary` and exits without recording anything, so there is no name to match. An
unnamed `cannot_run` states no property, which is exactly right -- naming it would
publish a check the shell suite does not have. The btree_gist refusal IS named, because
the shell suite names it.
"""
import psycopg

ROWS_OK = ("(1,'[2020-01-01,2020-06-01)'),"
           "(1,'[2020-06-01,2021-01-01)'),"
           "(2,'[2020-01-01,2021-01-01)')")
ROW_OVERLAPPING = "(1,'[2020-03-01,2020-09-01)')"


def _major(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        return int(cur.fetchone()[0]) // 10000


def _ran(conn, sql):
    """-> 'ok' when the statement succeeded, 'err' when the server rejected it.

    autocommit is on, so each statement is its own transaction and a rejection cannot
    poison the next one. Only `psycopg.Error` is caught: a bug in this file should
    raise, not be scored as a constraint doing its job.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error:
        return "err"


def _rows(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, valid FROM {table}")
        return sorted(cur.fetchall())


def _both(expect, conn, label, want, sql_h, sql_c):
    """The same statement against heap and columnar; both must agree with `want`."""
    expect.text(_ran(conn, sql_h), want, f"{label} (heap)")
    expect.text(_ran(conn, sql_c), want, f"{label} (columnar)")


def test_temporal(pgc_conn, expect):
    major = _major(pgc_conn)
    if major < 18:
        # UNNAMED, deliberately -- see the module docstring.
        expect.cannot_run(
            # UNSUPPORTED_MAJOR, from the closed list. I invented "UNSUPPORTED_VERSION"
            # and the layer refused it on PG15 -- which is the closed list doing exactly
            # what it is for, and the reason #1131 kept it closed rather than letting the
            # name replace the code.
            "UNSUPPORTED_MAJOR",
            f"WITHOUT OVERLAPS arrived in PostgreSQL 18 and this server is {major}, "
            f"so there is no temporal constraint machinery to compare")
        return

    try:
        with pgc_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    except psycopg.Error:
        expect.cannot_run(
            "MISSING_DEPENDENCY",
            "WITHOUT OVERLAPS needs a GiST index over the scalar key part, and "
            "btree_gist is contrib: a source build configured without it cannot "
            "create the constraint at all",
            # WRITTEN HERE RATHER THAN IN A CONSTANT. `compare_to_bash` parses this
            # file and resolves string LITERALS at the call site, so a name behind a
            # module constant is a name the grader cannot see. Measured: with the
            # constant this pair still reported `missing: 1`.
            name="btree_gist not available; temporal constraints need it")
        return

    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE tt_heap (id int, valid daterange, "
                    "PRIMARY KEY (id, valid WITHOUT OVERLAPS)) USING heap")
        cur.execute("CREATE TABLE tt_col (id int, valid daterange, "
                    "PRIMARY KEY (id, valid WITHOUT OVERLAPS)) USING pgcolumnar")

    _both(expect, pgc_conn, "non-overlapping insert accepted", "ok",
          f"INSERT INTO tt_heap VALUES {ROWS_OK}",
          f"INSERT INTO tt_col  VALUES {ROWS_OK}")
    _both(expect, pgc_conn, "overlapping insert rejected", "err",
          f"INSERT INTO tt_heap VALUES {ROW_OVERLAPPING}",
          f"INSERT INTO tt_col  VALUES {ROW_OVERLAPPING}")

    heap, col = _rows(pgc_conn, "tt_heap"), _rows(pgc_conn, "tt_col")
    print(f"-- after the PK arms: heap {len(heap)} rows, columnar {len(col)} rows")
    # MAKES AN IMPLICATION EXPLICIT; it does not close a hole. @jdatcmd checked the
    # claim this comment used to make and it was wrong twice over: the accepted arm
    # above asserts `ok` for an `INSERT ... VALUES` of three literal rows on BOTH
    # tables, and such a statement either inserts all three or raises -- so a table
    # that rejected them is already red there, and `len(col) >= 3` follows. The shell
    # suite makes the same assertion before its own hash comparison (temporal.sh:65,
    # before the hash at :73), so it does not have the hole I attributed to it either.
    #
    # Kept because the general rule is worth stating where a reader meets it: an arm
    # whose subject is a REJECTION wants a premise that the acceptance half happened.
    # Here that premise is implied rather than load-bearing, and saying so is the
    # honest version -- claiming otherwise would send someone to "fix" a suite that is
    # not broken.
    expect.at_least(len(col), 3, "premise: the accepted rows are actually in the "
                                 "columnar table, so the comparison is not of two "
                                 "empty sets")
    expect.rows(col, heap, "temporal PK contents match")



def _grammar_has_for_portion_of(conn):
    """-> None when the server parses UPDATE ... FOR PORTION OF, else its SQLSTATE.

    The GRAMMAR, not the major (#1337): 19beta2 had the clause and 19beta4 removed
    it, so a version gate ran these arms on a server that cannot parse them. The
    probe is an EXPLAIN that writes nothing.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE fpo_probe (id int, valid daterange)")
            cur.execute("EXPLAIN UPDATE fpo_probe FOR PORTION OF valid "
                        "FROM '2020-03-01' TO '2020-05-01' SET id = id WHERE false")
        return None
    except psycopg.Error as e:
        return e.sqlstate


def test_for_portion_of(pgc_conn, expect):
    """UPDATE ... FOR PORTION OF splits a period identically on heap and columnar.

    Its own test because its precondition is its own: the clause has to be in the
    server's grammar, which it is on 19beta2 and is not on 15 to 18 or from 19beta4
    (#1337). Where it is absent this test is unrunnable, under the names the shell
    suite records as SKIPs, and expected_unrunnable.txt lists it per major.

    Its own fixture, not test_temporal's: one row a key, other dates.
    """
    missing = _grammar_has_for_portion_of(pgc_conn)
    if missing is not None:
        # UNNAMED: the arms below carry the names, and the shell suite records
        # them as SKIPs under those names.
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"this server's grammar has no UPDATE ... FOR PORTION OF ({missing})")
        return
    try:
        with pgc_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    except psycopg.Error:
        expect.cannot_run("MISSING_DEPENDENCY",
                          "WITHOUT OVERLAPS needs btree_gist, which is contrib",
                          name="btree_gist not available; temporal constraints need it")
        return
    with pgc_conn.cursor() as cur:
        for t, am in (("fp_heap", "heap"), ("fp_col", "pgcolumnar")):
            cur.execute(f"CREATE TABLE {t} (id int, valid daterange, "
                        f"PRIMARY KEY (id, valid WITHOUT OVERLAPS)) USING {am}")
            cur.execute(f"INSERT INTO {t} VALUES (7, '[2021-01-01,2022-01-01)'), "
                        f"(8, '[2021-01-01,2022-01-01)')")
    # Named literally, as the shell suite now names them (its skip loop is held to
    # exactly these by #994's checker).
    expect.text(_ran(pgc_conn, "UPDATE fp_heap FOR PORTION OF valid FROM '2021-04-01' "
                               "TO '2021-07-01' SET id = 70 WHERE id = 7"), "ok",
                "for portion of update applied (heap)")
    expect.text(_ran(pgc_conn, "UPDATE fp_col  FOR PORTION OF valid FROM '2021-04-01' "
                               "TO '2021-07-01' SET id = 70 WHERE id = 7"), "ok",
                "for portion of update applied (columnar)")
    # Agreeing is not enough: two failed updates agree too, which is how the shell
    # arm passed on 19beta4 (#1337). The portion must be there under its new key.
    col, heap = _rows(pgc_conn, "fp_col"), _rows(pgc_conn, "fp_heap")
    with pgc_conn.cursor() as cur:   # the server's own text, not psycopg's Range repr
        cur.execute("SELECT coalesce(string_agg(valid::text, ',' ORDER BY valid), 'none') "
                    "FROM fp_col WHERE id = 70")
        portion = cur.fetchone()[0]
    expect.text(f"{col == heap}|{portion}", "True|[2021-04-01,2021-07-01)",
                "for portion of result matches")

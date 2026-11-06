"""Fetch-by-row-number with a column projection (#157, #1341). The pytest twin of
`test/native_fetch_projection.sh`, every check by the shell's name.

A row fetched by number decoded every column whatever the caller wanted. Two narrower
entry points exist beside it: one decodes a given set of columns, one answers
visibility and decodes nothing. They have no SQL surface of their own, so they are
exercised through their callers, pgcolumnar.read_projection and
pgcolumnar.reconstruct_via_projection, on a 41-column table: the output is unchanged
against the base table, a deleted row stops being read, and a projection covering
every column reconstructs right with nothing decoded from the base. Then the source:
neither caller decodes every column, index deletion asks no row whether it is live,
and every needed-set membership test consults the explicit all-columns flag. The
source arms read code with its comments blanked, as the shell's code_of does.
"""
import pathlib
import re

import psycopg
import pytest

ROWS = 20000
NCOLS = 40
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
COLS = "".join(f", c{i} bigint" for i in range(1, NCOLS + 1))
SEL = "".join(f", g * {i}" for i in range(1, NCOLS + 1))
_COMMENT = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|/\*.*?\*/|//[^\n]*", re.S)


def _code(name):
    """-> the source file with its C comments blanked (line count kept), literals kept."""
    s = (SRC / name).read_text()
    return _COMMENT.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else "\n" * m.group(0).count("\n"), s)


def _lines(code, pat):
    """-> how many lines hold `pat`, as grep -c counts."""
    return sum(pat in line for line in code.splitlines())


def _one(c, sql):
    return c.execute(sql).fetchone()[0]


@pytest.fixture(scope="module")
def fp(pgc_cluster):
    """-> a connection to a database holding fp_w and its projection fp_p."""
    db = "pgc_fetchproj"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute(f"CREATE TABLE fp_w (id int{COLS}) USING pgcolumnar")
            c.execute(f"INSERT INTO fp_w SELECT g{SEL} FROM generate_series(1, {ROWS}) g")
            c.execute("SELECT pgcolumnar.add_projection('fp_w', 'fp_p', ARRAY['id','c1'], ARRAY['id'])")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_the_projected_fetch_returns_the_base_tables_rows_and_sees_deletes(fp, expect):
    c = fp
    expect.num(_one(c, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'fp_w'"),
               NCOLS + 1, "the wide fixture has 41 columns")
    # the oracle is the base table, which does not go through the projected fetch
    last = NCOLS + 1
    expect.num(_one(c, f"""WITH viaproj AS (
            SELECT pgcolumnar.reconstruct_via_projection('fp_w','fp_p') AS r
        ), direct AS (
            SELECT id::text || '|' || c1::text || '|' || c2::text || '|' || c{NCOLS}::text AS d FROM fp_w
        )
        SELECT count(*) FROM viaproj
        WHERE split_part(r, '|', 1) || '|' || split_part(r, '|', 2) || '|' ||
              split_part(r, '|', 3) || '|' || split_part(r, '|', {last})
              NOT IN (SELECT d FROM direct)"""), 0, "every reconstructed row matches the base table")

    expect.num(_one(c, "SELECT count(*) FROM pgcolumnar.read_projection('fp_w','fp_p')"), ROWS,
               "the projection reads every row to start with")
    c.execute("DELETE FROM fp_w WHERE id <= 100")
    expect.num(_one(c, "SELECT count(*) FROM pgcolumnar.read_projection('fp_w','fp_p')"), ROWS - 100,
               "a deleted row stops being read through the projection")
    expect.num(_one(c, "SELECT count(*) FROM pgcolumnar.reconstruct_via_projection('fp_w','fp_p')"), ROWS - 100,
               "and stops being reconstructed too")
    # a value from an uncovered column, so the set was not narrowed by one column too many
    expect.text(_one(c, f"SELECT split_part(r, '|', {last}) "
                        "FROM pgcolumnar.reconstruct_via_projection('fp_w','fp_p') AS r "
                        "WHERE split_part(r, '|', 1) = '101'"),
                _one(c, f"SELECT c{NCOLS}::text FROM fp_w WHERE id = 101"),
                "an uncovered column still reads correctly after a delete")


def test_a_projection_covering_every_column_decodes_nothing_and_is_right(fp, expect):
    c = fp
    # the computed set of base columns comes out EMPTY here, which once read as "every column"
    c.execute(f"CREATE TABLE fp_all (id int{COLS}) USING pgcolumnar")
    c.execute(f"INSERT INTO fp_all SELECT g{SEL} FROM generate_series(1, 2000) g")
    every = ", ".join(["'id'"] + [f"'c{i}'" for i in range(1, NCOLS + 1)])
    c.execute(f"SELECT pgcolumnar.add_projection('fp_all', 'fp_ap', ARRAY[{every}], ARRAY['id'])")
    expect.num(_one(c, "SELECT count(*) FROM pgcolumnar.reconstruct_via_projection('fp_all','fp_ap')"), 2000,
               "the all-covering projection reconstructs every row")
    expect.text(_one(c, f"SELECT split_part(r, '|', 2) || '/' || split_part(r, '|', {NCOLS + 1}) "
                        "FROM pgcolumnar.reconstruct_via_projection('fp_all','fp_ap') AS r "
                        "WHERE split_part(r, '|', 1) = '77'"),
                _one(c, f"SELECT c1::text || '/' || c{NCOLS}::text FROM fp_all WHERE id = 77"),
                "and its values are right with nothing decoded from the base")


def test_no_caller_decodes_every_column_and_index_deletion_reads_no_row(expect):
    proj = _code("columnar_projection.c")
    # a want-zero on the full decode; the premises keep a file that calls nothing from passing
    expect.at_least(_lines(proj, "PgColumnarRowIsLive("), 1, "premise: the visibility-only entry point is called at all")
    expect.at_least(_lines(proj, "PgColumnarReadRowByNumberCols("), 1,
                    "premise: the column-set entry point is called at all")
    expect.num(_lines("PgColumnarReadRowByNumberCols(a, b)", "PgColumnarReadRowByNumber("), 0,
               "premise: the column-set caller is not counted as a full decode")
    expect.text(f"{_lines(proj, 'PgColumnarReadRowByNumber(')} full decode(s)", "0 full decode(s)",
                "neither caller decodes every column: no full decode in the projection path")

    lines = _code("columnar_tableam.c").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("pgcolumnar_index_delete_tuples(")), None)
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("}")), len(lines)) if start is not None else 0
    deltuples = "\n".join(lines[start:end + 1]) if start is not None else ""
    expect.text("yes" if "ndeltids" in deltuples else f"extracted=<{deltuples or 'empty'}>", "yes",
                "premise: the index deletion function was extracted, not blank")
    expect.text("no (asks per row)" if "PgColumnarRowIsLive" in deltuples else "yes", "yes",
                "index deletion asks no row whether it is live: it vouches for nothing")
    expect.text("no (still decodes)" if "PgColumnarReadRowByNumber" in deltuples else "yes", "yes",
                "and does not decode the row to find out")


def test_every_needed_set_test_consults_the_all_columns_flag(expect):
    reader = _code("columnar_reader.c").splitlines()
    tests = sum("bms_is_member(c, needed)" in l for l in reader)
    guarded = sum("!allColumns && !bms_is_member(c, needed)" in l for l in reader)
    expect.at_least(tests, 1, "premise: there is a needed-set membership test to guard")
    expect.at_least(sum("bool allColumns" in l for l in reader), 1, "premise: the explicit all-columns flag is declared")
    # honest callers move both counts together; an unguarded test moves only the total
    expect.text(f"guarded {guarded} of {tests}", f"guarded {tests} of {tests}",
                "every needed-set membership test consults that flag rather than a null set")

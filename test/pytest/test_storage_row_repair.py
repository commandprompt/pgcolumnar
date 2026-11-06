"""The alpha5 release notes' repair procedure works as written.

Two alpha4 defects (#1211, #1275) left a table's pgcolumnar.storage row naming a
relation that no longer exists, and the upgrade does not repair existing rows.
RELEASE_NOTES_1.0-alpha5.md, "After the upgrade: repair entries alpha4 left wrong",
gives a query that finds them and two repairs. This performs them.

Independent of test/storage_row_repair.sh: own tables, own row count, own way of
making the damage. The notes' query is read from the notes, so an edit to the
document is what this runs.

SCOPED TO ITS OWN STORAGE IDS. pgc_conn gives a private schema, not a private
database, and the pgcolumnar catalogs are per database, so the notes' query sees
every other test's storage rows too. The count asserted is of rows the query
returns for the two storage ids this test damaged, which only this test can move.
"""
import pathlib
import re

ROWS = 1500
NOTES = pathlib.Path(__file__).resolve().parents[2] / "RELEASE_NOTES_1.0-alpha5.md"


def _notes_query():
    text = NOTES.read_text()
    after = text.split("find the rows that name no relation", 1)
    if len(after) != 2:
        return None
    m = re.search(r"```sql\n(.*?)```", after[1], re.S)
    return m.group(1).strip().rstrip(";") if m else None


def _one(cur, sql):
    cur.execute(sql)
    row = cur.fetchone()
    return row[0] if row else None


def test_the_release_notes_repair_procedure_works(pgc_conn, expect):
    find = _notes_query()
    expect.text("found" if find and "pgcolumnar.storage" in find else "missing", "found",
                "premise: the release notes give the query that finds wrong rows")
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rep_t (k int, w int) USING pgcolumnar")
        cur.execute(f"INSERT INTO rep_t SELECT g, g * 2 FROM generate_series(1,{ROWS}) g")
        cur.execute("CREATE MATERIALIZED VIEW rep_mv USING pgcolumnar AS "
                    "SELECT k FROM rep_t WITH DATA")
        t_sid = _one(cur, "SELECT pgcolumnar.get_storage_id('rep_t')")
        mv_sid = _one(cur, "SELECT pgcolumnar.get_storage_id('rep_mv')")
        mine = f"({t_sid}, {mv_sid})"

        def found_mine():
            return _one(cur, f"SELECT count(*) FROM ({find}) z WHERE z.storage_id IN {mine}")

        expect.num(found_mine(), 0, "no row names a missing relation before the damage")

        # The damage: each row names a relation created and dropped for the purpose.
        cur.execute("CREATE TABLE rep_gone_1 (x int)")
        cur.execute("CREATE TABLE rep_gone_2 (x int)")
        g1 = _one(cur, "SELECT 'rep_gone_1'::regclass::oid")
        g2 = _one(cur, "SELECT 'rep_gone_2'::regclass::oid")
        cur.execute("DROP TABLE rep_gone_1, rep_gone_2")
        cur.execute(f"UPDATE pgcolumnar.storage SET relation_oid = {g1} WHERE storage_id = {t_sid}")
        cur.execute(f"UPDATE pgcolumnar.storage SET relation_oid = {g2} WHERE storage_id = {mv_sid}")

        expect.num(found_mine(), 2, "the notes' query finds both rows alpha4 left wrong")
        expect.text(_one(cur, "SELECT count(*) || '/' || sum(w) FROM rep_t"),
                    f"{ROWS}/{ROWS * (ROWS + 1)}",
                    "premise: the damaged table still reads every row")

        cur.execute("REFRESH MATERIALIZED VIEW rep_mv")
        expect.num(_one(cur, "SELECT (relation_oid = 'rep_mv'::regclass)::int "
                             "FROM pgcolumnar.storage WHERE storage_id = "
                             "pgcolumnar.get_storage_id('rep_mv')"), 1,
                   "REFRESH MATERIALIZED VIEW repairs the view's row")
        cur.execute("ALTER TABLE rep_t ALTER COLUMN w TYPE int USING w")
        expect.num(_one(cur, "SELECT (relation_oid = 'rep_t'::regclass)::int "
                             "FROM pgcolumnar.storage WHERE storage_id = "
                             "pgcolumnar.get_storage_id('rep_t')"), 1,
                   "a same-type ALTER COLUMN ... USING repairs the table's row")

        # A REFRESH gives the view a NEW storage id, so the final count covers the
        # original ids AND the current ones: a damaged row left behind under its old
        # id must still be counted.
        t_now = _one(cur, "SELECT pgcolumnar.get_storage_id('rep_t')")
        mv_now = _one(cur, "SELECT pgcolumnar.get_storage_id('rep_mv')")
        mine = f"({t_sid}, {mv_sid}, {t_now}, {mv_now})"
        expect.num(found_mine(), 0, "after both repairs the notes' query finds nothing")
        expect.text(_one(cur, "SELECT count(*) || '/' || sum(w) FROM rep_t"),
                    f"{ROWS}/{ROWS * (ROWS + 1)}",
                    "and the repaired table holds the same rows")
        expect.text(_one(cur, "SELECT count(*) || '/' || sum(k) FROM rep_mv"),
                    f"{ROWS}/{ROWS * (ROWS + 1) // 2}",
                    "and so does the refreshed view")

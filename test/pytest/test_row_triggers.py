"""Row-level triggers on a columnar table fire once per row with the right row, as on heap
(#179). The pytest twin of `test/row_triggers.sh`, every check by the shell's name
(#1341).

An AFTER INSERT ... FOR EACH ROW trigger re-fetches its row by TID when the event fires,
which is before the stripe is flushed, so the access method must answer from its write
buffer; deferred, UPDATE and DELETE triggers fetch rows already on disk. Each shape runs
on a columnar table and its heap twin with the same trigger, and the trigger's log of
(count, sum of ids, sum of values) must agree. Plus a BEFORE trigger, a deferred
constraint trigger, and an Arrow import into a table with an AFTER trigger.

WHAT A COMPARISON NEEDS. The two logs agree only when the heap twin's trigger fired at
all: a pair whose tables or triggers were never created logged nothing on either side,
and "0/0/0" agreed with "0/0/0". A statement "succeeds" only when it raised nothing,
where one message used to be counted.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import pathlib

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_row_triggers"
ROWS = 500
TR_F = """CREATE OR REPLACE FUNCTION tr_f() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        INSERT INTO tr_log VALUES (TG_TABLE_NAME, TG_OP, OLD.id, OLD.val);
    ELSE
        INSERT INTO tr_log VALUES (TG_TABLE_NAME, TG_OP, NEW.id, NEW.val);
    END IF;
    RETURN NULL;
END $$"""


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, *sqls):
        """-> "succeeded", or the first error, after running `sqls` on one session."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                for sql in sqls:
                    conn.execute(sql)
        except psycopg.Error as exc:
            return f"SQLSTATE {exc.sqlstate}: {str(exc).splitlines()[0]}"
        return "succeeded"

    def q(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return "" if row is None or row[0] is None else str(row[0])

    def fired(self, table):
        return self.q("SELECT count(*) || '/' || coalesce(sum(id), 0) || '/' || coalesce(sum(val), 0) "
                      f"FROM tr_log WHERE src = '{table}'")

    def same_as_heap(self, suffix):
        """-> "same", both summaries, or "no reading" when the heap trigger fired nothing."""
        c, h = self.fired(f"tr_c{suffix}"), self.fired(f"tr_h{suffix}")
        if not c or not h or not h[0].isdigit() or h.split("/")[0] == "0":
            return f"no reading: columnar [{c}], heap [{h}]"
        return "same" if c == h else f"differ: columnar [{c}], heap [{h}]"

    def pair(self, suffix, clause):
        c, h = f"tr_c{suffix}", f"tr_h{suffix}"
        # One transaction, as the shell's single psql -c is: a failed trigger takes the
        # tables with it.
        self.run("BEGIN", f"DROP TABLE IF EXISTS {c}", f"DROP TABLE IF EXISTS {h}",
                 f"CREATE TABLE {c} (id int, val int) USING pgcolumnar", f"CREATE TABLE {h} (id int, val int)",
                 f"CREATE TRIGGER {c}_t {clause} ON {c} FOR EACH ROW EXECUTE FUNCTION tr_f()",
                 f"CREATE TRIGGER {h}_t {clause} ON {h} FOR EACH ROW EXECUTE FUNCTION tr_f()", "COMMIT")

    def both(self, suffix, *sqls):
        """Run each statement against the columnar table, then the heap twin."""
        for t in (f"tr_c{suffix}", f"tr_h{suffix}"):
            self.run(*[s.format(t=t) for s in sqls])


@pytest.fixture(scope="module")
def rt(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        d.run("CREATE TABLE tr_log (src text, ev text, id int, val int)", TR_F)
        r = {}

        d.pair(1, "AFTER INSERT")
        r["single"] = d.run("INSERT INTO tr_c1 VALUES (1, 10)")
        d.run("INSERT INTO tr_h1 VALUES (1, 10)")
        r["landed"] = d.q("SELECT count(*) FROM tr_c1")
        r["same 1"] = d.same_as_heap(1)

        d.pair(2, "AFTER INSERT")
        d.both(2, f"INSERT INTO {{t}} SELECT g, g * 2 FROM generate_series(1, {ROWS}) g")
        r["same 2"] = d.same_as_heap(2)

        d.run("DROP TABLE IF EXISTS tr_c3", "DROP TABLE IF EXISTS tr_h3",
              "SET pgcolumnar.stripe_row_limit = 1000",
              "CREATE TABLE tr_c3 (id int, val int) USING pgcolumnar",
              "CREATE TRIGGER tr_c3_t AFTER INSERT ON tr_c3 FOR EACH ROW EXECUTE FUNCTION tr_f()")
        d.run("CREATE TABLE tr_h3 (id int, val int)",
              "CREATE TRIGGER tr_h3_t AFTER INSERT ON tr_h3 FOR EACH ROW EXECUTE FUNCTION tr_f()")
        d.run("SET pgcolumnar.stripe_row_limit = 1000", "INSERT INTO tr_c3 SELECT g, g FROM generate_series(1, 4500) g")
        d.run("INSERT INTO tr_h3 SELECT g, g FROM generate_series(1, 4500) g")
        r["same 3"] = d.same_as_heap(3)

        d.run("DROP TABLE IF EXISTS tr_c7", "DROP TABLE IF EXISTS tr_h7",
              "SET pgcolumnar.stripe_row_limit = 100000",
              "CREATE TABLE tr_c7 (id int, val int) USING pgcolumnar",
              "CREATE TRIGGER tr_c7_t AFTER INSERT ON tr_c7 FOR EACH ROW EXECUTE FUNCTION tr_f()")
        d.run("CREATE TABLE tr_h7 (id int, val int)",
              "CREATE TRIGGER tr_h7_t AFTER INSERT ON tr_h7 FOR EACH ROW EXECUTE FUNCTION tr_f()")
        d.run("SET pgcolumnar.stripe_row_limit = 100000", "INSERT INTO tr_c7 SELECT g, g FROM generate_series(1, 1500) g")
        d.run("INSERT INTO tr_h7 SELECT g, g FROM generate_series(1, 1500) g")
        r["same 7"] = d.same_as_heap(7)

        d.pair(4, "AFTER UPDATE")
        d.both(4, "INSERT INTO {t} SELECT g, g FROM generate_series(1, 50) g")
        d.both(4, "UPDATE {t} SET val = val + 1 WHERE id % 3 = 0")
        r["same 4"] = d.same_as_heap(4)

        d.pair(5, "AFTER DELETE")
        d.both(5, "INSERT INTO {t} SELECT g, g FROM generate_series(1, 50) g")
        d.both(5, "DELETE FROM {t} WHERE id % 4 = 0")
        r["same 5"] = d.same_as_heap(5)

        d.pair(6, "AFTER INSERT OR UPDATE")
        d.both(6, "BEGIN", "INSERT INTO {t} SELECT g, g FROM generate_series(1, 30) g",
               "UPDATE {t} SET val = 0 WHERE id <= 10", "COMMIT")
        r["same 6"] = d.same_as_heap(6)

        d.run("DROP TABLE IF EXISTS tr_b", "CREATE TABLE tr_b (id int, val int) USING pgcolumnar",
              "CREATE TRIGGER tr_b_t BEFORE INSERT ON tr_b FOR EACH ROW EXECUTE FUNCTION tr_f()",
              "INSERT INTO tr_b SELECT g, g FROM generate_series(1, 20) g")
        r["before"] = d.q("SELECT count(*) FROM tr_log WHERE src = 'tr_b'")

        d.run("DROP TABLE IF EXISTS tr_d", "CREATE TABLE tr_d (id int, val int) USING pgcolumnar",
              "CREATE CONSTRAINT TRIGGER tr_d_t AFTER INSERT ON tr_d DEFERRABLE INITIALLY DEFERRED "
              "FOR EACH ROW EXECUTE FUNCTION tr_f()",
              "BEGIN", "INSERT INTO tr_d SELECT g, g FROM generate_series(1, 20) g", "COMMIT")
        r["deferred"] = d.q("SELECT count(*) FROM tr_log WHERE src = 'tr_d'")

        arrow = str(server_dir / "pgc_trig.arrow")
        d.run("DROP TABLE IF EXISTS tr_src", "CREATE TABLE tr_src (id int, val int) USING pgcolumnar",
              "INSERT INTO tr_src SELECT g, g FROM generate_series(1, 40) g")
        d.run(f"SELECT pgcolumnar.export_arrow('tr_src', '{arrow}')")
        d.run("DROP TABLE IF EXISTS tr_imp", "CREATE TABLE tr_imp (id int, val int) USING pgcolumnar",
              "CREATE INDEX tr_imp_i ON tr_imp (id)",
              "CREATE TRIGGER tr_imp_t AFTER INSERT ON tr_imp FOR EACH ROW EXECUTE FUNCTION tr_f()")
        r["import"] = d.run(f"SELECT pgcolumnar.import_arrow('tr_imp', '{arrow}')")
        r["imported"] = d.q("SELECT count(*) FROM tr_imp")
        r["import fired"] = d.q("SELECT count(*) FROM tr_log WHERE src = 'tr_imp'")
        pathlib.Path(arrow).unlink(missing_ok=True)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_a_single_row_insert_with_an_after_row_trigger_succeeds(rt, expect):
    expect.text(rt["single"], "succeeded", "a single-row insert with an after-row trigger succeeds")


def test_the_single_row_landed(rt, expect):
    expect.text(rt["landed"], "1", "and the row itself landed")


def test_the_trigger_saw_the_same_row_heap_did(rt, expect):
    expect.text(rt["same 1"], "same", "the trigger saw the same row heap did")


def test_a_multi_row_insert_fires_once_per_row(rt, expect):
    expect.text(rt["same 2"], "same", "a multi-row insert fires once per row with the right values")


def test_an_insert_crossing_a_stripe_boundary_fires_for_every_row(rt, expect):
    expect.text(rt["same 3"], "same", "an insert crossing a stripe boundary fires for every row")


def test_1500_rows_with_no_mid_statement_flush_all_fire(rt, expect):
    expect.text(rt["same 7"], "same", "1500 rows with no mid-statement flush all fire")


def test_after_update_fires_with_the_new_row(rt, expect):
    expect.text(rt["same 4"], "same", "after-update fires with the new row")


def test_after_delete_fires_with_the_old_row(rt, expect):
    expect.text(rt["same 5"], "same", "after-delete fires with the old row")


def test_insert_then_update_in_one_transaction(rt, expect):
    expect.text(rt["same 6"], "same", "insert then update in one transaction")


def test_a_before_row_trigger_still_fires_once_per_row(rt, expect):
    expect.text(rt["before"], "20", "a before-row trigger still fires once per row")


def test_a_deferred_constraint_trigger_still_fires_at_commit(rt, expect):
    expect.text(rt["deferred"], "20", "a deferred constraint trigger still fires at commit")


def test_an_import_into_a_table_with_an_after_row_trigger_succeeds(rt, expect):
    expect.text(rt["import"], "succeeded", "an import into a table with an after-row trigger succeeds")


def test_the_imported_rows_landed(rt, expect):
    expect.text(rt["imported"], "40", "the imported rows landed")


def test_the_trigger_fired_once_per_imported_row(rt, expect):
    expect.text(rt["import fired"], "40", "and the trigger fired once per imported row")

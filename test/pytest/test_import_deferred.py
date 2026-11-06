"""import_arrow and import_parquet honour a DEFERRABLE constraint (issue #168). The
pytest twin of `test/import_deferred.sh`, every check by the shell's name (#1341).

A row imported over one that the same transaction then removes must commit under a
deferred unique constraint, as it does on heap and through an ordinary columnar
INSERT. And the check is deferred, not gone: a duplicate that survives to commit, an
IMMEDIATE constraint, SET CONSTRAINTS ... IMMEDIATE part-way through, and an
exclusion constraint all still fail, each with its own SQLSTATE. A partial unique
index is maintained for exactly the rows it covers, and a rewrite of a uniquely
indexed table enforces nothing -- through compact_rewrite, which reaches the per-row
loop that vacuum()'s reindex never does.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). heap is the oracle where the shell uses it. The export
files live in `server_dir`, because the backend writes and reads them.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_import_deferred"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _outcome(dsn, sql):
    """-> 'committed', or the SQLSTATE the statement string SQL failed with."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "committed"


def _mk(dsn, t, am, clause):
    _run(dsn, f"DROP TABLE IF EXISTS {t}; CREATE TABLE {t} (id int) USING {am}; INSERT INTO {t} VALUES (1); "
              f"ALTER TABLE {t} ADD CONSTRAINT {t}_uq UNIQUE (id) {clause};")


def _delete_first(t):
    return f"DELETE FROM {t} WHERE ctid = (SELECT min(ctid) FROM {t});"


@pytest.fixture(scope="module")
def df(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        arrow, parq, piarrow = server_dir / "df.arrow", server_dir / "df.parquet", server_dir / "df_pi.arrow"
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE df_src (id int) USING pgcolumnar; INSERT INTO df_src VALUES (1); "
                  f"SELECT pgcolumnar.export_arrow('df_src', '{arrow}'); "
                  f"SELECT pgcolumnar.export_parquet('df_src', '{parq}');")
        deferred = "DEFERRABLE INITIALLY DEFERRED"
        r = {}
        _mk(dsn, "df_h", "heap", deferred)
        r["heap transient"] = _outcome(dsn, f"BEGIN; INSERT INTO df_h VALUES (1); {_delete_first('df_h')} COMMIT;")
        _mk(dsn, "df_i", "pgcolumnar", deferred)
        r["insert transient"] = _outcome(dsn, f"BEGIN; INSERT INTO df_i VALUES (1); {_delete_first('df_i')} COMMIT;")
        _mk(dsn, "df_a", "pgcolumnar", deferred)
        r["arrow transient"] = _outcome(dsn, f"BEGIN; SELECT pgcolumnar.import_arrow('df_a', '{arrow}'); "
                                             f"{_delete_first('df_a')} COMMIT;")
        _mk(dsn, "df_p", "pgcolumnar", deferred)
        r["parquet transient"] = _outcome(dsn, f"BEGIN; SELECT pgcolumnar.import_parquet('df_p', '{parq}'); "
                                               f"{_delete_first('df_p')} COMMIT;")

        _mk(dsn, "df_h2", "heap", deferred)
        r["heap permanent"] = _outcome(dsn, "BEGIN; INSERT INTO df_h2 VALUES (1); COMMIT;")
        _mk(dsn, "df_a2", "pgcolumnar", deferred)
        r["arrow permanent"] = _outcome(dsn, f"BEGIN; SELECT pgcolumnar.import_arrow('df_a2', '{arrow}'); COMMIT;")
        r["a2 rows"] = _value(dsn, "SELECT count(*) FROM df_a2")
        _mk(dsn, "df_a3", "pgcolumnar", "")
        r["immediate"] = _outcome(dsn, f"BEGIN; SELECT pgcolumnar.import_arrow('df_a3', '{arrow}'); "
                                       f"{_delete_first('df_a3')} COMMIT;")
        _mk(dsn, "df_a4", "pgcolumnar", deferred)
        r["set immediate"] = _outcome(dsn, f"BEGIN; SELECT pgcolumnar.import_arrow('df_a4', '{arrow}'); "
                                           f"SET CONSTRAINTS df_a4_uq IMMEDIATE; {_delete_first('df_a4')} COMMIT;")

        _run(dsn, "DROP TABLE IF EXISTS df_x; CREATE TABLE df_x (id int) USING pgcolumnar; "
                  "INSERT INTO df_x VALUES (1); ALTER TABLE df_x ADD CONSTRAINT df_x_ex EXCLUDE ((id) WITH =);")
        r["exclusion"] = _outcome(dsn, f"BEGIN; SELECT pgcolumnar.import_arrow('df_x', '{arrow}'); COMMIT;")

        _run(dsn, "CREATE TABLE df_pisrc (id int, keep bool) USING pgcolumnar; "
                  "INSERT INTO df_pisrc SELECT g, (g % 2 = 0) FROM generate_series(1, 200) g; "
                  f"SELECT pgcolumnar.export_arrow('df_pisrc', '{piarrow}'); "
                  "CREATE TABLE df_pi (id int, keep bool) USING pgcolumnar; "
                  "CREATE UNIQUE INDEX df_pi_partial ON df_pi (id) WHERE keep; "
                  f"SELECT pgcolumnar.import_arrow('df_pi', '{piarrow}');")
        r["partial"] = _outcome(dsn, f"SELECT pgcolumnar.import_arrow('df_pi', '{piarrow}');")
        r["partial rows"] = _value(dsn, "SELECT count(*) FROM df_pi WHERE NOT keep")

        _run(dsn, "CREATE TABLE df_rw (id int) USING pgcolumnar; "
                  "INSERT INTO df_rw SELECT g FROM generate_series(1, 500) g; "
                  "CREATE UNIQUE INDEX df_rw_uq ON df_rw (id); DELETE FROM df_rw WHERE id % 3 = 0;")
        # compact_rewrite, not vacuum(): vacuum() reindexes and never reaches the per-row loop
        r["rewrite"] = _outcome(dsn, "SELECT pgcolumnar.compact_rewrite('df_rw', 0.0);")
        r["rewrite rows"] = _value(dsn, "SELECT count(*) FROM df_rw")
        r["rewrite want"] = _value(dsn, "SELECT count(*) FROM generate_series(1,500) g WHERE g % 3 <> 0")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_heap_allows_a_transient_collision(df, expect):
    expect.text(str(df["heap transient"]), "committed", "premise: heap allows a transient collision under a deferred constraint")


def test_an_ordinary_columnar_insert_still_allows_it(df, expect):
    expect.text(str(df["insert transient"]), str(df["heap transient"]), "an ordinary columnar INSERT still allows it")


def test_import_arrow_allows_it(df, expect):
    expect.text(str(df["arrow transient"]), str(df["heap transient"]), "import_arrow allows it")


def test_import_parquet_allows_it(df, expect):
    expect.text(str(df["parquet transient"]), str(df["heap transient"]), "import_parquet allows it")


def test_heap_rejects_a_duplicate_still_there_at_commit(df, expect):
    expect.text(str(df["heap permanent"]), "23505", "premise: heap rejects a duplicate that is still there at commit")


def test_import_arrow_rejects_a_duplicate_that_survives_to_commit(df, expect):
    expect.text(str(df["arrow permanent"]), str(df["heap permanent"]), "import_arrow rejects a duplicate that survives to commit")


def test_the_import_rolled_back(df, expect):
    expect.text(str(df["a2 rows"]), "1", "and the import rolled back rather than leaving the row")


def test_an_immediate_constraint_still_rejects_at_insert_time(df, expect):
    expect.text(str(df["immediate"]), "23505", "an IMMEDIATE constraint still rejects at insert time")


def test_set_constraints_immediate_is_honoured_mid_transaction(df, expect):
    expect.text(str(df["set immediate"]), "23505", "SET CONSTRAINTS IMMEDIATE is honoured mid-transaction")


def test_an_exclusion_constraint_is_still_enforced_on_import(df, expect):
    expect.text(str(df["exclusion"]), "23P01", "an exclusion constraint is still enforced on import")


def test_a_partial_unique_index_accepts_rows_it_does_not_cover(df, expect):
    expect.text(str(df["partial"]), "23505", "a partial unique index accepts rows it does not cover")


def test_rows_outside_the_partial_index_are_all_present(df, expect):
    expect.text(str(df["partial rows"]), "100", "rows outside the partial index are all present")


def test_a_rewrite_of_a_uniquely_indexed_table_still_succeeds(df, expect):
    expect.text(str(df["rewrite"]), "committed", "a rewrite of a uniquely-indexed table still succeeds")


def test_and_the_surviving_rows_are_intact(df, expect):
    expect.text(str(df["rewrite rows"]), str(df["rewrite want"]), "and the surviving rows are intact")

"""ALTER TABLE forms that read every existing row through the access method (#1341).
The pytest twin of `test/alter_table_scans.sh`, every check by the shell's name.

A columnar table keeps a deleted row in its file until compaction, so each form has
to see the live rows and only those. SET NOT NULL, ADD PRIMARY KEY and VALIDATE
CONSTRAINT run on a columnar table and on heap with the same rows: first a row
violates each, and both refuse it with the same SQLSTATE; then that row is deleted,
and both accept it. ALTER COLUMN TYPE and ADD COLUMN with a volatile default rewrite
the table, and must keep the live rows and leave the deleted ones gone.
"""
import psycopg

AMS = ("pgcolumnar", "heap")


def _outcome(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no sqlstate: {exc}"


def _both(conn, sql):
    return "|".join(_outcome(conn, sql.replace("%t", f"at_{am}")) for am in AMS)


def _answers(conn, sql, setup=()):
    out = []
    for am in AMS:
        with conn.cursor() as cur:
            for s in setup:
                cur.execute(s)
            cur.execute(sql.replace("%t", f"at_{am}"))
            out.append("/".join(str(x) for x in cur.fetchone()))
            cur.execute("RESET enable_seqscan"); cur.execute("RESET enable_bitmapscan")
    return "|".join(out)


def test_alter_table_forms_see_the_live_rows_and_only_those(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        for am in AMS:
            cur.execute(f"CREATE TABLE at_{am} (id int, v int, w text) USING {am}")
            cur.execute(f"INSERT INTO at_{am} SELECT g, g, 'x' FROM generate_series(1, 3000) g")
            cur.execute(f"INSERT INTO at_{am} VALUES (5000, NULL, 'null'), (1, -1, 'dup')")

    expect.text(_both(pgc_conn, "ALTER TABLE %t ALTER COLUMN v SET NOT NULL"), "23502|23502",
                "SET NOT NULL is refused over a NULL (23502), as on heap")
    expect.text(_both(pgc_conn, "ALTER TABLE %t ADD PRIMARY KEY (id)"), "23505|23505",
                "ADD PRIMARY KEY is refused over a duplicate (23505), as on heap")
    expect.text(_both(pgc_conn, "ALTER TABLE %t ADD CONSTRAINT v_pos CHECK (v > 0) NOT VALID"), "ok|ok",
                "a CHECK added NOT VALID is accepted, as on heap")
    expect.text(_both(pgc_conn, "ALTER TABLE %t VALIDATE CONSTRAINT v_pos"), "23514|23514",
                "and VALIDATE CONSTRAINT is refused over the row that breaks it (23514), as on heap")

    with pgc_conn.cursor() as cur:
        for am in AMS:
            cur.execute(f"DELETE FROM at_{am} WHERE v IS NULL OR v < 0")
    expect.text(_answers(pgc_conn, "SELECT count(*), sum(v) FROM %t"), "3000/4501500|3000/4501500",
                "the deleted rows are gone from both tables, which hold the same rows")
    expect.text(_both(pgc_conn, "ALTER TABLE %t ALTER COLUMN v SET NOT NULL"), "ok|ok",
                "SET NOT NULL succeeds once the NULL row is deleted, as on heap")
    expect.text(_both(pgc_conn, "ALTER TABLE %t ADD PRIMARY KEY (id)"), "ok|ok",
                "ADD PRIMARY KEY succeeds once the duplicate is deleted, as on heap")
    expect.text(_both(pgc_conn, "ALTER TABLE %t VALIDATE CONSTRAINT v_pos"), "ok|ok",
                "VALIDATE CONSTRAINT succeeds once the breaking row is deleted, as on heap")
    expect.text(_answers(pgc_conn, "SELECT convalidated FROM pg_constraint "
                                   "WHERE conrelid = '%t'::regclass AND conname = 'v_pos'"), "True|True",
                "and the constraint is recorded as validated")

    expect.text(_both(pgc_conn, "ALTER TABLE %t ALTER COLUMN v TYPE bigint"), "ok|ok",
                "ALTER COLUMN TYPE rewrites the table, as on heap")
    expect.text(_answers(pgc_conn, "SELECT count(*), sum(v), (SELECT format_type(atttypid, NULL) "
                                   "FROM pg_attribute WHERE attrelid = '%t'::regclass AND attname = 'v') FROM %t"),
                "3000/4501500/bigint|3000/4501500/bigint",
                "and the rewrite kept the live rows and not the deleted ones")
    expect.text(_both(pgc_conn, "ALTER TABLE %t ADD COLUMN z float8 DEFAULT random()"), "ok|ok",
                "ADD COLUMN with a volatile default rewrites the table, as on heap")
    expect.text(_answers(pgc_conn, "SELECT count(*), count(z), count(*) FILTER (WHERE v IS NULL OR v < 0) FROM %t"),
                "3000/3000/0|3000/3000/0",
                "and that rewrite gave every live row a value and brought back no deleted one")

    idx = ("SET enable_seqscan = off", "SET enable_bitmapscan = off")
    plans = []
    for am in AMS:
        with pgc_conn.cursor() as cur:
            for s in idx:
                cur.execute(s)
            cur.execute(f"EXPLAIN (COSTS OFF) SELECT count(*) FROM at_{am} WHERE id = 1")
            plan = "\n".join(r[0] for r in cur.fetchall())
            cur.execute("RESET enable_seqscan"); cur.execute("RESET enable_bitmapscan")
        plans.append(str(int(f"Scan using at_{am}_pkey" in plan and "Index" in plan)))
    expect.text("|".join(plans), "1|1", "the lookup of id 1 is an index scan on the primary key")
    expect.text(_answers(pgc_conn, "SELECT count(*), min(w) FROM %t WHERE id = 1", idx), "1/x|1/x",
                "the primary key still finds id 1 once, through the index, after both rewrites")

"""A foreign key that references a columnar table is refused where it is chosen
(#1341). The pytest twin of `test/fk_referencing.sh`, every check by the shell's name.

Referential integrity reads the referenced row with FOR KEY SHARE, and row locking is
not implemented for columnar tables, so a constraint referencing one would make every
insert into the referencing table fail. It is refused at DDL instead, by either route
into that state: creating the constraint against a columnar table, or converting a
referenced table to columnar. Only the REFERENCED side is refused: a columnar table may
reference a heap table, and the controls hold everything else steady -- other
constraints, heap-to-heap keys, conversions of an unreferenced table, of the
referencing side, back to heap, and of the referenced table to a row store.
"""
import psycopg


def _err(conn, sql):
    """-> '' when sql succeeds, else the error's message, detail and hint, joined."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return ""
    except psycopg.Error as exc:
        d = exc.diag
        return " ".join(x for x in (d.message_primary, d.message_detail, d.message_hint) if x) or str(exc)


def _has(text, needle):
    return "yes" if needle in text else f"no ({text})"


def _ok(text):
    return "yes" if not text else f"no ({text})"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _run(conn, *sqls):
    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


def test_the_referenced_side_is_refused_at_ddl(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE fk_parent (id int PRIMARY KEY) USING pgcolumnar",
         "INSERT INTO fk_parent VALUES (1), (2)")
    err = _err(conn, "CREATE TABLE fk_child (id int REFERENCES fk_parent(id))")
    expect.text(_has(err, "cannot create a foreign key referencing columnar table"), "yes",
                "a foreign key referencing a columnar table is refused")
    expect.text(_has(err, "row locking is not supported"), "yes", "and the error says why")
    expect.num(_one(conn, "SELECT count(*) FROM pg_class WHERE relname = 'fk_child' "
                          "AND relnamespace = current_schema()::regnamespace"), 0,
               "the refused table was not left behind")

    _run(conn, "CREATE TABLE fk_child2 (id int)")
    err2 = _err(conn, "ALTER TABLE fk_child2 ADD CONSTRAINT fk_c2 FOREIGN KEY (id) REFERENCES fk_parent(id)")
    expect.text(_has(err2, "cannot create a foreign key referencing columnar table"), "yes",
                "ALTER TABLE ADD CONSTRAINT is refused by the same rule")


def test_the_referencing_side_still_works(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE fk_h_parent (id int PRIMARY KEY)", "INSERT INTO fk_h_parent VALUES (1), (2), (3)")
    expect.text(_ok(_err(conn, "CREATE TABLE fk_c_child (id int REFERENCES fk_h_parent(id), v text) "
                               "USING pgcolumnar")), "yes",
                "a columnar table may reference a heap table")
    _err(conn, "INSERT INTO fk_c_child VALUES (1, 'a'), (2, 'b')")
    expect.num(_one(conn, "SELECT count(*) FROM fk_c_child"), 2, "and rows that satisfy it are accepted")
    expect.text(_has(_err(conn, "INSERT INTO fk_c_child VALUES (99, 'nope')"), "violates foreign key constraint"),
                "yes", "a row that violates it is still rejected")


def test_constraints_that_need_no_row_lock_are_unaffected(pgc_conn, expect):
    conn = pgc_conn
    expect.text(_ok(_err(conn, "CREATE TABLE fk_other (id int PRIMARY KEY, k int UNIQUE, v int CHECK (v > 0), "
                               "w int NOT NULL) USING pgcolumnar")), "yes",
                "primary key, unique, check and not-null on a columnar table are unaffected")
    _err(conn, "INSERT INTO fk_other VALUES (1, 10, 5, 7)")
    expect.num(_one(conn, "SELECT count(*) FROM fk_other"), 1, "and they still work")
    expect.text(_has(_err(conn, "INSERT INTO fk_other VALUES (1, 11, 5, 7)"), "duplicate key"), "yes",
                "the primary key is still enforced")
    _run(conn, "CREATE TABLE fk_hh_parent (id int PRIMARY KEY)")
    expect.text(_ok(_err(conn, "CREATE TABLE fk_hh_child (id int REFERENCES fk_hh_parent(id))")), "yes",
                "a foreign key between two heap tables is unaffected")


def test_converting_a_referenced_table_is_refused_and_nothing_else_is(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE fk_conv_parent (id int PRIMARY KEY)",
         "CREATE TABLE fk_conv_child (id int REFERENCES fk_conv_parent(id))",
         "INSERT INTO fk_conv_parent VALUES (1)", "INSERT INTO fk_conv_child VALUES (1)")
    conv = _err(conn, "ALTER TABLE fk_conv_parent SET ACCESS METHOD pgcolumnar")
    expect.text(_has(conv, "referenced by a foreign key"), "yes",
                "converting an FK-referenced table to columnar is refused")
    expect.text(_has(conv, "row locking is not supported"), "yes", "and the error names the constraint")
    expect.text(_one(conn, "SELECT am.amname::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                           "WHERE c.oid = 'fk_conv_parent'::regclass"), "heap", "the parent is still a row store")
    _err(conn, "INSERT INTO fk_conv_parent VALUES (2)")
    expect.text(_ok(_err(conn, "INSERT INTO fk_conv_child VALUES (2)")), "yes",
                "the child is still writable afterwards")
    expect.text(_has(_err(conn, "SELECT pgcolumnar.alter_table_set_access_method('fk_conv_parent', 'pgcolumnar')"),
                     "referenced by a foreign key"), "yes", "the conversion helper is refused identically")

    # The over-rejection controls: a rule keyed on the wrong side, or firing for any
    # ALTER TABLE or any target, passes every arm above and fails one of these.
    _run(conn, "CREATE TABLE fk_conv_plain (id int PRIMARY KEY)", "INSERT INTO fk_conv_plain VALUES (1)")
    expect.text(_ok(_err(conn, "ALTER TABLE fk_conv_plain SET ACCESS METHOD pgcolumnar")), "yes",
                "a table with no foreign key still converts")
    expect.text(_ok(_err(conn, "ALTER TABLE fk_conv_child SET ACCESS METHOD pgcolumnar")), "yes",
                "converting the referencing side is still allowed")
    expect.text(_ok(_err(conn, "ALTER TABLE fk_conv_parent ADD COLUMN note text")), "yes",
                "an unrelated ALTER TABLE on a referenced table is unaffected")
    expect.text(_ok(_err(conn, "ALTER TABLE fk_conv_plain SET ACCESS METHOD heap")), "yes",
                "converting back to heap is unaffected")
    # The control above has no foreign key; this one is the referenced table itself.
    expect.text(_ok(_err(conn, "ALTER TABLE fk_conv_parent SET ACCESS METHOD heap")), "yes",
                "an FK-referenced table may still be set to a row store")


def test_a_partitioned_referenced_parent(pgc_conn, expect):
    """Setting a partitioned parent's access method chooses what every later partition
    inherits, and core clones the foreign key to each, so it is refused too (#201)."""
    conn = pgc_conn
    _run(conn, "CREATE TABLE fk_pp (id int PRIMARY KEY) PARTITION BY RANGE (id)",
         "CREATE TABLE fk_pp1 PARTITION OF fk_pp FOR VALUES FROM (1) TO (100)",
         "CREATE TABLE fk_pc (id int REFERENCES fk_pp(id))")
    expect.text(_has(_err(conn, "CREATE TABLE fk_pp2 PARTITION OF fk_pp FOR VALUES FROM (100) TO (200) "
                                "USING pgcolumnar"), "cannot create a foreign key referencing columnar table"),
                "yes", "a columnar partition of an FK-referenced parent is refused")
    expect.text(_has(_err(conn, "ALTER TABLE fk_pp SET ACCESS METHOD pgcolumnar"), "cannot convert table"), "yes",
                "and setting the parent's access method is refused as well")
    # "did it become columnar", not "is it heap": a partitioned table never given an
    # access method has relam = 0, which joins to no pg_am row.
    expect.num(_one(conn, "SELECT count(*) FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
                          "WHERE c.oid = 'fk_pp'::regclass AND a.amname = 'pgcolumnar'"), 0,
               "so the parent did not become columnar")
    expect.num(0 if not _err(conn, "CREATE TABLE fk_pp3 PARTITION OF fk_pp FOR VALUES FROM (200) TO (300)") else 1,
               0, "and a partition can still be created the ordinary way")

    _run(conn, "CREATE TABLE fk_free (id int) PARTITION BY RANGE (id)")
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    if major >= 17:
        expect.num(0 if not _err(conn, "ALTER TABLE fk_free SET ACCESS METHOD pgcolumnar") else 1, 0,
                   "a partitioned table with no foreign key still converts")
        _err(conn, "CREATE TABLE fk_free1 PARTITION OF fk_free FOR VALUES FROM (1) TO (100)")
        expect.text(_one(conn, "SELECT amname::text FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
                               "WHERE c.oid = 'fk_free1'::regclass"), "pgcolumnar",
                    "and its partitions inherit the columnar access method")
    else:
        # Before 17 core refuses SET ACCESS METHOD on every partitioned table, so the
        # refusal must be core's, by core's message.
        expect.text(_has(_err(conn, "ALTER TABLE fk_free SET ACCESS METHOD pgcolumnar"),
                         "cannot change access method of a partitioned table"), "yes",
                    "before 17, core refuses this for every partitioned table")

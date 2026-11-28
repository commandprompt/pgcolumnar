"""DROP COLUMN must not invalidate a materialized projection (#432, port of
projection_drop_column.sh).

Projections are extension metadata rather than `pg_depend` objects, so PostgreSQL
accepted `DROP COLUMN` even when a projection stored that column. Dropping its sort key
left the projection's attnum pointing at a dropped `pg_attribute` row, and the next
INSERT failed in `lookup_type_cache` with "type with OID 0 does not exist" -- the table
became unwritable. Until projections can join `DROP ... CASCADE`, the dependent DROP is
refused at its public DDL boundary.

**SQLSTATE, never the message.** `2BP01` is `dependent_objects_still_exist` and `42501`
is `insufficient_privilege`; both are typed fields rather than text the server is free
to reword. The bash suite already does this, by parsing `VERBOSITY verbose` output with
`sed`. The port reads `exc.sqlstate` off the raised error, which is the same claim
without a regex over a message.

**SET ROLE, not a login role, and the two precedents disagree so the choice is stated.**
`test_projection_privilege.py` uses real logins because there the ACL layers ARE the
subject: a deny arm must prove the call reached the code that denies it, and it recorded
`SET ROLE` arms still passing with `NOLOGIN` injected. `test_native_ownership.py` uses
`SET ROLE` for the opposite reason: when only ownership is under test, requiring the role
to log in adds a way for the arm to fail that has nothing to do with the property. This
suite is the second kind. Nothing here asserts anything about connecting, so the role
never needs to.

**The schema grant is load-bearing**, and it is the trap `test_native_ownership.py`
records. `pgc_conn` puts each test in a private schema. Without USAGE on it the
non-owner gets `42P01 relation "pdc" does not exist`, because an unqualified name
resolves through `search_path` and a schema the role cannot use is simply skipped -- so
the refusal arms would fail against the wrong SQLSTATE rather than pass falsely. The
premise below fails first and names reachability instead of leaving `42501 != 42P01` to
be interpreted.

THE TWO NON-OWNER REFUSALS ARE COMPARED TO EACH OTHER, in both suites (#1339). The arms
exist to show that a stranger cannot tell a projected column from an unprojected one, so
the property -- these two are INDISTINGUISHABLE -- is asserted directly rather than
implied by two separate constants, and it is asserted before them, so it can fail.

**The lock is tested by contention.** The ownership check runs before the lock and again
after it, and both raise 42501, so a SQLSTATE alone cannot tell which one refused. While
the owner holds SHARE on the table, only the unlocked check refuses a stranger at once.
"""

import psycopg

NOBODY = "pdc_nobody"


def _err(cur, sql):
    """-> the raised error, or None if the statement succeeded.

    `pgc_conn` is autocommit, so a failed statement is its own aborted transaction and
    the next one starts clean. No savepoint is needed and none is taken.
    """
    try:
        cur.execute(sql)
        return None
    except psycopg.Error as exc:
        return exc


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def _as_nobody(cur, sql):
    """Run one statement as the non-owner and come back, whatever happened."""
    cur.execute(f"SET ROLE {NOBODY}")
    try:
        return _err(cur, sql)
    finally:
        cur.execute("RESET ROLE")


def test_drop_column_is_refused_while_a_projection_depends_on_it(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pdc (id int, payload text, sort_key int) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.add_projection('pdc', 'by_sort', "
            "ARRAY['id','sort_key'], ARRAY['sort_key'])"
        )
        cur.execute(
            "INSERT INTO pdc SELECT g, 'row-' || g, g % 10 FROM generate_series(1,1000) g"
        )

        # The role is cluster-global and these tests run in parallel under xdist, so it
        # is created idempotently and never dropped: DROP ROLE fails once it holds a
        # grant, and dropping a role another worker is using is worse than leaving it.
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (NOBODY,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {NOBODY} NOSUPERUSER")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {NOBODY}')

        # EXTRA, and it is what stops the two arms below passing for the wrong reason.
        #
        # THE PROPERTY IS RESOLUTION, NOT READABILITY, and my first version of this
        # asserted the wrong one. USAGE on the schema lets the name resolve; SELECT is a
        # separate grant the non-owner does not have, so a read comes back 42501 and an
        # arm demanding success fails on a correctly configured fixture. What matters
        # here is only that the name is not invisible: 42P01 would mean `search_path`
        # skipped the schema, and then the DROP arms below would be asserting 42501
        # against a table the role cannot see rather than against ownership.
        seen = _as_nobody(cur, "SELECT 1 FROM pdc LIMIT 1")
        expect.text("resolved" if seen is None or seen.sqlstate != "42P01"
                    else "unresolved (42P01)",
                    "resolved",
                    "premise: the non-owner can resolve the table, so a refusal is about ownership")

        # --- the stranger learns nothing ------------------------------------
        proj_err = _as_nobody(cur, "ALTER TABLE pdc DROP COLUMN sort_key")
        plain_err = _as_nobody(cur, "ALTER TABLE pdc DROP COLUMN payload")

        # The property is that the two are INDISTINGUISHABLE, not that each happens to
        # equal a constant, so it is compared FIRST. Placed after the two constant arms
        # it could never run red: `expect` stops at the first failure, and if both
        # constants hold the two are equal. With both ownership checks deleted, the
        # stranger gets 2BP01 for the projected column and 42501 for the other, and
        # this is the check that names it (#1339).
        expect.text(proj_err.sqlstate if proj_err else "no error",
                    plain_err.sqlstate if plain_err else "no error",
                    "and the two refusals are the same error, so neither leaks")
        expect.sqlstate(proj_err, "42501",
                        "a non-owner learns nothing from a projected column")
        expect.sqlstate(plain_err, "42501",
                        "the same non-owner error is returned for an unprojected column")

        # THE LOCK HALF OF THE PROPERTY (#1339). The ownership check runs twice: once
        # with no lock, then again after ShareUpdateExclusiveLock. The arms above
        # cannot tell them apart, because both raise 42501. Contention can: while the
        # owner holds SHARE on pdc, which conflicts with ShareUpdateExclusiveLock, a
        # stranger refused by the unlocked check gets 42501 at once, and one that
        # reaches the lock first waits out its lock_timeout (55P03).
        holder = psycopg.connect(pgc_conn.info.dsn, autocommit=False)
        try:
            holder.execute(f'SET search_path TO "{schema}"')
            holder.execute("LOCK TABLE pdc IN SHARE MODE")
            held = _one(cur, "SELECT count(*) FROM pg_locks WHERE relation = 'pdc'::regclass "
                             "AND mode = 'ShareLock' AND granted")
            expect.num(held, 1, "premise: the owner holds SHARE on pdc before the stranger asks")
            cur.execute("SET lock_timeout = '2s'")
            try:
                waited_err = _as_nobody(cur, "ALTER TABLE pdc DROP COLUMN sort_key")
            finally:
                cur.execute("RESET lock_timeout")
            expect.sqlstate(waited_err, "42501",
                            "a non-owner is refused before it waits for the table lock")
        finally:
            holder.rollback()
            holder.close()

        # --- the owner is refused as a dependency ---------------------------
        #
        # Red before the fix: PostgreSQL accepts this, and the next INSERT reaches the
        # projection writer with a type OID of zero.
        expect.sqlstate(_err(cur, "ALTER TABLE pdc DROP COLUMN sort_key"), "2BP01",
                        "dropping a projected column is refused as a dependency")

        cur.execute("INSERT INTO pdc VALUES (1001, 'still-writable', 1)")
        expect.num(_one(cur, "SELECT count(*) FROM pdc"), 1001,
                   "the rejected DDL leaves the table writable")
        expect.num(
            _one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('pdc','by_sort')"),
            1001,
            "and the projection still receives the row",
        )

        # A column outside every projection remains ordinary DDL.
        cur.execute("ALTER TABLE pdc DROP COLUMN payload")
        cur.execute("INSERT INTO pdc VALUES (1002, 2)")
        expect.num(_one(cur, "SELECT count(*) FROM pdc"), 1002,
                   "dropping an unrelated column remains allowed")

        # --- a partitioned parent recurses into columnar partitions ---------
        #
        # The parent has no storage of its own, but DROP COLUMN recurses, so the
        # dependency check has to walk the same hierarchy.
        cur.execute("CREATE TABLE pdc_parent (id int, sort_key int) PARTITION BY RANGE (id)")
        cur.execute(
            "CREATE TABLE pdc_child PARTITION OF pdc_parent "
            "FOR VALUES FROM (0) TO (100) USING pgcolumnar"
        )
        cur.execute(
            "SELECT pgcolumnar.add_projection('pdc_child', 'child_sort', "
            "ARRAY['id','sort_key'], ARRAY['sort_key'])"
        )
        cur.execute("INSERT INTO pdc_parent VALUES (1, 1)")

        expect.sqlstate(_err(cur, "ALTER TABLE pdc_parent DROP COLUMN sort_key"), "2BP01",
                        "a parent DROP sees projections on columnar partitions")

        cur.execute("INSERT INTO pdc_parent VALUES (2, 2)")
        expect.num(_one(cur, "SELECT count(*) FROM pdc_parent"), 2,
                   "the rejected parent DDL leaves its partition writable")


# --- #2240: a column dropped by a cascade takes its projections with it ------
#
# ALTER TYPE ... DROP ATTRIBUTE ... CASCADE and DROP DOMAIN ... CASCADE drop a
# column without passing through ALTER TABLE, so the refusal above never saw them.
# The projection kept the dropped attnum, the next INSERT failed with "type with OID
# 0 does not exist", and the declaration kept the name. Both routes need CASCADE,
# and the projection now goes with the column. A refusal there is not possible:
# core drops a column on its own just before dropping its table in DROP SCHEMA ...
# CASCADE, and the drop hook cannot tell that drop from these.

_CASCADE = "drop cascades to projection "


def _crashes(pgc_cluster):
    """-> backends the server log says died, or None when the log can't be read."""
    try:
        return (pgc_cluster.datadir / "server.log").read_text(
            errors="replace").count("terminated by signal")
    except OSError:
        return None


def _cascade_notices(conn, sql):
    """Run one statement -> its "drop cascades to projection" notices, sorted and
    joined by newlines, or "none". A statement that fails reports none, as the bash
    suite's psql does, so the named check is what goes red."""
    seen = []

    def handler(diag):
        msg = diag.message_primary or ""
        if msg.startswith(_CASCADE):
            seen.append(msg)

    conn.add_notice_handler(handler)
    try:
        conn.execute(sql)
    except psycopg.Error:
        pass
    finally:
        conn.remove_notice_handler(handler)
    return "\n".join(sorted(seen)) if seen else "none"


def _decls(cur, rel):
    """-> the table's declarations as 'name:columns', sorted, or "none"."""
    v = _one(cur, "SELECT string_agg(name || ':' || columns::text, ' ' ORDER BY name COLLATE \"C\") "
                  f"FROM pgcolumnar.projection_declaration WHERE rel = '{rel}'::regclass")
    return v if v else "none"


def _projs(cur, rel):
    """-> the table's materialized projections (base excluded) as 'name:attnums'."""
    v = _one(cur, "SELECT string_agg(name || ':' || columns::text, ' ' ORDER BY name COLLATE \"C\") "
                  "FROM pgcolumnar.projection "
                  f"WHERE storage_id = pgcolumnar.get_storage_id('{rel}') AND projection_id <> 0")
    return v if v else "none"


def _uses(cur, rel, col):
    """-> the projection EXPLAIN says the scan reads, or "base"."""
    cur.execute(f"EXPLAIN (COSTS OFF) SELECT a, {col} FROM {rel} WHERE {col} BETWEEN 2 AND 3")
    for (line,) in cur.fetchall():
        s = line.strip()
        if s.startswith("Columnar Projection: "):
            return s[len("Columnar Projection: "):]
    return "base"


def _rows(cur, rel, col):
    cur.execute(f"SELECT a, {col} FROM {rel} WHERE {col} BETWEEN 2 AND 3 ORDER BY a")
    return cur.fetchall()


def _proj_sid(cur, rel, name):
    return _one(cur, "SELECT proj_storage_id FROM pgcolumnar.projection "
                     f"WHERE storage_id = pgcolumnar.get_storage_id('{rel}') AND name = '{name}'")


def test_a_cascaded_column_drop_takes_its_projections(pgc_cluster, pgc_conn, expect):
    crash0 = _crashes(pgc_cluster)
    with pgc_conn.cursor() as cur:
        # --- ALTER TYPE ... DROP ATTRIBUTE ... CASCADE, two typed tables of one type
        #
        # Two columnar tables of the type, because the cascade reaches each one by its
        # own column drop and a fix that handled only the first would pass with one.
        # The heap table of the same type is the oracle, and loses bb in the same
        # statement. "BB" differs from bb only in case, and its projection must stay.
        cur.execute('CREATE TYPE pdc_pt AS (a int, bb int, "BB" int)')
        for t in ("pdc_tp1", "pdc_tp2"):
            cur.execute(f"CREATE TABLE {t} OF pdc_pt USING pgcolumnar")
            cur.execute(f"SELECT pgcolumnar.set_options('{t}', stripe_row_limit => 1000, "
                        "chunk_group_row_limit => 500)")
            cur.execute(f"SELECT pgcolumnar.add_projection('{t}', 'pj', ARRAY['bb','a'], ARRAY['bb'])")
        cur.execute("CREATE TABLE pdc_tph OF pdc_pt")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_tp1', 'pj_upper', "
                    "ARRAY['BB','a'], ARRAY['BB'])")
        for t in ("pdc_tp1", "pdc_tp2", "pdc_tph"):
            cur.execute(f"INSERT INTO {t} SELECT g, g % 100, g % 70 FROM generate_series(1, 20000) g")
            cur.execute(f"ANALYZE {t}")
        pj1 = _proj_sid(cur, "pdc_tp1", "pj")
        pj2 = _proj_sid(cur, "pdc_tp2", "pj")
        expect.text(f"{_uses(cur, 'pdc_tp1', 'bb')} {_uses(cur, 'pdc_tp2', 'bb')}", "pj pj",
                    "premise: the planner reads pj on both typed tables before the drop")
        groups = [_one(cur, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid}")
                  for sid in (pj1, pj2)]
        expect.text(" ".join("t" if g > 0 else "f" for g in groups), "t t",
                    "premise: both pj projections hold row groups before the drop")

        # Core refuses this without CASCADE, before any column is dropped.
        expect.sqlstate(_err(cur, "ALTER TYPE pdc_pt DROP ATTRIBUTE bb"), "2BP01",
                        "premise: without CASCADE, ALTER TYPE ... DROP ATTRIBUTE is refused")
        expect.text(f"{_decls(cur, 'pdc_tp1')} | {_decls(cur, 'pdc_tp2')}",
                    "pj:{bb,a} pj_upper:{BB,a} | pj:{bb,a}",
                    "premise: and the refused ALTER TYPE leaves every declaration")

        # Red before the fix: the cascade succeeded and left pj declared and
        # materialized on both tables, with no notice.
        expect.text(_cascade_notices(pgc_conn, "ALTER TYPE pdc_pt DROP ATTRIBUTE bb CASCADE"),
                    'drop cascades to projection "pj" on table "pdc_tp1"\n'
                    'drop cascades to projection "pj" on table "pdc_tp2"',
                    "ALTER TYPE ... DROP ATTRIBUTE CASCADE reports each projection it drops")
        expect.text(_decls(cur, "pdc_tp1"), "pj_upper:{BB,a}",
                    "the cascade drops pj's declaration on the first typed table, and keeps the one on BB")
        expect.text(_decls(cur, "pdc_tp2"), "none",
                    "the cascade drops pj's declaration on the second typed table")
        expect.text(f"{_projs(cur, 'pdc_tp1')} | {_projs(cur, 'pdc_tp2')}",
                    "pj_upper:{3,1} | none",
                    "the cascade drops pj's projection on both typed tables, and keeps the one on BB")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.row_group "
                             f"WHERE storage_id IN ({pj1}, {pj2})"), 0,
                   "the dropped projections' row groups are freed")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.read_projection('pdc_tp1', 'pj_upper')"),
                   20000, "the projection on BB still holds every row")

        # Red before the fix: "type with OID 0 does not exist".
        for t in ("pdc_tp1", "pdc_tp2", "pdc_tph"):
            _err(cur, f"INSERT INTO {t} VALUES (20001, 5)")
        counts = " ".join(str(_one(cur, f"SELECT count(*) FROM {t}")) for t in ("pdc_tp1", "pdc_tp2"))
        expect.text(counts, "20001 20001",
                    "both typed tables stay writable after the cascade")

        # bb comes back under the same name, with a new attnum and new values. The
        # dropped projection must not answer for it. An INSERT that fails here is
        # left to the oracle arms below, as the bash suite leaves it.
        cur.execute("ALTER TYPE pdc_pt ADD ATTRIBUTE bb int CASCADE")
        for t in ("pdc_tp1", "pdc_tp2", "pdc_tph"):
            _err(cur, f'INSERT INTO {t} (a, bb, "BB") '
                      "SELECT g, (g * 7) % 100, g % 70 FROM generate_series(20002, 24000) g")
            cur.execute(f"ANALYZE {t}")
        expect.text(f"{_uses(cur, 'pdc_tp1', 'bb')} {_uses(cur, 'pdc_tp2', 'bb')}", "base base",
                    "after bb is added back, the planner reads the base on both typed tables")
        heap = _rows(cur, "pdc_tph", "bb")
        expect.rows([_rows(cur, "pdc_tp1", "bb"), _rows(cur, "pdc_tp2", "bb")], [heap, heap],
                    "after bb is added back, both typed tables answer as heap does")
        readd = _err(cur, "SELECT pgcolumnar.add_projection('pdc_tp1', 'pj', ARRAY['bb','a'], ARRAY['bb'])")
        expect.text(readd.sqlstate if readd else "00000", "00000",
                    "add_projection takes the name pj again on the added-back bb")
        cur.execute("ANALYZE pdc_tp1")
        expect.rows([_uses(cur, "pdc_tp1", "bb"), _rows(cur, "pdc_tp1", "bb")], ["pj", heap],
                    "and the planner reads the new pj, which answers as heap does")

        # --- DROP DOMAIN ... CASCADE on a plain table ---------------------------
        #
        # pd and pd2 store c, the domain column, and go. c is pd's first column and
        # pd2's last, so a loop that stops early misses one. pd_decl names c too
        # but has no materialized projection, as a rewrite that could not re-record
        # it leaves; it is made here by deleting its projection row. pd_upper stores
        # "C", pa stores neither, and pd on pdc_do is another table's projection of
        # the same name on a column of the same name: all three stay.
        cur.execute("CREATE DOMAIN pdc_d AS int")
        cur.execute('CREATE TABLE pdc_dt (a int, c pdc_d, "C" int) USING pgcolumnar')
        cur.execute('CREATE TABLE pdc_dh (a int, c pdc_d, "C" int)')
        cur.execute("CREATE TABLE pdc_do (a int, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('pdc_dt', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_dt', 'pd', ARRAY['c','a'], ARRAY['c'])")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_dt', 'pd_upper', ARRAY['C','a'], ARRAY['C'])")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_dt', 'pa', ARRAY['a'])")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_dt', 'pd2', ARRAY['a','c'])")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_dt', 'pd_decl', ARRAY['a','c'])")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_do', 'pd', ARRAY['c','a'], ARRAY['c'])")
        cur.execute("DELETE FROM pgcolumnar.projection "
                    "WHERE storage_id = pgcolumnar.get_storage_id('pdc_dt') AND name = 'pd_decl'")
        for t in ("pdc_dt", "pdc_dh"):
            cur.execute(f"INSERT INTO {t} SELECT g, g % 100, g % 70 FROM generate_series(1, 20000) g")
            cur.execute(f"ANALYZE {t}")
        cur.execute("INSERT INTO pdc_do SELECT g, g % 100 FROM generate_series(1, 20000) g")
        expect.text(_uses(cur, "pdc_dt", "c"), "pd", "premise: the planner reads pd before the drop")
        expect.text(f"{_decls(cur, 'pdc_dt')} | {_projs(cur, 'pdc_dt')}",
                    "pa:{a} pd:{c,a} pd2:{a,c} pd_decl:{a,c} pd_upper:{C,a} | pa:{1} pd:{2,1} pd2:{1,2} pd_upper:{3,1}",
                    "premise: pd_decl is declared with no materialized projection")

        expect.text(_cascade_notices(pgc_conn, "DROP DOMAIN pdc_d CASCADE"),
                    'drop cascades to projection "pd" on table "pdc_dt"\n'
                    'drop cascades to projection "pd2" on table "pdc_dt"\n'
                    'drop cascades to projection "pd_decl" on table "pdc_dt"',
                    "DROP DOMAIN ... CASCADE reports each projection it drops")
        expect.text(_decls(cur, "pdc_dt"), "pa:{a} pd_upper:{C,a}",
                    "DROP DOMAIN ... CASCADE drops the declarations naming c, a materialized one or not")
        expect.text(_projs(cur, "pdc_dt"), "pa:{1} pd_upper:{3,1}",
                    "DROP DOMAIN ... CASCADE drops every projection storing c and keeps the others")
        expect.text(f"{_decls(cur, 'pdc_do')} | {_projs(cur, 'pdc_do')}", "pd:{c,a} | pd:{2,1}",
                    "another table's projection of the same name on a column of the same name stays")

        for t in ("pdc_dt", "pdc_dh"):
            _err(cur, f"INSERT INTO {t} VALUES (20001, 5)")
        expect.num(_one(cur, "SELECT count(*) FROM pdc_dt"), 20001,
                   "the table stays writable after DROP DOMAIN ... CASCADE")

        cur.execute("ALTER TABLE pdc_dt ADD COLUMN c int")
        cur.execute("ALTER TABLE pdc_dh ADD COLUMN c int")
        for t in ("pdc_dt", "pdc_dh"):
            _err(cur, f'INSERT INTO {t} (a, "C", c) '
                      "SELECT g, g % 70, (g * 7) % 100 FROM generate_series(20002, 24000) g")
            cur.execute(f"ANALYZE {t}")
        dheap = _rows(cur, "pdc_dh", "c")
        expect.rows([_uses(cur, "pdc_dt", "c"), _rows(cur, "pdc_dt", "c")], ["base", dheap],
                    "after c is added back, the planner reads the base and answers as heap does")
        readd = _err(cur, "SELECT pgcolumnar.add_projection('pdc_dt', 'pd', ARRAY['c','a'], ARRAY['c'])")
        expect.text(readd.sqlstate if readd else "00000", "00000",
                    "add_projection takes the name pd again on the added-back c")
        cur.execute("ANALYZE pdc_dt")
        expect.rows([_uses(cur, "pdc_dt", "c"), _rows(cur, "pdc_dt", "c")], ["pd", dheap],
                    "and the planner reads the new pd, which answers as heap does")

        # A write, the cascade and another write in one transaction. The second write
        # must not reach the dropped projection's storage through a writer cached by
        # the first.
        cur.execute("CREATE DOMAIN pdc_dx AS int")
        cur.execute("CREATE TABLE pdc_x (a int, b int, c pdc_dx) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_x', 'pxb', ARRAY['b','a'], ARRAY['b'])")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_x', 'pxc', ARRAY['c','a'], ARRAY['c'])")
        cur.execute("INSERT INTO pdc_x SELECT g, g % 10, g % 7 FROM generate_series(1, 100) g")
        pxc = _proj_sid(cur, "pdc_x", "pxc")
        try:
            with pgc_conn.transaction():
                cur.execute("INSERT INTO pdc_x SELECT g, g % 10, g % 7 FROM generate_series(101, 200) g")
                cur.execute("DROP DOMAIN pdc_dx CASCADE")
                cur.execute("INSERT INTO pdc_x SELECT g, g % 10 FROM generate_series(201, 300) g")
        except psycopg.Error:
            pass
        got = " ".join(str(_one(cur, sql)) for sql in (
            "SELECT count(*) FROM pdc_x",
            "SELECT count(*) FROM pgcolumnar.read_projection('pdc_x', 'pxb')",
            f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {pxc}"))
        expect.text(got, "300 300 0",
                    "a write, the cascade and a write in one transaction leave the dropped storage empty")

        # WHY THE CASCADE DROPS RATHER THAN REFUSES. The table predates the domain, so
        # DROP SCHEMA ... CASCADE drops its column c on its own, just before the table
        # itself. The drop hook sees that column drop as it sees DROP DOMAIN's, and a
        # refusal there failed this DROP SCHEMA.
        cur.execute("CREATE SCHEMA pdc_s")
        cur.execute("CREATE TABLE pdc_s.t (a int) USING pgcolumnar")
        cur.execute("CREATE DOMAIN pdc_s.d AS int")
        cur.execute("ALTER TABLE pdc_s.t ADD COLUMN c pdc_s.d")
        cur.execute("INSERT INTO pdc_s.t SELECT g, g % 10 FROM generate_series(1, 1000) g")
        cur.execute("SELECT pgcolumnar.add_projection('pdc_s.t', 'ps', ARRAY['c','a'], ARRAY['c'])")
        oid = _one(cur, "SELECT 'pdc_s.t'::regclass::oid")
        dropped = _err(cur, "DROP SCHEMA pdc_s CASCADE")
        left = _one(cur, f"SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel::oid = {oid}")
        expect.text(f"{dropped.sqlstate if dropped else '00000'} {left}", "00000 0",
                    "DROP SCHEMA ... CASCADE drops a table whose projection stores a later domain's column")

    crash1 = _crashes(pgc_cluster)
    expect.text(str(crash1) if crash1 is not None else "unread",
                str(crash0) if crash0 is not None else "unread-before",
                "no backend crashed during the cascade arms")

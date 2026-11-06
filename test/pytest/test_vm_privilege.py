"""The visibility-map debug entry points are owner-only, and refuse a heap relation
(#748): the pytest twin of `test/vm_privilege.sh` (#1341). Every check carries the
shell's name.

vm_selftest sets an all-visible bit; set on a block holding dead rows, it makes an
index-only scan return them. Two layers stand in the way: a REVOKE that stops a role
holding only schema USAGE, and a C ownership check that stops a role granted EXECUTE.
Both are probed with SET ROLE on a superuser connection, as the shell does, and the
damage is measured, not only the refusal: no bit set, and the index-only scan still
agrees with a sequential scan. On a HEAP relation the functions refuse even the owner
(42809), because setting heap's bits by hand corrupts its index-only scans.

The roles hold USAGE on this test's schema, as the shell's roles hold it on public by
PUBLIC's default, so a refusal is never "permission denied for schema".
"""
import pathlib
import re

import pytest

NONE, EXEC = "t_vmnone_py", "t_vmexec_py"
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _as(conn, role, sql):
    """-> (sqlstate or '', primary message or '', first value or None) under SET ROLE."""
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET ROLE {role}")
            try:
                cur.execute(sql)
                row = cur.fetchone() if cur.description else None
                return "", "", (row[0] if row else None)
            finally:
                cur.execute("RESET ROLE")
    except psycopg.Error as e:
        with conn.cursor() as cur:
            cur.execute("RESET ROLE")
        return e.sqlstate or "?", e.diag.message_primary or "", None


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _state(conn, sql):
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return ""
    except psycopg.Error as e:
        return e.sqlstate or "?"


def test_the_visibility_map_entry_points(pgc_conn, expect):
    conn = pgc_conn
    with conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        cur.execute("CREATE TABLE vmvictim (id int, secret text) USING pgcolumnar")
        cur.execute("INSERT INTO vmvictim SELECT g, 'secret-'||g FROM generate_series(1,20000) g")
        cur.execute("DELETE FROM vmvictim WHERE id % 2 = 0")
        cur.execute("ANALYZE vmvictim")
        cur.execute("CREATE TABLE vmheap (id int primary key, secret text)")
        cur.execute("INSERT INTO vmheap SELECT g, 'secret-'||g FROM generate_series(1,20000) g")
        cur.execute("DELETE FROM vmheap WHERE id % 2 = 0")
        cur.execute("ANALYZE vmheap")
        for r in (NONE, EXEC):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {r} NOSUPERUSER; "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
            cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {r}')
        cur.execute(f"GRANT EXECUTE ON FUNCTION pgcolumnar.vm_selftest(regclass,int) TO {EXEC}")
        cur.execute(f"GRANT EXECUTE ON FUNCTION pgcolumnar.vm_is_visible(regclass,int) TO {EXEC}")
    nblocks = _one(conn, "SELECT pg_relation_size('vmvictim')/8192")

    expect.num(_one(conn, "SELECT count(*) FROM vmvictim"), 10000, "the fixture has 10000 live rows")
    expect.text(str(_one(conn, f"SELECT rolsuper FROM pg_roles WHERE rolname = '{NONE}'")), "False",
                "premise: the test role is not a superuser")
    expect.text(str(_one(conn, f"SELECT has_table_privilege('{NONE}', 'vmvictim', 'SELECT')")), "False",
                "premise: the test role cannot read the victim")
    expect.text(str(_one(conn, f"SELECT has_schema_privilege('{NONE}', 'pgcolumnar', 'USAGE')")), "True",
                "premise: the test role does reach the schema (so it meets the C gate)")
    expect.text(_one(conn, "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam"
                           " WHERE c.oid = 'vmvictim'::regclass"), "pgcolumnar",
                "premise: the boundary victim is columnar, where the gate still applies")
    expect.text(_one(conn, "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam"
                           " WHERE c.oid = 'vmheap'::regclass"), "heap",
                "premise: the heap fixture really is heap, or the #748 arm is vacuous")
    expect.text(str(_one(conn, "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass, 0)")), "False",
                "no all-visible bit is set yet")
    expect.at_least(nblocks, 2, "the fixture spans more than one block")

    def ios_count():
        with conn.cursor() as cur:
            cur.execute("SET enable_seqscan = off")
            cur.execute("SET enable_bitmapscan = off")
            cur.execute("SELECT count(*) FROM vmheap WHERE id > 0")
            n = cur.fetchone()[0]
            cur.execute("RESET enable_seqscan")
            cur.execute("RESET enable_bitmapscan")
        return n

    def seq_count():
        with conn.cursor() as cur:
            cur.execute("SET enable_indexonlyscan = off")
            cur.execute("SET enable_indexscan = off")
            cur.execute("SELECT count(*) FROM vmheap WHERE id > 0")
            n = cur.fetchone()[0]
            cur.execute("RESET enable_indexonlyscan")
            cur.execute("RESET enable_indexscan")
        return n

    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("EXPLAIN (COSTS OFF) SELECT count(*) FROM vmheap WHERE id > 0")
        plan = "\n".join(r[0] for r in cur.fetchall())
        cur.execute("RESET enable_seqscan")
        cur.execute("RESET enable_bitmapscan")
    expect.text("yes" if "Index Only Scan" in plan else "no", "yes",
                "premise: the query under test really is an Index Only Scan")
    expect.num(ios_count(), 10000, "premise: the index-only scan is correct BEFORE anything is attempted")

    # ---- the attack: a role granted EXECUTE, on every block
    attack = [_as(conn, EXEC, f"SELECT pgcolumnar.vm_selftest('vmvictim'::regclass, {b})") for b in range(nblocks)]
    msgs = " | ".join(m for _, m, _ in attack)
    expect.text("yes" if "permission denied for function" in msgs else "no", "no",
                "the granted-but-not-owner role reaches the C gate, not the SQL grant")
    expect.text("yes" if "must be owner" in msgs else "no", "yes", "the C ownership check names ownership")
    _, revoke_msg, _ = _as(conn, NONE, "SELECT pgcolumnar.vm_selftest('vmvictim'::regclass, 0)")
    expect.text("yes" if "permission denied for function" in revoke_msg else "no", "yes",
                "a role with only schema USAGE is stopped by the REVOKE")
    expect.text("yes" if re.search(r"must be owner|permission denied", msgs) else "no", "yes",
                "an unprivileged role is refused by vm_selftest")
    expect.text("yes" if any(v is True for _, _, v in attack) else "no", "no",
                "vm_selftest did not report success to the unprivileged role")
    expect.text(str(_one(conn, "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass, 0)")), "False",
                "no all-visible bit was set on block 0 by the unprivileged role")
    expect.num(ios_count(), 10000, "the index-only scan still returns only live rows")
    expect.text("yes" if ios_count() == seq_count() else "no", "yes",
                "index-only and sequential scans agree after the attempt")
    _, isvis_msg, _ = _as(conn, EXEC, "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass, 0)")
    expect.text("yes" if re.search(r"must be owner|permission denied", isvis_msg) else "no", "yes",
                "an unprivileged role is refused by vm_is_visible")
    # On a table of its own (#1341): on vmvictim, an attack that got through had
    # already set block 0's bit, so this control reddened for the attack.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE vmown (id int) USING pgcolumnar")
        cur.execute("INSERT INTO vmown SELECT g FROM generate_series(1, 20000) g")
        cur.execute("DELETE FROM vmown WHERE id % 2 = 0")
    expect.text(str(_one(conn, "SELECT pgcolumnar.vm_selftest('vmown'::regclass, 0)")), "True",
                "the owner can still call vm_selftest")
    expect.text(str(_one(conn, "SELECT pgcolumnar.vm_is_visible('vmown'::regclass, 0)")), "True",
                "the owner can still call vm_is_visible")

    # ---- #748: a heap relation is refused, even for its owner
    expect.text(_state(conn, "SELECT pgcolumnar.vm_selftest('vmheap'::regclass, 0)"), "42809",
                "vm_selftest refuses a heap relation even for its owner (#748)")
    expect.text(_state(conn, "SELECT pgcolumnar.vm_is_visible('vmheap'::regclass, 0)"), "42809",
                "vm_is_visible refuses a heap relation even for its owner (#748)")
    heap_blocks = _one(conn, "SELECT pg_relation_size('vmheap')/8192")
    for b in range(heap_blocks):
        _state(conn, f"SELECT pgcolumnar.vm_selftest('vmheap'::regclass, {b})")
    expect.at_least(heap_blocks, 2, "premise: the heap sweep covered more than one block")
    expect.num(ios_count(), 10000, "the heap index-only scan still returns only live rows after the sweep (#748)")
    expect.num(ios_count(), seq_count(), "and it still agrees with the sequential scan (#748)")
    expect.text(_state(conn, "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass, 0)") or "accepted", "accepted",
                "the same call on a columnar relation is still accepted (#748 control)")

    # ---- coverage: every vm_* function the install script creates is probed above
    control = (ROOT / "pgcolumnar.control").read_text()
    ver = re.search(r"default_version = '([^']+)'", control).group(1)
    sql = (ROOT / f"pgcolumnar--{ver}.sql").read_text()
    discovered = sorted(set(re.findall(r"CREATE FUNCTION pgcolumnar\.(vm_[a-z_]+)", sql)))
    covered = {"vm_selftest", "vm_is_visible"}
    for fn in discovered:
        expect.text("yes" if fn in covered else "no", "yes",
                    f"vm entry point is covered by this boundary test: {fn}")
    expect.num(len(discovered), 2, "coverage scan found the vm functions (not an empty scan)")

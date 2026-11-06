"""The debug metadata mutators are owner-only (#707): the pytest twin of
`test/debug_hook_privilege.sh` (#1341). Every check carries the shell's name.

pgcolumnar_debug_advance_reserved_offset and pgcolumnar_debug_set_metapage_version
ship unbound, but one CREATE FUNCTION binds them, and both mutate storage metadata:
one burns reserved file space, the other overwrites the metapage format version and
makes the table unreadable. They are bound here as the test suites bind them, and
probed by a NON-OWNER role on its own login. A refusal is 42501 AND "must be owner",
because 42501 alone could be an EXECUTE denial. The mirrors show the gate is
ownership, not superuser-ness: the same role may mutate its own table.

A PRIVATE DATABASE, because the last mirror leaves a table whose version is
overwritten, and dropping such a table fails: a per-test schema would fail its own
teardown. Dropping the database does not read the table.
"""
import pytest

PEON = "peon_dbg_py"


def _as(cluster, db, role, sql):
    """-> (sqlstate or '', primary message or '', first value or None) on a fresh login."""
    import psycopg
    dsn = f"host=127.0.0.1 port={cluster.port} user={role} dbname={db}"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            cur = c.execute(sql)
            row = cur.fetchone() if cur.description else None
            return "", "", (row[0] if row else None)
    except psycopg.Error as e:
        return e.sqlstate or "?", e.diag.message_primary or "", None


def test_the_debug_mutators_are_owner_only(pgc_own_db, pgc_cluster, expect):
    conn = pgc_own_db
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        db = cur.fetchone()[0]
        cur.execute("CREATE FUNCTION public.dbg_advance(regclass, int) RETURNS void"
                    " AS 'pgcolumnar', 'pgcolumnar_debug_advance_reserved_offset' LANGUAGE C")
        cur.execute("CREATE FUNCTION public.dbg_setver(regclass, int, int) RETURNS void"
                    " AS 'pgcolumnar', 'pgcolumnar_debug_set_metapage_version' LANGUAGE C")
        cur.execute(f"DO $$ BEGIN CREATE ROLE {PEON} LOGIN; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        cur.execute(f"GRANT CREATE, USAGE ON SCHEMA public TO {PEON}")
        cur.execute("CREATE TABLE owned (id int) USING pgcolumnar")
        cur.execute("INSERT INTO owned VALUES (1), (2)")

    # ---- premises: bound, callable, and the role really runs its statements
    # An arm, not a premise: a hook that refused every caller reddens it (#1381 review).
    expect.text(_as(pgc_cluster, db, "postgres", "SELECT public.dbg_advance('owned', 0)")[0] or "ok", "ok",
                "the owner can call the advance hook (no-op advance)")
    expect.text(str(_as(pgc_cluster, db, PEON, "SELECT current_user")[2]), PEON,
                "premise: the peon role runs its own statements")

    # ---- the gate: a non-owner is refused by ownership, before any mutation
    state, msg, _ = _as(pgc_cluster, db, PEON, "SELECT public.dbg_advance('owned', 1)")
    expect.text(state, "42501", "peon cannot advance another owner's reserved offset (42501)")
    expect.num(1 if "must be owner" in msg else 0, 1, "and the refusal is the owner gate, not an EXECUTE denial")
    state, msg, _ = _as(pgc_cluster, db, PEON, "SELECT public.dbg_setver('owned', 99, 0)")
    expect.text(state, "42501", "peon cannot overwrite another owner's metapage version (42501)")
    expect.num(1 if "must be owner" in msg else 0, 1, "and that refusal is the owner gate too")
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM owned")
            still = cur.fetchone()[0]
    except psycopg.Error:
        still = -1  # unreadable: a red arm by name, not a crash (#1381 review)
    expect.num(still, 2, "the gated table still reads (version untouched by the refused calls)")

    # ---- mirrors: the gate is ownership, not superuser-ness
    with conn.cursor() as cur:
        cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {PEON}")
    _as(pgc_cluster, db, PEON, "CREATE TABLE peon_t (id int) USING pgcolumnar")
    expect.text(_as(pgc_cluster, db, PEON, "SELECT public.dbg_advance('peon_t', 0)")[0] or "ok", "ok",
                "peon may advance its OWN table (gate is ownership)")
    _as(pgc_cluster, db, PEON, "CREATE TABLE peon_scratch (id int) USING pgcolumnar")
    expect.text(_as(pgc_cluster, db, PEON, "SELECT public.dbg_setver('peon_scratch', 99, 0)")[0] or "ok", "ok",
                "peon may even brick its OWN scratch table's version (gate is ownership)")
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1, "backend alive")

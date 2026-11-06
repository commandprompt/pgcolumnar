"""The direct-storage paths refuse a relation with RLS in force (#563): the pytest twin
of `test/rls_direct_storage.sh` (#1341). Every check carries the shell's name.

Row-level security is applied by the REWRITER to a query's range table entry. These
functions open the relation's storage and read or write it, so no policy is ever
consulted. The fixture that matters is a role that HOLDS SELECT and INSERT and is
restricted by a policy: a role with no privilege is refused by the ACL check first,
and with the RLS guard deleted a no-privilege arm stays green. Only a granted but
restricted caller reaches the RLS check with every other refuser cleared.

The bar is check_enable_rls(...) == RLS_ENABLED. The owner without FORCE (and a
superuser) is RLS_NONE_ENV and stays allowed; under FORCE a NON-superuser owner is
refused too, which is why an owner-only rule would not close the hole.

REAL LOGINS, NOT `SET ROLE`, as in the shell twin. Every role holds USAGE on this
test's schema, as the shell's roles hold it on public, so a "permission denied" is
never the schema's.
"""
import os

import pytest

RLS, NONE, OWNER = "t_rls_py", "t_rlsnone_py", "t_owner_rls_py"
ROWS = 200


def _login(cluster, role, schema, sql):
    """-> (first value or None, error or None) for `sql` on a fresh login as `role`."""
    import psycopg
    dsn = f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f'SET search_path TO "{schema}", pgcolumnar, public')
            cur = c.execute(sql)
            row = cur.fetchone() if cur.description else None
            return (row[0] if row else None), None
    except psycopg.Error as e:
        return None, e


def _verdict(cluster, schema, sql, role=RLS):
    """-> rls | acl | allowed | other: <state> <message>, as the shell's verdict."""
    _, err = _login(cluster, role, schema, sql)
    if err is None:
        return "allowed"
    msg = err.diag.message_primary or ""
    if "row-level security" in msg:
        return "rls"
    if "permission denied" in msg:
        return "acl"
    return f"other: {err.sqlstate} {msg}"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def test_the_direct_storage_paths_refuse_a_policy_restricted_caller(pgc_conn, pgc_cluster, server_dir, expect):
    conn = pgc_conn
    out = str(server_dir)
    schema = _one(conn, "SELECT current_schema()")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE rls_t (id int, owner_name text, ssn text) USING pgcolumnar")
        cur.execute(f"INSERT INTO rls_t SELECT g, CASE WHEN g = 1 THEN '{RLS}' ELSE 'someone' END,"
                    f" 'ssn-'||g FROM generate_series(1,{ROWS}) g")
        cur.execute("CREATE TABLE plain_t (id int, v text) USING pgcolumnar")
        cur.execute(f"INSERT INTO plain_t SELECT g, 'v'||g FROM generate_series(1,{ROWS}) g")
        for t in ("rls_t", "plain_t"):
            cur.execute(f"SELECT pgcolumnar.add_projection('{t}','p1',ARRAY['id'],ARRAY['id'])")
            cur.execute(f"SELECT pgcolumnar.rebuild_projections('{t}')")
        for r in (RLS, NONE, OWNER):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {r} NOSUPERUSER LOGIN; "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
            cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {r}')
        cur.execute(f"GRANT pg_read_server_files, pg_write_server_files TO {RLS}")
        # SELECT and INSERT DELIBERATELY: this role must clear every bar but RLS.
        cur.execute(f"GRANT SELECT, INSERT ON rls_t TO {RLS}")
        cur.execute(f"GRANT SELECT, INSERT ON plain_t TO {RLS}")
        # #567 revoked EXECUTE on the projection readers from PUBLIC; without these
        # grants the projection arms stop at the grant layer and prove nothing here.
        for r in (RLS, NONE, OWNER):
            cur.execute(f"GRANT EXECUTE ON FUNCTION pgcolumnar.read_projection(regclass,text) TO {r}")
        cur.execute(f"GRANT EXECUTE ON FUNCTION pgcolumnar.reconstruct_via_projection(regclass,text) TO {RLS}")
        # Sources exported from rls_t BY THE OWNER, so their shape matches the import
        # target: a differently shaped source fails on column counts, not on RLS.
        # BEFORE RLS is enabled, so no version of the guard under test can stop them.
        cur.execute(f"SELECT pgcolumnar.export_parquet('rls_t','{out}/src.parquet')")
        cur.execute(f"SELECT pgcolumnar.export_arrow('rls_t','{out}/src.arrow')")
        cur.execute(f"COPY rls_t TO '{out}/src.txt'")
        cur.execute("ALTER TABLE rls_t ENABLE ROW LEVEL SECURITY")
        cur.execute(f"CREATE POLICY p_own ON rls_t FOR SELECT TO {RLS} USING (owner_name = current_user)")
        cur.execute(f"CREATE POLICY p_ins ON rls_t FOR INSERT TO {RLS} WITH CHECK (owner_name = current_user)")

    def as_rls(sql):
        return _login(pgc_cluster, RLS, schema, sql)

    # ---- premises, every one run BY the restricted role
    expect.text(str(as_rls("SELECT 1")[0]), "1", "premise: the role can open a session")
    expect.text(str(as_rls("SELECT has_table_privilege('rls_t','SELECT')")[0]), "True",
                "premise: the role HOLDS select, so the ACL check cannot be what refuses")
    got, _ = as_rls("SELECT count(*) FROM rls_t")
    expect.num(-1 if got is None else got, 1, "the policy is in force for ordinary SQL")
    expect.text(str(as_rls("SELECT has_function_privilege('pgcolumnar.read_projection(regclass,text)','EXECUTE')")[0]),
                "True", "premise: the role holds EXECUTE, so the grant layer is not what refuses")
    expect.num(_one(conn, "SELECT count(*) FROM rls_t"), ROWS,
               "the owner sees every row, so the policy hides something real")
    expect.text("yes" if all(os.path.exists(f"{out}/{f}") and os.path.getsize(f"{out}/{f}") > 0
                                for f in ("src.parquet", "src.arrow", "src.txt"))
                else "no", "yes", "the source files the import arms need exist")

    # ---- the refusals: on unfixed code each returns "allowed", which is the leak
    def v(sql):
        return _verdict(pgc_cluster, schema, sql)

    expect.text(v("SELECT count(*) FROM pgcolumnar.read_projection('rls_t','p1')"), "rls",
                "read_projection refuses a policy-restricted caller")
    expect.text(v("SELECT count(*) FROM pgcolumnar.reconstruct_via_projection('rls_t','p1')"), "rls",
                "reconstruct_via_projection refuses one too")
    expect.text(v(f"SELECT pgcolumnar.export_parquet('rls_t','{out}/leak1.parquet')"), "rls",
                "export_parquet refuses one")
    expect.text(v(f"SELECT pgcolumnar.export_arrow('rls_t','{out}/leak2.arrow')"), "rls",
                "export_arrow refuses one")
    expect.text(v(f"SELECT pgcolumnar.parallel_export_parquet('rls_t','{out}/leak3',2)"), "rls",
                "parallel_export_parquet refuses one")
    expect.text(v(f"SELECT pgcolumnar.import_parquet('rls_t','{out}/src.parquet')"), "rls",
                "import_parquet refuses a policy-restricted writer")
    expect.text(v(f"SELECT pgcolumnar.import_arrow('rls_t','{out}/src.arrow')"), "rls",
                "import_arrow refuses one")
    expect.text(v(f"SELECT pgcolumnar.parallel_copy('rls_t','{out}/src.txt',2)"), "rls",
                "parallel_copy refuses one")

    # The refusal must not have done the work anyway. A message is not a behaviour.
    written = [f for f in ("leak1.parquet", "leak2.arrow", "leak3") if os.path.exists(f"{out}/{f}")]
    expect.text(" ".join(written) or "none", "none", "no export file was written for the refused caller")
    expect.num(_one(conn, "SELECT count(*) FROM rls_t"), ROWS, "and no imported row landed in the restricted table")

    # ---- a caller WITHOUT privilege sees the ACL error, not the RLS one
    _, err = _login(pgc_cluster, NONE, schema, "SELECT count(*) FROM rls_t")
    expect.num(1 if err is not None and "permission denied" in (err.diag.message_primary or "") else 0, 1,
               "premise: the no-privilege role is told permission denied by ordinary SQL")
    _, err = _login(pgc_cluster, NONE, schema, "SELECT count(*) FROM pgcolumnar.read_projection('rls_t','p1')")
    msg = (err.diag.message_primary or "") if err is not None else ""
    expect.num(1 if "permission denied" in msg else 0, 1,
               "and read_projection tells it the same thing, not that RLS exists")
    expect.num(1 if "row-level security" in msg else 0, 0,
               "so no caller without privilege learns RLS state from these functions")

    # ---- not over-refusing: a fix that refused everyone would pass the arms above
    expect.text(v("SELECT count(*) FROM pgcolumnar.read_projection('plain_t','p1')"), "allowed",
                "a table with NO policy is still readable by the same role")
    expect.text(v(f"SELECT pgcolumnar.export_parquet('plain_t','{out}/ok1.parquet')"), "allowed",
                "and still exportable by it")
    # Against the LIVE count: on unfixed code the import arms leak rows into rls_t.
    owner_rows = _one(conn, "SELECT count(*) FROM rls_t")
    import psycopg
    try:
        owner_read = _one(conn, "SELECT count(*) FROM pgcolumnar.read_projection('rls_t','p1')")
    except psycopg.Error:
        owner_read = -1  # refused: a red arm by name, not a crash
    expect.num(owner_read, owner_rows, "the OWNER can still read the RLS table's storage (RLS_NONE_ENV)")
    try:
        _one(conn, f"SELECT pgcolumnar.export_parquet('rls_t','{out}/owner.parquet')")
    except psycopg.Error:
        pass
    # No file, an empty one, or one that does not read holds no rows: a refused or
    # empty export is a red arm by name, not a crash (#1383 review).
    exported = 0
    if os.path.exists(f"{out}/owner.parquet") and os.path.getsize(f"{out}/owner.parquet") > 0:
        try:
            exported = _one(conn, f"SELECT count(*) FROM pgcolumnar.read_parquet('{out}/owner.parquet') AS (id int,"
                                  " owner_name text, ssn text)")
        except psycopg.Error:
            exported = 0
    expect.num(exported, owner_rows, "and the owner can still export it")

    # ---- FORCE ROW LEVEL SECURITY reaches a NON-superuser owner too
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE force_t (id int, owner_name text) USING pgcolumnar")
        cur.execute("INSERT INTO force_t SELECT g, 'nobody' FROM generate_series(1,50) g")
        cur.execute("SELECT pgcolumnar.add_projection('force_t','p1',ARRAY['id'],ARRAY['id'])")
        cur.execute("SELECT pgcolumnar.rebuild_projections('force_t')")
        cur.execute(f"ALTER TABLE force_t OWNER TO {OWNER}")
        cur.execute("ALTER TABLE force_t ENABLE ROW LEVEL SECURITY")
        cur.execute(f"CREATE POLICY p_none ON force_t FOR SELECT TO {OWNER} USING (owner_name = current_user)")
    expect.text(_one(conn, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'force_t'::regclass"),
                OWNER, "premise: t_owner owns force_t")
    got, _ = _login(pgc_cluster, OWNER, schema, "SELECT count(*) FROM pgcolumnar.read_projection('force_t','p1')")
    expect.num(-1 if got is None else got, 50, "without FORCE the owner is allowed, because RLS_NONE_ENV is not RLS_ENABLED")
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE force_t FORCE ROW LEVEL SECURITY")
    _, err = _login(pgc_cluster, OWNER, schema, "SELECT count(*) FROM pgcolumnar.read_projection('force_t','p1')")
    expect.num(1 if err is not None and "row-level security" in (err.diag.message_primary or "") else 0, 1,
               "under FORCE the non-superuser owner IS refused, so owner-only is no substitute")

"""Every relation-taking entry point has a decided privilege bar (#569): the pytest
twin of `test/entry_point_privilege.sh` (#1341). Every check carries the shell's name.

The shell's three design rules hold here, and are why this file is shaped as it is:

  1. ENUMERATE FROM THE CATALOG. pg_proc.prosrc IS the C symbol, so no naming
     convention can drop a member (two hand-written enumerations once lost
     get_storage_id and columnar_handler that way). The install script is then
     cross-checked against the catalog as a SET.
  2. BUCKET ON THE TYPE. "Takes a relation" is proargtypes[0] = regclass, never a
     parameter name or a line of the script.
  3. ASSERT SQLSTATE. Only aclcheck_error produces 42501; "permission denied" text
     is also satisfied by a login FATAL or a schema refusal.

The deny arms run as a role on its own LOGIN that holds EXECUTE on each function,
so the SQL grant layer is cleared and the C check is the only refuser left. Each
is preceded by the owner making the identical call, so a refusal is not a broken
call. The role holds USAGE on this test's schema, as the shell's role holds it on
public, so no refusal is the schema's.
"""
import pathlib
import re

import pytest

ROLE = "t_ep_py"
ROOT = pathlib.Path(__file__).resolve().parents[2]

# Functions that take no relation, by name, each with its reason in the shell twin.
EXEMPT = {"columnar_handler", "parquet_fdw_handler", "parquet_fdw_validator", "iceberg_fdw_handler",
          "iceberg_fdw_validator", "iceberg_catalog_fdw_validator", "parquet_schema", "read_parquet",
          "file_split_offsets", "iceberg_current_snapshot", "iceberg_data_files", "iceberg_scan",
          "iceberg_rest_table_location", "iceberg_rest_scan", "iceberg_rest_namespaces",
          "iceberg_rest_tables", "read_avro_manifest", "read_manifest_list", "row_tid",
          # A trigger function (#1376): never called directly, it runs on writes to
          # pgcolumnar.rollup and checks SELECT on the source itself (test_rollup.py).
          "rollup_record_dependencies"}
# Relation-taking entry points with no privilege check of any kind: none, and a
# name added here is a claim that something is ungated.
KNOWN_UNGUARDED: set = set()
# Act on a relation without declaring one as their first argument.
RELATION_BY_OTHER_MEANS = {"alter_table_set_access_method", "vacuum_full", "create_rollup"}
# Not C: governed by the ACL on the catalog tables they read (#560).
CATALOG_GOVERNED = {"analyze", "rebuild_projections", "reset_options", "set_options", "sort_status", "stats"}
# Asserted behaviourally below, each with a call the owner can make.
GUARDED_BEHAVIOURAL = {
    "read_projection": "SELECT pgcolumnar.read_projection('ep_target','p1')",
    "reconstruct_via_projection": "SELECT pgcolumnar.reconstruct_via_projection('ep_target','p1')",
    "vm_is_visible": "SELECT pgcolumnar.vm_is_visible('ep_target',0)",
    "vm_selftest": "SELECT pgcolumnar.vm_selftest('ep_target',0)",
    "get_storage_id": "SELECT pgcolumnar.get_storage_id('ep_target')",
    # #1386: no arm anywhere called these as a caller their bar should stop.
    "cluster_hilbert": "SELECT pgcolumnar.cluster_hilbert('ep_target', 'id')",
    "recluster_hilbert": "SELECT pgcolumnar.recluster_hilbert('ep_target', 'id')",
    "truncate": "SELECT pgcolumnar.truncate('ep_target')",
    "require_caller_select": "SELECT pgcolumnar.require_caller_select('ep_target')",
}
# Where each other claimed-guarded entry point's bar is asserted (#1386): the pytest
# file whose tests call it as a caller its bar should stop. A suite with no pytest twin
# yet is named as such, so the name is still accounted for and a new one is not.
NO_TWIN_YET = "no pytest twin yet: "
BAR_ASSERTED_IN = {
    **{f: "test_vacuum_lock_privilege.py" for f in ("add_projection", "drop_projection", "vacuum",
                                                    "vacuum_sorted", "cluster", "recluster",
                                                    "compact", "compact_rewrite")},
    **{f: "test_import_export_privilege.py" for f in ("export_arrow", "export_parquet", "import_arrow",
                                                      "import_parquet", "parallel_copy",
                                                      "parallel_export_parquet")},
    "expire": "test_ttl_expire.py",
    "column_extent": "test_column_extent.py",
    "maintenance_due": NO_TWIN_YET + "maintenance_due.sh",
    **{f: "test_rollup.py" for f in ("refresh_rollup", "rebase_rollup", "rollup_source_groups",
                                     "rollup_source_retired")},
    "tier_partition": "test_tier_partition.py",
    "partition_create_ahead": "test_partition_lifecycle.py",
}
# Relation-taking, with no bar to assert: they return what pg_inherits and the
# partition bounds already tell any role, and the plan's statements run as their caller.
NO_BAR_NEEDED = {"partition_drop_candidates", "partition_drop_plan"}
FILE_SIBLINGS = ("export_arrow", "export_parquet", "import_arrow", "import_parquet")


def _words(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return {r[0] for r in cur.fetchall()}


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _as_ep(cluster, schema, sql):
    """-> (sqlstate or 'noerror', primary message or '', first value or None) on a login as ROLE."""
    import psycopg
    dsn = f"host=127.0.0.1 port={cluster.port} user={ROLE} dbname=postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f'SET search_path TO "{schema}", pgcolumnar, public')
            cur = c.execute(sql)
            row = cur.fetchone() if cur.description else None
            return "noerror", "", (row[0] if row else None)
    except psycopg.Error as e:
        return e.sqlstate or "?", e.diag.message_primary or "", None


def _install_symbols():
    """The MODULE_PATHNAME symbols the install script declares, comments blanked first."""
    control = (ROOT / "pgcolumnar.control").read_text()
    ver = re.search(r"default_version *= *'([^']+)'", control).group(1)
    text = (ROOT / f"pgcolumnar--{ver}.sql").read_text()
    text = " ".join(re.sub(r"--.*", "", line) for line in text.split("\n"))
    return set(re.findall(r"AS 'MODULE_PATHNAME'[, ]*'([a-z_]*)'", text))


def test_every_relation_taking_entry_point_has_a_decided_bar(pgc_conn, pgc_cluster, expect):
    conn = pgc_conn
    schema = _one(conn, "SELECT current_schema()")
    in_ext = ("FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'pgcolumnar'")
    is_c = " AND p.prolang = (SELECT oid FROM pg_language WHERE lanname = 'c')"
    installed = _one(conn, f"SELECT count(*) {in_ext}{is_c}")
    symbols = _words(conn, f"SELECT DISTINCT p.prosrc {in_ext}{is_c}")
    # NO LANGUAGE FILTER on the population: one once dropped the six sql/plpgsql
    # relation-taking functions while the coverage arm still balanced.
    relfns = _words(conn, f"SELECT DISTINCT p.proname {in_ext} AND p.proargtypes[0] = 'regclass'::regtype")
    allfns = _words(conn, f"SELECT DISTINCT p.proname {in_ext}")

    # ---- premises: the enumeration is real
    expect.at_least(installed, 20, "premise: the extension is installed and C entry points were found")
    expect.text("yes" if relfns else "no", "yes", "premise: at least one relation-taking entry point was found")
    src_syms = _install_symbols()
    expect.at_least(len(src_syms), 20, "premise: the install script declares MODULE_PATHNAME symbols at all")
    expect.text("same" if src_syms == symbols else "differs: " + " ".join(sorted(src_syms ^ symbols)), "same",
                "the catalog and the install script agree on the symbol set")

    # ---- the coverage assertion: every entry point lands in exactly one bucket
    exempt_seen = rel_seen = other_seen = 0
    unbucketed = []
    for fn in sorted(allfns):
        if fn in EXEMPT:
            exempt_seen += 1
        elif fn in relfns:
            rel_seen += 1
        elif fn in RELATION_BY_OTHER_MEANS:
            other_seen += 1
        else:
            unbucketed.append(fn)
    missing_cg = sorted(CATALOG_GOVERNED - relfns)
    expect.text(" ".join(missing_cg) or "none", "none",
                "every catalog-governed name is still a relation-taking entry point")
    expect.num(len(allfns), exempt_seen + rel_seen + other_seen + len(unbucketed),
               "coverage: inputs == sum(buckets)")
    expect.num(other_seen, 3, "the relation-by-other-means class is the three that are known")
    expect.text(" ".join(unbucketed) or "none", "none", "coverage: no entry point is unbucketed")
    # EXEMPT is an escape hatch: a relation-taking name inside it leaves the
    # population and the coverage arm still balances.
    expect.text(" ".join(sorted(EXEMPT & relfns)) or "none", "none", "no exempt name actually takes a relation")

    stale = sorted(KNOWN_UNGUARDED - relfns)
    expect.text(" ".join(stale) or "none", "none", "no pinned name is stale (all still exist and take a relation)")
    catalog_n = len(relfns & CATALOG_GOVERNED)
    pinned_n = len((relfns - CATALOG_GOVERNED) & KNOWN_UNGUARDED)
    guarded_n = len(relfns - CATALOG_GOVERNED - KNOWN_UNGUARDED)
    expect.num(len(relfns), pinned_n + guarded_n + catalog_n,
               "coverage: relation-taking == pinned + claimed-guarded + catalog-governed")
    expect.num(catalog_n, 6, "the catalog-governed class is the six from #560")
    expect.num(pinned_n, 0, "no relation-taking C entry point is still ungated")

    # ---- every claimed-guarded entry point names where its bar is asserted (#1386)
    claimed = sorted(relfns - CATALOG_GOVERNED - KNOWN_UNGUARDED - NO_BAR_NEEDED - set(GUARDED_BEHAVIOURAL))
    unhomed = [f for f in claimed if f not in BAR_ASSERTED_IN]
    badhome = []
    for f in claimed:
        home = BAR_ASSERTED_IN.get(f)
        if home is None or home.startswith(NO_TWIN_YET):
            continue
        path = pathlib.Path(__file__).resolve().parent / home
        if not (path.is_file() and f"pgcolumnar.{f}(" in path.read_text()):
            badhome.append(f"{f}:{home}")
    stale = sorted((set(BAR_ASSERTED_IN) | NO_BAR_NEEDED) - relfns)
    expect.text(" ".join(unhomed) or "none", "none",
                "every claimed-guarded entry point names the suite that asserts its bar (#1386)")
    expect.text(" ".join(badhome) or "none", "none", "and each named suite exists and calls that entry point")
    expect.text(" ".join(stale) or "none", "none",
                "and no entry names a function that is not a relation-taking entry point")

    # ---- the behavioural arms
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE ep_target (id int, v text) USING pgcolumnar")
        cur.execute("INSERT INTO ep_target SELECT g, 'v'||g FROM generate_series(1,10) g")
        cur.execute("REVOKE ALL ON ep_target FROM PUBLIC")
        cur.execute(f"DO $$ BEGIN CREATE ROLE {ROLE} NOSUPERUSER LOGIN; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {ROLE}")
        cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {ROLE}')
    expect.text(str(_as_ep(pgc_cluster, schema, "SELECT 1")[2]), "1", "premise: t_ep can open a session")
    expect.text(_as_ep(pgc_cluster, schema, "SELECT count(*) FROM ep_target")[0], "42501",
                "premise: t_ep cannot read the table by any ordinary route")

    with conn.cursor() as cur:
        cur.execute("SELECT pgcolumnar.add_projection('ep_target','p1',ARRAY['id'],ARRAY['id'])")
        cur.execute("SELECT pgcolumnar.rebuild_projections('ep_target')")
        # EXECUTE on every overload, by OID: the role must clear the SQL layer, or
        # the REVOKE answers 42501 whether or not the C check exists.
        for fn in GUARDED_BEHAVIOURAL:
            cur.execute(f"SELECT p.oid::regprocedure::text {in_ext} AND p.proname = %s", (fn,))
            for (sig,) in cur.fetchall():
                cur.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {ROLE}")
    expect.text(str(_one(conn, f"SELECT bool_and(has_function_privilege('{ROLE}', p.oid, 'EXECUTE')) {in_ext}"
                               " AND p.proname IN ('read_projection','reconstruct_via_projection',"
                               "'vm_is_visible','vm_selftest','cluster_hilbert','recluster_hilbert',"
                               "'truncate','require_caller_select')")), "True",
                "premise: t_ep now holds EXECUTE, so a refusal below can only be the C check")

    import psycopg
    expect.num(len(GUARDED_BEHAVIOURAL), 9, "premise: the behavioural loop covers the nine guarded names")
    # In declaration order, as the shell's loop runs: the projection readers before the
    # Hilbert rewrites.
    for fn, sql in GUARDED_BEHAVIOURAL.items():
        try:
            _one(conn, sql)
            owner = "ok"
        except psycopg.Error:
            owner = "broken"
        expect.text(owner, "ok", f"the owner can call {fn}, so a refusal below is a refusal")
        expect.text(_as_ep(pgc_cluster, schema, sql)[0], "42501", f"{fn} refuses an unprivileged caller with 42501")

    # The four file siblings gate on a server-file role BEFORE the table, so the role
    # holds both or it is refused by the wrong layer. A path that cannot exist still
    # discriminates: an unguarded function reaches the file layer and fails there.
    with conn.cursor() as cur:
        cur.execute(f"GRANT pg_read_server_files, pg_write_server_files TO {ROLE}")
    expect.text(str(_as_ep(pgc_cluster, schema, f"SELECT pg_has_role('{ROLE}','pg_read_server_files','member')")[2]),
                "True", "premise: t_ep holds the server-file roles, so it reaches the table check")
    for fn in FILE_SIBLINGS:
        _, msg, _ = _as_ep(pgc_cluster, schema, f"SELECT pgcolumnar.{fn}('ep_target','/nonexistent/dir/x')")
        if "permission denied for table" in msg:
            state = "refused"
        elif "could not open file" in msg:
            state = "reached-the-file-layer"
        else:
            state = f"unexpected: {msg}"
        expect.text(state, "refused", f"{fn} refuses a caller with no privilege before touching the file")

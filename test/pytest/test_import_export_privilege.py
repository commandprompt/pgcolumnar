"""Import and export check the table's privilege, not only the server-file role
(#559, #566), and the parallel twins check it too (#1341). The pytest twin of
`test/import_export_privilege.sh`.

LAYER ISOLATION. These functions have three refusers before the table check: USAGE
on schema pgcolumnar, EXECUTE on the function, and the server-file role. The
attacker role holds ALL THREE and no privilege on the target table, so the table
ACL is the only thing that can refuse, and a refusal is asserted as SQLSTATE 42501
with a message that names the table: 42501 alone also comes from a schema or a
function refusal, and the name alone could come from any error about the table.

THE PARALLEL TWINS. `parallel_copy` and `parallel_export_parquet` always checked
INSERT and SELECT, and no arm asked them. Their loaders and workers skip the
executor's permission check, so the check before anything is spawned is the only
one, and with it deleted the attacker writes into or reads out of the table.

REAL LOGINS, NOT `SET ROLE`, as in the shell twin: a failed SET ROLE leaves the
session as the superuser, and a deny arm would then read as allowed for the wrong
reason. Every file is in `server_dir`, which the server can write.
"""

import pytest

ATTACKER = "t_iep_files"
VICTIM_ROWS = 200
MINE_ROWS = 50


def _as(cluster, role, sql, schema):
    """-> (first column of the first row, error) for `sql` on a fresh login as `role`."""
    import psycopg

    dsn = f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}", pgcolumnar, public')
                cur.execute(sql)
                row = cur.fetchone() if cur.description else None
                return (row[0] if row else None), None
    except psycopg.Error as exc:
        return None, exc


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _verdict(err, table):
    """'denied' only for the table ACL: 42501 with a message naming `table`."""
    if err is None:
        return "allowed"
    msg = err.diag.message_primary or ""
    if err.sqlstate == "42501" and f'table {table}' in msg:
        return "denied"
    return f"{err.sqlstate} {msg}"


@pytest.fixture
def iep(pgc_conn, server_dir):
    """The victim, the attacker's own table and its files, and the paths an attempt
    would write. Returns (schema, paths)."""
    p = {k: str(server_dir / v) for k, v in (
        ("mine_arrow", "mine.arrow"), ("mine_parq", "mine.parquet"), ("mine_text", "mine.txt"),
        ("arrow_out", "iep_out.arrow"), ("parq_out", "iep_out.parquet"),
        ("pex_out", "iep_pex_out.parquet"), ("mine2_arrow", "mine2.arrow"),
        ("mine2_parq", "mine2.parquet"), ("mine_pex", "mine_pex.parquet"))}
    with pgc_conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{ATTACKER}'")
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {ATTACKER} NOSUPERUSER LOGIN")
        cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {ATTACKER}")
        cur.execute(f"GRANT pg_read_server_files, pg_write_server_files TO {ATTACKER}")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {ATTACKER}')
        cur.execute("CREATE TABLE victim (id int, secret text) USING pgcolumnar")
        cur.execute(f"INSERT INTO victim SELECT g, 'ssn-'||g FROM generate_series(1,{VICTIM_ROWS}) g")
        cur.execute("REVOKE ALL ON victim FROM PUBLIC")
        cur.execute("CREATE TABLE mine (id int, secret text) USING pgcolumnar")
        cur.execute(f"ALTER TABLE mine OWNER TO {ATTACKER}")
        cur.execute(f"INSERT INTO mine SELECT g, 'x'||g FROM generate_series(1,{MINE_ROWS}) g")
        cur.execute(f"SELECT pgcolumnar.export_arrow('mine'::regclass, '{p['mine_arrow']}')")
        cur.execute(f"SELECT pgcolumnar.export_parquet('mine'::regclass, '{p['mine_parq']}')")
        cur.execute(f"COPY (SELECT id, secret FROM mine) TO '{p['mine_text']}' WITH (FORMAT text)")
    return schema, p


def test_the_premises_isolate_the_table_acl(pgc_conn, pgc_cluster, iep, expect):
    schema, p = iep
    import os
    one, err = _as(pgc_cluster, ATTACKER, "SELECT 1", schema)
    expect.text(f"{one}|{err}", "1|None", "premise: the attacker can open a session")
    expect.text(str(_one(pgc_conn, f"SELECT pg_has_role('{ATTACKER}','pg_read_server_files','USAGE')")),
                "True", "premise: the attacker holds pg_read_server_files")
    expect.text(str(_one(pgc_conn, f"SELECT pg_has_role('{ATTACKER}','pg_write_server_files','USAGE')")),
                "True", "premise: the attacker holds pg_write_server_files")
    expect.text(str(_one(pgc_conn, f"SELECT has_schema_privilege('{ATTACKER}','pgcolumnar','USAGE')")),
                "True", "premise: the attacker reaches the schema (so a refusal is not schema USAGE)")
    expect.text(str(_one(pgc_conn, f"SELECT has_table_privilege('{ATTACKER}','victim','SELECT')"
                                   f" OR has_table_privilege('{ATTACKER}','victim','INSERT')")),
                "False", "premise: the attacker has NO privilege on victim")
    expect.text(str(_one(pgc_conn, f"SELECT has_function_privilege('{ATTACKER}',"
                                   "'pgcolumnar.import_parquet(regclass,text)','EXECUTE')")),
                "True", "premise: the attacker can execute the functions (so a refusal is the table ACL)")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM victim"), VICTIM_ROWS,
               "premise: victim has its rows before any import is attempted")
    expect.text("yes" if all(os.path.getsize(p[k]) > 0 for k in ("mine_arrow", "mine_parq")) else "no",
                "yes", "premise: the attacker's own source files exist")


def test_a_table_the_caller_holds_no_grant_on_is_refused(pgc_conn, pgc_cluster, iep, expect):
    """Every deny arm, in the shell twin's order, then the consequence asked once:
    nothing landed in victim and no file of it was written, whatever any refusal
    said. Under a mutation only the arm whose check is gone fails, so the hard
    assert stops on the arm that names it."""
    import os
    schema, p = iep

    def verdict(sql):
        return _verdict(_as(pgc_cluster, ATTACKER, sql.format(**p), schema)[1], "victim")

    expect.text(verdict("SELECT pgcolumnar.import_parquet('victim'::regclass, '{mine_parq}')"), "denied",
                "import_parquet refuses INSERT into a table the caller cannot write")
    expect.text(verdict("SELECT pgcolumnar.import_arrow('victim'::regclass, '{mine_arrow}')"), "denied",
                "import_arrow refuses INSERT into a table the caller cannot write")
    expect.text(verdict("SELECT pgcolumnar.export_parquet('victim'::regclass, '{parq_out}')"), "denied",
                "export_parquet refuses SELECT of a table the caller cannot read")
    expect.text(verdict("SELECT pgcolumnar.export_arrow('victim'::regclass, '{arrow_out}')"), "denied",
                "export_arrow refuses SELECT of a table the caller cannot read")
    expect.text(verdict("SELECT pgcolumnar.get_storage_id('victim'::regclass)"), "denied",
                "get_storage_id refuses a caller who cannot SELECT the relation")
    expect.text(verdict("SELECT pgcolumnar.parallel_copy('victim'::regclass, '{mine_text}', 2)"), "denied",
                "parallel_copy refuses INSERT into a table the caller cannot write")
    expect.text(verdict("SELECT pgcolumnar.parallel_export_parquet('victim'::regclass, '{pex_out}', 2)"), "denied",
                "parallel_export_parquet refuses SELECT of a table the caller cannot read")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM victim"), VICTIM_ROWS,
               "victim still holds exactly its own rows (no import landed)")
    expect.text("written" if os.path.exists(p["parq_out"]) or os.path.exists(p["arrow_out"]) else "none",
                "none", "no victim file was written by the export attempts")
    expect.text("written" if os.path.exists(p["pex_out"]) else "none", "none", "nor by the parallel export")


def test_the_owner_is_not_refused(pgc_cluster, iep, expect):
    """The same calls on the attacker's own table: the fix refuses the table, not the role."""
    schema, p = iep

    def verdict(sql):
        return _verdict(_as(pgc_cluster, ATTACKER, sql.format(**p), schema)[1], "mine")

    expect.text(verdict("SELECT pgcolumnar.export_arrow('mine'::regclass, '{mine2_arrow}')"), "allowed",
                "the owner can still export_arrow its own table")
    expect.text(verdict("SELECT pgcolumnar.export_parquet('mine'::regclass, '{mine2_parq}')"), "allowed",
                "the owner can still export_parquet its own table")
    expect.text(verdict("SELECT pgcolumnar.get_storage_id('mine'::regclass)"), "allowed",
                "the owner can still read its own storage id")
    expect.text(verdict("SELECT pgcolumnar.import_arrow('mine'::regclass, '{mine_arrow}')"), "allowed",
                "the owner can still import into its own table")


def test_the_owner_runs_the_parallel_twins(pgc_cluster, iep, expect):
    """Their answers, not only the absence of an error: the rows loaded, and an
    export that wrote something."""
    schema, p = iep
    loaded, err = _as(pgc_cluster, ATTACKER,
                      f"SELECT pgcolumnar.parallel_copy('mine'::regclass, '{p['mine_text']}', 2)", schema)
    expect.text(f"{loaded}|{err}", f"{MINE_ROWS}|None", "the owner can still parallel_copy into its own table")
    wrote, err = _as(pgc_cluster, ATTACKER,
                     f"SELECT pgcolumnar.parallel_export_parquet('mine'::regclass, '{p['mine_pex']}', 2) > 0",
                     schema)
    expect.text(f"{wrote}|{err}", "True|None", "the owner can still parallel_export_parquet its own table")

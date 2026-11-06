"""A NOLOGIN role's background workers connect and act as that role (#1585). The pytest
twin of `test/nologin_workers.sh`, every check by the shell's name.

parallel_export_parquet, parallel_copy and the parallel flush start background workers
for the calling role. A worker used to connect AS that role, which PostgreSQL 15 and 16
refuse for a role without LOGIN, so after SET ROLE to a NOLOGIN role the export and the
copy failed with no reason given and the flush fell back to serial. A worker now
connects as the authenticated user and then takes the caller's user id. Asserted, as a
NOLOGIN role holding both server-file roles: the export and the copy return every row;
a row trigger inside the copy worker records that role, not the authenticated user; the
flush encodes every column in a worker; and the server log holds no refused login for
the role beyond the one the premise provoked.

ON A PRIVATE DATABASE, with the role created for this module and dropped after.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_nologin_workers"
ROLE = "t_nl"
OTHER = "t_nl_other"
ROWS = 2000
REFUSED = rf'role "{ROLE}" is not permitted to log in'


def _refusals(cluster):
    """-> the lines in the server log refusing the role a login, as the shell's grep -c."""
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return sum(1 for line in text.splitlines() if re.search(REFUSED, line))


def _as_nl(dsn, sql, settings=()):
    """-> (the first column of the first row as text, or query_error() carrying the failure;
    the role current_user reported; the debug messages the statement raised)."""
    messages = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: messages.append(d.message_primary or ""))
            conn.execute(f"SET ROLE {ROLE}")
            who = conn.execute("SELECT current_user").fetchone()[0]
            for s in settings:
                conn.execute(s)
            cur = conn.execute(sql)
            row = cur.fetchone() if cur.description else None
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__), None, messages
    return ("" if row is None or row[0] is None else str(row[0])), who, messages


def _one(dsn, sql):
    """-> the first column of the first row as text, or query_error() carrying the failure,
    so a failed final reading reds its check by name rather than erroring the fixture."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    return "" if row is None or row[0] is None else str(row[0])


def _rollback_prepared(dsn):
    """Finish whatever a refused copy left prepared, so it cannot hold locks past its check."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for (gid,) in conn.execute("SELECT gid FROM pg_prepared_xacts").fetchall():
                conn.execute("ROLLBACK PREPARED %s", (gid,))
    except psycopg.Error:
        pass


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


@pytest.fixture(scope="module")
def nw(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"DROP ROLE IF EXISTS {ROLE}")
        a.execute(f"DROP ROLE IF EXISTS {OTHER}")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"CREATE ROLE {ROLE} NOSUPERUSER NOLOGIN")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        infile = server_dir / "nw_in.txt"
        infile.write_text("".join(f"{g}\t\n" for g in range(1, ROWS + 1)))
        infile.chmod(0o644)
        outdir = server_dir / "nw_dir"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {ROLE}")
            conn.execute(f"GRANT pg_read_server_files, pg_write_server_files TO {ROLE}")
            conn.execute("CREATE TABLE nw_src (a int, b text) USING pgcolumnar")
            conn.execute(f"INSERT INTO nw_src SELECT g, 'x' || g FROM generate_series(1, {ROWS}) g")
            # The trigger runs inside the copy worker, so it records the worker's current_user.
            conn.execute("CREATE TABLE nw_dst (a int, who text) USING pgcolumnar")
            conn.execute("CREATE FUNCTION nw_stamp() RETURNS trigger LANGUAGE plpgsql "
                         "AS $$BEGIN NEW.who := current_user; RETURN NEW; END$$")
            conn.execute("CREATE TRIGGER nw_stamp BEFORE INSERT ON nw_dst "
                         "FOR EACH ROW EXECUTE FUNCTION nw_stamp()")
            conn.execute("CREATE TABLE nw_wide (a int, b int, c int) USING pgcolumnar")
            conn.execute(f"GRANT SELECT ON nw_src TO {ROLE}")
            # A trigger that resets the role inside the copy worker, and one that switches to
            # another role the caller belongs to (#1612 review): neither may leave the worker
            # writing, or preparing, as anyone but the caller.
            conn.execute("CREATE TABLE nw_esc (a int, who text) USING pgcolumnar")
            conn.execute("CREATE FUNCTION nw_reset_stamp() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN "
                         "BEGIN PERFORM set_config('role', 'none', true); "
                         "EXCEPTION WHEN OTHERS THEN NULL; END; "
                         "NEW.who := current_user; RETURN NEW; END$$")
            conn.execute("CREATE TRIGGER nw_reset_stamp BEFORE INSERT ON nw_esc "
                         "FOR EACH ROW EXECUTE FUNCTION nw_reset_stamp()")
            conn.execute(f"CREATE ROLE {OTHER} NOSUPERUSER NOLOGIN")
            conn.execute(f"GRANT {OTHER} TO {ROLE}")
            conn.execute("CREATE TABLE nw_sw (a int, who text) USING pgcolumnar")
            conn.execute("CREATE FUNCTION nw_switch_stamp() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN "
                         f"PERFORM set_config('role', '{OTHER}', true); "
                         "NEW.who := current_user; RETURN NEW; END$$")
            conn.execute("CREATE TRIGGER nw_switch_stamp BEFORE INSERT ON nw_sw "
                         "FOR EACH ROW EXECUTE FUNCTION nw_switch_stamp()")
            conn.execute(f"GRANT SELECT, INSERT ON nw_sw TO {OTHER}")
            # The definer: a superuser session calls it, and inside it the caller is t_nl
            # under a security-restricted context, which must hold in the workers too.
            conn.execute("CREATE TABLE nw_def (a int, who text) USING pgcolumnar")
            conn.execute("CREATE TRIGGER nw_def_stamp BEFORE INSERT ON nw_def "
                         "FOR EACH ROW EXECUTE FUNCTION nw_reset_stamp()")
            conn.execute("CREATE FUNCTION nw_definer_copy(p text) RETURNS bigint LANGUAGE sql "
                         "SECURITY DEFINER AS $$SELECT pgcolumnar.parallel_copy('nw_def'::regclass, p, 2)$$")
            conn.execute(f"ALTER FUNCTION nw_definer_copy(text) OWNER TO {ROLE}")
            conn.execute(f"GRANT SELECT, INSERT ON nw_dst, nw_wide, nw_esc, nw_def, nw_sw TO {ROLE}")

        before = _refusals(pgc_cluster)
        try:
            with psycopg.connect(dsn.replace("user=postgres", f"user={ROLE}"), connect_timeout=5):
                login = "connected"
        except psycopg.Error as exc:
            login = str(exc)
        after_login = _refusals(pgc_cluster)

        _, took, _ = _as_nl(dsn, "SELECT 1")
        export, _, _ = _as_nl(dsn, f"SELECT pgcolumnar.parallel_export_parquet('nw_src'::regclass, '{outdir}', 2)")
        copy, _, _ = _as_nl(dsn, f"SELECT pgcolumnar.parallel_copy('nw_dst'::regclass, '{infile}', 2)")
        _, _, flush = _as_nl(dsn, f"INSERT INTO nw_wide SELECT g, g, g FROM generate_series(1, {ROWS}) g",
                             settings=("SET pgcolumnar.parallel_flush = on",
                                       "SET client_min_messages = debug1"))
        writers = _one(dsn, "SELECT string_agg(DISTINCT coalesce(who, '<null>'), ',') FROM nw_dst")
        flushed = _one(dsn, "SELECT count(*) FROM nw_wide")

        stamped = "count(*) || ':' || coalesce(string_agg(DISTINCT coalesce(who, '<null>'), ','), '')"
        _as_nl(dsn, f"SELECT pgcolumnar.parallel_copy('nw_esc'::regclass, '{infile}', 2)")
        esc = _one(dsn, f"SELECT {stamped} FROM nw_esc")
        esc_prepared = _one(dsn, "SELECT count(*) FROM pg_prepared_xacts")
        _rollback_prepared(dsn)
        switched, _, _ = _as_nl(dsn, f"SELECT pgcolumnar.parallel_copy('nw_sw'::regclass, '{infile}', 2)")
        sw_prepared = _one(dsn, "SELECT count(*) FROM pg_prepared_xacts")
        _rollback_prepared(dsn)
        _one(dsn, f"SELECT nw_definer_copy('{infile}')")
        definer = _one(dsn, f"SELECT {stamped} FROM nw_def")
        dispatch = [re.sub(r".* -> ", "", m) for m in flush if "parallel_flush dispatch:" in m]
        split = [re.search(r"parallel_flush: [0-9]+ of [0-9]+ columns", m) for m in flush]
        split = [m.group(0) for m in split if m]
        yield {
            "login refused": 1 if "is not permitted to log in" in login else 0,
            "log records it": (after_login - before) if isinstance(after_login, int) and isinstance(before, int) else after_login,
            "took": f"as:{took}",
            "export": export,
            "copy": copy,
            "writers": writers or "",
            "dispatch": dispatch[0] if dispatch else "",
            "split": split[0] if split else "",
            "flushed": _int(flushed),
            "esc": esc,
            "esc prepared": _int(esc_prepared),
            "switch refused": ("refused" if "no longer running as the role that called parallel_copy" in str(switched)
                               else f"not refused: {switched}"),
            "sw prepared": _int(sw_prepared),
            "definer": definer,
            "refused in all": (_refusals(pgc_cluster) - before) if isinstance(before, int) else before,
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            a.execute(f"DROP ROLE IF EXISTS {ROLE}")
            a.execute(f"DROP ROLE IF EXISTS {OTHER}")


def test_the_nologin_role_cannot_open_a_session(nw, expect):
    expect.num(nw["login refused"], 1, "premise: the NOLOGIN role cannot open a session")


def test_the_server_log_records_the_refused_login(nw, expect):
    expect.num(nw["log records it"], 1, "premise: the server log records the NOLOGIN role's refused login")


def test_set_role_to_the_nologin_role_takes(nw, expect):
    expect.text(nw["took"], f"as:{ROLE}", "premise: SET ROLE to the NOLOGIN role takes")


def test_a_nologin_roles_parallel_export_exports_every_row(nw, expect):
    expect.num(_int(nw["export"]), ROWS, "a NOLOGIN role's parallel_export_parquet exports every row")


def test_a_nologin_roles_parallel_copy_loads_every_row(nw, expect):
    expect.num(_int(nw["copy"]), ROWS, "a NOLOGIN role's parallel_copy loads every row")


def test_parallel_copys_workers_wrote_as_the_nologin_role(nw, expect):
    expect.text(nw["writers"], ROLE, "parallel_copy's workers wrote every row as the NOLOGIN role")


def test_the_flush_is_dispatched_to_workers(nw, expect):
    expect.text(nw["dispatch"], "parallel", "the flush is dispatched to workers")


def test_a_nologin_roles_parallel_flush_encodes_every_column_in_a_worker(nw, expect):
    expect.text(nw["split"], "parallel_flush: 3 of 3 columns",
                "a NOLOGIN role's parallel flush encodes every column in a worker")


def test_the_table_holds_every_row_it_flushed(nw, expect):
    expect.num(nw["flushed"], ROWS, "and the table holds every row it flushed")


def test_no_worker_was_refused_a_login(nw, expect):
    # The premise's own refused login is the one line expected, as in the shell.
    expect.num(nw["refused in all"], 1, "no worker was refused a login as the NOLOGIN role")


def test_a_role_resetting_copy_trigger_writes_as_the_nologin_role(nw, expect):
    expect.text(nw["esc"], f"{ROWS}:{ROLE}",
                "a copy trigger that resets the role still writes every row as the NOLOGIN role")


def test_the_role_resetting_copy_leaves_nothing_prepared(nw, expect):
    expect.num(nw["esc prepared"], 0, "and that copy leaves no prepared transaction behind")


def test_a_role_switching_copy_trigger_is_refused_before_prepare(nw, expect):
    expect.text(nw["switch refused"], "refused",
                "a copy trigger that switches to another role is refused before the worker prepares")


def test_the_refused_copy_leaves_nothing_prepared(nw, expect):
    expect.num(nw["sw prepared"], 0, "and the refused copy leaves no prepared transaction behind")


def test_a_definer_copy_keeps_its_restriction_in_the_workers(nw, expect):
    expect.text(nw["definer"], f"{ROWS}:{ROLE}",
                "a copy run by a SECURITY DEFINER function keeps the definer's restriction in its workers")

"""docs/configuration.md is held to the server, both ways (#1341 item 42), and the two
settings no test had set are set (items 57 and 83). The pytest twin of
`test/settings_reference.sh`, written independently: it reads the reference's
tables cell by cell, where the shell twin splits each row with awk.

THE CONDITION. Each row says, in its description, the one thing a reader must know
before setting it: "superuser", "only at server start", or "Reloadable". Absent all
three, the setting is one any user may change. The server's `pg_settings.context`
is the truth each is compared to: superuser, postmaster, sighup, user.

THE DEVELOPER HOOKS. A setting the server marks NOT_IN_SAMPLE (rollup_pause,
tier_fault_after, bulk_parallel_writer) is not for a reader: the reference may
document it, and then it is compared like any other, but need not.

REAL LOGINS, NOT `SET ROLE`: a failed SET ROLE leaves the session as the superuser,
and the refusal arm would then read as allowed for the wrong reason.
"""
import pathlib
import re

DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "configuration.md"
PLAIN = "t_sr_plain"


def _documented():
    """-> {name: (default, condition)} for every `pgcolumnar.*` row of the reference."""
    rows = {}
    for line in DOC.read_text().splitlines():
        if not line.startswith("| `pgcolumnar."):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        name = cells[0].strip("`")
        default = re.sub(r"\s*\(empty\)$", "", cells[2]).strip("`")
        if default == "''":
            default = ""
        if re.search(r"[Ss]uperuser", line):
            cond = "superuser"
        elif "only at server start" in line:
            cond = "postmaster"
        elif "Reloadable" in line:
            cond = "sighup"
        else:
            cond = "user"
        rows[name] = (default, cond)
    return rows


def _server(conn):
    """-> {name: (boot_val, context, public)} for every pgcolumnar.* setting."""
    with conn.cursor() as cur:
        cur.execute("SELECT name, boot_val, context, "
                    "NOT ('NOT_IN_SAMPLE' = ANY (pg_settings_get_flags(name))) "
                    "FROM pg_settings WHERE name LIKE 'pgcolumnar.%'")
        return {n: (b, c, p) for n, b, c, p in cur.fetchall()}


def _names(xs):
    return " ".join(sorted(xs)) or "none"


def test_the_reference_and_the_server_name_the_same_settings(pgc_conn, expect):
    doc, srv = _documented(), _server(pgc_conn)
    expect.at_least(len(doc), 30,
                    "the reference documents at least thirty settings, so the comparisons below read something")
    public = {n for n, (_, _, p) in srv.items() if p}
    expect.text(_names(public - doc.keys()), "none",
                "every setting the server defines is documented, the developer hooks aside")
    expect.text(_names(doc.keys() - srv.keys()), "none",
                "every documented setting exists on the server")


def test_each_documented_default_and_condition_is_the_servers(pgc_conn, expect):
    doc, srv = _documented(), _server(pgc_conn)
    both = doc.keys() & srv.keys()
    expect.text(_names(f"{n}: doc {doc[n][0]!r}, server {srv[n][0]!r}"
                       for n in both if doc[n][0] != srv[n][0]), "none",
                "each documented default is the server's")
    expect.text(_names(f"{n}: doc {doc[n][1]}, server {srv[n][1]}"
                       for n in both if doc[n][1] != srv[n][1]), "none",
                "each documented condition is the server's context for the setting")


def _as(cluster, role, sql):
    """-> None, or the error from `sql` on a fresh login as `role`."""
    import psycopg

    dsn = f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc
    return None


def test_only_a_superuser_sets_enable_end_truncation(pgc_cluster, pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{PLAIN}'")
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {PLAIN} NOSUPERUSER LOGIN")
    err = _as(pgc_cluster, PLAIN, "SET pgcolumnar.enable_end_truncation = on")
    if err is None:
        expect.text("allowed", "42501",
                    "a role that is not a superuser cannot set enable_end_truncation (42501)")
    else:
        expect.sqlstate(err, "42501",
                        "a role that is not a superuser cannot set enable_end_truncation (42501)")
    with pgc_conn.cursor() as cur:
        cur.execute("SET pgcolumnar.enable_end_truncation = on")
        cur.execute("SHOW pgcolumnar.enable_end_truncation")
        expect.text(cur.fetchone()[0], "on", "and a superuser can")


def test_compression_level_reaches_the_zstd_codec(pgc_conn, expect):
    """The same text at level 1 and level 19: the higher level stores less."""
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT 'zstd' = ANY (enumvals) FROM pg_settings "
                    "WHERE name = 'pgcolumnar.compression'")
        if not cur.fetchone()[0]:
            expect.cannot_run("MISSING_DEPENDENCY", "no zstd codec",
                              name="this build has no zstd codec")
            return
        size = {}
        for lvl in (1, 19):
            cur.execute("SET pgcolumnar.compression = zstd")
            cur.execute(f"SET pgcolumnar.compression_level = {lvl}")
            cur.execute(f"CREATE TABLE sr_l{lvl} (t text) USING pgcolumnar")
            cur.execute(f"INSERT INTO sr_l{lvl} SELECT string_agg(md5((g * 7 + k)::text), ' ') "
                        "FROM generate_series(1, 20000) g, generate_series(1, 3) k GROUP BY g")
            cur.execute(f"SELECT pg_relation_size('sr_l{lvl}')")
            size[lvl] = cur.fetchone()[0]
    expect.text("less" if size[19] < size[1] else f"{size[19]} vs {size[1]}", "less",
                "compression_level reaches the zstd codec: level 19 stores less than level 1")

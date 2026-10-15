"""Userinfo in a URL is refused on every entry point (#706, #995, #997): the pytest
twin of `test/objstore_userinfo.sh` (#1341). Every check carries the shell's name.

The read path, the write path, the s3 and gs bucket authority, and an endpoint a
foreign server configures each refuse `user@host` with the parse guard's 22023.
22023 comes only from the parse guards here and 42501 only from the allow-list, so
the SQLSTATE says WHICH layer refused, and the message pins which 22023 guard.

THE CONTROLS ARE WHAT MAKE THE ARMS MEAN ANYTHING. With no endpoint configured every
s3 URL is refused 28000, the clean one included, so a userinfo refusal must be told
apart from an unreachable object store: a clean URL has to reach the endpoint demand,
and say so. 28000 has three causes in os_resolve_s3 (no endpoint, no credential, the
allow-list's authorization), so each control names the one it expects.

No object-store server is needed: every refusal here fires before any network I/O.
The allow-list is set per session, as test_objstore_endpoint_userinfo.py does; it is
superuser-settable. Nothing here reads anything under `test/*.sh`.
"""
import pytest

ALLOWED = "127.0.0.1"
URL = "http://u:p@127.0.0.1:1/x.parquet"
S3UI = "s3://u:p@mybucket/x.parquet"


def _err(conn, sql):
    """-> (sqlstate, primary message) of the error `sql` raises, or ('', '')."""
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                cur.fetchall()
        return "", ""
    except psycopg.Error as e:
        return e.sqlstate or "?", e.diag.message_primary or ""


def _has(text, word):
    return 1 if word in text else 0


@pytest.fixture
def ui(pgc_conn):
    with pgc_conn.cursor() as cur:
        cur.execute(f"SET pgcolumnar.objstore_allowed_endpoints = '{ALLOWED}'")
        cur.execute("CREATE TABLE ex (id int, v text) USING pgcolumnar")
        cur.execute("INSERT INTO ex SELECT g, 'v'||g FROM generate_series(1,100) g")
    return pgc_conn


def test_userinfo_in_a_url_is_refused_by_the_parse_guard(ui, expect):
    """The http read and write paths, and the s3 and gs bucket authority."""
    rp = f"SELECT * FROM pgcolumnar.read_parquet('{URL}') AS t(v int)"
    state, msg = _err(ui, rp)
    expect.text(state, "22023", "read_parquet refuses userinfo (22023, the parse guard)")
    expect.num(_has(msg, "userinfo"), 1, "and the read_parquet message names userinfo")

    ep = f"SELECT pgcolumnar.export_parquet('ex', '{URL}')"
    state, msg = _err(ui, ep)
    expect.text(state, "22023", "export_parquet refuses userinfo (22023, not an allow-list 42501)")
    expect.num(_has(msg, "userinfo"), 1, "and the export_parquet message names userinfo")
    expect.text(_err(ui, "SELECT pgcolumnar.export_arrow('ex', 'http://u@127.0.0.1:1/x.arrow')")[0], "22023",
                "export_arrow refuses userinfo through the same handle (22023)")

    s3r = f"SELECT * FROM pgcolumnar.read_parquet('{S3UI}') AS t(v int)"
    state, msg = _err(ui, s3r)
    expect.text(state, "22023", "read_parquet refuses userinfo in an s3:// bucket authority (22023)")
    expect.num(_has(msg, "userinfo"), 1, "and the s3 read message names userinfo")
    expect.text(_err(ui, "SELECT * FROM pgcolumnar.read_parquet('gs://u@mybucket/x.parquet') AS t(v int)")[0],
                "22023", "a gs:// bucket authority is refused by the same guard")
    s3w = f"SELECT pgcolumnar.export_parquet('ex', '{S3UI}')"
    state, msg = _err(ui, s3w)
    expect.text(state, "22023",
                "export_parquet refuses it on the WRITE path too, where acceptance writes to a bucket nobody named")
    expect.num(_has(msg, "userinfo"), 1, "and the s3 write message names userinfo")


def test_a_clean_url_reaches_the_refusal_it_should(ui, expect):
    """The controls: a clean s3 URL demands an endpoint, gs demands a credential, a
    malformed URL is the bucket/key refusal, '@' in a KEY is not userinfo, and a clean
    URL to a non-allowed endpoint is still the allow-list's 42501."""
    clean = "SELECT * FROM pgcolumnar.read_parquet('s3://mybucket/x.parquet') AS t(v int)"
    state, msg = _err(ui, clean)
    expect.text(state, "28000", "control: a clean s3:// URL is not refused as userinfo, it demands an endpoint")
    expect.num(_has(msg, "AWS_ENDPOINT_URL"), 1,
               "control: and that refusal NAMES AWS_ENDPOINT_URL, pinning which 28000 fired")
    expect.num(_has(msg, "userinfo"), 0,
               "control: and it does not name userinfo, so the new guard did not fire on it")
    _, msg = _err(ui, "SELECT * FROM pgcolumnar.read_parquet('gs://mybucket/x.parquet') AS t(v int)")
    expect.num(_has(msg, "AWS_ACCESS_KEY_ID"), 1,
               "control: the gs:// refusal names a CREDENTIAL, not the endpoint -- same 28000, other cause")
    _, msg = _err(ui, "SELECT * FROM pgcolumnar.read_parquet('s3://nokey') AS t(v int)")
    expect.num(_has(msg, "userinfo"), 0, "control: a malformed s3:// URL is still the bucket/key refusal, naming no userinfo")
    state, msg = _err(ui, "SELECT * FROM pgcolumnar.read_parquet('s3://mybucket/my@file.parquet') AS t(v int)")
    expect.num(_has(msg, "userinfo"), 0, "control: an '@' in the KEY is not userinfo, and is not refused as it")
    expect.text(state, "28000", "control: and such a URL still reaches the endpoint demand, so it was not refused earlier")
    expect.text(_err(ui, "SELECT pgcolumnar.export_parquet('ex', 'http://127.0.0.2:1/x.parquet')")[0], "42501",
                "a clean URL to a non-allowed endpoint is still the allow-list's 42501")
    with ui.cursor() as cur:
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1, "backend alive")


def test_userinfo_in_a_configured_endpoint_is_refused(ui, expect):
    """The endpoint a foreign server configures, which the bucket guard cannot see.
    Two shapes: with a colon in the userinfo, and without one, which used to keep a
    valid port and reach only the allow-list."""
    with ui.cursor() as cur:
        for srv, endpoint in (("s3ui_py", "http://u:p@127.0.0.1:1"), ("s3ui2_py", "http://user@127.0.0.1:1"),
                              ("s3ok_py", "http://127.0.0.1:1")):
            cur.execute(f"CREATE SERVER IF NOT EXISTS {srv} FOREIGN DATA WRAPPER pgcolumnar_parquet"
                        f" OPTIONS (endpoint '{endpoint}')")
        cur.execute("CREATE FOREIGN TABLE ft_ui (id int) SERVER s3ui_py OPTIONS (path 's3://mybucket/x.parquet')")
        cur.execute("CREATE FOREIGN TABLE ft_ui2 (id int) SERVER s3ui2_py OPTIONS (path 's3://mybucket/x.parquet')")
        cur.execute("CREATE FOREIGN TABLE ft_ok (id int) SERVER s3ok_py OPTIONS (path 's3://mybucket/x.parquet')")
    state, msg = _err(ui, "SELECT * FROM ft_ui")
    expect.text(state, "22023", "a userinfo endpoint is refused by the parse guard (22023)")
    expect.num(_has(msg, "userinfo"), 1, "and the message names userinfo")
    expect.num(_has(msg, "endpoint"), 1, "and it names the ENDPOINT, not the s3:// URL, which carries none")
    state, msg = _err(ui, "SELECT * FROM ft_ui2")
    expect.text(state, "22023", "the user@host shape is refused by the same guard (22023, not an allow-list 42501)")
    expect.num(_has(msg, "userinfo"), 1, "and its message names userinfo too")
    state, msg = _err(ui, "SELECT * FROM ft_ok")
    expect.num(_has(msg, "userinfo"), 0, "control: a clean endpoint is not refused as userinfo")
    expect.text("yes" if state != "22023" else "no", "yes",
                "control: and it gets past the parse to a connection failure, not 22023")

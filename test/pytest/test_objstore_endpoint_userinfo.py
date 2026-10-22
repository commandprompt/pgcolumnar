"""Userinfo in an object-store ENDPOINT is refused, and refused before anything else
the endpoint is missing (#995).

`s3://u:p@bucket/key` — userinfo in the URL the CALLER writes — was closed by #997.
This is the other half: userinfo in the endpoint the OPERATOR configures, which the
bucket guard cannot see because it is not in the URL at all.

WHY IT IS A GUARD AND NOT A MESSAGE CHANGE. Both shapes failed closed already, and by
two different accidents:

    http://u:p@host:30829   ->  host "u",          port 0       <- refused, wrong reason
    http://user@host:30829  ->  host "user@host",  port 30829   <- port VALID

The first has a colon inside the userinfo, so the authority split lands there and the
port becomes `atoi("p@host:30829")` = 0. The second has no such colon: the real port
survives, the `@` rides along in the host, and the invalid-port refusal never fires.
What caught it instead was the allow-list, whose hint then told the operator

    ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = 'user@host'

— a diagnostic inviting them to widen a security boundary to accommodate a parse bug.

THIS FILE IS NOT A PORT of `objstore_userinfo.sh` and does not pair with it. It
asserts the same properties through the python harness, independently: nothing here
sources, invokes or reads anything under `test/*.sh`, and it behaves identically if
that suite is deleted.
"""
import pytest

ALLOWED = "127.0.0.1"
URL = "s3://mybucket/x.parquet"

# endpoint, alias, and what the shape is called. The two userinfo shapes are separate
# rows because they used to fail by DIFFERENT mechanisms, and a single row would not
# have told them apart.
ENDPOINTS = [
    ("http://u:p@127.0.0.1:1", "ui_pass",
     "user:password@ — the colon inside the userinfo used to eat the port"),
    ("http://user@127.0.0.1:1", "ui_bare",
     "user@ — no colon, so the port stayed valid and only the allow-list refused it"),
]


@pytest.fixture(scope="module")
def endpoints(pgc_cluster):
    """-> (connection, {alias: foreign table}) over servers with the endpoints above.

    A FOREIGN SERVER rather than `AWS_ENDPOINT_URL`, because the environment variable
    is read from the POSTMASTER's environment and would need a restart. Both reach the
    same `ep` in `os_resolve_s3`, so the server option exercises the guard without one.
    """
    import psycopg

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # SUSET, and this connection is a superuser, so no restart is needed. Set at
        # all because an EMPTY allow-list refuses every endpoint, which would make a
        # clean control indistinguishable from a refused one.
        cur.execute(f"SET pgcolumnar.objstore_allowed_endpoints = '{ALLOWED}'")
        tables = {}
        for endpoint, alias, _why in ENDPOINTS + [("http://127.0.0.1:1", "clean", "")]:
            cur.execute(f"CREATE SERVER srv_{alias} FOREIGN DATA WRAPPER "
                        f"pgcolumnar_parquet OPTIONS (endpoint '{endpoint}')")
            cur.execute(f"CREATE FOREIGN TABLE ft_{alias} (id int) SERVER srv_{alias} "
                        f"OPTIONS (path '{URL}')")
            tables[alias] = f"ft_{alias}"
    yield conn, tables
    conn.close()


def _error(conn, sql):
    """-> (the exception, its message) for a statement expected to fail.

    THE EXCEPTION ITSELF, not its sqlstate string: `expect.sqlstate` refuses a bare
    string, because a string carries no evidence that the comparison is about an error
    at all. It caught that here on the first run.
    """
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return None, ""
    except psycopg.Error as exc:
        return exc, str(exc)


@pytest.mark.parametrize("endpoint,alias,name", [
    (e, a, f"a {w.split(' — ')[0]} endpoint is refused by the parse guard")
    for e, a, w in ENDPOINTS
])
def test_a_userinfo_endpoint_is_refused(endpoints, expect, endpoint, alias, name):
    conn, tables = endpoints
    exc, message = _error(conn, f"SELECT * FROM {tables[alias]}")
    expect.num(1 if exc is not None else 0, 1,
               f"premise: {endpoint} raises at all, so the state below is an error's")
    expect.sqlstate(exc, "22023", name)
    expect.num(1 if "userinfo" in message else 0, 1,
               f"and the message for {endpoint} names userinfo")
    # The offending string is the ENDPOINT. Saying `userinfo in "s3://bucket/key"`
    # would name a URL that carries none, and send the reader to the wrong place.
    # THE QUOTED STRING, not the word (#1339). "endpoint" is in the message's own
    # text and in its hint, so a message that quoted the s3:// URL instead (`ep`
    # swapped for `url` in the errmsg) still contained it and this passed.
    expect.text(
        "endpoint" if f'"{endpoint}"' in message and URL not in message
        else f"not the endpoint: {message.splitlines()[0] if message else 'no message'}",
        "endpoint",
        f"and it names the endpoint rather than the s3:// URL, for {endpoint}")


def test_the_guard_fires_without_a_region_configured(endpoints, expect):
    """THE PLACEMENT, which is the part a message change would not have given.

    The guard sits before the scheme and region demands rather than at the authority
    parse eighty lines later. Placed there it would be unreachable whenever no region
    is configured -- an operator with a userinfo endpoint and no `AWS_REGION` would be
    told about the region, and an arm for it would need a region set for no reason
    connected to what it tests.

    MEASURED: every endpoint arm returned `requires a region option` until the guard
    moved. This cluster configures no region, so a `userinfo` message here IS the
    reachability claim.

    AND THAT "NO REGION" IS ASSERTED, NOT ASSUMED (#1339). Nothing here set a region,
    but nothing showed none was set either: with `AWS_REGION` in the server's
    environment and the guard moved back below the region demand, every arm in this
    file passed. The clean endpoint shows the cluster's state: with no region
    configured, it is refused for want of one.
    """
    conn, tables = endpoints
    _exc, clean = _error(conn, f"SELECT * FROM {tables['clean']}")
    expect.text(
        "no region" if "requires a region option" in clean
        else f"a region is configured: {clean.splitlines()[0] if clean else 'no error'}",
        "no region",
        "no region is configured: a clean endpoint here is refused for want of one")
    _exc, message = _error(conn, f"SELECT * FROM {tables['ui_pass']}")
    expect.num(1 if "region" in message else 0, 0,
               "no region is configured, and the refusal is NOT about the region")
    expect.num(1 if "userinfo" in message else 0, 1,
               "so the userinfo guard is reached before the region demand")


def test_a_clean_endpoint_is_not_refused_as_userinfo(endpoints, expect):
    """THE CONTROL, without which the arms above are worth nothing.

    #995's first probe proved exactly this much and no more: with no credentials every
    s3 URL failed, the clean one included, so a userinfo refusal was indistinguishable
    from an object store that could not be reached at all. A clean endpoint must get
    PAST the guard and fail for some other reason.
    """
    conn, tables = endpoints
    exc, message = _error(conn, f"SELECT * FROM {tables['clean']}")
    expect.num(1 if "userinfo" in message else 0, 0,
               "a clean endpoint is not refused as userinfo")
    state = getattr(exc, "sqlstate", "noerror")
    expect.text("refused" if state == "22023" else "reached further", "reached further",
                "and it is not the parse guard's 22023 at all, so the guard is "
                "discriminating rather than refusing everything")


def test_an_at_sign_in_the_object_key_is_not_userinfo(endpoints, expect):
    """AND THE OTHER DIRECTION. `@` is legal in an S3 key, and the guard scans the
    ENDPOINT, so a key carrying one must be untouched. Without this the arms above
    would also pass on a guard that refused every `@` anywhere.
    """
    conn, tables = endpoints
    with conn.cursor() as cur:
        cur.execute("CREATE FOREIGN TABLE ft_atkey (id int) SERVER srv_clean "
                    "OPTIONS (path 's3://mybucket/my@file.parquet')")
    _exc, message = _error(conn, "SELECT * FROM ft_atkey")
    expect.num(1 if "userinfo" in message else 0, 0,
               "an '@' in the object KEY is not userinfo and is not refused as it")

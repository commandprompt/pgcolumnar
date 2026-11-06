"""An under-filled DICT chunk is refused cleanly, and no backend dies (#1341). The pytest
twin of `test/native_dict_underfill.sh`, every check by the shell's name.

The test inflates a DICT chunk's value_raw_length in its encoding descriptor, so the
codes decode to fewer bytes than declared. Without the fill check the raw buffer's
tail is left uninitialized and a varlena length is read out of it. The read must raise
XX001, and the crash is read from the server log, since a fresh connection succeeds
again once crash recovery has finished.

The post-codec encoding choice (#1132) is turned off for this session, as the shell
does for its database, so the fixture is DICT-encoded whether or not DICT beats zstd.
"""
import psycopg

from pgc_vacuity import query_error


def _crashes(pgc_cluster):
    """-> how many backends the server log says died, or a failed reading when the
    log can't be read: a blind count must not read as "no crashes"."""
    try:
        return (pgc_cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _since(pgc_cluster, before):
    """-> backends died since `before`, or the failed reading when either count is
    one: a blind count must not read as "no crashes"."""
    now = _crashes(pgc_cluster)
    for v in (before, now):
        if not isinstance(v, int):
            return v
    return now - before


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


RAW = ("(get_byte(encoding_descriptor, 11) + (get_byte(encoding_descriptor, 12) << 8) + "
       "(get_byte(encoding_descriptor, 13) << 16) + (get_byte(encoding_descriptor, 14) << 24) + 64)")


def test_an_under_filled_dict_chunk_is_refused(pgc_conn, pgc_cluster, expect):
    conn = pgc_conn
    with conn.cursor() as cur:
        cur.execute("SET pgcolumnar.enable_post_codec_encoding_choice = off")
        cur.execute("CREATE TABLE dt (id int, v text) USING pgcolumnar")
        # low-cardinality short text: a DICT-encoded varlena column, one small chunk
        cur.execute("INSERT INTO dt SELECT g, (ARRAY['aa', 'bb', 'cc', 'dd'])[1 + (g % 4)] "
                    "FROM generate_series(1, 300) g")
    sid = _one(conn, "SELECT pgcolumnar.get_storage_id('dt')")
    # column_index is 0-based: v is attribute 2, index 1; byte 6 is vector 0's encoding type
    expect.num(_one(conn, f"SELECT get_byte(encoding_descriptor, 6) FROM pgcolumnar.column_chunk "
                          f"WHERE storage_id = {sid} AND column_index = 1 LIMIT 1"), 6,
               "the text column is DICT-encoded (precondition)")

    crashes = _crashes(pgc_cluster)
    # rawLen is the uint32 LE at descriptor offset 11: raise it by 64
    rewrote = _one(conn, f"""WITH u AS (UPDATE pgcolumnar.column_chunk c SET encoding_descriptor =
        set_byte(set_byte(set_byte(set_byte(encoding_descriptor,
            11, {RAW} & 255), 12, ({RAW} >> 8) & 255), 13, ({RAW} >> 16) & 255), 14, ({RAW} >> 24) & 255)
        WHERE c.storage_id = {sid} AND c.column_index = 1 RETURNING 1) SELECT count(*) FROM u""")
    expect.num(rewrote, 1, "premise: the inflation rewrote the DICT chunk's descriptor")

    try:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute(f"SET search_path TO {conn.execute('SELECT current_schema()').fetchone()[0]}")
            c.execute("SELECT v FROM dt").fetchall()
        rejected = "no error"
    except psycopg.Error as exc:
        rejected = exc.sqlstate or f"no SQLSTATE ({str(exc).splitlines()[0]})"
    expect.text(rejected, "XX001", "under-filled DICT chunk is rejected with a clean error")

    try:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            alive = c.execute("SELECT 1").fetchone()[0] == 1
    except psycopg.Error:
        alive = False
    died = _since(pgc_cluster, crashes)
    expect.text(f"{'1' if alive else ''}|{died} backend crashes" if isinstance(died, int) else died, "1|0 backend crashes",
                "backend survives the under-filled DICT chunk")

"""set_options and reset_options reach the same verdict on every kind of relation, and
both accept a columnar materialized view (#1265, #1341). Part of the pytest twin of
`test/phase5.sh`: these arms only, by the shell's names, and the pair is declared
INCOMPLETE.

reset_options had no guard at all, so it reported success for a heap table and a
partitioned parent while set_options refused the same relation. The property is
parity, not a relkind list. Parity alone is satisfied by both refusing, so the
settled verdict on a matview (accept) and on a partitioned parent (refuse) are
asserted by name too, and an ordinary columnar table is the control both must accept.
"""
import psycopg


def _verdict(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "accepted"
    except psycopg.Error:
        return "refused"


def _set(conn, rel):
    return _verdict(conn, f"SELECT pgcolumnar.set_options('{rel}', chunk_group_row_limit => 1000)")


def _reset(conn, rel):
    return _verdict(conn, f"SELECT pgcolumnar.reset_options('{rel}', chunk_group_row_limit => true)")


def _agree(conn, rel):
    s, r = _set(conn, rel), _reset(conn, rel)
    return "agree" if s == r else f"set={s} reset={r}"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_set_and_reset_options_agree(pgc_conn, expect):
    conn = pgc_conn
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE o_par_col (a int) USING pgcolumnar")
        cur.execute("CREATE TABLE o_par_heap (a int)")
        cur.execute("CREATE TABLE o_par_part (a int) PARTITION BY RANGE (a)")
        cur.execute("CREATE TABLE o_par_mvsrc (a int)")
        cur.execute("INSERT INTO o_par_mvsrc VALUES (1), (2)")
        cur.execute("CREATE MATERIALIZED VIEW o_par_mv USING pgcolumnar AS SELECT a FROM o_par_mvsrc")

    expect.text(_one(conn, "SELECT c.relkind::text || am.amname::text FROM pg_class c JOIN pg_am am "
                           "ON am.oid = c.relam WHERE c.oid = 'o_par_col'::regclass"), "rpgcolumnar",
                "premise: the columnar fixture is relkind r on the columnar access method")
    expect.text(_one(conn, "SELECT c.relkind::text || (am.amname <> 'pgcolumnar')::text FROM pg_class c "
                           "JOIN pg_am am ON am.oid = c.relam WHERE c.oid = 'o_par_heap'::regclass"), "rtrue",
                "premise: the heap fixture is relkind r and is NOT columnar")
    expect.text(_one(conn, "SELECT c.relkind::text || c.relfilenode::text FROM pg_class c "
                           "WHERE c.oid = 'o_par_part'::regclass"), "p0",
                "premise: the partitioned fixture is relkind p with no storage")
    expect.text(_one(conn, "SELECT c.relkind::text || am.amname::text FROM pg_class c JOIN pg_am am "
                           "ON am.oid = c.relam WHERE c.oid = 'o_par_mv'::regclass"), "mpgcolumnar",
                "premise: the matview fixture is relkind m on the columnar access method")

    # Without it "agree" is satisfied by a build where both refuse everything.
    expect.text(f"{_set(conn, 'o_par_col')}/{_reset(conn, 'o_par_col')}", "accepted/accepted",
                "control: both entry points accept an ordinary columnar table")

    expect.text(_agree(conn, "o_par_col"), "agree", "set_options and reset_options agree about a columnar table")
    expect.text(_agree(conn, "o_par_heap"), "agree", "set_options and reset_options agree about a heap table")
    expect.text(_agree(conn, "o_par_part"), "agree",
                "set_options and reset_options agree about a partitioned parent")
    expect.text(_agree(conn, "o_par_mv"), "agree", "set_options and reset_options agree about a columnar matview")

    expect.text(f"{_set(conn, 'o_par_mv')}/{_reset(conn, 'o_par_mv')}", "accepted/accepted",
                "both entry points accept a columnar matview (#1265, settled)")
    # Widening to matviews is not widening to everything without storage.
    expect.text(f"{_set(conn, 'o_par_part')}/{_reset(conn, 'o_par_part')}", "refused/refused",
                "control: a partitioned parent is still refused by both")

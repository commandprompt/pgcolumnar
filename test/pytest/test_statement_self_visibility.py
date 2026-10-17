"""A statement does not see the rows it writes, even after it has flushed some of
them into row groups part-way through (#1341). The pytest twin of
`test/statement_self_visibility.sh`, every check by the shell's name.

A scan reads its row-group list once, when it starts, so groups its own statement
flushes later are not in it. A reader that picked them up would make
INSERT INTO t SELECT FROM t feed on its own output, and UPDATE t reach a row twice.

Each shape runs on a columnar table and on heap, and heap is the answer. The
columnar table writes row groups of 1000 rows, so every statement crosses several
flushes. Every statement runs under a statement_timeout, because a scan that fed on
its own output would otherwise never end. Both tables run every statement, so a
columnar failure cannot leave heap untouched and the comparison vacuous.
"""
import psycopg

from pgc_vacuity import query_error

AMS = ("pgcolumnar", "heap")


def _session(cur):
    cur.execute("SET pgcolumnar.stripe_row_limit = 1000")
    cur.execute("SET statement_timeout = '60s'")


def _run(conn, sql):
    """Run `sql` (with %t for the table) on both tables -> 'ok' or the errors."""
    errs = []
    for am in AMS:
        try:
            with conn.cursor() as cur:
                _session(cur)
                cur.execute(sql.replace("%t", f"sv_{am}"))
        except psycopg.Error as exc:
            errs.append(f"{am}: {str(exc).splitlines()[0]}")
            conn.execute("ROLLBACK")  # a failed BEGIN ... COMMIT leaves the session in it
    return " ".join(errs) or "ok"


def _answer(conn, sql, am):
    """The row the query returns, or its error's first line if it failed."""
    try:
        with conn.cursor() as cur:
            _session(cur)
            cur.execute(sql.replace("%t", f"sv_{am}"))
            return cur.fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _same(conn, sql):
    c, h = (_answer(conn, sql, am) for am in AMS)
    return "same" if c == h else f"columnar={c} heap={h}"


def _groups(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pgcolumnar.row_group "
                    "WHERE storage_id = pgcolumnar.get_storage_id('sv_pgcolumnar')")
        return cur.fetchone()[0]


def _fresh(conn):
    """Both tables made again, holding rows 1..5000. Every shape starts here, so a
    shape that goes wrong cannot fail the ones after it."""
    with conn.cursor() as cur:
        _session(cur)
        for am in AMS:
            cur.execute(f"DROP TABLE IF EXISTS sv_{am}")
            cur.execute(f"CREATE TABLE sv_{am} (id int, v int) USING {am}")
            cur.execute(f"INSERT INTO sv_{am} SELECT g, 0 FROM generate_series(1, 5000) g")


def test_a_statement_does_not_see_its_own_writes(pgc_conn, expect):
    _fresh(pgc_conn)
    before = _groups(pgc_conn)

    expect.text(_run(pgc_conn, "INSERT INTO %t SELECT id + 5000, v FROM %t"), "ok",
                "INSERT INTO t SELECT FROM t runs to completion")
    after = _groups(pgc_conn)
    expect.text("yes" if after - before >= 2 else f"no ({before} groups before, {after} after)", "yes",
                "the INSERT flushed row groups while its scan was still reading")
    expect.text(_same(pgc_conn, "SELECT count(*), count(DISTINCT id), max(id) FROM %t"), "same",
                "and it read only the rows that were there when it started, as heap does")

    _fresh(pgc_conn)
    expect.text(_run(pgc_conn, "UPDATE %t SET v = v + 1"), "ok",
                "UPDATE of every row runs to completion")
    expect.text(_same(pgc_conn, "SELECT count(*), min(v), max(v) FROM %t"), "same",
                "and it changed each row exactly once, as heap does")

    _fresh(pgc_conn)
    expect.text(_run(pgc_conn, "BEGIN; INSERT INTO %t SELECT g, 0 FROM generate_series(20001, 23000) g; "
                               "UPDATE %t SET v = v + 1; INSERT INTO %t SELECT id + 100000, v FROM %t; COMMIT"),
                "ok",
                "in one transaction, an INSERT, an UPDATE of every row and an INSERT SELECT run to completion")
    expect.text(_same(pgc_conn, "SELECT count(*), count(DISTINCT id), min(v), max(v), sum(v) FROM %t"), "same",
                "and each statement saw the earlier ones but not itself, as heap does")

    # No WHERE on the scan: every row the INSERT writes would pass it, so a scan
    # that fed on its own output would meet those rows.
    _fresh(pgc_conn)
    expect.text(_same(pgc_conn, "WITH w AS (INSERT INTO %t SELECT id + 1000000, v FROM %t "
                                "RETURNING 1) SELECT count(*) FROM w"), "same",
                "an INSERT in a WITH clause reading its own table returns the rows it inserted, as heap does")
    expect.text(_same(pgc_conn, "SELECT count(*) FROM %t"), "same",
                "and the table holds them once, as heap does")

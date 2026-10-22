"""Deferred column decode on the index fetch path (#157). The pytest twin of
`test/native_lazy_slot.sh`, every check by the shell's name (#1341).

An index fetch hands the executor a slot carrying the row's address, and the columns
are decoded when slot_getsomeattrs asks for them, the smallest prefix it needs.
Asserted, through an index scan with the custom scan off: values match a heap table for
every column position and for count(*); a row still in this transaction's write buffer
reads correctly; the paths that need a whole row (sort, CREATE TABLE AS, a unique
check) still get one; and a wide table is not in a different class from a narrow one
for the same one-column fetches.

THE HEAP MIRROR IS WRITTEN FROM THE GENERATOR, as the shell's is: a copy read through
the columnar reader agreed with it on a value the writer stored wrong.

READINGS ARE VALUES: a failed query is a query_error(), which every comparison refuses
by name, so two failed readings never agree; a timing whose statement failed is no
reading rather than a fast one.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_lazy_slot"
ROWS = 20000
NCOLS = 40
CLIFF_ROWS = 30000
FORCE = ("SET enable_seqscan = off", "SET enable_bitmapscan = off",
         "SET pgcolumnar.enable_custom_scan = off", "SET max_parallel_workers_per_gather = 0")


def _message(exc):
    diag = getattr(exc, "diag", None)
    primary = diag.message_primary if diag is not None else None
    return primary or (str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql, force=True):
    """-> the first column of the last row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            if force:
                for s in FORCE:
                    conn.execute(s)
            cur = conn.execute(sql)
            rows = cur.fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if not rows or rows[-1][0] is None:
        return query_error(f"no value from {sql}")
    return str(rows[-1][0])


def _lines(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in FORCE:
                conn.execute(s)
            return [r[0] for r in conn.execute(sql).fetchall()]
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _setup(dsn, statements):
    """-> None, or the failure as a query_error(), so a setup that fails reds the
    readings that depend on it by name instead of erroring the module."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in statements:
                conn.execute(s)
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return None


def _ms(dsn, table):
    """-> milliseconds for the shell's 2000 one-column fetches, or a query_error() when
    the statement failed: a statement that errors at once is fast, and two of them
    must not pass the ratio."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in FORCE:
                conn.execute(s)
            t0 = time.perf_counter()
            conn.execute(f"SELECT count(c1) FROM {table} WHERE id <= 2000").fetchall()
            t1 = time.perf_counter()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return int((t1 - t0) * 1000)


def _build_wide(table, ncols):
    cols = "".join(f", c{i} text" for i in range(1, ncols + 1))
    vals = "".join(", repeat('x', 40)" for _ in range(ncols))
    return [f"DROP TABLE IF EXISTS {table}",
            f"CREATE TABLE {table} (id int{cols}) USING pgcolumnar",
            f"INSERT INTO {table} SELECT g{vals} FROM generate_series(1, {CLIFF_ROWS}) g",
            f"CREATE INDEX {table}_id ON {table} (id)"]


@pytest.fixture(scope="module")
def lz(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        cols = "".join(f", c{i} bigint" for i in range(1, NCOLS + 1))
        sel = "".join(f", g * {i}" for i in range(1, NCOLS + 1))
        gen = f"SELECT g{sel}, 'v' || g FROM generate_series(1, {ROWS}) g"
        r = {}
        err = _setup(dsn, [
            "CREATE EXTENSION IF NOT EXISTS pgcolumnar",
            f"CREATE TABLE lz_c (id int{cols}, txt text) USING pgcolumnar",
            f"INSERT INTO lz_c {gen}",
            "CREATE INDEX lz_c_id ON lz_c (id)",
            "CREATE TABLE lz_h (LIKE lz_c)",
            # from the generator, not from lz_c (see the module docstring)
            f"INSERT INTO lz_h {gen}",
            "CREATE INDEX lz_h_id ON lz_h (id)"])

        plan = _lines(dsn, "EXPLAIN (COSTS off) SELECT c1 FROM lz_c WHERE id = 5")
        r["index scan"] = err or (plan if isinstance(plan, str)
                                  else str(sum(1 for line in plan if "Index Scan" in line)))

        bad = []
        for expr in ("c1", "c2", "c20", f"c{NCOLS}", "txt", f"c1 + c{NCOLS}", "id"):
            cv = _value(dsn, f"SELECT {expr} FROM lz_c WHERE id = 7777")
            hv = _value(dsn, f"SELECT {expr} FROM lz_h WHERE id = 7777")
            # failures are query_error() values, unique per failure, so two never agree
            if cv != hv:
                bad.append(f"[{expr}: {cv} vs {hv}]")
        r["positions"] = " ".join(bad) if bad else "same"

        for key, c_sql, h_sql in (
                ("star", "SELECT md5(lz_c::text) FROM lz_c WHERE id = 999",
                 "SELECT md5(lz_h::text) FROM lz_h WHERE id = 999"),
                ("range", f"SELECT sum(c1) + sum(c{NCOLS}) FROM lz_c WHERE id <= 2000",
                 f"SELECT sum(c1) + sum(c{NCOLS}) FROM lz_h WHERE id <= 2000"),
                ("count", "SELECT count(*) FROM lz_c WHERE id <= 2000",
                 "SELECT count(*) FROM lz_h WHERE id <= 2000"),
                ("order", "SELECT string_agg(c1::text, ',' ORDER BY c1) "
                          "FROM (SELECT c1 FROM lz_c WHERE id <= 20) s",
                 "SELECT string_agg(c1::text, ',' ORDER BY c1) "
                 "FROM (SELECT c1 FROM lz_h WHERE id <= 20) s")):
            r[key] = (_value(dsn, c_sql), _value(dsn, h_sql))

        # a row still in this transaction's write buffer
        try:
            with psycopg.connect(dsn, connect_timeout=10) as conn:
                for s in FORCE:
                    conn.execute(s)
                conn.execute("INSERT INTO lz_c (id, c1, txt) VALUES (999999, 4242, 'buffered')")
                row = conn.execute("SELECT c1 || '|' || txt FROM lz_c WHERE id = 999999").fetchone()
                conn.rollback()
            r["buffered"] = row[0] if row and row[0] is not None else query_error("no buffered row")
        except psycopg.Error as exc:
            r["buffered"] = query_error(_message(exc))

        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
                for s in FORCE:
                    conn.execute(s)
                conn.execute("DROP TABLE IF EXISTS lz_copy")
                conn.execute("CREATE TABLE lz_copy AS SELECT * FROM lz_c WHERE id <= 500")
                n = conn.execute(f"SELECT count(*) FROM lz_copy c JOIN lz_h h USING (id) "
                                 f"WHERE c.c1 IS DISTINCT FROM h.c1 "
                                 f"OR c.c{NCOLS} IS DISTINCT FROM h.c{NCOLS} "
                                 f"OR c.txt IS DISTINCT FROM h.txt").fetchone()[0]
            r["copy"] = str(n)
        except psycopg.Error as exc:
            r["copy"] = query_error(_message(exc))

        u_err = _setup(dsn, ["DROP TABLE IF EXISTS lz_u",
                             "CREATE TABLE lz_u (id int, v bigint) USING pgcolumnar",
                             "INSERT INTO lz_u SELECT g, g FROM generate_series(1, 1000) g",
                             "CREATE UNIQUE INDEX lz_u_id ON lz_u (id)"])
        if u_err:
            r["unique"] = u_err
        else:
            try:
                with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
                    conn.execute("INSERT INTO lz_u VALUES (500, 500)")
                r["unique"] = "no (the duplicate was accepted)"
            except psycopg.Error as exc:
                msg = _message(exc)
                r["unique"] = "yes" if "duplicate key" in msg else f"no ({msg})"

        w_err = _setup(dsn, _build_wide("lz_n", 2) + _build_wide("lz_w", NCOLS))
        narrow = w_err or _ms(dsn, "lz_n")
        wide = w_err or _ms(dsn, "lz_w")
        if isinstance(narrow, int) and isinstance(wide, int):
            r["cliff"] = "yes" if narrow > 0 and wide / narrow < 25 else f"no ({narrow}ms vs {wide}ms)"
        else:
            r["cliff"] = f"no reading ({narrow} / {wide})"
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_lazy_slot_the_fetch_is_an_index_scan(lz, expect):
    expect.text(lz["index scan"], "1", "the fetch really is an index scan")


def test_lazy_slot_every_column_position_matches_heap(lz, expect):
    expect.text(lz["positions"], "same", "single-row index fetches match heap for every column position")


def test_lazy_slot_select_star_matches_heap(lz, expect):
    c, h = lz["star"]
    expect.text(c, h, "SELECT * through the index matches heap")


def test_lazy_slot_a_range_sums_the_same_as_heap(lz, expect):
    c, h = lz["range"]
    expect.text(c, h, "a range of rows sums the same as heap")


def test_lazy_slot_count_star_matches_heap(lz, expect):
    c, h = lz["count"]
    expect.text(c, h, "count(*) through the index matches heap")


def test_lazy_slot_a_buffered_row_reads_correctly(lz, expect):
    expect.text(lz["buffered"], "4242|buffered", "a row fetched before it is flushed reads correctly")


def test_lazy_slot_an_order_by_matches_heap(lz, expect):
    c, h = lz["order"]
    expect.text(c, h, "an ORDER BY over index-fetched rows matches heap")


def test_lazy_slot_a_copy_keeps_every_column(lz, expect):
    expect.text(lz["copy"], "0", "copying index-fetched rows into a heap table keeps every column")


def test_lazy_slot_a_unique_violation_is_detected(lz, expect):
    expect.text(lz["unique"], "yes", "a unique violation is still detected through the fetch path")


def test_lazy_slot_a_wide_table_is_not_in_another_class(lz, expect):
    expect.text(lz["cliff"], "yes", "a wide table is not in a different class from a narrow one")

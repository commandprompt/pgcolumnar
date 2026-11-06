"""Native fetch-by-row-number through an index (Phase D6a, #1341). The pytest twin of
`test/native_index.sh`, every check by the shell's name.

An index scan, and unique and primary-key enforcement, all route through the fetch by
row number. Before D6a an index scan on a native table returned no rows, and unique
was silently unenforced. Lookups are compared with a heap mirror under a forced index
path. The columnar custom scan is off as well as seqscan, because seqscan alone left
the range aggregate planned as the custom scan, and each lookup asserts first that its
plan is an Index Scan through n_pkey.
"""
import psycopg

from pgc_vacuity import query_error

FORCE_INDEX = ("SET enable_seqscan=off", "SET enable_bitmapscan=off", "SET enable_indexonlyscan=off",
               "SET pgcolumnar.enable_custom_scan=off")


def _one(cur, sql):
    cur.execute(sql)
    row = cur.fetchone()
    return "|".join("" if v is None else str(v) for v in row) if row else ""


def _forced(cur, sql):
    """-> `sql` under the forced index path, as the shell's iscan; an error is a value."""
    try:
        for s in FORCE_INDEX:
            cur.execute(s)
        return _one(cur, sql)
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)
    finally:
        for g in ("enable_seqscan", "enable_bitmapscan", "enable_indexonlyscan", "pgcolumnar.enable_custom_scan"):
            cur.execute(f"RESET {g}")


def _plan_fetches(cur, sql):
    """-> 'yes' when `sql` plans an Index Scan under the forced path, as pgc_uses_row_fetch."""
    try:
        for s in FORCE_INDEX:
            cur.execute(s)
        cur.execute(f"EXPLAIN (COSTS OFF) {sql}")
        return "yes" if any("Index Scan using" in r[0] for r in cur.fetchall()) else "no"
    finally:
        for g in ("enable_seqscan", "enable_bitmapscan", "enable_indexonlyscan", "pgcolumnar.enable_custom_scan"):
            cur.execute(f"RESET {g}")


def _refused(cur, sql):
    """-> 1 when `sql` is refused as a duplicate, as the shell's grep -c counts it."""
    try:
        cur.execute(sql)
        return 0
    except psycopg.errors.UniqueViolation:
        return 1
    except psycopg.Error:
        return 0


def test_index_lookups_match_the_heap(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE h (id int PRIMARY KEY, k bigint, v text)")
        cur.execute("CREATE TABLE n (id int PRIMARY KEY, k bigint, v text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048)")
        gen = "SELECT g, (g * 10)::bigint, 'row-' || g FROM generate_series(1, 8000) g"
        cur.execute(f"INSERT INTO h {gen}")
        cur.execute(f"INSERT INTO n {gen}")
        expect.num(int(_one(cur, "SELECT count(*) FROM n")), 8000, "row count")
        for label, sql in (
            ("point lookup", "SELECT v FROM n WHERE id = 4242"),
            ("range aggregate", "SELECT count(*), sum(k), min(v), max(v) FROM n WHERE id BETWEEN 100 AND 400"),
            ("IN lookup", "SELECT count(*), sum(k) FROM n WHERE id IN (1,2000,4000,6000,8000)"),
            ("lookup miss", "SELECT count(*) FROM n WHERE id = 999999"),
        ):
            expect.text(_plan_fetches(cur, sql), "yes", f"premise: the index {label} fetches through n_pkey")
        expect.text(_forced(cur, "SELECT v FROM n WHERE id = 4242"), _one(cur, "SELECT v FROM h WHERE id = 4242"),
                    "index point lookup")
        expect.text(_forced(cur, "SELECT count(*), sum(k), min(v), max(v) FROM n WHERE id BETWEEN 100 AND 400"),
                    _one(cur, "SELECT count(*), sum(k), min(v), max(v) FROM h WHERE id BETWEEN 100 AND 400"),
                    "index range aggregate")
        expect.text(_forced(cur, "SELECT count(*), sum(k) FROM n WHERE id IN (1,2000,4000,6000,8000)"),
                    _one(cur, "SELECT count(*), sum(k) FROM h WHERE id IN (1,2000,4000,6000,8000)"),
                    "index IN lookup")
        expect.text(_forced(cur, "SELECT count(*) FROM n WHERE id = 999999"), "0", "index lookup miss returns nothing")

        # the fetch is what finds the conflicting row: before D6a duplicates went in
        expect.num(_refused(cur, "INSERT INTO n VALUES (4242, 1, 'dup')"), 1, "duplicate id rejected")
        expect.num(int(_one(cur, "SELECT count(*) FROM n")), 8000, "row count unchanged after rejected dup")


def test_a_standalone_unique_index_enforces(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE nu (id int, k bigint) USING pgcolumnar")
        cur.execute("CREATE UNIQUE INDEX nu_k ON nu (k)")
        cur.execute("INSERT INTO nu SELECT g, g FROM generate_series(1,1000) g")
        expect.num(_refused(cur, "INSERT INTO nu VALUES (99999, 500)"), 1, "unique index rejects duplicate")
        try:
            cur.execute("INSERT INTO nu VALUES (99999, 100000)")
            errors = 0
        except psycopg.Error:
            errors = 1
        expect.num(errors, 0, "unique index admits a new key")

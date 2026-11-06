"""CREATE INDEX projects only the columns the index needs, serial and parallel, and the
index it builds agrees with a sequential scan (#413, #1341). The pytest twin of
`test/native_index_projection.sh`, every check by the shell's name.

A 20-column table and indexes on a key, two keys, a late column, an expression, a
partial predicate and a text expression: each build's own DEBUG1 line says which
branch ran and how many columns its reader decodes, which is the check a do-nothing fix
would fail. The parallel build, the default for any table of size, must project too.
Then correctness: point lookups through the index, and the FULL ordered result of an
index scan against a sequential scan. The index side runs with the columnar scan off
and each oracle asserts the index it reads: with only seqscan and bitmapscan off, the
late-column, expression and text-expression oracles ran as the columnar scan on both
sides, comparing a sequential scan with itself. amcheck closes it.
"""
import re

import psycopg
import pytest

ROWS = 300000
IXOFF = ("SET enable_seqscan=off", "SET enable_bitmapscan=off", "SET pgcolumnar.enable_custom_scan=off")
SEQ = ("SET enable_indexscan=off", "SET enable_bitmapscan=off", "SET enable_indexonlyscan=off")
PAR = ("SET max_parallel_maintenance_workers=4", "SET min_parallel_table_scan_size='0'",
       "SET maintenance_work_mem='256MB'")
INDEXES = ("w_k", "w_k12", "w_c18", "w_expr", "w_part", "w_len", "w_par", "w_par2", "w_pare", "w_parp", "w_ser")
LINE = re.compile(r'(parallel|serial) index build on "([a-z0-9_]+)" projecting (\d+) of (\d+)')


def _conn(cluster, db):
    return psycopg.connect(cluster.dsn(dbname=db), autocommit=True)


def _build(conn, sets, ddl):
    """-> 'branch n of m' from the build's DEBUG1 line, or '' when it printed none."""
    seen = []

    def handler(diag):
        seen.append(diag.message_primary or "")

    conn.add_notice_handler(handler)
    try:
        conn.execute("SET client_min_messages = debug1")
        for s in sets:
            conn.execute(s)
        conn.execute(ddl)
    finally:
        conn.execute("RESET client_min_messages")
        for g in ("max_parallel_maintenance_workers", "min_parallel_table_scan_size", "maintenance_work_mem"):
            conn.execute(f"RESET {g}")
        conn.remove_notice_handler(handler)
    for msg in seen:
        m = LINE.search(msg)
        if m:
            return f"{m.group(1)} {m.group(3)} of {m.group(4)}"
    return ""


def _under(conn, sets, sql):
    for s in sets:
        conn.execute(s)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        for g in ("enable_seqscan", "enable_bitmapscan", "enable_indexscan", "enable_indexonlyscan",
                  "pgcolumnar.enable_custom_scan"):
            conn.execute(f"RESET {g}")


@pytest.fixture(scope="module")
def wide(pgc_cluster):
    """-> (cluster, db, {index: 'branch n of m'}): the table and its SERIAL indexes. The
    parallel ones come later, as in the shell, so the serial oracles cannot pick them."""
    db = "pgc_idxproj"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with _conn(pgc_cluster, db) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            cols = ", ".join(["k int", "k2 int"] + [f"c{i} text" for i in range(1, 19)])
            vals = ", ".join(["g", "g*2"] + ["repeat('x',80)||g"] * 18)
            c.execute(f"CREATE TABLE w ({cols}) USING pgcolumnar")
            c.execute(f"INSERT INTO w SELECT {vals} FROM generate_series(1,{ROWS}) g")
            built = {}
            for ix, ddl in (("w_k", "CREATE INDEX w_k ON w (k)"), ("w_k12", "CREATE INDEX w_k12 ON w (k, k2)"),
                            ("w_c18", "CREATE INDEX w_c18 ON w (c18)"),
                            ("w_expr", "CREATE INDEX w_expr ON w ((k + k2))"),
                            ("w_part", "CREATE INDEX w_part ON w (k) WHERE k2 > 100"),
                            ("w_len", "CREATE INDEX w_len ON w ((length(c1)))")):
                built[ix] = _build(c, (), ddl)
        yield pgc_cluster, db, built
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


@pytest.fixture(scope="module")
def par(wide):
    """-> wide, after the parallel builds and the serial c17 build have been added."""
    cluster, db, built = wide
    with _conn(cluster, db) as c:
        for ix, ddl in (("w_par", "CREATE INDEX w_par ON w (k)"), ("w_par2", "CREATE INDEX w_par2 ON w (k2)"),
                        ("w_pare", "CREATE INDEX w_pare ON w ((k + k2))"),
                        ("w_parp", "CREATE INDEX w_parp ON w (k) WHERE k2 > 100")):
            built[ix] = _build(c, PAR, ddl)
        built["w_ser"] = _build(c, ("SET max_parallel_maintenance_workers=0",), "CREATE INDEX w_ser ON w (c17)")
    return wide


def _proj(built, ix):
    return built[ix].split(" ", 1)[1] if " " in built[ix] else built[ix]


def test_the_built_indexes_answer_as_a_sequential_scan_does(wide, expect):
    cluster, db, _ = wide
    lookup = ("SET enable_seqscan=off", "SET pgcolumnar.enable_custom_scan=off")
    with _conn(cluster, db) as c:
        expect.num(_under(c, lookup, "SELECT k2 FROM w WHERE k = 12345"), 24690, "plain index finds the row")
        expect.num(_under(c, lookup, "SELECT count(*) FROM w WHERE (k + k2) = 30000"), 1, "expression index finds the row")
        expect.num(_under(c, lookup, "SELECT count(*) FROM w WHERE k = 50000 AND k2 > 100"), 1,
                   "partial index finds the row inside its predicate")
        for label, pred, sel, ix in (
            ("plain index agrees with a sequential scan", "k BETWEEN 1000 AND 9999", "k", "w_k(12)?"),
            ("late-column index agrees", "c18 > repeat('x',80)||'99000'", "k", "w_c18"),
            ("expression index agrees", "(k + k2) BETWEEN 300 AND 30000", "k", "w_expr"),
            ("partial index agrees on its subset", "k2 > 100 AND k < 5000", "k", "w_part"),
            ("text expression index agrees", "length(c1) = 83", "k", "w_len"),
        ):
            # the plan of the oracle query itself, fenced so the aggregate's ORDER BY
            # cannot send it down a full scan of another index
            agg = (f"SELECT md5(string_agg(t::text, ',' ORDER BY t)) "
                   f"FROM (SELECT {sel} AS t FROM w WHERE {pred} OFFSET 0) s")
            for s in IXOFF:
                c.execute(s)
            plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {agg}"))
            for g in ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan"):
                c.execute(f"RESET {g}")
            expect.num(len(re.findall(rf"Index (Only )?Scan using {ix} on w", plan)), 1,
                       f"premise: {label} -- the index side reads {ix}")
            viaix, viaseq = _under(c, IXOFF, agg), _under(c, SEQ, agg)
            # both sides a real md5: two empty results compare equal and prove nothing (#418)
            if not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{32}", h) for h in (viaix, viaseq)):
                expect.text(f"index=[{viaix}] seq=[{viaseq}] (both sides must be a real md5)", "two md5 hashes", label)
                continue
            expect.text(viaix, viaseq, label)


def test_each_build_projects_only_what_it_needs(par, expect):
    cluster, db, built = par
    with _conn(cluster, db) as c:
        expect.num(c.execute("SELECT count(*) FROM w").fetchone()[0], ROWS, "fixture rows")
        expect.num(c.execute("SELECT count(*) FROM pg_attribute WHERE attrelid='w'::regclass AND attnum>0")
                   .fetchone()[0], 20, "fixture is wide")
    expect.text(_proj(built, "w_k"), "1 of 20", "plain index projects one column of twenty")
    expect.text(_proj(built, "w_k12"), "2 of 20", "two-column index projects two")
    expect.text(_proj(built, "w_c18"), "1 of 20", "an index on a late column projects one, not everything before it")
    expect.text(_proj(built, "w_expr"), "2 of 20", "an expression index projects its expression's columns")
    expect.text(_proj(built, "w_part"), "2 of 20", "a partial index projects the predicate's columns too")
    expect.text(_proj(built, "w_len"), "1 of 20", "an expression over a text column projects that column")
    # the premise first: if core declines to go parallel, the next checks read the serial branch
    expect.text(built["w_par"].split(" ")[0], "parallel",
                "forcing parallel maintenance workers does reach the parallel branch")
    expect.text(built["w_par2"], "parallel 1 of 20", "a parallel build projects one column of twenty, not all twenty")
    expect.text(built["w_pare"], "parallel 2 of 20", "a parallel expression build projects its expression's columns")
    expect.text(built["w_parp"], "parallel 2 of 20", "a parallel partial build projects the predicate's columns too")
    expect.text(built["w_ser"].split(" ")[0], "serial", "the serial branch is still reached when workers are refused")


def test_the_parallel_built_index_agrees_and_holds_each_row_once(par, expect):
    cluster, db, _ = par
    with _conn(cluster, db) as c:
        # premise first: the query names a column, not an index
        for s in ("SET enable_seqscan=off", "SET enable_bitmapscan=off"):
            c.execute(s)
        plan = "\n".join(r[0] for r in c.execute("EXPLAIN (COSTS OFF) SELECT k2 FROM w WHERE k2 BETWEEN 2000 AND 19998"))
        for g in ("enable_seqscan", "enable_bitmapscan"):
            c.execute(f"RESET {g}")
        expect.text((re.findall(r"using ([a-z0-9_]+)", plan) or [""])[0], "w_par2",
                    "the oracle below really reads the parallel-built index")
        agg = ("SELECT md5(string_agg(t::text, ',' ORDER BY t)) "
               "FROM (SELECT k2 AS t FROM w WHERE k2 BETWEEN 2000 AND 19998 OFFSET 0) s")
        for st in IXOFF:
            c.execute(st)
        plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {agg}"))
        for g in ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan"):
            c.execute(f"RESET {g}")
        expect.num(len(re.findall(r"Index (Only )?Scan using w_par2 on w", plan)), 1,
                   "premise: parallel-built index agrees with a sequential scan -- the index side reads w_par2")
        expect.text(_under(c, IXOFF, agg), _under(c, SEQ, agg), "parallel-built index agrees with a sequential scan")
        expect.num(_under(c, ("SET enable_seqscan=off", "SET enable_bitmapscan=off"),
                          "SELECT count(*) FROM w WHERE k2 BETWEEN 2 AND 2000"), 1000,
                   "parallel-built index returns each row once")


def test_amcheck_finds_every_index_sound(par, expect):
    cluster, db, _ = par
    with _conn(cluster, db) as c:
        try:
            c.execute("CREATE EXTENSION IF NOT EXISTS amcheck")
        except psycopg.Error as exc:
            expect.cannot_run("MISSING_DEPENDENCY", f"amcheck is not installed on this build ({exc.diag.message_primary})")
            return
        for ix in INDEXES:
            try:
                c.execute(f"SELECT bt_index_check('{ix}'::regclass)")
                verdict = "ok"
            except psycopg.Error:
                verdict = "bad"
            expect.text(verdict, "ok", f"bt_index_check({ix})")

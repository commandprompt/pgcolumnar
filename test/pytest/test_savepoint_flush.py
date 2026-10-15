"""Rolling back to a savepoint, or failing a statement, after rows have already
been flushed into row groups (#1341). The pytest twin of `test/savepoint_flush.sh`,
every check by the shell's name.

A flushed group is a catalog row written under the subtransaction, so the abort
must hide it; rows still buffered, and buffered delete marks, must be thrown away.

Each shape runs on a columnar table and on heap, and heap is the answer. The
columnar table writes row groups of 1000 rows, so every write crosses several
flushes. Each transaction runs one statement at a time and goes on after an error,
as psql does with ON_ERROR_STOP off, and records labelled values (label=value).
The two tables are compared label by label, inside the transaction and after it.
Both tables run every transaction, so a columnar failure cannot leave heap
untouched and the comparison vacuous. Every shape starts from fresh tables, so a
shape that goes wrong cannot make the ones after it fail for its rows.
"""
import psycopg

AMS = ("pgcolumnar", "heap")
SESSION = ["SET pgcolumnar.stripe_row_limit = 1000", "SET statement_timeout = '60s'"]

# What every reader sees: a plain sequential scan through the table AM, the custom
# scan without vectorization, the vectorized count, and the primary key's index.
# Each is the settings that choose its path and its query.
READERS = {
    "seq": (["SET pgcolumnar.enable_custom_scan = off"],
            "SELECT 'seq=' || count(*) || '|' || coalesce(sum(id), 0) || '|' || coalesce(sum(v), 0) FROM %t"),
    "scan": (["SET pgcolumnar.enable_vectorization = off"],
             "SELECT 'scan=' || count(*) || '|' || coalesce(sum(id), 0) || '|' || coalesce(sum(v), 0) FROM %t"),
    "vec": (["SET pgcolumnar.enable_vectorization = on"],
            "SELECT 'vec=' || count(*) || '|' || coalesce(sum(id), 0) FROM %t"),
    "idx": (["SET pgcolumnar.enable_custom_scan = off", "SET enable_seqscan = off",
             "SET enable_bitmapscan = off"],
            "SELECT 'idx=' || count(*) || '|' || coalesce(sum(v), 0) FROM %t WHERE id > 1000"),
}
RESET = ["RESET pgcolumnar.enable_custom_scan", "RESET pgcolumnar.enable_vectorization",
         "RESET enable_seqscan", "RESET enable_bitmapscan"]
# This transaction's inserts into row_group and delete_vector. pg_stat_xact_all_tables
# still counts a subtransaction's inserts after it aborts, so it shows what a
# rolled-back write flushed.
# Read once after BEGIN ("b") and once after ROLLBACK TO ("a"); the premise is the
# difference, since this one connection's counters include every earlier shape.
def _catalog_ins(tag):
    return [f"SELECT 'rg_{tag}=' || n_tup_ins FROM pg_stat_xact_all_tables "
            "WHERE relid = 'pgcolumnar.row_group'::regclass",
            f"SELECT 'dv_{tag}=' || n_tup_ins FROM pg_stat_xact_all_tables "
            "WHERE relid = 'pgcolumnar.delete_vector'::regclass"]
# Reads the table at row 2500 -- a scan starting flushes the delete marks buffered
# so far -- and divides by zero at row 5000.
PROBE = ("CREATE FUNCTION sp_probe(t regclass, id int) RETURNS int LANGUAGE plpgsql AS $$ "
         "BEGIN IF id = 2500 THEN EXECUTE format('SELECT count(*) FROM %s', t); END IF; "
         "RETURN 1 / (5000 - id) * 0; END $$")
GROUPS = ("SELECT 'groups=' || count(*) FROM pgcolumnar.row_group "
          "WHERE storage_id = pgcolumnar.get_storage_id('sp_pgcolumnar')")


def _reads(prefix=""):
    out = []
    for name, (sets, query) in READERS.items():
        out += sets + [query.replace(f"'{name}=", f"'{prefix}{name}=")] + RESET
    return out


READS = _reads()
READS_IN = _reads("in_")


def _xact(conn, statements):
    """Run `statements` (with %t for the table) on both tables, one at a time.

    -> {am: (lines, errors)}: each row's first value, and each error's first line.
    """
    out = {}
    for am in AMS:
        lines, errors = [], []
        for sql in SESSION + statements:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql.replace("%t", f"sp_{am}"))
                    if cur.description:
                        lines += [str(r[0]) for r in cur.fetchall()]
            except psycopg.Error as exc:
                errors.append(str(exc).splitlines()[0])
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.execute("ROLLBACK")  # a psql session would have ended here
        out[am] = (lines, errors)
    return out


def _pick(out, am, label):
    return next((ln.split("=", 1)[1] for ln in out[am][0] if ln.startswith(f"{label}=")), "")


def _same(out, *labels):
    diff = ""
    for label in labels:
        c, h = _pick(out, "pgcolumnar", label), _pick(out, "heap", label)
        if not h:
            diff += f" {label}: heap printed nothing ({out['heap']})"
        elif c != h:
            diff += f" {label}: columnar=[{c}] heap=[{h}]"
    return diff or "same"


def _errors(out, msg=None):
    bad = ""
    want = 1 if msg else 0
    for am in AMS:
        errs = out[am][1]
        if len(errs) != want or (msg and msg not in errs[0]):
            bad += f" {am}: {len(errs)} errors, first [{errs[0] if errs else ''}]"
    return bad or "as expected"


def _grew(out):
    g = [int(v.split("=", 1)[1]) for v in out["pgcolumnar"][0] if v.startswith("groups=")]
    return "yes" if len(g) >= 2 and g[1] - g[0] >= 2 else f"no (groups read: {g})"


def _flushed(out):
    """yes when both catalogs gained two rows or more between the b and a readings."""
    vals = {k: _pick(out, "pgcolumnar", k) for k in ("rg_b", "dv_b", "rg_a", "dv_a")}
    if not all(v.isdigit() for v in vals.values()):
        return f"no (readings {vals})"
    rg, dv = int(vals["rg_a"]) - int(vals["rg_b"]), int(vals["dv_a"]) - int(vals["dv_b"])
    return "yes" if rg >= 2 and dv >= 2 else f"no (row_group inserts [{rg}], delete_vector inserts [{dv}])"


def _fresh(conn, n=2000):
    """Both tables dropped and made again, holding rows 1..n."""
    with conn.cursor() as cur:
        for sql in SESSION:
            cur.execute(sql)
        for am in AMS:
            cur.execute(f"DROP TABLE IF EXISTS sp_{am}")
            cur.execute(f"CREATE TABLE sp_{am} (id int PRIMARY KEY, v int) USING {am}")
            cur.execute(f"INSERT INTO sp_{am} SELECT g, 0 FROM generate_series(1, {n}) g")


def _plan(conn, reader):
    sets, query = READERS[reader]
    with conn.cursor() as cur:
        for sql in SESSION + sets:
            cur.execute(sql)
        cur.execute("EXPLAIN (COSTS OFF) " + query.replace("%t", "sp_pgcolumnar"))
        plan = "\n".join(r[0] for r in cur.fetchall())
        for sql in RESET:
            cur.execute(sql)
    return plan


def _count(plan, *words):
    return sum(1 for ln in plan.splitlines() if any(w in ln for w in words))


def test_a_savepoint_rolled_back_after_a_flush(pgc_conn, expect):
    pgc_conn.execute(PROBE)
    _fresh(pgc_conn)

    # ---- the readers take the paths they are named for ----
    p = _plan(pgc_conn, "seq")
    expect.text(f"{_count(p, 'Seq Scan')}|{_count(p, 'Custom Scan')}", "1|0",
                "the seq reader is a plain sequential scan, not the custom scan")
    p = _plan(pgc_conn, "scan")
    expect.text(f"{_count(p, 'Custom Scan')}|{_count(p, 'Columnar Vectorized Aggregates')}", "1|0",
                "the scan reader is the custom scan with no vectorized aggregate")
    expect.text(str(_count(_plan(pgc_conn, "vec"), "Columnar Vectorized Aggregates")), "1",
                "the vec reader is a vectorized aggregate")
    p = _plan(pgc_conn, "idx")
    expect.text(f"{_count(p, 'Index Scan', 'Index Only Scan')}|{_count(p, 'Seq Scan', 'Custom Scan')}", "1|0",
                "the idx reader reads the primary key's index")

    every = ("seq", "scan", "vec", "idx")

    # ---- a flushed INSERT rolled back to a savepoint ----
    _fresh(pgc_conn)
    out = _xact(pgc_conn, ["BEGIN", GROUPS, "SAVEPOINT s",
                           "INSERT INTO %t SELECT g, 1 FROM generate_series(10001, 15000) g",
                           GROUPS, "SELECT 'inside=' || count(*) FROM %t",
                           "ROLLBACK TO s"] + READS + ["COMMIT"])
    expect.text(_errors(out), "as expected", "a savepoint's INSERT runs, and ROLLBACK TO it, on both tables")
    expect.text(_grew(out), "yes", "the savepoint's INSERT flushed row groups before ROLLBACK TO")
    expect.text(_same(out, "inside"), "same", "inside the savepoint, every reader saw the INSERT, as heap does")
    expect.text(_same(out, *every), "same",
                "after ROLLBACK TO, in the same transaction, no reader sees the savepoint's rows, as heap does")
    expect.text(_same(_xact(pgc_conn, READS), *every), "same",
                "and after COMMIT, no reader sees them, as heap does")

    # ---- the same, under one snapshot ----
    # At REPEATABLE READ the reads after ROLLBACK TO use the snapshot the reads
    # inside the savepoint used, so only the abort itself can hide the groups.
    _fresh(pgc_conn)
    out = _xact(pgc_conn, ["BEGIN ISOLATION LEVEL REPEATABLE READ", "SELECT 1 FROM %t LIMIT 1",
                           "SAVEPOINT s",
                           "INSERT INTO %t SELECT g, 1 FROM generate_series(10001, 15000) g"]
                + READS_IN + ["ROLLBACK TO s"] + READS + ["COMMIT"])
    expect.text(_errors(out), "as expected",
                "at REPEATABLE READ, a savepoint's INSERT runs, and ROLLBACK TO it, on both tables")
    expect.text(_same(out, "in_seq", "in_scan", "in_vec", "in_idx"), "same",
                "at REPEATABLE READ, inside the savepoint, every reader saw the INSERT, as heap does")
    expect.text(_same(out, *every), "same",
                "at REPEATABLE READ, after ROLLBACK TO, no reader sees the savepoint's rows, as heap does")

    # ---- the same unique keys again after ROLLBACK TO ----
    _fresh(pgc_conn)
    out = _xact(pgc_conn, ["BEGIN", "SAVEPOINT s",
                           "INSERT INTO %t SELECT g, 2 FROM generate_series(10001, 15000) g",
                           "ROLLBACK TO s",
                           "INSERT INTO %t SELECT g, 3 FROM generate_series(10001, 15000) g"]
                + READS + ["COMMIT"])
    expect.text(_errors(out), "as expected",
                "after ROLLBACK TO, the same unique keys insert again in the same transaction, as on heap")
    expect.text(_same(out, *every), "same",
                "and every reader sees each key once, with the second insert's values, as heap does")
    expect.text(_same(_xact(pgc_conn, READS), *every), "same",
                "and after COMMIT, each key is there once, as heap does")

    # ---- a large UPDATE rolled back to a savepoint ----
    _fresh(pgc_conn, 5000)
    out = _xact(pgc_conn, ["BEGIN"] + _catalog_ins("b") + ["SAVEPOINT s", "UPDATE %t SET v = v + 10",
                           "SELECT 'inside=' || count(*) || '|' || sum(v) FROM %t",
                           "ROLLBACK TO s"] + _catalog_ins("a") + READS + ["COMMIT"])
    expect.text(_errors(out), "as expected",
                "an UPDATE of every row in a savepoint runs, and ROLLBACK TO it, on both tables")
    expect.text(_flushed(out), "yes",
                "the savepoint's UPDATE flushed new versions and delete marks before ROLLBACK TO")
    expect.text(_same(out, "inside"), "same",
                "inside the savepoint, the UPDATE changed each row once, as heap does")
    expect.text(_same(out, *every), "same",
                "after ROLLBACK TO, in the same transaction, every reader sees the rows as they were, as heap does")
    expect.text(_same(_xact(pgc_conn, READS), *every), "same",
                "and after COMMIT, the rows are as they were, as heap does")

    # ---- a statement that fails after it has flushed ----
    # Rows are generated in order, so the division by zero comes at the 3500th row,
    # after three flushes. The same statement without the failing row shows the flushes.
    fail = "INSERT INTO %t SELECT g, 1 / (g - 23500) FROM generate_series(20001, 24000) g"
    _fresh(pgc_conn)
    out = _xact(pgc_conn, ["BEGIN", GROUPS,
                           "INSERT INTO %t SELECT g, 1 / (g - 99999) FROM generate_series(20001, 24000) g",
                           GROUPS, "ROLLBACK"])
    expect.text(_grew(out), "yes", "the failing INSERT's shape flushes row groups before its 3500th row")

    _fresh(pgc_conn)
    expect.text(_errors(_xact(pgc_conn, [fail]), "division by zero"), "as expected",
                "an INSERT that fails after flushing is refused on both tables")
    expect.text(_same(_xact(pgc_conn, READS), *every), "same",
                "and no reader sees any of its rows, as heap does")

    _fresh(pgc_conn)
    out = _xact(pgc_conn, ["BEGIN", "SAVEPOINT s", fail, "ROLLBACK TO s",
                           "INSERT INTO %t VALUES (30001, 5), (30002, 5)"] + READS + ["COMMIT"])
    expect.text(_errors(out, "division by zero"), "as expected",
                "in a savepoint, an INSERT that fails after flushing raises its error and nothing else, on both tables")
    expect.text(_same(out, *every), "same",
                "and in the same transaction every reader sees only the rows written after it, as heap does")
    expect.text(_same(_xact(pgc_conn, READS), *every), "same",
                "and after COMMIT, only the rows written after the failed INSERT are there, as heap does")

    # An UPDATE of every row that fails at the last row it reaches (5000, the last
    # in insert order). By then it has flushed new versions, and sp_probe's read at
    # row 2500 flushed the delete marks of the rows before it. The premise reads both
    # catalogs' inserts right after ROLLBACK TO, before anything else writes.
    _fresh(pgc_conn, 5000)
    out = _xact(pgc_conn, ["BEGIN"] + _catalog_ins("b") + ["SAVEPOINT s", "UPDATE %t SET v = v + sp_probe('%t', id)",
                           "ROLLBACK TO s"] + _catalog_ins("a") + ["UPDATE %t SET v = v + 100 WHERE id = 1"]
                + READS + ["COMMIT"])
    expect.text(_errors(out, "division by zero"), "as expected",
                "in a savepoint, an UPDATE of every row that fails at the last raises its error and nothing else, "
                "on both tables")
    expect.text(_flushed(out), "yes",
                "the failing UPDATE flushed new versions and delete marks before it failed")
    expect.text(_same(out, *every), "same",
                "and in the same transaction every reader sees the rows as they were, plus the later UPDATE, "
                "as heap does")
    expect.text(_same(_xact(pgc_conn, READS), *every), "same",
                "and after COMMIT, the rows are as they were plus the later UPDATE, as heap does")

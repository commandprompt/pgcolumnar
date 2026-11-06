"""The bug-audit regressions, each a defect the audit found (#965). The pytest twin of
`test/audit.sh`, every check by the shell's name.

Six blocks, as in the shell: ADD COLUMN after data exists reads the missing value
(NULL, or the constant default) through every read path; chunk-group skipping does
not use a predicate under another collation; set_options refuses an out-of-range
limit, a heap table and a partitioned parent, each with its own guard's message,
and records a valid call; a parallel CREATE INDEX completes, leaks no relation
reference and indexes every row once; a write buffer opened before ADD COLUMN in the
same transaction keeps the values written into the new column; and a same-statement
duplicate against a unique index is rejected rather than spun on.

HOW A REFUSAL IS READ. As "rejected" only when the error carries the guard's own
message: with set_options failing at entry for an unrelated reason, every one of the
shell's refusal arms used to pass. Any other error is a failed query, which reds the
check by name.

HOW THE INDEX ARMS ARE READ. The build's own outcome is a check, and the row count is
read only when its plan reads the index: with a parallel build participant raising
an error, no index existed and the count was answered by a Seq Scan.

THE LEAK IS READ FROM THE SHARED CLUSTER'S LOG, from the offset at which this module
started and for the relation this module builds, so another module's warnings are
not counted.

ON A PRIVATE DATABASE. The partitioned-parent arm needs PostgreSQL 17 (a partitioned
table carrying an access method); before it, each of its tests declines under the
shell's skip name.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_audit"
IDXONLY = ("SET enable_seqscan=off", "SET enable_bitmapscan=off", "SET pgcolumnar.enable_custom_scan=off")
SKIP_NAME = "the partitioned-parent arm"


def _message(exc):
    return (exc.diag.message_primary if getattr(exc, "diag", None) and exc.diag.message_primary
            else str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, statements):
    """Run statements on one session; -> None, or the psycopg error that stopped them."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in statements:
                conn.execute(s)
    except psycopg.Error as exc:
        return exc
    return None


def _value(dsn, sql, settings=()):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _rows(dsn, sql):
    """-> every row's columns joined by ':' and the rows by ' ', or a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "".join(":".join("-" if v is None else str(v) for v in r) + " " for r in rows)


def _refusal(dsn, sql, want):
    """-> "rejected" when sql is refused with `want` in its message, "statement
    unexpectedly succeeded" when it runs, or a query_error() naming the other reason."""
    err = _run(dsn, [sql])
    if err is None:
        return "statement unexpectedly succeeded"
    msg = _message(err)
    if want in msg:
        return "rejected"
    return query_error(f"refused for another reason: {msg}")


def _sqlstate(dsn, sql):
    err = _run(dsn, [sql])
    if err is None:
        return query_error(f"no error from {sql}")
    return err.sqlstate or query_error(f"no SQLSTATE: {_message(err)}")


def _reads_index(dsn, index, sql):
    """-> yes when the plan of sql, with seq scans and the custom scan off, reads index."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in IDXONLY:
                conn.execute(s)
            plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS OFF) {sql}"))
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if f"Index Scan using {index} " in plan or f"Index Only Scan using {index} " in plan:
        return "yes"
    scan = next((l.strip() for l in plan.splitlines() if "Scan" in l), plan)
    return f"no: {scan}"


def _log_tail(path, offset):
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")


@pytest.fixture(scope="module")
def au(pgc_cluster):
    log = pgc_cluster.datadir / "server.log"
    try:
        start = log.stat().st_size
    except OSError:
        start = 0
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    dsn = pgc_cluster.dsn(dbname=DB)
    r = {}
    try:
        _run(dsn, ["CREATE EXTENSION IF NOT EXISTS pgcolumnar"])

        # ---- 1. ADD COLUMN after data exists --------------------------------------
        _run(dsn, ["CREATE TABLE ac (a int) USING pgcolumnar",
                   "INSERT INTO ac SELECT g FROM generate_series(1,5) g",
                   "ALTER TABLE ac ADD COLUMN b int",
                   "ALTER TABLE ac ADD COLUMN c int DEFAULT 42",
                   "ALTER TABLE ac ADD COLUMN d text DEFAULT 'hi'"])
        r["ac null"] = _value(dsn, "SELECT count(*) FROM ac WHERE b IS NULL")
        r["ac const"] = _value(dsn, "SELECT string_agg(DISTINCT c::text, ',') FROM ac")
        r["ac text"] = _value(dsn, "SELECT string_agg(DISTINCT d, ',') FROM ac")
        r["ac projected"] = _value(dsn, "SELECT c || '/' || d FROM ac ORDER BY a LIMIT 1")
        _run(dsn, ["INSERT INTO ac VALUES (6, 7, 8, 'new')"])
        r["ac mixed"] = _rows(dsn, "SELECT a, b, c, d FROM ac WHERE a IN (5,6) ORDER BY a")
        r["ac agg"] = _value(dsn, "SELECT sum(c) || '/' || count(d) || '/' || count(b) FROM ac")
        _run(dsn, ["CREATE INDEX ac_c_idx ON ac(c)"])
        r["ac plan"] = _reads_index(dsn, "ac_c_idx", "SELECT count(*) FROM ac WHERE c=42")
        r["ac index"] = _value(dsn, "SELECT count(*) FROM ac WHERE c=42", IDXONLY)

        # ---- 2. chunk-group skipping and collation --------------------------------
        _run(dsn, ["SET pgcolumnar.chunk_group_row_limit=100", "SET pgcolumnar.stripe_row_limit=1000",
                   'CREATE TABLE col (t text COLLATE "en_US") USING pgcolumnar',
                   "INSERT INTO col SELECT chr(65 + (g%20)) || lpad(g::text,5,'0') FROM generate_series(1,300) g"])
        q = "SELECT count(*) FROM col WHERE t < 'a' COLLATE \"C\""
        r["coll on"] = _value(dsn, q, ("SET pgcolumnar.enable_qual_pushdown=on",))
        r["coll off"] = _value(dsn, q, ("SET pgcolumnar.enable_qual_pushdown=off",))
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
                plan = "\n".join(x[0] for x in conn.execute("EXPLAIN (COSTS off) SELECT t FROM col WHERE t < 'A00001'"))
            r["pushed"] = str(sum(1 for l in plan.splitlines() if "Pushed-Down Filters: 1" in l))
        except psycopg.Error as exc:
            r["pushed"] = query_error(_message(exc))

        # ---- 3. set_options bounds and relation kinds -----------------------------
        _run(dsn, ["CREATE TABLE opt (a int) USING pgcolumnar", "CREATE TABLE opt_heap (a int) USING heap"])
        r["rej chunk"] = _refusal(dsn, "SELECT pgcolumnar.set_options('opt'::regclass, chunk_group_row_limit => 0)",
                                  "chunk_group_row_limit must be at least 100")
        r["rej stripe"] = _refusal(dsn, "SELECT pgcolumnar.set_options('opt'::regclass, stripe_row_limit => 5)",
                                   "stripe_row_limit must be at least 1000")
        r["rej level"] = _refusal(dsn, "SELECT pgcolumnar.set_options('opt'::regclass, compression_level => 99)",
                                  "compression_level must be between 1 and 22")
        heap_call = "SELECT pgcolumnar.set_options('opt_heap'::regclass, chunk_group_row_limit => 1000)"
        r["rej heap"] = _refusal(dsn, heap_call, "is not a columnar table")
        err = _run(dsn, [heap_call])
        r["heap says"] = ("yes" if err is not None and "is not a columnar table" in _message(err)
                          else query_error(f"no [{_message(err) if err is not None else 'it ran'}]"))
        r["heap recorded"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt_heap'::regclass")
        r["probe"] = _sqlstate(dsn, "SELECT 1/0")
        r["heap state"] = _sqlstate(dsn, heap_call)
        _run(dsn, ["DROP TABLE opt_heap"])
        major = _value(dsn, "SHOW server_version_num")
        r["major"] = major
        if isinstance(major, str) and major.isdigit() and int(major) >= 170000:
            _run(dsn, ["CREATE TABLE opt_part (id int, v text) PARTITION BY RANGE (id) USING pgcolumnar"])
            r["part premise"] = _value(dsn, "SELECT c.relkind::text || '/' || a.amname FROM pg_class c "
                                            "JOIN pg_am a ON a.oid = c.relam WHERE c.relname = 'opt_part'")
            r["rej part"] = _refusal(dsn, "SELECT pgcolumnar.set_options('opt_part'::regclass, chunk_group_row_limit => 1000)",
                                     "is not a columnar table")
            r["part recorded"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt_part'::regclass")
            _run(dsn, ["CREATE TABLE opt_part_1 PARTITION OF opt_part FOR VALUES FROM (0) TO (100)"])
            _run(dsn, ["SELECT pgcolumnar.set_options('opt_part_1'::regclass, chunk_group_row_limit => 1000)"])
            r["leaf recorded"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt_part_1'::regclass")
            _run(dsn, ["DROP TABLE opt_part"])
        elif isinstance(major, str) and major.isdigit():
            r["decline"] = f"PG{int(major) // 10000} refuses PARTITION BY ... USING pgcolumnar"
        # One statement per call, as the shell runs them: a failed set_options must not
        # stop the INSERT and DELETE that "valid options delete works" reads.
        _run(dsn, ["SELECT pgcolumnar.set_options('opt'::regclass, chunk_group_row_limit => 1000, "
                   "stripe_row_limit => 2000, compression_level => 9)"])
        _run(dsn, ["INSERT INTO opt SELECT g FROM generate_series(1,10) g"])
        _run(dsn, ["DELETE FROM opt WHERE a=5"])
        r["opt delete"] = _value(dsn, "SELECT count(*) FROM opt")
        r["opt recorded"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'opt'::regclass")

        # ---- 4. parallel CREATE INDEX ---------------------------------------------
        _run(dsn, ["SET pgcolumnar.stripe_row_limit=10000",
                   "CREATE TABLE idxleak (a int, b int) USING pgcolumnar WITH (parallel_workers=2)",
                   "INSERT INTO idxleak SELECT g, g%100 FROM generate_series(1,50000) g"])
        err = _run(dsn, ["SET max_parallel_maintenance_workers=2", "SET min_parallel_table_scan_size=0",
                         "CREATE INDEX idxleak_a_idx ON idxleak(a)"])
        r["built"] = "yes" if err is None else f"no: {_message(err)}"
        text = _log_tail(log, start)
        r["leaks"] = (str(sum(1 for l in text.splitlines()
                              if "resource was not closed" in l and 'relation "idxleak"' in l))
                      if not text.startswith(QUERY_ERROR) else text)
        r["idx plan"] = _reads_index(dsn, "idxleak_a_idx", "SELECT count(*) FROM idxleak WHERE a>0")
        r["idx once"] = (_value(dsn, "SELECT count(*) FROM idxleak WHERE a>0", IDXONLY) if r["idx plan"] == "yes"
                         else "not read through idxleak_a_idx")

        # ---- 5. ADD COLUMN with a write buffer open in the same transaction -------
        for t, stmts in (("wsalter", ["INSERT INTO wsalter VALUES (0,'x')",
                                      "ALTER TABLE wsalter ADD COLUMN w int DEFAULT 7",
                                      "INSERT INTO wsalter VALUES (1,'a',99)"]),
                         ("wsalter2", ["INSERT INTO wsalter2 VALUES (0,'x')",
                                       "ALTER TABLE wsalter2 ADD COLUMN w int",
                                       "INSERT INTO wsalter2 VALUES (1,'a',99)"]),
                         ("wsalter3", ["INSERT INTO wsalter3 VALUES (0,'x')",
                                       "ALTER TABLE wsalter3 ADD COLUMN w int DEFAULT 7",
                                       "UPDATE wsalter3 SET w = 55"])):
            _run(dsn, [f"CREATE TABLE {t} (id int, v text) USING pgcolumnar", "BEGIN", *stmts, "COMMIT"])
        r["ws default"] = _value(dsn, "SELECT string_agg(DISTINCT coalesce(w::text,'NULL'), ',' "
                                      "ORDER BY coalesce(w::text,'NULL')) FROM wsalter")
        r["ws nodefault"] = _value(dsn, "SELECT count(*) FROM wsalter2 WHERE w = 99")
        r["ws update"] = _value(dsn, "SELECT string_agg(DISTINCT w::text, ',') FROM wsalter3")

        # ---- 6. same-statement duplicate against a unique index -------------------
        _run(dsn, ["CREATE TABLE ss_dup (k int) USING pgcolumnar", "CREATE UNIQUE INDEX ss_dup_uidx ON ss_dup (k)"])
        err = _run(dsn, ["SET statement_timeout = '60s'", "INSERT INTO ss_dup SELECT 7 FROM generate_series(1,2)"])
        if err is not None and err.sqlstate == "23505":
            r["dup"] = "rejected"
        elif err is not None and err.sqlstate == "57014":
            r["dup"] = "TIMED OUT (uniqueness check did not terminate)"
        else:
            r["dup"] = query_error(f"not refused as a duplicate: {_message(err) if err is not None else 'it ran'}")
        r["dup rows"] = _value(dsn, "SELECT count(*) FROM ss_dup")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _partitioned(au, expect):
    """False after declining on a major that refuses a partitioned parent with an AM."""
    if "decline" in au:
        expect.cannot_run("UNSUPPORTED_MAJOR", au["decline"], name="the partitioned-parent arm")
        return False
    return True


# ---- 1 -----------------------------------------------------------------------------
def test_audit_add_column_nullable_is_null(au, expect):
    expect.text(au["ac null"], "5", "add-column nullable is null")


def test_audit_add_column_const_default(au, expect):
    expect.text(au["ac const"], "42", "add-column const default")


def test_audit_add_column_text_default(au, expect):
    expect.text(au["ac text"], "hi", "add-column text default")


def test_audit_add_column_projected_only(au, expect):
    expect.text(au["ac projected"], "42/hi", "add-column projected only")


def test_audit_add_column_mixed_rows(au, expect):
    expect.text(au["ac mixed"], "5:-:42:hi 6:7:8:new ", "add-column mixed rows")


def test_audit_add_column_aggregate(au, expect):
    expect.text(au["ac agg"], "218/6/1", "add-column aggregate")


def test_audit_the_add_column_lookup_reads_its_index(au, expect):
    expect.text(au["ac plan"], "yes", "premise: the add-column lookup reads ac_c_idx")


def test_audit_add_column_index_scan(au, expect):
    expect.text(au["ac index"], "5", "add-column index scan")


# ---- 2 -----------------------------------------------------------------------------
def test_audit_collation_pushdown_matches_no_pushdown(au, expect):
    expect.text(au["coll on"], au["coll off"], "collation pushdown == no-pushdown")


def test_audit_collation_result_correct(au, expect):
    expect.text(au["coll on"], "300", "collation result correct")


def test_audit_same_collation_still_pushes(au, expect):
    expect.text(au["pushed"], "1", "same-collation still pushes")


# ---- 3 -----------------------------------------------------------------------------
def test_audit_reject_chunk_group_row_limit_0(au, expect):
    expect.text(au["rej chunk"], "rejected", "reject chunk_group_row_limit 0")


def test_audit_reject_stripe_row_limit_5(au, expect):
    expect.text(au["rej stripe"], "rejected", "reject stripe_row_limit 5")


def test_audit_reject_compression_level_99(au, expect):
    expect.text(au["rej level"], "rejected", "reject compression_level 99")


def test_audit_reject_set_options_on_a_heap_table(au, expect):
    expect.text(au["rej heap"], "rejected", "reject set_options on a heap table")


def test_audit_the_refusal_says_not_columnar(au, expect):
    expect.text(au["heap says"], "yes", "the refusal says the relation is not columnar")


def test_audit_nothing_was_recorded_for_the_heap_table(au, expect):
    expect.text(au["heap recorded"], "0", "nothing was recorded for the heap table")


def test_audit_the_sqlstate_probe_reads_a_known_code(au, expect):
    expect.text(au["probe"], "22012", "premise: the SQLSTATE probe reads a known code")


def test_audit_set_options_on_a_heap_raises_42809(au, expect):
    expect.text(au["heap state"], "42809", "set_options on a non-columnar relation raises 42809, not P0001")


def test_audit_a_partitioned_parent_carries_the_am(au, expect):
    if _partitioned(au, expect):
        expect.text(au["part premise"], "p/pgcolumnar",
                    "premise: this major really lets a partitioned parent carry the AM")


def test_audit_reject_set_options_on_a_partitioned_parent(au, expect):
    if _partitioned(au, expect):
        expect.text(au["rej part"], "rejected", "reject set_options on a partitioned parent")


def test_audit_nothing_was_recorded_for_the_partitioned_parent(au, expect):
    if _partitioned(au, expect):
        expect.text(au["part recorded"], "0", "nothing was recorded for the partitioned parent")


def test_audit_the_partition_itself_is_still_accepted(au, expect):
    if _partitioned(au, expect):
        expect.text(au["leaf recorded"], "1", "the partition itself is still accepted")


def test_audit_valid_options_delete_works(au, expect):
    expect.text(au["opt delete"], "9", "valid options delete works")


def test_audit_the_same_call_on_a_columnar_table_is_recorded(au, expect):
    expect.text(au["opt recorded"], "1", "the same call on a columnar table is recorded")


# ---- 4 -----------------------------------------------------------------------------
def test_audit_the_parallel_create_index_completed(au, expect):
    expect.text(au["built"], "yes", "the parallel CREATE INDEX completed")


def test_audit_create_index_no_relation_leak(au, expect):
    expect.text(au["leaks"], "0", "create index no relation leak")


def test_audit_the_indexed_once_count_reads_its_index(au, expect):
    expect.text(au["idx plan"], "yes", "the indexed-once count reads idxleak_a_idx")


def test_audit_create_index_rows_indexed_once(au, expect):
    expect.text(au["idx once"], "50000", "create index rows indexed once")


# ---- 5 -----------------------------------------------------------------------------
def test_audit_add_column_mid_transaction_keeps_the_inserted_value(au, expect):
    expect.text(au["ws default"], "7,99", "add-column mid-transaction keeps the inserted value")


def test_audit_add_column_without_a_default_keeps_the_inserted_value(au, expect):
    expect.text(au["ws nodefault"], "1", "add-column without a default keeps the inserted value")


def test_audit_add_column_mid_transaction_keeps_a_later_update(au, expect):
    expect.text(au["ws update"], "55", "add-column mid-transaction keeps a later UPDATE")


# ---- 6 -----------------------------------------------------------------------------
def test_audit_same_statement_duplicate_is_rejected(au, expect):
    expect.text(au["dup"], "rejected", "same-statement duplicate is rejected, not spun on")


def test_audit_same_statement_duplicate_inserted_nothing(au, expect):
    expect.text(au["dup rows"], "0", "same-statement duplicate inserted nothing")

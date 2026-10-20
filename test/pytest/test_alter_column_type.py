"""ALTER TABLE ... ALTER COLUMN TYPE on a columnar table converts every value (#178). The
pytest twin of `test/alter_column_type.sh`, every check by the shell's name (#1341).

Phase 2 of ATRewriteTable updates pg_attribute before phase 3 scans the old relation,
so the relation's descriptor describes the new types while the bytes on disk are
still the old ones; core hands the scan a slot built from tab->oldDesc, and the scan
must decode against that. Each conversion is applied to a columnar table and to a heap
table of the same shape, and every row must match heap, every row must survive, and
the column must have the new type: an ALTER that failed and rolled back leaves a
widening such as int -> bigint equal to heap's converted values. The rewrite must also
leave the table readable through an index and an aggregate, and keep nulls on their
rows, and every row must still be reachable through the index, not one probe alone;
each index reading carries the access path, since enable_seqscan=off only prices a
seq scan up and with no index the planner still takes one.
The conversion that crashed the backend runs last, as in the shell.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). Each statement has a
connection of its own, as the shell's psql calls do. A failed query is a
`query_error()`.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_alter_column_type"
ROWS = 20000
# label, column type, value expression, alter clause: the shell's conv() calls, in order
CONV = [("int to bigint", "int", "g", "bigint"),
        ("bigint to int", "bigint", "g", "int"),
        ("smallint to int", "smallint", "(g % 1000)::smallint", "int"),
        ("int to smallint", "int", "(g % 1000)", "smallint"),
        ("float4 to float8", "float4", "(g * 1.5)::float4", "float8"),
        ("float8 to float4", "float8", "(g % 512)::float8", "float4"),
        ("date to timestamp", "date", "date '2024-01-01' + g", "timestamp"),
        ("int to numeric", "int", "g", "numeric"),
        ("int to text", "int", "g", "text"),
        ("bool to int", "bool", "(g % 2 = 0)", "int USING v::int"),
        ("numeric to text", "numeric", "g", "text"),
        ("text to varchar(64)", "text", "'s' || g", "varchar(64)"),
        ("text to bytea", "text", "'s' || g", "bytea USING v::bytea"),
        ("text rewritten by USING", "text", "'s' || g", "text USING upper(v)")]
CRASH = ("text to int (the crash)", "text", "g::text", "int USING v::int")
BROKEN = re.compile(r"corrupt encoded chunk[^\"]*|server closed|terminated", re.I)


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql, options=""):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=options) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


INDEX_OPTS = "-c enable_seqscan=off -c pgcolumnar.enable_custom_scan=off"
INDEX_PATH = re.compile(r"Index Scan using at_ix_i|Index Only Scan using at_ix_i|Bitmap Index Scan on at_ix_i")


def _via_index(dsn, sql):
    """-> "<answer> via at_ix_i", or "<answer> via no index": enable_seqscan=off only
    prices a seq scan up, and with no index the planner still takes one."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=INDEX_OPTS) as conn:
            plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS OFF) {sql}"))
    except psycopg.Error as exc:
        plan = query_error(_message(exc))
    path = "at_ix_i" if INDEX_PATH.search(plan) else "no index"
    return f"{_value(dsn, sql, options=INDEX_OPTS)} via {path}"


def _num(dsn, sql, options=""):
    v = _value(dsn, sql, options)
    return int(v) if v.lstrip("-").isdigit() else v if v else query_error(f"no value from {sql}")


def _run(dsn, sql):
    """-> the statement's error text, or "" when it ran: the shell's psql_run, whose
    failures show up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())  # simple protocol: one implicit transaction, as psql -c
    except psycopg.Error as exc:
        return str(exc)
    return ""


def _part(v):
    """One part of a composite reading: the value, or "x" when it was not read."""
    return "x" if not isinstance(v, int) or isinstance(v, bool) else str(v)


def _text(v):
    return v if v and not v.startswith("QUERY_ERROR") else ""


def _conv(dsn, typ, expr, clause):
    """The shell's conv(): -> "mismatch/count/type", or "failed: ..." when the columnar
    ALTER reported a corrupt chunk or lost the backend."""
    _run(dsn, f"DROP TABLE IF EXISTS at_c; CREATE TABLE at_c (id int, v {typ}) USING pgcolumnar; "
              f"INSERT INTO at_c SELECT g, {expr} FROM generate_series(1, {ROWS}) g;")
    _run(dsn, f"DROP TABLE IF EXISTS at_h; CREATE TABLE at_h (id int, v {typ}); "
              f"INSERT INTO at_h SELECT g, {expr} FROM generate_series(1, {ROWS}) g;")
    err = _run(dsn, f"ALTER TABLE at_c ALTER COLUMN v TYPE {clause};")
    _run(dsn, f"ALTER TABLE at_h ALTER COLUMN v TYPE {clause};")
    m = BROKEN.search(err)
    if m:
        return f"failed: {m.group(0)}"
    mismatch = _num(dsn, "SELECT count(*) FROM at_c a JOIN at_h b USING (id) WHERE a.v IS DISTINCT FROM b.v")
    count = _num(dsn, "SELECT count(*) FROM at_c")
    ctype = _text(_value(dsn, "SELECT atttypid::regtype::text FROM pg_attribute "
                              "WHERE attrelid = 'at_c'::regclass AND attname = 'v'"))
    return f"{_part(mismatch)}/{_part(count)}/{ctype or 'x'}"


def _want(dsn, clause):
    target = _text(_value(dsn, f"SELECT '{clause.split(' USING')[0]}'::regtype::text"))
    return f"0/{ROWS}/{target or 'the target type'}"


@pytest.fixture(scope="module")
def act(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {label: (_conv(dsn, typ, expr, clause), _want(dsn, clause))
             for label, typ, expr, clause in CONV}

        # the shape must survive too
        _run(dsn, f"DROP TABLE IF EXISTS at_ix; CREATE TABLE at_ix (id int, v int) USING pgcolumnar; "
                  f"INSERT INTO at_ix SELECT g, g * 2 FROM generate_series(1, {ROWS}) g; "
                  "CREATE INDEX at_ix_i ON at_ix (id);")
        _run(dsn, "ALTER TABLE at_ix ALTER COLUMN v TYPE bigint;")
        r["ix type"] = _value(dsn, "SELECT atttypid::regtype::text FROM pg_attribute "
                                   "WHERE attrelid = 'at_ix'::regclass AND attname = 'v'")
        r["ix probe"] = _via_index(dsn, f"SELECT v FROM at_ix WHERE id = {ROWS // 2}")
        r["ix every"] = _via_index(dsn, f"SELECT count(*) FROM at_ix WHERE id BETWEEN 1 AND {ROWS}")
        r["ix sum"] = _num(dsn, "SELECT sum(v) FROM at_ix")
        r["ix want"] = _num(dsn, f"SELECT sum(g * 2)::bigint FROM generate_series(1, {ROWS}) g")
        r["ix count"] = _value(dsn, "SELECT count(*) FROM at_ix")

        # a column with nulls
        _run(dsn, "DROP TABLE IF EXISTS at_n; DROP TABLE IF EXISTS at_nh; "
                  "CREATE TABLE at_n (id int, v int) USING pgcolumnar; "
                  "INSERT INTO at_n SELECT g, CASE WHEN g % 3 = 0 THEN NULL ELSE g END "
                  f"FROM generate_series(1, {ROWS}) g;")
        _run(dsn, "CREATE TABLE at_nh (id int, v int); "
                  "INSERT INTO at_nh SELECT g, CASE WHEN g % 3 = 0 THEN NULL ELSE g END "
                  f"FROM generate_series(1, {ROWS}) g;")
        _run(dsn, "ALTER TABLE at_n ALTER COLUMN v TYPE bigint;")
        _run(dsn, "ALTER TABLE at_nh ALTER COLUMN v TYPE bigint;")
        r["n type"] = _value(dsn, "SELECT atttypid::regtype::text FROM pg_attribute "
                                  "WHERE attrelid = 'at_n'::regclass AND attname = 'v'")
        mismatch = _num(dsn, "SELECT count(*) FROM at_n a JOIN at_nh b USING (id) WHERE a.v IS DISTINCT FROM b.v")
        r["n rows"] = f"{_part(mismatch)}/{_part(_num(dsn, 'SELECT count(*) FROM at_n'))}"

        # the crash, deliberately last
        label, typ, expr, clause = CRASH
        r[label] = (_conv(dsn, typ, expr, clause), _want(dsn, clause))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_int_to_bigint(act, expect):
    expect.text(*act["int to bigint"], "int to bigint")


def test_bigint_to_int(act, expect):
    expect.text(*act["bigint to int"], "bigint to int")


def test_smallint_to_int(act, expect):
    expect.text(*act["smallint to int"], "smallint to int")


def test_int_to_smallint(act, expect):
    expect.text(*act["int to smallint"], "int to smallint")


def test_float4_to_float8(act, expect):
    expect.text(*act["float4 to float8"], "float4 to float8")


def test_float8_to_float4(act, expect):
    expect.text(*act["float8 to float4"], "float8 to float4")


def test_date_to_timestamp(act, expect):
    expect.text(*act["date to timestamp"], "date to timestamp")


def test_int_to_numeric(act, expect):
    expect.text(*act["int to numeric"], "int to numeric")


def test_int_to_text(act, expect):
    expect.text(*act["int to text"], "int to text")


def test_bool_to_int(act, expect):
    expect.text(*act["bool to int"], "bool to int")


def test_numeric_to_text(act, expect):
    expect.text(*act["numeric to text"], "numeric to text")


def test_text_to_varchar_64(act, expect):
    expect.text(*act["text to varchar(64)"], "text to varchar(64)")


def test_text_to_bytea(act, expect):
    expect.text(*act["text to bytea"], "text to bytea")


def test_text_rewritten_by_using(act, expect):
    expect.text(*act["text rewritten by USING"], "text rewritten by USING")


def test_the_column_really_is_bigint_now(act, expect):
    expect.text(act["ix type"], "bigint", "the column really is bigint now")


def test_an_index_scan_still_finds_a_row_after_the_rewrite(act, expect):
    expect.text(act["ix probe"], f"{ROWS} via at_ix_i", "an index scan still finds a row after the rewrite")


def test_an_index_scan_finds_every_row_after_the_rewrite(act, expect):
    expect.text(act["ix every"], f"{ROWS} via at_ix_i", "an index scan finds every row after the rewrite")


def test_the_aggregate_over_the_converted_column_is_right(act, expect):
    expect.num(act["ix sum"], act["ix want"], "the aggregate over the converted column is right")


def test_every_row_survived_the_rewrite(act, expect):
    expect.text(act["ix count"], str(ROWS), "every row survived the rewrite")


def test_the_nullable_column_really_is_bigint_now(act, expect):
    expect.text(act["n type"], "bigint", "the nullable column really is bigint now")


def test_nulls_stay_on_the_same_rows_through_a_conversion(act, expect):
    expect.text(act["n rows"], f"0/{ROWS}", "nulls stay on the same rows through a conversion")


def test_text_to_int_the_crash(act, expect):
    expect.text(*act["text to int (the crash)"], "text to int (the crash)")

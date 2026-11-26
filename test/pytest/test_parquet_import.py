"""The pytest twin of `test/parquet_import.sh`, every check by the shell's name (#432).

pgcolumnar.import_parquet(rel, path) reads a Parquet file itself (Thrift metadata,
Snappy, PLAIN and dictionary encodings, DATA_PAGE v1 and v2) and inserts its rows. The
file is written here with pyarrow from data this module generates, and the same data is
loaded into a heap table by COPY FROM STDIN, which is the oracle: nine types, nulls in
five of them, a dictionary-encoded snappy file per page version, and an uncompressed
PLAIN one. A file that is not Parquet and a target with a column the file lacks are
refused, each with its own SQLSTATE and message.

The importer inserts through table_tuple_insert, which maintains no index, so it keeps
the indexes itself (#153). Its arms compare an index scan with a sequential scan on one
predicate, assert that the forced plan is an index scan (enable_seqscan alone leaves the
custom scan cheapest), and require a unique index to refuse a second import of the same
keys and a partial index to hold the rows its predicate covers.

The module fixture builds and measures everything once, in the shell's order, in a
private database; each check is its own test, so one red does not hide the next.

import_parquet refuses a partitioned columnar parent rather than segfaulting (#1263,
#1341). It never asked whether its target had storage. Handed a partitioned parent,
relam pgcolumnar and relfilenode 0, it dereferenced its way to SIGSEGV, on a release
build too. The file must exist before the call, or the import fails at the OPEN and
never reaches the relation. A heap target must still import: the guard is about
storage, not about being columnar. A refusal is 42809, and the crash is read from the
server log, since a dropped connection is an error too and a later one succeeds.

THE PARQUET EXAMPLES IN THE DOCS RUN AS WRITTEN (#2093). The how-to's read_parquet
declared 3 of the file's columns, and read_parquet needs every one; the SQL
reference's round trip, run on the user guide's `events` with a bare numeric amount,
was refused at the import. The blocks are read from the pages by this file's own
reader and run through psycopg, with their /data/ and /tmp/ paths moved into a
directory the server can write. They need no pyarrow, so they have their own fixture.
"""
import datetime
import pathlib
import re
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parquet_import"
N = 20000
SCHEMA = ("id int, big bigint, f4 real, f8 double precision, s text, bl bool, dt date, "
          "ts timestamp, by bytea")
SEL = "SELECT id, big, f4, f8, s, bl, dt, ts, encode(by, 'hex') FROM"
# the forced index scan: enable_seqscan does not discourage a custom scan, so the
# columnar path is taken out of the running by its own setting
IDX = ("SET enable_seqscan = off", "SET pgcolumnar.enable_custom_scan = off")
SEQ = ("SET enable_indexscan = off", "SET enable_bitmapscan = off", "SET enable_indexonlyscan = off")


def _first_line(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


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


def _one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _sqlstate(conn, sql, params=None):
    """-> 'accepted', or the SQLSTATE the statement raised."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        return "accepted"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no SQLSTATE ({str(exc).splitlines()[0]})"


def _refusal(conn, sql, params=None):
    """-> 'accepted', or 'SQLSTATE: primary message' of the error the statement raised,
    the shell's reading of psql's verbose ERROR line."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        return "accepted"
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return f"no SQLSTATE ({_first_line(exc)})"
        return f"{exc.sqlstate}: {exc.diag.message_primary}"


def _q(conn, sql, params=None, settings=()):
    """-> the first column of the first row, or a failed reading. `settings` are SET
    for this statement only."""
    try:
        with conn.cursor() as cur:
            for s in settings:
                cur.execute(s)
            cur.execute(sql, params)
            row = cur.fetchone()
            cur.execute("RESET ALL")
    except psycopg.Error as exc:
        return query_error(_first_line(exc))
    return row[0] if row else query_error("no row")


def _rows(conn, sql, settings=()):
    try:
        with conn.cursor() as cur:
            for s in settings:
                cur.execute(s)
            cur.execute(sql)
            rows = cur.fetchall()
            cur.execute("RESET ALL")
            return rows
    except psycopg.Error as exc:
        return query_error(_first_line(exc))


def _data():
    """-> the rows, as Python values in the table's column order."""
    base, bts = datetime.date(2020, 1, 1), datetime.datetime(2020, 1, 1)
    return [(g,
             None if g % 11 == 0 else g * 100000,
             g * 0.5,                      # exact in binary32
             g * 0.25,
             None if g % 13 == 0 else f"v{g}",
             None if g % 7 == 0 else g % 2 == 0,
             base + datetime.timedelta(days=g),
             bts + datetime.timedelta(seconds=g),
             None if g % 17 == 0 else bytes([g & 0xff, (g >> 8) & 0xff]))
            for g in range(1, N + 1)]


def _write_parquet(pa, pq, rows, path, page_version):
    cols = list(zip(*rows))
    types = [pa.int32(), pa.int64(), pa.float32(), pa.float64(), pa.string(), pa.bool_(),
             pa.date32(), pa.timestamp("us"), pa.binary()]
    names = ["id", "big", "f4", "f8", "s", "bl", "dt", "ts", "by"]
    t = pa.table({n: pa.array(list(c), ty) for n, c, ty in zip(names, cols, types)})
    pq.write_table(t, path, compression="snappy", use_dictionary=True,
                   data_page_version=page_version)


# THE FILES ARE WRITTEN IN A SUBPROCESS, as test_native_parquet_units.py writes its
# own, so this module imports no pyarrow: every test declines through
# expect.require_module before it reads the fixture (#1286 review). The writer loads
# this file for _data and _write_parquet, so the rows the files hold and the rows the
# heap oracle is loaded with have one definition.
WRITER = r'''
import importlib.util, sys
import pyarrow as pa, pyarrow.parquet as pq
here, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, str(__import__("pathlib").Path(here).parent))
spec = importlib.util.spec_from_file_location("pqi_source", here)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
rows = m._data()
for pv in ("1.0", "2.0"):
    m._write_parquet(pa, pq, rows, f"{out}/d_{pv}.parquet", pv)
# uncompressed, no dictionary: the PLAIN pages
pq.write_table(pa.table({"id": pa.array(list(range(1, 5001)), pa.int32()),
                         "s": pa.array([f"x{g}" for g in range(1, 5001)], pa.string())}),
               f"{out}/plain.parquet", compression="none", use_dictionary=False)
'''


@pytest.fixture(scope="module")
def pqi(pgc_cluster, server_dir):
    w = subprocess.run([sys.executable, "-c", WRITER, str(pathlib.Path(__file__).resolve()),
                        str(server_dir)], capture_output=True, text=True)
    if w.returncode != 0:
        print(f"-- parquet writer failed: {(w.stderr or '').strip().splitlines()[-1:]}")
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    r = {}
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=DB), autocommit=True) as conn:
            def build(*stmts):
                try:
                    for sql in stmts:
                        conn.execute(sql)
                except psycopg.Error as exc:
                    print(f"-- fixture build failed: {_first_line(exc)}")

            build("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            rows = _data()
            for label, pv in (("v1", "1.0"), ("v2", "2.0")):
                path = str(server_dir / f"d_{pv}.parquet")
                build("DROP TABLE IF EXISTS pc", "DROP TABLE IF EXISTS po",
                      f"CREATE TABLE pc ({SCHEMA}) USING pgcolumnar",
                      f"CREATE TABLE po ({SCHEMA}) USING heap")
                try:
                    with conn.cursor() as cur, cur.copy("COPY po FROM STDIN") as cp:
                        for row in rows:
                            cp.write_row(row)
                except psycopg.Error as exc:
                    print(f"-- oracle load failed: {_first_line(exc)}")
                r[label] = {
                    "imported": _q(conn, "SELECT pgcolumnar.import_parquet('pc', %s)", (path,)),
                    "counts": (_q(conn, "SELECT count(*) FROM pc"), _q(conn, "SELECT count(*) FROM po")),
                    "contents": (_rows(conn, f"{SEL} pc"), _rows(conn, f"{SEL} po")),
                }

            # uncompressed, no dictionary: the PLAIN pages, written by WRITER
            plain = str(server_dir / "plain.parquet")
            build("CREATE TABLE pp (id int, s text) USING pgcolumnar")
            r["plain_rows"] = _q(conn, "SELECT pgcolumnar.import_parquet('pp', %s)", (plain,))
            r["plain_sum"] = _q(conn, "SELECT sum(id)::bigint FROM pp")
            s42 = _rows(conn, "SELECT s FROM pp WHERE id = 42")
            # no row is a reading to fail on, not a failed one
            r["plain_s"] = s42 if isinstance(s42, str) else (s42[0][0] if s42 else "(no row)")

            # argument validation
            notpq = server_dir / "not_parquet.txt"
            notpq.write_text("this file is plain text and carries no PAR1 magic\n")
            r["notpq_path"] = str(notpq)
            r["notpq"] = _refusal(conn, "SELECT pgcolumnar.import_parquet('pp', %s)", (str(notpq),))
            build("CREATE TABLE pmismatch (id int, s text, extra int) USING pgcolumnar")
            r["mismatch"] = _refusal(conn, "SELECT pgcolumnar.import_parquet('pmismatch', %s)", (plain,))

            # import into a table that already has indexes (#153)
            ix = str(server_dir / "ix_roundtrip.parquet")
            build("CREATE TABLE ix_src (id int, v text) USING pgcolumnar",
                  "INSERT INTO ix_src SELECT g, 'v' || g FROM generate_series(1, 3000) g")
            build(f"SELECT pgcolumnar.export_parquet('ix_src', '{ix}')")
            build("CREATE TABLE ix_tgt (id int, v text) USING pgcolumnar",
                  "CREATE INDEX ix_tgt_id ON ix_tgt (id)")
            build(f"SELECT pgcolumnar.import_parquet('ix_tgt', '{ix}')")
            pred = "SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199"
            plan = _rows(conn, f"EXPLAIN (COSTS OFF) {pred}", IDX)
            r["idx_plan"] = plan if isinstance(plan, str) else (
                "yes" if any("index" in p[0].lower() and "scan" in p[0].lower() for p in plan)
                else "no: " + " / ".join(p[0].strip() for p in plan))
            r["idx_count"] = _q(conn, pred, settings=IDX)
            r["seq_count"] = _q(conn, pred, settings=SEQ)
            r["point"] = _q(conn, "SELECT count(*) FROM ix_tgt WHERE id = 2222", settings=IDX)

            # a unique index must police the second import rather than take it
            build("CREATE TABLE ix_uq (id int, v text) USING pgcolumnar",
                  "CREATE UNIQUE INDEX ix_uq_id ON ix_uq (id)")
            build(f"SELECT pgcolumnar.import_parquet('ix_uq', '{ix}')")
            r["unique"] = _refusal(conn, "SELECT pgcolumnar.import_parquet('ix_uq', %s)", (ix,))
            r["unique_count"] = _q(conn, "SELECT count(*) FROM ix_uq")

            # a partial index must receive only the rows its predicate covers
            build("CREATE TABLE ix_part (id int, v text) USING pgcolumnar",
                  "CREATE INDEX ix_part_id ON ix_part (id) WHERE id <= 500")
            build(f"SELECT pgcolumnar.import_parquet('ix_part', '{ix}')")
            r["partial"] = _q(conn, "SELECT count(*) FROM ix_part WHERE id BETWEEN 100 AND 199",
                              settings=IDX)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


@pytest.mark.parametrize("label", ["v1", "v2"])
def test_every_row_is_imported(pqi, label, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi[label]["imported"], N, f"{label} rows imported")


@pytest.mark.parametrize("label", ["v1", "v2"])
def test_the_row_count_matches_the_oracle(pqi, label, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    got, want = pqi[label]["counts"]
    expect.num(got, want, f"{label} row count matches oracle")


@pytest.mark.parametrize("label", ["v1", "v2"])
def test_the_contents_match_the_heap_oracle(pqi, label, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    got, want = pqi[label]["contents"]
    expect.row_set(got, want, f"{label} contents match heap oracle")


def test_a_plain_file_imports_every_row(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi["plain_rows"], 5000, "plain import rows")


def test_a_plain_file_imports_its_ids(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi["plain_sum"], 12502500, "plain import sum(id)")


def test_a_plain_file_imports_its_text(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.text(pqi["plain_s"], "x42", "plain import first s")


def test_a_file_that_is_not_parquet_is_refused_by_import(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.text(pqi["notpq"], f'XX001: "{pqi["notpq_path"]}" is not a Parquet file (bad magic)',
                "reject non-parquet file")


def test_a_target_with_a_column_the_file_lacks_is_refused(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.text(pqi["mismatch"], "42804: Parquet file has fewer columns than the target table",
                "reject column-count mismatch")


def test_the_index_scan_check_uses_the_index(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.text(pqi["idx_plan"], "yes", "the index-scan check really uses the index")


def test_the_index_and_sequential_scans_agree(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi["idx_count"], pqi["seq_count"], "import into an indexed table: both scans agree")


def test_the_imported_rows_are_there(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi["seq_count"], 100, "import into an indexed table: the rows are there")


def test_an_index_point_lookup_finds_its_row(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi["point"], 1, "import into an indexed table: a point lookup finds its row")


def test_a_unique_index_refuses_the_second_import(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.text(pqi["unique"], '23505: duplicate key value violates unique constraint "ix_uq_id"',
                "importing the same keys twice raises unique_violation")


def test_the_refused_import_left_the_row_count_alone(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi["unique_count"], 3000, "the failed second import left the row count alone")


def test_a_partial_index_covers_its_rows(pqi, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet import verification") is None:
        return
    expect.num(pqi["partial"], 100, "partial index covers its rows after import")


def test_a_partitioned_parent_is_refused(pgc_conn, pgc_cluster, server_dir, expect):
    conn = pgc_conn
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    if major < 17:
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"a partitioned table cannot carry an access method before PostgreSQL 17; this server is {major}",
                          name="importing into the partitioned parent is refused")
        return
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE pq_parent (id int, v text) PARTITION BY RANGE (id)")
        cur.execute("ALTER TABLE pq_parent SET ACCESS METHOD pgcolumnar")
        cur.execute("CREATE TABLE pq_leaf PARTITION OF pq_parent FOR VALUES FROM (0) TO (100)")
        cur.execute("INSERT INTO pq_parent SELECT g, 'r' || g FROM generate_series(0, 49) g")
    expect.text(_one(conn, "SELECT c.relkind::text || am.amname::text FROM pg_class c "
                           "JOIN pg_am am ON am.oid = c.relam WHERE c.oid = 'pq_parent'::regclass"),
                "ppgcolumnar", "premise: the parent is partitioned and carries the columnar access method")
    expect.num(_one(conn, "SELECT relfilenode::int FROM pg_class WHERE oid = 'pq_parent'::regclass"), 0,
               "premise: so it has no storage of its own")

    path = str(server_dir / "pq_leaf.parquet")
    _sqlstate(conn, "SELECT pgcolumnar.export_parquet('pq_leaf', %s)", (path,))
    exists = (server_dir / "pq_leaf.parquet").exists() and (server_dir / "pq_leaf.parquet").stat().st_size > 0
    expect.text("yes" if exists else "no", "yes",
                "premise: a parquet file exists, so the import reaches the relation")

    expect.num(_one(conn, "SELECT pgcolumnar.import_parquet('pq_leaf', %s)", (path,)), 50,
               "control: importing that file into the LEAF still works")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE pq_heap (id int, v text)")
    expect.num(_one(conn, "SELECT pgcolumnar.import_parquet('pq_heap', %s)", (path,)), 50,
               "control: a HEAP target still imports, so the guard is about storage")

    crashes = _crashes(pgc_cluster)
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.import_parquet('pq_parent', %s)", (path,)), "42809",
                "importing into the partitioned parent is refused")
    alive = _sqlstate(conn, "SELECT 1") == "accepted"
    died = _since(pgc_cluster, crashes)
    expect.text(f"{'1' if alive else ''}|{died} backend crashes" if isinstance(died, int) else died, "1|0 backend crashes",
                "and the cluster is still up, so the refusal was not an abort")


# ---- the documents' Parquet examples (#2093) -------------------------------------------
DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs"
DOCS_DB = "pgc_parquet_import_docs"
DOC_ROWS = ("INSERT INTO events (id, ts, amount) "
            "SELECT g, timestamptz '2026-01-01 00:00+00' + g * interval '1 minute', g / 100.0 "
            "FROM generate_series(1, 5000) g")


def _doc_sql(page, heading, n):
    """-> the nth ```sql block in the section of `page` whose heading starts with
    `heading`, or None."""
    lines = (DOCS / page).read_text().split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return None
    k, fence, body = 0, None, []
    for line in lines[start + 1:]:
        if line.startswith("```"):
            if fence is None:
                fence = line[3:].strip()
                k += fence == "sql"
                continue
            if fence == "sql" and k == n:
                return "\n".join(body)
            fence = None
            continue
        if fence is None and line.startswith("#"):
            return None
        if fence == "sql" and k == n:
            body.append(line)
    return None


def _statements(sql):
    """-> the statements of a doc block, one per string, as psql sends them. One
    multi-statement string would run as one implicit transaction, where VACUUM fails."""
    out = []
    for part in re.split(r";[ \t]*(?:--[^\n]*)?(?:\n|$)", sql):
        if re.sub(r"--[^\n]*", "", part).strip():
            out.append(part)
    return out


@pytest.fixture(scope="module")
def pq_docs(pgc_cluster, server_dir):
    base = server_dir / "docs"
    (base / "events").mkdir(parents=True, exist_ok=True)
    for d in (base, base / "events"):
        d.chmod(0o777)

    def run(conn, page, heading, n):
        sql = _doc_sql(page, heading, n)
        if not sql:
            return f"no block {n} under '{heading}' in {page}"
        sql = re.sub(r"'(?:/(?:data|tmp)/|s3://bucket/)", "'" + str(base) + "/", sql)
        try:
            for stmt in _statements(sql):
                conn.execute(stmt)
        except psycopg.Error as exc:
            return query_error(_first_line(exc))
        return "ok"

    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DOCS_DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DOCS_DB}")
    r = {}
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=DOCS_DB), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION pgcolumnar")
            r["howto create"] = run(conn, "how-to.md", "## Create a columnar table", 1)
            conn.execute(DOC_ROWS)
            r["howto export"] = run(conn, "how-to.md", "## Read and write Parquet", 2)
            r["howto read"] = run(conn, "how-to.md", "## Read and write Parquet", 1)
            # s3://bucket/ is pointed at the same local directory (mutant M1).
            r["howto s3 read"] = run(conn, "how-to.md", "## Use object storage", 1)
            conn.execute("DROP TABLE events CASCADE")
            r["ug create"] = run(conn, "user-guide.md", "## Create a columnar table", 1)
            conn.execute(DOC_ROWS)
            conn.execute("SELECT pgcolumnar.export_parquet('events', %s)",
                         (str(base / "events" / "part-0.parquet"),))
            r["sr round trip"] = run(conn, "sql-reference.md", "### pgcolumnar.import_parquet(", 1)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DOCS_DB} WITH (FORCE)")


def test_the_how_to_creates_its_events_table_for_parquet(pq_docs, expect):
    expect.text(pq_docs["howto create"], "ok", "the how-to's first example creates its events table")


def test_the_how_to_parquet_export_runs(pq_docs, expect):
    expect.text(pq_docs["howto export"], "ok", "the how-to's Parquet export example runs on that events table")


def test_the_how_to_parquet_read_runs(pq_docs, expect):
    expect.text(pq_docs["howto read"], "ok", "the how-to's Parquet read example runs on the file that export wrote")


def test_the_how_to_object_storage_read_runs_on_that_file(pq_docs, expect):
    expect.text(pq_docs["howto s3 read"], "ok", "the how-to's object-storage read_parquet example runs on that file")


def test_the_user_guide_creates_its_events_table(pq_docs, expect):
    expect.text(pq_docs["ug create"], "ok", "the user guide's first example creates its events table")


def test_the_sql_reference_round_trip_runs(pq_docs, expect):
    expect.text(pq_docs["sr round trip"], "ok",
                "the SQL reference's Parquet round trip runs on the user guide's events table")


def _projection_import_case(pgc_cluster, server_dir, schema, opt, proj):
    """-> 'oOPTpPROJ:imported/rows/projection rows' for one case of #2092's matrix.

    Every statement runs on a fresh connection, because a crash ends every backend,
    the fixture's own included. Each case has its own table."""
    import time
    import psycopg

    def connect():
        c = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
        c.execute(f'SET search_path TO "{schema}", public')
        return c

    t = f"pqp_o{opt}p{proj}"
    path = server_dir / f"{t}.parquet"
    with connect() as c:
        c.execute(f"CREATE TABLE {t} (id bigint, c int) USING pgcolumnar")
        c.execute(f"INSERT INTO {t} SELECT g, g % 10 FROM generate_series(1, 1000) g")
        if opt:
            c.execute(f"SELECT pgcolumnar.set_options('{t}', compression => 'zstd')")
        if proj:
            c.execute(f"SELECT pgcolumnar.add_projection('{t}', 'p', columns => ARRAY['c','id'],"
                      " sort_key => ARRAY['c'])")
        c.execute(f"SELECT pgcolumnar.export_parquet('{t}', '{path}')")
    try:
        with connect() as c:
            got = str(c.execute(f"SELECT pgcolumnar.import_parquet('{t}', '{path}')").fetchone()[0])
    except psycopg.Error as e:
        got = e.sqlstate or "lost connection"
    # after a crash the server needs a moment of recovery before it answers
    for _ in range(60):
        try:
            with connect() as c:
                rows = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                prows = (c.execute(f"SELECT count(*) FROM pgcolumnar.read_projection('{t}', 'p')").fetchone()[0]
                         if proj else "-")
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else:
        rows = prows = "server down"
    return f"o{opt}p{proj}:{got}/{rows}/{prows}"


def test_a_parquet_import_into_a_table_with_a_projection_keeps_the_server_up(pgc_conn, pgc_cluster, server_dir,
                                                                      expect):
    """#2092: the import writes the projection's rows when the statement ends. The
    import is a SELECT, so its statement ends in portal cleanup, where no snapshot is
    active. The flush then read the options catalog with a snapshot nobody had
    registered. An assert build aborted on the first catalog row the read examined, and
    the postmaster restarted every backend.

    Any table's options row is enough, so the test gives one to a neighbour table first.
    Without it, the case without set_options passed or failed on main's code depending
    on what earlier tests had left in the catalog. The matrix crosses projection with
    set_options; the crash count comes from the server log, not from timing."""
    import psycopg

    schema = _one(pgc_conn, "SELECT current_schema()")
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        c.execute(f'SET search_path TO "{schema}", public')
        c.execute("CREATE TABLE pqp_neighbour (id int) USING pgcolumnar")
        c.execute("SELECT pgcolumnar.set_options('pqp_neighbour', compression => 'zstd')")
    before = _crashes(pgc_cluster)
    got = " ".join(_projection_import_case(pgc_cluster, server_dir, schema, opt, proj)
                   for opt in (0, 1) for proj in (0, 1))
    now = _crashes(pgc_cluster)
    got += (f" | {now - before} backend crashes" if isinstance(before, int) and isinstance(now, int)
            else " | crash count unreadable")
    expect.text(got, "o0p0:1000/2000/- o0p1:1000/2000/2000 o1p0:1000/2000/- o1p1:1000/2000/2000"
                     " | 0 backend crashes",
                "import_parquet into a table with a projection, with or without set_options, keeps the server up"
                " and fills the projection (#2092)")

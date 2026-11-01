"""pgcolumnar splits a text file into record-aligned ranges and loads it in parallel,
atomically. The pytest twin of `test/parallel_copy.sh`, every check by the shell's
name (#1341).

`pgcolumnar.file_split_offsets` must cut a file into ranges that start right after a
newline, cover the file, and, on a file with far more records than workers, are all
non-empty; loading the ranges one by one must give back the rows the file was written
from. `pgcolumnar.parallel_copy` must load a partitioned or a single columnar target
with the rows a single COPY gives, leave no prepared transaction behind, roll every
worker back when one fails, and refuse what it cannot load: a text partition key, an
unsorted file, a DEFAULT partition, more workers than prepared-transaction slots, a
missing file, a heap target, and a FIFO, which it must refuse rather than hang on.
Both functions read local files only, so both refuse an object-storage URL with
0A000, naming themselves and the URL, before they look at the target (#1886).

THREE FIXES THE SHELL SHARES. The per-range loads are compared with the generator
that wrote the file, not with a single columnar COPY of it, which shares every defect
of the COPY path. A failed load must carry the loader's own message, not any message
with "failed" in it. And a backend that died does not pass as one that survived: a
fresh connection answers after crash recovery, so survival also reads the server log's
count of killed backends.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture. Prepared
transactions are counted in that database only, so another file's leftover cannot
redden this one.
"""
import os
import random
import threading

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parallel_copy"
GEN_F = "SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt FROM generate_series(1, {n}) g"
GEN_BIG = "SELECT g AS id, 'h'||g||'_'||repeat('x', 70000) AS txt FROM generate_series(1, 20) g"
GEN_NONL = "SELECT g AS id, 'r'||g AS txt FROM generate_series(1,1000) g"
LEAK = "SELECT count(*) FROM pg_prepared_xacts WHERE database = current_database()"
REMOTE_S3 = "s3://pgc-test/f1886/data.txt"
REMOTE_HTTP = "http://127.0.0.1:9/f1886/data.txt"
REMOTE_OTHER = ("https://127.0.0.1:9/f1886/data.txt", "gs://pgc-test/f1886/data.txt",
                "az://pgc-test/f1886/data.txt", "S3://pgc-test/f1886/data.txt")

# (reading key, the shell's check name)
STRUCTURE = (
    ("5000/1", "5000 rows / 1: offsets well-formed and newline-aligned"),
    ("5000/2", "5000 rows / 2: offsets well-formed and newline-aligned"),
    ("5000/3", "5000 rows / 3: offsets well-formed and newline-aligned"),
    ("5000/8", "5000 rows / 8: offsets well-formed and newline-aligned"),
    ("5000/16", "5000 rows / 16: offsets well-formed and newline-aligned"),
    ("big/4", "long lines >64kB / 4 workers: offsets well-formed and newline-aligned"),
    ("tiny/8", "5 rows / 8 workers: offsets well-formed and newline-aligned"),
    ("one/4", "1 row / 4 workers (edge smoke): offsets well-formed and newline-aligned"),
    ("nonl/4", "no trailing newline / 4 workers: offsets well-formed and newline-aligned"),
)
RECONSTRUCT = (
    ("5000/1", "5000 rows / 1: per-range loads reconstruct the whole file"),
    ("5000/2", "5000 rows / 2: per-range loads reconstruct the whole file"),
    ("5000/3", "5000 rows / 3: per-range loads reconstruct the whole file"),
    ("5000/8", "5000 rows / 8: per-range loads reconstruct the whole file"),
    ("5000/16", "5000 rows / 16: per-range loads reconstruct the whole file"),
    ("big/4", "long lines >64kB / 4 workers: per-range loads reconstruct the whole file"),
    ("tiny/8", "5 rows / 8 workers (empty ranges): per-range loads reconstruct the whole file"),
    ("one/4", "1 row / 4 workers (edge smoke): per-range loads reconstruct the whole file"),
    ("nonl/4", "no trailing newline / 4 workers: per-range loads reconstruct the whole file"),
)
SPLIT_REAL = (
    ("5000/2", "5000 rows / 2: split is real (interior offsets distinct, ranges non-empty)"),
    ("5000/3", "5000 rows / 3: split is real (interior offsets distinct, ranges non-empty)"),
    ("5000/8", "5000 rows / 8: split is real (interior offsets distinct, ranges non-empty)"),
    ("5000/16", "5000 rows / 16: split is real (interior offsets distinct, ranges non-empty)"),
    ("big/4", "long lines >64kB / 4 workers: split is real (interior offsets distinct, ranges non-empty)"),
)
LOAD_ROWS = (
    ("partitioned/1", "parallel_copy(partitioned, 1 workers): rows returned = 5000"),
    ("partitioned/2", "parallel_copy(partitioned, 2 workers): rows returned = 5000"),
    ("partitioned/4", "parallel_copy(partitioned, 4 workers): rows returned = 5000"),
    ("single/1", "parallel_copy(single table, 1 workers): rows returned = 5000"),
    ("single/2", "parallel_copy(single table, 2 workers): rows returned = 5000"),
    ("single/4", "parallel_copy(single table, 4 workers): rows returned = 5000"),
)
LOAD_ORACLE = (
    ("partitioned/1", "parallel_copy(partitioned, 1 workers): result == single-COPY oracle"),
    ("partitioned/2", "parallel_copy(partitioned, 2 workers): result == single-COPY oracle"),
    ("partitioned/4", "parallel_copy(partitioned, 4 workers): result == single-COPY oracle"),
    ("single/1", "parallel_copy(single table, 1 workers): result == single-COPY oracle"),
    ("single/2", "parallel_copy(single table, 2 workers): result == single-COPY oracle"),
    ("single/4", "parallel_copy(single table, 4 workers): result == single-COPY oracle"),
)
LOAD_LEAK = (
    ("partitioned/1", "parallel_copy(partitioned, 1 workers): no prepared-transaction leak"),
    ("partitioned/2", "parallel_copy(partitioned, 2 workers): no prepared-transaction leak"),
    ("partitioned/4", "parallel_copy(partitioned, 4 workers): no prepared-transaction leak"),
    ("single/1", "parallel_copy(single table, 1 workers): no prepared-transaction leak"),
    ("single/2", "parallel_copy(single table, 2 workers): no prepared-transaction leak"),
    ("single/4", "parallel_copy(single table, 4 workers): no prepared-transaction leak"),
)


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _want_refusal(fn, url):
    """The refusal #1886 promises for `fn` given `url`."""
    return f'0A000 columnar: {fn} does not read "{url}" | {fn} reads local files only. Use a local filesystem path.'


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        """A setup step. A failure is printed; the readings that need the step fail."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {_message(exc)}")

    def value(self, sql):
        """-> the first column of the first row, or the failure as a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(_message(exc))
        if row is None or row[0] is None:
            return query_error(f"no value from {sql}")
        return row[0]

    def set_hash(self, sql):
        """lib.sh's pgc_set_hash: md5 over the sorted rendered rows, EMPTY for none."""
        return self.value("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                          f"FROM (SELECT _row::text AS t FROM ( {sql} ) _row) _s")

    def refused(self, sql, phrase, *alternatives):
        """-> "refused: <phrase>" when `sql` raised with `phrase`, or one of the
        alternatives, in its message (any case); the message when it has none of them;
        "noerror" when it did not raise; a query_error() when the server never answered."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql).fetchall()
        except psycopg.Error as exc:
            if not exc.sqlstate:
                return query_error(_message(exc))
            said = str(exc).lower()
            hit = any(p.lower() in said for p in (phrase,) + alternatives)
            return f"refused: {phrase}" if hit else str(exc)
        return "noerror"

    def remote_refusal(self, sql):
        """-> "SQLSTATE message | hint" of the error `sql` raised, or "no error (...)"."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            if not exc.sqlstate:
                return query_error(_message(exc))
            return f"{exc.sqlstate} {exc.diag.message_primary} | {exc.diag.message_hint or 'no hint'}"
        return f"no error ({row[0] if row else None})"

    def copy_in(self, table, data):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                with conn.cursor().copy(f"COPY {table} FROM STDIN WITH (FORMAT text)") as cp:
                    cp.write(data)
        except psycopg.Error as exc:
            print(f"-- copy into {table} failed: {_message(exc)}")

    def offsets(self, path, workers):
        return self.value(f"SELECT pgcolumnar.file_split_offsets('{path}', {workers})")

    def sqlstate_or_hang(self, sql, fifo):
        """-> the SQLSTATE `sql` raised, "HANG" when it did not return in 5 s, or
        "noerror". A hang is released by opening the FIFO for writing, as the shell does."""
        out = {}

        def go():
            try:
                with psycopg.connect(self.dsn, autocommit=True) as conn:
                    conn.execute(sql).fetchall()
                out["r"] = "noerror"
            except psycopg.Error as exc:
                out["r"] = exc.sqlstate or query_error(_message(exc))

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(5)
        if t.is_alive():
            try:
                fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass
            t.join(10)
            return "HANG"
        return out["r"]


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(db, cluster, before):
    up, now = db.value("SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == 1 and now == before else f"no ([{up}], {now - before} backend crashes)"


def _structure(data, offs, w):
    """-> 1 when the offsets are w+1, start at 0, end at the size, never decrease, and
    every interior one sits right after a newline; else 0 or what was read."""
    if not isinstance(offs, list):
        return offs
    if len(offs) != w + 1:
        return f"got {len(offs)} offsets, want {w + 1}"
    size = len(data)
    ok = offs[0] == 0 and offs[w] == size
    for i in range(1, w + 1):
        ok = ok and offs[i] >= offs[i - 1]
    for i in range(1, w):
        if 0 < offs[i] < size and data[offs[i] - 1] != 0x0a:
            ok = False
    return 1 if ok else 0


def _split_real(data, offs, w):
    """-> 1 when the w-1 interior offsets strictly increase inside (0, size)."""
    if not isinstance(offs, list):
        return offs
    if len(offs) != w + 1:
        return f"got {len(offs)} offsets, want {w + 1}"
    prev, ok = 0, True
    for i in range(1, w):
        ok = ok and prev < offs[i] < len(data)
        prev = offs[i]
    return 1 if ok else 0


def _split_load_hash(db, data, offs, w):
    """Load each range into a fresh columnar t_split -> the set hash of t_split."""
    if not isinstance(offs, list):
        return offs
    db.run("DROP TABLE IF EXISTS t_split; CREATE TABLE t_split (id int, txt text) USING pgcolumnar")
    for i in range(w):
        lo = offs[i] if i < len(offs) else 0
        hi = offs[i + 1] if i + 1 < len(offs) else 0
        if hi - lo > 0:
            db.copy_in("t_split", data[lo:hi])
    return db.set_hash("SELECT * FROM t_split")


def _mkpart(db, name, n):
    """A RANGE-partitioned columnar table over id in [1,5000], n partitions."""
    step = (5000 + n) // n
    ddl = f"DROP TABLE IF EXISTS {name} CASCADE; CREATE TABLE {name} (id int, txt text) PARTITION BY RANGE (id);"
    for i in range(n):
        lo = "MINVALUE" if i == 0 else str(i * step)
        hi = "MAXVALUE" if i == n - 1 else str((i + 1) * step)
        ddl += f" CREATE TABLE {name}_p{i} PARTITION OF {name} FOR VALUES FROM ({lo}) TO ({hi}) USING pgcolumnar;"
    db.run(ddl)


def _server_file(db, d, name, sql):
    """The server writes `sql`'s rows to d/name -> the path."""
    path = d / name
    db.run(f"COPY ({sql}) TO '{path}' WITH (FORMAT text)")
    return path


def _read(path):
    try:
        return path.read_bytes()
    except OSError as exc:
        return query_error(f"cannot read {path}: {exc}")


def _split_readings(r, db, key, path, w, gen, *, real=False):
    data = _read(path)
    offs = db.offsets(path, w)
    if not isinstance(data, bytes):
        offs = data
    r["structure " + key] = _structure(data, offs, w)
    r["reconstruct " + key] = _split_load_hash(db, data, offs, w)
    r["reconstruct want " + key] = db.set_hash(gen)
    if real:
        r["split real " + key] = _split_real(data, offs, w)


@pytest.fixture(scope="module")
def pcopy(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    d = server_dir
    db = _Db(pgc_cluster.dsn(dbname=DB))
    try:
        db.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        before = _crashes(pgc_cluster)

        # ---- the splitter -------------------------------------------------------
        db.run("CREATE TABLE t_heap (id int, txt text) USING heap; "
               "CREATE TABLE t_col (id int, txt text) USING pgcolumnar")
        f = _server_file(db, d, "data_5000.txt", GEN_F.format(n=5000))
        db.run(f"COPY t_heap FROM '{f}' WITH (FORMAT text)")
        db.run(f"COPY t_col FROM '{f}' WITH (FORMAT text)")
        r["single col"] = db.set_hash("SELECT * FROM t_col")
        r["single heap"] = db.set_hash("SELECT * FROM t_heap")
        for w in (1, 2, 3, 8, 16):
            _split_readings(r, db, f"5000/{w}", f, w, GEN_F.format(n=5000), real=w >= 2)
        big = _server_file(db, d, "biglines.txt", GEN_BIG)
        _split_readings(r, db, "big/4", big, 4, GEN_BIG, real=True)
        tiny = _server_file(db, d, "data_5.txt", GEN_F.format(n=5))
        _split_readings(r, db, "tiny/8", tiny, 8, GEN_F.format(n=5))
        one = _server_file(db, d, "data_1.txt", GEN_F.format(n=1))
        _split_readings(r, db, "one/4", one, 4, GEN_F.format(n=1))
        nonl = _server_file(db, d, "nonl.txt", GEN_NONL)
        try:
            os.truncate(nonl, nonl.stat().st_size - 1)
        except OSError as exc:
            print(f"-- truncate failed: {exc}")
        _split_readings(r, db, "nonl/4", nonl, 4, GEN_NONL)

        r["workers 0"] = db.refused(f"SELECT pgcolumnar.file_split_offsets('{f}', 0)", "at least 1")
        r["directory"] = db.refused(f"SELECT pgcolumnar.file_split_offsets('{d}', 1)", "not a regular file")

        # ---- the coordinator: partitioned target --------------------------------
        for w in (1, 2, 4):
            _mkpart(db, "t_pcp", 4)
            r[f"rows partitioned/{w}"] = db.value(f"SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '{f}', {w})")
            r[f"oracle partitioned/{w}"] = db.set_hash("SELECT * FROM t_pcp")
            r[f"oracle want partitioned/{w}"] = db.set_hash("SELECT * FROM t_heap")
            r[f"leak partitioned/{w}"] = db.value(LEAK)

        signed = _server_file(db, d, "signed.txt",
                              "SELECT g AS id, 'h'||g AS txt FROM generate_series(-100, 399) g")
        db.run("CREATE TABLE t_heap_s (id int, txt text)")
        db.run(f"COPY t_heap_s FROM '{signed}' WITH (FORMAT text)")
        db.run("CREATE TABLE t_signed (id int, txt text) PARTITION BY RANGE (id); "
               "CREATE TABLE t_signed_a PARTITION OF t_signed FOR VALUES FROM (MINVALUE) TO (100) USING pgcolumnar; "
               "CREATE TABLE t_signed_b PARTITION OF t_signed FOR VALUES FROM (100) TO (300) USING pgcolumnar; "
               "CREATE TABLE t_signed_c PARTITION OF t_signed FOR VALUES FROM (300) TO (MAXVALUE) USING pgcolumnar")
        r["signed rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('t_signed'::regclass, '{signed}', 3)")
        r["signed"] = db.set_hash("SELECT * FROM t_signed")
        r["signed want"] = db.set_hash("SELECT * FROM t_heap_s")
        r["signed leak"] = db.value(LEAK)

        db.run("CREATE TABLE t_txtkey (k text, v int) PARTITION BY RANGE (k); "
               "CREATE TABLE t_txtkey_a PARTITION OF t_txtkey FOR VALUES FROM (MINVALUE) TO ('m') USING pgcolumnar; "
               "CREATE TABLE t_txtkey_b PARTITION OF t_txtkey FOR VALUES FROM ('m') TO (MAXVALUE) USING pgcolumnar")
        r["text key"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_txtkey'::regclass, '{f}', 2)",
                                   "numeric or date/time")
        r["text key up"] = _survived(db, pgc_cluster, before)

        gen = _server_file(db, d, "genkey.txt", "SELECT 'lbl'||g AS label, g AS id FROM generate_series(1,3000) g")
        schema = "label text, gcol int GENERATED ALWAYS AS (id*2) STORED, id int"
        db.run(f"CREATE TABLE t_heap_g ({schema})")
        db.run(f"COPY t_heap_g (label, id) FROM '{gen}' WITH (FORMAT text)")
        db.run(f"CREATE TABLE t_gen ({schema}) PARTITION BY RANGE (id); "
               "CREATE TABLE t_gen_a PARTITION OF t_gen FOR VALUES FROM (MINVALUE) TO (1000) USING pgcolumnar; "
               "CREATE TABLE t_gen_b PARTITION OF t_gen FOR VALUES FROM (1000) TO (2000) USING pgcolumnar; "
               "CREATE TABLE t_gen_c PARTITION OF t_gen FOR VALUES FROM (2000) TO (MAXVALUE) USING pgcolumnar")
        r["gen rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('t_gen'::regclass, '{gen}', 3)")
        r["gen"] = db.set_hash("SELECT * FROM t_gen")
        r["gen want"] = db.set_hash("SELECT * FROM t_heap_g")

        badkey = d / "badkey.txt"
        badkey.write_text("".join(f"{i}\thost_{i}\n" for i in range(1, 401)) + "notanint\tboom\n"
                          + "".join(f"{i}\thost_{i}\n" for i in range(401, 801)))
        badkey.chmod(0o644)
        _mkpart(db, "t_pcp", 4)
        r["bad key"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '{badkey}', 4)",
                                  "invalid input syntax")
        r["bad key empty"] = db.value("SELECT count(*) FROM t_pcp")
        r["bad key leak"] = db.value(LEAK)

        db.run("CREATE TABLE t_gap (id int, txt text) PARTITION BY RANGE (id); "
               "CREATE TABLE t_gap_a PARTITION OF t_gap FOR VALUES FROM (MINVALUE) TO (2000) USING pgcolumnar; "
               "CREATE TABLE t_gap_b PARTITION OF t_gap FOR VALUES FROM (3000) TO (MAXVALUE) USING pgcolumnar")
        r["gap"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_gap'::regclass, '{f}', 4)",
                              "failed: no partition of relation")
        r["gap empty"] = db.value("SELECT count(*) FROM t_gap")
        r["gap leak"] = db.value(LEAK)
        r["gap up"] = _survived(db, pgc_cluster, before)

        lines = f.read_bytes().splitlines(keepends=True)
        random.Random(1341).shuffle(lines)
        shuf = d / "shuf.txt"
        shuf.write_bytes(b"".join(lines))
        shuf.chmod(0o644)
        _mkpart(db, "t_pcp", 4)
        r["unsorted"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '{shuf}', 4)", "not sorted")
        r["unsorted empty"] = db.value("SELECT count(*) FROM t_pcp")

        db.run("CREATE TABLE t_def (id int, txt text) PARTITION BY RANGE (id); "
               "CREATE TABLE t_def_a PARTITION OF t_def FOR VALUES FROM (MINVALUE) TO (2500) USING pgcolumnar; "
               "CREATE TABLE t_def_d PARTITION OF t_def DEFAULT USING pgcolumnar")
        r["default"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_def'::regclass, '{f}', 4)", "DEFAULT partition")

        _mkpart(db, "t_pcp10", 10)
        r["guard"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_pcp10'::regclass, '{f}', 10)",
                                "max_prepared_transactions")
        r["guard empty"] = db.value("SELECT count(*) FROM t_pcp10")

        _mkpart(db, "t_pcp", 4)
        r["missing"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '{d / 'nope.txt'}', 2)",
                                  "could not open", "could not stat", "no such file", "not a regular file")
        r["missing up"] = _survived(db, pgc_cluster, before)
        r["missing empty"] = db.value("SELECT count(*) FROM t_pcp")

        db.run("CREATE TABLE t_heaptgt (id int, txt text)")
        r["heap target"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_heaptgt'::regclass, '{f}', 2)",
                                      "not a pgcolumnar table")

        # ---- the coordinator: one columnar table ---------------------------------
        r["guc"] = db.value("SHOW pgcolumnar.bulk_parallel_writer")
        for w in (1, 2, 4):
            db.run("DROP TABLE IF EXISTS t_single; CREATE TABLE t_single (id int, txt text) USING pgcolumnar")
            r[f"rows single/{w}"] = db.value(f"SELECT pgcolumnar.parallel_copy('t_single'::regclass, '{f}', {w})")
            r[f"oracle single/{w}"] = db.set_hash("SELECT * FROM t_single")
            r[f"oracle want single/{w}"] = db.set_hash("SELECT * FROM t_heap")
            r[f"leak single/{w}"] = db.value(LEAK)

        fw = _server_file(db, d, "data_40000.txt", GEN_F.format(n=40000))
        db.run("CREATE TABLE t_wit_heap (id int, txt text)")
        db.run(f"COPY t_wit_heap FROM '{fw}' WITH (FORMAT text)")
        db.run("CREATE TABLE t_wit (id int, txt text) USING pgcolumnar; "
               "SELECT pgcolumnar.set_options('t_wit'::regclass, chunk_group_row_limit => 1000, "
               "stripe_row_limit => 2000)")
        r["wit rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('t_wit'::regclass, '{fw}', 4)")
        r["wit"] = db.set_hash("SELECT * FROM t_wit")
        r["wit want"] = db.set_hash("SELECT * FROM t_wit_heap")
        r["wit stripes"] = db.value("SELECT (count(DISTINCT stripeid) > 4)::text FROM pgcolumnar.stats('t_wit'::regclass)")
        r["wit overlaps"] = db.value(
            "WITH s AS (SELECT stripeid, fileoffset AS lo, fileoffset+datalength AS hi "
            "FROM pgcolumnar.stats('t_wit'::regclass)) "
            "SELECT count(*) FROM s a JOIN s b ON a.stripeid <> b.stripeid AND a.lo < b.hi AND b.lo < a.hi")
        r["wit coverage"] = db.value("SELECT sum(rowcount)::bigint FROM pgcolumnar.stats('t_wit'::regclass)")
        r["wit leak"] = db.value(LEAK)

        db.run("DROP TABLE IF EXISTS t_single; CREATE TABLE t_single (id int, txt text) USING pgcolumnar")
        r["single bad"] = db.refused(f"SELECT pgcolumnar.parallel_copy('t_single'::regclass, '{badkey}', 4)",
                                     "failed: invalid input syntax")
        r["single bad empty"] = db.value("SELECT count(*) FROM t_single")
        r["single bad leak"] = db.value(LEAK)

        # ---- #686: a FIFO is refused, not a hang --------------------------------
        db.run("CREATE TABLE t_fifo (id int, txt text) USING pgcolumnar")
        fifo = d / "pc.fifo"
        os.mkfifo(fifo, 0o666)
        r["fifo"] = db.sqlstate_or_hang(f"SELECT pgcolumnar.parallel_copy('t_fifo'::regclass, '{fifo}', 2)", fifo)
        r["fifo up"] = _survived(db, pgc_cluster, before)
        _mkpart(db, "t_fifo_part", 3)
        fifo2 = d / "pc2.fifo"
        os.mkfifo(fifo2, 0o666)
        r["fifo part"] = db.sqlstate_or_hang(
            f"SELECT pgcolumnar.parallel_copy('t_fifo_part'::regclass, '{fifo2}', 3)", fifo2)
        r["fifo part up"] = _survived(db, pgc_cluster, before)

        # ---- #1886: a remote URL is refused up front ------------------------------
        r["split s3"] = db.remote_refusal(f"SELECT pgcolumnar.file_split_offsets('{REMOTE_S3}', 2)")
        r["split http"] = db.remote_refusal(f"SELECT pgcolumnar.file_split_offsets('{REMOTE_HTTP}', 2)")
        r["split other"] = " ; ".join(
            db.remote_refusal(f"SELECT pgcolumnar.file_split_offsets('{u}', 2)") for u in REMOTE_OTHER)
        db.run("CREATE TABLE t_rem (id int, txt text) USING pgcolumnar")
        _mkpart(db, "t_rem_part", 4)
        r["pcopy s3"] = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '{REMOTE_S3}', 2)")
        r["pcopy http"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '{REMOTE_HTTP}', 2)")
        r["pcopy other"] = " ; ".join(
            db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_rem_part'::regclass, '{u}', 2)")
            for u in REMOTE_OTHER)
        r["pcopy heap"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_heaptgt'::regclass, '{REMOTE_S3}', 2)")
        # d/"s3:" is a directory, so d/"s3://ctl.txt" is a local file whose path holds
        # "s3://" past its start.
        sub = d / "s3:"
        sub.mkdir()
        os.chmod(sub, 0o777)
        local = f"{d}/s3://ctl.txt"
        db.run(f"COPY ({GEN_F.format(n=5000)}) TO '{local}' WITH (FORMAT text)")
        offs = db.offsets(local, 2)
        r["split local"] = offs[2] if isinstance(offs, list) and len(offs) == 3 else offs
        try:
            r["split local size"] = (sub / "ctl.txt").stat().st_size
        except OSError as exc:
            r["split local size"] = query_error(f"cannot stat the control file: {exc}")
        r["pcopy local"] = db.value(f"SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '{local}', 2)")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- the splitter ---------------------------------------------------------------

def test_parallel_copy_single_copy_heap_and_columnar_agree(pcopy, expect):
    expect.hash(pcopy["single col"], pcopy["single heap"], "single-COPY heap and columnar agree")


@pytest.mark.parametrize("key,name", STRUCTURE, ids=[k for k, _ in STRUCTURE])
def test_parallel_copy_split_offsets_are_newline_aligned(pcopy, expect, key, name):
    expect.num(pcopy["structure " + key], 1, name)


@pytest.mark.parametrize("key,name", RECONSTRUCT, ids=[k for k, _ in RECONSTRUCT])
def test_parallel_copy_range_loads_give_back_the_generated_rows(pcopy, expect, key, name):
    expect.hash(pcopy["reconstruct " + key], pcopy["reconstruct want " + key], name)


@pytest.mark.parametrize("key,name", SPLIT_REAL, ids=[k for k, _ in SPLIT_REAL])
def test_parallel_copy_every_range_of_a_large_file_is_non_empty(pcopy, expect, key, name):
    expect.num(pcopy["split real " + key], 1, name)


def test_parallel_copy_zero_workers_is_rejected(pcopy, expect):
    expect.text(pcopy["workers 0"], "refused: at least 1", "workers < 1 is rejected")


def test_parallel_copy_split_of_a_directory_is_rejected(pcopy, expect):
    expect.text(pcopy["directory"], "refused: not a regular file",
                "file_split_offsets: a directory is rejected (not an 8-exabyte file)")


# ---- the coordinator --------------------------------------------------------------

@pytest.mark.parametrize("key,name", LOAD_ROWS, ids=[k for k, _ in LOAD_ROWS])
def test_parallel_copy_returns_every_row_loaded(pcopy, expect, key, name):
    expect.num(pcopy["rows " + key], 5000, name)


@pytest.mark.parametrize("key,name", LOAD_ORACLE, ids=[k for k, _ in LOAD_ORACLE])
def test_parallel_copy_load_equals_the_single_copy(pcopy, expect, key, name):
    expect.hash(pcopy["oracle " + key], pcopy["oracle want " + key], name)


@pytest.mark.parametrize("key,name", LOAD_LEAK, ids=[k for k, _ in LOAD_LEAK])
def test_parallel_copy_load_leaves_no_prepared_transaction(pcopy, expect, key, name):
    expect.num(pcopy["leak " + key], 0, name)


def test_parallel_copy_signed_keys_return_every_row(pcopy, expect):
    expect.num(pcopy["signed rows"], 500, "signed keys straddling 0: rows returned = 500")


def test_parallel_copy_signed_keys_equal_the_oracle(pcopy, expect):
    expect.hash(pcopy["signed"], pcopy["signed want"],
                "signed keys straddling 0: result == oracle (no misbucketing/deadlock)")


def test_parallel_copy_signed_keys_leave_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["signed leak"], 0, "signed keys straddling 0: no prepared-transaction leak")


def test_parallel_copy_text_partition_key_is_rejected(pcopy, expect):
    expect.text(pcopy["text key"], "refused: numeric or date/time", "text partition key is rejected (not a crash)")


def test_parallel_copy_text_partition_key_leaves_the_server_up(pcopy, expect):
    expect.text(pcopy["text key up"], "1", "text partition key: server still up (no crash)")


def test_parallel_copy_key_after_a_generated_column_returns_every_row(pcopy, expect):
    expect.num(pcopy["gen rows"], 3000, "key not in column 1 (generated col before it): rows = 3000")


def test_parallel_copy_key_after_a_generated_column_equals_the_oracle(pcopy, expect):
    expect.hash(pcopy["gen"], pcopy["gen want"], "key not in column 1: result == oracle")


def test_parallel_copy_bad_partition_key_value_is_rejected(pcopy, expect):
    expect.text(pcopy["bad key"], "refused: invalid input syntax", "atomic: a bad partition-key value is rejected")


def test_parallel_copy_bad_partition_key_loads_nothing(pcopy, expect):
    expect.num(pcopy["bad key empty"], 0, "atomic: bad-key load leaves the target empty")


def test_parallel_copy_bad_partition_key_leaves_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["bad key leak"], 0, "atomic: no prepared-transaction leak after bad-key")


def test_parallel_copy_loader_failure_fails_the_load_with_its_cause(pcopy, expect):
    expect.text(pcopy["gap"], "refused: failed: no partition of relation",
                "atomic: a loader failure fails the whole load")


def test_parallel_copy_loader_failure_rolls_back_the_siblings(pcopy, expect):
    expect.num(pcopy["gap empty"], 0, "atomic: loader-failure leaves the target empty (siblings rolled back)")


def test_parallel_copy_loader_failure_leaves_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["gap leak"], 0, "atomic: no prepared-transaction leak after loader failure")


def test_parallel_copy_loader_failure_leaves_the_server_up(pcopy, expect):
    expect.text(pcopy["gap up"], "1", "atomic: server still up after a failed load")


def test_parallel_copy_unsorted_input_is_rejected(pcopy, expect):
    expect.text(pcopy["unsorted"], "refused: not sorted", "unsorted input is rejected")


def test_parallel_copy_unsorted_input_loads_nothing(pcopy, expect):
    expect.num(pcopy["unsorted empty"], 0, "unsorted rejection loads nothing")


def test_parallel_copy_default_partition_is_rejected(pcopy, expect):
    expect.text(pcopy["default"], "refused: DEFAULT partition", "DEFAULT partition target is rejected")


def test_parallel_copy_prepared_transaction_guard_fires(pcopy, expect):
    expect.text(pcopy["guard"], "refused: max_prepared_transactions", "max_prepared_transactions guard fires")


def test_parallel_copy_prepared_transaction_guard_loads_nothing(pcopy, expect):
    expect.num(pcopy["guard empty"], 0, "guard rejects before loading anything")


def test_parallel_copy_missing_file_errors(pcopy, expect):
    expect.text(pcopy["missing"], "refused: could not open", "missing file: errors, not crashes")


def test_parallel_copy_missing_file_leaves_the_server_up(pcopy, expect):
    expect.text(pcopy["missing up"], "1", "missing file: server still up (no worker crash)")


def test_parallel_copy_missing_file_loads_nothing(pcopy, expect):
    expect.num(pcopy["missing empty"], 0, "missing file: nothing loaded")


def test_parallel_copy_heap_target_is_rejected(pcopy, expect):
    expect.text(pcopy["heap target"], "refused: not a pgcolumnar table", "non-columnar target is rejected")


def test_parallel_copy_bulk_parallel_writer_is_off_by_default(pcopy, expect):
    expect.text(pcopy["guc"], "off", "bulk_parallel_writer GUC defaults off (core write path unchanged)")


# ---- the concurrency witness -------------------------------------------------------

def test_parallel_copy_witness_loads_every_row(pcopy, expect):
    expect.num(pcopy["wit rows"], 40000, "witness: parallel_copy(single table, 4 workers, 40000 rows) loads every row")


def test_parallel_copy_witness_equals_the_oracle(pcopy, expect):
    expect.hash(pcopy["wit"], pcopy["wit want"], "witness: result == single-COPY oracle")


def test_parallel_copy_witness_has_more_stripes_than_workers(pcopy, expect):
    expect.text(pcopy["wit stripes"], "true", "witness: >4 distinct stripe ids (each worker reserved several stripes)")


def test_parallel_copy_witness_stripes_do_not_overlap(pcopy, expect):
    expect.num(pcopy["wit overlaps"], 0, "witness: no stripe byte-range overlaps another (no on-disk collision)")


def test_parallel_copy_witness_stripes_cover_every_row(pcopy, expect):
    expect.num(pcopy["wit coverage"], 40000, "witness: stripe rowcounts sum to every input row (complete coverage)")


def test_parallel_copy_witness_leaves_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["wit leak"], 0, "witness: no prepared-transaction leak")


def test_parallel_copy_single_table_bad_row_fails_with_its_cause(pcopy, expect):
    expect.text(pcopy["single bad"], "refused: failed: invalid input syntax",
                "single table: a bad row fails the whole load")


def test_parallel_copy_single_table_failure_loads_nothing(pcopy, expect):
    expect.num(pcopy["single bad empty"], 0, "single table: failed load leaves the target empty")


def test_parallel_copy_single_table_failure_leaves_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["single bad leak"], 0, "single table: no prepared-transaction leak after failure")


# ---- #686: a FIFO is refused, not a hang --------------------------------------------

def test_parallel_copy_fifo_is_refused_not_a_hang(pcopy, expect):
    expect.text(pcopy["fifo"], "42809", "parallel_copy on a FIFO is refused, not a hang (42809)")


def test_parallel_copy_fifo_refusal_leaves_the_backend_up(pcopy, expect):
    expect.text(pcopy["fifo up"], "1", "backend still up after the parallel_copy FIFO refusal")


def test_parallel_copy_fifo_on_a_partitioned_target_is_refused_not_a_hang(pcopy, expect):
    expect.text(pcopy["fifo part"], "42809",
                "parallel_copy on a FIFO via the partitioned coordinator is refused, not a hang (42809)")


def test_parallel_copy_partitioned_fifo_refusal_leaves_the_backend_up(pcopy, expect):
    expect.text(pcopy["fifo part up"], "1", "backend still up after the partitioned-coordinator FIFO refusal")


# ---- #1886: a remote URL is refused up front, not opened as a local file -------------

def test_parallel_copy_split_refuses_an_s3_url(pcopy, expect):
    expect.text(pcopy["split s3"], _want_refusal("file_split_offsets", REMOTE_S3),
                "file_split_offsets refuses an s3:// URL with 0A000, naming itself and the URL (#1886)")


def test_parallel_copy_split_refuses_an_http_url(pcopy, expect):
    expect.text(pcopy["split http"], _want_refusal("file_split_offsets", REMOTE_HTTP),
                "file_split_offsets refuses an http:// URL with 0A000, naming itself and the URL (#1886)")


def test_parallel_copy_split_refuses_the_other_remote_schemes(pcopy, expect):
    expect.text(pcopy["split other"], " ; ".join(_want_refusal("file_split_offsets", u) for u in REMOTE_OTHER),
                "file_split_offsets refuses https://, gs://, az:// and S3:// URLs with 0A000 too (#1886)")


def test_parallel_copy_refuses_an_s3_url(pcopy, expect):
    expect.text(pcopy["pcopy s3"], _want_refusal("parallel_copy", REMOTE_S3),
                "parallel_copy refuses an s3:// URL with 0A000, naming itself and the URL (#1886)")


def test_parallel_copy_refuses_an_http_url(pcopy, expect):
    expect.text(pcopy["pcopy http"], _want_refusal("parallel_copy", REMOTE_HTTP),
                "parallel_copy refuses an http:// URL with 0A000, naming itself and the URL (#1886)")


def test_parallel_copy_refuses_the_other_remote_schemes_for_a_partitioned_target(pcopy, expect):
    expect.text(pcopy["pcopy other"], " ; ".join(_want_refusal("parallel_copy", u) for u in REMOTE_OTHER),
                "parallel_copy refuses https://, gs://, az:// and S3:// URLs for a partitioned target with 0A000 too (#1886)")


def test_parallel_copy_refuses_a_remote_url_before_the_target(pcopy, expect):
    expect.text(pcopy["pcopy heap"], _want_refusal("parallel_copy", REMOTE_S3),
                "parallel_copy refuses a remote URL before it looks at the target: a heap target gets 0A000 (#1886)")


def test_parallel_copy_split_still_reads_a_local_path_holding_a_scheme(pcopy, expect):
    expect.num(pcopy["split local"], pcopy["split local size"],
               "control: file_split_offsets still splits a local path that holds s3:// past its start (#1886)")


def test_parallel_copy_still_loads_a_local_path_holding_a_scheme(pcopy, expect):
    expect.num(pcopy["pcopy local"], 5000,
               "control: parallel_copy still loads a local path that holds s3:// past its start (#1886)")

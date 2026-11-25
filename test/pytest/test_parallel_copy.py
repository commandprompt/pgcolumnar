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
0A000, naming themselves and the URL (#1886). parallel_copy refuses after its INSERT
check and before it checks the target's kind, so a heap target gets 0A000. The
refusal covers s3a:// and s3n:// too, in any case (#1918).

THREE FIXES THE SHELL SHARES. The per-range loads are compared with the generator
that wrote the file, not with a single columnar COPY of it, which shares every defect
of the COPY path. A failed load must carry the loader's own message, not any message
with "failed" in it. And a backend that died does not pass as one that survived: a
fresh connection answers after crash recovery, so survival also reads the server log's
count of killed backends.

A LOADER THAT FAILS CLOSES ITS FILE ONCE (#2083). The server log must carry no
"CloseTransientFile was not obtained" WARNING from the failed loads.

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
HADOOP = ("s3a://pgc-test/f1918/data.txt", "s3n://pgc-test/f1918/data.txt")
HADOOP_CASE = ("S3A://pgc-test/f1918/data.txt", "S3n://pgc-test/f1918/data.txt")

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

    def sqlstate(self, sql):
        """-> the SQLSTATE `sql` raised under a 30 s statement_timeout, or "noerror (value)"."""
        try:
            with psycopg.connect(self.dsn, autocommit=True, options="-c statement_timeout=30s") as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return exc.sqlstate or query_error(_message(exc))
        return f"noerror ({row[0] if row else None})"

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


def _double_closes(cluster):
    """-> how many times the server log says a file was closed twice (#2083)."""
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count(
            "CloseTransientFile was not obtained")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")


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


def _even_run_cuts(data, offs):
    """-> how many interior offsets rise, stay inside the file and follow a newline
    that comes after an even, non-empty backslash run."""
    n = 0
    for k in range(1, len(offs) - 1):
        o = offs[k]
        if not (offs[k - 1] < o < len(data)) or data[o - 1] != 0x0A:
            continue
        run = 0
        while o - 2 - run >= 0 and data[o - 2 - run] == 0x5C:
            run += 1
        if run and run % 2 == 0:
            n += 1
    return n


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


def _projection_load(db, path, workers, preload):
    """-> 'wWORKERS:returned/rows/projection rows' for a parallel_copy into a fresh table
    with an options row and a projection, which holds `preload` rows first.

    The load runs under a statement_timeout: 2 or more workers into a projection that
    holds no rows yet hang until cancelled (#2161), so a regression must not hang the
    module."""
    t = f"t_proj_w{workers}"
    db.run(f"CREATE TABLE {t} (id int, txt text) USING pgcolumnar")
    db.run(f"SELECT pgcolumnar.set_options('{t}', compression => 'zstd')")
    if preload:
        db.run(f"INSERT INTO {t} SELECT g, 'pre'||g FROM generate_series(1, {preload}) g")
    db.run(f"SELECT pgcolumnar.add_projection('{t}', 'p', columns => ARRAY['txt','id'], sort_key => ARRAY['id'])")
    try:
        with psycopg.connect(db.dsn, autocommit=True, options="-c statement_timeout=60s") as conn:
            got = conn.execute(f"SELECT pgcolumnar.parallel_copy('{t}'::regclass, '{path}', {workers})").fetchone()[0]
    except psycopg.Error as exc:
        got = _message(exc)
    rows = db.value(f"SELECT count(*) FROM {t}")
    prows = db.value(f"SELECT count(*) FROM pgcolumnar.read_projection('{t}', 'p')")
    return f"w{workers}:{got}/{rows}/{prows}"


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
        closes_before = _double_closes(pgc_cluster)

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
        closes_now = _double_closes(pgc_cluster)
        r["double close"] = (closes_now - closes_before
                             if isinstance(closes_before, int) and isinstance(closes_now, int)
                             else query_error(f"server log unreadable: {closes_before} / {closes_now}"))

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

        # ---- #1918: s3a:// and s3n:// are refused too, in any case ---------------
        for key, urls in (("hadoop", HADOOP), ("hadoop case", HADOOP_CASE)):
            r[f"split {key}"] = " ; ".join(
                db.remote_refusal(f"SELECT pgcolumnar.file_split_offsets('{u}', 2)") for u in urls)
            r[f"pcopy {key}"] = " ; ".join(
                db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '{u}', 2)") for u in urls)
        # d/"s3a:" is a directory, so d/"s3a://ctl.txt" is a local file whose path
        # holds "s3a://" past its start.
        sub_a = d / "s3a:"
        sub_a.mkdir()
        os.chmod(sub_a, 0o777)
        local_a = f"{d}/s3a://ctl.txt"
        db.run(f"COPY ({GEN_F.format(n=5000)}) TO '{local_a}' WITH (FORMAT text)")
        offs = db.offsets(local_a, 2)
        r["split local s3a"] = offs[2] if isinstance(offs, list) and len(offs) == 3 else offs
        try:
            r["split local s3a size"] = (sub_a / "ctl.txt").stat().st_size
        except OSError as exc:
            r["split local s3a size"] = query_error(f"cannot stat the control file: {exc}")
        r["pcopy local s3a"] = db.value(f"SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '{local_a}', 2)")

        # ---- #2084: ranges follow COPY text syntax, not every newline byte ----------
        bsnl = d / "bsnl.txt"
        bsnl.write_bytes(b"".join(b"first%d\\\nsecond%d\n" % (i, i) for i in range(2000)))
        bsnl2 = d / "bsnl2.txt"
        bsnl2.write_bytes(b"".join(b"%d\tline1_%d\\\nline2_%d\n" % (i, i, i) for i in range(1, 2001)))
        eod = d / "eod.txt"
        eod.write_bytes(b"".join(b"%d\tx\n" % i for i in range(1, 1001)) + b"\\.\n"
                        + b"".join(b"%d\ty\n" % i for i in range(1001, 2001)))
        for f_ in (bsnl, bsnl2, eod):
            os.chmod(f_, 0o644)
        db.run(f"CREATE TABLE h_bsnl (t text); COPY h_bsnl FROM '{bsnl}'; "
               f"CREATE TABLE h_bsnl2 (id int, txt text); COPY h_bsnl2 FROM '{bsnl2}'; "
               f"CREATE TABLE h_eod (id int, txt text); COPY h_eod FROM '{eod}'; "
               "CREATE TABLE c_bsnl (t text) USING pgcolumnar; "
               "CREATE TABLE c_eod (id int, txt text) USING pgcolumnar")
        _mkpart(db, "t_bsnlp", 4)
        db.run("CREATE TABLE t_eodp (id int, txt text) PARTITION BY RANGE (id); "
               "CREATE TABLE t_eodp_a PARTITION OF t_eodp FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar; "
               "CREATE TABLE t_eodp_b PARTITION OF t_eodp FOR VALUES FROM (500) TO (1000) USING pgcolumnar; "
               "CREATE TABLE t_eodp_c PARTITION OF t_eodp FOR VALUES FROM (1000) TO (1500) USING pgcolumnar; "
               "CREATE TABLE t_eodp_d PARTITION OF t_eodp FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar")
        data = _read(bsnl)
        offs = db.offsets(bsnl, 4)
        if isinstance(data, bytes) and isinstance(offs, list) and len(offs) == 5:
            # a backslash two bytes before a cut: the cut follows an escaped newline
            r["2084 escaped cuts"] = sum(1 for o in offs[1:4] if o >= 2 and data[o - 2] == 0x5c)
        else:
            r["2084 escaped cuts"] = data if not isinstance(data, bytes) else query_error(f"offsets: {offs}")
        for key, tgt, src, heap in (("bsnl single", "c_bsnl", bsnl, "h_bsnl"),
                                    ("bsnl part", "t_bsnlp", bsnl2, "h_bsnl2"),
                                    ("eod single", "c_eod", eod, "h_eod"),
                                    ("eod part", "t_eodp", eod, "h_eod")):
            r[f"2084 {key} rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('{tgt}'::regclass, '{src}', 4)")
            r[f"2084 {key} rows want"] = db.value(f"SELECT count(*) FROM {heap}")
            r[f"2084 {key} hash"] = db.set_hash(f"SELECT * FROM {tgt}")
            r[f"2084 {key} hash want"] = db.set_hash(f"SELECT * FROM {heap}")
            if key == "eod single":
                r["2084 eod single leak"] = db.value(LEAK)

        # Only an odd backslash run escapes the newline after it (review of #2148,
        # mutants c1 and c2). even.txt alternates runs of 2 and 4. evenp.txt puts an
        # unsorted row (id 5) right after row 1500, which ends in an even run and sits
        # in a later partition: the partitioned splitter must refuse it.
        #
        # eodjunk.txt holds rows after \. that COPY would reject: COPY never
        # reads them, so they must not fail the load (mutant c4, the stop at \.).
        even = d / "even.txt"
        even.write_bytes(b"".join((b"v%d\\\\\n" if i % 2 else b"v%d\\\\\\\\\n") % i for i in range(1, 2001)))
        evenp = d / "evenp.txt"
        evenp.write_bytes(b"".join(b"%d\tv%d\\\\\n" % (i, i) + (b"5\tback\n" if i == 1500 else b"")
                                   for i in range(1, 2001)))
        eodj = d / "eodjunk.txt"
        eodj.write_bytes(b"".join(b"%d\tx\n" % i for i in range(1, 1001)) + b"\\.\n" + b"notanint\tz\n" * 1000)
        for f_ in (even, evenp, eodj):
            os.chmod(f_, 0o644)
        db.run(f"CREATE TABLE h_even (t text); COPY h_even FROM '{even}'; "
               f"CREATE TABLE h_eodj (id int, txt text); COPY h_eodj FROM '{eodj}'; "
               "CREATE TABLE c_even (t text) USING pgcolumnar; "
               "CREATE TABLE c_eodj (id int, txt text) USING pgcolumnar")
        _mkpart(db, "t_evenp", 4)
        _mkpart(db, "t_eodjp", 4)
        data = _read(even)
        offs = db.offsets(even, 4)
        if isinstance(data, bytes) and isinstance(offs, list) and len(offs) == 5:
            r["2084 even cuts"] = _even_run_cuts(data, offs)
        else:
            r["2084 even cuts"] = data if not isinstance(data, bytes) else query_error(f"offsets: {offs}")
        r["2084 evenp unsorted"] = db.sqlstate(f"SELECT pgcolumnar.parallel_copy('t_evenp'::regclass, '{evenp}', 4)")
        for key, tgt, src, heap in (("even single", "c_even", even, "h_even"),
                                    ("eodjunk single", "c_eodj", eodj, "h_eodj"),
                                    ("eodjunk part", "t_eodjp", eodj, "h_eodj")):
            r[f"2084 {key} rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('{tgt}'::regclass, '{src}', 4)")
            r[f"2084 {key} rows want"] = db.value(f"SELECT count(*) FROM {heap}")
            r[f"2084 {key} hash"] = db.set_hash(f"SELECT * FROM {tgt}")
            r[f"2084 {key} hash want"] = db.set_hash(f"SELECT * FROM {heap}")
            if key == "eodjunk single":
                r["2084 eodjunk single leak"] = db.value(LEAK)

        # ---- #2092: a load into a table with a projection fills the projection ----
        r["projection"] = " ".join(_projection_load(db, f, w, pre) for w, pre in ((1, 0), (2, 1000)))
        r["projection leak"] = db.value(LEAK)
        r["projection up"] = _survived(db, pgc_cluster, before)
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


def test_parallel_copy_failed_loaders_close_their_file_once(pcopy, expect):
    """#2083: the loads that failed in their loaders (the gapped target and the bad
    row) must not log "fd passed to CloseTransientFile was not obtained from
    OpenTransientFile", the sign of a file closed twice."""
    expect.num(pcopy["double close"], 0,
               "failed loaders close their file once: no CloseTransientFile WARNING in the server log (#2083)")


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


# ---- #1918: s3a:// and s3n:// are refused too ----------------------------------------

def test_parallel_copy_split_refuses_s3a_and_s3n_urls(pcopy, expect):
    expect.text(pcopy["split hadoop"], " ; ".join(_want_refusal("file_split_offsets", u) for u in HADOOP),
                "file_split_offsets refuses s3a:// and s3n:// URLs with 0A000, naming itself and the URL (#1918)")


def test_parallel_copy_split_refuses_s3a_and_s3n_in_any_case(pcopy, expect):
    expect.text(pcopy["split hadoop case"],
                " ; ".join(_want_refusal("file_split_offsets", u) for u in HADOOP_CASE),
                "file_split_offsets refuses S3A:// and S3n:// URLs too: the scheme match ignores case (#1918)")


def test_parallel_copy_refuses_s3a_and_s3n_urls(pcopy, expect):
    expect.text(pcopy["pcopy hadoop"], " ; ".join(_want_refusal("parallel_copy", u) for u in HADOOP),
                "parallel_copy refuses s3a:// and s3n:// URLs with 0A000, naming itself and the URL (#1918)")


def test_parallel_copy_refuses_s3a_and_s3n_in_any_case(pcopy, expect):
    expect.text(pcopy["pcopy hadoop case"], " ; ".join(_want_refusal("parallel_copy", u) for u in HADOOP_CASE),
                "parallel_copy refuses S3A:// and S3n:// URLs too: the scheme match ignores case (#1918)")


def test_parallel_copy_split_still_reads_a_local_path_holding_s3a(pcopy, expect):
    expect.num(pcopy["split local s3a"], pcopy["split local s3a size"],
               "control: file_split_offsets still splits a local path that holds s3a:// past its start (#1918)")


def test_parallel_copy_still_loads_a_local_path_holding_s3a(pcopy, expect):
    expect.num(pcopy["pcopy local s3a"], 5000,
               "control: parallel_copy still loads a local path that holds s3a:// past its start (#1918)")


# ---- #2084: ranges follow COPY text syntax, not every newline byte ----------------
# In COPY text format a backslash before a raw newline makes the newline part of the
# value, and a line holding only \. ends the data. Heap COPY of the same file is the
# oracle.

def test_parallel_copy_split_never_cuts_at_an_escaped_newline(pcopy, expect):
    expect.num(pcopy["2084 escaped cuts"], 0,
               "#2084 escaped newline / 4 workers: no split offset follows an escaped newline")


SYNTAX_ROWS = (
    ("bsnl single", "#2084 escaped newline / single table, 4 workers: rows returned = heap COPY's 2000"),
    ("bsnl part", "#2084 escaped newline / partitioned, 4 workers: rows returned = heap COPY's 2000"),
    ("eod single", "#2084 end-of-data marker / single table, 4 workers: rows returned = heap COPY's 1000"),
    ("eod part", "#2084 end-of-data marker / partitioned, 4 workers: rows returned = heap COPY's 1000"),
    ("even single", "#2084 even backslash run / single table, 4 workers: rows returned = heap COPY's 2000"),
    ("eodjunk single",
     "#2084 bad rows after the end-of-data marker / single table, 4 workers: rows returned = heap COPY's 1000"),
    ("eodjunk part",
     "#2084 bad rows after the end-of-data marker / partitioned, 4 workers: rows returned = heap COPY's 1000"),
)
SYNTAX_VALUES = (
    ("bsnl single", "#2084 escaped newline / single table, 4 workers: result == heap COPY"),
    ("bsnl part", "#2084 escaped newline / partitioned, 4 workers: result == heap COPY"),
    ("eod single", "#2084 end-of-data marker / single table, 4 workers: result == heap COPY"),
    ("eod part", "#2084 end-of-data marker / partitioned, 4 workers: result == heap COPY"),
    ("even single", "#2084 even backslash run / single table, 4 workers: result == heap COPY"),
    ("eodjunk single", "#2084 bad rows after the end-of-data marker / single table, 4 workers: result == heap COPY"),
    ("eodjunk part", "#2084 bad rows after the end-of-data marker / partitioned, 4 workers: result == heap COPY"),
)


@pytest.mark.parametrize("key,name", SYNTAX_ROWS, ids=[k for k, _ in SYNTAX_ROWS])
def test_parallel_copy_loads_the_rows_heap_copy_loads(pcopy, expect, key, name):
    expect.num(pcopy[f"2084 {key} rows"], pcopy[f"2084 {key} rows want"], name)


@pytest.mark.parametrize("key,name", SYNTAX_VALUES, ids=[k for k, _ in SYNTAX_VALUES])
def test_parallel_copy_loads_the_values_heap_copy_loads(pcopy, expect, key, name):
    expect.hash(pcopy[f"2084 {key} hash"], pcopy[f"2084 {key} hash want"], name)


def test_parallel_copy_end_of_data_marker_leaves_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["2084 eod single leak"], 0, "#2084 end-of-data marker / single table: no prepared-transaction leak")


def test_parallel_copy_split_cuts_after_an_even_backslash_run(pcopy, expect):
    """An even backslash run does not escape the newline after it, so a cut may follow
    it (review of #2148, mutant c1)."""
    expect.num(pcopy["2084 even cuts"], 3,
               "#2084 even backslash run / 4 workers: every split offset is distinct and follows an even backslash run")


def test_parallel_copy_unsorted_row_after_an_even_backslash_run_is_refused(pcopy, expect):
    """The partitioned splitter reads the line after an even run as its own row, so the
    sort guard sees it (review of #2148, mutant c2)."""
    expect.text(pcopy["2084 evenp unsorted"], "22000",
                "#2084 even backslash run / partitioned: an unsorted row after an even run is refused (22000)")


def test_parallel_copy_bad_rows_after_the_end_of_data_marker_leave_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["2084 eodjunk single leak"], 0,
               "#2084 bad rows after the end-of-data marker / single table: no prepared-transaction leak")

# ---- #2092: a load into a table with a projection --------------------------------

def test_parallel_copy_into_a_table_with_a_projection_fills_the_projection(pcopy, expect):
    """#2092 asked whether parallel_copy shares the import functions' crash: they wrote
    a projection at statement end with no active snapshot. It does not: on main's code,
    too, both loads fill the projection and no backend dies. The test keeps that true.

    Not covered: 2 or more workers into a projection that holds no rows yet. That load
    hangs until cancelled, on another cause (#2161), so the 2-worker case fills its
    projection first."""
    expect.text(f"{pcopy['projection']} | leak {pcopy['projection leak']} | survived {pcopy['projection up']}",
                "w1:5000/5000/5000 w2:5000/6000/6000 | leak 0 | survived 1",
                "parallel_copy into a table with a projection fills the projection, leaves no prepared transaction"
                " and keeps the server up (#2092)")

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
refusal covers s3a:// and s3n:// too, in any case (#1918). parallel_copy refuses more
than one worker for a single table with a unique index, which used to hang or fail
with a deadlock error (#2080). Each loader checks again under its own lock. On
PostgreSQL 18 and later that covers a temporal primary key, whose one index is both
unique and exclusion (#2200); the test declines before 18, which has no WITHOUT OVERLAPS.

#2187. A CREATE INDEX that queues between the loaders' locks on the table must not
hang the load: pgcolumnar.parallel_copy_open_hold_ms holds every loader but the first
before it opens the table, so the index queues in that gap every run, and the load
must finish with the index built after it. A loader that exits before it opens the
table fails the load rather than hanging it. A cancelled load returns only once its
prepared transactions are gone, so the caller's next statement sees none. A single
table with an enabled user trigger on INSERT is refused with more than one worker,
by the calling backend and by each loader, and a loader's refusal carries its HINT.

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
import re
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parallel_copy"
GEN_F = "SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt FROM generate_series(1, {n}) g"
GEN_BIG = "SELECT g AS id, 'h'||g||'_'||repeat('x', 70000) AS txt FROM generate_series(1, 20) g"
GEN_NONL = "SELECT g AS id, 'r'||g AS txt FROM generate_series(1,1000) g"
LEAK = "SELECT count(*) FROM pg_prepared_xacts WHERE database = current_database()"
LOADER = "backend_type = 'pgcolumnar parallel_copy loader'"
ROW_WAIT = f"SELECT count(*) FROM pg_stat_activity WHERE {LOADER} AND wait_event = 'transactionid'"
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


def _timeout(timeout):
    """-> connect() keywords that set statement_timeout to `timeout`, or none."""
    return {"options": f"-c statement_timeout={timeout}"} if timeout else {}


def _want_trigger(table, trigger):
    """The refusal #2187 promises for a single table with a trigger on INSERT."""
    return (f'0A000 pgcolumnar.parallel_copy cannot load "{table}" with more than one worker, '
            f'because it has trigger "{trigger}" on INSERT | Load it with workers => 1, or with COPY.')


def _want_unique(table):
    """The refusal #2080 promises for a single table with a unique index."""
    return (f'0A000 pgcolumnar.parallel_copy cannot load "{table}" with more than one worker, '
            'because it has a unique or exclusion index | Load it with workers => 1, or with COPY.')


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

    def value(self, sql, timeout=None):
        """-> the first column of the first row, or the failure as a query_error().
        `timeout` is a statement_timeout, so a hang ends as an error."""
        try:
            with psycopg.connect(self.dsn, autocommit=True, **_timeout(timeout)) as conn:
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

    def bad_names(self, sql):
        """-> the distinct bad_* words in the error `sql` raised, sorted and space-joined;
        "noerror" when it did not raise; a query_error() when the server never answered."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql).fetchall()
        except psycopg.Error as exc:
            if not exc.sqlstate:
                return query_error(_message(exc))
            return " ".join(sorted(set(re.findall(r"bad_[a-z]+", str(exc)))))
        return "noerror"

    def remote_refusal(self, sql, timeout=None, options=""):
        """-> "SQLSTATE message | hint" of the error `sql` raised, or "no error (...)".
        `timeout` is a statement_timeout, so a hang ends as an error. `options`
        adds more "-c name=value" settings."""
        kw = _timeout(timeout)
        if options:
            kw["options"] = f"{kw.get('options', '')} {options}".strip()
        try:
            with psycopg.connect(self.dsn, autocommit=True, **kw) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            if not exc.sqlstate:
                return query_error(_message(exc))
            return f"{exc.sqlstate} {exc.diag.message_primary} | {exc.diag.message_hint or 'no hint'}"
        return f"no error ({row[0] if row else None})"

    def unique_index_after_check(self, path, table="t_ur", workers=2, ddl=None):
        """-> remote_refusal() of a `workers`-worker parallel_copy into `table`
        (which must exist) that gains a unique index after the calling backend's
        check, with "worker N" for the worker number, which varies. `ddl` is a list
        of statements to run there instead of creating the index."""
        out = {}
        try:
            with psycopg.connect(self.dsn, autocommit=True) as lk:
                lk.execute("BEGIN")
                lk.execute(f"LOCK TABLE {table} IN SHARE MODE")
                load = threading.Thread(target=lambda: out.setdefault("got", self.remote_refusal(
                    f"SELECT pgcolumnar.parallel_copy('{table}'::regclass, '{path}', {workers})",
                    timeout="20s")))
                load.start()
                lk.execute("DO $$ BEGIN FOR i IN 1..200 LOOP "
                           "EXIT WHEN EXISTS (SELECT 1 FROM pg_locks "
                           f"WHERE relation = '{table}'::regclass AND NOT granted); "
                           "PERFORM pg_sleep(0.05); END LOOP; END $$")
                for stmt in ddl or [f"CREATE UNIQUE INDEX {table}_id ON {table} (id)"]:
                    lk.execute(stmt)
                lk.execute("COMMIT")
                load.join()
        except psycopg.Error as exc:
            return query_error(_message(exc))
        return re.sub(r"worker \d+ failed", "worker N failed", out.get("got", "no result"))

    def ddl_outcome(self, sql, timeout=None):
        """-> "no error (<command tag>)" when `sql` ran, else remote_refusal()'s "SQLSTATE message | hint"."""
        try:
            with psycopg.connect(self.dsn, autocommit=True, **_timeout(timeout)) as conn:
                cur = conn.execute(sql)
        except psycopg.Error as exc:
            if not exc.sqlstate:
                return query_error(_message(exc))
            return f"{exc.sqlstate} {exc.diag.message_primary} | {exc.diag.message_hint or 'no hint'}"
        return f"no error ({cur.statusmessage})"

    def _hold(self, ms):
        """Hold every loader but the first `ms` ms before it opens its table, or reset."""
        if ms:
            self.run(f"ALTER DATABASE {DB} SET pgcolumnar.parallel_copy_open_hold_ms = {ms}")
        else:
            self.run(f"ALTER DATABASE {DB} RESET pgcolumnar.parallel_copy_open_hold_ms")

    def _poll(self, sql, done, tries=100):
        """-> the last value of `sql`, read every 50 ms until done(value) or `tries` reads."""
        got = None
        for _ in range(tries):
            got = self.value(sql)
            if done(got):
                break
            time.sleep(0.05)
        return got

    def _loaders_on(self, table):
        """-> "H held, W waiting": the loaders' locks on `table`. A loader that has
        prepared holds its lock with no process (pid is null)."""
        return self.value("SELECT count(*) FILTER (WHERE l.granted) || ' held, ' || "
                          "count(*) FILTER (WHERE NOT l.granted) || ' waiting' "
                          f"FROM pg_locks l LEFT JOIN pg_stat_activity a USING (pid) "
                          f"WHERE l.relation = '{table}'::regclass AND (l.pid IS NULL OR a.{LOADER})")

    def index_mid_open(self, path, table, workers, ddl):
        """-> (what the loaders held when `ddl` queued, "load | ddl | rows | valid | leak").
        Every loader but the first is held 4 s before it opens `table`; `ddl` runs once
        the first holds its lock, so it queues between the loaders' requests (#2187)."""
        out = {}
        self._hold(4000)
        try:
            load = threading.Thread(target=lambda: out.setdefault("load", self.remote_refusal(
                f"SELECT pgcolumnar.parallel_copy('{table}'::regclass, '{path}', {workers})", timeout="15s")))
            load.start()
            got = None
            for _ in range(100):
                got = self._loaders_on(table)
                if isinstance(got, str) and not got.startswith("0 held"):
                    break
                time.sleep(0.05)
            ddl_t = threading.Thread(target=lambda: out.setdefault("ddl", self.ddl_outcome(ddl, timeout="15s")))
            ddl_t.start()
            waiting = f"SELECT count(*) FROM pg_locks WHERE relation = '{table}'::regclass AND mode = 'ShareLock' AND NOT granted"
            self._poll(waiting, lambda g: g == 1)
            seen = f"loaders {self._loaders_on(table)}, index {self.value(waiting)} waiting"
            load.join()
            ddl_t.join()
        finally:
            self._hold(0)
        rows = self.value(f"SELECT count(*) FROM {table}")
        valid = self.value(f"SELECT indisvalid::text FROM pg_index WHERE indexrelid = '{table}_id'::regclass")
        valid = {"true": "t", "false": "f"}.get(valid, valid)
        return seen, (f"{out.get('load', 'no result')} | {out.get('ddl', 'no result')} | {rows} | "
                      f"valid {valid} | leak {self.value(LEAK)}")

    def gone_load(self, path, table):
        """-> "load | rows | leak" of a 2-worker load whose second loader is terminated
        while it is held before opening `table` (#2187)."""
        out = {}
        self._hold(4000)
        try:
            load = threading.Thread(target=lambda: out.setdefault("load", self.remote_refusal(
                f"SELECT pgcolumnar.parallel_copy('{table}'::regclass, '{path}', 2)", timeout="15s")))
            load.start()
            victim = None
            for _ in range(100):
                held = self._loaders_on(table)
                if isinstance(held, str) and held.startswith("1 held"):
                    victim = self.value(f"SELECT pid FROM pg_stat_activity WHERE {LOADER} AND pid NOT IN "
                                        f"(SELECT pid FROM pg_locks WHERE relation = '{table}'::regclass AND pid IS NOT NULL)")
                    if isinstance(victim, int):
                        break
                time.sleep(0.05)
            self.value(f"SELECT pg_terminate_backend({victim})")
            load.join()
        finally:
            self._hold(0)
        return (f"{out.get('load', 'no result')} | {self.value(f'SELECT count(*) FROM {table}')} | "
                f"leak {self.value(LEAK)}")

    def cancelled_load(self, path, table, ref):
        """-> (what was seen before the cancel, "error | prepared seen by the same session").
        Another session locks row 1 of `ref`, which the first loader's foreign-key check
        needs; the second loader prepares; statement_timeout cancels the load (#2187).
        `<table>_load` catches the 57014 in plpgsql and counts the prepared transactions
        in the caller's backend at once, so the reading does not race the client."""
        seen = None
        try:
            with psycopg.connect(self.dsn, autocommit=True) as lk:
                lk.execute("BEGIN")
                lk.execute(f"SELECT id FROM {ref} WHERE id = 1 FOR UPDATE")
                out = {}

                def go():
                    try:
                        with psycopg.connect(self.dsn, autocommit=True, **_timeout("6s")) as conn:
                            out["got"] = conn.execute(f"SELECT {table}_load('{path}')").fetchone()[0]
                    except psycopg.Error as exc:
                        out["err"] = query_error(_message(exc))

                load = threading.Thread(target=go)
                load.start()
                for _ in range(100):
                    seen = f"prepared {self.value(LEAK)}, loaders waiting on a row {self.value(ROW_WAIT)}"
                    if seen == "prepared 1, loaders waiting on a row 1":
                        break
                    time.sleep(0.05)
                load.join()
                lk.execute("COMMIT")
        except psycopg.Error as exc:
            return seen, query_error(_message(exc))
        return seen, out.get("got", out.get("err", "no result"))

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

    def outcome(self, sql, table):
        """-> "N rows" (N = `table`'s count) when `sql` ran under a 30 s statement_timeout,
        or the SQLSTATE it raised; a query_error() when the server never answered."""
        try:
            with psycopg.connect(self.dsn, autocommit=True, options="-c statement_timeout=30s") as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            return exc.sqlstate or query_error(_message(exc))
        n = self.value(f"SELECT count(*) FROM {table}")
        return f"{n} rows" if isinstance(n, int) else n

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


def _odd_run(i):
    """A run of 3 backslashes for odd i, of 1 for even i: both escape a newline."""
    return b"\\\\\\" if i % 2 else b"\\"


def _long_rec(i):
    """One bsnl64k.txt record of two lines: 17 bytes for odd i, 15 for even i."""
    return b"a%05d%s\nb%05d\n" % (i, _odd_run(i), i)


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


def _self_lock_copy(dsn, setup, table, path, workers=2):
    """-> "SQLSTATE|message" of the error, or "none|<rows returned>", for BEGIN; setup;
    parallel_copy of path into table with `workers`; COMMIT in one session under a 5 s
    statement_timeout (#2183, #2211). The timeout makes the old wait a 57014 rather
    than a hang. A failure that never reached the server is a query_error()."""
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SET statement_timeout = '5s'")
            conn.execute(setup)
            row = conn.execute(
                f"SELECT pgcolumnar.parallel_copy('{table}'::regclass, '{path}', {workers})").fetchone()
            conn.commit()
    except psycopg.Error as exc:
        if exc.sqlstate is None:
            return query_error(_message(exc))
        return f"{exc.sqlstate}|{exc.diag.message_primary or ''}"
    return f"none|{row[0] if row else None}"


def _own_write_copy(dsn, setup, table, path):
    """-> "SQLSTATE|message|DETAIL" of the error for BEGIN; setup; a 2-worker parallel_copy
    of path into table, under a 5 s statement_timeout (#2211), or what _self_lock_copy
    returns when there is no server error."""
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SET statement_timeout = '5s'")
            conn.execute(setup)
            row = conn.execute(f"SELECT pgcolumnar.parallel_copy('{table}'::regclass, '{path}', 2)").fetchone()
            conn.commit()
    except psycopg.Error as exc:
        if exc.sqlstate is None:
            return query_error(_message(exc))
        return f"{exc.sqlstate}|{exc.diag.message_primary or ''}|{exc.diag.message_detail or ''}"
    return f"none|{row[0] if row else None}"


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
        # odd rows end line1 in a run of 3, even rows in a run of 1: both escape the newline
        bsnl2.write_bytes(b"".join(b"%d\tline1_%d%s\nline2_%d\n" % (i, i, _odd_run(i), i) for i in range(1, 2001)))
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
        # mutants c1 and c2). even.txt and evenp.txt alternate runs of 2 and 4 (a6).
        # evenp.txt puts an unsorted row (id 5) right after row 1500, which ends in a
        # run of 4 and sits in a later partition: the partitioned splitter must refuse it.
        #
        # eodjunk.txt holds rows after \. that COPY would reject: COPY never
        # reads them, so they must not fail the load (mutant c4, the stop at \.).
        even = d / "even.txt"
        even.write_bytes(b"".join((b"v%d\\\\\n" if i % 2 else b"v%d\\\\\\\\\n") % i for i in range(1, 2001)))
        evenp = d / "evenp.txt"
        evenp.write_bytes(b"".join(b"%d\tv%d%s\n" % (i, i, b"\\\\" if i % 2 else b"\\\\\\\\")
                                   + (b"5\tback\n" if i == 1500 else b"")
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

        # Three fixture gaps the review of #2148 at 7a40d479 found; each let a mutant
        # of the new splitter code pass. bsnl64k.txt (single table) mixes runs of 1
        # and 3 and spans two 64 KiB reads: its middle record's run of 3 ends at byte
        # 65535 and the newline it escapes is byte 65536 (e1, e2, n35). multi.txt has
        # records of 3 and 4 lines with the key after them (n27, n45, n08). contp.txt
        # has an unsorted row after continued records (n44). eofrec.txt ends inside a
        # continued record with an unsorted key (n31). eodcrlf.txt is eodjunk.txt with
        # CRLF line ends (n36). twobad.txt has bad rows in two ranges; the load must
        # name the first, as heap COPY does (n04). The shell gives the byte layout.
        long_ = d / "bsnl64k.txt"
        long_.write_bytes(b"".join(_long_rec(i) for i in range(1, 3751)) + b"x" * 5533 + b"\\\\\\\ntail\n"
                          + b"".join(_long_rec(i) for i in range(3751, 7501)))
        multi = d / "multi.txt"
        # Odd records start with a line that is only \ and even records have a middle
        # line that is only \\\, so a run starts at byte 0 of a line (a2).
        multi.write_bytes(b"".join((b"\\\nm%d_b\\\\\\\nm%d_c\t%d\n" % (i, i, i)) if i % 2 else
                                   (b"m%d_a\\\n\\\\\\\nm%d_c\\\\\\\nm%d_d\t%d\n" % (i, i, i, i))
                                   for i in range(1, 2001)))
        contp = d / "contp.txt"
        contp.write_bytes(b"".join(b"%d\tline1_%d%s\nline2_%d\n" % (i, i, _odd_run(i), i)
                                   + (b"5\tback\n" if i == 1500 else b"") for i in range(1, 2001)))
        eofrec = d / "eofrec.txt"
        eofrec.write_bytes(b"".join(b"%d\tx%d\n" % (i, i) for i in range(1, 2001)) + b"5\tlast\\\\\\\n")
        eodcrlf = d / "eodcrlf.txt"
        eodcrlf.write_bytes(b"".join(b"%d\tx\r\n" % i for i in range(1, 1001)) + b"\\.\r\n"
                            + b"notanint\tz\r\n" * 1000)
        twobad = d / "twobad.txt"
        twobad.write_bytes(b"".join(b"bad_early\tz\n" if i == 1501 else b"bad_late\tz\n" if i == 3501
                                    else b"%d\tx%d\n" % (i, i) for i in range(1, 5001)))
        for f_ in (long_, multi, contp, eofrec, eodcrlf, twobad):
            os.chmod(f_, 0o644)
        db.run(f"CREATE TABLE h_long (t text); COPY h_long FROM '{long_}'; "
               f"CREATE TABLE h_multi (txt text, id int); COPY h_multi FROM '{multi}'; "
               f"CREATE TABLE h_eodcrlf (id int, txt text); COPY h_eodcrlf FROM '{eodcrlf}'; "
               "CREATE TABLE c_long (t text) USING pgcolumnar; "
               "CREATE TABLE c_eodcrlf (id int, txt text) USING pgcolumnar; "
               "CREATE TABLE c_twobad (id int, txt text) USING pgcolumnar; "
               "CREATE TABLE t_multip (txt text, id int) PARTITION BY RANGE (id); "
               "CREATE TABLE t_multip_a PARTITION OF t_multip FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar; "
               "CREATE TABLE t_multip_b PARTITION OF t_multip FOR VALUES FROM (500) TO (1000) USING pgcolumnar; "
               "CREATE TABLE t_multip_c PARTITION OF t_multip FOR VALUES FROM (1000) TO (1500) USING pgcolumnar; "
               "CREATE TABLE t_multip_d PARTITION OF t_multip FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar")
        _mkpart(db, "t_contp", 4)
        _mkpart(db, "t_eofrecp", 4)
        _mkpart(db, "t_eodcrlfp", 4)
        data = _read(long_)
        counts = db.value("SELECT concat_ws(' ', (SELECT count(*) FROM h_bsnl), (SELECT count(*) FROM h_bsnl2), "
                          "(SELECT count(*) FROM h_eod), (SELECT count(*) FROM h_even), (SELECT count(*) FROM h_eodj), "
                          "(SELECT count(*) FROM h_long), (SELECT count(*) FROM h_multi), "
                          "(SELECT count(*) FROM h_eodcrlf))")
        r["2084 premise"] = (f"{counts} {len(data)} {data[65535:65537].hex()}" if isinstance(data, bytes)
                             else data)
        for w in (2, 4):
            offs = db.offsets(long_, w)
            r[f"2084 long offsets {w}"] = " ".join(map(str, offs)) if isinstance(offs, list) else offs
        r["2084 contp unsorted"] = db.sqlstate(f"SELECT pgcolumnar.parallel_copy('t_contp'::regclass, '{contp}', 4)")
        r["2084 eofrec unsorted"] = db.sqlstate(
            f"SELECT pgcolumnar.parallel_copy('t_eofrecp'::regclass, '{eofrec}', 4)")
        for key, tgt, src, heap in (("long single", "c_long", long_, "h_long"),
                                    ("multi part", "t_multip", multi, "h_multi"),
                                    ("eodcrlf part", "t_eodcrlfp", eodcrlf, "h_eodcrlf"),
                                    ("eodcrlf single", "c_eodcrlf", eodcrlf, "h_eodcrlf")):
            r[f"2084 {key} rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('{tgt}'::regclass, '{src}', 4)")
            r[f"2084 {key} rows want"] = db.value(f"SELECT count(*) FROM {heap}")
            r[f"2084 {key} hash"] = db.set_hash(f"SELECT * FROM {tgt}")
            r[f"2084 {key} hash want"] = db.set_hash(f"SELECT * FROM {heap}")
        r["2084 twobad"] = db.bad_names(f"SELECT pgcolumnar.parallel_copy('c_twobad'::regclass, '{twobad}', 4)")

        # Three more gaps, from the review of #2148 at 29b71633. esc.txt has an escape
        # of a byte that is not a newline (\t) before an escaped newline (a1). In
        # eodstart.txt the \. line sits exactly on the first interior split offset for
        # 4 workers, with valid rows after it (a8). The shell gives the byte layout.
        esc = d / "esc.txt"
        esc.write_bytes(b"".join(b"x%d\\tq\\\ny%d\n" % (i, i) for i in range(1, 2001)))
        eodstart = d / "eodstart.txt"
        eodstart.write_bytes(b"".join(b"%04d\tx\n" % i for i in range(1, 501)) + b"\\.\n" + b"9999\ty\n" * 1499)
        for f_ in (esc, eodstart):
            os.chmod(f_, 0o644)
        db.run(f"CREATE TABLE h_esc (t text); COPY h_esc FROM '{esc}'; "
               f"CREATE TABLE h_eodstart (id int, txt text); COPY h_eodstart FROM '{eodstart}'; "
               "CREATE TABLE c_esc (t text) USING pgcolumnar; "
               "CREATE TABLE c_eodstart (id int, txt text) USING pgcolumnar")
        data = _read(eodstart)
        counts = db.value("SELECT concat_ws(' ', (SELECT count(*) FROM h_esc), (SELECT count(*) FROM h_eodstart))")
        r["2084 premise 2"] = f"{counts} {len(data)} {data[3500:3503].hex()}" if isinstance(data, bytes) else data
        for key, path in (("esc", esc), ("eodstart", eodstart)):
            offs = db.offsets(path, 4)
            r[f"2084 {key} offsets 4"] = " ".join(map(str, offs)) if isinstance(offs, list) else offs
        for key, tgt, src, heap in (("esc single", "c_esc", esc, "h_esc"),
                                    ("eodstart single", "c_eodstart", eodstart, "h_eodstart")):
            r[f"2084 {key} rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('{tgt}'::regclass, '{src}', 4)")
            r[f"2084 {key} rows want"] = db.value(f"SELECT count(*) FROM {heap}")
            r[f"2084 {key} hash"] = db.set_hash(f"SELECT * FROM {tgt}")
            r[f"2084 {key} hash want"] = db.set_hash(f"SELECT * FROM {heap}")

        # Two more gaps, from the review of #2148 at 7e71ab06. eod.txt loaded twice with
        # dedup on: nothing but the rollback after the marker resolves the ranges after
        # it (m1). contdot.txt (partitioned) puts a line that is only \. inside a
        # continued record: PG 17's COPY loads every row, PG 18's refuses with 22P04
        # (m5, which only PG 17 shows). The shell gives the byte layout.
        db.run("CREATE TABLE c_eodd (id int, txt text) USING pgcolumnar")
        for n in (1, 2):
            r[f"2084 eodd load {n}"] = db.value(f"SELECT pgcolumnar.parallel_copy('c_eodd'::regclass, '{eod}', 4, true)")
            r[f"2084 eodd leak {n}"] = db.value(LEAK)
        r["2084 eodd table"] = db.value("SELECT count(*) FROM c_eodd")
        r["2084 eodd want"] = db.value("SELECT count(*) FROM h_eod")
        # A leaked range holds locks for the rest of the run, so resolve any here.
        gids = db.value("SELECT coalesce(string_agg(gid, ' '), '') FROM pg_prepared_xacts"
                        " WHERE database = current_database()")
        for gid in (gids.split() if isinstance(gids, str) else ()):
            db.run(f"ROLLBACK PREPARED '{gid}'")
        contdot = d / "contdot.txt"
        contdot.write_bytes(b"".join(b"%d\tx\n" % i for i in range(1, 1000)) + b"999\tcont\\\n\\.\n"
                            + b"".join(b"%d\ty\n" % i for i in range(1000, 2000)))
        os.chmod(contdot, 0o644)
        db.run("CREATE TABLE h_contdot (id int, txt text); "
               "CREATE TABLE t_contdotp (id int, txt text) PARTITION BY RANGE (id); "
               "CREATE TABLE t_contdotp_a PARTITION OF t_contdotp FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar; "
               "CREATE TABLE t_contdotp_b PARTITION OF t_contdotp FOR VALUES FROM (500) TO (1000) USING pgcolumnar; "
               "CREATE TABLE t_contdotp_c PARTITION OF t_contdotp FOR VALUES FROM (1000) TO (1500) USING pgcolumnar; "
               "CREATE TABLE t_contdotp_d PARTITION OF t_contdotp FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar")
        srv = db.value("SHOW server_version_num")
        r["2084 contdot want"] = "22P04" if str(srv).isdigit() and int(srv) >= 180000 else "2000 rows"
        r["2084 contdot heap"] = db.outcome(f"COPY h_contdot FROM '{contdot}'", "h_contdot")
        r["2084 contdot part"] = db.outcome(
            f"SELECT pgcolumnar.parallel_copy('t_contdotp'::regclass, '{contdot}', 4)", "t_contdotp")
        r["2084 contdot hash"] = db.set_hash("SELECT * FROM t_contdotp")
        r["2084 contdot hash want"] = db.set_hash("SELECT * FROM h_contdot")

        # Two more gaps, from the review of #2148 at 33724a76. eodcr.txt (\.\r\n in an
        # LF file) and eodnonl.txt (\. at the end with no newline) are partitioned loads
        # heap COPY refuses with 22P04; the last range must keep the marker line so its
        # loader refuses too (n22, n55). eodearly.txt has the \. line in the first range
        # (n57). The shell gives the byte layout.
        eodcr = d / "eodcr.txt"
        eodcr.write_bytes(b"".join(b"%d\tx\n" % i for i in range(1, 1001)) + b"\\.\r\n"
                          + b"".join(b"%d\ty\n" % i for i in range(1001, 2001)))
        eodnonl = d / "eodnonl.txt"
        eodnonl.write_bytes(b"".join(b"%d\tx\n" % i for i in range(1, 1001)) + b"\\.")
        eodearly = d / "eodearly.txt"
        eodearly.write_bytes(b"".join(b"%d\tx\n" % i for i in range(1, 101)) + b"\\.\n"
                             + b"".join(b"%d\tx\n" % i for i in range(101, 2001)))
        for f_ in (eodcr, eodnonl, eodearly):
            os.chmod(f_, 0o644)
        db.run("CREATE TABLE h_eodcr (id int, txt text); CREATE TABLE h_eodnonl (id int, txt text); "
               f"CREATE TABLE h_eodearly (id int, txt text); COPY h_eodearly FROM '{eodearly}'; "
               "CREATE TABLE c_eodearly (id int, txt text) USING pgcolumnar")
        for t in ("t_eodcrp", "t_eodnonlp"):
            db.run(f"CREATE TABLE {t} (id int, txt text) PARTITION BY RANGE (id); "
                   f"CREATE TABLE {t}_a PARTITION OF {t} FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar; "
                   f"CREATE TABLE {t}_b PARTITION OF {t} FOR VALUES FROM (500) TO (1000) USING pgcolumnar; "
                   f"CREATE TABLE {t}_c PARTITION OF {t} FOR VALUES FROM (1000) TO (1500) USING pgcolumnar; "
                   f"CREATE TABLE {t}_d PARTITION OF {t} FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar")
        for key, path in (("eodcr", eodcr), ("eodnonl", eodnonl)):
            r[f"2084 {key} heap"] = db.outcome(f"COPY h_{key} FROM '{path}'", f"h_{key}")
            r[f"2084 {key} part"] = db.outcome(
                f"SELECT pgcolumnar.parallel_copy('t_{key}p'::regclass, '{path}', 4)", f"t_{key}p")
            r[f"2084 {key} left"] = f"{db.value(f'SELECT count(*) FROM t_{key}p')} {db.value(LEAK)}"
        data = _read(eodearly)
        offs = db.offsets(eodearly, 4)
        r["2084 eodearly premise"] = (
            f"{db.value('SELECT count(*) FROM h_eodearly')} {len(data)} {data[492:495].hex()} "
            f"{' '.join(map(str, offs)) if isinstance(offs, list) else offs}" if isinstance(data, bytes) else data)
        r["2084 eodearly rows"] = db.value(f"SELECT pgcolumnar.parallel_copy('c_eodearly'::regclass, '{eodearly}', 4)")
        r["2084 eodearly rows want"] = db.value("SELECT count(*) FROM h_eodearly")
        r["2084 eodearly hash"] = db.set_hash("SELECT * FROM c_eodearly")
        r["2084 eodearly hash want"] = db.set_hash("SELECT * FROM h_eodearly")
        r["2084 eodearly leak"] = db.value(LEAK)

        # ---- #2080: a single table with a unique index needs one worker ---------
        # statement_timeout turns the old hang into 57014, a red the test can read.
        # t_uk's second, non-unique index comes after its primary key, so the
        # refusal must find the unique index when it is not the last one. t_ui's
        # non-unique index comes before its unique one, so the refusal must also
        # find the unique index when it is not the first one. t_ui's unique index
        # has two key columns, so the refusal must not count only one-column ones.
        db.run("CREATE TABLE t_uk (id int PRIMARY KEY, txt text) USING pgcolumnar; "
               "CREATE INDEX t_uk_txt ON t_uk (txt); "
               "CREATE TABLE t_ui (id int, txt text) USING pgcolumnar; "
               "CREATE INDEX t_ui_txt ON t_ui (txt); "
               "CREATE UNIQUE INDEX t_ui_id ON t_ui (id, txt); "
               "CREATE TABLE t_uk1 (id int PRIMARY KEY, txt text) USING pgcolumnar; "
               "CREATE TABLE t_nu (id int, txt text) USING pgcolumnar; "
               "CREATE INDEX t_nu_id ON t_nu (id)")
        r["unique pk"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '{f}', 2)", timeout="10s")
        r["unique index"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_ui'::regclass, '{f}', 4)", timeout="10s")
        # An exclusion constraint: indisunique false, indisexclusion true. Its keys
        # are distinct, so a load that is not refused would succeed.
        db.run("CREATE TABLE t_ux (id int, txt text, EXCLUDE USING btree (id WITH =)) USING pgcolumnar")
        r["exclusion"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_ux'::regclass, '{f}', 2)", timeout="10s")
        # A deferrable unique index and a partial unique index whose predicate
        # every row matches are refused too. The keys are distinct, so a
        # deferrable load that is not refused would succeed.
        db.run("CREATE TABLE t_ud (id int UNIQUE DEFERRABLE INITIALLY DEFERRED, txt text) USING pgcolumnar; "
               "CREATE TABLE t_up (id int, txt text) USING pgcolumnar; "
               "CREATE UNIQUE INDEX t_up_id ON t_up (id) WHERE id >= 0")
        r["deferrable"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_ud'::regclass, '{f}', 2)", timeout="10s")
        r["partial"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_up'::regclass, '{f}', 2)", timeout="10s")
        # A deferrable exclusion constraint: indisunique and indimmediate are
        # false, so only indisexclusion catches it. An expression unique index:
        # the loaders hash its key into their unique locks, so two loaders wait
        # on each other even with distinct keys. The keys are distinct, so a load
        # that is not refused would succeed or hang, and either is red.
        db.run("CREATE TABLE t_uxd (id int, txt text, "
               "EXCLUDE USING btree (id WITH =) DEFERRABLE INITIALLY DEFERRED) USING pgcolumnar; "
               "CREATE TABLE t_ue (id int, txt text) USING pgcolumnar; "
               "CREATE UNIQUE INDEX t_ue_id ON t_ue ((id + 0))")
        r["deferrable exclusion"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_uxd'::regclass, '{f}', 2)", timeout="10s")
        r["expression"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_ue'::regclass, '{f}', 2)", timeout="10s")
        # A gist exclusion constraint, whose ranges do not overlap: the refusal
        # must not look at the access method. A NULLS NOT DISTINCT unique index
        # with an INCLUDE column: the refusal must not compare indnatts with
        # indnkeyatts or read indnullsnotdistinct.
        db.run("CREATE TABLE t_gx (id int, txt text, "
               "EXCLUDE USING gist (int4range(id, id, '[]') WITH &&)) USING pgcolumnar; "
               "CREATE TABLE t_inc (id int, txt text, "
               "UNIQUE NULLS NOT DISTINCT (id) INCLUDE (txt)) USING pgcolumnar")
        r["gist exclusion"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_gx'::regclass, '{f}', 2)", timeout="10s")
        r["include"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_inc'::regclass, '{f}', 2)", timeout="10s")
        # The refusal keeps its place in the order of checks: 42501 for a caller
        # without INSERT first (#563), the #1886 refusal for a URL first, and
        # dedup => true does not skip the calling backend's refusal.
        db.run("DO $$ BEGIN "
               "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pcopy_noins') THEN "
               "CREATE ROLE pcopy_noins; END IF; END $$; "
               "GRANT pg_read_server_files TO pcopy_noins; "
               "GRANT USAGE ON SCHEMA pgcolumnar TO pcopy_noins; "
               "GRANT SELECT ON t_uk TO pcopy_noins")
        r["no insert"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '{f}', 2)",
            timeout="10s", options="-c role=pcopy_noins")
        r["unique url"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '{REMOTE_S3}', 2)", timeout="10s")
        r["unique dedup"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '{f}', 2, dedup => true)", timeout="10s")
        # An invalid unique index counts too. CREATE UNIQUE INDEX CONCURRENTLY
        # fails on a columnar table and leaves the index invalid but ready, and a
        # ready unique index still checks every insert. The keys are distinct, so
        # a load that is not refused would succeed.
        db.run("CREATE TABLE t_uv (id int, txt text) USING pgcolumnar")
        db.run("CREATE UNIQUE INDEX CONCURRENTLY t_uv_id ON t_uv (id)")
        r["invalid premise"] = db.value(
            "SELECT concat_ws('|', indisunique, indisvalid, indisready) FROM pg_index "
            "WHERE indrelid = 't_uv'::regclass")
        r["invalid"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_uv'::regclass, '{f}', 2)", timeout="10s")
        # The default worker count is more than one here: max_parallel_workers=4
        # gives 2. The calling backend must refuse it with its hint.
        db.run("CREATE TABLE t_ukd (id int PRIMARY KEY, txt text) USING pgcolumnar")
        r["default workers"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_ukd'::regclass, '{f}')",
            timeout="10s", options="-c max_parallel_workers=4")
        r["unique empty"] = db.value(
            "SELECT (SELECT count(*) FROM t_uk) + (SELECT count(*) FROM t_ui) + (SELECT count(*) FROM t_ud) "
            "+ (SELECT count(*) FROM t_up) + (SELECT count(*) FROM t_uv) + (SELECT count(*) FROM t_ukd) "
            "+ (SELECT count(*) FROM t_ue) + (SELECT count(*) FROM t_inc)")
        r["exclusion empty"] = db.value("SELECT (SELECT count(*) FROM t_ux) + (SELECT count(*) FROM t_uxd) "
                                        "+ (SELECT count(*) FROM t_gx)")
        r["unique one worker"] = db.value(
            f"SELECT pgcolumnar.parallel_copy('t_uk1'::regclass, '{f}', 1)", timeout="30s")
        r["non-unique index"] = db.value(
            f"SELECT pgcolumnar.parallel_copy('t_nu'::regclass, '{f}', 2)", timeout="30s")
        # A partitioned target with a primary key keeps more than one worker: each
        # worker loads distinct partitions. Its parent's index is unique, so a
        # refusal that also covered partitioned targets would refuse this.
        db.run("CREATE TABLE t_ppk (id int PRIMARY KEY, txt text) PARTITION BY RANGE (id); "
               "CREATE TABLE t_ppk_1 PARTITION OF t_ppk FOR VALUES FROM (MINVALUE) TO (1251) USING pgcolumnar; "
               "CREATE TABLE t_ppk_2 PARTITION OF t_ppk FOR VALUES FROM (1251) TO (2501) USING pgcolumnar; "
               "CREATE TABLE t_ppk_3 PARTITION OF t_ppk FOR VALUES FROM (2501) TO (3751) USING pgcolumnar; "
               "CREATE TABLE t_ppk_4 PARTITION OF t_ppk FOR VALUES FROM (3751) TO (MAXVALUE) USING pgcolumnar")
        loaded = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_ppk'::regclass, '{f}', 4)", timeout="30s")
        r["partitioned pk"] = f"{loaded} | {db.value('SELECT count(*) FROM t_ppk')}"

        # A unique index created after the calling backend's check, before the
        # loaders open the table. A second session holds SHARE on t_ur: the check
        # passes, and the coordinator stops at ROW EXCLUSIVE before any loader
        # starts. Once it waits, the session creates the unique index and commits.
        db.run("CREATE TABLE t_ur (id int, txt text) USING pgcolumnar")
        r["unique late"] = db.unique_index_after_check(f)
        r["unique late empty"] = db.value("SELECT count(*) FROM t_ur")
        # The same with 4 workers, where every loader must refuse. A row trigger
        # that calls nextval records any insert, because a sequence does not roll
        # back: no loader may insert a row. The trigger arrives with the index,
        # after the calling backend's check, which would refuse the trigger
        # itself (#2187). Each loader checks the index first.
        db.run("CREATE TABLE t_ur4 (id int, txt text) USING pgcolumnar; "
               "CREATE SEQUENCE t_ur4_rows; "
               "CREATE FUNCTION t_ur4_row() RETURNS trigger LANGUAGE plpgsql "
               "AS $$ BEGIN PERFORM nextval('t_ur4_rows'); RETURN NEW; END $$")
        late4 = db.unique_index_after_check(f, "t_ur4", 4, ddl=[
            "CREATE UNIQUE INDEX t_ur4_id ON t_ur4 (id)",
            "CREATE TRIGGER t_ur4_row BEFORE INSERT ON t_ur4 FOR EACH ROW EXECUTE FUNCTION t_ur4_row()"])
        inserted = db.value("SELECT CASE WHEN is_called THEN 'a loader inserted a row' "
                            "ELSE 'no row inserted' END FROM t_ur4_rows")
        r["unique late 4"] = f"{late4} | {inserted}"
        # ---- #2200: a temporal primary key is a unique AND an exclusion index ----
        # PostgreSQL 18's PRIMARY KEY (id, during WITHOUT OVERLAPS) builds one index
        # with indisunique and indisexclusion both true. With the refusal testing
        # `indisunique != indisexclusion`, every other test here passed and this load
        # of distinct keys hung until its statement_timeout (57014). The file has a
        # third column, so the rows fit: a load that is not refused would run. The int
        # key part needs btree_gist; without it the table is never made and the test
        # reads that failure. WITHOUT OVERLAPS does not exist before 18.
        r["major"] = db.value("SELECT current_setting('server_version_num')::int / 10000")
        if isinstance(r["major"], int) and r["major"] >= 18:
            tpk = _server_file(db, d, "temporal_pk.txt",
                               "SELECT g, 'host_'||g, daterange(date '2020-01-01' + g, date '2020-01-01' + g + 1) "
                               "FROM generate_series(1, 5000) g")
            try:
                with psycopg.connect(db.dsn, autocommit=True) as conn:
                    conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gist; "
                                 "CREATE TABLE t_utp (id int, txt text, during daterange, "
                                 "PRIMARY KEY (id, during WITHOUT OVERLAPS)) USING pgcolumnar")
            except psycopg.Error as exc:
                r["temporal pk"] = f"setup failed: {exc.sqlstate} {_message(exc)}"
            else:
                refusal = db.remote_refusal(
                    f"SELECT pgcolumnar.parallel_copy('t_utp'::regclass, '{tpk}', 2)", timeout="10s")
                r["temporal pk"] = f"{refusal} | {db.value('SELECT count(*) FROM t_utp')} rows"

        # ---- #2187: a lock queued between the loaders, a trigger, a cancel ---------
        # 1. CREATE INDEX queued after the first loader's lock and before the others'.
        db.run("CREATE TABLE t_mo (id int, txt text) USING pgcolumnar; "
               "CREATE TABLE t_mo4 (id int, txt text) USING pgcolumnar")
        r["mid open seen"], r["mid open"] = db.index_mid_open(f, "t_mo", 2, "CREATE UNIQUE INDEX t_mo_id ON t_mo (id)")
        r["mid open 4 seen"], r["mid open 4"] = db.index_mid_open(f, "t_mo4", 4, "CREATE INDEX t_mo4_id ON t_mo4 (id)")
        # A loader that exits before it opens the table.
        db.run("CREATE TABLE t_gone (id int, txt text) USING pgcolumnar")
        r["gone"] = db.gone_load(f, "t_gone")
        # 2. A cancelled load, with one loader prepared and one waiting on a row.
        db.run("CREATE TABLE t_cx_ref (id int PRIMARY KEY); "
               "INSERT INTO t_cx_ref SELECT generate_series(1, 5000); "
               "CREATE TABLE t_cx (id int REFERENCES t_cx_ref, txt text) USING pgcolumnar; "
               "CREATE FUNCTION t_cx_load(f text) RETURNS text LANGUAGE plpgsql AS $$ "
               "BEGIN PERFORM pgcolumnar.parallel_copy('t_cx'::regclass, f, 2); RETURN 'no error'; "
               "EXCEPTION WHEN query_canceled THEN RETURN SQLSTATE || ' ' || SQLERRM || ' | same session ' || "
               "(SELECT count(*) FROM pg_prepared_xacts WHERE database = current_database()); END $$")
        r["cancel seen"], cancelled = db.cancelled_load(f, "t_cx", "t_cx_ref")
        r["cancel"] = f"{cancelled} | after {db.value(LEAK)}"
        # 3. Triggers on INSERT.
        db.run("CREATE TABLE t_tgk (id int PRIMARY KEY) USING pgcolumnar; "
               "CREATE FUNCTION t_tg_row() RETURNS trigger LANGUAGE plpgsql "
               "AS $$ BEGIN INSERT INTO t_tgk VALUES (NEW.id); RETURN NEW; END $$; "
               "CREATE FUNCTION t_tg_stmt() RETURNS trigger LANGUAGE plpgsql "
               "AS $$ BEGIN INSERT INTO t_tgk SELECT -pg_backend_pid(); RETURN NULL; END $$; "
               "CREATE TABLE t_tga (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tga_k AFTER INSERT ON t_tga FOR EACH ROW EXECUTE FUNCTION t_tg_row(); "
               "CREATE TABLE t_tgb (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tgb_k BEFORE INSERT ON t_tgb FOR EACH ROW EXECUTE FUNCTION t_tg_row(); "
               "CREATE TABLE t_tgs (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tgs_k AFTER INSERT ON t_tgs FOR EACH STATEMENT EXECUTE FUNCTION t_tg_stmt()")
        r["trigger after"] = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_tga'::regclass, '{f}', 2)", timeout="10s")
        r["trigger before"] = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_tgb'::regclass, '{f}', 2)", timeout="10s")
        r["trigger stmt"] = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_tgs'::regclass, '{f}', 4)", timeout="10s")
        # A trigger that writes a columnar table with no index and no storage row yet.
        db.run("CREATE TABLE t_tgn_log (id int) USING pgcolumnar; "
               "CREATE FUNCTION t_tg_fresh() RETURNS trigger LANGUAGE plpgsql "
               "AS $$ BEGIN INSERT INTO t_tgn_log VALUES (NEW.id); RETURN NEW; END $$; "
               "CREATE TABLE t_tgn (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tgn_k AFTER INSERT ON t_tgn FOR EACH ROW EXECUTE FUNCTION t_tg_fresh()")
        fresh = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_tgn'::regclass, '{f}', 2)", timeout="10s")
        r["trigger fresh"] = (f"{fresh} | {db.value('SELECT count(*) FROM t_tgn')} "
                              f"{db.value('SELECT count(*) FROM t_tgn_log')} | leak {db.value(LEAK)}")
        r["trigger empty"] = (f"{db.value('SELECT count(*) FROM t_tga')} {db.value('SELECT count(*) FROM t_tgb')} "
                              f"{db.value('SELECT count(*) FROM t_tgs')} | {db.value('SELECT count(*) FROM t_tgk')} | "
                              f"leak {db.value(LEAK)}")
        db.run("CREATE TABLE t_tgl (id int, txt text) USING pgcolumnar")
        late_tg = db.unique_index_after_check(f, "t_tgl", 2, ddl=[
            "CREATE TRIGGER t_tgl_k AFTER INSERT ON t_tgl FOR EACH ROW EXECUTE FUNCTION t_tg_row()"])
        r["trigger late"] = (f"{late_tg} | {db.value('SELECT count(*) FROM t_tgl')} "
                             f"{db.value('SELECT count(*) FROM t_tgk')}")
        db.run("CREATE TABLE t_tg1 (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tg1_k AFTER INSERT ON t_tg1 FOR EACH ROW EXECUTE FUNCTION t_tg_row(); "
               "CREATE TABLE t_tgd (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tgd_k AFTER INSERT ON t_tgd FOR EACH ROW EXECUTE FUNCTION t_tg_row(); "
               "ALTER TABLE t_tgd DISABLE TRIGGER t_tgd_k; "
               "CREATE TABLE t_tgf_ref (id int PRIMARY KEY); "
               "INSERT INTO t_tgf_ref SELECT generate_series(1, 5000); "
               "CREATE TABLE t_tgf (id int REFERENCES t_tgf_ref, txt text) USING pgcolumnar; "
               "CREATE TABLE t_tgu (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tgu_k AFTER UPDATE ON t_tgu FOR EACH ROW EXECUTE FUNCTION t_tg_row()")
        one = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('t_tg1'::regclass, '{f}', 1)", timeout="30s")
        r["trigger one"] = f"{one} | {db.value('SELECT count(*) FROM t_tgk')}"
        for key, t in (("trigger disabled", "t_tgd"), ("trigger fk", "t_tgf"), ("trigger update", "t_tgu")):
            got = db.remote_refusal(f"SELECT pgcolumnar.parallel_copy('{t}'::regclass, '{f}', 2)", timeout="30s")
            r[key] = f"{got} | {db.value(f'SELECT count(*) FROM {t}')}"

        # ---- #2092: a load into a table with a projection fills the projection ----
        r["projection"] = " ".join(_projection_load(db, f, w, pre) for w, pre in ((1, 0), (2, 1000)))
        r["projection leak"] = db.value(LEAK)
        r["projection up"] = _survived(db, pgc_cluster, before)

        # ---- #2183: a lock the caller holds is refused, not waited on -------------
        db.run("CREATE TABLE t_lkc (id int, txt text) USING pgcolumnar; CREATE TABLE t_lkc_other (a int)")
        _mkpart(db, "t_lkp", 4)
        for key, setup, table in (
                ("lk ael", "LOCK TABLE t_lkc IN ACCESS EXCLUSIVE MODE", "t_lkc"),
                ("lk trunc", "TRUNCATE t_lkc", "t_lkc"),
                ("lk share", "LOCK TABLE t_lkc IN SHARE MODE", "t_lkc"),
                ("lk rowex", "LOCK TABLE t_lkc IN ROW EXCLUSIVE MODE", "t_lkc"),
                ("lk other", "LOCK TABLE t_lkc_other IN ACCESS EXCLUSIVE MODE", "t_lkc"),
                ("lk part", "LOCK TABLE t_lkp_p2 IN ACCESS EXCLUSIVE MODE", "t_lkp")):
            r[key] = _self_lock_copy(db.dsn, setup, table, f)
        r["lk after"] = (f"{db.value('SELECT count(*) FROM t_lkc')} rows, "
                         f"{db.value('SELECT count(*) FROM t_lkp')} partitioned rows, {db.value(LEAK)} prepared")
        # the order of the refusals (#2183 with #2080): the lock refusal before the
        # unique-index one, and a caller without INSERT gets 42501 before both
        db.run("CREATE TABLE t_lko (id int PRIMARY KEY, txt text) USING pgcolumnar; "
               "GRANT SELECT, UPDATE ON t_lko TO pcopy_noins")
        r["lk order unique"] = _self_lock_copy(db.dsn, "LOCK TABLE t_lko IN SHARE MODE", "t_lko", f)
        r["lk order acl"] = _self_lock_copy(db.dsn, "SET ROLE pcopy_noins; LOCK TABLE t_lko IN SHARE MODE", "t_lko", f)
        # the trigger refusal (#2187) after the lock refusal and after the unique-index one
        db.run("CREATE TABLE t_lkt (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_lkt_k AFTER INSERT ON t_lkt FOR EACH ROW EXECUTE FUNCTION t_tg_row(); "
               "CREATE TABLE t_tgpk (id int PRIMARY KEY, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_tgpk_k AFTER INSERT ON t_tgpk FOR EACH ROW EXECUTE FUNCTION t_tg_row()")
        r["lk order trigger"] = _self_lock_copy(db.dsn, "LOCK TABLE t_lkt IN SHARE MODE", "t_lkt", f)
        r["order trigger unique"] = db.remote_refusal(
            f"SELECT pgcolumnar.parallel_copy('t_tgpk'::regclass, '{f}', 2)", timeout="10s")
        # the rest of the conflict table, and the partition tree in depth (#2212 review)
        db.run("CREATE TABLE t_lk2 (id int, txt text) PARTITION BY RANGE (id); "
               "CREATE TABLE t_lk2_a PARTITION OF t_lk2 FOR VALUES FROM (MINVALUE) TO (2501) PARTITION BY RANGE (id); "
               "CREATE TABLE t_lk2_a1 PARTITION OF t_lk2_a FOR VALUES FROM (MINVALUE) TO (1251) USING pgcolumnar; "
               "CREATE TABLE t_lk2_a2 PARTITION OF t_lk2_a FOR VALUES FROM (1251) TO (2501) USING pgcolumnar; "
               "CREATE TABLE t_lk2_b PARTITION OF t_lk2 FOR VALUES FROM (2501) TO (MAXVALUE) USING pgcolumnar")
        for key, setup, table in (
                ("lk sue", "LOCK TABLE t_lkc IN SHARE UPDATE EXCLUSIVE MODE", "t_lkc"),
                ("lk excl", "LOCK TABLE t_lkc IN EXCLUSIVE MODE", "t_lkc"),
                ("lk sre", "LOCK TABLE t_lkc IN SHARE ROW EXCLUSIVE MODE", "t_lkc"),
                ("lk only parent", "LOCK TABLE ONLY t_lkp IN SHARE MODE", "t_lkp"),
                ("lk only mid", "LOCK TABLE ONLY t_lk2_a IN ACCESS EXCLUSIVE MODE", "t_lk2"),
                ("lk deep leaf", "LOCK TABLE t_lk2_a1 IN SHARE MODE", "t_lk2")):
            r[key] = _self_lock_copy(db.dsn, setup, table, f)

        # ---- #2211: the caller's own write to the target is refused, not waited on --
        db.run("CREATE TABLE t_ownw (id int, txt text) USING pgcolumnar; "
               "CREATE TABLE t_ownw1 (id int, txt text) USING pgcolumnar; "
               "INSERT INTO t_ownw1 VALUES (-1, 'committed'); "
               "CREATE TABLE t_ownw_other (id int, txt text) USING pgcolumnar")
        _mkpart(db, "t_ownp", 4)
        r["ownw premise"] = db.value(
            "SELECT count(*) FILTER (WHERE relation_oid = 't_ownw'::regclass) || '/' || "
            "count(*) FILTER (WHERE relation_oid = 't_ownw1'::regclass) FROM pgcolumnar.storage")
        for key, setup, table, workers in (
                ("ownw single", "INSERT INTO t_ownw VALUES (0, 'pre')", "t_ownw", 2),
                ("ownw one worker", "INSERT INTO t_ownw VALUES (0, 'pre')", "t_ownw", 1),
                ("ownw committed row", "INSERT INTO t_ownw1 VALUES (0, 'pre')", "t_ownw1", 2),
                ("ownw partition", "INSERT INTO t_ownp VALUES (0, 'pre')", "t_ownp", 2)):
            r[key] = _self_lock_copy(db.dsn, setup, table, f, workers)
        r["ownw after"] = db.value(
            "SELECT (SELECT count(*) FROM t_ownw) || ' rows, ' || (SELECT count(*) FROM t_ownw1) || ' rows, ' || "
            "(SELECT count(*) FROM t_ownp) || ' partitioned rows, ' || "
            "(SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 't_ownw'::regclass) || ' storage rows, ' || "
            "(SELECT count(*) FROM pg_prepared_xacts) || ' prepared'")
        for key, setup, table in (
                ("ownw other", "INSERT INTO t_ownw_other VALUES (0, 'pre')", "t_ownw"),
                ("ownw none", "SELECT 1", "t_ownw1"),
                ("ownw none partitioned", "SELECT 1", "t_ownp")):
            r[key] = _self_lock_copy(db.dsn, setup, table, f)
        # which partition, and in what order (#2214 review)
        db.run("CREATE TABLE t_ownu (id int PRIMARY KEY, txt text) USING pgcolumnar")
        r["ownw last partition"] = _own_write_copy(db.dsn, "INSERT INTO t_ownp VALUES (4000, 'x')", "t_ownp", f)
        for key, setup, table in (
                ("ownw later partition", "INSERT INTO t_ownp_p2 VALUES (3000, 'x')", "t_ownp"),
                ("ownw deep leaf", "INSERT INTO t_lk2 VALUES (2000, 'x')", "t_lk2"),
                ("ownw order unique", "INSERT INTO t_ownu VALUES (0, 'pre')", "t_ownu"),
                ("ownw order lock",
                 "LOCK TABLE t_ownw IN ACCESS EXCLUSIVE MODE; INSERT INTO t_ownw VALUES (0, 'pre')", "t_ownw")):
            r[key] = _self_lock_copy(db.dsn, setup, table, f)
        # the trigger refusal (#2187) after the own-write refusal too
        db.run("CREATE TABLE t_owntg (id int, txt text) USING pgcolumnar; "
               "CREATE TRIGGER t_owntg_k AFTER INSERT ON t_owntg FOR EACH ROW EXECUTE FUNCTION t_tg_row()")
        r["ownw order trigger"] = _self_lock_copy(db.dsn, "INSERT INTO t_owntg VALUES (-7, 'pre')", "t_owntg", f)
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
    ("long single",
     "#2084 odd backslash run across a 64 KiB read / single table, 4 workers: rows returned = heap COPY's 7501"),
    ("multi part",
     "#2084 records of 3 and 4 lines, key after them / partitioned, 4 workers: rows returned = heap COPY's 2000"),
    ("eodcrlf part",
     "#2084 CRLF end-of-data marker, bad rows after it / partitioned, 4 workers: rows returned = heap COPY's 1000"),
    ("eodcrlf single",
     "#2084 CRLF end-of-data marker, bad rows after it / single table, 4 workers: rows returned = heap COPY's 1000"),
    ("esc single",
     "#2084 escape of a byte that is not a newline / single table, 4 workers: rows returned = heap COPY's 2000"),
    ("eodstart single",
     "#2084 end-of-data marker on a range start / single table, 4 workers: rows returned = heap COPY's 500"),
)
SYNTAX_VALUES = (
    ("bsnl single", "#2084 escaped newline / single table, 4 workers: result == heap COPY"),
    ("bsnl part", "#2084 escaped newline / partitioned, 4 workers: result == heap COPY"),
    ("eod single", "#2084 end-of-data marker / single table, 4 workers: result == heap COPY"),
    ("eod part", "#2084 end-of-data marker / partitioned, 4 workers: result == heap COPY"),
    ("even single", "#2084 even backslash run / single table, 4 workers: result == heap COPY"),
    ("eodjunk single", "#2084 bad rows after the end-of-data marker / single table, 4 workers: result == heap COPY"),
    ("eodjunk part", "#2084 bad rows after the end-of-data marker / partitioned, 4 workers: result == heap COPY"),
    ("long single", "#2084 odd backslash run across a 64 KiB read / single table, 4 workers: result == heap COPY"),
    ("multi part", "#2084 records of 3 and 4 lines, key after them / partitioned, 4 workers: result == heap COPY"),
    ("eodcrlf part", "#2084 CRLF end-of-data marker, bad rows after it / partitioned, 4 workers: result == heap COPY"),
    ("eodcrlf single",
     "#2084 CRLF end-of-data marker, bad rows after it / single table, 4 workers: result == heap COPY"),
    ("esc single", "#2084 escape of a byte that is not a newline / single table, 4 workers: result == heap COPY"),
    ("eodstart single", "#2084 end-of-data marker on a range start / single table, 4 workers: result == heap COPY"),
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


def test_parallel_copy_fixture_premise(pcopy, expect):
    """The "rows returned = heap COPY's N" checks compare with the heap table, so this
    pins N, and the bsnl64k.txt byte layout the pinned offsets rely on."""
    expect.text(pcopy["2084 premise"], "2000 2000 1000 2000 1000 7501 2000 1000 125542 5c0a",
                "#2084 premise: heap COPY loads bsnl 2000, bsnl2 2000, eod 1000, even 2000, eodjunk 1000, bsnl64k 7501,"
                " multi 2000, eodcrlf 1000 rows; bsnl64k.txt is 125542 bytes with 5c 0a at bytes 65535-65536")


def test_parallel_copy_split_keeps_backslash_parity_across_a_read_two_workers(pcopy, expect):
    """The run of 3 that ends at byte 65535 escapes the newline at byte 65536, the first
    byte of the next read (mutants e2 and n35)."""
    expect.text(pcopy["2084 long offsets 2"], "0 65542 125542",
                "#2084 odd backslash run across a 64 KiB read / 2 workers: offsets are 0 65542 125542"
                " (the newline at byte 65536 is escaped)")


def test_parallel_copy_split_keeps_backslash_parity_across_a_read_four_workers(pcopy, expect):
    expect.text(pcopy["2084 long offsets 4"], "0 31392 65542 94167 125542",
                "#2084 odd backslash run across a 64 KiB read / 4 workers: offsets are 0 31392 65542 94167 125542")


def test_parallel_copy_unsorted_row_after_continued_records_is_refused(pcopy, expect):
    """The splitter starts a new record after each continued one, so the sort guard sees
    the unsorted row (mutant n44)."""
    expect.text(pcopy["2084 contp unsorted"], "22000",
                "#2084 unsorted row after continued records / partitioned: refused (22000)")


def test_parallel_copy_record_the_file_ends_inside_is_keyed(pcopy, expect):
    """A file that ends inside a continued record: that record is the last row, and
    its unsorted key is refused (mutant n31)."""
    expect.text(pcopy["2084 eofrec unsorted"], "22000",
                "#2084 file ends inside a continued record with an unsorted key / partitioned: refused (22000)")


def test_parallel_copy_names_the_first_bad_row(pcopy, expect):
    """Two ranges fail; the load reports the first in file order, as heap COPY does
    (mutant n04)."""
    expect.text(pcopy["2084 twobad"], "bad_early",
                "#2084 two bad rows in different ranges / single table, 4 workers: the error names the first one,"
                " bad_early, as heap COPY's does")


def test_parallel_copy_fixture_premise_esc_and_eodstart(pcopy, expect):
    """Pins N for the two fixtures below, and the eodstart.txt byte layout its pinned
    offsets rely on: the \\. line at byte 3500."""
    expect.text(pcopy["2084 premise 2"], "2000 500 13996 5c2e0a",
                "#2084 premise: heap COPY loads esc 2000 and eodstart 500 rows; eodstart.txt is 13996 bytes"
                " with 5c 2e 0a at bytes 3500-3502")


def test_parallel_copy_split_clears_the_escape_after_a_non_newline_byte(pcopy, expect):
    """In x1 \\ t q \\ LF the \\t escape ends at the t, so the next \\ escapes the newline
    and no cut follows it (mutant a1)."""
    expect.text(pcopy["2084 esc offsets 4"], "0 7456 14906 22346 29786",
                "#2084 escape of a byte that is not a newline / 4 workers: offsets are 0 7456 14906 22346 29786")


def test_parallel_copy_split_puts_the_end_of_data_marker_on_a_range_start(pcopy, expect):
    """The fixture premise for mutant a8: the second range starts at the \\. line."""
    expect.text(pcopy["2084 eodstart offsets 4"], "0 3500 7003 10503 13996",
                "#2084 premise: eodstart.txt with 4 workers: offsets are 0 3500 7003 10503 13996,"
                " so the second range starts at the end-of-data marker")


def test_parallel_copy_end_of_data_marker_with_dedup_leaves_no_prepared_transaction(pcopy, expect):
    """With dedup on, only the rollback after the marker resolves the ranges after it,
    on the first load and on the refused repeat (mutant m1)."""
    expect.text(f"{pcopy['2084 eodd leak 1']} {pcopy['2084 eodd leak 2']}", "0 0",
                "#2084 end-of-data marker, dedup on / single table, 4 workers: no prepared-transaction leak"
                " after the load or after its repeat")


def test_parallel_copy_end_of_data_marker_with_dedup_loads_once(pcopy, expect):
    want = pcopy["2084 eodd want"]
    expect.text(f"{pcopy['2084 eodd load 1']} {pcopy['2084 eodd load 2']} {pcopy['2084 eodd table']}",
                f"{want} 0 {want}",
                "#2084 end-of-data marker, dedup on / single table, 4 workers: the load returns heap COPY's 1000,"
                " the repeat returns 0, the table holds 1000")


def test_parallel_copy_fixture_premise_contdot(pcopy, expect):
    """Pins heap COPY's outcome on contdot.txt for this major: PG 17 reads on past a
    \\. line inside a record, PG 18 refuses the file."""
    expect.text(pcopy["2084 contdot heap"], pcopy["2084 contdot want"],
                "#2084 premise: heap COPY of contdot.txt loads 2000 rows on PG 17 and older, and refuses it"
                " with 22P04 on PG 18 and newer")


def test_parallel_copy_marker_line_inside_a_continued_record_gives_heap_copys_outcome(pcopy, expect):
    """A line that is only \\. inside a continued record does not end the data for the
    partitioned splitter (mutant m5, red on PG 17)."""
    expect.text(pcopy["2084 contdot part"], pcopy["2084 contdot heap"],
                "#2084 end-of-data marker line inside a continued record / partitioned, 4 workers:"
                " the outcome is heap COPY's")


def test_parallel_copy_marker_line_inside_a_continued_record_loads_heap_copys_values(pcopy, expect):
    expect.hash(pcopy["2084 contdot hash"], pcopy["2084 contdot hash want"],
                "#2084 end-of-data marker line inside a continued record / partitioned, 4 workers:"
                " result == heap COPY")


def test_parallel_copy_fixture_premise_malformed_markers(pcopy, expect):
    """Pins heap COPY's outcome on the two malformed-marker files: both refused."""
    expect.text(f"{pcopy['2084 eodcr heap']} {pcopy['2084 eodnonl heap']}", "22P04 22P04",
                "#2084 premise: heap COPY refuses eodcr.txt (\\.\\r\\n in an LF file) and eodnonl.txt"
                " (\\. at the end with no newline) with 22P04")


def test_parallel_copy_malformed_marker_in_an_lf_file_gives_heap_copys_outcome(pcopy, expect):
    """The last range keeps the \\.\\r\\n line, so its loader refuses it as heap COPY
    does (mutant n22)."""
    expect.text(pcopy["2084 eodcr part"], pcopy["2084 eodcr heap"],
                "#2084 malformed end-of-data marker \\.\\r\\n in an LF file / partitioned, 4 workers:"
                " the outcome is heap COPY's")


def test_parallel_copy_malformed_marker_in_an_lf_file_leaves_nothing(pcopy, expect):
    expect.text(pcopy["2084 eodcr left"], "0 0",
                "#2084 malformed end-of-data marker \\.\\r\\n in an LF file / partitioned, 4 workers:"
                " the partitions hold 0 rows and no prepared transaction is left")


def test_parallel_copy_marker_at_eof_without_newline_gives_heap_copys_outcome(pcopy, expect):
    """A \\. with no newline at the end is the marker too, and the last range keeps it
    (mutants n22 and n55)."""
    expect.text(pcopy["2084 eodnonl part"], pcopy["2084 eodnonl heap"],
                "#2084 end-of-data marker \\. at the end with no newline / partitioned, 4 workers:"
                " the outcome is heap COPY's")


def test_parallel_copy_marker_at_eof_without_newline_leaves_nothing(pcopy, expect):
    expect.text(pcopy["2084 eodnonl left"], "0 0",
                "#2084 end-of-data marker \\. at the end with no newline / partitioned, 4 workers:"
                " the partitions hold 0 rows and no prepared transaction is left")


def test_parallel_copy_fixture_premise_eodearly(pcopy, expect):
    """Pins N and the layout mutant n57 needs: the \\. line at byte 492, before the
    first interior offset 3225."""
    expect.text(pcopy["2084 eodearly premise"], "100 12896 5c2e0a 0 3225 6449 9676 12896",
                "#2084 premise: heap COPY loads eodearly 100 rows; eodearly.txt is 12896 bytes with 5c 2e 0a"
                " at bytes 492-494; offsets with 4 workers are 0 3225 6449 9676 12896, so the marker is in"
                " the first range")


def test_parallel_copy_marker_in_the_first_range_stops_the_load(pcopy, expect):
    """A marker in range 0 ends the load there (mutant n57)."""
    expect.num(pcopy["2084 eodearly rows"], pcopy["2084 eodearly rows want"],
               "#2084 end-of-data marker in the first range / single table, 4 workers: rows returned = heap COPY's 100")


def test_parallel_copy_marker_in_the_first_range_loads_heap_copys_values(pcopy, expect):
    expect.hash(pcopy["2084 eodearly hash"], pcopy["2084 eodearly hash want"],
                "#2084 end-of-data marker in the first range / single table, 4 workers: result == heap COPY")


def test_parallel_copy_marker_in_the_first_range_leaves_no_prepared_transaction(pcopy, expect):
    expect.num(pcopy["2084 eodearly leak"], 0,
               "#2084 end-of-data marker in the first range / single table, 4 workers: no prepared-transaction leak")

# ---- #2080: a single table with a unique index needs one worker --------------------

def test_parallel_copy_refuses_a_primary_key_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["unique pk"], _want_unique("t_uk"),
                "parallel_copy refuses a single table with a primary key and 2 workers with 0A000, not a hang (#2080)")


def test_parallel_copy_refuses_a_unique_index_table_with_four_workers(pcopy, expect):
    expect.text(pcopy["unique index"], _want_unique("t_ui"),
                "parallel_copy refuses a single table with a unique index and 4 workers with 0A000, not a hang (#2080)")


def test_parallel_copy_refuses_an_exclusion_constraint_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["exclusion"], _want_unique("t_ux"),
                "parallel_copy refuses a single table with an exclusion constraint and 2 workers with 0A000 (#2080)")


def test_parallel_copy_refuses_a_deferrable_unique_index_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["deferrable"], _want_unique("t_ud"),
                "parallel_copy refuses a single table with a deferrable unique index and 2 workers with 0A000 (#2080)")


def test_parallel_copy_refuses_a_partial_unique_index_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["partial"], _want_unique("t_up"),
                "parallel_copy refuses a single table with a partial unique index and 2 workers with 0A000, not a hang (#2080)")


def test_parallel_copy_refuses_a_deferrable_exclusion_constraint_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["deferrable exclusion"], _want_unique("t_uxd"),
                "parallel_copy refuses a single table with a deferrable exclusion constraint and 2 workers with 0A000 (#2080)")


def test_parallel_copy_refuses_an_expression_unique_index_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["expression"], _want_unique("t_ue"),
                "parallel_copy refuses a single table with an expression unique index and 2 workers with 0A000, not a hang (#2080)")


def test_parallel_copy_refuses_a_gist_exclusion_constraint_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["gist exclusion"], _want_unique("t_gx"),
                "parallel_copy refuses a single table with a gist exclusion constraint and 2 workers with 0A000, not a hang (#2080)")


def test_parallel_copy_refuses_a_nulls_not_distinct_include_unique_index_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["include"], _want_unique("t_inc"),
                "parallel_copy refuses a single table with a NULLS NOT DISTINCT unique index with an INCLUDE column and 2 workers with 0A000, not a hang (#2080)")


def test_parallel_copy_gives_a_caller_without_insert_42501_before_the_unique_refusal(pcopy, expect):
    expect.text(pcopy["no insert"], "42501 permission denied for table t_uk | no hint",
                "parallel_copy gives a caller without INSERT 42501 before the unique-index refusal (#2080)")


def test_parallel_copy_refuses_a_url_before_the_unique_refusal(pcopy, expect):
    expect.text(pcopy["unique url"], _want_refusal("parallel_copy", REMOTE_S3),
                "parallel_copy refuses an s3:// URL with the #1886 refusal before the unique-index refusal (#2080)")


def test_parallel_copy_refuses_a_primary_key_table_with_dedup(pcopy, expect):
    expect.text(pcopy["unique dedup"], _want_unique("t_uk"),
                "parallel_copy refuses a single table with a primary key, 2 workers and dedup => true with 0A000 and the hint (#2080)")


def test_parallel_copy_concurrently_leaves_an_invalid_ready_unique_index(pcopy, expect):
    expect.text(pcopy["invalid premise"], "t|f|t",
                "premise: CREATE UNIQUE INDEX CONCURRENTLY leaves a columnar table an invalid, ready unique index (#2080)")


def test_parallel_copy_refuses_an_invalid_unique_index_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["invalid"], _want_unique("t_uv"),
                "parallel_copy refuses a single table with an invalid unique index and 2 workers with 0A000 (#2080)")


def test_parallel_copy_refuses_a_primary_key_table_with_the_default_worker_count(pcopy, expect):
    expect.text(pcopy["default workers"], _want_unique("t_ukd"),
                "parallel_copy refuses a single table with a primary key and the default worker count with 0A000 (#2080)")


def test_parallel_copy_unique_refusal_loads_nothing(pcopy, expect):
    expect.num(pcopy["unique empty"], 0, "the unique-index refusal loads nothing (#2080)")


def test_parallel_copy_exclusion_refusal_loads_nothing(pcopy, expect):
    expect.num(pcopy["exclusion empty"], 0, "the exclusion-constraint refusal loads nothing (#2080)")


def test_parallel_copy_still_loads_a_primary_key_table_with_one_worker(pcopy, expect):
    expect.num(pcopy["unique one worker"], 5000,
               "control: parallel_copy with 1 worker still loads a single table with a primary key (#2080)")


def test_parallel_copy_still_loads_a_non_unique_index_table_with_two_workers(pcopy, expect):
    expect.num(pcopy["non-unique index"], 5000,
               "control: parallel_copy with 2 workers still loads a single table with a non-unique index (#2080)")


def test_parallel_copy_still_loads_a_partitioned_primary_key_table_with_four_workers(pcopy, expect):
    expect.text(pcopy["partitioned pk"], "no error (5000) | 5000",
                "control: parallel_copy with 4 workers still loads a partitioned table with a primary key (#2080)")


def test_parallel_copy_refuses_a_unique_index_created_after_its_check(pcopy, expect):
    expect.text(pcopy["unique late"],
                '0A000 pgcolumnar.parallel_copy worker N failed: pgcolumnar.parallel_copy cannot load "t_ur" '
                'with more than one worker, because it has a unique or exclusion index '
                '| Load it with workers => 1, or with COPY.',
                "parallel_copy refuses 2 workers when a unique index appears after its check, not a hang (#2080)")


def test_parallel_copy_late_unique_refusal_loads_nothing(pcopy, expect):
    expect.num(pcopy["unique late empty"], 0, "the late unique-index refusal loads nothing (#2080)")


def test_parallel_copy_refuses_a_late_unique_index_in_every_loader(pcopy, expect):
    expect.text(pcopy["unique late 4"],
                '0A000 pgcolumnar.parallel_copy worker N failed: pgcolumnar.parallel_copy cannot load "t_ur4" '
                'with more than one worker, because it has a unique or exclusion index '
                '| Load it with workers => 1, or with COPY. | no row inserted',
                "parallel_copy refuses 4 workers when a unique index appears after its check, "
                "before any loader inserts a row (#2080)")


# ---- #2187: a lock queued between the loaders, a trigger, a cancel ------------------

def test_parallel_copy_premise_unique_index_queued_between_two_loaders(pcopy, expect):
    expect.text(pcopy["mid open seen"], "loaders 1 held, 0 waiting, index 1 waiting",
                "premise: CREATE UNIQUE INDEX queued while 1 of 2 loaders held the table and the other had not asked (#2187)")


def test_parallel_copy_finishes_when_a_unique_index_queues_between_two_loaders(pcopy, expect):
    expect.text(pcopy["mid open"], "no error (5000) | no error (CREATE INDEX) | 5000 | valid t | leak 0",
                "parallel_copy with 2 workers finishes when CREATE UNIQUE INDEX queues between its loaders' locks, "
                "and the index builds after it (#2187)")


def test_parallel_copy_premise_index_queued_between_four_loaders(pcopy, expect):
    expect.text(pcopy["mid open 4 seen"], "loaders 1 held, 0 waiting, index 1 waiting",
                "premise: CREATE INDEX queued while 1 of 4 loaders held the table and the others had not asked (#2187)")


def test_parallel_copy_finishes_when_an_index_queues_between_four_loaders(pcopy, expect):
    expect.text(pcopy["mid open 4"], "no error (5000) | no error (CREATE INDEX) | 5000 | valid t | leak 0",
                "parallel_copy with 4 workers finishes when CREATE INDEX queues between its loaders' locks, "
                "and the index builds after it (#2187)")


def test_parallel_copy_fails_when_a_loader_exits_before_it_opens_the_table(pcopy, expect):
    expect.text(pcopy["gone"],
                "XX000 pgcolumnar.parallel_copy worker 1 failed: loader exited without reporting a result | no hint "
                "| 0 | leak 0",
                "a loader terminated before it opens the table fails the load instead of hanging its sibling (#2187)")


def test_parallel_copy_a_loader_prepared_before_the_cancel(pcopy, expect):
    expect.text(pcopy["cancel seen"], "prepared 1, loaders waiting on a row 1",
                "before the cancel, one loader had prepared and the other waited on a locked row (#2187)")


def test_parallel_copy_cancelled_load_leaves_no_prepared_transaction(pcopy, expect):
    expect.text(pcopy["cancel"], "57014 canceling statement due to statement timeout | same session 0 | after 0",
                "a cancelled parallel_copy leaves no prepared transaction for the caller's next statement (#2187)")


def test_parallel_copy_refuses_an_after_row_trigger_with_two_workers(pcopy, expect):
    expect.text(pcopy["trigger after"], _want_trigger("t_tga", "t_tga_k"),
                "parallel_copy refuses a single table with an AFTER INSERT row trigger and 2 workers with 0A000, "
                "not a deadlock (#2187)")


def test_parallel_copy_refuses_a_before_row_trigger_with_two_workers(pcopy, expect):
    expect.text(pcopy["trigger before"], _want_trigger("t_tgb", "t_tgb_k"),
                "parallel_copy refuses a single table with a BEFORE INSERT row trigger and 2 workers with 0A000, "
                "not a deadlock (#2187)")


def test_parallel_copy_refuses_a_statement_trigger_with_four_workers(pcopy, expect):
    expect.text(pcopy["trigger stmt"], _want_trigger("t_tgs", "t_tgs_k"),
                "parallel_copy refuses a single table with an AFTER INSERT statement trigger and 4 workers with 0A000, "
                "not a hang (#2187)")


def test_parallel_copy_refuses_a_trigger_that_writes_a_fresh_columnar_table(pcopy, expect):
    expect.text(pcopy["trigger fresh"], f"{_want_trigger('t_tgn', 't_tgn_k')} | 0 0 | leak 0",
                "parallel_copy refuses a single table whose AFTER INSERT row trigger writes a fresh columnar table "
                "with no index, 2 workers, with 0A000, not a hang (#2187)")

def test_parallel_copy_trigger_refusals_load_and_write_nothing(pcopy, expect):
    expect.text(pcopy["trigger empty"], "0 0 0 | 0 | leak 0",
                "the trigger refusals load nothing, write nothing through the trigger and leave no prepared "
                "transaction (#2187)")


def test_parallel_copy_refuses_a_trigger_created_after_its_check(pcopy, expect):
    expect.text(pcopy["trigger late"],
                '0A000 pgcolumnar.parallel_copy worker N failed: pgcolumnar.parallel_copy cannot load "t_tgl" '
                'with more than one worker, because it has trigger "t_tgl_k" on INSERT '
                '| Load it with workers => 1, or with COPY. | 0 0',
                "parallel_copy refuses 2 workers when an INSERT trigger appears after its check, with the hint (#2187)")


def test_parallel_copy_still_loads_a_trigger_table_with_one_worker(pcopy, expect):
    expect.text(pcopy["trigger one"], "no error (5000) | 5000",
                "control: parallel_copy with 1 worker still loads a single table with an INSERT trigger, "
                "and the trigger runs (#2187)")


def test_parallel_copy_still_loads_a_disabled_trigger_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["trigger disabled"], "no error (5000) | 5000",
                "control: parallel_copy with 2 workers still loads a single table whose INSERT trigger is disabled (#2187)")


def test_parallel_copy_still_loads_a_foreign_key_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["trigger fk"], "no error (5000) | 5000",
                "control: parallel_copy with 2 workers still loads a single table with a foreign key (#2187)")


def test_parallel_copy_still_loads_an_update_trigger_table_with_two_workers(pcopy, expect):
    expect.text(pcopy["trigger update"], "no error (5000) | 5000",
                "control: parallel_copy with 2 workers still loads a single table with an UPDATE-only trigger (#2187)")


def test_parallel_copy_refuses_a_temporal_primary_key_table_with_two_workers(pcopy, expect):
    """A temporal primary key's index is both unique and exclusion (#2200). WITHOUT
    OVERLAPS arrived in PostgreSQL 18, so the test declines before it, as the shell
    records a SKIP under the same name."""
    major = pcopy["major"]
    if not isinstance(major, int):
        # An unreadable major fails the test rather than being taken for an old one.
        expect.text(f"unreadable server major [{major}]", "a server major to gate on",
                    "parallel_copy refuses a single table with a temporal primary key (unique and exclusion) "
                    "and 2 workers with 0A000 and the hint, and loads nothing (#2200)")
        return
    if major < 18:
        expect.cannot_run(
            "UNSUPPORTED_MAJOR",
            f"WITHOUT OVERLAPS arrived in PostgreSQL 18; this server is {major}",
            name="parallel_copy refuses a single table with a temporal primary key (unique and exclusion) "
                 "and 2 workers with 0A000 and the hint, and loads nothing (#2200)")
        return
    expect.text(pcopy["temporal pk"], f"{_want_unique('t_utp')} | 0 rows",
                "parallel_copy refuses a single table with a temporal primary key (unique and exclusion) "
                "and 2 workers with 0A000 and the hint, and loads nothing (#2200)")


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


# ---- #2183: a lock the caller holds ------------------------------------------------

@pytest.mark.parametrize("key,want,name", [
    ("lk ael", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_lkc"',
     "parallel_copy after LOCK TABLE IN ACCESS EXCLUSIVE MODE in the same transaction is refused with 55000,"
     " not waited on (#2183)"),
    ("lk trunc", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_lkc"',
     "parallel_copy after TRUNCATE in the same transaction is refused with 55000, not waited on (#2183)"),
    ("lk share", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lkc"',
     "parallel_copy after LOCK TABLE IN SHARE MODE in the same transaction is refused with 55000, not waited on"
     " (#2183)"),
    ("lk rowex", "none|5000",
     "parallel_copy after LOCK TABLE IN ROW EXCLUSIVE MODE in the same transaction still loads (#2183)"),
    ("lk other", "none|5000",
     "parallel_copy while the transaction holds ACCESS EXCLUSIVE on another table still loads (#2183)"),
    ("lk part", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_lkp_p2"',
     "parallel_copy into a partitioned table after LOCK TABLE on one partition IN ACCESS EXCLUSIVE MODE is"
     " refused with 55000 (#2183)")])
def test_parallel_copy_a_lock_the_caller_holds_is_refused_not_waited_on(pcopy, expect, key, want, name):
    """#2183: the coordinator and the loaders take ROW EXCLUSIVE on the target, and the
    loaders on the partitions they route to, in their own transactions, outside the
    caller's lock group; the caller waits for them on a latch. A caller holding a lock
    that conflicts with ROW EXCLUSIVE (LOCK TABLE, TRUNCATE, SHARE) waited until
    statement_timeout; it is now refused with 55000, by SQLSTATE and message. A lock
    ROW EXCLUSIVE does not conflict with still loads."""
    expect.text(pcopy[key], want, name)


@pytest.mark.parametrize("key,want,name", [
    ("lk order unique", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lko"',
     "parallel_copy into a primary-key table under the caller's SHARE lock gets the lock refusal (55000) before the"
     " unique-index refusal (#2183)"),
    ("lk order acl", "42501|permission denied for table t_lko",
     "parallel_copy gives a caller without INSERT that holds SHARE on the table 42501 before the lock refusal"
     " (#2183)")])
def test_parallel_copy_refusal_order_with_a_lock_the_caller_holds(pcopy, expect, key, want, name):
    """#2183 with #2080: parallel_copy refuses a caller without INSERT (42501) first, then
    a lock the caller holds (55000), then a unique index with more than one worker
    (0A000). The unique-index hint says "workers => 1", which would only lead a caller
    holding SHARE to the lock refusal, so that refusal comes first."""
    expect.text(pcopy[key], want, name)


def test_parallel_copy_lock_refusal_before_the_trigger_refusal(pcopy, expect):
    """#2187 with #2183: a caller holding SHARE on a table with an INSERT trigger gets the
    lock refusal (55000), whose cause holds for every worker count, not the trigger's."""
    expect.text(pcopy["lk order trigger"],
                '55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lkt"',
                "parallel_copy into a table with an INSERT trigger under the caller's SHARE lock gets the lock refusal"
                " (55000) before the trigger refusal (#2187)")


def test_parallel_copy_unique_refusal_before_the_trigger_refusal(pcopy, expect):
    """#2187 with #2080: a primary-key table with an INSERT trigger gets the unique-index
    refusal; the trigger refusal comes after it."""
    expect.text(pcopy["order trigger unique"], _want_unique("t_tgpk"),
                "parallel_copy into a primary-key table with an INSERT trigger gets the unique-index refusal before"
                " the trigger refusal (#2187)")


@pytest.mark.parametrize("key,want,name", [
    ("lk sue", "none|5000",
     "parallel_copy after LOCK TABLE IN SHARE UPDATE EXCLUSIVE MODE in the same transaction still loads (#2183)"),
    ("lk excl", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds ExclusiveLock on "t_lkc"',
     "parallel_copy after LOCK TABLE IN EXCLUSIVE MODE in the same transaction is refused with 55000, naming"
     " ExclusiveLock (#2183)"),
    ("lk sre", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareRowExclusiveLock on "t_lkc"',
     "parallel_copy after LOCK TABLE IN SHARE ROW EXCLUSIVE MODE in the same transaction is refused with 55000,"
     " naming ShareRowExclusiveLock (#2183)"),
    ("lk only parent", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lkp"',
     "parallel_copy into a partitioned table after LOCK TABLE ONLY the parent IN SHARE MODE is refused with 55000,"
     " naming the parent (#2183)"),
    ("lk only mid", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_lk2_a"',
     "parallel_copy into a two-level partitioned table after LOCK TABLE ONLY the mid level IN ACCESS EXCLUSIVE MODE is"
     " refused with 55000, naming it (#2183)"),
    ("lk deep leaf", '55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lk2_a1"',
     "parallel_copy into a two-level partitioned table after LOCK TABLE on a second-level leaf IN SHARE MODE is"
     " refused with 55000, naming it (#2183)")])
def test_parallel_copy_lock_modes_and_partition_depth(pcopy, expect, key, want, name):
    """#2183, #2212 review: the rest of PostgreSQL's conflict table for ROW EXCLUSIVE
    (SHARE UPDATE EXCLUSIVE still loads; EXCLUSIVE and SHARE ROW EXCLUSIVE are refused,
    each naming its mode), and the partition tree in depth (ONLY the parent, ONLY a
    partitioned mid level, a second-level leaf)."""
    expect.text(pcopy[key], want, name)


def test_parallel_copy_a_refused_load_loads_nothing(pcopy, expect):
    expect.text(pcopy["lk after"], "10000 rows, 0 partitioned rows, 0 prepared",
                "parallel_copy refused for a lock the caller holds loads nothing and leaves no prepared transaction"
                " (#2183)")


# ---- #2211: the caller's own write to the target -----------------------------------

def test_parallel_copy_own_write_premise(pcopy, expect):
    expect.text(pcopy["ownw premise"], "0/1",
                "premise: the table written in the #2211 checks has no storage row before them, and the table with"
                " one committed row has one")


@pytest.mark.parametrize("key,want,name", [
    ("ownw single", '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownw"',
     "parallel_copy after an INSERT into the same table in the same transaction is refused with 55000, not waited"
     " on (#2211)"),
    ("ownw one worker", '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownw"',
     "parallel_copy with workers => 1 after an INSERT into the same table in the same transaction is refused with"
     " 55000 (#2211)"),
    ("ownw committed row",
     '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownw1"',
     "parallel_copy after an INSERT into a table that already has a committed row is refused with 55000 (#2211)"),
    ("ownw partition",
     '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownp_p0"',
     "parallel_copy into a partitioned table after an INSERT that reached one partition is refused with 55000,"
     " naming the partition (#2211)")])
def test_parallel_copy_the_callers_own_write_is_refused_not_waited_on(pcopy, expect, key, want, name):
    """#2211: the first writer of a columnar storage holds a transaction-scoped advisory
    lock on it (PGCOLUMNAR_LOCKCLASS_STORAGE_ROW) to transaction end, taken on every
    flush. After an INSERT into the target, or into one of its partitions, in the
    caller's transaction, the coordinator or a loader waited on that lock in its own
    transaction while the caller waited on them on a latch, until statement_timeout.
    It is now refused with 55000, by SQLSTATE and message naming the written relation."""
    expect.text(pcopy[key], want, name)


def test_parallel_copy_refused_after_its_own_write_loads_nothing(pcopy, expect):
    """#2211: the refused loads were rolled back with the caller's INSERT, and leave no
    committed storage row for the table that had none: the coordinator used to create
    and commit one once the caller's rollback released the lock."""
    expect.text(pcopy["ownw after"], "0 rows, 1 rows, 0 partitioned rows, 0 storage rows, 0 prepared",
                "parallel_copy refused after the transaction's own INSERT loads nothing and leaves no prepared"
                " transaction and no storage row (#2211)")


@pytest.mark.parametrize("key,name", [
    ("ownw other", "parallel_copy after an INSERT into another columnar table in the same transaction still loads"
     " (#2211)"),
    ("ownw none", "parallel_copy in a transaction that wrote nothing still loads (#2211)"),
    ("ownw none partitioned",
     "parallel_copy into a partitioned table in a transaction that wrote nothing still loads (#2211)")])
def test_parallel_copy_without_a_write_to_the_target_still_loads(pcopy, expect, key, name):
    """#2211: only the target's own storage lock is refused. A write to another table, or
    none, still loads all 5000 rows inside a transaction block."""
    expect.text(pcopy[key], "none|5000", name)


def test_parallel_copy_own_write_refusal_names_the_last_partition_and_its_detail(pcopy, expect):
    """#2211, #2214 review: S4 writes the first partition, so a check of only the first
    columnar member passed it. A write that reaches the last partition is refused naming
    it, and the DETAIL names the same partition, not the target."""
    expect.text(pcopy["ownw last partition"],
                '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownp_p3"|The write'
                ' holds the lock that serializes the first writers of "t_ownp_p3" until this transaction ends, and the'
                ' background workers take that lock in their own transactions, so they would wait for this transaction'
                ' to end.',
                "parallel_copy into a partitioned table after an INSERT that reached its last partition is refused"
                " with 55000, naming that partition in the message and the DETAIL (#2211)")


@pytest.mark.parametrize("key,want,name", [
    ("ownw later partition",
     '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownp_p2"',
     "parallel_copy into a partitioned table after a direct INSERT into a later partition is refused with 55000,"
     " naming it (#2211)"),
    ("ownw deep leaf", '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_lk2_a2"',
     "parallel_copy into a two-level partitioned table after an INSERT that reached a second-level leaf is refused"
     " with 55000, naming the leaf (#2211)"),
    ("ownw order unique", '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownu"',
     "parallel_copy into a primary-key table after the caller's own INSERT with 2 workers gets the #2211 refusal"
     " (55000) before the unique-index refusal (#2211)"),
    ("ownw order lock",
     '55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_ownw"',
     "parallel_copy after LOCK TABLE IN ACCESS EXCLUSIVE MODE and an INSERT into the same table gets the #2183 lock"
     " refusal first (#2211)")])
def test_parallel_copy_own_write_refusal_covers_the_tree_and_keeps_its_order(pcopy, expect, key, want, name):
    """#2211, #2214 review: a direct write to a later partition and a write reaching a
    second-level leaf are refused naming that relation. The #2211 refusal comes after
    #2183's lock refusal and before #2080's unique-index refusal."""
    expect.text(pcopy[key], want, name)


def test_parallel_copy_own_write_refusal_before_the_trigger_refusal(pcopy, expect):
    """#2187 with #2211: the caller's own INSERT into a table with an INSERT trigger, then
    2 workers, gets the own-write refusal (55000), not the trigger refusal."""
    expect.text(pcopy["ownw order trigger"],
                '55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_owntg"',
                "parallel_copy into a table with an INSERT trigger after the caller's own INSERT with 2 workers gets"
                " the #2211 refusal (55000) before the trigger refusal (#2187)")

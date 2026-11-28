"""Parallel Parquet export equals serial export equals the source (#300). The pytest
twin of `test/parallel_export_parquet.sh`, every check by the shell's name (#1341).

pgcolumnar.parallel_export_parquet(target, dir, workers) fans a read-only export
across background workers, each writing part-NNNN.parquet into one directory that
pgcolumnar.read_parquet reads back as one relation. Asserted, for a single columnar
table split by row-group ranges and a partitioned one written one file per partition,
across 1, 2 and 4 workers: the rows returned, the read-back, one file per worker or
partition, and an empty _SUCCESS marker (#394). Then: empty input; an export inside a
transaction is the committed image (#329); a cancelled export leaves no files and no
marker, and a retry into the directory succeeds; a cancel does not return while a
worker it held stopped still runs (#1864 review); the destination-length guard (#863)
at both boundaries and in the window the old probe left open; the two refusals; and
the backend survives.

THE ORACLES ARE THE GENERATOR, not the table being exported. With tuple_insert
dropping one row, the table, its serial export and every parallel export agreed with
each other, and the shell's old form passed all 54 checks.

EVERY READING MUST BE A MEASUREMENT. A failed query is a query_error(), shown in the
comparison as "no reading (...)" so the check reds by its own name. A refusal is
matched by its own message, a dead server is not "none" (accepted) or "absent" (not
created), and "still up" also needs the server log's count of backends killed by a
signal unmoved since setup: a fresh connection answers again after crash recovery.

ON A PRIVATE DATABASE. Files the backend writes live in `server_dir`. Four workers at
most, inside the cluster's default max_worker_processes of 8, except the #2191 arm,
which checks that six fit and seven do not.
"""
import os
import pathlib
import re
import signal
import subprocess
import threading
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_parallel_export_parquet"
SRCDIR = pathlib.Path(__file__).resolve().parents[2]
COLS = "id int, k int, v float8, txt text"
RB = COLS                                  # read_parquet column list
GEN_COL = "SELECT g, g%1000, g::float8/7, 'r'||g FROM generate_series(1,80000) g"
GEN_PART = "SELECT g, g%600, g::float8/7, 'r'||g FROM generate_series(1,80000) g"
GEN_TX_COMMITTED = "SELECT g, g, g::float8/7, 'c'||g FROM generate_series(1,20000) g"
NSRC = 80000
NPART = 80000
NC = 20000
TOO_LONG = "54000"                         # ERRCODE_PROGRAM_LIMIT_EXCEEDED
LEN_OK, LEN_OVER, LEN_WINDOW = 984, 985, 994


def _err(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0]}")

    def one(self, sql):
        """-> the first column of the first row, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return _err(exc)
        if row is None or row[0] is None:
            return query_error("no row")
        return row[0]

    def set_hash(self, sql):
        """lib.sh's pgc_set_hash: md5 over the sorted row texts, EMPTY for no rows."""
        return self.one("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                        f"FROM (SELECT _row::text AS t FROM ( {sql} ) _row) _s")

    def sqlstate(self, sql):
        """-> (SQLSTATE or "none", message), as the shell's run_sqlstate. A failure that
        never reached the server has no SQLSTATE and is a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            if exc.sqlstate is None:
                return _err(exc), ""
            return exc.sqlstate, exc.diag.message_primary or ""
        return "none", ""

    def refusal(self, sql, pattern):
        """-> "refused" when the statement failed with a message matching pattern."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            if exc.sqlstate is None:
                return _err(exc)
            msg = exc.diag.message_primary or ""
            return "refused" if re.search(pattern, msg) else f"another error: {msg}"
        return "accepted"


def _nfiles(d):
    return len(list(pathlib.Path(d).glob("*.parquet"))) if pathlib.Path(d).is_dir() else 0


def _anyfiles(d):
    """Every file, in-flight temps included: the #394 sink writes part-N.parquet.tmp.PID
    and renames it when the part is done."""
    return sum(len(files) for _, _, files in os.walk(d))


def _success(d):
    return "yes" if (pathlib.Path(d) / "_SUCCESS").is_file() else "no"


def _success_empty(d):
    m = pathlib.Path(d) / "_SUCCESS"
    return "empty" if m.is_file() and m.stat().st_size == 0 else "no"


def _crashes(cluster):
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return sum(1 for line in text.splitlines() if "terminated by signal" in line)


def _survived(cluster, db, before):
    """-> "1" when the server answers and no backend died since setup, as the shell's."""
    up = db.one("SELECT 1")
    now = _crashes(cluster)
    if not (isinstance(before, int) and isinstance(now, int)):
        return "no (crash count unreadable)"
    if up == 1 and now == before:
        return "1"
    return f"no ([{up}], {now - before} backend crashes)"


def _premise(cluster):
    """-> None when the length arithmetic still holds, else what moved. The shell exits
    on this; here every length reading becomes a query_error() and reds by name."""
    try:
        inc = subprocess.run([cluster.pg_config, "--includedir-server"], capture_output=True,
                             text=True, check=True).stdout.strip()
        manual = (pathlib.Path(inc) / "pg_config_manual.h").read_text()
        pexport = (SRCDIR / "src" / "columnar_parallel_export.c").read_text()
        sink = (SRCDIR / "src" / "columnar_sink.c").read_text()
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"PREMISE FAILED: {exc}"
    m = re.search(r"^#define\s+MAXPGPATH\s+([0-9]+)", manual, re.M)
    if not m or m.group(1) != "1024":
        return f"PREMISE FAILED: MAXPGPATH is [{m.group(1) if m else ''}]; these arms are written for 1024"
    a = pexport.count('snprintf(fp, sizeof(fp), "%s/%s", dir, de->d_name);')
    b = sink.count('psprintf("%s.tmp.%d", path, MyProcPid)')
    if (a, b) != (1, 1):
        return f"PREMISE FAILED: cleanup-scan anchor [{a}] sink .tmp anchor [{b}], wanted 1 and 1"
    return None


def _path_of_len(base, want):
    """A directory path of exactly `want` bytes under base/lp, its parents created 777
    so the server can create the last component itself; or a query_error()."""
    p = str(base / "lp")
    while want - len(p) - 1 > 255:
        p = f"{p}/{'a' * 200}"
    rem = want - len(p) - 1
    if rem < 1:
        return query_error(f"cannot build a {want}-byte path under {base}")
    p = f"{p}/{'a' * rem}"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        for root, dirs, _ in os.walk(base / "lp"):
            os.chmod(root, 0o777)
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o777)
    except OSError as exc:
        return query_error(f"mkdir for {want} failed: {exc}")
    if len(p) != want or os.path.exists(p):
        return query_error(f"built {len(p)} bytes or the target exists, wanted {want}")
    return p


def _not_created(path, state):
    """-> "absent" only when the statement reached the server: with the server gone
    nothing could create the directory, and "absent" measured nothing."""
    if not isinstance(path, str) or path.startswith(QUERY_ERROR):
        return f"no reading ({path})"
    if os.path.exists(path):
        return "created"
    if state == "none" or re.fullmatch(r"[0-9A-Z]{5}", state):
        return "absent"
    return f"no reading (the statement did not run: {state})"


def _length_arm(db, base, want, premise):
    if premise:
        return {"path": query_error(premise), "len": query_error(premise),
                "state": query_error(premise), "msg": ""}
    path = _path_of_len(base, want)
    if path.startswith(QUERY_ERROR):
        return {"path": path, "len": path, "state": path, "msg": ""}
    state, msg = db.sqlstate(f"SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '{path}', 2)")
    return {"path": path, "len": len(path), "state": state, "msg": msg,
            "files": _nfiles(path), "success": _success(path), "created": _not_created(path, state)}


def _signal_quietly(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _cancel_run(dsn, target, out):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SELECT pgcolumnar.parallel_export_parquet('t_big'::regclass, '{target}', 4)")
        out.append("completed")
    except psycopg.Error as exc:
        out.append(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _self_lock_export(dsn, setup, table, target):
    """-> "SQLSTATE|message" of the error, or "none|<rows returned>", for BEGIN; setup;
    the export of table; COMMIT in one session under a 5 s statement_timeout (#2183).
    The timeout makes the old wait a 57014 rather than a hang. A failure that never
    reached the server is a query_error()."""
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SET statement_timeout = '5s'")
            conn.execute(setup)
            row = conn.execute(f"SELECT pgcolumnar.parallel_export_parquet('{table}'::regclass, "
                               f"'{target}', 2)").fetchone()
            conn.commit()
    except psycopg.Error as exc:
        if exc.sqlstate is None:
            return _err(exc)
        return f"{exc.sqlstate}|{exc.diag.message_primary or ''}"
    return f"none|{row[0] if row else None}"


# ---- #2245: the caller waits for its workers in the lock manager -------------
def _qx_sess(dsn, app, stmts, options=None, out=None):
    """Run stmts in order on one autocommit connection named app. -> (error, last):
    error is "SQLSTATE message" of the first error or None, last the first column of
    the last statement that returned a row. A failure that never reached the server
    is a query_error()."""
    err, last = None, None
    try:
        kw = {"application_name": app, "autocommit": True}
        if options:
            kw["options"] = options
        with psycopg.connect(dsn, **kw) as conn:
            for sql in stmts:
                cur = conn.execute(sql)
                if cur.description is not None:
                    row = cur.fetchone()
                    if row is not None:
                        last = row[0]
    except psycopg.Error as exc:
        err = _err(exc) if exc.sqlstate is None else f"{exc.sqlstate} {exc.diag.message_primary or ''}"
    if out is not None:
        out.append((err, last))
    return err, last


def _qx_result(res):
    err, last = res
    return err if err else f"no error ({last})"


def _qx_wait_lock(db, app, table, mode):
    """Return once session app holds mode on table, up to 5 s."""
    for _ in range(100):
        if db.one("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                  f"WHERE a.application_name = '{app}' AND l.relation = '{table}'::regclass "
                  f"AND l.mode = '{mode}' AND l.granted") == 1:
            return
        time.sleep(0.05)


def _qx_under(ms, limit):
    return f"under {limit} s" if ms < limit * 1000 else f"took {ms} ms"


def _qx_queue(db, dsn, target, locked, d):
    """-> "export | time | other session | rows read back". The caller selects from
    locked, waits until an ACCESS EXCLUSIVE request on locked queues behind it, then
    exports target with 2 workers. The caller's wait is bounded at 10 s, the other
    session's at 20 s."""
    out = []
    b = threading.Thread(target=_qx_sess, daemon=True, args=(dsn, "pexport_qx_b", [
        "BEGIN", f"SELECT count(*) FROM {locked}",
        "DO $$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = "
        f"'{locked}'::regclass AND mode = 'AccessExclusiveLock' AND NOT granted); PERFORM pg_sleep(0.02); "
        "END LOOP; END $$",
        "SET statement_timeout = '10s'",
        f"SELECT pgcolumnar.parallel_export_parquet('{target}'::regclass, '{d}', 2)", "COMMIT"]),
        kwargs={"out": out})
    b.start()
    _qx_wait_lock(db, "pexport_qx_b", locked, "AccessShareLock")
    t0 = time.monotonic()
    ea, _ = _qx_sess(dsn, "pexport_qx_a", ["SET statement_timeout = '20s'", "BEGIN",
                                           f"LOCK TABLE {locked} IN ACCESS EXCLUSIVE MODE", "COMMIT"])
    b.join(timeout=60)
    ms = int((time.monotonic() - t0) * 1000)
    res = out[0] if out else ("the export did not return", None)
    rows = db.one(f"SELECT count(*) FROM pgcolumnar.read_parquet('{d}') AS t({RB})")
    return (f"{_qx_result(res)} | {_qx_under(ms, 6)} | other session {ea or 'locked'} | "
            f"{_shown(rows)} rows read back")


def _qx_cycle(db, dsn, d):
    """-> "outcome | time": a session holding ACCESS EXCLUSIVE on t_lk waits for a lock
    the caller holds once the caller waits for t_lk."""
    out = []
    a = threading.Thread(target=_qx_sess, daemon=True, args=(dsn, "pexport_qx_c", [
        "SET statement_timeout = '20s'", "BEGIN", "LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE",
        "DO $$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = "
        "'t_lk'::regclass AND NOT granted); PERFORM pg_sleep(0.02); END LOOP; END $$",
        "LOCK TABLE t_lk_other IN SHARE MODE", "COMMIT"]), kwargs={"out": out})
    a.start()
    _qx_wait_lock(db, "pexport_qx_c", "t_lk", "AccessExclusiveLock")
    t0 = time.monotonic()
    eb, _ = _qx_sess(dsn, "pexport_qx_d", [
        "BEGIN", "INSERT INTO t_lk_other VALUES (1)", "SET statement_timeout = '10s'",
        f"SELECT pgcolumnar.parallel_export_parquet('t_lk'::regclass, '{d}', 2)", "ROLLBACK"])
    a.join(timeout=60)
    ms = int((time.monotonic() - t0) * 1000)
    ea = out[0][0] if out else "the other session did not return"
    if (ea and ea.startswith("40P01") and not eb) or (eb and eb.startswith("40P01") and not ea):
        outcome = "one side 40P01, the other went on"
    else:
        outcome = f"A {ea or 'committed'}, B {eb or 'exported'}"
    return f"{outcome} | {_qx_under(ms, 5)}"


def _qx_lock_timeout(db, dsn, d):
    """-> "error | time": another session holds ACCESS EXCLUSIVE on t_lk for 6 s; the
    caller's lock_timeout is 1 s."""
    h = threading.Thread(target=_qx_sess, daemon=True, args=(dsn, "pexport_qx_h", [
        "BEGIN", "LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE", "SELECT pg_sleep(6)", "COMMIT"]))
    h.start()
    _qx_wait_lock(db, "pexport_qx_h", "t_lk", "AccessExclusiveLock")
    t0 = time.monotonic()
    res = _qx_sess(dsn, "pexport_qx_t", [f"SELECT pgcolumnar.parallel_export_parquet('t_lk'::regclass, '{d}', 2)"],
                   options="-c statement_timeout=10s -c lock_timeout=1s")
    ms = int((time.monotonic() - t0) * 1000)
    h.join(timeout=30)
    return f"{_qx_result(res)} | {_qx_under(ms, 4)}"


def _qx_victim(db, dsn, d):
    """-> "caller's error | what its detail names | time". A cycle of hard edges in
    which the one worker is the likely victim: the caller waits for its run lock, the
    worker for another session's ACCESS EXCLUSIVE on pgcolumnar.row_group, and that
    session for SHARE on t_wv_w, which the caller wrote. The worker is first held at
    leaf t_wv1 by a third session's ACCESS EXCLUSIVE request queued behind the caller,
    and the other session takes row_group meanwhile. The caller and that session run
    with deadlock_timeout 10 s, so the worker checks first."""
    out, detail = [], []

    def caller():
        try:
            with psycopg.connect(dsn, application_name="pexport_qx_vb", autocommit=True) as conn:
                for sql in ["SET deadlock_timeout = '10s'", "BEGIN", "INSERT INTO t_wv_w VALUES (1)",
                            "LOCK TABLE t_wv1 IN ACCESS SHARE MODE",
                            "DO $$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE "
                            "relation = 't_wv1'::regclass AND mode = 'AccessExclusiveLock' AND NOT granted); "
                            "PERFORM pg_sleep(0.02); END LOOP; END $$",
                            "SET statement_timeout = '10s'"]:
                    conn.execute(sql)
                row = conn.execute(f"SELECT pgcolumnar.parallel_export_parquet('t_wv'::regclass, '{d}', 1)").fetchone()
                conn.execute("ROLLBACK")
            out.append(f"no error ({row[0]})")
        except psycopg.Error as exc:
            out.append(_err(exc) if exc.sqlstate is None else f"{exc.sqlstate} {exc.diag.message_primary or ''}")
            detail.append(exc.diag.message_detail or "")

    b = threading.Thread(target=caller, daemon=True)
    b.start()
    _qx_wait_lock(db, "pexport_qx_vb", "t_wv1", "AccessShareLock")
    t0 = time.monotonic()
    c = threading.Thread(target=_qx_sess, daemon=True, args=(dsn, "pexport_qx_vc", [
        "SET statement_timeout = '20s'", "BEGIN", "LOCK TABLE t_wv1 IN ACCESS EXCLUSIVE MODE", "COMMIT"]))
    c.start()
    _qx_sess(dsn, "pexport_qx_va", [
        "SET statement_timeout = '20s'", "SET deadlock_timeout = '10s'", "BEGIN",
        "DO $$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = "
        "'t_wv1'::regclass AND mode = 'AccessShareLock' AND NOT granted); PERFORM pg_sleep(0.02); END LOOP; END $$",
        "LOCK TABLE pgcolumnar.row_group IN ACCESS EXCLUSIVE MODE", "LOCK TABLE t_wv_w IN SHARE MODE", "COMMIT"])
    b.join(timeout=60)
    ms = int((time.monotonic() - t0) * 1000)
    c.join(timeout=30)
    said = out[0] if out else "the export did not return"
    det = detail[0] if detail else ""
    names = ("detail names the run lock"
             if re.search(r"waits for ShareLock on advisory lock \[[0-9]+,[0-9]+,0,108\]", det)
             else f"detail [{det.splitlines()[0] if det else ''}]")
    return f"{said} | {names} | {_qx_under(ms, 8)}"


def _qx_lock_waits_logged(cluster):
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return text.count("still waiting for ShareLock on advisory lock")


@pytest.fixture(scope="module")
def pep(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        db = _Db(dsn)
        db.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        w = server_dir / "pep"
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        before = _crashes(pgc_cluster)
        r = {}

        # ---- single columnar table: 80000 rows / 5000 per row group = 16 groups
        db.run(f"CREATE TABLE t_col ({COLS}) USING pgcolumnar")
        db.run("SELECT pgcolumnar.set_options('t_col'::regclass, stripe_row_limit => 5000)")
        db.run(f"INSERT INTO t_col {GEN_COL}")
        r["src"] = db.set_hash(GEN_COL)
        db.one(f"SELECT pgcolumnar.export_parquet('t_col'::regclass, '{w}/serial.parquet')")
        r["serial"] = db.set_hash(f"SELECT * FROM pgcolumnar.read_parquet('{w}/serial.parquet') AS t({RB})")
        for n in (1, 2, 4):
            d = w / f"st_{n}"
            r[f"single {n}"] = {
                "n": db.one(f"SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '{d}', {n})"),
                "rb": db.set_hash(f"SELECT * FROM pgcolumnar.read_parquet('{d}') AS t({RB})"),
                "files": _nfiles(d), "success": _success(d), "success empty": _success_empty(d)}

        # ---- partitioned: k = g%600, so t_p3 (750..) holds no rows
        db.run(f"CREATE TABLE t_part ({COLS}) PARTITION BY RANGE (k)")
        for name, lo, hi in (("t_p0", 0, 250), ("t_p1", 250, 500), ("t_p2", 500, 750), ("t_p3", 750, 2000)):
            db.run(f"CREATE TABLE {name} PARTITION OF t_part FOR VALUES FROM ({lo}) TO ({hi}) USING pgcolumnar")
        db.run(f"INSERT INTO t_part {GEN_PART}")
        r["psrc"] = db.set_hash(GEN_PART)
        for n in (1, 2, 4):
            d = w / f"pt_{n}"
            r[f"part {n}"] = {
                "n": db.one(f"SELECT pgcolumnar.parallel_export_parquet('t_part'::regclass, '{d}', {n})"),
                "rb": db.set_hash(f"SELECT * FROM pgcolumnar.read_parquet('{d}') AS t({RB})"),
                "files": _nfiles(d)}

        # ---- empty input
        db.run(f"CREATE TABLE t_empty ({COLS}) USING pgcolumnar")
        r["empty n"] = db.one(f"SELECT pgcolumnar.parallel_export_parquet('t_empty'::regclass, '{w}/empty', 2)")
        r["empty rb"] = db.set_hash(f"SELECT * FROM pgcolumnar.read_parquet('{w}/empty') AS t({RB})")

        # ---- an export inside a transaction holding uncommitted rows (#329)
        db.run(f"CREATE TABLE t_tx ({COLS}) USING pgcolumnar")
        db.run("SELECT pgcolumnar.set_options('t_tx'::regclass, stripe_row_limit => 3000)")
        db.run(f"INSERT INTO t_tx {GEN_TX_COMMITTED}")
        try:
            with psycopg.connect(dsn) as conn:
                conn.execute("INSERT INTO t_tx SELECT g, g, g::float8/7, 'u'||g "
                             "FROM generate_series(100001,100500) g")
                conn.execute(f"SELECT pgcolumnar.parallel_export_parquet('t_tx'::regclass, '{w}/tx', 4)")
                conn.commit()
        except psycopg.Error as exc:
            print(f"-- in-txn export failed: {str(exc).splitlines()[0]}")
        rb_tx = f"SELECT * FROM pgcolumnar.read_parquet('{w}/tx') AS t({RB})"
        r["tx count"] = db.one(f"SELECT count(*) FROM ({rb_tx}) s")
        r["tx distinct"] = db.one(f"SELECT count(DISTINCT id) FROM ({rb_tx}) s")
        r["tx rb"] = db.set_hash(rb_tx)
        r["tx committed"] = db.set_hash(GEN_TX_COMMITTED)

        # ---- a cancelled export leaves a clean directory (item 2)
        db.one(f"SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '{w}/cx_ok', 2)")
        r["control"] = "yes" if _nfiles(w / "cx_ok") >= 1 else "no"
        db.run(f"CREATE TABLE t_big ({COLS}) USING pgcolumnar")
        db.run("SELECT pgcolumnar.set_options('t_big'::regclass, stripe_row_limit => 4000)")
        db.run("INSERT INTO t_big SELECT g, g%1000, g::float8/7, 'r' FROM generate_series(1,20000000) g")
        cx = w / "cx"
        outcome = []
        bg = threading.Thread(target=_cancel_run, args=(dsn, cx, outcome), daemon=True)
        bg.start()
        r["wrote"] = "no"
        for _ in range(200):
            if _anyfiles(cx) >= 1:
                r["wrote"] = "yes"
                break
            time.sleep(0.1)
        db.one("SELECT count(pg_cancel_backend(pid)) FROM pg_stat_activity "
               "WHERE query LIKE '%parallel_export_parquet%' AND state = 'active' "
               "AND pid <> pg_backend_pid()")
        bg.join(timeout=180)
        said = outcome[0] if outcome else "the export did not return"
        r["cancelled"] = ("cancelled" if re.search(r"canceling statement|canceled on user request", said, re.I)
                          else f"completed ({said})")
        r["leftover"] = _anyfiles(cx)
        r["no marker"] = "present" if (cx / "_SUCCESS").exists() else "absent"
        db.one(f"SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '{cx}', 2)")
        r["retry"] = "yes" if _nfiles(cx) >= 1 else "no"
        r["retry marker"] = _success(cx)

        # ---- a local cancel waits for the workers before it deletes (#1864 review)
        lw = w / "cxw"
        lw_out = []
        bg = threading.Thread(target=_cancel_run, args=(dsn, lw, lw_out), daemon=True)
        bg.start()
        for _ in range(200):
            if _anyfiles(lw) >= 1:
                break
            time.sleep(0.1)
        worker = db.one("SELECT pid FROM pg_stat_activity "
                        "WHERE backend_type = 'pgcolumnar parallel_export worker' LIMIT 1")
        stopped, cont = False, None
        try:
            os.kill(int(worker), signal.SIGSTOP)
            stopped = True
            cont = threading.Timer(2.0, lambda: _signal_quietly(int(worker), signal.SIGCONT))
            cont.start()
        except (TypeError, ValueError, OSError):
            pass
        db.one("SELECT count(pg_cancel_backend(pid)) FROM pg_stat_activity "
               "WHERE query LIKE '%parallel_export_parquet%' AND state = 'active' "
               "AND pid <> pg_backend_pid()")
        bg.join(timeout=180)
        alive = 0
        if stopped:
            try:
                os.kill(int(worker), 0)
                alive = 1
            except OSError:
                pass
        if cont is not None:
            cont.join()
        said = lw_out[0] if lw_out else "the export did not return"
        r["lw stopped"] = "yes" if stopped else f"no (worker pid [{worker}])"
        r["lw cancelled"] = ("cancelled" if re.search(r"canceling statement|canceled on user request", said, re.I)
                             else f"completed ({said})")
        r["lw alive"] = alive

        # ---- the destination-length guard (#863)
        premise = _premise(pgc_cluster)
        r["ok"] = _length_arm(db, w, LEN_OK, premise)
        r["over"] = _length_arm(db, w, LEN_OVER, premise)
        r["window"] = _length_arm(db, w, LEN_WINDOW, premise)

        # ---- the refusals, each by its own message
        r["non-empty"] = db.refusal(f"SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '{w}/st_1', 2)",
                                    r'output directory ".*" is not empty')
        db.run(f"CREATE TABLE t_heap2 ({COLS})")
        r["heap"] = db.refusal(
            f"SELECT pgcolumnar.parallel_export_parquet('t_heap2'::regclass, '{w}/heaptgt', 2)",
            r"is not a columnar table or a partitioned table with columnar partitions")

        # ---- the workers use the caller's export_group_bytes (#1843 review)
        db.run("CREATE TABLE t_cap (id int, s text) USING pgcolumnar")
        db.run("SELECT pgcolumnar.set_options('t_cap'::regclass, stripe_row_limit => 1000)")
        db.run("INSERT INTO t_cap SELECT g, repeat('x', 3000) FROM generate_series(1, 4000) g")
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("SET pgcolumnar.export_group_bytes = 65536")
                row = conn.execute("SELECT pgcolumnar.parallel_export_parquet("
                                   f"'t_cap'::regclass, '{w}/capdir', 2)").fetchone()
                r["cap n"] = row[0]
        except psycopg.Error as exc:
            r["cap n"] = _err(exc)
        r["cap dir"] = w / "capdir"

        # ---- a lock the caller holds is refused, not waited on (#2183)
        db.run(f"CREATE TABLE t_lk ({COLS}) USING pgcolumnar")
        db.run(f"INSERT INTO t_lk {GEN_TX_COMMITTED}")
        db.run("CREATE TABLE t_lk_other (a int)")
        for key, setup, table in (
                ("lk ael", "LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE", "t_lk"),
                ("lk trunc", "TRUNCATE t_lk", "t_lk"),
                ("lk share", "LOCK TABLE t_lk IN SHARE MODE", "t_lk"),
                ("lk other", "LOCK TABLE t_lk_other IN ACCESS EXCLUSIVE MODE", "t_lk"),
                ("lk leaf", "LOCK TABLE t_p1 IN ACCESS EXCLUSIVE MODE", "t_part"),
                ("lk subx", "SAVEPOINT s; LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE", "t_lk")):
            r[key] = _self_lock_export(dsn, setup, table, w / key.replace(" ", "_"))
        # the HINT of the ACCESS EXCLUSIVE refusal (#2215, #2226 review)
        try:
            with psycopg.connect(dsn) as conn:
                conn.execute("SET statement_timeout = '5s'")
                conn.execute("LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE")
                conn.execute(f"SELECT pgcolumnar.parallel_export_parquet('t_lk'::regclass, '{w / 'lk_hint'}', 2)")
                conn.commit()
            r["lk hint"] = "no error"
        except psycopg.Error as exc:
            r["lk hint"] = (exc.diag.message_hint or "no hint") if exc.sqlstate else _err(exc)

        # ---- #2245: the caller waits for its workers in the lock manager
        r["qx single"] = _qx_queue(db, dsn, "t_lk", "t_lk", w / "qx_single")
        r["qx leaf"] = _qx_queue(db, dsn, "t_part", "t_p1", w / "qx_leaf")
        r["qx cycle"] = _qx_cycle(db, dsn, w / "qx_cycle")
        r["qx lock timeout"] = _qx_lock_timeout(db, dsn, w / "qx_lt")
        lw_before = _qx_lock_waits_logged(pgc_cluster)
        got = _qx_result(_qx_sess(dsn, "pexport_qx_n", [
            f"SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '{w}/qx_1ms', 2)"],
            options="-c statement_timeout=30s -c lock_timeout=1ms -c deadlock_timeout=1ms -c log_lock_waits=on"))
        lw_after = _qx_lock_waits_logged(pgc_cluster)
        logged = (lw_after - lw_before if isinstance(lw_before, int) and isinstance(lw_after, int)
                  else f"no reading ({lw_before}, {lw_after})")
        r["qx 1ms"] = f"{got} | {logged} lock waits logged"
        shown = []
        try:
            with psycopg.connect(dsn, application_name="pexport_qx_g") as conn:
                conn.execute("SET LOCAL lock_timeout = '2s'")
                conn.execute("SET LOCAL log_lock_waits = on")
                shown.append(conn.execute("SELECT pgcolumnar.parallel_export_parquet("
                                          f"'t_lk'::regclass, '{w}/qx_guc', 2)").fetchone()[0])
                shown.append(conn.execute("SHOW lock_timeout").fetchone()[0])
                shown.append(conn.execute("SHOW log_lock_waits").fetchone()[0])
                conn.commit()
            r["qx guc"] = "|".join(str(x) for x in shown)
        except psycopg.Error as exc:
            r["qx guc"] = _err(exc) if exc.sqlstate is None else f"{exc.sqlstate} {exc.diag.message_primary or ''}"

        db.run(f"CREATE TABLE t_wv ({COLS}) PARTITION BY RANGE (id)")
        db.run("CREATE TABLE t_wv1 PARTITION OF t_wv FOR VALUES FROM (MINVALUE) TO (10001) USING pgcolumnar")
        db.run("CREATE TABLE t_wv2 PARTITION OF t_wv FOR VALUES FROM (10001) TO (MAXVALUE) USING pgcolumnar")
        db.run(f"INSERT INTO t_wv {GEN_TX_COMMITTED}")
        db.run("CREATE TABLE t_wv_w (a int)")
        r["qx victim"] = _qx_victim(db, dsn, w / "qx_victim")

        r["up"] = _survived(pgc_cluster, db, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _shown(v):
    """A reading for expect.text: a failed query shown as "no reading (...)", so the
    check reds by its own name with the error in the output (#1522)."""
    if isinstance(v, str) and v.startswith(QUERY_ERROR):
        return f"no reading ({v})"
    return v


def _num(v):
    """A count for expect.num, or -1 with the error printed: expect.num refuses a
    non-number with a VacuityError rather than a named red (#1522)."""
    if isinstance(v, int):
        return v
    print(f"-- not a count: {v}")
    return -1


def test_pexport_serial_export_reads_back_the_source(pep, expect):
    expect.text(_shown(pep["serial"]), _shown(pep["src"]), "serial export read-back == source")


@pytest.mark.parametrize("n,name", [
    (1, "single(1): rows returned == source count"),
    (2, "single(2): rows returned == source count"),
    (4, "single(4): rows returned == source count")])
def test_pexport_single_returns_the_source_count(pep, expect, n, name):
    expect.num(_num(pep[f"single {n}"]["n"]), NSRC, name)


@pytest.mark.parametrize("n,name", [
    (1, "single(1): read-back == source"),
    (2, "single(2): read-back == source"),
    (4, "single(4): read-back == source")])
def test_pexport_single_reads_back_the_source(pep, expect, n, name):
    expect.text(_shown(pep[f"single {n}"]["rb"]), _shown(pep["src"]), name)


@pytest.mark.parametrize("n,name", [
    (1, "single(1): read-back == serial export"),
    (2, "single(2): read-back == serial export"),
    (4, "single(4): read-back == serial export")])
def test_pexport_single_reads_back_the_serial_export(pep, expect, n, name):
    expect.text(_shown(pep[f"single {n}"]["rb"]), _shown(pep["serial"]), name)


@pytest.mark.parametrize("n,name", [
    (1, "single(1): wrote exactly 1 files (distinct per worker)"),
    (2, "single(2): wrote exactly 2 files (distinct per worker)"),
    (4, "single(4): wrote exactly 4 files (distinct per worker)")])
def test_pexport_single_writes_one_file_per_worker(pep, expect, n, name):
    expect.num(pep[f"single {n}"]["files"], n, name)


@pytest.mark.parametrize("n,name", [
    (1, "single(1): a _SUCCESS completion marker is written"),
    (2, "single(2): a _SUCCESS completion marker is written"),
    (4, "single(4): a _SUCCESS completion marker is written")])
def test_pexport_single_writes_a_success_marker(pep, expect, n, name):
    expect.text(pep[f"single {n}"]["success"], "yes", name)


@pytest.mark.parametrize("n,name", [
    (1, "single(1): the _SUCCESS marker is empty"),
    (2, "single(2): the _SUCCESS marker is empty"),
    (4, "single(4): the _SUCCESS marker is empty")])
def test_pexport_single_success_marker_is_empty(pep, expect, n, name):
    expect.text(pep[f"single {n}"]["success empty"], "empty", name)


@pytest.mark.parametrize("n,name", [
    (1, "part(1): rows returned == source count"),
    (2, "part(2): rows returned == source count"),
    (4, "part(4): rows returned == source count")])
def test_pexport_part_returns_the_source_count(pep, expect, n, name):
    expect.num(_num(pep[f"part {n}"]["n"]), NPART, name)


@pytest.mark.parametrize("n,name", [
    (1, "part(1): read-back == source"),
    (2, "part(2): read-back == source"),
    (4, "part(4): read-back == source")])
def test_pexport_part_reads_back_the_source(pep, expect, n, name):
    expect.text(_shown(pep[f"part {n}"]["rb"]), _shown(pep["psrc"]), name)


@pytest.mark.parametrize("n,name", [
    (1, "part(1): one file per partition (4, incl the empty one)"),
    (2, "part(2): one file per partition (4, incl the empty one)"),
    (4, "part(4): one file per partition (4, incl the empty one)")])
def test_pexport_part_writes_one_file_per_partition(pep, expect, n, name):
    expect.num(pep[f"part {n}"]["files"], 4, name)


def test_pexport_empty_returns_zero_rows(pep, expect):
    expect.num(_num(pep["empty n"]), 0, "empty: rows returned == 0")


def test_pexport_empty_reads_back_the_empty_set(pep, expect):
    expect.text(_shown(pep["empty rb"]), "EMPTY", "empty: read-back is the empty set")


def test_pexport_in_txn_count_is_the_committed_count(pep, expect):
    expect.num(_num(pep["tx count"]), NC, "in-txn export: read-back count == committed count (no duplication)")


def test_pexport_in_txn_has_no_duplicate_ids(pep, expect):
    expect.num(_num(pep["tx distinct"]), NC, "in-txn export: no duplicate ids in the read-back")


def test_pexport_in_txn_reads_back_the_committed_rows(pep, expect):
    expect.text(_shown(pep["tx rb"]), _shown(pep["tx committed"]),
                "in-txn export: read-back == committed rows (uncommitted rows absent)")


def test_pexport_cancel_control_writes_files(pep, expect):
    expect.text(pep["control"], "yes", "cancel-control: a completed export writes files")


def test_pexport_a_file_was_on_disk_before_the_cancel(pep, expect):
    expect.text(pep["wrote"], "yes", "a file was on disk before the cancel (cleanup has work to do)")


def test_pexport_the_export_was_cancelled_mid_flight(pep, expect):
    expect.text(pep["cancelled"], "cancelled",
                "the export was cancelled mid-flight, not completed first (fixture premise)")


def test_pexport_a_cancelled_export_leaves_no_files(pep, expect):
    expect.num(pep["leftover"], 0, "a cancelled export leaves no partial files (item 2)")


def test_pexport_a_cancelled_export_writes_no_marker(pep, expect):
    expect.text(pep["no marker"], "absent", "a cancelled export writes no _SUCCESS marker")


def test_pexport_a_retry_into_the_cleaned_directory_succeeds(pep, expect):
    expect.text(pep["retry"], "yes", "retry into the cleaned directory succeeds")


def test_pexport_the_retry_writes_a_fresh_marker(pep, expect):
    expect.text(pep["retry marker"], "yes", "the retry writes a fresh _SUCCESS marker")


def test_pexport_local_stop_premise_a_worker_was_stopped(pep, expect):
    expect.text(pep["lw stopped"], "yes", "premise: local stop: a worker was stopped before the cancel")


def test_pexport_local_stop_premise_the_export_was_cancelled(pep, expect):
    expect.text(pep["lw cancelled"], "cancelled", "premise: local stop: the export was cancelled, not completed")


def test_pexport_local_stop_no_worker_runs_when_the_cancel_returns(pep, expect):
    expect.num(pep["lw alive"], 0, "local stop: no worker is still running when the cancel returns")


def test_pexport_accepted_boundary_length(pep, expect):
    expect.num(_num(pep["ok"]["len"]), LEN_OK, "accepted boundary: the destination is exactly MAXPGPATH-40 bytes")


def test_pexport_accepted_boundary_still_exports(pep, expect):
    expect.text(_shown(pep["ok"]["state"]), "none", "accepted boundary: a 984-byte destination still exports")


def test_pexport_accepted_boundary_wrote_both_parts(pep, expect):
    expect.num(_num(pep["ok"].get("files")), 2, "accepted boundary: it wrote both part files")


def test_pexport_accepted_boundary_wrote_a_marker(pep, expect):
    expect.text(_shown(pep["ok"].get("success", pep["ok"]["state"])), "yes",
                "accepted boundary: it wrote a _SUCCESS marker")


def test_pexport_rejected_boundary_length(pep, expect):
    expect.num(_num(pep["over"]["len"]), LEN_OVER, "rejected boundary: the destination is exactly MAXPGPATH-39 bytes")


def test_pexport_rejected_boundary_raises_54000(pep, expect):
    expect.text(_shown(pep["over"]["state"]), TOO_LONG, "rejected boundary: a 985-byte destination raises 54000")


def test_pexport_rejected_boundary_creates_nothing(pep, expect):
    expect.text(_shown(pep["over"].get("created", pep["over"]["state"])), "absent",
                "rejected boundary: the destination was not created")


def test_pexport_window_length(pep, expect):
    expect.num(_num(pep["window"]["len"]), LEN_WINDOW, "cleanup-scan window: the destination is exactly MAXPGPATH-30 bytes")


def test_pexport_window_raises_54000(pep, expect):
    expect.text(_shown(pep["window"]["state"]), TOO_LONG, "cleanup-scan window: a 994-byte destination raises 54000")


def test_pexport_window_message_is_ours(pep, expect):
    expect.text("ours" if "destination is too long" in pep["window"]["msg"].lower() else "other", "ours",
                "cleanup-scan window: and it is OUR message, not another 54000")


def test_pexport_window_creates_nothing(pep, expect):
    expect.text(_shown(pep["window"].get("created", pep["window"]["state"])), "absent",
                "cleanup-scan window: the destination was not created")


def test_pexport_rejects_a_non_empty_directory(pep, expect):
    expect.text(_shown(pep["non-empty"]), "refused", "reject a non-empty output directory")


def test_pexport_rejects_a_non_columnar_target(pep, expect):
    expect.text(_shown(pep["heap"]), "refused", "reject a non-columnar target")


def test_pexport_the_server_survives_the_exports(pep, expect):
    expect.text(pep["up"], "1", "server still up after the exports")


def _cap_groups(pq, d):
    """-> "rows read back|the most rows in any row group", over the part files."""
    rows, most = 0, 0
    for f in sorted(pathlib.Path(d).glob("part-*.parquet")):
        md = pq.ParquetFile(str(f)).metadata
        rows += md.num_rows
        most = max([most] + [md.row_group(i).num_rows for i in range(md.num_row_groups)])
    return f"{rows}|{most}"


def test_pexport_workers_use_the_callers_group_byte_cap(pep, expect):
    """#1843 review: background workers start with the server's settings, so a SET of
    the byte cap did not reach them. Each row buffers 3010 bytes, so under a 65536-byte
    cap a group holds at most 21 rows."""
    pq = expect.require_module("pyarrow.parquet",
                               name="the workers cut row groups at the caller's export_group_bytes: 4000 rows of 3000 bytes under a 65536-byte cap, at most 21 rows a group")
    if pq is None:
        return
    try:
        groups = _cap_groups(pq, pep["cap dir"])
    except (OSError, ValueError) as exc:
        groups = f"unreadable: {exc}"
    expect.text(f"{_shown(pep['cap n'])}|{groups}", "4000|4000|21",
                "the workers cut row groups at the caller's export_group_bytes: 4000 rows of 3000 bytes under a 65536-byte cap, at most 21 rows a group")


@pytest.mark.parametrize("key,want,name", [
    ("lk ael", '55000|pgcolumnar.parallel_export_parquet cannot run while this transaction holds AccessExclusiveLock'
     ' on "t_lk"',
     "parallel_export_parquet after LOCK TABLE IN ACCESS EXCLUSIVE MODE in the same transaction is refused"
     " with 55000, not waited on (#2183)"),
    ("lk trunc", '55000|pgcolumnar.parallel_export_parquet cannot run while this transaction holds AccessExclusiveLock'
     ' on "t_lk"',
     "parallel_export_parquet after TRUNCATE in the same transaction is refused with 55000, not waited on (#2183)"),
    ("lk share", "none|20000",
     "parallel_export_parquet after LOCK TABLE IN SHARE MODE in the same transaction still exports (#2183)"),
    ("lk other", "none|20000",
     "parallel_export_parquet while the transaction holds ACCESS EXCLUSIVE on another table still exports (#2183)"),
    ("lk leaf", '55000|pgcolumnar.parallel_export_parquet cannot run while this transaction holds AccessExclusiveLock'
     ' on "t_p1"',
     "parallel_export_parquet of a partitioned table after LOCK TABLE on one leaf IN ACCESS EXCLUSIVE MODE is"
     " refused with 55000 (#2183)"),
    ("lk subx", "25001|cannot export a snapshot from a subtransaction",
     "parallel_export_parquet in a savepoint after LOCK TABLE IN ACCESS EXCLUSIVE MODE keeps the subtransaction"
     " refusal 25001 (#2183)")])
def test_pexport_a_lock_the_caller_holds_is_refused_not_waited_on(pep, expect, key, want, name):
    """#2183: the workers take ACCESS SHARE on the table, or each leaf, in their own
    transactions, outside the caller's lock group, and the caller waits for them on a
    latch. A caller holding ACCESS EXCLUSIVE (LOCK TABLE, TRUNCATE) waited until
    statement_timeout; it is now refused with 55000, by SQLSTATE and message. A lock
    the workers do not conflict with still exports the committed rows."""
    expect.text(_shown(pep[key]), want, name)


def test_pexport_lock_refusal_hint_does_not_name_the_projection_functions(pep, expect):
    """#2215, #2226 review: the refusal's add_projection and drop_projection clause is
    parallel_copy's alone. Their SHARE and SHARE ROW EXCLUSIVE do not conflict with the
    export workers' ACCESS SHARE, so export's HINT must not name them."""
    expect.text(_shown(pep["lk hint"]),
                "Run it in a transaction that does not hold that lock, for example before the LOCK TABLE or"
                " TRUNCATE, or after COMMIT.",
                "parallel_export_parquet refused after LOCK TABLE IN ACCESS EXCLUSIVE MODE gives a HINT that does"
                " not name add_projection or drop_projection (#2215)")


def test_pexport_queued_access_exclusive_on_the_table_does_not_hang(pep, expect):
    """#2245: ACCESS EXCLUSIVE queued behind the caller's ACCESS SHARE; the workers
    queued behind it, and the caller waited for them on a latch, which the deadlock
    detector does not follow. The caller now waits on the workers' run locks."""
    expect.text(pep["qx single"], f"no error ({NC}) | under 6 s | other session locked | {NC} rows read back",
                "parallel_export_parquet with ACCESS EXCLUSIVE on the table queued behind the caller exports within 6 s, not a wait until statement_timeout, and the other session then gets its lock (#2245)")


def test_pexport_queued_access_exclusive_on_a_leaf_does_not_hang(pep, expect):
    expect.text(pep["qx leaf"], f"no error ({NPART}) | under 6 s | other session locked | {NPART} rows read back",
                "parallel_export_parquet of a partitioned table with ACCESS EXCLUSIVE on one leaf queued behind the caller exports within 6 s, not a wait until statement_timeout, and the other session then gets its lock (#2245)")


def test_pexport_control_a_cycle_through_the_dispatcher_ends_in_40p01(pep, expect):
    """The dispatcher waits for the table in the lock manager before any worker
    starts, so this cycle was already seen on main."""
    expect.text(pep["qx cycle"], "one side 40P01, the other went on | under 5 s",
                "control: a session holding ACCESS EXCLUSIVE on the table that then waits for a lock the parallel_export_parquet caller holds gets 40P01 on one side within 5 s (#2245)")


def test_pexport_the_callers_lock_timeout_bounds_its_wait_for_the_table(pep, expect):
    expect.text(pep["qx lock timeout"], "55P03 canceling statement due to lock timeout | under 4 s",
                "the caller's lock_timeout of 1 s ends a parallel_export_parquet that waits for another session's ACCESS EXCLUSIVE on the table with 55P03 within 4 s (#2245)")


def test_pexport_the_waits_on_the_workers_ignore_lock_timeout_and_are_not_logged(pep, expect):
    """The run-lock waits are waits for the export, not lock contention."""
    expect.text(pep["qx 1ms"], f"no error ({NSRC}) | 0 lock waits logged",
                "parallel_export_parquet under lock_timeout 1 ms, deadlock_timeout 1 ms and log_lock_waits on still exports, and logs no wait for its workers (#2245)")


def test_pexport_the_callers_settings_read_back_unchanged(pep, expect):
    expect.text(pep["qx guc"], f"{NC}|2s|on",
                "the caller's lock_timeout and log_lock_waits read back unchanged after parallel_export_parquet in the same transaction (#2245)")


def test_pexport_a_worker_deadlock_victim_reaches_the_caller_as_40p01(pep, expect):
    """#2245: whichever process the deadlock detector picks, the caller reports 40P01.
    The worker's error, with its detail, is re-raised by the caller, not "exited
    without reporting a result"."""
    expect.text(pep["qx victim"],
                "40P01 pgcolumnar.parallel_export_parquet worker 0 failed: deadlock detected | detail names the run lock | under 8 s",
                "a parallel_export_parquet worker chosen as the deadlock victim reaches the caller as 40P01 with the cycle in its detail, within 8 s (#2245)")


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


def _doc_counts(page):
    """Every explicit worker count in a page's parallel_export_parquet calls."""
    text = (SRCDIR / "docs" / page).read_text()
    return [int(n) for n in re.findall(r"parallel_export_parquet\('[^']*', *'[^']*', *([0-9]+)", text)]


def _doc_figure(page):
    """The page's default-server worker figure ("of 8 fits six workers" or "of 8, six
    workers fit"), or None."""
    text = " ".join((SRCDIR / "docs" / page).read_text().split())
    m = re.search(r"of 8,? (?:fits ([a-z]+) workers|([a-z]+) workers fit)", text)
    return _WORDS.get((m.group(1) or m.group(2))) if m else None


def test_pexport_the_documented_worker_counts_fit_a_default_server(pep, pgc_cluster, server_dir, expect):
    """#2191: each worker takes one background worker slot, and the server's own
    background workers hold slots too. The docs asked for 8 workers, which failed on a
    table of 7 or more row groups at the default max_worker_processes = 8. Every
    explicit count on each page is read from the page and exported with, from a table
    of 16 row groups. The figure the SQL reference and the user guide give is the
    largest that fits: it exports, and one worker more is refused with 53000."""
    db = _Db(pgc_cluster.dsn(dbname=DB))
    expect.text(db.one("SELECT (setting = boot_val)::text FROM pg_settings WHERE name = 'max_worker_processes'"), "true",
                "premise: the cluster runs at the default max_worker_processes")
    db.run("CREATE TABLE t_docw (id int, txt text) USING pgcolumnar")
    db.run("SELECT pgcolumnar.set_options('t_docw'::regclass, stripe_row_limit => 1000)")
    db.run("INSERT INTO t_docw SELECT g, 'r' || g FROM generate_series(1, 16000) g")
    expect.num(db.one("SELECT count(*) FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s USING (storage_id) "
                      "WHERE s.relation_oid = 't_docw'::regclass"), 16,
               "premise: the documented-count table holds 16 row groups, more than any count tried")
    # The earlier tests' workers must have exited, or they still hold slots. Bounded.
    deadline = time.monotonic() + 10
    while db.one("SELECT count(*) FROM pg_stat_activity "
                 "WHERE backend_type LIKE 'pgcolumnar parallel_export%'") != 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    out = server_dir / "pep_docw"
    out.mkdir(exist_ok=True)
    out.chmod(0o777)
    seq = iter(range(1, 1000))

    def export(n):
        """-> rows exported, or "SQLSTATE: message" for a refusal."""
        sql = f"SELECT pgcolumnar.parallel_export_parquet('t_docw'::regclass, '{out}/docw_{next(seq)}', {n})"
        try:
            with psycopg.connect(db.dsn, autocommit=True) as conn:
                return str(conn.execute(sql).fetchone()[0])
        except psycopg.Error as exc:
            return f"{exc.sqlstate}: {exc.diag.message_primary}"

    def runs(page):
        got = " ".join(f"{n}={export(n)}" for n in _doc_counts(page))
        want = " ".join(f"{n}=16000" for n in _doc_counts(page))
        return got or "no worker count found", want or "at least one worker count"

    got, want = runs("how-to.md")
    expect.text(got, want,
                "the how-to's parallel_export_parquet worker counts export at the default max_worker_processes")
    got, want = runs("user-guide.md")
    expect.text(got, want,
                "the user guide's parallel_export_parquet worker counts export at the default max_worker_processes")
    got, want = runs("sql-reference.md")
    expect.text(got, want,
                "the SQL reference's parallel_export_parquet worker counts export at the default max_worker_processes")
    fig = _doc_figure("sql-reference.md")
    expect.text(f"sql-reference.md:{fig} user-guide.md:{_doc_figure('user-guide.md')}",
                f"sql-reference.md:{fig} user-guide.md:{fig if fig else 'a-figure'}",
                "the SQL reference and the user guide give the same default-server worker figure")
    expect.text(export(fig) if fig else "no figure on the page", "16000",
                "the documented default-server worker figure exports at the default max_worker_processes")
    expect.text(export(fig + 1) if fig else "no figure on the page",
                f"53000: could not register pgcolumnar parallel_export worker {(fig or 0) + 1} of {(fig or 0) + 1}",
                "and one worker more is refused with 53000")

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
most, inside the cluster's default max_worker_processes of 8.
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

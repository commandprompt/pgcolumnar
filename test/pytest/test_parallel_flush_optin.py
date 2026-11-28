"""pgcolumnar.parallel_flush stays opt-in, and the metric a default-on gate would need
cannot separate opposite-cost shapes (#445). The pytest twin of
`test/parallel_flush_optin.sh`, every check by the shell's name (#1341).

Asserted: the GUC is off by default and a default flush dispatches serial; with it on,
a wide-numeric flush dispatches parallel AND workers produce its columns, and the
stored chunks and rows are identical to the serial path's; and a random and a constant
bigint table produce the same dispatch metric.

TWO FIXES THE SHELL SHARES. The dispatch line says "-> parallel" before any worker is
registered, so with no worker ever registered the old ON check passed; the split line
("N of M columns by K worker(s)") must now show worker columns. And the eight wnum
columns were eight copies of g, so a parallel assembly that stored each column under
the next index wrote the same bytes; the columns now have eight different shapes, and
the fingerprint adds the rows read back.

The DEBUG1 lines arrive as notices on the inserting session. ON A PRIVATE DATABASE,
built in the shell's order by a module fixture.
"""
import pathlib
import re
import shutil
import tempfile
import threading
import time

import psycopg
import pytest

from pgc_cluster import Cluster, pick_port
from pgc_vacuity import query_error

DB = "pgc_parallel_flush_optin"
WNUM = ("SELECT g, g % 7, ((g::bigint * 7919) % 65537)::int, g / 10, -g, g % 2, 0, (g * 31) % 1000 "
        "FROM generate_series(1,300000) g")
FP = ("SELECT (SELECT md5(string_agg(group_number || ':' || column_index || ':' || value_count || ':' "
      "|| encode(encoding_descriptor, 'hex') || ':' || block_codec || ':' || page_length, '|' "
      "ORDER BY group_number, column_index)) FROM pgcolumnar.column_chunk "
      "WHERE storage_id = pgcolumnar.get_storage_id('{t}')) "
      "|| '/' || (SELECT md5(string_agg(t::text, '|' ORDER BY t.a)) FROM {t} t)")


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

    def q(self, sql):
        """-> the first column as text, "" for NULL, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return _err(exc)
        return "" if row is None or row[0] is None else str(row[0])

    def flush(self, guc, insert):
        """Run guc then insert in one session at DEBUG1 -> (dispatch, split).

        dispatch is the tail of the last dispatch line ("rows=.. -> serial"), "" when
        there is none. split is "workers" when every split line shows worker columns,
        else what was read. A failed session is a query_error() for both."""
        notes = []
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.add_notice_handler(lambda d: notes.append(d.message_primary or ""))
                conn.execute("SET client_min_messages = debug1")
                conn.execute(guc)
                conn.execute(insert)
        except psycopg.Error as exc:
            return _err(exc), _err(exc)
        disp = [m.group(1) for m in (re.search(r"parallel_flush dispatch: (.*-> (parallel|serial))", n)
                                     for n in notes) if m]
        lines = [m.group(0) for m in (re.search(r"parallel_flush: [0-9]+ of [0-9]+ columns by [0-9]+ worker", n)
                                      for n in notes) if m]
        if not lines:
            split = "no split line"
        elif any(re.search(r"parallel_flush: 0 of ", ln) for ln in lines):
            split = f"serial fall-back:[{';'.join(lines)};]"
        else:
            split = "workers"
        return (disp[-1] if disp else ""), split


def _decision(d):
    return d.rsplit("-> ", 1)[-1] if "-> " in d else d


@pytest.fixture(scope="module")
def pf(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {"default": d.q("SHOW pgcolumnar.parallel_flush")}
        d.run("CREATE TABLE wnum (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar")
        r["d default"], _ = d.flush("SELECT 1", f"INSERT INTO wnum {WNUM}")
        d.run("TRUNCATE wnum")
        r["d on"], r["s on"] = d.flush("SET pgcolumnar.parallel_flush = on", f"INSERT INTO wnum {WNUM}")
        r["fp on"] = d.q(FP.format(t="wnum"))
        d.run("TRUNCATE wnum")
        d.flush("SET pgcolumnar.parallel_flush = off", f"INSERT INTO wnum {WNUM}")
        r["fp off"] = d.q(FP.format(t="wnum"))
        d.run("CREATE TABLE i5_rand (a bigint, b bigint, c bigint, d bigint, e bigint) USING pgcolumnar")
        d.run("CREATE TABLE i5_const (a bigint, b bigint, c bigint, d bigint, e bigint) USING pgcolumnar")
        r["m rand"], _ = d.flush("SET pgcolumnar.parallel_flush = on",
                                 "INSERT INTO i5_rand SELECT (random()*9e18)::bigint, (random()*9e18)::bigint, "
                                 "(random()*9e18)::bigint, (random()*9e18)::bigint, (random()*9e18)::bigint "
                                 "FROM generate_series(1,400000) g")
        r["m const"], _ = d.flush("SET pgcolumnar.parallel_flush = on",
                                  "INSERT INTO i5_const SELECT 7::bigint, 7::bigint, 7::bigint, 7::bigint, 7::bigint "
                                  "FROM generate_series(1,400000) g")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# Not a premise (#1339): the boot default is pgcolumnar's own, and setting it to true
# reddens this.
def test_optin_parallel_flush_is_off_by_default(pf, expect):
    expect.text(pf["default"], "off", "parallel_flush is off by default")


def test_optin_a_default_flush_dispatches_serial(pf, expect):
    d = pf["d default"]
    expect.text("serial" if _decision(d) == "serial" else f"parallel:[{d}]", "serial",
                "a default flush dispatches serial")


def test_optin_the_guc_on_flushes_in_workers(pf, expect):
    d, s = pf["d on"], pf["s on"]
    got = f"serial:[{d}]" if _decision(d) != "parallel" else (s if s != "workers" else "parallel")
    expect.text(got, "parallel",
                "with the GUC on, a wide-numeric flush dispatches parallel (not a silent serial fall-back)")


def test_optin_stores_the_serial_paths_chunks(pf, expect):
    expect.text(pf["fp on"], pf["fp off"], "opting in stores byte-identical chunks to the serial path")


def test_optin_the_dispatch_metric_cannot_separate_the_shapes(pf, expect):
    expect.text(pf["m rand"].split(" -> ")[0], pf["m const"].split(" -> ")[0],
                "the dispatch metric is byte-identical for a random and a constant column "
                "(the metric cannot separate opposite-cost shapes)")


# ---- 4. a second cancel during the error cleanup frees what the workers pinned (#1745)
#
# The shell's arm 4, on A PRIVATE CLUSTER: dynamic_shared_memory_type and
# max_worker_processes take effect only at server start, and mmap puts every dynamic
# segment in this cluster's own pg_dynshmem, where it can be counted from SQL. Seven
# cheap columns publish within a second; the text column at zstd 19 keeps one worker
# encoding for many seconds. The first cancel is a 2s statement_timeout, the second a
# pg_cancel_backend at 3.5s, while the cleanup waits for the encoding worker.
DC_INSERT = ("INSERT INTO dc SELECT g, g, g, g, g, g, g, "
             "md5(g::text) || md5((g * 7)::text) || md5((g * 13)::text) "
             "FROM generate_series(1, 150000) g")


def _segments(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        return {r[0] for r in c.execute("SELECT f FROM pg_ls_dir('pg_dynshmem') f")}


def _flush_workers(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute("SELECT count(*) FROM pg_stat_activity "
                         "WHERE backend_type = 'pgcolumnar parallel_flush worker'").fetchone()[0]


@pytest.fixture(scope="module")
def dc(pgc_cluster):
    """-> the readings of one double-cancelled parallel flush on a private cluster."""
    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-pfdc-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "pfdc", datadir, pick_port(41))
    try:
        cluster.initdb()
        cluster.start()
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("ALTER SYSTEM SET max_worker_processes = 16")
            c.execute("ALTER SYSTEM SET dynamic_shared_memory_type = 'mmap'")
        cluster.stop()
        cluster.start()
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE dc (a int, b int, c int, d int, e int, f int, g int, x text) USING pgcolumnar")
        before = _segments(dsn)
        result = {}

        def insert():
            try:
                with psycopg.connect(dsn, autocommit=True) as c:
                    c.execute("SET application_name = 'pf_double_cancel'")
                    c.execute("SET pgcolumnar.parallel_flush = on")
                    c.execute("SET pgcolumnar.compression = zstd")
                    c.execute("SET pgcolumnar.compression_level = 19")
                    c.execute("SET pgcolumnar.stripe_row_limit = 1000000")
                    c.execute("SET statement_timeout = '2s'")
                    c.execute(DC_INSERT)
                result["refused"] = "no error"
            except psycopg.Error as exc:
                result["refused"] = f"{exc.sqlstate}: {exc.diag.message_primary}"

        t = threading.Thread(target=insert)
        t.start()
        time.sleep(3.5)
        r = {"workers at": _flush_workers(dsn), "new at": len(_segments(dsn) - before)}
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                      "WHERE application_name = 'pf_double_cancel'")
        t.join(300)
        for _ in range(120):
            if _flush_workers(dsn) == 0:
                break
            time.sleep(1)
        r["error"] = result.get("refused", "the insert never returned")
        r["left"] = len(_segments(dsn) - before)
        yield r
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def test_double_cancel_a_worker_is_still_encoding(dc, expect):
    w = dc["workers at"]
    expect.text("yes" if w >= 1 else f"no (workers=[{w}])", "yes",
                "premise: a flush worker is still encoding when the second cancel lands")


def test_double_cancel_the_cheap_workers_published(dc, expect):
    n = dc["new at"]
    expect.text("yes" if n >= 2 else f"no (new segments=[{n}])", "yes",
                "premise: the cheap workers published their segments before the second cancel")


def test_double_cancel_reports_the_first_cancel(dc, expect):
    expect.text(dc["error"], "57014: canceling statement due to statement timeout",
                "a twice-cancelled parallel flush reports the first cancel")


def test_double_cancel_leaves_no_segment_pinned(dc, expect):
    expect.text(f"{dc['left']} new segments", "0 new segments",
                "a twice-cancelled parallel flush leaves no worker segment pinned")


# ---- 5. the collect loop forgets a handle as soon as it unpins it (#1753) -----
#
# The shell's arm 5. While the handle stayed set after the collect loop's unpin, an
# error before the detach sent pflush_error_cleanup to attach that segment again,
# and "can't attach the same segment more than once" replaced the original error
# and left the other segments pinned. With comments blanked, the statement right
# after the unpin must forget the handle.
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def _after_unpin():
    text = (SRC / "columnar_write_state.c").read_text()
    m = re.search(r"\nflush_columns_parallel\(.*?\n\{(.*?)\n\}\n", text, re.S)
    if not m:
        return "flush_columns_parallel not found"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    b = re.search(r"dsm_unpin_segment\(slots\[i\]\.outHandle\);\s*([^;]*;)", body, re.S)
    if not b:
        return "the collect loop does not unpin slots[i].outHandle"
    return re.sub(r"\s+", " ", b.group(1)).strip()


def test_the_collect_loop_forgets_a_handle_after_its_unpin(expect):
    expect.text(_after_unpin(), "slots[i].outHandle = DSM_HANDLE_INVALID;",
                "the collect loop forgets a worker's handle in the statement after it unpins the segment")


# ---- 6. nothing raises between a worker's pin and its DONE (#1747) ----------
#
# The shell's arm 6. On the NORMAL path the collect loop unpins only a DONE slot, so a
# worker that raised after pinning its OUTPUT segment left it pinned until postmaster
# restart. The worker now commits first and pins last: from dsm_pin_segment to the
# end of the PG_TRY block, with comments blanked, only the detach and the DONE write
# may run (an ERROR after DONE lands in PG_CATCH too, which overwrites it with FAILED).
def _after_pin():
    text = (SRC / "columnar_write_state.c").read_text()
    m = re.search(r"\npgcolumnar_parallel_flush_worker\(Datum main_arg\)\n\{(.*?)\n\}\n", text, re.S)
    if not m:
        return "pgcolumnar_parallel_flush_worker not found"
    b = re.search(r"dsm_pin_segment\(outseg\);(.*?)\}\s*PG_CATCH\(\);", m.group(1), re.S)
    if not b:
        return "the pin and the PG_CATCH are not both in the worker"
    body = re.sub(r"/\*.*?\*/", "", b.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body or "(nothing)"


def test_a_flush_worker_raises_nothing_after_its_pin(expect):
    expect.text(_after_pin(), "dsm_detach(outseg); pg_atomic_write_u32(&me->state, PFLUSH_DONE);",
                "a flush worker runs nothing that can raise after pinning its output, to the end of its PG_TRY block")


# ---- 7. a flush worker takes no lock that can wait on the table (#2072) ------
#
# The shell's arm 7. Each flush worker opened the table with ACCESS SHARE. The
# workers are not in the inserting backend's lock group, and the backend waits for
# them on a latch, so the deadlock detector cannot see the wait. An INSERT hung until
# statement_timeout when its own transaction held ACCESS EXCLUSIVE (TRUNCATE, LOCK
# TABLE, ALTER TABLE before it), or when another session's ALTER TABLE queued behind
# it. Each case must finish inside its timeout with worker columns, so a fix that
# silently fell back to serial does not pass.
LK_DB = "pgc_parallel_flush_locks"
LK_ROWS = ("SELECT g, g % 7, ((g::bigint * 7919) % 65537)::int, g / 10, -g, g % 2, 0, (g * 31) % 1000 "
           "FROM generate_series(1,20000) g")
LK_WANT = "every column in workers, 20000 rows, sum(a) 200010000"
LR_ALTER = "ALTER COLUMN b TYPE bigint, ALTER COLUMN h TYPE text"
LQ_WAIT = "alter active Lock/relation, insert active"


def _all_split(notes):
    """How every parallel flush in notes was split, read strictly.

    -> "every column in workers" when each split line reads "N of N columns" and no
    worker failed. A worker that fails hands its columns back to the backend, which
    encodes them serially with the right attributes, so the rows still match and a
    reading that only asks for some worker columns still says "workers": only this
    reading sees the failure (#2140 review). Else -> what was read."""
    splits = [m for m in (re.search(r"parallel_flush: ([0-9]+) of ([0-9]+) columns by [0-9]+ worker", n)
                          for n in notes) if m]
    failed = [m.group(0) for m in (re.search(r"worker [0-9]+ failed, completing its columns serially: .*", n)
                                   for n in notes) if m]
    if not splits:
        return "no split line"
    if failed:
        return f"a worker failed:[{failed[0]}]"
    short = sorted({m.group(0) for m in splits if m.group(1) != m.group(2)})
    if short:
        return f"not every column in workers:[{';'.join(short)};]"
    return "every column in workers"


def _lock_flush(db, setup, table="lk"):
    """BEGIN; setup; INSERT into table; COMMIT in one session, parallel_flush on, 15s
    statement_timeout -> the error, or _all_split()'s reading and the rows read back."""
    notes = []
    try:
        with psycopg.connect(db.dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: notes.append(d.message_primary or ""))
            conn.execute("SET client_min_messages = debug1")
            conn.execute("SET pgcolumnar.parallel_flush = on")
            conn.execute("SET statement_timeout = '15s'")
            with conn.transaction():
                for stmt in setup:
                    conn.execute(stmt)
                conn.execute(f"INSERT INTO {table} (a, b, c, d, e, f, g, h) {LK_ROWS}")
    except psycopg.Error as exc:
        return f"{exc.sqlstate}: {exc.diag.message_primary}"
    return f"{_all_split(notes)}, " + db.q(f"SELECT count(*) || ' rows, sum(a) ' || sum(a) FROM {table}")


@pytest.fixture(scope="module")
def lk(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {LK_DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {LK_DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=LK_DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        d.run("CREATE TABLE lk (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar")
        r = {
            "truncate": _lock_flush(d, ["TRUNCATE lk"]),
            "lock": _lock_flush(d, ["TRUNCATE lk", "LOCK TABLE lk IN ACCESS EXCLUSIVE MODE"]),
            "alter": _lock_flush(d, ["TRUNCATE lk", "ALTER TABLE lk ADD COLUMN x int DEFAULT 5"]),
        }
        r["added column"] = d.q("SELECT count(x) || ' values, sum ' || sum(x) FROM lk")

        # The workers must encode with the attributes the inserting transaction sees.
        # An added int column has the same bytes under any int4 attribute; a retype in
        # the same transaction does not. lr is retyped and loaded in parallel, lr_s
        # serially; their chunks and rows must match (#2140 review).
        for t in ("lr", "lr_s"):
            d.run(f"CREATE TABLE {t} (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar")
        r["retype"] = _lock_flush(d, ["TRUNCATE lr", f"ALTER TABLE lr {LR_ALTER}"], table="lr")
        d.run(f"SET pgcolumnar.parallel_flush = off; BEGIN; TRUNCATE lr_s; ALTER TABLE lr_s {LR_ALTER}; "
              f"INSERT INTO lr_s (a, b, c, d, e, f, g, h) {LK_ROWS}; COMMIT")
        r["retype fp"] = (d.q(FP.format(t="lr")), d.q(FP.format(t="lr_s")))

        # The second case: many small stripes, so each stripe starts its own workers
        # while the ALTER waits. The INSERT takes about 8s alone and gets 60s; the
        # ALTER is queued 1s in.
        d.run("CREATE TABLE lq (a int, b int8, c float8) USING pgcolumnar")
        d.run("SELECT pgcolumnar.set_options('lq', stripe_row_limit => 1000)")
        out = {}

        lq_notes = []

        def insert():
            try:
                with psycopg.connect(d.dsn, autocommit=True) as c:
                    c.add_notice_handler(lambda n: lq_notes.append(n.message_primary or ""))
                    c.execute("SET application_name = 'pf_lock_queue'")
                    c.execute("SET pgcolumnar.parallel_flush = on")
                    c.execute("SET client_min_messages = debug1")
                    c.execute("SET statement_timeout = '60s'")
                    cur = c.execute("INSERT INTO lq SELECT g, g, g FROM generate_series(1, 600000) g")
                    out["insert"] = f"INSERT 0 {cur.rowcount}"
            except psycopg.Error as exc:
                out["insert"] = f"{exc.sqlstate}: {exc.diag.message_primary}"

        def alter():
            try:
                with psycopg.connect(d.dsn, autocommit=True) as c:
                    c.execute("SET application_name = 'pf_lock_alter'")
                    c.execute("SET lock_timeout = '120s'")
                    c.execute("ALTER TABLE lq ADD COLUMN d int")
                    out["alter"] = "ALTER TABLE"
            except psycopg.Error as exc:
                out["alter"] = f"{exc.sqlstate}: {exc.diag.message_primary}"

        ti = threading.Thread(target=insert)
        ti.start()
        time.sleep(1)
        ta = threading.Thread(target=alter)
        ta.start()
        # Read both sessions until the ALTER waits on the table lock, for up to 5s,
        # so a slow start does not read as a failed premise.
        for _ in range(25):
            time.sleep(0.2)
            r["alter wait"] = d.q(
                "SELECT 'alter ' || coalesce((SELECT state || ' ' || coalesce(wait_event_type || '/' "
                "|| wait_event, 'not waiting') FROM pg_stat_activity WHERE application_name = "
                "'pf_lock_alter' AND query LIKE 'ALTER%'), 'not started') || ', insert ' || "
                "coalesce((SELECT state FROM pg_stat_activity WHERE application_name = "
                "'pf_lock_queue'), 'gone')")
            if r["alter wait"] == LQ_WAIT:
                break
        ti.join(180)
        ta.join(180)
        r["queued insert"] = out.get("insert", "the insert never returned")
        r["queued alter"] = out.get("alter", "the alter never returned")
        r["queued split"] = _all_split(lq_notes)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {LK_DB} WITH (FORCE)")


def test_lock_a_flush_after_truncate_finishes(lk, expect):
    expect.text(lk["truncate"], LK_WANT,
                "a parallel flush after TRUNCATE in the same transaction finishes, in workers (#2072)")


def test_lock_a_flush_after_lock_table_finishes(lk, expect):
    expect.text(lk["lock"], LK_WANT,
                "a parallel flush after LOCK TABLE IN ACCESS EXCLUSIVE MODE in the same transaction "
                "finishes, in workers (#2072)")


def test_lock_a_flush_after_alter_table_finishes(lk, expect):
    expect.text(lk["alter"], LK_WANT,
                "a parallel flush after ALTER TABLE ADD COLUMN in the same transaction finishes, "
                "in workers (#2072)")


def test_lock_a_flush_writes_the_added_column(lk, expect):
    expect.text(lk["added column"], "20000 values, sum 100000",
                "a parallel flush writes the column ALTER TABLE added in the same transaction (#2072)")


def test_lock_a_flush_after_a_retype_finishes_in_workers(lk, expect):
    expect.text(lk["retype"], LK_WANT,
                "a parallel flush after ALTER COLUMN TYPE in the same transaction finishes, every column "
                "in workers (#2140)")


def test_lock_a_flush_after_a_retype_stores_the_serial_paths_chunks(lk, expect):
    expect.text(lk["retype fp"][0], lk["retype fp"][1],
                "a parallel flush after ALTER COLUMN TYPE in the same transaction stores the serial path's "
                "chunks and rows (#2140)")


def test_lock_the_alter_waited_for_the_insert(lk, expect):
    expect.text(lk["alter wait"], LQ_WAIT,
                "premise: ALTER TABLE was waiting for the table lock while the parallel INSERT ran")


def test_lock_a_queued_alter_does_not_stall_the_insert(lk, expect):
    expect.text(lk["queued insert"], "INSERT 0 600000",
                "a parallel INSERT finishes while another session's ALTER TABLE waits for its lock (#2072)")


def test_lock_the_queued_insert_flushes_in_workers(lk, expect):
    # Or a fix that went serial whenever a lock waits would pass the test above.
    expect.text(lk["queued split"], "every column in workers",
                "a parallel INSERT that another session's ALTER TABLE waits behind flushes every column "
                "in workers (#2140)")


def test_lock_the_queued_alter_completes(lk, expect):
    # A liveness premise: main's code passes it too.
    expect.text(lk["queued alter"], "ALTER TABLE",
                "premise: the waiting ALTER TABLE completes once the parallel INSERT commits (#2072)")


# ---- 8. a column type a worker cannot resolve keeps the flush serial (#2182) -----
#
# The shell's arm 8. A flush worker reads the type cache for each column it encodes.
# For a composite, the type cache opens the composite's table with ACCESS SHARE, and
# so do record_cmp and hash_record for a composite field. The worker is not in the
# inserting backend's lock group, so after TRUNCATE or LOCK TABLE on the composite's
# table in the inserting transaction the INSERT waited until statement_timeout:
# #2072's wait on another table. A worker also runs on its own snapshot, so a type
# created in the inserting transaction was invisible to it: it failed with a WARNING
# and its columns were redone serially. The backend now keeps such a flush serial.
# Every way a worker reaches a composite is tried: directly, as an array element,
# under a domain, as a composite's field, as a range's subtype and in a multirange.
# tyc and ty_n each carry a dropped column, which the walk must step over.
TY_DB = "pgc_parallel_flush_types"
TY_G = "FROM generate_series(1,20000) g"
TY_SERIAL = "serial, no split line, 20000 rows, sum 200010000"
TY_WORKERS = "parallel, every column in workers, 20000 rows, sum 200010000"
TY_LOCK = "LOCK TABLE ty_c IN ACCESS EXCLUSIVE MODE"


def _type_flush(db, setup, insert, table, sum_expr):
    """BEGIN; setup; insert; COMMIT in one session at DEBUG1, parallel_flush on, 10s
    statement_timeout -> the error, or the distinct dispatch decisions, _all_split()'s
    reading and the rows read back."""
    notes = []
    try:
        with psycopg.connect(db.dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: notes.append(d.message_primary or ""))
            conn.execute("SET client_min_messages = debug1")
            conn.execute("SET pgcolumnar.parallel_flush = on")
            conn.execute("SET statement_timeout = '10s'")
            with conn.transaction():
                for stmt in setup:
                    conn.execute(stmt)
                conn.execute(insert)
    except psycopg.Error as exc:
        return f"{exc.sqlstate}: {exc.diag.message_primary}"
    decisions = sorted({m.group(1) for m in (re.search(r"parallel_flush dispatch: .*-> (parallel|serial)", n)
                                             for n in notes) if m})
    dec = "/".join(decisions) if decisions else "no dispatch line"
    rows = db.q(f"SELECT count(*) || ' rows, sum ' || sum({sum_expr}) FROM {table}")
    return f"{dec}, {_all_split(notes)}, {rows}"


@pytest.fixture(scope="module")
def ty(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {TY_DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {TY_DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=TY_DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        for ddl in ("CREATE TABLE ty_c (x int, y int)",
                    "CREATE TABLE ty_n (x int, gone int, z ty_c)",
                    "ALTER TABLE ty_n DROP COLUMN gone",
                    "CREATE DOMAIN ty_d AS ty_c",
                    "CREATE TYPE ty_r AS RANGE (subtype = ty_c)",
                    "CREATE TABLE tyc (a int, b int, gone int, r ty_c) USING pgcolumnar",
                    "ALTER TABLE tyc DROP COLUMN gone",
                    "CREATE TABLE tya (a int, b int, r ty_c[]) USING pgcolumnar",
                    "CREATE TABLE tyd (a int, b int, r ty_d) USING pgcolumnar",
                    "CREATE TABLE tyn (a int, b int, r ty_n) USING pgcolumnar",
                    "CREATE TABLE tyr (a int, b int, r ty_r) USING pgcolumnar",
                    "CREATE TABLE tym (a int, b int, r ty_r_multirange) USING pgcolumnar",
                    "CREATE TABLE tye (a int, b int) USING pgcolumnar",
                    "CREATE TABLE tyk (a int, b int) USING pgcolumnar",
                    "CREATE TABLE tysr (a int, b int) USING pgcolumnar",
                    "CREATE TABLE tyso (a int, b int) USING pgcolumnar",
                    "CREATE TABLE tycr (a int, b int) USING pgcolumnar",
                    "CREATE TABLE tycp (a int, b int) USING pgcolumnar",
                    "CREATE TABLE tyl (a int, b int) USING pgcolumnar",
                    "CREATE COLLATION ty_co (provider = icu, locale = 'und')",
                    "CREATE TABLE tyo (a int, b int, s text COLLATE ty_co) USING pgcolumnar"):
            d.run(ddl)
        rng = "ty_r(row(g, g)::ty_c, row(g + 1, g)::ty_c)"
        r = {
            "truncate": _type_flush(d, ["TRUNCATE ty_c, tyc"],
                                    f"INSERT INTO tyc SELECT g, g, row(g, g)::ty_c {TY_G}", "tyc", "(r).x"),
            "array": _type_flush(d, [TY_LOCK], f"INSERT INTO tya SELECT g, g, array[row(g, g)::ty_c] {TY_G}",
                                 "tya", "(r[1]).x"),
            "domain": _type_flush(d, [TY_LOCK], f"INSERT INTO tyd SELECT g, g, row(g, g)::ty_c {TY_G}",
                                  "tyd", "(r).x"),
            "field": _type_flush(d, [TY_LOCK],
                                 f"INSERT INTO tyn SELECT g, g, row(g, row(g, g)::ty_c)::ty_n {TY_G}",
                                 "tyn", "((r).z).x"),
            "range": _type_flush(d, [TY_LOCK], f"INSERT INTO tyr SELECT g, g, {rng} {TY_G}", "tyr",
                                 "(lower(r)).x"),
            "multirange": _type_flush(d, [TY_LOCK],
                                      f"INSERT INTO tym SELECT g, g, ty_r_multirange({rng}) {TY_G}",
                                      "tym", "(lower(r)).x"),
            "new type": _type_flush(d, ["CREATE TYPE ty_e AS ENUM ('x', 'y')", "ALTER TABLE tye ADD COLUMN e ty_e"],
                                    f"INSERT INTO tye SELECT g, g, 'x' {TY_G}", "tye", "a"),
            # A collation created in the transaction is just as invisible to a worker
            # (#2250 review), with either provider; one from an earlier transaction is not.
            "new icu collation": _type_flush(d, ["CREATE COLLATION ty_ck (provider = icu, locale = 'und')",
                                                 "ALTER TABLE tyk ADD COLUMN s text COLLATE ty_ck"],
                                             f"INSERT INTO tyk SELECT g, g, 'v' || g {TY_G}", "tyk", "a"),
            "new libc collation": _type_flush(d, ["CREATE COLLATION ty_cl (provider = libc, locale = 'C')",
                                                  "ALTER TABLE tyl ADD COLUMN s text COLLATE ty_cl"],
                                              f"INSERT INTO tyl SELECT g, g, 'v' || g {TY_G}", "tyl", "a"),
            # The same inside a savepoint, released or still open: the row's xmin is the
            # subtransaction's, not the top-level transaction's (#2250 review).
            "savepoint type released": _type_flush(
                d, ["SAVEPOINT s", "CREATE TYPE ty_esr AS ENUM ('x', 'y')", "RELEASE SAVEPOINT s",
                    "ALTER TABLE tysr ADD COLUMN e ty_esr"], f"INSERT INTO tysr SELECT g, g, 'x' {TY_G}", "tysr", "a"),
            "savepoint type open": _type_flush(
                d, ["SAVEPOINT s", "CREATE TYPE ty_eso AS ENUM ('x', 'y')", "ALTER TABLE tyso ADD COLUMN e ty_eso"],
                f"INSERT INTO tyso SELECT g, g, 'x' {TY_G}", "tyso", "a"),
            "savepoint collation released": _type_flush(
                d, ["SAVEPOINT s", "CREATE COLLATION ty_ccr (provider = icu, locale = 'und')", "RELEASE SAVEPOINT s",
                    "ALTER TABLE tycr ADD COLUMN s text COLLATE ty_ccr"],
                f"INSERT INTO tycr SELECT g, g, 'v' || g {TY_G}", "tycr", "a"),
            "savepoint collation open": _type_flush(
                d, ["SAVEPOINT s", "CREATE COLLATION ty_ccp (provider = icu, locale = 'und')",
                    "ALTER TABLE tycp ADD COLUMN s text COLLATE ty_ccp"],
                f"INSERT INTO tycp SELECT g, g, 'v' || g {TY_G}", "tycp", "a"),
            "old collation": _type_flush(d, ["TRUNCATE tyo"], f"INSERT INTO tyo SELECT g, g, 'v' || g {TY_G}",
                                         "tyo", "a"),
            "unlocked": _type_flush(d, ["TRUNCATE tyc"],
                                    f"INSERT INTO tyc SELECT g, g, row(g, g)::ty_c {TY_G}", "tyc", "(r).x"),
            "share": _type_flush(d, ["TRUNCATE tyc", "LOCK TABLE ty_c IN SHARE MODE"],
                                 f"INSERT INTO tyc SELECT g, g, row(g, g)::ty_c {TY_G}", "tyc", "(r).x"),
        }
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {TY_DB} WITH (FORCE)")


def test_type_a_flush_after_truncating_the_composites_table_is_serial(ty, expect):
    expect.text(ty["truncate"], TY_SERIAL,
                "a parallel flush after TRUNCATE of a composite column's table in the same transaction "
                "finishes, serially (#2182)")


def test_type_an_array_of_a_locked_composite_is_serial(ty, expect):
    expect.text(ty["array"], TY_SERIAL,
                "a parallel flush of an array-of-composite column whose table the transaction locked "
                "finishes, serially (#2182)")


def test_type_a_domain_over_a_locked_composite_is_serial(ty, expect):
    expect.text(ty["domain"], TY_SERIAL,
                "a parallel flush of a domain-over-composite column whose table the transaction locked "
                "finishes, serially (#2182)")


def test_type_a_composite_with_a_locked_field_is_serial(ty, expect):
    expect.text(ty["field"], TY_SERIAL,
                "a parallel flush of a composite whose field's table the transaction locked finishes, "
                "serially (#2182)")


def test_type_a_range_over_a_locked_composite_is_serial(ty, expect):
    expect.text(ty["range"], TY_SERIAL,
                "a parallel flush of a range-over-composite column whose table the transaction locked "
                "finishes, serially (#2182)")


def test_type_a_multirange_over_a_locked_composite_is_serial(ty, expect):
    expect.text(ty["multirange"], TY_SERIAL,
                "a parallel flush of a multirange-over-composite column whose table the transaction locked "
                "finishes, serially (#2182)")


def test_type_a_type_created_in_the_transaction_is_serial(ty, expect):
    # The reading names a worker that failed on it, so a flush that hands its columns
    # back still reads red.
    expect.text(ty["new type"], TY_SERIAL,
                "a parallel flush of a column whose type the same transaction created runs serially, with "
                "no worker failure (#2182)")


def test_type_an_icu_collation_created_in_the_transaction_is_serial(ty, expect):
    expect.text(ty["new icu collation"], TY_SERIAL,
                "a parallel flush of a column whose ICU collation the same transaction created runs serially, "
                "with no worker failure (#2182)")


def test_type_a_libc_collation_created_in_the_transaction_is_serial(ty, expect):
    expect.text(ty["new libc collation"], TY_SERIAL,
                "a parallel flush of a column whose libc collation the same transaction created runs serially, "
                "with no worker failure (#2182)")


def test_type_a_type_from_a_released_savepoint_is_serial(ty, expect):
    expect.text(ty["savepoint type released"], TY_SERIAL,
                "a parallel flush of a column whose type a released savepoint of the same transaction created "
                "runs serially, with no worker failure (#2182)")


def test_type_a_type_from_an_open_savepoint_is_serial(ty, expect):
    expect.text(ty["savepoint type open"], TY_SERIAL,
                "a parallel flush of a column whose type a still-open savepoint created runs serially, with no "
                "worker failure (#2182)")


def test_type_a_collation_from_a_released_savepoint_is_serial(ty, expect):
    expect.text(ty["savepoint collation released"], TY_SERIAL,
                "a parallel flush of a column whose collation a released savepoint of the same transaction "
                "created runs serially, with no worker failure (#2182)")


def test_type_a_collation_from_an_open_savepoint_is_serial(ty, expect):
    expect.text(ty["savepoint collation open"], TY_SERIAL,
                "a parallel flush of a column whose collation a still-open savepoint created runs serially, with "
                "no worker failure (#2182)")


def test_type_an_earlier_collation_flushes_in_workers(ty, expect):
    # A control: only a collation this transaction created keeps the flush serial.
    expect.text(ty["old collation"], TY_WORKERS,
                "a parallel flush of a column whose non-default collation an earlier transaction created flushes "
                "every column in workers (#2182)")


def test_type_an_unlocked_composite_flushes_in_workers(ty, expect):
    # A control. The TRUNCATE of the columnar table holds ACCESS EXCLUSIVE on it, which
    # #2072 already handles, so a fix that checked that table instead reads red here.
    expect.text(ty["unlocked"], TY_WORKERS,
                "a parallel flush of a composite column whose table the transaction does not lock flushes "
                "every column in workers (#2182)")


def test_type_a_composite_held_in_share_mode_flushes_in_workers(ty, expect):
    # A control: ACCESS SHARE does not conflict with SHARE.
    expect.text(ty["share"], TY_WORKERS,
                "a parallel flush of a composite column whose table the transaction holds in SHARE mode "
                "flushes every column in workers (#2182)")


# ---- the backend waits for its flush workers in the lock manager (#2256) ----------
#
# A worker reads the type cache in its own transaction, so for a composite column it
# takes ACCESS SHARE on the composite's table. The backend waited for its workers on a
# latch, which the deadlock detector does not follow. So when another session B held
# ACCESS EXCLUSIVE on that table and then asked for SHARE on the columnar table, the
# worker waited for B, B waited for the backend, and the backend waited for the worker
# unseen: the INSERT waited until statement_timeout. Each worker now holds an advisory
# lock of class 110 from the moment it connects, and the backend waits on those, so
# the detector sees the cycle and ends it with 40P01.
#
# jdatcmd's order: A loads cy_c's type cache entry first, B takes ACCESS EXCLUSIVE on
# cy_c, A runs a parallel INSERT with one worker, and B, once that worker waits on
# cy_c, asks for SHARE on the columnar table. The worker runs at the server's 1 s
# deadlock_timeout; A and B pick theirs, which decides the victim. A gets a 10 s
# statement_timeout and B 20 s; on main every case is A's 57014 at 10 s.
CY_DB = "pgc_parallel_flush_cycle"
CY_RUN_LOCK = re.compile(r"waits for ShareLock on advisory lock \[[0-9]*,[0-9]*,[0-9]*,110\]")
CY_WORKER_VICTIM = ("A: committed after WARNING 40P01 pgcolumnar parallel_flush worker 0 failed, completing its "
                    "columns serially: deadlock detected | B: got both | 20000 rows | ")
CY_CALLER_VICTIM = "A: 40P01 deadlock detected | B: got both | 0 rows | "
CY_OTHER_VICTIM = "A: committed, every column in workers | B: 40P01 deadlock detected | 20000 rows | "


def _cy_until(cond):
    """A DO block that polls cond every 20 ms, for up to 8 s, as the shell's cy_until.
    pg_stat_activity is read once per transaction unless the snapshot is cleared."""
    return (f"DO $$ BEGIN FOR i IN 1..400 LOOP PERFORM pg_stat_clear_snapshot(); EXIT WHEN {cond}; "
            "PERFORM pg_sleep(0.02); END LOOP; END $$")


def _cy_crashes(cluster):
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return sum(1 for line in text.splitlines() if "terminated by signal" in line)


def _cy_note(n):
    return (n.severity_nonlocalized or n.severity or "", n.sqlstate or "", n.message_primary or "",
            n.message_detail or "")


def _cycle(db, table, a_dt, b_dt, mpw=2, select="g, row(g, g)::cy_c"):
    """-> ("A: .. | B: .. | <rows> rows | <run lock named?> | <time>", A's details), as the
    shell's cycle. max_parallel_workers 2 gives the flush one worker."""
    out = {}
    notes = []
    details = []
    t0 = time.monotonic()

    def a():
        try:
            with psycopg.connect(db.dsn, autocommit=True, application_name="pf_cy_a") as c:
                c.add_notice_handler(lambda n: notes.append(_cy_note(n)))
                c.execute(f"SET deadlock_timeout = '{a_dt}'")
                c.execute(f"SET max_parallel_workers = {mpw}")
                c.execute("SELECT (row(1, 1)::cy_c).x")
                c.execute(_cy_until("EXISTS (SELECT 1 FROM pg_locks WHERE relation = 'cy_c'::regclass "
                                    "AND mode = 'AccessExclusiveLock' AND granted)"))
                c.execute("SET pgcolumnar.parallel_flush = on")
                c.execute("SET client_min_messages = debug1")
                c.execute("SET statement_timeout = '10s'")
                c.execute(f"INSERT INTO {table} SELECT {select} FROM generate_series(1, 20000) g")
        except psycopg.Error as exc:
            out["a"] = f"{exc.sqlstate} {exc.diag.message_primary}"
            out["a detail"] = exc.diag.message_detail or ""
            details.append(exc.diag.message_detail or "")

    t = threading.Thread(target=a)
    t.start()
    try:
        with psycopg.connect(db.dsn, autocommit=True, application_name="pf_cy_b") as c:
            c.execute(f"SET deadlock_timeout = '{b_dt}'")
            c.execute("SET statement_timeout = '20s'")
            c.execute(_cy_until("EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = 'pf_cy_a' "
                                "AND query LIKE 'DO%')"))
            with c.transaction():
                c.execute("LOCK TABLE cy_c IN ACCESS EXCLUSIVE MODE")
                c.execute(_cy_until("EXISTS (SELECT 1 FROM pg_locks WHERE relation = 'cy_c'::regclass "
                                    "AND NOT granted)"))
                c.execute(f"LOCK TABLE {table} IN SHARE MODE")
                b = "got both"
    except psycopg.Error as exc:
        b = f"{exc.sqlstate} {exc.diag.message_primary}"
        details.append(exc.diag.message_detail or "")
    t.join(60)
    ms = int((time.monotonic() - t0) * 1000)
    warnings = [n for n in notes if n[0] == "WARNING"]
    details += [n[3] for n in notes]
    if t.is_alive():
        a_got = "the insert never returned"
    elif "a" in out:
        a_got = out["a"]
    elif warnings:
        a_got = f"committed after WARNING {warnings[0][1]} {warnings[0][2]}"
    else:
        a_got = f"committed, {_all_split([n[2] for n in notes])}"
    named = ("the deadlock names the run lock" if any(CY_RUN_LOCK.search(d) for d in details)
             else "no run lock named")
    return (f"A: {a_got} | B: {b} | {db.q(f'SELECT count(*) FROM {table}')} rows | {named} | "
            + ("under 6 s" if ms < 6000 else f"took {ms} ms")), [out.get("a detail", "")] + [n[3] for n in notes]


@pytest.fixture(scope="module")
def cy(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {CY_DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {CY_DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=CY_DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        d.run("CREATE TABLE cy_c (x int, y int)")
        for t in ("cy_w", "cy_wa", "cy_wb", "cy_wd"):
            d.run(f"CREATE TABLE {t} (a int, r cy_c) USING pgcolumnar")
        d.run("CREATE TABLE cy_lt (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar")
        before = _cy_crashes(pgc_cluster)
        r = {
            "worker": _cycle(d, "cy_w", "30s", "30s")[0],
            "caller": _cycle(d, "cy_wa", "500ms", "30s")[0],
            "other": _cycle(d, "cy_wb", "30s", "100ms")[0],
            "default": _cycle(d, "cy_wd", "1s", "1s")[0],
        }
        # Two workers, each encoding a composite column, so both wait on cy_c. The
        # backend waits for worker 0 first; at the server's 1 s worker 0 is the victim.
        # The backend then waits for worker 1, which closes the cycle again: worker 1,
        # if its own check comes after, or the backend at its 2 s, ends it. Either way
        # a report A received names worker 1's run lock.
        d.run("CREATE TABLE cy_w2 (r cy_c, s cy_c) USING pgcolumnar")
        r["two"] = _cycle(d, "cy_w2", "2s", "30s", 4, "row(g, g)::cy_c, row(g, g)::cy_c")

        # A 1 ms lock_timeout does not end an uncontended parallel flush, and with
        # deadlock_timeout 1 ms and log_lock_waits on, no wait for a worker is
        # reported. client_min_messages = debug1 sends the LOG lines to the session.
        notes = []
        try:
            with psycopg.connect(d.dsn, autocommit=True,
                                 options="-c lock_timeout=1ms -c deadlock_timeout=1ms -c log_lock_waits=on") as c:
                c.add_notice_handler(lambda n: notes.append(n.message_primary or ""))
                c.execute("SET pgcolumnar.parallel_flush = on")
                c.execute("SET client_min_messages = debug1")
                c.execute("SET statement_timeout = '60s'")
                c.execute(f"INSERT INTO cy_lt {WNUM}")
            got = f"{d.q('SELECT count(*) FROM cy_lt')} rows"
        except psycopg.Error as exc:
            got = f"{exc.sqlstate} {exc.diag.message_primary}"
        logged = sum(1 for n in notes if "still waiting for ShareLock on advisory lock" in n)
        r["lock_timeout"] = f"{got}, {_all_split(notes)}, {logged} waits for a worker logged"

        # and the session's settings are its own again once the flush returns
        notes = []
        try:
            with psycopg.connect(d.dsn, autocommit=True) as c:
                c.add_notice_handler(lambda n: notes.append(n.message_primary or ""))
                with c.transaction():
                    c.execute("SET LOCAL lock_timeout = '2s'")
                    c.execute("SET LOCAL log_lock_waits = on")
                    c.execute("SET LOCAL pgcolumnar.parallel_flush = on")
                    c.execute("SET LOCAL client_min_messages = debug1")
                    c.execute(f"INSERT INTO cy_lt (a, b, c, d, e, f, g, h) {LK_ROWS}")
                    shown = [c.execute("SHOW lock_timeout").fetchone()[0],
                             c.execute("SHOW log_lock_waits").fetchone()[0]]
            r["gucs"] = f"{_all_split(notes)}, {'|'.join(shown)}"
        except psycopg.Error as exc:
            r["gucs"] = f"{exc.sqlstate} {exc.diag.message_primary}"
        now = _cy_crashes(pgc_cluster)
        r["crashes"] = (f"{now - before} terminated by signal" if isinstance(before, int) and isinstance(now, int)
                        else f"unreadable:[{before}][{now}]")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {CY_DB} WITH (FORCE)")


def test_cycle_a_worker_victim_reaches_the_backend_as_a_warning(cy, expect):
    expect.text(cy["worker"], CY_WORKER_VICTIM + "the deadlock names the run lock | under 6 s",
                "a parallel flush worker chosen as the deadlock victim reaches the backend as a 40P01 WARNING "
                "naming the run lock, and the INSERT commits its rows serially within 6 s (#2256)")


def test_cycle_the_backend_as_victim_fails_with_40p01(cy, expect):
    expect.text(cy["caller"], CY_CALLER_VICTIM + "the deadlock names the run lock | under 6 s",
                "a backend waiting for its parallel flush worker, chosen as the deadlock victim, fails with 40P01 "
                "naming the run lock within 6 s (#2256)")


def test_cycle_the_other_session_as_victim_lets_the_insert_finish(cy, expect):
    expect.text(cy["other"], CY_OTHER_VICTIM + "the deadlock names the run lock | under 6 s",
                "the other session chosen as the deadlock victim fails with 40P01 naming the run lock, and the "
                "parallel INSERT finishes in its worker within 6 s (#2256)")


def test_cycle_the_issues_case_ends_with_one_side_giving_way(cy, expect):
    # Every deadlock_timeout at 1 s: any of the three outcomes, well before the timeout.
    got = cy["default"]
    gave = (got.rsplit(" | ", 1)[-1]
            if any(got.startswith(w) for w in (CY_WORKER_VICTIM, CY_CALLER_VICTIM, CY_OTHER_VICTIM))
            else f"neither gave way:[{got}]")
    expect.text(gave, "under 6 s",
                "with every deadlock_timeout at 1 s, the ordered two-session cycle through a parallel flush worker "
                "ends with one side giving way within 6 s, not at statement_timeout (#2256)")


def test_cycle_the_backend_waits_for_the_second_worker_too(cy, expect):
    got, details = cy["two"]
    ok = (got == CY_CALLER_VICTIM + "the deadlock names the run lock | under 6 s"
          or re.fullmatch(r"A: committed after WARNING 40P01 pgcolumnar parallel_flush worker [01] failed, "
                          r"completing its columns serially: deadlock detected \| B: got both \| 20000 rows \| "
                          r"the deadlock names the run lock \| under 6 s", got))
    if ok:
        named = any(re.search(r"waits for ShareLock on advisory lock \[[0-9]*,[0-9]*,1,110\]", d) for d in details)
        reading = ("worker 1's run lock named" if named else "worker 1's run lock not named") + ", under 6 s"
    else:
        reading = f"neither gave way:[{got}]"
    expect.text(reading, "worker 1's run lock named, under 6 s",
                "with two parallel flush workers waiting on the cycle, the backend waits for the second worker's "
                "run lock too, and one side gives way within 6 s (#2256)")


def test_cycle_the_worker_waits_ignore_lock_timeout(cy, expect):
    expect.text(cy["lock_timeout"], "300000 rows, every column in workers, 0 waits for a worker logged",
                "a parallel flush under lock_timeout 1 ms, deadlock_timeout 1 ms and log_lock_waits on still "
                "flushes every column in workers, and logs no wait for its workers (#2256)")


def test_cycle_the_sessions_settings_read_back(cy, expect):
    expect.text(cy["gucs"], "every column in workers, 2s|on",
                "lock_timeout and log_lock_waits read back unchanged after a parallel flush in the same "
                "transaction (#2256)")


def test_cycle_no_backend_died(cy, expect):
    expect.text(cy["crashes"], "0 terminated by signal",
                "no backend died during the lock-cycle cases (#2256)")

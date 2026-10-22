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

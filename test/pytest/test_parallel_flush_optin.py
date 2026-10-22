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
import re

import psycopg
import pytest

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

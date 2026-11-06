"""The columnar reader reuses one group buffer across row groups instead of allocating a
fresh one per group, and the reuse never shows in an answer. The pytest twin of
`test/reader_buffer_reuse.sh`, every check by the shell's name (#1341).

Asserted: reading every group of a 600,000-row table costs well under one group's worth
of minor page faults per extra group (log_executor_stats, as the shell reads it); the
full scan, a projected scan and a short group after full ones match a heap mirror, as
does the whole-group read (column projection off, and a whole-row read) before and after
the short group; and
on a five-column table, eight projected subsets, a column added afterwards, an all-NULL
column, and reads after DROP COLUMN all match their heap twin, as does a rescan.

THE HEAP MIRRORS ARE BUILT FROM THE GENERATORS, not copied from the tables under test:
a copy agrees with whatever the reader returned, and with a row corrupted before the
copy the shell's old form passed. A differential comparison refuses an empty side.

ON A PRIVATE DATABASE, with max_parallel_workers_per_gather = 0 as the shell's cluster
runs, built in the shell's order by a module fixture.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_reader_buffer_reuse"
STRIPE = 150000
ROWS = 600000
OFF = ["SET pgcolumnar.enable_ungrouped_vector_agg = off", "SET pgcolumnar.enable_vectorization = off"]
VAL = "md5(g::text)||md5((g*7)::text)||md5((g*13)::text)||md5((g*17)::text)"
DIGEST = "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM {t}"
SUMS = "SELECT count(*)||'/'||sum(id) FROM {t}"
WHOLEROW = "SELECT count(*)||'/'||md5(string_agg(r::text, ',' ORDER BY (r).id)) FROM {t} r"
NOPROJ = "-c pgcolumnar.enable_column_projection=off"
ONE = f"SELECT count(v) FROM rb WHERE id <= {STRIPE}"
ALL = "SELECT count(v) FROM rb"
PROJECTIONS = [
    ("projected subset [i] matches the heap twin", "i"),
    ("projected subset [s] matches the heap twin", "s"),
    ("projected subset [b] matches the heap twin", "b"),
    ("projected subset [t] matches the heap twin", "t"),
    ("projected subset [u] matches the heap twin", "u"),
    ("projected subset [i||':'||t] matches the heap twin", "i||':'||t"),
    ("projected subset [s||':'||u] matches the heap twin", "s||':'||u"),
    ("projected subset [i||b::text||t||u] matches the heap twin", "i||b::text||t||u"),
]


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0]}")

    def q(self, sql, options=None):
        """-> the first column of the first row as text; a connection or query error as a value.
        options: libpq startup options, as the shell passes PGOPTIONS."""
        try:
            with psycopg.connect(self.dsn, autocommit=True, **({"options": options} if options else {})) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        if row is None or row[0] is None:
            return ""
        return ("t" if row[0] else "f") if isinstance(row[0], bool) else str(row[0])

    def n(self, sql):
        v = self.q(sql)
        return int(v) if v.isdigit() else v

    def groups_read(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                for s in OFF:
                    conn.execute(s)
                plan = "\n".join(r[0] for r in conn.execute(
                    f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}"))
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0])
        m = re.search(r"Columnar Chunk Groups Read: (\d+)", plan)
        return int(m.group(1)) if m else query_error("no 'Chunk Groups Read' line")

    def faults(self, sql):
        """-> the minor page faults of the third of three runs, from log_executor_stats."""
        notes = []
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.add_notice_handler(lambda d: notes.append(d.message_detail or ""))
                conn.execute("SET client_min_messages = log")
                conn.execute("SET log_executor_stats = on")
                for s in OFF:
                    conn.execute(s)
                for _ in range(3):
                    conn.execute(sql).fetchall()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0])
        found = re.findall(r"0/(\d+) \[0/\d+\] page faults", "\n".join(notes))
        return int(found[-1]) if found else query_error("no page-fault line in the executor stats")

    def dq_pair(self, proj):
        a = self.q(f"SELECT md5(string_agg(x, ',' ORDER BY x)) FROM (SELECT ({proj})::text x FROM rbd) z")
        b = self.q(f"SELECT md5(string_agg(x, ',' ORDER BY x)) FROM (SELECT ({proj})::text x FROM rbd_h) z")
        return "match" if a and not a.startswith("QUERY_ERROR") and a == b else f"differ({a} vs {b})"


@pytest.fixture(scope="module")
def rb(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        # serial, as the shell's cluster runs: a Gather splits the groups between
        # workers, and the leader's count then read 2 of 4
        a.execute(f"ALTER DATABASE {DB} SET max_parallel_workers_per_gather = 0")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        d.run("CREATE TABLE rb (id int, v text) USING pgcolumnar")
        d.run(f"SELECT pgcolumnar.set_options('rb', stripe_row_limit => {STRIPE}, "
              "compression => 'none', encode_effort => 'fast')")
        d.run(f"INSERT INTO rb SELECT g, {VAL} FROM generate_series(1, {ROWS}) g")
        r["ngroups"] = d.n("SELECT count(*) FROM pgcolumnar.stats('rb')")
        r["g one"], r["g all"] = d.groups_read(ONE), d.groups_read(ALL)
        r["f one"], r["f all"] = d.faults(ONE), d.faults(ALL)

        # the mirror comes from the generator, not from rb
        d.run("CREATE TABLE rb_h (id int, v text)")
        d.run(f"INSERT INTO rb_h SELECT g, {VAL} FROM generate_series(1, {ROWS}) g")
        r["full"] = (d.q(DIGEST.format(t="rb")), d.q(DIGEST.format(t="rb_h")))
        r["projected"] = (d.q(SUMS.format(t="rb")), d.q(SUMS.format(t="rb_h")))
        # the whole-group read: no projection, so the group is read into the reused
        # buffer in one call
        r["noproj"] = (d.q(DIGEST.format(t="rb"), options=NOPROJ), d.q(DIGEST.format(t="rb_h")))
        r["wholerow"] = (d.q(WHOLEROW.format(t="rb")), d.q(WHOLEROW.format(t="rb_h")))
        for t in ("rb", "rb_h"):
            d.run(f"INSERT INTO {t} SELECT g, {VAL} FROM generate_series({ROWS + 1}, {ROWS + 2000}) g")
        r["short premise"] = d.q("SELECT (SELECT min(rowcount) FROM pgcolumnar.stats('rb')) < "
                                 "(SELECT max(rowcount) FROM pgcolumnar.stats('rb'))")
        r["short"] = (d.q(DIGEST.format(t="rb")), d.q(DIGEST.format(t="rb_h")))
        r["short noproj"] = (d.q(DIGEST.format(t="rb"), options=NOPROJ), d.q(DIGEST.format(t="rb_h")))
        r["short wholerow"] = (d.q(WHOLEROW.format(t="rb")), d.q(WHOLEROW.format(t="rb_h")))

        d.run("CREATE TABLE rbd (i int, s smallint, b bigint, t text, u text) USING pgcolumnar")
        d.run("SELECT pgcolumnar.set_options('rbd', stripe_row_limit => 20000, encode_effort => 'fast')")
        d.run("INSERT INTO rbd SELECT g, (g%100)::smallint, g::bigint*1000, md5(g::text), "
              "repeat(md5((g*7)::text), 3) FROM generate_series(1, 120000) g")
        d.run("CREATE TABLE rbd_h AS SELECT g AS i, (g%100)::smallint AS s, g::bigint*1000 AS b, "
              "md5(g::text) AS t, repeat(md5((g*7)::text), 3) AS u FROM generate_series(1, 120000) g")
        r["rbd groups"] = d.n("SELECT count(*) FROM pgcolumnar.stats('rbd')")
        for name, proj in PROJECTIONS:
            r[name] = d.dq_pair(proj)
        for t in ("rbd", "rbd_h"):
            d.run(f"ALTER TABLE {t} ADD COLUMN added int")
        r["added"] = d.dq_pair("coalesce(added::text,'N')||'|'||t")
        for t in ("rbd", "rbd_h"):
            d.run(f"ALTER TABLE {t} ADD COLUMN allnull text")
            d.run(f"UPDATE {t} SET allnull = NULL WHERE i <= 10")
        r["allnull"] = d.dq_pair("coalesce(allnull,'N')||'|'||u")
        for t in ("rbd", "rbd_h"):
            d.run(f"ALTER TABLE {t} DROP COLUMN b")
        r["dropped"] = d.dq_pair("i||':'||t||':'||u")
        r["rescan"] = d.q("SELECT count(*) FROM (SELECT 1 FROM generate_series(1,3) s, rb WHERE rb.id <= 100) x")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _at_least(n, k, what):
    return ("yes" if n >= k else f"no ({n} {what})") if isinstance(n, int) else f"no ({n})"


def test_the_reuse_fixture_has_several_row_groups(rb, expect):
    expect.text(_at_least(rb["ngroups"], 3, "groups"), "yes",
                "premise: the fixture has several row groups, or reuse cannot matter")


def test_the_one_group_arm_reads_one_group(rb, expect):
    expect.num(rb["g one"], 1, "premise: the one-group arm really reads exactly one row group")


def test_the_all_groups_arm_reads_every_group(rb, expect):
    expect.num(rb["g all"], rb["ngroups"], "premise: the all-groups arm really reads every row group")


def test_the_instrument_reports_faults(rb, expect):
    f1, fa = rb["f one"], rb["f all"]
    ok = isinstance(f1, int) and isinstance(fa, int) and f1 > 0 and fa > 0
    expect.text("yes" if ok else f"no ({f1}, {fa})", "yes",
                "premise: the instrument reports a nonzero fault count at all")


def test_reading_every_group_reuses_the_buffer(rb, expect):
    f1, fa, n = rb["f one"], rb["f all"], rb["ngroups"]
    if not all(isinstance(v, int) for v in (f1, fa, n)) or f1 <= 0:
        got = f"no reading ({f1}, {fa}, {n})"
    else:
        ratio = fa / f1
        got = "reused" if ratio < 1 + (n - 1) * 0.5 else f"per-group (ratio {ratio:.2f} over {n} groups)"
    expect.text(got, "reused",
                "reading every group does not cost per-group faults (ratio well under the group count)")


def test_the_full_scan_matches_the_mirror(rb, expect):
    got, want = rb["full"]
    expect.text(got, want, "the full scan matches a heap mirror exactly")


def test_a_projected_scan_matches_too(rb, expect):
    got, want = rb["projected"]
    expect.text(got, want, "a PROJECTED scan (one column of two) matches too")


def test_the_full_scan_with_projection_off_matches_the_mirror(rb, expect):
    got, want = rb["noproj"]
    expect.text(got, want, "the full scan with column projection off (a whole-group read) matches a heap mirror")


def test_a_whole_row_read_matches_the_mirror(rb, expect):
    got, want = rb["wholerow"]
    expect.text(got, want, "a whole-row read (a whole-group read) matches a heap mirror")


def test_a_smaller_group_was_added(rb, expect):
    expect.text(rb["short premise"], "t", "premise: that really added a group smaller than the ones before it")


def test_a_short_group_after_a_full_one_reads_correctly(rb, expect):
    got, want = rb["short"]
    expect.text(got, want, "a short group after a full one still reads correctly")


def test_a_short_group_reads_correctly_with_projection_off(rb, expect):
    got, want = rb["short noproj"]
    expect.text(got, want, "a short group after a full one reads correctly with column projection off")


def test_a_short_group_reads_correctly_in_a_whole_row_read(rb, expect):
    got, want = rb["short wholerow"]
    expect.text(got, want, "a short group after a full one reads correctly in a whole-row read")


def test_the_differential_fixture_spans_several_groups(rb, expect):
    expect.text(_at_least(rb["rbd groups"], 3, "groups"), "yes",
                "premise: the differential fixture spans several row groups")


@pytest.mark.parametrize("name,proj", PROJECTIONS)
def test_a_projected_subset_matches_the_heap_twin(rb, name, proj, expect):
    expect.text(rb[name], "match", name)


def test_an_added_column_reads_back_null(rb, expect):
    expect.text(rb["added"], "match", "a column ADDED after the groups exist reads back NULL, as on heap")


def test_an_all_null_column_beside_a_wide_one_matches(rb, expect):
    expect.text(rb["allnull"], "match", "an all-NULL column beside a wide one matches")


def test_reads_after_drop_column_match(rb, expect):
    expect.text(rb["dropped"], "match", "reads after DROP COLUMN match")


def test_a_rescan_returns_the_same_rows(rb, expect):
    expect.text(rb["rescan"], "300", "a rescan (nested loop) returns the same rows")

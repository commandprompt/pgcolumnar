"""The fetch-by-row-number cache (#143, #353, #359, #1341). The pytest twin of
`test/native_fetch_cache.sh`, every check by the shell's name.

A fetch by row number decoded a whole row group per row; a statement-scoped cache of
the decoded group removes the repeat. The cache is timed as a ratio: the same index
fetches from one big row group and from ten small ones, each reading on a freshly
built fixture because an UPDATE moves the rows it reads into a new group (#801). The
UPDATE fetches its rows by number whatever its scan; every timed point query asserts
first that its plan is an index scan, under the SETs that make one (#797). Then the
scoping guards
in the source, a wide group's decode scratch (#353), and crossing the cache cap
costing per column rather than per entry (#359).
"""
import os
import pathlib
import re
import time

import psycopg
import pytest

ROWS = int(os.environ.get("PGC_FETCH_ROWS", "20000"))
UPD = ROWS // 10
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
FETCH_PATH = ("max_parallel_workers_per_gather=0", "enable_seqscan=off", "enable_bitmapscan=off",
              "pgcolumnar.enable_index_fetch_penalty=off")


def _one(c, sql):
    return c.execute(sql).fetchone()[0]


class _Sets:
    def __init__(self, c, sets):
        self.c, self.sets = c, sets

    def __enter__(self):
        for s in self.sets:
            self.c.execute(f"SET {s}")

    def __exit__(self, *exc):
        for s in self.sets:
            self.c.execute(f"RESET {s.split('=')[0]}")


def _row_fetch(c, sql):
    """-> 'yes' when `sql` plans an Index Scan under the fetch-path SETs, as pgc_uses_row_fetch."""
    with _Sets(c, FETCH_PATH):
        plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {sql}"))
    return "yes" if "Index Scan using" in plan else "no"


def _ms(c, sql, sets=FETCH_PATH):
    with _Sets(c, sets):
        t0 = time.monotonic()
        c.execute(sql)
        return int((time.monotonic() - t0) * 1000)


def _ratio(a, b, bound):
    """-> 'within' when a/b, rounded to two places, is at most `bound`; else why not,
    as check_ratio words it: a zero side measured nothing."""
    if a == 0 or b == 0:
        return f"a side of the ratio is zero, so nothing was measured: a=[{a}] b=[{b}]"
    r = round(a / b, 2)
    return "within" if r <= bound else f"{r:.2f}x exceeds the {bound}x bound (a={a} b={b})"


def _groups(c, table):
    return _one(c, f"SELECT count(*) FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s USING (storage_id) "
                   f"WHERE s.relation_oid = '{table}'::regclass")


def _build(c, table, per_group):
    c.execute(f"DROP TABLE IF EXISTS {table}")
    c.execute(f"SET pgcolumnar.stripe_row_limit = {per_group}")
    c.execute(f"SET pgcolumnar.chunk_group_row_limit = {per_group}")
    try:
        c.execute(f"CREATE TABLE {table} (id int, v int, t text) USING pgcolumnar")
        c.execute(f"INSERT INTO {table} SELECT g, g, 'row' || g FROM generate_series(1, {ROWS}) g")
        c.execute(f"CREATE INDEX {table}_id ON {table} (id)")
    finally:
        c.execute("RESET pgcolumnar.stripe_row_limit")
        c.execute("RESET pgcolumnar.chunk_group_row_limit")


def _fn(path, name):
    m = re.search(rf"^{name}\(.*?^}}", (SRC / path).read_text(), re.S | re.M)
    return m.group(0) if m else ""


@pytest.fixture(scope="module")
def fc(pgc_cluster):
    db = "pgc_fetchcache"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_the_cache_makes_one_big_group_cost_what_ten_small_ones_do(fc, expect):
    c = fc
    # the shell's SETs for this arm; the UPDATE fetches its target rows by number anyway
    upd_sets = ("max_parallel_workers_per_gather=0", "enable_seqscan=off", "enable_bitmapscan=off")
    upd = f"UPDATE {{}} SET v = id + 1 WHERE id <= {UPD}"
    # rebuilt before every reading: the UPDATE moves the rows it read into a new group (#801)
    best, geom = {}, {}
    for table, per in (("fc_one", ROWS), ("fc_many", ROWS // 10)):
        times, seen = [], []
        for _ in range(3):
            _build(c, table, per)
            seen.append(str(_groups(c, table)))
            times.append(_ms(c, upd.format(table), upd_sets))
        best[table], geom[table] = min(times), " ".join(seen)
    print(f"-- {UPD} fetches: one group of {ROWS} took {best['fc_one']} ms; ten groups took {best['fc_many']} ms")
    expect.text(geom["fc_one"], "1 1 1", "every fc_one reading measured one row group (#801)")
    expect.text(geom["fc_many"], "10 10 10", "every fc_many reading measured ten row groups (#801)")
    expect.text(_ratio(best["fc_one"], best["fc_many"], 3), "within",
                "fetching from one big group is not far dearer than from ten small ones")
    expect.num(_one(c, "SELECT count(*) FROM fc_one WHERE v = id + 1"), UPD, "the updated rows are correct")
    expect.num(_one(c, "SELECT count(*) FROM fc_one WHERE v = id"), ROWS - UPD, "the untouched rows are unchanged")


def test_the_scoping_guards_are_in_the_source(expect):
    reader = (SRC / "columnar_reader.c").read_text()
    slot, row = _fn("columnar_reader.c", "pgcolumnar_fetch_group_slot"), _fn("columnar_reader.c", "pgcolumnar_fetch_row")
    expect.text("yes" if slot and row else "no", "yes", "premise: the fetch-cache functions were extracted, not empty ranges")
    lines = reader.splitlines()
    grp = sum("e->groupNumber == groupNumber" in l for l in lines)
    keyed = sum("e->storageId == storageId && e->groupNumber == groupNumber" in l for l in lines)
    expect.at_least(grp, 1, "premise: there is a group-number comparison to key")
    expect.text(f"keyed {keyed} of {grp}", f"keyed {grp} of {grp}", "every group-number comparison is keyed by the storage id too")
    expect.text("yes" if "e->cid != cid" in slot else "no", "yes", "an entry from an earlier command is rejected")
    for f in ("firstRowNumber", "rowCount", "fileOffset"):
        expect.text("yes" if f"entry->{f} != rg->{f}" in row else "no", "yes",
                    f"a hit re-checks the group's {f} against the row group")
    expect.text("yes" if "entry->natts != natts" in row else "no", "yes",
                "a hit re-checks the group's natts against the scan's column count")
    for fn in ("pgcolumnar_executor_end", "pgcolumnar_xact_callback"):
        body = _fn("columnar_tableam.c", fn)
        expect.text("yes" if body else "no", "yes", f"premise: {fn} was extracted, not an empty range")
        expect.text("yes" if "PgColumnarDiscardFetchCache" in body else "no", "yes",
                    f"the fetch cache is discarded from {fn}")


def test_a_wide_groups_decode_scratch_does_not_blow_the_cap(fc, expect):
    c = fc
    for table, stripe in (("fc_wide", 150000), ("fc_wnarrow", 20000)):
        c.execute(f"DROP TABLE IF EXISTS {table}")
        c.execute(f"""CREATE TABLE {table} (id int, h text, c1 int,c2 int,c3 int,c4 int,c5 int,c6 int,
                         c7 int,c8 int,c9 int,c10 int, u float8,
                         t1 text,t2 text,t3 text,t4 text,t5 text,t6 text,t7 text,t8 text) USING pgcolumnar""")
        c.execute(f"SELECT pgcolumnar.set_options('{table}', stripe_row_limit => {stripe})")
        c.execute(f"""INSERT INTO {table} SELECT g, 'h' || (g % 500), g,g,g,g,g,g,g,g,g,g, (g % 100)::float8,
                         repeat('x',40),repeat('y',40),repeat('z',40),'a'||g,'b'||g,'c'||g,'d'||g,'e'||g
                         FROM generate_series(1, 300000) g""")
        c.execute(f"CREATE INDEX {table}_h ON {table} (h)")
    point = "SELECT count(*), max(u) FROM {} WHERE h='h1'"
    for table in ("fc_wide", "fc_wnarrow"):
        expect.text(_row_fetch(c, point.format(table)), "yes", f"the {table} point query really drives the fetch path (#797)")
    bigw = min(_ms(c, point.format("fc_wide")) for _ in range(3))
    smallw = min(_ms(c, point.format("fc_wnarrow")) for _ in range(3))
    print(f"-- #353 wide point query: default-stripe {bigw} ms, small-stripe {smallw} ms")
    expect.text(_ratio(bigw, smallw, 5), "within", "a wide group's fetch is not far dearer than a small group's (#353)")
    both = "SELECT count(*) || '|' || coalesce(max(u)::text,'z') FROM {} WHERE h='h1'"
    expect.text(_one(c, both.format("fc_wide")), _one(c, both.format("fc_wnarrow")),
                "the wide-group point query is correct (#353)")


def test_crossing_the_cap_costs_per_column(fc, expect):
    c = fc
    c.execute("DROP TABLE IF EXISTS fc_w359")
    c.execute("CREATE TABLE fc_w359 (id int, h text, t1 text,t2 text,t3 text,t4 text,t5 text) USING pgcolumnar")
    c.execute("SELECT pgcolumnar.set_options('fc_w359', stripe_row_limit => 50000)")
    c.execute("""INSERT INTO fc_w359 SELECT g, 'h' || (g % 1000),
                 repeat('a',150),repeat('b',150),repeat('c',150),repeat('d',150),repeat('e',150)
                 FROM generate_series(1,100000) g""")
    c.execute("CREATE INDEX fc_w359_h ON fc_w359 (h)")

    def query(n):
        return "SELECT count(*)" + "".join(f", max(t{i})" for i in range(1, n + 1)) + " FROM fc_w359 WHERE h='h7'"

    def warm_ms(n):
        _ms(c, query(n))            # warm: decode cost, not first-touch I/O
        return _ms(c, query(n))

    for n in (4, 5):
        expect.text(_row_fetch(c, query(n)), "yes", f"the {n}-column projection really drives the fetch path (#797)")
    under = min(warm_ms(4) for _ in range(3))     # ~30 MB decoded: fits
    over = min(warm_ms(5) for _ in range(3))      # ~38 MB decoded: does not
    print(f"-- #359 projection width: four columns {under} ms, five columns {over} ms")
    expect.text(_ratio(over, under, 12), "within", "crossing the fetch cache cap costs proportionally, not totally (#359)")
    # the overflowing columns are re-decoded, not skipped: fast and null would be wrong
    expect.text(_one(c, "SELECT max(t1) || '|' || max(t4) FROM fc_w359 WHERE h='h7'"),
                _one(c, "SELECT max(t1) || '|' || max(t4) FROM fc_w359 WHERE h='h7' AND t5 IS NOT NULL"),
                "the over-cap projection returns the same values as the under-cap one (#359)")
    expect.num(_one(c, """SELECT count(*) FROM fc_w359 WHERE h='h7'
                          AND t1 = repeat('a',150) AND t2 = repeat('b',150) AND t3 = repeat('c',150)
                          AND t4 = repeat('d',150) AND t5 = repeat('e',150)"""),
               _one(c, "SELECT count(*) FROM fc_w359 WHERE h='h7'"),
               "every over-cap column reads back its written value (#359)")

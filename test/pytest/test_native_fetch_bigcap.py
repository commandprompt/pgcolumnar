"""Fetching from a row group larger than the fetch-cache cap (#433, #1341). The pytest
twin of `test/native_fetch_bigcap.sh`, every check by the shell's name.

Above the 32 MB cap the hit rate used to be zero by construction: the entry was
populated, used once and dropped whole, so every fetched row re-read the whole group.
The measure is BUFFERS, a count rather than a time. The fixture's premise is its
on-disk size, read back from pgcolumnar.row_group. Every measured query is forced
onto the index path, with the custom scan off, and asserts its plan. A payload wider
than the cap re-decodes per fetch by design (#359); a narrow projection, and columns
that fit, must be cached anyway. Correctness last.
"""
import psycopg
import pytest

CAP = 32 * 1024 * 1024
FORCE_INDEX = ("max_parallel_workers_per_gather=0", "enable_seqscan=off", "enable_bitmapscan=off",
               "pgcolumnar.enable_index_fetch_penalty=off", "pgcolumnar.enable_custom_scan=off")


class _Sets:
    def __init__(self, c, sets):
        self.c, self.sets = c, sets

    def __enter__(self):
        for s in self.sets:
            self.c.execute(f"SET {s}")

    def __exit__(self, *exc):
        for s in self.sets:
            self.c.execute(f"RESET {s.split('=')[0]}")


def _one(c, sql):
    try:
        return c.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


def _index_scan(c, sql):
    """-> 1 when `sql` plans an Index Scan under FORCE_INDEX, as the shell counts it."""
    with _Sets(c, FORCE_INDEX):
        return int(any("Index Scan" in r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {sql}")))


def _bufs(c, sql):
    """-> the buffers EXPLAIN (ANALYZE, BUFFERS) reports for `sql`, after one warming run,
    summed as the shell's awk sums them: every shared/read/hit token's digits."""
    total = 0
    with _Sets(c, FORCE_INDEX):
        c.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT TEXT) {sql}").fetchall()
        for (line,) in c.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT TEXT) {sql}"):
            if "Buffers:" not in line:
                continue
            for tok in line.split():
                if tok.startswith(("shared", "read", "hit")):
                    digits = "".join(ch for ch in tok if ch.isdigit())
                    if digits:
                        total += int(digits)
    return total


def _ratio(a, b, bound):
    """-> 'within' when a/b, rounded to two places, is at most `bound`, as check_ratio."""
    if a == 0 or b == 0:
        return f"a side of the ratio is zero, so nothing was measured: a=[{a}] b=[{b}]"
    r = round(a / b, 2)
    return "within" if r <= bound else f"{r:.2f}x exceeds the {bound}x bound (a={a} b={b})"


@pytest.fixture(scope="module")
def bigcap(pgc_cluster):
    db = "pgc_bigcap"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute("CREATE TABLE bigcap (id int, tag int, m1 text, m2 text, m3 text, payload text) USING pgcolumnar")
            # per-row distinct hex from a subquery correlated on g, the codec off: this
            # fixture compressed 17x and 25x in earlier attempts, so its size is asserted
            c.execute("SELECT pgcolumnar.set_options('bigcap', stripe_row_limit => 10000, compression => 'none')")
            c.execute("""INSERT INTO bigcap
                SELECT g, g % 97, md5((g * 7)::text), md5((g * 11)::text), md5((g * 13)::text),
                       (SELECT string_agg(md5((g * 300 + s)::text), '') FROM generate_series(1, 256) s)
                  FROM generate_series(1, 20000) g""")
            c.execute("CREATE INDEX bigcap_id ON bigcap (id)")
            c.execute("ANALYZE bigcap")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_a_group_above_the_cap_is_read_once_for_what_fits(bigcap, expect):
    c = bigcap
    maxg = _one(c, "SELECT max(byte_length) FROM pgcolumnar.row_group "
                   "WHERE storage_id = pgcolumnar.get_storage_id('bigcap')")
    print(f"-- largest row group: {maxg} bytes, cap {CAP}")
    expect.at_least(maxg if isinstance(maxg, int) else 0, CAP + 1,
                    "premise: the fixture really has a row group above the cap")
    expect.num(_one(c, "SELECT count(*) FROM bigcap"), 20000, "premise: the table loaded every row")

    payload = "SELECT sum(length(payload)) FROM bigcap WHERE id BETWEEN 1 AND {}"
    expect.num(_index_scan(c, payload.format(1)), 1, "premise: one row is fetched through an index scan")
    expect.num(_index_scan(c, payload.format(10)), 1, "premise: ten rows are fetched through an index scan")
    b1, b10 = _bufs(c, payload.format(1)), _bufs(c, payload.format(10))
    print(f"-- buffers: 1 row = {b1}, 10 rows = {b10}")
    # both sides a measurement before any comparison (#418): a failed EXPLAIN reads as 0
    expect.at_least(b1, 101, "premise: fetching one row touched a measurable number of buffers")
    expect.at_least(b10, 101, "premise: fetching ten rows touched a measurable number of buffers")
    # the payload alone exceeds the cap, so it re-decodes per fetch: #359's design, not #433
    expect.at_least(b10, b1 + 1, "premise: the wide column is the one over the cap, so this arm is #359's")

    narrow = "SELECT sum(tag) FROM bigcap WHERE id BETWEEN 1 AND {}"
    expect.num(_index_scan(c, narrow.format(10)), 1, "premise: the narrow projection is fetched through an index scan")
    n1, n10 = _bufs(c, narrow.format(1)), _bufs(c, narrow.format(10))
    print(f"-- narrow projection buffers: 1 row = {n1}, 10 rows = {n10}")
    expect.at_least(n1, 1, "premise: the narrow projection touched a measurable number of buffers")
    expect.text(_ratio(n10, n1, 3), "within", "a narrow projection over a wide group is cached too (#433)")

    medium = "SELECT count(m1 || m2 || m3) FROM bigcap WHERE id BETWEEN 1 AND {}"
    expect.num(_index_scan(c, medium.format(10)), 1, "premise: the medium projection is fetched through an index scan")
    m1, m10 = _bufs(c, medium.format(1)), _bufs(c, medium.format(10))
    print(f"-- three medium columns from an oversized group: 1 row = {m1}, 10 rows = {m10}")
    expect.at_least(m1, 1, "premise: the medium projection touched a measurable number of buffers")
    expect.text(_ratio(m10, m1, 3), "within", "columns that fit are cached even when their group does not (#433)")


def test_caching_changes_no_row(bigcap, expect):
    c = bigcap
    row = "md5(string_agg(id || ':' || tag || ':' || m1 || m2 || m3 || ':' || md5(payload), ',' ORDER BY id))"
    expect.text(str(_one(c, f"SELECT {row} FROM bigcap WHERE id BETWEEN 1 AND 10")),
                str(_one(c, f"SELECT {row} FROM (SELECT * FROM bigcap ORDER BY id LIMIT 10) s")),
                "the fetched rows are the right rows")
    expect.num(_one(c, "SELECT count(*) FROM bigcap WHERE length(payload) <> 8192"), 0,
               "every payload survived the round trip at full length")

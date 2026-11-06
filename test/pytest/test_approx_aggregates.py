"""Approximate distinct counts and percentiles come from hll and tdigest (#1296).

The compatibility statement for the two extensions over columnar tables. Phase 0
on #1296 measured both reading through pgColumnar's own scan, and decided against
building sketches here for 1.0.

Independent of `test/approx_aggregates.sh`: its own generator (other multipliers,
the same sizes, because the check names state them), its own connection and its
own SQL. Neither file reads the other. Check names match so the pair is graded by
name.

THE HEAP MIRROR IS WRITTEN FROM THE GENERATOR, never copied from the columnar
table. A copy reads through the scan under test, so a scan that lost rows would lose
them on both sides; the shell suite measured exactly that before it was fixed.

The bounds against the exact answer are PREMISES: they state the extension's own
documented error (hll at log2m 11, three standard errors, 6.9%; tdigest at
compression 100, 0.1% at p99), and a change here that broke them would break the
equalities first. hll 2.19 does not build against PostgreSQL 19, so on 19 the hll
test is UNSUPPORTED_MAJOR, listed in expected_unrunnable.txt.
"""

ROWS = 200000
DEVICES = 50000
GEN = (f"SELECT g % 7, ((g::bigint * 31337) % {DEVICES})::int, "
       f"((g::bigint * 92821) % 100000) / 100.0 FROM generate_series(1, {ROWS}) g")


def _one(conn, sql):
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def _within(got, want, rel):
    if got is None or want is None:
        return f"got {got} want {want}"
    off = abs(got - want) / abs(want)
    return "ok" if off <= rel else f"got {got} want {want} (off {off:.4f}, bound {rel})"


def _fixture(conn):
    conn.execute("SET max_parallel_workers_per_gather = 0")
    conn.execute("CREATE TABLE ax (day int, dev int, v float8) USING pgcolumnar")
    conn.execute(f"INSERT INTO ax {GEN}")
    conn.execute("CREATE TABLE ax_heap (day int, dev int, v float8) USING heap")
    conn.execute(f"INSERT INTO ax_heap {GEN}")


def _available(conn, ext):
    return _one(conn, f"SELECT count(*) FROM pg_available_extensions WHERE name = '{ext}'") == 1


def _plan_reads_columnar(conn, sql):
    plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS OFF) {sql}").fetchall())
    return plan.count("Custom Scan (PgColumnarScan) on ax")


def test_tdigest_over_columnar(pgc_conn, expect):
    conn = pgc_conn
    if not _available(conn, "tdigest"):
        expect.cannot_run("MISSING_DEPENDENCY", "tdigest is not installed for this major",
                          name="tdigest is not installed for this major")
        return
    _fixture(conn)
    expect.num(_one(conn, "SELECT count(*) + count(DISTINCT dev) FROM ax"), ROWS + DEVICES,
               "the columnar table returns the 200,000 rows written, over 50,000 devices")
    conn.execute("CREATE EXTENSION IF NOT EXISTS tdigest")
    expect.num(_one(conn, "SELECT count(*) FROM pg_extension WHERE extname = 'tdigest'"), 1,
               "premise: tdigest is installed in this database")
    expect.num(_plan_reads_columnar(conn, "SELECT tdigest_percentile(v, 100, 0.99) FROM ax"), 1,
               "the tdigest aggregate reads through the columnar scan")
    col = _one(conn, "SELECT tdigest_percentile(v, 100, 0.99) FROM ax")
    heap = _one(conn, "SELECT tdigest_percentile(v, 100, 0.99) FROM ax_heap")
    exact = _one(conn, "SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY v) FROM ax_heap")
    expect.text(repr(col), repr(heap), "tdigest over a columnar table gives exactly what it gives over heap")
    expect.text(_within(col, exact, 0.001), "ok", "premise: and its p99 is within 0.1% of the exact percentile")
    # Built once into heap and stored from there, as the shell does: a tdigest depends on
    # the order its inputs arrive in, so only a copy isolates the round trip (#1339).
    conn.execute("CREATE TABLE ax_td_src USING heap AS SELECT day, tdigest(v, 100) AS t FROM ax GROUP BY day")
    conn.execute("CREATE TABLE ax_td (day int, t tdigest) USING pgcolumnar")
    conn.execute("INSERT INTO ax_td SELECT day, t FROM ax_td_src")
    expect.text(_one(conn, "SELECT ((SELECT string_agg(t::text, ',' ORDER BY day) FROM ax_td) "
                           "= (SELECT string_agg(t::text, ',' ORDER BY day) FROM ax_td_src))::text"), "true",
                "a tdigest stored in a columnar table comes back byte for byte")
    expect.text(_within(_one(conn, "SELECT tdigest_percentile(t, 0.99) FROM ax_td"), exact, 0.001), "ok",
                "premise: a tdigest stored in a columnar table still answers: "
                "the union of daily digests is within 0.1% at p99")


def test_hll_over_columnar(pgc_conn, expect):
    conn = pgc_conn
    if not _available(conn, "hll"):
        if int(_one(conn, "SHOW server_version_num")) // 10000 == 19:
            expect.cannot_run("UNSUPPORTED_MAJOR", "hll 2.19 does not build against PostgreSQL 19",
                              name="hll 2.19 does not build against PostgreSQL 19")
        else:
            expect.cannot_run("MISSING_DEPENDENCY", "hll is not installed for this major",
                              name="hll is not installed for this major")
        return
    _fixture(conn)
    conn.execute("CREATE EXTENSION IF NOT EXISTS hll")
    expect.num(_one(conn, "SELECT count(*) FROM pg_extension WHERE extname = 'hll'"), 1,
               "premise: hll is installed in this database")
    agg = "hll_add_agg(hll_hash_integer(dev))"
    expect.num(_plan_reads_columnar(conn, f"SELECT {agg} FROM ax"), 1,
               "the hll aggregate reads through the columnar scan")
    col = _one(conn, f"SELECT {agg}::text FROM ax")
    heap = _one(conn, f"SELECT {agg}::text FROM ax_heap")
    expect.text("same" if col == heap else "different", "same",
                "hll over a columnar table builds the same sketch, byte for byte, as over heap")
    est = _one(conn, f"SELECT hll_cardinality({agg}) FROM ax")
    expect.text(_within(est, DEVICES, 0.069), "ok",
                "premise: and its estimate is within three standard errors (6.9%) of the exact distinct count")
    conn.execute("CREATE TABLE ax_hll (day int, s hll) USING pgcolumnar")
    conn.execute(f"INSERT INTO ax_hll SELECT day, {agg} FROM ax GROUP BY day")
    stored = _one(conn, "SELECT string_agg(s::text, ',' ORDER BY day) FROM ax_hll")
    fresh = _one(conn, f"SELECT string_agg(s::text, ',' ORDER BY day) FROM "
                       f"(SELECT day, {agg} s FROM ax_heap GROUP BY day) x")
    expect.text("same" if stored == fresh else "different", "same",
                "an hll stored in a columnar table comes back byte for byte")
    union = _one(conn, "SELECT hll_cardinality(hll_union_agg(s)) FROM ax_hll")
    whole = _one(conn, f"SELECT hll_cardinality({agg}) FROM ax_heap")
    expect.text("equal" if union == whole else f"{union} vs {whole}", "equal",
                "and the union of the stored sketches counts what one sketch over all rows counts")

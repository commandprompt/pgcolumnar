"""The gap-tolerant write path (Phase F). The pytest twin of `test/native_gap.sh`, every
check by the shell's name.

The metapage highwater can sit ahead of the physical EOF: the state an aborted or
crash-interrupted end-truncation leaves. A write whose target block is beyond EOF fills
the gap with empty pages and succeeds. A test-only hook, bound here as the shell binds
it, pushes the highwater 50 pages past EOF; then the writes across the gap succeed, the
rows match a heap mirror, the file grows by the gap, and the table stays usable: more
inserts, an index read through, a delete. The fixture runs every step and records
each reading, and each check is its own test, so one red cannot hide the next.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
IDX_SETS = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off")
LOOKUP = "SELECT count(v) FROM n WHERE id BETWEEN 1 AND 7000"


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _size(c):
    return int(c.execute("SELECT pg_relation_size('n')").fetchone()[0])


def _hashes(c):
    """-> (columnar, heap) set digests of the rows."""
    return _one(c, HASH.format("SELECT id, v FROM n")), _one(c, HASH.format("SELECT id, v FROM h"))


def _both(c, sql):
    """Run the statement on the heap mirror, then on the columnar table; a write the
    gap refuses is left for the checks to see, as psql_run leaves it."""
    c.execute(sql.format(t="h"))
    try:
        c.execute(sql.format(t="n"))
    except psycopg.Error as exc:
        print(f"-- columnar write failed: {exc.diag.message_primary}")


@pytest.fixture(scope="module")
def gap(pgc_cluster):
    """Every step of the shell, in its order, with each reading recorded rather than
    asserted, so each check is its own test and one red cannot stop the rest (#1517
    review): 3000 rows, the highwater pushed 50 pages past EOF, 3000 more across the
    gap, 1000 more after it, an index read through, a delete."""
    db = "pgc_gap"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            # the internal test hook, deliberately not in the shipped catalog
            c.execute("CREATE FUNCTION pgcolumnar.debug_advance_reserved_offset(regclass, int) "
                      "RETURNS void AS 'pgcolumnar', 'pgcolumnar_debug_advance_reserved_offset' LANGUAGE C")
            c.execute("CREATE TABLE h (id int, v text)")
            c.execute("CREATE TABLE n (id int, v text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
            r = {}
            _both(c, "INSERT INTO {t} SELECT g, md5(g::text) FROM generate_series(1, 3000) g")
            r["baseline count"], r["baseline"] = _one(c, "SELECT count(*) FROM n"), _hashes(c)
            before = _size(c)

            # the highwater 50 pages past the physical EOF: the next write targets beyond it
            c.execute("SELECT pgcolumnar.debug_advance_reserved_offset('n', 50)")
            _both(c, "INSERT INTO {t} SELECT g, md5(g::text) FROM generate_series(3001, 6000) g")
            r["gapped count"], r["gapped"] = _one(c, "SELECT count(*) FROM n"), _hashes(c)
            r["growth"] = _size(c) - before
            print(f"-- file size: before-gap={before} after-gap={before + r['growth']}")

            _both(c, "INSERT INTO {t} SELECT g, md5(g::text) FROM generate_series(6001, 7000) g")
            r["post-gap"] = _hashes(c)
            c.execute("CREATE INDEX n_id ON n (id)")
            # forced to fetch every row through n_id: by default this count is an Index
            # Only Scan, which stops fetching once a VACUUM marks the pages all-visible
            for s in IDX_SETS:
                c.execute(s)
            try:
                r["plan"] = "\n".join(row[0] for row in c.execute(f"EXPLAIN (COSTS OFF) {LOOKUP}"))
                r["lookup"] = _one(c, LOOKUP)
            finally:
                for g in ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan"):
                    c.execute(f"RESET {g}")

            _both(c, "DELETE FROM {t} WHERE id % 7 = 0")
            r["deleted"] = _hashes(c)
            yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_baseline_row_count(gap, expect):
    expect.text(gap["baseline count"], "3000", "baseline row count")


def test_baseline_parity(gap, expect):
    expect.text(*gap["baseline"], "baseline parity")


def test_row_count_after_gapped_insert(gap, expect):
    expect.text(gap["gapped count"], "6000", "row count after gapped insert")


def test_parity_after_gapped_insert(gap, expect):
    expect.text(*gap["gapped"], "parity after gapped insert")


def test_gap_was_physically_materialized(gap, expect):
    # the 50-page gap and the new rows, with slack: at least 40 pages of growth
    expect.at_least(gap["growth"], 40 * 8192 + 1, "gap was physically materialized")


def test_parity_after_post_gap_insert(gap, expect):
    expect.text(*gap["post-gap"], "parity after post-gap insert")


def test_the_indexed_lookup_reads_n_id(gap, expect):
    expect.text("yes" if "Index Scan using n_id" in gap["plan"] else "no", "yes",
                "premise: the indexed lookup reads n_id")


def test_indexed_lookup_after_gap(gap, expect):
    expect.text(gap["lookup"], "7000", "indexed lookup after gap")


def test_parity_after_delete_on_gapped_layout(gap, expect):
    expect.text(*gap["deleted"], "parity after delete on gapped layout")

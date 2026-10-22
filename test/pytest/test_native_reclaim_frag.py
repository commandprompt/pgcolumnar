"""Coalescing adjacent frees must merge them, stay correct, and never cost file size.

Phase F split/coalesce: with `pgcolumnar.reclaim_coalesce` on, an oversized freed
range is split on reuse and adjacent same-transaction frees are merged. The same
retire+recluster workload runs in both modes and must agree on the data while
disagreeing on the free list -- fewer `free_space` rows with coalescing on is the
direct, deterministic evidence that the coalesce path ran at all.

Independent of test/native_reclaim_frag.sh: same public seam (`pgcolumnar.free_space`,
`pg_relation_size`, and the maintenance functions), own fixture, own observations.
Assertion names match the shell suite so the two can be compared by name, not by
importing each other.

Row sets are compared as sorted tuples in Python rather than through `pgc_set_hash`,
which keeps the two harnesses independent by construction.

WHAT THE PORT ASSERTS THAT THE ORIGINAL SWALLOWS. The original wraps the whole
build-and-compact sequence in `{ ... } >/dev/null 2>&1`. That is deliberate -- the
noise is real -- but it means a failed `INSERT`, a refused `set_options` or an errored
`compact` produces exactly the same visible state as a successful one, and the arms
below it would then compare two empty tables and report parity. Here the driver raises
on any of them, and the row counts are asserted per mode before anything is compared,
so "both modes agree" cannot be satisfied by both modes having done nothing.

THE SPLIT IS MEASURED AS A GAP (#1339). "Never larger" cannot see it: with the split
switched off (`if (pgcolumnar_reclaim_coalesce)` around the remnant insert made
`if (false)`) the final sizes stayed 262144 against 286720, and the arm stayed green.
What the missing split does leave is a tail that belongs to nothing: no live group
and no `free_space` row covers it. So the live groups, rounded up to whole pages as
the allocator reserves them, and the free ranges are laid out in offset order, and
each must end where the next one starts.
"""
ROWS = 30_000
GROUP = 1_000
DEL_LO, DEL_HI = 6001, 24000
LIVE = ROWS - (DEL_HI - DEL_LO + 1)

GEN = (f"SELECT g AS id, repeat('ab', 5 + ((g / 1000) % 16) * 8) AS payload "
       f"FROM generate_series(1, {ROWS}) g")


GAPS = """
WITH p AS (SELECT current_setting('block_size')::bigint - 24 AS pg),
r AS (SELECT file_offset AS off, ((byte_length + p.pg - 1) / p.pg) * p.pg AS len
        FROM pgcolumnar.row_group, p
       WHERE storage_id = pgcolumnar.get_storage_id('n') AND byte_length > 0
      UNION ALL
      SELECT file_offset, byte_length FROM pgcolumnar.free_space
       WHERE storage_id = pgcolumnar.get_storage_id('n')),
s AS (SELECT off, len, lead(off) OVER (ORDER BY off) AS nxt FROM r)
SELECT count(*) FILTER (WHERE nxt > off + len),
       coalesce(sum(nxt - off - len) FILTER (WHERE nxt > off + len), 0)
  FROM s
"""


def _rows(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, payload FROM {table}")
        return sorted(cur.fetchall())


def _run(conn, expect, mode):
    """One whole cycle in one coalesce mode.

    -> (free rows after compact, final size, (gaps, unowned bytes) after recluster)
    """
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS n")
        cur.execute("DROP TABLE IF EXISTS h")
        cur.execute(f"SET pgcolumnar.reclaim_coalesce = {mode}")
        cur.execute("CREATE TABLE h (id int, payload text)")
        cur.execute("CREATE TABLE n (id int, payload text) USING pgcolumnar")
        cur.execute(
            f"SELECT pgcolumnar.set_options('n', stripe_row_limit => {GROUP}, "
            f"chunk_group_row_limit => {GROUP})"
        )
        cur.execute(f"INSERT INTO h {GEN}")
        cur.execute(f"INSERT INTO n {GEN}")
        cur.execute("SELECT count(*) FROM n")
        expect.num(cur.fetchone()[0], ROWS,
                   f"premise: coalesce {mode} -- n holds all {ROWS} rows before the delete")

        # A large CONTIGUOUS block, so compaction frees many small adjacent ranges in
        # one transaction -- which is the only shape coalescing can merge.
        cur.execute(f"DELETE FROM h WHERE id BETWEEN {DEL_LO} AND {DEL_HI}")
        cur.execute(f"DELETE FROM n WHERE id BETWEEN {DEL_LO} AND {DEL_HI}")
        cur.execute("SELECT pgcolumnar.compact('n')")
        cur.execute("SELECT count(*) FROM n")
        expect.num(cur.fetchone()[0], LIVE,
                   f"premise: coalesce {mode} -- {LIVE} rows survive the delete")

        # Counted before anything consumes them.
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.free_space "
            "WHERE storage_id = pgcolumnar.get_storage_id('n')"
        )
        free_rows = cur.fetchone()[0]

        # Force large output groups and recluster, which reuses the freed space.
        cur.execute(
            "SELECT pgcolumnar.set_options('n', stripe_row_limit => 20000, "
            "chunk_group_row_limit => 20000)"
        )
        cur.execute("SELECT pgcolumnar.recluster('n', 'id')")
        cur.execute("SELECT pg_relation_size('n')")
        size = cur.fetchone()[0]
        cur.execute(GAPS)
        gaps, unowned = cur.fetchone()
    return free_rows, size, (gaps, int(unowned))


def test_native_reclaim_frag(pgc_conn, expect):
    on_free, on_size, on_gap = _run(pgc_conn, expect, "on")
    expect.rows(_rows(pgc_conn, "n"), _rows(pgc_conn, "h"),
                "coalesce on: parity with heap")

    off_free, off_size, off_gap = _run(pgc_conn, expect, "off")
    expect.rows(_rows(pgc_conn, "n"), _rows(pgc_conn, "h"),
                "coalesce off: parity with heap")

    print(f"-- free_space rows after compact: on={on_free} off={off_free}")
    print(f"-- final file size: on={on_size} off={off_size}")
    print(f"-- (gaps, unowned bytes) after recluster: on={on_gap} off={off_gap}")

    expect.text(
        "yes" if on_free < off_free else f"no (on={on_free} off={off_free})",
        "yes",
        "coalesce merges adjacent frees (fewer free_space rows)",
    )
    expect.text(
        "yes" if on_size <= off_size else f"no (on={on_size} off={off_size})",
        "yes",
        "coalesce never larger than whole-range reuse",
    )
    expect.text(
        f"{on_gap[0]} gaps of {on_gap[1]} bytes",
        "0 gaps of 0 bytes",
        "coalesce on: reuse splits an oversized freed range, so no byte between "
        "ranges is lost",
    )

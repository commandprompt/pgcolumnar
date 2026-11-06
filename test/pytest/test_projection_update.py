"""UPDATE must fan the new row number out to covering projections (#432, port of
projection_update.sh).

A projection stores the base row number and filters with the base delete vector, so
DELETE needs no rewrite of the projection. UPDATE is delete-old plus insert-new, and
the insert half used to skip fan-out: the projection kept only the deleted number,
`read_projection` returned no rows, and a planner covering-projection scan answered as
if the updated rows had been deleted.

**heap is the oracle**, as in the bash suite. Every comparison is against the same data
in a heap table, so a wrong answer that is wrong the same way on both sides is the only
thing that can slip through, and that is not a shape this defect has.

TWO THINGS THIS PORT DOES DIFFERENTLY.

**The row sets are compared as sets, in Python.** `projection_update.sh` uses
`pgc_set_hash`, which sorts the rendered rows and hashes them. Fetching and sorting
tuples here gives the same order-blind comparison without reaching into the shell
harness for it, and it means a failure prints the rows that differ rather than two
unequal hashes. The two harnesses stay independent by construction rather than by
agreement.

**The UPDATEs assert they changed something.** The bash suite issues each UPDATE and
compares afterwards. An UPDATE whose WHERE matched nothing leaves both sides identical
and every arm below it passes, having exercised no fan-out at all -- the suite would
report green for the defect it exists to catch. The premises here pin the affected row
counts, so the fixture has to keep expressing the question.
"""


def _set(cur, sql):
    """-> the rows as a sorted list, so the comparison is order-blind like pgc_set_hash."""
    cur.execute(sql)
    return sorted(tuple(r) for r in cur.fetchall())


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def test_update_fans_the_new_row_number_out_to_projections(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pu (a int, b text, c int) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('pu', stripe_row_limit => 2000, "
            "chunk_group_row_limit => 1000)"
        )
        cur.execute(
            "SELECT pgcolumnar.add_projection('pu', 'pc', ARRAY['a','c'], ARRAY['c'])"
        )
        cur.execute(
            "INSERT INTO pu SELECT g, 'r'||g, (g*7)%1000 FROM generate_series(1,20000) g"
        )
        cur.execute("ANALYZE pu")
        cur.execute("CREATE TABLE pu_h (a int, b text, c int) USING heap")
        cur.execute(
            "INSERT INTO pu_h SELECT g, 'r'||g, (g*7)%1000 FROM generate_series(1,20000) g"
        )

        # EXTRA: the qual has to select part of the table, not all of it and not none.
        # A range that matched everything would make the covering scan and the full
        # scan the same query, and the arms below would stop being distinct.
        sel = _one(cur, "SELECT count(*) FROM pu WHERE c BETWEEN 100 AND 200")
        expect.text("partial" if 0 < sel < 20000 else f"degenerate ({sel})", "partial",
                    "premise: the sort-key range selects part of the table")

        cur.execute("EXPLAIN (COSTS OFF) SELECT a, c FROM pu WHERE c BETWEEN 100 AND 200")
        plan = "\n".join(r[0] for r in cur.fetchall())
        expect.num(plan.count("Columnar Projection: pc"), 1,
                   "planner uses the covering projection")

        q_c = "SELECT a, c FROM pu WHERE c BETWEEN 100 AND 200"
        q_h = "SELECT a, c FROM pu_h WHERE c BETWEEN 100 AND 200"
        expect.rows(_set(cur, q_c), _set(cur, q_h),
                    "projection matches the heap before any update")

        # --- updating a PROJECTED column ------------------------------------
        cur.execute("UPDATE pu   SET a = a + 1 WHERE a % 10 = 0")
        n_c = cur.rowcount
        cur.execute("UPDATE pu_h SET a = a + 1 WHERE a % 10 = 0")
        n_h = cur.rowcount
        # EXTRA: an UPDATE that matched nothing leaves every arm below green having
        # exercised no fan-out.
        expect.num(n_c, 2000, "premise: the projected-column update changed rows")
        expect.num(n_h, n_c, "premise: and changed the same number on the heap side")

        expect.rows(_set(cur, q_c), _set(cur, q_h),
                    "covering projection scan matches heap after updating a projected column")
        expect.rows(
            _set(cur, "SELECT pgcolumnar.read_projection('pu','pc')"),
            _set(cur, "SELECT a::text || '|' || c::text FROM pu"),
            "read_projection matches the live base after that update",
        )
        expect.rows(
            _set(cur, "SELECT a, c FROM pu"),
            _set(cur, "SELECT a, c FROM pu_h"),
            "full-table projection scan still matches heap",
        )

        # --- updating a column the projection does not store -----------------
        #
        # It still allocates a new base row number, so the fan-out has to happen for a
        # column the projection has never heard of.
        cur.execute("UPDATE pu   SET b = 'y' WHERE a % 7 = 0")
        m_c = cur.rowcount
        cur.execute("UPDATE pu_h SET b = 'y' WHERE a % 7 = 0")
        expect.text("changed" if m_c > 0 else "matched nothing", "changed",
                    "premise: the non-projected-column update changed rows")

        expect.rows(_set(cur, q_c), _set(cur, q_h),
                    "covering projection scan matches heap after updating a non-projected column")
        expect.rows(
            _set(cur, "SELECT pgcolumnar.read_projection('pu','pc')"),
            _set(cur, "SELECT a::text || '|' || c::text FROM pu"),
            "read_projection still matches the live base",
        )
        # AGAINST HEAP, NOT THE BASE (#1339). Both arms above and this one used to
        # compare with `pu` itself, and b was never compared with heap after its own
        # update: an UPDATE that kept b's old value while fanning the new row number
        # out matched the base it had just written, and every arm stayed green.
        expect.rows(
            _set(cur, "SELECT pgcolumnar.reconstruct_via_projection('pu','pc')"),
            _set(cur, "SELECT a::text || '|' || b || '|' || c::text FROM pu_h"),
            "reconstruct via the projection still rebuilds every live row",
        )
        expect.rows(_set(cur, "SELECT a, b, c FROM pu"), _set(cur, "SELECT a, b, c FROM pu_h"),
                    "every column matches heap after updating a non-projected column")

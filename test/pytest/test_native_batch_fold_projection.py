"""The ungrouped batch fold gathers exactly the columns it needs on a wide table (#10,
#1341). The pytest twin of `test/native_batch_fold_projection.sh`, every check by the
shell's name.

The fold's per-row loops step over compact key and payload column lists instead of
masking every attribute, and a list that came out short would silently return a wrong
aggregate. So a 47-column table sums its LAST column, under a selective pushed key so
the deferred-payload path runs, and the folded answer must equal the unfolded one. The
fold must really have run (EXPLAIN ANALYZE's "Batch Fold: yes", not the prediction a
plain EXPLAIN prints); the reference must really be unfolded, or the comparison is of
the fold with itself; and the reference must equal the generator's own answer, which
involves no table.
"""
FILL = ", ".join(f"f{i} bigint" for i in range(44))
FVALS = ", ".join(f"g*{i + 2}" for i in range(44))
INNER = "SELECT count(*) n, sum(v) sv FROM t WHERE k < 400"
PAR = ("SET max_parallel_workers_per_gather=4", "SET parallel_setup_cost=0", "SET parallel_tuple_cost=0",
       "SET min_parallel_table_scan_size=0", "SET min_parallel_index_scan_size=0")
FOLDOFF = PAR + ("SET pgcolumnar.enable_ungrouped_vector_agg=off", "SET pgcolumnar.enable_group_vectorization=off")
FOLDON = PAR + ("SET pgcolumnar.enable_ungrouped_vector_agg=on", "SET pgcolumnar.enable_parallel_vector_agg=on")
RESETS = ("max_parallel_workers_per_gather", "parallel_setup_cost", "parallel_tuple_cost",
          "min_parallel_table_scan_size", "min_parallel_index_scan_size", "pgcolumnar.enable_ungrouped_vector_agg",
          "pgcolumnar.enable_group_vectorization", "pgcolumnar.enable_parallel_vector_agg")


def _under(cur, gucs, sql):
    """-> the rows of `sql` under `gucs`, every setting reset afterwards."""
    for g in gucs:
        cur.execute(g)
    try:
        cur.execute(sql)
        return cur.fetchall()
    finally:
        for g in RESETS:
            cur.execute(f"RESET {g}")


def _answer(rows):
    return "|".join(str(v) for v in rows[0])


def _folded(cur, gucs):
    plan = "\n".join(r[0] for r in _under(cur, gucs, f"EXPLAIN (ANALYZE, COSTS OFF, VERBOSE, TIMING OFF) {INNER}"))
    return sum(1 for line in plan.splitlines() if "batch fold: yes" in line.lower()), plan


def test_the_batch_fold_gathers_the_right_columns_on_a_wide_table(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute(f"CREATE TABLE t (id int, k int, {FILL}, v float8) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 40000)")
        cur.execute(f"INSERT INTO t SELECT g, g % 1000, {FVALS}, "
                    "CASE WHEN g % 50 = 0 THEN NULL ELSE ((g % 13) - 6)::float8 END FROM generate_series(1, 400000) g")
        cur.execute("ANALYZE t")

        ref = _answer(_under(cur, FOLDOFF, INNER))
        got = _answer(_under(cur, FOLDON, INNER))
        print(f"-- ref=[{ref}] got=[{got}]")
        # ANALYZE: a plain EXPLAIN prints the prediction, not whether the fold ran (#1149)
        folds, plan = _folded(cur, FOLDON)
        expect.text("yes" if folds > 0 else f"no ({plan.lower().count('batch fold')} Batch Fold lines)", "yes",
                    "the ungrouped batch fold actually ran (Batch Fold: yes)")
        expect.text("yes" if ref not in ("", "<none>") else "no", "yes",
                    "ref is a non-empty result (not a vacuous empty compare)")
        expect.num(_folded(cur, FOLDOFF)[0], 0, "premise: the reference runs without the batch fold")
        cur.execute("SELECT count(*), sum(CASE WHEN g % 50 = 0 THEN NULL ELSE ((g % 13) - 6)::float8 END) "
                    "FROM generate_series(1, 400000) g WHERE g % 1000 < 400")
        expect.text(ref, _answer(cur.fetchall()), "the non-fold aggregate equals the generator's own answer")
        expect.text(got, ref, "the folded aggregate equals the non-fold aggregate (payload columns correct)")
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1, "backend alive")

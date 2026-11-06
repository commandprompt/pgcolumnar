"""A covering projection scan must not be priced at half the base.

PgColumnarSetRelPathlist offers a covering-projection path by taking the
base custom-scan run cost and multiplying by 0.5. That constant does not
depend on the restriction, so two ranges on the sort key are quoted at
the same fraction of the base.

This file asserts the PLANNER ratio, not a runtime. Independent of
test/projection_scan_cost.sh: same public seam (EXPLAIN of a columnar
scan with the projection-scan GUC on and off), own fixture, own
observations. Assertion names match the shell suite so the two can be
compared by name, not by importing each other.
"""


def _nodes(plan):
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        yield node
        stack.extend(node.get("Plans") or ())


def _custom_scan(plan):
    for node in _nodes(plan):
        if node.get("Node Type") == "Custom Scan":
            return node
    return None


def _plan(conn, sql, projection_scan):
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SET pgcolumnar.enable_ungrouped_vector_agg = off")
        cur.execute("SET pgcolumnar.enable_group_vectorization = off")
        cur.execute("SET jit = off")
        cur.execute(
            "SET pgcolumnar.enable_projection_scan = "
            + ("on" if projection_scan else "off")
        )
        cur.execute("EXPLAIN (FORMAT JSON, COSTS ON) " + sql)
        return cur.fetchone()[0]


def test_projection_scan_cost(pgc_conn, expect):
    n = 24000
    tight_hi = 1800
    loose_hi = 12000
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pscost (k int, blob text) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('pscost', stripe_row_limit => 1200, "
            "chunk_group_row_limit => 400)"
        )
        # Scrambled insert order so the base layout cannot prune on k.
        # Different N, stripe, payload, and bounds from the shell twin.
        cur.execute(
            f"INSERT INTO pscost SELECT k, md5(k::text) FROM generate_series(1, {n}) k "
            "ORDER BY md5((k + 17)::text)"
        )
        cur.execute(
            "SELECT pgcolumnar.add_projection('pscost', 'onk', ARRAY['k'], ARRAY['k'])"
        )
        cur.execute("ANALYZE pscost")
        cur.execute("SELECT count(*) FROM pscost")
        expect.num(cur.fetchone()[0], n, "premise: the table holds every inserted row")
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.projection_declaration "
            "WHERE rel = 'pscost'::regclass AND name = 'onk'"
        )
        expect.num(cur.fetchone()[0], 1, "premise: a covering projection exists")

    sql_tight = f"SELECT k FROM pscost WHERE k BETWEEN 1 AND {tight_hi}"
    sql_loose = f"SELECT k FROM pscost WHERE k BETWEEN 1 AND {loose_hi}"

    tight_proj = _plan(pgc_conn, sql_tight, True)
    loose_proj = _plan(pgc_conn, sql_loose, True)
    tight_base = _plan(pgc_conn, sql_tight, False)
    loose_base = _plan(pgc_conn, sql_loose, False)

    tp = _custom_scan(tight_proj)
    lp = _custom_scan(loose_proj)
    tb = _custom_scan(tight_base)
    lb = _custom_scan(loose_base)

    expect.text(
        (tp or {}).get("Columnar Projection") or "none",
        "onk",
        "the tight plan uses the covering projection",
    )
    expect.text(
        (lp or {}).get("Columnar Projection") or "none",
        "onk",
        "the loose plan uses the covering projection",
    )
    expect.text(
        "none" if tb is None or "Columnar Projection" not in tb else tb["Columnar Projection"],
        "none",
        "premise: without the projection scan, the tight plan is a base columnar scan",
    )
    expect.text(
        "none" if lb is None or "Columnar Projection" not in lb else lb["Columnar Projection"],
        "none",
        "premise: without the projection scan, the loose plan is a base columnar scan",
    )

    t_proj_run = tp["Total Cost"] - tp["Startup Cost"]
    l_proj_run = lp["Total Cost"] - lp["Startup Cost"]
    t_base_run = tb["Total Cost"] - tb["Startup Cost"]
    l_base_run = lb["Total Cost"] - lb["Startup Cost"]
    zero = [what for what, run in (("tight projection", t_proj_run),
                                   ("loose projection", l_proj_run),
                                   ("tight base", t_base_run),
                                   ("loose base", l_base_run)) if run <= 0]
    expect.text(
        "all positive" if not zero else "no run cost: " + ", ".join(zero),
        "all positive",
        "premise: every compared scan has a positive run cost",
    )

    t_ratio = t_proj_run / t_base_run
    l_ratio = l_proj_run / l_base_run
    print(
        f"-- tight proj_run={t_proj_run} base_run={t_base_run} ratio={t_ratio:.3f}"
    )
    print(
        f"-- loose proj_run={l_proj_run} base_run={l_base_run} ratio={l_ratio:.3f}"
    )

    expect.text(
        "tighter" if t_ratio < l_ratio
        else f"tight {t_ratio:.3f} not below loose {l_ratio:.3f}",
        "tighter",
        "a tight covering projection is cheaper relative to the base than a loose one",
    )
    both_halved = (
        0.45 < t_ratio < 0.55 and 0.45 < l_ratio < 0.55
    )
    expect.text(
        "both-halved" if both_halved else "scaled",
        "scaled",
        "tight and loose covering scans are not both priced at half the base",
    )

    # Independent of the shell twin: different table, N, stripe, column names,
    # and rare-value density. Same public EXPLAIN seam.
    n_attr = 30000
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE psmis (ikey int, flag text) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('psmis', stripe_row_limit => 1500, "
            "chunk_group_row_limit => 500)"
        )
        cur.execute(
            f"INSERT INTO psmis SELECT ikey, "
            f"CASE WHEN ikey % 1500 = 0 THEN 'x' ELSE 'y' END "
            f"FROM generate_series(1, {n_attr}) ikey "
            "ORDER BY md5((ikey + 41)::text)"
        )
        cur.execute(
            "SELECT pgcolumnar.add_projection('psmis', 'onikey', "
            "ARRAY['ikey','flag'], ARRAY['ikey'])"
        )
        cur.execute("ANALYZE psmis")
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.projection_declaration "
            "WHERE rel = 'psmis'::regclass AND name = 'onikey'"
        )
        expect.num(
            cur.fetchone()[0],
            1,
            "premise: the misattributed query has a covering projection",
        )

    sql_mis = (
        f"SELECT ikey FROM psmis WHERE ikey BETWEEN 1 AND {n_attr} "
        "AND flag = 'x'"
    )
    mis_proj = _plan(pgc_conn, sql_mis, True)
    mis_base = _plan(pgc_conn, sql_mis, False)
    mp = _custom_scan(mis_proj)
    mb = _custom_scan(mis_base)
    m_proj_run = mp["Total Cost"] - mp["Startup Cost"]
    m_base_run = mb["Total Cost"] - mb["Startup Cost"]
    zero = [what for what, run in (("projection", m_proj_run),
                                   ("base", m_base_run)) if run <= 0]
    expect.text(
        "all positive" if not zero else "no run cost: " + ", ".join(zero),
        "all positive",
        "premise: every misattributed scan has a positive run cost",
    )
    m_ratio = m_proj_run / m_base_run
    print(f"-- misattr proj_run={m_proj_run} base_run={m_base_run} ratio={m_ratio:.3f}")
    expect.text(
        "not-cheap" if m_ratio >= 0.8 else f"ratio {m_ratio:.3f} below 0.8",
        "not-cheap",
        "a non-sort-key restriction does not cheapen a covering projection",
    )

    # ---- mentioning the sort key is not pruning on it (#1126) ----------------
    #
    # The arm above covers a clause on ANOTHER column. This one covers a clause
    # that references the sort key and still cannot prune on it: one RestrictInfo
    # ORing a sort-key range with a predicate on another column. A membership test
    # counts it whole; a scan-key test does not, because a BoolExpr never becomes
    # a key.
    #
    # THE RANGE MUST CLEAR THE ONE-STRIPE FLOOR. 30000 rows at stripe_row_limit
    # 1500 is 20 stripes, so the floor is 0.05 and a small range prices there
    # whether the arithmetic is right or wrong. 3000 rows is 10%. The shell twin's
    # first version of this arm used a range below the floor and passed against the
    # defect; the premise below is what stops that recurring here.
    #
    # Same psmis fixture, because the question is about the CLAUSE and not the
    # data: flag = 'x' holds where ikey % 1500 = 0, so its rows sit in every stripe
    # and no sort order on ikey gathers them.
    or_hi = 3000
    sql_or = f"SELECT ikey FROM psmis WHERE ikey BETWEEN 1 AND {or_hi} OR flag = 'x'"
    sql_prune = f"SELECT ikey FROM psmis WHERE ikey BETWEEN 1 AND {or_hi}"
    sql_saop = "SELECT ikey FROM psmis WHERE ikey = ANY (ARRAY[1,2,3,4,5,6,7,8,9,10])"

    def _run_ratio(sql):
        pj = _custom_scan(_plan(pgc_conn, sql, True))
        bs = _custom_scan(_plan(pgc_conn, sql, False))
        pr = pj["Total Cost"] - pj["Startup Cost"]
        br = bs["Total Cost"] - bs["Startup Cost"]
        return pr, br, (pr / br if br > 0 else 0.0)

    or_run, or_base, or_ratio = _run_ratio(sql_or)
    pr_run, pr_base, pr_ratio = _run_ratio(sql_prune)
    sa_run, sa_base, sa_ratio = _run_ratio(sql_saop)
    print(f"-- unprunable OR ratio={or_ratio:.3f} "
          f"prunable range ratio={pr_ratio:.3f} IN-list ratio={sa_ratio:.3f}")

    zero = [what for what, run in (("OR projection", or_run), ("OR base", or_base),
                                   ("range projection", pr_run), ("range base", pr_base),
                                   ("IN-list projection", sa_run),
                                   ("IN-list base", sa_base)) if run <= 0]
    expect.text(
        "all positive" if not zero else "no run cost: " + ", ".join(zero),
        "all positive",
        "premise: every unprunable-clause scan has a positive run cost",
    )

    # Without this the three arms below can all agree at the floor, which is
    # agreement for a reason unrelated to what they assert.
    expect.text(
        "above" if pr_ratio > 0.051
        else f"ratio {pr_ratio:.3f} at or below the 0.051 one-stripe floor",
        "above",
        "premise: the prunable range is priced above the one-stripe floor, "
        "so the arms differ",
    )

    expect.text(
        "not-cheap" if or_ratio >= 0.8 else f"ratio {or_ratio:.3f} below 0.8",
        "not-cheap",
        "a clause that mentions the sort key but cannot prune on it does not "
        "cheapen a covering projection",
    )

    # The silent direction. Declining a projection that would have won costs a
    # plan and reddens nothing, so both controls ship with the arm.
    expect.text(
        "cheap" if pr_ratio < 0.8 else f"ratio {pr_ratio:.3f} at or above 0.8",
        "cheap",
        "while a plain range on the sort key still earns its discount",
    )

    # An IN-list range key is conservative, so `exact` is false for it. A fix
    # gating on exactness rather than on producing a key would decline this.
    expect.text(
        "cheap" if sa_ratio < 0.8 else f"ratio {sa_ratio:.3f} at or above 0.8",
        "cheap",
        "and an IN-list on the sort key keeps its discount, which gating on "
        "exactness would lose",
    )

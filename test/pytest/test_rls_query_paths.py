"""Row-level security on every columnar read path (#1341). The pytest twin of
`test/rls_query_paths.sh`, every check by the shell's name.

A columnar table and a heap mirror hold the same 20000 rows, half owned by each of two
roles, under a policy that shows a role only its own rows. One role, on its own login,
queries both by the custom scan, the vectorized and grouped aggregates, a parallel
scan, an index-only scan and a covering projection, and each answer must be heap's; the
plan premises check each path was taken. A cheap function that is not leakproof must
see only that role's rows: its NOTICEs are counted by a notice handler.
"""
import psycopg

ALICE, BOB = "rqp_alice", "rqp_bob"
AGG = ("SELECT count(*) || '/' || sum(v) || '/' || min(v) || '/' || max(v) || '/' || "
       "count(*) FILTER (WHERE owner <> current_user) FROM %t")
GRP = ("SELECT string_agg(grp || ':' || n || ':' || s, ',' ORDER BY grp) "
       "FROM (SELECT grp, count(*) n, sum(v) s FROM %t GROUP BY grp) x")
OFF = ("SET pgcolumnar.enable_vectorization = off", "SET pgcolumnar.enable_group_vectorization = off",
       "SET pgcolumnar.enable_custom_scan = off")
PAR = ("SET parallel_setup_cost = 0", "SET parallel_tuple_cost = 0", "SET min_parallel_table_scan_size = 0",
       "SET max_parallel_workers_per_gather = 2")
IOS = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off")
IQ = "SELECT count(*) || '/' || sum(id) FROM %t WHERE id BETWEEN 100 AND 15000"
PQ = "SELECT count(*) || '/' || sum(v) FROM %t WHERE id BETWEEN 100 AND 15000"


def test_row_level_security_holds_on_every_read_path(pgc_cluster, pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        for r in (ALICE, BOB):
            cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{r}'")
            if cur.fetchone() is None:
                cur.execute(f"CREATE ROLE {r} LOGIN NOSUPERUSER")
            cur.execute(f'GRANT USAGE ON SCHEMA "{schema}", pgcolumnar TO {r}')
        cur.execute("CREATE FUNCTION rq_leak(text) RETURNS bool LANGUAGE plpgsql COST 0.0001 "
                    "AS $$ BEGIN RAISE NOTICE 'seen:%', $1; RETURN true; END $$")
        for t, am in (("rq", "pgcolumnar"), ("rh", "heap")):
            cur.execute(f"CREATE TABLE {t} (id int, owner name, grp int, v int, secret text) USING {am}")
            cur.execute(f"INSERT INTO {t} SELECT g, CASE WHEN g % 2 = 0 THEN '{ALICE}' ELSE '{BOB}' END, g % 10, g, "
                        f"'S-' || CASE WHEN g % 2 = 0 THEN '{ALICE}' ELSE '{BOB}' END || '-' || g "
                        f"FROM generate_series(1, 20000) g")
            cur.execute(f"CREATE INDEX {t}_io ON {t} (id, owner)")
            cur.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            cur.execute(f"CREATE POLICY {t}_own ON {t} USING (owner = current_user)")
            cur.execute(f"GRANT SELECT ON {t} TO {ALICE}, {BOB}")
            cur.execute(f"VACUUM ANALYZE {t}")
        cur.execute("SELECT pgcolumnar.add_projection('rq', 'rq_p', ARRAY['id', 'owner', 'v'], ARRAY['id'])")
        cur.execute("SELECT count(*) FROM rq")
        total = cur.fetchone()[0]

    alice = psycopg.connect(pgc_cluster.dsn().replace("user=postgres", f"user={ALICE}"), autocommit=True)
    notices = []
    alice.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    try:
        alice.execute(f'SET search_path TO "{schema}", public, pgcolumnar')

        def run(setup, sql):
            try:
                for s in setup:
                    alice.execute(s)
                return alice.execute(sql).fetchone()[0]
            except psycopg.Error as exc:
                return f"ERROR: {exc.sqlstate}"
            finally:
                alice.execute("RESET ALL")
                alice.execute(f'SET search_path TO "{schema}", public, pgcolumnar')

        def same(setup, sql):
            c, h = run(setup, sql.replace("%t", "rq")), run(setup, sql.replace("%t", "rh"))
            ok = c == h and c is not None and not str(c).startswith("ERROR")
            return "same" if ok else f"columnar=[{c}] heap=[{h}]"

        def plan_has(setup, sql, needle):
            for s in setup:
                alice.execute(s)
            plan = "\n".join(r[0] for r in alice.execute("EXPLAIN (COSTS OFF) " + sql.replace("%t", "rq")).fetchall())
            alice.execute("RESET ALL")
            alice.execute(f'SET search_path TO "{schema}", public, pgcolumnar')
            return sum(1 for line in plan.splitlines() if needle in line)

        def leaks(table, setup):
            notices.clear()
            run(setup, f"SELECT count(*) FROM {table} WHERE rq_leak(secret)")
            seen = [n for n in notices if n.startswith("seen:")]
            return f"{len(seen)}|{sum(1 for n in seen if n.startswith(f'seen:S-{BOB}'))}"

        expect.text(f"{total}|{run((), 'SELECT count(*) FROM rq')}|{run((), 'SELECT count(*) FROM rh')}",
                    "20000|10000|10000",
                    "premise: a superuser sees all 20000 rows, and rq_alice her 10000 on both tables")
        expect.num(plan_has((), AGG, "Custom Scan (PgColumnarScan)"), 1,
                   "premise: the aggregate is answered by the columnar custom scan")
        expect.text(same((), AGG), "same", "count, sum, min and max under the policy, by the custom scan, are heap's")
        expect.text(same((), GRP), "same", "a GROUP BY under the policy, with vectorization on, is heap's")
        expect.text(f"{same(OFF, GRP)}|{same(OFF, AGG)}", "same|same", "and with vectorization and the custom scan off")
        expect.num(plan_has(PAR, AGG, "Gather"), 1, "premise: the parallel arm's plan gathers from workers")
        expect.text(f"{same(PAR, AGG)}|{same(PAR, GRP)}", "same|same",
                    "count, sum, min and max under the policy, by a parallel scan, are heap's")
        expect.num(plan_has(IOS, IQ, "Index Only Scan"), 1, "premise: the index-only arm's plan is an index-only scan")
        expect.text(same(IOS, IQ), "same", "an index-only scan under the policy is heap's")
        expect.num(plan_has((), PQ, "Columnar Projection: rq_p"), 1,
                   "premise: the projection arm's plan reads the covering projection")
        expect.text(same((), PQ), "same", "a covering projection scan under the policy is heap's")
        expect.text(f"{leaks('rq', ())}|{leaks('rh', ())}", "10000|0|10000|0",
                    "a cheap function that is not leakproof sees only rq_alice's rows on columnar, as on heap")
        expect.text(leaks("rq", OFF), "10000|0", "and the same with vectorization and the custom scan off")
        expect.text(leaks("rq", PAR), "10000|0", "and through a parallel scan")
    finally:
        alice.close()

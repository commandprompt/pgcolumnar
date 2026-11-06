"""pgcolumnar.maintenance_due(rel) reports whether compact_rewrite or recluster is worth
running, from table statistics alone (#415). The pytest twin of
`test/maintenance_due.sh`, every check by the shell's name (#1341).

Five fixtures: a clean ordered table (nothing due); 40% deleted and never ordered
(compact_rewrite only, and the threshold is a live parameter); ordered then 25%
appended (recluster only, likewise); never ordered in two batches, where sort_status
counts every group appended and the sorted-run guard must still say nothing is due; and
both decays at once. A role holding SELECT on the table gets a verdict, and a role
without it is refused with 42501.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). The two roles log in on their own, as the shell's do, and
are dropped at teardown. A failed reading is a failed reading: two failures cannot
agree.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_maintenance_due"
READER, NONE = "pgc_md_reader", "pgc_md_none"
CG = 1000


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _text(v):
    """psql's -A rendering of a scalar: booleans as t/f, NULL as empty."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v)


def _q(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no row from {sql}") if row is None else _text(row[0])


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _mk(dsn, t, n):
    _run(dsn, f"CREATE TABLE {t} (ts int, v int, pad text) USING pgcolumnar; "
              f"SELECT pgcolumnar.set_options('{t}', chunk_group_row_limit => {CG});")
    _gen(dsn, t, n)


def _gen(dsn, t, n):
    _run(dsn, f"INSERT INTO {t} SELECT g, g % 100, md5(g::text) FROM generate_series(1, {n}) g")


def _as(cluster, role, sql):
    """-> (sqlstate or None, value) for SQL on a login as ROLE."""
    dsn = f"host=127.0.0.1 port={cluster.port} user={role} dbname={DB}"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute(sql).fetchone()
            return None, (_text(row[0]) if row else "")
    except psycopg.Error as exc:
        return exc.sqlstate, query_error(_message(exc))


@pytest.fixture(scope="module")
def md(pgc_cluster):
    admin = pgc_cluster.dsn()
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        for role in (READER, NONE):
            a.execute(f"DROP ROLE IF EXISTS {role}")
            a.execute(f"CREATE ROLE {role} LOGIN")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        due = "SELECT {} FROM pgcolumnar.maintenance_due('{}')"
        r = {}

        _mk(dsn, "md_clean", 20000)
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('md_clean', 'ts')")
        r["clean deleted"] = _q(dsn, "SELECT COALESCE(sum(deletedrows),0) FROM pgcolumnar.stats('md_clean')")
        r["clean appended"] = _q(dsn, "SELECT appended_groups FROM pgcolumnar.sort_status('md_clean')")
        r["clean run"] = _q(dsn, "SELECT sorted_groups > 0 FROM pgcolumnar.sort_status('md_clean')")
        for col in ("compact_rewrite_due", "recluster_due", "recommendation IS NULL"):
            r[f"clean {col}"] = _q(dsn, due.format(col, "md_clean"))

        _mk(dsn, "md_deleted", 20000)
        _run(dsn, "DELETE FROM md_deleted WHERE (ts * 2654435761)::bigint % 100 < 40")
        _run(dsn, "SELECT pgcolumnar.compact('md_deleted')")
        # core's reading of the DELETE's predicate: maintenance_due sums stats(), so an
        # oracle read from stats() moves with it (#1597 review)
        r["df"] = _q(dsn, "SELECT round(avg((((g * 2654435761)::bigint % 100) < 40)::int), 2) "
                          "FROM generate_series(1, 20000) g")
        r["df md"] = _q(dsn, "SELECT round(deleted_fraction::numeric,2) FROM pgcolumnar.maintenance_due('md_deleted')")
        for col in ("compact_rewrite_due", "recluster_due", "recommendation"):
            r[f"deleted {col}"] = _q(dsn, due.format(col, "md_deleted"))
        r["deleted raised"] = _q(dsn, "SELECT compact_rewrite_due FROM pgcolumnar.maintenance_due('md_deleted', 0.5, 0.05)")

        _mk(dsn, "md_appended", 20000)
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('md_appended', 'ts')")
        _gen(dsn, "md_appended", 5000)
        r["appended appended"] = _q(dsn, "SELECT appended_groups > 0 FROM pgcolumnar.sort_status('md_appended')")
        r["appended run"] = _q(dsn, "SELECT sorted_groups > 0 FROM pgcolumnar.sort_status('md_appended')")
        for col in ("recluster_due", "compact_rewrite_due", "recommendation"):
            r[f"appended {col}"] = _q(dsn, due.format(col, "md_appended"))
        r["appended raised"] = _q(dsn, "SELECT recluster_due FROM pgcolumnar.maintenance_due('md_appended', 0.2, 0.99)")

        _mk(dsn, "md_neverord", 20000)
        _gen(dsn, "md_neverord", 5000)
        r["never run"] = _q(dsn, "SELECT sorted_groups = 0 FROM pgcolumnar.sort_status('md_neverord')")
        r["never appended"] = _q(dsn, "SELECT appended_groups > 0 FROM pgcolumnar.sort_status('md_neverord')")
        for col in ("recluster_due", "recommendation IS NULL"):
            r[f"never {col}"] = _q(dsn, due.format(col, "md_neverord"))

        _mk(dsn, "md_both", 20000)
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('md_both', 'ts')")
        _run(dsn, "DELETE FROM md_both WHERE (ts * 2654435761)::bigint % 100 < 40")
        _gen(dsn, "md_both", 5000)
        for col in ("compact_rewrite_due", "recluster_due", "recommendation"):
            r[f"both {col}"] = _q(dsn, due.format(col, "md_both"))

        _run(dsn, f"GRANT USAGE ON SCHEMA pgcolumnar TO {READER}; GRANT SELECT ON md_clean TO {READER}; "
                  f"GRANT USAGE ON SCHEMA pgcolumnar TO {NONE};")
        verdict = "SELECT compact_rewrite_due FROM pgcolumnar.maintenance_due('md_clean')"
        # a refusal is a reading, not a failed query: it is recorded by name, as the shell's is
        state, value = _as(pgc_cluster, READER, verdict)
        r["positive"] = f"refused ({state})" if state else value
        r["deny"] = _as(pgc_cluster, NONE, verdict)[0] or "no error"
        yield r
    finally:
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            for role in (READER, NONE):
                a.execute(f"DROP ROLE IF EXISTS {role}")


def test_md_clean_has_no_deleted_rows(md, expect):
    expect.text(md["clean deleted"], "0", "md_clean has no deleted rows")


def test_md_clean_is_fully_in_its_sorted_run(md, expect):
    expect.text(md["clean appended"], "0", "md_clean is fully in its sorted run (0 appended groups)")


def test_md_clean_has_a_sorted_run(md, expect):
    expect.text(md["clean run"], "t", "md_clean has a sorted run")


def test_clean_compact_rewrite_not_due(md, expect):
    expect.text(md["clean compact_rewrite_due"], "f", "clean: compact_rewrite not due")


def test_clean_recluster_not_due(md, expect):
    expect.text(md["clean recluster_due"], "f", "clean: recluster not due")


def test_clean_recommendation_is_null(md, expect):
    expect.text(md["clean recommendation IS NULL"], "t", "clean: recommendation is null")


def test_md_deleted_deleted_fraction_is_about_0_40(md, expect):
    expect.text(md["df"], "0.40", "premise: md_deleted deleted fraction ~0.40")


def test_delete_maintenance_due_reports_the_same_deleted_fraction(md, expect):
    expect.text(md["df md"], md["df"], "delete: maintenance_due reports the same deleted_fraction")


def test_delete_compact_rewrite_due(md, expect):
    expect.text(md["deleted compact_rewrite_due"], "t", "delete: compact_rewrite due (0.40 >= 0.2 default)")


def test_delete_recluster_not_due(md, expect):
    expect.text(md["deleted recluster_due"], "f", "delete: recluster NOT due (table was never ordered)")


def test_delete_recommendation_names_compact_rewrite_only(md, expect):
    expect.text(md["deleted recommendation"], "compact_rewrite", "delete: recommendation names compact_rewrite only")


def test_delete_raising_the_compact_threshold_makes_it_not_due(md, expect):
    expect.text(md["deleted raised"], "f", "delete: raising the compact threshold above 0.40 makes it not due")


def test_md_appended_has_appended_groups(md, expect):
    expect.text(md["appended appended"], "t", "md_appended has appended groups")


def test_md_appended_still_has_a_sorted_run(md, expect):
    expect.text(md["appended run"], "t", "md_appended still has a sorted run")


def test_appended_recluster_due(md, expect):
    expect.text(md["appended recluster_due"], "t", "appended: recluster due")


def test_appended_compact_rewrite_not_due(md, expect):
    expect.text(md["appended compact_rewrite_due"], "f", "appended: compact_rewrite not due (no deletes)")


def test_appended_recommendation_names_recluster_only(md, expect):
    expect.text(md["appended recommendation"], "recluster", "appended: recommendation names recluster only")


def test_appended_raising_the_recluster_threshold_makes_it_not_due(md, expect):
    expect.text(md["appended raised"], "f",
                "appended: raising the recluster threshold above the fraction makes it not due")


def test_never_ordered_table_has_no_sorted_run(md, expect):
    expect.text(md["never run"], "t", "never-ordered table has NO sorted run")


def test_yet_sort_status_still_counts_appended_groups(md, expect):
    expect.text(md["never appended"], "t", "yet sort_status still counts appended groups (> 0)")


def test_guard_recluster_not_due_on_a_never_ordered_table(md, expect):
    expect.text(md["never recluster_due"], "f", "GUARD: recluster NOT due on a never-ordered table")


def test_guard_recommendation_is_null_on_a_never_ordered_table(md, expect):
    expect.text(md["never recommendation IS NULL"], "t", "GUARD: recommendation is null on a never-ordered table")


def test_both_compact_rewrite_due(md, expect):
    expect.text(md["both compact_rewrite_due"], "t", "both: compact_rewrite due")


def test_both_recluster_due(md, expect):
    expect.text(md["both recluster_due"], "t", "both: recluster due")


def test_both_recommendation_names_both_verbs(md, expect):
    expect.text(md["both recommendation"], "compact_rewrite, recluster", "both: recommendation names both verbs")


def test_positive_a_select_holder_gets_a_verdict(md, expect):
    expect.text(md["positive"], "f", "positive: a SELECT-holder gets a verdict, not a false deny")


def test_deny_a_role_without_select_is_refused(md, expect):
    expect.text(str(md["deny"]), "42501", "deny: a role without SELECT on the relation is refused (42501)")

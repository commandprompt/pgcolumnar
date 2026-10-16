"""Physical page reclaim: repeated online compaction reuses freed space, and the
compaction thresholds are guarded in both directions (Phase F, #860, #1341 item 70). The
pytest twin of `test/native_reclaim.sh`, every check by the shell's name.

Retiring a row group records its byte range in pgcolumnar.free_space, and a later
online compaction reserves from a freed range instead of growing the file. So repeated
reclusters plateau the file, free_space stays bounded, and the reused blocks read back
the heap mirror's rows. The reclusters ALTERNATE their key: recluster self-gates on a
table already in that key's order, so four more recluster('n', 'id') calls each
returned 0 and the file "plateaued" with reuse disabled; and free_space is compared with
its count after the first cycle, since "> 0" held with nothing reused.

compact_rewrite() and maintenance_due() refuse NaN, negative, above-1 and (for
maintenance_due) NULL thresholds with 22023 and accept the boundaries 0.0 and 1.0, with
controls pinning that other failures carry other SQLSTATEs; and max_groups caps one
compact_rewrite call, which returns what it rewrote.
"""
import psycopg

GEN = "SELECT g, g AS v, md5(g::text) AS payload FROM generate_series(1, 6000) g"
FREE = "SELECT count(*) FROM pgcolumnar.free_space WHERE storage_id = pgcolumnar.get_storage_id('n')"


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def _rows(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def _sqlstate(cur, sql):
    """-> 'OK', or the SQLSTATE the statement raised; never 'OK' for a failure."""
    try:
        cur.execute(sql)
        return "OK"
    except psycopg.Error as exc:
        return exc.sqlstate or f"ERR_NO_SQLSTATE ({exc})"


def test_repeated_recluster_reuses_space_and_compact_rewrite_is_guarded(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE h (id int, v int, payload text)")
        cur.execute("CREATE TABLE n (id int, v int, payload text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
        cur.execute(f"INSERT INTO h {GEN}")
        cur.execute(f"INSERT INTO n {GEN}")
        size1 = _one(cur, "SELECT pg_relation_size('n')")
        expect.num(_one(cur, "SELECT count(*) FROM n"), 6000, "initial data present")
        expect.num(_one(cur, FREE), 0, "no free space yet")

        # the first recluster appends, so the file grows and free_space fills
        cur.execute("SELECT pgcolumnar.recluster('n', 'id')")
        size2, free2 = _one(cur, "SELECT pg_relation_size('n')"), _one(cur, FREE)
        expect.text("yes" if free2 > 0 else "no", "yes", "free space recorded after first recluster")
        expect.num(_one(cur, "SELECT count(*) FROM n"), 6000, "data intact after first recluster")

        recs = [_one(cur, f"SELECT pgcolumnar.recluster('n', '{key}')") for key in ("v", "id", "v", "id")]
        size3, free3 = _one(cur, "SELECT pg_relation_size('n')"), _one(cur, FREE)
        print(f"-- file size: initial={size1} after-1={size2} after-5={size3}; free_space rows {free2} -> {free3}")
        expect.text("yes" if all(r > 0 for r in recs) else f"no ({recs})", "yes",
                    "each repeated recluster rewrote the table, so none was skipped as already ordered")
        expect.text("yes" if size3 <= size2 + size1 else f"no ({size2} -> {size3})", "yes",
                    "repeated recluster reuses space (file plateaus)")
        expect.text("yes" if 0 < free3 <= free2 else f"no ({free2} -> {free3} rows)", "yes",
                    "free space is being reused (bounded, not growing per cycle)")
        expect.row_set(_rows(cur, "SELECT id, v, payload FROM n"), _rows(cur, "SELECT id, v, payload FROM h"),
                       "reused-block data matches heap mirror")

        cur.execute("DELETE FROM h WHERE id % 4 = 0")
        cur.execute("DELETE FROM n WHERE id % 4 = 0")
        # Controls: each is what an "it errored" assertion cannot tell from the guard.
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.no_such_function(1)"), "42883",
                    "control: a missing function is 42883, not the guard's 22023")
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.compact_rewrite('n', 0.5, 1, 'x')"), "42883",
                    "control: a wrong-arity compact_rewrite is 42883, not 22023")
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.compact_rewrite(NULL, 0.5)"), "22004",
                    "control: a null table name is 22004, not 22023")
        expect.text(_sqlstate(cur, "SELECT 1/0"), "22012", "control: division by zero is 22012, not 22023")
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.compact_rewrite('n', 'NaN'::float8)"), "22023",
                    "compact_rewrite rejects a NaN threshold with 22023")
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.compact_rewrite('n', 2.0)"), "22023",
                    "compact_rewrite rejects a threshold above 1 with 22023")
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.compact_rewrite('n', -1.0)"), "22023",
                    "compact_rewrite rejects a negative threshold with 22023")
        # the other direction: 0.0 and 1.0 sit on the boundary and are legal
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.compact_rewrite('n', 0.0)"), "OK",
                    "compact_rewrite ACCEPTS the boundary threshold 0.0")
        expect.text(_sqlstate(cur, "SELECT pgcolumnar.compact_rewrite('n', 1.0)"), "OK",
                    "compact_rewrite ACCEPTS the boundary threshold 1.0")

        cur.execute("SELECT pgcolumnar.recluster('n', 'id')")
        expect.row_set(_rows(cur, "SELECT id, v, payload FROM n"), _rows(cur, "SELECT id, v, payload FROM h"),
                       "data correct after delete + compact_rewrite + recluster")
        expect.num(_one(cur, "SELECT count(*) FROM n"), _one(cur, "SELECT count(*) FROM h"), "row count correct after cycle")


def test_maintenance_due_validates_both_thresholds(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE nd (id int, v int, payload text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('nd', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
        cur.execute("INSERT INTO nd SELECT g, g, md5(g::text) FROM generate_series(1, 20000) g")
        cur.execute("DELETE FROM nd WHERE id % 2 = 0")
        # every verdict below is about a table that is 50% deleted
        total, deleted = _rows(cur, "SELECT total_rows, deleted_rows FROM pgcolumnar.maintenance_due('nd', 0.2, 0.05)")[0]
        expect.num(total, 20000, "premise: nd holds 20000 rows")
        expect.num(deleted, 10000, "premise: 10000 of nd's rows are deleted")
        due = "SELECT compact_rewrite_due FROM pgcolumnar.maintenance_due('nd', {}, 0.05)"
        expect.text(str(_one(cur, due.format(0.2))), "True", "maintenance_due(0.2) on a 50%-deleted table is still due")
        expect.text(str(_one(cur, due.format(0.6))), "False", "maintenance_due(0.6) on a 50%-deleted table is not due")

        md = "SELECT * FROM pgcolumnar.maintenance_due('nd', {}, {})"
        for args, name in (
            (("'NaN'::float8", "0.05"), "maintenance_due rejects a NaN compact threshold with 22023"),
            (("2.0", "0.05"), "maintenance_due rejects a compact threshold above 1 with 22023"),
            (("-1.0", "0.05"), "maintenance_due rejects a negative compact threshold with 22023"),
            (("NULL::float8", "0.05"), "maintenance_due rejects a NULL compact threshold with 22023"),
            (("0.2", "'NaN'::float8"), "maintenance_due rejects a NaN recluster threshold with 22023"),
            (("0.2", "2.0"), "maintenance_due rejects a recluster threshold above 1 with 22023"),
            (("0.2", "-1.0"), "maintenance_due rejects a negative recluster threshold with 22023"),
            (("0.2", "NULL::float8"), "maintenance_due rejects a NULL recluster threshold with 22023"),
        ):
            expect.text(_sqlstate(cur, md.format(*args)), "22023", name)
        # not over-broad: the boundaries are legal on both, and the defaults resolve
        expect.text(_sqlstate(cur, md.format("0.0", "0.0")), "OK", "maintenance_due ACCEPTS the boundary thresholds 0.0, 0.0")
        expect.text(_sqlstate(cur, md.format("1.0", "1.0")), "OK", "maintenance_due ACCEPTS the boundary thresholds 1.0, 1.0")
        expect.text(_sqlstate(cur, "SELECT * FROM pgcolumnar.maintenance_due('nd')"), "OK",
                    "maintenance_due ACCEPTS its own defaults")


def test_max_groups_caps_one_compact_rewrite(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ncap (id int, v text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('ncap', stripe_row_limit => 1000)")
        cur.execute("INSERT INTO ncap SELECT g, 'v'||g FROM generate_series(1, 10000) g")
        cur.execute("DELETE FROM ncap WHERE id % 2 = 0")
        # ten groups only if stripe_row_limit is honoured, and each half deleted
        groups = _one(cur, "SELECT count(*) || '|' || count(*) FILTER (WHERE g.row_count - coalesce(d.deleted_count, 0) = 500 "
                           "AND coalesce(d.deleted_count, 0) = 500) FROM pgcolumnar.row_group g "
                           "LEFT JOIN pgcolumnar.delete_vector d ON d.storage_id = g.storage_id "
                           "AND d.group_number = g.group_number WHERE g.storage_id = pgcolumnar.get_storage_id('ncap')")
        expect.text(f"{groups}|{_one(cur, 'SELECT count(*) FROM ncap')}", "10|10|5000",
                    "ten row groups, every one half deleted")
        calls = [_one(cur, f"SELECT pgcolumnar.compact_rewrite('ncap', 0.1, {cap})") for cap in (3, 0, 0)]
        expect.text("|".join(str(c) for c in calls), "3|7|0",
                    "compact_rewrite with max_groups 3 rewrites three, then an uncapped call the other seven")
        expect.text(_one(cur, "SELECT count(*) || '|' || count(DISTINCT id) || '|' || sum(id) FROM ncap"),
                    "5000|5000|25000000", "and every live row is still there, once")

"""A statement does not see the rows it writes, even after it has flushed some of
them into row groups part-way through (#1341). The pytest twin of
`test/statement_self_visibility.sh`, every check by the shell's name.

A scan reads its row-group list once, when it starts, so groups its own statement
flushes later are not in it. A reader that picked them up would make
INSERT INTO t SELECT FROM t feed on its own output, and UPDATE t reach a row twice.

Each shape runs on a columnar table and on heap, and heap is the answer. The
columnar table writes row groups of 1000 rows, so every statement crosses several
flushes. Every statement runs under a statement_timeout, because a scan that fed on
its own output would otherwise never end. Both tables run every statement, so a
columnar failure cannot leave heap untouched and the comparison vacuous.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

AMS = ("pgcolumnar", "heap")


def _session(cur):
    cur.execute("SET pgcolumnar.stripe_row_limit = 1000")
    cur.execute("SET statement_timeout = '60s'")


def _run(conn, sql):
    """Run `sql` (with %t for the table) on both tables -> 'ok' or the errors."""
    errs = []
    for am in AMS:
        try:
            with conn.cursor() as cur:
                _session(cur)
                cur.execute(sql.replace("%t", f"sv_{am}"))
        except psycopg.Error as exc:
            errs.append(f"{am}: {str(exc).splitlines()[0]}")
            conn.execute("ROLLBACK")  # a failed BEGIN ... COMMIT leaves the session in it
    return " ".join(errs) or "ok"


def _answer(conn, sql, am):
    """The row the query returns, or its error's first line if it failed."""
    try:
        with conn.cursor() as cur:
            _session(cur)
            cur.execute(sql.replace("%t", f"sv_{am}"))
            return cur.fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _same(conn, sql):
    c, h = (_answer(conn, sql, am) for am in AMS)
    return "same" if c == h else f"columnar={c} heap={h}"


def _groups(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pgcolumnar.row_group "
                    "WHERE storage_id = pgcolumnar.get_storage_id('sv_pgcolumnar')")
        return cur.fetchone()[0]


def _fresh(conn):
    """Both tables made again, holding rows 1..5000. Every shape starts here, so a
    shape that goes wrong cannot fail the ones after it."""
    with conn.cursor() as cur:
        _session(cur)
        for am in AMS:
            cur.execute(f"DROP TABLE IF EXISTS sv_{am}")
            cur.execute(f"CREATE TABLE sv_{am} (id int, v int) USING {am}")
            cur.execute(f"INSERT INTO sv_{am} SELECT g, 0 FROM generate_series(1, 5000) g")


def test_a_statement_does_not_see_its_own_writes(pgc_conn, expect):
    _fresh(pgc_conn)
    before = _groups(pgc_conn)

    expect.text(_run(pgc_conn, "INSERT INTO %t SELECT id + 5000, v FROM %t"), "ok",
                "INSERT INTO t SELECT FROM t runs to completion")
    after = _groups(pgc_conn)
    expect.text("yes" if after - before >= 2 else f"no ({before} groups before, {after} after)", "yes",
                "the INSERT flushed row groups while its scan was still reading")
    expect.text(_same(pgc_conn, "SELECT count(*), count(DISTINCT id), max(id) FROM %t"), "same",
                "and it read only the rows that were there when it started, as heap does")

    _fresh(pgc_conn)
    expect.text(_run(pgc_conn, "UPDATE %t SET v = v + 1"), "ok",
                "UPDATE of every row runs to completion")
    expect.text(_same(pgc_conn, "SELECT count(*), min(v), max(v) FROM %t"), "same",
                "and it changed each row exactly once, as heap does")

    _fresh(pgc_conn)
    expect.text(_run(pgc_conn, "BEGIN; INSERT INTO %t SELECT g, 0 FROM generate_series(20001, 23000) g; "
                               "UPDATE %t SET v = v + 1; INSERT INTO %t SELECT id + 100000, v FROM %t; COMMIT"),
                "ok",
                "in one transaction, an INSERT, an UPDATE of every row and an INSERT SELECT run to completion")
    expect.text(_same(pgc_conn, "SELECT count(*), count(DISTINCT id), min(v), max(v), sum(v) FROM %t"), "same",
                "and each statement saw the earlier ones but not itself, as heap does")

    # No WHERE on the scan: every row the INSERT writes would pass it, so a scan
    # that fed on its own output would meet those rows.
    _fresh(pgc_conn)
    expect.text(_same(pgc_conn, "WITH w AS (INSERT INTO %t SELECT id + 1000000, v FROM %t "
                                "RETURNING 1) SELECT count(*) FROM w"), "same",
                "an INSERT in a WITH clause reading its own table returns the rows it inserted, as heap does")
    expect.text(_same(pgc_conn, "SELECT count(*) FROM %t"), "same",
                "and the table holds them once, as heap does")


# A self-join UPDATE, DELETE or INSERT SELECT reads its own table on the inner side
# of a join, or in a subquery, while it writes (#2247). Heap's inner scan sees the
# table as it was when the statement started. A columnar index fetch used to see
# the statement's own new row versions, from the write buffer or from row groups
# the statement flushed, and to lose the old versions it had marked deleted. Heap
# is the answer. The pytest twin of the shell section of the same name, every check
# by the shell's name, one test per check so that each one reports on its own. Each
# shape runs in a transaction that is rolled back.
NL = ["SET LOCAL enable_hashjoin = off", "SET LOCAL enable_mergejoin = off"]
IDX = ["SET LOCAL enable_seqscan = off", "SET LOCAL enable_bitmapscan = off"]
NOIDX = ["SET LOCAL enable_indexscan = off", "SET LOCAL enable_indexonlyscan = off",
         "SET LOCAL enable_bitmapscan = off"]
PAR = ["SET LOCAL max_parallel_workers_per_gather = 2", "SET LOCAL parallel_setup_cost = 0",
       "SET LOCAL parallel_tuple_cost = 0", "SET LOCAL min_parallel_table_scan_size = 0"] + NOIDX
UPD = "UPDATE %t SET pad = t2.pad + 1000000 FROM %t t2 WHERE t2.id = %t.id - 1 AND %t.id <= 10"
SHOW = "SELECT string_agg(id || ':' || pad, ' ' ORDER BY id) FROM %t WHERE id <= 11"
BIG = ["UPDATE %t SET pad = t2.pad + 1000000 FROM %t t2 WHERE t2.id = %t.id - 1 AND %t.id <= 3000",
       "SELECT count(*) FILTER (WHERE pad <> id + 999999), min(pad), max(pad) FROM %t WHERE id BETWEEN 2 AND 3000"]
SUBQ = "UPDATE %t SET pad = (SELECT t2.pad FROM %t t2 WHERE t2.id = %t.id - 1) + 1000000 WHERE %t.id BETWEEN 2 AND 10"
PARW = ["UPDATE %t SET pad = pad + 1 WHERE id % 3 = 0",
        "INSERT INTO %t SELECT g, g FROM generate_series(40001, 50000) g"]


def _cs(on):
    return [f"SET LOCAL pgcolumnar.enable_custom_scan = {on}"]


CSOFF = _cs("off")


def _fresh_pk(conn):
    """Both tables, (id PRIMARY KEY, pad) holding pad = id for 1..40000, with a
    covering index and a VOLATILE function that counts the rows above 100000."""
    with conn.cursor() as cur:
        _session(cur)
        for am in AMS:
            cur.execute(f"DROP TABLE IF EXISTS sv_{am} CASCADE")
            cur.execute(f"DROP FUNCTION IF EXISTS seen_sv_{am}()")
            cur.execute(f"CREATE TABLE sv_{am} (id int PRIMARY KEY, pad bigint) USING {am}")
            cur.execute(f"INSERT INTO sv_{am} SELECT g, g FROM generate_series(1, 40000) g")
            cur.execute(f"CREATE UNIQUE INDEX sv_{am}_cover ON sv_{am} (id) INCLUDE (pad)")
            cur.execute(f"CREATE FUNCTION seen_sv_{am}() RETURNS bigint VOLATILE LANGUAGE sql "
                        f"AS 'SELECT count(*) FROM sv_{am} WHERE id > 100000'")
            cur.execute(f"DROP FUNCTION IF EXISTS sib_sv_{am}()")
            cur.execute(f"CREATE FUNCTION sib_sv_{am}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                        f"BEGIN INSERT INTO sv_{am} VALUES (-NEW.id, "
                        f"(SELECT count(*) FROM sv_{am} WHERE id > 100000) * 1000 "
                        f"+ (SELECT count(*) FROM sv_{am} WHERE id < 0)); RETURN NEW; END $$")
            cur.execute(f"VACUUM ANALYZE sv_{am}")
        cur.execute("CREATE OR REPLACE FUNCTION sv_try(q text) RETURNS text LANGUAGE plpgsql AS $$ "
                    "BEGIN EXECUTE q; RETURN 'ok'; "
                    "EXCEPTION WHEN unique_violation THEN RETURN 'unique_violation'; END $$")


def _tx(conn, am, statements, fetch="one"):
    """Run `statements` (with %t for the table) in one transaction on sv_<am>, roll
    it back, and return the last statement's row (or all its rows), or the error's
    first line."""
    try:
        with conn.cursor() as cur:
            _session(cur)
            cur.execute("BEGIN")
            for sql in statements:
                # With no parameters psycopg sends the text as is, so % needs no escape.
                cur.execute(sql.replace("%t", f"sv_{am}"))
            out = cur.fetchone() if fetch == "one" else cur.fetchall()
    except psycopg.Error as exc:
        out = query_error(str(exc).splitlines()[0])
    conn.execute("ROLLBACK")
    return out


def _same_tx(conn, statements):
    c, h = (_tx(conn, am, statements) for am in AMS)
    return "same" if c == h else f"columnar={c} heap={h}"


def _error_of(conn, statements):
    """The first line of the error `statements` raise on the columnar table, run in
    a transaction that is rolled back, or 'no error'."""
    try:
        with conn.cursor() as cur:
            _session(cur)
            cur.execute("BEGIN")
            for sql in statements:
                cur.execute(sql.replace("%t", "sv_pgcolumnar"))
            out = "no error"
    except psycopg.Error as exc:
        out = str(exc).splitlines()[0]
    conn.execute("ROLLBACK")
    return out


def _plan_has(conn, statements, pattern):
    import re
    rows = _tx(conn, "pgcolumnar", statements, fetch="all")
    text = " ".join(r[0] for r in rows) if isinstance(rows, list) else str(rows)
    return "yes" if re.search(pattern, text) else f"no: {text[:300]}"



MJ = CSOFF + ["SET LOCAL enable_hashjoin = off", "SET LOCAL enable_nestloop = off"]


@pytest.mark.parametrize("cs", ("off", "on"))
def test_premise_the_self_join_runs_as_a_nested_loop(pgc_conn, expect, cs):
    _fresh_pk(pgc_conn)
    expect.text(_plan_has(pgc_conn, _cs(cs) + NL + IDX + ["EXPLAIN (COSTS OFF) " + UPD],
                          "Nested Loop"), "yes",
                f'premise: the self-join UPDATE runs as a nested loop over index scans (custom scan {cs})')


@pytest.mark.parametrize("cs", ("off", "on"))
def test_a_self_join_update_through_index_scans(pgc_conn, expect, cs):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, _cs(cs) + NL + IDX + [UPD, SHOW]), "same",
                f"a self-join UPDATE through index scans reads each row's old value, as heap does (custom scan {cs})")


def test_premise_the_self_join_runs_as_a_merge_join(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_plan_has(pgc_conn, MJ + ["EXPLAIN (COSTS OFF) " + UPD],
                          "Merge Join"), "yes",
                'premise: the self-join UPDATE runs as a merge join')


def test_a_self_join_update_through_a_merge_join(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, MJ + [UPD, SHOW]), "same",
                "a self-join UPDATE through a merge join reads each row's old value, as heap does")


def test_a_self_join_delete_through_index_scans(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + IDX + ["DELETE FROM %t USING %t t2 WHERE t2.id = %t.id - 1 AND %t.id <= 10",
                         "SELECT count(*), string_agg(id::text, ',' ORDER BY id) FROM %t WHERE id <= 11"]), "same",
                'a self-join DELETE through index scans deletes the rows heap deletes')


# Bounded by id < 200000, so a scan that fed on its own output stops after one more
# pass instead of running until the statement_timeout.
@pytest.mark.parametrize("limit", (150000, 1000))
def test_an_insert_select_through_an_index_range_scan(pgc_conn, expect, limit):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, ["SET LOCAL statement_timeout = '20s'", f"SET LOCAL pgcolumnar.stripe_row_limit = {limit}"] + CSOFF + IDX + [
        "INSERT INTO %t SELECT id + 100000, pad FROM %t WHERE id > 30000 AND id < 200000",
        "SELECT count(*), max(id) FROM %t"]), "same",
                f'an INSERT SELECT from its own table through an index range scan inserts each row once, as heap does (stripe_row_limit {limit})')


def test_a_correlated_subquery_through_an_index_scan(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + IDX + ["SET LOCAL enable_indexonlyscan = off", SUBQ, SHOW]), "same",
                'an UPDATE with a correlated subquery through an index scan reads the old value, as heap does')


def test_premise_the_subquery_runs_as_an_index_only_scan(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_plan_has(pgc_conn, CSOFF + IDX + ["EXPLAIN (COSTS OFF) " + SUBQ],
                          "Index Only Scan"), "yes",
                'premise: the correlated subquery runs as an index-only scan')


def test_a_correlated_subquery_through_an_index_only_scan(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + IDX + [SUBQ, SHOW]), "same",
                'an UPDATE with a correlated subquery through an index-only scan reads the old value, as heap does')


def test_a_self_join_chained_across_row_groups(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + IDX + [
        "UPDATE %t SET pad = t2.pad + 1000000 FROM %t t2 WHERE t2.id = %t.id - 10000 AND %t.id % 10000 = 5",
        "SELECT string_agg(id || ':' || pad, ' ' ORDER BY id) FROM %t WHERE id % 10000 = 5"]), "same",
                "a self-join UPDATE chained across row groups reads each row's old value, as heap does")


def test_a_big_self_join_flushed_part_way(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + IDX + BIG), "same",
                "a 3000-row self-join UPDATE through index scans reads each row's old value, with row groups flushed part-way, as heap does")


def test_a_big_self_join_fully_buffered(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, ["SET LOCAL pgcolumnar.stripe_row_limit = 150000"] + CSOFF + NL + IDX + BIG), "same",
                "a 3000-row self-join UPDATE through index scans reads each row's old value, with every new row still buffered, as heap does")


def test_a_self_join_after_an_earlier_update(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + IDX + ["UPDATE %t SET pad = pad + 7 WHERE id <= 10", UPD, SHOW]), "same",
                'after an earlier UPDATE in the same transaction, a self-join UPDATE sees that UPDATE but not its own rows, as heap does')


def test_a_target_row_joined_twice(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + IDX + ["UPDATE %t SET pad = pad + 1 FROM (VALUES (1), (1), (2)) v(x) WHERE %t.id = v.x",
                         "SELECT string_agg(id || ':' || pad, ' ' ORDER BY id) FROM %t WHERE id <= 3"]), "same",
                'an UPDATE that joins the same target row twice through an index changes it once, as heap does')


def test_a_cursor_through_an_index_scan(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + IDX + ["DECLARE c CURSOR FOR SELECT sum(pad) FROM %t WHERE id <= 10",
                    "UPDATE %t SET pad = 0 WHERE id <= 10", "FETCH ALL FROM c"]), "same",
                'a cursor fetched after a later UPDATE reads the old values through an index scan, as heap does')


# A BEFORE ROW trigger that inserts into its own table writes under a new command
# for each outer row, so the write buffer holds runs of rows from different
# commands. Each trigger call must see the outer rows and its own earlier rows,
# but not the row it is writing.
def test_a_before_row_trigger_sees_earlier_rows(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + IDX + [
        "CREATE TRIGGER sib BEFORE INSERT ON %t FOR EACH ROW WHEN (NEW.id > 0) EXECUTE FUNCTION sib_%t()",
        "INSERT INTO %t SELECT 100000 + g, 0 FROM generate_series(1, 5) g",
        "SELECT string_agg(pad::text, ',' ORDER BY id DESC) FROM %t WHERE id < 0"]), "same",
                "a BEFORE ROW trigger inserting into its own table sees the statement's earlier rows and its own earlier rows, as heap does")


# Shapes that must keep working (#2247 guards): a plan that reads the inner side
# once, a nested read of earlier rows, and a parallel scan after this transaction's
# flushed writes. All match heap before the fix; the fix must not break them.
def test_guard_a_function_sees_the_statement_s_earlier_rows(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + ["INSERT INTO %t SELECT g, seen_%t() FROM generate_series(100001, 100005) g",
             "SELECT string_agg(pad::text, ',' ORDER BY id) FROM %t WHERE id > 100000"]), "same",
                "a VOLATILE function that reads the table inside an INSERT on it sees the statement's earlier rows, as heap does")


def test_guard_a_hash_join(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + ["SET LOCAL enable_mergejoin = off", "SET LOCAL enable_nestloop = off", UPD, SHOW]), "same",
                "a self-join UPDATE through a hash join reads each row's old value, as heap does")


def test_guard_a_materialized_inner_scan(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + NOIDX + [UPD, SHOW]), "same",
                "a self-join UPDATE through a materialized sequential inner scan reads each row's old value, as heap does")


def test_guard_an_insert_select_by_sequential_scan(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + ["INSERT INTO %t SELECT id + 100000, pad FROM %t", "SELECT count(*), max(id) FROM %t"]), "same",
                'an INSERT SELECT from its own table through a sequential scan inserts each row once, as heap does')


# Snapshots that are not MVCC see every write: the unique check's dirty snapshot
# (#212) and the AFTER trigger's re-fetch under SnapshotAny (#179).
def test_guard_a_duplicate_key_in_one_statement(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + [
        "SELECT sv_try('INSERT INTO %t VALUES (900001, 1), (900001, 2)')"]), "same",
                "a duplicate key inserted twice in one statement is still refused, as heap does")


def test_guard_an_after_row_trigger_reads_old_and_new(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + IDX + [
        "CREATE TEMP TABLE trig_log (o bigint, n bigint)",
        "CREATE FUNCTION pg_temp.trig_note() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN INSERT INTO trig_log VALUES (OLD.pad, NEW.pad); RETURN NULL; END $$",
        "CREATE TRIGGER au AFTER UPDATE ON %t FOR EACH ROW EXECUTE FUNCTION pg_temp.trig_note()",
        UPD, "SELECT count(*), sum(o), sum(n) FROM trig_log"]), "same",
                "an AFTER ROW trigger of a self-join UPDATE still reads OLD and NEW, as heap does")


@pytest.mark.parametrize("cs", ("off", "on"))
def test_guard_a_parallel_worker_refuses_a_leader_it_could_not_hide(pgc_conn, expect, cs):
    _fresh_pk(pgc_conn)
    expect.text(_error_of(pgc_conn, _cs(cs) + ["SET LOCAL pgcolumnar.debug_registry_cid_skew = 1"] + PAR + [
        "SELECT count(*), sum(pad) FROM %t"]),
                "columnar: a parallel worker cannot hide this transaction's own writes",
                f"a parallel worker refuses a leader whose own writes it could not hide (custom scan {cs})")


@pytest.mark.parametrize("cs", ("off", "on"))
def test_guard_premise_the_count_runs_in_parallel_workers(pgc_conn, expect, cs):
    _fresh_pk(pgc_conn)
    expect.text(_plan_has(pgc_conn, _cs(cs) + PARW + PAR + [
        "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*), sum(pad) FROM %t"],
                          r"Workers Launched: [1-9]"), "yes",
                f"premise: the count after this transaction's writes runs in parallel workers (custom scan {cs})")


@pytest.mark.parametrize("cs", ("off", "on"))
def test_guard_a_parallel_scan_after_flushed_writes(pgc_conn, expect, cs):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, _cs(cs) + PARW + PAR + ["SELECT count(*), sum(pad) FROM %t"]), "same",
                f"a parallel scan after this transaction's flushed writes sees them, as heap does (custom scan {cs})")

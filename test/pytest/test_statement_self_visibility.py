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
            # the review of #2269: u for ON CONFLICT, r with a plain index for R4 and R5
            cur.execute(f"DROP TABLE IF EXISTS sv_{am}_u, sv_{am}_r CASCADE")
            cur.execute(f"CREATE TABLE sv_{am}_u (id int PRIMARY KEY) USING {am}")
            cur.execute(f"CREATE TABLE sv_{am}_r (id int, pad int) USING {am}")
            cur.execute(f"INSERT INTO sv_{am}_r SELECT g, g FROM generate_series(1, 3000) g")
            cur.execute(f"CREATE INDEX ON sv_{am}_r (id)")
            cur.execute(f"CREATE OR REPLACE FUNCTION hr_sv_{am}_r() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                        f"SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; "
                        f"SET LOCAL pgcolumnar.enable_custom_scan = off; "
                        f"INSERT INTO sv_{am}_r SELECT id + 3000, 0 FROM sv_{am}_r WHERE id BETWEEN 1 AND 20000; "
                        f"RETURN NEW; END $$")
            cur.execute(f"VACUUM ANALYZE sv_{am}_r")
        cur.execute("CREATE OR REPLACE FUNCTION sv_try(q text) RETURNS text LANGUAGE plpgsql AS $$ "
                    "BEGIN EXECUTE q; RETURN 'ok'; "
                    "EXCEPTION WHEN unique_violation THEN RETURN 'unique_violation'; END $$")
        cur.execute("CREATE OR REPLACE FUNCTION cnt_idx(t regclass) RETURNS bigint STABLE LANGUAGE plpgsql AS $$ "
                    "DECLARE n bigint; BEGIN EXECUTE format('SELECT count(pad) FROM %s WHERE id BETWEEN 1 AND 30000', t) "
                    "INTO n; RETURN n; END $$")
        cur.execute("CREATE OR REPLACE FUNCTION cnt_seq(t regclass, c text) RETURNS bigint STABLE LANGUAGE plpgsql AS $$ "
                    "DECLARE n bigint; BEGIN EXECUTE format('SELECT count(*) FROM %s WHERE %I %% 7 <> 3', t, c) "
                    "INTO n; RETURN n; END $$")


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


# A function that reads the table runs a statement of its own, and that statement
# writes the outer statement's buffered rows and delete marks to the catalog. The
# outer statement's index fetches must still not see them.
def test_a_self_join_calling_a_function_that_reads_the_table(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, CSOFF + NL + IDX + [
        "UPDATE %t SET pad = t2.pad + 1000000 + 0 * seen_%t() FROM %t t2 WHERE t2.id = %t.id - 1 AND %t.id <= 10",
        SHOW]), "same",
                "a self-join UPDATE that calls a function reading the table still reads each row's old value, as heap does")


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


# The review of #2269: each probe is the reviewer's SQL, on this module's tables: t
# is the main table, r a table with a plain index holding 1..3000, u an empty
# table with a primary key.
PROBE = ["SET LOCAL enable_seqscan = off", "SET LOCAL enable_bitmapscan = off",
         "SET LOCAL pgcolumnar.enable_custom_scan = off"]


def _raw(conn, am, statements):
    """Run `statements` (with %t for the table) as given, in autocommit, and return
    the last row a statement returned, or the error's first line."""
    out = None
    try:
        with conn.cursor() as cur:
            _session(cur)
            for sql in statements:
                cur.execute(sql.replace("%t", f"sv_{am}"))
                if cur.description is not None:
                    out = cur.fetchone()
    except psycopg.Error as exc:
        out = query_error(str(exc).splitlines()[0])
        conn.execute("ROLLBACK")
    return out


def test_a_mark_under_a_rolled_back_savepoint_is_forgotten(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, PROBE + [
        "SAVEPOINT a", "DELETE FROM %t WHERE id = 5", "ROLLBACK TO a",
        "DECLARE c CURSOR FOR SELECT id || '|' || pad FROM %t WHERE id = 5",
        "DELETE FROM %t WHERE id = 5", "FETCH c"]), "same",
                "a delete mark made under a savepoint that rolled back is forgotten, as heap does")


def test_a_mark_under_a_released_savepoint_goes_with_its_parent(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, PROBE + [
        "SAVEPOINT o", "SAVEPOINT i", "DELETE FROM %t WHERE id = 5", "RELEASE i", "ROLLBACK TO o",
        "DECLARE c CURSOR FOR SELECT id || '|' || pad FROM %t WHERE id = 5",
        "DELETE FROM %t WHERE id = 5", "FETCH c"]), "same",
                "a delete mark made under a released savepoint goes with its parent's rollback, as heap does")


@pytest.mark.parametrize("iso", ("REPEATABLE READ", "SERIALIZABLE"))
def test_on_conflict_do_nothing_with_a_duplicate_in_one_statement(pgc_conn, expect, iso):
    _fresh_pk(pgc_conn)
    stmts = ["TRUNCATE %t_u", f"BEGIN ISOLATION LEVEL {iso}",
             "INSERT INTO %t_u VALUES (1), (1) ON CONFLICT DO NOTHING",
             "SELECT count(*) FROM %t_u", "COMMIT"]
    c, h = (_raw(pgc_conn, am, stmts) for am in AMS)
    expect.text("same" if c == h else f"columnar={c} heap={h}", "same",
                f"ON CONFLICT DO NOTHING with a duplicate in one statement keeps one row at {iso}, as heap does")


def test_the_first_row_flushed_after_an_earlier_run_is_hidden(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, PROBE + [
        "SET LOCAL pgcolumnar.stripe_row_limit = 1000",
        "INSERT INTO %t_r SELECT -g, 0 FROM generate_series(1, 1000) g",
        "INSERT INTO %t_r SELECT id + 3000, 0 FROM %t_r WHERE id BETWEEN -5000 AND 100000",
        "SELECT count(*) || '|' || max(id) FROM %t_r"]), "same",
                "the first row a statement flushes, just after an earlier statement's run, is hidden from it, as heap does")


def test_a_trigger_does_not_read_its_own_rows_in_the_outer_buffer(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, PROBE + [
        "CREATE TRIGGER hr BEFORE INSERT ON %t_r FOR EACH ROW WHEN (NEW.id = 100002) EXECUTE FUNCTION hr_%t_r()",
        "INSERT INTO %t_r VALUES (100001, 0), (100002, 0)",
        "SELECT count(*) || '|' || max(id) FROM %t_r WHERE id < 100000"]), "same",
                "a trigger's INSERT does not read rows it wrote into the outer statement's buffer, as heap does")


# A STABLE function's query runs under the caller's snapshot and may get a
# parallel plan, so the workers must hide the caller's writes as the leader does.
# Through an index the answer is heap's. Through a sequential scan the answer is
# still wrong (#2267), but it must be the same in parallel as serially, and no
# error.
PARIDX = CSOFF + ["SET LOCAL max_parallel_workers_per_gather = 2", "SET LOCAL parallel_setup_cost = 0",
                  "SET LOCAL parallel_tuple_cost = 0", "SET LOCAL min_parallel_index_scan_size = 0",
                  "SET LOCAL min_parallel_table_scan_size = 0", "SET LOCAL enable_seqscan = off",
                  "SET LOCAL enable_bitmapscan = off", "SET LOCAL enable_indexonlyscan = off"]


def test_premise_the_function_index_query_runs_in_parallel(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_plan_has(pgc_conn, PARIDX + [
        "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(pad) FROM %t WHERE id BETWEEN 1 AND 30000"],
        r"Workers Launched: [1-9]"), "yes",
                "premise: the function's index query runs in parallel workers")


def test_a_stable_function_parallel_index_scan_inside_an_update(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, PARIDX + [
        "UPDATE %t SET id = id + 1000000, pad = cnt_idx('%t') WHERE id % 1000 = 1",
        "SELECT count(*) || '|' || min(pad) || '|' || max(pad) FROM %t WHERE id > 1000000"]), "same",
                "a STABLE function's parallel index scan inside an UPDATE sees the rows as they were, as heap does")


def test_a_stable_function_parallel_index_scan_inside_a_cte_delete(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, PARIDX + [
        "WITH d AS (DELETE FROM %t WHERE id % 1000 = 1 AND cnt_idx('%t') > 0 RETURNING cnt_idx('%t') AS n) "
        "SELECT count(*) || '|' || min(n) || '|' || max(n) FROM d"]), "same",
                "a STABLE function's parallel index scan inside a DELETE in a WITH clause sees the rows as they were, as heap does")


def test_a_stable_function_parallel_index_scan_in_a_cursor(pgc_conn, expect):
    _fresh_pk(pgc_conn)
    expect.text(_same_tx(pgc_conn, PARIDX + [
        "DECLARE c CURSOR FOR SELECT cnt_idx('%t')", "DELETE FROM %t WHERE id % 30 = 0", "FETCH ALL FROM c"]), "same",
                "a STABLE function's parallel index scan in a cursor does not see a later DELETE, as heap does")


# The harness cluster sets no workers per Gather, so each variant asks for
# PostgreSQL's default of 2.
PAR0 = ["SET LOCAL max_parallel_workers_per_gather = 2", "SET LOCAL parallel_setup_cost = 0",
        "SET LOCAL parallel_tuple_cost = 0", "SET LOCAL min_parallel_table_scan_size = 0"]
PARDEF = ["SET LOCAL max_parallel_workers_per_gather = 2"]
SEQ_SETTINGS = {"parallel costs at 0": ("sv_pgcolumnar", PAR0, "pad"),
                "default parallel settings": ("sv_big", PARDEF, "v")}


def _seq_setting(conn, setting):
    table, settings, col = SEQ_SETTINGS[setting]
    _fresh_pk(conn)
    if table == "sv_big":
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS sv_big")
            cur.execute("CREATE TABLE sv_big (id int, v int) USING pgcolumnar")
            cur.execute("INSERT INTO sv_big SELECT g, g FROM generate_series(1, 3000000) g")
            cur.execute("ANALYZE sv_big")
    return table, settings, col


def _par_vs_serial(conn, table, settings, statements):
    def run(extra):
        stmts = ["SET LOCAL pgcolumnar.enable_custom_scan = on"] + extra + [
            x.replace("%t", table) for x in statements]
        return _tx(conn, "pgcolumnar", stmts)
    a = run(settings)
    b = run(["SET LOCAL max_parallel_workers_per_gather = 0"])
    return "same" if a == b else f"parallel={a} serial={b}"


@pytest.mark.parametrize("setting", tuple(SEQ_SETTINGS))
def test_premise_the_function_sequential_query_runs_in_parallel(pgc_conn, expect, setting):
    table, settings, col = _seq_setting(pgc_conn, setting)
    expect.text(_plan_has(pgc_conn, ["SET LOCAL pgcolumnar.enable_custom_scan = on"] + settings + [
        f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM {table} WHERE {col} % 7 <> 3"],
        r"Workers Launched: [1-9]"), "yes",
                f"premise: the function's sequential query runs in parallel workers ({setting})")


@pytest.mark.parametrize("setting", tuple(SEQ_SETTINGS))
def test_a_stable_function_parallel_sequential_scan_inside_an_update(pgc_conn, expect, setting):
    table, settings, col = _seq_setting(pgc_conn, setting)
    expect.text(_par_vs_serial(pgc_conn, table, settings, [
        f"UPDATE %t SET {col} = cnt_seq('%t', '{col}') WHERE id <= 3",
        f"SELECT string_agg({col}::text, ',' ORDER BY id) FROM %t WHERE id <= 3"]), "same",
                f"a STABLE function's parallel sequential scan inside an UPDATE answers as it does serially ({setting})")


@pytest.mark.parametrize("setting", tuple(SEQ_SETTINGS))
def test_a_stable_function_parallel_sequential_scan_inside_a_cte_delete(pgc_conn, expect, setting):
    table, settings, col = _seq_setting(pgc_conn, setting)
    expect.text(_par_vs_serial(pgc_conn, table, settings, [
        f"WITH d AS (DELETE FROM %t WHERE id <= 5 AND cnt_seq('%t', '{col}') > 0 RETURNING cnt_seq('%t', '{col}') AS n) "
        "SELECT count(*) || '|' || min(n) || '|' || max(n) FROM d"]), "same",
                f"a STABLE function's parallel sequential scan inside a DELETE in a WITH clause answers as it does serially ({setting})")


@pytest.mark.parametrize("setting", tuple(SEQ_SETTINGS))
def test_a_stable_function_parallel_sequential_scan_in_a_cursor(pgc_conn, expect, setting):
    table, settings, col = _seq_setting(pgc_conn, setting)
    expect.text(_par_vs_serial(pgc_conn, table, settings, [
        f"DECLARE c CURSOR FOR SELECT cnt_seq('%t', '{col}')", "DELETE FROM %t WHERE id <= 1000",
        "FETCH ALL FROM c"]), "same",
                f"a STABLE function's parallel sequential scan in a cursor answers as it does serially ({setting})")


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


# Without shared_preload_libraries (review of #2269). The workers' copy of the
# registry lives in shared memory that exists only when pgcolumnar is preloaded.
# Without the library preloaded, no session can load it: it defines settings fixed
# at server start, and PostgreSQL refuses those after startup with a FATAL. So a
# parallel plan cannot give a wrong answer for want of the copy. If that ever
# changes, columnar_cid_registry.c refuses the plan with SQLSTATE 0A000 instead.
# A private cluster, started without the library preloaded.
NOPRELOAD_LOAD = "cannot create PGC_POSTMASTER variables after startup"
NOPRELOAD_REFUSAL = ("columnar: a parallel plan cannot read this transaction's own writes "
                     "when pgcolumnar is not preloaded")


def _np_tx(dsn, table, statements):
    """Run `statements` on `table` in a fresh session, roll back, and return the last
    row, or the error's text."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
            _session(cur)
            cur.execute("BEGIN")
            for sql in statements:
                cur.execute(sql.replace("%t", table))
            return cur.fetchone()
    except psycopg.Error as exc:
        return str(exc)


def test_without_preload_a_parallel_index_scan_is_refused_or_right(pgc_cluster, expect):
    import pathlib
    import shutil
    import tempfile
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-nopreload-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "nopreload", datadir, pick_port(17))
    try:
        cluster.initdb()
        cluster.start()
        # the tables and functions, made while the library is preloaded
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            _fresh_pk(c)
        cluster.stop()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\nshared_preload_libraries=''\n")
        cluster.start()
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            expect.text(str(c.execute("SELECT current_setting('shared_preload_libraries') = ''").fetchone()[0]),
                        "True", "premise: the server now runs without pgcolumnar in shared_preload_libraries")
        stmts = PARIDX + ["UPDATE %t SET id = id + 1000000, pad = cnt_idx('%t') WHERE id % 1000 = 1",
                          "SELECT count(*) || '|' || min(pad) || '|' || max(pad) FROM %t WHERE id > 1000000"]
        col = _np_tx(cluster.dsn(), "sv_pgcolumnar", stmts)
        heap = _np_tx(cluster.dsn(), "sv_heap", stmts)
        # the answer first: it is the claim, and the load refusal below is how it holds today
        ok = col == heap or NOPRELOAD_LOAD in str(col) or NOPRELOAD_REFUSAL in str(col)
        expect.text("ok" if ok else f"wrong: columnar={col} heap={heap}", "ok",
                    "without shared_preload_libraries, a STABLE function's parallel index scan inside an "
                    "UPDATE is refused or answers as heap does")
        expect.text("refused" if NOPRELOAD_LOAD in str(col) else f"not refused: {col}", "refused",
                    "without shared_preload_libraries, the library refuses to load rather than run a columnar query")
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)

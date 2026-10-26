"""The pytest twin of `test/phase5.sh`, every check by the shell's name (#432).

Phase 5 is planner integration and vacuum. The custom scan is the plan for a plain and a
filtered SELECT; a narrow range reads one chunk group of five and skips four, while an
unfiltered count(*) or sum is answered from zone-map metadata and scans none; results are
the same with qual pushdown on and off; only the referenced columns are projected;
per-table options (compression, chunk_group_row_limit, and its reset) take effect for
later writes; vacuum compacts five stripes into one, drops the deleted rows and keeps
the values, and rebuilds an index so index scans stay right after renumbering;
vacuum_full keeps the data; the default plan is the serial custom scan, and disabling it
falls back to a sequential scan with the same answer.

The module fixture builds and measures all of it once, in the shell's order, in a
private database, and each check is its own test, so one red does not hide the next.
The index arms switch off both the sequential and the custom scan, as the shell now
does, and assert that the point lookup's plan is an index scan.

set_options and reset_options reach the same verdict on every kind of relation, and
both accept a columnar materialized view (#1265, #1341). reset_options had no guard at
all, so it reported success for a heap table and a partitioned parent while set_options
refused the same relation. The property is parity, not a relkind list. Parity alone is
satisfied by both refusing, so the settled verdict on a matview (accept) and on a
partitioned parent (refuse) are asserted by name too, and an ordinary columnar table is
the control both must accept.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error


def _verdict(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "accepted"
    except psycopg.Error:
        return "refused"


def _set(conn, rel):
    return _verdict(conn, f"SELECT pgcolumnar.set_options('{rel}', chunk_group_row_limit => 1000)")


def _reset(conn, rel):
    return _verdict(conn, f"SELECT pgcolumnar.reset_options('{rel}', chunk_group_row_limit => true)")


def _agree(conn, rel):
    s, r = _set(conn, rel), _reset(conn, rel)
    return "agree" if s == r else f"set={s} reset={r}"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_set_and_reset_options_agree(pgc_conn, expect):
    conn = pgc_conn
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE o_par_col (a int) USING pgcolumnar")
        cur.execute("CREATE TABLE o_par_heap (a int)")
        cur.execute("CREATE TABLE o_par_part (a int) PARTITION BY RANGE (a)")
        cur.execute("CREATE TABLE o_par_mvsrc (a int)")
        cur.execute("INSERT INTO o_par_mvsrc VALUES (1), (2)")
        cur.execute("CREATE MATERIALIZED VIEW o_par_mv USING pgcolumnar AS SELECT a FROM o_par_mvsrc")

    expect.text(_one(conn, "SELECT c.relkind::text || am.amname::text FROM pg_class c JOIN pg_am am "
                           "ON am.oid = c.relam WHERE c.oid = 'o_par_col'::regclass"), "rpgcolumnar",
                "premise: the columnar fixture is relkind r on the columnar access method")
    expect.text(_one(conn, "SELECT c.relkind::text || (am.amname <> 'pgcolumnar')::text FROM pg_class c "
                           "JOIN pg_am am ON am.oid = c.relam WHERE c.oid = 'o_par_heap'::regclass"), "rtrue",
                "premise: the heap fixture is relkind r and is NOT columnar")
    expect.text(_one(conn, "SELECT c.relkind::text || c.relfilenode::text FROM pg_class c "
                           "WHERE c.oid = 'o_par_part'::regclass"), "p0",
                "premise: the partitioned fixture is relkind p with no storage")
    expect.text(_one(conn, "SELECT c.relkind::text || am.amname::text FROM pg_class c JOIN pg_am am "
                           "ON am.oid = c.relam WHERE c.oid = 'o_par_mv'::regclass"), "mpgcolumnar",
                "premise: the matview fixture is relkind m on the columnar access method")

    # Without it "agree" is satisfied by a build where both refuse everything.
    expect.text(f"{_set(conn, 'o_par_col')}/{_reset(conn, 'o_par_col')}", "accepted/accepted",
                "control: both entry points accept an ordinary columnar table")

    expect.text(_agree(conn, "o_par_col"), "agree", "set_options and reset_options agree about a columnar table")
    expect.text(_agree(conn, "o_par_heap"), "agree", "set_options and reset_options agree about a heap table")
    expect.text(_agree(conn, "o_par_part"), "agree",
                "set_options and reset_options agree about a partitioned parent")
    expect.text(_agree(conn, "o_par_mv"), "agree", "set_options and reset_options agree about a columnar matview")

    expect.text(f"{_set(conn, 'o_par_mv')}/{_reset(conn, 'o_par_mv')}", "accepted/accepted",
                "both entry points accept a columnar matview (#1265, settled)")
    # Widening to matviews is not widening to everything without storage.
    expect.text(f"{_set(conn, 'o_par_part')}/{_reset(conn, 'o_par_part')}", "refused/refused",
                "control: a partitioned parent is still refused by both")


DB = "pgc_phase5"
EA = "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)"


def _first_line(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _lines(conn, sql, settings=()):
    """-> the statement's output as text lines (an EXPLAIN's plan), or a failed reading.
    `settings` are SET for this statement only."""
    try:
        with conn.cursor() as cur:
            for st in settings:
                cur.execute(st)
            cur.execute(sql)
            out = [str(r[0]) for r in cur.fetchall()]
            cur.execute("RESET ALL")
            return out
    except psycopg.Error as exc:
        return query_error(_first_line(exc))


def _q(conn, sql, settings=()):
    """-> the first column of the first row, "(no row)" when there is none (a reading to
    fail on, not a failed one), or a failed reading."""
    rows = _lines(conn, sql, settings)
    if isinstance(rows, str):
        return rows
    return rows[0] if rows else "(no row)"


def _qn(conn, sql, settings=()):
    """-> _q as an int when it is one."""
    v = _q(conn, sql, settings)
    return int(v) if re.fullmatch(r"-?[0-9]+", v) else v


def _label_num(plan, label):
    """-> the integer trailing the first plan line carrying `label`, "(absent)" when no
    line carries it, or the failed reading."""
    if isinstance(plan, str):
        return plan
    for line in plan:
        if label in line:
            m = re.search(r"([0-9]+)$", line)
            return int(m.group(1)) if m else f"(no number in {line.strip()!r})"
    return "(absent)"


def _plan_matches(plan, want, notwant=None):
    """-> "matches" when the plan contains `want` and not `notwant`, else the plan."""
    if isinstance(plan, str):
        return plan
    text = "\n".join(plan)
    if want in text and (notwant is None or notwant not in text):
        return "matches"
    return "plan was: " + " / ".join(line.strip() for line in plan)


@pytest.fixture(scope="module")
def p5(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    r = {}
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=DB), autocommit=True) as conn:
            def build(*stmts):
                try:
                    for sql in stmts:
                        conn.execute(sql)
                except psycopg.Error as exc:
                    print(f"-- fixture build failed: {_first_line(exc)}")

            # the custom scan path; one row group per 10000 rows, so 50000 rows form five
            build("CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                  "CREATE TABLE t (a int, b text, c int) USING pgcolumnar",
                  "SELECT pgcolumnar.set_options('t', stripe_row_limit => 10000)",
                  "INSERT INTO t SELECT g, 'r' || g, g % 7 FROM generate_series(1, 50000) g")
            r["plain_plan"] = _plan_matches(_lines(conn, "EXPLAIN (COSTS OFF) SELECT * FROM t"),
                                            "Custom Scan (PgColumnarScan)", "Seq Scan")
            r["filtered_plan"] = _plan_matches(
                _lines(conn, "EXPLAIN (COSTS OFF) SELECT a FROM t WHERE a = 12345"),
                "Custom Scan (PgColumnarScan)", "Seq Scan")

            # chunk-group skipping: unfiltered aggregates are answered from metadata
            def total_lines(sql):
                plan = _lines(conn, f"{EA} {sql}")
                return plan if isinstance(plan, str) else sum("Chunk Groups Total" in x for x in plan)
            r["cover_lines"] = total_lines("SELECT count(*) FROM t")
            r["sum_lines"] = total_lines("SELECT sum(a) FROM t")
            narrow = _lines(conn, f"{EA} SELECT sum(a) FROM t WHERE a BETWEEN 12000 AND 12100")
            r["read_f"] = _label_num(narrow, "Chunk Groups Read")
            r["skip_f"] = _label_num(narrow, "Chunk Groups Removed by Filter")
            r["cnt_read_f"] = _label_num(
                _lines(conn, f"{EA} SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100"),
                "Chunk Groups Read")

            # results identical with pushdown on and off
            on = ("SET pgcolumnar.enable_qual_pushdown = on",)
            off = ("SET pgcolumnar.enable_qual_pushdown = off",)
            r["on_cnt"] = _qn(conn, "SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100", on)
            r["off_cnt"] = _qn(conn, "SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100", off)
            r["on_sum"] = _qn(conn, "SELECT sum(a) FROM t WHERE a > 49990", on)
            r["off_sum"] = _qn(conn, "SELECT sum(a) FROM t WHERE a > 49990", off)
            r["eq"] = (_qn(conn, "SELECT count(*) FROM t WHERE c = 3"),
                       _qn(conn, "SELECT count(*) FROM t WHERE c = 3", off))

            # column projection
            one = _lines(conn, "EXPLAIN (COSTS OFF) SELECT a FROM t WHERE a = 5")
            r["proj_one"] = _label_num(one, "Projected Columns")
            r["tot_cols"] = _label_num(one, "Total Columns")
            r["proj_star"] = _label_num(_lines(conn, "EXPLAIN (COSTS OFF) SELECT * FROM t"),
                                        "Projected Columns")
            r["proj_two"] = _label_num(_lines(conn, "EXPLAIN (COSTS OFF) SELECT a, c FROM t WHERE a = 5"),
                                       "Projected Columns")

            # per-table options take effect for later writes
            codec = ("SELECT max(block_codec) FROM pgcolumnar.column_chunk "
                     "WHERE storage_id = pgcolumnar.get_storage_id('{}')")
            vectors = ("SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id = "
                       "pgcolumnar.get_storage_id('{}') AND vector_index >= 0 AND column_index = 0")
            build("CREATE TABLE o_def (a int, b text) USING pgcolumnar",
                  "INSERT INTO o_def SELECT g, repeat('x', 200) FROM generate_series(1, 20000) g")
            r["codec_def"] = _qn(conn, codec.format("o_def"))
            build("CREATE TABLE o_none (a int, b text) USING pgcolumnar",
                  "SELECT pgcolumnar.set_options('o_none', compression => 'none')",
                  "INSERT INTO o_none SELECT g, repeat('x', 200) FROM generate_series(1, 20000) g")
            r["codec_none"] = _qn(conn, codec.format("o_none"))
            build("CREATE TABLE o_cg (a int) USING pgcolumnar",
                  "SELECT pgcolumnar.set_options('o_cg', chunk_group_row_limit => 1000)",
                  "INSERT INTO o_cg SELECT g FROM generate_series(1, 5000) g")
            r["cg"] = _qn(conn, vectors.format("o_cg"))
            build("CREATE TABLE o_reset (a int) USING pgcolumnar",
                  "SELECT pgcolumnar.set_options('o_reset', chunk_group_row_limit => 1000)",
                  "SELECT pgcolumnar.reset_options('o_reset', chunk_group_row_limit => true)",
                  "INSERT INTO o_reset SELECT g FROM generate_series(1, 5000) g")
            r["reset"] = _qn(conn, vectors.format("o_reset"))

            # vacuum: five stripes, half deleted, compacted into one
            build("CREATE TABLE v (a int, b text) USING pgcolumnar",
                  "SELECT pgcolumnar.set_options('v', stripe_row_limit => 1000)",
                  "INSERT INTO v SELECT g, 'v' || g FROM generate_series(1, 5000) g")
            r["v_stripes0"] = _qn(conn, "SELECT count(*) FROM pgcolumnar.stats('v')")
            build("DELETE FROM v WHERE a % 2 = 0")
            r["v_live"] = _qn(conn, "SELECT count(*) FROM v")
            r["v_deleted"] = _qn(conn, "SELECT sum(deletedrows) FROM pgcolumnar.stats('v')")
            build("SELECT pgcolumnar.reset_options('v', stripe_row_limit => true)",
                  "SELECT pgcolumnar.vacuum('v')")
            r["v_stripes1"] = _qn(conn, "SELECT count(*) FROM pgcolumnar.stats('v')")
            r["v_deleted1"] = _qn(conn, "SELECT COALESCE(sum(deletedrows), 0) FROM pgcolumnar.stats('v')")
            r["v_rows"] = _qn(conn, "SELECT count(*) FROM v")
            r["v_sum"] = (_qn(conn, "SELECT sum(a) FROM v"),
                          _qn(conn, "SELECT sum(a) FROM (SELECT generate_series(1, 5000) a) s WHERE a % 2 = 1"))
            r["v_value"] = _q(conn, "SELECT b FROM v WHERE a = 4999")

            # vacuum rebuilds indexes, so index scans stay right after renumbering
            build("CREATE TABLE vi (a int, b text) USING pgcolumnar",
                  "INSERT INTO vi SELECT g, 'i' || g FROM generate_series(1, 20000) g",
                  "CREATE INDEX vi_a_idx ON vi (a)",
                  "DELETE FROM vi WHERE a BETWEEN 100 AND 5000",
                  "SELECT pgcolumnar.vacuum('vi')")
            # enable_seqscan alone leaves the custom scan cheapest, so the lookups would
            # never read vi_a_idx: the custom scan is switched off too, and the plan read
            idx = ("SET enable_seqscan = off", "SET pgcolumnar.enable_custom_scan = off")
            r["vi_count"] = _qn(conn, "SELECT count(*) FROM vi")
            vi_plan = _lines(conn, "EXPLAIN (COSTS OFF) SELECT b FROM vi WHERE a = 12345", idx)
            r["vi_plan"] = vi_plan if isinstance(vi_plan, str) else (
                "yes" if any("Index Scan using vi_a_idx" in x for x in vi_plan)
                else "no: " + " ".join(vi_plan))
            r["vi_point"] = _q(conn, "SELECT b FROM vi WHERE a = 12345", idx)
            r["vi_deleted"] = _qn(conn, "SELECT count(*) FROM vi WHERE a BETWEEN 100 AND 5000", idx)
            r["vi_survivor"] = _qn(conn, "SELECT count(*) FROM vi WHERE a BETWEEN 1 AND 99", idx)

            build("SELECT pgcolumnar.vacuum_full('public')")
            r["vacuum_full"] = _qn(conn, "SELECT count(*) FROM t")

            # the default plan is the serial custom scan; disabled, a sequential scan
            dflt = _lines(conn, "EXPLAIN (COSTS OFF) SELECT count(*) FROM t")
            r["default_not_seq"] = _plan_matches(dflt, "Custom Scan (PgColumnarScan)", "Seq Scan")
            r["default_not_parallel"] = _plan_matches(dflt, "Custom Scan (PgColumnarScan)", "Gather")
            nocs = ("SET pgcolumnar.enable_custom_scan = off",)
            r["disabled_plan"] = _plan_matches(
                _lines(conn, "EXPLAIN (COSTS OFF) SELECT count(*) FROM t", nocs), "Seq Scan", "Custom Scan")
            r["fallback"] = _qn(conn, "SELECT count(*) FROM t WHERE a BETWEEN 12000 AND 12100", nocs)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_a_plain_select_uses_the_custom_scan(p5, expect):
    expect.text(p5["plain_plan"], "matches", "plain select uses custom scan")


def test_a_filtered_select_uses_the_custom_scan(p5, expect):
    expect.text(p5["filtered_plan"], "matches", "filtered select uses custom scan")


def test_a_covering_count_skips_the_scan(p5, expect):
    expect.num(p5["cover_lines"], 0, "covering count(*) skips the scan")


def test_an_unfiltered_sum_is_answered_from_metadata(p5, expect):
    expect.num(p5["sum_lines"], 0, "unfiltered sum answered from metadata")


def test_a_narrow_range_reads_one_group(p5, expect):
    expect.num(p5["read_f"], 1, "filtered reads one group")


def test_a_narrow_range_skips_four_groups(p5, expect):
    expect.num(p5["skip_f"], 4, "filtered skips four groups")


def test_a_filtered_count_also_scans_one_group(p5, expect):
    expect.num(p5["cnt_read_f"], 1, "filtered count(*) also scans one group")


def test_the_pushdown_on_count(p5, expect):
    expect.num(p5["on_cnt"], 101, "pushdown-on count")


def test_the_pushdown_count_matches_off(p5, expect):
    expect.num(p5["on_cnt"], p5["off_cnt"], "pushdown matches off")


def test_the_pushdown_sum_matches_off(p5, expect):
    expect.num(p5["on_sum"], p5["off_sum"], "pushdown-on sum matches off")


def test_an_equality_on_a_low_cardinality_column_returns_every_match(p5, expect):
    got, want = p5["eq"]
    expect.num(got, want, "equality full result")


def test_one_referenced_column_is_projected(p5, expect):
    expect.num(p5["proj_one"], 1, "projects one column")


def test_the_table_has_three_columns(p5, expect):
    expect.num(p5["tot_cols"], 3, "table has three columns")


def test_select_star_projects_every_column(p5, expect):
    expect.num(p5["proj_star"], 3, "select star projects all")


def test_two_referenced_columns_are_projected(p5, expect):
    expect.num(p5["proj_two"], 2, "projects two columns")


def test_the_default_compression_is_zstd(p5, expect):
    expect.num(p5["codec_def"], 3, "default uses zstd")


def test_compression_none_stores_uncompressed(p5, expect):
    expect.num(p5["codec_none"], 0, "option none disables compression")


def test_the_chunk_group_limit_applies(p5, expect):
    expect.num(p5["cg"], 5, "chunk group limit applied")


def test_reset_restores_the_default_limit(p5, expect):
    expect.num(p5["reset"], 1, "reset restores default limit")


def test_the_vacuum_table_starts_with_five_stripes(p5, expect):
    expect.num(p5["v_stripes0"], 5, "v starts with five stripes")


def test_the_live_rows_after_delete(p5, expect):
    expect.num(p5["v_live"], 2500, "v live rows after delete")


def test_the_deleted_rows_are_tracked(p5, expect):
    expect.num(p5["v_deleted"], 2500, "v deleted rows tracked")


def test_vacuum_compacts_to_one_stripe(p5, expect):
    expect.num(p5["v_stripes1"], 1, "v compacts to one stripe")


def test_vacuum_leaves_no_deleted_rows(p5, expect):
    expect.num(p5["v_deleted1"], 0, "v no deleted rows after vacuum")


def test_the_rows_are_right_after_vacuum(p5, expect):
    expect.num(p5["v_rows"], 2500, "v rows correct after vacuum")


def test_the_sum_is_right_after_vacuum(p5, expect):
    got, want = p5["v_sum"]
    expect.num(got, want, "v sum correct after vacuum")


def test_a_value_is_intact_after_vacuum(p5, expect):
    expect.text(p5["v_value"], "v4999", "v value intact after vacuum")


def test_the_indexed_table_count_after_vacuum(p5, expect):
    expect.num(p5["vi_count"], 15099, "vi count after vacuum")


def test_the_point_lookup_uses_the_index(p5, expect):
    expect.text(p5["vi_plan"], "yes", "the vi point lookup really uses the index")


def test_an_index_point_lookup_after_vacuum(p5, expect):
    expect.text(p5["vi_point"], "i12345", "vi index point after vacuum")


def test_the_index_sees_the_deletion_after_vacuum(p5, expect):
    expect.num(p5["vi_deleted"], 0, "vi index sees deletion after vacuum")


def test_the_index_finds_the_survivors_after_vacuum(p5, expect):
    expect.num(p5["vi_survivor"], 99, "vi index survivor after vacuum")


def test_vacuum_full_keeps_the_data(p5, expect):
    expect.num(p5["vacuum_full"], 50000, "vacuum_full keeps data")


def test_the_default_plan_is_the_custom_scan(p5, expect):
    expect.text(p5["default_not_seq"], "matches", "columnar default uses custom scan, not seqscan")


def test_the_default_plan_is_not_parallel(p5, expect):
    expect.text(p5["default_not_parallel"], "matches", "columnar default plan is not parallel")


def test_disabling_the_custom_scan_falls_back_to_a_seq_scan(p5, expect):
    expect.text(p5["disabled_plan"], "matches", "disable custom scan -> seq scan")


def test_the_seq_scan_fallback_returns_the_right_rows(p5, expect):
    expect.num(p5["fallback"], 101, "seq-scan fallback returns correct rows")

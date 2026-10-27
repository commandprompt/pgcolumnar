"""Per-row-group distinct-count sketches, the catalog and the writer (#1309 phase 1).
The pytest twin of `test/native_sketch.sh`, every check by the shell's name.

A column named in set_options(sketch_columns => ...) gets one HyperLogLog sketch per
row group in pgcolumnar.sketch: 2048 one-byte registers, filled from the type's
extended hash (seed 0) of each non-NULL value. Only named columns get one; the option
refuses an unknown column, a type with no extended hash and a nondeterministic
collation; NULLs are not hashed; the registers of a fixed group are pinned, because
they are stored on disk and merged across builds; a parallel flush writes the same
sketch; a projection's storage gets none; TRUNCATE, DROP and a rewrite leave none
behind for a row group that no longer exists.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_sketch"
PIN = "ce25d8b59b9f5a5752c42116b4ff9212"
# THE RULE, RECOMPUTED in SQL from core's hashint8extended: the low 11 bits pick the
# register, its value one more than the trailing zeros of the other 53 bits (54 if zero).
# A read inside an EXCEPTION block writes the INSERT's buffer and keeps it (#1360); the
# block aborts and the rows are written again. The table's sketch union must be the pin.
KEPT_FN = "CREATE FUNCTION skx_f(g int) RETURNS bigint LANGUAGE plpgsql AS $f$ BEGIN IF g = 500 THEN BEGIN PERFORM count(*) FROM skx; RAISE EXCEPTION 'boom'; EXCEPTION WHEN others THEN NULL; END; END IF; RETURN g; END $f$;"
KEPT_UNION = "SELECT md5(decode(string_agg(lpad(to_hex(m), 2, '0'), '' ORDER BY i), 'hex')) FROM (SELECT i, max(get_byte(k.registers, i)) AS m FROM pgcolumnar.sketch k, generate_series(0, 2047) i WHERE k.storage_id = pgcolumnar.get_storage_id('skx') GROUP BY i) u"
ORACLE = "WITH h AS (SELECT hashint8extended(g::bigint, 0) AS h FROM generate_series(1, 1000) g), r AS (SELECT (h & 2047)::int AS idx, (h >> 11) & 9007199254740991 AS w FROM h), k AS (SELECT idx, max(CASE WHEN w = 0 THEN 54 ELSE round(log(2, (w & -w)::numeric))::int + 1 END) AS rank FROM r GROUP BY idx) SELECT md5(decode(string_agg(lpad(to_hex(coalesce(k.rank, 0)), 2, '0'), '' ORDER BY i), 'hex')) FROM generate_series(0, 2047) i LEFT JOIN k ON k.idx = i"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())  # simple protocol: several statements, as psql -c
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _sqlstate(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "noerror"


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


# THE BASE STORAGE ONLY: a projection's storage row carries the table's oid too, so a
# join on relation_oid would count the projection's row groups as the table's.
def _sketches(dsn, table, column=None):
    col = f" AND k.column_index = {column}" if column is not None else ""
    return _value(dsn, "SELECT count(*) FROM pgcolumnar.sketch k "
                       f"WHERE k.storage_id = pgcolumnar.get_storage_id('{table}'){col}")


def _groups(dsn, table):
    return _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group r "
                       f"WHERE r.storage_id = pgcolumnar.get_storage_id('{table}')")


def _nonzero(dsn, table, which):
    return _value(dsn, f"""
        SELECT count(*) FROM pgcolumnar.sketch k
          JOIN pgcolumnar.storage s ON s.storage_id = k.storage_id,
               generate_series(0, length(k.registers) - 1) i
         WHERE s.relation_oid = '{table}'::regclass AND k.column_index = 0
           AND k.group_number = (SELECT {which}(r.group_number) FROM pgcolumnar.row_group r
                                  WHERE r.storage_id = s.storage_id)
           AND get_byte(k.registers, i) <> 0""")


def _md5(dsn, table):
    return _value(dsn, "SELECT md5(k.registers) FROM pgcolumnar.sketch k JOIN pgcolumnar.storage s "
                       f"ON s.storage_id = k.storage_id WHERE s.relation_oid = '{table}'::regclass "
                       "AND k.column_index = 0")


def _option(dsn, table):
    return _value(dsn, "SELECT coalesce(sketch_columns::text, 'NULL') FROM pgcolumnar.options "
                       f"WHERE regclass = '{table}'::regclass")


@pytest.fixture(scope="module")
def nsk(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}

        # 1. the option
        _run(dsn, "CREATE TABLE sk (a bigint, b text) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('sk', stripe_row_limit => 1000);")
        r["accept"] = _sqlstate(dsn, "SELECT pgcolumnar.set_options('sk', sketch_columns => ARRAY['a'])")
        r["recorded"] = _option(dsn, "sk")
        r["unknown"] = _sqlstate(dsn, "SELECT pgcolumnar.set_options('sk', sketch_columns => ARRAY['nosuch'])")
        _run(dsn, "CREATE TABLE skp (p point) USING pgcolumnar")
        r["nohash"] = _sqlstate(dsn, "SELECT pgcolumnar.set_options('skp', sketch_columns => ARRAY['p'])")
        _run(dsn, "CREATE COLLATION IF NOT EXISTS sk_ci "
                  "(provider = icu, locale = 'und-u-ks-level2', deterministic = false)")
        if _value(dsn, "SELECT count(*) FROM pg_collation WHERE collname = 'sk_ci'") == "1":
            _run(dsn, "CREATE TABLE skc (t text COLLATE sk_ci) USING pgcolumnar")
            r["nondet"] = _sqlstate(dsn, "SELECT pgcolumnar.set_options('skc', sketch_columns => ARRAY['t'])")
        else:
            r["nondet"] = None
        r["still"] = _option(dsn, "sk")

        # 2. the writer
        _run(dsn, "INSERT INTO sk SELECT g, 'v' || g FROM generate_series(1, 3000) g")
        r["groups"] = _groups(dsn, "sk")
        r["named"] = _sketches(dsn, "sk", 0)
        r["unnamed"] = _sketches(dsn, "sk", 1)
        r["length"] = _value(dsn, "SELECT string_agg(DISTINCT length(k.registers)::text, ',') "
                                  "FROM pgcolumnar.sketch k JOIN pgcolumnar.storage s "
                                  "ON s.storage_id = k.storage_id WHERE s.relation_oid = 'sk'::regclass")
        _run(dsn, "CREATE TABLE skn (a bigint) USING pgcolumnar; "
                  "INSERT INTO skn SELECT g FROM generate_series(1, 3000) g;")
        r["none"] = f"{_sketches(dsn, 'skn')} over {_groups(dsn, 'skn')} groups"
        _run(dsn, "CREATE TABLE sk1 (a bigint) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('sk1', stripe_row_limit => 1000, sketch_columns => ARRAY['a']); "
                  "INSERT INTO sk1 SELECT 42 FROM generate_series(1, 1000); "
                  "INSERT INTO sk1 SELECT NULL FROM generate_series(1, 1000);")
        r["sk1 groups"] = _value(dsn, "SELECT string_agg(r.row_count::text, ',' ORDER BY r.group_number) "
                                      "FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s "
                                      "ON s.storage_id = r.storage_id WHERE s.relation_oid = 'sk1'::regclass")
        r["one value"] = _nonzero(dsn, "sk1", "min")
        r["all null"] = _nonzero(dsn, "sk1", "max")
        _run(dsn, "CREATE TABLE skpin (a bigint) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('skpin', sketch_columns => ARRAY['a']); "
                  "INSERT INTO skpin SELECT g FROM generate_series(1, 1000) g;")
        r["pin"] = _md5(dsn, "skpin")
        # the same registers built in SQL from core's hashint8extended (see the shell)
        r["oracle"] = _value(dsn, ORACLE)

        # a separate transaction for the create and the insert, and two columns,
        # or the writer stays serial; the DEBUG1 line shows a worker flushed
        _run(dsn, "CREATE TABLE skpar (a bigint, b bigint) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('skpar', sketch_columns => ARRAY['a'])")
        notices = []
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
            try:
                conn.execute(b"SET pgcolumnar.parallel_flush = on; SET client_min_messages = debug1; "
                             b"INSERT INTO skpar SELECT g, g FROM generate_series(1, 1000) g;")
            except psycopg.Error as exc:
                notices.append(_message(exc))
        r["par flushed"] = sum(1 for n in notices
                               if re.search(r"parallel_flush: [1-9][0-9]* of 2 columns by [1-9]", n))
        r["par"] = _md5(dsn, "skpar")

        # 3. reset, projections, cleanup
        _run(dsn, "SELECT pgcolumnar.add_projection('sk', 'skproj', ARRAY['a'])")
        _run(dsn, "INSERT INTO sk SELECT g, 'p' || g FROM generate_series(1, 1000) g")
        r["proj groups"] = _value(dsn, "SELECT CASE WHEN count(*) > 0 THEN 'some' ELSE 'none' END "
                                       "FROM pgcolumnar.row_group WHERE storage_id IN (SELECT proj_storage_id "
                                       "FROM pgcolumnar.projection WHERE name = 'skproj')")
        r["proj"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.sketch WHERE storage_id IN "
                                "(SELECT proj_storage_id FROM pgcolumnar.projection WHERE name = 'skproj')")
        _run(dsn, "SELECT pgcolumnar.vacuum('sk')")
        g = _groups(dsn, "sk")
        r["rewrite"] = (f"{_sketches(dsn, 'sk', 0)} for {g} groups", f"{g} for {g} groups")
        # compact_rewrite retires groups one at a time; vacuum drops the old storage whole
        rw = _value(dsn, "WITH d AS (DELETE FROM sk WHERE a % 3 = 0 RETURNING 1) SELECT count(*) FROM d")
        rw = _value(dsn, "SELECT pgcolumnar.compact_rewrite('sk')")
        r["compacted"] = ("yes" if isinstance(rw, str) and rw.isdigit() and int(rw) > 0 else f"no ({rw})")
        g = _groups(dsn, "sk")
        r["compact"] = (f"{_sketches(dsn, 'sk', 0)} for {g} groups", f"{g} for {g} groups")
        _run(dsn, "SELECT pgcolumnar.reset_options('sk', sketch_columns => true)")
        r["reset"] = _option(dsn, "sk")
        before, gbefore = _sketches(dsn, "sk", 0), _groups(dsn, "sk")
        _run(dsn, "INSERT INTO sk SELECT g, 'w' || g FROM generate_series(1, 1000) g")
        after, gafter = _sketches(dsn, "sk", 0), _groups(dsn, "sk")
        nums = all(isinstance(v, str) and v.isdigit() for v in (before, gbefore, after, gafter))
        r["reset wrote"] = str(int(gafter) - int(gbefore)) if nums else query_error(f"{gbefore} / {gafter}")
        r["after reset"] = (f"{int(after) - int(before)} new over {before} before" if nums
                            else query_error(f"{before} / {after}"), f"0 new over {before} before")
        sid = _value(dsn, "SELECT storage_id FROM pgcolumnar.storage WHERE relation_oid = 'sk1'::regclass")
        _run(dsn, "TRUNCATE sk1")
        r["truncate"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.sketch WHERE storage_id = {sid}")
        sid = _value(dsn, "SELECT storage_id FROM pgcolumnar.storage WHERE relation_oid = 'skpin'::regclass")
        _run(dsn, "DROP TABLE skpin")
        r["drop"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.sketch WHERE storage_id = {sid}")
        # 4. a kept flush (#1360), a rename, and groups from before the option
        _run(dsn, "CREATE TABLE skx (a bigint) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('skx', sketch_columns => ARRAY['a']); " + KEPT_FN)
        _run(dsn, "INSERT INTO skx SELECT skx_f(g) FROM generate_series(1, 1000) g")
        r["kept rows"] = _value(dsn, "SELECT count(*) || '/' || count(DISTINCT a) FROM skx")
        r["kept union"] = _value(dsn, KEPT_UNION)
        _run(dsn, "CREATE TABLE skr (a bigint) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('skr', sketch_columns => ARRAY['a']);")
        _run(dsn, "INSERT INTO skr SELECT g FROM generate_series(1, 100) g")
        _run(dsn, "ALTER TABLE skr RENAME COLUMN a TO a2")
        _run(dsn, "INSERT INTO skr SELECT g FROM generate_series(101, 200) g")
        r["renamed option"] = _value(dsn, "SELECT sketch_columns::text FROM pgcolumnar.options "
                                          "WHERE regclass = 'skr'::regclass")
        r["renamed"] = f"{_sketches(dsn, 'skr', 0)} for {_groups(dsn, 'skr')} groups"
        _run(dsn, "CREATE TABLE skb (a bigint) USING pgcolumnar")
        _run(dsn, "INSERT INTO skb SELECT g FROM generate_series(1, 100) g")
        _run(dsn, "SELECT pgcolumnar.set_options('skb', sketch_columns => ARRAY['a'])")
        _run(dsn, "INSERT INTO skb SELECT g FROM generate_series(101, 200) g")
        r["before"] = f"{_sketches(dsn, 'skb', 0)} for {_groups(dsn, 'skb')} groups"
        r["orphans"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.sketch k WHERE NOT EXISTS "
                                   "(SELECT 1 FROM pgcolumnar.row_group r WHERE r.storage_id = k.storage_id "
                                   "AND r.group_number = k.group_number)")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_set_options_accepts_sketch_columns(nsk, expect):
    expect.text(nsk["accept"], "noerror", "set_options accepts sketch_columns naming a column")


def test_the_options_row_records_it(nsk, expect):
    expect.text(nsk["recorded"], "{a}", "and the options row records it")


def test_an_unknown_column_is_refused(nsk, expect):
    expect.text(nsk["unknown"], "42703", "an unknown column is refused")


def test_a_type_with_no_extended_hash_is_refused(nsk, expect):
    expect.text(nsk["nohash"], "0A000", "a column whose type has no extended hash is refused")


def test_a_nondeterministic_collation_is_refused(nsk, expect):
    if nsk["nondet"] is None:
        expect.cannot_run("MISSING_DEPENDENCY",
                          "this server has no ICU, so no nondeterministic collation can be created",
                          name="a column with a nondeterministic collation is refused")
        return
    expect.text(nsk["nondet"], "0A000", "a column with a nondeterministic collation is refused")


def test_the_refusals_left_the_option_alone(nsk, expect):
    expect.text(nsk["still"], "{a}", "the refusals left the recorded option alone")


def test_the_insert_wrote_three_row_groups(nsk, expect):
    expect.text(nsk["groups"], "3", "premise: the insert wrote three row groups")


def test_each_row_group_gets_one_sketch(nsk, expect):
    expect.text(nsk["named"], "3", "each row group gets one sketch for the named column")


def test_a_column_not_named_gets_no_sketch(nsk, expect):
    expect.text(nsk["unnamed"], "0", "a column not named gets no sketch")


def test_each_sketch_holds_2048_registers(nsk, expect):
    expect.text(nsk["length"], "2048", "each sketch holds 2048 registers")


def test_a_table_naming_no_column_has_no_sketch(nsk, expect):
    expect.text(nsk["none"], "0 over 1 groups", "a table that names no column has no sketch rows")


def test_one_group_of_a_value_and_one_of_nulls(nsk, expect):
    expect.text(nsk["sk1 groups"], "1000,1000", "one group of a single value and one of NULLs was written")


def test_one_distinct_value_sets_one_register(nsk, expect):
    expect.text(nsk["one value"], "1", "a row group of one distinct value sets exactly one register")


def test_an_all_null_group_sets_no_register(nsk, expect):
    expect.text(nsk["all null"], "0", "an all-NULL row group sets no register")


def test_the_sketch_of_a_fixed_group_is_pinned(nsk, expect):
    expect.text(nsk["pin"], PIN, "the sketch of a fixed row group is pinned")


def test_the_registers_follow_the_stated_rule(nsk, expect):
    expect.text(nsk["pin"], nsk["oracle"], "the registers follow the stated rule, recomputed in SQL")


def test_the_parallel_insert_used_a_worker(nsk, expect):
    expect.num(nsk["par flushed"], 1, "premise: the parallel insert flushed columns in a worker")


def test_a_parallel_flush_writes_the_same_sketch(nsk, expect):
    expect.text(nsk["par"], PIN, "a parallel flush writes the same sketch as a serial one")


def test_the_projection_storage_holds_groups(nsk, expect):
    expect.text(nsk["proj groups"], "some", "premise: the projection's storage holds row groups")


def test_a_projection_gets_no_sketch(nsk, expect):
    expect.text(nsk["proj"], "0", "a projection's storage gets no sketch")


def test_a_rewrite_leaves_one_sketch_per_group(nsk, expect):
    got, want = nsk["rewrite"]
    expect.text(got, want, "a rewrite leaves one sketch per surviving row group")


def test_compact_rewrite_rewrote_groups(nsk, expect):
    expect.text(nsk["compacted"], "yes", "premise: compact_rewrite rewrote row groups")


def test_compact_rewrite_leaves_one_sketch_per_group(nsk, expect):
    got, want = nsk["compact"]
    expect.text(got, want, "compact_rewrite leaves one sketch per surviving row group")


def test_reset_options_clears_sketch_columns(nsk, expect):
    expect.text(nsk["reset"], "NULL", "reset_options clears sketch_columns")


def test_the_insert_after_the_reset_wrote_a_group(nsk, expect):
    expect.text(nsk["reset wrote"], "1", "premise: the insert after the reset wrote a row group")


def test_a_group_after_the_reset_gets_no_sketch(nsk, expect):
    got, want = nsk["after reset"]
    expect.text(got, want, "a row group written after the reset gets no sketch")


def test_truncate_leaves_no_sketch(nsk, expect):
    expect.text(nsk["truncate"], "0", "TRUNCATE leaves no sketch for the old storage")


def test_drop_leaves_no_sketch(nsk, expect):
    expect.text(nsk["drop"], "0", "DROP TABLE leaves no sketch behind")


def test_the_insert_around_the_aborted_block_wrote_every_row(nsk, expect):
    expect.text(nsk["kept rows"], "1000/1000", "premise: the insert around the aborted block wrote 1000 distinct rows")


def test_a_kept_flush_loses_no_row_from_the_sketch(nsk, expect):
    expect.text(nsk["kept union"], PIN, "a kept flush whose subtransaction aborted loses no row from the sketch")


def test_a_rename_follows_into_sketch_columns(nsk, expect):
    expect.text(nsk["renamed option"], "{a2}", "a rename follows into sketch_columns")


def test_a_group_after_a_rename_gets_its_sketch(nsk, expect):
    expect.text(nsk["renamed"], "2 for 2 groups", "a row group written after a rename still gets its sketch")


def test_a_group_from_before_the_option_has_no_sketch(nsk, expect):
    expect.text(nsk["before"], "1 for 2 groups", "a row group written before the option was set has no sketch row")


def test_no_sketch_outlives_its_row_group(nsk, expect):
    expect.text(nsk["orphans"], "0", "no sketch outlives its row group")

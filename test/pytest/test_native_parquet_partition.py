"""Hive-style partition pruning for the Parquet FDW (Phase G). The pytest twin of
`test/native_parquet_partition.sh`, every check by the shell's name.

A tree like events/dt=2026-01-02/region=eu/part.parquet carries column values in its
directory names, declared through the partition_columns option. Asserted: the values
materialize as typed columns; a predicate on a partition column drops whole files
before they are opened (the EXPLAIN counters, never timing); partition and file
predicates combine; a volatile clause never decides a whole file; a tree that does not
match the declaration fails loudly; and Hive value encoding (percent-escapes, the null
marker, a refused NUL or invalid byte) decodes as Hive and Spark write it.

The pruning counters are read fail-closed: EXPLAIN prints "Files Pruned" only when it
is above 0, so a missing line counts as 0 only when the plan's Files line shows the
scan ran; a failed reading is "no reading", never 0. Each refusal wants its own
guard's SQLSTATE and message, since any error used to pass: with the unknown-column
guard removed a later guard's 42804 stood in, and with the explicit encoded-NUL guard
removed the encoding check's 22021 did. Every reading waits for the server, and
survival also reads the server log's count of killed backends.

ON A PRIVATE DATABASE. The shell skips whole without pyarrow, so every test declines
under the shell's skip name before it asks for the fixture: the writer needs pyarrow,
and it runs as a subprocess so this module imports none. The trees live in
`server_dir`.
"""
import re
import shutil
import subprocess
import sys
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_parquet_partition"

# the same writers the shell runs
WRITER = r'''
import os, sys
import pyarrow as pa, pyarrow.parquet as pq
ROOT, ENC = sys.argv[1], sys.argv[2]
plan = [("2026-01-01", "eu", 0), ("2026-01-01", "us", 1000),
        ("2026-01-02", "eu", 2000), ("2026-01-02", "us", 3000)]
for dt, region, base in plan:
    d = os.path.join(ROOT, f"dt={dt}", f"region={region}")
    os.makedirs(d, exist_ok=True)
    t = pa.table({"id": pa.array(range(base, base + 1000), pa.int32()),
                  "v":  pa.array([f"r{i}" for i in range(base, base + 1000)])})
    pq.write_table(t, os.path.join(d, "part.parquet"), compression="none",
                   row_group_size=250)
plan = [("a%3Db", 0), ("__HIVE_DEFAULT_PARTITION__", 1000), ("plain", 2000)]
for region, base in plan:
    d = os.path.join(ENC, "region=" + region)
    os.makedirs(d, exist_ok=True)
    pq.write_table(pa.table({"id": pa.array(range(base, base + 100), pa.int32())}),
                   os.path.join(d, "part.parquet"), compression="none")
'''

# the refusal each arm wants, matched against the primary message as the shell does
MISSING = (r'has no "region=" directory component', "its own message")
UNKNOWN = (r'"nosuch" named in "partition_columns" does not exist', "its own message")
EMPTY = (r'"partition_columns" option cannot be empty', "its own message")
DUPCOL = (r'has no "v=" directory component', "its own message")
NUL = (r"contains an encoded null byte", "its own message")
BADBYTE = (r"invalid byte sequence for encoding", "its own message")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _wait_up(dsn):
    for _ in range(60):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _rows(dsn, sql, sep="|"):
    """-> the rows joined as the shell's psql -qtA prints them, or the failure as a
    query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join(sep.join("" if v is None else str(v) for v in row) for row in rows)


def _explain(dsn, where, table="ev"):
    return _rows(dsn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                      f"SELECT count(*) FROM {table} WHERE {where}")


def _counter(plan, name):
    for line in plan.splitlines():
        m = re.match(rf"\s*{re.escape(name)}:\s*(\d+)", line)
        if m:
            return m.group(1)
    return None


def _files_for(dsn, where, table="ev"):
    plan = _explain(dsn, where, table)
    n = _counter(plan, "Files")
    return n if n is not None else "no reading (no Files line)"


def _pruned_for(dsn, where, table="ev"):
    """-> files dropped before opening; "Files Pruned" prints only when above 0, so a
    missing line is 0 only when the Files line shows the scan ran."""
    plan = _explain(dsn, where, table)
    if _counter(plan, "Files") is None:
        return "no reading (no Files line)"
    n = _counter(plan, "Files Pruned")
    return n if n is not None else "0"


def _refusal(dsn, sql, want):
    """-> "<SQLSTATE> (<label>)" when the statement raised the wanted refusal,
    "<SQLSTATE>: <message>" for any other error, or what happened instead."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return "no refusal: the connection was lost"
        msg = exc.diag.message_primary or ""
        regex, label = want
        return f"{exc.sqlstate} ({label})" if re.search(regex, msg) else f"{exc.sqlstate}: {msg}"
    return "no error"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    """-> "1" when the server answers and no backend died since `before`."""
    _wait_up(dsn)
    up, now = _rows(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    return f"no ([{up if up == '1' else ''}], {now - before} backend crashes)"


def _opened(path):
    """Let the server, which may run as another user, traverse and read the tree."""
    for p in [path, *path.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o644)


@pytest.fixture(scope="module")
def ppart(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir / "ppart"
        shutil.rmtree(w, ignore_errors=True)
        w.mkdir()
        root, enc = w / "events", w / "enc"
        subprocess.run([sys.executable, "-c", WRITER, str(root), str(enc)], capture_output=True, text=True)
        _opened(w)
        r = {}
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
                conn.execute("CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
                conn.execute(f"CREATE FOREIGN TABLE ev (id int, v text, dt date, region text) "
                             f"SERVER pq OPTIONS (path '{root}', partition_columns 'dt,region')")
        except psycopg.Error as exc:
            r["setup"] = query_error(_message(exc))
        before = _crashes(pgc_cluster)

        # ---- the values are real columns
        r["all four"] = _rows(dsn, "SELECT count(*) FROM ev")
        r["materialize"] = _rows(dsn, "SELECT dt || ' ' || region FROM ev WHERE id = 2500")
        r["typed"] = _rows(dsn, "SELECT count(*) FROM ev WHERE dt = DATE '2026-01-01'")
        g = _rows(dsn, "SELECT region || '=' || count(*) FROM ev GROUP BY region ORDER BY region")
        r["group by"] = g if g.startswith("QUERY_ERROR") else " ".join(g.splitlines()) + " "

        # ---- pruning drops whole files before they are opened
        r["prunes half"] = _pruned_for(dsn, "dt = DATE '2026-01-02'")
        r["reads half"] = _files_for(dsn, "dt = DATE '2026-01-02'")
        r["both columns"] = _pruned_for(dsn, "dt = DATE '2026-01-02' AND region = 'eu'")
        r["right rows"] = _rows(dsn, "SELECT count(*), min(id), max(id) FROM ev "
                                     "WHERE dt = DATE '2026-01-02' AND region = 'eu'", sep=" ")
        r["inequality"] = _pruned_for(dsn, "dt > DATE '2026-01-01'")
        r["in list"] = _pruned_for(dsn, "region IN ('eu')")
        r["everything"] = _pruned_for(dsn, "dt >= DATE '2026-01-01'")
        r["file column"] = _pruned_for(dsn, "id < 10")
        ga = _counter(_rows(dsn, "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM ev"),
                      "Row Groups") or ""
        go = _counter(_explain(dsn, "dt = DATE '2026-01-02' AND region = 'eu'"), "Row Groups") or ""
        r["groups"] = f"{ga} {go}"

        # ---- partition and file predicates combine
        r["combine"] = _rows(dsn, "SELECT count(*) FROM ev WHERE region = 'eu' AND id BETWEEN 2100 AND 2199")
        r["groups skipped"] = _counter(_explain(dsn, "region = 'eu' AND id BETWEEN 2100 AND 2199"),
                                       "Row Groups Skipped") or ""

        # ---- a volatile clause must not decide a whole file
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE SEQUENCE volseq")
                conn.execute("CREATE FUNCTION vol_odd() RETURNS boolean LANGUAGE sql VOLATILE "
                             "AS $$ SELECT nextval('volseq') % 2 = 1 $$")
        except psycopg.Error as exc:
            r["volatile setup"] = query_error(_message(exc))
        r["volatile prunes"] = _pruned_for(dsn, "dt = DATE '2026-01-02' OR vol_odd()")
        r["volatile keeps"] = _rows(dsn, "SELECT count(*) FROM ev "
                                         "WHERE (dt = DATE '2026-01-02' OR vol_odd()) AND dt = DATE '2026-01-02'")
        r["stable"] = _pruned_for(dsn, "dt = (DATE '2026-01-02' + 0)")

        # ---- a tree that does not match the declaration fails loudly
        loose = root / "dt=2026-01-03"
        loose.mkdir()
        shutil.copy(root / "dt=2026-01-01" / "region=eu" / "part.parquet", loose / "loose.parquet")
        _opened(loose)
        r["missing"] = _refusal(dsn, "SELECT count(*) FROM ev", MISSING)
        shutil.rmtree(loose)

        decoy = root / "dt=2026-01-01" / "region=eu" / "dt=1999-01-01.parquet"
        shutil.copy(root / "dt=2026-01-01" / "region=eu" / "part.parquet", decoy)
        decoy.chmod(0o644)
        r["basename"] = _rows(dsn, "SELECT count(*) FROM ev WHERE dt = DATE '1999-01-01'")
        decoy.unlink()

        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(f"CREATE FOREIGN TABLE ev_bad (id int, v text, dt date, region text) "
                             f"SERVER pq OPTIONS (path '{root}', partition_columns 'dt,nosuch')")
                conn.execute(f"CREATE FOREIGN TABLE ev_dup (id int, v text, dt date, region text) "
                             f"SERVER pq OPTIONS (path '{root}', partition_columns 'dt,region,v')")
        except psycopg.Error as exc:
            r["declarations"] = query_error(_message(exc))
        r["unknown"] = _refusal(dsn, "SELECT count(*) FROM ev_bad", UNKNOWN)
        r["empty"] = _refusal(dsn, f"CREATE FOREIGN TABLE ev_empty (id int) SERVER pq "
                                   f"OPTIONS (path '{root}', partition_columns '')", EMPTY)
        r["dup column"] = _refusal(dsn, "SELECT count(*) FROM ev_dup", DUPCOL)
        r["survived declarations"] = _survived(dsn, pgc_cluster, before)

        # ---- Hive value encoding
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(f"CREATE FOREIGN TABLE ev_enc (id int, region text) "
                             f"SERVER pq OPTIONS (path '{enc}', partition_columns 'region')")
        except psycopg.Error as exc:
            r["enc setup"] = query_error(_message(exc))
        r["decodes"] = _rows(dsn, "SELECT DISTINCT region FROM ev_enc WHERE id < 100")
        r["as itself"] = _rows(dsn, "SELECT count(*) FROM ev_enc WHERE region = 'a=b'")
        r["null marker"] = _rows(dsn, "SELECT count(*) FROM ev_enc WHERE region IS NULL")
        r["not literal"] = _rows(dsn, "SELECT count(*) FROM ev_enc WHERE region = '__HIVE_DEFAULT_PARTITION__'")
        r["plain"] = _rows(dsn, "SELECT count(*) FROM ev_enc WHERE region = 'plain'")
        r["three"] = _rows(dsn, "SELECT count(*) FROM ev_enc")
        r["null prunes"] = _pruned_for(dsn, "region IS NOT NULL", "ev_enc")

        nul, ff = enc / "region=a%00b", enc / "region=a%FFb"
        for d in (nul, ff):
            d.mkdir()
            shutil.copy(enc / "region=plain" / "part.parquet", d / "part.parquet")
            _opened(d)
        r["nul"] = _refusal(dsn, "SELECT count(*) FROM ev_enc", NUL)
        shutil.rmtree(nul)
        r["bad byte"] = _refusal(dsn, "SELECT count(*) FROM ev_enc", BADBYTE)
        shutil.rmtree(ff)
        r["reads again"] = _rows(dsn, "SELECT count(*) FROM ev_enc")
        r["survived values"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_all_four_partitions_read(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["all four"], "4000", "all four partitions read")


def test_partition_values_materialize(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["materialize"], "2026-01-02 eu", "partition values materialize")


def test_a_partition_column_is_typed_not_text(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["typed"], "2000", "partition column is typed, not text")


def test_group_by_a_partition_column(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["group by"], "eu=2000 us=2000 ", "group by a partition column")


def test_one_partition_value_prunes_half_the_files(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["prunes half"], "2", "one partition value prunes half the files")


def test_and_reads_the_other_half(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["reads half"], "2", "and reads the other half")


def test_both_partition_columns_prune_down_to_one_file(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["both columns"], "3", "both columns prune down to one file")


def test_a_pruned_scan_still_returns_the_right_rows(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["right rows"], "1000 2000 2999", "pruned scan still returns the right rows")


def test_an_inequality_on_a_partition_column_prunes(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["inequality"], "2", "an inequality on a partition column prunes")


def test_in_on_a_partition_column_prunes(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["in list"], "2", "IN on a partition column prunes")


def test_a_predicate_matching_everything_prunes_nothing(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["everything"], "0", "a predicate matching everything prunes nothing")


def test_a_predicate_on_a_file_column_prunes_no_files(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["file column"], "0", "a predicate on a file column prunes no files")


def test_a_pruned_files_row_groups_are_never_counted(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["groups"], "16 4", "a pruned file's row groups are never counted")


def test_partition_prune_plus_row_group_skip(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["combine"], "100", "partition prune plus row-group skip")


def test_row_groups_are_still_skipped_inside_the_surviving_files(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["groups skipped"], "7", "row groups are still skipped inside the surviving files")


def test_a_volatile_clause_prunes_nothing(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["volatile prunes"], "0", "a volatile clause prunes nothing")


def test_a_volatile_clause_keeps_every_matching_partition_row(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["volatile keeps"], "2000", "a volatile clause keeps every matching partition row")


def test_a_stable_clause_still_prunes(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["stable"], "2", "a stable clause still prunes")


def test_a_file_missing_a_declared_partition_component_errors(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["missing"], "HV004 (its own message)", "a file missing a declared partition component errors")


def test_a_basename_that_looks_like_a_partition_component_is_unaffected(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["basename"], "0", "a file whose basename looks like a partition component is unaffected")


def test_an_unknown_partition_column_errors(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["unknown"], "42703 (its own message)", "an unknown partition column errors rather than being ignored")


def test_an_empty_partition_columns_option_is_rejected(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["empty"], "HV00D (its own message)", "an empty partition_columns option is rejected")


def test_declaring_a_file_column_as_a_partition_column_errors(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["dup column"], "HV004 (its own message)", "declaring a file column as a partition column errors")


def test_the_backend_survives_the_bad_declarations(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["survived declarations"], "1", "backend survived the bad declarations")


def test_a_percent_encoded_partition_value_decodes(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["decodes"], "a=b", "a percent-encoded partition value decodes")


def test_the_encoded_value_is_queryable_as_itself(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["as itself"], "100", "the encoded value is queryable as itself")


def test_hive_default_partition_reads_as_null(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["null marker"], "100", "__HIVE_DEFAULT_PARTITION__ reads as null")


def test_a_null_partition_value_is_not_the_literal_marker(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["not literal"], "0", "a null partition value is not the literal marker")


def test_an_unencoded_value_is_unaffected(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["plain"], "100", "an unencoded value is unaffected")


def test_all_three_encoded_partitions_still_read(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["three"], "300", "all three partitions still read")


def test_a_null_partition_value_prunes(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["null prunes"], "1", "a null partition value prunes")


def test_an_encoded_null_byte_in_a_partition_value_is_rejected(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["nul"], "HV004 (its own message)", "an encoded null byte in a partition value is rejected")


def test_a_byte_invalid_in_the_server_encoding_is_rejected(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["bad byte"], "22021 (its own message)", "a byte invalid in the server encoding is rejected")


def test_the_encoded_tree_reads_again_once_both_are_gone(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["reads again"], "300", "the tree reads again once both are gone")


def test_the_backend_survives_the_malformed_partition_values(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; partition suite needs it") is None:
        return
    r = request.getfixturevalue("ppart")
    expect.text(r["survived values"], "1", "backend survived the malformed partition values")

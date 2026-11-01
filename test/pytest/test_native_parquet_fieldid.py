"""read_parquet(path, field_ids) binds each output column to the file column carrying
the requested field id (#388 phase 3c). The pytest twin of
`test/native_parquet_fieldid.sh`, every check by the shell's name.

pyarrow, an independent writer, writes a file whose field ids are out of order and
disjoint from position (alpha=7, beta=3, gamma=12), so a reader that bound by position
or by name could not pass the projection arms. A second file has the shape Iceberg
writes for a struct, a list and a map, with their field ids on group nodes; asking for
one of those ids is refused as nested, naming the column and its kind, and not as an
id the file lacks (#1788). The refusal arms each want their own
guard's message, not only the SQLSTATE: four of them share 22023, and with the "no
field ids" guard removed an id-less file fell through to the absent-id refusal and
still passed. Every reading waits for the server first, and survival also reads the
server log's count of killed backends, since SELECT 1 answers again after crash
recovery.

ON A PRIVATE DATABASE. The shell skips whole without pyarrow, so every test declines
under the shell's skip name before it asks for the fixture: the writer needs pyarrow,
and it runs as a subprocess so this module imports none. The files live in
`server_dir`.
"""
import re
import subprocess
import sys
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_parquet_fieldid"

# the same writer the shell runs
WRITER = r'''
import re, sys, pyarrow as pa, pyarrow.parquet as pq
fid, noid, dup, nest, groups, leaves = sys.argv[1:7]
schema = pa.schema([
    pa.field("alpha", pa.int32(),  metadata={b"PARQUET:field_id": b"7"}),
    pa.field("beta",  pa.string(), metadata={b"PARQUET:field_id": b"3"}),
    pa.field("gamma", pa.int32(),  metadata={b"PARQUET:field_id": b"12"}),
])
pq.write_table(pa.table({
    "alpha": pa.array([10, 20, 30], pa.int32()),
    "beta":  pa.array(["x", "y", "z"], pa.string()),
    "gamma": pa.array([100, 200, 300], pa.int32()),
}, schema=schema), fid)
pq.write_table(pa.table({"n": pa.array([1, 2], pa.int32())}), noid)
dschema = pa.schema([
    pa.field("p", pa.int32(), metadata={b"PARQUET:field_id": b"5"}),
    pa.field("q", pa.int32(), metadata={b"PARQUET:field_id": b"5"}),
])
pq.write_table(pa.table({"p": pa.array([1], pa.int32()),
                         "q": pa.array([2], pa.int32())}, schema=dschema), dup)
F = lambda i: {b"PARQUET:field_id": str(i).encode()}
nschema = pa.schema([
    pa.field("id", pa.int64(), metadata=F(1)),
    pa.field("st", pa.struct([pa.field("a", pa.int32(), metadata=F(5)),
                              pa.field("b", pa.string(), metadata=F(6))]), metadata=F(2)),
    pa.field("li", pa.list_(pa.field("element", pa.int32(), metadata=F(7))), metadata=F(3)),
    pa.field("mp", pa.map_(pa.field("key", pa.string(), nullable=False, metadata=F(8)),
                           pa.field("value", pa.int64(), metadata=F(9))), metadata=F(4)),
])
pq.write_table(pa.Table.from_pylist(
    [{"id": 1, "st": {"a": 1, "b": "x"}, "li": [1, None, 3], "mp": [("k", 1)]}],
    schema=nschema), nest)
found = re.findall(r"group field_id=(\d+) (\w+)", str(pq.ParquetFile(nest).schema))
open(groups, "w").write(" ".join(f"{n}={i}" for i, n in found) + "\n")
leaf_ids = re.findall(r"^\s*(?:optional|required|repeated) (?!group\b)\w+ field_id=(-?\d+) ",
                      str(pq.ParquetFile(nest).schema), re.M)
open(leaves, "w").write(",".join(str(i) for i in sorted(int(x) for x in leaf_ids)) + "\n")
'''

# the refusal each arm wants, matched against the primary message as the shell does
ABSENT = (r"no Parquet column has field id 999", "absent id")
DUPFILE = (r"field id 5 is not unique among", "duplicate in the file")
SCALAR = (r"supports only scalar columns", "not scalar")
COUNT = (r"field_ids has 2 element\(s\) but the column definition list has 1 column", "count mismatch")
NOIDS = (r"carries no field ids", "no field ids")
DUPREQ = (r"field id 7 is requested more than once", "requested twice")
NSTRUCT = (r'field id 2 \(requested for output column "st"\) is a nested struct column', "nested struct")
NLIST = (r'field id 3 \(requested for output column "li"\) is a nested list column', "nested list")
NMAP = (r'field id 4 \(requested for output column "mp"\) is a nested map column', "nested map")
NABSENT = (r"no Parquet column has field id -1", "absent id")


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


def _rows(dsn, sql):
    """-> the rows joined as the shell's psql -qtA prints them, or the failure as a
    query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join("|".join("" if v is None else str(v) for v in row) for row in rows)


def _refusal(dsn, sql, want):
    """-> "<SQLSTATE> (<label>)" when the statement raised the wanted refusal,
    "<SQLSTATE>: <message>" for any other error, or what happened instead."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return "no refusal: the connection was lost"
        msg = exc.diag.message_primary or ""
        regex, label = want
        return f"{exc.sqlstate} ({label})" if re.search(regex, msg) else f"{exc.sqlstate}: {msg}"
    return "no error"


def _names_the_id(dsn, sql):
    """-> how many lines of the error text say the id was requested more than once,
    as the shell's grep -c does over psql's output."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return str(sum(1 for line in str(exc).splitlines() if "requested more than once" in line))
    return "0"


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


@pytest.fixture(scope="module")
def pfid(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir / "pfid"
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        fid, noid, dup = w / "fieldid.parquet", w / "noid.parquet", w / "dup.parquet"
        nest, groups, leaves = w / "nested.parquet", w / "nested_groups.txt", w / "nested_leaves.txt"
        subprocess.run([sys.executable, "-c", WRITER, str(fid), str(noid), str(dup),
                        str(nest), str(groups), str(leaves)],
                                 capture_output=True, text=True)
        for f in (fid, noid, dup, nest):
            if f.exists():
                f.chmod(0o644)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        r["reorder"] = _rows(dsn, f"SELECT g || '|' || a FROM pgcolumnar.read_parquet('{fid}', ARRAY[12,7]) "
                                  f"AS t(g int, a int) ORDER BY a")
        r["subset"] = _rows(dsn, f"SELECT b FROM pgcolumnar.read_parquet('{fid}', ARRAY[3]) AS t(b text) ORDER BY b")
        r["positional"] = _rows(dsn, f"SELECT alpha || '|' || beta || '|' || gamma FROM pgcolumnar.read_parquet('{fid}') "
                                     f"AS t(alpha int, beta text, gamma int) ORDER BY alpha")
        before = _crashes(pgc_cluster)
        r["absent"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{fid}', ARRAY[999]) AS t(x int)", ABSENT)
        r["dup file"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{dup}', ARRAY[5]) AS t(x int)", DUPFILE)
        r["nested"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{fid}', ARRAY[7]) AS t(a int[])", SCALAR)
        r["count"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{fid}', ARRAY[7,3]) AS t(a int)", COUNT)
        r["no ids"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{noid}', ARRAY[1]) AS t(x int)", NOIDS)
        dupreq = f"SELECT * FROM pgcolumnar.read_parquet('{fid}', ARRAY[7,7]) AS t(a1 int, a2 int)"
        r["dup req"] = _refusal(dsn, dupreq, DUPREQ)
        r["names id"] = _names_the_id(dsn, dupreq)
        # pyarrow reads the leaf ids back: parquet_schema reports a nested column's own
        # (group) id since #1893, not its leaves' ids
        try:
            r["nest leaves"] = leaves.read_text().strip()
        except OSError as exc:
            r["nest leaves"] = query_error(f"the leaf-id record is unreadable: {exc}")
        try:
            r["nest groups"] = groups.read_text().strip()
        except OSError as exc:
            r["nest groups"] = query_error(f"the group-id record is unreadable: {exc}")
        r["nest top ids"] = _rows(dsn, "SELECT string_agg(column_name || '=' || coalesce(field_id::text, 'NULL'), ' ' "
                                       "ORDER BY ord) FROM (SELECT column_name, field_id, row_number() OVER () AS ord "
                                       f"FROM pgcolumnar.parquet_schema('{nest}')) s")
        r["nest scalar"] = _rows(dsn, f"SELECT id FROM pgcolumnar.read_parquet('{nest}', ARRAY[1]) AS t(id int8)")
        r["nest struct"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{nest}', ARRAY[1,2]) "
                                         f"AS t(id int8, st text)", NSTRUCT)
        r["nest list"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{nest}', ARRAY[3]) AS t(li int4)", NLIST)
        r["nest map"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{nest}', ARRAY[4]) AS t(mp int8)", NMAP)
        # the list's and map's inner groups carry no id (-1): -1 must still be absent
        r["nest minus one"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{nest}', ARRAY[-1]) AS t(x int4)",
                                       NABSENT)
        r["survived"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_fieldid_projection_binds_and_reorders_by_id(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["reorder"], "100|10\n200|20\n300|30", "field-id projection binds and reorders by id (g=gamma, a=alpha)")


def test_fieldid_projection_reads_a_subset(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["subset"], "x\ny\nz", "field-id projection reads a subset (only id 3 = beta)")


def test_positional_read_parquet_is_unaffected_by_fieldid_binding(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["positional"], "10|x|100\n20|y|200\n30|z|300", "positional read_parquet is unaffected")


def test_an_absent_field_id_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["absent"], "22023 (absent id)", "an absent field id is refused (22023)")


def test_a_duplicate_field_id_in_the_parquet_file_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["dup file"], "42702 (duplicate in the file)", "a duplicate field id in the file is refused (42702)")


def test_a_nested_fieldid_output_column_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nested"], "0A000 (not scalar)", "a nested/array output column is refused (0A000)")


def test_a_field_ids_column_count_mismatch_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["count"], "22023 (count mismatch)", "a field_ids/column-count mismatch is refused (22023)")


def test_a_parquet_file_with_no_field_ids_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["no ids"], "22023 (no field ids)", "a file with no field ids is refused (22023)")


def test_a_duplicate_requested_field_id_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["dup req"], "22023 (requested twice)", "a duplicate requested field id is refused (22023)")


def test_the_duplicate_requested_id_error_names_the_id(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["names id"], "1", "the duplicate-requested-id error names the id, not the file")


def test_the_nested_files_leaves_carry_only_the_child_ids(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest leaves"], "1,5,6,7,8,9", "premise: the nested file's leaves carry ids 1,5,6,7,8,9 and none of 2,3,4")


def test_the_nested_files_groups_carry_the_nested_ids(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest groups"], "st=2 li=3 mp=4", "premise: the nested file's groups carry ids st=2 li=3 mp=4")


def test_parquet_schema_reports_each_nested_columns_own_field_id(request, expect):
    # one row per top-level column (#1893): a nested column carries its group's id
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest top ids"], "id=1 st=2 li=3 mp=4",
                "parquet_schema reports each nested column's own field id, 1,2,3,4 (#1893)")


def test_a_scalar_beside_nested_columns_still_reads_by_field_id_premise(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest scalar"], "1", "premise: a scalar beside nested columns still reads by field id")


def test_a_struct_field_id_read_as_text_is_refused_as_nested(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest struct"], "0A000 (nested struct)", "a struct field id read as text is refused as nested (0A000)")


def test_a_list_field_id_read_as_int4_is_refused_as_nested(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest list"], "0A000 (nested list)", "a list field id read as int4 is refused as nested (0A000)")


def test_a_map_field_id_read_as_int8_is_refused_as_nested(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest map"], "0A000 (nested map)", "a map field id read as int8 is refused as nested (0A000)")


def test_field_id_minus_one_over_the_nested_file_is_refused_as_absent(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["nest minus one"], "22023 (absent id)", "field id -1 over the nested file is refused as absent, not as a group without an id (22023)")


def test_the_backend_survives_the_fieldid_refusals(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow is needed to write the field-id fixture") is None:
        return
    r = request.getfixturevalue("pfid")
    expect.text(r["survived"], "1", "backend still up after the refusals")

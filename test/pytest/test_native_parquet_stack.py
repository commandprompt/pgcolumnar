"""Parquet footer recursion-depth hardening (#1341). The pytest twin of
`test/native_parquet_stack.sh`, every check by the shell's name.

The footer parser has two independent unbounded recursions, each driven by a depth
read straight from the file: PgColumnarThriftSkip (reached from the raw footer) and
walk_schema (reached only after the footer parses). Each is guarded by
check_stack_depth(), which turns a would-be SIGSEGV into a caught ERROR 54001. The
fixture pins max_stack_depth to its minimum so a ~20 KB crafted footer trips the
guard without overflowing the real C stack, so removing a guard fails the check by a
wrong SQLSTATE rather than by crashing. The footers are hand-crafted (no writer makes
them), so no pyarrow: struct and the compact-protocol bytes build them directly.
"""
import struct

import psycopg

from pgc_vacuity import query_error


def _uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _wrap(meta):
    return b"PAR1" + meta + struct.pack("<I", len(meta)) + b"PAR1"


def _nested(n):
    # n compact-protocol struct openers (0x1c): nest n deep through PgColumnarThriftSkip.
    return _wrap(b"\x1c" * n)


def _walk_chain(n):
    # a schema list of n elements, each a group with one child but the leaf: parses
    # flat, then walk_schema descends the chain n-1 deep.
    m = bytearray(b"\x29\xfc")
    m += _uleb(n)
    m += b"\x55\x02\x00" * (n - 1)
    m += b"\x00"   # leaf element
    m += b"\x00"   # FileMetaData STOP
    return _wrap(bytes(m))


def _write(server_dir):
    (server_dir / "stack_nested.parquet").write_bytes(_nested(20000))
    (server_dir / "stack_walk.parquet").write_bytes(_walk_chain(20000))
    (server_dir / "stack_walk_ok.parquet").write_bytes(_walk_chain(2))
    for f in ("stack_nested.parquet", "stack_walk.parquet", "stack_walk_ok.parquet"):
        (server_dir / f).chmod(0o644)


def _sqlstate(dsn, fn, path):
    """-> the SQLSTATE `fn(path)` raises under a pinned stack depth, 'NOERR' if it
    raises nothing, or a query_error() if the server could not be reached. The call is
    caught in-session, so only a real crash drops the connection, which the next
    survival check sees. A failed connection is a value, never an exception, so two
    failures cannot agree as a bare string."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET max_stack_depth = '100kB'")
            try:
                conn.execute(f"SELECT count(*) FROM pgcolumnar.{fn}('{path}')")
                return "NOERR"
            except psycopg.Error as exc:
                return exc.sqlstate or query_error(str(exc).splitlines()[0])
    except psycopg.Error as exc:
        return query_error(f"server unreachable: {str(exc).splitlines()[0]}")


def _alive(dsn):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            return str(conn.execute("SELECT 1").fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _count(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            return str(conn.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def test_nested_struct_footer_raises_stack_depth(pgc_cluster, server_dir, expect):
    _write(server_dir)
    dsn = pgc_cluster.dsn()
    expect.text(_sqlstate(dsn, "parquet_schema", server_dir / "stack_nested.parquet"), "54001",
                "nested-struct footer raises stack-depth, not SIGSEGV")


def test_backend_survived_the_nested_struct_footer(pgc_cluster, server_dir, expect):
    _write(server_dir)
    _sqlstate(pgc_cluster.dsn(), "parquet_schema", server_dir / "stack_nested.parquet")
    expect.text(_alive(pgc_cluster.dsn()), "1", "backend survived the nested-struct footer")


def test_schema_chain_footer_raises_stack_depth(pgc_cluster, server_dir, expect):
    _write(server_dir)
    expect.text(_sqlstate(pgc_cluster.dsn(), "parquet_schema", server_dir / "stack_walk.parquet"), "54001",
                "schema-chain footer raises stack-depth, not SIGSEGV")


def test_backend_survived_the_schema_chain_footer(pgc_cluster, server_dir, expect):
    _write(server_dir)
    _sqlstate(pgc_cluster.dsn(), "parquet_schema", server_dir / "stack_walk.parquet")
    expect.text(_alive(pgc_cluster.dsn()), "1", "backend survived the schema-chain footer")


def test_a_shallow_valid_schema_chain_still_reads(pgc_cluster, server_dir, expect):
    _write(server_dir)
    expect.text(_count(pgc_cluster.dsn(),
                       f"SELECT count(*) FROM pgcolumnar.parquet_schema('{server_dir}/stack_walk_ok.parquet')"),
                "1", "a shallow valid schema chain still reads")


def test_backend_survived_the_valid_file(pgc_cluster, server_dir, expect):
    _write(server_dir)
    _count(pgc_cluster.dsn(), f"SELECT count(*) FROM pgcolumnar.parquet_schema('{server_dir}/stack_walk_ok.parquet')")
    expect.text(_alive(pgc_cluster.dsn()), "1", "backend survived the valid file")


def test_read_parquet_rejects_the_nested_struct_footer(pgc_cluster, server_dir, expect):
    _write(server_dir)
    # read_parquet reaches the same footer walk; it must refuse the crash input, not
    # become a way around the guard. count(*) over a one-int projection reaches the walk.
    try:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET max_stack_depth = '100kB'")
            try:
                conn.execute("SELECT count(*) FROM pgcolumnar.read_parquet"
                             f"('{server_dir}/stack_nested.parquet') AS t(c int)")
                st = "NOERR"
            except psycopg.Error as exc:
                st = exc.sqlstate or query_error(str(exc).splitlines()[0])
    except psycopg.Error as exc:
        st = query_error(f"server unreachable: {str(exc).splitlines()[0]}")
    expect.text(st, "54001", "read_parquet rejects the nested-struct footer the same way")


def test_backend_survived_read_parquet_on_the_crash_input(pgc_cluster, server_dir, expect):
    _write(server_dir)
    expect.text(_alive(pgc_cluster.dsn()), "1", "backend survived read_parquet on the crash input")

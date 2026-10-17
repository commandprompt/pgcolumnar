"""Multi-file and directory Parquet reads (Phase G, #1341). The pytest twin of
`test/native_parquet_multifile.sh`, every check by the shell's name.

A directory reads every *.parquet below it as one relation, a glob expands the same
way, and the union must equal a single-file oracle through read_parquet,
import_parquet and the FDW. Per-file pushdown and the EXPLAIN counters aggregate
across files. An empty directory, a glob matching nothing, a schema-mismatched file,
a dangling symlink and a tree past the depth bound must each fail; a FIFO and a
directory named *.parquet are skipped; the walk skips _ and . names, does not follow
a symlinked directory (so a loop terminates), still follows a symlinked file, and
orders files by full path.
"""
import os
import shutil
import threading

import psycopg

from pgc_vacuity import query_error

COLS = "id int, v int8"
HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")


def _hash(cur, sql):
    try:
        cur.execute(HASH.format(sql))
        return cur.fetchone()[0]
    except psycopg.Error as exc:
        return query_error(f"{exc.diag.message_primary} [{sql}]")


def _one(cur, sql):
    try:
        cur.execute(sql)
        return str(cur.fetchone()[0])
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _errs(cur, sql):
    """-> OK when `sql` raises, as the shell's errs counts any ERROR."""
    try:
        cur.execute(sql)
        cur.fetchall()
        return "NO ERROR"
    except psycopg.Error:
        return "OK"


def _plan(cur, sql):
    """-> the EXPLAIN ANALYZE lines, or the error as the one line: a broken read names its red."""
    try:
        cur.execute(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}")
        return [r[0] for r in cur.fetchall()]
    except psycopg.Error as exc:
        return [query_error(exc.diag.message_primary)]


def _counter(cur, where, label):
    for line in _plan(cur, f"SELECT count(*) FROM ftd WHERE {where}"):
        if label in line:
            digits = "".join(ch if ch.isdigit() else " " for ch in line.split(label, 1)[1]).split()
            return digits[0] if digits else ""
    return ""


def _cols_read(cur, sql):
    for line in _plan(cur, sql):
        if "Columns Read:" in line:
            return "".join(ch if ch.isdigit() else " " for ch in line.split("Columns Read:", 1)[1]).split()[0]
    return ""


def _rp(path):
    return f"SELECT * FROM pgcolumnar.read_parquet('{path}') AS t({COLS})"


def _count(path):
    return f"SELECT count(*) FROM pgcolumnar.read_parquet('{path}') AS t({COLS})"


def _bounded(cluster, sql, seconds, fifo=None):
    """-> the first value of `sql` on its own connection, or TIMED OUT after `seconds`.

    The shell runs psql under `timeout`; a cancel is no substitute, because a backend
    blocked in open(2) on a FIFO resumes the open after a signal (SA_RESTART) rather
    than failing. So the query runs in a thread and is waited for. When it times out
    on a FIFO, opening the FIFO for writing releases the blocked reader, so the
    backend and the cluster's teardown are not left hanging.
    """
    out = []

    def run():
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True) as c:
                out.append(str(c.execute(sql).fetchone()[0]))
        except psycopg.Error as exc:
            out.append(query_error(exc.diag.message_primary))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        if fifo is not None:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            t.join(30)
        return "TIMED OUT"
    return out[0] if out else "no result"


def test_directories_and_globs_read_as_one_relation(pgc_conn, pgc_cluster, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; multi-file suite needs it")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow not available; multi-file suite needs it")
    w = server_dir
    parts = w / "parts"
    parts.mkdir()

    def tbl(lo, hi):
        return pa.table({"id": pa.array(range(lo, hi), pa.int32()),
                         "v": pa.array([i * 2 for i in range(lo, hi)], pa.int64())})

    for i in range(3):
        pq.write_table(tbl(i * 1000, i * 1000 + 1000), str(parts / f"part-{i}.parquet"), row_group_size=1000)
    (parts / "README.txt").write_text("not parquet\n")
    pq.write_table(tbl(0, 3000), str(w / "all.parquet"), row_group_size=1000)
    for p in list(parts.iterdir()) + [w / "all.parquet"]:
        p.chmod(0o644)

    with pgc_conn.cursor() as cur:
        cur.execute("CREATE SERVER IF NOT EXISTS pq_multi FOREIGN DATA WRAPPER pgcolumnar_parquet")
        oracle = _hash(cur, _rp(w / "all.parquet"))
        expect.text(_hash(cur, _rp(parts)), oracle, "read_parquet(directory) == oracle")
        cur.execute(f"CREATE TABLE imp ({COLS})")
        _one(cur, f"SELECT pgcolumnar.import_parquet('imp'::regclass, '{parts}')")
        expect.text(_hash(cur, "SELECT * FROM imp"), oracle, "import_parquet(directory) == oracle")
        expect.text(_one(cur, "SELECT count(*) FROM imp"), "3000", "import_parquet(directory) row count")
        cur.execute(f"CREATE FOREIGN TABLE ftd ({COLS}) SERVER pq_multi OPTIONS (path '{parts}')")
        expect.text(_hash(cur, "SELECT * FROM ftd"), oracle, "FDW(directory) == oracle")
        expect.text(_one(cur, "SELECT count(*) FROM ftd"), "3000", "directory read ignores non-parquet files")
        expect.text(_hash(cur, _rp(f"{parts}/part-[01].parquet")),
                    _hash(cur, _rp(w / "all.parquet") + " WHERE id < 2000"), "glob part-[01] == first two files")

        # id >= 2500 lies only in part-2: the other two files' groups skip
        expect.text(_counter(cur, "true", "Files:"), "3", "EXPLAIN reports 3 files")
        expect.text(_counter(cur, "true", "Row Groups:"), "3", "EXPLAIN sums row groups across files")
        expect.text(_counter(cur, "id >= 2500", "Row Groups Skipped"), "2",
                    "predicate skips the two non-matching files' groups")
        expect.text(_hash(cur, "SELECT * FROM ftd WHERE id >= 2500"),
                    _hash(cur, _rp(w / "all.parquet") + " WHERE id >= 2500"), "cross-file predicate result == oracle")
        expect.text(_cols_read(cur, "SELECT count(*) FROM ftd"), "0", "count(*) over directory reads 0 columns")
        expect.text(_cols_read(cur, "SELECT id FROM ftd"), "1", "one column over directory reads 1")
        expect.text(_one(cur, "SELECT count(*) FROM ftd"), "3000", "count(*) over directory is correct")

        # ---- error and skip cases
        (w / "empty").mkdir()
        expect.text(_errs(cur, _rp(w / "empty")), "OK", "empty directory errors")
        expect.text(_errs(cur, _rp(f"{parts}/nope-*.parquet")), "OK", "non-matching glob errors")
        mixed = w / "mixed"
        mixed.mkdir()
        shutil.copy(parts / "part-0.parquet", mixed / "a.parquet")
        pq.write_table(pa.table({"x": pa.array(["a", "b"])}), str(mixed / "b.parquet"))
        for p in mixed.iterdir():
            p.chmod(0o644)
        expect.text(_errs(cur, _rp(mixed)), "OK", "schema-mismatched file in directory errors")

        (parts / "sub.parquet").mkdir()
        expect.text(_one(cur, "SELECT count(*) FROM ftd"), "3000", "directory ignores a subdir named *.parquet")
        (parts / "sub.parquet").rmdir()
        (parts / "part-9.parquet").mkdir()
        expect.text(_hash(cur, _rp(f"{parts}/part-*.parquet")), oracle, "glob skips a directory match, reads the files")
        (parts / "part-9.parquet").rmdir()

        onlydirs = w / "onlydirs"
        (onlydirs / "a.parquet").mkdir(parents=True)
        (onlydirs / "b.parquet").mkdir()
        expect.text(_errs(cur, _rp(f"{onlydirs}/*.parquet")), "OK", "glob matching only directories errors")
        expect.text(_errs(cur, _rp(onlydirs)), "OK", "directory of only *.parquet subdirs errors")

        # a path that cannot be stat'ed is reported, never dropped
        os.symlink(parts / "does-not-exist.parquet", parts / "broken.parquet")
        expect.text(_errs(cur, _count(parts)), "OK", "dangling symlink in directory errors, not skipped")
        expect.text(_errs(cur, _count(f"{parts}/*.parquet")), "OK", "dangling symlink in a glob errors, not skipped")
        (parts / "broken.parquet").unlink()



def test_a_fifo_named_parquet_is_skipped_not_opened(pgc_cluster, server_dir, expect):
    """Its own test because the shell declines this one check when mkfifo is unavailable,
    and cannot_run declines a whole test."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; multi-file suite needs it")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow not available; multi-file suite needs it")
    parts = server_dir / "fifoparts"
    parts.mkdir()
    for i in range(3):
        path = parts / f"part-{i}.parquet"
        pq.write_table(pa.table({"id": pa.array(range(i * 1000, i * 1000 + 1000), pa.int32()),
                                 "v": pa.array([j * 2 for j in range(i * 1000, i * 1000 + 1000)], pa.int64())}),
                       str(path), row_group_size=1000)
        path.chmod(0o644)
    try:
        os.mkfifo(parts / "pipe.parquet")
    except OSError as exc:
        expect.cannot_run("MISSING_DEPENDENCY", f"mkfifo unavailable ({exc})", name="the FIFO case")
        return
    (parts / "pipe.parquet").chmod(0o644)
    # a FIFO blocks open(2) until a writer appears; the bound is the assertion
    expect.text(_bounded(pgc_cluster, _count(parts), 30, fifo=parts / "pipe.parquet"), "3000",
                "FIFO named *.parquet is skipped, does not block")


def test_the_recursive_walk(pgc_conn, pgc_cluster, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; multi-file suite needs it")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow not available; multi-file suite needs it")
    w = server_dir

    def part(i, path):
        pq.write_table(pa.table({"id": pa.array(range(i * 1000, i * 1000 + 1000), pa.int32()),
                                 "v": pa.array([j * 2 for j in range(i * 1000, i * 1000 + 1000)], pa.int64())}),
                       str(path), row_group_size=1000)
        path.chmod(0o644)

    tree = w / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "c").mkdir()
    part(0, tree / "a" / "part-0.parquet")
    part(1, tree / "a" / "b" / "part-1.parquet")
    part(2, tree / "c" / "part-2.parquet")
    (tree / "a" / "b" / "notes.txt").write_text("not parquet\n")
    part(0, w / "p0.parquet")
    with pgc_conn.cursor() as cur:
        oracle = _hash(cur, " UNION ALL ".join(
            f"SELECT g AS id, (g * 2)::int8 AS v FROM generate_series({lo}, {lo + 999}) g" for lo in (0, 1000, 2000)))
        expect.text(_hash(cur, _rp(tree)), oracle, "nested tree reads every level as one relation")
        expect.text(_one(cur, _count(tree)), "3000", "nested tree row count")

        # _temporary and . names are not walked; the file below _temporary is real parquet
        (tree / "_temporary" / "task-0").mkdir(parents=True)
        (tree / ".hidden").mkdir()
        shutil.copy(w / "p0.parquet", tree / "_temporary" / "task-0" / "in-progress.parquet")
        shutil.copy(w / "p0.parquet", tree / ".hidden" / "hidden.parquet")
        (tree / "_SUCCESS").touch()
        expect.text(_one(cur, _count(tree)), "3000", "_temporary and dot directories are not walked")
        shutil.copy(w / "p0.parquet", tree / "c" / "_part.parquet")
        expect.text(_one(cur, _count(tree)), "3000", "a file whose name starts with _ is skipped too")
        for p in (tree / "_temporary", tree / ".hidden"):
            shutil.rmtree(p)
        for p in (tree / "_SUCCESS", tree / "c" / "_part.parquet"):
            p.unlink()

        # sorted by full path: a/b/part-1, a/part-0, c/part-2, so part-1's rows come first
        try:
            cur.execute(f"SELECT id FROM pgcolumnar.read_parquet('{tree}') AS t({COLS}) LIMIT 3")
            first = " ".join(str(r[0]) for r in cur.fetchall()) + " "
        except psycopg.Error as exc:
            first = query_error(exc.diag.message_primary)
        expect.text(first, "1000 1001 1002 ",
                    "tree read order follows the sorted full path")

        # a symlink to an ancestor: the bound is the assertion
        os.symlink(tree, tree / "a" / "loop")
        expect.text(_bounded(pgc_cluster, _count(tree), 60), "3000",
                    "a symlink loop terminates and does not multiply rows")
        (tree / "a" / "loop").unlink()

        elsewhere = w / "elsewhere"
        elsewhere.mkdir()
        shutil.copy(w / "p0.parquet", elsewhere / "extra.parquet")
        os.symlink(elsewhere, tree / "linkdir")
        expect.text(_one(cur, _count(tree)), "3000", "a symlinked directory is not descended")
        (tree / "linkdir").unlink()
        os.symlink(elsewhere / "extra.parquet", tree / "c" / "linked.parquet")
        expect.text(_one(cur, _count(tree)), "4000", "a symlinked file is still read")
        (tree / "c" / "linked.parquet").unlink()

        # past the bound it errors rather than reading the part it walked
        d = w / "deep"
        for i in range(1, 41):
            d = d / f"l{i}"
        d.mkdir(parents=True)
        shutil.copy(w / "p0.parquet", d / "part-0.parquet")
        expect.text(_errs(cur, _count(w / "deep")), "OK", "a tree deeper than the bound is rejected, not truncated")

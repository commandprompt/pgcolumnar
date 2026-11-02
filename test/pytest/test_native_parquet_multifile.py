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
import time

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


def _sqlstate(cluster, sql, options=None):
    """-> the SQLSTATE `sql` fails with on its own connection, "ok" when it succeeds,
    or "no answer" when the connection is lost, as the shell's st reads it."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, options=options or "") as c:
            c.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or "no answer"
    return "ok"


def _answer(conn, sql):
    """-> the value `sql` returns on `conn`, or the SQLSTATE it fails with."""
    try:
        return str(conn.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return exc.sqlstate or "no answer"


def _wait_up(cluster):
    """Return once the server answers SELECT 1, or after 60 s."""
    for _ in range(60):
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
                c.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(1)


def _up(cluster):
    """-> "1" when the server answers SELECT 1 now, else why not."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            return str(c.execute("SELECT 1").fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def test_a_local_glob_is_bounded_and_read_in_its_folded_form(pgc_conn, pgc_cluster, server_dir, expect):
    """#1977 review. The reader bounds and globs a local pattern in a folded form:
    a run of '/' becomes one '/', and a '.' name with a '/' after it is dropped.
    The kernel reads both spellings as the same file, so the rows do not change.
    A '.' at the end is kept, as it makes the pattern match only directories, and
    so is a name followed by '..', as a symbolic link before it leads the kernel
    somewhere else."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; multi-file suite needs it")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow not available; multi-file suite needs it")
    w = server_dir / "foldglob"
    parts = w / "parts"
    parts.mkdir(parents=True)

    def tbl(lo, hi):
        return pa.table({"id": pa.array(range(lo, hi), pa.int32()),
                         "v": pa.array([i * 2 for i in range(lo, hi)], pa.int64())})

    pq.write_table(tbl(0, 1000), str(parts / "part-0.parquet"), row_group_size=1000)
    # fold/ln leads to fold/away/inner, so fold/ln/.. is fold/away to the kernel.
    # fold/away/z-0.parquet holds ids 0..999; fold/z-0.parquet, the file a
    # lexical fold of ln/.. would read, holds ids 1000..1999.
    (w / "fold" / "away" / "inner").mkdir(parents=True)
    pq.write_table(tbl(0, 1000), str(w / "fold" / "away" / "z-0.parquet"), row_group_size=1000)
    pq.write_table(tbl(1000, 2000), str(w / "fold" / "z-0.parquet"), row_group_size=1000)
    (w / "fold" / "ln").symlink_to(w / "fold" / "away" / "inner")
    for p in (parts / "part-0.parquet", w / "fold" / "away" / "z-0.parquet", w / "fold" / "z-0.parquet"):
        p.chmod(0o644)

    def rp(expr):
        return f"SELECT * FROM pgcolumnar.read_parquet({expr}) AS t({COLS})"

    def cnt(expr):
        return f"SELECT count(*) FROM pgcolumnar.read_parquet({expr}) AS t({COLS})"

    with pgc_conn.cursor() as cur:
        plain = _hash(cur, _rp(parts / "part-0.parquet"))
        fold_dot = f"'{w}/par[t]s' || repeat('/.', 200) || '/part-0.parquet'"
        expect.text(f"{_one(cur, cnt(fold_dot))}|{_hash(cur, rp(fold_dot))}", f"1000|{plain}",
                    "read_parquet reads a local glob over 128 levels as written but not once its '.' names "
                    "are dropped, the same rows as the plain path")
        fold_slash = f"'{w}/par[t]s' || repeat('/', 201) || 'part-0.parquet'"
        expect.text(f"{_one(cur, cnt(fold_slash))}|{_hash(cur, rp(fold_slash))}", f"1000|{plain}",
                    "read_parquet reads a local glob over 128 levels as written but not once its runs of '/' "
                    "are folded, the same rows as the plain path")
        expect.text(_errcode(cur, cnt(f"'{w}/par[t]s/part-0.parquet/.'")), "58P01",
                    "read_parquet keeps a '.' name at the end of a local glob, so a file followed by /. "
                    "matches nothing")
        # glob(3) matches a pattern that ends in '/' against a file, so dropping a
        # '.' name that only '/' or '\/' follow would read the file.
        ends = [_errcode(cur, cnt(f"'{w}/par[t]s/part-0.parquet{tail}'")) for tail in ("/./", "/.//", "/./\\/")]
        expect.text("|".join(ends), "58P01|58P01|58P01",
                    "read_parquet keeps a '.' name that only '/' follow at the end of a local glob, so a file "
                    "followed by /./ matches nothing")
        expect.text(_one(cur, f"SELECT sum(id) FROM pgcolumnar.read_parquet('{w}/fold/ln/../z-[0].parquet') "
                              f"AS t({COLS})"), "499500",
                    "read_parquet does not fold a name followed by '..' in a local glob, so it reads past a "
                    "symbolic link as the kernel does")
    # The pattern has 1991 levels as written and 1 once folded, so the bound lets
    # it through. glob(3) of the form as written did not crash in a probe on glibc
    # 2.43 (2022 levels, #1977 review), so this check does not tell which form
    # glob(3) expands; it holds that the read is not refused.
    deep = f"'{w}/par[t]s' || repeat('/.', 1990) || '/part-0.parquet'"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        got = _answer(c, cnt(deep))
    expect.text(f"{got}|{_up(pgc_cluster)}", "1000|1",
                "read_parquet reads a local glob of 1990 '.' levels in its folded form, and the server stays up")
    _wait_up(pgc_cluster)
    # 5001 levels and over PATH_MAX as written, 1 level once folded. glob(3) of
    # the form as written crashed the server, so this check fails if the reader
    # globs the pattern as written instead of the folded form.
    over = f"'{w}/par[t]s' || repeat('/.', 5000) || '/part-0.parquet'"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        got = _answer(c, cnt(over))
    expect.text(f"{got}|{_up(pgc_cluster)}", "1000|1",
                "read_parquet reads a local glob over PATH_MAX as written but not once folded, and the server "
                "stays up")
    _wait_up(pgc_cluster)


def _errcode(cur, sql):
    """-> the SQLSTATE `sql` fails with on `cur` (an autocommit connection), or "ok"."""
    try:
        cur.execute(sql)
        cur.fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or "no answer"
    return "ok"


def test_a_glob_too_deep_or_too_long_for_glob3_is_an_error(pgc_conn, pgc_cluster, server_dir, expect):
    """#1968, #1973. glob(3) calls itself for every '/' after the first *, ? or [;
    the reader refuses more than 128 such levels, or 4096 bytes, with 54000. The
    in-memory walk of path_glob_matches is bounded, so repeat('/.*', 40) fails
    with 54000 rather than walking 2^40 paths. The crash probes run last and wait
    for the server to answer first."""
    d = server_dir / "deepglob"
    d.mkdir()

    def rp(expr):
        return f"SELECT count(*) FROM pgcolumnar.read_parquet({expr}) AS t({COLS})"

    levels = [_sqlstate(pgc_cluster, rp("'%s/' || repeat('*/', %d) || 'x.parquet'" % (d, n))) for n in (128, 129)]
    expect.text("|".join(levels), "58P01|54000", "read_parquet looks for a local glob of 128 */ levels and refuses 129 with 54000")
    expect.text(_sqlstate(pgc_cluster, rp(f"rpad('{d}/*', 4088, 'a') || '.parquet'")), "54000",
                "read_parquet refuses a local glob of 4096 bytes with 54000")
    expect.text(_sqlstate(pgc_cluster, "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 40) || '/x', "
                                       "repeat('/..', 40) || '/x')", options="-c statement_timeout=1s"),
                "54000", "path_glob_matches refuses repeat('/.*', 40) with 54000 inside a 1 s statement_timeout")
    expect.text(_one(pgc_conn.cursor(), "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 10) || '/x', "
                                        "repeat('/..', 10) || '/x')::text"),
                "true", "path_glob_matches still matches repeat('/.*', 10) against its own path")
    # The step count and the "stopped" flag are statics. Each walk must reset
    # them, or one refused walk fails every later call in that session with 54000.
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        same = [_answer(c, sql) for sql in (
            "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 40) || '/x', repeat('/..', 40) || '/x')::text",
            "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 10) || '/x', repeat('/..', 10) || '/x')::text",
            "SELECT pgcolumnar.path_glob_matches('/a/*', '/a/b')::text")]
    expect.text("|".join(same), "54000|true|true",
                "after a refused walk, path_glob_matches still answers true in the same session")

    deep = f"'{d}/' || repeat('*/', 4000) || 'x.parquet'"
    expect.text(f"{_sqlstate(pgc_cluster, rp(deep))}|{_up(pgc_cluster)}", "54000|1",
                "read_parquet refuses a local glob of 4000 */ levels with 54000, and the server stays up")
    _wait_up(pgc_cluster)
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        c.execute("CREATE SERVER IF NOT EXISTS pq_multi FOREIGN DATA WRAPPER pgcolumnar_parquet")
        c.execute(f"DO $$BEGIN EXECUTE format('CREATE FOREIGN TABLE ftdeep ({COLS}) SERVER pq_multi "
                  f"OPTIONS (path %L)', {deep}); END$$")
    try:
        expect.text(f"{_sqlstate(pgc_cluster, 'SELECT count(*) FROM ftdeep')}|{_up(pgc_cluster)}", "54000|1",
                    "a pgcolumnar_parquet foreign table over a local glob of 4000 */ levels raises 54000, "
                    "and the server stays up")
    finally:
        # the cluster is shared: tier_partition matches every foreign table's
        # path, and this one's is past the bound, so it must not outlive the test
        _wait_up(pgc_cluster)
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute("DROP FOREIGN TABLE IF EXISTS ftdeep")


def _state_within(cluster, sql, seconds, options=None):
    """-> the SQLSTATE `sql` fails with on its own connection, "ok", or
    "no answer in N s" when it has not ended after `seconds`. A backend still
    running the statement then is killed: before #1984 no cancel stopped it."""
    out = []

    def run():
        out.append(_sqlstate(cluster, sql, options=options))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        _kill_walks(cluster)
        return f"no answer in {seconds} s"
    return out[0] if out else "no result"


def _kill_walks(cluster):
    """SIGKILL every backend whose statement holds the tag pgc1984, then wait."""
    import signal
    with psycopg.connect(cluster.dsn(), autocommit=True) as c:
        pids = [r[0] for r in c.execute("SELECT pid FROM pg_stat_activity WHERE query LIKE '%pgc1984%' "
                                        "AND pid <> pg_backend_pid()").fetchall()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _wait_up(cluster)


def test_the_readers_local_glob_walk_is_bounded(pgc_cluster, server_dir, expect):
    """#1984. glob(3) lists . and .. in every real directory, and .* matches both,
    so each level of .*/ multiplies the reader's walk. The reader counts the steps
    and stops past 1,000,000 with 54000, and a cancel stops the walk. Each probe
    runs under a client timeout, and a backend still walking then is killed."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; multi-file suite needs it")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow not available; multi-file suite needs it")
    w = server_dir / "walk1984"
    (w / "emptyglob").mkdir(parents=True)
    fan = w / "fan"
    fan.mkdir()
    for i in range(1, 11):
        (fan / f"l{i}").symlink_to(".")
    parts = w / "parts"
    parts.mkdir()
    for i in range(2):
        pq.write_table(pa.table({"id": pa.array(range(i * 1000, i * 1000 + 1000), pa.int32()),
                                 "v": pa.array(range(i * 1000, i * 1000 + 1000), pa.int64())}),
                       str(parts / f"part-{i}.parquet"))
    many = w / "many"
    many.mkdir()
    for i in range(2000):
        pq.write_table(pa.table({"id": pa.array([i], pa.int32()), "v": pa.array([i], pa.int64())}),
                       str(many / f"m-{i:05d}.parquet"))
    for i in range(10000):
        (many / f"n-{i:05d}.txt").touch()
    ccileaf = w / "ccileaf"
    ccileaf.mkdir()
    pq.write_table(pa.table({"id": pa.array([7], pa.int32()), "v": pa.array([7], pa.int64())}),
                   str(ccileaf / "m.parquet"))
    for i in range(1000):
        (ccileaf / f"n-{i:04d}.txt").touch()
    ccitree = w / "ccitree"
    ccitree.mkdir()
    for i in range(200):
        (ccitree / f"d{i:03d}").symlink_to(ccileaf)
    for p in list(parts.iterdir()) + list(many.iterdir()) + list(ccileaf.iterdir()):
        p.chmod(0o644)

    def rp(expr, tag="pgc1984"):
        return f"SELECT /* {tag} */ count(*) FROM pgcolumnar.read_parquet({expr}) AS t({COLS})"

    dots18 = f"'{w}/emptyglob/' || repeat('.*/', 18) || 'x.parquet'"
    expect.text(_state_within(pgc_cluster, rp(dots18), 20, options="-c statement_timeout=10s"), "54000",
                "read_parquet refuses a local glob of 18 .*/ levels with 54000 inside a 10 s statement_timeout")
    # fan/ holds 10 links to itself, l1 to l10. */ never matches . or .., so
    # the walk stays inside fan/. 5 levels take 366,664 steps with glibc 2.43,
    # under the bound, and a 20 ms statement_timeout fires inside the walk.
    fan5 = f"'{fan}/' || repeat('*/', 5) || 'x.parquet'"
    expect.text(f"{_state_within(pgc_cluster, rp(fan5), 60)}|"
                f"{_state_within(pgc_cluster, rp(fan5), 60, options='-c statement_timeout=20ms')}",
                "58P01|57014",
                "a 20 ms statement_timeout stops read_parquet's local glob walk with 57014; without it the walk "
                "ends with 58P01")
    # 6 levels take 3,666,664 steps, so the walk runs until the bound stops it;
    # the cancel is sent 0.1 s after the statement shows as active.
    fan6 = f"'{fan}/' || repeat('*/', 6) || 'x.parquet'"
    out = []
    t = threading.Thread(target=lambda: out.append(_sqlstate(pgc_cluster, rp(fan6, "pgc1984 cancel"))), daemon=True)
    t.start()
    pid = None
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        for _ in range(500):
            row = c.execute("SELECT pid FROM pg_stat_activity WHERE query LIKE '%pgc1984 cancel%' "
                            "AND state = 'active' AND pid <> pg_backend_pid() LIMIT 1").fetchone()
            if row:
                pid = row[0]
                break
            time.sleep(0.01)
        time.sleep(0.1)
        if pid is not None:
            c.execute("SELECT pg_cancel_backend(%s)", (pid,))
    t.join(60)
    got = out[0] if out else "no answer in 60 s"
    if t.is_alive():
        _kill_walks(pgc_cluster)
    expect.text(got if pid is not None else f"{got}|no active backend seen", "57014",
                "pg_cancel_backend stops read_parquet's local glob walk with 57014")
    # The step count and the "stopped" flags are statics; each walk resets them.
    same = []

    def run_same():
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True, options="-c statement_timeout=20s") as c:
            same.extend(_answer(c, sql) for sql in (rp(dots18), rp(f"'{parts}/part-[01].parquet'", "")))

    t = threading.Thread(target=run_same, daemon=True)
    t.start()
    t.join(20)
    if t.is_alive():
        _kill_walks(pgc_cluster)
    expect.text("|".join(same), "54000|2000",
                "after a refused walk, read_parquet still reads a local glob in the same session")
    # A walk cut by an interrupt sets a static flag too, and each walk resets it.
    cut = []

    def run_cut():
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute("SET statement_timeout = '20ms'")
            cut.append(_answer(c, rp(fan5)))
            c.execute("RESET statement_timeout")
            cut.append(_answer(c, rp(f"'{parts}/part-[01].parquet'", "")))

    t = threading.Thread(target=run_cut, daemon=True)
    t.start()
    t.join(60)
    if t.is_alive():
        _kill_walks(pgc_cluster)
    expect.text("|".join(cut), "57014|2000",
                "after a walk a statement_timeout cut, read_parquet still reads a local glob in the same session")
    # glob(3) closes each directory it opened, also after the walk stopped.
    fds = []
    codes = {}

    def run_fds():
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            fds.append(_answer(c, "SELECT count(*) FROM pg_ls_dir('/proc/self/fd')"))
            for _ in range(10):
                st = _answer(c, rp(dots18))
                codes[st] = codes.get(st, 0) + 1
            c.execute("SET statement_timeout = '20ms'")
            for _ in range(10):
                st = _answer(c, rp(fan5))
                codes[st] = codes.get(st, 0) + 1
            c.execute("RESET statement_timeout")
            fds.append(_answer(c, "SELECT count(*) FROM pg_ls_dir('/proc/self/fd')"))

    t = threading.Thread(target=run_fds, daemon=True)
    t.start()
    t.join(120)
    if t.is_alive():
        _kill_walks(pgc_cluster)
    got = "|".join(fds) + "|" + " ".join(f"{n}x{k}" for k, n in sorted(codes.items()))
    before = fds[0] if fds else ""
    expect.text(got, f"{before}|{before}|10x54000 10x57014",
                "10 refused and 10 cut local glob walks leave the backend's open file count unchanged")
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        got = _answer(c, f"SELECT count(*) || '|' || sum(id) FROM pgcolumnar.read_parquet('{many}/m-*.parquet') "
                         f"AS t({COLS})")
    expect.text(got, "2000|1999000", "read_parquet reads all 2000 files a local glob matches among 12000 names")
    # client_connection_check_interval raises an interrupt each time it fires,
    # and that interrupt raises no error. The walk must not stop for it.
    cci = []

    def run_cci():
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True,
                             options="-c client_connection_check_interval=10ms") as c:
            cci.append(_answer(c, f"SELECT /* pgc1984 */ count(*) || '|' || sum(id) FROM pgcolumnar.read_parquet("
                                  f"'{ccitree}/*/*.parquet') AS t({COLS})"))

    t = threading.Thread(target=run_cci, daemon=True)
    t.start()
    t.join(60)
    if t.is_alive():
        _kill_walks(pgc_cluster)
    expect.text(cci[0] if cci else "no answer in 60 s", "200|1400",
                "read_parquet reads all 200 matches of a local glob under client_connection_check_interval = 10ms")

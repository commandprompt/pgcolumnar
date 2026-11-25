"""The export byte sink checks every write and never shows a partial file under the
final name (#394 step 1). The pytest twin of `test/export_sink.sh`, every check by the
shell's name (#1341).

No fwrite call remains in either export file, so the count is a property of the tree.
Parquet and arrow exports of a 40,000-row table import back hash-equal to the source,
leaving no `*.tmp.*` debris. With `pgcolumnar.sink_fail_after` injecting the error a
full disk raises, each export fails 53100, nothing exists at its final path, and no
temp file remains; the arrow arm fails inside write_record_batch by arithmetic on the
measured full size. A fresh session exports again.

An export that ends with FATAL leaves no temp file either (#2075). A FATAL exit
(pg_terminate_backend, SIGTERM at shutdown) skips the writers' PG_CATCH, so only an
exit callback can remove the file. exit_on_error promotes the injected ERROR to FATAL,
which takes that same exit path at a known byte, with no timing race. The directory is
read only after the backend has left pg_stat_activity, because the client sees the
FATAL message before the backend runs its exit callbacks. Each target holds a good file
first, which the FATAL re-export must not delete. Two arms end a transaction with FATAL
after a finished export and after a failed one rolled back to a savepoint, so the exit
callback walks a list that once held other sinks. Three more fail an export at the
rename (a non-empty directory at the final name) and at the open (a missing parent
directory): a FATAL at the rename leaves no temp file, and a failed rename or open rolled
back to a savepoint leaves no freed sink on the list. The server log must show no backend
killed by a signal, and no AddressSanitizer report, during these arms.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). The files live in `server_dir`, because the backend writes
them. Each round-trip reading carries its row count, so two empty tables cannot
hash-match, and a directory that cannot be listed is a failed reading, never "no
debris".
"""
import hashlib
import os
import pathlib
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_export_sink"
ROWS = 40000
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _counted_hash(dsn, table):
    """-> "count:md5" of a table, the shell's pgc_set_hash with its row count."""
    n = _value(dsn, f"SELECT count(*) FROM {table}")
    h = _value(dsn, HASH.format(f"SELECT * FROM {table}"))
    return f"{n}:{h}" if isinstance(n, int) and isinstance(h, str) and not _failed(h) else query_error(
        f"no counted hash of {table}: {n} / {h}")


def _sqlstate(dsn, fail_after, sql):
    """-> the SQLSTATE the export raises with the injection set in its session."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SET pgcolumnar.sink_fail_after = {fail_after}")
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


def _debris(d):
    """-> the count of *.tmp.* names under d. A directory that is missing or cannot be
    listed is a failed reading: rglob yields nothing for one, so walk and keep its errors."""
    errors = []
    found = sum(1 for _, dirs, files in os.walk(d, onerror=errors.append)
                for n in dirs + files if ".tmp." in n)
    return query_error(f"listing {d} failed: {errors[0]}") if errors else found


def _fatal_session(dsn, statements, want=("53100", "export write failed (injected fault)")):
    """Runs statements in one session; the last one must end it with FATAL.
    -> (seen, results). seen is "fatal gone" when the session ended with a FATAL of
    want's SQLSTATE whose message holds want's text (by default the injected fault's,
    53100), and its backend has left pg_stat_activity; otherwise what was seen instead.
    results holds, for each earlier statement, its first value, its SQLSTATE if it
    raised, or None (no row, or no result)."""
    try:
        conn = psycopg.connect(dsn, autocommit=True)
    except psycopg.Error as exc:
        return query_error(_message(exc)), []
    seen, pid, results = "no fatal (the export returned)", None, []
    try:
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        for sql in statements[:-1]:
            try:
                cur = conn.execute(sql)
                row = cur.fetchone() if cur.description else None
                results.append(row[0] if row else None)
            except psycopg.Error as exc:
                results.append(exc.sqlstate or _message(exc))
        conn.execute(statements[-1])
    except psycopg.Error as exc:
        seen = "fatal" if exc.sqlstate == want[0] and conn.closed \
            and want[1] in str(exc) \
            else f"no fatal ({exc.sqlstate} {_message(exc)})"
    finally:
        conn.close()
    if pid is None:
        return query_error(f"no backend pid: {seen}"), results
    n = None
    for _ in range(100):
        n = _value(dsn, f"SELECT count(*) FROM pg_stat_activity WHERE pid = {pid}")
        if n == 0:
            break
        time.sleep(0.1)
    return f"{seen} {'gone' if n == 0 else f'still listed ({n})'}", results


FATAL_PRE = ["SET exit_on_error = on", "SET pgcolumnar.sink_fail_after = 8192"]


def _log_size(log):
    try:
        return log.stat().st_size
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")


def _signals_since(log, offset):
    """-> how many backends the server log says a signal killed after byte offset. On an
    ASan build a fault is an AddressSanitizer report and an exit code of 1, so those
    reports count too."""
    if not isinstance(offset, int):
        return query_error(f"no log offset: {offset}")
    try:
        text = log.read_bytes()[offset:].decode(errors="replace")
        return text.count("terminated by signal") + text.count("ERROR: AddressSanitizer")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")


def _tmp_of(d, name):
    """-> the count of name.tmp.* files directly in d, or the listing failure."""
    try:
        return sum(1 for p in pathlib.Path(d).iterdir() if p.name.startswith(name + ".tmp."))
    except OSError as exc:
        return query_error(f"listing {d} failed: {exc}")


def _md5(path):
    try:
        return hashlib.md5(pathlib.Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _fwrites(name):
    try:
        return sum(1 for line in (SRC / name).read_text().splitlines() if "fwrite(" in line)
    except OSError as exc:
        return query_error(f"{name} unreadable: {exc}")


@pytest.fixture(scope="module")
def es(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        d = server_dir
        pqf, arf = d / "es.parquet", d / "es.arrow"
        r = {"census parquet": _fwrites("columnar_parquet.c"), "census arrow": _fwrites("columnar_arrow.c")}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # one statement per call, as the shell's psql_run: a failed load keeps its table
        _run(dsn, "CREATE TABLE es_src (id int, v int8, t text) USING pgcolumnar")
        _run(dsn, f"INSERT INTO es_src SELECT g, g*17, 'row-'||g FROM generate_series(1,{ROWS}) g")
        r["pq export"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('es_src', '{pqf}')")
        r["ar export"] = _value(dsn, f"SELECT pgcolumnar.export_arrow('es_src', '{arf}')")
        r["debris ok"] = _debris(d)

        _run(dsn, "CREATE TABLE es_back_pq (id int, v int8, t text) USING pgcolumnar")
        _run(dsn, f"SELECT pgcolumnar.import_parquet('es_back_pq', '{pqf}')")
        r["pq back"] = _counted_hash(dsn, "es_back_pq")
        _run(dsn, "CREATE TABLE es_back_ar (id int, v int8, t text) USING pgcolumnar")
        _run(dsn, f"SELECT pgcolumnar.import_arrow('es_back_ar', '{arf}')")
        r["ar back"] = _counted_hash(dsn, "es_back_ar")
        src = _value(dsn, HASH.format("SELECT * FROM es_src"))
        r["src"] = query_error(f"no hash of es_src: {src}") if _failed(src) else f"{ROWS}:{src}"

        try:
            size = os.stat(arf).st_size
        except OSError:
            size = 0
        r["arrow size"] = size
        before = _md5(pqf)
        before = before if before else "unread before the failed export"

        failp = d / "es_fail.parquet"
        r["pq inject"] = _sqlstate(dsn, 8192, f"SELECT pgcolumnar.export_parquet('es_src', '{failp}')")
        r["pq final"] = "dirty" if failp.exists() else "clean"
        state = _sqlstate(dsn, 8192, f"SELECT pgcolumnar.export_parquet('es_src', '{pqf}')")
        r["pq intact"] = f"{state}:{_md5(pqf)}"
        r["pq before"] = f"53100:{before}"
        r["pq debris"] = _debris(d)

        faila = d / "es_fail.arrow"
        r["ar inject"] = _sqlstate(dsn, size - 100, f"SELECT pgcolumnar.export_arrow('es_src', '{faila}')")
        r["ar final"] = "dirty" if faila.exists() else "clean"
        r["ar debris"] = _debris(d)

        r["again"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es2.parquet'}')")

        # The client sees the FATAL before the backend runs its exit callbacks, so a
        # callback that crashes after its unlink passes every other reading here.
        log = pgc_cluster.datadir / "server.log"
        log_at = _log_size(log)
        for kind in ("parquet", "arrow"):
            name = f"es_fatal.{kind}"
            # a good file first: a callback that unlinked the final name would delete it
            pre = _value(dsn, f"SELECT pgcolumnar.export_{kind}('es_src', '{d / name}')")
            before = _md5(d / name) or "unread before the FATAL re-export"
            r[f"{kind} fatal"], _ = _fatal_session(
                dsn, FATAL_PRE + [f"SELECT pgcolumnar.export_{kind}('es_src', '{d / name}')"])
            r[f"{kind} fatal intact"] = f"{pre}:{_md5(d / name)}"
            r[f"{kind} fatal before"] = f"{ROWS}:{before}"
            r[f"{kind} fatal tmp"] = _tmp_of(d, name)
        # a finished export stays listed only if Finish forgets to unlist it
        seen, res = _fatal_session(dsn, [
            "BEGIN",
            f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_tx1.parquet'}')",
            f"SELECT pgcolumnar.export_arrow('es_src', '{d / 'es_tx2.arrow'}')",
            *FATAL_PRE,
            f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_tx3.parquet'}')"])
        r["tx fatal"] = f"{res.count(ROWS)}:{seen}"
        r["tx debris"] = _tmp_of(d, "es_tx3.parquet")
        # a failed export, rolled back to a savepoint, only if Abort forgets
        seen, res = _fatal_session(dsn, [
            "BEGIN", "SAVEPOINT s", "SET pgcolumnar.sink_fail_after = 8192",
            f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_sp1.parquet'}')",
            "ROLLBACK TO SAVEPOINT s",
            *FATAL_PRE,
            f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_sp2.parquet'}')"])
        r["sp fatal"] = f"{res.count('53100')}:{seen}"
        r["sp debris"] = _tmp_of(d, "es_sp2.parquet")
        # The arms above fail only in Write, with the file open. A non-empty directory
        # at the final name fails durable_rename (42809), after Finish closed the file;
        # a missing parent directory fails the temp file's open (58P01).
        (d / "es_isdir" / "x").mkdir(parents=True, exist_ok=True)
        # a rename that raised must stay listed, or a FATAL there leaves its temp file
        r["rename fatal"], _ = _fatal_session(
            dsn, ["SET exit_on_error = on",
                  f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_isdir'}')"],
            want=("42809", str(d / "es_isdir")))
        r["rename fatal tmp"] = _tmp_of(d, "es_isdir")
        # Abort must unlist a sink whose file Finish already closed; the repeat()
        # query allocates after ROLLBACK TO frees it, as the review's repro did
        seen, res = _fatal_session(dsn, [
            "BEGIN", "SAVEPOINT s",
            f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_isdir'}')",
            "ROLLBACK TO SAVEPOINT s",
            "SELECT repeat('x', 4000) FROM generate_series(1, 50) LIMIT 0",
            *FATAL_PRE,
            f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_sp3.parquet'}')"])
        r["sp rename fatal"] = f"{res.count('42809')}:{seen}"
        r["sp rename tmp"] = _tmp_of(d, "es_sp3.parquet")
        # A failed open must leave nothing listed: no Abort would unlist it. The walk
        # of a freed sink faults on an assert build (signal) and an ASan build (an
        # AddressSanitizer report); a release build reads it without faulting.
        nodir = d / "es_nodir"
        seen, res = _fatal_session(dsn, [
            "BEGIN", "SAVEPOINT s",
            f"SELECT pgcolumnar.export_parquet('es_src', '{nodir / 'a.parquet'}')",
            "ROLLBACK TO SAVEPOINT s",
            *FATAL_PRE,
            f"SELECT pgcolumnar.export_parquet('es_src', '{d / 'es_sp4.parquet'}')"])
        r["sp open fatal"] = f"{res.count('58P01')}:{seen}"
        r["sp open tmp"] = _tmp_of(d, "es_sp4.parquet")
        # a crash restarts the cluster: wait for it to answer before reading the log
        for _ in range(100):
            if _value(dsn, "SELECT 1") == 1:
                break
            time.sleep(0.1)
        r["signals"] = _signals_since(log, log_at)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_census_zero_fwrite_remain_in_columnar_parquet_c(es, expect):
    expect.text(str(es["census parquet"]), "0", "census: zero fwrite remain in columnar_parquet.c")


def test_census_zero_fwrite_remain_in_columnar_arrow_c(es, expect):
    expect.text(str(es["census arrow"]), "0", "census: zero fwrite remain in columnar_arrow.c")


def test_parquet_export_succeeds_through_the_sink(es, expect):
    expect.text(str(es["pq export"]), str(ROWS), "parquet export succeeds through the sink")


def test_arrow_export_succeeds_through_the_sink(es, expect):
    expect.text(str(es["ar export"]), str(ROWS), "arrow export succeeds through the sink")


def test_no_temp_debris_after_successful_exports(es, expect):
    expect.text(str(es["debris ok"]), "0", "no temp debris after successful exports")


def test_parquet_round_trip_hash_matches_the_source(es, expect):
    expect.text(es["pq back"], es["src"], "parquet round-trip hash-matches the source")


def test_arrow_round_trip_hash_matches_the_source(es, expect):
    expect.text(es["ar back"], es["src"], "arrow round-trip hash-matches the source")


def test_the_arrow_size_supports_a_mid_batch_injection_point(es, expect):
    expect.text("yes" if es["arrow size"] > 4200 else f"no ({es['arrow size']} bytes)", "yes",
                "measured arrow size supports a mid-batch injection point")


def test_inject_parquet_export_fails_as_disk_full(es, expect):
    expect.text(str(es["pq inject"]), "53100", "inject: parquet export fails as disk-full (53100)")


def test_inject_nothing_exists_at_the_parquet_final_path(es, expect):
    expect.text(es["pq final"], "clean", "inject: nothing exists at the parquet FINAL path")


def test_inject_a_failed_re_export_leaves_the_previous_parquet_file_intact(es, expect):
    expect.text(es["pq intact"], es["pq before"],
                "inject: a failed re-export leaves the previous parquet file intact")


def test_inject_no_parquet_temp_debris(es, expect):
    expect.text(str(es["pq debris"]), "0", "inject: no parquet temp debris")


def test_inject_arrow_export_fails_inside_write_record_batch(es, expect):
    expect.text(str(es["ar inject"]), "53100", "inject: arrow export fails inside write_record_batch (53100)")


def test_inject_nothing_exists_at_the_arrow_final_path(es, expect):
    expect.text(es["ar final"], "clean", "inject: nothing exists at the arrow FINAL path")


def test_inject_no_arrow_temp_debris(es, expect):
    expect.text(str(es["ar debris"]), "0", "inject: no arrow temp debris")


def test_after_reset_exports_work_again(es, expect):
    expect.text(str(es["again"]), str(ROWS), "after RESET, exports work again")


@pytest.mark.parametrize("kind", ["parquet", "arrow"])
def test_fatal_the_export_ends_its_backend_with_fatal(es, expect, kind):
    expect.text(es[f"{kind} fatal"], "fatal gone",
                f"fatal: the {kind} export ends its backend with FATAL")


@pytest.mark.parametrize("kind", ["parquet", "arrow"])
def test_fatal_a_fatal_re_export_leaves_the_previous_file_intact(es, expect, kind):
    expect.text(es[f"{kind} fatal intact"], es[f"{kind} fatal before"],
                f"fatal: a FATAL re-export leaves the previous {kind} file intact")


@pytest.mark.parametrize("kind", ["parquet", "arrow"])
def test_fatal_no_temp_file_after_a_fatal_exit(es, expect, kind):
    expect.text(str(es[f"{kind} fatal tmp"]), "0", f"fatal: no {kind} temp file after a FATAL exit")


def test_fatal_after_finished_exports_in_one_transaction_ends_with_fatal(es, expect):
    expect.text(es["tx fatal"], "2:fatal gone",
                "fatal: a FATAL export after two finished exports in one transaction ends with FATAL")


def test_fatal_no_temp_file_after_finished_exports_in_one_transaction(es, expect):
    expect.text(str(es["tx debris"]), "0",
                "fatal: no temp file after a FATAL export that followed finished exports")


def test_fatal_after_a_failed_export_rolled_back_to_a_savepoint_ends_with_fatal(es, expect):
    expect.text(es["sp fatal"], "1:fatal gone",
                "fatal: a FATAL export after a failed export rolled back to a savepoint ends with FATAL")


def test_fatal_no_temp_file_after_a_rolled_back_export(es, expect):
    expect.text(str(es["sp debris"]), "0",
                "fatal: no temp file after a FATAL export that followed a rolled-back export")


def test_fatal_no_backend_died_by_a_signal_during_the_fatal_arms(es, expect):
    expect.text(str(es["signals"]), "0", "fatal: no backend died by a signal during the FATAL arms")


def test_fatal_an_export_whose_rename_fails_ends_its_backend_with_fatal(es, expect):
    expect.text(es["rename fatal"], "fatal gone",
                "fatal: an export whose rename fails ends its backend with FATAL")


def test_fatal_no_temp_file_after_a_fatal_at_the_rename(es, expect):
    expect.text(str(es["rename fatal tmp"]), "0", "fatal: no temp file after a FATAL at the rename")


def test_fatal_after_a_failed_rename_rolled_back_to_a_savepoint_ends_with_fatal(es, expect):
    expect.text(es["sp rename fatal"], "1:fatal gone",
                "fatal: a FATAL export after a failed rename rolled back to a savepoint ends with FATAL")


def test_fatal_no_temp_file_after_a_failed_rename(es, expect):
    expect.text(str(es["sp rename tmp"]), "0",
                "fatal: no temp file after a FATAL export that followed a failed rename")


def test_fatal_after_a_failed_open_rolled_back_to_a_savepoint_ends_with_fatal(es, expect):
    expect.text(es["sp open fatal"], "1:fatal gone",
                "fatal: a FATAL export after a failed open rolled back to a savepoint ends with FATAL")


def test_fatal_no_temp_file_after_a_failed_open(es, expect):
    expect.text(str(es["sp open tmp"]), "0",
                "fatal: no temp file after a FATAL export that followed a failed open")

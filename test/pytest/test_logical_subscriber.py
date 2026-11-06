"""Columnar as a logical replication APPLY TARGET (#435, #1341). The pytest twin of
`test/logical_subscriber.sh`, every check by the shell's name.

A heap table on a publisher, a columnar mirror on a subscriber. Two facts are pinned:
an INSERT-only publication works end to end (initial sync and streamed INSERTs take no
tuple lock), and a publication that carries UPDATE raises OUR error, which names logical
replication and the way out, retried by the apply worker forever. An ordinary
SELECT ... FOR UPDATE / SHARE / NO KEY UPDATE on a columnar table keeps the generic
row-locking message: the replication one is gated on the apply worker.

HOW THE READINGS ARE MADE FAIL-CLOSED, as in the shell:
- an INSERT committed after the insert-only UPDATE must arrive before "skipped by design"
  and "no apply errors" are read, so a subscription that applied nothing cannot pass them;
- "does NOT say only 'row locking'" is read only once the replication error is in the
  subscriber's log, and "is not told about replication" only once the statement raised
  the generic error, so an absence that is no evidence cannot pass;
- the wedge reads "publisher -> subscriber", so an UPDATE never sent cannot pass as one
  never applied.

PRIVATE CLUSTERS, two of them: the publisher at wal_level=logical, and a subscriber whose
server log holds the apply worker's error, which never reaches a client.
"""
import pathlib
import shutil
import tempfile
import time

import psycopg
import pytest

from pgc_vacuity import query_error

ROUND_TRIP = "the logical subscriber round trip"
REPL_ERR = "logical replication cannot apply UPDATE or DELETE"
GENERIC = "row locking is not supported"


def _q(cluster, sql):
    """-> the first column of the last statement's first row, as text; or query_error()."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            cur = None
            for stmt in [s for s in sql.split(";") if s.strip()]:
                cur = c.execute(stmt)
            row = cur.fetchone() if cur is not None and cur.description else None
            return "" if row is None or row[0] is None else str(row[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(cluster, *sqls):
    """Setup statements, as the shell runs them with their output dropped. A failure is
    left for the readings after it to report by name."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            for sql in sqls:
                c.execute(sql)
    except psycopg.Error:
        pass


def _as_psql(exc):
    """-> a raised error's lines as psql prints them, so the shell's grep -c counts match."""
    d = exc.diag
    lines = [f"ERROR:  {d.message_primary or str(exc)}"]
    if d.message_detail:
        lines.append(f"DETAIL:  {d.message_detail}")
    if d.message_hint:
        lines.append(f"HINT:  {d.message_hint}")
    return "\n".join(lines)


def _said(cluster, sql):
    """-> the error a statement raised, as psql prints it, or "" when it raised nothing.
    Compared only with fixed counts, never with another reading."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            c.execute(sql).fetchall()
            return ""
    except psycopg.Error as exc:
        raised = exc
    return _as_psql(raised)


def _lines(text, needle):
    return sum(1 for line in text.splitlines() if needle in line)


def _log_has(log, needle):
    """-> how many lines of the subscriber's log hold `needle`, or query_error()."""
    try:
        return _lines(log.read_text(errors="replace"), needle)
    except OSError as exc:
        return query_error(f"subscriber log unreadable: {exc}")


def _wait(cluster, sql, want, tries=30):
    for _ in range(tries):
        if _q(cluster, sql) == want:
            break
        time.sleep(1)
    return _q(cluster, sql)


def _conf(datadir, *lines):
    with open(datadir / "postgresql.conf", "a") as fh:
        fh.write("\n" + "\n".join(lines) + "\n")


def _yes_if(n, at_least):
    return "yes" if isinstance(n, int) and n >= at_least else f"no ({n})"


def _lock_readings(pub, mode):
    out = _said(pub, f"SELECT * FROM lk WHERE id = 1 {mode}")
    generic = _lines(out, GENERIC)
    told = (_lines(out, "logical replication cannot apply") if generic
            else f"no reading (no generic error: {out.splitlines()[0] if out else 'the statement raised nothing'})")
    return generic, told


@pytest.fixture(scope="module")
def ls(pgc_cluster):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-lsub-"))
    work.chmod(0o777)
    pub = sub = None
    r = {}
    try:
        try:
            pub_port, sub_port = pick_port(26), pick_port(27)
        except RuntimeError as exc:
            yield {"no port": f"no free port for the subscriber cluster ({exc})"}
            return
        (work / "pub").mkdir()
        (work / "sub").mkdir()
        pub = Cluster(pgc_cluster.pg_config, "lsub-pub", work / "pub", pub_port)
        pub.initdb()
        _conf(work / "pub", "wal_level=logical", "max_wal_senders=10", "max_replication_slots=10",
              "max_locks_per_transaction=256")
        pub.start()
        sub = Cluster(pgc_cluster.pg_config, "lsub-sub", work / "sub", sub_port)
        sub.initdb()
        _conf(work / "sub", "wal_level=logical", "max_locks_per_transaction=256")
        try:
            sub.start()
        except RuntimeError:
            pass        # the premise below reports it by name
        log = work / "sub" / "server.log"
        conninfo = f"host=127.0.0.1 port={pub.port} user=postgres dbname=postgres"

        r["wal_level"] = _q(pub, "SHOW wal_level")
        r["sub answers"] = _q(sub, "SELECT 1")
        r["sub loaded"] = _q(sub, "CREATE EXTENSION IF NOT EXISTS pgcolumnar; "
                                  "SELECT count(*) FROM pg_extension WHERE extname='pgcolumnar'")
        _run(pub, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")

        # ---- 1. the working pattern: publish inserts only ----
        _run(pub, "CREATE TABLE src (id int PRIMARY KEY, v text)",
             "INSERT INTO src SELECT g, 'v'||g FROM generate_series(1,10) g",
             "CREATE PUBLICATION p_ins FOR TABLE src WITH (publish = 'insert')")
        _run(sub, "CREATE TABLE src (id int PRIMARY KEY, v text) USING pgcolumnar")
        _run(sub, f"CREATE SUBSCRIPTION s_ins CONNECTION '{conninfo}' PUBLICATION p_ins")
        r["initial sync"] = _wait(sub, "SELECT count(*) FROM src", "10")
        _run(pub, "INSERT INTO src SELECT g, 'v'||g FROM generate_series(11,15) g")
        r["streamed"] = _wait(sub, "SELECT count(*) FROM src", "15")
        # the INSERT after the UPDATE proves the UPDATE before it was processed
        _run(pub, "UPDATE src SET v = 'changed' WHERE id = 1")
        _run(pub, "INSERT INTO src VALUES (16, 'after the update')")
        sentinel = _wait(sub, "SELECT count(*) FROM src WHERE id = 16", "1")

        def after_sentinel(reading):
            return reading if sentinel == "1" else \
                f"no reading (the INSERT after the UPDATE never arrived: [{sentinel}]; reading [{reading}])"
        r["skipped"] = after_sentinel(_q(sub, "SELECT v FROM src WHERE id = 1"))
        r["apply errors"] = after_sentinel(
            _q(sub, "SELECT coalesce(sum(apply_error_count),0)::text FROM pg_stat_subscription_stats"))

        # ---- 2. the wedge: a publication carrying UPDATE ----
        _run(pub, "CREATE TABLE upd (id int PRIMARY KEY, v text)",
             "INSERT INTO upd SELECT g, 'v'||g FROM generate_series(1,5) g",
             "CREATE PUBLICATION p_all FOR TABLE upd")
        _run(sub, "CREATE TABLE upd (id int PRIMARY KEY, v text) USING pgcolumnar")
        _run(sub, f"CREATE SUBSCRIPTION s_all CONNECTION '{conninfo}' PUBLICATION p_all")
        r["second synced"] = _wait(sub, "SELECT count(*) FROM upd", "5")
        _run(pub, "UPDATE upd SET v = 'changed' WHERE id = 1")
        for _ in range(25):
            if isinstance(_log_has(log, REPL_ERR), int) and _log_has(log, REPL_ERR) >= 1:
                break
            time.sleep(1)
        repl = _log_has(log, REPL_ERR)
        r["names replication"] = _yes_if(repl, 1)
        if not (isinstance(repl, int) and repl >= 1):
            r["not only generic"] = "no reading (the subscriber log holds no replication error)"
        else:
            g = _log_has(log, GENERIC)
            r["not only generic"] = "yes" if g == 0 else f"no ({g})"
        r["hint"] = _yes_if(_log_has(log, "publish = "), 1)
        r["wedge"] = f"{_q(pub, 'SELECT v FROM upd WHERE id = 1')} -> {_q(sub, 'SELECT v FROM upd WHERE id = 1')}"
        for _ in range(20):
            n = _log_has(log, REPL_ERR)
            if isinstance(n, int) and n >= 2:
                break
            time.sleep(2)
        r["retries"] = _yes_if(_log_has(log, REPL_ERR), 2)

        # ---- 3. the gate must not widen ----
        _run(pub, "CREATE TABLE lk (id int primary key, v text) USING pgcolumnar",
             "INSERT INTO lk VALUES (1, 'a')")
        r["lk am"] = _q(pub, "SELECT amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam WHERE c.relname='lk'")
        for mode in ("FOR UPDATE", "FOR SHARE", "FOR NO KEY UPDATE"):
            r[mode] = _lock_readings(pub, mode)

        _run(sub, "DROP SUBSCRIPTION s_ins")
        _run(sub, "DROP SUBSCRIPTION s_all")
        yield r
    finally:
        for c in (sub, pub):
            if c is not None:
                c.stop()
        shutil.rmtree(work, ignore_errors=True)


def _runnable(ls, expect):
    if "no port" in ls:
        expect.cannot_run("UNAVAILABLE_ENDPOINT", ls["no port"], name="the logical subscriber round trip")
        return False
    return True


def test_the_publisher_has_logical_decoding_on(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["wal_level"], "logical", "premise: the publisher has logical decoding on")


def test_the_subscriber_cluster_answers(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["sub answers"], "1", "premise: the subscriber cluster answers")


def test_the_subscriber_loaded_pgcolumnar(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["sub loaded"], "1", "premise: the subscriber loaded pgcolumnar")


def test_insert_only_initial_sync_copied_every_row(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["initial sync"], "10", "insert-only: initial sync copied every row")


def test_insert_only_streamed_inserts_applied(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["streamed"], "15", "insert-only: streamed INSERTs applied")


def test_insert_only_the_update_is_skipped_by_design(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["skipped"], "v1", "insert-only: the UPDATE is skipped by design, not applied")


def test_insert_only_no_apply_errors(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["apply errors"], "0", "insert-only: no apply errors")


def test_the_second_subscription_synced_before_the_update(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["second synced"], "5", "the second subscription synced before the UPDATE")


def test_the_apply_error_names_logical_replication(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["names replication"], "yes", "the error names logical replication, not row locking")


def test_the_apply_error_does_not_say_only_row_locking(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["not only generic"], "yes", "the error does NOT say only 'row locking is not supported'")


def test_the_apply_hint_names_the_insert_only_publication(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["hint"], "yes", "the hint names the insert-only publication")


def test_the_wedged_row_is_unchanged_on_the_subscriber(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["wedge"], "changed -> v1", "the wedge is real: the row is still unchanged on the subscriber")


def test_the_apply_worker_retries_so_the_subscription_is_wedged(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["retries"], "yes", "it retries rather than giving up, so the subscription is wedged")


def test_the_lock_fixture_is_columnar(ls, expect):
    if _runnable(ls, expect):
        expect.text(ls["lk am"], "pgcolumnar", "premise: the lock fixture really is columnar")


def test_select_for_update_keeps_the_generic_message(ls, expect):
    if _runnable(ls, expect):
        expect.text(str(ls["FOR UPDATE"][0]), "1", "an ordinary SELECT ... FOR UPDATE keeps the generic message")


def test_select_for_update_is_not_told_about_replication(ls, expect):
    if _runnable(ls, expect):
        expect.text(str(ls["FOR UPDATE"][1]), "0",
                    "an ordinary SELECT ... FOR UPDATE is not told about replication")


def test_select_for_share_keeps_the_generic_message(ls, expect):
    if _runnable(ls, expect):
        expect.text(str(ls["FOR SHARE"][0]), "1", "an ordinary SELECT ... FOR SHARE keeps the generic message")


def test_select_for_share_is_not_told_about_replication(ls, expect):
    if _runnable(ls, expect):
        expect.text(str(ls["FOR SHARE"][1]), "0",
                    "an ordinary SELECT ... FOR SHARE is not told about replication")


def test_select_for_no_key_update_keeps_the_generic_message(ls, expect):
    if _runnable(ls, expect):
        expect.text(str(ls["FOR NO KEY UPDATE"][0]), "1",
                    "an ordinary SELECT ... FOR NO KEY UPDATE keeps the generic message")


def test_select_for_no_key_update_is_not_told_about_replication(ls, expect):
    if _runnable(ls, expect):
        expect.text(str(ls["FOR NO KEY UPDATE"][1]), "0",
                    "an ordinary SELECT ... FOR NO KEY UPDATE is not told about replication")

"""The skip loop resolves pgcolumnar.zone_map ONCE per scan, not once per chunk group per
predicate column (#744). The pytest twin of `test/native_zonemap_session.sh`, every check
by the shell's name (#1341).

What is asserted is the work done, not the answer: a scan returns the same rows either
way. The witness is the DEBUG1 line each scan's read session emits when it closes,
"pgcolumnar zone map read: probes=N opens=M". Probes must scale with the group count and
opens must stay at one.

Each scan owns its session, so two scans open at once, the two sides of a join, report
separately and each reports its OWN groups. A session shared between them, the static the
first implementation held, folds one scan's probes into the other's and leaves the second
report empty, while the shared handle still reads correctly; only the witness sees it. The
same holds around a scan that errors inside a subtransaction: the scans before, after the
ROLLBACK TO and after the COMMIT each open for themselves and probe only their own groups.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, and a failed query is a `query_error()`.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_zonemap_session"
N = 40
ROWS = N * 10000
WITNESS = re.compile(r"probes=(\d+) opens=(\d+)")
SCAN_SETTINGS = ("SET client_min_messages = debug1", "SET max_parallel_workers_per_gather = 0",
                 "SET enable_indexscan = off", "SET enable_bitmapscan = off")


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, sql):
    """Run one setup statement and swallow its failure, as the shell's psql_run does: a
    failed step shows up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _one(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _witness(dsn, where):
    """-> (probes, opens) from the LAST 'probes=N opens=M' line the scan raised, as the
    shell's witness() reads it; or a failure."""
    messages = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: messages.append(d.message_primary or ""))
            for s in SCAN_SETTINGS:
                conn.execute(s)
            conn.execute(f"SELECT count(*) FROM zs WHERE {where}").fetchone()
    except psycopg.Error as exc:
        failed = _failed(exc)
        return failed, failed
    found = [m.groups() for line in messages for m in [WITNESS.search(line)] if m]
    if not found:
        failed = query_error(f"no 'probes= opens=' line from the scan on {where}")
        return failed, failed
    return found[-1]


def _nested(dsn):
    """-> (the join's answer, the scan reports it raised); either may be a failure."""
    messages = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: messages.append(d.message_primary or ""))
            conn.execute("SET client_min_messages = debug1")
            conn.execute("SET enable_indexscan = off")
            conn.execute("SET max_parallel_workers_per_gather = 0")
            row = conn.execute("SELECT count(*) FROM zs2 a JOIN zs2 b ON a.k = b.k "
                               "WHERE a.k <= 1000 AND b.k <= 1000").fetchone()
    except psycopg.Error as exc:
        failed = _failed(exc)
        return failed, failed
    return str(row[0]), messages


def _around_abort(dsn):
    """Run the shell's zsub.sql statement by statement, continuing past the error as psql
    does with ON_ERROR_STOP=0. -> (the counts the three scans answered, the error messages,
    the scan reports raised); or a failure if the session itself could not run."""
    messages, answers, errors = [], [], []
    statements = [
        "SET client_min_messages = debug1",
        "SET enable_indexscan = off",
        "SELECT count(*) FROM zs2 WHERE k <= 1000",
        "BEGIN",
        "SAVEPOINT s1",
        "SELECT count(*) FROM zs2 WHERE k <= 5000 AND 1/(v-4000) > 0",
        "ROLLBACK TO s1",
        "SELECT count(*) FROM zs2 WHERE k <= 1000",
        "COMMIT",
        "SELECT count(*) FROM zs2 WHERE k <= 1000",
    ]
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: messages.append(d.message_primary or ""))
            for s in statements:
                try:
                    cur = conn.execute(s)
                    if cur.description:
                        answers.append(str(cur.fetchone()[0]))
                except psycopg.errors.QueryCanceled:
                    raise
                except psycopg.OperationalError:
                    raise
                except psycopg.Error as exc:
                    errors.append(str(exc))
    except psycopg.Error as exc:
        return _failed(exc)
    return answers, errors, messages


@pytest.fixture(scope="module")
def zs(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _run(dsn, "CREATE TABLE zs (k bigint, w bigint, v bigint) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('zs', stripe_row_limit => 10000)")
        _run(dsn, f"INSERT INTO zs SELECT g, g, g FROM generate_series(1, {ROWS}) g")
        r["groups"] = _one(dsn, "SELECT count(*) FROM pgcolumnar.row_group "
                                "WHERE storage_id = pgcolumnar.get_storage_id('zs')")
        r["probes"], r["opens"] = _witness(dsn, "k <= 10000")
        r["probes2"], r["opens2"] = _witness(dsn, "k <= 10000 AND w <= 10000")
        _run(dsn, "CREATE TABLE zs2 (k bigint, v bigint) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('zs2', stripe_row_limit => 1000)")
        _run(dsn, "INSERT INTO zs2 SELECT g, g FROM generate_series(1, 20000) g")
        r["groups2"] = _one(dsn, "SELECT count(*) FROM pgcolumnar.row_group "
                                 "WHERE storage_id = pgcolumnar.get_storage_id('zs2')")
        r["nested"], r["nested reports"] = _nested(dsn)
        r["abort"] = _around_abort(dsn)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _num(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def _more_than_one(v):
    return "yes" if isinstance(v, str) and v.isdigit() and int(v) > 1 else f"no ({v})"


def _reports_matching(messages, pattern):
    """-> how many scan reports match pattern, or the failure that left none to count."""
    if not isinstance(messages, list):
        return messages
    return sum(1 for line in messages if re.search(pattern, line))


def _own_groups(r):
    g = r["groups2"]
    return rf"zone map read: probes={g} opens=1$" if isinstance(g, str) and g.isdigit() else None


def test_native_zonemap_session_fixture_has_several_groups(zs, expect):
    expect.text(_more_than_one(zs["groups"]), "yes", "the fixture has more than one chunk group")


def test_native_zonemap_session_scan_probes_once_per_group(zs, expect):
    expect.num(_num(zs["probes"]), _num(zs["groups"]), "the scan really probed the zone maps, once per group")


def test_native_zonemap_session_scan_resolves_zone_map_once(zs, expect):
    expect.num(_num(zs["opens"]), 1, "the scan resolves zone_map ONCE, not once per group (#744)")


def test_native_zonemap_session_second_column_still_opens_once(zs, expect):
    expect.num(_num(zs["opens2"]), 1, "a second predicate column still resolves zone_map once")


def test_native_zonemap_session_short_circuit_adds_one_probe(zs, expect):
    g = _num(zs["groups"])
    expect.num(_num(zs["probes2"]), g + 1 if isinstance(g, int) else g,
               "and the short circuit means it adds one probe, not one per group")


def test_native_zonemap_session_zs2_has_several_groups(zs, expect):
    expect.text(_more_than_one(zs["groups2"]), "yes", "zs2 has more than one chunk group")


def test_native_zonemap_session_nested_scans_answer(zs, expect):
    expect.num(_num(zs["nested"]), 1000, "nested columnar scans return the right answer")


def test_native_zonemap_session_nested_scans_hold_their_own_handle(zs, expect):
    pattern = _own_groups(zs)
    got = _reports_matching(zs["nested reports"], pattern) if pattern else zs["groups2"]
    expect.num(got, 2, "nested columnar scans each hold their own handle")


def _abort_part(zs, i):
    a = zs["abort"]
    return a[i] if isinstance(a, tuple) else a


def test_native_zonemap_session_middle_scan_errored(zs, expect):
    errors = _abort_part(zs, 1)
    got = sum(1 for e in errors if "division by zero" in e) if isinstance(errors, list) else errors
    expect.num(got, 1, "the middle scan really did error inside the subtransaction")


def test_native_zonemap_session_every_scan_reports_its_own_session(zs, expect):
    expect.num(_reports_matching(_abort_part(zs, 2), r"zone map read: probes="), 3,
               "every scan around an aborted one reports its own session (#744)")


def test_native_zonemap_session_each_scan_opened_once(zs, expect):
    expect.num(_reports_matching(_abort_part(zs, 2), r"zone map read: probes=\d* opens=1"), 3,
               "and each of them opened exactly once")


def test_native_zonemap_session_each_scan_probed_its_own_groups(zs, expect):
    pattern = _own_groups(zs)
    got = _reports_matching(_abort_part(zs, 2), pattern) if pattern else zs["groups2"]
    expect.num(got, 3, "and each of them probed exactly its own groups")


def test_native_zonemap_session_scan_after_abort_answers(zs, expect):
    answers = _abort_part(zs, 0)
    got = sum(1 for a in answers if a == "1000") if isinstance(answers, list) else answers
    expect.num(got, 3, "the scan after the subtransaction abort returns the right answer")

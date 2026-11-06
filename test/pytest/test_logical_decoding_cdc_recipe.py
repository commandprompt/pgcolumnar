"""The documented CDC recipe works (#754). The pytest twin of
`test/logical_decoding_cdc_recipe.sh`, every check by the shell's name.

docs/user-guide.md offers a workaround for "a columnar table is not a logical decoding
source": capture rows into a heap table with a row trigger, and decode that. The
recipe's SQL is EXTRACTED FROM THE GUIDE AT RUN TIME and executed, so the guide is the
thing under test; premises assert what the extraction contains, not merely that it ran.

Asserted: the trigger fires on a columnar table and sees the table's values; an UPDATE
is captured as one UPDATE, never as the storage's delete and insert; a DELETE captures
the OLD row; the capture is transactional, read inside the transaction before it is
rolled back so the rollback has something to undo; the capture table decodes through a
slot while the columnar table emits nothing; a publication FOR ALL TABLES includes
pgcolumnar's internal tables; and a bulk load writes one capture row per loaded row.

A PRIVATE CLUSTER at wal_level=logical: the level cannot be raised without a restart,
and the shared cluster runs at the default.
"""
import pathlib
import re
import shutil
import tempfile

import psycopg
import pytest

from pgc_vacuity import query_error

GUIDE = pathlib.Path(__file__).resolve().parents[2] / "docs" / "user-guide.md"
SLOT = "pgc_cdc"


def _err(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _q(dsn, sql, params=None):
    """-> the first column of the first row as text, or query_error() carrying the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute(sql, params).fetchone()
            return "" if row is None or row[0] is None else str(row[0])
    except psycopg.Error as exc:
        return _err(exc)


def _run(dsn, sql):
    """-> None, or query_error() when the statement failed (the next reading then fails by name)."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql)
        return None
    except psycopg.Error as exc:
        return _err(exc)


def _int(v):
    return int(v) if isinstance(v, str) and re.fullmatch(r"-?\d+", v) else v


def _extract(text):
    """The first ```sql fence inside the CDC section, as the shell's awk reads it."""
    out, in_sec, in_sql, seen = [], False, False, False
    for line in text.splitlines():
        if not in_sec:
            in_sec = line.startswith("## Capture changes for replication or CDC")
            continue
        if not in_sql and line.startswith("## "):
            break
        if not in_sql and not seen and line == "```sql":
            in_sql = seen = True
            continue
        if in_sql and line == "```":
            break
        if in_sql:
            out.append(line)
    return "\n".join(out)


@pytest.fixture(scope="module")
def cdc(pgc_cluster):
    from pgc_cluster import Cluster, pick_port

    if not (pathlib.Path(pgc_cluster.libdir) / "test_decoding.so").is_file():
        yield {"missing": f"test_decoding.so is not in {pgc_cluster.libdir}"}
        return
    r = {"guide": "found" if GUIDE.is_file() else f"missing ({GUIDE})"}
    recipe = _extract(GUIDE.read_text()) if GUIDE.is_file() else ""
    r["recipe"] = recipe
    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-ldcr-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "ldcr", datadir, pick_port(27))
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\nwal_level=logical\nmax_replication_slots=8\nmax_wal_senders=8\n")
        cluster.start()
        dsn = cluster.dsn()
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r["wal_level"] = _q(dsn, "SHOW wal_level")
        # The guide's own SQL, as the shell's psql -f runs it: statement errors do not stop it.
        if recipe:
            _run(dsn, recipe)
        am = "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam WHERE c.relname = %s"
        r["events am"] = _q(dsn, am, ("events",))
        r["cdc am"] = _q(dsn, am, ("events_cdc",))
        _run(dsn, f"SELECT pg_create_logical_replication_slot('{SLOT}', 'test_decoding')")
        r["slot type"] = _q(dsn, f"SELECT slot_type FROM pg_replication_slots WHERE slot_name = '{SLOT}'")

        def n(sql):
            return _int(_q(dsn, sql))

        # 1. the trigger fires on a columnar table
        _run(dsn, "INSERT INTO events VALUES (1,'sale',100), (2,'refund',50), (3,'sale',75)")
        r["inserted"] = n("SELECT count(*) FROM events_cdc WHERE op = 'INSERT'")
        r["seen"] = _q(dsn, "SELECT string_agg(id||':'||kind||':'||amount, ',' ORDER BY id) "
                            "FROM events_cdc WHERE op='INSERT'")
        r["held"] = _q(dsn, "SELECT string_agg(id||':'||kind||':'||amount, ',' ORDER BY id) FROM events")
        # 2. an UPDATE arrives as one UPDATE
        before_ins = n("SELECT count(*) FROM events_cdc WHERE op='INSERT'")
        before_del = n("SELECT count(*) FROM events_cdc WHERE op='DELETE'")
        _run(dsn, "UPDATE events SET amount = 150 WHERE id = 1")
        r["updates"] = n("SELECT count(*) FROM events_cdc WHERE op='UPDATE'")
        r["update row"] = _q(dsn, "SELECT op||':'||id||':'||amount FROM events_cdc WHERE op='UPDATE'")
        r["dels after update"] = (n("SELECT count(*) FROM events_cdc WHERE op='DELETE'"), before_del)
        r["ins after update"] = (n("SELECT count(*) FROM events_cdc WHERE op='INSERT'"), before_ins)
        # a DELETE captures the OLD row
        _run(dsn, "DELETE FROM events WHERE id = 2")
        r["delete row"] = _q(dsn, "SELECT op||':'||id||':'||kind||':'||amount FROM events_cdc WHERE op='DELETE'")
        r["gone"] = n("SELECT count(*) FROM events WHERE id = 2")
        # 3. the capture is transactional: read inside the transaction, then roll back
        cap_before = n("SELECT count(*) FROM events_cdc")
        try:
            with psycopg.connect(dsn) as c:
                c.execute("INSERT INTO events VALUES (99,'rolledback',1)")
                row = c.execute("SELECT ((SELECT count(*) FROM events_cdc) - %s) || ':' || "
                                "(SELECT count(*) FROM events WHERE id = 99)",
                                (cap_before if isinstance(cap_before, int) else 0,)).fetchone()
                r["inside"] = str(row[0])
                c.rollback()
        except psycopg.Error as exc:
            r["inside"] = _err(exc)
        r["after rollback"] = (n("SELECT count(*) FROM events_cdc"), cap_before)
        r["row 99"] = n("SELECT count(*) FROM events WHERE id = 99")
        _run(dsn, "INSERT INTO events VALUES (98,'committed',1)")
        r["after commit"] = (n("SELECT count(*) FROM events_cdc"),
                             cap_before + 1 if isinstance(cap_before, int) else cap_before)
        # 4. the capture table decodes, and the columnar table still does not
        _run(dsn, f"CREATE TABLE decoded AS SELECT data FROM pg_logical_slot_get_changes('{SLOT}', NULL, NULL)")

        def d(pattern):  # LIKE, as the shell's d()
            return _int(_q(dsn, "SELECT count(*) FROM decoded WHERE data LIKE %s", (f"%{pattern}%",)))

        def dq(text):  # strpos, as the shell's dq()
            return _int(_q(dsn, "SELECT count(*) FROM decoded WHERE strpos(data, %s) > 0", (text,)))

        r["decoded"] = n("SELECT count(*) FROM decoded")
        r["col decoded"] = d("table public.events:")
        r["cdc decoded"] = d("table public.events_cdc: INSERT")
        r["captured"] = n("SELECT count(*) FROM events_cdc")
        r["rolledback decoded"] = d("rolledback")
        r["op update"] = dq("op[text]:'UPDATE'")
        r["op delete"] = dq("op[text]:'DELETE'")
        r["op insert"] = dq("op[text]:'INSERT'")
        # the guide's warning about the pgcolumnar schema
        _run(dsn, "CREATE PUBLICATION pgc_all FOR ALL TABLES")
        r["pub internal"] = n("SELECT count(*) FROM pg_publication_tables "
                              "WHERE pubname='pgc_all' AND schemaname='pgcolumnar'")
        _run(dsn, "DROP PUBLICATION pgc_all")
        # the guide's cost claim
        bulk_before = n("SELECT count(*) FROM events_cdc")
        _run(dsn, "INSERT INTO events SELECT g, 'bulk', g FROM generate_series(1000, 6000) g")
        bulk_after = n("SELECT count(*) FROM events_cdc")
        r["bulk"] = (bulk_after - bulk_before if isinstance(bulk_after, int) and isinstance(bulk_before, int)
                     else f"no reading ({bulk_before} then {bulk_after})")
        _run(dsn, f"SELECT pg_drop_replication_slot('{SLOT}')")
        yield r
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def test_ldcr_test_decoding_is_installed(cdc, expect):
    # The shell's pgc_skip records a FAIL under this name when the plugin is missing, and
    # nothing when it is there. Here the same name passes when it is there.
    expect.text("missing" if "missing" in cdc else "installed", "installed",
                "test_decoding.so is not installed; build contrib/test_decoding")


def _declined(cdc, expect):
    if "missing" in cdc:
        expect.cannot_run("MISSING_DEPENDENCY", cdc["missing"])
        return True
    return False


def _has(cdc, text):
    return "yes" if text in cdc["recipe"] else "no"


def _gt(v, floor):
    return "yes" if isinstance(v, int) and v > floor else f"no ({v})"


def test_ldcr_server_runs_at_wal_level_logical(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["wal_level"], "logical", "premise: the server is running at wal_level=logical")


def test_ldcr_guide_is_where_expected(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["guide"], "found", "premise: the guide is where this suite expects it")


def test_ldcr_extraction_is_not_empty(cdc, expect):
    if not _declined(cdc, expect):
        expect.text("nonempty" if cdc["recipe"] else "EMPTY", "nonempty", "premise: the extraction is not empty")


def test_ldcr_recipe_contains_events(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_has(cdc, "CREATE TABLE events"), "yes",
                    "premise: the extracted recipe contains [CREATE TABLE events]")


def test_ldcr_recipe_contains_events_cdc(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_has(cdc, "CREATE TABLE events_cdc"), "yes",
                    "premise: the extracted recipe contains [CREATE TABLE events_cdc]")


def test_ldcr_recipe_contains_capture_function(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_has(cdc, "CREATE FUNCTION events_capture"), "yes",
                    "premise: the extracted recipe contains [CREATE FUNCTION events_capture]")


def test_ldcr_recipe_contains_trigger(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_has(cdc, "CREATE TRIGGER events_cdc_t"), "yes",
                    "premise: the extracted recipe contains [CREATE TRIGGER events_cdc_t]")


def test_ldcr_recipe_contains_using_pgcolumnar(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_has(cdc, "USING pgcolumnar"), "yes",
                    "premise: the extracted recipe contains [USING pgcolumnar]")


def test_ldcr_recipe_contains_delete_branch(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_has(cdc, "TG_OP = 'DELETE'"), "yes",
                    "premise: the extracted recipe contains [TG_OP = 'DELETE']")


def test_ldcr_recipe_contains_old_id(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_has(cdc, "OLD.id"), "yes", "premise: the extracted recipe contains [OLD.id]")


def test_ldcr_recipe_is_the_whole_block(cdc, expect):
    if not _declined(cdc, expect):
        n = cdc["recipe"].count(";")
        expect.text("yes" if n >= 4 else f"no ({n} statements)", "yes",
                    "premise: and it is the whole block, not a fragment")


def test_ldcr_events_is_columnar(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["events am"], "pgcolumnar", "premise: events is a COLUMNAR table")


def test_ldcr_events_cdc_is_heap(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["cdc am"], "heap", "premise: events_cdc is a HEAP table")


def test_ldcr_slot_exists_and_is_logical(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["slot type"], "logical", "premise: the slot exists and is logical")


def test_ldcr_trigger_fires_once_per_row(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["inserted"], 3, "a row trigger on a COLUMNAR table fires once per inserted row")


def test_ldcr_trigger_saw_the_table_values(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["seen"], cdc["held"], "and it saw the same values the table holds")


def test_ldcr_update_captures_one_row(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["updates"], 1, "an UPDATE captures exactly one row")


def test_ldcr_update_recorded_with_new_value(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["update row"], "UPDATE:1:150", "and it is recorded as UPDATE with the NEW value")


def test_ldcr_update_leaks_no_delete(cdc, expect):
    if not _declined(cdc, expect):
        got, want = cdc["dels after update"]
        expect.num(got, want, "the update did NOT leak the storage's internal DELETE")


def test_ldcr_update_leaks_no_insert(cdc, expect):
    if not _declined(cdc, expect):
        got, want = cdc["ins after update"]
        expect.num(got, want, "nor the storage's internal INSERT")


def test_ldcr_delete_captures_old_values(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["delete row"], "DELETE:2:refund:50",
                    "a DELETE captures the OLD values, which are gone from the table")


def test_ldcr_deleted_row_is_gone(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["gone"], 0, "and the row really is gone from the columnar table")


def test_ldcr_inside_the_transaction_the_row_was_captured(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(cdc["inside"], "1:1",
                    "inside the transaction the row was captured and is in the columnar table, before the rollback")


def test_ldcr_rollback_captures_nothing(cdc, expect):
    if not _declined(cdc, expect):
        got, want = cdc["after rollback"]
        expect.num(got, want, "a rolled-back transaction captures nothing")


def test_ldcr_rollback_leaves_no_row(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["row 99"], 0, "and leaves no row in the columnar table either")


def test_ldcr_committed_insert_captures(cdc, expect):
    if not _declined(cdc, expect):
        got, want = cdc["after commit"]
        expect.num(got, want, "premise: the identical insert COMMITTED does capture, so the arm above is real")


def test_ldcr_slot_decoded_something(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_gt(cdc["decoded"], 0), "yes", "premise: the slot decoded something at all")


def test_ldcr_columnar_table_emits_nothing(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["col decoded"], 0, "CONTROL: the columnar table itself still emits nothing")


def test_ldcr_capture_table_is_carried(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_gt(cdc["cdc decoded"], 0), "yes", "the capture table IS carried by the slot")


def test_ldcr_one_decoded_row_per_capture(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["cdc decoded"], cdc["captured"], "one decoded capture row per captured change")


def test_ldcr_rolled_back_row_not_in_stream(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(str(cdc["rolledback decoded"]), "0", "and the rolled-back row is not in the stream")


def test_ldcr_stream_carries_update(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["op update"], 1, "the decoded stream carries the UPDATE as an UPDATE")


def test_ldcr_stream_carries_delete(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["op delete"], 1, "and the DELETE as a DELETE")


def test_ldcr_matcher_finds_inserts(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_gt(cdc["op insert"], 1), "yes",
                    "premise: the same matcher finds the INSERTs, so a 1 above is a real match")


def test_ldcr_publication_includes_internal_tables(cdc, expect):
    if not _declined(cdc, expect):
        expect.text(_gt(cdc["pub internal"], 0), "yes",
                    "premise: FOR ALL TABLES really does include pgcolumnar's internal tables")


def test_ldcr_bulk_load_one_capture_per_row(cdc, expect):
    if not _declined(cdc, expect):
        expect.num(cdc["bulk"], 5001, "a bulk load writes exactly one capture row per loaded row")

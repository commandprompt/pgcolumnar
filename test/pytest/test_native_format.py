"""The native format is stamped, guarded on read, and the stamp is a RANGE (#240, #1137).

Two versions guard a table. The metapage in block 0 stamps versionMajor/versionMinor,
and `PgColumnarReadMetapage` rejects a major this build does not understand.
`pgcolumnar.storage.format_version` is the data-format stamp. Every decode entry
point checks it, so a future encoding that keeps the metapage layout is rejected,
not misread. 1.0-alpha5 stamps 2 (chunks with no validity bitmap, #1130), still
reads 1, and an append to a table stamped 1 raises its stamp.

Each range arm runs one query per entry point that checks the stamp:

    seq scan        PgColumnarBeginReadWithStorage   a non-aggregate SELECT
    aggregate scan  PgColumnarBeginAggScan           count(*)
    group agg scan  PgColumnarBeginGroupAggScan      GROUP BY
    row fetch       pgcolumnar_fetch_row             index scan, custom scan OFF

The custom scan has to be off for the fourth. With it on, the index query is served
by pgcolumnar's own node and never reaches `pgcolumnar_fetch_row`.

The GROUP BY query does not prove the group agg scan's own check. It plans the group
agg node, but with that node's stamp check removed no arm here goes red (#1531
review): an earlier check refuses the query first.

THE PORT OF test/native_format.sh, BY NAME (#1341). The metapage version is planted
through the internal test hook `pgcolumnar_debug_set_metapage_version`, bound here
as the shell binds it (deliberately not in the shipped catalog). A catalog UPDATE
stands in for another build's format_version. Independent of the shell suite:
neither file reads or runs the other.

ONE CHECK PER TEST (#1339). The `nf` module fixture runs the shell's sequence once,
in its order, and records every reading. Each test asserts one of them, so a red
cannot stop the next check from running. A failed reading is a `query_error()`.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DIVERSE = "id bigint, a int, b text, c numeric(12,4), d float8, e bool, f timestamptz, g bytea"
DIVERSE_LOAD = ("SELECT s, (s * 7) % 1000, md5(s::text), (s * 1.25)::numeric(12,4), "
                "s::float8 / 3, (s % 2 = 0), "
                "'2020-01-01'::timestamptz + (s || ' minutes')::interval, "
                "decode(md5(s::text), 'hex') FROM generate_series(1, 5000) s")
# The severity a refusal is raised at. Named here, not spelled in the except handler,
# where test_failed_query_sentinel.py's scan reads any string starting "error" as a
# failure the handler minted (#1527); this one is compared, never returned.
REFUSAL_SEVERITY = "ERROR"
FETCH_GUCS = ("pgcolumnar.enable_custom_scan", "enable_seqscan", "enable_bitmapscan")


def _failed(exc):
    """A failed reading. A backend that died carries no primary message."""
    return query_error(exc.diag.message_primary or (str(exc).splitlines() or [""])[0]
                       or type(exc).__name__)


def _session(db):
    """A NEW session on the suite's schema, as each of the shell's psql calls is.
    The read paths cache a table's format check per session, so a reading on a
    shared connection could skip a check the shell's next psql would make."""
    dsn, schema = db
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(f"SET search_path = {schema}, public")
    return conn


def _one(db, sql):
    """The first column of the first row, or a query_error naming the failure."""
    try:
        with _session(db) as conn:
            return conn.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return _failed(exc)


def _refusal(db, statements):
    """(SQLSTATE, message and detail) of the ERROR the statements fail with, or
    ("ok", "") when none did, in a session of their own. The shell's `err_of` and
    `sqlstate_of` read an ERROR line and nothing else: a warning or a FATAL is not
    the refusal, so either one is a failed reading here and reads as no refusal."""
    try:
        with _session(db) as conn:
            for s in statements:
                cur = conn.execute(s)
                if cur.description:
                    cur.fetchall()
        return "ok", ""
    except psycopg.Error as exc:
        if exc.diag.severity_nonlocalized != REFUSAL_SEVERITY or not exc.sqlstate:
            return _failed(exc), ""
        return exc.sqlstate, "\n".join(filter(None, (exc.diag.message_primary,
                                                      exc.diag.message_detail)))


def _four_paths(db, t):
    return " ".join(_refusal(db, s)[0] for s in (
        [f"SELECT v FROM {t} LIMIT 1"],
        [f"SELECT count(*) FROM {t}"],
        [f"SELECT id % 3, count(*) FROM {t} GROUP BY 1"],
        ["SET pgcolumnar.enable_custom_scan = off", "SET enable_seqscan = off",
         "SET enable_bitmapscan = off", f"SELECT v FROM {t} WHERE id = 42"]))


def _alive(db):
    """What the shell's `q 'SELECT 1;'` reads: a NEW session still gets an answer."""
    return _one(db, "SELECT 1")


def _ordered(db, table):
    try:
        with _session(db) as conn:
            return [r[0] for r in conn.execute(
                f"SELECT t::text FROM (SELECT * FROM {table} ORDER BY id) t").fetchall()]
    except psycopg.Error as exc:
        return _failed(exc)


def _stamp(db, table):
    return _one(db, "SELECT format_version FROM pgcolumnar.storage "
                    f"WHERE storage_id = pgcolumnar.get_storage_id('{table}')")


def _run(conn, sql):
    """A setup step. A failure is printed and leaves its readings to name it."""
    try:
        conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- setup failed: {sql[:60]}: {exc.diag.message_primary}")


@pytest.fixture(scope="module")
def nf(pgc_cluster):
    schema = "pgc_native_format"
    c = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    got = {}
    db = (pgc_cluster.dsn(), schema)
    try:
        c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.execute(f"CREATE SCHEMA {schema}")
        c.execute(f"SET search_path = {schema}, public")
        # the internal test hook, deliberately not in the shipped catalog
        _run(c, "CREATE OR REPLACE FUNCTION pgcolumnar.debug_set_metapage_version("
                "regclass, int, int) RETURNS void AS 'pgcolumnar', "
                "'pgcolumnar_debug_set_metapage_version' LANGUAGE C")

        # --- 1 & 3: stamped identifiers and a diverse-type round trip ----------
        _run(c, f"CREATE TABLE t_heap ({DIVERSE})")
        _run(c, f"CREATE TABLE t_col ({DIVERSE}) USING pgcolumnar")
        _run(c, f"INSERT INTO t_heap {DIVERSE_LOAD}")
        _run(c, "INSERT INTO t_col SELECT * FROM t_heap")
        got["format_version"] = _stamp(db, "t_col")
        got["vector_length"] = _one(db, "SELECT vector_length FROM pgcolumnar.storage "
                                       "WHERE storage_id = pgcolumnar.get_storage_id('t_col')")
        got["diverse col"] = _ordered(db, "t_col")
        got["diverse heap"] = _ordered(db, "t_heap")

        # --- 2: an unsupported metapage version is rejected, not misread -------
        _run(c, "CREATE TABLE bad (id int, v text) USING pgcolumnar")
        _run(c, "INSERT INTO bad SELECT s, md5(s::text) FROM generate_series(1, 2000) s")
        got["bad wrote"] = _one(db, "SELECT count(*) FROM bad")
        _run(c, "SELECT pgcolumnar.debug_set_metapage_version('bad', 2, 2)")
        got["bad same major"] = _one(db, "SELECT count(*) FROM bad")
        _run(c, "SELECT pgcolumnar.debug_set_metapage_version('bad', 99, 0)")
        got["bad bumped"] = _refusal(db, ["SELECT count(*) FROM bad"])
        got["bad alive"] = _alive(db)

        # --- 4: the native format_version is enforced on read ------------------
        _run(c, "CREATE TABLE fv (id int, v text) USING pgcolumnar")
        _run(c, "INSERT INTO fv SELECT s, md5(s::text) FROM generate_series(1, 2000) s")
        _run(c, "CREATE INDEX fv_id ON fv (id)")
        got["fv reads"] = _one(db, "SELECT count(*) FROM fv")
        _run(c, "UPDATE pgcolumnar.storage SET format_version = 99 "
                "WHERE storage_id = pgcolumnar.get_storage_id('fv')")
        got["fv scan"] = _refusal(db, ["SELECT v FROM fv LIMIT 1"])
        got["fv agg"] = _refusal(db, ["SELECT count(*) FROM fv"])
        # The custom scan is off, as in part 5: with it on, pgcolumnar's own node
        # serves this query and it never reaches pgcolumnar_fetch_row.
        got["fv idx"] = _refusal(db, ["SET pgcolumnar.enable_custom_scan = off",
                                     "SET enable_seqscan = off", "SET enable_bitmapscan = off",
                                     "SELECT v FROM fv WHERE id = 42"])
        got["fv alive"] = _alive(db)

        # --- 5: the stamp is a range, and an append raises it (#1137) ----------
        _run(c, "CREATE TABLE fr (id int, v text) USING pgcolumnar")
        _run(c, "INSERT INTO fr SELECT s, md5(s::text) FROM generate_series(1, 2000) s")
        _run(c, "CREATE INDEX fr_id ON fr (id)")
        got["fr stamped"] = _stamp(db, "fr")
        _run(c, "UPDATE pgcolumnar.storage SET format_version = 1 "
                "WHERE storage_id = pgcolumnar.get_storage_id('fr')")
        got["fr 1 paths"] = _four_paths(db, "fr")
        got["fr 1 rows"] = str(_one(db, "SELECT count(*) || '|' || sum(id) FROM fr"))
        got["fr 1 stamp"] = _stamp(db, "fr")
        _run(c, "INSERT INTO fr SELECT s, md5(s::text) FROM generate_series(2001, 2100) s")
        got["fr appended stamp"] = _stamp(db, "fr")
        got["fr appended rows"] = str(_one(db, "SELECT count(*) || '|' || sum(id) FROM fr"))
        _run(c, "UPDATE pgcolumnar.storage SET format_version = 3 "
                "WHERE storage_id = pgcolumnar.get_storage_id('fr')")
        got["fr 3 paths"] = _four_paths(db, "fr")
        _run(c, "UPDATE pgcolumnar.storage SET format_version = 0 "
                "WHERE storage_id = pgcolumnar.get_storage_id('fr')")
        got["fr 0 paths"] = _four_paths(db, "fr")
        got["fr alive"] = _alive(db)
        yield got
    finally:
        try:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        except psycopg.Error:
            pass
        c.close()


def _fails(reading):
    """The shell's `[ -n "$emsg" ] && echo yes || echo no`: an ERROR was raised."""
    return "yes" if len(reading[0]) == 5 and reading[0].isalnum() else "no"


def _says(reading, text):
    """The shell's `contains "$emsg" TEXT`, over the message and its detail."""
    return "yes" if text in reading[1] else f"no: {reading}"


# ---- 1 & 3 -------------------------------------------------------------------------

def test_the_native_format_version_is_2(nf, expect):
    expect.num(nf["format_version"], 2, "native format_version is 2")


def test_the_native_vector_length_is_1024(nf, expect):
    expect.num(nf["vector_length"], 1024, "native vector_length is 1024")


def test_a_diverse_type_round_trip_matches_heap(nf, expect):
    expect.ordered_rows(nf["diverse col"], nf["diverse heap"],
                        "diverse-type round-trip matches heap")


# ---- 2: the metapage version ---------------------------------------------------------

def test_the_table_reads_at_the_version_this_build_wrote(nf, expect):
    expect.num(nf["bad wrote"], 2000, "table reads at the version this build wrote")


def test_the_metapage_major_this_build_writes_is_still_2(nf, expect):
    """Re-stamping the current major must be a no-op the read accepts. Bump
    COLUMNAR_VERSION_MAJOR by accident and this goes red; on a deliberate bump,
    change the 2 in the hook call in the same commit."""
    expect.num(nf["bad same major"], 2000, "the metapage major this build writes is still 2")


def test_a_read_of_the_bumped_version_fails(nf, expect):
    expect.text(_fails(nf["bad bumped"]), "yes",
                "a read of the bumped version fails (not silently misread)")


def test_the_failure_is_the_format_version_guard(nf, expect):
    expect.text(_says(nf["bad bumped"], "unsupported columnar format version"), "yes",
                "the failure is the format-version guard")


def test_the_error_names_the_offending_version(nf, expect):
    expect.text(_says(nf["bad bumped"], "99.0"), "yes", "the error names the offending version")


def test_the_backend_survives_the_rejection(nf, expect):
    expect.num(nf["bad alive"], 1, "backend survives the rejection")


# ---- 4: the native format_version ------------------------------------------------------

def test_the_table_reads_at_the_native_format_version_this_build_writes(nf, expect):
    expect.num(nf["fv reads"], 2000, "table reads at the native format_version this build writes")


# (name, reading): a literal table, so the twin grader reads its names.
FUTURE_REFUSED = [
    ("a seq-scan read of a future native format_version fails (not silently misread)",
     "fv scan"),
    ("an aggregate over a future native format_version fails (not silently misread)",
     "fv agg"),
    ("an index-scan fetch of a decoded column fails (not silently misread)", "fv idx"),
]


@pytest.mark.parametrize("name, reading", FUTURE_REFUSED)
def test_a_future_native_format_version_is_refused(nf, expect, name, reading):
    expect.text(_fails(nf[reading]), "yes", name)


def test_the_failure_is_the_native_format_version_guard(nf, expect):
    expect.text(_says(nf["fv idx"], "unsupported columnar native format version"), "yes",
                "the failure is the native-format-version guard")


def test_the_native_format_error_names_the_offending_version(nf, expect):
    expect.text(_says(nf["fv idx"], "99"), "yes",
                "the native-format error names the offending version")


def test_the_backend_survives_the_native_format_rejection(nf, expect):
    expect.num(nf["fv alive"], 1, "backend survives the native-format rejection")


# ---- 5: the stamp is a range ---------------------------------------------------------

def test_the_fixture_was_stamped_by_this_build(nf, expect):
    expect.num(nf["fr stamped"], 2, "premise: the fixture was stamped by this build")


def test_a_table_stamped_1_is_read_on_all_four_paths(nf, expect):
    expect.text(nf["fr 1 paths"], "ok ok ok ok", "a table stamped 1 is read on all four paths")


def test_a_table_stamped_1_returns_its_rows(nf, expect):
    expect.text(nf["fr 1 rows"], "2000|2001000", "and returns its rows")


def test_reading_a_table_stamped_1_does_not_raise_its_stamp(nf, expect):
    expect.num(nf["fr 1 stamp"], 1, "reading a table stamped 1 does not raise its stamp")


def test_appending_to_a_table_stamped_1_raises_its_stamp(nf, expect):
    expect.num(nf["fr appended stamp"], 2, "appending to a table stamped 1 raises its stamp")


def test_the_appended_table_still_reads_whole(nf, expect):
    expect.text(nf["fr appended rows"], "2100|2206050", "and the table still reads whole")


def test_a_stamp_above_this_builds_is_refused(nf, expect):
    expect.text(nf["fr 3 paths"], "0A000 0A000 0A000 0A000",
                "a stamp above this build's is refused on all four paths")


def test_a_stamp_below_min_readable_is_refused(nf, expect):
    expect.text(nf["fr 0 paths"], "0A000 0A000 0A000 0A000",
                "a stamp below MIN_READABLE is refused on all four paths")


def test_the_backend_survives_the_range_rejections(nf, expect):
    expect.num(nf["fr alive"], 1, "backend survives the range rejections")

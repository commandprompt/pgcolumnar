"""Z-order clustering reorders storage, tightens the zone maps, and refuses what it
cannot cluster on. The pytest twin of `test/native_cluster.sh`, every check by the
shell's name (#1341).

pgcolumnar.cluster(table, cols) physically reorders rows by the Z-order (Morton) curve
over the given columns, so the per-group min/max zone maps of every clustered column
tighten at once. Asserted, on 40960 rows in 20 chunk groups with x and y scattered by
coprime hashes: the table answers as a heap mirror built from the same generator before
and after clustering; a 2D box skips more groups after clustering by (x, y), and most of
them; clustering on id alone brings back an id range's skipping, which the (x, y)
clustering scattered; and an unsupported type and a missing column are each refused by
their own guard, with the backend still up.

WHAT A REFUSAL READING IS: the SQLSTATE when the statement is refused with the guard's
own words, the SQLSTATE with the other guard's words, "no error", or a failed reading
when the server could not be asked. The refusals used to accept any failure, a crash
included: with the unsupported-type refusal replaced by abort(), both passed. The
survival check reads the server log's count of backends killed by a signal as well as
SELECT 1, which answers again once crash recovery has finished.

WHAT A SKIP READING IS: the "Columnar Chunk Groups Removed by Filter" count of an
EXPLAIN ANALYZE, or a failed reading. An unread count used to default to 0, and a
"more than before" comparison then beat it for free.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.

THE HOW-TO'S EXAMPLES (#2093). Its first example creates `events`, and its sort,
bloom, projection and index examples must run on that table as written. They used
`customer_id` and `order_id`, which the table did not have, and clustered on a
numeric column, which cluster refuses. Each block is read from docs/how-to.md by
this file's own reader and run through psycopg, apart from the shell's awk and psql.
"""
import pathlib
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_cluster"
GEN = """SELECT g,
       ((g::bigint * 7919) % 200)::int   AS x,
       ((g::bigint * 104729) % 200)::int AS y,
       'p' || (g % 97)                    AS payload
  FROM generate_series(1, 40960) g"""
BOX = "x BETWEEN 20 AND 40 AND y BETWEEN 20 AND 40"
IDR = "id BETWEEN 1 AND 2048"


HOWTO = pathlib.Path(__file__).resolve().parents[2] / "docs" / "how-to.md"


def _howto_sql(heading, n):
    """-> the nth ```sql block in the how-to section whose heading starts with
    `heading`, or None."""
    lines = HOWTO.read_text().split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return None
    k, fence, body = 0, None, []
    for line in lines[start + 1:]:
        if line.startswith("```"):
            if fence is None:
                fence = line[3:].strip()
                if fence == "sql":
                    k += 1
                continue
            if fence == "sql" and k == n:
                return "\n".join(body)
            fence = None
            continue
        if fence is None and line.startswith("#"):
            return None
        if fence == "sql" and k == n:
            body.append(line)
    return None


def _statements(sql):
    """-> the statements of a doc block, one per string, as psql sends them. One
    multi-statement string would run as one implicit transaction, where VACUUM fails."""
    out = []
    for part in re.split(r";[ \t]*(?:--[^\n]*)?(?:\n|$)", sql):
        if re.sub(r"--[^\n]*", "", part).strip():
            out.append(part)
    return out


def _run_howto(dsn, heading, *blocks):
    """-> "ok" when every block runs, else the first error or the missing block."""
    for n in blocks:
        sql = _howto_sql(heading, n)
        if not sql:
            return f"no block {n} under '{heading}'"
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                for stmt in _statements(sql):
                    conn.execute(stmt)
        except psycopg.Error as exc:
            return query_error(_message(exc))
    return "ok"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- step failed: {_message(exc)}")


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


def _set_hash(dsn, query):
    """-> md5 of the rows' text, sorted, as the shell's pgc_set_hash; EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _is_columnar_scan(dsn, query):
    """-> yes|no: the plan carries the columnar scan's own marker, as the shell's helper."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS OFF) {query}"))
    except psycopg.Error:
        return "no"
    return "yes" if "Columnar Projected Columns" in plan else "no"


def _skipped(dsn, query):
    """-> the chunk groups the zone maps removed for query, or a failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = "\n".join(r[0] for r in conn.execute(
                f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {query}"))
    except psycopg.Error as exc:
        return query_error(_message(exc))
    m = re.search(r"Columnar Chunk Groups Removed by Filter: (\d+)", plan)
    return int(m.group(1)) if m else query_error("no 'Chunk Groups Removed by Filter' line")


def _more_than(before, after):
    if not (isinstance(before, int) and isinstance(after, int)):
        return f"no reading (before=[{before}] after=[{after}])"
    return "yes" if after > before else f"no (before={before} after={after})"


def _at_least_10(n):
    if not isinstance(n, int):
        return f"no reading [{n}]"
    return "yes" if n >= 10 else f"no ({n} of 20)"


def _more_and_most(before, after):
    if not (isinstance(before, int) and isinstance(after, int)):
        return f"no reading (before=[{before}] after=[{after}])"
    return "yes" if after > before and after >= 10 else f"no (before={before} after={after} of 20)"


def _await_up(dsn):
    """Wait, at most 60 s, until the server answers, as the shell's await_up."""
    for _ in range(120):
        if _value(dsn, "SELECT 1") == "1":
            return
        time.sleep(0.5)


def _refusal(dsn, sql, phrase):
    """-> the SQLSTATE when sql is refused with phrase in its primary message, the
    SQLSTATE with the other guard's message, "no error", or query_error()."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return exc.sqlstate if phrase in msg else f"{exc.sqlstate} (another refusal: {msg})"
    return "no error"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    _await_up(dsn)
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


@pytest.fixture(scope="module")
def nc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    dsn = pgc_cluster.dsn(dbname=DB)
    try:
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE h (id int, x int, y int, payload text)")
        _run(dsn, "CREATE TABLE n (id int, x int, y int, payload text) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024)")
        _run(dsn, f"INSERT INTO h {GEN}")
        _run(dsn, f"INSERT INTO n {GEN}")
        r = {}
        r["count"] = _value(dsn, "SELECT count(*) FROM n")
        r["box pre"] = (_set_hash(dsn, f"SELECT id, x, y, payload FROM n WHERE {BOX}"),
                        _set_hash(dsn, f"SELECT id, x, y, payload FROM h WHERE {BOX}"))
        r["custom scan"] = _is_columnar_scan(dsn, f"SELECT id FROM n WHERE {BOX}")
        before = _skipped(dsn, f"SELECT id FROM n WHERE {BOX}")

        _run(dsn, "SELECT pgcolumnar.cluster('n', 'x', 'y')")
        r["count after"] = _value(dsn, "SELECT count(*) FROM n")
        r["box post"] = (_set_hash(dsn, f"SELECT id, x, y, payload FROM n WHERE {BOX}"),
                         _set_hash(dsn, f"SELECT id, x, y, payload FROM h WHERE {BOX}"))
        r["full"] = (_set_hash(dsn, "SELECT id, x, y, payload FROM n ORDER BY id"),
                     _set_hash(dsn, "SELECT id, x, y, payload FROM h ORDER BY id"))
        after = _skipped(dsn, f"SELECT id FROM n WHERE {BOX}")
        print(f"  (chunk groups skipped for the box: before={before} after={after} of 20)")
        r["more"] = _more_than(before, after)
        r["most"] = _at_least_10(after)

        id_before = _skipped(dsn, f"SELECT id FROM n WHERE {IDR}")
        _run(dsn, "SELECT pgcolumnar.cluster('n', 'id')")
        id_after = _skipped(dsn, f"SELECT id FROM n WHERE {IDR}")
        print(f"  (chunk groups skipped for the id range: before={id_before} after={id_after} of 20)")
        r["single skip"] = _more_and_most(id_before, id_after)
        r["single parity"] = (_set_hash(dsn, "SELECT id, x, y, payload FROM n ORDER BY id"),
                              _set_hash(dsn, "SELECT id, x, y, payload FROM h ORDER BY id"))

        crashes_before = _crashes(pgc_cluster)
        r["unsupported"] = _refusal(dsn, "SELECT pgcolumnar.cluster('n', 'payload')",
                                    'column "payload" of type text cannot be used as a clustering key')
        r["missing"] = _refusal(dsn, "SELECT pgcolumnar.cluster('n', 'nope')",
                                'column "nope" does not exist in table "n"')
        r["up"] = _survived(dsn, pgc_cluster, crashes_before)

        r["howto create"] = _run_howto(dsn, "## Create a columnar table", 1)
        _run(dsn, "INSERT INTO events (id, ts, region) "
                  "SELECT g, timestamptz '2026-01-01 00:00+00' + g * interval '1 minute', "
                  "'r' || (g % 5) FROM generate_series(1, 5000) g")
        r["howto sort"] = _run_howto(dsn, "## Sort a table to skip more chunk groups", 1, 3)
        r["howto bloom"] = _run_howto(dsn, "## Skip more chunk groups on equality filters", 1)
        r["howto projection"] = _run_howto(dsn, "## Add a projection for a second sort order", 1)
        r["howto index"] = _run_howto(dsn, "## Add indexes and use index-only scans", 1)
        yield r
    finally:
        _await_up(pgc_cluster.dsn())
        try:
            with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
                a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        except psycopg.Error as exc:
            print(f"-- teardown failed: {_message(exc)}")


def test_native_cluster_row_count(nc, expect):
    expect.text(nc["count"], "40960", "row count")


def test_native_cluster_box_parity_before_clustering(nc, expect):
    expect.text(nc["box pre"][0], nc["box pre"][1], "box parity (pre-cluster)")


def test_native_cluster_plan_is_the_columnar_scan(nc, expect):
    expect.text(nc["custom scan"], "yes", "the plan under test is a columnar custom scan")


def test_native_cluster_row_count_after_clustering(nc, expect):
    expect.text(nc["count after"], "40960", "row count after cluster")


def test_native_cluster_box_parity_after_clustering(nc, expect):
    expect.text(nc["box post"][0], nc["box post"][1], "box parity (post-cluster)")


def test_native_cluster_full_table_parity_after_clustering(nc, expect):
    expect.text(nc["full"][0], nc["full"][1], "full-table parity after cluster")


def test_native_cluster_increases_2d_skipping(nc, expect):
    expect.text(nc["more"], "yes", "clustering increases 2D skipping")


def test_native_cluster_skips_most_groups(nc, expect):
    expect.text(nc["most"], "yes", "clustering skips most groups")


def test_native_cluster_single_column_restores_id_skipping(nc, expect):
    expect.text(nc["single skip"], "yes", "single-column clustering lets an id range skip most groups")


def test_native_cluster_single_column_parity(nc, expect):
    expect.text(nc["single parity"][0], nc["single parity"][1], "single-column cluster parity")


def test_native_cluster_unsupported_type_is_refused(nc, expect):
    expect.text(nc["unsupported"], "0A000", "unsupported type errors cleanly")


def test_native_cluster_missing_column_is_refused(nc, expect):
    expect.text(nc["missing"], "42703", "missing column errors cleanly")


def test_native_cluster_backend_survives_the_refusals(nc, expect):
    expect.text(nc["up"], "1", "backend still up after the refusals")


def test_the_how_to_creates_its_events_table(nc, expect):
    expect.text(nc["howto create"], "ok", "the how-to's first example creates its events table")


def test_the_how_to_sort_examples_run_on_its_events_table(nc, expect):
    expect.text(nc["howto sort"], "ok", "the how-to's sort examples run on that events table")


def test_the_how_to_bloom_example_runs_on_its_events_table(nc, expect):
    expect.text(nc["howto bloom"], "ok", "the how-to's bloom example runs on that events table")


def test_the_how_to_projection_example_runs_on_its_events_table(nc, expect):
    expect.text(nc["howto projection"], "ok", "the how-to's projection example runs on that events table")


def test_the_how_to_index_example_runs_on_its_events_table(nc, expect):
    expect.text(nc["howto index"], "ok", "the how-to's index example runs on that events table")

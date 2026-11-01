"""Iceberg row-level deletes (#388 phases 4a position + 4b equality, phase 5 partition
scope): iceberg_scan reads a table whose current snapshot carries delete files and drops
the deleted rows. The pytest twin of `test/iceberg_deletes.sh`, every check by the
shell's name (#1341).

The fixture is hand-crafted (test/fixtures/iceberg/warehouse_del): real pyarrow data
and delete Parquet files, manifests with exact sequence numbers so both ordering rules
(position: data_seq <= delete_seq; equality: data_seq < delete_seq) sit on testable
boundaries, and an oracle of the surviving rows. The warehouse is relocated so its
recorded root differs from where it lies.

Asserted: position deletes, sequence ordering and inheritance, per-file scoping, the
ordinal-0 control; equality deletes (the strict boundary, multi-column AND, extra
columns, null matching, unions, mixed with position deletes, unprojected columns);
partition-scoped equality deletes; thirteen corrupt or unsupported snapshots refused,
each by its own guard; and no backend dies on the way. A second warehouse
(warehouse_posmany, #1868) has 32 data files and 106 position-delete rows: its arms
check the survivors against the oracle and read the scan's DEBUG1 lines, which must
show one index build and, per data file, only the rows naming that file.

HOW A REFUSAL IS READ. Several guards share a SQLSTATE (XX001, 22023), so a refusal
counts only when its message is that guard's own. A refusal reads as its SQLSTATE,
"<SQLSTATE> (another refusal: ...)", "no error", or a query_error() when the server
could not be asked.

HOW SURVIVAL IS READ. A fresh connection answers again after crash recovery, so
"SELECT 1" alone passed with a backend killed by a refusal arm (abort() in place of the
null-pos refusal; the partition spec's fields guard removed, which segfaults). Each
survival check also requires that the server log's count of backends terminated by a
signal has not moved since its section began. Every reading first waits, bounded, for
the server to answer, so a crash reds its own arm and its section's survival check, not
the arms after it.

ON A PRIVATE DATABASE, with the warehouse copied into `server_dir`, because the backend
reads it. Where the fixture is missing the first test fails under the shell's pgc_skip
message, and the rest decline.
"""
import json
import pathlib
import shutil
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_deletes"
FX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg"
WHD = FX / "warehouse_del"
PMW = FX / "warehouse_posmany"
COLS = "AS t(id bigint, region text, amount int)"
PCOLS = "AS t(id bigint, region text, grp int)"
EQ_APPLIED = ("eqapply", "eqboundary", "eqmulti", "eqextra", "eqnull", "eqtwo", "eqmixed")
# each refusal's own guard, by the phrase its message carries
PHRASE = {
    "nullpos": "has a null pos",
    "negpos": "has a negative pos",
    "eqnoids": "has no equality_ids",
    "eqpart_datanospec": "carries no partition_spec_id, which a partition-scoped",
    "eqpart_incomparable": "has a partition tuple this reader cannot compare",
    "eqtype": "is not supported",
    "eqmissing": "no Parquet column has field id",
    "eqescape": "is not under the table location",
    "eqdotdot": "escapes the table location",
    "eqbigid": "corrupt Avro manifest entry",
    "eqnospec": "the manifest list carries no partition_spec_id",
    "eqnofields": "has no fields array",
    "badseq": "has no sequence number",
}


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else row[0]


def _await_up(dsn):
    """Wait, at most 60 s, until the server answers, as the shell's await_up: a refusal
    that kills a backend sends the server through crash recovery, and the next readings
    would otherwise red as collateral, by timing. _survived() still counts the crash."""
    for _ in range(120):
        if _value(dsn, "SELECT 1") == 1:
            return
        time.sleep(0.5)


def _read(dsn, sql):
    _await_up(dsn)
    return _value(dsn, sql)


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


def _names(dsn, sql, phrase):
    """-> "1" when sql's refusal message carries phrase, "0" when it does not, as the
    shell's grep -c over errmsg_of; query_error() when the server could not be asked."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return "1" if phrase in (exc.diag.message_primary or "") else "0"
    return "0"


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
    return "1" if up == 1 and now == before else f"no ([{up}], {now - before} backend crashes)"


def _pm_probe(dsn, sql):
    """-> (index, per data file) over the DEBUG1 lines of one run of sql, as the shell's
    pm_probe: "builds=B rows=R paths=P" summed over the position-delete index lines,
    and "files=F compared=C visited=V longest=L applied=A" over the per-data-file lines
    (compared, visited and applied summed, longest the largest visited). query_error() for both when the
    server could not be asked."""
    _await_up(dsn)
    lines = []
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.add_notice_handler(lambda d: lines.append(d.message_primary or ""))
            conn.execute("SET client_min_messages = debug1")
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        err = query_error(_message(exc))
        return err, err
    builds = rows = paths = files = compared = visited = longest = applied = 0
    for line in lines:
        f = {}
        for word in line.split():
            key, _, val = word.partition("=")
            if val.isdigit():
                f[key] = int(val)
        if "position-delete index:" in line:
            builds += 1
            rows += f.get("rows", 0)
            paths += f.get("paths", 0)
        elif "iceberg_scan position deletes:" in line:
            files += 1
            compared += f.get("compared", 0)
            visited += f.get("visited", 0)
            longest = max(longest, f.get("visited", 0))
            applied += f.get("applied", 0)
    return (f"builds={builds} rows={rows} paths={paths}",
            f"files={files} compared={compared} visited={visited} longest={longest} applied={applied}")


@pytest.fixture(scope="module")
def dl(pgc_cluster, server_dir):
    if not ((WHD / "db" / "t" / "metadata" / "apply.metadata.json").is_file()
            and (PMW / "expected_posmany.json").is_file()):
        yield None
        return
    dest = server_dir / "del"
    shutil.copytree(WHD / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    mdir = dest / "db" / "t" / "metadata"
    oracle = json.loads((WHD / "expected_deletes.json").read_text())

    def md(name):
        return mdir / (name + ".metadata.json")

    def scan(name, cols=COLS):
        return f"SELECT * FROM pgcolumnar.iceberg_scan('{md(name)}') {cols}"

    def ids(name, cols=COLS):
        return f"SELECT string_agg(id::text, ',' ORDER BY id) FROM pgcolumnar.iceberg_scan('{md(name)}') {cols}"

    def count(name, where="", cols=COLS):
        return f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{md(name)}') {cols} {where}"

    admin = pgc_cluster.dsn()
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {
            "oracle": "\n".join("%d|%s|%d" % (o["id"], o["region"], o["amount"]) for o in oracle["surviving"]),
            "eq oracle": {k: ",".join(str(i) for i in v) for k, v in oracle["eq_surviving"].items()},
            "part oracle": {k: ",".join(str(i) for i in v) for k, v in oracle["part_surviving"].items()},
        }
        # the shell's order, section by section, each with its own crash baseline
        r["data rows"] = _read(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet("
                                    f"'{dest / 'db' / 't' / 'data' / 'data.parquet'}') {COLS}")
        r["apply rows"] = _read(dsn, f"SELECT string_agg(id || '|' || region || '|' || amount, E'\\n' ORDER BY id) "
                                     f"FROM pgcolumnar.iceberg_scan('{md('apply')}') {COLS}")
        r["apply gone"] = _read(dsn, count("apply", "WHERE id IN (2,4)"))
        r["apply count"] = _read(dsn, count("apply"))
        r["apply projected"] = _read(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) "
                                          f"FROM pgcolumnar.iceberg_scan('{md('apply')}') AS t(id bigint)")
        r["noapply count"] = _read(dsn, count("noapply"))
        r["noapply kept"] = _read(dsn, count("noapply", "WHERE id IN (2,4)"))
        r["inherit count"] = _read(dsn, count("inherit"))
        r["wrongpath count"] = _read(dsn, count("wrongpath"))
        before = _crashes(pgc_cluster)
        r["nullpos"] = _refusal(dsn, scan("nullpos"), PHRASE["nullpos"])
        r["nullpos names"] = _names(dsn, scan("nullpos"), "null pos")
        r["up after nullpos"] = _survived(dsn, pgc_cluster, before)
        before = _crashes(pgc_cluster)
        r["negpos"] = _refusal(dsn, scan("negpos"), PHRASE["negpos"])
        r["negpos names"] = _names(dsn, scan("negpos"), "negative pos")
        r["up after negpos"] = _survived(dsn, pgc_cluster, before)
        r["zeropos"] = _read(dsn, ids("zeropos"))
        before = _crashes(pgc_cluster)
        for name in EQ_APPLIED:
            r[name] = _read(dsn, ids(name))
            if name == "eqnull":
                r["eqnull nulls"] = _read(dsn, count("eqnull", "WHERE region IS NULL"))
        r["eq unprojected"] = _read(dsn, f"SELECT string_agg(amount::text, ',' ORDER BY amount) "
                                         f"FROM pgcolumnar.iceberg_scan('{md('eqapply')}') AS t(amount int)")
        r["up after eq applications"] = _survived(dsn, pgc_cluster, before)
        before = _crashes(pgc_cluster)
        r["eqnoids"] = _refusal(dsn, scan("eqnoids"), PHRASE["eqnoids"])
        r["equality count"] = _read(dsn, count("equality"))
        r["eqpart_apply"] = _read(dsn, ids("eqpart_apply", PCOLS))
        r["eqpart_apply us"] = _read(dsn, count("eqpart_apply", "WHERE region = 'us'", PCOLS))
        r["eqpart_nomatch"] = _read(dsn, count("eqpart_nomatch", "", PCOLS))
        r["eqpart_datanospec"] = _refusal(dsn, scan("eqpart_datanospec", PCOLS), PHRASE["eqpart_datanospec"])
        r["eqpart_crossspec"] = _read(dsn, count("eqpart_crossspec", "", PCOLS))
        r["eqpart_incomparable"] = _refusal(dsn, scan("eqpart_incomparable", PCOLS), PHRASE["eqpart_incomparable"])
        r["eqtype"] = _refusal(dsn, scan("eqtype"), PHRASE["eqtype"])
        r["eqtype names"] = _names(dsn, scan("eqtype"), "type")
        r["eqmissing"] = _refusal(dsn, scan("eqmissing"), PHRASE["eqmissing"])
        r["eqescape"] = _refusal(dsn, scan("eqescape"), PHRASE["eqescape"])
        r["eqescape names"] = _names(dsn, scan("eqescape"), "table location")
        r["eqdotdot"] = _refusal(dsn, scan("eqdotdot"), PHRASE["eqdotdot"])
        r["up after eq refusals"] = _survived(dsn, pgc_cluster, before)
        before = _crashes(pgc_cluster)
        r["eqbigid"] = _refusal(dsn, scan("eqbigid"), PHRASE["eqbigid"])
        r["eqnospec"] = _refusal(dsn, scan("eqnospec"), PHRASE["eqnospec"])
        r["eqnofields"] = _refusal(dsn, scan("eqnofields"), PHRASE["eqnofields"])
        r["eqstaletype count"] = _read(dsn, count("eqstaletype"))
        r["v1seq count"] = _read(dsn, count("v1seq"))
        r["up after audit"] = _survived(dsn, pgc_cluster, before)
        before = _crashes(pgc_cluster)
        r["badseq"] = _refusal(dsn, scan("badseq"), PHRASE["badseq"])
        r["up after badseq"] = _survived(dsn, pgc_cluster, before)
        # #1868: many data files, many position-delete rows, in their own warehouse
        pmdest = server_dir / "posmany"
        shutil.copytree(PMW / "db", pmdest / "db")
        for p in [pmdest, *pmdest.rglob("*")]:
            p.chmod(0o777 if p.is_dir() else 0o666)
        pm = json.loads((PMW / "expected_posmany.json").read_text())
        pmmd = pmdest / "db" / "t" / "metadata" / "posmany.metadata.json"
        r["pm oracle md5"] = pm["surviving_md5"]
        r["pm oracle index"] = "builds={builds} rows={rows} paths={paths}".format(**pm["index"])
        r["pm oracle probe"] = "files={files} compared={compared} visited={visited} longest={longest} applied={applied}".format(
            **pm["probe"])
        r["pm data rows"] = _read(dsn, f"SELECT count(*) || ' rows in ' || count(DISTINCT i) || ' files' "
                                       f"FROM generate_series(0, 31) AS i, LATERAL pgcolumnar.read_parquet("
                                       f"format('%s/d%s.parquet', '{pmdest / 'db' / 't' / 'data'}', "
                                       f"lpad(i::text, 2, '0'))) {COLS}")
        r["pm md5"] = _read(dsn, f"SELECT md5(string_agg(id::text, ',' ORDER BY id)) "
                                 f"FROM pgcolumnar.iceberg_scan('{pmmd}') {COLS}")
        r["pm ordinal 0"] = _read(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{pmmd}') {COLS} "
                                       f"WHERE id % 100 = 0")
        r["pm s3 row"] = _read(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{pmmd}') {COLS} "
                                    f"WHERE id = 507")
        r["pm index"], r["pm probe"] = _pm_probe(
            dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{pmmd}') {COLS}")
        yield r
    finally:
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(dl, expect):
    if dl is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the delete fixture is missing")
    return True


# ---- position deletes ------------------------------------------------------------

def test_iceberg_deletes_the_data_file_has_five_rows(dl, expect):
    if dl is None:
        expect.text("missing", "present", "delete fixture is missing")
        return
    expect.require_module("json", name="python3 is needed")
    expect.text(str(dl["data rows"]), "5", "the data file has 5 rows before deletes")


def test_position_deletes_drop_the_listed_rows(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["apply rows"]), dl["oracle"], "position deletes drop the listed rows (surviving == oracle)")


def test_position_deleted_ids_are_gone(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["apply gone"]), "0", "the deleted ids (2, 4) are gone")


def test_position_deletes_remove_exactly_two_rows(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["apply count"]), "3", "exactly the two deleted rows were removed (5 - 2 = 3)")


def test_position_deletes_apply_under_projection(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["apply projected"]), "1,3,5",
                "position deletes apply under column projection (id only -> 1,3,5)")


def test_a_position_delete_older_than_the_data_does_not_apply(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["noapply count"]), "5", "a delete older than the data (lower seq) does not apply (5 rows)")


def test_older_position_delete_leaves_its_ids(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["noapply kept"]), "2",
                "the would-be-deleted ids survive when the delete is older than the data")


def test_a_position_delete_inherits_a_null_sequence_number(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["inherit count"]), "3",
                "a position delete with an inherited (null-entry) sequence number applies")


def test_a_position_delete_naming_another_file_deletes_nothing(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["wrongpath count"]), "5",
                "a position delete naming a different data file deletes nothing (5 rows)")


def test_a_null_pos_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["nullpos"]), "XX001", "a position delete with a null pos is refused (XX001, not silently dropped)")


def test_the_null_pos_refusal_names_pos(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["nullpos names"]), "1", "...and the null-pos refusal names pos")


def test_the_backend_survives_the_null_pos_refusal(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["up after nullpos"]), "1", "backend still up after the null-pos refusal")


def test_a_negative_pos_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["negpos"]), "XX001", "a position delete with a negative pos is refused (XX001, not cast inert)")


def test_the_negative_pos_refusal_names_it(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["negpos names"]), "1", "...and the negative-pos refusal names the negative pos")


def test_the_backend_survives_the_negative_pos_refusal(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["up after negpos"]), "1", "backend still up after the negative-pos refusal")


def test_a_position_delete_of_ordinal_zero_deletes_the_first_row(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["zeropos"]), "2,3,4,5",
                "a position delete of ordinal 0 deletes the first row (id 1 gone, 4 remain)")


# ---- equality deletes --------------------------------------------------------------

def test_an_equality_delete_drops_matching_rows(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqapply"]), dl["eq oracle"]["eqapply"], "equality delete drops matching rows (survivors == oracle)")


def test_an_equality_delete_at_an_equal_sequence_does_not_apply(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqboundary"]), dl["eq oracle"]["eqboundary"],
                "an equality delete at an equal sequence number does not apply")


def test_multi_column_equality_is_an_and(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqmulti"]), dl["eq oracle"]["eqmulti"], "multi-column equality matches all columns (AND, not OR)")


def test_columns_beyond_equality_ids_are_ignored(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqextra"]), dl["eq oracle"]["eqextra"], "columns beyond equality_ids are ignored for matching")


def test_a_null_equality_value_matches_only_nulls(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqnull"]), dl["eq oracle"]["eqnull"], "a null delete value matches only null data values")


def test_the_null_region_row_is_gone(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqnull nulls"]), "0", "the null-region row is gone, the non-null rows remain")


def test_multiple_equality_delete_files_apply_their_union(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqtwo"]), dl["eq oracle"]["eqtwo"], "multiple equality-delete files apply their union")


def test_position_and_equality_deletes_combine(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqmixed"]), dl["eq oracle"]["eqmixed"], "position and equality deletes combine")


def test_equality_deletes_apply_to_an_unprojected_column(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eq unprojected"]), "10,30,50",
                "equality deletes apply when the delete column is not projected")


def test_the_backend_survives_the_equality_applications(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["up after eq applications"]), "1", "backend still up after the equality applications")


# ---- equality refusals and partition scope ----------------------------------------

def test_an_equality_delete_without_equality_ids_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqnoids"]), "XX001", "an equality delete without equality_ids is refused (XX001)")


def test_a_never_applicable_equality_delete_without_ids_is_skipped(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["equality count"]), "5",
                "a never-applicable equality delete without equality_ids is skipped (5 rows)")


def test_a_partition_scoped_equality_delete_stays_in_its_partition(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqpart_apply"]), dl["part oracle"]["eqpart_apply"],
                "a partition-scoped equality delete applies only to its partition")


def test_the_us_partition_survives_an_eu_scoped_delete(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqpart_apply us"]), "3", "the us-partition rows survive an eu-scoped delete")


def test_a_delete_scoped_to_an_empty_partition_deletes_nothing(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqpart_nomatch"]), "5", "a delete scoped to an empty partition deletes nothing (5 rows)")


def test_a_data_file_without_partition_spec_id_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqpart_datanospec"]), "XX001",
                "a data file lacking partition_spec_id is refused under a partitioned delete (XX001)")


def test_a_cross_spec_partition_delete_applies_to_nothing(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqpart_crossspec"]), "5", "a cross-spec partition-scoped delete applies to nothing (5 rows)")


def test_an_uncomparable_partition_value_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqpart_incomparable"]), "0A000",
                "a partition-scoped delete with an uncomparable partition value is refused (0A000)")


def test_an_equality_delete_on_an_unsupported_type_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqtype"]), "0A000", "an equality delete on an unsupported column type is refused (0A000)")


def test_the_unsupported_type_refusal_names_the_type(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqtype names"]), "1", "...and the refusal names the type")


def test_an_equality_column_missing_from_the_data_file_errors(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqmissing"]), "22023", "an equality column missing from the data file errors (22023)")


def test_an_equality_delete_path_outside_the_table_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqescape"]), "22023", "an equality-delete path outside the table location is refused (22023)")


def test_the_boundary_refusal_names_the_escape(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqescape names"]), "1", "...and the boundary refusal names the escape")


def test_a_dotdot_escaping_delete_path_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqdotdot"]), "22023", "a dotdot-escaping delete path is refused (22023)")


def test_the_backend_survives_the_equality_refusals(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["up after eq refusals"]), "1", "backend still up after the equality refusals")


# ---- audit arms --------------------------------------------------------------------

def test_an_equality_id_beyond_int32_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqbigid"]), "XX001", "an equality_ids value beyond int32 refuses the manifest (XX001)")


def test_a_manifest_list_without_partition_spec_id_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqnospec"]), "XX001",
                "a manifest list without partition_spec_id is refused for equality deletes (XX001)")


def test_a_partition_spec_without_fields_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqnofields"]), "XX001", "a partition spec without its fields key is refused (XX001)")


def test_a_never_applicable_equality_delete_is_not_validated(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["eqstaletype count"]), "5",
                "a never-applicable equality delete is skipped, not validated (5 rows)")


def test_a_v1_manifest_without_sequence_numbers_reads(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["v1seq count"]), "5", "a v1 manifest without a sequence_number column reads (default 0, 5 rows)")


def test_the_backend_survives_the_delete_audit_arms(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["up after audit"]), "1", "backend still up after the audit arms")


def test_an_existing_entry_with_a_null_sequence_number_is_refused(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["badseq"]), "XX001", "an EXISTING entry with a null sequence number is refused (XX001)")


def test_the_backend_survives_the_bad_sequence_refusal(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["up after badseq"]), "1", "backend still up after the bad-sequence refusal")


# ---- #1868: many data files, many position-delete rows ------------------------------

def test_the_many_file_position_delete_table_has_its_rows(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["pm data rows"]), "256 rows in 32 files",
                "premise: the many-file position-delete table holds 256 rows in 32 data files before deletes")


def test_position_deletes_over_many_files_match_the_oracle(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["pm md5"]), dl["pm oracle md5"],
                "position deletes over 32 data files drop exactly the oracle's rows (survivors == oracle, #1868)")


def test_ordinal_zero_in_the_other_spelling_is_dropped_from_every_file(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["pm ordinal 0"]), "0",
                "ordinal 0, named in the other path spelling (file:// vs bare), is dropped from all 32 data files (#1868)")


def test_an_s3_spelled_delete_row_does_not_apply_to_a_local_file(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["pm s3 row"]), "1",
                "a delete row naming a data file under s3:// does not apply to the local file (#1868)")


def test_each_data_file_visits_only_its_own_position_deletes(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["pm probe"]), dl["pm oracle probe"],
                "each data file visits only the position-delete rows naming it, not every row (#1868)")


def test_the_position_delete_index_is_built_once_per_scan(dl, expect):
    if _declined(dl, expect):
        return
    expect.text(str(dl["pm index"]), dl["pm oracle index"],
                "the position-delete index is built once per scan, one group per normalized path (#1868)")

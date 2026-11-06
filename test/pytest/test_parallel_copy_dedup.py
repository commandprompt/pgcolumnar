"""pgcolumnar.parallel_copy can refuse a load it has already done (#403 item 7). The
pytest twin of `test/parallel_copy_dedup.sh`, every check by the shell's name (#1341).

Asserted: without dedup a repeated load doubles the table; with dedup the second load
of the same file stores nothing, returns 0 and says so in a NOTICE; a file changed at
the same path loads; the record is per table; dedup off loads again; the table's
records are exactly the SHA-256 digests of the two files it loaded; and no prepared
transaction is left behind.

THREE FIXES THE SHELL SHARES. The changed file now differs from the first ONLY in its
last row, and that row lies past the coordinator's first 256 KiB read, so a fingerprint
of the first read alone refuses it; the records are compared with the server's own
sha256() of each file. The skip's NOTICE is read, not only the 0. And the two control
checks lose their "premise:" prefix under #1339: dedup is pgcolumnar's own default, and
forcing it on reddens them.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture. The files live
in `server_dir`, because the backend writes and reads them.
"""
import shutil

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_parallel_copy_dedup"
ROWS = 25000
FIRST_READ = 262144  # PCOPY_FINGERPRINT_BUFSZ, the coordinator's read size
SKIP_NOTICE = 'pgcolumnar.parallel_copy skipped a load already applied to "pcd"'


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _err(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _int(v):
    return int(v) if isinstance(v, str) and v.lstrip("-").isdigit() else v


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0]}")

    def q(self, sql):
        """-> the first column as text, "" for NULL, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return _err(exc)
        return "" if row is None or row[0] is None else str(row[0])

    def copy_noticed(self, sql):
        """-> (the first column as text, the number of skip NOTICEs), or two query_error()s."""
        notes = []
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.add_notice_handler(lambda d: notes.append((d.severity_nonlocalized or d.severity,
                                                                d.message_primary or "")))
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return _err(exc), _err(exc)
        got = "" if row is None or row[0] is None else str(row[0])
        return got, str(sum(1 for sev, msg in notes if sev == "NOTICE" and msg == SKIP_NOTICE))

    def mk(self, table):
        self.run(f"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} (id int, txt text) USING pgcolumnar")


@pytest.fixture(scope="module")
def pcd(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        f = server_dir / "pcd_load.txt"
        g = server_dir / "pcd_other.txt"
        d.run(f"COPY (SELECT g AS id, 'v'||g AS txt FROM generate_series(1,{ROWS}) g) "
              f"TO '{f}' WITH (FORMAT text)")
        # differs from f ONLY in its last row, past the first read
        d.run(f"COPY (SELECT g AS id, CASE WHEN g = {ROWS} THEN 'changed' ELSE 'v'||g END AS txt "
              f"FROM generate_series(1,{ROWS}) g) TO '{g}' WITH (FORMAT text)")
        sha = "SELECT encode(sha256(pg_read_binary_file('{p}')), 'hex')"
        r = {"sha f": d.q(sha.format(p=f)), "sha g": d.q(sha.format(p=g))}
        r["past first read"] = d.q(f"SELECT ((SELECT size FROM pg_stat_file('{g}')) "
                                   f"- octet_length('{ROWS}'||E'\\tchanged\\n') >= {FIRST_READ})::int")

        load = "SELECT pgcolumnar.parallel_copy('{t}', '{p}', 4{dd})"
        d.mk("pcd_plain")
        r["r1"] = d.q(load.format(t="pcd_plain", p=f, dd=""))
        r["r2"] = d.q(load.format(t="pcd_plain", p=f, dd=""))
        r["plain count"] = d.q("SELECT count(*) FROM pcd_plain")

        d.mk("pcd")
        r["d1"] = d.q(load.format(t="pcd", p=f, dd=", true"))
        r["c1"] = d.q("SELECT count(*) FROM pcd")
        r["d2"], r["skip notices"] = d.copy_noticed(load.format(t="pcd", p=f, dd=", true"))
        r["c2"] = d.q("SELECT count(*) FROM pcd")

        shutil.copyfile(g, f)
        r["d3"] = d.q(load.format(t="pcd", p=f, dd=", true"))
        r["c3"] = d.q("SELECT count(*) FROM pcd")

        d.mk("pcd_other")
        r["d4"] = d.q(load.format(t="pcd_other", p=f, dd=", true"))
        r["other count 1"] = d.q("SELECT count(*) FROM pcd_other")
        r["d5"] = d.q(load.format(t="pcd_other", p=f, dd=", false"))
        r["other count 2"] = d.q("SELECT count(*) FROM pcd_other")

        r["records"] = d.q("SELECT count(*) FROM pgcolumnar.load_fingerprint "
                           "WHERE relation_oid = 'pcd'::regclass")
        r["digests"] = d.q("SELECT string_agg(encode(fingerprint, 'hex'), ',' ORDER BY loaded_at) "
                           "FROM pgcolumnar.load_fingerprint WHERE relation_oid = 'pcd'::regclass")
        # this database's own: other suites in the session prepare transactions too
        r["prepared"] = d.q("SELECT count(*) FROM pg_prepared_xacts WHERE database = current_database()")
        print(f"-- dedup on: load 1 returned {r['d1']} (table {r['c1']}), "
              f"load 2 returned {r['d2']} (table {r['c2']}); changed file: {r['d3']} (table {r['c3']})")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_pcdedup_the_changed_row_lies_past_the_first_read(pcd, expect):
    expect.num(_int(pcd["past first read"]), 1, "premise: the changed row lies past the first 256 KiB of the file")


# Not premises (#1339): dedup is pgcolumnar's own default, and forcing it on reddens both.
def test_pcdedup_without_dedup_a_repeated_load_doubles_the_table(pcd, expect):
    expect.num(_int(pcd["plain count"]), ROWS * 2,
               "without dedup a repeated load doubles the table, which is the defect")


def test_pcdedup_each_plain_load_reported_its_rows(pcd, expect):
    r1, r2 = pcd["r1"], pcd["r2"]
    got = r1 if _failed(r1) else r2 if _failed(r2) else f"{r1}/{r2}"
    expect.text(got, f"{ROWS}/{ROWS}", "and each of those loads reported its rows")


def test_pcdedup_the_first_load_stores_its_rows(pcd, expect):
    expect.num(_int(pcd["c1"]), ROWS, "the first load stores its rows normally (#403 item 7)")


def test_pcdedup_the_first_load_reports_them(pcd, expect):
    expect.num(_int(pcd["d1"]), ROWS, "and reports them")


def test_pcdedup_the_second_load_of_the_same_file_stores_nothing(pcd, expect):
    expect.num(_int(pcd["c2"]), ROWS, "the second load of the same file stores nothing (#403 item 7)")


def test_pcdedup_the_second_load_reports_0(pcd, expect):
    expect.num(_int(pcd["d2"]), 0, "and reports 0 rather than claiming it loaded them")


def test_pcdedup_the_skip_says_so_in_a_notice(pcd, expect):
    expect.num(_int(pcd["skip notices"]), 1,
               "and says in a NOTICE that it skipped the load, rather than returning a silent 0")


def test_pcdedup_a_changed_file_at_the_same_path_is_loaded(pcd, expect):
    expect.num(_int(pcd["c3"]), ROWS * 2, "a changed file at the same path is loaded, not skipped (#403 item 7)")


def test_pcdedup_the_changed_file_reports_its_rows(pcd, expect):
    expect.num(_int(pcd["d3"]), ROWS, "and reports the rows it loaded")


def test_pcdedup_the_same_file_loads_into_a_different_table(pcd, expect):
    expect.num(_int(pcd["other count 1"]), ROWS,
               "the same file loads into a DIFFERENT table (the record is per table)")


def test_pcdedup_the_other_table_load_reports_its_rows(pcd, expect):
    expect.num(_int(pcd["d4"]), ROWS, "and reports its rows")


def test_pcdedup_dedup_off_loads_again(pcd, expect):
    expect.num(_int(pcd["other count 2"]), ROWS * 2,
               "asking for no dedup loads again even though the file is on record")


def test_pcdedup_dedup_off_reports_those_rows(pcd, expect):
    expect.num(_int(pcd["d5"]), ROWS, "and reports those rows too")


def test_pcdedup_the_skipped_load_left_no_record(pcd, expect):
    expect.num(_int(pcd["records"]), 2, "the skipped load left exactly one record for that table and file")


def test_pcdedup_the_records_hold_each_files_sha256(pcd, expect):
    sf, sg = pcd["sha f"], pcd["sha g"]
    want = sf if _failed(sf) else sg if _failed(sg) else f"{sf},{sg}"
    expect.text(pcd["digests"], want, "the records hold the SHA-256 of each loaded file's bytes")


def test_pcdedup_no_prepared_transaction_leaked(pcd, expect):
    expect.num(_int(pcd["prepared"]), 0, "no prepared transaction leaked through any of it")

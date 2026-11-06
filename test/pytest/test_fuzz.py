"""Randomized differential runs, heap as the oracle. The pytest twin of `test/fuzz.sh`,
every check by the shell's name.

Each iteration draws a row count, a null density, chunk-group and stripe limits, a codec
and level, and filter bounds; loads a heap table from the generator and the columnar
table from the heap; and compares a battery of queries by the shell's order-blind set
hash. The draws come from the shell's own 31-bit LCG, transcribed here, so one seed is
one run in both harnesses (bash's $RANDOM changed algorithm in 5.1 and could not be
transcribed at all).

The check names are the property, asserted once over the whole run; a failure names the
iterations that broke it. Two #1341 arms: the run must cover every iteration, and every
iteration's storage options must read back as set -- set_options' result was thrown
away, so a call that failed or ignored an argument left the iteration on default
storage and nothing said so.

PGC_SEED and PGC_ITERS are the shell's knobs, with its defaults.
"""
import os

import psycopg
import pytest

from pgc_vacuity import query_error

SEED = int(os.environ.get("PGC_SEED", "12345"))
ITERS = int(os.environ.get("PGC_ITERS", "25"))
DB = "pgc_fuzz"
CODECS = ["none", "pglz", "lz4", "zstd"]
DEFS = ("id int, c_int int, c_num numeric(20,4), c_f8 double precision, c_bool boolean, "
        "c_date date, c_ts timestamp, c_vc varchar(40), c_text text")
KINDS = {
    "whole-row": "SELECT * FROM %T",
    "count": "SELECT count(*), count(c_int), count(c_text) FROM %T",
    "minmax": "SELECT min(c_int), max(c_int), min(c_vc), max(c_vc), min(c_date), max(c_date) FROM %T",
    "sumavg": "SELECT sum(c_int), avg(c_num), sum(c_f8) FROM %T",
    "range-int": "SELECT id FROM %T WHERE c_int BETWEEN {lo} AND {hi}",
    "range-date": "SELECT id FROM %T WHERE c_date >= DATE '2010-06-01'",
    "eq": "SELECT id, c_vc FROM %T WHERE id = {eq}",
    "isnull": "SELECT count(*) FROM %T WHERE c_text IS NULL",
    "compound": "SELECT id FROM %T WHERE c_int > 0 AND c_bool AND c_num < 1000",
}


class _Lcg:
    """The shell's rnd/pick: S = (S * 1103515245 + 12345) mod 2^31, draw (S >> 16) mod n."""

    def __init__(self, seed):
        self.s = seed & 0x7FFFFFFF

    def rnd(self, n):
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return (self.s >> 16) % n

    def pick(self, items):
        return items[self.rnd(len(items))]


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else str(row[0])


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _set_hash(dsn, query):
    """The shell's pgc_set_hash: md5 of the rows' text, sorted; EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


@pytest.fixture(scope="module")
def fz(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        g = _Lcg(SEED)
        bad = {k: [] for k in KINDS}
        bad_rows, bad_opts, its = [], [], 0
        print(f"-- fuzz seed={SEED} iters={ITERS}  (reproduce a failure with PGC_SEED={SEED})")
        for it in range(1, ITERS + 1):
            n = 1 + g.rnd(5000)
            nullmod = 2 + g.rnd(18)
            cg = g.pick([100, 250, 500, 1000])
            sr = g.pick([1000, 2000, 5000])
            codec = g.pick(CODECS)
            lvl = 1 + g.rnd(19)
            span = n if n > 1 else 2
            lo = g.rnd(span)
            hi = lo + 1 + g.rnd(500)
            eq = 1 + g.rnd(span)
            tag = f"it={it} N={n} nullmod={nullmod} cg={cg} sr={sr} codec={codec} lvl={lvl}"
            print(f"-- {tag}")

            _run(dsn, "DROP TABLE IF EXISTS t_heap; DROP TABLE IF EXISTS t_col")
            _run(dsn, f"CREATE TABLE t_heap ({DEFS}) USING heap")
            _run(dsn, f"CREATE TABLE t_col ({DEFS}) USING pgcolumnar")
            _run(dsn, f"SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => {cg}, "
                      f"stripe_row_limit => {sr}, compression => '{codec}', compression_level => {lvl})")
            got = _value(dsn, "SELECT chunk_group_row_limit || '/' || stripe_row_limit || '/' || compression "
                              "|| '/' || compression_level FROM pgcolumnar.options WHERE regclass = 't_col'::regclass")
            if got != f"{cg}/{sr}/{codec}/{lvl}":
                bad_opts.append(f"[{tag}: read back '{got}']")
            # load_pair: generated once into heap, then copied to columnar
            _run(dsn, f"""INSERT INTO t_heap SELECT
                g,
                CASE WHEN g%{nullmod}=0 THEN NULL ELSE (g*3 - 500) END,
                CASE WHEN g%{nullmod + 1}=0 THEN NULL ELSE (g::numeric*0.5 - 100) END,
                CASE WHEN g%{nullmod}=0 THEN NULL ELSE (sqrt(g::float8) * (CASE WHEN g%2=0 THEN -1 ELSE 1 END)) END,
                CASE WHEN g%{nullmod}=0 THEN NULL ELSE (g%2=0) END,
                CASE WHEN g%{nullmod}=0 THEN NULL ELSE (DATE '2010-01-01' + g) END,
                CASE WHEN g%{nullmod}=0 THEN NULL ELSE (TIMESTAMP '2010-01-01' + make_interval(mins => g)) END,
                CASE WHEN g%{nullmod}=0 THEN NULL ELSE ('v'||lpad(g::text,6,'0')) END,
                CASE WHEN g%{nullmod}=0 THEN NULL WHEN g%997=0 THEN repeat('q',3000) ELSE ('t'||g) END
                FROM generate_series(1,{n}) g""")
            _run(dsn, "INSERT INTO t_col SELECT * FROM t_heap")

            rows = _value(dsn, "SELECT count(*) FROM t_col")
            if rows != str(n):
                bad_rows.append(f"[{tag}: {rows} rows]")
            for kind, tmpl in KINDS.items():
                q = tmpl.format(lo=lo, hi=hi, eq=eq)
                hq, hc = _set_hash(dsn, q.replace("%T", "t_heap")), _set_hash(dsn, q.replace("%T", "t_col"))
                if hc != hq:
                    bad[kind].append(f"[{tag}: {hc} vs {hq}]")
            its += 1
        yield {"its": its, "opts": " ".join(bad_opts) or "none", "rows": " ".join(bad_rows) or "none",
               **{k: " ".join(v) or "none" for k, v in bad.items()}}
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_fuzz_run_covered_every_iteration(fz, expect):
    expect.num(fz["its"], ITERS, "the run covered every iteration")


def test_every_fuzz_iterations_storage_options_read_back(fz, expect):
    expect.text(fz["opts"], "none", "every iteration's storage options read back as set")


def test_every_fuzz_iteration_loaded_all_its_rows(fz, expect):
    expect.text(fz["rows"], "none", "every iteration loaded all its rows")


def test_every_fuzz_iterations_whole_rows_agree(fz, expect):
    expect.text(fz["whole-row"], "none", "every iteration's whole rows agree with heap")


def test_every_fuzz_iterations_counts_agree(fz, expect):
    expect.text(fz["count"], "none", "every iteration's counts agree with heap")


def test_every_fuzz_iterations_min_and_max_agree(fz, expect):
    expect.text(fz["minmax"], "none", "every iteration's min and max agree with heap")


def test_every_fuzz_iterations_sums_and_averages_agree(fz, expect):
    expect.text(fz["sumavg"], "none", "every iteration's sums and averages agree with heap")


def test_every_fuzz_iterations_integer_range_agrees(fz, expect):
    expect.text(fz["range-int"], "none", "every iteration's integer range agrees with heap")


def test_every_fuzz_iterations_date_range_agrees(fz, expect):
    expect.text(fz["range-date"], "none", "every iteration's date range agrees with heap")


def test_every_fuzz_iterations_equality_lookup_agrees(fz, expect):
    expect.text(fz["eq"], "none", "every iteration's equality lookup agrees with heap")


def test_every_fuzz_iterations_is_null_count_agrees(fz, expect):
    expect.text(fz["isnull"], "none", "every iteration's IS NULL count agrees with heap")


def test_every_fuzz_iterations_compound_filter_agrees(fz, expect):
    expect.text(fz["compound"], "none", "every iteration's compound filter agrees with heap")

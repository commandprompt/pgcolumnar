"""Statistics in the Parquet files pgcolumnar.export_parquet() writes (#850). The pytest
twin of `test/parquet_export_stats.sh`, every check by the shell's name (#1341).

A 196609-row table (four row groups, the last holding one row) is exported, and
`test/parquet_stats.py` reads the footer with no Parquet library. Asserted: every
column chunk of an INT32, INT64, FLOAT or DOUBLE column that holds a value carries both
bounds, at the physical width, equal per row group to the heap's; null_count is written
always and counts the date and timestamp infinities the export folds to null; the float
rules of parquet.thrift's TYPE_ORDER hold on a small NaN fixture; FileMetaData carries
one TYPE_ORDER ColumnOrder per leaf; and the Parquet FDW skips row groups on the
exported file, refuses the documented cross-type and float cases, and returns exactly
what read_parquet returns for every predicate.

THE ORACLE FOR A BOUND OR A NULL COUNT IS THE HEAP, es_h, built from the generator, not
the exported file. A file-derived oracle agrees with any fold the export applies to its
data and its statistics alike: with 2000-01-01 folded to null, the shell's old form
passed all 162 checks. A "no chunk carries X" reading carries the number of chunks it
examined, so a leaf that did not resolve reads "0 of 0 chunks" rather than 0.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). The exported files live
in `server_dir`, because the backend writes them. A failed query, or a footer the
parser could not read, is a `query_error()`.
"""
import pathlib
import re
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_parquet_export_stats"
STATS_PY = pathlib.Path(__file__).resolve().parent.parent / "parquet_stats.py"
ROWS, RGSIZE, NGROUPS = 196609, 65536, 4
COLS = ("id int4, i2 int2, i8 int8, f4 float4, d date, ts timestamp, "
        "nul int4, alln int4, t text, b bool")
ALL_COLS = "id,i2,i8,f4,d,ts,nul,alln,t,b"
SET_HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
            "FROM (SELECT _row::text AS t FROM ( {q} ) _row) _s")

BOUNDS = [
    ("id", 0, "min", "group 0 min(id) equals the oracle"),
    ("id", 0, "max", "group 0 max(id) equals the oracle"),
    ("id", 1, "min", "group 1 min(id) equals the oracle"),
    ("id", 1, "max", "group 1 max(id) equals the oracle"),
    ("id", 2, "min", "group 2 min(id) equals the oracle"),
    ("id", 2, "max", "group 2 max(id) equals the oracle"),
    ("id", 3, "min", "group 3 min(id) equals the oracle"),
    ("id", 3, "max", "group 3 max(id) equals the oracle"),
    ("i2", 0, "min", "group 0 min(i2) equals the oracle"),
    ("i2", 0, "max", "group 0 max(i2) equals the oracle"),
    ("i2", 1, "min", "group 1 min(i2) equals the oracle"),
    ("i2", 1, "max", "group 1 max(i2) equals the oracle"),
    ("i2", 2, "min", "group 2 min(i2) equals the oracle"),
    ("i2", 2, "max", "group 2 max(i2) equals the oracle"),
    ("i2", 3, "min", "group 3 min(i2) equals the oracle"),
    ("i2", 3, "max", "group 3 max(i2) equals the oracle"),
    ("i8", 0, "min", "group 0 min(i8) equals the oracle"),
    ("i8", 0, "max", "group 0 max(i8) equals the oracle"),
    ("i8", 1, "min", "group 1 min(i8) equals the oracle"),
    ("i8", 1, "max", "group 1 max(i8) equals the oracle"),
    ("i8", 2, "min", "group 2 min(i8) equals the oracle"),
    ("i8", 2, "max", "group 2 max(i8) equals the oracle"),
    ("i8", 3, "min", "group 3 min(i8) equals the oracle"),
    ("i8", 3, "max", "group 3 max(i8) equals the oracle"),
    ("f4", 0, "min", "group 0 min(f4) equals the oracle"),
    ("f4", 0, "max", "group 0 max(f4) equals the oracle"),
    ("f4", 1, "min", "group 1 min(f4) equals the oracle"),
    ("f4", 1, "max", "group 1 max(f4) equals the oracle"),
    ("f4", 2, "min", "group 2 min(f4) equals the oracle"),
    ("f4", 2, "max", "group 2 max(f4) equals the oracle"),
    ("f4", 3, "min", "group 3 min(f4) equals the oracle"),
    ("f4", 3, "max", "group 3 max(f4) equals the oracle"),
    ("d", 0, "min", "group 0 min(d) equals the oracle"),
    ("d", 0, "max", "group 0 max(d) equals the oracle"),
    ("d", 1, "min", "group 1 min(d) equals the oracle"),
    ("d", 1, "max", "group 1 max(d) equals the oracle"),
    ("d", 2, "min", "group 2 min(d) equals the oracle"),
    ("d", 2, "max", "group 2 max(d) equals the oracle"),
    ("d", 3, "min", "group 3 min(d) equals the oracle"),
    ("d", 3, "max", "group 3 max(d) equals the oracle"),
    ("ts", 0, "min", "group 0 min(ts) equals the oracle"),
    ("ts", 0, "max", "group 0 max(ts) equals the oracle"),
    ("ts", 1, "min", "group 1 min(ts) equals the oracle"),
    ("ts", 1, "max", "group 1 max(ts) equals the oracle"),
    ("ts", 2, "min", "group 2 min(ts) equals the oracle"),
    ("ts", 2, "max", "group 2 max(ts) equals the oracle"),
    ("ts", 3, "min", "group 3 min(ts) equals the oracle"),
    ("ts", 3, "max", "group 3 max(ts) equals the oracle"),
    ("nul", 0, "min", "group 0 min(nul) equals the oracle"),
    ("nul", 0, "max", "group 0 max(nul) equals the oracle"),
    ("nul", 1, "min", "group 1 min(nul) equals the oracle"),
    ("nul", 1, "max", "group 1 max(nul) equals the oracle"),
    ("nul", 2, "min", "group 2 min(nul) equals the oracle"),
    ("nul", 2, "max", "group 2 max(nul) equals the oracle"),
    ("nul", 3, "min", "group 3 min(nul) equals the oracle"),
    ("nul", 3, "max", "group 3 max(nul) equals the oracle"),
]

NULLS = [
    ("nul", 0, "group 0 null_count(nul) equals the oracle"),
    ("nul", 1, "group 1 null_count(nul) equals the oracle"),
    ("nul", 2, "group 2 null_count(nul) equals the oracle"),
    ("nul", 3, "group 3 null_count(nul) equals the oracle"),
    ("d", 0, "group 0 null_count(d) equals the oracle"),
    ("d", 1, "group 1 null_count(d) equals the oracle"),
    ("d", 2, "group 2 null_count(d) equals the oracle"),
    ("d", 3, "group 3 null_count(d) equals the oracle"),
    ("ts", 0, "group 0 null_count(ts) equals the oracle"),
    ("ts", 1, "group 1 null_count(ts) equals the oracle"),
    ("ts", 2, "group 2 null_count(ts) equals the oracle"),
    ("ts", 3, "group 3 null_count(ts) equals the oracle"),
    ("alln", 0, "group 0 null_count(alln) equals the oracle"),
    ("alln", 1, "group 1 null_count(alln) equals the oracle"),
    ("alln", 2, "group 2 null_count(alln) equals the oracle"),
    ("alln", 3, "group 3 null_count(alln) equals the oracle"),
    ("id", 0, "group 0 null_count(id) equals the oracle"),
    ("id", 1, "group 1 null_count(id) equals the oracle"),
    ("id", 2, "group 2 null_count(id) equals the oracle"),
    ("id", 3, "group 3 null_count(id) equals the oracle"),
]

SOUND = [
    ("id BETWEEN 70000 AND 71000", "the FDW result equals the oracle for [id BETWEEN 70000 AND 71000]"),
    ("id < 5000", "the FDW result equals the oracle for [id < 5000]"),
    ("i2 = 777", "the FDW result equals the oracle for [i2 = 777]"),
    ("i8 < 1000000000::bigint", "the FDW result equals the oracle for [i8 < 1000000000::bigint]"),
    ("f4 < 1.0::real", "the FDW result equals the oracle for [f4 < 1.0::real]"),
    ("f4 >= 1.0::real", "the FDW result equals the oracle for [f4 >= 1.0::real]"),
    ("d < DATE '2000-01-05'", "the FDW result equals the oracle for [d < DATE '2000-01-05']"),
    ("d > DATE '2060-01-01'", "the FDW result equals the oracle for [d > DATE '2060-01-01']"),
    ("ts < TIMESTAMP '2000-01-01 00:30:00'", "the FDW result equals the oracle for [ts < TIMESTAMP '2000-01-01 00:30:00']"),
    ("nul < 1000100", "the FDW result equals the oracle for [nul < 1000100]"),
    ("i8 < '5'", "the FDW result equals the oracle for [i8 < '5']"),
    ("i8 < 5000000000", "the FDW result equals the oracle for [i8 < 5000000000]"),
    ("f4 < '0.4'", "the FDW result equals the oracle for [f4 < '0.4']"),
    ("i2 < '500'", "the FDW result equals the oracle for [i2 < '500']"),
    ("i2 < 500", "the FDW result equals the oracle for [i2 < 500]"),
    ("d < '2000-01-05'", "the FDW result equals the oracle for [d < '2000-01-05']"),
    ("d < TIMESTAMP '2000-01-05 00:00:00'", "the FDW result equals the oracle for [d < TIMESTAMP '2000-01-05 00:00:00']"),
    ("id < -1", "the FDW result equals the oracle for [id < -1]"),
    ("id < -5000000000", "the FDW result equals the oracle for [id < -5000000000]"),
    ("alln IS NULL", "the FDW result equals the oracle for [alln IS NULL]"),
    ("i8 < 5::int", "the FDW result equals the oracle for [i8 < 5::int]"),
    ("i8 < 5", "the FDW result equals the oracle for [i8 < 5]"),
    ("f4 < 0.4", "the FDW result equals the oracle for [f4 < 0.4]"),
]


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        """Run one statement and swallow its failure, as psql_run does: a failed step
        shows up in the readings after it."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")

    def q(self, sql):
        """-> the first column as psql -At prints it, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        if row is None or row[0] is None:
            return ""
        return ("t" if row[0] else "f") if isinstance(row[0], bool) else str(row[0])

    def lines(self, sql):
        """-> every first-column value as a list, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                return [str(r[0]) for r in conn.execute(sql).fetchall()]
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)

    def set_hash(self, sql):
        """pgc_set_hash: md5 over the sorted rendered rows, EMPTY for no rows."""
        return self.q(SET_HASH.format(q=sql))


class _Footer:
    """test/parquet_stats.py's reading of one file, or why there is none."""

    def __init__(self, path):
        self.file, self.chunks, self.error = {}, [], None
        try:
            out = subprocess.run([sys.executable, str(STATS_PY), str(path)],
                                 capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return
        if out.returncode != 0:
            tail = out.stderr.strip().splitlines()
            self.error = tail[-1] if tail else f"parser exit {out.returncode}"
            return
        for line in out.stdout.splitlines():
            tok = line.split()
            if not tok:
                continue
            kv = dict(t.split("=", 1) for t in tok[1:] if "=" in t)
            if tok[0] == "FILE" and not self.file:
                self.file = kv
            elif tok[0] == "CHUNK":
                self.chunks.append(kv)

    def failed(self):
        return query_error(f"the footer parser could not read the file: {self.error}")

    def fileval(self, key):
        return self.failed() if self.error else self.file.get(key, "")

    def leafof(self, path):
        for c in self.chunks:
            if c.get("path") == path:
                return c.get("leaf", "")
        return ""

    def cf(self, rg, leaf, key):
        if self.error:
            return self.failed()
        for c in self.chunks:
            if c.get("rg") == str(rg) and c.get("leaf") == leaf:
                return c.get(key, "")
        return ""

    def count(self, leaves, key, val):
        return sum(1 for c in self.chunks if c.get("leaf") in leaves and c.get(key) == val)

    def none_of(self, leaves, key, val):
        """-> "N of M chunks": M is how many chunks the leaf set selected."""
        if self.error:
            return self.failed()
        sel = sum(1 for c in self.chunks if c.get("leaf") in leaves)
        return f"{self.count(leaves, key, val)} of {sel} chunks"


def _num(v):
    """-> v as a number, or v unchanged so expect.num refuses it by name."""
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _g9(got, want):
    """The shell's as_float: both sides as %.9g, or a query_error() when either is no number."""
    try:
        return "%.9g" % float(got), "%.9g" % float(want)
    except (TypeError, ValueError):
        return query_error(f"not a measurement: got [{got}] want [{want}]"), "%.9g" % 0


def _is_float_text(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _explain_val(db, label, where):
    """The first integer on the EXPLAIN ANALYZE line carrying `label:`."""
    out = db.lines("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                   f"SELECT count(*) FROM es_ft {where}")
    if isinstance(out, str):
        return out
    for line in out:
        if f"{label}:" in line:
            m = re.search(r"\d+", line)
            if m:
                return int(m.group(0))
    return query_error(f"no '{label}:' line in the plan")


def _filter_has(db, where, pattern):
    out = db.lines(f"EXPLAIN (VERBOSE, COSTS OFF) SELECT count(*) FROM es_ft {where}")
    if isinstance(out, str):
        return out
    return sum(1 for line in out if "filter" in line.lower() and pattern in line)


def _ora(db, col, g, side):
    """The bound the HEAP says group g must carry, in physical space."""
    w = f"id / {RGSIZE} = {g}"
    if col == "d":
        return db.q(f"SELECT ({side}(d) FILTER (WHERE isfinite(d)) - DATE '1970-01-01')::text "
                    f"FROM es_h WHERE {w}")
    if col == "ts":
        return db.q(f"SELECT (EXTRACT(EPOCH FROM ({side}(ts) FILTER (WHERE isfinite(ts)) "
                    f"- TIMESTAMP '1970-01-01')) * 1000000)::bigint::text FROM es_h WHERE {w}")
    if col == "f4":
        return db.q(f"SELECT {side}(f4)::float8::text FROM es_h WHERE {w}")
    return db.q(f"SELECT {side}({col})::text FROM es_h WHERE {w}")


def _nulls_of(db, col, g):
    """The nulls group g must count, a folded date or timestamp infinity included."""
    if col in ("d", "ts"):
        return db.q(f"SELECT count(*) FILTER (WHERE {col} IS NULL OR NOT isfinite({col})) "
                    f"FROM es_h WHERE id / {RGSIZE} = {g}")
    return db.q(f"SELECT count(*) - count({col}) FROM es_h WHERE id / {RGSIZE} = {g}")


def _width(ft):
    """"bad of n bounds": every INT32/FLOAT bound 4 bytes, every INT64/DOUBLE 8."""
    if ft.error:
        return ft.failed()
    bad = n = 0
    for c in ft.chunks:
        w = {"1": 4, "4": 4, "2": 8, "5": 8}.get(c.get("ptype"), 0)
        if not w:
            continue
        for k in ("minlen", "maxlen"):
            v = c.get(k, "")
            if v != "-":
                n += 1
                if not (v.isdigit() and int(v) == w):
                    bad += 1
    return f"{bad} of {n} bounds"


def _min_le_max(ft):
    if ft.error:
        return ft.failed()
    bad = n = 0
    for c in ft.chunks:
        mn, mx = c.get("min", ""), c.get("max", "")
        if mn not in ("-", "") and mx not in ("-", ""):
            n += 1
            if _is_float_text(mn) and _is_float_text(mx) and float(mn) > float(mx):
                bad += 1
    return f"{bad} of {n} chunks"


def _sentinels(ft, lds, d_range, ts_range):
    """Date and timestamp bounds outside the heap's finite range, in physical space."""
    if ft.error:
        return ft.failed()
    rng = {}
    for leaf, text in zip(lds, (d_range, ts_range)):
        parts = text.split() if isinstance(text, str) and not text.startswith(QUERY_ERROR) else []
        if len(parts) != 2:
            return query_error(f"no heap range: [{text}]")
        rng[leaf] = (int(parts[0]), int(parts[1]))
    bad = n = 0
    for c in ft.chunks:
        if c.get("leaf") not in rng:
            continue
        lo, hi = rng[c["leaf"]]
        for k in ("min", "max"):
            v = c.get(k, "-")
            if v == "-":
                continue
            n += 1
            if not re.fullmatch(r"-?\d+", v) or not lo <= int(v) <= hi:
                bad += 1
    return f"{bad} of {n} bounds"


@pytest.fixture(scope="module")
def es(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        db = _Db(pgc_cluster.dsn(dbname=DB))
        parq, nanq = server_dir / "export_stats.parquet", server_dir / "nan.parquet"
        r = {}
        db.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        db.run(f"CREATE TABLE es_h ({COLS})")
        db.run(f"""INSERT INTO es_h
                   SELECT g - 1,
                          (g % 20000) + 1,
                          g::bigint * 1000000,
                          g * 0.5,
                          CASE WHEN g % {RGSIZE} = 3 THEN 'infinity'::date
                               WHEN g % {RGSIZE} = 4 THEN '-infinity'::date
                               ELSE DATE '2000-01-01' + (g / 8) END,
                          CASE WHEN g % {RGSIZE} = 5 THEN 'infinity'::timestamp
                               ELSE TIMESTAMP '2000-01-01' + (g || ' sec')::interval END,
                          CASE WHEN g % 5 = 0 THEN NULL ELSE g + 1000000 END,
                          NULL,
                          'r' || g,
                          (g % 2 = 0)
                   FROM generate_series(1, {ROWS}) g""")
        db.run(f"CREATE TABLE es_c ({COLS}) USING pgcolumnar")
        db.run("INSERT INTO es_c SELECT * FROM es_h")
        db.run(f"SELECT pgcolumnar.export_parquet('es_c', '{parq}')")
        ft = _Footer(parq)

        L = {c: ft.leafof(c) for c in ("id", "i2", "i8", "f4", "d", "ts", "nul", "alln", "t", "b")}
        skippable = {L[c] for c in ("id", "i2", "i8", "f4", "d", "ts", "nul")}
        skiptype = skippable | {L["alln"]}

        # ---- premises: the instrument reached the data
        r["leaves"] = ft.failed() if ft.error else str(sum(1 for v in L.values() if v.isdigit()))
        r["created_by"] = ft.fileval("created_by")
        r["row_groups"] = _num(ft.fileval("row_groups"))
        r["nleaves"] = _num(ft.fileval("leaves"))
        r["num_values"] = ft.failed() if ft.error else sum(
            int(c["num_values"]) for c in ft.chunks
            if c.get("leaf") == L["id"] and c.get("num_values", "").isdigit())
        r["es_c rows"] = _num(db.q("SELECT count(*) FROM es_c"))

        db.run(f"""CREATE TABLE oracle AS
                   SELECT (row_number() OVER () - 1) / {RGSIZE} AS grp, *
                   FROM pgcolumnar.read_parquet('{parq}') AS t({COLS})""")
        r["buckets"] = db.q("SELECT string_agg(c::text, ',' ORDER BY grp) "
                            "FROM (SELECT grp, count(*) c FROM oracle GROUP BY grp) s")
        r["contiguous"] = db.q(f"SELECT bool_and(mn = grp * {RGSIZE} AND mx = grp * {RGSIZE} + c - 1) "
                               "FROM (SELECT grp, min(id) mn, max(id) mx, count(*) c "
                               "FROM oracle GROUP BY grp) s")
        r["oracle rows"] = _num(db.q("SELECT count(*) FROM oracle"))
        r["unfolded file"] = db.set_hash("SELECT id,i2,i8,f4,nul,alln,t,b FROM oracle")
        r["unfolded heap"] = db.set_hash("SELECT id,i2,i8,f4,nul,alln,t,b FROM es_h")

        # ---- (a) presence
        if ft.error:
            for k in ("both", "bothmax", "onesided", "accounted"):
                r[k] = ft.failed()
        else:
            both = ft.count(skippable, "has_min", "1")
            neither = ft.count({L["alln"]}, "has_min", "0")
            twosided = sum(1 for c in ft.chunks if c.get("leaf") in skiptype
                           and c.get("has_min") == "1" and c.get("has_max") == "1")
            onesided = (ft.count(skiptype, "has_min", "1") + ft.count(skiptype, "has_max", "1")
                        - 2 * twosided)
            r["both"], r["bothmax"], r["onesided"] = both, ft.count(skippable, "has_max", "1"), onesided
            r["accounted"] = both + neither + onesided
        r["alln bounds"] = ft.none_of({L["alln"]}, "has_min", "1")
        r["width"] = _width(ft)

        # ---- (b) the values are right, per row group
        r["bounds"] = {(col, g, side): (ft.cf(g, L[col], side), _ora(db, col, g, side))
                       for col in ("id", "i2", "i8", "f4", "d", "ts", "nul")
                       for g in range(NGROUPS) for side in ("min", "max")}
        r["id minima"] = ft.failed() if ft.error else len(
            {c.get("min") for c in ft.chunks if c.get("leaf") == L["id"]})
        r["min<=max"] = _min_le_max(ft)

        # ---- (c) null_count
        r["null_count written"] = ft.failed() if ft.error else \
            f"{sum(1 for c in ft.chunks if c.get('null_count') == '-')} of {len(ft.chunks)} chunks"
        r["nulls"] = {(col, g): (ft.cf(g, L[col], "null_count"), _nulls_of(db, col, g))
                      for col in ("nul", "d", "ts", "alln", "id") for g in range(NGROUPS)}

        # ---- (d) values with no Parquet representation are folded
        r["inf dates"] = db.q("SELECT count(*) > 0 FROM es_h WHERE d IN ('infinity','-infinity')")
        r["inf timestamps"] = db.q("SELECT count(*) > 0 FROM es_h WHERE ts = 'infinity'")
        d_range = db.q("SELECT (min(d) - DATE '1970-01-01') || ' ' || (max(d) - DATE '1970-01-01') "
                       "FROM es_h WHERE isfinite(d)")
        ts_range = db.q("SELECT (EXTRACT(EPOCH FROM min(ts) - TIMESTAMP '1970-01-01') * 1000000)::bigint "
                        "|| ' ' || (EXTRACT(EPOCH FROM max(ts) - TIMESTAMP '1970-01-01') * 1000000)::bigint "
                        "FROM es_h WHERE isfinite(ts)")
        r["sentinels"] = _sentinels(ft, (L["d"], L["ts"]), d_range, ts_range)

        # ---- (e) the float rules; z8's -0.0 is quoted so float8in keeps its sign
        db.run("CREATE TABLE nan_h (f8a float8, f8b float8, z8 float8, z9 float8)")
        db.run("""INSERT INTO nan_h VALUES
                    (1.0,  'NaN',  -1.0,   0.0),
                    ('NaN','NaN',  '-0.0', 1.0),
                    (-2.0, 'NaN',  -3.0,   2.0),
                    (NULL, NULL,   NULL,   NULL)""")
        db.run("CREATE TABLE nan_c (LIKE nan_h) USING pgcolumnar")
        db.run("INSERT INTO nan_c SELECT * FROM nan_h")
        db.run(f"SELECT pgcolumnar.export_parquet('nan_c', '{nanq}')")
        nt = _Footer(nanq)
        N = {c: nt.leafof(c) for c in ("f8a", "f8b", "z8", "z9")}
        r["f8a has_min"] = _num(nt.cf(0, N["f8a"], "has_min"))
        r["f8a min"] = nt.cf(0, N["f8a"], "min")
        r["f8a max"] = nt.cf(0, N["f8a"], "max")
        r["f8b has_min"] = _num(nt.cf(0, N["f8b"], "has_min"))
        r["f8b has_max"] = _num(nt.cf(0, N["f8b"], "has_max"))
        floats = [c for c in nt.chunks if c.get("ptname") in ("FLOAT", "DOUBLE")]
        r["nan_count written"] = nt.failed() if nt.error else \
            f"{sum(1 for c in floats if c.get('nan_count') == '-')} of {len(floats)} chunks"
        r["f8b nan_count"] = _num(nt.cf(0, N["f8b"], "nan_count"))
        r["nan bounds"] = nt.failed() if nt.error else \
            f"{sum(1 for c in nt.chunks if {c.get('min'), c.get('max')} & {'nan', '-nan'})} of {len(nt.chunks)} chunks"
        r["z8 maxhex"] = nt.cf(0, N["z8"], "maxhex")
        r["z9 minhex"] = nt.cf(0, N["z9"], "minhex")

        # ---- (f) column_orders; (g) the types this change does not claim
        r["column_orders"] = _num(ft.fileval("column_orders"))
        r["order_ids"] = ft.fileval("order_ids")
        every = set(L.values())
        r["dep_min"] = ft.none_of(every, "has_dep_min", "1")
        r["dep_max"] = ft.none_of(every, "has_dep_max", "1")
        r["t bounds"] = ft.none_of({L["t"]}, "has_min", "1")
        r["b bounds"] = ft.none_of({L["b"]}, "has_min", "1")

        # ---- (h) the payoff: the FDW skips on a file we exported
        db.run("CREATE SERVER es_srv FOREIGN DATA WRAPPER pgcolumnar_parquet")
        db.run(f"CREATE FOREIGN TABLE es_ft ({COLS}) SERVER es_srv OPTIONS (path '{parq}')")
        r["fdw groups"] = _explain_val(db, "Row Groups", "WHERE id < 10")
        r["skipped"] = {w: _explain_val(db, "Row Groups Skipped", w) for w in (
            "", "WHERE id BETWEEN 70000 AND 71000", "WHERE i8 < 1000000000::bigint",
            "WHERE f4 < 1.0::real", "WHERE d < DATE '2000-01-05'",
            "WHERE ts < TIMESTAMP '2000-01-01 00:30:00'", "WHERE i8 < 5::bigint",
            "WHERE i8 < 5::int", "WHERE f4 < 0.4::real", "WHERE f4 >= 200000.0::real",
            "WHERE i8 < '5'", "WHERE i8 < 5", "WHERE i8 < 5000000000", "WHERE f4 < '0.4'",
            "WHERE f4 < 0.4", "WHERE i2 < '500'", "WHERE i2 < 500", "WHERE d < '2000-01-05'",
            "WHERE d < TIMESTAMP '2000-01-05 00:00:00'", "WHERE id < -1",
            "WHERE id < -5000000000")}
        r["filter"] = {k: _filter_has(db, *k) for k in (
            ("WHERE i8 < 5.0", "::numeric"), ("WHERE i8 < '5'", "::bigint"),
            ("WHERE d < TIMESTAMP '2000-01-05 00:00:00'", "::timestamp"),
            ("WHERE d < '2000-01-05'", "::date"))}
        r["sound"] = {p: (db.set_hash(f"SELECT {ALL_COLS} FROM es_ft WHERE {p}"),
                          db.set_hash(f"SELECT {ALL_COLS} FROM oracle WHERE {p}"))
                      for p, _ in SOUND}
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- premises: the instrument reached the data

def test_export_stats_leaf_indexes_resolve(es, expect):
    expect.text(es["leaves"], "10", "premise: leaf indexes resolved for all ten columns")


def test_export_stats_file_is_one_we_wrote(es, expect):
    expect.text(es["created_by"], "pgColumnar", "premise: the file under test is one we wrote")


def test_export_stats_footer_declares_four_row_groups(es, expect):
    expect.num(es["row_groups"], NGROUPS, "premise: the footer declares 4 row groups")


def test_export_stats_footer_declares_ten_leaves(es, expect):
    expect.num(es["nleaves"], 10, "premise: the footer declares 10 leaf columns")


def test_export_stats_parser_reaches_column_metadata(es, expect):
    expect.num(es["num_values"], es["es_c rows"],
               "premise: the parser reaches ColumnMetaData (num_values sums to the table)")


# Not premises (#1339, 2026-10-21): with date and timestamp infinities no longer folded,
# the export writes values read_parquet refuses, and all four go red.
def test_export_stats_oracle_buckets_are_row_groups(es, expect):
    expect.text(es["buckets"], f"{RGSIZE},{RGSIZE},{RGSIZE},1",
                "the oracle's buckets are the file's row groups")


def test_export_stats_buckets_hold_contiguous_ids(es, expect):
    expect.text(es["contiguous"], "t", "each bucket holds its own contiguous id range")


def test_export_stats_oracle_holds_every_row(es, expect):
    expect.num(es["oracle rows"], ROWS, "the oracle holds every exported row")


def test_export_stats_exported_data_equals_the_heap(es, expect):
    expect.hash(es["unfolded file"], es["unfolded heap"],
                "the exported data equals the heap on the columns nothing folds")


# ---- (a) presence

def test_export_stats_every_chunk_carries_a_minimum(es, expect):
    expect.num(es["both"], NGROUPS * 7, "every value-bearing skippable chunk carries a minimum")


def test_export_stats_every_chunk_carries_a_maximum(es, expect):
    expect.num(es["bothmax"], NGROUPS * 7, "every value-bearing skippable chunk carries a maximum")


def test_export_stats_no_chunk_is_one_sided(es, expect):
    expect.num(es["onesided"], 0, "no chunk carries exactly one bound")


def test_export_stats_buckets_account_for_every_chunk(es, expect):
    expect.num(es["accounted"], NGROUPS * 8, "buckets account for every skippable-type chunk")


def test_export_stats_all_null_column_has_no_bounds(es, expect):
    expect.text(es["alln bounds"], f"0 of {NGROUPS} chunks", "the all-null column carries no bounds")


def test_export_stats_bounds_are_physical_width(es, expect):
    expect.text(es["width"], f"0 of {NGROUPS * 7 * 2} bounds", "every bound is its physical width")


# ---- (b) the values are right, per row group, against the heap

@pytest.mark.parametrize("col,g,side,name", BOUNDS)
def test_export_stats_group_bound_equals_the_heap(es, expect, col, g, side, name):
    got, want = es["bounds"][(col, g, side)]
    if col == "f4":
        got, want = _g9(got, want)
        expect.text(got, want, name)
    else:
        expect.num(_num(got), _num(want), name)


def test_export_stats_four_distinct_id_minima(es, expect):
    expect.num(es["id minima"], NGROUPS, "the four groups report four distinct id minima")


def test_export_stats_min_le_max(es, expect):
    expect.text(es["min<=max"], f"0 of {NGROUPS * 7} chunks", "min <= max in every chunk carrying both")


# ---- (c) null_count

def test_export_stats_null_count_always_written(es, expect):
    expect.text(es["null_count written"], f"0 of {NGROUPS * 10} chunks",
                "null_count is written for every chunk, including where it is zero")


@pytest.mark.parametrize("col,g,name", NULLS)
def test_export_stats_group_null_count_equals_the_heap(es, expect, col, g, name):
    got, want = es["nulls"][(col, g)]
    expect.num(_num(got), _num(want), name)


# ---- (d) values with no Parquet representation are folded, not admitted

def test_export_stats_fixture_has_infinite_dates(es, expect):
    expect.text(es["inf dates"], "t", "premise: the fixture's infinite dates are more than none")


def test_export_stats_fixture_has_infinite_timestamps(es, expect):
    expect.text(es["inf timestamps"], "t", "premise: the fixture's infinite timestamps are more than none")


def test_export_stats_no_infinity_reaches_a_bound(es, expect):
    expect.text(es["sentinels"], f"0 of {NGROUPS * 2 * 2} bounds",
                "no date or timestamp bound is an infinity sentinel")


# ---- (e) the float rules

def test_export_stats_nan_column_keeps_its_bounds(es, expect):
    expect.num(es["f8a has_min"], 1, "a float column holding a NaN still carries both bounds")


def test_export_stats_nan_column_minimum(es, expect):
    got, want = _g9(es["f8a min"], "-2.0")
    expect.text(got, want, "min(f8a) is the smallest non-NaN value")


def test_export_stats_nan_column_maximum(es, expect):
    got, want = _g9(es["f8a max"], "1.0")
    expect.text(got, want, "max(f8a) is the largest non-NaN value")


def test_export_stats_all_nan_column_has_no_minimum(es, expect):
    expect.num(es["f8b has_min"], 0, "an all-NaN column carries no minimum")


def test_export_stats_all_nan_column_has_no_maximum(es, expect):
    expect.num(es["f8b has_max"], 0, "an all-NaN column carries no maximum")


def test_export_stats_nan_count_always_written(es, expect):
    expect.text(es["nan_count written"], "0 of 4 chunks", "nan_count is written for every float chunk")


def test_export_stats_nan_count_counts(es, expect):
    expect.num(es["f8b nan_count"], 3, "nan_count counts the NaNs")


def test_export_stats_no_nan_bound(es, expect):
    expect.text(es["nan bounds"], "0 of 4 chunks", "no float bound is a NaN bit pattern")


def test_export_stats_zero_maximum_is_positive(es, expect):
    expect.text(es["z8 maxhex"], "0000000000000000", "a zero maximum is written as +0.0")


def test_export_stats_zero_minimum_is_negative(es, expect):
    expect.text(es["z9 minhex"], "0000000000000080", "a zero minimum is written as -0.0")


# ---- (f) column_orders

def test_export_stats_one_column_order_per_leaf(es, expect):
    expect.num(es["column_orders"], 10, "the footer carries one ColumnOrder per leaf column")


def test_export_stats_column_orders_are_type_order(es, expect):
    expect.text(es["order_ids"], "1", "every ColumnOrder is TYPE_ORDER")


# ---- (g) the types this change does not claim

def test_export_stats_no_deprecated_minimum(es, expect):
    expect.text(es["dep_min"], f"0 of {NGROUPS * 10} chunks", "no chunk carries the deprecated minimum (field 2)")


def test_export_stats_no_deprecated_maximum(es, expect):
    expect.text(es["dep_max"], f"0 of {NGROUPS * 10} chunks", "no chunk carries the deprecated maximum (field 1)")


def test_export_stats_byte_array_has_no_bounds(es, expect):
    expect.text(es["t bounds"], f"0 of {NGROUPS} chunks", "the BYTE_ARRAY column carries no bounds")


def test_export_stats_boolean_has_no_bounds(es, expect):
    expect.text(es["b bounds"], f"0 of {NGROUPS} chunks", "the BOOLEAN column carries no bounds")


# ---- (h) the payoff: the FDW skips on a file we exported

def test_export_stats_fdw_reports_four_groups(es, expect):
    expect.num(es["fdw groups"], NGROUPS, "premise: the FDW reports 4 row groups")


def test_export_stats_unrestricted_scan_skips_nothing(es, expect):
    expect.num(es["skipped"][""], 0, "an unrestricted scan skips nothing")


def test_export_stats_one_group_predicate_skips_three(es, expect):
    expect.num(es["skipped"]["WHERE id BETWEEN 70000 AND 71000"], 3,
               "a predicate confined to one group skips the other three")


def test_export_stats_bigint_predicate_skips(es, expect):
    expect.num(es["skipped"]["WHERE i8 < 1000000000::bigint"], 3, "a bigint predicate skips")


def test_export_stats_float_predicate_skips(es, expect):
    expect.num(es["skipped"]["WHERE f4 < 1.0::real"], 3, "a float predicate skips")


def test_export_stats_date_predicate_skips(es, expect):
    expect.num(es["skipped"]["WHERE d < DATE '2000-01-05'"], 3, "a date predicate skips")


def test_export_stats_timestamp_predicate_skips(es, expect):
    expect.num(es["skipped"]["WHERE ts < TIMESTAMP '2000-01-01 00:30:00'"], 3, "a timestamp predicate skips")


def test_export_stats_matching_constant_type_skips(es, expect):
    expect.num(es["skipped"]["WHERE i8 < 5::bigint"], NGROUPS,
               "control: the same column skips when the constant matches its type")


def test_export_stats_cross_type_constant_skips_like_the_matching_one(es, expect):
    # Since #1712 a constant of another type in the family skips (was: nothing).
    expect.num(es["skipped"]["WHERE i8 < 5::int"], NGROUPS,
               "a cross-type constant skips as the matching one does, on the identical predicate")


def test_export_stats_float_less_than_skips(es, expect):
    expect.num(es["skipped"]["WHERE f4 < 0.4::real"], NGROUPS,
               "control: a float < predicate below every bound skips")


def test_export_stats_float_greater_equal_skips_nothing(es, expect):
    expect.num(es["skipped"]["WHERE f4 >= 200000.0::real"], 0,
               "a float >= predicate above every bound still skips nothing")


def test_export_stats_quoted_bigint_literal_skips(es, expect):
    expect.num(es["skipped"]["WHERE i8 < '5'"], NGROUPS,
               "a quoted literal takes a bigint column's type and skips")


def test_export_stats_unquoted_bigint_literal_skips_as_well(es, expect):
    expect.num(es["skipped"]["WHERE i8 < 5"], NGROUPS,
               "the same digits unquoted are an integer, and skip as well")


def test_export_stats_wide_digits_skip_a_bigint(es, expect):
    expect.num(es["skipped"]["WHERE i8 < 5000000000"], 3,
               "digits too wide for an integer are a bigint, and skip")


def test_export_stats_decimal_literal_casts_the_column(es, expect):
    expect.num(es["filter"][("WHERE i8 < 5.0", "::numeric")], 1,
               "a decimal-point literal casts the column, not the constant")


def test_export_stats_quoted_literal_casts_the_constant(es, expect):
    expect.num(es["filter"][("WHERE i8 < '5'", "::bigint")], 1,
               "control: the quoted form casts the constant to the column's type")


def test_export_stats_quoted_real_literal_skips(es, expect):
    expect.num(es["skipped"]["WHERE f4 < '0.4'"], NGROUPS,
               "a quoted literal takes a real column's type and skips")


def test_export_stats_unquoted_real_literal_skips_as_well(es, expect):
    expect.num(es["skipped"]["WHERE f4 < 0.4"], NGROUPS,
               "the same literal unquoted is double precision, and skips as well")


def test_export_stats_quoted_smallint_literal_skips(es, expect):
    expect.num(es["skipped"]["WHERE i2 < '500'"], 1,
               "a quoted literal takes a smallint column's type and skips")


def test_export_stats_unquoted_smallint_literal_skips_as_well(es, expect):
    expect.num(es["skipped"]["WHERE i2 < 500"], 1,
               "the same digits unquoted are an integer, and skip as well (int2)")


def test_export_stats_adorned_date_skips(es, expect):
    expect.num(es["skipped"]["WHERE d < DATE '2000-01-05'"], 3,
               "an adorned literal of the column's own type skips")


def test_export_stats_unadorned_date_skips(es, expect):
    expect.num(es["skipped"]["WHERE d < '2000-01-05'"], 3,
               "the same date unadorned is unknown, takes the column's type and skips")


def test_export_stats_timestamp_adorned_date_skips_as_well(es, expect):
    expect.num(es["skipped"]["WHERE d < TIMESTAMP '2000-01-05 00:00:00'"], 3,
               "the same date adorned as a timestamp skips as well")


def test_export_stats_adorned_literal_keeps_its_type(es, expect):
    expect.num(es["filter"][("WHERE d < TIMESTAMP '2000-01-05 00:00:00'", "::timestamp")], 1,
               "an adorned literal keeps its own type, not the column's")


def test_export_stats_unadorned_literal_takes_the_column_type(es, expect):
    expect.num(es["filter"][("WHERE d < '2000-01-05'", "::date")], 1,
               "control: the unadorned form resolves to the column's type instead")


def test_export_stats_integer_digits_prune_every_group(es, expect):
    expect.num(es["skipped"]["WHERE id < -1"], NGROUPS,
               "control: a digit string that fits an integer prunes every group")


def test_export_stats_int4_column_skips_wide_digits(es, expect):
    expect.num(es["skipped"]["WHERE id < -5000000000"], NGROUPS,
               "digits too wide for an integer are a bigint, and an int4 column skips as well")


@pytest.mark.parametrize("pred,name", SOUND)
def test_export_stats_fdw_result_equals_the_oracle(es, expect, pred, name):
    got, want = es["sound"][pred]
    expect.hash(got, want, name)


# ---- the other direction: the instrument can report a bound that IS there

def _pyarrow_footer(pa, pq, tmp_path):
    path = tmp_path / "pyarrow.parquet"
    pq.write_table(pa.table({"id": pa.array(range(200000), pa.int64())}), str(path),
                   row_group_size=50000)
    return _Footer(path)


def test_export_stats_parser_finds_pyarrow_bounds(expect, tmp_path):
    pa = expect.require_module("pyarrow", name="control: the parser finds pyarrow's bounds")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="control: the parser finds pyarrow's bounds")
    if pq is None:
        return
    ft = _pyarrow_footer(pa, pq, tmp_path)
    expect.num(ft.failed() if ft.error else sum(1 for c in ft.chunks if c.get("has_min") == "1"), 4,
               "control: the parser finds pyarrow's bounds")


def test_export_stats_parser_reports_deprecated_fields(expect, tmp_path):
    pa = expect.require_module("pyarrow", name="control: the parser reports the deprecated fields where they exist")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="control: the parser reports the deprecated fields where they exist")
    if pq is None:
        return
    ft = _pyarrow_footer(pa, pq, tmp_path)
    expect.num(ft.failed() if ft.error else sum(1 for c in ft.chunks if c.get("has_dep_min") == "1"), 4,
               "control: the parser reports the deprecated fields where they exist")

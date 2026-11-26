"""pgcolumnar.iceberg_scan reads an Iceberg table at its current snapshot, resolving
columns by field id (#388 phase 3c). The pytest twin of `test/iceberg_scan.sh`, every
check by the shell's name (#1341).

The warehouse is relocated so its recorded root differs from where it lies. The scan
reads the current snapshot's five rows, projects and reorders by field id, reads a
subset, and still reads a column the schema renamed, because it selects by id and not
by name; a name the schema lacks is refused with 42703. A struct, list or map column
declared with a scalar type is refused with 0A000, naming the column and its kind,
through iceberg_scan and through a pgcolumnar_iceberg foreign table, instead of
reading as null on every row (#1788). After an int -> long, float -> double or
decimal precision promotion (#1789), files written before it read as the current
schema's types: alone, mixed with newer files, under equality deletes on the promoted
column, and through a pgcolumnar_iceberg foreign table. A narrower declared type is
still refused with 42804, as are a date column declared bigint and positional
read_parquet's bigint over INT32, while read_parquet by field id widens. A role with
USAGE on the schema but without pg_read_server_files is refused with 42501, and a
non-superuser holding that role reads every row.

ON A PRIVATE DATABASE, with the warehouse copied into `server_dir`, because the
backend reads it. The two roles log in on their own, as the shell's do, and are
dropped at teardown. Where the fixture is missing the first test fails under the
shell's pgc_skip message, as pgc_skip does, and the rest decline unnamed.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_scan"
WH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse"
TMWH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse_typmod"
VARIANTS = WH.parent / "gen_iceberg_variants.py"
ROWS5 = "1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50"
SNAPCOLS = ("snapshot_id || '|' || coalesce(parent_snapshot_id::text, '-') || '|' || "
            "sequence_number || '|' || manifest_list")
WHP = WH.parent / "warehouse_promo"
WHD = WH.parent / "warehouse_del"
UNPRIV, READER = "pgc_icescan_unpriv", "pgc_icescan_reader"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _rows(dsn, sql, user=None, port=None):
    target = dsn if user is None else f"host=127.0.0.1 port={port} user={user} dbname={DB}"
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            return [r[0] for r in conn.execute(sql)]
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _refused_by(dsn, sql, phrase):
    """-> the SQLSTATE when `sql` is refused with `phrase` in its message, "SQLSTATE
    (another refusal: MESSAGE)" for any other refusal, "no error" when it ran."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return exc.sqlstate if phrase in msg else f"{exc.sqlstate} (another refusal: {msg})"
    return "no error"


def _sqlstate(dsn, sql, user=None, port=None):
    target = dsn if user is None else f"host=127.0.0.1 port={port} user={user} dbname={DB}"
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


def _state_or_value(dsn, sql):
    """-> the SQLSTATE `sql` raised, or "no error (<first value>)" when it raised none."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return f"no error ({row[0] if row else ''})"
def _refusal_phrase(dsn, sql, phrase, label):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, else
    "<SQLSTATE>: <message>", or "no error"."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or _message(exc)
        return exc.sqlstate + label if phrase in msg else f"{exc.sqlstate}: {msg}"
    return "no error"


def _variants(dest):
    """-> {name: path} from gen_iceberg_variants.py, or {} with the failure under "error"."""
    run = subprocess.run([sys.executable, str(VARIANTS), str(dest)],
                         capture_output=True, text=True)
    out = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line)
    if run.returncode != 0:
        out["error"] = (run.stdout + run.stderr).strip()
    return out


def _refusal(dsn, sql, regex, label):
    """-> "<SQLSTATE> (<label>)" when the statement raised an error whose primary
    message matches `regex`, "<SQLSTATE>: <message>" for any other error, or
    "no error: <rows>" with what it read instead."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return f"{exc.sqlstate} ({label})" if re.search(regex, msg) else f"{exc.sqlstate}: {msg}"
    return "no error: " + " ".join("|".join("" if v is None else str(v) for v in r) for r in rows)


def _error(dsn, sql):
    """-> the primary message of the error `sql` raised, or "no error: <rows>" (#1871)."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.diag.message_primary or ""
    return f"no error: {rows}"


def _lines(v):
    return v if isinstance(v, str) else "\n".join(str(x) for x in v)


def _v3_default_copies(md, eq_md):
    """Write three format-version 3 copies of fixture metadata (#2087) and return
    their paths. v3default.metadata.json: the events table, whose current schema
    gains field 4, qty, required, initial-default 7, which no data file carries.
    v3optdefault.metadata.json: the same, but qty is optional, initial-default 42.
    eq_v3default.metadata.json: warehouse_promo's equality-delete table, with
    initial-default 0 on k, the column its equality deletes compare. k is made
    required, so an eq-delete refusal that skips required fields fails."""
    m = json.loads(md.read_text())
    m["format-version"] = 3
    cur = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
    added = json.loads(json.dumps(cur))
    added["schema-id"] = max(s["schema-id"] for s in m["schemas"]) + 1
    added["fields"].append({"id": 4, "name": "qty", "required": True, "type": "int",
                            "initial-default": 7, "write-default": 7})
    m["schemas"].append(added)
    m["current-schema-id"] = added["schema-id"]
    m["last-column-id"] = 4
    v3md = md.parent / "v3default.metadata.json"
    v3md.write_text(json.dumps(m))
    added["fields"][-1] = {"id": 4, "name": "qty", "required": False, "type": "int",
                           "initial-default": 42, "write-default": 42}
    v3opt = md.parent / "v3optdefault.metadata.json"
    v3opt.write_text(json.dumps(m))
    e = json.loads(eq_md.read_text())
    e["format-version"] = 3
    for f in next(s for s in e["schemas"] if s["schema-id"] == e["current-schema-id"])["fields"]:
        if f["name"] == "k":
            f["initial-default"] = 0
            f["required"] = True
    v3eq = eq_md.parent / "eq_v3default.metadata.json"
    v3eq.write_text(json.dumps(e))
    for p in (v3md, v3opt, v3eq):
        p.chmod(0o666)
    return v3md, v3opt, v3eq


def _v3_more_copies(v3md, eq_md):
    """Write five more format-version 3 copies (#2087), each pinning one part of
    the refusal's condition, and return their paths in this order.
    v3strdefault / v3booldefault: qty optional, a string with initial-default ""
    and a boolean with initial-default false, so a refusal narrowed to numbers,
    or one that takes a falsy default for none, reads null again.
    v3nulldefault: qty optional with initial-default null, which means none, and
    a write-default of 5, so a reader that falls back to the write-default for a
    null initial-default refuses it (review of #2151 at 48251e9f).
    v3firstdefault: v3default with qty, initial-default 7, first in the field list,
    so a refusal above the name match would refuse the other columns.
    eq_v3otherdefault: the eq table with a new field "extra", initial-default 7,
    first in the field list; the equality deletes compare k, not extra."""
    def variant(name, field, first=False):
        m = json.loads(v3md.read_text())
        cur = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
        cur["fields"] = [f for f in cur["fields"] if f["id"] != 4]
        cur["fields"].insert(0 if first else len(cur["fields"]), field)
        p = v3md.parent / f"{name}.metadata.json"
        p.write_text(json.dumps(m))
        return p
    out = [
        variant("v3strdefault", {"id": 4, "name": "qty", "required": False, "type": "string",
                                 "initial-default": "", "write-default": ""}),
        variant("v3booldefault", {"id": 4, "name": "qty", "required": False, "type": "boolean",
                                  "initial-default": False, "write-default": False}),
        variant("v3nulldefault", {"id": 4, "name": "qty", "required": False, "type": "int",
                                  "initial-default": None, "write-default": 5}),
        variant("v3firstdefault", {"id": 4, "name": "qty", "required": True, "type": "int",
                                   "initial-default": 7, "write-default": 7}, first=True),
    ]
    e = json.loads(eq_md.read_text())
    e["format-version"] = 3
    cur = next(s for s in e["schemas"] if s["schema-id"] == e["current-schema-id"])
    cur["fields"].insert(0, {"id": 6, "name": "extra", "required": False, "type": "int",
                             "initial-default": 7, "write-default": 7})
    e["last-column-id"] = 6
    eq_out = eq_md.parent / "eq_v3otherdefault.metadata.json"
    eq_out.write_text(json.dumps(e))
    out.append(eq_out)
    for p in out:
        p.chmod(0o666)
    return out


def _v3_round3_copies(v3md, dmd):
    """Write two more format-version 3 copies (#2087, review of #2151 at 09098feb)
    and return their paths. eq_v3strdefault.metadata.json: warehouse_del's eqnull
    table, whose equality deletes compare region, a string, with region's
    initial-default "x"; a refusal narrowed to long columns or to numeric
    defaults reads rows again. v3writedefault.metadata.json: the events table
    with qty optional and only a write-default of 5; with no initial-default,
    old rows read null, and the scan must not refuse."""
    e = json.loads((dmd / "eqnull.metadata.json").read_text())
    e["format-version"] = 3
    cur = next(s for s in e["schemas"] if s["schema-id"] == e["current-schema-id"])
    for f in cur["fields"]:
        if f["name"] == "region":
            f["initial-default"] = "x"
            f["write-default"] = "x"
    eq_out = dmd / "eq_v3strdefault.metadata.json"
    eq_out.write_text(json.dumps(e))
    m = json.loads(v3md.read_text())
    cur = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
    cur["fields"] = [f for f in cur["fields"] if f["id"] != 4]
    cur["fields"].append({"id": 4, "name": "qty", "required": False, "type": "int",
                          "write-default": 5})
    v3_out = v3md.parent / "v3writedefault.metadata.json"
    v3_out.write_text(json.dumps(m))
    for p in (eq_out, v3_out):
        p.chmod(0o666)
    return eq_out, v3_out


def _v3_round4_copies(v3opt, dmd):
    """Write two more copies (#2087, review of #2151 at 48251e9f) and return their
    paths. eq_v3intdefault.metadata.json: warehouse_del's eqpart_apply table, whose
    equality deletes compare grp, an int, as format-version 3 with grp's
    initial-default 0; a refusal placed after the int type match reads rows again.
    v2optdefault.metadata.json: v3optdefault with format-version 2. Iceberg forbids
    a non-null initial-default below v3, but the reader refuses it there too, so a
    refusal gated on format-version 3 fails."""
    e = json.loads((dmd / "eqpart_apply.metadata.json").read_text())
    e["format-version"] = 3
    cur = next(s for s in e["schemas"] if s["schema-id"] == e["current-schema-id"])
    for f in cur["fields"]:
        if f["name"] == "grp":
            f["initial-default"] = 0
            f["write-default"] = 0
    eq_out = dmd / "eq_v3intdefault.metadata.json"
    eq_out.write_text(json.dumps(e))
    m = json.loads(v3opt.read_text())
    m["format-version"] = 2
    v2_out = v3opt.parent / "v2optdefault.metadata.json"
    v2_out.write_text(json.dumps(m))
    for p in (eq_out, v2_out):
        p.chmod(0o666)
    return eq_out, v2_out


@pytest.fixture(scope="module")
def isc(pgc_cluster, server_dir):
    if not list(WH.glob("db/events/data/*/*.parquet")):
        yield None
        return
    dest = server_dir / "relocated"
    shutil.copytree(WH / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    md = sorted((dest / "db" / "events" / "metadata").glob("*.metadata.json"))[-1]
    var = _variants(dest)
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)

    def vpath(name):
        return var.get(name, f"no variant {name}: {var.get('error', var)}")
    df = sorted((dest / "db" / "events" / "data").glob("*/*.parquet"))[0]
    m = json.loads(md.read_text())
    sch = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
    for f in sch["fields"]:
        if f["id"] == 3:
            f["name"] = "amt2"
    ren = md.parent / "renamed.metadata.json"
    ren.write_text(json.dumps(m))
    ren.chmod(0o666)
    tmdest = server_dir / "tmwh"
    shutil.copytree(TMWH / "db", tmdest / "db")
    for p in [tmdest, *tmdest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    md_pad = sorted((tmdest / "db" / "byregion_pad" / "metadata").glob("*.metadata.json"))[-1]
    md_frac = sorted((tmdest / "db" / "byhour_frac" / "metadata").glob("*.metadata.json"))[-1]
    # #1788: the current schema also gets a struct, a list and a map, added after
    # the data files were written, as the shell's nested.metadata.json does
    m = json.loads(md.read_text())
    sch = next(s for s in m["schemas"] if s["schema-id"] == m["current-schema-id"])
    sch["fields"] += [
        {"id": 4, "name": "st", "required": False, "type": {"type": "struct", "fields": [
            {"id": 5, "name": "a", "required": False, "type": "int"}]}},
        {"id": 6, "name": "li", "required": False, "type": {
            "type": "list", "element-id": 7, "element": "int", "element-required": False}},
        {"id": 8, "name": "mp", "required": False, "type": {
            "type": "map", "key-id": 9, "key": "string", "value-id": 10, "value": "long",
            "value-required": False}},
    ]
    m["last-column-id"] = 10
    nest = md.parent / "nested.metadata.json"
    nest.write_text(json.dumps(m))
    nest.chmod(0o666)
    pdest = server_dir / "promo"
    shutil.copytree(WHP / "db", pdest / "db")
    for p in [pdest, *pdest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    pmd = pdest / "db" / "t" / "metadata"
    ddest = server_dir / "del_v3"
    shutil.copytree(WHD / "db", ddest / "db")
    for p in [ddest, *ddest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    dmd = ddest / "db" / "t" / "metadata"
    pdata = pdest / "db" / "t" / "data"
    admin = pgc_cluster.dsn()
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        for role in (UNPRIV, READER):
            a.execute(f"DROP ROLE IF EXISTS {role}")
            a.execute(f"CREATE ROLE {role} LOGIN")
        a.execute(f"GRANT pg_read_server_files TO {READER}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {UNPRIV}, {READER}")
            c.execute("CREATE SERVER icescan_nest FOREIGN DATA WRAPPER pgcolumnar_iceberg")
            c.execute("CREATE FOREIGN TABLE icescan_fnest (id bigint, st text) "
                      f"SERVER icescan_nest OPTIONS (metadata_path '{nest}')")
        scan = "pgcolumnar.iceberg_scan('{}')"
        r = {
            "ids": _rows(dsn, f"SELECT string_agg(field_id::text, ',' ORDER BY field_id) "
                              f"FROM pgcolumnar.parquet_schema('{df}') WHERE field_id IS NOT NULL"),
            "full": _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM {scan.format(md)} "
                               "AS t(id bigint, region text, amount int) ORDER BY id"),
            "reorder": _rows(dsn, f"SELECT amount || '|' || id FROM {scan.format(md)} AS t(amount int, id bigint) "
                                  "ORDER BY id"),
            "subset": _rows(dsn, f"SELECT string_agg(region, ',' ORDER BY region) FROM {scan.format(md)} AS t(region text)"),
            "renamed": _rows(dsn, f"SELECT string_agg(amt2::text, ',' ORDER BY amt2) FROM {scan.format(ren)} AS t(amt2 int)"),
            "nosuch": _sqlstate(dsn, f"SELECT * FROM {scan.format(md)} AS t(nosuch int)"),
            "plain snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{md}')"),
            "gz_infix snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_infix')}')"),
            "gz_suffix snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_suffix')}')"),
            "gz_bomb": _refusal_phrase(dsn, f"SELECT * FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_bomb')}')",
                                       "inflates past", " past the cap"),
            "gz_trunc": _refusal_phrase(dsn, f"SELECT * FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_trunc')}')",
                                        "gzip", " naming gzip"),
            "gz_multi snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_multi')}')"),
            "gz_padded snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_padded')}')"),
            "notpq": _refusal_phrase(dsn, f"SELECT count(*) FROM pgcolumnar.iceberg_scan('{vpath('notpq')}') AS t(id bigint)",
                                     "only PARQUET is supported", " naming PARQUET"),
            "gz_large snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_large')}')"),
            "gz_grow snap": _rows(dsn, f"SELECT {SNAPCOLS} FROM pgcolumnar.iceberg_current_snapshot('{vpath('gz_grow')}')"),
            "az": _refusal_phrase(dsn, "SELECT * FROM pgcolumnar.iceberg_current_snapshot('az://b/t/metadata/v1.metadata.json')",
                                  'reading "az://', " remote refusal"),
            "nest scalar": _rows(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {scan.format(nest)} "
                                      "AS t(id bigint)"),
            "nest struct": _refusal(dsn, f"SELECT * FROM {scan.format(nest)} AS t(id bigint, st text)",
                                    r'output column "st" is a struct field', "struct"),
            "nest list": _refusal(dsn, f"SELECT * FROM {scan.format(nest)} AS t(id bigint, li int4)",
                                  r'output column "li" is a list field', "list"),
            "nest map": _refusal(dsn, f"SELECT * FROM {scan.format(nest)} AS t(id bigint, mp int8)",
                                 r'output column "mp" is a map field', "map"),
            "fdw struct": _refusal(dsn, "SELECT * FROM icescan_fnest",
                                   r'output column "st" is a struct field', "struct"),
            "money": _error(dsn, f"SELECT * FROM {scan.format(md)} AS t(id bigint, region text, amount money)"),
            "unpriv": _sqlstate(dsn, f"SELECT * FROM {scan.format(md)} AS t(id bigint)", UNPRIV, pgc_cluster.port),
            "reader": _rows(dsn, f"SELECT count(*) FROM {scan.format(md)} AS t(id bigint)", READER, pgc_cluster.port),
            # a declared typmod applies to every value (#1790)
            "tm v1": _state_or_value(dsn, f"SELECT string_agg(region, ',') FROM {scan.format(md)} "
                                          "AS t(region varchar(1))"),
            "tm v2": _rows(dsn, f"SELECT string_agg(region, ',' ORDER BY region) FROM {scan.format(md)} "
                                "AS t(region varchar(2))"),
            "tm pad": _rows(dsn, "SELECT string_agg(id || ':' || region || ':' || octet_length(region), ',' "
                                 f"ORDER BY id) FROM {scan.format(md_pad)} AS t(id bigint, region varchar(2))"),
            "tm frac": _rows(dsn, f"SELECT string_agg(id || '|' || ts, ',' ORDER BY id) FROM {scan.format(md_frac)} "
                                  "AS t(id bigint, ts timestamp(0))"),
        }
        for name in ("gz_infix", "gz_suffix", "lower", "mixed"):
            r[name + " rows"] = _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM "
                                           f"{scan.format(vpath(name))} AS t(id bigint, region text, amount int) "
                                           "ORDER BY id")
        # type promotion (#1789): warehouse_promo, see gen_promotion_fixture.py
        prow = "id || '|' || k || '|' || f || '|' || d"
        pcols = "AS t(id bigint, k bigint, f double precision, d numeric(20,2))"
        old_md, mixed_md, eq_md = (pmd / f"{n}.metadata.json" for n in ("old", "mixed", "eq"))
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE SERVER ice_promo FOREIGN DATA WRAPPER pgcolumnar_iceberg")
            c.execute("CREATE FOREIGN TABLE promo_mixed (id bigint, k bigint, f double precision, d numeric(20,2)) "
                      f"SERVER ice_promo OPTIONS (metadata_path '{mixed_md}')")
            c.execute("CREATE FOREIGN TABLE promo_eq (id bigint, k bigint) "
                      f"SERVER ice_promo OPTIONS (metadata_path '{eq_md}')")
        r.update({
            "promo types": _rows(dsn, "SELECT string_agg(f || ':' || column_name || ':' || data_type, ',' "
                                      "ORDER BY f, field_id) FROM (VALUES ('old'), ('old2'), ('new')) AS v(f), "
                                      f"LATERAL pgcolumnar.parquet_schema('{pdata}/' || f || '.parquet') "
                                      "WHERE field_id BETWEEN 2 AND 4"),
            "promo long": _rows(dsn, "SELECT string_agg(id || ':' || k, ',' ORDER BY id) "
                                     f"FROM {scan.format(old_md)} AS t(id bigint, k bigint)"),
            "promo double": _rows(dsn, "SELECT string_agg(f::text, ',' ORDER BY id) "
                                       f"FROM {scan.format(old_md)} AS t(id bigint, f double precision)"),
            "promo decimal": _rows(dsn, "SELECT string_agg(d::text, ',' ORDER BY id) "
                                        f"FROM {scan.format(old_md)} AS t(id bigint, d numeric(20,2))"),
            "promo mixed": _rows(dsn, f"SELECT {prow} FROM {scan.format(mixed_md)} {pcols} ORDER BY id"),
            "promo eq": _rows(dsn, "SELECT string_agg(id || ':' || k, ',' ORDER BY id) "
                                   f"FROM {scan.format(eq_md)} AS t(id bigint, k bigint)"),
            "promo eq unprojected": _rows(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) "
                                               f"FROM {scan.format(eq_md)} AS t(id bigint)"),
            "promo fdw mixed": _rows(dsn, f"SELECT {prow} FROM promo_mixed ORDER BY id"),
            "promo fdw eq": _rows(dsn, "SELECT string_agg(id || ':' || k, ',' ORDER BY id) FROM promo_eq"),
            "promo narrow int": _refused_by(dsn, f"SELECT * FROM {scan.format(mixed_md)} AS t(k int)",
                                            'has a physical type incompatible with output column "k"'),
            "promo narrow real": _refused_by(dsn, f"SELECT * FROM {scan.format(mixed_md)} AS t(f real)",
                                             'has a physical type incompatible with output column "f"'),
            "promo date": _refused_by(dsn, f"SELECT * FROM {scan.format(old_md)} AS t(dt bigint)",
                                      'has a physical type incompatible with output column "dt"'),
            "promo by id": _rows(dsn, "SELECT string_agg(k::text, ',' ORDER BY k) FROM "
                                      f"pgcolumnar.read_parquet('{pdata}/old2.parquet', ARRAY[2]) AS t(k bigint)"),
            "promo positional": _refused_by(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{pdata}/old2.parquet') "
                                                 "AS t(id bigint, k bigint, f real, d numeric(10,2), dt date)",
                                            'physical type is not compatible with target column "k"'),
        })
        # #2087: format-version 3 initial-default, which the reader cannot apply yet
        v3md, v3opt, v3eq = _v3_default_copies(md, eq_md)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE FOREIGN TABLE icescan_v3default (id bigint, qty int) "
                      f"SERVER ice_promo OPTIONS (metadata_path '{v3md}')")
        r.update({
            "v3 default": _refusal(dsn, f"SELECT id, qty FROM {scan.format(v3md)} AS t(id bigint, qty int) "
                                        "ORDER BY id", r'field "qty" has an initial-default', "refusal naming qty"),
            # qty first, so a refusal that skips the first output column fails
            "v3 optional default": _refusal(dsn, f"SELECT id, qty FROM {scan.format(v3opt)} "
                                                 "AS t(qty int, id bigint) ORDER BY id",
                                            r'field "qty" has an initial-default', "refusal naming qty"),
            "v3 other columns": _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM {scan.format(v3md)} "
                                           "AS t(id bigint, region text, amount int) ORDER BY id"),
            "v3 eq default": _refusal(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) "
                                           f"FROM {scan.format(v3eq)} AS t(id bigint)",
                                      r'field "k" has an initial-default', "refusal naming k"),
            "v3 fdw default": _refusal(dsn, "SELECT id, qty FROM icescan_v3default ORDER BY id",
                                       r'field "qty" has an initial-default', "refusal naming qty"),
        })
        v3str, v3bool, v3null, v3first, v3eqother = _v3_more_copies(v3md, eq_md)
        r.update({
            "v3 string default": _refusal(dsn, f"SELECT id, qty FROM {scan.format(v3str)} "
                                               "AS t(id bigint, qty text) ORDER BY id",
                                          r'field "qty" has an initial-default', "refusal naming qty"),
            "v3 boolean default": _refusal(dsn, f"SELECT id, qty FROM {scan.format(v3bool)} "
                                                "AS t(id bigint, qty boolean) ORDER BY id",
                                           r'field "qty" has an initial-default', "refusal naming qty"),
            "v3 null default": _rows(dsn, "SELECT id || '|' || coalesce(qty::text, 'null') "
                                          f"FROM {scan.format(v3null)} AS t(id bigint, qty int) ORDER BY id"),
            "v3 default first": _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM {scan.format(v3first)} "
                                           "AS t(id bigint, region text, amount int) ORDER BY id"),
            "v3 eq other default": _rows(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) "
                                              f"FROM {scan.format(v3eqother)} AS t(id bigint)"),
        })
        v3eqstr, v3write = _v3_round3_copies(v3md, dmd)
        r.update({
            "v3 eq string default": _refusal(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) "
                                                  f"FROM {scan.format(v3eqstr)} AS t(id bigint)",
                                             r'field "region" has an initial-default', "refusal naming region"),
            "v3 write default only": _rows(dsn, "SELECT id || '|' || coalesce(qty::text, 'null') "
                                                f"FROM {scan.format(v3write)} AS t(id bigint, qty int) ORDER BY id"),
        })
        v3eqint, v2opt = _v3_round4_copies(v3opt, dmd)
        r.update({
            "v3 eq int default": _refusal(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) "
                                               f"FROM {scan.format(v3eqint)} AS t(id bigint)",
                                          r'field "grp" has an initial-default', "refusal naming grp"),
            "v2 default": _refusal(dsn, "SELECT id, qty FROM "
                                        f"{scan.format(v2opt)} AS t(id bigint, qty int) ORDER BY id",
                                   r'field "qty" has an initial-default', "refusal naming qty"),
        })
        yield r
    finally:
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            for role in (UNPRIV, READER):
                a.execute(f"DROP ROLE IF EXISTS {role}")


def _declined(isc, expect):
    if isc is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the iceberg warehouse fixture is missing")
    return True


def test_the_data_file_carries_field_ids(isc, expect):
    if isc is None:
        expect.text("missing", "present", "iceberg warehouse data files are missing")
        return
    expect.require_module("json", name="python3 is needed")
    expect.text(_lines(isc["ids"]), "1,2,3", "premise: the data file carries field ids (id=1,region=2,amount=3)")


def test_iceberg_scan_reads_the_current_snapshots_rows(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["full"]), "1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50",
                "iceberg_scan reads the current snapshot's rows")


def test_iceberg_scan_projects_and_reorders_by_field_id(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["reorder"]), "10|1\n20|2\n30|3\n40|4\n50|5",
                "iceberg_scan projects and reorders by field id (amount, id)")


def test_iceberg_scan_reads_a_subset(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["subset"]), "eu,eu,us,us,us", "iceberg_scan reads a subset (region only)")


def test_a_renamed_column_still_reads_by_field_id(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["renamed"]), "10,20,30,40,50",
                "a column renamed in the schema still reads by field id (amt2 = amount)")


def test_an_output_column_not_in_the_schema_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["nosuch"]), "42703", "an output column not in the schema is refused (42703)")


def test_iceberg_scan_type_refusal_names_iceberg_scan(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["money"], 'output column "amount" has type money, which pgcolumnar.iceberg_scan does not support',
                "iceberg_scan's type refusal names iceberg_scan")


def test_an_unprivileged_role_is_refused_the_scan(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["unpriv"]), "42501", "an unprivileged role is refused (42501)")


def test_a_pg_read_server_files_member_may_scan(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["reader"]), "5", "a pg_read_server_files member who is not superuser may scan")


def test_iceberg_scan_refuses_eu_as_varchar_1(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["tm v1"]), "22001", "typmod: iceberg_scan refuses 'eu' as varchar(1) (22001)")


def test_iceberg_scan_reads_region_as_varchar_2_unchanged(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["tm v2"]), "eu,eu,us,us,us", "typmod: iceberg_scan reads region as varchar(2) unchanged")


def test_iceberg_scan_drops_excess_trailing_spaces_as_varchar_2(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["tm pad"]), "1:eu:2,2:us:2",
                "typmod: iceberg_scan drops the excess trailing spaces of 'eu  ' as varchar(2)")


def test_iceberg_scan_rounds_to_whole_seconds_as_timestamp_0(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["tm frac"]), "1|2021-03-01 01:00:00,2|2021-03-01 05:00:00",
                "typmod: iceberg_scan rounds 00:59:59.7 to 01:00:00 as timestamp(0)")


def _plain_snapshot(isc):
    return _lines(isc["plain snap"]) or "no snapshot from the plain file"


def test_iceberg_current_snapshot_reads_a_gz_metadata_json_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_infix snap"]), _plain_snapshot(isc),
                       "iceberg_current_snapshot reads a .gz.metadata.json file as it reads the plain file")


def test_iceberg_current_snapshot_reads_a_metadata_json_gz_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_suffix snap"]), _plain_snapshot(isc),
                       "iceberg_current_snapshot reads a .metadata.json.gz file as it reads the plain file")


def test_iceberg_scan_reads_a_table_whose_metadata_is_gz_metadata_json(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_infix rows"]), ROWS5,
                       "iceberg_scan reads the rows of a table whose metadata is .gz.metadata.json")


def test_iceberg_scan_reads_a_table_whose_metadata_is_metadata_json_gz(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_suffix rows"]), ROWS5,
                       "iceberg_scan reads the rows of a table whose metadata is .metadata.json.gz")


def test_a_gzip_metadata_file_past_the_cap_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["gz_bomb"]), "54000 past the cap",
                       "a gzip metadata file that inflates past the 64 MiB cap is refused (54000)")


def test_a_cut_short_gzip_metadata_file_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["gz_trunc"]), "XX001 naming gzip",
                       "a cut-short gzip metadata file is refused as corrupt gzip (XX001)")


def test_iceberg_current_snapshot_reads_a_two_member_gzip_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_multi snap"]), _plain_snapshot(isc),
                       "iceberg_current_snapshot reads a gzip metadata file of two members as it reads the plain file")


def test_iceberg_current_snapshot_reads_a_zero_padded_gzip_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_padded snap"]), _plain_snapshot(isc),
                       "iceberg_current_snapshot reads a gzip metadata file followed by zero padding")


def test_iceberg_current_snapshot_reads_a_gzip_file_that_outgrows_the_first_buffer(isc, expect):
    # about 2 MB once inflated, from a few KiB on disk: the reader grows its first
    # buffer (8 times the file size, at least 64 KiB) several times
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_large snap"]), _plain_snapshot(isc),
                       "iceberg_current_snapshot reads a gzip metadata file that outgrows the first inflate buffer")


def test_iceberg_current_snapshot_reads_a_two_member_gzip_file_that_grows_in_its_second_member(isc, expect):
    # the metadata between two 1 MB pads, as two members split mid-text: the buffer
    # grows while the second member inflates
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["gz_grow snap"]), _plain_snapshot(isc),
                       "iceberg_current_snapshot reads a two-member gzip file that grows the buffer in its second member")


def test_an_az_metadata_path_is_refused_as_remote(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["az"]), "0A000 remote refusal",
                       "an az:// metadata path is refused as a remote URL it cannot read (0A000)")


def test_iceberg_scan_reads_a_lowercase_parquet_file_format(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["lower rows"]), ROWS5,
                       "iceberg_scan reads a data file whose file_format is lowercase parquet")


def test_iceberg_scan_refuses_a_data_file_format_that_is_not_parquet(isc, expect):
    # a data file_format that is not Parquet in any spelling is refused, not read as Parquet
    if _declined(isc, expect):
        return
    expect.text(str(isc["notpq"]), "0A000 naming PARQUET",
                       "iceberg_scan refuses a data file whose file_format is not Parquet (0A000)")


def test_iceberg_scan_reads_mixed_s3_and_s3a_paths(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["mixed rows"]), ROWS5,
                       "iceberg_scan reads a table whose location is s3:// and whose recorded paths are s3a://")


def test_iceberg_scan_reads_the_scalars_beside_nested_fields(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["nest scalar"]), "1,2,3,4,5",
                "iceberg_scan still reads the scalar columns beside nested fields")


def test_iceberg_scan_refuses_a_struct_column_declared_text(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["nest struct"], "0A000 (struct)", "iceberg_scan refuses a struct column declared text (0A000)")


def test_iceberg_scan_refuses_a_list_column_declared_int4(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["nest list"], "0A000 (list)", "iceberg_scan refuses a list column declared int4 (0A000)")


def test_iceberg_scan_refuses_a_map_column_declared_int8(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["nest map"], "0A000 (map)", "iceberg_scan refuses a map column declared int8 (0A000)")


def test_an_iceberg_foreign_table_refuses_a_struct_column_declared_text(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(isc["fdw struct"], "0A000 (struct)",
                "a pgcolumnar_iceberg foreign table refuses a struct column declared text (0A000)")


OLD_ROWS = ("1|-2147483648|1.5|1.25\n2|-1|-2.25|-3.50\n3|0|0.5|0.00\n4|2|1024.75|99999999.99\n"
            "5|2147483647|3.4028234663852886e+38|-0.01\n6|7|-0.125|10.10\n7|2|2|2.00")
NEW_ROWS = "8|5000000000|3.25|123456789012345678.90\n9|2|0.1|-1.00"


def test_the_promotion_fixture_stores_the_old_and_new_physical_types(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo types"]),
                "new:k:bigint,new:f:double precision,new:d:numeric(20,2),old:k:integer,old:f:real,"
                "old:d:numeric(10,2),old2:k:integer,old2:f:real,old2:d:numeric(10,2)",
                "premise: the old files store k, f, d as INT32, FLOAT and decimal(10,2), "
                "the new file as INT64, DOUBLE and decimal(20,2)")


def test_a_promoted_int_column_reads_old_files_as_bigint(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo long"]), "1:-2147483648,2:-1,3:0,4:2,5:2147483647,6:7,7:2",
                "a promoted int -> long column reads the pre-promotion files as bigint")


def test_a_promoted_float_column_reads_old_files_as_double(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo double"]), "1.5,-2.25,0.5,1024.75,3.4028234663852886e+38,-0.125,2",
                "a promoted float -> double column reads the pre-promotion files as double precision")


def test_a_promoted_decimal_column_reads_old_files(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo decimal"]), "1.25,-3.50,0.00,99999999.99,-0.01,10.10,2.00",
                "a promoted decimal(10,2) -> decimal(20,2) column reads the pre-promotion files")


def test_old_and_new_files_bind_to_the_promoted_types(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo mixed"]), OLD_ROWS + "\n" + NEW_ROWS,
                "old and new files both bind to the promoted types in one snapshot")


def test_an_equality_delete_on_a_promoted_column_applies(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo eq"]), "1:-2147483648,2:-1,5:2147483647,6:7,8:5000000000",
                "an equality delete on a promoted column applies to old and new files")


def test_an_equality_delete_on_an_unprojected_promoted_column_applies(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo eq unprojected"]), "1,2,5,6,8",
                "an equality delete on a promoted column applies when that column is not projected")


def test_a_foreign_table_reads_the_promoted_types(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo fdw mixed"]), OLD_ROWS + "\n" + NEW_ROWS,
                "a pgcolumnar_iceberg foreign table reads old and new files as the promoted types")


def test_a_foreign_table_applies_an_equality_delete_on_a_promoted_column(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo fdw eq"]), "1:-2147483648,2:-1,5:2147483647,6:7,8:5000000000",
                "a pgcolumnar_iceberg foreign table applies an equality delete on a promoted column")


def test_a_column_declared_int_over_an_int64_file_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo narrow int"]), "42804",
                "a column declared int over a post-promotion INT64 file is refused (42804)")


def test_a_column_declared_real_over_a_double_file_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo narrow real"]), "42804",
                "a column declared real over a post-promotion DOUBLE file is refused (42804)")


def test_a_date_column_declared_bigint_is_refused(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo date"]), "42804", "a date column declared bigint is refused, not widened (42804)")


def test_read_parquet_by_field_id_widens_an_int32_column(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["promo by id"]), "2,7", "read_parquet by field id reads an INT32 column as bigint")


def test_positional_read_parquet_keeps_its_exact_type_map(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["promo positional"]), "42804",
                "positional read_parquet still refuses bigint over an INT32 column (42804)")


def test_iceberg_scan_refuses_a_v3_column_with_an_initial_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 default"]), "0A000 (refusal naming qty)",
                "iceberg_scan refuses a v3 column whose initial-default it cannot apply (0A000)")


def test_iceberg_scan_refuses_an_optional_v3_column_with_an_initial_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 optional default"]), "0A000 (refusal naming qty)",
                "iceberg_scan refuses an optional v3 column whose initial-default it cannot apply (0A000)")


def test_iceberg_scan_reads_the_v3_columns_without_an_initial_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["v3 other columns"]), ROWS5,
                "iceberg_scan still reads the v3 table's columns that have no initial-default")


def test_iceberg_scan_refuses_an_equality_delete_on_a_v3_column_with_an_initial_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 eq default"]), "0A000 (refusal naming k)",
                "iceberg_scan refuses an equality delete on a v3 column with an initial-default (0A000)")


def test_a_foreign_table_refuses_a_v3_column_with_an_initial_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 fdw default"]), "0A000 (refusal naming qty)",
                "a pgcolumnar_iceberg foreign table refuses a v3 column with an initial-default (0A000)")


def test_iceberg_scan_refuses_a_v3_string_column_with_an_empty_initial_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 string default"]), "0A000 (refusal naming qty)",
                "iceberg_scan refuses an optional v3 string column whose initial-default is empty (0A000)")


def test_iceberg_scan_refuses_a_v3_boolean_column_with_a_false_initial_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 boolean default"]), "0A000 (refusal naming qty)",
                "iceberg_scan refuses an optional v3 boolean column whose initial-default is false (0A000)")


def test_iceberg_scan_reads_a_v3_column_with_a_null_initial_default_as_null(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["v3 null default"]), "1|null\n2|null\n3|null\n4|null\n5|null",
                "iceberg_scan reads an optional v3 column whose initial-default is null as null")


def test_iceberg_scan_reads_other_columns_when_the_defaulted_v3_field_comes_first(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["v3 default first"]), ROWS5,
                "iceberg_scan still reads the other columns when the v3 field with an initial-default comes first")


def test_iceberg_scan_applies_eq_deletes_past_an_earlier_defaulted_v3_field(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["v3 eq other default"]), "1,2,5,6,8",
                "iceberg_scan still applies equality deletes when an earlier, uncompared v3 field has an initial-default")


def test_iceberg_scan_refuses_an_eq_delete_on_a_v3_string_column_with_a_string_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 eq string default"]), "0A000 (refusal naming region)",
                "iceberg_scan refuses an equality delete on a v3 string column whose initial-default is a string (0A000)")


def test_iceberg_scan_reads_a_v3_column_with_only_a_write_default_as_null(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(_lines(isc["v3 write default only"]), "1|null\n2|null\n3|null\n4|null\n5|null",
                "iceberg_scan reads an optional v3 column that has only a write-default as null")


def test_iceberg_scan_refuses_an_eq_delete_on_a_v3_int_column_with_a_zero_default(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v3 eq int default"]), "0A000 (refusal naming grp)",
                "iceberg_scan refuses an equality delete on a v3 int column whose initial-default is 0 (0A000)")


def test_iceberg_scan_refuses_an_initial_default_in_a_v2_metadata_file(isc, expect):
    if _declined(isc, expect):
        return
    expect.text(str(isc["v2 default"]), "0A000 (refusal naming qty)",
                "iceberg_scan refuses an initial-default in a format-version 2 metadata file (0A000)")

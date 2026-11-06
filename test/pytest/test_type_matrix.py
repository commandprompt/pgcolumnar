"""Types the differential matrix never stored, against heap (#1341). The pytest twin
of `test/type_matrix.sh`, every check by the shell's name.

The encoder has fast paths for fixed widths 1, 2, 4 and 8; every other width and every
varlena takes a generic path, and a zone map is built for each column whose type has a
btree ordering. These columns cover widths 6, 12, 16 and 64 by reference, the 1-byte
"char", time, money, inet, cidr, bit, varbit, tsvector, xml, json, an enum, a composite,
a domain, and numeric, timestamp and date special values. 12000 rows in row groups of
1000, each column NULL on its own pattern, generated once into heap and copied.

Each comparison is of the rows themselves (`expect.row_set`), so a failure names the rows
that differ. The range bounds are the values heap holds at ids 3005 and 9005, written as
literals so the columnar scan can prune row groups by zone map (not 3001, which holds n's
NaN). Each range arm also says whether the zone maps skipped chunk groups, from EXPLAIN
ANALYZE; four that measure 0 are pinned at 0 with the reason. timetz's zone offset varies
with the row, so its 12-byte width is used past byte 8.
"""
import re

import psycopg

COLS = """id int, tz timetz, mac macaddr, mac8 macaddr8, nm name, pt point, ch "char",
      tm time, mo money, ip inet, cd cidr, bt bit(16), vb varbit, tsv tsvector, x xml,
      j json, e tm_mood, cmp tm_pair, dom tm_pos, n numeric, tsi timestamp, d date"""

LOAD = """SELECT g,
  CASE WHEN g % 7 = 0 THEN NULL ELSE ((('00:00'::time + (g * 7) * interval '1 s')::text || '+' || lpad((g % 13)::text, 2, '0'))::timetz) END,
  CASE WHEN g % 11 = 0 THEN NULL ELSE ('08:00:2b:' || lpad(to_hex(g / 256 % 256), 2, '0') || ':' || lpad(to_hex(g % 256), 2, '0') || ':01')::macaddr END,
  CASE WHEN g % 13 = 0 THEN NULL ELSE ('08:00:2b:01:02:03:' || lpad(to_hex(g / 256 % 256), 2, '0') || ':' || lpad(to_hex(g % 256), 2, '0'))::macaddr8 END,
  CASE WHEN g % 17 = 0 THEN NULL ELSE ('name_' || lpad(g::text, 6, '0'))::name END,
  CASE WHEN g % 19 = 0 THEN NULL ELSE point(g, -g) END,
  CASE WHEN g % 23 = 0 THEN NULL ELSE chr(65 + g % 26)::"char" END,
  CASE WHEN g % 29 = 0 THEN NULL ELSE ('00:00'::time + g * interval '7 s') END,
  CASE WHEN g % 31 = 0 THEN NULL ELSE (g * 1.25)::numeric::money END,
  CASE WHEN g % 37 = 0 THEN NULL ELSE ('10.' || g / 256 % 256 || '.' || g % 256 || '.' || g % 7 || '/32')::inet END,
  CASE WHEN g % 41 = 0 THEN NULL ELSE ('10.' || g / 256 % 256 || '.' || g % 256 || '.0/24')::cidr END,
  CASE WHEN g % 43 = 0 THEN NULL ELSE g::bit(16) END,
  CASE WHEN g % 47 = 0 THEN NULL ELSE (g::bit(16) || (g % 7)::bit(3))::varbit END,
  CASE WHEN g % 53 = 0 THEN NULL ELSE to_tsvector('simple', 'word' || g || ' common term' || g % 9) END,
  CASE WHEN g % 59 = 0 THEN NULL ELSE ('<r id="' || g || '">' || md5(g::text) || '</r>')::xml END,
  CASE WHEN g % 61 = 0 THEN NULL ELSE json_build_object('g', g, 's', md5(g::text)) END,
  CASE WHEN g % 67 = 0 THEN NULL ELSE (enum_range(NULL::tm_mood))[1 + g % 4] END,
  CASE WHEN g % 71 = 0 THEN NULL ELSE ROW(g, 'p' || g)::tm_pair END,
  CASE WHEN g % 73 = 0 THEN NULL ELSE (g)::tm_pos END,
  CASE WHEN g % 79 = 0 THEN NULL WHEN g % 1000 = 1 THEN 'NaN'::numeric WHEN g % 1000 = 2 THEN 'Infinity'::numeric
       WHEN g % 1000 = 3 THEN '-Infinity'::numeric ELSE g / 7.0 END,
  CASE WHEN g % 83 = 0 THEN NULL WHEN g = 500 THEN 'infinity'::timestamp WHEN g = 501 THEN '-infinity'::timestamp
       ELSE '2026-01-01'::timestamp + g * interval '1 min' END,
  CASE WHEN g % 89 = 0 THEN NULL WHEN g = 600 THEN 'infinity'::date WHEN g = 601 THEN '-infinity'::date
       ELSE '2026-01-01'::date + g END
  FROM generate_series(1, 12000) g"""

ALL = ("id, tz::text, mac::text, mac8::text, nm::text, pt::text, ch::text, tm::text, mo::text, ip::text, "
       "cd::text, bt::text, vb::text, tsv::text, x::text, j::text, e::text, cmp::text, dom::text, n::text, "
       "tsi::text, d::text")


def _rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _both(conn, sql):
    return _rows(conn, sql.replace("%T", "t_col")), _rows(conn, sql.replace("%T", "t_heap"))


NO_PRUNE = {
    "ch": "every row group holds every value, since the column cycles through 26 letters",
    "e": "every row group holds every value, since the column cycles through the 4 labels",
    "n": "every row group holds NaN and both infinities, so each zone map spans every value",
    # cd was pinned here until #1433, when a cidr key was dropped as cross-type
}


def _removed(conn, sql):
    plan = "\n".join(r[0] for r in _rows(conn, "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                                         + sql.replace("%T", "t_col")))
    m = re.search(r"Columnar Chunk Groups Removed by Filter: (\d+)", plan)
    return m.group(1) if m else "none"


def test_types_the_matrix_never_stored_answer_as_heap(pgc_conn, expect):
    # xml needs a server built with libxml; without it every arm after the load would
    # fail for that reason, so this says so first.
    try:
        xml_ok = "yes" if _rows(pgc_conn, "SELECT '<a/>'::xml IS NOT NULL")[0][0] else "no"
    except psycopg.Error as exc:
        xml_ok = f"no ({str(exc).splitlines()[0]})"
    expect.text(xml_ok, "yes", "premise: the server supports xml (built with libxml)")
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TYPE tm_mood AS ENUM ('sad', 'ok', 'happy', 'elated')")
        cur.execute("CREATE TYPE tm_pair AS (a int, b text)")
        cur.execute("CREATE DOMAIN tm_pos AS int CHECK (VALUE > 0)")
        cur.execute(f"CREATE TABLE t_heap ({COLS})")
        cur.execute(f"CREATE TABLE t_col ({COLS}) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000)")
        cur.execute(f"INSERT INTO t_heap {LOAD}")
        cur.execute("INSERT INTO t_col SELECT * FROM t_heap")
    groups = _rows(pgc_conn, "SELECT count(*) FROM pgcolumnar.row_group "
                             "WHERE storage_id = pgcolumnar.get_storage_id('t_col')")[0][0]
    n_heap = _rows(pgc_conn, "SELECT count(*) FROM t_heap")[0][0]
    n_col = _rows(pgc_conn, "SELECT count(*) FROM t_col")[0][0]
    expect.text(f"{n_heap}|{n_col}|{'many' if groups >= 12 else groups}", "12000|12000|many",
                "premise: both tables hold 12000 rows, the columnar one in at least twelve row groups")

    got, want = _both(pgc_conn, f"SELECT {ALL} FROM %T")
    expect.row_set(got, want, "every row, as text, is heap's")
    counts = ", ".join(f"count({c}), count(*) FILTER (WHERE {c} IS NULL)"
                       for c in "tz mac mac8 nm pt ch tm mo ip cd bt vb tsv x j e cmp dom n tsi d".split())
    got, want = _both(pgc_conn, f"SELECT {counts} FROM %T")
    expect.row_set(got, want, "each column's count and NULL count is heap's")

    for c in ("tz", "mac", "mac8", "nm", "ch", "tm", "mo", "ip", "cd", "bt", "vb", "e", "dom", "n", "tsi", "d"):
        # format_type keeps the typmod: pg_typeof would make bt's bounds bit(1).
        ty = _rows(pgc_conn, f"SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                             f"WHERE attrelid = 't_heap'::regclass AND attname = '{c}'")[0][0]
        lo, hi = (_rows(pgc_conn, f"SELECT quote_literal({c}::text) || '::' || '{ty}' "
                                  f"FROM t_heap WHERE id = {i}")[0][0] for i in (3005, 9005))
        sql = f"SELECT id FROM %T WHERE {c} BETWEEN least({lo}, {hi}) AND greatest({lo}, {hi})"
        # Heap's answer must not be empty: wrong bounds would make both answers empty.
        n = _rows(pgc_conn, f"SELECT count(*) FROM ({sql.replace('%T', 't_heap')}) x")[0][0]
        expect.text("yes" if n > 0 else f"no ({n} rows)", "yes",
                    f"a range on {c} holds rows on heap, so its comparison reads something")
        got, want = _both(pgc_conn, sql)
        expect.row_set(got, want, f"a range on {c} finds heap's rows")
        r = _removed(pgc_conn, sql)
        if c in NO_PRUNE:
            expect.text(r, "0", f"a range on {c} skips no chunk groups: {NO_PRUNE[c]}")
        else:
            expect.text("skips" if r.isdigit() and int(r) >= 1 else f"removed {r}", "skips",
                        f"a range on {c} lets the zone maps skip chunk groups")
    got, want = _both(pgc_conn, "SELECT id FROM %T WHERE j::text = (SELECT j::text FROM t_heap WHERE id = 4242)")
    expect.row_set(got, want, "equality on json, as text, finds heap's rows")
    got, want = _both(pgc_conn, "SELECT id FROM %T WHERE pt ~= point(4242, -4242)")
    expect.row_set(got, want, "point equality (~=) finds heap's rows")
    got, want = _both(pgc_conn, "SELECT id FROM %T WHERE tsv @@ to_tsquery('simple', 'term3')")
    expect.row_set(got, want, "a tsvector match finds heap's rows")
    got, want = _both(pgc_conn, "SELECT min(n)::text, max(n)::text, count(*) FILTER (WHERE n = 'NaN'), "
                                "count(*) FILTER (WHERE n = 'Infinity'), count(*) FILTER (WHERE n = '-Infinity'), "
                                "min(tsi)::text, max(tsi)::text, min(d)::text, max(d)::text FROM %T")
    expect.row_set(got, want, "the aggregates over numeric NaN and Infinity and timestamp and date infinity are heap's")
    got, want = _both(pgc_conn, "SELECT (cmp).a, (cmp).b, e::text, e > 'ok' FROM %T WHERE id % 97 = 0")
    expect.row_set(got, want, "the composite's fields and the enum's order read back as heap's")

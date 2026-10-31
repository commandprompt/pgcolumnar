#!/usr/bin/env bash
#
# pgColumnar Parquet export suite (gap 27, piece 2).
#
# pgcolumnar.export_parquet(rel, path) writes a Parquet file. This suite exports a
# mixed-type columnar table (NULLs, boundary integers, NaN/Inf floats,
# empty/unicode text, bytea) that mirrors a heap table, reads it back, and
# asserts every value equals the heap oracle. pyarrow provides exact typed
# values for the comparison; if the DuckDB CLI is present it is used as a second
# independent reader (row counts, a non-null count and a decimal), and without it
# those four checks are skipped by name. Also covers multiple row groups, an
# empty table, and error cases.
#
# Requires pyarrow (python3-pyarrow); if it is not importable the suite skips
# with a note.
#
# Usage:  test/parquet_export.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; skipping Parquet export verification"
fi

# expect_refusal NAME SQL MESSAGE -- SQL must fail with MESSAGE in its error. Any
# error used to count, so a backend crash or an unrelated failure passed as the
# refusal (#1341): with the columnar check removed, exporting a heap failed for
# another reason, and with the type check removed the backend aborted, and both
# passed. psql's exit status says whether it failed; the message says why.
expect_refusal() {
	local name="$1" sql="$2" msg="$3" out got
	if out="$(psql_run "$sql" 2>&1)"; then
		got="succeeded"
	elif grep -qF -- "$msg" <<<"$out"; then
		got="refused: $msg"
	else
		got="other error: $(head -n1 <<<"$out")"
	fi
	check "$name" "$got" "refused: $msg"
}

# duckdb_skip NAME -- without the duckdb CLI, record the DuckDB check NAME as
# skipped, by name. The four checks used to be left out without a word, so a run
# without DuckDB counted four checks fewer and said nothing (#1341). The twin
# declines the same four by the same names. A skip, not check_unrunnable: the
# suites job installs no DuckDB CLI, and an unrunnable check makes the suite
# INCOMPLETE, which fails its major there. CI holds the four in the twin instead,
# where PGC_REQUIRE names duckdb and turns a decline into a failure.
duckdb_skip() {
	check_skip "$1" "SKIP  $1 (the duckdb CLI is not installed)" "duckdb CLI not installed"
}

PARQUET="$PGC_WORKDIR/t.parquet"
PGCSV="$PGC_WORKDIR/pg.csv"

# > PARQUET_ROWGROUP_ROWS (65536) rows to force multiple row groups, plus
# explicit edge-case rows.
make_pair "id int, a int2, b int4, c int8, d float4, e float8, f bool, g text, h bytea"
load_pair "
  SELECT s AS id, (s%30000-15000)::int2, s*2, s::int8*1000,
         (s::float4/3), (s::float8/7), (s%2=0), 'r'||s,
         decode(lpad(to_hex(s),6,'0'),'hex')
  FROM generate_series(1,80000) s
  UNION ALL VALUES
   (100001, NULL::int2, NULL::int4, NULL::int8, NULL::float4, NULL::float8, NULL::bool, NULL::text, NULL::bytea),
   (100002, (-32768)::int2, (-2147483648)::int4, (-9223372036854775808)::int8, '-Infinity'::float4, 'NaN'::float8, false, ''::text, '\\x'::bytea),
   (100003, (32767)::int2, (2147483647)::int4, (9223372036854775807)::int8, 'Infinity'::float4, (3.14)::float8, true, 'unicode q'::text, '\\x00ff10'::bytea)
"

echo "-- export and verify against heap oracle via pyarrow"
rows_written="$(q "SELECT pgcolumnar.export_parquet('t_col', '$PARQUET');")"
check "export_parquet rows written" "$rows_written" "80003"

psql_run "COPY (SELECT id, a, b, c, (d::float8), e, f, g, encode(h,'hex')
                FROM t_heap ORDER BY id)
          TO '$PGCSV' WITH (FORMAT csv, NULL E'\\\\N')"

pyres="$(python3 - "$PARQUET" "$PGCSV" <<'PY'
import sys, csv, math
import pyarrow.parquet as pq
tbl = pq.read_table(sys.argv[1])
cols=['id','a','b','c','d','e','f','g','h']
if tbl.schema.names != cols:
    print("SCHEMA_NAMES_MISMATCH"); sys.exit(0)
ad=tbl.to_pydict()
ar={}
for i in range(tbl.num_rows):
    row=tuple(ad[c][i] for c in cols); ar[row[0]]=row
def pf(x): return None if x==r'\N' else float(x)
pg={}
for r in csv.reader(open(sys.argv[2],newline='')):
    i,a,b,c,d,e,fb,g,h=r
    pg[int(i)]=(int(i),None if a==r'\N' else int(a),None if b==r'\N' else int(b),
        None if c==r'\N' else int(c),pf(d),pf(e),None if fb==r'\N' else (fb=='t'),
        None if g==r'\N' else g,None if h==r'\N' else bytes.fromhex(h))
def feq(x,y):
    if isinstance(x,float) or isinstance(y,float):
        if x is None or y is None: return x is y
        if math.isnan(x) and math.isnan(y): return True
        return x==y
    return x==y
# The ids the generator wrote, so that two empty readings cannot agree (#1341).
ids=set(range(1,80001))|{100001,100002,100003}
# And the file's own row count: rows are compared by id, so a row written twice
# would otherwise collapse into one.
if set(pg)!=ids or set(ar)!=ids or tbl.num_rows!=len(ids):
    print("ROWS heap %d parquet %d, not the generator's 80003"%(len(pg),tbl.num_rows)); sys.exit(0)
ok = all(all(feq(a,b) for a,b in zip(ar[k],pg[k])) for k in pg)
print("MATCH" if ok else "MISMATCH")
PY
)"
check "parquet values match heap oracle" "$pyres" "MATCH"

sch="$(python3 - "$PARQUET" <<'PY'
import sys, pyarrow.parquet as pq
s=pq.read_table(sys.argv[1]).schema
print(",".join(f"{f.name}:{f.type}" for f in s))
PY
)"
check "parquet schema" "$sch" "id:int32,a:int16,b:int32,c:int64,d:float,e:double,f:bool,g:string,h:binary"

# DuckDB as a second, independent reader.
if command -v duckdb >/dev/null 2>&1; then
	dcount="$(duckdb -noheader -list -c "SELECT count(*) FROM read_parquet('$PARQUET');" 2>/dev/null | tr -d '[:space:]')"
	check "duckdb row count" "$dcount" "80003"
	dsum_d="$(duckdb -noheader -list -c "SELECT count(b) FROM read_parquet('$PARQUET');" 2>/dev/null | tr -d '[:space:]')"
	# The generator's count, 80000 rows plus the two edge rows with a b: t_col is
	# the table under test, so its own count agreed with whatever it lost, and with
	# an empty read on both sides (#1341).
	check_num "duckdb non-null count(b)" "$dsum_d" "80002"
else
	duckdb_skip "duckdb row count"
	duckdb_skip "duckdb non-null count(b)"
fi

# Empty table.
echo "-- empty table"
psql_run "CREATE TABLE t_empty (a int, b text) USING pgcolumnar;"
n0="$(q "SELECT pgcolumnar.export_parquet('t_empty', '$PGC_WORKDIR/e.parquet');")"
check "empty export rows written" "$n0" "0"
er="$(python3 - "$PGC_WORKDIR/e.parquet" <<'PY'
import sys, pyarrow.parquet as pq
t=pq.read_table(sys.argv[1]); print(t.num_rows, ",".join(t.schema.names))
PY
)"
check "empty parquet readable" "$er" "0 a,b"

# Errors.
echo "-- argument validation"
expect_refusal "reject non-columnar table" "SELECT pgcolumnar.export_parquet('t_heap', '$PGC_WORKDIR/x.parquet');" \
	"is not a columnar table"
psql_run "CREATE TABLE t_un (a int, p point) USING pgcolumnar;"
psql_run "INSERT INTO t_un VALUES (1, '(1,2)');"
expect_refusal "reject unsupported type" "SELECT pgcolumnar.export_parquet('t_un', '$PGC_WORKDIR/x.parquet');" \
	'column "p" has type point, which columnar parquet export does not support'

# ---------------------------------------------------------------------------
# Extended type coverage: date, time, timestamp(+tz), uuid, numeric(p,s) as
# DECIMAL, unconstrained numeric as text, json, jsonb. Verified against a heap
# oracle rendered to the canonical form each type maps to. Parquet's
# TIMESTAMP_MICROS is UTC-normalized, so timestamps are compared by their raw
# microsecond value rather than by schema. Non-finite dates/timestamps and NaN
# numerics export as null.
# ---------------------------------------------------------------------------
echo "-- extended type coverage"
EXT="$PGC_WORKDIR/ext.parquet"
EXTCSV="$PGC_WORKDIR/ext.csv"
psql_run "CREATE TABLE t_ext_heap (id int, dt date, tm time, ts timestamp,
          tz timestamptz, u uuid, num numeric(20,4), numun numeric,
          j json, jb jsonb);"
psql_run "CREATE TABLE t_ext_col (LIKE t_ext_heap) USING pgcolumnar;"
psql_run "INSERT INTO t_ext_heap VALUES
  (1,'2021-03-04','12:34:56.789012','2021-03-04 12:34:56.789012',
     '2021-03-04 12:34:56.789012+00','11111111-2222-3333-4444-555555555555',
     12345.6789, 9999999999999999.1234, '{\"a\": 1}', '{\"b\": 2}'),
  (2,'1970-01-01','00:00:00','2000-01-01 00:00:00','2000-01-01 00:00:00+00',
     '00000000-0000-0000-0000-000000000000', -0.0001, -1234567890.5,
     '[1, 2, 3]', '[3, 2, 1]'),
  (3, NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
  (4,'infinity','23:59:59.999999','infinity','-infinity',
     'ffffffff-ffff-ffff-ffff-ffffffffffff', 'NaN', 'NaN', 'null', 'true');"
psql_run "INSERT INTO t_ext_col SELECT * FROM t_ext_heap;"

extrows="$(q "SELECT pgcolumnar.export_parquet('t_ext_col', '$EXT');")"
check "extended export rows written" "$extrows" "4"

psql_run "COPY (SELECT id,
    CASE WHEN dt IS NULL OR NOT isfinite(dt) THEN NULL
         ELSE (dt - DATE '1970-01-01') END,
    CASE WHEN tm IS NULL THEN NULL
         ELSE (extract(epoch from tm)*1000000)::bigint END,
    CASE WHEN ts IS NULL OR NOT isfinite(ts) THEN NULL
         ELSE (extract(epoch from ts)*1000000)::bigint END,
    CASE WHEN tz IS NULL OR NOT isfinite(tz) THEN NULL
         ELSE (extract(epoch from tz)*1000000)::bigint END,
    CASE WHEN u IS NULL THEN NULL ELSE replace(u::text,'-','') END,
    CASE WHEN num IS NULL OR num = 'NaN'::numeric THEN NULL ELSE num::text END,
    CASE WHEN numun IS NULL THEN NULL ELSE numun::text END,
    j::text, jb::text
   FROM t_ext_heap ORDER BY id)
   TO '$EXTCSV' WITH (FORMAT csv, NULL E'\\\\N')"

extres="$(python3 - "$EXT" "$EXTCSV" <<'PY'
import sys, csv
import pyarrow as pa, pyarrow.parquet as pq
t = pq.read_table(sys.argv[1])
def col(n): return t.column(n)
def i(n): return col(n).cast(pa.int64()).to_pylist()
ids = col('id').to_pylist()
dt = col('dt').cast(pa.int32()).to_pylist()   # date32 casts to int32, not int64
tm = i('tm'); ts = i('ts'); tz = i('tz')
u  = [None if v is None else v.hex() for v in col('u').to_pylist()]
num= [None if v is None else str(v) for v in col('num').to_pylist()]
nun= [None if v is None else str(v) for v in col('numun').to_pylist()]
j  = col('j').to_pylist(); jb = col('jb').to_pylist()
ar = {}
for k in range(t.num_rows):
    ar[ids[k]] = (ids[k], dt[k], tm[k], ts[k], tz[k], u[k], num[k], nun[k], j[k], jb[k])
def N(x): return None if x==r'\N' else x
def I(x): return None if x==r'\N' else int(x)
pg = {}
for r in csv.reader(open(sys.argv[2],newline='')):
    pg[int(r[0])] = (int(r[0]), I(r[1]), I(r[2]), I(r[3]), I(r[4]),
                     N(r[5]), N(r[6]), N(r[7]), N(r[8]), N(r[9]))
# The ids the INSERT wrote, so that two empty readings cannot agree (#1341).
if set(pg)!={1,2,3,4} or set(ar)!={1,2,3,4} or t.num_rows!=4:
    print("ROWS heap %r parquet %r, not ids 1..4"%(sorted(pg),ids)); sys.exit(0)
print("MATCH" if ar==pg else "MISMATCH %r %r"%(ar,pg))
PY
)"
check "extended values match heap oracle" "$extres" "MATCH"

# Key type mappings visible in the Parquet schema.
exttypes="$(python3 - "$EXT" <<'PY'
import sys, pyarrow.parquet as pq
s=pq.read_table(sys.argv[1]).schema
d=dict((f.name,str(f.type)) for f in s)
print("%s|%s|%s|%s" % (d['dt'], d['tm'], d['u'], d['num']))
PY
)"
check "extended parquet types" "$exttypes" \
"date32[day]|time64[us]|fixed_size_binary[16]|decimal128(20, 4)"

if command -v duckdb >/dev/null 2>&1; then
	dnum="$(duckdb -noheader -list -c "SELECT num FROM read_parquet('$EXT') WHERE id=1;" 2>/dev/null | tr -d '[:space:]')"
	check "duckdb reads decimal" "$dnum" "12345.6789"
	dext="$(duckdb -noheader -list -c "SELECT count(*) FROM read_parquet('$EXT');" 2>/dev/null | tr -d '[:space:]')"
	check "duckdb extended row count" "$dext" "4"
else
	duckdb_skip "duckdb reads decimal"
	duckdb_skip "duckdb extended row count"
fi

# ---- row groups are cut by bytes as well as by rows (#1811) ------------------------
# The writer buffers each column of a row group in one allocation, and PostgreSQL caps
# one allocation at 1 GiB. Groups were cut every 65536 rows only, so a table with more
# than 1 GiB of one column in a group failed with a raw string-buffer error.
# pgcolumnar.export_group_bytes is the byte cap; lowering it shows the cut at small
# scale. Each row buffers 10010 bytes (id: 4 + 1 level byte; s: 4-byte length + 10000 +
# 1 level byte), and the writer starts a new group before a row that would take the
# group past 65536. So 40 rows go into groups of 6, 6, 6, 6, 6, 6 and 4.
BYTESPQ="$PGC_WORKDIR/bytes.parquet"
BYTES_DIGEST="SELECT string_agg(id || ':' || length(s) || ':' || md5(s), ' ' ORDER BY id) FROM pq_bytes"
psql_run "CREATE TABLE pq_bytes (id int, s text) USING pgcolumnar;
          INSERT INTO pq_bytes SELECT g, repeat(chr(64 + g % 26), 10000) FROM generate_series(1, 40) g;"
# The SET and the export go as two commands, so the export runs even where the setting
# is unknown, and the reading is the file it wrote.
pq_bytes_rows="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
	-c "SET pgcolumnar.export_group_bytes = 65536" -c "SELECT pgcolumnar.export_parquet('pq_bytes', '$BYTESPQ')" 2>/dev/null | tail -1)"
pq_groups() {	# pq_groups FILE -> "rows|n1,n2,..." (rows per row group), or the read failure
	python3 - "$1" <<'PY'
import sys, pyarrow.parquet as pq
try:
    md = pq.ParquetFile(sys.argv[1]).metadata
    print("%d|%s" % (md.num_rows, ",".join(str(md.row_group(i).num_rows) for i in range(md.num_row_groups))))
except Exception as e:
    print("unreadable: %s" % e)
PY
}
pq_digest() {	# pq_digest FILE -> "id:length:md5 ..." by id, as BYTES_DIGEST reads the table
	python3 - "$1" <<'PY'
import sys, hashlib, pyarrow.parquet as pq
try:
    t = pq.read_table(sys.argv[1]).to_pydict()
    rows = sorted(zip(t["id"], t["s"]))
    print(" ".join("%d:%d:%s" % (i, len(s), hashlib.md5(s.encode()).hexdigest()) for i, s in rows))
except Exception as e:
    print("unreadable: %s" % e)
PY
}
check "a row group is cut at the byte cap: 40 rows of 10000 bytes under a 65536-byte cap make groups of 6,6,6,6,6,6,4" \
	"$pq_bytes_rows|$(pq_groups "$BYTESPQ")" "40|40|6,6,6,6,6,6,4"
check "and the byte-cut file reads back every row exactly, by id, length and md5" \
	"$(pq_digest "$BYTESPQ")" "$(q "$BYTES_DIGEST")"
# The cut counts the bytes the writer buffers for a row, not the bytes PostgreSQL stores
# (#1843 review). Under a 1024-byte cap: an int4 buffers 4 bytes and a level byte, so a
# group holds 204 rows (1020 bytes), not 205 (1025). An unconstrained numeric is written
# as text: 1e1000 stores in a few bytes but buffers 1006 (a 4-byte length, 1001 digits
# and a level byte), so each row is a group of its own. A numeric[] of three 1e300
# buffers 921 bytes (each element: 301 digits, a length and two level bytes).
pq_cut() {	# pq_cut TABLE FILE CAP -> "rows|rows|n1,n2,..." for the export under CAP
	local n
	n="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET pgcolumnar.export_group_bytes = $3" -c "SELECT pgcolumnar.export_parquet('$1', '$2')" 2>&1 | tail -1)"
	echo "$n|$(pq_groups "$2")"
}
psql_run "CREATE TABLE pq_w4 (id int) USING pgcolumnar;
          INSERT INTO pq_w4 SELECT g FROM generate_series(1, 1000) g;
          CREATE TABLE pq_wnum (n numeric) USING pgcolumnar;
          INSERT INTO pq_wnum SELECT 1e1000 FROM generate_series(1, 10);
          CREATE TABLE pq_wlist (a numeric[]) USING pgcolumnar;
          INSERT INTO pq_wlist SELECT ARRAY[1e300, 1e300, 1e300] FROM generate_series(1, 10);"
check "the byte cut counts an int4's level byte: 1000 int4 rows under a 1024-byte cap make groups of 204,204,204,204,184" \
	"$(pq_cut pq_w4 "$PGC_WORKDIR/w4.parquet" 1024)" "1000|1000|204,204,204,204,184"
check "the byte cut counts a numeric's text form: 10 rows of 1e1000 under a 1024-byte cap make 10 groups of 1" \
	"$(pq_cut pq_wnum "$PGC_WORKDIR/wnum.parquet" 1024)" "10|10|1,1,1,1,1,1,1,1,1,1"
check "the byte cut counts a list's elements as written: 10 rows of three 1e300 under a 1024-byte cap make 10 groups of 1" \
	"$(pq_cut pq_wlist "$PGC_WORKDIR/wlist.parquet" 1024)" "10|10|1,1,1,1,1,1,1,1,1,1"
# The writer converts a numeric or jsonb to text once, while it counts the row, and
# keeps the text for the append (#1843 review). Each kept text must reach its own
# value: scalars, list elements (with a NULL among them), and a composite's field.
psql_run "CREATE TYPE pq_tpair AS (x numeric, y text);
          CREATE TABLE pq_txt (id int, n numeric, a numeric[], j jsonb, c pq_tpair) USING pgcolumnar;
          INSERT INTO pq_txt SELECT g,
                 CASE WHEN g % 10 = 0 THEN NULL ELSE g::numeric / 7 END,
                 CASE WHEN g % 7 = 0 THEN NULL ELSE ARRAY[g::numeric / 3, NULL, g * 1e20] END,
                 jsonb_build_object('k', g, 'v', g::numeric / 9),
                 ROW(g::numeric / 11, 't' || g)::pq_tpair
            FROM generate_series(1, 300) g;"
TXT_DIGEST="SELECT string_agg(id || '|' || coalesce(n::text, 'NULL') || '|' || coalesce(array_to_string(a, ',', 'N'), 'NULL')
                   || '|' || j::text || '|' || (c).x::text || '|' || (c).y, ' ' ORDER BY id) FROM pq_txt"
pq_txt_rows="$(q "SELECT pgcolumnar.export_parquet('pq_txt', '$PGC_WORKDIR/txt.parquet')")"
pq_txt_read="$(python3 - "$PGC_WORKDIR/txt.parquet" <<'PY'
import sys
try:
    import pyarrow.parquet as pq
    t = pq.read_table(sys.argv[1]).to_pydict()
    def arr(v):
        return "NULL" if v is None else ",".join("N" if e is None else e for e in v)
    print(" ".join("%d|%s|%s|%s|%s|%s" % (i, "NULL" if n is None else n, arr(a), j, c["x"], c["y"])
                   for i, n, a, j, c in sorted(zip(t["id"], t["n"], t["a"], t["j"], t["c"]))))
except Exception as e:
    print("unreadable: %s" % e)
PY
)"
check "every numeric and jsonb text reaches its own value: 300 rows of numeric, numeric[], jsonb and a composite read back from the file as PostgreSQL prints them" \
	"$pq_txt_rows|$pq_txt_read" "300|$(q "$TXT_DIGEST")"
check "export_group_bytes refuses a value above its 256 MiB maximum" \
	"$(out="$(psql_run "SET pgcolumnar.export_group_bytes = 268435457" 2>&1)"; e="$(sed -n 's/^ERROR: *//p' <<<"$out" | head -1)"; echo "${e:-accepted}")" \
	'268435457 is outside the valid range for parameter "pgcolumnar.export_group_bytes" (1024 .. 268435456)'

# Over 1 GiB of one column at the default cap (#1811's repro). Opt-in, as
# large_values.sh's arm is: the table and its file are each about 1.1 GB. 2200 rows of
# 500000 bytes; each row buffers 500010 bytes, so a 256 MiB group holds 536 rows and
# the file has 5 groups (4 of 536, then 56).
OVERPQ="over 1 GiB of one column (2200 values of 500000 bytes) exports whole, in 5 row groups of at most 536 rows"
if [ "${PGC_RUN_OVER_1GB:-0}" = 1 ]; then
	HUGEPQ="$PGC_WORKDIR/huge.parquet"
	psql_run "CREATE TABLE pq_huge (id int, s text) USING pgcolumnar;
	          SELECT pgcolumnar.set_options('pq_huge', stripe_row_limit => 1000);
	          INSERT INTO pq_huge SELECT g, repeat('x', 500000) FROM generate_series(1, 2200) g;" >/dev/null
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET statement_timeout = '900s'" \
		-c "SELECT pgcolumnar.export_parquet('pq_huge', '$HUGEPQ')" 2>&1)"
	got="$(grep -m1 '^ERROR' <<<"$out" || tail -1 <<<"$out")"
	check "$OVERPQ" "$got|$(pq_groups "$HUGEPQ")" "2200|2200|536,536,536,536,56"
	rm -f "$HUGEPQ"
	psql_run "DROP TABLE pq_huge;"
else
	check_skip "$OVERPQ" "SKIP  $OVERPQ (set PGC_RUN_OVER_1GB=1; it needs several GB of memory and disk)" "opt-in heavy arm"
fi

# A row whose written form is far larger than its stored form, after a large row (#1843
# review). Opt-in for the same reason. Row 1 holds 260 MB of text. Row 2 holds 8200
# numerics of 1e100000: about 98 KB stored, but 820 MB as text. Counted by its stored
# size, row 2 joined row 1's group and the export failed with the raw string-buffer
# error. Counted as written, it goes in a group of its own.
WIDEPQN="a row of 8200 numerics of 1e100000 (98 KB stored, 820 MB as text) after a 260 MB row exports whole, in 2 row groups of 1"
if [ "${PGC_RUN_OVER_1GB:-0}" = 1 ]; then
	WIDEPQ="$PGC_WORKDIR/wide.parquet"
	psql_run "CREATE TABLE pq_wide (id int, t text, n numeric[]) USING pgcolumnar;
	          INSERT INTO pq_wide VALUES (1, repeat('a', 260000000), NULL);
	          INSERT INTO pq_wide SELECT 2, NULL, array_fill(1e100000::numeric, ARRAY[8200]);" >/dev/null
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET statement_timeout = '900s'" \
		-c "SELECT pgcolumnar.export_parquet('pq_wide', '$WIDEPQ')" 2>&1)"
	got="$(grep -m1 '^ERROR' <<<"$out" || tail -1 <<<"$out")"
	check "$WIDEPQN" "$got|$(pq_groups "$WIDEPQ")" "2|2|1,1"
	rm -f "$WIDEPQ"
	psql_run "DROP TABLE pq_wide;"
else
	check_skip "$WIDEPQN" "SKIP  $WIDEPQN (set PGC_RUN_OVER_1GB=1; it needs several GB of memory and disk)" "opt-in heavy arm"
fi

pgc_summary

#!/usr/bin/env bash
#
# pgColumnar Arrow IPC export suite (gap 27, piece 1).
#
# pgcolumnar.export_arrow(rel, path) writes an Arrow IPC stream file. This suite
# exports a mixed-type columnar table (with NULLs, boundary integers, NaN/Inf
# floats, empty/unicode text, and bytea) that mirrors a heap table, reads the
# file back with pyarrow, and asserts every value equals the heap oracle. It
# also exercises multiple record batches, an empty table, and error cases.
#
# Requires pyarrow (python3-pyarrow) as the reference reader; if it is not
# importable the suite skips with a note (like the DuckDB-gated benchmarks).
#
# Usage:  test/arrow_export.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; skipping Arrow export verification"
fi

# refusal LABEL SQL MESSAGE: the SQL must fail with MESSAGE. Any error used to pass,
# so a missing table, or a refusal removed and a later error raised in its place,
# read as the refusal.
refusal() {
	local label="$1" sql="$2" msg="$3" out got
	if out="$(psql_run "$sql" 2>&1)"; then
		got="succeeded"
	else
		case "$out" in
		*"$msg"*) got="refused" ;;
		*) got="other error: $(printf '%s' "$out" | head -n 1)" ;;
		esac
	fi
	check "$label" "$got" "refused"
}

ARROW="$PGC_WORKDIR/t.arrows"
PGCSV="$PGC_WORKDIR/pg.csv"

# Mixed-type pair; > ARROW_BATCH_ROWS (16384) rows to force multiple batches,
# plus explicit edge-case rows (NULLs, int boundaries, NaN/Inf, empty/unicode).
make_pair "id int, a int2, b int4, c int8, d float4, e float8, f bool, g text, h bytea"
load_pair "
  SELECT s AS id, (s%30000-15000)::int2, s*2, s::int8*1000,
         (s::float4/3), (s::float8/7), (s%2=0), 'r'||s,
         decode(lpad(to_hex(s),6,'0'),'hex')
  FROM generate_series(1,20000) s
  UNION ALL VALUES
   (100001, NULL::int2, NULL::int4, NULL::int8, NULL::float4, NULL::float8, NULL::bool, NULL::text, NULL::bytea),
   (100002, (-32768)::int2, (-2147483648)::int4, (-9223372036854775808)::int8, '-Infinity'::float4, 'NaN'::float8, false, ''::text, '\\x'::bytea),
   (100003, (32767)::int2, (2147483647)::int4, (9223372036854775807)::int8, 'Infinity'::float4, (3.14)::float8, true, 'unicode q'::text, '\\x00ff10'::bytea)
"

echo "-- export and verify against heap oracle via pyarrow"
rows_written="$(q "SELECT pgcolumnar.export_arrow('t_col', '$ARROW');")"
check "export_arrow rows written" "$rows_written" "20003"

# Heap oracle as CSV; float4 widened to float8 so its text matches Arrow's value.
psql_run "COPY (SELECT id, a, b, c, (d::float8), e, f, g, encode(h,'hex')
                FROM t_heap ORDER BY id)
          TO '$PGCSV' WITH (FORMAT csv, NULL E'\\\\N')"

pyres="$(python3 - "$ARROW" "$PGCSV" <<'PY'
import sys, csv, math
import pyarrow as pa, pyarrow.ipc as ipc
tbl = ipc.open_stream(pa.OSFile(sys.argv[1],'rb')).read_all()
cols=['id','a','b','c','d','e','f','g','h']
ad=tbl.to_pydict(); ar={tuple(ad[c][i] for c in cols)[0]:tuple(ad[c][i] for c in cols) for i in range(tbl.num_rows)}
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
# the oracle must hold the fixture's rows: with the load failed, both sides were
# empty and agreed over nothing
if len(pg) != 20003:
    print("ORACLE %d rows, not 20003" % len(pg))
    sys.exit(0)
# the file's own row count too: ar is keyed by id, so a row written twice collapsed
# into one and an export that repeated a whole batch read MATCH
if tbl.num_rows != len(pg):
    print("ROWS %d in the file, %d in the oracle" % (tbl.num_rows, len(pg)))
    sys.exit(0)
ok = set(ar)==set(pg) and all(all(feq(a,b) for a,b in zip(ar[k],pg[k])) for k in pg)
print("MATCH" if ok else "MISMATCH")
PY
)"
check "arrow values match heap oracle" "$pyres" "MATCH"

# Schema fidelity: pyarrow sees the expected field types.
sch="$(python3 - "$ARROW" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
s=ipc.open_stream(pa.OSFile(sys.argv[1],'rb')).read_all().schema
print(",".join(f"{f.name}:{f.type}" for f in s))
PY
)"
check "arrow schema" "$sch" "id:int32,a:int16,b:int32,c:int64,d:float,e:double,f:bool,g:string,h:binary"

# Empty table: schema-only stream, zero rows, still readable.
echo "-- empty table"
psql_run "CREATE TABLE t_empty (a int, b text) USING pgcolumnar;"
n0="$(q "SELECT pgcolumnar.export_arrow('t_empty', '$PGC_WORKDIR/e.arrows');")"
check "empty export rows written" "$n0" "0"
emptyrows="$(python3 - "$PGC_WORKDIR/e.arrows" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
t=ipc.open_stream(pa.OSFile(sys.argv[1],'rb')).read_all()
print(t.num_rows, ",".join(t.schema.names))
PY
)"
check "empty arrow readable" "$emptyrows" "0 a,b"

# Errors.
echo "-- argument validation"
refusal "reject non-columnar table" "SELECT pgcolumnar.export_arrow('t_heap', '$PGC_WORKDIR/x.arrows');" \
	'is not a columnar table'
psql_run "CREATE TABLE t_un (a int, p point) USING pgcolumnar;"
psql_run "INSERT INTO t_un VALUES (1, '(1,2)');"
refusal "reject unsupported type" "SELECT pgcolumnar.export_arrow('t_un', '$PGC_WORKDIR/x.arrows');" \
	'which columnar.export_arrow does not support'

# ---------------------------------------------------------------------------
# Extended type coverage: date, time, timestamp(+tz), uuid, numeric(p,s) as
# Decimal128, unconstrained numeric as text, json, jsonb. Verified against a
# heap oracle rendered to the same canonical form each type maps to (raw
# day/us integers, uuid hex, decimal/numeric text). Non-finite dates and
# timestamps and NaN numerics are documented to export as null.
# ---------------------------------------------------------------------------
echo "-- extended type coverage"
EXT="$PGC_WORKDIR/ext.arrows"
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

extrows="$(q "SELECT pgcolumnar.export_arrow('t_ext_col', '$EXT');")"
check "extended export rows written" "$extrows" "4"

# Canonical oracle: the exact representation each type maps into.
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
import pyarrow as pa, pyarrow.ipc as ipc
t = ipc.open_stream(pa.OSFile(sys.argv[1],'rb')).read_all()
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
if len(pg) != 4:
    print("ORACLE %d rows, not 4" % len(pg))
    sys.exit(0)
if t.num_rows != len(pg):
    print("ROWS %d in the file, %d in the oracle" % (t.num_rows, len(pg)))
    sys.exit(0)
print("MATCH" if ar==pg else "MISMATCH %r %r"%(ar,pg))
PY
)"
check "extended values match heap oracle" "$extres" "MATCH"

extsch="$(python3 - "$EXT" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
s=ipc.open_stream(pa.OSFile(sys.argv[1],'rb')).read_all().schema
print(",".join(f"{f.name}:{f.type}" for f in s))
PY
)"
check "extended arrow schema" "$extsch" \
"id:int32,dt:date32[day],tm:time64[us],ts:timestamp[us],tz:timestamp[us, tz=UTC],u:fixed_size_binary[16],num:decimal128(20, 4),numun:string,j:string,jb:string"

# ---- record batches are cut by bytes as well as by rows (#1811) --------------------
# The writer builds a whole record batch in one allocation, and PostgreSQL caps one
# allocation at 1 GiB. Batches were cut every 16384 rows only, so a table with more than
# 1 GiB in a batch failed with a raw string-buffer error. pgcolumnar.export_group_bytes
# is the byte cap; lowering it shows the cut at small scale. Each row buffers 10010
# bytes (id: 1 validity + 4; s: 1 validity + 10000 + a 4-byte offset), and the writer
# starts a new batch before a row that would take the batch past 65536. So 40 rows go
# into batches of 6, 6, 6, 6, 6, 6 and 4.
BYTESAR="$PGC_WORKDIR/bytes.arrow"
BYTES_DIGEST="SELECT string_agg(id || ':' || length(s) || ':' || md5(s), ' ' ORDER BY id) FROM ar_bytes"
psql_run "CREATE TABLE ar_bytes (id int, s text) USING pgcolumnar;
          INSERT INTO ar_bytes SELECT g, repeat(chr(64 + g % 26), 10000) FROM generate_series(1, 40) g;"
# The SET and the export go as two commands, so the export runs even where the setting
# is unknown, and the reading is the file it wrote.
ar_bytes_rows="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
	-c "SET pgcolumnar.export_group_bytes = 65536" -c "SELECT pgcolumnar.export_arrow('ar_bytes', '$BYTESAR')" 2>/dev/null | tail -1)"
ar_batches() {	# ar_batches FILE -> "rows|n1,n2,..." (rows per record batch), or the read failure
	python3 - "$1" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
try:
    sizes = [b.num_rows for b in ipc.open_stream(pa.OSFile(sys.argv[1], 'rb'))]
    print("%d|%s" % (sum(sizes), ",".join(str(n) for n in sizes)))
except Exception as e:
    print("unreadable: %s" % e)
PY
}
ar_digest() {	# ar_digest FILE -> "id:length:md5 ..." by id, as BYTES_DIGEST reads the table
	python3 - "$1" <<'PY'
import sys, hashlib, pyarrow as pa, pyarrow.ipc as ipc
try:
    t = ipc.open_stream(pa.OSFile(sys.argv[1], 'rb')).read_all().to_pydict()
    rows = sorted(zip(t["id"], t["s"]))
    print(" ".join("%d:%d:%s" % (i, len(s), hashlib.md5(s.encode()).hexdigest()) for i, s in rows))
except Exception as e:
    print("unreadable: %s" % e)
PY
}
check "a record batch is cut at the byte cap: 40 rows of 10000 bytes under a 65536-byte cap make batches of 6,6,6,6,6,6,4" \
	"$ar_bytes_rows|$(ar_batches "$BYTESAR")" "40|40|6,6,6,6,6,6,4"
check "and the byte-cut stream reads back every row exactly, by id, length and md5" \
	"$(ar_digest "$BYTESAR")" "$(q "$BYTES_DIGEST")"

# The cut counts the bytes the writer buffers for a row, not the bytes PostgreSQL stores
# (#1843 review). Under a 1024-byte cap: an int4 buffers 4 bytes and a validity byte, so
# a batch holds 204 rows (1020 bytes), not 205 (1025). An unconstrained numeric is
# written as text: 1e1000 stores in a few bytes but buffers 1006 (a validity byte, a
# 4-byte offset and 1001 digits), so each row is a batch of its own. A numeric[] of
# three 1e300 buffers 923 bytes (the list's validity and offset, then each element's).
ar_cut() {	# ar_cut TABLE FILE CAP -> "rows|rows|n1,n2,..." for the export under CAP
	local n
	n="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET pgcolumnar.export_group_bytes = $3" -c "SELECT pgcolumnar.export_arrow('$1', '$2')" 2>&1 | tail -1)"
	echo "$n|$(ar_batches "$2")"
}
psql_run "CREATE TABLE ar_w4 (id int) USING pgcolumnar;
          INSERT INTO ar_w4 SELECT g FROM generate_series(1, 1000) g;
          CREATE TABLE ar_wnum (n numeric) USING pgcolumnar;
          INSERT INTO ar_wnum SELECT 1e1000 FROM generate_series(1, 10);
          CREATE TABLE ar_wlist (a numeric[]) USING pgcolumnar;
          INSERT INTO ar_wlist SELECT ARRAY[1e300, 1e300, 1e300] FROM generate_series(1, 10);"
check "the byte cut counts an int4's validity byte: 1000 int4 rows under a 1024-byte cap make batches of 204,204,204,204,184" \
	"$(ar_cut ar_w4 "$PGC_WORKDIR/w4.arrow" 1024)" "1000|1000|204,204,204,204,184"
check "the byte cut counts a numeric's text form: 10 rows of 1e1000 under a 1024-byte cap make 10 batches of 1" \
	"$(ar_cut ar_wnum "$PGC_WORKDIR/wnum.arrow" 1024)" "10|10|1,1,1,1,1,1,1,1,1,1"
check "the byte cut counts a list's elements as written: 10 rows of three 1e300 under a 1024-byte cap make 10 batches of 1" \
	"$(ar_cut ar_wlist "$PGC_WORKDIR/wlist.arrow" 1024)" "10|10|1,1,1,1,1,1,1,1,1,1"

# The incoming row's estimate must count every column shape, or a large row joins a
# nearly full batch and takes it past the cap (#1843 review, second round). Under a
# 65536-byte cap, each of these rows buffers about 10 KB, so 40 rows make batches of
# 6,6,6,6,6,6,4. A composite of one 10000-byte text buffers its validity byte, then
# its field's validity, offset and bytes. A text[] of two 5000-byte texts buffers its
# list validity and offset, then each element's validity, offset and bytes. An int[]
# of 2000 elements buffers 2000 x (1 validity + 4) = 10000 bytes.
psql_run "CREATE TYPE ar_rec AS (t text);
          CREATE TABLE ar_wst (id int, r ar_rec) USING pgcolumnar;
          INSERT INTO ar_wst SELECT g, ROW(repeat('x', 10000))::ar_rec FROM generate_series(1, 40) g;
          CREATE TABLE ar_wtl (id int, a text[]) USING pgcolumnar;
          INSERT INTO ar_wtl SELECT g, ARRAY[repeat('x', 5000), repeat('y', 5000)] FROM generate_series(1, 40) g;
          CREATE TABLE ar_wil (id int, a int[]) USING pgcolumnar;
          INSERT INTO ar_wil SELECT g, array_fill(7, ARRAY[2000]) FROM generate_series(1, 40) g;"
check "the byte cut counts a composite's fields as written: 40 rows of a 10000-byte text field under a 65536-byte cap make batches of 6,6,6,6,6,6,4" \
	"$(ar_cut ar_wst "$PGC_WORKDIR/wst.arrow" 65536)" "40|40|6,6,6,6,6,6,4"
check "the byte cut counts a text list's elements as written: 40 rows of two 5000-byte texts under a 65536-byte cap make batches of 6,6,6,6,6,6,4" \
	"$(ar_cut ar_wtl "$PGC_WORKDIR/wtl.arrow" 65536)" "40|40|6,6,6,6,6,6,4"
check "the byte cut counts an int list's elements as written: 40 rows of 2000 int4 under a 65536-byte cap make batches of 6,6,6,6,6,6,4" \
	"$(ar_cut ar_wil "$PGC_WORKDIR/wil.arrow" 65536)" "40|40|6,6,6,6,6,6,4"
# A row larger than the cap goes in a batch of its own, and no empty batch is written
# before it: 3 rows of a 2000-byte text under a 1024-byte cap make 3 batches of 1.
psql_run "CREATE TABLE ar_wbig (s text) USING pgcolumnar;
          INSERT INTO ar_wbig SELECT repeat('z', 2000) FROM generate_series(1, 3);"
check "a row larger than the byte cap goes in a batch of its own: 3 rows of 2000 bytes under a 1024-byte cap make batches of 1,1,1" \
	"$(ar_cut ar_wbig "$PGC_WORKDIR/wbig.arrow" 1024)" "3|3|1,1,1"

# The writer converts a numeric or jsonb to text once, while it counts the row, and
# keeps the text for the append (#1843 review). Each kept text must reach its own
# value: scalars, list elements (with a NULL among them), and a composite's field.
psql_run "CREATE TYPE ar_tpair AS (x numeric, y text);
          CREATE TABLE ar_txt (id int, n numeric, a numeric[], j jsonb, c ar_tpair) USING pgcolumnar;
          INSERT INTO ar_txt SELECT g,
                 CASE WHEN g % 10 = 0 THEN NULL ELSE g::numeric / 7 END,
                 CASE WHEN g % 7 = 0 THEN NULL ELSE ARRAY[g::numeric / 3, NULL, g * 1e20] END,
                 jsonb_build_object('k', g, 'v', g::numeric / 9),
                 ROW(g::numeric / 11, 't' || g)::ar_tpair
            FROM generate_series(1, 300) g;"
TXT_DIGEST="SELECT string_agg(id || '|' || coalesce(n::text, 'NULL') || '|' || coalesce(array_to_string(a, ',', 'N'), 'NULL')
                   || '|' || j::text || '|' || (c).x::text || '|' || (c).y, ' ' ORDER BY id) FROM ar_txt"
ar_txt_rows="$(q "SELECT pgcolumnar.export_arrow('ar_txt', '$PGC_WORKDIR/txt.arrow')")"
ar_txt_read="$(python3 - "$PGC_WORKDIR/txt.arrow" <<'PY'
import sys
try:
    import pyarrow as pa, pyarrow.ipc as ipc
    t = ipc.open_stream(pa.OSFile(sys.argv[1], 'rb')).read_all().to_pydict()
    def arr(v):
        return "NULL" if v is None else ",".join("N" if e is None else e for e in v)
    print(" ".join("%d|%s|%s|%s|%s|%s" % (i, "NULL" if n is None else n, arr(a), j, c["x"], c["y"])
                   for i, n, a, j, c in sorted(zip(t["id"], t["n"], t["a"], t["j"], t["c"]))))
except Exception as e:
    print("unreadable: %s" % e)
PY
)"
check "every numeric and jsonb text reaches its own value: 300 rows of numeric, numeric[], jsonb and a composite read back from the stream as PostgreSQL prints them" \
	"$ar_txt_rows|$ar_txt_read" "300|$(q "$TXT_DIGEST")"

# Over 1 GiB in one batch at the default cap (#1811's repro). Opt-in, as
# large_values.sh's arm is: the table and its file are each about 1.1 GB. 2200 rows of
# 500000 bytes; each row buffers 500010 bytes, so a 256 MiB batch holds 536 rows and
# the stream has 5 batches (4 of 536, then 56).
OVERAR="over 1 GiB of one column (2200 values of 500000 bytes) exports whole, in 5 record batches of at most 536 rows"
if [ "${PGC_RUN_OVER_1GB:-0}" = 1 ]; then
	HUGEAR="$PGC_WORKDIR/huge.arrow"
	psql_run "CREATE TABLE ar_huge (id int, s text) USING pgcolumnar;
	          SELECT pgcolumnar.set_options('ar_huge', stripe_row_limit => 1000);
	          INSERT INTO ar_huge SELECT g, repeat('x', 500000) FROM generate_series(1, 2200) g;" >/dev/null
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET statement_timeout = '900s'" \
		-c "SELECT pgcolumnar.export_arrow('ar_huge', '$HUGEAR')" 2>&1)"
	got="$(grep -m1 '^ERROR' <<<"$out" || tail -1 <<<"$out")"
	check "$OVERAR" "$got|$(ar_batches "$HUGEAR")" "2200|2200|536,536,536,536,56"
	rm -f "$HUGEAR"
	psql_run "DROP TABLE ar_huge;"
else
	check_skip "$OVERAR" "SKIP  $OVERAR (set PGC_RUN_OVER_1GB=1; it needs several GB of memory and disk)" "opt-in heavy arm"
fi

# A row whose written form is far larger than its stored form, after a large row (#1843
# review). Opt-in for the same reason. Row 1 holds 260 MB of text. Row 2 holds 8200
# numerics of 1e100000: about 98 KB stored, but 820 MB as text. Counted by its stored
# size, row 2 joined row 1's group and the export failed with the raw string-buffer
# error. Counted as written, it goes in a group of its own.
WIDEARN="a row of 8200 numerics of 1e100000 (98 KB stored, 820 MB as text) after a 260 MB row exports whole, in 2 record batches of 1"
if [ "${PGC_RUN_OVER_1GB:-0}" = 1 ]; then
	WIDEAR="$PGC_WORKDIR/wide.arrow"
	psql_run "CREATE TABLE ar_wide (id int, t text, n numeric[]) USING pgcolumnar;
	          INSERT INTO ar_wide VALUES (1, repeat('a', 260000000), NULL);
	          INSERT INTO ar_wide SELECT 2, NULL, array_fill(1e100000::numeric, ARRAY[8200]);" >/dev/null
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET statement_timeout = '900s'" \
		-c "SELECT pgcolumnar.export_arrow('ar_wide', '$WIDEAR')" 2>&1)"
	got="$(grep -m1 '^ERROR' <<<"$out" || tail -1 <<<"$out")"
	check "$WIDEARN" "$got|$(ar_batches "$WIDEAR")" "2|2|1,1"
	rm -f "$WIDEAR"
	psql_run "DROP TABLE ar_wide;"
else
	check_skip "$WIDEARN" "SKIP  $WIDEARN (set PGC_RUN_OVER_1GB=1; it needs several GB of memory and disk)" "opt-in heavy arm"
fi

pgc_summary

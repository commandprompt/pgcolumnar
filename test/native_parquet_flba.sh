#!/usr/bin/env bash
#
# pgColumnar Parquet FIXED_LEN_BYTE_ARRAY read coverage (Phase G).
#
# The reader advertises uuid and numeric(p,s) for FLBA columns via
# parquet_schema(), and the exporter writes both. This suite covers the read side
# that made those advertisements truthful: uuid as a 16-byte FLBA, and DECIMAL as
# a big-endian two's-complement integer at the column's scale, in either a fixed
# (FLBA) or variable (BYTE_ARRAY) width. It rounds our own export back through the
# reader and also reads pyarrow-written files, and checks correctness against a
# heap oracle.
#
# Usage:  test/native_parquet_flba.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

have_pyarrow=0
python3 -c 'import pyarrow.parquet' 2>/dev/null && have_pyarrow=1

W="$PGC_WORKDIR"
psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# ---- round-trip our own exporter (uuid + numeric via FLBA) -----------------
psql_run "CREATE TABLE src (
            id int,
            u  uuid,
            d  numeric(12,4),
            dn numeric(12,4),
            fb bytea)
          USING pgcolumnar;"
psql_run "INSERT INTO src VALUES
            (1, '11111111-2222-3333-4444-555555555555', 123.4500, -0.0001, '\\xdeadbeef'),
            (2, 'ffffffff-ffff-ffff-ffff-ffffffffffff', -9999.9999, 0, '\\x00'),
            (3, '00000000-0000-0000-0000-000000000000', 0.0000, 42.0, NULL);"

EXP="$W/roundtrip.parquet"
psql_run "SELECT pgcolumnar.export_parquet('src'::regclass, '$EXP');"

# read_parquet must return exactly what the heap holds
check "uuid round-trips through export/read" \
	"$(pgc_set_hash "SELECT id, u FROM pgcolumnar.read_parquet('$EXP') AS t(id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea)")" \
	"$(pgc_set_hash "SELECT id, u FROM src")"
check "numeric round-trips through export/read" \
	"$(pgc_set_hash "SELECT id, d, dn FROM pgcolumnar.read_parquet('$EXP') AS t(id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea)")" \
	"$(pgc_set_hash "SELECT id, d, dn FROM src")"
check "whole-row round-trip == source" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$EXP') AS t(id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea)")" \
	"$(pgc_set_hash "SELECT * FROM src")"

# import_parquet shares the decoder
psql_run "CREATE TABLE imp (id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea);"
psql_run "SELECT pgcolumnar.import_parquet('imp'::regclass, '$EXP');"
check "import_parquet == source" \
	"$(pgc_set_hash "SELECT * FROM imp")" "$(pgc_set_hash "SELECT * FROM src")"

# FDW surface
psql_run "CREATE FOREIGN TABLE ft (id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea)
          SERVER pq OPTIONS (path '$EXP');"
check "FDW scan == source" \
	"$(pgc_set_hash "SELECT * FROM ft")" "$(pgc_set_hash "SELECT * FROM src")"

# ---- pyarrow-written files (foreign producer) ------------------------------
if [ "$have_pyarrow" = 1 ]; then
	python3 - "$W" <<'PY'
import sys, uuid, decimal, pyarrow as pa, pyarrow.parquet as pq
W = sys.argv[1]
us = [uuid.UUID('11111111-2222-3333-4444-555555555555').bytes,
      uuid.UUID('00000000-0000-0000-0000-000000000000').bytes]
pq.write_table(pa.table({"u": pa.array(us, pa.binary(16))}), f"{W}/pa_uuid.parquet")
# decimal128 -> FLBA; a range that stresses sign and scale
ds = [decimal.Decimal("123.45"), decimal.Decimal("-9999.99"),
      decimal.Decimal("0.01"), decimal.Decimal("0.00")]
pq.write_table(pa.table({"d": pa.array(ds, pa.decimal128(10, 2))}), f"{W}/pa_dec.parquet")
PY
	# uuid stored as raw bytes; declare bytea to compare, since pyarrow gives no
	# uuid logical type, then also read as uuid (our exporter's convention).
	# Both values, in order. The arm read only the smallest, the all-zero uuid,
	# which every byte order leaves unchanged: with the reader reversing a
	# uuid's bytes it stayed green (#1341).
	check "pyarrow uuid reads as uuid" \
		"$(q "SELECT string_agg(u::text, ',' ORDER BY u) FROM pgcolumnar.read_parquet('$W/pa_uuid.parquet') AS t(u uuid);")" \
		"00000000-0000-0000-0000-000000000000,11111111-2222-3333-4444-555555555555"
	check "pyarrow decimal128 values are exact" \
		"$(pgc_set_hash "SELECT d FROM pgcolumnar.read_parquet('$W/pa_dec.parquet') AS t(d numeric(10,2))")" \
		"$(pgc_set_hash "SELECT * FROM (VALUES (123.45::numeric(10,2)),(-9999.99),(0.01),(0.00)) v(d)")"

	# ---- crafted out-of-range scale must be rejected, not decoded -----------
	# The DECIMAL scale comes straight from the footer, and left unvalidated it
	# drives pq_decimal_to_numeric's zero-fill (~scale bytes) into a fixed stack
	# buffer: a large scale is a stack smash on a crafted file. Rewrite the schema
	# element's scale field (SchemaElement field 7) in a decimal128(10,2) file from
	# 2 to 50 -- out of range (> precision, > 38) but small enough that the
	# unguarded decoder does NOT crash: it decodes a (wrong) value with no error.
	# The guard must instead reject the bind. zigzag(2)=0x04 and zigzag(50)=0x64 are
	# both one byte, so the edit is in place and the footer stays well-formed.
	python3 - "$W" <<'PY'
import sys, decimal, pyarrow as pa, pyarrow.parquet as pq
W = sys.argv[1]
pq.write_table(pa.table({"d": pa.array([decimal.Decimal("1.23")], pa.decimal128(10, 2))}),
               f"{W}/dec_badscale.parquet")
raw = bytearray(open(f"{W}/dec_badscale.parquet", "rb").read())
flen = int.from_bytes(raw[-8:-4], "little")
pos = len(raw) - 8 - flen

def varint():
    global pos
    sh = 0; out = 0
    while True:
        b = raw[pos]; pos += 1
        out |= (b & 0x7F) << sh
        if not b & 0x80: return out
        sh += 7
def zigzag():
    u = varint(); return (u >> 1) ^ -(u & 1)
def field(last):
    global pos
    b = raw[pos]; pos += 1
    if b == 0: return 0, 0, last
    t = b & 0x0F; d = b >> 4
    fid = last + d if d else zigzag()
    return t, fid, fid
def skip(t):
    global pos
    if t in (1, 2): return
    if t == 3: pos += 1
    elif t in (4, 5, 6): zigzag()
    elif t == 7: pos += 8
    elif t == 8:
        blen = varint()      # advance past the length bytes, then the content;
        pos += blen          # `pos += varint()` would drop the length advance
    elif t in (9, 10):
        b = raw[pos]; pos += 1
        n = (b >> 4) & 0x0F; et = b & 0x0F
        if n == 0x0F: n = varint()
        for _ in range(n): skip(et)
    elif t == 12:
        last = 0
        while True:
            ft, _f, last = field(last)
            if ft == 0: break
            skip(ft)

# FileMetaData field 2 is the schema list<SchemaElement>; the scale (field 7) sits
# on the leaf element. Walk to it and patch the one byte in place.
patched = False
last = 0
while not patched:
    ft, fid, last = field(last)
    if ft == 0: break
    if fid == 2 and ft in (9, 10):
        b = raw[pos]; pos += 1
        n = (b >> 4) & 0x0F; et = b & 0x0F
        if n == 0x0F: n = varint()
        for _ in range(n):
            elast = 0
            while True:
                eft, efid, elast = field(elast)
                if eft == 0: break
                if efid == 7 and eft in (5, 6, 4):
                    assert raw[pos] == 0x04, hex(raw[pos])   # zigzag(2)
                    raw[pos] = 0x64                           # zigzag(50)
                    patched = True
                    break
                skip(eft)
            if patched: break
        break
    skip(ft)
assert patched, "did not find a scale field to patch"
open(f"{W}/dec_badscale.parquet", "wb").write(bytes(raw))
print("  patched scale 2 -> 50", file=sys.stderr)
PY

	psql_run "CREATE FUNCTION pgc_try_flba(q text) RETURNS text LANGUAGE plpgsql AS \$\$
	          BEGIN EXECUTE q; RETURN 'NO ERROR';
	          EXCEPTION WHEN OTHERS THEN RETURN 'REJECTED ' || SQLSTATE; END \$\$;"
	# Fixed: the bind guard rejects scale > precision/38 -> REJECTED. Unguarded: the
	# decoder accepts it and returns a wrong-but-valid numeric -> NO ERROR. So this
	# check distinguishes fixed from unfixed without depending on a crash.
	#
	# BY THE BIND, WHICH IS WHAT "not decoded" MEANS. Any error used to count.
	# With the bind guard removed, the decoder's own scale check still refused
	# the file, as XX001 "could not decode Parquet column 0" -- after decoding
	# began -- and the arm stayed green (#1341). The bind refuses with 42804.
	check "crafted out-of-range scale is rejected, not decoded" \
		"$(q "SELECT pgc_try_flba(\$q\$SELECT * FROM pgcolumnar.read_parquet('$W/dec_badscale.parquet') AS t(d numeric)\$q\$);")" \
		"REJECTED 42804"
	check "backend survived the crafted scale" "$(q 'SELECT 1;')" "1"

	# ---- DECIMAL held in an INT32 or INT64 ----------------------------------
	#
	# The spec allows a DECIMAL to be stored as a plain integer, which is what
	# writers use for small precisions, and pyarrow does it on request. Verified
	# in this pyarrow (23.0.1) rather than assumed from the spec's thresholds:
	# precision up to 9 becomes INT32, up to 18 becomes INT64, beyond that FLBA.
	# Unlike the byte-array forms the integer is little-endian, so the decode
	# widens it into a big-endian buffer and reuses the one scale conversion.
	python3 - "$W" <<'PYINT'
import decimal, os, sys
import pyarrow as pa, pyarrow.parquet as pq
W = sys.argv[1]
vals = [decimal.Decimal("1.25"), decimal.Decimal("-3.50"),
        decimal.Decimal("0.00"), None]
for name, p, s in (("i32", 9, 2), ("i64", 18, 6)):
    t = pa.table({"d": pa.array([None if v is None else v.scaleb(0) for v in vals],
                                pa.decimal128(p, s))})
    pq.write_table(t, os.path.join(W, f"dec_{name}.parquet"),
                   store_decimal_as_integer=True, compression="none")
phys = {}
for name in ("i32", "i64"):
    f = pq.ParquetFile(os.path.join(W, f"dec_{name}.parquet"))
    phys[name] = f.metadata.row_group(0).column(0).physical_type
if phys != {"i32": "INT32", "i64": "INT64"}:
    sys.exit("unexpected physical types: %s" % phys)
PYINT
	if [ $? -ne 0 ]; then
		# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994): a single skip under a
		# name none of the six arms has leaves all six with no record, so a skipped
		# arm and a deleted one are indistinguishable to a reader and to the ledger.
		for _fl_n in "INT32-backed DECIMAL reads" \
				"INT64-backed DECIMAL reads" \
				"INT32-backed DECIMAL keeps its null" \
				"parquet_schema advises numeric for an INT32 DECIMAL" \
				"parquet_schema advises numeric for an INT64 DECIMAL" \
				"an INT64 DECIMAL still binds to bigint as the unscaled integer"; do
			check_skip "$_fl_n" \
				"SKIP  $_fl_n (this pyarrow does not store decimals as integers as expected)" \
				"this pyarrow does not store decimals as integers"
		done
	else
		check "INT32-backed DECIMAL reads" \
			"$(q "SELECT string_agg(d::text, ',' ORDER BY d) FROM pgcolumnar.read_parquet('$W/dec_i32.parquet') AS t(d numeric);")" \
			"-3.50,0.00,1.25"
		check "INT64-backed DECIMAL reads" \
			"$(q "SELECT string_agg(d::text, ',' ORDER BY d) FROM pgcolumnar.read_parquet('$W/dec_i64.parquet') AS t(d numeric);")" \
			"-3.500000,0.000000,1.250000"
		check "INT32-backed DECIMAL keeps its null" \
			"$(q "SELECT count(*) - count(d) FROM pgcolumnar.read_parquet('$W/dec_i32.parquet') AS t(d numeric);")" "1"
		check "parquet_schema advises numeric for an INT32 DECIMAL" \
			"$(q "SELECT data_type FROM pgcolumnar.parquet_schema('$W/dec_i32.parquet');")" \
			"numeric(9,2)"
		check "parquet_schema advises numeric for an INT64 DECIMAL" \
			"$(q "SELECT data_type FROM pgcolumnar.parquet_schema('$W/dec_i64.parquet');")" \
			"numeric(18,6)"
		# the integer reading is not silently reinterpreted: declaring bigint
		# against a DECIMAL column must still bind to the raw integer
		check "an INT64 DECIMAL still binds to bigint as the unscaled integer" \
			"$(q "SELECT string_agg(d::text, ',' ORDER BY d) FROM pgcolumnar.read_parquet('$W/dec_i64.parquet') AS t(d bigint);")" \
			"-3500000,0,1250000"
	fi
else
	# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994). This is the OUTER gate:
	# without pyarrow none of the ten arms in the `then` branch runs, and a single
	# skip named for none of them left all ten with no record. Only one branch ever
	# fires, so naming the six that the inner gate also names duplicates nothing.
	for _fl_o in "pyarrow uuid reads as uuid" \
			"pyarrow decimal128 values are exact" \
			"crafted out-of-range scale is rejected, not decoded" \
			"backend survived the crafted scale" \
			"INT32-backed DECIMAL reads" \
			"INT64-backed DECIMAL reads" \
			"INT32-backed DECIMAL keeps its null" \
			"parquet_schema advises numeric for an INT32 DECIMAL" \
			"parquet_schema advises numeric for an INT64 DECIMAL" \
			"an INT64 DECIMAL still binds to bigint as the unscaled integer"; do
		check_skip "$_fl_o" \
			"SKIP  $_fl_o (pyarrow not available)" \
			"pyarrow not available"
	done
fi

# ---- an unannotated FLBA (Iceberg fixed(L)) reads as bytea (#1792) ----------
#
# Iceberg writes fixed(L) as a FIXED_LEN_BYTE_ARRAY of width L with no logical
# type, and parquet_schema advises bytea for it (uuid at L = 16). The bind step
# wanted a BYTE_ARRAY for every bytea target, so the advised column definition
# was refused by read_parquet, iceberg_scan and both FDWs. And uuid bound to an
# FLBA of any width, so a 3-byte column declared uuid reached the decoder and
# failed there as XX001 instead of at bind.
#
# The fixture is a committed pyiceberg table (gen_iceberg_fixed.py), so the file
# is what an Iceberg writer really produces and no pyarrow is needed to run this:
# id bigint (field 1), fx fixed(3) (field 2), f16 fixed(16) (field 3), with rows
# (1,'abc',00..0f), (2,'xyz',ff..ff), (3,NULL,NULL).
FXWH="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg/warehouse_fixed/db/fixed"
if [ -f "$(ls "$FXWH"/data/*.parquet 2>/dev/null | head -1)" ]; then
	FXD="$W/ice_fixed"
	rm -rf "$FXD"; mkdir -p "$FXD"
	cp -r "$FXWH" "$FXD/fixed"
	chmod -R u+rwX,go+rX "$FXD"
	FXMD="$(ls "$FXD"/fixed/metadata/*.metadata.json | sort | tail -1)"
	FXDF="$(ls "$FXD"/fixed/data/*.parquet | head -1)"
	FX3="1:616263,2:78797a,3:NULL"
	FX16="1:000102030405060708090a0b0c0d0e0f,2:ffffffffffffffffffffffffffffffff,3:NULL"
	# pgc_fixed_q: the query's one value, or "SQLSTATE: message" when it fails, so
	# a red names the refusal rather than reading as an empty string.
	# pgc_fixed_err: "NO ERROR", or the same reading of the refusal.
	psql_run "CREATE FUNCTION pgc_fixed_q(q text) RETURNS text LANGUAGE plpgsql AS \$\$
	          DECLARE r text; BEGIN EXECUTE q INTO r; RETURN r;
	          EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE || ': ' || SQLERRM; END \$\$;"
	psql_run "CREATE FUNCTION pgc_fixed_err(q text) RETURNS text LANGUAGE plpgsql AS \$\$
	          BEGIN EXECUTE q; RETURN 'NO ERROR';
	          EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE || ': ' || SQLERRM; END \$\$;"
	# id:hex(COL) per row in id order; a NULL reads as the word, not as ''
	hexrows() {	# hexrows COL FROM -> "id:hex,..." or "SQLSTATE: message"
		q "SELECT pgc_fixed_q(\$q\$SELECT string_agg(id || ':' || coalesce(encode($1, 'hex'), 'NULL'), ',' ORDER BY id) FROM $2\$q\$);"
	}

	check "parquet_schema advises bytea for Iceberg fixed(3) and uuid for fixed(16)" \
		"$(q "SELECT string_agg(column_name || '|' || data_type, ',') FROM pgcolumnar.parquet_schema('$FXDF');")" \
		"id|bigint,fx|bytea,f16|uuid"
	check "fixed(3) reads as bytea through read_parquet" \
		"$(hexrows fx "pgcolumnar.read_parquet('$FXDF') AS t(id bigint, fx bytea, f16 uuid)")" "$FX3"
	check "fixed(16) reads as bytea through read_parquet" \
		"$(hexrows f16 "pgcolumnar.read_parquet('$FXDF') AS t(id bigint, fx bytea, f16 bytea)")" "$FX16"
	check "fixed(3) reads as bytea through read_parquet field ids" \
		"$(hexrows fx "pgcolumnar.read_parquet('$FXDF', ARRAY[2,1]) AS t(fx bytea, id bigint)")" "$FX3"
	check "fixed(3) reads as bytea through iceberg_scan" \
		"$(hexrows fx "pgcolumnar.iceberg_scan('$FXMD') AS t(id bigint, fx bytea)")" "$FX3"
	psql_run "CREATE FOREIGN TABLE ft_fixed (id bigint, fx bytea, f16 bytea)
	          SERVER pq OPTIONS (path '$FXDF');"
	check "fixed(3) reads as bytea through the parquet FDW" \
		"$(hexrows fx "ft_fixed")" "$FX3"
	psql_run "CREATE SERVER ice_fixed FOREIGN DATA WRAPPER pgcolumnar_iceberg;"
	psql_run "CREATE FOREIGN TABLE ice_ft_fixed (id bigint, fx bytea)
	          SERVER ice_fixed OPTIONS (metadata_path '$FXMD');"
	check "fixed(3) reads as bytea through the iceberg FDW" \
		"$(hexrows fx "ice_ft_fixed")" "$FX3"
	# uuid still binds to the 16-byte column, and only to it. By field id, so the
	# reading does not depend on the fixed(3) column binding as well.
	check "fixed(16) still reads as uuid" \
		"$(q "SELECT pgc_fixed_q(\$q\$SELECT string_agg(coalesce(f16::text, 'NULL'), ',' ORDER BY id) FROM pgcolumnar.read_parquet('$FXDF', ARRAY[1,3]) AS t(id bigint, f16 uuid)\$q\$);")" \
		"00010203-0405-0607-0809-0a0b0c0d0e0f,ffffffff-ffff-ffff-ffff-ffffffffffff,NULL"
	# Refused BY THE BIND, which says why; the decoder's XX001 "could not decode"
	# is what the unfixed width check gave.
	check "uuid over fixed(3) is refused at bind (42804)" \
		"$(q "SELECT pgc_fixed_err(\$q\$SELECT * FROM pgcolumnar.read_parquet('$FXDF') AS t(id bigint, fx uuid, f16 uuid)\$q\$);")" \
		'42804: Parquet column 1 physical type is not compatible with target column "fx" (uuid)'
	check "uuid over fixed(3) is refused at field-id bind (42804)" \
		"$(q "SELECT pgc_fixed_err(\$q\$SELECT * FROM pgcolumnar.iceberg_scan('$FXMD') AS t(id bigint, fx uuid)\$q\$);")" \
		'42804: Parquet column with field id 2 has a physical type incompatible with output column "fx" (uuid)'
else
	# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994)
	for _fl_x in "parquet_schema advises bytea for Iceberg fixed(3) and uuid for fixed(16)" \
			"fixed(3) reads as bytea through read_parquet" \
			"fixed(16) reads as bytea through read_parquet" \
			"fixed(3) reads as bytea through read_parquet field ids" \
			"fixed(3) reads as bytea through iceberg_scan" \
			"fixed(3) reads as bytea through the parquet FDW" \
			"fixed(3) reads as bytea through the iceberg FDW" \
			"fixed(16) still reads as uuid" \
			"uuid over fixed(3) is refused at bind (42804)" \
			"uuid over fixed(3) is refused at field-id bind (42804)"; do
		check_skip "$_fl_x" \
			"SKIP  $_fl_x (the warehouse_fixed fixture is missing)" \
			"the warehouse_fixed fixture is missing"
	done
fi

pgc_summary

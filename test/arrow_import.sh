#!/usr/bin/env bash
#
# pgColumnar Arrow IPC import suite.
#
# pgcolumnar.import_arrow(rel, path) inserts the rows of an Arrow IPC stream file
# into an existing columnar table, the reverse of pgcolumnar.export_arrow. This
# suite covers three things:
#   1. Round trip: export a mixed-type columnar table, import it into a fresh
#      columnar table, and assert the two are identical (differential oracle).
#   2. Foreign file: read a file written by pyarrow (not by pgColumnar) and
#      assert the values arrive correctly.
#   3. The documented lossy mappings (non-finite date/timestamp and NaN numeric
#      export as null) and the error cases.
#
# Requires pyarrow to build the foreign file and to cross-check; if it is not
# importable the suite skips with a note.
#
# Usage:  test/arrow_import.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Run one statement under a HARD wall-clock cap. A cancel-resistant open (a FIFO)
# cannot be ended by statement_timeout, so the external timeout is the only
# reliable detector. Prints the 5-char SQLSTATE, or HANG when psql was KILLed.
sqlstate_or_hang() {
	local out rc
	out="$(timeout -s KILL 5 env PATH="$PGC_BINDIR:$PATH" psql \
		-h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY sqlstate
$1;
SQLEOF
)"
	rc=$?
	if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then echo HANG; return; fi
	printf '%s\n' "$out" | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
fifo_release() { exec 9<>"$1" 2>/dev/null; exec 9>&- 2>/dev/null; }

have_pyarrow=1
python3 -c 'import pyarrow' 2>/dev/null || have_pyarrow=0

expect_error() {
	local label="$1" sql="$2"
	if psql_run "$sql" >/dev/null 2>&1; then
		check "$label (expected error)" "succeeded" "error"
	else
		check "$label" "error" "error"
	fi
}

ARROW="$PGC_WORKDIR/rt.arrows"

# --- 1. round trip through pgColumnar's own writer ---------------------------
echo "-- round trip: export then import"
# 16 columns: the maximum pgcolumnar.export_arrow supports.
psql_run "CREATE TABLE ri_src (id int, c int8, d float4, e float8, f bool,
          g text, h bytea, dt date, tm time, ts timestamp, tz timestamptz,
          u uuid, num numeric(20,4), numun numeric, j json, jb jsonb) USING pgcolumnar;"
psql_run "INSERT INTO ri_src
  SELECT s, s::int8*7, (s::float4/3), (s::float8/9), (s%2=0),
         'row'||s, decode(lpad(to_hex(s),4,'0'),'hex'),
         DATE '2000-01-01' + s, TIME '00:00:00' + (s || ' seconds')::interval,
         TIMESTAMP '2001-01-01' + (s||' minutes')::interval,
         TIMESTAMPTZ '2001-01-01 00:00:00+00' + (s||' minutes')::interval,
         ('00000000-0000-0000-0000-' || lpad(to_hex(s),12,'0'))::uuid,
         (s::numeric/100)::numeric(20,4), (s::numeric/7),
         json_build_object('n', s), jsonb_build_object('n', s)
  FROM generate_series(1, 3000) s;"
# a few explicit NULL rows and float NaN/Inf (these round trip exactly)
psql_run "INSERT INTO ri_src (id, d, e) VALUES
  (900001, NULL, NULL),
  (900002, 'NaN'::float4, 'Infinity'::float8),
  (900003, '-Infinity'::float4, 'NaN'::float8);"

src_rows="$(q "SELECT count(*) FROM ri_src;")"
w="$(q "SELECT pgcolumnar.export_arrow('ri_src', '$ARROW');")"
check "export rows" "$w" "$src_rows"

psql_run "CREATE TABLE ri_dst (LIKE ri_src) USING pgcolumnar;"
n="$(q "SELECT pgcolumnar.import_arrow('ri_dst', '$ARROW');")"
check "import rows" "$n" "$src_rows"

h_src="$(pgc_set_hash "SELECT * FROM ri_src")"
h_dst="$(pgc_set_hash "SELECT * FROM ri_dst")"
check "round trip identical" "$h_dst" "$h_src"

# --- 2. a file written by pyarrow (foreign producer) ------------------------
if [ "$have_pyarrow" = 1 ]; then
	echo "-- import a pyarrow-written file"
	PYF="$PGC_WORKDIR/foreign.arrows"
	python3 - "$PYF" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
t = pa.table({
    'a': pa.array([1, 2, None, 4], pa.int64()),
    'b': pa.array([1.5, None, 3.5, 4.5], pa.float64()),
    'c': pa.array(['x', 'y', 'z', None], pa.string()),
})
with ipc.new_stream(pa.OSFile(sys.argv[1], 'wb'), t.schema) as w:
    w.write_table(t)
PY
	psql_run "CREATE TABLE ri_for (a bigint, b float8, c text) USING pgcolumnar;"
	nf="$(q "SELECT pgcolumnar.import_arrow('ri_for', '$PYF');")"
	check "pyarrow import rows" "$nf" "4"
	vals="$(q "SELECT string_agg(coalesce(a::text,'-')||'/'||coalesce(b::text,'-')||'/'||coalesce(c,'-'), ',' ORDER BY a NULLS LAST) FROM ri_for;")"
	check "pyarrow values" "$vals" "1/1.5/x,2/-/y,4/4.5/-,-/3.5/z"
else
	echo "-- pyarrow not available; skipping foreign-file import"
fi

# --- 3. documented lossy mapping: non-finite -> null ------------------------
echo "-- non-finite date/timestamp and NaN numeric import as null"
LOSSY="$PGC_WORKDIR/lossy.arrows"
psql_run "CREATE TABLE ri_nf (id int, dt date, ts timestamp, num numeric(10,2)) USING pgcolumnar;"
psql_run "INSERT INTO ri_nf VALUES (1, 'infinity', '-infinity', 'NaN'), (2, '2020-01-01', '2020-01-01 00:00', 1.25);"
psql_run "SELECT pgcolumnar.export_arrow('ri_nf', '$LOSSY');"
psql_run "CREATE TABLE ri_nf2 (LIKE ri_nf) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.import_arrow('ri_nf2', '$LOSSY');"
nulls="$(q "SELECT count(*) FROM ri_nf2 WHERE id=1 AND dt IS NULL AND ts IS NULL AND num IS NULL;")"
check "non-finite imported as null" "$nulls" "1"
keep="$(q "SELECT count(*) FROM ri_nf2 WHERE id=2 AND dt='2020-01-01' AND num=1.25;")"
check "finite row preserved" "$keep" "1"

# --- 3b. documented lossy mapping: nanosecond -> microsecond, and it is REPORTED
#
# PostgreSQL timestamps and times are int64 MICROSECONDS; Arrow parameterises the
# unit in the type. Of the four Arrow units, second, millisecond and microsecond
# all widen or match exactly, so only nanosecond can lose anything -- and only for
# values that are not already on a microsecond boundary. A pandas datetime64[ns]
# column built from second- or millisecond-resolution data is nanosecond-TYPED and
# entirely lossless to convert.
#
# The contract is: never refuse over this. Narrow it, keep every row, and say how
# many values actually lost digits. Silence would make an import that changed the
# data indistinguishable from one that did not -- and truncation does more than
# reduce precision, it can make distinct rows EQUAL, which is what a later UNIQUE
# violation would be reporting without explaining.
#
# The two controls are the point of the section: an ns-typed file whose values are
# all on a microsecond boundary must report NOTHING, and a microsecond file must
# report nothing, or the counter is measuring the unit rather than the loss.
if [ "$have_pyarrow" = 1 ]; then
	echo "-- nanosecond narrowing is reported, never refused"
	psql_c() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -At -c "$1" 2>&1; }

	NSLOSSY="$PGC_WORKDIR/ns_lossy.arrows"
	NSEXACT="$PGC_WORKDIR/ns_exact.arrows"
	USPLAIN="$PGC_WORKDIR/us_plain.arrows"
	NSDEEP="$PGC_WORKDIR/ns_deep.arrows"
	python3 - "$NSLOSSY" "$NSEXACT" "$USPLAIN" "$NSDEEP" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
S2000 = 946684800
def w(path, a, b):
    t = pa.table({'ts': a, 'ts2': b})
    with ipc.new_stream(pa.OSFile(path, 'wb'), t.schema) as o:
        o.write_table(t)
# TWO temporal columns over four rows, so the number of VALUES that lose digits
# (8) differs from the number of ROWS (4). With one column the two coincide and
# an arm cannot tell which one the message is reporting.
def ns(off):
    return pa.array([(S2000 + i) * 10**9 + off for i in range(4)], pa.timestamp('ns'))
# every value carries 123ns past a microsecond boundary: all EIGHT truncate
w(sys.argv[1], ns(123), ns(123))
# nanosecond-TYPED but microsecond-exact: nothing is lost, nothing to report
w(sys.argv[2], ns(0), ns(0))
# a different unit entirely: the counter must not fire on it
us = lambda: pa.array([(S2000 + i) * 10**6 for i in range(4)], pa.timestamp('us'))
w(sys.argv[3], us(), us())
# The counter is reached from TWO call sites -- the time arm and the timestamp
# arm -- and is threaded through the list and struct recursion. A file with only
# a top-level timestamp column leaves the other three paths unmeasured: passing
# NULL at the time site, or dropping the counter from the nested recursion,
# would not move a single arm above. This file covers them.
#   time64[ns]                  4 values, all 123ns past a boundary
#   list<timestamp[ns]> x2      8 values, likewise
#                              12 total
NOON = 12 * 3600
tm = pa.array([(NOON + i) * 10**9 + 123 for i in range(4)], pa.time64('ns'))
lst = pa.array([[(S2000 + i) * 10**9 + 123, (S2000 + i + 1) * 10**9 + 123]
                for i in range(4)], pa.list_(pa.timestamp('ns')))
deep = pa.table({'tm': tm, 'lst': lst})
with ipc.new_stream(pa.OSFile(sys.argv[4], 'wb'), deep.schema) as o:
    o.write_table(deep)
PY

	psql_run "CREATE TABLE ri_nsl (ts timestamp, ts2 timestamp) USING pgcolumnar;"
	psql_run "CREATE TABLE ri_nse (ts timestamp, ts2 timestamp) USING pgcolumnar;"
	psql_run "CREATE TABLE ri_usp (ts timestamp, ts2 timestamp) USING pgcolumnar;"

	nsl_out="$(psql_c "SELECT pgcolumnar.import_arrow('ri_nsl', '$NSLOSSY');")"
	nse_out="$(psql_c "SELECT pgcolumnar.import_arrow('ri_nse', '$NSEXACT');")"
	usp_out="$(psql_c "SELECT pgcolumnar.import_arrow('ri_usp', '$USPLAIN');")"

	# THE CONTRACT: not one row refused, on any of the three.
	check "a lossy nanosecond file imports every row" \
		"$(q "SELECT count(*) FROM ri_nsl;")" "4"
	check "so does a microsecond-exact nanosecond file" \
		"$(q "SELECT count(*) FROM ri_nse;")" "4"
	check "and a microsecond file" \
		"$(q "SELECT count(*) FROM ri_usp;")" "4"

	# THE ARM: the loss is counted and reported, once, with the number -- and the
	# number is the count of VALUES (8), not of rows (4). The fixture has two
	# temporal columns precisely so those two differ: with one column they
	# coincide, and an arm matching "4" is satisfied by either, which is how an
	# earlier revision of this suite passed a build whose counter reported the
	# wrong population entirely.
	check "the narrowing reports the number of VALUES that lost digits, not rows" \
		"$(printf '%s' "$nsl_out" | grep -c 'columnar.import_arrow: 8 values lost sub-microsecond precision')" "1"
	check "and does not report the row count instead" \
		"$(printf '%s' "$nsl_out" | grep -c 'import_arrow: 4 values lost')" "0"

	# The other three paths that reach the counter. Without this arm, passing
	# NULL for nsTrunc at the time call site, or dropping it from the list or
	# struct recursion, leaves every arm above green.
	psql_run "CREATE TABLE ri_nsd (tm time, lst timestamp[]) USING pgcolumnar;"
	nsd_out="$(psql_c "SELECT pgcolumnar.import_arrow('ri_nsd', '$NSDEEP');")"
	check "a time64 column and a nested list are counted too, not just a top-level timestamp" \
		"$(printf '%s' "$nsd_out" | grep -c 'columnar.import_arrow: 12 values lost sub-microsecond precision')" "1"
	check "and that file imports every row as well" \
		"$(q "SELECT count(*) FROM ri_nsd;")" "4"

	# THE CONTROLS: no report when nothing was lost.
	check "control: an ns file on microsecond boundaries reports nothing" \
		"$(printf '%s' "$nse_out" | grep -ci 'sub-microsecond')" "0"
	check "control: a microsecond file reports nothing" \
		"$(printf '%s' "$usp_out" | grep -ci 'sub-microsecond')" "0"

	# The conversion itself is unchanged: floor to the microsecond, not rounded
	# and not refused. 123ns past the boundary lands ON the boundary.
	check "the narrowed values are floored to the microsecond" \
		"$(q "SELECT string_agg(ts::text, ',' ORDER BY ts) FROM ri_nsl;")" \
		"2000-01-01 00:00:00,2000-01-01 00:00:01,2000-01-01 00:00:02,2000-01-01 00:00:03"
else
	echo "-- pyarrow not available; skipping nanosecond narrowing checks"
fi

# --- 4. error cases ---------------------------------------------------------
echo "-- argument validation"
psql_run "CREATE TABLE ri_heap (a bigint, b float8, c text) USING heap;"
expect_error "reject non-columnar target" "SELECT pgcolumnar.import_arrow('ri_heap', '$ARROW');"
psql_run "CREATE TABLE ri_wrong (a int) USING pgcolumnar;"
expect_error "reject column-count mismatch" "SELECT pgcolumnar.import_arrow('ri_wrong', '$ARROW');"

if [ "$have_pyarrow" = 1 ]; then
	DICTF="$PGC_WORKDIR/dict.arrows"
	python3 - "$DICTF" <<'PY'
import sys, pyarrow as pa, pyarrow.ipc as ipc
arr = pa.array(['a', 'b', 'a', 'b']).dictionary_encode()
t = pa.table({'c': arr})
with ipc.new_stream(pa.OSFile(sys.argv[1], 'wb'), t.schema) as w:
    w.write_table(t)
PY
	psql_run "CREATE TABLE ri_d (c text) USING pgcolumnar;"
	expect_error "reject dictionary-encoded file" "SELECT pgcolumnar.import_arrow('ri_d', '$DICTF');"

	# ---- temporal carriers and units (#864, #865) -----------------------------
	#
	# Arrow gives Date two carriers (DAY on 4 bytes, MILLISECOND on 8) and gives
	# Timestamp and Time four units each (s, ms, us, ns). The reader decoded every
	# one of them as though it were the single shape our own exporter writes, so a
	# VALID file in any other shape imported as a different, valid-looking value:
	#
	#     date64        2000-01-01  ->  4908285-05-04
	#     timestamp(s)  2000-01-01  ->  1970-01-01 00:15:46.6848
	#     timestamp(ms) 2000-01-01  ->  1970-01-11 22:58:04.8
	#     timestamp(ns) 2000-01-01  ->  31969-04-01
	#     time64(ns)    12:00:00    ->  12000:00:00
	#
	# These assert the VALUE, not that an error was raised. A wrong value cannot
	# satisfy them for an unrelated reason the way a deny arm can: a missing
	# fixture or a renamed table gives an empty result, not the right timestamp.
	# Every file below holds a value that is valid in its own unit, so nothing here
	# is testing overflow handling -- that is what the out-of-range arms are for.
	python3 - "$PGC_WORKDIR" <<'PY'
import os, struct, sys, pyarrow as pa, pyarrow.ipc as ipc
out = sys.argv[1]
S2000 = 946684800                      # 2000-01-01T00:00:00Z in seconds
NOON  = 12 * 3600                      # 12:00:00 in seconds
cases = {
    'tu_date32':  (pa.date32(),               S2000 // 86400),
    'tu_date64':  (pa.date64(),               S2000 * 1000),
    'tu_ts_s':    (pa.timestamp('s'),         S2000),
    'tu_ts_ms':   (pa.timestamp('ms'),        S2000 * 1000),
    'tu_ts_us':   (pa.timestamp('us'),        S2000 * 1000000),
    'tu_ts_ns':   (pa.timestamp('ns'),        S2000 * 1000000000),
    'tu_t64_us':  (pa.time64('us'),           NOON * 1000000),
    'tu_t64_ns':  (pa.time64('ns'),           NOON * 1000000000),
    'tu_t32_s':   (pa.time32('s'),            NOON),
    'tu_t32_ms':  (pa.time32('ms'),           NOON * 1000),
    # guard inputs: each is valid for its carrier and wrong for PostgreSQL
    'tu_ts_s_ovf':   (pa.timestamp('s'),  2**63 - 1),        # x1e6 overflows int64
    'tu_ts_ms_far':  (pa.timestamp('ms'), -300000000000000),  # far past, below MIN_TIMESTAMP
    'tu_d64_frac':   (pa.date64(),        86400001),         # not a whole day
    'tu_d64_neg':    (pa.date64(),        -1),               # 1969-12-31T23:59:59.999
    'tu_t64_ns_ovf': (pa.time64('ns'),    25 * 3600 * 10**9),# past midnight
    'tu_ts_ns_trunc':(pa.timestamp('ns'), S2000 * 10**9 + 1500),  # 1.5us past the epoch
    'tu_t64_ns_neg': (pa.time64('ns'),    -500),              # narrows to 0us: not midnight
    'tu_ts_ns_neg':  (pa.timestamp('ns'), -1500),             # 1.5us BEFORE the epoch
    # a temporal carrier whose tag no other temporal target can hold
    'tu_t64_plain':  (pa.time64('us'),    NOON * 10**6),
}
# #881 fixtures. These go through pa.array rather than the raw-byte packing
# below, because that builder derives its struct format from bit_width and can
# express neither a uint64 above 2^63, nor a float, nor a 128-bit decimal.
import decimal
extra = {
    'tu_u64': pa.array([2**63 + 5], pa.uint64()),
    'tu_i32': pa.array([7], pa.int32()),
    'tu_i64': pa.array([7], pa.int64()),
    'tu_f32': pa.array([1.5], pa.float32()),
    'tu_f64': pa.array([1.5], pa.float64()),
    'tu_d102': pa.array([decimal.Decimal('1.25')], pa.decimal128(10, 2)),
    'tu_d204': pa.array([decimal.Decimal('1.25')], pa.decimal128(20, 4)),
    # A WIDER fixed-size binary is the case the buffer-length check cannot see:
    # 32 bytes per row is more than uuid's 16, so the buffer is long enough and
    # the reader would take the first half of each value.
    'tu_fsb32': pa.array([b'0123456789abcdef0123456789abcdef'], pa.binary(32)),
    'tu_fsb16': pa.array([b'0123456789abcdef'], pa.binary(16)),
}
for name, arr in extra.items():
    tab = pa.table({'v': arr})
    with ipc.new_stream(pa.OSFile(os.path.join(out, name + '.arrows'), 'wb'), tab.schema) as w:
        w.write_table(tab)

for name, (typ, v) in cases.items():
    raw = struct.pack('<i' if typ.bit_width == 32 else '<q', v)
    arr = pa.Array.from_buffers(typ, 1, [None, pa.py_buffer(raw)])
    tab = pa.table({'v': arr})
    with ipc.new_stream(pa.OSFile(os.path.join(out, name + '.arrows'), 'wb'), tab.schema) as w:
        w.write_table(tab)
PY

	python3 - "$PGC_WORKDIR" <<'PY'
import os, sys, pyarrow as pa, pyarrow.ipc as ipc
out = sys.argv[1]
t = pa.table({'v': pa.array([946684800 * 10**6], pa.int64())})
with ipc.new_stream(pa.OSFile(os.path.join(out, 'tu_int64_ts.arrows'), 'wb'), t.schema) as w:
    w.write_table(t)
# Three date32 rows: a 12-byte data buffer. A decoder reading 8 bytes at stride 4
# runs off the end of it, which is what the width/kind cross-check prevents.
t = pa.table({'v': pa.array([10957, 10958, 10959], pa.date32())})
with ipc.new_stream(pa.OSFile(os.path.join(out, 'tu_d32_3.arrows'), 'wb'), t.schema) as w:
    w.write_table(t)
PY

	# Nested temporal carriers. The unit lives on the CHILD field, so these fail
	# unless the schema walk recurses. The struct puts its timestamp SECOND: a list
	# only ever reaches children[0], so it cannot catch an error in the child
	# vector's index arithmetic, and a struct at index 1 can.
	python3 - "$PGC_WORKDIR" <<'PY'
import os, sys, pyarrow as pa, pyarrow.ipc as ipc
out = sys.argv[1]
S2000 = 946684800
ns = S2000 * 10**9

lst = pa.array([[ns]], type=pa.list_(pa.timestamp('ns')))
with ipc.new_stream(pa.OSFile(os.path.join(out, 'tu_list_ns.arrows'), 'wb'),
                    pa.schema([pa.field('v', lst.type)])) as w:
    w.write_table(pa.table({'v': lst}))

st = pa.array([{'a': 7, 'b': ns}],
              type=pa.struct([('a', pa.int32()), ('b', pa.timestamp('ns'))]))
with ipc.new_stream(pa.OSFile(os.path.join(out, 'tu_struct_ns.arrows'), 'wb'),
                    pa.schema([pa.field('v', st.type)])) as w:
    w.write_table(pa.table({'v': st}))
PY

	tu_value() {	# tu_value FIXTURE PGTYPE -> the stored value, or the empty string
		psql_run "DROP TABLE IF EXISTS tu_t; CREATE TABLE tu_t (v $2) USING pgcolumnar;" \
			>/dev/null 2>&1
		psql_run "SELECT pgcolumnar.import_arrow('tu_t', '$PGC_WORKDIR/$1.arrows');" \
			>/dev/null 2>&1 || { echo "IMPORT-FAILED"; return; }
		q "SELECT v::text FROM tu_t LIMIT 1;"
	}

	# PREMISE. The unit our own exporter writes must still round-trip, or every
	# check below could pass on a reader that rejects everything.
	check "premise: the exporter's own timestamp unit still imports correctly" \
		"$(tu_value tu_ts_us timestamp)" "2000-01-01 00:00:00"
	check "premise: and its own time unit does too" \
		"$(tu_value tu_t64_us time)" "12:00:00"
	check "premise: and a date32 carrier, which shares Arrow's tag 8 with date64" \
		"$(tu_value tu_date32 date)" "2000-01-01"

	check "a date64 carrier decodes to the date it holds (#864)" \
		"$(tu_value tu_date64 date)" "2000-01-01"

	check "a timestamp in seconds decodes to the instant it holds (#865)" \
		"$(tu_value tu_ts_s timestamp)" "2000-01-01 00:00:00"
	check "a timestamp in milliseconds decodes to the instant it holds (#865)" \
		"$(tu_value tu_ts_ms timestamp)" "2000-01-01 00:00:00"
	check "a timestamp in nanoseconds decodes to the instant it holds (#865)" \
		"$(tu_value tu_ts_ns timestamp)" "2000-01-01 00:00:00"

	check "a time64 in nanoseconds decodes to the time it holds (#865)" \
		"$(tu_value tu_t64_ns time)" "12:00:00"
	check "a time32 in seconds decodes to the time it holds (#865)" \
		"$(tu_value tu_t32_s time)" "12:00:00"
	check "a time32 in milliseconds decodes to the time it holds (#865)" \
		"$(tu_value tu_t32_ms time)" "12:00:00"

	# ---- the guards the unit fix introduced -------------------------------
	#
	# Scaling a coarse unit up can leave the value outside what PostgreSQL can
	# store, and in C a signed overflow is undefined behaviour rather than a
	# wraparound, so each of these has to be refused before the multiply lands.
	# 22008 is datetime_field_overflow.
	tu_import_state() {	# tu_import_state FIXTURE PGTYPE -> SQLSTATE, 00000 on success
		psql_run "DROP TABLE IF EXISTS tu_t; CREATE TABLE tu_t (v $2) USING pgcolumnar;" \
			>/dev/null 2>&1
		local st
		st="$(sqlstate_or_hang "SELECT pgcolumnar.import_arrow('tu_t', '$PGC_WORKDIR/$1.arrows')")"
		[ -z "$st" ] && st=00000
		printf '%s\n' "$st"
	}

	check "premise: a well-formed import reports 00000, so the probe reads success" \
		"$(tu_import_state tu_ts_us timestamp)" "00000"

	check "a second count that overflows on scaling is refused, not wrapped" \
		"$(tu_import_state tu_ts_s_ovf timestamp)" "22008"
	# Far PAST, not far future. The two bounds are not symmetric here: exceeding
	# END_TIMESTAMP needs 9224318016000000000 microseconds, which is larger than
	# INT64_MAX, so for every unit the overflow guard fires before the range check
	# can. Only the lower bound is reachable, so only it can be asserted.
	check "a timestamp before PostgreSQL's range is refused" \
		"$(tu_import_state tu_ts_ms_far timestamp)" "22008"
	check "a time past midnight is refused" \
		"$(tu_import_state tu_t64_ns_ovf time)" "22008"

	# date64 is milliseconds, so an instant need not land on midnight. The date
	# it falls IN is the floor, and C division truncates toward zero: without a
	# floor correction a pre-epoch instant reports the day after the one it is in.
	check "a date64 instant mid-day reports the day it falls in" \
		"$(tu_value tu_d64_frac date)" "1970-01-02"
	check "a date64 instant before the epoch floors rather than truncating" \
		"$(tu_value tu_d64_neg date)" "1969-12-31"

	# PostgreSQL has no nanosecond timestamp, so sub-microsecond precision cannot
	# survive; truncating keeps the instant, where reading ns as us is 1000x wrong.
	check "a nanosecond timestamp truncates to microseconds" \
		"$(tu_value tu_ts_ns_trunc timestamp)" "2000-01-01 00:00:00.000001"

	# Narrowing floors rather than truncating toward zero, so an instant before
	# the epoch reports the microsecond and the day it is IN. Truncation would
	# move both of these forward, and would disagree with the date64 arm above.
	check "a nanosecond timestamp before the epoch floors rather than truncating" \
		"$(tu_value tu_ts_ns_neg timestamp)" "1969-12-31 23:59:59.999998"
	check "a negative nanosecond time is refused, not narrowed into midnight" \
		"$(tu_import_state tu_t64_ns_neg time)" "22008"

	# ---- the file's declared type must match the target's -------------------
	#
	# n->width is both the decode stride and the divisor in the row-count bounds
	# check, while each non-temporal decode arm reads a size fixed by its kind.
	# Taking a width from a tag the target does not share separates the two: a
	# Date-tagged field under a bigint node gave stride 4 to an arm reading 8,
	# which passed the bounds check and read past the body. Every arm below is
	# refused on unpatched main with XX001; these pin that it stays refused.
	for tu_target in bigint uuid time "numeric(20,4)" timestamp; do
		check "a date32 file is refused for a $tu_target column, not read past its buffer" \
			"$(tu_import_state tu_d32_3 "$tu_target")" "42804"
	done

	# The other direction, and the reason the refusal is by name rather than a
	# bounds error. These three were ACCEPTED on unpatched main, storing the low
	# 4 bytes of an 8-byte carrier as a plausible value: 687342-02-27,
	# 2722128-09-17, and 1970-01-11 22:58:04.8 respectively.
	check "a time64 file is refused for a date column, not stored as year 687342" \
		"$(tu_import_state tu_t64_plain date)" "42804"
	check "a timestamp file is refused for a date column" \
		"$(tu_import_state tu_ts_us date)" "42804"
	check "a date64 file is refused for a timestamp column" \
		"$(tu_import_state tu_date64 timestamp)" "42804"

	# ---- the file's own Int/Float/Decimal parameters (#881) ----------------
	#
	# The stride, the sign and the scale all came from the TARGET column, so a
	# file that declared something else was decoded as though it had not:
	#
	#     uint64 2^63+5      into bigint          -> -9223372036854775803
	#     int64  1,2,3,4     into int             -> 0,0,1,2
	#     decimal(10,2) 1.25 into numeric(20,4)   -> 0.0125
	#
	# Every one imported without an error. They are refused now. The controls
	# below are the matching files, which must still import, or the refusal is
	# just "no Arrow file works".
	check "a uint64 file is refused for bigint (#881)" \
		"$(tu_import_state tu_u64 bigint)" "42804"
	check "an int64 file is refused for a 4-byte int (#881)" \
		"$(tu_import_state tu_i64 int)" "42804"
	check "an int32 file is refused for bigint (#881)" \
		"$(tu_import_state tu_i32 bigint)" "42804"
	check "a float32 file is refused for float8 (#881)" \
		"$(tu_import_state tu_f32 float8)" "42804"
	check "control: a matching int64 file imports into bigint" \
		"$(tu_import_state tu_i64 bigint)" "00000"
	check "control: a matching int32 file imports into int" \
		"$(tu_import_state tu_i32 int)" "00000"
	check "control: a matching float64 file imports into float8" \
		"$(tu_import_state tu_f64 float8)" "00000"
	check "control: a matching float32 file imports into float4" \
		"$(tu_import_state tu_f32 float4)" "00000"
	# A NARROWER carrier is already caught by the buffer-length check, so it
	# cannot tell this guard from its absence. A WIDER one can: the buffer is
	# long enough and the reader would silently take the first 16 bytes.
	check "a wider fixed-size binary is refused for uuid (#881)" \
		"$(tu_import_state tu_fsb32 uuid)" "42804"
	check "control: a 16-byte fixed-size binary imports into uuid" \
		"$(tu_import_state tu_fsb16 uuid)" "00000"
	check "a decimal at another scale is refused (#881)" \
		"$(tu_import_state tu_d102 'numeric(20,4)')" "42804"
	check "control: a matching decimal imports" \
		"$(tu_import_state tu_d204 'numeric(20,4)')" "00000"

	# A refusal must leave nothing behind: an arm that only reads the SQLSTATE
	# would pass for a fix that errored after writing the row.
	tu_import_state tu_u64 bigint >/dev/null
	check "and a refused import leaves the target empty" \
		"$(q "SELECT count(*) FROM tu_t;")" "0"

	# A NON-temporal tag is still left alone: an int64 file keeps importing into
	# a timestamp column as raw microseconds, exactly as it did before.
	check "control: a non-temporal tag is not caught by the temporal refusal" \
		"$(tu_import_state tu_int64_ts timestamp)" "00000"

	# ---- the unit on a nested field ---------------------------------------
	check "a list element's nanosecond unit is honoured, not just the top level" \
		"$(tu_value tu_list_ns 'timestamp[]')" "{\"2000-01-01 00:00:00\"}"

	psql_run "DROP TYPE IF EXISTS tu_st CASCADE; CREATE TYPE tu_st AS (a int, b timestamp);" \
		>/dev/null 2>&1
	check "a struct field's nanosecond unit is honoured at child index 1" \
		"$(tu_value tu_struct_ns tu_st)" "(7,\"2000-01-01 00:00:00\")"
fi

IXFILE="$PGC_WORKDIR/ix_roundtrip.arrows"

# ---------------------------------------------------------------------------
# Import into a table that already has indexes (issue #153).
#
# table_tuple_insert writes the row and nothing else: index maintenance is the
# executor's job, and an importer calling it directly has to do that itself.
# When it did not, the rows landed in the table and in no index, so an index
# scan returned nothing while a sequential scan returned everything, and a
# unique index accepted the same key twice. Nothing errored either time.
#
# The checks below compare the two access paths against each other on the same
# predicate, which is the shape that catches it: a count that only ever uses one
# path passes whether or not the index was maintained.
# ---------------------------------------------------------------------------

psql_run "CREATE TABLE ix_src (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO ix_src SELECT g, 'v' || g FROM generate_series(1, 3000) g;"
psql_run "SELECT pgcolumnar.export_arrow('ix_src', '$IXFILE');"

psql_run "CREATE TABLE ix_tgt (id int, v text) USING pgcolumnar;"
psql_run "CREATE INDEX ix_tgt_id ON ix_tgt (id);"
psql_run "SELECT pgcolumnar.import_arrow('ix_tgt', '$IXFILE');"

# q echoes one line per statement, so take the last: the count, not the SETs.
#
# enable_seqscan does not discourage a custom scan, so turning it off on its own
# leaves the columnar scan the cheapest path and the index is never consulted.
# That made an earlier version of this check pass against a build with index
# maintenance deliberately removed. pgcolumnar.enable_custom_scan is what takes
# the columnar path out of the running, and the plan is asserted below so a
# future costing change cannot quietly put it back.
IDX_SETUP="SET enable_seqscan = off; SET pgcolumnar.enable_custom_scan = off;"
idx_count() {  # force an index scan
	q "$IDX_SETUP
	   SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199;" | tail -1
}
idx_plan_is_index_scan() {
	q "$IDX_SETUP
	   EXPLAIN (COSTS OFF) SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199;" \
		| grep -qi 'Index.*Scan' && echo yes || echo no
}
seq_count() {  # force a sequential scan
	q "SET enable_indexscan = off; SET enable_bitmapscan = off;
	   SET enable_indexonlyscan = off;
	   SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199;" | tail -1
}

check "the index-scan check really uses the index" "$(idx_plan_is_index_scan)" "yes"
check "import into an indexed table: both scans agree" "$(idx_count)" "$(seq_count)"
check "import into an indexed table: the rows are there" "$(seq_count)" "100"
check "import into an indexed table: a point lookup finds its row" \
	"$(q "$IDX_SETUP
	      SELECT count(*) FROM ix_tgt WHERE id = 2222;" | tail -1)" "1"

# A unique index must police the second import rather than take it.
psql_run "CREATE TABLE ix_uq (id int, v text) USING pgcolumnar;"
psql_run "CREATE UNIQUE INDEX ix_uq_id ON ix_uq (id);"
psql_run "SELECT pgcolumnar.import_arrow('ix_uq', '$IXFILE');"
expect_error "importing the same keys twice raises unique_violation" \
	"SELECT pgcolumnar.import_arrow('ix_uq', '$IXFILE');"
check "the failed second import left the row count alone" \
	"$(q 'SELECT count(*) FROM ix_uq;')" "3000"

# A partial index must receive only the rows its predicate covers.
psql_run "CREATE TABLE ix_part (id int, v text) USING pgcolumnar;"
psql_run "CREATE INDEX ix_part_id ON ix_part (id) WHERE id <= 500;"
psql_run "SELECT pgcolumnar.import_arrow('ix_part', '$IXFILE');"
check "partial index covers its rows after import" \
	"$(q "$IDX_SETUP
	      SELECT count(*) FROM ix_part WHERE id BETWEEN 100 AND 199;" | tail -1)" "100"

# --- #686/#644: import_arrow must refuse a FIFO, not hang the backend ---------
# fopen("rb") on a FIFO blocks in open(2); with SA_RESTART the block survives a
# cancel/statement_timeout. RED (unguarded) is a wall-clock HANG; GREEN is XX001.
echo "-- FIFO refusal"
psql_run "CREATE TABLE ia_fifo (a int) USING pgcolumnar;"
IAFIFO="$PGC_WORKDIR/ia.fifo"; mkfifo "$IAFIFO"
check "import_arrow on a FIFO is refused, not a hang (XX001)" \
	"$(sqlstate_or_hang "SELECT pgcolumnar.import_arrow('ia_fifo', '$IAFIFO')")" \
	"XX001"; fifo_release "$IAFIFO"
check "backend still up after the import_arrow FIFO refusal" "$(q 'SELECT 1')" "1"

pgc_summary

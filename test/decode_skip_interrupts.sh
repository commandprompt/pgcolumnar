#!/usr/bin/env bash
#
# pgColumnar skip-path interrupt discipline (Thrift + Avro).
#
# The decode_interrupts.sh suite covers the native column decoders. This covers
# the two *skip* loops that walk unconsumed fields of an attacker-authored file:
#
#   1. PgColumnarThriftSkip (columnar_thrift.c) skips every unrecognised field of
#      a Parquet footer. Its TC_LIST/TC_SET arm loops `size` times, and `size` is
#      a file-controlled varint up to 0xFFFFFFFF; TC_BOOL_TRUE/TC_BOOL_FALSE and
#      the default arm consume ZERO bytes and never set r->error. So a footer with
#      one list<bool> of 0xFFFFFFFF drives ~4.3 billion no-op iterations, and a
#      struct holding K such lists multiplies that without bound -- all from a
#      sub-2 KB file. The function had check_stack_depth() (recursion) but no
#      CHECK_FOR_INTERRUPTS, so the loop ignored SIGINT and statement_timeout: an
#      uninterruptible backend hang, the #686 FIFO-hang class on the footer path.
#      Since #1822 a bool element of a list takes its one byte and an invalid type
#      is refused, so every skipped value consumes a byte or stops the parse. The
#      bomb now runs off the end of its footer and is refused as corrupt in
#      milliseconds; the interrupt check stays, and the shape arm pins it.
#
#   2. av_skip (columnar_avro.c) skips unconsumed Avro manifest fields. Its
#      interrupt check sat at block granularity; an array<null> block declares a
#      count up to AV_MAX_OBJECTS (50,000,000) and AV_NULL consumes zero bytes, so
#      one block ran 50M no-op skips with no per-element check. The AV_RECORD
#      field loop had none at all.
#
# Both are reachable today by an honest caller who names a hostile file:
# parquet_schema()/read_parquet() for (1), read_manifest/iceberg_scan for (2).
#
# This suite asserts the fix two ways. The functional arm crafts the Thrift bomb
# and wants it refused as corrupt within 3 s. Since #1822 that refusal comes from
# the footer's end, not from the 1 s statement_timeout, so no test now runs the
# Thrift skip's interrupt check: a hostile footer can make the skip loop only as
# long as the footer itself. The shape arm (like decode_interrupts.sh /
# wal_envelope.sh) pins the CHECK_FOR_INTERRUPTS into both skip bodies, so an edit
# that drops either fails here without depending on timing.
#
# Removal proof: drop either interrupt check and the matching shape check goes to
# 0. Skip a bool list element as zero bytes again and the functional check reads
# 57014 instead of the corrupt-footer refusal.
#
# Usage:  test/decode_skip_interrupts.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

W="$PGC_WORKDIR"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

# --- craft the Thrift skip-bomb footer ----------------------------------------
# Before #1822 this spun until the 1 s statement_timeout cancelled it (57014). Now
# each bool element consumes a byte, so the 256 lists of 4.3e9 bools run off the
# end of a ~1.8 KB footer and the parse is refused at once.
# A Parquet file is PAR1 + FileMetaData + <uint32 metalen> + PAR1; the crafted
# metadata is what parse_file_metadata walks. No writer produces this, so it is
# built by hand (as native_parquet_stack.sh does).
python3 - "$W" <<'PY'
import struct, sys
W = sys.argv[1]

def uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b); break
    return bytes(out)

def wrap(meta):
    return b'PAR1' + meta + struct.pack('<I', len(meta)) + b'PAR1'

# 0x1c  : top-level field header (delta 1, type TC_STRUCT=12) -> the else-arm of
#         parse_file_metadata routes it to PgColumnarThriftSkip(TC_STRUCT), which
#         loops reading inner fields.
# per inner field:
#   0x19 : field header (delta 1, type TC_LIST=9)
#   0xf1 : list header -- size nibble 0x0f (long form) | element type
#          TC_BOOL_TRUE=1; a bool element consumes ZERO bytes when skipped.
#   uleb(0xFFFFFFFF) : the long-form element count, ~4.3e9.
# K such lists inside the struct => K * 4.3e9 zero-byte skips before the struct
# STOP is ever reached. K=256 is an effectively unbounded (hour-scale) loop from
# ~1.8 KB, so on unfixed code the KILL fires long before it could finish.
BOMB = 0xFFFFFFFF
K = 256
meta = bytearray(b'\x1c')
meta += (b'\x19\xf1' + uleb(BOMB)) * K
meta += b'\x00'   # struct STOP
meta += b'\x00'   # FileMetaData STOP
open(f"{W}/skip_bomb.parquet", "wb").write(wrap(bytes(meta)))
PY

# probe FILE -> "SQLSTATE: message|ms" of the error parquet_schema(FILE) raised and
# how long after the statement began it was raised, NOERR, or HANG (the backend
# never yielded and the KILL wrapper terminated the client). statement_timeout is
# small; the KILL cap is far larger, so on fixed code the cancel lands well inside it.
probe() {
	local out rc
	# query_canceled (57014) must be caught by name: PL/pgSQL's WHEN OTHERS
	# deliberately does NOT trap QUERY_CANCELED, so a WHEN OTHERS handler would let
	# the timeout propagate uncaught and print nothing to match here.
	out="$(timeout -s KILL 30 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 \
		-p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -c "SET statement_timeout='1s';
		DO \$\$ DECLARE t0 timestamptz := statement_timestamp(); BEGIN
		PERFORM count(*) FROM pgcolumnar.parquet_schema('$1');
		RAISE NOTICE 'NOERR';
		EXCEPTION WHEN query_canceled OR OTHERS THEN RAISE NOTICE 'R=%: %|%', SQLSTATE, SQLERRM,
		round(extract(epoch FROM clock_timestamp() - t0) * 1000); END \$\$;" 2>&1)"
	rc=$?
	# coreutils timeout reports 124 when the command times out (128+9=137 only
	# with --preserve-status); either means the client was killed because the
	# backend never yielded -- an uninterruptible hang.
	if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then echo "HANG"; return; fi
	echo "$out" | grep -m1 -oE 'R=.*|NOERR' | sed 's/^R=//'
}
# A liveness probe alone passes once crash recovery finishes (a backend that
# aborted inside the list arm passed 3 runs of 3, #1341), so survival also requires
# that no backend has died since the scenario started, read from the server log.
crashes() {
	# a number, or nothing when the log can't be read: a blind count must not read
	# as "no crashes"
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
alive() {	# since CRASHES_BEFORE
	# the postmaster logs a death when it reaps the backend: wait for the server to
	# answer before counting, so the count includes the crash it is for
	local _i
	for _i in $(seq 60); do [ "$(q 'SELECT 1;' 2>/dev/null)" = "1" ] && break; sleep 1; done
	local _now; _now="$(crashes)"
	if ! [[ "$CRASHES_BEFORE" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then
		echo "no (crash count unreadable)"
	elif [ "$(q 'SELECT 1;')" = "1" ] && [ "$_now" = "$CRASHES_BEFORE" ]; then
		echo yes
	else
		echo "no ($(( _now - CRASHES_BEFORE )) backend crashes)"
	fi
}

CRASHES_BEFORE="$(crashes)"
P="$(probe "$W/skip_bomb.parquet")"
echo "probe: $P"
# The refusal must be the footer parser's own: a 57014 here would mean the skip
# still walked the lists until the timeout, as it did before #1822. Before the
# interrupt check this input spun uninterruptibly until the KILL (HANG).
check "Thrift skip-bomb footer is refused as corrupt, not an uninterruptible hang" \
	"${P%%|*}" "0A000: could not parse \"$W/skip_bomb.parquet\" (unsupported or corrupt Parquet metadata)"
# The answer must come within 3 s under a 1 s timeout: a skip that walks the lists
# again would take until the timeout or the 30 s KILL.
ms="${P##*|}"
check "the skip-bomb answer lands within 3 s of the statement's start under a 1 s timeout" \
	"$( [[ "$P" == *"|"* && "$ms" =~ ^[0-9]+$ ]] && { [ "$ms" -lt 3000 ] && echo yes || echo "no (${ms} ms)"; } || echo "no ($P)")" "yes"
check "backend survived the Thrift skip-bomb footer" "$(alive)" "yes"

# --- shape arm: the interrupt check must be present in the skip body ----------
TF="$SRC/columnar_thrift.c"
AF="$SRC/columnar_avro.c"
# Comments are not code: with the call written as /* CHECK_FOR_INTERRUPTS(); */ the
# counts below passed (#1341). Each comment is blanked to its newlines first, so the
# function's lines and their order are kept.
nocomments() {
	perl -0777 -pe 's{/\*.*?\*/}{"\n" x ($& =~ tr/\n//)}gse; s{//[^\n]*}{}g' "$1"
}

# Before the switch, so every value skipped -- each list element is one Skip call --
# passes it; counting anywhere in the body passed with the check moved into the
# struct arm (per field, not per element).
check "PgColumnarThriftSkip checks for interrupts" \
	"$(nocomments "$TF" | awk '/^PgColumnarThriftSkip\(/{f=1} f&&/switch \(ftype\)/{print n+0; exit} f&&/CHECK_FOR_INTERRUPTS/{n++}')" \
	"1"

# av_skip is guarded by shape rather than a live cancel, deliberately. Its unbounded
# arm is one array<null> block, capped at AV_MAX_OBJECTS (50M) zero-byte skips ~ a
# low-seconds window; a statement_timeout test on a window that small is unreliable
# (the same reason decode_interrupts.sh is a shape test). The reachable-now behaviour
# of this whole skip-loop class is proven functionally above via the Thrift bomb.
#
# Placement matters: av_skip already had a CHECK_FOR_INTERRUPTS in the AV_ARRAY and
# AV_MAP arms, but per BLOCK (before the count), not per element -- the inner element
# loop ran uninterrupted. So counting checks anywhere in av_skip would pass on the
# unguarded code. Instead assert a check exists BEFORE the switch, i.e. on the
# per-call path every element and record field takes. That reads 0 on the old code
# (its checks are inside the switch arms) and 1 once the per-element check is added.
check "av_skip checks for interrupts per element (before the switch, not just per block)" \
	"$(nocomments "$AF" | awk '/^av_skip\(/{f=1} f&&/switch \(s->kind\)/{print n+0; exit} f&&/CHECK_FOR_INTERRUPTS/{n++}')" \
	"1"

pgc_summary

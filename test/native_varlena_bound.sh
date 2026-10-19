#!/usr/bin/env bash
#
# pgColumnar bounds a varlena value's stored length against its buffer.
#
# A varying-length value in a native chunk (and in a zone map's min/max) carries
# its own length prefix, which the decoder trusted: PgColumnarDecodeValue,
# pgcolumnar_skip_value, and pgcolumnar_build_val_offsets read
# PgColumnarVarSizeAnyUnaligned and memcpy that many bytes with no check that the
# value fits its buffer. A corrupt chunk or catalog row (bit rot, a hand-written
# stripe) can then declare a value that runs past the buffer -- an out-of-bounds
# read -- and a header carrying the external-TOAST tag is later detoasted through
# a pointer to nothing. This is the trusted-storage boundary core PostgreSQL does
# not defend for a heap page either, but the reader aspires to (see corruption.sh
# / hardening.sh), and a clean error beats a crash.
#
# Every decode site now passes the value stream's end and reads through
# PgColumnarVarSizeAnyUnalignedBounded, which refuses a length (or an external
# tag) that would read past it. The zone map's min/max is a catalog bytea, so it
# is corruptible with a plain UPDATE (unlike the relation-file value stream),
# which makes this reachable and testable: point a min at a 5-byte value whose
# 4-byte header claims ~1 GB, then a predicate that consults it.
#
# RED (unbounded) is a ~1 GB memcpy from a 5-byte buffer -- a SIGSEGV that takes
# the backend (and the cluster) down. GREEN is a clean DATA_CORRUPTED (XX001) and
# a surviving backend. The other decode sites (the sequential scan, the per-row
# fetch, the vector aggregate) share the one bounded reader, so they are bounded
# by the same fix; the min/max path is the one a catalog UPDATE can reach.
#
# Reproducing the removal proof (two traps that both come back falsely GREEN):
#   - Do a fresh (make clean) build, NOT PGC_SKIP_BUILD=1. The bounded reader is a
#     header inline; PGXS does not track header deps, so a skip build tests the
#     old .so and the gutted guard never takes effect (gate-environment stale-.so).
#   - Poison column_index=1 (txt), not 0. Column 0 is the int id and has no varlena
#     min/max, so corrupting its zone map does not reach the varlena decoder.
# With a clean build and column 1 poisoned, the ungutted bound gives XX001; the
# gutted one turns the ~1 GB claim into a memcpy that SIGSEGVs the backend.
#
# Usage:  test/native_varlena_bound.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
q "CREATE TABLE t (id int, txt text) USING pgcolumnar;
   INSERT INTO t SELECT g, 'v'||g FROM generate_series(1,2000) g;" >/dev/null
SID="$(q "SELECT pgcolumnar.get_storage_id('t');")"

check "the txt column has a whole-chunk zone map to corrupt" \
	"$(q "SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;")" \
	"1"

# 0xfcffffff is a 4-byte varlena header claiming (0x3fffffff) ~1 GB; the value is
# only 5 bytes. errors() runs the query; SQLSTATE / survival are asserted below.
errcode() { # SQL -> the 5-char SQLSTATE it raises, or NOERR
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qtA -v VERBOSITY=sqlstate -c "$1" 2>&1 \
		| sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
refusal() { # SQL -> "SQLSTATE: message" of the error it raises, or NOERR's empty
	# the SQLSTATE alone cannot tell which bound refused: with the header bound gone,
	# the length check after it raises the same XX001 (#1524 review)
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qtA -v VERBOSITY=verbose -c "$1" 2>&1 \
		| sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)/\1: \2/p' | head -1
}
# A liveness probe alone passes once crash recovery finishes, so survival also
# requires that no backend has died since its scenario started, read from the server
# log rather than from timing (corruption.sh measured the same hole).
crashes() {
	# a number, or nothing when the log can't be read: grep -c prints 0 itself when
	# nothing matches (and exits 1), but nothing at all for a file it cannot open,
	# and a blind count must not read as "no crashes"
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
alive() {	# since this scenario's corruption, which notes the count
	# a crash puts the server through recovery: wait it out, so the next scenario
	# starts on a server that answers and one crash cannot fail the checks after it
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
# the zone map's untouched bounds, to put back: native_zone_excludes decodes the
# minimum BEFORE the maximum, so a corrupt minimum left in place answers for the
# maximum's check, which then never reaches the maximum
ORIG_MIN="$(q "SELECT encode(minimum, 'hex') FROM pgcolumnar.zone_map
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;")"
ORIG_MAX="$(q "SELECT encode(maximum, 'hex') FROM pgcolumnar.zone_map
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;")"

# --- corrupt the MINIMUM ------------------------------------------------------
q "UPDATE pgcolumnar.zone_map SET minimum = E'\\\\xfcffffff00'::bytea
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
CRASHES_BEFORE="$(crashes)"
check "a min with an over-long varlena length is refused, not an OOB read (XX001)" \
	"$(errcode "SELECT count(*) FROM t WHERE txt > 'a';")" "XX001"
check "backend survived the corrupt minimum" "$(alive)" "yes"

# --- restore, then corrupt the MAXIMUM ---------------------------------------
q "UPDATE pgcolumnar.zone_map SET minimum = decode('$ORIG_MIN', 'hex')
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
# put back from a saved hex: an empty or wrong restore would leave the minimum
# corrupt, and its XX001 would answer for the maximum
check "with the minimum restored, the range read is clean" \
	"$(q "SELECT count(*) FROM t WHERE txt < 'zzz';")" "2000"
q "UPDATE pgcolumnar.zone_map SET maximum = E'\\\\xfcffffff00'::bytea
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
CRASHES_BEFORE="$(crashes)"
check "a max with an over-long varlena length is refused, not an OOB read (XX001)" \
	"$(errcode "SELECT count(*) FROM t WHERE txt < 'zzz';")" "XX001"
check "backend survived the corrupt maximum" "$(alive)" "yes"

# --- an external-TOAST tag in the raw value stream is refused -----------------
# 0x01 is the 1-byte external-TOAST tag; native storage never holds one, and
# detoasting it would deref a bogus pointer. VARSIZE alone would accept it.
# The maximum is restored first, so only the tagged minimum is wrong.
q "UPDATE pgcolumnar.zone_map SET maximum = decode('$ORIG_MAX', 'hex')
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
q "UPDATE pgcolumnar.zone_map SET minimum = E'\\\\x0102030405060708090a0b0c0d0e0f1011'::bytea
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
CRASHES_BEFORE="$(crashes)"
check "an external-TOAST-tagged min value is refused (XX001)" \
	"$(errcode "SELECT count(*) FROM t WHERE txt > 'a';")" "XX001"
check "backend survived the external-tagged minimum" "$(alive)" "yes"

# --- the boundary: a length that fits exactly, and one that runs one byte past --
# A length far past the stream cannot tell a correct bound from an off-by-one,
# and a one-byte overread is what the bound exists to stop (#1524 review). 0x07 is
# a 1-byte header saying 3 bytes, header included: 'v1', exactly the stream. 0x09
# says 4, one past it. Then 3 bytes whose first is a 4-byte header: the header
# itself runs past the stream end, read by its message: the length check after the
# header bound raises the same XX001. (The header bytes are little-endian's layout.)
q "UPDATE pgcolumnar.zone_map SET minimum = E'\\\\x077631'::bytea
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
check "a min whose length exactly fits its buffer reads cleanly" \
	"$(q "SELECT count(*) FROM t WHERE txt > 'a';")" "2000"
q "UPDATE pgcolumnar.zone_map SET minimum = E'\\\\x097631'::bytea
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
check "a min whose length runs one byte past its buffer is refused (XX001)" \
	"$(errcode "SELECT count(*) FROM t WHERE txt > 'a';")" "XX001"
q "UPDATE pgcolumnar.zone_map SET minimum = E'\\\\x0c0000'::bytea
   WHERE storage_id=$SID AND column_index=1 AND vector_index=-1;" >/dev/null
check "a 4-byte header cut off by the buffer's end is refused (XX001)" \
	"$(refusal "SELECT count(*) FROM t WHERE txt > 'a';")" \
	"XX001: pgcolumnar: varlena header runs past the value stream end"

pgc_summary

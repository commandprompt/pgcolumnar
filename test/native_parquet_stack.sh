#!/usr/bin/env bash
#
# pgColumnar Parquet recursion-depth hardening suite.
#
# The footer parser has two recursions whose depth is taken directly from the
# file, so a crafted footer can drive the C stack into its guard page and
# SIGSEGV the backend. A backend segfault is a crash: the postmaster terminates
# every other session and reinitializes the cluster, so this is a whole-cluster
# denial of service from one input file, not a single-statement error.
#
# The two recursions are independent and each needs its own guard -- one does not
# cover the other, because they are reached at different points:
#
#   1. PgColumnarThriftSkip recurses through nested structs (and lists of structs).
#      Every unrecognised metadata field is skipped through it, so it is reached
#      straight from the footer bytes, before any schema is interpreted. A footer
#      that is just N struct openers nests N deep.
#
#   2. walk_schema recurses through the schema tree, once per nesting level, with
#      num_children read straight from the footer. This runs only AFTER the footer
#      parses, so a footer that never trips (1) reaches it. A schema that chains a
#      group inside a group inside a group descends here as deep as the file says.
#
# Both are guarded with check_stack_depth(), which turns the crash into a caught
# ERROR (SQLSTATE 54001, statement_too_complex). That each guard is load-bearing
# for its own vector was proven by removal at the depths that actually overflow an
# 8 MB stack: with only the PgColumnarThriftSkip guard present a schema chain still
# SIGSEGVs, and with only the walk_schema guard present a nested-struct footer
# still SIGSEGVs.
#
# This suite is the regression for that, and deliberately does NOT overflow the
# real C stack: it pins max_stack_depth to its minimum so a ~20 KB fixture trips
# the guard on any build in the matrix. At that fixture depth, removing a guard
# makes its check fail by returning the wrong answer (NOERR, or a parse error)
# rather than by crashing. That is the intended trade: a harness that proved
# itself by segfaulting a backend could not be run in the matrix at all.
#
# Run on an assert-enabled build, so if a guard is ever removed the crash it lets
# through is unmistakable.
#
# Usage:  test/native_parquet_stack.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

W="$PGC_WORKDIR"

# Craft the footers by hand -- no pyarrow, because these files are deliberately
# malformed in a way no writer produces. A Parquet file is PAR1 + FileMetaData +
# <uint32 metalen> + PAR1; the crafted metadata is what the parser walks.
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
            out.append(b)
            break
    return bytes(out)

def wrap(meta):
    return b'PAR1' + meta + struct.pack('<I', len(meta)) + b'PAR1'

# Vector 1: N nested struct openers. 0x1c is a compact-protocol field header
# meaning (delta 1, type TC_STRUCT) -- "open a struct". N of them nest N deep
# through PgColumnarThriftSkip. This never parses a schema; it crashes on the
# top-level field skip.
def nested(n):
    return wrap(b'\x1c' * n)

# Vector 2: a schema list of N elements forming a linear chain -- each a group
# with exactly one child, the last a leaf. This parses cleanly (the elements are
# read in a flat loop), then walk_schema descends the chain N-1 deep.
#   0x29        FileMetaData field 2 (schema), type TC_LIST
#   0xfc + ulen list header: long-form size, element type TC_STRUCT
#   0x48 0x01 g per element: field 4 (name) = "g", a required field (#1852)
#   0x15 0x02   then field 5 (num_children) = zigzag(1), i.e. one child
#   0x00        element STOP
#   leaf        0x48 0x01 x 0x00: named "x", num_children defaults to 0
#   0x16 0x00   FileMetaData field 3 (num_rows) = 0, a required field (#1852)
#   0x19 0x0c   field 4 (row_groups), an empty list, a required field (#1852)
#   final 0x00  FileMetaData STOP
def walk_chain(n):
    m = bytearray(b'\x29\xfc')
    m += uleb(n)
    m += b'\x48\x01g\x15\x02\x00' * (n - 1)
    m += b'\x48\x01x\x00'  # leaf element
    m += b'\x16\x00\x19\x0c'  # num_rows 0, no row groups
    m += b'\x00'          # FileMetaData STOP
    return wrap(bytes(m))

# Deep enough to overflow max_stack_depth='100kB' with a wide margin on any build
# (the guard trips near depth 2000 there; 20000 is ~10x that), yet only tens of KB.
open(f"{W}/stack_nested.parquet", "wb").write(nested(20000))
open(f"{W}/stack_walk.parquet",   "wb").write(walk_chain(20000))
# A shallow chain on the SAME walk_schema path: the guard must not reject valid
# nesting. One group, one leaf -> parquet_schema reports its single leaf column.
open(f"{W}/stack_walk_ok.parquet", "wb").write(walk_chain(2))
PY

# Survival reads the server log, not a bare SELECT 1, which answers again once crash
# recovery is over (jdatcmd's #1708 review: with an abort() in the Thrift guard and a 3 s
# delay, both survival checks after the aborts passed). Each arm takes its own baseline
# right before its trigger, and every reading first waits for the server, so a crash
# fails only its own arm and its own survival check (the pattern of #1701 and #1705).
wait_up() { local _i; for _i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && return; sleep 0.5; done; }
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since BEFORE
	local _up _now
	wait_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"; return; fi
	if [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1; return; fi
	echo "no ([$_up], $(( _now - $1 )) backend crashes)"
}

# sqlstate QUERY -> the SQLSTATE of the error it raises, or 'NOERR'. A DO block so
# the error is caught in-session and the connection is never actually torn down by
# a controlled error (only a real crash would drop it, which the survival check sees).
sqlstate() {
	wait_up
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "SET max_stack_depth='100kB';
		DO \$\$ BEGIN PERFORM count(*) FROM pgcolumnar.parquet_schema('$1');
		RAISE NOTICE 'NOERR';
		EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'SQLSTATE=%', SQLSTATE; END \$\$;" 2>&1 \
		| grep -m1 -oE 'SQLSTATE=[0-9A-Z]+|NOERR' | sed 's/SQLSTATE=//'
}

# --- vector 1: nested Thrift structs ------------------------------------------
# 54001 is statement_too_complex, what check_stack_depth raises. Pinning it rather
# than accepting any error means an unrelated failure cannot pass this: before the
# guard, this input did not raise 54001, it SIGSEGV'd.
B="$(crashes)"
check "nested-struct footer raises stack-depth, not SIGSEGV" \
	"$(sqlstate "$W/stack_nested.parquet")" "54001"
check "backend survived the nested-struct footer" "$(survived "$B")" "1"

# --- vector 2: nested schema tree (the one a single guard misses) -------------
B="$(crashes)"
check "schema-chain footer raises stack-depth, not SIGSEGV" \
	"$(sqlstate "$W/stack_walk.parquet")" "54001"
check "backend survived the schema-chain footer" "$(survived "$B")" "1"

# --- the guard must not reject valid nesting ----------------------------------
# The same walk_schema path, one level deep, must still describe the file. If the
# guard were keyed on nesting rather than on stack exhaustion this would fail.
B="$(crashes)"
wait_up
check "a shallow valid schema chain still reads" \
	"$(q "SELECT count(*) FROM pgcolumnar.parquet_schema('$W/stack_walk_ok.parquet');")" "1"
check "backend survived the valid file" "$(survived "$B")" "1"

# --- the same parser backs read_parquet, so the guard covers it too -----------
# read_parquet reaches the identical footer walk. It must reject the crash input
# the same way rather than becoming a way around the guard.
B="$(crashes)"
wait_up
check "read_parquet rejects the nested-struct footer the same way" \
	"$(errs_state="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "SET max_stack_depth='100kB';
		DO \$\$ BEGIN PERFORM count(*) FROM pgcolumnar.read_parquet('$W/stack_nested.parquet') AS t(c int);
		EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'SQLSTATE=%', SQLSTATE; END \$\$;" 2>&1 \
		| grep -m1 -oE 'SQLSTATE=[0-9A-Z]+' | sed 's/SQLSTATE=//')"; echo "$errs_state")" "54001"
check "backend survived read_parquet on the crash input" "$(survived "$B")" "1"

pgc_summary

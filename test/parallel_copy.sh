#!/usr/bin/env bash
#
# pgColumnar #300: parallel bulk ingest.
#
# Phase 1 covers the file range splitter (pgcolumnar.file_split_offsets): given a
# text file and a worker count N, it returns N+1 byte offsets that partition the
# file into N record-aligned ranges. The parallel loader hands range
# [off[i], off[i+1]) to worker i, so the splitter is only correct if:
#
#   1. structure: off[0]=0, off[N]=filesize, offsets non-decreasing, and every
#      interior offset sits immediately after a newline (a real record boundary);
#   2. coverage + reconstruction: COPYing each range separately loads exactly the
#      same rows as one COPY of the whole file -- no record split, dropped, or
#      duplicated across ranges.
#
# The heap mirror is the oracle: the union of the per-range loads must equal a
# single COPY of the file. Covers many worker counts (including more workers than
# rows -> empty ranges), a file with no trailing newline, and a single row.
#
# Usage:  test/parallel_copy.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Atomic parallel_copy prepares one transaction per worker and runs a coordinator
# bgworker plus N loader bgworkers, so the cluster needs 2PC capacity and enough
# worker slots. max_prepared_transactions is PGC_POSTMASTER, so it must be set
# before the cluster starts (pgc_setup reads PGC_EXTRA_CONF into postgresql.conf).
export PGC_EXTRA_CONF=$'max_prepared_transactions=8\nmax_worker_processes=16'

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

DATADIR="$PGC_WORKDIR/pcopy"
mkdir -p "$DATADIR"; chmod 777 "$DATADIR"
if [ "$(id -u)" = "0" ]; then chown postgres "$DATADIR"; fi

# server-side path helper: write a file via COPY TO (owned by the server) so
# file_split_offsets can read it, then dd out byte ranges as needed.
make_file() {	# rows -> echoes the server path
	local rows="$1"
	local f="$DATADIR/data_${rows}.txt"
	psql_run "COPY (SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt
	                FROM generate_series(1, $rows) g)
	          TO '$f' WITH (FORMAT text);" >/dev/null
	printf '%s\n' "$f"
}

# offsets array text -> bash array via the caller
offsets_of() {	# path workers -> space-separated offsets
	q "SELECT array_to_string(pgcolumnar.file_split_offsets('$1', $2), ' ')"
}

# A backend that died does not pass as one that survived. A fresh connection answers
# again after crash recovery, so "SELECT 1" alone passed with a backend killed by
# SIGSEGV right before each of this suite's five survival checks (measured on PG17,
# #1341). survived() also requires the server log's count of killed backends to be
# unmoved since the suite began, and fails when that count cannot be read.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}
CRASHES_BEFORE="$(crashes)"

# #2083: a failed loader closed its file twice. AbortOutOfAnyTransaction already
# closes every transient file, so a second CloseTransientFile logged "fd passed
# to CloseTransientFile was not obtained from OpenTransientFile" and called
# close() on a descriptor number that might belong to something else by then.
double_closes() {	# -> count of those WARNINGs in the server log, or nothing
	local _n
	_n="$(pgc_pg "grep -c 'CloseTransientFile was not obtained' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
DOUBLE_CLOSES_BEFORE="$(double_closes)"

# ---- structural checks: boundaries land after a newline, cover the file -------
# Each helper below echoes one reading; the check, with its literal name, is at the
# call site.

structure_of() {	# path workers -> 1 when the offsets are well-formed and aligned
	local f="$1" w="$2"
	local size off arr ok prev i b
	size=$(stat -c %s "$f")
	read -r -a arr <<<"$(offsets_of "$f" "$w")"
	ok=1
	if [ "${#arr[@]}" != "$((w + 1))" ]; then
		echo "got ${#arr[@]} offsets, want $((w + 1))"
		return
	fi
	[ "${arr[0]}" = "0" ] || ok=0
	[ "${arr[$w]}" = "$size" ] || ok=0
	prev=-1
	for i in $(seq 0 "$w"); do
		off=${arr[$i]:-}
		[ "$off" -ge "$prev" ] 2>/dev/null || ok=0
		prev=$off
		# interior offset must sit right after a newline
		if [ "$i" -gt 0 ] && [ "$i" -lt "$w" ] && [ "$off" -gt 0 ] && [ "$off" -lt "$size" ]; then
			b=$(dd if="$f" bs=1 skip=$((off - 1)) count=1 2>/dev/null | od -An -tx1 | tr -d ' ')
			[ "$b" = "0a" ] || ok=0
		fi
	done
	echo "$ok"
}

# ---- end-to-end: per-range loads reconstruct the whole file -------------------
# The oracle is the generator that wrote the file, not a single columnar COPY of it.
# A single COPY into pgcolumnar shares every defect of pgcolumnar's COPY path with
# the per-range loads: with multi_insert dropping the row whose id is 1, both lost
# the same row and all nine reconstruct checks passed (measured on PG17, #1341).

split_load_hash() {	# path workers -> set hash of the per-range loads
	local f="$1" w="$2"
	local arr i off len rf
	read -r -a arr <<<"$(offsets_of "$f" "$w")"
	psql_run "DROP TABLE IF EXISTS t_split;
	          CREATE TABLE t_split (id int, txt text) USING pgcolumnar;" >/dev/null
	for i in $(seq 0 $((w - 1))); do
		off=${arr[$i]:-0}; len=$(( ${arr[$((i + 1))]:-0} - off ))
		[ "$len" -gt 0 ] || continue
		rf="$DATADIR/range_${w}_${i}.txt"
		dd if="$f" iflag=skip_bytes,count_bytes skip="$off" count="$len" of="$rf" 2>/dev/null
		[ "$(id -u)" = "0" ] && chown postgres "$rf"
		psql_run "\copy t_split FROM '$rf' WITH (FORMAT text)" >/dev/null
	done
	pgc_set_hash "SELECT * FROM t_split"
}

# ---- the split must actually split -------------------------------------------
# structure_of and split_load_hash both pass on a splitter degraded to a
# single range ({0, size, size, ..., size}): the offsets are ascending and
# newline-aligned, and one range covering the whole file reconstructs it. That is
# not hypothetical -- a bare-CR file and phase 5's "fall back to a single COPY"
# path both produce exactly that shape. So when the file has far more records than
# workers, assert every range is non-empty: the W-1 interior offsets must be
# strictly increasing and strictly inside (0, size). A degraded split fails here.
split_real_of() {	# path workers -> 1 when every range is non-empty
	local f="$1" w="$2"
	local size arr ok prev i off
	size=$(stat -c %s "$f")
	read -r -a arr <<<"$(offsets_of "$f" "$w")"
	ok=1
	prev=0
	for i in $(seq 1 $((w - 1))); do
		off=${arr[$i]:-}
		{ [ "$off" -gt "$prev" ] && [ "$off" -lt "$size" ]; } 2>/dev/null || ok=0
		prev=$off
	done
	echo "$ok"
}

# ---- the main file: 5000 rows -----------------------------------------------

GEN_F="SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt FROM generate_series(1, 5000) g"
make_pair "id int, txt text"
F=$(make_file 5000)
psql_run "\copy t_heap FROM '$F' WITH (FORMAT text)" >/dev/null
psql_run "\copy t_col  FROM '$F' WITH (FORMAT text)" >/dev/null
check "single-COPY heap and columnar agree" \
	"$(pgc_set_hash "SELECT * FROM t_col")" "$(pgc_set_hash "SELECT * FROM t_heap")"

# 5000 rows >> W, so every range must be non-empty: the split-is-real checks catch a
# degraded split (W=1 has no interior offset, so it has none).
check "5000 rows / 1: offsets well-formed and newline-aligned" "$(structure_of "$F" 1)" 1
check "5000 rows / 1: per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F" 1)" "$(pgc_set_hash "$GEN_F")"
check "5000 rows / 2: offsets well-formed and newline-aligned" "$(structure_of "$F" 2)" 1
check "5000 rows / 2: per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F" 2)" "$(pgc_set_hash "$GEN_F")"
check "5000 rows / 2: split is real (interior offsets distinct, ranges non-empty)" \
	"$(split_real_of "$F" 2)" 1
check "5000 rows / 3: offsets well-formed and newline-aligned" "$(structure_of "$F" 3)" 1
check "5000 rows / 3: per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F" 3)" "$(pgc_set_hash "$GEN_F")"
check "5000 rows / 3: split is real (interior offsets distinct, ranges non-empty)" \
	"$(split_real_of "$F" 3)" 1
check "5000 rows / 8: offsets well-formed and newline-aligned" "$(structure_of "$F" 8)" 1
check "5000 rows / 8: per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F" 8)" "$(pgc_set_hash "$GEN_F")"
check "5000 rows / 8: split is real (interior offsets distinct, ranges non-empty)" \
	"$(split_real_of "$F" 8)" 1
check "5000 rows / 16: offsets well-formed and newline-aligned" "$(structure_of "$F" 16)" 1
check "5000 rows / 16: per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F" 16)" "$(pgc_set_hash "$GEN_F")"
check "5000 rows / 16: split is real (interior offsets distinct, ranges non-empty)" \
	"$(split_real_of "$F" 16)" 1

# a line longer than the 64 kB scan chunk: the boundary for a mid-file split lands
# only after crossing a read-chunk boundary, exercising the multi-chunk read (the
# only part of the scan with carried-over state).
F_BIG="$DATADIR/biglines.txt"
GEN_BIG="SELECT g AS id, 'h'||g||'_'||repeat('x', 70000) AS txt FROM generate_series(1, 20) g"
psql_run "COPY ($GEN_BIG) TO '$F_BIG' WITH (FORMAT text);" >/dev/null
[ "$(id -u)" = "0" ] && chown postgres "$F_BIG"
check "long lines >64kB / 4 workers: offsets well-formed and newline-aligned" \
	"$(structure_of "$F_BIG" 4)" 1
check "long lines >64kB / 4 workers: per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F_BIG" 4)" "$(pgc_set_hash "$GEN_BIG")"
check "long lines >64kB / 4 workers: split is real (interior offsets distinct, ranges non-empty)" \
	"$(split_real_of "$F_BIG" 4)" 1

# more workers than rows: extra ranges must be empty, load still exact
F_TINY=$(make_file 5)
check "5 rows / 8 workers: offsets well-formed and newline-aligned" "$(structure_of "$F_TINY" 8)" 1
check "5 rows / 8 workers (empty ranges): per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F_TINY" 8)" \
	"$(pgc_set_hash "SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt FROM generate_series(1, 5) g")"

# single row: an edge smoke test only. With one record and 4 workers the correct
# result is {0, size, size, size} -- the degenerate single-range shape -- so this
# case cannot distinguish a working splitter from a degraded one (that is what
# the split-is-real checks on the 5000-row file are for); it just confirms one record
# does not crash or drop.
F_ONE=$(make_file 1)
check "1 row / 4 workers (edge smoke): offsets well-formed and newline-aligned" \
	"$(structure_of "$F_ONE" 4)" 1
check "1 row / 4 workers (edge smoke): per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F_ONE" 4)" \
	"$(pgc_set_hash "SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt FROM generate_series(1, 1) g")"

# file with no trailing newline: last range must still run to EOF
F_NONL="$DATADIR/nonl.txt"
GEN_NONL="SELECT g AS id, 'r'||g AS txt FROM generate_series(1,1000) g"
psql_run "COPY ($GEN_NONL) TO '$F_NONL' WITH (FORMAT text);" >/dev/null
# strip the trailing newline
truncate -s -1 "$F_NONL"
[ "$(id -u)" = "0" ] && chown postgres "$F_NONL"
check "no trailing newline / 4 workers: offsets well-formed and newline-aligned" \
	"$(structure_of "$F_NONL" 4)" 1
check "no trailing newline / 4 workers: per-range loads reconstruct the whole file" \
	"$(split_load_hash "$F_NONL" 4)" "$(pgc_set_hash "$GEN_NONL")"

# workers < 1 is rejected (capture stderr into a var first: q() hides stderr, and
# a `psql | grep` pipeline would trip set -o pipefail on psql's error exit).
err_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -Atc "SELECT pgcolumnar.file_split_offsets('$F', 0)" 2>&1 || true)"
check "workers < 1 is rejected" \
	"$(grep -qi "at least 1" <<<"$err_out" && echo ok || echo no)" ok

# a directory is rejected, not reported as an 8-exabyte splittable file (regression
# for the missing fstat/S_ISREG guard: lseek(SEEK_END) on a directory fd returns a
# huge value on Linux, so without the guard this returned {0, 9.2e18}).
dir_err="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -Atc "SELECT pgcolumnar.file_split_offsets('$DATADIR', 1)" 2>&1 || true)"
check "file_split_offsets: a directory is rejected (not an 8-exabyte file)" \
	"$(grep -qi "not a regular file" <<<"$dir_err" && echo ok || echo no)" ok

# ---- coordinator: pgcolumnar.parallel_copy (partition-parallel, atomic 2PC) ---
# Each worker loads a DISTINCT partition (distinct storage id -> parallel AND
# 2PC-atomic, no deadlock). The N-worker load of F must produce exactly the rows a
# single COPY does; t_heap already holds F (id 1..5000, sorted by id) and is the
# oracle. F is sorted by id, so we range-partition the target by id.
err_of() {	# runs a query, echoes stderr+stdout (for error-path assertions)
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atc "$1" 2>&1 || true
}
mkpart() {	# name nparts  -> range-partitioned columnar table over id in [1,5000]
	local name="$1" n="$2" i lo hi step ddl
	step=$(( (5000 + n) / n ))
	ddl="DROP TABLE IF EXISTS $name CASCADE; CREATE TABLE $name (id int, txt text) PARTITION BY RANGE (id);"
	for i in $(seq 0 $((n - 1))); do
		if [ "$i" = 0 ]; then lo=MINVALUE; else lo=$((i * step)); fi
		if [ "$i" = $((n - 1)) ]; then hi=MAXVALUE; else hi=$(((i + 1) * step)); fi
		ddl="$ddl CREATE TABLE ${name}_p$i PARTITION OF $name FOR VALUES FROM ($lo) TO ($hi) USING pgcolumnar;"
	done
	psql_run "$ddl" >/dev/null
}

mkpart t_pcp 4
check "parallel_copy(partitioned, 1 workers): rows returned = 5000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '$F', 1)")" 5000
check "parallel_copy(partitioned, 1 workers): result == single-COPY oracle" \
	"$(pgc_set_hash "SELECT * FROM t_pcp")" "$(pgc_set_hash "SELECT * FROM t_heap")"
check "parallel_copy(partitioned, 1 workers): no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
mkpart t_pcp 4
check "parallel_copy(partitioned, 2 workers): rows returned = 5000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '$F', 2)")" 5000
check "parallel_copy(partitioned, 2 workers): result == single-COPY oracle" \
	"$(pgc_set_hash "SELECT * FROM t_pcp")" "$(pgc_set_hash "SELECT * FROM t_heap")"
check "parallel_copy(partitioned, 2 workers): no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
mkpart t_pcp 4
check "parallel_copy(partitioned, 4 workers): rows returned = 5000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '$F', 4)")" 5000
check "parallel_copy(partitioned, 4 workers): result == single-COPY oracle" \
	"$(pgc_set_hash "SELECT * FROM t_pcp")" "$(pgc_set_hash "SELECT * FROM t_heap")"
check "parallel_copy(partitioned, 4 workers): no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0

# ---- signed keys straddling 0 with an unbounded first partition --------------
# Regression for the bucket miscompare that ignored MINVALUE/MAXVALUE bound kinds:
# a [MINVALUE,100) partition holds keys on BOTH sides of 0, and a signed key
# compared against the (undefined) MINVALUE datum-read-as-0 would split that one
# partition across workers -> wrong result or the write-lock deadlock. Must load
# correctly. (Positive-only keys hid this; hence an explicit negative fixture.)
F_SIGNED="$DATADIR/signed.txt"
psql_run "COPY (SELECT g AS id, 'h'||g AS txt FROM generate_series(-100, 399) g)
          TO '$F_SIGNED' WITH (FORMAT text);" >/dev/null
[ "$(id -u)" = "0" ] && chown postgres "$F_SIGNED"
psql_run "DROP TABLE IF EXISTS t_heap_s; CREATE TABLE t_heap_s (id int, txt text);" >/dev/null
psql_run "\copy t_heap_s FROM '$F_SIGNED' WITH (FORMAT text)" >/dev/null
psql_run "DROP TABLE IF EXISTS t_signed CASCADE;
          CREATE TABLE t_signed (id int, txt text) PARTITION BY RANGE (id);
          CREATE TABLE t_signed_a PARTITION OF t_signed FOR VALUES FROM (MINVALUE) TO (100) USING pgcolumnar;
          CREATE TABLE t_signed_b PARTITION OF t_signed FOR VALUES FROM (100) TO (300) USING pgcolumnar;
          CREATE TABLE t_signed_c PARTITION OF t_signed FOR VALUES FROM (300) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
check "signed keys straddling 0: rows returned = 500" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_signed'::regclass, '$F_SIGNED', 3)")" 500
check "signed keys straddling 0: result == oracle (no misbucketing/deadlock)" \
	"$(pgc_set_hash "SELECT * FROM t_signed")" "$(pgc_set_hash "SELECT * FROM t_heap_s")"
check "signed keys straddling 0: no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0

# ---- a text partition key (with MINVALUE/MAXVALUE bounds) is rejected, not crash --
# jdatcmd's crash repro: a by-reference key + an unbounded bound would deref an
# undefined datum. Now the numeric/temporal-key restriction rejects it cleanly, and
# the kind-dispatch means even a would-be compare never touches the datum.
psql_run "DROP TABLE IF EXISTS t_txtkey CASCADE;
          CREATE TABLE t_txtkey (k text, v int) PARTITION BY RANGE (k);
          CREATE TABLE t_txtkey_a PARTITION OF t_txtkey FOR VALUES FROM (MINVALUE) TO ('m') USING pgcolumnar;
          CREATE TABLE t_txtkey_b PARTITION OF t_txtkey FOR VALUES FROM ('m') TO (MAXVALUE) USING pgcolumnar;" >/dev/null
tk_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_txtkey'::regclass, '$F', 2)")"
check "text partition key is rejected (not a crash)" \
	"$(grep -qi "numeric or date/time" <<<"$tk_err" && echo ok || echo no)" ok
check "text partition key: server still up (no crash)" "$(survived "$CRASHES_BEFORE")" 1

# ---- key NOT in column 1, with a generated column before it -------------------
# COPY's default column list skips generated columns, so the splitter's key-field
# index must skip them too; otherwise every row buckets wrong.
F_GEN="$DATADIR/genkey.txt"
psql_run "COPY (SELECT 'lbl'||g AS label, g AS id FROM generate_series(1,3000) g)
          TO '$F_GEN' WITH (FORMAT text);" >/dev/null
[ "$(id -u)" = "0" ] && chown postgres "$F_GEN"
GENSCHEMA="label text, gcol int GENERATED ALWAYS AS (id*2) STORED, id int"
psql_run "DROP TABLE IF EXISTS t_heap_g; CREATE TABLE t_heap_g ($GENSCHEMA);" >/dev/null
psql_run "\copy t_heap_g (label, id) FROM '$F_GEN' WITH (FORMAT text)" >/dev/null
psql_run "DROP TABLE IF EXISTS t_gen CASCADE;
          CREATE TABLE t_gen ($GENSCHEMA) PARTITION BY RANGE (id);
          CREATE TABLE t_gen_a PARTITION OF t_gen FOR VALUES FROM (MINVALUE) TO (1000) USING pgcolumnar;
          CREATE TABLE t_gen_b PARTITION OF t_gen FOR VALUES FROM (1000) TO (2000) USING pgcolumnar;
          CREATE TABLE t_gen_c PARTITION OF t_gen FOR VALUES FROM (2000) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
check "key not in column 1 (generated col before it): rows = 3000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_gen'::regclass, '$F_GEN', 3)")" 3000
check "key not in column 1: result == oracle" \
	"$(pgc_set_hash "SELECT * FROM t_gen")" "$(pgc_set_hash "SELECT * FROM t_heap_g")"

# ---- atomicity 1: a bad partition-key value is rejected by the splitter -------
# The splitter parses the key of every row; a non-integer id fails there, before
# any worker/2PC -- nothing is loaded, no prepared transaction is created.
F_BADKEY="$DATADIR/badkey.txt"
{ for i in $(seq 1 400); do printf '%s\thost_%s\n' "$i" "$i"; done
  printf '%s\t%s\n' "notanint" "boom"
  for i in $(seq 401 800); do printf '%s\thost_%s\n' "$i" "$i"; done; } > "$F_BADKEY"
[ "$(id -u)" = "0" ] && chown postgres "$F_BADKEY"
mkpart t_pcp 4
bk_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '$F_BADKEY', 4)")"
check "atomic: a bad partition-key value is rejected" \
	"$(grep -qi 'invalid input syntax' <<<"$bk_err" && echo ok || echo no)" ok
check "atomic: bad-key load leaves the target empty" "$(q "SELECT count(*) FROM t_pcp")" 0
check "atomic: no prepared-transaction leak after bad-key" "$(q "SELECT count(*) FROM pg_prepared_xacts")" 0

# ---- atomicity 2: a loader failure rolls back the PREPARED siblings -----------
# A gapped partitioned target (ids 2000..2999 belong to NO partition, no default):
# workers whose ranges route rows into the gap fail with "no partition of relation",
# so the coordinator must ROLLBACK PREPARED every worker that already prepared ->
# the target is left empty, with no orphaned prepared transaction.
psql_run "DROP TABLE IF EXISTS t_gap CASCADE;
          CREATE TABLE t_gap (id int, txt text) PARTITION BY RANGE (id);
          CREATE TABLE t_gap_a PARTITION OF t_gap FOR VALUES FROM (MINVALUE) TO (2000) USING pgcolumnar;
          CREATE TABLE t_gap_b PARTITION OF t_gap FOR VALUES FROM (3000) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
gap_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_gap'::regclass, '$F', 4)")"
# The loader's own error, not any message with "failed" in it: every worker failure
# reads "worker N failed: <cause>", so with the loader's message dropped the old
# pattern still matched "failed: loader exited without reporting a result" (#1341).
check "atomic: a loader failure fails the whole load" \
	"$(grep -qi 'failed: no partition of relation' <<<"$gap_err" && echo ok || echo no)" ok
check "atomic: loader-failure leaves the target empty (siblings rolled back)" \
	"$(q "SELECT count(*) FROM t_gap")" 0
check "atomic: no prepared-transaction leak after loader failure" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
check "atomic: server still up after a failed load" "$(survived "$CRASHES_BEFORE")" 1

# ---- input must be sorted by the partition key -------------------------------
F_SHUF="$DATADIR/shuf.txt"
shuf "$F" > "$F_SHUF"; [ "$(id -u)" = "0" ] && chown postgres "$F_SHUF"
mkpart t_pcp 4
shuf_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '$F_SHUF', 4)")"
check "unsorted input is rejected" \
	"$(grep -qi "not sorted" <<<"$shuf_err" && echo ok || echo no)" ok
check "unsorted rejection loads nothing" "$(q "SELECT count(*) FROM t_pcp")" 0

# ---- a DEFAULT partition is rejected (it could catch any worker's rows) -------
psql_run "DROP TABLE IF EXISTS t_def CASCADE;
          CREATE TABLE t_def (id int, txt text) PARTITION BY RANGE (id);
          CREATE TABLE t_def_a PARTITION OF t_def FOR VALUES FROM (MINVALUE) TO (2500) USING pgcolumnar;
          CREATE TABLE t_def_d PARTITION OF t_def DEFAULT USING pgcolumnar;" >/dev/null
def_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_def'::regclass, '$F', 4)")"
check "DEFAULT partition target is rejected" \
	"$(grep -qi "DEFAULT partition" <<<"$def_err" && echo ok || echo no)" ok

# ---- the max_prepared_transactions guard fires up front ----------------------
# max_prepared_transactions is 8 (set above); a target with more partitions than
# that, loaded with that many workers, must error before spawning, naming the GUC.
mkpart t_pcp10 10
guard_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_pcp10'::regclass, '$F', 10)")"
check "max_prepared_transactions guard fires" \
	"$(grep -qi "max_prepared_transactions" <<<"$guard_err" && echo ok || echo no)" ok
check "guard rejects before loading anything" "$(q "SELECT count(*) FROM t_pcp10")" 0

# ---- a missing file errors cleanly -------------------------------------------
mkpart t_pcp 4
mf_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_pcp'::regclass, '$DATADIR/nope.txt', 2)")"
check "missing file: errors, not crashes" \
	"$(grep -qiE "could not (open|stat)|no such file|not a regular file" <<<"$mf_err" && echo ok || echo no)" ok
check "missing file: server still up (no worker crash)" "$(survived "$CRASHES_BEFORE")" 1
check "missing file: nothing loaded" "$(q "SELECT count(*) FROM t_pcp")" 0

# ---- a non-columnar (heap) target is rejected --------------------------------
# t_heaptgt has a primary key, so the #2080 section below can check that a heap
# target gets 42809 before the unique-index refusal (#2209).
psql_run "DROP TABLE IF EXISTS t_heaptgt; CREATE TABLE t_heaptgt (id int PRIMARY KEY, txt text);" >/dev/null
nc_err="$(err_of "SELECT pgcolumnar.parallel_copy('t_heaptgt'::regclass, '$F', 2)")"
check "non-columnar target is rejected" \
	"$(grep -qi "not a pgcolumnar table" <<<"$nc_err" && echo ok || echo no)" ok

# ---- single columnar table: workers write ONE storage concurrently -----------
# The storage-row creation lock is skipped by the loaders (the coordinator
# pre-creates the row committed), so N loaders write the one storage in parallel and
# 2PC-atomically. The result must equal a single COPY -- a concurrency race on the
# shared storage would diverge from the oracle. The GUC that gates the engine's
# skip must default off, so an ordinary write never takes that path.
check "bulk_parallel_writer GUC defaults off (core write path unchanged)" \
	"$(q "SHOW pgcolumnar.bulk_parallel_writer")" off
psql_run "DROP TABLE IF EXISTS t_single; CREATE TABLE t_single (id int, txt text) USING pgcolumnar;" >/dev/null
check "parallel_copy(single table, 1 workers): rows returned = 5000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_single'::regclass, '$F', 1)")" 5000
check "parallel_copy(single table, 1 workers): result == single-COPY oracle" \
	"$(pgc_set_hash "SELECT * FROM t_single")" "$(pgc_set_hash "SELECT * FROM t_heap")"
check "parallel_copy(single table, 1 workers): no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
psql_run "DROP TABLE IF EXISTS t_single; CREATE TABLE t_single (id int, txt text) USING pgcolumnar;" >/dev/null
check "parallel_copy(single table, 2 workers): rows returned = 5000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_single'::regclass, '$F', 2)")" 5000
check "parallel_copy(single table, 2 workers): result == single-COPY oracle" \
	"$(pgc_set_hash "SELECT * FROM t_single")" "$(pgc_set_hash "SELECT * FROM t_heap")"
check "parallel_copy(single table, 2 workers): no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
psql_run "DROP TABLE IF EXISTS t_single; CREATE TABLE t_single (id int, txt text) USING pgcolumnar;" >/dev/null
check "parallel_copy(single table, 4 workers): rows returned = 5000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_single'::regclass, '$F', 4)")" 5000
check "parallel_copy(single table, 4 workers): result == single-COPY oracle" \
	"$(pgc_set_hash "SELECT * FROM t_single")" "$(pgc_set_hash "SELECT * FROM t_heap")"
check "parallel_copy(single table, 4 workers): no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0

# ---- concurrency witness: prove the writers actually shared the storage -------
# The oracle-equality checks above pass even if the loaders serialised by accident,
# in which case they exercise nothing a single COPY did not. This block forces real
# concurrent writing and asserts the structural fingerprint of it, so the guarantee
# is visible in the suite rather than incidental. A small stripe_row_limit (set on
# the table, so every loader reads it) makes each of N workers flush SEVERAL stripes
# from a 40k-row file; then:
#   (a) MORE distinct stripe ids than workers  -> many stripe reservations happened
#       against the one shared metapage (each worker wrote more than one group);
#   (b) no two stripes' byte ranges [fileoffset, fileoffset+datalength) overlap, and
#       the per-stripe rowcounts cover every input row exactly  -> the concurrent
#       writers neither collided on disk nor lost/duplicated a row. This is the
#       SQL-visible form of COLUMNAR_ASSERT_NO_OVERLAP, which the assert gate also
#       enforces from C. Row-number uniqueness is implied: a collision would drop or
#       duplicate rows and fail (b)'s coverage and the oracle below.
# And structurally: N 2PC-prepared loaders can only COMMIT together if the
# storage-row lock was skipped -- with it held, loader 2 blocks on loader 1's
# prepared xact forever -- so a passing run is itself evidence the skip fired.
F_W=$(make_file 40000)
psql_run "DROP TABLE IF EXISTS t_wit_heap; CREATE TABLE t_wit_heap (id int, txt text);" >/dev/null
psql_run "\copy t_wit_heap FROM '$F_W' WITH (FORMAT text)" >/dev/null
psql_run "DROP TABLE IF EXISTS t_wit; CREATE TABLE t_wit (id int, txt text) USING pgcolumnar;
          SELECT pgcolumnar.set_options('t_wit'::regclass,
                                        chunk_group_row_limit => 1000,
                                        stripe_row_limit => 2000);" >/dev/null
check "witness: parallel_copy(single table, 4 workers, 40000 rows) loads every row" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_wit'::regclass, '$F_W', 4)")" 40000
check "witness: result == single-COPY oracle" \
	"$(pgc_set_hash "SELECT * FROM t_wit")" "$(pgc_set_hash "SELECT * FROM t_wit_heap")"
# (a) more distinct stripes than workers: concurrent reservations, >1 group per worker
check "witness: >4 distinct stripe ids (each worker reserved several stripes)" \
	"$(q "SELECT count(DISTINCT stripeid) > 4 FROM pgcolumnar.stats('t_wit'::regclass)")" t
# (b) no two stripe byte ranges overlap (each overlap counted twice; want 0)
check "witness: no stripe byte-range overlaps another (no on-disk collision)" \
	"$(q "WITH s AS (SELECT stripeid, fileoffset AS lo, fileoffset+datalength AS hi
	                 FROM pgcolumnar.stats('t_wit'::regclass))
	      SELECT count(*) FROM s a JOIN s b
	        ON a.stripeid <> b.stripeid AND a.lo < b.hi AND b.lo < a.hi")" 0
# (b) per-stripe rowcounts cover every input row exactly (no lost/duplicated rows)
check "witness: stripe rowcounts sum to every input row (complete coverage)" \
	"$(q "SELECT sum(rowcount) FROM pgcolumnar.stats('t_wit'::regclass)")" 40000
check "witness: no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0

# single-table atomicity: a bad row rolls back the whole load, no leak
psql_run "DROP TABLE IF EXISTS t_single; CREATE TABLE t_single (id int, txt text) USING pgcolumnar;" >/dev/null
st_bad="$(err_of "SELECT pgcolumnar.parallel_copy('t_single'::regclass, '$F_BADKEY', 4)")"
# The loader's own error, for the reason given at the loader-failure check above.
check "single table: a bad row fails the whole load" \
	"$(grep -qi 'failed: invalid input syntax' <<<"$st_bad" && echo ok || echo no)" ok
check "single table: failed load leaves the target empty" "$(q "SELECT count(*) FROM t_single")" 0
check "single table: no prepared-transaction leak after failure" "$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
# The loads above that failed in their loaders (the gapped target and the bad row)
# must not have closed a loader's file twice (#2083).
dc_now="$(double_closes)"
check "failed loaders close their file once: no CloseTransientFile WARNING in the server log (#2083)" \
	"$( [[ "$DOUBLE_CLOSES_BEFORE" =~ ^[0-9]+$ && "$dc_now" =~ ^[0-9]+$ ]] \
		&& echo $(( dc_now - DOUBLE_CLOSES_BEFORE )) || echo "unreadable [$DOUBLE_CLOSES_BEFORE] [$dc_now]")" 0

# --- #686: parallel_copy must refuse a FIFO, not hang the backend -------------
# pcopy_open_regular_file opened O_RDONLY then fstat, so a FIFO blocked in open(2)
# (cancel-resistant, its own S_ISREG check unreachable). It must open O_NONBLOCK,
# reject the non-regular file (42809), and never block. RED (unguarded) is a
# wall-clock HANG; GREEN is 42809.
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
psql_run "DROP TABLE IF EXISTS t_fifo; CREATE TABLE t_fifo (id int, txt text) USING pgcolumnar;" >/dev/null
PCFIFO="$PGC_WORKDIR/pc.fifo"; mkfifo "$PCFIFO"
check "parallel_copy on a FIFO is refused, not a hang (42809)" \
	"$(sqlstate_or_hang "SELECT pgcolumnar.parallel_copy('t_fifo'::regclass, '$PCFIFO', 2)")" \
	"42809"
exec 9<>"$PCFIFO" 2>/dev/null; exec 9>&- 2>/dev/null    # release any blocked opener
check "backend still up after the parallel_copy FIFO refusal" "$(survived "$CRASHES_BEFORE")" "1"

# The partition-aligned coordinator (pcopy_partition_aligned_offsets, reached only
# for a PARTITIONED target) opens the file via getline() and had the same
# stat-before-open FIFO hazard the single-file path fixed (#696). A FIFO named to a
# partitioned target must also be refused, not hang.
mkpart t_fifo_part 3
PCFIFO2="$PGC_WORKDIR/pc2.fifo"; mkfifo "$PCFIFO2"
check "parallel_copy on a FIFO via the partitioned coordinator is refused, not a hang (42809)" \
	"$(sqlstate_or_hang "SELECT pgcolumnar.parallel_copy('t_fifo_part'::regclass, '$PCFIFO2', 3)")" \
	"42809"
exec 9<>"$PCFIFO2" 2>/dev/null; exec 9>&- 2>/dev/null
check "backend still up after the partitioned-coordinator FIFO refusal" "$(survived "$CRASHES_BEFORE")" "1"


# ---- #1886: a remote URL is refused up front, not opened as a local file -----
# file_split_offsets and parallel_copy read local files only. Before #1886 an
# s3:// or http:// URL went to open(2) and failed with ENOENT ("could not open
# file "s3://..."), which says the object does not exist when the real reason is
# that these functions never read object storage. Each now refuses every scheme
# PgColumnarPathIsRemote recognises with 0A000, naming itself and the URL, with a
# hint that it reads local files only. parallel_copy refuses after the INSERT
# check (test/import_export_privilege.sh pins that order) and before it checks
# the target's kind, so a heap target gets the refusal, not 42809. The controls
# hold the other side: a LOCAL path that carries "s3://" past its first byte is
# still read.
remote_refusal() {	# SQL -> "SQLSTATE message | hint" of its error, or "no error (...)"
	local out msg hint
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$1" 2>&1)"
	case "$out" in
		*ERROR:*)
			msg="$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' <<<"$out")"
			hint="$(sed -n '/^HINT:/{s/^HINT: *//p;q;}' <<<"$out")"
			printf '%s | %s\n' "$msg" "${hint:-no hint}";;
		*) printf 'no error (%s)\n' "$out";;
	esac
}
remote_detail() {	# SQL -> "SQLSTATE | DETAIL" of its error, or "no error (...)"
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$1" 2>&1)"
	case "$out" in
		*ERROR:*) printf '%s | %s\n' "$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): .*$/\1/p;q;}' <<<"$out")" \
			"$(sed -n '/^DETAIL:/{s/^DETAIL: *//p;q;}' <<<"$out")";;
		*) printf 'no error (%s)\n' "$out";;
	esac
}
want_refusal() {	# FUNCTION URL -> the refusal #1886 promises
	printf '0A000 columnar: %s does not read "%s" | %s reads local files only. Use a local filesystem path.\n' \
		"$1" "$2" "$1"
}
REMOTE_S3="s3://pgc-test/f1886/data.txt"
REMOTE_HTTP="http://127.0.0.1:9/f1886/data.txt"
REMOTE_OTHER="https://127.0.0.1:9/f1886/data.txt gs://pgc-test/f1886/data.txt az://pgc-test/f1886/data.txt S3://pgc-test/f1886/data.txt"
each_refusal() {	# SQL-with-@URL@ -> one remote_refusal per URL in REMOTE_OTHER, joined by " ; "
	local u acc=""
	for u in $REMOTE_OTHER; do acc="$acc${acc:+ ; }$(remote_refusal "${1//@URL@/$u}")"; done
	printf '%s\n' "$acc"
}
each_want() {	# FUNCTION -> want_refusal per URL in REMOTE_OTHER, joined by " ; "
	local u acc=""
	for u in $REMOTE_OTHER; do acc="$acc${acc:+ ; }$(want_refusal "$1" "$u")"; done
	printf '%s\n' "$acc"
}

check "file_split_offsets refuses an s3:// URL with 0A000, naming itself and the URL (#1886)" \
	"$(remote_refusal "SELECT pgcolumnar.file_split_offsets('$REMOTE_S3', 2)")" \
	"$(want_refusal file_split_offsets "$REMOTE_S3")"
check "file_split_offsets refuses an http:// URL with 0A000, naming itself and the URL (#1886)" \
	"$(remote_refusal "SELECT pgcolumnar.file_split_offsets('$REMOTE_HTTP', 2)")" \
	"$(want_refusal file_split_offsets "$REMOTE_HTTP")"
check "file_split_offsets refuses https://, gs://, az:// and S3:// URLs with 0A000 too (#1886)" \
	"$(each_refusal "SELECT pgcolumnar.file_split_offsets('@URL@', 2)")" \
	"$(each_want file_split_offsets)"

psql_run "DROP TABLE IF EXISTS t_rem; CREATE TABLE t_rem (id int, txt text) USING pgcolumnar;" >/dev/null
mkpart t_rem_part 4
check "parallel_copy refuses an s3:// URL with 0A000, naming itself and the URL (#1886)" \
	"$(remote_refusal "SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '$REMOTE_S3', 2)")" \
	"$(want_refusal parallel_copy "$REMOTE_S3")"
check "parallel_copy refuses an http:// URL with 0A000, naming itself and the URL (#1886)" \
	"$(remote_refusal "SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '$REMOTE_HTTP', 2)")" \
	"$(want_refusal parallel_copy "$REMOTE_HTTP")"
check "parallel_copy refuses https://, gs://, az:// and S3:// URLs for a partitioned target with 0A000 too (#1886)" \
	"$(each_refusal "SELECT pgcolumnar.parallel_copy('t_rem_part'::regclass, '@URL@', 2)")" \
	"$(each_want parallel_copy)"
check "parallel_copy refuses a remote URL before it looks at the target: a heap target gets 0A000 (#1886)" \
	"$(remote_refusal "SELECT pgcolumnar.parallel_copy('t_heaptgt'::regclass, '$REMOTE_S3', 2)")" \
	"$(want_refusal parallel_copy "$REMOTE_S3")"

# The controls: "$DATADIR/s3:" is a directory, so "$DATADIR/s3://ctl.txt" is a
# local file whose path holds "s3://" past its start.
mkdir -p "$DATADIR/s3:"; chmod 777 "$DATADIR/s3:"
if [ "$(id -u)" = "0" ]; then chown postgres "$DATADIR/s3:"; fi
LOCAL_S3="$DATADIR/s3://ctl.txt"
psql_run "COPY (SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt FROM generate_series(1, 5000) g)
          TO '$LOCAL_S3' WITH (FORMAT text);" >/dev/null
check "control: file_split_offsets still splits a local path that holds s3:// past its start (#1886)" \
	"$(q "SELECT (pgcolumnar.file_split_offsets('$LOCAL_S3', 2))[3]")" "$(stat -c %s "$DATADIR/s3:/ctl.txt")"
check "control: parallel_copy still loads a local path that holds s3:// past its start (#1886)" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '$LOCAL_S3', 2)")" 5000

# ---- #1918: s3a:// and s3n:// are refused too -------------------------------
# s3a:// and s3n:// are the Hadoop names for S3, and the Iceberg reader accepts
# them (#1825). PgColumnarPathIsRemote does not list them, so before #1918 they
# went to open(2) and failed with 58P01. The refusal now covers them in any
# case. The controls: a LOCAL path with "s3a://" past its start is still read.
joined_refusals() {	# SQL-with-@URL@ URL... -> remote_refusal per URL, joined by " ; "
	local sql=$1 u acc=""; shift
	for u in "$@"; do acc="$acc${acc:+ ; }$(remote_refusal "${sql//@URL@/$u}")"; done
	printf '%s\n' "$acc"
}
joined_wants() {	# FUNCTION URL... -> want_refusal per URL, joined by " ; "
	local fn=$1 u acc=""; shift
	for u in "$@"; do acc="$acc${acc:+ ; }$(want_refusal "$fn" "$u")"; done
	printf '%s\n' "$acc"
}
HADOOP="s3a://pgc-test/f1918/data.txt s3n://pgc-test/f1918/data.txt"
HADOOP_CASE="S3A://pgc-test/f1918/data.txt S3n://pgc-test/f1918/data.txt"
# shellcheck disable=SC2086
check "file_split_offsets refuses s3a:// and s3n:// URLs with 0A000, naming itself and the URL (#1918)" \
	"$(joined_refusals "SELECT pgcolumnar.file_split_offsets('@URL@', 2)" $HADOOP)" \
	"$(joined_wants file_split_offsets $HADOOP)"
# shellcheck disable=SC2086
check "file_split_offsets refuses S3A:// and S3n:// URLs too: the scheme match ignores case (#1918)" \
	"$(joined_refusals "SELECT pgcolumnar.file_split_offsets('@URL@', 2)" $HADOOP_CASE)" \
	"$(joined_wants file_split_offsets $HADOOP_CASE)"
# shellcheck disable=SC2086
check "parallel_copy refuses s3a:// and s3n:// URLs with 0A000, naming itself and the URL (#1918)" \
	"$(joined_refusals "SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '@URL@', 2)" $HADOOP)" \
	"$(joined_wants parallel_copy $HADOOP)"
# shellcheck disable=SC2086
check "parallel_copy refuses S3A:// and S3n:// URLs too: the scheme match ignores case (#1918)" \
	"$(joined_refusals "SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '@URL@', 2)" $HADOOP_CASE)" \
	"$(joined_wants parallel_copy $HADOOP_CASE)"

mkdir -p "$DATADIR/s3a:"; chmod 777 "$DATADIR/s3a:"
if [ "$(id -u)" = "0" ]; then chown postgres "$DATADIR/s3a:"; fi
LOCAL_S3A="$DATADIR/s3a://ctl.txt"
psql_run "COPY (SELECT g AS id, 'host_'||(g%97)||'_x'||g AS txt FROM generate_series(1, 5000) g)
          TO '$LOCAL_S3A' WITH (FORMAT text);" >/dev/null
check "control: file_split_offsets still splits a local path that holds s3a:// past its start (#1918)" \
	"$(q "SELECT (pgcolumnar.file_split_offsets('$LOCAL_S3A', 2))[3]")" "$(stat -c %s "$DATADIR/s3a:/ctl.txt")"
check "control: parallel_copy still loads a local path that holds s3a:// past its start (#1918)" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_rem'::regclass, '$LOCAL_S3A', 2)")" 5000


# ---- #2084: ranges follow COPY text syntax, not every newline byte ------------
# In COPY text format a backslash before a raw newline makes the newline part of
# the value, and a line holding only \. ends the data. Heap COPY of the same file
# is the oracle. A cut at an escaped newline split one value into two rows (single
# table) or fed "line2" to the key parser (partitioned target). A range that
# started after the \. line loaded rows COPY never reads.
F_BSNL="$DATADIR/bsnl.txt"
awk 'BEGIN { for (i = 0; i < 2000; i++) printf "first%d\\\nsecond%d\n", i, i }' >"$F_BSNL"
F_BSNL2="$DATADIR/bsnl2.txt"
# odd rows end line1 in a run of 3, even rows in a run of 1: both escape the newline
awk 'BEGIN { for (i = 1; i <= 2000; i++) printf "%d\tline1_%d%s\nline2_%d\n", i, i, (i % 2 ? "\\\\\\" : "\\"), i }' >"$F_BSNL2"
F_EOD="$DATADIR/eod.txt"
awk 'BEGIN { for (i = 1; i <= 1000; i++) printf "%d\tx\n", i; printf "\\.\n";
             for (i = 1001; i <= 2000; i++) printf "%d\ty\n", i }' >"$F_EOD"
chmod 644 "$F_BSNL" "$F_BSNL2" "$F_EOD"
psql_run "DROP TABLE IF EXISTS h_bsnl, h_bsnl2, h_eod, c_bsnl, c_eod CASCADE;
          CREATE TABLE h_bsnl (t text);           COPY h_bsnl FROM '$F_BSNL';
          CREATE TABLE h_bsnl2 (id int, txt text); COPY h_bsnl2 FROM '$F_BSNL2';
          CREATE TABLE h_eod (id int, txt text);   COPY h_eod FROM '$F_EOD';
          CREATE TABLE c_bsnl (t text) USING pgcolumnar;
          CREATE TABLE c_eod (id int, txt text) USING pgcolumnar;" >/dev/null
mkpart t_bsnlp 4
psql_run "DROP TABLE IF EXISTS t_eodp CASCADE;
          CREATE TABLE t_eodp (id int, txt text) PARTITION BY RANGE (id);
          CREATE TABLE t_eodp_a PARTITION OF t_eodp FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar;
          CREATE TABLE t_eodp_b PARTITION OF t_eodp FOR VALUES FROM (500) TO (1000) USING pgcolumnar;
          CREATE TABLE t_eodp_c PARTITION OF t_eodp FOR VALUES FROM (1000) TO (1500) USING pgcolumnar;
          CREATE TABLE t_eodp_d PARTITION OF t_eodp FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar;" >/dev/null

# the byte two before each interior offset: a backslash there means the cut
# follows an escaped newline (this file has no doubled backslashes)
escaped_cuts() {	# path workers -> how many interior offsets follow an escaped newline
	local f="$1" w="$2" arr i n=0 b
	read -r -a arr <<<"$(offsets_of "$f" "$w")"
	for i in $(seq 1 $((w - 1))); do
		[ "${arr[$i]:-0}" -ge 2 ] || continue
		b=$(dd if="$f" bs=1 skip=$((arr[i] - 2)) count=1 2>/dev/null | od -An -tx1 | tr -d ' ')
		[ "$b" = "5c" ] && n=$((n + 1))
	done
	echo "$n"
}
check "#2084 escaped newline / 4 workers: no split offset follows an escaped newline" \
	"$(escaped_cuts "$F_BSNL" 4)" 0
check "#2084 escaped newline / single table, 4 workers: rows returned = heap COPY's 2000" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_bsnl'::regclass, '$F_BSNL', 4)")" "$(q "SELECT count(*) FROM h_bsnl")"
check "#2084 escaped newline / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_bsnl")" "$(pgc_set_hash "SELECT * FROM h_bsnl")"
check "#2084 escaped newline / partitioned, 4 workers: rows returned = heap COPY's 2000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_bsnlp'::regclass, '$F_BSNL2', 4)")" "$(q "SELECT count(*) FROM h_bsnl2")"
check "#2084 escaped newline / partitioned, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM t_bsnlp")" "$(pgc_set_hash "SELECT * FROM h_bsnl2")"
check "#2084 end-of-data marker / single table, 4 workers: rows returned = heap COPY's 1000" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_eod'::regclass, '$F_EOD', 4)")" "$(q "SELECT count(*) FROM h_eod")"
check "#2084 end-of-data marker / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_eod")" "$(pgc_set_hash "SELECT * FROM h_eod")"
check "#2084 end-of-data marker / single table: no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
check "#2084 end-of-data marker / partitioned, 4 workers: rows returned = heap COPY's 1000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_eodp'::regclass, '$F_EOD', 4)")" "$(q "SELECT count(*) FROM h_eod")"
check "#2084 end-of-data marker / partitioned, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM t_eodp")" "$(pgc_set_hash "SELECT * FROM h_eod")"

# Only an odd backslash run escapes the newline after it. "v1\\" then a newline is
# the value v1\ and a whole row, so a cut may follow it. even.txt alternates runs
# of 2 and 4. Without these, a splitter that treats any run as escaping passed
# every check above (review of #2148, mutants c1 and c2). evenp.txt alternates
# runs of 2 and 4 too (a6) and puts an unsorted row (id 5) right after row 1500,
# which ends in a run of 4 and sits in a later partition: the partitioned
# splitter must read it as its own row and refuse the file. Joining it to the
# line before hid it from the sort guard and the load hung, so the load runs
# under a timeout.
F_EVEN="$DATADIR/even.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) { fmt = i % 2 ? "v%d\\\\\n" : "v%d\\\\\\\\\n"; printf fmt, i } }' >"$F_EVEN"
F_EVENP="$DATADIR/evenp.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) { printf "%d\tv%d%s\n", i, i, (i % 2 ? "\\\\" : "\\\\\\\\"); if (i == 1500) printf "5\tback\n" } }' >"$F_EVENP"
# Rows after \. that COPY would reject: COPY never reads them, so they must not
# fail the load (review of #2148, mutant c4 and the splitter's stop at \.).
F_EODJ="$DATADIR/eodjunk.txt"
awk 'BEGIN { for (i = 1; i <= 1000; i++) printf "%d\tx\n", i; printf "\\.\n";
             for (i = 1; i <= 1000; i++) printf "notanint\tz\n" }' >"$F_EODJ"
chmod 644 "$F_EVEN" "$F_EVENP" "$F_EODJ"
psql_run "DROP TABLE IF EXISTS h_even, h_eodj, c_even, c_eodj CASCADE;
          CREATE TABLE h_even (t text);           COPY h_even FROM '$F_EVEN';
          CREATE TABLE h_eodj (id int, txt text); COPY h_eodj FROM '$F_EODJ';
          CREATE TABLE c_even (t text) USING pgcolumnar;
          CREATE TABLE c_eodj (id int, txt text) USING pgcolumnar;" >/dev/null
mkpart t_evenp 4
mkpart t_eodjp 4

even_cuts() {	# path workers -> how many interior offsets rise, stay inside the file and follow an even backslash run and a newline
	local f="$1" w="$2" arr i n=0 size hex run
	size=$(stat -c %s "$f")
	read -r -a arr <<<"$(offsets_of "$f" "$w")"
	for i in $(seq 1 $((w - 1))); do
		[[ "${arr[$i]:-x}" =~ ^[0-9]+$ ]] || continue
		[ "${arr[$i]}" -gt "${arr[$((i - 1))]}" ] && [ "${arr[$i]}" -lt "$size" ] && [ "${arr[$i]}" -ge 9 ] || continue
		hex=$(dd if="$f" bs=1 skip=$((arr[i] - 9)) count=9 2>/dev/null | od -An -tx1 | tr -d ' \n')
		[ "${hex: -2}" = "0a" ] || continue
		hex=${hex%0a}; run=0
		while [ "${hex: -2}" = "5c" ]; do run=$((run + 1)); hex=${hex%5c}; done
		[ "$run" -gt 0 ] && [ $((run % 2)) = 0 ] && n=$((n + 1))
	done
	echo "$n"
}
sqlstate_of() {	# SQL -> the SQLSTATE it raised under a 30 s statement_timeout, or "noerror (output)"
	local out s
	out="$(env PGOPTIONS='-c statement_timeout=30s' PATH="$PGC_BINDIR:$PATH" psql \
		-h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY sqlstate
$1;
SQLEOF
)"
	s="$(printf '%s\n' "$out" | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1)"
	echo "${s:-noerror ($out)}"
}
check "#2084 even backslash run / 4 workers: every split offset is distinct and follows an even backslash run" \
	"$(even_cuts "$F_EVEN" 4)" 3
check "#2084 even backslash run / single table, 4 workers: rows returned = heap COPY's 2000" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_even'::regclass, '$F_EVEN', 4)")" "$(q "SELECT count(*) FROM h_even")"
check "#2084 even backslash run / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_even")" "$(pgc_set_hash "SELECT * FROM h_even")"
check "#2084 even backslash run / partitioned: an unsorted row after an even run is refused (22000)" \
	"$(sqlstate_of "SELECT pgcolumnar.parallel_copy('t_evenp'::regclass, '$F_EVENP', 4)")" 22000
check "#2084 bad rows after the end-of-data marker / single table, 4 workers: rows returned = heap COPY's 1000" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_eodj'::regclass, '$F_EODJ', 4)")" "$(q "SELECT count(*) FROM h_eodj")"
check "#2084 bad rows after the end-of-data marker / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_eodj")" "$(pgc_set_hash "SELECT * FROM h_eodj")"
check "#2084 bad rows after the end-of-data marker / single table: no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0
check "#2084 bad rows after the end-of-data marker / partitioned, 4 workers: rows returned = heap COPY's 1000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_eodjp'::regclass, '$F_EODJ', 4)")" "$(q "SELECT count(*) FROM h_eodj")"
check "#2084 bad rows after the end-of-data marker / partitioned, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM t_eodjp")" "$(pgc_set_hash "SELECT * FROM h_eodj")"

# Three fixture gaps the review of #2148 at 7a40d479 found; each let a mutant of
# the new splitter code pass every check above.
#
# bsnl64k.txt (single table) mixes runs of 1 and 3 and is longer than one 64 KiB
# read. Its middle record ends in a run of 3 whose last backslash is byte 65535,
# the last byte of the first read; the newline it escapes is byte 65536, the
# first byte of the second read. The pinned offsets come from that layout: 3750
# records of 17 and 15 bytes (60000 bytes), the long record (60000..65541), then
# 3750 more. Mutants e1, e2 and n35.
#
# multi.txt (partitioned) has records of 3 and 4 lines with the key after them.
# Odd records start with a line that is only \ and even records have a middle
# line that is only \\\, so a run starts at byte 0 of a line (a2).
# contp.txt puts an unsorted row (id 5) after continued records, past row 1500.
# eofrec.txt ends inside a continued record, whose key 5 is unsorted. Mutants
# n27, n45, n08 (multi), n44 (contp) and n31 (eofrec).
#
# eodcrlf.txt is eodjunk.txt with CRLF line ends, so the marker is \.\r\n.
# Mutant n36.
#
# twobad.txt has a bad row at 1501 and another at 3501, in different ranges. The
# load must name the first one, as heap COPY does. Mutant n04.
F_LONG="$DATADIR/bsnl64k.txt"
awk 'BEGIN { for (i = 1; i <= 3750; i++) printf "a%05d%s\nb%05d\n", i, (i % 2 ? "\\\\\\" : "\\"), i;
             x = ""; for (k = 0; k < 5533; k++) x = x "x"; printf "%s\\\\\\\ntail\n", x;
             for (i = 3751; i <= 7500; i++) printf "a%05d%s\nb%05d\n", i, (i % 2 ? "\\\\\\" : "\\"), i }' >"$F_LONG"
F_MULTI="$DATADIR/multi.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++)
               if (i % 2) printf "\\\nm%d_b\\\\\\\nm%d_c\t%d\n", i, i, i;
               else       printf "m%d_a\\\n\\\\\\\nm%d_c\\\\\\\nm%d_d\t%d\n", i, i, i, i }' >"$F_MULTI"
F_CONTP="$DATADIR/contp.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) { printf "%d\tline1_%d%s\nline2_%d\n", i, i, (i % 2 ? "\\\\\\" : "\\"), i;
                                           if (i == 1500) printf "5\tback\n" } }' >"$F_CONTP"
F_EOFREC="$DATADIR/eofrec.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) printf "%d\tx%d\n", i, i; printf "5\tlast\\\\\\\n" }' >"$F_EOFREC"
F_EODCRLF="$DATADIR/eodcrlf.txt"
awk 'BEGIN { for (i = 1; i <= 1000; i++) printf "%d\tx\r\n", i; printf "\\.\r\n";
             for (i = 1; i <= 1000; i++) printf "notanint\tz\r\n" }' >"$F_EODCRLF"
F_TWOBAD="$DATADIR/twobad.txt"
awk 'BEGIN { for (i = 1; i <= 5000; i++)
               if (i == 1501) printf "bad_early\tz\n"; else if (i == 3501) printf "bad_late\tz\n"; else printf "%d\tx%d\n", i, i }' >"$F_TWOBAD"
chmod 644 "$F_LONG" "$F_MULTI" "$F_CONTP" "$F_EOFREC" "$F_EODCRLF" "$F_TWOBAD"
psql_run "DROP TABLE IF EXISTS h_long, h_multi, h_eodcrlf, c_long, c_eodcrlf, c_twobad, t_multip CASCADE;
          CREATE TABLE h_long (t text);              COPY h_long FROM '$F_LONG';
          CREATE TABLE h_multi (txt text, id int);   COPY h_multi FROM '$F_MULTI';
          CREATE TABLE h_eodcrlf (id int, txt text); COPY h_eodcrlf FROM '$F_EODCRLF';
          CREATE TABLE c_long (t text) USING pgcolumnar;
          CREATE TABLE c_eodcrlf (id int, txt text) USING pgcolumnar;
          CREATE TABLE c_twobad (id int, txt text) USING pgcolumnar;
          CREATE TABLE t_multip (txt text, id int) PARTITION BY RANGE (id);
          CREATE TABLE t_multip_a PARTITION OF t_multip FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar;
          CREATE TABLE t_multip_b PARTITION OF t_multip FOR VALUES FROM (500) TO (1000) USING pgcolumnar;
          CREATE TABLE t_multip_c PARTITION OF t_multip FOR VALUES FROM (1000) TO (1500) USING pgcolumnar;
          CREATE TABLE t_multip_d PARTITION OF t_multip FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
mkpart t_contp 4
mkpart t_eofrecp 4
mkpart t_eodcrlfp 4

# The "rows returned = heap COPY's N" checks compare with the heap table, so this
# premise pins N, and the byte layout the offsets below rely on.
check "#2084 premise: heap COPY loads bsnl 2000, bsnl2 2000, eod 1000, even 2000, eodjunk 1000, bsnl64k 7501, multi 2000, eodcrlf 1000 rows; bsnl64k.txt is 125542 bytes with 5c 0a at bytes 65535-65536" \
	"$(q "SELECT concat_ws(' ', (SELECT count(*) FROM h_bsnl), (SELECT count(*) FROM h_bsnl2), (SELECT count(*) FROM h_eod),
	                          (SELECT count(*) FROM h_even), (SELECT count(*) FROM h_eodj), (SELECT count(*) FROM h_long),
	                          (SELECT count(*) FROM h_multi), (SELECT count(*) FROM h_eodcrlf))") $(stat -c %s "$F_LONG") $(od -An -tx1 -j 65535 -N 2 "$F_LONG" | tr -d ' ')" \
	"2000 2000 1000 2000 1000 7501 2000 1000 125542 5c0a"
check "#2084 odd backslash run across a 64 KiB read / 2 workers: offsets are 0 65542 125542 (the newline at byte 65536 is escaped)" \
	"$(offsets_of "$F_LONG" 2)" "0 65542 125542"
check "#2084 odd backslash run across a 64 KiB read / 4 workers: offsets are 0 31392 65542 94167 125542" \
	"$(offsets_of "$F_LONG" 4)" "0 31392 65542 94167 125542"
check "#2084 odd backslash run across a 64 KiB read / single table, 4 workers: rows returned = heap COPY's 7501" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_long'::regclass, '$F_LONG', 4)")" "$(q "SELECT count(*) FROM h_long")"
check "#2084 odd backslash run across a 64 KiB read / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_long")" "$(pgc_set_hash "SELECT * FROM h_long")"
check "#2084 records of 3 and 4 lines, key after them / partitioned, 4 workers: rows returned = heap COPY's 2000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_multip'::regclass, '$F_MULTI', 4)")" "$(q "SELECT count(*) FROM h_multi")"
check "#2084 records of 3 and 4 lines, key after them / partitioned, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM t_multip")" "$(pgc_set_hash "SELECT * FROM h_multi")"
check "#2084 unsorted row after continued records / partitioned: refused (22000)" \
	"$(sqlstate_of "SELECT pgcolumnar.parallel_copy('t_contp'::regclass, '$F_CONTP', 4)")" 22000
check "#2084 file ends inside a continued record with an unsorted key / partitioned: refused (22000)" \
	"$(sqlstate_of "SELECT pgcolumnar.parallel_copy('t_eofrecp'::regclass, '$F_EOFREC', 4)")" 22000
check "#2084 CRLF end-of-data marker, bad rows after it / partitioned, 4 workers: rows returned = heap COPY's 1000" \
	"$(q "SELECT pgcolumnar.parallel_copy('t_eodcrlfp'::regclass, '$F_EODCRLF', 4)")" "$(q "SELECT count(*) FROM h_eodcrlf")"
check "#2084 CRLF end-of-data marker, bad rows after it / partitioned, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM t_eodcrlfp")" "$(pgc_set_hash "SELECT * FROM h_eodcrlf")"
check "#2084 CRLF end-of-data marker, bad rows after it / single table, 4 workers: rows returned = heap COPY's 1000" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_eodcrlf'::regclass, '$F_EODCRLF', 4)")" "$(q "SELECT count(*) FROM h_eodcrlf")"
check "#2084 CRLF end-of-data marker, bad rows after it / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_eodcrlf")" "$(pgc_set_hash "SELECT * FROM h_eodcrlf")"
_twobad="$(err_of "SELECT pgcolumnar.parallel_copy('c_twobad'::regclass, '$F_TWOBAD', 4)")"
check "#2084 two bad rows in different ranges / single table, 4 workers: the error names the first one, bad_early, as heap COPY's does" \
	"$(grep -o 'bad_[a-z]*' <<<"$_twobad" | sort -u | tr '\n' ' ' | sed 's/ $//')" "bad_early"

# Three more fixture gaps, from the review of #2148 at 29b71633.
#
# esc.txt (single table): each record is x1 \ t q \ LF y1 LF. The \t is an
# escape of a byte that is not a backslash or a newline, so the splitter must
# clear its escape flag there, and the newline after the next \ is escaped.
# Mutant a1.
#
# eodstart.txt (single table): 500 rows of 7 bytes, then \. at byte 3500, which
# is exactly the first interior split offset for 4 workers, then 1499 valid rows
# COPY never reads. The range that starts at the marker loads 0 rows and must
# still stop the load there. Mutant a8.
F_ESC="$DATADIR/esc.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) printf "x%d\\tq\\\ny%d\n", i, i }' >"$F_ESC"
F_EODSTART="$DATADIR/eodstart.txt"
awk 'BEGIN { for (i = 1; i <= 500; i++) printf "%04d\tx\n", i; printf "\\.\n";
             for (i = 1; i <= 1499; i++) printf "9999\ty\n" }' >"$F_EODSTART"
chmod 644 "$F_ESC" "$F_EODSTART"
psql_run "DROP TABLE IF EXISTS h_esc, h_eodstart, c_esc, c_eodstart CASCADE;
          CREATE TABLE h_esc (t text);                 COPY h_esc FROM '$F_ESC';
          CREATE TABLE h_eodstart (id int, txt text);  COPY h_eodstart FROM '$F_EODSTART';
          CREATE TABLE c_esc (t text) USING pgcolumnar;
          CREATE TABLE c_eodstart (id int, txt text) USING pgcolumnar;" >/dev/null
check "#2084 premise: heap COPY loads esc 2000 and eodstart 500 rows; eodstart.txt is 13996 bytes with 5c 2e 0a at bytes 3500-3502" \
	"$(q "SELECT concat_ws(' ', (SELECT count(*) FROM h_esc), (SELECT count(*) FROM h_eodstart))") $(stat -c %s "$F_EODSTART") $(od -An -tx1 -j 3500 -N 3 "$F_EODSTART" | tr -d ' ')" \
	"2000 500 13996 5c2e0a"
check "#2084 escape of a byte that is not a newline / 4 workers: offsets are 0 7456 14906 22346 29786" \
	"$(offsets_of "$F_ESC" 4)" "0 7456 14906 22346 29786"
check "#2084 escape of a byte that is not a newline / single table, 4 workers: rows returned = heap COPY's 2000" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_esc'::regclass, '$F_ESC', 4)")" "$(q "SELECT count(*) FROM h_esc")"
check "#2084 escape of a byte that is not a newline / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_esc")" "$(pgc_set_hash "SELECT * FROM h_esc")"
check "#2084 premise: eodstart.txt with 4 workers: offsets are 0 3500 7003 10503 13996, so the second range starts at the end-of-data marker" \
	"$(offsets_of "$F_EODSTART" 4)" "0 3500 7003 10503 13996"
check "#2084 end-of-data marker on a range start / single table, 4 workers: rows returned = heap COPY's 500" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_eodstart'::regclass, '$F_EODSTART', 4)")" "$(q "SELECT count(*) FROM h_eodstart")"
check "#2084 end-of-data marker on a range start / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_eodstart")" "$(pgc_set_hash "SELECT * FROM h_eodstart")"

# Two more fixture gaps, from the review of #2148 at 7e71ab06.
#
# eod.txt again, loaded twice with dedup on. The coordinator rolls back every
# range after the one that stopped at the end-of-data marker. On a dedup load
# nothing else resolves those ranges, so a skipped rollback leaves them
# prepared, holding locks. Mutant m1.
#
# contdot.txt (partitioned): row 999 ends in an escaped newline, so its record
# goes on into the next line, which is \. alone. That line is part of the
# record, not the end-of-data marker. PG 17's COPY reads on past it and loads
# all 2000 rows; PG 18's COPY refuses the file with 22P04. parallel_copy must
# give heap COPY's outcome on each major. Mutant m5, which only PG 17 shows.
F_CONTDOT="$DATADIR/contdot.txt"
awk 'BEGIN { for (i = 1; i <= 999; i++) printf "%d\tx\n", i; printf "999\tcont\\\n\\.\n";
             for (i = 1000; i <= 1999; i++) printf "%d\ty\n", i }' >"$F_CONTDOT"
chmod 644 "$F_CONTDOT"
psql_run "DROP TABLE IF EXISTS c_eodd, h_contdot, t_contdotp CASCADE;
          CREATE TABLE c_eodd (id int, txt text) USING pgcolumnar;
          CREATE TABLE h_contdot (id int, txt text);
          CREATE TABLE t_contdotp (id int, txt text) PARTITION BY RANGE (id);
          CREATE TABLE t_contdotp_a PARTITION OF t_contdotp FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar;
          CREATE TABLE t_contdotp_b PARTITION OF t_contdotp FOR VALUES FROM (500) TO (1000) USING pgcolumnar;
          CREATE TABLE t_contdotp_c PARTITION OF t_contdotp FOR VALUES FROM (1000) TO (1500) USING pgcolumnar;
          CREATE TABLE t_contdotp_d PARTITION OF t_contdotp FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
_eodd1="$(q "SELECT pgcolumnar.parallel_copy('c_eodd'::regclass, '$F_EOD', 4, true)")"
_eodd_leak1="$(q "SELECT count(*) FROM pg_prepared_xacts")"
_eodd2="$(q "SELECT pgcolumnar.parallel_copy('c_eodd'::regclass, '$F_EOD', 4, true)")"
_eodd_leak2="$(q "SELECT count(*) FROM pg_prepared_xacts")"
check "#2084 end-of-data marker, dedup on / single table, 4 workers: no prepared-transaction leak after the load or after its repeat" \
	"$_eodd_leak1 $_eodd_leak2" "0 0"
check "#2084 end-of-data marker, dedup on / single table, 4 workers: the load returns heap COPY's 1000, the repeat returns 0, the table holds 1000" \
	"$_eodd1 $_eodd2 $(q "SELECT count(*) FROM c_eodd")" "$(q "SELECT count(*) FROM h_eod") 0 $(q "SELECT count(*) FROM h_eod")"
# A leaked range holds locks for the rest of the run, so resolve any here.
for _g in $(q "SELECT gid FROM pg_prepared_xacts"); do q "ROLLBACK PREPARED '$_g'" >/dev/null; done

outcome_of() {	# SQL TABLE -> "N rows" (N = TABLE's count) when SQL ran, else the SQLSTATE it raised
	local s
	s="$(sqlstate_of "$1")"
	case "$s" in noerror*) echo "$(q "SELECT count(*) FROM $2") rows" ;; *) echo "$s" ;; esac
}
_srv="$(q 'SHOW server_version_num')"
if [ "${_srv:-0}" -ge 180000 ]; then _contdot_want="22P04"; else _contdot_want="2000 rows"; fi
_contdot_heap="$(outcome_of "COPY h_contdot FROM '$F_CONTDOT'" h_contdot)"
check "#2084 premise: heap COPY of contdot.txt loads 2000 rows on PG 17 and older, and refuses it with 22P04 on PG 18 and newer" \
	"$_contdot_heap" "$_contdot_want"
check "#2084 end-of-data marker line inside a continued record / partitioned, 4 workers: the outcome is heap COPY's" \
	"$(outcome_of "SELECT pgcolumnar.parallel_copy('t_contdotp'::regclass, '$F_CONTDOT', 4)" t_contdotp)" "$_contdot_heap"
check "#2084 end-of-data marker line inside a continued record / partitioned, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM t_contdotp")" "$(pgc_set_hash "SELECT * FROM h_contdot")"

# Two more fixture gaps, from the review of #2148 at 33724a76.
#
# eodcr.txt and eodnonl.txt (partitioned) end the data with a malformed marker:
# \.\r\n in an LF file, and \. at the end of the file with no newline. Heap COPY
# refuses both with 22P04. The partitioned splitter must keep the marker line in
# the last range, so core COPY in that loader sees it and refuses the file too.
# Mutants n22 (both files) and n55 (eodnonl.txt).
#
# eodearly.txt (single table): 100 rows, \. at byte 492, then 1900 rows COPY
# never reads. The marker is in the first range, so the load must stop there.
# Mutant n57.
F_EODCR="$DATADIR/eodcr.txt"
awk 'BEGIN { for (i = 1; i <= 1000; i++) printf "%d\tx\n", i; printf "\\.\r\n";
             for (i = 1001; i <= 2000; i++) printf "%d\ty\n", i }' >"$F_EODCR"
F_EODNONL="$DATADIR/eodnonl.txt"
awk 'BEGIN { for (i = 1; i <= 1000; i++) printf "%d\tx\n", i; printf "\\." }' >"$F_EODNONL"
F_EODEARLY="$DATADIR/eodearly.txt"
awk 'BEGIN { for (i = 1; i <= 100; i++) printf "%d\tx\n", i; printf "\\.\n";
             for (i = 101; i <= 2000; i++) printf "%d\tx\n", i }' >"$F_EODEARLY"
chmod 644 "$F_EODCR" "$F_EODNONL" "$F_EODEARLY"
psql_run "DROP TABLE IF EXISTS h_eodcr, h_eodnonl, h_eodearly, c_eodearly, t_eodcrp, t_eodnonlp CASCADE;
          CREATE TABLE h_eodcr (id int, txt text);
          CREATE TABLE h_eodnonl (id int, txt text);
          CREATE TABLE h_eodearly (id int, txt text); COPY h_eodearly FROM '$F_EODEARLY';
          CREATE TABLE c_eodearly (id int, txt text) USING pgcolumnar;" >/dev/null
for _t in t_eodcrp t_eodnonlp; do
	psql_run "CREATE TABLE $_t (id int, txt text) PARTITION BY RANGE (id);
	          CREATE TABLE ${_t}_a PARTITION OF $_t FOR VALUES FROM (MINVALUE) TO (500) USING pgcolumnar;
	          CREATE TABLE ${_t}_b PARTITION OF $_t FOR VALUES FROM (500) TO (1000) USING pgcolumnar;
	          CREATE TABLE ${_t}_c PARTITION OF $_t FOR VALUES FROM (1000) TO (1500) USING pgcolumnar;
	          CREATE TABLE ${_t}_d PARTITION OF $_t FOR VALUES FROM (1500) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
done
_eodcr_heap="$(outcome_of "COPY h_eodcr FROM '$F_EODCR'" h_eodcr)"
_eodnonl_heap="$(outcome_of "COPY h_eodnonl FROM '$F_EODNONL'" h_eodnonl)"
check "#2084 premise: heap COPY refuses eodcr.txt (\.\r\n in an LF file) and eodnonl.txt (\. at the end with no newline) with 22P04" \
	"$_eodcr_heap $_eodnonl_heap" "22P04 22P04"
check "#2084 malformed end-of-data marker \.\r\n in an LF file / partitioned, 4 workers: the outcome is heap COPY's" \
	"$(outcome_of "SELECT pgcolumnar.parallel_copy('t_eodcrp'::regclass, '$F_EODCR', 4)" t_eodcrp)" "$_eodcr_heap"
check "#2084 malformed end-of-data marker \.\r\n in an LF file / partitioned, 4 workers: the partitions hold 0 rows and no prepared transaction is left" \
	"$(q "SELECT count(*) FROM t_eodcrp") $(q "SELECT count(*) FROM pg_prepared_xacts")" "0 0"
check "#2084 end-of-data marker \. at the end with no newline / partitioned, 4 workers: the outcome is heap COPY's" \
	"$(outcome_of "SELECT pgcolumnar.parallel_copy('t_eodnonlp'::regclass, '$F_EODNONL', 4)" t_eodnonlp)" "$_eodnonl_heap"
check "#2084 end-of-data marker \. at the end with no newline / partitioned, 4 workers: the partitions hold 0 rows and no prepared transaction is left" \
	"$(q "SELECT count(*) FROM t_eodnonlp") $(q "SELECT count(*) FROM pg_prepared_xacts")" "0 0"
check "#2084 premise: heap COPY loads eodearly 100 rows; eodearly.txt is 12896 bytes with 5c 2e 0a at bytes 492-494; offsets with 4 workers are 0 3225 6449 9676 12896, so the marker is in the first range" \
	"$(q "SELECT count(*) FROM h_eodearly") $(stat -c %s "$F_EODEARLY") $(od -An -tx1 -j 492 -N 3 "$F_EODEARLY" | tr -d ' ') $(offsets_of "$F_EODEARLY" 4)" \
	"100 12896 5c2e0a 0 3225 6449 9676 12896"
check "#2084 end-of-data marker in the first range / single table, 4 workers: rows returned = heap COPY's 100" \
	"$(q "SELECT pgcolumnar.parallel_copy('c_eodearly'::regclass, '$F_EODEARLY', 4)")" "$(q "SELECT count(*) FROM h_eodearly")"
check "#2084 end-of-data marker in the first range / single table, 4 workers: result == heap COPY" \
	"$(pgc_set_hash "SELECT * FROM c_eodearly")" "$(pgc_set_hash "SELECT * FROM h_eodearly")"
check "#2084 end-of-data marker in the first range / single table, 4 workers: no prepared-transaction leak" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" 0

# ---- #2202: the key parser reads COPY escapes as COPY does ---------------------
# The partitioned splitter reads the fields up to the partition key itself, so it
# must split and decode them as core COPY's text format does, with the delimiter
# (TAB) and NULL marker (\N) the loaders use: a backslash escapes the byte after
# it, so a backslash and a raw TAB are a TAB inside the value, not a field end;
# \NNN (octal), \xHH (hex) and \b \f \n \r \t \v decode; two backslashes are one;
# any other escaped byte is itself; and a field that is exactly \N is NULL. Before
# the fix the parser split at every TAB byte and gave the raw key text to the
# integer input function, so these loads failed with 22P02.
#
# Each file is loaded by heap COPY into a heap table partitioned the same way,
# and by parallel_copy with 2 and 4 workers. The reading is the outcome (row
# count or SQLSTATE), the rows per partition bound, and a hash of the rows with
# their bound, and it must be heap COPY's.
#   esctab   (t, id)  t = a, backslash, TAB, b<i>              the issue's case 1
#   octkey   (id, t)  every key digit as \0NN                  the issue's case 2
#   hexkey   (id, t)  every key digit as \x3N
#   namedkey (id, t)  \t<i>, <i>\n, \v<i>\f, \r<i>, backslash-space <i>, \+<i>
#   escfield (t, id)  t holds \101 \x41 \b \f \n \r \t \v, two backslashes, a
#                     backslash and a TAB, and \N inside a longer value; every
#                     7th t is exactly \N (NULL)
#   bsbs     (t, id)  odd rows: t ends in two backslashes right before the
#                     delimiter; even rows: three backslashes, a TAB, then b
#   nullkey  (id, t)  row 1200's key is \N: COPY refuses it with 23514, since
#                     no RANGE partition takes NULL and a DEFAULT one is refused
#   plainkt, plaintk  controls, no escapes
F_ESCTAB="$DATADIR/esctab.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) printf "a\\\tb%d\t%d\n", i, i }' >"$F_ESCTAB"
F_OCTKEY="$DATADIR/octkey.txt"
awk 'function oct(n,   s, k, d) { s = ""; n = n ""; for (k = 1; k <= length(n); k++) { d = substr(n, k, 1) + 0; s = s (d < 8 ? "\\06" d : "\\07" (d - 8)) } return s }
     BEGIN { for (i = 1; i <= 2000; i++) printf "%s\tx%d\n", oct(i), i }' >"$F_OCTKEY"
F_HEXKEY="$DATADIR/hexkey.txt"
awk 'function hex(n,   s, k) { s = ""; n = n ""; for (k = 1; k <= length(n); k++) s = s "\\x3" substr(n, k, 1); return s }
     BEGIN { for (i = 1; i <= 2000; i++) printf "%s\tx%d\n", hex(i), i }' >"$F_HEXKEY"
F_NAMEDKEY="$DATADIR/namedkey.txt"
awk 'BEGIN { split("\\t%d|%d\\n|\\v%d\\f|\\r%d|\\ %d|\\+%d", fm, "|");
             for (i = 1; i <= 2000; i++) { printf fm[i % 6 + 1], i; printf "\tx%d\n", i } }' >"$F_NAMEDKEY"
F_ESCFIELD="$DATADIR/escfield.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++)
               if (i % 7 == 0) printf "\\N\t%d\n", i;
               else printf "o\\101h\\x41n\\b\\f\\n\\r\\t\\v\\\\s\\\tq\\N%d\t%d\n", i, i }' >"$F_ESCFIELD"
F_BSBS="$DATADIR/bsbs.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++)
               if (i % 2) printf "a%d\\\\\t%d\n", i, i; else printf "a%d\\\\\\\tb\t%d\n", i, i }' >"$F_BSBS"
F_NULLKEY="$DATADIR/nullkey.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\N\tnull\n"; else printf "%d\tx%d\n", i, i }' >"$F_NULLKEY"
F_PLAINKT="$DATADIR/plainkt.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) printf "%d\tx%d\n", i, i }' >"$F_PLAINKT"
F_PLAINTK="$DATADIR/plaintk.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) printf "a%d\t%d\n", i, i }' >"$F_PLAINTK"
chmod 644 "$F_ESCTAB" "$F_OCTKEY" "$F_HEXKEY" "$F_NAMEDKEY" "$F_ESCFIELD" "$F_BSBS" "$F_NULLKEY" "$F_PLAINKT" "$F_PLAINTK"
mkesc() {	# NAME "COLUMNS" AM -> NAME, RANGE-partitioned on id at 500, 1000 and 1500, partitions USING AM
	psql_run "DROP TABLE IF EXISTS $1 CASCADE;
	          CREATE TABLE $1 ($2) PARTITION BY RANGE (id);
	          CREATE TABLE $1_a PARTITION OF $1 FOR VALUES FROM (MINVALUE) TO (500) USING $3;
	          CREATE TABLE $1_b PARTITION OF $1 FOR VALUES FROM (500) TO (1000) USING $3;
	          CREATE TABLE $1_c PARTITION OF $1 FOR VALUES FROM (1000) TO (1500) USING $3;
	          CREATE TABLE $1_d PARTITION OF $1 FOR VALUES FROM (1500) TO (MAXVALUE) USING $3;" >/dev/null
}
for _e in esctab:tk octkey:kt hexkey:kt namedkey:kt escfield:tk bsbs:tk nullkey:kt plainkt:kt plaintk:tk; do
	case "${_e#*:}" in kt) _cols="id int, t text" ;; *) _cols="t text, id int" ;; esac
	mkesc "h_${_e%%:*}" "$_cols" heap
	mkesc "t_${_e%%:*}" "$_cols" pgcolumnar
done
esc_msg_outcome() {	# SQL TABLE -> "SQLSTATE: primary message" of the error SQL raised, or "N rows"
	local out o
	out="$(env PGOPTIONS='-c statement_timeout=30s' PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -qtA -c '\set VERBOSITY verbose' -c "$1" 2>&1)"
	o="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1: \2/p' <<<"$out" | head -1)"
	if [ -n "$o" ]; then echo "$o"; else echo "$(q "SELECT count(*) FROM $2") rows"; fi
}
esc_reading() {	# SQL TABLE [msg] -> "outcome | rows per partition bound | hash of the rows with their bound";
	# with msg, an error's outcome carries its primary message too (esc_msg_outcome)
	local o
	if [ "${3:-}" = msg ]; then o="$(esc_msg_outcome "$1" "$2")"; else o="$(outcome_of "$1" "$2")"; fi
	echo "$o | $(q "SELECT coalesce(string_agg(b || '=' || n, ', ' ORDER BY b), 'no rows')
	                FROM (SELECT pg_get_expr(c.relpartbound, c.oid) AS b, count(*) AS n
	                      FROM $2 x JOIN pg_class c ON c.oid = x.tableoid GROUP BY 1) s") | $(pgc_set_hash "SELECT pg_get_expr(c.relpartbound, c.oid) AS b, x.* FROM $2 x JOIN pg_class c ON c.oid = x.tableoid")"
}
esc_pcopy() {	# NAME FILE WORKERS [msg] -> esc_reading of a parallel_copy of FILE into an emptied t_NAME
	psql_run "TRUNCATE t_$1" >/dev/null
	esc_reading "SELECT pgcolumnar.parallel_copy('t_$1'::regclass, '$2', $3)" "t_$1" "${4:-}"
}
for _e in esctab octkey hexkey namedkey escfield bsbs nullkey plainkt plaintk; do
	_f="$DATADIR/$_e.txt"
	printf -v "ESC_HEAP_$_e" '%s' "$(esc_reading "COPY h_$_e FROM '$_f'" "h_$_e")"
done
check "premise: heap COPY loads esctab, octkey, hexkey, namedkey, escfield, bsbs, plainkt and plaintk 2000 rows each and refuses nullkey with 23514; esctab's t is a, TAB, b<id> on every row; the files start 61 5c 09 62 (esctab) and 5c 30 36 31 (octkey) (#2202)" \
	"$(for _e in esctab octkey hexkey namedkey escfield bsbs plainkt plaintk nullkey; do _v="ESC_HEAP_$_e"; printf '%s ' "${!_v%% |*}"; done)$(q "SELECT count(*) FROM h_esctab WHERE t = 'a' || chr(9) || 'b' || id") $(od -An -tx1 -N 4 "$F_ESCTAB" | tr -d ' ') $(od -An -tx1 -N 4 "$F_OCTKEY" | tr -d ' ')" \
	"2000 rows 2000 rows 2000 rows 2000 rows 2000 rows 2000 rows 2000 rows 2000 rows 23514 2000 615c0962 5c303631"
check "#2202 a backslash before a raw TAB in the field before the key / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy esctab "$F_ESCTAB" 2)" "$ESC_HEAP_esctab"
check "#2202 a backslash before a raw TAB in the field before the key / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy esctab "$F_ESCTAB" 4)" "$ESC_HEAP_esctab"
check "#2202 a key written in octal escapes / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy octkey "$F_OCTKEY" 2)" "$ESC_HEAP_octkey"
check "#2202 a key written in octal escapes / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy octkey "$F_OCTKEY" 4)" "$ESC_HEAP_octkey"
check "#2202 a key written in hex escapes / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexkey "$F_HEXKEY" 2)" "$ESC_HEAP_hexkey"
check "#2202 a key written in hex escapes / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexkey "$F_HEXKEY" 4)" "$ESC_HEAP_hexkey"
check "#2202 a key with named escapes and escaped plain bytes around it / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy namedkey "$F_NAMEDKEY" 2)" "$ESC_HEAP_namedkey"
check "#2202 a key with named escapes and escaped plain bytes around it / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy namedkey "$F_NAMEDKEY" 4)" "$ESC_HEAP_namedkey"
check "#2202 octal, hex and named escapes, an escaped TAB and NULL markers in the field before the key / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy escfield "$F_ESCFIELD" 2)" "$ESC_HEAP_escfield"
check "#2202 octal, hex and named escapes, an escaped TAB and NULL markers in the field before the key / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy escfield "$F_ESCFIELD" 4)" "$ESC_HEAP_escfield"
check "#2202 an escaped backslash right before the delimiter in the field before the key / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy bsbs "$F_BSBS" 2)" "$ESC_HEAP_bsbs"
check "#2202 an escaped backslash right before the delimiter in the field before the key / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy bsbs "$F_BSBS" 4)" "$ESC_HEAP_bsbs"
check "#2202 a NULL marker key / partitioned, 2 workers: outcome (23514), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy nullkey "$F_NULLKEY" 2)" "$ESC_HEAP_nullkey"
check "#2202 a NULL marker key / partitioned, 4 workers: outcome (23514), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy nullkey "$F_NULLKEY" 4)" "$ESC_HEAP_nullkey"
check "#2202 control, plain keys, key first / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy plainkt "$F_PLAINKT" 2)" "$ESC_HEAP_plainkt"
check "#2202 control, plain keys, key first / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy plainkt "$F_PLAINKT" 4)" "$ESC_HEAP_plainkt"
check "#2202 control, plain keys, key last / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy plaintk "$F_PLAINTK" 2)" "$ESC_HEAP_plaintk"
check "#2202 control, plain keys, key last / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy plaintk "$F_PLAINTK" 4)" "$ESC_HEAP_plaintk"

# Over-applied rules, from the review of the first version: each file below loads,
# or fails, the way heap COPY does only if the decoder applies its rules no wider
# than COPY. The checks compare the same reading as above.
#   nprefkey  (id, t)  row 1200's key is \N5: not the NULL marker, so COPY reads
#                      "N5" and refuses it with 22P02
#   bsnkey    (id, t)  row 1200's key is two backslashes and N: the value \N,
#                      not NULL, so 22P02 again
# For these two the outcome also carries the error's message. A key read as NULL
# by mistake stays in its range, and core COPY in the loader then refuses the row
# with the same SQLSTATE, so only the message, which a loader's error carries
# behind "pgcolumnar.parallel_copy worker N failed:", tells the two apart.
#   escother  (id, t)  every 8 and 9 in a key written as a backslash and the digit:
#                      not an escape, so the digit itself, and every row loads
#   hexlit    (t, id)  t ends in \x right before the delimiter (odd rows) or holds
#                      \xg (even rows): \x with no hex digit is a literal x, and
#                      it does not take the TAB after it
#   hexlitkey (id, t)  row 1200's key ends in \x: a literal x, so 22P02, not the
#                      22021 a NUL would give
#   nulkey    (id, t)  row 1200's key is \x00, a NUL byte: COPY refuses it with
#                      22021
#   highbit   (id, t)  row 1200's key ends in \351, a lone 0xE9 byte, which is not
#                      UTF-8: COPY refuses it with 22021
# nulkey and highbit load in a database created with ENCODING 'UTF8', so the
# encoding check does not depend on the cluster's locale.
F_NPREFKEY="$DATADIR/nprefkey.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\N5\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_NPREFKEY"
F_BSNKEY="$DATADIR/bsnkey.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\\\N\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_BSNKEY"
F_ESCOTHER="$DATADIR/escother.txt"
awk 'function esc(n,   s, k, c) { s = ""; n = n ""; for (k = 1; k <= length(n); k++) { c = substr(n, k, 1); s = s (c == "8" || c == "9" ? "\\" c : c) } return s }
     BEGIN { for (i = 1; i <= 2000; i++) printf "%s\tx%d\n", esc(i), i }' >"$F_ESCOTHER"
F_HEXLIT="$DATADIR/hexlit.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i % 2) printf "a%d\\x\t%d\n", i, i; else printf "a%d\\xg\t%d\n", i, i }' >"$F_HEXLIT"
F_HEXLITKEY="$DATADIR/hexlitkey.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "1200\\x\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_HEXLITKEY"
F_NULKEY="$DATADIR/nulkey.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\x00\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_NULKEY"
F_HIGHBIT="$DATADIR/highbit.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "1200\\351\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_HIGHBIT"
chmod 644 "$F_NPREFKEY" "$F_BSNKEY" "$F_ESCOTHER" "$F_HEXLIT" "$F_HEXLITKEY" "$F_NULKEY" "$F_HIGHBIT"
ESC_UTF8=pgc_pcopy_utf8
psql_admin "DROP DATABASE IF EXISTS $ESC_UTF8" >/dev/null 2>&1
psql_admin "CREATE DATABASE $ESC_UTF8 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0" >/dev/null
PGC_DB=$ESC_UTF8 psql_run "CREATE EXTENSION pgcolumnar" >/dev/null
for _e in nprefkey:kt bsnkey:kt escother:kt hexlit:tk hexlitkey:kt nulkey:kt:u highbit:kt:u; do
	case "$_e" in *:kt*) _cols="id int, t text" ;; *) _cols="t text, id int" ;; esac
	_n="${_e%%:*}"; _db="$PGC_DB"; case "$_e" in *:u) _db="$ESC_UTF8" ;; esac
	PGC_DB="$_db" mkesc "h_$_n" "$_cols" heap
	PGC_DB="$_db" mkesc "t_$_n" "$_cols" pgcolumnar
done
for _e in escother hexlit hexlitkey; do
	printf -v "ESC_HEAP_$_e" '%s' "$(esc_reading "COPY h_$_e FROM '$DATADIR/$_e.txt'" "h_$_e")"
done
for _e in nprefkey bsnkey; do
	printf -v "ESC_HEAP_$_e" '%s' "$(esc_reading "COPY h_$_e FROM '$DATADIR/$_e.txt'" "h_$_e" msg)"
done
for _e in nulkey highbit; do
	printf -v "ESC_HEAP_$_e" '%s' "$(PGC_DB=$ESC_UTF8 esc_reading "COPY h_$_e FROM '$DATADIR/$_e.txt'" "h_$_e")"
done
check "premise: heap COPY loads escother and hexlit 2000 rows each, refuses nprefkey with 22P02 on N5, bsnkey with 22P02 on backslash N, hexlitkey with 22P02 and nulkey and highbit with 22021; hexlit's t is a<id>x or a<id>xg; the database nulkey and highbit load in is UTF8 (#2202)" \
	"$(for _e in escother hexlit nprefkey bsnkey hexlitkey nulkey highbit; do _v="ESC_HEAP_$_e"; printf '%s ' "${!_v%% |*}"; done)$(q "SELECT count(*) FROM h_hexlit WHERE t = 'a' || id || CASE WHEN id % 2 = 1 THEN 'x' ELSE 'xg' END") $(PGC_DB=$ESC_UTF8 q "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()")" \
	'2000 rows 2000 rows 22P02: invalid input syntax for type integer: "N5" 22P02: invalid input syntax for type integer: "\N" 22P02 22021 22021 2000 UTF8'
check "#2202 a key that starts with the NULL marker but is longer (\N5) / partitioned, 2 workers: outcome (22P02 and its message), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy nprefkey "$F_NPREFKEY" 2 msg)" "$ESC_HEAP_nprefkey"
check "#2202 a key that starts with the NULL marker but is longer (\N5) / partitioned, 4 workers: outcome (22P02 and its message), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy nprefkey "$F_NPREFKEY" 4 msg)" "$ESC_HEAP_nprefkey"
check "#2202 a key whose decoded value is backslash N, not the NULL marker / partitioned, 2 workers: outcome (22P02 and its message), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy bsnkey "$F_BSNKEY" 2 msg)" "$ESC_HEAP_bsnkey"
check "#2202 a key whose decoded value is backslash N, not the NULL marker / partitioned, 4 workers: outcome (22P02 and its message), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy bsnkey "$F_BSNKEY" 4 msg)" "$ESC_HEAP_bsnkey"
check "#2202 a key with escaped digits 8 and 9, which are not escapes / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy escother "$F_ESCOTHER" 2)" "$ESC_HEAP_escother"
check "#2202 a key with escaped digits 8 and 9, which are not escapes / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy escother "$F_ESCOTHER" 4)" "$ESC_HEAP_escother"
check "#2202 a hex escape with no hex digit, before the delimiter, in the field before the key / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexlit "$F_HEXLIT" 2)" "$ESC_HEAP_hexlit"
check "#2202 a hex escape with no hex digit, before the delimiter, in the field before the key / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexlit "$F_HEXLIT" 4)" "$ESC_HEAP_hexlit"
check "#2202 a key that ends in a hex escape with no hex digit / partitioned, 2 workers: outcome (22P02), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexlitkey "$F_HEXLITKEY" 2)" "$ESC_HEAP_hexlitkey"
check "#2202 a key that ends in a hex escape with no hex digit / partitioned, 4 workers: outcome (22P02), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexlitkey "$F_HEXLITKEY" 4)" "$ESC_HEAP_hexlitkey"
check "#2202 a key that decodes to a NUL byte, in a UTF8 database / partitioned, 2 workers: outcome (22021), rows per partition and rows == heap COPY's" \
	"$(PGC_DB=$ESC_UTF8 esc_pcopy nulkey "$F_NULKEY" 2)" "$ESC_HEAP_nulkey"
check "#2202 a key that decodes to a NUL byte, in a UTF8 database / partitioned, 4 workers: outcome (22021), rows per partition and rows == heap COPY's" \
	"$(PGC_DB=$ESC_UTF8 esc_pcopy nulkey "$F_NULKEY" 4)" "$ESC_HEAP_nulkey"
check "#2202 a key that decodes to a byte that is not UTF-8, in a UTF8 database / partitioned, 2 workers: outcome (22021), rows per partition and rows == heap COPY's" \
	"$(PGC_DB=$ESC_UTF8 esc_pcopy highbit "$F_HIGHBIT" 2)" "$ESC_HEAP_highbit"
check "#2202 a key that decodes to a byte that is not UTF-8, in a UTF8 database / partitioned, 4 workers: outcome (22021), rows per partition and rows == heap COPY's" \
	"$(PGC_DB=$ESC_UTF8 esc_pcopy highbit "$F_HIGHBIT" 4)" "$ESC_HEAP_highbit"

# Six more decoder rules, from jdatcmd's review of 19bb9309 (mutants M04-M07, M10
# and M13 there). Same reading as above.
#   crtk     (t, id)  CR-only line ends: CR ends the key field, and the file has
#                     no LF at all
#   barecr   (t, id)  row 1200 is a, TAB, 1200, a raw CR, junk: COPY refuses the
#                     raw CR with 22P04, and the key is 1200, not 1200junk
#   bseof    (t, id)  the last row, 2000, ends in a lone backslash with no newline
#                     after it: COPY drops the backslash
#   hex3     (id, t)  row 1200's key is \x31200: \x reads two hex digits, \x31 is
#                     1, and the key is 1200
#   hexspc   (id, t)  row 1200's key is \x201200\x20: a space each side
#   oct400   (id, t)  row 1200's key is 12\400: \400 is 0x100 & 0xFF, a NUL, so
#                     22021 (in the UTF8 database), not a key of 12
#   hexupper (id, t)  row 1200's key is \x2B1200, +1200: an uppercase hex digit
#   toofew   (t, id)  row 1200 is just a, with no key field: COPY refuses it with
#                     22P04, and so must the splitter, not read an empty key
F_CRTK="$DATADIR/crtk.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) printf "a%d\t%d\r", i, i }' >"$F_CRTK"
F_BARECR="$DATADIR/barecr.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "a\t1200\rjunk\n"; else printf "a%d\t%d\n", i, i }' >"$F_BARECR"
F_BSEOF="$DATADIR/bseof.txt"
awk 'BEGIN { for (i = 1; i < 2000; i++) printf "a%d\t%d\n", i, i; printf "a2000\t2000\\" }' >"$F_BSEOF"
F_HEX3="$DATADIR/hex3.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\x31200\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_HEX3"
F_HEXSPC="$DATADIR/hexspc.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\x201200\\x20\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_HEXSPC"
F_OCT400="$DATADIR/oct400.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "12\\400\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_OCT400"
F_HEXUPPER="$DATADIR/hexupper.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\x2B1200\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_HEXUPPER"
F_TOOFEW="$DATADIR/toofew.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "a\n"; else printf "a%d\t%d\n", i, i }' >"$F_TOOFEW"
chmod 644 "$F_CRTK" "$F_BARECR" "$F_BSEOF" "$F_HEX3" "$F_HEXSPC" "$F_OCT400" "$F_HEXUPPER" "$F_TOOFEW"
for _e in crtk:tk barecr:tk bseof:tk hex3:kt hexspc:kt oct400:kt:u hexupper:kt toofew:tk; do
	case "$_e" in *:kt*) _cols="id int, t text" ;; *) _cols="t text, id int" ;; esac
	_n="${_e%%:*}"; _db="$PGC_DB"; case "$_e" in *:u) _db="$ESC_UTF8" ;; esac
	PGC_DB="$_db" mkesc "h_$_n" "$_cols" heap
	PGC_DB="$_db" mkesc "t_$_n" "$_cols" pgcolumnar
done
for _e in crtk barecr bseof hex3 hexspc hexupper toofew; do
	printf -v "ESC_HEAP_$_e" '%s' "$(esc_reading "COPY h_$_e FROM '$DATADIR/$_e.txt'" "h_$_e")"
done
printf -v ESC_HEAP_oct400 '%s' "$(PGC_DB=$ESC_UTF8 esc_reading "COPY h_oct400 FROM '$F_OCT400'" h_oct400)"
check "premise: heap COPY loads crtk, bseof, hex3, hexspc and hexupper 2000 rows each and refuses barecr and toofew with 22P04 and oct400 with 22021; crtk.txt holds no LF byte and bseof.txt ends in 30 5c (#2202)" \
	"$(for _e in crtk bseof hex3 hexspc hexupper barecr toofew oct400; do _v="ESC_HEAP_$_e"; printf '%s ' "${!_v%% |*}"; done)$(tr -cd '\n' <"$F_CRTK" | wc -c) $(tail -c 2 "$F_BSEOF" | od -An -tx1 | tr -d ' ')" \
	"2000 rows 2000 rows 2000 rows 2000 rows 2000 rows 22P04 22P04 22021 0 305c"
check "#2202 CR-only line ends / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy crtk "$F_CRTK" 2)" "$ESC_HEAP_crtk"
check "#2202 CR-only line ends / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy crtk "$F_CRTK" 4)" "$ESC_HEAP_crtk"
check "#2202 a raw CR in the middle of the key's line / partitioned, 2 workers: outcome (22P04), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy barecr "$F_BARECR" 2)" "$ESC_HEAP_barecr"
check "#2202 a raw CR in the middle of the key's line / partitioned, 4 workers: outcome (22P04), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy barecr "$F_BARECR" 4)" "$ESC_HEAP_barecr"
check "#2202 a lone backslash at the end of the file, after the key / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy bseof "$F_BSEOF" 2)" "$ESC_HEAP_bseof"
check "#2202 a lone backslash at the end of the file, after the key / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy bseof "$F_BSEOF" 4)" "$ESC_HEAP_bseof"
check "#2202 a hex escape followed by a third hex digit in the key / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hex3 "$F_HEX3" 2)" "$ESC_HEAP_hex3"
check "#2202 a hex escape followed by a third hex digit in the key / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hex3 "$F_HEX3" 4)" "$ESC_HEAP_hex3"
check "#2202 a key between two hex-escaped spaces / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexspc "$F_HEXSPC" 2)" "$ESC_HEAP_hexspc"
check "#2202 a key between two hex-escaped spaces / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexspc "$F_HEXSPC" 4)" "$ESC_HEAP_hexspc"
check "#2202 a key with an octal escape that decodes to a NUL byte, in a UTF8 database / partitioned, 2 workers: outcome (22021), rows per partition and rows == heap COPY's" \
	"$(PGC_DB=$ESC_UTF8 esc_pcopy oct400 "$F_OCT400" 2)" "$ESC_HEAP_oct400"
check "#2202 a key with an octal escape that decodes to a NUL byte, in a UTF8 database / partitioned, 4 workers: outcome (22021), rows per partition and rows == heap COPY's" \
	"$(PGC_DB=$ESC_UTF8 esc_pcopy oct400 "$F_OCT400" 4)" "$ESC_HEAP_oct400"
check "#2202 a key with an uppercase hex escape / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexupper "$F_HEXUPPER" 2)" "$ESC_HEAP_hexupper"
check "#2202 a key with an uppercase hex escape / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexupper "$F_HEXUPPER" 4)" "$ESC_HEAP_hexupper"
check "#2202 a row with too few fields for the key / partitioned, 2 workers: outcome (22P04), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy toofew "$F_TOOFEW" 2)" "$ESC_HEAP_toofew"
check "#2202 a row with too few fields for the key / partitioned, 4 workers: outcome (22P04), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy toofew "$F_TOOFEW" 4)" "$ESC_HEAP_toofew"

# Two more, from jdatcmd's re-review at 960286e1 (M15 and M16 there).
#   nullkeylast (t, id)  row 1200 is a, TAB, \N: the NULL marker in the last
#                        column, ended by the LF, so 23514 as for nullkey
#   hexlower    (id, t)  row 1200's key is \x2b1200, +1200: a lowercase hex digit
#   dateesc     (id date, t)  keys are 2020-01-01 plus 1..2000 days; row 1200's
#                        key has its dashes written \055 and \x2d. Partitioned on
#                        the date at plus 500, 1000 and 1500 days. The server
#                        writes it as tab-delimited CSV, which leaves backslashes
#                        alone.
F_NULLKEYLAST="$DATADIR/nullkeylast.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "a\t\\N\n"; else printf "a%d\t%d\n", i, i }' >"$F_NULLKEYLAST"
F_HEXLOWER="$DATADIR/hexlower.txt"
awk 'BEGIN { for (i = 1; i <= 2000; i++) if (i == 1200) printf "\\x2b1200\tx%d\n", i; else printf "%d\tx%d\n", i, i }' >"$F_HEXLOWER"
F_DATEESC="$DATADIR/dateesc.txt"
psql_run "COPY (SELECT CASE WHEN g = 1200 THEN to_char(d, 'YYYY') || '\\055' || to_char(d, 'MM') || '\\x2d' || to_char(d, 'DD')
                            ELSE to_char(d, 'YYYY-MM-DD') END, 'x' || g
                FROM (SELECT g, date '2020-01-01' + g AS d FROM generate_series(1, 2000) g) s ORDER BY g)
          TO '$F_DATEESC' WITH (FORMAT csv, DELIMITER E'\\t')" >/dev/null
chmod 644 "$F_NULLKEYLAST" "$F_HEXLOWER"
mkesc h_nullkeylast "t text, id int" heap
mkesc t_nullkeylast "t text, id int" pgcolumnar
mkesc h_hexlower "id int, t text" heap
mkesc t_hexlower "id int, t text" pgcolumnar
for _am in h:heap t:pgcolumnar; do
	_t="${_am%%:*}_dateesc"; _u="${_am#*:}"
	psql_run "DROP TABLE IF EXISTS $_t CASCADE;
	          CREATE TABLE $_t (id date, t text) PARTITION BY RANGE (id);
	          CREATE TABLE ${_t}_a PARTITION OF $_t FOR VALUES FROM (MINVALUE) TO ('2021-05-15') USING $_u;
	          CREATE TABLE ${_t}_b PARTITION OF $_t FOR VALUES FROM ('2021-05-15') TO ('2022-09-27') USING $_u;
	          CREATE TABLE ${_t}_c PARTITION OF $_t FOR VALUES FROM ('2022-09-27') TO ('2024-02-09') USING $_u;
	          CREATE TABLE ${_t}_d PARTITION OF $_t FOR VALUES FROM ('2024-02-09') TO (MAXVALUE) USING $_u;" >/dev/null
done
for _e in nullkeylast hexlower dateesc; do
	printf -v "ESC_HEAP_$_e" '%s' "$(esc_reading "COPY h_$_e FROM '$DATADIR/$_e.txt'" "h_$_e")"
done
check "premise: heap COPY loads hexlower and dateesc 2000 rows each and refuses nullkeylast with 23514; dateesc.txt's line 1200 is 2023, 5c 30 35 35, 04, 5c 78 32 64, 15, and heap COPY reads it as 2023-04-15; the date bounds are 500, 1000 and 1500 days after 2020-01-01 (#2202)" \
	"$(for _e in hexlower dateesc nullkeylast; do _v="ESC_HEAP_$_e"; printf '%s ' "${!_v%% |*}"; done)$(sed -n '1200p' "$F_DATEESC" | cut -f1 | tr -d '\n' | od -An -tx1 | tr -d ' \n') $(q "SELECT count(*) FROM h_dateesc WHERE id = date '2023-04-15'") $(q "SELECT (date '2021-05-15' - date '2020-01-01') || ' ' || (date '2022-09-27' - date '2020-01-01') || ' ' || (date '2024-02-09' - date '2020-01-01')")" \
	"2000 rows 2000 rows 23514 323032335c30353530345c7832643135 1 500 1000 1500"
check "#2202 a NULL marker key in the last column / partitioned, 2 workers: outcome (23514), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy nullkeylast "$F_NULLKEYLAST" 2)" "$ESC_HEAP_nullkeylast"
check "#2202 a NULL marker key in the last column / partitioned, 4 workers: outcome (23514), rows per partition and rows == heap COPY's" \
	"$(esc_pcopy nullkeylast "$F_NULLKEYLAST" 4)" "$ESC_HEAP_nullkeylast"
check "#2202 a key with a lowercase hex escape / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexlower "$F_HEXLOWER" 2)" "$ESC_HEAP_hexlower"
check "#2202 a key with a lowercase hex escape / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy hexlower "$F_HEXLOWER" 4)" "$ESC_HEAP_hexlower"
check "#2202 a date key written with octal and lowercase hex escapes / partitioned, 2 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy dateesc "$F_DATEESC" 2)" "$ESC_HEAP_dateesc"
check "#2202 a date key written with octal and lowercase hex escapes / partitioned, 4 workers: outcome, rows per partition and rows == heap COPY's" \
	"$(esc_pcopy dateesc "$F_DATEESC" 4)" "$ESC_HEAP_dateesc"

# ---- #2080: a single table with a unique index needs one worker ------------
# Each loader keeps its unique-key locks in a prepared transaction until every
# loader has finished, so two loaders of one table with a unique index waited on
# each other, even with distinct keys: the load hung or failed with 40P01.
# parallel_copy now refuses that load up front. statement_timeout turns the old
# hang into 57014, a red the check can read. The controls: 1 worker, and a non-unique index, still load.
want_unique() {	# TABLE -> the refusal #2080 promises
	printf '0A000 pgcolumnar.parallel_copy cannot load "%s" with more than one worker, because it has a unique or exclusion index | Load it with workers => 1, or with COPY.\n' "$1"
}
# t_uk gets a second, non-unique index after its primary key, so the refusal
# must find the unique index even when it is not the last one in the list.
# t_ui gets a non-unique index before its unique one, so the refusal must also
# find the unique index when it is not the first one. t_ui's unique index has
# two key columns, so the refusal must not count only one-column indexes.
psql_run "CREATE TABLE t_uk (id int PRIMARY KEY, txt text) USING pgcolumnar;
          CREATE INDEX t_uk_txt ON t_uk (txt);
          CREATE TABLE t_ui (id int, txt text) USING pgcolumnar;
          CREATE INDEX t_ui_txt ON t_ui (txt);
          CREATE UNIQUE INDEX t_ui_id ON t_ui (id, txt);
          CREATE TABLE t_uk1 (id int PRIMARY KEY, txt text) USING pgcolumnar;
          CREATE TABLE t_nu (id int, txt text) USING pgcolumnar;
          CREATE INDEX t_nu_id ON t_nu (id);" >/dev/null
check "parallel_copy refuses a single table with a primary key and 2 workers with 0A000, not a hang (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '$F', 2)")" \
	"$(want_unique t_uk)"
check "parallel_copy refuses a single table with a unique index and 4 workers with 0A000, not a hang (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_ui'::regclass, '$F', 4)")" \
	"$(want_unique t_ui)"
# An exclusion constraint has indisunique false and indisexclusion true. Its
# keys are distinct, so a load that is not refused would succeed: the check reads
# the refusal, not a hang.
psql_run "CREATE TABLE t_ux (id int, txt text, EXCLUDE USING btree (id WITH =)) USING pgcolumnar;" >/dev/null
check "parallel_copy refuses a single table with an exclusion constraint and 2 workers with 0A000 (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_ux'::regclass, '$F', 2)")" \
	"$(want_unique t_ux)"
# A deferrable unique index and a partial unique index are refused too. The
# loaders take the per-key unique lock for a partial index's matching rows, and
# every row here matches. Columnar's own unique-lock set skips deferrable
# indexes, but a deferrable unique table with duplicate keys hung on main too.
# The keys are distinct, so a deferrable load that is not refused would succeed.
psql_run "CREATE TABLE t_ud (id int UNIQUE DEFERRABLE INITIALLY DEFERRED, txt text) USING pgcolumnar;
          CREATE TABLE t_up (id int, txt text) USING pgcolumnar;
          CREATE UNIQUE INDEX t_up_id ON t_up (id) WHERE id >= 0;" >/dev/null
check "parallel_copy refuses a single table with a deferrable unique index and 2 workers with 0A000 (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_ud'::regclass, '$F', 2)")" \
	"$(want_unique t_ud)"
check "parallel_copy refuses a single table with a partial unique index and 2 workers with 0A000, not a hang (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_up'::regclass, '$F', 2)")" \
	"$(want_unique t_up)"
# A deferrable exclusion constraint is refused too: indisunique is false and
# indimmediate is false, so only indisexclusion catches it. Columnar's own
# unique-lock set skips indexes that are not immediate, so a refusal that copied
# that filter would let this load run. An expression unique index is refused
# too: the loaders hash its key into their unique locks, so two loaders wait on
# each other even with distinct keys. The keys are distinct, so a load that is
# not refused would succeed or hang, and either is red.
psql_run "CREATE TABLE t_uxd (id int, txt text,
            EXCLUDE USING btree (id WITH =) DEFERRABLE INITIALLY DEFERRED) USING pgcolumnar;
          CREATE TABLE t_ue (id int, txt text) USING pgcolumnar;
          CREATE UNIQUE INDEX t_ue_id ON t_ue ((id + 0));" >/dev/null
check "parallel_copy refuses a single table with a deferrable exclusion constraint and 2 workers with 0A000 (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uxd'::regclass, '$F', 2)")" \
	"$(want_unique t_uxd)"
check "parallel_copy refuses a single table with an expression unique index and 2 workers with 0A000, not a hang (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_ue'::regclass, '$F', 2)")" \
	"$(want_unique t_ue)"
# A gist exclusion constraint is refused like a btree one: the refusal must not
# look at the index's access method. The ranges are distinct and do not overlap.
# A unique index with an INCLUDE column and NULLS NOT DISTINCT is refused too: the
# refusal must not compare indnatts with indnkeyatts or read indnullsnotdistinct.
psql_run "CREATE TABLE t_gx (id int, txt text,
            EXCLUDE USING gist (int4range(id, id, '[]') WITH &&)) USING pgcolumnar;
          CREATE TABLE t_inc (id int, txt text,
            UNIQUE NULLS NOT DISTINCT (id) INCLUDE (txt)) USING pgcolumnar;" >/dev/null
check "parallel_copy refuses a single table with a gist exclusion constraint and 2 workers with 0A000, not a hang (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_gx'::regclass, '$F', 2)")" \
	"$(want_unique t_gx)"
check "parallel_copy refuses a single table with a NULLS NOT DISTINCT unique index with an INCLUDE column and 2 workers with 0A000, not a hang (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_inc'::regclass, '$F', 2)")" \
	"$(want_unique t_inc)"
# The refusal keeps its place in the order of checks. A caller without INSERT
# gets 42501 first (#563), a URL gets the #1886 refusal first, and dedup => true
# does not skip the calling backend's refusal or its hint.
psql_run "DO \$\$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pcopy_noins') THEN
              CREATE ROLE pcopy_noins; END IF; END \$\$;
          GRANT pg_read_server_files TO pcopy_noins;
          GRANT USAGE ON SCHEMA pgcolumnar TO pcopy_noins;
          GRANT SELECT ON t_uk TO pcopy_noins;" >/dev/null
check "parallel_copy gives a caller without INSERT 42501 before the unique-index refusal (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s -c role=pcopy_noins' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '$F', 2)")" \
	"42501 permission denied for table t_uk | no hint"
check "parallel_copy refuses an s3:// URL with the #1886 refusal before the unique-index refusal (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '$REMOTE_S3', 2)")" \
	"$(want_refusal parallel_copy "$REMOTE_S3")"
check "parallel_copy refuses a single table with a primary key, 2 workers and dedup => true with 0A000 and the hint (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '$F', 2, dedup => true)")" \
	"$(want_unique t_uk)"
# A heap target with a primary key gets 42809 first (#2209). The unique-index
# refusal's hint says "workers => 1", and one worker would still get 42809, so
# the refusal whose cause holds for every worker count comes first.
check "premise: t_heaptgt, the heap target, has a primary key (#2209)" \
	"$(q "SELECT count(*) FROM pg_index WHERE indrelid = 't_heaptgt'::regclass AND indisprimary")" 1
check "parallel_copy gives a heap target with a primary key and 2 workers 42809 before the unique-index refusal (#2209)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_heaptgt'::regclass, '$F', 2)")" \
	'42809 "t_heaptgt" is not a pgcolumnar table or a partitioned table | no hint'
# The refusal comes before the file is opened (#2209): it depends only on the
# table and the worker count, so a missing file on a table with a primary key
# gets 0A000, not 58P01. With 1 worker the same call opens the file and gets
# 58P01, which shows that the path is really missing.
check "parallel_copy refuses a missing file for a single table with a primary key and 2 workers with 0A000, before it opens the file (#2209)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '$DATADIR/nope.txt', 2)")" \
	"$(want_unique t_uk)"
check "control: parallel_copy gives a missing file for a single table with a primary key and 1 worker 58P01 (#2209)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uk'::regclass, '$DATADIR/nope.txt', 1)")" \
	"58P01 could not open file \"$DATADIR/nope.txt\" for reading: No such file or directory | no hint"
# An invalid unique index counts too. CREATE UNIQUE INDEX CONCURRENTLY fails on
# a columnar table and leaves the index invalid but ready, and a ready unique
# index still checks every insert, so two loaders would wait on each other. The
# keys are distinct, so a load that is not refused would succeed. CONCURRENTLY
# cannot run in a transaction block, so it runs as a statement of its own.
psql_run "CREATE TABLE t_uv (id int, txt text) USING pgcolumnar;" >/dev/null
q "CREATE UNIQUE INDEX CONCURRENTLY t_uv_id ON t_uv (id)" >/dev/null
check "premise: CREATE UNIQUE INDEX CONCURRENTLY leaves a columnar table an invalid, ready unique index (#2080)" \
	"$(q "SELECT concat_ws('|', indisunique, indisvalid, indisready) FROM pg_index WHERE indrelid = 't_uv'::regclass")" "t|f|t"
check "parallel_copy refuses a single table with an invalid unique index and 2 workers with 0A000 (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_uv'::regclass, '$F', 2)")" \
	"$(want_unique t_uv)"
# The default worker count is more than one here: max_parallel_workers=4 gives
# 2. The calling backend must refuse it with its hint, not leave it to a loader.
psql_run "CREATE TABLE t_ukd (id int PRIMARY KEY, txt text) USING pgcolumnar;" >/dev/null
check "parallel_copy refuses a single table with a primary key and the default worker count with 0A000 (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=10s -c max_parallel_workers=4' remote_refusal "SELECT pgcolumnar.parallel_copy('t_ukd'::regclass, '$F')")" \
	"$(want_unique t_ukd)"
check "the unique-index refusal loads nothing (#2080)" \
	"$(q "SELECT (SELECT count(*) FROM t_uk) + (SELECT count(*) FROM t_ui) + (SELECT count(*) FROM t_ud)
	         + (SELECT count(*) FROM t_up) + (SELECT count(*) FROM t_uv) + (SELECT count(*) FROM t_ukd)
	         + (SELECT count(*) FROM t_ue) + (SELECT count(*) FROM t_inc)")" 0
check "the exclusion-constraint refusal loads nothing (#2080)" \
	"$(q "SELECT (SELECT count(*) FROM t_ux) + (SELECT count(*) FROM t_uxd) + (SELECT count(*) FROM t_gx)")" 0
check "control: parallel_copy with 1 worker still loads a single table with a primary key (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=30s' q "SELECT pgcolumnar.parallel_copy('t_uk1'::regclass, '$F', 1)")" 5000
check "control: parallel_copy with 2 workers still loads a single table with a non-unique index (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=30s' q "SELECT pgcolumnar.parallel_copy('t_nu'::regclass, '$F', 2)")" 5000
# A partitioned target keeps more than one worker even with a primary key: each
# worker loads distinct partitions, so distinct indexes. Its parent's index is
# unique, so a refusal that also covered partitioned targets would refuse this.
psql_run "CREATE TABLE t_ppk (id int PRIMARY KEY, txt text) PARTITION BY RANGE (id);
          CREATE TABLE t_ppk_1 PARTITION OF t_ppk FOR VALUES FROM (MINVALUE) TO (1251) USING pgcolumnar;
          CREATE TABLE t_ppk_2 PARTITION OF t_ppk FOR VALUES FROM (1251) TO (2501) USING pgcolumnar;
          CREATE TABLE t_ppk_3 PARTITION OF t_ppk FOR VALUES FROM (2501) TO (3751) USING pgcolumnar;
          CREATE TABLE t_ppk_4 PARTITION OF t_ppk FOR VALUES FROM (3751) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
check "control: parallel_copy with 4 workers still loads a partitioned table with a primary key (#2080)" \
	"$(PGOPTIONS='-c statement_timeout=30s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_ppk'::regclass, '$F', 4)") | $(q "SELECT count(*) FROM t_ppk")" \
	"no error (5000) | 5000"

# A unique index created after the calling backend's check, but before the loaders
# open the table, must be refused too: the check runs under AccessShareLock, which
# CREATE INDEX does not wait for. A second session holds SHARE on t_ur, which lets
# the check pass but stops the coordinator at its ROW EXCLUSIVE lock. No loader
# has started yet. Once the coordinator waits, the session creates the unique
# index and commits, and the loaders then open a table that has it.
# Which worker reports first varies, so the check reads "worker N".
pcopy_psql() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -q "$@"; }
late_unique() {	# TABLE WORKERS -> the load's refusal, with "worker N"
	late_ddl "$1" "$2" "CREATE UNIQUE INDEX ${1}_id ON $1 (id);"
}
late_ddl() {	# TABLE WORKERS DDL -> the same, with DDL run in place of the index
	local t="$1" w="$2" pid got
	pcopy_psql >/dev/null 2>&1 <<SQL &
BEGIN;
LOCK TABLE $t IN SHARE MODE;
DO \$\$ BEGIN FOR i IN 1..200 LOOP
	EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = '$t'::regclass AND NOT granted);
	PERFORM pg_sleep(0.05); END LOOP; END \$\$;
$3
COMMIT;
SQL
	pid=$!
	for _ in $(seq 100); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = '$t'::regclass AND mode = 'ShareLock' AND granted")" = 1 ] && break
		sleep 0.1
	done
	got="$(PGOPTIONS='-c statement_timeout=20s' "${LATE_READER:-remote_refusal}" "SELECT pgcolumnar.parallel_copy('$t'::regclass, '$F', $w)" \
		| sed 's/worker [0-9]* failed/worker N failed/')"
	wait "$pid"
	printf '%s\n' "$got"
}
psql_run "CREATE TABLE t_ur (id int, txt text) USING pgcolumnar;" >/dev/null
ur_got="$(late_unique t_ur 2)"
# The loader's HINT reaches the caller too (#2187): it used to read "no hint".
check "parallel_copy refuses 2 workers when a unique index appears after its check, not a hang (#2080)" \
	"$ur_got" \
	'0A000 pgcolumnar.parallel_copy worker N failed: pgcolumnar.parallel_copy cannot load "t_ur" with more than one worker, because it has a unique or exclusion index | Load it with workers => 1, or with COPY.'
check "the late unique-index refusal loads nothing (#2080)" \
	"$(q "SELECT count(*) FROM t_ur")" 0
# The same with 4 workers, where every loader, not only one, must refuse. A
# loader that skips the check would insert rows against its siblings, which
# hangs or fails only in some runs. A row trigger that calls nextval records
# any insert, because a sequence does not roll back: no loader may insert a row.
# The trigger arrives with the index, after the calling backend's check, which
# would refuse the trigger itself (#2187). Each loader checks the index first.
psql_run "CREATE TABLE t_ur4 (id int, txt text) USING pgcolumnar;
          CREATE SEQUENCE t_ur4_rows;
          CREATE FUNCTION t_ur4_row() RETURNS trigger LANGUAGE plpgsql
            AS \$\$ BEGIN PERFORM nextval('t_ur4_rows'); RETURN NEW; END \$\$;" >/dev/null
check "parallel_copy refuses 4 workers when a unique index appears after its check, before any loader inserts a row (#2080)" \
	"$(late_ddl t_ur4 4 "CREATE UNIQUE INDEX t_ur4_id ON t_ur4 (id);
		CREATE TRIGGER t_ur4_row BEFORE INSERT ON t_ur4 FOR EACH ROW EXECUTE FUNCTION t_ur4_row();") | $(q "SELECT CASE WHEN is_called THEN 'a loader inserted a row' ELSE 'no row inserted' END FROM t_ur4_rows")" \
	'0A000 pgcolumnar.parallel_copy worker N failed: pgcolumnar.parallel_copy cannot load "t_ur4" with more than one worker, because it has a unique or exclusion index | Load it with workers => 1, or with COPY. | no row inserted'

# ---- #2187: a lock queued between the loaders, a trigger, a cancel ------------
# 1. A CREATE INDEX that queues for SHARE after the first loader has its ROW
#    EXCLUSIVE lock, and before the others ask, used to hang the load: the first
#    loader prepared, CREATE INDEX waited on its prepared transaction, and the
#    others waited behind CREATE INDEX. pgcolumnar.parallel_copy_open_hold_ms
#    holds every loader but the first before it opens the table, so the index can
#    be queued in that gap every run; the premise reads that it was. The load
#    must now finish, and the index build after it.
pcopy_hold() {	# MS -> sets the loaders' hold in this database, 0 resets it
	if [ "$1" = 0 ]; then psql_run "ALTER DATABASE \"$PGC_DB\" RESET pgcolumnar.parallel_copy_open_hold_ms;"
	else psql_run "ALTER DATABASE \"$PGC_DB\" SET pgcolumnar.parallel_copy_open_hold_ms = $1;"; fi
}
LOADER="backend_type = 'pgcolumnar parallel_copy loader'"
loaders_on() {	# TABLE -> "H held, W waiting": the loaders' locks on TABLE. A loader
	# that has prepared holds its lock with no process (pid is null).
	q "SELECT count(*) FILTER (WHERE l.granted) || ' held, ' || count(*) FILTER (WHERE NOT l.granted) || ' waiting'
	   FROM pg_locks l LEFT JOIN pg_stat_activity a USING (pid)
	   WHERE l.relation = '$1'::regclass AND (l.pid IS NULL OR a.$LOADER)"
}
index_mid_open() {	# TABLE WORKERS DDL -> "what was queued when DDL waited || load | ddl | rows | index valid | leak"
	local t="$1" w="$2" lp dp seen
	pcopy_hold 4000 >/dev/null
	( PGOPTIONS='-c statement_timeout=15s' remote_refusal \
		"SELECT pgcolumnar.parallel_copy('$t'::regclass, '$F', $w)" >"$DATADIR/$t.load" ) &
	lp=$!
	for _ in $(seq 100); do
		case "$(loaders_on "$t")" in "0 held"*|"") sleep 0.05 ;; *) break ;; esac
	done
	( PGOPTIONS='-c statement_timeout=15s' remote_refusal "$3" >"$DATADIR/$t.ddl" ) &
	dp=$!
	for _ in $(seq 100); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = '$t'::regclass AND mode = 'ShareLock' AND NOT granted")" = 1 ] && break
		sleep 0.05
	done
	seen="loaders $(loaders_on "$t"), index $(q "SELECT count(*) FROM pg_locks WHERE relation = '$t'::regclass AND mode = 'ShareLock' AND NOT granted") waiting"
	wait "$lp"; wait "$dp"
	pcopy_hold 0 >/dev/null
	printf '%s || %s | %s | %s | valid %s | leak %s\n' "$seen" "$(cat "$DATADIR/$t.load")" "$(cat "$DATADIR/$t.ddl")" \
		"$(q "SELECT count(*) FROM $t")" "$(q "SELECT indisvalid FROM pg_index WHERE indexrelid = '${t}_id'::regclass")" \
		"$(q "SELECT count(*) FROM pg_prepared_xacts")"
}
psql_run "CREATE TABLE t_mo (id int, txt text) USING pgcolumnar;
          CREATE TABLE t_mo4 (id int, txt text) USING pgcolumnar;" >/dev/null
mo_got="$(index_mid_open t_mo 2 "CREATE UNIQUE INDEX t_mo_id ON t_mo (id)")"
check "premise: CREATE UNIQUE INDEX queued while 1 of 2 loaders held the table and the other had not asked (#2187)" \
	"${mo_got%% || *}" "loaders 1 held, 0 waiting, index 1 waiting"
check "parallel_copy with 2 workers finishes when CREATE UNIQUE INDEX queues between its loaders' locks, and the index builds after it (#2187)" \
	"${mo_got#* || }" "no error (5000) | no error (CREATE INDEX) | 5000 | valid t | leak 0"
mo4_got="$(index_mid_open t_mo4 4 "CREATE INDEX t_mo4_id ON t_mo4 (id)")"
check "premise: CREATE INDEX queued while 1 of 4 loaders held the table and the others had not asked (#2187)" \
	"${mo4_got%% || *}" "loaders 1 held, 0 waiting, index 1 waiting"
check "parallel_copy with 4 workers finishes when CREATE INDEX queues between its loaders' locks, and the index builds after it (#2187)" \
	"${mo4_got#* || }" "no error (5000) | no error (CREATE INDEX) | 5000 | valid t | leak 0"

# A loader that exits before it opens the table must not leave its siblings
# waiting for it to open it: the load fails, it does not hang.
psql_run "CREATE TABLE t_gone (id int, txt text) USING pgcolumnar;" >/dev/null
gone_load() {	# -> "load | rows | leak", with the second loader terminated while it is held
	local lp victim=""
	pcopy_hold 4000 >/dev/null
	( PGOPTIONS='-c statement_timeout=15s' remote_refusal \
		"SELECT pgcolumnar.parallel_copy('t_gone'::regclass, '$F', 2)" >"$DATADIR/t_gone.load" ) &
	lp=$!
	for _ in $(seq 100); do
		case "$(loaders_on t_gone)" in "1 held"*)
			victim="$(q "SELECT pid FROM pg_stat_activity WHERE $LOADER AND pid NOT IN
				(SELECT pid FROM pg_locks WHERE relation = 't_gone'::regclass AND pid IS NOT NULL)")"
			[ -n "$victim" ] && break ;;
		esac
		sleep 0.05
	done
	q "SELECT pg_terminate_backend($victim)" >/dev/null
	wait "$lp"
	pcopy_hold 0 >/dev/null
	printf '%s | %s | leak %s\n' "$(cat "$DATADIR/t_gone.load")" "$(q "SELECT count(*) FROM t_gone")" \
		"$(q "SELECT count(*) FROM pg_prepared_xacts")"
}
check "a loader terminated before it opens the table fails the load instead of hanging its sibling (#2187)" \
	"$(gone_load)" \
	"XX000 pgcolumnar.parallel_copy worker 1 failed: loader exited without reporting a result | no hint | 0 | leak 0"

# 2. The caller must not get its error back while the coordinator still holds
#    prepared transactions. A cancelled load returned at once, and the caller's
#    own next statement saw the loaders' prepared transactions. Here the first
#    loader's foreign-key check waits on a row another session locked, the
#    second loader prepares, and statement_timeout cancels the load. The count
#    is read in the caller's own backend the moment the error reaches it: a
#    plpgsql handler catches the 57014 and counts at once, so the reading does
#    not depend on how fast a client sends its next statement.
psql_run "CREATE TABLE t_cx_ref (id int PRIMARY KEY);
          INSERT INTO t_cx_ref SELECT generate_series(1, 5000);
          CREATE TABLE t_cx (id int REFERENCES t_cx_ref, txt text) USING pgcolumnar;
          CREATE FUNCTION t_cx_load(f text) RETURNS text LANGUAGE plpgsql AS \$\$
          BEGIN
            PERFORM pgcolumnar.parallel_copy('t_cx'::regclass, f, 2);
            RETURN 'no error';
          EXCEPTION WHEN query_canceled THEN
            RETURN SQLSTATE || ' ' || SQLERRM || ' | same session ' || (SELECT count(*) FROM pg_prepared_xacts);
          END \$\$;" >/dev/null
cancelled_load() {	# -> "premise || error | prepared seen by the same session"
	local kp lp seen="" out
	pcopy_psql -c "BEGIN" -c "SELECT id FROM t_cx_ref WHERE id = 1 FOR UPDATE" -c "SELECT pg_sleep(10)" -c "COMMIT" >/dev/null 2>&1 &
	kp=$!
	for _ in $(seq 100); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = 't_cx_ref'::regclass AND mode = 'RowShareLock' AND granted")" = 1 ] && break
		sleep 0.05
	done
	( env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c statement_timeout=6s' psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -X -At -c "SELECT t_cx_load('$F')" >"$DATADIR/t_cx.load" 2>&1 ) &
	lp=$!
	for _ in $(seq 100); do
		seen="prepared $(q "SELECT count(*) FROM pg_prepared_xacts"), loaders waiting on a row $(q "SELECT count(*) FROM pg_stat_activity WHERE $LOADER AND wait_event = 'transactionid'")"
		[ "$seen" = "prepared 1, loaders waiting on a row 1" ] && break
		sleep 0.05
	done
	wait "$lp"; wait "$kp"
	out="$(cat "$DATADIR/t_cx.load")"
	printf '%s || %s\n' "$seen" "$out"
}
cx_got="$(cancelled_load)"
check "before the cancel, one loader had prepared and the other waited on a locked row (#2187)" \
	"${cx_got%% || *}" "prepared 1, loaders waiting on a row 1"
check "a cancelled parallel_copy leaves no prepared transaction for the caller's next statement (#2187)" \
	"${cx_got#* || } | after $(q "SELECT count(*) FROM pg_prepared_xacts")" \
	"57014 canceling statement due to statement timeout | same session 0 | after 0"

# 3. A trigger on INSERT can write to another table. Each loader runs it in its
#    own prepared transaction, so the loaders waited on each other over what
#    the trigger wrote: on main a row trigger that inserts into a table with a
#    primary key failed the load with 40P01, and a statement trigger hung it.
#    With more than one worker the load is now refused before it starts.
want_trigger() {	# TABLE TRIGGER -> the refusal #2187 promises
	printf '0A000 pgcolumnar.parallel_copy cannot load "%s" with more than one worker, because it has trigger "%s" on INSERT | Load it with workers => 1, or with COPY.\n' "$1" "$2"
}
psql_run "CREATE TABLE t_tgk (id int PRIMARY KEY) USING pgcolumnar;
          CREATE FUNCTION t_tg_row() RETURNS trigger LANGUAGE plpgsql
            AS \$\$ BEGIN INSERT INTO t_tgk VALUES (NEW.id); RETURN NEW; END \$\$;
          CREATE FUNCTION t_tg_stmt() RETURNS trigger LANGUAGE plpgsql
            AS \$\$ BEGIN INSERT INTO t_tgk SELECT -pg_backend_pid(); RETURN NULL; END \$\$;
          CREATE TABLE t_tga (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tga_k AFTER INSERT ON t_tga FOR EACH ROW EXECUTE FUNCTION t_tg_row();
          CREATE TABLE t_tgb (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgb_k BEFORE INSERT ON t_tgb FOR EACH ROW EXECUTE FUNCTION t_tg_row();
          CREATE TABLE t_tgs (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgs_k AFTER INSERT ON t_tgs FOR EACH STATEMENT EXECUTE FUNCTION t_tg_stmt();" >/dev/null
check "parallel_copy refuses a single table with an AFTER INSERT row trigger and 2 workers with 0A000, not a deadlock (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tga'::regclass, '$F', 2)")" \
	"$(want_trigger t_tga t_tga_k)"
check "parallel_copy refuses a single table with a BEFORE INSERT row trigger and 2 workers with 0A000, not a deadlock (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgb'::regclass, '$F', 2)")" \
	"$(want_trigger t_tgb t_tgb_k)"
check "parallel_copy refuses a single table with an AFTER INSERT statement trigger and 4 workers with 0A000, not a hang (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgs'::regclass, '$F', 4)")" \
	"$(want_trigger t_tgs t_tgs_k)"
# A trigger that writes a columnar table with no index and no storage row yet
# hung the load instead (issue #2187, comment of 2026-11-27): the first loader
# took that table's storage-row lock and prepared, and the second waited on it.
psql_run "CREATE TABLE t_tgn_log (id int) USING pgcolumnar;
          CREATE FUNCTION t_tg_fresh() RETURNS trigger LANGUAGE plpgsql
            AS \$\$ BEGIN INSERT INTO t_tgn_log VALUES (NEW.id); RETURN NEW; END \$\$;
          CREATE TABLE t_tgn (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgn_k AFTER INSERT ON t_tgn FOR EACH ROW EXECUTE FUNCTION t_tg_fresh();" >/dev/null
check "parallel_copy refuses a single table whose AFTER INSERT row trigger writes a fresh columnar table with no index, 2 workers, with 0A000, not a hang (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgn'::regclass, '$F', 2)") | $(q "SELECT count(*) FROM t_tgn") $(q "SELECT count(*) FROM t_tgn_log") | leak $(q "SELECT count(*) FROM pg_prepared_xacts")" \
	"$(want_trigger t_tgn t_tgn_k) | 0 0 | leak 0"
check "the trigger refusals load nothing, write nothing through the trigger and leave no prepared transaction (#2187)" \
	"$(q "SELECT (SELECT count(*) FROM t_tga) || ' ' || (SELECT count(*) FROM t_tgb) || ' ' || (SELECT count(*) FROM t_tgs) || ' | ' || (SELECT count(*) FROM t_tgk)") | leak $(q "SELECT count(*) FROM pg_prepared_xacts")" \
	"0 0 0 | 0 | leak 0"
# A trigger created after the calling backend's check is refused by the loaders,
# with the hint.
psql_run "CREATE TABLE t_tgl (id int, txt text) USING pgcolumnar;" >/dev/null
check "parallel_copy refuses 2 workers when an INSERT trigger appears after its check, with the hint (#2187)" \
	"$(late_ddl t_tgl 2 "CREATE TRIGGER t_tgl_k AFTER INSERT ON t_tgl FOR EACH ROW EXECUTE FUNCTION t_tg_row();") | $(q "SELECT count(*) FROM t_tgl") $(q "SELECT count(*) FROM t_tgk")" \
	'0A000 pgcolumnar.parallel_copy worker N failed: pgcolumnar.parallel_copy cannot load "t_tgl" with more than one worker, because it has trigger "t_tgl_k" on INSERT | Load it with workers => 1, or with COPY. | 0 0'
# Controls. One worker still loads, and runs the trigger. A disabled trigger, a
# foreign key's internal triggers and a trigger on UPDATE only do not fire on
# INSERT from these loaders, so 2 workers still load those tables.
psql_run "CREATE TABLE t_tg1 (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tg1_k AFTER INSERT ON t_tg1 FOR EACH ROW EXECUTE FUNCTION t_tg_row();
          CREATE TABLE t_tgd (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgd_k AFTER INSERT ON t_tgd FOR EACH ROW EXECUTE FUNCTION t_tg_row();
          ALTER TABLE t_tgd DISABLE TRIGGER t_tgd_k;
          CREATE TABLE t_tgf_ref (id int PRIMARY KEY);
          INSERT INTO t_tgf_ref SELECT generate_series(1, 5000);
          CREATE TABLE t_tgf (id int REFERENCES t_tgf_ref, txt text) USING pgcolumnar;
          CREATE TABLE t_tgu (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgu_k AFTER UPDATE ON t_tgu FOR EACH ROW EXECUTE FUNCTION t_tg_row();" >/dev/null
check "control: parallel_copy with 1 worker still loads a single table with an INSERT trigger, and the trigger runs (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=30s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tg1'::regclass, '$F', 1)") | $(q "SELECT count(*) FROM t_tgk")" \
	"no error (5000) | 5000"
check "control: parallel_copy with 2 workers still loads a single table whose INSERT trigger is disabled (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=30s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgd'::regclass, '$F', 2)") | $(q "SELECT count(*) FROM t_tgd")" \
	"no error (5000) | 5000"
check "control: parallel_copy with 2 workers still loads a single table with a foreign key (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=30s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgf'::regclass, '$F', 2)") | $(q "SELECT count(*) FROM t_tgf")" \
	"no error (5000) | 5000"
check "control: parallel_copy with 2 workers still loads a single table with an UPDATE-only trigger (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=30s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgu'::regclass, '$F', 2)") | $(q "SELECT count(*) FROM t_tgu")" \
	"no error (5000) | 5000"

# ---- #2200: a temporal primary key is a unique AND an exclusion index ---------
# PostgreSQL 18's PRIMARY KEY (id, during WITHOUT OVERLAPS) builds one index with
# indisunique and indisexclusion both true, the one shape no table above has. So
# the refusal must not test that exactly one of the two flags is set: with
# `indisunique != indisexclusion` every check above passed, and this load of
# distinct keys hung until its statement_timeout (57014). The file carries a
# third column, so the rows fit the table: a load that is not refused would run.
# The int key part needs btree_gist, which test/temporal.sh needs too. A missing
# btree_gist fails this check on 18 and later (#448: a box that cannot run it has
# not passed it). WITHOUT OVERLAPS does not exist before 18, so the check is a
# SKIP there. The major is asserted first, so an unreadable one fails the check
# rather than being taken for an old one.
if ! pgc_is_number "${PGC_MAJOR:-}"; then
	check "parallel_copy refuses a single table with a temporal primary key (unique and exclusion) and 2 workers with 0A000 and the hint, and loads nothing (#2200)" \
		"unreadable server major [${PGC_MAJOR:-<none>}]" "a server major to gate on"
elif [ "$PGC_MAJOR" -ge 18 ]; then
	F_TPK="$DATADIR/temporal_pk.txt"
	psql_run "COPY (SELECT g, 'host_'||g, daterange(date '2020-01-01' + g, date '2020-01-01' + g + 1)
	                FROM generate_series(1, 5000) g) TO '$F_TPK' WITH (FORMAT text);" >/dev/null
	tpk_setup="$(remote_refusal "CREATE EXTENSION IF NOT EXISTS btree_gist;
	    CREATE TABLE t_utp (id int, txt text, during daterange,
	      PRIMARY KEY (id, during WITHOUT OVERLAPS)) USING pgcolumnar;")"
	if [ "${tpk_setup#no error (}" = "$tpk_setup" ]; then
		check "parallel_copy refuses a single table with a temporal primary key (unique and exclusion) and 2 workers with 0A000 and the hint, and loads nothing (#2200)" "setup failed: $tpk_setup" "$(want_unique t_utp) | 0 rows"
	else
		check "parallel_copy refuses a single table with a temporal primary key (unique and exclusion) and 2 workers with 0A000 and the hint, and loads nothing (#2200)" \
			"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_utp'::regclass, '$F_TPK', 2)") | $(q "SELECT count(*) FROM t_utp") rows" \
			"$(want_unique t_utp) | 0 rows"
	fi
else
	check_skip "parallel_copy refuses a single table with a temporal primary key (unique and exclusion) and 2 workers with 0A000 and the hint, and loads nothing (#2200)" \
		"SKIP  parallel_copy refuses a single table with a temporal primary key (unique and exclusion) and 2 workers with 0A000 and the hint, and loads nothing (#2200) (PostgreSQL $PGC_MAJOR has no WITHOUT OVERLAPS; 18 added it)" \
		"PostgreSQL $PGC_MAJOR has no WITHOUT OVERLAPS"
fi

# ---- #2092: a load into a table with a projection fills the projection ---------
# #2092 asked whether parallel_copy shares the import functions' crash: they wrote
# a projection at statement end with no active snapshot. It does not: on main's
# code, too, both cases below load and no backend dies. The check keeps that true.
# Each table gets an options row (set_options), because the crash needed one.
# The 2-worker case fills its projection first. A projection that holds no rows
# yet is #2161's case, checked in its own section at the end of this suite.
proj_load() {	# proj_load WORKERS PRELOAD -> "wWORKERS:returned/rows/projection rows"
	local t="t_proj_w$1"
	psql_run "CREATE TABLE $t (id int, txt text) USING pgcolumnar;
		SELECT pgcolumnar.set_options('$t', compression => 'zstd');" >/dev/null
	[ "$2" -gt 0 ] && psql_run "INSERT INTO $t SELECT g, 'pre'||g FROM generate_series(1, $2) g;" >/dev/null
	psql_run "SELECT pgcolumnar.add_projection('$t', 'p', columns => ARRAY['txt','id'], sort_key => ARRAY['id']);" >/dev/null
	printf 'w%s:%s/%s/%s' "$1" \
		"$(PGOPTIONS='-c statement_timeout=60s' q "SELECT pgcolumnar.parallel_copy('$t'::regclass, '$F', $1)")" \
		"$(q "SELECT count(*) FROM $t")" "$(q "SELECT count(*) FROM pgcolumnar.read_projection('$t', 'p')")"
}
_proj_got="$(proj_load 1 0) $(proj_load 2 1000)"
check "parallel_copy into a table with a projection fills the projection, leaves no prepared transaction and keeps the server up (#2092)" \
	"$_proj_got | leak $(q "SELECT count(*) FROM pg_prepared_xacts") | survived $(survived "$CRASHES_BEFORE")" \
	"w1:5000/5000/5000 w2:5000/6000/6000 | leak 0 | survived 1"

# ---- #2183: a lock the caller holds is refused, not waited on ------------------
# The coordinator and the loaders open the target with ROW EXCLUSIVE in their own
# transactions, and the loaders route rows into its partitions. They are not in
# the caller's lock group, and the caller waits for them on a latch, so the
# deadlock detector cannot see the wait: after LOCK TABLE ... IN ACCESS EXCLUSIVE
# MODE, TRUNCATE, or LOCK TABLE ... IN SHARE MODE in the caller's transaction, the
# load waited until statement_timeout. It is now refused up front with 55000,
# naming the lock and the relation. A lock ROW EXCLUSIVE does not conflict with
# (ROW EXCLUSIVE on the target, ACCESS EXCLUSIVE on another table) must still
# load, and a lock on one partition is refused like one on the table.
# Every run gets a 5s statement_timeout, so the old wait reads 57014, not a hang.
#
# self_lock_copy SETUP TABLE [WORKERS] -> "SQLSTATE|message" of the first ERROR, or
# "none|<rows returned>" when BEGIN; SETUP; parallel_copy of F into TABLE with WORKERS
# (default 2); COMMIT ran.
psql_run "DROP TABLE IF EXISTS t_lkc, t_lkc_other;
          CREATE TABLE t_lkc (id int, txt text) USING pgcolumnar;
          CREATE TABLE t_lkc_other (a int);" >/dev/null
mkpart t_lkp 4
self_lock_copy() {
	local out rc
	out="$(timeout -s KILL 60 env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 \
		-c "SET statement_timeout = '5s'" -c '\set VERBOSITY verbose' \
		-c "BEGIN; $1; SELECT pgcolumnar.parallel_copy('$2'::regclass, '$F', ${3:-2}); COMMIT;" 2>&1)"
	rc=$?
	if grep -q '^ERROR:' <<<"$out"; then
		sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' <<<"$out" | head -1
	elif [ "$rc" -eq 0 ]; then
		echo "none|$(tail -1 <<<"$out")"
	else
		echo "no reading (psql exit $rc: $(head -1 <<<"$out"))"
	fi
}
LKC_REFUSED='55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_lkc"'
check "parallel_copy after LOCK TABLE IN ACCESS EXCLUSIVE MODE in the same transaction is refused with 55000, not waited on (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkc IN ACCESS EXCLUSIVE MODE" t_lkc)" "$LKC_REFUSED"
check "parallel_copy after TRUNCATE in the same transaction is refused with 55000, not waited on (#2183)" \
	"$(self_lock_copy "TRUNCATE t_lkc" t_lkc)" "$LKC_REFUSED"
check "parallel_copy after LOCK TABLE IN SHARE MODE in the same transaction is refused with 55000, not waited on (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkc IN SHARE MODE" t_lkc)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lkc"'
check "parallel_copy after LOCK TABLE IN ROW EXCLUSIVE MODE in the same transaction still loads (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkc IN ROW EXCLUSIVE MODE" t_lkc)" "none|5000"
check "parallel_copy while the transaction holds ACCESS EXCLUSIVE on another table still loads (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkc_other IN ACCESS EXCLUSIVE MODE" t_lkc)" "none|5000"
check "parallel_copy into a partitioned table after LOCK TABLE on one partition IN ACCESS EXCLUSIVE MODE is refused with 55000 (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkp_p2 IN ACCESS EXCLUSIVE MODE" t_lkp)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_lkp_p2"'
check "parallel_copy refused for a lock the caller holds loads nothing and leaves no prepared transaction (#2183)" \
	"$(q "SELECT count(*) FROM t_lkc") rows, $(q "SELECT count(*) FROM t_lkp") partitioned rows, $(q "SELECT count(*) FROM pg_prepared_xacts") prepared" \
	"10000 rows, 0 partitioned rows, 0 prepared"
# The order of the refusals (#2183 with #2080). parallel_copy checks, in order:
# INSERT (42501), row-level security, a remote URL (0A000), then the caller's own
# lock (55000), and only then a unique index with more than one worker (0A000).
# The lock refusal comes before the unique-index one because its cause holds for
# every worker count: the unique-index hint says "workers => 1", and with that a
# caller holding SHARE would only reach the lock refusal next. A caller without
# INSERT still gets 42501 first, whatever lock it holds. pcopy_noins holds
# UPDATE on t_lko, which is enough to LOCK it IN SHARE MODE, and no INSERT.
psql_run "DROP TABLE IF EXISTS t_lko;
          CREATE TABLE t_lko (id int PRIMARY KEY, txt text) USING pgcolumnar;
          GRANT SELECT, UPDATE ON t_lko TO pcopy_noins;" >/dev/null
check "parallel_copy into a primary-key table under the caller's SHARE lock gets the lock refusal (55000) before the unique-index refusal (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lko IN SHARE MODE" t_lko)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lko"'
check "parallel_copy gives a caller without INSERT that holds SHARE on the table 42501 before the lock refusal (#2183)" \
	"$(PGOPTIONS='-c role=pcopy_noins' self_lock_copy "LOCK TABLE t_lko IN SHARE MODE" t_lko)" \
	'42501|permission denied for table t_lko'
# The trigger refusal (#2187) keeps the same place as the unique-index one: after
# the caller's lock refusal, whose cause holds for every worker count, and after
# the unique-index refusal, which comes first of the two "workers => 1" refusals.
psql_run "DROP TABLE IF EXISTS t_lkt, t_tgpk;
          CREATE TABLE t_lkt (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_lkt_k AFTER INSERT ON t_lkt FOR EACH ROW EXECUTE FUNCTION t_tg_row();
          CREATE TABLE t_tgpk (id int PRIMARY KEY, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgpk_k AFTER INSERT ON t_tgpk FOR EACH ROW EXECUTE FUNCTION t_tg_row();" >/dev/null
check "parallel_copy into a table with an INSERT trigger under the caller's SHARE lock gets the lock refusal (55000) before the trigger refusal (#2187)" \
	"$(self_lock_copy "LOCK TABLE t_lkt IN SHARE MODE" t_lkt)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lkt"'
check "parallel_copy into a primary-key table with an INSERT trigger gets the unique-index refusal before the trigger refusal (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgpk'::regclass, '$F', 2)")" \
	"$(want_unique t_tgpk)"
# The rest of the conflict table, and the partition tree in depth (#2212 review).
# SHARE UPDATE EXCLUSIVE is stronger than ROW EXCLUSIVE by number but does not
# conflict with it, so the load runs; EXCLUSIVE and SHARE ROW EXCLUSIVE conflict,
# and each refusal names its mode. LOCK TABLE ONLY locks the parent alone, and
# t_lk2 has a partitioned partition t_lk2_a, so both the mid level and a
# second-level leaf must be checked.
psql_run "DROP TABLE IF EXISTS t_lk2 CASCADE;
          CREATE TABLE t_lk2 (id int, txt text) PARTITION BY RANGE (id);
          CREATE TABLE t_lk2_a PARTITION OF t_lk2 FOR VALUES FROM (MINVALUE) TO (2501) PARTITION BY RANGE (id);
          CREATE TABLE t_lk2_a1 PARTITION OF t_lk2_a FOR VALUES FROM (MINVALUE) TO (1251) USING pgcolumnar;
          CREATE TABLE t_lk2_a2 PARTITION OF t_lk2_a FOR VALUES FROM (1251) TO (2501) USING pgcolumnar;
          CREATE TABLE t_lk2_b PARTITION OF t_lk2 FOR VALUES FROM (2501) TO (MAXVALUE) USING pgcolumnar;" >/dev/null
check "parallel_copy after LOCK TABLE IN SHARE UPDATE EXCLUSIVE MODE in the same transaction still loads (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkc IN SHARE UPDATE EXCLUSIVE MODE" t_lkc)" "none|5000"
check "parallel_copy after LOCK TABLE IN EXCLUSIVE MODE in the same transaction is refused with 55000, naming ExclusiveLock (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkc IN EXCLUSIVE MODE" t_lkc)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds ExclusiveLock on "t_lkc"'
check "parallel_copy after LOCK TABLE IN SHARE ROW EXCLUSIVE MODE in the same transaction is refused with 55000, naming ShareRowExclusiveLock (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lkc IN SHARE ROW EXCLUSIVE MODE" t_lkc)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareRowExclusiveLock on "t_lkc"'
check "parallel_copy into a partitioned table after LOCK TABLE ONLY the parent IN SHARE MODE is refused with 55000, naming the parent (#2183)" \
	"$(self_lock_copy "LOCK TABLE ONLY t_lkp IN SHARE MODE" t_lkp)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lkp"'
check "parallel_copy into a two-level partitioned table after LOCK TABLE ONLY the mid level IN ACCESS EXCLUSIVE MODE is refused with 55000, naming it (#2183)" \
	"$(self_lock_copy "LOCK TABLE ONLY t_lk2_a IN ACCESS EXCLUSIVE MODE" t_lk2)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_lk2_a"'
check "parallel_copy into a two-level partitioned table after LOCK TABLE on a second-level leaf IN SHARE MODE is refused with 55000, naming it (#2183)" \
	"$(self_lock_copy "LOCK TABLE t_lk2_a1 IN SHARE MODE" t_lk2)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds ShareLock on "t_lk2_a1"'

# ---- #2211: the caller's own write to the target is refused, not waited on -----
# The first writer of a columnar storage takes a transaction-scoped advisory lock
# on that storage (PGCOLUMNAR_LOCKCLASS_STORAGE_ROW) on every flush and holds it to
# transaction end, whether or not the storage row is committed already. After an
# INSERT into the target in the caller's transaction, the coordinator (single
# table, which pre-creates the storage row) or the loader routing rows into the
# written partition waited on that lock, and the caller waited on them on a latch,
# so the load waited until statement_timeout (57014) with 1 worker or 2. It is now
# refused up front with 55000, naming the written relation. A write to another
# table, and no write, must still load. The refused, rolled-back loads leave no
# rows, no prepared transaction, and no committed storage row behind: the
# coordinator used to create and commit one once the caller's rollback let it.
psql_run "DROP TABLE IF EXISTS t_ownw, t_ownw1, t_ownw_other;
          CREATE TABLE t_ownw (id int, txt text) USING pgcolumnar;
          CREATE TABLE t_ownw1 (id int, txt text) USING pgcolumnar;
          INSERT INTO t_ownw1 VALUES (-1, 'committed');
          CREATE TABLE t_ownw_other (id int, txt text) USING pgcolumnar;" >/dev/null
mkpart t_ownp 4
check "premise: the table written in the #2211 checks has no storage row before them, and the table with one committed row has one" \
	"$(q "SELECT count(*) FILTER (WHERE relation_oid = 't_ownw'::regclass) || '/' || count(*) FILTER (WHERE relation_oid = 't_ownw1'::regclass) FROM pgcolumnar.storage")" "0/1"
check "parallel_copy after an INSERT into the same table in the same transaction is refused with 55000, not waited on (#2211)" \
	"$(self_lock_copy "INSERT INTO t_ownw VALUES (0, 'pre')" t_ownw)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownw"'
check "parallel_copy with workers => 1 after an INSERT into the same table in the same transaction is refused with 55000 (#2211)" \
	"$(self_lock_copy "INSERT INTO t_ownw VALUES (0, 'pre')" t_ownw 1)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownw"'
check "parallel_copy after an INSERT into a table that already has a committed row is refused with 55000 (#2211)" \
	"$(self_lock_copy "INSERT INTO t_ownw1 VALUES (0, 'pre')" t_ownw1)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownw1"'
check "parallel_copy into a partitioned table after an INSERT that reached one partition is refused with 55000, naming the partition (#2211)" \
	"$(self_lock_copy "INSERT INTO t_ownp VALUES (0, 'pre')" t_ownp)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownp_p0"'
check "parallel_copy refused after the transaction's own INSERT loads nothing and leaves no prepared transaction and no storage row (#2211)" \
	"$(q "SELECT (SELECT count(*) FROM t_ownw) || ' rows, ' || (SELECT count(*) FROM t_ownw1) || ' rows, ' || (SELECT count(*) FROM t_ownp) || ' partitioned rows, ' || (SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 't_ownw'::regclass) || ' storage rows, ' || (SELECT count(*) FROM pg_prepared_xacts) || ' prepared'")" \
	"0 rows, 1 rows, 0 partitioned rows, 0 storage rows, 0 prepared"
check "parallel_copy after an INSERT into another columnar table in the same transaction still loads (#2211)" \
	"$(self_lock_copy "INSERT INTO t_ownw_other VALUES (0, 'pre')" t_ownw)" "none|5000"
check "parallel_copy in a transaction that wrote nothing still loads (#2211)" \
	"$(self_lock_copy "SELECT 1" t_ownw1)" "none|5000"
check "parallel_copy into a partitioned table in a transaction that wrote nothing still loads (#2211)" \
	"$(self_lock_copy "SELECT 1" t_ownp)" "none|5000"
# Which partition, and in what order (#2214 review). The refusal must name the
# partition the write reached wherever it sits in the tree: the last of four (S4
# above writes the first), a later one written directly, and a leaf one level
# down in t_lk2. own_write_copy also returns the DETAIL, which names the same
# relation. The refusal sits after #2183's lock refusal and before #2080's
# unique-index refusal: a primary-key table with the caller's own INSERT and 2
# workers gets 55000, not 0A000, and a caller that took ACCESS EXCLUSIVE and then
# wrote gets #2183's message.
#
# own_write_copy SETUP TABLE -> "SQLSTATE|message|DETAIL" of the first ERROR, or
# what self_lock_copy returns when there is none.
own_write_copy() {
	local out
	out="$(timeout -s KILL 60 env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 \
		-c "SET statement_timeout = '5s'" -c '\set VERBOSITY verbose' \
		-c "BEGIN; $1; SELECT pgcolumnar.parallel_copy('$2'::regclass, '$F', 2); COMMIT;" 2>&1)"
	if grep -q '^ERROR:' <<<"$out"; then
		echo "$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' <<<"$out" | head -1)|$(sed -n 's/^DETAIL:  //p' <<<"$out" | head -1)"
	else
		self_lock_copy "$1" "$2"
	fi
}
psql_run "DROP TABLE IF EXISTS t_ownu;
          CREATE TABLE t_ownu (id int PRIMARY KEY, txt text) USING pgcolumnar;" >/dev/null
check "parallel_copy into a partitioned table after an INSERT that reached its last partition is refused with 55000, naming that partition in the message and the DETAIL (#2211)" \
	"$(own_write_copy "INSERT INTO t_ownp VALUES (4000, 'x')" t_ownp)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownp_p3"|The write holds the lock that serializes the first writers of "t_ownp_p3" until this transaction ends, and the background workers take that lock in their own transactions, so they would wait for this transaction to end.'
check "parallel_copy into a partitioned table after a direct INSERT into a later partition is refused with 55000, naming it (#2211)" \
	"$(self_lock_copy "INSERT INTO t_ownp_p2 VALUES (3000, 'x')" t_ownp)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownp_p2"'
check "parallel_copy into a two-level partitioned table after an INSERT that reached a second-level leaf is refused with 55000, naming the leaf (#2211)" \
	"$(self_lock_copy "INSERT INTO t_lk2 VALUES (2000, 'x')" t_lk2)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_lk2_a2"'
check "parallel_copy into a primary-key table after the caller's own INSERT with 2 workers gets the #2211 refusal (55000) before the unique-index refusal (#2211)" \
	"$(self_lock_copy "INSERT INTO t_ownu VALUES (0, 'pre')" t_ownu)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_ownu"'
check "parallel_copy after LOCK TABLE IN ACCESS EXCLUSIVE MODE and an INSERT into the same table gets the #2183 lock refusal first (#2211)" \
	"$(self_lock_copy "LOCK TABLE t_ownw IN ACCESS EXCLUSIVE MODE; INSERT INTO t_ownw VALUES (0, 'pre')" t_ownw)" \
	'55000|pgcolumnar.parallel_copy cannot run while this transaction holds AccessExclusiveLock on "t_ownw"'

# ---- #2161: 2 or more workers into a projection that holds no rows yet ---------
# A projection's flush records the projection's own storage row, and a projection
# that holds no rows yet has none. The coordinator pre-created only the TABLE's
# row, so the first loader to flush the projection created its row under the
# storage-row lock (class 102), and its PREPARE TRANSACTION kept that lock. The
# next loader to flush the projection waited for the prepared transaction, and the
# coordinator waited for that loader before it would COMMIT PREPARED. The deadlock
# detector cannot see a prepared transaction's side of the cycle, so the load ran
# until cancelled. The coordinator now pre-creates each projection's row too.
# Controls: 1 worker into the same shape, 2 workers into a projection that already
# holds rows, 2 workers into a table with no projection. Every table gets
# stripe_row_limit 1000, so the 5000 rows make several row groups and the
# projection's storage row must carry the table's limit, not the default.
# Every load runs under a 10s statement_timeout, so the old wait reads 57014.
#
# ep_load TABLE WORKERS PRELOAD PROJECTION(yes|no)
#   -> "returned|table rows|projection rows|projection = table" or "SQLSTATE|message"
ep_load() {
	local t="$1" out rc
	psql_run "CREATE TABLE $t (id int, txt text) USING pgcolumnar;
		SELECT pgcolumnar.set_options('$t', stripe_row_limit => 1000);" >/dev/null
	[ "$3" -gt 0 ] && psql_run "INSERT INTO $t SELECT g, 'pre'||g FROM generate_series(1, $3) g;" >/dev/null
	[ "$4" = yes ] && psql_run "SELECT pgcolumnar.add_projection('$t', 'p', columns => ARRAY['txt','id'], sort_key => ARRAY['id']);" >/dev/null
	out="$(timeout -s KILL 60 env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 \
		-c "SET statement_timeout = '10s'" -c '\set VERBOSITY verbose' \
		-c "SELECT pgcolumnar.parallel_copy('$t'::regclass, '$F', $2)" 2>&1)"
	rc=$?
	if grep -q '^ERROR:' <<<"$out"; then
		sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' <<<"$out" | head -1
		return
	elif [ "$rc" -ne 0 ]; then
		echo "no reading (psql exit $rc: $(head -1 <<<"$out"))"
		return
	fi
	if [ "$4" = yes ]; then
		printf '%s|%s|%s|%s\n' "$(tail -1 <<<"$out")" "$(q "SELECT count(*) FROM $t")" \
			"$(q "SELECT count(*) FROM pgcolumnar.read_projection('$t', 'p')")" \
			"$(q "SELECT CASE WHEN (SELECT md5(string_agg(x, E'\n' ORDER BY x)) FROM pgcolumnar.read_projection('$t', 'p') x)
			                     = (SELECT md5(string_agg(txt||'|'||id, E'\n' ORDER BY txt||'|'||id)) FROM $t)
			                  THEN 'projection = table' ELSE 'projection != table' END")"
	else
		printf '%s|%s|no projection\n' "$(tail -1 <<<"$out")" "$(q "SELECT count(*) FROM $t")"
	fi
}
# ep_storage TABLE -> the projection's storage row: whose it is, its row group
# limit, and whether its format and vector length are the 1-worker load's (t_ep1)
ep_storage() {
	q "WITH r AS (SELECT s.* FROM pgcolumnar.storage s JOIN pgcolumnar.projection p
	                ON p.proj_storage_id = s.storage_id
	              WHERE p.storage_id = pgcolumnar.get_storage_id('$1') AND p.name = 'p'),
	        w1 AS (SELECT s.* FROM pgcolumnar.storage s JOIN pgcolumnar.projection p
	                ON p.proj_storage_id = s.storage_id
	              WHERE p.storage_id = pgcolumnar.get_storage_id('t_ep1') AND p.name = 'p')
	   SELECT coalesce((SELECT CASE WHEN r.relation_oid = '$1'::regclass THEN 'own table'
	                               ELSE 'relation ' || r.relation_oid END
	                      || '|rgl ' || r.row_group_limit || '|'
	                      || CASE WHEN (r.format_version, r.vector_length)
	                                   = (SELECT format_version, vector_length FROM w1)
	                              THEN 'format as 1 worker' ELSE 'format differs' END
	                    FROM r), 'no storage row')"
}
check "parallel_copy with 1 worker into a table whose projection holds no rows yet loads every row and fills the projection (#2161)" \
	"$(ep_load t_ep1 1 0 yes)" "5000|5000|5000|projection = table"
check "parallel_copy with 2 workers into a table whose projection holds no rows yet loads every row and fills the projection, not 57014 (#2161)" \
	"$(ep_load t_ep2 2 0 yes)" "5000|5000|5000|projection = table"
check "parallel_copy with 4 workers into a table whose projection holds no rows yet loads every row and fills the projection, not 57014 (#2161)" \
	"$(ep_load t_ep4 4 0 yes)" "5000|5000|5000|projection = table"
check "parallel_copy with 2 workers into a table whose projection already holds rows loads every row and fills the projection (#2161)" \
	"$(ep_load t_ep2p 2 1000 yes)" "5000|6000|6000|projection = table"
check "parallel_copy with 2 workers into a table with no projection loads every row (#2161)" \
	"$(ep_load t_ep2n 2 0 no)" "5000|5000|no projection"
check "parallel_copy with 2 and 4 workers records the empty projection's storage row as a 1-worker load does (#2161)" \
	"$(ep_storage t_ep2) ; $(ep_storage t_ep4)" \
	"own table|rgl 1000|format as 1 worker ; own table|rgl 1000|format as 1 worker"
# More than one empty projection: the coordinator must create EVERY projection's
# storage row, not the first one's (#2213 review: a `break;` after the first
# insert passed every check above, each of which has one projection).
psql_run "CREATE TABLE t_ep3 (id int, txt text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('t_ep3', stripe_row_limit => 1000);
	SELECT pgcolumnar.add_projection('t_ep3', 'pa', columns => ARRAY['txt','id'], sort_key => ARRAY['id']);
	SELECT pgcolumnar.add_projection('t_ep3', 'pb', columns => ARRAY['id'], sort_key => ARRAY['id']);
	SELECT pgcolumnar.add_projection('t_ep3', 'pc', columns => ARRAY['txt'], sort_key => ARRAY['txt']);" >/dev/null
ep3_load="$(PGOPTIONS='-c statement_timeout=10s' q "SELECT pgcolumnar.parallel_copy('t_ep3'::regclass, '$F', 2)" 2>&1 | tail -1)"
ep3_rows() {	# -> "pa N/own pb N/own pc N/own": each projection's rows and whose storage row it has
	q "SELECT string_agg(p.name || ' ' || (SELECT count(*) FROM pgcolumnar.read_projection('t_ep3', p.name))
	                     || '/' || coalesce((SELECT CASE WHEN s.relation_oid = 't_ep3'::regclass THEN 'own' ELSE 'other' END
	                                           FROM pgcolumnar.storage s WHERE s.storage_id = p.proj_storage_id), 'none'),
	                     ' ' ORDER BY p.name)
	     FROM pgcolumnar.projection p
	    WHERE p.storage_id = pgcolumnar.get_storage_id('t_ep3') AND p.projection_id <> 0"
}
check "parallel_copy with 2 workers into a table with three empty projections fills every projection and records each one's storage row (#2161)" \
	"$ep3_load|$(q "SELECT count(*) FROM t_ep3")|$(ep3_rows)" "5000|5000|pa 5000/own pb 5000/own pc 5000/own"

# A projection that commits after the coordinator's pre-create and before the
# loaders list projections has no storage row (#2213 review). add_projection
# releases its table lock before it commits (#2215), so it can commit during a
# load. A loader would then create the row under the storage-row lock, keep it
# through PREPARE TRANSACTION, and the next loader would wait on it until
# statement_timeout, which is #2161 again. A loader now fails on such a row with
# 55000, and the coordinator rolls every range back. The race is forced, not
# timed: a CHECK constraint holds both loaders on an advisory lock until
# add_projection's transaction has committed, and only then lets them insert,
# which is when a loader lists the projections. COPY checks the constraint
# before it hands the row to the table. (A BEFORE INSERT trigger did this until
# #2187 refused a table with an INSERT trigger with more than one worker.)
psql_run "CREATE TABLE epr_go (step int);
	CREATE FUNCTION epr_gate(int) RETURNS boolean LANGUAGE plpgsql
	  AS \$\$ BEGIN PERFORM pg_advisory_xact_lock_shared(2161); RETURN true; END \$\$;
	CREATE TABLE t_epr (id int CONSTRAINT epr_gate CHECK (epr_gate(id)), txt text) USING pgcolumnar;" >/dev/null
epr_psql() { timeout -s KILL 60 env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA "$@"; }
epr_psql -c "SELECT pg_advisory_lock(2161); SET application_name = 'epr_gate';
	DO \$\$ BEGIN WHILE NOT EXISTS (SELECT 1 FROM epr_go WHERE step >= 2) LOOP PERFORM pg_sleep(0.05); END LOOP; END \$\$;
	SELECT pg_advisory_unlock(2161);" >/dev/null 2>&1 &
epr_bg_gate=$!
epr_psql -c "BEGIN" -c "SELECT pgcolumnar.add_projection('t_epr', 'p', columns => ARRAY['txt','id'], sort_key => ARRAY['id'])" \
	-c "SET application_name = 'epr_ddl'" \
	-c "DO \$\$ BEGIN WHILE NOT EXISTS (SELECT 1 FROM epr_go WHERE step >= 1) LOOP PERFORM pg_sleep(0.05); END LOOP; END \$\$" \
	-c "COMMIT" >/dev/null 2>&1 &
epr_bg_ddl=$!
epr_named() { local i; for i in $(seq 1 200); do [ "$(q "SELECT count(*) FROM pg_stat_activity WHERE $1")" = "$2" ] && { echo yes; return; }; sleep 0.05; done; echo no; }
epr_ready="gate $(epr_named "application_name = 'epr_gate'" 1) ddl $(epr_named "application_name = 'epr_ddl'" 1)"
epr_psql -v ON_ERROR_STOP=1 -c "SET statement_timeout = '10s'" -c '\set VERBOSITY verbose' \
	-c "SELECT pgcolumnar.parallel_copy('t_epr'::regclass, '$F', 2)" >"$PGC_WORKDIR/epr_load.out" 2>&1 &
epr_bg_load=$!
epr_held="$(epr_named "backend_type = 'pgcolumnar parallel_copy loader' AND wait_event = 'advisory'" 2)"
psql_run "INSERT INTO epr_go VALUES (1);" >/dev/null	# add_projection commits
epr_committed="$(epr_named "application_name = 'epr_ddl'" 0)"
psql_run "INSERT INTO epr_go VALUES (2);" >/dev/null	# the loaders go on
wait "$epr_bg_load"; wait "$epr_bg_ddl"; wait "$epr_bg_gate"
check "premise: both loaders waited at the gate while add_projection committed" \
	"$epr_ready | held $epr_held | committed $epr_committed" "gate yes ddl yes | held yes | committed yes"
epr_got="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' "$PGC_WORKDIR/epr_load.out" | head -1 \
	| sed -E 's/worker [0-9]+ failed/worker N failed/; s/storage [0-9]+ of/storage N of/')"
check "parallel_copy fails with 55000, not 57014, when a projection committed during the load reaches the loaders, and loads nothing (#2161)" \
	"${epr_got:-no error: $(head -1 "$PGC_WORKDIR/epr_load.out")} | rows $(q "SELECT count(*) FROM t_epr") | prepared $(q "SELECT count(*) FROM pg_prepared_xacts")" \
	'55000|pgcolumnar.parallel_copy worker N failed: storage N of "t_epr" has no storage row that was committed before the load started; a projection added during the load cannot be loaded in parallel, so run the load again | rows 0 | prepared 0'
# The loader refuses an absent storage row of its TARGET only. A trigger may write
# another columnar table, whose row the coordinator never pre-created; with one
# worker that load must still go through. (Two or more workers into such a
# trigger are refused before the load starts, by #2187's trigger refusal.)
psql_run "CREATE TABLE t_ept (id int, txt text) USING pgcolumnar;
	CREATE TABLE t_ept_side (id int, txt text) USING pgcolumnar;
	CREATE FUNCTION t_ept_copy() RETURNS trigger LANGUAGE plpgsql
	  AS \$\$ BEGIN INSERT INTO t_ept_side VALUES (NEW.id, NEW.txt); RETURN NEW; END \$\$;
	CREATE TRIGGER t_ept_copy AFTER INSERT ON t_ept FOR EACH ROW EXECUTE FUNCTION t_ept_copy();" >/dev/null
ept_got="$(epr_psql -v ON_ERROR_STOP=1 -c "SET statement_timeout = '10s'" -c '\set VERBOSITY verbose' \
	-c "SELECT pgcolumnar.parallel_copy('t_ept'::regclass, '$F', 1)" 2>&1)"
ept_got="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' <<<"$ept_got" | head -1 | grep . || tail -1 <<<"$ept_got")"
check "parallel_copy with 1 worker into a table whose trigger writes another empty columnar table still loads (#2161)" \
	"$ept_got|$(q "SELECT count(*) FROM t_ept")|$(q "SELECT count(*) FROM t_ept_side")" "5000|5000|5000"
check "parallel_copy into an empty projection leaves no prepared transaction and keeps the server up (#2161)" \
	"leak $(q "SELECT count(*) FROM pg_prepared_xacts") | survived $(survived "$CRASHES_BEFORE")" \
	"leak 0 | survived 1"

# The trigger refusal (#2187) comes after this one too: the caller's own INSERT
# into a table with an INSERT trigger, then 2 workers, gets 55000. The caller's
# INSERT runs the trigger itself, which writes to t_tgk.
psql_run "DROP TABLE IF EXISTS t_owntg;
          CREATE TABLE t_owntg (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_owntg_k AFTER INSERT ON t_owntg FOR EACH ROW EXECUTE FUNCTION t_tg_row();" >/dev/null
check "parallel_copy into a table with an INSERT trigger after the caller's own INSERT with 2 workers gets the #2211 refusal (55000) before the trigger refusal (#2187)" \
	"$(self_lock_copy "INSERT INTO t_owntg VALUES (-7, 'pre')" t_owntg)" \
	'55000|pgcolumnar.parallel_copy cannot run after this transaction has written to "t_owntg"'

# ---- #2187, the review of #2219 ------------------------------------------------
# 1. The caller waits for the coordinator to exit after a cancel. With dedup =>
#    true the coordinator reads pgcolumnar.load_fingerprint, and a lock the
#    caller holds there kept it waiting with no end: the caller returned only on
#    a second cancel, and the rows committed after the caller's ROLLBACK. Its
#    SIGTERM now raises a cancel, so the wait ends and the load rolls back. A
#    watchdog cancels the caller again at 12 s, so the old hang reads as 57014
#    "user request" after 12 s rather than hanging the suite.
psql_run "DROP TABLE IF EXISTS t_ddl; CREATE TABLE t_ddl (id int, txt text) USING pgcolumnar;" >/dev/null
dedup_lock_probe() {	# -> "error | time | rows | prepared"
	local t0 t1 ms out wd
	( sleep 12; q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name = 'pcopy_ddl_probe'" >/dev/null ) &
	wd=$!
	t0=$(date +%s%N)
	out="$(timeout -s KILL 60 env PGAPPNAME=pcopy_ddl_probe PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -X -At -v VERBOSITY=verbose -c "BEGIN" \
		-c "LOCK pgcolumnar.load_fingerprint IN ACCESS EXCLUSIVE MODE" -c "SET statement_timeout = '4s'" \
		-c "SELECT pgcolumnar.parallel_copy('t_ddl'::regclass, '$F', 2, dedup => true)" -c "ROLLBACK" 2>&1)"
	t1=$(date +%s%N)
	kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
	for _ in $(seq 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type LIKE 'pgcolumnar parallel_copy%'")" = 0 ] && break
		sleep 0.1
	done
	ms=$(( (t1 - t0) / 1000000 ))
	printf '%s | %s | %s rows | %s prepared\n' \
		"$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' <<<"$out")" \
		"$([ "$ms" -lt 10000 ] && echo 'under 10 s' || echo "took $ms ms")" \
		"$(q "SELECT count(*) FROM t_ddl")" "$(q "SELECT count(*) FROM pg_prepared_xacts")"
}
check "parallel_copy with dedup under the caller's ACCESS EXCLUSIVE lock on pgcolumnar.load_fingerprint returns its statement_timeout within 10 s, loads nothing and leaves no prepared transaction (#2187)" \
	"$(dedup_lock_probe)" "57014 canceling statement due to statement timeout | under 10 s | 0 rows | 0 prepared"
# The same bound when the coordinator waits on the target before it starts any
# loader: another session holds SHARE on it for 8 s, and the caller's 2 s
# statement_timeout must end the call near 2 s.
psql_run "DROP TABLE IF EXISTS t_shw; CREATE TABLE t_shw (id int, txt text) USING pgcolumnar;" >/dev/null
share_wait_probe() {	# -> "error | time | prepared"
	local hp t0 t1 ms out
	pcopy_psql -c "BEGIN" -c "LOCK TABLE t_shw IN SHARE MODE" -c "SELECT pg_sleep(8)" -c "COMMIT" >/dev/null 2>&1 &
	hp=$!
	for _ in $(seq 100); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = 't_shw'::regclass AND mode = 'ShareLock' AND granted")" = 1 ] && break
		sleep 0.05
	done
	t0=$(date +%s%N)
	out="$(PGOPTIONS='-c statement_timeout=2s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_shw'::regclass, '$F', 2)")"
	t1=$(date +%s%N)
	ms=$(( (t1 - t0) / 1000000 ))
	wait "$hp"
	printf '%s | %s | %s prepared\n' "${out%% | *}" "$([ "$ms" -lt 5000 ] && echo 'under 5 s' || echo "took $ms ms")" \
		"$(q "SELECT count(*) FROM pg_prepared_xacts")"
}
check "parallel_copy whose coordinator waits on another session's SHARE lock returns its 2 s statement_timeout within 5 s, not when the lock is released (#2187)" \
	"$(share_wait_probe)" "57014 canceling statement due to statement timeout | under 5 s | 0 prepared"

# 2. What the earlier checks did not read. The loaders' DETAIL reaches the caller.
#    An ENABLE ALWAYS trigger fires in the loaders and is refused; an ENABLE
#    REPLICA trigger never fires there, so 2 workers still load and the trigger
#    writes nothing. The hold setting is superuser-only.
psql_run "DROP TABLE IF EXISTS t_tgl2, t_tgalw, t_tgrep;
          CREATE TABLE t_tgl2 (id int, txt text) USING pgcolumnar;
          CREATE TABLE t_tgalw (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgalw_k AFTER INSERT ON t_tgalw FOR EACH ROW EXECUTE FUNCTION t_tg_row();
          ALTER TABLE t_tgalw ENABLE ALWAYS TRIGGER t_tgalw_k;
          CREATE TABLE t_tgrep (id int, txt text) USING pgcolumnar;
          CREATE TRIGGER t_tgrep_k AFTER INSERT ON t_tgrep FOR EACH ROW EXECUTE FUNCTION t_tg_row();
          ALTER TABLE t_tgrep ENABLE REPLICA TRIGGER t_tgrep_k;" >/dev/null
check "the loaders' refusal of an INSERT trigger created after the caller's check carries its DETAIL to the caller (#2187)" \
	"$(LATE_READER=remote_detail late_ddl t_tgl2 2 "CREATE TRIGGER t_tgl2_k AFTER INSERT ON t_tgl2 FOR EACH ROW EXECUTE FUNCTION t_tg_row();")" \
	"0A000 | Each worker runs the trigger in its own prepared transaction, so writes the trigger makes can wait on another worker until the load is cancelled."
check "parallel_copy refuses a single table with an ENABLE ALWAYS INSERT trigger and 2 workers with 0A000 (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=10s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgalw'::regclass, '$F', 2)")" \
	"$(want_trigger t_tgalw t_tgalw_k)"
tgk_before="$(q "SELECT count(*) FROM t_tgk")"
check "control: parallel_copy with 2 workers still loads a single table whose INSERT trigger is ENABLE REPLICA, and the trigger does not fire (#2187)" \
	"$(PGOPTIONS='-c statement_timeout=30s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_tgrep'::regclass, '$F', 2)") | $(q "SELECT count(*) FROM t_tgrep") | trigger rows added $(( $(q "SELECT count(*) FROM t_tgk") - tgk_before ))" \
	"no error (5000) | 5000 | trigger rows added 0"
check "a role without superuser cannot set pgcolumnar.parallel_copy_open_hold_ms (#2187)" \
	"$(PGOPTIONS='-c role=pcopy_noins' remote_refusal "SET pgcolumnar.parallel_copy_open_hold_ms = 1")" \
	'42501 permission denied to set parameter "pgcolumnar.parallel_copy_open_hold_ms" | no hint'

# 3. One loader lost must not stop the wait for the others. With 3 workers held,
#    the second loader is terminated before it opens the table, then CREATE INDEX
#    queues: the first loader must still wait for the third, so the load fails
#    promptly with the lost loader's error instead of hanging behind the index.
psql_run "DROP TABLE IF EXISTS t_lost3; CREATE TABLE t_lost3 (id int, txt text) USING pgcolumnar;" >/dev/null
lost_one_of_three() {	# -> "load | ddl | rows | leak"
	local lp dp victim=""
	pcopy_hold 4000 >/dev/null
	( PGOPTIONS='-c statement_timeout=15s' remote_refusal \
		"SELECT pgcolumnar.parallel_copy('t_lost3'::regclass, '$F', 3)" >"$DATADIR/t_lost3.load" ) &
	lp=$!
	for _ in $(seq 100); do
		case "$(loaders_on t_lost3)" in "1 held"*)
			[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE $LOADER")" = 3 ] &&
				victim="$(q "SELECT pid FROM pg_stat_activity WHERE $LOADER AND pid NOT IN
					(SELECT pid FROM pg_locks WHERE relation = 't_lost3'::regclass AND pid IS NOT NULL) ORDER BY pid LIMIT 1")"
			[ -n "$victim" ] && break ;;
		esac
		sleep 0.05
	done
	q "SELECT pg_terminate_backend($victim)" >/dev/null
	( PGOPTIONS='-c statement_timeout=15s' remote_refusal "CREATE INDEX t_lost3_id ON t_lost3 (id)" >"$DATADIR/t_lost3.ddl" ) &
	dp=$!
	wait "$lp"; wait "$dp"
	pcopy_hold 0 >/dev/null
	printf '%s | %s | %s | leak %s\n' "$(sed 's/worker [0-9]* failed/worker N failed/' "$DATADIR/t_lost3.load")" \
		"$(cat "$DATADIR/t_lost3.ddl")" "$(q "SELECT count(*) FROM t_lost3")" "$(q "SELECT count(*) FROM pg_prepared_xacts")"
}
check "with 3 workers, one loader lost before it opens the table and a CREATE INDEX queued after the first, the load fails promptly instead of hanging (#2187)" \
	"$(lost_one_of_three)" \
	"XX000 pgcolumnar.parallel_copy worker N failed: loader exited without reporting a result | no hint | no error (CREATE INDEX) | 0 | leak 0"

# 4. A cancel after the rows commit must not stop the fingerprint record (the
#    second review of #2219). Another session holds EXCLUSIVE on
#    pgcolumnar.load_fingerprint for 8 s: the check's ACCESS SHARE passes, the
#    rows commit, and the record waits. The caller's 3 s statement_timeout then
#    cancels the call. The record must still happen, so a dedup retry of the
#    same file is refused with the skip NOTICE and the table holds the file once.
psql_run "DROP TABLE IF EXISTS t_rec; CREATE TABLE t_rec (id int, txt text) USING pgcolumnar;" >/dev/null
dedup_retry_probe() {	# -> "first error | retry | rows | prepared"
	local hp first retry
	pcopy_psql -c "BEGIN" -c "LOCK pgcolumnar.load_fingerprint IN EXCLUSIVE MODE" -c "SELECT pg_sleep(8)" -c "COMMIT" >/dev/null 2>&1 &
	hp=$!
	for _ in $(seq 100); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = 'pgcolumnar.load_fingerprint'::regclass AND mode = 'ExclusiveLock' AND granted")" = 1 ] && break
		sleep 0.05
	done
	first="$(PGOPTIONS='-c statement_timeout=3s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_rec'::regclass, '$F', 2, dedup => true)")"
	wait "$hp"
	for _ in $(seq 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type LIKE 'pgcolumnar parallel_copy%'")" = 0 ] && break
		sleep 0.1
	done
	retry="$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c statement_timeout=30s' psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -X -At -c "SELECT pgcolumnar.parallel_copy('t_rec'::regclass, '$F', 2, dedup => true)" 2>&1)"
	printf 'first %s | retry %s %s | %s rows | %s prepared\n' "${first%% *}" "$(grep -v '^NOTICE\|^DETAIL\|^HINT' <<<"$retry" | tail -1)" \
		"$(grep -q 'skipped a load already applied to "t_rec"' <<<"$retry" && echo 'with the skip NOTICE' || echo 'without the skip NOTICE')" \
		"$(q "SELECT count(*) FROM t_rec")" "$(q "SELECT count(*) FROM pg_prepared_xacts")"
}
check "a dedup load cancelled after its rows commit still records the fingerprint: a retry of the same file returns 0 with the skip NOTICE and the table holds the file once (#2187)" \
	"$(dedup_retry_probe)" "first 57014 | retry 0 with the skip NOTICE | 5000 rows | 0 prepared"
# The third review of #2219. The record has no deadline: a cancelled caller stops
# waiting once the rows commit, and the coordinator records when the lock frees.
# B: the caller itself holds SHARE on pgcolumnar.load_fingerprint, which the
#    record's ROW EXCLUSIVE waits for. The caller must return near its 3 s
#    timeout, and once its ROLLBACK frees the lock the fingerprint is recorded.
#    A watchdog cancels the caller again at 25 s, so a caller that kept waiting
#    reads as a late return rather than hanging the suite.
psql_run "DROP TABLE IF EXISTS t_rb, t_ra; CREATE TABLE t_rb (id int, txt text) USING pgcolumnar;
          CREATE TABLE t_ra (id int, txt text) USING pgcolumnar;" >/dev/null
fp_rows() {	# TABLE -> fingerprint rows recorded for it
	q "SELECT count(*) FROM pgcolumnar.load_fingerprint WHERE relation_oid = '$1'::regclass"
}
no_coordinator() {
	for _ in $(seq 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type LIKE 'pgcolumnar parallel_copy%'")" = 0 ] && break
		sleep 0.1
	done
}
caller_lock_record_probe() {	# -> "error | time | rows | fingerprints | prepared"
	local t0 t1 ms out wd
	( sleep 25; q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name = 'pcopy_rb_probe'" >/dev/null ) &
	wd=$!
	t0=$(date +%s%N)
	out="$(timeout -s KILL 60 env PGAPPNAME=pcopy_rb_probe PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -X -At -v VERBOSITY=verbose -c "BEGIN" \
		-c "LOCK pgcolumnar.load_fingerprint IN SHARE MODE" -c "SET statement_timeout = '3s'" \
		-c "SELECT pgcolumnar.parallel_copy('t_rb'::regclass, '$F', 2, dedup => true)" -c "ROLLBACK" 2>&1)"
	t1=$(date +%s%N)
	kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
	no_coordinator
	ms=$(( (t1 - t0) / 1000000 ))
	printf '%s | %s | %s rows | %s fingerprint | %s prepared\n' \
		"$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' <<<"$out")" \
		"$([ "$ms" -lt 8000 ] && echo 'under 8 s' || echo "took $ms ms")" \
		"$(q "SELECT count(*) FROM t_rb")" "$(fp_rows t_rb)" "$(q "SELECT count(*) FROM pg_prepared_xacts")"
}
check "a dedup load whose caller holds SHARE on pgcolumnar.load_fingerprint returns its statement_timeout within 8 s and records the fingerprint once the caller's ROLLBACK frees the lock (#2187)" \
	"$(caller_lock_record_probe)" \
	"57014 canceling statement due to statement timeout | under 8 s | 5000 rows | 1 fingerprint | 0 prepared"
# A: another session holds EXCLUSIVE on it for 16 s, longer than the caller's
#    2 s timeout plus any grace a record might allow itself. The caller returns
#    near 2 s, the fingerprint is recorded when the session lets go, and a dedup
#    retry is refused.
other_lock_record_probe() {	# -> "error | time | rows | fingerprints | retry"
	local hp t0 t1 ms first retry
	pcopy_psql -c "BEGIN" -c "LOCK pgcolumnar.load_fingerprint IN EXCLUSIVE MODE" -c "SELECT pg_sleep(16)" -c "COMMIT" >/dev/null 2>&1 &
	hp=$!
	for _ in $(seq 100); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = 'pgcolumnar.load_fingerprint'::regclass AND mode = 'ExclusiveLock' AND granted")" = 1 ] && break
		sleep 0.05
	done
	t0=$(date +%s%N)
	first="$(PGOPTIONS='-c statement_timeout=2s' remote_refusal "SELECT pgcolumnar.parallel_copy('t_ra'::regclass, '$F', 2, dedup => true)")"
	t1=$(date +%s%N)
	wait "$hp"
	no_coordinator
	ms=$(( (t1 - t0) / 1000000 ))
	retry="$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c statement_timeout=30s' psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -X -At -c "SELECT pgcolumnar.parallel_copy('t_ra'::regclass, '$F', 2, dedup => true)" 2>&1)"
	printf '%s | %s | %s fingerprint | retry %s | %s rows\n' "${first%% | *}" \
		"$([ "$ms" -lt 6000 ] && echo 'under 6 s' || echo "took $ms ms")" "$(fp_rows t_ra)" \
		"$(grep -q 'skipped a load already applied to "t_ra"' <<<"$retry" && echo 'skipped' || echo "not skipped ($(tail -1 <<<"$retry"))")" \
		"$(q "SELECT count(*) FROM t_ra")"
}
check "a dedup load cancelled while another session holds load_fingerprint for 16 s returns within 6 s, records the fingerprint when the lock frees, and its retry is skipped (#2187)" \
	"$(other_lock_record_probe)" \
	"57014 canceling statement due to statement timeout | under 6 s | 1 fingerprint | retry skipped | 5000 rows"

# ---- #2189: every range keeps the file's newline style -----------------------
# COPY FROM takes the newline style (LF or CRLF) from the file's first line and
# refuses any later line ending that differs with 22P04. Each loader ran core
# COPY over its own byte range, so a range that started where the style changed
# took the new style from its own first line, and the load went through:
#   mark<W>.txt: an LF file with \.\r\n at the start of a range ("end-of-copy
#     marker does not match previous newline style");
#   lfcr<W>.txt / crlf<W>.txt: LF lines, then CRLF lines (and the reverse) from
#     the start of a range ("literal carriage return found in data" / "literal
#     newline found in data").
# W is the worker count, 2, 3 or 4; each file puts its event on a range start of
# both the single-table split and the partitioned one, at the range the check
# names. The marker cannot start a partitioned range: the partitioned splitter
# ends the last range at the marker line, so that loader has seen the lines
# before it, and those checks passed before the fix too.
# The refusal must be heap COPY's on the same file: its SQLSTATE and message,
# after parallel_copy's "worker N failed: ", with 0 rows and no prepared
# transaction. The controls load as heap COPY does: a consistent LF file, a
# consistent CRLF file, \.\r\n at a range start in a CRLF file, \.\n in the middle
# of a range, and a CRLF file whose first record holds an escaped LF, which is
# data and not the first line's ending, and ends in an escaped backslash (an even
# run of 2, so the CR after it is the line's ending).
nl_gen() {	# KIND N S PATH: rows 1..N of "%04d\tx", the KIND's event at row S
	awk -v kind="$1" -v n="$2" -v s="$3" 'BEGIN {
		for (k = 1; k <= n; k++) {
			if (k == s && (kind == "mark" || kind == "crmark")) printf "\\.\r\n"
			if (k == s && kind == "midmark") printf "\\.\n"
			eol = "\n"
			if ((kind == "lfcr" && k >= s) || (kind == "crlf" && k < s) || kind == "crmark" || kind == "crall" || kind == "esc") eol = "\r\n"
			if (kind == "esc" && k == 1) printf "0001\ta\\\nb\\\\\r\n"
			else printf "%04d\tx%s", k, eol
		}
	}' >"$4"
	chmod 644 "$4"
}
nl_target() {	# single|part [COLS] -> (re)creates t_nl as that target, empty; COLS defaults to "id int, txt text"
	local ddl i lo hi cols="${2:-id int, txt text}"
	if [ "$1" = single ]; then
		psql_run "DROP TABLE IF EXISTS t_nl CASCADE; CREATE TABLE t_nl ($cols) USING pgcolumnar;" >/dev/null
		return
	fi
	ddl="DROP TABLE IF EXISTS t_nl CASCADE; CREATE TABLE t_nl ($cols) PARTITION BY RANGE (id);"
	for i in 0 1 2 3 4 5 6 7; do
		if [ "$i" = 0 ]; then lo=MINVALUE; else lo=$((i * 500 + 1)); fi
		if [ "$i" = 7 ]; then hi=MAXVALUE; else hi=$(((i + 1) * 500 + 1)); fi
		ddl="$ddl CREATE TABLE t_nl_p$i PARTITION OF t_nl FOR VALUES FROM ($lo) TO ($hi) USING pgcolumnar;"
	done
	psql_run "$ddl" >/dev/null
}
nl_outcome() {	# SQL TABLE -> "SQLSTATE|message|N rows|P prepared" when SQL failed, else "loaded|N rows|P prepared"
	local out head
	out="$(timeout -s KILL 120 env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -qtA -c "SET statement_timeout = '60s'" -c '\set VERBOSITY verbose' -c "$1" 2>&1)"
	if grep -q '^ERROR:' <<<"$out"; then
		head="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' <<<"$out" | head -1)"
	else
		head="loaded"
	fi
	echo "${head:-unparsed ($out)}|$(q "SELECT count(*) FROM $2") rows|$(q "SELECT count(*) FROM pg_prepared_xacts") prepared"
}
nl_heap() {	# PATH -> nl_outcome of heap COPY of PATH into a fresh h_nl
	psql_run "DROP TABLE IF EXISTS h_nl; CREATE TABLE h_nl (id int, txt text);" >/dev/null
	nl_outcome "COPY h_nl FROM '$1'" h_nl
}
nl_at() {	# PATH WORKERS BYTE -> the index of BYTE among the file's split offsets, or "none (offsets)"
	local arr i
	read -r -a arr <<<"$(offsets_of "$1" "$2")"
	for i in $(seq 1 $(($2 - 1))); do
		[ "${arr[$i]:-x}" = "$3" ] && { echo "$i"; return; }
	done
	echo "none (${arr[*]})"
}
nl_refused() {	# PATH TARGET WORKERS RANGE -> "ok", or what the load gave against what heap COPY gives
	local heap got want
	heap="$(nl_heap "$1")"
	nl_target "$2"
	got="$(nl_outcome "SELECT pgcolumnar.parallel_copy('t_nl'::regclass, '$1', $3)" t_nl)"
	want="${heap%%|*}|pgcolumnar.parallel_copy worker $4 failed: ${heap#*|}"
	if [ "$got" = "$want" ]; then echo ok; else echo "got [$got] want [$want]"; fi
}
nl_loads() {	# PATH TARGET -> "W:ok" per worker count 2, 3, 4, or what it gave against heap COPY
	local w heap hh got gh res=""
	heap="$(nl_heap "$1")"; hh="$(pgc_set_hash "SELECT * FROM h_nl")"
	for w in 2 3 4; do
		nl_target "$2"
		got="$(nl_outcome "SELECT pgcolumnar.parallel_copy('t_nl'::regclass, '$1', $w)" t_nl)"
		gh="$(pgc_set_hash "SELECT * FROM t_nl")"
		if [ "$got" = "$heap" ] && [ "$gh" = "$hh" ]; then res="$res $w:ok"
		else res="$res $w:[$got $gh] want [$heap $hh]"; fi
	done
	echo "${res# }"
}
NL="$DATADIR/nl"; mkdir -p "$NL"; chmod 755 "$NL"
nl_gen mark 3998 2001 "$NL/mark2.txt"; nl_gen mark 3748 2501 "$NL/mark3.txt"; nl_gen mark 3332 2501 "$NL/mark4.txt"
nl_gen lfcr 3749 2001 "$NL/lfcr2.txt"; nl_gen lfcr 3593 2501 "$NL/lfcr3.txt"; nl_gen lfcr 3228 2501 "$NL/lfcr4.txt"
nl_gen crlf 3212 1501 "$NL/crlf2.txt"; nl_gen crlf 3142 2001 "$NL/crlf3.txt"; nl_gen crlf 3451 2501 "$NL/crlf4.txt"
nl_gen crmark 3998 2001 "$NL/crmark2.txt"; nl_gen crmark 3748 2501 "$NL/crmark3.txt"; nl_gen crmark 3332 2501 "$NL/crmark4.txt"
nl_gen lf 4000 0 "$NL/lf.txt"; nl_gen crall 4000 0 "$NL/crall.txt"
nl_gen midmark 4000 1234 "$NL/midmark.txt"; nl_gen esc 4000 0 "$NL/esc.txt"
[ "$(id -u)" = "0" ] && chown -R postgres "$NL"

_M='22P04|end-of-copy marker does not match previous newline style|0 rows|0 prepared'
_CR='22P04|literal carriage return found in data|0 rows|0 prepared'
_NLM='22P04|literal newline found in data|0 rows|0 prepared'
check "#2189 premise: heap COPY refuses mark2, mark3 and mark4 with 22P04 'end-of-copy marker does not match previous newline style', and the marker line is split offset 1 of 2, 2 of 3 and 3 of 4" \
	"$(nl_heap "$NL/mark2.txt") @$(nl_at "$NL/mark2.txt" 2 14000) ; $(nl_heap "$NL/mark3.txt") @$(nl_at "$NL/mark3.txt" 3 17500) ; $(nl_heap "$NL/mark4.txt") @$(nl_at "$NL/mark4.txt" 4 17500)" \
	"$_M @1 ; $_M @2 ; $_M @3"
check "#2189 premise: heap COPY refuses lfcr2, lfcr3 and lfcr4 with 22P04 'literal carriage return found in data', and the first CRLF line is split offset 1 of 2, 2 of 3 and 3 of 4" \
	"$(nl_heap "$NL/lfcr2.txt") @$(nl_at "$NL/lfcr2.txt" 2 14000) ; $(nl_heap "$NL/lfcr3.txt") @$(nl_at "$NL/lfcr3.txt" 3 17500) ; $(nl_heap "$NL/lfcr4.txt") @$(nl_at "$NL/lfcr4.txt" 4 17500)" \
	"$_CR @1 ; $_CR @2 ; $_CR @3"
check "#2189 premise: heap COPY refuses crlf2, crlf3 and crlf4 with 22P04 'literal newline found in data', and the first LF line is split offset 1 of 2, 2 of 3 and 3 of 4" \
	"$(nl_heap "$NL/crlf2.txt") @$(nl_at "$NL/crlf2.txt" 2 12000) ; $(nl_heap "$NL/crlf3.txt") @$(nl_at "$NL/crlf3.txt" 3 16000) ; $(nl_heap "$NL/crlf4.txt") @$(nl_at "$NL/crlf4.txt" 4 20000)" \
	"$_NLM @1 ; $_NLM @2 ; $_NLM @3"
check "#2189 premise: the \.\r\n line of crmark2, crmark3 and crmark4 is split offset 1 of 2, 2 of 3 and 3 of 4, and midmark's \.\n line is no split offset for 2, 3 or 4 workers" \
	"$(nl_at "$NL/crmark2.txt" 2 16000) $(nl_at "$NL/crmark3.txt" 3 20000) $(nl_at "$NL/crmark4.txt" 4 20000) $(nl_at "$NL/midmark.txt" 2 8631 | cut -c1-4) $(nl_at "$NL/midmark.txt" 3 8631 | cut -c1-4) $(nl_at "$NL/midmark.txt" 4 8631 | cut -c1-4)" \
	"1 2 3 none none none"

check "#2189 \.\r\n at the start of range 1 of 2 in an LF file / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/mark2.txt" single 2 1)" ok
check "#2189 \.\r\n at the start of range 2 of 3 in an LF file / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/mark3.txt" single 3 2)" ok
check "#2189 \.\r\n at the start of range 3 of 4 in an LF file / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/mark4.txt" single 4 3)" ok
check "#2189 \.\r\n in an LF file, 2 workers / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/mark2.txt" part 2 1)" ok
check "#2189 \.\r\n in an LF file, 3 workers / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/mark3.txt" part 3 2)" ok
check "#2189 \.\r\n in an LF file, 4 workers / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/mark4.txt" part 4 3)" ok
check "#2189 LF to CRLF at the start of range 1 of 2 / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/lfcr2.txt" single 2 1)" ok
check "#2189 LF to CRLF at the start of range 2 of 3 / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/lfcr3.txt" single 3 2)" ok
check "#2189 LF to CRLF at the start of range 3 of 4 / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/lfcr4.txt" single 4 3)" ok
check "#2189 LF to CRLF at the start of range 1 of 2 / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/lfcr2.txt" part 2 1)" ok
check "#2189 LF to CRLF at the start of range 2 of 3 / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/lfcr3.txt" part 3 2)" ok
check "#2189 LF to CRLF at the start of range 3 of 4 / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/lfcr4.txt" part 4 3)" ok
check "#2189 CRLF to LF at the start of range 1 of 2 / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/crlf2.txt" single 2 1)" ok
check "#2189 CRLF to LF at the start of range 2 of 3 / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/crlf3.txt" single 3 2)" ok
check "#2189 CRLF to LF at the start of range 3 of 4 / single table: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/crlf4.txt" single 4 3)" ok
check "#2189 CRLF to LF at the start of range 1 of 2 / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/crlf2.txt" part 2 1)" ok
check "#2189 CRLF to LF at the start of range 2 of 3 / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/crlf3.txt" part 3 2)" ok
check "#2189 CRLF to LF at the start of range 3 of 4 / partitioned: refused with heap COPY's SQLSTATE and message, 0 rows, no prepared transaction" \
	"$(nl_refused "$NL/crlf4.txt" part 4 3)" ok

check "#2189 control: a consistent LF file / single table, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/lf.txt" single)" "2:ok 3:ok 4:ok"
check "#2189 control: a consistent LF file / partitioned, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/lf.txt" part)" "2:ok 3:ok 4:ok"
check "#2189 control: a consistent CRLF file / single table, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/crall.txt" single)" "2:ok 3:ok 4:ok"
check "#2189 control: a consistent CRLF file / partitioned, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/crall.txt" part)" "2:ok 3:ok 4:ok"
_crm=""
for _w in 2 3 4; do
	_h="$(nl_heap "$NL/crmark$_w.txt")"; _hh="$(pgc_set_hash "SELECT * FROM h_nl")"; nl_target single
	_g="$(nl_outcome "SELECT pgcolumnar.parallel_copy('t_nl'::regclass, '$NL/crmark$_w.txt', $_w)" t_nl)"
	if [ "$_g $(pgc_set_hash "SELECT * FROM t_nl")" = "$_h $_hh" ]; then _crm="$_crm $_w:ok"; else _crm="$_crm $_w:[$_g] want [$_h]"; fi
done
check "#2189 control: \.\r\n at a range start in a CRLF file / single table, 2, 3 and 4 workers: loads heap COPY's rows" \
	"${_crm# }" "2:ok 3:ok 4:ok"
check "#2189 control: \.\n in the middle of a range in an LF file / single table, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/midmark.txt" single)" "2:ok 3:ok 4:ok"
check "#2189 control: \.\n in the middle of a range in an LF file / partitioned, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/midmark.txt" part)" "2:ok 3:ok 4:ok"
check "#2189 control: a CRLF file whose first record holds an escaped LF and ends in an escaped backslash / single table, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/esc.txt" single)" "2:ok 3:ok 4:ok"
check "#2189 control: a CRLF file whose first record holds an escaped LF and ends in an escaped backslash / partitioned, 2, 3 and 4 workers: loads heap COPY's rows" \
	"$(nl_loads "$NL/esc.txt" part)" "2:ok 3:ok 4:ok"
check "#2189 premise: heap COPY loads 4000 rows from lf, crall, esc, 1233 from midmark, and 2000, 2500 and 2500 from crmark2, crmark3 and crmark4" \
	"$(for _f in lf crall esc midmark crmark2 crmark3 crmark4; do nl_heap "$NL/$_f.txt" | cut -d'|' -f2 | tr '\n' ' '; done | sed 's/ $//')" \
	"4000 rows 4000 rows 4000 rows 1233 rows 2000 rows 2500 rows 2500 rows"

# ---- #2189, review of #2221: the first-line scan, probe by probe --------------
# pcopy_file_eol_type reads the file in COLUMNAR_SPLIT_SCAN_CHUNK (64 KiB) reads.
# Five mutants of it passed every check above; each probe below tells one apart
# from the fix, compared with heap COPY on the same file:
#   P1 (m06, one chunk read): a CRLF first line of 65537 bytes, then LF lines,
#      which COPY refuses;
#   P2 (m07, the CR flag reset per chunk): the first line's CR is byte 65535,
#      the last of the first read, and its LF byte 65536, the first of the next;
#   P3 (m08, a backslash does not escape a CR): an escaped CR on the first line
#      of an LF file;
#   P4 (m11, the escape flag reset per chunk): a backslash at byte 65535 escapes
#      the CR at byte 65536, in an LF file;
#   P5 (m09, the scan starts at byte 1): a CRLF file whose first line is empty
#      (one text column), and P5b, whose first byte is a backslash escaping a CR
#      (text before the key, so a partitioned target can take it too).
# Keys go 0100, 0200, ... 4000 in p3 and p5b. In p1, p2 and p4 they go 0100, then
# 0600, 0700, ... 4400, so the long line 1 is alone in the first partition and a
# partitioned load starts range 1 at line 2, as the single-table split does.
nl_probe() {	# KIND PATH: one probe file, 40 rows
	awk -v kind="$1" 'BEGIN {
		pad = "y"; while (length(pad) < 65530) pad = pad pad; pad = substr(pad, 1, 65530)
		if (kind == "p1" || kind == "p2") printf "0100\t%s\r\n", pad
		if (kind == "p3") printf "0100\tx\\\r\n"
		if (kind == "p4") printf "0100\t%s\\\rz\n", pad
		if (kind == "p5") { printf "\r\n"; for (k = 1; k <= 40; k++) printf "a\r\n"; exit }
		if (kind == "p5b") { printf "\\\r\t0100\r\n"; for (k = 2; k <= 40; k++) printf "x\t%04d\r\n", k * 100; exit }
		for (k = 2; k <= 40; k++) printf "%04d\tx%s", (kind == "p3" ? k : k + 4) * 100, (kind == "p2" ? "\r\n" : "\n")
	}' >"$2"
	chmod 644 "$2"
}
nl_same() {	# PATH TARGET COLS -> "W:ok" per worker count 2, 3, 4 when the load gives heap COPY's outcome,
	# message (after "worker N failed: ") and rows, or what it gave against heap COPY
	local w heap hh got gh res=""
	psql_run "DROP TABLE IF EXISTS h_nl; CREATE TABLE h_nl ($3);" >/dev/null
	heap="$(nl_outcome "COPY h_nl FROM '$1'" h_nl)"; hh="$(pgc_set_hash "SELECT * FROM h_nl")"
	for w in 2 3 4; do
		nl_target "$2" "$3"
		got="$(nl_outcome "SELECT pgcolumnar.parallel_copy('t_nl'::regclass, '$1', $w)" t_nl | sed 's/|pgcolumnar\.parallel_copy worker [0-9]* failed: /|/')"
		gh="$(pgc_set_hash "SELECT * FROM t_nl")"
		if [ "$got" = "$heap" ] && [ "$gh" = "$hh" ]; then res="$res $w:ok"
		else res="$res $w:[$got $gh] want [$heap $hh]"; fi
	done
	echo "${res# }"
}
for _p in p1 p2 p3 p4 p5 p5b; do nl_probe "$_p" "$NL/$_p.txt"; done
[ "$(id -u)" = "0" ] && chown -R postgres "$NL"
_probe_heap() {	# PATH COLS -> heap COPY's outcome
	psql_run "DROP TABLE IF EXISTS h_nl; CREATE TABLE h_nl ($2);" >/dev/null
	nl_outcome "COPY h_nl FROM '$1'" h_nl
}
check "#2189 premise: COLUMNAR_SPLIT_SCAN_CHUNK is 65536; p1 and p2 hold 0d 0a at bytes 65535-65536 and p4 5c 0d; heap COPY refuses p1 with 22P04 'literal newline found in data' and loads 40 rows from p2, p3, p4, p5b and 41 from p5" \
	"$(grep -c '^#define COLUMNAR_SPLIT_SCAN_CHUNK 65536$' "$PGC_SRCDIR/src/columnar_parallel_copy.c") $(od -An -tx1 -j 65535 -N 2 "$NL/p1.txt" | tr -d ' ') $(od -An -tx1 -j 65535 -N 2 "$NL/p2.txt" | tr -d ' ') $(od -An -tx1 -j 65535 -N 2 "$NL/p4.txt" | tr -d ' ') ; $(_probe_heap "$NL/p1.txt" 'id int, txt text') ; $(for _p in p2 p3 p4; do _probe_heap "$NL/$_p.txt" 'id int, txt text' | cut -d'|' -f2 | tr '\n' ' '; done)$(_probe_heap "$NL/p5b.txt" 'txt text, id int' | cut -d'|' -f2) $(_probe_heap "$NL/p5.txt" 't text' | cut -d'|' -f2)" \
	"1 0d0a 0d0a 5c0d ; 22P04|literal newline found in data|0 rows|0 prepared ; 40 rows 40 rows 40 rows 40 rows 41 rows"
check "#2189 probe P1, a CRLF first line longer than one 64 KiB scan read, then LF lines / single table, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p1.txt" single 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P1, a CRLF first line longer than one 64 KiB scan read, then LF lines / partitioned, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p1.txt" part 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P2, the first line's CR ends one 64 KiB scan read and its LF starts the next, a CRLF file / single table, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p2.txt" single 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P2, the first line's CR ends one 64 KiB scan read and its LF starts the next, a CRLF file / partitioned, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p2.txt" part 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P3, an escaped CR on the first line of an LF file / single table, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p3.txt" single 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P3, an escaped CR on the first line of an LF file / partitioned, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p3.txt" part 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P4, a backslash ending one 64 KiB scan read escapes a CR starting the next, an LF file / single table, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p4.txt" single 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P4, a backslash ending one 64 KiB scan read escapes a CR starting the next, an LF file / partitioned, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p4.txt" part 'id int, txt text')" "2:ok 3:ok 4:ok"
check "#2189 probe P5, a CRLF file whose first line is empty / single table of one text column, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p5.txt" single 't text')" "2:ok 3:ok 4:ok"
check "#2189 probe P5b, a CRLF file whose first byte is a backslash escaping a CR / single table, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p5b.txt" single 'txt text, id int')" "2:ok 3:ok 4:ok"
check "#2189 probe P5b, a CRLF file whose first byte is a backslash escaping a CR / partitioned, 2, 3 and 4 workers: heap COPY's outcome, message and rows" \
	"$(nl_same "$NL/p5b.txt" part 'txt text, id int')" "2:ok 3:ok 4:ok"

# ---- #2188: the caller's client_encoding, and characters that hold 0x5C -------
# COPY FROM reads a server file in the caller's client_encoding. When that is SJIS,
# BIG5, GBK, GB18030, UHC or JOHAB (the client-only encodings) and the database is
# not SQL_ASCII, core converts the bytes to the database encoding before it looks
# for backslashes and newlines, so the second byte of a two-byte character is never
# a backslash. In SJIS 83 5c is one character (U+30BD), and 83 5c 5c 0a is that
# character and an escaped newline. parallel_copy got both halves wrong: its byte
# scans read every 0x5c as a backslash, so they cut records in two, and its loaders
# read the file in their own client_encoding (the database's default), not the
# caller's, so they stored other text than COPY FROM does, or refused the file.
# Files, rows k = 1..N, id k:
#   sjis    (SJIS)  odd k: k TAB a k 83 5c 5c LF b LF, a character before an escaped
#                   newline; even k: k TAB a k 5c 83 5c LF, a backslash before it
#   big5    (BIG5)  the same with a5 5c (U+529F)
#   sjisbs  (SJIS)  every row k TAB a k 5c 83 5c LF
#   sjiseol (SJIS)  an LF file whose first line is 1 TAB a 83 5c 5c CR LF: the
#                   backslash escapes the CR, so the first line ends in LF
#   chunknl (SJIS)  line 1 puts the 83 at byte 65535, the last of the first 64 KiB
#                   scan read, and 5c 5c LF b LF after it; then 0002 TAB x LF ...
#   chunkcr (SJIS)  line 1 the same with 5c 5c CR LF; then LF lines
#   gb18030 (GB18030) odd k: k TAB a 81 30 81 30 81 5c z 5c LF b LF, a four-byte
#                   character, a two-byte one ending in 5c, a byte, an escaped
#                   newline; even k: k TAB a b0 a1 z 5c LF y LF
#   gbk     (GBK)   k TAB a 81 5c z 5c LF b LF
#   sjistk  (SJIS)  a k 83 5c TAB k LF, loaded into (txt text, id int): the 5c
#                   right before the TAB that ends the field before the key
#   utf8txt (UTF8)  k TAB c a f c3 a9 k LF, loaded with the database's default
#                   client_encoding set to SJIS and the caller's UTF8
# Controls, where 0x5c after a high-bit byte IS a backslash:
#   latin1  (LATIN1)                  k TAB a e9 5c LF b LF
#   utf8    (UTF8)                    k TAB a c3 a9 5c LF b LF
#   ascii   (SJIS, SQL_ASCII database) k TAB a 83 5c LF b LF: core does not convert
#                   when the database is SQL_ASCII, so it reads 5c LF as an escaped
#                   newline, and so must the splitters
# Each load is compared with heap COPY FROM of the same file in the same session
# encoding: outcome (with "worker N failed: " taken out), rows, and the row hash.
# The scratch databases are created from template0 with the C locale.
ENC_DB=pgc_pcopy_enc
ENC_ASCII=pgc_pcopy_ascii
for _d in "$ENC_DB:UTF8" "$ENC_ASCII:SQL_ASCII"; do
	psql_admin "DROP DATABASE IF EXISTS ${_d%%:*}" >/dev/null 2>&1
	psql_admin "CREATE DATABASE ${_d%%:*} ENCODING '${_d#*:}' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0" >/dev/null
	PGC_DB="${_d%%:*}" psql_run "CREATE EXTENSION pgcolumnar" >/dev/null
done
ENC="$DATADIR/enc"; mkdir -p "$ENC"; chmod 755 "$ENC"
enc_gen() {	# KIND N PATH: writes PATH and PATH.starts, the byte offset of every record but the first
	LC_ALL=C awk -v kind="$1" -v n="$2" -v starts="$3.starts" 'BEGIN {
		pad = "y"; while (length(pad) < 65530) pad = pad pad; pad = substr(pad, 1, 65530)
		off = 0
		for (k = 1; k <= n; k++) {
			if (kind == "sjis" || kind == "big5") {
				c = (kind == "sjis") ? "\203\134" : "\245\134"
				r = (k % 2) ? sprintf("%d\ta%d%s\134\nb\n", k, k, c) : sprintf("%d\ta%d\134%s\n", k, k, c)
			} else if (kind == "sjisbs") r = sprintf("%d\ta%d\134\203\134\n", k, k)
			else if (kind == "sjiseol") r = (k == 1) ? "1\ta\203\134\134\r\n" : sprintf("%d\tx\n", k)
			else if (kind == "chunknl") r = (k == 1) ? "0001\t" pad "\203\134\134\nb\n" : sprintf("%04d\tx\n", k)
			else if (kind == "chunkcr") r = (k == 1) ? "0001\t" pad "\203\134\134\r\n" : sprintf("%04d\tx\n", k)
			else if (kind == "gb18030") r = (k % 2) ? sprintf("%d\ta\201\060\201\060\201\134z\134\nb\n", k) : sprintf("%d\ta\260\241z\134\ny\n", k)
			else if (kind == "gbk") r = sprintf("%d\ta\201\134z\134\nb\n", k)
			else if (kind == "sjistk") r = sprintf("a%d\203\134\t%d\n", k, k)
			else if (kind == "utf8txt") r = sprintf("%d\tcaf\303\251%d\n", k, k)
			else if (kind == "latin1") r = sprintf("%d\ta\351\134\nb\n", k)
			else if (kind == "utf8") r = sprintf("%d\ta\303\251\134\nb\n", k)
			else if (kind == "ascii") r = sprintf("%d\ta\203\134\nb\n", k)
			if (k > 1) print off > starts
			printf "%s", r
			off += length(r)
		}
	}' >"$3"
	chmod 644 "$3" "$3.starts"
}
enc_outcome() {	# DB CLIENT_ENCODING SQL TABLE -> "SQLSTATE|message|N rows|P prepared" or "loaded|N rows|P prepared"
	local out head
	out="$(timeout -s KILL 120 env PATH="$PGC_BINDIR:$PATH" PGCLIENTENCODING="$2" psql -X -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$1" -qtA -c "SET statement_timeout = '60s'" -c '\set VERBOSITY verbose' -c "$3" 2>&1)"
	if grep -q '^ERROR:' <<<"$out"; then
		head="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' <<<"$out" | head -1 | sed 's/|pgcolumnar\.parallel_copy worker [0-9]* failed: /|/')"
	else
		head="loaded"
	fi
	echo "${head:-unparsed ($out)}|$(PGC_DB=$1 q "SELECT count(*) FROM $4") rows|$(PGC_DB=$1 q "SELECT count(*) FROM pg_prepared_xacts") prepared"
}
enc_target() {	# DB single|part -> (re)creates t_enc ($ENC_COLS, default "id int, txt text") as that target, empty
	local ddl i lo hi cols="${ENC_COLS:-id int, txt text}"
	if [ "$2" = single ]; then
		PGC_DB=$1 psql_run "DROP TABLE IF EXISTS t_enc CASCADE; CREATE TABLE t_enc ($cols) USING pgcolumnar;" >/dev/null
		return
	fi
	ddl="DROP TABLE IF EXISTS t_enc CASCADE; CREATE TABLE t_enc ($cols) PARTITION BY RANGE (id);"
	for i in 0 1 2 3 4 5 6 7; do
		if [ "$i" = 0 ]; then lo=MINVALUE; else lo=$((i * 500 + 1)); fi
		if [ "$i" = 7 ]; then hi=MAXVALUE; else hi=$(((i + 1) * 500 + 1)); fi
		ddl="$ddl CREATE TABLE t_enc_p$i PARTITION OF t_enc FOR VALUES FROM ($lo) TO ($hi) USING pgcolumnar;"
	done
	PGC_DB=$1 psql_run "$ddl" >/dev/null
}
enc_heap() {	# DB CLIENT_ENCODING PATH -> enc_outcome of heap COPY of PATH into a fresh h_enc
	PGC_DB=$1 psql_run "DROP TABLE IF EXISTS h_enc; CREATE TABLE h_enc (${ENC_COLS:-id int, txt text});" >/dev/null
	enc_outcome "$1" "$2" "COPY h_enc FROM '$3'" h_enc
}
enc_row() {	# DB ID -> h_enc's txt for that id, as hex bytes
	PGC_DB=$1 q "SELECT encode(convert_to(txt, pg_encoding_to_char(encoding)), 'hex') FROM h_enc, pg_database WHERE datname = current_database() AND id = $2"
}
enc_same() {	# DB CLIENT_ENCODING PATH TARGET [WORKERS...] -> "W:ok" per worker count (2 3 4 by default)
	# when the load gives heap COPY's outcome, rows and row hash, or what it gave against heap COPY
	local db="$1" ce="$2" path="$3" tgt="$4" w heap hh got gh res=""
	shift 4
	heap="$(enc_heap "$db" "$ce" "$path")"; hh="$(PGC_DB=$db pgc_set_hash "SELECT * FROM h_enc")"
	for w in ${*:-2 3 4}; do
		enc_target "$db" "$tgt"
		got="$(enc_outcome "$db" "$ce" "SELECT pgcolumnar.parallel_copy('t_enc'::regclass, '$path', $w)" t_enc)"
		gh="$(PGC_DB=$db pgc_set_hash "SELECT * FROM t_enc")"
		if [ "$got" = "$heap" ] && [ "$gh" = "$hh" ]; then res="$res $w:ok"
		else res="$res $w:[$got $gh] want [$heap $hh]"; fi
	done
	echo "${res# }"
}
enc_cuts() {	# CLIENT_ENCODING PATH -> "W:ok" per worker count 2, 3, 4 when every interior split
	# offset is a record start and no range is empty, or the offsets
	local w arr i bad res=""
	for w in 2 3 4; do
		read -r -a arr <<<"$(PGC_DB=$ENC_DB PGCLIENTENCODING="$1" q "SELECT array_to_string(pgcolumnar.file_split_offsets('$2', $w), ' ')")"
		bad=""
		[ "${#arr[@]}" = $((w + 1)) ] || bad=1
		for i in $(seq 1 $((w - 1))); do
			grep -qx -- "${arr[$i]:-x}" "$2.starts" || bad=1
			[ "${arr[$i]:-0}" -gt "${arr[$((i - 1))]:-0}" ] 2>/dev/null || bad=1
		done
		[ "${arr[$w]:-0}" -gt "${arr[$((w - 1))]:-0}" ] 2>/dev/null || bad=1
		if [ -z "$bad" ]; then res="$res $w:ok"; else res="$res $w:[${arr[*]}]"; fi
	done
	echo "${res# }"
}
for _f in sjis:2000 big5:2000 sjisbs:2000 sjiseol:2000 chunknl:9363 chunkcr:4000 gb18030:2000 gbk:2000 sjistk:2000 utf8txt:2000 latin1:2000 utf8:2000 ascii:2000; do
	enc_gen "${_f%%:*}" "${_f#*:}" "$ENC/${_f%%:*}.txt"
done
[ "$(id -u)" = "0" ] && chown -R postgres "$ENC"

check "#2188 premise: the scratch databases are UTF8 and SQL_ASCII; under client_encoding SJIS heap COPY loads 2000 rows from sjis and from sjisbs and stores sjis row 1 as 61 31 e3 82 bd 0a 62 and row 2 as 61 32 e3 82 bd; under BIG5 2000 rows from big5, row 1 61 31 e5 8a 9f 0a 62; under SJIS 2000 rows from sjistk, row 1 61 31 e3 82 bd" \
	"$(PGC_DB=$ENC_DB q "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()") $(PGC_DB=$ENC_ASCII q "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()") $(enc_heap $ENC_DB SJIS "$ENC/sjisbs.txt" | cut -d'|' -f2) $(enc_heap $ENC_DB SJIS "$ENC/sjis.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1) $(enc_row $ENC_DB 2) $(enc_heap $ENC_DB BIG5 "$ENC/big5.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1) $(ENC_COLS="txt text, id int" enc_heap $ENC_DB SJIS "$ENC/sjistk.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1)" \
	"UTF8 SQL_ASCII 2000 rows 2000 rows 6131e382bd0a62 6132e382bd 2000 rows 6131e58a9f0a62 2000 rows 6131e382bd"
check "#2188 premise: under client_encoding SJIS heap COPY loads 2000 rows from sjiseol and stores row 1 as 61 e3 82 bd 0d; 9363 rows from chunknl, whose bytes 65535-65538 are 83 5c 5c 0a, and 4000 from chunkcr, whose bytes 65535-65539 are 83 5c 5c 0d 0a" \
	"$(enc_heap $ENC_DB SJIS "$ENC/sjiseol.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1) $(enc_heap $ENC_DB SJIS "$ENC/chunknl.txt" | cut -d'|' -f2) $(od -An -tx1 -j 65535 -N 4 "$ENC/chunknl.txt" | tr -d ' ') $(enc_heap $ENC_DB SJIS "$ENC/chunkcr.txt" | cut -d'|' -f2) $(od -An -tx1 -j 65535 -N 5 "$ENC/chunkcr.txt" | tr -d ' ')" \
	"2000 rows 61e382bd0d 9363 rows 835c5c0a 4000 rows 835c5c0d0a"
check "#2188 SJIS, a two-byte character whose second byte is 0x5C before an escaped newline, and a backslash before such a character / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB SJIS "$ENC/sjis.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 SJIS, a two-byte character whose second byte is 0x5C before an escaped newline, and a backslash before such a character / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB SJIS "$ENC/sjis.txt" part)" "2:ok 3:ok 4:ok"
check "#2188 BIG5, a two-byte character whose second byte is 0x5C before an escaped newline, and a backslash before such a character / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB BIG5 "$ENC/big5.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 BIG5, a two-byte character whose second byte is 0x5C before an escaped newline, and a backslash before such a character / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB BIG5 "$ENC/big5.txt" part)" "2:ok 3:ok 4:ok"
check "#2188 SJIS, a backslash before a two-byte character whose second byte is 0x5C, right before every newline / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB SJIS "$ENC/sjisbs.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 SJIS, a two-byte character whose second byte is 0x5C right before the TAB that ends the field before the key / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(ENC_COLS="txt text, id int" enc_same $ENC_DB SJIS "$ENC/sjistk.txt" part)" "2:ok 3:ok 4:ok"
check "#2188 SJIS and BIG5: file_split_offsets cuts sjis, sjisbs and big5 only at record starts, with no range empty, for 2, 3 and 4 workers" \
	"$(enc_cuts SJIS "$ENC/sjis.txt") ; $(enc_cuts SJIS "$ENC/sjisbs.txt") ; $(enc_cuts BIG5 "$ENC/big5.txt")" \
	"2:ok 3:ok 4:ok ; 2:ok 3:ok 4:ok ; 2:ok 3:ok 4:ok"
check "#2188 SJIS, the first line's two-byte character whose second byte is 0x5C before an escaped CR, in an LF file / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB SJIS "$ENC/sjiseol.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 SJIS, the first line's two-byte character whose second byte is 0x5C before an escaped CR, in an LF file / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB SJIS "$ENC/sjiseol.txt" part)" "2:ok 3:ok 4:ok"
check "#2188 SJIS, a two-byte character split across two 64 KiB scan reads, before an escaped newline / single table, 2 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB SJIS "$ENC/chunknl.txt" single 2)" "2:ok"
check "#2188 SJIS, the first line's two-byte character split across two 64 KiB scan reads, before an escaped CR, in an LF file / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB SJIS "$ENC/chunkcr.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 premise: under client_encoding GB18030 heap COPY loads 2000 rows from gb18030 and stores row 1 as 61 c2 80 e4 b9 97 7a 0a 62 and row 2 as 61 e5 95 8a 7a 0a 79; under GBK 2000 rows from gbk, row 1 61 e4 b9 97 7a 0a 62" \
	"$(enc_heap $ENC_DB GB18030 "$ENC/gb18030.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1) $(enc_row $ENC_DB 2) $(enc_heap $ENC_DB GBK "$ENC/gbk.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1)" \
	"2000 rows 61c280e4b9977a0a62 61e5958a7a0a79 2000 rows 61e4b9977a0a62"
check "#2188 GB18030, a four-byte character next to a two-byte character whose second byte is 0x5C, then a byte and an escaped newline / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB GB18030 "$ENC/gb18030.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 GB18030, a four-byte character next to a two-byte character whose second byte is 0x5C, then a byte and an escaped newline / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB GB18030 "$ENC/gb18030.txt" part)" "2:ok 3:ok 4:ok"
check "#2188 GBK, a two-byte character whose second byte is 0x5C, then a byte and an escaped newline / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB GBK "$ENC/gbk.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 GBK, a two-byte character whose second byte is 0x5C, then a byte and an escaped newline / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB GBK "$ENC/gbk.txt" part)" "2:ok 3:ok 4:ok"

PGC_DB=$ENC_DB psql_run "ALTER DATABASE $ENC_DB SET client_encoding = 'SJIS'" >/dev/null
check "#2188 premise: with the database's default client_encoding set to SJIS a new session's is SJIS, and heap COPY under the caller's UTF8 loads 2000 rows from utf8txt and stores row 1 as 63 61 66 c3 a9 31" \
	"$(PGC_DB=$ENC_DB q "SHOW client_encoding") $(enc_heap $ENC_DB UTF8 "$ENC/utf8txt.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1)" \
	"SJIS 2000 rows 636166c3a931"
check "#2188 the database's default client_encoding SJIS and the caller's UTF8, UTF8 text / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB UTF8 "$ENC/utf8txt.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 the database's default client_encoding SJIS and the caller's UTF8, UTF8 text / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB UTF8 "$ENC/utf8txt.txt" part)" "2:ok 3:ok 4:ok"
PGC_DB=$ENC_DB psql_run "ALTER DATABASE $ENC_DB RESET client_encoding" >/dev/null

check "#2188 premise: heap COPY loads 2000 rows each from latin1 under LATIN1, utf8 under UTF8 and, in the SQL_ASCII database, ascii under SJIS, storing row 1 as 61 c3 a9 0a 62, 61 c3 a9 0a 62 and 61 83 0a 62" \
	"$(enc_heap $ENC_DB LATIN1 "$ENC/latin1.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1) $(enc_heap $ENC_DB UTF8 "$ENC/utf8.txt" | cut -d'|' -f2) $(enc_row $ENC_DB 1) $(enc_heap $ENC_ASCII SJIS "$ENC/ascii.txt" | cut -d'|' -f2) $(enc_row $ENC_ASCII 1)" \
	"2000 rows 61c3a90a62 2000 rows 61c3a90a62 2000 rows 61830a62"
check "#2188 control: LATIN1, a high-bit byte before an escaped newline / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB LATIN1 "$ENC/latin1.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 control: LATIN1, a high-bit byte before an escaped newline / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB LATIN1 "$ENC/latin1.txt" part)" "2:ok 3:ok 4:ok"
check "#2188 control: UTF8, a two-byte character before an escaped newline / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB UTF8 "$ENC/utf8.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 control: UTF8, a two-byte character before an escaped newline / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_DB UTF8 "$ENC/utf8.txt" part)" "2:ok 3:ok 4:ok"
check "#2188 control: a SQL_ASCII database under client_encoding SJIS, 83 5c before a newline, which COPY reads as an escaped newline / single table, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_ASCII SJIS "$ENC/ascii.txt" single)" "2:ok 3:ok 4:ok"
check "#2188 control: a SQL_ASCII database under client_encoding SJIS, 83 5c before a newline, which COPY reads as an escaped newline / partitioned, 2, 3 and 4 workers: heap COPY's outcome, rows and content" \
	"$(enc_same $ENC_ASCII SJIS "$ENC/ascii.txt" part)" "2:ok 3:ok 4:ok"
pgc_summary

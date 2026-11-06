#!/usr/bin/env bash
#
# pgColumnar fetch-by-row-number cache (issue #143).
#
# PgColumnarReadRowByNumber() used to read and decode a whole row group per row
# returned, so fetching N rows out of one group cost N times the group. A
# statement-scoped cache of the decoded group removes the repeat.
#
# Two things are asserted, because they need different kinds of evidence.
#
# 1. The cache is used. Timed as a ratio rather than a threshold: the same number
#    of index-driven fetches is run against the same rows laid out as one big row
#    group and as many small ones. Without the cache the per-fetch cost is
#    proportional to the group size, so the single-group case is many times
#    slower; with it, both are dominated by the fetches themselves and the ratio
#    collapses. A ratio is portable where a millisecond count is not.
#
# 2. The scoping that makes the cache correct is present. This part is a source
#    assertion, in the style of wal_envelope.sh and decode_interrupts.sh, and the
#    reason is worth recording: removing the command-id rejection on its own does
#    not fail any suite, because four other things independently prevent a stale
#    hit (the storage id is in the key, compaction retires group numbers rather
#    than rewriting them under the same number, the geometry check rejects a
#    mismatched entry, and the executor-end hook releases the cache between
#    statements). A behavioural test for that one guard would have to defeat the
#    other four, which is not a shape the suite should carry. Asserting the guards
#    exist is the honest cover for them.
#
# Usage:  test/native_fetch_cache.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_FETCH_ROWS:-20000}
UPD=$((ROWS / 10))

# --- 1. the cache is used -----------------------------------------------------

build() {  # table, rows-per-group
	psql_run "DROP TABLE IF EXISTS $1;
		SET pgcolumnar.stripe_row_limit = $2;
		SET pgcolumnar.chunk_group_row_limit = $2;
		CREATE TABLE $1 (id int, v int, t text) USING pgcolumnar;
		INSERT INTO $1 SELECT g, g, 'row' || g FROM generate_series(1, $ROWS) g;
		CREATE INDEX ${1}_id ON $1 (id);"
}

# Every updated row goes through the fetch path whatever the scan plans: the
# UPDATE fetches its target rows by number. Measured (#1341) on PG18: this
# UPDATE plans as Custom Scan (PgColumnarScan) under these SETs, and with the
# cache made to miss on every lookup the ratio below still goes red (844 ms
# against 93 ms). So the SETs are not what makes it a fetch, and no plan
# premise is needed here, unlike the point queries further down.
upd_ms() {
	local start end
	start=$(date +%s%N)
	psql_run "SET max_parallel_workers_per_gather = 0;
		SET enable_seqscan = off;
		SET enable_bitmapscan = off;
		UPDATE $1 SET v = id + 1 WHERE id <= $UPD;" >/dev/null 2>&1
	end=$(date +%s%N)
	echo $(( (end - start) / 1000000 ))
}

# Best of three readings, not one and not the average.
#
# Scheduling noise on a shared runner only ever ADDS latency, so the minimum is
# the reading least polluted by it, and a single descheduled run cannot widen the
# ratio on its own. test/cancel_decode.sh makes this argument for its own ratio
# and runs in CI on the strength of it; these three had the same-run half and not
# this half, which is why they were guarded instead (#792).
#
# Measured before the change, six busy cores on an eight-core box: the one/many
# ratio reached 2.39 against its bound of 3 on a single reading, where the best of
# three across the same runs was 0.89 -- which is where the idle readings sit.
#
# A REPEATED MEASUREMENT MUST BE IDEMPOTENT. upd_ms updated with "v = v + 1",
# which is fine once and wrong three times: the suite's own correctness arms below
# assert v = id + 1, and they caught it. It sets "v = id + 1" now, which is the
# same work per row and true after any number of runs.
# The three cost assertions below go through check_ratio rather than computing
# the comparison here and passing yes/no to check. They were hand-rolled while
# they used check_timing, which takes a scalar; now that they are ordinary checks
# (#792) the ratio helper is available and is strictly better:
#
#   * it refuses a zero on EITHER side -- "a side of the ratio is zero, so
#     nothing was measured". The hand-rolled form guarded only the denominator,
#     so a numerator timing at 0 ms, which means the measurement fell below timer
#     resolution, passed silently as a ratio of 0;
#   * it compares as a float instead of truncating integer division, and prints
#     the ratio with both sides in the verdict rather than a hand-built string.
#
# The bound moves by a hair and it is worth saying so exactly, since being exact
# about it is the point. The old form failed at a ratio of exactly 3.0 (integer
# division, "< 3"). check_ratio rounds to two places before comparing, so it
# fails above 3.005 rather than above 3.0: a true 3.004 prints as 3.00 and
# passes. Nothing observed on these shapes is within two whole units of that.

min3() {  # min3 FUNC ARG...
	local a b c
	a="$("$@")"; b="$("$@")"; c="$("$@")"
	printf '%s\n' "$a" "$b" "$c" | sort -n | head -1
}

# THE FIXTURE HAS TO BE REBUILT BETWEEN READINGS, because the measured statement
# destroys the geometry it measures (#801).
#
# An UPDATE on a columnar table does not rewrite the group it read. It marks the
# rows deleted and appends them as a NEW group. So the first reading of fc_one
# leaves groups 1:20000 and 2:2000, and every later reading finds the rows it
# targets in the small appended group -- exactly the geometry fc_many has. The
# two arms stop differing after one reading.
#
# min3 then takes a minimum across two different fixtures, and the post-rewrite
# readings are the fast ones, so it returns one of them every time. That is why
# the guard could not fail. Measured with a per-fetch decode counter, against a
# build whose fetch cache retains nothing so every fetch re-decodes:
#
#              reading 1   reading 2   reading 3     this suite's verdict
#   reused      629 ms       81 ms       96 ms        PASS 0.98x   <- false
#   rebuilt     617 ms      623 ms      620 ms        FAIL 6.13x   <- correct
#
# The per-reading times come from a standalone probe, which prints them. The
# verdicts come from running this suite whole against that same .so, once with
# each form of the fixture handling, so they are two runs and not one. The
# rebuilt figure was 5.89x and 6.13x on the two runs of it.
#
# The #353 guard below fails on that .so as well, at 6.48x against its 5x
# bound, which is what shows the ablation was real rather than a build that did
# nothing. On a stock .so this ratio has read 0.63x, 0.76x, 0.77x, 0.92x and
# 1.00x, the 0.76x under the matrix running six suites at once. So the bound of
# 3 sits about three times above the worst passing reading and about half the
# smallest failing one.
#
# The decode counter says the same thing in rows rather than milliseconds:
# reused, fc_one decodes 40,000,000 rows on the first reading and 4,000,000 on
# the next two; rebuilt, it decodes 40,000,000 on all three.
min3_fresh() {  # table, rows-per-group -- rebuild before every reading
	local a b c
	build "$1" "$2" >/dev/null; geom_seen "$1"; a="$(upd_ms "$1")"
	build "$1" "$2" >/dev/null; geom_seen "$1"; b="$(upd_ms "$1")"
	build "$1" "$2" >/dev/null; geom_seen "$1"; c="$(upd_ms "$1")"
	printf '%s\n' "$a" "$b" "$c" | sort -n | head -1
}

# The premise the ratio rests on, recorded at the moment of each reading rather
# than once at the start: the number of row groups the table is laid out in.
# A rebuild that stopped happening, or a stripe limit that stopped being
# honoured, shows up here as "1 2 3" instead of "1 1 1" -- which is precisely
# the state that produced the false green above.
#
# It doubles as the fixture's row-count premise. build's output is discarded so
# that three rebuilds do not print three copies of its notices, so an INSERT
# that died would otherwise be silent; an empty table has no row groups at all
# and records "0 0 0" here.
#
# Both halves were proved by mutation, each changing one thing and nothing else.
# Dropping the rebuild records "1 2 3" for fc_one and "10 11 12" for fc_many;
# making the INSERT populate nothing records "0 0 0" for both. In BOTH mutants
# the ratio check below still passed -- at 0.77x and at 1.14x -- so this premise
# is the only thing standing between either fault and a green suite.
#
# A record accumulates in a file rather than a variable because each reading
# runs inside the command substitution that captures the timing, so an
# assignment would not survive it. xargs normalises the separators, so the
# expected value below is not carrying an invisible trailing space.
geom_seen() {  # table -- append this reading's group count to its record
	q "SELECT count(*) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s USING (storage_id)
		WHERE s.relation_oid = '$1'::regclass" >> "$PGC_WORKDIR/geom.$1"
}

: > "$PGC_WORKDIR/geom.fc_one"
: > "$PGC_WORKDIR/geom.fc_many"
one="$(min3_fresh fc_one "$ROWS")"          # every row in a single row group
many="$(min3_fresh fc_many $((ROWS / 10)))" # the same rows across ten
echo "-- $UPD fetches: one group of $ROWS took ${one} ms; ten groups took ${many} ms"

check "every fc_one reading measured one row group (#801)" \
	"$(xargs < "$PGC_WORKDIR/geom.fc_one")" "1 1 1"
check "every fc_many reading measured ten row groups (#801)" \
	"$(xargs < "$PGC_WORKDIR/geom.fc_many")" "10 10 10"

# Without the cache the single-group case decodes ten times as much per fetch.
# Measured at 6.13x on the build described above; with the cache both are
# dominated by the fetches themselves and the ratio sits at or below 1x.
check_ratio "fetching from one big group is not far dearer than from ten small ones" \
	"$one" "$many" "3"

# and the rows are still right
check "the updated rows are correct" \
	"$(q "SELECT count(*) FROM fc_one WHERE v = id + 1;")" "$UPD"
check "the untouched rows are unchanged" \
	"$(q "SELECT count(*) FROM fc_one WHERE v = id;")" "$((ROWS - UPD))"

# --- 2. the scoping guards are present ----------------------------------------

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

# PIN THE PROPERTY, NOT THE CALLER COUNT (#1078's class). Each of these four
# counted a string across a whole file and compared it against a literal, which
# asserts HOW MANY sites exist today rather than that every site is honest. The
# failure direction differs per arm and so does the repair, which is why they are
# not all rewritten the same way:
#
#   the entry key    both directions -- a second honest lookup reddens it, and a
#                    lookup that drops the storage id is invisible. Self-referential.
#   the cid reject   noise only -- it is an EXISTENCE claim, and a second honest
#                    site reddened it. Scoped to the function that must contain it.
#   the geometry     the count was a proxy for one of FOUR compared fields, and
#                    said nothing about the other three. Membership instead.
#   the discard      the count 2 was a proxy for "from these two places". Name them.

_nfc_slot="$(awk '/^pgcolumnar_fetch_group_slot\(/,/^}/' "$SRC/columnar_reader.c")"
_nfc_row="$(awk '/^pgcolumnar_fetch_row\(/,/^}/' "$SRC/columnar_reader.c")"

check "premise: the fetch-cache functions were extracted, not empty ranges" \
	"$([ -n "$_nfc_slot" ] && [ -n "$_nfc_row" ] && echo yes || echo no)" "yes"

# EVERY group-number comparison is keyed by the storage id as well. Honest extra
# lookups move both counts together; one that forgets the storage id moves only
# the total. Both numbers are in the compared strings, so the message reconciles.
_nfc_grp="$(grep -c 'e->groupNumber == groupNumber' "$SRC/columnar_reader.c")"
_nfc_keyed="$(grep -c 'e->storageId == storageId && e->groupNumber == groupNumber' \
	"$SRC/columnar_reader.c")"

check "premise: there is a group-number comparison to key" \
	"$([ "${_nfc_grp:-0}" -ge 1 ] && echo yes || echo no)" "yes"

check "every group-number comparison is keyed by the storage id too" \
	"keyed $_nfc_keyed of $_nfc_grp" "keyed $_nfc_grp of $_nfc_grp"

# `case` OVER A CAPTURED STRING, NOT A PIPE INTO grep -q. `grep -q` exits on its
# first match and closes the pipe, so the writer can take SIGPIPE -- #486, and
# selftest/080 refuses it. The same file family already has the answer at
# native_fetch_projection.sh:155: a `case` needs no subprocess and no pipe.
check "an entry from an earlier command is rejected" \
	"$(case "$_nfc_slot" in *'e->cid != cid'*) echo yes ;; *) echo no ;; esac)" "yes"

# The recheck compares four fields. Counting one of them asserted nothing about
# the other three, and a reflow that dropped one would have passed.
#
# THREE COMPARE AGAINST THE ROW GROUP AND ONE DOES NOT. `natts` is compared against
# the scan's own column count rather than against `rg`, so it needs its own pattern.
# Reported by @jdatcmd: the first version put `!= natts` in the alternation for ALL
# four, which is harmless -- `entry->firstRowNumber != natts` appears nowhere -- and
# is still wrong as a claim, because each arm would then accept a comparison the
# property does not describe.
for _f in firstRowNumber rowCount fileOffset; do
	check "a hit re-checks the group's $_f against the row group" \
		"$(case "$_nfc_row" in \
			*"entry->$_f != rg->$_f"*) echo yes ;; \
			*) echo no ;; esac)" "yes"
done

check "a hit re-checks the group's natts against the scan's column count" \
	"$(case "$_nfc_row" in \
		*"entry->natts != natts"*) echo yes ;; \
		*) echo no ;; esac)" "yes"

# Named call sites rather than a count of 2: a third honest caller is not a defect,
# and losing either of these two is.
#
# CAPTURED FIRST, WITH ITS OWN PREMISE, so "this function does not discard the
# cache" and "the awk range matched nothing" are different answers. Without the
# premise a renamed function reads as a missing call, which is the both-readings
# problem a literal match always has.
for _fn in pgcolumnar_executor_end pgcolumnar_xact_callback; do
	_nfc_fn="$(awk "/^$_fn\\(/,/^}/" "$SRC/columnar_tableam.c")"

	check "premise: $_fn was extracted, not an empty range" \
		"$([ -n "$_nfc_fn" ] && echo yes || echo no)" "yes"

	check "the fetch cache is discarded from $_fn" \
		"$(case "$_nfc_fn" in *PgColumnarDiscardFetchCache*) echo yes ;; *) echo no ;; esac)" "yes"
done

# The SETs every fetch-path guard below runs under, defined once because the
# premise that asserts the plan and the measurement that depends on it must not
# drift apart (#797). enable_seqscan/enable_bitmapscan alone are not enough:
# neither disables the columnar custom scan, and pgcolumnar.enable_index_fetch_penalty
# (on by default since #355) costs the per-row fetch highly enough that the
# planner answers these queries with a group scan instead -- which is how these
# guards spent months timing a mechanism they do not exercise.
FETCH_PATH_SETS="SET max_parallel_workers_per_gather=0; SET enable_seqscan=off;
	SET enable_bitmapscan=off; SET pgcolumnar.enable_index_fetch_penalty=off;"

# --- #353: a wide group's decode scratch must not blow the fetch cap ----------
# The by-row-number decode allocated its intermediates -- the decompressed region
# and each vector's decoded buffer -- in the cached entry, ~3x the result. A wide
# group's projected prefix then exceeded the 32MB cap and was re-decoded on every
# fetch (measured ~200x). The scratch now lives in a transient context, so the
# entry keeps only the result. Test: a wide table at the default stripe (one big
# group, large decoded prefix) vs a small-stripe control; the wide case must not
# be far dearer per fetch, and must return the same answer.
wbuild() {  # table, stripe
	psql_run "DROP TABLE IF EXISTS $1;
		CREATE TABLE $1 (id int, h text, c1 int,c2 int,c3 int,c4 int,c5 int,c6 int,
		                 c7 int,c8 int,c9 int,c10 int, u float8,
		                 t1 text,t2 text,t3 text,t4 text,t5 text,t6 text,t7 text,t8 text)
		    USING pgcolumnar;
		SELECT pgcolumnar.set_options('$1', stripe_row_limit => $2);
		INSERT INTO $1 SELECT g, 'h' || (g % 500), g,g,g,g,g,g,g,g,g,g, (g % 100)::float8,
		    repeat('x',40),repeat('y',40),repeat('z',40),'a'||g,'b'||g,'c'||g,'d'||g,'e'||g
		    FROM generate_series(1, 300000) g;
		CREATE INDEX ${1}_h ON $1 (h);"
}
w_ms() {  # index-driven point query over one host: many fetches into few groups
	local start end
	start=$(date +%s%N)
	psql_run "$FETCH_PATH_SETS
		SELECT count(*), max(u) FROM $1 WHERE h='h1';" >/dev/null 2>&1
	end=$(date +%s%N); echo $(( (end - start) / 1000000 ))
}
wbuild fc_wide 150000      # default stripe: one/two big groups, wide decoded prefix
wbuild fc_wnarrow 20000    # smaller groups that fit under the cap regardless
for _t in fc_wide fc_wnarrow; do
	check "the $_t point query really drives the fetch path (#797)" \
		"$(pgc_uses_row_fetch "$FETCH_PATH_SETS" \
			"SELECT count(*), max(u) FROM $_t WHERE h='h1'")" yes
done
bigw="$(min3 w_ms fc_wide)"; smallw="$(min3 w_ms fc_wnarrow)"
echo "-- #353 wide point query: default-stripe ${bigw} ms, small-stripe ${smallw} ms"
check_ratio "a wide group's fetch is not far dearer than a small group's (#353)" \
	"$bigw" "$smallw" "5"
check "the wide-group point query is correct (#353)" \
	"$(q "SELECT count(*) || '|' || coalesce(max(u)::text,'z') FROM fc_wide WHERE h='h1'")" \
	"$(q "SELECT count(*) || '|' || coalesce(max(u)::text,'z') FROM fc_wnarrow WHERE h='h1'")"

# --- #359: crossing the cap must cost proportionally, not totally -------------
# The #353 case above measures a two-column projection across two group sizes. It
# therefore never crosses the relocated cap, and went green on exactly the query
# family that still cliffed. The axis it holds constant is the one that mattered:
# projection width at a fixed group size. This varies that instead.
#
# Each text column below decodes to ~154 bytes x 50,000 rows = ~7.7 MB, so the
# 32 MB cap falls between four and five projected columns. Going over it used to
# drop the entry whole, so the fifth column cost a re-read of the group and a
# re-decode of all five, on every fetch. The cache now keeps the columns that fit
# and re-decodes only the remainder.
#
# Measured here, PG18 assert, going from four to five columns. Both figures are
# from this suite rather than from a standalone probe, because the ~600 MB of
# fixtures built above leave the box in a different state than a cold one, and the
# ratio moves with it -- the same fixed build measures 2.2x standalone and 5.8x
# here. Comparing a suite number against a standalone number would be comparing
# two machines:
#   before   77 ms -> 1902 ms   (24.7x, flat either side -- the cliff)
#   after    64 ms ->  368 ms   (5.8x -- a ramp)
# Four runs of the fixed build in this suite gave 5.6x, 5.6x, 5.9x and 6.4x. The
# bound is 12x, roughly a factor of two clear of either build, so it discriminates
# the shape rather than encoding one box's timings.
psql_run "DROP TABLE IF EXISTS fc_w359;
	CREATE TABLE fc_w359 (id int, h text,
	    t1 text,t2 text,t3 text,t4 text,t5 text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('fc_w359', stripe_row_limit => 50000);
	INSERT INTO fc_w359 SELECT g, 'h' || (g % 1000),
	    repeat('a',150),repeat('b',150),repeat('c',150),
	    repeat('d',150),repeat('e',150)
	FROM generate_series(1,100000) g;
	CREATE INDEX fc_w359_h ON fc_w359 (h);"

w359_ms() {  # number of projected text columns
	local n=$1 sel="count(*)" i start end
	for i in $(seq 1 "$n"); do sel="$sel, max(t$i)"; done
	# warm, so the comparison is decode cost rather than first-touch I/O
	psql_run "$FETCH_PATH_SETS
		SELECT $sel FROM fc_w359 WHERE h='h7';" >/dev/null 2>&1
	start=$(date +%s%N)
	psql_run "$FETCH_PATH_SETS
		SELECT $sel FROM fc_w359 WHERE h='h7';" >/dev/null 2>&1
	end=$(date +%s%N); echo $(( (end - start) / 1000000 ))
}

for _n in 4 5; do
	_sel="count(*)"; for _i in $(seq 1 "$_n"); do _sel="$_sel, max(t$_i)"; done
	check "the $_n-column projection really drives the fetch path (#797)" \
		"$(pgc_uses_row_fetch "$FETCH_PATH_SETS" \
			"SELECT $_sel FROM fc_w359 WHERE h='h7'")" yes
done
under="$(min3 w359_ms 4)"   # ~30 MB decoded: fits
over="$(min3 w359_ms 5)"    # ~38 MB decoded: does not
echo "-- #359 projection width: four columns ${under} ms, five columns ${over} ms"
check_ratio "crossing the fetch cache cap costs proportionally, not totally (#359)" \
	"$over" "$under" "12"

# The columns that overflow are re-decoded rather than skipped, so they must still
# read correctly -- a cache that quietly returned nulls for them would be fast and
# wrong, and the timing check alone would not notice.
check "the over-cap projection returns the same values as the under-cap one (#359)" \
	"$(q "SELECT max(t1) || '|' || max(t4) FROM fc_w359 WHERE h='h7'")" \
	"$(q "SELECT max(t1) || '|' || max(t4) FROM fc_w359 WHERE h='h7' AND t5 IS NOT NULL")"
check "every over-cap column reads back its written value (#359)" \
	"$(q "SELECT count(*) FROM fc_w359 WHERE h='h7'
	      AND t1 = repeat('a',150) AND t2 = repeat('b',150) AND t3 = repeat('c',150)
	      AND t4 = repeat('d',150) AND t5 = repeat('e',150)")" \
	"$(q "SELECT count(*) FROM fc_w359 WHERE h='h7'")"

pgc_summary

#!/usr/bin/env bash
#
# pgColumnar: gate phase-2 decode gating on PAYLOAD WIDTH (#595).
#
# #452 phase 2 evaluates an unprunable qual (a leading-wildcard LIKE) per
# 1024-row vector between the two decode passes, so a vector no row can pass
# skips its PAYLOAD columns' decode. That per-vector evaluation costs the same
# whatever the projection, but its only payoff is the payload it spares -- the
# projected columns that are NOT the qual's own. On a WIDE SELECT * there is a
# lot to spare and gating wins (ClickBench q24: 6098->3395 ms). On a NARROW
# aggregate (a count, a one-column sum) there is almost nothing to spare and the
# evaluation is pure cost -- eight ClickBench queries regressed 1.2-2x.
#
# So #595 gates the gate on width: it runs only when the number of projected
# non-qual columns is at least pgcolumnar.qual_skipvec_min_payload_cols (default
# 20). This suite pins that policy -- NOT the machinery, which
# native_decode_gating.sh covers -- so its observable is not "does the mask drop
# the right rows" but "did the per-vector gating RUN AT ALL for this projection".
#
# The observable is the machinery suite's own primary one: "Columnar Vector
# Decodes", which counts (vector, column) decodes.
#   - gating RAN     -> the payload columns are NOT decoded on skipped vectors,
#                       so the count drops toward the qual column alone;
#   - gating did NOT -> every projected column is decoded for every vector, so
#                       the count is exactly VECTORS * (projected columns).
# The difference between those two numbers is the whole signal, and both are
# present and countable in the plan (the machinery suite pins the counter's unit),
# so a "did not gate" arm reads a real number rather than an absent line.
#
# The threshold is exercised by MOVING it across a fixed projection width rather
# than by changing the table: the same wide SELECT * gates at the default and
# stops gating once the GUC is raised above its width, and the same narrow
# projection does not gate at the default but does once the GUC is set to 0.
# The GUC is set per DATABASE (ALTER DATABASE ... SET) because every psql helper
# opens a fresh connection; a session SET would not survive to the next query.
#
# Usage:  test/native_decode_gate_width.sh [PG_CONFIG]
# Written fresh for pgColumnar; it reuses no upstream test file.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=32768
VECTORS=32				# ROWS / 1024
PCOLS=24				# payload columns; SELECT * is 24 >= the default 20 -> WIDE
NCOLS_WIDE=$(( PCOLS + 1 ))		# tag plus the payload columns
NARROW_NCOLS=3				# the narrow projection: tag, p1, p2 (payload = 2)
SURVIVING_VECTORS=1			# the needle lives only in the first vector
DEFAULT_GUC=20				# the shipped pgcolumnar.qual_skipvec_min_payload_cols

# Build the wide payload column list programmatically: p1 int, ..., p24 int, and
# the matching value list g + 0, ..., g + 23. A wide fixture is the point -- the
# projection width is what the gate reads.
COLDEFS=""; COLVALS=""
for i in $(seq 1 "$PCOLS"); do
	sep=""; [ "$i" -gt 1 ] && sep=", "
	COLDEFS="$COLDEFS$sep p$i int"
	COLVALS="$COLVALS$sep g + $(( i - 1 ))"
done

psql_run "CREATE TABLE hw (tag text, $COLDEFS);"
psql_run "CREATE TABLE nw (tag text, $COLDEFS) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('nw', stripe_row_limit => 65536, chunk_group_row_limit => 1024);"

# Every tag begins 'row' so LIKE '%row%' matches all rows (matches-everything
# control). Only the first vector's 1024 rows also contain 'needle', so
# LIKE '%needle%' survives in exactly one vector; LIKE '%zzzzz%' survives none.
# The payload columns are the monotone integers phase 2 needs for per-vector (D4)
# structure. The heap mirror carries identical data and is the correctness oracle.
GEN="SELECT 'row' || CASE WHEN g <= 1024 THEN 'needle' ELSE 'miss' END,
	 $COLVALS FROM generate_series(1, $ROWS) g"
psql_run "INSERT INTO hw $GEN;"
psql_run "INSERT INTO nw $GEN;"

explain_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null
}
counter_in() { grep -F "$2" <<<"$1" | grep -oE '[0-9]+' | head -1; }
has_line()   { grep -qF "$2" <<<"$1" && echo yes || echo no; }

# Set the width gate for the DATABASE (survives to the next fresh connection) and
# report the value a new connection actually sees, so a failed ALTER is caught as
# a wrong premise rather than misattributed to the feature.
set_gate()  { psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.qual_skipvec_min_payload_cols = $1;" >/dev/null; }
reset_gate(){ psql_run "ALTER DATABASE $PGC_DB RESET pgcolumnar.qual_skipvec_min_payload_cols;" >/dev/null; }
gate_now()  { q "SHOW pgcolumnar.qual_skipvec_min_payload_cols;"; }

# The three infix-LIKE shapes: all unprunable, differing only in how many vectors
# hold a survivor. Projections: WIDE is SELECT * (24 payload cols, at/above the
# default); NARROW is three columns (2 payload cols, below it).
WIDE_NOMATCH="SELECT * FROM nw WHERE tag LIKE '%zzzzz%'"
WIDE_NEEDLE="SELECT * FROM nw WHERE tag LIKE '%needle%'"
WIDE_CONTROL="SELECT * FROM nw WHERE tag LIKE '%row%'"
NARROW_NOMATCH="SELECT tag, p1, p2 FROM nw WHERE tag LIKE '%zzzzz%'"
NARROW_CONTROL="SELECT tag, p1, p2 FROM nw WHERE tag LIKE '%row%'"

# ---- premises: the GUC landed, and the fixture is the shape the gate reads ----

# SHOW erroring (GUC undefined) -> q is empty -> this fails: it is also the guard
# that the extern + DefineCustomIntVariable actually shipped.
check "premise: the width-gate GUC exists and defaults to the shipped $DEFAULT_GUC" \
	"$(gate_now)" "$DEFAULT_GUC"
# The rejected selectivity-probe idea's two GUCs must NOT have shipped. SHOW of an
# unknown parameter errors; q swallows the error and returns empty, so an empty
# result is the proof the parameter does not exist. The paired positive premise
# above (min_payload_cols == 20) rules out an empty result from a dead connection.
# By SQLSTATE rather than by an empty result (#1341): q returns empty for a dead
# connection as well, and an empty expectation is the shape the pytest layer refuses.
show_state() {	# show_state PARAM -> the SQLSTATE SHOW raised, or "shown"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qAtX -v VERBOSITY=verbose -c "SHOW $1;" >/dev/null 2>"$PGC_WORKDIR/show.err" && { echo shown; return; }
	sed -nE 's/^ERROR:  ([0-9A-Z]{5}): .*/\1/p' "$PGC_WORKDIR/show.err" | head -1
}
check "premise: the rejected probe GUC qual_skipvec_probe_vecs was NOT shipped" \
	"$(show_state pgcolumnar.qual_skipvec_probe_vecs)" "42704"
check "premise: the rejected probe GUC qual_skipvec_min_skip_pct was NOT shipped" \
	"$(show_state pgcolumnar.qual_skipvec_min_skip_pct)" "42704"
check "premise: one row group, so every count below is one group's" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('nw');")" \
	"1"

PLAN_WIDE_NEEDLE="$(explain_of "$WIDE_NEEDLE")"
# The late-materialization counter is asserted on plans that HAVE matches (like
# the sibling suite's needle plan); a zero-match plan may not print it. So the
# narrow late-mat premise uses a narrow NEEDLE plan, while the narrow projection
# and observable premises use the narrow NOMATCH plan the arms below measure.
PLAN_NARROW_NEEDLE="$(explain_of "SELECT tag, p1, p2 FROM nw WHERE tag LIKE '%needle%'")"
PLAN_NARROW_NOMATCH="$(explain_of "$NARROW_NOMATCH")"

check "premise: the wide plan is a columnar custom scan with projection" \
	"$(has_line "$PLAN_WIDE_NEEDLE" 'Columnar Projected Columns')" "yes"
check "premise: the narrow plan is a columnar custom scan with projection" \
	"$(has_line "$PLAN_NARROW_NOMATCH" 'Columnar Projected Columns')" "yes"
check "premise: the LIKE is unprunable (no usable skip predicate)" \
	"$(counter_in "$PLAN_WIDE_NEEDLE" 'Columnar Usable Skip Predicates')" "0"
check "premise: no whole row group is pruned" \
	"$(counter_in "$PLAN_WIDE_NEEDLE" 'Columnar Chunk Groups Removed by Filter')" "0"
check "premise: no vector is ruled out by VALUE (that is 1b-ii's path, not phase 2)" \
	"$(counter_in "$PLAN_WIDE_NEEDLE" 'Columnar Vectors Ruled Out by Value')" "0"
# Phase 2 rides on 1a's callback: both projections must take the
# late-materialization path or no decode count below measures phase 2.
check "premise: the wide query takes the late-materialization path" \
	"$(has_line "$PLAN_WIDE_NEEDLE" 'Columnar Rows Filtered Before Materialization')" "yes"
check "premise: the narrow query takes the late-materialization path" \
	"$(has_line "$PLAN_NARROW_NEEDLE" 'Columnar Rows Filtered Before Materialization')" "yes"
check "premise: EXPLAIN reports Columnar Vector Decodes (the observable)" \
	"$(has_line "$PLAN_NARROW_NOMATCH" 'Columnar Vector Decodes')" "yes"

check "premise: the needle matches exactly one vector's worth of rows" \
	"$(q "SELECT count(*) FROM nw WHERE tag LIKE '%needle%';")" "1024"
check "premise: the nomatch predicate matches no row" \
	"$(q "SELECT count(*) FROM nw WHERE tag LIKE '%zzzzz%';")" "0"
check "premise: the control matches every row" \
	"$(q "SELECT count(*) FROM nw WHERE tag LIKE '%row%';")" "$ROWS"

# ---- ARM 1: at the shipped default, WIDE gates and NARROW does not -----------
#
# This is the corrected acceptance. WIDE (24 payload cols >= 20) gates: a
# no-match vector skips all 24 payload columns, so a zero-matching scan decodes
# the qual column alone (VECTORS) instead of every column of every vector. NARROW
# (2 payload cols < 20) does NOT gate: all three projected columns are decoded for
# all 32 vectors (VECTORS * NARROW_NCOLS). The gap between VECTORS and
# VECTORS*NARROW_NCOLS is the whole signal, and it is exactly the payload the gate
# chose to spare or not.

check "premise: still at the default before arm 1" "$(gate_now)" "$DEFAULT_GUC"

PLAN_WIDE_NOMATCH="$(explain_of "$WIDE_NOMATCH")"
check "wide, default: a zero-match LIKE decodes the qual column and NO payload -> gated" \
	"$(counter_in "$PLAN_WIDE_NOMATCH" 'Columnar Vector Decodes')" "$VECTORS"
check "wide, default: and reports the payload vectors skipped" \
	"$(counter_in "$PLAN_WIDE_NOMATCH" 'Columnar Vectors Skipped')" "$VECTORS"

PLAN_WIDE_NEEDLE_D="$(explain_of "$WIDE_NEEDLE")"
check "wide, default: a one-vector LIKE decodes the qual column plus payload for that vector only" \
	"$(counter_in "$PLAN_WIDE_NEEDLE_D" 'Columnar Vector Decodes')" \
	"$(( VECTORS + (NCOLS_WIDE - 1) * SURVIVING_VECTORS ))"
check "wide, default: skips exactly the vectors holding no match" \
	"$(counter_in "$PLAN_WIDE_NEEDLE_D" 'Columnar Vectors Skipped')" \
	"$(( VECTORS - SURVIVING_VECTORS ))"

check "narrow, default: a zero-match LIKE decodes EVERY projected column of every vector -> NOT gated" \
	"$(counter_in "$(explain_of "$NARROW_NOMATCH")" 'Columnar Vector Decodes')" \
	"$(( VECTORS * NARROW_NCOLS ))"

# ---- ARM 2: removal proof for NARROW -- set the GUC to 0 and it DOES gate -----
#
# If narrow's "not gated" above were caused by anything other than the width gate,
# disabling the width gate would not change it. Setting the GUC to 0 (always gate,
# the pre-#595 behaviour) makes the same narrow projection gate: the zero-match
# scan now decodes the qual column alone.

set_gate 0
check "premise: the width gate is now disabled (0)" "$(gate_now)" "0"
check "narrow, gate off (GUC=0): the SAME narrow projection now gates -> qual column alone" \
	"$(counter_in "$(explain_of "$NARROW_NOMATCH")" 'Columnar Vector Decodes')" "$VECTORS"

# matches-everything control, with gating FORCED ON: an unprunable qual that
# matches every row can skip no vector, so nothing is spared even though the gate
# ran. Both projections decode in full.
check "control, gate forced on: a matches-everything wide qual skips no vector (full decode)" \
	"$(counter_in "$(explain_of "$WIDE_CONTROL")" 'Columnar Vector Decodes')" \
	"$(( VECTORS * NCOLS_WIDE ))"
check "control, gate forced on: a matches-everything narrow qual skips no vector (full decode)" \
	"$(counter_in "$(explain_of "$NARROW_CONTROL")" 'Columnar Vector Decodes')" \
	"$(( VECTORS * NARROW_NCOLS ))"

# ---- ARM 3: removal proof for WIDE -- raise the GUC above its width -----------
#
# The wide projection has 24 payload columns. At the threshold (24) it still
# gates; one above (25) it stops. This pins the gate's ">=" operator exactly: a
# fix that used ">" would fail the boundary check, and one that ignored the GUC
# would fail the suppression check.

set_gate "$NCOLS_WIDE"					# 25 > 24 payload cols -> must NOT gate
check "premise: the width threshold is now above the projection width" "$(gate_now)" "$NCOLS_WIDE"
check "wide, gate raised above width: the SAME wide SELECT * stops gating -> full decode" \
	"$(counter_in "$(explain_of "$WIDE_NOMATCH")" 'Columnar Vector Decodes')" \
	"$(( VECTORS * NCOLS_WIDE ))"

set_gate "$PCOLS"					# exactly 24 == 24 payload cols -> gates (>=)
check "premise: the width threshold now equals the projection width" "$(gate_now)" "$PCOLS"
check "wide, gate AT the width (>= boundary): gates again -> qual column alone" \
	"$(counter_in "$(explain_of "$WIDE_NOMATCH")" 'Columnar Vector Decodes')" "$VECTORS"

# ---- correctness: the gate is output-invariant, and columnar matches the heap --
#
# The gate is a decode-time optimisation and must never change a result. Assert
# it against the heap oracle AND against itself with the gate on vs off.

reset_gate
check "premise: back to the shipped default" "$(gate_now)" "$DEFAULT_GUC"

HASH_HEAP_NEEDLE="$(pgc_set_hash "SELECT * FROM hw WHERE tag LIKE '%needle%'")"
HASH_COL_NEEDLE_GATED="$(pgc_set_hash "$WIDE_NEEDLE")"
check "correctness: wide needle, gate ON, matches the heap oracle" \
	"$HASH_COL_NEEDLE_GATED" "$HASH_HEAP_NEEDLE"

set_gate "$NCOLS_WIDE"					# gate OFF for the wide projection
check "premise: gate suppressed for the invariance arm" "$(gate_now)" "$NCOLS_WIDE"
check "correctness: wide needle, gate OFF, returns the identical rows (output-invariant)" \
	"$(pgc_set_hash "$WIDE_NEEDLE")" "$HASH_COL_NEEDLE_GATED"

check "correctness: narrow needle matches the heap either way" \
	"$(pgc_set_hash "SELECT tag, p1, p2 FROM nw WHERE tag LIKE '%needle%'")" \
	"$(pgc_set_hash "SELECT tag, p1, p2 FROM hw WHERE tag LIKE '%needle%'")"
check "correctness: the full scan matches the heap" \
	"$(pgc_set_hash "SELECT * FROM nw")" \
	"$(pgc_set_hash "SELECT * FROM hw")"

reset_gate
pgc_summary

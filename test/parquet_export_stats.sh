#!/usr/bin/env bash
#
# Statistics in the files WE export (#850).
#
# pgcolumnar.export_parquet() wrote no per-row-group statistics, so a file we
# exported could never drive the Parquet FDW's row-group skipping: every
# documented condition in docs/limitations.md's "Row-group skipping" section
# could hold and "Row Groups Skipped" would still be 0, because the reader had
# no min/max to test. The native chunk-group skip on the same data was, and is,
# unaffected -- the fault was entirely on the write side.
#
# What this suite pins, in the bytes rather than in a reader's opinion of them:
#   * every column chunk of a skippable physical type carries min_value and
#     max_value, and the values are CORRECT per row group, not merely present;
#   * null_count is written always, and counts nulls -- including values that
#     have no Parquet representation and are folded to null (date and timestamp
#     infinity), which must not reach a bound;
#   * the float rules parquet.thrift states for TYPE_ORDER: NaN excluded from
#     the bounds, nan_count always written, no bounds at all when every non-null
#     value is NaN, a zero maximum written as +0.0 and a zero minimum as -0.0;
#   * FileMetaData.column_orders, which the spec makes mandatory once
#     min_value/max_value are written and without which Arrow discards them;
#   * and the payoff: the FDW skips row groups on a file we exported, and the
#     rows it returns still match an oracle exactly.
#
# The instrument is test/parquet_stats.py, which reads the footer with no
# Parquet library. That is deliberate. A suite whose instrument is an optional
# import reports SKIP when the import is missing, and a skip is not coverage;
# and column_orders is invisible through pyarrow's statistics API, so a
# third-party reader cannot tell us whether we wrote it.
#
# Usage:  test/parquet_export_stats.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

STATS_PY="$(dirname "${BASH_SOURCE[0]}")/parquet_stats.py"
PARQ="$PGC_WORKDIR/export_stats.parquet"
S="$PGC_WORKDIR/stats.txt"

# 196609 = 3*65536 + 1, so the export writes 4 row groups and the last holds a
# single row. The one-row group is the min==max edge and costs nothing.
ROWS=196609
RGSIZE=65536
NGROUPS=4

COLS="id int4, i2 int2, i8 int8, f4 float4, d date, ts timestamp,
      nul int4, alln int4, t text, b bool"

# ---- fixture ---------------------------------------------------------------
#
# Every column earns its place:
#   id    ascending, so the four groups hold disjoint ranges: without that a
#         correct writer and a writer that stamps one file-wide min/max onto
#         every chunk are indistinguishable.
#   i2    a PG int2 stored as a Parquet INT32, so a statistic written at the
#         PostgreSQL width rather than the physical one is visible.
#   f4    exact halves, so the float text comparison is not a rounding argument.
#   d,ts  ascending AND carrying infinities, which have no Parquet
#         representation and are folded to null: they must not reach a bound.
#   nul   nulls, with every non-null value above 1000000, so a null admitted
#         into the accumulator as 0 lands outside the true range.
#   alln  all null: the column that must carry no bounds at all.
#   t,b   a BYTE_ARRAY and a BOOLEAN, whose bounds this change does not write.
psql_run "CREATE EXTENSION IF NOT EXISTS pgcolumnar;"
psql_run "CREATE TABLE es_h ($COLS);"
psql_run "INSERT INTO es_h
          SELECT g - 1,
                 (g % 20000) + 1,
                 g::bigint * 1000000,
                 g * 0.5,
                 CASE WHEN g % $RGSIZE = 3 THEN 'infinity'::date
                      WHEN g % $RGSIZE = 4 THEN '-infinity'::date
                      ELSE DATE '2000-01-01' + (g / 8) END,
                 CASE WHEN g % $RGSIZE = 5 THEN 'infinity'::timestamp
                      ELSE TIMESTAMP '2000-01-01' + (g || ' sec')::interval END,
                 CASE WHEN g % 5 = 0 THEN NULL ELSE g + 1000000 END,
                 NULL,
                 'r' || g,
                 (g % 2 = 0)
          FROM generate_series(1, $ROWS) g;"
psql_run "CREATE TABLE es_c ($COLS) USING pgcolumnar;"
psql_run "INSERT INTO es_c SELECT * FROM es_h;"
psql_run "SELECT pgcolumnar.export_parquet('es_c', '$PARQ');"

# A parser failure is not its own check, and does not end the run: it recorded one
# name and skipped the 162 below, so a lost footer read as one red. Every check
# below reads an empty value from a failed parse and refuses it by name.
if ! python3 "$STATS_PY" "$PARQ" > "$S" 2>"$PGC_WORKDIR/stats.err"; then
	echo "-- the footer parser could not read the exported file:"
	sed 's/^/      /' "$PGC_WORKDIR/stats.err"
fi

# ---- accessors over the parser's output ------------------------------------

fileval() {  # fileval KEY
	awk -v k="$1=" '$1=="FILE"{for(i=2;i<=NF;i++) if(index($i,k)==1)
		{print substr($i,length(k)+1); exit}}' "$S"
}
leafof() {  # leafof PATH -> the leaf index carrying that column
	awk -v p="path=$1" '$1=="CHUNK" && $0 ~ ("(^| )" p "( |$)"){
		for(i=2;i<=NF;i++) if(index($i,"leaf=")==1)
			{print substr($i,6); exit}}' "$S"
}
cf() {  # cf RG LEAF KEY -> that chunk's value for KEY
	awk -v rg="rg=$1" -v lf="leaf=$2" -v k="$3=" '
		$1=="CHUNK" && $2==rg && $3==lf {
			for(i=4;i<=NF;i++) if(index($i,k)==1)
				{print substr($i,length(k)+1); exit}}' "$S"
}
# Count CHUNK lines whose KEY equals VAL, restricted to a set of leaf indexes.
countchunks() {  # countchunks "LEAFSET" KEY VAL
	awk -v leaves="$1" -v k="$2=" -v want="$3" '
		BEGIN{n=split(leaves,a," "); for(i=1;i<=n;i++) keep[a[i]]=1}
		$1=="CHUNK" {
			lf=""; v="";
			for(i=2;i<=NF;i++){
				if(index($i,"leaf=")==1) lf=substr($i,6);
				if(index($i,k)==1) v=substr($i,length(k)+1);
			}
			if(lf in keep && v==want) c++
		} END{print c+0}' "$S"
}
# How many CHUNK lines a leaf set selects. A "no chunk carries X" count reads 0
# for a leaf set that selects nothing -- an unresolved leaf, a parser that
# printed no chunks -- so those checks report the count beside this one, and
# "0 of 0 chunks" is a red rather than a pass.
none_of() {  # none_of "LEAFSET" KEY VAL -> "N of M chunks"
	echo "$(countchunks "$1" "$2" "$3") of $(awk -v leaves="$1" '
		BEGIN{n=split(leaves,a," "); for(i=1;i<=n;i++) keep[a[i]]=1}
		$1=="CHUNK"{for(i=2;i<=NF;i++)
			if(index($i,"leaf=")==1 && (substr($i,6) in keep)) c++}
		END{print c+0}' "$S") chunks"
}

# Leaf indexes, resolved from the file rather than assumed from column order.
L_ID="$(leafof id)";   L_I2="$(leafof i2)";  L_I8="$(leafof i8)"
L_F4="$(leafof f4)";   L_D="$(leafof d)";    L_TS="$(leafof ts)"
L_NUL="$(leafof nul)"; L_ALLN="$(leafof alln)"
L_T="$(leafof t)";     L_B="$(leafof b)"
# The seven skippable-type columns that hold at least one value, and alln,
# which is skippable by type and holds none.
SKIPPABLE="$L_ID $L_I2 $L_I8 $L_F4 $L_D $L_TS $L_NUL"
SKIPTYPE="$SKIPPABLE $L_ALLN"

# ---- premises: the instrument reached the data ------------------------------
#
# Every red below is ambiguous without these. They separate "no statistics were
# written" from "the parser stopped early", "we measured someone else's file",
# and "the exported data is wrong".

check "premise: leaf indexes resolved for all ten columns" \
	"$(printf '%s\n' "$L_ID" "$L_I2" "$L_I8" "$L_F4" "$L_D" "$L_TS" \
		"$L_NUL" "$L_ALLN" "$L_T" "$L_B" | grep -c '^[0-9]\+$')" "10"
check "premise: the file under test is one we wrote" "$(fileval created_by)" "pgColumnar"
check_num "premise: the footer declares 4 row groups" \
	"$(fileval row_groups)" "$NGROUPS"
check_num "premise: the footer declares 10 leaf columns" "$(fileval leaves)" "10"
check_num "premise: the parser reaches ColumnMetaData (num_values sums to the table)" \
	"$(awk -v lf="leaf=$L_ID" '$1=="CHUNK" && $3==lf {
		for(i=4;i<=NF;i++) if(index($i,"num_values=")==1) s+=substr($i,12)
	} END{print s+0}' "$S")" \
	"$(q "SELECT count(*) FROM es_c")"

# The oracle. read_parquet decodes DATA PAGES -- a different code path from the
# footer under test -- so a bound is compared against the values themselves.
# Materialized once: re-decoding the file per check would be slow enough to
# discourage checking.
psql_run "CREATE TABLE oracle AS
          SELECT (row_number() OVER () - 1) / $RGSIZE AS grp, *
          FROM pgcolumnar.read_parquet('$PARQ') AS t($COLS);"
# The bucket mapping is what every per-group value check rests on. These two
# assert it against the fixture's ascending id rather than assuming that
# read_parquet returned the file in order.
#
# These four are not premises (#1339, 2026-10-21): with date and timestamp
# infinities no longer folded to null, the export writes values read_parquet
# refuses, and all four go red on a one-line change to the writer.
check "the oracle's buckets are the file's row groups" \
	"$(q "SELECT string_agg(c::text, ',' ORDER BY grp)
	      FROM (SELECT grp, count(*) c FROM oracle GROUP BY grp) s")" \
	"$RGSIZE,$RGSIZE,$RGSIZE,1"
check "each bucket holds its own contiguous id range" \
	"$(q "SELECT bool_and(mn = grp * $RGSIZE AND mx = grp * $RGSIZE + c - 1)
	      FROM (SELECT grp, min(id) mn, max(id) mx, count(*) c
	            FROM oracle GROUP BY grp) s")" "t"
check_num "the oracle holds every exported row" \
	"$(q "SELECT count(*) FROM oracle")" "$ROWS"
check "the exported data equals the heap on the columns nothing folds" \
	"$(pgc_set_hash "SELECT id,i2,i8,f4,nul,alln,t,b FROM oracle")" \
	"$(pgc_set_hash "SELECT id,i2,i8,f4,nul,alln,t,b FROM es_h")"

# ---- (a) presence, with the buckets accounting for every chunk -------------

BOTH="$(countchunks "$SKIPPABLE" has_min 1)"
BOTHMAX="$(countchunks "$SKIPPABLE" has_max 1)"
NEITHER="$(countchunks "$L_ALLN" has_min 0)"
TOTSKIP="$(countchunks "$SKIPTYPE" rg 0)"
ONESIDED=$(( $(countchunks "$SKIPTYPE" has_min 1) + $(countchunks "$SKIPTYPE" has_max 1)
	     - 2 * $(awk -v leaves="$SKIPTYPE" '
		BEGIN{n=split(leaves,a," "); for(i=1;i<=n;i++) keep[a[i]]=1}
		$1=="CHUNK"{lf="";mn="";mx="";
			for(i=2;i<=NF;i++){
				if(index($i,"leaf=")==1) lf=substr($i,6);
				if(index($i,"has_min=")==1) mn=substr($i,9);
				if(index($i,"has_max=")==1) mx=substr($i,9);
			}
			if((lf in keep) && mn=="1" && mx=="1") c++
		} END{print c+0}' "$S") ))
echo "-- chunks of a skippable physical type: $(( NGROUPS * 8 )) expected;" \
     "with both bounds $BOTH, with neither $NEITHER, one-sided $ONESIDED"
check_num "every value-bearing skippable chunk carries a minimum" \
	"$BOTH" "$(( NGROUPS * 7 ))"
check_num "every value-bearing skippable chunk carries a maximum" \
	"$BOTHMAX" "$(( NGROUPS * 7 ))"
check_num "no chunk carries exactly one bound" "$ONESIDED" "0"
check_num "buckets account for every skippable-type chunk" \
	"$(( BOTH + NEITHER + ONESIDED ))" "$(( NGROUPS * 8 ))"
check "the all-null column carries no bounds" \
	"$(none_of "$L_ALLN" has_min 1)" "0 of $NGROUPS chunks"

# A statistic must be written at the PHYSICAL width: a PG int2 is a Parquet
# INT32 and its bound is 4 bytes, not 2. The count of bounds examined is part of
# the reading: a file with no bounds at all has none of the wrong width, and
# read "0" here while every presence check above was red.
check "every bound is its physical width" \
	"$(awk '$1=="CHUNK"{pt="";mnl="";mxl="";
		for(i=2;i<=NF;i++){
			if(index($i,"ptype=")==1) pt=substr($i,7);
			if(index($i,"minlen=")==1) mnl=substr($i,8);
			if(index($i,"maxlen=")==1) mxl=substr($i,8);
		}
		w=0; if(pt=="1"||pt=="4") w=4; else if(pt=="2"||pt=="5") w=8;
		if(w>0){ if(mnl!="-"){ n++; if(mnl+0!=w) bad++ }
		         if(mxl!="-"){ n++; if(mxl+0!=w) bad++ } }
	} END{print bad+0 " of " n+0 " bounds"}' "$S")" "0 of $(( NGROUPS * 7 * 2 )) bounds"

# ---- (b) the values are right, per row group -------------------------------

# A float bound and its oracle are the same number written two ways: the parser
# prints Python's repr (32768.0) and PostgreSQL prints its own text form (32768).
# Comparing them as strings fails on the presentation, so both sides are printed
# as numbers -- while keeping check_num's refusal to compare a measurement nobody
# took, which is the whole reason the value checks are trustworthy: a side that
# is not a number prints as a refusal naming that side, so two empty readings
# differ instead of agreeing. A plain `check` rather than a suite-local check_
# helper, so the name sits at the call site where the grader reads it.
as_float() {  # as_float got|want VALUE -> %.9g, or a refusal naming the side
	if pgc_is_number "$2"; then
		awk -v v="$2" 'BEGIN{printf "%.9g", v+0}'
	else
		printf 'not a measurement, so nothing was compared: %s [%s]' "$1" "$2"
	fi
}

# The oracle for a bound is the HEAP, es_h, built from the generator, grouped by
# the id range each row group holds (the premise above pins that mapping). It is
# not the exported file. A bound and a value decoded from the same file agree
# whenever the export mangles both alike: with 2000-01-01 folded to null, the
# file's min(d) and read_parquet's min(d) were both 2000-01-02, and every check
# here passed over a file that had lost five dates. Date and timestamp
# infinities are folded to null, so the heap's bounds are over its finite values.
# Physical space: days and microseconds since 1970-01-01.
ora() {  # ora COL GRP min|max
	local w="id / $RGSIZE = $2"
	case "$1" in
		d)  q "SELECT ($3(d) FILTER (WHERE isfinite(d)) - DATE '1970-01-01')::text
		       FROM es_h WHERE $w" ;;
		ts) q "SELECT (EXTRACT(EPOCH FROM ($3(ts) FILTER (WHERE isfinite(ts))
		               - TIMESTAMP '1970-01-01')) * 1000000)::bigint::text
		       FROM es_h WHERE $w" ;;
		f4) q "SELECT $3(f4)::float8::text FROM es_h WHERE $w" ;;
		*)  q "SELECT $3($1)::text FROM es_h WHERE $w" ;;
	esac
}
# f4's bound is a float on one side and PostgreSQL's float text on the other, so
# it is compared through as_float; every other column here is an integer in
# physical space.
check_num "group 0 min(id) equals the oracle" \
	"$(cf 0 "$L_ID" min)" "$(ora id 0 min)"
check_num "group 0 max(id) equals the oracle" \
	"$(cf 0 "$L_ID" max)" "$(ora id 0 max)"
check_num "group 1 min(id) equals the oracle" \
	"$(cf 1 "$L_ID" min)" "$(ora id 1 min)"
check_num "group 1 max(id) equals the oracle" \
	"$(cf 1 "$L_ID" max)" "$(ora id 1 max)"
check_num "group 2 min(id) equals the oracle" \
	"$(cf 2 "$L_ID" min)" "$(ora id 2 min)"
check_num "group 2 max(id) equals the oracle" \
	"$(cf 2 "$L_ID" max)" "$(ora id 2 max)"
check_num "group 3 min(id) equals the oracle" \
	"$(cf 3 "$L_ID" min)" "$(ora id 3 min)"
check_num "group 3 max(id) equals the oracle" \
	"$(cf 3 "$L_ID" max)" "$(ora id 3 max)"
check_num "group 0 min(i2) equals the oracle" \
	"$(cf 0 "$L_I2" min)" "$(ora i2 0 min)"
check_num "group 0 max(i2) equals the oracle" \
	"$(cf 0 "$L_I2" max)" "$(ora i2 0 max)"
check_num "group 1 min(i2) equals the oracle" \
	"$(cf 1 "$L_I2" min)" "$(ora i2 1 min)"
check_num "group 1 max(i2) equals the oracle" \
	"$(cf 1 "$L_I2" max)" "$(ora i2 1 max)"
check_num "group 2 min(i2) equals the oracle" \
	"$(cf 2 "$L_I2" min)" "$(ora i2 2 min)"
check_num "group 2 max(i2) equals the oracle" \
	"$(cf 2 "$L_I2" max)" "$(ora i2 2 max)"
check_num "group 3 min(i2) equals the oracle" \
	"$(cf 3 "$L_I2" min)" "$(ora i2 3 min)"
check_num "group 3 max(i2) equals the oracle" \
	"$(cf 3 "$L_I2" max)" "$(ora i2 3 max)"
check_num "group 0 min(i8) equals the oracle" \
	"$(cf 0 "$L_I8" min)" "$(ora i8 0 min)"
check_num "group 0 max(i8) equals the oracle" \
	"$(cf 0 "$L_I8" max)" "$(ora i8 0 max)"
check_num "group 1 min(i8) equals the oracle" \
	"$(cf 1 "$L_I8" min)" "$(ora i8 1 min)"
check_num "group 1 max(i8) equals the oracle" \
	"$(cf 1 "$L_I8" max)" "$(ora i8 1 max)"
check_num "group 2 min(i8) equals the oracle" \
	"$(cf 2 "$L_I8" min)" "$(ora i8 2 min)"
check_num "group 2 max(i8) equals the oracle" \
	"$(cf 2 "$L_I8" max)" "$(ora i8 2 max)"
check_num "group 3 min(i8) equals the oracle" \
	"$(cf 3 "$L_I8" min)" "$(ora i8 3 min)"
check_num "group 3 max(i8) equals the oracle" \
	"$(cf 3 "$L_I8" max)" "$(ora i8 3 max)"
check "group 0 min(f4) equals the oracle" \
	"$(as_float got "$(cf 0 "$L_F4" min)")" "$(as_float want "$(ora f4 0 min)")"
check "group 0 max(f4) equals the oracle" \
	"$(as_float got "$(cf 0 "$L_F4" max)")" "$(as_float want "$(ora f4 0 max)")"
check "group 1 min(f4) equals the oracle" \
	"$(as_float got "$(cf 1 "$L_F4" min)")" "$(as_float want "$(ora f4 1 min)")"
check "group 1 max(f4) equals the oracle" \
	"$(as_float got "$(cf 1 "$L_F4" max)")" "$(as_float want "$(ora f4 1 max)")"
check "group 2 min(f4) equals the oracle" \
	"$(as_float got "$(cf 2 "$L_F4" min)")" "$(as_float want "$(ora f4 2 min)")"
check "group 2 max(f4) equals the oracle" \
	"$(as_float got "$(cf 2 "$L_F4" max)")" "$(as_float want "$(ora f4 2 max)")"
check "group 3 min(f4) equals the oracle" \
	"$(as_float got "$(cf 3 "$L_F4" min)")" "$(as_float want "$(ora f4 3 min)")"
check "group 3 max(f4) equals the oracle" \
	"$(as_float got "$(cf 3 "$L_F4" max)")" "$(as_float want "$(ora f4 3 max)")"
check_num "group 0 min(d) equals the oracle" \
	"$(cf 0 "$L_D" min)" "$(ora d 0 min)"
check_num "group 0 max(d) equals the oracle" \
	"$(cf 0 "$L_D" max)" "$(ora d 0 max)"
check_num "group 1 min(d) equals the oracle" \
	"$(cf 1 "$L_D" min)" "$(ora d 1 min)"
check_num "group 1 max(d) equals the oracle" \
	"$(cf 1 "$L_D" max)" "$(ora d 1 max)"
check_num "group 2 min(d) equals the oracle" \
	"$(cf 2 "$L_D" min)" "$(ora d 2 min)"
check_num "group 2 max(d) equals the oracle" \
	"$(cf 2 "$L_D" max)" "$(ora d 2 max)"
check_num "group 3 min(d) equals the oracle" \
	"$(cf 3 "$L_D" min)" "$(ora d 3 min)"
check_num "group 3 max(d) equals the oracle" \
	"$(cf 3 "$L_D" max)" "$(ora d 3 max)"
check_num "group 0 min(ts) equals the oracle" \
	"$(cf 0 "$L_TS" min)" "$(ora ts 0 min)"
check_num "group 0 max(ts) equals the oracle" \
	"$(cf 0 "$L_TS" max)" "$(ora ts 0 max)"
check_num "group 1 min(ts) equals the oracle" \
	"$(cf 1 "$L_TS" min)" "$(ora ts 1 min)"
check_num "group 1 max(ts) equals the oracle" \
	"$(cf 1 "$L_TS" max)" "$(ora ts 1 max)"
check_num "group 2 min(ts) equals the oracle" \
	"$(cf 2 "$L_TS" min)" "$(ora ts 2 min)"
check_num "group 2 max(ts) equals the oracle" \
	"$(cf 2 "$L_TS" max)" "$(ora ts 2 max)"
check_num "group 3 min(ts) equals the oracle" \
	"$(cf 3 "$L_TS" min)" "$(ora ts 3 min)"
check_num "group 3 max(ts) equals the oracle" \
	"$(cf 3 "$L_TS" max)" "$(ora ts 3 max)"
check_num "group 0 min(nul) equals the oracle" \
	"$(cf 0 "$L_NUL" min)" "$(ora nul 0 min)"
check_num "group 0 max(nul) equals the oracle" \
	"$(cf 0 "$L_NUL" max)" "$(ora nul 0 max)"
check_num "group 1 min(nul) equals the oracle" \
	"$(cf 1 "$L_NUL" min)" "$(ora nul 1 min)"
check_num "group 1 max(nul) equals the oracle" \
	"$(cf 1 "$L_NUL" max)" "$(ora nul 1 max)"
check_num "group 2 min(nul) equals the oracle" \
	"$(cf 2 "$L_NUL" min)" "$(ora nul 2 min)"
check_num "group 2 max(nul) equals the oracle" \
	"$(cf 2 "$L_NUL" max)" "$(ora nul 2 max)"
check_num "group 3 min(nul) equals the oracle" \
	"$(cf 3 "$L_NUL" min)" "$(ora nul 3 min)"
check_num "group 3 max(nul) equals the oracle" \
	"$(cf 3 "$L_NUL" max)" "$(ora nul 3 max)"

# A writer that computed one min/max for the whole file would satisfy every
# presence check above and still make skipping impossible.
check_num "the four groups report four distinct id minima" \
	"$(awk -v lf="leaf=$L_ID" '$1=="CHUNK" && $3==lf {
		for(i=4;i<=NF;i++) if(index($i,"min=")==1) print substr($i,5)
	}' "$S" | sort -u | wc -l)" "$NGROUPS"
check "min <= max in every chunk carrying both" \
	"$(awk '$1=="CHUNK"{mn="";mx="";
		for(i=2;i<=NF;i++){
			if(index($i,"min=")==1 && index($i,"minhex=")!=1 && index($i,"minlen=")!=1)
				mn=substr($i,5);
			if(index($i,"max=")==1 && index($i,"maxhex=")!=1 && index($i,"maxlen=")!=1)
				mx=substr($i,5);
		}
		if(mn!="-" && mx!="-" && mn!="" && mx!=""){ n++; if(mn+0>mx+0) bad++ }
	} END{print bad+0 " of " n+0 " chunks"}' "$S")" "0 of $(( NGROUPS * 7 )) chunks"

# ---- (c) null_count --------------------------------------------------------

check "null_count is written for every chunk, including where it is zero" \
	"$(awk '$1=="CHUNK"{c++; for(i=2;i<=NF;i++) if(index($i,"null_count=")==1 &&
		substr($i,12)=="-") n++} END{print n+0 " of " c+0 " chunks"}' "$S")" \
	"0 of $(( NGROUPS * 10 )) chunks"
# The heap again, not the file: a value the export folds to null is counted by
# the file's null_count AND missing from read_parquet's output, so the two agree
# on a fold that should not have happened. A date or timestamp infinity is the
# one fold that should, so the heap counts it with the nulls.
nulls_of() {  # nulls_of COL GRP -> the nulls group GRP must count
	case "$1" in
		d|ts) q "SELECT count(*) FILTER (WHERE $1 IS NULL OR NOT isfinite($1))
		         FROM es_h WHERE id / $RGSIZE = $2" ;;
		*)    q "SELECT count(*) - count($1) FROM es_h WHERE id / $RGSIZE = $2" ;;
	esac
}
check_num "group 0 null_count(nul) equals the oracle" \
	"$(cf 0 "$L_NUL" null_count)" "$(nulls_of nul 0)"
check_num "group 1 null_count(nul) equals the oracle" \
	"$(cf 1 "$L_NUL" null_count)" "$(nulls_of nul 1)"
check_num "group 2 null_count(nul) equals the oracle" \
	"$(cf 2 "$L_NUL" null_count)" "$(nulls_of nul 2)"
check_num "group 3 null_count(nul) equals the oracle" \
	"$(cf 3 "$L_NUL" null_count)" "$(nulls_of nul 3)"
check_num "group 0 null_count(d) equals the oracle" \
	"$(cf 0 "$L_D" null_count)" "$(nulls_of d 0)"
check_num "group 1 null_count(d) equals the oracle" \
	"$(cf 1 "$L_D" null_count)" "$(nulls_of d 1)"
check_num "group 2 null_count(d) equals the oracle" \
	"$(cf 2 "$L_D" null_count)" "$(nulls_of d 2)"
check_num "group 3 null_count(d) equals the oracle" \
	"$(cf 3 "$L_D" null_count)" "$(nulls_of d 3)"
check_num "group 0 null_count(ts) equals the oracle" \
	"$(cf 0 "$L_TS" null_count)" "$(nulls_of ts 0)"
check_num "group 1 null_count(ts) equals the oracle" \
	"$(cf 1 "$L_TS" null_count)" "$(nulls_of ts 1)"
check_num "group 2 null_count(ts) equals the oracle" \
	"$(cf 2 "$L_TS" null_count)" "$(nulls_of ts 2)"
check_num "group 3 null_count(ts) equals the oracle" \
	"$(cf 3 "$L_TS" null_count)" "$(nulls_of ts 3)"
check_num "group 0 null_count(alln) equals the oracle" \
	"$(cf 0 "$L_ALLN" null_count)" "$(nulls_of alln 0)"
check_num "group 1 null_count(alln) equals the oracle" \
	"$(cf 1 "$L_ALLN" null_count)" "$(nulls_of alln 1)"
check_num "group 2 null_count(alln) equals the oracle" \
	"$(cf 2 "$L_ALLN" null_count)" "$(nulls_of alln 2)"
check_num "group 3 null_count(alln) equals the oracle" \
	"$(cf 3 "$L_ALLN" null_count)" "$(nulls_of alln 3)"
check_num "group 0 null_count(id) equals the oracle" \
	"$(cf 0 "$L_ID" null_count)" "$(nulls_of id 0)"
check_num "group 1 null_count(id) equals the oracle" \
	"$(cf 1 "$L_ID" null_count)" "$(nulls_of id 1)"
check_num "group 2 null_count(id) equals the oracle" \
	"$(cf 2 "$L_ID" null_count)" "$(nulls_of id 2)"
check_num "group 3 null_count(id) equals the oracle" \
	"$(cf 3 "$L_ID" null_count)" "$(nulls_of id 3)"

# ---- (d) values with no Parquet representation are folded, not admitted ----

check "premise: the fixture's infinite dates are more than none" \
	"$(q "SELECT count(*) > 0 FROM es_h WHERE d IN ('infinity','-infinity')")" "t"
check "premise: the fixture's infinite timestamps are more than none" \
	"$(q "SELECT count(*) > 0 FROM es_h WHERE ts = 'infinity'")" "t"
# An infinity is INT32_MAX or INT64_MAX in PostgreSQL's space, but a bound is in
# Parquet's: the export adds the 1970 epoch offset, so an infinity admitted into
# a bound arrives as -2147472692 days or -9222425352054775809 microseconds, and a
# test for the PostgreSQL sentinels passed with both in the file. So every date
# and timestamp bound must lie inside the heap's finite range, in physical space,
# and the reading carries how many bounds it examined.
D_RANGE="$(q "SELECT (min(d) - DATE '1970-01-01') || ' ' || (max(d) - DATE '1970-01-01')
             FROM es_h WHERE isfinite(d)")"
TS_RANGE="$(q "SELECT (EXTRACT(EPOCH FROM min(ts) - TIMESTAMP '1970-01-01') * 1000000)::bigint
             || ' ' || (EXTRACT(EPOCH FROM max(ts) - TIMESTAMP '1970-01-01') * 1000000)::bigint
             FROM es_h WHERE isfinite(ts)")"
check "no date or timestamp bound is an infinity sentinel" \
	"$(awk -v ld="leaf=$L_D" -v lt="leaf=$L_TS" -v dr="$D_RANGE" -v tr="$TS_RANGE" '
		BEGIN{ if(split(dr,D," ")!=2 || split(tr,T," ")!=2){print "no heap range"; exit} }
		$1=="CHUNK" && ($3==ld || $3==lt) {
			lo=($3==ld)?D[1]:T[1]; hi=($3==ld)?D[2]:T[2];
			for(i=4;i<=NF;i++){
				if(index($i,"min=")==1 || index($i,"max=")==1){
					v=substr($i,5); if(v=="-") continue;
					n++; if(v+0<lo+0 || v+0>hi+0) bad++
				}
			}
		} END{if(dr!="" && tr!="") print bad+0 " of " n+0 " bounds"}' "$S")" \
	"0 of $(( NGROUPS * 2 * 2 )) bounds"

# ---- (e) the float rules ---------------------------------------------------

# z8's zero is QUOTED. Unquoted, -0.0 is a numeric literal, numeric has no
# negative zero, and the row stored +0.0: the column's measured maximum was
# already +0.0, so "a zero maximum is written as +0.0" passed with the writer's
# normalisation deleted. '-0.0' is parsed by float8in and stays negative.
psql_run "CREATE TABLE nan_h (f8a float8, f8b float8, z8 float8, z9 float8);"
psql_run "INSERT INTO nan_h VALUES
            (1.0,  'NaN',  -1.0,   0.0),
            ('NaN','NaN',  '-0.0', 1.0),
            (-2.0, 'NaN',  -3.0,   2.0),
            (NULL, NULL,   NULL,   NULL);"
psql_run "CREATE TABLE nan_c (LIKE nan_h) USING pgcolumnar;"
psql_run "INSERT INTO nan_c SELECT * FROM nan_h;"
NANQ="$PGC_WORKDIR/nan.parquet"
psql_run "SELECT pgcolumnar.export_parquet('nan_c', '$NANQ');"
NS="$PGC_WORKDIR/nanstats.txt"
# Not gated on the parser's exit status: a gate recorded one failure under its
# own name and none of the ten below, so a run that lost the NaN fixture read as
# one red instead of ten. Each check now reads its own value, and a parser that
# failed leaves them empty, which every check below refuses by name.
if ! python3 "$STATS_PY" "$NANQ" > "$NS" 2>"$PGC_WORKDIR/nanstats.err"; then
	echo "-- the footer parser could not read the NaN fixture:"
	sed 's/^/      /' "$PGC_WORKDIR/nanstats.err"
fi
nf() {  # nf LEAF KEY  (over the NaN file)
	awk -v lf="leaf=$1" -v k="$2=" '$1=="CHUNK" && $3==lf {
		for(i=4;i<=NF;i++) if(index($i,k)==1)
			{print substr($i,length(k)+1); exit}}' "$NS"
}
nleafof() {
	awk -v p="path=$1" '$1=="CHUNK" && $0 ~ ("(^| )" p "( |$)"){
		for(i=2;i<=NF;i++) if(index($i,"leaf=")==1)
			{print substr($i,6); exit}}' "$NS"
}
N_A="$(nleafof f8a)"; N_B="$(nleafof f8b)"
N_Z8="$(nleafof z8)"; N_Z9="$(nleafof z9)"

check_num "a float column holding a NaN still carries both bounds" \
	"$(nf "$N_A" has_min)" "1"
check "min(f8a) is the smallest non-NaN value" \
	"$(as_float got "$(nf "$N_A" min)")" "$(as_float want -2.0)"
check "max(f8a) is the largest non-NaN value" \
	"$(as_float got "$(nf "$N_A" max)")" "$(as_float want 1.0)"
check_num "an all-NaN column carries no minimum" "$(nf "$N_B" has_min)" "0"
check_num "an all-NaN column carries no maximum" "$(nf "$N_B" has_max)" "0"
check "nan_count is written for every float chunk" \
	"$(awk '$1=="CHUNK"{p="";n="";for(i=2;i<=NF;i++){
		if(index($i,"ptname=")==1) p=substr($i,8);
		if(index($i,"nan_count=")==1) n=substr($i,11);
	} if(p=="FLOAT"||p=="DOUBLE"){ c++; if(n=="-") bad++ } }
	END{print bad+0 " of " c+0 " chunks"}' "$NS")" "0 of 4 chunks"
check_num "nan_count counts the NaNs" "$(nf "$N_B" nan_count)" "3"
check "no float bound is a NaN bit pattern" \
	"$(awk '$1=="CHUNK"{mn="";mx="";c++;
		for(i=2;i<=NF;i++){
			if(index($i,"min=")==1 && index($i,"minhex=")!=1 && index($i,"minlen=")!=1)
				mn=substr($i,5);
			if(index($i,"max=")==1 && index($i,"maxhex=")!=1 && index($i,"maxlen=")!=1)
				mx=substr($i,5);
		}
		if(mn=="nan"||mx=="nan"||mn=="-nan"||mx=="-nan") bad++
	} END{print bad+0 " of " c+0 " chunks"}' "$NS")" "0 of 4 chunks"
# parquet.thrift, TYPE_ORDER: a computed max of zero is written +0.0 and a
# computed min of zero is written -0.0, whichever zero was measured.
check "a zero maximum is written as +0.0" \
	"$(nf "$N_Z8" maxhex)" "0000000000000000"
check "a zero minimum is written as -0.0" \
	"$(nf "$N_Z9" minhex)" "0000000000000080"
# ---- (f) column_orders, without which the bounds have no defined meaning ----

check_num "the footer carries one ColumnOrder per leaf column" \
	"$(fileval column_orders)" "10"
check "every ColumnOrder is TYPE_ORDER" "$(fileval order_ids)" "1"

# ---- (g) the types this change does not claim ------------------------------

# Each reads "N of M chunks": M is how many chunks the leaf set selected, so an
# unresolved leaf reads "0 of 0 chunks" and is red, where a bare count read 0.
check "no chunk carries the deprecated minimum (field 2)" \
	"$(none_of "$L_ID $L_I2 $L_I8 $L_F4 $L_D $L_TS $L_NUL $L_ALLN $L_T $L_B" has_dep_min 1)" \
	"0 of $(( NGROUPS * 10 )) chunks"
check "no chunk carries the deprecated maximum (field 1)" \
	"$(none_of "$L_ID $L_I2 $L_I8 $L_F4 $L_D $L_TS $L_NUL $L_ALLN $L_T $L_B" has_dep_max 1)" \
	"0 of $(( NGROUPS * 10 )) chunks"

check "the BYTE_ARRAY column carries no bounds" \
	"$(none_of "$L_T" has_min 1)" "0 of $NGROUPS chunks"
check "the BOOLEAN column carries no bounds" \
	"$(none_of "$L_B" has_min 1)" "0 of $NGROUPS chunks"

# ---- (h) the payoff: the FDW skips on a file we exported -------------------

psql_run "CREATE SERVER es_srv FOREIGN DATA WRAPPER pgcolumnar_parquet;"
psql_run "CREATE FOREIGN TABLE es_ft ($COLS) SERVER es_srv OPTIONS (path '$PARQ');"

explain_val() {  # explain_val LABEL WHERE
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM es_ft $2" \
		| grep "$1:" | grep -oE '[0-9]+' | head -1
}
skipped_for() { explain_val "Row Groups Skipped" "$1"; }

check_num "premise: the FDW reports 4 row groups" \
	"$(explain_val "Row Groups" "WHERE id < 10")" "$NGROUPS"
check_num "an unrestricted scan skips nothing" "$(skipped_for "")" "0"
check_num "a predicate confined to one group skips the other three" \
	"$(skipped_for "WHERE id BETWEEN 70000 AND 71000")" "3"
check_num "a bigint predicate skips" \
	"$(skipped_for "WHERE i8 < 1000000000::bigint")" "3"
check_num "a float predicate skips" "$(skipped_for "WHERE f4 < 1.0::real")" "3"
check_num "a date predicate skips" \
	"$(skipped_for "WHERE d < DATE '2000-01-05'")" "3"
check_num "a timestamp predicate skips" \
	"$(skipped_for "WHERE ts < TIMESTAMP '2000-01-01 00:30:00'")" "3"
# Documented refusals, reachable from our own exported files for the first time.
#
# Each runs beside a predicate that differs in ONE respect and does skip. Without
# that pair the checks are worthless: every group's max(i8) exceeds 5 and every
# group's max(f4) exceeds 1.0, so `i8 > 5` and `f4 >= 1.0` are unprunable on this
# data whether the refusal exists or not, and a check that reads 0 either way is
# not evidence of a refusal.
check_num "control: the same column skips when the constant matches its type" \
	"$(skipped_for "WHERE i8 < 5::bigint")" "$NGROUPS"
check_num "a cross-type constant skips nothing, on the identical predicate" \
	"$(skipped_for "WHERE i8 < 5::int")" "0"
check_num "control: a float < predicate below every bound skips" \
	"$(skipped_for "WHERE f4 < 0.4::real")" "$NGROUPS"
# The reader refuses > and >= on a float column outright, because Parquet
# excludes NaN from the bounds while PostgreSQL sorts NaN above every value. The
# refusal does not depend on this column holding a NaN, and f4 holds none; the
# NaN rules themselves are pinned on the nan_c fixture below.
check_num "a float >= predicate above every bound still skips nothing" \
	"$(skipped_for "WHERE f4 >= 200000.0::real")" "0"

# How the literal is WRITTEN decides the constant's type, and therefore whether
# the exact-type condition above is met at all. Each pair below differs in one
# respect -- a pair of quotes, or the width of the value -- so the arm that reads
# 0 is refusing rather than merely having nothing to prune. docs/limitations.md
# states the rule; these are what make it a measurement.
#
# The i2 arms skip 1 and not $NGROUPS because i2 is deliberately not ascending in
# this fixture: only the last group, the single row whose min equals its max, has
# bounds that exclude 500. One skipped group is still a skip that a cross-type
# constant cannot produce, which is what the pair is for.
check_num "a quoted literal takes a bigint column's type and skips" \
	"$(skipped_for "WHERE i8 < '5'")" "$NGROUPS"
check_num "the same digits unquoted are an integer, and skip nothing" \
	"$(skipped_for "WHERE i8 < 5")" "0"
check_num "digits too wide for an integer are a bigint, and skip" \
	"$(skipped_for "WHERE i8 < 5000000000")" "3"
# A decimal-point literal is a `numeric`, and PostgreSQL resolves
# `bigint < numeric` by casting the COLUMN. A skip count of 0 for that predicate
# would be over-determined -- the matcher rejects a left argument that is not a
# bare Var, AND the exact-type test would reject a numeric constant -- so no
# single mutation could turn it red and it would not be evidence of anything.
# The plan shape is what the documentation actually claims, so assert that, with
# the quoted form beside it asserting the opposite resolution positively.
filter_has() {  # filter_has WHERE PATTERN
	q "EXPLAIN (VERBOSE, COSTS OFF) SELECT count(*) FROM es_ft $1" \
		| grep -i 'filter' | grep -c "$2"
}
check_num "a decimal-point literal casts the column, not the constant" \
	"$(filter_has "WHERE i8 < 5.0" '::numeric')" "1"
check_num "control: the quoted form casts the constant to the column's type" \
	"$(filter_has "WHERE i8 < '5'" '::bigint')" "1"
check_num "a quoted literal takes a real column's type and skips" \
	"$(skipped_for "WHERE f4 < '0.4'")" "$NGROUPS"
check_num "the same literal unquoted is double precision, and skips nothing" \
	"$(skipped_for "WHERE f4 < 0.4")" "0"
check_num "a quoted literal takes a smallint column's type and skips" \
	"$(skipped_for "WHERE i2 < '500'")" "1"
check_num "the same digits unquoted are an integer, and skip nothing (int2)" \
	"$(skipped_for "WHERE i2 < 500")" "0"

# ADORNED is not the same as quoted. A literal that names its own type is that
# type, not `unknown`, so it does not take the column's and the exact-type
# condition applies to it like any other constant. The three arms differ only in
# how one date is written. The third is the form docs/limitations.md briefly
# promised would skip, and the same page's conditions list said the opposite
# twenty lines above; this is which of the two is true.
check_num "an adorned literal of the column's own type skips" \
	"$(skipped_for "WHERE d < DATE '2000-01-05'")" "3"
check_num "the same date unadorned is unknown, takes the column's type and skips" \
	"$(skipped_for "WHERE d < '2000-01-05'")" "3"
check_num "the same date adorned as a timestamp does not skip" \
	"$(skipped_for "WHERE d < TIMESTAMP '2000-01-05 00:00:00'")" "0"
# That last arm is a documented-behaviour guard rather than evidence, and the
# distinction is worth stating rather than leaving for a reader to discover.
# No mutation in this suite's ledger reddens it: with the reader's
# exact-constant-type refusal deleted, the timestamp datum read as a date lands
# outside this fixture's range in the direction that prunes nothing, so the arm
# still reports 0 for a second reason. What IS falsifiable is the typing the
# sentence rests on, and the planner will say it: an adorned literal keeps its
# own type beside a bare Var, while the unadorned form resolves to the column's.
check_num "an adorned literal keeps its own type, not the column's" \
	"$(filter_has "WHERE d < TIMESTAMP '2000-01-05 00:00:00'" '::timestamp')" "1"
check_num "control: the unadorned form resolves to the column's type instead" \
	"$(filter_has "WHERE d < '2000-01-05'" '::date')" "1"

# Width decides which integer type a digit string is, so an `integer` column
# refuses the wide form exactly as a `bigint` column refuses the narrow one.
# Both arms return no rows, which is what makes the pair a measurement of the
# refusal rather than of the data: on identical output one prunes every group
# and the other prunes none.
check_num "control: a digit string that fits an integer prunes every group" \
	"$(skipped_for "WHERE id < -1")" "$NGROUPS"
check_num "digits too wide for an integer are a bigint, so an int4 column refuses" \
	"$(skipped_for "WHERE id < -5000000000")" "0"

# Soundness. A skipped group emits nothing, and the executor's recheck cannot
# recover a row that never arrived, so every predicate above must return exactly
# what the oracle returns.
fdw_vs_oracle() {  # fdw_vs_oracle NAME PRED
	check "$1" \
		"$(pgc_set_hash "SELECT id,i2,i8,f4,d,ts,nul,alln,t,b FROM es_ft WHERE $2")" \
		"$(pgc_set_hash "SELECT id,i2,i8,f4,d,ts,nul,alln,t,b FROM oracle WHERE $2")"
}
fdw_vs_oracle "the FDW result equals the oracle for [id BETWEEN 70000 AND 71000]" \
	"id BETWEEN 70000 AND 71000"
fdw_vs_oracle "the FDW result equals the oracle for [id < 5000]" \
	"id < 5000"
fdw_vs_oracle "the FDW result equals the oracle for [i2 = 777]" \
	"i2 = 777"
fdw_vs_oracle "the FDW result equals the oracle for [i8 < 1000000000::bigint]" \
	"i8 < 1000000000::bigint"
fdw_vs_oracle "the FDW result equals the oracle for [f4 < 1.0::real]" \
	"f4 < 1.0::real"
fdw_vs_oracle "the FDW result equals the oracle for [f4 >= 1.0::real]" \
	"f4 >= 1.0::real"
fdw_vs_oracle "the FDW result equals the oracle for [d < DATE '2000-01-05']" \
	"d < DATE '2000-01-05'"
fdw_vs_oracle "the FDW result equals the oracle for [d > DATE '2060-01-01']" \
	"d > DATE '2060-01-01'"
fdw_vs_oracle "the FDW result equals the oracle for [ts < TIMESTAMP '2000-01-01 00:30:00']" \
	"ts < TIMESTAMP '2000-01-01 00:30:00'"
fdw_vs_oracle "the FDW result equals the oracle for [nul < 1000100]" \
	"nul < 1000100"
fdw_vs_oracle "the FDW result equals the oracle for [i8 < '5']" \
	"i8 < '5'"
fdw_vs_oracle "the FDW result equals the oracle for [i8 < 5000000000]" \
	"i8 < 5000000000"
fdw_vs_oracle "the FDW result equals the oracle for [f4 < '0.4']" \
	"f4 < '0.4'"
fdw_vs_oracle "the FDW result equals the oracle for [i2 < '500']" \
	"i2 < '500'"
fdw_vs_oracle "the FDW result equals the oracle for [i2 < 500]" \
	"i2 < 500"
fdw_vs_oracle "the FDW result equals the oracle for [d < '2000-01-05']" \
	"d < '2000-01-05'"
fdw_vs_oracle "the FDW result equals the oracle for [d < TIMESTAMP '2000-01-05 00:00:00']" \
	"d < TIMESTAMP '2000-01-05 00:00:00'"
fdw_vs_oracle "the FDW result equals the oracle for [id < -1]" \
	"id < -1"
fdw_vs_oracle "the FDW result equals the oracle for [id < -5000000000]" \
	"id < -5000000000"
fdw_vs_oracle "the FDW result equals the oracle for [alln IS NULL]" \
	"alln IS NULL"

# ---- the other direction: the instrument can report a bound that IS there ---
#
# Every absence above is evidence only if this parser is capable of reporting a
# presence. pyarrow writes statistics; if it is here, the same parser must find
# them. Gated in an if rather than a pgc_skip: this arm is a control on the
# instrument, not the coverage the suite exists for.
if python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	PYQ="$PGC_WORKDIR/pyarrow.parquet"
	python3 - "$PYQ" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"id": pa.array(range(200000), pa.int64())}),
               sys.argv[1], row_group_size=50000)
PY
	check_num "control: the parser finds pyarrow's bounds" \
		"$(python3 "$STATS_PY" "$PYQ" | grep -c 'has_min=1')" "4"
	check_num "control: the parser reports the deprecated fields where they exist" \
		"$(python3 "$STATS_PY" "$PYQ" | grep -c 'has_dep_min=1')" "4"
else
	# RECORDED UNDER EACH ARM'S OWN NAME, not as a note (#1185). A bare echo
	# leaves the accounting at `160 passed + 0 skipped` and the verdict PASSED,
	# so a run that lost both controls reads exactly like a run that kept them.
	# These two are the controls -- what is left without them is a suite that
	# cannot fail for the reason it exists.
	check_skip "control: the parser finds pyarrow's bounds" \
		"SKIP  pyarrow absent, so the can-report-presence control did not run" \
		"pyarrow not available"
	check_skip "control: the parser reports the deprecated fields where they exist" \
		"SKIP  pyarrow absent, so the can-report-presence control did not run" \
		"pyarrow not available"
fi

pgc_summary

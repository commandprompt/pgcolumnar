#!/usr/bin/env bash
#
# pgColumnar Iceberg foreign-data wrapper: identity-partition pruning (#388).
# iceberg_scan is a bare SRF that receives no predicate; the FDW gives Iceberg a
# predicate-bearing scan node and prunes whole data files whose identity-
# partition value cannot match a qual, reading the file's partition value out of
# the manifest (already decoded and typed) before opening it. Pruning is only an
# optimization: the proof is that pruned reads return the SAME rows as
# iceberg_scan of the same table with the same predicate, and EXPLAIN shows the
# files were pruned. A predicate on a non-partition column prunes nothing yet
# still returns correct rows.
#
# The committed warehouse/db/events is identity-partitioned by region (eu: 2
# rows, us: 3 rows).
#
# Usage:  test/iceberg_fdw.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WH="$FX/warehouse"
[ -f "$(ls "$WH"/db/events/data/*/*.parquet 2>/dev/null | head -1)" ] \
	|| pgc_skip fixture "iceberg warehouse data files are missing"

DEST="$PGC_WORKDIR/wh"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WH/db" "$DEST/db"
chmod -R u+rwX "$DEST"
MD="$(ls "$DEST"/db/events/metadata/*.metadata.json | sort | tail -1)"

q "CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg" >/dev/null
q "CREATE FOREIGN TABLE ev (id bigint, region text, amount int)
   SERVER ice OPTIONS (metadata_path '$MD')" >/dev/null

# number of "Files Pruned" reported by EXPLAIN ANALYZE for a query
fp() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null \
		| grep -oE 'Files Pruned: [0-9]+' | grep -oE '[0-9]+' | head -1
}

# ---- premise: the foreign table reads the whole table like iceberg_scan ------
check "the FDW reads the whole table (5 rows, no predicate)" \
	"$(q "SELECT id||'|'||region||'|'||amount FROM ev ORDER BY id")" \
	"$(printf '1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50')"
check "no predicate prunes no files" "$(fp "SELECT * FROM ev")" "0"

# ---- SAME ORACLE: a partition predicate returns iceberg_scan's rows ----------
FDW_EU="$(q "SELECT id||'|'||region||'|'||amount FROM ev WHERE region='eu' ORDER BY id")"
SCAN_EU="$(q "SELECT id||'|'||region||'|'||amount
              FROM pgcolumnar.iceberg_scan('$MD') AS t(id bigint, region text, amount int)
              WHERE region='eu' ORDER BY id")"
check "region='eu' returns the eu rows" "$FDW_EU" "$(printf '1|eu|10\n2|eu|20')"
check_text "the FDW matches iceberg_scan under the same predicate (same oracle)" \
	"$FDW_EU" "$SCAN_EU"

# ---- and it PRUNED the us file (EXPLAIN, both arms) --------------------------
check "region='eu' prunes the us file (Files Pruned: 1)" \
	"$(fp "SELECT * FROM ev WHERE region='eu'")" "1"
check "region='us' prunes the eu file (Files Pruned: 1)" \
	"$(fp "SELECT * FROM ev WHERE region='us'")" "1"
check "region='us' returns the three us rows" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev WHERE region='us'")" \
	"3,4,5"

# ---- a value in no partition prunes everything, returns nothing --------------
check "region='zz' prunes both files (Files Pruned: 2)" \
	"$(fp "SELECT * FROM ev WHERE region='zz'")" "2"
check "region='zz' returns no rows" \
	"$(q "SELECT count(*) FROM ev WHERE region='zz'")" "0"

# ---- metrics pruning: a NON-partition int column prunes by file min/max ------
# amount bounds: region=eu file [10,20], region=us file [30,50].
check "amount > 25 prunes the eu file by metrics (Files Pruned: 1)" \
	"$(fp "SELECT * FROM ev WHERE amount > 25")" "1"
check "amount > 25 still returns the right rows" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev WHERE amount > 25")" \
	"3,4,5"
check "amount < 15 prunes the us file by metrics (Files Pruned: 1)" \
	"$(fp "SELECT * FROM ev WHERE amount < 15")" "1"
check "amount < 15 returns only id 1" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev WHERE amount < 15")" "1"
check "amount = 30 prunes the eu file, keeps us (Files Pruned: 1)" \
	"$(fp "SELECT * FROM ev WHERE amount = 30")" "1"
check "amount = 30 returns id 3" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev WHERE amount = 30")" "3"
check "amount = 100 prunes both files by metrics (Files Pruned: 2)" \
	"$(fp "SELECT * FROM ev WHERE amount = 100")" "2"
check "amount = 100 returns no rows" \
	"$(q "SELECT count(*) FROM ev WHERE amount = 100")" "0"
check_text "a metrics-pruned query matches iceberg_scan (same oracle)" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev WHERE amount >= 30")" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$MD') AS t(id bigint, region text, amount int)
	      WHERE amount >= 30")"

# ---- a DATE identity partition must NOT over-prune (the #660 bug) ------------
# the FDW cannot convert a date partition cell, so it must read the file in full
# and let the recheck qual filter -- never NULL-fill and prune, which would drop
# rows. dt=2020-01-01 -> ids 1,2 ; dt=2020-02-01 -> ids 3,4.
if [ -f "$FX/warehouse_datepart/db/byday/metadata/00001-08bb95d5-d5eb-442f-b2f4-11b44457c66a.metadata.json" ]; then
	DDEST="$PGC_WORKDIR/whd"; rm -rf "$DDEST"; mkdir -p "$DDEST"
	cp -r "$FX/warehouse_datepart/db" "$DDEST/db"; chmod -R u+rwX "$DDEST"
	MDD="$(ls "$DDEST"/db/byday/metadata/*.metadata.json | sort | tail -1)"
	q "CREATE FOREIGN TABLE evd (id bigint, dt date, amount int)
	   SERVER ice OPTIONS (metadata_path '$MDD')" >/dev/null
	check "a date-partitioned FDW reads the whole table (4 rows)" \
		"$(q "SELECT count(*) FROM evd")" "4"
	check "dt='2020-01-01' returns its rows, not over-pruned to zero" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evd WHERE dt=DATE '2020-01-01'")" \
		"1,2"
	check_text "the date FDW matches iceberg_scan under the same predicate" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evd WHERE dt=DATE '2020-01-01'")" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
		      FROM pgcolumnar.iceberg_scan('$MDD') AS t(id bigint, dt date, amount int)
		      WHERE dt=DATE '2020-01-01'")"
	check "a date partition prunes nothing in this release (Files Pruned: 0)" \
		"$(fp "SELECT * FROM evd WHERE dt=DATE '2020-01-01'")" "0"
fi

# ---- bucket[N] partition pruning (murmur3 cross-checked vs pyiceberg) --------
# warehouse_bucket is partitioned by bucket[8](id); pyiceberg wrote the buckets
# (1->4, 4->6, 3->3), so a green same-oracle read proves the C murmur3/bucket
# agrees with Iceberg. 5 files (buckets 1,3,4,6,7); WHERE id=k keeps one.
if [ -f "$FX/warehouse_bucket/db/byid/data/id_bucket=4/00000-0-aa63bd5e-94cd-4a44-a529-fde21cc16eab.parquet" ]; then
	BDEST="$PGC_WORKDIR/whb"; rm -rf "$BDEST"; mkdir -p "$BDEST"
	cp -r "$FX/warehouse_bucket/db" "$BDEST/db"; chmod -R u+rwX "$BDEST"
	MDB="$(ls "$BDEST"/db/byid/metadata/*.metadata.json | sort | tail -1)"
	q "CREATE FOREIGN TABLE evb (id bigint, val text, amount int)
	   SERVER ice OPTIONS (metadata_path '$MDB')" >/dev/null
	check "the bucket FDW reads the whole table (8 rows)" \
		"$(q "SELECT count(*) FROM evb")" "8"
	check "id = 1 keeps only its bucket file (Files Pruned: 4)" \
		"$(fp "SELECT * FROM evb WHERE id = 1")" "4"
	check "id = 1 returns row 1" \
		"$(q "SELECT string_agg(id::text||'|'||val, ',' ORDER BY id) FROM evb WHERE id = 1")" \
		"1|v1"
	check "id = 4 keeps only its bucket file (Files Pruned: 4)" \
		"$(fp "SELECT * FROM evb WHERE id = 4")" "4"
	check "id = 4 returns row 4" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evb WHERE id = 4")" "4"
	check_text "a bucket-pruned equality matches iceberg_scan (murmur3 oracle)" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evb WHERE id = 3")" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
		      FROM pgcolumnar.iceberg_scan('$MDB') AS t(id bigint, val text, amount int)
		      WHERE id = 3")"
	check "id = 3 returns row 3" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evb WHERE id = 3")" "3"
	# id=5 (bucket 7): metrics alone keeps bucket-3 [3,7] AND bucket-7 (prunes 3),
	# but bucket pruning drops bucket-3 too (prunes 4). This distinguishes bucket
	# pruning from metrics, so it is the removal-proof target.
	check "id = 5 bucket-prunes past metrics (Files Pruned: 4)" \
		"$(fp "SELECT * FROM evb WHERE id = 5")" "4"
	check "id = 5 returns row 5" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evb WHERE id = 5")" "5"
	# a RANGE predicate cannot bucket-prune (the hash destroys order); metrics
	# pruning still applies to id's own min/max, so 2 all-<=4 bucket files drop.
	check "id > 4 prunes by metrics not bucket, correctly (Files Pruned: 2)" \
		"$(fp "SELECT * FROM evb WHERE id > 4")" "2"
	check "id > 4 still returns the right rows" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evb WHERE id > 4")" \
		"5,6,7,8"
fi

# ---- truncate[W] partition pruning (range, order-preserving) -----------------
# warehouse_trunc is partitioned by truncate[100](amount) with column metrics
# DISABLED, so truncate is the only mechanism (metrics would otherwise subsume
# it). Files: amount_tr=0 (ids 1,2), =100 (ids 3,4), =200 (id 5).
if ls "$FX"/warehouse_trunc/db/bytr/metadata/*.metadata.json >/dev/null 2>&1; then
	TDEST="$PGC_WORKDIR/wht"; rm -rf "$TDEST"; mkdir -p "$TDEST"
	cp -r "$FX/warehouse_trunc/db" "$TDEST/db"; chmod -R u+rwX "$TDEST"
	MDT="$(ls "$TDEST"/db/bytr/metadata/*.metadata.json | sort | tail -1)"
	q "CREATE FOREIGN TABLE evt (id bigint, amount int)
	   SERVER ice OPTIONS (metadata_path '$MDT')" >/dev/null
	check "the truncate FDW reads the whole table (5 rows)" \
		"$(q "SELECT count(*) FROM evt")" "5"
	check "amount < 100 truncate-prunes 2 files" \
		"$(fp "SELECT * FROM evt WHERE amount < 100")" "2"
	check "amount < 100 returns ids 1,2" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evt WHERE amount < 100")" "1,2"
	check "amount = 130 truncate-prunes 2 files" \
		"$(fp "SELECT * FROM evt WHERE amount = 130")" "2"
	check "amount = 130 returns id 3" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evt WHERE amount = 130")" "3"
	check "amount >= 200 truncate-prunes 2 files" \
		"$(fp "SELECT * FROM evt WHERE amount >= 200")" "2"
	check_text "a truncate-pruned query matches iceberg_scan (same oracle)" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evt WHERE amount >= 200")" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
		      FROM pgcolumnar.iceberg_scan('$MDT') AS t(id bigint, amount int)
		      WHERE amount >= 200")"
fi

# ---- day() temporal partition pruning (date, epoch-converted) ----------------
# warehouse_day is partitioned by day(dt) with column metrics disabled, so day()
# is the sole mechanism. The stored cell is Iceberg days (from 1970); the FDW
# converts a PG date const (+10957). A wrong offset would drop the wrong file, so
# the same-oracle read is the epoch cross-check. Dates 2020-01-01/02, 2020-03-15.
if ls "$FX"/warehouse_day/db/byday/metadata/*.metadata.json >/dev/null 2>&1; then
	YDEST="$PGC_WORKDIR/why"; rm -rf "$YDEST"; mkdir -p "$YDEST"
	cp -r "$FX/warehouse_day/db" "$YDEST/db"; chmod -R u+rwX "$YDEST"
	MDY="$(ls "$YDEST"/db/byday/metadata/*.metadata.json | sort | tail -1)"
	q "CREATE FOREIGN TABLE evy (id bigint, dt date)
	   SERVER ice OPTIONS (metadata_path '$MDY')" >/dev/null
	check "the day() FDW reads the whole table (3 rows)" \
		"$(q "SELECT count(*) FROM evy")" "3"
	check "dt = 2020-01-02 day-prunes 2 files" \
		"$(fp "SELECT * FROM evy WHERE dt = DATE '2020-01-02'")" "2"
	check "dt = 2020-01-02 returns id 2" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evy WHERE dt = DATE '2020-01-02'")" "2"
	check "dt < 2020-01-02 day-prunes 2 files" \
		"$(fp "SELECT * FROM evy WHERE dt < DATE '2020-01-02'")" "2"
	check "dt < 2020-01-02 returns id 1" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evy WHERE dt < DATE '2020-01-02'")" "1"
	check "dt >= 2020-02-01 day-prunes 2 files" \
		"$(fp "SELECT * FROM evy WHERE dt >= DATE '2020-02-01'")" "2"
	check_text "a day()-pruned query matches iceberg_scan (epoch cross-check)" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM evy WHERE dt >= DATE '2020-02-01'")" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
		      FROM pgcolumnar.iceberg_scan('$MDY') AS t(id bigint, dt date)
		      WHERE dt >= DATE '2020-02-01'")"
fi

# ---- coarse temporal pruning: year/month/hour/day() on a TIMESTAMP -----------
# warehouse_temporal has four tables partitioned by year(ts), month(ts), day(ts),
# hour(ts) with column metrics disabled, so the temporal transform is the sole
# mechanism. Each transform's bucket is coarse (spans a range), so at the boundary
# bucket V == b the file MUST be read: the ">" predicates below keep the file
# whose bucket equals the constant's bucket AND that file holds a matching row, so
# a wrong exact-[V,V] rule would over-prune and drop it. pyiceberg-core wrote the
# buckets (year 2020->50; month 2021-06->617; day 2021-03-01->18687; hour ->448488),
# so the same-oracle read is the transform cross-check.
ftmp() {   # create a foreign table `rel` over warehouse_temporal/db/<sub>
	local rel="$1" sub="$2" md
	md="$(ls "$FX/warehouse_temporal/db/$sub"/metadata/*.metadata.json 2>/dev/null | sort | tail -1)"
	[ -n "$md" ] || return 1
	local dest="$PGC_WORKDIR/wt_$sub"; rm -rf "$dest"; mkdir -p "$dest"
	cp -r "$FX/warehouse_temporal/db/$sub" "$dest/$sub"; chmod -R u+rwX "$dest"
	md="$(ls "$dest/$sub"/metadata/*.metadata.json | sort | tail -1)"
	eval "MD_$rel=\"$md\""
	q "CREATE FOREIGN TABLE $rel (id bigint, ts timestamp)
	   SERVER ice OPTIONS (metadata_path '$md')" >/dev/null
}
oracle() {   # ids from iceberg_scan of MD_<rel> under the same WHERE
	local rel="$1" where="$2" md
	eval "md=\$MD_$rel"
	q "SELECT string_agg(id::text, ',' ORDER BY id)
	   FROM pgcolumnar.iceberg_scan('$md') AS t(id bigint, ts timestamp) WHERE $where"
}
if ftmp ty byyear && ftmp tm bymonth && ftmp td bydayts && ftmp th byhour; then
	# year(ts): 2020,2021,2023
	check "the year() FDW reads the whole table (3 rows)" "$(q 'SELECT count(*) FROM ty')" "3"
	check "ts > 2021-01-01 year-prunes the 2020 file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM ty WHERE ts > TIMESTAMP '2021-01-01 00:00:00'")" "1"
	check "ts > 2021-01-01 keeps the boundary-year file, returns ids 2,3" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ty WHERE ts > TIMESTAMP '2021-01-01 00:00:00'")" "2,3"
	check_text "year(): boundary read matches iceberg_scan (no over-prune)" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ty WHERE ts > TIMESTAMP '2021-01-01 00:00:00'")" \
		"$(oracle ty "ts > TIMESTAMP '2021-01-01 00:00:00'")"
	check "ts = 2021-06-01 year-prunes to one file (Files Pruned: 2)" \
		"$(fp "SELECT * FROM ty WHERE ts = TIMESTAMP '2021-06-01 00:00:00'")" "2"
	check "ts = 2021-06-01 returns id 2" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM ty WHERE ts = TIMESTAMP '2021-06-01 00:00:00'")" "2"

	# month(ts): 2021-01,-02,-06
	check "ts >= 2021-02-01 month-prunes the Jan file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM tm WHERE ts >= TIMESTAMP '2021-02-01 00:00:00'")" "1"
	check_text "month(): boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM tm WHERE ts >= TIMESTAMP '2021-02-01 00:00:00'")" \
		"$(oracle tm "ts >= TIMESTAMP '2021-02-01 00:00:00'")"

	# day(ts): 2021-03-01,-02,-05 (coarse: a whole day of timestamps per bucket)
	check "ts >= 2021-03-02 day-prunes the Mar-01 file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM td WHERE ts >= TIMESTAMP '2021-03-02 00:00:00'")" "1"
	check_text "day()-on-timestamp: boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM td WHERE ts >= TIMESTAMP '2021-03-02 00:00:00'")" \
		"$(oracle td "ts >= TIMESTAMP '2021-03-02 00:00:00'")"

	# hour(ts): 2021-03-01 00,01,05
	check "ts >= 2021-03-01 01:00 hour-prunes the 00h file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM th WHERE ts >= TIMESTAMP '2021-03-01 01:00:00'")" "1"
	check_text "hour(): boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM th WHERE ts >= TIMESTAMP '2021-03-01 01:00:00'")" \
		"$(oracle th "ts >= TIMESTAMP '2021-03-01 01:00:00'")"
fi

# ---- coarse temporal on a DATE (year/month) and a TIMESTAMPTZ (all four) ------
# warehouse_temporal_xt covers the source types beyond plain timestamp: year() and
# month() on a DATE column (day() on a date keeps the exact path), and all four on
# a TIMESTAMPTZ column, whose stored value is the UTC instant. Same coarse rule and
# boundary proof; pyiceberg-core wrote the buckets, so the same-oracle read is the
# cross-check. The timestamptz literals are absolute (+00), so the result is
# timezone-independent.
ftmpx() {   # rel sub "coldef"; foreign table over warehouse_temporal_xt/db/<sub>
	local rel="$1" sub="$2" coldef="$3" md
	md="$(ls "$FX/warehouse_temporal_xt/db/$sub"/metadata/*.metadata.json 2>/dev/null | sort | tail -1)"
	[ -n "$md" ] || return 1
	local dest="$PGC_WORKDIR/wx_$sub"; rm -rf "$dest"; mkdir -p "$dest"
	cp -r "$FX/warehouse_temporal_xt/db/$sub" "$dest/$sub"; chmod -R u+rwX "$dest"
	md="$(ls "$dest/$sub"/metadata/*.metadata.json | sort | tail -1)"
	eval "MD_$rel=\"$md\""; eval "COL_$rel=\"$coldef\""
	q "CREATE FOREIGN TABLE $rel (id bigint, $coldef)
	   SERVER ice OPTIONS (metadata_path '$md')" >/dev/null
}
oraclex() {   # rel where; ids from iceberg_scan of MD_<rel> under the same WHERE
	local rel="$1" where="$2" md coldef
	eval "md=\$MD_$rel"; eval "coldef=\$COL_$rel"
	q "SELECT string_agg(id::text, ',' ORDER BY id)
	   FROM pgcolumnar.iceberg_scan('$md') AS t(id bigint, $coldef) WHERE $where"
}
if ftmpx yd byyear_d "dt date" && ftmpx mo bymonth_d "dt date" \
	&& ftmpx yz byyear_tz "tt timestamptz" && ftmpx mz bymonth_tz "tt timestamptz" \
	&& ftmpx dz byday_tz "tt timestamptz" && ftmpx hz byhour_tz "tt timestamptz"; then

	# year() / month() on a DATE column
	check "year()-on-date prunes the 2020 file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM yd WHERE dt > DATE '2021-01-01'")" "1"
	check_text "year()-on-date: boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM yd WHERE dt > DATE '2021-01-01'")" \
		"$(oraclex yd "dt > DATE '2021-01-01'")"
	check "year()-on-date equality prunes to one file (Files Pruned: 2)" \
		"$(fp "SELECT * FROM yd WHERE dt = DATE '2021-06-01'")" "2"
	check "year()-on-date equality returns id 2" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM yd WHERE dt = DATE '2021-06-01'")" "2"
	check "month()-on-date prunes the Jan file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM mo WHERE dt >= DATE '2021-02-01'")" "1"
	check_text "month()-on-date: boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM mo WHERE dt >= DATE '2021-02-01'")" \
		"$(oraclex mo "dt >= DATE '2021-02-01'")"

	# year / month / day / hour on a TIMESTAMPTZ column (UTC instants)
	check "year()-on-timestamptz prunes the 2020 file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM yz WHERE tt > TIMESTAMPTZ '2021-01-01 00:00:00+00'")" "1"
	check_text "year()-on-timestamptz: boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM yz WHERE tt > TIMESTAMPTZ '2021-01-01 00:00:00+00'")" \
		"$(oraclex yz "tt > TIMESTAMPTZ '2021-01-01 00:00:00+00'")"
	check "month()-on-timestamptz prunes the Jan file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM mz WHERE tt >= TIMESTAMPTZ '2021-02-01 00:00:00+00'")" "1"
	check_text "month()-on-timestamptz: boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM mz WHERE tt >= TIMESTAMPTZ '2021-02-01 00:00:00+00'")" \
		"$(oraclex mz "tt >= TIMESTAMPTZ '2021-02-01 00:00:00+00'")"
	check "day()-on-timestamptz prunes the Mar-01 file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM dz WHERE tt >= TIMESTAMPTZ '2021-03-02 00:00:00+00'")" "1"
	check_text "day()-on-timestamptz: boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM dz WHERE tt >= TIMESTAMPTZ '2021-03-02 00:00:00+00'")" \
		"$(oraclex dz "tt >= TIMESTAMPTZ '2021-03-02 00:00:00+00'")"
	check "hour()-on-timestamptz prunes the 00h file (Files Pruned: 1)" \
		"$(fp "SELECT * FROM hz WHERE tt >= TIMESTAMPTZ '2021-03-01 01:00:00+00'")" "1"
	check_text "hour()-on-timestamptz: boundary read matches iceberg_scan" \
		"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM hz WHERE tt >= TIMESTAMPTZ '2021-03-01 01:00:00+00'")" \
		"$(oraclex hz "tt >= TIMESTAMPTZ '2021-03-01 01:00:00+00'")"
fi

# ---- a temporal transform read into the OTHER timestamp type (#1706) ---------
# The reader converts a naive (timestamp) source read into timestamptz, and a
# timestamptz source read into timestamp, as PostgreSQL's casts do in the session
# zone. The writer computed each partition cell in the source's own kind, so a
# cell does not bound the converted rows, and the FDW must not prune on it.
# In New York, byhour's naive 2021-03-01 00:00, 01:00, 05:00 are 05:00Z, 06:00Z
# and 10:00Z; byhour_tz's 00:00Z, 01:00Z, 05:00Z are the wall times 2021-02-28
# 19:00, 20:00 and 2021-03-01 00:00. Each FDW read is compared with iceberg_scan
# under the same declaration and zone, and check_text refuses two empty readings.
qny() { PGOPTIONS="-c TimeZone=America/New_York" q "$1"; }
if [ -n "${MD_th:-}" ] && ftmpx hn byhour_tz "tt timestamp"; then
	q "CREATE FOREIGN TABLE hw (id bigint, ts timestamptz)
	   SERVER ice OPTIONS (metadata_path '$MD_th')" >/dev/null
	check_text "hour() on a naive source read as timestamptz in New York matches iceberg_scan" \
		"$(qny "SELECT string_agg(id::text, ',' ORDER BY id) FROM hw WHERE ts >= TIMESTAMPTZ '2021-03-01 06:00:00+00'")" \
		"$(qny "SELECT string_agg(id::text, ',' ORDER BY id)
		        FROM pgcolumnar.iceberg_scan('$MD_th') AS t(id bigint, ts timestamptz)
		        WHERE ts >= TIMESTAMPTZ '2021-03-01 06:00:00+00'")"
	check_text "hour() on a timestamptz source read as timestamp in New York matches iceberg_scan" \
		"$(qny "SELECT string_agg(id::text, ',' ORDER BY id) FROM hn WHERE tt <= TIMESTAMP '2021-02-28 20:00:00'")" \
		"$(qny "SELECT string_agg(id::text, ',' ORDER BY id)
		        FROM pgcolumnar.iceberg_scan('$MD_hn') AS t(id bigint, tt timestamp)
		        WHERE tt <= TIMESTAMP '2021-02-28 20:00:00'")"
fi

# ---- a declared typmod applies, and pruning respects it (#1790) ---------------
# The foreign table returns each value as its declared type, typmod included. A
# typmod can change a value: varchar(2) reads the stored 'eu  ' as 'eu', and
# timestamp(0) reads 00:59:59.7 as 01:00:00. The partition value in the manifest
# is the stored one, so a file pruned on it could hold a row the query must
# return. warehouse_typmod (fixtures/iceberg/gen_iceberg_typmod.py) holds exactly
# those two values, each in its own partition.
q "CREATE FOREIGN TABLE ev_v1 (id bigint, region varchar(1), amount int)
   SERVER ice OPTIONS (metadata_path '$MD')" >/dev/null
# tm_state SQL -> the SQLSTATE SQL raised, or "no error (<first output line>)"
tm_state() {
	local out state
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	echo "${state:-no error ($(head -1 <<<"$out"))}"
}
check "typmod: a varchar(1) Iceberg foreign column refuses 'eu' (22001)" \
	"$(tm_state "SELECT string_agg(region, ',' ORDER BY region) FROM ev_v1")" "22001"
TMWH="$PGC_WORKDIR/tmwh"
rm -rf "$TMWH"; mkdir -p "$TMWH"
cp -r "$FX/warehouse_typmod/db" "$TMWH/db"
chmod -R u+rwX "$TMWH"
MD_PAD="$(ls "$TMWH"/db/byregion_pad/metadata/*.metadata.json | sort | tail -1)"
MD_FRAC="$(ls "$TMWH"/db/byhour_frac/metadata/*.metadata.json | sort | tail -1)"
q "CREATE FOREIGN TABLE pad2 (id bigint, region varchar(2))
   SERVER ice OPTIONS (metadata_path '$MD_PAD')" >/dev/null
q "CREATE FOREIGN TABLE frac0 (id bigint, ts timestamp(0))
   SERVER ice OPTIONS (metadata_path '$MD_FRAC')" >/dev/null
check "typmod: region = 'eu' on varchar(2) returns id 1, stored as 'eu  '" \
	"$(q "SELECT coalesce(string_agg(id::text, ',' ORDER BY id), 'none') FROM pad2 WHERE region = 'eu'")" "1"
check "typmod: ts >= 01:00 on timestamp(0) returns id 1, stored at 00:59:59.7, and id 2" \
	"$(q "SELECT coalesce(string_agg(id::text, ',' ORDER BY id), 'none') FROM frac0
	      WHERE ts >= TIMESTAMP '2021-03-01 01:00:00'")" "1,2"
# byregion_bucket is bucket[8](region): 'eu  ' (id 1) and 'eu' (id 3) sit in
# different buckets, yet both read 'eu' as varchar(2). A bucket pruned on the
# constant 'eu' would drop id 1. Today the planner wraps a varchar column in a
# cast to text for "=", so its qual never reaches bucket pruning; the typmod
# guard in ice_fdw_bucket_quals keeps id 1 if that changes.
MD_BKT="$(ls "$TMWH"/db/byregion_bucket/metadata/*.metadata.json | sort | tail -1)"
q "CREATE FOREIGN TABLE bkt2 (id bigint, region varchar(2))
   SERVER ice OPTIONS (metadata_path '$MD_BKT')" >/dev/null
check "typmod: region = 'eu' on a bucket-partitioned varchar(2) returns ids 1 and 3" \
	"$(q "SELECT coalesce(string_agg(id::text, ',' ORDER BY id), 'none') FROM bkt2 WHERE region = 'eu'")" "1,3"
# A time type at full microsecond precision changes no value, so it keeps
# pruning: the hour(ts) table read as timestamp(6) prunes as plain timestamp does.
q "CREATE FOREIGN TABLE th6 (id bigint, ts timestamp(6))
   SERVER ice OPTIONS (metadata_path '${MD_th:-}')" >/dev/null
check "typmod: ts >= 01:00 on timestamp(6) still hour-prunes the 00h file (Files Pruned: 1)" \
	"$(fp "SELECT * FROM th6 WHERE ts >= TIMESTAMP '2021-03-01 01:00:00'")" "1"

# ---- an invalid option is rejected by the validator -------------------------
check "an unknown table option is refused (FDW_INVALID_OPTION_NAME)" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
CREATE FOREIGN TABLE bad (id bigint) SERVER ice OPTIONS (nosuch 'x');
SQLEOF
)" "HV00D"

pgc_summary

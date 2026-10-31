#!/usr/bin/env bash
#
# pgColumnar Iceberg row-level deletes (#388 phases 4a position + 4b equality).
# iceberg_scan reads a table whose current snapshot carries delete files and
# drops the deleted rows. The fixture is hand-crafted
# (test/fixtures/iceberg/warehouse_del) because no available writer emits
# merge-on-read deletes; the data and delete Parquet are real pyarrow files and
# the manifests set exact sequence numbers, so both ordering rules (position:
# data_seq <= delete_seq; equality: data_seq < delete_seq, strictly) sit on
# testable boundaries. The oracle is the data rows minus the deletes.
#
# Usage:  test/iceberg_deletes.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# refusal SQL PHRASE -> the SQLSTATE when SQL is refused with PHRASE in its message;
# "SQLSTATE (another refusal: MESSAGE)" when a different guard refused it; "no
# error" when it ran; nothing when the server could not be asked. Each guard here
# has its own message, and several share a SQLSTATE (XX001 and 22023), so an arm
# counts only its own guard's refusal.
refusal() {
	local out st msg
	await_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then
		case "$msg" in *"$2"*) echo "$st" ;; *) echo "$st (another refusal: $msg)" ;; esac
	elif grep -q 'could not connect\|server closed the connection\|terminating connection' <<<"$out"; then
		:
	else
		echo "no error"
	fi
}

# the first ERROR message line, for telling apart same-SQLSTATE refusals
errmsg_of() {
	await_up
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  //p' | head -1
$1;
SQLEOF
}

# await_up -> wait, at most 60 s, until the server answers. A refusal arm that kills a
# backend sends the server through crash recovery; without the wait, the next arms'
# readings fail on recovery and red as collateral, more or fewer of them by timing
# (with the partition spec's fields guard removed, the backend segfaults and two
# later arms went red). The crash itself is still caught: survived() counts it.
await_up() {
	local i
	for i in $(seq 1 120); do
		[ "$(q 'SELECT 1')" = 1 ] && return 0
		sleep 0.5
	done
}

# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read. A fresh connection answers again after crash recovery, so
# "SELECT 1" alone passed with a backend killed by a refusal arm: with abort() in
# place of the null-pos refusal, or the fields guard removed (a segfault), and the
# check run once recovery had finished. The survival checks below also require
# that this count has not moved since their section began.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	await_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WHD="$FX/warehouse_del"
[ -f "$WHD/db/t/metadata/apply.metadata.json" ] || pgc_skip fixture "delete fixture is missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"

# relocate so recorded root (/tmp/pgc_ice_del) != actual root (rebasing)
DEST="$PGC_WORKDIR/del"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WHD/db" "$DEST/db"
chmod -R u+rwX "$DEST"
MDIR="$DEST/db/t/metadata"

# the full data, ignoring deletes, is 5 rows -- the baseline the delete removes from
check "the data file has 5 rows before deletes" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$DEST/db/t/data/data.parquet')
	      AS t(id bigint, region text, amount int)")" "5"

# ---- position deletes are applied ------------------------------------------
ORACLE="$(python3 - "$FX/warehouse_del/expected_deletes.json" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
print("\n".join("%d|%s|%d" % (r["id"], r["region"], r["amount"]) for r in o["surviving"]))
PY
)"
await_up
check_text "position deletes drop the listed rows (surviving == oracle)" \
	"$(q "SELECT id || '|' || region || '|' || amount
	      FROM pgcolumnar.iceberg_scan('$MDIR/apply.metadata.json')
	        AS t(id bigint, region text, amount int) ORDER BY id")" \
	"$ORACLE"
await_up
check "the deleted ids (2, 4) are gone" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/apply.metadata.json')
	      AS t(id bigint, region text, amount int) WHERE id IN (2,4)")" "0"
await_up
check "exactly the two deleted rows were removed (5 - 2 = 3)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/apply.metadata.json')
	      AS t(id bigint, region text, amount int)")" "3"
# deletes are by row ordinal, independent of column projection: reading only one
# column must still drop the same rows (the ordinal is the row index, not a
# column value)
await_up
check "position deletes apply under column projection (id only -> 1,3,5)" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$MDIR/apply.metadata.json') AS t(id bigint)")" \
	"1,3,5"

# ---- sequence-number ordering ----------------------------------------------
# The apply arm above uses a delete at the SAME sequence number as the data (5),
# which the spec says applies (data_seq <= delete_seq) -- the boundary that a
# strict > would wrongly exclude. This arm is the other side: a delete OLDER than
# the data (seq 4 < data seq 5) must NOT apply, so all five rows survive.
await_up
check "a delete older than the data (lower seq) does not apply (5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/noapply.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"
await_up
check "the would-be-deleted ids survive when the delete is older than the data" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/noapply.metadata.json')
	      AS t(id bigint, region text, amount int) WHERE id IN (2,4)")" "2"

# ---- the delete's sequence number is inherited when the entry leaves it null -
# real writers record a null entry sequence number and inherit the manifest's;
# this variant's delete entry is null, inheriting seq 5 from the manifest, so it
# must still apply over the seq-5 data (3 rows survive), proving inheritance.
await_up
check "a position delete with an inherited (null-entry) sequence number applies" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/inherit.metadata.json')
	      AS t(id bigint, region text, amount int)")" "3"

# ---- per-file scoping: a delete naming a different data file does not apply -
# the delete is applicable by sequence, but its rows target another data file, so
# none of data.parquet's rows may be dropped (proves the path match excludes, not
# just includes -- a basename-only or always-true match would wrongly delete).
await_up
check "a position delete naming a different data file deletes nothing (5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/wrongpath.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"

# ---- defect #5: a corrupt position-delete `pos` is refused, not swallowed ----
# On current main a null pos is silently dropped (the `!n1 && !n2` filter) and a
# negative pos is cast to a huge uint64 that matches nothing: the scan returns
# the phantom row(s) with NO error. The reader must instead refuse (XX001).
CRASHES_BEFORE="$(crashes)"
check "a position delete with a null pos is refused (XX001, not silently dropped)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nullpos.metadata.json')
	                AS t(id bigint, region text, amount int)" "has a null pos")" "XX001"
check "...and the null-pos refusal names pos" \
	"$(errmsg_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nullpos.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "null pos")" "1"
check "backend still up after the null-pos refusal" "$(survived "$CRASHES_BEFORE")" "1"

CRASHES_BEFORE="$(crashes)"
check "a position delete with a negative pos is refused (XX001, not cast inert)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/negpos.metadata.json')
	                AS t(id bigint, region text, amount int)" "has a negative pos")" "XX001"
check "...and the negative-pos refusal names the negative pos" \
	"$(errmsg_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/negpos.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "negative pos")" "1"
check "backend still up after the negative-pos refusal" "$(survived "$CRASHES_BEFORE")" "1"

# RISK control: pos == 0 is a LEGITIMATE delete of the first row (id 1). It must
# apply, not be rejected -- a `pos <= 0` guard would wrongly turn this red.
await_up
check "a position delete of ordinal 0 deletes the first row (id 1 gone, 4 remain)" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$MDIR/zeropos.metadata.json')
	        AS t(id bigint, region text, amount int)")" "2,3,4,5"

# ---- 4b: equality deletes are applied ---------------------------------------
# per-variant surviving id list from the generator's oracle
eq_oracle() {
	python3 - "$FX/warehouse_del/expected_deletes.json" "$1" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
print(",".join(str(i) for i in o["eq_surviving"][sys.argv[2]]))
PY
}
eq_ids_of() {
	q "SELECT string_agg(id::text, ',' ORDER BY id)
	   FROM pgcolumnar.iceberg_scan('$MDIR/$1.metadata.json')
	     AS t(id bigint, region text, amount int)"
}

CRASHES_BEFORE="$(crashes)"
await_up
check_text "equality delete drops matching rows (survivors == oracle)" \
	"$(eq_ids_of eqapply)" "$(eq_oracle eqapply)"
# THE boundary that distinguishes the equality rule (strict <) from the position
# rule (<=): the same delete file at a sequence number EQUAL to the data's must
# delete NOTHING (an equality delete never touches same-commit data)
await_up
check_text "an equality delete at an equal sequence number does not apply" \
	"$(eq_ids_of eqboundary)" "$(eq_oracle eqboundary)"
# multi-column equality is an AND: (3,'eu') matches no row (3 is 'us'), so only
# (4,'us') deletes
await_up
check_text "multi-column equality matches all columns (AND, not OR)" \
	"$(eq_ids_of eqmulti)" "$(eq_oracle eqmulti)"
# only the equality_ids columns define the match; the delete file's extra amount
# column carries a wrong value that must be ignored
await_up
check_text "columns beyond equality_ids are ignored for matching" \
	"$(eq_ids_of eqextra)" "$(eq_oracle eqextra)"
# null matches null (IS NULL semantics), and ONLY null: id 6 (region NULL) goes,
# id 7 ('eu') stays
await_up
check_text "a null delete value matches only null data values" \
	"$(eq_ids_of eqnull)" "$(eq_oracle eqnull)"
await_up
check "the null-region row is gone, the non-null rows remain" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/eqnull.metadata.json')
	      AS t(id bigint, region text, amount int) WHERE region IS NULL")" "0"
# two delete files with different equality_ids in one manifest: both apply
await_up
check_text "multiple equality-delete files apply their union" \
	"$(eq_ids_of eqtwo)" "$(eq_oracle eqtwo)"
# position and equality deletes in one snapshot: the skip sets merge
await_up
check_text "position and equality deletes combine" \
	"$(eq_ids_of eqmixed)" "$(eq_oracle eqmixed)"
# many delete rows (#1817): 4005 data rows against 2000 id keys and 1001
# (id, region) pairs. 20 rows match, including two NULL-region pairs. The
# survivor list is long, so both sides are compared as an md5.
await_up
check_text "many equality-delete rows drop exactly the matching rows (survivors == oracle)" \
	"$(q "SELECT md5(string_agg(id::text, ',' ORDER BY id))
	      FROM pgcolumnar.iceberg_scan('$MDIR/eqmany.metadata.json')
	        AS t(id bigint, region text, amount int)")" \
	"$(eq_oracle eqmany | tr -d '\n' | md5sum | cut -d' ' -f1)"
# the probe's DEBUG1 line counts the (data row, delete row) pairs it tested
# for equality. A hash probe tests a data row only against delete rows with
# the same hash, so the count stays near the number of matches. The old walk
# tested every data row against every delete row: about 4005 x 3001 pairs.
await_up
eqmany_probe="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -qtA -c "SET client_min_messages = debug1" \
	-c "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/eqmany.metadata.json')
	      AS t(id bigint, region text, amount int)" 2>&1 |
	awk -F'[ =]' '/equality-delete probe:/ {
		for (i = 1; i <= NF; i++) {
			if ($i == "data_rows") r += $(i + 1)
			if ($i == "delete_rows") d += $(i + 1)
			if ($i == "comparisons") c += $(i + 1)
		}
		n++
	}
	END { if (n) printf "%d %d %d", r, d, c }')"
read -r eqmany_rows eqmany_dels eqmany_cmp <<<"$eqmany_probe"
echo "-- eqmany probe (data_rows delete_rows comparisons): $eqmany_probe"
check "the equality-delete probe tests each data row against at most one delete row, not all of them (#1817)" \
	"$(if [ -n "$eqmany_cmp" ] && [ "$eqmany_rows" -eq 4005 ] && [ "$eqmany_dels" -gt 0 ] &&
	      [ "$eqmany_cmp" -le "$eqmany_rows" ]; then echo bounded
	   else echo "unbounded (data_rows=${eqmany_rows:-none} delete_rows=${eqmany_dels:-none} comparisons=${eqmany_cmp:-none})"; fi)" \
	"bounded"
# the delete columns come from the table schema, not the output list: a scan
# projecting only amount must still drop the id-matched rows
await_up
check "equality deletes apply when the delete column is not projected" \
	"$(q "SELECT string_agg(amount::text, ',' ORDER BY amount)
	      FROM pgcolumnar.iceberg_scan('$MDIR/eqapply.metadata.json') AS t(amount int)")" \
	"10,30,50"
check "backend still up after the equality applications" "$(survived "$CRASHES_BEFORE")" "1"

# ---- 4b: equality refusal and corruption arms -------------------------------
# content=2 with null equality_ids is corrupt metadata (the spec requires it)
CRASHES_BEFORE="$(crashes)"
check "an equality delete without equality_ids is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqnoids.metadata.json')
	                AS t(id bigint, region text, amount int)" "has no equality_ids")" "XX001"
# the pre-4b 'equality' fixture also lacks equality_ids, but its delete sits at
# the data's own sequence number, so under the strict-< rule it can never apply
# and is skipped before validation: the data reads untouched
await_up
check "a never-applicable equality delete without equality_ids is skipped (5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/equality.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"
# a partition-scoped equality delete would over-delete if applied globally; it is
# ---- phase 5: partition-scoped equality deletes are APPLIED, scoped ---------
# a partition-scoped equality delete (spec 1, identity region) on grp=9 tagged
# region=eu deletes only the eu-partition rows (ids 1,2); us rows survive. grp
# is 9 on every row, so only the partition scoping -- not the value match --
# can distinguish the partitions.
part_ids() {
	q "SELECT string_agg(id::text, ',' ORDER BY id)
	   FROM pgcolumnar.iceberg_scan('$MDIR/$1.metadata.json')
	     AS t(id bigint, region text, grp int)"
}
part_oracle() {
	python3 - "$FX/warehouse_del/expected_deletes.json" "$1" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
print(",".join(str(i) for i in o["part_surviving"][sys.argv[2]]))
PY
}
await_up
check_text "a partition-scoped equality delete applies only to its partition" \
	"$(part_ids eqpart_apply)" "$(part_oracle eqpart_apply)"
await_up
check "the us-partition rows survive an eu-scoped delete" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/eqpart_apply.metadata.json')
	      AS t(id bigint, region text, grp int) WHERE region = 'us'")" "3"
# a delete scoped to a partition (region=zz) that no data file is in applies to
# nothing -- a legitimate no-op, not an error and not a global delete
await_up
check "a delete scoped to an empty partition deletes nothing (5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/eqpart_nomatch.metadata.json')
	      AS t(id bigint, region text, grp int)")" "5"
# corrupt metadata: a data file with no partition_spec_id, matched by a
# partition-scoped delete, would silently under-delete; refuse it (the mirror
# of the delete-side has_spec_id check)
check "a data file lacking partition_spec_id is refused under a partitioned delete (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqpart_datanospec.metadata.json')
	                AS t(id bigint, region text, grp int)" "carries no partition_spec_id, which a partition-scoped")" "XX001"
# a delete written under a partition spec no data file uses (partition
# evolution): per the spec a partitioned delete never crosses spec ids, so it
# applies to nothing -- a no-op, all rows survive, never a global over-delete
await_up
check "a cross-spec partition-scoped delete applies to nothing (5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/eqpart_crossspec.metadata.json')
	      AS t(id bigint, region text, grp int)")" "5"
# a partition tuple the reader cannot compare exactly (a double) is refused
check "a partition-scoped delete with an uncomparable partition value is refused (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqpart_incomparable.metadata.json')
	                AS t(id bigint, region text, grp int)" "has a partition tuple this reader cannot compare")" "0A000"
# an equality column of a type with no supported mapping (timestamp) is refused
# before any file is opened
check "an equality delete on an unsupported column type is refused (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqtype.metadata.json')
	                AS t(id bigint, region text, amount int)" "is not supported")" "0A000"
check "...and the refusal names the type" \
	"$(errmsg_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqtype.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "type")" "1"
# an equality column present in the schema and delete file but absent from the
# data file errors loudly (the reader's missing-field-id error), never silently
# skips the delete
check "an equality column missing from the data file errors (22023)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqmissing.metadata.json')
	                AS t(id bigint, region text, amount int)" "no Parquet column has field id")" "22023"
# a delete-file path outside the table root is stopped by the path boundary
# (22023 from the boundary, BEFORE the file is opened; if the boundary were
# gone, /etc/hostname would be read and fail differently)
check "an equality-delete path outside the table location is refused (22023)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqescape.metadata.json')
	                AS t(id bigint, region text, amount int)" "is not under the table location")" "22023"
check "...and the boundary refusal names the escape" \
	"$(errmsg_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqescape.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "table location")" "1"
# a path under the table root that climbs out with ".." is refused by the
# containment (canonicalize collapses it and the re-check fails)
check "a dotdot-escaping delete path is refused (22023)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqdotdot.metadata.json')
	                AS t(id bigint, region text, amount int)" "escapes the table location")" "22023"
check "backend still up after the equality refusals" "$(survived "$CRASHES_BEFORE")" "1"

# ---- 4b audit arms (multi-agent adversarial audit findings) -----------------
# an equality_ids value beyond int32 must refuse the manifest (XX001); a silent
# 32-bit truncation would alias field 2^32+2 onto the real field 2 and delete
# rows the manifest never named
CRASHES_BEFORE="$(crashes)"
check "an equality_ids value beyond int32 refuses the manifest (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqbigid.metadata.json')
	                AS t(id bigint, region text, amount int)" "corrupt Avro manifest entry")" "XX001"
# a manifest list omitting partition_spec_id leaves an equality delete's scope
# undecidable; defaulting it to 0 (unpartitioned) would defeat the
# partition-scope guard, so the reader must refuse (XX001)
check "a manifest list without partition_spec_id is refused for equality deletes (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqnospec.metadata.json')
	                AS t(id bigint, region text, amount int)" "the manifest list carries no partition_spec_id")" "XX001"
# a partition-specs entry matching the delete's spec id but lacking the
# required "fields" key: treating it as unpartitioned would silently globalize
# the delete, so it is corrupt metadata (XX001)
check "a partition spec without its fields key is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/eqnofields.metadata.json')
	                AS t(id bigint, region text, amount int)" "has no fields array")" "XX001"
# an equality delete that can never apply (its sequence number exceeds no data
# file's) is skipped per spec, not validated: this variant's delete has an
# unsupported column type (field 8, timestamp) at the data's own sequence
# number, and must NOT refuse -- all five rows read
await_up
check "a never-applicable equality delete is skipped, not validated (5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/eqstaletype.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"
# a v1-shaped manifest (no sequence_number column in the entry schema) defaults
# every file's sequence number to 0 per spec, even for EXISTING entries -- the
# opposite of badseq, whose v2 schema HAS the column and carries a null
await_up
check "a v1 manifest without a sequence_number column reads (default 0, 5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/v1seq.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"
check "backend still up after the audit arms" "$(survived "$CRASHES_BEFORE")" "1"

# ---- spec: inheritance is ADDED-only; a null seq on an EXISTING entry is bad --
# the spec inherits a null sequence number only for status-1 (ADDED) entries; an
# EXISTING (status 0) entry with a null sequence number is corrupt, and silently
# inheriting the too-new manifest number could keep rows a delete should remove.
CRASHES_BEFORE="$(crashes)"
check "an EXISTING entry with a null sequence number is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/badseq.metadata.json')
	                AS t(id bigint, region text, amount int)" "has no sequence number")" "XX001"
check "backend still up after the bad-sequence refusal" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary

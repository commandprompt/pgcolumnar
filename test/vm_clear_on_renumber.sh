#!/usr/bin/env bash
#
# pgColumnar: the visibility map is cleared wherever LIVE rows are RENUMBERED.
#
# Three paths retire a row group and give its live rows new row numbers, and each
# must clear the all-visible bits over the OLD numbers. Otherwise an index-only
# scan answers from the index for a TID whose group no longer exists:
#
#   src/columnar_vacuum.c:2310  pgcolumnar_expire          -- held by ttl_expire.sh
#   src/columnar_vacuum.c:346   rewrite_one_group          -- held by NOTHING before this file
#   src/columnar_vacuum.c:765   pgcolumnar_recluster_online -- held by NOTHING before this file
#
# Only expire's clear was covered, because only expire's was reported as data
# loss. Deleting the other two left 319 checks across 14 suites green. This file
# is the two missing arms, and it exists as its own suite because the rule is one
# rule -- the comment at :765 states it as "wherever LIVE rows are renumbered
# rather than only where they expire" -- while the operations that break it live
# in different suites.
#
# THE INSTRUMENT, and why the obvious one does not work. `pg_class.relallvisible`
# is a statistic VACUUM refreshes; clearing a VM bit does not touch it, so an arm
# reading it reports the same number on a tree with the clear and a tree without.
# `pg_visibility` is not built under these prefixes. What does work is already in
# the extension: `pgcolumnar.vm_is_visible(rel, blk)` reads the fork through
# visibilitymap_get_status -- the same call the index-only-scan executor makes.
#
# THE FIXTURE'S ONE SUBTLETY. A DELETE clears the bits of the blocks holding the
# deleted rows, so a group that has been made compactable is ALREADY not
# all-visible over those blocks and an arm there cannot fail. The arms below
# therefore VACUUM first, delete only the FRONT of the target group, and assert
# over the group's LATER blocks -- all-visible on the way in, and reachable only
# by the rewrite's clear. Every arm is paired with a control block in a group the
# operation does not touch, so a clear that wiped the whole fork fails too.
#
# Usage:  test/vm_clear_on_renumber.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# premise: the block arithmetic below is written for MaxHeapTuplesPerPage at an
# 8 kB block size. A synthetic block covers COLUMNAR_VALID_ITEMPOINTER_OFFSETS
# row numbers (src/columnar.h), which is MaxHeapTuplesPerPage, 291 at BLCKSZ
# 8192. At another block size every block number below names a different row
# range and the arms would measure nothing.
PGC_BLOCK_SIZE="$(q "SELECT current_setting('block_size');")"
if [ "$PGC_BLOCK_SIZE" != "8192" ]; then
	echo "PREMISE FAILED: block_size is [$PGC_BLOCK_SIZE]; these arms are written for 8192" >&2
	exit 1
fi
SPAN=291

# Block holding a given row number, the same division the extension does.
blk_of() { echo $(( $1 / SPAN )); }

# t/f for one synthetic block.
vis() { q "SELECT pgcolumnar.vm_is_visible('$1', $2);"; }

# ---------------------------------------------------------------------------
# Part 1: rewrite_one_group, reached through pgcolumnar.compact_rewrite
# ---------------------------------------------------------------------------

psql_run "CREATE TABLE cw (id int, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('cw', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO cw SELECT g, g FROM generate_series(1,20480) g;"
psql_run "CREATE INDEX cw_id ON cw (id);"
psql_run "VACUUM cw;"

# Take the geometry from the catalog rather than from the INSERT, so a change to
# the group size is a premise failure here instead of an arm that silently moves
# to a different part of the table.
CW_G3_FIRST="$(q "SELECT first_row_number FROM pgcolumnar.row_group
                  WHERE storage_id = pgcolumnar.get_storage_id('cw') AND group_number = 3;")"
CW_G3_COUNT="$(q "SELECT row_count FROM pgcolumnar.row_group
                  WHERE storage_id = pgcolumnar.get_storage_id('cw') AND group_number = 3;")"
CW_G5_FIRST="$(q "SELECT first_row_number FROM pgcolumnar.row_group
                  WHERE storage_id = pgcolumnar.get_storage_id('cw') AND group_number = 5;")"
case "$CW_G3_FIRST$CW_G3_COUNT$CW_G5_FIRST" in
	*[!0-9]*|"") echo "PREMISE FAILED: group 3/5 geometry unreadable [$CW_G3_FIRST/$CW_G3_COUNT/$CW_G5_FIRST]" >&2; exit 1 ;;
esac

CW_LAST=$(( CW_G3_FIRST + CW_G3_COUNT - 1 ))
CW_FRONT_END=$(( CW_G3_FIRST + 600 ))                 # deleted; ~29% of a 2048 group
CW_TARGET_BLK=$(blk_of $(( CW_LAST - 100 )))          # late in group 3, no deleted row
CW_FRONT_BLK=$(blk_of "$CW_G3_FIRST")                 # early in group 3, deleted rows
CW_CTRL_BLK=$(blk_of $(( CW_G5_FIRST + 100 )))        # group 5, untouched throughout

# The arm is only meaningful if the target block holds no deleted row, otherwise
# the DELETE below clears it and the rewrite is not what the arm measures.
if [ "$CW_TARGET_BLK" -le "$(blk_of "$CW_FRONT_END")" ]; then
	echo "PREMISE FAILED: target block $CW_TARGET_BLK is inside the deleted range" >&2
	exit 1
fi

check "premise: VACUUM set all-visible on group 3's late block" \
	"$(vis cw "$CW_TARGET_BLK")" "t"
check "premise: and on the untouched control group's block" \
	"$(vis cw "$CW_CTRL_BLK")" "t"

psql_run "DELETE FROM cw WHERE id BETWEEN $CW_G3_FIRST AND $CW_FRONT_END;"

# These two are what make the arm attributable. The DELETE clears the blocks it
# touches; if it also cleared the target block there would be nothing left for
# the rewrite to do and the arm could not fail.
check "the delete cleared the block holding the deleted rows" \
	"$(vis cw "$CW_FRONT_BLK")" "f"
check "premise: but left group 3's late block all-visible" \
	"$(vis cw "$CW_TARGET_BLK")" "t"
check "premise: and left the control block all-visible" \
	"$(vis cw "$CW_CTRL_BLK")" "t"

# Measure the work, not the intent: the call must report that it rewrote exactly
# the one qualifying group. A zero here would leave every arm below green for a
# rewrite that never ran.
CW_REWROTE="$(q "SELECT pgcolumnar.compact_rewrite('cw', 0.2);")"
check "compact_rewrite rewrote exactly the one qualifying group" "$CW_REWROTE" "1"
check "premise: group 3 is gone from the catalog, so its rows were renumbered" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group
	      WHERE storage_id = pgcolumnar.get_storage_id('cw') AND group_number = 3;")" "0"

# THE ARM. Revert src/columnar_vacuum.c:346 and this goes green-to-red: the block
# stays all-visible over row numbers whose group no longer exists.
check "rewrite_one_group cleared the retired group's late block (#877)" \
	"$(vis cw "$CW_TARGET_BLK")" "f"

# THE CONTROL. A clear that wiped the whole fork would satisfy the arm above.
check "control: a group the rewrite did not touch keeps its all-visible bit" \
	"$(vis cw "$CW_CTRL_BLK")" "t"

# ---------------------------------------------------------------------------
# Part 2: pgcolumnar_recluster_online, reached through pgcolumnar.recluster
# ---------------------------------------------------------------------------
#
# Recluster renumbers every live row and takes no group limit, so there is no
# untouched group to use as an in-table control. A second columnar table,
# vacuumed the same way and never reclustered, carries that half: it fails a
# clear that reached beyond the relation it was called on.

for t in rc rcctl; do
	psql_run "CREATE TABLE $t (id int, x int, y int) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$t', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
	psql_run "INSERT INTO $t SELECT g, ((g::bigint*7919)%200)::int, ((g::bigint*104729)%200)::int
	          FROM generate_series(1,20480) g;"
	psql_run "VACUUM $t;"
done

RC_FIRST="$(q "SELECT min(first_row_number) FROM pgcolumnar.row_group
               WHERE storage_id = pgcolumnar.get_storage_id('rc');")"
RC_LAST="$(q "SELECT max(first_row_number + row_count - 1) FROM pgcolumnar.row_group
              WHERE storage_id = pgcolumnar.get_storage_id('rc');")"
case "$RC_FIRST$RC_LAST" in *[!0-9]*|"") echo "PREMISE FAILED: rc geometry unreadable" >&2; exit 1 ;; esac

# A synthetic block is all-visible only when every row number it spans exists.
# The first and last blocks of a relation are PARTIALLY covered -- block 0 spans
# row 0, which no relation has, and the top block runs past the last row -- so
# both read `f` on a correct tree and an arm placed there cannot fail. Step three
# whole blocks in from each end, and gate that the result is genuinely interior.
RC_EARLY_BLK=$(( $(blk_of "$RC_FIRST") + 3 ))
RC_LATE_BLK=$(( $(blk_of "$RC_LAST") - 3 ))
if [ "$RC_EARLY_BLK" -ge "$RC_LATE_BLK" ] || \
   [ $(( RC_EARLY_BLK * SPAN )) -lt "$RC_FIRST" ] || \
   [ $(( (RC_LATE_BLK + 1) * SPAN - 1 )) -gt "$RC_LAST" ]; then
	echo "PREMISE FAILED: blocks $RC_EARLY_BLK/$RC_LATE_BLK are not wholly inside rows $RC_FIRST..$RC_LAST" >&2
	exit 1
fi

check "premise: VACUUM set all-visible early in rc"        "$(vis rc "$RC_EARLY_BLK")"    "t"
check "premise: and late in rc"                            "$(vis rc "$RC_LATE_BLK")"     "t"
check "premise: and on the control relation"               "$(vis rcctl "$RC_EARLY_BLK")" "t"

RC_GROUPS="$(q "SELECT pgcolumnar.recluster('rc','x','y');")"
check "recluster reported reclustering groups" \
	"$([ "${RC_GROUPS:-0}" -gt 0 ] 2>/dev/null && echo yes || echo no)" "yes"

# THE ARMS. Revert src/columnar_vacuum.c:765 and both go red: recluster gives
# every live row a new number, so every old block must lose its bit.
check "recluster_online cleared the old numbers' early block (#877)" \
	"$(vis rc "$RC_EARLY_BLK")" "f"
check "recluster_online cleared the old numbers' late block (#877)" \
	"$(vis rc "$RC_LATE_BLK")" "f"

# THE CONTROL, the half that fails an over-broad clear.
check "control: a relation that was not reclustered keeps its all-visible bits" \
	"$(vis rcctl "$RC_EARLY_BLK")" "t"
check "control: and its late block too" \
	"$(vis rcctl "$RC_LATE_BLK")" "t"

pgc_summary

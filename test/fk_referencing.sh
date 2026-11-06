#!/usr/bin/env bash
#
# pgColumnar foreign keys that reference a columnar table.
#
# Referential integrity reads the referenced row with FOR KEY SHARE, to hold the
# parent key still until the referencing transaction ends. Row locking is not
# implemented for columnar tables, so that read raises.
#
# Before this was refused at DDL, the constraint was accepted and then every
# insert into the referencing table failed, including inserts that satisfied it:
#
#     CREATE TABLE parent (id int PRIMARY KEY) USING pgcolumnar;
#     INSERT INTO parent VALUES (1);
#     CREATE TABLE child (id int REFERENCES parent(id));   -- accepted
#     INSERT INTO child VALUES (1);                        -- parent row exists
#     ERROR:  columnar: row locking is not supported yet
#
# A table that can never be written to is worse than a constraint that is
# refused, and the refusal belongs where the configuration is chosen. Core does
# the same for an unlogged table under a foreign key: it fails at CREATE TABLE
# rather than at every INSERT.
#
# The direction matters and is the reason this file is not one check. Only the
# REFERENCED side is refused. Columnar on the REFERENCING side reads a heap
# parent and writes its own rows through the ordinary insert path, so it works
# and must keep working -- a fix that rejected both directions would take away
# something that does.
#
# There are two ways into that configuration and the file covers both. Creating
# the constraint against a columnar table is one; making an already-referenced
# table columnar is the other, and it reaches the identical dead end from the
# opposite side (sections 5 and 6). The second is refused at
# ALTER TABLE ... SET ACCESS METHOD, so the controls there are about not
# over-reaching: an unrelated ALTER TABLE on a referenced table, a conversion of
# the referencing side, and a conversion of a table with no foreign key at all
# must all still work.
#
# Usage:  test/fk_referencing.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# --- 1. the referenced side is refused, at DDL ---------------------------------

psql_run "DROP TABLE IF EXISTS fk_child; DROP TABLE IF EXISTS fk_parent;
	CREATE TABLE fk_parent (id int PRIMARY KEY) USING pgcolumnar;
	INSERT INTO fk_parent VALUES (1), (2);" >/dev/null

err="$(psql_run "CREATE TABLE fk_child (id int REFERENCES fk_parent(id));" 2>&1 || true)"

check "a foreign key referencing a columnar table is refused" \
	"$(case "$err" in *"cannot create a foreign key referencing columnar table"*) echo yes ;;
		*) echo "no ($err)" ;; esac)" \
	"yes"

# The message has to name the reason, because the person reading it chose a
# perfectly ordinary schema and needs to know which half to change.
check "and the error says why" \
	"$(case "$err" in *"row locking is not supported"*) echo yes ;; *) echo "no ($err)" ;; esac)" \
	"yes"

check "the refused table was not left behind" \
	"$(q "SELECT count(*) FROM pg_class WHERE relname = 'fk_child';")" "0"

# --- 2. ALTER TABLE ADD CONSTRAINT is refused too ------------------------------

# Same constraint by a different route. A check on CREATE TABLE alone would let
# this one through, and the trap would be identical.
psql_run "DROP TABLE IF EXISTS fk_child2;
	CREATE TABLE fk_child2 (id int);" >/dev/null

err2="$(psql_run "ALTER TABLE fk_child2 ADD CONSTRAINT fk_c2
	FOREIGN KEY (id) REFERENCES fk_parent(id);" 2>&1 || true)"

check "ALTER TABLE ADD CONSTRAINT is refused by the same rule" \
	"$(case "$err2" in *"cannot create a foreign key referencing columnar table"*) echo yes ;;
		*) echo "no ($err2)" ;; esac)" \
	"yes"

# --- 3. the other direction still works ----------------------------------------

# This is the check that fails if the fix is too broad, and it is the one worth
# keeping if either is ever dropped: a columnar table on the REFERENCING side
# reads a heap parent, which locks fine, and writes its own rows through the
# ordinary insert path. That combination worked before and must still.

psql_run "DROP TABLE IF EXISTS fk_c_child; DROP TABLE IF EXISTS fk_h_parent;
	CREATE TABLE fk_h_parent (id int PRIMARY KEY);
	INSERT INTO fk_h_parent VALUES (1), (2), (3);" >/dev/null

err3="$(psql_run "CREATE TABLE fk_c_child (id int REFERENCES fk_h_parent(id), v text)
	USING pgcolumnar;" 2>&1 || true)"

check "a columnar table may reference a heap table" \
	"$(case "$err3" in *ERROR*) echo "no ($err3)" ;; *) echo yes ;; esac)" \
	"yes"

psql_run "INSERT INTO fk_c_child VALUES (1, 'a'), (2, 'b');" >/dev/null 2>&1

check "and rows that satisfy it are accepted" \
	"$(q "SELECT count(*) FROM fk_c_child;")" "2"

# and the constraint is still enforced in that direction
bad="$(psql_run "INSERT INTO fk_c_child VALUES (99, 'nope');" 2>&1 || true)"
check "a row that violates it is still rejected" \
	"$(case "$bad" in *"violates foreign key constraint"*) echo yes ;; *) echo "no ($bad)" ;; esac)" \
	"yes"

# --- 4. constraints that do not need a row lock are unaffected -----------------

# The rule keys on the constraint being FOREIGN and on the referenced side being
# columnar. Everything else must be untouched, or this fix has taken away far
# more than the trap it removes.

psql_run "DROP TABLE IF EXISTS fk_other;" >/dev/null
err4="$(psql_run "CREATE TABLE fk_other (
		id int PRIMARY KEY,
		k  int UNIQUE,
		v  int CHECK (v > 0),
		w  int NOT NULL
	) USING pgcolumnar;" 2>&1 || true)"

check "primary key, unique, check and not-null on a columnar table are unaffected" \
	"$(case "$err4" in *ERROR*) echo "no ($err4)" ;; *) echo yes ;; esac)" \
	"yes"

psql_run "INSERT INTO fk_other VALUES (1, 10, 5, 7);" >/dev/null 2>&1
check "and they still work" "$(q "SELECT count(*) FROM fk_other;")" "1"

viol="$(psql_run "INSERT INTO fk_other VALUES (1, 11, 5, 7);" 2>&1 || true)"
check "the primary key is still enforced" \
	"$(case "$viol" in *"duplicate key"*) echo yes ;; *) echo "no ($viol)" ;; esac)" \
	"yes"

# a foreign key between two heap tables must be entirely unaffected
psql_run "DROP TABLE IF EXISTS fk_hh_child; DROP TABLE IF EXISTS fk_hh_parent;
	CREATE TABLE fk_hh_parent (id int PRIMARY KEY);" >/dev/null
err5="$(psql_run "CREATE TABLE fk_hh_child (id int REFERENCES fk_hh_parent(id));" 2>&1 || true)"

check "a foreign key between two heap tables is unaffected" \
	"$(case "$err5" in *ERROR*) echo "no ($err5)" ;; *) echo yes ;; esac)" \
	"yes"

# --- 5. the same configuration reached by converting the parent ----------------

# Refusing the constraint closes the door where the constraint is created. The
# same unusable state is reachable from the other end: create both tables as
# heap, then make the referenced one columnar. The constraint remains, the
# parent is columnar, and every later insert into the child fails.
psql_run "DROP TABLE IF EXISTS fk_conv_child; DROP TABLE IF EXISTS fk_conv_parent;
	CREATE TABLE fk_conv_parent (id int PRIMARY KEY);
	CREATE TABLE fk_conv_child (id int REFERENCES fk_conv_parent(id));
	INSERT INTO fk_conv_parent VALUES (1);
	INSERT INTO fk_conv_child VALUES (1);" >/dev/null

conv="$(psql_run "ALTER TABLE fk_conv_parent SET ACCESS METHOD pgcolumnar;" 2>&1 || true)"

check "converting an FK-referenced table to columnar is refused" \
	"$(case "$conv" in *"referenced by a foreign key"*) echo yes ;; *) echo "no ($conv)" ;; esac)" \
	"yes"

check "and the error names the constraint" \
	"$(case "$conv" in *"row locking is not supported"*) echo yes ;; *) echo "no ($conv)" ;; esac)" \
	"yes"

# The refusal has to leave the table alone, not half-convert it.
check "the parent is still a row store" \
	"$(q "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam
		WHERE c.relname = 'fk_conv_parent';" | tail -1)" "heap"

# The trap this exists to prevent: the child stays writable.
psql_run "INSERT INTO fk_conv_parent VALUES (2);" >/dev/null 2>&1
ins="$(psql_run "INSERT INTO fk_conv_child VALUES (2);" 2>&1 || true)"
check "the child is still writable afterwards" \
	"$(case "$ins" in *ERROR*) echo "no ($ins)" ;; *) echo yes ;; esac)" \
	"yes"

# The extension's own helper runs the same statement, so it must be refused the
# same way rather than becoming a way around the check.
helper="$(psql_run "SELECT pgcolumnar.alter_table_set_access_method('fk_conv_parent', 'pgcolumnar');" 2>&1 || true)"
check "the conversion helper is refused identically" \
	"$(case "$helper" in *"referenced by a foreign key"*) echo yes ;; *) echo "no ($helper)" ;; esac)" \
	"yes"

# --- 6. and nothing else about SET ACCESS METHOD changes -----------------------

# The over-rejection controls. A rule keyed on the wrong side, or one that fired
# for any ALTER TABLE rather than this one, passes every check above and fails
# these.

psql_run "DROP TABLE IF EXISTS fk_conv_plain;
	CREATE TABLE fk_conv_plain (id int PRIMARY KEY);
	INSERT INTO fk_conv_plain VALUES (1);" >/dev/null
plain="$(psql_run "ALTER TABLE fk_conv_plain SET ACCESS METHOD pgcolumnar;" 2>&1 || true)"
check "a table with no foreign key still converts" \
	"$(case "$plain" in *ERROR*) echo "no ($plain)" ;; *) echo yes ;; esac)" \
	"yes"

# The referencing side is the direction that works, so converting the CHILD must
# still be allowed -- this is the mirror of check 3 above.
childconv="$(psql_run "ALTER TABLE fk_conv_child SET ACCESS METHOD pgcolumnar;" 2>&1 || true)"
check "converting the referencing side is still allowed" \
	"$(case "$childconv" in *ERROR*) echo "no ($childconv)" ;; *) echo yes ;; esac)" \
	"yes"

# An unrelated ALTER TABLE on an FK-referenced table must be untouched. Checking
# the command rather than the post-alter state is what keeps this true: a rule
# that fired whenever a referenced table was columnar would reject this.
other="$(psql_run "ALTER TABLE fk_conv_parent ADD COLUMN note text;" 2>&1 || true)"
check "an unrelated ALTER TABLE on a referenced table is unaffected" \
	"$(case "$other" in *ERROR*) echo "no ($other)" ;; *) echo yes ;; esac)" \
	"yes"

# Converting away from columnar is not this rule's business.
back="$(psql_run "ALTER TABLE fk_conv_plain SET ACCESS METHOD heap;" 2>&1 || true)"
check "converting back to heap is unaffected" \
	"$(case "$back" in *ERROR*) echo "no ($back)" ;; *) echo yes ;; esac)" \
	"yes"

# AND ON THE REFERENCED TABLE ITSELF (#1341). The control above converts a table
# with no foreign key, so it cannot see a rule that refuses every SET ACCESS METHOD
# on a referenced table whatever the target: with the guard's columnar-target test
# removed, this file stayed 26/26. Only a columnar target needs the row lock the
# constraint cannot get.
refheap="$(psql_run "ALTER TABLE fk_conv_parent SET ACCESS METHOD heap;" 2>&1 || true)"
check "an FK-referenced table may still be set to a row store" \
	"$(case "$refheap" in *ERROR*) echo "no ($refheap)" ;; *) echo yes ;; esac)" \
	"yes"

# --- 7. a partitioned parent is refused too (#201) -----------------------------

# A partitioned table has no storage, so setting its access method breaks nothing
# at the time. It chooses what every later partition inherits, and core clones a
# foreign key to each new partition, so with the constraint in place every
# ordinary partition creation is then refused -- by an error naming the partition
# the user just wrote, saying nothing about the ALTER that caused it.
#
# The routes that were already covered are asserted first, because they are what
# makes this a small gap rather than a trap: the constraint-side check fires on
# the cloned constraint, so a columnar partition is refused however it arrives.

psql_run "DROP TABLE IF EXISTS fk_pc; DROP TABLE IF EXISTS fk_pp;
	CREATE TABLE fk_pp (id int PRIMARY KEY) PARTITION BY RANGE (id);
	CREATE TABLE fk_pp1 PARTITION OF fk_pp FOR VALUES FROM (1) TO (100);
	CREATE TABLE fk_pc (id int REFERENCES fk_pp(id));" >/dev/null

perr="$(psql_run "CREATE TABLE fk_pp2 PARTITION OF fk_pp
	FOR VALUES FROM (100) TO (200) USING pgcolumnar;" 2>&1 || true)"

check "a columnar partition of an FK-referenced parent is refused" \
	"$(case "$perr" in *"cannot create a foreign key referencing columnar table"*) echo yes ;;
		*) echo "no ($perr)" ;; esac)" \
	"yes"

aerr="$(psql_run "ALTER TABLE fk_pp SET ACCESS METHOD pgcolumnar;" 2>&1 || true)"

check "and setting the parent's access method is refused as well" \
	"$(case "$aerr" in *"cannot convert table"*) echo yes ;; *) echo "no ($aerr)" ;; esac)" \
	"yes"

# The consequence that made it worth refusing: without this, the parent keeps a
# columnar access method and the next ordinary partition creation fails.
# Asked as "did it become columnar", not "is it heap": a partitioned table that
# has never been given an access method has relam = 0, so a join against pg_am
# returns nothing and an equality test against 'heap' fails on a correct build.
# It did, on the first run of this check.
check "so the parent did not become columnar" \
	"$(q "SELECT count(*) FROM pg_class c JOIN pg_am a ON a.oid = c.relam
		WHERE c.relname = 'fk_pp' AND a.amname = 'pgcolumnar';")" \
	"0"

check "and a partition can still be created the ordinary way" \
	"$(psql_run "CREATE TABLE fk_pp3 PARTITION OF fk_pp
		FOR VALUES FROM (200) TO (300);" 2>&1 | grep -c ERROR)" \
	"0"

# The control, and the one that fails if the rule is keyed on the wrong thing: a
# partitioned table nobody references converts fine, and its partitions inherit.
#
# Core accepts SET ACCESS METHOD on a partitioned table only from PostgreSQL 17.
# Before that it refuses every such ALTER whether or not a foreign key is
# involved, so the control cannot be expressed there and these two checks fail on
# a correct build. They did: added by #206 for issue #201 and gated on 18 and 19
# only, they went in red on 15 and 16 and stayed red until the next full matrix
# found them.
#
# The older majors get the inverse assertion rather than a silent skip, so the
# suite still says something on them: the refusal must be core's, by core's
# message. If this extension ever starts refusing first, or core lifts the
# restriction, that check fails and this branch gets revisited.
psql_run "DROP TABLE IF EXISTS fk_free;
	CREATE TABLE fk_free (id int) PARTITION BY RANGE (id);" >/dev/null

if [ "$PGC_MAJOR" -ge 17 ]; then
	check "a partitioned table with no foreign key still converts" \
		"$(psql_run "ALTER TABLE fk_free SET ACCESS METHOD pgcolumnar;" 2>&1 | grep -c ERROR)" \
		"0"

	check "and its partitions inherit the columnar access method" \
		"$(psql_run "CREATE TABLE fk_free1 PARTITION OF fk_free FOR VALUES FROM (1) TO (100);" >/dev/null 2>&1;
		   q "SELECT amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam
			WHERE c.relname = 'fk_free1';")" \
		"pgcolumnar"
else
	ferr="$(psql_run "ALTER TABLE fk_free SET ACCESS METHOD pgcolumnar;" 2>&1 || true)"

	check "before 17, core refuses this for every partitioned table" \
		"$(case "$ferr" in *"cannot change access method of a partitioned table"*) echo yes ;;
			*) echo "no ($ferr)" ;; esac)" \
		"yes"
fi

pgc_summary

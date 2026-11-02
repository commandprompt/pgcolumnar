#!/usr/bin/env bash
#
# pgColumnar visibility-map privilege boundary (#558).
#
# pgcolumnar.vm_selftest() writes an ALL_VISIBLE bit into a relation's
# visibility-map fork. It took an arbitrary regclass and checked nothing: not
# ownership, not any ACL, not even that the relation was columnar. CREATE FUNCTION
# grants EXECUTE to PUBLIC and the install script contains no REVOKE, so any role
# holding USAGE on the pgcolumnar schema could mark pages of ANY relation
# all-visible -- including a plain heap table it neither owned nor could read.
#
# The consequence is not an error message, it is wrong answers: an index-only scan
# over the victim skips the heap visibility check and returns deleted rows.
# Measured before the fix, on a table with 10,000 live rows and 10,000 dead ones:
#
#     index-only scan  20000        sequential scan  10000
#
# So this suite asserts the CONSEQUENCE and not merely that the call now errors.
# A suite that only checked for "must be owner" would stay green if a later change
# made the function succeed for some relation while still skipping the heap check,
# which is the shape of the original defect.
#
# Both directions are pinned, per CONTEXT.md:
#   - the unprivileged role is refused AND the scan stays correct;
#   - the OWNER is still allowed, so the gate is permission and not breakage.
#
# The schema does not grant USAGE to PUBLIC, and that is what accidentally
# contained this before it was found. It is granted here on purpose so the call
# reaches the C gate instead of stopping at the schema -- otherwise this suite
# would pass against the unfixed code and prove nothing. Sibling of
# test/server_file_privilege.sh, which makes the same argument for the
# server-file roles.
#
# Usage:  test/vm_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

_root="$(dirname "${BASH_SOURCE[0]}")/.."
_ver=$(grep -oE "default_version = '[^']+'" "$_root/pgcolumnar.control" | sed "s/.*'\(.*\)'/\1/")
SQLFILE="$_root/pgcolumnar--$_ver.sql"
[ -f "$SQLFILE" ] || { echo "FAIL  install script $SQLFILE not found"; exit 1; }

run_as() {	# role sql -> combined output
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -X -v ON_ERROR_STOP=0 -c "SET ROLE $1;" -c "$2" 2>&1
}

# ---- fixture: a HEAP table with dead tuples, owned by nobody in particular ----
# Heap on purpose. The function never checked that its argument was columnar, so
# the reachable blast radius was every table in the database, and a columnar-only
# fixture would understate it.
# #748 changed what this suite can assert, and the change is a strengthening.
# These entry points now refuse ANY relation that is not columnar, so the
# heap blast radius described above is closed by construction rather than by
# an ownership check. That makes two fixtures necessary:
#
#   vmvictim  columnar -- the ownership boundary still applies here, so this is
#                         where the refused/allowed arms and the wrong-answers
#                         consequence are asserted.
#   vmheap    heap     -- refused for EVERYONE now, owner included. That arm is
#                         the one that pins #748, and it is stronger than the
#                         ownership arm it replaces: no caller can reach a heap
#                         relation's visibility map at all.
psql_run "CREATE TABLE vmvictim (id int, secret text) USING pgcolumnar;"
psql_run "INSERT INTO vmvictim SELECT g, 'secret-'||g FROM generate_series(1,20000) g;"
psql_run "DELETE FROM vmvictim WHERE id % 2 = 0;"
psql_run "ANALYZE vmvictim;"
psql_run "CREATE TABLE vmheap (id int primary key, secret text);"
psql_run "INSERT INTO vmheap SELECT g, 'secret-'||g FROM generate_series(1,20000) g;"
psql_run "DELETE FROM vmheap WHERE id % 2 = 0;"
psql_run "ANALYZE vmheap;"

# TWO roles, because the fix has two layers and a single role would let one of
# them be deleted with this suite still green -- the question CONTEXT.md asks of
# every change ("can I delete this and still be green?").
#
#   t_vmnone  schema USAGE only.  Must be stopped by the SQL REVOKE.
#   t_vmexec  schema USAGE plus an explicit EXECUTE grant, which defeats the
#             REVOKE on purpose, so the call reaches the C ownership check and
#             THAT is what refuses it.
#
# Without t_vmexec, removing the C gate leaves this suite green, because the
# REVOKE alone would still refuse t_vmnone.
psql_run "DROP ROLE IF EXISTS t_vmnone;"
psql_run "CREATE ROLE t_vmnone NOSUPERUSER;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_vmnone;"

psql_run "DROP ROLE IF EXISTS t_vmexec;"
psql_run "CREATE ROLE t_vmexec NOSUPERUSER;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_vmexec;"
psql_run "GRANT EXECUTE ON FUNCTION pgcolumnar.vm_selftest(regclass,int) TO t_vmexec;"
psql_run "GRANT EXECUTE ON FUNCTION pgcolumnar.vm_is_visible(regclass,int) TO t_vmexec;"

nblocks="$(q "SELECT pg_relation_size('vmvictim')/8192;")"

# ---- premises -----------------------------------------------------------------
# Each of these makes a later assertion mean something. If any fails, the run is
# void rather than green. The three without the prefix read the columnar code under
# test (its scan, its visibility map, its storage size), so they are arms (#1381
# review): a premise is a fact about the fixture alone.
check_num "the fixture has 10000 live rows" \
	"$(q "SELECT count(*) FROM vmvictim;")" 10000
check "premise: the test role is not a superuser" \
	"$(q "SELECT rolsuper FROM pg_roles WHERE rolname='t_vmnone';")" "f"
check "premise: the test role cannot read the victim" \
	"$(q "SELECT has_table_privilege('t_vmnone','vmvictim','SELECT');")" "f"
check "premise: the test role does reach the schema (so it meets the C gate)" \
	"$(q "SELECT has_schema_privilege('t_vmnone','pgcolumnar','USAGE');")" "t"
check "premise: the boundary victim is columnar, where the gate still applies" \
	"$(q "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam WHERE c.relname='vmvictim';")" "pgcolumnar"
check "premise: the heap fixture really is heap, or the #748 arm is vacuous" \
	"$(q "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam WHERE c.relname='vmheap';")" "heap"
check "no all-visible bit is set yet" \
	"$(q "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass,0);")" "f"
check "the fixture spans more than one block" \
	"$([ "${nblocks:-0}" -gt 1 ] && echo yes || echo "no (blocks=${nblocks:-unset})")" "yes"

# tail -1 is not cosmetic. psql -At -c with several statements echoes a command
# tag per statement, so these helpers returned "SET\nSET\n20000". check_num then
# refused the value as "not a measurement" (correct), and worse, the equality
# check between the two comparing whole multi-line strings PASSED against unfixed
# code -- a check that could not fail, found by running the red step and reading
# it rather than by trusting the exit status.
# On the HEAP fixture. A columnar relation plans as Custom Scan
# (PgColumnarScan) and offers no Index Only Scan to corrupt, and the heap table
# is where the original blast radius was, so this is where the consequence is
# still observable.
ios_count() {	# the count as an index-only scan sees it
	q "SET enable_seqscan=off; SET enable_bitmapscan=off;
	          SELECT count(*) FROM vmheap WHERE id > 0;" | tail -1
}
seq_count() {	# the same count with the index taken away
	q "SET enable_indexonlyscan=off; SET enable_indexscan=off;
	          SELECT count(*) FROM vmheap WHERE id > 0;" | tail -1
}

plan="$(psql_run "SET enable_seqscan=off; SET enable_bitmapscan=off;
                  EXPLAIN (COSTS OFF) SELECT count(*) FROM vmheap WHERE id > 0;")"
check "premise: the query under test really is an Index Only Scan" \
	"$(grep -qi 'Index Only Scan' <<<"$plan" && echo yes || echo no)" "yes"
check_num "premise: the index-only scan is correct BEFORE anything is attempted" \
	"$(ios_count)" 10000

# ---- the boundary --------------------------------------------------------------
# LAYER 1 -- the C ownership check. t_vmexec holds EXECUTE, so a refusal here
# cannot be the REVOKE and cannot be the schema.
attack_out=""
for ((b = 0; b < nblocks; b++)); do
	attack_out+="$(run_as t_vmexec "SELECT pgcolumnar.vm_selftest('vmvictim'::regclass, $b);")"$'\n'
done
check "the granted-but-not-owner role reaches the C gate, not the SQL grant" \
	"$(grep -qi 'permission denied for function' <<<"$attack_out" && echo yes || echo no)" "no"
check "the C ownership check names ownership" \
	"$(grep -qi 'must be owner' <<<"$attack_out" && echo yes || echo no)" "yes"

# LAYER 2 -- the SQL REVOKE, tested on a role that was never granted EXECUTE.
revoke_out="$(run_as t_vmnone "SELECT pgcolumnar.vm_selftest('vmvictim'::regclass, 0);")"
check "a role with only schema USAGE is stopped by the REVOKE" \
	"$(grep -qi 'permission denied for function' <<<"$revoke_out" && echo yes || echo no)" "yes"

# Refused, and refused by the OWNERSHIP gate specifically. Matching on the error
# text rather than on "it failed" keeps a schema-usage or parse error from being
# read as a successful defence.
check "an unprivileged role is refused by vm_selftest" \
	"$(grep -qiE 'must be owner|permission denied' <<<"$attack_out" && echo yes || echo no)" "yes"
check "vm_selftest did not report success to the unprivileged role" \
	"$(grep -qE '^\s*t\s*$' <<<"$attack_out" && echo yes || echo no)" "no"

# The consequence. This is the assertion the issue is actually about.
check "no all-visible bit was set on block 0 by the unprivileged role" \
	"$(q "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass,0);")" "f"
check_num "the index-only scan still returns only live rows" "$(ios_count)" 10000
check "index-only and sequential scans agree after the attempt" \
	"$([ "$(ios_count)" = "$(seq_count)" ] && echo yes || echo no)" "yes"

# vm_is_visible is the read-only sibling and had the same absence of checks.
isvis_out="$(run_as t_vmexec "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass, 0);")"
check "an unprivileged role is refused by vm_is_visible" \
	"$(grep -qiE 'must be owner|permission denied' <<<"$isvis_out" && echo yes || echo no)" "yes"

# ---- the other direction: the owner must still be able to use them -------------
# Without this the suite would pass if the fix simply broke both functions.
# ON A TABLE OF ITS OWN (#1341): on vmvictim, an attack that got through had already
# set block 0's bit, so the owner's call reported f and this control reddened for the
# attack, not for the owner.
psql_run "CREATE TABLE vmown (id int) USING pgcolumnar;
          INSERT INTO vmown SELECT g FROM generate_series(1, 20000) g;
          DELETE FROM vmown WHERE id % 2 = 0;"
check "the owner can still call vm_selftest" \
	"$(q "SELECT pgcolumnar.vm_selftest('vmown'::regclass, 0);")" "t"
check "the owner can still call vm_is_visible" \
	"$(q "SELECT pgcolumnar.vm_is_visible('vmown'::regclass, 0);")" "t"

# ---- #748: a non-columnar relation is refused, whoever asks ---------------------
#
# The original defect reached every table in the database. Ownership alone did not
# close that: the OWNER of a heap table could still mark its pages all-visible
# through these functions, and a wrongly-set all-visible bit makes an index-only
# scan skip the heap visibility check. The relation-type gate closes it for every
# caller.
#
# SQLSTATE and not message text: 42809 is wrong_object_type, raised in C. A grep
# for "not a columnar table" would also be satisfied by the plpgsql guard on
# set_options, which raises a different SQLSTATE for the same sentence.
sqlstate_of() {	# sqlstate_of <sql> -> SQLSTATE=xxxxx, or empty when it succeeded
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=0 \
		-c "DO \$p\$ BEGIN PERFORM $1; EXCEPTION WHEN OTHERS THEN
		     RAISE NOTICE 'SQLSTATE=%', SQLSTATE; END \$p\$;" 2>&1 |
		grep -oE 'SQLSTATE=[0-9A-Z]{5}' | head -1
}
check "vm_selftest refuses a heap relation even for its owner (#748)" \
	"$(sqlstate_of "pgcolumnar.vm_selftest('vmheap'::regclass, 0)")" "SQLSTATE=42809"
check "vm_is_visible refuses a heap relation even for its owner (#748)" \
	"$(sqlstate_of "pgcolumnar.vm_is_visible('vmheap'::regclass, 0)")" "SQLSTATE=42809"

# The consequence, not just the error: nothing was written to the heap fork, so
# the index-only scan still agrees with the sequential one.
# CONTROLS, and labelled as such because they do not discriminate. The owner
# sweeps every block, which on unfixed code is permitted and does set the bits --
# but the index-only scan still returned 10000 on both arms when I ran it, so
# these stay green with the fix reverted and are NOT the removal proof. The two
# SQLSTATE checks above are. They earn their place as a guard against a future
# change that refuses the call while still writing to the fork, which is the
# shape of the original defect; they do not prove this one.
heap_blocks="$(q "SELECT pg_relation_size('vmheap')/8192;")"
for b in $(seq 0 $(( ${heap_blocks:-1} - 1 ))); do
	q "SELECT pgcolumnar.vm_selftest('vmheap'::regclass, $b);" >/dev/null 2>&1
done
check "premise: the heap sweep covered more than one block" \
	"$([ "${heap_blocks:-0}" -gt 1 ] && echo yes || echo "no (heap blocks=${heap_blocks:-unset})")" "yes"
check "the heap index-only scan still returns only live rows after the sweep (#748)" \
	"$(ios_count)" "10000"
check "and it still agrees with the sequential scan (#748)" \
	"$(ios_count)" "$(seq_count)"

# ran_ok ROLE SQL -> "ok" only when psql ran SQL as ROLE and exited 0, "failed" otherwise.
# Positive evidence (#1381, #1400 review): counting ERROR lines, or reading an empty
# SQLSTATE as success, also passes a session that never connected.
ran_ok() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -qtA -v ON_ERROR_STOP=1 -c "$2" >/dev/null 2>&1 && echo ok || echo failed
}
# And the positive control, because a guard that refused everything would satisfy
# both arms above: the same call on the columnar victim is accepted.
check "the same call on a columnar relation is still accepted (#748 control)" \
	"$(ran_ok postgres "SELECT pgcolumnar.vm_is_visible('vmvictim'::regclass, 0)")" "ok"

# ---- coverage ------------------------------------------------------------------
# A future vm_* entry point that forgets the gate should turn this red rather than
# slip past, the same argument test/server_file_privilege.sh makes.
discovered="$(grep -oE 'CREATE FUNCTION pgcolumnar\.(vm_[a-z_]+)' "$SQLFILE" \
	| sed 's/.*pgcolumnar\.//' | sort -u)"
covered=" vm_selftest vm_is_visible "
for fn in $discovered; do
	case "$covered" in
		*" $fn "*) r=yes ;;
		*)         r=no  ;;
	esac
	check "vm entry point is covered by this boundary test: $fn" "$r" "yes"
done
check_num "coverage scan found the vm functions (not an empty scan)" \
	"$(printf '%s\n' "$discovered" | grep -c .)" 2

pgc_summary

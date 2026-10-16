#!/usr/bin/env bash
#
# REPACK on a columnar table (issue #399). PostgreSQL 19 and later only.
#
# REPACK replaces CLUSTER and VACUUM FULL in 19. It is not a new table-AM
# callback: it reuses the CLUSTER machinery, which dispatches a rewrite through
# relation_copy_for_cluster. design/PG18_19_OPPORTUNITIES.md concluded from that
# dispatch that REPACK "should work" on a columnar table, because pgColumnar
# registers that callback.
#
# It does not work. The callback is registered and is a stub that raises, so the
# reasoning was true of the symbol and false of the behaviour. That is the gap a
# suite closes and a grep does not, and it is why this file exists.
#
# What is asserted here is therefore the ACTUAL behaviour, not the hoped-for one:
# every spelling of the command errors, it errors naming the command the user
# typed, the table is unharmed afterwards, and the supported alternative works.
# If someone later implements relation_copy_for_cluster, these checks fail and
# should be rewritten to assert success. That is the intended signal.
#
# The heap control is not decoration. Without it, "REPACK fails here" cannot be
# told apart from "REPACK does not work on this build at all".
#
# REPACK (CONCURRENTLY) needs two fixtures for the same reason, and the first
# version of this suite got it wrong. Without an identity index both access
# methods are refused before the AM is reached, so asserting that failure alone
# passes for a reason that has nothing to do with us, and would keep passing if
# our behaviour changed. With a primary key and wal_level=logical, heap succeeds
# and columnar raises our error, so the concurrent form IS a columnar behaviour.
#
# Usage:  test/native_repack.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# REPACK (CONCURRENTLY) needs logical decoding of the relation's changes, so the
# cluster must be able to provide it. Without this the concurrent form fails for a
# reason that has nothing to do with the access method.
PGC_EXTRA_CONF="wal_level=logical
max_locks_per_transaction=1024
max_wal_senders=10
max_replication_slots=10"
pgc_setup "${1:-/usr/local/pg19/bin/pg_config}"

# Version gate, and it must be VISIBLE. A suite that silently passes on 15 to 18
# is the failure mode this project keeps finding.
srv="$(q 'SHOW server_version_num')"
# An empty answer is a cluster that stopped talking, not PostgreSQL 18. ${srv:-0}
# made those identical, so a dead postmaster reported SKIPPED and the major passed.
#
# The failure is raised only when it happens, rather than as a check that always
# runs: a passing check would put PGC_CHECKS at 1, and this suite's whole verdict
# on an older major depends on it being 0. Asserting the premise must not destroy
# the skip it guards.
if ! pgc_is_number "$srv"; then
	pgc_fail "the server did not answer 'SHOW server_version_num'" "got [$srv]"
	pgc_summary
fi
if [ "$srv" -lt 190000 ]; then
	check_skip "REPACK" "SKIP  REPACK requires PostgreSQL 19 (server_version_num=$srv)" "requires PostgreSQL 19"
	pgc_summary
fi

ROWS=${PGC_REPACK_ROWS:-20000}

psql_run "CREATE TABLE rp (id int, v text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('rp', stripe_row_limit => 5000, compression => 'zstd');
	INSERT INTO rp SELECT g, 'x'||g FROM generate_series(1,$ROWS) g;
	DELETE FROM rp WHERE id % 3 = 0;
	CREATE INDEX rp_id ON rp (id);
	CREATE TABLE rp_heap (id int, v text);
	INSERT INTO rp_heap SELECT g, 'x'||g FROM generate_series(1,$ROWS) g;
	CREATE INDEX rp_heap_id ON rp_heap (id);"

live=$(q "SELECT count(*) FROM rp")
hash_before=$(q "SELECT md5(string_agg(id::text||':'||v, ',' ORDER BY id)) FROM rp")
opts_before=$(q "SELECT stripe_row_limit || '/' || compression FROM pgcolumnar.options o
	JOIN pg_class c ON c.oid = o.regclass WHERE c.relname = 'rp'")

dead_sql="SELECT coalesce(sum(deleted_count), 0) FROM pgcolumnar.delete_vector WHERE storage_id = pgcolumnar.get_storage_id('rp')"
check "premise: the table holds deleted rows for the alternative to reclaim" \
	"$(q "$dead_sql")" "$(( ROWS / 3 ))"

err_of() { psql_run "$1" 2>&1 | grep -oE 'ERROR:.*' | head -1; }

# ---- every spelling errors, and the message names the command ----------------
e="$(err_of 'REPACK rp;')"
check "REPACK on a columnar table errors (#399)" \
	"$(case "$e" in *ERROR*) echo yes ;; *) echo "no (succeeded)" ;; esac)" "yes"
check "and the error names REPACK, which is what the user typed (#399)" \
	"$(case "$e" in *REPACK*) echo yes ;; *) echo "no ($e)" ;; esac)" "yes"

check "REPACK ... USING INDEX errors the same way" \
	"$(case "$(err_of 'REPACK rp USING INDEX rp_id;')" in *"not supported"*) echo yes ;; *) echo no ;; esac)" "yes"
check "REPACK (VERBOSE) errors the same way" \
	"$(case "$(err_of 'REPACK (VERBOSE) rp;')" in *"not supported"*) echo yes ;; *) echo no ;; esac)" "yes"
check "CLUSTER errors the same way" \
	"$(case "$(err_of 'CLUSTER rp USING rp_id;')" in *"not supported"*) echo yes ;; *) echo no ;; esac)" "yes"
check "VACUUM FULL errors the same way" \
	"$(case "$(err_of 'VACUUM FULL rp;')" in *"not supported"*) echo yes ;; *) echo no ;; esac)" "yes"

# ---- REPACK (CONCURRENTLY), which needs two fixtures to mean anything ---------
# "REPACK CONCURRENTLY" without parentheses is not syntax; it is an option.
#
# The concurrent form has a precondition: the relation needs an identity index. On
# a table without one BOTH access methods fail, and the failure says so. Asserting
# only that is a trap, because the assertion passes for the wrong reason and would
# keep passing if the columnar behaviour changed. It is kept below, labelled as
# what it is, and paired with a fixture that MEETS the precondition.
# The distinction is the assertion: without an identity index the error is
# PostgreSQL's own precondition ("cannot execute REPACK (CONCURRENTLY) on
# relation"), raised before the access method is consulted. With one, below, the
# error is ours. Two different failures that a single "it errors" check would
# conflate.
nopk="$(err_of 'REPACK (CONCURRENTLY) rp;')"
check "without an identity index it is refused by PostgreSQL, not by us" \
	"$(case "$nopk" in
		*"not supported"*)          echo "no (ours: $nopk)" ;;
		*"cannot execute REPACK"*)  echo yes ;;
		*)                          echo "no ($nopk)" ;;
	esac)" \
	"yes"

psql_run "CREATE TABLE rp_pk (id int PRIMARY KEY, v text) USING pgcolumnar;
	INSERT INTO rp_pk SELECT g, 'x'||g FROM generate_series(1,$ROWS) g;
	CREATE TABLE rp_pk_heap (id int PRIMARY KEY, v text);
	INSERT INTO rp_pk_heap SELECT g, 'x'||g FROM generate_series(1,$ROWS) g;"
pk_rows="$(q 'SELECT count(*) FROM rp_pk')"
pk_hash="$(q "SELECT md5(string_agg(id::text||':'||v, ',' ORDER BY id)) FROM rp_pk")"

# With the precondition met, heap succeeds. So the concurrent form IS reachable on
# this build, and a columnar failure is not a missing primary key.
check "control: with an identity index, REPACK (CONCURRENTLY) succeeds on heap" \
	"$(psql_run 'REPACK (CONCURRENTLY) rp_pk_heap;' 2>&1 | grep -c 'ERROR' || true)" "0"

# WHO REFUSES CHANGED IN POSTGRESQL 19beta4 (#1337). From 19beta4, core restricts
# REPACK (CONCURRENTLY) to the heap access method (repack.c: "This operation is only
# supported for the "heap" access method.", 0A000), before the access method is
# consulted. Earlier 19 betas reach our refusal instead. Either is a refusal of the
# concurrent form on a columnar table, for a stated reason; anything else -- a
# success, or another error -- is not. The cause is read from the DETAIL, because
# both of core's REPACK refusals share one ERROR line.
c_pk="$(psql_run 'REPACK (CONCURRENTLY) rp_pk;' 2>&1 | grep -oE '(ERROR|DETAIL):.*' | tr '\n' ' ')"
check "and on a columnar table it is refused: by core's heap-only check from 19beta4, else by ours" \
	"$(case "$c_pk" in
		*"not supported yet"*)                                   echo yes ;;
		*'only supported for the "heap" access method'*)         echo yes ;;
		*)                                                       echo "no (${c_pk:-succeeded})" ;;
	esac)" "yes"

# A half-finished concurrent rewrite would be the bad outcome. It does not happen.
check "the columnar table is undamaged after the refused concurrent repack: rows" \
	"$(q 'SELECT count(*) FROM rp_pk')" "$pk_rows"
check "the columnar table is undamaged after the refused concurrent repack: content" \
	"$(q "SELECT md5(string_agg(id::text||':'||v, ',' ORDER BY id)) FROM rp_pk")" "$pk_hash"

# ---- the control: the same command works on heap -----------------------------
check "control: REPACK works on a heap table on this build" \
	"$(psql_run 'REPACK rp_heap;' 2>&1 | grep -c ERROR || true)" "0"

# ---- a failed rewrite must leave the table untouched -------------------------
check "the columnar table is unharmed: row count" "$(q 'SELECT count(*) FROM rp')" "$live"
check "the columnar table is unharmed: content hash" \
	"$(q "SELECT md5(string_agg(id::text||':'||v, ',' ORDER BY id)) FROM rp")" "$hash_before"
check "the columnar table is unharmed: storage options" \
	"$(q "SELECT stripe_row_limit || '/' || compression FROM pgcolumnar.options o
	      JOIN pg_class c ON c.oid = o.regclass WHERE c.relname = 'rp'")" "$opts_before"
check "the columnar table is unharmed: access method" \
	"$(q "SELECT amname FROM pg_am a JOIN pg_class c ON c.relam = a.oid WHERE c.relname = 'rp'")" \
	"pgcolumnar"

# ---- and the supported route does work ---------------------------------------
# "Works" is that it reclaims, not only that it returns (#1341): with vacuum()
# returning before it rewrote anything, every arm here stayed green. The rows the
# fixture deleted are in storage when it is made (asserted there, before any REPACK
# can touch the table), and gone after.
check "pgcolumnar.vacuum is the supported alternative and succeeds" \
	"$(psql_run "SELECT pgcolumnar.vacuum('rp');" 2>&1 | grep -c ERROR || true)" "0"
check "and it reclaimed the deleted rows" "$(q "$dead_sql")" "0"
check "and it kept every live row" "$(q 'SELECT count(*) FROM rp')" "$live"
check "and the content is still identical" \
	"$(q "SELECT md5(string_agg(id::text||':'||v, ',' ORDER BY id)) FROM rp")" "$hash_before"

pgc_summary

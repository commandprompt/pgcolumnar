#!/usr/bin/env bash
#
# The debug metadata mutators are owner-only (#707).
#
# pgcolumnar_debug_advance_reserved_offset and
# pgcolumnar_debug_set_metapage_version ship UNBOUND, but a binding is one
# CREATE FUNCTION away -- native_gap.sh and native_format.sh do exactly that --
# and both mutate storage metadata: one burns reserved file space, the other
# overwrites the metapage format version and bricks every subsequent read of
# the table. Before #707 a binding exposed them to any role that could execute
# the function, with no ownership gate at all.
#
# This suite binds them the way the test suites do and probes as a NON-OWNER
# role that runs its own statements (a premise about a role is only proved by
# that role running it). The refusal is asserted as SQLSTATE 42501 -- which
# aclcheck_error raises -- plus the "must be owner" message, because a 42501
# could also be an EXECUTE denial and the message is what pins the owner gate.
# The functions are bound in public (PUBLIC keeps EXECUTE on new functions),
# so the only gate standing between the peon and the mutation is the one under
# test. The mirror arms prove the gate is ownership, not superuser-ness: the
# peon may still mutate its OWN table.
#
# Usage:  test/debug_hook_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
psql_run "CREATE FUNCTION public.dbg_advance(regclass, int)
  RETURNS void AS 'pgcolumnar', 'pgcolumnar_debug_advance_reserved_offset'
  LANGUAGE C;
CREATE FUNCTION public.dbg_setver(regclass, int, int)
  RETURNS void AS 'pgcolumnar', 'pgcolumnar_debug_set_metapage_version'
  LANGUAGE C;
CREATE ROLE peon LOGIN;
GRANT CREATE, USAGE ON SCHEMA public TO peon;
CREATE TABLE owned (id int) USING pgcolumnar;
INSERT INTO owned VALUES (1), (2);
CREATE TABLE owned_scratch (id int) USING pgcolumnar;
INSERT INTO owned_scratch VALUES (1);"

# SQLSTATE of a statement run AS A GIVEN ROLE; empty when it succeeds.
sqlstate_as() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
$2;
SQLEOF
}
# ran_ok ROLE SQL -> "ok" only when psql ran SQL as ROLE and exited 0, "failed" otherwise.
# Positive evidence (#1381, #1400 review): counting ERROR lines, or reading an empty
# SQLSTATE as success, also passes a session that never connected.
ran_ok() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -qtA -v ON_ERROR_STOP=1 -c "$2" >/dev/null 2>&1 && echo ok || echo failed
}
msg_as() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -qtA -c "$2" 2>&1 | grep -c 'must be owner'
}

# --- premises: bound, callable, and the role really runs its statements ------
# An arm, not a premise: a hook that refused every caller reddens it (#1381 review).
# Expected as "ok" rather than an empty SQLSTATE, so an empty capture cannot pass.
check "the owner can call the advance hook (no-op advance)" \
	"$(ran_ok postgres "SELECT public.dbg_advance('owned', 0)")" "ok"

check "premise: the peon role runs its own statements" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U peon -d "$PGC_DB" -qtA -c 'SELECT current_user;')" \
	"peon"

# --- the gate: a non-owner is refused by ownership, before any mutation ------
check "peon cannot advance another owner's reserved offset (42501)" \
	"$(sqlstate_as peon "SELECT public.dbg_advance('owned', 1)")" "42501"
check "and the refusal is the owner gate, not an EXECUTE denial" \
	"$(msg_as peon "SELECT public.dbg_advance('owned', 1)")" "1"

check "peon cannot overwrite another owner's metapage version (42501)" \
	"$(sqlstate_as peon "SELECT public.dbg_setver('owned', 99, 0)")" "42501"
check "and that refusal is the owner gate too" \
	"$(msg_as peon "SELECT public.dbg_setver('owned', 99, 0)")" "1"

# The gated table is intact: readable, and its rows survived the attempts.
check "the gated table still reads (version untouched by the refused calls)" \
	"$(q 'SELECT count(*) FROM owned;')" "2"

# --- mirrors: the gate is ownership, not superuser-ness ----------------------
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO peon;" >/dev/null 2>&1
check "peon may advance its OWN table (gate is ownership)" \
	"$(ran_ok peon "CREATE TABLE peon_t (id int) USING pgcolumnar;
	                 SELECT public.dbg_advance('peon_t', 0);")" "ok"

# No DROP after the brick: dropping a columnar table re-reads its metapage,
# so a bricked version makes the DROP itself fail. The scratch table stays,
# bricked, in this throwaway cluster -- which is itself a demonstration of why
# these hooks are owner-only.
check "peon may even brick its OWN scratch table's version (gate is ownership)" \
	"$(ran_ok peon "CREATE TABLE peon_scratch (id int) USING pgcolumnar;
	                 SELECT public.dbg_setver('peon_scratch', 99, 0);")" "ok"

check "backend alive" "$(q 'SELECT 1;')" "1"

pgc_summary

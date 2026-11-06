#!/usr/bin/env bash
#
# pgColumnar: a NOLOGIN role's background workers connect and act as that role (#1585).
#
# parallel_export_parquet, parallel_copy and the parallel flush each start background
# workers for the calling role. A worker used to connect AS that role, and before
# PostgreSQL 17 a background worker cannot connect as a role without LOGIN: there is no
# BGWORKER_BYPASS_ROLELOGINCHECK. So after SET ROLE to a NOLOGIN group role, on PG15 and
# PG16, the export failed "worker 0 failed: worker exited without reporting a result",
# parallel_copy failed "coordinator exited without reporting a result", and the parallel
# flush silently encoded every column serially. The server log said why:
# 'role "..." is not permitted to log in'.
#
# A worker now connects as the session's authenticated user, which logged in, and then
# takes the caller's user id, as core's parallel workers do. The second half matters as
# much as the first: a worker that connected as the authenticated user and stopped there
# would run the caller's COPY as that user, typically a superuser. The row trigger on
# nw_dst records current_user inside the copy worker, which is what catches that.
#
# Usage:  test/nologin_workers.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# parallel_copy prepares one transaction per worker.
export PGC_EXTRA_CONF=$'max_prepared_transactions=4\nmax_worker_processes=16'
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=2000
outdir="$PGC_WORKDIR/nw_dir"
infile="$PGC_WORKDIR/nw_in.txt"
seq 1 "$ROWS" | sed 's/$/\t/' >"$infile"
chmod 644 "$infile"

psql_run "DROP ROLE IF EXISTS t_nl;"
psql_run "DROP ROLE IF EXISTS t_nl_other;"
psql_run "CREATE ROLE t_nl NOSUPERUSER NOLOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_nl;"
psql_run "GRANT pg_read_server_files, pg_write_server_files TO t_nl;"
psql_run "CREATE TABLE nw_src (a int, b text) USING pgcolumnar;"
psql_run "INSERT INTO nw_src SELECT g, 'x' || g FROM generate_series(1, $ROWS) g;"
# The trigger runs inside the copy worker, so it records the worker's current_user.
psql_run "CREATE TABLE nw_dst (a int, who text) USING pgcolumnar;"
psql_run "CREATE FUNCTION nw_stamp() RETURNS trigger LANGUAGE plpgsql
          AS \$\$BEGIN NEW.who := current_user; RETURN NEW; END\$\$;"
psql_run "CREATE TRIGGER nw_stamp BEFORE INSERT ON nw_dst FOR EACH ROW EXECUTE FUNCTION nw_stamp();"
psql_run "CREATE TABLE nw_wide (a int, b int, c int) USING pgcolumnar;"
# A trigger that resets the role inside the copy worker (#1612 review). The worker must
# not end up as some other user: its rows would be written as that user, and its
# prepared transaction would belong to a user the leader cannot finish it as.
psql_run "CREATE TABLE nw_esc (a int, who text) USING pgcolumnar;"
psql_run "CREATE FUNCTION nw_reset_stamp() RETURNS trigger LANGUAGE plpgsql
          AS \$\$BEGIN
              BEGIN PERFORM set_config('role', 'none', true);
              EXCEPTION WHEN OTHERS THEN NULL; END;
              NEW.who := current_user; RETURN NEW;
          END\$\$;"
psql_run "CREATE TRIGGER nw_reset_stamp BEFORE INSERT ON nw_esc FOR EACH ROW EXECUTE FUNCTION nw_reset_stamp();"
# A trigger that switches to ANOTHER role the caller belongs to: the worker would then
# PREPARE as that role, which the coordinator, running as the caller, cannot finish.
psql_run "CREATE ROLE t_nl_other NOSUPERUSER NOLOGIN;"
psql_run "GRANT t_nl_other TO t_nl;"
psql_run "CREATE TABLE nw_sw (a int, who text) USING pgcolumnar;"
psql_run "CREATE FUNCTION nw_switch_stamp() RETURNS trigger LANGUAGE plpgsql
          AS \$\$BEGIN PERFORM set_config('role', 't_nl_other', true);
              NEW.who := current_user; RETURN NEW; END\$\$;"
psql_run "CREATE TRIGGER nw_switch_stamp BEFORE INSERT ON nw_sw FOR EACH ROW EXECUTE FUNCTION nw_switch_stamp();"
psql_run "GRANT SELECT, INSERT ON nw_sw TO t_nl_other;"
psql_run "CREATE TABLE nw_def (a int, who text) USING pgcolumnar;"
psql_run "CREATE TRIGGER nw_def_stamp BEFORE INSERT ON nw_def FOR EACH ROW EXECUTE FUNCTION nw_reset_stamp();"
# The definer: a superuser session calls it, and inside it the caller is t_nl under a
# security-restricted context, which must hold in the workers too.
psql_run "CREATE FUNCTION nw_definer_copy(p text) RETURNS bigint LANGUAGE sql SECURITY DEFINER
          AS \$\$SELECT pgcolumnar.parallel_copy('nw_def'::regclass, p, 2)\$\$;"
psql_run "ALTER FUNCTION nw_definer_copy(text) OWNER TO t_nl;"
psql_run "GRANT SELECT ON nw_src TO t_nl;"
psql_run "GRANT SELECT, INSERT ON nw_dst, nw_wide, nw_esc, nw_def, nw_sw TO t_nl;"

as_nl() {	# sql -> its output as t_nl, the debug messages included, after the role took
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -X -qAt -v ON_ERROR_STOP=1 -c "SET ROLE t_nl;" \
		-c "SELECT 'as:' || current_user;" -c "$1" 2>&1
}
refusals() {	# -> lines in the server log refusing t_nl a login
	pgc_pg "grep -c 'role \"t_nl\" is not permitted to log in' '$PGC_LOGFILE'" 2>/dev/null || echo 0
}

# ---- premises: the role cannot log in, and the log records it when it tries ----
check "premise: the NOLOGIN role cannot open a session" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_nl -d "$PGC_DB" -c 'SELECT 1' 2>&1 |
		grep -c 'is not permitted to log in')" "1"
check "premise: the server log records the NOLOGIN role's refused login" "$(refusals)" "1"
check "premise: SET ROLE to the NOLOGIN role takes" "$(as_nl 'SELECT 1;' | head -1)" "as:t_nl"

# ---- parallel_export_parquet ----------------------------------------------------
reply="$(as_nl "SELECT pgcolumnar.parallel_export_parquet('nw_src'::regclass, '$outdir', 2);")"
check_num "a NOLOGIN role's parallel_export_parquet exports every row" \
	"$(sed -n 2p <<<"$reply")" "$ROWS"

# ---- parallel_copy ----------------------------------------------------------------
reply="$(as_nl "SELECT pgcolumnar.parallel_copy('nw_dst'::regclass, '$infile', 2);")"
check_num "a NOLOGIN role's parallel_copy loads every row" "$(sed -n 2p <<<"$reply")" "$ROWS"
check "parallel_copy's workers wrote every row as the NOLOGIN role" \
	"$(q "SELECT string_agg(DISTINCT coalesce(who, '<null>'), ',') FROM nw_dst;")" "t_nl"

# ---- a trigger that resets the role inside the copy worker (#1612 review) -----------
# On PG17+ a worker that connected as the authenticated user became that user on RESET
# ROLE, PREPAREd as it, and left the leader "permission denied to finish prepared
# transaction" with two orphaned prepared transactions holding locks.
reply="$(as_nl "SELECT pgcolumnar.parallel_copy('nw_esc'::regclass, '$infile', 2);")"
check "a copy trigger that resets the role still writes every row as the NOLOGIN role" \
	"$(q "SELECT count(*) || ':' || coalesce(string_agg(DISTINCT coalesce(who, '<null>'), ','), '') FROM nw_esc;")" "$ROWS:t_nl"
check_num "and that copy leaves no prepared transaction behind" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts;")" "0"
q "SELECT 'ROLLBACK PREPARED ' || quote_literal(gid) FROM pg_prepared_xacts" | while read -r _rb; do q "$_rb" >/dev/null; done

# ---- a trigger that switches to another role is refused before it prepares ---------
reply="$(as_nl "SELECT pgcolumnar.parallel_copy('nw_sw'::regclass, '$infile', 2);")"
check "a copy trigger that switches to another role is refused before the worker prepares" \
	"$(grep -q 'no longer running as the role that called parallel_copy' <<<"$reply" && echo refused || echo "not refused: $(sed -n 2p <<<"$reply")")" "refused"
check_num "and the refused copy leaves no prepared transaction behind" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts;")" "0"
q "SELECT 'ROLLBACK PREPARED ' || quote_literal(gid) FROM pg_prepared_xacts" | while read -r _rb; do q "$_rb" >/dev/null; done

# ---- a copy called from a SECURITY DEFINER function keeps its restriction -----------
# The worker takes the leader's security context with its user id. Without it, the
# trigger's role reset succeeds inside the worker and runs as the superuser session.
q "SELECT nw_definer_copy('$infile');" >/dev/null
check "a copy run by a SECURITY DEFINER function keeps the definer's restriction in its workers" \
	"$(q "SELECT count(*) || ':' || coalesce(string_agg(DISTINCT coalesce(who, '<null>'), ','), '') FROM nw_def;")" "$ROWS:t_nl"

# ---- the parallel flush ------------------------------------------------------------
reply="$(as_nl "SET pgcolumnar.parallel_flush = on; SET client_min_messages = debug1;
                INSERT INTO nw_wide SELECT g, g, g FROM generate_series(1, $ROWS) g;")"
check "the flush is dispatched to workers" \
	"$(grep -o 'parallel_flush dispatch: .* -> [a-z]*' <<<"$reply" | sed 's/.* -> //' | head -1)" "parallel"
check "a NOLOGIN role's parallel flush encodes every column in a worker" \
	"$(grep -o 'parallel_flush: [0-9]* of [0-9]* columns' <<<"$reply" | head -1)" "parallel_flush: 3 of 3 columns"
check_num "and the table holds every row it flushed" "$(q 'SELECT count(*) FROM nw_wide;')" "$ROWS"

# ---- and no worker was refused on the way ------------------------------------------
check "no worker was refused a login as the NOLOGIN role" "$(refusals)" "1"

pgc_summary

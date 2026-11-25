# ---- a documented SQL block fails on psql's exit status, not on a grep (#2093) --
#
# native_cluster.sh, parquet_import.sh and rollup.sh run the SQL examples in
# docs/ as written and expect "ok". Each had its own copy of one line: pipe
# psql's stderr into `grep -m1 -E 'ERROR|FATAL'` and say "ok" when grep found
# nothing. psql's exit status was thrown away.
#
# A backend crash prints "server closed the connection unexpectedly" and psql
# exits 2. That text holds neither word, so the check said "ok" while the
# example had killed the server. OffgridwithJD measured it on #2159: a how-to
# block that sends SIGSEGV to its own backend gave native_cluster 18 passed, 0
# failed. A port with no server, and a failed meta-command such as `\i` on a
# missing file, gave "ok" the same way: psql writes "error", in lower case.
#
# So the three suites now share one helper, psql_block in lib.sh, and it reads
# psql's exit status. The arms below run it on this suite's own cluster (part
# 020 started it). A crash is not one of them: it would restart the cluster
# every later part uses. The crash case is the same exit-status path as the
# missing server (psql rc=2), and native_cluster.sh's doc checks cover it.

check "control: psql_block says ok for a block that runs" \
	"$(psql_block "SELECT 1;
SELECT 2;")" "ok"

_pb_err="$(psql_block "SELECT 1;
SELECT no_such_column FROM pg_class;")"
check "psql_block names the error of a block whose statement fails" \
	"$(case "$_pb_err" in "psql rc=3: "*"ERROR:"*no_such_column*) echo named ;; *) echo "$_pb_err" ;; esac)" "named"

_pb_meta="$(psql_block "\\i $PGC_WORKDIR/no_such_file_2093.sql
SELECT 1;")"
check "psql_block does not say ok when a meta-command fails" \
	"$(case "$_pb_meta" in "psql rc=3: "*no_such_file_2093*) echo refused ;; *) echo "$_pb_meta" ;; esac)" "refused"

# A port inside this run's band that nothing listens on. pgc_port_free is the
# harness's own test for that, so the arm does not guess.
_pb_saved_port="$PGC_PORT"
_pb_free=""
for _pb_try in $(seq 1 50); do
	_pb_cand=$(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))
	if pgc_port_free "$_pb_cand"; then _pb_free="$_pb_cand"; break; fi
done
check "premise: a port with no server on it was found" \
	"$([ -n "$_pb_free" ] && echo yes || echo no)" "yes"
PGC_PORT="${_pb_free:-1}"
_pb_gone="$(psql_block "SELECT 1;")"
PGC_PORT="$_pb_saved_port"
check "psql_block does not say ok when no server answers" \
	"$(case "$_pb_gone" in "psql rc=2: "*) echo refused ;; *) echo "$_pb_gone" ;; esac)" "refused"

# The three suites must use the helper, so a copy of the old grep cannot come
# back in one of them. Read from each suite's run_doc body.
_pb_users=""
for _pb_s in native_cluster parquet_import rollup; do
	_pb_body="$(awk '/^run_doc\(\)/ {inb = 1} inb {print} inb && /^}/ {exit}' "$PGC_TESTDIR/$_pb_s.sh")"
	case "$_pb_body" in
	*psql_block*) case "$_pb_body" in *"grep -m1 -E 'ERROR|FATAL'"*) _pb_users="$_pb_users $_pb_s:grep" ;; esac ;;
	*) _pb_users="$_pb_users $_pb_s:no-psql_block" ;;
	esac
done
check "every suite's run_doc runs its block through psql_block" "${_pb_users:-all}" "all"

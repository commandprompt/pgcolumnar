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
# back in one of them. Two guards, one static and one by behaviour.
#
# The static guard reads each suite's run_doc body with its comments removed,
# and wants a psql_block call and no psql call of its own. The comments go
# first because rollup.sh's `run_doc() {` line carries one that names
# psql_block: when the guard matched the raw text, that comment kept it green
# for any rollup body. OffgridwithJD showed it on #2159 with mutant G1 (rollup's
# old psql | grep pipe put back): rollup.sh passed a backend crash as "ok", and
# this part still said every suite used the helper. A comment here is a `#` at
# the start of a line or after a space, as bash reads one; the `#` inside
# parquet_import's sed expression is neither.
_pb_judge() {	# _pb_judge BODY -> ok, no-psql_block, or psql (a direct psql call)
	local code
	code="$(sed -E 's/(^|[[:space:]])#.*//' <<<"$1")"
	case "$code" in *psql_block*) ;; *) echo no-psql_block; return ;; esac
	if grep -qE '(^|[^[:alnum:]_])psql([^[:alnum:]_]|$)' <<<"$code"; then echo psql; return; fi
	echo ok
}

# The judge on bodies whose answer is known, so each arm of it is seen to fire.
check "the run_doc guard accepts a body that calls psql_block" \
	"$(_pb_judge 'run_doc() {
	psql_block "$b"
}')" "ok"
check "the run_doc guard refuses a body whose only psql_block is in a comment" \
	"$(_pb_judge 'run_doc() {	# runs the block (psql_block, #2093)
	# psql_block used to be called here
	echo ok
}')" "no-psql_block"
check "the run_doc guard refuses a body that also calls psql itself" \
	"$(_pb_judge 'run_doc() {
	[ "$(psql_block "SELECT 1;")" = ok ] || return
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -X -v ON_ERROR_STOP=1 <<<"$b" 2>&1 >/dev/null)"
	echo "${out:-ok}"
}')" "psql"

_pb_users=""
for _pb_s in native_cluster parquet_import rollup; do
	_pb_body="$(awk '/^run_doc\(\)/ {inb = 1} inb {print} inb && /^}/ {exit}' "$PGC_TESTDIR/$_pb_s.sh")"
	_pb_v="$(_pb_judge "$_pb_body")"
	[ "$_pb_v" = ok ] || _pb_users="$_pb_users $_pb_s:$_pb_v"
done
check "every suite's run_doc runs its block through psql_block" "${_pb_users:-all}" "all"

# The behaviour guard runs each suite's real run_doc, with doc_sql and
# psql_block replaced by stubs, in a subshell so the real psql_block above is
# untouched. run_doc must print what psql_block printed, unchanged: "ok" for a
# block that ran, and a crash's "psql rc=2: ..." as it came. This catches a
# run_doc that skips the helper (G1 never reaches the stub), and a native_cluster
# loop that lets some failures through as ok: its `[ "$out" = ok ]` test is the
# one place a body can drop a verdict while still calling psql_block.
# The stub prints its verdict only when it is handed the block doc_sql printed.
# Otherwise it prints "not the doc block", so a run_doc that calls psql_block on
# some other SQL and ignores the block fails here (OffgridwithJD's mutant G4).
_pb_relay() {	# _pb_relay SUITE STUB -> what SUITE's run_doc prints when psql_block prints STUB
	local body
	body="$(awk '/^run_doc\(\)/ {inb = 1} inb {print} inb && /^}/ {exit}' "$PGC_TESTDIR/$1.sh")"
	(
		PQ_DOCDIR="$PGC_WORKDIR"
		_pb_stub="$2"
		doc_sql() { echo "SELECT 'pb650 doc block';"; }
		eval "$body"
		psql_block() {
			case "$1" in
			*"pb650 doc block"*) echo "$_pb_stub" ;;
			*) echo "not the doc block" ;;
			esac
		}
		run_doc '## a heading' 1 2 2>&1
	)
}
_pb_drop=""
for _pb_s in native_cluster parquet_import rollup; do
	for _pb_out in ok "psql rc=2: server closed the connection unexpectedly" "psql rc=3: ERROR:  boom"; do
		_pb_got="$(_pb_relay "$_pb_s" "$_pb_out")"
		[ "$_pb_got" = "$_pb_out" ] || _pb_drop="$_pb_drop $_pb_s:[$_pb_out]->[$_pb_got]"
	done
done
check "every suite's run_doc prints psql_block's verdict unchanged" "${_pb_drop:-all}" "all"

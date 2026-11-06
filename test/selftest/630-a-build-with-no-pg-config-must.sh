# ---- a build with no pg_config must refuse, not wait on stdin (#1344) ---------
#
# The Makefile reads CC out of the server's Makefile.global with
# `sed -n 's/^CC = //p' $(PG_MAKEFILE_GLOBAL)`, and that path comes from
# `$(PG_CONFIG) --pgxs`. When PG_CONFIG names no program (`pg_config` not on
# root's PATH under sudo, or a wrong path), the path is EMPTY and sed reads make's
# stdin. Measured on main at 3677825, `make -s clean PG_CONFIG=/nonexistent/...`:
#
#     stdin an open pipe   no exit until killed (rc 124 under timeout 10)
#     stdin closed         rc 0, nothing cleaned
#
# Two such seds were found on a host, one 22 hours old, the other blocking an
# evidence script at `make clean`. A background job or a CI step has exactly that
# stdin. The rc 0 is the same defect seen from the other side: a build that cannot
# know its server does nothing and reports success.
#
# DRIVEN, NOT READ. make runs on the real Makefile with -n, so the parse -- which is
# where both the probe and the refusal live -- happens in full and nothing is
# built or removed. stdin is a FIFO this part holds open for read and write, so it
# never reaches EOF: the shape of the stdin that hung. GNU timeout kills the
# process group, the sed in make's $(shell) included.
_np_src="${PGC_SRCDIR:-}"

check "premise: the selftest knows where the Makefile it drives is" \
	"$([ -n "$_np_src" ] && [ -f "$_np_src/Makefile" ] && echo yes || echo "no (PGC_SRCDIR=[$_np_src])")" \
	"yes"

_np_tmp="$(mktemp -d)"
mkfifo "$_np_tmp/stdin"
exec {_np_fd}<>"$_np_tmp/stdin"

# The fixture's own proof that this stdin is one a reader waits on: without it, a
# make that "ends" could be ending because its stdin did.
timeout 2 cat <&"$_np_fd" >/dev/null 2>&1
_np_ctl=$?
check_num "premise: a reader of this stdin waits rather than reaching EOF" "$_np_ctl" "124"

timeout 20 make -n -C "$_np_src" clean PG_CONFIG="$_np_tmp/no-such/pg_config" \
	<&"$_np_fd" >"$_np_tmp/out" 2>&1
_np_rc=$?
exec {_np_fd}>&-
echo "      make with no pg_config: rc $_np_rc: $(tr '\n' ' ' <"$_np_tmp/out" | cut -c1-200)"

check "a make with no usable pg_config ends instead of waiting on stdin" \
	"$([ "$_np_rc" != 124 ] && echo yes || echo "no (still running at 20s)")" "yes"
# make's own error line, `Makefile:N: *** ... Stop.`, not any line: -n echoes the
# objstore sub-make's command, which carries PG_CONFIG= whether make refused or not.
check "and the refusal is make's own error, naming PG_CONFIG" \
	"$(grep -E '\*\*\*.*PG_CONFIG' "$_np_tmp/out" >/dev/null && echo yes || echo no)" "yes"

# The other face of the defect: with stdin closed sed finds EOF at once, the parse
# goes on, and make exits 0. A run of its own, so this arm does not ride on the hang.
timeout 20 make -n -C "$_np_src" clean PG_CONFIG="$_np_tmp/no-such/pg_config" \
	</dev/null >"$_np_tmp/out2" 2>&1
_np_rc2=$?
echo "      the same make, stdin closed: rc $_np_rc2"
# THIS refusal, not any non-zero exit: a Makefile that fails to parse exits 2 as
# well, and would satisfy a bare rc test (#1346 review, jdatcmd).
check "and with stdin closed it refuses, rather than exiting 0 having done nothing" \
	"$([ "$_np_rc2" != 0 ] && [ "$_np_rc2" != 124 ] && grep -E '\*\*\*.*PG_CONFIG' "$_np_tmp/out2" >/dev/null \
		&& echo yes || echo "no (rc $_np_rc2: $(grep -m1 -F '***' "$_np_tmp/out2" || echo 'no make error'))")" "yes"
rm -rf "$_np_tmp"

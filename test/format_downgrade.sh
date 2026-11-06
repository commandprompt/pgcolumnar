#!/usr/bin/env bash
#
# pgColumnar format-downgrade gate (#1137).
#
# 1.0-alpha5 stops writing a validity bitmap for a chunk with no nulls (#1130). An
# alpha4 binary misreads such a chunk without refusing it: measured with the real
# v1.0-alpha4 library, 9 of 40 single-row fetches returned NULL for a row that holds
# a value. #1137 answered that with the stamp: a table this build WRITES carries
# pgcolumnar.storage.format_version 2, alpha4 refuses any stamp but 1, and a table
# this build only READS keeps 1 and stays readable by alpha4.
#
# THAT CLAIM IS ABOUT TWO BINARIES, so only a test with both can hold it. The arms in
# native_format.sh stand in for alpha4 with a catalog UPDATE; this suite installs the
# real old library, writes with it, upgrades, and then puts the OLD library back
# under the upgraded catalog -- which is exactly what a user who rolls back the
# package does.
#
#     t_read     written by alpha4, only read by this build   alpha4 still reads it
#     t_append   written by alpha4, appended to by this build  alpha4 refuses it
#     t_new      written by this build                         alpha4 refuses it
#
# Refusal is asserted by SQLSTATE (0A000), on a sequential scan AND on an index fetch
# with the custom scan off, which is the path that returned the silent NULLs.
#
# Not in run_all_versions.sh, for extension_upgrade.sh's reason: it builds an old ref.
#
#   test/format_downgrade.sh [PG_CONFIG] [OLD_REF_OR_DIR]     default old ref: v1.0-alpha4
#
# The old source is resolved exactly as extension_upgrade.sh resolves it.

set -uo pipefail
PG_CONFIG=${1:-pg_config}
OLD_SRC=${2:-${PGC_DOWNGRADE_OLD_SRC:-v1.0-alpha4}}
SRCDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
. "$(dirname "${BASH_SOURCE[0]}")/portlib.sh"
# lib.sh for its recorders and accounting only, the way capability_sweep.sh uses
# it: this suite stands up its own cluster (it must swap the library under it), so
# pgc_setup and its EXIT trap are not used.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
command -v "$PG_CONFIG" >/dev/null 2>&1 || { echo "FATAL: $PG_CONFIG not found"; exit 1; }
BINDIR=$($PG_CONFIG --bindir)
PKGLIB=$($PG_CONFIG --pkglibdir)
PGMAJ=$($PG_CONFIG --version | grep -oE '[0-9]+' | head -1)
PGC_MAJOR="$(pgc_major_of "$PG_CONFIG")"
TMP=$(mktemp -d /tmp/pgc-fmtdown.XXXXXX)
DATA=$TMP/data
PORT=$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" "$$")
LOG=$TMP/server.log
fail=0
cleanup () {
	runuser -u postgres -- "$BINDIR/pg_ctl" -D "$DATA" -w stop >/dev/null 2>&1
	# LEAVE THE PREFIX WITH THE TREE'S OWN LIBRARY. This suite puts the old one in
	# place on purpose; a suite run after it must not find it there.
	[ -f "$TMP/new.so" ] && cp "$TMP/new.so" "$PKGLIB/pgcolumnar.so"
	rm -rf "$TMP"
}
trap cleanup EXIT
runpg () { runuser -u postgres -- "$@"; }
q () { runpg "$BINDIR/psql" -h /tmp -p "$PORT" -d fmtdown -X -Atc "$1" 2>&1; }
# sqlstate SQL -> the SQLSTATE it fails with, or ok
sqlstate () {
	local out
	out=$(runpg "$BINDIR/psql" -h /tmp -p "$PORT" -d fmtdown -X -At \
		-v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
echo "== format_downgrade: PG$PGMAJ, old $OLD_SRC"
EXPLICIT=0
{ [ "$#" -ge 2 ] || [ -n "${PGC_DOWNGRADE_OLD_SRC:-}" ]; } && EXPLICIT=1

# ASK GIT WHETHER IT CAN ANSWER, do not stat .git (#1224). A linked worktree's
# .git is a regular FILE holding `gitdir: <path>`, and so is the one-line gitfile
# devloop.sh writes into its build dir and that an agent staging across a
# container boundary has to write. git works in all three; `[ -d .git ]` is false
# in all three, so this suite skipped its default path in environments selftest
# 310 lists as supported -- "the audit container, worktrees of a clone" among
# them. devloop.sh already had the right form and the reason beside it.
#
# `git clone --shared` from a linked worktree is measured to work, and old refs
# are reachable through it, so this is the whole fix rather than a step of it.
# AND IT MUST BE THIS TREE'S REPOSITORY, NOT AN ENCLOSING ONE (#1228).
# rev-parse searches UPWARD, so a copy with no git presence of its own that
# happens to sit inside any repository answers yes. `git clone --shared` then
# takes the path literally rather than walking up and dies with
# `fatal: repository does not exist`, so the cost was a SKIP carrying
# instructions becoming a FATAL about a missing repository -- a diagnosis
# regression rather than a wrong result, measured by @OffgridwithJD.
#
# --show-toplevel separates the three, measured: a linked worktree and a
# gitfile-staged copy both report THEMSELVES, and a directory nested in a
# repository reports the enclosing root.
pgc_eu_own_repo() {	# pgc_eu_own_repo DIR -> 0 when DIR is its own work tree
	local _top
	_top="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || return 1
	[ -n "$_top" ] && [ "$_top" = "$(cd "$1" 2>/dev/null && pwd -P)" ]
}

if [ "$EXPLICIT" = 0 ] && ! pgc_eu_own_repo "$SRCDIR"; then
	# WHICH of the two it is, rather than a disjunction the reader has to
	# resolve themselves. They are different situations with different repairs,
	# and the enclosing root is the fact that makes the second one legible.
	_eu_encl="$(git -C "$SRCDIR" rev-parse --show-toplevel 2>/dev/null || true)"
	echo "  SKIP  $SRCDIR is not its own git work tree, so the default ref" \
		"$OLD_SRC cannot be built."
	if [ -n "$_eu_encl" ]; then
		echo "        It sits inside the repository at $_eu_encl, which is a"
		echo "        different tree and does not carry this one's history."
	else
		echo "        It has no git presence of its own and none above it."
	fi
	echo "        Supply the old source:"
	echo "        PGC_DOWNGRADE_OLD_SRC=/path/to/old/source, or pass it as the second argument."
	echo "== extension_upgrade: SKIP"
	exit 2
fi

# A value that looks like a path but is not there is a wrong path, not a ref. Falling
# through to the ref branch would answer a question about directories with an error about
# git checkouts, which is the kind of message that costs someone an afternoon.
case "$OLD_SRC" in
	*/*)
		if [ ! -d "$OLD_SRC" ]; then
			echo "  FAIL  $OLD_SRC looks like a path and is not a directory."
			echo "        Give a checked-out source tree of the previous release."
			exit 1
		fi
		;;
esac

# Directory form: a path to an already-checked-out old tree needs no git at all.
if [ -d "$OLD_SRC" ]; then
	[ -f "$OLD_SRC/Makefile" ] || { echo "  FAIL  $OLD_SRC has no Makefile"; exit 1; }
	cp -a "$OLD_SRC" "$TMP/old" || { echo "FATAL: could not copy $OLD_SRC"; exit 1; }
	echo "  old source: directory $OLD_SRC"
else
	# Only reachable when an old source was named explicitly, so this is a failure and
	# not a skip: the caller asked for something this tree cannot provide.
	if ! pgc_eu_own_repo "$SRCDIR"; then
		_eu_encl="$(git -C "$SRCDIR" rev-parse --show-toplevel 2>/dev/null || true)"
		echo "  FAIL  $SRCDIR is not its own git work tree, so the ref $OLD_SRC" \
			"cannot be built."
		if [ -n "$_eu_encl" ]; then
			echo "        It sits inside the repository at $_eu_encl, which is a"
			echo "        different tree and does not carry this one's history."
		else
			echo "        It has no git presence of its own and none above it."
		fi
		echo "        Pass a directory:"
		echo "        test/extension_upgrade.sh $PG_CONFIG /path/to/old/source"
		exit 1
	fi
	if ! git -C "$SRCDIR" rev-parse --verify -q "$OLD_SRC^{commit}" >/dev/null 2>&1; then
		echo "  FAIL  $OLD_SRC is not present. Fetch tags, or pass an explicit ref or dir:"
		echo "        git fetch --tags && test/extension_upgrade.sh $PG_CONFIG <ref>"
		exit 1
	fi
	git clone -q --shared "$SRCDIR" "$TMP/old" || { echo "FATAL: clone failed"; exit 1; }
	git -C "$TMP/old" checkout -q --detach "$OLD_SRC" \
		|| { echo "FATAL: checkout $OLD_SRC failed"; exit 1; }
	echo "  old source: ref $OLD_SRC"
fi
make -C "$TMP/old" PG_CONFIG="$PG_CONFIG" clean >/dev/null 2>&1
make -C "$TMP/old" PG_CONFIG="$PG_CONFIG" -j"$(nproc)" >"$TMP/build_old.log" 2>&1
old_rc=$?
check_num "premise: the old build compiles" "$old_rc" "0"
[ "$old_rc" = 0 ] || { tail -20 "$TMP/build_old.log"; pgc_summary; }
make -C "$TMP/old" PG_CONFIG="$PG_CONFIG" install >/dev/null 2>&1 \
	|| { echo "FAIL  old install"; exit 1; }
cp "$PKGLIB/pgcolumnar.so" "$TMP/old.so"

chown -R postgres "$TMP" 2>/dev/null
runpg "$BINDIR/initdb" -D "$DATA" --locale=C -U postgres >/dev/null 2>&1 \
	|| { echo "FAIL  initdb"; exit 1; }
{
	echo "shared_preload_libraries='pgcolumnar'"
	echo "port=$PORT"
	echo "unix_socket_directories='/tmp'"      # extension_upgrade.sh says why
} >> "$DATA/postgresql.conf"
runpg "$BINDIR/pg_ctl" -D "$DATA" -l "$LOG" -w start >/dev/null 2>&1 \
	|| { echo "FAIL  start"; tail -10 "$LOG"; exit 1; }
runpg "$BINDIR/createdb" -h /tmp -p "$PORT" fmtdown >/dev/null 2>&1

# ---- 1. the old build writes two tables -------------------------------------------
# Wide text with no nulls, compression off: the shape whose bitmap #1130 elides, and
# the shape #1137 measured the silent NULLs on.
q "CREATE EXTENSION pgcolumnar" >/dev/null
for t in t_read t_append; do
	q "CREATE TABLE $t (id int, v text) USING pgcolumnar" >/dev/null
	q "SELECT pgcolumnar.set_options('$t', compression => 'none')" >/dev/null
	q "INSERT INTO $t SELECT g, repeat(md5(g::text), 6) FROM generate_series(1, 3000) g" >/dev/null
	q "CREATE INDEX ON $t (id)" >/dev/null
done
check "premise: the old build stored both tables" \
	"$(q "SELECT count(*) FROM t_read")|$(q "SELECT count(*) FROM t_append")" "3000|3000"

# ---- 2. this build: upgrade, read one, append to one, create one ------------------
make -C "$SRCDIR" PG_CONFIG="$PG_CONFIG" clean >/dev/null 2>&1
make -C "$SRCDIR" PG_CONFIG="$PG_CONFIG" -j"$(nproc)" >"$TMP/build_new.log" 2>&1 \
	|| { echo "FAIL  new build"; tail -20 "$TMP/build_new.log"; exit 1; }
make -C "$SRCDIR" PG_CONFIG="$PG_CONFIG" install >/dev/null 2>&1 \
	|| { echo "FAIL  new install"; exit 1; }
cp "$PKGLIB/pgcolumnar.so" "$TMP/new.so"
# What the cleanup trap puts back. Counted here because a restore of nothing is
# the silent way this suite could leave the prefix holding the old library.
check_num "premise: the prefix holds the tree's own install to put back" \
	"$(n=$(ls "$PKGLIB"/pgcolumnar*.so 2>/dev/null | grep -c .); [ "$n" -lt 2 ] && echo "$n" || echo 2)" "2"
check "premise: the two libraries differ" \
	"$(cmp -s "$TMP/old.so" "$TMP/new.so" && echo same || echo differ)" "differ"
runpg "$BINDIR/pg_ctl" -D "$DATA" -l "$LOG" -w restart >/dev/null 2>&1 \
	|| { echo "FAIL  restart on the new build"; tail -10 "$LOG"; exit 1; }
upd=$(q "ALTER EXTENSION pgcolumnar UPDATE")
check "ALTER EXTENSION UPDATE succeeds" "$(case "$upd" in *ERROR*) echo "$upd";; *) echo ok;; esac)" "ok"

stamp () { q "SELECT format_version FROM pgcolumnar.storage WHERE storage_id = pgcolumnar.get_storage_id('$1')"; }
check "this build reads the table the old build wrote" \
	"$(q "SELECT count(*), sum(id) FROM t_read")" "3000|4501500"
q "INSERT INTO t_append SELECT g, repeat(md5(g::text), 6) FROM generate_series(3001, 3100) g" >/dev/null
q "CREATE TABLE t_new (id int, v text) USING pgcolumnar" >/dev/null
q "SELECT pgcolumnar.set_options('t_new', compression => 'none')" >/dev/null
q "INSERT INTO t_new SELECT g, repeat(md5(g::text), 6) FROM generate_series(1, 3000) g" >/dev/null
q "CREATE INDEX ON t_new (id)" >/dev/null
check "stamps after this build: read, appended, new" \
	"$(stamp t_read) $(stamp t_append) $(stamp t_new)" "1 2 2"

# ---- 3. roll the library back, under the upgraded catalog --------------------------
runpg "$BINDIR/pg_ctl" -D "$DATA" -w stop >/dev/null 2>&1
cp "$TMP/old.so" "$PKGLIB/pgcolumnar.so"
runpg "$BINDIR/pg_ctl" -D "$DATA" -l "$LOG" -w start >/dev/null 2>&1 \
	|| { echo "FAIL  start on the old library"; tail -10 "$LOG"; exit 1; }
check "premise: the server now runs the old library" \
	"$(cmp -s "$PKGLIB/pgcolumnar.so" "$TMP/old.so" && echo old || echo other)" "old"

fetch () {	# fetch TABLE -> SQLSTATE of an index fetch of a non-key column
	sqlstate "SET pgcolumnar.enable_custom_scan = off; SET enable_seqscan = off;
	          SET enable_bitmapscan = off; SELECT v FROM $1 WHERE id = 1234"
}
check "the old build still reads a table this build only read" \
	"$(q "SELECT count(*), sum(id) FROM t_read")" "3000|4501500"
# THE VALUE, NOT ONLY "IT RAN" (#1339). The defect #1137 measured was a fetch that
# SUCCEEDS and returns NULL for a row holding a value, so an arm reading only the
# SQLSTATE passed it. The value is compared with its generator, not with a read.
fetchv () {	# fetchv TABLE -> the value an index fetch returns for id 1234
	q "SET pgcolumnar.enable_custom_scan = off; SET enable_seqscan = off;
	   SET enable_bitmapscan = off; SELECT coalesce(v, '<NULL>') FROM $1 WHERE id = 1234" | tail -1
}
check_text "and fetches from it by index" "$(fetchv t_read)" "$(q "SELECT repeat(md5('1234'), 6)")"
check "the old build refuses a table this build appended to, on a scan" \
	"$(sqlstate "SELECT count(v) FROM t_append")" "0A000"
check "the old build refuses a table this build appended to, on an index fetch" "$(fetch t_append)" "0A000"
check "the old build refuses a table this build wrote, on a scan" \
	"$(sqlstate "SELECT count(v) FROM t_new")" "0A000"
check "the old build refuses a table this build wrote, on an index fetch" "$(fetch t_new)" "0A000"

pgc_summary

#!/usr/bin/env bash
#
# pgColumnar extension-upgrade gate (#382).
#
# The failure this exists to catch is invisible to a build and to CI. Every SQL-callable
# function records its C link name in pg_proc.prosrc at CREATE EXTENSION time. Rename a
# link name in C and the old rows point at a symbol the new library no longer exports.
# Replacing only the shared library, which is exactly what a package upgrade does, then
# leaves the extension inert rather than degraded: reading an existing columnar table
# fails with "could not find function columnar_handler", and so does creating one.
#
# It compiles, it links, every suite passes on a fresh install, and every existing
# install is broken. That combination is why this needs its own gate.
#
# Not in run_all_versions.sh, for the same reason pg_upgrade.sh is not: the matrix builds
# one tree per invocation and this needs two builds of the extension at once. It is a
# second gate beside the matrix, run explicitly.
#
# Usage:
#   test/extension_upgrade.sh [PG_CONFIG] [OLD_REF_OR_DIR]
#
# PGC_UPGRADE_OLD_SRC is the same thing by environment, which is how run_all_versions.sh
# supplies it in an environment with no .git. An explicit second argument wins over it.
#
# The second argument is either a git ref or a path to an already-checked-out source
# tree. A ref defaults to the previous release and is built in a throwaway clone, so the
# working tree is never checked out from under the caller.
#
# The directory form exists because the container dev loop copies the tree WITHOUT .git
# (see docs/testing.md), so the ref form cannot work there. Point it at a second copy of
# the old source instead:
#
#   test/extension_upgrade.sh /usr/local/pg18a/bin/pg_config /root/pgcolumnar-1.0-alpha
#
# Requires a real checkout with tags when the ref form is used. That is a precondition,
# not a bug, and it is reported as one.
set -uo pipefail

PG_CONFIG=${1:-pg_config}
OLD_SRC=${2:-${PGC_UPGRADE_OLD_SRC:-v1.0-alpha}}
SRCDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# The cluster binds a port, so it must come from the band portlib.sh carves BELOW the
# kernel's ephemeral range, probed rather than assumed free. Picking out of the
# ephemeral range means the kernel can hand the same port to something else between
# the choice and the bind. harness_selftest enforces this, and caught it here.
. "$(dirname "${BASH_SOURCE[0]}")/portlib.sh"

command -v "$PG_CONFIG" >/dev/null 2>&1 || { echo "FATAL: $PG_CONFIG not found"; exit 1; }
BINDIR=$($PG_CONFIG --bindir)
SHAREDIR=$($PG_CONFIG --sharedir)
PGMAJ=$($PG_CONFIG --version | grep -oE '[0-9]+' | head -1)

TMP=$(mktemp -d /tmp/pgc-extupg.XXXXXX)
DATA=$TMP/data
PORT=$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" "$$")
LOG=$TMP/server.log
fail=0

cleanup () {
	runuser -u postgres -- "$BINDIR/pg_ctl" -D "$DATA" -w stop >/dev/null 2>&1
	rm -rf "$TMP"
}
trap cleanup EXIT

runpg () { runuser -u postgres -- "$@"; }
q () { runpg "$BINDIR/psql" -h /tmp -p "$PORT" -d extupg -X -Atc "$1" 2>&1; }

echo "== extension_upgrade: PG$PGMAJ, old $OLD_SRC"

# ---- 1. build and install the old extension, from a throwaway clone ------------------
# Skip and fail are different answers, and the difference is who asked for what.
#
#   named an old source that cannot be honoured -> FAIL, the caller asked for a thing
#   named nothing, and the tree cannot supply one -> SKIP, the environment cannot
#
# A silent skip that exits 0 is the bug this suite exists to catch, so the skip is loud,
# reports SKIP rather than PASS, and exits 2 so the runner can tell the two apart.
EXPLICIT=0
{ [ "$#" -ge 2 ] || [ -n "${PGC_UPGRADE_OLD_SRC:-}" ]; } && EXPLICIT=1

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
	echo "        PGC_UPGRADE_OLD_SRC=/path/to/old/source, or pass it as the second argument."
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
make -C "$TMP/old" PG_CONFIG="$PG_CONFIG" -j"$(nproc)" >"$TMP/build_old.log" 2>&1 \
	|| { echo "FAIL  old build"; tail -20 "$TMP/build_old.log"; exit 1; }
make -C "$TMP/old" PG_CONFIG="$PG_CONFIG" install >/dev/null 2>&1 \
	|| { echo "FAIL  old install"; exit 1; }

chown -R postgres "$TMP" 2>/dev/null
runpg "$BINDIR/initdb" -D "$DATA" --locale=C -U postgres >/dev/null 2>&1 \
	|| { echo "FAIL  initdb"; exit 1; }
{
	echo "shared_preload_libraries='pgcolumnar'"
	echo "port=$PORT"
	# Every connection below is `psql -h /tmp`, and where the socket actually
	# lands is a property of how PostgreSQL was BUILT, not of this suite: a
	# source build defaults to /tmp, while the Debian and PGDG packages compile
	# in /var/run/postgresql. So against a packaged server every psql here failed
	# with `connection to server on socket "/tmp/.s.PGSQL.NNNN" failed`, and the
	# suite reported "old install did not store rows" -- a connection fault
	# wearing the costume of a product failure.
	#
	# It went unseen because the two never met: developers run this against
	# source builds, and #741 is the issue that CI had never run it at all. The
	# first CI run after wiring it up failed here.
	#
	# Stated rather than inherited, so -h /tmp is true by construction on either
	# kind of build. lib.sh sidesteps the whole question by connecting over TCP;
	# this suite carries its own harness and connects by socket.
	echo "unix_socket_directories='/tmp'"
} >> "$DATA/postgresql.conf"
runpg "$BINDIR/pg_ctl" -D "$DATA" -l "$LOG" -w start >/dev/null 2>&1 \
	|| { echo "FAIL  start"; tail -10 "$LOG"; exit 1; }
runpg "$BINDIR/createdb" -h /tmp -p "$PORT" extupg >/dev/null 2>&1

q "CREATE EXTENSION pgcolumnar" >/dev/null
q "CREATE TABLE t (id int, v text) USING pgcolumnar" >/dev/null
q "INSERT INTO t SELECT g, 'v'||g FROM generate_series(1,1000) g" >/dev/null
before_rows=$(q "SELECT count(*) FROM t")
old_ver=$(q "SELECT extversion FROM pg_extension WHERE extname='pgcolumnar'")
echo "  old install: $before_rows rows, extversion $old_ver"
[ "$before_rows" = "1000" ] || { echo "FAIL  old install did not store rows"; exit 1; }

# ---- 2. install the tree under test over it, library and scripts ---------------------
# Clean first. This tree may have last been built against another major, and make
# would happily relink those objects into a .so this server cannot load. That is how
# this gate first failed: a pg19 build silently relinked for pg18, the postmaster
# refused to start, and every check below reported a connection error instead.
make -C "$SRCDIR" PG_CONFIG="$PG_CONFIG" clean >/dev/null 2>&1
make -C "$SRCDIR" PG_CONFIG="$PG_CONFIG" -j"$(nproc)" >"$TMP/build_new.log" 2>&1 \
	|| { echo "FAIL  new build"; tail -20 "$TMP/build_new.log"; exit 1; }
make -C "$SRCDIR" PG_CONFIG="$PG_CONFIG" install >/dev/null 2>&1 \
	|| { echo "FAIL  new install"; exit 1; }
if ! runpg "$BINDIR/pg_ctl" -D "$DATA" -l "$LOG" -w restart >/dev/null 2>&1; then
	echo "  FAIL  server did not come back after installing the new build"
	tail -15 "$LOG"
	exit 1
fi

# ---- 3. upgrade, and require that it be available at all ----------------------------
# If the link names did not move, nothing is broken here and the upgrade is a no-op. If
# they did move, ALTER EXTENSION UPDATE is the only route back that keeps user tables,
# so its absence is itself the failure.
new_default=$(grep -oE "default_version = '[^']+'" "$SRCDIR/pgcolumnar.control" | sed "s/.*'\(.*\)'/\1/")
# A single direct pgcolumnar--OLD--NEW.sql is not the only valid route: PostgreSQL
# will walk a chain of upgrade scripts, and this release ships one (the v1.0-alpha
# tag installs 1.0-dev, which reaches 1.0-alpha2 via dev->alpha->alpha2). Ask the
# server whether any update path exists rather than assuming a one-hop file, so a
# correctly chained upgrade is not reported as a missing script.
haspath=$(q "SELECT count(*) FROM pg_extension_update_paths('pgcolumnar')
             WHERE source='$old_ver' AND target='$new_default' AND path IS NOT NULL")
if [ "$new_default" != "$old_ver" ] && [ "${haspath:-0}" = "0" ]; then
	echo "  FAIL  default_version moved $old_ver -> $new_default with no update path"
	echo "        (need pgcolumnar--$old_ver--$new_default.sql or a chain of upgrade scripts)"
	fail=1
fi

upd=$(q "ALTER EXTENSION pgcolumnar UPDATE")
case "$upd" in
	*ERROR*) echo "  FAIL  ALTER EXTENSION UPDATE: $upd"; fail=1 ;;
	*)       echo "  ok    ALTER EXTENSION UPDATE -> $(q "SELECT extversion FROM pg_extension WHERE extname='pgcolumnar'")" ;;
esac

# ---- 4. the extension must work on the upgraded install -----------------------------
chk () {
	local label=$1 want=$2 got
	got=$(q "$3")
	if [ "$got" = "$want" ]; then
		echo "  PASS  $label: $got"
	else
		echo "  FAIL  $label: got [$got] want [$want]"
		fail=1
	fi
}
chk "existing rows still readable" "1000" "SELECT count(*) FROM t"
q "INSERT INTO t VALUES (0,'x')" >/dev/null
chk "insert into an existing table" "1001" "SELECT count(*) FROM t"
q "CREATE TABLE t2 (a int) USING pgcolumnar" >/dev/null
q "INSERT INTO t2 SELECT generate_series(1,3)" >/dev/null
chk "new columnar table creatable" "3" "SELECT count(*) FROM t2"
chk "access method still bound" "pgcolumnar" "SELECT amname FROM pg_am WHERE amname='pgcolumnar'"
chk "maintenance function callable" "" "SELECT pgcolumnar.vacuum('t')"

# Every C function's recorded link name must resolve in the library we just installed.
# This is the general form of the bug, so it catches the next rename as well as this one.
missing=$(q "SELECT string_agg(p.proname||' -> '||p.prosrc, ', ')
             FROM pg_proc p
             JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
             JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'pgcolumnar'
             WHERE p.prolang = (SELECT oid FROM pg_language WHERE lanname='c')
               AND p.prosrc !~ '^pgcolumnar'")
if [ -n "$missing" ]; then
	echo "  FAIL  link names left outside the pgcolumnar namespace: $missing"
	fail=1
else
	echo "  PASS  every C function's link name is namespaced"
fi

# ---- 5. the upgraded catalog must MATCH a fresh install of the new version -----------
# The checks above prove the upgraded extension works; they do not prove the upgrade
# script is COMPLETE. A missed REVOKE leaves an internal function world-executable, a
# missed CREATE OR REPLACE leaves a stale definition or link symbol, a missed new
# function or column is simply absent -- all with every functional check still green.
# Diff the upgraded schema against a fresh CREATE EXTENSION of the new default, object
# by object: every function's definition (which carries its link symbol) and ACL, every
# relation's kind and ACL, every column's type and NOT NULL, every non-base type, and
# every foreign-data wrapper. This runs the real released upgrade path (dev->alpha2 via
# the chain) to convergence, which a single-library test cannot reach.
snap () {
	runpg "$BINDIR/psql" -h /tmp -p "$PORT" -d "$1" -X -F'|' -Atc "
	  SELECT 'FN|'||p.oid::regprocedure||'|'||CASE WHEN p.prokind='a' THEN md5((SELECT ag::text FROM pg_aggregate ag WHERE ag.aggfnoid=p.oid)) ELSE md5(pg_get_functiondef(p.oid)) END||'|'||coalesce(p.proacl::text,'(def)')||'|'||md5(coalesce(obj_description(p.oid,'pg_proc'),''))
	    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='pgcolumnar'
	  UNION ALL SELECT 'REL|'||c.relkind::text||'|'||c.relname||'|'||coalesce(c.relacl::text,'(def)')||'|'||md5(coalesce(obj_description(c.oid,'pg_class'),''))
	    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='pgcolumnar'
	  UNION ALL SELECT 'COL|'||c.relname||'.'||a.attname||'|'||format_type(a.atttypid,a.atttypmod)||'|'||a.attnotnull::text||'|'||md5(coalesce(col_description(c.oid,a.attnum),''))
	    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
	    WHERE n.nspname='pgcolumnar' AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
	  UNION ALL SELECT 'TYP|'||t.typname||'|'||t.typtype::text FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
	    WHERE n.nspname='pgcolumnar' AND t.typtype<>'b'
	  UNION ALL SELECT 'FDW|'||w.fdwname||'|'||coalesce(h.oid::regproc::text,'-')||'|'||coalesce(v.oid::regproc::text,'-')
	    FROM pg_foreign_data_wrapper w LEFT JOIN pg_proc h ON h.oid=w.fdwhandler LEFT JOIN pg_proc v ON v.oid=w.fdwvalidator
	    WHERE w.fdwname LIKE 'pgcolumnar%'
	  ORDER BY 1;" 2>&1 | sort
}
runpg "$BINDIR/createdb" -h /tmp -p "$PORT" extfresh >/dev/null 2>&1
fresh_err=$(runpg "$BINDIR/psql" -h /tmp -p "$PORT" -d extfresh -X -Atc "CREATE EXTENSION pgcolumnar" 2>&1)
fresh_snap=$(snap extfresh)
if [ -z "$fresh_snap" ]; then
	echo "  FAIL  could not stand up a fresh $new_default install to compare against: $fresh_err"
	fail=1
else
	conv_diff=$(diff <(printf '%s\n' "$fresh_snap") <(snap extupg) || true)
	if [ -z "$conv_diff" ]; then
		echo "  PASS  upgraded catalog is byte-identical to a fresh $new_default install ($(printf '%s\n' "$fresh_snap" | grep -c .) objects)"
	else
		echo "  FAIL  upgraded catalog diverges from a fresh $new_default install:"
		printf '%s\n' "$conv_diff" | head -40 | sed 's/^/      /'
		fail=1
	fi
fi

echo "== extension_upgrade: $([ $fail -eq 0 ] && echo PASS || echo FAIL)"
exit $fail

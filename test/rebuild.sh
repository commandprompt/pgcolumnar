#!/usr/bin/env bash
#
# Clean rebuild + install of pgcolumnar for one PG_CONFIG.
#
# Two stale-artifact failures are easy to hit and both produce confusing results
# rather than honest errors:
#
#   1. `make clean` without PG_CONFIG uses whatever pg_config is on PATH. If that
#      is a different major (or absent), nothing is cleaned and the previous
#      major's .o files are relinked into this major's .so. The result installs
#      fine and then fails at load with an undefined symbol, e.g. smgrtruncate2
#      when PG15-17 objects are linked for PG18.
#   2. Installing from one build tree and then running a suite from another leaves
#      the tests measuring the wrong .so entirely, silently.
#
# This script removes both possibilities: it cleans with the correct PG_CONFIG,
# deletes the installed artifacts before rebuilding so a failed install cannot
# leave the old one in place, and then verifies every undefined symbol in the
# built .so resolves against the target postgres binary or its own shared libs.
# That last check is what catches case 1 before a cluster ever starts.
#
# Usage:  test/rebuild.sh [PG_CONFIG] [SRCDIR]
#         test/rebuild.sh /usr/local/pg18/bin/pg_config
#
# Exits non-zero on any failure, so it is safe to chain with &&.

set -uo pipefail

# lib.sh FOR THE RECORD ONLY (#1230). The build below stays here: it has a
# parallel `-j`, the compiler-warning gate that mirrors the matrix, and error
# extraction from the build log, and the harness builder has none of the three.
# What was missing was the RECORD -- this script installed a correct library and
# wrote no stamp, so the next suite run with PGC_SKIP_BUILD=1 refused it with
# "the binary under test was not built from this source", which was false in both
# halves. Sourcing is safe here: at source time lib.sh only sets variables and
# sources portlib.sh, which only computes a port band.
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# THE EXEMPTION MUST BE IN SCOPE, AND set -u WILL NOT TELL US (#1248). The
# symbol check below asks pgc_symbol_is_toolchain about every undefined name.
# If that function is not defined here, the command substitution yields the
# empty string, `[ "" = no ]` is false, the `&&` short-circuits, and the
# undefined list comes out EMPTY -- so the check prints "all resolve" and exits
# 0 on a genuinely mislinked major. It fails OPEN.
#
# The exemption used to be a VARIABLE, and `set -u` turned a missing one into
# `IGNORE: unbound variable` and status 1 -- fail CLOSED. A missing FUNCTION is
# invisible to set -u, so moving the exemption into lib.sh loses that for free
# unless it is asserted. Driven, same pipeline both ways:
#
#     function defined     unresolved: [ExecInitNode]   <- the check works
#     function undefined   unresolved: [<none>]         <- silently clean
#
# Part 580's `type -t` arm cannot cover this: it runs in the selftest's shell,
# which sources lib.sh, so it says nothing about what THIS script sees.
# Reported by @jdatcmd.
if [ "$(type -t pgc_symbol_is_toolchain || true)" != function ]; then
	echo "rebuild: pgc_symbol_is_toolchain is not defined; lib.sh did not load" >&2
	echo "  the symbol check cannot run without it and would report success" >&2
	exit 1
fi

PG_CONFIG="${1:-/usr/local/pg17/bin/pg_config}"
SRCDIR="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [ ! -x "$PG_CONFIG" ]; then
	echo "rebuild: no such pg_config: $PG_CONFIG" >&2
	exit 1
fi

PGVER="$("$PG_CONFIG" --version)"
PKGLIB="$("$PG_CONFIG" --pkglibdir)"
SHAREDIR="$("$PG_CONFIG" --sharedir)"
BINDIR="$("$PG_CONFIG" --bindir)"
SO="$PKGLIB/pgcolumnar.so"

echo "== rebuild: $PGVER"
echo "== srcdir:  $SRCDIR"
echo "== target:  $SO"

cd "$SRCDIR" || exit 1

# ---- 1. clean the tree, with the right PG_CONFIG ---------------------------
make PG_CONFIG="$PG_CONFIG" clean >/dev/null 2>&1
# belt and braces: PGXS clean can be a no-op if the Makefile did not load
rm -f src/*.o src/*.bc ./*.o ./*.bc ./*.so 2>/dev/null

leftover="$(find . -name '*.o' -o -name '*.so' -o -name '*.bc' 2>/dev/null | head -5)"
if [ -n "$leftover" ]; then
	echo "rebuild: tree still dirty after clean:" >&2
	echo "$leftover" >&2
	exit 1
fi
echo "-- clean: tree has no .o/.so/.bc"

# ---- 2. remove the installed artifacts -------------------------------------
# So a build or install failure cannot leave the previous .so loadable and make
# a suite silently test stale code. The paths come from pg_config, so sanity-check
# them before deleting through a glob: an empty or unexpected SHAREDIR would turn
# the next line into a much wider delete than intended.
case "$SHAREDIR" in
	/*/*) ;;
	*) echo "rebuild: refusing to delete from implausible sharedir '$SHAREDIR'" >&2
	   exit 1 ;;
esac
[ -d "$SHAREDIR/extension" ] || {
	echo "rebuild: no extension dir under '$SHAREDIR'" >&2; exit 1; }

rm -f "$SO"
rm -f "$SHAREDIR"/extension/pgcolumnar--*.sql "$SHAREDIR"/extension/pgcolumnar.control
echo "-- uninstalled previous artifacts"

# ---- 3. build --------------------------------------------------------------
buildlog="$(mktemp -t pgc_rebuild.XXXXXX)"
trap 'rm -f "$buildlog"' EXIT
if ! make PG_CONFIG="$PG_CONFIG" -j"$(nproc)" > "$buildlog" 2>&1; then
	echo "rebuild: BUILD FAILED" >&2
	grep -E 'error:|Error' "$buildlog" | head -20 >&2
	exit 1
fi
warns="$(grep -cE 'warning:' "$buildlog")"
echo "-- build: OK ($warns warnings)"
# The version matrix (run_all_versions.sh) treats any compiler warning as a
# failure, so the dev inner loop must too -- otherwise a warning slips through
# here and only surfaces at the gate. Set PGC_ALLOW_WARNINGS=1 to override for a
# deliberate warning-tolerant build.
if [ "$warns" -gt 0 ] && [ -z "${PGC_ALLOW_WARNINGS:-}" ]; then
	echo "rebuild: $warns compiler warning(s) -- the matrix gate rejects these" >&2
	grep -E 'warning:' "$buildlog" | head -20 >&2
	exit 1
fi

if ! make PG_CONFIG="$PG_CONFIG" install >/dev/null 2>&1; then
	echo "rebuild: INSTALL FAILED" >&2
	exit 1
fi
[ -f "$SO" ] || { echo "rebuild: $SO missing after install" >&2; exit 1; }
echo "-- install: OK"

# ---- 3b. record what was just installed, the way the harness does ----------
# AFTER the install and after the artifact check, never before: the digest is of
# the library the install wrote, and a record written for an install that failed
# certifies the previous one. Both stamps, because they answer different
# questions -- which major built the objects (#536), and whether the binary under
# test came from this source (#959).
pgc_write_build_stamp "$SRCDIR/.pgc_built_for_major" "$(pgc_major_of "$PG_CONFIG")"
pgc_record_source_stamp "$SRCDIR" "$PG_CONFIG"
echo "-- recorded: major $(pgc_major_of "$PG_CONFIG"), source $(pgc_source_fingerprint "$SRCDIR")"

# ---- 4. verify the .so resolves against THIS postgres ----------------------
# Every undefined symbol must be satisfied by the server binary or by one of the
# .so's own shared libraries. An object built against another major shows up here
# as an unresolved PostgreSQL symbol, before any cluster tries to load it.
if command -v nm >/dev/null 2>&1; then
	# Symbol names are compared with any @GLIBC_x.y version suffix stripped, since
	# the reference copy in libc is versioned and the reference in postgres is not.
	# The exemption lives in lib.sh as pgc_symbol_is_toolchain and is driven by
	# part 580 with literals, because __stack_chk_guard cannot be reached on
	# x86_64 and a fix verified there proves nothing (#1248).
	strip_ver() { sed 's/@.*//'; }

	undef="$(nm -D --undefined-only "$SO" 2>/dev/null | awk '{print $NF}' |
		strip_ver |
		while IFS= read -r _sym; do
			[ "$(pgc_symbol_is_toolchain "$_sym")" = no ] &&
				printf '%s\n' "$_sym"
		done | LC_ALL=C sort -u)"
	# The .so is dlopen'd into the running postgres, so its symbols resolve against
	# the server binary, everything the server itself links (libm, libssl, ...), and
	# the .so's own dependencies. All three belong in the reference set.
	defined="$(nm -D --defined-only "$BINDIR/postgres" 2>/dev/null | awk '{print $NF}')"
	for lib in $(ldd "$SO" "$BINDIR/postgres" 2>/dev/null |
			awk '/=>/ {print $3}' | grep -v '^$' | LC_ALL=C sort -u); do
		defined="$defined
$(nm -D --defined-only "$lib" 2>/dev/null | awk '{print $NF}')"
	done
	defined="$(echo "$defined" | strip_ver | LC_ALL=C sort -u)"
	# comm requires ONE collation across both inputs and does not check. Both
	# sorts above are pinned to C, so comm is pinned to C too -- inputs sorted one
	# way and compared another is the same defect with an extra step (#552).
	missing="$(LC_ALL=C comm -23 <(echo "$undef") <(echo "$defined"))"
	if [ -n "$missing" ]; then
		echo "rebuild: UNRESOLVED SYMBOLS against $PGVER:" >&2
		echo "$missing" | head -20 >&2
		echo "(objects from another major linked in, or a symbol this check's" >&2
		echo " reference set does not cover -- it reads the server binary and" >&2
		echo " everything ldd reports with '=>', which omits the loader and the" >&2
		echo " vdso)" >&2
		exit 1
	fi
	echo "-- symbols: all resolve against $(basename "$BINDIR")/postgres"
else
	echo "-- symbols: nm unavailable, skipped"
fi

echo "== rebuild OK: $PGVER"

#!/usr/bin/env bash
#
# The shipped upgrade scripts converge: a database on any previously released
# version, after ALTER EXTENSION pgcolumnar UPDATE, has a catalog byte-identical
# to a fresh install of the current default_version.
#
# This is the real guarantee behind "run one command to upgrade". The 1.0-alpha2
# cycle renamed the extension's C link symbols (columnar_* -> pgcolumnar_*, #382),
# added Iceberg/object-store/maintenance functions and two foreign-data wrappers,
# added two pgcolumnar.storage columns, and revoked PUBLIC execute on the internal
# projection and visibility-map functions. Every one of those deltas has to be in
# the upgrade script, or an upgraded database silently diverges from a fresh one:
# a missed C-symbol rewrite leaves an existing columnar table unreadable ("could
# not find function columnar_handler"), a missed REVOKE leaves an internal
# function world-executable.
#
# Released starting points, both of which must reach 1.0-alpha2:
#   1.0-dev   -- what the v1.0-alpha tag actually installed (default_version=1.0-dev)
#   1.0-alpha -- what a build from main installed after the #382 rename landed
# PostgreSQL walks dev->alpha->alpha2 or alpha->alpha2 from the shipped upgrade
# scripts. The old base install scripts are NOT shipped (project convention drops
# them); they live here as frozen fixtures purely to stand up an old-version
# database to upgrade.
#
# This CI test exercises the alpha->alpha2 leg, which is the one this release
# introduces and the one a single installed library can stand up per PostgreSQL
# major in the matrix. The dev leg cannot run against one library: a C function
# resolves its link symbol unconditionally at CREATE FUNCTION (check_function_bodies
# does not gate it), and the pre-#382 1.0-dev base names columnar_* symbols the
# 1.0-alpha2 library no longer exports. Standing up 1.0-dev therefore needs that
# release's own library. The full dev->alpha->alpha2 chain -- including that an
# existing columnar table still reads across the C-symbol rename, and that the
# upgraded catalog converges to a fresh install -- is validated by the two-build
# gate test/extension_upgrade.sh (run under PGC_RUN_UPGRADE=1, not in the per-major
# matrix, because it builds the previous release's shared library alongside this one).
#
# The snapshot compares, across the pgcolumnar schema: every function's full
# definition (pg_get_functiondef, which includes the AS '...','symbol' clause, so
# a wrong link symbol shows up here), its ACL, and its comment; every relation's
# kind, ACL, and comment; every column's type, NOT NULL, and comment; every
# non-base type; and every foreign-data wrapper's handler and validator.
# Convergence = an empty diff on all of it. The comment (pg_description) is in the
# snapshot on purpose: pg_get_functiondef does NOT emit COMMENT, so without this a
# generator that diffs only function definitions is blind to a missing comment,
# and an upgraded install silently loses the 44 function comments a fresh install
# carries (found in review of this release).
#
# Removal proof: this test caught three defects in the generated upgrade script
# before it went green -- psql command tags captured into the SQL body, function
# definitions concatenated without a terminating semicolon, and the ACL REVOKEs
# omitted entirely (the def-only diff that generated the script could not see an
# ACL-only change). Mutate the upgrade script (drop a REVOKE line, or a
# CREATE OR REPLACE) and the matching leg goes FAIL.
#
# Usage:  test/native_upgrade_converge.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

EXTDIR="$("$PGC_PG_CONFIG" --sharedir)/extension"
TARGET="$(sed -n "s/^default_version *= *'\\(.*\\)'.*/\\1/p" "$HERE/../pgcolumnar.control")"
check "control default_version is 1.0-alpha5" "$TARGET" "1.0-alpha5"

# Stage the frozen old base install scripts so an old-version extension can be
# created. These are fixtures, not shipped; whatever was there is put back at the
# end.
#
# STAGED UNCONDITIONALLY (#1090). The old form staged only `if [ ! -f "$dst" ]`,
# so an install script left in the extension directory by an older `make install`
# WON over the committed fixture. This repository shipped
# pgcolumnar--1.0-alpha2.sql and pgcolumnar--1.0-alpha3.sql from the tree until
# the cycle-open rename moved them under test/fixtures, so any prefix installed
# before that still carries them and nothing prunes them. And because the leftover
# was not staged, the EXIT trap did not remove it either: it persisted and won
# again on every later run.
#
# IT FAILS IN BOTH DIRECTIONS, and the quiet one is the dangerous one. Measured on
# this tree, same commit, two prefixes on one machine:
#
#   leftover DIFFERS from the fixture   PG15's alpha2 is the v1.0-alpha2 TAG
#                                       content (fead351f84ca) against the
#                                       fixture's cbb4f36e4308. The suite reported
#                                       11 passed + 0 failed -- CONVERGED, having
#                                       tested a file nobody committed, with
#                                       nothing in the output naming which file it
#                                       read.
#   leftover is BROKEN                  planting a one-line invalid file gave
#                                       8 passed + 3 failed, a red for a defect
#                                       that is not in the tree at all.
#
# The second is how this was noticed; the first is what it costs.
#
# A pre-existing file is preserved and restored rather than deleted: this suite
# did not create it and removing it would change the state of a prefix it does not
# own. Its CONTENT is restored, not its every attribute -- `cp -p` cannot give back
# an owner the suite does not have, and these leftovers are root-owned on a
# developer's box while the suite runs as `postgres`. Content is what the next run
# reads, which is what this is protecting.
#
# ONE LIST. The versions were written out FOUR times -- here, in the staging arm,
# as a literal `3` in its count, and again in the convergence loop that upgrades
# from each of them -- so adding a fixture meant editing four places, and editing
# three of them left an arm passing while testing less than its name claims.
# Reported by jdatcmd, who counted three; the convergence loop is the fourth.
_NUC_FIXTURES=(1.0-alpha 1.0-alpha2 1.0-alpha3 1.0-alpha4)

# A `.pgcbak` ALREADY HERE MEANS AN EARLIER RUN DIED before its EXIT trap, so the
# original is in the backup and the install script holds the fixture. Taking the
# backup again would overwrite the original WITH the fixture and lose it for good:
#
#     start:          ext=[ORIGINAL]          bak=-
#     run 1 crashes:  ext=[FIXTURE]           bak=[ORIGINAL]
#     run 2 unguarded:ext=[FIXTURE]           bak=[FIXTURE]    <- original gone
#
# That is the outcome the preserve-rather-than-delete design exists to avoid,
# reached by another route, and those leftovers are the evidence for #901. So the
# backup is taken only when there is not one already, and the state is NAMED rather
# than silently worked around: the prefix is mid-surgery and a reader should know.
# To clear it, restore by hand from the `.pgcbak` and remove it. Reported by
# jdatcmd, who reproduced the loss.
_nuc_mid=""
for v in "${_NUC_FIXTURES[@]}"; do
	[ -f "$EXTDIR/pgcolumnar--$v.sql.pgcbak" ] && _nuc_mid="$_nuc_mid $v"
done
check "premise: no earlier run of this suite died with a backup still staged" \
	"${_nuc_mid# }" ""

STAGED=()
for v in "${_NUC_FIXTURES[@]}"; do
	src="$HERE/fixtures/pgcolumnar--$v.sql"
	dst="$EXTDIR/pgcolumnar--$v.sql"
	[ -f "$src" ] || continue
	[ -f "$dst" ] && [ ! -f "$dst.pgcbak" ] && cp -p "$dst" "$dst.pgcbak"
	cp "$src" "$dst"
	STAGED+=("$dst")
done
# CHAINED, not replacing: pgc_setup armed `trap pgc_on_exit EXIT`, and bash keeps
# one EXIT trap. Without this line the trap below replaced it, and every run left
# its server listening and its workdir in /tmp (selftest part 620). First, so it
# reads the suite's exit status before anything here overwrites `$?`.
cleanup() {
	pgc_on_exit
	for f in "${STAGED[@]:-}"; do
		[ -n "$f" ] || continue
		if [ -f "$f.pgcbak" ]; then
			mv -f "$f.pgcbak" "$f"
		else
			rm -f "$f"
		fi
	done
}
trap cleanup EXIT

# ---- each fixture must be what its tag actually shipped (#901) --------------
#
# THE FIXTURES ARE THE PREMISE OF EVERY ARM BELOW. This suite claims that each
# released starting point upgrades to the current catalog, and it can only claim
# that if the fixture IS the released starting point.
#
# They were made by renaming the root base script when the next cycle opened. A
# rename at cycle-open equals the release only if nothing touched the file between
# the tag and the rename, and for `1.0-alpha2` something did: two post-release
# `set_options` fixes. The fixture was the released script plus those, so this
# suite spent two cycles upgrading from a state no user ever had.
#
# Comparing BLOB IDS rather than diffing, because a blob id is a lookup. No
# similarity heuristic and no pathspec can distort it, and a rename-detection
# argument once turned a content-preserving move into a fabricated `R098` here.
#
# `1.0-alpha` IS DELIBERATELY ABSENT FROM THIS LIST, and that is the finding this
# check exists to stop being invisible. `v1.0-alpha` shipped
# `pgcolumnar--1.0-dev.sql` with `default_version = 1.0-dev`; there is no
# `pgcolumnar--1.0-alpha.sql` at that tag or any other. Its fixture is constructed
# rather than released, and it cannot be replaced by the real `1.0-dev` script:
# that script names the pre-rename C symbol and the current library does not
# export it.
#
#     ERROR:  could not find function "columnar_handler" in file "pgcolumnar.so"
#
# So no single-library test can start from a genuine `1.0-dev` or `1.0-alpha`
# install. The arm is kept because it is the only cover for the
# `1.0-dev--1.0-alpha` and `1.0-alpha--1.0-alpha2` upgrade scripts, which ship.
# What it tests is catalog shape, not a released artifact, and it is named that
# way rather than counted with the others.
# DERIVED FROM THE FIXTURES ON DISK rather than listed, so a fixture added at the
# next cycle-open is checked without anyone remembering to add it here. A list
# that has to be edited alongside the thing it describes goes stale, and this
# suite already carries three copies of its version list for exactly that reason.
# A hardcoded list here could not see its own incompleteness: the one case it
# must catch is a NEW fixture, and a new fixture is precisely what it would omit.
#
# THE LOCAL TAG IS TRUSTED, AND THAT IS A REAL LIMIT. `git fetch` never moves an
# existing local tag, so a stale one compares the fixture against the wrong blob
# and this arm reports a drift that is not there, or misses one that is. A false
# release-integrity issue has already been filed off a stale local ref in this
# repository. If this arm fails and the fixture looks right, check the tag against
# the server before believing it:
#
#     git ls-remote origin refs/tags/v<version>
_FX_TAGGED=""
for _fx_f in "$HERE"/fixtures/pgcolumnar--*.sql; do
	[ -f "$_fx_f" ] || continue
	_fx_v="${_fx_f##*/pgcolumnar--}"; _fx_v="${_fx_v%.sql}"
	# 1.0-alpha is synthetic and has no tag artifact: see above.
	[ "$_fx_v" = "1.0-alpha" ] && continue
	_FX_TAGGED="$_FX_TAGGED $_fx_v"
done
_FX_TAGGED="${_FX_TAGGED# }"

# The sweep is a claim too. An empty one would make every arm below vanish and the
# suite would report clean having compared nothing.
check "premise: the fixture sweep found fixtures to compare against their tags" \
	"$([ -n "$_FX_TAGGED" ] && echo yes || echo no)" "yes"

_fx_git() { git -C "$HERE/.." "$@" 2>/dev/null; }
if ! _fx_git rev-parse --git-dir >/dev/null; then
	_fx_why="no git repository in the tree under test"
elif [ -z "$(_fx_git tag -l 'v1.0-alpha*')" ]; then
	# actions/checkout fetches no tags, so CI fetches the release tags in a step
	# of its own for these arms (#1341). A local checkout without them still
	# lands here, and the arms skip rather than compare against nothing.
	_fx_why="no release tags in this checkout"
else
	_fx_why=""
fi

# The name is written out in each call rather than held in a variable: the twin
# grader reads a check's name from the call, and `check "$_fx_name"` was invisible
# to it, so a port could drop this arm and still grade complete.
for v in $_FX_TAGGED; do
	if [ -n "$_fx_why" ]; then
		check_skip "the $v fixture is byte-identical to what v$v shipped" \
			"SKIP  the $v fixture is byte-identical to what v$v shipped ($_fx_why)" "$_fx_why"
		continue
	fi
	_fx_tag="$(_fx_git rev-parse "v$v:pgcolumnar--$v.sql" || echo "no such path at v$v")"
	_fx_fix="$(_fx_git hash-object "$HERE/fixtures/pgcolumnar--$v.sql" || echo "fixture missing")"
	check "the $v fixture is byte-identical to what v$v shipped" "$_fx_fix" "$_fx_tag"
done

# THE ARM THAT CATCHES THE QUIET HALF. Staging unconditionally fixes it; this
# says so out loud, so a future change that reintroduces a conditional cannot
# pass silently. Comparing the installed file against the fixture is the only
# thing that separates "tested the committed fixture" from "tested whatever was
# lying in the extension directory".
_nuc_staged=0
_nuc_wrong=""
for v in "${_NUC_FIXTURES[@]}"; do
	src="$HERE/fixtures/pgcolumnar--$v.sql"
	dst="$EXTDIR/pgcolumnar--$v.sql"
	[ -f "$src" ] || continue
	_nuc_staged=$((_nuc_staged + 1))
	cmp -s "$src" "$dst" || _nuc_wrong="$_nuc_wrong $v"
done

# The count is pinned separately: without it a fixture that vanished would leave
# the content arm comparing nothing and reporting "none".
check "premise: every fixture named for staging was staged" \
	"$_nuc_staged" "${#_NUC_FIXTURES[@]}"

check "every staged install script is the committed fixture, not a leftover" \
	"${_nuc_wrong# }" ""

P() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -tAq "$@"; }

# Comprehensive catalog snapshot of the pgcolumnar schema, one line per object.
snap() {
	P -d "$1" -F'|' -c "
	  SELECT 'FN|'||p.oid::regprocedure||'|'||md5(pg_get_functiondef(p.oid))||'|'||coalesce(p.proacl::text,'(def)')||'|'||md5(coalesce(obj_description(p.oid,'pg_proc'),''))
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
	  ORDER BY 1;" | sort
}

# Fresh install of the target version is the reference.
P -d postgres -c "DROP DATABASE IF EXISTS conv_fresh;" >/dev/null
P -d postgres -c "CREATE DATABASE conv_fresh;" >/dev/null
P -d conv_fresh -c "CREATE EXTENSION pgcolumnar VERSION '$TARGET';" >/dev/null
REF="$(mktemp)"; snap conv_fresh > "$REF"
check "fresh $TARGET install has objects to compare" \
	"$([ "$(wc -l <"$REF")" -gt 100 ] && echo yes || echo no)" "yes"

# Each released starting point must upgrade to an identical catalog.
for from in "${_NUC_FIXTURES[@]}"; do
	[ -f "$EXTDIR/pgcolumnar--$from.sql" ] || { check "fixture for $from present" "missing" "present"; continue; }
	db="conv_from_$(echo "$from" | tr '.-' '__')"
	P -d postgres -c "DROP DATABASE IF EXISTS $db;" >/dev/null
	P -d postgres -c "CREATE DATABASE $db;" >/dev/null
	P -d "$db" -c "CREATE EXTENSION pgcolumnar VERSION '$from';" >/dev/null
	err="$(P -d "$db" -c "ALTER EXTENSION pgcolumnar UPDATE TO '$TARGET';" 2>&1)"
	check "$from -> $TARGET upgrade applies without error" "${err:-OK}" "OK"
	check "$from is at $TARGET after upgrade" \
		"$(P -d "$db" -c "SELECT extversion FROM pg_extension WHERE extname='pgcolumnar';")" "$TARGET"
	got="$(mktemp)"; snap "$db" > "$got"
	d="$(diff "$REF" "$got" || true)"
	check "$from -> $TARGET converges to a fresh $TARGET catalog" "${d:-CONVERGED}" "CONVERGED"
	[ -n "$d" ] && { echo "---- divergence ($from) ----"; echo "$d" | head -40; echo "----"; }
	rm -f "$got"
	P -d postgres -c "DROP DATABASE IF EXISTS $db;" >/dev/null
done
rm -f "$REF"
P -d postgres -c "DROP DATABASE IF EXISTS conv_fresh;" >/dev/null

# ---- the window: this library over the previous release's catalog (#1341 item 66) ----
#
# docs/installation.md steps 1 and 2 put the new library in place; step 3,
# ALTER EXTENSION UPDATE, brings the catalog up to it. Between them a database
# runs this library over its previous release's catalog. The library deletes
# from retired_group unconditionally (columnar_metadata.c, PgColumnarDeleteMetadata),
# which that catalog does not have, so DROP TABLE, TRUNCATE and pgcolumnar.vacuum()
# fail there: measured, with 42P01 'columnar metadata table
# "pgcolumnar.retired_group" does not exist', which names neither the cause nor step 3.
#
# The arms hold what the window must keep: reads and writes work, a refused
# operation changes nothing and names the remedy, and step 3 makes every one of
# them work on the same rows. PREV is the newest fixture, so these follow the
# release they describe rather than naming it.
PREV="${_NUC_FIXTURES[${#_NUC_FIXTURES[@]}-1]}"
WIN=conv_window
P -d postgres -c "DROP DATABASE IF EXISTS $WIN;" >/dev/null
P -d postgres -c "CREATE DATABASE $WIN;" >/dev/null
P -d "$WIN" -c "CREATE EXTENSION pgcolumnar VERSION '$PREV';" >/dev/null
check "premise: the window database runs this library over the previous release's catalog" \
	"$(P -d "$WIN" -c "SELECT extversion FROM pg_extension WHERE extname='pgcolumnar';")" "$PREV"
check "premise: that catalog has no retired_group, which this library deletes from" \
	"$(P -d "$WIN" -c "SELECT count(*) FROM pg_class WHERE relname = 'retired_group' AND relnamespace = 'pgcolumnar'::regnamespace;")" "0"
P -d "$WIN" -c "CREATE TABLE w (a int, b text) USING pgcolumnar;" >/dev/null
P -d "$WIN" -c "INSERT INTO w SELECT g, 'x' || g FROM generate_series(1, 20000) g;" >/dev/null
P -d "$WIN" -c "DELETE FROM w WHERE a % 3 = 0;" >/dev/null
win_rows() { P -d "$WIN" -c "SELECT count(*) || '|' || sum(a) FROM w;"; }
check "in the window, a columnar table is written, deleted from and read" "$(win_rows)" "13334|133346667"
win_refusal() {	# win_refusal SQL -> SQLSTATE|whether the HINT names step 3, or what happened instead
	local out
	out="$(P -d "$WIN" -v VERBOSITY=verbose -c "$1" 2>&1)" && { echo accepted; return; }
	echo "$(sed -nE 's/^ERROR:  ([0-9A-Z]{5}): .*/\1/p' <<<"$out" | head -1)|$(grep -q '^HINT:  .*ALTER EXTENSION pgcolumnar UPDATE' <<<"$out" && echo names-the-update || echo no-hint)"
}
check "in the window, DROP TABLE is refused and names ALTER EXTENSION UPDATE" \
	"$(win_refusal "DROP TABLE w;")" "42P01|names-the-update"
check "in the window, TRUNCATE is refused and names ALTER EXTENSION UPDATE" \
	"$(win_refusal "TRUNCATE w;")" "42P01|names-the-update"
check "in the window, pgcolumnar.vacuum() is refused and names ALTER EXTENSION UPDATE" \
	"$(win_refusal "SELECT pgcolumnar.vacuum('w');")" "42P01|names-the-update"
check "in the window, the refused operations changed nothing" "$(win_rows)" "13334|133346667"

P -d "$WIN" -c "ALTER EXTENSION pgcolumnar UPDATE;" >/dev/null
check "after ALTER EXTENSION UPDATE, the same rows are there" "$(win_rows)" "13334|133346667"
check "after ALTER EXTENSION UPDATE, pgcolumnar.vacuum() succeeds and keeps them" \
	"$(P -d "$WIN" -c "SELECT pgcolumnar.vacuum('w');" >/dev/null 2>&1 && win_rows || echo refused)" "13334|133346667"
check "after ALTER EXTENSION UPDATE, TRUNCATE and DROP TABLE succeed" \
	"$(P -d "$WIN" -c "TRUNCATE w;" >/dev/null 2>&1 && P -d "$WIN" -c "DROP TABLE w;" >/dev/null 2>&1 && echo succeeded || echo refused)" "succeeded"
P -d postgres -c "DROP DATABASE IF EXISTS $WIN;" >/dev/null

pgc_summary

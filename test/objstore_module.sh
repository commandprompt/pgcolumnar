#!/usr/bin/env bash
#
# The object-store module's packaging and loader (#393).
#
# The module is a SEPARATE, non-preloaded shared library, because pgColumnar loads
# through shared_preload_libraries and anything the main library links is mapped into
# the postmaster and inherited by every backend. This suite asserts that separation
# holds, because it is the property the whole design rests on and it is easy to lose
# by accident: adding one object to the main OBJS list would undo it silently.
#
# It also asserts a remote path reports a remote error. Before this, s3://bucket/key
# reached AllocateFile and reported "No such file or directory", which is true of the
# filesystem and useless to the reader.
#
# Usage:  test/objstore_module.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# The remote reads below are refused for want of an endpoint and credentials, and
# each check names the refusal it wants. The postmaster inherits this environment,
# so an ambient AWS_* would turn those refusals into connection attempts: clear them.
unset AWS_ENDPOINT_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN \
	AWS_REGION AWS_DEFAULT_REGION
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# nm reads the symbol tables the separation checks rest on; stat sizes the
# stand-in module. Without this, a missing nm makes "the symbol is absent from
# the main library" true for the wrong reason, which is the whole failure this
# suite exists to catch, turned on itself.
pgc_require_tools nm stat ldd || { pgc_summary; exit 1; }

LIBDIR="$("$PGC_PG_CONFIG" --pkglibdir 2>/dev/null || echo "")"
[ -n "$LIBDIR" ] || LIBDIR="$(pgc_pg "pg_config --pkglibdir" | tail -1 | tr -d '\r')"
MOD="$LIBDIR/pgcolumnar_objstore.so"

# Recover from a stash left by an interrupted run, and decide which kind it is.
#
# An interrupt between a move and its restore leaves a .probe or .away behind. The
# dangerous case is real: inside the broken-module arm below, this run's STAND-IN
# is installed as the module and the real one is parked at .away, so a later run
# that moved the stand-in aside would overwrite the only real copy with 19 bytes
# of garbage and destroy the installation. Running alone does not prevent it,
# because the two runs are sequential.
#
# But refusing on PRESENCE alone refuses forever. Every matrix leg runs
# `make install`, so the ordinary leftover is debris sitting beside a module that
# is already fine, and this suite then stays red on that major until somebody
# moves a file by hand. On 2026-08-06 a .probe from 15:09 did exactly that to PG17
# in a five-major matrix, while the other 119 suites passed.
#
# What tells the two apart is the module BESIDE the stash, not the stash. nm is
# required above, so this discriminator cannot go quiet the way a bare -e test can.
stash_is_debris() {
	[ -e "$MOD" ] || return 1
	[ "$(nm -D --defined-only "$MOD" 2>/dev/null |
		grep -c ' T pgcolumnar_objstore_init')" -ge 1 ]
}
for stash in "$MOD.away" "$MOD.probe"; do
	[ -e "$stash" ] || continue
	if stash_is_debris; then
		echo "NOTE  $stash was left by an interrupted run. The installed module is"
		echo "      valid, so the stash is debris; removing it and continuing."
		rm -f "$stash"
		continue
	fi
	# Nothing valid is installed, so this stash is the only surviving copy. Put it
	# back. The old advice was to do this by hand, which is why an interrupt on one
	# run reddened every later run on that major until somebody read the message.
	echo "NOTE  $stash was left by an interrupted run, and the module beside it is"
	echo "      missing or not a module, so the stash is the only surviving copy."
	echo "      Restoring it and continuing."
	rm -f "$MOD"
	mv "$stash" "$MOD"
	# Restoring garbage is not recovery. If the stash was not a module either,
	# every check below would run against a broken installation and report the
	# confusing half of the truth, so stop here and say which file to look at.
	if ! stash_is_debris; then
		# A literal name, so the twin can carry it: the paths go in the detail.
		pgc_fail "a stash restored over a missing module is a module" \
			"restored $stash to $MOD, but that is not a module either; this installation needs 'make install' before the suite can run"
		pgc_summary
		exit 1
	fi
done

check "the main library is installed" \
	"$(pgc_pg "test -f '$LIBDIR/pgcolumnar.so' && echo yes || echo no" | tail -1)" "yes"
check "the object-store module is installed BESIDE it, not inside it" \
	"$(pgc_pg "test -f '$MOD' && echo yes || echo no" | tail -1)" "yes"

# ---- the separation the design rests on ---------------------------------------
#
# The question is whether the module's code is INSIDE the preloaded library. The
# obvious check does not answer it: `readelf -d pgcolumnar.so | grep -c objstore`
# counts DT_NEEDED entries, and the failure mode we care about -- someone adds
# the module's objects to the main OBJS list -- links them STATICALLY and emits no
# DT_NEEDED entry at all. That check reads 0 whether or not the mistake was made.
# It also reads 0 when readelf is absent, or when the .so is not there.
#
# Ask about the symbol instead, as a PAIR. The entry point must be absent from
# the main library and present in the module. A broken or missing instrument
# fails the positive half, so the pair cannot go quiet the way a lone count of
# zero can.
check_num "the module's entry point is NOT inside the preloaded library" \
	"$(pgc_pg "nm -D --defined-only '$LIBDIR/pgcolumnar.so' | grep -c pgcolumnar_objstore_init" | tail -1)" "0"
check_num "positive control: it IS defined in the module, so nm really looked" \
	"$(pgc_pg "nm -D --defined-only '$MOD' | grep -c ' T pgcolumnar_objstore_init'" | tail -1)" "1"

# A remote path's refusal, verbose: "ERROR:  SQLSTATE: message" on its first line.
refusal_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -q -c '\set VERBOSITY verbose' -c "$1" 2>&1
}

# What each URL must be refused with, SQLSTATE and message. The checks used to take
# any of seven phrases, and "is not supported" is one of them: with the module
# dropping s3:// or gs:// from the schemes it handles, each read said so and main
# passed 30 of 30 (#1341). An https:// read reaches the allow-list only when the
# module was built with OpenSSL; without it the scheme is unhandled, by design.
if [ "$(pgc_pg "ldd '$MOD'" 2>/dev/null | grep -c libssl)" -ge 1 ]; then
	HTTPS_REFUSAL='42501: columnar: endpoint "host:443" is not in pgcolumnar.objstore_allowed_endpoints'
else
	HTTPS_REFUSAL='0A000: columnar: reading "https://host/key.parquet" is not supported'
fi
refusal_for() {
	case "$1" in
	s3://*) echo "28000: columnar: reading \"$1\" requires AWS_ENDPOINT_URL in the server environment" ;;
	gs://*) echo "28000: columnar: reading \"$1\" requires AWS_ACCESS_KEY_ID in the server environment" ;;
	https://*) echo "$HTTPS_REFUSAL" ;;
	esac
}

# A remote path must report a remote error, from the reader, without a connection.
for url in "s3://bucket/key.parquet" "gs://bucket/key.parquet" "https://host/key.parquet"; do
	# The scheme, by parameter expansion rather than a `cut` subshell, because BOTH
	# names need it now: the headline already carried it and the continuation did not,
	# which is why three iterations produced one ledger key (#982).
	scheme="${url%%:*}"
	out=$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$url') AS (a int)")
	check "a $scheme URL reports an object-storage error, not a missing file" \
		"$(head -1 <<<"$out")" "ERROR:  $(refusal_for "$url")"
	check_num "and the $scheme URL is NOT reported as a missing file" \
		"$(grep -c 'No such file or directory' <<<"$out")" "0"
done

# ---- a remote glob must be expanded REMOTELY, never against the local FS -------
#
# pq_resolve_paths runs AHEAD of the byte source at every entry point, so before
# #393's fix an s3:// key containing a glob metacharacter went to glob() against
# the LOCAL filesystem and came back "no files match pattern". Since #619 a remote
# glob is expanded by a ListObjectsV2 call through the module, not against the
# local filesystem: with no endpoint configured here it fails as a remote/object-
# storage error, and the invariant this arm still guards is that it is NEVER a
# local filesystem miss. `*`, `?` and `[` are all legal in an S3 key.
for pat in "s3://bucket/a*.parquet" "s3://bucket/a?.parquet" "s3://bucket/a[0-9].parquet"; do
	# The metacharacter under test, which is what distinguishes the three iterations.
	# Here BOTH names collided: unlike the loop above, the headline did not carry it
	# either, so three iterations produced one key for each of two checks (#982).
	meta="${pat#s3://bucket/a}"
	meta="${meta%.parquet}"
	out=$(refusal_of "SELECT * FROM pgcolumnar.read_parquet('$pat') AS (a int)")
	# The listing is refused for want of an endpoint, as a read is; the URL it names
	# is the listing's, not the pattern, so only the SQLSTATE and the cause are pinned.
	check "a remote glob ($meta) is handled remotely (an object-storage error, not a local one)" \
		"$(head -1 <<<"$out" | sed -n 's/^ERROR:  \(28000\): columnar: reading "s3:[^"]*" \(requires AWS_ENDPOINT_URL\) .*/\1 \2/p')" \
		"28000 requires AWS_ENDPOINT_URL"
	check_num "and the $meta glob is NOT reported as a local filesystem miss" \
		"$(grep -c 'no files match pattern\|matched no regular files\|No such file or directory' <<<"$out")" "0"
done

# A local path must be entirely unaffected, which is the regression this could cause.
# Read THROUGH read_parquet, the path the remote dispatch sits in front of. This used to
# count a columnar table, which never reaches the dispatch: with every local
# read_parquet refused as a missing file, main passed 30 of 30 (#1341).
psql_run "CREATE TABLE lp (a int) USING pgcolumnar; INSERT INTO lp VALUES (1),(2),(3);
	SELECT pgcolumnar.export_parquet('lp', '$PGC_WORKDIR/lp.parquet');" >/dev/null 2>&1
check_num "a local path still works" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$PGC_WORKDIR/lp.parquet') AS (a int) WHERE a IN (1, 2, 3)")" "3"
# A RELATIVE path, which is what the label says. The previous version of this
# passed an absolute one, so the case it named was never exercised.
check_num "a relative path is not mistaken for a URL" \
	"$(psql_run "SELECT * FROM pgcolumnar.read_parquet('nonexistent-dir/x.parquet') AS (a int)" 2>&1 |
	   grep -c 'No such file or directory\|could not open')" "1"

# ---- everything below MOVES the installed module ------------------------------
#
# That needs write permission on its directory, which the suite has when it runs as
# the installing user and may not otherwise. Skip visibly rather than fail: a suite
# that cannot perform its manipulation has not found a defect, and a red gate here
# would be about the environment.
#
# This suite also runs ALONE in the matrix, because the file it moves is shared with
# every other suite in the run.
#

# Armed BEFORE the writability probe below, which is itself a move. The first
# version armed it after, leaving a two-syscall window in which an interrupt left
# the real module at $MOD.probe with nothing at $MOD: restore_module knew only
# .away, so the next run sailed past the start guard, found no module to move,
# and reported SKIP on an installation that was itself broken and stayed broken
# until somebody noticed the .probe file. Same shape as the defect this suite was
# written to catch, one move earlier. Both suffixes are handled here and above.
restore_module() {
	local stash
	for stash in "$MOD.away" "$MOD.probe"; do
		[ -e "$stash" ] || continue
		rm -f "$MOD"
		mv "$stash" "$MOD"
	done
}
# Chained, not replacing. pgc_setup installs `trap pgc_teardown EXIT`, and a bare
# `trap restore_module EXIT` overwrites it: the module came back but the cluster
# was never stopped and its workdir never removed. Every run of this suite then
# left a live postmaster holding a port. Measured at 32 orphaned postmasters from
# one matrix plus one gate, which is enough to exhaust the port band -- and a
# suite that cannot get a port fails with 8 start attempts, which reads exactly
# like a real red. replication.sh's sb_teardown already chains this way.
objstore_teardown() { restore_module; pgc_teardown; }
trap objstore_teardown EXIT INT TERM

if ! mv "$MOD" "$MOD.probe" 2>/dev/null; then
	check_skip "the absent and broken module paths" "SKIP  cannot move $MOD, so the absent and broken paths are untested here" "cannot move $MOD"
	pgc_summary
	exit 0
fi
mv "$MOD.probe" "$MOD" 2>/dev/null

# ---- present, absent, and broken must be three DIFFERENT reports ---------------
#
# Three states, three messages, and the checks below are written so that each one
# fails if the state it names is not the state that occurred.
#
# The earlier version could not do that. A module that handles no scheme yet and a
# module that is not installed at all both produced "is not supported", so the
# absent arm passed whether or not the mv succeeded: deleting both mv lines left
# every check green. That is why the loader now names the missing module
# separately -- it is a real distinction for the operator ("install a package"
# against "this build will never read that URL"), and it is what makes this arm
# able to fail.
#
# What is still asserted here from before:
#
#  1 a missing module must report the remote path against the missing module, not
#    'could not access file "pgcolumnar_objstore"'. signalNotFound = false
#    suppresses a missing SYMBOL; a missing LIBRARY is raised earlier by
#    internal_load_library and needs a PG_TRY.
#
#  2 the SAME query must give the SAME error twice in one session. The cache flag
#    used to be set before the load, so the ereport unwound past the assignment
#    while the static kept its new value: the first read reported the raw load
#    failure and every later one reported the documented message. Two identical
#    queries, one session, two different errors, the second one plausible.
#
# Both reads run in ONE psql session on purpose. Separate sessions cannot see it.
# az:// deliberately: the scheme must be one the module recognizes as remote
# but does NOT handle. s3:// stopped qualifying when #393 M2 implemented it, and
# gs:// stopped qualifying when #621 implemented GCS interop (their no-endpoint
# errors are pinned in the loop above and in the addressing suite); az:// stays
# unhandled until an Azure milestone exists, if one ever does.
Q="SELECT * FROM pgcolumnar.read_parquet('az://bucket/key.parquet') AS (a int)"
two_reads() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-At -q -c "$Q" -c "$Q" 2>&1
}

both=$(two_reads)
check_num "with the module present, both reads report an unhandled scheme" \
	"$(grep -c 'is not supported' <<<"$both")" "2"
check_num "and neither claims the module is missing" \
	"$(grep -c 'requires the object-store module' <<<"$both")" "0"

# ---- the module ABSENT, which is a supported configuration --------------------
mv "$MOD" "$MOD.away" 2>/dev/null
# Recorded DURING the run, not after the restore. The previous premise ran after
# the module was back and asserted it was present, so it could only fail if the
# RESTORE failed -- it said nothing about the state the reads actually saw.
absent_state=$([ -e "$MOD" ] && echo present || echo absent)
absent=$(two_reads)
mv "$MOD.away" "$MOD" 2>/dev/null

check "premise: the module really was absent while those two reads ran" \
	"$absent_state" "absent"
check "premise: and the real module is back afterwards" \
	"$([ -e "$MOD" ] && echo yes || echo no)" "yes"
check_num "with the module absent, neither read leaks the loader's own error" \
	"$(grep -c 'could not access file' <<<"$absent")" "0"
check_num "both reads name the missing module, identically" \
	"$(grep -c 'requires the object-store module' <<<"$absent")" "2"
check_num "and neither downgrades it to an unhandled scheme" \
	"$(grep -c 'is not supported' <<<"$absent")" "0"

# ---- installed but BROKEN is not the same as not installed --------------------
#
# Only "not installed" is supported. A truncated library or a permission problem is the
# operator's to fix, and reporting it as an unsupported scheme sends them elsewhere.
#
# TWO premises, because the first version of this test had neither and passed while doing
# nothing:
#   1 the module must actually BE corrupt during the run. Overwriting it in place fails
#     silently: it is root-owned 0755 and this runs as postgres, which can write the
#     DIRECTORY but not that file. So move it aside and CREATE a new one.
#   2 the SQLSTATE cannot be used to tell the two cases apart. internal_load_library uses
#     errcode_for_file_access() for both the stat failure and the dlopen failure, so
#     missing and "file too short" both arrive as 58P01. Measured. The check is on file
#     presence for that reason.
mv "$MOD" "$MOD.away" 2>/dev/null && printf 'not a shared object' > "$MOD" 2>/dev/null
check_num "premise: the module really is corrupt for this run, not merely intended to be" \
	"$(stat -c %s "$MOD" 2>/dev/null)" "19"
broken=$(two_reads)
rm -f "$MOD" 2>/dev/null; mv "$MOD.away" "$MOD" 2>/dev/null
check "premise: the real module was restored afterwards" \
	"$([ "$(stat -c %s "$MOD" 2>/dev/null || echo 0)" -gt 1000 ] && echo yes || echo no)" "yes"
check_num "a broken module does NOT masquerade as an unhandled scheme" \
	"$(grep -c 'is not supported' <<<"$broken")" "0"
check_num "nor as a module that was never installed" \
	"$(grep -c 'requires the object-store module' <<<"$broken")" "0"
check "and the loader's own reason survives" \
	"$([ "$(grep -ci 'could not load library' <<<"$broken")" -ge 1 ] && echo yes || echo no)" "yes"

pgc_summary

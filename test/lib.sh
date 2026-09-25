#!/usr/bin/env bash
#
# pgColumnar shared test harness.
#
# Sourced by the differential/recovery/fuzz suites. Provides a throwaway
# cluster lifecycle, a heap-vs-columnar differential oracle, and pass/fail
# accounting. Written fresh for pgColumnar; it does not reuse any upstream test
# file or expected-output file.
#
# The differential oracle is the core idea: every table under test has a heap
# mirror loaded with identical data, and a query is run against both. The two
# result sets are compared as order-independent hashes, so heap acts as the
# reference oracle for pgcolumnar. This catches encode/decode and skipping bugs
# generically instead of via hardcoded expected values.
#
# Conventions for suites that source this file:
#   - Call pgc_setup "$@" once (passes through the optional PG_CONFIG arg).
#   - Use q "SQL" for a scalar, psql_run for a statement, psql_file FILE.
#   - Use diff_query LABEL "SQL with %T" to compare a heap/columnar pair.
#   - Finish with pgc_summary (exits non-zero if any check failed).
#
# Client SQL runs as the current (root) user over TCP with trust auth, so
# -f files never have postgres-ownership problems; only initdb and pg_ctl run
# as the postgres OS user.

# Do not set -e here: the suite sets its own shell options. The oracle helpers
# must not abort the run on a single mismatch or a SQL error; they record a
# failure and continue.

PGC_FAIL=0
# The suite's own name, resolved ONCE at load rather than per check: pgc_record
# runs at every one of 3,762 call sites, and a basename fork at each of them is
# 3,762 forks a suite does not need.
PGC_SUITE="$(basename "$0" .sh)"
PGC_CHECKS=0
# A FOURTH OUTCOME. A check deliberately not asked -- a wall-clock measurement on
# a shared runner under PGC_SKIP_TIMING -- is not a pass, not a failure, and not
# unrunnable. It is counted, so `checks run:` reports the checks a suite
# ENCOUNTERED rather than the ones it managed to evaluate, and pgc_summary
# reconciles four counters against that count instead of three.
PGC_SKIPPED=0

# The status pgc_summary uses for "ran no checks".
#
# NOT 2. #448 used 2 and that was wrong: 2 is a status suites already produce for
# unrelated reasons. bash exits 2 on a parse error in the suite file (verified),
# and the suites that run under `set -euo pipefail` -- smoke, phase2 through
# phase6, audit -- abort with whatever status the failing command returned, so a
# dead postmaster or a typo became "ran no checks" and every runner reported the
# major green. That is precisely the lie #447 was opened to remove, relocated one
# layer down.
#
# 66 is not produced by bash (1, 2, 126, 127, 128+n), by psql (1, 2, 3), or by
# make. It cannot be made collision-proof -- `set -e` propagates any status an
# aborting command returns -- so the runners ALSO require the SKIPPED line in the
# log before believing it. Two independent signals, because one was not enough.
PGC_EXIT_SKIPPED=66

# The status pgc_summary uses for "a check could not be evaluated".
#
# NOT 66. 66 means the suite ran no checks at all; a suite holding one unrunnable
# check DID run checks, and collapsing the two loses the difference between "this
# suite is inert" and "this suite could not evaluate one thing". 67 is chosen on
# the same grounds 66 was -- bash produces 1, 2, 126, 127 and 128+n, psql 1, 2, 3,
# make 1 and 2 -- and, as with 66, the code alone is not trusted: a runner must
# also see the INCOMPLETE line in the log, because `set -e` propagates whatever
# status an aborting command returned.
PGC_EXIT_INCOMPLETE=67

# Counts for the three states. Every state is in a total or it is a state that
# can go missing, and pgc_summary reconciles them against PGC_CHECKS.
PGC_PASSED=0
PGC_FAILED=0
PGC_UNRUN=0

# The closed set of reasons a check could not be evaluated. Prose would have to
# be rewritten at every call site the day anything wants to group these, so the
# reason is a code plus a detail from the start. A code outside this set is a
# FAILURE rather than a silent acceptance: an enum that accepts anything is not
# an enum, and the first invented code would make it prose again.
# Set by pgc_summary the moment it starts, and read by the EXIT trap (#1233).
# It answers one question: did this suite reach its own summary? A suite that
# dies on one of pgc_setup's FATALs never does, and before this it also produced
# no accounting line -- which is the same signal a mid-run crash gives.
PGC_SUMMARY_PRINTED=0
PGC_UNRUN_REASONS="MISSING_DEPENDENCY UNSUPPORTED_MAJOR ABSENT_FIXTURE UNAVAILABLE_ENDPOINT UNMET_PRECONDITION"

# ---- cluster identity helpers ----------------------------------------------

# Normalize a directory for comparison. `cd && pwd -P` is POSIX; realpath -m is
# GNU only, and falling back to a raw string compare would compare exactly the
# unresolved paths this check exists to reconcile.
pgc_norm_path() {
	(cd "$1" 2>/dev/null && pwd -P) || printf '%s\n' "$1"
}

# The data directory of whatever server answers on PGC_PORT, or empty.
# Retried, because pg_ctl -w can return just before the server is connectable and
# an unanswered probe must never be mistaken for a match.
pgc_cluster_datadir() {
	local _i _d

	for _i in $(seq 1 15); do
		_d="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
			-U postgres -d postgres -At -c 'SHOW data_directory' 2>/dev/null)"
		if [ -n "$_d" ]; then
			printf '%s\n' "$_d"
			return 0
		fi
		sleep 1
	done
	return 1
}

. "$(dirname "${BASH_SOURCE[0]}")/portlib.sh"

# True when the server answering on PGC_PORT is the cluster at PGC_PGDATA. This is
# the guard the start loop applies before trusting a started cluster; naming it
# lets the self-test exercise the decision itself rather than only its inputs. An
# empty (unanswered) probe is deliberately not ours.
pgc_cluster_is_ours() {
	local _d
	_d="$(pgc_cluster_datadir)"
	[ -n "$_d" ] && 		[ "$(pgc_norm_path "$_d")" = "$(pgc_norm_path "$PGC_PGDATA")" ]
}

# ---- setup / teardown ------------------------------------------------------

# pgc_so_line
#		One line naming the shared library the suites are about to exercise.
#
# Printed on every run, not only when something looks wrong, because the failure
# it catches is invisible in a PASS/FAIL list: a suite reporting checks against a
# binary nobody just built. Three separate instances in one session -- a compile
# error the harness did not check (#508), a PGC_SKIP_BUILD run that skipped the
# INSTALL and exercised a guard-removed leftover, and objects from another major
# linked into a third -- all produced plausible results and all were one md5sum
# from being obvious.
#
# It also makes a red-on-change proof self-evidencing: two arms that report the
# same hash have proved nothing, whatever their check counts say.
pgc_so_line() {
	local so
	so="$("$PGC_PG_CONFIG" --pkglibdir)/pgcolumnar.so"
	if [ -r "$so" ]; then
		echo "-- .so: $(md5sum "$so" | cut -c1-12) $so"
	else
		echo "-- .so: NOT PRESENT at $so"
	fi
}

# pgc_build_and_install SRCDIR PG_CONFIG MAJOR
#
# Build and install the extension, or fail. Extracted from pgc_setup UNCHANGED so
# the pytest harness can drive the same implementation instead of carrying a
# second one: test/pytest/ never built or installed, so it reported 25 passed
# against source carrying `#error THIS SOURCE IS BROKEN AND CANNOT BUILD`
# (@jdatcmd, #897 review). Two implementations of "is the thing under test the
# thing in this tree" would drift, and the drift would be invisible in exactly
# the way that defect was.
#
# Returns non-zero rather than calling exit, so a caller that is not a suite --
# the Python harness -- can turn it into its own kind of failure. pgc_setup
# passes the exit through, so bash behaviour is unchanged.
pgc_build_and_install() {
	_pgc_bi_src="$1"
	_pgc_bi_cfg="$2"
	_pgc_bi_major="$3"
	_pgc_bi_stamp="$_pgc_bi_src/.pgc_built_for_major"
	_pgc_bi_had="$(cat "$_pgc_bi_stamp" 2>/dev/null | tr -dc '0-9')"
	_pgc_bi_objs=no
	[ -n "$(find "$_pgc_bi_src/src" -maxdepth 1 -name '*.o' -print -quit 2>/dev/null)" ] && _pgc_bi_objs=yes
	# ASK THE OBJECTS FIRST, THEN THE STAMP (#1219). These are two different
	# questions and the stamp cannot answer the dangerous one.
	#
	# The stamp records which major THIS HARNESS last built. A hand-run `make`
	# for another major leaves foreign objects and never touches it, so had and
	# major agree, the clean below is skipped, make finds everything up to date,
	# and one major's objects are installed into another's prefix. The objects
	# themselves carry the answer and cannot be out of step with themselves.
	#
	# The stamp check stays rather than being replaced: it catches the case
	# where the objects cannot be read at all (no DWARF, no readelf), and it is
	# the cheaper of the two. Both fail towards cleaning.
	_pgc_bi_foreign="$(pgc_objects_built_for "$_pgc_bi_src" "$_pgc_bi_cfg")"
	# Objects from another major link but do not load (#536).
	if [ "$_pgc_bi_foreign" = no ]; then
		# SAY WHICH CHECK REFUSED. pgc_build_stale_message reports the STAMP,
		# and in this case the stamp agrees with the major being built -- that
		# is the whole point. Printing it here tells the reader "last built for
		# 17 and this run wants 17; cleaning first", which is a contradiction
		# and sends them at the wrong thing.
		echo "-- the objects in the tree were not built against" \
			"$("$_pgc_bi_cfg" --includedir-server 2>/dev/null || echo 'this major')," \
			"whatever the build stamp says; cleaning first (#1219)"
		make -C "$_pgc_bi_src" clean PG_CONFIG="$_pgc_bi_cfg" >/dev/null 2>&1 || true
	elif [ "$(pgc_build_needs_clean "$_pgc_bi_had" "$_pgc_bi_major" "$_pgc_bi_objs")" = yes ]; then
		pgc_build_stale_message "$_pgc_bi_had" "$_pgc_bi_major"
		make -C "$_pgc_bi_src" clean PG_CONFIG="$_pgc_bi_cfg" >/dev/null 2>&1 || true
	fi
	echo "-- building"
	if ! make -C "$_pgc_bi_src" PG_CONFIG="$_pgc_bi_cfg" >/dev/null; then
		echo "FATAL: the build failed, so there is nothing new to test" >&2
		echo "       (refusing to report checks against the previously installed .so)" >&2
		return 1
	fi
	# Stamped only after a build that succeeded. printf '%s\n', NOT '%s\\n':
	# the doubled backslash writes the four bytes 1 9 \ n, which only worked
	# because the reader strips non-digits. Caught in review, not by a test.
	pgc_write_build_stamp "$_pgc_bi_stamp" "$_pgc_bi_major"
	echo "-- installing"
	if ! make -C "$_pgc_bi_src" install PG_CONFIG="$_pgc_bi_cfg" >/dev/null; then
		echo "FATAL: the install failed, so the .so under test is not the one just built" >&2
		echo "       (refusing to report checks against the previously installed .so)" >&2
		return 1
	fi

	# THIS CALL IS THE CONTROLLER for whatever follows in this batch: it built
	# and installed, so record what the binary was built from.
	#
	# THE STAMP IS WRITTEN ON THE PATH THAT BUILT AND INSTALLED, AND ON NO OTHER.
	# That is a statement about WHICH PATH, not about which line, and it is why
	# the write lives HERE rather than in pgc_setup. The pytest harness calls
	# this function directly, so a stamp written in the caller is not written at
	# all for that harness, and every pytest run reports "freshness UNVERIFIED"
	# while looking healthy. Hoisted above the install it would be written for
	# an install that may have failed.
	#
	# The wording matters because this is a merge conflict site. #898's version
	# of this comment said the stamp is "written HERE and nowhere else", meaning
	# not in the SKIP-build branch -- but at the conflict that reads as a claim
	# about the LINE and argues for leaving the write in pgc_setup, which is the
	# resolution that silently disables the check for the pytest harness
	# (@jdatcmd, #898 approval).
	#
	# An earlier revision wrote it in the SKIP-build branch, which made the check
	# tautological -- every run recorded the source it was about to compare
	# against, so a suite measuring an edited tree reported "matches the binary
	# under test". A red arm caught it, which is the only reason this exists.
	# The digest is read AFTER the install, because the install is what writes the
	# library. Taken before, it would record the previous one and certify exactly
	# the state this check exists to refuse.
	pgc_record_source_stamp "$_pgc_bi_src" "$_pgc_bi_cfg"
	return 0
}

# ONE PLACE KNOWS WHAT A RECORD OF AN INSTALL IS (#1230).
#
# The three arguments have to be gathered in one order and at one moment: the
# path is keyed by the installation, the fingerprint is of the SOURCE, and the
# digest must be read AFTER the install, because the install is what writes the
# library. Taken before, it records the previous one and certifies exactly the
# state the freshness check exists to refuse.
#
# It is a function rather than three lines at each caller because there are now
# two callers. `rebuild.sh` keeps its own build -- it has a parallel `-j`, the
# compiler-warning gate that mirrors the matrix, and error extraction from the
# build log, none of which the builder above has -- so it could not be routed
# through the builder to get the record. It was missing only the record, and a
# second copy of the stamp format is what makes the two drift.
pgc_record_source_stamp() {	# pgc_record_source_stamp SRCDIR PG_CONFIG
	# REFUSE RATHER THAN RECORD SOMETHING PLAUSIBLE (@jdatcmd, #1232 review).
	# With no arguments this did not fail, it wrote a believable record in the
	# wrong place: the path `./.pgc_source_stamp.0.nolibd41` in the CURRENT
	# DIRECTORY, keyed by the md5 of an EMPTY pkglibdir -- `d41` is the front of
	# d41d8cd98f00, the md5 of nothing -- carrying a fingerprint of the current
	# directory rather than of the source. Both callers pass real arguments, so
	# it was latent; a function whose job is to record what was installed must
	# refuse when it has not been told what was installed.
	[ -n "${1:-}" ] && [ -n "${2:-}" ] || return 1
	pgc_write_source_stamp \
		"$(pgc_source_stamp_path "${1:-}" "${2:-}")" \
		"$(pgc_source_fingerprint "${1:-}")" \
		"$(pgc_installed_library_digest "${2:-}")"
}

pgc_setup() {
	PGC_PG_CONFIG="${1:-/usr/local/pg17/bin/pg_config}"
	PGC_BINDIR="$("$PGC_PG_CONFIG" --bindir)"
	# One definition of the server major, for the suites that must branch on it.
	# A behavior that exists only from some major is core's, not this extension's,
	# and a check written against the newer one fails on the older ones for a
	# reason that is not a defect. Branch on this rather than deriving it again.
	PGC_MAJOR="$(pgc_major_of "$PGC_PG_CONFIG")"
	# Derived from this process rather than a fixed 54329: two suites run at
	# once on one box otherwise start on the same port, and the loser reports a
	# wall of ERROR: database "regress" already exists with no named check
	# failing -- a red that reads exactly like a real one. There is a retry
	# below for when this still collides; the default should not guarantee it.
	PGC_PORT="${PGC_PORT:-$(pgc_pick_port)}"
	PGC_DB="${PGC_DB:-regress}"
	PGC_LIBDIR="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")"
	PGC_SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

	PGC_WORKDIR="$(mktemp -d /tmp/pgcolumnar-test.XXXXXX)"
	PGC_PGDATA="$PGC_WORKDIR/data"
	PGC_LOGFILE="$PGC_WORKDIR/server.log"
	PGC_SQLDIR="$PGC_WORKDIR/sql"
	mkdir -p "$PGC_SQLDIR"
	chmod 777 "$PGC_WORKDIR" "$PGC_SQLDIR"

	echo "== pgColumnar test: $(basename "$0") =="
	echo "PG_CONFIG=$PGC_PG_CONFIG"
	echo "version=$("$PGC_PG_CONFIG" --version)"
	echo "workdir=$PGC_WORKDIR"

	# initdb and pg_ctl cannot run as root; use postgres when we are root.
	#
	# Settled BEFORE the build, and before the trap below, because pgc_teardown
	# reaches pg_ctl through pgc_pg, which expands PGC_RUNPG. Installing the trap
	# while that array is still unset would turn any early failure into an
	# unbound-variable error under `set -u` instead of a cleanup.
	if [ "$(id -u)" = "0" ]; then
		PGC_RUNPG=(runuser -u postgres --)
		chown -R postgres "$PGC_WORKDIR"
		chmod 777 "$PGC_WORKDIR" "$PGC_SQLDIR"
	else
		PGC_RUNPG=(env)
	fi

	# Armed here rather than after the build: the build below can exit, and
	# between mktemp above and this line there is nothing to remove the workdir.
	# Stopping a cluster that was never started is a no-op, so arming it early
	# costs nothing and covers every failure path after the directory exists.
	trap pgc_on_exit EXIT

	# The matrix runner builds and installs once per version and sets
	# PGC_SKIP_BUILD so parallel suites do not each rebuild (a no-op relink) or
	# race on writing the shared .so during "make install". A suite run on its own
	# still builds and installs.
	#
	# Both steps are status-checked. lib.sh sets `set -uo pipefail` but not -e, so
	# an unchecked make that fails to compile does not stop the suite: it carries
	# on and runs every check against the PREVIOUSLY INSTALLED .so, then prints a
	# full PASS/FAIL report for code that does not exist. That is indistinguishable
	# from a real result, and it was caught only because someone fingerprinted the
	# installed .so and saw the same hash either side of a source change that could
	# not have produced it.
	if [ -z "${PGC_SKIP_BUILD:-}" ]; then
		pgc_build_and_install "$PGC_SRCDIR" "$PGC_PG_CONFIG" "$PGC_MAJOR" || exit 1
	else
		# Named because the variable is not what it says. It reads as "skip the
		# build" and means "skip the build AND the install, and test whatever is
		# already installed" -- correct for the matrix, which installs once per
		# major before setting it, and a trap for a person who has just edited
		# source and run make by hand.
		echo "-- PGC_SKIP_BUILD=1: not building AND NOT INSTALLING;"
		echo "   whatever is already installed is what these checks measure"
	fi

	pgc_so_line

	# And verify it, whether this run built or skipped. A skipped build is exactly
	# when the binary can be older than the source.
	_pgc_stamp_file="$(pgc_source_stamp_path "$PGC_SRCDIR" "$PGC_PG_CONFIG")"
	_pgc_fresh_recorded="$(pgc_read_source_stamp "$_pgc_stamp_file")"
	_pgc_fresh_current="$(pgc_source_fingerprint "$PGC_SRCDIR")"
	# AND THE ARTIFACT, not only the source (#959). The sentence below is about the
	# BINARY, so the binary has to be evidence in it.
	_pgc_bin_recorded="$(pgc_read_installed_stamp "$_pgc_stamp_file")"
	_pgc_bin_current="$(pgc_installed_library_digest "$PGC_PG_CONFIG")"
	case "$(pgc_freshness_claim \
			"$(pgc_freshness_verdict "$_pgc_fresh_recorded" "$_pgc_fresh_current")" \
			"$(pgc_binary_identity_verdict "$_pgc_bin_recorded" "$_pgc_bin_current")")" in
		verified)
			echo "-- source: $_pgc_fresh_current matches the binary under test"
			;;
		source-only)
			# The stamp predates #959, so it records the source and not the library.
			# The source claim is still earned; the binary claim is not, and saying
			# the first while implying the second is the defect this removes.
			echo "-- source: $_pgc_fresh_current matches what this tree last built;"
			echo "   the installed library was not recorded, so it is UNVERIFIED"
			;;
		refuse-binary)
			echo "FATAL: the installed library is not the one this tree built" >&2
			echo "       library now $_pgc_bin_current, this tree installed $_pgc_bin_recorded" >&2
			echo "       (another build wrote $("$PGC_PG_CONFIG" --pkglibdir 2>/dev/null)," >&2
			echo "        so these checks would measure somebody else's binary)" >&2
			exit 1
			;;
		refuse-source)
			echo "FATAL: the binary under test was not built from this source" >&2
			echo "       source now $_pgc_fresh_current, binary built from $_pgc_fresh_recorded" >&2
			echo "       (refusing to report checks about code that is not installed)" >&2
			# WHAT IT HASHED, not just what it hashed TO. Two CI failures reported
			# this pair of hashes and nothing else, identically, across two
			# branches and two majors -- which made the second occurrence another
			# sample rather than an answer. Twelve characters cannot name a file;
			# the manifest can, and a diff against the next occurrence's manifest
			# says whether something appeared, vanished or changed.
			pgc_freshness_report "$PGC_SRCDIR" >&2
			exit 1
			;;
		unverified)
			# Not a failure: a person who ran make install by hand has no stamp, and
			# refusing would break a documented workflow. Said plainly so the reader
			# knows which question was not answered.
			echo "-- source: $_pgc_fresh_current, freshness UNVERIFIED (no stamp for major $PGC_MAJOR)"
			;;
	esac

	echo "-- initdb"
	pgc_pg "initdb -D '$PGC_PGDATA' -A trust" >/dev/null 2>&1
	{
		echo "port=$PGC_PORT"
		echo "listen_addresses='127.0.0.1'"
		echo "shared_preload_libraries='pgcolumnar'"
		# Deterministic text output so heap and columnar hashes match.
		echo "extra_float_digits=3"
		echo "timezone='UTC'"
		echo "datestyle='ISO, MDY'"
		echo "bytea_output='hex'"
		# Keep planner honest but let small tables use the custom scan.
		echo "max_parallel_workers_per_gather=0"
		# No pgcolumnar.* GUC is set here. A global override makes every suite
		# in the tree measure something other than the shipped default, and the
		# divergence is invisible from inside the suite that trips over it: this
		# line used to read unique_lock_buckets=100003 against a shipped default
		# of 128, so a 20,000-row insert into a table with a unique index
		# exhausted max_locks_per_transaction and failed with "out of shared
		# memory" -- an error that reads as a product defect and is not one
		# (#799). unique_conc.sh sets the value on its own cluster, where it
		# belongs; anything else that needs a non-default GUC has PGC_EXTRA_CONF
		# below. Selftest 280 keeps this section free of them.
		# Per-suite extra GUCs, set before the cluster starts (some, like
		# max_prepared_transactions, are PGC_POSTMASTER and cannot be changed
		# later). parallel_copy.sh uses this for 2PC capacity + worker slots.
		[ -n "${PGC_EXTRA_CONF:-}" ] && printf '%s\n' "$PGC_EXTRA_CONF"
	} | pgc_pg "cat >> '$PGC_PGDATA/postgresql.conf'"

	# Start, retrying a few times: under rapid cluster churn (the version matrix
	# runs many throwaway clusters back to back) a start can transiently fail to
	# bind or acquire resources before the previous cluster is fully gone.
	#
	# The retry must also not hand this suite someone else's cluster. pg_ctl -w
	# only proves that *something* answers on the port, so if another suite's
	# postmaster already owns it, ours failed to bind while pg_ctl reported
	# success. Every statement would then run against that cluster: its log grows
	# a stray "database already exists" and this suite's own objects are invisible.
	# So the server answering on PGC_PORT must identify itself as ours before the
	# suite proceeds, and if it never does, the suite fails rather than guessing.
	echo "-- start"
	{
		local _a _i _dd _started _nforeign

		_started=0
		_nforeign=0
		for _a in 1 2 3 4 5 6 7 8; do
			_dd=""
			if pgc_pg "pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1; then
				if pgc_cluster_is_ours; then
					_started=1
					break
				fi
				_dd="$(pgc_cluster_datadir)"
			fi
			if [ -n "$_dd" ]; then
				_nforeign=$(( _nforeign + 1 ))
				echo "-- port $PGC_PORT serves $_dd, not ours; retrying on a fresh port"
			else
				echo "-- start attempt $_a failed; retrying on a fresh port"
			fi
			# Only ever stops our own data directory, so a squatter is never touched.
			pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m immediate -w" >/dev/null 2>&1 || true
			# Prefer a port nothing is already listening on, so collisions are
			# avoided rather than merely detected afterwards.
			for _i in 1 2 3 4 5 6 7 8 9 10; do
				# Stays inside the main band. A retry that lands in the
				# ephemeral range can be stolen between this probe and the bind
				# below, which is the failure the retry exists to escape; one
				# that lands in the auxiliary band collides with the extra
				# clusters replication stands up. See portlib.sh.
				PGC_PORT=$(( PGC_PORT_LO + (PGC_PORT + 1 + RANDOM % 5000) % (PGC_PORT_HI - PGC_PORT_LO) ))
				if pgc_port_free "$PGC_PORT"; then
					break
				fi
			done
			pgc_pg "sed -i 's/^port=.*/port=$PGC_PORT/' '$PGC_PGDATA/postgresql.conf'"
			sleep 1
		done

		if [ "$_started" != "1" ]; then
			# The reason FIRST, then the verdict. pg_ctl -l has been writing it
			# to this file since attempt one, and pgc_teardown removes the
			# workdir on exit, so a verdict without it is the last thing anyone
			# sees before the evidence is deleted (#537).
			pgc_start_log_report "${PGC_LOGFILE:-}"
			pgc_start_failure_message "$_a" "$PGC_PORT" "$_nforeign" >&2
			exit 1
		fi
	}
	# The cluster is up, connectable, confirmed ours, and was initdb'd minutes ago
	# into a private mktemp directory. So PGC_DB cannot legitimately already
	# exist, and there is nothing here to retry: create it once, and treat
	# anything else as the problem it is.
	#
	# This used to loop ten times, creating the database and then failing to
	# notice it had. The check asked psql_admin, which does not pass -At, so a
	# one-row answer came back as a bordered table and "tr -dc 0-9" reduced
	# "(1 row)" along with the value to "11" -- never equal to "1". The loop
	# therefore always ran to its limit, and every suite emitted nine
	# ERROR: database "regress" already exists into the server log on every run,
	# passing or failing.
	#
	# That noise is why a genuine failure was twice read as port contention, by
	# two different people on the same day: a red result whose log is a wall of
	# "already exists" looks exactly like a suite that landed on someone else's
	# cluster. Removing the noise is most of the value here; failing loudly on
	# the impossible case is the rest.
	# The server is up, so now ask whether it is RUNNING the binary we verified on
	# disk. A cp is not enough: shared_preload_libraries maps the .so at start.
	pgc_check_running_binary "$("$PGC_PG_CONFIG" --pkglibdir)/pgcolumnar.so" || exit 1

	{
		local _exists

		_exists="$(psql_admin_scalar "SELECT count(*) FROM pg_database WHERE datname = '$PGC_DB';")"
		case "$_exists" in
			0)
				if ! psql_admin "CREATE DATABASE $PGC_DB;" >/dev/null 2>&1; then
					echo "FATAL: could not create database $PGC_DB on our own cluster" >&2
					exit 1
				fi
				;;
			1)
				echo "FATAL: database $PGC_DB already exists on a cluster this suite" >&2
				echo "       just created from a fresh initdb in $PGC_PGDATA." >&2
				echo "       That means the server on port $PGC_PORT is not the one we" >&2
				echo "       started, so nothing this suite reports would be about the" >&2
				echo "       build under test." >&2
				exit 1
				;;
			*)
				echo "FATAL: could not determine whether $PGC_DB exists (got '$_exists')" >&2
				exit 1
				;;
		esac
	}
	psql_run "CREATE EXTENSION pgcolumnar;" >/dev/null
}

# ---- the accounting a declared suite owes even when it dies early (#1233) ---
#
# pgc_setup has EIGHT exit paths -- the build, the concurrent-install stamp, the
# freshness report, the start failure, the running-binary check, the database
# create, a cluster we did not start, and the existence check -- and calls
# pgc_summary on none of them. Every one of the 252 suites that calls pgc_setup
# also calls pgc_summary, so every one of them DECLARES accounting and then, on
# any of those eight, exits without producing it.
#
# That is not a false PASS: rc is 1 and the FATAL is correct. What is lost is
# the machine-readable line, and six readers consume it -- run_all_versions.sh's
# reconciliation, pgc_vacuity.py, test_check_records.py,
# test_residual_is_counted.py, test_suite_accounting.py and smoke.sh. To all of
# them a declared suite with no accounting is indistinguishable from a suite
# that died mid-run. run_all_versions.sh already names the case in the
# reconciliation's own comment -- "declared but never accounted: the suite died
# before reaching its summary" -- so the comment is older than the defect.
#
# EMITTED FROM THE TRAP, WHICH IS ONE SITE RATHER THAN EIGHT. pgc_teardown
# already runs on every one of those exits. A per-exit patch fixes the ones that
# exist today and cannot cover the ninth; this covers it by construction.
#
# THE FALSE-POSITIVE POPULATION IS EMPTY, measured rather than assumed. A suite
# that deliberately exits early would be mislabelled TERMINATED, and a guard
# that mislabels correct behaviour gets switched off. There are twelve exits in
# this file and no others reachable from a suite: eight in pgc_setup and four in
# pgc_summary. No suite of the 252 has a bare exit of its own, and pgc_skip --
# the one helper that looks like an early exit -- ends with pgc_summary on both
# branches. So every exit from a declared suite is either a FATAL or the
# summary, and there is no third case to get wrong.
pgc_exit_accounting() {
	[ "${PGC_SUMMARY_PRINTED:-0}" = 0 ] || return 0
	echo
	echo "checks run: $PGC_CHECKS"
	echo "checks unrunnable: $PGC_UNRUN"
	# THE SAME SHAPE THE READERS MATCH, deliberately: pgc_log_shows_accounting
	# anchors on this exact line, and a differently-shaped one would leave the
	# reconciliation still unable to see the suite.
	echo "accounting: $PGC_PASSED passed + $PGC_FAILED failed + $PGC_UNRUN unrunnable + $PGC_SKIPPED skipped = $PGC_CHECKS"
	# AND A MARKER, so the three states stay distinguishable to a READER as well
	# as to a parser: ran to completion (pgc_summary prints PASSED, FAILED,
	# SKIPPED or INCOMPLETE), terminated before its summary (this), and died
	# mid-run (no accounting at all, which is what the reconciliation still
	# catches).
	echo "$(basename "$0"): TERMINATED before its summary"
}

# The EXIT trap: accounting first, then teardown, and the status untouched.
# `$?` is captured on the first line because everything after it overwrites it,
# and the trap must not change what the suite exited with -- a FATAL is rc=1 and
# stays rc=1.
pgc_on_exit() {
	local _rc=$?
	pgc_exit_accounting
	pgc_teardown
	return $_rc
}

pgc_teardown() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m immediate -w" >/dev/null 2>&1 || true
	rm -rf "$PGC_WORKDIR"
}

# Run a command as the postgres OS user (for initdb/pg_ctl).
pgc_pg() {
	"${PGC_RUNPG[@]}" env PATH="$PGC_BINDIR:$PATH" bash -lc "$1"
}

# ---- reporting a failure that happened before any check ran (#537) ----------

# The events in a server log that mean "this was not a failed assertion", for the
# SUMMARY path.
#
# There are deliberately TWO patterns, not one, and an earlier version of this
# comment claimed they were one shared definition while the code had already
# diverged -- the exact defect #537 is about, committed in the fix for it. They
# are named functions so the divergence is visible and greppable rather than two
# literals in two places: pgc_fatal_pattern here, pgc_start_fatal_pattern below.
# Their reasons for differing are given at each.
#
# "could not load library" is the addition. Bare "FATAL:" deliberately is NOT in
# here, and the reason is measured rather than reasoned, because the first reason
# written here was wrong and did not survive being checked.
#
# What is true: a crash restart produces routine FATALs in TWO classes, and
# neither is a cause of anything. A PASSING run of native_backend_crash.sh leaves
# two lines of the first class; forcing a crash and then attempting twelve
# connections during the recovery window produces both:
#
#     5  FATAL:  the database system is not yet accepting connections
#     3  FATAL:  the database system is in recovery mode
#
# The second class is the one that matters for this decision, because its count
# scales with how many connections arrive during recovery rather than with
# anything about the failure. So the wallpaper bare FATAL would print is not
# bounded at the two lines the crash suite happens to show; a busier run prints
# as many as it raced. Matching them would put a consequence under "first fatal
# events" as though it were a cause, which is the exact defect #537 exists to
# fix. PANIC stays in the pattern because a PANIC is a cause.
#
# What is NOT true, and was the original justification here: that a cluster
# stopped with -m immediate logs a routine FATAL per live backend. Measured twice,
# independently, on two majors and two machines -- four backends held open on
# pg_sleep, then pg_ctl stop -m immediate:
#
#     PG18: 0 FATAL lines        PG17: 0 FATAL lines, before and after
#
# An immediate stop SIGQUITs them and they log nothing. Do not restore that
# reasoning. It is recorded here BECAUSE it is the intuitive answer and will
# otherwise be re-derived by whoever reads this next; it was written into this
# file once already as though it were a finding.
#
# The START path can afford a bare FATAL grep, and does one, because a cluster
# that never started has produced no routine FATALs to confuse it.
pgc_fatal_pattern() {
	printf '%s\n' 'AddressSanitizer|UndefinedBehaviorSanitizer|runtime error:|terminated by signal|PANIC:|could not load library'
}

# The same question for the START path, which can afford a bare FATAL where the
# summary path cannot. A cluster that never started has produced no routine
# FATALs -- the two routine classes both come from crash RECOVERY, which requires
# having started -- so here every FATAL is a candidate cause.
pgc_start_fatal_pattern() {
	printf '%s\n' 'FATAL:|PANIC:'
}

# What the server log says about a cluster that would not start.
#
# Takes the log path so it can be tested against a fixture without standing a
# cluster up. Prints the first FATAL lines with their line numbers, then a tail,
# and says so explicitly when it found neither -- silence here reads as "there
# was nothing to say", which was the whole complaint in #537.
pgc_start_log_report() {
	local _log="$1" _fatal _tail

	if [ -z "$_log" ] || [ ! -s "$_log" ]; then
		echo "---- server log: absent or empty at ${_log:-<unset>} ----" >&2
		return 0
	fi

	_fatal="$(grep -nE "$(pgc_start_fatal_pattern)" "$_log" 2>/dev/null | head -5 || true)"
	if [ -n "$_fatal" ]; then
		echo "---- why the cluster would not start ----" >&2
		printf '%s\n' "$_fatal" >&2
	else
		echo "---- no FATAL in the server log; its tail follows ----" >&2
	fi
	_tail="$(tail -20 "$_log" 2>/dev/null || true)"
	if [ -n "$_tail" ]; then
		echo "---- server log tail ($_log) ----" >&2
		printf '%s\n' "$_tail" >&2
	fi
	return 0
}

# The verdict, which must not assert a cause the code has not established.
#
# The third argument is HOW MANY attempts actually found another cluster's data
# directory on the port. A count rather than a flag, because a flag was sticky:
# set on any attempt and never cleared, so one squatter on attempt 1 followed by
# seven genuine start failures printed the squatter verdict for all eight. That
# is #537's own defect narrowed rather than removed, and it is reachable, since
# escaping a port collision is what the retry loop exists for.
#
# Three cases, and the mixed one is why this is not a branch on zero.
pgc_start_failure_message() {
	local _attempts="$1" _port="$2" _nforeign="$3"

	printf '%s\n' "FATAL: no cluster of our own on port $_port after $_attempts attempts"
	if [ "$_nforeign" = "0" ]; then
		printf '%s\n' "       (nothing was squatting: our own postmaster failed to start, and the"
		printf '%s\n' "        reason is in the server log reported above)"
	elif [ "$_nforeign" = "$_attempts" ]; then
		printf '%s\n' "       (a cluster this suite does not own held the port on every attempt;"
		printf '%s\n' "        refusing to use it)"
	else
		printf '%s\n' "       ($_nforeign of $_attempts attempts found a cluster this suite does not"
		printf '%s\n' "        own; the other $(( _attempts - _nforeign )) failed to start, and that"
		printf '%s\n' "        reason is in the server log reported above)"
	fi
}


# ---- an in-tree build must not reuse another major's objects (#536) ---------
#
# lib.sh builds in $PGC_SRCDIR with no clean and no record of which major the
# objects belong to. One suite against pg18a then pg19a in the same tree links
# the first run's objects into the second .so, which fails to load with
# "undefined symbol: get_relation_info_hook": every cluster start dies and the
# suite reports eight retries with no cause.
#
# The MATRIX is not exposed -- run_all_versions.sh cleans each per-major copy
# right after its cp -a. Measured, after #536 was filed claiming otherwise.
#
# Objects present with NO stamp are unknown provenance and must be cleaned: that
# is what a hand-run `make PG_CONFIG=...` leaves, which is how anyone debugging
# builds and how every gate script here builds.
# pgc_objects_built_for SRCDIR PG_CONFIG -> yes | no | unknown
#
# WHICH MAJOR BUILT THE OBJECTS, asked of the objects themselves (#1219).
#
# pgc_build_needs_clean compares a STAMP this harness writes against the major
# being built. Neither side is a property of the objects in the tree, so a
# hand-run `make` for another major leaves foreign objects and never touches the
# stamp: have and want agree, the clean is skipped, make finds everything up to
# date, and one major's objects are installed into another's prefix. Measured by
# @OffgridwithJD -- a PG18 run died on `undefined symbol:
# build_simple_rel_hook`, which is the PG19 name.
#
# THE STAMP FAILS CLOSED IN ONE DIRECTION AND OPEN IN THE OTHER, and only the
# open direction is dangerous. A stale or absent stamp forces a clean nobody
# needed. A stamp that MATCHES while the objects are foreign is silent.
#
# THIS FUNCTION HAS THREE ANSWERS, NOT TWO, and what each costs differs:
#
#     DWARF present and disagrees  ->  no       caller cleans, whatever the stamp says
#     pg_config cannot be asked    ->  no       caller cleans
#     no objects                   ->  unknown  stamp decides
#     no -g, or no readelf         ->  unknown  stamp decides
#
# An earlier revision of this paragraph claimed every failure returns "no". That
# was true before the -g refinement below and was not updated with it -- a
# record drifting from the thing it describes, inside a change about exactly
# that. Caught in review by @OffgridwithJD, not by any test here.
#
# The build passes -g, so each .o carries a DWARF directory table naming the
# server headers it was compiled against. Compare that against what pg_config
# says it wants.
#
# DO NOT PARSE A MAJOR NUMBER OUT OF THE PATH. The layouts differ --
# /usr/local/pgNN/include/postgresql/server against
# /usr/include/postgresql/NN/server -- and a pattern written for one returns
# EMPTY for the other, which reads exactly like "not derivable". Asking
# pg_config for the string and comparing it whole needs no knowledge of either.
#
# DO NOT USE `grep -q` HERE. This file runs under `set -o pipefail`; `grep -q`
# exits at the first match, readelf dies on the closed pipe, and the pipeline
# reports FAILURE for a pipeline that matched. Measured by @OffgridwithJD on the
# same object: `grep -cF` gives 14, `grep -qF` gives rc 141. `grep -c` reads all
# of its input, so it cannot raise SIGPIPE.
#
# EVERY object is checked, not the first. A tree half-rebuilt across majors is
# precisely the state a cross-major preflight leaves behind, so it is the case
# this exists for rather than an edge of it.
pgc_objects_built_for() {
	local srcdir="${1:-}" cfg="${2:-}" want obj n hits dbg

	want="$("$cfg" --includedir-server 2>/dev/null)" || want=""
	[ -n "$want" ] || { echo no; return; }

	n=0
	for obj in "$srcdir"/src/*.o; do
		[ -f "$obj" ] || continue
		n=$((n + 1))

		# NO DEBUG INFO IS "UNKNOWN", NOT "FOREIGN", and the difference is the
		# whole design. A server built without -g -- /usr/local/pg18_nc here,
		# whose pg_config --cflags carries no -g at all -- produces objects with
		# ZERO .debug_ sections, so there is nothing to compare. Reporting those
		# as foreign is correct in the fail-closed sense and useless in
		# practice: every suite would clean and rebuild on every run, and a
		# guard that makes the tree slow gets switched off, taking the rule with
		# it. Returning unknown hands the decision back to the stamp, which is
		# exactly the behaviour those builds have today -- no protection gained,
		# none lost.
		#
		# WHICH BUILDS LOSE OUT IS NOT ARBITRARY, and that is why this is a
		# choice rather than a hole. The prefixes without -g here are the ones
		# configured with neither --enable-cassert nor --enable-debug (measured:
		# pg17_nc and pg18_nc are cassert=0 debug=0; pg17 is 1 and 1). Those are
		# the measurement builds by local convention -- their output is numbers,
		# not verdicts -- so the build kind that keeps stamp-only protection is
		# the one whose results are timings rather than correctness claims. The
		# repository does not declare that convention, so treat it as an
		# observation about this host rather than a guarantee.
		#
		# UNDEFINED SYMBOLS WERE THE OBVIOUS FALLBACK AND THEY DO NOT WORK.
		# @OffgridwithJD built it rather than arguing it: `nm -D` needs no debug
		# info and the count does carry provenance -- the correct pairing is the
		# minimum in every row -- but even the correct pairing leaves 49 symbols
		# unresolved, because libc supplies them at load. So there is no
		# threshold for "is this foreign", only "which candidate server is it
		# least foreign to", which needs every prefix on the box to decide one
		# thing about one prefix. And it is weaker where it matters: symbols
		# catch the LOAD failure, not an ABI change that is symbol-compatible
		# and corrupts quietly. DWARF provenance catches both.
		dbg="$(readelf -S "$obj" 2>/dev/null | grep -c 'debug_')" || dbg=0
		case "$dbg" in '' | *[!0-9]*) dbg=0 ;; esac
		[ "$dbg" -ge 1 ] || { echo unknown; return; }

		hits="$(readelf --debug-dump=line "$obj" 2>/dev/null \
			| sed -n '/The Directory Table/,/The File Name Table/p' \
			| grep -cF "$want")" || hits=0
		case "$hits" in '' | *[!0-9]*) hits=0 ;; esac
		[ "$hits" -ge 1 ] || { echo no; return; }
	done

	[ "$n" -ge 1 ] || { echo unknown; return; }
	echo yes
}

pgc_build_needs_clean() {
	local have="${1:-}" want="${2:-}" objects="${3:-}"

	case "$want" in '' | *[!0-9]*) echo yes; return ;; esac
	[ "$objects" = yes ] || { echo no; return; }
	[ -z "$have" ] && { echo yes; return; }
	case "$have" in *[!0-9]*) echo yes; return ;; esac
	[ "$have" = "$want" ] && echo no || echo yes
}

# The stamp writer, as a function so a check can exercise THE WRITER rather
# than a copy of it. It was written inline as printf '%s\\n' -- a doubled
# backslash inside single quotes -- which emits the four bytes `1 9 \ n`. That
# passed unnoticed because the reader does tr -dc '0-9' and strips the junk; a
# direct comparison against the major failed. Found in review, not by a check.
# ---- is the binary under test built from the source in this tree? -----------
#
# THE GAP THIS CLOSES, AND WHAT ALREADY COVERED THE REST.
#
# selftest 110 compares the INSTALLED .so against the one built in this tree, so a
# missed install and a foreign overwrite are already caught. Neither that check nor
# pgc_so_line can see the case where BOTH copies agree with each other and both are
# stale against edited source: nothing in the harness derives anything from the
# source text. That is the hole, and it is the one PGC_SKIP_BUILD opens widest,
# because its whole purpose is not to rebuild.
#
# Measured cost of the miss: a probe run under PGC_SKIP_BUILD=1 that asserted its fix
# was "present" by grepping the SOURCE while measuring a .so another worktree had
# installed. The control failed and the failure read as a product defect.
#
# THE CONTROLLER SHAPE. Whoever builds records a fingerprint of the build inputs
# beside the install. Every suite in that batch recomputes the fingerprint and
# compares. One hash per suite, one build per batch, and a stale binary can no longer
# report a plausible list of checks.
#
# pgc_freshness_verdict is a pure function of two strings so it can be tested without
# a build, the same reason pgc_build_needs_clean is.

# The build inputs, hashed. Sources, headers, the Makefile, the control file and the
# SQL that ships: anything whose change should invalidate a binary. Sorted, because a
# directory listing is not ordered and an unordered input makes the hash unstable.
# pgc_source_build_dirs DIR -> one source directory per line
#
# DERIVED, NOT LISTED. The first version read $dir/src only, and objstore/ is a
# SEPARATE shared library that the top-level Makefile builds and installs by
# recursion. Editing objstore/columnar_objstore_module.c left the fingerprint
# unchanged, so objstore_module, objstore_sink_write and objstore_stash_recovery
# could measure a stale module while the suite printed "matches the binary under
# test" (@jdatcmd, #898 review). A stale binary under an explicit assurance is
# worse than one under no assurance, because the line is what stops the next
# person checking.
#
# A remembered list would have the same defect again the next time a module is
# added, so this returns every directory that has its own Makefile. That is the
# same rule the build itself follows.
pgc_source_build_dirs() {	# pgc_source_build_dirs DIR -> dirs
	local dir="${1:-.}"
	printf '%s\n' "$dir/src"
	find "$dir" -mindepth 2 -maxdepth 2 -type f -name Makefile \
		-printf '%h\n' 2>/dev/null | grep -v "^$dir/src$" || true
}

# A value no md5sum digest can be, so it cannot be confused with one.
_pgc_fp_failed='PGC_FINGERPRINT_DIGEST_FAILED'

# What the fingerprint was taken over, for the FATAL path. A FUNCTION rather than
# three lines inline, so an arm can drive it: a dump nobody can run is a dump
# nobody knows is empty. Two CI failures printed only the pair of hashes, and the
# second occurrence was therefore another sample rather than an answer.
pgc_freshness_report() {	# pgc_freshness_report DIR -> the manifest, annotated
	local dir="${1:-.}" n
	n="$(pgc_source_manifest "$dir" | wc -l)"
	echo "       the manifest this fingerprint was taken over:"
	if [ "$n" -eq 0 ]; then
		echo "       | (empty -- nothing under $dir matched, which is itself the finding)"
	else
		pgc_source_manifest "$dir" | sed 's/^/       | /'
	fi
	echo "       ($n files, under $dir)"
}

# THE ONE IMPLEMENTATION LIVES IN test/pgc_fingerprint.py.
#
# This file and test/pytest/pgc_cluster.py each used to carry their own, and the
# pair produced four defects in one day -- two in each copy, and not one found by
# whoever wrote that copy (#907). The Python docstring asserted parity with this
# function throughout all four; it was false when written and stayed false through
# two rounds of fixing.
#
# PYTHON RATHER THAN SHELL, which is the opposite of what was first proposed here.
# The single implementation belongs in the more portable language: bash is largely
# a GNU thing, while Python is present on FreeBSD and Windows where bash is not
# (jd). This file already requires bash, so calling a more portable interpreter
# from it cannot cost portability. It is also not a cost at all -- the shell forked
# md5sum once per file, and one interpreter start beats 64 forks:
#
#     shell, forking md5sum per file    239 ms/call
#     the module                         26 ms/call
#     across 261 suites x 2 calls        124 s  ->  13 s
#
# It fixed a defect on the way, which is the argument for one implementation in
# miniature: `sort -z` uses LOCALE COLLATION and no locale is pinned anywhere in
# this harness, so the same tree fingerprinted two ways depending on whose desktop
# it was --
#
#     LC_ALL=C            6d122a7158d5
#     LC_ALL=en_US.UTF-8  0b59bd75fa4f
#
# A developer on an en_US.UTF-8 default stamping a tree that CI then reads under
# C.UTF-8 is a FATAL naming a stale binary against a clean tree. The module sorts
# bytes, which is what LC_ALL=C did and what every stamp on disk was written with.
_pgc_fp_module() {
	printf '%s\n' "$(dirname "${BASH_SOURCE[0]}")/pgc_fingerprint.py"
}

# SYSTEM python3, NOT the pytest venv. test/pytest/README.md records that the
# interpreter is EXTERNALLY-MANAGED and that pytest runs from a virtualenv; a
# freshness gate that needed those test dependencies would make every suite in
# this directory unrunnable until somebody had installed pytest. The module
# imports nothing outside the standard library and nothing from test/pytest/.
_pgc_fp_python() {
	command -v python3 2>/dev/null
}

# A MISSING python3 IS REPORTED, NOT SWALLOWED. Returning empty alone would make
# the verdict `unknown` and print "freshness UNVERIFIED", which is deliberately
# not a failure -- and the whole gate would then be off with nothing saying so.
# Loud but not fatal: the asymmetry this controller is built on is that a false
# UNVERIFIED costs a line of output while a false FATAL costs a matrix.
_pgc_fp_warn_once() {
	[ -n "${_pgc_fp_warned:-}" ] && return 0
	_pgc_fp_warned=1
	echo "-- python3 not found: the freshness check cannot run (see test/pgc_fingerprint.py)" >&2
}

pgc_source_manifest() {	# pgc_source_manifest DIR -> "relpath digest" per file, or EMPTY
	local py out rc
	py="$(_pgc_fp_python)" || true
	[ -n "$py" ] || { _pgc_fp_warn_once; printf '%s\n' "$_pgc_fp_failed"; return 0; }
	out="$("$py" "$(_pgc_fp_module)" manifest "${1:-.}" 2>/dev/null)"
	rc=$?
	# A DIGEST THAT FAILED MUST NOT LOOK LIKE ONE THAT SUCCEEDED. The module exits
	# 1 and prints nothing when a file could not be read, so the marker is emitted
	# here and pgc_source_fingerprint below turns it into no fingerprint at all.
	# Before this was true anywhere, a failed md5sum contributed an EMPTY digest
	# and the function returned a confident WRONG hash at status 0, which cost a
	# matrix (@OffgridwithJD, measured: 5 of 5 suites stale on a correct tree).
	[ "$rc" -eq 0 ] || { printf '%s\n' "$_pgc_fp_failed"; return 0; }
	[ -n "$out" ] || return 0
	printf '%s\n' "$out"
}

pgc_source_fingerprint() {	# pgc_source_fingerprint DIR -> hash, or EMPTY if it could not be computed
	local py out rc
	py="$(_pgc_fp_python)" || true
	[ -n "$py" ] || { _pgc_fp_warn_once; printf ''; return 0; }
	out="$("$py" "$(_pgc_fp_module)" fingerprint "${1:-.}" 2>/dev/null)"
	rc=$?
	[ "$rc" -eq 0 ] || { printf ''; return 0; }
	printf '%s\n' "$out"
}

# fresh   the binary was built from this source
# stale   it was not, and every check that follows would be about the wrong code
# unknown nobody in this batch recorded a fingerprint, so this cannot be answered
pgc_freshness_verdict() {	# pgc_freshness_verdict RECORDED CURRENT -> verdict
	local recorded="${1:-}" current="${2:-}"
	[ -z "$recorded" ] && { echo unknown; return; }
	[ -z "$current" ] && { echo unknown; return; }
	[ "$recorded" = "$current" ] && echo fresh || echo stale
}

# AND A cp IS NOT ENOUGH: THE POSTMASTER MAPS THE .so AT START.
#
# shared_preload_libraries='pgcolumnar' means the library is loaded once, when the
# postmaster starts. `make install` over a running instance changes the file and
# nothing else: every backend keeps executing the code it already mapped. So a
# binary can match the source exactly and the server can still be running something
# older, which the source check above cannot see.
#
# The harness normally escapes this because each suite initdb's and starts its own
# cluster after the install. It stops escaping it the moment a cluster outlives an
# install: a persistent cluster reused between runs, a bench rig left up, or a second
# batch installing into a prefix whose server is already serving.
#
# So compare when the binary was installed against when the server started. A
# postmaster older than the binary has the old code mapped, whatever the file says.
#
# fresh     the server started after the binary was installed
# predates  the binary is newer than the server, so the server has older code
# unknown   one of the two timestamps could not be read
# pgc_check_running_binary SO_PATH
#
# The runtime half of the freshness check: does the RUNNING server postdate the
# library on disk? Extracted from pgc_setup so it can be driven, because the
# verdict function alone could not be.
#
# WHY THAT MATTERS. Every suite initdb's a fresh cluster and starts it after the
# install, so through any shipped path the postmaster is always newer than the
# .so and `predates` is UNREACHABLE. selftest 340 fed the verdict function
# fixture values and proved its arithmetic; the CALL SITE could have been deleted
# with every check still passing (@jdatcmd, #898 review). That is this project's
# own rule about a helper a suite merely sources.
#
# Taking the path as an argument is what makes it reachable: the selftest points
# it at a file it has just touched, so the stat, the pg_postmaster_start_time()
# query, the verdict and the refusal all run for real and only the path is
# redirected. Nothing has to touch the installed library to prove the guard
# fires.
#
# Returns non-zero rather than calling exit, so a caller that is not a suite can
# turn it into its own kind of failure. pgc_setup passes the status through.
pgc_check_running_binary() {	# pgc_check_running_binary SO_PATH -> 0|1
	local _so_path="$1" _so_epoch _pm_epoch
	_so_epoch="$(stat -c %Y "$_so_path" 2>/dev/null || echo '')"
	_pm_epoch="$(psql_admin_scalar \
		"SELECT floor(extract(epoch from pg_postmaster_start_time()))::bigint;" \
		2>/dev/null | tr -dc '0-9')"
	case "$(pgc_running_binary_verdict "$_so_epoch" "$_pm_epoch")" in
		fresh)
			echo "-- server: started after the binary was installed"
			;;
		predates)
			echo "FATAL: this server was already running when the binary changed" >&2
			echo "       .so installed at epoch $_so_epoch, postmaster started $_pm_epoch" >&2
			echo "       shared_preload_libraries maps the library at start, so the" >&2
			echo "       backends are executing older code than the file on disk." >&2
			echo "       Restart the cluster; a reinstall alone does not reload it." >&2
			return 1
			;;
		unknown)
			echo "-- server: could not compare binary and postmaster timestamps"
			;;
	esac
	return 0
}

pgc_running_binary_verdict() {	# pgc_running_binary_verdict SO_EPOCH PM_EPOCH
	local so="${1:-}" pm="${2:-}"
	case "$so" in '' | *[!0-9]*) echo unknown; return ;; esac
	case "$pm" in '' | *[!0-9]*) echo unknown; return ;; esac
	[ "$pm" -ge "$so" ] && echo fresh || echo predates
}

# THE BINARY, NOT ONLY THE SOURCE (#959). `pgc_freshness_verdict` compares two
# SOURCE fingerprints, and the caller printed "source X matches the binary under
# test" -- a claim about the BINARY from evidence about the SOURCE. It is false
# whenever another process has written the shared prefix, and it is false in the
# POSITIVE branch, which is the only one that asserts anything.
#
# Measured: two trees whose src/ differs by five files. B built and installed
# through this function, A then installed its own library into the same prefix, and
# B ran #945's suite under PGC_SKIP_BUILD=1 -- ".so: d312a10c0cfb" beside "source:
# a0e6afc3e13e matches the binary under test", then nine failures with the code
# entirely innocent. @jdatcmd measured the same sentence above two different
# libraries on PG 17, 25+19 failed against 44+0.
#
# The stamp cannot see it because it is keyed per SOURCE TREE: two trees installing
# into one prefix have two stamp files, and each records only what its own tree
# built. So the evidence has to be the artifact itself.
pgc_installed_library_digest() {	# pgc_installed_library_digest PG_CONFIG -> 12 hex or empty
	local so
	so="$("${1:-}" --pkglibdir 2>/dev/null)/pgcolumnar.so"
	[ -r "$so" ] || { echo ""; return; }
	md5sum "$so" 2>/dev/null | cut -c1-12
}

pgc_binary_identity_verdict() {	# pgc_binary_identity_verdict RECORDED CURRENT -> verdict
	local recorded="${1:-}" current="${2:-}"
	[ -z "$recorded" ] && { echo unknown; return; }
	[ -z "$current" ] && { echo unknown; return; }
	[ "$recorded" = "$current" ] && echo fresh || echo replaced
}

# ONE DECISION, so no caller can claim the binary on source evidence alone. Pure,
# like its two siblings, because that is what let them be exercised without a build.
#
# A stale source outranks everything: the tree has moved, so nothing installed can
# be what it would now produce, and saying which of two reasons came first is less
# useful than refusing.
pgc_freshness_claim() {	# pgc_freshness_claim SOURCE_VERDICT BINARY_VERDICT -> decision
	case "${1:-}" in
		stale)   echo refuse-source; return ;;
		unknown) echo unverified;    return ;;
	esac
	case "${2:-}" in
		replaced) echo refuse-binary ;;
		fresh)    echo verified ;;
		*)        echo source-only ;;
	esac
}

pgc_write_source_stamp() {	# pgc_write_source_stamp FILE SOURCE_HASH [LIBRARY_DIGEST]
	# NO `|| true`. It was there, and it made both controllers' warning branches
	# UNREACHABLE: run_all_versions.sh and devloop.sh each wrap this in `if (...)`
	# and promise to say so when the stamp cannot be written, and each carries a
	# comment saying "NOT || true" -- while the function they call swallowed the
	# status (@linuxhikerpm, #898 review). Driven against an unwritable target:
	#
	#     write_rc=0 exists=no
	#
	# The stamp absent, nothing warned, every child suite degraded to UNVERIFIED.
	# A comment that argues for a guarantee the code does not provide is worse
	# than no comment, because it stops the next person checking.
	# Two lines when a digest is known, one when it is not. A one-line stamp stays
	# valid and reads as "source recorded, library unrecorded", which is what every
	# stamp written before #959 is -- so the migration needs no special case.
	if [ -n "${3:-}" ]; then
		printf '%s\n%s\n' "${2:-}" "$3" > "${1:-/dev/null}" 2>/dev/null
	else
		printf '%s\n' "${2:-}" > "${1:-/dev/null}" 2>/dev/null
	fi
}

pgc_read_installed_stamp() {	# pgc_read_installed_stamp FILE -> digest or empty
	# THE SECOND LINE ONLY. Reading hex from the whole file would return the SOURCE
	# fingerprint for every pre-#959 stamp, certifying a source hash as a library
	# digest -- the exact confusion this change exists to remove.
	[ -r "${1:-}" ] || { echo ""; return; }
	sed -n '2p' "$1" | tr -dc 'a-f0-9' | head -c 12
}

pgc_read_source_stamp() {	# pgc_read_source_stamp FILE -> hash or empty
	# THE FIRST LINE ONLY, and this is the mirror of the trap in the reader above
	# (#959 review, @jdatcmd). Stripping hex from the WHOLE FILE was right while a
	# stamp was one line and became wrong the moment this change added a second:
	#
	#     source unfingerprintable + a digest recorded
	#       file                "" + "d312a10c0cfb"
	#       whole-file read     d312a10c0cfb      <- the LIBRARY digest, as the source
	#       verdict             stale             <- was `unknown` before this change
	#       decision            refuse-source     <- a FATAL naming a library digest
	#                                                as a source fingerprint
	#
	# Before, that case wrote a one-line empty stamp, read empty, and reported
	# UNVERIFIED -- which is the documented behaviour for a tree that cannot be
	# fingerprinted. So the regression was introduced by the second line, not found
	# lying in wait, and it belongs in this change rather than a follow-up.
	#
	# A short first line would also have spliced across the newline: "abc" plus
	# "d312a10c0cfb" read as "abcd312a10c0".
	[ -r "${1:-}" ] || { echo ""; return; }
	sed -n '1p' "$1" | tr -dc 'a-f0-9' | head -c 12
}

# The stamp lives beside the tree that built the binary, keyed by major, because one
# tree installs into several prefixes and each has its own binary.
# pgc_major_of PG_CONFIG -> major version number
#
# One definition, because devloop.sh now writes a stamp whose PATH is keyed on
# the major and pgc_setup reads it back. Two copies of this sed would be two
# answers to "which major", and the failure would be a stamp written where
# nothing looks for it -- a silent UNVERIFIED rather than an error.
pgc_major_of() {	# pgc_major_of PG_CONFIG -> major
	"$1" --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/'
}

pgc_source_stamp_path() {	# pgc_source_stamp_path DIR PG_CONFIG
	# KEYED BY THE INSTALLATION, NOT ONLY THE MAJOR. The key was
	# `.pgc_source_stamp.<major>`, and the comment above it already said that one
	# tree installs into several prefixes each with its own binary -- so the key
	# discarded the distinction the comment drew (@linuxhikerpm, #898 review).
	#
	# Not hypothetical on this box: pg18a, pg18n and pg18_san are three PG18
	# installations with different pkglibdirs, and all three resolved to
	# `.pgc_source_stamp.18`. Build current source into one prefix, then run
	# PGC_SKIP_BUILD=1 against another, and the fingerprint matches while the
	# binary is stale -- and the postmaster arm passes too, because the freshly
	# started server is newer than the old .so. The run then reports fresh while
	# executing the other prefix's binary, which is this file's whole subject.
	#
	# pkglibdir rather than the pg_config path, because that is where the .so
	# actually lands: two pg_configs pointing at one prefix ARE the same
	# installation and should share a stamp.
	local _pgc_sp_dir="${1:-.}" _pgc_sp_cfg="${2:-}"
	local _pgc_sp_major _pgc_sp_lib _pgc_sp_id
	_pgc_sp_major="$(pgc_major_of "$_pgc_sp_cfg" 2>/dev/null)"
	_pgc_sp_lib="$("$_pgc_sp_cfg" --pkglibdir 2>/dev/null)"
	# An unreadable pg_config gets a key that matches nothing rather than one
	# every broken config shares: `unknown` would alias them together, which is
	# the defect being fixed, one level down.
	if [ -n "$_pgc_sp_lib" ]; then
		_pgc_sp_id="$(printf '%s' "$_pgc_sp_lib" | md5sum | cut -c1-8)"
	else
		_pgc_sp_id="nolib$(printf '%s' "$_pgc_sp_cfg" | md5sum | cut -c1-3)"
	fi
	printf '%s/.pgc_source_stamp.%s.%s\n' \
		"$_pgc_sp_dir" "${_pgc_sp_major:-0}" "$_pgc_sp_id"
}

pgc_write_build_stamp() {
	printf '%s\n' "${2:-}" > "${1:-/dev/null}" 2>/dev/null || true
}

# An absent stamp is not "built for PG?" -- that asserts a provenance the code
# never recorded, which is the defect #537 was filed about.
pgc_build_stale_message() {
	if [ -z "${1:-}" ]; then
		printf -- '-- the tree holds objects with no recorded major and this run wants PG%s; cleaning first (#536)\n' "${2:-?}"
	else
		printf -- '-- the tree was last built for PG%s and this run wants PG%s; cleaning first (#536)\n' "$1" "${2:-?}"
	fi
}

# ---- SQL helpers (run as root over TCP, trust auth) ------------------------

PGC_PSQL_BASE() {
	echo "psql -h 127.0.0.1 -p $PGC_PORT -U postgres"
}

# Run a statement against the test database, stop on error.
psql_run() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -q -c "$1"
}

# Run a statement against the maintenance database (for CREATE DATABASE etc.).
psql_admin() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d postgres -v ON_ERROR_STOP=1 -q -c "$1"
}

# Scalar form of psql_admin. Separate because psql_admin is used for statements
# and deliberately keeps psql's ordinary output; -At here means a caller reading
# a single value gets the value and not a bordered table with "(1 row)" under it,
# which is the mistake that made the cluster setup retry ten times.
psql_admin_scalar() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d postgres -At -c "$1" 2>/dev/null
}

# Scalar query: echo a single value (empty on error).
q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>/dev/null || true
}

# Run a SQL file, returning At output.
psql_file() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -f "$1" 2>/dev/null || true
}

# ---- the rebuild symbol check's exemption, exposed to be judged -------------
#
# A name the TOOLCHAIN leaves undefined by design, never a PostgreSQL symbol.
# rebuild.sh's check (#382) exists to catch a .so linked against another major,
# which shows up as an unresolved PostgreSQL symbol; these are the names that
# are undefined for reasons that have nothing to do with the major.
#
# __stack_chk_guard IS ARCHITECTURE-SPECIFIC, WHICH IS WHY IT COST FOUR NIGHTS
# (#1248). x86_64 reads the stack canary from %fs:0x28 and needs no symbol at
# all; aarch64 references a global DATA object. Measured on x86_64: our .so
# leaves `__stack_chk_fail` undefined and libc exports it (so the check passes),
# while `__stack_chk_guard` is exported by neither libc nor the loader and is
# not referenced at all. So the aarch64 build was never broken -- `build: OK (0
# warnings)`, install OK, stamp recorded -- and the check was reporting a
# toolchain symbol as evidence of a mislinked major.
#
# EXPOSED AS A FUNCTION BECAUSE THE SYMBOL CANNOT BE REACHED ON x86_64. A fix
# verified on this architecture proves nothing: the name never appears, so a
# no-op looks identical to a repair. Part 580 drives this with literals instead,
# which runs the same on every architecture -- the same reason `_hr_dependents`
# and `_hr_diagnose` are functions rather than inline.
# WHAT EACH ENTRY IS FOR, measured on x86_64 by driving it against the real .so
# and the real reference set rather than read off a comment:
#
#     _ITM_deregisterTMCloneTable   undefined in our .so, resolvable 0  -> needed
#     __gmon_start__                undefined in our .so, resolvable 0  -> needed
#     __cxa_finalize                undefined in our .so, resolvable 1  -> redundant HERE
#     __stack_chk_guard             not referenced on this arch at all
#
# __cxa_finalize IS KEPT DESPITE RESOLVING HERE. libc defines it and libc is in
# the reference set, so on this toolchain the exemption buys nothing -- but
# "redundant on x86_64" is not "redundant everywhere", and dropping it would be
# the same architecture-blind edit this whole issue is about.
#
# THE OLD COMMENT SAID __cxa_finalize WAS "supplied by the loader" AND THAT IS
# FALSE. Measured: the loader defines it 0 times, libc defines it once. That
# sentence was read, believed, and used as the premise of a published
# conclusion before anyone tested it -- a wrong comment is an input to the next
# reader's reasoning, and nothing executes it, so no test was ever going to
# catch it. Named by @jdatcmd, who was the reader it misled.
pgc_symbol_is_toolchain() {	# pgc_symbol_is_toolchain NAME -> yes|no
	case "${1%%@*}" in
		_ITM_* | __gmon_start__ | __cxa_finalize | __stack_chk_guard)
			echo yes ;;
		*)
			echo no ;;
	esac
}

# ---- assertions ------------------------------------------------------------


# Record an outcome for a check a SUITE-LOCAL helper counted.
#
# PGC_CHECKS is an invariant that only this file can keep, because pgc_summary
# reconciles PASSED + FAILED + UNRUN against it. A suite that bumps PGC_CHECKS
# itself and prints its own PASS or FAIL leaves the totals short, and the
# reconciliation then reds a healthy tree -- which is a worse defect than the
# miscount it exists to find. That is not hypothetical: projections.sh has an
# expect_fail() with ten call sites that has been counting checks whose outcome
# nothing recorded for as long as it has existed, and nothing could tell.
#
# A suite-local helper calls these instead of touching the counters. They are the
# only supported way to add a check from outside this file, and selftest part 320
# sweeps for direct PGC_CHECKS writes so the next expect_fail is caught when it
# is written rather than when it reddens something.
# ---- one place that counts a check, and it is the same place that records it -
#
# lib.sh had ELEVEN sites bumping PGC_CHECKS, each with its own outcome line
# beside it. That is eleven chances to add a twelfth and forget the line, which
# is exactly what projections.sh's expect_fail did with ten call sites for as
# long as it existed.
#
# Counting and recording are therefore ONE operation. A helper cannot report an
# outcome without being counted, and cannot be counted without reporting one,
# because there is no code path that does either alone. `checks run: N` and the
# N record lines are the same increment seen twice.
#
# DISPLAY is passed whole rather than composed here, so every existing human line
# stays byte-identical: suites, selftests and CI all grep `^PASS` and `^FAIL`,
# and 3,762 call sites is far past what a careful refactor can be trusted on.
#
# The record is tab separated, five columns after the RESULT marker:
#
#     RESULT <TAB> suite <TAB> part <TAB> name <TAB> verdict <TAB> reason
#
# so a check name containing spaces survives. The reason carries the REASON_CODE
# #915 introduced, which is what makes this more than a reformat: an unrunnable
# check is distinguishable from a passing one without parsing prose. The verdict
# is one of PASS, FAIL, UNRUN or SKIP.
#
# There is no mutation column here. That one belongs to the LEDGER (#918), which
# keys on (suite, part, name) and records which mutation reddened a check; a
# record is one observation, not a history.
pgc_record() {	# pgc_record VERDICT NAME DISPLAY [REASON]
	local _v="$1" _name="$2" _display="$3" _reason="${4:-}"
	PGC_CHECKS=$((PGC_CHECKS + 1))
	case "$_v" in
		PASS)  PGC_PASSED=$((PGC_PASSED + 1)) ;;
		FAIL)  PGC_FAILED=$((PGC_FAILED + 1)); PGC_FAIL=1 ;;
		UNRUN) PGC_UNRUN=$((PGC_UNRUN + 1)) ;;
		SKIP)  PGC_SKIPPED=$((PGC_SKIPPED + 1)) ;;
		*)
			# An unknown verdict is a failure of the harness, not a check to
			# drop. Dropping it would leave PGC_CHECKS bumped with no outcome
			# recorded, which is the reconciliation failure pgc_summary refuses.
			PGC_FAILED=$((PGC_FAILED + 1)); PGC_FAIL=1
			_display="FAIL  $_name: pgc_record was given the verdict [$_v], which is not PASS, FAIL, UNRUN or SKIP"
			_v=FAIL
			;;
	esac
	# WHICH PART asked this question, derived from the call stack.
	#
	# The suite is not enough. harness_selftest sources 40-odd parts into one
	# shell, and its premises are phrased to be COPIED -- "premise: the pytest
	# layer is where THIS PART thinks it is" says "this part" precisely so the
	# same sentence works in any of them. main carries two copies of that one and
	# two of another, and the number grows with every part anyone adds.
	#
	# So a key of (suite, name) is not a key of checks, it is a key of check
	# NAMES, and they differ by however many parts share a boilerplate premise.
	# One of them going red would then mark every sharer as observed red -- a
	# claim about a check nothing attacked. Found by OffgridwithJD, who noticed
	# that all six of their own branches added more.
	#
	# BASH_SOURCE, not a convention change, so the next part written the same way
	# is keyed correctly without anyone remembering. Parameter expansion only: no
	# basename fork, at 3,762 call sites.
	local _part="" _bs
	for _bs in "${BASH_SOURCE[@]}"; do
		case "$_bs" in */lib.sh|lib.sh) continue ;; esac
		_part="${_bs##*/}"; _part="${_part%.sh}"
		break
	done

	printf '%s\n' "$_display"
	# Tabs in a field would split it, and a NEWLINE splits the whole record just
	# as completely -- it ends the line, so what follows becomes a second line the
	# reader cannot key. Nothing in the tree puts either in a check name, and this
	# makes that true rather than assumed. The tab was stripped here from the
	# first version; the newline was not, which @linuxhikerpm named on #917: the
	# reason already written for the tab is the reason for both. Measured before
	# the fix, a newline in the name gave a record of 4 fields plus two stray
	# lines; after it, 6 fields and one line.
	#
	# PARAMETER EXPANSION, not `printf | tr` in a command substitution. The first
	# version paid four forks per record -- two subshells and two tr processes --
	# in the function that runs at every one of 3,762 check sites, and whose own
	# comment hoists PGC_SUITE out of the body on exactly that ground. Measured on
	# an idle box, 2,000 calls, identical output on every input including a real
	# tab: 3.1577 ms per call against 0.0096 ms, 331x, or 11.9 seconds of pure
	# fork overhead across a full suite against 36 ms. Reported by OffgridwithJD.
	local _nl_name="${_name//$'\t'/ }" _nl_reason="${_reason//$'\t'/ }"
	_nl_name="${_nl_name//$'\n'/ }"; _nl_reason="${_nl_reason//$'\n'/ }"
	_nl_name="${_nl_name//$'\r'/ }"; _nl_reason="${_nl_reason//$'\r'/ }"
	# THE MAJOR THIS CHECK WAS OBSERVED UNDER (#1010). A check's EXISTENCE depends
	# on it -- analyze_differential.sh emits one record on PG15-17 and N on PG18+,
	# and fk_referencing.sh's two branches emit different check NAMES -- so a
	# record that does not name its major identifies a check only partly, and the
	# ledger keyed on it has to take the major from whoever invoked the tool. That
	# is the `--date not-a-date` failure one field over: a PG15 log merged as PG18
	# is misattributed and nothing in the log can contradict it.
	#
	# `unknown` is this function's OWN word for a field the harness did not set,
	# already used two lines down for an unset suite and part, and it is a REAL case
	# rather than a courtesy: PGC_MAJOR is set in pgc_setup, and 14 suites need no
	# cluster so never call it. Measured on a full pg18 matrix, 544 of 6753 records
	# carry it -- audit, concurrency, decode_interrupts, hilbert_curve,
	# objstore_stash_recovery, phase2-6, smoke, unique_conc, update_conc, wal_envelope.
	#
	# It is therefore ORDER-DEPENDENT inside a suite that sources parts into one shell:
	# a record emitted before the first pgc_setup in that shell says `unknown` and one
	# after it names the major. harness_selftest is that shape -- 10 of its 46 parts
	# call pgc_setup -- and on pg18 all 916 of its records named the major, so the
	# first setup precedes the first record today. A part added ahead of it would not.
	printf 'RESULT\t%s\t%s\t%s\t%s\t%s\t%s\n' \
		"${PGC_SUITE:-unknown}" \
		"${_part:-${PGC_SUITE:-unknown}}" \
		"${_nl_name}" \
		"$_v" \
		"${PGC_MAJOR:-unknown}" \
		"${_nl_reason}"
}

pgc_pass() {	# pgc_pass NAME
	pgc_record PASS "$1" "PASS  $1"
}

pgc_fail() {	# pgc_fail NAME DETAIL
	if [ -n "${2:-}" ]; then
		pgc_record FAIL "$1" "FAIL  $1: $2"
	else
		pgc_record FAIL "$1" "FAIL  $1"
	fi
}

# A check that could not be evaluated is a third state, not a pass.
#
# When a check's INPUT is absent -- a fixture that did not build, a capability the
# server lacks, an endpoint that is not reachable -- the check either passes
# vacuously or fails for a reason unrelated to the property under test. Neither
# answer is true, and "checks run: N" counts it either way, so a reader counting
# greens counts one that never asked its question.
#
# pgc_skip already refuses to let a MISSING DEPENDENCY read as a pass at suite
# granularity. This is the same honesty for one check.
#
# The suite exits PGC_EXIT_INCOMPLETE, so an unrunnable check cannot hide inside a
# suite that reports PASSED. A failure still outranks it: a suite with both is
# FAILED, because the failure is the more urgent fact.
check_unrunnable() {	# check_unrunnable NAME REASON_CODE DETAIL
	local name="$1" reason="${2:-}" detail="${3:-}"
	case " $PGC_UNRUN_REASONS " in
		*" $reason "*) ;;
		*)
			pgc_record FAIL "$name" \
				"FAIL  $name: unrunnable reason [$reason] is not one of: $PGC_UNRUN_REASONS"
			return
			;;
	esac
	pgc_record UNRUN "$name" "UNRUN  $name: $reason: $detail" "$reason"
}

check() {
	local name="$1" got="$2" want="$3"
	if [ "$got" = "$want" ]; then
		pgc_record PASS "$name" "PASS  $name"
	else
		pgc_record FAIL "$name" "FAIL  $name: got [$got] want [$want]"
	fi
}

# ---- assertions that refuse to compare a measurement nobody took ------------
#
# check "$label" "$a" "$b" with both sides empty compares "" with "" and prints
# PASS (#418). Every way a measurement goes missing produces exactly that: a
# tool that is not installed, a grep that matched no line, a psql against a
# cluster that is down, a substitution that expanded to nothing. The suite then
# reports success for a number nobody has.
#
# This is not hypothetical and it is not rare. column_projection.sh piped its
# buffer count through bc, bc was absent on one machine, and both sides came
# back empty. It failed there only because one side happened to be non-empty.
# Two green checks were produced the same day that measured nothing at all.
#
# So: a measurement must look like a number before it is compared, and the
# failure says which side was not one. "got [] want []" is the message that cost
# the time.

# Integer or decimal, optional leading sign. Deliberately strict: an empty
# string, a psql error message, and "no" are all not numbers.
pgc_is_number() {	# $1 -> 0 when $1 is a number
	local v="${1#-}"
	v="${v#+}"
	case "$v" in
		'' | . | *[!0-9.]* | *.*.*) return 1 ;;
	esac
	return 0
}

# check_text LABEL GOT WANT -- check, with both sides required to be non-empty.
#
# check_num covers a measurement that is a NUMBER. Plenty of oracles are not: an
# md5 over an ordered result, a plan node name, a returned string. check_num
# rejects those outright -- it refuses two identical md5 hashes, because an md5
# is not a number -- so a suite comparing one has nothing to reach for and falls
# back to plain check, where "" equals "" and prints PASS (#418).
#
# That is the same defect, on the larger half: 35 places in this tree compare an
# md5(string_agg(...)) oracle, and every one of them is a down cluster or an
# errored query away from comparing nothing with nothing.
#
# Deliberately weaker than check_num: it asserts presence, not shape. A caller
# that knows the shape should say so, and native_index_projection.sh's agree()
# additionally requires 32 hex characters before it trusts either side.
check_text() {
	local name="$1" got="$2" want="$3"
	if [ -z "$got" ] || [ -z "$want" ]; then
		pgc_record FAIL "$name" \
			"FAIL  $name: a side is empty, so nothing was compared: got [$got] want [$want]"
		return 1
	fi
	check "$name" "$got" "$want"
}

# check_num LABEL GOT WANT -- check, with both sides required to be numbers.
check_num() {
	local name="$1" got="$2" want="$3"
	if ! pgc_is_number "$got" || ! pgc_is_number "$want"; then
		pgc_record FAIL "$name" \
			"FAIL  $name: not a measurement, so nothing was compared: got [$got] want [$want]"
		return 1
	fi
	check "$name" "$got" "$want"
}

# check_ratio LABEL A B MAX -- assert A divided by B is at most MAX.
#
# awk rather than bc, on purpose. bc is not part of a base install and its
# absence is what produced the empty measurement in the first place; awk is
# required by POSIX and is present wherever these suites can run at all.
#
# Zero on EITHER side is refused, not only the denominator.
#
# The first version of this checked only the denominator while this comment
# claimed both. The same commit changed column_projection.sh's bufs() to sum with
# awk, which returns 0 where it used to return the empty string, and that
# converted a failure mode this helper rejects into one it accepted: a projected
# read touching no buffers gives a ratio of 0.00, inside any bound, and passes.
# #418 moved rather than closed, inside the very check that started it.
#
# A zero numerator is "the thing we measured cost nothing", which is nearly
# always "the thing we measured did not happen". A call site that genuinely needs
# to permit zero should say so under its own name rather than get it by default.
# Runs everywhere, including CI. For a ratio whose arms move together under
# load -- measured back to back in the same run, compared by minimum. If the
# ratio needs an idle machine to mean anything, use
# check_ratio_needs_quiet_machine instead and read why there.
check_ratio() {	# $1 label, $2 a, $3 b, $4 max
	local name="$1" a="$2" b="$3" max="$4" ratio

	if ! pgc_is_number "$a" || ! pgc_is_number "$b" || ! pgc_is_number "$max"; then
		pgc_record FAIL "$name" \
			"FAIL  $name: not a measurement, so no ratio was formed: a=[$a] b=[$b] max=[$max]"
		return 1
	fi
	if [ "$(awk -v x="$a" -v y="$b" 'BEGIN { print (x + 0 == 0 || y + 0 == 0) ? "yes" : "no" }')" = yes ]; then
		pgc_record FAIL "$name" \
			"FAIL  $name: a side of the ratio is zero, so nothing was measured: a=[$a] b=[$b]"
		return 1
	fi
	ratio="$(awk -v a="$a" -v b="$b" 'BEGIN { printf "%.2f", a / b }')"
	if [ "$(awk -v r="$ratio" -v m="$max" 'BEGIN { print (r <= m) ? "yes" : "no" }')" = yes ]; then
		pgc_record PASS "$name" "PASS  $name (${ratio}x, bound ${max}x, from a=$a b=$b)"
	else
		pgc_record FAIL "$name" "FAIL  $name: ${ratio}x exceeds the ${max}x bound (a=$a b=$b)"
	fi
}

# pgc_require_tools TOOL... -- one clear line at the top, rather than an empty
# string three checks later. A suite that needs a tool it does not have has not
# been skipped; it has been silently narrowed.
pgc_require_tools() {
	local t missing=""
	for t in "$@"; do
		command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
	done
	if [ -n "$missing" ]; then
		pgc_record FAIL "the tools this suite measures with are missing" \
			"FAIL  the tools this suite measures with are missing:$missing"
		return 1
	fi
	return 0
}

# A check whose subject is a wall-clock ratio.
#
# PGC_SKIP_TIMING exists because a shared runner cannot hold a ratio still, and
# a gate that reds for reasons unrelated to the change teaches its readers to
# discount red. Until now it was applied a whole suite at a time, which is too
# blunt: native_cancel and native_fetch_cache each hold one ratio and several
# correctness checks, so dropping the suite dropped the correctness with it
# (#254). native_fetch_cache was not dropped at all, and flaked CI instead:
#
#     FAIL  fetching from one big group is not far dearer than from ten small
#           ones: got [no (one=397ms ten=91ms)]
#
# So the ratio is skipped and the rest of the suite runs. A skip is announced
# rather than silent, and it is not counted as a pass, because a count that
# includes checks nobody ran is the thing this project keeps having to unlearn.
# A named check that could not run HERE, for a reason the suite knows.
#
# WHY THIS EXISTS. `echo "SKIP  ..."` printed a line a reader sees and left
# PGC_CHECKS alone, so the outcome existed for a human and for nobody else: no
# record, no count, and nothing for `pgc_reconcile_records` to reconcile. The
# tree's own comment at native_index_projection.sh said why that mattered -- "the
# skip must be visible: a check that reports nothing is indistinguishable from a
# check that passes" -- and that was TRUE while the human line WAS the record. It
# stopped being true when the RESULT stream became the machine-readable one, and
# 22 sites were left behind on the wrong side of the change. Named by
# @linuxhikerpm on #917; the owner asked for every site, not the three examples.
#
# DISPLAY IS PASSED WHOLE, exactly as pgc_record takes it, so every existing
# human line stays byte-identical. These messages are individually worded and a
# reader greps them; recomposing them here would change what people see for no
# gain, which is the same reason pgc_record does not compose PASS lines either.
check_skip() {	# check_skip NAME DISPLAY [REASON]
	pgc_record SKIP "$1" "$2" "${3:-}"
}

check_timing() {
	local name="$1" got="$2" want="$3"

	if [ "${PGC_SKIP_TIMING:-0}" = 1 ]; then
		# Counted and recorded, like every other outcome. It printed a line a
		# reader sees; leaving PGC_CHECKS at zero made that outcome invisible to
		# the count and to the records both (#917, found by @linuxhikerpm).
		pgc_record SKIP "$name" \
			"SKIP  $name (PGC_SKIP_TIMING: wall-clock measurement)" \
			"PGC_SKIP_TIMING"
		return 0
	fi
	check "$name" "$got" "$want"
}

# A ratio check whose subject is a wall-clock ratio.
#
# check_timing does this for a scalar; a ratio needs its own entry point because
# check_ratio takes a bound as well as two sides.
#
# It exists so that a suite never has to read PGC_SKIP_TIMING to decide whether
# to ASSERT. planner_choice_quality did read it, branched on it, and then called
# check_timing with two empty strings for got and want -- the "" vs "" compare
# check_text and check_num were added to forbid (#418). That was safe only while
# the suite's copy of the condition agreed with this file's, which is exactly the
# coupling this helper removes. Deciding whether to MEASURE is still the suite's
# business; deciding whether to assert is this file's.
# A wall-clock ratio that is only meaningful on an unloaded machine.
#
# THIS HELPER REMOVES THE CHECK FROM EVERY AUTOMATED GATE. Both .github/workflows/
# ci.yml and .github/workflows/nightly.yml set PGC_SKIP_TIMING, so a check written
# with it runs only when someone runs the suite by hand. That is correct for a
# ratio a shared runner can distort, and it is the whole cost of using it.
#
# USE IT when the ratio compares against an absolute or cross-run baseline, where
# a loaded machine can move one side and not the other.
#
# DO NOT USE IT when the two arms are measured back to back in the same run and
# compared by minimum. Both readings then move together under load, which is what
# makes that shape safe on shared hardware -- use check_ratio, which runs
# everywhere. test/cancel_decode.sh carries the worked argument for its own ratio
# and test/int8_agg_int128.sh is a second instance.
#
# The name says what the helper DOES rather than what it measures. It was
# check_ratio_timing, which read as "the helper for ratios of timings" -- so an
# author holding the safe shape picked it by matching units and lost the check in
# CI silently. Three suites had each derived the exemption for themselves in their
# own headers (bloom_sizing a size, native_fetch_bigcap buffers, cancel_decode a
# same-run ratio) and nothing said it here, where it is decided (#787).
check_ratio_needs_quiet_machine() {  # <name> <a> <b> <bound>
	if [ "${PGC_SKIP_TIMING:-0}" = 1 ]; then
		pgc_record SKIP "$1" \
			"SKIP  $1 (PGC_SKIP_TIMING: wall-clock ratio)" \
			"PGC_SKIP_TIMING"
		return 0
	fi
	check_ratio "$@"
}

# Is this query's plan the columnar custom scan?
#
# For a check that reads a counter out of EXPLAIN and asserts on it. Those
# counters exist only on this node, so if the planner stops choosing it the read
# returns nothing and the check fails describing skipping, or bloom, or
# clustering -- anything except the plan change that actually happened.
#
# "Columnar Projected Columns" is the node's own marker: no other node reports
# it, and the vectorized aggregate node reports "Columnar Vectorized Aggregates"
# instead. A positive grep for the marker is deliberately not an absence test --
# a plan that fell back to a sequential scan has no Columnar lines to be absent,
# so an absence test would pass for exactly the case worth catching.
#
# EXPLAIN without ANALYZE is enough: the line comes from the plan rather than the
# run, so the assertion costs a plan and does not execute the query.
pgc_is_columnar_scan() {	# query -> yes|no
	# grep -c, NOT grep -q. grep -q exits the moment it matches, psql is still
	# writing, and under a suite's `set -o pipefail` the pipeline reports failure
	# though the pattern WAS present -- so this helper answers "no" for a plan that
	# contains the line. Latent rather than live at EXPLAIN size (0 wrong in 200
	# trials at 1.9KB) but with no floor in the mechanism: measured 1/200 wrong at
	# 8.9KB and 40/40 at 289KB, on a loaded machine. grep -c reads to EOF.
	local _plan
	_plan="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (COSTS OFF) $1" 2>/dev/null)"
	[ "$(grep -c 'Columnar Projected Columns' <<<"$_plan" || true)" -ne 0 ] \
		&& echo yes || echo no
}

# Does this query's plan drive the per-row fetch path?
#
# The fetch cache and its cap live in pgcolumnar_fetch_row(), which is reached
# only from the index fetch. A cost guard that means to measure the cache must
# assert this, because enable_seqscan=off and enable_bitmapscan=off do NOT
# disable the columnar custom scan: the planner is free to answer the same query
# with a group scan that never fetches a row, and the guard then times a
# different mechanism and stays green forever (#797).
#
# The setup argument carries the SET statements the guard runs under, because
# the plan depends on them -- pgcolumnar.enable_index_fetch_penalty in
# particular decides this very choice. Passing them separately keeps them out of
# the EXPLAIN target.
#
# A positive grep for "Index Scan using" rather than an absence test, for the
# same reason as pgc_is_columnar_scan above: a plan that fell back has no line
# to be absent, so an absence test passes for exactly the case worth catching.
#
# This asks about a SELECT that reaches the fetch path THROUGH AN INDEX SCAN. It
# is the wrong premise for a DML guard: an UPDATE reaches pgcolumnar_fetch_row()
# for every row it modifies while still planning as a custom scan, so this would
# report "no" for a query that does exercise the cache. Measured: an UPDATE over
# 2,000 rows made 20,366 fetch_row calls under a Custom Scan plan (#797).
pgc_uses_row_fetch() {	# setup query -> yes|no
	# grep -c, not grep -q; see pgc_is_columnar_scan above for the measurement.
	local _plan
	_plan="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" -c "EXPLAIN (COSTS OFF) $2" 2>/dev/null)"
	[ "$(grep -c 'Index Scan using' <<<"$_plan" || true)" -ne 0 ] \
		&& echo yes || echo no
}

# Order-independent set hash of an arbitrary query's result. The row is cast to
# text as a whole composite so any column list works. No single quotes are used
# in the wrapper (dollar-quoting + chr(10)) so the inner query may contain its
# own quotes freely.
pgc_set_hash() {
	local query="$1" out res
	out="$PGC_SQLDIR/h.$$.$RANDOM.sql"
	cat > "$out" <<SQL
SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), \$e\$EMPTY\$e\$)
FROM (SELECT _row::text AS t FROM ( $query ) _row) _s;
SQL
	res="$(psql_file "$out")"
	rm -f "$out"
	# A blank result means the query errored or the server is gone; a genuinely
	# empty result set hashes to EMPTY. Return a value unique to this call, so
	# two failing queries can never compare equal and pass the check vacuously.
	# The counter lives in a file because each call runs in its own subshell, and
	# $$/$RANDOM are not reliably distinct between siblings on every bash.
	if [ -z "$res" ]; then
		local seq
		seq=$(( $(cat "$PGC_WORKDIR/.query_error_seq" 2>/dev/null || echo 0) + 1 ))
		echo "$seq" > "$PGC_WORKDIR/.query_error_seq"
		res="QUERY_ERROR.$seq"
	fi
	printf '%s\n' "$res"
}

# pgc_seq_hash QUERY -> md5 over the rendered rows IN THE ORDER THE QUERY
# RETURNED THEM, or EMPTY for a genuinely empty result set.
#
# The sibling pgc_set_hash sorts the rendered rows before hashing them
# (string_agg(... ORDER BY t)). That is what makes it a SET comparison, and it is
# right for the ~150 diff_query sites that do not name an order. It is wrong for
# the ones that do: an ORDER BY the oracle cannot fail on is not an assertion.
# Measured on the oracle itself -- five rows forward and the same five reversed
# both hash to 2603e60e802d02d5370794d279cb522a, while a genuinely different row
# set does hash differently, so the set oracle is order-blind rather than broken.
#
# row_number() OVER () numbers the rows as they arrive from the subquery and
# string_agg then orders on that number, so what is hashed is the query's own
# output order. The sentinels are pgc_set_hash's, unchanged: a genuinely empty
# result hashes to EMPTY, and a query that errored returns a unique
# QUERY_ERROR.$seq so two failing queries can never compare equal and pass
# vacuously (#418).
pgc_seq_hash() {
	local query="$1" out res
	out="$PGC_SQLDIR/q.$$.$RANDOM.sql"
	cat > "$out" <<SQL
SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY n)), \$e\$EMPTY\$e\$)
FROM (SELECT row_number() OVER () AS n, _row::text AS t
      FROM ( $query ) _row) _s;
SQL
	res="$(psql_file "$out")"
	rm -f "$out"
	if [ -z "$res" ]; then
		local seq
		seq=$(( $(cat "$PGC_WORKDIR/.query_error_seq" 2>/dev/null || echo 0) + 1 ))
		echo "$seq" > "$PGC_WORKDIR/.query_error_seq"
		res="QUERY_ERROR.$seq"
	fi
	printf '%s\n' "$res"
}

# diff_query LABEL "QUERY with %T placeholder for the table name"
# Runs QUERY against t_heap and t_col and asserts identical result sets.
diff_query() {
	local label="$1" tmpl="$2"
	local hq hc
	hq="$(pgc_set_hash "${tmpl//%T/t_heap}")"
	hc="$(pgc_set_hash "${tmpl//%T/t_col}")"
	# A blank result means the query errored; surface it as a distinct value.
	[ -z "$hq" ] && hq="HEAP_ERROR"
	[ -z "$hc" ] && hc="COL_ERROR"
	check "$label" "$hc" "$hq"
}

# diff_query_ordered LABEL "QUERY with %T placeholder for the table name"
# As diff_query, but the row ORDER is part of the assertion. Use this for any
# query that names an ORDER BY, and diff_query for everything else. Keeping the
# two apart is deliberate: most comparisons here want set semantics, and only a
# query that asks for an order can be wrong about one. test/selftest enforces the
# split so a new ordered site cannot quietly land on the order-blind oracle.
diff_query_ordered() {
	local label="$1" tmpl="$2"
	local hq hc
	hq="$(pgc_seq_hash "${tmpl//%T/t_heap}")"
	hc="$(pgc_seq_hash "${tmpl//%T/t_col}")"
	# A blank result means the query errored; surface it as a distinct value.
	[ -z "$hq" ] && hq="HEAP_ERROR"
	[ -z "$hc" ] && hc="COL_ERROR"
	check "$label" "$hc" "$hq"
}
# pgc_check_ordered_oracle
# The premise behind every diff_query_ordered call: the ordered oracle must be
# able to fail on row order, and the set oracle must deliberately not be. Run it
# once in any suite that uses diff_query_ordered. Without it those assertions
# could pass by construction, which is the failure this whole split is about --
# an assertion that cannot fail for the reason it names is not an assertion.
# Static checks cannot see this; it runs against the cluster under test.
pgc_check_ordered_oracle() {
	local fwd rev sfwd srev again
	fwd="$(pgc_seq_hash 'SELECT g FROM generate_series(1,5) g ORDER BY g')"
	rev="$(pgc_seq_hash 'SELECT g FROM generate_series(1,5) g ORDER BY g DESC')"
	again="$(pgc_seq_hash 'SELECT g FROM generate_series(1,5) g ORDER BY g')"
	sfwd="$(pgc_set_hash 'SELECT g FROM generate_series(1,5) g ORDER BY g')"
	srev="$(pgc_set_hash 'SELECT g FROM generate_series(1,5) g ORDER BY g DESC')"
	check "premise: the ordered oracle is order-sensitive" \
		"$([ "$fwd" != "$rev" ] && echo yes || echo no)" "yes"
	check "premise: the ordered oracle agrees with itself" \
		"$([ "$fwd" = "$again" ] && echo yes || echo no)" "yes"
	check "control: the set oracle is order-blind by design" \
		"$([ "$sfwd" = "$srev" ] && echo yes || echo no)" "yes"
}
# ---- pair construction -----------------------------------------------------

# make_pair "COLUMN DEFS" ["WITH options for columnar"]
# Creates t_heap (heap) and t_col (columnar) with the same schema.
make_pair() {
	local defs="$1"
	psql_run "DROP TABLE IF EXISTS t_heap; DROP TABLE IF EXISTS t_col;"
	psql_run "CREATE TABLE t_heap ($defs) USING heap;"
	psql_run "CREATE TABLE t_col  ($defs) USING pgcolumnar;"
}

# load_pair "INSERT SELECT body" : insert identical rows into both. The body is
# everything after INSERT INTO <t>, e.g. "SELECT g, g::text FROM generate_series(1,10) g".
# Data is generated once into heap, then copied to columnar, so both hold
# byte-identical logical contents regardless of any volatile generators.
load_pair() {
	local body="$1"
	psql_run "INSERT INTO t_heap $body;"
	psql_run "INSERT INTO t_col SELECT * FROM t_heap;"
}

# Storage id for a columnar relation by name.
storage_id_of() {
	q "SELECT pgcolumnar.get_storage_id('$1');"
}

# Number of row groups physically written for a columnar relation (default
# t_col). The row group is the native write unit and honors stripe_row_limit.
stripe_count() {
	local rel="${1:-t_col}"
	q "SELECT count(*) FROM pgcolumnar.row_group
	   WHERE storage_id = pgcolumnar.get_storage_id('$rel');"
}

# Number of vectors written for a columnar relation (default t_col). The vector
# is the unit of encoding and of min/max skipping and honors chunk_group_row_limit;
# counted as one per-vector zone map for column 0 across all row groups.
chunk_group_count() {
	local rel="${1:-t_col}"
	q "SELECT count(*) FROM pgcolumnar.zone_map
	   WHERE storage_id = pgcolumnar.get_storage_id('$rel')
	     AND vector_index >= 0 AND column_index = 0;"
}

# ---- summary ---------------------------------------------------------------

# A dependency this suite needs is not installed.
#
# This FAILS, and that is the point. A skip is a red that nobody has to look at,
# which is how fifteen suites came to report PASSED while asserting nothing, and
# how temporal.sh has been green on PG18 without btree_gist. A box that cannot run
# a suite is not a box that passed it.
#
# The opt-out is explicit and per-capability, so a developer without pyarrow can
# still work, and so the waiver is visible in the command rather than implied by
# silence:
#
#     PGC_ALLOW_MISSING_PYARROW=1 test/native_parquet_units.sh
#     PGC_ALLOW_MISSING=1         test/run_all_versions.sh
#
# Missing DEPENDENCY and not-applicable-to-this-MAJOR are different things and are
# deliberately not the same code path. PostgreSQL 15 has no WITHOUT OVERLAPS to
# test and no amount of installing will give it one, so those gates call
# pgc_summary directly and report SKIPPED. Nothing is broken there. Here it is.
pgc_skip() {  # pgc_skip <capability> <message>
	local cap allow_one
	cap="$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
	allow_one="PGC_ALLOW_MISSING_$cap"
	if [ "${PGC_ALLOW_MISSING:-0}" = 1 ] || [ "${!allow_one:-0}" = 1 ]; then
		# The unwaived branch below records a FAIL. This one printed and left
		# PGC_CHECKS at zero, so waiving a dependency also erased the outcome --
		# the same asymmetry check_timing had, in the function whose whole
		# subject is "a missing dependency is not a pass".
		check_skip "$2" "SKIP  $2 (waived by $allow_one or PGC_ALLOW_MISSING)" \
			"waived by $allow_one or PGC_ALLOW_MISSING"
		pgc_summary
	fi
	pgc_record FAIL "$2" "FAIL  $2"
	echo "      A missing dependency is an environment defect, not a pass. Install"
	echo "      it, or set $allow_one=1 to run knowingly without this coverage."
	pgc_summary
}

# Three states, not two (#447).
#
# The verdict used to be a function of PGC_FAIL alone, and PGC_CHECKS was printed
# and never read. So a suite that asserted NOTHING printed PASSED and exited 0,
# indistinguishable from one that ran four hundred checks. Fifteen suites do that
# whenever pyarrow is absent, which is how an entire Parquet and Arrow surface,
# including both fuzzers, can leave a run with every line still saying PASSED.
#
# A suite that ran no checks did not pass. It is also not a failure: PostgreSQL 15
# genuinely has no WITHOUT OVERLAPS to test, and a developer box without an
# optional dependency is a supported configuration rather than a defect. Making
# those red is the "a red everyone knows to ignore is a red nobody reads" failure
# this tree keeps arguing against.
#
# So skipped is its own exit code. 0 passed, 1 failed, 2 ran nothing. The drivers
# count 2 separately and report how many suites actually ran, which is what #422
# did one level up for how many VERSIONS actually ran.
pgc_summary() {
	# FIRST, before any output and before any branch: every path out of this
	# function exits, so a flag set later would be missed by whichever path
	# takes an early one. The trap only needs to know that the summary was
	# REACHED, not that it completed.
	PGC_SUMMARY_PRINTED=1
	local _failed=$PGC_FAILED
	local _sum=$((PGC_PASSED + PGC_FAILED + PGC_UNRUN + PGC_SKIPPED))
	echo
	echo "checks run: $PGC_CHECKS"
	echo "checks unrunnable: $PGC_UNRUN"
	# Every state in a total. A state that is not in a total is a state that can
	# go missing, and this harness has 3,762 check sites -- far past what anyone
	# notices by reading. If this line does not add up the harness is lying about
	# its own arithmetic, so it is a failure rather than a note.
	echo "accounting: $PGC_PASSED passed + $_failed failed + $PGC_UNRUN unrunnable + $PGC_SKIPPED skipped = $PGC_CHECKS"
	# A MEASUREMENT, not an identity. The failed count is its own counter rather
	# than CHECKS - PASSED - UNRUN, because a derived third term makes
	# P + (N-P-U) + U = N true for ANY values: a helper that counts a check and
	# records no outcome drifts invisibly. That is not hypothetical -- check_ratio
	# printed PASS and never touched PGC_PASSED, so every passing ratio check was
	# reported as a failure in six shipped suites, and the first version of this
	# line could not see it. Three counters maintained independently, reconciled
	# against a fourth, is the only version of it that can fail.
	if [ "$_sum" != "$PGC_CHECKS" ]; then
		echo "FAIL  the summary does not reconcile: $PGC_PASSED passed + $PGC_FAILED failed + $PGC_UNRUN unrunnable + $PGC_SKIPPED skipped = $_sum, but $PGC_CHECKS checks ran"
		echo "      A check was counted whose outcome nothing recorded. Find the helper"
		echo "      that bumps PGC_CHECKS without touching PGC_PASSED, PGC_FAILED, PGC_UNRUN or PGC_SKIPPED."
		PGC_FAIL=1
	fi
	if [ "$PGC_FAIL" != "0" ]; then
		echo "$(basename "$0"): FAILED"
		# A source-shape suite (wal_envelope, decode_interrupts) never calls
		# pgc_setup, so there is no cluster and no log. Without this guard the
		# summary dies on an unset PGC_LOGFILE under `set -u` and the suite
		# reports "unbound variable" instead of which check failed.
		if [ -n "${PGC_LOGFILE:-}" ]; then
			# The tail is the right thing to show when one statement failed and
			# the wrong thing after a crash. A crashing backend takes the
			# postmaster through "terminating any other active server processes"
			# and recovery for every subsequent check, so the cause sits at the
			# TOP of the log and the last 40 lines are its aftermath.
			#
			# Measured under the pg18_san build, with a deliberate heap overrun:
			# 67 AddressSanitizer reports in an 8,777-line log, the first at line
			# 12. The tail showed lines 8738-8777 and pgc_teardown then removed
			# the file, so a suite reported 123 failures with no way to find out
			# why -- the diagnosis existed, for a quarter of a second, 8,765 lines
			# above the only window anyone was shown.
			#
			# grep the whole file for the events that mean "this was not a failed
			# assertion", and print the first few with line numbers.
			_pgc_fatal="$(pgc_pg "grep -nE '$(pgc_fatal_pattern)' '$PGC_LOGFILE' | head -5" 2>/dev/null || true)"
			if [ -n "$_pgc_fatal" ]; then
				echo "---- first fatal events in the server log ----"
				printf '%s\n' "$_pgc_fatal"
				echo "(the tail below is what followed; the cause is above)"
			fi
			echo "---- server log tail ----"
			pgc_pg "tail -40 '$PGC_LOGFILE'" 2>/dev/null || true
		fi
		exit 1
	fi
	# EVALUATED nothing, not ENCOUNTERED nothing. Before the fourth counter a
	# skipped check left PGC_CHECKS at zero, so this branch caught the all-skipped
	# suite by accident; now it is counted, and the suite would report PASSED with
	# nothing behind it. The condition is what it always meant.
	if [ "$((PGC_PASSED + PGC_FAILED + PGC_UNRUN))" = "0" ]; then
		echo "$(basename "$0"): SKIPPED (ran no checks)"
		exit $PGC_EXIT_SKIPPED
	fi
	if [ "$PGC_UNRUN" != "0" ]; then
		echo "$(basename "$0"): INCOMPLETE"
		exit $PGC_EXIT_INCOMPLETE
	fi
	echo "$(basename "$0"): PASSED"
	exit 0
}

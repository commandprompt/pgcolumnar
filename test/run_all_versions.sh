#!/usr/bin/env bash
#
# pgColumnar multi-version build-and-test matrix.
#
# For each pg_config given, this builds the extension fresh in a per-major build
# directory (so nothing leaks between majors) and runs every suite
# (smoke + phase2..phase6 + audit + concurrency + unique_conc + differential +
# recovery + fuzz) against it. It prints a
# per-version PASS/FAIL line and
# a final summary table, and exits non-zero if any version fails to build or any
# suite fails.
#
# Usage:
#   test/run_all_versions.sh [PG_CONFIG ...]
#
# With no arguments it uses a default list covering PostgreSQL 15 through 19.
# PostgreSQL 13 (end of life) and 14 are no longer in the default matrix.
# Each PG_CONFIG must point at an assert-enabled build to exercise the asserts.
# Run as a user that may "runuser -u postgres" (e.g. root); the suites start
# throwaway clusters as the postgres OS user.
#
# Written fresh for pgColumnar. It reuses no upstream test harness.

set -uo pipefail

# One name per line, and it must stay that way.
#
# This was a single backslash-continued line, so every pull request that adds a
# suite edited the same line and any two of them conflicted by construction. It
# happened four times in one day (#459 vs #462, #460 vs #462, #462 vs #468, and
# #444 behind them) and four more times the night #446, #468 and #444 landed.
#
# The resolution was the dangerous part, not the conflict. Appending the new name
# after the closing paren is valid shell that `bash -n` accepts, and it does not
# merely leave a stray command: `NAME=value cmd` scopes the assignment to that
# command, and an array literal is no exception, so `SUITES=(...) my_suite` leaves
# SUITES UNSET and the matrix runs nothing at all. harness_selftest pins that (#469).
#
# One name per line means two pull requests adding two suites touch two different
# lines and merge cleanly. Do not re-flow this into one line to save space.
SUITES=(
	advisory_lock_class
	alter_am_cleanup
	alter_column_type
	analyze_differential
	analyze_function
	analyze_reltuples
	analyze_stats
	arrow_export
	arrow_import
	arrow_nested
	arrow_nested_import
	audit
	autovacuum
	autovacuum_yield
	avro_manifest
	batch_fold_explain
	bench_guards
	bloom_lazy
	bloom_setting
	bloom_sizing
	cancel_decode
	catalog_natts
	column_projection
	concurrency
	concurrent_diff
	corruption
	cost_written_geometry
	debug_hook_privilege
	decode_interrupts
	decode_skip_interrupts
	dependency_estimate
	differential
	doc_parallel_premise
	docs_style
	drop_cleanup
	eager_ordering_record
	encode_effort
	encode_invariants
	entry_point_privilege
	estimate_deleted
	export_sink
	fk_referencing
	fsst_margin
	fsst_verdict_cache
	fuzz
	fuzz_arrow
	fuzz_avro
	fuzz_listing
	fuzz_parquet
	generated_columns
	groupagg_table_sizing
	hardening
	harness_selftest
	hilbert_cluster
	hilbert_curve
	hilbert_locality
	iceberg_catalog
	iceberg_data_files
	iceberg_deletes
	iceberg_dv
	iceberg_fdw
	iceberg_fdw_estimate
	iceberg_fdw_projection
	iceberg_malformed
	iceberg_name_mapping
	iceberg_name_mapping_memory
	iceberg_objstore
	iceberg_rest
	iceberg_rest_scan
	iceberg_rest_server
	iceberg_rest_vended
	iceberg_scan
	import_deferred
	import_exclusion
	import_export_privilege
	index_delete_liveness
	index_fetch_penalty_width
	index_only
	inheritance
	int8_agg_int128
	isolation
	local_open_race_free
	logical_decoding_cdc_recipe
	logical_decoding_source
	logical_subscriber
	maintenance_due
	native_agg
	native_agg_addcolumn
	native_agg_deletes
	native_backend_crash
	native_batch_fold_projection
	native_bloom
	native_cancel
	native_chunk_length_bound
	native_cluster
	native_compact
	native_ctas
	native_decode_gate_width
	native_decode_gating
	native_delete_vector_index
	native_delete_visibility_paths
	native_dict_underfill
	native_dml
	native_encdesc_golden
	native_encoding
	native_exact_selection
	native_fastdecode
	native_fetch_bigcap
	native_fetch_cache
	native_fetch_group_memo
	native_fetch_interrupt
	native_fetch_position
	native_fetch_projection
	native_fetch_sort_context
	native_fold_deferral
	native_fold_skipguard
	native_format
	native_gap
	native_groupagg
	native_groupagg_batch
	native_groupagg_wide_cost
	native_index
	native_index_fetch_stripe_cost
	native_index_projection
	native_ios
	native_join_runtime_filter
	native_join_vector_agg
	native_late_materialization
	native_lazy_slot
	native_metadata_flush
	native_ownership
	native_page_offset_bound
	native_param_pushdown
	native_parquet_codecs
	native_parquet_dict_oob
	native_parquet_fdw
	native_parquet_fieldid
	native_parquet_flba
	native_parquet_hardening
	native_parquet_multifile
	native_parquet_partition
	native_parquet_projection
	native_parquet_pushdown
	native_parquet_schema
	native_parquet_stack
	native_parquet_streaming
	native_parquet_units
	native_projection
	native_read_parquet
	native_reclaim
	native_reclaim_cycles
	native_reclaim_frag
	native_reclaim_reconcile
	native_recluster
	native_repack
	native_rewrite
	native_rewrite_conc
	native_roundtrip
	native_saop_pushdown
	native_skip
	native_sort_by
	native_toasted_write
	native_truncate
	native_upgrade_converge
	native_vacuum_race
	native_varlena_bound
	native_vecdecode
	native_vecskip
	native_writer
	native_zonemap
	native_zonemap_narrow
	native_zonemap_session
	objstore_addressing
	objstore_allowlist
	objstore_credentials
	objstore_crlf
	objstore_http_read
	objstore_listing
	objstore_module
	objstore_s3_read
	objstore_sink_write
	objstore_stash_recovery
	objstore_tls_read
	objstore_userinfo
	parallel
	parallel_copy
	parallel_copy_dedup
	parallel_degree
	parallel_export_parquet
	parallel_flush_optin
	parallel_vector_agg
	parquet_count_bounds
	parquet_export
	parquet_export_stats
	parquet_import
	parquet_level_width
	parquet_nested
	parquet_nested_import
	pg19_vacuum_options
	pg_dump_roundtrip
	phase2
	phase3
	phase4
	phase5
	phase6
	planner_choice_quality
	preimage_rewrite
	projection_drop_column
	projection_privilege
	projection_rename_restore
	projection_rewrite
	projection_update
	projections
	pushdown_report
	qual_order_selectivity
	read_stream
	reader_buffer_reuse
	recluster_extent
	recluster_gate
	recovery
	replication
	rewrite_group_scan
	rls_direct_storage
	row_triggers
	scan_decode_cost
	scan_direction
	server_file_privilege
	smoke
	sort_status
	sort_status_privilege
	sorted_mark_rename
	sorted_pathkeys
	sorted_projection
	stats_privilege
	tablesample
	temporal
	truncate_cleanup
	ttl_expire
	ungrouped_vector_agg
	unique_conc
	update_conc
	vacuum_lock_privilege
	vacuum_sorted_gate
	vacuum_stripe_count
	vector_agg_rescan_memory
	vector_agg_tlist_shape
	vm_clear_on_renumber
	vm_privilege
	wal_envelope
	write_fsst_compressed
	write_minmax_fastpath
	zonemap_boundaries
	zonemap_cost
	zonemap_estimate_sample)


# ---------------------------------------------------------------------------
# Run from a private copy of this script, and refuse to run twice at once.
#
# Both guards exist because both failures happened, and neither announced
# itself as what it was.
#
# bash reads a script incrementally as it executes it, so editing this file
# while a run is in progress corrupts the run in place. A gate died at
# "line 139: `done'" with the file on disk perfectly valid, because the bytes
# had moved under the interpreter between one read and the next. Re-executing
# from a copy makes an in-flight run immune to whatever happens to the original.
#
# And two runs at once quietly ruin each other: they contend for clusters and
# ports, and the symptom is a suite failing with no named check -- a wall of
# ERROR: database "regress" already exists and a red result that looks exactly
# like a real one. Three false reds in one day were traced to this. A run now
# leaves a lock naming its pid, so the second one says so and stops instead of
# producing a result nobody can trust.
# ---------------------------------------------------------------------------

PGC_RUN_LOCK="${PGC_RUN_LOCK:-/tmp/pgcolumnar-run_all_versions.lock}"

# --stop: the supported way to end a run in progress.
#
# It exists because the alternative was pkill, and pkill is wrong here twice: the
# driver re-executes itself from /tmp under a generated name, so the obvious
# pattern misses it, and killing postmasters directly bypasses pg_ctl. This reads
# the lock, signals the owner, and lets the owner's own trap stop the suites and
# their clusters properly.
# --list-suites: print the matrix's suite list, one name per line, then exit.
#
# It exists so that nothing has to parse this array a second time. A gate that
# re-implements bash's array parsing in awk disagrees with bash on the very
# mistake this array invites: a name after the closing paren is a stray COMMAND
# to bash and a member to a text parser, so the gate passed while the suite
# silently never ran. Asking the runner means there is one parser, bash's, and
# no way for the two to drift.
#
# Handled BEFORE the run lock on purpose. harness_selftest calls this from
# inside a running matrix, and taking the lock there would refuse to answer.
if [ "${1:-}" = "--list-suites" ]; then
	printf '%s\n' "${SUITES[@]}"
	exit 0
fi

if [ "${1:-}" = "--stop" ]; then
	if [ ! -e "$PGC_RUN_LOCK" ]; then
		echo "no matrix run in progress (no lock at $PGC_RUN_LOCK)"
		exit 0
	fi
	_owner="$(sed -n 1p "$PGC_RUN_LOCK" 2>/dev/null)"
	if [ -z "$_owner" ] || ! kill -0 "$_owner" 2>/dev/null; then
		echo "stale lock from pid ${_owner:-unknown}; removing it"
		rm -f "$PGC_RUN_LOCK"
		exit 0
	fi
	echo "stopping matrix run (pid $_owner)"
	kill -TERM "$_owner" 2>/dev/null
	for _i in $(seq 1 60); do
		kill -0 "$_owner" 2>/dev/null || break
		sleep 1
	done
	if kill -0 "$_owner" 2>/dev/null; then
		echo "pid $_owner did not exit after 60s; sending KILL" >&2
		kill -KILL "$_owner" 2>/dev/null
	fi
	rm -f "$PGC_RUN_LOCK"
	echo "stopped"
	exit 0
fi

if [ -z "${PGC_RUN_REEXEC:-}" ]; then
	# Take the lock before copying, so two starts cannot both decide they are first.
	if [ -e "$PGC_RUN_LOCK" ]; then
		_holder="$(sed -n 1p "$PGC_RUN_LOCK" 2>/dev/null)"
		if [ -n "$_holder" ] && kill -0 "$_holder" 2>/dev/null; then
			echo "FATAL: a matrix run is already in progress (pid $_holder)" >&2
			sed -n '2,$p' "$PGC_RUN_LOCK" >&2 2>/dev/null
			echo "       wait for it, or kill $_holder, or set PGC_RUN_LOCK to run" >&2
			echo "       against a separate tree on a different port." >&2
			exit 1
		fi
		echo "note: taking over a stale lock from pid ${_holder:-unknown}" >&2
		rm -f "$PGC_RUN_LOCK"
	fi

	{
		echo "$$"
		echo "       started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
		echo "       tree:    $(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
		echo "       args:    ${*:-<default matrix>}"
	} > "$PGC_RUN_LOCK"

	# The lock is removed only by the process that took it, so a stale-lock
	# takeover cannot have its lock deleted by the run it replaced.
	# INT and TERM as well as EXIT. A bash EXIT trap does not run when the shell
	# is terminated by an untrapped signal, so without these a killed run left
	# its lock behind and the next run refused to start against a pid that was
	# already gone.
	trap 'if [ "$(sed -n 1p "$PGC_RUN_LOCK" 2>/dev/null)" = "$$" ]; then rm -f "$PGC_RUN_LOCK"; fi' EXIT INT TERM

	_self="$(mktemp "/tmp/pgcolumnar-run_all_versions.$$.XXXXXX.sh")"
	cp "${BASH_SOURCE[0]}" "$_self"
	chmod +x "$_self"
	# Run the copy in the background and forward signals to it, rather than
	# calling it directly. --stop signals this parent, but the child is the one
	# holding the suite jobs, so a TERM that stops here and not there is exactly
	# the orphaning this is meant to prevent.
	PGC_RUN_REEXEC=1 \
	PGC_RUN_SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" \
	PGC_RUN_LOCK="$PGC_RUN_LOCK" \
	PGC_RUN_OWNER="$$" \
		bash "$_self" "$@" &
	_child=$!
	_forward_stop() {
		kill -TERM "$_child" 2>/dev/null
		wait "$_child" 2>/dev/null
		if [ "$(sed -n 1p "$PGC_RUN_LOCK" 2>/dev/null)" = "$$" ]; then
			rm -f "$PGC_RUN_LOCK"
		fi
		exit 130
	}
	trap _forward_stop INT TERM
	wait "$_child"
	_rc=$?
	rm -f "$_self"
	exit $_rc
fi

# Re-executed from the copy: the lock belongs to the parent, which removes it,
# and the tree to test is the original one rather than wherever the copy landed.
trap - EXIT

# ---------------------------------------------------------------------------
# Stopping a run
# ---------------------------------------------------------------------------
#
# This driver used to have no cleanup at all -- the line above dropped the
# parent's EXIT trap and put nothing in its place -- and the suites it starts run
# as background jobs. So killing the driver did not stop them: they were
# reparented and carried on, holding their clusters and their ports, writing into
# the log someone was reading. Both of those cost real time on this branch. One
# orphaned run's output interleaved into a live run's log and produced a FAIL that
# belonged to neither; another held the run lock for twenty minutes after it was
# "killed", so three matrices refused to start and reported nothing.
#
# The only way to stop a run was pkill, which is the wrong instrument twice over:
# it misses this process (re-executed from /tmp under a generated name) and it
# kills postmasters out from under pg_ctl instead of asking them to stop.
#
# So: a signal now stops the suites, then stops their clusters with pg_ctl -- the
# supported path, using the pg_ctl matching each cluster's own major, read from
# its PG_VERSION rather than guessed.
pgc_stop_clusters() {
	local d sub datadir ver pgctl stopped=0
	for d in /tmp/pgcolumnar-test.*/; do
		[ -d "$d" ] || continue
		for sub in data standby restore; do
			datadir="$d$sub"
			[ -f "$datadir/postmaster.pid" ] || continue
			ver="$(sed -n 1p "$datadir/PG_VERSION" 2>/dev/null)"
			pgctl="/usr/local/pg${ver}/bin/pg_ctl"
			# Fall back to any pg_ctl only if the versioned one is absent; a
			# mismatched pg_ctl refuses rather than corrupting anything.
			[ -x "$pgctl" ] || pgctl="$(command -v pg_ctl 2>/dev/null)"
			[ -n "$pgctl" ] && [ -x "$pgctl" ] || continue
			if [ "$(id -u)" = 0 ] && id postgres >/dev/null 2>&1; then
				su postgres -c "'$pgctl' -D '$datadir' stop -m immediate -w -t 30" >/dev/null 2>&1
			else
				"$pgctl" -D "$datadir" stop -m immediate -w -t 30 >/dev/null 2>&1
			fi
			stopped=$((stopped + 1))
		done
	done
	[ "$stopped" -gt 0 ] && echo "stopped $stopped cluster(s) with pg_ctl" >&2
	return 0
}

pgc_run_cleanup() {
	local j
	trap - INT TERM EXIT
	echo "" >&2
	echo "matrix interrupted -- stopping suites and their clusters" >&2
	# Stop the suites first so they cannot start anything else, then their
	# clusters. Their own EXIT traps handle the ones they can still reach.
	for j in $(jobs -p 2>/dev/null); do kill -TERM "$j" 2>/dev/null; done
	sleep 2
	for j in $(jobs -p 2>/dev/null); do kill -KILL "$j" 2>/dev/null; done
	pgc_stop_clusters
	if [ -n "${PGC_CUR_BUILDDIR:-}" ] && [ -d "$PGC_CUR_BUILDDIR" ]; then
		rm -rf "$PGC_CUR_BUILDDIR"
		echo "removed the in-progress build directory" >&2
	fi
	exit 130
}
trap pgc_run_cleanup INT TERM

SRCDIR="${PGC_RUN_SRCDIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Default matrix: one assert-enabled pg_config per major, 15 through 19.
DEFAULT_CONFIGS=(
	/usr/local/pg15/bin/pg_config
	/usr/local/pg16/bin/pg_config
	/usr/local/pg17/bin/pg_config
	/usr/local/pgsql/bin/pg_config
	/usr/local/pg19/bin/pg_config
)

if [ "$#" -gt 0 ]; then
	CONFIGS=("$@")
else
	CONFIGS=("${DEFAULT_CONFIGS[@]}")
fi

# A private base port per run, bumped per suite, to avoid clashes.
#
# Derived from the run's own pid rather than fixed, because a fixed default is
# not private: two runs on one box then start at the same port and fight over
# every cluster. The lock above makes that refuse rather than happen, but the
# suites are also run individually, and they should not collide either.
#
# Below the ephemeral floor, deliberately. This is the second copy of the
# arithmetic in portlib.sh, kept because the driver re-executes itself from /tmp
# and a tree-relative source would be the fragility that re-exec removes.
#
# The old band, 40000-59999, was entirely inside /proc/sys/net/ip_local_port_range
# (32768-60999 here), so the kernel could hand a cluster's port to an outbound
# connection between the free-check and the bind. See portlib.sh for the full
# account; it is the cause of the intermittent replication failure.
_pgc_eph_floor="$(awk '{print $1}' /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null)"
case "$_pgc_eph_floor" in ''|*[!0-9]*) _pgc_eph_floor=32768 ;; esac
if [ "$_pgc_eph_floor" -lt 20000 ]; then
	echo "FATAL: ephemeral floor $_pgc_eph_floor too low to carve a private port band" >&2
	echo "       /proc/sys/net/ipv4/ip_local_port_range starts at $_pgc_eph_floor; this" >&2
	echo "       needs at least 20000 so cluster ports sit below the range the kernel" >&2
	echo "       assigns to outbound connections. Raise it:" >&2
	echo "           sysctl -w net.ipv4.ip_local_port_range=\"32768 60999\"" >&2
	exit 1
fi
# Mirrors portlib.sh: main band below the auxiliary band, both below the floor.
_pgc_aux_hi=$(( _pgc_eph_floor - 1000 ))
_pgc_aux_lo=$(( _pgc_aux_hi - 2000 ))
PGC_PORT_LO=10000
PGC_PORT_HI=$(( _pgc_aux_lo - 200 ))
_pgc_walk=1500
_pgc_span=$(( PGC_PORT_HI - PGC_PORT_LO - _pgc_walk ))
BASE_PORT="${PGC_BASE_PORT:-$(( PGC_PORT_LO + (${PGC_RUN_OWNER:-$$} % _pgc_span) ))}"

# The band must hold one port per suite per major, plus the auxiliary clusters a
# suite stands up beyond its own. Asserted rather than assumed: the walk is a
# constant here and the suite list grows, so a silent overrun would put a cluster
# back inside the ephemeral range -- the exact failure this layout removes, and
# one that costs days to recognise.
_pgc_need=$(( ${#SUITES[@]} * ${#CONFIGS[@]} + 16 ))
if [ $(( BASE_PORT + _pgc_need )) -ge "$PGC_PORT_HI" ]; then
	echo "FATAL: port band too small for this run" >&2
	echo "       ${#SUITES[@]} suites x ${#CONFIGS[@]} majors needs $_pgc_need ports from $BASE_PORT," >&2
	echo "       which passes the top of the band ($PGC_PORT_HI)." >&2
	echo "       Raise net.ipv4.ip_local_port_range's floor, or set PGC_BASE_PORT lower." >&2
	exit 1
fi

overall=0
declare -a SUMMARY

# Suites whose checks compare wall-clock times, and which therefore cannot be run
# beside five others.
#
# The rest of the matrix runs PGC_JOBS suites at once, each with its own cluster,
# so every ratio in those suites is measured under six-way contention. That is
# not a fixture problem and no threshold survives it: the same check on the same
# build measures 1.11 alone and 2.11 inside the matrix, against a bound of 2.0.
# Three suites produced red gates that way in one session, each costing a re-run
# to disprove, which is how a matrix teaches its readers to discount red.
#
# Tuning the fixtures was tried first and does not work. On the group-doubling
# check, raising the fetch count to dilute the shared decode makes the ratio
# worse rather than better -- 1.08 at 6,000 fetches, 1.23 at 20,000, 1.29 at
# 40,000 -- because per-fetch cost is itself a function of group size, so more
# fetches amplify the difference instead of averaging it away. The comment in
# native_fetch_position.sh that recommended exactly that has been corrected.
#
# Not every wall-clock ratio belongs here, and the distinction is measurable
# rather than a matter of taste. native_fetch_cache asserts one on the same
# index-driven fetch with the same stopwatch, and is deliberately absent: it
# compares one big group against ten small ones, and the cache equalises
# per-fetch cost across both sides, so contention is common-mode and cancels in
# the ratio. Measured, six-way: absolute times roughly doubled and the ratio
# stayed near 1 against a bound of 3, worst case 1.15.
#
# The three below measure quantities whose per-fetch or per-query cost is itself
# a function of the thing being varied, so contention is differential and does
# not cancel. That is the test for membership: does the load move both sides of
# the ratio together?
#
# So they run alone. It costs a few minutes per major and it buys a timing
# result that means something.
is_timing_suite() {
	case "$1" in
		native_fetch_position|native_cancel|native_agg_deletes) return 0 ;;
		# planner_choice_quality's only assertion is a wall-clock ratio. Left out
		# of this list it still RAN under PGC_SKIP_TIMING, skipped the ratio, and
		# reported PASS on the strength of its premises -- so a regression of #434
		# would have been reported green by the suite that exists to catch it. A
		# suite whose subject is dropped has not passed, and the driver already
		# knows how to say that.
		planner_choice_quality) return 0 ;;
		*) return 1 ;;
	esac
}

# Suites that must not run beside five others, which is a wider set than the
# timing ones and for a second reason.
#
# The timing suites are here because a wall-clock ratio cannot be measured under
# contention. replication is here because it is heavy rather than because it
# measures anything: it stands up a SECOND cluster, runs pg_basebackup, and
# streams between the two, so at PGC_JOBS=6 it is competing with six other
# clusters for the same box. It failed roughly one full matrix in three, on a
# different major each time -- PG18, then PG19, then PG16 -- which is the
# signature of resource contention rather than a defect in the suite or the
# major.
#
# Deliberately NOT the same predicate as is_timing_suite, because
# PGC_SKIP_TIMING drops those in CI and replication must still run there. Serial
# and skipped are different properties and were one list until this suite needed
# one without the other.
# planner_choice_quality is here THROUGH is_timing_suite, not beside it, and
# this comment used to say the opposite (#764): "deliberately not in
# is_timing_suite: its premises are plan-shape assertions, and those are worth
# running in CI". It is in that list, so under PGC_SKIP_TIMING the driver does
# not invoke the suite at all and none of those plan-shape assertions runs in
# CI. A reader who found this comment believed they did.
#
# The entry in is_timing_suite is the correct half and its own comment says why:
# left out of that list the suite RAN, dropped the ratio, and reported PASS on
# its premises alone, so a regression of #434 would have been green from the
# suite that exists to catch it. Confirmed by running it directly with
# PGC_SKIP_TIMING=1: PASSED, 7 checks, only the two wall-clock ratios skipped.
#
# The coverage that costs -- no plan-shape assertion from this suite in CI -- is
# a real gap and #764 owns the decision. A planner change should carry its own
# plan-shape arms in a suite CI invokes rather than lean on this one.
runs_alone() {
	case "$1" in
		replication) return 0 ;;
		# objstore_module moves the INSTALLED module aside to test the
		# not-installed and broken paths. That file is shared by every suite in
		# the run, so doing it while others execute would break them, and the
		# breakage would look like a defect in whichever suite happened to load
		# the extension at the wrong moment.
		objstore_module) return 0 ;;
		# objstore_stash_recovery arranges that same shared file into the states
		# an interrupted run leaves behind, and runs objstore_module against each.
		# It moves the module for the same reason and must be alone for it.
		objstore_stash_recovery) return 0 ;;
		*) is_timing_suite "$1" ;;
	esac
}

# How many majors were actually built and run. A matrix that ran nothing is not
# a matrix that passed, and until #418 it said "ALL VERSIONS PASSED" and exited
# 0 when every configured pg_config was missing. See the summary block.
VERSIONS_RUN=0

for pgc in "${CONFIGS[@]}"; do
	if [ ! -x "$pgc" ]; then
		echo "SKIP  $pgc (not executable)"
		SUMMARY+=("SKIP   $pgc")
		continue
	fi
	VERSIONS_RUN=$((VERSIONS_RUN + 1))

	ver="$("$pgc" --version)"
	major="$(echo "$ver" | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"
	builddir="$(mktemp -d "/tmp/pgcolumnar-matrix-${major}.XXXXXX")"
	# Recorded so an interrupted run can remove it. A completed major removes
	# its own at the bottom of the loop; one that is stopped part-way used to
	# leave a full source tree and install behind in /tmp.
	PGC_CUR_BUILDDIR="$builddir"

	echo "==================================================================="
	echo "== $ver"
	echo "== pg_config=$pgc"
	echo "== builddir=$builddir"
	echo "==================================================================="

	# Fresh copy of the tree so each major builds in isolation.
	cp -a "$SRCDIR/." "$builddir/"
	make -C "$builddir" clean PG_CONFIG="$pgc" >/dev/null 2>&1 || true

	if ! make -C "$builddir" PG_CONFIG="$pgc" >/dev/null 2>"$builddir/build.err"; then
		echo "BUILD FAILED"
		sed 's/^/    /' "$builddir/build.err"
		SUMMARY+=("FAIL   PG$major  (build)")
		overall=1
		continue
	fi
	# Any compiler warning is a failure for this matrix.
	if grep -q "warning:" "$builddir/build.err"; then
		echo "BUILD WARNINGS"
		grep "warning:" "$builddir/build.err" | sed 's/^/    /'
		SUMMARY+=("FAIL   PG$major  (warnings)")
		overall=1
		continue
	fi

	# Install the extension once for this version; the suites then skip their own
	# build/install (PGC_SKIP_BUILD) and run in parallel, each in its own throwaway
	# cluster on its own port. This keeps per-suite cluster isolation (crash and
	# recovery suites need it) while removing the redundant per-suite rebuild and
	# the serial initdb/start bottleneck. PGC_JOBS controls the degree.
	if ! make -C "$builddir" install PG_CONFIG="$pgc" >/dev/null 2>>"$builddir/build.err"; then
		echo "INSTALL FAILED"
		sed 's/^/    /' "$builddir/build.err"
		SUMMARY+=("FAIL   PG$major  (install)")
		overall=1
		continue
	fi

	# THIS RUNNER IS THE CONTROLLER FOR THIS MAJOR'S BATCH. It built and installed
	# once; the suites below all run with PGC_SKIP_BUILD=1 and would otherwise have
	# no way to tell whether the binary they measure came from this tree. Record the
	# fingerprint of the build inputs so each of them can check it.
	#
	# In a subshell sourcing lib.sh rather than recomputing the hash here: two
	# implementations of one fingerprint would drift, and the suites compare against
	# whatever this writes. lib.sh's top level is assignments and function
	# definitions only, so sourcing it costs nothing and starts nothing.
	# NOT `|| true`. If the stamp cannot be written, every suite in this batch
	# reports "freshness UNVERIFIED" and the controller arm silently stops being a
	# controller arm -- the whole batch degrades to the state this exists to
	# prevent, and nothing says so. Say so.
	if (
		. "$builddir/test/lib.sh"
		pgc_write_source_stamp \
			"$(pgc_source_stamp_path "$builddir" "$pgc")" \
			"$(pgc_source_fingerprint "$builddir")" \
			"$(pgc_installed_library_digest "$pgc")"
	); then
		:
	else
		echo "WARNING: could not record the source stamp for major $major." >&2
		echo "         Every suite below will report freshness UNVERIFIED." >&2
	fi

	verfail=0
	results=""
	maxjobs="${PGC_JOBS:-6}"
	for s in "${SUITES[@]}"; do
		if runs_alone "$s"; then
			continue
		fi
		# throttle to maxjobs concurrent suites
		while [ "$(jobs -rp | wc -l)" -ge "$maxjobs" ]; do wait -n; done
		port=$((BASE_PORT++))
		(
			PGC_SKIP_BUILD=1 PGC_PORT="$port" \
				bash "$builddir/test/${s}.sh" "$pgc" >"$builddir/${s}.log" 2>&1
			echo $? >"$builddir/${s}.rc"
		) &
	done
	wait

	# Then the timing-sensitive suites, one at a time, with nothing else running.
	#
	# PGC_SKIP_TIMING=1 drops them entirely. That exists for shared CI hardware,
	# where the wall-clock ratios these three assert cannot be trusted: a runner
	# is noisy by construction and a gate that goes red for reasons unrelated to
	# the change teaches its readers to discount red, which is worse than not
	# running it. They stay in every local run, which is where the numbers mean
	# something.
	for s in "${SUITES[@]}"; do
		if ! runs_alone "$s"; then
			continue
		fi
		# PGC_SKIP_TIMING drops the wall-clock suites only. A suite that runs
		# alone for a resource reason rather than a measurement one -- replication
		# -- still runs, because there is nothing about a shared runner that makes
		# its assertions untrustworthy, only slower.
		if [ "${PGC_SKIP_TIMING:-0}" = 1 ] && is_timing_suite "$s"; then
			echo "  SKIP  $s (PGC_SKIP_TIMING)"
			# 66, not 0. This suite did not run, and the collector has a state
			# that says so. Recording it as a pass was the same lie the zero-check
			# suites were telling, just written by the driver.
			#
			# The log is written too, because the collector requires the marker as
			# well as the status: this branch never executes the suite, so nothing
			# else would produce one and the run would be classified a failure.
			echo 66 >"$builddir/${s}.rc"
			echo "$s.sh: SKIPPED (ran no checks)" >"$builddir/${s}.log"
			# Record the decision where it is made (#916). This suite calls
			# pgc_summary and will produce no accounting line, because it was
			# never run; the reconciliation needs that said by the driver rather
			# than guessed from the log the driver just forged.
			printf '%s\n' "$s" >>"$builddir/accounting.notdispatched"
			continue
		fi
		port=$((BASE_PORT++))
		PGC_SKIP_BUILD=1 PGC_PORT="$port" \
			bash "$builddir/test/${s}.sh" "$pgc" >"$builddir/${s}.log" 2>&1
		echo $? >"$builddir/${s}.rc"
	done

	# collect results in suite order for a stable, readable summary
	suites_ran=0
	suites_skipped=0
# Classify one suite's exit status. A function, not four inline branches,
# because the selftest evals THIS TEXT rather than re-deriving the condition: a
# check that recomputes a rule tests the world instead of the code.
#
# Two independent signals for every non-pass state, which is why 66 was chosen
# in the first place -- `set -e` propagates whatever status an aborting command
# returned, so a bare code can be produced by accident. 67 gets the same
# treatment: the code AND the line.
# Does a suite verdict fail its major?
#
# Split out for the same reason the classifier was: the selftest evals THIS TEXT.
# The first version of the INCOMPLETE branch set a write-only MAJOR_FAIL flag,
# assigned once and read nowhere, so a state that had been failing the gate by
# accident (67 fell to the else, which sets verfail=1) was routed explicitly to a
# branch that could not fail it. The classifier was right and the dispatch threw
# the answer away, which is why this is a function and not a line in a branch.
#
# The comment names the flag without spelling the assignment, because the arm in
# selftest 320 greps for it: a test for a pattern must not contain the pattern.
pgc_verdict_fails_major() {	# pgc_verdict_fails_major VERDICT -> yes|no
	case "$1" in
		PASS|SKIP) echo no ;;
		*)         echo yes ;;
	esac
}

pgc_classify_suite_rc() {	# pgc_classify_suite_rc RC LOGFILE -> PASS|SKIP|INCOMPLETE|FAIL
	local rc="$1" log="$2"
	if [ "$rc" = 0 ]; then
		echo PASS
	elif [ "$rc" = 66 ] && grep -q 'SKIPPED (ran no checks)' "$log" 2>/dev/null; then
		echo SKIP
	elif [ "$rc" = 67 ] && grep -q ': INCOMPLETE$' "$log" 2>/dev/null; then
		# A check could not be evaluated (#858). NOT a pass: the suite reached a
		# question it could not ask. Not a plain FAIL either, because nothing
		# asserted false -- but it must never reach the PASS branch, and the
		# reason travels with it.
		echo INCOMPLETE
	else
		echo FAIL
	fi
}

# Tally one suite's verdict into the per-major counters (#858).
#
# A function, not four branches in the middle of a loop, for the same reason
# the classifier is one: the selftest evals THIS TEXT and drives it. #859's
# regression was not in the classifier. The classifier returned INCOMPLETE
# correctly and the CALLER threw the answer away into a write-only flag -- and
# nothing could reach the caller, because the caller was a branch buried in a
# loop that needs a suite list and a populated build directory to run at all.
# Extracted, the whole chain is drivable end to end, which is what selftest 330
# does.
#
# It writes the CALLER'S counters on purpose, and must never declare them
# local. verfail, suites_ran, suites_skipped, suites_incomplete, results and
# skipped_names belong to the per-major scope; a `local` on any of them here
# would leave every count at zero while every arm that drives this function
# still passed -- the same shape of defect as the write-only flag, and just as
# invisible to a green run.
# ---- accounting membership, derived rather than listed -----------------------
#
# The matrix prints "suites that ran: N of M" and never checks it, and twelve
# registered suites exit 0 without ever calling pgc_summary. Measured, with a
# pattern tight enough to exclude portlib.sh -- a looser one matched it and gave
# both reviewers of this change the same wrong answer: NONE of the twelve sources
# test/lib.sh. Each defines its own check(), and ten of them keep no tally at all.
# So the harness cannot see their checks. The number is not written elsewhere on
# purpose; the reconciliation prints it at runtime. Counted among the
# suites that "ran", they are the same overcount #447 added that line to stop,
# one level further down.
#
# A COUNT cannot fix this. Two errors of opposite sign cancel, and an exempt list
# maintained by hand makes the count agree by construction -- the check then
# measures the list rather than the run. So membership is derived from a property
# each suite carries, and the two readings are reconciled as SETS, in both
# directions.

pgc_suite_declares_accounting() {	# pgc_suite_declares_accounting FILE -> yes|no
	# A suite participates in check accounting exactly when it calls pgc_summary,
	# which is the only thing that prints the accounting line and sets the status
	# pgc_classify_suite_rc reads.
	#
	# Comments are stripped first. A suite that explains in prose why it cannot
	# account would otherwise read as one that does, which is the failure mode
	# this whole approach exists to avoid: a claim satisfied by a mention.
	# grep -c, NOT grep -q, and the reason is the bug this harness has already
	# paid for once. `grep -q` exits the moment it matches, which closes the pipe
	# while sed is still writing; sed takes EPIPE and exits non-zero, and under
	# `set -o pipefail` the PIPELINE reports that failure even though grep
	# matched. It is a race between the two, so it reproduces on large files and
	# not small ones, and it names DIFFERENT innocent suites each run.
	#
	# That is not a hypothetical. Selftest 040 carries the same story from #473
	# and #476, and the first version of this function reproduced it exactly:
	# analyze_function and hilbert_curve -- two of the longest suites -- read as
	# not declaring accounting inside the selftest and as declaring it outside.
	#
	# grep -c reads to EOF, so sed never sees a closed pipe.
	# An ABSENT file is its own answer, not "does not declare accounting".
	# Conflating them classifies a registered suite whose .sh has vanished as
	# exempt, and the reconciliation then reads clean -- a suite disappearing
	# from the matrix, inside the check whose whole subject is suites that go
	# missing from the accounting. Reported by OffgridwithJD reviewing #922.
	local _f="$1" _n
	[ -f "$_f" ] || { echo absent; return 0; }
	# Strip a `#` only where the shell would treat one as starting a comment:
	# at the start of a line, or after whitespace. `sed 's/#.*$//'` strips from
	# ANY hash, so a `#` inside a quoted string earlier on the line would hide a
	# pgc_summary call after it. Measured across all 251 registered suites: three
	# carry a line with both, and in every one the `#` starts the line, so no
	# suite is misread today. The arm in selftest 390 keeps that true.
	_n="$(sed 's/\(^\|[[:space:]]\)#.*$/\1/' "$_f" \
		| grep -cE '(^|[^_[:alnum:]])pgc_summary([^_[:alnum:]]|$)' || true)"
	if [ "${_n:-0}" -gt 0 ]; then
		echo yes
	else
		echo no
	fi
}

pgc_log_shows_accounting() {	# pgc_log_shows_accounting LOGFILE -> yes|no
	# The runtime twin of the declaration above. pgc_summary prints this line on
	# EVERY exit path -- pass, failure, skip and incomplete -- before it decides
	# the status, so its presence says "this suite reached its summary" and not
	# "this suite passed". Anchored and fully shaped, so the word appearing in a
	# suite's own prose cannot satisfy it.
	# Unlike the declaration reader above, this one deliberately does NOT
	# distinguish an absent log from a present one carrying no accounting line.
	# Both mean the same thing here -- this suite did not reach its summary --
	# and a declared suite with no log at all is exactly the catch. The asymmetry
	# between the two readers is intentional and is noted because it is the kind
	# of thing that reads as an oversight later.
	local _log="$1"
	[ -f "$_log" ] || { echo no; return 0; }
	if grep -qE '^accounting: [0-9]+ passed \+ [0-9]+ failed \+ [0-9]+ unrunnable \+ [0-9]+ skipped = [0-9]+$' "$_log"; then
		echo yes
	else
		echo no
	fi
}

pgc_reconcile_records() {	# pgc_reconcile_records LOGFILE -> 0 ok, 1 mismatch
	# A suite's log states `checks run: N` and carries N record lines. They are
	# the same increment seen twice -- pgc_record does both -- so this cannot
	# fail by drifting. It CAN fail, which is why it is asserted: a suite killed
	# mid-way, a truncated log, or a helper that prints an outcome without
	# recording it all separate the two.
	#
	# A log with no `checks run:` line at all never reached its summary. That is a
	# different fault from a miscount, and it must not read as a clean
	# reconciliation just because there is nothing to compare against.
	local _log="$1" _records _stated
	if [ ! -f "$_log" ]; then
		echo "    no log to reconcile records against: $_log"
		return 1
	fi
	_records="$(grep -c '^RESULT	' "$_log" || true)"

	# THE COUNT IS NOT THE SCHEMA, and counting alone let six malformed shapes
	# reconcile cleanly: a record missing two fields, one carrying extra fields,
	# a verdict outside the vocabulary, an empty check name, and every field
	# empty. All measured returning 0 before this arm, against a well-formed
	# control that also returned 0 -- so the function could not tell them apart.
	# Reported by @linuxhikerpm on #917.
	#
	# ONE awk PASS, not a loop with a fork per record: a full matrix run carries
	# thousands of these, and the emitter next door already paid for that lesson
	# at 331x. The verdict list is the emitter's own, so the two cannot drift
	# without this going red.
	local _bad
	_bad="$(awk -F'\t' '
		/^RESULT	/ {
			n++
			if (NF != 7)                       { why[n] = "has " NF-1 " fields, want 6"; bad++; next }
			if ($2 == "" || $3 == "" || $4 == "") { why[n] = "has an empty suite, part or name"; bad++; next }
			if ($5 != "PASS" && $5 != "FAIL" && $5 != "UNRUN" && $5 != "SKIP") {
				why[n] = "has verdict \"" $5 "\", which pgc_record cannot emit"; bad++; next
			}
			# THE MAJOR IS VALIDATED, not merely present (#1010). Stored verbatim a
			# typo becomes a version the ledger then treats as authoritative --
			# exactly what a free-form --date did -- and a major decides WHICH
			# CHECKS CAN EXIST, so the consequence is larger here than for a date.
			if ($6 !~ /^([0-9]+|unknown)$/) {
				why[n] = "has major \"" $6 "\", which is neither a number nor \"unknown\""; bad++; next
			}
		}
		END {
			if (bad) { for (i = 1; i <= n; i++) if (i in why) print "      record " i " " why[i] }
			exit 0
		}' "$_log")"
	if [ -n "$_bad" ]; then
		echo "    $_records record(s) present but at least one does not parse:"
		printf '%s\n' "$_bad" | head -5
		return 1
	fi

	_stated="$(sed -n 's/^checks run: \([0-9][0-9]*\)$/\1/p' "$_log" | tail -1)"
	if [ -z "$_stated" ]; then
		echo "    records=$_records but the log never stated a count, so it did not reach its summary"
		return 1
	fi
	if [ "$_records" != "$_stated" ]; then
		echo "    records=$_records but the log states checks run: $_stated"
		# NAME THE CAUSE, not just the arithmetic. The two directions have
		# different causes and a reader who has not met either has no route from
		# a pair of numbers to the defect. Raised by OffgridwithJD.
		if [ "$_records" -gt "$_stated" ]; then
			echo "      $((_records - _stated)) check(s) reported an outcome the count never saw:"
			echo "      a check ran in a subshell, so its counter bump died with it while its"
			echo "      outcome and record still reached the log. The usual shape is a check"
			# The example is ASSEMBLED, not written out: spelling the shape here
			# made the sweep in selftest 400 flag this very line.
			printf '      inside a piped loop -- `cmd %s while read x; do check ...; done`.\n' '|'
		else
			echo "      $((_stated - _records)) check(s) were counted without emitting a record:"
			echo "      something bumped PGC_CHECKS without going through pgc_record."
		fi
		return 1
	fi
	return 0
}

# Which suites accounted by a mechanism of their own rather than by lib.sh's.
#
# A FUNCTION so an arm can drive it, for the reason `pgc_reconcile_records` is one: a
# set difference computed inline can only be tested by re-implementing it, and a test
# that re-implements its subject agrees with it by construction.
#
# The NARROW file holds the suites whose log carries lib.sh's `accounting:` line; the
# WIDE file holds those accounted by any mechanism, which also accepts a suite printing
# its own `checks run:`. The difference is therefore the suites with a private tally --
# a different mechanism, not a debt, and the thing that made one report give two answers
# to the same question (#928).
pgc_own_mechanism_suites() {	# pgc_own_mechanism_suites NARROWFILE WIDEFILE -> names
	comm -13 <(sort "$1") <(sort "$2")
}

pgc_log_shows_any_accounting() {	# pgc_log_shows_any_accounting LOGFILE -> yes|no
	# Did this suite count its checks AT RUNTIME, by any mechanism the log shows?
	#
	# Two exist in the tree. lib.sh's pgc_summary prints the accounting line, and
	# 239 suites use it. bench_guards and docs_style keep private counters and
	# print their own `checks run: N`; they never source lib.sh, so the first
	# reader cannot see them, and calling them unaccounted would be false.
	#
	# Both are runtime-observable and derived rather than declared, so a suite
	# that adopts either mechanism leaves the debt bucket on its own -- which is
	# the property that keeps the debt file from becoming a permission slip.
	# A HALF-MERGE WOULD BE CONFUSING RATHER THAN LOUD, and it is worth knowing
	# which way. The `checks run:` alternative below answers YES to an accounting
	# line of ANY shape, so it MASKS a change to that line: if the producer ever
	# moved without this file, the population reconciliation would stay green
	# while pgc_log_shows_accounting broke and the accounting reconciliation
	# reddened. Two checks disagreeing about the same log is a worse signal than
	# either failing.
	#
	# It cannot happen inside one tree -- producer and both readers move in the
	# same commit -- so this is a note about what to look for, not a defect.
	# Raised by OffgridwithJD while verifying the four-term shape change.
	local _log="$1"
	[ -f "$_log" ] || { echo no; return 0; }
	if [ "$(grep -cE '^accounting: [0-9]+ passed \+ [0-9]+ failed \+ [0-9]+ unrunnable \+ [0-9]+ skipped = [0-9]+$' "$_log" || true)" -ne 0 ] \
		|| [ "$(grep -cE '^checks run: [0-9]+$' "$_log" || true)" -ne 0 ]; then
		echo yes
	else
		echo no
	fi
}

pgc_reconcile_population() {	# pgc_reconcile_population REGISTERED ACCOUNTED NOTDISPATCHED DEBT -> 0 ok, 1 unaccounted
	# THE REGISTERED SET IS AN INPUT. pgc_reconcile_accounting reconciles the
	# declared set against the observed one, and both are derived from the suites
	# themselves -- so a registered suite in NEITHER is outside the universe it
	# reconciles. Driven from that function with all its inputs empty: it prints
	# `inputs=0 | both=0 ... sum=0` and returns 0, whatever SUITES holds.
	#
	# Reported by @linuxhikerpm, structurally: treating absence of a declaration
	# as absence from the population preserves the overcount this change is named
	# for. So the population is reconciled separately, over its own four buckets,
	# and every registered suite must land in exactly one:
	#
	#   accounted        its log shows it counted its checks, by either mechanism
	#   not dispatched   the driver recorded that it never ran it
	#   known debt       named in the tracked debt file
	#   unaccounted      none of the above -- FAILS, by name
	#
	# The debt file is DEBT, not an exemption: it is tracked, so adding a name is
	# a diff a reviewer sees, and a name that starts accounting or stops being
	# registered is reported so the burn-down cannot stall silently.
	local _reg="$1" _acct="$2" _nd="${3:-}" _debt="${4:-}" _rc=0
	local _rf _af _ndf _df _unacc _stale_acct _stale_reg _n
	local _nreg _nacc _nnd _ndebt _nunacc _sum

	_rf="$(mktemp)"; _af="$(mktemp)"; _ndf="$(mktemp)"; _df="$(mktemp)"
	LC_ALL=C sort -u "$_reg" 2>/dev/null | sed '/^$/d' >"$_rf"
	LC_ALL=C sort -u "$_acct" 2>/dev/null | sed '/^$/d' >"$_af"
	[ -n "$_nd" ] && [ -f "$_nd" ] && LC_ALL=C sort -u "$_nd" | sed '/^$/d' >"$_ndf"
	[ -n "$_debt" ] && [ -f "$_debt" ] && \
		grep -vE '^[[:space:]]*(#|$)' "$_debt" | LC_ALL=C sort -u >"$_df"

	# Buckets, in precedence order, so each registered name lands in exactly one.
	local _t1 _t2
	_t1="$(mktemp)"; _t2="$(mktemp)"
	LC_ALL=C comm -23 "$_rf" "$_af"  >"$_t1"          # registered, not accounted
	LC_ALL=C comm -23 "$_t1" "$_ndf" >"$_t2"          # ... nor not-dispatched
	_unacc="$(LC_ALL=C comm -23 "$_t2" "$_df")"       # ... nor known debt

	_nreg="$(grep -c . "$_rf" || true)"
	_nacc="$(LC_ALL=C comm -12 "$_rf" "$_af" | grep -c . || true)"
	_nnd="$(LC_ALL=C comm -12 "$_t1" "$_ndf" | grep -c . || true)"
	_ndebt="$(LC_ALL=C comm -12 "$_t2" "$_df" | grep -c . || true)"
	_nunacc="$(printf '%s' "$_unacc" | grep -c . || true)"
	_sum=$(( _nacc + _nnd + _ndebt + _nunacc ))

	# Debt that is no longer debt. Reported rather than fatal: a burn-down that
	# reddens the gate the moment someone FIXES something teaches people not to.
	_stale_acct="$(LC_ALL=C comm -12 "$_df" "$_af")"
	_stale_reg="$(LC_ALL=C comm -23 "$_df" "$_rf")"
	rm -f "$_rf" "$_af" "$_ndf" "$_df" "$_t1" "$_t2"

	if [ "$_nunacc" != 0 ]; then
		while IFS= read -r _n; do
			[ -n "$_n" ] && echo "    registered but accounted by nothing: $_n"
		done <<<"$_unacc"
		_rc=1
	fi
	while IFS= read -r _n; do
		[ -n "$_n" ] && echo "    listed as debt but now accounts: $_n"
	done <<<"$_stale_acct"
	while IFS= read -r _n; do
		[ -n "$_n" ] && echo "    listed as debt but not registered: $_n"
	done <<<"$_stale_reg"

	# registered == sum(buckets), printed beside every reconciliation per the
	# house rule. BE PRECISE ABOUT WHAT IT CAN CATCH, because the next reader will
	# go looking for a data case and there is not one: the four buckets are built
	# by successive subtraction FROM the registered set, so their sum equals it
	# identically. OffgridwithJD measured it -- 400 random four-set inputs, zero
	# firings, while the real bucket findings fired on 353 of them.
	#
	# What can make it false is comm being fed unsorted input, which produces
	# buckets that are not a partition at all. It is a comm tripwire, exactly like
	# the one on pgc_reconcile_accounting, and selftest 390 drives it there with
	# that mutation.
	echo "  population reconciliation: registered=$_nreg | accounted=$_nacc, not dispatched=$_nnd, known debt=$_ndebt, unaccounted=$_nunacc | sum=$_sum"
	if [ "$_nreg" != "$_sum" ]; then
		echo "    the population does not add up: $_nreg registered, $_sum in the buckets"
		_rc=1
	fi
	return $_rc
}

pgc_reconcile_accounting() {	# pgc_reconcile_accounting DECLARED OBSERVED [NOTDISPATCHED] -> 0 ok, 1 asymmetric
	# Set equality in both directions. The two directions catch opposite
	# mistakes and neither can stand in for the other:
	#
	#   declared but never accounted   the suite died before reaching its
	#                                  summary. Today rc=0 makes that a PASS.
	#   accounted but never declared   the reading of the source is stale.
	#
	# comm needs both sides sorted under the same collation; selftest 070 is the
	# record of what an unsorted input costs here.
	# The third list is the driver's own record of suites it chose not to
	# dispatch -- PGC_SKIP_TIMING drops four on every CI run. Those suites DO
	# declare accounting and correctly produced none, so without this term the
	# reconciliation goes red for the one reason that is not a defect.
	#
	# It is recorded by the branch that makes the decision, not inferred from the
	# log that branch forges. Inferring it would mean trusting a marker the driver
	# wrote on the suite's behalf, which is the kind of claim this check exists to
	# stop.
	local _decl="$1" _obs="$2" _nd="${3:-}" _rc=0
	local _dfile _ofile _ndfile _obsonly _both _donly _oonly _clash
	local _nboth _ndonly _noonly _nclash _inputs _sum _n

	_dfile="$(mktemp)"; _ofile="$(mktemp)"; _ndfile="$(mktemp)"; _obsonly="$(mktemp)"
	LC_ALL=C sort -u "$_decl" 2>/dev/null | sed '/^$/d' >"$_dfile"
	LC_ALL=C sort -u "$_obs"  2>/dev/null | sed '/^$/d' >"$_obsonly"
	if [ -n "$_nd" ] && [ -f "$_nd" ]; then
		LC_ALL=C sort -u "$_nd" 2>/dev/null | sed '/^$/d' >"$_ndfile"
	fi
	# The observed side is what accounted PLUS what was deliberately not run.
	LC_ALL=C sort -u "$_obsonly" "$_ndfile" | sed '/^$/d' >"$_ofile"

	# A suite cannot both have reached its summary and not have been dispatched.
	# If it is in both lists one of the two readings is wrong, and the union above
	# would hide that by absorbing it.
	_clash="$(LC_ALL=C comm -12 "$_obsonly" "$_ndfile")"
	_nclash="$(printf '%s' "$_clash" | grep -c . || true)"

	_both="$(LC_ALL=C comm -12 "$_dfile" "$_ofile")"
	_donly="$(LC_ALL=C comm -23 "$_dfile" "$_ofile")"
	_oonly="$(LC_ALL=C comm -13 "$_dfile" "$_ofile")"

	_nboth="$(printf '%s' "$_both"  | grep -c . || true)"
	_ndonly="$(printf '%s' "$_donly" | grep -c . || true)"
	_noonly="$(printf '%s' "$_oonly" | grep -c . || true)"

	# inputs is counted from the FILES, independently of the three buckets. A
	# derived total makes the identity below true for any values.
	#
	# BUT BE PRECISE ABOUT WHAT IT CAN CATCH, because the next reader will go
	# looking for a data case and there is not one: for sets, |D u O| always
	# equals |D n O| + |D \ O| + |O \ D|. OffgridwithJD measured it -- 400 random
	# set pairs, zero firings. What can make it false is comm being fed unsorted
	# input, which produces buckets that are not a partition at all. It is a comm
	# tripwire, and selftest 390 drives it with exactly that mutation.
	_inputs="$(LC_ALL=C sort -u "$_dfile" "$_ofile" | grep -c . || true)"
	_sum=$(( _nboth + _ndonly + _noonly ))
	rm -f "$_dfile" "$_ofile" "$_ndfile" "$_obsonly"

	if [ "$_nclash" != 0 ]; then
		while IFS= read -r _n; do
			[ -n "$_n" ] && echo "    both accounted and recorded as never dispatched: $_n"
		done <<<"$_clash"
		_rc=1
	fi

	if [ "$_ndonly" != 0 ]; then
		while IFS= read -r _n; do
			[ -n "$_n" ] && echo "    declared but never accounted: $_n"
		done <<<"$_donly"
		_rc=1
	fi
	if [ "$_noonly" != 0 ]; then
		while IFS= read -r _n; do
			[ -n "$_n" ] && echo "    accounted but never declared: $_n"
		done <<<"$_oonly"
		_rc=1
	fi

	# Printed from the data on every path, green included, per the house rule
	# that any list-derived claim shows inputs == sum(buckets).
	echo "  accounting reconciliation: inputs=$_inputs | both=$_nboth, declared only=$_ndonly, accounted only=$_noonly | sum=$_sum"
	if [ "$_inputs" != "$_sum" ]; then
		echo "    the reconciliation does not add up: $_inputs names across both lists, $_sum in the buckets"
		_rc=1
	fi
	return $_rc
}

pgc_tally_suite() {	# pgc_tally_suite NAME VERDICT LOGFILE
	local _name="$1" _verdict="$2" _log="$3"
	if [ "$_verdict" = PASS ]; then
		echo "  PASS  $_name"
		results+="$_name=PASS "
		suites_ran=$((suites_ran + 1))
	elif [ "$_verdict" = INCOMPLETE ]; then
		echo "  INCOMPLETE  $_name (a check could not be evaluated)"
		grep -E '^UNRUN' "$_log" | sed 's/^/      >> /'
		results+="$_name=INCOMPLETE "
		suites_ran=$((suites_ran + 1))
		suites_incomplete=$((suites_incomplete + 1))
		[ "$(pgc_verdict_fails_major "$_verdict")" = yes ] && verfail=1
	elif [ "$_verdict" = SKIP ]; then
		# Exit 66 is pgc_summary's third state: the suite ran no checks (#447).
		# Not a pass, because it asserted nothing. Not a failure, because a
		# major without the feature and a box without an optional dependency
		# are both supported. Counted, so the total below can say so.
		echo "  SKIP  $_name (ran no checks)"
		results+="$_name=SKIP "
		suites_skipped=$((suites_skipped + 1))
		skipped_names="$skipped_names $_name"
	else
		echo "  FAIL  $_name"
		# The failing check first, then the tail. A suite that prints a
		# diagnostic and a server-log dump on failure pushes its own FAIL
		# lines out of a 20-line tail, which is how an intermittent
		# replication failure stayed unreadable across many matrices: the
		# evidence was in the log and the summary showed everything but.
		if grep -qE '^FAIL' "$_log"; then
			grep -E '^FAIL' "$_log" | sed 's/^/      >> /'
		fi
		# 60, not 20: a suite that prints a failure diagnostic and a
		# server-log dump needs more room than 20 lines, and truncating it
		# is how the replication failures stayed unreadable.
		tail -60 "$_log" | sed 's/^/      /'
		results+="$_name=FAIL "
		# A failed suite RAN. Counting only passes here made the tally
		# contradict itself in the one case that matters. The five-major
		# matrix reported
		#
		#     PG19  suites that ran: 121 of 122 (skipped: 0)
		#
		# with temporal failing: 121 + 0 is not 122, and the failing suite was
		# in neither bucket of the count that exists to say what ran. Four
		# majors hid it, because a tally only disagrees with itself once
		# something actually fails.
		suites_ran=$((suites_ran + 1))
		verfail=1
	fi
}

	skipped_names=""
	# Reset, not a `set -u` guard. suites_ran and suites_skipped are zeroed
	# unconditionally above; this line used to read ${suites_incomplete:-0},
	# which KEEPS whatever the previous major left. On a five-major matrix
	# PG16 would report PG15's incomplete suites in its own summary line and
	# still print PASS, because verfail is per major and this count was not.
	suites_incomplete=0
	_rec_bad=0
	for s in "${SUITES[@]}"; do
		_rc="$(cat "$builddir/${s}.rc" 2>/dev/null)"
		_verdict="$(pgc_classify_suite_rc "$_rc" "$builddir/${s}.log")"
		pgc_tally_suite "$s" "$_verdict" "$builddir/${s}.log"
		# Only a suite that reached its summary has a count to reconcile against
		# (#917). One that was never dispatched, or that does not use lib.sh's
		# accounting at all, has nothing to compare and is not a mismatch.
		if [ "$(pgc_log_shows_accounting "$builddir/${s}.log")" = yes ]; then
			if ! pgc_reconcile_records "$builddir/${s}.log"; then
				echo "    in $s"
				_rec_bad=$((_rec_bad + 1))
			fi
		fi
	done
	if [ "$_rec_bad" != 0 ]; then
		echo "  $_rec_bad suite(s) on PG$major state a check count their records do not match"
		verfail=1
	fi

	# How many suites actually asserted something, said out loud (#447).
	#
	# #422 added this one level up, after a matrix reported ALL VERSIONS PASSED
	# having run none of them. The same hole existed per-suite: fifteen suites
	# report a verdict without running a check when pyarrow is absent, and the old
	# per-version line counted them among the passes. A count that includes suites
	# nobody ran is the thing this project keeps having to unlearn.
	# Reconcile what the suites SAY they account for against what this run
	# OBSERVED (#916). Two readings taken from different places -- the suite's own
	# text, and the log it produced -- so neither can satisfy the other by
	# construction. A count over SUITES could not do this: the collect loop above
	# visits every registered name, so any total derived from it is an identity.
	_acc_declared="$builddir/accounting.declared"
	_acc_observed="$builddir/accounting.observed"
	_acc_notdisp="$builddir/accounting.notdispatched"
	: >"$_acc_declared"
	: >"$_acc_observed"
	[ -f "$_acc_notdisp" ] || : >"$_acc_notdisp"
	_acc_absent=0
	for s in "${SUITES[@]}"; do
		_acc_verdict="$(pgc_suite_declares_accounting "$builddir/test/${s}.sh")"
		case "$_acc_verdict" in
			yes)	printf '%s\n' "$s" >>"$_acc_declared" ;;
			no)	;;
			absent)	echo "    registered but has no file: $s.sh"
				_acc_absent=$((_acc_absent + 1)) ;;
			*)
				# A default arm must not quietly name a real outcome. If the
				# reader grows a fourth answer, this says so instead of filing
				# it under "does not declare".
				echo "    pgc_suite_declares_accounting answered [$_acc_verdict] for $s, which is none of yes/no/absent"
				_acc_absent=$((_acc_absent + 1))
				;;
		esac
		[ "$(pgc_log_shows_accounting "$builddir/${s}.log")" = yes ] \
			&& printf '%s\n' "$s" >>"$_acc_observed"
	done
	if [ "$_acc_absent" != 0 ]; then
		echo "  $_acc_absent registered suite(s) on PG$major have no file, which is not a pass"
		verfail=1
	fi
	if ! pgc_reconcile_accounting "$_acc_declared" "$_acc_observed" "$_acc_notdisp"; then
		echo "  PG$major cannot account for every registered suite, which is not a pass"
		verfail=1
	fi

	# THE POPULATION, which the symmetry check above cannot see (#916, reported by
	# @linuxhikerpm). Its inputs are both derived from the suites, so a registered
	# suite in neither is outside the universe it reconciles. Here the registered
	# set IS the input, and a name accounted by nothing fails by name.
	_acc_registered="$builddir/accounting.registered"
	_acc_accounted="$builddir/accounting.accounted"
	printf '%s\n' "${SUITES[@]}" >"$_acc_registered"
	: >"$_acc_accounted"
	for s in "${SUITES[@]}"; do
		[ "$(pgc_log_shows_any_accounting "$builddir/${s}.log")" = yes ] \
			&& printf '%s\n' "$s" >>"$_acc_accounted"
	done
	if ! pgc_reconcile_population "$_acc_registered" "$_acc_accounted" \
		"$_acc_notdisp" "$builddir/test/suites_without_accounting.txt"; then
		echo "  PG$major has a registered suite nothing accounts for, which is not a pass"
		verfail=1
	fi

	# THE LEDGER GATE (#918). Every suite's log is here and the build directory is
	# about to be removed, so this is the only place a matrix run can feed it.
	#
	# The gate runs against the COMMITTED ledger: a check it has never seen is
	# named and refused, which is the allowlist the issue asks for. Regenerating
	# the ledger is the intended fix and a reviewable diff, so this cannot
	# deadlock the way a ceiling on `never` rows did.
	#
	# CI verifies; humans commit. A ledger that CI rewrote by itself would be a
	# file nobody reads changing under everybody.
	#
	# Only logs that CARRY records are passed. The twelve suites outside lib.sh's
	# accounting produce none, and the tool fails closed on an empty input --
	# correctly, since a caller asking it to reconcile nothing is a caller with a
	# bug.
	_led_logs=""
	for s in "${SUITES[@]}"; do
		[ -s "$builddir/${s}.log" ] || continue
		[ "$(grep -c '^RESULT	' "$builddir/${s}.log" || true)" -ne 0 ] \
			&& _led_logs="$_led_logs $builddir/${s}.log"
	done
	if [ -z "$_led_logs" ]; then
		echo "  no suite emitted a check record on PG$major, so the ledger has nothing to gate"
		verfail=1
	elif [ ! -f "$builddir/test/check_ledger.tsv" ]; then
		echo "  the ledger is missing from the tree under test, which is not a pass"
		verfail=1
	else
		# WHICH REF CARRIES THE PRIOR CEILING is resolved by the tool, from
		# GITHUB_BASE_REF in CI or the local main's configured upstream outside
		# it, and it FAILS CLOSED when neither gives a trustworthy answer.
		#
		# It used to be chosen here, preferring origin/main with a printed
		# fallback to HEAD. Both halves were wrong. `origin` is per-clone -- in a
		# contributor's setup it is their fork, measured 446 commits stale -- and
		# comparing against an older main makes the check WEAKER rather than
		# falsely red, because the ceiling may only fall. And the fallback to HEAD
		# compares a committed file against itself, so it caught nothing for any
		# change under review while printing that it had compared. A gate that
		# quietly enforces less than it claims is what this whole change refuses.
		# shellcheck disable=SC2086
		python3 "$builddir/test/pgc_ledger.py" gate \
			--ledger "$builddir/test/check_ledger.tsv" \
			--budget "$builddir/test/check_ledger_budget.txt" \
			--registered "$_acc_registered" \
			--against auto \
			$_led_logs
		_led_rc=$?
		# BRANCH ON THE STATUS THE TOOL WENT TO THE TROUBLE OF DISTINGUISHING.
		# rc=1 is a real refusal and the fix is to regenerate the ledger; rc=2 is
		# the gate unable to do its job at all, where regenerating helps nothing.
		# Collapsing them printed "has a check the ledger has never seen" three
		# lines below the gate's own "new this run=0", which contradicts it and
		# sends the reader at the wrong repair. Reported by OffgridwithJD.
		case "$_led_rc" in
			0)	;;
			1)	echo "  PG$major has a check the ledger has never seen, which is not a pass"
				verfail=1 ;;
			*)	echo "  PG$major could not run the ledger gate at all, which is not a pass:"
				echo "  the input or the prior ceiling was unusable, and regenerating the"
				echo "  ledger will not help. The failure above says which."
				verfail=1 ;;
		esac
	fi

	# How many of the suites counted as having RUN actually accounted for their
	# checks (#916). Counting a suite that never accounted among the suites that ran
	# is the overcount #447 added this line to stop, one level further down, and
	# printing the total without this breakdown leaves it exactly where it was.
	# Raised by OffgridwithJD, who was right that the drift detector alone does not
	# close it.
	#
	# ONE READER FOR THE HEADLINE, because two readers gave one report two answers.
	# This line derived from `_acc_observed`, built with the NARROW reader, while the
	# population reconciliation three lines above counts `_acc_accounted`, built with
	# the WIDE one -- so a single PG 17 matrix report said `accounted=237` and then
	# `of those, 235 accounted for their checks and 7 did not`, three lines apart,
	# differing by exactly 2 (#928). The figure a reader acts on is the second,
	# because it is the one phrased as a problem, and it overstated the debt.
	#
	# THE GAP IS A DIFFERENT MECHANISM, NOT A DEBT. The wide reader also accepts a
	# suite that prints its own `checks run:` line. Measured on this tree: twelve
	# registered suites never call `pgc_summary`, and of those exactly two --
	# `bench_guards` and `docs_style` -- emit a tally of their own, which is the 2.
	# The other ten keep no tally at all and are the real debt.
	#
	# THE NAMES ARE PRINTED, NOT COUNTED, so nothing here can go stale: the two are
	# named by the run rather than by this comment, and a third adopting its own
	# mechanism appears without anyone editing a number. That is the rule this
	# directory learned from nine collisions on one written count in a day.
	_acc_ran="$(grep -c . "$_acc_observed" 2>/dev/null || true)"
	_acc_any="$(grep -c . "$_acc_accounted" 2>/dev/null || true)"
	_acc_own="$(pgc_own_mechanism_suites "$_acc_observed" "$_acc_accounted" | tr '\n' ' ')"
	echo "  suites that ran: $suites_ran of ${#SUITES[@]} (skipped: $suites_skipped, incomplete: $suites_incomplete)"
	echo "  of those, $_acc_any accounted for their checks and $((suites_ran - _acc_any)) did not"
	if [ -n "${_acc_own// /}" ]; then
		# SAY WHICH LINE, because "by their own mechanism" was read twice in one night
		# as "emits no RESULT records" and produced a wrong planning number from it.
		# This set is the suites whose log lacks lib.sh's `accounting:` line and has
		# their own `checks run:` instead. That is a statement about the ACCOUNTING
		# LINE and nothing else: measured over all twelve of them, ten emit RESULT
		# records perfectly well (audit 31, phase4 38, unique_conc 31, ...) and only
		# bench_guards and docs_style emit none -- which is the pair the comment on
		# pgc_log_shows_any_accounting already names, for the real reason: those two
		# never source lib.sh at all.
		echo "    $_acc_ran printed lib.sh's accounting line; these printed their own \`checks run:\` instead (a different accounting LINE, not a missing RESULT record):${_acc_own% }"
	fi
	if [ "$suites_skipped" != 0 ]; then
		echo "  skipped:${skipped_names}"
	fi
	if [ "$suites_ran" = 0 ]; then
		echo "  NO SUITES RAN on PG$major, which is not a pass"
		verfail=1
	fi

	if [ "$verfail" = 0 ]; then
		SUMMARY+=("PASS   PG$major  ($suites_ran ran, $suites_skipped skipped, $suites_incomplete incomplete)  ${results}")
	else
		SUMMARY+=("FAIL   PG$major  ($suites_ran ran, $suites_skipped skipped, $suites_incomplete incomplete)  ${results}")
		overall=1
	fi
	# KEEP THE LOGS, THEN DELETE THE BUILD DIRECTORY. The gate runs above and the
	# rm runs here, and CI's "Collect logs on failure" step globs
	# /tmp/pgcolumnar-matrix-*/*.log AFTER this loop has finished -- so it searched
	# a directory this line had already removed and collected nothing. Run
	# 34503924812 is the measurement. That mattered beyond diagnosis: the ledger is
	# fed by merging real logs, and a CI red is exactly the run whose logs record a
	# check going red for the first time. Deleting them meant CI could never feed
	# the thing it gates. Reported by @linuxhikerpm.
	#
	# Copied rather than left in place, because the build directory is large and
	# the logs are not, and because a retained path that does not move is what a
	# workflow step can name.
	_logkeep="${PGC_LOG_KEEP:-/tmp/pgcolumnar-logs}"
	mkdir -p "$_logkeep"
	for _l in "$builddir"/*.log; do
		[ -e "$_l" ] || continue
		cp -p "$_l" "$_logkeep/pg${major}-${_l##*/}"
	done
	rm -rf "$builddir"
done

# ---------------------------------------------------------------------------
# Cross-major upgrade (opt-in: PGC_RUN_UPGRADE=1)
# ---------------------------------------------------------------------------
#
# Off by default and deliberately not part of the per-PR gate. pg_upgrade needs
# two majors at once, runs the upgrade twice per pair (copy and link), and is
# expensive -- the same reason run_san.sh sits beside the matrix rather than in
# it.
#
# It is here rather than only in a checklist because docs/limitations.md now makes
# a cross-major claim -- that data written by one build reads back identically on
# any build of the same format version, across every supported major -- and
# test/pg_upgrade.sh is the only thing that tests it. A claim backed by a suite
# nobody runs is the failure mode that left native_scale dark (#257).
#
# Adjacent pairs of the majors being tested, both transfer modes: link shares the
# data files with the old cluster and copy does not, and they fail differently.
if [ "${PGC_RUN_UPGRADE:-0}" = 1 ]; then
	echo "==================================================================="
	echo "== cross-major upgrade (PGC_RUN_UPGRADE=1)"
	echo "==================================================================="

	_ucount=0
	for _i in $(seq 0 $(( ${#CONFIGS[@]} - 2 ))); do
		_old="${CONFIGS[$_i]}"
		_new="${CONFIGS[$((_i + 1))]}"
		[ -x "$_old" ] && [ -x "$_new" ] || continue
		_omaj="$("$_old" --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"
		_nmaj="$("$_new" --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"
		for _mode in copy link; do
			_ucount=$((_ucount + 1))
			_ulog="$(mktemp "/tmp/pgcolumnar-upgrade-${_omaj}-${_nmaj}-${_mode}.XXXXXX.log")"
			if bash "$SRCDIR/test/pg_upgrade.sh" "$_old" "$_new" "$_mode" \
				>"$_ulog" 2>&1; then
				echo "  PASS  pg_upgrade PG$_omaj -> PG$_nmaj ($_mode)"
				SUMMARY+=("PASS   upgrade PG$_omaj->PG$_nmaj ($_mode)")
				rm -f "$_ulog"
			else
				echo "  FAIL  pg_upgrade PG$_omaj -> PG$_nmaj ($_mode)"
				if grep -qE '^FAIL' "$_ulog"; then
					grep -E '^FAIL' "$_ulog" | sed 's/^/      >> /'
				fi
				tail -40 "$_ulog" | sed 's/^/      /'
				echo "      full log: $_ulog"
				SUMMARY+=("FAIL   upgrade PG$_omaj->PG$_nmaj ($_mode)")
				overall=1
			fi
		done
	done

	# Asked for the gate and got nothing: that is a failure, not a quiet pass.
	# One pg_config short of a pair, or a typo in the list, would otherwise report
	# green having upgraded nothing.
	if [ "$_ucount" = 0 ]; then
		echo "  FAIL  PGC_RUN_UPGRADE=1 but no adjacent pair of majors was runnable"
		SUMMARY+=("FAIL   upgrade (no runnable pair)")
		overall=1
	fi

	# Extension upgrade, on the same opt-in switch and for the same reason.
	# test/extension_upgrade.sh had pg_upgrade's exemption from the registration
	# check without pg_upgrade's invocation, so nothing ran it (#396). A guard
	# nobody runs is the failure mode #257 existed to close, and it matters more
	# here: the break it catches is invisible until a user upgrades.
	#
	# One major is enough, so this runs once against the first config rather than
	# per pair. It builds the previous release from a throwaway clone, so it needs
	# a checkout with tags.
	_ex="${CONFIGS[0]}"
	if [ -x "$_ex" ]; then
		_exmaj="$("$_ex" --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"
		_exlog="$(mktemp "/tmp/pgcolumnar-extupgrade-${_exmaj}.XXXXXX.log")"
		# The suite builds a previous release, so it has to be told where to find one.
		# PGC_UPGRADE_OLD_SRC carries a directory through, which is the only form that
		# works where the tree has no .git, and the documented container loop is exactly
		# that. Without it the suite falls back to a ref and cannot build one there.
		bash "$SRCDIR/test/extension_upgrade.sh" "$_ex" \
			${PGC_UPGRADE_OLD_SRC:+"$PGC_UPGRADE_OLD_SRC"} >"$_exlog" 2>&1
		_exrc=$?
		# Exit 2 is "the environment could not supply an old source", which is not a
		# product failure. It is reported as SKIP and not as PASS, because a gate that
		# reports green having run nothing is the defect this suite was written for.
		if [ "$_exrc" = 0 ]; then
			echo "  PASS  extension_upgrade PG$_exmaj"
			SUMMARY+=("PASS   extension_upgrade PG$_exmaj")
			rm -f "$_exlog"
		elif [ "$_exrc" = 2 ]; then
			echo "  SKIP  extension_upgrade PG$_exmaj"
			grep -E '^\s*(SKIP|  )' "$_exlog" | sed 's/^/      /'
			SUMMARY+=("SKIP   extension_upgrade PG$_exmaj")
			rm -f "$_exlog"
		else
			echo "  FAIL  extension_upgrade PG$_exmaj"
			grep -E '^\s*FAIL' "$_exlog" | sed 's/^/      >> /'
			tail -30 "$_exlog" | sed 's/^/      /'
			echo "      full log: $_exlog"
			SUMMARY+=("FAIL   extension_upgrade PG$_exmaj")
			overall=1
		fi
	else
		echo "  FAIL  PGC_RUN_UPGRADE=1 but no runnable pg_config for extension_upgrade"
		SUMMARY+=("FAIL   extension_upgrade (no runnable pg_config)")
		overall=1
	fi
fi

echo
echo "===================== MATRIX SUMMARY ============================"
for line in "${SUMMARY[@]}"; do
	echo "  $line"
done
echo "  versions run: $VERSIONS_RUN of ${#CONFIGS[@]} configured"
echo "================================================================"

# A run that built nothing is not a pass (#418).
#
# The default list names /usr/local/pg15 through /usr/local/pg19. On a box whose
# assert builds are pg15a through pg19a, every entry misses, each prints one
# SKIP line, and this block used to print ALL VERSIONS PASSED and exit 0. That
# output then gets pasted into a pull request as the gate. It is the same defect
# as check "" "" one level up, and it is worse, because this is the line people
# read instead of the checks.
#
# Reported rather than merely counted, because the count is what nobody looks at.
if [ "$VERSIONS_RUN" = 0 ]; then
	echo "NO VERSIONS RAN: every configured pg_config was missing or not executable."
	echo "  configured: ${CONFIGS[*]}"
	echo "  Pass the pg_configs this box has, e.g. test/run_all_versions.sh /usr/local/pg18a/bin/pg_config"
	exit 1
fi
if [ "$overall" = 0 ]; then
	if [ "$VERSIONS_RUN" -lt "${#CONFIGS[@]}" ]; then
		echo "VERSIONS RUN PASSED ($VERSIONS_RUN of ${#CONFIGS[@]}; the rest were skipped)"
	else
		echo "ALL VERSIONS PASSED"
	fi
else
	echo "SOME VERSIONS FAILED"
fi
exit "$overall"

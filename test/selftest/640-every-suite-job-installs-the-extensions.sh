# ---- every job that runs the suites installs the same extension packages -----
#
# Suites such as partman_columnar and approx_aggregates need a PGDG extension
# package (postgresql-N-partman, -hll, -tdigest). Without it they record a FAIL,
# not a pass (#1215). #1291 and #1296 added those packages to the per-PR jobs and
# to the nightly's suites and pytest jobs, but not to the nightly's coverage job,
# which runs the same suites under run_coverage.sh. The nightly went red (#1382).
#
# So the list is derived, not trusted: every job that runs the suites must
# install every extension package any such job installs. A job runs the suites
# when it calls run_all_versions.sh or run_coverage.sh, or runs pytest against a
# cluster (a job whose key names a cluster). Comment lines are skipped: two jobs
# that run no suite mention run_all_versions.sh in their comments.

# _ej_scan <workflow>: one line per suite-running job, "job pkg pkg ...", where
# each pkg is the suffix of postgresql-<major>-<suffix> on the job's lines.
_ej_scan() {
	awk '
	/^jobs:/ { inj = 1; next }
	inj && /^  [a-zA-Z_-]+:[[:space:]]*$/ { flush(); job = $1; sub(/:$/, "", job); runs = 0; delete pk; next }
	inj && job != "" && $0 !~ /^[[:space:]]*#/ {
		if ($0 ~ /run_all_versions\.sh|run_coverage\.sh/ || ($0 ~ /\/pytest -q/ && job ~ /cluster/)) runs = 1
		line = $0
		while (match(line, /postgresql-(\$\{\{ matrix\.pg \}\}|[0-9]+)-[a-z0-9]+/)) {
			p = substr(line, RSTART, RLENGTH); sub(/.*-/, "", p); pk[p] = 1
			line = substr(line, RSTART + RLENGTH)
		}
	}
	function flush(  s, p) {
		if (job != "" && runs) { s = job; for (p in pk) s = s " " p; print s }
	}
	END { flush() }' "$1"
}

# _ej_missing <workflow>...: "file:job:pkg" for each package a suite job lacks, and
# "file:unreadable" or "file:no-suite-job" for a workflow it could not compare, so
# an empty answer can only mean nothing is missing.
_ej_missing() {
	local f all
	all="$(for f in "$@"; do [ -r "$f" ] && _ej_scan "$f" | awk '{ $1 = ""; print }' | tr ' ' '\n'; done | grep . | sort -u)"
	for f in "$@"; do
		if [ ! -r "$f" ]; then echo "${f##*/}:unreadable"; continue; fi
		[ -n "$(_ej_scan "$f")" ] || echo "${f##*/}:no-suite-job"
		_ej_scan "$f" | while read -r job pkgs; do
			for p in $all; do
				case " $pkgs " in *" $p "*) ;; *) echo "${f##*/}:$job:$p" ;; esac
			done
		done
	done | sort | tr '\n' ' ' | sed 's/ $//'
}

_ej_ci="$TESTDIR/../.github/workflows/ci.yml"
_ej_nl="$TESTDIR/../.github/workflows/nightly.yml"

check "premise: both workflows are present" \
	"$([ -r "$_ej_ci" ] && [ -r "$_ej_nl" ] && echo yes || echo no)" "yes"

_ej_jobs="$(for f in "$_ej_ci" "$_ej_nl"; do _ej_scan "$f" | sed "s|^|${f##*/}:|"; done)"
echo "-- suite jobs and their extension packages:"
printf '%s\n' "$_ej_jobs" | sed 's/^/     /'

# The detector proven on a workflow made to lack one package, so the real check
# below cannot pass by finding nothing to compare.
_ej_tmp="$(mktemp -d)"
cat >"$_ej_tmp/w.yml" <<'EOF'
jobs:
  a:
    steps:
      - run: apt-get install postgresql-${{ matrix.pg }}-partman postgresql-${{ matrix.pg }}-hll
      - run: bash test/run_all_versions.sh x
  b:
    steps:
      - run: apt-get install postgresql-18 postgresql-server-dev-18 postgresql-18-partman
      - run: bash test/run_coverage.sh x
  c:
    steps:
      # c runs no suite, though this comment names run_all_versions.sh
      - run: apt-get install postgresql-18
EOF
check "the detector names a suite job that lacks a package the others install" \
	"$(_ej_missing "$_ej_tmp/w.yml")" "w.yml:b:hll"

# An empty answer must mean "nothing missing", never "nothing read" (#1385 review):
# a workflow the detector cannot read, or in which it finds no suite job, is named.
sed -n '/^  c:/,$p' "$_ej_tmp/w.yml" | sed '1i jobs:' >"$_ej_tmp/nosuite.yml"
check "the detector names a workflow it cannot read" \
	"$(_ej_missing "$_ej_tmp/absent.yml")" "absent.yml:unreadable"
check "the detector names a workflow in which it finds no suite job" \
	"$(_ej_missing "$_ej_tmp/nosuite.yml")" "nosuite.yml:no-suite-job"
rm -rf "${_ej_tmp:?}"

# Not a premise: renaming, adding or deleting a job reddens it (#1236). Named as a
# set, so a cluster job renamed without "cluster" in its key cannot drop out quietly.
check "the suite-running jobs are exactly the five this part knows" \
	"$(printf '%s\n' "$_ej_jobs" | awk '{ print $1 }' | sort | tr '\n' ' ' | sed 's/ $//')" \
	"ci.yml:pytest-cluster ci.yml:suites nightly.yml:coverage nightly.yml:pytest-cluster nightly.yml:suites"

check "every job that runs the suites installs every extension package (#1382)" \
	"$(_ej_m="$(_ej_missing "$_ej_ci" "$_ej_nl")"; echo "${_ej_m:-none}")" "none"

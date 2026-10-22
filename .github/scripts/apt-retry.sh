#!/usr/bin/env bash
# apt-retry.sh SECONDS ATTEMPTS APT-GET-ARGS...
#
# Run `sudo apt-get APT-GET-ARGS` up to ATTEMPTS times, each attempt bounded by
# SECONDS. A stalled package mirror used to spend a job's whole single 600-second
# attempt and fail it with exit 124 before any test ran (#1729; most of the CI reds
# on 2026-10-22). Now a stalled attempt is cut short and retried, the worst case
# stays ATTEMPTS * SECONDS, and a connection that sends nothing for 30 seconds
# fails at once instead of holding the attempt. Partial downloads are kept, so a
# retry resumes them.
set -uo pipefail
secs="$1" tries="$2"
shift 2
for i in $(seq 1 "$tries"); do
	if sudo timeout "$secs" apt-get -o Acquire::Retries=3 \
		-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"; then
		exit 0
	fi
	echo "apt-get $1: attempt $i of $tries failed or ran past ${secs}s" >&2
done
exit 1

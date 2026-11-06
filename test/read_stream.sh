#!/usr/bin/env bash
#
# pgColumnar read stream / prefetch suite (gap 29).
#
# The columnar block reads go through the read stream API on PostgreSQL 17+
# (pgcolumnar.enable_read_stream, on by default), which must return byte-identical
# data to the synchronous ReadBuffer path. This runs a battery of scan shapes
# against a heap oracle with the stream on and off, over a multi-stripe table so
# the streamed block range spans many blocks. On PostgreSQL 13-16 the streaming
# path is compiled out and the GUC has no effect; both modes still match.
#
# Usage:  test/read_stream.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Small stripes and wide-ish rows so each stripe spans many blocks.
make_pair "id int, k int, v bigint, t text"
q "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 2000, chunk_group_row_limit => 1000);" >/dev/null
load_pair "SELECT g, g%100, g::bigint*3, repeat('x', (g%40)+1) FROM generate_series(1,50000) g"
NSTRIPES="$(stripe_count t_col)"
check "multiple stripes present" "$([ "${NSTRIPES:-0}" -ge 10 ] 2>/dev/null && echo yes || echo "no ($NSTRIPES)")" "yes"

setg() { q "ALTER DATABASE $PGC_DB SET $1 = $2;" >/dev/null; }

# Each mode by literal name (#1341). ALTER DATABASE SET reaches every new session,
# and q drops its error, so a setting that never took would run both modes down the
# same path: each mode first reads the setting back from a fresh session.
# The read-back proves the setting reached the session, not which path a read took,
# and nothing here observes the path: with the stream path taken whatever the setting
# says and the synchronous path copying one byte late, all 13 checks pass. So the
# [off] arms vouch for the synchronous path only while the setting routes reads. On
# PostgreSQL 15 and 16 the stream path is compiled out, and [on] repeats [off].
setg pgcolumnar.enable_read_stream on
check "[on] a fresh session reads enable_read_stream as on" "$(q "SHOW pgcolumnar.enable_read_stream;")" "on"
diff_query "[on] full scan" "SELECT id, k, v, t FROM %T"
diff_query "[on] filtered" "SELECT id, v FROM %T WHERE k = 7"
diff_query "[on] range" "SELECT id, t FROM %T WHERE id BETWEEN 10000 AND 20000"
diff_query "[on] aggregate" "SELECT k, count(*), sum(v) FROM %T GROUP BY k"
diff_query "[on] wide sample" "SELECT * FROM %T WHERE id % 997 = 0"
setg pgcolumnar.enable_read_stream off
check "[off] a fresh session reads enable_read_stream as off" "$(q "SHOW pgcolumnar.enable_read_stream;")" "off"
diff_query "[off] full scan" "SELECT id, k, v, t FROM %T"
diff_query "[off] filtered" "SELECT id, v FROM %T WHERE k = 7"
diff_query "[off] range" "SELECT id, t FROM %T WHERE id BETWEEN 10000 AND 20000"
diff_query "[off] aggregate" "SELECT k, count(*), sum(v) FROM %T GROUP BY k"
diff_query "[off] wide sample" "SELECT * FROM %T WHERE id % 997 = 0"

setg pgcolumnar.enable_read_stream on
q "ALTER DATABASE $PGC_DB RESET ALL;" >/dev/null

pgc_summary

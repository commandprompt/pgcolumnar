#!/usr/bin/env bash
#
# A Parquet timestamp read into the other PostgreSQL timestamp type follows
# PostgreSQL's cast semantics (#1706 item 2).
#
# Parquet marks each timestamp column isAdjustedToUTC: true for an instant,
# false for a wall time with no zone. The reader ignored the flag and returned
# the raw microseconds for either target type, so two of the four cases
# disagreed with PostgreSQL, in a session whose zone is not UTC:
#
#     file       read as        before           now, as PostgreSQL casts
#     naive      timestamptz    taken as UTC     the wall time in the session zone
#     adjusted   timestamp      the UTC wall     the session-zone wall time
#
# The same-kind reads (naive into timestamp, adjusted into timestamptz) are
# unchanged, and are the controls.
#
# A WALL TIME IS NOT MONOTONIC IN ITS INSTANT across a spring-forward gap:
# PostgreSQL reads the nonexistent 02:30 as standard time, so in New York on
# 2024-03-10 the wall times 02:00, 02:30, 03:00 become 07:00Z, 07:30Z, 07:00Z.
# A row group's converted min and max then need not bound its converted rows,
# so row-group skipping must not use them. The last arm reads such a group
# through the foreign table with a qual only the 02:30 row meets.
#
# The other arms: the naive file read again in a second zone (Tokyo), so a reader
# that hard-codes a zone fails; the repeated hour of New York's 2024-11-03
# fall-back, which must resolve as PostgreSQL's cast resolves it; and a file
# whose timestamp carries only the legacy TIMESTAMP_MICROS converted type, no
# logical type. The Parquet spec defines that type as UTC-adjusted. The fixture
# test/fixtures/parquet/legacy_timestamp_micros.parquet is real: pgcolumnar's own
# export_parquet before #1707 (main at 0c5e924d) wrote it, from one timestamp
# column holding 2024-06-01 12:00, so the file holds the instant 12:00Z.
#
# Usage:  test/parquet_timestamp_zone.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; Parquet timestamp zone suite needs it"
fi

W="$PGC_WORKDIR"
# Every session of this suite runs in New York, so each reading below is in that zone.
export PGOPTIONS="-c TimeZone=America/New_York"
psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

python3 - "$W" <<'PY'
import sys, datetime as dt, pyarrow as pa, pyarrow.parquet as pq
w = sys.argv[1]
noon = dt.datetime(2024, 6, 1, 12, 0)
# naive: timestamp[us], isAdjustedToUTC=false; adjusted: tz=UTC, isAdjustedToUTC=true
pq.write_table(pa.table({"t": pa.array([noon], pa.timestamp("us"))}), f"{w}/naive.parquet")
pq.write_table(pa.table({"t": pa.array([noon], pa.timestamp("us", tz="UTC"))}), f"{w}/adjusted.parquet")
# one row group spanning New York's 2024-03-10 spring-forward gap
gap = [dt.datetime(2024, 3, 10, 2, 0), dt.datetime(2024, 3, 10, 2, 30), dt.datetime(2024, 3, 10, 3, 0)]
pq.write_table(pa.table({"t": pa.array(gap, pa.timestamp("us"))}), f"{w}/gap.parquet", row_group_size=3)
# the repeated hour of New York's 2024-11-03 fall-back
fb = [dt.datetime(2024, 11, 3, 1, 30)]
pq.write_table(pa.table({"t": pa.array(fb, pa.timestamp("us"))}), f"{w}/fallback.parquet")
PY
cp "$(dirname "${BASH_SOURCE[0]}")/fixtures/parquet/legacy_timestamp_micros.parquet" "$W/legacy.parquet"
for f in naive adjusted gap fallback legacy; do chmod 644 "$W/$f.parquet"; done

check "premise: pyarrow wrote the naive file without isAdjustedToUTC and the other with it" \
	"$(python3 -c "
import sys, pyarrow.parquet as pq
a = lambda f: pq.ParquetFile(f).schema_arrow.field('t').type.tz
print(a('$W/naive.parquet'), a('$W/adjusted.parquet'))")" "None UTC"
# pyarrow derives a logical type from a converted type and says so in this flag
check "premise: the legacy fixture carries the TIMESTAMP_MICROS converted type and no logical type" \
	"$(python3 -c "
import pyarrow.parquet as pq
c = pq.ParquetFile('$W/legacy.parquet').metadata.schema.column(0)
print(c.converted_type, 'is_from_converted_type=true' in str(c.logical_type))")" "TIMESTAMP_MICROS True"

psql_run "CREATE TABLE n_ts (t timestamp) USING pgcolumnar; CREATE TABLE n_tz (t timestamptz) USING pgcolumnar;
          CREATE TABLE a_ts (t timestamp) USING pgcolumnar; CREATE TABLE a_tz (t timestamptz) USING pgcolumnar;"
for t in n_ts n_tz; do psql_run "SELECT pgcolumnar.import_parquet('$t', '$W/naive.parquet');"; done
for t in a_ts a_tz; do psql_run "SELECT pgcolumnar.import_parquet('$t', '$W/adjusted.parquet');"; done

check_text "a naive file read into timestamptz is its wall time in the session zone" \
	"$(q "SELECT t::text FROM n_tz;")" "2024-06-01 12:00:00-04"
check_text "and that is what PostgreSQL's cast from timestamp gives" \
	"$(q "SELECT (SELECT t FROM n_tz) = '2024-06-01 12:00'::timestamp::timestamptz;")" "t"
check_text "an adjusted file read into timestamp is the session-zone wall time of its instant" \
	"$(q "SELECT t::text FROM a_ts;")" "2024-06-01 08:00:00"
check_text "and that is what PostgreSQL's cast from timestamptz gives" \
	"$(q "SELECT (SELECT t FROM a_ts) = '2024-06-01 12:00+00'::timestamptz::timestamp;")" "t"
check_text "control: a naive file read into timestamp keeps its wall time" \
	"$(q "SELECT t::text FROM n_ts;")" "2024-06-01 12:00:00"
check_text "control: an adjusted file read into timestamptz keeps its instant" \
	"$(q "SELECT t::text FROM a_tz;")" "2024-06-01 08:00:00-04"

# The round trip through pgcolumnar's own export, in the same zone. This holds only
# because the exporter writes timestamp as naive (#1707): with the reader honouring
# the flag and the old exporter marking timestamp adjusted, the timestamp came back
# 08:00, four hours early.
psql_run "CREATE TABLE rt_src (ts timestamp, tz timestamptz) USING pgcolumnar;
          INSERT INTO rt_src VALUES ('2024-06-01 12:00', '2024-06-01 12:00');
          CREATE TABLE rt_dst (ts timestamp, tz timestamptz) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.export_parquet('rt_src', '$W/rt.parquet');"
psql_run "SELECT pgcolumnar.import_parquet('rt_dst', '$W/rt.parquet');"
check_text "an export and import round trip keeps timestamp and timestamptz in a non-UTC session" \
	"$(q "SELECT ts::text || ' | ' || tz::text FROM rt_dst;")" "2024-06-01 12:00:00 | 2024-06-01 12:00:00-04"

psql_run "CREATE FOREIGN TABLE f_gap (t timestamptz) SERVER pq OPTIONS (path '$W/gap.parquet');"
check_text "the gap file's wall times 02:00, 02:30 and 03:00 become, sorted, the instants 07:00Z, 07:00Z and 07:30Z" \
	"$(q "SELECT string_agg(to_char(t AT TIME ZONE 'UTC', 'HH24:MI'), ',' ORDER BY t) FROM f_gap;")" \
	"07:00,07:00,07:30"
check_text "a row group spanning a spring-forward gap is not skipped on its converted min and max" \
	"$(q "SELECT count(*) FROM f_gap WHERE t > '2024-03-10 07:15+00';")" "1"

# A second zone: the same naive file read through a foreign table in Tokyo.
psql_run "CREATE FOREIGN TABLE f_naive (t timestamptz) SERVER pq OPTIONS (path '$W/naive.parquet');"
qtk() { PGOPTIONS="-c TimeZone=Asia/Tokyo" q "$1"; }
check_text "a naive file read into timestamptz in Tokyo is its wall time there, as the cast gives" \
	"$(qtk "SELECT t::text || ' ' || (t = '2024-06-01 12:00'::timestamp::timestamptz)::text FROM f_naive;")" \
	"2024-06-01 12:00:00+09 true"
check_text "and the Tokyo reading is a different instant from the New York one" \
	"$(qtk "SELECT ((SELECT t FROM f_naive) <> (SELECT t FROM n_tz))::text;")" "true"

# The repeated hour: 01:30 happens twice in New York on 2024-11-03.
psql_run "CREATE FOREIGN TABLE f_fb (t timestamptz) SERVER pq OPTIONS (path '$W/fallback.parquet');"
check_text "a naive wall time in the fall-back hour read into timestamptz is what the cast gives" \
	"$(q "SELECT t::text || ' ' || (t = '2024-11-03 01:30'::timestamp::timestamptz)::text FROM f_fb;")" \
	"2024-11-03 01:30:00-05 true"

# The legacy converted type, read as the instant the spec defines it to be.
psql_run "CREATE FOREIGN TABLE f_leg_ts (t timestamp) SERVER pq OPTIONS (path '$W/legacy.parquet');
          CREATE FOREIGN TABLE f_leg_tz (t timestamptz) SERVER pq OPTIONS (path '$W/legacy.parquet');"
check_text "a legacy TIMESTAMP_MICROS file read into timestamp is the session-zone wall time of its instant" \
	"$(q "SELECT t::text || ' ' || (t = '2024-06-01 12:00+00'::timestamptz::timestamp)::text FROM f_leg_ts;")" \
	"2024-06-01 08:00:00 true"
check_text "a legacy TIMESTAMP_MICROS file read into timestamptz keeps its instant" \
	"$(q "SELECT t::text FROM f_leg_tz;")" "2024-06-01 08:00:00-04"

pgc_summary

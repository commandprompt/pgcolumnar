#!/usr/bin/env bash
#
# The reader reuses one row-group buffer instead of allocating one per group
# (#768).
#
# src/columnar_reader.c materializes the whole row group into a single buffer,
# sized stripe_row_limit x row width. At the default stripe_row_limit = 150000
# that is ~19.8 MB for a 128-character text column. Allocated and freed per
# group it is far past ALLOC_CHUNK_LIMIT, so it is its own malloc block, glibc
# hands it back to the kernel on free, and the next group faults every page in
# again.
#
# MEASURED, not asserted. Same cluster, same table, same query, only the
# installed .so changing (md5 printed on both sides), 1,000,000 rows of
# 128-character text at stripe_row_limit = 150000:
#
#     before:  57,846 minor faults per query    143.0 ms
#     after:    4,838 minor faults per query     85.8 ms
#
# THE INSTRUMENT. PostgreSQL reports its own minor faults: log_executor_stats
# prints "0/N [0/M] page faults/reclaims" per statement, and with
# client_min_messages = log it reaches the client. That measures the WORK the
# fix is supposed to remove, rather than the intent of the code that removes it.
#
# THE ARM IS A RATIO, NOT A THRESHOLD. An absolute fault count depends on the
# machine, the page size and how much the rest of the executor churns, so a
# fixed number would be brittle in CI. Instead, read ONE group and then ALL
# groups of the same table. Without reuse the buffer is re-faulted per group and
# the count tracks the group count; with reuse it is allocated once and the
# count barely moves. The plan's own "Columnar Chunk Groups Read" pins that the
# two arms really did read 1 group and N groups.
#
# Usage:  test/reader_buffer_reuse.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

STRIPE=150000
ROWS=600000
OFF="SET pgcolumnar.enable_ungrouped_vector_agg=off; SET pgcolumnar.enable_vectorization=off"

psql_run "CREATE TABLE rb (id int, v text) USING pgcolumnar;"
# encode_effort=fast skips the FSST substring search. Without it the encoder
# behaves differently at different chunk fills and the two arms would not be
# reading comparably encoded bytes.
psql_run "SELECT pgcolumnar.set_options('rb', stripe_row_limit => $STRIPE,
              compression => 'none', encode_effort => 'fast');"
psql_run "INSERT INTO rb SELECT g,
              md5(g::text)||md5((g*7)::text)||md5((g*13)::text)||md5((g*17)::text)
          FROM generate_series(1, $ROWS) g;"

NGROUPS="$(q "SELECT count(*) FROM pgcolumnar.stats('rb');")"
check "premise: the fixture has several row groups, or reuse cannot matter" \
	"$([ "${NGROUPS:-0}" -ge 3 ] && echo yes || echo "no ($NGROUPS)")" "yes"

# Faults reported by the executor for the LAST statement of the session. The
# first execution of a query in a fresh backend faults in a great deal that has
# nothing to do with this buffer (catalogs, the plan, the extension), so every
# arm runs the same statement three times and reads the third.
faults() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -q \
		-c "SET client_min_messages=log;" -c "SET log_executor_stats=on;" -c "$OFF;" \
		-c "$1" -c "$1" -c "$1" 2>&1 \
	| grep -oE '0/[0-9]+ \[0/[0-9]+\] page faults' | tail -1 \
	| grep -oE '^0/[0-9]+' | cut -d/ -f2
}
groups_read() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "$OFF;" -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>&1 \
	| grep -oE 'Columnar Chunk Groups Read: [0-9]+' | grep -oE '[0-9]+$'
}

ONE="SELECT count(v) FROM rb WHERE id <= $STRIPE"
ALL="SELECT count(v) FROM rb"

G_ONE="$(groups_read "$ONE")"
G_ALL="$(groups_read "$ALL")"
check_num "premise: the one-group arm really reads exactly one row group" "$G_ONE" "1"
check_num "premise: the all-groups arm really reads every row group" \
	"$G_ALL" "$NGROUPS"

F_ONE="$(faults "$ONE")"
F_ALL="$(faults "$ALL")"
echo "-- minor faults per query: one group $F_ONE, all $NGROUPS groups $F_ALL"

check "premise: the instrument reports a nonzero fault count at all" \
	"$([ "${F_ONE:-0}" -gt 0 ] && [ "${F_ALL:-0}" -gt 0 ] && echo yes || echo no)" "yes"

# THE ARM. Reading N groups instead of 1 must not cost N times the faults,
# because the buffer that dominates them is allocated once. Without reuse this
# ratio is close to the group count.
RATIO="$(awk -v a="$F_ALL" -v b="$F_ONE" 'BEGIN{ printf "%.2f", (b>0)? a/b : 999 }')"
echo "-- faults(all)/faults(one) = $RATIO with $NGROUPS groups"
check "reading every group does not cost per-group faults (ratio well under the group count)" \
	"$(awk -v r="$RATIO" -v n="$NGROUPS" 'BEGIN{ print (r < 1 + (n-1)*0.5) ? "reused" : "per-group (ratio " r " over " n " groups)" }')" \
	"reused"

# ---- correctness: the reused buffer must not leak one group into the next ----
# This is the risk the change introduces. A region that is not read now holds
# the PREVIOUS group's bytes where it used to hold fresh zeroed pages, so a
# projected read -- which deliberately does not read every column's range -- is
# where a leak would show.
psql_run "CREATE TABLE rb_h (id int, v text);"
# From the generator, not copied from rb: a copy agrees with whatever the reader
# returned, so a full-scan error showed on both sides and passed.
psql_run "INSERT INTO rb_h SELECT g,
              md5(g::text)||md5((g*7)::text)||md5((g*13)::text)||md5((g*17)::text)
          FROM generate_series(1, $ROWS) g;"
check_text "the full scan matches a heap mirror exactly" \
	"$(q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb;")" \
	"$(q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb_h;")"
check_text "a PROJECTED scan (one column of two) matches too" \
	"$(q "SELECT count(*)||'/'||sum(id) FROM rb;")" \
	"$(q "SELECT count(*)||'/'||sum(id) FROM rb_h;")"

# ---- the whole-group read ----------------------------------------------------
# Every read above names its columns, so it takes the projected read, which fills
# only the wanted columns' ranges of the reused buffer. The reader has a second
# path: with no projection it reads the whole group into the reused buffer in one
# call. It takes that path for a whole-row Var, and for every read when
# pgcolumnar.enable_column_projection = off. A whole-group read that stops short
# leaves the previous group's bytes in the tail, so compare both forms with the
# generator-built mirror. A setting that fails to apply errors the session, and
# check_text refuses the empty side.
NOPROJ="-c pgcolumnar.enable_column_projection=off"
WHOLEROW="SELECT count(*)||'/'||md5(string_agg(r::text, ',' ORDER BY (r).id)) FROM"
check_text "the full scan with column projection off (a whole-group read) matches a heap mirror" \
	"$(PGOPTIONS="$NOPROJ" q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb;")" \
	"$(q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb_h;")"
check_text "a whole-row read (a whole-group read) matches a heap mirror" \
	"$(q "$WHOLEROW rb r;")" \
	"$(q "$WHOLEROW rb_h r;")"

# A group SMALLER than the one before it is the case that exposes a stale tail:
# the buffer is not shrunk, so the bytes past this group's end are the previous
# group's. Append a short group and read the whole table again.
psql_run "INSERT INTO rb SELECT g,
              md5(g::text)||md5((g*7)::text)||md5((g*13)::text)||md5((g*17)::text)
          FROM generate_series($((ROWS+1)), $((ROWS+2000))) g;"
psql_run "INSERT INTO rb_h SELECT g,
              md5(g::text)||md5((g*7)::text)||md5((g*13)::text)||md5((g*17)::text)
          FROM generate_series($((ROWS+1)), $((ROWS+2000))) g;"
check "premise: that really added a group smaller than the ones before it" \
	"$(q "SELECT (SELECT min(rowcount) FROM pgcolumnar.stats('rb')) < (SELECT max(rowcount) FROM pgcolumnar.stats('rb'));")" \
	"t"
check_text "a short group after a full one still reads correctly" \
	"$(q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb;")" \
	"$(q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb_h;")"
check_text "a short group after a full one reads correctly with column projection off" \
	"$(PGOPTIONS="$NOPROJ" q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb;")" \
	"$(q "SELECT count(*)||'/'||md5(string_agg(v, ',' ORDER BY id)) FROM rb_h;")"
check_text "a short group after a full one reads correctly in a whole-row read" \
	"$(q "$WHOLEROW rb r;")" \
	"$(q "$WHOLEROW rb_h r;")"

# ---- differential arms: the shapes where a stale read would actually show ----
# These exist because of a finding on the #776 review: the arms above pass
# unchanged when the reused buffer is filled with a poison pattern, so this file
# did not cover the stale-bytes hazard that the code comment names as its safety
# argument.
#
# I tried making the poison permanent under USE_ASSERT_CHECKING and then measured
# whether it earned its place. It does not. With the not-projected skip deleted,
# these arms redden IDENTICALLY with and without the poison -- decoding a chunk
# out of zeroed buffer fails the same way as out of 0xA5. The poison could not be
# shown to make any arm redder, so it was dropped rather than shipped with a
# comment claiming coverage it does not have.
#
# What DOES cover the hazard is the differential below.
#
# What does exercise it is a DIFFERENTIAL against a heap twin over shapes where
# only part of the buffer is filled: columns of different widths read in
# subsets, a column added after the groups already exist, an all-NULL column,
# and a column dropped. Each reads a different subset of each group's bytes, so
# the unread remainder differs per query.
psql_run "CREATE TABLE rbd (i int, s smallint, b bigint, t text, u text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('rbd', stripe_row_limit => 20000, encode_effort => 'fast');"
psql_run "INSERT INTO rbd SELECT g, (g%100)::smallint, g::bigint*1000,
              md5(g::text), repeat(md5((g*7)::text), 3)
          FROM generate_series(1, 120000) g;"
# From the generator too, for the same reason.
psql_run "CREATE TABLE rbd_h AS SELECT g AS i, (g%100)::smallint AS s, g::bigint*1000 AS b,
              md5(g::text) AS t, repeat(md5((g*7)::text), 3) AS u
          FROM generate_series(1, 120000) g;"
RBD_GROUPS="$(q "SELECT count(*) FROM pgcolumnar.stats('rbd');")"
check "premise: the differential fixture spans several row groups" \
	"$([ "${RBD_GROUPS:-0}" -ge 3 ] 2>/dev/null && echo yes || echo "no ($RBD_GROUPS groups)")" "yes"

dq_pair() {   # same projection against columnar and its heap twin
	local proj="$1"
	local a b
	a="$(q "SELECT md5(string_agg(x, ',' ORDER BY x)) FROM (SELECT ($proj)::text x FROM rbd) z;")"
	b="$(q "SELECT md5(string_agg(x, ',' ORDER BY x)) FROM (SELECT ($proj)::text x FROM rbd_h) z;")"
	[ -n "$a" ] && [ "$a" = "$b" ] && echo match || echo "differ($a vs $b)"
}
check "projected subset [i] matches the heap twin" "$(dq_pair "i")" "match"
check "projected subset [s] matches the heap twin" "$(dq_pair "s")" "match"
check "projected subset [b] matches the heap twin" "$(dq_pair "b")" "match"
check "projected subset [t] matches the heap twin" "$(dq_pair "t")" "match"
check "projected subset [u] matches the heap twin" "$(dq_pair "u")" "match"
check "projected subset [i||':'||t] matches the heap twin" "$(dq_pair "i||':'||t")" "match"
check "projected subset [s||':'||u] matches the heap twin" "$(dq_pair "s||':'||u")" "match"
check "projected subset [i||b::text||t||u] matches the heap twin" "$(dq_pair "i||b::text||t||u")" "match"

psql_run "ALTER TABLE rbd ADD COLUMN added int;"
psql_run "ALTER TABLE rbd_h ADD COLUMN added int;"
check "a column ADDED after the groups exist reads back NULL, as on heap" \
	"$(dq_pair "coalesce(added::text,'N')||'|'||t")" "match"
psql_run "ALTER TABLE rbd ADD COLUMN allnull text;"
psql_run "ALTER TABLE rbd_h ADD COLUMN allnull text;"
psql_run "UPDATE rbd SET allnull = NULL WHERE i <= 10;"
psql_run "UPDATE rbd_h SET allnull = NULL WHERE i <= 10;"
check "an all-NULL column beside a wide one matches" \
	"$(dq_pair "coalesce(allnull,'N')||'|'||u")" "match"
psql_run "ALTER TABLE rbd DROP COLUMN b;"
psql_run "ALTER TABLE rbd_h DROP COLUMN b;"
check "reads after DROP COLUMN match" "$(dq_pair "i||':'||t||':'||u")" "match"

# ---- and a rescan reuses rather than rebuilding ------------------------------
check "a rescan (nested loop) returns the same rows" \
	"$(q "SELECT count(*) FROM (SELECT 1 FROM generate_series(1,3) s, rb WHERE rb.id <= 100) x;")" \
	"300"

pgc_summary

#!/usr/bin/env bash
#
# pgColumnar: pgcolumnar.parallel_copy can refuse a load it has already done
# (#403 item 7).
#
# THE DEFECT. A committed load whose acknowledgement the client never sees is
# retried, and the rows go in twice. Measured before this change: the same file
# loaded twice into the same table gives 100,000 rows and then 200,000.
#
# WHY THE UNIT IS THE WHOLE LOAD, not a "part". The paper this comes from keeps
# hashes of the last N inserted PARTS, because there each part commits on its
# own. pgcolumnar.parallel_copy is atomic through 2PC: the loaders PREPARE and a
# coordinator commits all or none, proved by a malformed row at line 50,001
# leaving 0 rows and 0 prepared transactions. Parts never commit independently
# here, so a part hash would deduplicate nothing that is not already
# all-or-nothing. The load is the unit.
#
# WHY IT IS OPT-IN. Discarding rows a client asked to insert is not SQL INSERT
# behaviour. It is off unless asked for, it is scoped to the bulk-load path, and
# it says what it skipped rather than returning a silent 0.
#
# WHAT MAKES TWO LOADS "THE SAME". The SHA-256 of the file's bytes, so a file
# that changed at the same path is a different load and is loaded. Path, size
# and mtime would all call that file the same one.
#
# THE ORDER OF THE TWO WRITES IS THE SAFETY ARGUMENT. The data commits first and
# the fingerprint is recorded after. A crash between them leaves data with no
# fingerprint, so a retry loads again -- which is exactly today's behaviour and
# is the safe direction. The reverse order would leave a fingerprint with no
# data, and a later load would be refused for rows that were never stored.
#
# RUN THIS AGAINST A BUILD WITH OpenSSL. The coordinator fingerprints the file
# from inside a transaction, and that is not tidiness: on a --with-openssl build
# pg_cryptohash_create registers the hash context with CurrentResourceOwner,
# which is NULL outside a transaction, and the coordinator segfaults. A build
# without OpenSSL uses the in-core SHA-2, touches no resource owner, and passes.
# This suite therefore passed on the local source builds and crashed on CI's
# packaged PostgreSQL until the transaction was added.
#
# Usage:  test/parallel_copy_dedup.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
export PGC_EXTRA_CONF=$'max_prepared_transactions=8\nmax_worker_processes=16'
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

DATADIR="$PGC_WORKDIR/pcdedup"
mkdir -p "$DATADIR"; chmod 777 "$DATADIR"
if [ "$(id -u)" = "0" ]; then chown postgres "$DATADIR"; fi

# The coordinator reads the file 256 KiB at a time (PCOPY_FINGERPRINT_BUFSZ). The
# changed file G differs from F ONLY in its last row, and 25,000 rows put that row
# past the first read. When G differed from its first bytes and the file fitted in
# one read, a fingerprint of the first read alone loaded G as a change and this
# suite passed 14 of 14.
ROWS=25000
F="$DATADIR/load.txt"
G="$DATADIR/other.txt"

psql_run "COPY (SELECT g AS id, 'v'||g AS txt FROM generate_series(1,$ROWS) g)
          TO '$F' WITH (FORMAT text);" >/dev/null
psql_run "COPY (SELECT g AS id, CASE WHEN g = $ROWS THEN 'changed' ELSE 'v'||g END AS txt
          FROM generate_series(1,$ROWS) g) TO '$G' WITH (FORMAT text);" >/dev/null

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$1" 2>&1 | tail -1
}

# The SHA-256 of each file's bytes, computed by the server from the file itself.
sha() { q "SELECT encode(sha256(pg_read_binary_file('$1')), 'hex')"; }
sha_f="$(sha "$F")"
sha_g="$(sha "$G")"
check_num "premise: the changed row lies past the first 256 KiB of the file" \
	"$(q "SELECT ((SELECT size FROM pg_stat_file('$G'))
	        - octet_length('$ROWS'||E'\tchanged\n') >= 262144)::int")" 1

mk() { psql_run "DROP TABLE IF EXISTS $1; CREATE TABLE $1 (id int, txt text) USING pgcolumnar;" >/dev/null; }

# ---- the control: without dedup the retry doubles the table ----------------
mk pcd_plain
r1="$(q "SELECT pgcolumnar.parallel_copy('pcd_plain','$F',4)")"
r2="$(q "SELECT pgcolumnar.parallel_copy('pcd_plain','$F',4)")"
# Not premises (#1339): dedup is pgcolumnar's own default, and forcing it on reddens
# both of these.
check "without dedup a repeated load doubles the table, which is the defect" \
	"$(q "SELECT count(*) FROM pcd_plain")" "$(( ROWS * 2 ))"
check "and each of those loads reported its rows" "$r1/$r2" "$ROWS/$ROWS"

# ---- the same load twice, with dedup --------------------------------------
mk pcd
d1="$(q "SELECT pgcolumnar.parallel_copy('pcd','$F',4,true)")"
c1="$(q "SELECT count(*) FROM pcd")"
# All of psql's output, NOTICE included: the header promises the skip says so.
o2="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SELECT pgcolumnar.parallel_copy('pcd','$F',4,true)" 2>&1)"
d2="$(printf '%s\n' "$o2" | tail -1)"
c2="$(q "SELECT count(*) FROM pcd")"
echo "-- dedup on: load 1 returned $d1 (table $c1), load 2 returned $d2 (table $c2)"

check "the first load stores its rows normally (#403 item 7)" "$c1" "$ROWS"
check "and reports them" "$d1" "$ROWS"
check "the second load of the same file stores nothing (#403 item 7)" "$c2" "$ROWS"
check "and reports 0 rather than claiming it loaded them" "$d2" "0"
check_num "and says in a NOTICE that it skipped the load, rather than returning a silent 0" \
	"$(printf '%s\n' "$o2" | grep -c 'NOTICE:  pgcolumnar.parallel_copy skipped a load already applied to "pcd"')" 1

# ---- a changed file at the same path is a different load -------------------
cp "$G" "$F"
d3="$(q "SELECT pgcolumnar.parallel_copy('pcd','$F',4,true)")"
c3="$(q "SELECT count(*) FROM pcd")"
echo "-- after replacing the file at the same path: returned $d3, table $c3"
check "a changed file at the same path is loaded, not skipped (#403 item 7)" "$c3" "$(( ROWS * 2 ))"
check "and reports the rows it loaded" "$d3" "$ROWS"

# ---- the fingerprint is per table -----------------------------------------
mk pcd_other
d4="$(q "SELECT pgcolumnar.parallel_copy('pcd_other','$F',4,true)")"
check "the same file loads into a DIFFERENT table (the record is per table)" \
	"$(q "SELECT count(*) FROM pcd_other")" "$ROWS"
check "and reports its rows" "$d4" "$ROWS"

# ---- dedup off still loads twice, on the same table ------------------------
d5="$(q "SELECT pgcolumnar.parallel_copy('pcd_other','$F',4,false)")"
check "asking for no dedup loads again even though the file is on record" \
	"$(q "SELECT count(*) FROM pcd_other")" "$(( ROWS * 2 ))"
check "and reports those rows too" "$d5" "$ROWS"

# ---- the record itself -----------------------------------------------------
check "the skipped load left exactly one record for that table and file" \
	"$(q "SELECT count(*) FROM pgcolumnar.load_fingerprint
	      WHERE relation_oid = 'pcd'::regclass")" "2"

# The SHA-256 of the file's bytes, not of some part of them: the two records are
# exactly the two files' digests, in load order.
check_text "the records hold the SHA-256 of each loaded file's bytes" \
	"$(q "SELECT string_agg(encode(fingerprint, 'hex'), ',' ORDER BY loaded_at)
	      FROM pgcolumnar.load_fingerprint WHERE relation_oid = 'pcd'::regclass")" \
	"$sha_f,$sha_g"

check "no prepared transaction leaked through any of it" \
	"$(q "SELECT count(*) FROM pg_prepared_xacts")" "0"

pgc_summary

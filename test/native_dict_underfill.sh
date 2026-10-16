#!/usr/bin/env bash
# Repro for the DICT decode under-fill: inflate a DICT chunk's value_raw_length
# in the encoding descriptor so the codes decode to fewer bytes than declared,
# leaving the raw buffer tail uninitialized (a varlena length is then read out of
# that garbage). Post-fix the reader must raise a clean error and survive.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# #1132: the writer now keeps an encoding only when it is smaller AFTER block
# compression, and this suite's subject is the ENCODERS rather than that choice.
# Its fixtures are chosen to exercise a particular encoder, which is not the same
# as being fixtures where that encoder beats zstd -- two of its controls assert
# the encoder actually ran, and those went red when the choice landed. Pinning the
# pre-#1132 behaviour here keeps this suite testing what it is named for; the
# selection policy has its own suite, encode_post_codec.sh.
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_post_codec_encoding_choice = off;" >/dev/null 2>&1

q "CREATE TABLE dt (id int, v text) USING pgcolumnar" >/dev/null
# low-cardinality short text -> DICT-encoded varlena column, one small chunk
q "INSERT INTO dt SELECT g, (ARRAY['aa','bb','cc','dd'])[1+(g%4)] FROM generate_series(1,300) g" >/dev/null

SID="$(q "SELECT pgcolumnar.get_storage_id('dt')")"
# column_index is 0-based; v is attribute 2 -> index 1
ENC="$(q "SELECT get_byte(encoding_descriptor, 6) FROM pgcolumnar.column_chunk WHERE storage_id=$SID AND column_index=1 LIMIT 1")"
echo "-- v-column vector0 encoding type byte (DICT=6): $ENC"
check "the text column is DICT-encoded (precondition)" "$ENC" "6"

# read old rawLen (uint32 LE at descriptor offset 11), inflate by 64, write back
crashes() {
	# grep -c prints 0 itself when nothing matches, and then exits 1
	pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null || true
}
crashes0="$(crashes)"
q "WITH u AS (UPDATE pgcolumnar.column_chunk c SET encoding_descriptor =
     set_byte(set_byte(set_byte(set_byte(encoding_descriptor,
       11, ( (get_byte(encoding_descriptor,11) + (get_byte(encoding_descriptor,12)<<8) + (get_byte(encoding_descriptor,13)<<16) + (get_byte(encoding_descriptor,14)<<24)) + 64 )       & 255),
       12, (((get_byte(encoding_descriptor,11) + (get_byte(encoding_descriptor,12)<<8) + (get_byte(encoding_descriptor,13)<<16) + (get_byte(encoding_descriptor,14)<<24)) + 64) >> 8) & 255),
       13, (((get_byte(encoding_descriptor,11) + (get_byte(encoding_descriptor,12)<<8) + (get_byte(encoding_descriptor,13)<<16) + (get_byte(encoding_descriptor,14)<<24)) + 64) >>16) & 255),
       14, (((get_byte(encoding_descriptor,11) + (get_byte(encoding_descriptor,12)<<8) + (get_byte(encoding_descriptor,13)<<16) + (get_byte(encoding_descriptor,14)<<24)) + 64) >>24) & 255)
     WHERE c.storage_id=$SID AND c.column_index=1 RETURNING 1) SELECT count(*) FROM u" > "$PGC_WORKDIR/uf.n"
check "premise: the inflation rewrote the DICT chunk's descriptor" "$(cat "$PGC_WORKDIR/uf.n")" "1"

env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
    -v VERBOSITY=verbose -c "SELECT v FROM dt" >/dev/null 2>"$PGC_WORKDIR/uf.err"
echo "-- select stderr:"; sed 's/^/     /' "$PGC_WORKDIR/uf.err" | head -3
# A CLEAN ERROR IS XX001, AND A CRASH IS READ FROM THE SERVER LOG (#1341). The
# rejection used to match 'does not match|corrupt|invalid|out of' in any message,
# which "out of memory" satisfies too; and survival was a fresh SELECT 1, which a
# crashed backend passes once crash recovery has finished. The crash flag below was
# computed and never checked.
rejected="$(sed -nE 's/^ERROR:  ([0-9A-Z]{5}): .*/\1/p' "$PGC_WORKDIR/uf.err" | head -1 | grep . || echo "no SQLSTATE ($(head -1 "$PGC_WORKDIR/uf.err"))")"
echo "-- rejected=$rejected crashes=$(( $(crashes) - crashes0 ))"
check "under-filled DICT chunk is rejected with a clean error" "$rejected" "XX001"
check "backend survives the under-filled DICT chunk" \
	"$(q 'SELECT 1')|$(( $(crashes) - crashes0 )) backend crashes" "1|0 backend crashes"
pgc_summary

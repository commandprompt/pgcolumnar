#!/usr/bin/env bash
#
# pgColumnar multi-file / directory Parquet reads (Phase G).
#
# A path that names a directory reads every *.parquet in it as one relation; a
# path with glob metacharacters expands the same way; a plain file is unchanged.
# This suite checks that the union of a directory's files matches a single-file
# oracle across all read surfaces (read_parquet, import_parquet, FDW), that a
# glob selects the right subset in sorted order, that per-file predicate pushdown
# and the aggregated EXPLAIN counters work, and that mismatched or empty inputs
# fail cleanly.
#
# Usage:  test/native_parquet_multifile.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; multi-file suite needs it"
fi

W="$PGC_WORKDIR"
DIR="$W/parts"
mkdir -p "$DIR"
psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# Three part files with disjoint id ranges, plus one combined oracle file. Each
# part is its own row group range so predicate pushdown can skip whole files.
python3 - "$W" "$DIR" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
W, DIR = sys.argv[1], sys.argv[2]
def tbl(lo, hi):
    return pa.table({"id": pa.array(range(lo, hi), pa.int32()),
                     "v":  pa.array([i * 2 for i in range(lo, hi)], pa.int64())})
# part-0: 0..999, part-1: 1000..1999, part-2: 2000..2999
for i in range(3):
    pq.write_table(tbl(i * 1000, i * 1000 + 1000), f"{DIR}/part-{i}.parquet",
                   row_group_size=1000)
# a non-parquet file in the dir must be ignored
open(f"{DIR}/README.txt", "w").write("not parquet\n")
# the oracle: all 3000 rows in one file
pq.write_table(tbl(0, 3000), f"{W}/all.parquet", row_group_size=1000)
PY

cols="id int, v int8"
oracle="$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$W/all.parquet') AS t($cols)")"

# ---- directory read == single-file oracle, across surfaces -----------------
check "read_parquet(directory) == oracle" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$DIR') AS t($cols)")" "$oracle"

psql_run "CREATE TABLE imp ($cols);"
psql_run "SELECT pgcolumnar.import_parquet('imp'::regclass, '$DIR');"
check "import_parquet(directory) == oracle" \
	"$(pgc_set_hash "SELECT * FROM imp")" "$oracle"
check "import_parquet(directory) row count" "$(q 'SELECT count(*) FROM imp;')" "3000"

psql_run "CREATE FOREIGN TABLE ftd ($cols) SERVER pq OPTIONS (path '$DIR');"
check "FDW(directory) == oracle" \
	"$(pgc_set_hash "SELECT * FROM ftd")" "$oracle"

# the non-parquet file is ignored, not read as data
check "directory read ignores non-parquet files" "$(q 'SELECT count(*) FROM ftd;')" "3000"

# ---- glob selects a subset -------------------------------------------------
check "glob part-[01] == first two files" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$DIR/part-[01].parquet') AS t($cols)")" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$W/all.parquet') AS t($cols) WHERE id < 2000")"

# ---- per-file predicate pushdown across the directory ----------------------
# id >= 2500 falls only in part-2; the other two files' groups must skip.
skipped_for() {
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM ftd WHERE $1" | grep 'Row Groups Skipped' | grep -oE '[0-9]+' | head -1
}
groups_for() {
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM ftd WHERE $1" | grep 'Row Groups:' | grep -oE '[0-9]+' | head -1
}
files_for() {
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM ftd WHERE $1" | grep 'Files:' | grep -oE '[0-9]+' | head -1
}
check "EXPLAIN reports 3 files" "$(files_for 'true')" "3"
check "EXPLAIN sums row groups across files" "$(groups_for 'true')" "3"
check "predicate skips the two non-matching files' groups" "$(skipped_for 'id >= 2500')" "2"
check "cross-file predicate result == oracle" \
	"$(pgc_set_hash "SELECT * FROM ftd WHERE id >= 2500")" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$W/all.parquet') AS t($cols) WHERE id >= 2500")"

# ---- projection composes with multi-file -----------------------------------
# The "Columns Read" projection counter (same set per file) alongside directory
# reads: count(*) over the directory decodes no columns, one-column select one.
cols_read() {
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" \
		| grep 'Columns Read:' | grep -oE '[0-9]+' | head -1
}
check "count(*) over directory reads 0 columns" "$(cols_read "SELECT count(*) FROM ftd")" "0"
check "one column over directory reads 1"       "$(cols_read "SELECT id FROM ftd")" "1"
check "count(*) over directory is correct"      "$(q 'SELECT count(*) FROM ftd;')" "3000"

# ---- error cases -----------------------------------------------------------
errs() {
	# Capture the whole output first: piping into `grep -q` under `pipefail`
	# closes the pipe early, SIGPIPEs psql, and makes the pipeline non-zero.
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -At -c "$1" 2>&1)"
	case "$out" in
		*ERROR*) echo OK ;;
		*) echo "NO ERROR" ;;
	esac
}
mkdir -p "$W/empty"
check "empty directory errors" "$(errs "SELECT * FROM pgcolumnar.read_parquet('$W/empty') AS t($cols)")" "OK"
check "non-matching glob errors" "$(errs "SELECT * FROM pgcolumnar.read_parquet('$DIR/nope-*.parquet') AS t($cols)")" "OK"

# a directory with a schema-mismatched file must fail, not silently skip/garble
mkdir -p "$W/mixed"
cp "$DIR/part-0.parquet" "$W/mixed/a.parquet"
python3 - "$W/mixed" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"x": pa.array(["a", "b"])}), f"{sys.argv[1]}/b.parquet")
PY
check "schema-mismatched file in directory errors" \
	"$(errs "SELECT * FROM pgcolumnar.read_parquet('$W/mixed') AS t($cols)")" "OK"

# a subdirectory whose name ends in .parquet is skipped, not read and failed on
mkdir -p "$DIR/sub.parquet"
check "directory ignores a subdir named *.parquet" "$(q 'SELECT count(*) FROM ftd;')" "3000"
rmdir "$DIR/sub.parquet"

# a glob that catches a directory (part-9.parquet/) skips it and reads the files
mkdir -p "$DIR/part-9.parquet"
check "glob skips a directory match, reads the files" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$DIR/part-*.parquet') AS t($cols)")" \
	"$oracle"
rmdir "$DIR/part-9.parquet"

# a glob whose every match is a directory has nothing to read and must say so
mkdir -p "$W/onlydirs/a.parquet" "$W/onlydirs/b.parquet"
check "glob matching only directories errors" \
	"$(errs "SELECT * FROM pgcolumnar.read_parquet('$W/onlydirs/*.parquet') AS t($cols)")" "OK"
check "directory of only *.parquet subdirs errors" \
	"$(errs "SELECT * FROM pgcolumnar.read_parquet('$W/onlydirs') AS t($cols)")" "OK"
rmdir "$W/onlydirs/a.parquet" "$W/onlydirs/b.parquet"

# A path we cannot stat must NOT be dropped: a dangling symlink named *.parquet
# has to surface as an error naming the file, not as a query that quietly returns
# the other files' rows. Silent row loss is the failure this reader must not have.
ln -s "$DIR/does-not-exist.parquet" "$DIR/broken.parquet"
check "dangling symlink in directory errors, not skipped" \
	"$(errs "SELECT count(*) FROM pgcolumnar.read_parquet('$DIR') AS t($cols)")" "OK"
check "dangling symlink in a glob errors, not skipped" \
	"$(errs "SELECT count(*) FROM pgcolumnar.read_parquet('$DIR/*.parquet') AS t($cols)")" "OK"
rm -f "$DIR/broken.parquet"

# A FIFO must be skipped rather than opened: AllocateFile() on a FIFO blocks in
# open(2) until a writer appears, and with SA_RESTART handlers a query cancel
# does not reliably return the backend. The timeout is the real assertion here;
# without it a regression hangs the suite instead of failing it.
if mkfifo "$DIR/pipe.parquet" 2>/dev/null; then
	fifo_out="$(timeout 30 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 \
		-p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SELECT count(*) FROM pgcolumnar.read_parquet('$DIR') AS t($cols)" 2>&1)"
	fifo_rc=$?
	if [ "$fifo_rc" = 124 ]; then
		fifo_out="TIMED OUT (blocked opening the FIFO)"
	fi
	check "FIFO named *.parquet is skipped, does not block" "$fifo_out" "3000"
	rm -f "$DIR/pipe.parquet"
else
	check_skip "the FIFO case" "SKIP  mkfifo unavailable; FIFO case not exercised" "mkfifo unavailable"
fi

# ---- recursive walk --------------------------------------------------------
#
# A directory now reads *.parquet at any depth below it, not only directly
# inside. The cases that matter are the ones where recursion can go wrong:
# unbounded depth, symlink cycles, and entries that are neither.

TREE="$W/tree"
mkdir -p "$TREE/a/b" "$TREE/c"
cp "$DIR/part-0.parquet" "$TREE/a/"
cp "$DIR/part-1.parquet" "$TREE/a/b/"
cp "$DIR/part-2.parquet" "$TREE/c/"
echo "not parquet" > "$TREE/a/b/notes.txt"

check "nested tree reads every level as one relation" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$TREE') AS t($cols)")" \
	"$oracle"
check "nested tree row count" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$TREE') AS t($cols);")" "3000"
# Spark and Hive write _SUCCESS beside the data and stage task output under
# _temporary, so a walk that descends into them reads files another writer is
# still producing. Names beginning with _ or . are skipped, directories and files
# alike. The parquet file below _temporary is real, so if the rule were missing
# this would read 4000 rows rather than 3000, which is what makes the check
# independent of the row-count check above.
mkdir -p "$TREE/_temporary/task-0" "$TREE/.hidden"
cp "$DIR/part-0.parquet" "$TREE/_temporary/task-0/in-progress.parquet"
cp "$DIR/part-0.parquet" "$TREE/.hidden/hidden.parquet"
touch "$TREE/_SUCCESS"
check "_temporary and dot directories are not walked" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$TREE') AS t($cols);")" "3000"
cp "$DIR/part-0.parquet" "$TREE/c/_part.parquet"
check "a file whose name starts with _ is skipped too" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$TREE') AS t($cols);")" "3000"
rm -rf "$TREE/_temporary" "$TREE/.hidden" "$TREE/_SUCCESS" "$TREE/c/_part.parquet"

# Order is by full path, not by directory-entry order, which varies by
# filesystem. Sorting the three paths puts a/b/part-1 first, then a/part-0, then
# c/part-2, so the first rows are part-1's. Asserting the actual ids pins the
# ordering rule; comparing two reads to each other would pass even if both were
# empty, which is what it did on the pre-change build.
check "tree read order follows the sorted full path" \
	"$(q "SELECT id FROM pgcolumnar.read_parquet('$TREE') AS t($cols) LIMIT 3;" | tr '\n' ' ')" \
	"1000 1001 1002 "

# A symlink to an ancestor is the classic infinite walk. The rule is that a
# directory reached through a symlink is not descended, so this terminates and
# returns the tree's real files. The timeout is the assertion: without the rule
# this never returns.
ln -s "$TREE" "$TREE/a/loop"
loop_out="$(timeout 60 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
	-U postgres -d "$PGC_DB" -At \
	-c "SELECT count(*) FROM pgcolumnar.read_parquet('$TREE') AS t($cols)" 2>&1)"
loop_rc=$?
[ "$loop_rc" = 124 ] && loop_out="TIMED OUT (symlink loop followed)"
check "a symlink loop terminates and does not multiply rows" "$loop_out" "3000"
rm -f "$TREE/a/loop"

# A symlinked directory holding a parquet file is not descended either, which is
# the cost of the rule and is asserted so the behaviour is deliberate.
mkdir -p "$W/elsewhere"
cp "$DIR/part-0.parquet" "$W/elsewhere/extra.parquet"
ln -s "$W/elsewhere" "$TREE/linkdir"
check "a symlinked directory is not descended" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$TREE') AS t($cols);")" "3000"
rm -f "$TREE/linkdir"

# A symlink to a FILE is still followed, as it was before the walk existed.
ln -s "$W/elsewhere/extra.parquet" "$TREE/c/linked.parquet"
check "a symlinked file is still read" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$TREE') AS t($cols);")" "4000"
rm -f "$TREE/c/linked.parquet"

# Depth is bounded, and the bound errors rather than silently truncating: a tree
# that is too deep must not read as "the part we felt like walking".
deep="$W/deep"
mkdir -p "$deep"
d="$deep"
for i in $(seq 1 40); do d="$d/l$i"; done
mkdir -p "$d"
cp "$DIR/part-0.parquet" "$d/"
check "a tree deeper than the bound is rejected, not truncated" \
	"$(errs "SELECT count(*) FROM pgcolumnar.read_parquet('$deep') AS t($cols)")" "OK"

# ---- a glob too deep or too long for glob(3) is an error (#1968, #1973) -----
#
# glibc glob(3) calls itself once for every '/' after the first *, ? or [, and
# each call keeps a copy of the pattern on the stack. The reader refuses a
# pattern with more than 128 such levels, or of PATH_MAX (4096) bytes or more,
# with error 54000 before glob(3) runs, as path_glob_matches does (#1964).
# st SQL -> the SQLSTATE SQL fails with, "ok" when it succeeds, or "no answer"
st() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	out=$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)
	echo "${out:-no answer}"
}
# wait_up -> return once the server answers SELECT 1, or after 60 s
wait_up() { local i; for i in $(seq 60); do [ "$(q "SELECT 1;")" = 1 ] && return; sleep 1; done; }
check "read_parquet looks for a local glob of 128 */ levels and refuses 129 with 54000" \
	"$(st "SELECT count(*) FROM pgcolumnar.read_parquet('$DIR/' || repeat('*/', 128) || 'x.parquet') AS t($cols)")|$(st "SELECT count(*) FROM pgcolumnar.read_parquet('$DIR/' || repeat('*/', 129) || 'x.parquet') AS t($cols)")" \
	"58P01|54000"
check "read_parquet refuses a local glob of 4096 bytes with 54000" \
	"$(st "SELECT count(*) FROM pgcolumnar.read_parquet(rpad('$DIR/*', 4088, 'a') || '.parquet') AS t($cols)")" \
	"54000"
# glob(3) in path_glob_matches walks a tree in memory where every directory
# lists . and .., and .* matches both. So repeat('/.*', n) walks 2^n paths in
# memory that glibc allocates outside PostgreSQL. The walk is bounded, and a
# pattern past the bound fails with 54000; it never answers false (#1973).
check "path_glob_matches refuses repeat('/.*', 40) with 54000 inside a 1 s statement_timeout" \
	"$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c statement_timeout=1s' psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v VERBOSITY=sqlstate -c "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 40) || '/x', repeat('/..', 40) || '/x')::text;" 2>&1 | head -1 | sed 's/^ERROR: *//')" \
	"54000"
check "path_glob_matches still matches repeat('/.*', 10) against its own path" \
	"$(q "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 10) || '/x', repeat('/..', 10) || '/x')::text;")" "true"
# The step count and the "stopped" flag are statics. Each walk must reset
# them, or one refused walk fails every later call in that session with
# 54000. Every other check opens a new connection, so only one session that
# runs all three statements can see a missing reset.
check "after a refused walk, path_glob_matches still answers true in the same session" \
	"$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c statement_timeout=1s' psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -v VERBOSITY=sqlstate \
		-c "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 40) || '/x', repeat('/..', 40) || '/x')::text;" \
		-c "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 10) || '/x', repeat('/..', 10) || '/x')::text;" \
		-c "SELECT pgcolumnar.path_glob_matches('/a/*', '/a/b')::text;" 2>&1 | sed 's/^ERROR: *//' | paste -sd'|')" \
	"54000|true|true"

# The reader bounds and globs a local pattern in a folded form: a run of '/'
# becomes one '/', and a '.' name with a '/' after it is dropped. The kernel
# reads both spellings as the same file, so the rows do not change, and a
# pattern over 128 levels as written but not once folded is read (#1978's
# tier_partition check reads such a table). A '.' at the end is kept: it
# makes the pattern match only directories. A name followed by '..' is kept
# too: a symbolic link before the '..' leads the kernel somewhere else.
plain_rows="$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$DIR/part-0.parquet') AS t($cols)")"
fold_dot="'$W/par[t]s' || repeat('/.', 200) || '/part-0.parquet'"
check "read_parquet reads a local glob over 128 levels as written but not once its '.' names are dropped, the same rows as the plain path" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet($fold_dot) AS t($cols)")|$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet($fold_dot) AS t($cols)")" \
	"1000|$plain_rows"
fold_slash="'$W/par[t]s' || repeat('/', 201) || 'part-0.parquet'"
check "read_parquet reads a local glob over 128 levels as written but not once its runs of '/' are folded, the same rows as the plain path" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet($fold_slash) AS t($cols)")|$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet($fold_slash) AS t($cols)")" \
	"1000|$plain_rows"
check "read_parquet keeps a '.' name at the end of a local glob, so a file followed by /. matches nothing" \
	"$(st "SELECT count(*) FROM pgcolumnar.read_parquet('$W/par[t]s/part-0.parquet/.') AS t($cols)")" \
	"58P01"
# glob(3) matches a pattern that ends in '/' against a file, so dropping a '.'
# name that only '/' or '\/' follow would read the file (#1977 review).
check "read_parquet keeps a '.' name that only '/' follow at the end of a local glob, so a file followed by /./ matches nothing" \
	"$(st "SELECT count(*) FROM pgcolumnar.read_parquet('$W/par[t]s/part-0.parquet/./') AS t($cols)")|$(st "SELECT count(*) FROM pgcolumnar.read_parquet('$W/par[t]s/part-0.parquet/.//') AS t($cols)")|$(st "SELECT count(*) FROM pgcolumnar.read_parquet('$W/par[t]s/part-0.parquet/./\/') AS t($cols)")" \
	"58P01|58P01|58P01"
# fold/ln leads to fold/away/inner, so fold/ln/.. is fold/away to the kernel.
# fold/away/z-0.parquet holds part-0 (ids 0..999); fold/z-0.parquet, the file
# a lexical fold of ln/.. would read, holds part-1 (ids 1000..1999).
mkdir -p "$W/fold/away/inner"
cp "$DIR/part-0.parquet" "$W/fold/away/z-0.parquet"
cp "$DIR/part-1.parquet" "$W/fold/z-0.parquet"
ln -s "$W/fold/away/inner" "$W/fold/ln"
check "read_parquet does not fold a name followed by '..' in a local glob, so it reads past a symbolic link as the kernel does" \
	"$(q "SELECT sum(id) FROM pgcolumnar.read_parquet('$W/fold/ln/../z-[0].parquet') AS t($cols)")" \
	"499500"

# Last in the suite: before the fix these reads crashed the server, and the
# crash recovery failed every check after them. Each check waits for the
# server to answer first, so a regression fails its own check alone.
check "read_parquet refuses a local glob of 4000 */ levels with 54000, and the server stays up" \
	"$(st "SELECT count(*) FROM pgcolumnar.read_parquet('$DIR/' || repeat('*/', 4000) || 'x.parquet') AS t($cols)")|$(q "SELECT 1;")" \
	"54000|1"
wait_up
# The pattern has 1991 levels as written and 1 once folded, so the bound
# lets it through. glob(3) of the form as written did not crash in a probe on
# glibc 2.43 (2022 levels, #1977 review), so this check does not tell which
# form glob(3) expands; it holds that the read is not refused.
check "read_parquet reads a local glob of 1990 '.' levels in its folded form, and the server stays up" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$W/par[t]s' || repeat('/.', 1990) || '/part-0.parquet') AS t($cols)")|$(q "SELECT 1;")" \
	"1000|1"
wait_up
# Over 10,000 bytes and 5001 levels as written, 1 level once folded. glob(3) of
# the form as written crashed the server (#1977 review), so this check fails
# if the reader globs the pattern as written instead of the folded form.
check "read_parquet reads a local glob over PATH_MAX as written but not once folded, and the server stays up" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$W/par[t]s' || repeat('/.', 5000) || '/part-0.parquet') AS t($cols)")|$(q "SELECT 1;")" \
	"1000|1"
wait_up
psql_run "DO \$\$BEGIN EXECUTE format('CREATE FOREIGN TABLE ftdeep ($cols) SERVER pq OPTIONS (path %L)', '$DIR/' || repeat('*/', 4000) || 'x.parquet'); END\$\$;"
check "a pgcolumnar_parquet foreign table over a local glob of 4000 */ levels raises 54000, and the server stays up" \
	"$(st "SELECT count(*) FROM ftdeep")|$(q "SELECT 1;")" \
	"54000|1"
wait_up
psql_run "DROP FOREIGN TABLE ftdeep;"

# ---- the reader's walk on the real filesystem is bounded (#1984) -----------
#
# glob(3) lists . and .. in every real directory, and .* matches both, so each
# level of .*/ multiplies the walk. glob(3) does not check for interrupts and
# keeps what it finds in memory it mallocs outside PostgreSQL. The reader counts
# the steps of the walk and stops past 1,000,000 with 54000, as
# path_glob_matches does (#1973), and a cancel stops the walk. Before the fix,
# 18 levels ran for minutes and no cancel stopped them, so each probe here runs
# under a client timeout, and a backend still walking then is killed.
# kill_walks -> SIGKILL every backend whose statement holds the tag pgc1984
kill_walks() {
	local pid
	for pid in $(q "SELECT pid FROM pg_stat_activity WHERE query LIKE '%pgc1984%' AND pid <> pg_backend_pid();"); do
		pgc_pg "kill -9 $pid" >/dev/null 2>&1
	done
	wait_up
}
# st_in SECONDS OPTIONS SQL -> as st, with PGOPTIONS set to OPTIONS, or
# "no answer in SECONDS s" when the client times out
st_in() {
	local out rc
	out=$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="$2" timeout "$1" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$3" 2>&1 >/dev/null)
	rc=$?
	if [ "$rc" = 124 ]; then
		kill_walks
		echo "no answer in $1 s"
		return
	fi
	[ -z "$out" ] && { echo ok; return; }
	out=$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)
	echo "${out:-no answer}"
}
mkdir -p "$W/emptyglob"
dots18="'$W/emptyglob/' || repeat('.*/', 18) || 'x.parquet'"
check "read_parquet refuses a local glob of 18 .*/ levels with 54000 inside a 1 s statement_timeout" \
	"$(st_in 20 '-c statement_timeout=1s' "SELECT /* pgc1984 */ count(*) FROM pgcolumnar.read_parquet($dots18) AS t($cols)")" \
	"54000"
# fan/ holds 8 symbolic links to itself, so each level of .*/ in fan/ matches
# ., and the 8 links, and the walk grows ninefold per level. 5 levels take
# about 220,000 steps, under the bound: the walk ends and matches nothing
# (58P01). A 20 ms statement_timeout fires inside that walk, and the walk
# must stop for it (57014). Before the fix the walk ran to its end and the
# read raised 58P01 before the timeout was seen.
mkdir -p "$W/fan"
for i in 1 2 3 4 5 6 7 8; do ln -sfn . "$W/fan/.l$i"; done
fan5="'$W/fan/' || repeat('.*/', 5) || 'x.parquet'"
check "a 20 ms statement_timeout stops read_parquet's local glob walk with 57014; without it the walk ends with 58P01" \
	"$(st_in 60 '' "SELECT /* pgc1984 */ count(*) FROM pgcolumnar.read_parquet($fan5) AS t($cols)")|$(st_in 60 '-c statement_timeout=20ms' "SELECT /* pgc1984 */ count(*) FROM pgcolumnar.read_parquet($fan5) AS t($cols)")" \
	"58P01|57014"
# 6 levels over fan/ take about 2,000,000 steps, so the walk runs until the
# bound stops it, about 1 s on the test host. The cancel is sent once the
# statement is active, and the walk must stop for it before the bound.
# cancel_walk SQL -> the SQLSTATE SQL fails with after pg_cancel_backend
cancel_walk() {
	local f="$W/cancel1984.out" pid="" i bg
	st_in 60 '' "$1" >"$f" &
	bg=$!
	for i in $(seq 500); do
		pid=$(q "SELECT pid FROM pg_stat_activity WHERE query LIKE '%pgc1984 cancel%' AND state = 'active' AND pid <> pg_backend_pid() LIMIT 1;")
		[ -n "$pid" ] && break
		sleep 0.01
	done
	sleep 0.1
	[ -n "$pid" ] && q "SELECT pg_cancel_backend($pid);" >/dev/null
	wait "$bg"
	if [ -n "$pid" ]; then cat "$f"; else echo "$(cat "$f")|no active backend seen"; fi
}
fan6="'$W/fan/' || repeat('.*/', 6) || 'x.parquet'"
check "pg_cancel_backend stops read_parquet's local glob walk with 57014" \
	"$(cancel_walk "SELECT /* pgc1984 cancel */ count(*) FROM pgcolumnar.read_parquet($fan6) AS t($cols)")" \
	"57014"
# The step count and the "stopped" flags are statics; each walk resets them.
check "after a refused walk, read_parquet still reads a local glob in the same session" \
	"$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c statement_timeout=20s' timeout 20 psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -v VERBOSITY=sqlstate \
		-c "SELECT /* pgc1984 */ count(*) FROM pgcolumnar.read_parquet($dots18) AS t($cols);" \
		-c "SELECT count(*) FROM pgcolumnar.read_parquet('$DIR/part-[01].parquet') AS t($cols);" 2>&1 | sed 's/^ERROR: *//' | paste -sd'|')" \
	"54000|2000"
kill_walks
# Each name glob(3) reads is a step, so a glob over a large directory must
# stay far below the bound and still read every match.
python3 - "$W/many" <<'PY'
import os, sys, pyarrow as pa, pyarrow.parquet as pq
d = sys.argv[1]
os.makedirs(d)
for i in range(2000):
    pq.write_table(pa.table({"id": pa.array([i], pa.int32()), "v": pa.array([i], pa.int64())}),
                   f"{d}/m-{i:05d}.parquet")
for i in range(10000):
    open(f"{d}/n-{i:05d}.txt", "w").close()
PY
check "read_parquet reads all 2000 files a local glob matches among 12000 names" \
	"$(q "SELECT count(*) || '|' || sum(id) FROM pgcolumnar.read_parquet('$W/many/m-*.parquet') AS t($cols)")" \
	"2000|1999000"

pgc_summary

#!/usr/bin/env bash
#
# pgcolumnar.tier_partition moves a cold columnar partition to Parquet, in one
# transaction, and a failure at any step leaves the rows exactly once (#1295).
#
# WRITTEN BEFORE THE CODE, against the API proposed on #1295:
#
#     pgcolumnar.tier_partition(partition regclass, server name, path text) -> rows
#
# The owner's decision (#1295, 2026-10-12): atomic, with a brief ACCESS EXCLUSIVE
# on the parent. Export under the partition's SHARE lock, then detach, drop,
# create the foreign table under the partition's own name, and attach it with the
# partition's own bounds.
#
# "EXACTLY ONCE" IS TESTED, NOT ARGUED. pgcolumnar.tier_fault_after raises an
# ERROR right after the step it names. Each fault arm first requires that the
# error raised IS the injected one: a missing function also fails, and leaves
# the rows where they were, so "the rows are still local" alone proves nothing.
#
# A local path stands in for object storage; `s3://` is the same writer with
# another path and is not exercised here.
#
# Usage:  test/tier_partition.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# The endpoint allow-list (#393) defaults to deny-all. The object-store section
# at the end (#1795) writes to two local SigV4 fixture servers.
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='127.0.0.1'"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

W="$PGC_WORKDIR/tier"
mkdir -p "$W" && chmod 777 "$W"

# sqlstate SQL... -> the SQLSTATE the last statement fails with, or ok
sqlstate() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate "${@/#/-c}" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
# errtext SQL... -> the first ERROR line's message, or ok
errtext() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 "${@/#/-c}" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^ERROR: *//p' | head -1
}
# errdetail SQL... -> the first DETAIL line of the error, or ok, or none
errdetail() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 "${@/#/-c}" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	out=$(echo "$out" | sed -n 's/^DETAIL: *//p' | head -1)
	echo "${out:-none}"
}

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
psql_run "CREATE SERVER tier_pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# build NAME: a parent with a January and a February columnar partition
build() {
	psql_run "DROP TABLE IF EXISTS $1 CASCADE;"
	psql_run "CREATE TABLE $1 (ts timestamptz NOT NULL, dev int, v bigint, note text)
	          PARTITION BY RANGE (ts);"
	psql_run "CREATE TABLE ${1}_jan PARTITION OF $1
	          FOR VALUES FROM ('2026-01-01') TO ('2026-02-01') USING pgcolumnar;"
	psql_run "CREATE TABLE ${1}_feb PARTITION OF $1
	          FOR VALUES FROM ('2026-02-01') TO ('2026-03-01') USING pgcolumnar;"
	psql_run "INSERT INTO $1 SELECT '2026-01-01'::timestamptz + g * interval '5 min', g % 13,
	          (hashint4(g)::bigint * 7) % 100003, md5(g::text)
	          FROM generate_series(0, 16991) g;"
}
# every row, decoded: an unfiltered count/sum is answered from zone maps
digest() { q "SELECT count(*) || '|' || md5(string_agg(ts::text || dev || v || note, ',' ORDER BY ts)) FROM $1;"; }
# where the January range lives: relkind, access method, and its bound
jan_shape() {
	q "SELECT c.relkind::text || ':' || coalesce(a.amname, '-') || ':' || pg_get_expr(c.relpartbound, c.oid)
	   FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid LEFT JOIN pg_am a ON a.oid = c.relam
	   WHERE i.inhparent = '$1'::regclass AND c.relname = '${1}_jan';"
}
JAN_BOUND="FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-02-01 00:00:00+00')"

psql_run "SET timezone = 'UTC'; ALTER DATABASE \"$PGC_DB\" SET timezone = 'UTC';"
build m
check_num "premise: the parent holds every row" "$(q "SELECT count(*) FROM m;")" "16992"
check_num "premise: the cold partition holds its share" "$(q "SELECT count(*) FROM m_jan;")" "8928"
before="$(digest m)"
check "premise: the cold partition is local and columnar, with its bound" "$(jan_shape m)" "r:pgcolumnar:$JAN_BOUND"
jan_storage="$(q "SELECT pgcolumnar.get_storage_id('m_jan');")"

# ---- the move ---------------------------------------------------------------
check "tier_partition moves the cold partition and returns the rows it moved" \
	"$(q "SELECT pgcolumnar.tier_partition('m_jan', 'tier_pq', '$W/m_jan.parquet');")" "8928"
# The relkind rides along: with nothing moved the digest is trivially unchanged.
check "the answers over the parent are the same after the move" \
	"$(digest m)|$(q "SELECT relkind FROM pg_class WHERE oid = 'm_jan'::regclass;")" "$before|f"
check "the cold partition is now a foreign partition with its own name and bound" "$(jan_shape m)" "f:-:$JAN_BOUND"
check "the foreign partition reads the exported file" \
	"$(q "SELECT srvname || ':' || array_to_string(ftoptions, ',') FROM pg_foreign_table t
	      JOIN pg_foreign_server s ON s.oid = t.ftserver WHERE t.ftrelid = 'm_jan'::regclass;")" \
	"tier_pq:path=$W/m_jan.parquet"
check_num "the local partition leaves no pgcolumnar storage row behind" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = ${jan_storage:-0};")" "0"
check_num "premise: the storage id was read before the move" "$([ -n "$jan_storage" ] && echo 1 || echo 0)" "1"
check_num "nor any row-group row" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = ${jan_storage:-0};")" "0"

# ---- pruning over the mixed parent ------------------------------------------
plan_jan="$(q "EXPLAIN SELECT max(v) FROM m WHERE ts < '2026-01-20';")"
plan_feb="$(q "EXPLAIN SELECT max(v) FROM m WHERE ts >= '2026-02-10';")"
check "a query on the tiered range scans only the foreign partition" \
	"$(grep -c 'Foreign Scan' <<<"$plan_jan")|$(grep -c 'm_feb' <<<"$plan_jan")" "1|0"
check "a query on the other range scans only the local partition, beside a foreign one" \
	"$(grep -c 'Foreign Scan' <<<"$plan_feb")|$(grep -c 'm_feb' <<<"$plan_feb")|$(grep -c 'Foreign Scan' <<<"$plan_jan")" "0|1|1"
check "an insert routed to the tiered range is refused" \
	"$(sqlstate "INSERT INTO m VALUES ('2026-01-15', 1, 1, 'x');")" "0A000"

# ---- refusals, before anything moves ----------------------------------------
# Measured with export_parquet on main: an existing path is overwritten without a
# word, which is why tier_partition must refuse one.
rb="$(digest m)"
psql_run "CREATE TABLE lone (ts timestamptz, v int) USING pgcolumnar;"
check "a table that is not a partition is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('lone', 'tier_pq', '$W/lone.parquet');")" "42809"
psql_run "CREATE TABLE m_mar PARTITION OF m FOR VALUES FROM ('2026-03-01') TO ('2026-04-01') USING heap;"
# Refused by export_parquet, the first step, so nothing has moved: the arm holds
# the property, not which line of tier_partition raises it.
check "a partition that is not columnar is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_mar', 'tier_pq', '$W/m_mar.parquet');")" "42809"
psql_run "CREATE TABLE m_def PARTITION OF m DEFAULT USING pgcolumnar;"
check "a DEFAULT partition is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_def', 'tier_pq', '$W/m_def.parquet');")" "0A000"
psql_run "CREATE SERVER tier_ice FOREIGN DATA WRAPPER pgcolumnar_iceberg;"
# Before anything moves (#1339): a refusal that came only from a later step, after
# the export, would leave the file behind with the same SQLSTATE.
check "a server of another wrapper is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_ice', '$W/m_feb.parquet');")|$([ -e "$W/m_feb.parquet" ] && echo file || echo nofile)" \
	"42809|nofile"
check "an existing path is refused, so no file a table reads is overwritten" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/m_jan.parquet');")" "58P02"
# The readers read s3a:// and s3n:// as s3://, and match the scheme in any case
# (#1930), so these spellings name the object a tier to s3:// would overwrite
# (#1936). The refusal comes before the export, so no store is contacted.
psql_run "CREATE FOREIGN TABLE dup_s3a (x int) SERVER tier_pq OPTIONS (path 's3a://tierb/m_feb_a.parquet');"
psql_run "CREATE FOREIGN TABLE dup_upper (x int) SERVER tier_pq OPTIONS (path 'S3://tierb/m_feb_u.parquet');"
check "a path a foreign table reads as s3a:// is refused like the identical s3:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/m_feb_a.parquet');")" "58P02"
check "a path a foreign table reads as S3:// is refused like the identical s3:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/m_feb_u.parquet');")" "58P02"
# Each branch of the rule has a check of its own (#1937 review): s3n:// on the
# table side, an upper-case scheme on the target side, and the scheme case of
# gs://, az://, http:// and https://.
psql_run "CREATE FOREIGN TABLE dup_s3n (x int) SERVER tier_pq OPTIONS (path 's3n://tierb/m_feb_n.parquet');"
psql_run "CREATE FOREIGN TABLE dup_lower (x int) SERVER tier_pq OPTIONS (path 's3://tierb/m_feb_l.parquet');"
psql_run "CREATE FOREIGN TABLE dup_gs (x int) SERVER tier_pq OPTIONS (path 'GS://tierb/m_feb_g.parquet');"
psql_run "CREATE FOREIGN TABLE dup_az (x int) SERVER tier_pq OPTIONS (path 'Az://tierb/m_feb_z.parquet');"
psql_run "CREATE FOREIGN TABLE dup_http (x int) SERVER tier_pq OPTIONS (path 'HTTP://h.example/m_feb_h.parquet');"
psql_run "CREATE FOREIGN TABLE dup_https (x int) SERVER tier_pq OPTIONS (path 'HTTPS://h.example/m_feb_t.parquet');"
check "a path a foreign table reads as s3n:// is refused like the identical s3:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/m_feb_n.parquet');")" "58P02"
check "an S3:// path is refused when a foreign table reads the identical s3:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'S3://tierb/m_feb_l.parquet');")" "58P02"
check "a path a foreign table reads as GS:// is refused like the identical gs:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'gs://tierb/m_feb_g.parquet');")" "58P02"
check "a path a foreign table reads as Az:// is refused like the identical az:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'az://tierb/m_feb_z.parquet');")" "58P02"
check "a path a foreign table reads as HTTP:// is refused like the identical http:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'http://h.example/m_feb_h.parquet');")" "58P02"
check "a path a foreign table reads as HTTPS:// is refused like the identical https:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'https://h.example/m_feb_t.parquet');")" "58P02"
# tier_partition is not STRICT. With foreign tables on a path= option present, a
# NULL path must still reach the null refusal, not the same-object refusal.
check "a NULL path is refused as null, not as a file a foreign table reads" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', NULL);")" "22004"
psql_run "DROP FOREIGN TABLE dup_s3a, dup_upper, dup_s3n, dup_lower, dup_gs, dup_az, dup_http, dup_https;"
# A foreign table on a directory or a glob reads every file the reader expands
# it to (pq_resolve_paths), so a path under that directory or matching that glob
# names a file the table reads (#1941). The host of an http(s) URL matches in
# any case. A remote target that passes the check fails in the export with
# 28000, because tier_pq has no credentials, so nothing is written. A local
# target that passed would move the partition, so each local call runs in a
# transaction that rolls back.
psql_run "CREATE FOREIGN TABLE dup_rdir (x int) SERVER tier_pq OPTIONS (path 's3://tierb/dir/');
          CREATE FOREIGN TABLE dup_rglob (x int) SERVER tier_pq OPTIONS (path 's3a://tierb/g/*.parquet');
          CREATE FOREIGN TABLE dup_rclass (x int) SERVER tier_pq OPTIONS (path 's3://tierb/c/m_?[0-9].parquet');
          CREATE FOREIGN TABLE dup_resc (x int) SERVER tier_pq OPTIONS (path 's3://tierb/e/\\*.parquet');
          CREATE FOREIGN TABLE dup_rkey (x int) SERVER tier_pq OPTIONS (path 's3://tierb/key');
          CREATE FOREIGN TABLE dup_host (x int) SERVER tier_pq OPTIONS (path 'http://H.Example/m_feb_k.parquet');
          CREATE FOREIGN TABLE dup_ldir (x int) SERVER tier_pq OPTIONS (path '$W/ldir');
          CREATE FOREIGN TABLE dup_lglob (x int) SERVER tier_pq OPTIONS (path '$W/lg/*.parquet');
          CREATE FOREIGN TABLE dup_runt (x int) SERVER tier_pq OPTIONS (path 's3://tierb/u/?[]x.parquet');
          CREATE FOREIGN TABLE dup_rbang (x int) SERVER tier_pq OPTIONS (path 's3://tierb/n/a[!]x*');
          CREATE FOREIGN TABLE dup_rbesc (x int) SERVER tier_pq OPTIONS (path 's3://tierb/b/m[\\]]x.parquet');
          CREATE FOREIGN TABLE dup_rdigit (x int) SERVER tier_pq OPTIONS (path 's3://tierb/pd/2026_[[:digit:]][[:digit:]].parquet');
          CREATE FOREIGN TABLE dup_rcaret (x int) SERVER tier_pq OPTIONS (path 's3://tierb/ca/[^]x].parquet');
          CREATE FOREIGN TABLE dup_lslash (x int) SERVER tier_pq OPTIONS (path '$W/ls/*[a/b].parquet');
          CREATE FOREIGN TABLE dup_rlead (x int) SERVER tier_pq OPTIONS (path 's3://tierb/la/[]a].parquet');
          CREATE FOREIGN TABLE dup_rbunt (x int) SERVER tier_pq OPTIONS (path 's3://tierb/xb/x[\\].parquet');
          CREATE FOREIGN TABLE dup_ropen (x int) SERVER tier_pq OPTIONS (path 's3://tierb/pu/m[[:digit:].parquet');
          CREATE FOREIGN TABLE dup_requiv (x int) SERVER tier_pq OPTIONS (path 's3://tierb/eq/[[=]=]].parquet');
          CREATE FOREIGN TABLE dup_rzq (x int) SERVER tier_pq OPTIONS (path 's3://tierb/zq/[[:z:]].parquet');
          CREATE FOREIGN TABLE dup_rrange (x int) SERVER tier_pq OPTIONS (path 's3://tierb/rg/a[!:-[:digit:]].parquet');
          CREATE FOREIGN TABLE dup_rlate (x int) SERVER tier_pq OPTIONS (path 's3://tierb/lt/[1a-[:alpha:]]x.parquet');
          CREATE FOREIGN TABLE dup_rlateq (x int) SERVER tier_pq OPTIONS (path 's3://tierb/le/[1a-[=b=]]x.parquet');
          CREATE FOREIGN TABLE dup_llate (x int) SERVER tier_pq OPTIONS (path '$W/llt/[1a-[:alpha:]]x.parquet');
          CREATE FOREIGN TABLE dup_rcdash (x int) SERVER tier_pq OPTIONS (path 's3://tierb/cd/[[:alpha:]-[:digit:]].parquet');
          CREATE FOREIGN TABLE dup_rrdash (x int) SERVER tier_pq OPTIONS (path 's3://tierb/rd/-[/-[-[:alpha:][=a=]].parquet');
          CREATE FOREIGN TABLE dup_rnone (x int) SERVER tier_pq OPTIONS (path 's3://tierb/nn/[1[::]-a]x.parquet');
          CREATE FOREIGN TABLE dup_rcollab (x int) SERVER tier_pq OPTIONS (path 's3://tierb/cb/[1[.ab.]-a]x.parquet');
          CREATE FOREIGN TABLE dup_rvneg (x int) SERVER tier_pq OPTIONS (path 's3://tierb/vn/[^-:-[:alpha:]x].parquet');
          CREATE FOREIGN TABLE dup_lvneg (x int) SERVER tier_pq OPTIONS (path '$W/lvn/[!-:-[:alpha:]x].parquet');
          CREATE FOREIGN TABLE dup_rquote (x int) SERVER tier_pq OPTIONS (path 's3://tierb/q/it''s.parquet');
          CREATE FOREIGN TABLE dup_rdslash (x int) SERVER tier_pq OPTIONS (path 's3://tierb/dp/[1a-[:alpha:]]x/*.parquet');
          CREATE FOREIGN TABLE dup_rndash (x int) SERVER tier_pq OPTIONS (path 's3://tierb/nd/[^[:alpha:]-[-[=a=]]x.parquet');
          CREATE FOREIGN TABLE dup_lndash (x int) SERVER tier_pq OPTIONS (path '$W/lnd/[![:alpha:]-[-[=a=]]x.parquet');
          CREATE FOREIGN TABLE dup_hglob (x int) SERVER tier_pq OPTIONS (path 'http://h.example/hg/*.parquet');
          CREATE FOREIGN TABLE dup_gglob (x int) SERVER tier_pq OPTIONS (path 'gs://tierb/gg/*.parquet');
          CREATE FOREIGN TABLE dup_rcunt (x int) SERVER tier_pq OPTIONS (path 's3://tierb/nc/a[^]x*');
          CREATE FOREIGN TABLE dup_rspan (x int) SERVER tier_pq OPTIONS (path 's3://tierb/sp/x[a/b].parquet');
          CREATE FOREIGN TABLE dup_rcoll (x int) SERVER tier_pq OPTIONS (path 's3://tierb/co/[[.].]].parquet');
          CREATE FOREIGN TABLE dup_lequiv (x int) SERVER tier_pq OPTIONS (path '$W/le/[[=/=]a].parquet');
          CREATE FOREIGN TABLE dup_rnskip (x int) SERVER tier_pq OPTIONS (path 's3://tierb/nsk/[![-x[!-[=a=]');
          CREATE FOREIGN TABLE dup_lnskip (x int) SERVER tier_pq OPTIONS (path '$W/lnsk/[![-x[!-[=a=]');
          CREATE FOREIGN TABLE dup_lbslash (x int) SERVER tier_pq OPTIONS (path '$W/lbs/[\\/]');
          CREATE FOREIGN TABLE dup_ltrail (x int) SERVER tier_pq OPTIONS (path '$W/lts/*//');
          CREATE FOREIGN TABLE dup_lhide (x int) SERVER tier_pq OPTIONS (path '$W/lgh/*.parquet');
          CREATE FOREIGN TABLE dup_rnl (x int) SERVER tier_pq OPTIONS (path E's3://tierb/nl/a.parquet\\nHINT:  x');
          CREATE FOREIGN TABLE dup_rnlg (x int) SERVER tier_pq OPTIONS (path E's3://tierb/nlg/[a\\nHINT:  x]x.parquet');
          CREATE FOREIGN TABLE \"dup_tnl
HINT:  trust me\" (x int) SERVER tier_pq OPTIONS (path 's3://tierb/tnl/a.parquet');
          CREATE FOREIGN TABLE dup_rlitx (x int) SERVER tier_pq OPTIONS (path 's3://tierb/litx/a\\x0A.parquet');"
# The directories exist, so a local target the check let through would be written.
mkdir -p "$W/ldir/sub" "$W/lg" && chmod 777 "$W/ldir" "$W/ldir/sub" "$W/lg"
# rolled SQL -> the SQLSTATE of SQL run inside a transaction that rolls back
rolled() { sqlstate "BEGIN" "$1" "ROLLBACK"; }
check "a path under a directory a foreign table reads as s3://b/dir/ is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/dir/x.parquet');")" "58P02"
check "a path two levels under a directory a foreign table reads is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/dir/a/b.parquet');")" "58P02"
check "a path matching a glob a foreign table reads as s3a://b/g/*.parquet is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/g/x.parquet');")" "58P02"
check "a path matching a glob with ? and a [class] a foreign table reads is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/c/m_x7.parquet');")" "58P02"
check "an http path is refused when a foreign table reads the same URL with its host in another case" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'http://h.example/m_feb_k.parquet');")" "58P02"
check "a local path under a directory a foreign table reads without a trailing slash is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/ldir/sub/x.parquet');")" "58P02"
check "a local path matching a glob a foreign table reads is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/lg/x.parquet');")" "58P02"
check "the refusal's detail names the foreign table and the path it reads" \
	"$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/dir/x.parquet');")" \
	"Foreign table public.dup_rdir reads path 's3://tierb/dir/'."
# A bracket follows fnmatch: a '[' with no closing ']' is a literal '[', and a
# backslash inside a class escapes the next character, so '[\]]' is one class.
check "an unterminated [ in a glob is a literal [, so a path holding it is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/u/q[]x.parquet');")" "58P02"
check "a [!] with no closing bracket in a glob is literal, so a path holding it is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/n/a[!]xfoo.parquet');")" "58P02"
check "a backslash in a glob [class] escapes a ], so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/b/m]x.parquet');")" "58P02"
# fnmatch also reads a POSIX class such as [:digit:] inside a class, and [^ as
# [!. A local glob is split at each slash before it is matched, so a class that
# spans a slash is a literal [ there; in an s3:// key it is a class (#1946 review).
check "a glob POSIX class such as [[:digit:]] is one class, so a path it matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/pd/2026_01.parquet');")" "58P02"
check "a [^ in a glob negates like [!, so a path the class [^]x] matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/ca/a.parquet');")" "58P02"
check "a local glob splits at a slash before a [class], so a path holding the [ literally is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/ls/foo[a/b].parquet');")" "58P02"
check "a backslash in a glob escapes a star, so a path holding a literal star is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/e/*.parquet');")" "58P02"
check "a ] right after [ in a glob is a class member, so a path the class []a] matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/la/a.parquet');")" "58P02"
check "a [ whose only ] is escaped in a glob is a literal [, so a path holding it is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/xb/x[].parquet');")" "58P02"
# glibc reads a [:name:] inside a class only when the name is letters a to y
# (a 'z' ends it), and an unclosed [ is a literal [ even when a POSIX class
# follows it (#1946 review, found by comparing with glibc fnmatch).
check "a [ left open before a POSIX class in a glob is a literal [, so a path holding it is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/pu/m[d.parquet');")" "58P02"
check "an equivalence class [=]=] in a glob holds a ], so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/eq/].parquet');")" "58P02"
check "a [:z:] in a glob is not a POSIX class to fnmatch, so a path that reads it as members is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/zq/z].parquet');")" "58P02"
check "a [ that ends a range in a glob is a plain member, so a path the shorter class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/rg/ab].parquet');")" "58P02"
# An unclosed [^] is a literal [ as [!] is; an s3:// key is matched whole, so
# a class there may span a slash; and a collating symbol may hold a ], as an
# equivalence class may (#1946 review, third round).
check "a [^] with no closing bracket in a glob is literal, so a path holding it is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/nc/a[^]xfoo.parquet');")" "58P02"
check "an s3:// glob [class] may span a slash, so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/sp/xa.parquet');")" "58P02"
check "a collating symbol [.].] in a glob holds a ], so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/co/].parquet');")" "58P02"
# After a member matches, glibc skips to the closing ] with code that knows no
# ranges. So when a range ends in a plain [ and a POSIX class or an equivalence
# class seems to follow, a member before the range makes fnmatch close the class
# at a later ]. Such a glob is refused from that class on, whatever follows it.
# A - after a class or a range is a plain member, and [::] or [.ab.] is one
# unit to the skip (#1951, found by comparing with glibc fnmatch).
check "a [ that ends a range before a POSIX class lets fnmatch close the class later, so a path it then matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/lt/1x.parquet');")" "58P02"
check "a [ that ends a range before an equivalence class lets fnmatch close the class later, so a path it then matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/le/1x.parquet');")" "58P02"
check "a local glob [ that ends a range before a POSIX class lets fnmatch close the class later, so a path it then matches is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/llt/1x.parquet');")" "58P02"
check "a - after a POSIX class in a glob is a member, not a range, so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/cd/-.parquet');")" "58P02"
check "a - after a range in a glob is a member, so the POSIX class after it is one member and a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/rd/-a.parquet');")" "58P02"
check "an empty [::] in a glob class is one unit to fnmatch, so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/nn/1x.parquet');")" "58P02"
check "a collating symbol [.ab.] in a glob class is one unit to fnmatch, so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/cb/1x.parquet');")" "58P02"
# A ! or ^ right after [ always negates, even where reading it as a member
# would give a longer token. Here fnmatch closes at the first ] and the x] after
# it is literal (#1953 review).
check "a ! or ^ after [ in a glob always negates, so a path the negated class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/vn/1x].parquet');")" "58P02"
check "a ! or ^ after [ in a local glob always negates, so a path the negated class matches is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/lvn/1x].parquet');")" "58P02"
# The widened class runs to the end of the pattern, so it still refuses a key
# below a / that follows it. And a - after a POSIX class stays a member in a
# negated class too, where reading it as a range would give the longer token:
# fnmatch closes at the first ], so 1] matches (#1953 review, third round).
check "a widened bracket followed by a slash still refuses the deeper key" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/dp/1x/f.parquet');")" "58P02"
check "a - after a POSIX class in a negated glob class is a member, so a path the class matches is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/nd/1]x.parquet');")" "58P02"
check "a - after a POSIX class in a negated local glob class is a member, so a path the class matches is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/lnd/1]x.parquet');")" "58P02"
check "the refusal's detail prints a path holding a backslash as written, not as an E'' string" \
	"$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/e/*.parquet');")" \
	"Foreign table public.dup_resc reads path 's3://tierb/e/\\*.parquet'."
# The detail quotes the path by hand, so it doubles a ' in it. Otherwise a
# table owner's path could close the quote and append a line that reads like
# a server HINT under the refusal (#1953 review).
check "the refusal's detail doubles a quote in the path, so the path cannot close its quotes early" \
	"$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/q/it''s.parquet');")" \
	"Foreign table public.dup_rquote reads path 's3://tierb/q/it''s.parquet'."
# The function reads its own patterns with standard_conforming_strings on, so a
# session that turns it off gets the same refusal (#1946 review). PostgreSQL 19
# refuses to turn it off (0A000), so there the session keeps it on.
scs_off="DO \$\$BEGIN SET standard_conforming_strings = off;
         EXCEPTION WHEN feature_not_supported THEN NULL; END\$\$"
check "a session that asks for standard_conforming_strings off still has a path a glob matches refused" \
	"$(sqlstate "$scs_off" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/g/x.parquet');")" "58P02"
# The rule follows the reader, so it refuses no more than the table reads.
check "a glob * does not cross a slash, so a path one level deeper is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/g/sub/x.parquet');")" "28000"
check "a glob [class] does not match a slash, so a path with a slash in its place is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/c/m_x/.parquet');")" "28000"
check "a class fnmatch may close at either ] still does not match a slash, so a path with a slash in its place is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/lt//x.parquet');")" "28000"
check "an escaped star in a glob matches only a literal star, so another name is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/e/x.parquet');")" "28000"
check "a remote key without a trailing slash is one object, so a path under it is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/key/x.parquet');")" "28000"
check "a path that only starts with a remote directory's name is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/dirx.parquet');")" "28000"
# A local target that passes the check fails opening its file, because the
# directory does not exist: 58P01, and nothing is written.
check "a path that only starts with a local directory's name is not refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/ldirx/x.parquet');")" "58P01"
check "a glob ? does not match a slash, so a path with a slash in its place is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/c/m_/7.parquet');")" "28000"
check "a glob matches the whole name, so a path with more after it is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/g/x.parquet.tmp');")" "28000"
check "a glob . is a literal dot, so a path with another character in its place is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/g/xXparquet');")" "28000"
# An http(s) target that passes the check is refused in the export with 42501,
# because its host is not on objstore_allowed_endpoints, so nothing is written.
check "an http path is refused only for the same key, so a key in another case is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'http://h.example/M_FEB_K.parquet');")" "42501"
check "an http path holding * is one object, not a glob, so another name is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'http://h.example/hg/x.parquet');")" "42501"
check "a gs:// path holding * is one object, not a glob, so another name is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'gs://tierb/gg/x.parquet');")" "28000"
# glob(3) splits a local pattern at each slash, even one inside [=/=], so the
# table reads no file named a.parquet in that directory.
check "a local glob splits at a slash inside an equivalence class, so a path the pieces do not match is not refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/le/a.parquet');")" "58P01"
# The check matches with the reader's own fnmatch and glob(3), through
# pgcolumnar.path_glob_matches, so it agrees with them where a reading of the
# glob in SQL did not (#1957, #1962). A negated class that glibc cannot close
# after a member matched is a literal [ to glibc.
check "a negated glob class that glibc cannot close is a literal [, so a path holding it is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/nsk/[!a');")" "58P02"
check "a negated local glob class that glibc cannot close is a literal [, so a path holding it is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/lnsk/[!a');")" "58P02"
# glob(3) splits a local pattern at every slash first, an escaped one too, so
# [\/] is a directory [ holding a file ]. And a pattern that ends in // names
# the directories it matches, as one that ends in / does.
check "a local glob splits at a backslash-escaped slash in a bracket, so a path of [ and ] is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/lbs/[/]');")" "58P02"
check "a local glob that ends in // matches the directory with one slash, so that path is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/lts/d/');")" "58P02"
# glibc skips a name that starts with '.' unless the pattern spells the dot
# (FNM_PERIOD), in an s3:// key as in a local glob, so the check refuses no
# more than the table reads.
check "a glob * does not match a leading dot in an s3:// key, so a hidden key is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/g/.x.parquet');")" "28000"
check "a local glob * does not match a leading dot, so a hidden file is not refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/lgh/.x.parquet');")" "58P01"
# The detail prints a control character in the path as \xHH, so a table
# owner's path cannot add a line, such as a fake HINT, under the refusal (#1960).
check "the refusal's detail prints a newline in the path as an escape, on one line" \
	"$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', E's3://tierb/nl/a.parquet\\nHINT:  x');")" \
	"Foreign table public.dup_rnl reads path 's3://tierb/nl/a.parquet\\x0AHINT:  x'."
# The class [a\n...] matches ax.parquet, so only the foreign table's path holds
# the newline: the reading is the refusal's SQLSTATE and its HINT line count.
nl_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-v VERBOSITY=verbose -c "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/nlg/ax.parquet');" 2>&1 >/dev/null)"
check "a newline in a foreign table's path adds no HINT line under the refusal" \
	"$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$nl_out" | head -1) with $(grep -c '^HINT:' <<<"$nl_out") HINT lines" \
	"58P02 with 0 HINT lines"
# The foreign table's name is another role's text too, and regclass output
# keeps a newline inside its quotes. The detail escapes the name as it
# escapes the path (#1964 review).
check "the refusal's detail prints a newline in the foreign table's name as an escape, on one line" \
	"$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/tnl/a.parquet');")" \
	"Foreign table public.\"dup_tnl\\x0AHINT:  trust me\" reads path 's3://tierb/tnl/a.parquet'."
# A path may hold the four characters \x0A. The detail prints a backslash
# that comes before an x as \x5C, so that path does not print as a newline.
check "the refusal's detail escapes a literal backslash before an x, so it differs from an escaped newline" \
	"$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/litx/a\\x0A.parquet');")" \
	"Foreign table public.dup_rlitx reads path 's3://tierb/litx/a\\x5Cx0A.parquet'."
# The helper itself: it reads no file, so the planner may fold and share it.
# qerr SQL -> the result, or the error when the query fails
qerr() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -c "$1" 2>&1 | head -1
}
# Its answer follows LC_COLLATE and glibc, which can answer a rare range
# differently from one call to the next, so it is VOLATILE (#1964 review).
check "path_glob_matches is VOLATILE, STRICT and PARALLEL SAFE" \
	"$(qerr "SELECT provolatile::text || proisstrict::text || proparallel::text FROM pg_proc WHERE oid = 'pgcolumnar.path_glob_matches(text, text)'::regprocedure;")" "vtrues"
check "path_glob_matches lets an s3:// class span a slash but splits a local glob there" \
	"$(qerr "SELECT pgcolumnar.path_glob_matches('s3://b/x[a/b]', 's3://b/xa') || '|' || pgcolumnar.path_glob_matches('/w/x[a/b]', '/w/xa');")" "true|false"
# glob(3) lists . and .. in each directory, and a pattern that spells the
# leading dot matches them, so the tree in memory lists them too.
check "path_glob_matches lists . and .. in a local directory, so .* matches both" \
	"$(qerr "SELECT pgcolumnar.path_glob_matches('/w/.*/a.parquet', '/w/./a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/.*/a.parquet', '/w/../a.parquet');")" "true|true"
# glob(3) only ever returns a path built from the tree, so a directory it
# lists must hold the path asked about, not merely some path (#1964 review).
check "path_glob_matches counts only the path asked about, not another path the glob returns" \
	"$(qerr "SELECT pgcolumnar.path_glob_matches('/w/*', '/w/a/b.parquet')::text;")" "false"
# glibc glob(3) splits a pattern at its last slash and, when the part before
# it holds *, ? or [, calls itself on that part. So it calls itself once for
# every slash after the first *, ? or [, even a slash between literal names,
# and each call keeps a copy of the pattern on the stack. The helper refuses a
# pattern with more than 128 such slashes, or of PATH_MAX bytes or more, with
# an error, before glob(3) runs. A crash here would restart the server, and
# any role may call the helper (#1964 review). PATH_MAX is 4096 on Linux, the
# only system the suite runs on, so the checks spell it as 4096.
# ansst SQL -> the query's one value, or its SQLSTATE when it fails
ansst() { local s; s=$(sqlstate "$1"); if [ "$s" = ok ]; then qerr "$1"; else echo "${s:-no answer}"; fi; }
check "path_glob_matches matches a pattern of 128 */ levels and refuses 129 with an error" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches(repeat('*/', 128) || 'x', repeat('a/', 128) || 'x')::text;")|$(ansst "SELECT pgcolumnar.path_glob_matches(repeat('*/', 129) || 'x', repeat('a/', 129) || 'x')::text;")" \
	"true|54000"
check "path_glob_matches counts every level after the first *, so one * and 129 more levels are refused with an error" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/*/' || repeat('a/', 129) || 'x', '/b/' || repeat('a/', 129) || 'x')::text;")" "54000"
check "path_glob_matches counts a level after a ? or a [, so 129 of either are refused with an error" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches(repeat('?/', 129) || 'x', repeat('a/', 129) || 'x')::text;")|$(ansst "SELECT pgcolumnar.path_glob_matches(repeat('[a]/', 129) || 'x', repeat('a/', 129) || 'x')::text;")" \
	"54000|54000"
check "path_glob_matches refuses a pattern of 4096 bytes with an error" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/w/[' || repeat('a', 4091) || ']', '/w/a')::text;")" "54000"
# glibc fnmatch calls itself for each * of an s3:// key, so the helper bounds
# a remote pattern as it bounds a local one: at most 128 *, and shorter than
# PATH_MAX bytes (#1964 review).
check "path_glob_matches matches an s3:// pattern of 128 * and refuses 129 with an error" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('*/', 128) || 'x', 's3://b/' || repeat('a/', 128) || 'x')::text;")|$(ansst "SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('*/', 129) || 'x', 's3://b/' || repeat('a/', 129) || 'x')::text;")" \
	"true|54000"
check "path_glob_matches matches an s3:// pattern of 4095 bytes and refuses one of 4096 with an error" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('a', 4087) || '*', 's3://b/' || repeat('a', 4087) || 'b')::text;")|$(ansst "SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('a', 4088) || '*', 's3://b/' || repeat('a', 4088) || 'b')::text;")" \
	"true|54000"
# No file has a path of PATH_MAX bytes or more: the reader could not open it.
# So the helper answers false for such a path before it builds anything, and a
# long path cannot keep a backend busy past a cancel (#1964 review).
check "path_glob_matches matches a path of 4095 bytes but not one of 4096, which no file can have" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches(repeat('a/', 2047) || '*', repeat('a/', 2047) || 'b')::text;")|$(ansst "SELECT pgcolumnar.path_glob_matches(repeat('a/', 2047) || '*', repeat('a/', 2047) || 'bc')::text;")" \
	"true|false"
check "path_glob_matches answers false for a path of 400000 parts before a 1 s statement_timeout" \
	"$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c statement_timeout=1s' psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -c "SELECT pgcolumnar.path_glob_matches('/w/*', repeat('a/', 400000))::text;" 2>&1 | head -1)" "false"
# An interrupt that raises no error, such as a check that the client is
# still connected, must not turn a match into no match: a false answer lets a
# move overwrite a file a table reads (#1964 review). .* matches . and .., so
# 18 levels of it make a walk of 2^18 names, far longer than the 1 ms timer.
# Every walk is cut short, and the helper gives up with 57014 after 100 walks.
# An answer of false would mean it took a cut walk as a full one.
check "path_glob_matches never answers false for a walk an interrupt that raises no error cut short" \
	"$(env PATH="$PGC_BINDIR:$PATH" PGOPTIONS='-c client_connection_check_interval=1ms' psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v VERBOSITY=sqlstate -c "SELECT pgcolumnar.path_glob_matches(repeat('/.*', 18) || '/x', repeat('/..', 18) || '/x')::text;" 2>&1 | head -1 | sed 's/^ERROR: *//')" \
	"57014"
# The detail escapes a C1 control character, U+0080 to U+009F, by what the
# character means, not by its byte. In LATIN1 the byte 0x85 is the control
# NEL, so it prints as \x85. In WIN1252 the byte 0x80 is the euro sign, a
# printable character, so it prints as written (#1964 review).
# encdetail ENCODING CODE -> the refusal's DETAIL in a database of ENCODING,
# for a foreign table whose path holds chr(CODE), read as UTF-8
encdetail() {
	local db="tier_$(tr 'A-Z' 'a-z' <<<"$1")" out
	psql_run "DROP DATABASE IF EXISTS $db;"
	psql_run "CREATE DATABASE $db TEMPLATE template0 ENCODING '$1' LC_COLLATE 'C' LC_CTYPE 'C';"
	out=$(env PATH="$PGC_BINDIR:$PATH" PGCLIENTENCODING=UTF8 psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$db" \
		-X -At -v ON_ERROR_STOP=1 \
		-c "CREATE EXTENSION pgcolumnar;" \
		-c "CREATE SERVER s FOREIGN DATA WRAPPER pgcolumnar_parquet;" \
		-c "CREATE TABLE p (d int) PARTITION BY RANGE (d);" \
		-c "CREATE TABLE p1 PARTITION OF p FOR VALUES FROM (0) TO (10) USING pgcolumnar;" \
		-c "DO \$\$BEGIN EXECUTE format('CREATE FOREIGN TABLE e (x int) SERVER s OPTIONS (path %L)', 's3://b/n' || chr($2) || '.parquet'); END\$\$;" \
		-c "SELECT pgcolumnar.tier_partition('p1', 's', 's3://b/n' || chr($2) || '.parquet');" 2>&1 >/dev/null)
	psql_run "DROP DATABASE $db;"
	out=$(sed -n 's/^DETAIL: *//p' <<<"$out" | head -1)
	echo "${out:-none}"
}
check "the refusal's detail escapes the LATIN1 control NEL in a path" \
	"$(encdetail LATIN1 133)" "Foreign table public.e reads path 's3://b/n\\x85.parquet'."
check "the refusal's detail prints the WIN1252 euro sign in a path as written" \
	"$(encdetail WIN1252 128)" "Foreign table public.e reads path 's3://b/n€.parquet'."
psql_run "DROP FOREIGN TABLE dup_rnskip, dup_lnskip, dup_lbslash, dup_ltrail, dup_lhide, dup_rnl, dup_rnlg, dup_rlitx;
          DROP FOREIGN TABLE \"dup_tnl
HINT:  trust me\";"
psql_run "DROP FOREIGN TABLE dup_rdir, dup_rglob, dup_rclass, dup_resc, dup_rkey, dup_host, dup_ldir, dup_lglob,
          dup_runt, dup_rbang, dup_rbesc, dup_rdigit, dup_rcaret, dup_lslash, dup_rlead, dup_rbunt, dup_ropen, dup_requiv, dup_rzq, dup_rrange, dup_hglob, dup_gglob,
          dup_rcunt, dup_rspan, dup_rcoll, dup_lequiv, dup_rlate, dup_rlateq, dup_llate, dup_rcdash, dup_rrdash, dup_rnone, dup_rcollab, dup_rvneg, dup_lvneg, dup_rquote, dup_rdslash, dup_rndash, dup_lndash;"
# An Iceberg foreign table has no path option. It reads data files under its
# table location, which is the parent of the directory holding metadata_path
# (#1949). A path under that location is refused; one beside it is not.
psql_run "CREATE FOREIGN TABLE dup_ice (ts timestamptz, v int) SERVER tier_ice
            OPTIONS (metadata_path '$W/ice/metadata/v1.metadata.json');
          CREATE FOREIGN TABLE dup_rice (ts timestamptz, v int) SERVER tier_ice
            OPTIONS (metadata_path 's3a://tierb/wh/ice/metadata/v3.metadata.json');"
mkdir -p "$W/ice/data" && chmod 777 "$W/ice" "$W/ice/data"
check "a local path under an Iceberg foreign table's location is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/ice/data/m_feb.parquet');")" "58P02"
check "an s3:// path under an Iceberg foreign table's location is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/wh/ice/data/x.parquet');")" "58P02"
check "the Iceberg refusal's detail names the foreign table and its location" \
	"$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/wh/ice/data/x.parquet');")" \
	"Foreign table public.dup_rice reads Iceberg files under 's3a://tierb/wh/ice/'."
check "a local path that only starts with an Iceberg location's name is not refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/icex/m_feb.parquet');")" "58P01"
check "an s3:// path beside an Iceberg foreign table's location is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/wh/icex.parquet');")" "28000"
psql_run "DROP FOREIGN TABLE dup_ice, dup_rice;"
# The location is cut from metadata_path as the reader cuts it: a metadata file
# at the root of the file system gives the location '/', and a relative
# metadata_path with no directory above metadata/ gives '.' (#1955 review).
psql_run "CREATE FOREIGN TABLE dup_iroot (ts timestamptz, v int) SERVER tier_ice
            OPTIONS (metadata_path '/v1.metadata.json');"
check "an Iceberg table whose metadata file sits at the root refuses a local path" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nodir/x.parquet');")" "58P02"
check "the root Iceberg refusal's detail names the location '/'" \
	"$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nodir/x.parquet');")" \
	"Foreign table public.dup_iroot reads Iceberg files under '/'."
psql_run "DROP FOREIGN TABLE dup_iroot;
          CREATE FOREIGN TABLE dup_irel (ts timestamptz, v int) SERVER tier_ice
            OPTIONS (metadata_path 'metadata/v1.metadata.json');"
check "an Iceberg table with a relative metadata_path refuses a path under '.'" \
	"$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', './nodir/x.parquet');")" \
	"Foreign table public.dup_irel reads Iceberg files under './'."
psql_run "DROP FOREIGN TABLE dup_irel;"
# The kernel reads // as / and drops a . component, so a local glob or path
# spelled with them names the plainly spelled file (#1965). It also takes a
# name and the .. after it away; the Iceberg reader resolves the location with
# realpath, so a metadata_path that holds .. reads the directory it leads to
# (#1959). The check compares each local path in that lexical form too. Each
# reader check first proves that the reader reads the plain file.
mkdir -p "$W/sp" "$W/sq/sub" && chmod 777 "$W/sp" "$W/sq" "$W/sq/sub"
psql_run "SELECT pgcolumnar.export_parquet('m_feb', '$W/sp/a.parquet');
          SELECT pgcolumnar.export_parquet('m_feb', '$W/sq/a.parquet');
          CREATE FOREIGN TABLE dup_ldbl (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sp//*.parquet');
          CREATE FOREIGN TABLE dup_ldot (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sp/./*.parquet');"
feb_rows="$(q "SELECT count(*) FROM m_feb;")"
check "a local glob spelled with // reads the file spelled plainly" \
	"$(q "SELECT count(*) FROM dup_ldbl;")" "$feb_rows"
check "a local glob spelled with /./ reads the file spelled plainly" \
	"$(q "SELECT count(*) FROM dup_ldot;")" "$feb_rows"
psql_run "DROP FOREIGN TABLE dup_ldot;"
check "a path a local glob spelled with // reads is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sp/a.parquet');")" "58P02"
check "the refusal under a glob spelled with // names the glob as written" \
	"$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sp/a.parquet');")" \
	"Foreign table public.dup_ldbl reads path '$W/sp//*.parquet'."
psql_run "DROP FOREIGN TABLE dup_ldbl;
          CREATE FOREIGN TABLE dup_ldot (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sp/./*.parquet');"
check "a path a local glob spelled with /./ reads is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sp/a.parquet');")" "58P02"
psql_run "DROP FOREIGN TABLE dup_ldot;
          CREATE FOREIGN TABLE dup_lplain (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sq/a.parquet');"
check "a target spelled with // is refused when a foreign table reads the plain spelling" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sq//a.parquet');")" "58P02"
check "a target spelled with /./ is refused when a foreign table reads the plain spelling" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sq/./a.parquet');")" "58P02"
check "a target spelled with a name and .. is refused when a foreign table reads the plain spelling" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sq/sub/../a.parquet');")" "58P02"
# A .. at the root stays at the root, as the kernel reads it (#1978 review).
check "a target spelled with .. at the root is refused when a foreign table reads the plain spelling" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '/..$W/sq/a.parquet');")" "58P02"
# An s3:// key is not a path: // in it is part of the key, so it is not folded.
psql_run "DROP FOREIGN TABLE dup_lplain;
          CREATE FOREIGN TABLE dup_rdbl (x int) SERVER tier_pq OPTIONS (path 's3://tierb/kd//a.parquet');"
check "an s3:// key with // is another key, so the key with one / is not refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/kd/a.parquet');")" "28000"
psql_run "DROP FOREIGN TABLE dup_rdbl;"
# A real Iceberg table, the fixture of test/iceberg_fdw.sh, read through a
# metadata_path that holds .. and one that holds a symbolic link (#1959).
IWH="$W/iwh"
rm -rf "$IWH" && mkdir -p "$IWH/x" && cp -r "$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg/warehouse/db" "$IWH/db"
chmod -R a+rwX "$IWH"
ln -sfn "$IWH/db/events" "$W/iwlink"
IMD="$(cd "$IWH/db/events/metadata" && ls *.metadata.json | sort | tail -1)"
psql_run "CREATE FOREIGN TABLE dup_iup (id bigint, region text, amount int) SERVER tier_ice
            OPTIONS (metadata_path '$IWH/x/../db/events/metadata/$IMD');
          CREATE FOREIGN TABLE dup_ilink (id bigint, region text, amount int) SERVER tier_ice
            OPTIONS (metadata_path '$W/iwlink/metadata/$IMD');"
check "the Iceberg reader reads a table whose metadata_path holds a name and .." \
	"$(q "SELECT count(*) || ' rows, ids ' || string_agg(id::text, ',' ORDER BY id) FROM dup_iup;")" "5 rows, ids 1,2,3,4,5"
check "the Iceberg reader reads a table whose metadata_path goes through a symbolic link" \
	"$(q "SELECT count(*) || ' rows, ids ' || string_agg(id::text, ',' ORDER BY id) FROM dup_ilink;")" "5 rows, ids 1,2,3,4,5"
psql_run "DROP FOREIGN TABLE dup_ilink;"
check "a path under an Iceberg location reached through a name and .. is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$IWH/db/events/data/m_feb.parquet');")" "58P02"
check "the refusal under an Iceberg location with .. names the location as written" \
	"$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$IWH/db/events/data/m_feb.parquet');")" \
	"Foreign table public.dup_iup reads Iceberg files under '$IWH/x/../db/events/'."
# A symbolic link is not resolved: that needs the file system at the time of
# the call, and the link can change after it. docs/sql-reference.md states the
# limit. This check holds it, so a change to it shows here and in the docs.
psql_run "DROP FOREIGN TABLE dup_iup;
          CREATE FOREIGN TABLE dup_ilink (id bigint, region text, amount int) SERVER tier_ice
            OPTIONS (metadata_path '$W/iwlink/metadata/$IMD');"
check "a path under an Iceberg location reached through a symbolic link is not refused, as documented" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$IWH/db/events/data/m_feb.parquet');")" "ok"
psql_run "DROP FOREIGN TABLE dup_ilink;"
# Only pgcolumnar's Parquet wrapper reads its path option as a Parquet path
# (#1949). The path option of a table under another wrapper means what that
# wrapper says, so it refuses nothing. A wrapper made with the Parquet handler
# under another name is still the Parquet reader.
psql_run "CREATE FOREIGN DATA WRAPPER tier_other_fdw;
          CREATE SERVER tier_other FOREIGN DATA WRAPPER tier_other_fdw;
          CREATE FOREIGN TABLE dup_oroot (x int) SERVER tier_other OPTIONS (path '/');
          CREATE FOREIGN TABLE dup_osame (x int) SERVER tier_other OPTIONS (path '$W/oth/x.parquet');
          CREATE FOREIGN DATA WRAPPER tier_pq2 HANDLER pgcolumnar.parquet_fdw_handler
            VALIDATOR pgcolumnar.parquet_fdw_validator;
          CREATE SERVER tier_pq2s FOREIGN DATA WRAPPER tier_pq2;
          CREATE FOREIGN TABLE dup_pq2 (x int) SERVER tier_pq2s OPTIONS (path '$W/pq2/x.parquet');"
check "a path option of '/' under another wrapper does not refuse a local path" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nodir/x.parquet');")" "58P01"
check "a path option under another wrapper does not refuse the same path" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/oth/x.parquet');")" "58P01"
check "a path a wrapper with the Parquet handler under another name reads is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/pq2/x.parquet');")" "58P02"
# The target server is also chosen by its wrapper's handler, as the path
# check chooses the Parquet reader (#1963). A move through tier_pq2s writes
# and reads back its file; a wrapper without the handler is still refused.
mkdir -p "$W/pq2t" && chmod 777 "$W/pq2t"
check "a server on a wrapper with the Parquet handler under another name is accepted as the target" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -q \
		-v VERBOSITY=sqlstate -c "BEGIN" -c "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq2s', '$W/pq2t/m_feb.parquet');" \
		-c "ROLLBACK" 2>&1 | head -1)" "$feb_rows"
check "a server on a wrapper without the Parquet handler is refused as the target" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_other', '$W/pq2t/o.parquet');")|$([ -e "$W/pq2t/o.parquet" ] && echo file || echo nofile)" \
	"42809|nofile"
# export_parquet takes the server by the same rule, in C (#1963 review).
check "export_parquet through a server on a wrapper with the Parquet handler under another name writes the file" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -q \
		-v VERBOSITY=sqlstate -c "SELECT pgcolumnar.export_parquet('m_feb', '$W/pq2t/e.parquet', 'tier_pq2s');" 2>&1 | head -1)|$([ -e "$W/pq2t/e.parquet" ] && echo file || echo nofile)" \
	"$feb_rows|file"
# A glob too large for glibc raises 54000 in path_glob_matches. The move
# still fails closed, and the error names the table that blocks it (#1974).
# A table that really reads the path is still reported as such.
psql_run "CREATE FOREIGN TABLE dup_deep (x int) SERVER tier_pq
            OPTIONS (path '/elsewhere/$(printf '*/%.0s' $(seq 200))x');"
check "a foreign table whose glob is too large to match blocks the move with an error that names it" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nodir/x.parquet');")|$(errtext "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nodir/x.parquet');")|$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nodir/x.parquet');")" \
	"54000|glob pattern has too many levels to match: 200|A glob pattern may have at most 128 levels after the first *, ? or [. Foreign table public.dup_deep reads path '/elsewhere/$(printf '*/%.0s' $(seq 200))x', so tier_partition cannot tell whether it reads the target."
check "a path another foreign table reads is still refused as read beside a glob too large to match" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/pq2/x.parquet');")" "58P02"
check "a foreign table whose glob is too large to match does not block a remote target" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/deep/x.parquet');")" "28000"
# The loop goes on past a glob too large to match, so a later glob that
# really reads the path is still reported as 58P02 (#1978 review). dup_deep
# was made first, so the loop meets it first.
psql_run "CREATE FOREIGN TABLE dup_gsp (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sp/*.parquet');"
check "a path a glob reads is refused as read when a glob too large to match comes first" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sp/a.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sp/a.parquet');")" \
	"58P02|Foreign table public.dup_gsp reads path '$W/sp/*.parquet'."
psql_run "DROP FOREIGN TABLE dup_gsp, dup_deep;"
# A glob over the limit as written can be within it once folded. The folded
# form is matched on its own, so a table that reads the path is reported as
# 58P02, not 54000 (#1978 review).
psql_run "CREATE FOREIGN TABLE dup_fold2 (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/s[p]$(printf '/.%.0s' $(seq 200))/a.parquet');"
check "a glob over the limit as written but not once folded reads the file spelled plainly" \
	"$(q "SELECT count(*) FROM dup_fold2;")" "$feb_rows"
check "a path a glob reads once folded is refused as read although the glob as written is too large to match" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sp/a.parquet');")" "58P02"
psql_run "DROP FOREIGN TABLE dup_fold2;"
# A remote glob with more than 128 * blocks a remote move with 54000 too, and
# the detail names the table (#1974, #1978 review).
psql_run "CREATE FOREIGN TABLE dup_rdeep (x int) SERVER tier_pq
            OPTIONS (path 's3://tierb/$(printf '*%.0s' $(seq 130))');"
check "a foreign table whose s3:// glob is too large to match blocks a remote target with an error that names it" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/k/x.parquet');")|$(errdetail "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/k/x.parquet');")" \
	"54000|A glob pattern may have at most 128 * in an s3:// key. Foreign table public.dup_rdeep reads path 's3://tierb/$(printf '*%.0s' $(seq 130))', so tier_partition cannot tell whether it reads the target."
psql_run "DROP FOREIGN TABLE dup_rdeep;"
# A table whose path is the target, plainly, is found without a call of the
# matcher: such rows come first, and the loop stops at the first table that
# reads the path (#1978 review). dup_gzz is a glob made first, which matches
# nothing; it would cost one call if it came first.
matcher_calls() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -q \
		-c "SET track_functions = 'all'" \
		-c "DO \$\$ DECLARE b bigint; r text; BEGIN
		      b := coalesce(pg_stat_get_xact_function_calls('pgcolumnar.path_glob_matches(text,text)'::regprocedure), 0);
		      BEGIN $1;
		            RAISE EXCEPTION 'done' USING ERRCODE = 'P0001';
		      EXCEPTION WHEN OTHERS THEN r := CASE WHEN SQLERRM = 'done' THEN 'ok' ELSE SQLSTATE END; END;
		      PERFORM set_config('pgc.calls', r || '|' || (coalesce(pg_stat_get_xact_function_calls(
		          'pgcolumnar.path_glob_matches(text,text)'::regprocedure), 0) - b) || ' matcher calls', false);
		    END \$\$; SELECT current_setting('pgc.calls');" 2>&1
}
psql_run "CREATE FOREIGN TABLE dup_gzz (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/zz/*.parquet');
          CREATE FOREIGN TABLE dup_lsq (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sq/a.parquet');"
check "premise: the counter of matcher calls counts each call" \
	"$(matcher_calls "PERFORM pgcolumnar.path_glob_matches('/a*', '/a' || g) FROM generate_series(1, 2) g")" \
	"ok|2 matcher calls"
check "a path a foreign table reads as written is refused without a call of the glob matcher" \
	"$(matcher_calls "PERFORM pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sq/a.parquet')")" "58P02|0 matcher calls"
psql_run "DROP FOREIGN TABLE dup_gzz, dup_lsq;"
# glob(3) unescapes a name without a glob character, so \. is . and \.\. is
# .. to the reader. The fold reads such a name as the reader does (#1978
# review). Each reader check first proves that the reader reads the plain file.
psql_run "CREATE FOREIGN TABLE dup_lesc (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sp/\./*.parquet');
          CREATE FOREIGN TABLE dup_lesc2 (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sq/sub/\.\./*.parquet');"
check "a local glob spelled with an escaped . reads the file spelled plainly" \
	"$(q "SELECT count(*) FROM dup_lesc;")" "$feb_rows"
check "a local glob spelled with an escaped .. reads the file spelled plainly" \
	"$(q "SELECT count(*) FROM dup_lesc2;")" "$feb_rows"
check "a path a local glob spelled with an escaped . reads is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sp/a.parquet');")" "58P02"
check "a path a local glob spelled with an escaped .. reads is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sq/a.parquet');")" "58P02"
psql_run "DROP FOREIGN TABLE dup_lesc, dup_lesc2;"
# glob(3) lists . and .. in each directory, and a glob name that spells the
# leading dot, such as .* or .?, matches them. So a table on sd/.*/a.parquet
# reads sd/./a.parquet, and one on sd/sub/.?/a.parquet reads
# sd/sub/../a.parquet: both are sd/a.parquet. path_glob_matches compares the
# lexical form of each path glob(3) returns with that of the target (#1982).
# A * does not match a leading dot (FNM_PERIOD), so a table on nd/*/a.parquet
# never reads nd/a.parquet. Each reader check first proves what the reader reads.
mkdir -p "$W/sd/sub" && chmod 777 "$W/sd" "$W/sd/sub"
psql_run "SELECT pgcolumnar.export_parquet('m_feb', '$W/sd/a.parquet');
          CREATE FOREIGN TABLE dup_dstar (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sd/.*/a.parquet');
          CREATE FOREIGN TABLE dup_dq (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sd/sub/.?/a.parquet');"
check "premise: a local glob through a .* name reads the file spelled plainly" \
	"$(q "SELECT count(*) FROM dup_dstar;")" "$feb_rows"
check "premise: a local glob through a .? name reads the file one .. above" \
	"$(q "SELECT count(*) FROM dup_dq;")" "$feb_rows"
psql_run "DROP FOREIGN TABLE dup_dq;"
check "a path a local glob reads through a .* name is refused, and the detail names that table" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sd/a.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sd/a.parquet');")" \
	"58P02|Foreign table public.dup_dstar reads path '$W/sd/.*/a.parquet'."
psql_run "DROP FOREIGN TABLE dup_dstar;
          CREATE FOREIGN TABLE dup_dq (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sd/sub/.?/a.parquet');"
check "a path a local glob reads through a .? name that matches .. is refused, and the detail names that table" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sd/a.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sd/a.parquet');")" \
	"58P02|Foreign table public.dup_dq reads path '$W/sd/sub/.?/a.parquet'."
psql_run "CREATE FOREIGN TABLE dup_dstar (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sd/.*/a.parquet');"
check "a path no table reads is not refused beside globs through .* and .? names" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nd/b.parquet');")" "58P01"
psql_run "DROP FOREIGN TABLE dup_dstar, dup_dq;
          CREATE FOREIGN TABLE dup_dplain (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/nd/*/a.parquet');"
check "a path a local glob * reaches only through . or .. is not refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nd/a.parquet');")" "58P01"
psql_run "DROP FOREIGN TABLE dup_dplain;"
# A wildcard name, then a later name that matches .., lets a glob climb out of
# a directory it never names: a table on sw/*/.?/a.parquet reads sw/a.parquet
# whenever sw holds any directory, as * matches it and .? matches .. (#1991).
# path_glob_matches does not know which directory * matched, so it walks a
# stand-in for it. A table on nw/*/a.parquet still refuses nothing above *.
# The directory nw does not exist, so a move that is not refused fails 58P01.
mkdir -p "$W/sw/d" && chmod 777 "$W/sw" "$W/sw/d"
psql_run "SELECT pgcolumnar.export_parquet('m_feb', '$W/sw/a.parquet');
          CREATE FOREIGN TABLE dup_wdq (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/sw/*/.?/a.parquet');"
check "premise: a local glob through a * name and then a .? name reads the file one .. above" \
	"$(q "SELECT count(*) FROM dup_wdq;")" "$feb_rows"
check "a path a local glob reads through a * name and then a .? name is refused, and the detail names that table" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sw/a.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/sw/a.parquet');")" \
	"58P02|Foreign table public.dup_wdq reads path '$W/sw/*/.?/a.parquet'."
check "a path no table reads is not refused beside a glob through a * name and then a .? name" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nw/a.parquet');")" "58P01"
psql_run "DROP FOREIGN TABLE dup_wdq;
          CREATE FOREIGN TABLE dup_wplain (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '$W/nw/*/a.parquet');"
check "a path above a local glob * with no later name that matches .. is not refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/nw/a.parquet');")" "58P01"
psql_run "DROP FOREIGN TABLE dup_wplain;"
# A relative path reads from the server's working directory, which is the
# data directory, and export_parquet writes a relative path there too. So a
# table on ../*/tier2001.parquet reads tier2001.parquet in the data
# directory: .. climbs to its parent and * matches the data directory's own
# name (#2001). path_glob_matches resolves a relative pattern and a relative
# target against the data directory before it walks, so the move is refused
# for the path spelled either way. A path the glob does not reach is not.
psql_run "SELECT pgcolumnar.export_parquet('m_feb', 'tier2001.parquet');
          CREATE FOREIGN TABLE dup_rel (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path '../*/tier2001.parquet');"
check "premise: export_parquet writes a relative path in the data directory, and a relative glob that climbs above it reads that file" \
	"$(q "SELECT (pg_stat_file(current_setting('data_directory') || '/tier2001.parquet')).size > 0;")|$(q "SELECT count(*) FROM dup_rel;")" \
	"t|$feb_rows"
check "a path a relative glob reads through a .. above the data directory and a * name is refused, spelled in full or relative, and the detail names that table" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', current_setting('data_directory') || '/tier2001.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'tier2001.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 'tier2001.parquet');")" \
	"58P02|58P02|Foreign table public.dup_rel reads path '../*/tier2001.parquet'."
check "a path no table reads is not refused beside a relative glob through a .. above the data directory" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', current_setting('data_directory') || '/nrel/tier2001.parquet');")" "58P01"
psql_run "DROP FOREIGN TABLE dup_rel;"
# The comparisons that need no glob (the same path, a path under a
# directory, a path under an Iceberg location, and the folded form of each)
# resolve a relative path against the data directory too, by the rule
# path_glob_matches uses (#2004). So a table on t2004/a.parquet refuses the
# path spelled in full under the data directory, and a table on a path in
# full refuses the relative spelling. A path no table reads is still moved,
# and a remote key is never resolved: s3:/tierb/r04.parquet is a local path
# in the data directory, not the key s3://tierb/r04.parquet. None of the
# directories under the data directory exists, so a move that is not refused
# fails 58P01; tier2004.parquet does exist.
D="$(q "SELECT current_setting('data_directory');")"
DB="$(basename "$D")"
psql_run "SELECT pgcolumnar.export_parquet('m_feb', 'tier2004.parquet');
          CREATE FOREIGN TABLE dup_r04 (ts timestamptz, dev int, v bigint, note text) SERVER tier_pq
            OPTIONS (path 'tier2004.parquet');
          CREATE FOREIGN TABLE dup_r04x (x int) SERVER tier_pq OPTIONS (path 't2004/a.parquet');
          CREATE FOREIGN TABLE dup_a04 (x int) SERVER tier_pq OPTIONS (path '$D/t2004/b.parquet');
          CREATE FOREIGN TABLE dup_rd04 (x int) SERVER tier_pq OPTIONS (path 't2004d');
          CREATE FOREIGN TABLE dup_ad04 (x int) SERVER tier_pq OPTIONS (path '$D/t2004e/');
          CREATE FOREIGN TABLE dup_rdd04 (x int) SERVER tier_pq OPTIONS (path '../$DB/t2004f/a.parquet');
          CREATE FOREIGN TABLE dup_add04 (x int) SERVER tier_pq OPTIONS (path '$D/t2004h/a.parquet');
          CREATE FOREIGN TABLE dup_ir04 (ts timestamptz, v int) SERVER tier_ice
            OPTIONS (metadata_path 'ice2004/metadata/v1.metadata.json');
          CREATE FOREIGN TABLE dup_s04 (x int) SERVER tier_pq OPTIONS (path 's3:/tierb/r04.parquet');
          CREATE FOREIGN TABLE dup_rs04 (x int) SERVER tier_pq OPTIONS (path 's3://tierb/r05.parquet');"
check "premise: a foreign table on a relative path without a glob reads the file export_parquet wrote under that name in the data directory" \
	"$(q "SELECT (pg_stat_file(current_setting('data_directory') || '/tier2004.parquet')).size > 0;")|$(q "SELECT count(*) FROM dup_r04;")" \
	"t|$feb_rows"
check "a path a foreign table reads as a relative path without a glob is refused spelled in full under the data directory, and the detail names that table" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/tier2004.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/t2004/a.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/t2004/a.parquet');")" \
	"58P02|58P02|Foreign table public.dup_r04x reads path 't2004/a.parquet'."
check "a relative target is refused when a foreign table reads it spelled in full under the data directory, and the detail names that table" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 't2004/b.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 't2004/b.parquet');")" \
	"58P02|Foreign table public.dup_a04 reads path '$D/t2004/b.parquet'."
check "a path under a directory a foreign table reads is refused when one of the two is relative and the other is spelled in full under the data directory" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/t2004d/sub/x.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 't2004e/sub/x.parquet');")" \
	"58P02|58P02"
check "a relative path whose .. climbs out of the data directory and back into it is refused against the file it names, spelled in full or relative" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/t2004f/a.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 't2004f/a.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '../$DB/t2004h/a.parquet');")" \
	"58P02|58P02|58P02"
check "a path under the location of an Iceberg table on a relative metadata_path is refused spelled in full under the data directory, and the detail names that location" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/ice2004/data/x.parquet');")|$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/ice2004/data/x.parquet');")" \
	"58P02|Foreign table public.dup_ir04 reads Iceberg files under 'ice2004/'."
check "a relative path no table reads is not refused beside tables on relative paths and on paths in full under the data directory" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 't2004n/a.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$D/t2004n/a.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '../$DB/t2004n/a.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 't2004dx/a.parquet');")" \
	"58P01|58P01|58P01|58P01"
check "a remote key is not resolved against the data directory, so a local path that spells it there is not refused and the key itself still is" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/r04.parquet');")|$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3:/tierb/r05.parquet');")|$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/r05.parquet');")" \
	"28000|58P01|58P02"
check "a NULL path is refused as null beside foreign tables on relative paths" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', NULL);")" "22004"
psql_run "DROP FOREIGN TABLE dup_r04, dup_r04x, dup_a04, dup_rd04, dup_ad04, dup_rdd04, dup_add04, dup_ir04, dup_s04, dup_rs04;"
# The Iceberg reader is also chosen by its handler, not by the wrapper's name.
# A path option under a wrapper with any handler but the Parquet one refuses
# nothing: the Iceberg handler does not read path, and a handler pgcolumnar
# does not know is another wrapper. pgcolumnar knows a reader only by the SQL
# function pgcolumnar.parquet_fdw_handler or pgcolumnar.iceberg_fdw_handler;
# here the unknown handler is the Iceberg reader's C function declared under
# another SQL name (#1955 review).
psql_run "CREATE FOREIGN DATA WRAPPER tier_ice2 HANDLER pgcolumnar.iceberg_fdw_handler
            VALIDATOR pgcolumnar.iceberg_fdw_validator;
          CREATE SERVER tier_ice2s FOREIGN DATA WRAPPER tier_ice2;
          CREATE FOREIGN TABLE dup_ice2 (ts timestamptz, v int) SERVER tier_ice2s
            OPTIONS (metadata_path '$W/ice2/metadata/v1.metadata.json');
          CREATE FOREIGN DATA WRAPPER tier_ice3 HANDLER pgcolumnar.iceberg_fdw_handler;
          CREATE SERVER tier_ice3s FOREIGN DATA WRAPPER tier_ice3;
          CREATE FOREIGN TABLE dup_ipath (x int) SERVER tier_ice3s OPTIONS (path '$W/ipath/x.parquet');
          CREATE FUNCTION public.tier_unknown_handler() RETURNS fdw_handler
            AS 'pgcolumnar', 'pgcolumnar_iceberg_fdw_handler' LANGUAGE C;
          CREATE FOREIGN DATA WRAPPER tier_unk HANDLER public.tier_unknown_handler;
          CREATE SERVER tier_unks FOREIGN DATA WRAPPER tier_unk;
          CREATE FOREIGN TABLE dup_upath (x int) SERVER tier_unks OPTIONS (path '$W/upath/x.parquet');"
check "a path under the location of a table on a wrapper with the Iceberg handler under another name is refused" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/ice2/data/x.parquet');")" "58P02"
# dup_oroot (path '/' under tier_other_fdw) is still here, so the SQLSTATE
# alone cannot tell which table refused; the detail names dup_ice2 (#1955 review).
check "the refusal under a wrapper with the Iceberg handler under another name names that table and its location" \
	"$(errdetail "BEGIN" "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/ice2/data/x.parquet');")" \
	"Foreign table public.dup_ice2 reads Iceberg files under '$W/ice2/'."
check "a path option under a wrapper with the Iceberg handler does not refuse the same path" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/ipath/x.parquet');")" "58P01"
check "a path option under a wrapper with a handler pgcolumnar does not know does not refuse the same path" \
	"$(rolled "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/upath/x.parquet');")" "58P01"
psql_run "DROP FOREIGN DATA WRAPPER tier_other_fdw, tier_pq2, tier_ice2, tier_ice3, tier_unk CASCADE;
          DROP FUNCTION public.tier_unknown_handler();"
check "and the refusals moved nothing" "$(digest m)" "$rb"
check "the other partition is still local after the refusals" \
	"$(q "SELECT relkind FROM pg_class WHERE oid = 'm_feb'::regclass;")" "r"

# ---- the parent's lock ------------------------------------------------------
build k
kb="$(digest k)"
lock="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "SELECT pgcolumnar.tier_partition('k_jan', 'tier_pq', '$W/k_jan.parquet')" \
	-c "SELECT coalesce(string_agg(mode, ',' ORDER BY mode), 'none') FROM pg_locks
	    WHERE relation = 'k'::regclass AND pid = pg_backend_pid() AND granted" \
	-c "ROLLBACK" 2>&1)"
# Line 2 is the call's result: a rollback of a call that never ran proves nothing.
check "inside the transaction the call moved the cold partition" "$(sed -n 2p <<<"$lock")" "8928"
check "the call holds ACCESS EXCLUSIVE on the parent until commit, as the docs say" \
	"$(grep -o 'AccessExclusiveLock' <<<"$lock")" "AccessExclusiveLock"
check "a rollback after the whole call leaves the rows local, exactly once" \
	"$(digest k)|$(jan_shape k)" "$kb|r:pgcolumnar:$JAN_BOUND"

# ---- a write in flight when the move starts ---------------------------------
# Without the SHARE lock the export does not see an uncommitted insert, DETACH
# then waits for it to commit, and DROP takes the row with the partition: lost.
# With it, the move waits for the insert and exports the row.
build w
wb="$(q "SELECT count(*) FROM w;")"; wj="$(q "SELECT count(*) FROM w_jan;")"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "INSERT INTO w_jan VALUES ('2026-01-31 23:59:00+00', 99, 4242, 'in flight')" \
	-c "SELECT pg_sleep(3)" -c "COMMIT" >/dev/null 2>&1 &
writer=$!
sleep 1
t0=$(date +%s%N)
moved="$(q "SELECT pgcolumnar.tier_partition('w_jan', 'tier_pq', '$W/w_jan.parquet');")"
waited=$(( ($(date +%s%N) - t0) / 1000000 ))
wait "$writer"
echo "-- the move waited ${waited} ms for the writer"
check_num "premise: the move started while the insert was in flight, and waited for it" \
	"$([ "$waited" -ge 1000 ] && echo 1 || echo "waited only $waited ms")" "1"
check "a write in flight when the move starts is in the exported rows" \
	"$moved|$(q "SELECT count(*) FROM w;")|$(q "SELECT count(*) FROM w WHERE note = 'in flight';")" \
	"$((wj + 1))|$((wb + 1))|1"

# ---- isolation: a writer that commits between the caller's snapshot and the lock
# Measured by @jdatcmd on #1295: the SHARE lock does not refresh a REPEATABLE READ
# snapshot, so the export missed rows that DROP then took with the partition.
iso_sqlstate() { sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' "$1" | head -1; }
n=0
for lvl in "read committed" "repeatable read" "serializable"; do
	n=$((n + 1)); build i
	ib="$(q "SELECT count(*) FROM i;")"; ij="$(q "SELECT count(*) FROM i_jan;")"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v VERBOSITY=sqlstate -c "BEGIN ISOLATION LEVEL $lvl" -c "SELECT 'seen=' || count(*) FROM i_jan" \
		-c "SELECT pg_sleep(2)" \
		-c "SELECT 'moved=' || pgcolumnar.tier_partition('i_jan', 'tier_pq', '$W/i_$n.parquet')" \
		-c "COMMIT" > "$W/iso_$n.out" 2>&1 &
	mover=$!
	sleep 1
	q "INSERT INTO i_jan VALUES ('2026-01-20 12:00:00+00', 7, 7, 'committed between');" >/dev/null
	wait "$mover"
	check_num "premise: at $lvl the caller's snapshot was taken before the writer committed" \
		"$(sed -n 's/^seen=//p' "$W/iso_$n.out")" "$ij"
	check "at $lvl a row committed between the caller's snapshot and the lock survives" \
		"$(q "SELECT count(*) FROM i;")|$(q "SELECT count(*) FROM i WHERE note = 'committed between';")" "$((ib + 1))|1"
	if [ "$lvl" = "read committed" ]; then
		check "at read committed the move goes ahead and exports that row" \
			"$(sed -n 's/^moved=//p' "$W/iso_$n.out")|$(q "SELECT relkind FROM pg_class WHERE oid = 'i_jan'::regclass;")" "$((ij + 1))|f"
	else
		check "at $lvl the move is refused, and the rows stay local" \
			"$(iso_sqlstate "$W/iso_$n.out")|$(q "SELECT relkind FROM pg_class WHERE oid = 'i_jan'::regclass;")" "25000|r"
	fi
done

# ---- refused up front: what ATTACH or DROP would trip over --------------------
# Each refusal leaves the rows local and writes no file: before this, a UNIQUE
# index on the parent surfaced at ATTACH, after the export, as an orphan file.
refused_cleanly() {	# refused_cleanly TABLE PATH -> "rows|relkind|file"
	echo "$(digest "$1")|$(q "SELECT relkind FROM pg_class WHERE oid = '${1}_jan'::regclass;")|$([ -e "$2" ] && echo file || echo nofile)"
}
build u
psql_run "CREATE UNIQUE INDEX u_key ON u (ts);"
ub="$(digest u)"
check "a parent with a UNIQUE index is refused before anything moves" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('u_jan', 'tier_pq', '$W/u.parquet');")|$(refused_cleanly u "$W/u.parquet")" \
	"0A000|$ub|r|nofile"

# A server that does not exist (#1341). Without its own branch the NULL wrapper name
# compares as NULL, nothing is refused, and core's own "server does not exist"
# arrives only at CREATE FOREIGN TABLE, after the export has written the file.
build ns
nsb="$(digest ns)"
check "a server that does not exist is refused before anything moves" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('ns_jan', 'tier_no_such', '$W/ns.parquet');")|$(refused_cleanly ns "$W/ns.parquet")" \
	"42704|$nsb|r|nofile"

# The fault hook's own guard (#1341): a step that is not one of tier_partition's
# must be refused, so a typo cannot leave a test believing it injected a fault.
check "a fault step that is not one of tier_partition's is refused" \
	"$(sqlstate "SET pgcolumnar.tier_fault_after = 'exprot';")" "22023"

build d
db="$(digest d)"
# Each dependent made and removed in statements of its own, with a premise that it
# exists: a multi-statement -c is one transaction, and in the first draft a failed
# CREATE rolled back the DROP of the case before, so the foreign-key arm passed
# on a leftover trigger and tested nothing.
dep_case() {	# dep_case PATH PREMISE_SQL -> "premise|sqlstate|rows|relkind|file"
	echo "$(q "$2")|$(sqlstate "SELECT pgcolumnar.tier_partition('d_jan', 'tier_pq', '$1');")|$(refused_cleanly d "$1")"
}
psql_run "CREATE VIEW d_view AS SELECT * FROM d_jan;"
check "a partition a view depends on is refused before anything moves" \
	"$(dep_case "$W/d1.parquet" "SELECT count(*) FROM pg_views WHERE viewname = 'd_view';")" "1|2BP01|$db|r|nofile"
psql_run "DROP VIEW d_view;"
psql_run "ALTER TABLE d_jan ENABLE ROW LEVEL SECURITY;"
psql_run "CREATE POLICY d_pol ON d_jan USING (dev > 0);"
check "a partition with a row-level policy is refused before anything moves" \
	"$(dep_case "$W/d2.parquet" "SELECT count(*) FROM pg_policy WHERE polname = 'd_pol';")" "1|2BP01|$db|r|nofile"
psql_run "DROP POLICY d_pol ON d_jan;"
psql_run "ALTER TABLE d_jan DISABLE ROW LEVEL SECURITY;"
psql_run "CREATE FUNCTION d_trg() RETURNS trigger LANGUAGE plpgsql AS \$\$BEGIN RETURN NEW; END\$\$;"
psql_run "CREATE TRIGGER d_local BEFORE UPDATE ON d_jan FOR EACH ROW EXECUTE FUNCTION d_trg();"
check "a partition with a trigger of its own is refused before anything moves" \
	"$(dep_case "$W/d3.parquet" "SELECT count(*) FROM pg_trigger WHERE tgname = 'd_local';")" "1|2BP01|$db|r|nofile"
psql_run "DROP TRIGGER d_local ON d_jan;"
# A foreign key REFERENCING a columnar table is refused by pgcolumnar itself (row
# locking), so the case that can arise is one the partition holds: a foreign
# table cannot carry it, and DROP would take it.
psql_run "CREATE TABLE d_dev (k int PRIMARY KEY) USING heap;"
psql_run "INSERT INTO d_dev SELECT g FROM generate_series(0, 12) g;"
psql_run "ALTER TABLE d_jan ADD CONSTRAINT d_fk FOREIGN KEY (dev) REFERENCES d_dev (k);"
check "a partition with a foreign key of its own is refused before anything moves" \
	"$(dep_case "$W/d4.parquet" "SELECT count(*) FROM pg_constraint WHERE conname = 'd_fk';")" "1|2BP01|$db|r|nofile"
psql_run "ALTER TABLE d_jan DROP CONSTRAINT d_fk;"
check_num "with the dependents gone, the same partition moves" \
	"$(q "SELECT pgcolumnar.tier_partition('d_jan', 'tier_pq', '$W/d5.parquet');")" "8928"

# ---- what the foreign table carries over --------------------------------------
build a
psql_run "CREATE ROLE tier_reader; GRANT SELECT ON a_jan TO tier_reader;
          COMMENT ON TABLE a_jan IS 'January, cold';"
check_num "the cold partition was moved with its grant and comment set" \
	"$(q "SELECT pgcolumnar.tier_partition('a_jan', 'tier_pq', '$W/a.parquet');")" "8928"
check "the foreign partition keeps the partition's grants and comment" \
	"$(q "SELECT relkind::text || '|' || has_table_privilege('tier_reader', 'a_jan', 'SELECT') || '|' ||
	         coalesce(obj_description('a_jan'::regclass, 'pg_class'), '-') FROM pg_class WHERE oid = 'a_jan'::regclass;")" \
	"f|true|January, cold"

# ---- an insert routed to the cold partition, racing the swap ------------------
# The mover holds SHARE on the partition and then asks for ACCESS EXCLUSIVE on the
# parent; an insert through the parent holds ROW EXCLUSIVE on the parent and waits
# for the partition. Whichever side the deadlock detector aborts, every row must
# be exactly once.
build r
rb="$(q "SELECT count(*) FROM r;")"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -v VERBOSITY=sqlstate \
	-c "SET pgcolumnar.tier_fault_after = 'pause'" \
	-c "SELECT 'moved=' || pgcolumnar.tier_partition('r_jan', 'tier_pq', '$W/r.parquet')" > "$W/race_mover.out" 2>&1 &
mover=$!
sleep 1
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -v VERBOSITY=sqlstate \
	-c "INSERT INTO r VALUES ('2026-01-26 08:00:00+00', 3, 3, 'raced')" > "$W/race_writer.out" 2>&1
wait "$mover"
echo "-- race: mover [$(tr '\n' ' ' < "$W/race_mover.out")] writer [$(tr '\n' ' ' < "$W/race_writer.out")]"
raced="$(q "SELECT count(*) FROM r WHERE note = 'raced';")"
check_num "premise: one side of the race gave way or both went through, and nothing hung" \
	"$(grep -c 'moved=\|ERROR' "$W/race_mover.out")" "1"
# THE WANT COMES FROM THE WRITER, NOT FROM THE TABLE (#1339). Reading `raced` back
# and putting it on both sides held for 0 and 1 alike, so a row the writer was told
# had committed and the move then dropped passed: with tier_partition's LOCK ... IN
# SHARE MODE removed, the insert commits into the partition during the pause and
# the DROP takes it. A committed insert must be there exactly once, a refused one
# not at all.
w_ok="$(grep -c '^INSERT 0 1$' "$W/race_writer.out")"
check "an insert racing the move leaves every row exactly once, whichever side gives way" \
	"$(q "SELECT count(*) FROM r;")|$raced" "$((rb + w_ok))|$w_ok"

# ---- a bounded wait for the parent's lock -------------------------------------
build l
lb="$(digest l)"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "SELECT count(*) FROM l" -c "SELECT pg_sleep(6)" -c "COMMIT" >/dev/null 2>&1 &
reader=$!
sleep 1
t0=$(date +%s%N)
lk="$(sqlstate "SELECT pgcolumnar.tier_partition('l_jan', 'tier_pq', '$W/l.parquet', lock_wait => '1 second');")"
lms=$(( ($(date +%s%N) - t0) / 1000000 ))
wait "$reader"
echo "-- the move gave up on the parent's lock after ${lms} ms"
check "a parent held by a long reader times the move out, as lock_wait says" \
	"$lk|$([ "$lms" -lt 4000 ] && echo prompt || echo "took $lms ms")" "55P03|prompt"
check "and the timed-out move leaves the rows local, with nothing attached" \
	"$(digest l)|$(q "SELECT relkind FROM pg_class WHERE oid = 'l_jan'::regclass;")" "$lb|r"

# The bound is set for the move's own locks only: set_config(..., true) would
# otherwise last to the end of the caller's transaction.
build t
lt="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "SET LOCAL lock_timeout = '7s'" \
	-c "SELECT 'moved=' || pgcolumnar.tier_partition('t_jan', 'tier_pq', '$W/t.parquet', lock_wait => '3 seconds')" \
	-c "SHOW lock_timeout" -c "ROLLBACK" 2>&1)"
check "the move ran inside the caller's transaction" "$(sed -n 's/^moved=//p' <<<"$lt")" "8928"
check "after the move the caller's lock_timeout is the caller's again" "$(sed -n '/^moved=/{n;p}' <<<"$lt")" "7s"

# ---- a retry after a failure, over the file the failure left ------------------
build o
ob="$(digest o)"
o_err="$(errtext "SET pgcolumnar.tier_fault_after = 'export'" \
	"SELECT pgcolumnar.tier_partition('o_jan', 'tier_pq', '$W/o.parquet')")"
check "the first attempt failed after export and left its file" \
	"$(grep -o 'injected fault after export' <<<"$o_err")|$([ -e "$W/o.parquet" ] && echo file || echo nofile)" \
	"injected fault after export|file"
check "a retry over the failed attempt's file moves the partition, exactly once" \
	"$(q "SELECT pgcolumnar.tier_partition('o_jan', 'tier_pq', '$W/o.parquet');")|$(digest o)|$(q "SELECT relkind FROM pg_class WHERE oid = 'o_jan'::regclass;")" \
	"8928|$ob|f"

# ---- names and columns the foreign table must reproduce (#1317 review) ---------
# Each failed after the export in the first version: a quoted schema, owner or
# grantee was quoted twice (%I over a reg* cast that quotes itself), and ATTACH
# refused a column whose collation or generation the foreign table lacked.
# Each moves a small partition, which must then read back as before.
mini() {	# mini SCHEMA.TABLE [COLUMNS]: a 100-row parent with one January partition
	psql_run "CREATE TABLE $1 (ts timestamptz NOT NULL, v int${2:-}) PARTITION BY RANGE (ts);"
	psql_run "CREATE TABLE ${1}_jan PARTITION OF $1 FOR VALUES FROM ('2026-01-01') TO ('2026-02-01') USING pgcolumnar;"
	psql_run "INSERT INTO $1 (ts, v) SELECT '2026-01-01'::timestamptz + g * interval '7 h', g FROM generate_series(0, 99) g;"
}
moved_reads() {	# moved_reads SCHEMA.TABLE PATH COLUMNS-TO-READ -> rows moved|relkind|digest-equal
	local before after
	before="$(q "SELECT md5(string_agg($3, ',' ORDER BY ts)) FROM $1;")"
	out="$(sqlstate_or "SELECT pgcolumnar.tier_partition('${1}_jan', 'tier_pq', '$2');")"
	after="$(q "SELECT md5(string_agg($3, ',' ORDER BY ts)) FROM $1;")"
	echo "$out|$(q "SELECT relkind FROM pg_class WHERE oid = '${1}_jan'::regclass;")|$([ -n "$before" ] && [ "$before" = "$after" ] && echo same || echo "differs")"
}
sqlstate_or() {
	local o
	o=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)
	case "$o" in ERROR:*) sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$o" | head -1;; *) echo "$o";; esac
}
psql_run 'CREATE SCHEMA "Tier Q";'
mini '"Tier Q".qs'
check "a partition in a schema whose name needs quoting moves and reads back the same" \
	"$(moved_reads '"Tier Q".qs' "$W/qs.parquet" "ts::text || v")" "100|f|same"
psql_run 'CREATE ROLE "Owner X";'
mini qo
psql_run 'ALTER TABLE qo_jan OWNER TO "Owner X";'
check "a partition owned by a role whose name needs quoting moves, and keeps that owner" \
	"$(moved_reads qo "$W/qo.parquet" "ts::text || v")|$(q "SELECT relowner::regrole FROM pg_class WHERE oid = 'qo_jan'::regclass;")" \
	'100|f|same|"Owner X"'
psql_run 'CREATE ROLE "Reader Y";'
mini qg
psql_run 'GRANT SELECT ON qg_jan TO "Reader Y";'
check "a grant to a role whose name needs quoting is carried to the foreign partition" \
	"$(moved_reads qg "$W/qg.parquet" "ts::text || v")|$(q "SELECT has_table_privilege('Reader Y', 'qg_jan', 'SELECT');")" \
	"100|f|same|t"
mini qc ', t text COLLATE "C"'
psql_run "UPDATE qc SET t = 'r' || v;"
check "a column with its own collation moves and reads back the same" \
	"$(moved_reads qc "$W/qc.parquet" "ts::text || v || t")" "100|f|same"
mini qgs ', w int GENERATED ALWAYS AS (v * 3) STORED'
check "a stored generated column moves and reads back the same" \
	"$(moved_reads qgs "$W/qgs.parquet" "ts::text || v || w")" "100|f|same"
if [ "$PGC_MAJOR" -ge 18 ]; then
	mini qgv ', w int GENERATED ALWAYS AS (v * 5) VIRTUAL'
	check "a virtual generated column moves and reads back the same" \
		"$(moved_reads qgv "$W/qgv.parquet" "ts::text || v || w")" "100|f|same"
else
	check_skip "a virtual generated column moves and reads back the same" \
		"SKIP  a virtual generated column moves and reads back the same (PostgreSQL $PGC_MAJOR has none; 18 added them)" \
		"PostgreSQL $PGC_MAJOR has no virtual generated columns"
fi

# ---- the caller's own privileges (#1341) -------------------------------------
#
# tier_partition is not SECURITY DEFINER: every step runs as the caller, so the
# owner-only steps (its LOCK ... IN SHARE MODE first) stop a role that does not own
# the table.
# Made SECURITY DEFINER, it would run as the extension's owner and let any caller
# detach, drop and replace a partition it does not own. Two roles, each holding
# every other privilege the call needs (both server-file roles, for the export and
# for the read-back of #1795, USAGE on the server and CREATE on the schema for the
# foreign table, and SELECT on the partition), so ownership is the only thing
# between them.
for r in tier_own tier_peer; do
	psql_run "DROP ROLE IF EXISTS $r; CREATE ROLE $r LOGIN NOSUPERUSER;
	          GRANT pg_write_server_files, pg_read_server_files TO $r; GRANT USAGE ON FOREIGN SERVER tier_pq TO $r;
	          GRANT USAGE, CREATE ON SCHEMA public TO $r; GRANT USAGE ON SCHEMA pgcolumnar TO $r;"
done
sqlstate_as() {	# sqlstate_as ROLE SQL -> the SQLSTATE SQL fails with as ROLE, or ok
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$2" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^\(ERROR\|FATAL\): *\([0-9A-Z]\{5\}\).*/\2/p' | head -1
}
# The owner's control has a table of its own (pw), so a refusal that let the peer
# through cannot make the control fail for the peer's reason.
for t in po pw; do
	build $t
	psql_run "ALTER TABLE $t OWNER TO tier_own; ALTER TABLE ${t}_jan OWNER TO tier_own;
	          ALTER TABLE ${t}_feb OWNER TO tier_own;"
done
psql_run "GRANT SELECT ON po, po_jan TO tier_peer;"
pb="$(digest po)"
check "premise: tier_own owns the parent and its partitions, and neither role is a superuser" \
	"$(q "SELECT string_agg(DISTINCT pg_get_userbyid(relowner), ',') FROM pg_class WHERE relname IN ('po', 'po_jan', 'po_feb', 'pw', 'pw_jan', 'pw_feb');")|$(q "SELECT count(*) FROM pg_roles WHERE rolname IN ('tier_own', 'tier_peer') AND rolsuper;")" \
	"tier_own|0"
check "premise: tier_peer can open a session and read the partition" \
	"$(sqlstate_as tier_peer "SELECT count(*) FROM po_jan")" "ok"
check "a role that does not own the table is refused by its own privileges (42501)" \
	"$(sqlstate_as tier_peer "SELECT pgcolumnar.tier_partition('po_jan', 'tier_pq', '$W/po_peer.parquet')")" "42501"
check "and the refused call leaves the partition local and attached, every row once" \
	"$(digest po)|$(jan_shape po)" "$pb|r:pgcolumnar:$JAN_BOUND"
check_num "with no foreign table behind it" \
	"$(q "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid WHERE c.relname LIKE 'po\\_%';")" "0"
# The first statement to need more than SELECT is the LOCK ... IN SHARE MODE ahead
# of the export, so the refusal comes before any file is written (measured on 15-19).
check "and the refused call wrote no export file" \
	"$([ -e "$W/po_peer.parquet" ] && echo written || echo absent)" "absent"
check "a non-superuser owner tiers its own partition" \
	"$(sqlstate_as tier_own "SELECT pgcolumnar.tier_partition('pw_jan', 'tier_pq', '$W/pw_own.parquet')")|$(jan_shape pw)" \
	"ok|f:-:$JAN_BOUND"
# A caller without pg_read_all_settings cannot read data_directory, so
# tier_partition cannot resolve a relative path for it (#2004). It then
# refuses a local target beside a table on a local path whenever either of
# the two is relative, as where resolving is unsound. Two paths in full are
# compared as before.
psql_run "CREATE FOREIGN TABLE dup_np04 (x int) SERVER tier_pq OPTIONS (path 'np2004/a.parquet');"
errdetail_as() {	# errdetail_as ROLE SQL -> the first DETAIL line SQL fails with as ROLE, or ok, or none
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -c "$2" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	out=$(echo "$out" | sed -n 's/^DETAIL: *//p' | head -1)
	echo "${out:-none}"
}
check "premise: tier_own cannot read data_directory" \
	"$(sqlstate_as tier_own "SELECT current_setting('data_directory')")" "42501"
check "a caller who cannot read data_directory is refused a path in full beside a table on a relative path, and the detail names that table" \
	"$(sqlstate_as tier_own "BEGIN; SELECT pgcolumnar.tier_partition('pw_feb', 'tier_pq', '$D/np2004/a.parquet'); ROLLBACK")|$(sqlstate_as tier_own "BEGIN; SELECT pgcolumnar.tier_partition('pw_feb', 'tier_pq', '$W/np2004/c.parquet'); ROLLBACK")|$(errdetail_as tier_own "BEGIN; SELECT pgcolumnar.tier_partition('pw_feb', 'tier_pq', '$W/np2004/c.parquet'); ROLLBACK")" \
	"58P02|58P02|Foreign table public.dup_np04 reads path 'np2004/a.parquet'."
psql_run "DROP FOREIGN TABLE dup_np04;"
check "a caller who cannot read data_directory is refused a relative target beside tables on paths in full" \
	"$(sqlstate_as tier_own "BEGIN; SELECT pgcolumnar.tier_partition('pw_feb', 'tier_pq', 'np2004/b.parquet'); ROLLBACK")" "58P02"
check "a caller who cannot read data_directory still moves a path in full no table reads beside tables on paths in full" \
	"$(sqlstate_as tier_own "BEGIN; SELECT pgcolumnar.tier_partition('pw_feb', 'tier_pq', '$W/np2004/c.parquet'); ROLLBACK")" "58P01"

# ---- a failure after each step ----------------------------------------------
for step in export detach drop create attach; do
	build f
	fb="$(digest f)"
	err="$(errtext "SET pgcolumnar.tier_fault_after = '$step'" \
		"SELECT pgcolumnar.tier_partition('f_jan', 'tier_pq', '$W/f_$step.parquet')")"
	check "the call fails with the fault injected after $step" \
		"$(grep -o "injected fault after $step" <<<"$err" || echo "$err")" "injected fault after $step"
	check "a failure after $step leaves the rows exactly once, still local" \
		"$(digest f)|$(jan_shape f)" "$fb|r:pgcolumnar:$JAN_BOUND"
	check_num "and no foreign table behind it after $step" \
		"$(q "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid
		      WHERE c.relname LIKE 'f\\_%';")" "0"
done


# ---- the write uses the named server and the caller's mapping (#1795) --------
#
# The foreign partition reads with the server's endpoint and region and the
# caller's user mapping. The write must use the same, or the move commits a
# partition that cannot read its own file. Two SigV4-verifying fixture servers,
# each with its own directory and secret: A is the one the postmaster environment
# names, B is reachable only through a server and a mapping. Where an object
# lands shows which identity wrote it.
S3_REGION="pgc-test-1"; S3_KEY="PGCTESTKEYID"
S3A_SECRET="pgctest-env-secret"; S3B_SECRET="pgctest-mapping-secret"
S3_PIDS=""
tier_s3_on_exit() {
	local rc=$?
	[ -n "$S3_PIDS" ] && kill $S3_PIDS 2>/dev/null
	(exit $rc)
	pgc_on_exit
}
trap tier_s3_on_exit EXIT
# s3_start NAME SECRET VAR: start a fixture whose bucket is $PGC_WORKDIR/NAME/tierb and
# set VAR to its port. Not run in $(...): the PID must reach S3_PIDS in this shell,
# or the trap cannot stop the server, and a server left running keeps the
# caller's lock file descriptor open.
s3_start() {
	local port out="$PGC_WORKDIR/$1.out"
	mkdir -p "$PGC_WORKDIR/$1/tierb" && chmod -R 777 "$PGC_WORKDIR/$1"
	port="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
	python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
		--dir "$PGC_WORKDIR/$1" --port "$port" --log "$PGC_WORKDIR/$1.log" \
		--sigv4-key "$S3_KEY" --sigv4-secret "$2" --sigv4-region "$S3_REGION" >"$out" 2>&1 &
	S3_PIDS="$S3_PIDS $!"
	for _ in $(seq 1 50); do grep -q READY "$out" 2>/dev/null && break; sleep 0.1; done
	printf -v "$3" '%s' "$port"
}
s3_start s3a "$S3A_SECRET" S3A_PORT
s3_start s3b "$S3B_SECRET" S3B_PORT
check "premise: both SigV4 fixture servers are up" \
	"$(cat "$PGC_WORKDIR/s3a.out" "$PGC_WORKDIR/s3b.out" 2>/dev/null | grep -c READY)" "2"
pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
pgc_pg "AWS_ENDPOINT_URL=http://127.0.0.1:$S3A_PORT AWS_ACCESS_KEY_ID=$S3_KEY \
	AWS_SECRET_ACCESS_KEY=$S3A_SECRET AWS_REGION=$S3_REGION \
	pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && break; sleep 0.5; done
# mdigest TABLE -> count|md5 of a mini table's rows, or the error that stopped the read
mdigest() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v ON_ERROR_STOP=1 -c "SELECT count(*) || '|' || md5(string_agg(ts::text || v, ',' ORDER BY ts)) FROM $1;" 2>&1)
	echo "$out" | sed -n 's/^ERROR: *//p;/^[0-9]*|/p' | head -1
}
objects() { (cd "$PGC_WORKDIR/$1/tierb" 2>/dev/null && ls | sort | tr '\n' ' ' | sed 's/ $//'); }
mini sp
check "premise: the postmaster environment writes to fixture A" \
	"$(q "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/premise.parquet');")|$(objects s3a)" \
	"100|premise.parquet"

# The repro from #1795: the server names fixture A, the environment holds A's
# valid keys, and the caller's mapping holds a wrong secret.
psql_run "CREATE SERVER tier_s3a FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3A_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR postgres SERVER tier_s3a
          OPTIONS (access_key_id '$S3_KEY', secret_access_key 'pgctest-bogus');"
mini sa
sab="$(mdigest sa)"
check "a mapping the object store rejects refuses the move (28000), not the environment's keys" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('sa_jan', 'tier_s3a', 's3://tierb/sa_jan.parquet')")" "28000"
check "and the refused move leaves the rows local and attached, every row once" \
	"$(mdigest sa)|$(jan_shape sa)" "$sab|r:pgcolumnar:$JAN_BOUND"
check "and the environment's keys wrote no object" "$(objects s3a)" "premise.parquet"

# The server names fixture B, which only the mapping's secret opens.
psql_run "CREATE SERVER tier_s3b FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3B_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR postgres SERVER tier_s3b
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');"
mini sb
sbb="$(mdigest sb)"
check "a move through a server writes with its endpoint and the caller's mapping" \
	"$(errtext "SELECT pgcolumnar.tier_partition('sb_jan', 'tier_s3b', 's3://tierb/sb_jan.parquet')")|A: $(objects s3a)|B: $(objects s3b)" \
	"ok|A: premise.parquet|B: sb_jan.parquet"
check "and the parent reads every row back through the foreign partition" \
	"$(mdigest sb)|$(jan_shape sb)" "$sbb|f:-:$JAN_BOUND"

# ---- export_parquet(rel, path, server) refuses before it writes (#1795) --------
#
# The three-argument form is public. Each refusal below must come before the
# file is opened, so neither fixture may hold an object afterwards.
#
# tier_s3pub names fixture B and has a PUBLIC mapping with B's valid secret. A
# role without USAGE on it would write to B with that mapping if the USAGE gate
# were gone, so the refusal guards credentials the role was never given.
# tier_s3half has a mapping for postgres with an access key and no secret.
psql_run "CREATE FOREIGN DATA WRAPPER tier_dummy; CREATE SERVER tier_other FOREIGN DATA WRAPPER tier_dummy;
          CREATE SERVER tier_s3pub FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3B_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR PUBLIC SERVER tier_s3pub
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');
          CREATE SERVER tier_s3half FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3B_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR postgres SERVER tier_s3half OPTIONS (access_key_id '$S3_KEY');
          DROP ROLE IF EXISTS tier_s3nouse; CREATE ROLE tier_s3nouse LOGIN NOSUPERUSER;
          GRANT pg_write_server_files TO tier_s3nouse; GRANT SELECT ON sp_jan TO tier_s3nouse;
          GRANT USAGE ON SCHEMA pgcolumnar TO tier_s3nouse;"
check "premise: the PUBLIC mapping on tier_s3pub writes to fixture B" \
	"$(errtext "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/pub_premise.parquet', 'tier_s3pub')")|$(objects s3b | tr ' ' '\n' | grep -c '^pub_premise')" \
	"ok|1"
# Backends killed by a signal, from the server log: a number, or nothing when the
# log cannot be read, so that a blind count never reads as "no crashes".
crashes() {
	local n
	n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$n" in '' | *[!0-9]*) ;; *) echo "$n" ;; esac
}
gA="$(objects s3a)"; gB="$(objects s3b)"; gcrash="$(crashes)"
check "export_parquet through a server of another wrapper is refused (42809)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_other.parquet', 'tier_other')")" "42809"
check "export_parquet through a server that does not exist is refused (42704)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_none.parquet', 'tier_nosuch')")" "42704"
check "export_parquet by a role without USAGE on the server is refused (42501)" \
	"$(sqlstate_as tier_s3nouse "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_usage.parquet', 'tier_s3pub')")" "42501"
check "export_parquet to a local path through a server whose mapping holds half a credential is refused (28000)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', '$W/g_half.parquet', 'tier_s3half')")|$([ -e "$W/g_half.parquet" ] && echo file || echo nofile)" \
	"28000|nofile"
# The null arm runs last, so that a backend it crashes cannot fail the arms above
# for want of a server in recovery.
check "export_parquet through a null server is refused (22004)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_null.parquet', NULL)")" "22004"
check_num "and no refused export crashed a backend" "$(crashes)" "$gcrash"
check "and no refused export wrote an object to A or B" "A: $(objects s3a)|B: $(objects s3b)" "A: $gA|B: $gB"
# After a crash the server takes a moment to accept connections again.
for _ in $(seq 1 60); do [ "$(q "SELECT 1;" 2>/dev/null)" = 1 ] && break; sleep 0.5; done

# ---- a non-superuser gets no environment keys (#1795) -------------------------
#
# The write takes the read's rule: the environment is a privilege of a superuser
# (or of a mapping a superuser marked credentials_required 'false'). Before #1795
# a member of pg_write_server_files with no mapping wrote with the environment's
# keys. Now it needs a mapping of its own, as it does to read.
count_as() {	# count_as ROLE TABLE -> count(*) of TABLE as ROLE, or the error that stopped it
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" -X -At \
		-v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $2;" 2>&1)
	echo "$out" | sed -n 's/^ERROR: *//p;/^[0-9][0-9]*$/p' | head -1
}
for r in tier_s3own tier_s3wo; do
	psql_run "DROP ROLE IF EXISTS $r; CREATE ROLE $r LOGIN NOSUPERUSER;
	          GRANT pg_write_server_files TO $r; GRANT USAGE ON FOREIGN SERVER tier_s3b TO $r;
	          GRANT USAGE, CREATE ON SCHEMA public TO $r; GRANT USAGE ON SCHEMA pgcolumnar TO $r;"
done
# tier_s3own can read server files too; tier_s3wo can only write them.
psql_run "GRANT pg_read_server_files TO tier_s3own;"
for t in so swo; do
	mini $t
done
psql_run "ALTER TABLE so OWNER TO tier_s3own; ALTER TABLE so_jan OWNER TO tier_s3own;
          ALTER TABLE swo OWNER TO tier_s3wo; ALTER TABLE swo_jan OWNER TO tier_s3wo;"
check "premise: neither owner is a superuser, and neither has a mapping on the server" \
	"$(q "SELECT count(*) FROM pg_roles WHERE rolname IN ('tier_s3own', 'tier_s3wo') AND rolsuper;")|$(q "SELECT count(*) FROM pg_user_mappings WHERE srvname = 'tier_s3b' AND usename IN ('tier_s3own', 'tier_s3wo', 'public');")" \
	"0|0"
gB="$(objects s3b)"
check "a non-superuser with no mapping is refused the export (28000), not given the environment's keys" \
	"$(sqlstate_as tier_s3own "SELECT pgcolumnar.export_parquet('so_jan', 's3://tierb/so_env.parquet', 'tier_s3b')")|A: $(objects s3a)" \
	"28000|A: $gA"
sob="$(mdigest so)"
check "a non-superuser owner with no mapping is refused the move (28000)" \
	"$(sqlstate_as tier_s3own "SELECT pgcolumnar.tier_partition('so_jan', 'tier_s3b', 's3://tierb/so_jan.parquet')")" "28000"
check "and its refused move leaves the rows local and attached, every row once, with no object written" \
	"$(mdigest so)|$(jan_shape so)|A: $(objects s3a)|B: $(objects s3b)" "$sob|r:pgcolumnar:$JAN_BOUND|A: $gA|B: $gB"
psql_run "CREATE USER MAPPING FOR tier_s3own SERVER tier_s3b
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');"
check "a non-superuser owner moves its partition through its own mapping" \
	"$(sqlstate_as tier_s3own "SELECT pgcolumnar.tier_partition('so_jan', 'tier_s3b', 's3://tierb/so_jan.parquet')")|$(jan_shape so)|B: $(objects s3b)" \
	"ok|f:-:$JAN_BOUND|B: $(echo $gB so_jan.parquet | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')"
check "and the owner reads every row back through the foreign partition" \
	"$(count_as tier_s3own so)|$(mdigest so)" "100|$sob"

# ---- the move reads its file back before it commits (#1795, Fix point 2) ------
#
# The write proves the key can write. A key that can write but not read (an IAM
# policy with s3:PutObject and no s3:GetObject) would commit a partition its
# caller cannot read. The move must count the rows through the new foreign
# partition before it returns.
psql_run "CREATE USER MAPPING FOR tier_s3wo SERVER tier_s3b
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');"
swb="$(mdigest swo)"
check "an owner without pg_read_server_files is refused the move before anything is written (42501)" \
	"$(sqlstate_as tier_s3wo "SELECT pgcolumnar.tier_partition('swo_jan', 'tier_s3b', 's3://tierb/swo_jan.parquet')")|B: $(objects s3b | tr ' ' '\n' | grep -c '^swo_jan')" \
	"42501|B: 0"
check "and the move refused for want of pg_read_server_files leaves the rows local and attached" \
	"$(mdigest swo)|$(jan_shape swo)" "$swb|r:pgcolumnar:$JAN_BOUND"
mini sr
srb="$(mdigest sr)"
: >"$PGC_WORKDIR/s3b/__deny_reads__"
check "a key that can write but not read refuses the move (28000)" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('sr_jan', 'tier_s3b', 's3://tierb/sr_jan.parquet')")" "28000"
rm -f "$PGC_WORKDIR/s3b/__deny_reads__"
check "and the move refused at the read-back leaves the rows local and attached, every row once" \
	"$(mdigest sr)|$(jan_shape sr)" "$srb|r:pgcolumnar:$JAN_BOUND"

# ---- a pattern too deep for glob(3) is an error, not a crash (#1964 review) --
#
# Last in the suite: before the fix these calls crashed the server, and the
# crash recovery failed every check after them. Each check below waits up to
# 60 s for the server to answer first, so a regression fails its own check
# alone.
# wait_up -> return once the server answers SELECT 1, or after 60 s
wait_up() { local i; for i in $(seq 60); do [ "$(qerr "SELECT 1;")" = 1 ] && return; sleep 1; done; }
check "path_glob_matches refuses a pattern of 4000 */ levels with an error, and the server stays up" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches(repeat('*/', 4000) || 'x', 'a/x')::text;")|$(qerr "SELECT 1;")" \
	"54000|1"
# A role may first use most of max_stack_depth in SQL, and glob(3) then has
# only what is left. pgm_deep calls itself n times, then the helper. The
# deepest n that still answers is found in steps of 100, from 3000 down.
wait_up
psql_run "CREATE FUNCTION pgm_deep(n int, p text, q text) RETURNS text LANGUAGE plpgsql AS \$\$
          BEGIN IF n = 0 THEN RETURN pgcolumnar.path_glob_matches(p, q)::text; END IF; RETURN pgm_deep(n - 1, p, q); END \$\$;"
deep=3000
while [ "$deep" -gt 0 ] && [ "$(ansst "SELECT pgm_deep($deep, '/w/*', '/w/a');")" != true ]; do deep=$((deep - 100)); done
echo "-- pgm_deep answers at $deep levels of SQL"
check "path_glob_matches refuses one * and 2045 more levels with an error near max_stack_depth, and the server stays up" \
	"$(ansst "SELECT pgm_deep($deep, '/*/' || repeat('a/', 2045) || 'x', '/b');")|$(qerr "SELECT 1;")" \
	"54000|1"
wait_up
psql_run "DROP FUNCTION pgm_deep(int, text, text);"
# fnmatch keeps more on the stack for each * in a UTF-8 locale than in C, so
# this runs in a database whose LC_CTYPE is C.UTF-8.
psql_run "DROP DATABASE IF EXISTS tier_cutf8;"
psql_run "CREATE DATABASE tier_cutf8 TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8';"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d tier_cutf8 -X -q -v ON_ERROR_STOP=1 -c "CREATE EXTENSION pgcolumnar;"
check "path_glob_matches refuses an s3:// pattern of 1000 */ levels with an error in a UTF-8 database, and the server stays up" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d tier_cutf8 -X -At -v VERBOSITY=sqlstate \
		-c "SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('*/', 1000) || 'x', 's3://b/' || repeat('a/', 1000) || 'x')::text;" 2>&1 | head -1 | sed 's/^ERROR: *//')|$(qerr "SELECT 1;")" \
	"54000|1"
wait_up
psql_run "DROP DATABASE tier_cutf8;"

# ---- path_glob_matches reads .* and .? as glob(3) does (#1982) ---------------
#
# glob(3) lists . and .. in each directory, and a glob name that spells the
# leading dot matches them, so it returns names such as /w/x/./a.parquet. The
# helper compares the lexical form of each one with that of the target. A
# directory the glob names before its first *, ? or [ is taken to exist, so
# /w/x/sub/.?/a.parquet reaches /w/x/a.parquet through sub. An s3:// key is
# not a path, so it is never folded. Last in the suite, as new C code: a
# crash here fails these checks alone.
wait_up
check "path_glob_matches answers true for a path a local glob reaches through a .* or a .? name" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('$W/sd/.*/a.parquet', '$W/sd/a.parquet') || '|' || pgcolumnar.path_glob_matches('$W/sd/sub/.?/a.parquet', '$W/sd/a.parquet');")" \
	"true|true"
check "path_glob_matches answers true for a path a local glob reaches through a .[.] or an escaped .* name" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/w/x/sub/.[.]/a.parquet', '/w/x/a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/x/\\.*/a.parquet', '/w/x/a.parquet');")" \
	"true|true"
check "path_glob_matches answers true for a target spelled with . or a name and .. that a .* glob reaches" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/w/x/.*/a.parquet', '/w/x/./a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/x/.*/a.parquet', '/w/x/sub/../a.parquet');")" \
	"true|true"
check "a local glob * or ?? does not match . or .., so path_glob_matches answers false for a path reached only through them" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/w/x/*/a.parquet', '/w/x/a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/x/sub/??/a.parquet', '/w/x/a.parquet');")" \
	"false|false"
check "path_glob_matches does not fold an s3:// key, so s3://b/x/*.parquet does not match s3://b/x/./a.parquet" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('s3://b/x/*.parquet', 's3://b/x/./a.parquet') || '|' || pgcolumnar.path_glob_matches('s3://b/x/*.parquet', 's3://b/x/a.parquet');")" \
	"false|true"
# A wildcard name, then a later name that matches .., climbs back out of the
# directory the wildcard matched (#1991). The helper cannot know that
# directory, so it walks a stand-in for it, and takes a name the glob spells
# below it to exist. A name that cannot match .., such as ?? (a leading dot
# must be spelled), climbs nothing, and the last name must still match.
check "path_glob_matches answers true for a path a local glob reaches through a * name and then a .? name" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('$W/sw/*/.?/a.parquet', '$W/sw/a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/x/*/*/.?/.?/a.parquet', '/w/x/a.parquet');")" \
	"true|true"
check "path_glob_matches answers true for a path a local glob reaches through a ? name and then .., or a [class] name and then .*" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/w/x/?/../a.parquet', '/w/x/a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/x/[d]/.*/a.parquet', '/w/x/a.parquet');")" \
	"true|true"
check "path_glob_matches answers true for a path a local glob reaches through a * name, a .? name and a directory it names after them" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/w/x/*/.?/sub/../a.parquet', '/w/x/a.parquet')::text;")" \
	"true"
check "path_glob_matches answers false for a path above a * name that no later name climbs back from, or that the last name does not match" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('/w/x/*/a.parquet', '/w/x/a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/x/*/??/a.parquet', '/w/x/a.parquet') || '|' || pgcolumnar.path_glob_matches('/w/x/*/.?/a.parquet', '/w/x/b.parquet');")" \
	"false|false|false"

# A relative pattern or target names a path under the data directory, the
# server's working directory, where the reader's glob(3) and export_parquet
# start (#2001). The helper resolves both against it before it walks, so a
# .. above the pattern's start climbs to the data directory's parent, and a
# * there can match the data directory's own name.
check "path_glob_matches answers true for a relative glob whose .. climbs above its start and comes back down through a * name, to a relative path or that path in full" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('../*/rg/a.parquet', 'rg/a.parquet') || '|' || pgcolumnar.path_glob_matches('../*/rg/a.parquet', current_setting('data_directory') || '/rg/a.parquet');")" \
	"true|true"
check "path_glob_matches answers true for a relative glob whose .* or .? name climbs above its start and comes back down through a * or a [class] name" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('.*/*/?.parquet', 'a.parquet') || '|' || pgcolumnar.path_glob_matches('rg/../.?/[!.]*/rg/a.parquet', 'rg/a.parquet');")" \
	"true|true"
check "path_glob_matches matches a relative glob against a path in full under the data directory, and a glob in full against a relative path" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('rg/*.parquet', current_setting('data_directory') || '/rg/a.parquet') || '|' || pgcolumnar.path_glob_matches(current_setting('data_directory') || '/rg/*.parquet', 'rg/a.parquet');")" \
	"true|true"
check "path_glob_matches answers false for a relative glob that climbs into another directory than the path's, or whose last name does not match" \
	"$(ansst "SELECT pgcolumnar.path_glob_matches('../*/rg/b.parquet', 'rg/a.parquet') || '|' || pgcolumnar.path_glob_matches('../../*/rg/a.parquet', 'rg/a.parquet') || '|' || pgcolumnar.path_glob_matches('../rg/*.parquet', current_setting('data_directory') || '/rg/a.parquet');")" \
	"false|false|false"
pgc_summary

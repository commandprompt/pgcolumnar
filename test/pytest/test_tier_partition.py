"""pgcolumnar.tier_partition moves a cold partition to Parquet, exactly once (#1295).

The owner's decision on #1295: atomic, with a brief ACCESS EXCLUSIVE on the
parent. Export under the partition's SHARE lock, then detach, drop, create the
foreign table under the partition's own name, and attach it with its own bounds.

"EXACTLY ONCE" IS TESTED, NOT ARGUED. `pgcolumnar.tier_fault_after` raises an
error right after the step it names, and each fault arm first requires that the
error raised IS the injected one: a missing function also fails and leaves the
rows where they were.

Two findings the shell twin made while the code was written, held here too:
- `pg_stat_file(...) IS NOT NULL` is false for an existing file, so the
  existing-path refusal never fired, and the export overwrote a file a foreign
  partition was reading.
- without the SHARE lock, a write in flight when the move starts was lost: the
  export did not see it, DETACH waited for it, DROP took it with the partition.

ON A PRIVATE DATABASE (`pgc_own_db`): foreign servers are per database. A local
path stands in for object storage. Independent of `test/tier_partition.sh`: its
own columns, months, row counts, paths and in-flight row.
"""

import threading
import time

import psycopg

BOUND = "FOR VALUES FROM ('2026-03-01 00:00:00+00') TO ('2026-04-01 00:00:00+00')"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _sqlstate(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error as e:
        return e.sqlstate or "?"


def _errtext(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error as e:
        return str(e).splitlines()[0]


def _errdetail(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error as e:
        return e.diag.message_detail or "none"


def _build(conn, t):
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        cur.execute(f"CREATE TABLE {t} (at timestamptz NOT NULL, sensor int, reading float8,"
                    " label text) PARTITION BY RANGE (at)")
        cur.execute(f"CREATE TABLE {t}_mar PARTITION OF {t} FOR VALUES FROM ('2026-03-01')"
                    " TO ('2026-04-01') USING pgcolumnar")
        cur.execute(f"CREATE TABLE {t}_apr PARTITION OF {t} FOR VALUES FROM ('2026-04-01')"
                    " TO ('2026-05-01') USING pgcolumnar")
        cur.execute(f"INSERT INTO {t} SELECT '2026-03-01'::timestamptz + g * interval '7 min',"
                    " g % 17, (g * 31 % 997) / 7.0, 'r' || (g * 13 % 101)"
                    " FROM generate_series(0, 12340) g")


def _digest(conn, t):
    # Every row decoded: an unfiltered count/sum is answered from the zone maps.
    return _one(conn, f"SELECT count(*) || '|' || md5(string_agg(at::text || sensor || reading"
                      f" || label, ',' ORDER BY at)) FROM {t}")


def _mar_shape(conn, t):
    return _one(conn, "SELECT c.relkind::text || ':' || coalesce(a.amname, '-') || ':'"
                      " || pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i"
                      " JOIN pg_class c ON c.oid = i.inhrelid LEFT JOIN pg_am a ON a.oid = c.relam"
                      f" WHERE i.inhparent = '{t}'::regclass AND c.relname = '{t}_mar'")


def _tier(t, path):
    return f"SELECT pgcolumnar.tier_partition('{t}_mar', 'tq', '{path}')"


def test_tier_partition_moves_a_partition_exactly_once(pgc_own_db, pgc_cluster, server_dir, expect):
    conn = pgc_own_db
    with conn.cursor() as cur:
        cur.execute("SET timezone = 'UTC'")
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
    _build(conn, "s")
    expect.num(_one(conn, "SELECT count(*) FROM s"), 12341, "premise: the parent holds every row")
    mar = _one(conn, "SELECT count(*) FROM s_mar")
    expect.num(mar, 6378, "premise: the cold partition holds its share")
    before = _digest(conn, "s")
    expect.text(_mar_shape(conn, "s"), f"r:pgcolumnar:{BOUND}",
                "premise: the cold partition is local and columnar, with its bound")
    sid = _one(conn, "SELECT pgcolumnar.get_storage_id('s_mar')")

    path = server_dir / "s_mar.parquet"
    expect.text(str(_one(conn, _tier("s", path))), str(mar),
                "tier_partition moves the cold partition and returns the rows it moved")
    kind = _one(conn, "SELECT coalesce((SELECT relkind::text FROM pg_class"
                      " WHERE oid = to_regclass('s_mar')), 'absent')")
    # The relkind rides along: with nothing moved the digest is trivially unchanged.
    expect.text(f"{_digest(conn, 's')}|{kind}", f"{before}|f",
                "the answers over the parent are the same after the move")
    expect.text(_mar_shape(conn, "s"), f"f:-:{BOUND}",
                "the cold partition is now a foreign partition with its own name and bound")
    expect.text(_one(conn, "SELECT srvname || ':' || array_to_string(ftoptions, ',')"
                           " FROM pg_foreign_table t JOIN pg_foreign_server s ON s.oid = t.ftserver"
                           " WHERE t.ftrelid = 's_mar'::regclass"),
                f"tq:path={path}", "the foreign partition reads the exported file")
    expect.text("read" if sid is not None else "unread", "read",
                "premise: the storage id was read before the move")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid or 0}"), 0,
               "the local partition leaves no pgcolumnar storage row behind")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid or 0}"), 0,
               "nor any row-group row")

    def plan(sql):
        with conn.cursor() as cur:
            cur.execute("EXPLAIN " + sql)
            return "\n".join(r[0] for r in cur.fetchall())
    p_mar = plan("SELECT min(reading) FROM s WHERE at < '2026-03-15'")
    p_apr = plan("SELECT min(reading) FROM s WHERE at >= '2026-04-12'")
    expect.text(f"{p_mar.count('Foreign Scan')}|{p_mar.count('s_apr')}", "1|0",
                "a query on the tiered range scans only the foreign partition")
    expect.text(f"{p_apr.count('Foreign Scan')}|{p_apr.count('s_apr')}|{p_mar.count('Foreign Scan')}",
                "0|1|1", "a query on the other range scans only the local partition, beside a foreign one")
    expect.text(_sqlstate(conn, "INSERT INTO s VALUES ('2026-03-09', 1, 1, 'x')"), "0A000",
                    "an insert routed to the tiered range is refused")

    # Refusals, before anything moves.
    rb = _digest(conn, "s")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE solo (at timestamptz, v int) USING pgcolumnar")
        cur.execute("CREATE TABLE s_may PARTITION OF s FOR VALUES FROM ('2026-05-01')"
                    " TO ('2026-06-01') USING heap")
        cur.execute("CREATE TABLE s_rest PARTITION OF s DEFAULT USING pgcolumnar")
        cur.execute("CREATE SERVER ti FOREIGN DATA WRAPPER pgcolumnar_iceberg")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('solo', 'tq', '{server_dir}/solo.parquet')"),
                    "42809", "a table that is not a partition is refused")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_may', 'tq', '{server_dir}/may.parquet')"),
                    "42809", "a partition that is not columnar is refused")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_rest', 'tq', '{server_dir}/rest.parquet')"),
                    "0A000", "a DEFAULT partition is refused")
    # Before anything moves (#1339): a refusal from a later step, after the export,
    # would leave the file behind with the same SQLSTATE.
    other = _sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_apr', 'ti', '{server_dir}/apr.parquet')")
    expect.text(f"{other}|{'file' if (server_dir / 'apr.parquet').exists() else 'nofile'}",
                "42809|nofile", "a server of another wrapper is refused")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_apr', 'tq', '{path}')"),
                    "58P02", "an existing path is refused, so no file a table reads is overwritten")
    # The readers read s3a:// as s3:// and match the scheme in any case (#1930),
    # so these spellings name the object a tier to s3:// would overwrite (#1936).
    # The refusal comes before the export, so no store is contacted.
    with conn.cursor() as cur:
        cur.execute("CREATE FOREIGN TABLE s_dup_s3a (x int) SERVER tq"
                    " OPTIONS (path 's3a://tierb/s_apr_a.parquet')")
        cur.execute("CREATE FOREIGN TABLE s_dup_upper (x int) SERVER tq"
                    " OPTIONS (path 'S3://tierb/s_apr_u.parquet')")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 's3://tierb/s_apr_a.parquet')"),
                "58P02", "a path a foreign table reads as s3a:// is refused like the identical s3:// spelling")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 's3://tierb/s_apr_u.parquet')"),
                "58P02", "a path a foreign table reads as S3:// is refused like the identical s3:// spelling")
    # Each branch of the rule has a check of its own (#1937 review): s3n:// on
    # the table side, an upper-case scheme on the target side, and the scheme
    # case of gs://, az://, http:// and https://.
    dups = {"s_dup_s3n": "s3n://tierb/s_apr_n.parquet", "s_dup_lower": "s3://tierb/s_apr_l.parquet",
            "s_dup_gs": "GS://tierb/s_apr_g.parquet", "s_dup_az": "Az://tierb/s_apr_z.parquet",
            "s_dup_http": "HTTP://h.example/s_apr_h.parquet",
            "s_dup_https": "HTTPS://h.example/s_apr_t.parquet"}
    with conn.cursor() as cur:
        for name, p in dups.items():
            cur.execute(f"CREATE FOREIGN TABLE {name} (x int) SERVER tq OPTIONS (path '{p}')")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 's3://tierb/s_apr_n.parquet')"),
                "58P02", "a path a foreign table reads as s3n:// is refused like the identical s3:// spelling")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 'S3://tierb/s_apr_l.parquet')"),
                "58P02", "an S3:// path is refused when a foreign table reads the identical s3:// spelling")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 'gs://tierb/s_apr_g.parquet')"),
                "58P02", "a path a foreign table reads as GS:// is refused like the identical gs:// spelling")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 'az://tierb/s_apr_z.parquet')"),
                "58P02", "a path a foreign table reads as Az:// is refused like the identical az:// spelling")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 'http://h.example/s_apr_h.parquet')"),
                "58P02", "a path a foreign table reads as HTTP:// is refused like the identical http:// spelling")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', 'https://h.example/s_apr_t.parquet')"),
                "58P02", "a path a foreign table reads as HTTPS:// is refused like the identical https:// spelling")
    # tier_partition is not STRICT. With foreign tables on a path= option
    # present, a NULL path must still reach the null refusal.
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('s_apr', 'tq', NULL)"),
                "22004", "a NULL path is refused as null, not as a file a foreign table reads")
    with conn.cursor() as cur:
        cur.execute("DROP FOREIGN TABLE s_dup_s3a, s_dup_upper, " + ", ".join(dups))
    # A foreign table on a directory or a glob reads every file the reader
    # expands it to, so a path under that directory or matching that glob names
    # a file the table reads (#1941). The host of an http(s) URL matches in any
    # case. A remote target that passes the check fails in the export with
    # 28000 (tq has no credentials), so nothing is written. A local target that
    # passed would move the partition, so each local call is rolled back.
    covers = {"s_dup_rdir": "s3://tierb/sdir/", "s_dup_rglob": "s3a://tierb/sg/*.parquet",
              "s_dup_rclass": "s3://tierb/sc/a_?[0-9].parquet",
              "s_dup_resc": "s3://tierb/se/\\*.parquet", "s_dup_rkey": "s3://tierb/skey",
              "s_dup_host": "http://H.Example/s_apr_k.parquet",
              "s_dup_ldir": f"{server_dir}/sldir", "s_dup_lglob": f"{server_dir}/slg/*.parquet",
              "s_dup_runt": "s3://tierb/su/?[]x.parquet", "s_dup_rbang": "s3://tierb/sn/a[!]x*",
              "s_dup_rbesc": "s3://tierb/sb/m[\\]]x.parquet",
              "s_dup_rdigit": "s3://tierb/spd/2026_[[:digit:]][[:digit:]].parquet",
              "s_dup_rcaret": "s3://tierb/sca/[^]x].parquet",
              "s_dup_lslash": f"{server_dir}/sls/*[a/b].parquet",
              "s_dup_rlead": "s3://tierb/sla/[]a].parquet",
              "s_dup_rbunt": "s3://tierb/sxb/x[\\].parquet",
              "s_dup_ropen": "s3://tierb/spu/m[[:digit:].parquet",
              "s_dup_requiv": "s3://tierb/seq/[[=]=]].parquet",
              "s_dup_rzq": "s3://tierb/szq/[[:z:]].parquet",
              "s_dup_rrange": "s3://tierb/srg/a[!:-[:digit:]].parquet",
              "s_dup_rlate": "s3://tierb/slt/[1a-[:alpha:]]x.parquet",
              "s_dup_rlateq": "s3://tierb/sle/[1a-[=b=]]x.parquet",
              "s_dup_llate": f"{server_dir}/sllt/[1a-[:alpha:]]x.parquet",
              "s_dup_rcdash": "s3://tierb/scd/[[:alpha:]-[:digit:]].parquet",
              "s_dup_rrdash": "s3://tierb/srd/-[/-[-[:alpha:][=a=]].parquet",
              "s_dup_rnone": "s3://tierb/snn/[1[::]-a]x.parquet",
              "s_dup_rcollab": "s3://tierb/scb/[1[.ab.]-a]x.parquet",
              "s_dup_rvneg": "s3://tierb/svn/[^-:-[:alpha:]x].parquet",
              "s_dup_lvneg": f"{server_dir}/slvn/[!-:-[:alpha:]x].parquet",
              "s_dup_hglob": "http://h.example/shg/*.parquet",
              "s_dup_gglob": "gs://tierb/sgg/*.parquet",
              "s_dup_rcunt": "s3://tierb/snc/a[^]x*",
              "s_dup_rspan": "s3://tierb/ssp/x[a/b].parquet",
              "s_dup_rcoll": "s3://tierb/sco/[[.].]].parquet",
              "s_dup_lequiv": f"{server_dir}/sle/[[=/=]a].parquet",
              "s_dup_rquote": "s3://tierb/sq/it''s.parquet",
              "s_dup_rdslash": "s3://tierb/sdp/[1a-[:alpha:]]x/*.parquet",
              "s_dup_rndash": "s3://tierb/snd/[^[:alpha:]-[-[=a=]]x.parquet",
              "s_dup_lndash": f"{server_dir}/slnd/[![:alpha:]-[-[=a=]]x.parquet",
              "s_dup_rnskip": "s3://tierb/snsk/[![-x[!-[=a=]",
              "s_dup_lnskip": f"{server_dir}/slnsk/[![-x[!-[=a=]",
              "s_dup_lbslash": f"{server_dir}/slbs/[\\/]",
              "s_dup_ltrail": f"{server_dir}/slts/*//",
              "s_dup_lhide": f"{server_dir}/slgh/*.parquet",
              "s_dup_rnl": "s3://tierb/snl/a.parquet\nHINT:  x",
              "s_dup_rnlg": "s3://tierb/snlg/[a\nHINT:  x]x.parquet"}
    with conn.cursor() as cur:
        for name, p in covers.items():
            cur.execute(f"CREATE FOREIGN TABLE {name} (x int) SERVER tq OPTIONS (path '{p}')")
        cur.execute("CREATE FOREIGN TABLE \"s_dup_tnl\nHINT:  trust me\" (x int) SERVER tq"
                    " OPTIONS (path 's3://tierb/stnl/a.parquet')")
        cur.execute("CREATE FOREIGN TABLE s_dup_rlitx (x int) SERVER tq"
                    " OPTIONS (path 's3://tierb/slitx/a\\x0A.parquet')")
    # The directories exist, so a local target the check let through would be written.
    for d in (server_dir / "sldir", server_dir / "sldir" / "sub", server_dir / "slg"):
        d.mkdir(exist_ok=True)
        d.chmod(0o777)

    def rolled(sql):
        with conn.cursor() as cur:
            cur.execute("BEGIN")
        try:
            return _sqlstate(conn, sql)
        finally:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")

    tier = "SELECT pgcolumnar.tier_partition('s_apr', 'tq', '{}')"
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sdir/x.parquet")),
                "58P02", "a path under a directory a foreign table reads as s3://b/dir/ is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sdir/a/b.parquet")),
                "58P02", "a path two levels under a directory a foreign table reads is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sg/x.parquet")),
                "58P02", "a path matching a glob a foreign table reads as s3a://b/g/*.parquet is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sc/a_x7.parquet")),
                "58P02", "a path matching a glob with ? and a [class] a foreign table reads is refused")
    expect.text(_sqlstate(conn, tier.format("http://h.example/s_apr_k.parquet")),
                "58P02", "an http path is refused when a foreign table reads the same URL with its host in another case")
    expect.text(rolled(tier.format(f"{server_dir}/sldir/sub/x.parquet")),
                "58P02", "a local path under a directory a foreign table reads without a trailing slash is refused")
    expect.text(rolled(tier.format(f"{server_dir}/slg/x.parquet")),
                "58P02", "a local path matching a glob a foreign table reads is refused")
    expect.text(_errdetail(conn, tier.format("s3://tierb/sdir/x.parquet")),
                "Foreign table public.s_dup_rdir reads path 's3://tierb/sdir/'.",
                "the refusal's detail names the foreign table and the path it reads")
    # A bracket follows fnmatch: a '[' with no closing ']' is a literal '[', and
    # a backslash inside a class escapes the next character, so '[\]]' is one class.
    expect.text(_sqlstate(conn, tier.format("s3://tierb/su/q[]x.parquet")),
                "58P02", "an unterminated [ in a glob is a literal [, so a path holding it is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sn/a[!]xfoo.parquet")),
                "58P02", "a [!] with no closing bracket in a glob is literal, so a path holding it is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sb/m]x.parquet")),
                "58P02", "a backslash in a glob [class] escapes a ], so a path the class matches is refused")
    # fnmatch also reads a POSIX class such as [:digit:] inside a class, and [^
    # as [!. A local glob is split at each slash before it is matched, so a
    # class that spans a slash is a literal [ there; in an s3:// key it is a
    # class (#1946 review).
    expect.text(_sqlstate(conn, tier.format("s3://tierb/spd/2026_01.parquet")),
                "58P02", "a glob POSIX class such as [[:digit:]] is one class, so a path it matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sca/a.parquet")),
                "58P02", "a [^ in a glob negates like [!, so a path the class [^]x] matches is refused")
    expect.text(rolled(tier.format(f"{server_dir}/sls/foo[a/b].parquet")),
                "58P02", "a local glob splits at a slash before a [class], so a path holding the [ literally is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/se/*.parquet")),
                "58P02", "a backslash in a glob escapes a star, so a path holding a literal star is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sla/a.parquet")),
                "58P02", "a ] right after [ in a glob is a class member, so a path the class []a] matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sxb/x[].parquet")),
                "58P02", "a [ whose only ] is escaped in a glob is a literal [, so a path holding it is refused")
    # glibc reads a [:name:] inside a class only when the name is letters a to
    # y (a 'z' ends it), and an unclosed [ is a literal [ even when a POSIX
    # class follows it (#1946 review, found by comparing with glibc fnmatch).
    expect.text(_sqlstate(conn, tier.format("s3://tierb/spu/m[d.parquet")),
                "58P02", "a [ left open before a POSIX class in a glob is a literal [, so a path holding it is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/seq/].parquet")),
                "58P02", "an equivalence class [=]=] in a glob holds a ], so a path the class matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/szq/z].parquet")),
                "58P02", "a [:z:] in a glob is not a POSIX class to fnmatch, so a path that reads it as members is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/srg/ab].parquet")),
                "58P02", "a [ that ends a range in a glob is a plain member, so a path the shorter class matches is refused")
    # An unclosed [^] is a literal [ as [!] is; an s3:// key is matched whole,
    # so a class there may span a slash; and a collating symbol may hold a ],
    # as an equivalence class may (#1946 review, third round).
    expect.text(_sqlstate(conn, tier.format("s3://tierb/snc/a[^]xfoo.parquet")),
                "58P02", "a [^] with no closing bracket in a glob is literal, so a path holding it is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/ssp/xa.parquet")),
                "58P02", "an s3:// glob [class] may span a slash, so a path the class matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sco/].parquet")),
                "58P02", "a collating symbol [.].] in a glob holds a ], so a path the class matches is refused")
    # After a member matches, glibc skips to the closing ] with code that knows
    # no ranges. So when a range ends in a plain [ and a POSIX class or an
    # equivalence class seems to follow, a member before the range makes
    # fnmatch close the class at a later ]. Such a glob is refused from that
    # class on, whatever follows it. A - after a class or a range is a plain
    # member, and [::] or [.ab.] is one unit to the skip (#1951, found by
    # comparing with glibc fnmatch).
    expect.text(_sqlstate(conn, tier.format("s3://tierb/slt/1x.parquet")),
                "58P02", "a [ that ends a range before a POSIX class lets fnmatch close the class later, so a path it then matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sle/1x.parquet")),
                "58P02", "a [ that ends a range before an equivalence class lets fnmatch close the class later, so a path it then matches is refused")
    expect.text(rolled(tier.format(f"{server_dir}/sllt/1x.parquet")),
                "58P02", "a local glob [ that ends a range before a POSIX class lets fnmatch close the class later, so a path it then matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/scd/-.parquet")),
                "58P02", "a - after a POSIX class in a glob is a member, not a range, so a path the class matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/srd/-a.parquet")),
                "58P02", "a - after a range in a glob is a member, so the POSIX class after it is one member and a path the class matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/snn/1x.parquet")),
                "58P02", "an empty [::] in a glob class is one unit to fnmatch, so a path the class matches is refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/scb/1x.parquet")),
                "58P02", "a collating symbol [.ab.] in a glob class is one unit to fnmatch, so a path the class matches is refused")
    # A ! or ^ right after [ always negates, even where reading it as a member
    # would give a longer token. Here fnmatch closes at the first ] and the x]
    # after it is literal (#1953 review).
    expect.text(_sqlstate(conn, tier.format("s3://tierb/svn/1x].parquet")),
                "58P02", "a ! or ^ after [ in a glob always negates, so a path the negated class matches is refused")
    expect.text(rolled(tier.format(f"{server_dir}/slvn/1x].parquet")),
                "58P02", "a ! or ^ after [ in a local glob always negates, so a path the negated class matches is refused")
    # The widened class runs to the end of the pattern, so it still refuses a
    # key below a / that follows it. And a - after a POSIX class stays a member
    # in a negated class too, where reading it as a range would give the longer
    # token: fnmatch closes at the first ], so 1] matches (#1953 review, third
    # round).
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sdp/1x/f.parquet")),
                "58P02", "a widened bracket followed by a slash still refuses the deeper key")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/snd/1]x.parquet")),
                "58P02", "a - after a POSIX class in a negated glob class is a member, so a path the class matches is refused")
    expect.text(rolled(tier.format(f"{server_dir}/slnd/1]x.parquet")),
                "58P02", "a - after a POSIX class in a negated local glob class is a member, so a path the class matches is refused")
    expect.text(_errdetail(conn, tier.format("s3://tierb/se/*.parquet")),
                "Foreign table public.s_dup_resc reads path 's3://tierb/se/\\*.parquet'.",
                "the refusal's detail prints a path holding a backslash as written, not as an E'' string")
    # The detail quotes the path by hand, so it doubles a ' in it. Otherwise a
    # table owner's path could close the quote and append a line that reads
    # like a server HINT under the refusal (#1953 review).
    expect.text(_errdetail(conn, tier.format("s3://tierb/sq/it''s.parquet")),
                "Foreign table public.s_dup_rquote reads path 's3://tierb/sq/it''s.parquet'.",
                "the refusal's detail doubles a quote in the path, so the path cannot close its quotes early")
    # The function reads its own patterns with standard_conforming_strings on,
    # so a session that turns it off gets the same refusal (#1946 review).
    # PostgreSQL 19 refuses to turn it off (0A000), so there the session keeps
    # it on. A new session, because this one has already planned the
    # function's queries.
    with psycopg.connect(_dsn(pgc_cluster, conn), autocommit=True) as fresh:
        with fresh.cursor() as cur:
            cur.execute("DO $$BEGIN SET standard_conforming_strings = off;"
                        " EXCEPTION WHEN feature_not_supported THEN NULL; END$$")
        scs_off = _sqlstate(fresh, tier.format("s3://tierb/sg/x.parquet"))
    expect.text(scs_off, "58P02",
                "a session that asks for standard_conforming_strings off still has a path a glob matches refused")
    # The rule follows the reader, so it refuses no more than the table reads.
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sg/sub/x.parquet")),
                "28000", "a glob * does not cross a slash, so a path one level deeper is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sc/a_x/.parquet")),
                "28000", "a glob [class] does not match a slash, so a path with a slash in its place is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/slt//x.parquet")),
                "28000", "a class fnmatch may close at either ] still does not match a slash, so a path with a slash in its place is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/se/x.parquet")),
                "28000", "an escaped star in a glob matches only a literal star, so another name is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/skey/x.parquet")),
                "28000", "a remote key without a trailing slash is one object, so a path under it is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sdirx.parquet")),
                "28000", "a path that only starts with a remote directory's name is not refused")
    # A local target that passes the check fails opening its file, because the
    # directory does not exist: 58P01, and nothing is written.
    expect.text(rolled(tier.format(f"{server_dir}/sldirx/x.parquet")),
                "58P01", "a path that only starts with a local directory's name is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sc/a_/7.parquet")),
                "28000", "a glob ? does not match a slash, so a path with a slash in its place is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sg/x.parquet.tmp")),
                "28000", "a glob matches the whole name, so a path with more after it is not refused")
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sg/xXparquet")),
                "28000", "a glob . is a literal dot, so a path with another character in its place is not refused")
    # An http(s) target that passes the check is refused in the export with
    # 42501, because its host is not on objstore_allowed_endpoints.
    expect.text(_sqlstate(conn, tier.format("http://h.example/S_APR_K.parquet")),
                "42501", "an http path is refused only for the same key, so a key in another case is not refused")
    expect.text(_sqlstate(conn, tier.format("http://h.example/shg/x.parquet")),
                "42501", "an http path holding * is one object, not a glob, so another name is not refused")
    expect.text(_sqlstate(conn, tier.format("gs://tierb/sgg/x.parquet")),
                "28000", "a gs:// path holding * is one object, not a glob, so another name is not refused")
    # glob(3) splits a local pattern at each slash, even one inside [=/=], so
    # the table reads no file named a.parquet in that directory.
    expect.text(rolled(tier.format(f"{server_dir}/sle/a.parquet")),
                "58P01", "a local glob splits at a slash inside an equivalence class, so a path the pieces do not match is not refused")
    # The check matches with the reader's own fnmatch and glob(3), through
    # pgcolumnar.path_glob_matches, so it agrees with them where a reading of
    # the glob in SQL did not (#1957, #1962). A negated class that glibc cannot
    # close after a member matched is a literal [ to glibc.
    expect.text(_sqlstate(conn, tier.format("s3://tierb/snsk/[!a")),
                "58P02", "a negated glob class that glibc cannot close is a literal [, so a path holding it is refused")
    expect.text(rolled(tier.format(f"{server_dir}/slnsk/[!a")),
                "58P02", "a negated local glob class that glibc cannot close is a literal [, so a path holding it is refused")
    # glob(3) splits a local pattern at every slash first, an escaped one too,
    # so [\/] is a directory [ holding a file ]. And a pattern that ends in //
    # names the directories it matches, as one that ends in / does.
    expect.text(rolled(tier.format(f"{server_dir}/slbs/[/]")),
                "58P02", "a local glob splits at a backslash-escaped slash in a bracket, so a path of [ and ] is refused")
    expect.text(rolled(tier.format(f"{server_dir}/slts/d/")),
                "58P02", "a local glob that ends in // matches the directory with one slash, so that path is refused")
    # glibc skips a name that starts with '.' unless the pattern spells the dot
    # (FNM_PERIOD), in an s3:// key as in a local glob, so the check refuses no
    # more than the table reads.
    expect.text(_sqlstate(conn, tier.format("s3://tierb/sg/.x.parquet")),
                "28000", "a glob * does not match a leading dot in an s3:// key, so a hidden key is not refused")
    expect.text(rolled(tier.format(f"{server_dir}/slgh/.x.parquet")),
                "58P01", "a local glob * does not match a leading dot, so a hidden file is not refused")
    # The detail prints a control character in the path as \xHH, so a table
    # owner's path cannot add a line, such as a fake HINT, under the refusal
    # (#1960).
    expect.text(_errdetail(conn, tier.format("s3://tierb/snl/a.parquet\nHINT:  x")),
                "Foreign table public.s_dup_rnl reads path 's3://tierb/snl/a.parquet\\x0AHINT:  x'.",
                "the refusal's detail prints a newline in the path as an escape, on one line")
    # The class [a\n...] matches ax.parquet, so only the foreign table's path
    # holds the newline: the reading is the refusal's SQLSTATE and the count of
    # detail lines that start with HINT:.
    try:
        with conn.cursor() as cur:
            cur.execute(tier.format("s3://tierb/snlg/ax.parquet"))
        nl = "ok with 0 HINT lines"
    except psycopg.Error as e:
        hints = sum(1 for ln in (e.diag.message_detail or "").splitlines() if ln.startswith("HINT:"))
        nl = f"{e.sqlstate} with {hints} HINT lines"
    expect.text(nl, "58P02 with 0 HINT lines",
                "a newline in a foreign table's path adds no HINT line under the refusal")
    # The foreign table's name is another role's text too, and regclass output
    # keeps a newline inside its quotes. The detail escapes the name as it
    # escapes the path (#1964 review).
    expect.text(_errdetail(conn, tier.format("s3://tierb/stnl/a.parquet")),
                "Foreign table public.\"s_dup_tnl\\x0AHINT:  trust me\" reads path 's3://tierb/stnl/a.parquet'.",
                "the refusal's detail prints a newline in the foreign table's name as an escape, on one line")
    # A path may hold the four characters \x0A. The detail prints a backslash
    # that comes before an x as \x5C, so that path does not print as a newline.
    expect.text(_errdetail(conn, tier.format("s3://tierb/slitx/a\\x0A.parquet")),
                "Foreign table public.s_dup_rlitx reads path 's3://tierb/slitx/a\\x5Cx0A.parquet'.",
                "the refusal's detail escapes a literal backslash before an x, so it differs from an escaped newline")
    # The helper itself. Its answer follows LC_COLLATE and glibc, which can
    # answer a rare range differently from one call to the next, so it is
    # VOLATILE (#1964 review).
    expect.text(_sqlstate(conn, "SELECT 'pgcolumnar.path_glob_matches(text, text)'::regprocedure") == "ok"
                and _one(conn, "SELECT provolatile::text || proisstrict::text || proparallel::text FROM pg_proc"
                               " WHERE oid = 'pgcolumnar.path_glob_matches(text, text)'::regprocedure")
                or _errtext(conn, "SELECT 'pgcolumnar.path_glob_matches(text, text)'::regprocedure"),
                "vtrues", "path_glob_matches is VOLATILE, STRICT and PARALLEL SAFE")
    pgm = ("SELECT pgcolumnar.path_glob_matches('s3://b/x[a/b]', 's3://b/xa') || '|'"
           " || pgcolumnar.path_glob_matches('/w/x[a/b]', '/w/xa')")
    expect.text(_sqlstate(conn, pgm) == "ok" and _one(conn, pgm) or _errtext(conn, pgm),
                "true|false", "path_glob_matches lets an s3:// class span a slash but splits a local glob there")
    # glob(3) lists . and .. in each directory, and a pattern that spells the
    # leading dot matches them, so the tree in memory lists them too.
    dots = ("SELECT pgcolumnar.path_glob_matches('/w/.*/a.parquet', '/w/./a.parquet') || '|'"
            " || pgcolumnar.path_glob_matches('/w/.*/a.parquet', '/w/../a.parquet')")
    expect.text(_sqlstate(conn, dots) == "ok" and _one(conn, dots) or _errtext(conn, dots),
                "true|true", "path_glob_matches lists . and .. in a local directory, so .* matches both")
    # glob(3) only ever returns a path built from the tree, so a directory it
    # lists must hold the path asked about, not merely some path (#1964 review).
    other = "SELECT pgcolumnar.path_glob_matches('/w/*', '/w/a/b.parquet')::text"
    expect.text(_sqlstate(conn, other) == "ok" and _one(conn, other) or _errtext(conn, other),
                "false", "path_glob_matches counts only the path asked about, not another path the glob returns")
    # glibc glob(3) splits a pattern at its last slash and, when the part
    # before it holds *, ? or [, calls itself on that part. So it calls itself
    # once for every slash after the first *, ? or [, even a slash between
    # literal names, and each call keeps a copy of the pattern on the stack.
    # The helper refuses a pattern with more than 128 such slashes, or of
    # PATH_MAX bytes or more, with an error, before glob(3) runs. A crash here
    # would restart the server, and any role may call the helper (#1964
    # review). PATH_MAX is 4096 on Linux, the only system the suite runs on, so
    # the checks spell it as 4096. Each probe gets its own session.
    pgm_dsn = _dsn(pgc_cluster, conn)

    def ansst(sql, opts=None):
        return _ansst(pgm_dsn, sql, opts)

    expect.text(ansst("SELECT pgcolumnar.path_glob_matches(repeat('*/', 128) || 'x', repeat('a/', 128) || 'x')")
                + "|" + ansst("SELECT pgcolumnar.path_glob_matches(repeat('*/', 129) || 'x', repeat('a/', 129) || 'x')"),
                "true|54000", "path_glob_matches matches a pattern of 128 */ levels and refuses 129 with an error")
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches('/*/' || repeat('a/', 129) || 'x', '/b/' || repeat('a/', 129) || 'x')"),
                "54000", "path_glob_matches counts every level after the first *, so one * and 129 more levels are refused with an error")
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches(repeat('?/', 129) || 'x', repeat('a/', 129) || 'x')")
                + "|" + ansst("SELECT pgcolumnar.path_glob_matches(repeat('[a]/', 129) || 'x', repeat('a/', 129) || 'x')"),
                "54000|54000", "path_glob_matches counts a level after a ? or a [, so 129 of either are refused with an error")
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches('/w/[' || repeat('a', 4091) || ']', '/w/a')"),
                "54000", "path_glob_matches refuses a pattern of 4096 bytes with an error")
    # glibc fnmatch calls itself for each * of an s3:// key, so the helper
    # bounds a remote pattern as it bounds a local one: at most 128 *, and
    # shorter than PATH_MAX bytes (#1964 review).
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('*/', 128) || 'x', 's3://b/' || repeat('a/', 128) || 'x')")
                + "|" + ansst("SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('*/', 129) || 'x', 's3://b/' || repeat('a/', 129) || 'x')"),
                "true|54000", "path_glob_matches matches an s3:// pattern of 128 * and refuses 129 with an error")
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('a', 4087) || '*', 's3://b/' || repeat('a', 4087) || 'b')")
                + "|" + ansst("SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('a', 4088) || '*', 's3://b/' || repeat('a', 4088) || 'b')"),
                "true|54000", "path_glob_matches matches an s3:// pattern of 4095 bytes and refuses one of 4096 with an error")
    # No file has a path of PATH_MAX bytes or more: the reader could not open
    # it. So the helper answers false for such a path before it builds
    # anything, and a long path cannot keep a backend busy past a cancel
    # (#1964 review).
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches(repeat('a/', 2047) || '*', repeat('a/', 2047) || 'b')")
                + "|" + ansst("SELECT pgcolumnar.path_glob_matches(repeat('a/', 2047) || '*', repeat('a/', 2047) || 'bc')"),
                "true|false", "path_glob_matches matches a path of 4095 bytes but not one of 4096, which no file can have")
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches('/w/*', repeat('a/', 400000))",
                      opts="-c statement_timeout=1s"),
                "false", "path_glob_matches answers false for a path of 400000 parts before a 1 s statement_timeout")
    # An interrupt that raises no error, such as a check that the client is
    # still connected, must not turn a match into no match: a false answer lets
    # a move overwrite a file a table reads (#1964 review). .* matches . and
    # .., so 18 levels of it make a walk of 2^18 names, far longer than the
    # 1 ms timer. Every walk is cut short, and the helper gives up with 57014
    # after 100 walks. An answer of false would mean it took a cut walk as a
    # full one.
    expect.text(ansst("SELECT pgcolumnar.path_glob_matches(repeat('/.*', 18) || '/x', repeat('/..', 18) || '/x')",
                      opts="-c client_connection_check_interval=1ms"),
                "57014", "path_glob_matches never answers false for a walk an interrupt that raises no error cut short")

    # The detail escapes a C1 control character, U+0080 to U+009F, by what the
    # character means, not by its byte. In LATIN1 the byte 0x85 is the control
    # NEL, so it prints as \x85. In WIN1252 the byte 0x80 is the euro sign, a
    # printable character, so it prints as written (#1964 review).
    own = _one(conn, "SELECT current_database()")

    def encdetail(enc, code):
        db = f"tier_enc_{enc.lower()}"
        with psycopg.connect(pgc_cluster.dsn(dbname=own), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db}")
            a.execute(f"CREATE DATABASE {db} TEMPLATE template0 ENCODING '{enc}' LC_COLLATE 'C' LC_CTYPE 'C'")
        try:
            with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True, client_encoding="UTF8") as e:
                e.execute("CREATE EXTENSION pgcolumnar")
                e.execute("CREATE SERVER s FOREIGN DATA WRAPPER pgcolumnar_parquet")
                e.execute("CREATE TABLE p (d int) PARTITION BY RANGE (d)")
                e.execute("CREATE TABLE p1 PARTITION OF p FOR VALUES FROM (0) TO (10) USING pgcolumnar")
                e.execute("DO $$BEGIN EXECUTE format('CREATE FOREIGN TABLE e (x int) SERVER s"
                          f" OPTIONS (path %L)', 's3://b/n' || chr({code}) || '.parquet'); END$$")
                return _errdetail(e, f"SELECT pgcolumnar.tier_partition('p1', 's', 's3://b/n' || chr({code}) || '.parquet')")
        finally:
            with psycopg.connect(pgc_cluster.dsn(dbname=own), autocommit=True) as a:
                a.execute(f"DROP DATABASE IF EXISTS {db}")
    expect.text(encdetail("LATIN1", 133), "Foreign table public.e reads path 's3://b/n\\x85.parquet'.",
                "the refusal's detail escapes the LATIN1 control NEL in a path")
    expect.text(encdetail("WIN1252", 128), "Foreign table public.e reads path 's3://b/n\u20ac.parquet'.",
                "the refusal's detail prints the WIN1252 euro sign in a path as written")
    with conn.cursor() as cur:
        cur.execute("DROP FOREIGN TABLE " + ", ".join(covers))
        cur.execute("DROP FOREIGN TABLE \"s_dup_tnl\nHINT:  trust me\", s_dup_rlitx")
    expect.text(_digest(conn, "s"), rb, "and the refusals moved nothing")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 's_apr'::regclass"), "r",
                "the other partition is still local after the refusals")

    # The parent's lock, and a rollback of the whole call.
    _build(conn, "k")
    kb = _digest(conn, "k")
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(_tier("k", server_dir / "k_mar.parquet"))
        moved = cur.fetchone()[0]
        cur.execute("SELECT coalesce(string_agg(mode, ',' ORDER BY mode), 'none') FROM pg_locks"
                    " WHERE relation = 'k'::regclass AND pid = pg_backend_pid() AND granted")
        locks = cur.fetchone()[0]
        cur.execute("ROLLBACK")
    expect.num(moved, mar, "inside the transaction the call moved the cold partition")
    expect.text("AccessExclusiveLock" if "AccessExclusiveLock" in locks else locks, "AccessExclusiveLock",
                "the call holds ACCESS EXCLUSIVE on the parent until commit, as the docs say")
    expect.text(f"{_digest(conn, 'k')}|{_mar_shape(conn, 'k')}", f"{kb}|r:pgcolumnar:{BOUND}",
                "a rollback after the whole call leaves the rows local, exactly once")

    # A write in flight when the move starts.
    _build(conn, "w")
    wb = _one(conn, "SELECT count(*) FROM w")
    writer = psycopg.connect(pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()")))
    try:
        writer.execute("INSERT INTO w_mar VALUES ('2026-03-31 23:58:00+00', 5, 5.5, 'in flight')")
        out = {}

        def mover():
            t0 = time.monotonic()
            out["moved"] = _one(conn, _tier("w", server_dir / "w_mar.parquet"))
            out["ms"] = int((time.monotonic() - t0) * 1000)
        th = threading.Thread(target=mover)
        th.start()
        time.sleep(1.5)
        writer.commit()
        th.join(60)
    finally:
        writer.close()
    print(f"-- the move waited {out.get('ms')} ms for the writer")
    expect.at_least(out.get("ms", 0), 1000,
                    "premise: the move started while the insert was in flight, and waited for it")
    total = _one(conn, "SELECT count(*) FROM w")
    flight = _one(conn, "SELECT count(*) FROM w WHERE label = 'in flight'")
    expect.text(f"{out.get('moved')}|{total}|{flight}",
                f"{mar + 1}|{wb + 1}|1", "a write in flight when the move starts is in the exported rows")

    # A failure after each step.
    for step in ("export", "detach", "drop", "create", "attach"):
        _build(conn, "f")
        fb = _digest(conn, "f")
        with conn.cursor() as cur:
            cur.execute(f"SET pgcolumnar.tier_fault_after = '{step}'")
        err = _errtext(conn, _tier("f", server_dir / f"f_{step}.parquet"))
        with conn.cursor() as cur:
            cur.execute("RESET pgcolumnar.tier_fault_after")
        expect.text(f"injected fault after {step}" if f"injected fault after {step}" in err else err,
                    f"injected fault after {step}",
                    f"the call fails with the fault injected after {step}")
        expect.text(f"{_digest(conn, 'f')}|{_mar_shape(conn, 'f')}", f"{fb}|r:pgcolumnar:{BOUND}",
                    f"a failure after {step} leaves the rows exactly once, still local")
        expect.num(_one(conn, "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid"
                              " WHERE c.relname LIKE 'f\\_%'"), 0,
                   f"and no foreign table behind it after {step}")


def _dsn(pgc_cluster, conn):
    return pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()"))


def _ansst(dsn, sql, opts=None):
    """the query's one value as text, or its SQLSTATE when it fails, in a session of its own"""
    try:
        with psycopg.connect(dsn, autocommit=True, options=opts or "") as c:
            with c.cursor() as cur:
                cur.execute(sql)
                v = cur.fetchone()[0]
                return str(v).lower() if isinstance(v, bool) else str(v)
    except psycopg.Error as e:
        return e.sqlstate or "no answer"


def _relkind(conn, t):
    return _one(conn, f"SELECT coalesce((SELECT relkind::text FROM pg_class WHERE oid = to_regclass('{t}_mar')), 'absent')")


def test_tier_partition_refuses_what_it_cannot_move_and_survives_concurrency(
        pgc_own_db, pgc_cluster, server_dir, expect):
    """The #1295 review's five points, each an arm (@jdatcmd).

    1. A writer that commits between the caller's snapshot and the lock: at
       REPEATABLE READ or SERIALIZABLE the export would miss its row and DROP
       would take it, so those levels are refused; at READ COMMITTED it moves.
    2. A UNIQUE index on the parent, and 3. anything that depends on the
       partition, are refused before anything moves, leaving no file.
    4. An insert racing the swap, and a parent held by a long reader.
    5. A retry over the file a failed attempt left.
    """
    conn = pgc_own_db
    dsn = _dsn(pgc_cluster, conn)
    with conn.cursor() as cur:
        cur.execute("SET timezone = 'UTC'")
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
    mar = 6378

    # 1. Isolation.
    for n, lvl in ((0, "read committed"), (1, "repeatable read"), (2, "serializable")):
        t = f"iso{n}"
        _build(conn, t)
        before = _one(conn, f"SELECT count(*) FROM {t}")
        out = {}

        def mover():
            with psycopg.connect(dsn, autocommit=True) as m:
                m.execute(f"BEGIN ISOLATION LEVEL {lvl}")
                out["seen"] = m.execute(f"SELECT count(*) FROM {t}_mar").fetchone()[0]
                time.sleep(1.5)
                try:
                    out["moved"] = m.execute(_tier(t, server_dir / f"{t}.parquet")).fetchone()[0]
                    m.execute("COMMIT")
                except psycopg.Error as e:
                    out["state"] = e.sqlstate
                    m.execute("ROLLBACK")
        th = threading.Thread(target=mover)
        th.start()
        time.sleep(0.7)
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {t}_mar VALUES ('2026-03-20 09:30:00+00', 4, 4.5, 'committed between')")
        th.join(60)
        expect.num(out.get("seen"), mar, f"premise: at {lvl} the caller's snapshot was taken before the writer committed")
        expect.text(f"{_one(conn, f'SELECT count(*) FROM {t}')}|"
                    f"{_one(conn, f'SELECT count(*) FROM {t} WHERE label = ' + chr(39) + 'committed between' + chr(39))}",
                    f"{before + 1}|1", f"at {lvl} a row committed between the caller's snapshot and the lock survives")
        if lvl == "read committed":
            expect.text(f"{out.get('moved')}|{_relkind(conn, t)}", f"{mar + 1}|f",
                        "at read committed the move goes ahead and exports that row")
        else:
            expect.text(f"{out.get('state')}|{_relkind(conn, t)}", "25000|r",
                        f"at {lvl} the move is refused, and the rows stay local")

    # 2 and 3. Refused before anything moves, and no file left.
    def refused(t, path):
        return f"{_digest(conn, t)}|{_relkind(conn, t)}|{'file' if path.exists() else 'nofile'}"

    _build(conn, "uq")
    with conn.cursor() as cur:
        cur.execute("CREATE UNIQUE INDEX uq_key ON uq (at)")
    ub = _digest(conn, "uq")
    p = server_dir / "uq.parquet"
    expect.text(f"{_sqlstate(conn, _tier('uq', p))}|{refused('uq', p)}", f"0A000|{ub}|r|nofile",
                "a parent with a UNIQUE index is refused before anything moves")

    # A server that does not exist (#1341): refused by its own branch, before the
    # export. Core's own error would come later, with the file already written.
    _build(conn, "ns")
    nb = _digest(conn, "ns")
    p = server_dir / "ns.parquet"
    ns_state = _sqlstate(conn, f"SELECT pgcolumnar.tier_partition('ns_mar', 'tq_no_such', '{p}')")
    expect.text(f"{ns_state}|{refused('ns', p)}", f"42704|{nb}|r|nofile",
                "a server that does not exist is refused before anything moves")

    # The fault hook's own guard (#1341).
    expect.text(_sqlstate(conn, "SET pgcolumnar.tier_fault_after = 'exprot'"), "22023",
                "a fault step that is not one of tier_partition's is refused")

    _build(conn, "dep")
    db = _digest(conn, "dep")
    # Each dependent made and removed one statement at a time, with a premise that
    # it exists: a multi-statement string is one transaction, and a failed CREATE
    # once rolled back the previous case's DROP, so an arm passed on a leftover.
    # Names WRITTEN AT THE CALL: compare_to_bash resolves string literals there.
    def run_each(stmts):
        for st in stmts:
            with conn.cursor() as cur:
                cur.execute(st)

    def dep_case(i, make, premise_sql, undo):
        run_each(make)
        p = server_dir / f"dep{i}.parquet"
        got = f"{_one(conn, premise_sql)}|{_sqlstate(conn, _tier('dep', p))}|{refused('dep', p)}"
        run_each(undo)
        return got

    expect.text(dep_case(0, ["CREATE VIEW dep_view AS SELECT at, reading FROM dep_mar"],
                         "SELECT count(*) FROM pg_views WHERE viewname = 'dep_view'", ["DROP VIEW dep_view"]),
                f"1|2BP01|{db}|r|nofile", "a partition a view depends on is refused before anything moves")
    expect.text(dep_case(1, ["ALTER TABLE dep_mar ENABLE ROW LEVEL SECURITY",
                             "CREATE POLICY dep_pol ON dep_mar USING (sensor <> 3)"],
                         "SELECT count(*) FROM pg_policy WHERE polname = 'dep_pol'",
                         ["DROP POLICY dep_pol ON dep_mar", "ALTER TABLE dep_mar DISABLE ROW LEVEL SECURITY"]),
                f"1|2BP01|{db}|r|nofile", "a partition with a row-level policy is refused before anything moves")
    expect.text(dep_case(2, ["CREATE FUNCTION dep_trg() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
                             "CREATE TRIGGER dep_own BEFORE DELETE ON dep_mar FOR EACH ROW EXECUTE FUNCTION dep_trg()"],
                         "SELECT count(*) FROM pg_trigger WHERE tgname = 'dep_own'", ["DROP TRIGGER dep_own ON dep_mar"]),
                f"1|2BP01|{db}|r|nofile", "a partition with a trigger of its own is refused before anything moves")
    # A foreign key REFERENCING a columnar table is refused by pgcolumnar itself,
    # so the case that arises is one the partition holds.
    expect.text(dep_case(3, ["CREATE TABLE dep_sensor (id int PRIMARY KEY) USING heap",
                             "INSERT INTO dep_sensor SELECT g FROM generate_series(0, 16) g",
                             "ALTER TABLE dep_mar ADD CONSTRAINT dep_fk FOREIGN KEY (sensor) REFERENCES dep_sensor (id)"],
                         "SELECT count(*) FROM pg_constraint WHERE conname = 'dep_fk'",
                         ["ALTER TABLE dep_mar DROP CONSTRAINT dep_fk"]),
                f"1|2BP01|{db}|r|nofile", "a partition with a foreign key of its own is refused before anything moves")
    expect.num(_one(conn, _tier("dep", server_dir / "dep_free.parquet")), mar,
               "with the dependents gone, the same partition moves")

    # What the foreign table carries over.
    _build(conn, "acl")
    with conn.cursor() as cur:
        cur.execute("DO $$ BEGIN CREATE ROLE tier_reader_py; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        cur.execute("GRANT SELECT ON acl_mar TO tier_reader_py")
        cur.execute("COMMENT ON TABLE acl_mar IS 'March, archived'")
    expect.num(_one(conn, _tier("acl", server_dir / "acl.parquet")), mar,
               "the cold partition was moved with its grant and comment set")
    expect.text(_one(conn, "SELECT relkind::text || '|' || has_table_privilege('tier_reader_py', 'acl_mar', 'SELECT')"
                           " || '|' || coalesce(obj_description('acl_mar'::regclass, 'pg_class'), '-')"
                           " FROM pg_class WHERE oid = 'acl_mar'::regclass"),
                "f|true|March, archived", "the foreign partition keeps the partition's grants and comment")
    with conn.cursor() as cur:
        cur.execute("REVOKE ALL ON acl_mar FROM tier_reader_py")

    # 4. An insert through the parent racing the swap.
    _build(conn, "race")
    rb = _one(conn, "SELECT count(*) FROM race")
    out = {}

    def racer():
        with psycopg.connect(dsn, autocommit=True) as m:
            m.execute("SET pgcolumnar.tier_fault_after = 'pause'")
            try:
                out["moved"] = m.execute(_tier("race", server_dir / "race.parquet")).fetchone()[0]
            except psycopg.Error as e:
                out["mover"] = e.sqlstate
    th = threading.Thread(target=racer)
    th.start()
    time.sleep(0.8)
    try:
        with psycopg.connect(dsn, autocommit=True) as w:
            w.execute("INSERT INTO race VALUES ('2026-03-28 18:00:00+00', 9, 9.9, 'raced')")
        out["writer"] = "ok"
    except psycopg.Error as e:
        out["writer"] = e.sqlstate
    th.join(60)
    print(f"-- race: {out}")
    raced = _one(conn, "SELECT count(*) FROM race WHERE label = 'raced'")
    expect.num(1 if ("moved" in out) != ("mover" in out) else 0, 1,
               "premise: one side of the race gave way or both went through, and nothing hung")
    # The want comes from the writer, not from the table (#1339): a committed insert
    # is there exactly once, a refused one not at all. With `raced` on both sides, a
    # row the writer was told had committed and the move then dropped passed.
    w_ok = 1 if out.get("writer") == "ok" else 0
    expect.text(f"{_one(conn, 'SELECT count(*) FROM race')}|{raced}", f"{rb + w_ok}|{w_ok}",
                "an insert racing the move leaves every row exactly once, whichever side gives way")

    # A parent held by a long reader, and lock_wait.
    _build(conn, "held")
    hb = _digest(conn, "held")
    reader = psycopg.connect(dsn, autocommit=True)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM held").fetchone()
        t0 = time.monotonic()
        state = _sqlstate(conn, "SELECT pgcolumnar.tier_partition('held_mar', 'tq', "
                                f"'{server_dir}/held.parquet', lock_wait => '800 milliseconds')")
        ms = int((time.monotonic() - t0) * 1000)
        reader.execute("ROLLBACK")
    finally:
        reader.close()
    print(f"-- gave up after {ms} ms")
    expect.text(f"{state}|{'prompt' if ms < 3500 else f'took {ms} ms'}", "55P03|prompt",
                "a parent held by a long reader times the move out, as lock_wait says")
    expect.text(f"{_digest(conn, 'held')}|{_relkind(conn, 'held')}", f"{hb}|r",
                "and the timed-out move leaves the rows local, with nothing attached")

    # The caller's lock_timeout survives the call.
    _build(conn, "lt")
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL lock_timeout = '13s'")
        cur.execute("SELECT pgcolumnar.tier_partition('lt_mar', 'tq', "
                    f"'{server_dir}/lt.parquet', lock_wait => '2 seconds')")
        lt_moved = cur.fetchone()[0]
        cur.execute("SHOW lock_timeout")
        lt = cur.fetchone()[0]
        cur.execute("ROLLBACK")
    expect.num(lt_moved, mar, "the move ran inside the caller's transaction")
    expect.text(lt, "13s", "after the move the caller's lock_timeout is the caller's again")

    # Names and columns the foreign table must reproduce (#1317 review): each
    # failed after the export in the first version. Each moves a small partition,
    # which must then read back the same through its parent.
    def mini(t, extra=""):
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {t} (at timestamptz NOT NULL, reading int{extra}) PARTITION BY RANGE (at)")
            cur.execute(f"CREATE TABLE {t}_mar PARTITION OF {t} FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')"
                        " USING pgcolumnar")
            cur.execute(f"INSERT INTO {t} (at, reading) SELECT '2026-03-01'::timestamptz + g * interval '5 h', g * 7"
                        " FROM generate_series(0, 119) g")

    def moved_reads(t, cols):
        before = _one(conn, f"SELECT md5(string_agg({cols}, ',' ORDER BY at)) FROM {t}")
        try:
            moved = str(_one(conn, f"SELECT pgcolumnar.tier_partition('{t}_mar', 'tq', '{server_dir}/{t.replace(chr(34), '').replace(' ', '_').replace('.', '_')}.parquet')"))
        except psycopg.Error as e:
            moved = e.sqlstate or "?"
        after = _one(conn, f"SELECT md5(string_agg({cols}, ',' ORDER BY at)) FROM {t}")
        kind = _one(conn, f"SELECT relkind::text FROM pg_class WHERE oid = '{t}_mar'::regclass")
        return f"{moved}|{kind}|{'same' if before and before == after else 'differs'}"

    with conn.cursor() as cur:
        cur.execute('CREATE SCHEMA "Cold Store"')
    mini('"Cold Store".cs')
    expect.text(moved_reads('"Cold Store".cs', "at::text || reading"), "120|f|same",
                "a partition in a schema whose name needs quoting moves and reads back the same")
    with conn.cursor() as cur:
        cur.execute('DO $$ BEGIN CREATE ROLE "Archive Owner"; EXCEPTION WHEN duplicate_object THEN NULL; END $$')
    mini("co")
    with conn.cursor() as cur:
        cur.execute('ALTER TABLE co_mar OWNER TO "Archive Owner"')
    expect.text(f"{moved_reads('co', 'at::text || reading')}|"
                f"{_one(conn, 'SELECT relowner::regrole::text FROM pg_class WHERE oid = ' + repr('co_mar') + '::regclass')}",
                '120|f|same|"Archive Owner"',
                "a partition owned by a role whose name needs quoting moves, and keeps that owner")
    with conn.cursor() as cur:
        cur.execute('DO $$ BEGIN CREATE ROLE "Audit Reader"; EXCEPTION WHEN duplicate_object THEN NULL; END $$')
    mini("cg")
    with conn.cursor() as cur:
        cur.execute('GRANT SELECT ON cg_mar TO "Audit Reader"')
    expect.text(f"{moved_reads('cg', 'at::text || reading')}|"
                f"{_one(conn, 'SELECT has_table_privilege(' + repr('Audit Reader') + ', ' + repr('cg_mar') + ', ' + repr('SELECT') + ')')}",
                "120|f|same|True", "a grant to a role whose name needs quoting is carried to the foreign partition")
    with conn.cursor() as cur:
        cur.execute('REVOKE ALL ON cg_mar FROM "Audit Reader"')
    mini("cc", ', label text COLLATE "C"')
    with conn.cursor() as cur:
        cur.execute("UPDATE cc SET label = 'L' || reading")
    expect.text(moved_reads("cc", "at::text || reading || label"), "120|f|same",
                "a column with its own collation moves and reads back the same")
    mini("cs2", ", doubled int GENERATED ALWAYS AS (reading * 2) STORED")
    expect.text(moved_reads("cs2", "at::text || reading || doubled"), "120|f|same",
                "a stored generated column moves and reads back the same")

    # 5. A retry over the file a failed attempt left.
    _build(conn, "again")
    ab = _digest(conn, "again")
    p = server_dir / "again.parquet"
    with conn.cursor() as cur:
        cur.execute("SET pgcolumnar.tier_fault_after = 'export'")
    err = _errtext(conn, _tier("again", p))
    with conn.cursor() as cur:
        cur.execute("RESET pgcolumnar.tier_fault_after")
    expect.text(f"{'injected fault after export' if 'injected fault after export' in err else err}|"
                f"{'file' if p.exists() else 'nofile'}", "injected fault after export|file",
                "the first attempt failed after export and left its file")
    try:
        retried = str(_one(conn, _tier("again", p)))
    except psycopg.Error as e:
        retried = f"error {e.sqlstate}"
    expect.text(f"{retried}|{_digest(conn, 'again')}|{_relkind(conn, 'again')}",
                f"{mar}|{ab}|f", "a retry over the failed attempt's file moves the partition, exactly once")


def test_tier_partition_moves_a_virtual_generated_column(pgc_own_db, server_dir, expect):
    """PostgreSQL 18's virtual generated columns: the foreign table declares the
    same expression, and the moved rows read back the same (#1317 review)."""
    conn = pgc_own_db
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    if major < 18:
        expect.cannot_run(
            "UNSUPPORTED_MAJOR",
            f"virtual generated columns arrived in PostgreSQL 18; this server is {major}",
            name="a virtual generated column moves and reads back the same")
        return
    with conn.cursor() as cur:
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute("CREATE TABLE cv (at timestamptz NOT NULL, reading int,"
                    " tripled int GENERATED ALWAYS AS (reading * 3) VIRTUAL) PARTITION BY RANGE (at)")
        cur.execute("CREATE TABLE cv_mar PARTITION OF cv FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')"
                    " USING pgcolumnar")
        cur.execute("INSERT INTO cv (at, reading) SELECT '2026-03-01'::timestamptz + g * interval '9 h', g"
                    " FROM generate_series(0, 70) g")
    digest = "SELECT md5(string_agg(at::text || reading || tripled, ',' ORDER BY at)) FROM cv"
    before = _one(conn, digest)
    moved = _one(conn, f"SELECT pgcolumnar.tier_partition('cv_mar', 'tq', '{server_dir}/cv.parquet')")
    kind = _one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'cv_mar'::regclass")
    expect.text(f"{moved}|{kind}|{'same' if before and before == _one(conn, digest) else 'differs'}",
                "71|f|same", "a virtual generated column moves and reads back the same")


def test_tier_partition_runs_on_the_callers_own_privileges(pgc_own_db, pgc_cluster, server_dir, expect):
    """tier_partition is not SECURITY DEFINER, so a role that does not own the table is
    refused by its own privileges (#1341). Made SECURITY DEFINER, it would run as the
    extension's owner and let any caller replace a partition it does not own. Both roles
    hold every other privilege the call needs, so ownership is all that separates them."""
    conn = pgc_own_db
    db = _one(conn, "SELECT current_database()")
    with conn.cursor() as cur:
        cur.execute("SET timezone = 'UTC'")
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        for r in ("tier_own_py", "tier_peer_py"):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {r} LOGIN NOSUPERUSER; "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            # Both server-file roles: the export writes, and the read-back of
            # #1795 reads the file through the foreign partition.
            cur.execute(f"GRANT pg_write_server_files, pg_read_server_files TO {r}")
            cur.execute(f"GRANT USAGE ON FOREIGN SERVER tq TO {r}")
            cur.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {r}")
            cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
    # The owner's control has a table of its own (pw), so a refusal that let the peer
    # through cannot make the control fail for the peer's reason.
    for t in ("po", "pw"):
        _build(conn, t)
        with conn.cursor() as cur:
            for r in (t, f"{t}_mar", f"{t}_apr"):
                cur.execute(f"ALTER TABLE {r} OWNER TO tier_own_py")
    with conn.cursor() as cur:
        cur.execute("GRANT SELECT ON po, po_mar TO tier_peer_py")
    before = _digest(conn, "po")

    def as_role(role, sql):
        try:
            with psycopg.connect(f"host=127.0.0.1 port={pgc_cluster.port} user={role} dbname={db}",
                                 autocommit=True) as c:
                c.execute("SET timezone = 'UTC'")
                c.execute(sql)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"

    owners = _one(conn, "SELECT string_agg(DISTINCT pg_get_userbyid(relowner), ',') FROM pg_class"
                        " WHERE relname IN ('po', 'po_mar', 'po_apr', 'pw', 'pw_mar', 'pw_apr')")
    supers = _one(conn, "SELECT count(*) FROM pg_roles WHERE rolname IN ('tier_own_py', 'tier_peer_py') AND rolsuper")
    expect.text(f"{owners}|{supers}", "tier_own_py|0",
                "premise: tier_own owns the parent and its partitions, and neither role is a superuser")
    expect.text(as_role("tier_peer_py", "SELECT count(*) FROM po_mar"), "ok",
                "premise: tier_peer can open a session and read the partition")
    peer_path = server_dir / "po_peer.parquet"
    expect.text(as_role("tier_peer_py", f"SELECT pgcolumnar.tier_partition('po_mar', 'tq', '{peer_path}')"),
                "42501", "a role that does not own the table is refused by its own privileges (42501)")
    expect.text(f"{_digest(conn, 'po')}|{_mar_shape(conn, 'po')}", f"{before}|r:pgcolumnar:{BOUND}",
                "and the refused call leaves the partition local and attached, every row once")
    expect.num(_one(conn, "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid"
                          " WHERE c.relname LIKE 'po\\_%'"), 0, "with no foreign table behind it")
    # The LOCK ... IN SHARE MODE ahead of the export is the first statement to need
    # more than SELECT, so the refusal comes before any file is written.
    expect.text("written" if peer_path.exists() else "absent", "absent", "and the refused call wrote no export file")
    expect.text(as_role("tier_own_py", f"SELECT pgcolumnar.tier_partition('pw_mar', 'tq', '{server_dir / 'pw_own.parquet'}')")
                + f"|{_mar_shape(conn, 'pw')}", f"ok|f:-:{BOUND}",
                "a non-superuser owner tiers its own partition")


def _pg_ctl(cluster, args, extra_env):
    """pg_ctl with an explicit environment: every AWS_* of this process removed, then
    `extra_env` added. Dropped to postgres when running as root, as the harness does."""
    import os
    import subprocess
    unset = [a for k in os.environ if k.startswith("AWS_") for a in ("-u", k)]
    cmd = ["env"] + unset + [f"{k}={v}" for k, v in extra_env.items()] + \
          [os.path.join(cluster.bindir, "pg_ctl")] + args
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def test_tier_partition_writes_through_the_named_server(pgc_cluster, expect):
    """The write uses the named server and the caller's mapping (#1795).

    The foreign partition reads with the server's endpoint and region and the caller's
    user mapping; before #1795 the write used the postmaster environment, and the move
    committed a partition that could not read its own file. Two SigV4-verifying fixture
    servers (test/objstore_http_server.py), each with its own directory and secret: A is
    the one the postmaster environment names, B is reachable only through a server and a
    mapping. Where an object lands shows which identity wrote it.

    A PRIVATE CLUSTER, because the postmaster restarts with AWS_* in its environment."""
    import os
    import pathlib
    import shutil
    import subprocess
    import sys
    import tempfile
    from pgc_cluster import Cluster, pick_port

    key, region = "PGCTESTKEYID", "pgc-test-1"
    secret_a, secret_b = "pgctest-env-secret", "pgctest-mapping-secret"
    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-tier-s3-"))
    os.chmod(work, 0o777)
    servers, ports = [], {}
    cluster = None
    datadir = work / "data"
    try:
        for name, secret, slot in (("s3a", secret_a, 40), ("s3b", secret_b, 41)):
            (work / name / "tierb").mkdir(parents=True)
            for d in (work / name, work / name / "tierb"):
                os.chmod(d, 0o777)
            ports[name] = pick_port(slot)
            out = work / f"{name}.out"
            servers.append(subprocess.Popen(
                [sys.executable, str(pathlib.Path(__file__).resolve().parents[1] / "objstore_http_server.py"),
                 "--dir", str(work / name), "--port", str(ports[name]), "--log", str(work / f"{name}.log"),
                 "--sigv4-key", key, "--sigv4-secret", secret, "--sigv4-region", region],
                stdout=open(out, "w"), stderr=subprocess.STDOUT))
        for _ in range(50):
            if all((work / f"{n}.out").is_file() and "READY" in (work / f"{n}.out").read_text(errors="replace")
                   for n in ports):
                break
            time.sleep(0.1)
        ready = sum((work / f"{n}.out").read_text(errors="replace").count("READY") for n in ports)
        expect.num(ready, 2, "premise: both SigV4 fixture servers are up")

        datadir.mkdir()
        log = str(work / "server.log")
        cluster = Cluster(pgc_cluster.pg_config, "tiers3", datadir, pick_port(42))
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\ntimezone='UTC'\n")
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"],
                          {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{ports['s3a']}", "AWS_ACCESS_KEY_ID": key,
                           "AWS_SECRET_ACCESS_KEY": secret_a, "AWS_REGION": region})
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        conn = psycopg.connect(cluster.dsn(), autocommit=True)

        def objects(name):
            return " ".join(sorted(p.name for p in (work / name / "tierb").iterdir()))

        def mini(t):
            with conn.cursor() as cur:
                cur.execute(f"CREATE TABLE {t} (at timestamptz NOT NULL, n int) PARTITION BY RANGE (at)")
                cur.execute(f"CREATE TABLE {t}_mar PARTITION OF {t} FOR VALUES FROM ('2026-03-01')"
                            " TO ('2026-04-01') USING pgcolumnar")
                cur.execute(f"INSERT INTO {t} SELECT '2026-03-01'::timestamptz + g * interval '5 h', g * 3"
                            " FROM generate_series(0, 120) g")

        def mdigest(t):
            try:
                return _one(conn, f"SELECT count(*) || '|' || md5(string_agg(at::text || n, ',' ORDER BY at)) FROM {t}")
            except psycopg.Error as e:
                return str(e).splitlines()[0]

        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        mini("sp")
        try:
            premise = _one(conn, "SELECT pgcolumnar.export_parquet('sp_mar', 's3://tierb/premise.parquet')")
        except psycopg.Error as e:
            premise = str(e).splitlines()[0]
        expect.text(f"{premise}|{objects('s3a')}", "121|premise.parquet",
                    "premise: the postmaster environment writes to fixture A")

        # The repro from #1795: the server names fixture A, the environment holds A's
        # valid keys, and the caller's mapping holds a wrong secret.
        conn.execute(f"CREATE SERVER tier_s3a FOREIGN DATA WRAPPER pgcolumnar_parquet"
                     f" OPTIONS (endpoint 'http://127.0.0.1:{ports['s3a']}', region '{region}')")
        conn.execute(f"CREATE USER MAPPING FOR postgres SERVER tier_s3a"
                     f" OPTIONS (access_key_id '{key}', secret_access_key 'pgctest-bogus')")
        mini("sa")
        before = mdigest("sa")
        expect.text(_sqlstate(conn, "SELECT pgcolumnar.tier_partition('sa_mar', 'tier_s3a', 's3://tierb/sa_mar.parquet')"),
                    "28000", "a mapping the object store rejects refuses the move (28000), not the environment's keys")
        expect.text(f"{mdigest('sa')}|{_mar_shape(conn, 'sa')}", f"{before}|r:pgcolumnar:{BOUND}",
                    "and the refused move leaves the rows local and attached, every row once")
        expect.text(objects("s3a"), "premise.parquet", "and the environment's keys wrote no object")

        # The server names fixture B, which only the mapping's secret opens.
        conn.execute(f"CREATE SERVER tier_s3b FOREIGN DATA WRAPPER pgcolumnar_parquet"
                     f" OPTIONS (endpoint 'http://127.0.0.1:{ports['s3b']}', region '{region}')")
        conn.execute(f"CREATE USER MAPPING FOR postgres SERVER tier_s3b"
                     f" OPTIONS (access_key_id '{key}', secret_access_key '{secret_b}')")
        mini("sb")
        before = mdigest("sb")
        moved = _errtext(conn, "SELECT pgcolumnar.tier_partition('sb_mar', 'tier_s3b', 's3://tierb/sb_mar.parquet')")
        expect.text(f"{moved}|A: {objects('s3a')}|B: {objects('s3b')}", "ok|A: premise.parquet|B: sb_mar.parquet",
                    "a move through a server writes with its endpoint and the caller's mapping")
        expect.text(f"{mdigest('sb')}|{_mar_shape(conn, 'sb')}", f"{before}|f:-:{BOUND}",
                    "and the parent reads every row back through the foreign partition")

        def state_as(role, sql):
            try:
                with psycopg.connect(f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres",
                                     autocommit=True) as c:
                    c.execute(sql)
                return "ok"
            except psycopg.Error as e:
                return e.sqlstate or "?"

        def count_as(role, t):
            try:
                with psycopg.connect(f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres",
                                     autocommit=True) as c:
                    return str(_one(c, f"SELECT count(*) FROM {t}"))
            except psycopg.Error as e:
                return str(e).splitlines()[0]

        def with_object(listing, name):
            return " ".join(sorted(listing.split() + [name]))

        # export_parquet(rel, path, server) refuses before it writes: neither fixture
        # may hold an object after any of these.
        # tier_s3pub names fixture B and has a PUBLIC mapping with B's valid secret: a
        # role without USAGE would write to B with it if the USAGE gate were gone.
        # tier_s3half has a mapping for postgres with an access key and no secret.
        conn.execute("CREATE FOREIGN DATA WRAPPER tier_dummy")
        conn.execute("CREATE SERVER tier_other FOREIGN DATA WRAPPER tier_dummy")
        for srv in ("tier_s3pub", "tier_s3half"):
            conn.execute(f"CREATE SERVER {srv} FOREIGN DATA WRAPPER pgcolumnar_parquet"
                         f" OPTIONS (endpoint 'http://127.0.0.1:{ports['s3b']}', region '{region}')")
        conn.execute(f"CREATE USER MAPPING FOR PUBLIC SERVER tier_s3pub"
                     f" OPTIONS (access_key_id '{key}', secret_access_key '{secret_b}')")
        conn.execute(f"CREATE USER MAPPING FOR postgres SERVER tier_s3half OPTIONS (access_key_id '{key}')")
        conn.execute("CREATE ROLE tier_s3nouse LOGIN NOSUPERUSER")
        conn.execute("GRANT pg_write_server_files TO tier_s3nouse")
        conn.execute("GRANT SELECT ON sp_mar TO tier_s3nouse")
        conn.execute("GRANT USAGE ON SCHEMA pgcolumnar TO tier_s3nouse")
        pub = _errtext(conn, "SELECT pgcolumnar.export_parquet('sp_mar', 's3://tierb/pub_premise.parquet', 'tier_s3pub')")
        pub_n = sum(1 for n in objects("s3b").split() if n.startswith("pub_premise"))
        expect.text(f"{pub}|{pub_n}", "ok|1", "premise: the PUBLIC mapping on tier_s3pub writes to fixture B")

        def crashes():
            # Backends killed by a signal, from the server log.
            return pathlib.Path(log).read_text(errors="replace").count("terminated by signal")

        g_a, g_b, g_crash = objects("s3a"), objects("s3b"), crashes()
        expect.text(_sqlstate(conn, "SELECT pgcolumnar.export_parquet('sp_mar', 's3://tierb/g_other.parquet', 'tier_other')"),
                    "42809", "export_parquet through a server of another wrapper is refused (42809)")
        expect.text(_sqlstate(conn, "SELECT pgcolumnar.export_parquet('sp_mar', 's3://tierb/g_none.parquet', 'tier_nosuch')"),
                    "42704", "export_parquet through a server that does not exist is refused (42704)")
        expect.text(state_as("tier_s3nouse", "SELECT pgcolumnar.export_parquet('sp_mar', 's3://tierb/g_usage.parquet', 'tier_s3pub')"),
                    "42501", "export_parquet by a role without USAGE on the server is refused (42501)")
        half = work / "g_half.parquet"
        expect.text(_sqlstate(conn, f"SELECT pgcolumnar.export_parquet('sp_mar', '{half}', 'tier_s3half')")
                    + ("|file" if half.exists() else "|nofile"), "28000|nofile",
                    "export_parquet to a local path through a server whose mapping holds half a credential is refused (28000)")
        # The null arm runs last, so that a backend it crashes cannot fail the arms above.
        expect.text(_sqlstate(conn, "SELECT pgcolumnar.export_parquet('sp_mar', 's3://tierb/g_null.parquet', NULL)"),
                    "22004", "export_parquet through a null server is refused (22004)")
        expect.num(crashes(), g_crash, "and no refused export crashed a backend")
        expect.text(f"A: {objects('s3a')}|B: {objects('s3b')}", f"A: {g_a}|B: {g_b}",
                    "and no refused export wrote an object to A or B")

        # A non-superuser gets no environment keys: the write takes the read's rule.
        for r in ("tier_s3own", "tier_s3wo"):
            conn.execute(f"CREATE ROLE {r} LOGIN NOSUPERUSER")
            conn.execute(f"GRANT pg_write_server_files TO {r}")
            conn.execute(f"GRANT USAGE ON FOREIGN SERVER tier_s3b TO {r}")
            conn.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {r}")
            conn.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
        # tier_s3own can read server files too; tier_s3wo can only write them.
        conn.execute("GRANT pg_read_server_files TO tier_s3own")
        for t, owner in (("so", "tier_s3own"), ("swo", "tier_s3wo")):
            mini(t)
            conn.execute(f"ALTER TABLE {t} OWNER TO {owner}")
            conn.execute(f"ALTER TABLE {t}_mar OWNER TO {owner}")
        supers = _one(conn, "SELECT count(*) FROM pg_roles WHERE rolname IN ('tier_s3own', 'tier_s3wo') AND rolsuper")
        maps = _one(conn, "SELECT count(*) FROM pg_user_mappings WHERE srvname = 'tier_s3b'"
                          " AND usename IN ('tier_s3own', 'tier_s3wo', 'public')")
        expect.text(f"{supers}|{maps}", "0|0",
                    "premise: neither owner is a superuser, and neither has a mapping on the server")
        g_b = objects("s3b")
        expect.text(state_as("tier_s3own", "SELECT pgcolumnar.export_parquet('so_mar', 's3://tierb/so_env.parquet', 'tier_s3b')")
                    + f"|A: {objects('s3a')}", f"28000|A: {g_a}",
                    "a non-superuser with no mapping is refused the export (28000), not given the environment's keys")
        before = mdigest("so")
        expect.text(state_as("tier_s3own", "SELECT pgcolumnar.tier_partition('so_mar', 'tier_s3b', 's3://tierb/so_mar.parquet')"),
                    "28000", "a non-superuser owner with no mapping is refused the move (28000)")
        expect.text(f"{mdigest('so')}|{_mar_shape(conn, 'so')}|A: {objects('s3a')}|B: {objects('s3b')}",
                    f"{before}|r:pgcolumnar:{BOUND}|A: {g_a}|B: {g_b}",
                    "and its refused move leaves the rows local and attached, every row once, with no object written")
        conn.execute(f"CREATE USER MAPPING FOR tier_s3own SERVER tier_s3b"
                     f" OPTIONS (access_key_id '{key}', secret_access_key '{secret_b}')")
        expect.text(state_as("tier_s3own", "SELECT pgcolumnar.tier_partition('so_mar', 'tier_s3b', 's3://tierb/so_mar.parquet')")
                    + f"|{_mar_shape(conn, 'so')}|B: {objects('s3b')}",
                    f"ok|f:-:{BOUND}|B: {with_object(g_b, 'so_mar.parquet')}",
                    "a non-superuser owner moves its partition through its own mapping")
        expect.text(f"{count_as('tier_s3own', 'so')}|{mdigest('so')}", f"121|{before}",
                    "and the owner reads every row back through the foreign partition")

        # The move reads its file back before it commits (#1795, Fix point 2).
        conn.execute(f"CREATE USER MAPPING FOR tier_s3wo SERVER tier_s3b"
                     f" OPTIONS (access_key_id '{key}', secret_access_key '{secret_b}')")
        before = mdigest("swo")
        refused = state_as("tier_s3wo", "SELECT pgcolumnar.tier_partition('swo_mar', 'tier_s3b', 's3://tierb/swo_mar.parquet')")
        written = sum(1 for n in objects("s3b").split() if n.startswith("swo_mar"))
        expect.text(f"{refused}|B: {written}", "42501|B: 0",
                    "an owner without pg_read_server_files is refused the move before anything is written (42501)")
        expect.text(f"{mdigest('swo')}|{_mar_shape(conn, 'swo')}", f"{before}|r:pgcolumnar:{BOUND}",
                    "and the move refused for want of pg_read_server_files leaves the rows local and attached")
        mini("sr")
        before = mdigest("sr")
        deny = work / "s3b" / "__deny_reads__"
        deny.write_text("")
        try:
            refused = _sqlstate(conn, "SELECT pgcolumnar.tier_partition('sr_mar', 'tier_s3b', 's3://tierb/sr_mar.parquet')")
        finally:
            deny.unlink()
        expect.text(refused, "28000", "a key that can write but not read refuses the move (28000)")
        expect.text(f"{mdigest('sr')}|{_mar_shape(conn, 'sr')}", f"{before}|r:pgcolumnar:{BOUND}",
                    "and the move refused at the read-back leaves the rows local and attached, every row once")
        conn.close()
    finally:
        if cluster is not None:
            _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        for s in servers:
            s.kill()
            s.wait()
        shutil.rmtree(work, ignore_errors=True)


def test_tier_partition_path_glob_matches_errors_where_glibc_would_crash(pgc_own_db, pgc_cluster, expect):
    """The patterns that crashed the server before #1964's bounds, last in the file.

    Before the bounds each of these calls overflowed the backend's stack, and the
    crash recovery failed every check after it. Each probe gets its own session,
    and each check waits up to 60 s for the server to answer first, so a
    regression fails its own check alone."""
    own = _one(pgc_own_db, "SELECT current_database()")
    dsn = pgc_cluster.dsn(dbname=own)

    def wait_up():
        for _ in range(60):
            if _ansst(dsn, "SELECT 1") == "1":
                return
            time.sleep(1)

    def run(sql):
        wait_up()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql)

    expect.text(_ansst(dsn, "SELECT pgcolumnar.path_glob_matches(repeat('*/', 4000) || 'x', 'a/x')")
                + "|" + _ansst(dsn, "SELECT 1"),
                "54000|1", "path_glob_matches refuses a pattern of 4000 */ levels with an error, and the server stays up")
    # A role may first use most of max_stack_depth in SQL, and glob(3) then has
    # only what is left. pgm_deep calls itself n times, then the helper. The
    # deepest n that still answers is found in steps of 100, from 3000 down.
    run("CREATE FUNCTION pgm_deep(n int, p text, q text) RETURNS text LANGUAGE plpgsql AS $$"
        " BEGIN IF n = 0 THEN RETURN pgcolumnar.path_glob_matches(p, q)::text; END IF;"
        " RETURN pgm_deep(n - 1, p, q); END $$")
    deep = 3000
    while deep > 0 and _ansst(dsn, f"SELECT pgm_deep({deep}, '/w/*', '/w/a')") != "true":
        deep -= 100
    print(f"-- pgm_deep answers at {deep} levels of SQL")
    expect.text(_ansst(dsn, f"SELECT pgm_deep({deep}, '/*/' || repeat('a/', 2045) || 'x', '/b')")
                + "|" + _ansst(dsn, "SELECT 1"),
                "54000|1", "path_glob_matches refuses one * and 2045 more levels with an error near max_stack_depth, and the server stays up")
    run("DROP FUNCTION pgm_deep(int, text, text)")
    # fnmatch keeps more on the stack for each * in a UTF-8 locale than in C,
    # so this runs in a database whose LC_CTYPE is C.UTF-8.
    db = "tier_cutf8_py"
    run(f"DROP DATABASE IF EXISTS {db}")
    run(f"CREATE DATABASE {db} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as e:
            e.execute("CREATE EXTENSION pgcolumnar")
        expect.text(_ansst(pgc_cluster.dsn(dbname=db),
                           "SELECT pgcolumnar.path_glob_matches('s3://b/' || repeat('*/', 1000) || 'x',"
                           " 's3://b/' || repeat('a/', 1000) || 'x')")
                    + "|" + _ansst(dsn, "SELECT 1"),
                    "54000|1", "path_glob_matches refuses an s3:// pattern of 1000 */ levels with an error in a UTF-8 database, and the server stays up")
    finally:
        run(f"DROP DATABASE IF EXISTS {db}")

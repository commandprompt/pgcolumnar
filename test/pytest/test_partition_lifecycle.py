"""pgcolumnar.partition_create_ahead keeps a time-partitioned parent ahead of now (#1297).

The function makes columnar partitions, each `width` wide and contiguous with the
highest existing upper bound, until they reach now plus `ahead` widths. It never
takes ACCESS EXCLUSIVE on the parent: a standalone CREATE followed by ATTACH
takes SHARE UPDATE EXCLUSIVE, which readers and writers pass (#1297 note 1).

THE SESSION'S ZONE IS WHERE THE FIRST PROTOTYPE BROKE. A `timestamp` key holds
wall-clock time with no zone. The prototype read its bounds as timestamptz in the
session's zone and wrote the next ones converted from UTC: in America/New_York
that left a four-hour gap between partitions, so a row at the boundary had
nowhere to go, and "daily" partitions made from Tokyo started at 15:00. So the
arms here continue from hand-made partitions in sessions west and east of UTC,
and check contiguity, routing at every boundary, coverage of the session's own
local now, and alignment to the width in the key's wall clock.

Independent of `test/partition_lifecycle.sh`: its own widths (six hours, not a
day), leads, zones (America/Los_Angeles, and Asia/Kolkata for a half-hour
offset) and rows.
"""

import re
import time

import psycopg

LEAD = 5           # widths ahead of now
WIDTH = "6 hours"

BOUND = re.compile(r"FROM \('([^']+)'\) TO \('([^']+)'\)")


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _bounds(conn, parent):
    """[(from, to)] as the catalog prints them, in order of `from`."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i"
                    " JOIN pg_class c ON c.oid = i.inhrelid"
                    f" WHERE i.inhparent = '{parent}'::regclass")
        out = []
        for (expr,) in cur.fetchall():
            m = BOUND.search(expr)
            out.append((m.group(1), m.group(2)) if m else (expr, expr))
    return sorted(out)


def _contiguous(bounds):
    for (_, prev_to), (nxt_from, _) in zip(bounds, bounds[1:]):
        if nxt_from != prev_to:
            return f"break: {prev_to} then {nxt_from}"
    return "ok"


def _kids(conn, parent):
    return _one(conn, f"SELECT count(*) FROM pg_inherits WHERE inhparent = '{parent}'::regclass")


def _create_ahead(conn, parent, ahead=LEAD, width=WIDTH):
    return _one(conn, f"SELECT pgcolumnar.partition_create_ahead('{parent}', '{width}', {ahead})")


def _create_ahead_or_error(conn, parent, ahead=LEAD, width=WIDTH):
    """The count as text, or the SQLSTATE: a repeat call that errors must be
    reported by the arm that asked, not as an unhandled exception."""
    try:
        return str(_create_ahead(conn, parent, ahead, width))
    except psycopg.Error as e:
        return f"error {e.sqlstate}"


def test_partition_create_ahead(pgc_conn, expect):
    conn = pgc_conn
    expect.num(_one(conn, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                          " WHERE n.nspname = 'pgcolumnar' AND p.proname = 'partition_create_ahead'"), 1,
               "premise: the function exists")

    # From nothing. In UTC, six-hour widths align to 00/06/12/18, so reaching
    # now + 5 widths takes 6 partitions (the one holding now, and 5 more), or 7
    # when now + 5 widths lands exactly on a boundary -- it never does here,
    # because now() carries microseconds.
    with conn.cursor() as cur:
        cur.execute("SET TimeZone = 'UTC'")
        cur.execute("CREATE TABLE gauge (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)")
    made = _create_ahead(conn, "gauge")
    expect.num(made, LEAD + 1, "from no partitions it creates enough to reach now plus the lead")
    expect.num(_kids(conn, "gauge"), made, "the parent now has the partitions it reported")
    expect.text(_one(conn, "SELECT string_agg(DISTINCT a.amname, ',') FROM pg_inherits i"
                           " JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_am a ON a.oid = c.relam"
                           " WHERE i.inhparent = 'gauge'::regclass"),
                "pgcolumnar", "every partition it creates is columnar")
    b = _bounds(conn, "gauge")
    widths = _one(conn, "SELECT count(DISTINCT t - f) FROM (VALUES "
                        + ", ".join(f"('{f}'::timestamptz, '{t}'::timestamptz)" for f, t in b)
                        + ") v(f, t)")
    expect.text(f"{_contiguous(b)}|{widths}", "ok|1", "they are contiguous, each one width wide")
    expect.text(str(_one(conn, f"SELECT now() + {LEAD} * interval '{WIDTH}' >= '{b[-1][0]}'::timestamptz"
                               f" AND now() + {LEAD} * interval '{WIDTH}' < '{b[-1][1]}'::timestamptz")),
                "True", "and they cover now() plus the lead")

    expect.text(_create_ahead_or_error(conn, "gauge"), "0", "a second call creates nothing")
    expect.text(_create_ahead_or_error(conn, "gauge", ahead=LEAD + 3), "3",
                "asking further ahead creates only the difference")

    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO gauge SELECT now() + g * interval '17 min', g"
                    f" FROM generate_series(0, 77) g")
    expect.num(_one(conn, "SELECT count(*) FROM gauge"), 78, "rows within the covered range go in")
    expect.text(_one(conn, "SELECT string_agg(DISTINCT a.amname, ',') FROM gauge t"
                           " JOIN pg_class c ON c.oid = t.tableoid JOIN pg_am a ON a.oid = c.relam"),
                "pgcolumnar", "and land in columnar partitions")

    # The parent's lock, read inside the transaction that made the partitions.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE gauge_lock (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)")
        cur.execute("BEGIN")
        cur.execute(f"SELECT pgcolumnar.partition_create_ahead('gauge_lock', '30 minutes', 9)")
        locked_made = cur.fetchone()[0]
        cur.execute("SELECT coalesce(string_agg(DISTINCT mode, ',' ORDER BY mode), 'none') FROM pg_locks"
                    " WHERE relation = 'gauge_lock'::regclass AND pid = pg_backend_pid()")
        locks = cur.fetchone()[0]
        cur.execute("ROLLBACK")
    expect.at_least(locked_made, 1, "premise: the call inside the transaction created partitions")
    expect.text(f"held: {locks}" if "AccessExclusiveLock" in locks else "none", "none",
                "it never takes AccessExclusiveLock on the parent")

    # West of UTC: continue from a hand-made partition, for both key types.
    for key in ("timestamp", "timestamptz"):
        t = f"west_{key}"
        with conn.cursor() as cur:
            cur.execute("SET TimeZone = 'America/Los_Angeles'")
            cur.execute(f"CREATE TABLE {t} (at {key} NOT NULL, reading float8) PARTITION BY RANGE (at)")
            cur.execute(f"CREATE TABLE {t}_seed PARTITION OF {t} FOR VALUES"
                        f" FROM (date_trunc('day', now() - interval '1 day')::{key})"
                        f" TO ((date_trunc('day', now() - interval '1 day') + interval '6 hours')::{key})"
                        " USING pgcolumnar")
        west = _create_ahead(conn, t, ahead=4)
        expect.at_least(west, 5, f"premise: a {key} parent continued from a hand-made partition, in a session west of UTC")
        expect.text(_contiguous(_bounds(conn, t)), "ok",
                    f"a {key} parent's partitions are contiguous across a non-UTC session")
        # One row at every boundary, each computed HERE from the seed's lower bound and
        # the width (#1339), as the shell twin does. Taken from the function's own
        # FROM bounds, every row landed on a partition's inclusive lower edge whatever
        # the function did, so a gap between partitions could not fail it.
        n = len(_bounds(conn, t))
        routed = 0
        for g in range(n):
            try:
                with conn.cursor() as cur:
                    cur.execute(f"INSERT INTO {t} VALUES ((date_trunc('day', now() - interval '1 day')"
                                f" + {g} * interval '{WIDTH}')::{key}, 1)")
                routed += 1
            except psycopg.Error:
                pass
        expect.num(routed, n,
                   f"and a row at every boundary has exactly one partition to go to ({key})")
        with conn.cursor() as cur:
            cur.execute("SET TimeZone = 'UTC'")

    # East of UTC, at a half-hour offset: coverage of the session's own local now,
    # and alignment to the width in the key's wall clock.
    with conn.cursor() as cur:
        cur.execute("SET TimeZone = 'Asia/Kolkata'")
        cur.execute("CREATE TABLE east (at timestamp NOT NULL, reading float8) PARTITION BY RANGE (at)")
    east = _create_ahead(conn, "east", ahead=3)
    expect.at_least(east, 1, "premise: a timestamp parent was given partitions from a session east of UTC")
    eb = _bounds(conn, "east")
    local_target = _one(conn, f"SELECT (localtimestamp + 3 * interval '{WIDTH}')::text")
    expect.text(str(_one(conn, f"SELECT bool_or('{local_target}'::timestamp >= f AND '{local_target}'::timestamp < t)"
                               " FROM (VALUES " + ", ".join(f"('{f}'::timestamp, '{t}'::timestamp)" for f, t in eb)
                               + ") v(f, t)")),
                "True", "a timestamp parent made east of UTC covers the local now plus the lead")
    off = [f for f, _ in eb if not re.search(r" (00|06|12|18):00:00$", f)]
    expect.text(f"not on the width: {off[0]}" if off else "ok", "ok",
                "and its partitions start on the width's boundary in the key's own wall clock")
    with conn.cursor() as cur:
        cur.execute("SET TimeZone = 'UTC'")

    # Inputs that broke the first version (@jdatcmd, #1308 review).
    def value_or_state(sql):
        try:
            return str(_one(conn, sql))
        except psycopg.Error as e:
            return e.sqlstate or "?"

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE gen (at timestamptz NOT NULL, reading float8,"
                    " doubled float8 GENERATED ALWAYS AS (reading * 2) STORED) PARTITION BY RANGE (at)")
    gen = value_or_state("SELECT pgcolumnar.partition_create_ahead('gen', '6 hours', 2)")
    doubled = value_or_state("INSERT INTO gen (at, reading) VALUES (now(), 2.25) RETURNING doubled")
    expect.text(f"{gen}|{doubled}", "3|4.5",
                "a parent with a generated column gets its partitions, and the column stays generated")

    # 63 bytes, the longest an identifier holds, as the shell twin's name is: 31
    # two-byte characters and one ASCII (#1339; it was 62 bytes under a name that
    # said 63 characters). The date suffix must still fit, so the prefix is
    # shortened by whole characters, never by a broken byte.
    long_name = "\u00e9" * 31 + "x"
    with conn.cursor() as cur:
        cur.execute(f'CREATE TABLE "{long_name}" (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)')
    long_made = value_or_state(f"""SELECT pgcolumnar.partition_create_ahead('"{long_name}"', '6 hours', 2)""")
    kept = _one(conn, "SELECT count(*) FILTER (WHERE c.relname ~ '_[0-9]{8}T[0-9]{6}$' AND octet_length(c.relname) <= 63)"
                      f""" FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = '"{long_name}"'::regclass""")
    expect.text(f"{long_made}|{kept}", "3|3",
                "a parent with a 63-byte name gets partitions whose names keep their dates")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE swarm (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)")
    t0 = time.monotonic()
    swarm = value_or_state("SELECT pgcolumnar.partition_create_ahead('swarm', '1 minute', 5000)")
    ms = int((time.monotonic() - t0) * 1000)
    expect.text(f"{swarm}|{_kids(conn, 'swarm')}|{'prompt' if ms < 5000 else f'took {ms} ms'}", "22023|0|prompt",
                "a call that would create more than 1000 partitions is refused before any DDL")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE capped (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)")
        cur.execute("CREATE TABLE capped_rest PARTITION OF capped FOR VALUES FROM ('2025-06-01') TO (MAXVALUE)"
                    " USING pgcolumnar")
    capped = value_or_state("SELECT pgcolumnar.partition_create_ahead('capped', '1 day', 2)")
    expect.text(f"{capped}|{_kids(conn, 'capped')}", "55000|1",
                "a parent with a partition bounded by MAXVALUE is refused, and nothing is created")

    # Refused before any DDL, each by its own check (#1341). The message is in the
    # want: with the width check gone, a zero or negative width never moves the
    # counting loop on, and the 1000-partition cap answers with the same 22023.
    def refusal(sql):
        try:
            return f"ok: {_one(conn, sql)}"
        except psycopg.Error as e:
            return f"{e.sqlstate} {e.diag.message_primary}"

    def name(t):
        # As the function's messages name it: regclass formatted under its own
        # search_path (pg_catalog, pg_temp), so always schema-qualified.
        return _one(conn, "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c"
                          f" JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = '{t}'::regclass")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE wid (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)")
    bad = "22023 width must be positive and ahead not negative"
    expect.text("|".join([refusal("SELECT pgcolumnar.partition_create_ahead('wid', '0 seconds', 1)"),
                          refusal("SELECT pgcolumnar.partition_create_ahead('wid', '-6 hours', 1)"),
                          refusal("SELECT pgcolumnar.partition_create_ahead('wid', '6 hours', -1)"),
                          str(_kids(conn, "wid"))]),
                f"{bad}|{bad}|{bad}|0",
                "a width that is not positive, or a negative lead, is refused before any DDL")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE lst (at timestamptz NOT NULL, reading float8) PARTITION BY LIST (at)")
        cur.execute("CREATE TABLE two (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at, reading)")
        cur.execute("CREATE TABLE flat (at timestamptz NOT NULL, reading float8) USING pgcolumnar")
    expect.text("|".join([refusal(f"SELECT pgcolumnar.partition_create_ahead('{t}', '6 hours', 1)")
                          for t in ("lst", "two", "flat")] + [str(_kids(conn, "lst")), str(_kids(conn, "two"))]),
                "|".join(f"55000 {name(t)} is not RANGE-partitioned on one column"
                         for t in ("lst", "two", "flat")) + "|0|0",
                "a parent that is not RANGE-partitioned on one column is refused before any DDL")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE bydate (day date NOT NULL, reading float8) PARTITION BY RANGE (day)")
        cur.execute("CREATE TABLE byint (n int NOT NULL, reading float8) PARTITION BY RANGE (n)")
    expect.text("|".join([refusal("SELECT pgcolumnar.partition_create_ahead('bydate', '6 hours', 1)"),
                          refusal("SELECT pgcolumnar.partition_create_ahead('byint', '6 hours', 1)"),
                          str(_kids(conn, "bydate")), str(_kids(conn, "byint"))]),
                f"55000 {name('bydate')} is partitioned on date, not on a timestamp"
                f"|55000 {name('byint')} is partitioned on integer, not on a timestamp|0|0",
                "a parent partitioned on a key that is not a timestamp is refused before any DDL")

    # Month and year widths (#1341): date_bin cannot bin by either, so the first
    # partition starts on the first of this month and each bound adds the width.
    def calendar(t, key, step, now_expr):
        return _one(conn, f"SELECT count(*) FILTER (WHERE f = date_trunc('month', f) AND t = f + interval '{step}')"
                          f" || '|' || (min(f) = date_trunc('month', {now_expr}))"
                          f" FROM (SELECT (regexp_match(pg_get_expr(c.relpartbound, c.oid),"
                          f" 'FROM \\(''([^'']+)''\\) TO \\(''([^'']+)''\\)'))[1]::{key} AS f,"
                          f" (regexp_match(pg_get_expr(c.relpartbound, c.oid),"
                          f" 'FROM \\(''([^'']+)''\\) TO \\(''([^'']+)''\\)'))[2]::{key} AS t"
                          f" FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                          f" WHERE i.inhparent = '{t}'::regclass) b")

    for key, now_expr in (("timestamptz", "now()"), ("timestamp", "localtimestamp")):
        t = f"monthly_{key}"
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {t} (at {key} NOT NULL, reading float8) PARTITION BY RANGE (at)")
        made = value_or_state(f"SELECT pgcolumnar.partition_create_ahead('{t}', '1 month', 2)")
        expect.text(f"{made}|{calendar(t, key, '1 month', now_expr)}", "3|3|true",
                    f"a {key} parent with a month width starts at this month's first and steps by calendar months")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE yearly (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)")
    made = value_or_state("SELECT pgcolumnar.partition_create_ahead('yearly', '1 year', 1)")
    expect.text(f"{made}|{calendar('yearly', 'timestamptz', '1 year', 'now()')}", "2|2|true",
                "a year width starts at this month's first and steps by calendar years")

    # Refused: a DEFAULT partition.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE fallback (at timestamptz NOT NULL, reading float8) PARTITION BY RANGE (at)")
        cur.execute("CREATE TABLE fallback_rest PARTITION OF fallback DEFAULT USING pgcolumnar")
    try:
        _create_ahead(conn, "fallback")
        state = "ok"
    except psycopg.Error as e:
        state = e.sqlstate or "?"
    expect.text(state, "55000", "a parent with a DEFAULT partition is refused")
    expect.num(_kids(conn, "fallback"), 1, "and nothing was created for it")


def test_partition_drop_plan(pgc_conn, pgc_cluster, expect):
    """partition_drop_plan returns the statements that retire every partition wholly
    older than the cutoff: DETACH ... CONCURRENTLY then DROP, in range order, or
    DETACH ... FINALIZE for one a concurrent detach left pending. They are returned,
    not run, because DETACH ... CONCURRENTLY refuses to run inside any function
    (#1297 note 1); each is run here at top level in its own autocommit session.

    Its own fixture: weekly partitions in 2019 where the shell suite has monthly
    ones in 2020, Asia/Tokyo where it has America/New_York, and this test's private
    schema, which every statement names.
    """
    conn = pgc_conn
    schema = _one(conn, "SELECT current_schema()")
    dsn = pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()"))

    def plan(parent, cutoff):
        with conn.cursor() as cur:
            cur.execute(f"SELECT s FROM pgcolumnar.partition_drop_plan('{parent}', %s) s", (cutoff,))
            return [r[0] for r in cur.fetchall()]

    def run(statements):
        with psycopg.connect(dsn, autocommit=True) as c:
            for st in statements:
                c.execute(st)

    def q(name):
        return f"{schema}.{name}"

    def gdrop(kind, name):
        """The drop the plan gives a partition: a DO block that refuses (55000)
        while it is still attached, so a failed DETACH is never followed by a DROP
        that locks the parent (#1331 review). Its body is a string literal, not
        dollar-quoted, so no partition name can end it."""
        def lit(x):
            return "'" + x.replace("'", "''") + "'"
        body = ("BEGIN IF EXISTS (SELECT 1 FROM pg_catalog.pg_inherits"
                f" WHERE inhrelid = {lit(name)}::regclass) THEN RAISE EXCEPTION '% is still attached to its parent;"
                f" its DETACH did not finish', {lit(name)} USING ERRCODE = '55000'; END IF; DROP {kind} {name}; END")
        return "DO " + lit(body)

    def sqlstate_with(sql, setup=None):
        """'ok', or the SQLSTATE the statement failed with, in its own session."""
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"SET search_path TO {schema}")
                if setup:
                    c.execute(setup)
                c.execute(sql)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate

    def sqlstate(sql):
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"SET search_path TO {schema}")
                c.execute(sql).fetchall()
            return "no error"
        except psycopg.Error as e:
            return e.sqlstate


    weeks = ["2019-03-04", "2019-03-11", "2019-03-18", "2019-03-25", "2019-04-01", "2019-04-08"]
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xl (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts)")
        # newest first, so catalog order is not range order
        for i, (lo, hi) in reversed(list(enumerate(zip(weeks, weeks[1:])))):
            cur.execute(f"CREATE TABLE xl_w{i} PARTITION OF xl FOR VALUES FROM ('{lo} 00:00+00')"
                        f" TO ('{hi} 00:00+00') USING pgcolumnar")
        cur.execute("INSERT INTO xl SELECT '2019-03-04 00:00+00'::timestamptz + g * interval '30 min', g"
                    " FROM generate_series(0, 1679) g")
    expect.text(f"{_one(conn, 'SELECT count(DISTINCT tableoid) FROM xl')}|{_kids(conn, 'xl')}", "5|5",
                "premise: the drop fixture has several partitions, each holding rows")

    want = []
    for i in range(3):
        want += [f"ALTER TABLE {q('xl')} DETACH PARTITION {q(f'xl_w{i}')} CONCURRENTLY", gdrop("TABLE", q(f"xl_w{i}"))]
    expect.text("\n".join(plan("xl", "2019-03-28 12:00+00")), "\n".join(want),
                "the plan retires the partitions wholly before the cutoff, in range order")
    expect.num(sum(1 for s in plan("xl", "2019-03-18 00:00+00") if s.startswith("DO ")), 2,
               "a cutoff on a partition's upper bound includes it")
    expect.num(len(plan("xl", "2018-12-31 00:00+00")), 0, "a cutoff before every partition plans nothing")

    sids = [_one(conn, f"SELECT pgcolumnar.get_storage_id('xl_w{i}')") for i in range(3)]
    run(plan("xl", "2019-03-28 12:00+00"))
    left = _one(conn, "SELECT string_agg(c.relname, ',' ORDER BY c.relname) FROM pg_inherits i"
                      " JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'xl'::regclass")
    gone = _one(conn, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                      f" WHERE n.nspname = '{schema}' AND c.relname IN ('xl_w0', 'xl_w1', 'xl_w2')")
    expect.text(f"{left}|{gone}", "xl_w3,xl_w4|0", "running the plan leaves only the partitions it did not name")
    expect.num(_one(conn, "SELECT count(*) FROM xl"),
               _one(conn, "SELECT count(*) FROM generate_series(0, 1679) g"
                          " WHERE '2019-03-04 00:00+00'::timestamptz + g * interval '30 min' >= '2019-03-25 00:00+00'"),
               "and every row the remaining partitions held is still there")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = ANY (%s)" % (
                   "ARRAY[" + ",".join(str(s) for s in sids) + "]::bigint[]")), 0,
               "and the dropped partitions' pgcolumnar storage rows are gone")
    expect.text(f"{len(plan('xl', '2019-03-28 12:00+00'))}|{_kids(conn, 'xl')}", "0|2",
                "a second plan for the same cutoff is empty, and the partitions it kept are still attached")

    # The cutoff's type must be the key's: a timestamp key with a timestamp cutoff
    # plans the same in every session, and a cutoff of the other type is refused.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xw (ts timestamp NOT NULL) PARTITION BY RANGE (ts)")
        cur.execute("CREATE TABLE xw_a PARTITION OF xw FOR VALUES FROM ('2019-05-01 00:00') TO ('2019-05-01 06:00') USING pgcolumnar")
        cur.execute("CREATE TABLE xw_b PARTITION OF xw FOR VALUES FROM ('2019-05-01 06:00') TO ('2019-05-01 12:00') USING pgcolumnar")
    got = []
    for zone in ("Asia/Tokyo", "America/Denver"):
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"SET search_path TO {schema}")
            c.execute(f"SET TimeZone = '{zone}'")
            got.append(c.execute("SELECT coalesce(string_agg(s, ';'), 'none')"
                                 " FROM pgcolumnar.partition_drop_plan('xw', '2019-05-01 06:00'::timestamp) s").fetchone()[0])
    one = f"ALTER TABLE {q('xw')} DETACH PARTITION {q('xw_a')} CONCURRENTLY;" + gdrop("TABLE", q("xw_a"))
    expect.text("|".join(got), f"{one}|{one}",
                "a timestamp key's plan for a timestamp cutoff is the same from sessions west and east of UTC")
    expect.text(sqlstate("SELECT * FROM pgcolumnar.partition_drop_plan('xw', '2019-05-01 06:00+00'::timestamptz)") + "|"
                + sqlstate("SELECT * FROM pgcolumnar.partition_drop_plan('xl', '2019-04-01 00:00'::timestamp)"),
                "42804|42804", "a cutoff of the other timestamp type is refused")
    expect.text(sqlstate("SELECT * FROM pgcolumnar.partition_drop_plan('xl', NULL::timestamptz)") + "|"
                + sqlstate("SELECT * FROM pgcolumnar.partition_drop_plan('xw', NULL::timestamp)"),
                "22023|22023", "a NULL cutoff is refused, in both overloads")

    # MINVALUE below is listed once its upper bound is past; MAXVALUE above never.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xm (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)")
        cur.execute("CREATE TABLE xm_lo PARTITION OF xm FOR VALUES FROM (MINVALUE) TO ('2019-07-01 00:00+00') USING pgcolumnar")
        cur.execute("CREATE TABLE xm_hi PARTITION OF xm FOR VALUES FROM ('2019-07-01 00:00+00') TO (MAXVALUE) USING pgcolumnar")
    lo = [s for s in plan("xm", "2019-07-01 00:00+00") if s.startswith("DO ")]
    hi = [s for s in plan("xm", "infinity") if "xm_hi" in s]
    expect.text(f"{';'.join(lo)}|{len(hi)}", gdrop("TABLE", q("xm_lo")) + "|0",
                "a partition from MINVALUE is listed once its upper bound is past, one to MAXVALUE never")

    # Names that need quoting, in a schema that needs quoting.
    with conn.cursor() as cur:
        cur.execute('CREATE SCHEMA "Py Drop"')
        cur.execute('CREATE TABLE "Py Drop"."Readings" (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)')
        cur.execute('CREATE TABLE "Py Drop"."Week One" PARTITION OF "Py Drop"."Readings"'
                    " FOR VALUES FROM ('2019-08-05 00:00+00') TO ('2019-08-12 00:00+00') USING pgcolumnar")
        cur.execute('CREATE TABLE "Py Drop"."Week Two" PARTITION OF "Py Drop"."Readings"'
                    " FOR VALUES FROM ('2019-08-12 00:00+00') TO ('2019-08-19 00:00+00') USING pgcolumnar")
    try:
        run(plan('"Py Drop"."Readings"', "2019-08-12 00:00+00"))
        left = _one(conn, "SELECT string_agg(c.relname, ',' ORDER BY c.relname) FROM pg_class c"
                          " JOIN pg_namespace n ON n.oid = c.relnamespace"
                          " WHERE n.nspname = 'Py Drop' AND c.relkind IN ('r', 'p')")
    finally:
        conn.execute('DROP SCHEMA "Py Drop" CASCADE')
    expect.text(left, "Readings,Week Two", "a plan for names that need quoting runs as returned")

    # A partition that is itself partitioned is listed whole and dropped with its children.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xs (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)")
        cur.execute("CREATE TABLE xs_h1 PARTITION OF xs FOR VALUES FROM ('2019-01-01 00:00+00') TO ('2019-07-01 00:00+00')"
                    " PARTITION BY RANGE (ts)")
        for name, lo_, hi_ in (("xs_h1_a", "2019-01-01", "2019-04-01"), ("xs_h1_b", "2019-04-01", "2019-07-01")):
            cur.execute(f"CREATE TABLE {name} PARTITION OF xs_h1 FOR VALUES FROM ('{lo_} 00:00+00') TO ('{hi_} 00:00+00')"
                        " USING pgcolumnar")
        cur.execute("CREATE TABLE xs_h2 PARTITION OF xs FOR VALUES FROM ('2019-07-01 00:00+00') TO ('2020-01-01 00:00+00')"
                    " USING pgcolumnar")
        cur.execute("INSERT INTO xs SELECT '2019-01-01 00:00+00'::timestamptz + g * interval '1 day' FROM generate_series(0, 364) g")
    expect.text(";".join(plan("xs", "2019-07-01 00:00+00")),
                f"ALTER TABLE {q('xs')} DETACH PARTITION {q('xs_h1')} CONCURRENTLY;" + gdrop("TABLE", q("xs_h1")),
                "a partition that is itself partitioned is planned whole")
    run(plan("xs", "2019-07-01 00:00+00"))
    gone = _one(conn, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                      f" WHERE n.nspname = '{schema}' AND c.relname IN ('xs_h1', 'xs_h1_a', 'xs_h1_b')")
    kept = _one(conn, "SELECT count(*) FROM generate_series(0, 364) g"
                      " WHERE '2019-01-01 00:00+00'::timestamptz + g * interval '1 day' >= '2019-07-01 00:00+00'")
    expect.text(f"{gone}|{_one(conn, 'SELECT count(*) FROM xs')}", f"0|{kept}",
                "and running the plan drops it with its children")

    # A partition tier_partition moved to Parquet: DROP FOREIGN TABLE, and its file stays.
    path = _one(conn, "SELECT current_setting('data_directory')") + f"/{schema}_xt_jan.parquet"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SERVER {schema}_pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute("CREATE TABLE xt (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts)")
        cur.execute("CREATE TABLE xt_jan PARTITION OF xt FOR VALUES FROM ('2019-01-01 00:00+00') TO ('2019-02-01 00:00+00') USING pgcolumnar")
        cur.execute("CREATE TABLE xt_feb PARTITION OF xt FOR VALUES FROM ('2019-02-01 00:00+00') TO ('2019-03-01 00:00+00') USING pgcolumnar")
        cur.execute("INSERT INTO xt SELECT '2019-01-01 00:00+00'::timestamptz + g * interval '2 hours', g FROM generate_series(0, 670) g")
    try:
        moved = _one(conn, f"SELECT pgcolumnar.tier_partition('xt_jan', '{schema}_pq', '{path}')")
        kind = _one(conn, "SELECT relkind FROM pg_class WHERE oid = 'xt_jan'::regclass")
        expect.text(f"{moved}|{kind}", "372|f", "premise: the January partition was tiered to Parquet")
        tiered = plan("xt", "2019-02-01 00:00+00")
        expect.text(";".join(tiered),
                    f"ALTER TABLE {q('xt')} DETACH PARTITION {q('xt_jan')} CONCURRENTLY;" + gdrop("FOREIGN TABLE", q("xt_jan")),
                    "a tiered partition is planned with DROP FOREIGN TABLE")
        run(tiered)
        left = _one(conn, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                          f" WHERE n.nspname = '{schema}' AND c.relname = 'xt_jan'")
        size = _one(conn, f"SELECT (pg_stat_file('{path}', true)).size")
        expect.text(f"{left}|{'file-kept' if size else 'file-gone'}", "0|file-kept",
                    "and running it drops the foreign table and leaves its Parquet file")
    finally:
        conn.execute(f"DROP SERVER {schema}_pq CASCADE")

    # A pending detach: an older snapshot holds the second phase, a timeout cancels it.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xp (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)")
        cur.execute("CREATE TABLE xp_1 PARTITION OF xp FOR VALUES FROM ('2019-06-01 00:00+00') TO ('2019-06-08 00:00+00') USING pgcolumnar")
        cur.execute("CREATE TABLE xp_2 PARTITION OF xp FOR VALUES FROM ('2019-06-08 00:00+00') TO ('2019-06-15 00:00+00') USING pgcolumnar")
    with psycopg.connect(dsn) as holder:
        holder.execute(f"SET search_path TO {schema}")
        holder.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        holder.execute("SELECT count(*) FROM xp").fetchone()
        with psycopg.connect(dsn, autocommit=True) as d:
            d.execute(f"SET search_path TO {schema}")
            d.execute("SET statement_timeout = '1500ms'")
            try:
                d.execute("ALTER TABLE xp DETACH PARTITION xp_1 CONCURRENTLY")
            except psycopg.errors.QueryCanceled:
                pass
        holder.rollback()
    expect.text(str(_one(conn, "SELECT inhdetachpending FROM pg_inherits WHERE inhrelid = 'xp_1'::regclass")),
                "True", "premise: the interrupted detach left the partition pending")
    pending_plan = plan("xp", "2019-06-08 00:00+00")
    expect.text(";".join(pending_plan),
                f"ALTER TABLE {q('xp')} DETACH PARTITION {q('xp_1')} FINALIZE;" + gdrop("TABLE", q("xp_1")),
                "a pending detach is finalised, not started again")
    run(pending_plan)
    gone = _one(conn, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                      f" WHERE n.nspname = '{schema}' AND c.relname = 'xp_1'")
    expect.text(f"{gone}|{_kids(conn, 'xp')}", "0|1", "and running it drops the pending partition")

    # A DETACH that fails on the parent's lock, and then the rest of the plan once
    # the lock is gone: the DROP must refuse, and the partition stay attached.
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xf (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)")
        cur.execute("CREATE TABLE xf_1 PARTITION OF xf FOR VALUES FROM ('2019-09-01 00:00+00') TO ('2019-09-08 00:00+00') USING pgcolumnar")
        cur.execute("CREATE TABLE xf_2 PARTITION OF xf FOR VALUES FROM ('2019-09-08 00:00+00') TO ('2019-09-15 00:00+00') USING pgcolumnar")
    detach, drop = plan("xf", "2019-09-08 00:00+00")
    states = []
    with psycopg.connect(dsn) as holder:
        holder.execute(f"SET search_path TO {schema}")
        holder.execute("LOCK TABLE xf IN SHARE UPDATE EXCLUSIVE MODE")
        states.append(sqlstate_with(detach, "SET lock_timeout = '1500ms'"))
        holder.rollback()
    states.append(sqlstate_with(drop))
    attached = _one(conn, "SELECT count(*) FROM pg_inherits WHERE inhrelid = 'xf_1'::regclass")
    expect.text(f"{';'.join(states)}|{_kids(conn, 'xf')}|{attached}", "55P03;55000|2|1",
                "after a DETACH that fails, the plan's DROP refuses and the partition stays attached")

    # A partition named with the drop block's own dollar-quote tag (#1331 review).
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xq (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)")
        cur.execute('CREATE TABLE "xq$pgcolumnar_drop$a" PARTITION OF xq'
                    " FOR VALUES FROM ('2019-10-01 00:00+00') TO ('2019-10-08 00:00+00') USING pgcolumnar")
        cur.execute("CREATE TABLE xq_b PARTITION OF xq"
                    " FOR VALUES FROM ('2019-10-08 00:00+00') TO ('2019-10-15 00:00+00') USING pgcolumnar")
    named = ("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
             f" WHERE n.nspname = '{schema}' AND c.relname = 'xq$pgcolumnar_drop$a'")
    expect.num(_one(conn, named), 1, "premise: a partition is named with the drop block's dollar-quote tag")
    failed = [st for st in plan("xq", "2019-10-08 00:00+00") if sqlstate_with(st) != "ok"]
    expect.text(f"{'; '.join(failed) or 'every statement ran'}|{_one(conn, named)}|{_kids(conn, 'xq')}",
                "every statement ran|0|1",
                "a partition named with the drop block's dollar-quote tag is dropped by the plan as returned")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE xd (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)")
        cur.execute("CREATE TABLE xd_default PARTITION OF xd DEFAULT USING pgcolumnar")
        cur.execute("CREATE TABLE xn (k bigint NOT NULL) PARTITION BY RANGE (k)")
        cur.execute("CREATE TABLE xnl (k int)")
        cur.execute("CREATE TABLE xls (ts timestamptz NOT NULL) PARTITION BY LIST (ts)")
        cur.execute("CREATE TABLE xr2 (ts timestamptz NOT NULL, k int NOT NULL) PARTITION BY RANGE (ts, k)")
    expect.text(sqlstate("SELECT * FROM pgcolumnar.partition_drop_plan('xd', now())"), "55000",
                "a drop plan for a parent with a DEFAULT partition is refused")
    # Each by its own check, so the message is in the want (#1339): both answer
    # 55000, and with one check gone the other could still answer it.
    def refusal(sql):
        try:
            return f"ok: {_one(conn, sql)}"
        except psycopg.Error as e:
            return f"{e.sqlstate} {e.diag.message_primary}"

    def name(t):
        return _one(conn, "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c"
                          f" JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = '{t}'::regclass")

    expect.text(refusal("SELECT count(*) FROM pgcolumnar.partition_drop_plan('xn', now())"),
                f"55000 {name('xn')} is partitioned on bigint, not on a timestamp",
                "a drop plan for a parent not partitioned on a timestamp is refused")
    # The RANGE-on-one-column check had only an unpartitioned table to refuse, which
    # NOT FOUND refuses alone. A LIST parent and a two-column RANGE parent, both led
    # by a timestamptz, pass the key check: only strat and natts stop them.
    expect.text("|".join(refusal(f"SELECT count(*) FROM pgcolumnar.partition_drop_plan('{t}', now())")
                         for t in ("xnl", "xls", "xr2")),
                "|".join(f"55000 {name(t)} is not RANGE-partitioned on one column" for t in ("xnl", "xls", "xr2")),
                "a drop plan for a parent not RANGE-partitioned on one column is refused, LIST and two-column RANGE included")


def test_partition_create_ahead_runs_on_the_callers_own_privileges(pgc_conn, pgc_cluster, expect):
    """partition_create_ahead is not SECURITY DEFINER, so its CREATE TABLE ... PARTITION
    OF runs as the caller and core refuses a role that does not own the parent (#1341).
    Made SECURITY DEFINER, it would run as the extension's owner and let any caller add
    partitions to a table it does not own. Both roles hold USAGE and CREATE on the
    schema, so ownership is all that separates them."""
    conn = pgc_conn
    schema = _one(conn, "SELECT current_schema()")
    with conn.cursor() as cur:
        for r in ("pl_own_py", "pl_peer_py"):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {r} LOGIN NOSUPERUSER; "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            cur.execute(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO {r}')
            cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
        # The owner's control has a parent of its own (pw2), so a refusal that let the
        # peer through cannot make the control fail for the peer's reason.
        for t in ("pv", "pw2"):
            cur.execute(f"CREATE TABLE {t} (ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts)")
            cur.execute(f"ALTER TABLE {t} OWNER TO pl_own_py")

    def as_role(role, sql):
        try:
            with psycopg.connect(f"host=127.0.0.1 port={pgc_cluster.port} user={role} dbname=postgres",
                                 autocommit=True) as c:
                c.execute(f'SET search_path TO "{schema}", pgcolumnar, public')
                return str(c.execute(sql).fetchone()[0])
        except psycopg.Error as e:
            return e.sqlstate or "?"

    ahead_pv = "SELECT pgcolumnar.partition_create_ahead('pv', '1 day', 2)"
    owner = _one(conn, "SELECT string_agg(DISTINCT pg_get_userbyid(relowner), ',') FROM pg_class"
                       " WHERE oid IN ('pv'::regclass, 'pw2'::regclass)")
    supers = _one(conn, "SELECT count(*) FROM pg_roles WHERE rolname IN ('pl_own_py', 'pl_peer_py') AND rolsuper")
    expect.text(f"{owner}|{supers}", "pl_own_py|0",
                "premise: pl_own owns the parent, and neither role is a superuser")
    expect.text(as_role("pl_peer_py", "SELECT 1"), "1", "premise: pl_peer can open a session")
    expect.text(f"{as_role('pl_peer_py', ahead_pv)}|{_kids(conn, 'pv')}",
                "42501|0", "a role that does not own the parent is refused by its own privileges (42501), and creates nothing")
    ahead_pw2 = "SELECT pgcolumnar.partition_create_ahead('pw2', '1 day', 2)"
    expect.text(f"{as_role('pl_own_py', ahead_pw2)}|{_kids(conn, 'pw2')}",
                "3|3", "a non-superuser owner creates partitions ahead on its own table")

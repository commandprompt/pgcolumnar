"""A column's collation changed without a rewrite (#1327).

ALTER COLUMN ... TYPE text COLLATE <other> keeps the relfilenode, so no zone map
is rewritten. Before the fix the stored minima and maxima were the old
collation's, and the reader compared them under the new one: chunk-group pruning
and the min/max fold skipped rows the new collation orders differently.

THE ORACLE IS HEAP: the same rows, the same ALTER, the same query.

Independent of `test/collation_change.sh`: its own values, row counts and group
geometry; the check names match. A server with no en_US collation cannot show
the bug, and the test says so through `cannot_run`, which fails the run rather
than passing it.
"""

GROUP = 1000


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return None if row is None else row[0]


def _same(conn, sql, columnar, heap):
    c = _one(conn, sql.replace("%T", columnar))
    h = _one(conn, sql.replace("%T", heap))
    # An answer is required (#1339): None on both sides is not the same answer.
    return "same" if c is not None and c == h else f"columnar [{c}] heap [{h}]"


def _value(g):
    # Each value starts with one of four letters; under C the upper-case 'Q' sorts
    # before every lower-case letter, under en_US it sorts among them.
    return f"(ARRAY['c','Q','x','q'])[{g} % 4 + 1] || ({g} % 9)"


def test_a_collation_change_without_a_rewrite(pgc_conn, expect):
    conn = pgc_conn
    coll = _one(conn, "SELECT collname FROM pg_collation WHERE collname IN ('en_US.utf8', 'en_US.UTF-8', 'en_US')"
                      " AND collencoding IN (-1, pg_char_to_encoding(current_setting('server_encoding')))"
                      " ORDER BY collname LIMIT 1")
    if coll is None:
        # A reason cannot_run knows (#1339): "no en_US collation" is not one, and it
        # raised VacuityError instead of recording the test as unrunnable.
        expect.cannot_run("MISSING_DEPENDENCY", "no en_US collation",
                          name="the server has no en_US collation to change a C column to")
        return
    expect.text(_one(conn, f"SELECT ('c' < 'Q' COLLATE \"{coll}\")::text || '|' || ('c' < 'Q' COLLATE \"C\")::text"),
                "true|false", "premise: the new collation orders a lower-case letter before an upper-case one that C puts first")

    with conn.cursor() as cur:
        cur.execute('CREATE TABLE xc (k int, s text COLLATE "C") USING pgcolumnar')
        cur.execute('CREATE TABLE xh (k int, s text COLLATE "C")')
        cur.execute(f"SELECT pgcolumnar.set_options('xc', stripe_row_limit => {GROUP}, chunk_group_row_limit => {GROUP})")
        for t in ("xc", "xh"):
            cur.execute(f"INSERT INTO {t} SELECT g, {_value('g')} FROM generate_series(1, {4 * GROUP}) g")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('xc')"),
               4, "premise: the columnar table has several row groups")
    # The extent existed, and held C's bound (#1339): the arm after the change accepts
    # "none", so without this a column_extent() that never returns a row passed it.
    expect.text(_one(conn, "SELECT coalesce((SELECT minimum || '|' || maximum"
                           " FROM pgcolumnar.column_extent('xc', 's')), 'none')"),
                _one(conn, "SELECT min(s) || '|' || max(s) FROM xh"),
                "before the change the column's extent is the C collation's bound")
    before = _one(conn, "SELECT pg_relation_filenode('xc')")
    for t in ("xc", "xh"):
        conn.execute(f'ALTER TABLE {t} ALTER COLUMN s TYPE text COLLATE "{coll}"')
    expect.num(_one(conn, "SELECT pg_relation_filenode('xc')"), before, "premise: the collation change rewrote nothing")

    expect.text(_same(conn, "SELECT min(s) || '|' || max(s) FROM %T", "xc", "xh"), "same",
                "after the collation change min and max are heap's")
    expect.text(_same(conn, "SELECT count(*) || ':' || string_agg(DISTINCT s, ',' ORDER BY s) FROM %T WHERE s < 'Q'",
                      "xc", "xh"), "same", "and a range below an upper-case bound returns heap's rows")
    expect.text(_same(conn, "SELECT count(*) FROM %T WHERE s = 'c3'", "xc", "xh"), "same",
                "and an equality on a value C sorts first returns heap's rows")
    expect.text(_one(conn, "SELECT coalesce((SELECT minimum || '|' || maximum FROM pgcolumnar.column_extent('xc', 's')),"
                           " 'none') IN ('none', (SELECT min(s) || '|' || max(s) FROM xh))::text"), "true",
                "the column's extent is not the old collation's bound")

    for t in ("xc", "xh"):
        conn.execute(f"INSERT INTO {t} SELECT g, 'P' || (g % 3) FROM generate_series({4 * GROUP + 1}, {5 * GROUP}) g")
    expect.text(_same(conn, "SELECT count(*) || ':' || min(s) || '|' || max(s) FROM %T WHERE s < 'Q'", "xc", "xh"),
                "same", "rows written after the change join heap's answer")

    with conn.cursor() as cur:
        cur.execute('CREATE TABLE xp (k int, s text COLLATE "C") PARTITION BY LIST ((k % 2))')
        cur.execute("CREATE TABLE xp0 PARTITION OF xp FOR VALUES IN (0) USING pgcolumnar")
        cur.execute("CREATE TABLE xp1 PARTITION OF xp FOR VALUES IN (1) USING pgcolumnar")
        cur.execute(f"INSERT INTO xp SELECT k, s FROM xh WHERE k <= {4 * GROUP}")
        cur.execute(f'ALTER TABLE xp ALTER COLUMN s TYPE text COLLATE "{coll}"')
    expect.text(_one(conn, f"SELECT ((SELECT count(*) FROM xp WHERE s < 'Q')"
                           f" = (SELECT count(*) FROM xh WHERE k <= {4 * GROUP} AND s < 'Q'))::text"), "true",
                "a collation change made through a partitioned parent reaches the columnar partition")

    with conn.cursor() as cur:
        cur.execute('CREATE TABLE xs (k int, s text COLLATE "C") USING pgcolumnar')
        cur.execute('CREATE TABLE xsh (k int, s text COLLATE "C")')
        for t in ("xs", "xsh"):
            cur.execute(f"INSERT INTO {t} SELECT k, s FROM xh WHERE k <= {4 * GROUP}")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('xs', 's')")
        for t in ("xs", "xsh"):
            cur.execute(f'ALTER TABLE {t} ALTER COLUMN s TYPE text COLLATE "{coll}"')
    expect.text(_same(conn, "SELECT string_agg(s, ',') FROM (SELECT s FROM %T ORDER BY s, k LIMIT 9) x", "xs", "xsh"),
                "same", "a table sorted under the old collation orders ORDER BY ... LIMIT as heap does")

    with conn.cursor() as cur:
        cur.execute('CREATE TABLE xj (k int, s text COLLATE "C") USING pgcolumnar')
        cur.execute(f"INSERT INTO xj SELECT k, s FROM xh WHERE k <= {4 * GROUP}")
        cur.execute("SELECT pgcolumnar.add_projection('xj', 'xj_by_s', columns => ARRAY['k', 's'], sort_key => ARRAY['s'])")
        cur.execute(f'ALTER TABLE xj ALTER COLUMN s TYPE text COLLATE "{coll}"')
    expect.text(_one(conn, f"SELECT ((SELECT count(*) FROM xj WHERE s < 'Q')"
                           f" = (SELECT count(*) FROM xh WHERE k <= {4 * GROUP} AND s < 'Q'))::text"), "true",
                "a projection sorted on the column answers a range as heap does")


def _signed(g):
    # Every group holds both signs, so under int4 each group's maximum is small and
    # under oid every negative value is near 2**32.
    return f"CASE WHEN {g} % 3 = 0 THEN -({g} * 5) ELSE {g} * 3 END"


def test_a_type_change_without_a_rewrite(pgc_conn, expect):
    """int4 to oid is binary-coercible, so ALTER COLUMN ... TYPE oid rewrites
    nothing, and oid orders unsigned (#1341 item 19). Zone maps, the extent, a
    vacuum_sorted claim and a projection's zone maps kept int4's order. No collation
    is involved, so this runs where the test above cannot."""
    conn = pgc_conn
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE yc (k int, c int) USING pgcolumnar")
        cur.execute("CREATE TABLE yh (k int, c int)")
        cur.execute(f"SELECT pgcolumnar.set_options('yc', stripe_row_limit => {GROUP}, chunk_group_row_limit => {GROUP})")
        for t in ("yc", "yh"):
            cur.execute(f"INSERT INTO {t} SELECT g, {_signed('g')} FROM generate_series(1, {4 * GROUP}) g")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('yc')"),
               4, "premise: the int table has several row groups")
    before = _one(conn, "SELECT pg_relation_filenode('yc')")
    for t in ("yc", "yh"):
        conn.execute(f"ALTER TABLE {t} ALTER COLUMN c TYPE oid")
    expect.num(_one(conn, "SELECT pg_relation_filenode('yc')"), before,
               "premise: the change from int4 to oid rewrote nothing")

    expect.text(_same(conn, "SELECT min(c) || '|' || max(c) FROM %T", "yc", "yh"), "same",
                "after int4 to oid min and max are heap's")
    expect.text(_same(conn, "SELECT count(*) || ':' || min(c) || '|' || max(c) FROM %T WHERE c > 3000000000",
                      "yc", "yh"), "same", "and a range above every int4 value returns heap's rows")
    expect.text(_same(conn, f"SELECT count(*) FROM %T WHERE c = {2 ** 32 - 15}", "yc", "yh"), "same",
                "and an equality on a value that was negative returns heap's rows")
    expect.text(_one(conn, "SELECT coalesce((SELECT minimum || '|' || maximum FROM pgcolumnar.column_extent('yc', 'c')),"
                           " 'none') IN ('none', (SELECT min(c) || '|' || max(c) FROM yh))::text"), "true",
                "the int column's extent is not int4's bound")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE ys (k int, c int) USING pgcolumnar")
        cur.execute("CREATE TABLE ysh (k int, c int)")
        for t in ("ys", "ysh"):
            cur.execute(f"INSERT INTO {t} SELECT g, {_signed('g')} FROM generate_series(1, {4 * GROUP}) g")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('ys', 'c')")
        for t in ("ys", "ysh"):
            cur.execute(f"ALTER TABLE {t} ALTER COLUMN c TYPE oid")
    expect.text(_same(conn, "SELECT string_agg(c::text, ',') FROM (SELECT c FROM %T ORDER BY c, k LIMIT 9) x",
                      "ys", "ysh"), "same",
                "a table sorted on int4 orders ORDER BY ... LIMIT as heap does after int4 to oid")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE yj (k int, c int) USING pgcolumnar")
        cur.execute(f"INSERT INTO yj SELECT g, {_signed('g')} FROM generate_series(1, {4 * GROUP}) g")
        cur.execute("SELECT pgcolumnar.add_projection('yj', 'yj_by_c', columns => ARRAY['k', 'c'], sort_key => ARRAY['c'])")
        cur.execute("ALTER TABLE yj ALTER COLUMN c TYPE oid")
    expect.text(_same(conn, "SELECT count(*) || ':' || min(c) FROM %T WHERE c > 3000000000", "yj", "ysh"), "same",
                "a projection sorted on the int column answers a range as heap does after int4 to oid")

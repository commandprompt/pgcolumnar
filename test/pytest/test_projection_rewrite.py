"""A declared projection survives a rewrite (#876, #887). The pytest twin of
`test/projection_rewrite.sh`, every check by the shell's name (#1341).

After any rewrite three properties must hold: P1, read_projection answers and holds
exactly the base table's rows; P2, no pgcolumnar.projection row names a storage id
no live columnar relation has; P3, the declaration survives, so a rebuild stays
possible. The rewrites covered: TRUNCATE (alone, with a re-insert, over two tables,
and through a foreign-key CASCADE, also with a trigger that re-enters the utility
hook), ALTER COLUMN TYPE on a covered and an uncovered column, ADD COLUMN with a
volatile default, a type change on a partitioned parent, REFRESH MATERIALIZED VIEW,
and the extension's own vacuum, vacuum_sorted and cluster. Also asserted: the repair
adds nothing when nothing was rewritten; a stale declaration degrades to WARNING
42703 instead of aborting the user's statement; a renamed column still resolves; and
rebuild_projections rebuilds a projection whose storage is absent.

EVERY ARM ASSERTS WHAT THE OPERATION DID before its outcome means anything: an
operation that failed or rewrote nothing leaves read_projection answering, exactly as
a correct repair does. A P1 comparison hashes both sides on the server with the
shell's own pgc_set_hash SQL, and a failed side reads as its own error. P2 is global,
so the readings are taken in the shell's order by one module fixture.

ON A PRIVATE DATABASE, with serial planning, as the shell's cluster runs.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_projection_rewrite"
N = 5000
FIXTURE = "SELECT g, g%50, 'b'||g FROM generate_series(1,{n}) g"

RETIRED_ROWS = """SELECT count(*) FROM pgcolumnar.projection p
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_am am ON am.oid = c.relam
         WHERE am.amname = 'pgcolumnar' AND c.relkind IN ('r','m')
           AND pgcolumnar.get_storage_id(c.oid) = p.storage_id)"""

# A reading that could not be taken because its precondition did not hold.
UNRUN = "unrun"

# name, expected text; None means a pair of set hashes that must agree.
PREMISES = [
    ("PREMISE base holds its rows", "5000"),
    ("PREMISE projection answers before any rewrite", "5000"),
    ("PREMISE projection agrees with base before any rewrite", None),
    ("PREMISE two catalog rows under the current storage", "2"),
    ("PREMISE no retired projection rows to begin with", "0"),
    ("PREMISE the cascade fixture reads before the truncate", "5000"),
    ("PREMISE the trigger is installed on the cascaded child", "1"),
    ("PREMISE the projection reads before the truncate", "5000"),
    ("PREMISE the nested utility statement really ran", "1"),
    ("PREMISE the projection is present before the metadata-only statements", "2"),
    ("PREMISE the matview projection reads before the refresh", "5000"),
    ("PREMISE the declaration names a column the table lacks", "{gone,b}"),
    ("PREMISE the control statement completed, so its output is not empty", "1"),
    ("PREMISE the control statement rewrote fresh2, so a warning was possible", "rewrote"),
    ("PREMISE the projection reads before it is removed", "5000"),
    ("PREMISE the projection is now genuinely absent", "0"),
    ("PREMISE and the declaration survived, so a rebuild is possible", "1"),
]

REWRITES = [
    ("t_trunc_reinsert: operation REWROTE", "REWROTE"),
    ("t_trunc_reinsert P1 projection agrees with base", None),
    ("t_trunc_reinsert P2 no retired projection rows", "0"),
    ("t_trunc_reinsert P3 declaration survives", "1"),
    ("t_trunc_empty: operation REWROTE", "REWROTE"),
    ("t_trunc_empty P1 projection agrees with base", None),
    ("t_trunc_empty P2 no retired projection rows", "0"),
    ("t_trunc_empty P3 declaration survives", "1"),
    ("t_altertype: operation REWROTE", "REWROTE"),
    ("t_altertype P1 projection agrees with base", None),
    ("t_altertype P2 no retired projection rows", "0"),
    ("t_altertype P3 declaration survives", "1"),
    ("t_altertype_unc: operation REWROTE", "REWROTE"),
    ("t_altertype_unc P1 projection agrees with base", None),
    ("t_altertype_unc P2 no retired projection rows", "0"),
    ("t_altertype_unc P3 declaration survives", "1"),
    ("t_addcol_vol: operation REWROTE", "REWROTE"),
    ("t_addcol_vol P1 projection agrees with base", None),
    ("t_addcol_vol P2 no retired projection rows", "0"),
    ("t_addcol_vol P3 declaration survives", "1"),
    ("t_trunc_multi: operation REWROTE", "REWROTE"),
    ("t_trunc_multi P1 projection agrees with base", None),
    ("t_trunc_multi P2 no retired projection rows", "0"),
    ("t_trunc_multi P3 declaration survives", "1"),
    ("t_trunc_multi: the SECOND table in the statement also survives", None),
    ("cas_child: the CASCADE rewrote the child", "rewrote"),
    ("cas_child P1 the cascaded child keeps its projection", None),
    ("cas_child P2 no retired projection rows", "0"),
    ("cas_child P3 declaration survives", "1"),
    ("reent_child: the CASCADE rewrote the child", "rewrote"),
    ("reent_child P1 the projection survives a nested utility", None),
    ("reent_child P2 no retired projection rows", "0"),
    ("ptr1: parent-level ALTER rewrote the child", "rewrote"),
    ("ptr1 P1 child projection agrees with base", None),
    ("mv1: REFRESH rewrote the matview", "rewrote"),
    ("mv1 P1 the refreshed matview keeps its projection", None),
    ("mv1 P2 no projection row names a storage the matview no longer has", "0"),
    ("mv1 P3 the declaration survives the refresh", "1"),
    ("t_addcol_vol base projection covers the added column", "{1,2,3,4}"),
    ("r_vacuum: operation REWROTE", "REWROTE"),
    ("r_vacuum P1 projection agrees with base", None),
    ("r_vacuum P2 no retired projection rows", "0"),
    ("r_vacuum P3 declaration survives", "1"),
    ("r_vacsorted: operation REWROTE", "REWROTE"),
    ("r_vacsorted P1 projection agrees with base", None),
    ("r_vacsorted P2 no retired projection rows", "0"),
    ("r_vacsorted P3 declaration survives", "1"),
    ("r_cluster: operation REWROTE", "REWROTE"),
    ("r_cluster P1 projection agrees with base", None),
    ("r_cluster P2 no retired projection rows", "0"),
    ("r_cluster P3 declaration survives", "1"),
    ("renamed: the declaration followed the rename", "{a2,b}"),
    ("renamed: and the projection survives a later rewrite", None),
]

GUARDS = [
    ("noop: the metadata-only statements rewrote nothing", "unchanged"),
    ("noop: and the repair added no projection row", "2"),
    ("noop: and the projection still agrees with the base table", None),
    ("stale: the statement survives a stale declaration", "survived"),
    ("stale: the base table keeps its rows", "5000"),
    ("stale: the unrelated type change took effect", "bigint"),
    ("stale: the skip warns with SQLSTATE 42703", "1"),
    ("stale: and the warning names the projection it skipped", "1"),
    ("stale: and it names a recovery the extension actually offers", "1"),
    ("stale: an intact declaration produces NO warning", "0"),
    ("rebuild_projections reports rebuilding exactly one projection", "1"),
    ("and the rebuilt projection matches the base table", None),
    ("and it leaves no retired projection rows", "0"),
    ("a second call rebuilds nothing, so it is safe to run at any time", "0"),
]


class _Run:
    """One autocommit session, read and written in the shell's order."""

    def __init__(self, conn):
        self.conn = conn
        self.notices = []
        conn.add_notice_handler(self._notice)

    def _notice(self, d):
        # Copied now: a Diagnostic reads None once the callback returns, which made
        # every captured warning render as "None:  None: None".
        self.notices.append((d.severity, d.sqlstate, d.message_primary, d.message_detail, d.message_hint))

    def run(self, sql):
        self.conn.execute(sql)

    def q(self, sql):
        try:
            row = self.conn.execute(sql).fetchone()
            return "" if row is None or row[0] is None else str(row[0])
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)

    def set_hash(self, query):
        """The shell's pgc_set_hash: md5 of the sorted row texts, or EMPTY."""
        return self.q("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
                      f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")

    def sid(self, rel):
        return self.q(f"SELECT pgcolumnar.get_storage_id('{rel}')")

    def table(self, tag, cols="ARRAY['a','b']", key="ARRAY['a']"):
        self.run(f"CREATE TABLE {tag} (id int, a int, b text) USING pgcolumnar")
        self.run(f"INSERT INTO {tag} " + FIXTURE.format(n=N))
        self.run(f"SELECT pgcolumnar.add_projection('{tag}','pp',{cols},{key})")


def _unrun(detail):
    return (UNRUN, detail)


def _arm(r, run, tag, op):
    """The shell's arm(): fixture, operation, then what it DID and P1-P3."""
    run.table(tag)
    sid0 = run.sid(tag)
    try:
        run.run(op)
        did = "NOOP" if run.sid(tag) == sid0 else "REWROTE"
    except psycopg.Error as exc:
        did = f"FAILED: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
    r[f"{tag}: operation REWROTE"] = did
    names = (f"{tag} P1 projection agrees with base", f"{tag} P2 no retired projection rows",
             f"{tag} P3 declaration survives")
    if did != "REWROTE":
        for name in names:
            r[name] = _unrun(f"operation did not REWROTE: {did}")
        return
    r[names[0]] = (run.set_hash(f"SELECT pgcolumnar.read_projection('{tag}','pp')"),
                   run.set_hash(f"SELECT a::text||'|'||b FROM {tag}"))
    r[names[1]] = run.q(RETIRED_ROWS)
    r[names[2]] = run.q("SELECT count(*) FROM pgcolumnar.projection_declaration "
                        f"WHERE rel = '{tag}'::regclass AND name = 'pp'")


def _cascade(r, run, child, sid0, rewrote, checks):
    """A child rewritten by a statement naming something else: the rewrite, then its checks."""
    if run.sid(child) == sid0:
        r[rewrote] = _unrun("the statement did not rewrite the child")
        for name in checks:
            r[name] = _unrun("the statement did not rewrite the child")
        return False
    r[rewrote] = "rewrote"
    return True


def _render(notices, verbose):
    lines = []
    for severity, sqlstate, primary, detail, hint in notices:
        code = f"{sqlstate}: " if verbose else ""
        lines.append(f"{severity}:  {code}{primary}")
        if detail:
            lines.append(f"DETAIL:  {detail}")
        if hint:
            lines.append(f"HINT:  {hint}")
    return lines


def _readings(run):
    r = {}
    proj = "SELECT pgcolumnar.read_projection('{t}','pp')"
    base = "SELECT a::text||'|'||b FROM {t}"

    run.table("prem")
    r["PREMISE base holds its rows"] = run.q("SELECT count(*) FROM prem")
    r["PREMISE projection answers before any rewrite"] = run.q(
        "SELECT count(*) FROM pgcolumnar.read_projection('prem','pp')")
    r["PREMISE projection agrees with base before any rewrite"] = (
        run.set_hash(proj.format(t="prem")), run.set_hash(base.format(t="prem")))
    r["PREMISE two catalog rows under the current storage"] = run.q(
        "SELECT count(*) FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('prem')")
    r["PREMISE no retired projection rows to begin with"] = run.q(RETIRED_ROWS)

    _arm(r, run, "t_trunc_reinsert", "TRUNCATE t_trunc_reinsert; "
         "INSERT INTO t_trunc_reinsert SELECT g, g%7, 'z'||g FROM generate_series(1,100) g")
    _arm(r, run, "t_trunc_empty", "TRUNCATE t_trunc_empty")
    _arm(r, run, "t_altertype", "ALTER TABLE t_altertype ALTER COLUMN a TYPE bigint")
    _arm(r, run, "t_altertype_unc", "ALTER TABLE t_altertype_unc ALTER COLUMN id TYPE bigint")
    _arm(r, run, "t_addcol_vol", "ALTER TABLE t_addcol_vol ADD COLUMN zz double precision DEFAULT random()")
    run.table("t_trunc_two")
    _arm(r, run, "t_trunc_multi", "TRUNCATE t_trunc_multi, t_trunc_two")
    r["t_trunc_multi: the SECOND table in the statement also survives"] = (
        run.set_hash(proj.format(t="t_trunc_two")), run.set_hash(base.format(t="t_trunc_two")))

    # A columnar child truncated through a foreign-key CASCADE from a heap parent.
    run.run("CREATE TABLE cas_parent (id int PRIMARY KEY)")
    run.run("CREATE TABLE cas_child (id int REFERENCES cas_parent(id), v int) USING pgcolumnar")
    run.run("SELECT pgcolumnar.add_projection('cas_child','pv',ARRAY['id','v'],ARRAY['v'])")
    run.run(f"INSERT INTO cas_parent SELECT g FROM generate_series(1,{N}) g")
    run.run(f"INSERT INTO cas_child SELECT g, g%7 FROM generate_series(1,{N}) g")
    r["PREMISE the cascade fixture reads before the truncate"] = run.q(
        "SELECT count(*) FROM pgcolumnar.read_projection('cas_child','pv')")
    sid0 = run.sid("cas_child")
    run.run("TRUNCATE cas_parent CASCADE")
    cas = ("cas_child P1 the cascaded child keeps its projection",
           "cas_child P2 no retired projection rows", "cas_child P3 declaration survives")
    if _cascade(r, run, "cas_child", sid0, "cas_child: the CASCADE rewrote the child", cas):
        r[cas[0]] = (run.set_hash("SELECT pgcolumnar.read_projection('cas_child','pv')"),
                     run.set_hash("SELECT id::text||'|'||v::text FROM cas_child"))
        r[cas[1]] = run.q(RETIRED_ROWS)
        r[cas[2]] = run.q("SELECT count(*) FROM pgcolumnar.projection_declaration "
                          "WHERE rel = 'cas_child'::regclass AND name = 'pv'")

    # The same, with an AFTER TRUNCATE trigger whose utility statement re-enters the hook.
    run.run("CREATE TABLE reent_parent (id int PRIMARY KEY)")
    run.run("CREATE TABLE reent_child (id int REFERENCES reent_parent(id), v int) USING pgcolumnar")
    run.run("SELECT pgcolumnar.add_projection('reent_child','pv',ARRAY['id','v'],ARRAY['v'])")
    run.run(f"INSERT INTO reent_parent SELECT g FROM generate_series(1,{N}) g")
    run.run(f"INSERT INTO reent_child SELECT g, g%7 FROM generate_series(1,{N}) g")
    run.run("CREATE FUNCTION reent_nested_utility() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            "BEGIN\n    CREATE TABLE IF NOT EXISTS reent_marker (x int);\n    RETURN NULL;\nEND$$")
    run.run("CREATE TRIGGER reent_child_trunc AFTER TRUNCATE ON reent_child "
            "FOR EACH STATEMENT EXECUTE FUNCTION reent_nested_utility()")
    r["PREMISE the trigger is installed on the cascaded child"] = run.q(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid='reent_child'::regclass AND NOT tgisinternal")
    r["PREMISE the projection reads before the truncate"] = run.q(
        "SELECT count(*) FROM pgcolumnar.read_projection('reent_child','pv')")
    sid0 = run.sid("reent_child")
    run.run("TRUNCATE reent_parent CASCADE")
    r["PREMISE the nested utility statement really ran"] = run.q(
        "SELECT count(*) FROM pg_class WHERE relname='reent_marker'")
    reent = ("reent_child P1 the projection survives a nested utility",
             "reent_child P2 no retired projection rows")
    if _cascade(r, run, "reent_child", sid0, "reent_child: the CASCADE rewrote the child", reent):
        r[reent[0]] = (run.set_hash("SELECT pgcolumnar.read_projection('reent_child','pv')"),
                       run.set_hash("SELECT id::text||'|'||v::text FROM reent_child"))
        r[reent[1]] = run.q(RETIRED_ROWS)

    # A partition rewritten by a type change on its parent.
    run.run("CREATE TABLE ptr (id int, a int, b text) PARTITION BY RANGE (id)")
    run.run("CREATE TABLE ptr1 PARTITION OF ptr FOR VALUES FROM (1) TO (100000) USING pgcolumnar")
    run.run("INSERT INTO ptr " + FIXTURE.format(n=N))
    run.run("SELECT pgcolumnar.add_projection('ptr1','pp',ARRAY['a','b'],ARRAY['a'])")
    sid0 = run.sid("ptr1")
    run.run("ALTER TABLE ptr ALTER COLUMN a TYPE bigint")
    ptr = ("ptr1 P1 child projection agrees with base",)
    if _cascade(r, run, "ptr1", sid0, "ptr1: parent-level ALTER rewrote the child", ptr):
        r[ptr[0]] = (run.set_hash(proj.format(t="ptr1")), run.set_hash(base.format(t="ptr1")))

    # Statements that rewrite nothing: the repair must add nothing.
    run.table("noop")
    sid0 = run.sid("noop")
    noop_rows = "SELECT count(*) FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('noop')"
    r["PREMISE the projection is present before the metadata-only statements"] = run.q(noop_rows)
    run.run("ALTER TABLE noop ALTER COLUMN a SET STATISTICS 50")
    run.run("ALTER TABLE noop SET (autovacuum_enabled = false)")
    sid1 = run.sid("noop")
    r["noop: the metadata-only statements rewrote nothing"] = (
        "unchanged" if sid1 == sid0 else f"storage id {sid0} became {sid1}")
    r["noop: and the repair added no projection row"] = run.q(noop_rows)
    r["noop: and the projection still agrees with the base table"] = (
        run.set_hash(proj.format(t="noop")), run.set_hash(base.format(t="noop")))

    # REFRESH MATERIALIZED VIEW, a rewrite that is neither ALTER nor TRUNCATE.
    run.run("CREATE TABLE mv_base (id int, a int, b text)")
    run.run("INSERT INTO mv_base " + FIXTURE.format(n=N))
    run.run("CREATE MATERIALIZED VIEW mv1 USING pgcolumnar AS SELECT id, a, b FROM mv_base")
    run.run("SELECT pgcolumnar.add_projection('mv1','pp',ARRAY['a','b'],ARRAY['a'])")
    r["PREMISE the matview projection reads before the refresh"] = run.q(
        "SELECT count(*) FROM pgcolumnar.read_projection('mv1','pp')")
    sid0 = run.sid("mv1")
    run.run("UPDATE mv_base SET b = 'z'||id WHERE id <= 10")
    run.run("REFRESH MATERIALIZED VIEW mv1")
    mv = ("mv1 P1 the refreshed matview keeps its projection",)
    if _cascade(r, run, "mv1", sid0, "mv1: REFRESH rewrote the matview", mv):
        r[mv[0]] = (run.set_hash(proj.format(t="mv1")), run.set_hash(base.format(t="mv1")))
    r["mv1 P2 no projection row names a storage the matview no longer has"] = run.q(RETIRED_ROWS)
    r["mv1 P3 the declaration survives the refresh"] = run.q(
        "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'mv1'::regclass AND name = 'pp'")

    r["t_addcol_vol base projection covers the added column"] = run.q(
        "SELECT columns::text FROM pgcolumnar.projection "
        "WHERE storage_id = pgcolumnar.get_storage_id('t_addcol_vol') AND projection_id = 0")

    # The extension's own rewrites, which already re-record: they must not double-record.
    _arm(r, run, "r_vacuum", "SELECT pgcolumnar.vacuum('r_vacuum')")
    _arm(r, run, "r_vacsorted", "SELECT pgcolumnar.vacuum_sorted('r_vacsorted','a')")
    _arm(r, run, "r_cluster", "SELECT pgcolumnar.cluster('r_cluster','a')")

    # A stale declaration must degrade to a warning, not abort the statement.
    stale = ("stale: the statement survives a stale declaration", "stale: the base table keeps its rows",
             "stale: the unrelated type change took effect")
    later = ("stale: the skip warns with SQLSTATE 42703", "stale: and the warning names the projection it skipped",
             "stale: and it names a recovery the extension actually offers",
             "PREMISE the control statement completed, so its output is not empty",
             "PREMISE the control statement rewrote fresh2, so a warning was possible",
             "stale: an intact declaration produces NO warning")
    run.table("stale")
    run.run("UPDATE pgcolumnar.projection_declaration SET columns = ARRAY['gone','b'], sort_key = ARRAY['gone'] "
            "WHERE rel = 'stale'::regclass AND name = 'pp'")
    decl = run.q("SELECT columns::text FROM pgcolumnar.projection_declaration WHERE rel='stale'::regclass")
    r["PREMISE the declaration names a column the table lacks"] = decl
    if decl != "{gone,b}":
        for name in stale + later:
            r[name] = _unrun(f"declaration is {decl}, so it is not stale")
    else:
        try:
            run.run("ALTER TABLE stale ALTER COLUMN id TYPE bigint")
            r[stale[0]] = "survived"
        except psycopg.Error as exc:
            r[stale[0]] = f"the re-record aborted an unrelated ALTER TABLE: {str(exc).splitlines()[0]}"
        r[stale[1]] = run.q("SELECT count(*) FROM stale")
        r[stale[2]] = run.q("SELECT format_type(atttypid,atttypmod) FROM pg_attribute "
                            "WHERE attrelid='stale'::regclass AND attname='id'")

        run.table("stale2")
        run.run("UPDATE pgcolumnar.projection_declaration SET columns = ARRAY['gone','b'], "
                "sort_key = ARRAY['gone'] WHERE rel = 'stale2'::regclass")
        run.notices.clear()
        try:
            run.run("ALTER TABLE stale2 ALTER COLUMN id TYPE bigint")
        except psycopg.Error as exc:
            run.notices.clear()
            for name in later[:3]:
                r[name] = query_error(str(exc).splitlines()[0])
        else:
            out = _render(run.notices, verbose=True)
            r[later[0]] = str(sum(1 for ln in out if ln.startswith("WARNING:  42703: ")))
            r[later[1]] = str(sum(1 for ln in out if 'could not restore projection "pp"' in ln))
            r[later[2]] = str(sum(1 for ln in out if "add_projection" in ln))

        run.table("fresh2")
        sid0 = run.sid("fresh2")
        run.notices.clear()
        cur = run.conn.cursor()
        try:
            cur.execute("ALTER TABLE fresh2 ALTER COLUMN id TYPE bigint")
            out = _render(run.notices, verbose=False) + [cur.statusmessage or ""]
        except psycopg.Error as exc:
            out = [query_error(str(exc).splitlines()[0])]
        r[later[3]] = str(sum(1 for ln in out if ln == "ALTER TABLE"))
        if run.sid("fresh2") == sid0:
            r[later[4]] = _unrun("the control ALTER did not rewrite fresh2")
            r[later[5]] = _unrun("the control ALTER did not rewrite fresh2")
        else:
            r[later[4]] = "rewrote"
            r[later[5]] = str(sum(1 for ln in out if "warning" in ln.lower()))

    # A renamed column no longer strands the declaration (#888).
    run.table("renamed")
    run.run("ALTER TABLE renamed RENAME COLUMN a TO a2")
    r["renamed: the declaration followed the rename"] = run.q(
        "SELECT columns::text FROM pgcolumnar.projection_declaration WHERE rel='renamed'::regclass")
    run.run("ALTER TABLE renamed ALTER COLUMN id TYPE bigint")
    r["renamed: and the projection survives a later rewrite"] = (
        run.set_hash(proj.format(t="renamed")), run.set_hash("SELECT a2::text||'|'||b FROM renamed"))

    # rebuild_projections rebuilds a projection whose storage is genuinely absent.
    run.table("rec")
    r["PREMISE the projection reads before it is removed"] = run.q(
        "SELECT count(*) FROM pgcolumnar.read_projection('rec','pp')")
    run.run("DELETE FROM pgcolumnar.projection "
            "WHERE storage_id = pgcolumnar.get_storage_id('rec') AND projection_id > 0")
    r["PREMISE the projection is now genuinely absent"] = run.q(
        "SELECT count(*) FROM pgcolumnar.projection "
        "WHERE storage_id = pgcolumnar.get_storage_id('rec') AND projection_id > 0")
    r["PREMISE and the declaration survived, so a rebuild is possible"] = run.q(
        "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'rec'::regclass AND name = 'pp'")
    r["rebuild_projections reports rebuilding exactly one projection"] = run.q(
        "SELECT pgcolumnar.rebuild_projections('rec')")
    r["and the rebuilt projection matches the base table"] = (
        run.set_hash(proj.format(t="rec")), run.set_hash(base.format(t="rec")))
    r["and it leaves no retired projection rows"] = run.q(RETIRED_ROWS)
    r["a second call rebuilds nothing, so it is safe to run at any time"] = run.q(
        "SELECT pgcolumnar.rebuild_projections('rec')")
    return r


@pytest.fixture(scope="module")
def pw(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"ALTER DATABASE {DB} SET max_parallel_workers_per_gather = 0")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=DB), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            yield _readings(_Run(conn))
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _judge(pw, name, want, expect):
    got = pw[name]
    if isinstance(got, tuple) and got[0] == UNRUN:
        expect.cannot_run("UNMET_PRECONDITION", got[1], name=name)
    elif want is None:
        expect.hash(got[0], got[1], name)
    else:
        expect.text(got, want, name)


@pytest.mark.parametrize("name,want", PREMISES)
def test_a_projection_rewrite_premise_holds(pw, name, want, expect):
    _judge(pw, name, want, expect)


@pytest.mark.parametrize("name,want", REWRITES)
def test_a_rewrite_keeps_the_projection(pw, name, want, expect):
    _judge(pw, name, want, expect)


@pytest.mark.parametrize("name,want", GUARDS)
def test_the_projection_repair_guards_hold(pw, name, want, expect):
    _judge(pw, name, want, expect)

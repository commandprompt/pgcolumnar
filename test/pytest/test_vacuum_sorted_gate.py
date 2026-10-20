"""pgcolumnar.vacuum_sorted skips its rewrite only when there is nothing to do (#760). The
pytest twin of `test/vacuum_sorted_gate.sh`, every check by the shell's name (#1341).

vacuum_sorted has two jobs: it orders the live rows, and it reclaims the space of deleted
ones. So its gate may skip only when the relation is already one lexicographic run over
exactly this key, with nothing appended AND nothing deleted. The arm this suite exists for
is "deleted rows defeat the gate": an ordering-only gate, the obvious mirror of
recluster's (#415), passes every other arm and fails there, leaving dead rows stored.

HOW A REWRITE IS READ. The storage id decides: a rewrite builds new storage and swaps it
in, so the id moves on a rewrite and not on a skip. The (stripe, offset, rowcount) layout
is a second witness on the no-op arms only, because a rewrite of unchanged data restarts
the layout and can reproduce it byte for byte. A call that FAILS moves nothing either, so
both readers report a failed call, or a missing reading, as itself and never as "no-op".

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order. The heap
mirror for the last check is built from the generator, not copied from the table under
test, since a copy would agree with whatever the calls did.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_vacuum_sorted_gate"
CG, TAIL = 1000, 500
ROW = "g, (g*2654435761)::bigint % 1000, g % 97, md5(g::text)"
ORDERED_KJ = ("SELECT bool_and(ok) FROM (SELECT (k,j) >= lag((k,j)) OVER (ORDER BY ctid) IS NOT FALSE "
              "AS ok FROM {t}) s")
ORDERED_K = "SELECT bool_and(ok) FROM (SELECT k >= lag(k) OVER (ORDER BY ctid) IS NOT FALSE AS ok FROM {t}) s"
KIND = ("SELECT s.sorted_kind FROM pgcolumnar.storage s "
        "WHERE s.storage_id = pgcolumnar.get_storage_id('{t}')")
KIND_BY = ("SELECT s.sorted_kind||'/'||s.sorted_by::text FROM pgcolumnar.storage s "
           "WHERE s.storage_id = pgcolumnar.get_storage_id('{t}')")
APPENDED = "SELECT appended_groups FROM pgcolumnar.sort_status('{t}')"
ROWSET = "SELECT count(*)||'/'||sum(k)||'/'||sum(j)||'/'||md5(string_agg(pad,',' ORDER BY id, pad)) FROM {t}"


def _call(dsn, sql):
    """-> "" when `sql` ran, else its error, which no reader below mistakes for a skip."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return ""


def _q(dsn, sql):
    """-> the first column of the first row as psql -At prints it ("" for NULL, t/f for a
    boolean), or a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    if row is None or row[0] is None:
        return ""
    return ("t" if row[0] else "f") if isinstance(row[0], bool) else str(row[0])


def _num(dsn, sql):
    v = _q(dsn, sql)
    return int(v) if v.lstrip("-").isdigit() else v


class _Table:
    """The readers the shell defines, over one table."""

    def __init__(self, dsn, t):
        self.dsn, self.t, self.err = dsn, t, ""

    def mk(self):
        for sql in [f"DROP TABLE IF EXISTS {self.t}",
                    f"CREATE TABLE {self.t} (id int, k int, j int, pad text) USING pgcolumnar",
                    f"SELECT pgcolumnar.set_options('{self.t}', chunk_group_row_limit => {CG})",
                    f"INSERT INTO {self.t} SELECT {ROW} FROM generate_series(1,20000) g"]:
            _call(self.dsn, sql)

    def vsort(self, *cols):
        self.err = _call(self.dsn, f"SELECT pgcolumnar.vacuum_sorted('{self.t}', "
                                   + ", ".join(f"'{c}'" for c in cols) + ")")

    def layout(self):
        return _q(self.dsn, "SELECT md5(string_agg(stripeid::text||':'||fileoffset::text||':'||rowcount::text, "
                            f"',' ORDER BY stripeid)) FROM pgcolumnar.stats('{self.t}')")

    def storid(self):
        return _q(self.dsn, f"SELECT pgcolumnar.get_storage_id('{self.t}')")

    def dead(self):
        return _num(self.dsn, f"SELECT COALESCE(sum(deletedrows),0) FROM pgcolumnar.stats('{self.t}')")

    def live(self):
        return _num(self.dsn, f"SELECT count(*) FROM {self.t}")

    def q(self, sql):
        return _q(self.dsn, sql.format(t=self.t))

    def snap(self):
        return self.layout(), self.storid()

    def did_rewrite(self, s0):
        s1 = self.storid()
        if self.err:
            return f"call failed: {self.err}"
        if not s0 or not s1 or s0.startswith("error") or s1.startswith("error"):
            return f"no reading: storage [{s0}] -> [{s1}]"
        return "no" if s0 == s1 else "yes"

    def stayed_put(self, before):
        (p0, s0), (p1, s1) = before, self.snap()
        if self.err:
            return f"call failed: {self.err}"
        if not all(v and not v.startswith("error") for v in (p0, s0, p1, s1)):
            return f"no reading: storage [{s0}] -> [{s1}], layout [{p0}] -> [{p1}]"
        if s0 != s1:
            return "rewritten(storage moved)"
        if p0 != p1:
            return "rewritten(layout moved)"
        return "same"


@pytest.fixture(scope="module")
def vg(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _call(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}

        # fixture: sort once by (k,j)
        t = _Table(dsn, "vg_t")
        t.mk()
        s0 = t.storid()
        t.vsort("k", "j")
        r["first"] = t.did_rewrite(s0)
        r["kind"] = t.q(KIND)
        r["key"] = t.q("SELECT sort_key::text FROM pgcolumnar.sort_status('{t}')")
        r["appended"] = t.q(APPENDED)
        r["dead0"] = t.dead()

        # the gate fires
        before = t.snap()
        t.vsort("k", "j")
        r["second"] = t.stayed_put(before)
        before = t.snap()
        t.vsort("k", "j")
        r["third"] = t.stayed_put(before)
        r["still ordered"] = t.q(ORDERED_KJ)

        # deleted rows defeat the gate
        _call(dsn, "DELETE FROM vg_t WHERE id % 2 = 0")
        dead = t.dead()
        verdict = ("yes" if dead > 0 else f"no ({dead})") if isinstance(dead, int) else f"no reading ({dead})"
        r["dead inside"] = f"{verdict}/{t.q(APPENDED)}"
        r["ordering view"] = t.q(KIND_BY)
        s3, live3 = t.storid(), t.live()
        t.vsort("k", "j")
        r["dead rewrite"] = t.did_rewrite(s3)
        r["dead after"] = t.dead()
        r["live after"], r["live before"] = t.live(), live3
        before = t.snap()
        t.vsort("k", "j")
        r["after reclaim"] = t.stayed_put(before)

        # an appended tail defeats the gate
        _call(dsn, f"INSERT INTO vg_t SELECT {ROW} FROM generate_series(100001,105000) g")
        r["tail appended"] = t.q("SELECT appended_groups > 0 FROM pgcolumnar.sort_status('{t}')")
        s5 = t.storid()
        t.vsort("k", "j")
        r["append rewrite"] = t.did_rewrite(s5)
        r["folded"] = _num(dsn, "SELECT appended_groups FROM pgcolumnar.sort_status('vg_t')")

        # a different key defeats the gate
        s6 = t.storid()
        t.vsort("j", "k")
        r["other key"] = t.did_rewrite(s6)
        r["new key"] = t.q("SELECT sort_key::text FROM pgcolumnar.sort_status('{t}')")
        s7 = t.storid()
        t.vsort("k")
        r["prefix"] = t.did_rewrite(s7)

        # a Z-order run over the same columns defeats the gate
        z = _Table(dsn, "vg_z")
        z.mk()
        _call(dsn, "SELECT pgcolumnar.cluster('vg_z','k','j')")
        r["zorder"] = z.q(KIND_BY)
        r["z unsorted"] = z.q(ORDERED_K)
        sz = z.storid()
        z.vsort("k", "j")
        r["z rewrite"] = z.did_rewrite(sz)
        r["z ordered"] = z.q(ORDERED_KJ)
        r["z kind"] = z.q(KIND)

        # an unsorted vacuum() clears the mark
        u = _Table(dsn, "vg_u")
        u.mk()
        u.vsort("k", "j")
        _call(dsn, "SELECT pgcolumnar.vacuum('vg_u')")
        r["u mark"] = u.q("SELECT COALESCE(s.sorted_kind,'none') FROM pgcolumnar.storage s "
                          "WHERE s.storage_id = pgcolumnar.get_storage_id('{t}')")
        su = u.storid()
        u.vsort("k", "j")
        r["u rewrite"] = u.did_rewrite(su)

        # uncommitted work in the same transaction must not be missed
        x = _Table(dsn, "vg_x")
        x.mk()
        x.vsort("k", "j")
        # A failed transaction rolls back to the already-sorted table, which reads as
        # ordered; its error is the reading instead (found by @OffgridwithJD on #1562).
        err = _call(dsn, f"BEGIN; INSERT INTO vg_x SELECT {ROW} FROM generate_series(200001,{200000 + TAIL}) g; "
                   "SELECT pgcolumnar.vacuum_sorted('vg_x','k','j'); COMMIT;")
        r["x ordered"] = f"call failed: {err}" if err else x.q(ORDERED_KJ)
        r["x rows"] = x.live()

        d = _Table(dsn, "vg_d")
        d.mk()
        d.vsort("k", "j")
        r["d dead0"] = d.dead()
        err = _call(dsn, "BEGIN; DELETE FROM vg_d WHERE id % 2 = 0; "
                   "SELECT pgcolumnar.vacuum_sorted('vg_d','k','j'); COMMIT;")
        r["d dead"] = f"call failed: {err}" if err else d.dead()
        r["d stored"] = _num(dsn, "SELECT COALESCE(sum(rowcount),0) FROM pgcolumnar.stats('vg_d')")

        # correctness: the mirror comes from the generator, not from vg_t
        _call(dsn, "CREATE TABLE vg_h AS SELECT g AS id, ((g*2654435761)::bigint % 1000)::int AS k, "
                   "g % 97 AS j, md5(g::text) AS pad FROM generate_series(1,20000) g WHERE g % 2 = 1 "
                   f"UNION ALL SELECT {ROW} FROM generate_series(100001,105000) g")
        r["rowset"], r["mirror"] = _q(dsn, ROWSET.format(t="vg_t")), _q(dsn, ROWSET.format(t="vg_h"))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- fixture: sort once by (k,j) --------------------------------------------

def test_the_first_vacuum_sorted_does_real_work(vg, expect):
    expect.text(vg["first"], "yes", "premise: the first vacuum_sorted does real work (layout and storage both move)")


def test_it_recorded_a_lexicographic_run(vg, expect):
    expect.text(vg["kind"], "lexicographic", "premise: it recorded a LEXICOGRAPHIC run (not zorder)")


def test_over_exactly_the_requested_key(vg, expect):
    expect.text(vg["key"], "{k,j}", "premise: over exactly the requested key")


def test_the_run_covers_the_whole_relation(vg, expect):
    expect.text(vg["appended"], "0", "premise: the run covers the whole relation (0 appended groups)")


def test_nothing_is_deleted_yet(vg, expect):
    expect.num(vg["dead0"], 0, "premise: nothing is deleted yet")


# ---- the gate fires ---------------------------------------------------------

def test_a_second_call_on_the_same_key_does_not_rewrite(vg, expect):
    expect.text(vg["second"], "same", "a second vacuum_sorted on the same key does NOT rewrite")


def test_a_third_identical_call_is_also_a_no_op(vg, expect):
    expect.text(vg["third"], "same", "a third identical call is also a no-op")


def test_the_relation_is_still_in_order_after_the_skips(vg, expect):
    expect.text(vg["still ordered"], "t", "and the relation is still physically in (k,j) order after the skips")


# ---- the arm this suite exists for: deleted rows defeat the gate -------------

def test_the_delete_left_dead_rows_inside_the_run(vg, expect):
    expect.text(vg["dead inside"], "yes/0", "premise: the delete left dead rows INSIDE the run, appending nothing")


def test_an_ordering_only_gate_would_see_nothing_to_do(vg, expect):
    expect.text(vg["ordering view"], "lexicographic/{k,j}",
                "premise: so an ORDERING-only gate would see 'already sorted, nothing appended'")


def test_dead_rows_force_a_rewrite(vg, expect):
    expect.text(vg["dead rewrite"], "yes", "vacuum_sorted with dead rows DOES rewrite, gate or no gate")


def test_it_reclaimed_the_space(vg, expect):
    expect.num(vg["dead after"], 0, "and it reclaimed the space: deleted rows go to zero")


def test_without_losing_a_live_row(vg, expect):
    expect.num(vg["live after"], vg["live before"], "without losing a live row")


def test_the_call_after_the_reclaim_is_a_no_op_again(vg, expect):
    expect.text(vg["after reclaim"], "same", "the call after the reclaim is a no-op again")


# ---- an appended tail defeats the gate --------------------------------------

def test_the_insert_appended_groups_outside_the_run(vg, expect):
    expect.text(vg["tail appended"], "t", "premise: the insert appended groups outside the run")


def test_an_append_forces_a_rewrite(vg, expect):
    expect.text(vg["append rewrite"], "yes", "vacuum_sorted after an append DOES rewrite")


def test_the_tail_is_folded_back_into_the_run(vg, expect):
    expect.num(vg["folded"], 0, "and folds the tail back into the run")


# ---- a different key defeats the gate ---------------------------------------

def test_a_different_key_rewrites(vg, expect):
    expect.text(vg["other key"], "yes", "vacuum_sorted by a DIFFERENT key (same columns, other order) rewrites")


def test_the_new_key_is_recorded(vg, expect):
    expect.text(vg["new key"], "{j,k}", "and records the new key")


def test_a_prefix_of_the_key_rewrites(vg, expect):
    expect.text(vg["prefix"], "yes", "a PREFIX of the recorded key is a different key, so it rewrites")


# ---- a Z-order run over the same columns defeats the gate --------------------

def test_cluster_recorded_a_zorder_run(vg, expect):
    expect.text(vg["zorder"], "zorder/{k,j}", "premise: cluster() recorded a ZORDER run over the same columns")


def test_the_zordered_table_is_not_in_k_order(vg, expect):
    expect.text(vg["z unsorted"], "f",
                "premise: and the Z-ordered table is NOT in k order (else this arm proves nothing)")


def test_a_zorder_run_forces_a_rewrite(vg, expect):
    expect.text(vg["z rewrite"], "yes", "vacuum_sorted over a ZORDER run of the same columns DOES rewrite")


def test_the_result_is_in_kj_order(vg, expect):
    expect.text(vg["z ordered"], "t", "and the result really is in (k,j) order")


def test_the_kind_is_now_lexicographic(vg, expect):
    expect.text(vg["z kind"], "lexicographic", "and the kind is now lexicographic")


# ---- an unsorted vacuum() clears the mark ------------------------------------

def test_a_plain_vacuum_leaves_no_mark(vg, expect):
    expect.text(vg["u mark"], "none", "premise: a plain vacuum() leaves no lexicographic mark")


def test_vacuum_sorted_after_a_plain_vacuum_rewrites(vg, expect):
    expect.text(vg["u rewrite"], "yes", "so vacuum_sorted after a plain vacuum rewrites")


# ---- uncommitted work in the same transaction must not be missed --------------

def test_the_tail_is_smaller_than_the_chunk_group_limit(expect):
    expect.text("yes" if TAIL < CG else f"no: {TAIL} >= {CG}", "yes",
                "premise: the tail is smaller than the chunk group limit")


def test_an_insert_in_the_same_transaction_defeats_the_gate(vg, expect):
    expect.text(vg["x ordered"], "t",
                "an insert earlier in the same transaction defeats the gate (rows come out ordered)")


def test_every_row_is_still_there(vg, expect):
    expect.num(vg["x rows"], 20000 + TAIL, "and every row is still there")


def test_nothing_is_deleted_before_the_transaction(vg, expect):
    expect.num(vg["d dead0"], 0, "premise: nothing is deleted before the transaction")


def test_a_delete_in_the_same_transaction_defeats_the_gate(vg, expect):
    expect.num(vg["d dead"], 0, "a delete earlier in the same transaction defeats the gate (space reclaimed)")


def test_the_rewrite_really_happened(vg, expect):
    expect.num(vg["d stored"], 10000, "and the rewrite really happened: the live rows are all that is stored")


# ---- correctness: a skipped rewrite changed nothing --------------------------

def test_the_gated_calls_preserved_the_exact_row_set(vg, expect):
    expect.text(vg["rowset"], vg["mirror"],
                "the gated calls preserved the exact row set (columnar == heap mirror)")

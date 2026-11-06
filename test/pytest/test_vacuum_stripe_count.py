"""pgcolumnar.vacuum refuses a stripe_count bound rather than ignoring it (#560). The pytest
twin of `test/vacuum_stripe_count.sh`, every check by the shell's name (#1341).

`stripe_count` was documented as bounding how many row groups one call combines, but
the C function never read it: every call rewrote the whole relation. It cannot simply
be honoured, because the compaction swaps the relation to a new relfilenode, so a
partial rewrite would destroy the groups left behind. So a non-default value is
refused, with a hint naming `pgcolumnar.compact_rewrite(rel, min_deleted_fraction,
max_groups)`, which does bound its work. Asserted: the default forms still vacuum; the
refusal is pgcolumnar's own, offered as a HINT, and does no work; `vacuum_full`, which
passes the argument through, inherits it; and the named alternative rewrites exactly
its bound.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order, each call on
a connection of its own. A call reads "noerror", "connfail", or "SQLSTATE <code>:
<message>", so an unreachable server can never read as success. A refusal reads
"refused" only for pgcolumnar's own message, so any other error reads as itself. The
bounded rewrite is read as work: how many groups it rewrote, and how many still hold
deleted rows, since each rewritten group is replaced by one new one and the group count
alone cannot move.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_vacuum_stripe_count"
NGROUPS, ROWS_PER, STRIPE_LIMIT = 6, 1000, 100000
REFUSAL = "does not support a stripe_count bound"


def _err(dsn, sql, **conn_kw):
    """-> "noerror", "connfail" when the server could not be reached, or the refusal as
    "SQLSTATE <code>: <message>", which every caller compares with a fixed expectation."""
    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5, **conn_kw)
    except psycopg.OperationalError:
        return "connfail"
    with conn:
        try:
            conn.execute(sql)
        except psycopg.Error as exc:
            return f"SQLSTATE {exc.sqlstate}: {exc.diag.message_primary}"
    return "noerror"


def _diag(dsn, sql):
    """-> the failed call's diagnostics as (primary, detail, hint), or None."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc.diag.message_primary, exc.diag.message_detail, exc.diag.message_hint
    return None


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
            return None if row is None else row[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _refused(dsn, sql):
    """-> "refused" for pgcolumnar's own stripe_count refusal, "accepted", or the other
    error: any error used to read as the refusal, a missing table's included."""
    e = _err(dsn, sql)
    return "refused" if REFUSAL in e else ("accepted" if e == "noerror" else f"other error: {e}")


def _build(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        for sql in ["DROP TABLE IF EXISTS vsc", "CREATE TABLE vsc (id int, v text) USING pgcolumnar",
                    f"SELECT pgcolumnar.set_options('vsc', stripe_row_limit => {STRIPE_LIMIT}, "
                    f"chunk_group_row_limit => {ROWS_PER})"] + \
                   [f"INSERT INTO vsc SELECT g, 'v' || g FROM generate_series(1, {ROWS_PER}) g"] * NGROUPS:
            try:
                conn.execute(sql)
            except psycopg.Error as exc:
                print(f"-- step failed: {str(exc).splitlines()[0]}")


@pytest.fixture(scope="module")
def vsc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _err(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        groups = lambda: _value(dsn, "SELECT count(*) FROM pgcolumnar.stats('vsc')")
        rows = lambda: _value(dsn, "SELECT count(*) FROM vsc")
        r = {"clean call": _err(dsn, "SELECT 1"),
             "real error": _err(dsn, "SELECT 1/0"),
             "unreachable": _err(dsn, "SELECT 1", port=1)}
        _build(dsn)
        r["groups built"], r["rows built"] = groups(), rows()
        r["one-arg"] = _err(dsn, "SELECT pgcolumnar.vacuum('vsc')")
        r["one-arg groups"], r["one-arg rows"] = groups(), rows()
        _build(dsn)
        r["zero"] = _err(dsn, "SELECT pgcolumnar.vacuum('vsc', 0)")
        r["zero groups"] = groups()
        _build(dsn)
        r["refusal"] = _refused(dsn, "SELECT pgcolumnar.vacuum('vsc', 4)")
        r["diag"] = _diag(dsn, "SELECT pgcolumnar.vacuum('vsc', 4)")
        r["refused groups"], r["refused rows"] = groups(), rows()
        r["full refusal"] = _refused(dsn, "SELECT pgcolumnar.vacuum_full('public', 0.0, 4)")
        r["full refused groups"] = groups()
        r["full default"] = _err(dsn, "SELECT pgcolumnar.vacuum_full('public', 0.0)")
        r["full default groups"] = groups()
        _build(dsn)
        _err(dsn, "DELETE FROM vsc WHERE id % 2 = 0")
        rewrote = _value(dsn, "SELECT pgcolumnar.compact_rewrite('vsc', 0.1, 2)")
        r["bounded accepted"] = rewrote if isinstance(rewrote, str) else "noerror"
        still = _value(dsn, "SELECT count(*) FROM pgcolumnar.stats('vsc') WHERE deletedrows > 0")
        r["bounded work"] = f"rewrote {rewrote} of {NGROUPS} groups, {still} still hold deleted rows"
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- the error reader, and the fixture --------------------------------------

def test_err_reports_a_clean_call(vsc, expect):
    expect.text(vsc["clean call"], "noerror", "premise: err_of reports a clean call")


def test_err_reports_a_real_error(vsc, expect):
    expect.text(vsc["real error"], "SQLSTATE 22012: division by zero", "premise: err_of reports a real error")


def test_err_cannot_mistake_an_unreachable_server_for_success(vsc, expect):
    expect.text(vsc["unreachable"], "connfail", "premise: err_of cannot mistake an unreachable server for success")


def test_the_fixture_constants_are_non_zero(expect):
    expect.text("sane" if NGROUPS > 1 and ROWS_PER >= 1000 else f"NGROUPS={NGROUPS} ROWS_PER={ROWS_PER}", "sane",
                "premise: the fixture constants are non-zero, so the arms can fail")


def test_the_whole_fixture_fits_in_one_stripe(expect):
    expect.text("combinable" if NGROUPS * ROWS_PER <= STRIPE_LIMIT else f"capped at {STRIPE_LIMIT}", "combinable",
                "premise: the whole fixture fits in one stripe, so combining is possible")


def test_the_fixture_has_several_row_groups(vsc, expect):
    expect.num(vsc["groups built"], NGROUPS, "premise: the fixture really has several row groups to combine")


def test_the_fixture_has_its_rows(vsc, expect):
    expect.num(vsc["rows built"], NGROUPS * ROWS_PER, "premise: the fixture has the rows it should")


# ---- the default form must keep working -------------------------------------

def test_the_one_argument_form_still_vacuums(vsc, expect):
    expect.text(vsc["one-arg"], "noerror", "the one-argument form still vacuums")


def test_it_combined_the_groups(vsc, expect):
    expect.num(vsc["one-arg groups"], 1, "and it combined the groups, so it did the work")


def test_it_preserved_every_row(vsc, expect):
    expect.num(vsc["one-arg rows"], NGROUPS * ROWS_PER, "and it preserved every row")


def test_the_explicit_default_zero_is_still_accepted(vsc, expect):
    expect.text(vsc["zero"], "noerror", "the explicit default 0 is still accepted")


def test_zero_also_did_the_work(vsc, expect):
    expect.num(vsc["zero groups"], 1, "and 0 also did the work")


# ---- the refusal ------------------------------------------------------------

def test_a_non_default_stripe_count_is_refused(vsc, expect):
    expect.text(vsc["refusal"], "refused", "a non-default stripe_count is refused rather than ignored")


def test_the_refusal_names_compact_rewrite(vsc, expect):
    d = vsc["diag"]
    expect.num(0 if d is None else sum(1 for f in d if f and "compact_rewrite" in f), 1,
               "and the refusal names the function that does bound the work")


def test_the_name_is_offered_as_a_hint(vsc, expect):
    d = vsc["diag"]
    expect.num(0 if d is None or not d[2] else 1, 1, "and it is offered as a HINT, where psql shows it to the user")


def test_the_refused_call_did_no_work(vsc, expect):
    expect.num(vsc["refused groups"], NGROUPS, "the refused call did no work: the groups are untouched")


def test_no_rows_were_lost_to_it(vsc, expect):
    expect.num(vsc["refused rows"], NGROUPS * ROWS_PER, "and no rows were lost to it")


# ---- the second instance, inherited ------------------------------------------

def test_vacuum_full_inherits_the_refusal(vsc, expect):
    expect.text(vsc["full refusal"], "refused", "vacuum_full inherits the refusal for a non-default stripe_count")


def test_the_refused_vacuum_full_did_no_work(vsc, expect):
    expect.num(vsc["full refused groups"], NGROUPS, "the refused vacuum_full did no work either")


def test_vacuum_fulls_default_form_still_works(vsc, expect):
    expect.text(vsc["full default"], "noerror", "vacuum_full's own default form still works")


def test_vacuum_full_combined_the_groups(vsc, expect):
    expect.num(vsc["full default groups"], 1, "and it combined the groups")


# ---- the documented alternative bounds work ---------------------------------

def test_compact_rewrites_bounded_form_is_accepted(vsc, expect):
    expect.text(vsc["bounded accepted"], "noerror", "premise: compact_rewrite's bounded form is accepted")


def test_the_named_alternative_rewrote_exactly_its_bound(vsc, expect):
    expect.text(vsc["bounded work"], f"rewrote 2 of {NGROUPS} groups, {NGROUPS - 2} still hold deleted rows",
                "the named alternative rewrote exactly its bound and left the other groups alone")

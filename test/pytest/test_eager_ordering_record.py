"""Eager rewrites record the ordering they applied (#758), and sort_status reports it
(#761). The pytest twin of `test/eager_ordering_record.sh`, every check by the shell's
name (#1341).

vacuum_sorted records a lexicographic run on its key, cluster a Z-order run on its two
columns, and the two are told apart both in pgcolumnar.storage and through
sort_status. On a two-column key, as the shell insists, the Z-ordered table really is
not in k order. The recluster self-gate skips exactly an eagerly clustered table on
the same key, and rewrites on a different key, past an appended tail, and over a
lexicographic run. An unsorted rewrite claims no ordering, and vacuum_sorted with no
columns records the declared key.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). "Distinguishable" and "moved" are read only over two
readings that were both taken, so a failed reading cannot differ its way to a pass.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_eager_ordering_record"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _q(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no row from {sql}") if row is None or row[0] is None else str(row[0])


def _mk(dsn, t):
    _run(dsn, f"CREATE TABLE {t} (id int, k int, j int) USING pgcolumnar; "
              f"SELECT pgcolumnar.set_options('{t}', stripe_row_limit => 1000, chunk_group_row_limit => 500); "
              f"INSERT INTO {t} SELECT g, (g*7919)%5000, g%13 FROM generate_series(1,5000) g; "
              f"SELECT pgcolumnar.set_options('{t}', sort_by => ARRAY['k']::name[]);")


def _storage(dsn, t, col):
    return _q(dsn, f"SELECT coalesce({col}::text,'<NULL>') FROM pgcolumnar.storage "
                   f"WHERE storage_id = pgcolumnar.get_storage_id('{t}')")


def _status(dsn, t, col):
    return _q(dsn, f"SELECT coalesce({col}::text,'<NULL>') FROM pgcolumnar.sort_status('{t}')")


def _inv(dsn, t):
    return _q(dsn, f"SELECT count(*) FROM (SELECT k, lag(k) OVER () AS p FROM {t}) s WHERE p > k")


def _layout(dsn, t):
    return _q(dsn, "SELECT coalesce(md5(string_agg(group_number || ':' || file_offset, ',' ORDER BY group_number)), "
                   f"'EMPTY') FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('{t}')")


def _differs(a, b, word):
    if _failed(a) or _failed(b):
        return f"unread [{a}|{b}]"
    return word if a != b else f"same [{a}]"


@pytest.fixture(scope="module")
def eo(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _mk(dsn, "lex")
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('lex', 'k')")
        r["lex inv"] = _inv(dsn, "lex")
        r["lex tail"] = _status(dsn, "lex", "appended_groups")
        r["lex kind"], r["lex by"] = _storage(dsn, "lex", "sorted_kind"), _storage(dsn, "lex", "sorted_by")
        r["lex kind st"] = _status(dsn, "lex", "sorted_kind")

        _mk(dsn, "zo")
        _run(dsn, "SELECT pgcolumnar.cluster('zo', 'k', 'j')")
        r["zo inv"] = _inv(dsn, "zo")
        r["zo tail"] = _status(dsn, "zo", "appended_groups")
        r["zo kind"], r["zo by"] = _storage(dsn, "zo", "sorted_kind"), _storage(dsn, "zo", "sorted_by")
        r["zo kind st"] = _status(dsn, "zo", "sorted_kind")
        r["zo key"], r["lex key"] = _status(dsn, "zo", "sort_key"), _status(dsn, "lex", "sort_key")

        _mk(dsn, "gate")
        _run(dsn, "SELECT pgcolumnar.cluster('gate', 'k', 'j')")
        r["gate groups"] = _q(dsn, "SELECT total_groups FROM pgcolumnar.sort_status('gate')")
        r["gate rows"] = _q(dsn, "SELECT count(*) FROM gate")
        before = _layout(dsn, "gate")
        r["gate same"] = _q(dsn, "SELECT pgcolumnar.recluster('gate', 'k', 'j')")
        r["gate after"], r["gate before"] = _layout(dsn, "gate"), before
        r["gate other"] = _q(dsn, "SELECT pgcolumnar.recluster('gate', 'j', 'k')")
        r["gate moved"] = _differs(_layout(dsn, "gate"), before, "moved")

        _mk(dsn, "tailgate")
        _run(dsn, "SELECT pgcolumnar.cluster('tailgate', 'k', 'j')")
        _run(dsn, "INSERT INTO tailgate VALUES (90001, 42, 1)")
        before = _layout(dsn, "tailgate")
        r["tail appended"] = _q(dsn, "SELECT appended_groups FROM pgcolumnar.sort_status('tailgate')")
        r["tail recluster"] = _q(dsn, "SELECT pgcolumnar.recluster('tailgate', 'k', 'j')")
        r["tail moved"] = _differs(_layout(dsn, "tailgate"), before, "moved")

        _mk(dsn, "lexgate")
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('lexgate', 'k')")
        before = _layout(dsn, "lexgate")
        r["lexgate recluster"] = _q(dsn, "SELECT pgcolumnar.recluster('lexgate', 'k')")
        r["lexgate moved"] = _differs(_layout(dsn, "lexgate"), before, "moved")

        _mk(dsn, "un")
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('un', 'k')")
        r["un mark"] = _storage(dsn, "un", "sorted_kind")
        _run(dsn, "SELECT pgcolumnar.vacuum('un')")
        r["un kind"], r["un by"] = _storage(dsn, "un", "sorted_kind"), _storage(dsn, "un", "sorted_by")
        r["un kind st"] = _status(dsn, "un", "sorted_kind")

        _mk(dsn, "dec")
        _run(dsn, "SELECT pgcolumnar.vacuum_sorted('dec')")
        r["dec by"], r["dec kind"] = _storage(dsn, "dec", "sorted_by"), _storage(dsn, "dec", "sorted_kind")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _positive(v):
    return ("yes" if int(v) > 0 else "no") if str(v).isdigit() else v


def test_the_lexicographic_rewrite_ordered_the_rows_on_k(eo, expect):
    expect.text(eo["lex inv"], "0", "the lexicographic rewrite really ordered the rows on k")


def test_it_left_no_unsorted_tail(eo, expect):
    expect.text(eo["lex tail"], "0", "it left no unsorted tail")


def test_eager_vacuum_sorted_records_the_kind_it_applied(eo, expect):
    expect.text(eo["lex kind"], "lexicographic", "eager vacuum_sorted records the kind it applied")


def test_sort_status_reports_that_kind_to_the_owner(eo, expect):
    expect.text(eo["lex kind st"], "lexicographic", "and sort_status reports that kind to the table's owner (#761)")


def test_eager_vacuum_sorted_records_the_key_it_applied(eo, expect):
    expect.text(eo["lex by"], "{k}", "eager vacuum_sorted records the key it applied")


def test_the_z_ordered_table_is_not_ordered_on_k(eo, expect):
    expect.text(_positive(eo["zo inv"]), "yes", "the Z-ordered table is NOT ordered on k")


def test_it_too_reports_a_full_run_with_no_tail(eo, expect):
    expect.text(eo["zo tail"], "0", "it too reports a full run with no tail")


def test_eager_cluster_records_the_kind_it_applied(eo, expect):
    expect.text(eo["zo kind"], "zorder", "eager cluster records the kind it applied")


def test_sort_status_reports_that_kind_not_the_lexicographic_one(eo, expect):
    expect.text(eo["zo kind st"], "zorder", "and sort_status reports THAT kind, not the lexicographic one (#761)")


def test_eager_cluster_records_the_key_it_applied(eo, expect):
    expect.text(eo["zo by"], "{k,j}", "eager cluster records the key it applied")


def test_the_two_eager_layouts_are_distinguishable_in_the_catalog(eo, expect):
    expect.text(_differs(eo["lex kind"], eo["zo kind"], "yes"), "yes",
                "the two eager layouts are now distinguishable in the catalog")


def test_sort_status_reports_the_applied_key_on_the_z_ordered_table(eo, expect):
    expect.text(eo["zo key"], "{k,j}", "sort_status reports the APPLIED key on the Z-ordered table, not the declared {k}")


def test_sort_status_still_reports_k_on_the_sorted_table(eo, expect):
    expect.text(eo["lex key"], "{k}", "sort_status still reports {k} on the lexicographically sorted table")


def test_the_two_layouts_are_distinguishable_through_sort_status_alone(eo, expect):
    expect.text(_differs(eo["lex kind st"], eo["zo kind st"], "yes"), "yes",
                "the two layouts are distinguishable through sort_status alone (#761)")


def test_the_gate_fixture_has_more_than_one_group(eo, expect):
    g = eo["gate groups"]
    expect.text(("yes" if int(g) > 1 else "no") if str(g).isdigit() else g, "yes",
                "the gate fixture has more than one group")


def test_and_live_rows_in_them(eo, expect):
    expect.text(eo["gate rows"], "5000", "premise: and live rows in them")


def test_recluster_on_the_applied_key_does_nothing(eo, expect):
    expect.text(eo["gate same"], "0", "recluster on the key the eager cluster already applied does nothing")


def test_it_rewrote_no_group(eo, expect):
    expect.text(eo["gate after"], eo["gate before"], "and it rewrote no group: the physical layout is unchanged")


def test_recluster_on_a_different_key_still_rewrites_every_group(eo, expect):
    expect.text(eo["gate other"], "5", "control: recluster on a DIFFERENT key still rewrites every group")


def test_that_rewrite_really_moved_the_layout(eo, expect):
    expect.text(eo["gate moved"], "moved", "control: and that rewrite really moved the layout")


def test_the_insert_appended_past_the_recorded_run(eo, expect):
    expect.text(_positive(eo["tail appended"]), "yes", "the insert appended past the recorded run")


def test_recluster_with_an_appended_tail_does_not_skip(eo, expect):
    expect.text(_positive(eo["tail recluster"]), "yes", "control: recluster with an appended tail does not skip")


def test_it_moved_the_tailgate_layout(eo, expect):
    expect.text(eo["tail moved"], "moved", "control: and it moved the tailgate layout")


def test_recluster_does_not_skip_a_lexicographic_run(eo, expect):
    expect.text(eo["lexgate recluster"], "5", "control: recluster does not skip a LEXICOGRAPHIC run of the same lead column")


def test_it_moved_the_lexgate_layout(eo, expect):
    expect.text(eo["lexgate moved"], "moved", "control: and it moved the lexgate layout")


def test_the_mark_is_set_before_the_unsorted_rewrite(eo, expect):
    expect.text(eo["un mark"], "lexicographic", "the mark is set before the unsorted rewrite")


def test_an_unsorted_rewrite_claims_no_ordering(eo, expect):
    expect.text(eo["un kind"], "<NULL>", "an unsorted rewrite claims no ordering")


def test_sort_status_reports_no_kind_either(eo, expect):
    expect.text(eo["un kind st"], "<NULL>", "and sort_status reports no kind either, rather than a stale one (#761)")


def test_and_records_no_key(eo, expect):
    expect.text(eo["un by"], "<NULL>", "and records no key")


def test_vacuum_sorted_with_no_columns_records_the_declared_key(eo, expect):
    expect.text(eo["dec by"], "{k}", "vacuum_sorted with no columns records the DECLARED key it applied")


def test_and_records_it_as_a_lexicographic_run(eo, expect):
    expect.text(eo["dec kind"], "lexicographic", "and records it as a lexicographic run")

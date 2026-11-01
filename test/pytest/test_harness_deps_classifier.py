"""The classifier's own controls, in the file the gate actually runs (#432).

WHY THIS FILE IS SEPARATE FROM `test_harness_deps.py`, which defines the classifier
it tests. That file is classified cluster-bound -- correctly, because it hands real
cluster-bound file names to pytest in a subprocess -- so the `pytest (harness guards,
no database)` job, whose file list IS `NO_CLUSTER`, never ran it. The classifier that
decides the job's contents was therefore the one thing the job could not check.

@linuxhikerpm measured the consequence: disabling transitive conftest-fixture closure
left shell selftest 350 green and every CI-selected test passing, and only the
excluded targeted test failed. A load-bearing branch could break with both real gates
green.

These arms drive a corpus they write in `tmp_path` and read nothing from the real
tree, so they need no database and this file is declared in `NO_CLUSTER`. It imports
the classifier as a LIBRARY, which does not make it cluster-bound: the propagation
rule reads string constants naming corpus files, not imports.
"""

import glob
import os
import shutil

from test_harness_deps import (
    _fake_corpus,
    cluster_fixtures,
    driver_dependent,
    job_runnable,
    partition,
)

def test_the_classifier_tells_a_plain_file_from_one_that_requests_a_cluster(tmp_path, expect):
    """The base case and its control, over a conftest this test wrote: the
    derivation of the cluster fixtures runs here too, on a fixture rather than on
    the real corpus."""
    d = _fake_corpus(tmp_path, {
        "test_plain.py": "def test_nothing(expect):\n    expect.num(1, 1, 'x')\n",
        "test_direct.py": "def test_it(pgc_conn, expect):\n    pass\n",
    })
    expect.text(" ".join(sorted(cluster_fixtures(d / "conftest.py"))),
                "pgc_cluster pgc_conn",
                "both connecting fixtures are derived, including the indirect one")
    free, bound = partition(d)
    expect.text(" ".join(free) + " / " + " ".join(bound),
                "test_plain.py / test_direct.py",
                "a test requesting a cluster fixture is bound; one requesting none is free")

def test_the_classifier_follows_a_cluster_fixture_through_a_local_wrapper(tmp_path, expect):
    """THE CASE A GREP CANNOT SEE. The file names no fixture of conftest's in any
    test; a module-local fixture does, and the tests request that."""
    d = _fake_corpus(tmp_path, {
        "test_wrapped.py": (
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def loaded(pgc_cluster):\n"
            "    yield pgc_cluster\n\n"
            "def test_it(loaded, expect):\n"
            "    pass\n"
        ),
    })
    free, bound = partition(d)
    expect.text(" ".join(free) + "/" + " ".join(bound), "/test_wrapped.py",
                "a cluster fixture reached through a local fixture is followed")

def test_the_classifier_catches_a_module_scope_driver_import(tmp_path, expect):
    """An import at module scope kills COLLECTION, so the file cannot run in the
    job even though no test of its own asks for a connection."""
    d = _fake_corpus(tmp_path, {
        "test_eager.py": "import psycopg\n\ndef test_it(expect):\n    pass\n",
    })
    free, bound = partition(d)
    expect.text(" ".join(free) + "/" + " ".join(bound), "/test_eager.py",
                "a module-scope driver import makes the file cluster-bound")

def test_the_classifier_is_not_fooled_by_prose_that_names_the_driver(tmp_path, expect):
    """THE REGEX TRAP, and this layer has paid for it once already: the
    broad-except refusal was first written as a line regex and rejected its own
    tests, because the forbidden shape appears inside a `makepyfile` string.

    A docstring naming the driver, a block-comment string naming a cluster
    fixture, and a test BUILT as a string are all prose about code."""
    d = _fake_corpus(tmp_path, {
        "test_needsdb.py": "def test_it(pgc_conn, expect):\n    pass\n",
        "test_prose.py": (
            '"""This file explains `import psycopg` and the pgc_conn fixture,\n'
            'and it discusses test_needsdb.py, which does need a cluster."""\n'
            "\n"
            "def test_it(pytester, expect):\n"
            '    """It uses pgc_conn nowhere; it writes a test that would."""\n'
            '    "a block comment mentioning import psycopg and pgc_cluster"\n'
            "    pytester.makepyfile(\n"
            '        "import psycopg\\n"\n'
            '        "def test_inner(pgc_conn):\\n    pass\\n"\n'
            "    )\n"
        ),
    })
    free, bound = partition(d)
    expect.text(" ".join(free) + " / " + " ".join(bound),
                "test_prose.py / test_needsdb.py",
                "prose, a generated test, and a file merely DISCUSSED are not requirements")

def test_the_classifier_takes_a_fixture_that_provisions_without_connecting(tmp_path, expect):
    """THE SECOND SIGNAL, isolated. A fixture can start a cluster and hand back the
    object without ever connecting, so it imports no driver: the only thing that
    marks it is the call into the module that provisions a server.

    Its own arm rather than a clause in the one below, because a fixture that
    neither imports nor calls would leave two rules untested at once."""
    d = _fake_corpus(tmp_path, {
        "test_raw.py": "def test_it(pgc_raw, expect):\n    pass\n",
    }, conftest=(
        "import pytest\n"
        "from pgc_cluster import make_cluster\n\n"
        "@pytest.fixture(scope='session')\n"
        "def pgc_raw():\n"
        "    yield make_cluster()\n"
    ))
    expect.text(" ".join(sorted(cluster_fixtures(d / "conftest.py"))), "pgc_raw",
                "a fixture that provisions a cluster is a root without importing a driver")
    free, bound = partition(d)
    expect.text(" ".join(free) + " / " + " ".join(bound), " / test_raw.py",
                "and the file requesting it is cluster-bound")

def test_the_classifier_follows_a_conftest_fixture_that_connects_indirectly(tmp_path, expect):
    """THE CASE THE CLOSURE OVER CONFTEST EXISTS FOR. A conftest fixture can reach
    a cluster through a SIBLING and import nothing of its own, so a test requesting
    only that fixture names no root and no driver anywhere.

    Without the closure the file reads as database-free and the gate would run it
    against a database that is not there."""
    d = _fake_corpus(tmp_path, {
        "test_indirect.py": "def test_it(pgc_readonly, expect):\n    pass\n",
    }, conftest=(
        "import pytest\n"
        "from pgc_cluster import make_cluster\n\n"
        "@pytest.fixture(scope='session')\n"
        "def pgc_cluster():\n"
        "    import psycopg\n"
        "    yield make_cluster()\n\n"
        "@pytest.fixture\n"
        "def pgc_readonly(pgc_cluster):\n"
        "    yield pgc_cluster\n"
    ))
    expect.text(" ".join(sorted(cluster_fixtures(d / "conftest.py"))),
                "pgc_cluster pgc_readonly",
                "a fixture reaching a cluster only through a sibling is a root too")
    free, bound = partition(d)
    expect.text(" ".join(free) + " / " + " ".join(bound), " / test_indirect.py",
                "and the file requesting it is cluster-bound")

def test_the_classifier_does_not_read_a_helpers_parameter_as_a_fixture(tmp_path, expect):
    """pytest resolves parameter names for tests and fixtures, not for helpers, so
    a helper taking `pgc_conn` pulls in nothing."""
    d = _fake_corpus(tmp_path, {
        "test_helper.py": (
            "def _plan(pgc_conn, sql):\n    return sql\n\n"
            "def test_it(expect):\n    expect.text(_plan(None, 'x'), 'x', 'n')\n"
        ),
    })
    free, bound = partition(d)
    expect.text(" ".join(free) + "/" + " ".join(bound), "test_helper.py/",
                "a helper's parameter name is just a parameter name")

def test_the_classifier_follows_a_file_that_drives_a_cluster_bound_file(tmp_path, expect):
    """This file's own shape. It requests no fixture, and it hands a cluster-bound
    file to pytest as a subprocess, so it needs whatever that file needs."""
    d = _fake_corpus(tmp_path, {
        "test_needy.py": "def test_it(pgc_conn, expect):\n    pass\n",
        "test_driver.py": (
            "import subprocess, sys\n\n"
            "def test_it(expect):\n"
            "    subprocess.run([sys.executable, '-m', 'pytest', 'test_needy.py'])\n"
        ),
    })
    free, bound = partition(d)
    expect.text(" ".join(free) + "/" + " ".join(bound), "/test_driver.py test_needy.py",
                "driving a cluster-bound file is inherited")


# ---------------------------------------------------------------------------
# ORDINARY PYTEST DEPENDENCY FORMS
#
# The classifier read module-level `def`s and positional parameters, and pytest
# resolves a fixture through five more shapes than that. @linuxhikerpm built a
# direct fixture in each and found them classified database-free and then failing
# under the no-driver shim. Measured against the classifier as it was:
#
#     a test method inside a class          free   <- wrong
#     @pytest.mark.usefixtures              free   <- wrong
#     a keyword-only fixture parameter      free   <- wrong
#     request.getfixturevalue               free   <- wrong
#     an ALIASED cluster root in conftest   free   <- wrong
#     module-level positional               bound     (the one shape it did see)
#
# The fifth needed a shape the review did not give: an alias on the TEST side is
# caught anyway, because the underlying fixture still takes the root positionally.
# It is an alias on the ROOT, in conftest, that hides it -- the root was recorded
# under the def's name while a test requests it under the alias.
#
# One arm per form, and each is a DIRECT dependency, so nothing here relies on the
# closure arms above.


def _one_file(tmp_path, body, conftest=None):
    return _fake_corpus(tmp_path, {"test_case.py": body}, conftest=conftest)


def _verdict(tmp_path, body, conftest=None):
    free, _bound = partition(_one_file(tmp_path, body, conftest))
    return "free" if "test_case.py" in free else "bound"


def test_a_test_method_inside_a_class_is_a_fixture_request(tmp_path, expect):
    """pytest collects `test_*` methods of a class and resolves their fixtures the
    same way. A walk over `tree.body` alone sees no def at all."""
    expect.text(_verdict(tmp_path, "class TestThing:\n"
                                   "    def test_a(self, pgc_conn):\n"
                                   "        assert pgc_conn\n"),
                "bound", "a class method requesting a cluster fixture is cluster-bound")


def test_usefixtures_is_a_fixture_request_without_a_parameter(tmp_path, expect):
    """The form a test uses when it wants the fixture's effect rather than its
    value -- which is exactly when the thing it wants is a cluster."""
    expect.text(_verdict(tmp_path, "import pytest\n"
                                   "@pytest.mark.usefixtures('pgc_conn')\n"
                                   "def test_a():\n"
                                   "    assert True\n"),
                "bound", "@pytest.mark.usefixtures names a dependency")


def test_a_keyword_only_parameter_is_a_fixture_request(tmp_path, expect):
    """pytest resolves a keyword-only parameter exactly as a positional one."""
    expect.text(_verdict(tmp_path, "def test_a(*, pgc_conn):\n    assert pgc_conn\n"),
                "bound", "a keyword-only parameter is resolved as a fixture")


def test_a_dynamic_request_is_treated_as_cluster_bound(tmp_path, expect):
    """`request.getfixturevalue(name)` computes the name at run time, so no AST can
    resolve it. The honest answer is not to ban the form but to stop claiming the
    file needs no database: wrong in the direction that costs CI time, not in the
    direction that greens a gate over tests nothing ran."""
    expect.text(_verdict(tmp_path, "def test_a(request):\n"
                                   "    c = request.getfixturevalue('pgc_conn')\n"
                                   "    assert c\n"),
                "bound", "an unresolvable request is cluster-bound, not free")
    # THE CASE THE DOCSTRING DESCRIBES (#1339). The literal above names a cluster root,
    # so a classifier that resolved literals, or one that skipped every non-literal,
    # passed it too. Here the name is computed, which no AST can resolve.
    expect.text(_verdict(tmp_path, "def test_a(request):\n"
                                   "    name = 'pgc_' + 'conn'\n"
                                   "    c = request.getfixturevalue(name)\n"
                                   "    assert c\n"),
                "bound", "a request whose name is computed at run time is cluster-bound")


def test_an_aliased_cluster_root_is_found_under_the_name_tests_request(tmp_path, expect):
    """`@pytest.fixture(name="conn")` makes the function requestable as `conn` and
    NOT under its own name. Recording the def's name did two wrong things at once:
    it missed the dependency a test declares, and it invented a root nothing can
    request."""
    aliased = (
        "import pytest\n"
        "from pgc_cluster import make_cluster\n\n"
        "@pytest.fixture(name='conn')\n"
        "def _mk():\n"
        "    cluster = make_cluster()\n"
        "    import psycopg\n"
        "    yield psycopg.connect('')\n"
    )
    d = _one_file(tmp_path, "def test_a(conn):\n    assert conn\n", conftest=aliased)
    expect.row_set([(n,) for n in sorted(cluster_fixtures(d / "conftest.py"))],
                   [("conn",)],
                   "the root is read under the name a test requests, not the def's")
    free, bound = partition(d)
    expect.text(" ".join(free) + "/" + " ".join(bound), "/test_case.py",
                "so a test requesting the alias is cluster-bound")


def test_a_plain_test_and_a_helpers_parameter_stay_database_free(tmp_path, expect):
    """The cost side, and it is the half a widened classifier gets wrong: a rule
    that calls everything cluster-bound would pass every arm above and empty the
    gate. `self` and `cls` are dropped for the same reason -- they are bound by
    Python, and no fixture can be requested under either name."""
    expect.text(_verdict(tmp_path, "def test_a():\n    assert True\n"),
                "free", "a test requesting nothing needs no database")
    expect.text(_verdict(tmp_path, "def helper(pgc_conn):\n    return pgc_conn\n"
                                   "def test_a():\n    assert True\n"),
                "free", "a helper's parameter is not a fixture request")
    expect.text(_verdict(tmp_path, "class TestThing:\n"
                                   "    def test_a(self):\n"
                                   "        assert True\n"),
                "free", "and `self` is not a fixture request")

# ---------------------------------------------------------------------------
# `usefixtures` ON A CLASS AND AT MODULE LEVEL
#
# The first version read `usefixtures` on a FUNCTION only. @jdatcmd found the other two
# places pytest reads it, and the class one is pointed: class-method descent exists to
# serve exactly that shape, so the two belonged in one change and only one was there.
# Measured against that version, four files through `partition()`:
#
#     @pytest.mark.usefixtures on a def      bound   (the only form it saw)
#     @pytest.mark.usefixtures on a class    free    <- wrong
#     pytestmark = pytest.mark.usefixtures   free    <- wrong
#     pytestmark = [ ... ]                   free    <- wrong
#
# pytest applies a class decorator to every method and a module-level `pytestmark` to
# every test, so each is a dependency of defs whose own decorator list and signature say
# nothing about it.


def test_usefixtures_on_a_class_reaches_its_methods(tmp_path, expect):
    """The form class-method descent exists to serve."""
    expect.text(_verdict(tmp_path, "import pytest\n"
                                   "@pytest.mark.usefixtures('pgc_conn')\n"
                                   "class TestThing:\n"
                                   "    def test_a(self):\n"
                                   "        assert True\n"),
                "bound", "a class-level usefixtures binds its methods")


def test_a_module_level_pytestmark_reaches_every_test(tmp_path, expect):
    """`pytestmark` is how a file says "all of these need it" with no decorator in sight."""
    expect.text(_verdict(tmp_path, "import pytest\n"
                                   "pytestmark = pytest.mark.usefixtures('pgc_conn')\n"
                                   "def test_a():\n"
                                   "    assert True\n"),
                "bound", "a module-level pytestmark binds every test in the file")


def test_a_pytestmark_written_as_a_list_reaches_every_test_too(tmp_path, expect):
    """The list form is the common one once a file has two marks, and reading only the
    bare form would have covered the arm above while missing the shape people write."""
    expect.text(_verdict(tmp_path, "import pytest\n"
                                   "pytestmark = [pytest.mark.usefixtures('pgc_conn')]\n"
                                   "def test_a():\n"
                                   "    assert True\n"),
                "bound", "a pytestmark list binds every test in the file")


def test_an_unrelated_class_decorator_does_not_bind_anything(tmp_path, expect):
    """The cost side, and the one a widened reader gets wrong: only `usefixtures` is a
    dependency. A rule that treated any class decorator as one would call every
    parametrised class cluster-bound and empty the gate."""
    expect.text(_verdict(tmp_path, "import pytest\n"
                                   "@pytest.mark.slow\n"
                                   "class TestThing:\n"
                                   "    def test_a(self):\n"
                                   "        assert True\n"),
                "free", "an unrelated mark on a class is not a fixture request")
    expect.text(_verdict(tmp_path, "import pytest\n"
                                   "pytestmark = pytest.mark.slow\n"
                                   "def test_a():\n"
                                   "    assert True\n"),
                "free", "nor is an unrelated module-level pytestmark")

# ---------------------------------------------------------------------------
# NEEDING A CLUSTER AND NEEDING THE DRIVER ARE TWO PROPERTIES
#
# The job this declaration feeds installs no driver as well as running no cluster, so its
# file list is the INTERSECTION. `test_raises_sqlstate.py` is the case that showed the
# difference: it requests no cluster fixture -- correctly cluster-free -- and four of its
# arms fail with psycopg shimmed out, because the modules it hands to `pytester` import it.
#
# Folding that into `partition()` was my first attempt and it contradicted
# `test_the_classifier_is_not_fooled_by_prose_that_names_the_driver`, which asserts that a
# generated inner test requesting a cluster fixture is the INNER run's requirement and not
# this file's. That arm is right. Two properties, derived separately, intersected for the
# job.


def test_a_file_whose_generated_tests_import_the_driver_is_driver_dependent(tmp_path, expect):
    """Cluster-free and still unable to run where there is no driver."""
    d = _fake_corpus(tmp_path, {
        "test_inner_driver.py": (
            "def test_it(pytester, expect):\n"
            "    pytester.makepyfile(\n"
            '        "import psycopg\\n"\n'
            '        "def test_inner():\\n    pass\\n"\n'
            "    )\n"
        ),
    })
    free, _bound = partition(d)
    expect.text(" ".join(free), "test_inner_driver.py",
                "it requests no cluster, so the cluster property calls it free")
    expect.row_set([(n,) for n in driver_dependent(d)], [("test_inner_driver.py",)],
                   "and the driver property calls it dependent")
    expect.num(len(job_runnable(d)), 0,
               "so the job's list, which is the intersection, excludes it")


def test_a_file_that_only_PARSES_a_driver_import_is_job_runnable(tmp_path, expect):
    """The control, and the reason the rule needs two conditions rather than one.

    `test_harness_deps_classifier.py` writes fixture corpora containing `import psycopg`
    and never runs them -- nothing imports those files, they are parsed. Reading only the
    string would throw this very file out of the gate it exists to be in.
    """
    d = _fake_corpus(tmp_path, {
        "test_parses_only.py": (
            "def test_it(tmp_path, expect):\n"
            '    (tmp_path / "fixture.py").write_text("import psycopg\\n")\n'
            '    expect.num(1, 1, "parsed, never run")\n'
        ),
    })
    expect.num(len(driver_dependent(d)), 0,
               "a driver import in a string nothing runs is not a dependency")
    expect.text(" ".join(job_runnable(d)), "test_parses_only.py",
                "so the job can run it")


def test_prose_naming_the_driver_is_not_a_driver_dependency(tmp_path, expect):
    """The same exclusion the cluster property makes, for the same reason: a docstring
    naming psycopg is a sentence about code."""
    d = _fake_corpus(tmp_path, {
        "test_prose_only.py": (
            '"""This file explains `import psycopg` and says nothing else."""\n'
            "def test_it(pytester, expect):\n"
            '    pytester.makepyfile("def test_inner():\\n    pass\\n")\n'
        ),
    })
    expect.num(len(driver_dependent(d)), 0,
               "prose naming the driver is not a dependency on it")


# ---- the default pg_config (#1933) -------------------------------------------
#
# conftest's default for `--pg-config` was a fixed path, /usr/local/pg18a, which one
# audit box has and the dev container does not. With PGC_PG_CONFIG unset, every
# cluster fixture then failed at setup on the missing file, and the driver control in
# test_harness_deps.py reported a missing shim name instead. These arms live here, in
# the database-free half, because they read files and run nothing.


def _executable_pg_configs():
    """Every pg_config this box has, read without conftest: the documented /usr/local
    prefixes and the one on PATH."""
    found = sorted(glob.glob("/usr/local/*/bin/pg_config"))
    on_path = shutil.which("pg_config")
    if on_path:
        found.append(on_path)
    return [p for p in found if os.access(p, os.X_OK)]


def test_the_default_pg_config_is_one_this_box_has(expect):
    """With PGC_PG_CONFIG unset, the default must name a pg_config that exists,
    whenever this box has one at all (#1933). On a box with none, the default
    cannot exist either, and the arm says so rather than skipping."""
    import conftest
    default = conftest.DEFAULT_PG_CONFIG
    present = _executable_pg_configs()
    expect.text(repr(os.access(default, os.X_OK)), repr(bool(present)),
                f"the default pg_config {default!r} is executable whenever this box "
                f"has one; it has {len(present)}: {present}")


def test_the_default_pg_config_takes_the_first_candidate_that_exists(tmp_path, expect):
    """The resolver's order, over files this test writes, so it holds on any box
    (#1933). The arm above only sees the branch this box takes."""
    import conftest

    def exe(name, mode=0o755):
        p = tmp_path / name
        p.write_text("#!/bin/sh\n")
        p.chmod(mode)
        return str(p)

    missing = str(tmp_path / "missing")
    first, second = exe("first"), exe("second")
    plain = exe("plain", 0o644)
    on_path = exe("on_path")
    pick = conftest.default_pg_config
    expect.text(pick((missing, first, second), lambda _: on_path), first,
                "a missing candidate is passed over for the first one that exists")
    expect.text(pick((plain, second), lambda _: on_path), second,
                "a candidate that is not executable is passed over too")
    expect.text(pick((missing,), lambda _: on_path), on_path,
                "with no candidate present, the pg_config on PATH is the default")
    expect.text(pick((missing, plain), lambda _: None), missing,
                "with nothing at all, the first candidate stays named")

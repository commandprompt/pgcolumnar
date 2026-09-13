"""The harness must be able to test ITSELF without a database.

WHY THIS EXISTS. `conftest.py` imported psycopg at module scope, and conftest is
imported before every run, so a DATABASE DRIVER was a hard requirement of the
whole corpus -- including every test that never opens a connection. With psycopg
absent the run did not fail a test, it failed to COLLECT:

    ImportError while loading conftest '.../conftest.py'
    conftest.py:15: in <module>
        import psycopg
    E   ModuleNotFoundError: No module named 'psycopg'

That coupling is why the guard-testing half of this corpus cannot run where the
gate runs. README.md records the decision not to register the corpus in `SUITES`
and names the price; this removes one of the two things making that price real.

These arms keep it removed. A module-scope import reads like an ordinary tidy-up
when someone adds a fixture, and nothing else here would notice.

AND THE MEMBERSHIP IS DECIDED RATHER THAN DECLAIMED. `NO_CLUSTER` below says
which files need no database, and the gate runs exactly those. A list that is
only ASSERTED is a silent coverage hole: a new database-free test file is simply
absent from the job, every arm stays green, and the tests never run anywhere. So
the property is computed from the corpus and the two are required to AGREE, in
both directions.
"""

import ast
import os
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# The database driver, and the module that provisions a cluster. Named once: the
# classifier below asks conftest.py which fixtures reach a cluster rather than
# being told their names, so these two are the only hard-coded identifiers.
DRIVER = "psycopg"
CLUSTER_MODULE = "pgc_cluster"

# Where the ci.yml region the count arm reads begins. The job's own comment, not
# the job key, because a prose count lives in the comment.
JOB_COMMENT = "# The pytest harness's own guards"

# THE FILES THAT REACH NO DATABASE. This is what the `pytest-guards` job in
# .github/workflows/ci.yml runs, derived from here rather than copied into the
# workflow.
#
# DECLARED HERE, DECIDED BELOW. `test_the_declaration_is_exactly_the_database_free_half`
# computes the property over the corpus and requires SET EQUALITY with this list,
# so a database-free file nobody adds here is NAMED rather than silently skipped,
# and a file listed here that starts using a cluster fixture is named too.
#
# A LIST *AND* A PROPERTY, RATHER THAN THE PROPERTY ALONE, DELIBERATELY. Deriving
# the membership outright and keeping no list would remove the hand-maintained
# value, but it would also leave the classifier with nothing to be checked
# against: a bug that dropped a file would quietly shrink what the gate runs and
# nothing would go red -- the same silent hole, one level down. Two declarations
# that must agree fail loudly whichever of them is wrong. It is the shape
# selftest 350 already uses for TESTS.md, for the same reason.
NO_CLUSTER = [
    "test_build_refusal.py",
    "test_docs_cover_the_corpus.py",
    "test_guards_pinned.py",
    "test_layer.py",
    "test_ordered.py",
    "test_runshape.py",
    # Landed on main in #922 after this list was written, and the arm above caught
    # it: the property says it needs no database, so the declaration must say so too.
    "test_suite_accounting.py",
    # The classifier's own controls, split out of this file so the job whose file
    # list IS this list actually runs them. This file cannot be in the list: it
    # hands cluster-bound file names to pytest, so it needs what they need.
    "test_harness_deps_classifier.py",
    "test_writes_wrote_rows.py",
    # An AST sweep plus `inspect`, so it needs neither a cluster nor the driver.
    "test_loop_coverage_premise.py",
    # Landed on main in #930 while this branch was in review, and the arm below named
    # it: cluster-free, not driver-dependent, so the job can run it and the
    # declaration has to say so. The third time this arm has caught a merge-order
    # consequence rather than a mistake.
    "test_failed_query_sentinel.py",
    # This branch's file, named by the same arm the moment #921's classifier
    # arrived on the base. It drives test/pgc_ledger.py, which is a python tool
    # rather than the shell harness, so it needs neither a cluster nor psycopg:
    # measured at 9 passed in a venv with no driver, and driver_dependent()
    # agrees. The fourth time this arm has caught a merge-order consequence
    # rather than a mistake, which is the argument for it.
    "test_mutation_ledger.py",
    # #937's first phase. It exercises `Expect` directly -- no connection, no
    # cluster, no driver -- so the classifier puts it here and the declaration must
    # agree. The fifth time this arm has decided a membership rather than been told
    # one.
    "test_check_records.py",
    # The twin #998 did not ship. That PR added `test/selftest/470` and no pytest
    # half, against the owner's rule that a test in one harness is not finished --
    # and the sixth catch of this arm was the file's arrival, not its absence. It
    # drives .github/scripts/skip-loop-arms.py by subprocess, which is a python
    # tool rather than the shell harness, so it needs neither a cluster nor the
    # driver.
    "test_skip_loop_arms.py",
    # #752 docs. Reads docs/how-to.md and docs/best-practices.md. No cluster,
    # no driver: the public seam is the published page.
    "test_docs_join_clustering.py",
    # #1017 docs. Reads configuration.md, administration.md and best-practices.md.
    # No cluster, no driver: the public seam is the published page.
    "test_docs_stripe_floor.py",
    "test_docs_table_structure.py",
]


# ---------------------------------------------------------------------------
# Deciding "this file needs no database"
#
# AN AST WALK RATHER THAN A LINE REGEX, and this layer has already paid for that
# lesson once: the broad-except refusal was first written as a line regex and
# immediately rejected its own tests, because the forbidden shape appears inside
# a `pytester.makepyfile` STRING. The same trap is here in three forms -- a file
# may name the driver in a docstring, build another test as a string, or discuss
# a cluster fixture in prose -- and a grep cannot tell any of those from code.
#
# NEEDING A DATABASE IS NOT THE SAME AS IMPORTING THE DRIVER. A test reaches a
# cluster through a FIXTURE and may import nothing at all, so the property is:
#
#   a file is cluster-bound if it imports the driver at module scope (which kills
#   collection outright), or if any test or fixture in it requests -- directly or
#   transitively -- a fixture that reaches a cluster, or if it DRIVES a
#   cluster-bound file as a subprocess.
#
# The cluster fixtures themselves are derived from conftest.py rather than typed
# here, so a new one is covered without a second edit.
# ---------------------------------------------------------------------------


def _parse(path):
    return ast.parse(pathlib.Path(path).read_text(), filename=pathlib.Path(path).name)


def _prose(tree):
    """id() of every string used as a STATEMENT: module, class and function
    docstrings, and the bare strings this corpus uses as block comments.

    These are the strings a reader writes ABOUT code, so nothing in them counts
    as a reference to anything."""
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out.add(id(node.value))
    return out


def _own_body(node):
    """Every node inside NODE, not descending into a nested function.

    A nested function's body runs when IT is called, not when NODE is, so an
    import inside one is not an import by NODE."""
    out, stack = [], list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))
    return out


def _module_scope(tree):
    """Every node that executes at import time: the module body and anything
    nested in its `if`/`try`/`with`, but nothing inside a def or a class."""
    out, stack = [], list(tree.body)
    while stack:
        n = stack.pop()
        out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))
    return out


def _needs_the_driver_installed(tree, prose):
    """Does this file need psycopg IMPORTABLE, even though it requests no cluster?

    The job this declaration feeds installs no driver as well as running no cluster, so
    the property it needs is wider than "requests no cluster fixture". A file that builds
    a source with `import psycopg` in it and gives that to `pytester` needs the driver:
    the inner run imports the generated module, and with the driver absent it fails at
    import rather than reaching whatever the arm was about.

    Measured: `test_raises_sqlstate.py` requests no cluster fixture and is correctly
    cluster-free, and four of its arms FAIL with psycopg shimmed out, because the files
    it generates import it.

    THIS IS NOT THE CLUSTER PROPERTY AND MUST NOT BE FOLDED INTO IT. `partition()` asks
    whether a file requests a cluster; a generated inner test requesting `pgc_conn` is
    the INNER run's requirement, not this file's, and
    `test_the_classifier_is_not_fooled_by_prose_that_names_the_driver` asserts exactly
    that. Wiring this into `partition()` contradicted that arm, correctly, on the first
    attempt. The job needs BOTH properties, so the job's list is the intersection and the
    two are derived separately.

    TWO CONDITIONS, because a driver import in a string is not enough on its own.
    `test_harness_deps_classifier.py` writes fixture corpora containing `import psycopg`
    and only ever PARSES them -- nothing runs them, so it needs no driver and belongs in
    the job. The difference is whether the file drives `pytester`, and reading only the
    string would have thrown that file out of the gate it exists to be in.

    Prose is excluded for the reason it is excluded everywhere here: a docstring naming
    the driver is a sentence, not an import.
    """
    drives_inner_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "pytester":
            drives_inner_run = True
            break
    if not drives_inner_run:
        return False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in prose
                and ("import psycopg" in node.value or "from psycopg" in node.value)):
            return True
    return False


def _imports_driver(nodes):
    for n in nodes:
        if isinstance(n, ast.Import):
            if any(a.name.split(".")[0] == DRIVER for a in n.names):
                return True
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[0] == DRIVER:
                return True
    return False


def _is_fixture(fn):
    """@pytest.fixture, @pytest.fixture(...), @fixture or @fixture(...)."""
    for dec in fn.decorator_list:
        f = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(f, ast.Attribute) and f.attr == "fixture":
            return True
        if isinstance(f, ast.Name) and f.id == "fixture":
            return True
    return False


def _fixture_name(fn):
    """The name a test REQUESTS this fixture by: the alias when it has one.

    `@pytest.fixture(name="conn")` makes the function requestable as `conn` and NOT
    as its own name. Recording the def's name therefore did two wrong things at
    once: it missed the dependency a test declares, and it invented a fixture name
    nothing can request. Reported by @linuxhikerpm.
    """
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        f = dec.func
        if (isinstance(f, ast.Attribute) and f.attr == "fixture") or \
           (isinstance(f, ast.Name) and f.id == "fixture"):
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) \
                   and isinstance(kw.value.value, str):
                    return kw.value.value
    return fn.name


def _usefixtures_in(node):
    """Fixture names a single `usefixtures(...)` call names, or [] if it is not one."""
    if not isinstance(node, ast.Call):
        return []
    f = node.func
    if (isinstance(f, ast.Attribute) and f.attr == "usefixtures") or \
       (isinstance(f, ast.Name) and f.id == "usefixtures"):
        return [a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return []


def _usefixtures(fn):
    """Fixture names pulled in by `@pytest.mark.usefixtures(...)` on this def.

    A dependency with NO PARAMETER, so a walk over the signature cannot see it.
    This is the form a test uses precisely when it wants the fixture's effect and
    not its value -- which is exactly when it is a cluster it wants.
    """
    out = []
    for dec in fn.decorator_list:
        out += _usefixtures_in(dec)
    return out


def _module_usefixtures(tree):
    """Fixture names a module-level `pytestmark` pulls in for EVERY test in the file.

    `pytestmark = pytest.mark.usefixtures("pgc_conn")`, and the list form. pytest
    applies it to every test in the module, so it is a dependency of all of them and
    of none of their signatures. Reported by @jdatcmd, who built it alongside the
    class form and measured both classified database-free.
    """
    out = []
    for n in tree.body:
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets):
            continue
        vals = n.value.elts if isinstance(n.value, (ast.List, ast.Tuple)) else [n.value]
        for v in vals:
            out += _usefixtures_in(v)
    return out


def _params(fn):
    """Every parameter pytest will try to resolve as a fixture.

    POSITIONAL-ONLY, POSITIONAL, AND KEYWORD-ONLY. pytest resolves a keyword-only
    parameter as a fixture exactly as it resolves a positional one; reading only
    `args.args` classified `def test_x(*, pgc_conn)` as needing nothing at all.

    `self` and `cls` are dropped: they are bound by Python, not by pytest, and a
    fixture cannot be requested under either name.
    """
    a = fn.args
    names = ([q.arg for q in getattr(a, "posonlyargs", [])]
             + [q.arg for q in a.args]
             + [q.arg for q in a.kwonlyargs])
    return [n for n in names if n not in ("self", "cls")]


def _collectable(tree):
    """(qualifier, def, inherited) for every def pytest can collect or resolve.

    `inherited` is the fixture names an enclosing CLASS or the MODULE pulls in with
    `usefixtures`. pytest applies a class decorator to every method and a module-level
    `pytestmark` to every test, so those are dependencies of defs whose own decorator
    list and signature say nothing. Without them the class form was classified
    database-free -- which is the form the class-method descent below exists to serve,
    so the two belonged in one change and only one of them was there.

    MODULE LEVEL AND CLASS BODIES. pytest collects `test_*` methods of a class and
    resolves their fixtures identically, so a walk over `tree.body` alone
    classified `class TestX: def test_y(self, pgc_conn)` as needing no database.
    Every class is walked rather than only `Test*`-named ones: a classifier that
    guesses the collection convention is one convention change from being wrong,
    and counting a non-collected method is conservative in the safe direction.
    """
    def walk(node, prefix, inherited):
        for n in node.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix, n, list(inherited)
            elif isinstance(n, ast.ClassDef):
                cls_marks = []
                for dec in n.decorator_list:
                    cls_marks += _usefixtures_in(dec)
                yield from walk(n, prefix + n.name + ".", list(inherited) + cls_marks)
    return list(walk(tree, "", _module_usefixtures(tree)))


def _defs(tree):
    """{key: (kind, params, body)} for every def pytest can collect or resolve.

    kind is "fixture", "test" or "helper". Only the first two can pull a fixture
    in: pytest resolves parameter names for those, and a helper's parameter is
    just a parameter -- so a helper taking `conn` named after a fixture must not
    make its file cluster-bound.

    A FIXTURE IS KEYED BY ITS REQUESTABLE NAME, because that is the name another
    def names to depend on it, and the closure below matches keys against
    parameters. Tests and helpers are keyed by their qualified name instead: those
    names are never requested, and two classes may both define `test_x`, which a
    bare-name key would collapse into one -- silently dropping a def from the walk.
    """
    out = {}
    for prefix, n, inherited in _collectable(tree):
        if _is_fixture(n):
            kind, key = "fixture", _fixture_name(n)
        elif n.name.startswith("test_"):
            kind, key = "test", prefix + n.name
        else:
            kind, key = "helper", prefix + n.name
        out[key] = (kind, _params(n) + _usefixtures(n) + inherited, _own_body(n))
    return out


def dynamic_requests(tree):
    """Call sites of `request.getfixturevalue(...)`, which no AST can resolve.

    The name is computed at run time, so a static classifier cannot know which
    fixture is pulled -- and the honest answer is not to ban the form but to stop
    claiming a file that uses it needs no database. `partition` treats such a file
    as cluster-bound, which is wrong only in the direction that costs a little CI
    time rather than the direction that reports a green gate for tests nothing ran.
    """
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
           and n.func.attr == "getfixturevalue":
            out.append(n.lineno)
    return out


def _imported_from(tree, module):
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "") == module:
            names |= {a.asname or a.name for a in n.names}
    return names


def _close_over_fixtures(defs, reached):
    """Add every fixture that requests something already reached, until stable."""
    changed = True
    while changed:
        changed = False
        for name, (kind, params, _body) in defs.items():
            if kind == "fixture" and name not in reached and set(params) & reached:
                reached.add(name)
                changed = True
    return reached


def cluster_fixtures(conftest):
    """The fixture names that reach a cluster, READ OFF conftest.py.

    A fixture reaches a cluster if it imports the driver, or calls something
    imported from pgc_cluster (which is what provisions a server), or requests a
    fixture that does. Derived rather than typed, so adding a third connecting
    fixture to conftest does not need an edit here."""
    tree = _parse(conftest)
    provisioners = _imported_from(tree, CLUSTER_MODULE)
    defs = _defs(tree)
    roots = set()
    for name, (kind, _params, body) in defs.items():
        if kind != "fixture":
            continue
        called = {n.func.id for n in body
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if _imports_driver(body) or (called & provisioners):
            roots.add(name)
    return _close_over_fixtures(defs, roots)


def _mentioned_files(tree, others):
    """Corpus file names this module names OUTSIDE its prose -- that is, in code.

    A file that hands another file's name to pytest is driving it, and inherits
    what that file needs."""
    prose = _prose(tree)
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in prose):
            found |= {o for o in others if o in node.value}
    return found


def partition(directory=None):
    """(database-free, cluster-bound) over every test_*.py in DIRECTORY, sorted.

    Every file lands in exactly one bucket, so the two lengths sum to the number
    of files the glob saw."""
    directory = HERE if directory is None else pathlib.Path(directory)
    names = sorted(p.name for p in directory.glob("test_*.py"))
    roots = cluster_fixtures(directory / "conftest.py")

    bound, mentions = {}, {}
    for name in names:
        tree = _parse(directory / name)
        defs = _defs(tree)
        # NO CLOSURE OVER THE FILE'S OWN FIXTURE GRAPH, and that is not an
        # omission. A chain of local fixtures can only reach a cluster if some
        # fixture IN the chain names a conftest root as its own parameter -- and
        # that fixture is itself in this loop, so walking the chain finds nothing
        # the direct check does not. Measured: neutering a closure here changed
        # no file's classification, which is what dead code does.
        #
        # The closure IS load-bearing inside conftest, where a fixture can reach
        # a cluster through a sibling without importing anything of its own, and
        # `test_the_classifier_follows_a_conftest_fixture_that_connects_indirectly`
        # kills it there.
        uses = any(kind in ("fixture", "test") and set(params) & roots
                   for kind, params, _body in defs.values())
        # A dynamic request is unresolvable, so the file is treated as
        # cluster-bound rather than assumed free. Wrong in the direction that
        # costs CI time, not in the direction that greens a gate over tests
        # nothing ran.
        dynamic = bool(dynamic_requests(tree))
        bound[name] = _imports_driver(_module_scope(tree)) or uses or dynamic
        mentions[name] = _mentioned_files(tree, set(names) - {name})

    # A file that drives a cluster-bound file needs whatever that file needs.
    changed = True
    while changed:
        changed = False
        for name in names:
            if not bound[name] and any(bound[o] for o in mentions[name]):
                bound[name] = True
                changed = True

    return ([n for n in names if not bound[n]], [n for n in names if bound[n]])


def driver_dependent(directory=None):
    """Cluster-free files that still need psycopg importable, so the job cannot run them.

    The job this declaration feeds installs no driver as well as running no cluster, so
    its file list is the INTERSECTION of the two properties rather than either one.
    `test_raises_sqlstate.py` is the case that showed the difference: it requests no
    cluster fixture, and four of its arms fail with psycopg shimmed out, because the
    modules it hands to `pytester` import it.
    """
    directory = HERE if directory is None else pathlib.Path(directory)
    out = []
    for path in sorted(directory.glob("test_*.py")):
        tree = _parse(path)
        if _needs_the_driver_installed(tree, _prose(tree)):
            out.append(path.name)
    return out


def job_runnable(directory=None):
    """The files the driver-free job can run: cluster-free AND not driver-dependent."""
    directory = HERE if directory is None else pathlib.Path(directory)
    free, _bound = partition(directory)
    return sorted(set(free) - set(driver_dependent(directory)))


def membership_report(directory=None, declared=None):
    """"[]" when the declaration is exactly the database-free half, else
    "[n: kind:file ...]" naming every disagreement and which way it goes.

    The same "[]" / "[n: ...]" shape the selftest parts use, so a failure names
    what is wrong rather than only that something is."""
    directory = HERE if directory is None else pathlib.Path(directory)
    declared = NO_CLUSTER if declared is None else declared
    runnable = set(job_runnable(directory))
    cluster_free = set(partition(directory)[0])
    driver_bound = set(driver_dependent(directory))
    present = {p.name for p in directory.glob("test_*.py")}
    bad = ["absent:" + n for n in sorted(set(declared) - present)]
    bad += ["needs-a-cluster:" + n
            for n in sorted((set(declared) & present) - cluster_free)]
    # A kind of its own, because "needs-a-cluster" would be a wrong diagnosis and the
    # reader's next action differs: this file needs the DRIVER, not a server.
    bad += ["needs-the-driver:" + n
            for n in sorted((set(declared) & present) & driver_bound)]
    bad += ["undeclared:" + n for n in sorted(runnable - set(declared))]
    return "[]" if not bad else "[%d: %s]" % (len(bad), " ".join(bad))


def test_the_declaration_names_each_file_once(expect):
    """`membership_report` compares SETS, so it cannot see a name listed twice.

    The list is the artifact, not the set: CI builds its file list with
    `" ".join(NO_CLUSTER)`, and the job prints how many files it is running. A
    duplicate makes that printed count one too high.

    MEASURED, because the first version of this docstring also claimed the file would
    RUN TWICE and that is false: 12 entries and 13 entries both give `215 passed`,
    because pytest deduplicates identical paths on its command line. So the cost is a
    wrong number in the job's own output, not wasted work -- which still matters here,
    since that printed count is the only place a reader learns how wide the
    database-free job is.

    Found by a count that did not reconcile: the declaration said 13 and
    `job_runnable` said 12 while both set differences were empty, which is only
    possible if a name appears twice.

    A set comparison hiding a duplicate is the same shape as a check-name collision
    folding two ledger rows into one: whenever the mechanism compares sets, the
    cardinality needs its own arm.
    """
    seen, dupes = set(), []
    for name in NO_CLUSTER:
        if name in seen:
            dupes.append(name)
        seen.add(name)
    expect.text(", ".join(sorted(set(dupes))) or "none", "none",
                "no file is declared twice in NO_CLUSTER")
    expect.num(len(NO_CLUSTER), len(seen),
               "and the list's length is its number of distinct names")


def _main(argv):
    """The gate's entry point. The pytest corpus is not in `SUITES`, so the arms
    below run nowhere the gate can see them; selftest 350 runs this instead."""
    if len(argv) < 2 or argv[0] not in ("--disagree", "--partition"):
        # A mode with no DIRECTORY has to be a usage error rather than an
        # IndexError: the caller is a shell arm, and a traceback on stderr with an
        # empty stdout is what a passing "[]" comparison looks like from bash.
        sys.stderr.write(
            "usage: test_harness_deps.py --disagree DIR [FILE...]\n"
            "       test_harness_deps.py --partition DIR\n")
        return 2
    mode, directory, rest = argv[0], argv[1], argv[2:]
    if mode == "--partition":
        free, bound = partition(directory)
        print("free: %s | bound: %s" % (" ".join(free) or "-",
                                        " ".join(bound) or "-"))
        return 0
    # An EMPTY declaration is a real question ("nothing is declared"), so the
    # default only applies when no FILE argument was given at all.
    print(membership_report(directory, rest if len(argv) > 2 else None))
    return 0


def _run_without_psycopg(args, expect, pg_config=None):
    """Run pytest with `import psycopg` forced to fail, and return the result.

    THE SUBPROCESS GETS THE SAME pg_config THIS RUN WAS GIVEN (#1016). Without it the
    child falls back to conftest's DEFAULT_PG_CONFIG, `/usr/local/pg18a/bin/pg_config`,
    which exists on the audit container and on no GitHub runner. The cluster fixture then
    fails on the missing pg_config BEFORE anything imports psycopg, so the output never
    names the shim and the arm below reports the shim absent when the shim was fine.
    Measured: passes against a source-built prefix, fails against a packaged one.

    A SHIM RATHER THAN AN UNINSTALL. Uninstalling psycopg would test the machine
    rather than the harness, cannot run concurrently with anything else, and
    leaves the environment broken if the test dies. A module that raises on
    import, placed FIRST on the path, is the same observation and is reversible
    by construction.
    """
    shim = tempfile.mkdtemp(prefix="pgc-nopsy-")
    pathlib.Path(shim, "psycopg.py").write_text(
        'raise ImportError("psycopg is shimmed out by '
        'test_harness_deps: the harness must self-test without a database")\n'
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = shim + os.pathsep + str(HERE)
    extra = ["--pg-config", pg_config] if pg_config else []
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra, *args],
        cwd=str(HERE), env=env, capture_output=True, text=True,
    )
    # PREMISE: the shim must actually bite, or this arm proves nothing at all.
    probe = subprocess.run(
        [sys.executable, "-c", "import psycopg"],
        cwd=str(HERE), env=env, capture_output=True, text=True,
    )
    expect.at_least(int("ImportError" in probe.stderr), 1,
                    "premise: the shim really does make `import psycopg` fail")
    return proc


def test_the_guard_half_of_the_corpus_runs_without_a_database_driver(expect):
    """The database-free files need no database, so they must not need its driver
    either. The count is not written down here: the run prints it."""
    proc = _run_without_psycopg(NO_CLUSTER, expect)
    out = proc.stdout + proc.stderr
    # Collection surviving is the first thing to establish: without it, a green
    # exit code would only mean pytest never got as far as running anything.
    defeated = "ImportError while loading conftest" in out or "ModuleNotFoundError" in out
    expect.text(repr(defeated), "False",
                "collection is not defeated by the absent driver")
    expect.num(proc.returncode, 0,
               f"the no-cluster files pass with psycopg absent: {out[-400:]}")
    print("\n-- no-cluster files: %d, driver shimmed out: %s"
          % (len(NO_CLUSTER), out.strip().splitlines()[-1]))


def test_a_cluster_test_still_needs_the_driver(expect, pytestconfig):
    """THE CONTROL, and without it the arm above is satisfied by a corpus that
    connects to nothing at all.

    Deferring the import must not have made the database optional -- only its
    IMPORT lazy. A test that actually wants a connection must still fail when the
    driver is gone, and it must fail for that reason rather than by being skipped.
    """
    # The pg_config THIS run was given, not conftest's default: see
    # _run_without_psycopg. Without it the child dies on a missing prefix before it can
    # reach the import, and this arm then reports the shim absent.
    proc = _run_without_psycopg(["test_connection.py"], expect,
                                pg_config=pytestconfig.getoption("--pg-config"))
    expect.at_least(proc.returncode, 1,
                    "a cluster test cannot pass without the driver")
    expect.at_least(
        int("psycopg is shimmed out" in (proc.stdout + proc.stderr)), 1,
        "and it fails BECAUSE the driver is gone, naming the shim")


def test_conftest_imports_no_database_driver_at_module_scope(expect):
    """The regression named directly, because the behavioural arm above is slow
    and a reader changing conftest deserves to be told in one line."""
    src = (HERE / "conftest.py").read_text()
    module_scope = [
        ln for ln in src.splitlines()
        if ln.startswith("import psycopg") or ln.startswith("from psycopg")
    ]
    expect.text(", ".join(module_scope) or "none", "none",
                "conftest.py imports no database driver at module scope")
    # PREMISE: the check can see an import at all -- otherwise "none" is what a
    # broken reader says too.
    fake = "import os\nimport psycopg\n"
    seen = [ln for ln in fake.splitlines() if ln.startswith("import psycopg")]
    expect.num(len(seen), 1, "premise: the reader recognises a module-scope import")


# ---- the declaration must be DECIDED, in both directions ---------------------
#
# WHAT WAS WRONG. The only arm over NO_CLUSTER asked whether the files it names
# EXIST. That is one direction of a membership claim, and the missing direction
# is the one that loses coverage: a database-free file nobody adds to the list is
# absent from the gate's job, every arm stays green, and nothing says so. It had
# already happened twice on this branch -- test_build_refusal.py and
# test_layer.py both need no database and neither was listed -- and a concurrent
# branch adds a third.
#
# A floor of 4 on the list length did not help either: the list had four entries,
# so the floor was satisfied by the very state it was meant to police.


def test_the_declaration_is_exactly_the_database_free_half(expect):
    """NO_CLUSTER == the files the property says need no database, both ways.

    This is the arm that closes the hole. It names the offender and which way the
    disagreement goes, because "the lists differ" is not something a reader can
    act on."""
    expect.text(membership_report(), "[]",
                "NO_CLUSTER is exactly the database-free half of the corpus")


def test_the_partition_accounts_for_every_file_in_the_corpus(expect):
    """PREMISE for the arm above: the classifier saw the corpus, and split it.

    A classifier that parsed nothing reports an empty database-free set, which
    agrees with an empty declaration and looks exactly like success. And one that
    called everything database-free would also pass a one-directional check."""
    free, bound = partition()
    files = sorted(p.name for p in HERE.glob("test_*.py"))
    expect.num(len(free) + len(bound), len(files),
               f"every file lands in exactly one bucket: {len(free)}+{len(bound)}")
    expect.at_least(len(files), 10, "premise: the glob found the corpus")
    expect.at_least(len(free), 1, "premise: the partition is not all cluster-bound")
    expect.at_least(len(bound), 1, "premise: the partition is not all database-free")
    print("\n-- partition: %d database-free, %d cluster-bound, %d files"
          % (len(free), len(bound), len(files)))


def test_the_cluster_fixtures_are_read_off_conftest_rather_than_named_here(expect):
    """The roots of the property are DERIVED. The only identifiers this module
    spells out are the driver and the module that provisions a server, so a third
    connecting fixture in conftest is covered without an edit here."""
    roots = cluster_fixtures(HERE / "conftest.py")
    expect.at_least(len(roots), 2,
                    f"conftest names fixtures that reach a cluster: {sorted(roots)}")
    print("\n-- cluster fixtures derived from conftest.py: %s" % sorted(roots))


# ---- and the classifier must be able to get it WRONG -------------------------
#
# Everything above passes on a healthy tree, which is what a classifier that
# returns a constant also does. These arms run it over fixtures built to be each
# shape it has to tell apart, so a future edit that neuters it reddens here while
# the real corpus stays clean.

_FAKE_CONFTEST = '''\
"""A conftest shaped like the real one: one fixture imports the driver, one
depends on that fixture and imports it too."""
import pytest
from pgc_cluster import make_cluster

@pytest.fixture(scope="session")
def pgc_cluster():
    cluster = make_cluster()
    import psycopg
    yield cluster

@pytest.fixture
def pgc_conn(pgc_cluster):
    import psycopg
    yield psycopg.connect("")
'''


def _fake_corpus(tmp_path, files, conftest=None):
    d = tmp_path / "corpus"
    d.mkdir(exist_ok=True)
    (d / "conftest.py").write_text(_FAKE_CONFTEST if conftest is None else conftest)
    for name, body in files.items():
        (d / name).write_text(body)
    return d


def test_the_membership_report_names_a_database_free_file_left_undeclared(tmp_path, expect):
    """THE HOLE, on a fixture. This is the exact shape that shipped: a file that
    needs no database and is in nobody's list."""
    d = _fake_corpus(tmp_path, {
        "test_free.py": "def test_it(expect):\n    pass\n",
        "test_bound.py": "def test_it(pgc_conn, expect):\n    pass\n",
    })
    expect.text(membership_report(d, []), "[1: undeclared:test_free.py]",
                "an undeclared database-free file is named")
    expect.text(membership_report(d, ["test_free.py"]), "[]",
                "control: the same corpus with it declared is clean")


def test_the_membership_report_names_a_declared_file_that_needs_a_cluster(tmp_path, expect):
    """The other direction: a listed file that starts using a cluster fixture
    would make the gate's job fail for a reason nobody declared."""
    d = _fake_corpus(tmp_path, {
        "test_free.py": "def test_it(expect):\n    pass\n",
        "test_bound.py": "def test_it(pgc_conn, expect):\n    pass\n",
    })
    expect.text(membership_report(d, ["test_free.py", "test_bound.py"]),
                "[1: needs-a-cluster:test_bound.py]",
                "a declared file that reaches a cluster is named")


def test_the_membership_report_names_a_declared_file_that_is_gone(tmp_path, expect):
    """A rename used to be the only thing the old arm caught. It still is caught,
    and as its own kind rather than as "needs a cluster"."""
    d = _fake_corpus(tmp_path, {
        "test_free.py": "def test_it(expect):\n    pass\n",
    })
    expect.text(membership_report(d, ["test_free.py", "test_renamed_away.py"]),
                "[1: absent:test_renamed_away.py]",
                "a declared file that does not exist is named")


def test_the_gate_runs_the_membership_decision_rather_than_only_this_file(expect):
    """A guard that does not run is a comment, and nothing in the gate runs
    pytest over this file: the corpus is not in `SUITES` (README.md), and the
    `pytest-guards` job runs the database-free files, which this is not.

    So selftest 350 runs the decision through this module's command line. This
    arm is what keeps that true."""
    part = (HERE.parent / "selftest" / "350-the-pytest-corpus-must-be.sh")
    expect.text(repr(part.is_file()), "True", "premise: selftest 350 is where this expects")
    text = part.read_text()
    expect.at_least(text.count("--disagree"), 1,
                    "selftest 350 decides the membership in the gate")
    # And the command line it uses must work, rather than being a string nobody
    # ran: the same call, made here.
    proc = subprocess.run([sys.executable, str(HERE / "test_harness_deps.py"),
                           "--disagree", str(HERE)],
                          capture_output=True, text=True)
    expect.num(proc.returncode, 0, f"the command line runs: {proc.stderr[-300:]}")
    expect.text(proc.stdout.strip(), "[]",
                "and gives the same verdict as the arm above")

    # AND A MISUSE MUST BE A USAGE ERROR, not a traceback. An empty stdout is
    # exactly what bash compares as a passing "[]", so a crash here would read as
    # a clean corpus: the arm in selftest 350 would pass while deciding nothing.
    bad = subprocess.run([sys.executable, str(HERE / "test_harness_deps.py"),
                          "--partition"], capture_output=True, text=True)
    expect.num(bad.returncode, 2, f"a mode with no directory is a usage error: {bad.stderr[-200:]}")
    expect.text(repr(bad.stdout), "''", "and it writes nothing to stdout")
    expect.at_least(bad.stderr.count("usage:"), 1, "and says how to call it instead")


def test_ci_derives_the_file_list_rather_than_repeating_it(expect):
    """CI must ask this module for NO_CLUSTER, not carry its own copy.

    A second copy of a list is the defect this repository spent a day removing
    from TESTS.md: a hand-maintained value whose correct content is a function of
    the tree, going stale silently because nothing compares the two. The job runs
    `from test_harness_deps import NO_CLUSTER`, so adding a file here changes what
    CI runs with no second edit.

    The pins are single-sourced the same way, out of requirements-test.txt, so the
    version CI installs cannot drift from the version the corpus was tested with.
    """
    ci = (HERE.parent.parent / ".github" / "workflows" / "ci.yml")
    expect.text(repr(ci.is_file()), "True", "premise: ci.yml is where this expects")
    text = ci.read_text()

    expect.at_least(text.count("from test_harness_deps import NO_CLUSTER"), 1,
                    "the job derives the file list from this module")
    expect.at_least(text.count("requirements-test.txt"), 1,
                    "and the pins from requirements-test.txt")

    # AND IT MUST NOT ALSO HARDCODE THEM. Deriving plus a stale literal copy is
    # worse than either alone, because the copy looks authoritative.
    job = text[text.index("pytest-guards:"):]
    job = job[:job.index("\n  build:")] if "\n  build:" in job else job
    hardcoded = [n for n in NO_CLUSTER if n in job]
    expect.text(", ".join(hardcoded) or "none", "none",
                "the job names no corpus file literally")

    # AND IT MUST STATE NO COUNT. A number in a comment is the same
    # hand-maintained derived value as a number in a list, and it went stale the
    # day it was written: the job's own comment said 152 while the corpus held
    # 154. The job prints what it ran instead.
    #
    # OVER THE COMMENT BLOCK AS WELL AS THE JOB BODY. The slice above starts at
    # the job key, and the comment explaining the job sits ABOVE that -- which is
    # exactly where the stale count was, so a region ending at the job key could
    # not see it.
    region = text[text.index(JOB_COMMENT):]
    region = region[:region.index("\n  build:")] if "\n  build:" in region else region
    expect.at_least(len(region), len(job), "premise: the region includes the comment block")
    counts = re.findall(r"\b\d+ (?:tests|files|of \d+)\b", region)
    expect.text(", ".join(counts) or "none", "none",
                "the job and its comment state no corpus count")


def test_the_cluster_job_runs_the_other_half_and_derives_it(expect):
    """The complement of NO_CLUSTER must be RUN, and derived rather than listed (#1016).

    93 test functions in 8 files ran in no CI job at all: ci.yml had one pytest job and it
    installs psycopg deliberately not, nightly.yml mentions pytest zero times, and
    run_all_versions.sh must mention it zero times because the two harnesses stay
    independent. A quarter of the corpus passed when somebody ran it by hand and nothing
    noticed when it stopped.

    Derived, for the reason `pytest-guards` derives its half: two hand-maintained copies of
    which file needs a database is a value whose correct content is a function of the tree,
    and it goes stale silently because nothing compares them.
    """
    ci = (HERE.parent.parent / ".github" / "workflows" / "ci.yml")
    expect.text(repr(ci.is_file()), "True", "premise: ci.yml is where this expects")
    text = ci.read_text()

    expect.at_least(text.count("pytest-cluster:"), 1,
                    "a job runs the half that needs a cluster")
    job = text[text.index("pytest-cluster:"):]
    job = job[:job.index("\n  build:")] if "\n  build:" in job else job

    expect.at_least(job.count("NO_CLUSTER"), 1,
                    "and it derives its file list from this module rather than listing it")
    expect.at_least(job.count("requirements-test.txt"), 1,
                    "and installs the pins from requirements-test.txt, not whatever is there")
    # The control for pytest-guards' own assertion. That job proves its files need no
    # database by psycopg's ABSENCE, so this one has to state its presence or the pair
    # proves nothing about the split.
    expect.at_least(job.count("pip show psycopg"), 1,
                    "and asserts the driver IS present, which is what makes it the other half")

    # It must not list the corpus either way round: neither the files it runs nor the
    # files it does not.
    hardcoded = [n for n in NO_CLUSTER if n in job]
    expect.text(", ".join(hardcoded) or "none", "none",
                "the cluster job names no database-free file literally")


def test_both_pytest_jobs_assert_how_many_tests_they_collected(expect):
    """A run that collects fewer tests than it should is a green that means nothing (#1016).

    `--pgc-expect-tests` existed and nothing passed it. What it closes is narrower than
    "pytest passed having run nothing" and worse: a nonexistent path already fails on its
    own, but a file list that resolves to REAL files and collects FEWER tests does not.
    Measured by dropping one file from the guard list:

        unarmed   rc=0   "255 passed"                              17 tests gone, silently
        armed     rc=4   "collected 255 test(s) but expected 272"

    THE NUMBERS LIVE IN A TRACKED FILE, for the reason check_ledger_budget.txt gives about
    its own: a change to one is then a diff a reviewer sees, next to the test that moved
    it. PGC_SKIP_TIMING is the precedent for the alternative -- set in two workflow files,
    suppressing whole suites for months, with no diff ever showing it.
    """
    ci = (HERE.parent.parent / ".github" / "workflows" / "ci.yml")
    text = ci.read_text()
    counts = HERE / "expected_tests.txt"
    expect.text(repr(counts.is_file()), "True",
                "premise: the expected counts are in a tracked file")

    nums = {}
    for line in counts.read_text().splitlines():
        f = line.split()
        if len(f) == 2 and f[1].isdigit() and not line.startswith("#"):
            nums[f[0]] = int(f[1])
    for key in ("guard_tests", "cluster_tests"):
        expect.at_least(nums.get(key, 0), 1,
                        f"{key} is named and positive, or the flag it feeds asserts nothing")

    # BOTH jobs, not one. Arming half of them would leave the other able to collect
    # nothing and pass, which is the state this closes.
    expect.num(text.count("--pgc-expect-tests"), 2,
               "both pytest jobs pass the flag")
    expect.num(text.count("expected_tests.txt"), 2,
               "and both read the number from the tracked file rather than stating it")
    # An empty read would OMIT the flag and fail open, so the value is guarded in the job.
    expect.num(text.count('test -n "$WANT"'), 2,
               "and each guards the read, because an empty value would fail open")


# A TRACKED KEY-VALUE FILE MUST NOT REPEAT A KEY, and the arm above could not see one.
# It builds `nums[f[0]] = int(f[1])`, so a duplicated key collapses and the last line
# wins -- the same shape as `read_ledger` before #982, one file over. A seam so the rule
# has fixtures, because the real file has no duplicate to redden on.
def _count_lines(text):
    """-> [(key, value)] for every `name N` line, REPEATS INCLUDED."""
    out = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        f = line.split()
        if len(f) == 2 and f[1].isdigit():
            out.append((f[0], int(f[1])))
    return out


def test_each_expected_count_is_stated_exactly_once(expect):
    """A merge that keeps both sides of this file duplicates a key, and CI says something
    unrecognisable instead of saying that.

    `ci.yml` reads the value with `awk '$1=="guard_tests"{print $2}'`, which prints one
    line per match. Two matches make `WANT` multi-line, `test -n "$WANT"` still passes,
    and the flag then refuses it. Measured, with two and with three duplicated lines:

        pytest: error: argument --pgc-expect-tests: invalid int value: '284\n280\n283'
        exit 4

    SO IT FAILS CLOSED, which is why this is an arm about legibility rather than a hole.
    Exit 4 reddens the job. What it does not do is say that a line is duplicated, and the
    person reading it has to work back from an int parse error to a merge resolution.

    THIS IS NOT HYPOTHETICAL. Three PRs of mine were open at once, each moving
    `guard_tests`, and a keep-both resolution across all three produced exactly this:

        guard_tests 284
        guard_tests 280
        guard_tests 283
        cluster_tests 205

    Keep-both is the right resolution for a changelog and the wrong one for a key-value
    file, and nothing in the tree said so.
    """
    counts = HERE / "expected_tests.txt"
    pairs = _count_lines(counts.read_text())
    expect.at_least(len(pairs), 2, "premise: the file states counts to check")
    keys = [k for k, _v in pairs]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    expect.text(", ".join(dupes) or "none", "none",
                "no key is stated twice in expected_tests.txt")
    expect.num(len(keys), len(set(keys)),
               "so the lines and the distinct keys are the same count")


def test_a_duplicated_count_is_caught_and_the_dict_form_is_not(expect):
    """The removal proof, and it shows WHY the arm above is not the one that existed.

    The older reading collapses the duplicate into a dict and reports a healthy file, so
    the two forms are run side by side on the same fixture.
    """
    fixture = ("# a comment\n"
               "guard_tests 284\n"
               "guard_tests 280\n"
               "cluster_tests 205\n")
    pairs = _count_lines(fixture)
    keys = [k for k, _v in pairs]
    expect.num(len(pairs), 3, "premise: the line reading sees all three lines")
    expect.text(", ".join(sorted({k for k in keys if keys.count(k) > 1})), "guard_tests",
                "the duplicated key is named")

    collapsed = {}
    for k, v in pairs:
        collapsed[k] = v
    expect.num(len(collapsed), 2, "while the dict form sees only two keys")
    expect.num(collapsed["guard_tests"], 280,
               "and keeps the LAST line, which is how the duplicate stayed invisible")


def test_the_job_installs_no_database_driver(expect):
    """The job's value is that it runs where there is no database.

    If it installed psycopg the tests would pass for the ordinary reason and prove
    nothing about the coupling this file exists to keep removed.
    """
    ci = (HERE.parent.parent / ".github" / "workflows" / "ci.yml")
    job = ci.read_text()
    job = job[job.index("pytest-guards:"):]
    job = job[:job.index("\n  build:")] if "\n  build:" in job else job
    # WHAT THIS USED TO ASK, AND WHY IT COULD NOT FAIL. The first assertion was
    #
    #     installs_driver = "psycopg" in job and "pip install" in job \
    #                       and "pip show psycopg" not in job
    #
    # and the second required `pip show psycopg` to be present. So whenever the second
    # passed, the third conjunct of the first was False and `installs_driver` was pinned
    # to False whatever the install line installed. @jdatcmd changed the job to
    # `pip install --quiet $PINS psycopg[binary]==3.3.5` and BOTH assertions still
    # passed. Two assertions guaranteeing each other's vacuity.
    #
    # The form below can fail: it looks at the install LINES and asks whether any of
    # them names the driver, and the premise after it is the control -- it appends such
    # a line to a copy and requires the same expression to see it.
    driver_installs = [l for l in job.splitlines()
                       if "pip install" in l and DRIVER in l]
    expect.num(len(driver_installs), 0,
               "no pip install line in the job names the database driver")
    planted = job + "\n          pip install --quiet $PINS " + DRIVER + "[binary]==3.3.5\n"
    expect.at_least(len([l for l in planted.splitlines()
                         if "pip install" in l and DRIVER in l]), 1,
                    "premise: and that test sees such a line when one is there")
    expect.at_least(job.count("pip show " + DRIVER), 1,
                    "and the job asserts the driver is absent rather than assuming it")


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

# ---- the harness-independence inventory, as a mechanism (#432) ------------------
#
# CONTEXT.md: the two harnesses are parallel in functionality and independent in
# implementation, and they must not call, import or reference each other. Its inventory
# of what still does was PROSE -- falsifiable by hand, but nothing reddened when a new
# reference appeared. #923 nearly landed a fourth coupled file, and the review that
# caught it was a person reading, not an arm.
#
# So the inventory is declared here and asserted in BOTH directions. A new file that
# reaches into the shell harness reddens this; a file that stops reaching and is not
# removed from the declaration reddens it too, which is what stops the list rotting
# into a permanent exemption.
#
# A FILE-LEVEL GUARD, which is what CONTEXT.md's rule asks for -- "count files, not
# lines" -- and also the most this can honestly be. Within a flagged file it cannot tell
# a path joined onto the REAL tree from the same name joined onto a `tmp_path`: both are
# the string "lib.sh" and only the dataflow says which. test_build_refusal.py contains
# both, and CONTEXT.md already records that the fake ones are rule 2 rather than
# references. So the assertion is over the SET OF FILES, and the per-file entry carries
# the mechanism a reader needs to check it by hand.
#
# THE COUNTING RULE IS CONTEXT.md'S, and the three exclusions are the ones it names,
# in the order it says they get confused: prose is not a reference, a file the test
# BUILDS ITSELF under tmp_path is not a reference even with the same name, and a word
# that merely looks like a filename is not one. Docstrings are dropped by AST position
# rather than by pattern, which is the only way to tell the first from a string the
# code actually passes to bash.

# THE DESCRIPTIONS NAME NO FILE, and that is not squeamishness: the first version
# spelled the helper library's path in them, and the detector flagged THIS file for its
# own inventory -- four files where the tree has three. The mechanism is what the entry
# has to carry; the filenames are in CONTEXT.md, which is prose and may name anything.
SHELL_REFERENCES = {
    "pgc_cluster.py":
        "sources the shell harness's helper library to call its build-and-install "
        "function, so the build refusal has one implementation rather than two",
    "test_build_refusal.py":
        "sources that library twice and for two reasons only: the one permitted "
        "cross-implementation arm, and a historical-parity arm whose fixture is its "
        "own. The pure-shell stamp and freshness drivers are gone; see CONTEXT.md",
    "test_suite_accounting.py":
        "reads the matrix runner's text and executes the real runner",
    "test_mutation_ledger.py":
        "executes the matrix runner with its list flag to get the registered suite "
        "list, which is the same mechanism the entry above uses",
}

_SHELL_NAMES = re.compile(
    r"(^|/)(lib\.sh|run_all_versions\.sh|portlib\.sh|harness_selftest\.sh)"
    r"|(^|/)test/[A-Za-z0-9_]+\.sh"
    r"|(^|/)selftest/")


def _docstring_nodes(tree):
    """Every Constant that is a docstring, by position rather than by content."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _shell_reference_sites(source):
    """-> [(lineno, text)] for executable strings naming a shell file in this tree.

    An f-string's PIECES are Constants of their own, so counting both the JoinedStr and
    its parts reported one reference as two. Measured: the fixture below asserted 1 and
    the first version answered 2.
    """
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for piece in node.values:
                if isinstance(piece, ast.Constant):
                    skip.add(id(piece))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            if _SHELL_NAMES.search(node.value):
                hits.append((node.lineno, node.value[:60]))
        elif isinstance(node, ast.JoinedStr):
            txt = "".join(v.value for v in node.values
                          if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if _SHELL_NAMES.search(txt):
                hits.append((node.lineno, txt[:60]))
    return sorted(set(hits))


# THE FIXTURES ARE ASSEMBLED, never written out, and the first version of this arm got
# that wrong: spelling the shell file's name in its own test data made THIS FILE a
# referencing file, and the inventory arm below reported four files where the tree has
# three. A scan flagging its own fixtures is the third time that shape has cost me a
# measurement today, so the name is built from fragments no single string contains.
_LIB = "test/" + "lib" + ".sh"


def test_the_shell_reference_detector_sees_code_and_not_prose(expect):
    """The premise, and the distinction the whole arm below rests on."""
    expect.num(len(_shell_reference_sites(
        'def f():\n    """This drives ' + _LIB + ' and says so."""\n    return 1\n')), 0,
        "a docstring naming a shell file is prose, not a reference")
    expect.num(len(_shell_reference_sites(
        'import subprocess\nsubprocess.run(["bash", "-c", ". ' + _LIB + '"])\n')), 1,
        "a string the code passes to bash is a reference")
    expect.num(len(_shell_reference_sites(
        'srcdir = "/x"\nscript = f\'. "{srcdir}/' + _LIB + '" || exit 1\'\n')), 1,
        "and so is one built with an f-string, counted ONCE rather than per piece")
    expect.num(len(_shell_reference_sites('sharedir = "/usr/share"\n')), 0,
        "a word that merely looks like a path is not a reference")


def test_the_harness_independence_inventory_is_exactly_what_the_corpus_does(expect):
    """CONTEXT.md's inventory, asserted in both directions.

    A file that starts reaching into the shell harness reddens this. A file that stops
    and is left in the declaration reddens it too -- otherwise the list becomes a
    permanent exemption that outlives the coupling it was written for, which is how
    every hand-maintained exempt list in this tree has gone wrong.
    """
    here = pathlib.Path(__file__).parent
    files = sorted(here.glob("*.py"))
    expect.at_least(len(files), 10, "premise: the scan has a corpus to read")
    found = {}
    for f in files:
        sites = _shell_reference_sites(f.read_text(encoding="utf-8"))
        if sites:
            found[f.name] = sites
    expect.text(repr(sorted(found)), repr(sorted(SHELL_REFERENCES)),
                "the files that reach into the shell harness are exactly the declared ones")
    for name in sorted(found):
        expect.at_least(len(SHELL_REFERENCES.get(name, "")), 20,
                        f"{name}'s entry says by what mechanism it reaches across")

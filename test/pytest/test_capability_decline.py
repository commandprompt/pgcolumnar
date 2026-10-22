"""A missing pyarrow declines a pytest test by name; nothing declines silently (#1215).

The pytest half of selftest parts 600 and 610, held by the PYTHON harness's own
machinery. It does not source, call or read lib.sh, capability_sweep.sh or either
part: the twins are independent, and each harness answers for itself.

The owner's rule of 2026-10-12: a missing capability may be bypassed as a named skip,
the run stays green, and no test may be silent or vacuous. On this side that is
`expect.declines` and `expect.require_module`, with five ways a decline could hide
something closed off (OffgridwithJD, review of #1286):

  1 where pyarrow is supposed to be (CI, PGC_REQUIRE=pyarrow) a decline FAILS
  2 a decline is a test's only outcome: not after a check, and no check after it
  3 only an ABSENT module declines; a broken install fails
  4 a test file that imports pyarrow outside require_module is refused, in every
    import spelling, each proven seen
  5 the run's accounting line carries the declines

duckdb declines the same way, and is held to the same three of those rules here
(by name, PGC_REQUIRE, the import scan): it joined the closed list on the owner's
decision on #1341 (2026-10-21), as the second reader of an exported Parquet file.

Runs pytest inside pytest; needs no cluster and no driver.
"""

# Absent for ONE inner test, through monkeypatch so it is undone: pytester runs
# in-process, and a module-level sys.modules entry would leak into every later test.
ABSENT = "    import sys\n"


def test_a_missing_pyarrow_declines_by_name_and_the_run_stays_green(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pytester.makepyfile(ABSENT + """
    def test_inner_declines_green(expect, monkeypatch):
        monkeypatch.setitem(sys.modules, "pyarrow", None)
        pa = expect.require_module("pyarrow", name="the parquet fixture can be written")
        assert pa is None
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a declined test leaves the run green", passed=1, failed=0)
    expect.num(result.ret, 0, "and the run exits 0")
    out = result.stdout.str()
    expect.contains(out, "+ 1 declined",
                    "the accounting line carries the decline (hole 5)")
    expect.contains(out, "DECLINED  test_a_missing_pyarrow_declines_by_name_and_the_run_stays_green.py::test_inner_declines_green: pyarrow",
                    "and the decline is named, with its capability")


def test_pgc_require_turns_a_decline_into_a_failure(pytester, monkeypatch, expect):
    monkeypatch.setenv("PGC_REQUIRE", "pyarrow")
    pytester.makepyfile(ABSENT + """
    def test_inner_required_fails(expect, monkeypatch):
        monkeypatch.setitem(sys.modules, "pyarrow", None)
        expect.require_module("pyarrow", name="the parquet fixture can be written")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "with PGC_REQUIRE=pyarrow a missing pyarrow fails (hole 1)",
                    passed=0, failed=1)
    expect.contains(result.stdout.str(), "PGC_REQUIRE names pyarrow",
                    "and it fails BECAUSE pyarrow is required, not for another reason")


def test_the_declinable_capabilities_are_exactly_pyarrow_and_duckdb(expect):
    # The list is closed, so "optional" cannot grow by one call site at a time. duckdb
    # joined on OffgridwithJD's decision on #1341 (2026-10-21): the second reader of an
    # exported Parquet file, pinned and required in CI like pyarrow.
    import pgc_vacuity
    expect.text(",".join(pgc_vacuity.DECLINABLE_CAPABILITIES), "pyarrow,duckdb",
                "the declinable capabilities are pyarrow and duckdb, and nothing else")


def test_a_missing_duckdb_declines_by_name_and_the_run_stays_green(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pytester.makepyfile(ABSENT + """
    def test_inner_duckdb_declines_green(expect, monkeypatch):
        monkeypatch.setitem(sys.modules, "duckdb", None)
        db = expect.require_module("duckdb", name="the second reader counts the rows")
        assert db is None
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a test declined on duckdb leaves the run green", passed=1, failed=0)
    expect.num(result.ret, 0, "and the run with a duckdb decline exits 0")
    expect.contains(result.stdout.str(),
                    "DECLINED  test_a_missing_duckdb_declines_by_name_and_the_run_stays_green.py::test_inner_duckdb_declines_green: duckdb",
                    "and the duckdb decline is named, with its capability")


def test_pgc_require_turns_a_duckdb_decline_into_a_failure(pytester, monkeypatch, expect):
    monkeypatch.setenv("PGC_REQUIRE", "pyarrow,duckdb")
    pytester.makepyfile(ABSENT + """
    def test_inner_duckdb_required_fails(expect, monkeypatch):
        monkeypatch.setitem(sys.modules, "duckdb", None)
        expect.require_module("duckdb", name="the second reader counts the rows")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "with PGC_REQUIRE=pyarrow,duckdb a missing duckdb fails",
                    passed=0, failed=1)
    expect.contains(result.stdout.str(), "PGC_REQUIRE names duckdb",
                    "and it fails BECAUSE duckdb is required, not for another reason")


def test_an_unguarded_duckdb_import_is_refused(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    # Inside a function that never runs: the scan reads the AST, so it must find the
    # import there too, and the real duckdb is never imported. An in-process import
    # that pytester's sys.modules snapshot then removed left duckdb's C extension
    # unimportable for every later test in the session (measured: the four DuckDB
    # tests of test_parquet_export.py failed after this file, in one session).
    pytester.makepyfile(test_duck_bare="def _never_called():\n    import duckdb\n\n\n"
                                       "def test_x(expect):\n    expect.num(1, 1, 'x')\n")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.contains(result.stdout.str(), "test_duck_bare.py imports duckdb outside require_module",
                    "a file importing duckdb outside require_module is named")
    expect.num(1 if result.ret != 0 else 0, 1, "and a run carrying it is refused")


def test_a_decline_after_a_counted_check_is_refused(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pytester.makepyfile("""
    def test_half_ran(expect):
        expect.num(1, 1, "a check that ran")
        expect.declines("pyarrow", name="too late to decline")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a decline after a counted check is refused (hole 2)",
                    passed=0, failed=1)
    expect.contains(result.stdout.str(), "a decline must be the test's first outcome",
                    "and it is refused for that reason")


def test_a_check_after_a_decline_is_refused(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pytester.makepyfile("""
    def test_kept_going(expect):
        expect.declines("pyarrow", name="declined")
        expect.num(1, 1, "a check after the decline")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a check after a decline is refused (hole 2)",
                    passed=0, failed=1)
    expect.contains(result.stdout.str(), "after this test declined",
                    "and it is refused for that reason")


def test_only_a_declinable_capability_may_decline(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pytester.makepyfile("""
    def test_wrong_capability(expect):
        expect.declines("btree_gist", name="not optional")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a capability outside the closed list cannot decline",
                    passed=0, failed=1)
    expect.contains(result.stdout.str(), "is not a declinable capability",
                    "and it is refused for that reason")


def test_a_broken_install_fails_rather_than_declines(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pkg = pytester.mkpydir("pyarrow")
    (pkg / "__init__.py").write_text("raise ImportError('pyarrow built against another ABI')\n")
    pytester.syspathinsert()
    pytester.makepyfile("""
    import sys

    def test_inner_broken_install_fails(expect, monkeypatch):
        # Drop any pyarrow already imported in this process, so the broken stub is
        # what the import finds.
        for k in [k for k in sys.modules if k == "pyarrow" or k.startswith("pyarrow.")]:
            monkeypatch.delitem(sys.modules, k)
        expect.require_module("pyarrow", name="the parquet fixture can be written")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "an ImportError from inside an installed pyarrow fails (hole 3)",
                    passed=0, failed=1)
    expect.contains(result.stdout.str(), "pyarrow built against another ABI",
                    "and the failure is the broken install's own error, not a decline")


def test_a_missing_dependency_of_pyarrow_fails_rather_than_declines(pytester, monkeypatch, expect):
    # An installed pyarrow that cannot import one of ITS dependencies raises
    # ModuleNotFoundError too, but naming the dependency. Declining on that would
    # green a broken install (hole 3); only a missing pyarrow itself declines.
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pkg = pytester.mkpydir("pyarrow")
    (pkg / "__init__.py").write_text("import numpy_is_not_here\n")
    pytester.syspathinsert()
    pytester.makepyfile("""
    import sys

    def test_inner_missing_dependency_fails(expect, monkeypatch):
        for k in [k for k in sys.modules if k == "pyarrow" or k.startswith("pyarrow.")]:
            monkeypatch.delitem(sys.modules, k)
        expect.require_module("pyarrow", name="the parquet fixture can be written")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a pyarrow missing its own dependency fails (hole 3)",
                    passed=0, failed=1)
    expect.contains(result.stdout.str(), "No module named 'numpy_is_not_here'",
                    "and the failure names the missing dependency, not a decline")


SPELLINGS = {
    "test_spell_dunder.py": "m = __import__('pyarrow')\n",
    "test_spell_import.py": "import pyarrow\n",
    "test_spell_import_sub.py": "import pyarrow.parquet as pq\n",
    "test_spell_from.py": "from pyarrow import table\n",
    "test_spell_from_sub.py": "from pyarrow.parquet import write_table\n",
    "test_spell_importlib.py": "import importlib\nm = importlib.import_module('pyarrow.parquet')\n",
}


def test_every_unguarded_import_spelling_is_refused(pytester, monkeypatch, expect):
    # The caller's PGC_REQUIRE must not decide this test (CI exports it).
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    for fname, body in SPELLINGS.items():
        pytester.makepyfile(**{fname[:-3]: body + "\ndef test_x(expect):\n    expect.num(1, 1, 'x')\n"})
    pytester.makepyfile(test_guarded="""
    def test_guarded(expect):
        expect.require_module("pyarrow", name="guarded")
""")
    result = pytester.runpytest("-p", "pgc_vacuity")
    out = result.stdout.str()
    seen = [f for f in SPELLINGS if f"{f[:-3]}.py imports pyarrow outside require_module" in out]
    # Not a premise (#1339): the scan is the layer under test, so a change to it
    # reddens this -- with importlib.import_module dropped from the spellings it
    # recognises, five of six were found.
    expect.num(len(seen), len(SPELLINGS),
               "the scan finds each of the six import spellings (hole 4)")
    expect.num(1 if result.ret != 0 else 0, 1, "and a run carrying them is refused")
    expect.num(out.count("test_guarded.py imports pyarrow"), 0,
               "while a file that goes through require_module is not named")


def test_an_unguarded_import_is_refused_under_xdist_too(pytester, monkeypatch, expect):
    """The refusal reaches the controller when workers do the collecting (#1305).

    Under `-n`, pytest_collect_file runs in the workers, so the sites they found
    never reached the controller, which prints the summary and sets the exit status:
    measured by @OffgridwithJD, the serial run said REFUSED and exited 1, the `-n 2`
    run said `2 passed` and exited 0. A subprocess, because xdist's workers are.
    """
    import pathlib
    monkeypatch.delenv("PGC_REQUIRE", raising=False)
    pkg = pytester.mkpydir("pyarrow")
    (pkg / "parquet.py").write_text("")
    here = pathlib.Path(__file__).resolve().parent
    monkeypatch.setenv("PYTHONPATH", f"{pytester.path}:{here}")
    pytester.makepyfile(test_bad="import pyarrow.parquet as pq\n\ndef test_x(expect):\n    expect.num(1, 1, 'x')\n",
                        test_ok="def test_y(expect):\n    expect.num(1, 1, 'y')\n")
    serial = pytester.runpytest_subprocess("-p", "pgc_vacuity", "-p", "no:cacheprovider")
    xdist = pytester.runpytest_subprocess("-p", "pgc_vacuity", "-p", "no:cacheprovider", "-n", "2")
    expect.num(1 if serial.ret != 0 and "REFUSED  test_bad.py" in serial.stdout.str() else 0, 1,
               "the serial run refuses the unguarded import")
    out = xdist.stdout.str()
    expect.num(out.count("REFUSED  test_bad.py imports pyarrow outside require_module"), 1,
               "under -n 2 the controller names the unguarded import, once")
    expect.num(1 if xdist.ret != 0 else 0, 1, "and the -n 2 run is refused, not green")

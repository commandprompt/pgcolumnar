"""The object-store module's packaging and loader (#393): the pytest twin of
`test/objstore_module.sh`, every check by the shell's name (#1341).

The module is a separate, non-preloaded library, so its entry point must be absent from
pgcolumnar.so and present in pgcolumnar_objstore.so (nm, as a pair, so a missing nm
cannot pass the negative half). A remote URL is refused for what it lacks, never as a
missing file; a remote glob is listed remotely, never matched against the local
filesystem; a local path, read through read_parquet, still reads. Then the installed
module is moved: present, absent and broken must be three different reports, each the
same twice in one session.

EACH REFUSAL IS PINNED, SQLSTATE AND MESSAGE. The shell took any of seven phrases, one
of them "is not supported", so a module that stopped handling s3:// or gs:// passed; and
its local-path check counted a columnar table, which never reaches read_parquet's
dispatch (#1341). An https:// read reaches the allow-list only when the module links
OpenSSL; without it the scheme is unhandled, by design, and `ldd` decides which.

THE INSTALLED MODULE IS SHARED, so this file must run alone, as the shell runner keeps
the shell suite in `runs_alone`. A pristine copy is taken first and put back in the
fixture's finally. The session's postmaster inherits this process's environment, so an
ambient AWS_* would turn the remote refusals into connection attempts: those tests then
decline rather than red.
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import psycopg
import pytest

from pgc_vacuity import query_error

AWS_VARS = ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION")
Q = "SELECT * FROM pgcolumnar.read_parquet('az://bucket/key.parquet') AS (a int)"


def _errors(dsn, *sqls):
    """-> the full text of each statement's error, in one session; a statement that
    succeeded reads as a query_error naming that, never as text that could match."""
    out = []
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for sql in sqls:
                try:
                    conn.execute(sql)
                    out.append(query_error(f"no error from {sql}"))
                except psycopg.Error as exc:
                    if exc.sqlstate is None:
                        raise
                    out.append(str(exc))
    except psycopg.Error as exc:
        return [query_error(f"connection failed: {exc}")] * len(sqls)
    return out


def _refusal(dsn, sql):
    """-> "SQLSTATE: primary message" and the full error text, or a query_error."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        if exc.sqlstate is None:
            v = query_error(str(exc))
            return v, v
        return f"{exc.sqlstate}: {exc.diag.message_primary}", str(exc)
    v = query_error(f"no error from {sql}")
    return v, v


def _lines(text, pattern):
    """grep -c: the lines of text matching pattern."""
    return sum(1 for l in text.splitlines() if re.search(pattern, l))


def _valid(path):
    """A module is valid when nm finds the entry point: the shell's stash_is_debris."""
    n = _nm_count(path, " T pgcolumnar_objstore_init") if path.exists() else 0
    return isinstance(n, int) and n >= 1


def _nm_count(path, pattern):
    try:
        r = subprocess.run(["nm", "-D", "--defined-only", str(path)], capture_output=True, text=True)
    except OSError as exc:
        return query_error(f"nm did not run: {exc}")
    if r.returncode != 0:
        return query_error(f"nm failed on {path}: {r.stderr.strip()}")
    return _lines(r.stdout, pattern)


def _https_refusal(mod):
    try:
        r = subprocess.run(["ldd", str(mod)], capture_output=True, text=True)
    except OSError as exc:
        return query_error(f"ldd did not run: {exc}")
    if "libssl" in r.stdout:
        return '42501: columnar: endpoint "host:443" is not in pgcolumnar.objstore_allowed_endpoints'
    return '0A000: columnar: reading "https://host/key.parquet" is not supported'


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return query_error(f"{path} is not there")


@pytest.fixture(scope="module")
def osm(pgc_cluster, server_dir):
    dsn = pgc_cluster.dsn()
    lib = pathlib.Path(pgc_cluster.libdir)
    mod = lib / "pgcolumnar_objstore.so"
    probe, away = mod.with_name(mod.name + ".probe"), mod.with_name(mod.name + ".away")
    r = {"ambient": [v for v in AWS_VARS if os.environ.get(v)], "unrecoverable": "none"}

    # The shell's start guard: a stash an interrupted run left is debris beside a valid
    # module (removed), or the only surviving copy (put back); one that is not a module
    # either leaves nothing to measure. objstore_stash_recovery drives the shell's own.
    for stash in (away, probe):
        if not stash.exists():
            continue
        if _valid(mod):
            stash.unlink()
            continue
        mod.unlink(missing_ok=True)
        stash.rename(mod)
        if not _valid(mod):
            r["unrecoverable"] = f"restored {stash} to {mod}, but that is not a module either"
            r["movable"] = False
            r["unmovable"] = r["unrecoverable"]
            return r
    r["main installed"] = (lib / "pgcolumnar.so").is_file()
    r["module installed"] = mod.is_file()
    r["main nm"] = _nm_count(lib / "pgcolumnar.so", "pgcolumnar_objstore_init")
    r["module nm"] = _nm_count(mod, " T pgcolumnar_objstore_init")

    want = {
        "s3://bucket/key.parquet":
            '28000: columnar: reading "s3://bucket/key.parquet" requires AWS_ENDPOINT_URL in the server environment',
        "gs://bucket/key.parquet":
            '28000: columnar: reading "gs://bucket/key.parquet" requires AWS_ACCESS_KEY_ID in the server environment',
        "https://host/key.parquet": _https_refusal(mod),
    }
    for url, w in want.items():
        got, text = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{url}') AS (a int)")
        r[url] = (got, w, _lines(text, "No such file or directory"))
    for pat in ("s3://bucket/a*.parquet", "s3://bucket/a?.parquet", "s3://bucket/a[0-9].parquet"):
        got, text = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{pat}') AS (a int)")
        # the URL the listing's refusal names is the listing's, not the pattern
        m = re.fullmatch(r'(28000): columnar: reading "s3:[^"]*" (requires AWS_ENDPOINT_URL) .*', got)
        r[pat] = (" ".join(m.groups()) if m else got,
                  _lines(text, "no files match pattern|matched no regular files|No such file or directory"))

    # a local file written by the server, read back through read_parquet
    lp = server_dir / "lp.parquet"
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("DROP TABLE IF EXISTS objstore_module_lp")
            conn.execute("CREATE TABLE objstore_module_lp (a int) USING pgcolumnar")
            conn.execute("INSERT INTO objstore_module_lp VALUES (1), (2), (3)")
            conn.execute(f"SELECT pgcolumnar.export_parquet('objstore_module_lp', '{lp}')")
            r["local"] = conn.execute(
                f"SELECT count(*) FROM pgcolumnar.read_parquet('{lp}') AS (a int) WHERE a IN (1, 2, 3)"
            ).fetchone()[0]
            conn.execute("DROP TABLE objstore_module_lp")
    except psycopg.Error as exc:
        r["local"] = query_error(str(exc))
    _, text = _refusal(dsn, "SELECT * FROM pgcolumnar.read_parquet('nonexistent-dir/x.parquet') AS (a int)")
    r["relative"] = _lines(text, "No such file or directory|could not open")

    # ---- everything below moves the installed module ----
    r["movable"] = False
    if not mod.is_file():
        return r
    fd, safe = tempfile.mkstemp(prefix="pgc-objstore-safe.")
    os.close(fd)
    safe = pathlib.Path(safe)
    shutil.copy2(mod, safe)
    try:
        try:
            mod.rename(probe)
            probe.rename(mod)
            r["movable"] = True
        except OSError as exc:
            r["unmovable"] = f"cannot move {mod}: {exc}"
            return r
        r["present"] = "\n".join(_errors(dsn, Q, Q))

        mod.rename(away)
        r["absent state"] = "present" if mod.exists() else "absent"
        r["absent"] = "\n".join(_errors(dsn, Q, Q))
        away.rename(mod)
        r["back"] = mod.exists()

        mod.rename(away)
        mod.write_bytes(b"not a shared object")
        r["corrupt size"] = _size(mod)
        r["broken"] = "\n".join(_errors(dsn, Q, Q))
        mod.unlink()
        away.rename(mod)
        r["restored size"] = _size(mod)
    finally:
        probe.unlink(missing_ok=True)
        away.unlink(missing_ok=True)
        if not (mod.is_file() and mod.read_bytes() == safe.read_bytes()):
            shutil.copy2(safe, mod)
        safe.unlink()
    return r


def _remote(osm, expect):
    """True when the remote refusals cannot be measured here, declared as such."""
    if osm["ambient"]:
        expect.cannot_run("UNMET_PRECONDITION",
                          f"the postmaster inherits {', '.join(osm['ambient'])}, so a remote read "
                          "is attempted rather than refused")
        return True
    return False


def _moved(osm, expect):
    """True when the module could not be moved, declared under the shell's skip name."""
    if osm["movable"]:
        return False
    expect.cannot_run("UNMET_PRECONDITION", osm.get("unmovable", "no module installed to move"),
                      name="the absent and broken module paths")
    return True


def _yes(b):
    return "yes" if b else "no"


def test_a_restored_stash_is_a_module(osm, expect):
    """The shell records this only when it fails; here it is read every run."""
    expect.text(osm["unrecoverable"], "none", "a stash restored over a missing module is a module")


def test_the_main_library_is_installed(osm, expect):
    expect.text(_yes(osm["main installed"]), "yes", "the main library is installed")


def test_the_module_is_installed_beside_it(osm, expect):
    expect.text(_yes(osm["module installed"]), "yes",
                "the object-store module is installed BESIDE it, not inside it")


def test_the_entry_point_is_not_in_the_preloaded_library(osm, expect):
    expect.num(osm["main nm"], 0, "the module's entry point is NOT inside the preloaded library")


def test_the_entry_point_is_defined_in_the_module(osm, expect):
    expect.num(osm["module nm"], 1, "positive control: it IS defined in the module, so nm really looked")


def test_an_s3_url_reports_an_object_storage_error(osm, expect):
    scheme, url = "s3", "s3://bucket/key.parquet"
    if _remote(osm, expect):
        return
    got, want, _ = osm[url]
    expect.text(got, want, f"a {scheme} URL reports an object-storage error, not a missing file")


def test_an_s3_url_is_not_a_missing_file(osm, expect):
    scheme, url = "s3", "s3://bucket/key.parquet"
    expect.num(osm[url][2], 0, f"and the {scheme} URL is NOT reported as a missing file")


def test_a_gs_url_reports_an_object_storage_error(osm, expect):
    scheme, url = "gs", "gs://bucket/key.parquet"
    if _remote(osm, expect):
        return
    got, want, _ = osm[url]
    expect.text(got, want, f"a {scheme} URL reports an object-storage error, not a missing file")


def test_a_gs_url_is_not_a_missing_file(osm, expect):
    scheme, url = "gs", "gs://bucket/key.parquet"
    expect.num(osm[url][2], 0, f"and the {scheme} URL is NOT reported as a missing file")


def test_an_https_url_reports_an_object_storage_error(osm, expect):
    scheme, url = "https", "https://host/key.parquet"
    got, want, _ = osm[url]
    expect.text(got, want, f"a {scheme} URL reports an object-storage error, not a missing file")


def test_an_https_url_is_not_a_missing_file(osm, expect):
    scheme, url = "https", "https://host/key.parquet"
    expect.num(osm[url][2], 0, f"and the {scheme} URL is NOT reported as a missing file")


def test_a_star_glob_is_handled_remotely(osm, expect):
    meta = "*"
    if _remote(osm, expect):
        return
    expect.text(osm[f"s3://bucket/a{meta}.parquet"][0], "28000 requires AWS_ENDPOINT_URL",
                f"a remote glob ({meta}) is handled remotely (an object-storage error, not a local one)")


def test_a_star_glob_is_not_a_local_miss(osm, expect):
    meta = "*"
    expect.num(osm[f"s3://bucket/a{meta}.parquet"][1], 0,
               f"and the {meta} glob is NOT reported as a local filesystem miss")


def test_a_question_glob_is_handled_remotely(osm, expect):
    meta = "?"
    if _remote(osm, expect):
        return
    expect.text(osm[f"s3://bucket/a{meta}.parquet"][0], "28000 requires AWS_ENDPOINT_URL",
                f"a remote glob ({meta}) is handled remotely (an object-storage error, not a local one)")


def test_a_question_glob_is_not_a_local_miss(osm, expect):
    meta = "?"
    expect.num(osm[f"s3://bucket/a{meta}.parquet"][1], 0,
               f"and the {meta} glob is NOT reported as a local filesystem miss")


def test_a_bracket_glob_is_handled_remotely(osm, expect):
    meta = "[0-9]"
    if _remote(osm, expect):
        return
    expect.text(osm[f"s3://bucket/a{meta}.parquet"][0], "28000 requires AWS_ENDPOINT_URL",
                f"a remote glob ({meta}) is handled remotely (an object-storage error, not a local one)")


def test_a_bracket_glob_is_not_a_local_miss(osm, expect):
    meta = "[0-9]"
    expect.num(osm[f"s3://bucket/a{meta}.parquet"][1], 0,
               f"and the {meta} glob is NOT reported as a local filesystem miss")


def test_a_local_path_still_works(osm, expect):
    expect.num(osm["local"], 3, "a local path still works")


def test_a_relative_path_is_not_a_url(osm, expect):
    expect.num(osm["relative"], 1, "a relative path is not mistaken for a URL")


# ---- present, absent and broken: three different reports ----

def test_present_both_reads_report_an_unhandled_scheme(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(_lines(osm["present"], "is not supported"), 2,
               "with the module present, both reads report an unhandled scheme")


def test_present_neither_claims_the_module_is_missing(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(_lines(osm["present"], "requires the object-store module"), 0,
               "and neither claims the module is missing")


def test_premise_the_module_was_absent(osm, expect):
    if _moved(osm, expect):
        return
    expect.text(osm["absent state"], "absent", "premise: the module really was absent while those two reads ran")


def test_premise_the_module_is_back(osm, expect):
    if _moved(osm, expect):
        return
    expect.text(_yes(osm["back"]), "yes", "premise: and the real module is back afterwards")


def test_absent_no_read_leaks_the_loader_error(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(_lines(osm["absent"], "could not access file"), 0,
               "with the module absent, neither read leaks the loader's own error")


def test_absent_both_reads_name_the_missing_module(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(_lines(osm["absent"], "requires the object-store module"), 2,
               "both reads name the missing module, identically")


def test_absent_neither_downgrades_to_an_unhandled_scheme(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(_lines(osm["absent"], "is not supported"), 0,
               "and neither downgrades it to an unhandled scheme")


def test_premise_the_module_is_corrupt(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(osm["corrupt size"], 19,
               "premise: the module really is corrupt for this run, not merely intended to be")


def test_premise_the_module_was_restored(osm, expect):
    if _moved(osm, expect):
        return
    size = osm["restored size"]
    expect.text("yes" if isinstance(size, int) and size > 1000 else f"no ({size})", "yes",
                "premise: the real module was restored afterwards")


def test_broken_is_not_an_unhandled_scheme(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(_lines(osm["broken"], "is not supported"), 0,
               "a broken module does NOT masquerade as an unhandled scheme")


def test_broken_is_not_a_module_never_installed(osm, expect):
    if _moved(osm, expect):
        return
    expect.num(_lines(osm["broken"], "requires the object-store module"), 0,
               "nor as a module that was never installed")


def test_broken_keeps_the_loader_reason(osm, expect):
    if _moved(osm, expect):
        return
    n = _lines(osm["broken"], "(?i)could not load library")
    expect.text("yes" if n >= 1 else f"no ({n} lines)", "yes", "and the loader's own reason survives")

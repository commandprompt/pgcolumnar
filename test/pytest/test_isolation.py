"""Deterministic MVCC isolation specs for the online compaction and truncation paths
(Phase F3). The pytest twin of `test/isolation.sh`, every check by the shell's name
(#1341).

pg_isolation_regress runs the seven specs in test/isolation/specs, each an exact,
repeatable interleaving with blocking detection: a DELETE racing compact_rewrite of
its group fails with a serialization failure rather than being lost; an old snapshot
keeps its rows through a concurrent delete and compaction; recluster against a
delete; and end-truncation against a reader, a writer and another truncation. Each
spec is its own check, read from the runner's line for it.

ON A PRIVATE DATABASE, which the specs run in with --use-existing, as the shell runs
them in its own. A spec the runner printed no line for is "not run", never ok. Where
pg_isolation_regress is not installed every test declines unnamed: the shell records
no check then, and its harness reports the suite SKIPPED.
"""
import os
import pathlib
import re
import subprocess

import psycopg
import pytest

DB = "pgc_isolation"
ROOT = pathlib.Path(__file__).resolve().parents[2]
SPECS = ("delete_vs_rewrite", "old_snapshot_compact", "compact_vs_reader", "recluster_vs_delete",
         "truncate_vs_reader", "truncate_vs_writer", "truncate_vs_truncate")


def _iso_binary(pg_config):
    pgxs = subprocess.run([pg_config, "--pgxs"], capture_output=True, text=True, check=True).stdout.strip()
    return pathlib.Path(os.path.dirname(pgxs)) / ".." / "test" / "isolation" / "pg_isolation_regress"


def _verdict(log, spec):
    if re.search(rf"^ok +\d+ +- +{spec}( |$)|^test {spec} +\.\.\. ok", log, re.M):
        return "ok"
    if re.search(rf"^not ok +\d+ +- +{spec}( |$)|^test {spec} +\.\.\. FAILED", log, re.M):
        return "failed"
    return "not run"


@pytest.fixture(scope="module")
def iso(pgc_cluster, tmp_path_factory):
    binary = _iso_binary(pgc_cluster.pg_config)
    if not binary.exists():
        yield None
        return
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=DB), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        out = tmp_path_factory.mktemp("iso_out")
        env = dict(os.environ, PGHOST="127.0.0.1", PGPORT=str(pgc_cluster.port), PGUSER="postgres")
        run = subprocess.run([str(binary), f"--inputdir={ROOT / 'test' / 'isolation'}", f"--outputdir={out}",
                              f"--bindir={pgc_cluster.bindir}", "--use-existing", f"--dbname={DB}", *SPECS],
                             capture_output=True, text=True, env=env, timeout=900)
        log = run.stdout + run.stderr
        print(log)
        yield {spec: _verdict(log, spec) for spec in SPECS}
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(iso, expect):
    if iso is not None:
        return False
    expect.cannot_run("MISSING_DEPENDENCY", "pg_isolation_regress is not installed beside this server's pgxs")
    return True


def test_isolation_spec_delete_vs_rewrite(iso, expect):
    if _declined(iso, expect):
        return
    expect.text(iso["delete_vs_rewrite"], "ok", "isolation spec delete_vs_rewrite")


def test_isolation_spec_old_snapshot_compact(iso, expect):
    if _declined(iso, expect):
        return
    expect.text(iso["old_snapshot_compact"], "ok", "isolation spec old_snapshot_compact")


def test_isolation_spec_compact_vs_reader(iso, expect):
    if _declined(iso, expect):
        return
    expect.text(iso["compact_vs_reader"], "ok", "isolation spec compact_vs_reader")


def test_isolation_spec_recluster_vs_delete(iso, expect):
    if _declined(iso, expect):
        return
    expect.text(iso["recluster_vs_delete"], "ok", "isolation spec recluster_vs_delete")


def test_isolation_spec_truncate_vs_reader(iso, expect):
    if _declined(iso, expect):
        return
    expect.text(iso["truncate_vs_reader"], "ok", "isolation spec truncate_vs_reader")


def test_isolation_spec_truncate_vs_writer(iso, expect):
    if _declined(iso, expect):
        return
    expect.text(iso["truncate_vs_writer"], "ok", "isolation spec truncate_vs_writer")


def test_isolation_spec_truncate_vs_truncate(iso, expect):
    if _declined(iso, expect):
        return
    expect.text(iso["truncate_vs_truncate"], "ok", "isolation spec truncate_vs_truncate")

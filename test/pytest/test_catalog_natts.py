"""Every Natts_* constant equals the width of the catalog it addresses. The pytest twin
of `test/catalog_natts.sh`, every check by the shell's name (#1341).

The constants in src/columnar_metadata.c size the stack arrays handed to
heap_modify_tuple, which iterates tupdesc->natts, so a constant smaller than its table
is a stack buffer overflow (Natts_options was left at 7 when options grew to 9, and a
RENAME COLUMN on a sorted table aborted the backend). This checks the invariant on every
major instead of waiting for a sanitizer build to see the overflow.

The widths come from the LIVE SERVER, not from the shipped .sql: the server is what
heap_modify_tuple iterates, and an upgrade script that adds a column changes the server
without changing any CREATE TABLE text. The source is read with the shell's own
patterns, and the premises pin that the constants still size arrays fed to
heap_modify_tuple. A failed query is a `query_error()`.
"""
import pathlib
import re

import psycopg
import pytest

from pgc_vacuity import query_error

REPO = pathlib.Path(__file__).resolve().parents[2]
META = REPO / "src" / "columnar_metadata.c"
DEFINE = re.compile(r"^#define Natts_([a-z_]+)[ \t]+([0-9]+)", re.M)
WIDTH = ("SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
         "JOIN pg_namespace n ON n.oid = c.relnamespace "
         "WHERE n.nspname = 'pgcolumnar' AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped")


def _table(name):
    """The catalog a constant addresses: Anum_native_storage_* address pgcolumnar.storage;
    everything else is identity. Explicit, so a renamed catalog fails loudly."""
    return {"native_storage": "storage"}.get(name, name)


def _source():
    try:
        return META.read_text()
    except OSError:
        return ""


SRC = _source()
CONSTANTS = [(m.group(1), int(m.group(2))) for m in DEFINE.finditer(SRC)]


@pytest.fixture(scope="module")
def widths(pgc_cluster):
    """-> {table: width on the live server, or a query_error()}, for every constant."""
    out = {}
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
    for name, _ in CONSTANTS:
        tbl = _table(name)
        try:
            with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
                out[tbl] = int(c.execute(WIDTH, (tbl,)).fetchone()[0])
        except psycopg.Error as exc:
            out[tbl] = query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    return out


def test_the_metadata_source_is_where_we_think_it_is(expect):
    expect.text("found" if META.is_file() else "missing", "found",
                "premise: the metadata source is where we think it is")


def test_the_source_defines_natts_constants(expect):
    expect.text("yes" if CONSTANTS else "no", "yes", "premise: the source defines Natts_* constants at all")


def test_those_constants_size_arrays(expect):
    expect.text("yes" if re.search(r"\[Natts_[a-z_]+\]", SRC) else "no", "yes",
                "premise: those constants SIZE arrays, so a wrong one is a real overflow")


def test_heap_modify_tuple_is_still_called_here(expect):
    expect.text("yes" if "heap_modify_tuple" in SRC else "no", "yes",
                "premise: heap_modify_tuple is still called here (the consumer of those arrays)")


@pytest.mark.parametrize("name,value", CONSTANTS, ids=[n for n, _ in CONSTANTS])
def test_each_constant_matches_its_catalogs_width(widths, name, value, expect):
    tbl = _table(name)
    width = widths[tbl]
    if isinstance(width, int) and width == 0:
        expect.text("absent", "present", f"premise: pgcolumnar.{tbl} exists on the server (for Natts_{name})")
    expect.num(value, width, f"Natts_{name} matches the width of pgcolumnar.{tbl}")


def test_every_constant_found_was_swept(widths, expect):
    swept = sum(1 for name, _ in CONSTANTS if _table(name) in widths)
    expect.num(swept, len(DEFINE.findall(SRC)), "premise: every constant found was swept")

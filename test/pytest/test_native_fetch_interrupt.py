"""The by-row-number fetch is cancellable (#212). The pytest twin of
`test/native_fetch_interrupt.sh`, every check by the shell's name (#1341).

pgcolumnar_fetch_row is reached once per candidate item pointer by _bt_check_unique()
during a unique INSERT, and before #212 it had no CHECK_FOR_INTERRUPTS, so a unique
INSERT whose conflict check did a lot of fetch work could not be cancelled and never
noticed postmaster death. The behavioural proof is not a stable CI assertion (see the
shell's header), so this reads the source, as the shell does: the guard is in
pgcolumnar_fetch_row, it precedes the pgcolumnar_lookup_row_group call it guards, and
that helper is where the catalog read lives.

INDEPENDENT OF THE SHELL. The shell awks and greps; this reads the file in Python and
matches with `re`. Both read the same tree.

COMMENTS ARE NOT CODE. Each body has its comments blanked, multi-line ones included,
before anything is counted: with the call written as /* CHECK_FOR_INTERRUPTS(); */ every
check passed (#1341). A blanked comment keeps its newlines, so positions in the body
still order the code.
"""
import pathlib
import re

READER = pathlib.Path(__file__).resolve().parents[2] / "src" / "columnar_reader.c"


def _body(fn):
    """fn's definition, from its signature line to the first closing brace at column 0,
    as the shell's awk takes it, with its comments blanked; empty when the file or the
    function is not there."""
    try:
        text = READER.read_text()
    except OSError:
        return ""
    m = re.search(r"^%s\(.*?^}" % re.escape(fn), text, re.M | re.S)
    if not m:
        return ""
    body = re.sub(r"/\*.*?\*/", lambda c: "\n" * c.group(0).count("\n"), m.group(0), flags=re.S)
    return re.sub(r"//[^\n]*", "", body)


def test_fetch_row_carries_an_interrupt_check(expect):
    expect.text("yes" if "CHECK_FOR_INTERRUPTS" in _body("pgcolumnar_fetch_row") else "no", "yes",
                "pgcolumnar_fetch_row carries an interrupt check")


def test_the_check_precedes_the_row_group_resolution(expect):
    body = _body("pgcolumnar_fetch_row")
    cfi, rgl = body.find("CHECK_FOR_INTERRUPTS"), body.find("pgcolumnar_lookup_row_group(")
    expect.text("yes" if 0 <= cfi < rgl else f"no (the check at offset {cfi}, the call at {rgl}; -1 is absent)", "yes",
                "the interrupt check precedes the row-group resolution")


def test_the_resolution_is_where_the_catalog_read_lives(expect):
    expect.text("yes" if "PgColumnarReadRowGroupList" in _body("pgcolumnar_lookup_row_group") else "no",
                "yes", "the row-group resolution is where the catalog read lives")

"""The decode path stays interruptible. The pytest twin of `test/decode_interrupts.sh`,
every check by the shell's name (#1341).

A column chunk's vector holds as many values as pgcolumnar.chunk_group_row_limit lets
it, and that setting is user-chosen and unbounded, so every per-value loop in a decoder
is a stretch of work whose length the user picks. Each one needs its own interrupt
check, and a cancel cannot tell them apart at any scale CI can carry (the shell's header
has the measurements), so this reads the source, as the shell does.

INDEPENDENT OF THE SHELL. The shell blanks comments with perl and walks braces in awk;
this reads the files in Python and walks them itself. Both read the same tree.

COMMENTS ARE NOT CODE. Both files have their comments blanked, multi-line ones
included, before anything is counted, each blanked comment keeping its newlines: with
decode_for's call written as /* COLUMNAR_DECODE_INTERRUPT(i); */, or the macro's own call
as /* CHECK_FOR_INTERRUPTS(); */, every check passed (#1341).

INSIDE A LOOP, NOT ONLY IN THE FUNCTION. A check counts only inside the braces of a
for, while or foreach body, or of the loop whose header the arm names. With
decode_for's check hoisted in front of its loop, decode_rle's and decode_fsst_shared's
moved below the loops they guarded, and either reader check moved in front of its loop,
every check passed (#1341).
"""
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

DECODERS = ["bitunpack", "decode_rle", "decode_for", "decode_delta", "decode_gorilla",
            "decode_dod", "decode_alp", "decode_dict", "decode_fsst_shared"]

MACRO = re.compile(r"COLUMNAR_DECODE_INTERRUPT\(")
CFI = re.compile(r"CHECK_FOR_INTERRUPTS")
HEADER = re.compile(r"(^|[^A-Za-z0-9_])(for|while|foreach)[ \t]*\(")


def _blanked(name):
    """A source file with its comments blanked, newlines kept, as lines; empty when absent."""
    try:
        text = (SRC / name).read_text()
    except OSError:
        return []
    text = re.sub(r"/\*.*?\*/", lambda c: "\n" * c.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text).split("\n")


def _range(lines, fn):
    """fn's lines, from its definition at column 0 to its closing brace at column 0."""
    start = next((i for i, l in enumerate(lines) if l.startswith(fn + "(")), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("}")), len(lines) - 1)
    return lines[start:end + 1]


def _in_loop(lines, fn, token, want=None):
    """How many lines of fn carry token inside the body of a loop (one whose header
    matches want, when given). Brace depth is walked character by character; a loop
    header whose statement ends at a ';' with no brace opens no block."""
    depth, loops, pending, tagged, parens, n = 0, [], False, False, 0, 0
    for line in _range(lines, fn):
        if HEADER.search(line) and not re.search(r"}[ \t]*while", line):
            pending, tagged, parens = True, want is None or re.search(want, line) is not None, 0
        for ch in line:
            if ch == "(":
                parens += 1
            elif ch == ")":
                parens -= 1
            elif ch == ";" and pending and parens <= 0:
                pending = False
            elif ch == "{":
                depth += 1
                loops.append(pending and tagged)
                pending = False
            elif ch == "}" and depth > 0:
                depth -= 1
                loops.pop()
        if token.search(line) and any(loops):
            n += 1
    return n


@pytest.fixture(scope="module")
def enc():
    return _blanked("columnar_encoding.c")


@pytest.fixture(scope="module")
def rd():
    return _blanked("columnar_reader.c")


def _macro(enc):
    """The macro's definition, from its #define to the first blank line, as the shell's awk takes it."""
    start = next((i for i, l in enumerate(enc) if l.startswith("#define COLUMNAR_DECODE_INTERRUPT(i)")), None)
    if start is None:
        return []
    end = next((i for i in range(start, len(enc)) if enc[i] == ""), len(enc))
    return enc[start:end + 1]


def test_the_decode_interrupt_macro_exists(enc, expect):
    expect.text(str(sum(l.startswith("#define COLUMNAR_DECODE_INTERRUPT(i)") for l in enc)), "1",
                "the decode interrupt macro exists")


def test_the_macro_calls_check_for_interrupts(enc, expect):
    expect.text(str(sum(bool(CFI.search(l)) for l in _macro(enc))), "1",
                "the macro calls CHECK_FOR_INTERRUPTS")


@pytest.mark.parametrize("f", DECODERS)
def test_a_decoder_was_found(enc, expect, f):
    expect.text("yes" if _range(enc, f) else "no", "yes", f"{f} was found")


@pytest.mark.parametrize("f", DECODERS)
def test_a_decoder_checks_inside_a_loop(enc, expect, f):
    expect.text("yes" if _in_loop(enc, f, MACRO) >= 1 else "no", "yes", f"{f} checks for interrupts")


def test_decode_rle_inner_loop_was_found(enc, expect):
    found = any(re.search(r"while \(run-- > 0", l) for l in _range(enc, "decode_rle"))
    expect.text("yes" if found else "no", "yes", "decode_rle's inner value loop was found")


def test_decode_rle_checks_in_its_value_loop(enc, expect):
    n = _in_loop(enc, "decode_rle", MACRO, r"while \(run-- > 0")
    expect.text("yes" if n >= 1 else "no", "yes", "decode_rle checks inside its value loop, not only around it")


def test_decode_fsst_shared_decode_loop_was_found(enc, expect):
    found = any(re.search(r"while \(p < end\)", l) for l in _range(enc, "decode_fsst_shared"))
    expect.text("yes" if found else "no", "yes", "decode_fsst_shared's decode loop was found")


def test_decode_fsst_shared_checks_in_its_decode_loop(enc, expect):
    n = _in_loop(enc, "decode_fsst_shared", MACRO, r"while \(p < end\)")
    expect.text("yes" if n >= 1 else "no", "yes",
                "decode_fsst_shared checks inside its decode loop, not only in the table parse")


def _stride(enc):
    m = next((re.match(r"#define COLUMNAR_DECODE_INTERRUPT_STRIDE ([0-9]+)", l) for l in enc
              if re.match(r"#define COLUMNAR_DECODE_INTERRUPT_STRIDE [0-9]+", l)), None)
    return int(m.group(1)) if m else None


def test_the_stride_is_defined(enc, expect):
    expect.text("yes" if _stride(enc) is not None else "no", "yes", "the stride is defined")


def test_the_stride_is_a_power_of_two(enc, expect):
    s = _stride(enc)
    expect.text("yes" if s is not None and s & (s - 1) == 0 else f"no (the stride is {s})", "yes",
                "the stride is a power of two")


def test_the_stride_is_not_too_large(enc, expect):
    s = _stride(enc)
    expect.text("yes" if s is not None and s <= 1048576 else f"no (the stride is {s})", "yes",
                "the stride is not so large that a check never lands")


def test_the_group_load_checks_per_column_chunk(rd, expect):
    expect.text(str(_in_loop(rd, "pgcolumnar_native_load_group", CFI, r"foreach\(lc, chunks\)")), "1",
                "the group load checks for interrupts per column chunk")


def test_the_row_loop_checks_for_interrupts(rd, expect):
    expect.text(str(_in_loop(rd, "pgcolumnar_native_next_row", CFI, r"for \(;;\)")), "1",
                "the row loop checks for interrupts")

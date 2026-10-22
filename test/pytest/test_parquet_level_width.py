"""The Parquet level-width helper is single-sourced, unsigned and bounded (#710). The
pytest twin of `test/parquet_level_width.sh`, every check by the shell's name (#1341).

pq_bits_for gives the bit width of the RLE/bit-pack level encoding. It used to exist
twice, in the writer and the reader, and to shift a signed 1, which is undefined once
the shift reaches 31. Neither is reachable from SQL, so this reads the source, as the
shell does: the shared helper in columnar_parquet_format.h exists, no local copy
survives in either .c file and both call the shared one, its shift is unsigned with no
signed `1 <<` left, and its loop is bounded below 32 shifts with a non-positive maximum
returning before any shift.

INDEPENDENT OF THE SHELL. The shell greps and awks; this reads the files in Python and
matches with `re`. Both read the same tree.

FAILS CLOSED. A count over a file that is missing, or over a body that was not found, is
0, which is the passing answer for two of these checks; each reads "no reading" instead.
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
HDR = SRC / "columnar_parquet_format.h"
WRITER = SRC / "columnar_parquet.c"
READER = SRC / "columnar_parquet_reader.c"


def _text(path):
    try:
        return path.read_text()
    except OSError:
        return None


def _body():
    """pq_bits_for's definition, from its signature line to the first closing brace at
    column 0, as the shell's awk takes it, with its comments blanked (a guard commented
    out of the code is not a guard); empty when the header or the function is not
    there."""
    text = _text(HDR) or ""
    m = re.search(r"^pq_bits_for\(int maxval\).*?^}", text, re.M | re.S)
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", m.group(0), flags=re.S)) if m else ""


def test_the_shared_helper_exists(expect):
    expect.text("yes" if _body() else "no", "yes", "the shared level-width helper exists")


def test_no_local_copy_survives(expect):
    writer, reader = _text(WRITER), _text(READER)
    if writer is None or reader is None:
        got = "no reading (a source file is missing)"
    else:
        got = str(sum(len(re.findall(r"^[a-z_]*bits_for\(int", t, re.M)) for t in (writer, reader)))
    expect.text(got, "0", "no local copy of the helper survives in the writer or the reader")


def test_both_callers_reach_the_shared_helper(expect):
    writer, reader = _text(WRITER) or "", _text(READER) or ""
    expect.text("yes" if "pq_bits_for(" in writer and "pq_bits_for(" in reader else "no", "yes",
                "and both callers reach the shared one")


def test_the_shift_is_unsigned(expect):
    expect.num(_body().count("1u <<"), 1, "the shift is unsigned")


def test_no_signed_shift_remains(expect):
    body = _body()
    got = (str(len(re.findall(r"\(1 <<|[^u] 1 <<", body))) if body else "no reading (no body)")
    expect.text(got, "0", "and no signed 1 << remains in it")


def test_the_loop_is_bounded(expect):
    expect.num(_body().count("b < 31"), 1, "the loop is bounded below 32 shifts")


def test_a_non_positive_maximum_returns_first(expect):
    expect.num(_body().count("maxval <= 0"), 1, "a non-positive maximum returns before shifting at all")

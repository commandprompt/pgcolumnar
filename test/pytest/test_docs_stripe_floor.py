"""The docs must name 1024 as the floor for `stripe_row_limit` (#1017).

A vector is a fixed 1024 values (`COLUMNAR_NATIVE_VECTOR_LENGTH`), so a row group
smaller than one vector never fills one and the chunk-shared FSST symbol table is
not built. Measured on 200,000 rows of a text column, `compression = none`:

    stripe_row_limit 1000   0 FSST tables    13,625,000   106.4% of raw
    stripe_row_limit 1200   166 tables        6,998,031    54.7% of raw

The accepted minimum is 1000, so the most aggressive legal setting is the one that
pays this. `docs/administration.md` tells a reader to LOWER this setting for
point-lookup-heavy tables, which is the path into it, so the warning has to live
beside that advice and not only in a reference table.

Public seam: the three published pages. Read independently of docs_style.sh --
this parses the pages itself rather than sharing a helper with the shell arm.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "docs" / "configuration.md"
ADMIN = ROOT / "docs" / "administration.md"
PRACTICES = ROOT / "docs" / "best-practices.md"


def _floor_line_sections(path):
    """The `##` headings under which a line names both the setting and the floor.

    ONE LINE, because a blank-line block and a three-line window are both green on
    `main`: `configuration.md`'s GUC table has no blank lines, so
    `stripe_row_limit`'s row shares a block with `chunk_group_row_limit`'s "fixed
    1024-value vectors", and those rows are adjacent. One line naming both is 0 on
    all three pages there, and it constrains the prose to state the floor in a
    sentence, which is what a warning needs.

    AND THE SECTION, because one line alone says nothing about WHERE. Reported by
    @OffgridwithJD, who moved the line out of the advice block to the end of
    `administration.md` -- 402 lines away -- and the arm still passed while claiming
    the floor was stated "beside the advice to lower the setting". Reproduced here
    before changing anything.

    A `##` heading is the boundary, not a blank line. That is what the paragraph
    reader got wrong: blank lines are absent inside a markdown table and arbitrary
    in prose, while a heading is declared.
    """
    out, heading = set(), None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
        elif "stripe_row_limit" in line and "1024" in line:
            out.add(heading)
    return out


def _sections_containing(path, needle):
    out, heading = set(), None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
        elif needle.lower() in line.lower():
            out.add(heading)
    return out


def test_configuration_states_the_floor_where_it_documents_the_setting(expect):
    expect.num(int(CONFIG.is_file()), 1, "premise: configuration.md is in the tree")
    expect.num(int(len(_floor_line_sections(CONFIG)) > 0), 1,
               "configuration.md states the 1024 floor on the setting's own line")


def test_administration_states_it_in_the_section_that_says_to_lower_it(expect):
    """`administration.md` tells a reader to LOWER this setting for point lookups.

    That is the path into the cliff, so the floor has to be in THAT section. The
    arm asserts the section and not merely the page, because the page-wide version
    passed with the two 402 lines apart.
    """
    expect.num(int(ADMIN.is_file()), 1, "premise: administration.md is in the tree")
    advice = _sections_containing(ADMIN, "lower this setting")
    expect.num(int(len(advice) > 0), 1,
               "premise: administration.md still tells a reader to lower the setting")
    floor = _floor_line_sections(ADMIN)
    expect.num(int(len(advice & floor) > 0), 1,
               "and the 1024 floor is stated in that same section")
    low = ADMIN.read_text(encoding="utf-8").lower()
    expect.num(int("fsst" in low), 1, "and names what lowering past it costs")


def test_best_practices_carries_the_floor_with_the_load_sizing_advice(expect):
    expect.num(int(PRACTICES.is_file()), 1, "premise: best-practices.md is in the tree")
    expect.num(int(len(_floor_line_sections(PRACTICES)) > 0), 1,
               "the load-sizing advice states the floor on the same line")

"""The user documentation names each storage unit one way, and defines each one.

A table is written in row groups, a row group is divided into chunk groups, and
a column within a chunk group is encoded in fixed 1024-value vectors. "Stripe" is
the older name for a row group. It survives in names that cannot change without
breaking callers (`stripe_row_limit`, `stripe_count`, the `stripeid` column), so
a reader meets it in code. In prose it is a fourth name for a thing that already
has one. The documentation audit after 1.0-alpha4 found it twice, and a benchmark
that counted 67 row groups as 67 chunk groups.

So the user pages (docs/*.md less the developer page ARCHITECTURE.md, and
README.md) never say "stripe" outside code. The user guide's "How a table is
stored" section defines the three units and names the old word, so a reader who
meets `stripe_row_limit` knows what it limits. Needs no cluster and no driver.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent

FENCE = re.compile(r"^```.*?^```", re.M | re.S)
CODE_SPAN = re.compile(r"`[^`\n]*`")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
STRIPE = re.compile(r"\bstripes?\b", re.I)
SECTION = "## How a table is stored"
STORAGE_SECTION = re.compile(rf"^{re.escape(SECTION)}\n.*?(?=^## |\Z)", re.M | re.S)


def prose(text):
    """The text a reader reads as words: no fences, code spans or link targets.

    A fence, and the storage section that names the old word on purpose, are
    blanked line for line, so a reported line is the file's line.
    """
    keep_lines = lambda m: "\n" * m.group(0).count("\n")
    blank = STORAGE_SECTION.sub(keep_lines, FENCE.sub(keep_lines, text))
    return LINK_TARGET.sub("]", CODE_SPAN.sub("", blank))


def stray_stripes(pages):
    """Space-joined `page:line` for each prose line that says stripe, sorted."""
    out = []
    for page in pages:
        for n, line in enumerate(prose(page.read_text(encoding="utf-8")).splitlines(), 1):
            if STRIPE.search(line):
                out.append(f"{page.name}:{n}")
    return " ".join(sorted(out))


def undefined(guide_text):
    """The terms the guide's storage section does not define, space-joined."""
    m = STORAGE_SECTION.search(guide_text)
    body = m.group(0) if m else ""
    terms = ("**row group**", "**chunk group**", "**vector**", "`stripe_row_limit`")
    return " ".join(t for t in terms if t not in body) or "none"


def user_pages():
    return sorted(p for p in (ROOT / "docs").glob("*.md")
                  if p.name != "ARCHITECTURE.md") + [ROOT / "README.md"]


def test_stripe_is_found_in_prose_and_not_in_code(tmp_path, expect):
    """The reader, on fixture pages: code, links and fences are not prose."""
    page = tmp_path / "p.md"
    page.write_text("Set `stripe_row_limit` and see [vacuum](ref.md#vacuum-stripe_count).\n"
                    "Quote `a stripe` and link [notes](notes.md#the-stripe).\n"
                    "```sql\nSELECT stripeid FROM t; -- a stripe\n```\nAll row groups.\n")
    expect.text(stray_stripes([page]) or "none", "none",
                "control: settings, a bare word in a code span, link targets and a fenced "
                "block are code")
    page.write_text(f"{SECTION}\n\nStripe is the old name.\n\n## Next\n\n"
                    "One Stripe here.\nTwo `x` stripes there.\nstripe_row_limit unquoted.\n")
    expect.text(stray_stripes([page]), "p.md:7 p.md:8",
                "the storage section may name the old word; after it, a capitalized and a "
                "plural use are each named at their file line, and an unquoted "
                "identifier is a different word")


def test_the_storage_section_is_found_by_its_heading(expect):
    """The definition check, on fixture text."""
    whole = (f"{SECTION}\n\nA **row group** ... a **chunk group** ... a **vector** ... "
             "`stripe_row_limit`\n\n## Next\n")
    expect.text(undefined(whole), "none", "control: every term defined in the section")
    later = f"{SECTION}\n\nA **row group**.\n\n## Next\n\n**chunk group** **vector** `stripe_row_limit`\n"
    expect.text(undefined(later), "**chunk group** **vector** `stripe_row_limit`",
                "a term defined only after the section does not count")


def test_the_user_pages_say_row_group_and_define_the_units(expect):
    """The property, on the repository's own documentation."""
    pages = user_pages()
    expect.at_least(len(pages), 10, "premise: the documentation pages were found")
    expect.text(stray_stripes(pages) or "none", "none",
                "no user page says stripe outside code")
    guide = (ROOT / "docs" / "user-guide.md").read_text(encoding="utf-8")
    expect.text(undefined(guide), "none",
                "the user guide defines row group, chunk group and vector, and names the old word")

"""The user pages say what the extension does, not which issue changed it.

An issue number is history. On a page that tells a reader how to use the
extension it is noise, and it goes stale: the documentation audit after
1.0-alpha4 found 92 on the user pages, and every one it followed was closed. Some
said an issue "tracks" work that had shipped, and one page said the Arrow
parser was not yet fuzzed when it was.

So a user page (docs/*.md and README.md, less the pages whose subject is the
project's history: benchmarks.md, testing.md, roadmap.md and the developer page
ARCHITECTURE.md) cites no issue in prose: no `#123` and no link to an issue or a
pull request. Code is not prose. CHANGELOG.md is where the history is kept.
Needs no cluster and no driver.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent

HISTORY_PAGES = ("ARCHITECTURE.md", "benchmarks.md", "roadmap.md", "testing.md")
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
CODE_SPAN = re.compile(r"`[^`\n]*`")
ISSUE = re.compile(r"(?<![\w&/#])#\d+\b|/(?:issues|pull)/\d+")


def prose(text):
    """Text outside fences and code spans, line for line.

    A heading marker needs no stripping: it is followed by a space, never a digit.
    """
    keep_lines = lambda m: "\n" * m.group(0).count("\n")
    return CODE_SPAN.sub("", FENCE.sub(keep_lines, text))


def citations(pages):
    """Space-joined `page:line` for each prose line that cites an issue, sorted."""
    out = []
    for page in pages:
        for n, line in enumerate(prose(page.read_text(encoding="utf-8")).splitlines(), 1):
            if ISSUE.search(line):
                out.append(f"{page.name}:{n}")
    return " ".join(sorted(out))


def user_pages():
    return sorted(p for p in (ROOT / "docs").glob("*.md")
                  if p.name not in HISTORY_PAGES) + [ROOT / "README.md"]


def test_a_citation_is_found_in_prose_and_not_in_code(tmp_path, expect):
    """The reader, on a fixture page."""
    page = tmp_path / "p.md"
    page.write_text("## Heading\n\nSee [the anchor](ref.md#section-2) and `SELECT 1 -- #12`.\n"
                    "```\n# 5 rows, see #77\n```\nColor &#35;42 and C# 7.\n")
    expect.text(citations([page]) or "none", "none",
                "control: a heading, an anchor, a code span, a fence, an entity and "
                "a language name are not citations")
    page.write_text("Fixed (#12).\nSee [issue 9](https://github.com/o/r/issues/9).\n"
                    "As [PR 4](https://github.com/o/r/pull/4) did.\n## Since #300\n")
    expect.text(citations([page]), "p.md:1 p.md:2 p.md:3 p.md:4",
                "a bare number, an issue link, a pull request link and a number in a "
                "heading are each named")


def test_the_user_pages_cite_no_issues(expect):
    """The property, on the repository's own documentation."""
    pages = user_pages()
    expect.at_least(len(pages), 8, "premise: the user pages were found")
    expect.text(citations(pages) or "none", "none",
                "no user page cites an issue or a pull request in prose")

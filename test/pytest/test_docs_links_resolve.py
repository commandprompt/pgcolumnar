"""Every internal link in the user documentation reaches a heading that exists.

A function's heading in docs/sql-reference.md is its full signature, so its anchor
changes whenever an argument is added. Adding `dedup` to `parallel_copy` and naming
`compact_rewrite`'s defaults left four links on three pages pointing at anchors no
heading produced any more, and every documentation gate stayed green: the style
gate reads sentences, not links.

So the links are resolved, not listed: each relative `[text](page.md#anchor)` or
`[text](#anchor)` in docs/*.md and README.md must name a file that exists and, for a
docs page, an anchor one of that page's headings produces. The anchor rule is the
site's (mkdocs.yml: pymdownx.slugs.slugify, case lower), which is also GitHub's for
these headings: lowercase, drop every character that is not a letter, digit,
underscore, hyphen or space, then each space becomes a hyphen. Needs no cluster and
no driver.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent

FENCE = re.compile(r"^```.*?^```", re.M | re.S)
HEADING = re.compile(r"^#{1,6} +(.+?)\s*#*\s*$", re.M)
LINK = re.compile(r"\]\(([^)\s]+)\)")
INLINE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def slug(heading):
    """The site's anchor for a heading's markdown text."""
    text = INLINE_LINK.sub(r"\1", heading).strip().lower()
    return "".join(c for c in text if c.isalnum() or c in " _-").replace(" ", "-")


def _prose(path):
    return FENCE.sub("", path.read_text(encoding="utf-8"))


def broken(pages, docs):
    """Space-joined "page:target" for each internal link that does not resolve, sorted.

    A link whose file is missing is named `page:target(no file)`, and one whose
    anchor no heading produces `page:target(no heading)`.
    """
    anchors = {p.name: {slug(h) for h in HEADING.findall(_prose(p))}
               for p in docs.glob("*.md")}
    out = []
    for page in pages:
        for target in LINK.findall(_prose(page)):
            if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
                continue
            name, _, anchor = target.partition("#")
            dest = (page.parent / name) if name else page
            if not dest.exists():
                out.append(f"{page.name}:{target}(no file)")
            elif (anchor and dest.suffix == ".md" and dest.parent.resolve() == docs.resolve()
                  and anchor not in anchors.get(dest.name, set())):
                out.append(f"{page.name}:{target}(no heading)")
    return " ".join(sorted(out))


def test_the_anchor_rule_is_the_sites(expect):
    """The rule on headings whose anchors the site is known to produce."""
    got = " | ".join(slug(h) for h in (
        "pgcolumnar.parallel_copy(target regclass, workers int DEFAULT NULL) returns bigint",
        "Skip fact-table work under a star-schema join",
        "`pgcolumnar.compression` and the encoding cascade",
        "Read [Parquet](sql-reference.md) files"))
    expect.text(got, "pgcolumnarparallel_copytarget-regclass-workers-int-default-null-returns-bigint"
                " | skip-fact-table-work-under-a-star-schema-join"
                " | pgcolumnarcompression-and-the-encoding-cascade"
                " | read-parquet-files",
                "punctuation and backticks dropped, underscores and hyphens kept, each space a "
                "hyphen, a link in a heading read as its text")


def test_a_link_that_reaches_no_heading_or_no_file_is_named(tmp_path, expect):
    """The reader, on fixture pages: each kind of break named, and a good link not."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ref.md").write_text("# Reference\n\n## f(a int, b int) returns int\n\n"
                                 "```\n## not a heading\n```\n")
    guide = docs / "guide.md"
    guide.write_text("# Guide\n\n## Start here\n\n"
                     "[ok](ref.md#fa-int-b-int-returns-int) [self](#start-here) "
                     "[web](https://example.org/#x) [mail](mailto:a@b.c)\n")
    expect.text(broken([guide], docs) or "none", "none",
                "control: links that resolve, and external ones, are not named")
    guide.write_text("[old](ref.md#fa-int-returns-int) [gone](missing.md) "
                     "[fenced](ref.md#not-a-heading) [self](#nowhere)\n")
    expect.text(broken([guide], docs),
                "guide.md:#nowhere(no heading) guide.md:missing.md(no file) "
                "guide.md:ref.md#fa-int-returns-int(no heading) "
                "guide.md:ref.md#not-a-heading(no heading)",
                "a stale signature anchor, a missing file, a heading inside a code fence and "
                "a missing same-page anchor are each named")


def test_every_internal_link_in_the_documentation_resolves(expect):
    """The property, on the repository's own documentation."""
    docs = ROOT / "docs"
    pages = sorted(docs.glob("*.md")) + [ROOT / "README.md"]
    expect.at_least(len(pages), 10, "premise: the documentation pages were found")
    expect.text(broken(pages, docs) or "none", "none",
                "every relative link names a file that exists and a heading that page has")

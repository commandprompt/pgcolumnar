"""docs/sql-reference.md opens with a contents list that matches its sections.

The SQL reference is the longest user page, over 2,300 lines in one flat run of
function headings. Rollups sat under "Import and export", `row_tid` between two
rollup functions, and the Iceberg readers under "Reading external Parquet". The
documentation audit after 1.0-alpha4 regrouped it into sections and put a
contents list at the top.

A hand-written contents list is the kind of claim that rots: a section is added
or renamed and the list still reads well. So the list is checked against the
page. Before the first `## ` heading, the page has one bullet list whose entries
are exactly the `## ` headings, in page order, each linking to the anchor that
heading produces (the site's rule, as in test_docs_links_resolve). Needs no
cluster and no driver.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent

FENCE = re.compile(r"^```.*?^```", re.M | re.S)
ENTRY = re.compile(r"^- \[([^\]]+)\]\(#([^)]+)\)$")


def slug(heading):
    text = heading.strip().lower()
    return "".join(c for c in text if c.isalnum() or c in " _-").replace(" ", "-")


def contents_drift(text):
    """`none`, or what differs between the contents list and the sections."""
    text = FENCE.sub("", text)
    head, sep, _ = text.partition("\n## ")
    if not sep:
        return "no sections"
    sections = re.findall(r"^## +(.+?)\s*$", text, re.M)
    items = [line for line in head.splitlines() if line.startswith("- ")]
    if not items:
        return "no contents list"
    out = []
    listed = []
    for item in items:
        m = ENTRY.match(item)
        if not m:
            out.append(f"not a link: {item}")
            continue
        title, anchor = m.groups()
        listed.append(title)
        if anchor != slug(title):
            out.append(f"wrong anchor: {title} -> #{anchor}")
    if listed != sections:
        out.append(f"listed {listed} but the sections are {sections}")
    return "; ".join(out) or "none"


def test_drift_is_named(expect):
    """The reader, on fixture pages: a control, then each kind of drift."""
    good = ("# Ref\n\nIntro.\n\n- [Table management](#table-management)\n"
            "- [Reading Apache Iceberg](#reading-apache-iceberg)\n\n"
            "## Table management\n\n```\n## not a section\n```\n\n### f()\n\n"
            "## Reading Apache Iceberg\n")
    expect.text(contents_drift(good), "none",
                "control: two sections listed in order; a fenced line is not a section")
    expect.text(contents_drift(good.replace("- [Reading Apache Iceberg](#reading-apache-iceberg)\n", "")),
                "listed ['Table management'] but the sections are "
                "['Table management', 'Reading Apache Iceberg']", "a missing section is named")
    expect.text(contents_drift(good.replace("(#table-management)", "(#tables)")),
                "wrong anchor: Table management -> #tables", "a wrong anchor is named")
    swapped = good.replace("- [Table management](#table-management)\n", "").replace(
        "(#reading-apache-iceberg)\n", "(#reading-apache-iceberg)\n- [Table management](#table-management)\n")
    expect.text(contents_drift(swapped),
                "listed ['Reading Apache Iceberg', 'Table management'] but the sections are "
                "['Table management', 'Reading Apache Iceberg']", "the wrong order is named")
    expect.text(contents_drift("# Ref\n\nIntro.\n\n## A\n"), "no contents list",
                "a page with no list is named")


def test_the_sql_reference_contents_match_its_sections(expect):
    """The property, on the repository's own SQL reference."""
    text = (ROOT / "docs" / "sql-reference.md").read_text(encoding="utf-8")
    expect.at_least(len(re.findall(r"^## ", FENCE.sub("", text), re.M)), 10,
                    "premise: the page is grouped into sections")
    expect.text(contents_drift(text), "none",
                "the contents list names every section, in order, with its anchor")

"""docs/ARCHITECTURE.md's module map names every source file and every catalog.

The map is how a new developer finds the code, and nothing held it to the tree.
By 1.0-alpha5 it named none of eleven source files in src/ (parallel copy and
export, the row lock, the extent, the sketch, the curve keys, index maintenance
for executor-less inserts, the export sink, and the Thrift, FlatBuffers and
Parquet-codec readers), nor the object-store module's own source. Seven
catalogs in the install script were absent too, rollups among them.

So the map is checked against the tree, not against a list:
- every .c file in src/ and objstore/ is named in a heading under `## Module map`;
- every file a map heading names exists, so a rename cannot leave a stale entry;
- every `CREATE TABLE pgcolumnar.<name>` in the default version's install script
  is named, in backticks, somewhere in the document.

Needs no cluster and no driver.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent

FILE_NAME = re.compile(r"\b([a-z_]+\.[ch])\b")


def map_headings(text):
    """The `###` headings between `## Module map` and the next `## ` heading."""
    m = re.search(r"^## Module map\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return re.findall(r"^### +(.+)$", m.group(1), re.M) if m else []


def catalogs(sql):
    return sorted(set(re.findall(r"^CREATE TABLE pgcolumnar\.([a-z_]+)", sql, re.M)))


def gaps(doc, sources, sql):
    """Space-joined, sorted: `unmapped:<file>`, `stale:<file>`, `catalog:<name>`."""
    named = {f for h in map_headings(doc) for f in FILE_NAME.findall(h)}
    present = {p.name for p in sources}
    out = [f"unmapped:{f}" for f in present if f.endswith(".c") and f not in named]
    out += [f"stale:{f}" for f in named if f not in present]
    out += [f"catalog:{c}" for c in catalogs(sql) if f"`{c}`" not in doc
            and f"`pgcolumnar.{c}`" not in doc]
    return " ".join(sorted(out))


def _install_script():
    control = (ROOT / "pgcolumnar.control").read_text(encoding="utf-8")
    version = re.search(r"^default_version = '([^']+)'", control, re.M).group(1)
    return (ROOT / f"pgcolumnar--{version}.sql").read_text(encoding="utf-8")


def test_each_kind_of_gap_is_named(tmp_path, expect):
    """The reader, on a fixture tree: a control that passes, then each gap."""
    for name in ("a.c", "b.c", "b.h"):
        (tmp_path / name).write_text("")
    sources = sorted(tmp_path.iterdir())
    sql = "CREATE TABLE pgcolumnar.alpha (\n);\nCREATE TABLE pgcolumnar.beta (\n);\n"
    doc = ("# Arch\n\n`alpha` here.\n\n## Module map\n\n### a.c and b.c\nText "
           "about `pgcolumnar.beta`.\n\n## Data flow\n\n### c.c\n")
    expect.text(gaps(doc, sources, sql) or "none", "none",
                "control: two files in one heading, a header not required, a catalog "
                "named with its schema, and a heading after the map not read")
    doc = ("# Arch\n\nalpha, unquoted.\n\n## Module map\n\n### a.c\nb.c only in prose.\n\n"
           "### gone.c\n\n## Data flow\n\n### b.c\n")
    expect.text(gaps(doc, sources, sql),
                "catalog:alpha catalog:beta stale:gone.c unmapped:b.c",
                "a file named only in prose or after the map, a heading for a file that "
                "is gone, and catalogs not named in backticks are each reported")


def test_the_module_map_matches_the_tree(expect):
    """The property, on the repository's own map, sources and install script."""
    doc = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    sources = sorted((ROOT / "src").glob("*.[ch]")) + sorted((ROOT / "objstore").glob("*.[ch]"))
    sql = _install_script()
    expect.at_least(len(sources), 30, "premise: the source files were found")
    expect.at_least(len(catalogs(sql)), 10, "premise: the catalogs were found")
    expect.at_least(len(map_headings(doc)), 20, "premise: the module map was found")
    expect.text(gaps(doc, sources, sql) or "none", "none",
                "every source file has a map entry, every entry a file, every catalog a mention")

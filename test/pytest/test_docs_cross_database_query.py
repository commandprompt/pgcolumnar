"""No documented query asks another database what extensions it has (#1288).

pg_extension is a per-database catalog. A statement that reads pg_database and tests
pg_extension in the same breath answers for the CURRENT database only, so it lists every
connectable database or none. docs/installation.md shipped exactly that query as the way
to find the databases that need `ALTER EXTENSION pgcolumnar UPDATE`.

The pytest half of the docs_style.sh arm of the same name, written independently: its
own parser for the fenced SQL and shell blocks, split into statements on ';', matched
case-insensitively. Prose that explains the rule cannot trip it, because only fenced code is
read.

Needs the repository only, not a cluster or the driver.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS = sorted((REPO / "docs").glob("*.md")) + [REPO / "README.md"]
# Every fence a query can be written in: SQL under any of its tags, and shell blocks, where
# the same statement arrives as `psql -c "..."`. Only ```sql was read, so the query passed
# in a ```sh block, under ```postgresql, or spelled PG_EXTENSION (#1339).
FENCE = re.compile(r"^[ \t]*```(?:sql|psql|pgsql|postgresql|postgres|sh|bash|shell|console)[ \t]*\n(.*?)^[ \t]*```",
                   re.S | re.M | re.I)


def _statements():
    out = []
    for path in DOCS:
        for block in FENCE.findall(path.read_text()):
            out.extend(s.strip() for s in block.split(";") if s.strip())
    return out


def test_no_documented_query_asks_another_database_for_its_extensions(expect):
    stmts = _statements()
    expect.at_least(sum(1 for s in stmts if re.search(r"\bSELECT\b", s)), 10,
                    "premise: the scan read SQL statements from the documents")
    bad = [s for s in stmts
           if re.search(r"\bpg_database\b", s, re.I) and re.search(r"\bpg_extension\b", s, re.I)]
    expect.num(len(bad), 0,
               "no documented query tests pg_extension while listing databases")

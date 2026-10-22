"""Public docs must name the layout that makes a join runtime filter a no-op (#752).

The skip measurement is already in native_join_runtime_filter: 19 of 20 groups
removed when the keys are local in the stripe, 0 of 20 when they cycle. The
how-to page named the GUC and not that discriminator. A star-schema fact table
that is not clustered on the join key still holds every key in every group, so
sideways information passing cannot skip.
"""

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
HOWTO = ROOT / "docs" / "how-to.md"
PRACTICES = ROOT / "docs" / "best-practices.md"


# A sentence that tells the reader to cluster ON the join key: "cluster", then within the
# same sentence "on the join key". The two words anywhere in a section are not that: a
# section can name clustering by time and mention a join key in passing (#1339).
CLUSTER_ON_KEY = re.compile(r"\bcluster\w*\b[^.]*?\bon (?:the |its |a )?join key", re.I)


def _read(path):
    """The page as a reader sees it: HTML comments are not shown, so they do not count."""
    return re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)


def _section_after(path, heading):
    text = _read(path)
    start = text.find(heading)
    if start < 0:
        return ""
    rest = text[start + len(heading):]
    nxt = rest.find("\n## ")
    if nxt < 0:
        return rest
    return rest[:nxt]


def test_how_to_names_join_key_clustering_for_the_runtime_filter(expect):
    """The star-schema how-to must say when group skip is a no-op.

    Public seam: docs/how-to.md. Independently of docs_style.sh, this reads the
    section as a reader sees it (HTML comments removed) and requires a sentence that
    says to cluster ON the join key: "cluster" in one sentence and "join key" in another
    passed before (#1339). The file and heading checks are not premises: a docs change
    reddens them (#1236).
    """
    expect.num(int(HOWTO.is_file()), 1, "how-to.md is in the tree")
    section = _section_after(HOWTO, "## Skip fact-table work under a star-schema join")
    expect.at_least(len(section), 1, "the star-schema join heading is present")
    low = section.lower()
    expect.contains(low, "cluster", "the runtime-filter how-to names clustering")
    expect.at_least(len(CLUSTER_ON_KEY.findall(section)), 1,
                    "and it names the join key as the clustering column")


def test_best_practices_names_join_key_clustering_for_a_fact_table(expect):
    """Layout guidance must cover the star-schema join key, not only timestamps.

    Public seam: docs/best-practices.md. The skipping section told readers to
    cluster on the column they filter by ranges. A join key is not a range.
    """
    expect.num(int(PRACTICES.is_file()), 1, "best-practices.md is in the tree")
    text = _read(PRACTICES)
    expect.at_least(len(CLUSTER_ON_KEY.findall(text)), 1,
                    "best-practices names clustering on the join key")

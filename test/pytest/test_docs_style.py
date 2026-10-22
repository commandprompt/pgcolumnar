"""The measurable plain-language and documentation-fact rules (#291). The pytest twin of
`test/docs_style.sh`, every check by the shell's name (#1341).

Asserted over the repository's published pages, read as files and never through a
server: the plain-language checker passes on every user-facing page and examined them;
the roadmap is reachable; no page carries a merge conflict marker; the pg_partman how-to
keeps its heading; CHANGELOG.md carries no em or en dash; the join-key and 1024-floor
advice sit where a reader needs them; every place a version is written (the VERSION
citations, the badge, its alt text, the latest-published claim, the upgrade chain and
META.json) agrees with the tree; every install script ships; CHANGELOG.md's union merge
driver and its CONTEXT.md instructions are in place; a released changelog section holds
what its tag shipped, plus only what test/changelog_post_tag.txt records; and no
documented query tests pg_extension while listing databases.

WHAT A READER SEES. Every arm asking whether a page SAYS something reads the page with
its HTML comments removed, and the nav check ignores YAML comments. A sentence kept only
in `<!-- -->` or an entry commented out with `#` is not on the published page, and both
used to pass in the shell (#1341 port, measured on main).

INDEPENDENT OF THE SHELL. The shell greps, awks and seds; this reads the files with
Python's `re` and `pathlib`. Both read the same tree, so they agree on a correct one and
disagree only when one of them reads it wrong. The plain-language checker and `git` are
the tools under test in both, so both call them.

NO DATABASE. This file is in NO_CLUSTER and runs in the guards job, which fetches the
release tags so the four changelog arms run there. In a checkout without them those
arms record UNMET_PRECONDITION by name. The shell prints a note for each instead,
because the nightly coverage job runs it from a tagless checkout and counts any
unrunnable suite as failed.
"""
import collections
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import io

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
NONE = "none"
UNREAD = "no reading"


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _rendered(path):
    """-> the page as a reader sees it: its text without HTML comments, or None."""
    text = _read(path)
    return None if text is None else re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _git(*args):
    """-> (returncode, stdout bytes) of git in the tree under test."""
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    except OSError:
        return 1, b""
    return r.returncode, r.stdout


def _corpus():
    """CHANGELOG.md, README.md and every file under docs/, the set the shell's grep -r reads."""
    files = [ROOT / "CHANGELOG.md", ROOT / "README.md"]
    files += [p for p in DOCS.rglob("*") if p.is_file()]
    return files


def _containing(needle_re):
    """-> sorted paths among the corpus whose raw text matches the regex."""
    out = []
    for p in _corpus():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(needle_re, text):
            out.append(p)
    return sorted(out, key=lambda p: str(p).encode())


def _listed(items):
    return " ".join(items) if items else NONE


# ---- plain language -------------------------------------------------------------------

def _plain():
    docs = sorted(DOCS.glob("*.md")) + [ROOT / "README.md"]
    try:
        r = subprocess.run([sys.executable, str(ROOT / "test" / "plain_language_check.py"),
                            *map(str, docs)], capture_output=True, text=True)
    except OSError as exc:
        return f"checker did not run: {exc}", 0
    out = r.stdout + r.stderr
    return str(r.returncode), sum(1 for line in out.splitlines() if line.startswith("  ok"))


# ---- the 1024 floor and the join key ----------------------------------------------------

def _floor_line(path):
    text = _rendered(path)
    if text is None:
        return f"{path.name} unreadable"
    return "yes" if any("stripe_row_limit" in l and "1024" in l for l in text.split("\n")) else "no"


def _floor_same_section(path, advice):
    text = _rendered(path)
    if text is None:
        return f"{path.name} unreadable"
    heading, said, floor = "", set(), set()
    for line in text.split("\n"):
        if line.startswith("## "):
            heading = line[3:]
        if advice.lower() in line.lower():
            said.add(heading)
        if "stripe_row_limit" in line and "1024" in line:
            floor.add(heading)
    return "yes" if said & floor else "no"


def _section(text, heading):
    start = text.find(heading)
    if start < 0:
        return None
    rest = text[start + len(heading):]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


# ---- versions -------------------------------------------------------------------------

def _pgxn(ver):
    """VERSION's 1.0-alpha5 -> PGXN's 1.0.0-alpha.5."""
    num, _, suffix = ver.partition("-")
    parts = num.split(".")
    while len(parts) < 3:
        parts.append("0")
    out = ".".join(parts)
    if suffix:
        out += "-" + re.sub(r"(\d+)$", r".\1", suffix)
    return out


def _sentences(text):
    """One sentence per item, with a column-0 list marker kept out of the split."""
    lines = [re.sub(r"^(\d+)\. ", r"\1.@LM@", line) for line in text.split("\n")]
    joined = " ".join(lines)
    return [s.replace("@LM@", " ") for s in joined.replace(". ", ".\n").split("\n")]


CLAIM = re.compile(r"previously (shipped|published) version")


def _upgrade_claim(path):
    text = _rendered(path) or ""
    claims = [s for s in _sentences(text) if CLAIM.search(s)]
    first = claims[0] if claims else ""
    sources = sorted(set(re.findall(r"`(1\.0-[a-z0-9]+)`", re.sub(r"reaches `[^`]*`", "", first))))
    target = sorted(set(re.findall(r"reaches `([^`]*)`", first)))
    return len(claims), " ".join(sources), " ".join(target)


def _upgrade_walk():
    scripts = sorted(ROOT.glob("pgcolumnar--*--*.sql"))
    pairs = collections.defaultdict(list)
    for p in scripts:
        a, b = p.name[len("pgcolumnar--"):-len(".sql")].split("--", 1)
        pairs[a].append(b)
    starts = sorted(pairs)
    control = _read(ROOT / "pgcolumnar.control") or ""
    m = re.search(r"^\s*default_version\s*=\s*'([^']*)'", control, re.M)
    defver = m.group(1) if m else ""
    branched = [f"{s}={len(pairs[s])}" for s in starts if len(pairs[s]) != 1]
    reach = []
    for s in starts:
        cur, steps = s, 0
        while cur != defver and steps < 50 and pairs.get(cur):
            cur, steps = sorted(pairs[cur])[0], steps + 1
        if cur == defver:
            reach.append(s)
    return starts, defver, branched, reach


# ---- the changelog's released sections -----------------------------------------------

DATED = re.compile(r"^## \[([^\]]+)\] - ", re.M)


def _entries(text, version):
    out, inside = [], False
    for line in text.split("\n"):
        if line.startswith("## ["):
            m = re.match(r"^## \[([^\]]+)\] - ", line)
            inside = bool(m) and m.group(1) == version
            continue
        if inside and line.startswith("- "):
            out.append(line)
    return sorted(out, key=str.encode)


def _changelog(allow_rows):
    """-> (why it cannot compare or "", versions seen, nosec, bad, lost, used rows)."""
    text = _read(ROOT / "CHANGELOG.md") or ""
    versions = DATED.findall(text)
    rc, _ = _git("rev-parse", "--git-dir")
    if rc != 0:
        return "no git repository in the tree under test", versions, [], [], [], []
    _, tags = _git("tag", "-l", "v1.0-alpha*")
    if not tags.strip():
        return "no release tags in this checkout", versions, [], [], [], []
    nosec, bad, lost, used = [], [], [], []
    for v in versions:
        rc, shipped = _git("show", f"v{v}:CHANGELOG.md")
        tagged = _entries(shipped.decode("utf-8", "replace"), v) if rc == 0 else []
        if not tagged:
            nosec.append(v)
            continue
        now = _entries(text, v)
        extra = sorted((collections.Counter(now) - collections.Counter(tagged)).elements(), key=str.encode)
        gone = sorted((collections.Counter(tagged) - collections.Counter(now)).elements(), key=str.encode)
        allowed = sorted((r[1] for r in allow_rows if r[0] == v and r[1]), key=str.encode)
        used += allowed
        if extra != allowed:
            bad.append(f"{v}: added {extra} recorded {allowed}")
        if gone:
            lost.append(f"{v}: {gone}")
    return "", versions, nosec, bad, lost, used


def _allow_rows():
    text = _read(ROOT / "test" / "changelog_post_tag.txt")
    if text is None:
        return None
    rows = []
    for line in text.split("\n"):
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        rows.append((parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
    return rows


# ---- the readings ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def ds():
    r = {}
    rc, n = _plain()
    r["plain rc"], r["examined"] = rc, ("yes" if n >= 10 else f"no (examined {n})")

    nav = _read(ROOT / "mkdocs.yml")
    if nav is None:
        r["nav"] = "mkdocs.yml unreadable"
    else:
        live = [re.sub(r"(^|\s)#.*", "", line) for line in nav.split("\n")]
        r["nav"] = "yes" if any("roadmap.md" in line for line in live) else "no"
    r["roadmap page"] = "yes" if (DOCS / "roadmap.md").is_file() else "no"

    marked = []
    for p in [q for q in DOCS.rglob("*") if q.is_file()] + sorted(ROOT.glob("*.md")):
        try:
            if re.search(r"^(<<<<<<< |>>>>>>> )", p.read_text(errors="replace"), re.M):
                marked.append(p.name)
        except OSError:
            pass
    r["conflicts"] = _listed(sorted(marked))

    howto = _rendered(DOCS / "how-to.md")
    if howto is None:
        r["partman"] = "how-to.md unreadable"
        r["rf"] = "how-to.md unreadable"
    else:
        lines = howto.split("\n")
        first = ""
        if "## Manage time partitions with pg_partman" in lines:
            i = lines.index("## Manage time partitions with pg_partman")
            first = next((l for l in lines[i + 1:] if l.strip()), "")
        r["partman"] = "1" if first.startswith("pg_partman can create and drop columnar partitions") else "0"
        sec = _section(howto, "## Skip fact-table work under a star-schema join")
        r["rf"] = "missing-heading" if sec is None else (
            "yes" if "cluster" in sec.lower() and "join key" in sec.lower() else "no")
    cl = _read(ROOT / "CHANGELOG.md")
    r["cl dashes"] = "CHANGELOG.md unreadable" if cl is None else str(
        sum(1 for line in cl.split("\n") if "—" in line or "–" in line))
    bp = _rendered(DOCS / "best-practices.md")
    r["bp join"] = "best-practices.md unreadable" if bp is None else ("yes" if "join key" in bp.lower() else "no")
    r["floor cfg"] = _floor_line(DOCS / "configuration.md")
    r["floor admin"] = _floor_same_section(DOCS / "administration.md", "Lower this setting")
    r["floor bp"] = _floor_line(DOCS / "best-practices.md")

    ver = "".join((_read(ROOT / "VERSION") or "").split())
    r["ver"] = "yes" if ver else "no"
    verdocs = _containing(r"recorded in `VERSION`")
    r["verdocs"] = "yes" if verdocs else "no"
    r["stale"] = _listed([p.name for p in verdocs
                          if f"`{ver}`, recorded in `VERSION`" not in (_rendered(p) or "")])

    badge = _read(ROOT / "badges" / "version.svg")
    r["badge"] = "yes" if badge is not None else "no"
    badge = badge or ""
    r["badge old"] = _listed(sorted(set(re.findall(r"1\.0-[a-z]+[0-9]*", badge)) - {ver}, key=str.encode))
    r["badge hits"] = "yes" if ver and any(ver in line for line in badge.split("\n")) else "no"
    readme = _read(ROOT / "README.md") or ""
    r["alt"] = "\n".join(re.findall(r'alt="Version ([^"]*)"', readme)) or "no alt text"

    form = re.compile(r"latest published pre-release is `(v[^`]*)`|`(v[^`]*)` is the latest published pre-release")
    pubdocs = _containing(r"latest published pre-release")
    r["pubdocs"] = "yes" if pubdocs else "no"
    pubvers, unparsed = set(), []
    for p in pubdocs:
        found = form.findall(_rendered(p) or "")
        if not found:
            unparsed.append(p.name)
        pubvers |= {a or b for a, b in found}
    r["pub unparsed"] = _listed(unparsed)
    vers = sorted(pubvers, key=str.encode)
    r["pub got"] = vers[0] if len(vers) == 1 else "DISAGREE:" + ",".join(vers)
    r["pub want"] = vers[0] if vers else "a version some document names"

    starts, defver, branched, reach = _upgrade_walk()
    r["upg scripts"] = "yes" if starts else "no"
    r["defver"] = "yes" if defver else "no"
    r["upg branch"] = _listed(branched)
    r["upg reach"] = "yes" if reach else "no"
    upgdocs = _containing(r"previously (shipped|published) version")
    r["upg docs"] = "yes" if upgdocs else "no"
    unread, src_got, src_want, dst_got, dst_want = [], [], [], [], []
    for p in upgdocs:
        n, sources, target = _upgrade_claim(p)
        if n != 1:
            unread.append(f"{p.name}:claims={n}")
        if not sources:
            unread.append(f"{p.name}:no-starting-versions")
        if not target:
            unread.append(f"{p.name}:no-destination")
        src_got.append(f"{p.name}=[{sources}]")
        src_want.append(f"{p.name}=[{' '.join(reach)}]")
        dst_got.append(f"{p.name}=[{target}]")
        dst_want.append(f"{p.name}=[{defver}]")
    r["upg unread"] = _listed(unread)
    r["upg src"] = (" ".join(src_got) or "no document", " ".join(src_want) or "a document")
    r["upg dst"] = (" ".join(dst_got) or "no document", " ".join(dst_want) or "a document")

    try:
        meta = json.loads(_read(ROOT / "META.json") or "")
        r["meta"] = "yes"
    except ValueError:
        meta, r["meta"] = {}, "no"
    pgxn = _pgxn(ver) if ver else ""
    r["pgxn"] = "yes" if pgxn else "no"
    r["pgxn want"] = pgxn or "a PGXN version"
    r["meta version"] = str(meta.get("version", "no version in META.json"))
    prov = meta.get("provides", {}).get("pgcolumnar", {})
    r["meta provides"] = str(prov.get("version", "no provides version in META.json"))
    meta_file = prov.get("file", "")
    r["meta file"] = "yes" if meta_file else "no"
    rc, tar = _git("archive", "HEAD")
    if rc != 0:
        shipped = None
    else:
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            shipped = set(t.getnames())
    r["meta shipped"] = UNREAD if shipped is None else str(int(meta_file in shipped))
    ondisk = sorted((p.name for p in ROOT.glob("pgcolumnar--*.sql")), key=str.encode)
    r["ondisk"] = "yes" if ondisk else "no"
    r["all shipped"] = UNREAD if shipped is None else (
        _listed([n for n in ondisk if n not in shipped]).replace(NONE, "all shipped"))

    attrs = _read(ROOT / ".gitattributes") or ""
    r["union"] = str(len(re.findall(r"^CHANGELOG\.md\s+merge=union$", attrs, re.M)))

    rows = _allow_rows()
    why, versions, nosec, bad, lost, used = _changelog(rows or [])
    r["cl why"] = why
    r["cl nosec"], r["cl bad"], r["cl lost"] = _listed(nosec), _listed(bad), _listed(lost)
    r["cl seen"] = "yes" if len(versions) >= 3 else "no"
    r["allow file"] = "yes" if rows is not None else "no"
    r["no reason"] = _listed([f"{a} {b}" for a, b, c in (rows or []) if not c])
    declared = sorted((b for a, b, c in (rows or []) if b), key=str.encode)
    stale = sorted((collections.Counter(declared) - collections.Counter(set(used))).elements(), key=str.encode)
    r["stale rows"] = _listed(stale)

    ctx = _read(ROOT / "CONTEXT.md") or ""
    ctx_lines = ctx.split("\n")
    r["ctx badge"] = str(sum("CONFLICTING badge on CHANGELOG.md means rebase locally" in l for l in ctx_lines))
    r["ctx cmd"] = str(sum("git rebase origin/main" in l for l in ctx_lines))
    r["ctx click"] = str(sum("do not click" in l.lower() for l in ctx_lines))
    r["ctx why"] = str(sum("merging **into**" in l for l in ctx_lines))

    stmts = []
    for p in sorted(DOCS.glob("*.md")) + [ROOT / "README.md"]:
        inside, buf = False, []
        for line in (_read(p) or "").split("\n"):
            if not inside and re.match(r"^\s*```sql", line):
                inside = True
                continue
            if inside and re.match(r"^\s*```", line):
                inside = False
                buf.append(";")
                continue
            if inside:
                buf.append(line + " ")
        stmts += "".join(buf).split(";")
    n_select = sum("SELECT" in s for s in stmts)
    r["sql read"] = "yes" if n_select >= 10 else f"no ({n_select} SELECT statements, want at least 10)"
    r["xdb"] = str(sum("pg_database" in s and "pg_extension" in s for s in stmts))
    return r


# ---- one check per test, every name the shell's -----------------------------------------

def test_every_user_facing_document_meets_the_plain_language_rules(ds, expect):
    expect.text(ds["plain rc"], "0", "every user-facing document meets the measurable plain-language rules")


def test_the_plain_language_checker_examined_the_documents(ds, expect):
    expect.text(ds["examined"], "yes", "and it actually examined the documents")


def test_the_roadmap_is_in_the_documentation_nav(ds, expect):
    expect.text(ds["nav"], "yes", "the roadmap is in the documentation nav")


def test_the_roadmap_nav_entry_points_at_a_page(ds, expect):
    expect.text(ds["roadmap page"], "yes", "and the page that nav entry points at exists")


def test_no_document_carries_a_merge_conflict_marker(ds, expect):
    expect.text(ds["conflicts"], NONE, "no document carries a merge conflict marker")


def test_the_how_to_keeps_its_pg_partman_heading(ds, expect):
    expect.text(ds["partman"], "1", "the how-to keeps its pg_partman section under its own heading")


def test_changelog_carries_no_em_or_en_dash(ds, expect):
    expect.text(ds["cl dashes"], "0", "CHANGELOG.md carries no em or en dash")


def test_how_to_names_clustering_on_the_join_key(ds, expect):
    expect.text(ds["rf"], "yes", "how-to names clustering on the join key for the runtime filter")


def test_best_practices_names_clustering_on_the_join_key(ds, expect):
    expect.text(ds["bp join"], "yes", "best-practices names clustering on the join key")


def test_configuration_states_the_1024_floor(ds, expect):
    expect.text(ds["floor cfg"], "yes", "configuration.md states the 1024 floor where it documents the setting")


def test_administration_states_the_floor_beside_its_advice(ds, expect):
    expect.text(ds["floor admin"], "yes",
                "administration.md states it in the section that says to lower the setting")


def test_best_practices_carries_the_floor(ds, expect):
    expect.text(ds["floor bp"], "yes", "best-practices.md carries it with the load-sizing advice")


def test_the_version_file_has_a_version(ds, expect):
    expect.text(ds["ver"], "yes", "premise: the VERSION file has a version to compare against")


def test_some_document_cites_the_version_file(ds, expect):
    expect.text(ds["verdocs"], "yes", "premise: at least one document cites the VERSION file")


def test_every_version_citation_quotes_version(ds, expect):
    expect.text(ds["stale"], NONE, "every document citing VERSION quotes the version VERSION holds")


def test_the_version_badge_is_present(ds, expect):
    expect.text(ds["badge"], "yes", "premise: the version badge is present")


def test_the_badge_names_no_other_version(ds, expect):
    expect.text(ds["badge old"], NONE, "the version badge names no version other than VERSION's")


def test_the_badge_names_the_version(ds, expect):
    expect.text(ds["badge hits"], "yes", "premise: and it names VERSION's version at all")


def test_the_readme_badge_alt_text_names_the_version(ds, expect):
    expect.text(ds["alt"], "".join((ROOT / "VERSION").read_text().split()) or "a version",
                "README's badge alt text names the version VERSION holds")


def test_some_document_names_the_latest_published_pre_release(ds, expect):
    expect.text(ds["pubdocs"], "yes", "premise: at least one document names the latest published pre-release")


def test_every_published_claim_is_in_a_readable_form(ds, expect):
    expect.text(ds["pub unparsed"], NONE,
                "every document making the claim states it in a form this rule can read")


def test_every_document_names_the_same_published_pre_release(ds, expect):
    expect.text(ds["pub got"], ds["pub want"], "every document names the same latest published pre-release")


def test_the_tree_ships_upgrade_scripts(ds, expect):
    expect.text(ds["upg scripts"], "yes", "premise: the tree ships upgrade scripts to derive the chain from")


def test_the_control_file_names_a_default_version(ds, expect):
    expect.text(ds["defver"], "yes", "premise: the control file names a default_version to arrive at")


def test_each_version_starts_one_upgrade_script(ds, expect):
    expect.text(ds["upg branch"], NONE,
                "each shipped version starts exactly one upgrade script, so the chain is a walk")


def test_some_version_reaches_the_default(ds, expect):
    expect.text(ds["upg reach"], "yes",
                "premise: some shipped version reaches default_version, so the walk found a chain")


def test_some_document_states_the_upgrade_chain(ds, expect):
    expect.text(ds["upg docs"], "yes", "premise: at least one document states the upgrade chain")


def test_every_upgrade_chain_claim_is_readable(ds, expect):
    expect.text(ds["upg unread"], NONE,
                "every document stating the upgrade chain states it in a form this rule can read")


def test_every_document_names_the_versions_that_reach_the_default(ds, expect):
    got, want = ds["upg src"]
    expect.text(got, want, "every such document names the versions that reach default_version in one update")


def test_every_document_names_the_default_as_the_destination(ds, expect):
    got, want = ds["upg dst"]
    expect.text(got, want, "every such document names default_version as the version one UPDATE reaches")


def test_meta_json_parses(ds, expect):
    expect.text(ds["meta"], "yes", "premise: META.json is present and parses")


def test_the_pgxn_form_was_derived(ds, expect):
    expect.text(ds["pgxn"], "yes", "premise: the PGXN form was derived from VERSION, not empty")


def test_meta_json_version_is_the_version(ds, expect):
    expect.text(ds["meta version"], ds["pgxn want"], "META.json version is the version VERSION holds, in PGXN form")


def test_meta_json_provides_version_is_the_version(ds, expect):
    expect.text(ds["meta provides"], ds["pgxn want"],
                "META.json provides.pgcolumnar.version is the version VERSION holds, in PGXN form")


def test_meta_json_names_a_base_script(ds, expect):
    expect.text(ds["meta file"], "yes", "premise: META.json names a base install script")


def test_the_meta_json_script_ships(ds, expect):
    expect.text(ds["meta shipped"], "1", "the script META.json names is in the published distribution")


def test_the_tree_has_install_scripts(ds, expect):
    expect.text(ds["ondisk"], "yes", "premise: the tree has install scripts to check")


def test_every_install_script_ships(ds, expect):
    expect.text(ds["all shipped"], "all shipped",
                "every pgcolumnar--*.sql in the tree is in the published distribution")


def test_changelog_has_a_union_merge_driver(ds, expect):
    expect.text(ds["union"], "1", "CHANGELOG.md has a union merge driver, so two entries do not conflict")


def test_every_dated_section_exists_at_its_tag(ds, expect):
    if ds["cl why"]:
        expect.cannot_run("UNMET_PRECONDITION", ds["cl why"],
                          name="every dated changelog section has its own section at its release tag")
        return
    expect.text(ds["cl nosec"], NONE, "every dated changelog section has its own section at its release tag")


def test_every_dated_section_holds_only_what_shipped_or_is_recorded(ds, expect):
    if ds["cl why"]:
        expect.cannot_run("UNMET_PRECONDITION", ds["cl why"],
                          name="every dated changelog section holds what its tag shipped, plus only what is recorded")
        return
    expect.text(ds["cl bad"], NONE,
                "every dated changelog section holds what its tag shipped, plus only what is recorded")


def test_every_dated_section_keeps_what_shipped(ds, expect):
    if ds["cl why"]:
        expect.cannot_run("UNMET_PRECONDITION", ds["cl why"],
                          name="every dated changelog section still holds every entry its tag shipped")
        return
    expect.text(ds["cl lost"], NONE, "every dated changelog section still holds every entry its tag shipped")


def test_the_changelog_sweep_found_sections(ds, expect):
    expect.text(ds["cl seen"], "yes", "premise: the changelog sweep found dated release sections")


def test_the_post_tag_allowance_file_is_readable(ds, expect):
    expect.text(ds["allow file"], "yes", "premise: the post-tag allowance file is present and readable")


def test_every_post_tag_entry_carries_a_reason(ds, expect):
    expect.text(ds["no reason"], NONE, "every recorded post-tag entry carries a reason")


def test_no_post_tag_entry_is_stale(ds, expect):
    if ds["cl why"]:
        expect.cannot_run("UNMET_PRECONDITION", ds["cl why"],
                          name="no recorded post-tag entry is stale, so the allowance cannot widen silently")
        return
    expect.text(ds["stale rows"], NONE,
                "no recorded post-tag entry is stale, so the allowance cannot widen silently")


def test_changelog_still_has_the_union_driver_for_the_instructions(ds, expect):
    expect.text(ds["union"], "1", "and CHANGELOG.md still has the union merge driver those instructions are about")


def test_context_explains_a_conflicting_badge(ds, expect):
    expect.text(ds["ctx badge"], "1", "and CONTEXT.md tells a contributor what a CONFLICTING badge means")


def test_context_names_the_rebase_command(ds, expect):
    expect.text(ds["ctx cmd"], "1", "and names the command rather than only the problem")


def test_context_warns_against_the_update_button(ds, expect):
    expect.text(ds["ctx click"], "1", "and warns against the button that reintroduces the conflict")


def test_context_says_why_rebasing_works(ds, expect):
    expect.text(ds["ctx why"], "1", "and says WHY rebasing works where merging does not")


def test_the_scan_read_sql_statements(ds, expect):
    expect.text(ds["sql read"], "yes", "premise: the scan read SQL statements from the documents")


def test_no_documented_query_tests_pg_extension_across_databases(ds, expect):
    expect.text(ds["xdb"], "0", "no documented query tests pg_extension while listing databases")

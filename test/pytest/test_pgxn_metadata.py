"""`META.json` is the published distribution metadata and nothing ever read it.

It hardcodes the version TWICE and names the base install script by filename. No
suite, no Makefile rule and no CI step consumed it, so it went stale for the whole
alpha4 cycle:

    version                    1.0.0-alpha.3     while VERSION said 1.0-alpha4
    provides.pgcolumnar.file   pgcolumnar--1.0-alpha3.sql

THE FILENAME IS THE HALF THAT MATTERS. `pgcolumnar--1.0-alpha3.sql` does not
exist. This repository opens each cycle by RENAMING the base script to the new
version, so the published metadata named a file the distribution does not
contain. The two version strings were only the visible symptom.

Public seam: the `META.json` file itself and `git archive`, which is what PGXN
receives. Read independently of `docs_style.sh` -- this parses the file and runs
`git archive` itself rather than sharing a helper with the shell arm.

WHY `git archive` AND NOT `os.path.exists`. The question is whether the
DISTRIBUTION contains the file, not whether the working tree does. An
`export-ignore` attribute can drop a file that is plainly present on disk, and
this repository has been bitten by an export-ignore rule before.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
META = ROOT / "META.json"
VERSION = ROOT / "VERSION"


def _pgxn_form(version_text):
    """`1.0-alpha4` -> `1.0.0-alpha.4`, PGXN's three-part semver.

    DERIVED, not hardcoded, so a future `1.0-beta1` or `1.0` is covered without
    editing this file. The numeric part is padded to three components and the
    suffix gets a dot before its trailing digits.
    """
    head, _, suffix = version_text.partition("-")
    parts = head.split(".")
    while len(parts) < 3:
        parts.append("0")
    head = ".".join(parts)
    if not suffix:
        return head
    # Split the suffix by hand rather than with a backreference. The regex form
    # was written three times and escaped wrongly twice, because it passes through
    # a shell heredoc on the way into this file; a loop cannot be mis-escaped.
    digits = len(suffix)
    while digits > 0 and suffix[digits - 1].isdigit():
        digits -= 1
    if digits == len(suffix):
        return f"{head}-{suffix}"
    return f"{head}-{suffix[:digits]}.{suffix[digits:]}"


def _archive_members():
    """What `git archive HEAD` would ship, which is what PGXN receives."""
    out = subprocess.run(
        ["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    listing = subprocess.run(
        ["tar", "-t"], input=out, capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")
    return [line for line in listing.splitlines() if line]


def test_the_version_derivation_maps_the_forms_this_project_uses(expect):
    """The transform, before anything relies on it.

    A derivation is a claim too. If `_pgxn_form` were wrong, every arm below would
    compare against a wrong expectation and could pass or fail for reasons that
    have nothing to do with `META.json`.
    """
    for given, want in (
        ("1.0-alpha4", "1.0.0-alpha.4"),
        ("1.0-beta1", "1.0.0-beta.1"),
        ("1.0", "1.0.0"),
        ("2.1-rc2", "2.1.0-rc.2"),
    ):
        expect.text(_pgxn_form(given), want, f"{given} maps to PGXN {want}")


def test_meta_json_states_the_version_the_VERSION_file_holds(expect):
    """Both version fields, against the file that is the source of truth.

    The two forms differ by convention and that is not a bug: PGXN requires
    three-part semver, so `1.0-alpha4` publishes as `1.0.0-alpha.4`.
    """
    version_text = VERSION.read_text(encoding="utf-8").strip()
    expect.text(
        "present" if version_text else "empty",
        "present",
        "premise: the VERSION file has a version to compare against",
    )
    want = _pgxn_form(version_text)

    meta = json.loads(META.read_text(encoding="utf-8"))
    expect.text(meta["version"], want, "META.json version is VERSION in PGXN form")
    expect.text(
        meta["provides"]["pgcolumnar"]["version"],
        want,
        "META.json provides.pgcolumnar.version is VERSION in PGXN form",
    )


def test_the_script_meta_json_names_is_in_the_published_distribution(expect):
    """The defect that actually shipped.

    `provides.pgcolumnar.file` named a script that had been renamed away at
    cycle-open, so the metadata pointed at a file PGXN would not receive.
    """
    meta = json.loads(META.read_text(encoding="utf-8"))
    named = meta["provides"]["pgcolumnar"]["file"]
    expect.text(
        "named" if named else "absent",
        "named",
        "premise: META.json names a base install script",
    )

    members = _archive_members()
    expect.text(
        "non-empty" if len(members) > 50 else f"only {len(members)}",
        "non-empty",
        "premise: git archive produced a distribution to look in",
    )
    expect.num(
        members.count(named), 1, f"the distribution contains {named} exactly once"
    )

    # SHIPPED IS NOT ENOUGH: IT MUST BE THE BASE SCRIPT (#1339). `provides.file` is
    # the script a fresh install runs, and every upgrade script ships too, so
    # naming `pgcolumnar--1.0-alpha4--1.0-alpha5.sql` passed the arm above.
    version_text = VERSION.read_text(encoding="utf-8").strip()
    expect.text(
        named, f"pgcolumnar--{version_text}.sql",
        "META.json names the base install script of the version VERSION holds",
    )


def test_every_sql_file_meta_json_could_name_is_shipped(expect):
    """The neighbouring failure: a script present in the tree and not in the archive.

    `provides.file` is one filename, so the arm above cannot see an `export-ignore`
    that drops a DIFFERENT install script -- the upgrade paths. An upgrade script
    missing from the distribution breaks `ALTER EXTENSION ... UPDATE` for a user
    who installed from PGXN, and nothing else here would notice.
    """
    on_disk = sorted(p.name for p in ROOT.glob("pgcolumnar--*.sql"))
    expect.text(
        "found" if on_disk else "none",
        "found",
        "premise: the tree has install scripts to check",
    )
    members = set(_archive_members())
    missing = [n for n in on_disk if n not in members]
    # A SENTINEL RATHER THAN AN EMPTY EXPECTATION. `expect.text(x, "")` is refused
    # as vacuous, and rightly: anything empty satisfies it, including a comparison
    # whose left side failed to produce anything at all.
    expect.text(
        "all shipped" if not missing else ", ".join(missing),
        "all shipped",
        "every pgcolumnar--*.sql in the tree is in the archive",
    )

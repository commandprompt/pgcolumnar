"""The index access methods `docs/features.md` claims must actually work.

The page said "builds btree and hash indexes over a columnar table" for as long
as it existed. GiST and SP-GiST build and answer queries too, and a reader of
that sentence concluded the opposite, which is why this pair was written. A range
column's overlap and containment queries have no other fast path: those operators
never prune chunk groups on a scan (`docs/limitations.md`, "Which predicates
prune"), so the index is the whole story.

IT READS THE DOCUMENT RATHER THAN CARRYING A LIST, for the reason
`doc_parallel_premise` gives about the published query: a suite holding its own
copy cannot see the page drift away from it. Adding a method to the page without
a probe here turns this red rather than publishing an untested claim.

Independent of `test/index_am_support.sh` per CONTEXT.md: the same public seams
-- the document, `CREATE INDEX`, and the two plans -- but its own cluster, its
own table name, its own row count, and the access methods extracted here with a
Python regex rather than with awk. The two agree on the property, not on the
implementation.

WHAT IS NOT CLAIMED. GIN and BRIN also build on a columnar table. They are
absent from the page and from this file on purpose: building is not the same as
being usable, and nobody has measured a plan that chooses them.
"""
import pathlib
import re

ROWS = 15_000            # own corpus size; the shell twin uses another

FEATURES = pathlib.Path(__file__).resolve().parents[2] / "docs" / "features.md"

# THE CLAIM IS WHITELISTED, NOT SCREENED FOR NEGATIONS. The first version of this
# pair refused a window containing `not|never|except|...`, which @OffgridwithJD
# broke twice: once with the edit it was written for, and then with
#
#     ...and `gist` but NOT `spgist` indexes        refused, correctly
#     ...though `spgist` cannot be chosen           PASSED
#
# because `\bnot\b` has no word boundary before the `not` inside `cannot`. Their
# own correction is the reason the fix is not another token: a denylist of
# negations is the losing game they had told me to avoid, one round at a time --
# `isn't`, `no longer`, `save for`, and finally a sentence that inverts the claim
# with no negation token in it at all.
#
# So the bullet has exactly ONE legitimate form and anything else is refused,
# including prose that is perfectly true. The refusal asks for its own sentence.
_CLAIM = re.compile(
    r"^- `CREATE INDEX` builds "
    r"(?P<names>`[a-z_]+`(?:, `[a-z_]+`)*(?: and `[a-z_]+`)?) "
    r"indexes over a columnar table\.$"
)

# The bullet, as one normalised line. The shell twin walks it with awk; this
# takes it with a regex, so neither inherits the other's blind spots while both
# read the same unit -- which the first version did NOT do, and a denial written
# past one side's stop phrase gave four names here and three there.
_BULLET = re.compile(r"^- `CREATE INDEX` builds.*?(?=^- |^\s*$)", re.S | re.M)


def _claim_line():
    m = _BULLET.search(FEATURES.read_text(encoding="utf-8"))
    return " ".join(m.group(0).split()) if m else ""


def _claim_is_readable():
    return _CLAIM.match(_claim_line()) is not None


def _claimed_ams():
    m = _CLAIM.match(_claim_line())
    return sorted(re.findall(r"`([a-z_]+)`", m.group("names"))) if m else []


# Each method's column, its operator and the probe value. A method the page names
# and this table does not cover falls through to the failing premise below.
PROBES = {
    "btree": ("id", "id = 4242"),
    "hash": ("id", "id = 4242"),
    "gist": ("span", "span && tstzrange('2020-01-01 02:00','2020-01-01 03:00')"),
    "spgist": ("span", "span @> timestamptz '2020-01-01 02:30'"),
}


def _count(cur, sql):
    cur.execute(sql)
    return int(cur.fetchone()[0])


def _load(cur):
    cur.execute("CREATE TABLE ias (id int, txt text, span tstzrange) USING pgcolumnar")
    cur.execute(
        f"""
        INSERT INTO ias
        SELECT g, 'v' || g,
               tstzrange(timestamptz '2020-01-01' + (g || ' minutes')::interval,
                         timestamptz '2020-01-01' + ((g + 60) || ' minutes')::interval)
          FROM generate_series(1, {ROWS}) g
        """
    )


def test_every_access_method_the_page_claims_builds_and_answers(pgc_conn, expect):
    """One probe per method named on the page, and the page is the list.

    THE EXTRACTOR IS A CLAIM TOO. A regex that matches nothing returns an empty
    list, and a loop over nothing passes every arm under it, so the premise below
    asserts the page named something before any result is believed.
    """
    ams = _claimed_ams()
    print(f"-- docs/features.md claims: {' '.join(ams) or '<nothing>'}")

    # THE BULLET MUST HOLD THE CLAIM AND NOTHING ELSE, asserted FIRST because
    # it explains the other: a bullet this file cannot read yields no names, so
    # the names premise fails as a consequence. pytest stops at its first failing
    # assertion, so the cause has to come first or only the consequence is seen. Everything below reads
    # names and nothing reads prose, so a qualification in the same bullet
    # inverts the page's meaning while every arm passes on the names it found.
    readable = _claim_is_readable()
    if not readable:
        print(f"-- refusing the claim bullet; it is not the one form this file reads:")
        print(f"--   {_claim_line()}")
        print("-- expected: - `CREATE INDEX` builds `a`, `b` and `c` indexes over "
              "a columnar table.")
        print("-- put anything else in its own bullet; this file reads NAMES and "
              "cannot read prose")
    expect.text(
        "the claim and nothing else" if readable else "unreadable",
        "the claim and nothing else",
        "premise: the claim bullet holds the claim and nothing else",
    )

    expect.text(
        "named" if ams else "named-nothing", "named",
        "premise: the features page names at least one index access method",
    )


    with pgc_conn.cursor() as c:
        _load(c)
        expect.num(_count(c, "SELECT count(*) FROM ias"), ROWS,
                   "premise: the fixture holds every row")

        for am in ams:
            col, pred = PROBES.get(am, (None, None))
            expect.text(
                "probed" if col else "no probe", "probed",
                f"premise: {am}, which the page claims, has a probe in this suite",
            )
            if not col:
                continue

            c.execute("DROP INDEX IF EXISTS ias_probe")
            c.execute(f"CREATE INDEX ias_probe ON ias USING {am} ({col})")
            expect.text(
                "built", "built",
                f"{am} builds an index over a columnar table",
            )

            # The scan path's answer is the oracle. Comparing against a literal
            # would pin this fixture rather than the property, and the property is
            # that the two paths agree.
            c.execute("SET enable_indexscan = off")
            c.execute("SET enable_indexonlyscan = off")
            c.execute("SET enable_bitmapscan = off")
            want = _count(c, f"SELECT count(*) FROM ias WHERE {pred}")
            c.execute("RESET enable_indexscan")
            c.execute("RESET enable_indexonlyscan")
            c.execute("RESET enable_bitmapscan")

            c.execute("SET pgcolumnar.enable_custom_scan = off")
            c.execute("SET enable_seqscan = off")
            got = _count(c, f"SELECT count(*) FROM ias WHERE {pred}")
            c.execute("RESET pgcolumnar.enable_custom_scan")
            c.execute("RESET enable_seqscan")

            # NON-EMPTY AS WELL AS EQUAL. Two paths that both return nothing
            # agree, and a predicate matching no row satisfies the arm below
            # while exercising neither path.
            expect.text(
                "matches" if want > 0 else "matches-nothing", "matches",
                f"premise: the {am} predicate matches rows at all",
            )
            expect.num(
                got, want,
                f"{am} answers its operator with the same rows the scan returns",
            )

        c.execute("DROP INDEX IF EXISTS ias_probe")

        # THE OTHER HALF OF THE PAGE'S CLAIM, and why the range methods are on
        # it. An overlap query has no scan-side pruning, so the index is not an
        # optimisation here, it is the only fast path. The plan is asserted, not
        # the timing.
        c.execute("CREATE INDEX ias_probe ON ias USING gist (span)")
        c.execute("SET pgcolumnar.enable_custom_scan = off")
        c.execute("SET enable_seqscan = off")
        c.execute(
            "EXPLAIN (COSTS OFF) SELECT count(*) FROM ias "
            "WHERE span && tstzrange('2020-01-01 02:00','2020-01-01 03:00')"
        )
        plan = [row[0] for row in c.fetchall()]
        expect.num(
            sum(1 for line in plan
                if re.search(r"Index (Only )?Scan using ias_probe", line)), 1,
            "premise: an overlap query reaches the row through a GiST index",
        )

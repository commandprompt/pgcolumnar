"""Index-fetch I/O is one read per column. The scan path coalesces adjacent
chunk ranges. Independent of test/native_fetch_coalesce.sh: same public seam,
own fixture, own observations. Assertion names match the shell suite.
"""

import pathlib
import re


NCOLS = 12
ROWS = 4500
STRIPE = 1500
TARGET_ID = 7


def _scan_node(plan):
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        t = node["Node Type"]
        if t in (
            "Index Scan",
            "Index Only Scan",
            "Bitmap Heap Scan",
            "Custom Scan",
            "Seq Scan",
        ):
            return t
        stack.extend(node.get("Plans", ()))
    return ""


def _buffer_touches(plan):
    """Sum Shared Hit + Shared Read walking the JSON plan.

    FORMAT JSON, not the text Buffers: line the shell suite parses. The two
    harnesses must not share an observer.
    """
    n = 0

    def walk(obj):
        nonlocal n
        if isinstance(obj, dict):
            hit = obj.get("Shared Hit Blocks")
            rd = obj.get("Shared Read Blocks")
            if hit is not None:
                n += int(hit)
            if rd is not None:
                n += int(rd)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(plan)
    return n


def _exec_buffer_touches(plan):
    """Executor pins only. Planning hits grow with the target list and are not
    fetch I/O; walking the whole JSON would count them.
    """
    return _buffer_touches(plan[0]["Plan"])


def _force(cur):
    cur.execute("SET max_parallel_workers_per_gather=0")
    cur.execute("SET enable_seqscan=off")
    cur.execute("SET enable_bitmapscan=off")
    cur.execute("SET pgcolumnar.enable_custom_scan=off")
    cur.execute("SET pgcolumnar.enable_index_fetch_penalty=off")


def _explain_buffers(cur, sql):
    _force(cur)
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT JSON) " + sql)
    cur.fetchone()
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT JSON) " + sql)
    return _exec_buffer_touches(cur.fetchone()[0])


def test_native_fetch_coalesce(pgc_conn, expect):
    cols = ", ".join(f"c{i:02d} int" for i in range(NCOLS))
    ins = ", ".join(f"g + {i}*100" for i in range(NCOLS))
    sel_wide = ", ".join(f"c{i:02d}" for i in range(NCOLS))
    with pgc_conn.cursor() as cur:
        cur.execute(f"CREATE TABLE nfc (id int, {cols}) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('nfc', stripe_row_limit => %s, "
            "chunk_group_row_limit => 500, compression => 'none')",
            (STRIPE,),
        )
        cur.execute(
            f"INSERT INTO nfc SELECT g, {ins} FROM generate_series(1, %s) g",
            (ROWS,),
        )
        cur.execute("CREATE INDEX nfc_id ON nfc (id)")
        cur.execute("ANALYZE nfc")
        _force(cur)
        cur.execute(
            f"EXPLAIN (FORMAT JSON, COSTS OFF) SELECT {sel_wide} FROM nfc "
            f"WHERE id = {TARGET_ID}"
        )
        plan = cur.fetchone()[0]
    expect.text(
        _scan_node(plan),
        "Index Scan",
        "premise: a point lookup uses the index",
    )

    with pgc_conn.cursor() as cur:
        _force(cur)
        cur.execute(f"SELECT {sel_wide} FROM nfc WHERE id = {TARGET_ID}")
        got = cur.fetchone()
    want = tuple(TARGET_ID + i * 100 for i in range(NCOLS))
    expect.rows(
        [got],
        [want],
        "the wide fetch returns the projected values",
    )

    with pgc_conn.cursor() as cur:
        narrow = _explain_buffers(cur, f"SELECT c00 FROM nfc WHERE id = {TARGET_ID}")
        wide = _explain_buffers(
            cur, f"SELECT {sel_wide} FROM nfc WHERE id = {TARGET_ID}"
        )
    expect.num(
        1 if narrow > 0 else 0,
        1,
        "premise: fetching one projected column touched a measurable number of buffers",
    )
    expect.num(
        1 if wide > 0 else 0,
        1,
        "premise: fetching every projected column touched a measurable number of buffers",
    )
    # A FEW PINS OF SLACK, NOT ONE PER EXTRA COLUMN (#1339): one per extra column is
    # the once-per-column cost the name forbids, and a fetch reading every column on
    # its own fitted it to the pin (measured 71 and 86 with coalescing disabled;
    # 70 and 70 coalesced).
    slack = 3
    print(f"-- index fetch buffers: narrow={narrow} wide={wide} "
          f"(bound narrow + {slack})")
    expect.text(
        "coalesced" if wide <= narrow + slack
        else f"wide {wide} over the bound {narrow + slack} "
             f"(narrow {narrow} + {slack})",
        "coalesced",
        "a wide index fetch does not pin once per column",
    )
# ---- the validity copy must be bounded by the chunk, not by the row count ----
#
# The coalesced fetch path copies the validity bytes out of a span buffer that is only
# guaranteed to hold page_length bytes for the chunk being served. The test that
# reconciles the two once ran AFTER the copy, which made a chunk whose catalog
# page_length was under its validity bitmap read past the allocation -- measured
# under -fsanitize=address as a heap-buffer-overflow that killed the backend on a
# plain index-scan SELECT.
#
# ORDERING, not presence: a check that both statements exist would pass on the
# broken code, because the broken code had both. This reads their positions.
#
# Independent of test/native_fetch_coalesce.sh by construction: that suite walks
# the function with awk and compares line numbers; this one slices the function
# out with a regex and compares character offsets. Same property, no shared
# observer, and neither invokes the other.
_FN = re.compile(
    r"^pgcolumnar_fetch_coalesce_read\(.*?^\}", re.S | re.M
)


def _coalesce_body():
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "columnar_reader.c"
    m = _FN.search(src.read_text(encoding="utf-8"))
    return m.group(0) if m else ""


def test_the_validity_copy_is_bounded_before_the_chunk_is_read(expect):
    """The bound on the vbits copy must precede the copy itself."""
    body = _coalesce_body()
    # THE FUNCTION HAS TWO GUARDS WITH THE SAME TEXT: the range-building loop
    # defers a chunk the checked decode path would refuse, and the distribution
    # loop bounds the validity copy. A plain find() returns the first, which is
    # in the wrong loop -- this check would then still pass with the
    # distribution guard deleted. The containment test belongs only to the
    # distribution loop, so searching after it pins the right guard.
    #
    # THE TEXT MOVED WITH #1130: both guards compared against `validityBytes`,
    # the row group's ceil(rowCount / 8), until the bitmap became a property of
    # the CHUNK. `cvb` is that chunk's own size, 0 where it was elided. The
    # property pinned here is unchanged; only its spelling moved.
    anchor = body.find("cc->pageOffset < start")
    guard = body.find("pageLength < (uint64) cvb", anchor + 1) if anchor >= 0 else -1
    copy = body.find("memcpy(entry->vbits")

    # NAMES THE MISSING ONE. Collapsed to a boolean this failed as `got 0 want 1`
    # for any of three different reasons -- a renamed anchor, a moved bound, a
    # rewritten copy -- and sent the reader to read the whole helper.
    absent = [what for what, at in (("the chunk-offset anchor", anchor),
                                    ("the pageLength bound", guard),
                                    ("the validity memcpy", copy)) if at < 0]
    expect.text(
        "all three present" if not absent else "not found: " + ", ".join(absent),
        "all three present",
        "premise: the coalescing helper holds both the bound and the validity copy",
    )
    expect.text(
        "bound first" if (guard >= 0 and copy >= 0 and guard < copy)
        else f"bound at {guard}, copy at {copy}",
        "bound first",
        "the validity copy is bounded by the chunk length before it runs",
    )

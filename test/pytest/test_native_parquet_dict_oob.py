"""A Parquet dictionary index with the high bit set must not read out of bounds.

The RLE_DICTIONARY decode path bounds-checked a file-controlled index with a SIGNED
comparison, `(int) idx[i] >= dictCount`. An index with the high bit set -- reachable at
bit_width 32, which the bit-packer accepts -- sign-extends to a negative int, slips past
the check, and `dict[idx[i]]` then reads about 16 GB past the dictionary. On the unfixed
build one crafted file, readable by anyone who may call `read_parquet`, takes down the
whole cluster with SIGSEGV.

Independent of test/native_parquet_dict_oob.sh: same public seam (`read_parquet` over the
two committed fixtures), own observations. Assertion names match the shell suite so the
two can be compared by name, not by importing each other.

The deterministic companion to the Parquet fuzzer, which asserts the same "a malformed
file raises an ERROR, never dies" property over random mutants but would not land on this
exact bit_width/index pair.

THE FIXTURES ARE COPIED, NOT READ IN PLACE, and that is not tidiness. The server runs as
the `postgres` OS user (`initdb` and `postgres` refuse root, so the harness uses
`runuser`), while the checkout belongs to whoever cloned it. `read_parquet` opens the file
as the server, so a fixture left in the source tree is a path the backend may not be able
to read -- and the failure would look exactly like the rejection this suite is trying to
prove, which is the worst possible confusion for an arm whose subject is "malformed input
is refused".

THE GATE CARRIES THE SHELL SUITE'S NAME (#1131). `pgc_skip fixture "dictionary-index OOB
fixtures are missing"` records under that name; before #1131 `cannot_run` could only
record its reason code and this pair could not reach `missing: 0`.
"""
import pathlib
import shutil

import psycopg

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "parquet"
BASE, EVIL = "dict_oob_base.parquet", "dict_oob_evil.parquet"

# What the reader must say when it refuses. The shell suite greps its stderr for these;
# the driver hands the message over directly, so the same vocabulary is matched without
# a subprocess in between.
REFUSAL = ("could not decode", "corrupt", "invalid", "out of range")


def _readable_copy(cluster, name):
    """-> a path to `name` that the server, running as postgres, can open."""
    dest = pathlib.Path(cluster.datadir).parent / name
    shutil.copyfile(FIXTURES / name, dest)
    dest.chmod(0o644)
    return dest


def test_native_parquet_dict_oob(pgc_cluster, pgc_conn, expect):
    if not ((FIXTURES / BASE).is_file() and (FIXTURES / EVIL).is_file()):
        expect.cannot_run(
            "ABSENT_FIXTURE",
            f"{FIXTURES} does not hold {BASE} and {EVIL}; they are built by "
            f"fixtures/parquet/gen_dict_oob.py and committed",
            # Written here rather than in a constant: compare_to_bash resolves string
            # LITERALS at the call site.
            name="dictionary-index OOB fixtures are missing")
        return

    base = _readable_copy(pgc_cluster, BASE)
    evil = _readable_copy(pgc_cluster, EVIL)

    # ---- control: the benign file still reads --------------------------------
    with pgc_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM pgcolumnar.read_parquet('{base}') AS t(v int)")
        expect.num(cur.fetchone()[0], 4,
                   "control: a normal dictionary-encoded file reads its 4 rows")

    # ---- attack: the crafted index must be refused, not dereferenced ---------
    message = None
    try:
        with pgc_conn.cursor() as cur:
            cur.execute(f"SELECT * FROM pgcolumnar.read_parquet('{evil}') AS t(v int)")
            cur.fetchall()
    except psycopg.Error as exc:
        message = str(exc)
    print(f"-- the crafted file answered: {message or 'NO ERROR -- it returned rows'}")
    expect.text(
        "yes" if message and any(p in message.lower() for p in REFUSAL) else f"no ({message})",
        "yes",
        "attack: the out-of-range dictionary index is rejected with an error")

    # ---- the decisive property ----------------------------------------------
    #
    # A rejection is the good outcome; a dead backend is the defect. Measured, with
    # the signed comparison restored:
    #
    #     the crafted file answered: consuming input failed: server closed the
    #     connection unexpectedly
    #     attack: the out-of-range dictionary index is rejected with an error:
    #         got 'no (...server closed the connection...)' want 'yes'
    #
    # So on a REAL crash it is the arm above that reddens, and the two below never
    # run -- the connection they would use is gone. They are not redundant: an
    # out-of-bounds read that happens to land on mapped memory returns garbage
    # WITHOUT raising and without dying, and then the arm above fails while these
    # two pass, which is a different and much quieter defect. Asserting all three
    # is what distinguishes those two worlds in the report.
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1,
                   "attack: the backend survives (no out-of-bounds crash)")

    log = pathlib.Path(pgc_cluster.datadir) / "server.log"
    text = log.read_text(errors="replace") if log.is_file() else ""
    crashes = sum(1 for line in text.splitlines()
                  if "terminated by signal" in line.lower()
                  or "segmentation" in line.lower())
    # THE LOG HAS TO EXIST, or "no segfault lines" is what an absent file says too --
    # the same shape as a sum over an empty set. Asserted before the count is believed.
    expect.text("present" if log.is_file() else f"absent ({log})", "present",
                "premise: the server log is where the crash would be recorded")
    expect.num(crashes, 0, "attack: the server log records no segfault")

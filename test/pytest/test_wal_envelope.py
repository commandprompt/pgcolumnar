"""pgColumnar emits only core's WAL, and keeps RelationTruncate's envelope around its one
direct record. The pytest twin of `test/wal_envelope.sh`, every check by the shell's
name (#1341).

An extension that registers a resource manager or a record type of its own couples the
WAL stream to its build, so a standby or a recovery without that build cannot replay
it. Asserted, by reading the source, not by running a server:

1. The only WAL emitted is core's: `log_newpage`, `log_newpage_buffer`, and exactly one
   direct `XLogInsert`, of core's `XLOG_SMGR_TRUNCATE` record.

2. That one site, `PgColumnarTruncateMainFork`, keeps the crash-safety envelope in
   order: checkpoint delay set, critical section entered, record inserted, WAL flushed,
   physical truncate, delay cleared, critical section left. The record covers the main
   fork only.

INDEPENDENT OF THE SHELL. The shell greps and awks; this reads the files in Python and
matches with `re` and `in`. Both read the same tree, so they agree on a correct one and
disagree only when one of them reads it wrong.

FAILS CLOSED. Every "none found" is read against how many files were scanned, and every
reading inside the function against whether the function was found: a count of matches
in no source is 0, which is the passing answer.
"""
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
FN = SRC / "columnar_storage.c"

# The envelope's steps as literal C text, in the order they must appear.
STEPS = [("delay_set", "delayChkptFlags |= DELAY_CHKPT_COMPLETE"),
         ("crit_in", "START_CRIT_SECTION()"),
         ("insert", "XLogInsert(RM_SMGR_ID"),
         ("flush", "XLogFlush("),
         ("trunc", "COLUMNAR_SMGRTRUNCATE("),
         ("delay_clear", "delayChkptFlags &= ~DELAY_CHKPT_COMPLETE"),
         ("crit_out", "END_CRIT_SECTION()")]
PRESENT = [("delay_set", "envelope step present: delay_set"),
           ("crit_in", "envelope step present: crit_in"),
           ("insert", "envelope step present: insert"),
           ("flush", "envelope step present: flush"),
           ("trunc", "envelope step present: trunc"),
           ("delay_clear", "envelope step present: delay_clear"),
           ("crit_out", "envelope step present: crit_out")]
ORDER = [("delay_set", "crit_in", "checkpoint delay is set before the critical section"),
         ("crit_in", "insert", "the record is written inside the critical section"),
         ("insert", "flush", "the WAL record precedes the flush"),
         ("flush", "trunc", "the WAL is flushed before the physical truncate"),
         ("trunc", "delay_clear", "the delay is cleared after the truncate"),
         ("delay_clear", "crit_out", "the critical section ends last")]


def _top_c():
    """-> {path: text} of src/*.c, the files the WAL-emitter counts read."""
    return {p: p.read_text(errors="replace") for p in sorted(SRC.glob("*.c"))}


def _every_file():
    """-> {path: text} of every file under src/, the files the rmgr checks read."""
    return {p: p.read_text(errors="replace") for p in sorted(SRC.rglob("*")) if p.is_file()}


def _scanned(count):
    """The count as text, or a sentence when there was no .c file to scan."""
    return str(count) if _top_c() else f"no .c files under {SRC}"


def _function():
    """-> (first line number, lines) of PgColumnarTruncateMainFork, its header up to the
    next top-level function header, or None when it is not there."""
    if not FN.is_file():
        return None
    lines = FN.read_text(errors="replace").split("\n")
    start = next((i for i, l in enumerate(lines) if l.startswith("PgColumnarTruncateMainFork(")), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"[A-Za-z_]\w*\(", lines[i])), len(lines))
    return start + 1, lines[start:end + 1]


def _at(text):
    """-> line number of the first line in the function that contains `text`, or None."""
    fn = _function()
    if fn is None:
        return None
    first, lines = fn
    return next((first + i for i, l in enumerate(lines) if text in l), None)


# ---- 1. only core mechanisms emit WAL --------------------------------------

def test_no_custom_resource_manager_is_registered(expect):
    pat = re.compile(r"RegisterCustomRmgr|RmgrData\s+[A-Za-z_]+\s*=")
    expect.text(_scanned(sum(1 for t in _every_file().values() if pat.search(t))), "0",
                "no custom resource manager is registered")


def test_no_rmgr_id_other_than_the_smgr_truncate_is_used(expect):
    ids = {m for t in _every_file().values() for m in re.findall(r"XLogInsert\(\s*(RM_[A-Z_]+)", t)}
    expect.text(_scanned(len(ids - {"RM_SMGR_ID"})), "0", "no rmgr id other than the smgr truncate is used")


def test_exactly_one_direct_xloginsert_in_the_tree(expect):
    expect.text(str(sum(1 for t in _top_c().values() for l in t.split("\n") if "XLogInsert(" in l)), "1",
                "exactly one direct XLogInsert in the tree")


def test_every_other_wal_emitter_is_a_core_full_page_image_helper(expect):
    found = {m for t in _top_c().values() for m in re.findall(
        r"\b(log_newpage_buffer|log_newpage|XLogInsert|GenericXLogFinish|XLogRegisterBuffer)\b", t)}
    others = found - {"log_newpage", "log_newpage_buffer", "XLogInsert"}
    expect.text(_scanned(len(others)), "0", "every other WAL emitter is a core full-page-image helper")


# ---- 2. the envelope around the one direct record ---------------------------

def test_pgcolumnartruncatemainfork_was_found(expect):
    expect.text("yes" if _function() else "no", "yes", "PgColumnarTruncateMainFork was found")


@pytest.mark.parametrize("step,name", PRESENT)
def test_envelope_step_present(step, name, expect):
    expect.text("yes" if _at(dict(STEPS)[step]) else "no", "yes", name)


@pytest.mark.parametrize("before,after,name", ORDER)
def test_envelope_steps_are_ordered(before, after, name, expect):
    a, b = _at(dict(STEPS)[before]), _at(dict(STEPS)[after])
    expect.text("yes" if a is not None and b is not None and a < b
                else f"no ({before} at line {a}, {after} at line {b})", "yes", name)


def _masks(pattern):
    fn = _function()
    if fn is None:
        return "PgColumnarTruncateMainFork not found"
    return str(sum(1 for l in fn[1] if re.search(pattern, l)))


def test_the_truncate_record_covers_the_main_fork_only(expect):
    expect.text(_masks(r"SMGR_TRUNCATE_HEAP"), "1", "the truncate record covers the main fork only")


def test_no_fork_mask_other_than_the_heap_fork_is_used_here(expect):
    expect.text(_masks(r"SMGR_TRUNCATE_(VM|FSM|ALL)"), "0", "no fork mask other than the heap fork is used here")

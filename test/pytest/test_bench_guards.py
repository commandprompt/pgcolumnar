"""The benchmark's own guards (#465). The pytest twin of `test/bench_guards.sh`, every
check by the shell's name (#1341).

bench/run_clickbench.sh publishes numbers, and a benchmark that reports a failed load as
a fast one is worse than one that does not run. Asserted, with no cluster:

1. Every bench runner refuses an assert build, offers a named override, exits non-zero,
   lets a plain build through, and refuses when pg_config cannot tell. Each is decided
   by RUNNING the runner's own guard against a stand-in pg_config, not by grepping for
   its words: the greps passed with the refusal gone.

2. bench/cb_guards.sh's arithmetic: the prepared-transaction and worker-slot preflights,
   the row-count check, the cold-run tag, and the win/tie/loss verdict with its band.

3. The report asks cb_verdict for its verdict, and docs/benchmarks.md calls no query
   "run to run variation" without a measured tie.

INDEPENDENT OF THE SHELL where it can be. The guard block is found by this file's own
reader, in Python, and the source checks read the files with `re`. The cb_* functions are
the product under test, so they are called through bash, as the benchmark calls them.

NO CLUSTER. This file is in NO_CLUSTER (test_harness_deps.py).
"""
import pathlib
import re
import subprocess

import pytest

from pgc_vacuity import query_error

ROOT = pathlib.Path(__file__).resolve().parents[2]
BENCH = ROOT / "bench"
GUARDS = BENCH / "cb_guards.sh"
CB = BENCH / "run_clickbench.sh"
DOC = ROOT / "docs" / "benchmarks.md"
RUNNERS = sorted(p.name for p in BENCH.glob("run_*.sh"))
MODES = {"cassert": "echo \"'--prefix=/nonexistent' '--enable-cassert'\"",
         "plain": "echo \"'--prefix=/nonexistent'\"",
         "empty": "exit 1"}


def _bash(script, env=None):
    """-> (exit status, stdout and stderr) of a bash script, or (None, query_error())."""
    try:
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, query_error(f"bash could not run: {exc}")
    return r.returncode, r.stdout + r.stderr


def _sh(expr):
    """-> what `$(expr)` gives with the guards sourced: stdout, trailing newlines dropped."""
    try:
        r = subprocess.run(["bash", "-c", f'. "{GUARDS}"; {expr}'], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return query_error(f"bash could not run: {exc}")
    return r.stdout.rstrip("\n")


def _ok(call):
    """-> "ok" when the guard call succeeds, else "refused", as `call && echo ok || echo refused`."""
    rc, _ = _bash(f'. "{GUARDS}"; {call}')
    return "ok" if rc == 0 else "refused"


def _lines(text, needle):
    """-> how many lines of text contain needle, as `grep -c`."""
    return sum(1 for l in str(text).splitlines() if needle in l)


def _code(path):
    """-> the file's lines with whole-line comments removed."""
    return [l for l in path.read_text(errors="replace").splitlines() if not re.match(r"\s*#", l)]


def _guard_of(path):
    """-> the runner's assert guard as code: from its --configure capture through the `fi`
    closing its ALLOW_CASSERT test, within 60 lines; "" when there is no complete guard."""
    buf, on, seen = [], False, False
    for line in _code(path):
        if not on and "--configure" in line and "PG_CONFIG" in line:
            on = True
        if on:
            buf.append(line)
            seen = seen or "ALLOW_CASSERT" in line
            if seen and line == "fi":
                return "\n".join(buf)
            if len(buf) > 60:
                return ""
    return ""


@pytest.fixture(scope="module")
def guards(tmp_path_factory):
    """-> {runner: {mode: (status, output)}} from running each runner's own guard."""
    d = tmp_path_factory.mktemp("bg_pg_config")
    for mode, conf in MODES.items():
        f = d / f"pg_config_{mode}"
        f.write_text("#!/bin/sh\ncase \"$1\" in\n"
                     f"  --configure) {conf} ;;\n"
                     "  --bindir) echo /nonexistent/bin ;;\n"
                     "  --version) echo \"PostgreSQL 17.0\" ;;\n"
                     "  *) echo /nonexistent ;;\nesac\n")
        f.chmod(0o755)
    out = {}
    for name in RUNNERS:
        block = _guard_of(BENCH / name)
        var = (re.findall(r"[A-Z_]*ALLOW_CASSERT", block) or ["NO_OVERRIDE_FOUND"])[0]
        r = {}
        for key, mode, allow in (("assert", "cassert", False), ("override", "cassert", True),
                                 ("plain", "plain", False), ("none", "empty", False)):
            env = {"PATH": "/usr/bin:/bin", "PG_CONFIG": str(d / f"pg_config_{mode}")}
            if allow:
                env[var] = "1"
            r[key] = _bash("set -uo pipefail\n" + block, env)
        r["piped"] = sum(1 for l in block.splitlines()
                         if re.search(r'\$\("\$PG_CONFIG" --configure[^)]*\| *grep -c', l))
        out[name] = r
    return out


# ---- every runner refuses an assert build, by running its own guard ---------------

@pytest.mark.parametrize("runner", RUNNERS)
def test_the_runner_refuses_an_assert_build(guards, runner, expect):
    rc, out = guards[runner]["assert"]
    expect.text("yes" if "is an --enable-cassert build" in out else f"no: {out[:200]}", "yes",
                f"[{runner}] refuses an assert build rather than documenting the requirement")


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_runner_offers_a_named_override(guards, runner, expect):
    rc, out = guards[runner]["override"]
    expect.text(f"rc={rc}/{_lines(out, 'WARNING')}", "rc=0/1",
                f"[{runner}] and offers a named override rather than only refusing")


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_runner_exits_non_zero_on_an_assert_build(guards, runner, expect):
    rc, _ = guards[runner]["assert"]
    expect.text(f"rc={rc}", "rc=1", f"[{runner}] and exits non-zero rather than warning past it")


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_runner_lets_a_plain_build_through(guards, runner, expect):
    rc, out = guards[runner]["plain"]
    expect.text(f"rc={rc}/{_lines(out, 'FATAL')}", "rc=0/0",
                f"[{runner}] and lets a non-assert build through")


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_runner_refuses_when_it_cannot_tell(guards, runner, expect):
    rc, out = guards[runner]["none"]
    expect.text(f"rc={rc}/{_lines(out, 'produced no --configure output')}", "rc=1/1",
                f"[{runner}] refuses when it CANNOT TELL, rather than treating that as a pass")


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_runner_does_not_pipe_configure_into_grep(guards, runner, expect):
    expect.text("yes" if guards[runner]["piped"] == 0 else "no", "yes",
                f"[{runner}] and does not decide from an unbuffered pipe into grep -c")


def test_every_bench_runner_was_read(expect):
    n = len(RUNNERS)
    expect.text("yes" if n >= 6 else f"no (found {n})", "yes",
                "premise: every bench runner was read, not a hand-kept subset")


def test_the_guards_file_is_present(expect):
    expect.text("yes" if GUARDS.is_file() else "no", "yes",
                "premise: bench/cb_guards.sh is present to be judged")


# ---- max_prepared_transactions is preflighted -------------------------------------

def test_the_stock_zero_is_refused(expect):
    expect.text(_ok("cb_prepared_xacts_ok 0 8"), "refused", "the stock 0 is refused for an 8-worker parallel arm")


def test_an_exact_prepared_match_is_accepted(expect):
    expect.text(_ok("cb_prepared_xacts_ok 8 8"), "ok", "an exact match is accepted")


def test_more_than_enough_prepared_is_accepted(expect):
    expect.text(_ok("cb_prepared_xacts_ok 16 8"), "ok", "more than enough is accepted")


def test_one_prepared_short_is_refused(expect):
    expect.text(_ok("cb_prepared_xacts_ok 7 8"), "refused",
                "one short is refused, which is the off-by-one that would run 7 workers")


def test_a_serial_arm_needs_no_prepared_transactions(expect):
    expect.text(_ok("cb_prepared_xacts_ok 0 0"), "ok", "a serial arm needs no prepared transactions")


# ---- max_worker_processes is preflighted ------------------------------------------

def test_the_stock_eight_slots_are_refused(expect):
    expect.text(_ok("cb_worker_slots_ok 8 8"), "refused",
                "the stock 8 is refused for an 8-worker arm, which is the case that bit us")


def test_n_plus_one_slots_are_refused(expect):
    expect.text(_ok("cb_worker_slots_ok 9 8"), "refused", "N + 1 is still refused: the coordinator needs a slot too")


def test_n_plus_two_slots_are_accepted(expect):
    expect.text(_ok("cb_worker_slots_ok 10 8"), "ok", "N + 2 is accepted, the measured minimum")


def test_more_than_enough_worker_slots_are_accepted(expect):
    expect.text(_ok("cb_worker_slots_ok 32 8"), "ok", "more than enough worker slots are accepted")


def test_four_workers_need_six_slots(expect):
    expect.text(_ok("cb_worker_slots_ok 6 4"), "ok", "the rule holds at another worker count (4 needs 6)")


def test_five_slots_for_four_workers_are_refused(expect):
    expect.text(_ok("cb_worker_slots_ok 5 4"), "refused", "and one below it does not")


def test_a_serial_arm_needs_no_worker_slots(expect):
    expect.text(_ok("cb_worker_slots_ok 0 0"), "ok", "a serial arm needs no worker slots")


def test_a_missing_slot_count_is_refused(expect):
    expect.text(_ok('cb_worker_slots_ok "" 8'), "refused", "a missing current value is refused, not compared")


def test_the_slot_message_names_the_setting(expect):
    m = _sh("cb_worker_slots_message 8 8")
    expect.text("yes" if "max_worker_processes" in m else "no", "yes", "the worker-slot message names the setting")


def test_the_slot_message_names_the_value(expect):
    m = _sh("cb_worker_slots_message 8 8")
    expect.text("yes" if "10" in m else "no", "yes", "and the value it must reach, not merely the worker count")


def test_the_slot_message_says_restart(expect):
    m = _sh("cb_worker_slots_message 8 8")
    expect.text("yes" if "restart" in m.lower() else "no", "yes", "and says it needs a restart")


def test_the_prepared_message_names_the_setting(expect):
    m = _sh("cb_prepared_xacts_message 0 8")
    expect.text("yes" if "max_prepared_transactions" in m else "no", "yes", "the message names the setting to change")


def test_the_prepared_message_names_the_number(expect):
    m = _sh("cb_prepared_xacts_message 0 8")
    expect.text("yes" if "8" in m else "no", "yes", "and the number it must reach")


def test_the_prepared_message_says_restart(expect):
    m = _sh("cb_prepared_xacts_message 0 8")
    expect.text("yes" if "restart" in m.lower() else "no", "yes",
                "and says it needs a restart, which is why this runs first")


# ---- a load that lost rows is a failure -------------------------------------------

def test_a_short_load_is_refused(expect):
    expect.text(_ok("cb_rows_ok 1999999 2000000"), "refused", "a short load is refused")


def test_an_empty_load_is_refused(expect):
    expect.text(_ok("cb_rows_ok 0 2000000"), "refused",
                "an empty load is refused, which is what an errored parallel arm produces")


def test_an_exact_load_is_accepted(expect):
    expect.text(_ok("cb_rows_ok 2000000 2000000"), "ok", "an exact load is accepted")


def test_two_missing_row_counts_are_refused(expect):
    expect.text(_ok("cb_rows_ok '' ''"), "refused", "two missing measurements are refused, not called equal")


def test_a_missing_row_count_is_refused(expect):
    expect.text(_ok("cb_rows_ok '' 2000000"), "refused",
                "a missing count is refused rather than compared with the expectation")


def test_a_missing_row_expectation_is_refused(expect):
    expect.text(_ok("cb_rows_ok 2000000 ''"), "refused", "and a missing expectation is refused too")


# ---- a report does not claim a cold run it did not perform (#506) -----------------

def test_the_cold_tag_guard_emits_a_tag(expect):
    expect.text("yes" if _sh("cb_cold_tag none 2>/dev/null") else "no", "yes",
                "the guard exists and emitted a tag to judge")


def test_a_warm_host_is_not_called_cold(expect):
    t = _sh("cb_cold_tag none 2>/dev/null")
    expect.text("claimed" if "lukewarm-cold-run" in t.lower() else "not-claimed", "not-claimed",
                "a host that could not drop the page cache is not called a cold run")


def test_the_warm_tag_says_warm(expect):
    expect.text("yes" if "WARM-RUN" in _sh("cb_cold_tag none 2>/dev/null") else "no", "yes",
                "and the tag says plainly that the run was warm")


# ---- a win is decided on the times (#531) -----------------------------------------

def test_the_verdict_helpers_are_present(expect):
    expect.text(_sh('echo "$(type -t cb_verdict)/$(type -t cb_warm_spread)/$(type -t cb_band)"'),
                "function/function/function", "the verdict helpers are present to be judged")


def test_a_clear_win(expect):
    expect.text(_sh("cb_verdict 500 1000 0.02"), "win", "a clear win is a win")


def test_a_clear_loss(expect):
    expect.text(_sh("cb_verdict 2000 1000 0.02"), "loss", "a clear loss is a loss")


def test_a_rounding_win_is_not_a_loss(expect):
    expect.text(_sh("cb_verdict 1582.859 1588.095 0.0000"), "win",
                "columnar ahead by 0.33 percent is not a LOSS, which is the #531 defect")


def test_a_gap_inside_the_band_is_a_tie(expect):
    expect.text(_sh("cb_verdict 1582.859 1588.095 0.0099"), "tie",
                "and under a band wider than the gap it is a tie, not a win either")


def test_a_win_just_outside_the_band(expect):
    expect.text(_sh("cb_verdict 900 1000 0.05"), "win", "a gap just outside the band is still called")


def test_a_loss_just_outside_the_band(expect):
    expect.text(_sh("cb_verdict 1100 1000 0.05"), "loss", "and a loss just outside it likewise")


def test_spread_is_over_the_best(expect):
    expect.text(_sh("cb_warm_spread 100 103"), "0.0300", "spread is the fractional range over the best")


def test_spread_ignores_order_and_err(expect):
    expect.text(_sh("cb_warm_spread 103 100 ERR"), "0.0300", "order does not matter, and ERR tries are dropped")


def test_one_try_has_no_spread(expect):
    expect.text(_sh("cb_warm_spread 100"), "0.0000", "a single warm try has no measurable scatter")


def test_no_usable_try_has_no_spread(expect):
    expect.text(_sh("cb_warm_spread '' ERR"), "-", "no usable try yields no spread, not zero")


def test_a_zero_best_is_refused(expect):
    expect.text(_sh("cb_warm_spread 0 5"), "-", "a zero best is refused rather than divided by")


def test_the_band_is_the_wider_arm(expect):
    expect.text(_sh("cb_band 0.0100 0.0290"), "0.0290", "the band is the wider of the two arms")


def test_an_unknown_arm_makes_the_band_unknown(expect):
    expect.text(_sh("cb_band - 0.0290"), "-", "and is unknown if either arm is unknown")


def test_an_unknown_band_suppresses_the_verdict(expect):
    expect.text(_sh("cb_verdict 1582.859 1588.095 -"), "-",
                "an unknown band suppresses the verdict, it does not strict-compare")


def test_an_errored_arm_has_no_verdict(expect):
    expect.text(_sh("cb_verdict ERR 1000 0.02"), "-", "an errored arm has no verdict")


def test_a_zero_baseline_has_no_verdict(expect):
    expect.text(_sh("cb_verdict 500 0 0.02"), "-", "and neither does a zero baseline")


# ---- the report asks the guard (#531) ---------------------------------------------

def test_the_benchmark_script_is_present(expect):
    expect.text("yes" if CB.is_file() else "no", "yes", "premise: the benchmark script is present to be judged")


def test_the_report_captures_ratio(expect):
    text = CB.read_text(errors="replace") if CB.is_file() else ""
    expect.text("yes" if re.search(r"(^|[^A-Za-z0-9_])r[12]=\$\(ratio", text, re.M) else "no", "yes",
                "premise: the report still captures ratio() into a display variable")


def test_ratio_reaches_only_printf(expect):
    lines = CB.read_text(errors="replace").splitlines() if CB.is_file() else []
    n = sum(1 for l in lines if re.search(r"\$r[12]", l) and "printf" not in l)
    expect.text(str(n) if CB.is_file() else "no benchmark script", "0",
                "ratio()'s output reaches printf and nothing else")


def test_the_verdict_is_asked_of_cb_verdict(expect):
    n = sum(1 for l in _code(CB) if 'cb_verdict "' in l) if CB.is_file() else 0
    expect.text("yes" if n >= 1 else "no", "yes",
                "and the verdict is asked of cb_verdict rather than recomputed inline")


# ---- the accelerator claim (#565) -------------------------------------------------

def test_the_benchmark_page_is_present(expect):
    expect.text("yes" if DOC.is_file() else "no", "yes", "premise: the benchmark page is present to be judged")


def test_the_extractor_reads_wrapped_ids(tmp_path, expect):
    fx = tmp_path / "claim.md"
    fx.write_text("A lead sentence naming q18 and q31 here.\ntheir rows are therefore run to\nrun variation, not an effect.\n")
    expect.text(" ".join(_sh(f'cb_doc_variation_qids "{fx}"').split()), "q18 q31",
                "the variation-claim extractor reads ids wrapped across two lines")


def test_no_query_is_called_variation_without_a_tie(expect):
    qids = _sh(f'cb_doc_variation_qids "{DOC}"').split()
    bad = ""
    for q in qids:
        fig = _sh(f'cb_doc_accel_figures "{DOC}" "{q}"').split()
        d, a = (fig + ["", ""])[:2]
        v = _sh(f'cb_verdict "{a}" "{d}" "$(cb_band - -)"')
        if v != "tie":
            bad += f" {q}({d or '?'}/{a or '?'}:{v})"
    expect.text(f"claims={len(qids)}{bad}", "claims=0",
                "no query is called run-to-run variation unless its figures are a measured tie")


def test_the_tuned_arm_names_its_settings(expect):
    text = DOC.read_text(errors="replace") if DOC.is_file() else ""
    m = re.search(r"^### The accelerations are off by default[^\n]*\n(.*?)(?=^#|\Z)", text, re.M | re.S)
    sec = m.group(1) if m else ""
    miss = "".join(f" {t}" for t in ("pgcolumnar.enable_group_vectorization",
                                     "pgcolumnar.enable_ungrouped_vector_agg",
                                     "pgcolumnar.enable_parallel_vector_agg",
                                     "pgcolumnar.groupagg_max_groups", "200000000") if t not in sec)
    expect.text(f"missing:{miss}", "missing:",
                "the tuned-arm section names all four settings and the group-cap value")

#!/usr/bin/env python3
"""Every unrunnable record must name the check it stands in for.

#1040. `check_unrunnable NAME REASON DETAIL` gives one check the honesty
`pgc_skip` gives a whole suite: the check did not run, and the reader is told
WHICH. That only works if the record carries the name the check uses when it DOES
run. Where the two spellings differ the property has two ledger keys, and which
one appears depends on runtime state -- on `hilbert_locality` it depended on
whether that box's two partitions came out different that day.

This is part 470's property one helper over. 470 asks whether a skip LOOP still
names the arms its sibling branch emits; this asks whether an unrunnable record
names a check the same file asserts anywhere.

`check_skip` IS NOT SWEPT, and that is a finding rather than an omission. A
skipped arm has no runnable counterpart by construction -- the name IS the arm --
so 21 of its 23 call sites have no twin and always will. Sweeping it would report
21 mismatches that are all correct code. For the same reason a SKIP record is not
a twin: neither side ran.

WHAT IS AND IS NOT COMPARED, printed rather than assumed:

    sites      every name passed to a refusal emitter
    dynamic    the whole name is one shell expansion, so there is no literal to
               compare (`arms_unrunnable` reads its names from a list)
    compared   the rest, where a missing twin is a real finding

`dynamic` is printed for the reason 470 prints `interpolated`: a comparison
nobody makes and a comparison that passes are indistinguishable in a total.

THREE THINGS THIS TOOL DERIVES RATHER THAN LISTS, each because a list of it was
wrong first:

1. WHICH FUNCTIONS RECORD, from the `pgc_record` call in their body. Two
   hand-written lists gave two wrong answers on this question. One omitted
   `pgc_pass`, and `projection_rewrite.sh` then reported a false orphan because
   its runnable twin records through `pgc_pass` and not a `check_*` helper.

2. WHICH ARGUMENT IS THE NAME. `pgc_skip <capability> <message>` records
   `pgc_record FAIL "$2"`: the first quoted argument is the CAPABILITY, not the
   name. A reader assuming argument one takes `arrow` where the check is called
   `arrow support is present` -- the bash-side mirror of the name-position defect
   #1036 and #1038 closed on the python side. 22 call sites.
   `pgc_require_tools` records a FIXED name and takes no name argument at all.

3. WHAT COUNTS AS A REFUSAL, from the verdict recorded (`UNRUN`), not from the
   helper's spelling.

THE TWO SETS ARE ASYMMETRIC ON PURPOSE, because their failure directions are.
An over-broad REFUSAL set sweeps a site that did not need sweeping: extra work,
visible. An over-broad TWIN set supplies a name nothing records and turns a real
mismatch green: silent. So refusal wrappers are followed transitively, while the
twin set is restricted to `lib.sh` and to suite-local definitions spelled
`check` or `check_<word>`. `check[a-z_]*` is NOT that spelling: it also matches
`checks_in`, the counting utility `decode_interrupts.sh` had until #1341, which records
nothing (@jdatcmd). A population named after a prefix is not a population named after a
behaviour.

AND THE SUBJECT MUST NOT BE IN THE REFERENCE SET. The first sweep of this
property subtracted the unrunnable names from the runnable set -- removing the
very names it was looking up -- so every name could only report absent and a
nine-for-nine file read as nine orphans. `--show-emitters` prints both sets so
the separation is inspectable rather than asserted.
"""

import pathlib
import re
import sys

_HELPER_DEF = re.compile(r"^([a-z_][a-z0-9_]*)\(\)\s*\{", re.M)
_SUITE_LOCAL = re.compile(r"^check(?:_[a-z_]+)?$")
_HEREDOC = re.compile(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?\s*$")
_BARE_VAR = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*|[0-9]+)\}?$")


def blank_heredocs(lines):
    """-> the lines with heredoc BODIES blanked out.

    A fixture written into a heredoc is text, not shell. Kept because
    `skip-loop-arms.py` needs it on this corpus; `--keep-heredocs` runs without
    it so the part can show the answer does not depend on it.
    """
    out, term = [], None
    for l in lines:
        if term is None:
            m = _HEREDOC.search(l)
            out.append(l)
            if m:
                term = m.group(1)
        else:
            out.append("")
            if l.strip() == term:
                term = None
    return out


def bodies(text):
    """-> [(name, body)] with the body ended by ITS OWN closing brace.

    `text.find("\n}")` is wrong for a one-line definition: `q() { psql ...; }` in
    `audit.sh` closes on its own line, so a search for a brace at column zero ran
    on to the NEXT function's and swallowed every `check` call in between. `q`
    then appeared in the twin set -- a psql wrapper offering SQL text as check
    names. Depth is counted per line from the opening brace.
    """
    out = []
    lines = text.splitlines()
    for i, l in enumerate(lines):
        m = _HELPER_DEF.match(l)
        if not m:
            continue
        depth = l.count("{") - l.count("}")
        body = [l[m.end():]]
        j = i + 1
        while j < len(lines) and depth > 0:
            depth += lines[j].count("{") - lines[j].count("}")
            body.append(lines[j])
            j += 1
        out.append((m.group(1), "\n".join(body)))
    return out


def split_args(line, fn):
    """-> the arguments of `fn` on this line, as written.

    A quoted argument yields its contents; an unquoted one yields the word. The
    POSITION is what matters, so counting only quoted strings would misread
    `pgc_skip $cap "the message"`.
    """
    m = re.match(r"^(?:.*?(?:;|&&|\|\||\||&))?\s*%s\s+(.*)$" % re.escape(fn), line)
    if not m:
        return None
    rest, args, buf, q, esc = m.group(1), [], "", None, False
    for ch in rest:
        if esc:
            buf += ch; esc = False; continue
        if ch == "\\":
            esc = True; continue
        if q:
            if ch == q:
                q = None
            else:
                buf += ch
            continue
        if ch in "\"'":
            q = ch; continue
        if ch.isspace():
            if buf:
                args.append(buf); buf = ""
            continue
        if ch in "#;&|" and not buf:
            break
        buf += ch
    if buf:
        args.append(buf)
    return args


def _resolve(expr, body):
    """-> the 1-based argument position the name comes from, or None.

    `pgc_record PASS "$1"` is position 1. `check()` writes `local name="$1"` and
    then records `"$name"`, so a one-step assignment is followed.
    """
    m = _BARE_VAR.match(expr.strip())
    if not m:
        return None
    tok = m.group(1)
    if tok.isdigit():
        return int(tok)
    a = re.search(r"\b%s=\"\$\{?([0-9]+)\}?\"" % re.escape(tok), body)
    return int(a.group(1)) if a else None


def recorders(text, lib):
    """-> (refusal, twins): {fn: name-position-or-None}, by the verdict recorded.

    None as a position means the function records a FIXED name, which still
    contributes that literal (`pgc_require_tools`) but takes no name argument.
    """
    defs, in_lib = [], set()
    for src, is_lib in ((lib, True), (text, False)):
        for name, body in bodies(src):
            defs.append((name, body))
            if is_lib:
                in_lib.add(name)

    refusal, twins, fixed = {"check_unrunnable": 1}, {}, {}
    for _ in range(2):
        for name, body in defs:
            eligible = name in in_lib or _SUITE_LOCAL.match(name)
            for verdict, expr in re.findall(r"pgc_record\s+(\w+)\s+\"([^\"]*)\"", body):
                pos = _resolve(expr, body)
                if verdict == "UNRUN":
                    refusal.setdefault(name, pos)
                elif verdict == "SKIP":
                    pass                     # neither side ran; not a twin
                elif eligible and name not in refusal:
                    # A REFUSAL EMITTER IS NEVER ITS OWN TWIN. `check_unrunnable`
                    # records FAIL when the reason code is not one of the closed
                    # list -- a harness-misuse record, not the check running. Left
                    # in the twin set it also blocked wrapper detection, because a
                    # wrapper's body naming `check_unrunnable` then looked like a
                    # body naming a runnable helper.
                    if pos is None and "$" not in expr:
                        fixed.setdefault(name, expr)
                    twins.setdefault(name, pos)
            # A wrapper that reaches a refusal emitter and no recorder is itself a
            # refusal: over-broad on the safe side.
            if name not in refusal and not re.search(r"\bpgc_record\b", body):
                if any(re.search(r"\b%s\b" % re.escape(r), body) for r in refusal):
                    if not any(re.search(r"\b%s\b" % re.escape(t), body) for t in twins):
                        refusal[name] = None
            # A helper that DELEGATES its name reaches the twin set through the
            # delegate, whether or not it also records directly: `check_timing`
            # records SKIP itself and passes the same name on to `check`.
            if name not in refusal and name not in twins and eligible:
                if True:
                    for t, tpos in list(twins.items()):
                        c = re.search(r"\b%s\s+(.*)" % re.escape(t), body)
                        if c and tpos:
                            args = split_args("\t" + t + " " + c.group(1), t) or []
                            # `check_ratio "$@"` forwards every argument, so the
                            # delegate's name position IS this one's. Without this
                            # `check_ratio_needs_quiet_machine` was absent from the
                            # twin set for no reason a reader could see, and an
                            # unexplained hole in a guard's own population is the
                            # thing the guard is supposed to be better than.
                            if args[:1] == ["$@"]:
                                twins.setdefault(name, tpos)
                            elif len(args) >= tpos:
                                p = _resolve(args[tpos - 1], body)
                                if p:
                                    twins.setdefault(name, p)
                            break
    for k in refusal:
        twins.pop(k, None)
    return refusal, twins, fixed


def names_for(lines, emitters):
    """-> [(name, lineno)] taking each emitter's OWN name argument, not argument one."""
    out = []
    for i, l in enumerate(lines):
        for fn, pos in emitters.items():
            if pos is None:
                continue
            args = split_args(l, fn)
            if args and len(args) >= pos:
                out.append((args[pos - 1], i + 1))
                break
    return out


def main(testdir, show_emitters=False, keep_heredocs=False):
    root = pathlib.Path(testdir)
    files = sorted(root.glob("*.sh"))
    lib = (root / "lib.sh").read_text(errors="replace") if (root / "lib.sh").exists() else ""

    sites = dynamic = compared = 0
    bad = []
    all_ref, all_twin, all_fixed = {}, {}, {}
    for f in files:
        text = f.read_text(errors="replace")
        lines = text.splitlines()
        if not keep_heredocs:
            lines = blank_heredocs(lines)
        refusal, twins, fixed = recorders(text, lib)
        all_ref.update(refusal); all_twin.update(twins); all_fixed.update(fixed)
        unrun = names_for(lines, refusal)
        if not unrun:
            continue
        seen = {n for n, _ in names_for(lines, twins)} | set(fixed.values())
        for name, lineno in unrun:
            sites += 1
            if _BARE_VAR.match(name):
                dynamic += 1
                continue
            compared += 1
            if name not in seen:
                bad.append(f"{f.name}:{lineno} {name}")

    print(f"files {len(files)}")
    print(f"sites {sites}")
    print(f"dynamic {dynamic}")
    print(f"compared {compared}")
    for b in bad:
        print(f"MISMATCH {b}")
    if show_emitters:
        print("refusal " + " ".join(f"{k}:{v}" for k, v in sorted(all_ref.items())))
        print("twins " + " ".join(f"{k}:{v}" for k, v in sorted(all_twin.items())))
        print("fixed " + " ".join(sorted(all_fixed)))
    # THE EXIT CODE IS THE VERDICT. It was a flat 0 in the first version, so the
    # tool printed two MISMATCH lines and reported success. Part 480 gates on the
    # parsed output and was never fooled, but the next caller is the hazard: anyone
    # wiring this into CI and trusting `$?` would get a gate that cannot fail.
    # Reported by @jdatcmd, who re-measured it without a pipe before believing it,
    # because the first reading was `tail`'s status and not this program's.
    return 1 if bad else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else "test",
                  show_emitters="--show-emitters" in sys.argv,
                  keep_heredocs="--keep-heredocs" in sys.argv))

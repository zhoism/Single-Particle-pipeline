#!/usr/bin/env python3
"""Mutation testing — the "who tests the tester" proof.

Deliberately inject semantic bugs into a COPY of the engine and confirm the
independent oracle/spec slice KILLS each mutant (i.e. detects the breakage). A
surviving mutant is a real hole in the harness, not a pass.

Each mutant is applied to a scratch copy of scripts/wrapper.py (+ a copy of
check_amber_vendored.py beside it) and exercised via SUBPROCESS, so we never
import a broken module into this process.

Run: python3 mutation_test.py   (exit 0 iff every non-equivalent mutant killed)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
SCRIPTS = SKILL_DIR / "scripts"
WRAPPER_SRC = (SCRIPTS / "wrapper.py").read_text()
VENDORED = SCRIPTS / "check_amber_vendored.py"

sys.path.insert(0, str(TESTS_DIR))
import oracle as O  # noqa: E402

GT = O.load_ground_truth()


def edit_expectation(stage, param, rendered):
    allowed = {("cntrl", param)}
    must = [("cntrl", param, rendered)]
    if param == "temp0" and GT.wt_temp0[stage]:
        allowed.add(("wt", "value2"))
        must.append(("wt", "value2", rendered))
    return allowed, must


def make_md(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    for s in O.STAGES:
        (d / O.STAGE_FILE[s]).write_text(GT.text[s], newline="")
    return d


def restore(md: Path):
    for s in O.STAGES:
        (md / O.STAGE_FILE[s]).write_text(GT.text[s], newline="")


def run_sub(wrapper: Path, md: Path, selector, param, value):
    cmd = ["python3", str(wrapper), "--md-dir", str(md), "--stage", str(selector),
           "--param", str(param), "--value", str(value)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(p.stdout), p
    except Exception:
        return None, p


def verify_one(wrapper: Path, selector, param, value, md) -> list[str]:
    """Return the list of contract violations (empty = engine matches spec/oracle)."""
    restore(md)
    exp = O.spec(selector, param, str(value), GT)
    env, p = run_sub(wrapper, md, selector, param, value)
    if env is None:
        return [f"no-json (rc={p.returncode})"]
    probs = []
    if exp.global_error:
        if env.get("ok") is not False or not any(exp.global_error in e for e in env.get("errors", [])):
            probs.append(f"global: exp {exp.global_error} got ok={env.get('ok')} {env.get('errors')}")
        return probs
    if env.get("ok") != exp.ok:
        probs.append(f"ok exp {exp.ok} got {env.get('ok')}")
    recs = {f["file"]: f for f in env.get("outputs", {}).get("files", [])}
    for st, (estatus, _) in exp.per_file.items():
        fn = O.STAGE_FILE[st]
        rec = recs.get(fn, {})
        if rec.get("status") != estatus:
            probs.append(f"{fn} status exp {estatus} got {rec.get('status')}")
        cur = (md / fn).read_text(newline="")
        orig = GT.text[st]
        if estatus in ("error", "skipped", "unchanged"):
            if cur != orig:
                probs.append(f"{fn} touched on {estatus}")
        elif estatus == "edited":
            allowed, must = edit_expectation(st, param, exp.rendered)
            try:
                O.verify_edit(orig, cur, allowed_changes=allowed, must_equal=must)
            except O.OracleError as e:
                probs.append(f"{fn} oracle: {e}")
            if exp.warn_cut and not rec.get("warnings"):
                probs.append(f"{fn} missing cut warn")
            if (not exp.warn_cut) and rec.get("warnings"):
                probs.append(f"{fn} spurious warn")
    return probs


def verify_ambiguous(wrapper: Path, base: Path) -> list[str]:
    """Duplicate-key file must yield AMBIGUOUS_PARAM and leave the file untouched."""
    d = base / "ambig"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "heat-1.in"
    f.write_text(" &cntrl\n  dt = 0.002,\n  dt = 0.002,\n /\n", newline="")
    before = f.read_text(newline="")
    env, p = run_sub(wrapper, d, "heat-1", "dt", "0.001")
    if env is None:
        return [f"ambiguous: no-json (rc={p.returncode})"]
    probs = []
    if env.get("ok") is not False or not any("AMBIGUOUS_PARAM" in e for e in env.get("errors", [])):
        probs.append(f"ambiguous: exp AMBIGUOUS_PARAM got ok={env.get('ok')} {env.get('errors')}")
    if f.read_text(newline="") != before:
        probs.append("ambiguous: file modified")
    return probs


# Curated slice: collectively exercises bounds, no-op, coupling, render-format,
# warn band (both sides), applicability, and the input gate.
SLICE = [
    ("heat-1", "dt", "0.001"),
    ("heat-1", "dt", "0.01"),       # OUT_OF_BOUNDS
    ("heat-1", "dt", "0.002"),      # at-target -> unchanged (no-op detection)
    ("heat-3", "temp0", "310"),     # coupling value2 (+ '.0' render)
    ("heat-1", "temp0", "305"),     # coupling on a stage where value2 differs
    ("relax", "cut", "7.0"),        # warn band present
    ("relax", "cut", "8.0"),        # warn band ABSENT (boundary)
    ("press-1", "restraint_wt", "1.0"),
    ("min1", "dt", "0.001"),        # PARAM_NOT_FOUND single -> error
    ("heat-1", "nstlim", "inf"),    # input gate -> INVALID_VALUE (no crash; caught by isfinite)
    ("heat-1", "nstlim", "1_000"),  # underscore literal -> INVALID_VALUE (VAL_ASCII-only catch)
    ("heat-1", "dt", "０.００２"),       # fullwidth unicode digits -> INVALID_VALUE (VAL_ASCII-only catch)
]

# (name, [(old, new), ...], invariant-it-breaks, equivalent?)
MUTANTS = [
    ("loosen-dt-bound",
     [("lambda v: 0 < v <= 0.002", "lambda v: 0 < v <= 0.02")],
     "dt upper bound", False),
    ("coupling-hits-value1",
     [('temp0_wt_span(text2), "value2", rendered',
       'temp0_wt_span(text2), "value1", rendered')],
     "temp0/&wt coupling targets the wrong key", False),
    ("render-drop-dot0",
     [('return f"{int(d)}.0"', 'return f"{int(d)}"')],
     "integral float rendered without .0 (non-canonical)", False),
    ("never-noop",
     [("if old == rendered:", "if False:  # mutant")],
     "no-op detection (at-target should be 'unchanged')", False),
    ("drop-ambiguous-guard",
     [("if len(matches) > 1:", "if False:  # mutant")],
     "ambiguous-key guard removed", False),
    ("cut-band-inclusive",
     [("if param == \"cut\" and 6.0 <= v < 8.0:",
       "if param == \"cut\" and 6.0 <= v <= 8.0:")],
     "cut WARN band boundary (8.0 wrongly warns)", False),
    ("remove-input-gate",
     [("if not _VAL_ASCII.fullmatch(raw):", "if False:  # mutant")],
     "ASCII/finite input gate removed (crash/silent-accept)", False),
    ("span-off-by-one",
     [('vstart, vend = start + m.start("val"), start + m.end("val")',
       'vstart, vend = start + m.start("val"), start + m.end("val") + 1')],
     "edit span eats one extra byte (corruption; self-check should catch)", False),
]


def apply_mutant(name, repls):
    src = WRAPPER_SRC
    for old, new in repls:
        if old not in src:
            return None, f"mutation target not found: {old!r}"
        src = src.replace(old, new, 1)
    d = Path(tempfile.mkdtemp(prefix=f"mut-{name}-"))
    (d / "wrapper.py").write_text(src)
    shutil.copy(VENDORED, d / "check_amber_vendored.py")
    return d / "wrapper.py", None


def main() -> int:
    base = SKILL_DIR / "test-runs" / "mutation"
    base.mkdir(parents=True, exist_ok=True)
    md = make_md(base, "scratch")

    # Sanity: the REAL engine must pass the whole slice (baseline green).
    print("[mutation] baseline (real engine must pass slice + ambiguous)", file=sys.stderr)
    base_probs = []
    for sel, p, v in SLICE:
        base_probs += [(sel, p, v, x) for x in verify_one(SCRIPTS / "wrapper.py", sel, p, v, md)]
    base_probs += [("ambig", "", "", x) for x in verify_ambiguous(SCRIPTS / "wrapper.py", base)]
    if base_probs:
        print("  BASELINE NOT CLEAN — harness/engine disagree before mutation:", file=sys.stderr)
        for b in base_probs[:10]:
            print(f"    {b}", file=sys.stderr)
        return 2

    print("[mutation] injecting mutants", file=sys.stderr)
    killed, survived = [], []
    for name, repls, invariant, equiv in MUTANTS:
        wp, err = apply_mutant(name, repls)
        if wp is None:
            print(f"  SKIP {name}: {err}", file=sys.stderr)
            survived.append((name, "could not apply"))
            continue
        # killed if ANY slice case (or the ambiguous probe) reports a problem
        killer = None
        for sel, p, v in SLICE:
            probs = verify_one(wp, sel, p, v, md)
            if probs:
                killer = f"{sel}/{p}={v}: {probs[0]}"
                break
        if killer is None:
            probs = verify_ambiguous(wp, base)
            if probs:
                killer = f"ambiguous: {probs[0]}"
        shutil.rmtree(wp.parent, ignore_errors=True)
        if killer:
            killed.append((name, invariant, killer))
            print(f"  KILLED  {name:22s} <- {killer}", file=sys.stderr)
        else:
            survived.append((name, invariant))
            print(f"  SURVIVED {name:22s} ({invariant})", file=sys.stderr)

    score = len(killed) / len(MUTANTS) if MUTANTS else 1.0
    print(f"\n[mutation] score {len(killed)}/{len(MUTANTS)} = {score:.0%}", file=sys.stderr)
    report = {
        "score": f"{len(killed)}/{len(MUTANTS)}",
        "killed": [{"mutant": k, "invariant": i, "killer": w} for k, i, w in killed],
        "survived": [{"mutant": s[0], "note": s[1] if len(s) > 1 else ""} for s in survived],
    }
    (base / "report.json").write_text(json.dumps(report, indent=2))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tier-1 deterministic rigor for mdin-edit: exhaustive matrix + property fuzz +
synthetic-input fuzz + crash-class + fault-injection + coverage gate +
format-equivalence. All verdicts come from the INDEPENDENT oracle/spec in
`oracle.py` (never the engine's own logic).

Engine is exercised two ways:
  - in-process: monkeypatch argv + call wrapper.main() (fast; detects uncaught
    crashes directly).
  - subprocess: real `python3 wrapper.py ...` (faithful; the matrix runs both and
    asserts they agree).

Usage:
  python3 fuzz_mdin_edit.py [--quick] [--seed N] [--fuzz N] [--out DIR]
Exit 0 iff every assertion held (after any pending engine fixes).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
SCRIPTS = SKILL_DIR / "scripts"
WRAPPER = SCRIPTS / "wrapper.py"

sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(SCRIPTS))
import oracle as O          # noqa: E402  (independent oracle/spec)
import wrapper as W         # noqa: E402  (engine under test — in-process)


# ---- Result + report -----------------------------------------------------

@dataclass
class Result:
    env: dict | None
    code: object
    stdout: str
    stderr: str
    crashed_detail: str | None  # set if an uncaught exception / no JSON

    @property
    def crashed(self) -> bool:
        return self.env is None


@dataclass
class Report:
    passed: int = 0
    failed: int = 0
    findings: list = field(default_factory=list)       # (category, message, repro)
    statuses_seen: set = field(default_factory=set)
    codes_seen: set = field(default_factory=set)
    crashes_seen: int = 0

    def add(self, ok: bool, category: str, message: str, repro: str = ""):
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.findings.append({"category": category, "message": message, "repro": repro})

    def note_status(self, s):
        self.statuses_seen.add(s)

    def note_code(self, c):
        if c:
            self.codes_seen.add(c)


REP = Report()


def repro(selector, param, value, dry=False):
    d = " --dry-run" if dry else ""
    return (f'python3 "{WRAPPER}" --md-dir <scratch> --stage {selector} '
            f'--param {param} --value {value!r}{d}')


# ---- Engine runners ------------------------------------------------------

def run_inproc(md_dir, selector, param, value, dry=False) -> Result:
    argv = ["wrapper.py", "--md-dir", str(md_dir), "--stage", str(selector),
            "--param", str(param), "--value", str(value)]
    if dry:
        argv.append("--dry-run")
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = argv
    detail = None
    code = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                W.main()
            except SystemExit as e:
                code = e.code
    except BaseException as e:  # noqa: BLE001 — we WANT to catch engine crashes
        detail = "".join(traceback.format_exception_only(type(e), e)).strip()
    finally:
        sys.argv = old
    raw = out.getvalue()
    env = None
    if detail is None:
        try:
            env = json.loads(raw)
        except Exception:
            detail = "stdout not valid JSON"
    return Result(env=env, code=code, stdout=raw, stderr=err.getvalue(), crashed_detail=detail)


def run_subproc(md_dir, selector, param, value, dry=False) -> Result:
    cmd = ["python3", str(WRAPPER), "--md-dir", str(md_dir), "--stage", str(selector),
           "--param", str(param), "--value", str(value)]
    if dry:
        cmd.append("--dry-run")
    p = subprocess.run(cmd, capture_output=True, text=True)
    env = None
    detail = None
    try:
        env = json.loads(p.stdout)
    except Exception:
        detail = f"no JSON (rc={p.returncode}); stderr: {p.stderr.strip()[:300]}"
    return Result(env=env, code=p.returncode, stdout=p.stdout, stderr=p.stderr, crashed_detail=detail)


# ---- Scratch ------------------------------------------------------------

GT = O.load_ground_truth()


def make_scratch(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    for s in O.STAGES:
        (d / O.STAGE_FILE[s]).write_text(GT.text[s])
    return d


def restore(scratch: Path, stages=None):
    for s in (stages or O.STAGES):
        (scratch / O.STAGE_FILE[s]).write_text(GT.text[s])


def file_text(scratch: Path, stage: str) -> str:
    return (scratch / O.STAGE_FILE[stage]).read_text()


# ---- Expected per-file edit (allowed changes + must-equal) ---------------

def edit_expectation(stage: str, param: str, rendered: str):
    allowed = {("cntrl", param)}
    must = [("cntrl", param, rendered)]
    if param == "temp0" and GT.wt_temp0[stage]:
        allowed.add(("wt", "value2"))
        must.append(("wt", "value2", rendered))
    return allowed, must


# ---- The core comparator: engine vs spec + oracle -----------------------

def check_case(selector, param, value, scratch, *, also_sub=False):
    """Restore scratch, run the editor, assert engine == spec and (for edits)
    oracle-verify the file bytes. Records findings; returns nothing."""
    restore(scratch)
    exp = O.spec(selector, param, str(value), GT)
    res = run_inproc(scratch, selector, param, value)
    rp = repro(selector, param, value)

    # CRASH (no JSON envelope) — always a failure under the desired contract.
    if res.crashed:
        REP.crashes_seen += 1
        REP.add(False, "CRASH",
                f"no JSON envelope for {selector}/{param}={value!r}: {res.crashed_detail}", rp)
        # originals must still be intact even on a crash
        if any(file_text(scratch, s) != GT.text[s] for s in O.STAGES):
            REP.add(False, "CRASH_WROTE", f"files changed despite crash: {rp}", rp)
        return
    env = res.env

    if exp.global_error:
        REP.note_code(exp.global_error)
        errs = env.get("errors", [])
        good = (env.get("ok") is False) and any(exp.global_error in e for e in errs)
        REP.add(good, "GLOBAL_ERROR",
                f"{selector}/{param}={value!r}: expected {exp.global_error}, got ok={env.get('ok')} errors={errs}", rp)
        if any(file_text(scratch, s) != GT.text[s] for s in O.STAGES):
            REP.add(False, "WROTE_ON_ERROR", f"global error but a file changed: {rp}", rp)
        if also_sub:
            _agree(selector, param, value, scratch, env)
        return

    # per-file expectations
    REP.add(env.get("ok") == exp.ok, "OK_FLAG",
            f"{selector}/{param}={value!r}: expected ok={exp.ok} got {env.get('ok')}", rp)
    recs = {f["file"]: f for f in env.get("outputs", {}).get("files", [])}
    for st, (estatus, ecode) in exp.per_file.items():
        REP.note_status(estatus)
        REP.note_code(ecode)
        fn = O.STAGE_FILE[st]
        rec = recs.get(fn)
        if rec is None:
            REP.add(False, "MISSING_REC", f"{fn} absent from envelope: {rp}", rp)
            continue
        REP.add(rec["status"] == estatus, "STATUS",
                f"{fn} [{selector}/{param}={value!r}]: expected {estatus} got {rec['status']} ({rec.get('reason')})", rp)
        cur, orig = file_text(scratch, st), GT.text[st]
        if estatus in ("error", "skipped", "unchanged"):
            REP.add(cur == orig, "UNTOUCHED",
                    f"{fn} should be byte-identical for status {estatus}: {rp}", rp)
        elif estatus == "edited":
            allowed, must = edit_expectation(st, param, exp.rendered)
            try:
                O.verify_edit(orig, cur, allowed_changes=allowed, must_equal=must)
                REP.add(True, "ORACLE", "")
            except O.OracleError as e:
                REP.add(False, "ORACLE", f"{fn} [{rp}]: {e}", rp)
            REP.add(cur != orig, "BICOND_EDITED",
                    f"{fn} reported edited but bytes identical: {rp}", rp)
            if exp.warn_cut:
                REP.add(bool(rec.get("warnings")), "CUT_WARN",
                        f"{fn} cut in [6,8) but no editor warning: {rp}", rp)
                v = env.get("validation", {}).get("per_file", {}).get(fn, {})
                delib = any(f.get("deliberate") for f in v.get("findings", []))
                REP.add(delib, "CUT_DELIBERATE",
                        f"{fn} cut<8 not marked deliberate (would block ok): {rp}", rp)
            else:
                REP.add(not rec.get("warnings"), "NO_SPURIOUS_WARN",
                        f"{fn} unexpected warning {rec.get('warnings')}: {rp}", rp)

    # idempotency: re-run, expect byte-identical + 'unchanged' on previously-edited files
    after1 = {O.STAGE_FILE[s]: file_text(scratch, s) for s in O.STAGES}
    res2 = run_inproc(scratch, selector, param, value)
    if not res2.crashed:
        for s in O.STAGES:
            REP.add(file_text(scratch, s) == after1[O.STAGE_FILE[s]], "IDEMPOTENT",
                    f"{O.STAGE_FILE[s]} changed on identical re-run: {rp}", rp)
        for f in res2.env.get("outputs", {}).get("files", []):
            if f["file"] in {O.STAGE_FILE[st] for st, (s, _) in exp.per_file.items() if s == "edited"}:
                REP.add(f["status"] in ("unchanged",), "IDEMPOTENT_STATUS",
                        f"{f['file']} re-run status {f['status']} != unchanged: {rp}", rp)

    if also_sub:
        restore(scratch)
        _agree(selector, param, value, scratch, env)


def _agree(selector, param, value, scratch, inproc_env):
    """Run subprocess on a fresh restore; assert it agrees with in-process on
    ok + per-file statuses (catches main()-vs-import drift)."""
    restore(scratch)
    sub = run_subproc(scratch, selector, param, value)
    rp = repro(selector, param, value)
    if (inproc_env is None) != sub.crashed:
        REP.add(False, "MODE_DISAGREE", f"inproc vs subproc crash mismatch: {rp}", rp)
        return
    if sub.crashed:
        return
    if inproc_env.get("ok") != sub.env.get("ok"):
        REP.add(False, "MODE_DISAGREE", f"ok differs inproc vs subproc: {rp}", rp)
    a = {f["file"]: f["status"] for f in inproc_env.get("outputs", {}).get("files", [])}
    b = {f["file"]: f["status"] for f in sub.env.get("outputs", {}).get("files", [])}
    REP.add(a == b, "MODE_DISAGREE", f"per-file status differs inproc vs subproc: {rp} ({a} vs {b})", rp)


# ---- Tier 0: spec agrees with the human acceptance suite -----------------

def tier0_anchor(scratch):
    print("[tier0] anchor: spec vs test_acceptance.sh semantics", file=sys.stderr)
    # the acceptance suite's hard-coded cells, re-encoded:
    cells = [
        ("heat-1", "dt", "0.001", "edited", True),
        ("min1", "dt", "0.001", "error", False),          # PARAM_NOT_FOUND single
        ("heat-1", "dt", "0.01", None, False),             # OUT_OF_BOUNDS
        ("group:third-onward", "temp0", "310", "edited", True),
        ("relax", "cut", "7.0", "edited", True),
        ("press-1", "restraint_wt", "1.0", "edited", True),
        ("relax", "restraint_wt", "1.0", "error", False),  # SKIPPED_RESTRAINTS_OFF single
    ]
    for sel, p, v, st0, okexp in cells:
        e = O.spec(sel, p, v, GT)
        if st0 is None:
            REP.add(e.global_error is not None and not e.ok, "TIER0",
                    f"spec {sel}/{p}={v}: expected global error, got {e}")
        else:
            first_stage = O.GROUPS[sel][0] if sel in O.GROUPS else sel
            stt = e.per_file.get(first_stage, (None,))[0]
            REP.add(stt == st0 and e.ok == okexp, "TIER0",
                    f"spec {sel}/{p}={v}: expected {st0}/ok={okexp}, got {stt}/ok={e.ok}")
    # run the real acceptance suite once (subprocess), require green
    r = subprocess.run(["bash", str(SKILL_DIR / "test_acceptance.sh")],
                       capture_output=True, text=True)
    REP.add(r.returncode == 0, "TIER0_ACCEPTANCE",
            f"test_acceptance.sh exit={r.returncode}: {r.stderr.strip()[-400:]}")


# ---- Tier 1a: exhaustive matrix -----------------------------------------

def value_classes(stage, param):
    """Yield (value, label) covering the contract classes for this cell."""
    cur = GT.current.get(stage, {}).get(param)
    out = []
    change = {"dt": "0.001", "cut": "7.0", "temp0": "311",
              "restraint_wt": "1.0", "nstlim": "111"}[param]
    out.append((change, "change"))
    if cur is not None:
        out.append((cur, "at-target"))
    oob = {"dt": "0.01", "cut": "20", "temp0": "500",
           "restraint_wt": "-1", "nstlim": "0"}[param]
    out.append((oob, "oob"))
    out.append(("abc", "malformed"))
    out.append(("nan", "crash"))
    if param == "nstlim":
        out.append(("1.5", "noninteger"))
        out.append(("1e999", "crash-inf"))
    return out


def tier1_matrix(scratch):
    print("[tier1] exhaustive matrix (in-process + subprocess agreement)", file=sys.stderr)
    n = 0
    for stage in O.STAGES:
        selectors = ["single"]
        for sel in ["group:third-onward", "group:all"]:
            if stage in O.GROUPS[sel]:
                selectors.append(sel)
        for param in O.SUPPORTED:
            for value, _label in value_classes(stage, param):
                for sel in selectors:
                    selector = stage if sel == "single" else sel
                    # only run the group once per group (not per member) to avoid dup:
                    if sel != "single" and stage != O.GROUPS[sel][0]:
                        continue
                    check_case(selector, param, value, scratch, also_sub=True)
                    n += 1
    print(f"[tier1] matrix cases: {n}", file=sys.stderr)


# ---- Tier 1b: property fuzz (random, fixed seed) -------------------------

def rand_value(rng, param):
    """Boundary-weighted random value (string) for a param."""
    pick = rng.random()
    if param == "nstlim":
        if pick < 0.5:
            return str(rng.randint(1, 5_000_000))
        if pick < 0.7:
            return rng.choice(["0", "-5", "1.5", "2.0", "1e3", "1e7"])
        return rng.choice(["inf", "nan", "1e999", "abc", "", "1_000", "０", "0x10"])
    lo_hi = {"dt": (0.0, 0.004), "cut": (3.0, 14.0),
             "temp0": (0.0, 500.0), "restraint_wt": (-2.0, 50.0)}[param]
    if pick < 0.6:
        v = rng.uniform(*lo_hi)
        return rng.choice([repr(v), f"{v:.4f}", f"{v:.1f}"])
    if pick < 0.8:  # near boundaries
        b = {"dt": [0.0, 0.002, 0.001], "cut": [6.0, 8.0, 12.0],
             "temp0": [0.0, 400.0], "restraint_wt": [0.0]}[param]
        return repr(rng.choice(b))
    return rng.choice(["inf", "nan", "abc", "", "+0.002", "-0.0", "1e999", "２"])


def tier1_property(scratch, n, seed):
    print(f"[tier1] property fuzz: {n} cases (seed={seed})", file=sys.stderr)
    rng = random.Random(seed)
    sel_pool = list(O.STAGES) + list(O.GROUPS)
    for _ in range(n):
        selector = rng.choice(sel_pool)
        param = rng.choice(O.SUPPORTED)
        value = rand_value(rng, param)
        check_case(selector, param, value, scratch, also_sub=False)


# ---- Tier 1c: format-equivalence ----------------------------------------

def tier1_format_equivalence(scratch):
    print("[tier1] format-equivalence", file=sys.stderr)
    groups = [
        ("heat-1", "dt", ["0.002", "+0.002", "0.0020", "2e-3", ".002", "0.00200"]),
        ("relax", "cut", ["9", "9.0", "9.00", "+9.0", "9e0"]),
        ("relax", "temp0", ["300", "300.0", "3e2", "+300"]),
        ("heat-1", "nstlim", ["50000", "5e4", "+50000"]),
    ]
    for stage, param, spellings in groups:
        rendered = set()
        for v in spellings:
            restore(scratch)
            res = run_inproc(scratch, stage, param, v)
            if res.crashed:
                REP.add(False, "FMT_EQUIV", f"{stage}/{param}={v!r} crashed", repro(stage, param, v))
                continue
            f = {x["file"]: x for x in res.env.get("outputs", {}).get("files", [])}.get(O.STAGE_FILE[stage])
            tok = file_text(scratch, stage)
            rendered.add(tok)
        REP.add(len(rendered) == 1, "FMT_EQUIV",
                f"{stage}/{param}: equivalent spellings produced {len(rendered)} distinct files")


# ---- Tier 1d: synthetic-input fuzz (style variants) ---------------------

SYN_BASE = """Synthetic MD
 &cntrl
  imin = 0,
  nstlim = 50000,
{DT}
  cut = 9.0,
  ntr = 1,
  restraint_wt = 5.0,
  restraintmask = "!:WAT,Cl-,K+,Na+ & !@H=",
  ntc = 2, ntf = 2,
  temp0 = 300.0,
 /
"""


def tier1_synthetic(scratch_base):
    print("[tier1] synthetic-input fuzz (style variants)", file=sys.stderr)
    d = scratch_base / "syn"
    d.mkdir(parents=True, exist_ok=True)
    variants = {
        "spaces":   "  dt = 0.002,",
        "nospace":  "  dt=0.002,",
        "tabs":     "  dt\t=\t0.002,",
        "extra":    "  dt   =   0.002,",
        "comment":  "  dt = 0.002,   ! the timestep",
        "attarget": "  dt = 0.001,",            # already at the target we'll set
        "crlf":     "  dt = 0.002,",            # whole file CRLF below
        "nonewline_eof": "  dt = 0.002,",
    }
    for name, dtline in variants.items():
        text = SYN_BASE.replace("{DT}", dtline)
        if name == "crlf":
            text = text.replace("\n", "\r\n")
        if name == "nonewline_eof":
            text = text.rstrip("\n")
        # only heat-1.in matters; create a one-file md dir
        sd = d / name
        sd.mkdir(exist_ok=True)
        f = sd / "heat-1.in"
        f.write_text(text, newline="")  # preserve exact bytes incl CRLF
        before = f.read_bytes()
        res = run_inproc(sd, "heat-1", "dt", "0.001")
        rp = f"syn[{name}] dt->0.001"
        if res.crashed:
            REP.add(False, "SYNTHETIC", f"{name}: crashed {res.crashed_detail}", rp)
            continue
        env = res.env
        recs = {x["file"]: x for x in env.get("outputs", {}).get("files", [])}
        rec = recs.get("heat-1.in", {})
        after = f.read_bytes()
        if rec.get("status") == "edited":
            # must be a clean numeric-only edit to 0.001, nothing else
            try:
                O.verify_edit(text, after.decode(), allowed_changes={("cntrl", "dt")},
                              must_equal=[("cntrl", "dt", "0.001")])
                REP.add(True, "SYNTHETIC", "")
            except O.OracleError as e:
                REP.add(False, "SYNTHETIC", f"{name}: dirty edit: {e}", rp)
            # idempotency on the variant
            r2 = run_inproc(sd, "heat-1", "dt", "0.001")
            REP.add(f.read_bytes() == after, "SYNTHETIC", f"{name}: not idempotent", rp)
        elif rec.get("status") in ("unchanged",):
            REP.add(name == "attarget", "SYNTHETIC",
                    f"{name}: unexpected 'unchanged' (only attarget should)", rp)
        elif rec.get("status") in ("error", "skipped"):
            # graceful refusal is acceptable for a weird variant; must not corrupt
            REP.add(after == before, "SYNTHETIC", f"{name}: refused but file changed", rp)
        else:
            REP.add(False, "SYNTHETIC", f"{name}: unexpected status {rec.get('status')}", rp)


# ---- Tier 1e: fault-injection (safety nets are live) --------------------

def tier1_faults(scratch_base):
    print("[tier1] fault-injection (safety nets live)", file=sys.stderr)
    d = scratch_base / "faults"
    d.mkdir(parents=True, exist_ok=True)

    # NAMELIST_NOT_FOUND: unterminated &cntrl
    sd = d / "noclose"; sd.mkdir(exist_ok=True)
    (sd / "heat-1.in").write_text(" &cntrl\n  dt = 0.002,\n")
    before = (sd / "heat-1.in").read_text()
    r = run_inproc(sd, "heat-1", "dt", "0.001")
    _expect_code(r, "NAMELIST_NOT_FOUND", "fault:noclose")
    REP.add(not r.crashed and (sd / "heat-1.in").read_text() == before, "FAULT_UNTOUCHED",
            "noclose modified file")

    # AMBIGUOUS_PARAM: duplicate key in &cntrl
    sd = d / "dup"; sd.mkdir(exist_ok=True)
    (sd / "heat-1.in").write_text(" &cntrl\n  dt = 0.002,\n  dt = 0.002,\n /\n")
    before = (sd / "heat-1.in").read_text()
    r = run_inproc(sd, "heat-1", "dt", "0.001")
    _expect_code(r, "AMBIGUOUS_PARAM", "fault:dup")
    REP.add(not r.crashed and (sd / "heat-1.in").read_text() == before, "FAULT_UNTOUCHED",
            "dup modified file")

    # STAGE_FILE_MISSING: empty md dir
    sd = d / "empty"; sd.mkdir(exist_ok=True)
    r = run_inproc(sd, "heat-1", "dt", "0.001")
    _expect_code(r, "STAGE_FILE_MISSING", "fault:empty")

    # MD_DIR_NOT_FOUND
    r = run_inproc(d / "does-not-exist", "heat-1", "dt", "0.001")
    _expect_code(r, "MD_DIR_NOT_FOUND", "fault:nodir")

    # UNKNOWN_STAGE / UNSUPPORTED_PARAM
    sc = make_scratch(d, "ok")
    r = run_inproc(sc, "heat-9", "dt", "0.001")
    _expect_code(r, "UNKNOWN_STAGE", "fault:badstage")
    r = run_inproc(sc, "heat-1", "gamma_ln", "2.0")
    _expect_code(r, "UNSUPPORTED_PARAM", "fault:badparam")


def _expect_code(res: Result, code: str, label: str):
    if res.crashed:
        REP.add(False, "FAULT", f"{label}: crashed instead of {code}: {res.crashed_detail}")
        return
    errs = res.env.get("errors", [])
    REP.note_code(code)
    REP.add(res.env.get("ok") is False and any(code in e for e in errs), "FAULT",
            f"{label}: expected {code}, got ok={res.env.get('ok')} errors={errs}")


# ---- Coverage gate ------------------------------------------------------

REQUIRED_STATUSES = {"edited", "unchanged", "skipped", "error"}
REQUIRED_CODES = {
    "UNSUPPORTED_PARAM", "OUT_OF_BOUNDS", "INVALID_VALUE", "NONINTEGER_VALUE",
    "PARAM_NOT_FOUND", "SKIPPED_RESTRAINTS_OFF", "NAMELIST_NOT_FOUND",
    "AMBIGUOUS_PARAM", "STAGE_FILE_MISSING", "MD_DIR_NOT_FOUND", "UNKNOWN_STAGE",
}


def coverage_gate():
    miss_s = REQUIRED_STATUSES - REP.statuses_seen
    miss_c = REQUIRED_CODES - REP.codes_seen
    REP.add(not miss_s, "COVERAGE", f"statuses never exercised: {miss_s}")
    REP.add(not miss_c, "COVERAGE", f"error codes never exercised: {miss_c}")
    REP.add(REP.crashes_seen >= 0, "COVERAGE_INFO", "")  # crashes are reported separately


# ---- Main ---------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--seed", type=int, default=20260608)
    ap.add_argument("--fuzz", type=int, default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    fuzz_n = args.fuzz if args.fuzz is not None else (300 if args.quick else 20000)
    out_dir = Path(args.out) if args.out else (SKILL_DIR / "test-runs" /
                                               f"tier1-{'quick' if args.quick else 'full'}")
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = make_scratch(out_dir, "scratch")

    t0 = time.time()
    tier0_anchor(scratch)
    tier1_matrix(scratch)
    tier1_property(scratch, fuzz_n, args.seed)
    tier1_format_equivalence(scratch)
    tier1_synthetic(out_dir)
    tier1_faults(out_dir)
    coverage_gate()
    dt = time.time() - t0

    report = {
        "passed": REP.passed, "failed": REP.failed, "elapsed_s": round(dt, 1),
        "crashes_seen": REP.crashes_seen,
        "statuses_seen": sorted(REP.statuses_seen), "codes_seen": sorted(REP.codes_seen),
        "findings": REP.findings[:500],
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    # group findings by category
    cats = {}
    for f in REP.findings:
        cats.setdefault(f["category"], 0)
        cats[f["category"]] += 1
    print(f"\n[tier1] {REP.passed} passed, {REP.failed} failed, "
          f"{REP.crashes_seen} crashes, {dt:.1f}s", file=sys.stderr)
    if cats:
        print("[tier1] findings by category:", file=sys.stderr)
        for c, n in sorted(cats.items(), key=lambda x: -x[1]):
            print(f"    {n:5d}  {c}", file=sys.stderr)
        print(f"[tier1] report: {out_dir/'report.json'}", file=sys.stderr)
        # print a few sample findings per category
        seen = {}
        for f in REP.findings:
            if seen.get(f["category"], 0) < 2:
                seen[f["category"]] = seen.get(f["category"], 0) + 1
                print(f"    e.g. [{f['category']}] {f['message'][:200]}", file=sys.stderr)
    return 1 if REP.failed else 0


if __name__ == "__main__":
    sys.exit(main())

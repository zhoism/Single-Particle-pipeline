#!/usr/bin/env python3
"""Independent oracle for the amber-recover failure detector.

This is a SECOND, deliberately-different implementation of the crash verdict
(line scan + substring tests, vs the wrapper's compiled-regex-on-full-text +
energy-block split). It (1) asserts the ground-truth label on the REAL pmemd
mdout fixtures captured during the build, (2) asserts the wrapper's detector
AGREES with this oracle on every fixture and fault-injected case, and (3) runs
synthetic fault injection. Stdlib only; runs under py3.9 + py3.11.

Run:  python3 test_detector.py
"""
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent / "scripts"))
import wrapper as W  # noqa: E402  (the wrapper under test)


# ---- Independent classifier (different code path from the wrapper) ----------

def oracle_crashed(out_text, stderr_text="", rc=None):
    lines = out_text.splitlines()
    low = out_text.lower()
    serr = stderr_text.lower()

    done_markers = ("total wall time", "final results",
                    "maximum number of minimization", "final performance info")
    finished = any(m in ln.lower() for ln in lines for m in done_markers)

    has_nan = "NaN" in out_text                      # NaN is sticky -> garbage
    has_inf = bool(re.search(r"[-+]?Infinity\b", out_text, re.I))  # IEEE inf -> garbage
    has_shake = ("coordinate resetting cannot be accomplished" in low
                 or "shake" in low and ("fail" in low or "not converge" in low))
    has_abnormal = "terminated abnormally" in low or "terminated abnormally" in serr
    has_box = ("changed too much" in low or "skinnb" in low
               or "unit cell" in low and "too small" in low)

    # last energy block: from the final "NSTEP =" line, the next ~14 lines
    idx = [i for i, ln in enumerate(lines) if re.search(r"NSTEP\s*=\s*\d+", ln)]
    final_block = "\n".join(lines[idx[-1]: idx[-1] + 14]) if idx else ""
    final_bad = bool(final_block) and ("NaN" in final_block
                                       or "Infinity" in final_block
                                       or "******" in final_block)

    temp_blowup = False
    for m in re.finditer(r"TEMP\(K\)\s*=\s*(NaN|\*+|[-\d.]+)", out_text):
        tok = m.group(1)
        if tok == "NaN" or tok.startswith("*"):
            temp_blowup = True
        else:
            try:
                temp_blowup = temp_blowup or abs(float(tok)) > 1000.0
            except ValueError:
                pass

    crashed = (not finished) or (rc is not None and rc != 0) or has_nan or has_inf \
        or has_shake or has_abnormal or has_box or final_bad or temp_blowup
    fatal = has_nan or has_inf or has_shake or has_box or temp_blowup or final_bad
    if not crashed:
        cls = "HEALTHY"
    elif fatal:
        cls = "INSTABILITY"
    else:
        cls = "INCOMPLETE"
    return crashed, cls


# ---- Synthetic mdout builders ----------------------------------------------

def clean_dyn(nblocks=3):
    head = "| Run on a Tuesday\n APPROXIMATING direct energy using CUBIC SPLINE\n"
    body = ""
    for i in range(1, nblocks + 1):
        n = i * 500
        body += (f" NSTEP = {n:8d}   TIME(PS) = {n*0.002:10.3f}  TEMP(K) = "
                 f"{300.0+i:7.2f}  PRESS = 0.0\n"
                 f" Etot   = {-7000.0-i:14.4f}  EKtot   = {1500.0:14.4f}  "
                 f"EPtot      = {-8500.0:14.4f}\n"
                 " BOND   =        10.0  ANGLE   =        20.0  DIHED      =  30.0\n")
    tail = ("|  Setup wall time:           0    seconds\n"
            "|  Total wall time:          11    seconds     0.00 hours\n")
    return head + body + tail


# ---- Cases ------------------------------------------------------------------

def main():
    fails = []

    def check(name, out_text, exp_crashed, exp_cls, stderr="", rc=None):
        o_crashed, o_cls = oracle_crashed(out_text, stderr, rc)
        wd = W.detect_failure(out_text, stderr_text=stderr, rc=rc)
        ok = True
        # 1. oracle matches ground truth
        if o_crashed != exp_crashed or o_cls != exp_cls:
            ok = False
            print(f"  ORACLE-MISMATCH {name}: oracle=({o_crashed},{o_cls}) "
                  f"expected=({exp_crashed},{exp_cls})")
        # 2. wrapper agrees with oracle (independence check)
        if wd["crashed"] != o_crashed or wd["classification"] != o_cls:
            ok = False
            print(f"  WRAPPER-DISAGREES {name}: wrapper=({wd['crashed']},"
                  f"{wd['classification']}) oracle=({o_crashed},{o_cls})")
        print(("PASS" if ok else "FAIL") + f": {name} "
              f"-> crashed={wd['crashed']} class={wd['classification']} "
              f"sig={wd['signatures']}")
        if not ok:
            fails.append(name)

    print("== real pmemd mdout fixtures (ground truth from the build) ==")
    check("clean_production", (FIX / "clean_production.out").read_text(),
          False, "HEALTHY")
    check("crash_unmin_shake_overflow",
          (FIX / "crash_unmin_shake_overflow.out").read_text(), True, "INSTABILITY")
    check("crash_nan_silent_finished (silent garbage: NaN but DONE+rc0)",
          (FIX / "crash_nan_silent_finished.out").read_text(), True, "INSTABILITY")
    check("recovered_early_overflow (HEALTHY despite transient ****)",
          (FIX / "recovered_early_overflow.out").read_text(), False, "HEALTHY")

    print("== synthetic fault injection ==")
    base = clean_dyn()
    check("clean_dynamics", base, False, "HEALTHY")
    check("nan_injected", base.replace("EPtot      =     -8500.0000",
                                       "EPtot      =            NaN", 1)
          if "EPtot      =     -8500.0000" in base else base + " Etot = NaN\n",
          True, "INSTABILITY")
    check("shake_injected",
          base + "\n     Coordinate resetting cannot be accomplished,\n     deviation is too large\n",
          True, "INSTABILITY")
    # the silent-pass class the adversarial review found: IEEE Infinity divergence
    # with a normal-termination banner present (rc 0) must NOT be called healthy
    check("inf_injected (IEEE Infinity, banner present)",
          base.replace("-8500.0000", "  Infinity", 1)
          if "-8500.0000" in base else base + " Etot = Infinity\n",
          True, "INSTABILITY")
    check("truncated_killed (no banner)",
          "\n".join(base.splitlines()[:6]), True, "INCOMPLETE")
    check("finite_vlimit_clamp (soft, tolerated)",
          base.replace("|  Setup wall time",
                       "vlimit exceeded for step 5; vmax = 21.0\n|  Setup wall time", 1),
          False, "HEALTHY")
    check("temp_blowup_final_block",
          base.replace("TEMP(K) = 303.00", "TEMP(K) = 5000.00")
          if "TEMP(K) = 303.00" in base else base + " NSTEP = 9999\n TEMP(K) = 5000.00\n Etot = 1.0\n",
          True, "INSTABILITY")
    check("abnormal_termination_stderr", "\n".join(base.splitlines()[:6]),
          True, "INCOMPLETE", stderr="STOP PMEMD Terminated Abnormally!")
    check("nonzero_rc_on_finished_run", base, True, "INCOMPLETE", rc=9)
    check("empty_out", "", True, "INCOMPLETE")

    print(f"\n{'ALL DETECTOR ORACLE CASES PASS' if not fails else 'FAILURES: ' + ','.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Oracle + regression test for the structural SYSTEM_NOT_NEUTRAL gate.

Background: the old gate scraped leap.log's "unperturbed charge" line, which
reports the PRE-neutralization charge and never matched the skill's real logs ->
the gate was vacuous on 100% of production runs. The fix reads the BUILT
comp_oct prmtop CHARGE block directly (prmtop_net_charge, sum / 18.2223).

This test:
  (A) ORACLE  - prmtop_net_charge on synthetic prmtops (neutral / charged /
      within-tolerance / malformed), and the > 0.5 e fire threshold.
  (B) REGRESSION - over every real comp_oct.top found under project-prime,
      assert |net| <= 0.5 e (proves the gate does NOT false-alarm on any real
      GREEN build). Skipped cleanly if no real prmtops are present.

Stdlib only; runs under py3.9 (system) and py3.11 (conda prime-amber).
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
WRAPPER = HERE.parents[1] / "scripts" / "wrapper.py"
PP_ROOT = HERE.parents[3]            # project-prime/
SCALE = 18.2223                      # AMBER prmtop charge pre-scale factor
TOL = 0.5                            # gate fires when |net| > 0.5 e


def load_wrapper():
    spec = importlib.util.spec_from_file_location("tleap_wrapper", WRAPPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # safe: main() is __main__-guarded
    return mod


def mk_prmtop(charges_e, *, omit_charge=False, garbage=False):
    """Write a minimal prmtop with a CHARGE block (charges given in electrons,
    stored pre-scaled) followed by another %FLAG so the block terminator matches.
    """
    if omit_charge:
        body = "%FLAG MASS\n%FORMAT(5E16.8)\n  1.00800000\n"
    elif garbage:
        body = ("%FLAG CHARGE\n%FORMAT(5E16.8)\n  not_a_number  1.0\n"
                "%FLAG MASS\n%FORMAT(5E16.8)\n  1.0\n")
    else:
        vals = "  ".join(f"{c * SCALE:.8E}" for c in charges_e) or "  "
        body = (f"%FLAG CHARGE\n%FORMAT(5E16.8)\n  {vals}\n"
                "%FLAG MASS\n%FORMAT(5E16.8)\n  1.00800000\n")
    f = tempfile.NamedTemporaryFile("w", suffix=".top", delete=False)
    f.write("%VERSION ...\n%FLAG TITLE\n%FORMAT(20a4)\ntest\n" + body)
    f.close()
    return Path(f.name)


def main():
    mod = load_wrapper()
    net = mod.prmtop_net_charge
    fails = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)

    print("(A) ORACLE — synthetic prmtops")
    # neutral system
    v = net(mk_prmtop([+1.0, -1.0]))
    check("neutral -> ~0.0", v is not None and abs(v) < 1e-6)
    check("neutral -> gate does NOT fire", not (v is not None and abs(v) > TOL))
    # +2 cation
    v = net(mk_prmtop([+1.0, +1.0]))
    check("+2 e -> ~2.0", v is not None and abs(v - 2.0) < 1e-6)
    check("+2 e -> gate FIRES", v is not None and abs(v) > TOL)
    # -1 anion
    v = net(mk_prmtop([-1.0]))
    check("-1 e -> ~-1.0 and FIRES", v is not None and abs(v + 1.0) < 1e-6 and abs(v) > TOL)
    # within tolerance (rounding noise) -> must NOT fire
    v = net(mk_prmtop([+0.3]))
    check("+0.3 e -> within tol, no fire", v is not None and not (abs(v) > TOL))
    # just over the boundary -> must fire
    v = net(mk_prmtop([+0.6]))
    check("+0.6 e -> FIRES", v is not None and abs(v) > TOL)
    # malformed -> None (treated as 'could not verify', never a false gate)
    check("missing CHARGE block -> None", net(mk_prmtop([], omit_charge=True)) is None)
    check("garbage token -> None", net(mk_prmtop([], garbage=True)) is None)
    check("nonexistent file -> None", net(Path("/no/such/file.top")) is None)

    print("(B) REGRESSION — real comp_oct.top builds (no false-alarm)")
    reals = sorted(PP_ROOT.glob("**/comp_oct.top"))
    if not reals:
        print("  SKIP  no real comp_oct.top found (gitignored run dirs absent)")
    else:
        worst = 0.0
        for p in reals:
            v = net(p)
            ok = v is not None and abs(v) <= TOL
            worst = max(worst, abs(v) if v is not None else 1e9)
            if not ok:
                check(f"{p.relative_to(PP_ROOT)} neutral", False)
        check(f"all {len(reals)} real builds |net|<=0.5 (worst={worst:.6f} e)", worst <= TOL)

    print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()

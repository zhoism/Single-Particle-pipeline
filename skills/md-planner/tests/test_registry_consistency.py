#!/usr/bin/env python3
"""Drift guard for the md-planner registry.

The registry (scripts/registry.py) is a dict literal — fast + deterministic at
runtime, but it can drift from the skills it transcribes. This test closes that:
it checks every registry CLI flag and every declared output key against the ACTUAL
target wrapper source (not the incomplete SKILL.md metadata), and confirms the
vendored check_amber bounds match the canonical md-param-check source. Runs without
AMBER, under py3.9 + py3.11.

Run:  python3 test_registry_consistency.py
"""
import filecmp
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
SKILLS = HERE.parents[1]                        # .../skills (tests -> md-planner -> skills)
sys.path.insert(0, str(SCRIPTS))
import registry as R  # noqa: E402


def main():
    fails = []

    def chk(cond, msg):
        print(("PASS" if cond else "FAIL") + ": " + msg)
        if not cond:
            fails.append(msg)

    print("== registry CLI flags + output keys vs actual wrapper source ==")
    for skill, reg in R.REGISTRY.items():
        wp = SKILLS / skill / "scripts" / "wrapper.py"
        chk(wp.is_file(), f"{skill}: wrapper.py exists")
        if not wp.is_file():
            continue
        src = wp.read_text()
        # every input + param CLI flag must be a real argparse flag
        flags = [spec["cli"] for spec in reg["inputs"].values()]
        flags += [spec["cli"] for spec in reg.get("params", {}).values()]
        for fl in flags:
            # quoted anywhere in the source — robust to argparse aliases like
            # add_argument("--ligand-resname", "--name", ...)
            present = (f'"{fl}"' in src) or (f"'{fl}'" in src)
            chk(present, f"{skill}: flag {fl} is a real argparse arg")
        # every declared output key must appear as an emitted key in the source
        for key in reg["outputs"]:
            present = (f'"{key}"' in src) or (f"'{key}'" in src)
            chk(present, f"{skill}: output key {key} emitted by wrapper")

    print("== vendored check_amber == canonical md-param-check source ==")
    vend = SCRIPTS / "check_amber_vendored.py"
    # canonical lives in the vault's .claude/skills; locate it relative to project-prime
    canon_candidates = [
        SKILLS.parents[0] / ".." / "Single Particle" / ".claude" / "skills"
        / "md-param-check" / "checks" / "check_amber.py",
        Path.home() / "Downloads" / "Single Particle" / "Single Particle" / ".claude"
        / "skills" / "md-param-check" / "checks" / "check_amber.py",
    ]
    canon = next((c for c in canon_candidates if c.is_file()), None)
    if canon is None:
        chk(False, "canonical check_amber.py located")
    else:
        chk(filecmp.cmp(str(vend), str(canon), shallow=False),
            "vendored check_amber byte-identical to canonical source")
        # and the bound constants the validator imports match the canonical values
        import check_amber_vendored as cav  # noqa: E402
        ctext = canon.read_text()
        for name in ("CUT_MIN", "CUT_MAX", "DT_MAX_SHAKE", "GAMMA_LN_MIN", "GAMMA_LN_MAX"):
            m = re.search(rf"^{name}\s*=\s*([\d.]+)", ctext, re.M)
            chk(m is not None and abs(float(m.group(1)) - getattr(cav, name)) < 1e-12,
                f"bound {name} matches canonical")

    print("\n" + ("REGISTRY CONSISTENCY OK" if not fails else "FAILURES: " + ",".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

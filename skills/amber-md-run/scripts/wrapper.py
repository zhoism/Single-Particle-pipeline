#!/usr/bin/env python3
"""amber-md-run wrapper (Phase 3 Stage 4).

Generate the standard 6-step explicit-solvent MD input chain (3 minimizations ->
heat -> density -> production) for a solvated topology, write a portable run.sh,
and (unless --dry-run) execute the chain locally to completion.

One exec call per skill turn. JSON envelope to stdout; progress to stderr.

Namelists are generated to pass the project's md-param-check gates:
  - dt <= 0.002 with SHAKE (ntc=2, ntf=2)
  - 8.0 <= cut <= 12.0
  - ntt=3 Langevin with gamma_ln in [1,5], ig=-1
  - temp0 == &wt value2 when nmropt=1 (avoids the heat-3 mismatch bug)
  - ntp=1, barostat=2 (MC) for NPT; iwrap=1 for production
No hardcoded AMBERHOME / engine path (the advisor's /Application/... is the
anti-example): the engine is resolved from --engine-home/bin then PATH.

Engine seam (Gap_Remote_HPC_Backend): --engine selects pmemd (serial, default),
pmemd.MPI (mpirun -np N), or sander. Swapping to a remote pmemd.cuda + scheduler
later touches only this seam, not the recipe files.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_NAME = "amber-md-run"
DT = 0.002  # ps; 2 fs with SHAKE


# ---- Envelope ------------------------------------------------------------

def envelope(ok: bool, dry_run: bool,
             outputs: dict[str, Any] | None = None,
             validation: dict[str, Any] | None = None,
             errors: list[str] | None = None) -> str:
    return json.dumps({
        "ok": ok, "skill": SKILL_NAME, "dry_run": dry_run,
        "outputs": outputs or {}, "validation": validation or {},
        "errors": errors or [],
    }, indent=2)


def emit_and_exit(*, ok: bool, dry_run: bool,
                  outputs: dict[str, Any] | None = None,
                  validation: dict[str, Any] | None = None,
                  errors: list[str] | None = None, code: int = 0) -> None:
    print(envelope(ok=ok, dry_run=dry_run, outputs=outputs,
                   validation=validation, errors=errors))
    sys.exit(code)


# ---- Binary resolution ---------------------------------------------------

def resolve_engine(engine: str, engine_home: str | None) -> str | None:
    """engine_home/bin first, then PATH. Returns absolute path or None."""
    if engine_home:
        cand = Path(engine_home).expanduser() / "bin" / engine
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which(engine)


# ---- Namelist templates --------------------------------------------------

def steps_to_run(which: str) -> list[str]:
    if which == "min":
        return ["min1", "min2", "min3"]
    return ["min1", "min2", "min3", "heat", "density", "product"]


def gen_inputs(*, cut: float, heat_ps: float, density_ps: float, sim_ps: float
               ) -> dict[str, str]:
    """Return {stage: namelist_text}. Restraint mask holds solute, frees solvent."""
    solute = "'!:WAT,Na+,Cl-'"
    solute_heavy = "'(!:WAT,Na+,Cl-) & (!@H=)'"
    heat_steps = int(round(heat_ps / DT))
    dens_steps = int(round(density_ps / DT))
    prod_steps = int(round(sim_ps / DT))
    # ~500 frames in the production trajectory.
    prod_ntwx = max(1, prod_steps // 500)

    files: dict[str, str] = {}

    files["min1"] = f"""min1: hold solute, minimize solvent only
 &cntrl
  imin=1, ncyc=500, maxcyc=1000, drms=0.001,
  ntb=1, cut={cut},
  ntr=1, restraint_wt=500.0, restraintmask={solute},
  ntpr=100, ntwr=1000,
 /
"""
    files["min2"] = f"""min2: hold heavy solute atoms, minimize solvent + H
 &cntrl
  imin=1, ncyc=500, maxcyc=1000, drms=0.001,
  ntb=1, cut={cut},
  ntr=1, restraint_wt=500.0, restraintmask={solute_heavy},
  ntpr=100, ntwr=1000,
 /
"""
    files["min3"] = f"""min3: unrestrained full-system minimization
 &cntrl
  imin=1, ncyc=500, maxcyc=1000, drms=0.001,
  ntb=1, cut={cut}, ntr=0,
  ntpr=100, ntwr=1000,
 /
"""
    files["heat"] = f"""heat: 0 -> 300 K, NVT, Langevin, weak solute restraint
 &cntrl
  imin=0, irest=0, ntx=1,
  nstlim={heat_steps}, dt={DT},
  ntc=2, ntf=2, cut={cut},
  ntb=1, ntp=0,
  ntt=3, gamma_ln=2.0, ig=-1,
  tempi=0.0, temp0=300.0,
  ntr=1, restraint_wt=2.0, restraintmask={solute},
  ntpr=500, ntwx=500, ntwr=5000,
  nmropt=1,
 /
 &wt TYPE='TEMP0', istep1=0, istep2={heat_steps}, value1=0.0, value2=300.0 /
 &wt TYPE='END' /
"""
    files["density"] = f"""density: NPT equilibration, MC barostat, weak solute restraint
 &cntrl
  imin=0, irest=1, ntx=5,
  nstlim={dens_steps}, dt={DT},
  ntc=2, ntf=2, cut={cut},
  ntb=2, ntp=1, barostat=2, pres0=1.0, taup=2.0,
  ntt=3, gamma_ln=2.0, ig=-1, temp0=300.0,
  ntr=1, restraint_wt=2.0, restraintmask={solute},
  iwrap=1, ntpr=500, ntwx=500, ntwr=5000,
 /
"""
    files["product"] = f"""production: NPT, MC barostat, unrestrained, {sim_ps} ps
 &cntrl
  imin=0, irest=1, ntx=5,
  nstlim={prod_steps}, dt={DT},
  ntc=2, ntf=2, cut={cut},
  ntb=2, ntp=1, barostat=2, pres0=1.0, taup=2.0,
  ntt=3, gamma_ln=2.0, ig=-1, temp0=300.0,
  ntr=0,
  iwrap=1, ioutfm=1, ntpr=500, ntwx={prod_ntwx}, ntwr=5000,
 /
"""
    return files


# Per-stage run wiring: (input coords source, needs -ref restraint, is minimization)
CHAIN = {
    "min1":    {"c": "TOP_CRD", "ref": True,  "min": True},
    "min2":    {"c": "min1",    "ref": True,  "min": True},
    "min3":    {"c": "min2",    "ref": False, "min": True},
    "heat":    {"c": "min3",    "ref": True,  "min": False},
    "density": {"c": "heat",    "ref": True,  "min": False},
    "product": {"c": "density", "ref": False, "min": False},
}


def gen_run_sh(*, engine: str, engine_path: str, ncpus: int, top_name: str,
               crd_name: str, stages: list[str]) -> str:
    if engine == "pmemd.MPI":
        launcher = f"mpirun -np {ncpus} {engine_path}"
    else:
        launcher = engine_path
    lines = ["#!/usr/bin/env bash",
             "# Generated by amber-md-run. Run from the md/ directory.",
             "set -euo pipefail", ""]
    for st in stages:
        info = CHAIN[st]
        cin = crd_name if info["c"] == "TOP_CRD" else f"{info['c']}.rst"
        cmd = [launcher, "-O", "-i", f"{st}.in", "-o", f"{st}.out",
               "-p", top_name, "-c", cin, "-r", f"{st}.rst"]
        if not info["min"]:
            cmd += ["-x", f"{st}.nc"]
        if info["ref"]:
            ref = crd_name if info["c"] == "TOP_CRD" else f"{info['c']}.rst"
            cmd += ["-ref", ref]
        lines.append(f'echo "[md] {st}" >&2')
        lines.append(" ".join(cmd))
        lines.append("")
    return "\n".join(lines) + "\n"


# ---- Output validation ---------------------------------------------------

CRASH_PATTERNS = [
    (re.compile(r"vlimit exceeded", re.I), "VLIMIT_EXCEEDED"),
    (re.compile(r"SHAKE.*(fail|could not|not converge)", re.I), "SHAKE_FAILED"),
    (re.compile(r"Coordinate resetting cannot be accomplished", re.I),
     "SHAKE_FAILED"),
]
DONE_RE = re.compile(r"(wall time|Final Performance|Maximum number of minimization)",
                     re.I)


def scan_out(out_path: Path) -> tuple[bool, list[str]]:
    """Return (finished_ok, crash_codes)."""
    if not out_path.exists():
        return False, []
    text = out_path.read_text(errors="replace")
    crashes = sorted({code for rx, code in CRASH_PATTERNS if rx.search(text)})
    return bool(DONE_RE.search(text)), crashes


# ---- Main ----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Generate + run the 6-step AMBER MD chain on a solvated topology.")
    p.add_argument("--top", required=True, help="Solvated topology (comp_oct.top).")
    p.add_argument("--crd", required=True, help="Solvated coords (comp_oct.crd).")
    p.add_argument("--output-dir", default="./md", help="MD working dir.")
    p.add_argument("--sim-ps", type=float, default=100.0, help="Production length (ps).")
    p.add_argument("--heat-ps", type=float, default=50.0, help="Heating length (ps).")
    p.add_argument("--density-ps", type=float, default=50.0,
                   help="NPT density equilibration length (ps).")
    p.add_argument("--cut", type=float, default=9.0, help="Non-bonded cutoff (A).")
    p.add_argument("--engine", default="pmemd",
                   choices=["pmemd", "pmemd.MPI", "sander"],
                   help="MD engine. Default serial pmemd (no launcher).")
    p.add_argument("--engine-home", default="~/Downloads/pmemd26",
                   help="Install root; engine resolved at <home>/bin first.")
    p.add_argument("--ncpus", type=int, default=4, help="Ranks for pmemd.MPI.")
    p.add_argument("--steps", default="all", choices=["all", "min"],
                   help="'min' runs only the 3 minimizations (fast smoke test).")
    p.add_argument("--dry-run", action="store_true",
                   help="Generate inputs + run.sh without executing.")
    args = p.parse_args()

    # Engine resolution (skip the executable check for dry-run).
    engine_path = resolve_engine(args.engine, args.engine_home)
    if engine_path is None and not args.dry_run:
        emit_and_exit(ok=False, dry_run=False,
                      errors=[f"MISSING_BINARY: engine {args.engine!r} not in "
                              f"{args.engine_home}/bin or on PATH"], code=1)
    if args.engine == "pmemd.MPI" and not args.dry_run and shutil.which("mpirun") is None:
        emit_and_exit(ok=False, dry_run=False,
                      errors=["MISSING_BINARY: mpirun not on PATH (needed for "
                              "pmemd.MPI; use --engine pmemd)"], code=1)

    top = Path(args.top).expanduser()
    crd = Path(args.crd).expanduser()
    if not args.dry_run:
        for label, pth in (("--top", top), ("--crd", crd)):
            if not pth.is_file():
                emit_and_exit(ok=False, dry_run=False,
                              errors=[f"INVALID_INPUT: {label} not found: {pth}"],
                              code=1)

    md_dir = Path(args.output_dir).expanduser().resolve()
    md_dir.mkdir(parents=True, exist_ok=True)

    # Copy topology/coords in with bare names (path-with-space safety).
    top_name, crd_name = "comp_oct.top", "comp_oct.crd"
    if not args.dry_run:
        shutil.copy(top, md_dir / top_name)
        shutil.copy(crd, md_dir / crd_name)

    stages = steps_to_run(args.steps)
    inputs = gen_inputs(cut=args.cut, heat_ps=args.heat_ps,
                        density_ps=args.density_ps, sim_ps=args.sim_ps)
    for st in stages:
        (md_dir / f"{st}.in").write_text(inputs[st])

    run_sh = gen_run_sh(engine=args.engine, engine_path=engine_path or args.engine,
                        ncpus=args.ncpus, top_name=top_name, crd_name=crd_name,
                        stages=stages)
    run_path = md_dir / "run.sh"
    run_path.write_text(run_sh)
    run_path.chmod(0o755)

    in_files = {st: str(md_dir / f"{st}.in") for st in stages}

    if args.dry_run:
        emit_and_exit(ok=True, dry_run=True,
                      outputs={"md_dir": str(md_dir), "run_sh": str(run_path),
                               "inputs": in_files, "stages": stages},
                      validation={"namelists": inputs}, errors=[], code=0)

    # Execute the chain.
    print(f"[{SKILL_NAME}] running {len(stages)} stages via {args.engine}", file=sys.stderr)
    log = (md_dir / "run.log").open("w")
    proc = subprocess.run(["bash", "run.sh"], cwd=str(md_dir),
                          stdout=log, stderr=subprocess.STDOUT)
    log.close()

    # Validate per-stage outputs.
    errors: list[str] = []
    stage_status: dict[str, Any] = {}
    for st in stages:
        rst = md_dir / f"{st}.rst"
        finished, crashes = scan_out(md_dir / f"{st}.out")
        stage_status[st] = {"rst": rst.exists(), "finished": finished,
                            "crashes": crashes}
        if crashes:
            errors.append(f"MD_CRASH[{st}]: {','.join(crashes)}")
        if not rst.exists():
            errors.append(f"STAGE_INCOMPLETE[{st}]: no {st}.rst produced")

    if proc.returncode != 0 and not errors:
        errors.append(f"RUN_SH_FAILED: run.sh exited {proc.returncode} "
                      f"(see {md_dir/'run.log'})")

    last = stages[-1]
    outputs: dict[str, Any] = {
        "md_dir": str(md_dir), "run_sh": str(run_path),
        "run_log": str(md_dir / "run.log"), "stages": stages,
        "final_rst": str(md_dir / f"{last}.rst"),
    }
    if "product" in stages:
        outputs["traj"] = str(md_dir / "product.nc")
        outputs["product_out"] = str(md_dir / "product.out")
        # parse wall time
        po = md_dir / "product.out"
        if po.exists():
            m = re.search(r"Total wall time:\s*([\d.]+)", po.read_text(errors="replace"))
            if m:
                outputs["wall_time_s"] = float(m.group(1))

    emit_and_exit(ok=not errors, dry_run=False, outputs=outputs,
                  validation={"stages": stage_status}, errors=errors,
                  code=0 if not errors else 3)


if __name__ == "__main__":
    main()

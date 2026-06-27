# AMBER end-to-end smoke test

Two-leg validation that the `prime-amber` conda env can run a complete MD pipeline
*and* a complete ligand-parameterization pipeline. Use this as the baseline that the
OpenClaw skills will eventually automate.

## What it tests

**Leg A — `aladip/` — full MD chain on alanine dipeptide in water:**
```
tleap (build + solvate)
   → sander (minimize)
   → sander (heat 0 → 300 K)
   → sander (NVT production)
   → cpptraj (rmsd + radgyr)
```
Field-standard "hello world" — protein force field (ff14SB), TIP3P water, ~1500-2000
atoms total, ~30 ps of dynamics. Validates the protein/water side.

**Leg B — `benzene/` — ligand parameterization chain:**
```
obabel (SMILES → 3D)
   → antechamber (GAFF2 + AM1-BCC)
   → parmchk2 (missing params)
   → tleap (load test)
```
Catches `sqm`/AmbiguousAtomType failures — the #1 real-world ligand-prep breakage.
Validates the small-molecule side.

## How to run

```bash
conda activate prime-amber
cd /path/to/project-prime/smoke-test
bash run.sh
```

Expected runtime: ~3–10 minutes total on a Mac CPU (mostly Leg A's sander steps).

## What "passed" means

`run.sh` uses `set -euo pipefail` and asserts on every step:
- `tleap` produced non-empty topology + coords
- `sander` outputs contain no `NaN` or overflow (`*****`) in the energy columns
- Trajectories (`.nc`) are non-empty
- `cpptraj` wrote the requested `.dat` files
- `antechamber` produced a GAFF2-typed mol2 and `parmchk2` produced an `frcmod`
- `tleap` could load the ligand outputs and build a topology

Final line: `SMOKE TEST PASSED`.

## File map

| File | Role |
|---|---|
| `run.sh` | Orchestrator (assertions live here) |
| `aladip/build.leap` | tleap script: build + solvate alanine dipeptide |
| `aladip/min.in` | sander config: minimization |
| `aladip/heat.in` | sander config: heating 0 → 300 K with NMR-restraint ramp |
| `aladip/prod.in` | sander config: NVT production |
| `aladip/analyze.cpptraj` | cpptraj script: rmsd + radgyr |
| `benzene/load.leap` | tleap script: load antechamber/parmchk2 outputs |
| `benzene/README.md` | Leg-B pipeline notes |

Generated outputs (`.prmtop`, `.inpcrd`, `.nc`, `.rst`, `.out`, `.dat`, `.log`,
intermediate `.mol2`, `.frcmod`) are produced inside the leg directories at runtime.
The `.nc`/`.rst`/`.mdcrd`/`.mdinfo`/etc. extensions are gitignored at the project
root, so re-runs won't pollute version control.

---
name: amber-md-run
description: "Generate and run the standard 6-step explicit-solvent AMBER MD chain (3 minimizations, heating 0->300 K, NPT density equilibration, NPT production) for a solvated topology. Writes md-param-check-clean namelists and a portable run.sh, then executes locally to completion. Production length is a parameter; engine is swappable (serial pmemd default, pmemd.MPI, or sander). System-agnostic."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Requires an AMBER MD engine (pmemd / pmemd.MPI from ~/Downloads/pmemd26, or sander from AmberTools). Topology + coords come from the tleap-build skill. macOS = CPU only (no pmemd.cuda).
metadata: {"openclaw":{"requires":{},"os":["darwin"]},"requires":{"bins":["pmemd"]},"inputs":{"top":"solvated topology (comp_oct.top)","crd":"solvated coords (comp_oct.crd)","sim-ps":"production length ps (default 100)","heat-ps":"default 50","density-ps":"default 50","cut":"angstrom (default 9.0)","engine":"pmemd|pmemd.MPI|sander (default pmemd)","engine-home":"default ~/Downloads/pmemd26","ncpus":"ranks for pmemd.MPI (default 4)","steps":"all|min (default all)","output_dir":"path (default ./md)"},"outputs":{"traj":"product.nc","final_rst":"product.rst","run_sh":"run.sh"},"validation":["each_stage_emits_rst","product_out_finished","no_vlimit_or_shake_crash"],"dry_run":true,"source":"project-prime/skills/amber-md-run","stage":"Phase3.Stage4"}
---

# amber-md-run

## Goal

Take a solvated, neutralized topology/coordinate pair from `tleap-build` and run
it through the canonical equilibration + production protocol, producing a
production trajectory (`product.nc`) ready for analysis. The wrapper generates
six AMBER input files and a `run.sh`, then executes the chain; the LLM never
hand-writes a namelist (that is the upstream amber-md failure mode this avoids).

## When to use

- Stage 4 of `Phase3_Taskboard_Manifest.md`, between `tleap-build` and
  `cpptraj-analysis`.
- `--steps min` for a fast plumbing check; `--dry-run` to inspect/validate the
  generated namelists with `md-param-check` before committing compute.

## The 6-step chain

| Stage | Ensemble | Restraint | Purpose |
|-------|----------|-----------|---------|
| min1 | — | solute fixed (`wt=500`) | minimize solvent only |
| min2 | — | heavy solute fixed | + minimize hydrogens |
| min3 | — | none | full-system minimization |
| heat | NVT | solute weak (`wt=2`) | 0 → 300 K Langevin ramp |
| density | NPT (MC) | solute weak | equilibrate to ~1 g/cm³ |
| product | NPT (MC) | none | production, `--sim-ps` long |

## Inputs

| Key | Default | Description |
|-----|---------|-------------|
| `--top` / `--crd` | required | Solvated topology + coords (`comp_oct.*`). |
| `--sim-ps` | 100 | Production length (ps). `nstlim = sim_ps / 0.002`. |
| `--heat-ps` / `--density-ps` | 50 / 50 | Equilibration lengths. |
| `--cut` | 9.0 | Non-bonded cutoff (md-param-check range 8–12). |
| `--engine` | `pmemd` | `pmemd` (serial), `pmemd.MPI` (mpirun), or `sander`. |
| `--engine-home` | `~/Downloads/pmemd26` | Engine resolved at `<home>/bin` first, then PATH. |
| `--ncpus` | 4 | Ranks for `pmemd.MPI`. |
| `--steps` | `all` | `min` = 3 minimizations only (fast smoke test). |
| `--output-dir` | `./md` | Working dir for inputs, restarts, trajectory. |
| `--dry-run` | — | Generate inputs + run.sh without executing. |

## Outputs

Single JSON envelope. Key fields: `outputs.traj` (`product.nc`),
`outputs.final_rst`, `outputs.run_sh`, `outputs.wall_time_s`, and
`validation.stages.<stage>.{rst,finished,crashes}`.

## Validation gates

- Every stage emitted its `.rst`.
- `product.out` shows a completion marker (`Total wall time` / `Final Performance`).
- No `vlimit exceeded` or `SHAKE failed` in any `.out` (these become
  `MD_CRASH[stage]` errors — the hook the Stage 8 bounded-recovery skill will
  later act on by lowering `dt` / restarting).

## Physical-realism guarantees (md-param-check clean)

`dt=0.002` with `ntc=2,ntf=2` (SHAKE); `8 ≤ cut ≤ 12`; `ntt=3` Langevin
`gamma_ln=2.0`, `ig=-1`; `temp0` == `&wt value2` in `heat.in` (no heat-3-style
mismatch); `ntp=1, barostat=2` for NPT; `iwrap=1` in production. No hardcoded
engine path — resolved from `--engine-home`/PATH.

## Engine seam (Gap_Remote_HPC_Backend)

The engine and its launcher are the only execution-context coupling. Today:
local CPU serial `pmemd` (~15.6 ns/day on this M-series Mac for the 1L2Y test
system; ~19 min for the default 200 ps of MD). Scenario B (remote `pmemd.cuda`
+ Slurm/PBS via DPDispatcher) swaps this seam without touching the recipe files.

## Acceptance test

`bash test_acceptance.sh`:
1. **Golden** — `--steps min` on the 1L2Y solvated topology → asserts `ok`, all
   three `.rst` produced, no crashes.
2. **Dry-run** — full chain `--dry-run` → asserts the generated `heat.in` has
   `temp0` consistent with `&wt value2` and `product.in` has `barostat=2`.
3. **Malformed** — nonexistent topology → asserts `ok:false` with a code.

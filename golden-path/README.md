# Golden path — real protein–ligand complex, end to end

This is the **canonical reference recipe** for Project Prime: a *real* protein–ligand
complex driven through the entire pipeline locally with `sander`. The `../smoke-test/`
legs validate the protein side and the ligand side *in isolation*; this is the combined
path the OpenClaw skills will actually automate, and the first place **PLIP** runs on real
MD output.

## System

**T4 lysozyme L99A + benzene (PDB `181L`)** — the textbook protein–ligand binding
benchmark. Benzene (`BNZ`) sits in an engineered hydrophobic cavity, so it's the standard
positive control for "does the pipeline detect a real binding interaction." The ligand
reuses the exact GAFF2 + AM1-BCC parameterization proven in `../smoke-test/benzene/`.

## Pipeline

```
fetch 181L  ──▶ pdb4amber (clean protein, strip waters/buffer/ions)
            ──▶ obabel +H ▶ antechamber (GAFF2 + AM1-BCC) ▶ parmchk2   (ligand params)
            ──▶ tleap: combine protein+ligand ▶ solvate TIP3P ▶ addions (neutralize)
            ──▶ sander: minimize ▶ heat 0→300K (NVT) ▶ equilibrate (NPT) ▶ produce
            ──▶ cpptraj: backbone RMSD + per-residue RMSF + ligand RMSD + frame export
            ──▶ PLIP: non-covalent interaction fingerprint (H-bonds, hydrophobic, π-stack)
```

Force field: **ff14SB** (protein) / **GAFF2** (ligand) / **TIP3P** (water), neutralized
with Cl⁻. System ≈ **24.5k atoms**. MD lengths are deliberately short (10 ps/stage) — the
goal is to validate the *pipeline*, not to generate production data.

Physical-realism hard limits (Project Prime §3) are enforced in every `*.in`:
`dt = 0.002` (≤ 2 fs), `ntc=2`/`ntf=2` (SHAKE), `cut = 8.0`.

## How to run

```bash
conda activate prime-amber
cd project-prime/golden-path
bash run.sh
```

Runtime: ~15–25 min on a Mac CPU (serial `sander`), dominated by the three MD stages.
If that's too slow, drop the solvent buffer to `8.0` in `build.leap` and/or trim `nstlim`.

## What "passed" means

`run.sh` uses `set -euo pipefail` and asserts every stage:
- `pdb4amber` cleaned the protein; `BNZ` ligand extracted
- `antechamber`/`parmchk2` produced a GAFF2-typed mol2 + frcmod
- `tleap` built a non-empty, **neutral** topology (no `FATAL`)
- no `NaN`/overflow (`*****`) in any sander `.out`; trajectories non-empty
- `cpptraj` wrote `rmsd_backbone.dat`, `rmsf.dat`, `rmsd_ligand.dat`, `complex_frame.pdb`
- PLIP produced a report containing an interaction section

Final line: `GOLDEN PATH PASSED`.

## HPC swap (Scenario A → B, when a cluster arrives)

Per `Gap_Remote_HPC_Backend`, development is local with `sander`. The recipe is
**engine-agnostic**: all MD goes through `run_md()` using the `ENGINE` variable. Moving to
a cluster is two changes, and **none of the recipe files change**:

1. `ENGINE=pmemd.cuda` (the cluster provides it via `module load amber` — we never compile it).
2. Wrap `run_md` in a DPDispatcher `SSHContext` + Slurm/PBS job descriptor (`Infra_DPDispatcher`).

## File map

| File | Role |
|---|---|
| `run.sh` | Orchestrator; all assertions + the `ENGINE` swap seam live here |
| `build.leap` | tleap: combine protein+ligand, solvate TIP3P, neutralize |
| `min.in` | sander: minimization (solute restrained) |
| `heat.in` | sander: heat 0→300 K, NVT, solute restrained, `nmropt` ramp |
| `equil.in` | sander: **NPT** equilibration (MC barostat), backbone restrained |
| `prod.in` | sander: unrestrained NVT production (the analyzed trajectory) |
| `analyze.cpptraj` | cpptraj: RMSD + RMSF + ligand RMSD + final-frame PDB export |

Generated outputs (`181L.pdb`, `*_clean.pdb`, `ligand_*`, `system.*`, `*.out`, `*.nc`,
`*.rst`, `*.dat`, `complex_frame.pdb`, `plip_out/`, logs) are produced at runtime and
gitignored by extension at the project root.

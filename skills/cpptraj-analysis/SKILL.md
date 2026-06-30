---
name: cpptraj-analysis
description: "Run the full post-MD analysis suite on an AMBER production trajectory: strip solvent, then RMSD, RMSF, radius of gyration, SASA, DSSP secondary structure, H-bonds, Cα distance matrix, k-means clustering, PCA, free-energy landscape, thermodynamics, and MM-GBSA binding free energy. Each analysis emits a .dat and a .png. Residue masks auto-detected; system-agnostic."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Requires AmberTools (cpptraj, MMPBSA.py) on PATH + Python with numpy/matplotlib. Inputs come from tleap-build (topologies) and amber-md-run (trajectory + mdout).
metadata: {"openclaw":{"requires":{},"os":["darwin"]},"requires":{"bins":["cpptraj","MMPBSA.py","parmed"]},"inputs":{"comp-oct-top":"solvated topology","comp-dry-top":"dry complex topology","traj":"production .nc","protein-top":"MM-GBSA receptor (optional)","ligand-top":"MM-GBSA ligand (optional)","mdout-dir":"dir with heat/density/product .out (thermo)","analyses":"comma list or all","output_dir":"path (default ./analysis)"},"outputs":{"analyses":"{name:{dat,png,ok}}","mmgbsa_dG_kcal_mol":"binding free energy"},"validation":["core_rmsd_rmsf_rg_produced","each_requested_emits_png","mmgbsa_dG_parsed","gb_radii_consistent_with_igb_fatal"],"dry_run":true,"source":"project-prime/skills/cpptraj-analysis","stage":"Phase3.Stage5"}
---

# cpptraj-analysis

## Goal

Turn a production trajectory into the standard set of structural, dynamical, and
thermodynamic observables plus publication-ready PNGs — the same ten-ish analyses
the upstream amber-md skill produces, but with the documented cpptraj footguns
fixed and residue masks auto-detected instead of hardcoded to 1L2Y.

## When to use

- Stage 5 of `Phase3_Taskboard_Manifest.md`, after `amber-md-run`.
- `--analyses rmsd,rmsf,mmgbsa` to run a subset.

## The suite

`strip` (preprocess) → rmsd, rmsf, rg, sasa, dssp, hbond, distmat, cluster, pca,
fel (free-energy landscape), thermo, mmgbsa.

## Inputs

| Key | Required | Description |
|-----|----------|-------------|
| `--comp-oct-top` | yes | Solvated topology — used ONLY for the strip step (matches the trajectory atom count). |
| `--comp-dry-top` | yes | Dry complex topology — used by every downstream analysis. |
| `--traj` | yes | Production trajectory (`product.nc`). |
| `--protein-top` / `--ligand-top` | for MM-GBSA | Component topologies from tleap-build. Omit for protein-only (MM-GBSA skipped). |
| `--mdout-dir` | for thermo | Dir holding `heat.out`/`density.out`/`product.out`. |
| `--analyses` | no (`all`) | Comma list or `all`. `fel` auto-pulls in `pca`. |
| `--output-dir` | no (`./analysis`) | One subdir per analysis. |
| `--dry-run` | no | Report detected masks + plan without running. |

## Outputs

Single JSON envelope. `outputs.analyses` maps each name to `{ok, dat, png, note}`;
`outputs.produced` / `outputs.failed` list names; `outputs.mmgbsa_dG_kcal_mol`
is the binding free energy (negative = favorable).

## Validation gates

- Preprocessing produced `strip.nc`.
- Core analyses (`rmsd`, `rmsf`, `rg`) produced output → suite `ok`.
- Each requested analysis produced its `.png` (an h-bond "no data" result still
  emits an explanatory figure and counts as ok).
- MM-GBSA `DELTA TOTAL` parsed into `mmgbsa_dG_kcal_mol`.

Optional-analysis failures are reported in `errors[]`/`failed[]` but do not sink
the whole suite (core must still pass).

## Correctness rules baked in (the value over upstream)

- **Strip uses the SOLVATED topology** (`comp_oct.top`) because it must match the
  trajectory's atom count; it writes a dry `strip.nc` that every downstream
  module reads with `comp_dry.top`. The upstream `strip.in` reads the solvated
  trajectory with `comp_dry.top` and only "works" because their comp_dry is
  secretly solvated (their save-order bug).
- **PCA is two cpptraj calls** (`diagmatrix`+`run`, then `projection` reading
  evecs from file). One call → "evecs contains no data".
- **Clustering keeps `repout` inside the single kmeans command** (splitting it
  silently reverts to hieragglo).
- **H-bond "no data" is a finding**, not an error (indole binds via hydrophobic /
  π–π contacts; reported with an explanatory figure).
- **Residue masks auto-detected** from the dry topology (protein = all but the
  last residue when a ligand is present; ligand = last residue).
- **Parsing gotchas handled**: `evecs.dat` hand-parsed (not `read_csv`);
  `summary.DENSITY` may be 1-column; cluster `summary.dat` header skipped.
- **Path-with-space safe**: inputs are copied in under bare names and referenced
  relatively, because cpptraj/MMPBSA tokenize input lines on whitespace.

## Acceptance test

`bash test_acceptance.sh` builds a topology, runs a short MD, then:
1. **Golden** — full suite on a protein-ligand trajectory → asserts core ok, all
   12 analyses produce PNGs, MM-GBSA ΔG is negative.
2. **Subset** — `--analyses rmsd,rg` → asserts only those run.
3. **Malformed** — nonexistent trajectory → asserts `ok:false`.

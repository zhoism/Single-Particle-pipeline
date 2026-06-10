---
name: plip-profile
description: "Profile protein-ligand non-covalent interactions (hydrophobic, hydrogen bond, water bridge, salt bridge, pi-stacking, pi-cation, halogen bond, metal complex) on an AMBER production trajectory with PLIP. Extracts a representative dry complex frame, normalizes AMBER variant residue names so PLIP does not invent phantom ligands, runs PLIP, and returns a structured interaction envelope. Stage 6, after cpptraj-analysis."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Requires PLIP 3.x + cpptraj (AmberTools) on PATH (source scripts/env.sh). Inputs come from tleap-build (topologies) and amber-md-run (trajectory) — the same inputs cpptraj-analysis consumes.
metadata: {"openclaw":{"requires":{"bins":["cpptraj","plip"]},"os":["darwin"]},"inputs":{"comp-oct-top":"solvated topology (strip)","comp-dry-top":"dry complex topology","traj":"production .nc","ligand-resname":"ligand residue name PLIP keys on (default MOL)","frame":"medoid|last|N","output_dir":"path (default ./plip)"},"outputs":{"interactions":"{type:[{residue,dist}]}","totals":"counts by type","contact_residues":"unique interacting residues","ligand":"hetid/smiles/properties"},"validation":["ligand_detected_and_keyed","no_phantom_protein_ligands","resnames_normalized"],"dry_run":true,"source":"project-prime/skills/plip-profile","stage":"Phase3.Stage6"}
---

# plip-profile

## Goal

Turn a production trajectory into a structured protein-ligand interaction
fingerprint: which residues contact the ligand and *how* (hydrophobic, hydrogen
bond, salt bridge, pi-stacking, pi-cation, halogen bond, metal). This is the
analysis the upstream amber-md prior art stops short of (it ends at MM-GBSA) —
the deterministic interaction layer is one of this project's differentiators.

## When to use

- Stage 6, after `cpptraj-analysis` (Stage 5). Consumes the same dry/solvated
  topologies + trajectory.
- "Profile the protein-ligand interactions for run X."

## Inputs

| Key | Required | Description |
|-----|----------|-------------|
| `--comp-oct-top` | yes | Solvated topology — used only to strip solvent (matches the trajectory atom count). |
| `--comp-dry-top` | yes | Dry complex topology (protein + ligand). |
| `--traj` | yes | Production trajectory (`product.nc`). |
| `--ligand-resname` / `--name` | no (`MOL`) | Ligand residue name PLIP must key on (e.g. `MOL`, `JZ4`). |
| `--frame` | no (`medoid`) | `medoid` (frame closest to the backbone average — deterministic), `last`, or a 1-based integer. |
| `--output-dir` | no (`./plip`) | Output directory. |
| `--dry-run` | no | Emit the plan (frame policy, normalization table, binaries) without running. |

## Outputs

Single JSON envelope. `outputs.interactions` maps each interaction type to a
list of `{residue, restype, resnr, reschain, dist}`; `outputs.totals.by_type`
gives counts; `outputs.contact_residues` is the unique interacting set;
`outputs.ligand` carries PLIP's hetid / SMILES / heavy-atom + ring counts.
Artifacts: `complex_frame.pdb` (the normalized dry frame), PLIP `report/*.{xml,txt}`,
`interaction_summary.txt`, and `interactions.png` (when matplotlib is present).

## Validation gates

- **Ligand detected and keyed** — PLIP reports a SMALLMOLECULE binding site whose
  hetid equals `--ligand-resname` (not a protein residue).
- **No phantom protein ligands** — no binding site is named for a standard amino
  acid. A phantom means the resname normalization missed a variant → the run
  fails loudly (`PHANTOM_LIGANDS` / `RESNAME_NORMALIZATION_INCOMPLETE`) so the
  mapping gets extended, never hacked around downstream.
- **Resnames normalized** — every AMBER variant resname (HIE/HID/HIP/CYX/...) is
  rewritten to its standard parent before PLIP; the output PDB is verified to
  contain none. `outputs.resnames_changed` records what fired.
- Malformed inputs / missing files / a standard-AA ligand name → structured
  `ok:false`, never a crash.

## Correctness rules baked in (the value)

- **THE PLIP resname footgun.** AMBER writes protonation / disulfide state into
  the residue *name* (HIE, HID, HIP, CYX, CYM, ASH, GLH, LYN, ...). PLIP treats
  any residue name it does not recognise as a small-molecule ligand, so it
  invents phantom ligands among the protein and can mis-key the real one. This
  skill normalizes those names to standard PDB names **before** PLIP and then
  **verifies** no variant leaked. (Proven load-bearing: on the raw T4-lysozyme
  frame PLIP reports `HIE:A:31` as a SMALLMOLECULE; after normalization it does
  not — see `references/plip-interactions.md`.)
- **Hydrogens are kept.** PLIP needs explicit H to assign donors/acceptors; the
  production complex already carries H (tleap added it). The skill never strips
  ligand H.
- **Deterministic representative frame.** Default `medoid` = the real frame whose
  protein backbone is closest to the trajectory average — reproducible (no RNG),
  so re-runs give a byte-identical interaction profile. `last` / explicit-N are
  available. Per-frame interaction occupancy (a time series) is a v2.
- **Strip uses the solvated topology** (atom-count match); the dry frame uses the
  dry topology — mirrors cpptraj-analysis. Solvent + neutralizing counter-ions
  (`:WAT,:Na+,:Cl-,:K+`) are stripped; functional-metal systems are a v2.
- **Path-with-space safe** — inputs are staged under bare names and cpptraj/PLIP
  run with a bare-name cwd (the project path contains a space).

## Acceptance test

`bash test_acceptance.sh` runs: the engine unit oracle (resname normalizer,
variant detector, XML parser — 52 checks); dry-run; malformed → `ok:false`;
standard-AA ligand name → `ok:false`; golden on the 1L2Y and 3HTB run fixtures
(ligand detected, no phantom, ≥1 interaction, normalization fired on 3HTB);
determinism (two runs → byte-identical profile); a phantom control (raw HIE
frame produces a phantom, the normalized run does not); and the `last`/N frame
policies. Real-trajectory cases skip loudly if a run fixture is absent.

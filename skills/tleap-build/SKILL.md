---
name: tleap-build
description: "Build an AMBER topology + coordinates from a protein PDB plus an optional parameterized ligand (mol2 + frcmod from antechamber-ligandprep). Generates a correct leap.in (saves the dry complex BEFORE solvation, saves protein/ligand component topologies for MM-GBSA), runs tleap, solvates in a TIP3P octahedron, and neutralizes. System-agnostic: handles protein-ligand and protein-only systems."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Requires AmberTools (tleap, pdb4amber) on PATH; AMBERHOME set. The ligand inputs come from the antechamber-ligandprep skill.
metadata: {"openclaw":{"requires":{"env":["AMBERHOME"]},"os":["darwin"]},"requires":{"bins":["tleap","pdb4amber"],"env":["AMBERHOME"]},"inputs":{"protein":"path_to_pdb","ligand-mol2":"path (optional)","ligand-frcmod":"path (required with ligand-mol2)","name":"ligand_resname (default LIG)","protein-ff":"default ff19SB","water":"default tip3p","ligand-ff":"default gaff2","buffer":"angstrom (default 10.0)","output_dir":"path (default ./)"},"outputs":{"comp_oct_top":"solvated topology (MD input)","comp_oct_crd":"solvated coords","comp_dry_top":"dry complex topology (analysis)","protein_top":"MM-GBSA receptor","ligand_top":"MM-GBSA ligand"},"validation":["leap_log_no_error","dry_atoms_lt_solvated_atoms","protein_plus_ligand_eq_dry","system_neutral"],"dry_run":true,"source":"project-prime/skills/tleap-build","stage":"Phase3.Stage3"}
---

# tleap-build

## Goal

Turn a protein PDB (and, for a complex, the GAFF2 `mol2`+`frcmod` produced by
`antechamber-ligandprep`) into the AMBER files the MD engine and the analysis
stage need: a **solvated, neutralized** topology/coordinate pair (`comp_oct.top`
/ `comp_oct.crd`) for the simulation, a **dry** complex topology (`comp_dry.top`)
for stripped-trajectory analysis, and — when a ligand is present — separate
`protein.top` and `ligand.top` for MM-GBSA decomposition. The wrapper writes a
correct `leap.in` and runs `tleap`; the LLM stays outside the deterministic path.

## When to use

- Stage 3 of `Phase3_Taskboard_Manifest.md`, directly downstream of
  `antechamber-ligandprep` and upstream of `amber-md-run`.
- Any time a `.parm7/.top` + `.rst7/.crd` is needed from a structure.

## Inputs

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `--protein` | path | yes | Protein PDB. Pre-cleaned with `pdb4amber` unless `--skip-protein-clean`. |
| `--ligand-mol2` | path | no | GAFF2 mol2 from `antechamber-ligandprep`. Omit for a protein-only build. |
| `--ligand-frcmod` | path | with mol2 | frcmod from `antechamber-ligandprep`. |
| `--name` | string | no (`LIG`) | Ligand residue name (used only to name the copied inputs). |
| `--protein-ff` | string | no (`ff19SB`) | `source leaprc.protein.<ff>`. |
| `--water` | string | no (`tip3p`) | `source leaprc.water.<model>`. |
| `--ligand-ff` | string | no (`gaff2`) | `source leaprc.<ff>`. |
| `--buffer` | float | no (`10.0`) | `solvateoct` buffer in Angstrom (≥8 recommended). |
| `--output-dir` | path | no (`./`) | Artifacts + per-run scratch dir. |
| `--dry-run` | flag | no | Write `leap.in` and emit the plan without running tleap. |

## Outputs

Single JSON envelope on stdout; stderr is per-step progress.

```json
{
  "ok": true,
  "skill": "tleap-build",
  "dry_run": false,
  "outputs": {
    "comp_oct_top": "/abs/tleap-build-run/comp_oct.top",
    "comp_dry_top": "/abs/tleap-build-run/comp_dry.top",
    "protein_top":  "/abs/tleap-build-run/protein.top",
    "ligand_top":   "/abs/tleap-build-run/ligand.top",
    "comp_oct_crd": "...", "comp_dry_crd": "...", "comp_oct_pdb": "...",
    "leap_in": "...", "run_dir": "..."
  },
  "validation": {
    "dry_atoms": 306, "solvated_atoms": 6847,
    "protein_atoms": 290, "ligand_atoms": 16,
    "waters_plus_ions_atoms": 6541, "residual_charge": 0.0
  },
  "errors": []
}
```

## Validation gates

- `leap.log` contains no `ERROR`/`FATAL` line.
- **`dry_atoms < solvated_atoms`** — the steal-list #1 sanity test. If the dry
  topology is not strictly smaller, `comp_dry.top` was saved after `solvateoct`
  (the upstream amber-md bug) and every stripped-trajectory analysis would later
  fail with `Number of atoms … does not match`.
- **`protein_atoms + ligand_atoms == dry_atoms`** — the combine invariant (only
  when a ligand is present).
- `residual_charge` ≈ 0 after `addions2`.

## How it works

`scripts/wrapper.py` runs the chain as ordinary subprocesses and returns one
envelope:

1. Copy inputs into the run dir under bare filenames — the project path contains
   a space (`Single Particle`) that LEaP mishandles in `loadpdb` paths.
2. `pdb4amber` pre-clean of the protein (HIS→HIE/HID/HIP, CYX, alt-locs).
3. Generate `leap.in` with the load-bearing save order: components →
   `comp_dry` **before** `solvateoct` → solvate → neutralize → `comp_oct`.
4. `tleap -f leap.in`.
5. Validate via `leap.log` parsing + prmtop `NATOM` introspection.

The ligand is loaded as a **mol2** and renumbered automatically by `combine`, so
the upstream brittle `sed` residue-collision hack is unnecessary.

## Errors

| Code | Cause | Recovery |
|------|-------|----------|
| `MISSING_BINARY` / `MISSING_ENV` | tleap/pdb4amber not found, AMBERHOME unset. | Activate `prime-amber` conda env. |
| `INVALID_INPUT` | Missing protein, or `--ligand-mol2` without `--ligand-frcmod`. | Fix args. |
| `TLEAP_STEP_FAILED` | pdb4amber or tleap exited non-zero. | Inspect `<run_dir>/03_tleap.err` + `leap.log`. |
| `DRY_TOPOLOGY_CONTAMINATED` | dry ≥ solvated atom count. | Save order regression — should never happen with this wrapper. |
| `COMPONENT_ATOM_MISMATCH` | protein+ligand ≠ dry complex. | Bad combine; check ligand mol2 residue. |
| `SYSTEM_NOT_NEUTRAL` | residual charge after addions2. | Check ligand net charge / counter-ion type. |

## References

`references/heuristics.md` — leap ordering, force-field selection, the
path-with-space gotcha, and the steal-list lineage.

## Acceptance test

`bash test_acceptance.sh` runs three cases on the 1L2Y golden fixture:
1. **Golden** — protein + indole ligand → asserts `ok`, dry < solvated, and the
   `protein+ligand == dry` invariant.
2. **Unrelated** — protein-only build (no ligand) → asserts `ok`, dry < solvated.
3. **Malformed** — nonexistent protein path → asserts `ok:false` with a code.

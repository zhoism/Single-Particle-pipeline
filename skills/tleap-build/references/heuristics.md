# tleap-build heuristics

Distilled from `Research_amber_md_skill.md` (steal-list), the upstream amber-md
`references/input-templates.md` §5 / `force-fields.md`, and Amber26.pdf ch.14
(LEaP). Cited, not depended on.

## The save-order rule (steal #1 — the single most important thing)

`saveamberparm comp comp_dry.top comp_dry.crd` **must run BEFORE** `solvateoct`.
After solvation the `comp` unit contains water + ions, so a "dry" topology saved
then is not dry — and every stripped-trajectory analysis later dies with
`Number of atoms in NetCDF file (306) does not match number in associated
topology (6847)`.

The upstream amber-md `examples/prep/leap.in` gets this **wrong** (it saves
comp_oct then comp_dry, both after solvation). Our generated leap.in saves
comp_dry first. The wrapper's `dry_atoms < solvated_atoms` gate is the regression
test for this exact mistake.

## MM-GBSA needs component topologies

`MMPBSA.py` computes ΔG_bind = G_complex − G_protein − G_ligand and therefore
needs `protein.top` and `ligand.top` saved as independent units, plus the dry
complex `comp_dry.top` and the solvated `comp_oct.top`. We save all four. The
combine invariant `protein_atoms + ligand_atoms == dry_atoms` is a free
correctness check that the components and the complex agree.

## Why load the ligand as mol2 (not loadpdb)

Loading the ligand as a `mol2` and `combine { prot LIG }` makes LEaP assign the
ligand a fresh residue number after the protein automatically. That sidesteps the
upstream residue-number collision (their indole was `MOL A 6`, colliding with
protein ALA 6) without the brittle `sed 's/MOL A   6/MOL A  21/'` hack. Beating
his fix by not needing it.

## Path-with-space gotcha

The project lives under `…/Single Particle/…`. LEaP's `loadpdb`/`loadmol2` choke
on spaces even when quoted in some builds. The wrapper copies every input into
the run dir under a bare filename and runs `tleap` with `cwd=run_dir`, so leap.in
only ever references simple relative names.

## Force-field defaults (upstream force-fields.md decision tree)

- Protein: **ff19SB** (QM-refined backbone; fixes ff14SB Gly/Pro bias). ff14SB is
  the backup for literature comparison.
- Water: **TIP3P** (fastest, most-validated with protein FFs). OPC if water
  dynamics matter (~20–30% more compute).
- Ligand: **GAFF2** + AM1-BCC charges (done upstream in antechamber-ligandprep).
- Ions: Joung–Cheatham via `addions2` neutralization; `addions2 … 0` adds only
  the counter-ion the net charge requires, so running both Na+ and Cl- is safe.

## Neutralization

`addions2 comp Na+ 0` then `addions2 comp Cl- 0`: the `0` count means "add enough
to neutralize." When the system is already +1, the Na+ pass adds nothing and the
Cl- pass adds one Cl-; when −1, vice-versa. Idempotent and sign-agnostic. 1L2Y
(Trp-cage) is net +1, so expect exactly one Cl-.

## Buffer

`solvateoct … 10.0` — a 10 Å buffer (≥8 Å minimum) keeps the solute away from its
periodic image. Smaller boxes risk self-interaction artifacts; larger ones cost
compute. 10 Å is the standard default.

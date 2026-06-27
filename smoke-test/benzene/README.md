# Leg B — benzene ligand parameterization

Standalone test of the `antechamber → parmchk2 → tleap` chain. No MD.
Catches `sqm`/AmbiguousAtomType failures (the #1 real-world ligand-prep breakage).

## Pipeline

1. `obabel` generates a 3D benzene structure from SMILES (`c1ccccc1`) → `benzene_input.mol2`.
2. `antechamber` assigns GAFF2 atom types + AM1-BCC charges → `benzene_gaff2.mol2`.
3. `parmchk2` fills any GAFF2 parameter gaps → `benzene.frcmod`.
4. `tleap` loads both and writes a topology → `benzene.prmtop` + `benzene.inpcrd`.

If all four succeed, the ligand-prep stack is healthy.

Run via the parent `smoke-test/run.sh`.

# PLIP interaction profiling — reference notes

Distilled reference for the `plip-profile` skill. Sources: PLIP 3.0.0 CLI + XML
report schema (read directly from real output), the project golden-path PLIP run
(`golden-path/`, T4 lysozyme + benzene `181L`), and the empirical control below.

## What PLIP detects

PLIP (Protein-Ligand Interaction Profiler) reads a **PDB of a complex** and
reports non-covalent interactions between each detected ligand ("small molecule")
and the protein. The eight categories it emits (and the short labels this skill
uses):

| PLIP XML container | label | typical distance field |
|---|---|---|
| `hydrophobic_interactions` | `hydrophobic` | `dist` |
| `hydrogen_bonds` | `hydrogen_bond` | `dist_d-a` (donor-acceptor), `dist_h-a` |
| `water_bridges` | `water_bridge` | `dist_a-w`, `dist_d-w` |
| `salt_bridges` | `salt_bridge` | `dist` |
| `pi_stacks` | `pi_stacking` | `centdist` |
| `pi_cation_interactions` | `pi_cation` | `dist` |
| `halogen_bonds` | `halogen_bond` | `dist` |
| `metal_complexes` | `metal_complex` | `dist` |

Each interaction element carries `resnr` / `restype` / `reschain` (the protein
residue), `*_lig` counterparts, and one or more distances. The skill flattens
these into `{residue, restype, resnr, reschain, dist}` and counts per type.

PLIP identifies a ligand by its **non-standard residue name**, independent of
ATOM vs HETATM record type. It needs explicit hydrogens to assign H-bond
donors/acceptors — never strip ligand H before profiling.

## THE footgun: AMBER variant residue names → phantom ligands

AMBER force fields write protonation / tautomer / disulfide state into the
residue **name**, not a separate field:

| AMBER variant | meaning | normalizes to |
|---|---|---|
| HID / HIE / HIP | His δ / ε / doubly-protonated | HIS |
| CYX / CYM | disulfide / deprotonated Cys | CYS |
| ASH | protonated Asp | ASP |
| GLH | protonated Glu | GLU |
| LYN | neutral Lys | LYS |

PLIP only recognises the 20 canonical 3-letter codes. **Any other residue name
is taken to be a small-molecule ligand.** So an unnormalized frame makes PLIP
invent phantom ligands out of protein residues — polluting the profile and
mis-stating what the ligand is.

### Proven, not theoretical (control on 3HTB / T4 lysozyme)

The dry frame of the T4-lysozyme+JZ4 system contains one `HIE` (His31). Running
PLIP on the **raw** frame:

```
HIE:A:31 (HIE) - SMALLMOLECULE     <- phantom: a protein His mistaken for a ligand
JZ4:A:164 (JZ4) - SMALLMOLECULE    <- the real ligand
```

After the skill normalizes `HIE → HIS` (17 atoms), PLIP reports a single binding
site (`JZ4`), zero phantoms. The normalization is load-bearing; this is exactly
the silent-error class the deterministic-wrapper thesis exists to catch.

The skill defends in depth: (1) normalize before PLIP, (2) verify the output PDB
has no surviving variant (`RESNAME_NORMALIZATION_INCOMPLETE` if it does), and
(3) fail if any PLIP binding site is named for a standard amino acid
(`PHANTOM_LIGANDS`). The golden-path precedent did the same normalization via
cpptraj `change resname`; this skill does it as a unit-tested column-exact text
transform so the full variant table is covered and idempotent.

## Frame policy

PLIP profiles a single PDB, so a frame must be chosen from the trajectory:

- **medoid (default)** — the real frame whose protein backbone (`@CA,C,N`) is
  closest to the trajectory average (two cpptraj passes: average, then per-frame
  RMSD-to-average; argmin, ties to lowest index). Deterministic and
  representative of the dominant conformation.
- **last** — the final production frame (what the golden-path used via `prod.rst`).
- **N** — an explicit 1-based frame index.

The cpptraj cluster representative (`analysis/cluster/rep.c0.pdb`) is an
alternative "representative" but is computed with `randompoint` kmeans (not
guaranteed reproducible), so this skill extracts its own deterministic frame
rather than depending on it.

## v2 / not built (named, not silently dropped)

- **Per-frame interaction occupancy** — run PLIP over many frames and report each
  interaction's persistence (fraction of frames present). The single-frame
  profile is a sanity fingerprint, like the single-trajectory MM-GBSA ΔG; it is
  not an occupancy-weighted binding analysis.
- **Functional metals / cofactors** — the strip mask removes neutralizing ions
  (`Na+/Cl-/K+`); a catalytic metal would need the mask relaxed and PLIP's
  metal-complex handling exercised.
- **Pocket/ligand-centred medoid** — the default medoid keys on protein backbone;
  a pocket- or ligand-aligned medoid could better capture the representative pose.

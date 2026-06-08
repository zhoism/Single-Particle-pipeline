---
name: antechamber-ligandprep
description: "Parameterize one ligand for AMBER MD: a PDB / SMILES / mol2 input becomes a GAFF2 .mol2 (with AM1-BCC partial charges) plus a parmchk2 .frcmod, ready for tleap to load into a protein-ligand topology. System-agnostic; no hardcoded ligand."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Requires AmberTools (antechamber, parmchk2, pdb4amber) + OpenBabel (obabel) on PATH; AMBERHOME set.
metadata: {"openclaw":{"requires":{"env":["AMBERHOME"]},"os":["darwin"]},"requires":{"bins":["antechamber","parmchk2","obabel","pdb4amber"],"env":["AMBERHOME"]},"inputs":{"input":"path_or_smiles","name":"residue_name (default LIG, 1-4 chars)","charge":"net_formal_charge (int, default 0)","output_dir":"path (default ./)"},"outputs":{"mol2":"<output_dir>/<NAME>.mol2","frcmod":"<output_dir>/<NAME>.frcmod"},"validation":["mol2_charge_column_present","atom_types_no_du_placeholder","frcmod_no_attn_lines","net_charge_within_5e-3_of_requested"],"dry_run":true,"source":"project-prime/skills/antechamber-ligandprep","stage":"Phase3.Stage2"}
---

# antechamber-ligandprep

## Goal

Take a single small-molecule ligand and produce the two AMBER files that `tleap` needs to combine it with a protein into a parameterized topology: a Sybyl `.mol2` carrying GAFF2 atom types and AM1-BCC partial charges, and a `.frcmod` filling in any parameters not already in the GAFF2 base. The skill handles three input shapes (a PDB extract, a SMILES string, or an already-typed mol2) and emits the same envelope regardless. Wrapper-internal chain — the LLM stays outside the deterministic path per the project's "lobster-like" discipline.

## When to use

- A new ligand needs parameters before tleap can combine protein + ligand into a `.parm7/.rst7` topology.
- Stage 2 of `Phase3_Taskboard_Manifest.md` — the prep skill upstream of `tleap-build`.
- Recovery (Stage 8) wants to re-derive charges with a different net charge or atom-type set.

## Inputs

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `--input` | path or SMILES | yes | A `.pdb` / `.mol2` / `.sdf` file path, OR a SMILES string. Auto-detected: a path that exists is treated as a file; otherwise the value is parsed as SMILES. |
| `--name` | string | no (default `LIG`) | Residue name written into the mol2 and used as the output basename. 1–4 chars; uppercase letters/digits. |
| `--charge` | int | no (default `0`) | Net formal charge for AM1-BCC. Required when the molecule is charged. |
| `--output-dir` | path | no (default `./`) | Where `<NAME>.mol2`, `<NAME>.frcmod`, and the per-run scratch dir are written. |
| `--dry-run` | flag | no | Plan and emit the command chain without executing. Used by Stage 8 Tier 2 recovery to validate a recipe before mutation. |

## Outputs

Single JSON envelope on stdout. stderr is human-readable per-step progress.

```json
{
  "ok": true,
  "skill": "antechamber-ligandprep",
  "dry_run": false,
  "outputs": {
    "mol2": "/abs/path/<NAME>.mol2",
    "frcmod": "/abs/path/<NAME>.frcmod",
    "run_dir": "/abs/path/antechamber-ligandprep-run"
  },
  "validation": {
    "atom_count": 12,
    "atom_types": ["ca", "ha"],
    "charge_sum": 0.0,
    "frcmod_missing": []
  },
  "errors": []
}
```

## Validation gates

- `mol2` exists, parses as Sybyl mol2, atom block has a populated charge column.
- No atom type equals `du` (antechamber's placeholder for "could not type").
- `frcmod` exists, parses, contains zero `ATTN` lines under BOND/ANGLE/DIHE/IMPROPER.
- Sum of the mol2 charge column is within `5e-3` of `--charge`. (Antechamber writes charges to 6-decimal precision; per-atom truncation accumulates to ~0.002 for small molecules. `5e-3` accepts that while still catching a real net-charge mismatch.)

`ok: false` is returned on any gate failure with the specific gate name in `errors[]`.

## Errors

| Code | Cause | Recovery |
|------|-------|----------|
| `MISSING_BINARY` | A required bin (`antechamber` / `parmchk2` / `obabel` / `pdb4amber`) is not on PATH. | Activate the `prime-amber` conda env or set PATH to include `$AMBERHOME/bin`. |
| `MISSING_ENV` | `AMBERHOME` is unset. | `source amber.sh` from the AmberTools install. |
| `INVALID_INPUT` | File extension unrecognized, file empty, or SMILES un-parseable. | Caller cleans input and retries. |
| `INPUT_PREP_FAILED` | `pdb4amber` or `obabel` exited non-zero on the input. | Inspect `<run_dir>/01_inputprep.err`. PDB likely has malformed `HETATM` columns or no atoms. |
| `AROMATIC_PERCEPTION_FAILED` | obabel logged `Failed to kekulize aromatic bonds` (H-absent PDB / sdf / SMILES path). Its perceived bond orders are unreliable, so GAFF2 typing would be silently wrong. | Supply a **fully-protonated PDB** (routes to direct antechamber perception, which kekulizes correctly) or a hand-built mol2. Inspect `<run_dir>/02_obabel_h.err`. |
| `SQM_CONVERGENCE_FAILED` | AM1 SCF in `sqm` did not converge during AM1-BCC. | See `references/heuristics.md` — try a different `--charge`, or hand-typed input. Rings + multiple halogens are the usual trigger. |
| `MISSING_PARAMETERS` | `parmchk2` flagged `ATTN` lines. | Manual frcmod edit, or alternative force field for the affected motif. |
| `NET_CHARGE_MISMATCH` | Sum of mol2 charges differs from `--charge` by more than `5e-3`. | Usually wrong `--charge` for the input topology; recompute. |

## How it works

Invocation (the agent makes ONE `exec` call):

```
python3 {baseDir}/scripts/wrapper.py --input <path-or-SMILES> --name <RESNAME> --charge <int> --output-dir <dir>
```

`{baseDir}/scripts/wrapper.py` runs the full chain as ordinary Python subprocesses and returns one envelope. Internal stages, suppressed from the LLM's view:

1. Input classification — PDB / mol2 / sdf / SMILES.
2. Input prep — **branches on whether a PDB already carries hydrogens**:
   - **PDB *with* H** → fed straight to `antechamber -fi pdb -j 4`, so antechamber does its **own** bond perception (kekulizes aromatic rings correctly and preserves ring N–H). `pdb4amber`/`obabel` are skipped.
   - **PDB *without* H** (and sdf) → `pdb4amber --nohyd` (PDB) then `obabel -p 7.4` to add hydrogens; SMILES → `obabel --gen3d -p 7.4`. These obabel steps are checked for a kekulization failure (→ `AROMATIC_PERCEPTION_FAILED`), since obabel can silently emit wrong bond orders when re-perceiving an aromatic from a heavy-atom-only skeleton.
   - **mol2** → passed through unchanged (the caller's bonds are trusted).
3. Atom typing + charges — `antechamber -c bcc -at gaff2 -rn <NAME> -nc <CHARGE>` (`-fi pdb -j 4` on the direct path, else `-fi mol2`).
4. Missing-parameter check — `parmchk2 -s gaff2`.
5. Validation sweep against the four gates above.

Each subprocess streams to `<run_dir>/NN_stage.{out,err}`. The envelope returns absolute paths.

**Limitation:** the wrapper does not auto-complete partial protonation or split multi-MODEL/alt-loc PDBs. Supply a fully-protonated, single-conformer PDB (it routes to direct antechamber perception) or use the SMILES/sdf path (obabel adds H).

## References

`references/heuristics.md` — adapted parameter heuristics from upstream `computational-chemistry-agent-skills/molecular-dynamics/antechamber/SKILL.md` (LGPL-3.0). Cited, not depended on.

## Acceptance test

`bash test_acceptance.sh` runs:
1. **Golden** — benzene from `project-prime/golden-path/ligand_raw.pdb` (extracted from PDB 181L; validated fixture from 2026-05-21; H-absent → obabel path). Asserts `ok: true`, atom types contain `ca` / `ha`.
2. **Unrelated** — methane via SMILES `C`. Asserts `ok: true`, atom types contain `c3` / `hc`.
3. **Malformed** — a deliberately broken PDB. Asserts `ok: false` with a parseable error code, NOT a silent crash.
4. **Aromatic** — indole from `project-prime/golden-path/1L2Y/ligand.pdb` (H-present → direct-antechamber path). Regression guard for the obabel-kekulize bug: asserts `ok: true`, atom types **contain** `ca` / `cc` / `cd` / `na` / `hn` (correct aromatic indole) and **exclude** `c2` / `ce` / `cf` / `ne` (the non-aromatic mis-typing).

All four must pass before Stage 2 flips to COMPLETE in `Phase3_Taskboard_Manifest.md`.

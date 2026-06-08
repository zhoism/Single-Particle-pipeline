# antechamber-ligandprep — parameter heuristics

Heuristics adapted from upstream `~/Downloads/Single Particle/upstream-reference/computational-chemistry-agent-skills/molecular-dynamics/antechamber/SKILL.md` (LGPL-3.0-or-later, © yuxt0261-ops). Cite, do not depend on, the upstream package at runtime — adapted text inherits the LGPL-3.0 obligation; runtime dependency is on the underlying AmberTools binaries only.

## Why this file exists

OpenClaw's stock skills are "documentation-skills" — the LLM constructs the antechamber CLI call from prose heuristics. The Project Prime model inverts that: the Python wrapper does the work; the SKILL.md is goal-oriented. The heuristics still matter, but for three reasons that live downstream of the wrapper:

1. **Stage 8 recovery** uses them to pick a recovery branch when a parameter step fails (e.g., switch `--charge`, fall back to `-at amber` for a modified residue).
2. **Stage 7 planner** uses them to decide which skill to invoke and with what inputs.
3. **Audit trail** — when a result is questioned (advisor review, anomalous trajectory), the heuristic chain explains why each parameter was chosen rather than burying it in code.

So this file is sibling to `SKILL.md` and consumed by the planner / recovery / human reader, not by the wrapper.

## Heuristics

### Charge method: `-c bcc` is the default

**When applies:** Any small-molecule ligand without pre-existing trusted partial charges. The wrapper always sets `-c bcc`.

**Why:** AM1-BCC produces RESP-quality charges in seconds rather than the hours that an RESP fit through Gaussian would take. For routine biomolecular MD this is the standard. RESP is only worth the cost when (a) the system is unusually charge-sensitive, or (b) the molecule is on the boundary of AM1's parameter coverage and the BCC correction is unreliable.

**Source:** Upstream `molecular-dynamics/antechamber/SKILL.md` §Charge Generation; Wang et al. 2000 (AM1-BCC original).

### Atom typing: `-at gaff2` is the default

**When applies:** Any non-standard residue or organic ligand. The wrapper always sets `-at gaff2`.

**Why:** GAFF2 (2014) is the current general-purpose force-field extension for AMBER protein simulations. GAFF (the original 2004 version) is superseded — torsion parameters in GAFF2 are refit and several atom types are corrected. Use `-at amber` only for modified residues that must be fully consistent with the protein force field (e.g., a phosphorylated tyrosine where the parameterized atoms must match the ff14SB envelope).

**Source:** Upstream `molecular-dynamics/antechamber/SKILL.md` §Atom Type Assignment.

### Net charge: `-nc` must match the protonation state on the input

**When applies:** Any non-neutral molecule. The wrapper threads `--charge` to `-nc <int>`.

**Why:** AM1-BCC integrates the partial charges to the requested net total. If `-nc` is wrong, you get a molecule whose summed partial charges are off by the difference, and the resulting electrostatics are physically wrong. The wrapper's validation gate checks `|charge_sum − requested|  ≤  1e-3` to catch silent mismatches.

**Source:** Upstream `molecular-dynamics/antechamber/SKILL.md` §Charge Generation.

### Input prep: route on whether the PDB already has hydrogens (2026-06-08 fix)

**When applies:** PDB input. The wrapper branches:
- **PDB *with* hydrogens** → fed straight to `antechamber -fi pdb -j 4`. `pdb4amber`/`obabel` are skipped.
- **PDB *without* hydrogens** (and sdf) → `pdb4amber --nohyd` then `obabel -p 7.4` to add H; SMILES → `obabel --gen3d -p 7.4`.

**Why:** The original chain *always* ran `pdb4amber --nohyd` (strip all H) → `obabel -p 7.4` (re-add H). For an **aromatic** ligand this is actively harmful: obabel must then re-perceive bonds from a heavy-atom-only skeleton, fails to kekulize the ring (`Failed to kekulize aromatic bonds`), and emits a non-aromatic perception that even drops the ring N–H. antechamber, fed that obabel `.mol2` (`-fi mol2`), trusts the broken bonds and types the ring as a conjugated polyene (`c2/ce/cf/ne`, no `ca/cc/cd/na`, no `hn`). Every output gate (charge, `du`, `ATTN`) passed → a silent scientific failure (confirmed on the 1L2Y indole; it had propagated into the published MM-GBSA ΔG). Feeding the **H-complete PDB** to antechamber with `-fi pdb -j 4` forces antechamber's *own* bond perception, which kekulizes correctly (verified: `6×ca, cc, cd, na, hn`, N–H restored). The `pdb4amber --nohyd` H-strip was a pattern borrowed from protein prep (ff19SB H-naming); it does not belong on a ligand that already carries good hydrogens.

**Source:** Project Day-8 (2026-06-08) bug investigation; verified empirically against the 1L2Y indole fixture. acdoctor stays ON on the direct path (it passes and types correctly — no need to silence it).

### Protonation: obabel `-p 7.4` only when hydrogens must be added

**When applies:** H-absent PDB, sdf, and SMILES paths (where the wrapper must add hydrogens). An H-complete PDB keeps the protonation state the caller supplied (more faithful than re-guessing at pH 7.4).

**Why:** Physiological pH is the default state for the protein-binding context. Setting `-p 7.4` makes obabel choose protonation per atom by the standard pKas, deterministically. But when the input already encodes a deliberate protonation/tautomer state (an H-complete PDB), re-running obabel would both discard that intent and risk the kekulization failure above — so the direct path trusts the input H instead.

**Source:** OpenBabel manual §Hydrogen treatment; project smoke-test 2026-05-19; Day-8 routing fix.

## Anti-heuristics (do not do this)

- **Don't use GAFF1 (`-at gaff`)** — superseded by GAFF2 (2014). Older trajectories may have been parameterized with GAFF1 for compatibility, but new work should be GAFF2.
- **Don't pass `-dr n` (acdoctor off) "to make it run"** — when acdoctor flags an input, it has found a real geometry / connectivity problem. Silencing it produces typed atoms with subtly broken bond patterns and the failure surfaces 10 hours later as an MD instability.
- **Don't run RESP without a real Gaussian ESP grid** — `-c resp` requires a Gaussian output file (`-fi gout`), a `.gesp`, or a GAMESS dat. The wrapper does NOT support RESP; defer that path to a separate skill if it's ever needed.
- **Don't hardcode `AMBERHOME` in any layer of this skill** — the wrapper resolves binaries via PATH first, then `$AMBERHOME/bin`. The advisor's `submit.sh` from `phase3-advisor-demo` hardcodes `/Application/software/Amber26/pmemd26`, which is the anti-example we're avoiding.
- **Don't write multi-line YAML for `metadata` in SKILL.md** — OpenClaw 2026.5.28's embedded parser only handles single-line JSON for the `metadata` key. Multi-line silently fails to load and the skill appears missing from `openclaw skills list`. (Empirical from Day 3 doc-read; canonical-paths memory §8.)

## Recurring failure modes

| Failure | Symptom | Root cause | Recovery |
|---------|---------|------------|----------|
| `SQM_CONVERGENCE_FAILED` | antechamber non-zero exit; `sqm.out` shows AM1 SCF did not converge in N cycles | Strained ring + multiple halogens, or input geometry with an unrealistic bond length | Re-energy-minimize the input geometry (obabel `--minimize` or a quick MM step), then retry. If still failing, fall back to `-c gas` (Gasteiger) — physically worse but unblocks the topology so the recovery skill can flag it for human review. |
| `MISSING_PARAMETERS` (ATTN lines) | parmchk2 frcmod contains `ATTN, need revision` lines under BOND/ANGLE/DIHE | A bond or angle in the molecule has no GAFF2 analogue (rare on common drug scaffolds, common on metal-containing or highly fluorinated species) | Manual frcmod edit using a chemically similar GAFF2 type, or a force-field swap (e.g., OpenFF). Out of Stage 2 scope; flag for Stage 8 recovery. |
| `NET_CHARGE_MISMATCH` | Validation gate fires with `sum != requested` | Wrong `--charge` for the protonation state obabel produced at pH 7.4. The common case: a histidine-like ligand where obabel chose a different tautomer than the crystal | Recompute net charge from the obabel-output mol2 (count formal charges in the atom block) and retry with the corrected `--charge`. |
| `INPUT_PREP_FAILED` | pdb4amber or obabel non-zero exit | Malformed PDB (truncated columns, no atoms, mixed alt-loc indicators) or unparseable SMILES | Caller cleans the input. The wrapper does NOT attempt to repair — that's a separate skill. |
| `AROMATIC_PERCEPTION_FAILED` | obabel `.err` contains `Failed to kekulize aromatic bonds` | An aromatic ligand was routed through obabel on a heavy-atom-only skeleton (H-absent PDB, or sdf/SMILES that obabel could not kekulize) → unreliable bond orders → wrong GAFF2 typing | Supply a fully-protonated PDB (routes to direct antechamber `-fi pdb -j 4` perception, which kekulizes correctly) or a hand-built mol2. This gate is the loud replacement for what used to be a silent mis-typing. |

## Attribution

Upstream: `jinzhezenggroup/computational-chemistry-agent-skills`, LGPL-3.0-or-later. Specific file referenced:

- `molecular-dynamics/antechamber/SKILL.md` — heuristics §Charge Generation, §Atom Type Assignment, §Basic IO adapted; flag tables consulted; no source code reused.

Upstream cloned read-only locally for reference at `~/Downloads/Single Particle/upstream-reference/computational-chemistry-agent-skills/`. The cloned tree is `.gitignore`d from this project's git so we never accidentally vendor it.

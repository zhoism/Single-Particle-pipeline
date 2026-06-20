# Workflow Feature Guide

What this pipeline can do, feature by feature. The system is a **decoupled hybrid
agent**: an LLM reads your intent and picks *one* skill; a deterministic Python
wrapper does the actual chemistry — with no LLM, network, or randomness inside it.
The model is the front door; the science is reproducible regardless of which model
(or rate limit) is behind it.

> Companion to [`README.md`](README.md) (the overview) — this is the full
> per-feature catalog. Each skill is a `SKILL.md` + a `scripts/wrapper.py` +
> a `test_acceptance.sh`; the LLM stays *outside* the wrapper.

---

## How you drive it

Three ways, the same wrappers underneath:

- **CLI** — call a wrapper directly; no LLM in the loop.
- **Natural language** — `openclaw agent` maps a plain-English goal to one skill's arguments.
- **Discord** — @-mention the bot to launch the full pipeline with live per-step updates.

**Model-agnostic** via the gateway — proven on free Cerebras `gpt-oss-120b` ($0) and
Google `gemini-3-flash-preview` (the default). ~$0.005 per agent turn; the MD runs
locally, so the simulation itself is $0. The LLM's only job is argument selection, so
**every NL drive is byte-identical to running the same CLI by hand**.

The flow: **ligand prep → system build → MD → analysis → interactions**, plus a
planner, a recovery skill, an async launcher, and a parameter editor.

---

## 1. `antechamber-ligandprep` — ligand parameterization (Stage 2)

Turns one small molecule into AMBER-ready parameters.

- **Accepts:** a `.pdb` / `.mol2` / `.sdf` file *or* a raw SMILES string (auto-detected), plus residue name + net charge.
- **Produces:** a GAFF2 `.mol2` (AM1-BCC charges) and a `.frcmod`.
- **Guards:** charge-sum match, no untyped atoms, frcmod completeness, and a **fatal aromatic-perception check** (no silent ring mis-typing). `--dry-run` prints the command chain.

## 2. `tleap-build` — system build & solvation (Stage 3)

Protein (± ligand) → a solvated, charge-neutral AMBER topology.

- **Accepts:** a protein PDB, an optional ligand (mol2 + frcmod), and force-field / water / buffer choices; protein-only builds supported.
- **Produces:** the solvated complex (MD input), the dry complex (analysis), and split protein/ligand topologies (MM-GBSA).
- **Guards:** atom-count & residue-identity checks, dry < solvated, component-sum, **structural net-neutrality** (prmtop charge block), and stray-HETATM / unknown-residue rejection. `--dry-run` writes the tleap script without running it.

## 3. `amber-md-run` — the MD engine chain (Stage 4)

Generates and runs the canonical 6-step explicit-solvent chain (3× minimize → heat → density → production).

- **Tunable:** production / heat / density lengths, cutoff, CPU ranks, engine (`pmemd` / `pmemd.MPI` / `sander`).
- **Modes:** `--steps min` for a fast smoke run; `--dry-run` to inspect every namelist before it runs.
- **Produces:** the production trajectory, per-stage restarts, and wall-time.
- **Guards:** the namelists are **fixed and physically clean** (dt = 2 fs + SHAKE, Langevin thermostat, coupled temperature ramp) — the LLM never writes one — plus a post-run NaN / SHAKE / incomplete-stage crash scan that hands failures to the recovery skill.

## 4. `cpptraj-analysis` — trajectory analysis + MM-GBSA (Stage 5)

The full post-MD suite on the production trajectory.

- **12 analyses:** RMSD, RMSF, Rg, SASA, DSSP, H-bonds, distance matrix, clustering, PCA, free-energy landscape, thermodynamics — run all or a chosen subset.
- **Plus MM-GBSA** binding free energy (when the protein and ligand topologies are present).
- **Produces:** a `.dat` + a publication `.png` per analysis, with auto-detected residue masks.
- **Guards:** the core RMSD/RMSF/Rg must succeed; optional analyses degrade gracefully; it always returns a result envelope, never a bare crash.

## 5. `plip-profile` — interaction profiling (Stage 6)

Extracts a representative frame and fingerprints the protein–ligand contacts.

- **Frame choice:** `medoid` (default, deterministic), `last`, or an explicit frame number.
- **Produces:** all 8 PLIP interaction categories (H-bonds, hydrophobic, π-stacking, salt bridges, …), the contact-residue set, ligand descriptors, and the annotated frame PDB.
- **Guards:** AMBER residue-name normalization with a **phantom-residue catch-all** (won't invent interactions from unmapped residues), and it refuses a ligand name that collides with a standard amino acid.

## 6. `md-planner` — the planning layer (Stage 7)

Validates, compiles, and executes a JSON plan that wires pipeline stages together.

- **Three modes:** `--validate` (gates only), `--dry-run` (compile + show the concrete commands), `--execute` (run the chain, halt on failure).
- **Capable of:** partial plans (e.g. just ligand prep), upstream→downstream output wiring, and per-stage validation conditions.
- **Guards:** six deterministic gate classes — known-catalog, unique/non-dangling IDs, acyclic DAG, satisfied inputs, MD-parameter bounds, and typed/known params — so an invalid plan **never compiles or runs**. The LLM writes the plan; the wrapper is a pure validator/executor.

## 7. `amber-recover` — bounded crash recovery (Stage 8)

Detects a genuine MD crash and salvages it within hard physical limits.

- **Detects:** NaN / Inf, SHAKE failure, temperature blow-up, box errors, abnormal termination (tolerating transient clamps that self-recover).
- **Recovers:** Tier 1 resume-from-checkpoint as-is → Tier 2 bounded SHAKE-off / smaller-dt stabilize-then-restore → a structured **HALT for a human** if it can't fix it safely.
- **Guards:** every namelist (mutated *and* original) is re-checked against `check_amber`; it refuses to breach the dt floor or run an out-of-bounds fix. `--detect-only` / `--dry-run` available. The LLM never diagnoses.

## 8. `pipeline-async` — detached full-pipeline launch

Fires the whole happy path as a background job and returns a run id immediately.

- **Accepts:** any target (protein / ligand-or-SMILES / charge / name), sim length, Discord channel, run id.
- **Produces:** live Discord progress — start → prep → topology → MD (each step) → analysis → done with the MM-GBSA ΔG and an RMSD plot, or a failure notice.
- **Guards:** up-front input validation; the detached job outlives the launching shell.

## 9. `mdin-edit` — natural-language parameter editor

Surgically edits a parameter in a pre-prepared `mdin` set from plain English.

- **Edits:** `dt`, `cut`, `temp0`, `restraint_wt`, `nstlim` — in one stage or a stage group (e.g. "third stage onward", "all stages").
- **Modes:** `--dry-run` to preview the change; `--submit` to prove the edited set runs a real `pmemd` smoke chain.
- **Guards:** range-checked before writing, **idempotent** (re-runs are byte-identical), stage-aware (won't put a param where it doesn't belong), couples `temp0` to the `&wt` ramp, all-or-nothing, with an audit log. The LLM never writes the file.

## The spine — `run_happy_path.sh`

The agent-free verification backbone that chains Stages 2–6 on any target and proves the result.

- **Default target** is the built-in 1L2Y fixture (a no-arg run works); `--protein` / `--ligand` / `--charge` / `--name` point it at anything else; `--sim-ps` sets length.
- **Asserts:** every stage envelope is `ok:true`, then the run produced ≥ 12 analyses, ≥ 10 plots, and a favorable (negative) MM-GBSA ΔG.
- **Opt-in:** `RECOVER=1` adds the recovery hook; `NOTIFY_CHANNEL` adds Discord updates — both non-fatal, default-off.

---

## The safety model (shared by every skill)

- **Deterministic gates, not trust.** Each gate is a cheap *proxy invariant* — true when a step worked, false when it broke (dt ≤ 2 fs, net charge ≈ 0, no NaN in the final energy block). It is **not** an "is the science correct" oracle — that can't exist.
- **Physical-realism bounds** live in a shared `check_amber` layer reused by the editor, the planner, and the recovery skill, so all three agree on what's legal.
- **One JSON envelope per skill** (`ok / outputs / validation / errors`) — uniform and machine-checkable.
- **see / do / verify:** `--dry-run` shows what a skill *would* do; the spine *does* it; each skill's `test_acceptance.sh` (golden + unrelated + malformed) *verifies* it.
- **New gates earn their place:** proxy invariant → oracle/regression test → adversarial review → commit. The LLM may *propose* a gate; it never *authors* a trusted one.

## Honest scope

- **CPU-only, local.** No GPU (`pmemd.cuda`) or remote-HPC dispatch yet — that's a designed-in seam, not exercised.
- **MM-GBSA ΔG is a short-run sanity number** (~±1 kcal/mol run-to-run), not a converged binding affinity — the *method* is verified, the *magnitude* is illustrative.
- **One ligand, standard residues.** Cofactors, metals, and multi-ligand systems are out of scope for now.

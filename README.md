# Single Particle — Agentic AMBER MD Pipeline

An **agentic workflow for explicit-solvent molecular dynamics in AMBER**. An LLM
decides *what* to run; hardened **deterministic Python wrappers** do *how* the
science executes. The model is the front door; the science is reproducible and
bounded regardless of which model (or rate limit) is behind it.

> Internship / demo project. Runs locally on a CPU-only Mac (AmberTools + a
> from-source `pmemd`). MM-GBSA ΔG figures here are short-run **sanity numbers**,
> not converged binding affinities — the *method* is verified, the *number* is
> illustrative.

## The idea (decoupled hybrid agent)

The system separates **reasoning** from **execution** so a flaky or unconstrained
LLM can never corrupt mission-critical chemistry:

```
  You (Discord / CLI)
        │  "run the full MD pipeline at 50 ps"
        ▼
  OpenClaw agent ── the ONLY LLM-in-the-loop step: it picks ONE skill
        │
        ▼
  deterministic wrapper(s)  ── plain Python; NO LLM / network / randomness inside
        │                       each validates its own output and emits a JSON envelope
        ▼
  AMBER (tleap / pmemd / cpptraj / MMPBSA) + PLIP
        │
        ▼
  results + per-stage progress (Discord notifications are LLM-free)
```

Every parameter the agent could propose is barred by **deterministic gates**
(physical-realism bounds in `check_amber`, per-skill validation, the bounded
recovery ladder) *before* it can touch the science. See
[`skills/*/SKILL.md`](skills) for each skill's contract.

## The nine skills

Each skill = a `SKILL.md` goal description + a `scripts/wrapper.py` that does the
work and validates its own output + a `test_acceptance.sh`. The LLM stays
*outside* the wrapper.

| Skill | Stage | What it does |
|-------|-------|--------------|
| `antechamber-ligandprep` | 2 | ligand (PDB/mol2/SDF/SMILES) → GAFF2 types + AM1-BCC charges + frcmod. Gates: charge sum, no untyped atoms, frcmod completeness, aromatic perception. |
| `tleap-build` | 3 | protein + ligand → solvated, neutralized topology (dry saved *before* solvation). Gates: atom counts, residue identity, dry<solvated, component sum, **net neutrality** (prmtop CHARGE-block). |
| `amber-md-run` | 4 | 6-step `min → heat → density → production` chain via `pmemd`; physical-realism-bounded namelists; post-run crash scan. |
| `cpptraj-analysis` | 5 | 12-analysis suite (RMSD/RMSF/Rg/SASA/DSSP/H-bond/dist-matrix/clustering/PCA/FEL/thermo) + MM-GBSA + plots. |
| `plip-profile` | 6 | representative frame → AMBER-resname normalization → PLIP → structured interaction envelope. |
| `md-planner` | 7 | goal → JSON **plan manifest** over the known skill catalog; deterministic `validate (G0–G6) → compile → execute`. |
| `amber-recover` | 8 | deterministic crash detector (NaN/Inf/SHAKE/box/overflow) + **bounded** tiered recovery (checkpoint-restore → bounded stabilize), every namelist re-validated. |
| `pipeline-async` | — | detached full-pipeline launch + LLM-free per-stage Discord progress. |
| `mdin-edit` | — | natural-language parameter editor over an existing `mdin` set — idempotent, bounds-checked, stage-aware. |

> Full per-feature catalog (every flag, mode, and guardrail, by section): **[`FEATURES.md`](FEATURES.md)**.

## Why deterministic gates (the thesis)

Reliability comes from **cheap deterministic proxy invariants**, not from trusting
the model. Each gate is true when a step worked and false when it broke (e.g.
`dt ≤ 2 fs`; net charge ≈ 0; no NaN in the final energy block) — *not* a general
"is the science correct" oracle, which cannot exist. New gates are only added
after clearing a discipline: **proxy invariant → oracle/regression test →
adversarial review → commit**. The LLM may *propose* a candidate; it never
*authors* a trusted gate.

## Quickstart

**Prereqs:** the `prime-amber` conda env (AmberTools 24.8 + PLIP; see
`env.lock.yml`) and a local `pmemd` (built from source). Then:

```bash
# activate the toolchain
source /opt/homebrew/Caskroom/miniforge/base/envs/prime-amber/amber.sh
export PATH="$HOME/Downloads/pmemd26/bin:$AMBERHOME/bin:$PATH"

# end-to-end on the built-in 1L2Y fixture (agent-free verification spine)
bash run_happy_path.sh 50

# any target
bash run_happy_path.sh 50 --protein my_protein.pdb --ligand LIG.pdb --name LIG --charge 0

# inspect what a single skill WOULD do, without running it
python3 skills/tleap-build/scripts/wrapper.py --dry-run --help
```

`run_happy_path.sh` chains stages 2–5, asserts `ok:true` on every JSON envelope,
and then asserts the run produced ≥12 analyses, ≥10 plots, and a favorable
(negative) MM-GBSA ΔG.

## Verification model — see / do / verify

- **see** — `--dry-run` on any wrapper prints the exact inputs it would generate.
- **do** — `run_happy_path.sh` runs the chain.
- **verify** — per-skill `test_acceptance.sh` (golden + unrelated + malformed) +
  the spine's envelope/output assertions.

## Repo layout

```
run_happy_path.sh     # the deterministic verification spine (stages 2–5)
skills/<name>/        # SKILL.md + scripts/wrapper.py + test_acceptance.sh
scripts/              # env.sh, notify_discord.sh, recover_hook.sh, watch_ratelimits.sh
golden-path/          # the 1L2Y validation fixture (inputs)
env.lock.yml          # conda environment lockfile
```

Run outputs, trajectories, and scratch dirs are gitignored (regenerated on demand).

## Honest scope / caveats

- **CPU-only, local.** No `pmemd.cuda`; remote-HPC dispatch is a designed-in seam,
  not yet exercised.
- **Single-trajectory MM-GBSA ΔG is a sanity number** (run-to-run ~±1 kcal/mol on
  short runs), not a converged affinity.
- **Demo-scale.** The deliverable is "an agent planned, ran, validated, and
  recovered an MD run," not publishable chemistry.

---
*Companion design notes, architecture, and the development log live in a separate
private docs repository. This repo is the executable pipeline.*

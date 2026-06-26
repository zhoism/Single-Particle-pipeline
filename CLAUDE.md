# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`project-prime/` is the **code home** for **Project Prime** — an internship project building OpenClaw Agent Skills that automate explicit-solvent MD simulations in AMBER, with post-processing through CPPTRAJ and PLIP. The agent is the front door; the science runs in hardened **deterministic Python wrappers** that the LLM cannot reach inside.

This directory is the *runnable* side. The *design / planning / market-research* side lives in the sibling Obsidian vault at `../Single Particle/` (the folder name has a space) — GitHub `zhoism/Single-Particle`. This code repo is `zhoism/Single-Particle-pipeline`. Read the vault's `CLAUDE.md` first for the architectural big picture; this file is scoped to the code.

## Current state (as of 2026-06-24)

The local pipeline is **complete and green** end to end. For the live, non-volatile detail — what shipped, what's open, and the latest decisions — read these instead of trusting any snapshot here:

- `README.md` — current scope, the decoupled-hybrid-agent thesis, and quickstart.
- `FEATURES.md` — the full per-skill workflow guide (every flag, mode, and guardrail).
- `../Single Particle/Dev_Log.md` — the running development log (single source of truth for "what changed and why").

One-paragraph orientation so a fresh session isn't blind: all **9 skills are built and passing** — `antechamber-ligandprep`, `tleap-build`, `amber-md-run`, `cpptraj-analysis`, `plip-profile`, `md-planner`, `amber-recover`, `pipeline-async`, `mdin-edit`. Deployment is **fully local, CPU-only (Scenario A)**: AmberTools comes from the `prime-amber` conda env and `pmemd` is built from source. The big open item is **remote-HPC dispatch** — a designed-in seam, not yet exercised (tracked in the vault as `Gap_Remote_HPC_Backend`).

## Toolchain (pinned)

- **AMBER:** AmberTools 24.8 from the `prime-amber` conda env (`env.lock.yml`; Python 3.11.15, PLIP 3.0.0). `pmemd 26` built from source at `~/Downloads/pmemd26` (212/0 tests passing).
- **Agent framework:** OpenClaw 2026.5.28.
- **Source the env before running anything:**
  ```bash
  source /opt/homebrew/Caskroom/miniforge/base/envs/prime-amber/amber.sh
  export PATH="$HOME/Downloads/pmemd26/bin:$AMBERHOME/bin:$PATH"
  ```

## LLM backend stack

- **Default model:** `google/gemini-3-flash-preview`, reached through OpenClaw's `--gateway`. ~$0.005 per agent turn; the MD itself runs locally, so the simulation is $0.
- **Model-agnostic by design.** The LLM's only job is argument selection, so **every natural-language drive is byte-identical to running the same CLI by hand**. Don't hard-code an Anthropic/OpenAI/Gemini client at a skill's call site — the gateway picks the backend; the skill just declares the LLM call's input shape.
- **Superseded — do not reintroduce:** Ollama-as-primary (dropped 2026-05-20 — insufficient Mac headroom) and Cerebras `gpt-oss-120b` (a transient free default). These are gone, not "fallbacks."

## How to run it (real commands)

```bash
# End-to-end on the built-in 1L2Y fixture — the agent-free verification spine (no args needed).
bash run_happy_path.sh

# Any target: protein PDB + ligand (file OR inline SMILES) + net charge + residue name.
bash run_happy_path.sh --sim-ps 50 --protein new-target-3HTB/protein.pdb \
  --ligand new-target-3HTB/ligand.pdb --name JZ4 --charge 0

# See what one skill WOULD do without running it (every wrapper supports --dry-run).
python3 skills/amber-md-run/scripts/wrapper.py --dry-run --top T.prmtop --crd T.inpcrd --output-dir /tmp/md

# Verify a single skill in isolation (golden + unrelated + malformed cases).
bash skills/tleap-build/test_acceptance.sh
```

`run_happy_path.sh` chains Stages 2–6, asserts `ok:true` on every JSON envelope, then asserts the run produced ≥12 analyses, ≥10 plots, and a favorable (negative) MM-GBSA ΔG. Opt-ins: `RECOVER=1` adds the bounded-recovery hook; `NOTIFY_CHANNEL=<id>` adds LLM-free Discord progress.

## Test systems & sanity numbers

- **1L2Y** is the default fixture (`golden-path/1L2Y/`). **3HTB** (T4-lysozyme + JZ4) is the proven arbitrary target — evidence that skills are system-agnostic, not 1L2Y-hardcoded.
- A short-run MM-GBSA **ΔG on 1L2Y of ≈ −17 to −18 kcal/mol (±1)** is the expected run-to-run **sanity** band — the *method* is verified, the *magnitude* is illustrative, not a converged affinity.
- **A ΔG of −12.84 is retracted garbage — do not cite it.**

## SOPs that carry over from the vault

These are enforced by the architecture, not optional:

1. **Plan first** via the `Arch_Taskboard_Manifest` pattern — translate the scientific goal into stage/input/output/validation specs before any skill executes.
2. **Physical realism is non-negotiable** for MD parameters — `dt ≤ 2 fs`, non-bonded `cut` in valid range, SHAKE on. Validate deterministically (regex, numeric bounds via the vendored `check_amber` layer) *before* the simulation runs — never by trusting the LLM.
3. **Memory Provenance labels** (`Design_Memory_Provenance`) — facts carry a provenance label; the LLM gets to play in *inferred* space only. Anything reaching `pmemd`/`cpptraj` must be observed or confirmed first.
4. **Bounded recovery** (`Workflow_Error_Recovery_Loop` + `Skill_Bounded_Recovery_AMBER`, shipped as the `amber-recover` skill) — when a run crashes, recover via mathematically bounded fixes (resume-from-checkpoint, smaller `dt`, SHAKE-off within limits), not LLM-invented patches.
5. **Skills are scalable** — handle any ligand/system; never hardcode the test case.

## Working principles (coding discipline)

A base layer for code work in this repo. For **vault** work, the vault's own action-first conventions govern (these don't override them).

1. **Think before coding.** State the assumptions a change rests on and surface real tradeoffs. Batch any genuinely *blocking* decisions into a **single pre-run check** rather than asking question by question — and bias to action on routine, reversible work. The one hard line for THIS repo: **never let the LLM invent MD parameters** — physical realism is deterministic and lives behind `check_amber`, the per-skill gates, and the bounded-recovery ladder.

2. **Simplicity first.** Write the minimum code that satisfies the task. No speculative abstraction, no config knob "for later," no framework where a function does. Before adding structure, ask: *would a senior engineer call this overcomplicated?* The wrappers are plain Python with a JSON envelope on purpose — keep them that way.

3. **Surgical changes.** Touch only what the task needs. Match the surrounding style of the file you're in. Remove only the orphans *your* change created; don't delete pre-existing dead code unless asked. Every changed line should trace back to the request.

4. **Goal-driven execution.** Turn each task into a verifiable goal, then verify against the **real assets** this repo already has — don't invent a new oracle:
   - the agent-free **spine** `run_happy_path.sh` (envelope `ok:true` + ≥12 analyses / ≥10 plots / negative ΔG),
   - the vendored **`check_amber`** bounds validator (the physical-realism gate),
   - each skill's **`test_acceptance.sh`** (golden + unrelated + malformed) and `tests/` unit dirs,
   - the **physical-realism gates** baked into every wrapper.
   "Fix the bug" means: **write a test that reproduces it first, then make it pass.** A change isn't done until the relevant gate is green.

## Definition of Done (anti-drift)

A change isn't done when the gate goes green — it's done when the **record** catches up too. Shared discipline + the full status-document inventory live in the vault's `Definition_of_Done.md`; this is the code-side half.

A **substantive code result** (a skill/wrapper change, a verified bug-fix, a new gate, a toolchain bump — *not* a scratch experiment) is **not done until:**

1. **Status docs current** — `README.md` (scope/thesis) and/or `FEATURES.md` (per-skill flags/guardrails) updated if behavior changed; the affected skill's `references/` updated; and a marker in `../Single Particle/Dev_Log.md` (the shared running log — single source of truth for what changed and why).
2. **Commit** the completed result.
3. **Independent review of that commit before its push** — run `/code-review` on the diff, fix-or-accept the findings (the [[feedback-verify-and-eval]] discipline). Each commit is reviewed once.
4. **Push** (`git push` prompts — that's the gate moment).

Both repos are private + solo, so push is low-stakes and reversible; the gate catches drift, it doesn't gatekeep. A user-scope `Stop`-hook backstop (in `~/.claude`) nudges once if a session ends with uncommitted or unpushed work here. *Don't push a feature branch's commits until each has been reviewed.*

## Companion vault — when to use which

For *design* questions, check the vault first: `../Single Particle/` (GitHub `zhoism/Single-Particle`). Landmark notes:

- `Project Prime.md` — phased roadmap and SOPs
- `Dev_Log.md` — running development log (what changed, when, why)
- `Arch_Taskboard_Manifest.md` — planner meta-skill
- `OpenClaw_Lobster_DAGs.md` — deterministic execution layer
- `Skill_Bounded_Recovery_AMBER.md` — recovery protocol
- `Design_Memory_Provenance.md` — observed/confirmed/inferred reasoning
- `Gap_Remote_HPC_Backend.md` — the big open item (HPC dispatch)

**Edits to vault notes go through the `mcp__obsidian__*` tools** (which route via the live Obsidian app + Local REST API plugin), not raw `Edit`/`Write` on disk paths. See the vault `CLAUDE.md` for the rationale.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`project-prime/` is the **code home** for **Project Prime** — an internship project building OpenClaw Agent Skills that automate explicit-solvent MD simulations in AMBER, with post-processing through CPPTRAJ and PLIP.

This directory is the *runnable* side of the project. The *design / planning / market-research* side lives in the sibling Obsidian vault at `../Single Particle/` (filename uses a space). Read that vault's `CLAUDE.md` first for the architectural big picture — this file is scoped to the code.

## Current state (as of 2026-05-14)

Nothing is installed yet. No AMBER, no OpenClaw, no Python virtual env. This scaffold is the empty harness; the vault's design notes describe what *will* be built.

**Resolved:**
- Deployment is fully local (no remote cluster) — AMBER compiles from source.
- LLM stack: **Ollama** as primary, **Google AI Studio API** as fallback for heavier reasoning. Both within free / limited-usage tiers.
- End-of-internship deliverable: working agent demo + written report.

**Still open:**
- Test protein-ligand system not chosen — skills must stay system-agnostic on day 1.
- Canonical `skill.md` format source — using public OpenClaw docs unless Single Particle provides an internal template.

## Layout

- `skills/` — OpenClaw skills, one subdirectory per skill. Each holds a `skill.md` (declarative spec, OpenClaw-readable) plus any Python/shell it shells out to. Skill names should mirror the `Skill_*.md` design notes in the vault.
- `runs/` — Simulation I/O scratch space (input files, trajectories, mdout, logs). Gitignored as a whole — trajectories are gigabyte-scale and must never enter version control.
- `docs/` — Internship report drafts, market-research write-ups, anything destined for an external audience.

## LLM backend stack

- **Primary:** Ollama (local, free, small-to-medium models). Use for routine reasoning — parameter parsing, plan generation, log inspection.
- **Fallback:** Google AI Studio API (limited free tier). Reserve for genuinely "intense" reasoning steps where the local model isn't capable.
- Skills should stay **backend-agnostic at the call site** — don't hard-code Anthropic/OpenAI client patterns. The orchestration layer picks the backend; the skill just declares "needs an LLM call with this input shape."

## SOPs that carry over from the vault

These are enforced by the architecture, not optional:

1. **Plan first** via the `Arch_Taskboard_Manifest` pattern — translate the scientific goal into stage/input/output/validation spec before any skill executes.
2. **Physical realism is non-negotiable** for MD parameters — `dt ≤ 2fs`, non-bonded `cut` in valid range, SHAKE constraints. Validate deterministically (regex, numeric bounds) before the simulation runs — never by trusting the LLM.
3. **Memory Providence labels** (`Design_Memory_Providence`) — every fact is Observed / Confirmed / Inferred. The LLM gets to play in *Inferred* space only; anything reaching `sander`/`pmemd.cuda` must be Confirmed or Observed.
4. **Bounded recovery** (`Workflow_Error_Recovery_Loop` + `Skill_Bounded_Recovery_AMBER`) — when a simulation crashes, recover via mathematically bounded fixes (lower `dt`, disable SHAKE, resume), not LLM-invented patches.
5. **Skills are scalable** — handle any ligand/system, never hardcode the test case.

## Companion vault — when to use which

For *design* questions, check the vault first: `../Single Particle/`. Landmark notes:
- `Project Prime.md` — phased roadmap and SOPs
- `Arch_Taskboard_Manifest.md` — planner meta-skill
- `OpenClaw_Lobster_DAGs.md` — deterministic execution layer
- `Skill_Bounded_Recovery_AMBER.md` — recovery protocol
- `Design_Memory_Providence.md` — Observed/Confirmed/Inferred reasoning

**Edits to vault notes go through the `mcp__obsidian__*` tools** (which route via the live Obsidian app + Local REST API plugin), not raw `Edit`/`Write` on disk paths. See the vault `CLAUDE.md` for the rationale.

## Common commands (placeholder — fill in as the stack solidifies)

- Package manager: `uv` (assumed; revisit if poetry/pip-tools is preferred)
- Run a skill locally: TBD
- Test a parameter validator: TBD
- Launch a simulation: TBD (depends on AMBER install + DPDispatcher wiring)

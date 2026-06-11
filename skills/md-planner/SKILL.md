---
name: md-planner
description: "Plan an AMBER molecular-dynamics workflow from a goal: validate a JSON plan manifest deterministically, compile it to a concrete execution plan, and (with --execute) run the chain manifest-first. The manifest selects + parameterizes + wires stages from the KNOWN skill catalog (antechamber-ligandprep, tleap-build, amber-md-run, cpptraj-analysis, plip-profile) — the main agent maps the goal to the manifest; this wrapper is a pure, deterministic validator/compiler/executor (no LLM call inside). Validation is deterministic, not human-review: DAG-acyclic, every input satisfied by an upstream output or a provided file, MD params within the reused check_amber bounds, typed params. Generalizes the hardcoded run_happy_path chain (partial + reordered chains become first-class) without touching it. System-agnostic."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: --validate/--dry-run are pure Python (no toolchain). --execute shells out to the chained skill wrappers, which need AmberTools (antechamber/tleap/cpptraj) + pmemd. macOS CPU.
metadata: {"openclaw":{"requires":{},"os":["darwin"]},"inputs":{"manifest":"path to the JSON plan manifest (the agent writes it from the goal)","run-root":"working dir for --execute (default ./md-planner-run)","validate":"validate only","dry-run":"validate + compile the plan, no execution (default)","execute":"validate + compile + run the chain (gated, HALT on failure)"},"outputs":{"plan":"compiled execution plan (ordered concrete CLI calls + wiring) [dry-run]","completed":"stages that ran [execute]","halted_at":"the stage that failed [execute]","stage_envelopes":"per-stage envelope paths [execute]"},"validation":["deterministic_gates_G0_G6","dag_acyclic","inputs_satisfied_vs_registry","md_params_within_check_amber_bounds","typed_params","no_llm_in_wrapper"],"dry_run":true,"source":"project-prime/skills/md-planner","stage":"Phase3.Stage7"}
---

# md-planner

## Goal

The **planning layer** of the decoupled hybrid agent: turn a scientific goal into
a **validated** stage/input/output/validation spec *before* execution. The LLM
(main agent) reasons only over the known catalog — selecting, parameterizing, and
wiring stages — and a **deterministic validator promotes that `inferred` manifest
to `confirmed`** before any byte reaches pmemd. This is the `Arch_Taskboard_Manifest`
slot / CLAUDE.md SOP #1 in skill form; the standard plan-and-execute pattern
(Plan-and-Solve, LangGraph) hardened with deterministic gates. See
`references/plan-manifest.md`.

## Modes

| Mode | Does | Touches pmemd |
|------|------|---------------|
| `--validate` | run the gates, emit the verdict | no |
| `--dry-run` (default) | validate + **compile** to a concrete, byte-inspectable execution plan | no |
| `--execute` | validate + compile + **run** the chain, gating each transition; HALT on failure | yes |

## The plan manifest (JSON — the main agent emits it)

```
{ "manifest_version":"1", "goal":"…",
  "system": {"protein":{"path":…}, "ligand":{"path":…}|{"smiles":…}, "charge":0, "name":"MOL"},
  "stages": [ {"id":"s2", "skill":"antechamber-ligandprep",
               "params":{"name":"MOL","charge":0},
               "inputs":{"input":{"from":"system.ligand"}},
               "validate":["envelope_ok","output_exists:mol2"]}, … ] }
```

- An input edge is `{"from":"<upstream_id>.<output_key>"}` (wired from a prior
  stage's declared output) or `{"from":"system.<field>"}` (a provided file). The
  JSON key is the skill's CLI flag minus `--`.
- `validate` is a **closed** vocabulary: `envelope_ok`, `output_exists:<key>`,
  `numeric:<dotted.path><op><value>`, `count_at_least:<dotted.path>:<n>`.
- `on_fail:"continue"` (default `"halt"`) keeps a non-fatal addendum (e.g. PLIP)
  from regressing the chain.
- A **partial** manifest is a shorter `stages` list — "just prep the ligand" = S2
  only — and is first-class.

## Deterministic gates (G0–G6)

shape (id/skill strings; `validate` a list of strings — malformed input is
rejected gracefully, never a crash) · **known catalog** (the bounded-LLM gate) ·
unique/non-dangling ids · **DAG acyclic** incl. self-loops (the topo order is the
execution order) · **every required input satisfied** by an upstream declared
output or a provided file (checked against the registry I/O contract) · **MD params
within the reused `check_amber` bounds** (cut∈[8,12], sim-ps>0) · **typed params**
(charge int, name `\A[A-Z0-9]{1,4}\Z`, sim-ps>0) · **any param not in the skill's
catalog is rejected** (so a hallucinated or unphysical flag — e.g. `dt` — cannot
reach the real CLI). Fail-collect → one complete diagnosis. `ok` iff no FAIL (WARN
is executable). The verdict/`Finding` shape matches `check_amber`/`mdin-edit`.

## What it does NOT do

- **No LLM inside the wrapper** — the agent maps goal→manifest; this stays a pure
  function (the determinism thesis; goal→manifest is the only `inferred` artifact).
- **No recovery** — on a stage failure the executor HALTs with the failed stage's
  envelope intact; Stage 8 `amber-recover` (and `run_happy_path.sh`'s opt-in
  `RECOVER=1` hook) owns recovery.
- **Does not touch or invoke `run_happy_path.sh`** — the executor calls the skill
  wrappers directly, so it cannot regress the proven spine.

## Acceptance test

`bash test_acceptance.sh` — golden `--dry-run` byte-asserts the compiled plan;
partial S2 validates; malformed (cyclic / unknown-skill / out-of-bounds-cut /
bad-name / missing) → `ok:false` + code; `--execute` S2-only runs real antechamber;
`LIVE=1` runs the full chain manifest-first at `--sim-ps 1` (ΔG<0).
`python3 tests/test_planner_oracle.py` — independent validator oracle (golden +
malformed matrix, py3.9 + py3.11). `python3 tests/test_registry_consistency.py` —
registry CLI-flag/output-key drift guard + vendored-check_amber parity.

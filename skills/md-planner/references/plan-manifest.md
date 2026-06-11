# The plan manifest — design, thesis, and lineage

This skill is the **planning layer** of the project's decoupled hybrid agent
(`Arch_Taskboard_Manifest`, CLAUDE.md SOP #1, `Design_Determinism_Spectrum`),
in skill form.

## The bounded-planner thesis

`Design_Determinism_Spectrum` places every surveyed system on one axis: *how much
of the workflow do you trust an LLM to reason about vs. hard-code away?* The
planner is where this project spends its LLM-reasoning budget — and it spends it
**bounded**:

- The LLM (the **main agent**, not this wrapper) maps a natural-language goal to a
  manifest. That is the *unstructured 5%* — picking which stages, with which
  params, wired how.
- The manifest may only **select, parameterize, and wire stages from the KNOWN
  catalog** (the five forward-chain skills). It cannot invent `gromacs-run` or a
  free-form workflow — the `UNKNOWN_SKILL` gate enforces this.
- A **deterministic validator** then promotes that `inferred` manifest to
  `confirmed` (the `Design_Memory_Provenance` discipline) before any byte reaches
  pmemd. The validator is pure Python — DAG soundness, I/O-contract satisfaction,
  reused `check_amber` bounds, typed params — never an LLM, never human-review.

This is the deliberate contrast with the incumbents and the upstream reference:
they get reliability by *removing the LLM* (Schrödinger) or *gating on human review*
(`light-cyan/AgentTaskboardManifest`). Here the LLM reasons over a deterministic
core, and the gate that keeps it honest is deterministic, not a human.

## Why no LLM inside the wrapper

Every other skill in this project follows "the main agent maps NL → structured
args; the deterministic wrapper validates/executes." Putting an `openclaw infer`
call *inside* this wrapper would (a) make a skill the project promises is
deterministic depend on a model, (b) couple it to a backend, and (c) make it
un-byte-testable. So the manifest is the only `inferred` artifact; the validator
is the deterministic promotion to `confirmed`. (An optional in-wrapper
`--from-goal` mode is named-not-built v2 — and would carry exactly these costs.)

## Lazy-load, made real

"Only the active stage in context" is a concrete property here, not a slogan. The
compiler **flattens the validated DAG into a flat list of independent, fully-bound
calls** — each call references only its already-resolved inputs, not the graph. The
executor then holds exactly one call + a `produced_paths` dict at a time. *Validate
globally once; execute locally one stage at a time.* This is the deterministic
analogue of a LangGraph state machine (global state = `produced_paths`; each node
reads only the keys its edges name), without an unbounded LLM per node.

## Why generalize the spine instead of replacing it

`run_happy_path.sh` is a *fixed* S2→S6 spine with a hardcoded final verdict. The
manifest generalizes it: partial ("prep the ligand" = S2 only) and reordered chains
become first-class, and the per-stage gates are *declared* (the `validate`
vocabulary expresses everything the spine asserts today: ≥12 analyses, ΔG<0). The
executor **re-implements the spine's `ok()`/`jget()` envelope-check loop in Python
and calls the wrappers directly** — it never edits or invokes `run_happy_path.sh`,
so the proven spine (and `pipeline-async` which depends on it) cannot regress. The
executor is deliberately *thin*: no retry, no recovery, no parallelism — the moment
it grows a recovery branch it has stolen Stage 8's job.

## Registry = the Confirmed I/O contract

`scripts/registry.py` is a dict literal (not a runtime SKILL.md parse) so the
contract the validator gates against is version-controlled and frozen at commit
time. The one risk of a literal — drift from the wrappers it transcribes — is closed
by `tests/test_registry_consistency.py`, which checks every registry CLI flag and
output key against the actual wrapper source. Two non-obvious load-bearing entries:
`amber-md-run.md_dir` (its working dir = what `cpptraj-analysis` takes as
`--mdout-dir`), and `tleap-build.ligand-frcmod requires_with ligand-mol2`.

## Tier + citations (keep the report honest)

**🟡 our framing of the standard plan-and-execute / planner-agent pattern** — *not*
paper-novel. arXiv:2603.25522 supports the general concept ("planning skills
externalize task descriptions into executable task specifications") but does **not**
coin "Taskboard Manifest" or specify the lazy-load / deterministic-validation-gate
mechanics; those are vault framing. **Do not call it "Taskboard Manifest" in the
report** — call it a *plan manifest*. Cite the lineage:

- Plan-and-Solve prompting (Wang et al., 2023) — plan then execute.
- LangGraph / LangChain plan-and-execute agents — graph state machines with
  deterministic node transitions (the model for the compiler/executor).
- `light-cyan/AgentTaskboardManifest` (LGPL-3.0) — prior art for the *idea*
  (lazy-load, I/O-contract-per-task, prohibit-native-generation, zero-overconfidence).
  We borrow the principles, not the YAML / human-review-gated format. Cite, don't
  depend.
- `Arch_Recursion_LOWE` — the planner-executor loop + strict verifier
  (Predict→Test→Falsify→Improve): the validator *is* the up-front falsification of
  an LLM-proposed plan before compute.

# Bounded recovery — the design this wrapper implements

This skill is the runnable form of the vault's strongest **paper-cited** element.
It implements, deterministically, the tiered recovery argued in
`Skill_Bounded_Recovery_AMBER` + `Workflow_Error_Recovery_Loop`, with the
control structure of `Arch_Recursion_LOWE` and the positioning of
`Design_Determinism_Spectrum`.

## Why this is defensible (the "you're letting an AI change physics" answer)

The competitive contrast (Round-2 survey): Schrödinger Multisim's *automatic*
crash response is a bitwise checkpoint-restore with identical config — it never
autonomously changes parameters (a human must use `-set` on a manual restart).
That is very safe but cannot fix a physically unstable run unattended. The gap
incumbents leave is **autonomy**, not capability.

So this skill is **tiered**, and mutation is the *escalation*, never the first
move:

1. **Tier 1 — checkpoint-restore, as-is.** Identical to the industry-default safe
   path. Restore the last good `.rst` and resume the crashed stage unchanged.
   Resolves transient failures (killed job, node death, timeout) with **zero
   physics risk**.
2. **Tier 2 — bounded parameter mutation, only on re-crash.** If Tier 1 re-crashes,
   run a bounded **stabilization** window — lower `dt` down a ladder and **disable
   SHAKE** — from the last good checkpoint; once the system survives N steps,
   **restore the original sane parameters** and resume. Disabling SHAKE removes
   the convergence-failure mode (`Coordinate resetting cannot be accomplished`);
   the tiny `dt` keeps a strained geometry from overflowing while Langevin
   friction bleeds off the excess. This is verbatim the protocol in
   `Skill_Bounded_Recovery_AMBER`.
3. **HALT — bounded.** If the `dt` floor is reached, the retry budget is spent, or
   a bound forbids the only fix, stop and return a structured human-request.
   Halting *is* the guarantee, not a failure of it.

## Detection is deterministic, not agentic

The LLM is barred from the diagnosis + execution path (it only picks the skill).
Failure is detected by regex/numeric parse of the `mdout` — see the verdict rule
in `scripts/wrapper.py` (`detect_failure`). Grounded in real captured pmemd
output:

- **`NaN`/`Infinity` are sticky.** Once an energy goes non-finite it propagates to
  every later step, so a run that printed a termination banner *and* a `NaN` or
  `Infinity` is numerical garbage — caught even though its exit code is 0 (the
  silent-pass class). gfortran prints true IEEE infinity as the literal token
  `Infinity` in a formatted field (it does NOT degrade to `******`), so both must
  be matched — an adversarial review caught the `Infinity` half being missed.
- **Overflow `******` and finite `vlimit` clamps are NOT fatal alone.** A
  successful stabilization of a strained geometry shows a transient early
  `******` and finite `vlimit exceeded` clamps, then settles. The verdict keys on
  the *final* energy block + the termination banner + `NaN`/SHAKE, not on any
  asterisk anywhere.

## Bounds are reused, never invented

`scripts/check_amber_vendored.py` is a copy of the project's `md-param-check`
validator. Every Tier-2 namelist is written and then run through it; a namelist
that FAILs a hard physical-realism bound is refused. SHAKE-off ⇒ `dt ≤ 1 fs`
(the validator's no-SHAKE cap), so the ladder is `0.001 → 0.0005 → …` floored at
`--dt-floor`. The mutation engine cannot emit a physically-impossible namelist —
the guarantee is deterministic, not a prompt instruction.

## The LOWE strict-verifier loop

Tier 2 is `Predict → Test → Falsify → Improve`: hypothesize a bounded fix
(stabilize at this `dt`) → run it → the **restore** at sane parameters is the
falsification test → if it re-crashes, lower `dt` and retry, else done. The
acceptance run shows this working: `dt=0.001` stabilization was *falsified*
(still unstable), the loop dropped to `dt=0.0005`, which stabilized, and the
sane-parameter restore then completed.

## Scope (v1) and named-not-built (v2)

- **v1:** Tier-2 mutations are `dt` + SHAKE only (the spec's example). Recovery
  restarts the crashed *stage* from the stabilized checkpoint — appropriate for
  equilibration-stage crashes.
- **v2 (named, not built):** mid-production *continuation* (vs stage restart);
  broader Tier-2 mutations (re-minimize, shrink `cut`, reseed velocities) —
  each still bounds-gated; remote/cluster crash-log gathering
  (`Infra_DPDispatcher` / `Gap_Remote_HPC_Backend`); an optional named recovery
  agent (still within the max-3-agent budget).

## Sources

- arXiv:2603.25522 — bounded recovery from runtime failures (methane-oxidation
  case study). NotebookLM-verified 2026-05-19.
- Vault: `Skill_Bounded_Recovery_AMBER`, `Workflow_Error_Recovery_Loop`,
  `Arch_Recursion_LOWE`, `Design_Determinism_Spectrum`.

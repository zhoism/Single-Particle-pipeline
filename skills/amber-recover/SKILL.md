---
name: amber-recover
description: "Detect a real pmemd MD runtime crash from its mdout and salvage the run via a TIERED, mathematically-bounded recovery loop — Tier 1 restores the last good checkpoint and resumes as-is (transient/killed failures), Tier 2 escalates to a bounded dt-lower + SHAKE-off stabilization window then restores normal parameters (numerical instability), and HALTs with a structured human-request when the dt floor or retry budget is hit. Detection is deterministic regex/numeric parsing of the mdout; the LLM never diagnoses or invents physics. Every mutated namelist is gated by the same check_amber bounds validator the rest of the pipeline uses. System-agnostic."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Wraps the amber-md-run pmemd chain (reads each stage's .out + .rst restart chain). Requires an AMBER MD engine (pmemd from ~/Downloads/pmemd26, or sander). macOS = CPU only.
metadata: {"openclaw":{"requires":{"bins":["pmemd"]},"os":["darwin"]},"inputs":{"md-dir":"amber-md-run working dir (namelists + comp_oct.top + restart chain + crashed stage .out)","stage":"crashed stage (auto-detected from last .out if omitted)","checkpoint":"last-good restart/coords (inferred from the amber-md-run chain if omitted)","dt-floor":"ps floor for Tier-2 dt (default 0.0005 = 0.5 fs)","stabilize-steps":"SHAKE-off window length (default 2000)","max-tier2-attempts":"dt-halving budget (default 4)","engine":"pmemd|pmemd.MPI|sander","detect-only":"classify only, no recovery","dry-run":"detect + plan + bounds-check, no pmemd"},"outputs":{"recovered":"bool","tier":"1|2|null","final_rst":"recovered restart","attempts":"per-tier ladder log","needs_human":"structured HALT block"},"validation":["detection_deterministic","every_mutation_check_amber_clean","tier1_before_tier2","bounded_halt"],"dry_run":true,"detect_only":true,"source":"project-prime/skills/amber-recover","stage":"Phase3.Stage8"}
---

# amber-recover

## Goal

When a pmemd MD stage crashes mid-run, salvage it **without a human babysitting
the `mdout`** and **without letting an LLM invent physics**. The wrapper detects
the failure deterministically and applies a tiered, mathematically-bounded
recovery; the LLM only picks this skill. This is the vault's strongest
paper-cited element (arXiv:2603.25522 bounded runtime recovery; see
`references/recovery-loop.md`).

## When to use

- An `amber-md-run` (or compatible) stage produced a crashed `mdout`: `NaN`
  energies, coordinate overflow (`******`), `vlimit exceeded`, a SHAKE
  convergence failure (`Coordinate resetting cannot be accomplished`), a
  temperature blow-up, or no normal-termination banner.
- Also catches the **silent-garbage** case: a run that printed a termination
  banner with exit 0 but whose body is full of `NaN` (rc + banner alone pass it).
- `--detect-only` to just diagnose; `--dry-run` to see the recovery plan (and the
  bounds-checked Tier-2 namelist) without running pmemd.

## The tiered loop

| Tier | Trigger | Action | Physics risk |
|------|---------|--------|--------------|
| **1** | always first | restore last-good checkpoint (`.rst`), resume the stage **as-is** | none — changes no parameters |
| **2** | only if Tier 1 re-crashes | bounded **stabilization** (dt lowered down a ladder, **SHAKE off**) from the checkpoint; once it survives `--stabilize-steps`, **restore** the original sane parameters and resume | bounded — every mutated namelist must pass `check_amber` |
| **HALT** | dt floor / retry budget / bound forbids the only fix | structured `ok:false` with a `needs_human` block (what was tried + recommendation) | none — refusing to breach a bound IS the guarantee |

Mutation is the **escalation, never the first move** — the defensible answer to
"you're letting an AI change physics."

## Detection is deterministic

Regex + numeric parse of the `mdout` (+ stderr / `run.log`), never agentic. A run
is **crashed** iff: no termination banner, OR non-zero rc, OR `NaN`/`Infinity`
present (IEEE non-finite is sticky → propagates → garbage; gfortran prints true
`inf` as the literal `Infinity`, not `******`), OR a SHAKE convergence failure,
OR the final energy block is non-finite, OR a temperature blow-up. A **finite** `vlimit`
clamp and a transient early `******` that the run recovers from are tolerated
(a successful stabilization of a strained geometry legitimately shows both).

## Bounds are reused, not invented

Every Tier-2 namelist is written, then validated by `check_amber_vendored.py`
(the same limits as the project's `md-param-check`): `dt ≤ 2 fs` with SHAKE / `≤ 1
fs` without, `8 ≤ cut ≤ 12`, Langevin `gamma_ln ∈ [1,5]`. SHAKE-off therefore
forces `dt ≤ 1 fs`; the `--dt-floor` (default 0.5 fs) caps how low the ladder
goes. A namelist that would FAIL the validator is refused → HALT.

## Inputs

| Key | Default | Description |
|-----|---------|-------------|
| `--md-dir` | required | The crashed `amber-md-run` working dir. |
| `--stage` | auto | Crashed stage; auto = last stage with an `.out`. |
| `--checkpoint` | inferred | Last-good `.rst`/coords; inferred from the chain (`heat←min3.rst`, …). |
| `--dt-floor` | 0.0005 | ps floor (0.5 fs). A fix needing less HALTs. |
| `--stabilize-steps` | 2000 | SHAKE-off window the system must survive. |
| `--max-tier2-attempts` | 4 | dt-halving budget before HALT. |
| `--engine` / `--engine-home` | `pmemd` / `~/Downloads/pmemd26` | Engine seam. |
| `--detect-only` / `--dry-run` | — | Diagnose / plan without recovering. |

## Outputs

Single JSON envelope. `outputs.{recovered,tier,final_rst,stabilize_dt,attempts}`;
`validation.detection.{crashed,classification,signatures,crash_nstep}`;
`validation.bounds.{checked,all_pass}`; on HALT, `outputs.needs_human`.

## Acceptance test

`bash test_acceptance.sh` (real pmemd on 1L2Y):
1. **Tier-2 golden** — sane 2 fs/SHAKE stage crashes on an un-minimized geometry;
   Tier 1 re-crashes → Tier 2 stabilize+restore → normal termination (`tier=2`).
2. **Tier-1 golden** — a killed stage over a good checkpoint resumes as-is (`tier=1`).
3. **HALT** — `--dt-floor 0.002` forbids the only fix → `ok:false needs_human`.
4. **Malformed** — missing `--md-dir` → graceful `ok:false`.
5. **No-failure** — a clean finished run → `ok:false NO_FAILURE_DETECTED`.
6. **Dry-run** — plan + bounds-check the mutation, no pmemd.

`python3 tests/test_detector.py` — independent detector oracle over real mdout
fixtures + fault injection (py3.9 + py3.11).

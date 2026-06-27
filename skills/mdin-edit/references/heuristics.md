# mdin-edit — design heuristics

This skill is **original** to Project Prime (not adapted from the upstream
`computational-chemistry-agent-skills` library — that library has ligand-prep / MD-run
skills, not a parameter editor). The one thing reused is the **validation logic**, vendored
from the in-repo `md-param-check` Claude Code skill.

## Why a hardened editor at all

Hand-editing mdin files, or letting an LLM rewrite them, is exactly how silent science bugs
get in (cf. `[[antechamber-aromatic-kekulize-bug]]`: a tool failed quietly and every gate
passed). A parameter edit looks trivial — change one number — but the failure modes are
subtle: clobbering a neighbouring value on a packed line, appending a duplicate key,
breaking the `temp0`/`&wt` agreement, or producing a different file each run. So the edit is
a deterministic Python engine with bounds, stage-awareness, a self-check, and a change log.
The LLM only chooses *what* to edit; it never writes the bytes.

## Reused validation (provenance)

`scripts/check_amber_vendored.py` is a verbatim copy (2026-06-08) of
`.claude/skills/md-param-check/checks/check_amber.py` (vault repo `Single-Particle`, `main`).
It is **vendored, not imported**: an OpenClaw skill under `project-prime/` must be
self-contained and cannot reach into the vault's `.claude/` tree (separate repo, may move).
Re-sync and bump the provenance date if the upstream bounds change. The wrapper uses it for
(a) the post-edit **self-check parse** (an independent parser confirming the rendered value
landed) and (b) **advisory** post-edit validation (e.g. confirming heat-3's `temp0`/`&wt value2` stay coherent).

## Heuristics

### Idempotency comes from pure value rendering
`render_value(param, value)` depends only on the parameter class and the numeric value — never
on the token currently in the file. Float params render integral inputs as `N.0` (`310` →
`310.0`); `nstlim` renders as a bare integer. Because the function is pure, running the same
edit again produces the same target string, the engine detects `old == new`, and the file is
left byte-identical. Pinning float-ness to the *param* (not the existing token) is the subtle
point: it stays stable even if a value crosses an integer boundary.

### Edit the smallest possible span
The replace regex captures only the numeric token (`(?P<val>…)`) and replaces it by index
slicing — never `re.sub` (no backreference surprises), never a line-greedy match. A left word
boundary `(?<![\w])` prevents `restraint_wt` from matching inside `restraintmask` and `step2`
inside `istep2`. On the packed `&wt` line `value1 = 5.0, value2 = 100.0,`, asking for `value2`
leaves `value1`, every comma, and any comment untouched.

### Scope to the namelist, select the right `&wt`
Edits are scoped to the `&cntrl` block (start → the line-anchored closing `/`). The `temp0`
coupling targets specifically the `&wt` block whose body is `type='TEMP0'` — not the
`type='END'` terminator — so `value2` is written in the ramp block only.

### Couple `temp0` to whatever the stage's temperature model needs
Two mutually-exclusive couplings, both keyed off the file:
- **Heating stage** (`nmropt=1` **and** a TEMP0 `&wt` block): also set the ramp end `value2`.
  `value1` (the ramp start) and `tempi` (also a ramp start) are never touched.
- **Constant-T stage** (`tempi` present, **no** TEMP0 ramp, `nmropt≠1` → relax/prod): also set
  `tempi` so `tempi == temp0` (no thermal transient at the start of a constant-T run).
Pressurization stages (`temp0`, no `tempi`, no ramp) get neither — just the plain `temp0`
edit. The `nmropt≠1` guard on the constant-T branch stops a half-disassembled heat stage
(ramp removed but `tempi` still a ramp start) from being mistaken for a constant-T stage.

### `dt` cap follows SHAKE; flag a hot `dt`
The `dt` hard cap is read from the stage: `≤0.002` ps with SHAKE on (`ntc=2, ntf=2`), `≤0.001`
ps without (the global ceiling stays `0.002` — we don't do HMR). Above 300 K a `dt` at the cap
gets a non-blocking advisory (hotter atoms travel further per step). The cap only tightens
where `dt` is actually present, so a `dt` edit on a min stage still falls through to
`PARAM_NOT_FOUND`, not `OUT_OF_BOUNDS`.

### `nstlim` carries its output schedule
After an `nstlim` edit the result reports `trajectory_frames = nstlim // ntwx` and
`energy_outputs = nstlim // ntpr`, warning on zero / very-sparse / non-multiple sampling.
`ntwx=0` means the trajectory is intentionally off (heat/press) — so it never warns about
frames there. The advisor's "present before applying" maps onto `--dry-run` (the skill is
non-interactive; dry-run is its review-before-commit step).

### Restraint transitions are a quarantined transaction
Enabling restraints is multi-key (`ntr`, `restraint_wt`, `restraintmask`) and may need to
**insert** a `restraintmask` line where a stage has none — the single deliberate exception to
"never append", confined to `--enable-restraints` and gated by a line-count self-check. The
mask is the only non-numeric value the skill writes: validated shallowly (non-empty, ≤256,
no `"`/`'`/`/`/newline — bytes that corrupt the namelist line or terminator) and **read back
through a quoted-value regex, NOT the vendored parser** (whose `! comment` strip would eat a
mask that starts with `!`). Disabling is just `ntr=0`; the now-inert `restraint_wt`/mask are
left in place (AMBER ignores them).

### Cut floor: accept the advisor's 7.0, but flag it
The project validator FAILs `cut < 8` Å for explicit solvent. The advisor's task explicitly
sets `cut = 7.0`. Resolution (decided with the user): the editor's HARD bound is `6 ≤ cut ≤ 12`,
so 7.0 is accepted, but `6 ≤ cut < 8` raises an advisory WARN, and the vendored FAIL on a value
the user deliberately set within editor bounds is downgraded to a WARN (the editor never rejects
its own intended edit). The shared validator is left unchanged. Physically: under PME, `cut`
only bounds the direct-space sum; long-range electrostatics are exact via reciprocal space, so a
7 Å direct cutoff is aggressive but defensible.

### Applicability keys off `ntr`, not line presence
`restraint_wt` is present in *all* stages (0.0 where restraints are off). Editing it where
`ntr=0` is a physical no-op, so the engine refuses it (single-stage → error) or skips it
(group → `skipped`, batch stays ok). The general rule: "not applicable → skip in a group, fail
in a single-stage request"; "applicable but rejected/errored → fail the whole batch and write
nothing."

### All-or-nothing, atomic, self-checked
All edits are computed in memory; if any file hard-errors, nothing is written. Each changed
file is written atomically (`.tmp` + `os.replace`). After computing each edit, the result is
re-parsed and the value asserted (`SELF_CHECK_FAILED` aborts before any write) — the backstop
that catches a wrong-span regex bug the unit tests might miss.

## Anti-heuristics (don't do this)

- **Don't append.** A missing parameter is a `PARAM_NOT_FOUND` refusal, never a new line — an
  appended `dt` in a minimization namelist would be silently ignored by sander *or* change
  behavior unexpectedly.
- **Don't edit the advisor's originals.** Always operate on a copy (`--md-dir` points at one);
  the acceptance test copies per case.
- **Don't let validator findings gate `ok`.** They are advisory; the editor's own bounds +
  self-check are authoritative. Otherwise the editor would reject its own deliberate `cut=7.0`.
- **Don't use a line-greedy regex** on the packed `&wt` lines — it clobbers the sibling value.
- **Don't render from the existing token** — it breaks idempotency across a value-type change.

## Recurring failure modes

| Failure | Symptom | Root cause | Guard |
|---|---|---|---|
| Sibling value clobbered | `value1` changes when editing `value2` | line-greedy / wrong span | numeric-token-only capture + index slice (case 5) |
| Prefix collision | `restraintmask` disturbed editing `restraint_wt` | no word boundary | `(?<![\w])` lookbehind (case 7) |
| Non-idempotent | second run changes bytes | rendering from current token | pure `render_value` (case 2) |
| Silent append | `dt` added to a min stage | append-on-missing | `PARAM_NOT_FOUND` refusal (case 4) |
| Half-write | file mutated after a rejected edit | write-before-validate | in-memory plan + atomic write (cases 3/0) |
| temp0/&wt drift | ramp end ≠ `temp0` | uncoupled edit | `&wt value2` coupling (case 5) |

## Cross-links
- `references/mdin-params.md` — the §23.6 per-stage write-up (Task 1).
- `[[antechamber-aromatic-kekulize-bug]]` — the verify-thoroughly lesson this skill embodies.
- `[[phase3-advisor-demo]]` — the demo set + the hardcoded-AMBERHOME note.

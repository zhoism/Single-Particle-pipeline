---
name: mdin-edit
description: "Edit one parameter in one stage (or a stage-group) of a pre-prepared AMBER mdin set. The agent maps natural language ('set the timestep to 0.001 in the first heating stage') to structured args; this wrapper does an idempotent, byte-minimal parse-replace (never appends), bounds-checked and stage-aware, with &wt temp0-ramp consistency, a post-edit self-check, and a change log. Operates on a COPY; the LLM never edits files."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Pure Python 3 (stdlib only). No AMBER binaries required to edit; AmberTools/pmemd only needed to later RUN the edited files.
metadata: {"openclaw":{"requires":{"env":[]},"os":["darwin"]},"requires":{"bins":[],"env":[]},"inputs":{"md_dir":"path — a COPY of the advisor's mdin set","stage":"stage name | group:third-onward | group:all (required unless --submit)","param":"dt|cut|temp0|restraint_wt|nstlim (required unless --submit)","value":"number (required unless --submit)","dry_run":"flag","submit":"flag — run the edited set locally (min1..prod pmemd chain)","reduce_nstlim":"int, default 120 — nstlim for the --submit smoke","keep":"flag — keep the --submit scratch dir","enable_restraints":"flag — ntr=1 + restraint_wt + restraintmask (inserts mask line if absent)","disable_restraints":"flag — ntr=0","restraint_wt":"number — force constant for --enable-restraints","restraintmask":"string — atom mask for --enable-restraints"},"outputs":{"files":"per-stage edit records (old→new per namelist.param)","log":"<md_dir>/mdin-edit.log","submit":"per-stage rc + normal_termination + final_rst7"},"validation":["idempotent_parse_replace_never_append","bounds_dt_cut_temp0_restraint_wt_nstlim","dt_shake_aware_cap","stage_aware_file_targeting","wt_temp0_value2_coupling","temp0_tempi_constant_T_coupling","nstlim_output_schedule","restraint_enable_disable_transitions","post_edit_self_check_reparse","submit_amberhome_rewrite_foreign_path_clean"],"dry_run":true,"source":"project-prime/skills/mdin-edit","stage":"Phase3.ParamEditor"}
---

# mdin-edit

## Goal

Take a pre-prepared AMBER molecular-dynamics input set (the advisor's `min1 … prod`
mdin chain) and change **one parameter** in **one stage** — or one parameter across a
named **group** of stages — *correctly and predictably, no matter how many times it is
run*. The agent turns a natural-language request ("relax the positional restraints to
1.0 in the second pressurization stage") into structured arguments; this wrapper does
the deterministic edit: it finds the parameter inside the right namelist, checks it
against physical bounds, rewrites only the numeric token (preserving every comma,
comment, and space), keeps the heating-stage `&wt` temperature ramp consistent,
re-parses to confirm the result, and appends a change-log line. The LLM never touches
the file — per the project's "lobster-like" discipline, all chemistry-critical mutation
is deterministic Python.

This is the *editor* counterpart to `amber-md-run` (which *generates* its own namelists).
`mdin-edit` does not generate anything — it surgically modifies files the advisor already
prepared.

## When to use

- The advisor's parameter-editing task: change `dt`, `cut`, `temp0`, `restraint_wt`, or
  `nstlim` in a named stage of `phase3-explicit-solvent-md/`, in natural language, then
  record the change.
- Tuning an existing mdin set (e.g. lengthen production, lower the cutoff, ramp to a new
  target temperature) without hand-editing files and risking a silent typo.
- Keeping `temp0` and the `&wt` ramp end in lockstep on heat stages — editing `temp0`
  couples the ramp end automatically, and a *pre-existing* mismatch is surfaced for a
  human decision rather than silently overwritten (see the coherence gate below).

## Inputs

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `--md-dir` | path | yes | Directory holding the mdin files. **Pass a COPY** of the advisor's set — the skill is non-destructive but the copy-first discipline is yours. |
| `--stage` | string | yes (not `--submit`) | A stage (`min1`, `min2`, `heat-1…3`, `press-1…3`, `relax`, `prod`) **or** a group: `group:third-onward` = {heat-3, press-3, relax, prod}; `group:all` = all 10. |
| `--param` | string | edit mode | One of `dt`, `cut`, `temp0`, `restraint_wt`, `nstlim`. |
| `--value` | number | edit mode | The new value. Rendered canonically (`310` → `310.0` for float params; integer for `nstlim`). |
| `--enable-restraints` | flag | no | Turn positional restraints **on** in `--stage`: sets `ntr=1`, `restraint_wt=<--restraint-wt>`, `restraintmask=<--restraintmask>` (inserts the mask line where the stage has none). Requires `--restraint-wt` and `--restraintmask`. |
| `--disable-restraints` | flag | no | Turn positional restraints **off** in `--stage`: sets `ntr=0` (leaves the now-inert `restraint_wt`/`restraintmask`). |
| `--restraint-wt` | number | with enable | Force constant (kcal/mol·Å²) for `--enable-restraints`. |
| `--restraintmask` | string | with enable | Atom-mask string for `--enable-restraints` (e.g. `'!:WAT,Cl-,K+,Na+ & !@H='`). No `"`, `'`, `/`, or newline. |
| `--couple` | flag | no | Only with `--param temp0`. On a heat stage whose `&wt value2` was **already** incoherent with `temp0`, set `value2` to the new `temp0` (cohere it). Mutually exclusive with `--keep-value2`. |
| `--keep-value2` | flag | no | Only with `--param temp0`. On a pre-incoherent heat stage, edit `temp0` only and leave `value2` untouched (preserve the deliberate mismatch; a non-blocking WARN records it). |
| `--dry-run` | flag | no | Plan + validate the edit and print the would-be result (incl. the `nstlim` output schedule); write nothing, log nothing. With `--submit`: plan the run (rewrite + reduce) but invoke no pmemd. |
| `--submit` | flag | no | Run the **already-edited** set locally to prove it runs (separate mode; `--stage/--param/--value` not needed). See **Submit** below. |
| `--reduce-nstlim` | int | no | `nstlim` for the `--submit` smoke (default `120`; `≥100` so the MC barostat is happy on the NPT stages). |
| `--keep` | flag | no | Keep the `--submit` scratch dir instead of deleting it (scratch is also kept automatically on a stage failure). |

## Outputs

Single JSON envelope on stdout; stderr is human-readable per-file progress.

```json
{
  "ok": true,
  "skill": "mdin-edit",
  "dry_run": false,
  "outputs": {
    "md_dir": "/abs/phase3-work",
    "stage": "group:third-onward",
    "param": "temp0",
    "value": "310.0",
    "files": [
      {"file": "heat-3.in", "status": "edited",
       "edits": [
         {"namelist": "cntrl", "param": "temp0",  "old": "300.0", "new": "310.0", "changed": true},
         {"namelist": "wt",    "param": "value2", "old": "300.0", "new": "310.0", "changed": true}
       ]},
      {"file": "press-3.in", "status": "edited",
       "edits": [{"namelist": "cntrl", "param": "temp0", "old": "300.0", "new": "310.0", "changed": true}]},
      {"file": "relax.in", "status": "edited", "edits": [/* temp0 only — no &wt */]},
      {"file": "prod.in",  "status": "edited", "edits": [/* temp0 only */]}
    ],
    "log": "/abs/phase3-work/mdin-edit.log"
  },
  "validation": {
    "per_file": {
      "heat-3.in": {"verdict": "WARN", "findings": [{"level": "PASS", "rule": "temp0 / &wt coherent", "detail": "...", "deliberate": false}]}
    }
  },
  "errors": []
}
```

Per-file `status` is one of `edited` (a value changed), `unchanged` (already at the target
— idempotent no-op), `skipped` (not applicable here — e.g. `restraint_wt` where `ntr=0`,
in a group edit), or `error` (a hard failure; see Errors). A single hard `error` makes the
whole batch `ok:false` and **writes nothing** (all-or-nothing).

## Validation gates

- **Idempotent parse-replace, never append.** Only the numeric token is rewritten; comma,
  inline comment, indentation and `=` spacing are preserved. Re-running the same edit yields
  a byte-identical file (`status:"unchanged"`).
- **Bounds (hard, reject = `OUT_OF_BOUNDS`):** `0 < temp0 ≤ 400` K, `restraint_wt ≥ 0`,
  `nstlim > 0` (integer), `6 ≤ cut ≤ 12` Å. **`dt` is SHAKE-aware:** `≤ 0.002` ps with SHAKE
  on (`ntc=2, ntf=2`), `≤ 0.001` ps without — read from the stage. Advisory **WARN** for
  `6 ≤ cut < 8` Å (below the validator's explicit-solvent floor, accepted deliberately — PME
  reciprocal space covers long-range electrostatics), and a **hot-`dt` advisory** when a stage
  ends up `temp0 > 300` K with `dt` at the cap (higher T → larger velocities; consider reducing `dt`).
- **Stage-aware targeting.** `--stage`/group resolves to the right file(s); a parameter that
  isn't present in a stage (`dt`/`temp0` in `min1/min2`) is refused, not appended.
- **Temperature coupling (`temp0`).** In a **heating** stage (`nmropt=1` + `&wt TEMP0` ramp),
  editing `temp0` also sets the ramp end `value2` (the `value1` ramp start is never touched).
  In a **constant-T** stage (relax/prod: `tempi` present, no ramp), editing `temp0` also sets
  `tempi` so `tempi == temp0`. Pressurization stages (no `tempi`, no ramp) get just `temp0`.
- **Coherence gate (heat `value2`).** Coupling silently rewrites `value2` *only when it was
  already coherent* with `temp0` (within 0.5 K, the validator's threshold). If `value2` was
  **already incoherent** (a deliberate pre-existing mismatch), the skill refuses to silently
  clobber it: the stage returns `status: needs_human`, the whole batch halts `ok:false` with
  `EDIT_HALTED: HEAT_TEMP0_INCOHERENT` and `outputs.needs_human` (writing nothing), and the
  human re-runs with `--couple` (cohere `value2`) or `--keep-value2` (edit `temp0` only). This
  mirrors `amber-recover`'s `needs_human` halt — the decision belongs to a human, not a silent
  default. (Constant-T `tempi` coupling is **not** gated — it stays silent.)
- **`nstlim` output schedule.** After an `nstlim` edit the envelope carries an
  `output_schedule` (`ntwx → trajectory_frames`, `ntpr → energy_outputs`) plus advisory
  warnings on zero / very-sparse / non-multiple sampling (`ntwx=0` = trajectory off, no warn).
  Use `--dry-run` to review the schedule before committing.
- **Restraint transitions.** `--enable-restraints` (ntr=1 + `restraint_wt` + `restraintmask`,
  inserting the mask line where absent) and `--disable-restraints` (ntr=0) — the insert is the
  only line the skill ever adds, quarantined to enable mode and line-count self-checked.
- **Post-edit self-check.** The result is re-parsed with an independent parser and the new
  value asserted; a mismatch is a hard `SELF_CHECK_FAILED` and **nothing is written**.
- **Advisory validation.** The vendored `check_amber` logic runs over the result for
  transparency (e.g. confirming heat-3's `temp0`/`&wt value2` stay coherent after an edit). Its findings never gate `ok`;
  a FAIL on the exact value you deliberately set within bounds is downgraded to a WARN.

## Errors

| Code | Cause | Recovery |
|------|-------|----------|
| `UNSUPPORTED_PARAM` | `--param` not in {dt, cut, temp0, restraint_wt, nstlim}. | Use a supported parameter (extend `HARD_BOUNDS` to add more). |
| `OUT_OF_BOUNDS` | Value violates the physical bound (incl. the SHAKE-aware `dt` cap). | Choose a value within bounds; see Validation gates. |
| `MODE_CONFLICT` | More than one of `--param` / `--enable-restraints` / `--disable-restraints` / `--submit`. | Use exactly one operation per call. |
| `INVALID_MASK` | `--restraintmask` empty, >256 chars, or contains `"` / `'` / `/` / newline. | Provide a plain AMBER atom mask (those bytes would corrupt the namelist). |
| `NONINTEGER_VALUE` | `nstlim` given a non-integer. | Pass an integer number of steps. |
| `UNKNOWN_STAGE` | `--stage` not a stage or group. | Use a listed stage or `group:third-onward`/`group:all`. |
| `MD_DIR_NOT_FOUND` | `--md-dir` is not a directory. | Point at a copy of the mdin set. |
| `STAGE_FILE_MISSING` | The stage's `.in` file isn't in `--md-dir`. | Copy the full set. |
| `PARAM_NOT_FOUND` | Param absent in that stage's `&cntrl` (e.g. `dt` in `min1`). | Single-stage → error; in a group it's a `skipped`, not a failure. Never appended. |
| `SKIPPED_RESTRAINTS_OFF` | `restraint_wt` where `ntr≠1` (min2/relax/prod). | Restraints are off there — editing has no effect. Single-stage → error; group → `skipped`. |
| `AMBIGUOUS_PARAM` | The param appears more than once in the target namelist. | Manual inspection — the engine refuses to guess. |
| `NAMELIST_NOT_FOUND` | No `&cntrl` block / unterminated namelist. | Fix the file structure. |
| `SELF_CHECK_FAILED` | Post-edit re-parse didn't read the rendered value. | Internal backstop — file untouched; report it (regex/format edge case). |
| `EDIT_HALTED: HEAT_TEMP0_INCOHERENT` | A `temp0` edit hit a heat stage whose `&wt value2` was **already** incoherent with `temp0`. Whole batch halts, nothing written; `outputs.needs_human` carries the per-stage `old_temp0`/`old_value2`/`new_temp0`. | Re-run with `--couple` (set `value2` to the new `temp0`) or `--keep-value2` (edit `temp0` only). |
| `FLAG_CONFLICT` | Both `--couple` and `--keep-value2` given. | Pass at most one. |
| `FLAG_NOT_APPLICABLE` | `--couple`/`--keep-value2` used without `--param temp0`. | Those flags apply only to a `temp0` edit. |

## How it works

Invocation (the agent makes ONE `exec` call):

```
python3 {baseDir}/scripts/wrapper.py --md-dir <copy> --stage <stage|group:...> --param <p> --value <v> [--dry-run]
```

1. **Validate** the param + value (bounds) before touching any file — fail fast, write nothing.
2. **Render** the value canonically (pure function of param+value → idempotency).
3. **Resolve** `--stage` to file(s) via an explicit map (groups included).
4. **Plan each file's edit in memory** (no writes yet): scope to the `&cntrl` block, replace
   only the numeric token by index-slice; for `temp0` in an `nmropt=1` stage, also set the
   `&wt TEMP0` `value2`; then **self-check** by re-parsing with the independent vendored parser.
5. **Commit all-or-nothing:** if any file hard-errors, write nothing. Otherwise write each
   changed file atomically (`.tmp` + `os.replace`) and append `{ts, file, namelist.param,
   old → new}` to `mdin-edit.log`.

`--dry-run` performs 1–4 (including the self-check) and prints the plan, but never writes or
logs.

## Submit — prove the edited set runs locally

```
python3 {baseDir}/scripts/wrapper.py --md-dir <COPY> --submit [--reduce-nstlim N] [--keep] [--dry-run]
```

`--submit` answers "does the set I just edited actually run?". It is a *separate* mode from the
editor (no `--stage/--param/--value`), and it **never mutates `--md-dir`** — it works on its own
scratch copy. On that scratch it:

1. **Rewrites** the advisor's hardcoded `export AMBERHOME=…` in `submit.sh` to `source` the local
   toolchain (`scripts/env.sh`), then asserts the result is foreign-path-clean (vendored detector).
2. **Reduces** `nstlim` to `--reduce-nstlim` (default 120) on all stages via this same engine
   (subprocess-to-self → the exact tested edit path), and trims the out-of-scope min/heat lengths
   (`maxcyc`, `&wt istep2`) so the smoke finishes in minutes — *those trims are smoke-only, not part
   of the edit contract*.
3. **Runs** the advisor's `min1 … prod` pmemd chain restart-chained (each stage `-c` the prior
   `.rst7`), asserting per stage: `rc==0`, no "Terminated Abnormally", non-empty `.rst7`.

The envelope reports `outputs.mode:"submit"`, a per-stage list (`rc`, `normal_termination`,
`rst7_bytes`), and `final_rst7`; `ok` is true only if all ten stages reach normal termination.
`--submit --dry-run` does steps 1–2 and reports the plan without invoking pmemd (no toolchain
needed — good for CI). This productizes `tests/smoke_edit_run.sh`.

## Restraint transitions (advisor feedback 2026-06-22)

Turning positional restraints on/off is a multi-key transaction, so it has its own modes
(mutually exclusive with `--param`/`--submit`):

```
# Enable: ntr=1 + restraint_wt + restraintmask (inserts the mask line if the stage has none)
python3 scripts/wrapper.py --md-dir <copy> --stage relax \
    --enable-restraints --restraint-wt 2.5 --restraintmask '!:WAT,Cl-,K+,Na+ & !@H='

# Disable: just ntr=0 (the now-inert restraint_wt/restraintmask are left in place)
python3 scripts/wrapper.py --md-dir <copy> --stage press-1 --disable-restraints
```

Enabling is idempotent (a no-op re-run is byte-identical), all-or-nothing, atomic, and
self-checked (ntr/restraint_wt re-parsed; the mask read back through a quoted-value reader
because the vendored parser's comment-strip would eat a mask starting with `!`). The mask line
insertion is the only line the skill ever adds and is gated by a line-count self-check.

## Reviewing an `nstlim` change before committing

`nstlim` edits return an `output_schedule` and sampling warnings. Run `--dry-run` first to see
the resulting trajectory/energy frame counts *without writing*, then re-run without `--dry-run`
to commit — this is the skill's "present and confirm before applying" path (it is
non-interactive / all-or-nothing, so `--dry-run` is the review step).

## Guarantees — how mistakes are avoided (Task 4 summary)

The whole point of this skill is to *never make a silent mistake* (the
`antechamber-aromatic-kekulize-bug` lesson). Concretely:

- **Predictable / idempotent:** value rendering depends only on `(param, value)`, not the
  current token, so running an edit any number of times converges to one byte-identical
  result. Acceptance case 2 proves byte-identity on re-run.
- **Byte-minimal:** only the numeric token is rewritten — a left word-boundary stops
  `restraint_wt` vs `restraintmask` collisions, and matching only the number leaves the
  sibling `value1`, the comma, and comments untouched (acceptance cases 5/7).
- **Stage-aware:** a parameter that doesn't belong in a stage is refused, never appended
  (case 4); restraint edits where `ntr=0` are refused/skipped (case 7b/7c).
- **In-bounds:** out-of-range values are rejected before any write (case 3).
- **Consistent ramps:** the heat-3 `temp0`/`&wt value2` class of bug is fixed automatically
  by the coupling (case 5).
- **Self-checked + atomic:** the result is re-parsed and asserted before an atomic write, so
  a half-written or wrong-span edit cannot reach disk.
- **Recorded:** every applied change is logged with old → new.

## Acceptance test

`bash test_acceptance.sh` runs on FRESH copies of the demo set and asserts the actual edited
**bytes** (never just `ok:true`):

0. **Malformed** — unterminated namelist → graceful `ok:false` (`NAMELIST_NOT_FOUND`), file untouched.
1. **Golden** — `dt → 0.001` in heat-1 (advisor example #1); exactly one line changes.
2. **Idempotency** — re-run is byte-identical; re-run reports `unchanged`; trailing newline intact.
3. **Out-of-bounds** — `dt = 0.01` rejected; file unchanged (no half-write).
4. **Wrong-param** — `dt` on min1 rejected; `dt` still absent (no append).
5. **Ext-A** — `temp0 → 310` `group:third-onward`: heat-3 `value2` coupled to the new temp0
   (value1/ramp-start preserved → heat-3 stays coherent, validator confirms), relax/prod get no
   `&wt`, heat-1/2 + press-1 untouched. **5b** exercises the coupling rewrite (`temp0 → 305` →
   `value2 = 305.0`); **5c** is the hermetic, demo-independent twin (synthetic coherent fixture).
6. **Ext-B** — `cut → 7.0`: `ok:true` despite the validator's <8 Å FAIL, with a deliberate WARN.
7. **Ext-C** — `restraint_wt 5.0 → 1.0` on press-1 (mask line intact); **7b** negative on relax
   (ntr=0) rejected; **7c** `group:all` skips the ntr=0 files and edits the rest.

Run `bash test_acceptance.sh --dry-run` for the plan-only smoke (no toolchain needed).

`bash tests/submit_acceptance.sh` proves the `--submit` mode: it edits a fresh copy, runs
`--submit --reduce-nstlim 120` (10/10 stages to normal termination, `final_rst7` present), and
checks the `--submit --dry-run` plan (no toolchain). `tests/smoke_edit_run.sh` remains the
independent end-to-end run oracle.

## Deferred (follow-up)

- **Live NL drive** — exercising the agent's NL → `--stage/--param/--value` mapping via
  `openclaw agent` (verified by byte-comparing the agent's edit to the CLI baseline).

## References

`references/mdin-params.md` — the §23.6-grounded per-stage parameter write-up (advisor Task 1).
`references/heuristics.md` — design rationale (idempotency, bounds, `&wt` coupling, cut policy)
and the vendored-validator provenance.

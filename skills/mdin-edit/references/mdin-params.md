# The advisor's mdin set — a §23.6-grounded parameter map

> **Advisor Task 1 deliverable.** A per-stage reading of the demo mdin files
> (`phase3-explicit-solvent-md/`), grounded in **Amber26 §23.6 "General minimization
> and dynamics parameters"** (sander; Amber26.pdf ≈ p425+; atom masks ch25 ≈ p509;
> the `&wt` weight-change namelist is the `nmropt=1` companion documented with the NMR
> restraints/weight-change section). Values below were read **directly from the files**
> (verified 2026-06-08), not assumed.

## The chain

`submit.sh` runs the stages in this order, chaining restart coordinates with `-c <prev>.rst7`:

```
min1 → min2 → heat-1 → press-1 → heat-2 → press-2 → heat-3 → press-3 → relax → prod
```

Heat and press are **interleaved**: heat to a target temperature (NVT), then pressurize at
that temperature (NPT). Two minimizations precede the dynamics; `relax` is an unrestrained
NPT equilibration; `prod` is the production run.

## Per-stage parameter table (verified ground truth)

| Stage | File | imin | Ensemble | dt | cut | ntt/γ | ntb·ntp·barostat | temp0 (tempi) | ntr·restraint_wt | nmropt / &wt ramp |
|---|---|---|---|---|---|---|---|---|---|---|
| Min 1 | min1.in | 1 | minimization | — | 9.0 | — | — | — | **1** · 5.0 | — |
| Min 2 | min2.in | 1 | minimization | — | 9.0 | — | — | — | 0 · 0.0 | — |
| Heat 1 | heat-1.in | 0 | NVT (ntb1 ntp0) | 0.002 | 9.0 | 3 / 2.0 | 1·0·2 | 100 (5) | **1** · 5.0 | 1 / TEMP0 5→100 |
| Press 1 | press-1.in | 0 | NPT (ntb2 ntp1) | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 100 | **1** · 5.0 | — |
| Heat 2 | heat-2.in | 0 | NVT | 0.002 | 9.0 | 3 / 2.0 | 1·0·2 | 200 (100) | **1** · 5.0 | 1 / TEMP0 100→200 |
| Press 2 | press-2.in | 0 | NPT | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 200 | **1** · 5.0 | — |
| Heat 3 | heat-3.in | 0 | NVT | 0.002 | 9.0 | 3 / 2.0 | 1·0·2 | 300 (200) | **1** · 5.0 | 1 / TEMP0 200→300 |
| Press 3 | press-3.in | 0 | NPT | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 300 | **1** · 5.0 | — |
| Relax | relax.in | 0 | NPT | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 300 (300) | 0 · 0.0 | — |
| Prod | prod.in | 0 | NPT | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 300 (300) | 0 · 0.0 | — |

`nstlim`: heat/press = 50000 (0.1 ns), relax = 500000 (1 ns), prod = 5000000 (**10 ns**).
`maxcyc` (min1/min2) = 10000. `restraintmask` (where `ntr=1`) = `"!:WAT,Cl-,K+,Na+ & !@H="`
(everything that is **not** water/ions, heavy atoms only — i.e. restrain the solute heavy
atoms, let solvent and hydrogens move).

**Parameter presence (what `mdin-edit` must know for stage-aware targeting):**
- `dt`, `temp0`: the **8 MD stages only** (absent in min1/min2). `dt`'s cap is SHAKE-aware
  (0.002 with `ntc=2,ntf=2`, else 0.001).
- `cut`: all 10.
- `restraint_wt`: **present in all 10** — but `0.0` with `ntr=0` (restraints OFF) in
  min2/relax/prod, and `5.0` with `ntr=1` (ON) in min1/heat-1/2/3/press-1/2/3.
- `restraintmask`: present **only where `ntr=1`** (min1/heat/press). min2/relax/prod have NO
  mask line — so `--enable-restraints` there must *insert* one.
- `tempi`: heat-1/2/3 (ramp **start**, ≠ temp0) and relax/prod (constant-T, == temp0); absent
  in min1/min2 and the press stages. Editing `temp0` couples `tempi` on relax/prod, never on
  the heat stages.
- `ntwx`/`ntpr`: trajectory/energy output frequency — `ntwx=0` (trajectory off) in heat/press,
  `>0` in relax/prod. An `nstlim` edit reports the resulting frame counts and warns on
  zero/sparse/non-multiple sampling.
- `&wt` TEMP0 ramp: **heat-1/2/3 only**.

See [[Research_Advisor_Feedback_mdin_edit]] for the chemistry rationale behind the
SHAKE-aware `dt` cap, the `temp0`↔`tempi` coupling, the restraint transitions, and the
`nstlim` output-schedule safety net (advisor feedback 2026-06-22).

## What each parameter means (Amber26 §23.6)

- **`imin`** — 1 = minimization (steepest-descent/CG, `maxcyc`/`ncyc`); 0 = molecular dynamics.
  The minimizers have no `dt`/`temp0`/thermostat — which is why editing those there is invalid.
- **`dt`** — integration time step (ps). With SHAKE on bonds-to-hydrogen (`ntc=2, ntf=2`) the
  fastest motions are removed, so the standard cap is **2 fs (`dt = 0.002`)**; larger steps
  with SHAKE on risk instability/`vlimit` errors. Without SHAKE the cap drops to ~1 fs. The
  whole set uses `dt = 0.002` with SHAKE.
- **`cut`** — real-space non-bonded cutoff (Å). Under PME (the default for `ntb>0`) this only
  bounds the *direct*-space sum; long-range electrostatics are handled in reciprocal space.
  The set uses `cut = 9.0`. Typical explicit-solvent range is 8–12 Å; 7 Å is aggressive but
  used in some protocols (and is the advisor's edit example).
- **`ntb` / `ntp` / `barostat` / `pres0` / `taup`** — periodic boundary + pressure control.
  `ntb=1` constant volume (NVT, heating); `ntb=2, ntp=1` constant pressure (NPT,
  pressurization/relax/prod). `barostat=2` is the Monte-Carlo barostat; `pres0=1.0` bar,
  `taup=2.0` ps relaxation. (The heat stages carry `barostat`/`pres0` even with `ntp=0`; that
  is harmless but the validator notes it as confusing.)
- **`ntt` / `gamma_ln` / `temp0` / `tempi`** — temperature control. `ntt=3` is the Langevin
  thermostat with collision frequency `gamma_ln=2.0` ps⁻¹ (good ergodicity, robust during
  heating). `temp0` is the **target** temperature; `tempi` the initial. During heating the
  target is *ramped* by the `&wt` namelist (below), so `temp0` and the ramp end must agree.
- **`ntr` / `restraint_wt` / `restraintmask`** — Cartesian positional restraints. `ntr=1`
  turns them on; `restraint_wt` (kcal/mol·Å²) is the force constant; `restraintmask` selects
  the restrained atoms. The set restrains solute heavy atoms at 5.0 during min1 + all
  heat/press, then releases them (`ntr=0`) for relax/prod. **Editing `restraint_wt` where
  `ntr=0` has no effect** — `mdin-edit` refuses/skips it there.
- **`nmropt` + `&wt`** — `nmropt=1` enables the weight-change (`&wt`) namelist. The heat
  stages use a `&wt type='TEMP0'` block with `istep1/istep2` and `value1/value2` to **ramp the
  target temperature linearly** from `value1` to `value2` over those steps, followed by a
  `&wt type='END'` terminator. Because the *actual* target the thermostat follows is the
  ramped `&wt` value, **`&cntrl temp0` should equal the ramp end `value2`** — otherwise the
  two disagree about the final temperature.

## Why `mdin-edit` couples `temp0` ↔ `&wt value2` on heat stages

`heat-3.in` is coherent: `&cntrl temp0 = 300.0` and its `&wt TEMP0` ramp ends at
`value2 = 300.0` (ramp 200 → 300 K), matching heat-1 (100/100) and heat-2 (200/200).

> **History:** the advisor's original `heat-3.in` had `value2 = 310.0` — a 10 K
> disagreement with `temp0 = 300`. That was a **typo**, not an intended target (confirmed
> 2026-06-26 and corrected in the demo, vault `51e15c1`); do not treat the old 300/310 as
> ground truth.

Because the two *can* silently drift (a hand-edit of `temp0` that forgets the ramp end is
exactly how the 310 typo would arise), `mdin-edit` **couples** them: editing `temp0` in an
`nmropt=1` heating stage also rewrites the `&wt value2` to match, so `&cntrl` and the ramp
endpoint can never disagree after an edit. The ramp START (`value1`/`tempi`) is left alone.

**The coherence gate.** Coupling is silent only when `value2` was *already coherent* with
`temp0` (within 0.5 K). If you hand an `mdin-edit` a heat stage that is *already* incoherent
(e.g. the old 300/310), a `temp0` edit does **not** silently overwrite `value2` — it returns
`status: needs_human` and the batch halts (`EDIT_HALTED: HEAT_TEMP0_INCOHERENT`, nothing
written). Re-run with `--couple` (cohere `value2`) or `--keep-value2` (edit `temp0` only,
keep the mismatch).

*Scope (parser edges).* The gate's ramp-block finder (`temp0_wt_span`) matches the advisor's
form — a single-quoted `type = 'TEMP0'` block with a plain-decimal `value2`. Two non-advisor
Fortran spellings behave *differently*, and are **not** co-scoped with the vendored `check_amber`
validator (whose namelist parser strips *both* quote styles, making it **broader** than the gate):

- **Double-quoted `"TEMP0"`** — the gate's single-quoted finder does not match it, so a `temp0`
  edit is applied alone and a pre-existing `value2` mismatch is left un-coupled and un-halted
  *by the gate*. The validator **does** read it (quotes stripped) and still WARNs on the
  mismatch — so it is caught, just by the validator rather than the gate.
- **`d`-exponent `value2` (e.g. `3.05d2`)** — the value regex captures only the decimal prefix
  (`3.05`), so it does **not** fall through ungated: the truncated `3.05` reads as a phantom
  mismatch against `temp0` and **trips** the coherence halt (`needs_human`, default). With
  `--couple` it is rewritten to a corrupt `305.0d2` (= 30 500 K) shipped `ok:true`. Tracked as a
  parser-scope candidate gate (reject any non-ASCII-finite `value2` as `INVALID_VALUE` *before*
  the coherence decision, in the shared parser layer) — see the vault's `Gap_Gate_Coverage`.

## `submit.sh` portability

`submit.sh` hardcodes `export AMBERHOME=/Application/software/Amber26/pmemd26` — the
**advisor's** machine. Any local run must rewrite this to the local toolchain
(`source project-prime/scripts/env.sh`; pmemd at `~/Downloads/pmemd26/bin`). The hardcoded-path
anti-pattern is exactly what the vendored validator's foreign-path detector flags; the planned
`--submit` action (deferred) performs this rewrite on a copy before running a reduced smoke.

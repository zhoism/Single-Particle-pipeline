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
| Heat 3 | heat-3.in | 0 | NVT | 0.002 | 9.0 | 3 / 2.0 | 1·0·2 | **300** (200) | **1** · 5.0 | 1 / TEMP0 200→**310** ⚠️ |
| Press 3 | press-3.in | 0 | NPT | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 300 | **1** · 5.0 | — |
| Relax | relax.in | 0 | NPT | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 300 (300) | 0 · 0.0 | — |
| Prod | prod.in | 0 | NPT | 0.002 | 9.0 | 3 / 2.0 | 2·1·2 | 300 (300) | 0 · 0.0 | — |

`nstlim`: heat/press = 50000 (0.1 ns), relax = 500000 (1 ns), prod = 5000000 (**10 ns**).
`maxcyc` (min1/min2) = 10000. `restraintmask` (where `ntr=1`) = `"!:WAT,Cl-,K+,Na+ & !@H="`
(everything that is **not** water/ions, heavy atoms only — i.e. restrain the solute heavy
atoms, let solvent and hydrogens move).

**Parameter presence (what `mdin-edit` must know for stage-aware targeting):**
- `dt`, `temp0`: the **8 MD stages only** (absent in min1/min2).
- `cut`: all 10.
- `restraint_wt`: **present in all 10** — but `0.0` with `ntr=0` (restraints OFF) in
  min2/relax/prod, and `5.0` with `ntr=1` (ON) in min1/heat-1/2/3/press-1/2/3.
- `&wt` TEMP0 ramp: **heat-1/2/3 only**.

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

## ⚠️ The heat-3 inconsistency (and why the advisor's Task 3 is clever)

`heat-3.in` sets `&cntrl temp0 = 300.0` but its `&wt TEMP0` ramp ends at `value2 = 310.0` —
a 10 K disagreement (heat-1 is 100/100 and heat-2 is 200/200, both consistent). The system
actually ramps to 310 K while `temp0` claims 300 K.

The advisor's instruction *"set the target temperature to 310 K from the third stage onward"*
sets `temp0 = 310` on {heat-3, press-3, relax, prod}. On heat-3 that **aligns `temp0` with the
existing `&wt value2 = 310`, resolving the bug**. This is exactly why `mdin-edit` couples the
two: editing `temp0` in an `nmropt=1` heating stage also writes the `&wt value2`, so they can
never silently drift apart again. (The complementary fix — `temp0 = 300` on heat-3 — would
instead pull `value2` down to 300; the coupling handles both directions.)

## `submit.sh` portability

`submit.sh` hardcodes `export AMBERHOME=/Application/software/Amber26/pmemd26` — the
**advisor's** machine. Any local run must rewrite this to the local toolchain
(`source project-prime/scripts/env.sh`; pmemd at `~/Downloads/pmemd26/bin`). The hardcoded-path
anti-pattern is exactly what the vendored validator's foreign-path detector flags; the planned
`--submit` action (deferred) performs this rewrite on a copy before running a reduced smoke.

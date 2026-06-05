# amber-md-run heuristics

Distilled from the upstream amber-md `references/input-templates.md`, the project
`md-param-check` rules, `Research_amber_md_skill.md` (steal #7), and Amber26.pdf
ch.23–24 (sander/pmemd). Cited, not depended on.

## Why three minimizations (steal #7)

Releasing restraints in stages avoids structural blowups: min1 holds the whole
solute (`restraint_wt=500`) so only water/ions relax around it; min2 additionally
frees hydrogens (`(!:WAT,Na+,Cl-) & (!@H=)`); min3 is unrestrained. This is
leaner and safer than the advisor's 11-stage chain for a verification run.

## Heating: avoid the heat-3 bug

`heat.in` uses `nmropt=1` with a `&wt TEMP0` ramp from 0 → 300 K. The `&cntrl
temp0` MUST equal the `&wt value2` (both 300.0) — the advisor's `heat-3.in` set
`temp0=300` but ramped `&wt` to 310, so Langevin silently tracked 310 K. The
generator hard-codes them equal so `md-param-check`'s consistency gate passes.

Langevin (`ntt=3, gamma_ln=2.0`) gives a smoother, more statistical-mechanically
sound thermostat than Berendsen. `ig=-1` randomizes the seed so runs aren't
bit-identical.

## Ensemble progression

NVT during heating (`ntb=1, ntp=0`) → NPT for density + production (`ntb=2,
ntp=1, barostat=2` Monte-Carlo). MC barostat is robust and the project standard.
`iwrap=1` in production keeps molecules in the primary box (avoids diffusion
artifacts in the trajectory). Production is unrestrained (`ntr=0`).

## Restart wiring

heat starts cold from min3 coords (`irest=0, ntx=1`). density and production
continue with velocities (`irest=1, ntx=5`). Each stage's `-c` is the previous
stage's `.rst`; restrained stages (`ntr=1`) also need `-ref`. The generator
encodes this in `run.sh` so the chain is reproducible by hand.

## Trajectory sampling

Production writes ~500 frames: `ntwx = nstlim / 500`. NetCDF (`ioutfm=1`) is
compact and fast for cpptraj. ~200 MB per ns at this size.

## Engine choice (this machine)

Serial `pmemd` from `~/Downloads/pmemd26/bin` does **15.6 ns/day** on the 1L2Y
test system (M-series CPU, ~6000 atoms). Default 200 ps of MD ≈ 19 min.
`pmemd.MPI` (needs `mpirun`) is faster but adds a launcher dependency and an
MPI-runtime compatibility risk; opt in with `--engine pmemd.MPI --ncpus N`.
`sander` (AmberTools conda) is the single-toolchain fallback but slower.
No `pmemd.cuda` on macOS — GPU is a Scenario-B (remote) capability.

## prmtop compatibility

The topology is built by AmberTools 24.8 (conda) but run by pmemd 26 (separate
build). The prmtop format is stable across these, verified: pmemd 26 reads the
AT-24.8 `comp_oct.top` cleanly. If a future mismatch appears, `--engine sander`
keeps everything inside the conda toolchain.

## Crash signatures (Stage 8 hooks)

`vlimit exceeded` and `SHAKE failed` in a `.out` mean the integrator went
unstable (bad contacts, dt too large). The wrapper surfaces these as
`MD_CRASH[stage]`; the future bounded-recovery skill acts on them (lower dt,
re-minimize, restart) within hard limits.

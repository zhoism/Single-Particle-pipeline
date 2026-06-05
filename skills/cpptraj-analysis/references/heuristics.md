# cpptraj-analysis heuristics

Distilled from the upstream amber-md `references/analysis.md` + the example
`analysis/*/*.in` files (which are the *buggy* "before" versions), the
`Research_amber_md_skill.md` steal-list, and Amber26.pdf ch.25 (atom masks).

## The strip topology trap (the big one)

The trajectory is solvated (~6000 atoms). To strip water you need a topology
that matches it — `comp_oct.top`. After stripping, the frames are dry (~300
atoms) and match `comp_dry.top`, which every downstream analysis uses.

The upstream `strip.in` does `parm comp_dry.top; trajin product.nc` — a topology/
trajectory atom mismatch that should error. It only runs for them because their
`comp_dry.top` was saved after solvation (their bug), so it's secretly solvated.
With a correct dry topology you MUST strip with `comp_oct.top`.

```
# strip/strip.in
parm   ../comp_oct.top     # solvated — matches the trajectory
trajin ../traj.nc
strip :WAT,:Na+,:Cl-,:K+
autoimage
trajout strip.nc netcdf
# every other analysis: parm ../comp_dry.top ; trajin ../strip/strip.nc
```

## PCA must be two cpptraj calls (steal #4)

`diagmatrix` is an *analysis* (runs on `run`); `projection` checks for the
eigenvector dataset at *parse* time. In one call, projection parses before
diagmatrix has produced anything → "Set 'evecs' contains no data". Split it:

```
# call 1: matrix covar + diagmatrix + run  -> writes evecs.dat
# call 2: readdata evecs.dat name evecs ; projection PROJ modes evecs ... ; run
```

## Clustering: repout stays in the single command (steal #4)

`cluster C0 kmeans clusters 5 ... repout rep` as ONE command. Splitting
`repout rep repframe` into a second `cluster C0 ...` line makes cpptraj treat it
as a new cluster action with no algorithm → it silently defaults to hieragglo and
overrides the kmeans run.

## H-bond empty is a finding (steal #4)

For an indole ligand bound by hydrophobic / π–π contacts, cpptraj reports
`Set 'HBOND[UU]' contains no data`. That is the scientific result ("no stable
protein–ligand H-bonds"), not a failure. The skill reports it with an
explanatory placeholder figure and keeps `ok:true`.

## Auto-detect masks, don't hardcode :1-20 / :21

The upstream `.in` files hardcode protein `:1-20` and ligand `:21` for 1L2Y. We
parse `RESIDUE_LABEL` from `comp_dry.top`: with a ligand, `combine{prot,lig}`
appends it last, so protein = `:1-(N-1)`, ligand = `:N`. Protein-only systems use
`:1-N` and skip the ligand-specific analyses (hbond intermolecular, MM-GBSA).

## MM-GBSA needs the solvated trajectory + 4 topologies

`MMPBSA.py -sp comp_oct.top -cp comp_dry.top -rp protein.top -lp ligand.top -y
product.nc`. It strips solvent itself using `-sp`, so feed it the raw solvated
`product.nc` (not strip.nc). `igb=5, saltcon=0.1`. ΔG_bind < 0 = favorable. The
1L2Y/indole reference is ≈ −16 kcal/mol on a 1 ns trajectory; short verification
runs land in the same sign/order.

## Python parsing gotchas (steal #5)

- `evecs.dat`: header + `****` separators; hand-parse, only take lines that are
  `<int> <float>` for eigenvalues. `pd.read_csv` chokes.
- `summary.DENSITY` from `process_mdout.perl` can be 1 column (value only) —
  generate the x-axis with `np.arange` when so.
- cluster `summary.dat` first line starts with `#` and names columns; parse with
  explicit indices, don't let it become a data row.

## Path-with-space safety

cpptraj and MMPBSA tokenize input-file lines (and even some CLI args) on
whitespace, so an absolute path under `.../Single Particle/...` breaks. The
wrapper copies all inputs into the analysis dir under bare names and references
them relatively (`../comp_dry.top`, `../strip/strip.nc`).

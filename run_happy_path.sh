#!/usr/bin/env bash
# run_happy_path.sh — end-to-end local AMBER MD pipeline on the 1L2Y fixture.
#
# Chains the four Stage-2..5 skills as deterministic wrappers:
#   antechamber-ligandprep -> tleap-build -> amber-md-run -> cpptraj-analysis
# asserting ok:true on each JSON envelope, then asserts the full analysis suite
# produced >=12 analyses, >=10 PNGs, and a favorable (negative) MM-GBSA dG.
#
# This is the agent-free verification spine ("do + verify"). The OpenClaw agent
# performs the same chain conversationally; this script is what proves it green.
#
# Usage:
#   bash run_happy_path.sh [SIM_PS] [OUTDIR]
#     SIM_PS  production length in ps (default 100)
#     OUTDIR  results dir (default ./happy-path-run)
#
# Requires: prime-amber conda env active OR AmberTools on PATH, and pmemd in
# ~/Downloads/pmemd26/bin. Run with the env sourced, e.g.:
#   source /opt/homebrew/Caskroom/miniforge/base/envs/prime-amber/amber.sh
#   export PATH="$HOME/Downloads/pmemd26/bin:$AMBERHOME/bin:$PATH"

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS="$ROOT/skills"
FIX="$ROOT/golden-path/1L2Y"
SIM_PS="${1:-100}"
OUT="${2:-$ROOT/happy-path-run}"

AC="$SKILLS/antechamber-ligandprep/scripts/wrapper.py"
TL="$SKILLS/tleap-build/scripts/wrapper.py"
MD="$SKILLS/amber-md-run/scripts/wrapper.py"
AN="$SKILLS/cpptraj-analysis/scripts/wrapper.py"

say()  { echo -e "\n=== $* ===" >&2; }
die()  { echo "FAIL: $*" >&2; exit 1; }
jget() { python3 -c "import json,sys;print(json.load(open('$1'))$2)"; }
ok()   { python3 - "$1" <<'PY' || die "$2 (envelope ok=false)";
import json,sys; e=json.load(open(sys.argv[1]))
sys.exit(0 if e.get("ok") else (print("  errors:",e.get("errors"),file=sys.stderr) or 1))
PY
echo "  ok: $2" >&2; }

rm -rf "$OUT" && mkdir -p "$OUT"
echo "Happy path on 1L2Y | sim_ps=$SIM_PS | out=$OUT" >&2

# ---- Stage 2: ligand parameterization ------------------------------------
say "Stage 2 — antechamber-ligandprep (ligand -> GAFF2 mol2 + frcmod)"
python3 "$AC" --input "$FIX/ligand.pdb" --name MOL --charge 0 \
  --output-dir "$OUT/prep" > "$OUT/s2.json" || true
ok "$OUT/s2.json" "ligand prepped"
MOL2=$(jget "$OUT/s2.json" "['outputs']['mol2']")
FRCMOD=$(jget "$OUT/s2.json" "['outputs']['frcmod']")

# ---- Stage 3: topology build ---------------------------------------------
say "Stage 3 — tleap-build (protein + ligand -> solvated topology)"
python3 "$TL" --protein "$FIX/1L2Y-1.pdb" \
  --ligand-mol2 "$MOL2" --ligand-frcmod "$FRCMOD" --name MOL \
  --output-dir "$OUT/build" > "$OUT/s3.json" || true
ok "$OUT/s3.json" "topology built"
OCT=$(jget "$OUT/s3.json" "['outputs']['comp_oct_top']")
OCTC=$(jget "$OUT/s3.json" "['outputs']['comp_oct_crd']")
DRY=$(jget "$OUT/s3.json" "['outputs']['comp_dry_top']")
PROT=$(jget "$OUT/s3.json" "['outputs']['protein_top']")
LIG=$(jget "$OUT/s3.json" "['outputs']['ligand_top']")
echo "  dry/solvated atoms: $(jget "$OUT/s3.json" "['validation']['dry_atoms']")/$(jget "$OUT/s3.json" "['validation']['solvated_atoms']")" >&2

# ---- Stage 4: MD run ------------------------------------------------------
say "Stage 4 — amber-md-run (6-step chain, ${SIM_PS} ps production)"
python3 "$MD" --top "$OCT" --crd "$OCTC" --output-dir "$OUT/md" \
  --sim-ps "$SIM_PS" --engine pmemd > "$OUT/s4.json" || true
ok "$OUT/s4.json" "MD complete"
TRAJ=$(jget "$OUT/s4.json" "['outputs']['traj']")
echo "  wall time (s): $(jget "$OUT/s4.json" "['outputs'].get('wall_time_s')")" >&2

# ---- Stage 5: analysis ----------------------------------------------------
say "Stage 5 — cpptraj-analysis (full suite)"
python3 "$AN" --comp-oct-top "$OCT" --comp-dry-top "$DRY" --traj "$TRAJ" \
  --protein-top "$PROT" --ligand-top "$LIG" --mdout-dir "$OUT/md" \
  --output-dir "$OUT/analysis" --analyses all > "$OUT/s5.json" || true
ok "$OUT/s5.json" "analysis complete"

# ---- Final verification ---------------------------------------------------
say "Verify"
python3 - "$OUT/s5.json" "$OUT/analysis" <<'PY' || die "final verification"
import json, sys, pathlib
e = json.load(open(sys.argv[1])); o = e["outputs"]
produced = o["produced"]
assert len(produced) >= 12, f"only {len(produced)} analyses: {sorted(produced)}"
pngs = list(pathlib.Path(sys.argv[2]).rglob("*.png"))
assert len(pngs) >= 10, f"only {len(pngs)} PNGs"
dG = o.get("mmgbsa_dG_kcal_mol")
assert dG is not None and dG < 0, f"MM-GBSA dG not favorable: {dG}"
print(f"  analyses: {len(produced)} | PNGs: {len(pngs)} | MM-GBSA dG: {dG:.2f} kcal/mol", file=sys.stderr)
PY

echo -e "\nHAPPY PATH GREEN. Results in $OUT/analysis (open the .png files)." >&2

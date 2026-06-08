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
die()  { FAIL_MSG="$*"; echo "FAIL: $*" >&2; exit 1; }
jget() { python3 -c "import json,sys;print(json.load(open('$1'))$2)"; }
ok()   { python3 - "$1" <<'PY' || die "$2 (envelope ok=false)";
import json,sys; e=json.load(open(sys.argv[1]))
sys.exit(0 if e.get("ok") else (print("  errors:",e.get("errors"),file=sys.stderr) or 1))
PY
echo "  ok: $2" >&2; }

# ---- Optional Discord notifications (opt-in via NOTIFY_CHANNEL) ------------
# When NOTIFY_CHANNEL is set, post progress to that Discord channel through the
# LLM-free helper (works even during an LLM rate limit). Unset = silent, so this
# stays the agent-free verification spine with byte-identical default behavior.
NOTIFY_CHANNEL="${NOTIFY_CHANNEL:-}"
RUN_ID="${RUN_ID:-$(basename "$OUT")}"
NOTIFY_SH="$ROOT/scripts/notify_discord.sh"
CUR_STAGE="startup"; FAIL_MSG=""; DONE_OK=0
notify() {  # notify "<message>" ["<media>"] — never breaks the pipeline
  [ -n "$NOTIFY_CHANNEL" ] || return 0
  bash "$NOTIFY_SH" "$NOTIFY_CHANNEL" "$1" "${2:-}" || true
}
on_exit() {  # post a failure notice on any non-zero exit that didn't reach success
  local rc=$?
  [ -n "$NOTIFY_CHANNEL" ] || return 0
  if [ "$rc" -ne 0 ] && [ "$DONE_OK" -eq 0 ]; then
    notify "❌ Run ${RUN_ID} failed at: ${FAIL_MSG:-$CUR_STAGE} (rc=${rc}; log: ${OUT}/run.log)"
  fi
}
trap on_exit EXIT

rm -rf "$OUT" && mkdir -p "$OUT"
echo "Happy path on 1L2Y | sim_ps=$SIM_PS | out=$OUT" >&2
notify "🚀 Run ${RUN_ID} started — full AMBER MD pipeline on 1L2Y, ${SIM_PS} ps production (~10–15 min). I'll post each stage here."

# ---- Stage 2: ligand parameterization ------------------------------------
CUR_STAGE="Stage 2 (ligand prep)"
say "Stage 2 — antechamber-ligandprep (ligand -> GAFF2 mol2 + frcmod)"
python3 "$AC" --input "$FIX/ligand.pdb" --name MOL --charge 0 \
  --output-dir "$OUT/prep" > "$OUT/s2.json" || true
ok "$OUT/s2.json" "ligand prepped"
MOL2=$(jget "$OUT/s2.json" "['outputs']['mol2']")
FRCMOD=$(jget "$OUT/s2.json" "['outputs']['frcmod']")
notify "🧪 [${RUN_ID}] prep ✓ — ligand parameterized (GAFF2 atom types + AM1-BCC charges)."

# ---- Stage 3: topology build ---------------------------------------------
CUR_STAGE="Stage 3 (topology build)"
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
DRYN=$(jget "$OUT/s3.json" "['validation']['dry_atoms']"); SOLVN=$(jget "$OUT/s3.json" "['validation']['solvated_atoms']")
echo "  dry/solvated atoms: $DRYN/$SOLVN" >&2
notify "🧬 [${RUN_ID}] topology ✓ — ${DRYN}/${SOLVN} atoms (dry/solvated complex)."

# ---- Stage 4: MD run ------------------------------------------------------
CUR_STAGE="Stage 4 (MD run)"
say "Stage 4 — amber-md-run (6-step chain, ${SIM_PS} ps production)"
python3 "$MD" --top "$OCT" --crd "$OCTC" --output-dir "$OUT/md" \
  --sim-ps "$SIM_PS" --engine pmemd > "$OUT/s4.json" || true
ok "$OUT/s4.json" "MD complete"
TRAJ=$(jget "$OUT/s4.json" "['outputs']['traj']")
WALL=$(jget "$OUT/s4.json" "['outputs'].get('wall_time_s')")
echo "  wall time (s): $WALL" >&2
notify "⚛️ [${RUN_ID}] MD ✓ — ${SIM_PS} ps production complete (wall ${WALL}s)."

# ---- Stage 5: analysis ----------------------------------------------------
CUR_STAGE="Stage 5 (analysis)"
say "Stage 5 — cpptraj-analysis (full suite)"
python3 "$AN" --comp-oct-top "$OCT" --comp-dry-top "$DRY" --traj "$TRAJ" \
  --protein-top "$PROT" --ligand-top "$LIG" --mdout-dir "$OUT/md" \
  --output-dir "$OUT/analysis" --analyses all > "$OUT/s5.json" || true
ok "$OUT/s5.json" "analysis complete"
notify "📊 [${RUN_ID}] analysis ✓ — full suite done; running final verification…"

# ---- Final verification ---------------------------------------------------
CUR_STAGE="final verification"
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

# ---- Success notification -------------------------------------------------
DG=$(jget "$OUT/s5.json" "['outputs']['mmgbsa_dG_kcal_mol']")
NPROD=$(python3 -c "import json;print(len(json.load(open('$OUT/s5.json'))['outputs']['produced']))")
PNG=$(find "$OUT/analysis" -iname '*rmsd*.png' 2>/dev/null | head -1)
if [ -z "$PNG" ]; then PNG=$(find "$OUT/analysis" -iname '*.png' 2>/dev/null | head -1); fi
DONE_OK=1
notify "✅ Run ${RUN_ID} done — ${NPROD} analyses, MM-GBSA ΔG ${DG} kcal/mol. Full results in ${OUT}/analysis." "$PNG"

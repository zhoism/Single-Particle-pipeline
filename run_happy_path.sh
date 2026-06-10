#!/usr/bin/env bash
# run_happy_path.sh — end-to-end local AMBER MD pipeline. Defaults to the 1L2Y
# fixture; runs ANY protein + ligand via flags (--protein/--ligand/--charge/--name).
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
#   bash run_happy_path.sh [SIM_PS] [OUTDIR] [flags]
#     SIM_PS         production length in ps (default 100; positional or --sim-ps)
#     OUTDIR         results dir (default ./happy-path-run; positional or --output-dir)
#   Flags (all optional; default to the 1L2Y fixture):
#     --protein P    protein PDB path (default golden-path/1L2Y/1L2Y-1.pdb)
#     --ligand  L    ligand as a .pdb/.mol2/.sdf file OR an inline SMILES string
#                    (default golden-path/1L2Y/ligand.pdb)
#     --charge  N    ligand net formal charge for AM1-BCC (int, default 0)
#     --name    RES  ligand residue name, 1-4 uppercase alnum (default MOL)
#   Legacy positional SIM_PS/OUTDIR still work (overnight.sh, pipeline-async).
#
# Requires: prime-amber conda env active OR AmberTools on PATH, and pmemd in
# ~/Downloads/pmemd26/bin. Run with the env sourced, e.g.:
#   source /opt/homebrew/Caskroom/miniforge/base/envs/prime-amber/amber.sh
#   export PATH="$HOME/Downloads/pmemd26/bin:$AMBERHOME/bin:$PATH"

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS="$ROOT/skills"
FIX="$ROOT/golden-path/1L2Y"

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

# ---- Target inputs: flags + legacy positional, default to the 1L2Y fixture --
# Keeps no-arg / `run_happy_path.sh <ps> [outdir]` (overnight.sh, pipeline-async)
# byte-green while allowing any --protein/--ligand/--charge/--name. bash 3.2:
# no arrays under `set -u`; guard every `shift 2` so a value-less flag dies clean.
PROTEIN=""; LIGAND=""; CHARGE="0"; LIGNAME="MOL"; SIM_PS=""; OUT=""; pos=0
while [ $# -gt 0 ]; do
  case "$1" in
    --protein)     [ $# -ge 2 ] || die "--protein requires a value";     PROTEIN="$2"; shift 2 ;;
    --ligand)      [ $# -ge 2 ] || die "--ligand requires a value";      LIGAND="$2";  shift 2 ;;
    --charge)      [ $# -ge 2 ] || die "--charge requires a value";      CHARGE="$2";  shift 2 ;;
    --name)        [ $# -ge 2 ] || die "--name requires a value";        LIGNAME="$2"; shift 2 ;;
    --sim-ps)      [ $# -ge 2 ] || die "--sim-ps requires a value";      SIM_PS="$2";  shift 2 ;;
    --output-dir)  [ $# -ge 2 ] || die "--output-dir requires a value";  OUT="$2";     shift 2 ;;
    -h|--help)     echo "usage: run_happy_path.sh [SIM_PS] [OUTDIR] [--protein P] [--ligand L|SMILES] [--charge N] [--name RES]" >&2; exit 0 ;;
    --*)           die "unknown flag: $1" ;;
    *)
      pos=$((pos + 1))
      case "$pos" in
        1) [ -n "$SIM_PS" ] || SIM_PS="$1" ;;
        2) [ -n "$OUT" ]    || OUT="$1" ;;
        *) die "unexpected extra positional argument: $1" ;;
      esac
      shift ;;
  esac
done

# Defaults (1L2Y fixture) — empty means "not supplied", so a flag and a
# positional can't fight (the flag, parsed first, wins; the positional is skipped).
PROTEIN="${PROTEIN:-$FIX/1L2Y-1.pdb}"
LIGAND="${LIGAND:-$FIX/ligand.pdb}"
SIM_PS="${SIM_PS:-100}"
OUT="${OUT:-$ROOT/happy-path-run}"

# Validate up front (clear `die`, not a cryptic downstream crash).
echo "$SIM_PS"  | grep -Eq '^[0-9]+$' && [ "$SIM_PS" -gt 0 ] || die "--sim-ps must be a positive integer, got: $SIM_PS"
echo "$CHARGE"  | grep -Eq '^-?[0-9]+$'    || die "--charge must be an integer, got: $CHARGE"
echo "$LIGNAME" | grep -Eq '^[A-Z0-9]{1,4}$' || die "--name must be 1-4 uppercase letters/digits, got: $LIGNAME"
[ -f "$PROTEIN" ] || die "protein PDB not found: $PROTEIN"
# Ligand: a file with a recognized molecular extension MUST exist (else a typo'd
# path is silently treated as SMILES by antechamber and fails cryptically in obabel).
# Anything else (no known ext) is passed through as an inline SMILES string.
LIG_EXT=""
case "$LIGAND" in
  *.pdb|*.PDB)   LIG_EXT="pdb" ;;
  *.mol2|*.MOL2) LIG_EXT="mol2" ;;
  *.sdf|*.SDF)   LIG_EXT="sdf" ;;
  # Other molecular-file extensions the pipeline can't consume: reject with a
  # clear message rather than letting antechamber treat the path as a SMILES
  # string and fail cryptically in obabel (a typo'd path otherwise slips through).
  *.mol|*.MOL|*.sd|*.SD|*.sdf.gz|*.xyz|*.XYZ|*.pdbqt|*.PDBQT|*.smi|*.SMI|*.smiles|*.inchi|*.InChI|*.ml2|*.mol2.gz|*.cif|*.CIF|*.mae|*.MAE)
    die "ligand has an unsupported file extension (${LIGAND##*/}); pass .pdb/.mol2/.sdf or an inline SMILES string" ;;
esac
[ -z "$LIG_EXT" ] || [ -f "$LIGAND" ] || die "ligand file not found: $LIGAND"
TARGET="$(basename "$PROTEIN")"; TARGET="${TARGET%.*}"

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
# MD sub-stage progress for notify mode: post each of the 6 MD steps as its
# restart file lands (a step's <name>.rst is written only on success), with a
# heartbeat every 2 min on the long ones so the channel is never silent during
# the ~10-15 min MD. Backgrounded during Stage 4; killed when the stage returns.
md_progress_watch() {
  local md="$1" start now last i=0
  local steps=(min1 min2 min3 heat density product)
  local labels=("solvent-only minimization" "solvent+H minimization" \
                "full-system minimization" "heating 0→300 K (NVT)" \
                "density equilibration (NPT)" "production (NPT)")
  start=$(date +%s); last=$start
  while [ "$i" -lt 6 ]; do
    if [ -s "$md/${steps[$i]}.rst" ]; then
      now=$(date +%s)
      notify "⚙️ [${RUN_ID}] MD step $((i+1))/6 ✓ — ${steps[$i]}: ${labels[$i]} (MD elapsed $(( now - start ))s)"
      i=$(( i + 1 )); last=$now; continue
    fi
    now=$(date +%s)
    if [ $(( now - last )) -ge 120 ]; then
      notify "⏳ [${RUN_ID}] MD still running — on ${steps[$i]} (${labels[$i]}); $(( now - start ))s into the MD…"
      last=$now
    fi
    sleep 5
  done
}
on_exit() {  # post a failure notice on any non-zero exit that didn't reach success
  local rc=$?
  [ -n "$NOTIFY_CHANNEL" ] || return 0
  if [ "$rc" -ne 0 ] && [ "$DONE_OK" -eq 0 ]; then
    notify "❌ Run ${RUN_ID} failed at: ${FAIL_MSG:-$CUR_STAGE} (rc=${rc}; outputs + log under ${OUT})"
  fi
}
trap on_exit EXIT

rm -rf "$OUT" && mkdir -p "$OUT"

# Stage inputs under bare names inside OUT — neutralizes spaces / odd chars in
# user-supplied paths and gives the skills a predictable filename. The default
# fixture is copied too, so the path is uniform (content is byte-identical).
mkdir -p "$OUT/inputs"
cp "$PROTEIN" "$OUT/inputs/protein.pdb"
PROT_STAGED="$OUT/inputs/protein.pdb"
if [ -n "$LIG_EXT" ]; then
  cp "$LIGAND" "$OUT/inputs/ligand.$LIG_EXT"
  LIG_FOR_AC="$OUT/inputs/ligand.$LIG_EXT"
else
  LIG_FOR_AC="$LIGAND"   # inline SMILES — antechamber's classifier handles it
fi

echo "Happy path on $TARGET | sim_ps=$SIM_PS | out=$OUT" >&2
notify "🚀 Run ${RUN_ID} started — full AMBER MD pipeline on ${TARGET} · ${SIM_PS} ps production (~10–15 min). Plan: (1) ligand prep → (2) topology → (3) MD [min1·min2·min3·heat·density·production] → (4) analysis + MM-GBSA → (5) verify. I'll ping before & after each stage, and on every MD step."

# ---- Stage 2: ligand parameterization ------------------------------------
CUR_STAGE="Stage 2 (ligand prep)"
notify "▶️ [${RUN_ID}] Stage 2/5 starting — ligand prep (antechamber: ligand → GAFF2 atom types + AM1-BCC charges)…"
say "Stage 2 — antechamber-ligandprep (ligand -> GAFF2 mol2 + frcmod)"
python3 "$AC" --input "$LIG_FOR_AC" --name "$LIGNAME" --charge "$CHARGE" \
  --output-dir "$OUT/prep" > "$OUT/s2.json" || true
ok "$OUT/s2.json" "ligand prepped"
MOL2=$(jget "$OUT/s2.json" "['outputs']['mol2']")
FRCMOD=$(jget "$OUT/s2.json" "['outputs']['frcmod']")
notify "🧪 [${RUN_ID}] prep ✓ — ligand parameterized (GAFF2 atom types + AM1-BCC charges)."

# ---- Stage 3: topology build ---------------------------------------------
CUR_STAGE="Stage 3 (topology build)"
notify "▶️ [${RUN_ID}] Stage 3/5 starting — topology build (tleap: protein + ligand → solvated octahedral box)…"
say "Stage 3 — tleap-build (protein + ligand -> solvated topology)"
python3 "$TL" --protein "$PROT_STAGED" \
  --ligand-mol2 "$MOL2" --ligand-frcmod "$FRCMOD" --name "$LIGNAME" \
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
notify "▶️ [${RUN_ID}] Stage 4/5 starting — MD (6-step chain: min1·min2·min3·heat·density·production, ${SIM_PS} ps). This is the long one (~10-15 min); I'll post each step as it finishes."
say "Stage 4 — amber-md-run (6-step chain, ${SIM_PS} ps production)"
WATCH_PID=""
if [ -n "$NOTIFY_CHANNEL" ]; then ( set +e; md_progress_watch "$OUT/md" ) & WATCH_PID=$!; fi
python3 "$MD" --top "$OCT" --crd "$OCTC" --output-dir "$OUT/md" \
  --sim-ps "$SIM_PS" --engine pmemd > "$OUT/s4.json" || true
[ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null || true
ok "$OUT/s4.json" "MD complete"
TRAJ=$(jget "$OUT/s4.json" "['outputs']['traj']")
WALL=$(jget "$OUT/s4.json" "['outputs'].get('wall_time_s')")
echo "  wall time (s): $WALL" >&2
notify "⚛️ [${RUN_ID}] MD ✓ — ${SIM_PS} ps production complete (wall ${WALL}s)."

# ---- Stage 5: analysis ----------------------------------------------------
CUR_STAGE="Stage 5 (analysis)"
notify "▶️ [${RUN_ID}] Stage 5/5 starting — analysis (cpptraj: RMSD/RMSF/PCA/FEL/clustering/H-bonds + MM-GBSA ΔG)…"
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

# ---- Stage 6: PLIP interaction profile (non-fatal addendum) ---------------
# Runs AFTER the pipeline's GREEN verdict is locked in, so a PLIP failure can
# never regress the proven happy path. Enriches the result with the
# protein-ligand interaction fingerprint (the differentiator past MM-GBSA).
# Guarded by set +e and presence checks; default-1L2Y runs stay byte-green.
PLIP_SUMMARY=""
PL="$SKILLS/plip-profile/scripts/wrapper.py"
if [ -f "$PL" ] && [ -n "${OCT:-}" ] && [ -n "${DRY:-}" ] && [ -n "${TRAJ:-}" ]; then
  set +e
  notify "🔬 [${RUN_ID}] Stage 6 — PLIP protein-ligand interaction profiling…"
  say "Stage 6 — plip-profile (interaction fingerprint)"
  python3 "$PL" --comp-oct-top "$OCT" --comp-dry-top "$DRY" --traj "$TRAJ" \
    --name "$LIGNAME" --output-dir "$OUT/plip" > "$OUT/s6.json" 2>"$OUT/plip.err"
  if [ "$(jget "$OUT/s6.json" "['ok']" 2>/dev/null)" = "True" ]; then
    PLIP_SUMMARY=$(python3 -c "import json;d=json.load(open('$OUT/s6.json'))['outputs'];t=d['totals'];print(str(t['total_interactions'])+' interactions ('+', '.join(k+' '+str(v) for k,v in t['by_type'].items() if v)+')')" 2>/dev/null)
    echo "  PLIP: ${PLIP_SUMMARY:-(profiled)}" >&2
  else
    echo "  PLIP profiling did not complete (non-fatal; see $OUT/plip.err)" >&2
  fi
  set -e
fi

# ---- Success notification -------------------------------------------------
DG=$(jget "$OUT/s5.json" "['outputs']['mmgbsa_dG_kcal_mol']")
NPROD=$(python3 -c "import json;print(len(json.load(open('$OUT/s5.json'))['outputs']['produced']))")
PNG=$(find "$OUT/analysis" -iname '*rmsd*.png' 2>/dev/null | head -1)
if [ -z "$PNG" ]; then PNG=$(find "$OUT/analysis" -iname '*.png' 2>/dev/null | head -1); fi
DONE_OK=1
notify "✅ Run ${RUN_ID} done — ${NPROD} analyses, MM-GBSA ΔG ${DG} kcal/mol${PLIP_SUMMARY:+ · PLIP ${PLIP_SUMMARY}}. Full results in ${OUT}/analysis." "$PNG"

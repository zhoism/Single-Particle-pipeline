#!/usr/bin/env bash
# make_skillbase_deliverable.sh — assemble the advisor-facing PROOF package for the
# whole 9-skill pipeline, mirroring the single-skill mdin-edit deliverable format.
#
# Three pillars of proof, one folder:
#   1. it runs end-to-end    -> a fresh full pipeline run on 1L2Y (raw protein+ligand -> dG + PLIP)
#   2. each skill is correct  -> every skill's acceptance suite + the independent oracles, captured
#   3. the hard cases         -> amber-recover (induced-crash salvage) + md-planner (manifest) ride
#                                in the captured suites; mdin-edit is its own prior package
#
# Produces, in $OUTDIR (default: the sibling vault's deliverables-skillbase-<date>/):
#   skillbase-code-<date>.zip        git archive HEAD  (clean, re-runnable, no scratch)
#   pipeline-run-1L2Y-<date>.zip      00-inputs / 01-stage-envelopes / 02-analysis + RUN_LOG.txt
#   TEST_LOG.txt                      all 9 acceptance suites (no LIVE) + 5 independent oracles
#   generality-3HTB/ (folded into the run zip, if a prior 3HTB run is on disk)
# README.md and skillbase_summary.md are authored by hand (advisor-facing prose) — not regenerated.
#
# Usage:  bash scripts/make_skillbase_deliverable.sh [SIM_PS] [DATE] [OUTDIR]
#   SIM_PS  production length in ps   (default 100, matches the reference green run)
#   DATE    YYYYMMDD stamp            (default: today)
#   OUTDIR  deliverable folder        (default: ../Single Particle/deliverables-skillbase-<date>)
#
# Run with bash (NOT zsh) so env.sh's unmatched nvm glob stays literal instead of aborting.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SIM_PS="${1:-100}"
DATE="${2:-$(date +%Y%m%d)}"
OUTDIR="${3:-$ROOT/../Single Particle/deliverables-skillbase-$DATE}"
RUN="$ROOT/deliv-run-1L2Y-$DATE"

mkdir -p "$OUTDIR"
echo "==> toolchain"
# shellcheck disable=SC1091
set +u; source scripts/env.sh; set -u   # amber.sh references an unguarded DYLD_FALLBACK_LIBRARY_PATH

# ----------------------------------------------------------------------------------
# Pillar 1 — the fresh capstone run (agent-free verification spine)
# ----------------------------------------------------------------------------------
echo "==> [1/4] fresh 1L2Y pipeline run (${SIM_PS} ps) -> $RUN"
rm -rf "$RUN"
bash run_happy_path.sh "$SIM_PS" "$RUN"

# ----------------------------------------------------------------------------------
# Pillar 2 — capture every acceptance suite + the independent oracles (no LIVE=1)
# ----------------------------------------------------------------------------------
echo "==> [2/4] capturing acceptance suites + oracles -> TEST_LOG.txt"
LOG="$OUTDIR/TEST_LOG.txt"
{
  echo "# Skillbase verification log — $DATE"
  echo "# project-prime HEAD: $(git rev-parse --short HEAD)"
  echo "# Each skill's deterministic acceptance suite (LIVE cases NOT triggered) + independent oracles."
  echo
  pass=0; total=0
  for s in skills/*/test_acceptance.sh; do
    name="$(basename "$(dirname "$s")")"; total=$((total+1))
    echo "============================================================"
    echo "=== acceptance: $name"
    echo "============================================================"
    if bash "$s"; then echo "[$name] exit=0 PASS"; pass=$((pass+1)); else echo "[$name] exit=$? FAIL"; fi
    echo
  done
  echo "============================================================"
  echo "=== independent oracles (bounded modes)"
  echo "============================================================"
  for o in \
    "skills/md-planner/tests/test_planner_oracle.py" \
    "skills/amber-recover/tests/test_detector.py" \
    "skills/tleap-build/tests/test_neutrality_gate.py" \
    "skills/mdin-edit/tests/oracle_selftest.py"; do
    [ -f "$o" ] || { echo "(skip missing $o)"; continue; }
    echo "--- $o"; python3 "$o" && echo "[oracle ok] $o" || echo "[oracle FAIL] $o"; echo
  done
  echo "--- skills/mdin-edit/tests/fuzz_mdin_edit.py --quick"
  python3 skills/mdin-edit/tests/fuzz_mdin_edit.py --quick && echo "[oracle ok] fuzz --quick" || echo "[oracle FAIL] fuzz"
  echo
  echo "============================================================"
  echo "SUMMARY: $pass/$total acceptance suites green"
  echo "============================================================"
} 2>&1 | tee "$LOG"

# ----------------------------------------------------------------------------------
# Pillar / artifact — the clean code zip (only git-tracked files; no run output)
# ----------------------------------------------------------------------------------
echo "==> [3/4] code zip"
git archive --format=zip --prefix=Single-Particle-pipeline/ HEAD \
  -o "$OUTDIR/skillbase-code-$DATE.zip"

# ----------------------------------------------------------------------------------
# The run-output zip — pipeline lifecycle, 00-inputs / 01-stage-envelopes / 02-analysis
# ----------------------------------------------------------------------------------
echo "==> [4/4] assembling run-output zip"
PKG="$RUN/_package"
rm -rf "$PKG"; mkdir -p "$PKG"/{00-inputs,01-stage-envelopes,02-analysis}

# 00 — provenance: what went in
cp golden-path/1L2Y/1L2Y-1.pdb "$PKG/00-inputs/protein.pdb"
cp golden-path/1L2Y/ligand.pdb "$PKG/00-inputs/ligand.pdb"
printf 'bash run_happy_path.sh %s ./deliv-run-1L2Y-%s\n# 1L2Y protein + indole ligand, %s ps production\n' \
  "$SIM_PS" "$DATE" "$SIM_PS" > "$PKG/00-inputs/COMMAND.txt"

# 01 — the per-stage JSON envelopes (machine-readable skill->skill handoff proof)
cp "$RUN"/s2.json "$RUN"/s3.json "$RUN"/s4.json "$RUN"/s5.json "$PKG/01-stage-envelopes/" 2>/dev/null || true
for p in "$RUN"/plip*/*.json "$RUN"/analysis/plip/*summary*.json; do
  [ -f "$p" ] && cp "$p" "$PKG/01-stage-envelopes/plip_summary.json" && break
done

# 02 — the analysis tree (.dat + .png + MM-GBSA + PLIP report) and the production trajectory
[ -d "$RUN/analysis" ] && cp -R "$RUN/analysis" "$PKG/02-analysis/analysis"
for nc in "$RUN"/md/prod*.nc "$RUN"/md/*prod*.nc; do [ -f "$nc" ] && cp "$nc" "$PKG/02-analysis/" && break; done

# generality exhibit — fold in a prior 3HTB run if present (clearly marked, zero new compute)
if [ -f new-target-run/s5.json ]; then
  mkdir -p "$PKG/generality-3HTB"
  cp new-target-run/s2.json new-target-run/s3.json new-target-run/s4.json new-target-run/s5.json "$PKG/generality-3HTB/" 2>/dev/null || true
  printf 'Prior, REUSED run (not freshly produced for this package).\nTarget: 3HTB (T4-lysozyme L99A/M102Q + JZ4 2-propylphenol).\nEnd-to-end GREEN; see the stage envelopes here. Answers: the pipeline is not hardcoded to 1L2Y.\n' \
    > "$PKG/generality-3HTB/README.txt"
fi

# RUN_LOG.txt — high-level per-stage proof, computed from the envelopes
python3 - "$RUN" "$SIM_PS" "$PKG/RUN_LOG.txt" <<'PY'
import json, sys, os, glob
run, sim_ps, out = sys.argv[1], sys.argv[2], sys.argv[3]
def env(n):
    p=os.path.join(run,n)
    return json.load(open(p)) if os.path.exists(p) else {}
def g(e,*ks):
    for k in ks:
        if isinstance(e,dict) and k in e: return e[k]
        if isinstance(e,dict) and isinstance(e.get('outputs'),dict) and k in e['outputs']: return e['outputs'][k]
    return None
lines=[f"Pipeline run — 1L2Y (indole), {sim_ps} ps production", ""]
rows=[("Stage 2  antechamber-ligandprep","s2.json","ligand parameterized (GAFF2 + AM1-BCC)"),
      ("Stage 3  tleap-build","s3.json","solvated topology built, neutralized"),
      ("Stage 4  amber-md-run","s4.json","6-step MD chain ran to completion"),
      ("Stage 5  cpptraj-analysis","s5.json","analysis suite + MM-GBSA")]
for label,fn,desc in rows:
    e=env(fn); ok=g(e,'ok'); ok = e.get('ok',ok)
    lines.append(f"{label:32s}  ok={ok}   {desc}")
s5=env("s5.json")
dG=g(s5,'mmgbsa_dG_kcal_mol'); prod=g(s5,'produced') or []
pngs=len(glob.glob(os.path.join(run,'analysis','**','*.png'),recursive=True))
lines += ["",
          f"Analyses produced : {len(prod)}  ({', '.join(prod)})",
          f"PNG figures       : {pngs}",
          f"MM-GBSA dG        : {dG} kcal/mol  (favorable = negative)"]
# PLIP interaction count if present
for pj in glob.glob(os.path.join(run,'**','*plip*summary*.json'),recursive=True)+glob.glob(os.path.join(run,'plip','*.json')):
    try:
        pe=json.load(open(pj)); tot=pe.get('totals',{}) or (pe.get('outputs',{}) or {}).get('totals',{})
        if tot: lines.append(f"PLIP interactions : {tot}")
        break
    except Exception: pass
lines += ["", "Honest scope: this is a short-trajectory run. Each line is a passing deterministic",
          "gate (Run-GREEN / Analysis-GREEN) — NOT a claim of trajectory convergence or that the",
          "dG is a publication-grade binding free energy."]
open(out,'w').write("\n".join(lines)+"\n")
print(open(out).read())
PY

( cd "$RUN" && zip -qr "$OUTDIR/pipeline-run-1L2Y-$DATE.zip" _package -x '*.DS_Store' && \
  printf 'wrote %s\n' "$OUTDIR/pipeline-run-1L2Y-$DATE.zip" )

echo
echo "==> done. Deliverable at: $OUTDIR"
ls -lh "$OUTDIR"

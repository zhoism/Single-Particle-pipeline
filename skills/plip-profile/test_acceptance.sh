#!/usr/bin/env bash
# Acceptance test for plip-profile (Phase 3 Stage 6).
#
# Deterministic gate. Always-on self-contained cases (engine oracle, dry-run,
# malformed, invalid-ligand) need no MD. The real-trajectory golden cases use
# the existing run fixtures (regression-1L2Y, new-target-run); if a fixture is
# absent the case is SKIPPED LOUDLY (never a silent pass) and counted, with a
# prominent warning at the end if no real-trajectory case ran.
#
# Run under bash (env.sh trips zsh nomatch): bash test_acceptance.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"          # project-prime/
W="$HERE/scripts/wrapper.py"
PASS=0; FAIL=0; SKIP=0; GOLDEN_RAN=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# conda's amber.sh (pulled in by env.sh) references DYLD_FALLBACK_LIBRARY_PATH
# unguarded -> a fatal unbound-variable error under `set -u` in a sourced file
# aborts the shell (||true can't catch it). Disable -u just around the source.
set +u
# shellcheck disable=SC1090
source "$ROOT/scripts/env.sh" >/dev/null 2>&1 || true
set -u

ok()   { echo "  PASS $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL $1"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP $1"; SKIP=$((SKIP+1)); }

# jget <json-file> <python-index-expr> -> prints value (or empty on error)
jget() { python3 -c "import json,sys
try:
    print(json.load(open('$1'))$2)
except Exception:
    print('')"; }

echo "=== plip-profile acceptance ==="

# ---- 0. Engine unit-test oracle (self-contained) ----
echo "[0] engine unit tests"
if python3 "$HERE/tests/test_engine.py" >"$TMP/engine.log" 2>&1; then
  ok "engine oracle ($(grep -oE '[0-9]+ passed' "$TMP/engine.log" | head -1))"
else
  bad "engine oracle"; tail -8 "$TMP/engine.log"
fi

# ---- 1. dry-run (self-contained) ----
echo "[1] dry-run emits a plan, ok:true"
python3 "$W" --dry-run --comp-oct-top a --comp-dry-top b --traj c --name MOL \
  >"$TMP/dry.json" 2>/dev/null
[ "$(jget "$TMP/dry.json" "['ok']")" = "True" ] && \
  [ "$(jget "$TMP/dry.json" "['outputs']['plan']['frame_policy']")" = "medoid" ] \
  && ok "dry-run plan" || bad "dry-run plan"

# ---- 2. malformed: nonexistent trajectory -> ok:false, graceful ----
echo "[2] malformed input -> ok:false (graceful, not a crash)"
python3 "$W" --comp-oct-top /no/such.top --comp-dry-top /no/such2.top \
  --traj /no/such.nc --name MOL --output-dir "$TMP/m" \
  >"$TMP/mal.json" 2>/dev/null
rc=$?
[ "$(jget "$TMP/mal.json" "['ok']")" = "False" ] && [ $rc -ne 0 ] \
  && ok "malformed graceful ok:false" || bad "malformed graceful ok:false"

# ---- 3. invalid ligand resname (a standard AA) -> ok:false ----
echo "[3] --name ALA (a standard AA) -> ok:false up front"
# uses any topology path that exists or not; the AA check fires before file work
python3 "$W" --comp-oct-top a --comp-dry-top b --traj c --name ALA \
  --output-dir "$TMP/aa" >"$TMP/aa.json" 2>/dev/null
errs=$(jget "$TMP/aa.json" "['errors']")
[ "$(jget "$TMP/aa.json" "['ok']")" = "False" ] && echo "$errs" | grep -q "standard amino acid" \
  && ok "invalid ligand resname rejected" || bad "invalid ligand resname rejected"

# ---- helper: run a golden case against a run fixture ----
# golden <run-dir> <ligand-resname> <expect-normalization yes|no>
golden() {
  local R="$1" LIG="$2" NORM="$3" name; name="$(basename "$R")"
  local oct="$R/build/tleap-build-run/comp_oct.top"
  local dry="$R/build/tleap-build-run/comp_dry.top"
  local traj="$R/md/product.nc"
  if [ ! -f "$oct" ] || [ ! -f "$dry" ] || [ ! -f "$traj" ]; then
    skip "golden $name (fixture absent: run the pipeline first)"; return
  fi
  GOLDEN_RAN=1
  local out="$TMP/$name"
  python3 "$W" --comp-oct-top "$oct" --comp-dry-top "$dry" --traj "$traj" \
    --name "$LIG" --output-dir "$out" >"$TMP/$name.json" 2>"$TMP/$name.err"
  local J="$TMP/$name.json"
  [ "$(jget "$J" "['ok']")" = "True" ] && ok "golden $name ok:true" || { bad "golden $name ok:true"; tail -5 "$TMP/$name.err"; }
  [ "$(jget "$J" "['validation']['ligand_detected']")" = "True" ] && ok "golden $name ligand $LIG detected" || bad "golden $name ligand detected"
  [ "$(jget "$J" "['validation']['phantom_ligands']")" = "[]" ] && ok "golden $name no phantom ligands" || bad "golden $name no phantom ligands"
  local nint; nint="$(jget "$J" "['validation']['interactions_found']")"
  [ -n "$nint" ] && [ "$nint" -ge 1 ] && ok "golden $name $nint interactions (>=1)" || bad "golden $name >=1 interaction"
  if [ "$NORM" = "yes" ]; then
    local changed; changed="$(jget "$J" "['outputs']['resnames_changed']")"
    echo "$changed" | grep -qE "HIE|HID|HIP|CYX|CYM|ASH|GLH|LYN" \
      && ok "golden $name normalization FIRED ($changed)" \
      || bad "golden $name normalization should have fired (got $changed)"
  fi
  # stash first golden for determinism + control reuse
  echo "$out" > "$TMP/last_golden_dir"
  echo "$J" > "$TMP/last_golden_json"
}

# ---- 4. golden: 1L2Y (no His -> normalization is a no-op) ----
echo "[4] golden 1L2Y (ligand MOL)"
golden "$ROOT/regression-1L2Y" "MOL" "no"

# ---- 5. golden + normalization: 3HTB (HIE present -> must normalize) ----
echo "[5] golden 3HTB (ligand JZ4, HIE -> HIS normalization must fire)"
golden "$ROOT/new-target-run" "JZ4" "yes"

# ---- 6. determinism: re-run a golden, interaction section byte-identical ----
echo "[6] determinism (medoid is reproducible)"
if [ -f "$ROOT/new-target-run/md/product.nc" ]; then
  R="$ROOT/new-target-run"
  for i in 1 2; do
    python3 "$W" --comp-oct-top "$R/build/tleap-build-run/comp_oct.top" \
      --comp-dry-top "$R/build/tleap-build-run/comp_dry.top" \
      --traj "$R/md/product.nc" --name JZ4 --output-dir "$TMP/det$i" \
      >"$TMP/det$i.json" 2>/dev/null
    python3 -c "import json; d=json.load(open('$TMP/det$i.json')); print(json.dumps({'interactions':d['outputs']['interactions'],'frame':d['outputs']['frame'],'totals':d['outputs']['totals']},sort_keys=True))" >"$TMP/det$i.norm"
  done
  if diff -q "$TMP/det1.norm" "$TMP/det2.norm" >/dev/null; then
    ok "determinism (two runs byte-identical interaction profile)"
  else
    bad "determinism (re-run differs)"; diff "$TMP/det1.norm" "$TMP/det2.norm" | head
  fi
else
  skip "determinism (fixture absent)"
fi

# ---- 7. phantom control: normalization is load-bearing ----
# PLIP on the RAW (un-normalized) cluster rep with HIE must invent a phantom
# protein-as-ligand; the skill's normalized run (case 5) must not.
echo "[7] phantom control (raw HIE -> PLIP phantom; normalized -> none)"
RAW="$ROOT/new-target-run/analysis/cluster/rep.c0.pdb"
if [ -f "$RAW" ] && command -v plip >/dev/null 2>&1 && grep -q " HIE " "$RAW"; then
  cdir="$TMP/control"; mkdir -p "$cdir"; cp "$RAW" "$cdir/raw.pdb"
  ( cd "$cdir" && plip -f raw.pdb -x -o out >plip.log 2>&1 || true )
  if grep -qoE "<hetid>(HIE|HID|HIP|CYX|CYM|ASH|GLH|LYN|HIS|CYS)</hetid>" "$cdir"/out/*.xml 2>/dev/null; then
    ok "control: raw HIE frame DID produce a phantom protein-as-ligand (footgun real)"
  else
    bad "control: expected a phantom on the raw frame, found none"
  fi
  # and confirm the skill's normalized 3HTB run had zero phantoms (re-assert)
  if [ -f "$TMP/new-target-run.json" ]; then
    [ "$(jget "$TMP/new-target-run.json" "['validation']['phantom_ligands']")" = "[]" ] \
      && ok "control: normalized run had zero phantoms (fix works)" \
      || bad "control: normalized run still had phantoms"
  fi
else
  skip "phantom control (raw HIE fixture or plip absent)"
fi

# ---- 8. frame policies: last + explicit-N ----
echo "[8] frame policies (last, explicit-N)"
if [ -f "$ROOT/regression-1L2Y/md/product.nc" ]; then
  R="$ROOT/regression-1L2Y"
  python3 "$W" --comp-oct-top "$R/build/tleap-build-run/comp_oct.top" \
    --comp-dry-top "$R/build/tleap-build-run/comp_dry.top" \
    --traj "$R/md/product.nc" --name MOL --frame last --output-dir "$TMP/last" \
    >"$TMP/last.json" 2>/dev/null
  [ "$(jget "$TMP/last.json" "['ok']")" = "True" ] && \
  [ "$(jget "$TMP/last.json" "['outputs']['frame']['policy']")" = "last" ] \
    && ok "frame=last" || bad "frame=last"
  python3 "$W" --comp-oct-top "$R/build/tleap-build-run/comp_oct.top" \
    --comp-dry-top "$R/build/tleap-build-run/comp_dry.top" \
    --traj "$R/md/product.nc" --name MOL --frame 250 --output-dir "$TMP/n" \
    >"$TMP/n.json" 2>/dev/null
  [ "$(jget "$TMP/n.json" "['outputs']['frame']['index']")" = "250" ] \
    && ok "frame=250 (explicit)" || bad "frame=250 (explicit)"
else
  skip "frame policies (fixture absent)"
fi

echo
echo "=== plip-profile acceptance: $PASS passed, $FAIL failed, $SKIP skipped ==="
if [ "$GOLDEN_RAN" -eq 0 ]; then
  echo "⚠️  WARNING: no real-trajectory golden case ran (run fixtures absent)."
  echo "    Self-contained cases passed, but end-to-end PLIP was NOT exercised."
fi
[ "$FAIL" -eq 0 ] || exit 1

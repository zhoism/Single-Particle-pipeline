#!/usr/bin/env bash
# cpptraj-analysis acceptance test.
#
# Builds a topology + short trajectory (antechamber -> tleap-build -> amber-md-run)
# then:
#   1. Golden    — full suite on the protein-ligand trajectory. Asserts core ok,
#                  >=12 analyses produced PNGs, MM-GBSA dG < 0, and the GB radii
#                  were retyped to mbondi2 (consistent; comp_dry_mbondi2.top exists).
#   2. Subset    — --analyses rmsd,rg. Asserts only those produced.
#   3. Malformed — nonexistent trajectory. Asserts ok=false.
#   4. GB fatal  — parmed shadowed/broken so the mbondi2 fix can't take; asserts
#                  ok=false BECAUSE of the surviving GB-radii mismatch (core still
#                  produced), proving the detector is now a fatal gate.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
ANTECHAMBER="$SKILL_DIR/../antechamber-ligandprep/scripts/wrapper.py"
TLEAP="$SKILL_DIR/../tleap-build/scripts/wrapper.py"
MDRUN="$SKILL_DIR/../amber-md-run/scripts/wrapper.py"
GOLDEN_DIR="$SKILL_DIR/../../golden-path/1L2Y"
RUN_BASE="$SKILL_DIR/test-runs"
mkdir -p "$RUN_BASE"

pass() { echo "PASS: $1" >&2; }
fail() { echo "FAIL: $1" >&2; exit 1; }
jget() { python3 -c "import json,sys;print(json.load(open('$1'))$2)"; }

assert_ok()   { python3 - "$1" <<'PY' || fail "$2";
import json,sys; e=json.load(open(sys.argv[1]))
sys.exit(0 if e.get("ok") else (print("  errors:",e.get("errors"),file=sys.stderr) or 1))
PY
pass "$2"; }
assert_fail() { python3 - "$1" <<'PY' || fail "$2 did not fail gracefully";
import json,sys; e=json.load(open(sys.argv[1]))
sys.exit(0 if (e.get("ok") is False and e.get("errors")) else 1)
PY
pass "$2"; }

# ---- setup: build topology + short trajectory ----------------------------
echo "[setup] antechamber -> tleap-build -> amber-md-run (short)" >&2
S="$RUN_BASE/setup"; rm -rf "$S" && mkdir -p "$S"
python3 "$ANTECHAMBER" --input "$GOLDEN_DIR/ligand.pdb" --name MOL --charge 0 \
  --output-dir "$S" > "$S/ac.json" || true
MOL2=$(jget "$S/ac.json" "['outputs']['mol2']")
FRCMOD=$(jget "$S/ac.json" "['outputs']['frcmod']")
python3 "$TLEAP" --protein "$GOLDEN_DIR/1L2Y-1.pdb" \
  --ligand-mol2 "$MOL2" --ligand-frcmod "$FRCMOD" --name MOL \
  --output-dir "$S" > "$S/tleap.json" || true
assert_ok "$S/tleap.json" "setup: topology"
OCT=$(jget "$S/tleap.json" "['outputs']['comp_oct_top']")
OCTC=$(jget "$S/tleap.json" "['outputs']['comp_oct_crd']")
DRY=$(jget "$S/tleap.json" "['outputs']['comp_dry_top']")
PROT=$(jget "$S/tleap.json" "['outputs']['protein_top']")
LIG=$(jget "$S/tleap.json" "['outputs']['ligand_top']")
python3 "$MDRUN" --top "$OCT" --crd "$OCTC" --output-dir "$S/md" \
  --heat-ps 4 --density-ps 4 --sim-ps 10 --engine pmemd > "$S/md.json" || true
assert_ok "$S/md.json" "setup: short MD"
TRAJ=$(jget "$S/md.json" "['outputs']['traj']")

# ---- Case 1: Golden (full suite) -----------------------------------------
echo "[case 1] Golden — full analysis suite" >&2
G="$RUN_BASE/golden"; rm -rf "$G" && mkdir -p "$G"
python3 "$WRAPPER" --comp-oct-top "$OCT" --comp-dry-top "$DRY" --traj "$TRAJ" \
  --protein-top "$PROT" --ligand-top "$LIG" --mdout-dir "$S/md" \
  --output-dir "$G/analysis" --analyses all > "$G/an.json" || true
assert_ok "$G/an.json" "Golden full suite (core ok)"
python3 - "$G/an.json" <<'PY' || fail "Golden: <12 analyses or MM-GBSA not negative"
import json,sys
e=json.load(open(sys.argv[1])); o=e["outputs"]
prod=set(o["produced"])
assert len(prod)>=12, f"only {len(prod)} produced: {sorted(prod)}"
dG=o.get("mmgbsa_dG_kcal_mol")
assert dG is not None and dG<0, f"MM-GBSA dG not favorable: {dG}"
PY
pass "Golden: 12 analyses + MM-GBSA dG<0"

# Golden also proves the GB-radii fix took: validation reports consistent, and the
# regenerated comp_dry topology actually reads (mbondi2) — the load-bearing
# assumption that parmed changeRadii rewrites the RADIUS_SET descriptor.
python3 - "$G/an.json" "$G/analysis" "$SKILL_DIR/scripts" <<'PY' || fail "Golden: GB radii not retyped to mbondi2 / not consistent"
import json,sys,pathlib
sys.path.insert(0, sys.argv[3]); import wrapper as W
e=json.load(open(sys.argv[1])); gb=e["validation"]["gb_radii"]
assert gb and gb["required"]=="mbondi2" and gb["consistent"] is True, f"gb_radii: {gb}"
top=pathlib.Path(sys.argv[2])/"comp_dry_mbondi2.top"
assert top.is_file(), f"missing regenerated top {top}"
assert W.prmtop_radius_set(top)=="mbondi2", W.prmtop_radius_set(top)
print("  gb_radii consistent:true + comp_dry_mbondi2.top reads (mbondi2)", file=sys.stderr)
PY
pass "Golden: GB radii retyped to mbondi2 (consistent)"

# ---- Case 2: Subset ------------------------------------------------------
echo "[case 2] Subset — rmsd,rg only" >&2
U="$RUN_BASE/subset"; rm -rf "$U" && mkdir -p "$U"
python3 "$WRAPPER" --comp-oct-top "$OCT" --comp-dry-top "$DRY" --traj "$TRAJ" \
  --output-dir "$U/analysis" --analyses rmsd,rg > "$U/an.json" || true
assert_ok "$U/an.json" "Subset run"
python3 - "$U/an.json" <<'PY' || fail "Subset produced unexpected analyses"
import json,sys
prod=set(json.load(open(sys.argv[1]))["outputs"]["produced"])
assert prod=={"rmsd","rg"}, f"expected just rmsd,rg got {sorted(prod)}"
PY
pass "Subset: only rmsd,rg"

# ---- Case 3: Malformed ---------------------------------------------------
echo "[case 3] Malformed — nonexistent trajectory" >&2
M="$RUN_BASE/malformed"; rm -rf "$M" && mkdir -p "$M"
python3 "$WRAPPER" --comp-oct-top "$OCT" --comp-dry-top "$DRY" \
  --traj "$M/nope.nc" --output-dir "$M/analysis" --analyses rmsd \
  > "$M/an.json" 2> "$M/stderr" || true
assert_fail "$M/an.json" "Malformed (graceful failure)"

# ---- Case 4: GB-radii fix tool unavailable -> persistent mismatch is fatal -
# Shadow `parmed` with a stub that fails (writes no retyped top) so the mbondi2
# fix can't take. The detector then sees the UNFIXED mbondi comp_dry, and the
# suite must red (ok:false) BECAUSE of the radii mismatch even though the core
# analyses (rmsd,rmsf,rg) still produced. Needs --protein/--ligand tops (else the
# protein-only early return makes mmgbsa skip and the run vacuously ok:true), and
# must run inside the AMBER env (real cpptraj/MMPBSA still resolve past the stub).
echo "[case 4] GB-radii fix forced off -> ok:false" >&2
F="$RUN_BASE/gbfatal"; rm -rf "$F" && mkdir -p "$F/fakebin"
printf '#!/bin/sh\nexit 1\n' > "$F/fakebin/parmed"; chmod +x "$F/fakebin/parmed"
PATH="$F/fakebin:$PATH" python3 "$WRAPPER" \
  --comp-oct-top "$OCT" --comp-dry-top "$DRY" --traj "$TRAJ" \
  --protein-top "$PROT" --ligand-top "$LIG" \
  --output-dir "$F/analysis" --analyses rmsd,rmsf,rg,mmgbsa > "$F/an.json" || true
assert_fail "$F/an.json" "Case 4 fatal gate (graceful ok:false)"
python3 - "$F/an.json" <<'PY' || fail "Case 4: expected read mismatch + FATAL error, core still produced"
import json,sys
e=json.load(open(sys.argv[1])); gb=e["validation"]["gb_radii"]
assert gb and gb["consistent"] is False, f"gb_radii not a read mismatch: {gb}"
assert any("GB_RADII_IGB_MISMATCH" in x for x in e["errors"]), e["errors"]
assert set(e["outputs"]["produced"]) >= {"rmsd","rmsf","rg"}, e["outputs"]["produced"]
PY
pass "Case 4: core green but persistent GB-radii mismatch -> ok:false"

echo "[acceptance] all cases passed" >&2

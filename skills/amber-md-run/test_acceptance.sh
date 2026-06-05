#!/usr/bin/env bash
# amber-md-run acceptance test.
#
# Builds a fresh 1L2Y topology (antechamber-ligandprep -> tleap-build) then:
#   1. Golden    — --steps min on the solvated topology. Asserts ok, all 3 .rst,
#                  no crashes.
#   2. Dry-run   — full chain --dry-run. Asserts heat temp0 == &wt value2 and
#                  product barostat=2 (md-param-check-relevant invariants).
#   3. Malformed — nonexistent topology. Asserts ok=false with a code.
#
# Needs the AMBER engine on PATH (pmemd from ~/Downloads/pmemd26) + AmberTools.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
ANTECHAMBER="$SKILL_DIR/../antechamber-ligandprep/scripts/wrapper.py"
TLEAP="$SKILL_DIR/../tleap-build/scripts/wrapper.py"
GOLDEN_DIR="$SKILL_DIR/../../golden-path/1L2Y"
RUN_BASE="$SKILL_DIR/test-runs"
mkdir -p "$RUN_BASE"

pass() { echo "PASS: $1" >&2; }
fail() { echo "FAIL: $1" >&2; exit 1; }

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

# ---- Build a topology for cases 1 + 2 ------------------------------------
echo "[setup] building 1L2Y topology (antechamber -> tleap-build)" >&2
B="$RUN_BASE/build"; rm -rf "$B" && mkdir -p "$B"
python3 "$ANTECHAMBER" --input "$GOLDEN_DIR/ligand.pdb" --name MOL --charge 0 \
  --output-dir "$B" > "$B/ac.json" || true
MOL2=$(python3 -c "import json;print(json.load(open('$B/ac.json'))['outputs']['mol2'])")
FRCMOD=$(python3 -c "import json;print(json.load(open('$B/ac.json'))['outputs']['frcmod'])")
python3 "$TLEAP" --protein "$GOLDEN_DIR/1L2Y-1.pdb" \
  --ligand-mol2 "$MOL2" --ligand-frcmod "$FRCMOD" --name MOL \
  --output-dir "$B" > "$B/tleap.json" || true
assert_ok "$B/tleap.json" "Topology build (prereq)"
TOP=$(python3 -c "import json;print(json.load(open('$B/tleap.json'))['outputs']['comp_oct_top'])")
CRD=$(python3 -c "import json;print(json.load(open('$B/tleap.json'))['outputs']['comp_oct_crd'])")

# ---- Case 1: Golden (min smoke test) ------------------------------------
echo "[case 1] Golden — --steps min" >&2
G="$RUN_BASE/min"; rm -rf "$G" && mkdir -p "$G"
python3 "$WRAPPER" --top "$TOP" --crd "$CRD" --output-dir "$G/md" \
  --steps min --engine pmemd > "$G/md.json" || true
assert_ok "$G/md.json" "Golden min run"
python3 - "$G/md.json" <<'PY' || fail "Golden min: not all stages produced .rst / clean"
import json,sys
v=json.load(open(sys.argv[1]))["validation"]["stages"]
bad=[k for k,s in v.items() if not s["rst"] or s["crashes"]]
sys.exit(1 if bad else 0)
PY
pass "Golden min: 3 .rst, no crashes"

# ---- Case 2: Dry-run namelist invariants --------------------------------
echo "[case 2] Dry-run — namelist invariants" >&2
python3 "$WRAPPER" --top "$TOP" --crd "$CRD" --output-dir "$RUN_BASE/dry/md" \
  --sim-ps 100 --dry-run > "$RUN_BASE/dry.json" || true
assert_ok "$RUN_BASE/dry.json" "Dry-run ok"
python3 - "$RUN_BASE/dry.json" <<'PY' || fail "Dry-run namelist invariants"
import json,sys,re
nl=json.load(open(sys.argv[1]))["validation"]["namelists"]
heat=nl["heat"]
t0=re.search(r"temp0=([\d.]+)",heat).group(1)
v2=re.search(r"value2=([\d.]+)",heat).group(1)
assert float(t0)==float(v2), f"heat temp0 {t0} != &wt value2 {v2}"
assert "barostat=2" in nl["product"], "product missing barostat=2"
assert "iwrap=1" in nl["product"], "product missing iwrap=1"
PY
pass "Dry-run: heat temp0==value2, product barostat=2 + iwrap=1"

# ---- Case 3: Malformed ---------------------------------------------------
echo "[case 3] Malformed — nonexistent topology" >&2
M="$RUN_BASE/malformed"; rm -rf "$M" && mkdir -p "$M"
python3 "$WRAPPER" --top "$M/nope.top" --crd "$M/nope.crd" \
  --output-dir "$M/md" > "$M/md.json" 2> "$M/stderr" || true
assert_fail "$M/md.json" "Malformed (graceful failure)"

echo "[acceptance] all cases passed" >&2

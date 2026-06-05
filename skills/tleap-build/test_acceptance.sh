#!/usr/bin/env bash
# tleap-build acceptance test.
#
# Three cases on the 1L2Y golden fixture:
#   1. Golden    — protein + indole ligand. Asserts ok, dry<solvated, and the
#                  protein+ligand==dry combine invariant.
#   2. Unrelated — protein-only build (no ligand). Asserts ok, dry<solvated.
#   3. Malformed — nonexistent protein path. Asserts ok=false with an error code.
#
# The ligand mol2/frcmod for case 1 are produced by running the upstream
# antechamber-ligandprep skill first (the real Stage 2 -> Stage 3 handoff).
#
# All three must pass before this skill flips BUILT -> COMPLETE in the manifest.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
ANTECHAMBER_WRAPPER="$SKILL_DIR/../antechamber-ligandprep/scripts/wrapper.py"
GOLDEN_DIR="$SKILL_DIR/../../golden-path/1L2Y"
PROTEIN="$GOLDEN_DIR/1L2Y-1.pdb"
LIGAND="$GOLDEN_DIR/ligand.pdb"
RUN_BASE="$SKILL_DIR/test-runs"

mkdir -p "$RUN_BASE"

pass() { echo "PASS: $1" >&2; }
fail() { echo "FAIL: $1" >&2; exit 1; }

assert_ok() {
  python3 - "$1" "$2" <<'PY' || fail "$2 (see $1)"
import json, sys
e = json.load(open(sys.argv[1]))
if not e.get("ok"):
    print("  errors:", e.get("errors", []), file=sys.stderr); sys.exit(1)
PY
  pass "$2"
}

assert_fail() {
  python3 - "$1" "$2" <<'PY' || fail "$2 did NOT fail gracefully"
import json, sys
e = json.load(open(sys.argv[1]))
if e.get("ok") is False and e.get("errors"):
    sys.exit(0)
print("  ok:", e.get("ok"), "errors:", e.get("errors"), file=sys.stderr); sys.exit(1)
PY
  pass "$2"
}

assert_dry_lt_solv() {
  python3 - "$1" "$2" <<'PY' || fail "$2"
import json, sys
e = json.load(open(sys.argv[1])); v = e.get("validation", {})
dry, solv = v.get("dry_atoms"), v.get("solvated_atoms")
if dry is None or solv is None or not (dry < solv):
    print("  dry:", dry, "solvated:", solv, file=sys.stderr); sys.exit(1)
PY
  pass "$2"
}

assert_combine_invariant() {
  python3 - "$1" "$2" <<'PY' || fail "$2"
import json, sys
e = json.load(open(sys.argv[1])); v = e.get("validation", {})
p, l, d = v.get("protein_atoms"), v.get("ligand_atoms"), v.get("dry_atoms")
if None in (p, l, d) or p + l != d:
    print("  protein:", p, "ligand:", l, "dry:", d, file=sys.stderr); sys.exit(1)
PY
  pass "$2"
}

[[ -f "$PROTEIN" ]] || fail "golden protein missing at $PROTEIN"
[[ -f "$LIGAND"  ]] || fail "golden ligand missing at $LIGAND"

# ---- Case 1: Golden (protein + ligand) -----------------------------------
echo "[case 1] Golden — 1L2Y protein + indole ligand" >&2
G="$RUN_BASE/golden"; rm -rf "$G" && mkdir -p "$G"

echo "  [1a] antechamber-ligandprep on the ligand (Stage 2 handoff)" >&2
python3 "$ANTECHAMBER_WRAPPER" \
  --input "$LIGAND" --name MOL --charge 0 \
  --output-dir "$G" > "$G/antechamber.json" || true
assert_ok "$G/antechamber.json" "Stage-2 ligand prep (MOL.mol2 + MOL.frcmod)"

# Read the real artifact paths from the Stage-2 envelope (they live in a -run/ subdir).
MOL2=$(python3 -c "import json;print(json.load(open('$G/antechamber.json'))['outputs']['mol2'])")
FRCMOD=$(python3 -c "import json;print(json.load(open('$G/antechamber.json'))['outputs']['frcmod'])")

echo "  [1b] tleap-build on protein + ligand params" >&2
python3 "$WRAPPER" \
  --protein "$PROTEIN" \
  --ligand-mol2 "$MOL2" --ligand-frcmod "$FRCMOD" --name MOL \
  --output-dir "$G" > "$G/tleap.json" || true
assert_ok "$G/tleap.json" "Golden tleap-build (protein+ligand)"
assert_dry_lt_solv "$G/tleap.json" "Golden dry < solvated (steal #1 sanity)"
assert_combine_invariant "$G/tleap.json" "Golden protein+ligand == dry complex"

# ---- Case 2: Unrelated (protein-only) ------------------------------------
echo "[case 2] Unrelated — protein-only build (no ligand)" >&2
U="$RUN_BASE/protein_only"; rm -rf "$U" && mkdir -p "$U"
python3 "$WRAPPER" \
  --protein "$PROTEIN" --output-dir "$U" > "$U/tleap.json" || true
assert_ok "$U/tleap.json" "Protein-only tleap-build"
assert_dry_lt_solv "$U/tleap.json" "Protein-only dry < solvated"

# ---- Case 3: Malformed (missing protein) ---------------------------------
echo "[case 3] Malformed — nonexistent protein path" >&2
M="$RUN_BASE/malformed"; rm -rf "$M" && mkdir -p "$M"
python3 "$WRAPPER" \
  --protein "$M/does_not_exist.pdb" --output-dir "$M" \
  > "$M/tleap.json" 2> "$M/wrapper.stderr" || true
assert_fail "$M/tleap.json" "Malformed (graceful failure)"

echo "[acceptance] all three cases passed" >&2

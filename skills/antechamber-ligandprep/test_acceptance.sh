#!/usr/bin/env bash
# antechamber-ligandprep acceptance test.
#
# Three test cases per the manifest's general acceptance discipline:
#   1. Golden    — benzene PDB extract from PDB 181L (project-prime/golden-path/).
#   2. Unrelated — methane via SMILES "C" (exercises the SMILES path).
#   3. Malformed — a junk PDB (asserts ok=false with a parseable error code).
#
# All three must pass before this skill is promoted from BUILT to COMPLETE
# in Phase3_Taskboard_Manifest.md.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
RUN_BASE="$SKILL_DIR/test-runs"
GOLDEN_INPUT="/Users/kevinzhou/Downloads/Single Particle/project-prime/golden-path/ligand_raw.pdb"

mkdir -p "$RUN_BASE"

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="--dry-run"
  echo "[acceptance] dry-run mode" >&2
fi

pass() { echo "PASS: $1" >&2; }
fail() { echo "FAIL: $1" >&2; exit 1; }

assert_envelope_ok() {
  local envelope_path="$1"
  local label="$2"
  python3 - <<PY || fail "$label envelope ok=false (see $envelope_path)"
import json, sys
e = json.load(open("$envelope_path"))
if not e.get("ok"):
    print("  errors:", e.get("errors", []), file=sys.stderr)
    sys.exit(1)
PY
  pass "$label"
}

assert_envelope_fail() {
  local envelope_path="$1"
  local label="$2"
  python3 - <<PY || fail "$label did NOT fail gracefully — envelope ok=true or no errors"
import json, sys
e = json.load(open("$envelope_path"))
errs = e.get("errors", [])
ok_flag = e.get("ok")
if ok_flag is False and errs:
    sys.exit(0)
print("  ok:", ok_flag, "errors:", errs, file=sys.stderr)
sys.exit(1)
PY
  pass "$label"
}

# ---- Case 1: Golden -------------------------------------------------------
echo "[case 1] Golden — benzene from PDB 181L extract" >&2

if [[ ! -f "$GOLDEN_INPUT" ]]; then
  fail "Golden fixture missing at $GOLDEN_INPUT (golden-path was built 2026-05-21)"
fi

GOLDEN_OUT="$RUN_BASE/golden"
rm -rf "$GOLDEN_OUT" && mkdir -p "$GOLDEN_OUT"

python3 "$WRAPPER" \
  --input "$GOLDEN_INPUT" \
  --name BNZ \
  --charge 0 \
  --output-dir "$GOLDEN_OUT" \
  $DRY_RUN > "$GOLDEN_OUT/envelope.json" || true

assert_envelope_ok "$GOLDEN_OUT/envelope.json" "Golden (benzene PDB)"

# Extra check: in non-dry-run, confirm GAFF2 aromatic-carbon typing
if [[ -z "$DRY_RUN" ]]; then
  python3 - "$GOLDEN_OUT/envelope.json" ca ha <<'PY' || fail "Golden mol2 missing GAFF2 aromatic-carbon types (ca/ha expected)"
import json, sys
envelope_path = sys.argv[1]
need = set(sys.argv[2:])
e = json.load(open(envelope_path))
types = set(e.get("validation", {}).get("atom_types", []))
if not need.issubset(types):
    print("  got types:", sorted(types), "missing:", sorted(need - types), file=sys.stderr)
    sys.exit(1)
PY
  pass "Golden GAFF2 atom-type check (ca, ha)"
fi

# ---- Case 2: Unrelated ----------------------------------------------------
echo "[case 2] Unrelated — methane via SMILES 'C'" >&2

UNRELATED_OUT="$RUN_BASE/unrelated"
rm -rf "$UNRELATED_OUT" && mkdir -p "$UNRELATED_OUT"

python3 "$WRAPPER" \
  --input "C" \
  --name MTH \
  --charge 0 \
  --output-dir "$UNRELATED_OUT" \
  $DRY_RUN > "$UNRELATED_OUT/envelope.json" || true

assert_envelope_ok "$UNRELATED_OUT/envelope.json" "Unrelated (methane SMILES)"

if [[ -z "$DRY_RUN" ]]; then
  python3 - "$UNRELATED_OUT/envelope.json" c3 hc <<'PY' || fail "Unrelated mol2 missing GAFF2 sp3-carbon types (c3/hc expected)"
import json, sys
envelope_path = sys.argv[1]
need = set(sys.argv[2:])
e = json.load(open(envelope_path))
types = set(e.get("validation", {}).get("atom_types", []))
if not need.issubset(types):
    print("  got types:", sorted(types), "missing:", sorted(need - types), file=sys.stderr)
    sys.exit(1)
PY
  pass "Unrelated GAFF2 atom-type check (c3, hc)"
fi

# ---- Case 3: Malformed ----------------------------------------------------
# Skipped in --dry-run mode: the wrapper's dry-run plans the command chain
# without executing any subprocess, so content-level malformation cannot be
# detected. The "fails gracefully on bad input" contract is meaningful only
# when the chain actually runs. Full-run executes Case 3 as designed.
if [[ -n "$DRY_RUN" ]]; then
  echo "[case 3] Malformed — SKIPPED in dry-run (planning cannot inspect content)" >&2
  echo "[acceptance] dry-run: cases 1 + 2 passed; case 3 deferred to full run" >&2
  exit 0
fi

echo "[case 3] Malformed — junk PDB; expect ok=false with parseable error" >&2

MALFORMED_OUT="$RUN_BASE/malformed"
rm -rf "$MALFORMED_OUT" && mkdir -p "$MALFORMED_OUT"
MALFORMED_PDB="$MALFORMED_OUT/malformed.pdb"

cat > "$MALFORMED_PDB" <<'EOF'
HEADER    THIS IS NOT A REAL PDB
HETATM XXXX XXX XXX A 999      ABC.DEF GHI.JKL MNO.PQR  1.00 20.00          C
GARBAGE LINE WITH NO STRUCTURE
END
EOF

# Wrapper is expected to exit non-zero AND emit a failure envelope with errors[].
python3 "$WRAPPER" \
  --input "$MALFORMED_PDB" \
  --name BAD \
  --charge 0 \
  --output-dir "$MALFORMED_OUT" \
  > "$MALFORMED_OUT/envelope.json" 2> "$MALFORMED_OUT/wrapper.stderr" || true

assert_envelope_fail "$MALFORMED_OUT/envelope.json" "Malformed (graceful failure)"

echo "[acceptance] all three cases passed" >&2

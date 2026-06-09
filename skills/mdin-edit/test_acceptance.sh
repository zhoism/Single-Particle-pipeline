#!/usr/bin/env bash
# mdin-edit acceptance test.
#
# Verifies the deterministic edit engine against the advisor's demo mdin set.
# EVERY case runs on a FRESH copy of the demo (the originals are never touched)
# and asserts the actual edited FILE BYTES — not just envelope ok:true (the
# antechamber-aromatic-kekulize-bug lesson: never trust ok:true).
#
# Cases:
#   0. Malformed   — an .in with no &cntrl close → graceful ok:false, untouched.
#   1. Golden      — dt → 0.001 in heat-1 (the advisor's example #1).
#   2. Idempotency — re-running the golden edit is byte-identical; no-op detected.
#   3. Out-of-bounds — dt = 0.01 rejected; file unchanged (no half-write).
#   4. Wrong-param — dt on min1 rejected; dt still absent (never appends).
#   5. Ext-A — temp0 → 310 group:third-onward (heat-3 &wt value2 coupled; value1
#              preserved; relax/prod get no &wt; heat-1/press-1 untouched; the
#              heat-3 temp0/&wt-mismatch WARN is gone). Plus a coupling-write
#              sub-case (temp0 → 305 on heat-3 alone → value2 also 305.0).
#   6. Ext-B — cut → 7.0 (ok:true despite the validator's <8 Å FAIL; deliberate WARN).
#   7. Ext-C — restraint_wt 5.0 → 1.0 on press-1 (mask line untouched); negative
#              single-stage on relax (ntr=0) rejected; group:all skips ntr=0 files.
#
# All cases must pass before the skill flips BUILT → COMPLETE in the manifest.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
VALIDATOR="$SKILL_DIR/scripts/check_amber_vendored.py"
RUN_BASE="$SKILL_DIR/test-runs"
DEMO_DIR="${MDIN_DEMO_DIR:-/Users/kevinzhou/Downloads/Single Particle/Single Particle/phase3-explicit-solvent-md}"

mkdir -p "$RUN_BASE"

pass() { echo "PASS: $1" >&2; }
fail() { echo "FAIL: $1" >&2; exit 1; }

[[ -d "$DEMO_DIR" ]] || fail "demo dir not found: $DEMO_DIR (set MDIN_DEMO_DIR)"

# Fresh scratch copy of the demo .in files. Echoes the scratch path.
fresh() {
  local name="$1" sc="$RUN_BASE/$1"
  rm -rf "$sc" && mkdir -p "$sc"
  cp "$DEMO_DIR"/*.in "$sc"/
  echo "$sc"
}

# assert_ok <envelope.json> <label>
assert_ok() {
  python3 -c "import json,sys; e=json.load(open('$1')); sys.exit(0 if e.get('ok') else 1)" \
    || { python3 -c "import json;print(' errors:',json.load(open('$1')).get('errors'))" >&2; fail "$2 (ok=false)"; }
}
# assert_fail <envelope.json> <label> <expected-code-substring>
assert_fail() {
  python3 -c "
import json,sys
e=json.load(open('$1'))
errs=e.get('errors',[])
ok = (e.get('ok') is False) and any('$3' in x for x in errs)
sys.exit(0 if ok else 1)" || fail "$2 (expected ok:false with '$3'; got $(cat "$1"))"
}

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  # Pure-Python skill, no toolchain — dry-run just proves planning + no-write.
  echo "[acceptance] dry-run mode (golden plan only)" >&2
  SC="$(fresh dryrun)"
  cp "$SC/heat-1.in" "$SC/.orig"
  python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.001 --dry-run \
    > "$SC/env.json"
  assert_ok "$SC/env.json" "dry-run golden plan"
  python3 -c "import json,sys;e=json.load(open('$SC/env.json'));sys.exit(0 if e['dry_run'] else 1)" \
    || fail "dry-run flag not set"
  diff "$SC/.orig" "$SC/heat-1.in" >/dev/null || fail "dry-run wrote the file"
  pass "dry-run plans without writing"
  echo "[acceptance] dry-run mode passed" >&2
  exit 0
fi

# ---- Case 0: Malformed ----------------------------------------------------
echo "[case 0] Malformed — .in with no &cntrl close → graceful ok:false" >&2
SC="$(fresh malformed)"
printf ' &cntrl\n  dt = 0.002,\n' > "$SC/heat-1.in"   # never closed
cp "$SC/heat-1.in" "$SC/.orig"
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.001 \
  > "$SC/env.json" 2>/dev/null || true
assert_fail "$SC/env.json" "Malformed" "NAMELIST_NOT_FOUND"
diff "$SC/.orig" "$SC/heat-1.in" >/dev/null || fail "Malformed: file was modified"
pass "Malformed (graceful, untouched)"

# ---- Case 1: Golden -------------------------------------------------------
echo "[case 1] Golden — dt → 0.001 in heat-1" >&2
SC="$(fresh golden)"
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.001 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "Golden"
grep -qx '  dt = 0.001,' "$SC/heat-1.in" || fail "Golden: heat-1 dt line not exact"
# exactly one line differs from the original (diff exits 1 when files differ → || true)
n=$(diff "$DEMO_DIR/heat-1.in" "$SC/heat-1.in" | grep -c '^[<>]' || true)
[[ "$n" -eq 2 ]] || fail "Golden: expected exactly 1 changed line, diff shows $n change-lines"
pass "Golden (dt=0.001, single-line change)"

# ---- Case 2: Idempotency --------------------------------------------------
echo "[case 2] Idempotency — re-run is byte-identical, no-op detected" >&2
SC="$(fresh idempotency)"
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.001 >/dev/null 2>&1
cp "$SC/heat-1.in" "$SC/.snap"
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.001 \
  > "$SC/env2.json" 2>/dev/null
assert_ok "$SC/env2.json" "Idempotency re-run"
cmp -s "$SC/.snap" "$SC/heat-1.in" || fail "Idempotency: re-run changed bytes"
python3 -c "import json,sys;e=json.load(open('$SC/env2.json'));sys.exit(0 if e['outputs']['files'][0]['status']=='unchanged' else 1)" \
  || fail "Idempotency: re-run not reported as 'unchanged'"
[[ "$(tail -c1 "$SC/heat-1.in" | xxd -p)" == "0a" ]] || fail "Idempotency: trailing newline drifted"
pass "Idempotency (byte-identical, no-op, newline intact)"

# ---- Case 3: Out-of-bounds ------------------------------------------------
echo "[case 3] Out-of-bounds — dt = 0.01 rejected, file unchanged" >&2
SC="$(fresh oob)"
cp "$SC/heat-1.in" "$SC/.orig"
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.01 \
  > "$SC/env.json" 2>/dev/null || true
assert_fail "$SC/env.json" "Out-of-bounds" "OUT_OF_BOUNDS"
diff "$SC/.orig" "$SC/heat-1.in" >/dev/null || fail "Out-of-bounds: file was modified (half-write)"
pass "Out-of-bounds (rejected, untouched)"

# ---- Case 4: Wrong-param-for-stage ---------------------------------------
echo "[case 4] Wrong-param — dt on min1 rejected, no append" >&2
SC="$(fresh wrongparam)"
cp "$SC/min1.in" "$SC/.orig"
python3 "$WRAPPER" --md-dir "$SC" --stage min1 --param dt --value 0.001 \
  > "$SC/env.json" 2>/dev/null || true
assert_fail "$SC/env.json" "Wrong-param" "PARAM_NOT_FOUND"
grep -q 'dt[[:space:]]*=' "$SC/min1.in" && fail "Wrong-param: dt was appended to min1" || true
diff "$SC/.orig" "$SC/min1.in" >/dev/null || fail "Wrong-param: min1 modified"
pass "Wrong-param (rejected, no append)"

# ---- Case 5: Ext-A — temp0 → 310 group:third-onward ----------------------
echo "[case 5] Ext-A — temp0 → 310 group:third-onward (+ &wt coupling)" >&2
SC="$(fresh extA)"
python3 "$WRAPPER" --md-dir "$SC" --stage group:third-onward --param temp0 --value 310 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "Ext-A"
grep -qx '  temp0 = 310.0,' "$SC/heat-3.in"  || fail "Ext-A: heat-3 temp0 not 310"
grep -qx '  value1 = 200.0, value2 = 310.0,' "$SC/heat-3.in" || fail "Ext-A: heat-3 &wt line wrong (value1 must stay 200.0, value2 → 310.0)"
grep -qx '  temp0 = 310.0,' "$SC/press-3.in" || fail "Ext-A: press-3 temp0 not 310"
grep -qx '  temp0 = 310.0,' "$SC/relax.in"   || fail "Ext-A: relax temp0 not 310"
grep -qx '  temp0 = 310.0,' "$SC/prod.in"    || fail "Ext-A: prod temp0 not 310"
grep -q 'value2' "$SC/relax.in" && fail "Ext-A: spurious &wt value2 created in relax" || true
# Stages NOT in the group must be untouched.
grep -qx '  temp0 = 100.0,' "$SC/heat-1.in"  || fail "Ext-A: heat-1 temp0 changed (should be 100)"
grep -qx '  temp0 = 200.0,' "$SC/heat-2.in"  || fail "Ext-A: heat-2 temp0 changed (should be 200)"
grep -qx '  temp0 = 100.0,' "$SC/press-1.in" || fail "Ext-A: press-1 temp0 changed (should be 100)"
# The heat-3 temp0/&wt mismatch WARN must be gone after the coupled edit.
python3 "$VALIDATOR" "$SC/heat-3.in" 2>/dev/null | grep -qi 'temp0 / &wt mismatch' \
  && fail "Ext-A: heat-3 temp0/&wt mismatch WARN still present" || true
python3 "$VALIDATOR" "$SC/heat-3.in" 2>/dev/null | grep -qi 'temp0 / &wt coherent' \
  || fail "Ext-A: heat-3 temp0/&wt not reported coherent"
pass "Ext-A (group temp0 + &wt coupling + bug aligned + neighbors untouched)"

echo "[case 5b] Ext-A coupling-write — temp0 → 305 on heat-3 alone → value2 305.0" >&2
SC="$(fresh extA_couple)"
python3 "$WRAPPER" --md-dir "$SC" --stage heat-3 --param temp0 --value 305 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "Ext-A coupling-write"
grep -qx '  temp0 = 305.0,' "$SC/heat-3.in" || fail "Ext-A 5b: temp0 not 305"
grep -qx '  value1 = 200.0, value2 = 305.0,' "$SC/heat-3.in" || fail "Ext-A 5b: &wt value2 not coupled to 305 (value1 must stay 200)"
pass "Ext-A 5b (coupling actually rewrites value2)"

# ---- Case 6: Ext-B — cut → 7.0 -------------------------------------------
echo "[case 6] Ext-B — cut → 7.0 (accepted with deliberate WARN)" >&2
SC="$(fresh extB)"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --param cut --value 7.0 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "Ext-B"
grep -qx '  cut = 7.0,' "$SC/relax.in" || fail "Ext-B: relax cut not 7.0"
python3 -c "
import json,sys
e=json.load(open('$SC/env.json'))
f=e['outputs']['files'][0]
warned = any('8' in w for w in f.get('warnings',[]))
v=e['validation']['per_file']['relax.in']
deliberate = any(x['rule']=='cut out of range' and x['deliberate'] for x in v['findings'])
sys.exit(0 if (warned and deliberate and e['ok']) else 1)" \
  || fail "Ext-B: expected ok:true + editor cut-floor warning + deliberate downgrade"
pass "Ext-B (cut=7.0 accepted, advisory WARN, not blocked)"

# ---- Case 7: Ext-C — restraint_wt --------------------------------------
echo "[case 7] Ext-C — restraint_wt 5.0 → 1.0 on press-1 (mask untouched)" >&2
SC="$(fresh extC)"
python3 "$WRAPPER" --md-dir "$SC" --stage press-1 --param restraint_wt --value 1.0 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "Ext-C press-1"
grep -qx '  restraint_wt = 1.0,' "$SC/press-1.in" || fail "Ext-C: press-1 restraint_wt not 1.0"
grep -qx '  restraintmask = "!:WAT,Cl-,K+,Na+ & !@H=",' "$SC/press-1.in" \
  || fail "Ext-C: press-1 restraintmask line was disturbed"
pass "Ext-C (restraint_wt edited, mask line intact)"

echo "[case 7b] Ext-C negative — restraint_wt on relax (ntr=0) single → rejected" >&2
SC="$(fresh extC_neg)"
cp "$SC/relax.in" "$SC/.orig"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --param restraint_wt --value 1.0 \
  > "$SC/env.json" 2>/dev/null || true
assert_fail "$SC/env.json" "Ext-C negative" "SKIPPED_RESTRAINTS_OFF"
diff "$SC/.orig" "$SC/relax.in" >/dev/null || fail "Ext-C neg: relax modified"
pass "Ext-C negative (ntr=0 single-stage rejected, untouched)"

echo "[case 7c] Ext-C group — restraint_wt group:all skips ntr=0 files, ok:true" >&2
SC="$(fresh extC_group)"
python3 "$WRAPPER" --md-dir "$SC" --stage group:all --param restraint_wt --value 1.0 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "Ext-C group"
python3 -c "
import json,sys
e=json.load(open('$SC/env.json'))
st={f['file']:f['status'] for f in e['outputs']['files']}
skipped={'min2.in','relax.in','prod.in'}
edited={'min1.in','heat-1.in','heat-2.in','heat-3.in','press-1.in','press-2.in','press-3.in'}
ok = all(st[f]=='skipped' for f in skipped) and all(st[f]=='edited' for f in edited)
sys.exit(0 if ok else 1)" || fail "Ext-C group: skip/edit partition wrong"
pass "Ext-C group (ntr=0 skipped, ntr=1 edited, batch ok)"

echo "[acceptance] all cases passed" >&2

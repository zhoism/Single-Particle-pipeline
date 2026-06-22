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
# Constant-T stages: tempi must be coupled to temp0 (advisor feedback). Heat-3's
# tempi is the ramp START and must stay 200.
grep -qx '  tempi = 310.0,' "$SC/relax.in"   || fail "Ext-A: relax tempi not coupled to 310"
grep -qx '  tempi = 310.0,' "$SC/prod.in"    || fail "Ext-A: prod tempi not coupled to 310"
grep -qx '  tempi = 200.0,' "$SC/heat-3.in"  || fail "Ext-A: heat-3 tempi (ramp start) was wrongly changed"
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

# ---- Case 8: dt cap depends on SHAKE (advisor feedback) -------------------
echo "[case 8] dt cap is SHAKE-aware (0.002 with SHAKE, 0.001 without)" >&2
SC="$(fresh dtshake)"
# Synthetic no-SHAKE stage (ntc=1, ntf=1): dt=0.002 must be rejected.
printf ' &cntrl\n  imin = 0,\n  nstlim = 50000,\n  dt = 0.001,\n  cut = 9.0,\n  ntc = 1,\n  ntf = 1,\n  temp0 = 300.0,\n  ntpr = 2500,\n  ntwx = 0,\n /\n' > "$SC/heat-1.in"
cp "$SC/heat-1.in" "$SC/.orig"
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.002 \
  > "$SC/env.json" 2>/dev/null || true
assert_fail "$SC/env.json" "no-SHAKE dt=0.002" "OUT_OF_BOUNDS"
diff "$SC/.orig" "$SC/heat-1.in" >/dev/null || fail "Case 8: no-SHAKE file modified on reject"
# 0.001 is accepted under no-SHAKE.
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param dt --value 0.0008 \
  > "$SC/env2.json" 2>/dev/null
assert_ok "$SC/env2.json" "no-SHAKE dt=0.0008"
# SHAKE-on demo stage still accepts 0.002.
SC2="$(fresh dtshake_on)"
python3 "$WRAPPER" --md-dir "$SC2" --stage heat-1 --param dt --value 0.002 \
  > "$SC2/env.json" 2>/dev/null
assert_ok "$SC2/env.json" "SHAKE-on dt=0.002"
pass "Case 8 (dt cap context-aware on ntc/ntf)"

# ---- Case 9: high-temperature dt advisory --------------------------------
echo "[case 9] hot-dt advisory when temp0>300 with dt at the cap" >&2
SC="$(fresh hotdt)"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --param temp0 --value 310 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "hot-dt"
python3 -c "
import json,sys
e=json.load(open('$SC/env.json'))
f=e['outputs']['files'][0]
w=' '.join(f.get('warnings',[]))
sys.exit(0 if ('300' in w and 'dt' in w) else 1)" \
  || fail "Case 9: expected a hot-dt advisory mentioning dt and 300"
pass "Case 9 (hot-dt advisory fires above 300 K)"

# ---- Case 10: enable-restraints with mask INSERT (relax) -----------------
echo "[case 10] enable-restraints on relax (inserts a restraintmask line)" >&2
SC="$(fresh enable_insert)"
before=$(wc -l < "$SC/relax.in")
python3 "$WRAPPER" --md-dir "$SC" --stage relax --enable-restraints \
  --restraint-wt 2.5 --restraintmask '!:WAT,Cl-,K+,Na+ & !@H=' \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "enable insert"
grep -qx '  ntr = 1,' "$SC/relax.in"           || fail "Case 10: ntr not 1"
grep -qx '  restraint_wt = 2.5,' "$SC/relax.in" || fail "Case 10: restraint_wt not 2.5"
grep -qx '  restraintmask = "!:WAT,Cl-,K+,Na+ & !@H=",' "$SC/relax.in" \
  || fail "Case 10: restraintmask line not inserted correctly"
after=$(wc -l < "$SC/relax.in")
[[ $((after - before)) -eq 1 ]] || fail "Case 10: expected exactly +1 line, got $((after-before))"
# idempotent: re-run byte-identical, reports unchanged
cp "$SC/relax.in" "$SC/.snap"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --enable-restraints \
  --restraint-wt 2.5 --restraintmask '!:WAT,Cl-,K+,Na+ & !@H=' \
  > "$SC/env2.json" 2>/dev/null
cmp -s "$SC/.snap" "$SC/relax.in" || fail "Case 10: re-run changed bytes (not idempotent)"
python3 -c "import json,sys;e=json.load(open('$SC/env2.json'));sys.exit(0 if e['outputs']['files'][0]['status']=='unchanged' else 1)" \
  || fail "Case 10: idempotent re-run not 'unchanged'"
pass "Case 10 (enable inserts mask, idempotent)"

# ---- Case 11: enable-restraints in place (press-1, mask present) ----------
echo "[case 11] enable-restraints on press-1 (edits mask in place, no insert)" >&2
SC="$(fresh enable_inplace)"
before=$(wc -l < "$SC/press-1.in")
python3 "$WRAPPER" --md-dir "$SC" --stage press-1 --enable-restraints \
  --restraint-wt 4.0 --restraintmask '!:WAT & !@H=' \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "enable in-place"
grep -qx '  restraint_wt = 4.0,' "$SC/press-1.in" || fail "Case 11: restraint_wt not 4.0"
grep -qx '  restraintmask = "!:WAT & !@H=",' "$SC/press-1.in" || fail "Case 11: mask not edited in place"
after=$(wc -l < "$SC/press-1.in")
[[ $((after - before)) -eq 0 ]] || fail "Case 11: line count changed (should edit in place)"
pass "Case 11 (enable edits mask in place, no append)"

# ---- Case 12: disable-restraints (press-1) -------------------------------
echo "[case 12] disable-restraints on press-1 (ntr→0, mask line left in place)" >&2
SC="$(fresh disable)"
python3 "$WRAPPER" --md-dir "$SC" --stage press-1 --disable-restraints \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "disable"
grep -qx '  ntr = 0,' "$SC/press-1.in" || fail "Case 12: ntr not 0"
grep -qx '  restraintmask = "!:WAT,Cl-,K+,Na+ & !@H=",' "$SC/press-1.in" \
  || fail "Case 12: mask line should be left in place on disable"
# already-off stage → unchanged
python3 "$WRAPPER" --md-dir "$SC" --stage relax --disable-restraints \
  > "$SC/env2.json" 2>/dev/null
python3 -c "import json,sys;e=json.load(open('$SC/env2.json'));sys.exit(0 if e['outputs']['files'][0]['status']=='unchanged' else 1)" \
  || fail "Case 12: disable on already-off relax not 'unchanged'"
pass "Case 12 (disable sets ntr=0, idempotent on already-off)"

# ---- Case 13: restraint-mode negative cases ------------------------------
echo "[case 13] enable/disable negative cases" >&2
SC="$(fresh restr_neg)"
cp "$SC/relax.in" "$SC/.orig"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --enable-restraints --restraint-wt 2.5 \
  > "$SC/e1.json" 2>/dev/null || true
assert_fail "$SC/e1.json" "missing mask" "MISSING_REQUIRED_ARG"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --enable-restraints --restraint-wt 2.5 --restraintmask '   ' \
  > "$SC/e2.json" 2>/dev/null || true
assert_fail "$SC/e2.json" "empty mask" "INVALID_MASK"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --enable-restraints --restraint-wt -1 --restraintmask '!:WAT' \
  > "$SC/e3.json" 2>/dev/null || true
assert_fail "$SC/e3.json" "negative wt" "OUT_OF_BOUNDS"
# A '/' or quote in the mask would corrupt the namelist (terminator / quote chars).
python3 "$WRAPPER" --md-dir "$SC" --stage relax --enable-restraints --restraint-wt 2.5 --restraintmask ':1-10/CA' \
  > "$SC/e5.json" 2>/dev/null || true
assert_fail "$SC/e5.json" "slash mask" "INVALID_MASK"
python3 "$WRAPPER" --md-dir "$SC" --stage relax --param dt --value 0.001 --disable-restraints \
  > "$SC/e4.json" 2>/dev/null || true
assert_fail "$SC/e4.json" "mode conflict" "MODE_CONFLICT"
diff "$SC/.orig" "$SC/relax.in" >/dev/null || fail "Case 13: relax modified by a rejected op"
pass "Case 13 (missing mask / empty mask / negative wt / mode conflict all rejected, untouched)"

# ---- Case 14: nstlim output-schedule warning -----------------------------
echo "[case 14] nstlim schedule: sparse sampling warning + schedule in envelope" >&2
SC="$(fresh nstlim_sched)"
python3 "$WRAPPER" --md-dir "$SC" --stage prod --param nstlim --value 15000 \
  > "$SC/env.json" 2>/dev/null
assert_ok "$SC/env.json" "nstlim schedule"
grep -qx '  nstlim = 15000,' "$SC/prod.in" || fail "Case 14: prod nstlim not 15000"
python3 -c "
import json,sys
e=json.load(open('$SC/env.json'))
f=e['outputs']['files'][0]
sched=f.get('output_schedule') or {}
w=' '.join(f.get('warnings',[]))
ok = (sched.get('trajectory_frames')==1) and ('sparse' in w or 'frame' in w)
sys.exit(0 if ok else 1)" \
  || fail "Case 14: expected output_schedule (1 frame) + a sparse-sampling warning"
# heat stage with ntwx=0 → no frame warning (trajectory off by design)
python3 "$WRAPPER" --md-dir "$SC" --stage heat-1 --param nstlim --value 25000 \
  > "$SC/env2.json" 2>/dev/null
python3 -c "
import json,sys
e=json.load(open('$SC/env2.json'))
f=e['outputs']['files'][0]
w=' '.join(f.get('warnings',[]))
sched=f.get('output_schedule') or {}
sys.exit(0 if (sched.get('trajectory_off') is True and 'frame' not in w) else 1)" \
  || fail "Case 14: ntwx=0 stage should report trajectory_off with no frame warning"
pass "Case 14 (nstlim schedule + sparse warn; ntwx=0 stays silent)"

echo "[acceptance] all cases passed" >&2

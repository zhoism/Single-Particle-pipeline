#!/usr/bin/env bash
# Verifies the run_happy_path Stage-4b recovery hook (scripts/recover_hook.sh):
# the RECOVER gate holds, it is a no-op on a healthy MD, and it dispatches
# amber-recover on a REAL crashed md_dir. Proves the wiring without a 15-min run.
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_PRIME="$(cd "$SKILL_DIR/../.." && pwd)"
HOOK="$PROJECT_PRIME/scripts/recover_hook.sh"
SRC="$PROJECT_PRIME/regression-1L2Y/md"
RUN="$SKILL_DIR/test-runs/wiring"; rm -rf "$RUN"; mkdir -p "$RUN"
set +u; source "$PROJECT_PRIME/scripts/env.sh" >/dev/null 2>&1; set -u
pass(){ echo "PASS: $1" >&2; }
fail(){ echo "FAIL: $1" >&2; exit 1; }

# A: completed CLEAN MD (real clean mdout) -> detector says no crash -> no-op
A="$RUN/ok"; mkdir -p "$A/md"
cp "$SKILL_DIR/tests/fixtures/clean_production.out" "$A/md/product.out"
echo '{"ok":true,"errors":[]}' > "$A/s4.json"
RECOVER=1 bash "$HOOK" "$A/s4.json" "$A/md" t-ok >/dev/null 2>&1
[ ! -f "$A/s4b.json" ] || fail "A: hook ran on a healthy MD"
pass "A: clean MD -> detector no-op"

# C: crash present but RECOVER unset -> gate holds, no-op
C="$RUN/disabled"; mkdir -p "$C/md"
echo '{"ok":false,"errors":["MD_CRASH[heat]: SHAKE_FAILED"]}' > "$C/s4.json"
RECOVER=0 bash "$HOOK" "$C/s4.json" "$C/md" t-dis >/dev/null 2>&1
[ ! -f "$C/s4b.json" ] || fail "C: hook ran with RECOVER=0"
pass "C: RECOVER!=1 -> hook no-op (gate holds)"

# B: crash + RECOVER=1 -> dispatch amber-recover on a REAL crashed md_dir
B="$RUN/crash"; mkdir -p "$B/md"
cp "$SRC/comp_oct.top" "$B/md/"; cp "$SRC/comp_oct.crd" "$B/md/"; cp "$SRC/min3.rst" "$B/md/"
cp "$SKILL_DIR/tests/fixtures/crash_unmin_shake_overflow.out" "$B/md/heat.out"
cat > "$B/md/heat.in" <<'EOF'
heat
 &cntrl
  imin=0, irest=0, ntx=1, nstlim=800, dt=0.002,
  ntc=2, ntf=2, cut=9.0, ntb=1, ntp=0,
  ntt=3, gamma_ln=2.0, ig=-1, tempi=0.0, temp0=300.0,
  ntr=1, restraint_wt=2.0, restraintmask='!:WAT,Na+,Cl-',
  ntpr=200, ntwx=0, ntwr=800,
 /
EOF
echo '{"ok":false,"errors":["MD_CRASH[heat]: SHAKE_FAILED,VLIMIT_EXCEEDED"]}' > "$B/s4.json"
RECOVER=1 bash "$HOOK" "$B/s4.json" "$B/md" t-crash 2>"$B/hook.err" || true
[ -f "$B/s4b.json" ] || { cat "$B/hook.err" >&2; fail "B: hook did not dispatch recovery"; }
python3 - "$B/s4b.json" <<'PY' || fail "B: recovery envelope not ok / no tier"
import json,sys
e=json.load(open(sys.argv[1]))
assert e["ok"] is True, e.get("errors")
assert e["outputs"]["tier"] in (1,2), e["outputs"].get("tier")
PY
pass "B: real crash + RECOVER=1 -> amber-recover dispatched & recovered (tier $(python3 -c "import json;print(json.load(open('$B/s4b.json'))['outputs']['tier'])"))"

# D: amber-md-run reported ok:TRUE, but the mdout is silent NaN garbage (banner + rst).
#    The STRONG deterministic detector (the M2 fix) catches it DESPITE the upstream ok
#    flag — the silent-garbage class amber-recover exists to catch is now reachable.
D="$RUN/silent_nan"; mkdir -p "$D/md"
cp "$SRC/comp_oct.top" "$D/md/"; cp "$SRC/comp_oct.crd" "$D/md/"; cp "$SRC/min3.rst" "$D/md/"
cp "$SKILL_DIR/tests/fixtures/crash_nan_silent_finished.out" "$D/md/heat.out"
cp "$B/md/heat.in" "$D/md/heat.in"
echo '{"ok":true,"errors":[]}' > "$D/s4.json"   # amber-md-run wrongly called it healthy
RECOVER=1 bash "$HOOK" "$D/s4.json" "$D/md" t-nan 2>"$D/hook.err" || true
[ -f "$D/s4b.json" ] || { cat "$D/hook.err" >&2; fail "D: detector missed the silent-NaN run (M2 hole reopened)"; }
pass "D: silent-NaN run (amber-md-run ok:true) -> detector caught it + dispatched recovery"

echo "[wiring] all 4 scenarios passed" >&2

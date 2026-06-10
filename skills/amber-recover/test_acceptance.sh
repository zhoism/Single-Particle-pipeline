#!/usr/bin/env bash
# amber-recover acceptance test — exercises the FULL bounded recovery loop with
# REAL pmemd on the 1L2Y fixture (genuine instabilities, not mocked strings).
#
#   1. Tier-2 golden  — a SANE stage (dt=2fs, SHAKE on, check_amber-clean) crashes
#                       on an un-minimized geometry (real SHAKE/overflow crash);
#                       Tier 1 re-crashes -> Tier 2 stabilize(SHAKE-off,tiny dt)
#                       + restore -> reaches normal termination. ok:true tier=2.
#   2. Tier-1 golden  — a transient/killed stage (real mdout truncated before its
#                       banner, restart removed) over a GOOD checkpoint; resume
#                       as-is completes. ok:true tier=1.
#   3. HALT (bounded) — the Tier-2 crash with --dt-floor 0.002: the only in-bounds
#                       SHAKE-off fix needs dt<=0.001, below the floor -> HALT with
#                       a structured needs_human block. ok:false, NOT a crash.
#   4. Malformed      — missing --md-dir -> ok:false INVALID_INPUT (graceful).
#   5. No-failure     — a CLEAN finished run -> ok:false NO_FAILURE_DETECTED
#                       (the skill refuses to fabricate a recovery).
#   6. Dry-run        — plan + bounds-check the mutated namelist, no pmemd.
#
# Needs pmemd (~/Downloads/pmemd26) + AmberTools. Run with the AMBER env sourced
# (the harness wraps env.sh in set +u/set -u; amber.sh trips set -u otherwise).

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_PRIME="$(cd "$SKILL_DIR/../.." && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
SRC="$PROJECT_PRIME/regression-1L2Y/md"        # reusable topology + good checkpoints
RUN_BASE="$SKILL_DIR/test-runs"
rm -rf "$RUN_BASE"; mkdir -p "$RUN_BASE"

# AMBER toolchain (guard env.sh against set -u; it sources conda amber.sh)
set +u; source "$PROJECT_PRIME/scripts/env.sh" >/dev/null 2>&1; set -u
PY="$(command -v python3)"

pass() { echo "PASS: $1" >&2; }
fail() { echo "FAIL: $1" >&2; exit 1; }

jget() { "$PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('$2',''))" "$1"; }
assert_ok()   { [ "$(jget "$1" ok)" = "True" ] || { "$PY" -c "import json;print(json.load(open('$1')).get('errors'))"; fail "$2"; }; pass "$2"; }
assert_fail() { [ "$(jget "$1" ok)" = "False" ] || fail "$2 (expected ok:false)"; pass "$2"; }

# the sane, check_amber-clean heat stage used as the crashed fixture
write_heat_in() {
  cat > "$1" <<'EOF'
heat: 0->300 K NVT Langevin, weak solute restraint (sane 2fs + SHAKE)
 &cntrl
  imin=0, irest=0, ntx=1,
  nstlim=800, dt=0.002,
  ntc=2, ntf=2, cut=9.0,
  ntb=1, ntp=0,
  ntt=3, gamma_ln=2.0, ig=-1,
  tempi=0.0, temp0=300.0,
  ntr=1, restraint_wt=2.0, restraintmask='!:WAT,Na+,Cl-',
  ntpr=200, ntwx=0, ntwr=800,
 /
EOF
}

stage_inputs() {   # $1 = dest md_dir
  mkdir -p "$1"
  cp "$SRC/comp_oct.top" "$1/comp_oct.top"
  cp "$SRC/comp_oct.crd" "$1/comp_oct.crd"
  cp "$SRC/min3.rst"     "$1/min3.rst"
  write_heat_in "$1/heat.in"
}

# ---- Case 1: Tier-2 golden (bad geometry, sane params) ---------------------
echo "[case 1] Tier-2: induce a real crash from un-minimized coords, recover" >&2
C1="$RUN_BASE/tier2"; stage_inputs "$C1"
( cd "$C1" && pmemd -O -i heat.in -o heat.out -p comp_oct.top -c comp_oct.crd \
    -ref comp_oct.crd -r heat.rst >heat.log 2>&1 || true )   # genuine crash
"$PY" "$WRAPPER" --md-dir "$C1" --stage heat --checkpoint comp_oct.crd \
  --stabilize-steps 1500 > "$C1/recover.json" 2> "$C1/recover.err" || true
assert_ok "$C1/recover.json" "Tier-2 recovery returned ok"
"$PY" - "$C1/recover.json" <<'PY' || fail "Tier-2: tier / final_rst / bounds / signatures"
import json,sys,os
e=json.load(open(sys.argv[1]))
assert e["outputs"]["tier"]==2, "expected tier=2, got %s" % e["outputs"].get("tier")
assert e["outputs"]["recovered"] is True
assert os.path.exists(e["outputs"]["final_rst"]), "final_rst missing"
b=e["validation"]["bounds"]; assert b["all_pass"] is True, "a mutated namelist breached bounds"
assert e["validation"]["detection"]["classification"]=="INSTABILITY"
sig=e["validation"]["detection"]["signatures"]
assert "SHAKE_FAILED" in sig or "COORD_OVERFLOW" in sig, sig
PY
pass "Tier-2: recovered at tier=2, bounds all-pass, real crash signatures present"

# ---- Case 2: Tier-1 golden (transient / killed) ----------------------------
echo "[case 2] Tier-1: a killed stage over a GOOD checkpoint resumes as-is" >&2
C2="$RUN_BASE/tier1"; stage_inputs "$C2"
# Produce a genuine (complete) heat.out from the GOOD min3.rst, then truncate it
# before the termination banner + drop the restart = the on-disk trace of a SIGKILL.
( cd "$C2" && pmemd -O -i heat.in -o heat.full -p comp_oct.top -c min3.rst \
    -ref min3.rst -r heat.rst >heat.log 2>&1 || true )
"$PY" - "$C2/heat.full" "$C2/heat.out" <<'PY'
import re,sys
src,dst=sys.argv[1],sys.argv[2]
out=[]
for ln in open(src):
    if re.search(r"Total wall time|Final Performance|FINAL RESULTS|Maximum number", ln): break
    out.append(ln.rstrip("\n"))
open(dst,"w").write("\n".join(out[:-3])+"\n")   # genuine partial, no banner
PY
rm -f "$C2/heat.rst" "$C2/heat.full"
"$PY" "$WRAPPER" --md-dir "$C2" --stage heat --checkpoint min3.rst \
  > "$C2/recover.json" 2> "$C2/recover.err" || true
assert_ok "$C2/recover.json" "Tier-1 recovery returned ok"
"$PY" - "$C2/recover.json" <<'PY' || fail "Tier-1: expected tier=1 resume-as-is"
import json,sys,os
e=json.load(open(sys.argv[1]))
assert e["outputs"]["tier"]==1, e["outputs"].get("tier")
assert e["outputs"]["recovered"] is True
assert os.path.exists(e["outputs"]["final_rst"])
assert e["validation"]["detection"]["classification"]=="INCOMPLETE", e["validation"]["detection"]
PY
pass "Tier-1: incomplete stage resumed as-is (tier=1), no mutation"

# ---- Case 3: HALT (bounded) ------------------------------------------------
echo "[case 3] HALT: same crash, --dt-floor 0.002 forbids the only fix" >&2
C3="$RUN_BASE/halt"; stage_inputs "$C3"
( cd "$C3" && pmemd -O -i heat.in -o heat.out -p comp_oct.top -c comp_oct.crd \
    -ref comp_oct.crd -r heat.rst >heat.log 2>&1 || true )
"$PY" "$WRAPPER" --md-dir "$C3" --stage heat --checkpoint comp_oct.crd \
  --dt-floor 0.002 > "$C3/recover.json" 2> "$C3/recover.err" || true
assert_fail "$C3/recover.json" "HALT returned ok:false (graceful, not a crash)"
"$PY" - "$C3/recover.json" <<'PY' || fail "HALT: structured needs_human / DT_FLOOR_REACHED"
import json,sys
e=json.load(open(sys.argv[1]))
assert any("RECOVERY_HALTED" in x for x in e["errors"]), e["errors"]
nh=e["outputs"]["needs_human"]; assert nh["reason"]=="DT_FLOOR_REACHED", nh["reason"]
assert "recommendation" in nh and nh["recommendation"]
# Tier 1 was tried first (the safe move), THEN it halted at Tier 2
tiers=[a["tier"] for a in e["outputs"]["attempts"]]
assert 1 in tiers, tiers
PY
pass "HALT: bounded -> ok:false needs_human DT_FLOOR_REACHED, Tier 1 tried first"

# ---- Case 4: Malformed -----------------------------------------------------
echo "[case 4] Malformed — nonexistent --md-dir" >&2
"$PY" "$WRAPPER" --md-dir "$RUN_BASE/nope" --stage heat \
  > "$RUN_BASE/malformed.json" 2>/dev/null || true
assert_fail "$RUN_BASE/malformed.json" "Malformed (graceful ok:false)"

# ---- Case 5: No-failure (honesty gate) -------------------------------------
echo "[case 5] No-failure — a CLEAN finished run must NOT be 'recovered'" >&2
C5="$RUN_BASE/nofail"; mkdir -p "$C5"
cp "$SKILL_DIR/tests/fixtures/clean_production.out" "$C5/product.out"
write_heat_in "$C5/product.in"
"$PY" "$WRAPPER" --md-dir "$C5" --stage product > "$C5/recover.json" 2>/dev/null || true
assert_fail "$C5/recover.json" "No-failure returns ok:false"
[ -n "$(grep -o NO_FAILURE_DETECTED "$C5/recover.json")" ] || fail "No-failure: expected NO_FAILURE_DETECTED"
pass "No-failure: NO_FAILURE_DETECTED (refuses to fabricate recovery)"

# ---- Case 6: Dry-run plan + bounds -----------------------------------------
echo "[case 6] Dry-run — plan + bounds-check the mutation, no pmemd" >&2
C6="$RUN_BASE/dry"; stage_inputs "$C6"
cp "$SKILL_DIR/tests/fixtures/crash_unmin_shake_overflow.out" "$C6/heat.out"
"$PY" "$WRAPPER" --md-dir "$C6" --stage heat --checkpoint comp_oct.crd --dry-run \
  > "$C6/recover.json" 2>/dev/null || true
assert_ok "$C6/recover.json" "Dry-run ok"
"$PY" - "$C6/recover.json" <<'PY' || fail "Dry-run: plan + tier2 bounds_pass"
import json,sys
e=json.load(open(sys.argv[1]))
assert e["dry_run"] is True
p=e["outputs"]["plan"]; assert p["tier1"] and p["tier2"]
assert p["tier2"]["bounds_pass"] is True, "planned mutation failed bounds"
assert e["validation"]["detection"]["crashed"] is True
PY
pass "Dry-run: plan emitted, mutated namelist is check_amber-clean"

# ---- Case 7: original namelist out of bounds -> HALT (won't auto-run) -------
echo "[case 7] Bounds: a crashed stage whose own namelist is out of bounds -> HALT" >&2
C7="$RUN_BASE/oob"; mkdir -p "$C7"
cp "$SRC/comp_oct.top" "$C7/"; cp "$SRC/comp_oct.crd" "$C7/"; cp "$SRC/min3.rst" "$C7/"
cp "$SKILL_DIR/tests/fixtures/crash_unmin_shake_overflow.out" "$C7/heat.out"
cat > "$C7/heat.in" <<'EOF'
heat (OUT OF BOUNDS: dt=0.004 = 4 fs with SHAKE -> check_amber FAIL)
 &cntrl
  imin=0, irest=0, ntx=1, nstlim=800, dt=0.004,
  ntc=2, ntf=2, cut=9.0, ntb=1, ntp=0,
  ntt=3, gamma_ln=2.0, ig=-1, tempi=0.0, temp0=300.0,
  ntpr=200, ntwx=0, ntwr=800,
 /
EOF
"$PY" "$WRAPPER" --md-dir "$C7" --stage heat --checkpoint comp_oct.crd \
  > "$C7/recover.json" 2>/dev/null || true
assert_fail "$C7/recover.json" "Out-of-bounds original -> ok:false"
"$PY" - "$C7/recover.json" <<'PY' || fail "Case 7: ORIGINAL_NAMELIST_OUT_OF_BOUNDS HALT"
import json,sys
e=json.load(open(sys.argv[1]))
assert any("ORIGINAL_NAMELIST_OUT_OF_BOUNDS" in x for x in e["errors"]), e["errors"]
assert e["outputs"]["needs_human"]["reason"]=="ORIGINAL_NAMELIST_OUT_OF_BOUNDS"
assert e["validation"]["bounds"]["all_pass"] is False
PY
pass "Case 7: out-of-bounds resumed namelist -> HALT (won't auto-run), bounds honest"

echo "[acceptance] all 7 cases passed" >&2

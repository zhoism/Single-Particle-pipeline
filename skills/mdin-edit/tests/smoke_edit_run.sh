#!/usr/bin/env bash
# Tier-3: reduced-nstlim edit->run smoke.
#
# Proves the mdin-edit skill's OUTPUT is genuinely pmemd-runnable, and exercises
# the AMBERHOME rewrite + restart-file plumbing on the advisor's chain — without a
# multi-hour run. Steps:
#   1. copy the demo (10 .in + complex.parm7/.rst7 + submit.sh) to a scratch dir;
#   2. use mdin-edit ITSELF to cut nstlim on the 8 MD stages (min stages skip);
#   3. reduce maxcyc (min) + &wt istep2 (heat) for a fast smoke (sed; not the
#      skill's job — those params are out of mdin-edit's scope);
#   4. rewrite the hardcoded foreign AMBERHOME in submit.sh -> local env.sh, and
#      assert the rewritten script is foreign-path-clean (vendored detector);
#   5. run the full min1..prod chain via pmemd at the reduced step count;
#   6. assert each stage: rc==0, no "Terminated Abnormally", non-empty .rst7.
# The advisor's ORIGINAL files are never touched (we operate on a scratch copy).
#
# Env: SMOKE_NSTLIM (default 20), MDIN_DEMO_DIR, SMOKE_KEEP=1 to keep the scratch.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TESTS_DIR/.." && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
VALIDATOR="$SKILL_DIR/scripts/check_amber_vendored.py"
PRIME_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"           # .../project-prime
ENV_SH="$PRIME_ROOT/scripts/env.sh"
DEMO="${MDIN_DEMO_DIR:-/Users/kevinzhou/Downloads/Single Particle/Single Particle/phase3-explicit-solvent-md}"
NSTLIM="${SMOKE_NSTLIM:-120}"  # >= default mcbarint (100) so the MC barostat (NPT) is happy

fail() { echo "SMOKE FAIL: $1" >&2; exit 1; }

[[ -f "$ENV_SH" ]] || fail "env.sh not found at $ENV_SH"
[[ -d "$DEMO" ]] || fail "demo dir not found: $DEMO"

SC="$(mktemp -d)/smoke"
mkdir -p "$SC"
cp "$DEMO"/*.in "$SC"/
cp "$DEMO"/complex.parm7 "$DEMO"/complex.rst7 "$SC"/
[[ -f "$DEMO/submit.sh" ]] && cp "$DEMO/submit.sh" "$SC"/
echo "[smoke] scratch: $SC (nstlim=$NSTLIM)" >&2

# --- 4. AMBERHOME rewrite + foreign-path validation (do it first; cheap) ------
if [[ -f "$SC/submit.sh" ]]; then
  python3 "$VALIDATOR" "$SC/submit.sh" >/dev/null 2>&1 \
    && echo "[smoke] WARN: original submit.sh already clean (expected foreign path)" >&2 || true
  # rewrite the advisor's machine path to source the local toolchain
  python3 - "$SC/submit.sh" "$ENV_SH" <<'PY'
import re, sys
p, env = sys.argv[1], sys.argv[2]
t = open(p).read()
t = re.sub(r'(?m)^\s*export\s+AMBERHOME=.*$', f'source "{env}"', t)
open(p, "w").write(t)
PY
  if python3 "$VALIDATOR" "$SC/submit.sh" 2>&1 | grep -q "hardcoded foreign path"; then
    fail "submit.sh still has a foreign hardcoded path after rewrite"
  fi
  echo "[smoke] submit.sh AMBERHOME rewrite -> clean" >&2
fi

# --- 2. mdin-edit reduces nstlim on the MD stages -----------------------------
ENV_JSON="$(python3 "$WRAPPER" --md-dir "$SC" --stage group:all --param nstlim --value "$NSTLIM" 2>/dev/null)"
echo "$ENV_JSON" | python3 -c "import json,sys; e=json.load(sys.stdin); sys.exit(0 if e['ok'] else 1)" \
  || fail "mdin-edit nstlim reduction returned ok:false"
# confirm an actual MD stage now has the reduced nstlim
grep -qx "  nstlim = $NSTLIM," "$SC/heat-1.in" || fail "heat-1 nstlim not reduced by mdin-edit"

# --- 2b. exercise the advisor-feedback edit paths so the chain runs WITH them ---
# (i) enable-restraints on min2 — the mask-INSERT path (min2 has ntr=0, no mask).
EJSON="$(python3 "$WRAPPER" --md-dir "$SC" --stage min2 --enable-restraints \
  --restraint-wt 5.0 --restraintmask '!:WAT,Cl-,K+,Na+ & !@H=' 2>/dev/null)"
echo "$EJSON" | python3 -c "import json,sys;sys.exit(0 if json.load(sys.stdin)['ok'] else 1)" \
  || fail "enable-restraints on min2 returned ok:false"
grep -qx '  ntr = 1,' "$SC/min2.in" || fail "min2 ntr not enabled"
grep -qx '  restraintmask = "!:WAT,Cl-,K+,Na+ & !@H=",' "$SC/min2.in" \
  || fail "min2 restraintmask line not inserted"
echo "[smoke] enable-restraints inserted a restraintmask line in min2.in" >&2
# (ii) temp0 -> 310 from the third stage onward — exercises the tempi coupling on
#      relax/prod and the &wt value2 coupling on heat-3, in a real run.
TJSON="$(python3 "$WRAPPER" --md-dir "$SC" --stage group:third-onward --param temp0 --value 310 2>/dev/null)"
echo "$TJSON" | python3 -c "import json,sys;sys.exit(0 if json.load(sys.stdin)['ok'] else 1)" \
  || fail "temp0->310 group edit returned ok:false"
grep -qx '  tempi = 310.0,' "$SC/relax.in" || fail "relax tempi not coupled to 310"
echo "[smoke] temp0->310 (relax/prod tempi coupled) applied" >&2

# --- 3. fast-smoke trims OUTSIDE mdin-edit's scope (maxcyc, &wt istep2) --------
sed -i.bak "s/maxcyc = 10000/maxcyc = $NSTLIM/" "$SC"/min1.in "$SC"/min2.in
sed -i.bak "s/istep2 = 40000/istep2 = $NSTLIM/" "$SC"/heat-1.in "$SC"/heat-2.in "$SC"/heat-3.in
rm -f "$SC"/*.bak

# --- 5. source toolchain + run the chain --------------------------------------
set +u  # amber.sh references DYLD_FALLBACK_LIBRARY_PATH etc. unguarded under set -u
# shellcheck disable=SC1090
source "$ENV_SH"
set -u
command -v pmemd >/dev/null 2>&1 || fail "pmemd not on PATH after env.sh"
echo "[smoke] pmemd: $(command -v pmemd)" >&2

cd "$SC" || fail "cannot cd scratch"   # bare filenames avoid the space-in-path footgun

run_stage() {
  local st="$1" c="$2" x=""
  [[ "$st" == min* ]] || x="-x ${st}.nc"
  pmemd -O -i "${st}.in" -p complex.parm7 -c "${c}" -ref "${c}" \
        -o "${st}.out" -r "${st}.rst7" ${x} >/dev/null 2>&1
  local rc=$?
  [[ $rc -eq 0 ]] || { echo "[smoke] $st rc=$rc"; tail -8 "${st}.out" 2>/dev/null >&2; return 1; }
  grep -qi "Terminated Abnormally" "${st}.out" 2>/dev/null && { echo "[smoke] $st abnormal"; return 1; }
  [[ -s "${st}.rst7" ]] || { echo "[smoke] $st empty rst7"; return 1; }
  echo "[smoke]   $st OK" >&2
}

# chain order + restart coords (from submit.sh)
run_stage min1   complex.rst7 || fail "min1"
run_stage min2   min1.rst7    || fail "min2"
run_stage heat-1 min2.rst7    || fail "heat-1"
run_stage press-1 heat-1.rst7 || fail "press-1"
run_stage heat-2 press-1.rst7 || fail "heat-2"
run_stage press-2 heat-2.rst7 || fail "press-2"
run_stage heat-3 press-2.rst7 || fail "heat-3"
run_stage press-3 heat-3.rst7 || fail "press-3"
run_stage relax  press-3.rst7 || fail "relax"
run_stage prod   relax.rst7   || fail "prod"

echo "SMOKE OK: 10/10 stages ran to normal termination (nstlim=$NSTLIM)" >&2
[[ "${SMOKE_KEEP:-0}" == "1" ]] || rm -rf "$(dirname "$SC")"
exit 0

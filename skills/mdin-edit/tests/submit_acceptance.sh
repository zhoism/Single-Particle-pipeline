#!/usr/bin/env bash
# Acceptance for `mdin-edit --submit` (the productized edit->run smoke).
#
# Proves the skill's --submit mode end-to-end:
#   A. --submit --dry-run  -> ok:true, plan emitted, NO pmemd, NO scratch left
#                             (no toolchain needed; CI-safe).
#   B. --submit (real run) -> edit a fresh copy (dt->0.001 in heat-1 to simulate
#                             "already-edited"), then run the min1..prod pmemd
#                             chain at reduced nstlim; assert ok:true, 10/10
#                             stages normal_termination, final_rst7 present, and
#                             that --md-dir was NOT mutated (scratch-only).
# The advisor's ORIGINAL files are never touched (we copy the demo first).
#
# Env: SUBMIT_NSTLIM (default 120, >=100 for the MC barostat), MDIN_DEMO_DIR,
#      SUBMIT_SKIP_RUN=1 to run only the dry-run part (no toolchain).
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TESTS_DIR/.." && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
PY="${PYTHON:-python3}"
DEMO="${MDIN_DEMO_DIR:-/Users/kevinzhou/Downloads/Single Particle/Single Particle/phase3-explicit-solvent-md}"
NSTLIM="${SUBMIT_NSTLIM:-120}"

fail() { echo "SUBMIT-ACCEPT FAIL: $1" >&2; exit 1; }
[[ -d "$DEMO" ]] || fail "demo dir not found: $DEMO"

# helper: read .ok / a dotted field out of an envelope on stdin
jget() { "$PY" -c "import json,sys; e=json.load(sys.stdin); print(eval('e'+sys.argv[1]))" "$1"; }

mkcopy() {
  local d; d="$(mktemp -d)/copy"; mkdir -p "$d"
  cp "$DEMO"/*.in "$DEMO"/complex.parm7 "$DEMO"/complex.rst7 "$DEMO"/submit.sh "$d"/
  echo "$d"
}

# --- A. dry-run (no toolchain) -----------------------------------------------
CP_A="$(mkcopy)"
ENV_A="$("$PY" "$WRAPPER" --md-dir "$CP_A" --submit --dry-run 2>/dev/null)"
echo "$ENV_A" | jget "['ok']"          | grep -qx True  || fail "dry-run ok!=true"
echo "$ENV_A" | jget "['dry_run']"     | grep -qx True  || fail "dry-run dry_run!=true"
echo "$ENV_A" | jget "['outputs']['mode']" | grep -qx submit || fail "dry-run mode!=submit"
echo "$ENV_A" | jget "['validation']['submit_script']['foreign_path_clean']" | grep -qx True \
  || fail "dry-run submit.sh not foreign-path-clean after rewrite"
NPLAN="$(echo "$ENV_A" | "$PY" -c "import json,sys; print(len(json.load(sys.stdin)['outputs']['stages']))")"
[[ "$NPLAN" == 10 ]] || fail "dry-run planned $NPLAN stages (expected 10)"
# nothing should run, nothing should leak
[[ -f "$CP_A/prod.out" ]] && fail "dry-run produced pmemd output (should not run)"
echo "[submit-accept] A. --submit --dry-run OK (10 stages planned, clean, no run)" >&2
rm -rf "$(dirname "$CP_A")"

if [[ "${SUBMIT_SKIP_RUN:-0}" == "1" ]]; then
  echo "SUBMIT-ACCEPT OK: dry-run only (SUBMIT_SKIP_RUN=1)" >&2
  exit 0
fi

# --- B. real run on an already-edited copy -----------------------------------
CP_B="$(mkcopy)"
# simulate "already edited": dt -> 0.001 in heat-1, via the editor itself
"$PY" "$WRAPPER" --md-dir "$CP_B" --stage heat-1 --param dt --value 0.001 >/dev/null 2>&1 \
  || fail "pre-edit (dt->0.001 heat-1) failed"
grep -qE '^\s*dt = 0.001,' "$CP_B/heat-1.in" || fail "pre-edit did not take"
BEFORE_HASH="$(cat "$CP_B"/*.in | shasum | awk '{print $1}')"

ENV_B="$("$PY" "$WRAPPER" --md-dir "$CP_B" --submit --reduce-nstlim "$NSTLIM" 2>/dev/null)"
echo "$ENV_B" | jget "['ok']" | grep -qx True || { echo "$ENV_B" >&2; fail "real submit ok!=true"; }
NSTAGES_OK="$(echo "$ENV_B" | "$PY" -c "import json,sys; e=json.load(sys.stdin); print(sum(1 for s in e['outputs']['stages'] if s['normal_termination']))")"
[[ "$NSTAGES_OK" == 10 ]] || fail "only $NSTAGES_OK/10 stages reached normal termination"
echo "$ENV_B" | jget "['outputs']['final_rst7']" | grep -q prod.rst7 || fail "no final_rst7 (prod) reported"

# --md-dir must NOT have been mutated by --submit (scratch-only)
AFTER_HASH="$(cat "$CP_B"/*.in | shasum | awk '{print $1}')"
[[ "$BEFORE_HASH" == "$AFTER_HASH" ]] || fail "--submit mutated --md-dir (.in files changed)"
[[ -f "$CP_B/prod.out" ]] && fail "--submit wrote pmemd output into --md-dir (should be scratch-only)"

echo "SUBMIT-ACCEPT OK: dry-run + real run 10/10 stages normal, final_rst7 present, --md-dir untouched (nstlim=$NSTLIM)" >&2
rm -rf "$(dirname "$CP_B")"
exit 0

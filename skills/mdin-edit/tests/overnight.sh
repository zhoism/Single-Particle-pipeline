#!/usr/bin/env bash
# Overnight robustness loop for mdin-edit (Tier 2/3).
#
# Runs the deterministic gate ONCE (oracle self-test + mutation + full Tier-1),
# then loops toolchain checks under a wall-clock cap, gating on success+sanity to
# catch flakiness / nondeterminism that a single pass can't:
#   every iter : fuzz with a FRESH seed (new random cases) + edit->run pmemd smoke
#   every 5    : mdin-edit + antechamber + tleap + amber-md-run acceptance suites
#   every 10   : cpptraj-analysis suite + run_happy_path.sh (short ps)
#
# Deterministic harness runs under the SYSTEM python (3.14, where it was
# validated); toolchain suites run in env.sh-sourced subshells (conda 3.11 +
# pmemd). Every result is appended to results.csv (iter,check,rc); summary.json
# is (re)written each iter and on exit.
#
# Env: OVERNIGHT_SECONDS (default 21600 = 6h), OVERNIGHT_SEED (default 20260608).
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TESTS_DIR/.." && pwd)"
PRIME_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
ENV_SH="$PRIME_ROOT/scripts/env.sh"
SYS_PY="$(command -v python3)"                       # capture BEFORE any env sourcing (3.14)
BUDGET="${OVERNIGHT_SECONDS:-21600}"
SEED_BASE="${OVERNIGHT_SEED:-20260608}"

TS="$(date +%Y%m%d-%H%M%S)"
OUT="$SKILL_DIR/test-runs/overnight-$TS"
mkdir -p "$OUT/failures"
LOG="$OUT/overnight.log"
CSV="$OUT/results.csv"
: > "$CSV"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
start="$(date +%s)"
elapsed() { echo $(( $(date +%s) - start )); }

iter=0
record() {  # check rc logpath
  local check="$1" rc="$2" logp="${3:-}"
  echo "${iter},${check},${rc}" >> "$CSV"
  if [[ "$rc" -ne 0 && -n "$logp" && -f "$logp" ]]; then
    cp "$logp" "$OUT/failures/${check}-iter${iter}.log"
    log "  FAIL $check (rc=$rc) -> failures/${check}-iter${iter}.log"
  fi
}
summarize() { "$SYS_PY" "$TESTS_DIR/summarize.py" "$CSV" "$OUT/summary.json" >> "$LOG" 2>&1 || true; }
trap 'summarize; log "overnight EXIT after $(elapsed)s, $iter iters"' EXIT

run_suite() {  # name cmd...
  local name="$1"; shift
  ( set +u; source "$ENV_SH" >/dev/null 2>&1; set -u; "$@" ) > "$OUT/last_${name}.log" 2>&1
  local rc=$?
  record "$name" "$rc" "$OUT/last_${name}.log"
  return 0
}

log "overnight start; budget=${BUDGET}s seed_base=${SEED_BASE} sys_py=$SYS_PY"
log "out=$OUT"

# ---- Deterministic gate (once) ----
log "gate: oracle self-test"
"$SYS_PY" "$TESTS_DIR/oracle_selftest.py" > "$OUT/gate_oracle.log" 2>&1; record gate_oracle $? "$OUT/gate_oracle.log"
log "gate: mutation"
"$SYS_PY" "$TESTS_DIR/mutation_test.py" > "$OUT/gate_mutation.log" 2>&1; record gate_mutation $? "$OUT/gate_mutation.log"
log "gate: tier1 (full)"
"$SYS_PY" "$TESTS_DIR/fuzz_mdin_edit.py" --out "$OUT/gate_tier1" > "$OUT/gate_tier1.log" 2>&1; record gate_tier1 $? "$OUT/gate_tier1.log"
summarize

# ---- Robustness loop ----
while [[ "$(elapsed)" -lt "$BUDGET" ]]; do
  iter=$((iter + 1))
  log "--- iter $iter (elapsed $(elapsed)s / $BUDGET) ---"

  # (a) fuzz, fresh seed (deterministic engine, new random cases)
  "$SYS_PY" "$TESTS_DIR/fuzz_mdin_edit.py" --quick --seed "$((SEED_BASE + iter))" \
      --out "$OUT/fuzz-iter" > "$OUT/last_fuzz.log" 2>&1
  record fuzz $? "$OUT/last_fuzz.log"

  # (b) edit->run pmemd smoke (Tier 3; nondeterministic MD -> flakiness)
  bash "$TESTS_DIR/smoke_edit_run.sh" > "$OUT/last_smoke.log" 2>&1
  record smoke $? "$OUT/last_smoke.log"

  # (c) every 5 iters: the fast acceptance suites
  if (( iter % 5 == 1 )); then
    run_suite acc_mdin   bash "$SKILL_DIR/test_acceptance.sh"
    run_suite acc_antech bash "$PRIME_ROOT/skills/antechamber-ligandprep/test_acceptance.sh"
    run_suite acc_tleap  bash "$PRIME_ROOT/skills/tleap-build/test_acceptance.sh"
    run_suite acc_ambmd  bash "$PRIME_ROOT/skills/amber-md-run/test_acceptance.sh"
  fi

  # (d) every 10 iters: the slow full-pipeline checks
  if (( iter % 10 == 1 )); then
    run_suite acc_cpptraj bash "$PRIME_ROOT/skills/cpptraj-analysis/test_acceptance.sh"
    run_suite happy_path  bash "$PRIME_ROOT/run_happy_path.sh" 2
  fi

  summarize
done

log "budget reached; finalizing"
summarize

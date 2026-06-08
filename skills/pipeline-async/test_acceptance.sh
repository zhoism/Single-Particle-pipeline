#!/usr/bin/env bash
# pipeline-async acceptance test.
#
# Fast cases (default):
#   1. Dry-run    — prints the launch plan, status "planned", spawns nothing.
#   2. Malformed  — bad --sim-ps -> graceful ok:false (not a crash).
# Slow case (opt-in via LIVE=1; ~10-15 min, may post to Discord):
#   3. Live       — real detached launch; asserts the envelope returns FAST with
#                   status "launched" and that run.log appears.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SKILL_DIR/scripts/wrapper.py"
RUN_BASE="$SKILL_DIR/test-runs"
mkdir -p "$RUN_BASE"

pass() { echo "PASS: $1" >&2; }
fail() { echo "FAIL: $1" >&2; exit 1; }

assert_status() {  # <envelope.json> <expected_status> <label>
  python3 - "$1" "$2" <<'PY' || fail "$3"
import json, sys
e = json.load(open(sys.argv[1]))
if not e.get("ok") or e.get("status") != sys.argv[2]:
    print("  got:", {k: e.get(k) for k in ("ok", "status", "errors")}, file=sys.stderr)
    sys.exit(1)
PY
  pass "$3"
}

assert_fail() {  # <envelope.json> <label>
  python3 - "$1" <<'PY' || fail "$2"
import json, sys
e = json.load(open(sys.argv[1]))
sys.exit(0 if (e.get("ok") is False and e.get("errors")) else 1)
PY
  pass "$2"
}

# ---- Case 1: dry-run ------------------------------------------------------
echo "[case 1] dry-run — launch plan only" >&2
OUT1="$RUN_BASE/dryrun"; rm -rf "$OUT1"; mkdir -p "$OUT1"
python3 "$WRAPPER" --sim-ps 50 --channel 1511130059061067858 \
  --output-dir "$OUT1/run" --dry-run > "$OUT1/env.json" || true
assert_status "$OUT1/env.json" "planned" "dry-run plans (status=planned)"
# It must NOT have created the output dir / spawned anything.
[ ! -d "$OUT1/run" ] && pass "dry-run spawned nothing" || fail "dry-run created an output dir"

# ---- Case 2: malformed input ----------------------------------------------
echo "[case 2] malformed — --sim-ps 0 -> graceful ok:false" >&2
OUT2="$RUN_BASE/malformed"; rm -rf "$OUT2"; mkdir -p "$OUT2"
python3 "$WRAPPER" --sim-ps 0 --output-dir "$OUT2/run" > "$OUT2/env.json" 2>/dev/null || true
assert_fail "$OUT2/env.json" "malformed --sim-ps fails gracefully"

# ---- Case 3: live launch (opt-in) -----------------------------------------
if [[ "${LIVE:-0}" == "1" ]]; then
  echo "[case 3] LIVE launch — real detached run (~10-15 min, may post to Discord)" >&2
  OUT3="$RUN_BASE/live"; rm -rf "$OUT3"; mkdir -p "$OUT3"
  START=$(date +%s)
  # No --channel notify unless NOTIFY_CHANNEL exported by the caller's run_happy_path env;
  # pass a channel only if you intend to post. Here we launch without notifications.
  python3 "$WRAPPER" --sim-ps 2 --channel "" --output-dir "$OUT3/run" > "$OUT3/env.json" || true
  ELAPSED=$(( $(date +%s) - START ))
  assert_status "$OUT3/env.json" "launched" "live launch returns status=launched"
  [ "$ELAPSED" -lt 15 ] && pass "launch returned fast (${ELAPSED}s < 15s)" || fail "launch blocked (${ELAPSED}s)"
  # run.log should appear shortly as the detached job starts writing.
  for _ in $(seq 1 20); do [ -s "$OUT3/run/run.log" ] && break; sleep 1; done
  [ -s "$OUT3/run/run.log" ] && pass "detached run.log is being written" || fail "no run.log after 20s"
  echo "[case 3] NOTE: the detached run continues for ~10-15 min; inspect $OUT3/run/run.log" >&2
else
  echo "[case 3] LIVE launch SKIPPED (set LIVE=1 to run the real ~10-15 min job)" >&2
fi

echo "[acceptance] pipeline-async fast cases passed" >&2

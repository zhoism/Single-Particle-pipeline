#!/usr/bin/env bash
# Bounded-recovery hook for run_happy_path.sh (Stage 4b).
#
# Opt-in (RECOVER=1), NON-FATAL (always exit 0). On the green happy path the MD
# completes cleanly, so the deterministic detector reports "no crash" and this is a
# no-op -> the proven pipeline stays byte-green.
#
# DETECTOR-AUTHORITATIVE: amber-recover's deterministic mdout detector (NaN/Infinity
# sticky, SHAKE-fail, overflow, temp-blowup, no-normal-termination) is STRICTLY
# STRONGER than amber-md-run's own crash check (which does NOT flag NaN/Infinity).
# So instead of trusting amber-md-run's `ok` flag — which reports a silent-NaN run
# (banner + rc 0 + .rst written) as healthy and would gate the recovery skill's
# headline capability out of the wired pipeline — this hook runs `amber-recover
# --detect-only` (.out-only, no pmemd) on the completed MD dir and escalates to full
# recovery iff the strong detector says crashed.
#
# Usage: recover_hook.sh <s4.json> <md_dir> [run_id]   (<s4.json> only locates the
#        output dir + run id; the detector, not its `ok` flag, is the authority)
set +e

S4="${1:-}"; MDDIR="${2:-}"; RUN_ID="${3:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RC="$ROOT/skills/amber-recover/scripts/wrapper.py"
OUTDIR="$(dirname "${S4:-.}")"

note() { echo "recover: $*" >&2; }

[ "${RECOVER:-0}" = "1" ] || { note "disabled (RECOVER!=1) — no-op"; exit 0; }
[ -f "$RC" ]   || { note "skill missing — no-op"; exit 0; }
[ -d "$MDDIR" ] || { note "md_dir $MDDIR missing — no-op"; exit 0; }

# Authoritative deterministic detection on the completed MD (auto-picks the last
# stage with an .out). Catches the silent NaN/Infinity-with-banner class that the
# upstream amber-md-run envelope misses.
DET="$OUTDIR/recover_detect.json"
python3 "$RC" --md-dir "$MDDIR" --detect-only > "$DET" 2>"$OUTDIR/recover.err"
CRASHED=$(python3 -c "import json;e=json.load(open('$DET'));print(e.get('validation',{}).get('detection',{}).get('crashed', False))" 2>/dev/null)
STAGE=$(python3 -c "import json;e=json.load(open('$DET'));print(e.get('outputs',{}).get('stage',''))" 2>/dev/null)

if [ "$CRASHED" != "True" ]; then
  note "deterministic detector: MD clean — nothing to recover"; exit 0
fi
[ -n "$STAGE" ] || { note "detector flagged a crash but no stage resolved — no-op"; exit 0; }

note "deterministic detector flags ${STAGE} crashed -> bounded recovery"
python3 "$RC" --md-dir "$MDDIR" --stage "$STAGE" \
  > "$OUTDIR/s4b.json" 2>>"$OUTDIR/recover.err"
ROK=$(python3 -c "import json;print(json.load(open('$OUTDIR/s4b.json')).get('ok'))" 2>/dev/null)
TIER=$(python3 -c "import json;print(json.load(open('$OUTDIR/s4b.json'))['outputs'].get('tier'))" 2>/dev/null)
if [ "$ROK" = "True" ]; then
  note "SUCCEEDED tier=${TIER} (crashed stage salvaged; see $OUTDIR/s4b.json)"
else
  note "HALTED — needs human review (see $OUTDIR/s4b.json)"
fi
exit 0

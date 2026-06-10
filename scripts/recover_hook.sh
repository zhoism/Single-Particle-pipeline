#!/usr/bin/env bash
# Bounded-recovery hook for run_happy_path.sh (Stage 4b).
#
# Opt-in (RECOVER=1), CRASH-ONLY, NON-FATAL. On the green happy path the MD never
# crashes, so this is a no-op and the proven pipeline stays byte-green. It fires
# ONLY when amber-md-run's envelope (s4.json) reports an MD crash, then dispatches
# the amber-recover skill on the crashed stage's md_dir. Always exits 0 so it can
# never make a failed run worse than it already is.
#
# Usage: recover_hook.sh <s4.json> <md_dir> [run_id]
# Emits a one-line "recover: ..." status to stderr; writes s4b.json on dispatch.
set +e

S4="${1:-}"; MDDIR="${2:-}"; RUN_ID="${3:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RC="$ROOT/skills/amber-recover/scripts/wrapper.py"

note() { echo "recover: $*" >&2; }

[ "${RECOVER:-0}" = "1" ] || { note "disabled (RECOVER!=1) — no-op"; exit 0; }
[ -f "$RC" ]   || { note "skill missing — no-op"; exit 0; }
[ -f "$S4" ]   || { note "no MD envelope — no-op"; exit 0; }

OKV=$(python3 -c "import json;print(json.load(open('$S4')).get('ok'))" 2>/dev/null)
[ "$OKV" = "True" ] && { note "MD ok — nothing to recover"; exit 0; }

STAGE=$(python3 -c "import json,re;e=json.load(open('$S4'));errs=' '.join(e.get('errors',[]) or []);m=re.search(r'(?:MD_CRASH|STAGE_INCOMPLETE)\[(\w+)\]',errs);print(m.group(1) if m else '')" 2>/dev/null)
[ -n "$STAGE" ] || { note "MD failed but no crashed stage in envelope — no-op"; exit 0; }
[ -d "$MDDIR" ] || { note "md_dir $MDDIR missing — no-op"; exit 0; }

note "MD crashed at ${STAGE} -> bounded recovery"
OUTDIR="$(dirname "$S4")"
python3 "$RC" --md-dir "$MDDIR" --stage "$STAGE" \
  > "$OUTDIR/s4b.json" 2>"$OUTDIR/recover.err"
ROK=$(python3 -c "import json;print(json.load(open('$OUTDIR/s4b.json')).get('ok'))" 2>/dev/null)
TIER=$(python3 -c "import json;print(json.load(open('$OUTDIR/s4b.json'))['outputs'].get('tier'))" 2>/dev/null)
if [ "$ROK" = "True" ]; then
  note "SUCCEEDED tier=${TIER} (crashed stage salvaged; see $OUTDIR/s4b.json)"
else
  note "HALTED — needs human review (see $OUTDIR/s4b.json)"
fi
exit 0

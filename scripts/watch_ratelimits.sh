#!/usr/bin/env bash
# watch_ratelimits.sh — when an OpenClaw agent turn fails on a usage/rate limit
# (429), tell the Discord channel so a silent bot doesn't need human diagnosis.
#
# OpenClaw 2026.5.28 has no native error-delivery flag and no agent-failure hook
# (hook events stop at message:sent, which never fires when the turn dies before
# replying). So this tails the gateway log for the rate-limit signature and posts
# via notify_discord.sh — which is LLM-FREE, so it works precisely when the LLM
# providers are the thing that's down.
#
# Usage:  bash watch_ratelimits.sh [channel_id]        # manual-start; run with &
# Env:    COOLDOWN=<sec>   collapse a failure's line-burst into one alert (default 60)
#         NOTIFY_DRYRUN=1  passed through to notify_discord.sh (no real post)
#
# Caveat: follows the log it starts on; after a midnight log rollover, restart it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY_SH="$HERE/notify_discord.sh"
CHANNEL_DEFAULT="${1:-1511130059061067858}"
COOLDOWN="${COOLDOWN:-60}"
LOGDIR="/tmp/openclaw"

LOG=$(ls -t "$LOGDIR"/openclaw-*.log 2>/dev/null | head -1 || true)
if [ -z "$LOG" ]; then
  echo "watch_ratelimits: no gateway log in $LOGDIR" >&2
  exit 1
fi
echo "watch_ratelimits: watching $LOG (default channel=$CHANNEL_DEFAULT, cooldown=${COOLDOWN}s)" >&2

# A rate-limited Discord turn logs a short burst; the decisive line carries the
# Discord lane + the failover error. Fire once per burst via a cooldown.
last=0
tail -F -n0 "$LOG" 2>/dev/null | while IFS= read -r line; do
  case "$line" in
    *"lane task error"*":discord:channel:"*"FailoverError"*) : ;;
    *) continue ;;
  esac
  now=$(date +%s)
  if [ $(( now - last )) -lt "$COOLDOWN" ]; then continue; fi
  last=$now

  reason="usage/rate limit"
  case "$line" in
    *RESOURCE_EXHAUSTED*|*quota*) reason="provider daily quota" ;;
    *"rate limit"*|*429*)         reason="per-minute rate limit (429)" ;;
  esac

  chan=$(printf '%s' "$line" | sed -nE 's/.*:discord:channel:([0-9]+).*/\1/p' | head -1)
  [ -z "$chan" ] && chan="$CHANNEL_DEFAULT"

  msg="⚠️ I hit a ${reason} and couldn't reply to the last message. Free Cerebras is ~60k tokens/min (wait ~1 min and re-@-mention me); Google free has a daily cap. Your request wasn't lost — just try again shortly."
  if bash "$NOTIFY_SH" "$chan" "$msg"; then
    echo "watch_ratelimits: alerted channel=$chan ($reason)" >&2
  else
    echo "watch_ratelimits: notify failed for channel=$chan" >&2
  fi
done

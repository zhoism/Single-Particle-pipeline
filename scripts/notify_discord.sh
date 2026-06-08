#!/usr/bin/env bash
# notify_discord.sh — post a message (optionally with media) to a Discord channel
# through OpenClaw's own bot connection (`openclaw message send`).
#
# Why this exists: it is LLM-FREE. The Discord bot connection is independent of
# the LLM providers, so this still delivers when an agent turn is rate-limited
# (429) — which is exactly when we most need to tell the user something. Used by
# run_happy_path.sh (notify mode) and watch_ratelimits.sh.
#
# Usage:  notify_discord.sh <channel_id> <message> [media_path]
# Env:    NOTIFY_DRYRUN=1   -> run `openclaw message send --dry-run` (prints the
#                              payload, posts nothing). For tests.
#         OPENCLAW=<bin>    -> override the openclaw binary (default: openclaw).
set -uo pipefail

CHANNEL="${1:?usage: notify_discord.sh <channel_id> <message> [media_path]}"
MESSAGE="${2:?usage: notify_discord.sh <channel_id> <message> [media_path]}"
MEDIA="${3:-}"
OPENCLAW="${OPENCLAW:-openclaw}"

args=(message send --channel discord --target "channel:${CHANNEL}" --message "$MESSAGE")
if [ -n "$MEDIA" ] && [ -f "$MEDIA" ]; then
  args+=(--media "$MEDIA")
fi

if [ "${NOTIFY_DRYRUN:-0}" = "1" ]; then
  # Exercise the real CLI but skip the actual send (validates target/payload).
  "$OPENCLAW" "${args[@]}" --dry-run
  exit $?
fi

if ! "$OPENCLAW" "${args[@]}" >/dev/null 2>&1; then
  echo "notify_discord: send failed (channel=$CHANNEL)" >&2
  exit 1
fi

# env.sh — activate the local AMBER toolchain for DETACHED / non-interactive runs.
#
# A detached pipeline job (see skills/pipeline-async) does not inherit the user's
# interactive shell, so it must bootstrap the toolchain itself. This is the single
# source of truth for where that toolchain lives; override either var if it moves.
#
#   PRIME_AMBER_SH  amber.sh from the prime-amber conda env (sets AMBERHOME etc.)
#   PMEMD_BIN       locally-built pmemd 26 bin dir (prepended so pmemd is found)
#
# Source it: `source scripts/env.sh`
PRIME_AMBER_SH="${PRIME_AMBER_SH:-/opt/homebrew/Caskroom/miniforge/base/envs/prime-amber/amber.sh}"
PMEMD_BIN="${PMEMD_BIN:-$HOME/Downloads/pmemd26/bin}"

# shellcheck disable=SC1090
[ -f "$PRIME_AMBER_SH" ] && source "$PRIME_AMBER_SH"
export PATH="$PMEMD_BIN:${AMBERHOME:+$AMBERHOME/bin:}$PATH"

# The detached pipeline posts to Discord via the `openclaw` CLI (LLM-free), which
# needs Node 22.19+. A job from the gateway's exec tool can have a STALE node ahead
# of nvm on PATH (e.g. /usr/local/bin/node v20) — so `openclaw` resolves but aborts
# with "Node.js v22.19+ is required". Prepend an nvm bin that ships a new-enough
# node ALONGSIDE openclaw, so both the right node and openclaw win over system node.
for _ocdir in "$HOME"/.nvm/versions/node/v2[2-9]*/bin \
              "$HOME"/.nvm/versions/node/v[3-9]*/bin \
              "$HOME"/.nvm/versions/node/*/bin; do
  if [ -x "$_ocdir/openclaw" ] && [ -x "$_ocdir/node" ]; then
    export PATH="$_ocdir:$PATH"; break
  fi
done
unset _ocdir

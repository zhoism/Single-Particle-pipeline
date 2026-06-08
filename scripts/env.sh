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

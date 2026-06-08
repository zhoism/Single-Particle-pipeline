# pipeline-async — design heuristics

Phase B (Discord orchestration). Why this skill is shaped the way it is.

## The core constraint: long run vs synchronous turn

A full pipeline run is ~10-15 min. OpenClaw aborts a model request after **120s** of idle (`concepts/agent-loop.md`), and Discord expects a prompt reply. So the agent **must not** babysit the run inside the turn. Instead:

1. The agent makes ONE `exec` call to this wrapper, which **launches a detached job and returns in <1s**.
2. The agent's normal reply delivers "started" synchronously.
3. The detached job posts progress + results on its own.

## Why detached `start_new_session=True`, not the `process` tool / sub-agents

- `start_new_session=True` puts the child in a **new session/process group**, so it survives the wrapper exiting *and* OpenClaw reaping the agent's `exec` (which may kill the exec's own process group). This is the robust OS-level "fire and forget".
- The `process` tool / polling would keep the agent loop alive (LLM round-trips) for 15 min — expensive and idle-timeout-prone. The detached pattern needs **zero** LLM involvement after launch.

## Why notifications go through `openclaw message send` (not the LLM)

`openclaw message send` posts via OpenClaw's own Discord bot connection — **independent of the LLM providers**. So progress/results land even when Cerebras/Google are rate-limited (429). A notification path that needed an agent turn would fail exactly when we most need to report. Verified: `openclaw message send --dry-run` reports `dryRun:true` and posts nothing — safe for tests via `NOTIFY_DRYRUN=1`.

## Why the spine stays DRY (one chain)

`run_happy_path.sh` gained an **opt-in** `NOTIFY_CHANNEL` mode rather than forking a second async runner. One chain definition = no drift between the verification spine and the async path (a second copy is exactly the kind of duplication that let the aromatic-typing bug hide unnoticed). Unset `NOTIFY_CHANNEL` → byte-identical default behavior.

## Toolchain bootstrap (`scripts/env.sh`)

A detached job does not inherit the user's interactive shell, so it sources `scripts/env.sh` (single, overridable source of truth: `PRIME_AMBER_SH` + `PMEMD_BIN`). The wrapper deliberately requires no AMBER binary itself — a missing toolchain surfaces as a Discord `❌ failed` notice with a `run.log` pointer, not a silent hang.

## Deferred (not in this skill)

- Arbitrary ligand/PDB from the message (this runs the fixed 1L2Y demo + a `--sim-ps`).
- Per-run secrets encryption (`openclaw secrets configure`) — the bot token stays plaintext in `openclaw.json` (mode 600); `message send` owns it, we never read it.
- Always-on deployment of the 429 watcher (it's manual-start for now).

---
name: pipeline-async
description: "Run the FULL local AMBER MD pipeline (ligand prep -> topology -> MD -> analysis + MM-GBSA) on the 1L2Y demo system in the BACKGROUND, and report progress + results to the Discord channel. Returns immediately with a run id so a chat turn never blocks on the ~10-15 min run. Use when asked to 'run the (full) pipeline' / 'run the MD' over Discord."
license: MIT
homepage: https://github.com/zhoism/Single-Particle
compatibility: Requires the prime-amber toolchain (sourced via scripts/env.sh by the detached job) + a working OpenClaw Discord channel for notifications.
metadata: {"openclaw":{"requires":{"env":["AMBERHOME"]},"os":["darwin"]},"requires":{"bins":["bash","python3"],"env":["AMBERHOME"]},"inputs":{"sim_ps":"production length in ps (int, default 50)","channel":"Discord channel id to notify (default project channel)","run_id":"run label (default timestamp)","output_dir":"results dir (default ROOT/pipeline-async-run-<run_id>)"},"outputs":{"run_id":"label","status":"launched|planned","outdir":"path","log":"<outdir>.log"},"dry_run":true,"source":"project-prime/skills/pipeline-async","stage":"PhaseB.async"}
---

# pipeline-async

## Goal

Kick off the full local AMBER MD happy path on the 1L2Y demo system **in the background** and hand the channel a run id right away. A full run is ~10-15 min — far longer than the 120s model idle limit and a synchronous chat reply — so this skill launches a detached job and returns instantly. The detached job posts each stage and the final MM-GBSA ΔG (with a plot) back to Discord on its own, using the **LLM-free** `openclaw message send`, so the updates land even if the LLM is rate-limited.

## When to use

- A Discord user asks to "run the full pipeline / run the MD" (optionally at a given ps).
- Any time you want the end-to-end pipeline but must not block the turn on it.
- NOT for a single stage — use `antechamber-ligandprep` / `tleap-build` / `amber-md-run` / `cpptraj-analysis` directly for those.

## Inputs

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `--sim-ps` | int | no (default `50`) | Production length in ps. |
| `--channel` | str | no | Discord channel id to notify (defaults to the project channel). |
| `--run-id` | str | no | Run label; also names the output dir. Defaults to a timestamp. |
| `--output-dir` | path | no | Results dir (default `project-prime/pipeline-async-run-<run_id>`). |
| `--dry-run` | flag | no | Print the launch plan without launching. |

## Invocation (the agent makes ONE `exec` call, then replies "started")

```
python3 {baseDir}/scripts/wrapper.py --sim-ps 50 --channel 1511130059061067858
```

## Outputs

One JSON envelope on stdout, returned immediately:

```json
{
  "ok": true, "skill": "pipeline-async", "dry_run": false,
  "run_id": "pa-20260608-143000", "status": "launched",
  "outputs": {"outdir": "/abs/.../pipeline-async-run-pa-...", "log": ".../run.log",
              "channel": "1511130059061067858", "sim_ps": 50, "eta": "~10-15 min"},
  "errors": []
}
```

After launch, the channel receives (from the detached job, via `openclaw message send`):
`🚀 started` → `🧪 prep ✓` → `🧬 topology ✓` → `⚛️ MD ✓` → `📊 analysis ✓` → `✅ done — N analyses, MM-GBSA ΔG X` (+ an RMSD plot), or `❌ failed at <stage>` with a log pointer.

## How it works

`{baseDir}/scripts/wrapper.py` validates inputs, then launches `run_happy_path.sh` in a **new session** (`start_new_session=True`) so it survives the wrapper — and the agent's `exec` — exiting. The detached command (a NON-login `bash -c`, so the user's profile can't switch node and break the LLM-free `openclaw` notify path) is `source scripts/env.sh && NOTIFY_CHANNEL=<id> RUN_ID=<id> bash run_happy_path.sh <ps> <outdir>`, with stdout/stderr → a sibling `<outdir>.log` (outside the dir `run_happy_path.sh` wipes). The wrapper itself touches no AMBER binary, so it returns in well under a second; the toolchain is bootstrapped by `scripts/env.sh` inside the detached job and validated stage-by-stage by the spine. `run_happy_path.sh` only notifies when `NOTIFY_CHANNEL` is set, so its plain (verification-spine) behavior is unchanged.

## Errors

| Code | Cause | Recovery |
|------|-------|----------|
| `INVALID_INPUT` | `--sim-ps` not a positive int. | Re-issue with a valid ps. |
| `INPUT_PREP_FAILED` | `run_happy_path.sh` or `scripts/env.sh` missing. | Repair the project layout. |
| `LAUNCH_FAILED` | The OS could not spawn the detached job. | Check `run.log` / disk / permissions. |

A run that *launches* but then fails mid-pipeline reports `❌ failed at <stage>` to the channel (not via this envelope, which has already returned). Inspect `<outdir>/run.log`.

## Acceptance test

`bash test_acceptance.sh` runs: (1) **dry-run** → `status:"planned"` with the launch command, nothing spawned; (2) **malformed** (`--sim-ps abc`/`0`) → graceful `ok:false`. A real background launch is gated behind `LIVE=1` (it takes ~10-15 min and posts to Discord).

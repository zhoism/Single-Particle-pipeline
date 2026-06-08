#!/usr/bin/env python3
"""pipeline-async wrapper — launch the full local AMBER MD pipeline in the
background and return IMMEDIATELY, so a Discord-triggered agent turn can reply
"started" without blocking (a full run is ~10-15 min, far past the 120s model
idle limit).

Design (Phase B):
  - The agent makes ONE exec call -> this wrapper -> detached `run_happy_path.sh`.
  - The detached job posts per-stage progress + the final result to Discord via
    the LLM-FREE `openclaw message send` (run_happy_path.sh NOTIFY_CHANNEL mode),
    so notifications work even while the LLM is rate-limited.
  - The detached child runs in a NEW session (start_new_session=True) so it
    survives this wrapper — and the agent's exec — exiting.

The wrapper itself touches no AMBER binary; the detached job sources
scripts/env.sh for the toolchain. So failures surface as a Discord "failed"
notice, not a silent hang.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SKILL_NAME = "pipeline-async"
ROOT = Path(__file__).resolve().parents[3]          # .../project-prime
RUN_HAPPY = ROOT / "run_happy_path.sh"
ENV_SH = ROOT / "scripts" / "env.sh"
DEFAULT_CHANNEL = "1511130059061067858"             # project Discord channel


def emit(ok, *, dry_run, run_id="", status="", outputs=None, errors=None, code=0):
    print(json.dumps({
        "ok": ok,
        "skill": SKILL_NAME,
        "dry_run": dry_run,
        "run_id": run_id,
        "status": status,
        "outputs": outputs or {},
        "errors": errors or [],
    }, indent=2))
    sys.exit(code)


def main():
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Launch the full AMBER MD pipeline in the background; "
                    "report progress + results to a Discord channel.")
    p.add_argument("--sim-ps", type=int, default=50,
                   help="Production length in ps (default 50).")
    p.add_argument("--channel", default=DEFAULT_CHANNEL,
                   help="Discord channel id to notify (default: project channel).")
    p.add_argument("--run-id", default=None,
                   help="Run label (default: timestamp). Also names the output dir.")
    p.add_argument("--output-dir", default=None,
                   help="Results dir (default: ROOT/pipeline-async-run-<run_id>).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the launch plan without launching.")
    args = p.parse_args()

    # run_id: timestamp-based. (import time lazily so --help stays cheap.)
    run_id = args.run_id
    if not run_id:
        import time
        run_id = "pa-" + time.strftime("%Y%m%d-%H%M%S")

    if args.sim_ps <= 0:
        emit(False, dry_run=args.dry_run, run_id=run_id,
             errors=[f"INVALID_INPUT: --sim-ps must be a positive int, got {args.sim_ps}"],
             code=1)

    outdir = Path(args.output_dir).expanduser().resolve() if args.output_dir \
        else ROOT / f"pipeline-async-run-{run_id}"
    log = outdir / "run.log"

    # Preflight: the spine + helper must exist (the toolchain itself is sourced
    # by env.sh inside the detached job and validated stage-by-stage there).
    missing = [str(f) for f in (RUN_HAPPY, ENV_SH) if not f.exists()]
    if missing:
        emit(False, dry_run=args.dry_run, run_id=run_id,
             errors=[f"INPUT_PREP_FAILED: missing required file(s): {missing}"], code=2)

    # The detached job: source the toolchain, set notify env, run the spine.
    inner = (
        f"source {shlex.quote(str(ENV_SH))} && "
        f"NOTIFY_CHANNEL={shlex.quote(args.channel)} RUN_ID={shlex.quote(run_id)} "
        f"bash {shlex.quote(str(RUN_HAPPY))} {args.sim_ps} {shlex.quote(str(outdir))}"
    )
    launch = ["bash", "-lc", inner]

    outputs = {
        "outdir": str(outdir),
        "log": str(log),
        "channel": args.channel,
        "sim_ps": args.sim_ps,
        "eta": "~10-15 min",
    }

    if args.dry_run:
        outputs["launch_cmd"] = launch
        emit(True, dry_run=True, run_id=run_id, status="planned",
             outputs=outputs, code=0)

    # Launch detached: new session so it outlives this wrapper AND the agent's
    # exec; stdout/stderr -> run.log; no stdin.
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        with open(log, "w") as lf:
            subprocess.Popen(
                launch, stdout=lf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(ROOT),
                start_new_session=True,
            )
    except OSError as exc:
        emit(False, dry_run=False, run_id=run_id,
             errors=[f"LAUNCH_FAILED: {exc}"], code=2)

    emit(True, dry_run=False, run_id=run_id, status="launched",
         outputs=outputs, code=0)


if __name__ == "__main__":
    main()

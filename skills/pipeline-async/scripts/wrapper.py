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
import re
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
    p.add_argument("--protein", default=None,
                   help="Protein PDB path (default: the 1L2Y fixture).")
    p.add_argument("--ligand", default=None,
                   help="Ligand .pdb/.mol2/.sdf file OR inline SMILES "
                        "(default: the 1L2Y fixture ligand).")
    p.add_argument("--charge", type=int, default=0,
                   help="Ligand net formal charge for AM1-BCC (default 0).")
    p.add_argument("--name", default="MOL",
                   help="Ligand residue name, 1-4 uppercase alnum (default MOL).")
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

    # Target validation — mirror run_happy_path.sh so a typo'd path fails as a
    # JSON envelope here, not as a cryptic obabel error inside the detached job.
    if args.protein and not Path(args.protein).expanduser().is_file():
        emit(False, dry_run=args.dry_run, run_id=run_id,
             errors=[f"INVALID_INPUT: protein PDB not found: {args.protein}"], code=1)
    if args.ligand:
        lp = Path(args.ligand).expanduser()
        ext = lp.suffix.lower()
        if ext in (".pdb", ".mol2", ".sdf"):
            # A recognized molecular file must resolve to a real file.
            if not lp.is_file():
                emit(False, dry_run=args.dry_run, run_id=run_id,
                     errors=[f"INVALID_INPUT: ligand file not found: {args.ligand}"], code=1)
        elif ext in (".mol", ".sd", ".xyz", ".pdbqt", ".smi", ".smiles",
                     ".inchi", ".ml2", ".cif", ".mae"):
            # A molecular-file extension the pipeline can't consume — reject
            # clearly instead of letting antechamber treat the path as a SMILES
            # string and fail cryptically in obabel.
            emit(False, dry_run=args.dry_run, run_id=run_id,
                 errors=[f"INVALID_INPUT: unsupported ligand file extension "
                         f"({lp.name}); pass .pdb/.mol2/.sdf or an inline SMILES"],
                 code=1)
        # else: no molecular-file extension -> treat as an inline SMILES string.
    if not re.fullmatch(r"[A-Z0-9]{1,4}", args.name):
        emit(False, dry_run=args.dry_run, run_id=run_id,
             errors=[f"INVALID_INPUT: --name must be 1-4 uppercase letters/digits, "
                     f"got {args.name!r}"], code=1)

    outdir = Path(args.output_dir).expanduser().resolve() if args.output_dir \
        else ROOT / f"pipeline-async-run-{run_id}"
    # Log lives BESIDE outdir, not inside it: run_happy_path.sh does `rm -rf $OUT`
    # at startup, which would unlink a log placed inside.
    log = outdir.parent / (outdir.name + ".log")

    # Preflight: the spine + helper must exist (the toolchain itself is sourced
    # by env.sh inside the detached job and validated stage-by-stage there).
    missing = [str(f) for f in (RUN_HAPPY, ENV_SH) if not f.exists()]
    if missing:
        emit(False, dry_run=args.dry_run, run_id=run_id,
             errors=[f"INPUT_PREP_FAILED: missing required file(s): {missing}"], code=2)

    # Target flags: only appended when the user overrode a default, so a no-target
    # launch is byte-identical to the pre-arbitrary-target detached command (and
    # run_happy_path.sh falls back to its own 1L2Y fixture defaults).
    target_flags = ""
    if args.protein:
        target_flags += f" --protein {shlex.quote(args.protein)}"
    if args.ligand:
        target_flags += f" --ligand {shlex.quote(args.ligand)}"
    if args.charge != 0:
        target_flags += f" --charge {args.charge}"
    if args.name != "MOL":
        target_flags += f" --name {shlex.quote(args.name)}"

    # The detached job: source the toolchain, set notify env, run the spine.
    inner = (
        f"source {shlex.quote(str(ENV_SH))} && "
        f"NOTIFY_CHANNEL={shlex.quote(args.channel)} RUN_ID={shlex.quote(run_id)} "
        f"bash {shlex.quote(str(RUN_HAPPY))} {args.sim_ps} {shlex.quote(str(outdir))}{target_flags}"
    )
    # NON-login shell: a login shell (-l) sources the user's profile, which here
    # switches node to a non-nvm version and breaks `openclaw` (the LLM-free
    # notify path). `bash -c` inherits the gateway's correct node + PATH; env.sh
    # adds the AMBER toolchain.
    launch = ["bash", "-c", inner]

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

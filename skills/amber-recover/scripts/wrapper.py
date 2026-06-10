#!/usr/bin/env python3
"""amber-recover wrapper (bounded AMBER MD runtime recovery).

Detects a REAL pmemd MD crash from its mdout and salvages the run through a
TIERED, mathematically-bounded recovery loop — without letting an LLM invent
physics. The LLM only picks this skill; the wrapper does all detection and
recovery deterministically.

The loop (strict-verifier / LOWE Predict -> Test -> Falsify -> Improve):

  Tier 1 (safe default)   restore the last good checkpoint (.rst) and resume the
                          crashed stage AS-IS. Handles transient failures
                          (killed job, node death, timeout) with zero physics
                          risk. Mutation is NEVER the first move.

  Tier 2 (escalation)     only if Tier 1 re-crashes: run a bounded STABILIZATION
                          window (lower dt + disable SHAKE) from the last good
                          checkpoint; once it survives, RESTORE the original sane
                          parameters and resume. dt is halved down a ladder,
                          floored at --dt-floor; SHAKE-off forces dt <= 1 fs
                          (the check_amber bound). Every mutated namelist is
                          gated by the SAME deterministic validator the rest of
                          the pipeline uses (check_amber_vendored) -- the skill
                          cannot emit a physically-impossible namelist.

  HALT (bounded)          if the dt floor is reached, the bound forbids the only
                          fix, or the retry budget is exhausted -> structured
                          ok:false with a needs_human block. Halting IS the
                          correct behaviour; that is the bounded guarantee.

Detection is DETERMINISTIC (regex/numeric parse of the mdout), never agentic.
See references/recovery-loop.md and the vault notes Skill_Bounded_Recovery_AMBER
+ Workflow_Error_Recovery_Loop for the design this implements.

One exec call per skill turn. JSON envelope to stdout; progress to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_amber_vendored as cav  # noqa: E402  (vendored bounds validator)

SKILL_NAME = "amber-recover"

# ---- Hard bounds (reuse check_amber's limits; do NOT invent new ones) -------
DT_SHAKEOFF_CAP = cav.DT_MAX_NOSHAKE   # 0.001 ps: SHAKE off requires dt <= 1 fs
DEFAULT_DT_FLOOR = 0.0005              # 0.5 fs conservative floor (SOP #3)
DEFAULT_STABILIZE_STEPS = 2000
DEFAULT_MAX_TIER2 = 4
GAMMA_LN_CAP = cav.GAMMA_LN_MAX        # 5.0: stay in check_amber's typical range
CUT_MIN, CUT_MAX = cav.CUT_MIN, cav.CUT_MAX

# Default amber-md-run chain: which stage reads which predecessor checkpoint.
CHAIN_PREDECESSOR = {
    "min1": "TOP_CRD", "min2": "min1", "min3": "min2",
    "heat": "min3", "density": "heat", "product": "density",
}
CHAIN_ORDER = ["min1", "min2", "min3", "heat", "density", "product"]
TOP_NAME, CRD_NAME = "comp_oct.top", "comp_oct.crd"


# ---- Envelope ---------------------------------------------------------------

def envelope(ok: bool, dry_run: bool,
             outputs: dict[str, Any] | None = None,
             validation: dict[str, Any] | None = None,
             errors: list[str] | None = None) -> str:
    return json.dumps({
        "ok": ok, "skill": SKILL_NAME, "dry_run": dry_run,
        "outputs": outputs or {}, "validation": validation or {},
        "errors": errors or [],
    }, indent=2)


def emit_and_exit(*, ok: bool, dry_run: bool,
                  outputs: dict[str, Any] | None = None,
                  validation: dict[str, Any] | None = None,
                  errors: list[str] | None = None, code: int = 0) -> None:
    print(envelope(ok=ok, dry_run=dry_run, outputs=outputs,
                   validation=validation, errors=errors))
    sys.exit(code)


def log(msg: str) -> None:
    print(f"[{SKILL_NAME}] {msg}", file=sys.stderr)


# ============================================================================
# DETERMINISTIC FAILURE DETECTOR
# ============================================================================
# A pmemd run is CRASHED iff any of:
#   - no normal-termination banner ("Total wall time" / "FINAL RESULTS" / ...)
#   - non-zero return code (when we ran it ourselves)
#   - NaN anywhere in the mdout (NaN is *sticky* -- it propagates to every later
#     energy, so a run that prints NaN and STILL finishes is numerical garbage;
#     this is the silent-pass class that rc + banner alone would miss)
#   - a SHAKE convergence failure ("coordinate resetting cannot be accomplished")
#   - the FINAL energy block is non-finite (NaN / ****** overflow)
#   - a temperature blow-up (TEMP(K) > 1000 K or non-finite)
#   - an abnormal-termination / box / setup error
# A finite "vlimit exceeded" (velocity clamp) and a TRANSIENT early ****** that
# the run later recovers from are NOT fatal on their own -- a successful
# stabilization of a strained geometry legitimately shows both.

DONE_RE = re.compile(
    r"Total wall time|Final Performance Info|Maximum number of minimization|FINAL RESULTS",
    re.I)
NSTEP_RE = re.compile(r"NSTEP\s*=\s*(\d+)")
NAN_RE = re.compile(r"\bNaN\b")
# gfortran prints true IEEE infinity as the literal token "Infinity"/"-Infinity"
# in a formatted field (it does NOT degrade to ******), so a run diverging to inf
# must be caught here exactly like NaN — both are non-finite garbage.
INF_RE = re.compile(r"[-+]?Infinity\b", re.I)
SHAKE_RE = re.compile(
    r"coordinate resetting cannot be accomplished"
    r"|shake\b[^\n]*(?:fail|could not|not converge|too many)", re.I)
ABNORMAL_RE = re.compile(r"terminated abnormally", re.I)
BOX_RE = re.compile(
    r"periodic box[^\n]*changed too much|box[^\n]*too (?:small|big)"
    r"|skinnb|unit cell[^\n]*too small", re.I)
OVERFLOW_RE = re.compile(r"\*{6,}")                       # ****** field overflow
VLIMIT_RE = re.compile(r"vlimit exceeded[^\n]*", re.I)
# stderr IEEE flags: only INVALID/OVERFLOW/DIVIDE matter; UNDERFLOW/DENORMAL are
# benign gfortran noise emitted by healthy runs too.
FPE_RE = re.compile(r"IEEE_(?:INVALID|OVERFLOW|DIVIDE_BY_ZERO)", re.I)
TEMP_RE = re.compile(r"TEMP\(K\)\s*=\s*(NaN|\*+|[-\d.]+)", re.I)
TEMP_BLOWUP_K = 1000.0

# An energy block = the text from one "NSTEP =" line up to the next blank-ish
# separator. We only need the LAST block's finiteness.
ENERGY_BLOCK_SPLIT = re.compile(r"(?=NSTEP\s*=\s*\d+)")


def _last_energy_block(out_text: str) -> str:
    blocks = [b for b in ENERGY_BLOCK_SPLIT.split(out_text) if NSTEP_RE.search(b)]
    return blocks[-1][:1200] if blocks else ""


def detect_failure(out_text: str, stderr_text: str = "", rc: int | None = None
                   ) -> dict[str, Any]:
    """Classify a pmemd run from its mdout (+ optional stderr / return code).

    Returns a detection dict. `crashed` is the deterministic verdict; the rest
    is diagnostic and goes into the envelope for the human."""
    signatures: list[str] = []

    finished = bool(DONE_RE.search(out_text))
    has_nan = bool(NAN_RE.search(out_text))
    has_inf = bool(INF_RE.search(out_text))
    has_shake = bool(SHAKE_RE.search(out_text) or SHAKE_RE.search(stderr_text))
    has_abnormal = bool(ABNORMAL_RE.search(out_text) or ABNORMAL_RE.search(stderr_text))
    has_box = bool(BOX_RE.search(out_text) or BOX_RE.search(stderr_text))
    has_overflow = bool(OVERFLOW_RE.search(out_text))
    vlimit_hits = VLIMIT_RE.findall(out_text)
    has_fpe = bool(FPE_RE.search(stderr_text))

    nsteps = [int(n) for n in NSTEP_RE.findall(out_text)]
    crash_nstep = nsteps[-1] if nsteps else None

    last_block = _last_energy_block(out_text)
    final_block_bad = bool(last_block and (NAN_RE.search(last_block)
                                           or INF_RE.search(last_block)
                                           or OVERFLOW_RE.search(last_block)))

    # temperature blow-up (numeric or non-finite)
    temp_blowup = False
    for t in TEMP_RE.findall(out_text):
        if t.lower() == "nan" or t.startswith("*"):
            temp_blowup = True
            break
        try:
            if abs(float(t)) > TEMP_BLOWUP_K:
                temp_blowup = True
                break
        except ValueError:
            pass

    # diagnostic signature labels
    if has_nan:
        signatures.append("NAN_ENERGY")
    if has_inf:
        signatures.append("INF_ENERGY")
    if has_shake:
        signatures.append("SHAKE_FAILED")
    if has_overflow:
        signatures.append("COORD_OVERFLOW")
    if vlimit_hits:
        signatures.append("VLIMIT_EXCEEDED")
    if temp_blowup:
        signatures.append("TEMP_BLOWUP")
    if has_box:
        signatures.append("BOX_ERROR")
    if has_abnormal:
        signatures.append("ABNORMAL_TERMINATION")
    if has_fpe:
        signatures.append("FP_EXCEPTION")
    if not finished:
        signatures.append("NO_NORMAL_TERMINATION")
    if rc is not None and rc != 0:
        signatures.append(f"NONZERO_RC({rc})")

    # The deterministic crash verdict (fatal signals only).
    crashed = bool(
        (not finished)
        or (rc is not None and rc != 0)
        or has_nan
        or has_inf
        or has_shake
        or has_abnormal
        or has_box
        or temp_blowup
        or final_block_bad
    )

    # classification (diagnostic): why it crashed / whether Tier 1 might suffice.
    fatal_instability = (has_nan or has_inf or has_shake or has_box or temp_blowup
                         or final_block_bad)
    if not crashed:
        classification = "HEALTHY"
    elif fatal_instability:
        classification = "INSTABILITY"      # Tier 1 will likely re-crash -> Tier 2
    else:
        classification = "INCOMPLETE"       # transient/killed -> Tier 1 should fix

    return {
        "crashed": crashed,
        "finished": finished,
        "classification": classification,
        "signatures": sorted(set(signatures)),
        "crash_nstep": crash_nstep,
        "vlimit_clamps": len(vlimit_hits),
        "final_block_finite": (not final_block_bad) if last_block else None,
        "rc": rc,
    }


# ============================================================================
# NAMELIST PARSE + GENERATE
# ============================================================================

def parse_cntrl(in_path: Path) -> dict[str, str]:
    text = in_path.read_text(errors="replace")
    m = re.search(r"&cntrl(.*?)/", text, re.DOTALL | re.IGNORECASE)
    body = re.sub(r"!.*", "", m.group(1) if m else text)
    kv: dict[str, str] = {}
    for k, v in re.findall(r"(\w+)\s*=\s*('[^']*'|\"[^\"]*\"|[-\d.eE+]+)", body):
        kv[k.lower()] = v.strip().strip("'\"")
    return kv


def fnum(kv: dict[str, str], key: str, default: float) -> float:
    try:
        return float(kv[key])
    except (KeyError, ValueError):
        return default


def fmt_dt(dt: float) -> str:
    # exact, no float dust (mirrors the engine-seam discipline of amber-md-run)
    return f"{dt:.6f}".rstrip("0").rstrip(".") or "0"


def gen_stabilize_namelist(*, dt: float, steps: int, cut: float, temp0: float,
                           gamma_ln: float, restraint_wt: float | None,
                           restraintmask: str | None) -> str:
    """A bounded NVT relaxation: tiny dt + SHAKE OFF, everything else conservative.
    Only dt + the SHAKE flags differ from a normal stage; SHAKE-off removes the
    convergence-failure mode and the tiny dt keeps the strained geometry from
    overflowing while Langevin friction bleeds off the excess."""
    restraint = ""
    if restraint_wt and restraint_wt > 0 and restraintmask:
        restraint = (f"  ntr=1, restraint_wt={restraint_wt}, "
                     f"restraintmask='{restraintmask}',\n")
    ntpr = max(1, steps // 5)
    return (
        "bounded-recovery stabilization: tiny dt + SHAKE off, NVT relax\n"
        " &cntrl\n"
        "  imin=0, irest=0, ntx=1,\n"
        f"  nstlim={steps}, dt={fmt_dt(dt)},\n"
        "  ntc=1, ntf=1,\n"
        f"  cut={cut},\n"
        "  ntb=1, ntp=0,\n"
        f"  ntt=3, gamma_ln={gamma_ln}, ig=-1,\n"
        f"  tempi=0.0, temp0={temp0},\n"
        f"{restraint}"
        f"  ntpr={ntpr}, ntwx=0, ntwr={steps},\n"
        " /\n"
    )


# ============================================================================
# ENGINE + STAGE EXECUTION
# ============================================================================

def resolve_engine(engine: str, engine_home: str | None) -> str | None:
    if engine_home:
        cand = Path(engine_home).expanduser() / "bin" / engine
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which(engine)


def run_stage(*, engine_path: str, launcher: list[str], cwd: Path, in_name: str,
              out_name: str, coords_name: str, rst_name: str,
              ref_name: str | None = None, traj_name: str | None = None,
              is_min: bool) -> tuple[int, str]:
    """Run one pmemd invocation inside cwd with bare filenames (space-safe)."""
    cmd = list(launcher) + ["-O", "-i", in_name, "-o", out_name,
                            "-p", TOP_NAME, "-c", coords_name, "-r", rst_name]
    if not is_min and traj_name:
        cmd += ["-x", traj_name]
    if ref_name:
        cmd += ["-ref", ref_name]
    proc = subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, errors="replace")
    return proc.returncode, proc.stderr


def read_out(cwd: Path, out_name: str) -> str:
    p = cwd / out_name
    return p.read_text(errors="replace") if p.exists() else ""


# ============================================================================
# CHECKPOINT LOCATION + VALIDATION
# ============================================================================

def locate_checkpoint(md_dir: Path, stage: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    pred = CHAIN_PREDECESSOR.get(stage)
    if pred is None:
        return None
    return CRD_NAME if pred == "TOP_CRD" else f"{pred}.rst"


def checkpoint_status(path: Path) -> tuple[bool, str]:
    """(usable, reason). Light validity check: exists, non-empty, and -- for an
    ASCII inpcrd -- coordinates are finite (NetCDF restarts are trusted by
    existence+size; pmemd validates them on read)."""
    if not path.exists():
        return False, "missing"
    if path.stat().st_size == 0:
        return False, "empty"
    head = path.read_bytes()[:4096]
    if head[:3] == b"CDF" or head[:4] == b"\x89HDF":
        return True, "netcdf"                      # binary restart; trust + let pmemd read
    try:
        text = head.decode("ascii", errors="replace")
    except Exception:
        return True, "binary"
    if NAN_RE.search(text) or OVERFLOW_RE.search(text) or "inf" in text.lower():
        return False, "non-finite-coords"          # the checkpoint itself is destroyed
    return True, "ascii"


# ============================================================================
# MAIN RECOVERY LOOP
# ============================================================================

def main() -> None:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Detect a real pmemd MD crash and salvage it via a tiered, "
                    "bounded recovery loop (Tier 1 resume-as-is; Tier 2 bounded "
                    "dt/SHAKE stabilize-then-restore; halt on bounded limit).")
    p.add_argument("--md-dir", required=True,
                   help="amber-md-run working dir (namelists, comp_oct.top, "
                        "restart chain, crashed stage's .out).")
    p.add_argument("--stage", default=None,
                   help="Crashed stage (heat/density/product/...). "
                        "Auto-detected from the last stage with an .out if omitted.")
    p.add_argument("--checkpoint", default=None,
                   help="Last-good restart/coords to rewind to. Inferred from the "
                        "amber-md-run chain if omitted (e.g. heat<-min3.rst).")
    p.add_argument("--dt-floor", type=float, default=DEFAULT_DT_FLOOR,
                   help="Conservative dt floor in ps (default 0.0005 = 0.5 fs). "
                        "A fix that would need dt below this HALTs.")
    p.add_argument("--stabilize-steps", type=int, default=DEFAULT_STABILIZE_STEPS,
                   help="Steps the SHAKE-off stabilization window must survive.")
    p.add_argument("--max-tier2-attempts", type=int, default=DEFAULT_MAX_TIER2,
                   help="dt-halving budget before HALT.")
    p.add_argument("--engine", default="pmemd",
                   choices=["pmemd", "pmemd.MPI", "sander"])
    p.add_argument("--engine-home", default="~/Downloads/pmemd26")
    p.add_argument("--ncpus", type=int, default=4)
    p.add_argument("--detect-only", action="store_true",
                   help="Only classify the crashed stage's .out; no recovery.")
    p.add_argument("--dry-run", action="store_true",
                   help="Detect + plan the Tier-1/Tier-2 recovery (and bounds-check "
                        "the mutated namelist) without executing pmemd.")
    args = p.parse_args()
    # A dt floor of 0 (with a large retry budget) would march the ladder to
    # absurd sub-fs values; clamp to a hard physical minimum (0.1 fs).
    args.dt_floor = max(args.dt_floor, 1e-4)

    md_dir = Path(args.md_dir).expanduser().resolve()
    if not md_dir.is_dir():
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=[f"INVALID_INPUT: --md-dir not a directory: {md_dir}"],
                      code=1)

    # ---- resolve the crashed stage -----------------------------------------
    stage = args.stage
    if stage is None:
        present = [s for s in CHAIN_ORDER if (md_dir / f"{s}.out").exists()]
        if not present:
            emit_and_exit(ok=False, dry_run=args.dry_run,
                          errors=["INVALID_INPUT: no <stage>.out in --md-dir and "
                                  "no --stage given"], code=1)
        stage = present[-1]
    out_name = f"{stage}.out"
    if not (md_dir / out_name).exists():
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=[f"INVALID_INPUT: {out_name} not found in {md_dir}"],
                      code=1)

    # ---- DETECT (deterministic) --------------------------------------------
    # also fold in run.log (amber-md-run's combined stderr) if present
    run_log = read_out(md_dir, "run.log")
    detection = detect_failure(read_out(md_dir, out_name), stderr_text=run_log)
    log(f"stage={stage} detection={detection['classification']} "
        f"signatures={detection['signatures']}")

    if args.detect_only:
        # Detection needs only the .out; no namelist / recovery required.
        emit_and_exit(ok=detection["crashed"], dry_run=True,
                      outputs={"stage": stage}, validation={"detection": detection},
                      errors=[] if detection["crashed"]
                             else ["NO_FAILURE_DETECTED"],
                      code=0)

    in_name = f"{stage}.in"
    if not (md_dir / in_name).exists():
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=[f"INVALID_INPUT: {in_name} (the crashed stage's "
                              f"namelist) not found in {md_dir}"],
                      code=1)

    if not detection["crashed"]:
        # Honesty gate: refuse to "recover" a healthy run.
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      outputs={"stage": stage},
                      validation={"detection": detection},
                      errors=["NO_FAILURE_DETECTED: stage reached normal "
                              "termination with no instability signature; nothing "
                              "to recover"], code=2)

    # ---- locate + validate the last-good checkpoint ------------------------
    ckpt_name = locate_checkpoint(md_dir, stage, args.checkpoint)
    if ckpt_name is None:
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      validation={"detection": detection},
                      errors=[f"INVALID_INPUT: cannot infer a checkpoint for stage "
                              f"{stage!r}; pass --checkpoint"], code=1)
    ckpt_path = md_dir / ckpt_name
    usable, reason = checkpoint_status(ckpt_path)
    if not usable:
        if reason == "non-finite-coords":
            # The checkpoint itself is destroyed -> no dt recovers it. HALT.
            needs_human = {
                "reason": "UNRECOVERABLE_CHECKPOINT",
                "detail": f"last-good checkpoint {ckpt_name} has non-finite "
                          "coordinates; the saved state is already destroyed",
                "checkpoint": ckpt_name,
                "recommendation": "rewind to an earlier known-good restart or "
                                  "re-run the preceding stage; recovery cannot "
                                  "integrate from a NaN/overflow checkpoint",
            }
            emit_and_exit(ok=False, dry_run=args.dry_run,
                          outputs={"stage": stage, "needs_human": needs_human},
                          validation={"detection": detection},
                          errors=["RECOVERY_HALTED: UNRECOVERABLE_CHECKPOINT"],
                          code=3)
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      validation={"detection": detection},
                      errors=[f"INVALID_INPUT: checkpoint {ckpt_name} {reason}"],
                      code=1)

    # ---- parse the crashed stage's namelist --------------------------------
    kv = parse_cntrl(md_dir / in_name)
    orig_dt = fnum(kv, "dt", 0.002)
    is_min = int(fnum(kv, "imin", 0)) == 1
    cut = fnum(kv, "cut", 9.0)
    cut = max(CUT_MIN, min(CUT_MAX, cut))
    temp0 = fnum(kv, "temp0", 300.0)
    gamma_ln = min(fnum(kv, "gamma_ln", 2.0), GAMMA_LN_CAP)
    if not (cav.GAMMA_LN_MIN <= gamma_ln <= GAMMA_LN_CAP):
        gamma_ln = 2.0
    ntr = int(fnum(kv, "ntr", 0))
    restraint_wt = fnum(kv, "restraint_wt", 0.0) if ntr == 1 else 0.0
    restraintmask = kv.get("restraintmask") if ntr == 1 else None

    engine_path = resolve_engine(args.engine, args.engine_home)
    if engine_path is None and not args.dry_run:
        emit_and_exit(ok=False, dry_run=False,
                      validation={"detection": detection},
                      errors=[f"MISSING_BINARY: engine {args.engine!r} not in "
                              f"{args.engine_home}/bin or on PATH"], code=1)
    launcher = ([f"mpirun", "-np", str(args.ncpus), engine_path]
                if args.engine == "pmemd.MPI" else [engine_path or args.engine])

    bounds_checked: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    def gate_namelist(name: str, text: str) -> bool:
        """Write a candidate namelist, run the SAME deterministic validator the
        rest of the pipeline uses, and refuse to execute it if it FAILs a hard
        physical-realism bound. This is the 'AI cannot invent impossible
        parameters' guarantee, made deterministic."""
        path = md_dir / name
        path.write_text(text)
        rep = cav.check_amber_in(path)
        passed = not rep.has_fail
        bounds_checked.append({
            "namelist": name, "pass": passed,
            "findings": [f"{f.level}:{f.rule}" for f in rep.findings
                         if f.level in ("FAIL", "WARN")],
        })
        return passed

    # ---- if minimization stage: Tier 2 (dt/SHAKE) does not apply -----------
    # (a failed minimization is an input/topology problem, not a dt instability.)

    # ---- bounds-gate the ORIGINAL namelist we are about to resume ----------
    # Tier 1 and the Tier-2 restore run the crashed stage's own namelist verbatim,
    # so the bounded guarantee must cover it too -- not only the mutations. If the
    # stage itself is out of physical-realism bounds, that is a human's call -> HALT
    # rather than auto-resume a physically-impossible namelist.
    orig_rep = cav.check_amber_in(md_dir / in_name)
    orig_pass = not orig_rep.has_fail
    bounds_checked.append({
        "namelist": in_name, "role": "original_resumed", "pass": orig_pass,
        "findings": [f"{f.level}:{f.rule}" for f in orig_rep.findings
                     if f.level in ("FAIL", "WARN")],
    })
    if not orig_pass:
        needs_human = {
            "reason": "ORIGINAL_NAMELIST_OUT_OF_BOUNDS",
            "detail": f"the crashed stage's own namelist {in_name} FAILs check_amber "
                      "(a physical-realism bound), so resuming/restoring it verbatim "
                      "would run an out-of-bounds simulation",
            "namelist": in_name,
            "findings": [f"{f.level}:{f.rule} — {f.detail}" for f in orig_rep.findings
                         if f.level == "FAIL"],
            "recommendation": "fix the stage's namelist to satisfy the bounds "
                              "(dt, cut, SHAKE) before recovering; recovery will not "
                              "auto-run a physically-impossible namelist",
        }
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      outputs={"stage": stage, "needs_human": needs_human},
                      validation={"detection": detection,
                                  "bounds": {"checked": bounds_checked, "all_pass": False}},
                      errors=["RECOVERY_HALTED: ORIGINAL_NAMELIST_OUT_OF_BOUNDS"],
                      code=3)

    # ========================================================================
    # DRY-RUN: detect + plan, no pmemd
    # ========================================================================
    if args.dry_run:
        plan: dict[str, Any] = {
            "stage": stage, "checkpoint": ckpt_name,
            "tier1": {"action": "resume crashed stage as-is from checkpoint",
                      "namelist": in_name},
        }
        if not is_min:
            stab_dt = min(orig_dt / 2.0, DT_SHAKEOFF_CAP)
            if stab_dt >= args.dt_floor:
                stab_text = gen_stabilize_namelist(
                    dt=stab_dt, steps=args.stabilize_steps, cut=cut, temp0=temp0,
                    gamma_ln=gamma_ln, restraint_wt=restraint_wt,
                    restraintmask=restraintmask)
                ok_bounds = gate_namelist("recover_stab_plan.in", stab_text)
                plan["tier2"] = {
                    "action": "stabilize (SHAKE off, tiny dt) then restore sane params",
                    "stabilize_dt": stab_dt, "dt_floor": args.dt_floor,
                    "bounds_pass": ok_bounds, "namelist": "recover_stab_plan.in",
                }
            else:
                plan["tier2"] = {"action": "HALT: even dt/2 is below --dt-floor",
                                 "dt_floor": args.dt_floor}
        else:
            plan["tier2"] = {"action": "n/a (minimization stage; not dt-recoverable)"}
        emit_and_exit(ok=True, dry_run=True, outputs={"plan": plan},
                      validation={"detection": detection,
                                  "bounds": {"checked": bounds_checked}},
                      errors=[], code=0)

    # ========================================================================
    # TIER 1 — resume the crashed stage AS-IS from the last-good checkpoint
    # ========================================================================
    log(f"Tier 1: resume {stage} as-is from {ckpt_name}")
    t1_rst = f"recover_t1_{stage}.rst"
    t1_out = f"recover_t1_{stage}.out"
    t1_ref = ckpt_name if ntr == 1 else None
    rc1, err1 = run_stage(engine_path=engine_path, launcher=launcher, cwd=md_dir,
                          in_name=in_name, out_name=t1_out, coords_name=ckpt_name,
                          rst_name=t1_rst, ref_name=t1_ref,
                          traj_name=f"recover_t1_{stage}.nc", is_min=is_min)
    d1 = detect_failure(read_out(md_dir, t1_out), stderr_text=err1, rc=rc1)
    attempts.append({"tier": 1, "action": "resume_as_is", "rc": rc1,
                     "classification": d1["classification"],
                     "signatures": d1["signatures"], "crash_nstep": d1["crash_nstep"]})
    if not d1["crashed"]:
        log("Tier 1 RECOVERED")
        emit_and_exit(ok=True, dry_run=False,
                      outputs={"recovered": True, "tier": 1, "stage": stage,
                               "checkpoint": ckpt_name,
                               "final_rst": str(md_dir / t1_rst),
                               "attempts": attempts},
                      validation={"detection": detection,
                                  "bounds": {"checked": bounds_checked, "all_pass": True}},
                      errors=[], code=0)

    # Tier 1 re-crashed. Mutation is the escalation, never the first move.
    if is_min:
        needs_human = {
            "reason": "MIN_STAGE_NOT_DT_RECOVERABLE",
            "detail": f"minimization stage {stage} re-crashed on resume; a failed "
                      "minimization is an input/topology problem, not a dt "
                      "instability, so bounded dt/SHAKE mutation does not apply",
            "checkpoint": ckpt_name,
            "recommendation": "inspect the topology/structure (clashes, missing "
                              "params, bad box) for this stage",
        }
        emit_and_exit(ok=False, dry_run=False,
                      outputs={"stage": stage, "needs_human": needs_human,
                               "attempts": attempts},
                      validation={"detection": detection,
                                  "bounds": {"checked": bounds_checked}},
                      errors=["RECOVERY_HALTED: MIN_STAGE_NOT_DT_RECOVERABLE"], code=3)

    # ========================================================================
    # TIER 2 — bounded stabilize-then-restore ladder
    # ========================================================================
    log("Tier 1 re-crashed -> escalating to Tier 2 (bounded dt/SHAKE)")
    halt_reason = None
    for attempt in range(1, args.max_tier2_attempts + 1):
        stab_dt = min(orig_dt / (2.0 ** attempt), DT_SHAKEOFF_CAP)
        if stab_dt < args.dt_floor - 1e-12:
            halt_reason = (f"DT_FLOOR_REACHED: a SHAKE-off fix needs dt<={DT_SHAKEOFF_CAP} "
                           f"and the ladder would go below the --dt-floor "
                           f"{args.dt_floor} ps; refusing to breach the bound")
            break

        stab_in = f"recover_stab{attempt}.in"
        stab_text = gen_stabilize_namelist(
            dt=stab_dt, steps=args.stabilize_steps, cut=cut, temp0=temp0,
            gamma_ln=gamma_ln, restraint_wt=restraint_wt, restraintmask=restraintmask)
        if not gate_namelist(stab_in, stab_text):
            halt_reason = (f"BOUNDS_VIOLATION: the stabilization namelist at "
                           f"dt={stab_dt} failed check_amber; refusing to run an "
                           "out-of-bounds mutation")
            break

        log(f"Tier 2 attempt {attempt}: stabilize dt={stab_dt} ps, SHAKE off")
        stab_rst = f"recover_stab{attempt}.rst"
        stab_out = f"recover_stab{attempt}.out"
        rc_s, err_s = run_stage(
            engine_path=engine_path, launcher=launcher, cwd=md_dir, in_name=stab_in,
            out_name=stab_out, coords_name=ckpt_name, rst_name=stab_rst,
            ref_name=(ckpt_name if restraint_wt > 0 else None), is_min=False)
        ds = detect_failure(read_out(md_dir, stab_out), stderr_text=err_s, rc=rc_s)
        attempts.append({"tier": 2, "phase": "stabilize", "attempt": attempt,
                         "dt": stab_dt, "rc": rc_s,
                         "classification": ds["classification"],
                         "signatures": ds["signatures"],
                         "vlimit_clamps": ds["vlimit_clamps"]})
        if ds["crashed"]:
            log(f"  stabilization at dt={stab_dt} still crashed -> lower dt")
            continue

        # Stabilized. RESTORE the original sane parameters from the relaxed state
        # (run the crashed stage's namelist VERBATIM from the stabilized coords).
        log(f"  stabilized; restoring sane params ({in_name}) from relaxed coords")
        restore_rst = f"recover_final.rst"
        restore_out = f"recover_restore.out"
        rc_r, err_r = run_stage(
            engine_path=engine_path, launcher=launcher, cwd=md_dir, in_name=in_name,
            out_name=restore_out, coords_name=stab_rst, rst_name=restore_rst,
            ref_name=(stab_rst if ntr == 1 else None),
            traj_name="recover_final.nc", is_min=False)
        dr = detect_failure(read_out(md_dir, restore_out), stderr_text=err_r, rc=rc_r)
        attempts.append({"tier": 2, "phase": "restore", "attempt": attempt,
                         "dt": orig_dt, "rc": rc_r,
                         "classification": dr["classification"],
                         "signatures": dr["signatures"]})
        if not dr["crashed"]:
            log(f"Tier 2 RECOVERED (stabilized@dt={stab_dt}, restored sane params)")
            emit_and_exit(ok=True, dry_run=False,
                          outputs={"recovered": True, "tier": 2, "stage": stage,
                                   "checkpoint": ckpt_name, "stabilize_dt": stab_dt,
                                   "stabilize_steps": args.stabilize_steps,
                                   "final_rst": str(md_dir / restore_rst),
                                   "attempts": attempts},
                          validation={"detection": detection,
                                      "bounds": {"checked": bounds_checked,
                                                 "all_pass": all(b["pass"] for b in bounds_checked)}},
                          errors=[], code=0)
        log(f"  restore at sane dt still crashed -> lower stabilization dt")
        # fall through: next attempt lowers dt further

    if halt_reason is None:
        halt_reason = (f"RECOVERY_EXHAUSTED: {args.max_tier2_attempts} bounded "
                       "Tier-2 attempts did not stabilize the run")

    needs_human = {
        "reason": halt_reason.split(":", 1)[0],
        "detail": halt_reason,
        "checkpoint": ckpt_name,
        "crash_signatures": detection["signatures"],
        "dt_floor": args.dt_floor,
        "attempts": attempts,
        "recommendation": "the system did not stabilize within the physical-realism "
                          "bounds; a human should inspect the structure/topology "
                          "(severe clash, bad parameters) before any further, "
                          "out-of-bounds intervention",
    }
    log(f"HALT: {halt_reason}")
    emit_and_exit(ok=False, dry_run=False,
                  outputs={"recovered": False, "stage": stage,
                           "needs_human": needs_human, "attempts": attempts},
                  validation={"detection": detection,
                              "bounds": {"checked": bounds_checked,
                                         "all_pass": all(b["pass"] for b in bounds_checked)
                                         if bounds_checked else True}},
                  errors=[f"RECOVERY_HALTED: {needs_human['reason']}"], code=3)


if __name__ == "__main__":
    main()

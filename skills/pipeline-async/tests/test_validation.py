#!/usr/bin/env python3
"""Deterministic validation test for pipeline-async's wrapper.

This skill LAUNCHES a detached background MD run, so we exercise it the only
safe way: as a SUBPROCESS, asserting the JSON envelope it prints on stdout.
Every expected value is hand-derived from reading wrapper.main()/emit() — NOT by
re-calling the function under test.

Key facts read out of wrapper.py (do not recompute):
  - emit() prints one JSON object: keys ok/skill/dry_run/run_id/status/outputs/
    errors, then sys.exit(code).  skill == "pipeline-async".
  - main() flags: --sim-ps(int,default 50) --protein --ligand --charge(int)
    --name(default "MOL") --channel --run-id --output-dir --dry-run.
  - --sim-ps <= 0  -> ok:false, errors[0] startswith
        "INVALID_INPUT: --sim-ps must be a positive int", code 1.
  - missing --protein file -> ok:false, errors[0] startswith
        "INVALID_INPUT: protein PDB not found:", code 1.
  - --name not matching ^[A-Z0-9]{1,4}$ -> ok:false, errors[0] startswith
        "INVALID_INPUT: --name must be 1-4 uppercase letters/digits", code 1.
  - --dry-run (valid args, with run-happy + env.sh present in the repo) ->
        ok:true, status "planned", outputs has "launch_cmd", code 0, and
        NO detached launch / NO outdir created (Popen is past the dry-run branch).

Run: MPLBACKEND=Agg \
  /opt/homebrew/Caskroom/miniforge/base/envs/prime-amber/bin/python test_validation.py
(exit 0 = all pass).
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRAPPER = HERE.parent / "scripts" / "wrapper.py"   # the unit under test
PY = sys.executable                                # the conda env python running us

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")


def run_wrapper(args):
    """Invoke wrapper.py as a subprocess; return (returncode, parsed_json|None,
    stdout, stderr). Parsed JSON is None if stdout isn't valid JSON."""
    proc = subprocess.run(
        [PY, str(WRAPPER), *args],
        capture_output=True, text=True, timeout=60,
    )
    try:
        envelope = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        envelope = None
    return proc.returncode, envelope, proc.stdout, proc.stderr


def has_traceback(*streams) -> bool:
    """Did a raw Python traceback leak (i.e. an UNgraceful failure)?"""
    blob = "\n".join(s or "" for s in streams)
    return ("Traceback (most recent call last)" in blob)


# --------------------------------------------------------------------------
# Case (a): --dry-run with otherwise-valid args -> ok:true, plan, no launch.
# --------------------------------------------------------------------------
def test_dry_run_plan():
    rc, env, out, err = run_wrapper(["--dry-run", "--sim-ps", "5"])
    check("a.json_parsed", env is not None, f"stdout not JSON: {out!r}")
    if env is None:
        return
    check("a.rc0", rc == 0, f"returncode={rc} stderr={err!r}")
    check("a.ok_true", env.get("ok") is True, repr(env.get("ok")))
    check("a.skill", env.get("skill") == "pipeline-async", repr(env.get("skill")))
    check("a.dry_run_true", env.get("dry_run") is True, repr(env.get("dry_run")))
    check("a.status_planned", env.get("status") == "planned", repr(env.get("status")))
    check("a.no_errors", env.get("errors") == [], repr(env.get("errors")))
    outputs = env.get("outputs") or {}
    # dry-run is the ONLY branch that adds "launch_cmd" to outputs.
    check("a.has_launch_cmd", "launch_cmd" in outputs, repr(list(outputs)))
    # sim_ps echoed back as the int we passed.
    check("a.sim_ps_echo", outputs.get("sim_ps") == 5, repr(outputs.get("sim_ps")))
    # launch_cmd is ["bash","-c", inner] per main(); inner must NOT have launched.
    lc = outputs.get("launch_cmd")
    check("a.launch_cmd_shape",
          isinstance(lc, list) and len(lc) == 3 and lc[0] == "bash" and lc[1] == "-c",
          repr(lc))
    # No detached launch: the dry-run branch returns BEFORE outdir.mkdir(), so the
    # planned outdir must not exist on disk.
    odir = outputs.get("outdir")
    check("a.no_outdir_created",
          odir is not None and not Path(odir).exists(),
          f"outdir unexpectedly created: {odir}")
    check("a.no_traceback", not has_traceback(out, err), err[-400:])


# --------------------------------------------------------------------------
# Case (b): --sim-ps 0 -> ok:false, graceful (no traceback leak). Also test -3.
# --------------------------------------------------------------------------
def test_sim_ps_nonpositive():
    for label, val in (("zero", "0"), ("neg", "-3")):
        rc, env, out, err = run_wrapper(["--dry-run", "--sim-ps", val])
        check(f"b.{label}.json_parsed", env is not None, f"stdout not JSON: {out!r}")
        if env is None:
            continue
        check(f"b.{label}.ok_false", env.get("ok") is False, repr(env.get("ok")))
        check(f"b.{label}.rc1", rc == 1, f"returncode={rc}")
        errs = env.get("errors") or []
        check(f"b.{label}.has_err", len(errs) == 1, repr(errs))
        if errs:
            check(f"b.{label}.err_substr",
                  errs[0].startswith("INVALID_INPUT: --sim-ps must be a positive int"),
                  repr(errs[0]))
        # GRACEFUL: no raw Python traceback may leak.
        check(f"b.{label}.no_traceback", not has_traceback(out, err), err[-400:])


# --------------------------------------------------------------------------
# Case (c): nonexistent --protein path -> ok:false, graceful error.
# --------------------------------------------------------------------------
def test_protein_missing():
    with tempfile.TemporaryDirectory() as td:
        ghost = str(Path(td) / "does_not_exist_protein.pdb")
        rc, env, out, err = run_wrapper(["--dry-run", "--protein", ghost])
        check("c.json_parsed", env is not None, f"stdout not JSON: {out!r}")
        if env is None:
            return
        check("c.ok_false", env.get("ok") is False, repr(env.get("ok")))
        check("c.rc1", rc == 1, f"returncode={rc}")
        errs = env.get("errors") or []
        check("c.has_err", len(errs) == 1, repr(errs))
        if errs:
            check("c.err_substr",
                  errs[0].startswith("INVALID_INPUT: protein PDB not found:"),
                  repr(errs[0]))
            # the offending path is echoed into the message.
            check("c.err_has_path", ghost in errs[0], repr(errs[0]))
        check("c.no_traceback", not has_traceback(out, err), err[-400:])


# --------------------------------------------------------------------------
# Case (d): bad --name -> ok:false. The wrapper DOES validate --name via
# re.fullmatch(r"[A-Z0-9]{1,4}", name), so this gate is real (not invented).
# "mol" (lowercase) and "TOOLONG" (5+ chars) both fail the pattern.
# --------------------------------------------------------------------------
def test_bad_name():
    for label, bad in (("lower", "mol"), ("toolong", "TOOLONG")):
        rc, env, out, err = run_wrapper(["--dry-run", "--name", bad])
        check(f"d.{label}.json_parsed", env is not None, f"stdout not JSON: {out!r}")
        if env is None:
            continue
        check(f"d.{label}.ok_false", env.get("ok") is False, repr(env.get("ok")))
        check(f"d.{label}.rc1", rc == 1, f"returncode={rc}")
        errs = env.get("errors") or []
        check(f"d.{label}.has_err", len(errs) == 1, repr(errs))
        if errs:
            check(f"d.{label}.err_substr",
                  errs[0].startswith(
                      "INVALID_INPUT: --name must be 1-4 uppercase letters/digits"),
                  repr(errs[0]))
        check(f"d.{label}.no_traceback", not has_traceback(out, err), err[-400:])

    # Sanity: a VALID name ("LIG") must NOT trip the --name gate. With --dry-run
    # and present spine files this should be ok:true / planned.
    rc, env, out, err = run_wrapper(["--dry-run", "--name", "LIG", "--sim-ps", "5"])
    check("d.valid.json_parsed", env is not None, f"stdout not JSON: {out!r}")
    if env is not None:
        check("d.valid.ok_true", env.get("ok") is True, repr(env.get("ok")))
        check("d.valid.no_name_err",
              not any("--name" in e for e in (env.get("errors") or [])),
              repr(env.get("errors")))


def main():
    print(f"wrapper under test: {WRAPPER}")
    print(f"python: {PY}")
    test_dry_run_plan()
    test_sim_ps_nonpositive()
    test_protein_missing()
    test_bad_name()
    print(f"\n{PASS} passed, {FAIL} failed  ({PASS + FAIL} assertions)")
    if FAIL:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()

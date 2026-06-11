#!/usr/bin/env python3
"""md-planner wrapper (Phase 3 Stage 7 — the planning layer).

Takes a JSON "plan manifest" (the main agent maps a scientific goal -> manifest;
this wrapper is PURE and deterministic — no LLM call), then:

  default / --dry-run   VALIDATE the manifest deterministically + COMPILE it to a
                        concrete, byte-inspectable execution plan. No execution.
  --validate            validate only (lightest gate).
  --execute             validate + compile + RUN the chain: for each stage in topo
                        order, call the skill wrapper, check its envelope ok + the
                        manifest's declared validation conditions, thread real
                        output paths forward; HALT on failure (recovery is Stage 8's
                        job, NOT done here) unless the stage is on_fail:continue.

The LLM reasons ONLY over the known catalog (select/parameterize/wire stages); the
deterministic validator promotes that `inferred` manifest to `confirmed` before any
byte reaches pmemd (Memory-Provenance discipline). Bounds are reused, not invented
(check_amber constants). run_happy_path.sh is neither edited nor invoked.

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
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry as R                      # noqa: E402  the Confirmed I/O contract
import check_amber_vendored as cav        # noqa: E402  imported bounds (reused, not invented)

SKILL_NAME = "md-planner"
SKILLS_DIR = Path(__file__).resolve().parents[2]     # .../skills
NAME_RE = re.compile(r"\A[A-Z0-9]{1,4}\Z")           # \A..\Z (not ^..$) — reject MOL\n
OPS = ["<=", ">=", "==", "!=", "<", ">"]             # 2-char first


# ---- Envelope ---------------------------------------------------------------

def envelope(ok: bool, dry_run: bool, outputs=None, validation=None, errors=None) -> str:
    return json.dumps({"ok": ok, "skill": SKILL_NAME, "dry_run": dry_run,
                       "outputs": outputs or {}, "validation": validation or {},
                       "errors": errors or []}, indent=2)


def emit_and_exit(*, ok, dry_run, outputs=None, validation=None, errors=None, code=0):
    print(envelope(ok, dry_run, outputs, validation, errors))
    sys.exit(code)


def log(msg: str) -> None:
    print(f"[{SKILL_NAME}] {msg}", file=sys.stderr)


# ============================================================================
# DECLARED VALIDATION CONDITIONS  (closed, bounded vocabulary)
# ============================================================================

def parse_condition(cond: str):
    if cond == "envelope_ok":
        return ("envelope_ok",)
    if cond.startswith("output_exists:"):
        return ("output_exists", cond.split(":", 1)[1])
    if cond.startswith("count_at_least:"):
        rest = cond.split(":", 1)[1]
        if ":" not in rest:
            return None
        path, n = rest.rsplit(":", 1)
        try:
            return ("count_at_least", path, int(n))
        except ValueError:
            return None
    if cond.startswith("numeric:"):
        expr = cond.split(":", 1)[1]
        for op in OPS:
            if op in expr:
                lhs, rhs = expr.split(op, 1)
                return ("numeric", lhs.strip(), op, rhs.strip())
    return None


def _dotted(obj: Any, path: str):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def eval_condition(env: dict, cond: str) -> tuple[bool, str | None]:
    p = parse_condition(cond)
    if p is None:
        return False, f"UNKNOWN_CONDITION:{cond}"
    kind = p[0]
    if kind == "envelope_ok":
        ok = env.get("ok") is True
        return ok, None if ok else "envelope ok=false"
    if kind == "output_exists":
        val = (env.get("outputs") or {}).get(p[1])
        ok = bool(val) and isinstance(val, str) and os.path.exists(val)
        return ok, None if ok else f"output {p[1]} missing/empty ({val})"
    if kind == "count_at_least":
        val = _dotted(env, p[1])
        ln = len(val) if isinstance(val, (list, tuple, dict)) else 0
        ok = ln >= p[2]
        return ok, None if ok else f"{p[1]} count {ln} < {p[2]}"
    if kind == "numeric":
        _, lhs_path, op, rhs_raw = p
        lhs = _dotted(env, lhs_path)
        try:
            rhs = float(rhs_raw)
        except ValueError:
            rhs = _dotted(env, rhs_raw)
        try:
            lf, rf = float(lhs), float(rhs)
        except (TypeError, ValueError):
            return False, f"numeric {lhs_path} not comparable ({lhs} {op} {rhs})"
        ok = {"<": lf < rf, "<=": lf <= rf, ">": lf > rf, ">=": lf >= rf,
              "==": lf == rf, "!=": lf != rf}[op]
        return ok, None if ok else f"{lhs_path}={lf} not {op} {rhs}"
    return False, f"UNKNOWN_CONDITION:{cond}"


# ============================================================================
# DETERMINISTIC VALIDATOR  (G0..G6, fail-collect)
# ============================================================================

def _f(level, gate, rule, detail):
    return {"level": level, "gate": gate, "rule": rule, "detail": detail}


def validate_manifest(manifest: Any) -> dict:
    """Pure validator. Returns
    {manifest_valid, ok, stage_order, stages:{id:{skill,verdict,findings}},
     graph:{acyclic,topo_order}, errors:[...]}.
    Stats provided system files (read-only); runs nothing."""
    errors: list[str] = []
    stages_report: dict[str, dict] = {}

    def fail(code):
        errors.append(code)

    # ---- G0 shape ----
    if not isinstance(manifest, dict):
        return {"manifest_valid": False, "ok": False, "stage_order": [],
                "stages": {}, "graph": {}, "errors": ["MALFORMED_MANIFEST: not an object"]}
    if str(manifest.get("manifest_version")) != "1":
        fail("MALFORMED_MANIFEST: manifest_version must be \"1\"")
    system = manifest.get("system")
    if not isinstance(system, dict):
        fail("MALFORMED_MANIFEST: missing/invalid system")
        system = {}
    stages = manifest.get("stages")
    if not isinstance(stages, list) or not stages:
        return {"manifest_valid": False, "ok": False, "stage_order": [],
                "stages": {}, "graph": {},
                "errors": errors + ["MALFORMED_MANIFEST: stages must be a non-empty list"]}

    # per-stage shape + id collection
    ids: list[str] = []
    stage_map: dict[str, dict] = {}
    for i, st in enumerate(stages):
        if not isinstance(st, dict) or not isinstance(st.get("id"), str) \
                or not isinstance(st.get("skill"), str) \
                or not isinstance(st.get("inputs", {}), dict) \
                or not isinstance(st.get("params", {}), dict) \
                or not (isinstance(st.get("validate", []), list)
                        and all(isinstance(c, str) for c in st.get("validate", []))):
            fail(f"MALFORMED_MANIFEST: stage #{i} bad id/skill/inputs/params/validate "
                 "(id+skill must be strings; validate a list of strings)")
            continue
        sid = st["id"]
        rep = {"skill": st.get("skill"), "verdict": "PASS", "findings": []}
        stages_report[sid] = rep
        if sid in stage_map:
            fail(f"DUPLICATE_STAGE_ID: {sid}")
            rep["findings"].append(_f("FAIL", "G2", "duplicate id", sid))
            rep["verdict"] = "FAIL"
        ids.append(sid)
        stage_map[sid] = st

    def add(sid, level, gate, rule, detail, code):
        stages_report[sid]["findings"].append(_f(level, gate, rule, detail))
        if level == "FAIL":
            stages_report[sid]["verdict"] = "FAIL"
            fail(code)
        elif level == "WARN" and stages_report[sid]["verdict"] != "FAIL":
            stages_report[sid]["verdict"] = "WARN"

    sys_charge = system.get("charge")
    sys_name = system.get("name")

    # ---- per-stage gates G1, G4(refs), G5, G6 ----
    for sid, st in stage_map.items():
        skill = st["skill"]
        # G1 known catalog
        if skill not in R.REGISTRY:
            add(sid, "FAIL", "G1", "unknown skill",
                f"{skill!r} not in the known catalog {sorted(R.REGISTRY)}",
                f"UNKNOWN_SKILL: {skill}")
            continue
        reg = R.REGISTRY[skill]
        ins = st.get("inputs", {})

        # G4 required inputs + resolvable wiring
        for key, spec in reg["inputs"].items():
            required = spec.get("required", False)
            rw = spec.get("requires_with")
            if rw and rw in ins:
                required = True            # frcmod becomes required when mol2 is present
            if key not in ins:
                if required:
                    add(sid, "FAIL", "G4", "missing input",
                        f"{sid}.{key} required", f"MISSING_INPUT: {sid}.{key}")
                continue
        # also: if a stage declares the partner of a requires_with, enforce both ways
        for key, spec in reg["inputs"].items():
            rw = spec.get("requires_with")
            if rw and key in ins and rw not in ins:
                add(sid, "FAIL", "G4", "conditional input",
                    f"{sid}.{key} requires {sid}.{rw}", f"MISSING_INPUT: {sid}.{rw}")

        # resolvability of every declared edge (G4 + G2 dangling)
        for key, edge in ins.items():
            if key not in reg["inputs"]:
                add(sid, "WARN", "G4", "unknown input key",
                    f"{sid}.{key} not in registry for {skill}", "")
            ref = edge.get("from") if isinstance(edge, dict) else None
            if not isinstance(ref, str):
                add(sid, "FAIL", "G4", "bad wiring",
                    f"{sid}.{key} has no string 'from'", f"UNSATISFIED_WIRING: {sid}.{key}")
                continue
            if ref.startswith("system."):
                field = ref.split(".", 1)[1]
                if field not in system:
                    add(sid, "FAIL", "G4", "unsatisfied wiring",
                        f"{sid}.{key} <- {ref} : system has no {field}",
                        f"UNSATISFIED_WIRING: {sid}.{key} <- {ref}")
            else:
                if "." not in ref:
                    add(sid, "FAIL", "G4", "bad ref", f"{ref} not <id>.<key>",
                        f"UNSATISFIED_WIRING: {sid}.{key} <- {ref}")
                    continue
                up_id, up_key = ref.split(".", 1)
                if up_id == sid:
                    add(sid, "FAIL", "G3", "self-loop",
                        f"{sid}.{key} wired from itself ({ref})",
                        f"CYCLIC_DAG: self-loop {sid}")
                elif up_id not in stage_map:
                    add(sid, "FAIL", "G2", "dangling ref",
                        f"{ref} -> no stage {up_id}", f"DANGLING_REF: {ref}")
                else:
                    up_skill = stage_map[up_id]["skill"]
                    up_outs = R.REGISTRY.get(up_skill, {}).get("outputs", [])
                    if up_key not in up_outs:
                        add(sid, "FAIL", "G4", "unsatisfied wiring",
                            f"{sid}.{key} <- {ref} : {up_skill} has no output {up_key}",
                            f"UNSATISFIED_WIRING: {sid}.{key} <- {ref}")

        # G5 MD-param bounds (imported constants — reused, not invented)
        if reg.get("md_param_stage"):
            cut = st.get("params", {}).get("cut")
            if cut is not None:
                try:
                    cutf = float(cut)
                    if not (cav.CUT_MIN - 1e-9 <= cutf <= cav.CUT_MAX + 1e-9):
                        add(sid, "FAIL", "G5", "cut out of range",
                            f"cut={cutf} outside [{cav.CUT_MIN},{cav.CUT_MAX}]",
                            f"MD_PARAM_OUT_OF_BOUNDS: {sid} cut={cutf}")
                except (TypeError, ValueError):
                    add(sid, "FAIL", "G5", "cut not numeric", f"cut={cut!r}",
                        f"MD_PARAM_OUT_OF_BOUNDS: {sid} cut={cut!r}")
            sp = st.get("params", {}).get("sim-ps")
            if sp is not None:
                try:
                    if float(sp) <= 0:
                        add(sid, "FAIL", "G5", "sim-ps not positive", f"sim-ps={sp}",
                            f"BAD_SIM_PS: {sid} sim-ps={sp}")
                except (TypeError, ValueError):
                    add(sid, "FAIL", "G5", "sim-ps not numeric", f"sim-ps={sp!r}",
                        f"BAD_SIM_PS: {sid}")

        # G6 param typing + required
        for pkey, pspec in reg.get("params", {}).items():
            if pspec.get("required") and pkey not in st.get("params", {}):
                add(sid, "FAIL", "G6", "missing param", f"{sid}.{pkey} required",
                    f"MISSING_PARAM: {sid}.{pkey}")
        for pkey, pval in st.get("params", {}).items():
            pspec = reg.get("params", {}).get(pkey)
            if pspec is None:
                # not in the skill's catalog -> would forward --<key> verbatim into
                # the real CLI. Reject (the bounded-LLM gate also covers params:
                # a hallucinated / unphysical flag like dt can't reach pmemd).
                add(sid, "FAIL", "G6", "unknown param",
                    f"{sid}.{pkey} not a param of {skill}", f"UNKNOWN_PARAM: {sid}.{pkey}")
                continue
            _typecheck(add, sid, pkey, pval, pspec["type"])

        # declared validate-condition syntax
        for cond in st.get("validate", []):
            if parse_condition(cond) is None:
                add(sid, "WARN", "G6", "unknown validate condition", cond, "")

    # ---- system-level typing (charge/name) ----
    if sys_charge is not None and not _is_int(sys_charge):
        fail(f"BAD_CHARGE: system.charge={sys_charge!r}")
    if sys_name is not None and not (isinstance(sys_name, str) and NAME_RE.match(sys_name)):
        fail(f"BAD_NAME: system.name={sys_name!r}")

    # ---- G3 DAG acyclic + topo order ----
    acyclic, topo = _toposort(stage_map)
    if not acyclic:
        fail(f"CYCLIC_DAG: residual {topo}")

    manifest_valid = not errors and all(
        r["verdict"] != "FAIL" for r in stages_report.values())
    return {"manifest_valid": manifest_valid, "ok": manifest_valid,
            "stage_order": topo, "stages": stages_report,
            "graph": {"acyclic": acyclic, "topo_order": topo}, "errors": errors}


def _is_int(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, str) and re.match(r"^-?\d+$", v) is not None


def _typecheck(add, sid, key, val, typ):
    if typ == "int":
        if not _is_int(val):
            add(sid, "FAIL", "G6", "bad type", f"{sid}.{key}={val!r} not int",
                f"BAD_CHARGE: {sid}.{key}={val!r}" if key == "charge"
                else f"BAD_PARAM: {sid}.{key}")
    elif typ == "resname":
        if not (isinstance(val, str) and NAME_RE.match(val)):
            add(sid, "FAIL", "G6", "bad name", f"{sid}.{key}={val!r}",
                f"BAD_NAME: {sid}.{key}={val!r}")
    elif typ in ("number", "pos_number"):
        try:
            f = float(val)
            if typ == "pos_number" and f <= 0:
                add(sid, "FAIL", "G6", "not positive", f"{sid}.{key}={val}",
                    f"BAD_SIM_PS: {sid}.{key}" if key == "sim-ps" else f"BAD_PARAM: {sid}.{key}")
        except (TypeError, ValueError):
            add(sid, "FAIL", "G6", "not numeric", f"{sid}.{key}={val!r}",
                f"BAD_PARAM: {sid}.{key}")
    elif typ.startswith("enum:"):
        allowed = typ.split(":", 1)[1].split("|")
        if str(val) not in allowed:
            add(sid, "FAIL", "G6", "bad enum", f"{sid}.{key}={val!r} not in {allowed}",
                f"BAD_PARAM: {sid}.{key}")
    # "str" -> anything accepted


def _toposort(stage_map: dict) -> tuple[bool, list]:
    """Kahn. Edges: stage -> stage for every {from:<id>.*}. Returns (acyclic, order)."""
    deps = {sid: set() for sid in stage_map}
    for sid, st in stage_map.items():
        for edge in st.get("inputs", {}).values():
            ref = edge.get("from") if isinstance(edge, dict) else None
            if isinstance(ref, str) and not ref.startswith("system.") and "." in ref:
                up = ref.split(".", 1)[0]
                if up in stage_map and up != sid:
                    deps[sid].add(up)
    order: list[str] = []
    ready = sorted([s for s, d in deps.items() if not d])
    deps = {s: set(d) for s, d in deps.items()}
    while ready:
        n = ready.pop(0)
        order.append(n)
        for s in list(deps):
            if n in deps[s]:
                deps[s].discard(n)
                if not deps[s] and s not in order and s not in ready:
                    ready.append(s)
        ready.sort()
    if len(order) != len(stage_map):
        return False, sorted(set(stage_map) - set(order))
    return True, order


# ============================================================================
# COMPILER  (manifest -> concrete execution plan; lazy-load = flat bound calls)
# ============================================================================

def _system_value(system: dict, field: str, staged: dict | None) -> str | None:
    spec = system.get(field)
    if not isinstance(spec, dict):
        return None
    if staged is not None and field in staged:
        return staged[field]
    if "path" in spec:
        return str(Path(spec["path"]).expanduser())
    if "smiles" in spec:
        return spec["smiles"]
    return None


def build_argv(stage: dict, out_dir: Path, resolve: Callable[[str], str | None]) -> list[str]:
    skill = stage["skill"]
    reg = R.REGISTRY[skill]
    argv = [sys.executable, str(SKILLS_DIR / skill / "scripts" / "wrapper.py")]
    for key, edge in stage.get("inputs", {}).items():
        cli = reg["inputs"].get(key, {}).get("cli", "--" + key)
        argv += [cli, str(resolve(edge["from"]))]
    for key, val in stage.get("params", {}).items():
        cli = reg["params"].get(key, {}).get("cli", "--" + key)
        argv += [cli, str(val)]
    argv += ["--output-dir", str(out_dir)]
    return argv


def compile_plan(manifest: dict, topo: list[str], run_root: Path) -> dict:
    """Compile to a flat, fully-decoupled list of bound calls (lazy-load: each
    call references only resolved values, not the graph). Upstream-derived inputs
    are SYMBOLIC here (no execution yet); system inputs are concrete."""
    system = manifest["system"]
    stage_map = {s["id"]: s for s in manifest["stages"]}

    def sym_resolve(ref: str) -> str:
        if ref.startswith("system."):
            return _system_value(system, ref.split(".", 1)[1], None) or f"<{ref}>"
        return "<" + ref + ">"

    calls = []
    for sid in topo:
        st = stage_map[sid]
        out_dir = run_root / f"{sid}-{st['skill']}"
        calls.append({
            "id": sid, "skill": st["skill"],
            "argv": build_argv(st, out_dir, sym_resolve),
            "output_dir": str(out_dir),
            "validate": st.get("validate", ["envelope_ok"]),
            "on_fail": st.get("on_fail", "halt"),
        })
    return {"run_root": str(run_root), "order": topo, "calls": calls}


# ============================================================================
# EXECUTOR  (run the chain; gate each transition; HALT; NO recovery)
# ============================================================================

def execute_plan(manifest: dict, topo: list[str], run_root: Path) -> dict:
    system = manifest["system"]
    stage_map = {s["id"]: s for s in manifest["stages"]}
    inputs_dir = run_root / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    # stage provided system files under bare names (path-with-space safety)
    staged: dict[str, str] = {}
    for field in sorted(R.SYSTEM_FIELDS):
        spec = system.get(field)
        if not isinstance(spec, dict):
            continue
        if "path" in spec:
            src = Path(spec["path"]).expanduser()
            if not src.is_file():
                return {"ok": False, "halt": "INVALID_SYSTEM_FILE",
                        "errors": [f"INVALID_SYSTEM_FILE: system.{field} not found: {src}"],
                        "completed": [], "stage_envelopes": {}, "stages": {}}
            dst = inputs_dir / f"{field}{src.suffix or '.dat'}"
            shutil.copy(src, dst)
            staged[field] = str(dst)
        elif "smiles" in spec:
            staged[field] = spec["smiles"]

    produced: dict[str, str] = {}

    def resolve(ref: str):
        if ref.startswith("system."):
            return staged.get(ref.split(".", 1)[1])
        return produced.get(ref)

    completed: list[str] = []
    failed: list[str] = []
    stage_envs: dict[str, str] = {}
    stages_report: dict[str, dict] = {}

    for sid in topo:
        st = stage_map[sid]
        on_fail = st.get("on_fail", "halt")
        out_dir = run_root / f"{sid}-{st['skill']}"
        rep = {"skill": st["skill"], "verdict": "PASS", "findings": []}
        stages_report[sid] = rep

        # all input refs must resolve at runtime (an upstream may have failed-continue)
        unresolved = [e["from"] for e in st.get("inputs", {}).values()
                      if resolve(e["from"]) is None]
        if unresolved:
            rep["verdict"] = "FAIL"
            rep["findings"].append(_f("FAIL", "exec", "unresolved input",
                                      f"{sid}: {unresolved}"))
            if on_fail == "continue":
                failed.append(sid)
                continue
            return _halt(run_root, completed, sid, stage_envs, stages_report,
                         f"UNRESOLVED_INPUT: {sid} {unresolved}")

        argv = build_argv(st, out_dir, lambda r: resolve(r))
        log(f"execute {sid} ({st['skill']})")
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, errors="replace")
        env_path = run_root / f"{sid}.json"
        env_path.write_text(proc.stdout or "")
        stage_envs[sid] = str(env_path)
        try:
            env = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            env = {"ok": False, "outputs": {}, "errors": ["NON_JSON_OUTPUT"]}

        cond_fail = []
        for cond in st.get("validate", ["envelope_ok"]):
            okc, why = eval_condition(env, cond)
            if not okc:
                cond_fail.append({"condition": cond, "why": why})
        stage_ok = (env.get("ok") is True) and not cond_fail

        if stage_ok:
            for k, v in (env.get("outputs") or {}).items():
                produced[f"{sid}.{k}"] = v
            completed.append(sid)
        else:
            rep["verdict"] = "FAIL"
            detail = (env.get("errors") or cond_fail or ["envelope ok=false"])
            rep["findings"].append(_f("FAIL", "stage_self", "stage failed", str(detail)))
            if on_fail == "continue":
                log(f"  {sid} failed but on_fail=continue (degraded)")
                failed.append(sid)
                continue
            return _halt(run_root, completed, sid, stage_envs, stages_report,
                         f"STAGE_FAILED: {sid} {st['skill']}")

    return {"ok": True, "halt": None, "errors": [], "completed": completed,
            "failed": failed, "degraded": bool(failed),
            "stage_envelopes": stage_envs, "produced": produced,
            "stages": stages_report, "run_root": str(run_root)}


def _halt(run_root, completed, halted_at, stage_envs, stages_report, reason):
    return {"ok": False, "halt": reason, "errors": [f"RECOVERY_NOT_ATTEMPTED — {reason}",
            reason], "completed": completed, "halted_at": halted_at,
            "stage_envelopes": stage_envs, "stages": stages_report,
            "run_root": str(run_root)}


# ============================================================================
# MAIN
# ============================================================================

def load_manifest(path: str):
    p = Path(path).expanduser()
    if not p.is_file():
        return None, f"INVALID_INPUT: --manifest not found: {p}"
    try:
        return json.loads(p.read_text()), None
    except json.JSONDecodeError as e:
        return None, f"MALFORMED_MANIFEST: invalid JSON ({e})"


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Validate / compile / execute a JSON plan manifest over the "
                    "known AMBER-pipeline skill catalog. The agent writes the "
                    "manifest; this wrapper is deterministic.")
    ap.add_argument("--manifest", required=True, help="Path to the plan manifest JSON.")
    ap.add_argument("--run-root", default="./md-planner-run",
                    help="Working dir for --execute (per-stage subdirs + staged inputs).")
    ap.add_argument("--validate", action="store_true", help="Validate only (no compile).")
    ap.add_argument("--execute", action="store_true",
                    help="Validate + compile + RUN the chain (gated, HALT on failure).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate + compile the plan, no execution (the default).")
    args = ap.parse_args()
    # A validator must NEVER crash without an envelope (the prior crash-on-malformed
    # bug class). Any unforeseen exception -> graceful ok:false, not a raw traceback.
    try:
        _dispatch(args)
    except SystemExit:
        raise                                  # normal emit_and_exit path
    except Exception as e:                      # noqa: BLE001 — last-resort guard
        emit_and_exit(ok=False, dry_run=not args.execute,
                      errors=[f"UNEXPECTED_ERROR: {type(e).__name__}: {e}"], code=1)


def _dispatch(args) -> None:
    manifest, err = load_manifest(args.manifest)
    if err:
        emit_and_exit(ok=False, dry_run=not args.execute, errors=[err], code=1)

    val = validate_manifest(manifest)
    validation = {"manifest_valid": val["manifest_valid"], "stage_order": val["stage_order"],
                  "stages": val["stages"], "graph": val["graph"]}
    log(f"validation: manifest_valid={val['manifest_valid']} errors={val['errors']}")

    # validate-only
    if args.validate and not args.execute:
        emit_and_exit(ok=val["manifest_valid"], dry_run=True,
                      outputs={"stage_order": val["stage_order"]},
                      validation=validation, errors=val["errors"], code=0)

    # an invalid manifest never compiles or executes (deterministic gate)
    if not val["manifest_valid"]:
        emit_and_exit(ok=False, dry_run=not args.execute, validation=validation,
                      errors=val["errors"], code=2)

    run_root = Path(args.run_root).expanduser().resolve()

    if not args.execute:                       # default / --dry-run: compile + emit plan
        plan = compile_plan(manifest, val["stage_order"], run_root)
        emit_and_exit(ok=True, dry_run=True, outputs={"plan": plan},
                      validation=validation, errors=[], code=0)

    # --execute
    run_root.mkdir(parents=True, exist_ok=True)
    res = execute_plan(manifest, val["stage_order"], run_root)
    for sid, rep in res.get("stages", {}).items():
        validation["stages"].setdefault(sid, {}).update(rep)
    outputs = {"run_root": res.get("run_root"), "order": val["stage_order"],
               "completed": res.get("completed", []),
               "failed": res.get("failed", []), "degraded": res.get("degraded", False),
               "stage_envelopes": res.get("stage_envelopes", {})}
    if res.get("halt"):
        outputs["halted_at"] = res.get("halted_at")
    emit_and_exit(ok=res["ok"], dry_run=False, outputs=outputs, validation=validation,
                  errors=res.get("errors", []), code=0 if res["ok"] else 3)


if __name__ == "__main__":
    main()

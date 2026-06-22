#!/usr/bin/env python3
"""mdin-edit wrapper.

Deterministically EDIT one parameter in one (or a group of) the advisor's
pre-prepared AMBER mdin files. One exec call per skill turn; OpenClaw sees a
single subprocess invocation that returns one JSON envelope on stdout.

The agent parses natural language ("set the timestep to 0.001 in the first
heating stage") into structured args (--stage heat-1 --param dt --value 0.001);
THIS wrapper does the byte-minimal, idempotent, bounds-checked, self-checked
edit. The LLM is never in the edit path.

Design constraints (do not violate):
  - Idempotent parse-REPLACE only; NEVER append. Re-running the same edit yields a
    byte-identical file.
  - Edit the smallest possible span (the numeric token), preserving indentation,
    the '=' spacing, the trailing comma, and any inline '! comment'.
  - Work on a COPY of the advisor's files (caller passes --md-dir to a working
    dir). The wrapper is non-destructive (atomic write, self-check) but the
    copy-first discipline is the caller's.
  - JSON envelope to stdout; human-readable progress to stderr.
  - Reuse the vendored md-param-check logic for ADVISORY post-edit validation and
    the self-check parse (see scripts/check_amber_vendored.py provenance header).

Usage:
    wrapper.py --md-dir <dir> --stage <stage|group:...> --param <p> --value <v> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

# Vendored validator (self-contained copy; see its provenance header).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_amber_vendored import (  # noqa: E402
    DT_MAX_NOSHAKE,
    DT_MAX_SHAKE,
    check_amber_in,
    num,
    parse_namelists,
)


SKILL_NAME = "mdin-edit"
LOG_NAME = "mdin-edit.log"


# ---- Stage → file map + groups -------------------------------------------

STAGE_FILES = {
    "min1": "min1.in", "min2": "min2.in",
    "heat-1": "heat-1.in", "heat-2": "heat-2.in", "heat-3": "heat-3.in",
    "press-1": "press-1.in", "press-2": "press-2.in", "press-3": "press-3.in",
    "relax": "relax.in", "prod": "prod.in",
}
GROUPS = {
    # The advisor explicitly named "from the third stage onward" as these four
    # (the final-target-temperature stages). NOT heat-1/2 or press-1/2.
    "group:third-onward": ["heat-3", "press-3", "relax", "prod"],
    "group:all": list(STAGE_FILES.keys()),
}


# ---- Param model ---------------------------------------------------------

# Rendering classes — see render_value(). Pinning float-ness to the PARAM, not
# the existing token, is what makes the edit idempotent across a type change.
INT_PARAMS = {"nstlim", "istep1", "istep2", "maxcyc", "ncyc", "ntr"}
FLOAT_PARAMS = {"dt", "cut", "temp0", "restraint_wt", "value1", "value2",
                "tempi", "gamma_ln", "pres0", "taup"}

# The parameters this skill will edit, with HARD accept/reject bounds.
HARD_BOUNDS = {
    "dt":           (lambda v: 0 < v <= 0.002,    "0 < dt ≤ 0.002 ps (2 fs SHAKE cap)"),
    "temp0":        (lambda v: 0 < v <= 400.0,    "0 < temp0 ≤ 400 K"),
    "restraint_wt": (lambda v: v >= 0.0,          "restraint_wt ≥ 0"),
    "nstlim":       (lambda v: v > 0,             "nstlim > 0"),
    "cut":          (lambda v: 6.0 <= v <= 12.0,  "6 ≤ cut ≤ 12 Å"),
}
SUPPORTED_PARAMS = set(HARD_BOUNDS)

# Validator rule names the editor "owns" for a given param: when the user sets a
# value that passes the editor's HARD bound, a vendored FAIL on these rules is a
# DELIBERATE choice (e.g. cut=7.0 < the validator's 8 Å floor) — surfaced as an
# advisory WARN, never a block.
OWNED_RULES = {
    "cut": {"cut out of range"},
    "dt": {"dt > 2 fs cap", "dt > 1 fs cap (no SHAKE)", "dt too large without SHAKE"},
    "temp0": {"temp0 / &wt mismatch"},
}

# Conditions that mean "this param doesn't belong in this stage". In a GROUP edit
# these are skipped (batch stays ok); in a SINGLE-stage edit they FAIL (the user
# explicitly targeted a stage that can't take the param).
NOT_APPLICABLE_CODES = {"PARAM_NOT_FOUND", "SKIPPED_RESTRAINTS_OFF"}


# ---- Envelope ------------------------------------------------------------

def envelope(ok: bool, dry_run: bool, outputs: dict[str, Any] | None = None,
             validation: dict[str, Any] | None = None,
             errors: list[str] | None = None) -> str:
    return json.dumps({
        "ok": ok,
        "skill": SKILL_NAME,
        "dry_run": dry_run,
        "outputs": outputs or {},
        "validation": validation or {},
        "errors": errors or [],
    }, indent=2)


def emit_and_exit(*, ok: bool, dry_run: bool,
                  outputs: dict[str, Any] | None = None,
                  validation: dict[str, Any] | None = None,
                  errors: list[str] | None = None, code: int = 0) -> None:
    print(envelope(ok=ok, dry_run=dry_run, outputs=outputs,
                   validation=validation, errors=errors))
    sys.exit(code)


class EditError(Exception):
    """A coded, user-facing edit failure. .code is a stable error code."""
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


# ---- The edit engine -----------------------------------------------------

# An AMBER numeric literal (int, float, sci-notation, optional sign).
VAL = r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?"

# ASCII-only numeric grammar for INPUT validation (uses [0-9], NOT \d which also
# matches unicode digits). Rejects 'inf'/'nan'/unicode/underscore/'0x..'/'' so the
# downstream float()/int() never sees a non-finite or non-ASCII value (which would
# otherwise crash int() or be silently coerced, e.g. '1_000'->1000, '０.００２'->0.002).
_VAL_ASCII = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def key_re(key: str) -> re.Pattern:
    r"""Match `<key> = <number>` capturing ONLY the numeric token in group 'val'.

    The left lookbehind (?<![\w]) prevents prefix collisions (restraint_wt vs
    restraintmask; step2 vs istep2). The value group stops before the comma, so
    on a two-param &wt line `value1 = 5.0, value2 = 100.0,` asking for value2
    leaves value1 and every comma untouched.
    """
    return re.compile(
        r"(?P<pre>(?<![\w])" + re.escape(key) + r"\s*=\s*)"
        r"(?P<val>" + VAL + r")",
        re.MULTILINE,
    )


CLOSE_RE = re.compile(r"^\s*/\s*$", re.MULTILINE)


def cntrl_span(text: str) -> Optional[tuple[int, int]]:
    """(start, end) of the &cntrl block body, end = the line-anchored closing '/'."""
    m = re.search(r"&cntrl\b", text, re.IGNORECASE)
    if not m:
        return None
    end = CLOSE_RE.search(text, m.end())
    if not end:
        return None
    return (m.end(), end.start())


def temp0_wt_span(text: str) -> Optional[tuple[int, int]]:
    """(start, end) of the &wt block whose body is type='TEMP0' (not the END block)."""
    for m in re.finditer(r"&wt\b", text, re.IGNORECASE):
        end = CLOSE_RE.search(text, m.end())
        if not end:
            continue
        body = text[m.end():end.start()]
        if re.search(r"type\s*=\s*'TEMP0'", body, re.IGNORECASE):
            return (m.end(), end.start())
    return None


def render_value(param: str, raw: str) -> str:
    """Canonical rendering, PURE in (param, raw) — independent of the current
    token (this is what guarantees idempotency). Fractional floats render via
    Decimal so the stored token faithfully round-trips the requested value with
    NO exponent and NO precision loss (a plain %.Nf would silently truncate a
    tiny dt like 2.55e-05)."""
    try:
        f = float(raw)
    except (ValueError, TypeError):
        raise EditError("INVALID_VALUE", f"{param}={raw!r} is not a number")
    if param in INT_PARAMS:
        if f != int(f):
            raise EditError("NONINTEGER_VALUE",
                            f"{param} requires an integer, got {raw!r}")
        return str(int(f))
    # float param: exact decimal of the input string, fixed-point, shortest form.
    d = Decimal(raw)
    if d == d.to_integral_value():
        return f"{int(d)}.0"            # integral -> 'N.0'
    s = format(d.normalize(), "f")      # fixed-point (no exponent), trailing zeros trimmed
    if "." not in s:
        s += ".0"
    return s


@dataclass
class Outcome:
    text: str
    found: bool
    changed: bool
    old: Optional[str]
    new: Optional[str]
    ambiguous: bool = False


def set_param_in_block(text: str, span: tuple[int, int], param: str,
                       rendered: str) -> Outcome:
    """Replace the numeric token of `param` inside [span) by index slicing.
    Never uses re.sub (no backreference surprises). Never appends."""
    start, end = span
    block = text[start:end]
    matches = list(key_re(param).finditer(block))
    if not matches:
        return Outcome(text, found=False, changed=False, old=None, new=rendered)
    if len(matches) > 1:
        return Outcome(text, found=True, changed=False, old=None,
                       new=rendered, ambiguous=True)
    m = matches[0]
    old = m.group("val")
    if old == rendered:
        return Outcome(text, found=True, changed=False, old=old, new=rendered)
    vstart, vend = start + m.start("val"), start + m.end("val")
    new_text = text[:vstart] + rendered + text[vend:]
    return Outcome(new_text, found=True, changed=True, old=old, new=rendered)


def cntrl_get(text: str, key: str) -> Optional[str]:
    nl = parse_namelists(text)
    if "cntrl" not in nl:
        return None
    return nl["cntrl"][0].get(key.lower())


def parsed_wt_temp0_value2(text: str) -> Optional[str]:
    for wt in parse_namelists(text).get("wt", []):
        if wt.get("type") == "TEMP0":  # parser strips the quotes
            return wt.get("value2")
    return None


# ---- Bounds --------------------------------------------------------------

def bounds_verdict(param: str, raw: str) -> tuple[bool, Optional[str], list[str]]:
    """(ok, hard_error_msg, warnings). Pure value check, independent of any file."""
    if not _VAL_ASCII.fullmatch(raw):
        return False, f"INVALID_VALUE: {param}={raw!r} is not a plain ASCII decimal number", []
    try:
        v = float(raw)
    except (ValueError, OverflowError):
        return False, f"INVALID_VALUE: {param}={raw!r} is not a number", []
    if not math.isfinite(v):
        return False, f"INVALID_VALUE: {param}={raw!r} is not a finite number", []
    if param in INT_PARAMS and v != int(v):
        return False, f"NONINTEGER_VALUE: {param} must be an integer, got {raw!r}", []
    ok_fn, desc = HARD_BOUNDS[param]
    if not ok_fn(v):
        return False, f"OUT_OF_BOUNDS: {param}={raw} violates {desc}", []
    warns: list[str] = []
    if param == "cut" and 6.0 <= v < 8.0:
        warns.append(
            f"cut={raw} Å is below the project validator's 8 Å floor for explicit "
            "solvent. Accepted deliberately (PME reciprocal space covers long-range "
            "electrostatics); confirm intent.")
    return True, None, warns


# ---- Stage-context advisories (advisor feedback 2026-06-22) ---------------
#
# These are file-AWARE: unlike bounds_verdict (a pure value check), they read the
# stage's own &cntrl to coordinate dt with SHAKE/temperature and nstlim with the
# output frequency. dt's hard cap becomes context-aware here; the rest are
# non-blocking WARNs surfaced through res.warnings (same channel as the cut floor).

def shake_on(text: str) -> bool:
    """SHAKE constrains bonds-to-H iff ntc==2 AND ntf==2 (matches the vendored
    validator's coherence rule). Anything else → treat SHAKE as off (stricter dt)."""
    return num(cntrl_get(text, "ntc")) == 2 and num(cntrl_get(text, "ntf")) == 2


def dt_cap_for(text: str) -> float:
    """Max dt for THIS stage: 0.002 ps with SHAKE on, 0.001 ps without (Amber
    manual). Reading ntc/ntf is what makes the cap context-aware rather than a flat
    2 fs everywhere."""
    return DT_MAX_SHAKE if shake_on(text) else DT_MAX_NOSHAKE


def hot_dt_advisory(temp0: float | None, dt: float | None, cap: float) -> Optional[str]:
    """Non-blocking advisory: above 300 K, larger atomic velocities mean longer
    travel per step, so a dt sitting at the SHAKE cap is risky. Fires only on the
    post-edit state of a dt/temp0 edit; never blocks (advisory only)."""
    if temp0 is not None and dt is not None and temp0 > 300.0 and dt >= cap - 1e-9:
        return (f"temp0={temp0:g} K (>300) with dt={dt:g} ps at the {cap:g} ps cap: "
                "higher temperature means larger atomic velocities per step — consider "
                "reducing dt for stability (Amber manual). Advisory only — not blocking.")
    return None


def output_schedule(text: str, nstlim: int) -> dict[str, Any]:
    """Compute the trajectory/energy output schedule a given nstlim produces, with
    advisory warnings when it yields zero/sparse frames or an uneven final interval.
    ntwx==0 means trajectory is intentionally OFF (heat/press stages) → no frame
    warning. All warnings are non-blocking. Returns {"schedule": {...}, "warnings": [...]}."""
    ntwx = num(cntrl_get(text, "ntwx"))
    ntpr = num(cntrl_get(text, "ntpr"))
    sched: dict[str, Any] = {
        "nstlim": nstlim,
        "ntwx": None if ntwx is None else int(ntwx),
        "ntpr": None if ntpr is None else int(ntpr),
        "trajectory_frames": None,
        "energy_outputs": None,
        "trajectory_off": False,
    }
    warnings: list[str] = []
    if ntwx is not None and int(ntwx) == 0:
        sched["trajectory_off"] = True            # intentional (heating/press): no warn
    elif ntwx is not None and int(ntwx) > 0:
        iw = int(ntwx)
        frames = nstlim // iw
        sched["trajectory_frames"] = frames
        if frames == 0:
            warnings.append(f"nstlim={nstlim} < ntwx={iw}: ZERO trajectory frames will "
                            "be written (no usable trajectory).")
        elif frames < 10:
            warnings.append(f"nstlim={nstlim} / ntwx={iw} → only {frames} trajectory "
                            "frame(s); very sparse sampling.")
        if nstlim % iw != 0:
            warnings.append(f"nstlim={nstlim} is not a multiple of ntwx={iw}: the final "
                            "trajectory interval is truncated (align nstlim % ntwx == 0).")
    if ntpr is not None and int(ntpr) > 0:
        ip = int(ntpr)
        eo = nstlim // ip
        sched["energy_outputs"] = eo
        if eo == 0:
            warnings.append(f"nstlim={nstlim} < ntpr={ip}: ZERO energy records will be "
                            "written.")
        if nstlim % ip != 0:
            warnings.append(f"nstlim={nstlim} is not a multiple of ntpr={ip}: the final "
                            "energy interval is truncated (align nstlim % ntpr == 0).")
    return {"schedule": sched, "warnings": warnings}


# ---- Restraint-mask string handling (enable-restraints op) ---------------
#
# restraintmask is the ONE non-numeric value this skill edits. It is handled by a
# small, separate path (NOT a generalization of the numeric token engine) used only
# by the restraint-transition modes. Validation is deliberately shallow — we guard
# the bytes that would corrupt the namelist line or our scanner, not AMBER mask
# semantics (the agent/user owns the mask's chemistry).

def validate_mask(raw: str) -> str:
    s = raw.strip()
    if not s:
        raise EditError("INVALID_MASK", "restraintmask is empty")
    if len(s) > 256:
        raise EditError("INVALID_MASK", f"restraintmask too long ({len(s)} > 256 chars)")
    # Forbid the bytes that would corrupt the namelist line or our scanners:
    #   " / '  → quote chars + the namelist terminator '/' (the vendored parser's
    #   NAMELIST_RE stops at the first '/', which would truncate every key after the
    #   mask line); newline/CR → split the line. AMBER masks never need any of these.
    bad = [c for c in ('"', "'", "/", "\n", "\r") if c in s]
    if bad:
        raise EditError("INVALID_MASK",
                        f"restraintmask must not contain {bad} (would corrupt the namelist)")
    return s


def render_mask_line(indent: str, mask: str, trailing_comma: bool) -> str:
    """A canonical, always-double-quoted restraintmask line, matching the demo style."""
    return f'{indent}restraintmask = "{mask}"' + ("," if trailing_comma else "")


# A restraintmask assignment scoped to &cntrl: captures the quoted value token only.
RMASK_RE = re.compile(r'(?<![\w])restraintmask\s*=\s*(?P<q>["\'])(?P<val>[^"\']*)(?P=q)')


# ---- Per-file orchestration ----------------------------------------------

@dataclass
class FileResult:
    file: str
    status: str  # edited | unchanged | skipped | error
    edits: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: Optional[str] = None      # CODE: detail (for skipped/error)
    new_text: Optional[str] = None    # in-memory result (None if no write needed)
    validation: dict | None = None    # advisory vendored verdict
    output_schedule: dict | None = None  # nstlim → trajectory/energy schedule (nstlim edits)


def validate_text(text: str) -> Any:
    """Run the vendored check_amber_in over an in-memory file. Returns a
    FileReport (advisory only — never gates ok)."""
    tf = tempfile.NamedTemporaryFile("w", suffix=".in", delete=False)
    try:
        tf.write(text)
        tf.close()
        return check_amber_in(Path(tf.name))
    finally:
        os.unlink(tf.name)


def plan_file_edit(path: Path, param: str, rendered: str, raw: str) -> FileResult:
    """Compute (in memory) the edit for one file. Writes NOTHING. Encodes expected
    conditions as status/reason on the result rather than raising."""
    name = path.name
    # builtin open(newline="") preserves CRLF/LF exactly (byte-minimal) AND works on
    # Python 3.11 — the conda env OpenClaw runs (Path.read_text(newline=) is 3.13+).
    with open(path, "r", newline="") as _fh:
        text = _fh.read()

    if cntrl_span(text) is None:
        return FileResult(name, "error", reason="NAMELIST_NOT_FOUND: no &cntrl block")

    # Context-aware dt cap (advisor feedback): the max timestep depends on SHAKE —
    # 0.002 ps with ntc=2,ntf=2, else 0.001 ps. Only applies where dt is present (the
    # MD stages); on a min stage with no dt this falls through to PARAM_NOT_FOUND.
    if param == "dt" and cntrl_get(text, "dt") is not None:
        cap = dt_cap_for(text)
        if float(raw) > cap + 1e-9:
            shake = "SHAKE on (ntc=2,ntf=2)" if cap == DT_MAX_SHAKE else "no SHAKE (ntc/ntf != 2)"
            return FileResult(name, "error",
                              reason=(f"OUT_OF_BOUNDS: dt={raw} ps exceeds the {cap:g} ps cap "
                                      f"for this stage [{shake}]"))

    # Applicability: restraint_wt is a no-op where restraints are off (ntr != 1).
    if param == "restraint_wt":
        ntr = num(cntrl_get(text, "ntr"))
        if ntr != 1:
            return FileResult(name, "skipped",
                              reason=f"SKIPPED_RESTRAINTS_OFF: ntr={ntr} "
                                     "(restraints off; editing restraint_wt has no effect)")

    edits: list[dict] = []

    # Primary edit, scoped to &cntrl.
    out = set_param_in_block(text, cntrl_span(text), param, rendered)
    if out.ambiguous:
        return FileResult(name, "error",
                          reason=f"AMBIGUOUS_PARAM: {param} appears more than once in &cntrl")
    if not out.found:
        return FileResult(name, "skipped",
                          reason=f"PARAM_NOT_FOUND: {param} not present in {name} &cntrl")
    text2 = out.text
    edits.append({"namelist": "cntrl", "param": param,
                  "old": out.old, "new": out.new, "changed": out.changed})

    # Coupling for temp0. Two mutually-exclusive cases (advisor feedback 2026-06-22):
    #   (a) HEAT stage (nmropt=1 with a TEMP0 &wt ramp): the ramp END value2 follows
    #       temp0 — keeps the heat-3 class of temp0/&wt drift from recurring. tempi
    #       is the ramp START and must NOT be touched.
    #   (b) CONSTANT-T stage (relax/prod: tempi present, NO TEMP0 ramp): tempi tracks
    #       temp0, so the constant-temperature stage starts and holds at the target.
    #   press stages (temp0, no tempi, no ramp) get neither — just the plain temp0 edit.
    if param == "temp0":
        wt_span = temp0_wt_span(text2)
        if wt_span is not None and num(cntrl_get(text2, "nmropt")) == 1:
            out2 = set_param_in_block(text2, wt_span, "value2", rendered)
            if out2.ambiguous:
                return FileResult(name, "error",
                                  reason="AMBIGUOUS_PARAM: value2 appears more than once in &wt TEMP0")
            if out2.found:
                text2 = out2.text
                edits.append({"namelist": "wt", "param": "value2",
                              "old": out2.old, "new": out2.new, "changed": out2.changed})
        elif (wt_span is None and num(cntrl_get(text2, "nmropt")) != 1
              and cntrl_get(text2, "tempi") is not None):
            # constant-T only: nmropt!=1 guards against a heat-like stage whose TEMP0
            # ramp has been removed but whose tempi is still a ramp START.
            out3 = set_param_in_block(text2, cntrl_span(text2), "tempi", rendered)
            if out3.ambiguous:
                return FileResult(name, "error",
                                  reason="AMBIGUOUS_PARAM: tempi appears more than once in &cntrl")
            if out3.found:
                text2 = out3.text
                edits.append({"namelist": "cntrl", "param": "tempi",
                              "old": out3.old, "new": out3.new, "changed": out3.changed})

    # Self-check: re-parse the result with the INDEPENDENT vendored parser and
    # assert it now reads the rendered value. Catches any wrong-span regex bug.
    got = cntrl_get(text2, param)
    if got != rendered:
        return FileResult(name, "error",
                          reason=f"SELF_CHECK_FAILED: after edit {param}={got!r} != {rendered!r}")
    if any(e["namelist"] == "wt" for e in edits):
        gv2 = parsed_wt_temp0_value2(text2)
        if gv2 != rendered:
            return FileResult(name, "error",
                              reason=f"SELF_CHECK_FAILED: after edit &wt value2={gv2!r} != {rendered!r}")
    if any(e["namelist"] == "cntrl" and e["param"] == "tempi" for e in edits):
        gti = cntrl_get(text2, "tempi")
        if gti != rendered:
            return FileResult(name, "error",
                              reason=f"SELF_CHECK_FAILED: after edit tempi={gti!r} != {rendered!r}")

    changed_any = any(e["changed"] for e in edits)
    res = FileResult(name, "edited" if changed_any else "unchanged",
                     edits=edits, new_text=text2 if changed_any else None)

    # Editor's own advisory warning (e.g. cut below the 8 Å project floor).
    _, _, warns = bounds_verdict(param, raw)
    res.warnings = list(warns)

    # Advisor feedback: coordinate dt with SHAKE + temperature. Reads the POST-edit
    # state so it fires whether the user raised dt on a hot stage or raised temp0
    # past 300 K on a dt-at-cap stage. Non-blocking.
    if param in ("dt", "temp0"):
        adv = hot_dt_advisory(num(cntrl_get(text2, "temp0")),
                              num(cntrl_get(text2, "dt")), dt_cap_for(text2))
        if adv:
            res.warnings.append(adv)

    # Advisor feedback: nstlim vs output frequency — attach the resulting trajectory/
    # energy schedule and warn on zero/sparse/uneven sampling. (Presented for review
    # in --dry-run before the user commits; never blocks.)
    if param == "nstlim":
        sr = output_schedule(text2, int(float(raw)))
        res.output_schedule = sr["schedule"]
        res.warnings.extend(sr["warnings"])

    # Advisory vendored validation on the would-be result (never gates ok).
    rep = validate_text(text2)
    findings = []
    verdict = "PASS"
    for f in rep.findings:
        deliberate = (f.level == "FAIL" and f.rule in OWNED_RULES.get(param, set()))
        level = "WARN" if deliberate else f.level
        findings.append({"level": level, "rule": f.rule, "detail": f.detail,
                         "deliberate": deliberate})
        if level == "FAIL":
            verdict = "FAIL"
        elif level == "WARN" and verdict != "FAIL":
            verdict = "WARN"
    res.validation = {"verdict": verdict, "findings": findings}
    return res


# ---- Restraint transitions (advisor feedback 2026-06-22) -----------------
#
# Enabling/disabling positional restraints is a multi-key TRANSACTION, not a single
# numeric edit, so it gets its own modes rather than the generic --param path:
#   enable  → ntr=1, restraint_wt=<user>, restraintmask=<user> (INSERT the mask line
#             where the stage has none — the ONLY place this skill adds a line, kept
#             out of the generic engine which never appends).
#   disable → ntr=0 (leave restraint_wt/restraintmask as-is; AMBER ignores them).
# Both are idempotent, all-or-nothing, atomic, self-checked — same as the editor.

def _read_text(path: Path) -> str:
    with open(path, "r", newline="") as fh:   # 3.11-safe; preserve exact line endings
        return fh.read()


def _insert_restraintmask_line(text: str, anchor_abs: int, mask: str) -> str:
    """Insert a `restraintmask = "<mask>"` line on its own line immediately after the
    line containing index `anchor_abs` (the restraint_wt line), copying that line's
    indentation + trailing-comma style + newline flavour. Index-based (no re.sub)."""
    # trailing-comma test ignores an inline '! comment' (the restraint_wt token is
    # numeric, so a bare '!' split is safe — no quotes on this line).
    def _ends_comma(ln: str) -> bool:
        return ln.split("!", 1)[0].rstrip().endswith(",")

    line_start = text.rfind("\n", 0, anchor_abs) + 1   # 0 if this is the first line
    nl_pos = text.find("\n", anchor_abs)
    if nl_pos == -1:                                    # anchor is the last (unterminated) line
        line = text[line_start:]
        indent = line[:len(line) - len(line.lstrip())]
        return text + "\n" + render_mask_line(indent, mask, _ends_comma(line))
    crlf = nl_pos > 0 and text[nl_pos - 1] == "\r"
    line = text[line_start:(nl_pos - 1 if crlf else nl_pos)]
    indent = line[:len(line) - len(line.lstrip())]
    comma = _ends_comma(line)
    newline = "\r\n" if crlf else "\n"
    insert_at = nl_pos + 1                              # start of the next line
    return text[:insert_at] + render_mask_line(indent, mask, comma) + newline + text[insert_at:]


def _read_mask(text: str) -> Optional[str]:
    """Read the &cntrl restraintmask value via the quoted-value regex. NOT cntrl_get:
    the vendored parser's comment-strip eats the '!' that masks start with, so it
    cannot see restraintmask at all. RMASK_RE reads the quoted token directly."""
    span = cntrl_span(text)
    if span is None:
        return None
    matches = list(RMASK_RE.finditer(text[span[0]:span[1]]))
    if len(matches) != 1:
        return None
    return matches[0].group("val")


def plan_enable_restraints(path: Path, wt_raw: str, mask_raw: str) -> FileResult:
    """Turn restraints ON: ntr=1, restraint_wt=<wt>, restraintmask=<mask> (edit the
    mask in place if present, else insert one line). All in memory; writes nothing."""
    name = path.name
    text = _read_text(path)
    if cntrl_span(text) is None:
        return FileResult(name, "error", reason="NAMELIST_NOT_FOUND: no &cntrl block")

    # Validate the user-supplied force constant + mask up front (hard, no write).
    try:
        mask = validate_mask(mask_raw)
    except EditError as exc:
        return FileResult(name, "error", reason=f"{exc.code}: {exc.msg}")
    ok_b, berr, _ = bounds_verdict("restraint_wt", wt_raw)
    if not ok_b:
        return FileResult(name, "error", reason=berr)
    wt_rendered = render_value("restraint_wt", wt_raw)

    edits: list[dict] = []
    text2 = text

    # 1. ntr = 1
    out_ntr = set_param_in_block(text2, cntrl_span(text2), "ntr", "1")
    if out_ntr.ambiguous:
        return FileResult(name, "error", reason="AMBIGUOUS_PARAM: ntr appears more than once in &cntrl")
    if not out_ntr.found:
        return FileResult(name, "error", reason="PARAM_NOT_FOUND: ntr not present in &cntrl")
    text2 = out_ntr.text
    edits.append({"namelist": "cntrl", "param": "ntr",
                  "old": out_ntr.old, "new": out_ntr.new, "changed": out_ntr.changed})

    # 2. restraint_wt = <wt>  (present in every stage, incl. ntr=0 ones at 0.0 → always an edit)
    out_wt = set_param_in_block(text2, cntrl_span(text2), "restraint_wt", wt_rendered)
    if out_wt.ambiguous:
        return FileResult(name, "error", reason="AMBIGUOUS_PARAM: restraint_wt appears more than once in &cntrl")
    if not out_wt.found:
        return FileResult(name, "error", reason="PARAM_NOT_FOUND: restraint_wt not present in &cntrl")
    text2 = out_wt.text
    edits.append({"namelist": "cntrl", "param": "restraint_wt",
                  "old": out_wt.old, "new": out_wt.new, "changed": out_wt.changed})

    # 3. restraintmask: edit in place if a line exists, else INSERT one (the one
    #    deliberate exception to never-append, scoped to this op).
    span = cntrl_span(text2)
    block = text2[span[0]:span[1]]
    mmatches = list(RMASK_RE.finditer(block))
    inserted = False
    if len(mmatches) > 1:
        return FileResult(name, "error", reason="AMBIGUOUS_PARAM: restraintmask appears more than once in &cntrl")
    if len(mmatches) == 1:
        m = mmatches[0]
        old_mask = m.group("val")
        if old_mask == mask:
            edits.append({"namelist": "cntrl", "param": "restraintmask",
                          "old": old_mask, "new": mask, "changed": False})
        else:
            vstart, vend = span[0] + m.start("val"), span[0] + m.end("val")
            text2 = text2[:vstart] + mask + text2[vend:]
            edits.append({"namelist": "cntrl", "param": "restraintmask",
                          "old": old_mask, "new": mask, "changed": True})
    else:
        wm = key_re("restraint_wt").search(block)   # anchor: the restraint_wt line
        if wm is None:                              # unreachable (edited it above) but safe
            return FileResult(name, "error", reason="PARAM_NOT_FOUND: restraint_wt anchor for mask insert")
        text2 = _insert_restraintmask_line(text2, span[0] + wm.start(), mask)
        inserted = True
        edits.append({"namelist": "cntrl", "param": "restraintmask",
                      "old": None, "new": mask, "changed": True})

    # Self-check (independent of the vendored parser for the mask — see _read_mask).
    if cntrl_get(text2, "ntr") != "1":
        return FileResult(name, "error",
                          reason=f"SELF_CHECK_FAILED: after enable ntr={cntrl_get(text2, 'ntr')!r} != '1'")
    if cntrl_get(text2, "restraint_wt") != wt_rendered:
        return FileResult(name, "error",
                          reason=f"SELF_CHECK_FAILED: after enable restraint_wt={cntrl_get(text2, 'restraint_wt')!r} != {wt_rendered!r}")
    if _read_mask(text2) != mask:
        return FileResult(name, "error",
                          reason=f"SELF_CHECK_FAILED: after enable restraintmask={_read_mask(text2)!r} != {mask!r}")
    # Line-count safety: enable adds exactly one line iff we inserted, else none.
    delta = text2.count("\n") - text.count("\n")
    if delta != (1 if inserted else 0):
        return FileResult(name, "error",
                          reason=f"SELF_CHECK_FAILED: enable changed line count by {delta} (expected {1 if inserted else 0})")

    changed_any = any(e["changed"] for e in edits)
    return FileResult(name, "edited" if changed_any else "unchanged",
                      edits=edits, new_text=text2 if changed_any else None)


def plan_disable_restraints(path: Path) -> FileResult:
    """Turn restraints OFF: ntr=0 only (leave restraint_wt/restraintmask — AMBER
    ignores them when ntr=0). All in memory; writes nothing."""
    name = path.name
    text = _read_text(path)
    if cntrl_span(text) is None:
        return FileResult(name, "error", reason="NAMELIST_NOT_FOUND: no &cntrl block")

    out = set_param_in_block(text, cntrl_span(text), "ntr", "0")
    if out.ambiguous:
        return FileResult(name, "error", reason="AMBIGUOUS_PARAM: ntr appears more than once in &cntrl")
    if not out.found:
        return FileResult(name, "error", reason="PARAM_NOT_FOUND: ntr not present in &cntrl")
    text2 = out.text
    if cntrl_get(text2, "ntr") != "0":
        return FileResult(name, "error",
                          reason=f"SELF_CHECK_FAILED: after disable ntr={cntrl_get(text2, 'ntr')!r} != '0'")
    edits = [{"namelist": "cntrl", "param": "ntr",
              "old": out.old, "new": out.new, "changed": out.changed}]
    return FileResult(name, "edited" if out.changed else "unchanged",
                      edits=edits, new_text=text2 if out.changed else None)


# ---- Submit: prove the (already-edited) mdin set runs locally ------------
#
# This productizes tests/smoke_edit_run.sh: take a COPY (possibly already
# edited), scratch-copy it, rewrite the advisor's hardcoded AMBERHOME to source
# the local toolchain, reduce nstlim via THIS engine, smoke-accelerate the
# out-of-scope min/heat lengths, then run the advisor's min1..prod pmemd chain
# (restart-chained) to normal termination. It does NOT mutate --md-dir.

# Files a --submit run needs in --md-dir (the 10 stages + topology + driver).
SUBMIT_REQUIRED = (["complex.parm7", "complex.rst7", "submit.sh"]
                   + [STAGE_FILES[s] for s in STAGE_FILES])

# Chain order + restart-coordinate source — lifted verbatim from the advisor's
# submit.sh (see tests/smoke_edit_run.sh). Each stage restarts from the prior.
SUBMIT_CHAIN = [
    ("min1", "complex.rst7"), ("min2", "min1.rst7"),
    ("heat-1", "min2.rst7"), ("press-1", "heat-1.rst7"),
    ("heat-2", "press-1.rst7"), ("press-2", "heat-2.rst7"),
    ("heat-3", "press-2.rst7"), ("press-3", "heat-3.rst7"),
    ("relax", "press-3.rst7"), ("prod", "relax.rst7"),
]
MCBAR_FLOOR = 100  # MC barostat (mcbarint default) needs nstlim ≥ 100 for NPT.


def _read(path: Path) -> str:
    with open(path, "r", newline="") as fh:  # 3.11-safe; preserve line endings
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", newline="") as fh:
        fh.write(text)


def _rewrite_amberhome(submit_path: Path, env_sh: Path) -> None:
    """Rewrite `export AMBERHOME=<advisor path>` → `source "<local env.sh>"`.
    Same regex as tests/smoke_edit_run.sh — makes the driver foreign-path-clean."""
    _write(submit_path,
           re.sub(r'(?m)^\s*export\s+AMBERHOME=.*$',
                  f'source "{env_sh}"', _read(submit_path)))


def _smoke_accelerate(scratch: Path, nstlim: int) -> None:
    """Trim params OUTSIDE mdin-edit's scope (maxcyc for min, &wt istep2 for heat)
    so the smoke finishes in minutes. NOT part of the edit contract — smoke-only;
    rewrites only the integer, preserving indentation/spacing."""
    for name in ("min1.in", "min2.in"):
        p = scratch / name
        _write(p, re.sub(r'(?m)(^\s*maxcyc\s*=\s*)\d+', rf'\g<1>{nstlim}', _read(p)))
    for name in ("heat-1.in", "heat-2.in", "heat-3.in"):
        p = scratch / name
        _write(p, re.sub(r'(?m)(^\s*istep2\s*=\s*)\d+', rf'\g<1>{nstlim}', _read(p)))


def _cleanup(parent: Path, keep: bool) -> None:
    if not keep:
        shutil.rmtree(parent, ignore_errors=True)


def run_submit(md_dir: Path, reduce_nstlim: int, dry_run: bool, keep: bool) -> None:
    skill_dir = Path(__file__).resolve().parent.parent       # skills/mdin-edit
    prime_root = skill_dir.parent.parent                     # project-prime
    env_sh = prime_root / "scripts" / "env.sh"
    validator = skill_dir / "scripts" / "check_amber_vendored.py"
    self_path = Path(__file__).resolve()

    # 1. Validate inputs — fail fast, touch nothing.
    if not md_dir.is_dir():
        emit_and_exit(ok=False, dry_run=dry_run, code=1,
                      errors=[f"MD_DIR_NOT_FOUND: {md_dir}"])
    if not env_sh.is_file():
        emit_and_exit(ok=False, dry_run=dry_run, code=1,
                      errors=[f"ENV_SH_NOT_FOUND: {env_sh}"])
    missing = [f for f in SUBMIT_REQUIRED if not (md_dir / f).is_file()]
    if missing:
        emit_and_exit(ok=False, dry_run=dry_run, code=1,
                      errors=[f"SUBMIT_INPUTS_MISSING: {missing}"])
    if reduce_nstlim < MCBAR_FLOOR:
        emit_and_exit(ok=False, dry_run=dry_run, code=1,
                      errors=[f"REDUCE_NSTLIM_TOO_SMALL: {reduce_nstlim} < "
                              f"{MCBAR_FLOOR} (MC barostat floor for NPT)"])

    # 2. Scratch copy — the caller's COPY is never mutated by rewrite/accel/run.
    scratch_parent = Path(tempfile.mkdtemp(prefix="mdin-submit-"))
    scratch = scratch_parent / "run"
    shutil.copytree(md_dir, scratch)
    print(f"[submit] scratch: {scratch} (reduce_nstlim={reduce_nstlim})",
          file=sys.stderr)

    try:
        # 3. AMBERHOME rewrite + foreign-path-clean assertion (vendored detector).
        submit_sh = scratch / "submit.sh"
        _rewrite_amberhome(submit_sh, env_sh)
        vproc = subprocess.run([sys.executable, str(validator), str(submit_sh)],
                               capture_output=True, text=True)
        foreign_clean = "hardcoded foreign path" not in (vproc.stdout + vproc.stderr)
        if not foreign_clean:
            _cleanup(scratch_parent, keep)
            emit_and_exit(ok=False, dry_run=dry_run, code=3,
                          errors=["SUBMIT_PATH_NOT_CLEAN: submit.sh still has a "
                                  "foreign hardcoded path after rewrite"])

        # 4. Reduce nstlim via THIS wrapper (subprocess-to-self → exact tested edit path).
        red = subprocess.run(
            [sys.executable, str(self_path), "--md-dir", str(scratch),
             "--stage", "group:all", "--param", "nstlim", "--value",
             str(reduce_nstlim)], capture_output=True, text=True)
        try:
            red_ok = json.loads(red.stdout).get("ok", False)
        except Exception:
            red_ok = False
        if not red_ok:
            _cleanup(scratch_parent, keep)
            emit_and_exit(ok=False, dry_run=dry_run, code=3,
                          errors=[f"NSTLIM_REDUCE_FAILED: "
                                  f"{(red.stdout or red.stderr).strip()[:300]}"])

        # 5. Smoke-accelerate the out-of-scope min/heat lengths.
        _smoke_accelerate(scratch, reduce_nstlim)

        outputs: dict[str, Any] = {
            "mode": "submit",
            "md_dir": str(md_dir),
            "scratch_dir": str(scratch),
            "reduce_nstlim": reduce_nstlim,
            "chain": [s for s, _ in SUBMIT_CHAIN],
        }
        validation = {"submit_script": {"foreign_path_clean": foreign_clean}}

        # 6. Dry-run → report the plan; run no pmemd, need no toolchain.
        if dry_run:
            outputs["stages"] = [{"stage": s, "restart_from": c, "planned": True}
                                 for s, c in SUBMIT_CHAIN]
            _cleanup(scratch_parent, keep)
            emit_and_exit(ok=True, dry_run=True, outputs=outputs,
                          validation=validation, errors=[], code=0)

        # 7. Run the chain (restart-chained). A failed stage blocks the rest.
        stage_results: list[dict[str, Any]] = []
        all_ok = True
        for st, coord in SUBMIT_CHAIN:
            xflag = "" if st.startswith("min") else f"-x {st}.nc"
            cmd = (f'source "{env_sh}" && cd "{scratch}" && '
                   f'pmemd -O -i {st}.in -p complex.parm7 -c {coord} -ref {coord} '
                   f'-o {st}.out -r {st}.rst7 {xflag}')
            proc = subprocess.run(["bash", "-c", cmd],
                                  capture_output=True, text=True)
            rc = proc.returncode
            out_path = scratch / f"{st}.out"
            abnormal = (out_path.is_file()
                        and "terminated abnormally"
                        in out_path.read_text(errors="ignore").lower())
            rst = scratch / f"{st}.rst7"
            rst_bytes = rst.stat().st_size if rst.is_file() else 0
            normal = rc == 0 and not abnormal and rst_bytes > 0
            stage_results.append({
                "stage": st, "rc": rc, "normal_termination": normal,
                "rst7": f"{st}.rst7" if rst_bytes > 0 else None,
                "rst7_bytes": rst_bytes,
            })
            print(f"[submit]   {st} rc={rc} normal={normal} rst7={rst_bytes}B",
                  file=sys.stderr)
            if not normal:
                all_ok = False
                break

        outputs["stages"] = stage_results
        final = scratch / "prod.rst7"
        outputs["final_rst7"] = (str(final)
                                 if all_ok and final.is_file() else None)
        errs = [] if all_ok else [
            f"SUBMIT_STAGE_FAILED: {stage_results[-1]['stage']} "
            f"(rc={stage_results[-1]['rc']})"]
        # Keep the scratch on failure (for debugging) or when --keep is set.
        _cleanup(scratch_parent, keep or not all_ok)
        emit_and_exit(ok=all_ok, dry_run=False, outputs=outputs,
                      validation=validation, errors=errs,
                      code=0 if all_ok else 3)
    except Exception as exc:  # never leave a scratch dir on an unexpected crash
        _cleanup(scratch_parent, keep)
        emit_and_exit(ok=False, dry_run=dry_run, code=3,
                      errors=[f"SUBMIT_EXCEPTION: {exc}"])


# ---- Main ----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Edit one parameter in one stage (or group) of the advisor's "
                    "AMBER mdin files — idempotent, bounds-checked, stage-aware.")
    p.add_argument("--md-dir", required=True,
                   help="Directory holding the mdin files (a COPY of the advisor's set).")
    p.add_argument("--stage", default=None,
                   help="A stage (min1, heat-1, press-3, relax, prod, ...) or a "
                        "group (group:third-onward, group:all). Required unless --submit.")
    p.add_argument("--param", default=None,
                   help=f"Parameter to edit: one of {sorted(SUPPORTED_PARAMS)}. "
                        "Required unless --submit.")
    p.add_argument("--value", default=None,
                   help="New value (number). Required unless --submit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan + validate without writing/logging (or, with --submit, "
                        "without running pmemd).")
    p.add_argument("--submit", action="store_true",
                   help="Run the (already-edited) mdin set locally: scratch-copy, rewrite "
                        "AMBERHOME to the local toolchain, reduce nstlim, run the advisor's "
                        "min1..prod pmemd chain to normal termination. Proves it runs.")
    p.add_argument("--reduce-nstlim", type=int, default=120,
                   help="nstlim for the --submit smoke (default 120; ≥100 for the MC barostat).")
    p.add_argument("--keep", action="store_true",
                   help="Keep the --submit scratch dir instead of deleting it.")
    p.add_argument("--enable-restraints", action="store_true",
                   help="Turn positional restraints ON in --stage: sets ntr=1, "
                        "restraint_wt=<--restraint-wt>, restraintmask=<--restraintmask> "
                        "(inserts the mask line where the stage has none).")
    p.add_argument("--disable-restraints", action="store_true",
                   help="Turn positional restraints OFF in --stage: sets ntr=0.")
    p.add_argument("--restraint-wt", default=None,
                   help="Force constant (kcal/mol·Å²) for --enable-restraints.")
    p.add_argument("--restraintmask", default=None,
                   help="Atom-mask string for --enable-restraints (e.g. "
                        "'!:WAT,Cl-,K+,Na+ & !@H=').")
    args = p.parse_args()

    # --- Exactly one operation mode: edit / enable / disable / submit. ---
    n_modes = sum([args.param is not None, bool(args.enable_restraints),
                   bool(args.disable_restraints), bool(args.submit)])
    if n_modes != 1:
        emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                      errors=["MODE_CONFLICT: use exactly one of --param / "
                              "--enable-restraints / --disable-restraints / --submit"])

    # --- Submit mode: run the edited set locally (separate from the edit path). ---
    if args.submit:
        run_submit(Path(args.md_dir).resolve(), args.reduce_nstlim,
                   args.dry_run, args.keep)
        return

    if args.stage is None:
        emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                      errors=["MISSING_REQUIRED_ARG: ['--stage']"])

    # Mode-specific argument validation + a per-file planner closure. `op_param` /
    # `op_value` only label the envelope; the closure carries the actual operation.
    if args.param is not None:
        if args.value is None:
            emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                          errors=["MISSING_REQUIRED_ARG: ['--value']"])
        param = args.param.lower()
        raw = args.value.strip()
        if param not in SUPPORTED_PARAMS:
            emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                          errors=[f"UNSUPPORTED_PARAM: {param!r}; supported: "
                                  f"{sorted(SUPPORTED_PARAMS)}"])
        ok_b, berr, _ = bounds_verdict(param, raw)         # file-independent fast-fail
        if not ok_b:
            emit_and_exit(ok=False, dry_run=args.dry_run, code=1, errors=[berr])
        try:
            rendered = render_value(param, raw)
        except EditError as exc:
            emit_and_exit(ok=False, dry_run=args.dry_run, code=1, errors=[str(exc)])
        op_param, op_value = param, rendered

        def plan_one(fp: Path) -> FileResult:
            return plan_file_edit(fp, param, rendered, raw)

    elif args.enable_restraints:
        if args.restraint_wt is None or args.restraintmask is None:
            emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                          errors=["MISSING_REQUIRED_ARG: --enable-restraints needs "
                                  "--restraint-wt and --restraintmask"])
        # Validate the mask + force constant ONCE (global) for a clean single error.
        try:
            validate_mask(args.restraintmask)
        except EditError as exc:
            emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                          errors=[f"{exc.code}: {exc.msg}"])
        ok_b, berr, _ = bounds_verdict("restraint_wt", args.restraint_wt.strip())
        if not ok_b:
            emit_and_exit(ok=False, dry_run=args.dry_run, code=1, errors=[berr])
        op_param = "enable-restraints"
        op_value = {"restraint_wt": render_value("restraint_wt", args.restraint_wt.strip()),
                    "restraintmask": args.restraintmask.strip()}

        def plan_one(fp: Path) -> FileResult:
            return plan_enable_restraints(fp, args.restraint_wt, args.restraintmask)

    else:  # args.disable_restraints
        op_param, op_value = "disable-restraints", "ntr=0"

        def plan_one(fp: Path) -> FileResult:
            return plan_disable_restraints(fp)

    # Resolve stages (shared across modes).
    if args.stage in GROUPS:
        stages = GROUPS[args.stage]
        is_group = True
    elif args.stage in STAGE_FILES:
        stages = [args.stage]
        is_group = False
    else:
        emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                      errors=[f"UNKNOWN_STAGE: {args.stage!r}; stages "
                              f"{sorted(STAGE_FILES)} or groups {sorted(GROUPS)}"])

    md_dir = Path(args.md_dir).resolve()
    if not md_dir.is_dir():
        emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                      errors=[f"MD_DIR_NOT_FOUND: {md_dir}"])

    # Plan every file's operation IN MEMORY (no writes yet — all-or-nothing batch).
    results: list[FileResult] = []
    for st in stages:
        fpath = md_dir / STAGE_FILES[st]
        if not fpath.is_file():
            results.append(FileResult(STAGE_FILES[st], "error",
                                      reason=f"STAGE_FILE_MISSING: {fpath}"))
            continue
        res = plan_one(fpath)
        # "Not applicable" → skip in a group, but FAIL in a single-stage request.
        if res.status == "skipped" and not is_group:
            code = (res.reason or "").split(":", 1)[0]
            if code in NOT_APPLICABLE_CODES:
                res.status = "error"
        print(f"[{res.file}] {res.status}"
              + (f" — {res.reason}" if res.reason else ""), file=sys.stderr)
        results.append(res)

    hard_errors = [r for r in results if r.status == "error"]

    # 6. Build the per-file output records + validation block.
    file_records = []
    validation_per_file: dict[str, Any] = {}
    for r in results:
        rec: dict[str, Any] = {"file": r.file, "status": r.status, "edits": r.edits}
        if r.warnings:
            rec["warnings"] = r.warnings
        if r.output_schedule is not None:
            rec["output_schedule"] = r.output_schedule
        if r.reason:
            rec["reason"] = r.reason
        file_records.append(rec)
        if r.validation is not None:
            validation_per_file[r.file] = r.validation

    outputs: dict[str, Any] = {
        "md_dir": str(md_dir),
        "stage": args.stage,
        "param": op_param,
        "value": op_value,
        "files": file_records,
    }
    validation = {"per_file": validation_per_file}

    # 7. Hard error anywhere → write NOTHING (all-or-nothing), ok:false.
    if hard_errors:
        errs = [r.reason for r in hard_errors if r.reason]
        emit_and_exit(ok=False, dry_run=args.dry_run, code=3,
                      outputs=outputs, validation=validation, errors=errs)

    # 8. Dry-run → report the plan, write nothing, log nothing.
    if args.dry_run:
        emit_and_exit(ok=True, dry_run=True, outputs=outputs,
                      validation=validation, errors=[], code=0)

    # 9. Commit: atomic write per changed file, then append the change log.
    log_path = md_dir / LOG_NAME
    log_lines: list[str] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in results:
        if r.status == "edited" and r.new_text is not None:
            tmp = md_dir / f".{r.file}.mdin-edit.tmp"
            with open(tmp, "w", newline="") as _fh:  # preserve exact line endings (3.11-safe)
                _fh.write(r.new_text)
            os.replace(tmp, md_dir / r.file)
            for e in r.edits:
                if e["changed"]:
                    log_lines.append(
                        f"{ts}  {r.file}  {e['namelist']}.{e['param']}  "
                        f"{e['old']} -> {e['new']}")
    if log_lines:
        with log_path.open("a") as fh:
            fh.write("\n".join(log_lines) + "\n")
        outputs["log"] = str(log_path)

    emit_and_exit(ok=True, dry_run=False, outputs=outputs,
                  validation=validation, errors=[], code=0)


if __name__ == "__main__":
    main()

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
INT_PARAMS = {"nstlim", "istep1", "istep2", "maxcyc", "ncyc"}
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
    text = path.read_text(newline="")  # newline="" preserves CRLF/LF exactly (byte-minimal)

    if cntrl_span(text) is None:
        return FileResult(name, "error", reason="NAMELIST_NOT_FOUND: no &cntrl block")

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

    # Coupling: temp0 in an nmropt=1 stage with a TEMP0 &wt ramp also sets value2.
    if param == "temp0":
        nmropt = num(cntrl_get(text2, "nmropt"))
        if nmropt == 1 and temp0_wt_span(text2) is not None:
            out2 = set_param_in_block(text2, temp0_wt_span(text2), "value2", rendered)
            if out2.ambiguous:
                return FileResult(name, "error",
                                  reason="AMBIGUOUS_PARAM: value2 appears more than once in &wt TEMP0")
            if out2.found:
                text2 = out2.text
                edits.append({"namelist": "wt", "param": "value2",
                              "old": out2.old, "new": out2.new, "changed": out2.changed})

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

    changed_any = any(e["changed"] for e in edits)
    res = FileResult(name, "edited" if changed_any else "unchanged",
                     edits=edits, new_text=text2 if changed_any else None)

    # Editor's own advisory warning (e.g. cut below the 8 Å project floor).
    _, _, warns = bounds_verdict(param, raw)
    res.warnings = warns

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


# ---- Main ----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Edit one parameter in one stage (or group) of the advisor's "
                    "AMBER mdin files — idempotent, bounds-checked, stage-aware.")
    p.add_argument("--md-dir", required=True,
                   help="Directory holding the mdin files (a COPY of the advisor's set).")
    p.add_argument("--stage", required=True,
                   help="A stage (min1, heat-1, press-3, relax, prod, ...) or a "
                        "group (group:third-onward, group:all).")
    p.add_argument("--param", required=True,
                   help=f"Parameter to edit: one of {sorted(SUPPORTED_PARAMS)}.")
    p.add_argument("--value", required=True, help="New value (number).")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan + validate the edit without writing or logging.")
    args = p.parse_args()

    param = args.param.lower()
    raw = args.value.strip()

    # 1. Param supported?
    if param not in SUPPORTED_PARAMS:
        emit_and_exit(ok=False, dry_run=args.dry_run, code=1,
                      errors=[f"UNSUPPORTED_PARAM: {param!r}; supported: "
                              f"{sorted(SUPPORTED_PARAMS)}"])

    # 2. Value bounds (file-independent) — fail fast, touch nothing.
    ok_b, berr, _ = bounds_verdict(param, raw)
    if not ok_b:
        emit_and_exit(ok=False, dry_run=args.dry_run, code=1, errors=[berr])

    # 3. Canonical rendering.
    try:
        rendered = render_value(param, raw)
    except EditError as exc:
        emit_and_exit(ok=False, dry_run=args.dry_run, code=1, errors=[str(exc)])

    # 4. Resolve stages.
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

    # 5. Plan every file's edit IN MEMORY (no writes yet — all-or-nothing batch).
    results: list[FileResult] = []
    for st in stages:
        fpath = md_dir / STAGE_FILES[st]
        if not fpath.is_file():
            results.append(FileResult(STAGE_FILES[st], "error",
                                      reason=f"STAGE_FILE_MISSING: {fpath}"))
            continue
        res = plan_file_edit(fpath, param, rendered, raw)
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
        if r.reason:
            rec["reason"] = r.reason
        file_records.append(rec)
        if r.validation is not None:
            validation_per_file[r.file] = r.validation

    outputs: dict[str, Any] = {
        "md_dir": str(md_dir),
        "stage": args.stage,
        "param": param,
        "value": rendered,
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
            tmp.write_text(r.new_text, newline="")  # preserve exact line endings
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

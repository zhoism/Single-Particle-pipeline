#!/usr/bin/env python3
"""Independent oracle + spec decision-function for the mdin-edit test harness.

TRUST ANCHOR. This module must NOT share logic with the engine under test
(`scripts/wrapper.py`). In particular it never imports `render_value`, `key_re`,
`VAL`, or `parse_namelists` from the engine. It re-derives everything from raw
text with its own scanner so that an engine bug cannot be masked by an oracle
that made the same mistake ("two wrongs cancel").

Provides:
  - `IndependentScanner` — a from-scratch namelist value extractor.
  - byte-level structural oracle (`verify_single_edit`) — asserts only a numeric
    token changed, nothing appended, siblings/comments preserved.
  - value oracle via `decimal.Decimal` + an independent canonical-format predicate.
  - coupling oracle for temp0 <-> &wt value2.
  - `GroundTruth` — demo facts, re-verified against the actual files at startup.
  - `spec(...)` — the DESIRED (post-fix) contract: expected outcome per
    (selector, param, raw). The harness asserts engine == spec; pre-fix
    divergences are the bugs to fix.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

# ---- Locations -----------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
WRAPPER = SKILL_DIR / "scripts" / "wrapper.py"
DEMO_DIR = Path(os.environ.get(
    "MDIN_DEMO_DIR",
    "/Users/kevinzhou/Downloads/Single Particle/Single Particle/phase3-explicit-solvent-md",
))

SUPPORTED = ("dt", "cut", "temp0", "restraint_wt", "nstlim")
STAGES = ["min1", "min2", "heat-1", "heat-2", "heat-3",
          "press-1", "press-2", "press-3", "relax", "prod"]
STAGE_FILE = {s: f"{s}.in" for s in STAGES}
GROUPS = {
    "group:third-onward": ["heat-3", "press-3", "relax", "prod"],
    "group:all": list(STAGES),
}

# The DESIRED input grammar (ASCII only — uses [0-9], NOT \d which matches unicode).
VAL_ASCII = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


# ---- Independent scanner -------------------------------------------------

def _strip_comment(line: str) -> str:
    """Drop an inline `! comment`, but not a `!` inside quotes (restraintmask)."""
    out, inq = [], None
    for ch in line:
        if inq:
            out.append(ch)
            if ch == inq:
                inq = None
        elif ch in "'\"":
            inq = ch
            out.append(ch)
        elif ch == "!":
            break
        else:
            out.append(ch)
    return "".join(out)


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are NOT inside quotes (so masks survive)."""
    parts, cur, inq = [], [], None
    for ch in s:
        if inq:
            cur.append(ch)
            if ch == inq:
                inq = None
        elif ch in "'\"":
            inq = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


_KV = re.compile(r"\s*([A-Za-z]\w*)\s*=\s*(.*\S)\s*\Z")


@dataclass
class Block:
    name: str
    kv: dict[str, str] = field(default_factory=dict)   # key -> raw value token


class IndependentScanner:
    """Walks lines, tracks the current &namelist, extracts key->token. Independent
    of the engine's regexes. Multiple `key=val` per line (the &wt lines) handled;
    quoted commas/bangs respected."""

    def __init__(self, text: str):
        self.blocks: list[Block] = []
        cur: Optional[Block] = None
        for line in text.split("\n"):
            st = line.strip()
            content = None
            if st.startswith("&"):
                name = st[1:].split()[0].lower() if len(st) > 1 else ""
                cur = Block(name=name)
                self.blocks.append(cur)
                rest = st[1 + len(name):].strip()
                content = rest if rest else None
            elif st == "/":
                cur = None
            elif cur is not None:
                content = line
            if content and cur is not None:
                for piece in _split_top_commas(_strip_comment(content)):
                    m = _KV.match(piece)
                    if m:
                        cur.kv[m.group(1).lower()] = m.group(2).strip()

    def cntrl(self) -> dict[str, str]:
        for b in self.blocks:
            if b.name == "cntrl":
                return b.kv
        return {}

    def wt_temp0(self) -> Optional[dict[str, str]]:
        for b in self.blocks:
            if b.name == "wt" and b.kv.get("type", "").strip("'\"").upper() == "TEMP0":
                return b.kv
        return None


def _as_decimal(tok: str) -> Optional[Decimal]:
    try:
        return Decimal(tok)
    except (InvalidOperation, ValueError):
        return None


# ---- Canonical-format predicate (independent of engine render_value) -----

def canonical_format_ok(param: str, tok: str) -> bool:
    """Is `tok` in the canonical rendered form we require, judged independently?
    Float params: a plain decimal with a dot, no '+', no exponent, no leading-zero
    pile-up. Int params: a bare integer, no dot, no '+', no leading zeros."""
    if param == "nstlim":
        return bool(re.fullmatch(r"-?(?:0|[1-9][0-9]*)", tok)) and tok not in ("-0",)
    # float params: -?INT.FRAC, no leading-zero pile-up, shortest form (no trailing
    # zeros in FRAC except the single ".0" integral form) — matches repr's output.
    m = re.fullmatch(r"-?[0-9]+\.([0-9]+)", tok)
    if not m:
        return False
    intpart = tok.lstrip("-").split(".")[0]
    if len(intpart) > 1 and intpart[0] == "0":
        return False  # e.g. '00.5'
    frac = m.group(1)
    if frac != "0" and frac.endswith("0"):
        return False  # e.g. '0.0020', '1.50' — not shortest form
    return True


# ---- Oracle results ------------------------------------------------------

class OracleError(AssertionError):
    pass


def line_split(text: str) -> list[str]:
    return text.split("\n")


def changed_lines(orig: str, result: str) -> list[int]:
    o, r = line_split(orig), line_split(result)
    if len(o) != len(r):
        raise OracleError(f"line-count changed {len(o)} -> {len(r)} (append/delete!)")
    return [i for i in range(len(o)) if o[i] != r[i]]


def numeric_run(o_line: str, r_line: str) -> tuple[str, str]:
    """Return (old_mid, new_mid): the single contiguous changed run, after peeling
    the common prefix/suffix. Raises if the changed run on either side contains a
    non-numeric byte (i.e. something other than a value token changed)."""
    p = 0
    while p < len(o_line) and p < len(r_line) and o_line[p] == r_line[p]:
        p += 1
    s = 0
    while (s < len(o_line) - p) and (s < len(r_line) - p) and o_line[-1 - s] == r_line[-1 - s]:
        s += 1
    o_mid = o_line[p:len(o_line) - s]
    r_mid = r_line[p:len(r_line) - s]
    numchars = re.compile(r"[0-9.eE+\-]*\Z")
    if not numchars.match(o_mid) or not numchars.match(r_mid):
        raise OracleError(f"non-numeric change on line: {o_line!r} -> {r_line!r}")
    return o_mid, r_mid


def flat_kv(text: str) -> dict[tuple[str, str], str]:
    """{(namelist_name, key): token} across ALL blocks (last-wins on a rare
    name/key collision, which only happens for non-numeric keys we never edit)."""
    out: dict[tuple[str, str], str] = {}
    for b in IndependentScanner(text).blocks:
        for k, v in b.kv.items():
            out[(b.name, k)] = v
    return out


def verify_edit(orig: str, result: str, *,
                allowed_changes: set[tuple[str, str]],
                must_equal: list[tuple[str, str, str]]) -> None:
    """Full independent oracle for a successful edit.

    allowed_changes: the (namelist,key) pairs that MAY differ (a superset; a
      coupled value2 that was already at target simply won't appear in the actual
      changed set). ANY change outside this set is collateral damage → reject.
    must_equal: (namelist,key,expected_value) that MUST hold in the RESULT
      regardless of whether they changed (so a coupling no-op is still verified).

    Asserts, all independently of the engine:
      1. line count unchanged (no append/delete);
      2. every changed *line* is a numeric-token-only change (byte oracle);
      3. no key added/removed; the changed (namelist,key) set ⊆ allowed_changes;
      4. each must_equal key Decimal-equals its expected value AND is canonical.
    """
    for i in changed_lines(orig, result):
        numeric_run(line_split(orig)[i], line_split(result)[i])
    old_kv, new_kv = flat_kv(orig), flat_kv(result)
    if set(old_kv) != set(new_kv):
        raise OracleError(f"key-set changed (added/removed): {set(new_kv) ^ set(old_kv)}")
    changed = {k for k in old_kv if old_kv[k] != new_kv[k]}
    if not changed <= allowed_changes:
        raise OracleError(f"collateral change: {changed - allowed_changes} not in {allowed_changes}")
    for nl, key, exp in must_equal:
        got = new_kv.get((nl, key))
        if got is None:
            raise OracleError(f"{nl}.{key} missing after edit")
        d_got, d_exp = _as_decimal(got), _as_decimal(exp)
        if d_got is None or d_exp is None or d_got != d_exp:
            raise OracleError(f"value mismatch {nl}.{key}: got {got!r} expected ~{exp!r}")
        if not canonical_format_ok("nstlim" if key == "nstlim" else "dt", got):
            raise OracleError(f"non-canonical token {got!r} for {nl}.{key}")


# ---- Ground truth (verified against the demo at startup) -----------------

@dataclass
class GroundTruth:
    present: dict[str, set]       # stage -> set of SUPPORTED params present in &cntrl
    ntr: dict[str, Optional[int]]
    wt_temp0: dict[str, bool]
    current: dict[str, dict[str, str]]  # stage -> {param: current token}
    sha: dict[str, str]
    text: dict[str, str]

    def snapshot_hash(self, stage: str) -> str:
        return self.sha[stage]


# Independently expected demo facts (hardcoded, asserted against the files).
EXPECT_PRESENT_DT_TEMP0 = {"heat-1", "heat-2", "heat-3", "press-1", "press-2", "press-3", "relax", "prod"}
EXPECT_NTR1 = {"min1", "heat-1", "heat-2", "heat-3", "press-1", "press-2", "press-3"}
EXPECT_WT = {"heat-1", "heat-2", "heat-3"}


def load_ground_truth(demo: Path = DEMO_DIR) -> GroundTruth:
    present, ntr, wt, current, sha, text = {}, {}, {}, {}, {}, {}
    for st in STAGES:
        p = demo / STAGE_FILE[st]
        raw = p.read_text()
        text[st] = raw
        sha[st] = hashlib.sha256(raw.encode()).hexdigest()
        sc = IndependentScanner(raw)
        c = sc.cntrl()
        present[st] = {k for k in SUPPORTED if k in c}
        ntr[st] = int(float(c["ntr"])) if "ntr" in c else None
        wt[st] = sc.wt_temp0() is not None
        current[st] = {k: c[k] for k in SUPPORTED if k in c}
    gt = GroundTruth(present, ntr, wt, current, sha, text)
    _verify_ground_truth(gt)
    return gt


def _verify_ground_truth(gt: GroundTruth) -> None:
    """Abort if the demo drifted from what the spec table assumes."""
    errs = []
    for st in STAGES:
        dt_temp0_here = {"dt", "temp0"} <= gt.present[st]
        if (st in EXPECT_PRESENT_DT_TEMP0) != dt_temp0_here:
            errs.append(f"{st}: dt/temp0 presence mismatch ({gt.present[st]})")
        if "cut" not in gt.present[st]:
            errs.append(f"{st}: cut missing (expected in all)")
        if "restraint_wt" not in gt.present[st]:
            errs.append(f"{st}: restraint_wt missing (expected in all)")
        if (gt.ntr[st] == 1) != (st in EXPECT_NTR1):
            errs.append(f"{st}: ntr mismatch (ntr={gt.ntr[st]})")
        if gt.wt_temp0[st] != (st in EXPECT_WT):
            errs.append(f"{st}: &wt TEMP0 presence mismatch")
    # nstlim present in the 8 MD stages only
    for st in STAGES:
        has_ns = "nstlim" in gt.present[st]
        if has_ns != (st in EXPECT_PRESENT_DT_TEMP0):
            errs.append(f"{st}: nstlim presence mismatch (present={has_ns})")
    # the famous heat-3 mismatch must still be there
    h3 = IndependentScanner(gt.text["heat-3"])
    if _as_decimal(h3.cntrl().get("temp0", "")) == _as_decimal((h3.wt_temp0() or {}).get("value2", "")):
        errs.append("heat-3 temp0/&wt value2 are already equal (demo changed?)")
    if errs:
        raise OracleError("GROUND-TRUTH DRIFT:\n  " + "\n  ".join(errs))


# ---- The spec decision-function (DESIRED post-fix contract) ---------------

@dataclass
class Expected:
    global_error: Optional[str] = None    # set -> ok=false, errors=[code], nothing written
    per_file: dict = field(default_factory=dict)  # stage -> ("edited"|"unchanged"|"skipped"|"error", code_or_None)
    ok: bool = True
    warn_cut: bool = False
    coupling: Optional[str] = None        # "wt" (value2 tracks temp0) | "none" | None(n/a)
    rendered: Optional[str] = None        # canonical token (curated values only)


def _value_class_error(param: str, raw: str) -> Optional[str]:
    """Global value-level error per the DESIRED contract, or None if value is OK."""
    raw = raw.strip()
    if not VAL_ASCII.fullmatch(raw):
        return "INVALID_VALUE"
    try:
        v = float(raw)
    except (ValueError, OverflowError):
        return "INVALID_VALUE"
    import math
    if not math.isfinite(v):
        return "INVALID_VALUE"      # e.g. 1e999 -> inf
    if param == "nstlim" and v != int(v):
        return "NONINTEGER_VALUE"
    bounds = {
        "dt": lambda x: 0 < x <= 0.002,
        "temp0": lambda x: 0 < x <= 400.0,
        "restraint_wt": lambda x: x >= 0.0,
        "nstlim": lambda x: x > 0,
        "cut": lambda x: 6.0 <= x <= 12.0,
    }
    if not bounds[param](v):
        return "OUT_OF_BOUNDS"
    return None


def render_expected(param: str, raw: str) -> str:
    """Independent canonical rendering for curated values (authored from the
    contract, NOT imported from the engine). Only valid for in-bounds values."""
    v = float(raw)
    if param == "nstlim":
        return str(int(v))
    if v == int(v):
        return f"{int(v)}.0"
    d = Decimal(raw)
    # normalize: strip exponent, strip trailing zeros but keep one decimal digit
    s = format(d.normalize(), "f")
    if "." not in s:
        s += ".0"
    return s


def spec(selector: str, param: str, raw: str, gt: GroundTruth) -> Expected:
    """Expected outcome under the DESIRED contract."""
    if param not in SUPPORTED:
        return Expected(global_error="UNSUPPORTED_PARAM", ok=False)
    verr = _value_class_error(param, raw)
    if verr:
        return Expected(global_error=verr, ok=False)

    rendered = render_expected(param, raw)
    fv = float(raw)
    warn_cut = (param == "cut" and 6.0 <= fv < 8.0)

    if selector in GROUPS:
        stages, is_group = GROUPS[selector], True
    elif selector in STAGE_FILE:
        stages, is_group = [selector], False
    else:
        return Expected(global_error="UNKNOWN_STAGE", ok=False)

    per = {}
    for st in stages:
        # applicability
        if param == "restraint_wt" and gt.ntr[st] != 1:
            per[st] = ("skipped", "SKIPPED_RESTRAINTS_OFF")
            continue
        if param not in gt.present[st]:
            per[st] = ("skipped", "PARAM_NOT_FOUND")
            continue
        at_target = (_as_decimal(gt.current[st].get(param, "")) == _as_decimal(rendered))
        # temp0 on a heat stage is only truly "at target" if the coupled &wt value2
        # is also already equal (else the engine edits value2 -> status 'edited').
        if param == "temp0" and gt.wt_temp0[st]:
            wt = IndependentScanner(gt.text[st]).wt_temp0() or {}
            if _as_decimal(wt.get("value2", "")) != _as_decimal(rendered):
                at_target = False
        per[st] = ("unchanged" if at_target else "edited", None)

    # single-stage: a not-applicable becomes a hard error
    if not is_group:
        st = stages[0]
        status, code = per[st]
        if status == "skipped" and code in ("PARAM_NOT_FOUND", "SKIPPED_RESTRAINTS_OFF"):
            per[st] = ("error", code)

    ok = all(s != "error" for s, _ in per.values())
    coupling = None
    if param == "temp0":
        # only meaningful where actually edited
        coupling = "wt-or-none"
    return Expected(per_file=per, ok=ok, warn_cut=warn_cut, coupling=coupling,
                    rendered=rendered)

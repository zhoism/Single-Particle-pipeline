#!/usr/bin/env python3
"""AMBER namelist + submit-script sanity check — VENDORED COPY.

PROVENANCE
----------
Vendored verbatim (parser + bounds + heat-3 rule + foreign-path detector) on
2026-06-08 from the Claude Code validator skill:
    .claude/skills/md-param-check/checks/check_amber.py
(in the vault repo `Single-Particle`, branch `main`).

WHY A COPY, NOT AN IMPORT: this is an OpenClaw skill under `project-prime/`; it
must be self-contained and cannot reach into the vault's `.claude/` tree (which is
a separate repo and may move). `mdin-edit/scripts/wrapper.py` imports
`check_amber_in` / `parse_namelists` from THIS file for its post-edit ADVISORY
validation and self-check parse. If the upstream validator's bounds change,
re-sync this file and bump the provenance date.

DIVERGENCE FROM SOURCE: none in this file. The cut-floor policy difference
(mdin-edit accepts 6 ≤ cut < 8 with a WARN, this validator FAILs cut < 8) lives in
the wrapper, NOT here — the wrapper treats this validator's findings as advisory.

Enforces SOP §3 hard limits and catches the recurring inconsistencies (heat-3
temp0/&wt mismatch, hardcoded AMBERHOME).

Usage:
    check_amber_vendored.py <file.in>
    check_amber_vendored.py <dir>/
    check_amber_vendored.py <file1.in> <file2.in> ...

Exit codes:
    0 — all PASS or only WARN (advisory)
    1 — at least one FAIL (physical realism violation)
    2 — invocation error
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---- Limits --------------------------------------------------------------

DT_MAX_SHAKE = 0.002  # ps; 2 fs hard cap with SHAKE on
DT_MAX_NOSHAKE = 0.001  # ps; 1 fs without SHAKE
CUT_MIN = 8.0  # Å
CUT_MAX = 12.0  # Å
GAMMA_LN_MIN = 1.0
GAMMA_LN_MAX = 5.0
ADVISOR_PATH_PATTERNS = [
    r"/Application(/[A-Za-z0-9_./-]+)?",
    r"/opt/(?!homebrew)[A-Za-z0-9_./-]+",
    r"/Users/(?!kevinzhou)[A-Za-z0-9_./-]+",
]


# ---- Result containers ---------------------------------------------------

@dataclass
class Finding:
    level: str  # PASS | WARN | FAIL
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"  {self.level}: {self.rule} — {self.detail}"


@dataclass
class FileReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_fail(self) -> bool:
        return any(f.level == "FAIL" for f in self.findings)

    @property
    def has_warn(self) -> bool:
        return any(f.level == "WARN" for f in self.findings)

    def __str__(self) -> str:
        lines = [f"[{self.path.name}]"]
        lines.extend(str(f) for f in self.findings)
        return "\n".join(lines)


# ---- Namelist parser -----------------------------------------------------

NAMELIST_RE = re.compile(r"&(\w+)\s*(.*?)/", re.DOTALL)
KV_RE = re.compile(r"(\w+)\s*=\s*('[^']*'|\"[^\"]*\"|[\d.eE+\-]+)")


def parse_namelists(content: str) -> dict[str, list[dict[str, str]]]:
    """Parse all &name ... / blocks. Returns dict mapping name → list of blocks
    (multiple blocks of the same name are kept in order)."""
    out: dict[str, list[dict[str, str]]] = {}
    for m in NAMELIST_RE.finditer(content):
        name = m.group(1).lower()
        body = m.group(2)
        # Strip line comments (anything after !)
        body = re.sub(r"!.*", "", body)
        kvs = {k.lower(): v.strip("'\"") for k, v in KV_RE.findall(body)}
        out.setdefault(name, []).append(kvs)
    return out


def num(v: str | None) -> float | None:
    """Best-effort numeric coercion."""
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ---- Validators ----------------------------------------------------------

def check_amber_in(path: Path) -> FileReport:
    """Validate an AMBER .in file."""
    rep = FileReport(path=path)
    content = path.read_text()
    nl = parse_namelists(content)

    if "cntrl" not in nl:
        rep.findings.append(Finding("FAIL", "&cntrl missing", "no &cntrl namelist found"))
        return rep
    c = nl["cntrl"][0]

    # imin = 1 is minimization — different rules
    imin = num(c.get("imin")) or 0
    is_min = imin == 1

    dt = num(c.get("dt"))
    ntc = num(c.get("ntc"))
    ntf = num(c.get("ntf"))
    ntt = num(c.get("ntt"))
    cut = num(c.get("cut"))
    gamma_ln = num(c.get("gamma_ln"))
    temp0 = num(c.get("temp0"))
    tempi = num(c.get("tempi"))
    nmropt = num(c.get("nmropt")) or 0
    ig = num(c.get("ig"))
    ntp = num(c.get("ntp"))
    barostat = num(c.get("barostat"))
    nstlim = num(c.get("nstlim"))
    iwrap = num(c.get("iwrap"))

    # --- dt + SHAKE ---
    if not is_min and dt is not None:
        shake_on = (ntc == 2 and ntf == 2)
        if shake_on:
            if dt > DT_MAX_SHAKE + 1e-9:
                rep.findings.append(Finding("FAIL", "dt > 2 fs cap",
                    f"dt={dt} ps with SHAKE on; SOP §3 cap is {DT_MAX_SHAKE}"))
            else:
                rep.findings.append(Finding("PASS", "dt", f"dt={dt} ps (within SHAKE-on cap)"))
        else:
            if dt > DT_MAX_NOSHAKE + 1e-9:
                rep.findings.append(Finding("FAIL", "dt > 1 fs cap (no SHAKE)",
                    f"dt={dt} ps without SHAKE; need ntc=2 ntf=2 or dt≤{DT_MAX_NOSHAKE}"))
            else:
                rep.findings.append(Finding("PASS", "dt", f"dt={dt} ps (no SHAKE; within cap)"))

    # --- SHAKE coherence ---
    if not is_min and ntc is not None and ntf is not None:
        if ntc == 2 and ntf != 2:
            rep.findings.append(Finding("FAIL", "SHAKE incoherent",
                f"ntc=2 (bonds-to-H constrained) but ntf={ntf} (should be 2 to skip those forces)"))
        elif ntc == 1 and ntf == 1 and (dt or 0) >= 0.002 - 1e-9:
            rep.findings.append(Finding("FAIL", "dt too large without SHAKE",
                f"ntc=1 ntf=1 (no SHAKE) with dt={dt}; need SHAKE or smaller dt"))

    # --- cut ---
    if cut is not None:
        if not (CUT_MIN - 1e-9 <= cut <= CUT_MAX + 1e-9):
            rep.findings.append(Finding("FAIL", "cut out of range",
                f"cut={cut} Å outside [{CUT_MIN}, {CUT_MAX}] for explicit solvent"))
        else:
            rep.findings.append(Finding("PASS", "cut", f"cut={cut} Å"))

    # --- Thermostat ---
    if not is_min and ntt is not None:
        if ntt != 3:
            rep.findings.append(Finding("WARN", "non-Langevin thermostat",
                f"ntt={ntt} (advisor demo uses ntt=3 Langevin); confirm intent"))
        elif gamma_ln is not None and not (GAMMA_LN_MIN <= gamma_ln <= GAMMA_LN_MAX):
            rep.findings.append(Finding("WARN", "gamma_ln out of typical range",
                f"gamma_ln={gamma_ln} outside [{GAMMA_LN_MIN}, {GAMMA_LN_MAX}]"))
        else:
            rep.findings.append(Finding("PASS", "thermostat",
                f"ntt=3 Langevin, gamma_ln={gamma_ln}"))

    # --- temp0 vs &wt value2 (the heat-3 bug class) ---
    if nmropt == 1 and "wt" in nl:
        for wt in nl["wt"]:
            if wt.get("type") in ("TEMP0", "'TEMP0'"):
                v2 = num(wt.get("value2"))
                if v2 is not None and temp0 is not None and abs(v2 - temp0) > 0.5:
                    rep.findings.append(Finding("WARN", "temp0 / &wt mismatch",
                        f"&cntrl temp0={temp0} but &wt TEMP0 ramp ends at value2={v2}; "
                        "Langevin follows the &wt-set TEMP0, system ramps to value2. "
                        "heat-3.in 2026-06-01 lesson — confirm intent."))
                elif v2 is not None and temp0 is not None:
                    rep.findings.append(Finding("PASS", "temp0 / &wt coherent",
                        f"temp0={temp0}, &wt ramp ends at {v2}"))

    # --- Barostat / NPT coherence ---
    if not is_min and ntp is not None and barostat is not None:
        if ntp == 1 and barostat not in (1, 2):
            rep.findings.append(Finding("WARN", "barostat choice",
                f"ntp=1 with barostat={barostat}; advisor demo uses barostat=2 (MC)"))
        if ntp == 0 and barostat in (1, 2):
            rep.findings.append(Finding("WARN", "barostat set but ntp=0",
                f"ntp=0 (no pressure scaling) but barostat={barostat} — harmless but confusing"))

    # --- ig (random seed) ---
    if not is_min and ig is not None and ig != -1:
        rep.findings.append(Finding("WARN", "fixed Langevin seed",
            f"ig={ig} (fixed); use ig=-1 for randomized seed unless determinism is required"))

    # --- iwrap on long production ---
    if not is_min and nstlim is not None and nstlim >= 1_000_000:
        if iwrap is None or iwrap == 0:
            rep.findings.append(Finding("WARN", "iwrap=0 on long run",
                f"nstlim={int(nstlim):,} suggests production; set iwrap=1 to prevent "
                "diffusion artifacts in trajectory"))

    return rep


def check_submit_script(path: Path) -> FileReport:
    """Scan a submit script for portability bugs."""
    rep = FileReport(path=path)
    content = path.read_text()

    # Hardcoded paths to other people's machines
    hits: list[str] = []
    for pat in ADVISOR_PATH_PATTERNS:
        for m in re.finditer(pat, content):
            hits.append(m.group(0))
    if hits:
        uniq = sorted(set(hits))[:5]
        rep.findings.append(Finding("FAIL", "hardcoded foreign path",
            f"found: {', '.join(uniq)} — resolve from $AMBERHOME or `which pmemd`"))
    else:
        rep.findings.append(Finding("PASS", "no foreign hardcoded paths", "scan clean"))

    # AMBERHOME literal assignment
    m = re.search(r'export\s+AMBERHOME\s*=\s*([^\s]+)', content)
    if m:
        val = m.group(1).strip()
        if val.startswith("/"):
            rep.findings.append(Finding("WARN", "AMBERHOME literal-assigned",
                f"AMBERHOME={val}; prefer `AMBERHOME=$(dirname $(dirname $(which pmemd)))` "
                "or rely on the env having it set by amber.sh"))

    # which pmemd as resolution
    if "which pmemd" in content or "command -v pmemd" in content:
        rep.findings.append(Finding("PASS", "portable pmemd resolution", "found"))

    return rep


# ---- CLI -----------------------------------------------------------------

def collect_files(paths: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(path.glob("*.in")))
            for name in ("submit.sh", "run.sh"):
                cand = path / name
                if cand.exists():
                    out.append(cand)
        elif path.is_file():
            out.append(path)
        else:
            print(f"WARN: {p} not found", file=sys.stderr)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    files = collect_files(sys.argv[1:])
    if not files:
        print("ERROR: no files to check", file=sys.stderr)
        return 2

    any_fail = False
    any_warn = False
    for f in files:
        if f.suffix == ".in":
            rep = check_amber_in(f)
        elif f.name in ("submit.sh", "run.sh"):
            rep = check_submit_script(f)
        else:
            continue
        print(rep)
        print()
        any_fail = any_fail or rep.has_fail
        any_warn = any_warn or rep.has_warn

    # Summary
    print("---")
    if any_fail:
        print("VERDICT: FAIL — physical-realism violations present; do not run as-is")
        return 1
    if any_warn:
        print("VERDICT: WARN — advisories only; confirm author intent then proceed")
        return 0
    print("VERDICT: PASS — all checks clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

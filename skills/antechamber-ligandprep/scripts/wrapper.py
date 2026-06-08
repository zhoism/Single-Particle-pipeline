#!/usr/bin/env python3
"""antechamber-ligandprep wrapper.

One exec call per skill turn. The chain (input-prep -> antechamber -> parmchk2)
runs internally; OpenClaw sees a single subprocess invocation that returns one
JSON envelope on stdout. Human-readable progress goes to stderr.

Design constraints (do not violate):
  - Single exec entrypoint per turn (Stage 1c latency finding: each LLM round-trip
    is ~100s on Flash; multi-call chains hit the 120s idle timeout).
  - --dry-run is mandatory (Tier 2 recovery hook from Stage 8 design).
  - JSON envelope to stdout; human-readable progress to stderr.
  - Binaries resolved via PATH first, then $AMBERHOME/bin. Never hardcoded to a
    specific install (the advisor's /Application/software/Amber26 hardcode is
    the anti-example).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_NAME = "antechamber-ligandprep"
REQUIRED_BINS = ["antechamber", "parmchk2", "obabel", "pdb4amber"]
REQUIRED_ENV = ["AMBERHOME"]

KNOWN_EXTS = {".pdb", ".mol2", ".sdf"}


# ---- Envelope ------------------------------------------------------------

def envelope(ok: bool, dry_run: bool,
             outputs: dict[str, Any] | None = None,
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
                  errors: list[str] | None = None,
                  code: int = 0) -> None:
    print(envelope(ok=ok, dry_run=dry_run, outputs=outputs,
                   validation=validation, errors=errors))
    sys.exit(code)


# ---- Binary resolution ---------------------------------------------------

def resolve_bin(name: str) -> str | None:
    """PATH first, then $AMBERHOME/bin. Returns absolute path or None."""
    p = shutil.which(name)
    if p:
        return p
    amber_home = os.environ.get("AMBERHOME")
    if amber_home:
        cand = Path(amber_home) / "bin" / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def preflight(dry_run: bool) -> list[str]:
    errors: list[str] = []
    for b in REQUIRED_BINS:
        if resolve_bin(b) is None:
            errors.append(f"MISSING_BINARY: {b} not on PATH and not in $AMBERHOME/bin")
    for e in REQUIRED_ENV:
        if not os.environ.get(e):
            errors.append(f"MISSING_ENV: {e} not set")
    return errors


# ---- Input classification ------------------------------------------------

INPUT_MODES = ("pdb", "mol2", "sdf", "smiles")


def classify_input(value: str) -> tuple[str, Path | None]:
    """Returns (mode, resolved_path-or-None).

    Heuristic: if `value` is an existing file, classify by extension.
    Otherwise treat as SMILES. We do not try to parse SMILES validity here —
    obabel will fail loudly downstream if the string is garbage.
    """
    path = Path(value).expanduser()
    if path.exists() and path.is_file():
        ext = path.suffix.lower()
        if ext in KNOWN_EXTS:
            return ext.lstrip("."), path.resolve()
        return "unknown", path.resolve()
    return "smiles", None


def pdb_has_hydrogens(path: Path) -> bool:
    """True if any ATOM/HETATM record in the PDB is a hydrogen.

    Trusts the PDB element column (cols 77-78) when populated; falls back to an
    atom-name heuristic (first alphabetic char of the name, after stripping any
    leading digits, is 'H') for hand-edited PDBs that omit the element field.
    Drives routing: an H-complete PDB is fed straight to antechamber so it does
    its OWN bond perception (kekulizes aromatics correctly), instead of routing
    through obabel on a heavy-atom-only skeleton where kekulization fails.
    """
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return False
    for ln in lines:
        if not (ln.startswith("ATOM") or ln.startswith("HETATM")):
            continue
        element = ln[76:78].strip() if len(ln) >= 78 else ""
        if element:
            if element.upper() == "H":
                return True
            continue  # populated element, not H -> not a hydrogen record
        name = ln[12:16].strip() if len(ln) >= 16 else ""
        if name.lstrip("0123456789")[:1].upper() == "H":
            return True
    return False


# ---- Step runner ---------------------------------------------------------

def run_step(label: str, cmd: list[str], run_dir: Path,
             dry_run: bool, cwd: Path | None = None) -> dict[str, Any]:
    print(f"[{label}] {' '.join(cmd)}", file=sys.stderr)
    if dry_run:
        return {"label": label, "planned": True, "cmd": cmd}
    stdout_path = run_dir / f"{label}.out"
    stderr_path = run_dir / f"{label}.err"
    with stdout_path.open("w") as so, stderr_path.open("w") as se:
        result = subprocess.run(cmd, stdout=so, stderr=se,
                                cwd=str(cwd) if cwd else str(run_dir))
    if result.returncode != 0:
        raise StepFailure(label, result.returncode, stderr_path)
    return {"label": label, "rc": 0,
            "stdout": str(stdout_path), "stderr": str(stderr_path)}


class StepFailure(RuntimeError):
    def __init__(self, label: str, rc: int, stderr_path: Path) -> None:
        super().__init__(f"{label} failed (rc={rc}); see {stderr_path}")
        self.label = label
        self.rc = rc
        self.stderr_path = stderr_path


def kekulize_failed(stderr_path: Path) -> bool:
    """True if an obabel step logged a kekulization failure (unreliable bonds)."""
    try:
        return "Failed to kekulize aromatic bonds" in stderr_path.read_text(
            errors="ignore")
    except OSError:
        return False


def _maybe_kekulize_error(label: str, run_dir: Path, dry_run: bool) -> str | None:
    """Turn an obabel kekulization failure into a fatal, inspectable error.

    obabel can silently emit chemically-wrong bond orders (and drop ring N-H)
    when it cannot kekulize an aromatic system; antechamber then types the
    garbage and every downstream gate passes. Surface it loudly instead.
    """
    if dry_run:
        return None
    if kekulize_failed(run_dir / f"{label}.err"):
        return ("AROMATIC_PERCEPTION_FAILED: obabel could not kekulize aromatic "
                f"bonds (see {label}.err); the perceived bond orders are "
                "unreliable, so the GAFF2 typing would be wrong. Supply a "
                "fully-protonated PDB (uses direct antechamber perception) or a "
                "hand-built mol2.")
    return None


# ---- Pipeline ------------------------------------------------------------

def prep_input(mode: str, src: Path | None, smiles: str | None,
               run_dir: Path, charge: int, dry_run: bool
               ) -> tuple[Path, str, list[dict[str, Any]], str | None]:
    """Prepare the ligand for antechamber.

    Returns (prepped_path, antechamber_input_format, step_records, prep_error).
    `antechamber_input_format` is "pdb" when we hand antechamber the raw PDB so it
    perceives its own bonds, else "mol2". `prep_error` is non-None (fatal) only
    when an obabel step failed to kekulize an aromatic system.
    """
    steps: list[dict[str, Any]] = []
    obabel = resolve_bin("obabel") or "obabel"
    pdb4amber = resolve_bin("pdb4amber") or "pdb4amber"

    if mode == "pdb":
        assert src is not None
        # If the PDB already carries hydrogens, feed it straight to antechamber:
        # antechamber's own bond perception kekulizes aromatics correctly, whereas
        # pdb4amber --nohyd + obabel must re-perceive from a heavy-atom-only
        # skeleton and silently mis-types aromatic rings (dropping ring N-H).
        if pdb_has_hydrogens(src):
            steps.append({"label": "01_direct_pdb", "planned": dry_run,
                          "cmd": ["(direct)", str(src)]})
            return src, "pdb", steps, None
        # H-absent: must add hydrogens. pdb4amber cleanup -> obabel protonation.
        cleaned = run_dir / "01_pdb4amber.pdb"
        steps.append(run_step("01_pdb4amber",
                              [pdb4amber, "-i", str(src),
                               "-o", str(cleaned), "--nohyd"],
                              run_dir, dry_run))
        prepped = run_dir / "02_obabel_h.mol2"
        steps.append(run_step("02_obabel_h",
                              [obabel, str(cleaned), "-O", str(prepped),
                               "-p", "7.4"],
                              run_dir, dry_run))
        return prepped, "mol2", steps, _maybe_kekulize_error(
            "02_obabel_h", run_dir, dry_run)

    if mode == "smiles":
        assert smiles is not None
        prepped = run_dir / "01_obabel_smiles.mol2"
        steps.append(run_step("01_obabel_smiles",
                              [obabel, f"-:{smiles}", "-O", str(prepped),
                               "--gen3d", "-p", "7.4"],
                              run_dir, dry_run))
        return prepped, "mol2", steps, _maybe_kekulize_error(
            "01_obabel_smiles", run_dir, dry_run)

    if mode == "mol2":
        assert src is not None
        prepped = run_dir / "01_passthrough.mol2"
        if not dry_run:
            shutil.copy(src, prepped)
        steps.append({"label": "01_passthrough", "planned": dry_run,
                      "cmd": ["cp", str(src), str(prepped)]})
        return prepped, "mol2", steps, None

    if mode == "sdf":
        assert src is not None
        prepped = run_dir / "01_obabel_sdf.mol2"
        steps.append(run_step("01_obabel_sdf",
                              [obabel, str(src), "-O", str(prepped),
                               "-p", "7.4"],
                              run_dir, dry_run))
        return prepped, "mol2", steps, _maybe_kekulize_error(
            "01_obabel_sdf", run_dir, dry_run)

    raise ValueError(f"INVALID_INPUT: unsupported mode {mode!r}")


def run_antechamber(prepped: Path, input_format: str, name: str, charge: int,
                    run_dir: Path, dry_run: bool) -> tuple[Path, dict[str, Any]]:
    antechamber = resolve_bin("antechamber") or "antechamber"
    out_mol2 = run_dir / f"{name}.mol2"
    cmd = [antechamber,
           "-i", str(prepped), "-fi", input_format,
           "-o", str(out_mol2), "-fo", "mol2",
           "-c", "bcc",
           "-nc", str(charge),
           "-at", "gaff2",
           "-rn", name,
           "-pf", "y"]
    if input_format == "pdb":
        # A PDB carries no bond orders, so antechamber runs its own atom+bond-type
        # perception (-j 4) and kekulizes aromatics correctly. acdoctor stays ON
        # (verified to pass and type the indole correctly) — it is a real
        # input-sanity gate, so we do NOT silence it with -dr no.
        cmd += ["-j", "4"]
    rec = run_step("03_antechamber", cmd, run_dir, dry_run)
    return out_mol2, rec


def run_parmchk2(typed_mol2: Path, name: str,
                 run_dir: Path, dry_run: bool) -> tuple[Path, dict[str, Any]]:
    parmchk2 = resolve_bin("parmchk2") or "parmchk2"
    out_frcmod = run_dir / f"{name}.frcmod"
    cmd = [parmchk2,
           "-i", str(typed_mol2), "-f", "mol2",
           "-o", str(out_frcmod),
           "-s", "gaff2"]
    rec = run_step("04_parmchk2", cmd, run_dir, dry_run)
    return out_frcmod, rec


# ---- Validation ----------------------------------------------------------

MOL2_ATOM_BLOCK_RE = re.compile(r"^@<TRIPOS>ATOM\s*$", re.MULTILINE)
MOL2_NEXT_BLOCK_RE = re.compile(r"^@<TRIPOS>\w+\s*$", re.MULTILINE)


def parse_mol2_atoms(mol2_path: Path) -> list[dict[str, Any]]:
    text = mol2_path.read_text()
    m = MOL2_ATOM_BLOCK_RE.search(text)
    if not m:
        return []
    start = m.end()
    rest = text[start:]
    next_block = MOL2_NEXT_BLOCK_RE.search(rest)
    block = rest[: next_block.start()] if next_block else rest
    atoms: list[dict[str, Any]] = []
    for line in block.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        atoms.append({
            "id": parts[0],
            "name": parts[1],
            "atom_type": parts[5],
            "charge": float(parts[8]),
        })
    return atoms


def validate(mol2_path: Path, frcmod_path: Path,
             requested_charge: int) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    validation: dict[str, Any] = {}

    if not mol2_path.exists():
        errors.append("MISSING_PARAMETERS: antechamber did not emit mol2")
        return validation, errors

    atoms = parse_mol2_atoms(mol2_path)
    validation["atom_count"] = len(atoms)
    types = sorted({a["atom_type"] for a in atoms})
    validation["atom_types"] = types

    if any(t.lower() == "du" for t in types):
        errors.append("MISSING_PARAMETERS: atom type 'du' (untyped atom) in mol2")

    if not atoms:
        errors.append("INPUT_PREP_FAILED: mol2 atom block empty")
    else:
        charges = [a["charge"] for a in atoms]
        charge_sum = sum(charges)
        validation["charge_sum"] = round(charge_sum, 6)
        if all(abs(c) < 1e-9 for c in charges):
            errors.append("MISSING_PARAMETERS: mol2 charge column is all zeros")
        elif abs(charge_sum - requested_charge) > 5e-3:
            errors.append(
                f"NET_CHARGE_MISMATCH: sum={charge_sum:.4f}, "
                f"requested={requested_charge}"
            )

    if not frcmod_path.exists():
        errors.append("MISSING_PARAMETERS: parmchk2 did not emit frcmod")
    else:
        frcmod_text = frcmod_path.read_text()
        attn_lines = [
            ln for ln in frcmod_text.splitlines() if "ATTN" in ln
        ]
        validation["frcmod_missing"] = attn_lines
        if attn_lines:
            errors.append(
                f"MISSING_PARAMETERS: parmchk2 emitted {len(attn_lines)} "
                "ATTN lines (see frcmod_missing)"
            )

    return validation, errors


# ---- sqm-failure diagnostic ----------------------------------------------

def classify_step_failure(exc: StepFailure, run_dir: Path) -> str:
    if exc.label.startswith("03_antechamber"):
        for log_name in ("sqm.out", "ANTECHAMBER_AM1BCC.AC", "antechamber.log"):
            log = run_dir / log_name
            if log.exists() and "SQM" in log.read_text().upper():
                return f"SQM_CONVERGENCE_FAILED: {exc}"
        try:
            err = exc.stderr_path.read_text()
            if "sqm" in err.lower() and "converg" in err.lower():
                return f"SQM_CONVERGENCE_FAILED: {exc}"
        except OSError:
            pass
        return f"INPUT_PREP_FAILED: {exc}"
    if exc.label.startswith(("01_pdb4amber", "01_obabel_smiles",
                             "01_obabel_sdf", "02_obabel_h",
                             "01_passthrough", "01_direct_pdb")):
        return f"INPUT_PREP_FAILED: {exc}"
    return f"INPUT_PREP_FAILED: {exc}"


# ---- Main ----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description=("Prepare one ligand for AMBER MD: input file or SMILES "
                     "becomes GAFF2 mol2 + parmchk2 frcmod."))
    p.add_argument("--input", required=True,
                   help="Path to .pdb / .mol2 / .sdf, OR a SMILES string.")
    p.add_argument("--name", default="LIG",
                   help="Residue name (1-4 chars, uppercase).")
    p.add_argument("--charge", type=int, default=0,
                   help="Net formal charge for AM1-BCC.")
    p.add_argument("--output-dir", default="./",
                   help="Where to write artifacts.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan commands without executing.")
    args = p.parse_args()

    pre_errors = preflight(args.dry_run)
    if pre_errors and not args.dry_run:
        emit_and_exit(ok=False, dry_run=False, errors=pre_errors, code=1)

    if not re.fullmatch(r"[A-Z0-9]{1,4}", args.name):
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=[f"INVALID_INPUT: --name {args.name!r} must be "
                              "1-4 uppercase letters/digits"],
                      code=1)

    mode, src = classify_input(args.input)
    if mode == "unknown":
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=[f"INVALID_INPUT: unsupported file extension on "
                              f"{args.input}"],
                      code=1)

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = out_dir / f"{SKILL_NAME}-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    smiles_in = args.input if mode == "smiles" else None
    planned_steps: list[dict[str, Any]] = []

    try:
        prepped, input_format, prep_records, prep_error = prep_input(
            mode, src, smiles_in, run_dir, args.charge, args.dry_run)
        planned_steps.extend(prep_records)
        if prep_error:
            emit_and_exit(ok=False, dry_run=args.dry_run,
                          outputs={"run_dir": str(run_dir),
                                   "planned_steps": planned_steps},
                          errors=[prep_error], code=2)

        out_mol2, ac_rec = run_antechamber(prepped, input_format, args.name,
                                           args.charge, run_dir, args.dry_run)
        planned_steps.append(ac_rec)

        out_frcmod, pc_rec = run_parmchk2(out_mol2, args.name, run_dir,
                                          args.dry_run)
        planned_steps.append(pc_rec)
    except StepFailure as exc:
        diag = classify_step_failure(exc, run_dir)
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      outputs={"run_dir": str(run_dir),
                               "planned_steps": planned_steps},
                      errors=[diag], code=2)
    except ValueError as exc:
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=[str(exc)], code=1)

    if args.dry_run:
        emit_and_exit(ok=True, dry_run=True,
                      outputs={"run_dir": str(run_dir),
                               "mol2": str(out_mol2),
                               "frcmod": str(out_frcmod),
                               "planned_steps": planned_steps},
                      validation={}, errors=[], code=0)

    validation, errors = validate(out_mol2, out_frcmod, args.charge)
    outputs = {"mol2": str(out_mol2),
               "frcmod": str(out_frcmod),
               "run_dir": str(run_dir)}
    emit_and_exit(ok=not errors, dry_run=False,
                  outputs=outputs, validation=validation,
                  errors=errors, code=0 if not errors else 3)


if __name__ == "__main__":
    main()

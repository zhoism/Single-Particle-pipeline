#!/usr/bin/env python3
"""tleap-build wrapper (Phase 3 Stage 3).

Combine a protein PDB with an (optional) parameterized ligand into AMBER
topology + coordinate files, in the order that keeps later analysis correct.

One exec call per skill turn: the chain (pdb4amber -> generate leap.in -> tleap)
runs internally; OpenClaw sees a single subprocess invocation that returns one
JSON envelope on stdout. Human-readable progress goes to stderr.

Design constraints (shared with antechamber-ligandprep, do not violate):
  - Single exec entrypoint per turn (Stage 1c latency finding).
  - --dry-run is mandatory (Tier 2 recovery hook from Stage 8 design).
  - JSON envelope to stdout; human-readable progress to stderr.
  - Binaries resolved via PATH first, then $AMBERHOME/bin. Never hardcoded.

Correctness rules baked in (from Research_amber_md_skill.md steal-list):
  - comp_dry.top is saved BEFORE solvateoct (steal #1). The upstream amber-md
    leap.in gets this WRONG; we don't. A dry topology that contains water breaks
    every stripped-trajectory analysis downstream.
  - protein.top + ligand.top are saved separately so MM-GBSA has its components.
  - The ligand is loaded as a mol2 and renumbered automatically by `combine`,
    so the brittle `sed 's/MOL A   6/.../'` residue-collision hack is unneeded.
  - Inputs are copied into the run dir and referenced by bare filename, because
    the project path contains a space ("Single Particle") that LEaP mishandles.
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


SKILL_NAME = "tleap-build"
REQUIRED_BINS = ["tleap", "pdb4amber"]
REQUIRED_ENV = ["AMBERHOME"]


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


def preflight() -> list[str]:
    errors: list[str] = []
    for b in REQUIRED_BINS:
        if resolve_bin(b) is None:
            errors.append(f"MISSING_BINARY: {b} not on PATH and not in $AMBERHOME/bin")
    for e in REQUIRED_ENV:
        if not os.environ.get(e):
            errors.append(f"MISSING_ENV: {e} not set")
    return errors


# ---- Step runner ---------------------------------------------------------

class StepFailure(RuntimeError):
    def __init__(self, label: str, rc: int, stderr_path: Path) -> None:
        super().__init__(f"{label} failed (rc={rc}); see {stderr_path}")
        self.label = label
        self.rc = rc
        self.stderr_path = stderr_path


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


# ---- prmtop introspection ------------------------------------------------

def prmtop_natom(top_path: Path) -> int | None:
    """Read NATOM (first POINTERS value) from an AMBER prmtop. Returns None on
    any parse failure — callers treat None as 'could not verify'."""
    try:
        text = top_path.read_text()
    except OSError:
        return None
    m = re.search(r"%FLAG POINTERS\s*\n%FORMAT\([^)]*\)\s*\n", text)
    if not m:
        return None
    rest = text[m.end():]
    # First whitespace-delimited integer in the data block is NATOM.
    tok = rest.split()
    if not tok:
        return None
    try:
        return int(tok[0])
    except ValueError:
        return None


# Standard residue labels expected in a dry protein(+ligand) topology: the 20 AAs
# + AMBER protonation / disulfide variants. Anything else in comp_dry that is NOT
# the ligand is a stray crystallographic residue (cofactor / metal / buffer /
# second ligand) that loadpdb silently absorbed.
STD_RESIDUES = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "HIE", "HID", "HIP", "CYX", "CYM", "ASH", "GLH", "LYN",
}


def residue_labels(top_path: Path) -> list[str] | None:
    """Read the %FLAG RESIDUE_LABEL block from an AMBER prmtop. None on any parse
    failure (callers treat None as 'could not verify')."""
    try:
        text = top_path.read_text()
    except OSError:
        return None
    m = re.search(r"%FLAG RESIDUE_LABEL\s*\n%FORMAT\([^)]*\)\s*\n(.*?)(?=\n%FLAG)",
                  text, re.S)
    if not m:
        return None
    return m.group(1).split()


# ---- leap.in generation --------------------------------------------------

def build_leap_in(*, protein_pdb: str, ligand_mol2: str | None,
                  ligand_frcmod: str | None, protein_ff: str, water: str,
                  ligand_ff: str, buffer: float) -> str:
    """Return leap.in text. All filenames are bare (relative to run_dir).

    Save order is load-bearing: comp_dry is saved BEFORE solvateoct.
    """
    lines: list[str] = [
        f"source leaprc.protein.{protein_ff}",
        f"source leaprc.water.{water}",
    ]
    has_ligand = bool(ligand_mol2)
    if has_ligand:
        lines += [
            f"source leaprc.{ligand_ff}",
            f"LIG = loadmol2 {ligand_mol2}",
            f"loadamberparams {ligand_frcmod}",
            "check LIG",
        ]
    lines += [
        f"prot = loadpdb {protein_pdb}",
        "check prot",
    ]
    if has_ligand:
        # Independent-component topologies for MM-GBSA decomposition.
        lines += [
            "saveamberparm prot protein.top protein.crd",
            "saveamberparm LIG ligand.top ligand.crd",
            "comp = combine { prot LIG }",
        ]
    else:
        lines += ["comp = prot"]
    lines += [
        # --- dry topology saved BEFORE solvation (steal #1) ---
        "saveamberparm comp comp_dry.top comp_dry.crd",
        f"solvateoct comp TIP3PBOX {buffer}",
        # addions2 with 0 neutralizes regardless of net-charge sign; running
        # both Na+ and Cl- is idempotent (the unneeded one adds nothing).
        "addions2 comp Na+ 0",
        "addions2 comp Cl- 0",
        "saveamberparm comp comp_oct.top comp_oct.crd",
        "savepdb comp comp_oct.pdb",
        "quit",
    ]
    return "\n".join(lines) + "\n"


# ---- leap.log parsing ----------------------------------------------------

def parse_leap_log(log_path: Path) -> tuple[list[str], dict[str, Any]]:
    """Return (error_lines, info). info captures residual charge + water adds."""
    errors: list[str] = []
    info: dict[str, Any] = {}
    if not log_path.exists():
        return ["LEAP_NO_LOG: tleap produced no leap.log"], info
    text = log_path.read_text()
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("ERROR") or "FATAL" in s:
            errors.append(s)
        # solvateoct reports added solvent residues.
        m = re.search(r"Added\s+(\d+)\s+residues", s)
        if m:
            info.setdefault("solvent_residues_added", []).append(int(m.group(1)))
        # residual charge after neutralization.
        m = re.search(r"unperturbed charge:\s*(-?\d+\.\d+)", s)
        if m:
            info["residual_charge"] = float(m.group(1))
    return errors, info


# ---- Validation ----------------------------------------------------------

def validate(run_dir: Path, has_ligand: bool,
             log_errors: list[str], log_info: dict[str, Any],
             ligand_resname: str | None = None
             ) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = list(log_errors)
    validation: dict[str, Any] = dict(log_info)

    required = ["comp_dry.top", "comp_dry.crd", "comp_oct.top", "comp_oct.crd",
                "comp_oct.pdb"]
    if has_ligand:
        required += ["protein.top", "protein.crd", "ligand.top", "ligand.crd"]
    missing = [f for f in required if not (run_dir / f).exists()]
    if missing:
        errors.append(f"MISSING_OUTPUTS: tleap did not emit {missing}")
        return validation, errors

    dry_atoms = prmtop_natom(run_dir / "comp_dry.top")
    oct_atoms = prmtop_natom(run_dir / "comp_oct.top")
    validation["dry_atoms"] = dry_atoms
    validation["solvated_atoms"] = oct_atoms

    # Residue-identity gate: comp_dry must contain ONLY standard AAs + the ligand.
    # A stray crystallographic residue (cofactor / metal / buffer / second ligand
    # left in the input PDB) is absorbed by loadpdb and passes the atom-count
    # invariants (it is counted in both topologies), then silently mis-shifts the
    # downstream last-residue==ligand mask assumption -> wrong-but-green analysis.
    labels = residue_labels(run_dir / "comp_dry.top")
    if labels is not None:
        allowed = set(STD_RESIDUES)
        if ligand_resname:
            allowed.add(ligand_resname.upper())
        stray = sorted({r for r in labels if r.upper() not in allowed})
        validation["nonstandard_residues"] = stray
        if stray:
            lig = f" ({ligand_resname})" if ligand_resname else ""
            errors.append(
                f"UNKNOWN_RESIDUE_IN_INPUT: comp_dry contains non-standard residue(s) "
                f"{stray} that are neither a standard amino acid nor the ligand{lig}. "
                "A stray crystallographic HETATM (cofactor / metal / buffer / second "
                "ligand) left in the input PDB was silently absorbed and would corrupt "
                "the analysis residue masks — remove or explicitly parameterize it.")

    # Steal #1 sanity test: a real dry topology is much smaller than solvated.
    # If dry >= solvated, comp_dry was saved after solvation (the upstream bug).
    if dry_atoms is not None and oct_atoms is not None:
        if oct_atoms <= dry_atoms:
            errors.append(
                f"DRY_TOPOLOGY_CONTAMINATED: comp_dry has {dry_atoms} atoms, "
                f"comp_oct has {oct_atoms}; dry must be strictly smaller "
                "(comp_dry.top was likely saved AFTER solvateoct)")
        validation["waters_plus_ions_atoms"] = oct_atoms - dry_atoms

    # Component invariant: protein + ligand atoms == dry complex atoms.
    if has_ligand:
        p_atoms = prmtop_natom(run_dir / "protein.top")
        l_atoms = prmtop_natom(run_dir / "ligand.top")
        validation["protein_atoms"] = p_atoms
        validation["ligand_atoms"] = l_atoms
        if (p_atoms is not None and l_atoms is not None
                and dry_atoms is not None and p_atoms + l_atoms != dry_atoms):
            errors.append(
                f"COMPONENT_ATOM_MISMATCH: protein({p_atoms}) + "
                f"ligand({l_atoms}) != dry complex({dry_atoms})")

    # Residual charge after neutralization should be ~integer-zero.
    rc = validation.get("residual_charge")
    if rc is not None and abs(rc) > 0.5:
        errors.append(f"SYSTEM_NOT_NEUTRAL: residual charge {rc} after addions2")

    return validation, errors


# ---- Main ----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description=("Build AMBER topology/coords from a protein PDB + optional "
                     "parameterized ligand (mol2+frcmod), solvate, neutralize."))
    p.add_argument("--protein", required=True, help="Protein PDB path.")
    p.add_argument("--ligand-mol2", default=None,
                   help="GAFF2 mol2 from antechamber-ligandprep. Omit for "
                        "protein-only systems.")
    p.add_argument("--ligand-frcmod", default=None,
                   help="frcmod from antechamber-ligandprep (required with "
                        "--ligand-mol2).")
    p.add_argument("--name", default="LIG", help="Ligand residue name (cosmetic).")
    p.add_argument("--protein-ff", default="ff19SB", help="Protein force field.")
    p.add_argument("--water", default="tip3p", help="Water model.")
    p.add_argument("--ligand-ff", default="gaff2", help="Small-molecule FF.")
    p.add_argument("--buffer", type=float, default=10.0,
                   help="solvateoct buffer (Angstrom).")
    p.add_argument("--skip-protein-clean", action="store_true",
                   help="Skip pdb4amber pre-clean of the protein PDB.")
    p.add_argument("--output-dir", default="./", help="Where to write artifacts.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan + write leap.in without executing tleap.")
    args = p.parse_args()

    pre = preflight()
    if pre and not args.dry_run:
        emit_and_exit(ok=False, dry_run=False, errors=pre, code=1)

    has_ligand = args.ligand_mol2 is not None
    if has_ligand and args.ligand_frcmod is None:
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=["INVALID_INPUT: --ligand-mol2 requires "
                              "--ligand-frcmod"], code=1)

    protein_src = Path(args.protein).expanduser()
    if not protein_src.is_file():
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      errors=[f"INVALID_INPUT: protein PDB not found: "
                              f"{args.protein}"], code=1)
    if has_ligand:
        for label, val in (("--ligand-mol2", args.ligand_mol2),
                           ("--ligand-frcmod", args.ligand_frcmod)):
            if not Path(val).expanduser().is_file():
                emit_and_exit(ok=False, dry_run=args.dry_run,
                              errors=[f"INVALID_INPUT: {label} not found: {val}"],
                              code=1)

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = out_dir / f"{SKILL_NAME}-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Copy inputs into run_dir with bare names (path-with-space safety).
    planned: list[dict[str, Any]] = []
    protein_in = run_dir / "protein_in.pdb"
    if not args.dry_run:
        shutil.copy(protein_src, protein_in)
    lig_mol2_name = lig_frcmod_name = None
    if has_ligand:
        lig_mol2_name = f"{args.name}.mol2"
        lig_frcmod_name = f"{args.name}.frcmod"
        if not args.dry_run:
            shutil.copy(Path(args.ligand_mol2).expanduser(),
                        run_dir / lig_mol2_name)
            shutil.copy(Path(args.ligand_frcmod).expanduser(),
                        run_dir / lig_frcmod_name)

    try:
        # Step 1: protein pre-clean (HIS/CYX naming, strip alt-locs).
        protein_for_leap = "protein_in.pdb"
        if not args.skip_protein_clean:
            pdb4amber = resolve_bin("pdb4amber") or "pdb4amber"
            # --nohyd strips input hydrogens (often PDB v2 names like 1HB that
            # ff19SB rejects); LEaP rebuilds them with correct v3 names.
            planned.append(run_step(
                "01_pdb4amber",
                [pdb4amber, "-i", "protein_in.pdb", "-o", "protein_clean.pdb",
                 "--nohyd"],
                run_dir, args.dry_run))
            protein_for_leap = "protein_clean.pdb"

        # Step 2: write leap.in.
        leap_text = build_leap_in(
            protein_pdb=protein_for_leap, ligand_mol2=lig_mol2_name,
            ligand_frcmod=lig_frcmod_name, protein_ff=args.protein_ff,
            water=args.water, ligand_ff=args.ligand_ff, buffer=args.buffer)
        leap_path = run_dir / "leap.in"
        if not args.dry_run:
            leap_path.write_text(leap_text)
        planned.append({"label": "02_write_leap_in", "planned": args.dry_run,
                        "leap_in": leap_text})

        # Step 3: run tleap.
        tleap = resolve_bin("tleap") or "tleap"
        planned.append(run_step("03_tleap", [tleap, "-f", "leap.in"],
                                run_dir, args.dry_run))
    except StepFailure as exc:
        emit_and_exit(ok=False, dry_run=args.dry_run,
                      outputs={"run_dir": str(run_dir), "planned_steps": planned},
                      errors=[f"TLEAP_STEP_FAILED: {exc}"], code=2)

    if args.dry_run:
        emit_and_exit(ok=True, dry_run=True,
                      outputs={"run_dir": str(run_dir),
                               "leap_in": str(leap_path),
                               "planned_steps": planned},
                      validation={}, errors=[], code=0)

    log_errors, log_info = parse_leap_log(run_dir / "leap.log")
    validation, errors = validate(run_dir, has_ligand, log_errors, log_info,
                                  ligand_resname=(args.name if has_ligand else None))

    def abspath(name: str) -> str:
        return str(run_dir / name)

    outputs = {
        "run_dir": str(run_dir),
        "comp_oct_top": abspath("comp_oct.top"),
        "comp_oct_crd": abspath("comp_oct.crd"),
        "comp_dry_top": abspath("comp_dry.top"),
        "comp_dry_crd": abspath("comp_dry.crd"),
        "comp_oct_pdb": abspath("comp_oct.pdb"),
        "leap_in": str(leap_path),
    }
    if has_ligand:
        outputs["protein_top"] = abspath("protein.top")
        outputs["ligand_top"] = abspath("ligand.top")

    emit_and_exit(ok=not errors, dry_run=False, outputs=outputs,
                  validation=validation, errors=errors,
                  code=0 if not errors else 3)


if __name__ == "__main__":
    main()

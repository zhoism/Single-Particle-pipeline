#!/usr/bin/env python3
"""Deterministic oracle for tleap-build's pure introspection / generation core.

Covers four pure functions in scripts/wrapper.py, every expected value
HAND-DERIVED from the function body (never produced by re-calling the function
under test):

  - prmtop_natom(top)     : first POINTERS integer (NATOM); None on malformed.
  - residue_labels(top)   : %FLAG RESIDUE_LABEL block as a token list; None on
                            malformed.
  - build_leap_in(...)    : the generated leap input, ligand + no-ligand cases.
  - validate(run_dir,...) : the real error-code substrings the gate emits
                            (DRY_TOPOLOGY_CONTAMINATED / UNKNOWN_RESIDUE_IN_INPUT
                            / COMPONENT_ATOM_MISMATCH), plus the clean-pass case.

Bootstrap mirrors plip-profile/tests/test_engine.py: insert the skill's scripts
dir on sys.path, import wrapper as W, a check(name,cond,detail) helper, print
failures, exit 1 if any FAIL else 0.

Run: MPLBACKEND=Agg <conda python> test_build_oracle.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import wrapper as W  # noqa: E402

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


# ---- prmtop fixture builders --------------------------------------------
# AMBER prmtop block layout the wrapper's regexes key off:
#   %FLAG <NAME>\n%FORMAT(...)\n<data>\n
# prmtop_natom keys off POINTERS; residue_labels keys off RESIDUE_LABEL and is
# terminated by a following "\n%FLAG" (non-greedy capture). The charge-scale of
# 18.2223 is irrelevant here (we never trigger the neutrality gate; we leave the
# CHARGE block off so prmtop_net_charge returns None = 'could not verify').


def mk_pointers(natom: int, *, extra_ints: str = "") -> str:
    """A POINTERS block whose first integer is `natom`. 10I8-style spacing is
    irrelevant — the wrapper does a whitespace split and takes tok[0]."""
    data = f"{natom}{(' ' + extra_ints) if extra_ints else ''}"
    return f"%FLAG POINTERS\n%FORMAT(10I8)\n  {data}\n"


def mk_residue_label(labels: list[str]) -> str:
    body = " ".join(labels)
    return f"%FLAG RESIDUE_LABEL\n%FORMAT(20a4)\n{body}\n"


def write_prmtop(path: Path, *, natom: int | None = None,
                 res_labels: list[str] | None = None) -> Path:
    """Compose a minimal prmtop. RESIDUE_LABEL (if present) is followed by a
    trailing %FLAG MASS so the non-greedy capture terminator matches."""
    parts = ["%VERSION test\n%FLAG TITLE\n%FORMAT(20a4)\nt\n"]
    if natom is not None:
        parts.append(mk_pointers(natom))
    if res_labels is not None:
        parts.append(mk_residue_label(res_labels))
    # Trailing flag so residue_labels' (?=\n%FLAG) terminator always matches.
    parts.append("%FLAG MASS\n%FORMAT(5E16.8)\n  1.00800000\n")
    path.write_text("".join(parts))
    return path


# ---- (1) prmtop_natom ----------------------------------------------------

def test_prmtop_natom(tmp: Path):
    # Well-formed: first POINTERS integer is NATOM. Hand-derived: tok[0]==304.
    p = write_prmtop(tmp / "good.top", natom=304,
                     res_labels=None)
    # add trailing ints to confirm only the FIRST is taken
    # (rewrite with extra ints inline)
    (tmp / "good2.top").write_text(
        "%VERSION test\n%FLAG POINTERS\n%FORMAT(10I8)\n"
        "     304      12      99\n%FLAG MASS\n%FORMAT(5E16.8)\n  1.0\n")
    check("natom reads first POINTERS int", W.prmtop_natom(tmp / "good2.top") == 304,
          repr(W.prmtop_natom(tmp / "good2.top")))
    check("natom single-value block", W.prmtop_natom(p) == 304,
          repr(W.prmtop_natom(p)))

    # Malformed: no POINTERS flag at all -> regex miss -> None.
    nop = tmp / "nopointers.top"
    nop.write_text("%VERSION test\n%FLAG MASS\n%FORMAT(5E16.8)\n  1.0\n")
    check("natom no POINTERS flag -> None", W.prmtop_natom(nop) is None,
          repr(W.prmtop_natom(nop)))

    # Truncated: POINTERS header present but data block empty (no tokens) -> None.
    trunc = tmp / "trunc.top"
    trunc.write_text("%VERSION test\n%FLAG POINTERS\n%FORMAT(10I8)\n")
    check("natom empty data block -> None", W.prmtop_natom(trunc) is None,
          repr(W.prmtop_natom(trunc)))

    # First token non-integer -> int() ValueError -> None.
    badtok = tmp / "badtok.top"
    badtok.write_text("%VERSION test\n%FLAG POINTERS\n%FORMAT(10I8)\n  XX  12\n")
    check("natom non-integer tok0 -> None", W.prmtop_natom(badtok) is None,
          repr(W.prmtop_natom(badtok)))

    # Nonexistent file -> OSError caught -> None.
    check("natom missing file -> None",
          W.prmtop_natom(tmp / "does_not_exist.top") is None)


# ---- (2) residue_labels --------------------------------------------------

def test_residue_labels(tmp: Path):
    labs = ["ALA", "GLY", "HIE", "MOL", "WAT", "WAT", "Na+"]
    p = write_prmtop(tmp / "res.top", natom=10, res_labels=labs)
    # Hand-derived: .split() of "ALA GLY HIE MOL WAT WAT Na+" preserving order
    # and duplicates.
    check("residue_labels exact list", W.residue_labels(p) == labs,
          repr(W.residue_labels(p)))

    # No RESIDUE_LABEL flag -> None.
    nores = tmp / "nores.top"
    nores.write_text("%VERSION test\n%FLAG MASS\n%FORMAT(5E16.8)\n  1.0\n")
    check("residue_labels no flag -> None", W.residue_labels(nores) is None,
          repr(W.residue_labels(nores)))

    # Nonexistent file -> None.
    check("residue_labels missing file -> None",
          W.residue_labels(tmp / "nope.top") is None)


# ---- (3) build_leap_in ---------------------------------------------------

def test_build_leap_in():
    # --- No-ligand case ---
    txt = W.build_leap_in(protein_pdb="protein_clean.pdb", ligand_mol2=None,
                          ligand_frcmod=None, protein_ff="ff19SB",
                          water="tip3p", ligand_ff="gaff2", buffer=10.0)
    lines = txt.splitlines()
    # Hand-derived expected line set for the no-ligand branch:
    check("noligand source protein ff",
          "source leaprc.protein.ff19SB" in lines, txt)
    check("noligand source water",
          "source leaprc.water.tip3p" in lines, txt)
    check("noligand loadpdb",
          "prot = loadpdb protein_clean.pdb" in lines, txt)
    check("noligand comp = prot", "comp = prot" in lines, txt)
    check("noligand saves dry BEFORE solvate",
          lines.index("saveamberparm comp comp_dry.top comp_dry.crd")
          < lines.index("solvateoct comp TIP3PBOX 10.0"), txt)
    check("noligand solvateoct line",
          "solvateoct comp TIP3PBOX 10.0" in lines, txt)
    check("noligand addions2 Na+", "addions2 comp Na+ 0" in lines, txt)
    check("noligand addions2 Cl-", "addions2 comp Cl- 0" in lines, txt)
    check("noligand saveamberparm oct",
          "saveamberparm comp comp_oct.top comp_oct.crd" in lines, txt)
    check("noligand savepdb", "savepdb comp comp_oct.pdb" in lines, txt)
    check("noligand quit", lines[-1] == "quit", repr(lines[-1]))
    # No ligand commands must be present.
    check("noligand no loadmol2", "loadmol2" not in txt, txt)
    check("noligand no loadamberparams", "loadamberparams" not in txt, txt)
    check("noligand no combine", "combine" not in txt, txt)
    check("noligand no source gaff", "source leaprc.gaff2" not in txt, txt)

    # --- Ligand case ---
    txt2 = W.build_leap_in(protein_pdb="protein_clean.pdb",
                           ligand_mol2="LIG.mol2", ligand_frcmod="LIG.frcmod",
                           protein_ff="ff19SB", water="tip3p",
                           ligand_ff="gaff2", buffer=12.0)
    L = txt2.splitlines()
    check("ligand source small-mol ff",
          "source leaprc.gaff2" in L, txt2)
    check("ligand loadmol2", "LIG = loadmol2 LIG.mol2" in L, txt2)
    check("ligand loadamberparams frcmod",
          "loadamberparams LIG.frcmod" in L, txt2)
    check("ligand check LIG", "check LIG" in L, txt2)
    check("ligand save protein component",
          "saveamberparm prot protein.top protein.crd" in L, txt2)
    check("ligand save ligand component",
          "saveamberparm LIG ligand.top ligand.crd" in L, txt2)
    check("ligand combine", "comp = combine { prot LIG }" in L, txt2)
    check("ligand dry BEFORE solvate",
          L.index("saveamberparm comp comp_dry.top comp_dry.crd")
          < L.index("solvateoct comp TIP3PBOX 12.0"), txt2)
    check("ligand buffer threaded through",
          "solvateoct comp TIP3PBOX 12.0" in L, txt2)
    # combine must come AFTER the per-component saves and BEFORE the dry save.
    check("ligand combine after components, before dry",
          L.index("saveamberparm LIG ligand.top ligand.crd")
          < L.index("comp = combine { prot LIG }")
          < L.index("saveamberparm comp comp_dry.top comp_dry.crd"), txt2)
    # No-ligand branch line must NOT appear.
    check("ligand no 'comp = prot'", "comp = prot" not in L, txt2)


# ---- (4) validate --------------------------------------------------------

def _make_run_dir(base: Path, name: str) -> Path:
    rd = base / name
    rd.mkdir(parents=True, exist_ok=True)
    return rd


def test_validate_clean_noligand(tmp: Path):
    """No-ligand, dry(304) < solvated(5000), all-standard residues, no CHARGE
    block in comp_oct (-> net_charge None -> neutrality gate cannot fire).
    Hand-derived: errors == [] (clean pass)."""
    rd = _make_run_dir(tmp, "v_clean")
    write_prmtop(rd / "comp_dry.top", natom=304,
                 res_labels=["ALA", "GLY", "HIS", "VAL"])
    write_prmtop(rd / "comp_oct.top", natom=5000, res_labels=None)
    # Required-output existence files (content irrelevant to the gates here).
    for f in ("comp_dry.crd", "comp_oct.crd", "comp_oct.pdb"):
        (rd / f).write_text("x\n")
    validation, errors = W.validate(rd, has_ligand=False,
                                    log_errors=[], log_info={})
    check("clean noligand no errors", errors == [], repr(errors))
    check("clean noligand dry_atoms=304", validation.get("dry_atoms") == 304,
          repr(validation.get("dry_atoms")))
    check("clean noligand solvated_atoms=5000",
          validation.get("solvated_atoms") == 5000,
          repr(validation.get("solvated_atoms")))
    # waters_plus_ions_atoms = oct - dry = 5000 - 304 = 4696 (hand-derived).
    check("clean noligand waters_plus_ions=4696",
          validation.get("waters_plus_ions_atoms") == 4696,
          repr(validation.get("waters_plus_ions_atoms")))


def test_validate_contaminated(tmp: Path):
    """dry(5000) >= solvated(304) -> DRY_TOPOLOGY_CONTAMINATED (the upstream
    save-after-solvate bug). All residues standard so it is the ONLY gate."""
    rd = _make_run_dir(tmp, "v_contam")
    write_prmtop(rd / "comp_dry.top", natom=5000,
                 res_labels=["ALA", "GLY"])
    write_prmtop(rd / "comp_oct.top", natom=304, res_labels=None)
    for f in ("comp_dry.crd", "comp_oct.crd", "comp_oct.pdb"):
        (rd / f).write_text("x\n")
    validation, errors = W.validate(rd, has_ligand=False,
                                    log_errors=[], log_info={})
    joined = " ".join(errors)
    check("contaminated fires DRY_TOPOLOGY_CONTAMINATED",
          "DRY_TOPOLOGY_CONTAMINATED" in joined, repr(errors))
    check("contaminated reports both counts in message",
          "5000 atoms" in joined and "304" in joined, repr(errors))
    check("contaminated is the only error", len(errors) == 1, repr(errors))


def test_validate_unknown_residue(tmp: Path):
    """A stray crystallographic residue (HEM) in comp_dry that is neither a
    standard AA nor the ligand -> UNKNOWN_RESIDUE_IN_INPUT. dry<solvated so the
    contamination gate stays silent."""
    rd = _make_run_dir(tmp, "v_stray")
    write_prmtop(rd / "comp_dry.top", natom=304,
                 res_labels=["ALA", "GLY", "HEM", "MOL"])
    write_prmtop(rd / "comp_oct.top", natom=5000, res_labels=None)
    for f in ("comp_dry.crd", "comp_oct.crd", "comp_oct.pdb"):
        (rd / f).write_text("x\n")
    # No ligand_resname passed -> MOL and HEM are both stray.
    validation, errors = W.validate(rd, has_ligand=False,
                                    log_errors=[], log_info={})
    joined = " ".join(errors)
    check("stray fires UNKNOWN_RESIDUE_IN_INPUT",
          "UNKNOWN_RESIDUE_IN_INPUT" in joined, repr(errors))
    # Hand-derived: stray = sorted({HEM, MOL}) = ['HEM', 'MOL'].
    check("stray set is sorted [HEM, MOL]",
          validation.get("nonstandard_residues") == ["HEM", "MOL"],
          repr(validation.get("nonstandard_residues")))

    # Now declare MOL as the ligand resname -> only HEM remains stray.
    validation2, errors2 = W.validate(rd, has_ligand=False,
                                      log_errors=[], log_info={},
                                      ligand_resname="mol")  # .upper() inside
    check("stray with ligand declared -> only HEM",
          validation2.get("nonstandard_residues") == ["HEM"],
          repr(validation2.get("nonstandard_residues")))
    check("stray with ligand still fires (HEM)",
          any("UNKNOWN_RESIDUE_IN_INPUT" in e for e in errors2), repr(errors2))


def test_validate_component_mismatch(tmp: Path):
    """Ligand case: protein(300)+ligand(10)=310 != dry(304) ->
    COMPONENT_ATOM_MISMATCH. dry<solvated + std residues so it is isolated."""
    rd = _make_run_dir(tmp, "v_comp")
    write_prmtop(rd / "comp_dry.top", natom=304,
                 res_labels=["ALA", "GLY", "LIG"])
    write_prmtop(rd / "comp_oct.top", natom=5000, res_labels=None)
    write_prmtop(rd / "protein.top", natom=300, res_labels=None)
    write_prmtop(rd / "ligand.top", natom=10, res_labels=None)
    for f in ("comp_dry.crd", "comp_oct.crd", "comp_oct.pdb",
              "protein.crd", "ligand.crd"):
        (rd / f).write_text("x\n")
    validation, errors = W.validate(rd, has_ligand=True,
                                    log_errors=[], log_info={},
                                    ligand_resname="LIG")
    joined = " ".join(errors)
    check("component mismatch fires COMPONENT_ATOM_MISMATCH",
          "COMPONENT_ATOM_MISMATCH" in joined, repr(errors))
    check("component mismatch reports 300 and 10 and 304",
          "300" in joined and "10" in joined and "304" in joined, repr(errors))
    check("component mismatch records protein_atoms=300",
          validation.get("protein_atoms") == 300, repr(validation))
    check("component mismatch records ligand_atoms=10",
          validation.get("ligand_atoms") == 10, repr(validation))


def test_validate_missing_outputs(tmp: Path):
    """Required outputs absent -> MISSING_OUTPUTS, and early return (no atom
    counts computed)."""
    rd = _make_run_dir(tmp, "v_missing")
    # Only one of the required files exists.
    write_prmtop(rd / "comp_dry.top", natom=304, res_labels=["ALA"])
    validation, errors = W.validate(rd, has_ligand=False,
                                    log_errors=[], log_info={})
    joined = " ".join(errors)
    check("missing outputs fires MISSING_OUTPUTS",
          "MISSING_OUTPUTS" in joined, repr(errors))
    check("missing outputs early-returns (no dry_atoms key)",
          "dry_atoms" not in validation, repr(validation))


def test_validate_log_errors_preserved(tmp: Path):
    """validate prepends incoming log_errors / log_info (clean build otherwise)."""
    rd = _make_run_dir(tmp, "v_logpass")
    write_prmtop(rd / "comp_dry.top", natom=304, res_labels=["ALA", "GLY"])
    write_prmtop(rd / "comp_oct.top", natom=5000, res_labels=None)
    for f in ("comp_dry.crd", "comp_oct.crd", "comp_oct.pdb"):
        (rd / f).write_text("x\n")
    validation, errors = W.validate(
        rd, has_ligand=False,
        log_errors=["ERROR: prior leap problem"],
        log_info={"solvent_residues_added": [1234]})
    check("log_errors carried into result",
          "ERROR: prior leap problem" in errors, repr(errors))
    check("log_info carried into validation",
          validation.get("solvent_residues_added") == [1234], repr(validation))


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_prmtop_natom(tmp)
        test_residue_labels(tmp)
        test_build_leap_in()
        test_validate_clean_noligand(tmp)
        test_validate_contaminated(tmp)
        test_validate_unknown_residue(tmp)
        test_validate_component_mismatch(tmp)
        test_validate_missing_outputs(tmp)
        test_validate_log_errors_preserved(tmp)

    total = PASS + FAIL
    print(f"\n{total} assertions: {PASS} PASS, {FAIL} FAIL")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()

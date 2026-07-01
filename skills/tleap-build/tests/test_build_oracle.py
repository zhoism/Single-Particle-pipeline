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


# ---- (5) parse_leap_log: bond-length capture (CROSS_GAP_SPURIOUS_BOND) ----

def test_parse_leap_log_bonds(tmp: Path):
    """parse_leap_log captures `bond of N angstroms` lengths, ignores the benign
    `Close contact of N angstroms` clash line, and still reads `Added N
    residues`. Hand-derived expected values from the regexes in the loop."""
    log = tmp / "leap.log"
    # Exact strings teLeap emits (ground-truthed by inducing a real gap):
    #   "There is a bond of 4.084 angstroms between C and N atoms:"
    #   "Close contact of N angstroms between nonbonded atoms ..."
    log.write_text(
        "Checking 'prot'....\n"
        "/path/bin/teLeap: Warning!\n"
        "There is a bond of 5.230 angstroms between C and N atoms:\n"
        "There is a bond of 2.050 angstroms between C and N atoms:\n"   # legit S-S
        "Close contact of 1.480 angstroms between nonbonded atoms HH11 and HD2\n"
        "Added 1894 residues.\n")
    errors, info = W.parse_leap_log(log)
    # Both "bond of" lines captured in order; the close-contact line is NOT.
    check("bond capture order+values",
          info.get("bond_lengths_angstrom") == [5.23, 2.05],
          repr(info.get("bond_lengths_angstrom")))
    check("close-contact line not captured as a bond",
          1.48 not in (info.get("bond_lengths_angstrom") or []),
          repr(info.get("bond_lengths_angstrom")))
    check("Added residues still parsed",
          info.get("solvent_residues_added") == [1894],
          repr(info.get("solvent_residues_added")))

    # No bond/clash lines at all -> key simply absent (not [] vs crash).
    clean = tmp / "leap_clean.log"
    clean.write_text("Checking 'prot'....\nAdded 2000 residues.\n")
    _, info2 = W.parse_leap_log(clean)
    check("no bonds -> key absent",
          "bond_lengths_angstrom" not in info2, repr(info2))

    # Missing log file -> the existing LEAP_NO_LOG error, no bond key.
    miss_err, miss_info = W.parse_leap_log(tmp / "no_such_leap.log")
    check("missing log -> LEAP_NO_LOG",
          any("LEAP_NO_LOG" in e for e in miss_err), repr(miss_err))


def test_parse_leap_log_real_fixture():
    """Pin CROSS_GAP to a REAL teLeap artifact, not just an assumed string —
    this is what stops the gate+test from being wrong together (the failure mode
    that made SYSTEM_NOT_NEUTRAL ship vacuous). The fixture is genuine
    `loadpdb + check` output on a PDB with an excised interior stretch (an
    un-TER'd chain gap); teLeap bonded across it and emitted the warning. It is
    vendored as .txt (not .log) only to dodge the repo's *.log run-output
    gitignore — the bond-warning line is byte-verbatim teLeap output."""
    fixture = HERE / "fixtures" / "induced_cross_gap_leap_log.txt"
    if not fixture.is_file():
        print("  SKIP test_parse_leap_log_real_fixture (fixture missing)")
        return
    _, info = W.parse_leap_log(fixture)
    bonds = info.get("bond_lengths_angstrom", [])
    # Real teLeap emitted exactly one long bond at 4.084 A across the gap.
    check("real fixture captures the 4.084 A cross-gap bond", bonds == [4.084],
          repr(bonds))
    long_bonds = [b for b in bonds if b > W.MAX_BOND_ANGSTROM]
    check("real fixture trips CROSS_GAP (4.084 > 3.0)", long_bonds == [4.084],
          repr(long_bonds))


# ---- (6) validate: SOLVENT_NOT_ADDED -------------------------------------

def _solvent_run_dir(tmp: Path, name: str, n_wat: int) -> Path:
    """A clean run dir (no-ligand, dry<solvated, std protein residues) whose
    comp_oct RESIDUE_LABEL carries `n_wat` WAT residues — isolates the water
    gate from every other check."""
    rd = _make_run_dir(tmp, name)
    write_prmtop(rd / "comp_dry.top", natom=304, res_labels=["ALA", "GLY"])
    write_prmtop(rd / "comp_oct.top", natom=5000,
                 res_labels=["ALA", "GLY"] + ["WAT"] * n_wat)
    for f in ("comp_dry.crd", "comp_oct.crd", "comp_oct.pdb"):
        (rd / f).write_text("x\n")
    return rd


def test_validate_solvent(tmp: Path):
    # 150 waters (>= SOLVENT_FLOOR 100) -> clean pass, water_residues recorded.
    rd = _solvent_run_dir(tmp, "v_solv_ok", 150)
    validation, errors = W.validate(rd, has_ligand=False,
                                    log_errors=[], log_info={})
    check("solvent ok: no SOLVENT_NOT_ADDED",
          not any("SOLVENT_NOT_ADDED" in e for e in errors), repr(errors))
    check("solvent ok: water_residues=150",
          validation.get("water_residues") == 150,
          repr(validation.get("water_residues")))
    check("solvent ok: no errors at all", errors == [], repr(errors))

    # 10 waters (< floor) -> SOLVENT_NOT_ADDED is the only error.
    rd2 = _solvent_run_dir(tmp, "v_solv_bad", 10)
    validation2, errors2 = W.validate(rd2, has_ligand=False,
                                      log_errors=[], log_info={})
    joined = " ".join(errors2)
    check("vacuum build fires SOLVENT_NOT_ADDED",
          "SOLVENT_NOT_ADDED" in joined, repr(errors2))
    check("vacuum build reports count 10",
          "only 10 water" in joined, repr(errors2))
    check("vacuum build: SOLVENT_NOT_ADDED is the only error",
          len(errors2) == 1, repr(errors2))


# ---- (7) validate: CROSS_GAP_SPURIOUS_BOND -------------------------------

def test_validate_cross_gap(tmp: Path):
    # Long bond (5.23 A > MAX_BOND_ANGSTROM 3.0) -> CROSS_GAP fires; solvent ok.
    rd = _solvent_run_dir(tmp, "v_gap", 150)
    validation, errors = W.validate(
        rd, has_ligand=False, log_errors=[],
        log_info={"bond_lengths_angstrom": [1.5, 5.23]})
    joined = " ".join(errors)
    check("long bond fires CROSS_GAP_SPURIOUS_BOND",
          "CROSS_GAP_SPURIOUS_BOND" in joined, repr(errors))
    check("CROSS_GAP reports the long length 5.23",
          "5.23" in joined, repr(errors))
    check("CROSS_GAP excludes the legit 1.5 A bond",
          "1.5" not in joined.split("5.23")[0], repr(errors))
    check("CROSS_GAP is the only error (solvent ok)",
          len(errors) == 1, repr(errors))

    # All bonds <= 3.0 A (incl. an S-S at 2.05) -> no CROSS_GAP, clean.
    rd2 = _solvent_run_dir(tmp, "v_nogap", 150)
    _, errors2 = W.validate(
        rd2, has_ligand=False, log_errors=[],
        log_info={"bond_lengths_angstrom": [1.21, 2.05, 1.53]})
    check("normal bonds -> no CROSS_GAP", errors2 == [], repr(errors2))


# ---- (8) structural CROSS_GAP: prmtop_bonds / read_amber_coords /
#          structural_long_bonds + the fail-CLOSED validate() wiring ----------
#
# The committed real-format fixtures pin the parse+distance path against AMBER's
# actual prmtop/restart layout (the "structural > log-scrape" durability lesson —
# a hand-built fixture the parser agrees with but real AMBER output disagrees with
# is exactly the SYSTEM_NOT_NEUTRAL failure). structural_cross_gap_prmtop.txt is a
# minimal NATOM=4 topology with a normal C-H bond (0-1) in BONDS_INC_HYDROGEN and a
# heavy-heavy cross-gap bond (2-3) in BONDS_WITHOUT_HYDROGEN; the .crd places 0-1 at
# 1.5 A and 2-3 at 4.0 A. Vendored as .txt (not .top/.crd) for the same reason the
# leap-log fixture is: keep it obviously a test artifact, not a real build output.

STRUCT_TOP = HERE / "fixtures" / "structural_cross_gap_prmtop.txt"
STRUCT_CRD = HERE / "fixtures" / "structural_cross_gap_crd.txt"


def test_prmtop_bonds(tmp: Path):
    # Committed fixture: (0,1) from INC_HYDROGEN then (2,3) from WITHOUT_HYDROGEN.
    # Hand-derived from the triples (3*ai, 3*aj, type) // 3.
    check("prmtop_bonds reads both blocks, in order",
          W.prmtop_bonds(STRUCT_TOP) == [(0, 1), (2, 3)],
          repr(W.prmtop_bonds(STRUCT_TOP)))
    # FAIL-CLOSED: both BONDS blocks are mandatory. NO bonds blocks -> None
    # (could-not-verify), never [] (which would read as a clean topology).
    nob = tmp / "nobonds.top"
    nob.write_text("%FLAG POINTERS\n%FORMAT(10I8)\n       4\n"
                   "%FLAG MASS\n%FORMAT(5E16.8)\n  1.0\n")
    check("prmtop_bonds no BONDS blocks -> None (fail-closed)",
          W.prmtop_bonds(nob) is None, repr(W.prmtop_bonds(nob)))
    # FAIL-CLOSED: BONDS_WITHOUT_HYDROGEN missing (where a heavy-atom cross-gap
    # bond lives) -> None even though INC_HYDROGEN parsed.
    onlyinc = tmp / "onlyinc.top"
    onlyinc.write_text(
        "%FLAG POINTERS\n%FORMAT(10I8)\n       4\n"
        "%FLAG BONDS_INC_HYDROGEN\n%FORMAT(10I8)\n       0       3       1\n"
        "%FLAG MASS\n%FORMAT(5E16.8)\n 1.0\n")
    check("prmtop_bonds missing WITHOUT_HYDROGEN -> None (fail-closed)",
          W.prmtop_bonds(onlyinc) is None, repr(W.prmtop_bonds(onlyinc)))
    # A %COMMENT line between %FLAG and %FORMAT (permitted by the AMBER format)
    # is tolerated, not silently dropped.
    withcomment = tmp / "comment.top"
    withcomment.write_text(
        "%FLAG BONDS_INC_HYDROGEN\n%COMMENT bogus\n%FORMAT(10I8)\n       0       3       1\n"
        "%FLAG BONDS_WITHOUT_HYDROGEN\n%FORMAT(10I8)\n       6       9       1\n"
        "%FLAG MASS\n%FORMAT(5E16.8)\n 1.0\n")
    check("prmtop_bonds tolerates %COMMENT between %FLAG and %FORMAT",
          W.prmtop_bonds(withcomment) == [(0, 1), (2, 3)],
          repr(W.prmtop_bonds(withcomment)))
    # Non-integer token inside a BONDS block -> ValueError -> None.
    bad = tmp / "badbonds.top"
    bad.write_text(
        "%FLAG BONDS_INC_HYDROGEN\n%FORMAT(10I8)\n       0       3       1\n"
        "%FLAG BONDS_WITHOUT_HYDROGEN\n%FORMAT(10I8)\n"
        "       0       X       1\n%FLAG MASS\n%FORMAT(5E16.8)\n 1.0\n")
    check("prmtop_bonds non-integer token -> None", W.prmtop_bonds(bad) is None,
          repr(W.prmtop_bonds(bad)))
    # Missing file -> None (OSError caught).
    check("prmtop_bonds missing file -> None",
          W.prmtop_bonds(tmp / "nope.top") is None)


def test_read_amber_coords(tmp: Path):
    # Committed fixture: 4 atoms; exact xyz hand-derived from the 6F12.7 lines.
    coords = W.read_amber_coords(STRUCT_CRD, 4)
    check("read_amber_coords exact xyz",
          coords == [(0.0, 0.0, 0.0), (1.5, 0.0, 0.0),
                     (10.0, 0.0, 0.0), (14.0, 0.0, 0.0)], repr(coords))
    # crd header NATOM disagrees with the requested (prmtop) NATOM -> None: a
    # stale/mismatched coordinate file must not be read as the wrong geometry.
    check("read_amber_coords header NATOM mismatch -> None",
          W.read_amber_coords(STRUCT_CRD, 5) is None,
          repr(W.read_amber_coords(STRUCT_CRD, 5)))
    # Header matches but the body is truncated (< 3*natom values) -> None.
    trunc = tmp / "trunc.crd"
    trunc.write_text("t\n       4\n"
                     f"{0.0:12.7f}{0.0:12.7f}{0.0:12.7f}\n")  # 3 of 12 needed
    check("read_amber_coords truncated body -> None",
          W.read_amber_coords(trunc, 4) is None,
          repr(W.read_amber_coords(trunc, 4)))
    # < 2 lines (no coordinate body) -> None.
    short = tmp / "short.crd"
    short.write_text("title only\n")
    check("read_amber_coords no body -> None",
          W.read_amber_coords(short, 1) is None)
    # Non-numeric coordinate field -> None.
    badc = tmp / "bad.crd"
    badc.write_text("t\n   1\n   not_a_num\n")
    check("read_amber_coords non-numeric -> None",
          W.read_amber_coords(badc, 1) is None)
    # Missing file -> None.
    check("read_amber_coords missing file -> None",
          W.read_amber_coords(tmp / "nope.crd", 1) is None)


def test_structural_long_bonds(tmp: Path):
    # Committed cross-gap fixture: bond (2,3)=4.0 A > 3.0; (0,1)=1.5 A. Only the
    # 4.0 A bond is returned (the normal bond does not false-fire).
    got = W.structural_long_bonds(STRUCT_TOP, STRUCT_CRD)
    check("structural finds the 4.0 A cross-gap bond only", got == [4.0],
          repr(got))
    # Clean geometry (both bonds < 3 A) -> [] : proves normal bonds never trip it.
    clean_top = tmp / "clean.top"
    clean_top.write_text(
        "%FLAG POINTERS\n%FORMAT(10I8)\n       4\n"
        "%FLAG BONDS_INC_HYDROGEN\n%FORMAT(10I8)\n       0       3       1\n"
        "%FLAG BONDS_WITHOUT_HYDROGEN\n%FORMAT(10I8)\n       6       9       1\n"
        "%FLAG MASS\n%FORMAT(5E16.8)\n 1.0\n")
    clean_crd = tmp / "clean.crd"
    clean_crd.write_text(
        "t\n       4\n"
        f"{0.0:12.7f}{0.0:12.7f}{0.0:12.7f}{1.5:12.7f}{0.0:12.7f}{0.0:12.7f}\n"
        f"{5.0:12.7f}{0.0:12.7f}{0.0:12.7f}{6.5:12.7f}{0.0:12.7f}{0.0:12.7f}\n")
    check("structural clean geometry -> [] (no false-fire)",
          W.structural_long_bonds(clean_top, clean_crd) == [],
          repr(W.structural_long_bonds(clean_top, clean_crd)))
    # NATOM > coords available -> coords parse returns None -> None (could-not-verify).
    mism_crd = tmp / "mism.crd"
    mism_crd.write_text(f"t\n       2\n{0.0:12.7f}{0.0:12.7f}{0.0:12.7f}"
                        f"{1.5:12.7f}{0.0:12.7f}{0.0:12.7f}\n")
    check("structural natom>coords -> None",
          W.structural_long_bonds(STRUCT_TOP, mism_crd) is None,
          repr(W.structural_long_bonds(STRUCT_TOP, mism_crd)))
    # Bond index beyond NATOM (internally inconsistent prmtop) -> None.
    oob_top = tmp / "oob.top"
    oob_top.write_text(
        "%FLAG POINTERS\n%FORMAT(10I8)\n       2\n"
        "%FLAG BONDS_WITHOUT_HYDROGEN\n%FORMAT(10I8)\n"
        "       0       9       1\n"  # references atom 3 (=9//3) but NATOM=2
        "%FLAG MASS\n%FORMAT(5E16.8)\n 1.0\n")
    oob_crd = tmp / "oob.crd"
    oob_crd.write_text(f"t\n       2\n{0.0:12.7f}{0.0:12.7f}{0.0:12.7f}"
                       f"{1.0:12.7f}{0.0:12.7f}{0.0:12.7f}\n")
    check("structural bond index out of range -> None",
          W.structural_long_bonds(oob_top, oob_crd) is None,
          repr(W.structural_long_bonds(oob_top, oob_crd)))
    # Missing top -> None.
    check("structural missing top -> None",
          W.structural_long_bonds(tmp / "nope.top", STRUCT_CRD) is None)


def test_validate_cross_gap_structural(tmp: Path):
    """Fail-CLOSED: the structural detector fires CROSS_GAP from the built
    comp_dry topology+coords even with NO log `bond of` line (teLeap reworded or
    dropped it). Uses the committed real-format cross-gap fixture as comp_dry."""
    rd = _make_run_dir(tmp, "v_gap_struct")
    (rd / "comp_dry.top").write_text(STRUCT_TOP.read_text())  # NATOM=4, ALA, 4.0 A gap
    (rd / "comp_dry.crd").write_text(STRUCT_CRD.read_text())
    write_prmtop(rd / "comp_oct.top", natom=5000, res_labels=None)
    for f in ("comp_oct.crd", "comp_oct.pdb"):
        (rd / f).write_text("x\n")
    # No bond_lengths_angstrom in log_info -> ONLY the structural signal can fire.
    validation, errors = W.validate(rd, has_ligand=False,
                                    log_errors=[], log_info={})
    joined = " ".join(errors)
    check("structural-only fires CROSS_GAP (no log signal present)",
          "CROSS_GAP_SPURIOUS_BOND" in joined, repr(errors))
    check("structural CROSS_GAP tagged [detected via structural]",
          "detected via structural]" in joined, repr(errors))
    check("structural CROSS_GAP reports the 4.0 A length",
          "4.0" in joined, repr(errors))
    check("structural_long_bonds recorded in validation",
          validation.get("structural_long_bonds") == [4.0],
          repr(validation.get("structural_long_bonds")))
    check("structural CROSS_GAP is the only error", len(errors) == 1,
          repr(errors))

    # Both signals present -> union of lengths, tagged structural+log.
    _, errors2 = W.validate(rd, has_ligand=False, log_errors=[],
                            log_info={"bond_lengths_angstrom": [5.23]})
    joined2 = " ".join(errors2)
    check("both signals -> [detected via structural+log]",
          "detected via structural+log]" in joined2, repr(errors2))
    # Structural is authoritative geometry: the reported length is the structural
    # 4.0, not the injected log 5.23 (a set-union would have listed both).
    check("both fire; reports authoritative structural 4.0, not the log 5.23",
          "4.0" in joined2 and "5.23" not in joined2, repr(errors2))


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
        test_parse_leap_log_bonds(tmp)
        test_parse_leap_log_real_fixture()
        test_validate_solvent(tmp)
        test_validate_cross_gap(tmp)
        test_prmtop_bonds(tmp)
        test_read_amber_coords(tmp)
        test_structural_long_bonds(tmp)
        test_validate_cross_gap_structural(tmp)

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

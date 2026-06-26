#!/usr/bin/env python3
"""Deterministic oracle tests for antechamber-ligandprep's pure engine functions.

No antechamber/parmchk2/obabel binaries are touched. This exercises the
deterministic core that gates ligand prep correctness:
  - classify_input()      : input routing (pdb/mol2/sdf/smiles/unknown)
  - pdb_has_hydrogens()   : H-presence detection (drives the kekulize-safe route)
  - kekulize_failed()     : obabel kekulization-failure scan
  - parse_mol2_atoms()    : @<TRIPOS>ATOM block parser (count/type/charge)
  - validate()            : the error-code gate (MISSING_PARAMETERS,
                            NET_CHARGE_MISMATCH, INPUT_PREP_FAILED)

Every expected value is HAND-DERIVED by reading wrapper.py, never produced by
re-calling the function under test. Run with the prime-amber conda python:
  MPLBACKEND=Agg .../prime-amber/bin/python test_engine.py   (exit 0 = all pass).

One assertion (empty_frcmod_silent_pass) is EXPECTED RED: it documents a real
silent-pass bug where an existing-but-empty frcmod produces no error because the
gate only looks for ATTN lines. Do NOT fix the wrapper to make it green.
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


# A session-scoped tmp dir holds every fixture file. Cleaned at process exit.
_TMP = tempfile.TemporaryDirectory(prefix="antechamber_ligandprep_oracle_")
TMP = Path(_TMP.name)


def _write(rel: str, text: str) -> Path:
    p = TMP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---------------------------------------------------------------------------
# classify_input(value) -> (mode, Path|None)
#   Existing file + known ext (.pdb/.mol2/.sdf) -> (ext_no_dot, path.resolve())
#   Existing file + unknown ext                 -> ("unknown", path.resolve())
#   Non-existent path                           -> ("smiles", None)
# ---------------------------------------------------------------------------

def test_classify_input():
    pdb = _write("lig.pdb", "REMARK fixture\nEND\n")
    mol2 = _write("lig.mol2", "@<TRIPOS>MOLECULE\nLIG\n")
    sdf = _write("lig.sdf", "LIG\n")
    txt = _write("lig.xyz", "garbage\n")

    m, p = W.classify_input(str(pdb))
    check("classify pdb mode", m == "pdb", repr(m))
    check("classify pdb path", p == pdb.resolve(), f"{p} vs {pdb.resolve()}")

    m, p = W.classify_input(str(mol2))
    check("classify mol2 mode", m == "mol2", repr(m))
    check("classify mol2 path", p == mol2.resolve(), f"{p} vs {mol2.resolve()}")

    m, p = W.classify_input(str(sdf))
    check("classify sdf mode", m == "sdf", repr(m))
    check("classify sdf path", p == sdf.resolve(), f"{p} vs {sdf.resolve()}")

    # Unknown extension on a real file -> "unknown", path resolved.
    m, p = W.classify_input(str(txt))
    check("classify unknown mode", m == "unknown", repr(m))
    check("classify unknown path", p == txt.resolve(), f"{p} vs {txt.resolve()}")

    # A non-existent path -> treated as SMILES, no path.
    m, p = W.classify_input("CCCc1ccccc1O")
    check("classify smiles mode", m == "smiles", repr(m))
    check("classify smiles path is None", p is None, repr(p))


# ---------------------------------------------------------------------------
# pdb_has_hydrogens(path) -> bool
#   element col [76:78] populated & == "H" -> True
#   element col populated & != "H"          -> that record is skipped
#   element col empty -> name heuristic: [12:16] stripped, strip leading
#       digits, first char upper == "H" -> True
# ---------------------------------------------------------------------------

def test_pdb_has_hydrogens():
    # PDB WITH hydrogens, declared in the element column (cols 77-78, 1-based).
    # Build columns exactly: cols 13-16 atom name, 18-20 resName, ... 77-78 elem.
    # "ATOM  " + serial(5) + " " + name(4) + altLoc(1) + resName(3) + ...
    with_h = (
        "ATOM      1  N   LIG     1       1.000   2.000   3.000  1.00  0.00           N  \n"
        "ATOM      2  H1  LIG     1       1.500   2.500   3.500  1.00  0.00           H  \n"
        "END\n"
    )
    ph = _write("with_h.pdb", with_h)
    check("pdb_has_hydrogens with H -> True", W.pdb_has_hydrogens(ph) is True, "")

    # PDB WITHOUT hydrogens: only heavy atoms, element column populated non-H.
    no_h = (
        "ATOM      1  N   LIG     1       1.000   2.000   3.000  1.00  0.00           N  \n"
        "ATOM      2  C   LIG     1       1.500   2.500   3.500  1.00  0.00           C  \n"
        "ATOM      3  O   LIG     1       2.000   3.000   4.000  1.00  0.00           O  \n"
        "END\n"
    )
    pn = _write("no_h.pdb", no_h)
    check("pdb_has_hydrogens no H -> False", W.pdb_has_hydrogens(pn) is False, "")

    # No element column (short hand-edited lines): name-heuristic must catch H.
    # Line is < 78 chars so element == ""; name field [12:16] -> "HA  " -> "HA"
    # -> lstrip digits -> "HA" -> first char "H" -> True.
    name_heur = (
        "ATOM      1  N   LIG     1       1.000   2.000   3.000\n"
        "ATOM      2  HA  LIG     1       1.500   2.500   3.500\n"
    )
    pnh = _write("name_heur.pdb", name_heur)
    check("pdb_has_hydrogens name-heuristic -> True",
          W.pdb_has_hydrogens(pnh) is True, "")


# ---------------------------------------------------------------------------
# kekulize_failed(stderr_path) -> bool
#   Reads file; True iff "Failed to kekulize aromatic bonds" substring present.
# ---------------------------------------------------------------------------

def test_kekulize_failed():
    dirty = _write("dirty.err",
                   "==============================\n"
                   "*** Open Babel Warning  in PerceiveBondOrders\n"
                   "  Failed to kekulize aromatic bonds in OBMol::PerceiveBondOrders\n"
                   "1 molecule converted\n")
    check("kekulize_failed dirty -> True", W.kekulize_failed(dirty) is True, "")

    clean = _write("clean.err", "1 molecule converted\n")
    check("kekulize_failed clean -> False", W.kekulize_failed(clean) is False, "")

    # A nonexistent file -> OSError swallowed -> False.
    check("kekulize_failed missing file -> False",
          W.kekulize_failed(TMP / "does_not_exist.err") is False, "")


# ---------------------------------------------------------------------------
# parse_mol2_atoms(mol2) -> list[dict]
#   Finds ^@<TRIPOS>ATOM$ block, stops at next ^@<TRIPOS>\w+$.
#   Each line: split() needs >= 9 parts; record:
#     id=parts[0], name=parts[1], atom_type=parts[5], charge=float(parts[8]).
# A standard TRIPOS atom line is:
#   id name x y z atom_type subst_id subst_name charge
#   idx: 0   1  2 3 4   5         6       7        8
# ---------------------------------------------------------------------------

def _mol2(atom_lines: str, with_bond_block: bool = True) -> str:
    body = (
        "@<TRIPOS>MOLECULE\n"
        "LIG\n"
        "@<TRIPOS>ATOM\n"
        f"{atom_lines}"
    )
    if with_bond_block:
        body += "@<TRIPOS>BOND\n     1    1    2 1\n"
    return body


def test_parse_mol2_atoms():
    atoms_block = (
        "      1 C1          0.0000    0.0000    0.0000 c3        1 LIG       -0.0813\n"
        "      2 O1          1.4000    0.0000    0.0000 oh        1 LIG        0.0813\n"
    )
    mp = _write("good.mol2", _mol2(atoms_block))
    atoms = W.parse_mol2_atoms(mp)
    check("parse atom count = 2", len(atoms) == 2, str(len(atoms)))
    check("parse atom0 id", atoms[0]["id"] == "1", str(atoms[0]))
    check("parse atom0 name", atoms[0]["name"] == "C1", str(atoms[0]))
    check("parse atom0 atom_type c3", atoms[0]["atom_type"] == "c3", str(atoms[0]))
    check("parse atom0 charge",
          abs(atoms[0]["charge"] - (-0.0813)) < 1e-9, str(atoms[0]))
    check("parse atom1 atom_type oh", atoms[1]["atom_type"] == "oh", str(atoms[1]))
    check("parse atom1 charge",
          abs(atoms[1]["charge"] - 0.0813) < 1e-9, str(atoms[1]))

    # The parser stops at the next @<TRIPOS> block: BOND line is NOT an atom.
    check("parse stops at BOND block", len(atoms) == 2, "leaked into BOND block")

    # A line with < 9 whitespace parts is skipped (not an atom record).
    short = (
        "      1 C1          0.0000    0.0000    0.0000 c3        1 LIG       -0.0813\n"
        "      2 TOOSHORT\n"
    )
    sp = _write("short.mol2", _mol2(short))
    check("parse skips short line", len(W.parse_mol2_atoms(sp)) == 1,
          str(len(W.parse_mol2_atoms(sp))))

    # No @<TRIPOS>ATOM block at all -> [].
    none = _write("noatomblock.mol2",
                  "@<TRIPOS>MOLECULE\nLIG\n@<TRIPOS>BOND\n     1    1    2 1\n")
    check("parse no ATOM block -> []", W.parse_mol2_atoms(none) == [], "")


# ---------------------------------------------------------------------------
# validate(mol2_path, frcmod_path, requested_charge) -> (validation, errors)
# Real error-code substrings emitted by the body:
#   "MISSING_PARAMETERS"  (missing mol2 / 'du' type / all-zero charges /
#                          missing frcmod / ATTN lines)
#   "NET_CHARGE_MISMATCH" (|sum - requested| > 5e-3, and NOT all-zero)
#   "INPUT_PREP_FAILED"   (atom block empty)
# Notes from the body:
#   - 'du' check is independent (always appended if present).
#   - all-zero-charges and NET_CHARGE_MISMATCH are an elif pair (zero wins).
#   - empty-atoms (INPUT_PREP_FAILED) and the charge branch are an if/else.
#   - frcmod: error only when ATTN lines exist OR the file is missing.
# ---------------------------------------------------------------------------

# A frcmod with real parameter content and NO "ATTN" anywhere -> no frcmod error.
FRCMOD_REAL = (
    "remark goes here\n"
    "MASS\n"
    "c3 12.010\n"
    "oh 16.000\n"
    "\n"
    "BOND\n"
    "c3-oh  320.0   1.410\n"
    "\n"
    "ANGLE\n"
    "c3-c3-oh  50.0   109.50\n"
    "\n"
    "DIHE\n"
    "\n"
    "IMPROPER\n"
    "\n"
    "NONBON\n"
    "  c3   1.9080  0.1094\n"
)

# A frcmod parmchk2 emits when it had to guess parameters: ATTN markers present.
FRCMOD_ATTN = (
    "remark goes here\n"
    "MASS\n"
    "BOND\n"
    "c3-xx  320.0   1.410       ATTN, need revision\n"
    "ANGLE\n"
    "DIHE\n"
    "IMPROPER\n"
    "NONBON\n"
)


def _frcmod(text: str, rel: str) -> Path:
    return _write(rel, text)


def _mol2_with_charges(rel: str, rows: list[tuple[str, str, str, float]]) -> Path:
    """rows = [(id, name, atom_type, charge), ...] -> a valid TRIPOS atom block."""
    lines = ""
    for i, (aid, name, atype, q) in enumerate(rows):
        x = f"{float(i):.4f}"
        lines += (f"{aid:>7} {name:<8} {x:>10}    0.0000    0.0000 "
                  f"{atype:<8} 1 LIG {q:>12.4f}\n")
    return _write(rel, _mol2(lines))


def test_validate_case_a_clean():
    # Sane nonzero charges summing to requested charge 0 (-0.4 + 0.4 = 0.0).
    m = _mol2_with_charges("case_a.mol2",
                           [("1", "C1", "c3", -0.4000),
                            ("2", "O1", "oh", 0.4000)])
    f = _frcmod(FRCMOD_REAL, "case_a.frcmod")
    validation, errors = W.validate(m, f, 0)
    check("(a) clean errors empty", errors == [], str(errors))
    check("(a) clean atom_count 2", validation["atom_count"] == 2,
          str(validation.get("atom_count")))
    check("(a) clean charge_sum ~0",
          abs(validation["charge_sum"]) < 1e-6, str(validation.get("charge_sum")))
    check("(a) clean no frcmod_missing",
          validation["frcmod_missing"] == [], str(validation.get("frcmod_missing")))


def test_validate_case_b_all_zero():
    # All charges exactly 0 -> all(abs(c) < 1e-9) True -> MISSING_PARAMETERS.
    m = _mol2_with_charges("case_b.mol2",
                           [("1", "C1", "c3", 0.0000),
                            ("2", "O1", "oh", 0.0000)])
    f = _frcmod(FRCMOD_REAL, "case_b.frcmod")
    validation, errors = W.validate(m, f, 0)
    check("(b) all-zero -> MISSING_PARAMETERS",
          any("MISSING_PARAMETERS" in e and "all zeros" in e for e in errors),
          str(errors))
    # The all-zero branch is taken, so NET_CHARGE_MISMATCH must NOT appear
    # (sum==0==requested anyway, and it's an elif).
    check("(b) no NET_CHARGE_MISMATCH",
          not any("NET_CHARGE_MISMATCH" in e for e in errors), str(errors))


def test_validate_case_c_charge_mismatch():
    # Nonzero charges that sum to +1.0 while requested is 0 -> > 5e-3 off.
    # Not all-zero, so the elif fires NET_CHARGE_MISMATCH (not the zero branch).
    m = _mol2_with_charges("case_c.mol2",
                           [("1", "C1", "c3", 0.6000),
                            ("2", "O1", "oh", 0.4000)])
    f = _frcmod(FRCMOD_REAL, "case_c.frcmod")
    validation, errors = W.validate(m, f, 0)
    check("(c) net charge mismatch -> NET_CHARGE_MISMATCH",
          any("NET_CHARGE_MISMATCH" in e for e in errors), str(errors))
    check("(c) no all-zeros error",
          not any("all zeros" in e for e in errors), str(errors))
    check("(c) charge_sum reported ~1.0",
          abs(validation["charge_sum"] - 1.0) < 1e-6,
          str(validation.get("charge_sum")))


def test_validate_case_d_du_type():
    # An atom typed 'du' (untyped) -> MISSING_PARAMETERS (independent check).
    # Charges sane summing to requested 0 so ONLY the du error fires.
    m = _mol2_with_charges("case_d.mol2",
                           [("1", "C1", "c3", -0.4000),
                            ("2", "X1", "du", 0.4000)])
    f = _frcmod(FRCMOD_REAL, "case_d.frcmod")
    validation, errors = W.validate(m, f, 0)
    check("(d) du type -> MISSING_PARAMETERS",
          any("MISSING_PARAMETERS" in e and "du" in e for e in errors),
          str(errors))
    check("(d) du is the only error",
          len(errors) == 1, str(errors))


def test_validate_case_e_empty_block():
    # @<TRIPOS>ATOM block present but contains NO atom records (all < 9 parts /
    # absent) -> parse returns [] -> INPUT_PREP_FAILED.
    empty = _write("case_e.mol2",
                   "@<TRIPOS>MOLECULE\nLIG\n@<TRIPOS>ATOM\n@<TRIPOS>BOND\n"
                   "     1    1    2 1\n")
    f = _frcmod(FRCMOD_REAL, "case_e.frcmod")
    validation, errors = W.validate(empty, f, 0)
    check("(e) empty atom block -> INPUT_PREP_FAILED",
          any("INPUT_PREP_FAILED" in e for e in errors), str(errors))
    check("(e) atom_count 0", validation["atom_count"] == 0,
          str(validation.get("atom_count")))
    # With empty atoms the charge branch (else) does not run -> no charge error.
    check("(e) no charge errors",
          not any("all zeros" in e or "NET_CHARGE_MISMATCH" in e for e in errors),
          str(errors))


def test_validate_case_f_attn_frcmod():
    # Valid mol2 (no atom/charge error) + frcmod WITH ATTN lines ->
    # the only error is the ATTN MISSING_PARAMETERS.
    m = _mol2_with_charges("case_f.mol2",
                           [("1", "C1", "c3", -0.4000),
                            ("2", "O1", "oh", 0.4000)])
    f = _frcmod(FRCMOD_ATTN, "case_f.frcmod")
    validation, errors = W.validate(m, f, 0)
    check("(f) ATTN frcmod -> MISSING_PARAMETERS",
          any("MISSING_PARAMETERS" in e and "ATTN" in e for e in errors),
          str(errors))
    check("(f) frcmod_missing captured 1 ATTN line",
          len(validation["frcmod_missing"]) == 1,
          str(validation.get("frcmod_missing")))
    check("(f) ATTN is the only error", len(errors) == 1, str(errors))


def test_validate_case_g_empty_frcmod_BUG():
    """REGRESSION (audit fix #6): an existing-but-empty / whitespace-only frcmod is
    an unusable parameter file and must be flagged. Previously validate() only
    flagged frcmod via ATTN lines, so a blank frcmod was a silent pass; the fix
    adds an explicit empty-frcmod check."""
    m = _mol2_with_charges("case_g.mol2",
                           [("1", "C1", "c3", -0.4000),
                            ("2", "O1", "oh", 0.4000)])
    f = _write("case_g.frcmod", "   \n\n\t\n")  # exists, whitespace-only
    validation, errors = W.validate(m, f, 0)
    check("(g) empty frcmod -> MISSING_PARAMETERS",
          any("MISSING_PARAMETERS" in e or "INPUT_PREP_FAILED" in e
              for e in errors),
          f"empty frcmod not flagged; errors={errors}")


def test_validate_missing_mol2():
    # Sanity: a missing mol2 short-circuits to MISSING_PARAMETERS and returns.
    missing = TMP / "nope.mol2"
    f = _frcmod(FRCMOD_REAL, "missing_pair.frcmod")
    validation, errors = W.validate(missing, f, 0)
    check("missing mol2 -> MISSING_PARAMETERS",
          any("MISSING_PARAMETERS" in e and "did not emit mol2" in e
              for e in errors), str(errors))
    check("missing mol2 short-circuits (single error)",
          len(errors) == 1, str(errors))


def test_validate_missing_frcmod():
    # Valid mol2 but frcmod path does not exist -> MISSING_PARAMETERS frcmod.
    m = _mol2_with_charges("ok_for_missing_frcmod.mol2",
                           [("1", "C1", "c3", -0.4000),
                            ("2", "O1", "oh", 0.4000)])
    missing_f = TMP / "nonexistent.frcmod"
    validation, errors = W.validate(m, missing_f, 0)
    check("missing frcmod -> MISSING_PARAMETERS",
          any("MISSING_PARAMETERS" in e and "did not emit frcmod" in e
              for e in errors), str(errors))


def main() -> int:
    for fn in (test_classify_input,
               test_pdb_has_hydrogens,
               test_kekulize_failed,
               test_parse_mol2_atoms,
               test_validate_case_a_clean,
               test_validate_case_b_all_zero,
               test_validate_case_c_charge_mismatch,
               test_validate_case_d_du_type,
               test_validate_case_e_empty_block,
               test_validate_case_f_attn_frcmod,
               test_validate_case_g_empty_frcmod_BUG,
               test_validate_missing_mol2,
               test_validate_missing_frcmod):
        fn()
    print(f"\nantechamber-ligandprep engine tests: {PASS} passed, {FAIL} failed")
    if FAIL:
        print("FAILURES:")
        for f in FAILURES:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

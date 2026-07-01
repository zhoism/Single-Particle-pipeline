#!/usr/bin/env python3
"""Deterministic oracle unit tests for cpptraj-analysis's PURE engine functions.

No cpptraj / MMPBSA / trajectory needed — these exercise only the in-process
parsers and the topology-driven mask detector. Every expected value below is
HAND-DERIVED by reading the function body in
  ../scripts/wrapper.py
NOT by re-calling the function under test. Fixtures are written to a tmp dir.

Functions under test:
  load_xy(path)
  _parse_evals(evecs)
  _parse_mmgbsa(dat)
  prmtop_residue_labels(top)
  detect_masks(top, has_lig)
  prmtop_radius_set(top)      # GB-radii <-> igb detector
  gb_radii_check(radii, igb)  # GB-radii <-> igb detector (non-fatal finding)

Run: MPLBACKEND=Agg <conda-python> test_engine.py   (exit 0 = all pass).
"""
import sys
import tempfile
from pathlib import Path

import numpy as np

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


# A tmp dir for all on-disk fixtures; cleaned at process exit.
_TMP = tempfile.TemporaryDirectory(prefix="cpptraj_engine_test_")
TMP = Path(_TMP.name)


def _write(name: str, text: str) -> Path:
    p = TMP / name
    p.write_text(text)
    return p


# ---- load_xy -------------------------------------------------------------
# Body (lines 130-148): returns None if the path is missing OR size==0.
# Otherwise iterates splitlines(); strips each; skips blank or '#'-prefixed
# lines; needs >=2 whitespace tokens; float()s tokens[0] and tokens[1];
# ValueError -> skip that row. If no rows survive -> None. Else returns
# (col0, col1) as two np arrays.

def test_load_xy_empty_file():
    p = _write("empty.dat", "")          # size 0 -> early None (line 132)
    check("load_xy empty -> None", W.load_xy(p) is None, repr(W.load_xy(p)))


def test_load_xy_missing_file():
    p = TMP / "does_not_exist.dat"
    check("load_xy missing -> None", W.load_xy(p) is None, repr(W.load_xy(p)))


def test_load_xy_comment_blank_only():
    # Non-zero size (passes the size gate) but every line is comment/blank ->
    # rows stays [] -> None (line 145-146).
    p = _write("comments.dat",
               "# frame value\n"
               "\n"
               "   \n"
               "#Atom CA\n")
    check("load_xy comment/blank only -> None",
          W.load_xy(p) is None, repr(W.load_xy(p)))


def test_load_xy_valid_two_col():
    # 3 data rows; a leading '#' header (skipped); a blank line (skipped);
    # one short row "5" (len(parts)<2 -> skipped); one non-numeric row
    # "x y" (float() ValueError -> skipped). Surviving rows in order:
    #   (1.0, 0.50), (2.0, 0.75), (3.0, 1.25)
    p = _write("rmsd_bb.dat",
               "#Frame   RMSD\n"
               "1   0.50\n"
               "\n"
               "5\n"
               "x y\n"
               "2   0.75\n"
               "3   1.25\n")
    res = W.load_xy(p)
    check("load_xy valid not None", res is not None, repr(res))
    if res is not None:
        x, y = res
        check("load_xy x length 3", len(x) == 3, str(len(x)))
        check("load_xy y length 3", len(y) == 3, str(len(y)))
        check("load_xy x values",
              np.array_equal(x, np.array([1.0, 2.0, 3.0])), str(x))
        check("load_xy y values",
              np.allclose(y, np.array([0.50, 0.75, 1.25])), str(y))
        check("load_xy returns np arrays",
              isinstance(x, np.ndarray) and isinstance(y, np.ndarray), "")


def test_load_xy_extra_columns_uses_first_two():
    # >=2 tokens passes; only tokens[0],tokens[1] are read; extra cols ignored.
    p = _write("multicol.dat",
               "10  2.0  99.9  -3\n"
               "20  4.0  88.8  -7\n")
    res = W.load_xy(p)
    check("load_xy multicol not None", res is not None, repr(res))
    if res is not None:
        x, y = res
        check("load_xy multicol x", np.array_equal(x, np.array([10.0, 20.0])),
              str(x))
        check("load_xy multicol y (2nd col only)",
              np.array_equal(y, np.array([2.0, 4.0])), str(y))


# ---- _parse_evals --------------------------------------------------------
# Body (lines 393-407): per line, strip; skip if blank, startswith '****', or
# startswith one of ('Eigenvector','COVAR','%'). For the rest: split; require
# len(p)==2 AND p[0].isdigit(); append float(p[1]) (ValueError -> skip).
# Returns np.array(evals) or np.array([1.0]) when none were found.
# This mirrors the REAL cpptraj evecs.dat layout: a header line, then repeating
# blocks of ' ****' followed by "<idx>  <eigenvalue>" then 2-D eigenvector
# component rows (which have >2 tokens -> skipped).

def test_parse_evals_minimal():
    evecs = _write(
        "evecs.dat",
        " Eigenvector file: COVAR nmodes 3 width 11\n"  # 'Eigenvector' -> skip
        " ****\n"                                        # '****'       -> skip
        "    1     0.81657\n"                            # parsed -> 0.81657
        "   16.27162   24.79315   14.31782\n"            # 3 tokens     -> skip
        " ****\n"                                        # skip
        "    2     0.31817\n"                            # parsed -> 0.31817
        "   22.67998   19.62813   18.58803\n"            # skip
        " ****\n"                                        # skip
        "    3     0.21511\n"                            # parsed -> 0.21511
        "   16.54443   22.52205   21.95904\n")           # skip
    evals = W._parse_evals(evecs)
    check("parse_evals length 3", len(evals) == 3, str(evals))
    check("parse_evals values",
          np.allclose(evals, np.array([0.81657, 0.31817, 0.21511])),
          str(evals))
    check("parse_evals is ndarray", isinstance(evals, np.ndarray), "")


def test_parse_evals_skips_covar_and_percent():
    # Lines beginning 'COVAR' and '%' are skipped; a 2-token line whose first
    # token is NOT a digit ("PC1 0.5") fails the isdigit() guard -> skipped.
    evecs = _write(
        "evecs2.dat",
        "COVAR matrix dummy header\n"   # skip (startswith COVAR)
        "%FLAG something\n"             # skip (startswith %)
        "PC1 0.5\n"                     # p[0] not digit -> skip
        "    7     0.07495\n")          # the only parsed value
    evals = W._parse_evals(evecs)
    check("parse_evals filtered length 1", len(evals) == 1, str(evals))
    check("parse_evals filtered value",
          np.allclose(evals, np.array([0.07495])), str(evals))


def test_parse_evals_none_found_fallback():
    # No 2-token digit-led line at all -> the body returns np.array([1.0]).
    evecs = _write(
        "evecs_empty.dat",
        " Eigenvector file: COVAR nmodes 0\n"
        " ****\n"
        "\n")
    evals = W._parse_evals(evecs)
    check("parse_evals fallback length 1", len(evals) == 1, str(evals))
    check("parse_evals fallback == [1.0]",
          np.array_equal(evals, np.array([1.0])), str(evals))


# ---- _parse_mmgbsa -------------------------------------------------------
# Body (lines 546-554): None if file missing; else regex
#   r"DELTA TOTAL\s+(-?\d+\.\d+)"  -> float(group(1)); no match -> None.
# Matches the real MMPBSA.py differences-block line:
#   "DELTA TOTAL                -18.6336    1.7721    0.1772"

def test_parse_mmgbsa_missing():
    p = TMP / "no_mmgbsa.dat"
    check("parse_mmgbsa missing -> None", W._parse_mmgbsa(p) is None,
          repr(W._parse_mmgbsa(p)))


def test_parse_mmgbsa_valid_negative():
    p = _write(
        "mmgbsa.dat",
        "MM-GBSA differences\n"
        "Differences (Complex - Receptor - Ligand):\n"
        "Energy Component     Average     Std. Dev.   Std. Err. of Mean\n"
        "-----------------------------------------------------------\n"
        "DELTA TOTAL                -18.6336    1.7721    0.1772\n")
    dG = W._parse_mmgbsa(p)
    check("parse_mmgbsa negative value",
          dG is not None and abs(dG - (-18.6336)) < 1e-9, str(dG))
    check("parse_mmgbsa returns float", isinstance(dG, float), str(type(dG)))


def test_parse_mmgbsa_first_match_wins():
    # re.search returns the FIRST match; only the first DELTA TOTAL is read.
    p = _write(
        "mmgbsa2.dat",
        "DELTA TOTAL    -5.5  0.1\n"
        "DELTA TOTAL    -9.9  0.2\n")
    dG = W._parse_mmgbsa(p)
    check("parse_mmgbsa first match",
          dG is not None and abs(dG - (-5.5)) < 1e-9, str(dG))


def test_parse_mmgbsa_garbage_no_match():
    # No "DELTA TOTAL <float>" anywhere -> None. (Integer-only would also fail
    # the \d+\.\d+ fractional requirement, but here there is no such line.)
    p = _write(
        "mmgbsa_garbage.dat",
        "this is not an mmpbsa output file\n"
        "Etot = -1234\n"
        "TOTAL ENERGY 42\n")
    check("parse_mmgbsa garbage -> None", W._parse_mmgbsa(p) is None,
          repr(W._parse_mmgbsa(p)))


def test_parse_mmgbsa_requires_decimal():
    # "DELTA TOTAL  12" has no decimal point -> the (-?\d+\.\d+) group fails to
    # match -> None. Documents the exact regex shape.
    p = _write("mmgbsa_int.dat", "DELTA TOTAL    12   0   0\n")
    check("parse_mmgbsa int-only -> None", W._parse_mmgbsa(p) is None,
          repr(W._parse_mmgbsa(p)))


# ---- prmtop_residue_labels -----------------------------------------------
# Body (lines 82-91): read text; regex
#   r"%FLAG RESIDUE_LABEL\s*\n%FORMAT\([^)]*\)\s*\n"
# must match (the %FORMAT line MUST directly follow). Take the text AFTER that
# match up to the next '%FLAG' (or EOF) and return block.split(). Whitespace
# split, so the fixed-width 20a4 packing collapses to bare resnames.

def _prmtop(residue_block: str) -> str:
    # A minimal synthetic prmtop: a preceding flag, the RESIDUE_LABEL block,
    # then a trailing flag so the 'next %FLAG' boundary is exercised.
    return (
        "%VERSION  VERSION_STAMP = V0001.000\n"
        "%FLAG TITLE\n"
        "%FORMAT(20a4)\n"
        "default_name\n"
        "%FLAG RESIDUE_LABEL\n"
        "%FORMAT(20a4)\n"
        f"{residue_block}"
        "%FLAG RESIDUE_POINTER\n"
        "%FORMAT(10I8)\n"
        "       1       6      12\n")


def test_prmtop_residue_labels_basic():
    # 4 protein residues + a ligand 'MOL'. 20a4 means 4-char fields; whitespace
    # split yields exactly the trimmed tokens, in order.
    top = _write("complex.prmtop",
                 _prmtop("ALA GLY SER VAL MOL \n"))
    labels = W.prmtop_residue_labels(top)
    check("residue_labels values",
          labels == ["ALA", "GLY", "SER", "VAL", "MOL"], str(labels))
    check("residue_labels count 5", len(labels) == 5, str(len(labels)))


def test_prmtop_residue_labels_multiline_block():
    # The block can span multiple lines (real prmtops wrap at 20 per line);
    # split() flattens across newlines. Boundary stops at the next %FLAG so
    # RESIDUE_POINTER numbers are NOT captured.
    top = _write("multi.prmtop",
                 _prmtop("ALA ARG ASN ASP CYS\n"
                         "GLN GLU GLY HIS ILE\n"
                         "WAT WAT\n"))
    labels = W.prmtop_residue_labels(top)
    expected = ["ALA", "ARG", "ASN", "ASP", "CYS",
                "GLN", "GLU", "GLY", "HIS", "ILE", "WAT", "WAT"]
    check("residue_labels multiline values", labels == expected, str(labels))
    check("residue_labels stops at next %FLAG (no '1'/'6' ints)",
          "1" not in labels and "6" not in labels, str(labels))


def test_prmtop_residue_labels_no_flag():
    # No RESIDUE_LABEL flag at all -> regex no-match -> [] (line 85-86).
    top = _write("noflag.prmtop",
                 "%FLAG POINTERS\n%FORMAT(10I8)\n   1  2  3\n")
    check("residue_labels no flag -> []", W.prmtop_residue_labels(top) == [],
          str(W.prmtop_residue_labels(top)))


# ---- detect_masks --------------------------------------------------------
# Body (lines 94-107): labels = prmtop_residue_labels(top); nres = len(labels).
# has_ligand True : prot_last = nres-1 ; lig_res = nres
# has_ligand False: prot_last = nres   ; lig_res = None
# returns {nres, prot_last, lig_res,
#          protein_mask=":1-{prot_last}",
#          ligand_mask=":{lig_res}" if lig_res else None,
#          solute_mask=":1-{nres}"}
# Use a 5-residue dry topology (4 protein + 1 ligand MOL) so the True case
# splits 1-4 protein / 5 ligand, and the False case treats all 5 as protein.

def test_detect_masks_with_ligand():
    top = _write("dry_complex.prmtop",
                 _prmtop("ALA GLY SER VAL MOL \n"))   # nres == 5
    m = W.detect_masks(top, has_ligand=True)
    check("masks(lig) nres 5", m["nres"] == 5, str(m))
    check("masks(lig) prot_last 4", m["prot_last"] == 4, str(m))
    check("masks(lig) lig_res 5", m["lig_res"] == 5, str(m))
    check("masks(lig) protein_mask :1-4", m["protein_mask"] == ":1-4", str(m))
    check("masks(lig) ligand_mask :5", m["ligand_mask"] == ":5", str(m))
    check("masks(lig) solute_mask :1-5", m["solute_mask"] == ":1-5", str(m))


def test_detect_masks_protein_only():
    top = _write("dry_apo.prmtop",
                 _prmtop("ALA GLY SER VAL MOL \n"))   # nres == 5
    m = W.detect_masks(top, has_ligand=False)
    check("masks(apo) nres 5", m["nres"] == 5, str(m))
    check("masks(apo) prot_last 5", m["prot_last"] == 5, str(m))
    check("masks(apo) lig_res None", m["lig_res"] is None, str(m))
    check("masks(apo) protein_mask :1-5", m["protein_mask"] == ":1-5", str(m))
    check("masks(apo) ligand_mask None", m["ligand_mask"] is None, str(m))
    check("masks(apo) solute_mask :1-5", m["solute_mask"] == ":1-5", str(m))


def test_detect_masks_keys_present():
    # The dict shape itself is part of the contract (downstream a_* indexers).
    top = _write("dry_keys.prmtop", _prmtop("ALA GLY MOL \n"))
    m = W.detect_masks(top, has_ligand=True)
    expected_keys = {"nres", "prot_last", "lig_res", "protein_mask",
                     "ligand_mask", "solute_mask"}
    check("masks dict has exactly the contract keys",
          set(m.keys()) == expected_keys, str(set(m.keys())))


# ---- prmtop_radius_set + gb_radii_check (GB-radii <-> igb detector) -------
# The exact RADIUS_SET value lines tleap writes for the Amber Table 4.1 sets.
RADIUS_SET_LINES = {
    "mbondi": "modified Bondi radii (mbondi)",
    "mbondi2": "H(N)-modified Bondi radii (mbondi2)",
    "mbondi3": "ArgH and AspGluO modified Bondi2 radii (mbondi3)",
    "bondi": "Bondi radii (bondi)",
}


def _prmtop_radius(radius_line: str) -> str:
    return (
        "%VERSION  VERSION_STAMP = V0001.000\n"
        "%FLAG TITLE\n%FORMAT(20a4)\ndefault_name\n"
        "%FLAG RADIUS_SET\n%FORMAT(1a80)\n"
        f"{radius_line}\n"
        "%FLAG RESIDUE_LABEL\n%FORMAT(20a4)\nALA GLY MOL\n")


def test_prmtop_radius_set():
    # Each of the four sets -> its parenthetical token (hand-derived). mbondi3's
    # line has "Bondi2" UNparenthesized, so the last-paren rule returns 'mbondi3'
    # (not 'bondi2') — the case the corrected wrapper comment describes.
    for token, line in RADIUS_SET_LINES.items():
        top = _write(f"rad_{token}.prmtop", _prmtop_radius(line))
        check(f"radius_set parses ({token})",
              W.prmtop_radius_set(top) == token, repr(W.prmtop_radius_set(top)))
    # Real-format layout pin: a committed excerpt from an ACTUAL parmed-retyped
    # mbondi2 prmtop (genuine 1a80 trailing padding + real %FLAG boundaries), so a
    # future prmtop-layout drift can't silently make the regex return None and stop
    # the GB-radii detector firing. Committed as .txt (not a gitignored *.top).
    real_fx = HERE / "fixtures" / "radius_set_mbondi2_real.txt"
    if real_fx.is_file():
        check("radius_set parses a REAL mbondi2 prmtop excerpt",
              W.prmtop_radius_set(real_fx) == "mbondi2",
              repr(W.prmtop_radius_set(real_fx)))
    else:
        print("  SKIP radius_set real fixture (missing)")
    # No RADIUS_SET flag -> None.
    noflag = _write("rad_noflag.prmtop",
                    "%FLAG POINTERS\n%FORMAT(10I8)\n   1 2 3\n")
    check("radius_set no flag -> None", W.prmtop_radius_set(noflag) is None,
          repr(W.prmtop_radius_set(noflag)))
    # Value line without a parenthetical token -> None (never guess).
    notok = _write("rad_notok.prmtop",
                   "%FLAG RADIUS_SET\n%FORMAT(1a80)\nsome radii no token\n"
                   "%FLAG RESIDUE_LABEL\n%FORMAT(20a4)\nALA\n")
    check("radius_set no token -> None", W.prmtop_radius_set(notok) is None,
          repr(W.prmtop_radius_set(notok)))
    # Missing file -> None.
    check("radius_set missing file -> None",
          W.prmtop_radius_set(Path("/no/such.prmtop")) is None)


def test_gb_radii_check():
    # igb=5 expects mbondi2; mbondi is the shipping reality -> MISMATCH + finding.
    r = W.gb_radii_check("mbondi", 5)
    check("igb5+mbondi consistent False", r["consistent"] is False, str(r))
    check("igb5+mbondi required mbondi2", r["required"] == "mbondi2", str(r))
    check("igb5+mbondi has GB_RADII_IGB_MISMATCH finding",
          "GB_RADII_IGB_MISMATCH" in r.get("finding", ""), str(r))
    # igb=5 + mbondi2 -> consistent, no finding.
    r2 = W.gb_radii_check("mbondi2", 5)
    check("igb5+mbondi2 consistent True", r2["consistent"] is True, str(r2))
    check("igb5+mbondi2 no finding", "finding" not in r2, str(r2))
    # igb=1 + mbondi -> consistent (Table 4.1).
    check("igb1+mbondi consistent True",
          W.gb_radii_check("mbondi", 1)["consistent"] is True, "")
    # radius_set None -> consistent None, NEVER a spurious finding.
    r4 = W.gb_radii_check(None, 5)
    check("radius_set None -> consistent None", r4["consistent"] is None, str(r4))
    check("radius_set None -> no finding", "finding" not in r4, str(r4))
    # Unknown igb -> required None -> no finding, no crash.
    r5 = W.gb_radii_check("mbondi", 99)
    check("unknown igb -> required None", r5["required"] is None, str(r5))
    check("unknown igb -> no finding", "finding" not in r5, str(r5))


# ---- TARGET_RADIUS_SET + parmed_radii_script + suite_ok (mbondi2 fix) -----

def test_target_radius_set():
    # The build target must be DERIVED from the igb map, not hardcoded — so the
    # radii we build with always equal the radii the check requires.
    check("MMGBSA_IGB is 5", W.MMGBSA_IGB == 5, str(W.MMGBSA_IGB))
    check("TARGET_RADIUS_SET derived from IGB_RADIUS_SET[MMGBSA_IGB]",
          W.TARGET_RADIUS_SET == W.IGB_RADIUS_SET[W.MMGBSA_IGB],
          str(W.TARGET_RADIUS_SET))
    check("TARGET_RADIUS_SET == mbondi2",
          W.TARGET_RADIUS_SET == "mbondi2", str(W.TARGET_RADIUS_SET))


def test_parmed_radii_script():
    s = W.parmed_radii_script("../comp_dry_mbondi2.top")
    lines = s.splitlines()
    check("parmed setOverwrite line", lines[0] == "setOverwrite True", repr(s))
    check("parmed changeRadii line", lines[1] == "changeRadii mbondi2", repr(s))
    check("parmed outparm line",
          lines[2] == "outparm ../comp_dry_mbondi2.top", repr(s))
    check("parmed quit line", lines[3] == "quit", repr(s))
    # Honors an explicit target (defaults to TARGET_RADIUS_SET otherwise).
    s2 = W.parmed_radii_script("../x.top", target="mbondi3")
    check("parmed honors explicit target",
          "changeRadii mbondi3\n" in s2, repr(s2))


def test_suite_ok():
    # No MM-GBSA in the run -> gb_radii None -> verdict is just core_ok.
    check("suite_ok None gb + core True", W.suite_ok(True, None) is True, "")
    check("suite_ok None gb + core False", W.suite_ok(False, None) is False, "")
    # Fixed build -> consistent True -> core passes through.
    good = W.gb_radii_check("mbondi2", 5)
    check("suite_ok consistent fix passes core",
          W.suite_ok(True, good) is True, str(good))
    # SURVIVING mismatch -> FATAL even when every core analysis produced.
    bad = W.gb_radii_check("mbondi", 5)
    check("suite_ok read mismatch -> False even if core True",
          W.suite_ok(True, bad) is False, str(bad))
    # Unverifiable radii (None) is not a spurious red -> core passes through.
    none = W.gb_radii_check(None, 5)
    check("suite_ok unverifiable radii passes core through",
          W.suite_ok(True, none) is True, str(none))


def main() -> int:
    tests = (
        test_prmtop_radius_set,
        test_gb_radii_check,
        test_target_radius_set,
        test_parmed_radii_script,
        test_suite_ok,
        test_load_xy_empty_file,
        test_load_xy_missing_file,
        test_load_xy_comment_blank_only,
        test_load_xy_valid_two_col,
        test_load_xy_extra_columns_uses_first_two,
        test_parse_evals_minimal,
        test_parse_evals_skips_covar_and_percent,
        test_parse_evals_none_found_fallback,
        test_parse_mmgbsa_missing,
        test_parse_mmgbsa_valid_negative,
        test_parse_mmgbsa_first_match_wins,
        test_parse_mmgbsa_garbage_no_match,
        test_parse_mmgbsa_requires_decimal,
        test_prmtop_residue_labels_basic,
        test_prmtop_residue_labels_multiline_block,
        test_prmtop_residue_labels_no_flag,
        test_detect_masks_with_ligand,
        test_detect_masks_protein_only,
        test_detect_masks_keys_present,
    )
    for fn in tests:
        fn()
    print(f"\ncpptraj-analysis engine tests: {PASS} passed, {FAIL} failed")
    if FAIL:
        print("FAILURES:")
        for f in FAILURES:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

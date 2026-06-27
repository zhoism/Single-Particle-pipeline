#!/usr/bin/env python3
"""Unit tests for plip-profile's pure engine functions (no cpptraj/PLIP needed).

Independent oracle for the deterministic core: the resname normalizer (THE PLIP
footgun fix), the variant-leak detector, the ligand-atom counter, and the PLIP
XML parser. Expected values are hand-derived, not produced by re-calling the
function under test. Run: python3 test_engine.py  (exit 0 = all pass).
"""
import sys
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


# A realistic AMBER cpptraj-style PDB fragment. Columns are PDB-exact:
# resName at 0-based [17:20]. Includes His (HIE), Cys disulfide (CYX),
# protonated Asp (ASH), neutral Lys (LYN), a standard residue (ALA), the ligand
# (MOL, 3-char), and a 4-char ion (Cl-) that must NOT be touched.
SAMPLE = (
    "ATOM      1  N   HIE     5      15.020  24.709  14.394  1.00  0.00           N  \n"
    "ATOM      2  CA  HIE     5      14.641  23.780  14.279  1.00  0.00           C  \n"
    "ATOM      3  SG  CYX    12      10.000  10.000  10.000  1.00  0.00           S  \n"
    "ATOM      4  OD2 ASH    20       1.000   2.000   3.000  1.00  0.00           O  \n"
    "ATOM      5  NZ  LYN    30       4.000   5.000   6.000  1.00  0.00           N  \n"
    "ATOM      6  CB  ALA    40       7.000   8.000   9.000  1.00  0.00           C  \n"
    "ATOM      7  CG  MOL    99      20.260  20.162  24.037  1.00  0.00           C  \n"
    "TER       8      MOL    99 \n"
    "ATOM      9 Cl-  Cl-   100       0.000   0.000   0.000  1.00  0.00          Cl  \n"
    "END\n"
)


def test_normalize():
    out, changes = W.normalize_resnames(SAMPLE)
    lines = out.splitlines()
    # HIE -> HIS on both HIE atom lines.
    check("HIE->HIS line1", lines[0][17:20] == "HIS", repr(lines[0][17:20]))
    check("HIE->HIS line2", lines[1][17:20] == "HIS", repr(lines[1][17:20]))
    check("CYX->CYS", lines[2][17:20] == "CYS", repr(lines[2][17:20]))
    check("ASH->ASP", lines[3][17:20] == "ASP", repr(lines[3][17:20]))
    check("LYN->LYS", lines[4][17:20] == "LYS", repr(lines[4][17:20]))
    check("ALA untouched", lines[5][17:20] == "ALA", repr(lines[5][17:20]))
    check("MOL ligand untouched", lines[6][17:20] == "MOL", repr(lines[6][17:20]))
    # The 4-char ion line resName field [17:20] is "l- " — not a variant key.
    check("Cl- ion untouched", "Cl-" in lines[8], lines[8])
    # change map: HIE counted twice, others once.
    check("changes HIE=2", changes.get("HIE") == 2, str(changes))
    check("changes CYX=1", changes.get("CYX") == 1, str(changes))
    check("changes ASH=1", changes.get("ASH") == 1, str(changes))
    check("changes LYN=1", changes.get("LYN") == 1, str(changes))
    check("changes has no ALA/MOL", "ALA" not in changes and "MOL" not in changes,
          str(changes))
    # Line count + non-ATOM lines preserved exactly.
    check("line count preserved",
          len(out.splitlines()) == len(SAMPLE.splitlines()),
          f"{len(out.splitlines())} vs {len(SAMPLE.splitlines())}")
    check("END preserved", out.splitlines()[-1] == "END", out.splitlines()[-1])
    # Coordinate columns unchanged on a normalized line (only cols 17-20 touched).
    check("coords preserved after rename",
          lines[0][20:] == SAMPLE.splitlines()[0][20:],
          "tail differs")
    check("prefix preserved after rename",
          lines[0][:17] == SAMPLE.splitlines()[0][:17],
          "prefix differs")


def test_idempotent_and_clean_roundtrip():
    once, _ = W.normalize_resnames(SAMPLE)
    twice, ch2 = W.normalize_resnames(once)
    check("idempotent", once == twice, "second pass changed bytes")
    check("idempotent no changes", ch2 == {}, str(ch2))
    # A clean PDB (no variants) round-trips byte-identical, terminators intact.
    clean = ("ATOM      1  N   ALA     1       1.000   2.000   3.000\r\n"
             "ATOM      2  CA  GLY     2       4.000   5.000   6.000\r\n")
    out, ch = W.normalize_resnames(clean)
    check("clean byte-identical", out == clean, "clean input mutated")
    check("clean no changes", ch == {}, str(ch))


def test_find_variants():
    leaked = W.find_amber_variants(SAMPLE)
    check("finds HIE", leaked.get("HIE") == 2, str(leaked))
    check("finds CYX/ASH/LYN",
          leaked.get("CYX") == 1 and leaked.get("ASH") == 1 and leaked.get("LYN") == 1,
          str(leaked))
    normalized, _ = W.normalize_resnames(SAMPLE)
    check("clean after normalize", W.find_amber_variants(normalized) == {},
          str(W.find_amber_variants(normalized)))


def test_count_ligand():
    check("MOL count = 1", W.count_ligand_atoms(SAMPLE, "MOL") == 1,
          str(W.count_ligand_atoms(SAMPLE, "MOL")))
    check("JZ4 count = 0", W.count_ligand_atoms(SAMPLE, "JZ4") == 0, "")
    check("HIE count = 2 (pre-norm)", W.count_ligand_atoms(SAMPLE, "HIE") == 2, "")


# Synthetic PLIP XML exercising multiple interaction types + a phantom.
SYNTH_XML = """<?xml version='1.0' encoding='UTF-8'?>
<report>
  <plipversion>3.0.0</plipversion>
  <bindingsite id="1" has_interactions="True">
    <identifiers>
      <longname>JZ4</longname><ligtype>SMALLMOLECULE</ligtype>
      <hetid>JZ4</hetid><chain>A</chain><position>200</position>
      <smiles>CCCc1ccccc1O</smiles>
    </identifiers>
    <lig_properties><num_heavy_atoms>11</num_heavy_atoms>
      <num_aromatic_rings>1</num_aromatic_rings></lig_properties>
    <interactions>
      <hydrophobic_interactions>
        <hydrophobic_interaction id="1">
          <resnr>78</resnr><restype>ILE</restype><reschain>A</reschain>
          <dist>3.80</dist></hydrophobic_interaction>
        <hydrophobic_interaction id="2">
          <resnr>111</resnr><restype>VAL</restype><reschain>A</reschain>
          <dist>3.90</dist></hydrophobic_interaction>
      </hydrophobic_interactions>
      <hydrogen_bonds>
        <hydrogen_bond id="1">
          <resnr>102</resnr><restype>GLN</restype><reschain>A</reschain>
          <dist_h-a>2.10</dist_h-a><dist_d-a>3.05</dist_d-a></hydrogen_bond>
      </hydrogen_bonds>
      <water_bridges/>
      <salt_bridges>
        <salt_bridge id="1"><resnr>90</resnr><restype>GLU</restype>
          <reschain>A</reschain><dist>3.50</dist></salt_bridge>
      </salt_bridges>
      <pi_stacks>
        <pi_stack id="1"><resnr>153</resnr><restype>PHE</restype>
          <reschain>A</reschain><centdist>4.20</centdist></pi_stack>
      </pi_stacks>
      <pi_cation_interactions/>
      <halogen_bonds/>
      <metal_complexes/>
    </interactions>
  </bindingsite>
  <bindingsite id="2" has_interactions="True">
    <identifiers>
      <longname>HISTIDINE</longname><ligtype>SMALLMOLECULE</ligtype>
      <hetid>HIS</hetid><chain>A</chain><position>31</position>
    </identifiers>
    <interactions>
      <hydrophobic_interactions/><hydrogen_bonds/><water_bridges/>
      <salt_bridges/><pi_stacks/><pi_cation_interactions/>
      <halogen_bonds/><metal_complexes/>
    </interactions>
  </bindingsite>
</report>
"""


def test_parse_synth():
    parsed = W.parse_plip_xml(SYNTH_XML)
    check("plipversion", parsed["plipversion"] == "3.0.0", "")
    check("two bindingsites", len(parsed["bindingsites"]) == 2,
          str(len(parsed["bindingsites"])))
    jz4 = next(s for s in parsed["bindingsites"] if s["hetid"] == "JZ4")
    check("JZ4 hydrophobic=2", jz4["counts"]["hydrophobic"] == 2,
          str(jz4["counts"]))
    check("JZ4 hbond=1", jz4["counts"]["hydrogen_bond"] == 1, str(jz4["counts"]))
    check("JZ4 salt=1", jz4["counts"]["salt_bridge"] == 1, str(jz4["counts"]))
    check("JZ4 pistack=1", jz4["counts"]["pi_stacking"] == 1, str(jz4["counts"]))
    check("JZ4 total=5", jz4["total"] == 5, str(jz4["total"]))
    check("JZ4 aromatic rings", jz4.get("num_aromatic_rings") == 1, "")
    # H-bond distance = the donor-acceptor distance (conventional H-bond length),
    # preferred over dist_h-a by the wrapper's field-priority order.
    hb = jz4["interactions"]["hydrogen_bond"][0]
    check("hbond dist parsed", abs(hb["dist"] - 3.05) < 1e-9, str(hb))
    check("hbond dist_field", hb["dist_field"] == "dist_d-a", str(hb))
    check("hbond residue label", hb["residue"] == "GLN102A", str(hb))
    # pi-stack distance from centdist.
    ps = jz4["interactions"]["pi_stacking"][0]
    check("pistack centdist", abs(ps["dist"] - 4.20) < 1e-9, str(ps))


def test_build_outputs_phantom():
    parsed = W.parse_plip_xml(SYNTH_XML)
    outputs, validation = W.build_outputs(
        parsed, "JZ4",
        {"policy": "medoid", "index": 5, "nframes": 10},
        {"complex_pdb": "x"})
    check("ligand detected", validation["ligand_detected"] is True, "")
    check("hetid matches", validation["ligand_hetid_matches"] is True, "")
    check("phantom = HIS", validation["phantom_ligands"] == ["HIS"],
          str(validation["phantom_ligands"]))
    check("total interactions 5", validation["interactions_found"] == 5,
          str(validation["interactions_found"]))
    cr = outputs["contact_residues"]
    check("contact residues set",
          set(cr) == {"ILE78A", "VAL111A", "GLN102A", "GLU90A", "PHE153A"},
          str(cr))
    check("by_type hydrophobic 2",
          outputs["totals"]["by_type"]["hydrophobic"] == 2, "")


def test_build_outputs_missing_ligand():
    parsed = W.parse_plip_xml(SYNTH_XML)
    outputs, validation = W.build_outputs(
        parsed, "ZZZ", {"policy": "last", "index": 1, "nframes": 1}, {})
    check("missing ligand not detected", validation["ligand_detected"] is False, "")
    check("missing ligand hetid None", outputs["ligand"]["hetid"] is None, "")


def test_unmapped_residue_gate():
    """The catch-all phantom gate (main()) computes
    residue_resnames(normalized) - STD_AA - {ligand}; any member would become a
    PLIP phantom. Verify the set logic flags caps/PTMs/nucleic/unmapped names and
    clears a clean frame. This is the regression guard for the HIGH-severity
    silent-pass the adversarial review found (an unmapped residue -> phantom)."""
    dirty = (
        "ATOM      1  C   ACE     0       0.000   0.000   0.000  1.00  0.00           C  \n"
        "ATOM      2  CA  ALA     1       1.000   1.000   1.000  1.00  0.00           C  \n"
        "ATOM      3  P   SEP    10       2.000   2.000   2.000  1.00  0.00           P  \n"
        "ATOM      4  N   NME    99       3.000   3.000   3.000  1.00  0.00           N  \n"
        "ATOM      5  N1  DA     50       4.000   4.000   4.000  1.00  0.00           N  \n"
        "ATOM      6  CG  MOL   100       5.000   5.000   5.000  1.00  0.00           C  \n"
    )
    norm, _ = W.normalize_resnames(dirty)
    unmapped = sorted(W.residue_resnames(norm) - W.STD_AA - {"MOL"})
    check("unmapped flags ACE/NME/SEP/DA",
          unmapped == ["ACE", "DA", "NME", "SEP"], str(unmapped))
    # CHARMM/AMBER His variants are normalized away, never flagged as unmapped.
    his = ("ATOM      1  CA  HSE     1       0.0     0.0     0.0  1.00  0.00           C  \n"
           "ATOM      2  CA  ALA     2       1.0     1.0     1.0  1.00  0.00           C  \n"
           "ATOM      3  CG  MOL   100       5.0     5.0     5.0  1.00  0.00           C  \n")
    hn, _ = W.normalize_resnames(his)
    check("HSE normalized to HIS (CHARMM)", "HIS" in W.residue_resnames(hn)
          and "HSE" not in W.residue_resnames(hn), str(W.residue_resnames(hn)))
    check("clean frame -> no unmapped",
          sorted(W.residue_resnames(hn) - W.STD_AA - {"MOL"}) == [],
          str(sorted(W.residue_resnames(hn) - W.STD_AA - {"MOL"})))


def test_parse_real_golden_xml():
    """If the golden-path BNZ XML fixture is reachable, parse it and assert the
    known 6 hydrophobic contacts on BNZ."""
    cand = (HERE.parent.parent.parent / "golden-path" / "plip_out"
            / "complex_frame_report.xml")
    if not cand.is_file():
        print("  SKIP test_parse_real_golden_xml (fixture not found)")
        return
    parsed = W.parse_plip_xml(cand.read_text())
    bnz = next((s for s in parsed["bindingsites"] if s["hetid"] == "BNZ"), None)
    check("golden BNZ present", bnz is not None, "")
    if bnz:
        check("golden BNZ hydrophobic=6", bnz["counts"]["hydrophobic"] == 6,
              str(bnz["counts"]))
        check("golden BNZ no hbonds", bnz["counts"]["hydrogen_bond"] == 0, "")
        outputs, validation = W.build_outputs(
            parsed, "BNZ", {"policy": "last", "index": 1, "nframes": 1}, {})
        check("golden BNZ contacts include LEU84A",
              "LEU84A" in outputs["contact_residues"],
              str(outputs["contact_residues"]))
        check("golden BNZ no phantoms", validation["phantom_ligands"] == [],
              str(validation["phantom_ligands"]))


def test_build_plip_cmd():
    """The PLIP argv must always carry --nohydro (keep tleap's authoritative,
    deterministic hydrogens; without it PLIP re-protonates via OpenBabel
    non-deterministically). Hard-asserted in build_plip_cmd as a regression
    tripwire — verify the flag is present and positioned as an option, and that
    the required-flags set is what we expect."""
    cmd = W.build_plip_cmd("plip", "complex_frame.pdb", "report")
    check("argv carries --nohydro", "--nohydro" in cmd, str(cmd))
    check("argv carries -x (XML)", "-x" in cmd, str(cmd))
    check("argv carries -t (text)", "-t" in cmd, str(cmd))
    check("argv: -f names the input pdb",
          cmd[cmd.index("-f") + 1] == "complex_frame.pdb", str(cmd))
    check("argv: -o names the out subdir",
          cmd[cmd.index("-o") + 1] == "report", str(cmd))
    check("required-flags constant is exactly (-t,-x,--nohydro)",
          W.PLIP_REQUIRED_FLAGS == ("-t", "-x", "--nohydro"),
          str(W.PLIP_REQUIRED_FLAGS))


def main() -> int:
    for fn in (test_normalize, test_idempotent_and_clean_roundtrip,
               test_find_variants, test_count_ligand, test_parse_synth,
               test_build_outputs_phantom, test_build_outputs_missing_ligand,
               test_unmapped_residue_gate, test_build_plip_cmd,
               test_parse_real_golden_xml):
        fn()
    print(f"\nplip-profile engine tests: {PASS} passed, {FAIL} failed")
    if FAIL:
        print("FAILURES:")
        for f in FAILURES:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

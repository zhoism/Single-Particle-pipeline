#!/usr/bin/env python3
"""plip-profile wrapper (Phase 3 Stage 6).

Profile protein-ligand non-covalent interactions on a production trajectory
using PLIP. Extracts a single representative complex frame (dry, hydrogens
intact), normalizes AMBER protonation / disulfide-variant residue names back to
standard PDB names (so PLIP classifies them as protein, not phantom ligands),
runs PLIP, and parses its XML report into a structured interaction envelope.

One exec call per skill turn. JSON envelope to stdout; progress to stderr.

Load-bearing rules (the Stage-6 traps):
  - THE PLIP RESNAME FOOTGUN. AMBER writes HIE/HID/HIP/CYX/CYM/ASH/GLH/LYN/...;
    PLIP treats any residue name it does not recognise as a small-molecule
    ligand -> it invents phantom ligands among the protein and/or mis-keys the
    real one. So we normalize the variant resnames to standard PDB names BEFORE
    PLIP, then VERIFY (a) no variant leaked into the PLIP input and (b) PLIP
    keyed on the real ligand resname, not a protein residue.
  - Never strip hydrogens from the ligand; PLIP needs explicit H to assign
    donors/acceptors and H-bonds. The production complex already carries H
    (tleap added it) and we keep it.
  - Single representative frame for v1. Default policy = a deterministic medoid
    (the real frame whose protein backbone is closest to the trajectory average)
    -> reproducible, byte-identical re-runs. last / explicit-N also available.
    Per-frame interaction occupancy is a v2 (noted, not built).
  - The strip step uses the SOLVATED topology (matches the trajectory atom
    count); the dry frame/trajectory uses the dry topology. This mirrors
    cpptraj-analysis and consumes the exact same inputs it does.

The wrapper does the work; the LLM only picks the skill.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional


SKILL_NAME = "plip-profile"
SCRIPT_DIR = Path(__file__).resolve().parent

# ---- AMBER variant resname -> standard PDB name --------------------------
# AMBER force fields (ff19SB/ff14SB) write protonation / tautomer / disulfide
# state into the residue NAME. PLIP only recognises the 20 canonical 3-letter
# codes; everything else is taken to be a ligand. Map every variant back to its
# canonical parent before PLIP sees it. The catch-all UNMAPPED_NONSTANDARD_
# RESIDUES gate in main() backstops anything NOT in this table (caps/PTMs/etc.)
# by failing loudly — so a missing entry can never become a silent phantom.
# Both AMBER (HID/HIE/HIP) and CHARMM (HSD/HSE/HSP + the HISx spellings)
# histidine names are included.
AMBER_VARIANTS: dict[str, str] = {
    # histidine tautomers / protonation (AMBER + CHARMM spellings)
    "HID": "HIS", "HIE": "HIS", "HIP": "HIS",
    "HSD": "HIS", "HSE": "HIS", "HSP": "HIS",
    "HISD": "HIS", "HISE": "HIS", "HISH": "HIS", "HISP": "HIS",
    # cysteine: disulfide / deprotonated
    "CYX": "CYS", "CYM": "CYS",
    # protonated acidics / neutral basics
    "ASH": "ASP", "GLH": "GLU", "LYN": "LYS",
    # rare neutral/charged tautomers
    "ARN": "ARG", "TYM": "TYR",
}

# The 20 canonical amino-acid 3-letter codes — used to flag a PLIP bindingsite
# whose hetid is actually a protein residue (a phantom ligand).
STD_AA: frozenset[str] = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
})

# Solvent + neutralizing counter-ions stripped before profiling (matches the
# cpptraj-analysis strip mask). Systems with functional metals/cofactors would
# need this relaxed — that is a v2 consideration, flagged in SKILL.md.
STRIP_MASK = ":WAT,:Na+,:Cl-,:K+"

# The eight PLIP interaction categories, container tag -> short label. Parsing
# is generic over <interactions>'s children, but this gives stable ordering and
# guarantees every category appears in the envelope (count 0 when absent).
INTERACTION_CATEGORIES: list[tuple[str, str]] = [
    ("hydrophobic_interactions", "hydrophobic"),
    ("hydrogen_bonds", "hydrogen_bond"),
    ("water_bridges", "water_bridge"),
    ("salt_bridges", "salt_bridge"),
    ("pi_stacks", "pi_stacking"),
    ("pi_cation_interactions", "pi_cation"),
    ("halogen_bonds", "halogen_bond"),
    ("metal_complexes", "metal_complex"),
]


# ---- Envelope ------------------------------------------------------------

def envelope(ok, dry_run, outputs=None, validation=None, errors=None) -> str:
    return json.dumps({
        "ok": ok, "skill": SKILL_NAME, "dry_run": dry_run,
        "outputs": outputs or {}, "validation": validation or {},
        "errors": errors or [],
    }, indent=2)


def emit_and_exit(*, ok, dry_run, outputs=None, validation=None, errors=None,
                  code=0) -> None:
    print(envelope(ok, dry_run, outputs, validation, errors))
    sys.exit(code)


def say(msg: str) -> None:
    print(f"[{SKILL_NAME}] {msg}", file=sys.stderr, flush=True)


# ---- Pure helpers (unit-tested) ------------------------------------------

def _resname_field(line: str) -> Optional[str]:
    """Return the 3-char PDB resName field (cols 18-20, 0-based [17:20]) of an
    ATOM/HETATM record, else None. Only exact-3-char names are returned so a
    4-char ligand/ion code is never touched."""
    if not (line.startswith("ATOM") or line.startswith("HETATM")):
        return None
    if len(line) < 20:
        return None
    return line[17:20]


def normalize_resnames(pdb_text: str) -> tuple[str, dict[str, int]]:
    """Rewrite AMBER variant residue names (HIE/CYX/...) to their standard PDB
    parent (HIS/CYS/...) on ATOM/HETATM lines only. Column-exact (resName field
    cols 18-20), idempotent, and order/line-count preserving. Returns the new
    text and a {variant: atom_lines_changed} map."""
    changes: dict[str, int] = {}
    out_lines: list[str] = []
    # splitlines(keepends=True) preserves the original line terminators exactly,
    # so a clean input round-trips byte-identical.
    for line in pdb_text.splitlines(keepends=True):
        field = _resname_field(line)
        if field is not None:
            key = field.strip()
            repl = AMBER_VARIANTS.get(key)
            if repl is not None:
                # All variants and parents are exactly 3 chars -> fixed-width.
                line = line[:17] + f"{repl:<3}" + line[20:]
                changes[key] = changes.get(key, 0) + 1
        out_lines.append(line)
    return "".join(out_lines), changes


def find_amber_variants(pdb_text: str) -> dict[str, int]:
    """Count ATOM/HETATM lines whose resName is still an AMBER variant. A clean
    (normalized) PDB returns {}. Used as the post-normalization verification
    gate — a non-empty result means the mapping is missing a variant."""
    seen: dict[str, int] = {}
    for line in pdb_text.splitlines():
        field = _resname_field(line)
        if field is not None:
            key = field.strip()
            if key in AMBER_VARIANTS:
                seen[key] = seen.get(key, 0) + 1
    return seen


def residue_resnames(pdb_text: str) -> set[str]:
    """Distinct resNames present on ATOM/HETATM lines."""
    names: set[str] = set()
    for line in pdb_text.splitlines():
        field = _resname_field(line)
        if field is not None:
            names.add(field.strip())
    return names


def count_ligand_atoms(pdb_text: str, resname: str) -> int:
    """Number of ATOM/HETATM lines whose resName matches `resname` exactly."""
    n = 0
    target = resname.strip()
    for line in pdb_text.splitlines():
        field = _resname_field(line)
        if field is not None and field.strip() == target:
            n += 1
    return n


def _txt(elem: Optional[ET.Element]) -> Optional[str]:
    if elem is None or elem.text is None:
        return None
    return elem.text.strip()


def _interaction_record(node: ET.Element) -> dict[str, Any]:
    """Flatten one interaction element (e.g. <hydrogen_bond>) into the salient
    fields. Generic: pulls the common residue identity + whatever distance-like
    fields PLIP emitted for this interaction type."""
    rec: dict[str, Any] = {}
    for tag in ("resnr", "restype", "reschain"):
        v = _txt(node.find(tag))
        if v is not None:
            rec[tag] = v
    # Distance fields differ by interaction type; capture the first present so
    # the summary always has a number, plus keep the raw tag name.
    for tag in ("dist", "dist_d-a", "dist_h-a", "centdist", "dist_a-w"):
        v = _txt(node.find(tag))
        if v is not None:
            try:
                rec["dist"] = float(v)
            except ValueError:
                rec["dist"] = v
            rec["dist_field"] = tag
            break
    # A compact, stable residue label for summaries: e.g. "LEU84A".
    if "restype" in rec and "resnr" in rec:
        rec["residue"] = f"{rec['restype']}{rec['resnr']}{rec.get('reschain', '')}"
    return rec


def parse_plip_xml(xml_text: str) -> dict[str, Any]:
    """Parse a PLIP XML report into a structured dict:
       { bindingsites: [ {hetid, longname, ligtype, chain, position, smiles,
                          num_heavy_atoms, num_aromatic_rings,
                          interactions: {label: [records...]},
                          counts: {label: n}, total: n } ],
         plipversion }
    Raises ET.ParseError on malformed XML (caller handles)."""
    root = ET.fromstring(xml_text)
    out: dict[str, Any] = {"plipversion": _txt(root.find("plipversion")),
                           "bindingsites": []}
    for bs in root.findall("bindingsite"):
        ident = bs.find("identifiers")
        props = bs.find("lig_properties")
        site: dict[str, Any] = {
            "hetid": _txt(ident.find("hetid")) if ident is not None else None,
            "longname": _txt(ident.find("longname")) if ident is not None else None,
            "ligtype": _txt(ident.find("ligtype")) if ident is not None else None,
            "chain": _txt(ident.find("chain")) if ident is not None else None,
            "position": _txt(ident.find("position")) if ident is not None else None,
            "smiles": _txt(ident.find("smiles")) if ident is not None else None,
            "has_interactions": bs.get("has_interactions"),
        }
        if props is not None:
            for tag in ("num_heavy_atoms", "num_aromatic_rings",
                        "num_hbd", "num_hba"):
                v = _txt(props.find(tag))
                if v is not None:
                    try:
                        site[tag] = int(v)
                    except ValueError:
                        site[tag] = v
        interactions: dict[str, list] = {}
        counts: dict[str, int] = {}
        inter = bs.find("interactions")
        for container, label in INTERACTION_CATEGORIES:
            recs: list[dict] = []
            if inter is not None:
                node = inter.find(container)
                if node is not None:
                    for child in list(node):
                        recs.append(_interaction_record(child))
            interactions[label] = recs
            counts[label] = len(recs)
        site["interactions"] = interactions
        site["counts"] = counts
        site["total"] = sum(counts.values())
        out["bindingsites"].append(site)
    return out


def summarize_text(outputs: dict[str, Any]) -> str:
    """A short human-readable interaction fingerprint."""
    lines: list[str] = []
    lig = outputs.get("ligand", {})
    frame = outputs.get("frame", {})
    lines.append(f"PLIP interaction profile — ligand {lig.get('hetid', '?')} "
                 f"(chain {lig.get('chain', '?')}, pos {lig.get('position', '?')})")
    lines.append(f"frame: {frame.get('policy', '?')} (index "
                 f"{frame.get('index', '?')}/{frame.get('nframes', '?')})")
    totals = outputs.get("totals", {})
    lines.append(f"total interactions: {totals.get('total_interactions', 0)}")
    by_type = totals.get("by_type", {})
    inter = outputs.get("interactions", {})
    for label, n in by_type.items():
        if n == 0:
            continue
        residues = [r.get("residue", "?") for r in inter.get(label, [])]
        lines.append(f"  {label:14s} {n:3d}  {', '.join(residues)}")
    cr = outputs.get("contact_residues", [])
    if cr:
        lines.append(f"contact residues ({len(cr)}): {', '.join(cr)}")
    return "\n".join(lines) + "\n"


# ---- cpptraj orchestration -----------------------------------------------

class StepFailure(RuntimeError):
    pass


def run_cpptraj(cpptraj: str, in_text: str, in_path: Path, cwd: Path) -> str:
    in_path.write_text(in_text)
    log = cwd / (in_path.stem + ".log")
    with log.open("w") as lf:
        r = subprocess.run([cpptraj, "-i", in_path.name], cwd=str(cwd),
                           stdout=lf, stderr=subprocess.STDOUT)
    out = log.read_text(errors="replace")
    if r.returncode != 0:
        raise StepFailure(f"cpptraj {in_path.name} rc={r.returncode}; see {log}")
    return out


def traj_nframes(cpptraj: str, top: str, traj: str, cwd: Path) -> int:
    """Frame count of a trajectory (cpptraj -tl). 0 if it can't be read."""
    r = subprocess.run([cpptraj, "-p", top, "-y", traj, "-tl"],
                       cwd=str(cwd), capture_output=True, text=True)
    m = re.search(r"Frames:\s*(\d+)", r.stdout + r.stderr)
    return int(m.group(1)) if m else 0


def strip_to_dry(cpptraj: str, cwd: Path) -> None:
    """Pass A: strip solvent + ions with the SOLVATED topology, autoimage, write
    a dry trajectory dry.nc (protein + ligand, H intact)."""
    txt = ("parm comp_oct.top\n"
           "trajin traj.nc\n"
           f"strip {STRIP_MASK}\n"
           "autoimage\n"
           "trajout dry.nc netcdf\n"
           "run\nquit\n")
    run_cpptraj(cpptraj, txt, cwd / "01_strip.in", cwd)
    if not (cwd / "dry.nc").exists() or (cwd / "dry.nc").stat().st_size == 0:
        raise StepFailure("strip produced no dry.nc")


def medoid_frame(cpptraj: str, cwd: Path) -> tuple[int, int]:
    """Default policy: the real frame whose protein backbone is closest to the
    trajectory average (a deterministic medoid). Two cpptraj passes over dry.nc:
    (B1) average structure -> avg.pdb; (B2) per-frame RMSD to avg -> rms.dat.
    Returns (1-based frame index, nframes). Ties broken by lowest index."""
    run_cpptraj(cpptraj,
                ("parm comp_dry.top\n"
                 "trajin dry.nc\n"
                 "rms first @CA,C,N\n"
                 "average avg.pdb pdb\n"
                 "run\nquit\n"),
                cwd / "02a_average.in", cwd)
    run_cpptraj(cpptraj,
                ("parm comp_dry.top\n"
                 "trajin dry.nc\n"
                 "reference avg.pdb\n"
                 # `reference` keyword = best-fit RMSD of each frame to the
                 # loaded average over the backbone; `ref <mask>` would instead
                 # be read as a reference *named* <mask>.
                 "rms ToAvg @CA,C,N reference out rms_to_avg.dat\n"
                 "run\nquit\n"),
                cwd / "02b_rmstoavg.in", cwd)
    rows: list[tuple[int, float]] = []
    for ln in (cwd / "rms_to_avg.dat").read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split()
        if len(parts) >= 2:
            try:
                rows.append((int(float(parts[0])), float(parts[1])))
            except ValueError:
                continue
    if not rows:
        raise StepFailure("medoid: rms_to_avg.dat had no data")
    best = min(rows, key=lambda fr: (fr[1], fr[0]))
    return best[0], len(rows)


def extract_frame_pdb(cpptraj: str, cwd: Path, frame_1based: int,
                      out_name: str) -> None:
    """Pass C: write one frame of dry.nc (already stripped + imaged) as a PDB."""
    txt = ("parm comp_dry.top\n"
           f"trajin dry.nc {frame_1based} {frame_1based} 1\n"
           f"trajout {out_name} pdb\n"
           "run\nquit\n")
    run_cpptraj(cpptraj, txt, cwd / "03_frame.in", cwd)
    if not (cwd / out_name).exists() or (cwd / out_name).stat().st_size == 0:
        raise StepFailure(f"frame extraction produced no {out_name}")


# ---- PLIP ----------------------------------------------------------------

def run_plip(plip: str, pdb_name: str, out_subdir: str, cwd: Path) -> Path:
    """Run PLIP (XML + TXT reports) on a dry complex PDB. Relative paths under a
    space-containing cwd (PLIP tokenizes safely on relative names — proven in
    golden-path). Returns the XML report path. Raises StepFailure on failure."""
    od = cwd / out_subdir
    if od.exists():
        shutil.rmtree(od)
    od.mkdir(parents=True, exist_ok=True)
    log = cwd / "plip.log"
    with log.open("w") as lf:
        r = subprocess.run([plip, "-f", pdb_name, "-t", "-x", "-o", out_subdir],
                           cwd=str(cwd), stdout=lf, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        tail = log.read_text(errors="replace")[-800:]
        raise StepFailure(f"plip rc={r.returncode}; log tail:\n{tail}")
    xmls = sorted(od.glob("*.xml"))
    if not xmls:
        raise StepFailure("PLIP produced no XML report")
    # Prefer the canonical "<stem>_report.xml"; fall back to the first XML.
    for x in xmls:
        if x.name.endswith("_report.xml"):
            return x
    return xmls[0]


# ---- plotting (optional — only if matplotlib is importable) ---------------

def maybe_plot(outputs: dict[str, Any], png_path: Path) -> Optional[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    by_type = outputs.get("totals", {}).get("by_type", {})
    items = [(lbl, n) for lbl, n in by_type.items() if n > 0]
    if not items:
        items = [("none", 0)]
    labels = [k for k, _ in items]
    vals = [v for _, v in items]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(range(len(vals)), vals, color="#3b7dd8")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("count")
    lig = outputs.get("ligand", {}).get("hetid", "?")
    ax.set_title(f"PLIP interactions — {lig}")
    fig.tight_layout()
    fig.savefig(png_path, dpi=150)
    plt.close(fig)
    return str(png_path) if png_path.exists() else None


# ---- Main ----------------------------------------------------------------

def build_outputs(parsed: dict, ligand_resname: str, frame_info: dict,
                  paths: dict) -> tuple[dict, dict]:
    """Assemble the outputs + validation blocks from the parsed PLIP XML.
    Returns (outputs, validation)."""
    sites = parsed.get("bindingsites", [])
    lig_resname = ligand_resname.strip()

    # The ligand bindingsite = the one whose hetid matches our ligand resname.
    lig_site = next((s for s in sites if (s.get("hetid") or "") == lig_resname),
                    None)
    # Phantom ligands = bindingsites PLIP keyed on a standard amino acid (i.e.
    # it mistook a protein residue for a ligand). Must be empty.
    phantoms = [s.get("hetid") for s in sites
                if (s.get("hetid") or "") in STD_AA]
    other_sites = [s.get("hetid") for s in sites
                   if s is not lig_site and (s.get("hetid") or "") not in STD_AA]

    interactions: dict[str, list] = {}
    counts: dict[str, int] = {}
    contact_residues: list[str] = []
    if lig_site is not None:
        interactions = lig_site["interactions"]
        counts = lig_site["counts"]
        seen = set()
        for _, label in INTERACTION_CATEGORIES:
            for rec in interactions.get(label, []):
                res = rec.get("residue")
                if res and res not in seen:
                    seen.add(res)
                    contact_residues.append(res)

    total = sum(counts.values())
    outputs = {
        "ligand": {
            "hetid": lig_site.get("hetid") if lig_site else None,
            "longname": lig_site.get("longname") if lig_site else None,
            "chain": lig_site.get("chain") if lig_site else None,
            "position": lig_site.get("position") if lig_site else None,
            "smiles": lig_site.get("smiles") if lig_site else None,
            "num_heavy_atoms": lig_site.get("num_heavy_atoms") if lig_site else None,
            "num_aromatic_rings": lig_site.get("num_aromatic_rings") if lig_site else None,
        },
        "frame": frame_info,
        "interactions": interactions,
        "totals": {"total_interactions": total, "by_type": counts},
        "contact_residues": contact_residues,
        "plip_version": parsed.get("plipversion"),
        **paths,
    }
    validation = {
        "ligand_detected": lig_site is not None,
        "ligand_hetid": lig_resname,
        "ligand_hetid_matches": lig_site is not None,
        "phantom_ligands": phantoms,
        "other_smallmolecules": other_sites,
        "interactions_found": total,
        "n_bindingsites": len(sites),
    }
    return outputs, validation


def main() -> None:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Profile protein-ligand interactions on an MD trajectory "
                    "with PLIP (deterministic wrapper).")
    p.add_argument("--comp-oct-top", required=True,
                   help="Solvated topology (used only to strip solvent).")
    p.add_argument("--comp-dry-top", required=True,
                   help="Dry complex topology (protein + ligand).")
    p.add_argument("--traj", required=True, help="Production trajectory (.nc).")
    p.add_argument("--ligand-resname", "--name", dest="ligand_resname",
                   default="MOL",
                   help="Ligand residue name PLIP must key on (default MOL).")
    p.add_argument("--frame", default="medoid",
                   help="Frame policy: 'medoid' (default, closest-to-average), "
                        "'last', or an integer (1-based).")
    p.add_argument("--output-dir", default="./plip")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    lig_resname = args.ligand_resname.strip()
    out_dir = Path(args.output_dir).expanduser().resolve()

    # Parse the frame policy up front so a bad value fails before any work.
    frame_policy = args.frame.strip().lower()
    explicit_n: Optional[int] = None
    if frame_policy not in ("medoid", "last"):
        try:
            explicit_n = int(frame_policy)
            if explicit_n < 1:
                raise ValueError
            frame_policy = "explicit"
        except ValueError:
            emit_and_exit(ok=False, dry_run=args.dry_run,
                          errors=[f"INVALID_INPUT: --frame must be "
                                  f"'medoid', 'last', or a positive integer; "
                                  f"got {args.frame!r}"], code=1)

    cpptraj = shutil.which("cpptraj")
    plip = shutil.which("plip")

    if args.dry_run:
        plan = {
            "output_dir": str(out_dir),
            "ligand_resname": lig_resname,
            "frame_policy": frame_policy,
            "explicit_frame": explicit_n,
            "strip_mask": STRIP_MASK,
            "normalization_variants": sorted(AMBER_VARIANTS),
            "interaction_categories": [lbl for _, lbl in INTERACTION_CATEGORIES],
            "cpptraj": cpptraj, "plip": plip,
        }
        emit_and_exit(ok=True, dry_run=True, outputs={"plan": plan},
                      validation={}, errors=[], code=0)

    # A ligand resname that is a standard amino acid would be invisible to PLIP
    # (it classifies it as protein). Refuse it up front — independent of files
    # and binaries — rather than silently "finding no ligand" downstream.
    if lig_resname in STD_AA:
        emit_and_exit(ok=False, dry_run=False,
                      errors=[f"INVALID_INPUT: --ligand-resname {lig_resname!r} "
                              f"is a standard amino acid; PLIP would treat it as "
                              f"protein. Use the ligand's non-standard resname."],
                      code=1)

    # Binaries present?
    missing_bins = [n for n, b in (("cpptraj", cpptraj), ("plip", plip))
                    if b is None]
    if missing_bins:
        emit_and_exit(ok=False, dry_run=False,
                      errors=[f"MISSING_BINARY: {', '.join(missing_bins)} "
                              f"not on PATH (source scripts/env.sh)"], code=1)

    # Inputs present?
    missing = []
    for label, val in (("--comp-oct-top", args.comp_oct_top),
                       ("--comp-dry-top", args.comp_dry_top),
                       ("--traj", args.traj)):
        if not Path(val).expanduser().is_file():
            missing.append(f"INVALID_INPUT: {label} not found: {val}")
    if missing:
        emit_and_exit(ok=False, dry_run=False, errors=missing, code=1)

    out_dir.mkdir(parents=True, exist_ok=True)
    # cpptraj/PLIP tokenize input lines on whitespace; the project path contains
    # a space. Copy inputs in under bare names and run with cwd=out_dir.
    try:
        shutil.copy(Path(args.comp_oct_top).expanduser(), out_dir / "comp_oct.top")
        shutil.copy(Path(args.comp_dry_top).expanduser(), out_dir / "comp_dry.top")
        shutil.copy(Path(args.traj).expanduser(), out_dir / "traj.nc")
    except OSError as e:
        emit_and_exit(ok=False, dry_run=False,
                      errors=[f"INPUT_STAGING_FAILED: {e}"], code=1)

    errors: list[str] = []

    # --- Frame extraction (cpptraj) ---
    try:
        say("strip solvent -> dry trajectory")
        strip_to_dry(cpptraj, out_dir)
        nframes = traj_nframes(cpptraj, "comp_dry.top", "dry.nc", out_dir)
        if nframes <= 0:
            raise StepFailure("could not determine frame count of dry.nc")

        if frame_policy == "explicit":
            if explicit_n > nframes:
                raise StepFailure(f"--frame {explicit_n} exceeds nframes={nframes}")
            chosen, policy_used = explicit_n, "explicit"
        elif frame_policy == "last":
            chosen, policy_used = nframes, "last"
        else:  # medoid (default), with graceful fallback for tiny trajectories
            if nframes < 3:
                chosen, policy_used = nframes, "last (fallback: <3 frames)"
                say(f"only {nframes} frames; medoid not meaningful -> last frame")
            else:
                say("selecting medoid frame (closest to backbone average)")
                chosen, _ = medoid_frame(cpptraj, out_dir)
                policy_used = "medoid"
        say(f"representative frame = {chosen}/{nframes} ({policy_used})")
        extract_frame_pdb(cpptraj, out_dir, chosen, "complex_raw.pdb")
    except StepFailure as e:
        emit_and_exit(ok=False, dry_run=False,
                      errors=[f"FRAME_EXTRACTION_FAILED: {e}"], code=2)

    frame_info = {"policy": policy_used, "index": chosen, "nframes": nframes}

    # --- Resname normalization (THE PLIP footgun) + verification ---
    raw = (out_dir / "complex_raw.pdb").read_text()
    normalized, changes = normalize_resnames(raw)
    (out_dir / "complex_frame.pdb").write_text(normalized)

    leaked = find_amber_variants(normalized)
    if leaked:
        # The mapping is missing a variant — fail loudly so it gets extended,
        # rather than letting PLIP invent phantom ligands downstream.
        emit_and_exit(
            ok=False, dry_run=False,
            outputs={"complex_pdb": str(out_dir / "complex_frame.pdb"),
                     "frame": frame_info, "resnames_changed": changes},
            validation={"resnames_normalized": False, "leaked_variants": leaked},
            errors=[f"RESNAME_NORMALIZATION_INCOMPLETE: AMBER variant resnames "
                    f"survived into the PLIP input: {leaked}. Extend "
                    f"AMBER_VARIANTS."], code=3)

    lig_atoms = count_ligand_atoms(normalized, lig_resname)
    if lig_atoms == 0:
        present = sorted(residue_resnames(normalized) - STD_AA)
        emit_and_exit(
            ok=False, dry_run=False,
            outputs={"complex_pdb": str(out_dir / "complex_frame.pdb"),
                     "frame": frame_info},
            validation={"resnames_normalized": True,
                        "ligand_resname": lig_resname,
                        "ligand_atoms_in_frame": 0,
                        "non_standard_resnames_present": present},
            errors=[f"LIGAND_NOT_IN_FRAME: no atoms with resname "
                    f"{lig_resname!r} in the extracted frame. Non-standard "
                    f"resnames present: {present}. Check --ligand-resname."],
            code=3)

    # Catch-all phantom gate (the load-bearing one). Any residue name that is
    # neither a standard amino acid nor the ligand has survived normalization ->
    # PLIP will profile it as a phantom small-molecule ligand. This covers what
    # the two gates above CANNOT: a variant missing from AMBER_VARIANTS, AMBER
    # caps (ACE/NME/NHE), PTMs (SEP/TPO/PTR), nucleic-acid residues, an ion or
    # cofactor the strip mask missed, or a non-standard name generally. Both the
    # known-variant gate (circular — only flags names already in the table) and
    # the standard-AA phantom gate are blind to it, so fail loudly here listing
    # the names rather than silently reporting ok:true with a phantom.
    unmapped = sorted(residue_resnames(normalized) - STD_AA - {lig_resname})
    if unmapped:
        emit_and_exit(
            ok=False, dry_run=False,
            outputs={"complex_pdb": str(out_dir / "complex_frame.pdb"),
                     "frame": frame_info, "resnames_changed": changes},
            validation={"resnames_normalized": True,
                        "ligand_resname": lig_resname,
                        "unmapped_nonstandard_resnames": unmapped},
            errors=[f"UNMAPPED_NONSTANDARD_RESIDUES: residue name(s) {unmapped} "
                    f"are neither a standard amino acid nor the ligand "
                    f"{lig_resname!r}; PLIP would profile them as phantom "
                    f"ligands. Extend AMBER_VARIANTS (a protonation/cap/PTM "
                    f"variant), relax the strip mask (an ion/cofactor), or fix "
                    f"--ligand-resname."], code=3)

    # --- PLIP ---
    try:
        say(f"running PLIP on dry complex frame (ligand {lig_resname})")
        xml_path = run_plip(plip, "complex_frame.pdb", "report", out_dir)
    except StepFailure as e:
        emit_and_exit(ok=False, dry_run=False,
                      outputs={"complex_pdb": str(out_dir / "complex_frame.pdb"),
                               "frame": frame_info},
                      errors=[f"PLIP_FAILED: {e}"], code=4)

    # --- Parse + assemble ---
    try:
        parsed = parse_plip_xml(xml_path.read_text())
    except ET.ParseError as e:
        emit_and_exit(ok=False, dry_run=False,
                      errors=[f"PLIP_XML_PARSE_FAILED: {e}"], code=4)

    txt_path = next(iter(sorted((out_dir / "report").glob("*.txt"))), None)
    paths = {
        "complex_pdb": str(out_dir / "complex_frame.pdb"),
        "plip_report_xml": str(xml_path),
        "plip_report_txt": str(txt_path) if txt_path else None,
        "resnames_changed": changes,
    }
    outputs, validation = build_outputs(parsed, lig_resname, frame_info, paths)
    validation["resnames_normalized"] = True

    # Human-readable summary + optional bar chart.
    summary = summarize_text(outputs)
    summary_path = out_dir / "interaction_summary.txt"
    summary_path.write_text(summary)
    outputs["summary_txt"] = str(summary_path)
    say("\n" + summary)
    png = maybe_plot(outputs, out_dir / "interactions.png")
    if png:
        outputs["interactions_png"] = png

    # --- Validation gates ---
    if not validation["ligand_detected"]:
        errors.append(f"LIGAND_NOT_DETECTED: PLIP did not key on {lig_resname!r}. "
                      f"bindingsites found: "
                      f"{validation['phantom_ligands'] + validation['other_smallmolecules']}")
    if validation["phantom_ligands"]:
        errors.append(f"PHANTOM_LIGANDS: PLIP classified protein residue(s) as "
                      f"ligands: {validation['phantom_ligands']} — normalization "
                      f"gap or a non-standard residue.")
    if validation["ligand_detected"] and validation["interactions_found"] == 0:
        # Not necessarily an error (a ligand can have zero contacts in one
        # frame), but flagged so it is never a silent pass.
        validation["note"] = ("ligand detected but zero interactions in this "
                              "frame — check the frame/pose if unexpected")

    ok = (validation["ligand_detected"]
          and not validation["phantom_ligands"]
          and not errors)
    emit_and_exit(ok=ok, dry_run=False, outputs=outputs, validation=validation,
                  errors=errors, code=0 if ok else 5)


if __name__ == "__main__":
    main()

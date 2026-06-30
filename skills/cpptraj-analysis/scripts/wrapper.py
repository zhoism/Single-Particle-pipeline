#!/usr/bin/env python3
"""cpptraj-analysis wrapper (Phase 3 Stage 5).

Run the full post-MD analysis suite on a production trajectory: preprocess
(strip solvent -> dry trajectory), then RMSD/RMSF/Rg, SASA, DSSP, H-bonds,
distance matrix, clustering, PCA, free-energy landscape, thermodynamics, and
MM-GBSA binding free energy. Each analysis emits a .dat and a .png.

One exec call per skill turn. JSON envelope to stdout; progress to stderr.

Correctness rules baked in (Research_amber_md_skill.md steal-list):
  - Preprocessing strips with the SOLVATED topology (comp_oct, matches the
    trajectory atom count) and writes a dry strip.nc; every downstream module
    uses the dry topology (comp_dry) + strip.nc (steal #6 + the atom-count fix).
  - PCA is two separate cpptraj calls (diagmatrix+run, then projection) — one
    call yields "evecs contains no data" (steal #4).
  - Clustering keeps repout INSIDE the single kmeans command (steal #4).
  - H-bond "no data" is a finding, not an error (steal #4).
  - Residue masks are auto-detected from the topology, not hardcoded :1-20/:21.
  - evecs.dat is hand-parsed, not pd.read_csv (steal #5).
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

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


SKILL_NAME = "cpptraj-analysis"
SCRIPT_DIR = Path(__file__).resolve().parent

ALL_ANALYSES = ["rmsd", "rmsf", "rg", "sasa", "dssp", "hbond", "distmat",
                "cluster", "pca", "fel", "thermo", "mmgbsa"]

# --- MM-GBSA implicit-solvent model + GB-radii consistency (Table 4.1) -----
# Single source of truth for the igb used to build mmgbsa.in AND the radii
# detector below, so the check can never drift from the actual run.
# igb=5 = Onufriev-Bashford-Case GB^OBC (II).
MMGBSA_IGB = 5

# Amber26 Table 4.1: each GB model is parameterized for one PBRadii set; running
# a prmtop built with a different RADIUS_SET silently degrades the GB solvation
# term. The token in the prmtop's RADIUS_SET line ("...(mbondi)") is the key.
IGB_RADIUS_SET = {1: "mbondi", 2: "mbondi2", 5: "mbondi2", 7: "bondi",
                  8: "mbondi3"}

# a_mmgbsa retypes the dry MM-GBSA component topologies to the radii the run's igb
# REQUIRES, derived from the same map the check reads — so "what we build with" can
# never drift from "what the check requires", and the fatal gate below cannot fire
# on our own fixed builds.
TARGET_RADIUS_SET = IGB_RADIUS_SET[MMGBSA_IGB]   # igb=5 -> "mbondi2"


def prmtop_radius_set(top_path: Path) -> str | None:
    """The PBRadii set token from a prmtop's %FLAG RADIUS_SET value line, e.g.
    'modified Bondi radii (mbondi)' -> 'mbondi'. The parenthetical token is the
    reliable key across all four sets (mbondi/mbondi2/mbondi3/bondi). Returns
    None on any parse failure (callers treat None as 'could not verify')."""
    try:
        text = top_path.read_text()
    except (OSError, UnicodeError):
        return None
    m = re.search(r"%FLAG RADIUS_SET\s*\n%FORMAT\([^)]*\)\s*\n([^\n]*)", text)
    if not m:
        return None
    # The set token is the LAST parenthetical on the line — mbondi2/mbondi3 carry
    # an earlier "(N)" / "(Bondi2)" aside (e.g. "H(N)-modified Bondi radii
    # (mbondi2)"), so a first-match grab would wrongly return 'n'.
    toks = re.findall(r"\(([A-Za-z0-9]+)\)", m.group(1))
    return toks[-1].lower() if toks else None


def gb_radii_check(radius_set: str | None, igb: int) -> dict[str, Any]:
    """Consistency record between a prmtop's RADIUS_SET and the GB igb.
    Returns {igb, radius_set, required, consistent[, finding]}. `consistent` is
    None when the radius set could not be read (never a spurious mismatch).

    a_mmgbsa retypes the dry topologies to the required set (TARGET_RADIUS_SET)
    before MM-GBSA, so on a fixed build this reports consistent. A SURVIVING
    mismatch is FATAL — it means the fix was missing or did not take; the suite
    verdict (suite_ok) reds the run rather than report a dG under wrong radii."""
    required = IGB_RADIUS_SET.get(igb)
    consistent = (radius_set == required) if radius_set is not None else None
    out: dict[str, Any] = {"igb": igb, "radius_set": radius_set,
                           "required": required, "consistent": consistent}
    if radius_set is not None and required is not None and radius_set != required:
        out["finding"] = (
            f"GB_RADII_IGB_MISMATCH: prmtop RADIUS_SET '{radius_set}' but igb={igb} "
            f"expects '{required}' (Amber Table 4.1). a_mmgbsa retypes the dry tops "
            "to the required set before MM-GBSA; a surviving mismatch = the fix was "
            "missing or did not take — FATAL.")
    return out


def parmed_radii_script(out_top_rel: str, target: str = TARGET_RADIUS_SET) -> str:
    """ParmEd action script (fed via `parmed -p <in> -i <script>`): retype the GB
    radii to `target` and write ONLY a new prmtop. `changeRadii` rewrites both the
    RADII array AND the %FLAG RADIUS_SET descriptor (the token prmtop_radius_set
    reads). `setOverwrite True` guards a re-run into a non-empty output dir.
    out_top_rel is relative (../name) so a space in the project path can't
    tokenize the line."""
    return f"setOverwrite True\nchangeRadii {target}\noutparm {out_top_rel}\nquit\n"


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


def suite_ok(core_ok: bool, gb_radii: dict[str, Any] | None) -> bool:
    """Final suite verdict. Core analyses must be produced AND, when MM-GBSA ran,
    the GB radii must be consistent with igb (Amber Table 4.1). A read mismatch
    (consistent is False) is FATAL — the mbondi2 fix was missing or did not take;
    MM-GBSA is not a core analysis, so without this a StepFailure would leave the
    suite ok:true. consistent None/absent (no MM-GBSA, or radii unverifiable)
    never fails core by itself."""
    if gb_radii is not None and gb_radii.get("consistent") is False:
        return False
    return core_ok


def resolve_bin(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    home = os.environ.get("AMBERHOME")
    if home:
        cand = Path(home) / "bin" / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


# ---- prmtop introspection (residue ranges) -------------------------------

STD_AA = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "HIE", "HID", "HIP", "CYX", "CYM", "ASH", "GLH", "LYN", "HISE", "HISD",
}


def prmtop_residue_labels(top_path: Path) -> list[str]:
    text = top_path.read_text()
    m = re.search(r"%FLAG RESIDUE_LABEL\s*\n%FORMAT\([^)]*\)\s*\n", text)
    if not m:
        return []
    rest = text[m.end():]
    end = rest.find("%FLAG")
    block = rest[:end] if end != -1 else rest
    # Fixed-width 20a4 but whitespace split is robust for resnames.
    return block.split()


def detect_masks(comp_dry_top: Path, has_ligand: bool) -> dict[str, Any]:
    labels = prmtop_residue_labels(comp_dry_top)
    nres = len(labels)
    if has_ligand:
        # combine{prot,lig} appends the ligand last.
        prot_last = nres - 1
        lig_res = nres
    else:
        prot_last = nres
        lig_res = None
    return {"nres": nres, "prot_last": prot_last, "lig_res": lig_res,
            "protein_mask": f":1-{prot_last}",
            "ligand_mask": f":{lig_res}" if lig_res else None,
            "solute_mask": f":1-{nres}"}


# ---- cpptraj / shell runner ----------------------------------------------

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


# ---- data helpers --------------------------------------------------------

def load_xy(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """Load a 2-column cpptraj .dat (frame value). Returns None if empty."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    rows = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split()
        if len(parts) >= 2:
            try:
                rows.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    if not rows:
        return None
    a = np.array(rows)
    return a[:, 0], a[:, 1]


def simple_line(dat: Path, png: Path, xlabel: str, ylabel: str, title: str,
                col: int = 1) -> bool:
    if not dat.exists() or dat.stat().st_size == 0:
        return False
    rows = []
    for ln in dat.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        p = ln.split()
        if len(p) > col:
            try:
                rows.append([float(p[0]), float(p[col])])
            except ValueError:
                continue
    if not rows:
        return False
    a = np.array(rows)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(a[:, 0], a[:, 1], lw=0.8)
    ax.set_xlabel(xlabel); ax.set_ylabel(ylabel); ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(png, dpi=150); plt.close(fig)
    return True


# ---- analyses ------------------------------------------------------------

def a_strip(ctx) -> None:
    """Preprocess: strip solvent with the SOLVATED topology, write dry strip.nc."""
    d = ctx["adir"]("strip")
    in_text = (
        f"parm {ctx['comp_oct_top']}\n"
        f"trajin {ctx['traj']}\n"
        f"strip :WAT,:Na+,:Cl-,:K+\n"
        f"autoimage\n"
        f"trajout strip.nc netcdf\n"
    )
    run_cpptraj(ctx["cpptraj"], in_text, d / "strip.in", d)
    sp = d / "strip.nc"
    if not sp.exists() or sp.stat().st_size == 0:
        raise StepFailure("strip produced no strip.nc")
    # Relative ref for sibling analysis subdirs (avoids space-in-path).
    ctx["strip_nc"] = "../strip/strip.nc"


def _base(ctx, name):
    """Common header for dry-topology analyses."""
    return f"parm {ctx['comp_dry_top']}\ntrajin {ctx['strip_nc']}\n"


def a_rmsd(ctx):
    d = ctx["adir"]("rmsd"); pm = ctx["masks"]["protein_mask"]
    txt = _base(ctx, "rmsd") + (
        f"rms first mass out rmsd_bb.dat {pm}@CA,C,N\n"
        f"rms first mass out rmsd_all.dat {pm}\n")
    run_cpptraj(ctx["cpptraj"], txt, d / "rmsd.in", d)
    ok1 = simple_line(d/"rmsd_bb.dat", d/"rmsd.png", "Frame", "RMSD (A)",
                      "Backbone RMSD")
    ctx["result"]("rmsd", d/"rmsd_bb.dat", d/"rmsd.png", ok1)


def a_rmsf(ctx):
    d = ctx["adir"]("rmsf"); pm = ctx["masks"]["protein_mask"]
    txt = _base(ctx, "rmsf") + f"atomicfluct out rmsf_byres.dat {pm} byres\n"
    run_cpptraj(ctx["cpptraj"], txt, d / "rmsf.in", d)
    ok = simple_line(d/"rmsf_byres.dat", d/"rmsf.png", "Residue", "RMSF (A)",
                     "Per-residue RMSF")
    ctx["result"]("rmsf", d/"rmsf_byres.dat", d/"rmsf.png", ok)


def a_rg(ctx):
    d = ctx["adir"]("rg"); pm = ctx["masks"]["protein_mask"]
    txt = _base(ctx, "rg") + f"radgyr out rg.dat {pm}\n"
    run_cpptraj(ctx["cpptraj"], txt, d / "rg.in", d)
    ok = simple_line(d/"rg.dat", d/"rg.png", "Frame", "Rg (A)",
                     "Radius of gyration")
    ctx["result"]("rg", d/"rg.dat", d/"rg.png", ok)


def a_sasa(ctx):
    d = ctx["adir"]("sasa"); pm = ctx["masks"]["protein_mask"]
    txt = _base(ctx, "sasa") + f"molsurf out sasa.dat {pm}\n"
    run_cpptraj(ctx["cpptraj"], txt, d / "sasa.in", d)
    ok = simple_line(d/"sasa.dat", d/"sasa.png", "Frame", "SASA (A^2)", "SASA")
    ctx["result"]("sasa", d/"sasa.dat", d/"sasa.png", ok)


def a_dssp(ctx):
    d = ctx["adir"]("dssp"); pm = ctx["masks"]["protein_mask"]
    txt = _base(ctx, "dssp") + (
        f"secstruct out dssp.dat {pm} sumout dssp_sum.dat\n")
    run_cpptraj(ctx["cpptraj"], txt, d / "dssp.in", d)
    # dssp_sum.dat: per-residue avg of each SS type. Plot a simple bar of total.
    ok = (d/"dssp.dat").exists() and (d/"dssp.dat").stat().st_size > 0
    if ok:
        try:
            _plot_dssp(d/"dssp_sum.dat", d/"dssp.png")
        except Exception:
            ok = simple_line(d/"dssp.dat", d/"dssp.png", "Frame", "SS code",
                             "Secondary structure")
    ctx["result"]("dssp", d/"dssp.dat", d/"dssp.png", ok)


def _plot_dssp(sumdat: Path, png: Path):
    data = np.genfromtxt(sumdat, comments="#")
    if data.ndim == 1:
        data = data.reshape(1, -1)
    res = data[:, 0]
    fig, ax = plt.subplots(figsize=(9, 4))
    for col, lbl in enumerate(["Para", "Anti", "3-10", "Alpha", "Pi", "Turn",
                               "Bend"], start=1):
        if col < data.shape[1]:
            ax.plot(res, data[:, col], label=lbl, lw=0.9)
    ax.set_xlabel("Residue"); ax.set_ylabel("Fraction"); ax.set_title("DSSP")
    ax.legend(fontsize=7, ncol=4); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(png, dpi=150); plt.close(fig)


def a_hbond(ctx):
    d = ctx["adir"]("hbond"); m = ctx["masks"]
    if m["ligand_mask"]:
        sel = f"{m['protein_mask']},{m['lig_res']}"
        txt = _base(ctx, "hbond") + (
            f"hbond HB out hbond.dat {sel} nointramol "
            f"avgout hbond_avg.dat\n")
    else:
        txt = _base(ctx, "hbond") + (
            f"hbond HB out hbond.dat {m['protein_mask']} "
            f"avgout hbond_avg.dat\n")
    run_cpptraj(ctx["cpptraj"], txt, d / "hbond.in", d)
    # "no data" is a valid finding (hydrophobic / pi-pi binding).
    xy = load_xy(d/"hbond.dat")
    note = None
    if xy is None:
        note = "no stable hbonds detected (hydrophobic/pi-pi binding likely)"
        ok = True
        # emit a placeholder figure so the gate (png exists) passes
        fig, ax = plt.subplots(figsize=(6, 2))
        ax.text(0.5, 0.5, "No stable H-bonds detected", ha="center", va="center")
        ax.axis("off"); fig.savefig(d/"hbond.png", dpi=120); plt.close(fig)
    else:
        ok = simple_line(d/"hbond.dat", d/"hbond.png", "Frame", "# H-bonds",
                         "H-bonds")
    ctx["result"]("hbond", d/"hbond.dat", d/"hbond.png", ok, note=note)


def a_distmat(ctx):
    d = ctx["adir"]("distmat"); pm = ctx["masks"]["protein_mask"]
    txt = _base(ctx, "distmat") + f"matrix dist {pm}@CA out distmat.dat byres\n"
    run_cpptraj(ctx["cpptraj"], txt, d / "distmat.in", d)
    ok = False
    if (d/"distmat.dat").exists() and (d/"distmat.dat").stat().st_size > 0:
        try:
            M = np.genfromtxt(d/"distmat.dat")
            fig, ax = plt.subplots(figsize=(6, 5))
            im = ax.imshow(M, cmap="viridis", origin="lower")
            plt.colorbar(im, ax=ax, label="Distance (A)")
            ax.set_title("Cα–Cα distance matrix")
            fig.tight_layout(); fig.savefig(d/"distmat.png", dpi=150); plt.close(fig)
            ok = (d/"distmat.png").exists()
        except Exception:
            ok = False
    ctx["result"]("distmat", d/"distmat.dat", d/"distmat.png", ok)


def a_cluster(ctx):
    d = ctx["adir"]("cluster"); pm = ctx["masks"]["protein_mask"]
    # repout MUST stay inside the single cluster command (steal #4).
    txt = _base(ctx, "cluster") + (
        f"rms first {pm}@CA\n"
        f"cluster C0 kmeans clusters 5 randompoint maxit 500 "
        f"rms {pm}@CA sieve 5 "
        f"out cnumvtime.dat summary summary.dat info info.dat "
        f"repout rep repfmt pdb\n")
    run_cpptraj(ctx["cpptraj"], txt, d / "cluster.in", d)
    ok = False
    if (d/"cnumvtime.dat").exists() and (d/"cnumvtime.dat").stat().st_size > 0:
        try:
            _plot_cluster(d/"cnumvtime.dat", d/"summary.dat", d/"cluster.png")
            ok = (d/"cluster.png").exists()
        except Exception:
            ok = simple_line(d/"cnumvtime.dat", d/"cluster.png", "Frame",
                             "Cluster", "Cluster vs time")
    ctx["result"]("cluster", d/"summary.dat", d/"cluster.png", ok)


def _plot_cluster(cnum: Path, summary: Path, png: Path):
    cn = load_xy(cnum)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    if cn is not None:
        axes[0].scatter(cn[0], cn[1], s=3)
        axes[0].set_xlabel("Frame"); axes[0].set_ylabel("Cluster #")
        axes[0].set_title("Cluster membership vs time")
    # summary.dat header begins with '#'; parse with explicit columns (steal #5).
    fracs = []
    if summary.exists():
        for ln in summary.read_text().splitlines():
            if ln.startswith("#") or not ln.strip():
                continue
            p = ln.split()
            if len(p) >= 3:
                try:
                    fracs.append((int(p[0]), float(p[2])))
                except ValueError:
                    continue
    if fracs:
        ids = [f[0] for f in fracs]; fr = [f[1] for f in fracs]
        axes[1].bar(ids, fr, color="steelblue")
        axes[1].set_xlabel("Cluster"); axes[1].set_ylabel("Fraction")
        axes[1].set_title("Cluster populations")
    fig.tight_layout(); fig.savefig(png, dpi=150); plt.close(fig)


def a_pca(ctx):
    """Two-call PCA: diagmatrix+run, then projection (steal #4)."""
    d = ctx["adir"]("pca"); pm = ctx["masks"]["protein_mask"]
    # Call 1: covariance + eigenvectors.
    c1 = _base(ctx, "pca") + (
        f"rms first {pm}@CA\n"
        f"matrix covar {pm}@CA name covar\n"
        f"diagmatrix covar out evecs.dat vecs 10 name evecs\n"
        f"run\n")
    run_cpptraj(ctx["cpptraj"], c1, d / "pca_evecs.in", d)
    # Call 2: project trajectory onto PC1/PC2 (reads evecs from file).
    c2 = _base(ctx, "pca") + (
        f"rms first {pm}@CA\n"
        f"readdata evecs.dat name evecs\n"
        f"projection PROJ modes evecs out proj.dat beg 1 end 2 {pm}@CA\n"
        f"run\n")
    run_cpptraj(ctx["cpptraj"], c2, d / "pca_proj.in", d)
    ok = False
    if (d/"proj.dat").exists() and (d/"proj.dat").stat().st_size > 0:
        try:
            _plot_pca(d/"proj.dat", d/"evecs.dat", d/"pca.png")
            ok = (d/"pca.png").exists()
        except Exception:
            ok = False
    ctx["result"]("pca", d/"proj.dat", d/"pca.png", ok)
    ctx["pca_proj"] = d/"proj.dat"


def _parse_evals(evecs: Path) -> np.ndarray:
    """Hand-parse eigenvalues from evecs.dat (NOT read_csv) — steal #5."""
    evals = []
    for ln in evecs.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("****") or ln.startswith(("Eigenvector",
                                                              "COVAR", "%")):
            continue
        p = ln.split()
        if len(p) == 2 and p[0].isdigit():
            try:
                evals.append(float(p[1]))
            except ValueError:
                continue
    return np.array(evals) if evals else np.array([1.0])


def _plot_pca(proj: Path, evecs: Path, png: Path):
    xy = []
    for ln in proj.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        p = ln.split()
        if len(p) >= 3:
            xy.append((float(p[1]), float(p[2])))
    a = np.array(xy)
    evals = _parse_evals(evecs)
    var = evals / evals.sum() * 100
    fig, ax = plt.subplots(figsize=(7, 5))
    sc = ax.scatter(a[:, 0], a[:, 1], c=range(len(a)), cmap="viridis", s=4)
    ax.set_xlabel(f"PC1 ({var[0]:.1f}%)")
    ax.set_ylabel(f"PC2 ({var[1]:.1f}%)" if len(var) > 1 else "PC2")
    plt.colorbar(sc, ax=ax, label="Frame"); ax.set_title("PCA (Cα)")
    fig.tight_layout(); fig.savefig(png, dpi=150); plt.close(fig)


def a_fel(ctx):
    """Free-energy landscape from the PCA projection."""
    d = ctx["adir"]("fel")
    proj = ctx.get("pca_proj")
    if proj is None or not Path(proj).exists():
        ctx["result"]("fel", None, None, False,
                      note="needs pca (proj.dat) first")
        return
    xy = []
    for ln in Path(proj).read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        p = ln.split()
        if len(p) >= 3:
            xy.append((float(p[1]), float(p[2])))
    a = np.array(xy)
    H, xe, ye = np.histogram2d(a[:, 0], a[:, 1], bins=40)
    P = H / H.sum()
    with np.errstate(divide="ignore"):
        G = -0.001987 * 300 * np.log(P / P.max())
    G[~np.isfinite(G)] = G[np.isfinite(G)].max() if np.isfinite(G).any() else 0
    fig, ax = plt.subplots(figsize=(7, 5))
    cf = ax.contourf(xe[:-1], ye[:-1], G.T, levels=20, cmap="viridis")
    plt.colorbar(cf, ax=ax, label="dG (kcal/mol)")
    ax.set_xlabel("PC1"); ax.set_ylabel("PC2")
    ax.set_title("Free energy landscape (300 K)")
    fig.tight_layout(); fig.savefig(d/"fel.png", dpi=150); plt.close(fig)
    np.savetxt(d/"fel.dat", G)
    ctx["result"]("fel", d/"fel.dat", d/"fel.png", (d/"fel.png").exists())


def a_thermo(ctx):
    d = ctx["adir"]("thermo")
    mdout = ctx.get("mdout_dir")
    perl = shutil.which("perl") or "perl"
    script = SCRIPT_DIR / "process_mdout.perl"
    outs = []
    if mdout:
        for f in ("heat.out", "density.out", "product.out"):
            p = Path(mdout) / f
            if p.exists():
                shutil.copy(p, d / f)
                outs.append(f)
    if not outs or not script.exists():
        ctx["result"]("thermo", None, None, False,
                      note="no mdout files or process_mdout.perl missing")
        return
    subprocess.run([perl, str(script)] + outs, cwd=str(d),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Plot whatever summary.* were produced (TEMP, DENSITY, ETOT).
    plotted = []
    for prop in ("TEMP", "DENSITY", "ETOT", "EPTOT"):
        sm = d / f"summary.{prop}"
        if sm.exists() and sm.stat().st_size > 0:
            # summary.DENSITY can be 1-col (steal #5).
            arr = np.genfromtxt(sm)
            if arr.ndim == 1:
                y = arr; x = np.arange(len(y))
            else:
                x, y = arr[:, 0], arr[:, 1]
            fig, ax = plt.subplots(figsize=(8, 3))
            ax.plot(x, y, lw=0.8); ax.set_title(prop); ax.set_xlabel("step")
            ax.grid(True, alpha=0.3)
            fig.tight_layout(); fig.savefig(d/f"thermo_{prop}.png", dpi=130)
            plt.close(fig); plotted.append(prop)
    ok = bool(plotted)
    png = d / f"thermo_{plotted[0]}.png" if plotted else None
    ctx["result"]("thermo", d/"summary.TEMP", png, ok,
                  note=f"plotted: {plotted}")


def a_mmgbsa(ctx):
    d = ctx["adir"]("mmgbsa")
    if not (ctx.get("protein_top") and ctx.get("ligand_top")):
        ctx["result"]("mmgbsa", None, None, False,
                      note="protein-only system: no binding free energy")
        return
    mmpbsa = resolve_bin("MMPBSA.py")
    if not mmpbsa:
        ctx["result"]("mmgbsa", None, None, False, note="MMPBSA.py not found")
        return
    out_dir = ctx["out_dir"]

    # --- GB-radii fix (Amber Table 4.1) -----------------------------------
    # tleap builds the dry component tops (-cp/-rp/-lp) with default mbondi, but
    # igb=MMGBSA_IGB is parameterized for TARGET_RADIUS_SET. Retype ONLY the radius
    # set on each dry top with parmed (changeRadii rewrites the RADII array AND the
    # RADIUS_SET descriptor). comp_oct (-sp) is solvent-strip-only — its radii never
    # enter the GB term — so it is left untouched. parmed runs in the mmgbsa subdir
    # and refs the out_dir tops as ../name (relative => space-safe), with stdout
    # redirected to a log so its splash text can't corrupt the JSON envelope.
    parmed = resolve_bin("parmed")
    fixed: dict[str, str] = {}
    fix_error: str | None = None
    if parmed is None:
        fix_error = (f"GB_RADII_FIX_FAILED: parmed not found — cannot retype GB "
                     f"radii to {TARGET_RADIUS_SET} (igb={MMGBSA_IGB}, "
                     "Amber Table 4.1)")
    else:
        for key, src in (("comp_dry", "comp_dry.top"), ("protein", "protein.top"),
                         ("ligand", "ligand.top")):
            out_name = f"{key}_{TARGET_RADIUS_SET}.top"
            sp = d / f"parmed_{key}.in"
            sp.write_text(parmed_radii_script(f"../{out_name}"))
            with (d / f"parmed_{key}.log").open("w") as lf:
                proc = subprocess.run([parmed, "-p", f"../{src}", "-i", sp.name],
                                      cwd=str(d), stdout=lf,
                                      stderr=subprocess.STDOUT)
            top = out_dir / out_name
            if proc.returncode != 0 or not top.is_file():
                fix_error = (f"GB_RADII_FIX_FAILED: parmed produced no {out_name} "
                             f"(rc={proc.returncode}; see parmed_{key}.log)")
                break
            if prmtop_radius_set(top) != TARGET_RADIUS_SET:
                fix_error = (f"GB_RADII_FIX_FAILED: {out_name} RADIUS_SET reads "
                             f"'{prmtop_radius_set(top)}', want '{TARGET_RADIUS_SET}'")
                break
            fixed[key] = f"../{out_name}"

    # Record consistency for the fatal gate in main(): check the FIXED comp_dry on
    # success (-> consistent True by construction), else the UNFIXED comp_dry so a
    # real mismatch persists into ctx and reds the suite (see suite_ok).
    check_top = (out_dir / f"comp_dry_{TARGET_RADIUS_SET}.top"
                 if fix_error is None else out_dir / "comp_dry.top")
    gb_radii = gb_radii_check(prmtop_radius_set(check_top), MMGBSA_IGB)
    ctx["gb_radii"] = gb_radii
    if fix_error is not None:
        # Fix attempted and FAILED -> the radii are NOT verified mbondi2. Force a
        # mismatch record so the suite reds (suite_ok + the emit FATAL append) even
        # if the unfixed top has no readable RADIUS_SET (consistent would otherwise
        # be None and slip the gate). The StepFailure is caught in main(), which
        # records mmgbsa failed; refuse to compute a dG under mismatched radii.
        gb_radii["consistent"] = False
        gb_radii.setdefault("finding", fix_error)
        raise StepFailure(fix_error)
    if gb_radii.get("finding"):
        print(f"[mmgbsa] {gb_radii['finding']}", file=sys.stderr)

    nframes = ctx.get("nframes", 0)
    end = max(nframes, 1)
    intval = 1 if nframes <= 100 else max(1, nframes // 100)
    mmin = (
        "MM-GBSA binding free energy\n"
        "&general\n"
        f"  startframe=1, endframe={end}, interval={intval}, verbose=1,\n"
        "/\n"
        "&gb\n"
        f"  igb={MMGBSA_IGB}, saltcon=0.100,\n"
        "/\n")
    (d / "mmgbsa.in").write_text(mmin)
    cmd = [mmpbsa, "-O", "-i", "mmgbsa.in", "-o", "mmgbsa.dat",
           "-sp", ctx["comp_oct_top"], "-cp", fixed["comp_dry"],
           "-rp", fixed["protein"], "-lp", fixed["ligand"],
           "-y", ctx["traj"]]
    log = d / "mmgbsa.log"
    with log.open("w") as lf:
        subprocess.run(cmd, cwd=str(d), stdout=lf, stderr=subprocess.STDOUT)
    dG = _parse_mmgbsa(d / "mmgbsa.dat")
    ok = dG is not None
    if ok:
        fig, ax = plt.subplots(figsize=(5, 4))
        ax.bar(["ΔG_bind"], [dG], color="indianred")
        ax.axhline(0, color="k", lw=0.6)
        ax.set_ylabel("kcal/mol")
        ax.set_title(f"MM-GBSA ΔG = {dG:.2f} kcal/mol")
        fig.tight_layout(); fig.savefig(d/"mmgbsa.png", dpi=150); plt.close(fig)
    note = f"dG={dG:.2f} kcal/mol" if ok else "parse failed"
    note += f"; GB radii {TARGET_RADIUS_SET} (igb={MMGBSA_IGB})"
    ctx["result"]("mmgbsa", d/"mmgbsa.dat", d/"mmgbsa.png", ok, note=note)
    if ok:
        ctx["mmgbsa_dG"] = dG


def _parse_mmgbsa(dat: Path) -> float | None:
    if not dat.exists():
        return None
    text = dat.read_text(errors="replace")
    # "DELTA TOTAL   <avg>  <stddev> ..." in the differences block.
    m = re.search(r"DELTA TOTAL\s+(-?\d+\.\d+)", text)
    if m:
        return float(m.group(1))
    return None


DISPATCH = {
    "rmsd": a_rmsd, "rmsf": a_rmsf, "rg": a_rg, "sasa": a_sasa, "dssp": a_dssp,
    "hbond": a_hbond, "distmat": a_distmat, "cluster": a_cluster, "pca": a_pca,
    "fel": a_fel, "thermo": a_thermo, "mmgbsa": a_mmgbsa,
}


# ---- Main ----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(prog=SKILL_NAME,
                                description="Full post-MD analysis suite.")
    p.add_argument("--comp-oct-top", required=True, help="Solvated topology.")
    p.add_argument("--comp-dry-top", required=True, help="Dry complex topology.")
    p.add_argument("--traj", required=True, help="Production trajectory (.nc).")
    p.add_argument("--protein-top", default=None, help="MM-GBSA receptor top.")
    p.add_argument("--ligand-top", default=None, help="MM-GBSA ligand top.")
    p.add_argument("--mdout-dir", default=None, help="Dir with heat/density/product .out for thermo.")
    p.add_argument("--analyses", default="all",
                   help="Comma list or 'all'.")
    p.add_argument("--output-dir", default="./analysis")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cpptraj = resolve_bin("cpptraj")
    if cpptraj is None and not args.dry_run:
        emit_and_exit(ok=False, dry_run=False,
                      errors=["MISSING_BINARY: cpptraj not found"], code=1)

    requested = (ALL_ANALYSES if args.analyses == "all"
                 else [a.strip() for a in args.analyses.split(",")])
    # PCA must precede FEL.
    if "fel" in requested and "pca" not in requested:
        requested = ["pca"] + requested

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    has_ligand = bool(args.protein_top and args.ligand_top)
    # detect_masks reads the file with Python (handles spaces) — use absolute.
    masks = detect_masks(Path(args.comp_dry_top).expanduser(), has_ligand)

    if args.dry_run:
        emit_and_exit(ok=True, dry_run=True,
                      outputs={"analysis_dir": str(out_dir),
                               "requested": requested},
                      validation={"masks": masks}, errors=[], code=0)

    # Validate inputs before any filesystem work so missing files fail cleanly.
    missing = []
    for label, val in (("--comp-oct-top", args.comp_oct_top),
                       ("--comp-dry-top", args.comp_dry_top),
                       ("--traj", args.traj)):
        if not Path(val).expanduser().is_file():
            missing.append(f"INVALID_INPUT: {label} not found: {val}")
    if has_ligand:
        for label, val in (("--protein-top", args.protein_top),
                           ("--ligand-top", args.ligand_top)):
            if not Path(val).expanduser().is_file():
                missing.append(f"INVALID_INPUT: {label} not found: {val}")
    if missing:
        emit_and_exit(ok=False, dry_run=False, errors=missing, code=1)

    # cpptraj/MMPBSA tokenize input-file lines on whitespace, so any absolute
    # path containing a space (".../Single Particle/...") breaks. Copy all inputs
    # into the analysis dir under bare names and reference them RELATIVELY from
    # each per-analysis subdir ("../name") — relative paths carry no spaces.
    shutil.copy(Path(args.comp_oct_top).expanduser(), out_dir / "comp_oct.top")
    shutil.copy(Path(args.comp_dry_top).expanduser(), out_dir / "comp_dry.top")
    shutil.copy(Path(args.traj).expanduser(), out_dir / "traj.nc")
    if has_ligand:
        shutil.copy(Path(args.protein_top).expanduser(), out_dir / "protein.top")
        shutil.copy(Path(args.ligand_top).expanduser(), out_dir / "ligand.top")

    # frame count for MM-GBSA windowing (run in out_dir with bare names).
    nframes = 0
    try:
        tl = subprocess.run([cpptraj, "-p", "comp_oct.top", "-y", "traj.nc",
                             "-tl"], cwd=str(out_dir),
                            capture_output=True, text=True)
        mt = re.search(r"Frames:\s*(\d+)", tl.stdout + tl.stderr)
        if mt:
            nframes = int(mt.group(1))
    except Exception:
        pass

    results: dict[str, Any] = {}
    errors: list[str] = []

    def adir(name: str) -> Path:
        dd = out_dir / name
        dd.mkdir(parents=True, exist_ok=True)
        return dd

    def result(name, dat, png, ok, note=None):
        results[name] = {"ok": bool(ok),
                         "dat": str(dat) if dat else None,
                         "png": str(png) if png else None}
        if note:
            results[name]["note"] = note

    # cpptraj/MMPBSA refs are RELATIVE from a per-analysis subdir (no spaces);
    # mdout_dir is read by Python (shutil.copy), so it stays absolute.
    ctx = {
        "cpptraj": cpptraj,
        "comp_oct_top": "../comp_oct.top",
        "comp_dry_top": "../comp_dry.top",
        "traj": "../traj.nc",
        "protein_top": "../protein.top" if has_ligand else None,
        "ligand_top": "../ligand.top" if has_ligand else None,
        "mdout_dir": str(Path(args.mdout_dir).expanduser().resolve()) if args.mdout_dir else None,
        "out_dir": out_dir,
        "masks": masks, "nframes": nframes,
        "adir": adir, "result": result,
    }

    # Preprocess first (all dry-topology analyses depend on strip.nc).
    needs_strip = any(a in requested for a in
                      ["rmsd", "rmsf", "rg", "sasa", "dssp", "hbond",
                       "distmat", "cluster", "pca", "fel"])
    if needs_strip:
        try:
            a_strip(ctx)
        except StepFailure as e:
            emit_and_exit(ok=False, dry_run=False,
                          errors=[f"STRIP_FAILED: {e}"], code=2)

    for name in requested:
        fn = DISPATCH.get(name)
        if fn is None:
            errors.append(f"UNKNOWN_ANALYSIS: {name}")
            continue
        try:
            fn(ctx)
        except StepFailure as e:
            results[name] = {"ok": False, "error": str(e)}
            errors.append(f"{name}: {e}")
        except Exception as e:  # noqa: BLE001 — keep the suite going
            results[name] = {"ok": False, "error": repr(e)}
            errors.append(f"{name}: {e!r}")

    produced = [n for n, r in results.items() if r.get("ok")]
    failed = [n for n, r in results.items() if not r.get("ok")]

    outputs = {"analysis_dir": str(out_dir), "analyses": results,
               "produced": produced, "failed": failed}
    if "mmgbsa_dG" in ctx:
        outputs["mmgbsa_dG_kcal_mol"] = ctx["mmgbsa_dG"]

    # Suite is ok if every REQUESTED core analysis produced output; individual
    # optional failures / not-applicable skips are reported but not fatal. A
    # SURVIVING GB-radii mismatch is the exception — it is fatal (suite_ok): the
    # mbondi2 fix was missing or did not take, so MM-GBSA ran (or would run) under
    # mismatched radii. MM-GBSA is not core, so StepFailure alone wouldn't red it.
    core = {"rmsd", "rmsf", "rg"} & set(requested)
    core_ok = core.issubset(set(produced))
    gbr = ctx.get("gb_radii")
    final_ok = suite_ok(core_ok, gbr)
    if gbr is not None and gbr.get("consistent") is False and gbr.get("finding"):
        errors.append("FATAL " + gbr["finding"])
    emit_and_exit(ok=final_ok, dry_run=False, outputs=outputs,
                  validation={"produced": produced, "failed": failed,
                              "nframes": nframes, "gb_radii": gbr},
                  errors=errors, code=0 if final_ok else 3)


if __name__ == "__main__":
    # A deterministic wrapper must NEVER crash without an envelope (thesis clause f).
    # detect_masks() reads --comp-dry-top before the input-existence gate, so a
    # missing/unreadable topology would otherwise raise a bare traceback to an
    # orchestrator. Last-resort guard -> graceful ok:false (mirrors md-planner).
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        emit_and_exit(ok=False, dry_run=False,
                      errors=[f"INVALID_INPUT: {type(e).__name__}: {e}"], code=1)

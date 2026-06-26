#!/usr/bin/env bash
## Golden-path end-to-end: a REAL protein-ligand complex through the full
## Project Prime pipeline, locally with sander.
##
##   fetch 181L (T4 lysozyme L99A + benzene)
##     -> pdb4amber (clean protein)
##     -> obabel/antechamber/parmchk2 (parameterize ligand, GAFF2 + AM1-BCC)
##     -> tleap (combine + solvate TIP3P + neutralize)
##     -> sander (minimize -> heat NVT -> equilibrate NPT -> produce)
##     -> cpptraj (RMSD + RMSF + ligand RMSD + export complex frame)
##     -> PLIP (non-covalent interaction fingerprint)
##
## Run after `conda activate prime-amber`.
##
## HPC SWAP SEAM: every MD call goes through run_md() using $ENGINE (default
## `sander`). To run on a cluster later: set ENGINE=pmemd.cuda and wrap run_md
## in a DPDispatcher SSHContext + Slurm/PBS submission. The recipe files
## (*.leap, *.in, *.cpptraj) do not change.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
START_TS=$(date +%s)

ENGINE="${ENGINE:-sander}"     ## override to pmemd / pmemd.cuda on HPC
PDBID="181L"                   ## T4 lysozyme L99A + benzene (BNZ in the cavity)

require_engine() {             ## hard-fail early if the MD engine is not on PATH
  command -v "$ENGINE" >/dev/null 2>&1 \
    || { echo "ERROR: MD engine '$ENGINE' not found on PATH" >&2; exit 1; }
}

## ---- Pre-flight ------------------------------------------------------------
if [ -z "${CONDA_PREFIX:-}" ] || [ "$(basename "${CONDA_PREFIX}")" != "prime-amber" ]; then
  echo "ERROR: conda env 'prime-amber' is not active. Run: conda activate prime-amber" >&2
  exit 1
fi
require_engine
echo "env:    $CONDA_PREFIX"
echo "engine: $ENGINE ($(command -v "$ENGINE"))"
echo

assert_no_nan() {
  local out="$1"
  ## Non-finite energies: gfortran prints NaN / Infinity literally, and overflows
  ## a fixed-width field as ******. Catch all three (matches amber-recover's detector).
  if grep -q -E "NaN|[-+]?Infinity\b|\*\*\*\*\*\*" "$out"; then
    echo "FAIL: $out contains NaN / Infinity / overflow (********)." >&2
    grep -n -E "NaN|[-+]?Infinity\b|\*\*\*\*\*\*" "$out" | head -5 >&2
    exit 1
  fi
}

assert_tleap_ok() {
  local log="$1"
  if grep -E "FATAL|Could not|^Error" "$log"; then
    echo "FAIL: tleap reported a hard error (see $log)." >&2
    exit 1
  fi
}

## Physical-realism gate: "no NaN" is not enough — the production run must have
## actually held its target temperature. Pull the AVERAGES-block TEMP and bound it.
assert_prod_temp() {
  local out="$1" lo=285 hi=315
  local t
  t=$(awk '/A V E R A G E S/{a=1} a&&/TEMP\(K\)/{for(i=1;i<=NF;i++) if($i=="TEMP(K)"){print $(i+2); exit}}' "$out")
  [ -n "$t" ] || { echo "FAIL: could not read average TEMP from $out" >&2; exit 1; }
  awk -v t="$t" -v lo=$lo -v hi=$hi 'BEGIN{exit !(t>=lo && t<=hi)}' \
    || { echo "FAIL: $out average TEMP=${t}K outside ${lo}-${hi}K (thermostat/dynamics problem)" >&2; exit 1; }
  echo "  production average TEMP = ${t} K (within ${lo}-${hi} K)"
}

## run_md <label> <mdin> <in_coord> <out> <rst> [traj] [ref]
run_md() {
  local label="$1" mdin="$2" inc="$3" out="$4" rst="$5" traj="${6:-}" ref="${7:-}"
  local args=(-O -i "$mdin" -o "$out" -p system.prmtop -c "$inc" -r "$rst")
  [ -n "$traj" ] && args+=(-x "$traj")
  [ -n "$ref" ]  && args+=(-ref "$ref")
  echo "[$label] $ENGINE -i $mdin"
  "$ENGINE" "${args[@]}"
  assert_no_nan "$out"
}

## extract_ligand <pdb> <resname> <outfile> — pull a ligand's HETATM records by
## resname (cols 18-20), tolerant of a PDB altLoc indicator in col 17; fire a
## friendly error (not a silent pipefail abort) when the resname is absent.
extract_ligand() {
  local pdb="$1" rn="$2" out="$3"
  awk -v rn="$rn" '
    substr($0,1,6)=="HETATM" { x=substr($0,18,3); gsub(/ /,"",x); if (x==rn) print }
  ' "$pdb" > "$out"
  if [ ! -s "$out" ]; then
    echo "FAIL: ligand '$rn' not found in $pdb" >&2
    return 1
  fi
  printf 'END\n' >> "$out"
}

## ---- Stage 1: fetch + clean protein, extract ligand ------------------------
echo "== Stage 1: fetch $PDBID + clean =="
if [ ! -s "${PDBID}.pdb" ]; then
  curl -sS -f -o "${PDBID}.pdb" "https://files.rcsb.org/download/${PDBID}.pdb" \
    || { echo "FAIL: could not download ${PDBID}.pdb"; exit 1; }
fi
grep -E "^ATOM" "${PDBID}.pdb" > protein_raw.pdb; printf "TER\nEND\n" >> protein_raw.pdb
extract_ligand "${PDBID}.pdb" BNZ ligand_raw.pdb
pdb4amber -i protein_raw.pdb -o protein_clean.pdb > pdb4amber.log 2>&1
test -s protein_clean.pdb || { echo "FAIL: pdb4amber produced no protein_clean.pdb"; cat pdb4amber.log; exit 1; }

## ---- Stage 2: parameterize the ligand --------------------------------------
echo "== Stage 2: ligand parameterization (GAFF2 + AM1-BCC) =="
obabel ligand_raw.pdb -O ligand_h.mol2 -h > obabel.log 2>&1
test -s ligand_h.mol2 || { echo "FAIL: obabel did not add hydrogens"; cat obabel.log; exit 1; }
antechamber -i ligand_h.mol2 -fi mol2 -o ligand_gaff2.mol2 -fo mol2 \
            -c bcc -at gaff2 -nc 0 -rn BNZ > antechamber.log 2>&1
test -s ligand_gaff2.mol2 || { echo "FAIL: antechamber failed"; tail -30 antechamber.log; exit 1; }
parmchk2 -i ligand_gaff2.mol2 -f mol2 -o ligand.frcmod -s gaff2 > parmchk2.log 2>&1
test -s ligand.frcmod || { echo "FAIL: parmchk2 produced no frcmod"; cat parmchk2.log; exit 1; }

## ---- Stage 3: build, solvate, neutralize -----------------------------------
echo "== Stage 3: tleap build + solvate + neutralize =="
tleap -f build.leap > tleap.log 2>&1
assert_tleap_ok tleap.log
test -s system.prmtop && test -s system.inpcrd || { echo "FAIL: system.prmtop/inpcrd missing"; exit 1; }

## ---- Stage 4: MD (minimize -> heat -> equilibrate -> produce) --------------
echo "== Stage 4: MD ($ENGINE) =="
run_md "min  " min.in   system.inpcrd min.out   min.rst   ""        system.inpcrd
run_md "heat " heat.in  min.rst       heat.out  heat.rst  heat.nc   min.rst
run_md "equil" equil.in heat.rst      equil.out equil.rst equil.nc  heat.rst
run_md "prod " prod.in  equil.rst     prod.out  prod.rst  prod.nc
test -s prod.nc || { echo "FAIL: prod.nc empty"; exit 1; }
assert_prod_temp prod.out

## ---- Stage 5: analysis -----------------------------------------------------
echo "== Stage 5: cpptraj (RMSD / RMSF / ligand RMSD / frame export) =="
cpptraj -i analyze.cpptraj > cpptraj.log 2>&1
for f in rmsd_backbone.dat rmsf.dat rmsd_ligand.dat complex_frame.pdb; do
  test -s "$f" || { echo "FAIL: $f missing"; tail -30 cpptraj.log; exit 1; }
done
## analyze.cpptraj normalizes AMBER protonation-variant resnames (HIE/CYX/...) -> standard
## PDB names so PLIP classifies them as protein, not phantom ligands. Verify it took.
! grep -qE " (HIE|HID|HIP|CYX|LYN|ASH|GLH) " complex_frame.pdb \
  || { echo "FAIL: non-standard AMBER resnames leaked into the PLIP input"; exit 1; }

## ---- Stage 6: PLIP ---------------------------------------------------------
echo "== Stage 6: PLIP interaction analysis =="
rm -rf plip_out && mkdir -p plip_out
## PLIP detects the ligand by its non-standard residue name (BNZ); ATOM/HETATM is irrelevant.
plip -f complex_frame.pdb -t -x -o plip_out > plip.log 2>&1 || { echo "FAIL: plip errored"; tail -30 plip.log; exit 1; }
PLIP_REPORT="$(ls plip_out/*.txt 2>/dev/null | head -1 || true)"
test -n "$PLIP_REPORT" || { echo "FAIL: PLIP produced no report"; cat plip.log; exit 1; }
## The benzene ligand must be detected, and exactly the expected ligands (only BNZ).
grep -q "BNZ.* SMALLMOLECULE" "$PLIP_REPORT" || { echo "FAIL: PLIP did not detect the BNZ ligand"; exit 1; }
PHANTOM=$(grep -cE "(HIS|CYS|LYS|ASP|GLU|ARG|PHE|TYR|TRP).* SMALLMOLECULE" "$PLIP_REPORT" || true)
[ "$PHANTOM" -eq 0 ] || { echo "FAIL: PLIP misclassified $PHANTOM protein residue(s) as ligands"; exit 1; }

## ---- Summary ---------------------------------------------------------------
TOTAL_DT=$(( $(date +%s) - START_TS ))
echo
echo "============================================================"
echo " GOLDEN PATH PASSED — real protein-ligand complex, end to end (${TOTAL_DT}s)"
echo "============================================================"
echo "system:        $(awk '/%FLAG POINTERS/{getline;getline;print $1" atoms"; exit}' system.prmtop)"
echo "trajectories:  $(ls -1 heat.nc equil.nc prod.nc 2>/dev/null | tr '\n' ' ')"
echo "metrics:       rmsd_backbone.dat  rmsf.dat  rmsd_ligand.dat"
echo "PLIP report:   $PLIP_REPORT"

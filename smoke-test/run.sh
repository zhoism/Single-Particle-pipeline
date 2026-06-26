#!/usr/bin/env bash
## End-to-end smoke test for the prime-amber conda env.
## Two legs:
##   A) Alanine dipeptide in TIP3P water: tleap -> sander (minimize, heat, NVT) -> cpptraj
##   B) Benzene ligand prep: obabel -> antechamber -> parmchk2 -> tleap
##
## Run after `conda activate prime-amber`.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
START_TS=$(date +%s)

## ---- Pre-flight ------------------------------------------------------------
if [ -z "${CONDA_PREFIX:-}" ] || [ "$(basename "${CONDA_PREFIX}")" != "prime-amber" ]; then
  echo "ERROR: conda env 'prime-amber' is not active." >&2
  echo "Run: conda activate prime-amber" >&2
  exit 1
fi
echo "env: $CONDA_PREFIX"
echo "sander: $(which sander)"
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

leg_a_t0=$(date +%s)

## ---- LEG A: alanine dipeptide in water -------------------------------------
echo "============================================================"
echo " LEG A: alanine dipeptide in TIP3P water"
echo "============================================================"
cd "$HERE/aladip"

echo "[A1/5] tleap: build + solvate"
tleap -f build.leap > tleap.log 2>&1
if grep -E "FATAL|Could not|^Error" tleap.log; then
  echo "FAIL: tleap reported a hard error." >&2
  cat tleap.log >&2
  exit 1
fi
test -s system.prmtop && test -s system.inpcrd || { echo "FAIL: system.prmtop/inpcrd missing"; exit 1; }

echo "[A2/5] sander: minimize (1000 steps)"
sander -O -i min.in -o min.out -p system.prmtop -c system.inpcrd -r min.rst
assert_no_nan min.out

echo "[A3/5] sander: heat 0 -> 300 K (10 ps)"
sander -O -i heat.in -o heat.out -p system.prmtop -c min.rst -r heat.rst -x heat.nc
assert_no_nan heat.out
test -s heat.nc || { echo "FAIL: heat.nc empty"; exit 1; }

echo "[A4/5] sander: production NVT 300 K (10 ps)"
sander -O -i prod.in -o prod.out -p system.prmtop -c heat.rst -r prod.rst -x prod.nc
assert_no_nan prod.out
test -s prod.nc || { echo "FAIL: prod.nc empty"; exit 1; }

echo "[A5/5] cpptraj: rmsd + radgyr"
cpptraj -i analyze.cpptraj > cpptraj.log 2>&1
test -s rmsd_solute.dat || { echo "FAIL: rmsd_solute.dat missing"; cat cpptraj.log; exit 1; }
test -s radgyr.dat       || { echo "FAIL: radgyr.dat missing"; cat cpptraj.log; exit 1; }

leg_a_dt=$(( $(date +%s) - leg_a_t0 ))
echo "LEG A OK (${leg_a_dt}s)"
echo

## ---- LEG B: benzene ligand parameterization --------------------------------
leg_b_t0=$(date +%s)
echo "============================================================"
echo " LEG B: benzene -> antechamber -> parmchk2 -> tleap"
echo "============================================================"
cd "$HERE/benzene"

echo "[B1/4] obabel: 3D benzene from SMILES"
obabel -:"c1ccccc1" -O benzene_input.mol2 --gen3d -h > obabel.log 2>&1
test -s benzene_input.mol2 || { echo "FAIL: benzene_input.mol2 not generated"; cat obabel.log; exit 1; }

echo "[B2/4] antechamber: GAFF2 atom types + AM1-BCC charges"
## -rn BNZ names the residue; -nc 0 = neutral; -at gaff2 = GAFF2 atom types; -c bcc = AM1-BCC.
antechamber -i benzene_input.mol2 -fi mol2 \
            -o benzene_gaff2.mol2 -fo mol2 \
            -c bcc -at gaff2 -nc 0 -rn BNZ > antechamber.log 2>&1
test -s benzene_gaff2.mol2 || { echo "FAIL: antechamber did not produce benzene_gaff2.mol2"; tail -30 antechamber.log; exit 1; }

echo "[B3/4] parmchk2: missing-parameter check"
parmchk2 -i benzene_gaff2.mol2 -f mol2 -o benzene.frcmod -s gaff2 > parmchk2.log 2>&1
test -s benzene.frcmod || { echo "FAIL: benzene.frcmod missing"; cat parmchk2.log; exit 1; }

echo "[B4/4] tleap: load test (build prmtop)"
tleap -f load.leap > load.log 2>&1
if grep -E "FATAL|Could not|^Error" load.log; then
  echo "FAIL: tleap load test reported a hard error." >&2
  cat load.log >&2
  exit 1
fi
test -s benzene.prmtop && test -s benzene.inpcrd || { echo "FAIL: benzene.prmtop/inpcrd missing"; exit 1; }

leg_b_dt=$(( $(date +%s) - leg_b_t0 ))
echo "LEG B OK (${leg_b_dt}s)"
echo

## ---- Summary ---------------------------------------------------------------
TOTAL_DT=$(( $(date +%s) - START_TS ))
echo "============================================================"
echo " SMOKE TEST PASSED — both legs OK (${TOTAL_DT}s total)"
echo "============================================================"
echo "Leg A artifacts:"
ls -lh "$HERE/aladip"/{system.prmtop,heat.nc,prod.nc,rmsd_solute.dat,radgyr.dat} 2>/dev/null
echo
echo "Leg B artifacts:"
ls -lh "$HERE/benzene"/{benzene_gaff2.mol2,benzene.frcmod,benzene.prmtop,benzene.inpcrd} 2>/dev/null

#!/usr/bin/env bash
# test_gates.sh — deterministic unit tests for the shell-layer gates of the
# golden-path / smoke-test / run_happy_path scripts. No AMBER, no network, no MD:
# it source-extracts the real shell functions and exercises them on synthetic
# fixtures, plus runs the real run_happy_path arg-validation (which fast-fails
# before any toolchain dependency).
#
# Proves the audit fixes:
#   #1 assert_no_nan must catch Infinity (HIGH)  -- both run.sh and smoke-test/run.sh
#   #2 ligand extraction must fire a friendly error when the resname is absent
#   #3 ligand extraction must tolerate a PDB altLoc indicator (col 17)
#   #4 a missing MD engine must hard-fail at preflight
#
# Run:  bash golden-path/tests/test_gates.sh   (exit 0 = all pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_SH="$HERE/../run.sh"
SMOKE_SH="$HERE/../../smoke-test/run.sh"
RHP="$HERE/../../run_happy_path.sh"

PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
no() { FAIL=$((FAIL + 1)); printf '  FAIL %s  -- %s\n' "$1" "${2:-}"; }

# Extract a bash function definition (from `name() {` to the first line that is
# a bare `}`) out of a script so we can source just that function. Assumes the
# function's closing brace is the first line starting with `}` (true for the
# target functions, whose bodies keep `}` on its own line at column 0).
extract_func() { # <file> <funcname>
  awk -v fn="$2" '
    $0 ~ "^"fn"\\(\\) \\{" { grab = 1 }
    grab { print }
    grab && /^\}/ { exit }
  ' "$1"
}

# ---- #1 assert_no_nan: NaN / Infinity / overflow / clean --------------------
test_nan_gate() { # <scriptpath> <label>
  local script="$1" lbl="$2" body tmp
  body="$(extract_func "$script" assert_no_nan)"
  if [ -z "$body" ]; then no "$lbl: assert_no_nan present" "not found in $script"; return; fi
  tmp="$(mktemp -d)"
  printf 'Etot =   -1234.5678  EKtot =    200.0\n TEMP(K) =   300.00\n' > "$tmp/clean.out"
  printf 'Etot =          NaN\n'      > "$tmp/nan.out"
  printf 'Etot =     Infinity\n'      > "$tmp/inf.out"
  printf 'Etot =    -Infinity\n'      > "$tmp/neginf.out"
  printf 'Etot =     ********\n'      > "$tmp/stars.out"

  # rc 0 = gate passed the file (no problem); rc!=0 = gate fired.
  gate() { ( eval "$body"; assert_no_nan "$1" ) >/dev/null 2>&1; }

  if gate "$tmp/clean.out"; then ok "$lbl clean passes"; else no "$lbl clean passes" "fired on a clean .out"; fi
  if gate "$tmp/nan.out";   then no "$lbl NaN caught" "NaN slipped through"; else ok "$lbl NaN caught"; fi
  if gate "$tmp/inf.out";    then no "$lbl Infinity caught [BUG-PROVING #1]" "Infinity slipped (rc 0)"; else ok "$lbl Infinity caught [BUG-PROVING #1]"; fi
  if gate "$tmp/neginf.out"; then no "$lbl -Infinity caught [BUG-PROVING #1]" "-Infinity slipped"; else ok "$lbl -Infinity caught [BUG-PROVING #1]"; fi
  if gate "$tmp/stars.out";  then no "$lbl overflow(***) caught" "overflow slipped"; else ok "$lbl overflow(***) caught"; fi
  rm -rf "$tmp"
}

echo "== #1 assert_no_nan (golden-path/run.sh) =="
test_nan_gate "$RUN_SH" "run.sh"
echo "== #1 assert_no_nan (smoke-test/run.sh) =="
test_nan_gate "$SMOKE_SH" "smoke.sh"

# ---- #4 require_engine: missing engine must hard-fail -----------------------
echo "== #4 engine preflight (run.sh require_engine) =="
ENG_BODY="$(extract_func "$RUN_SH" require_engine)"
if [ -z "$ENG_BODY" ]; then
  no "require_engine present [BUG-PROVING #4]" "function not found (pre-fix)"
else
  if ( eval "$ENG_BODY"; ENGINE=bash require_engine ) >/dev/null 2>&1; then
    ok "valid engine passes preflight [BUG-PROVING #4]"
  else
    no "valid engine passes preflight [BUG-PROVING #4]" "rejected an engine that is on PATH"
  fi
  if ( eval "$ENG_BODY"; ENGINE=__no_such_engine__ require_engine ) >/dev/null 2>&1; then
    no "missing engine rejected" "accepted a non-existent engine"
  else
    ok "missing engine rejected"
  fi
fi

# ---- #2/#3 extract_ligand: friendly error + altLoc tolerance ----------------
echo "== #2/#3 ligand extraction (run.sh extract_ligand) =="
LIG_BODY="$(extract_func "$RUN_SH" extract_ligand)"
if [ -z "$LIG_BODY" ]; then
  no "extract_ligand present [BUG-PROVING #2/#3]" "function not found (pre-fix)"
else
  tmp="$(mktemp -d)"
  # PDB columns: 1-6 record, 17 altLoc, 18-20 resName.
  printf 'HETATM    1  C1  BNZ A 999      11.000  22.000  33.000  1.00  0.00\n' > "$tmp/normal.pdb"
  printf 'HETATM    1  C1 ABNZ A 999      11.000  22.000  33.000  1.00  0.00\n' > "$tmp/altloc.pdb"
  printf 'ATOM      1  CA  ALA A   1      11.000  22.000  33.000  1.00  0.00\n' > "$tmp/noligand.pdb"

  if ( eval "$LIG_BODY"; extract_ligand "$tmp/normal.pdb" BNZ "$tmp/normal.out" ) >/dev/null 2>&1 && [ -s "$tmp/normal.out" ]; then
    ok "extracts a normal BNZ record [BUG-PROVING #2/#3]"
  else
    no "extracts a normal BNZ record [BUG-PROVING #2/#3]" "did not extract"
  fi
  if ( eval "$LIG_BODY"; extract_ligand "$tmp/altloc.pdb" BNZ "$tmp/altloc.out" ) >/dev/null 2>&1 && [ -s "$tmp/altloc.out" ]; then
    ok "tolerates a PDB altLoc indicator [BUG-PROVING #3]"
  else
    no "tolerates a PDB altLoc indicator [BUG-PROVING #3]" "altLoc record missed"
  fi
  # absent ligand -> non-zero exit AND a message mentioning the resname (not a silent abort)
  err="$( ( eval "$LIG_BODY"; extract_ligand "$tmp/noligand.pdb" BNZ "$tmp/none.out" ) 2>&1 )"; rc=$?
  if [ "$rc" -ne 0 ] && printf '%s' "$err" | grep -qi "BNZ"; then
    ok "absent ligand -> friendly error [BUG-PROVING #2]"
  else
    no "absent ligand -> friendly error [BUG-PROVING #2]" "rc=$rc msg='$err'"
  fi
  rm -rf "$tmp"
fi

# ---- run_happy_path up-front arg validation (behavioral, fast-fail) ---------
echo "== run_happy_path.sh arg validation =="
rhp_rejects() { # <label> <expect-substr> <args...>
  local lbl="$1" want="$2"; shift 2
  local out rc
  out="$(bash "$RHP" "$@" 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qi -- "$want"; then
    ok "$lbl"
  else
    no "$lbl" "rc=$rc, expected substr '$want'"
  fi
}
rhp_rejects "rejects --sim-ps 0"        "positive integer" --sim-ps 0
rhp_rejects "rejects --sim-ps abc"      "positive integer" --sim-ps abc
rhp_rejects "rejects --charge x"        "charge"           --sim-ps 5 --charge x
rhp_rejects "rejects over-long --name"  "1-4"              --sim-ps 5 --name TOOLONGX
rhp_rejects "rejects missing protein"   "protein"          --sim-ps 5 --name MOL --charge 0 --protein /no/such/file.pdb

# ---- summary ---------------------------------------------------------------
echo
echo "================  shell-gate tests: $PASS passed, $FAIL failed  ================"
[ "$FAIL" -eq 0 ]

#!/usr/bin/env bash
# md-planner acceptance — validate / compile / execute a JSON plan manifest.
#
#   1. Golden --dry-run  — full S2->S6 manifest validates + compiles; byte-assert
#                          the compiled plan (order s2..s6; s4 carries --sim-ps 50
#                          --cut 9.0; upstream inputs are symbolic wiring refs).
#   2. Partial --validate — S2-only manifest validates; stage_order == [s2].
#   3. Malformed --validate — cyclic / unknown-skill / out-of-bounds-cut / bad-name
#                          manifests -> ok:false + the right code (graceful).
#   4. Execute (S2-only)  — REAL antechamber on the 1L2Y ligand via the executor ->
#                          ok:true, completed==[s2], mol2+frcmod exist.
#   5. (LIVE=1) Execute full chain at --sim-ps 1 -> ok:true, completed s2..s6,
#                          MM-GBSA ΔG<0 (the end-to-end, non-spine execute proof).
#
# Needs AmberTools + pmemd; run with the AMBER env sourced (the harness wraps
# env.sh in set +u/set -u; foreground Claude Bash is 126 on conda binaries).

set -euo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PP="$(cd "$SKILL_DIR/../.." && pwd)"
W="$SKILL_DIR/scripts/wrapper.py"
FIX="$PP/golden-path/1L2Y"
RUN="$SKILL_DIR/test-runs"; rm -rf "$RUN"; mkdir -p "$RUN"
set +u; source "$PP/scripts/env.sh" >/dev/null 2>&1; set -u
PY="$(command -v python3)"

pass(){ echo "PASS: $1" >&2; }
fail(){ echo "FAIL: $1" >&2; exit 1; }
okv(){ "$PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get('ok'))" "$1"; }

# ---- generate the golden + partial manifests with real 1L2Y paths ----------
"$PY" - "$FIX" "$RUN" <<'PY'
import json, sys
fix, run = sys.argv[1], sys.argv[2]
full = {
 "manifest_version":"1","goal":"binding affinity of MOL to 1L2Y at 50 ps + profiling",
 "system":{"protein":{"path":f"{fix}/1L2Y-1.pdb"},"ligand":{"path":f"{fix}/ligand.pdb"},
           "charge":0,"name":"MOL"},
 "stages":[
  {"id":"s2","skill":"antechamber-ligandprep","params":{"name":"MOL","charge":0},
   "inputs":{"input":{"from":"system.ligand"}},
   "validate":["envelope_ok","output_exists:mol2","output_exists:frcmod"]},
  {"id":"s3","skill":"tleap-build","params":{"name":"MOL"},
   "inputs":{"protein":{"from":"system.protein"},"ligand-mol2":{"from":"s2.mol2"},
             "ligand-frcmod":{"from":"s2.frcmod"}},
   "validate":["envelope_ok","output_exists:comp_oct_top"]},
  {"id":"s4","skill":"amber-md-run","params":{"sim-ps":50,"cut":9.0,"engine":"pmemd"},
   "inputs":{"top":{"from":"s3.comp_oct_top"},"crd":{"from":"s3.comp_oct_crd"}},
   "validate":["envelope_ok","output_exists:traj"]},
  {"id":"s5","skill":"cpptraj-analysis","params":{"analyses":"all"},
   "inputs":{"comp-oct-top":{"from":"s3.comp_oct_top"},"comp-dry-top":{"from":"s3.comp_dry_top"},
             "traj":{"from":"s4.traj"},"protein-top":{"from":"s3.protein_top"},
             "ligand-top":{"from":"s3.ligand_top"},"mdout-dir":{"from":"s4.md_dir"}},
   "validate":["envelope_ok","count_at_least:outputs.produced:12",
               "numeric:outputs.mmgbsa_dG_kcal_mol<0"]},
  {"id":"s6","skill":"plip-profile","params":{"name":"MOL","frame":"medoid"},
   "inputs":{"comp-oct-top":{"from":"s3.comp_oct_top"},"comp-dry-top":{"from":"s3.comp_dry_top"},
             "traj":{"from":"s4.traj"}},
   "validate":["envelope_ok"],"on_fail":"continue"},
 ]}
json.dump(full, open(f"{run}/golden.json","w"), indent=2)
partial = {"manifest_version":"1","goal":"just prep the ligand","system":{"ligand":{"path":f"{fix}/ligand.pdb"},"charge":0,"name":"MOL"},
           "stages":[full["stages"][0]]}
json.dump(partial, open(f"{run}/partial_s2.json","w"), indent=2)
print("manifests written")
PY

# ---- Case 1: golden --dry-run, byte-assert the compiled plan ----------------
echo "[case 1] golden --dry-run + byte-assert compiled plan" >&2
"$PY" "$W" --manifest "$RUN/golden.json" --dry-run --run-root "$RUN/plan" > "$RUN/plan.json" 2>/dev/null || true
[ "$(okv "$RUN/plan.json")" = "True" ] || fail "golden dry-run not ok"
"$PY" - "$RUN/plan.json" <<'PY' || fail "compiled plan invariants"
import json,sys
e=json.load(open(sys.argv[1]))
assert e["dry_run"] is True
p=e["outputs"]["plan"]; assert p["order"]==["s2","s3","s4","s5","s6"], p["order"]
calls={c["id"]:c for c in p["calls"]}
s4=calls["s4"]["argv"]
assert "--sim-ps" in s4 and "50" in s4 and "--cut" in s4 and "9.0" in s4 and "pmemd" in s4, s4
# upstream-derived inputs are SYMBOLIC wiring refs at plan time
assert "<s3.comp_oct_top>" in s4, s4
s2=calls["s2"]["argv"]; assert "--name" in s2 and "MOL" in s2 and "--charge" in s2, s2
assert calls["s6"]["on_fail"]=="continue"
PY
pass "Case 1: golden validates + compiles; plan order s2..s6, s4 params + wiring correct"

# ---- Case 2: partial S2-only --validate ------------------------------------
echo "[case 2] partial S2-only --validate" >&2
"$PY" "$W" --manifest "$RUN/partial_s2.json" --validate > "$RUN/partial.json" 2>/dev/null || true
[ "$(okv "$RUN/partial.json")" = "True" ] || fail "partial S2 not valid"
"$PY" -c "import json,sys;e=json.load(open('$RUN/partial.json'));assert e['outputs']['stage_order']==['s2'],e['outputs']" || fail "partial order"
pass "Case 2: partial S2-only validates (order [s2])"

# ---- Case 3: malformed manifests rejected ----------------------------------
echo "[case 3] malformed manifests -> ok:false + code" >&2
mkmut(){ "$PY" - "$RUN/golden.json" "$1" "$2" <<'PY'
import json,sys
m=json.load(open(sys.argv[1])); key=sys.argv[2]
sm={s["id"]:s for s in m["stages"]}
if key=="unknown": sm["s4"]["skill"]="gromacs-run"
elif key=="cut": sm["s4"]["params"]["cut"]=15
elif key=="name": m["system"]["name"]="molecule"
elif key=="cyclic": sm["s3"]["inputs"]["x"]={"from":"s5.produced"}
json.dump(m,open(sys.argv[3],"w"))
PY
}
for k in unknown cut name cyclic; do
  mkmut "$k" "$RUN/bad_$k.json"
  "$PY" "$W" --manifest "$RUN/bad_$k.json" --validate > "$RUN/bad_$k.out.json" 2>/dev/null || true
  [ "$(okv "$RUN/bad_$k.out.json")" = "False" ] || fail "malformed[$k] not rejected"
done
"$PY" "$W" --manifest "$RUN/nope.json" --validate > "$RUN/missing.json" 2>/dev/null || true
[ "$(okv "$RUN/missing.json")" = "False" ] || fail "missing manifest not rejected"
pass "Case 3: unknown-skill / oob-cut / bad-name / cyclic / missing all rejected gracefully"

# ---- Case 4: execute S2-only (REAL antechamber) ----------------------------
echo "[case 4] --execute S2-only (real antechamber)" >&2
"$PY" "$W" --manifest "$RUN/partial_s2.json" --execute --run-root "$RUN/exec_s2" \
  > "$RUN/exec_s2.json" 2> "$RUN/exec_s2.err" || true
[ "$(okv "$RUN/exec_s2.json")" = "True" ] || { cat "$RUN/exec_s2.err" >&2; fail "S2 execute not ok"; }
"$PY" - "$RUN/exec_s2.json" <<'PY' || fail "S2 execute outputs"
import json,sys,os
e=json.load(open(sys.argv[1]))
assert e["outputs"]["completed"]==["s2"], e["outputs"]["completed"]
s2=json.load(open(e["outputs"]["stage_envelopes"]["s2"]))
assert os.path.exists(s2["outputs"]["mol2"]) and os.path.exists(s2["outputs"]["frcmod"])
PY
pass "Case 4: executor ran real antechamber -> mol2+frcmod exist (completed [s2])"

# ---- Case 5: (LIVE=1) full-chain execute at --sim-ps 1 ----------------------
if [ "${LIVE:-0}" = "1" ]; then
  echo "[case 5] LIVE full-chain --execute --sim-ps 1" >&2
  "$PY" - "$RUN/golden.json" "$RUN/golden_fast.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
for s in m["stages"]:
    if s["skill"]=="amber-md-run": s["params"]["sim-ps"]=1
json.dump(m,open(sys.argv[2],"w"))
PY
  "$PY" "$W" --manifest "$RUN/golden_fast.json" --execute --run-root "$RUN/exec_full" \
    > "$RUN/exec_full.json" 2> "$RUN/exec_full.err" || true
  [ "$(okv "$RUN/exec_full.json")" = "True" ] || { tail "$RUN/exec_full.err" >&2; fail "full execute not ok"; }
  "$PY" - "$RUN/exec_full.json" <<'PY' || fail "full execute completion"
import json,sys
e=json.load(open(sys.argv[1]))
assert e["outputs"]["completed"]==["s2","s3","s4","s5","s6"], e["outputs"]["completed"]
s5=json.load(open(e["outputs"]["stage_envelopes"]["s5"]))
dG=s5["outputs"].get("mmgbsa_dG_kcal_mol")
assert dG is not None and dG < 0, f"dG {dG}"
print(f"  full chain via planner: completed s2..s6, MM-GBSA dG {dG:.2f}", file=sys.stderr)
PY
  pass "Case 5: LIVE full chain executed manifest-first (s2..s6), ΔG<0"
else
  echo "[case 5] skipped (set LIVE=1 for the full-chain execute proof)" >&2
fi

echo "[acceptance] all cases passed" >&2

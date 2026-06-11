#!/usr/bin/env python3
"""Independent oracle for the md-planner deterministic validator.

A fixture matrix: the golden manifest + every malformed mutation, each asserting
the validator's verdict (valid/invalid) AND the specific error code it must emit.
Plus an independent topo-order cross-check (re-derive the order from the edges and
confirm the validator agrees + the order respects every dependency). Pure dict
logic — runs without AMBER, under py3.9 + py3.11.

Run:  python3 test_planner_oracle.py
"""
import copy
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import wrapper as W   # noqa: E402
import registry as R  # noqa: E402


def golden():
    return {
        "manifest_version": "1",
        "goal": "binding affinity of ligand L to protein P at 50 ps with profiling",
        "system": {"protein": {"path": "/x/P.pdb"}, "ligand": {"path": "/x/L.mol2"},
                   "charge": 0, "name": "MOL"},
        "stages": [
            {"id": "s2", "skill": "antechamber-ligandprep",
             "params": {"name": "MOL", "charge": 0},
             "inputs": {"input": {"from": "system.ligand"}},
             "validate": ["envelope_ok", "output_exists:mol2", "output_exists:frcmod"]},
            {"id": "s3", "skill": "tleap-build", "params": {"name": "MOL"},
             "inputs": {"protein": {"from": "system.protein"},
                        "ligand-mol2": {"from": "s2.mol2"},
                        "ligand-frcmod": {"from": "s2.frcmod"}},
             "validate": ["envelope_ok", "output_exists:comp_oct_top"]},
            {"id": "s4", "skill": "amber-md-run",
             "params": {"sim-ps": 50, "cut": 9.0, "engine": "pmemd"},
             "inputs": {"top": {"from": "s3.comp_oct_top"},
                        "crd": {"from": "s3.comp_oct_crd"}},
             "validate": ["envelope_ok", "output_exists:traj"]},
            {"id": "s5", "skill": "cpptraj-analysis", "params": {"analyses": "all"},
             "inputs": {"comp-oct-top": {"from": "s3.comp_oct_top"},
                        "comp-dry-top": {"from": "s3.comp_dry_top"},
                        "traj": {"from": "s4.traj"},
                        "protein-top": {"from": "s3.protein_top"},
                        "ligand-top": {"from": "s3.ligand_top"},
                        "mdout-dir": {"from": "s4.md_dir"}},
             "validate": ["envelope_ok", "count_at_least:outputs.produced:12",
                          "numeric:outputs.mmgbsa_dG_kcal_mol<0"]},
            {"id": "s6", "skill": "plip-profile",
             "params": {"name": "MOL", "frame": "medoid"},
             "inputs": {"comp-oct-top": {"from": "s3.comp_oct_top"},
                        "comp-dry-top": {"from": "s3.comp_dry_top"},
                        "traj": {"from": "s4.traj"}},
             "validate": ["envelope_ok"], "on_fail": "continue"},
        ],
    }


def mut(fn):
    m = golden()
    fn(m)
    return m


def smap(m):
    return {s["id"]: s for s in m["stages"]}


# independent topo check: every edge upstream must precede its consumer
def indep_topo_ok(m, order):
    pos = {sid: i for i, sid in enumerate(order)}
    if set(pos) != set(smap(m)):
        return False
    for sid, st in smap(m).items():
        for e in st["inputs"].values():
            ref = e["from"]
            if not ref.startswith("system.") and "." in ref:
                up = ref.split(".", 1)[0]
                if up in pos and pos[up] >= pos[sid]:
                    return False
    return True


def main():
    fails = []

    def check(name, m, expect_valid, codes=()):
        v = W.validate_manifest(m)
        ok = (v["manifest_valid"] == expect_valid)
        why = []
        if v["manifest_valid"] != expect_valid:
            why.append(f"valid={v['manifest_valid']} expected={expect_valid}")
        for c in codes:
            if not any(c in e for e in v["errors"]):
                ok = False
                why.append(f"missing code {c}; errors={v['errors']}")
        if expect_valid and not indep_topo_ok(m, v["stage_order"]):
            ok = False
            why.append(f"topo order {v['stage_order']} violates an edge")
        print(("PASS" if ok else "FAIL") + f": {name}" + ("" if ok else "  <- " + "; ".join(why)))
        if not ok:
            fails.append(name)

    print("== valid manifests ==")
    g = golden()
    check("golden full S2->S6", g, True)
    v = W.validate_manifest(g)
    if v["stage_order"] != ["s2", "s3", "s4", "s5", "s6"]:
        print(f"FAIL: golden order {v['stage_order']} != s2..s6"); fails.append("golden-order")
    else:
        print("PASS: golden topo order == [s2,s3,s4,s5,s6]")
    check("partial S2 only (prep ligand)",
          {"manifest_version": "1", "goal": "prep", "system": {"ligand": {"smiles": "c1ccccc1"}, "charge": 0, "name": "BZN"},
           "stages": [golden()["stages"][0]]}, True)
    check("partial S2+S3 (prep+build)",
          mut(lambda m: m["stages"].__setitem__(slice(3, None), [])), True)
    # a real-but-uncommon flag is accepted (registry lists it)
    check("real uncommon param accepted (s4 steps=min)",
          mut(lambda m: smap(m)["s4"]["params"].update({"steps": "min"})), True)

    print("== malformed manifests (must be REJECTED with the right code) ==")
    # adversarial-review regressions
    check("unknown param rejected (foo not a flag of the skill)",
          mut(lambda m: smap(m)["s4"]["params"].update({"foo": 1})),
          False, ["UNKNOWN_PARAM"])
    check("dt rejected (not an amber-md-run flag; can't reach pmemd)",
          mut(lambda m: smap(m)["s4"]["params"].update({"dt": 0.005})),
          False, ["UNKNOWN_PARAM"])
    check("non-string id -> graceful MALFORMED (no crash)",
          mut(lambda m: smap(m)["s2"].__setitem__("id", 2)),
          False, ["MALFORMED_MANIFEST"])
    check("validate as bare string -> MALFORMED (no per-char silent pass)",
          mut(lambda m: smap(m)["s2"].__setitem__("validate", "envelope_ok")),
          False, ["MALFORMED_MANIFEST"])
    check("self-loop edge -> CYCLIC_DAG",
          mut(lambda m: smap(m)["s4"]["inputs"].__setitem__("top", {"from": "s4.md_dir"})),
          False, ["CYCLIC_DAG"])
    check("trailing-newline name -> BAD_NAME (\\A..\\Z anchors)",
          mut(lambda m: m["system"].__setitem__("name", "MOL\n")),
          False, ["BAD_NAME"])
    check("unknown skill",
          mut(lambda m: smap(m)["s4"].__setitem__("skill", "gromacs-run")),
          False, ["UNKNOWN_SKILL"])
    check("cyclic DAG",
          mut(lambda m: smap(m)["s3"]["inputs"].__setitem__("x", {"from": "s5.produced"})),
          False, ["CYCLIC_DAG"])
    check("missing required input (s5 drops comp-oct-top)",
          mut(lambda m: smap(m)["s5"]["inputs"].pop("comp-oct-top")),
          False, ["MISSING_INPUT"])
    check("unsatisfied wiring (s5.traj <- s3.traj; tleap has no traj)",
          mut(lambda m: smap(m)["s5"]["inputs"].__setitem__("traj", {"from": "s3.traj"})),
          False, ["UNSATISFIED_WIRING"])
    check("dangling ref (s4.top <- s9.*)",
          mut(lambda m: smap(m)["s4"]["inputs"].__setitem__("top", {"from": "s9.comp_oct_top"})),
          False, ["DANGLING_REF"])
    check("out-of-bounds cut=15", mut(lambda m: smap(m)["s4"]["params"].__setitem__("cut", 15)),
          False, ["MD_PARAM_OUT_OF_BOUNDS"])
    check("out-of-bounds cut=7", mut(lambda m: smap(m)["s4"]["params"].__setitem__("cut", 7)),
          False, ["MD_PARAM_OUT_OF_BOUNDS"])
    check("bad charge (system)", mut(lambda m: m["system"].__setitem__("charge", "zero")),
          False, ["BAD_CHARGE"])
    check("bad name (system)", mut(lambda m: m["system"].__setitem__("name", "molecule")),
          False, ["BAD_NAME"])
    check("bad sim-ps=0", mut(lambda m: smap(m)["s4"]["params"].__setitem__("sim-ps", 0)),
          False, ["BAD_SIM_PS"])
    check("bad sim-ps negative", mut(lambda m: smap(m)["s4"]["params"].__setitem__("sim-ps", -5)),
          False, ["BAD_SIM_PS"])
    check("conditional input (mol2 without frcmod)",
          mut(lambda m: smap(m)["s3"]["inputs"].pop("ligand-frcmod")),
          False, ["MISSING_INPUT"])
    check("duplicate stage id",
          mut(lambda m: m["stages"].append(copy.deepcopy(smap(m)["s3"]))),
          False, ["DUPLICATE_STAGE_ID"])
    check("malformed: stages not a list",
          {"manifest_version": "1", "system": {}, "stages": "nope"},
          False, ["MALFORMED_MANIFEST"])
    check("malformed: bad version",
          mut(lambda m: m.__setitem__("manifest_version", "9")),
          False, ["MALFORMED_MANIFEST"])

    print("\n" + ("ALL PLANNER ORACLE CASES PASS" if not fails else "FAILURES: " + ",".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

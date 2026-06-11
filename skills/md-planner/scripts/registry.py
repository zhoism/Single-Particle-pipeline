"""md-planner stage registry — the Confirmed I/O contract of the forward chain.

A dict literal (NOT a runtime SKILL.md parse) so the contract the validator gates
against is version-controlled, diff-reviewable, and frozen at commit time — the
Memory-Provenance `confirmed` discipline. The single risk of a literal (drift from
the SKILL.md it transcribes) is closed by tests/test_registry_consistency.py, which
parses every skill's SKILL.md metadata at TEST time and asserts the registry is a
superset.

Per skill:
  inputs   key -> {required, cli, [requires_with]}   FILE/PATH inputs (wired from an
                                                      upstream output or a system file)
  params   key -> {cli, type, required}              SCALAR params (--flag value)
  outputs  list of declared envelope output keys      (what downstream stages may wire from)
  md_param_stage  bool                                 (triggers the check_amber bounds gate)

amber-recover + mdin-edit are deliberately EXCLUDED: recovery/editing are not
forward-chain stages, and the planner does not plan them.

Param types: int | pos_number | number | resname | str | enum:<a>|<b>|...
Output keys verified against each wrapper's success-path `outputs={...}` dict.
"""

REGISTRY = {
    "antechamber-ligandprep": {
        "stage": "S2",
        "md_param_stage": False,
        "inputs": {
            "input": {"required": True, "cli": "--input"},
        },
        "params": {
            "name":   {"cli": "--name",   "type": "resname", "required": False},
            "charge": {"cli": "--charge", "type": "int",     "required": False},
        },
        "outputs": ["mol2", "frcmod", "run_dir"],
    },
    "tleap-build": {
        "stage": "S3",
        "md_param_stage": False,
        "inputs": {
            "protein":       {"required": True,  "cli": "--protein"},
            "ligand-mol2":   {"required": False, "cli": "--ligand-mol2"},
            "ligand-frcmod": {"required": False, "cli": "--ligand-frcmod",
                              "requires_with": "ligand-mol2"},
        },
        "params": {
            "name":       {"cli": "--name",       "type": "resname", "required": False},
            "buffer":     {"cli": "--buffer",     "type": "number",  "required": False},
            "protein-ff": {"cli": "--protein-ff", "type": "str",     "required": False},
            "water":      {"cli": "--water",      "type": "str",     "required": False},
            "ligand-ff":  {"cli": "--ligand-ff",  "type": "str",     "required": False},
        },
        "outputs": ["comp_oct_top", "comp_oct_crd", "comp_dry_top", "comp_dry_crd",
                    "comp_oct_pdb", "protein_top", "ligand_top", "leap_in", "run_dir"],
    },
    "amber-md-run": {
        "stage": "S4",
        "md_param_stage": True,
        "inputs": {
            "top": {"required": True, "cli": "--top"},
            "crd": {"required": True, "cli": "--crd"},
        },
        "params": {
            "sim-ps":      {"cli": "--sim-ps",      "type": "pos_number", "required": False},
            "cut":         {"cli": "--cut",         "type": "number",     "required": False},
            "engine":      {"cli": "--engine",
                            "type": "enum:pmemd|pmemd.MPI|sander", "required": False},
            "heat-ps":     {"cli": "--heat-ps",     "type": "pos_number", "required": False},
            "density-ps":  {"cli": "--density-ps",  "type": "pos_number", "required": False},
            "ncpus":       {"cli": "--ncpus",       "type": "int",        "required": False},
            "steps":       {"cli": "--steps",       "type": "enum:all|min", "required": False},
            "engine-home": {"cli": "--engine-home", "type": "str",        "required": False},
        },
        # md_dir is the run's working dir — it is what cpptraj-analysis takes as
        # --mdout-dir (heat/density/product .out live there). Real envelope key.
        "outputs": ["traj", "final_rst", "md_dir", "run_sh", "run_log",
                    "product_out", "wall_time_s"],
    },
    "cpptraj-analysis": {
        "stage": "S5",
        "md_param_stage": False,
        "inputs": {
            "comp-oct-top": {"required": True,  "cli": "--comp-oct-top"},
            "comp-dry-top": {"required": True,  "cli": "--comp-dry-top"},
            "traj":         {"required": True,  "cli": "--traj"},
            "protein-top":  {"required": False, "cli": "--protein-top"},
            "ligand-top":   {"required": False, "cli": "--ligand-top"},
            "mdout-dir":    {"required": False, "cli": "--mdout-dir"},
        },
        "params": {
            "analyses": {"cli": "--analyses", "type": "str", "required": False},
        },
        "outputs": ["produced", "analyses", "failed", "mmgbsa_dG_kcal_mol",
                    "analysis_dir"],
    },
    "plip-profile": {
        "stage": "S6",
        "md_param_stage": False,
        "inputs": {
            "comp-oct-top": {"required": True, "cli": "--comp-oct-top"},
            "comp-dry-top": {"required": True, "cli": "--comp-dry-top"},
            "traj":         {"required": True, "cli": "--traj"},
        },
        "params": {
            "name":  {"cli": "--ligand-resname", "type": "resname", "required": False},
            "frame": {"cli": "--frame",          "type": "str",     "required": False},
        },
        "outputs": ["interactions", "totals", "contact_residues", "ligand",
                    "complex_pdb"],
    },
}

# System fields a stage input may be wired from (the provided-file roots).
SYSTEM_FIELDS = {"protein", "ligand"}

# Map a skill name -> its wrapper path (relative to the skills/ dir), for the
# compiler/executor. The planner is itself under skills/, so '..' reaches siblings.
WRAPPER_RELPATH = {name: f"{name}/scripts/wrapper.py" for name in REGISTRY}

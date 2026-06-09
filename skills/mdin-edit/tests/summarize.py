#!/usr/bin/env python3
"""Summarize an overnight results.csv (rows: iter,check,rc) into summary.json +
a human print. Used by overnight.sh (and the morning report)."""
import collections
import json
import sys

csv_path, out_path = sys.argv[1], sys.argv[2]
agg = collections.defaultdict(lambda: [0, 0])  # check -> [pass, fail]
failures = []
for line in open(csv_path):
    parts = line.strip().split(",")
    if len(parts) < 3:
        continue
    it, check, rc = parts[0], parts[1], parts[2]
    agg[check][0 if rc == "0" else 1] += 1
    if rc != "0":
        failures.append({"iter": it, "check": check, "rc": rc})

out = {
    "checks": {k: {"pass": v[0], "fail": v[1]} for k, v in sorted(agg.items())},
    "total_pass": sum(v[0] for v in agg.values()),
    "total_fail": sum(v[1] for v in agg.values()),
    "failures": failures[:500],
}
json.dump(out, open(out_path, "w"), indent=2)
print(f"SUMMARY: {out['total_pass']} pass, {out['total_fail']} fail")
for k, v in sorted(agg.items()):
    print(f"  {k:16s} {v[0]:4d} pass / {v[1]} fail")

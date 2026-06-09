#!/usr/bin/env python3
"""Self-test of the independent oracle (the trust anchor).

Proves the oracle is NOT a rubber stamp: it must ACCEPT known-good edits and
REJECT a battery of deliberately-corrupt "results" (appended line, wrong value,
collateral change, sibling clobber, eaten comment, non-numeric edit). Also
sanity-checks the spec decision-function + the demo ground-truth loader.

Run: python3 oracle_selftest.py   (exit 0 = oracle trustworthy)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oracle as O  # noqa: E402

PASS, FAIL = 0, 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  SELFTEST FAIL: {msg}", file=sys.stderr)


def expect_pass(orig, result, allowed, must_equal, msg):
    try:
        O.verify_edit(orig, result, allowed_changes=allowed, must_equal=must_equal)
        ok(True, msg)
    except O.OracleError as e:
        ok(False, f"{msg} — oracle wrongly REJECTED a good edit: {e}")


def expect_reject(orig, result, allowed, must_equal, msg):
    try:
        O.verify_edit(orig, result, allowed_changes=allowed, must_equal=must_equal)
        ok(False, f"{msg} — oracle FAILED TO CATCH a corrupt result")
    except O.OracleError:
        ok(True, msg)


CNTRL = """Heat MD
 &cntrl
  nstlim = 50000,
  dt = 0.002,
  cut = 9.0,
  temp0 = 300.0,
  nmropt = 1,
 /
  &wt
  type = 'TEMP0',
  istep1 = 0, istep2 = 40000,
  value1 = 200.0, value2 = 310.0,
  /
  &wt
  type = 'END',
  /
"""


def repl(text, old, new):
    assert old in text, f"setup bug: {old!r} not in text"
    return text.replace(old, new, 1)


def main() -> int:
    # ---- ground truth loads + no drift ----
    try:
        gt = O.load_ground_truth()
        ok(True, "ground truth loads without drift")
    except O.OracleError as e:
        ok(False, f"ground truth drift: {e}")
        gt = None

    DT = {("cntrl", "dt")}
    TC = {("cntrl", "temp0"), ("wt", "value2")}

    # ---- KNOWN-GOOD edits ----
    good = repl(CNTRL, "  dt = 0.002,", "  dt = 0.001,")
    expect_pass(CNTRL, good, DT, [("cntrl", "dt", "0.001")], "good: dt 0.002->0.001")

    # temp0 + coupled value2 (both change)
    g2 = repl(repl(CNTRL, "  temp0 = 300.0,", "  temp0 = 305.0,"),
              "value2 = 310.0,", "value2 = 305.0,")
    expect_pass(CNTRL, g2, TC, [("cntrl", "temp0", "305"), ("wt", "value2", "305")],
                "good: temp0->305 with coupled value2")

    # coupling no-op: temp0 set to 310 where value2 already 310 (only temp0 changes)
    g_noop = repl(CNTRL, "  temp0 = 300.0,", "  temp0 = 310.0,")
    expect_pass(CNTRL, g_noop, TC, [("cntrl", "temp0", "310"), ("wt", "value2", "310")],
                "good: temp0->310 coupling no-op (value2 already 310)")

    # value-at-target style (no change) — identical text, nothing allowed to change
    expect_pass(CNTRL, CNTRL, set(), [], "good: no-op (identical) empty allowed-set")

    # numeric format variants accepted by Decimal-equality
    g3 = repl(CNTRL, "  dt = 0.002,", "  dt = 0.0015,")
    expect_pass(CNTRL, g3, DT, [("cntrl", "dt", "0.0015")], "good: dt 0.0015")

    # ---- KNOWN-CORRUPT results (oracle MUST reject) ----
    bad_append = good + "  ig = -1,\n"
    expect_reject(CNTRL, bad_append, DT, [("cntrl", "dt", "0.001")], "corrupt: appended line")

    bad_val = repl(CNTRL, "  dt = 0.002,", "  dt = 0.003,")
    expect_reject(CNTRL, bad_val, DT, [("cntrl", "dt", "0.001")], "corrupt: wrong value")

    bad_collat = repl(repl(CNTRL, "  dt = 0.002,", "  dt = 0.001,"),
                      "  cut = 9.0,", "  cut = 8.0,")
    expect_reject(CNTRL, bad_collat, DT, [("cntrl", "dt", "0.001")], "corrupt: collateral cut change")

    bad_sib = repl(repl(CNTRL, "  temp0 = 300.0,", "  temp0 = 305.0,"),
                   "value1 = 200.0,", "value1 = 205.0,")
    expect_reject(CNTRL, bad_sib, TC, [("cntrl", "temp0", "305"), ("wt", "value2", "305")],
                  "corrupt: sibling value1 clobbered")

    cmt = repl(CNTRL, "  dt = 0.002,", "  dt = 0.002, ! tunable")
    cmt_bad = repl(cmt, "  dt = 0.002, ! tunable", "  dt = 0.001,")
    expect_reject(cmt, cmt_bad, DT, [("cntrl", "dt", "0.001")], "corrupt: eaten inline comment")

    cmt_good = repl(cmt, "  dt = 0.002, ! tunable", "  dt = 0.001, ! tunable")
    expect_pass(cmt, cmt_good, DT, [("cntrl", "dt", "0.001")], "good: edit preserves inline comment")

    bad_key = repl(good, "  cut = 9.0,", "  rcut = 9.0,")
    expect_reject(CNTRL, bad_key, DT, [("cntrl", "dt", "0.001")], "corrupt: key renamed (set change)")

    expect_reject(CNTRL, CNTRL, DT, [("cntrl", "dt", "0.001")], "corrupt: claimed edit not present")

    # ---- spec decision-function sanity ----
    if gt:
        s = O.spec("heat-1", "dt", "0.001", gt)
        ok(s.global_error is None and s.per_file["heat-1"][0] == "edited" and s.rendered == "0.001",
           "spec: dt on heat-1 -> edited 0.001")
        s = O.spec("min1", "dt", "0.001", gt)
        ok(s.per_file["min1"] == ("error", "PARAM_NOT_FOUND") and not s.ok,
           "spec: dt on min1 single -> error PARAM_NOT_FOUND")
        s = O.spec("group:all", "restraint_wt", "1.0", gt)
        skipped = {st for st, (stt, _) in s.per_file.items() if stt == "skipped"}
        ok(s.ok and skipped == {"min2", "relax", "prod"},
           "spec: restraint_wt group:all skips ntr=0 files")
        s = O.spec("relax", "cut", "7.0", gt)
        ok(s.warn_cut and s.per_file["relax"][0] == "edited", "spec: cut 7.0 -> warn band")
        s = O.spec("heat-1", "cut", "8.0", gt)
        ok(not s.warn_cut, "spec: cut 8.0 -> NO warn (band is [6,8))")
        s = O.spec("relax", "restraint_wt", "1.0", gt)
        ok(s.per_file["relax"] == ("error", "SKIPPED_RESTRAINTS_OFF"),
           "spec: restraint_wt on relax single -> error")
        # crash-class -> desired graceful INVALID_VALUE
        for bad in ("inf", "nan", "1e999", "0.00２", "1_000", "", "abc", "0x10"):
            s = O.spec("heat-1", "nstlim", bad, gt)
            ok(s.global_error in ("INVALID_VALUE", "NONINTEGER_VALUE", "OUT_OF_BOUNDS"),
               f"spec: nstlim {bad!r} -> graceful error (got {s.global_error})")
        s = O.spec("heat-1", "temp0", "310", gt)
        ok(s.rendered == "310.0", "spec: temp0 310 -> 310.0")
        s = O.spec("heat-1", "nstlim", "50", gt)
        ok(s.rendered == "50", "spec: nstlim 50 -> 50")

    # ---- canonical-format predicate ----
    ok(O.canonical_format_ok("dt", "0.001"), "fmt: 0.001 ok")
    ok(O.canonical_format_ok("temp0", "310.0"), "fmt: 310.0 ok")
    ok(not O.canonical_format_ok("dt", "0.0020"), "fmt: 0.0020 rejected (trailing zero)")
    ok(not O.canonical_format_ok("dt", ".5"), "fmt: .5 rejected")
    ok(not O.canonical_format_ok("dt", "1e-3"), "fmt: 1e-3 rejected (exponent)")
    ok(O.canonical_format_ok("nstlim", "50"), "fmt: int 50 ok")
    ok(not O.canonical_format_ok("nstlim", "050"), "fmt: 050 rejected")
    ok(not O.canonical_format_ok("nstlim", "5.0"), "fmt: 5.0 rejected for int")

    print(f"\n[oracle-selftest] {PASS} passed, {FAIL} failed", file=sys.stderr)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

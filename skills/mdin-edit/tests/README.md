# mdin-edit test harness

A deterministic, self-verifying harness for the `mdin-edit` engine. Built for an
overnight rigorous-testing pass (2026-06-08). Stdlib only.

## Run

```bash
# trust anchor — the oracle must reject known-corrupt edits before you trust it
python3 oracle_selftest.py            # 38 assertions

# Tier 1 — exhaustive matrix + property/synthetic fuzz + crash/fault + coverage
python3 fuzz_mdin_edit.py             # full (~240k assertions, ~55s)
python3 fuzz_mdin_edit.py --quick     # fast (~6k assertions)

# "who tests the tester" — inject engine mutants, every one must be killed
python3 mutation_test.py              # 8/8 = 100%

# Tier 3 — prove mdin-edit's output is pmemd-runnable (needs scripts/env.sh)
bash smoke_edit_run.sh                # reduced-nstlim edit->run, 10/10 stages

# Overnight robustness loop (gate once, then loop under a wall-clock cap)
OVERNIGHT_SECONDS=25200 caffeinate -i bash overnight.sh
```

Use the **system** python (3.14) for the harness; the engine itself runs under
both 3.11 (conda, what OpenClaw uses) and 3.14.

## Design (why it's trustworthy)

`oracle.py` is an **independent** oracle — it never imports the engine's
`render_value`/regex, so an engine bug can't be masked by a matching oracle bug:
- **byte-level** structural check (raw line diff; only a numeric token may change;
  comments/commas/siblings/structure byte-identical; no append/delete);
- **value** check via `decimal.Decimal` (numeric equality) + an independent
  canonical-format predicate (shortest-decimal, `.0` for integral);
- a from-scratch namelist **scanner** for the "no other key changed" check.
- a **spec decision-function** (the desired contract) re-verified against the demo
  files at startup (aborts on drift).

`oracle_selftest.py` proves the oracle rejects appended lines, wrong values,
collateral/sibling changes, and eaten comments — i.e. it is not a rubber stamp.

`mutation_test.py` injects 8 semantic engine mutants (bounds, coupling, render
format, no-op detection, ambiguous guard, cut band, input gate, span off-by-one)
and requires the harness to kill every one (score 8/8).

## Bugs found + fixed (this pass)

The harness found **5 engine bug classes** (+ 1 bug in its own spec); all fixed and
re-verified green:

1. **Crash class** — `nstlim` with `inf`/`nan`/`1e999` hit `int()` in
   `bounds_verdict` → uncaught `OverflowError`/`ValueError` with **no JSON
   envelope** (also `restraint_wt inf` crashed in `render_value`).
2. **Silent non-ASCII / underscore acceptance** — `float('０.００２')`→`0.002`,
   `'1_000'`→`1000` were silently accepted and **written**.
   → Fix (1+2): a strict ASCII numeric gate (`_VAL_ASCII`, `[0-9]` not `\d`) +
   `math.isfinite` in `bounds_verdict`, before any `float()`/`int()`.
3. **CRLF normalization** — `read_text`/`write_text` rewrote CRLF→LF (not
   byte-minimal). → read/write with `open(newline="")`.
4. **Precision loss** — a tiny `dt` like `2.55e-05` was truncated by `%.12f`.
   → `render_value` renders fractional floats via `Decimal` (full precision, no
   exponent).
5. **Python 3.11 incompatibility** — the CRLF fix first used
   `Path.read_text(newline=)` (3.13+); OpenClaw runs conda Python 3.11 → crash.
   Caught by the harness's cross-version checks. → builtin `open(newline="")`.

Plus a harness/spec bug it caught about *itself*: `temp0` "at-target" must account
for the coupled `&wt value2` (heat-3 `temp0=300` still edits `value2` 310→300).

## Status snapshot
Oracle self-test 38/38 · Tier-1 ~240k assertions, 0 failures, full status+code
coverage · mutation 8/8 · edit→run smoke 10/10 stages · under conda 3.11 + system
3.14.

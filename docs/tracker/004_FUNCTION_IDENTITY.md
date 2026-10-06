# Tracker: Function identity and the lineage DAG

**Task(s):** `docs/tasks/function-identity/` — created as tasks are written
**Priority:** P0
**Status:** ⬜ Not started
**Date:** 2026-10-06

---

## Summary
The typed lineage DAG with N:M extract/inline edges and per-edge confidence, built forward over
Python without rewriting the repository. ROADMAP §5 week 8 (ringed), §7. **This is the contribution.**

## Problem Statement
The novelty claim rests on one ablation table, and that table currently has no control, no
implemented dependent variable, and a ground truth that may not exist.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | [Control arms, dependent variable, pre-registration](../tasks/review-remediation/003_control_arms_and_preregistration.md) (finding 3) | ⬜ | `provenance/eval/metrics.py`, `docs/PREREGISTRATION.md` |
| 2 | One-hour spike: confirm CodeShovel's Python oracle exists (file, method count, source projects, label scheme); record the no-oracle fallback in an ADR (finding 9) | ⬜ | new ADR |
| 3 | Promote ROADMAP:195's own 100-stratified-function comparison to the week-8 oracle; re-measure the identity-breaking-event rate on Airflow at n≥300 using the `243fe86d4b` fixture | ⬜ | `symbol_lineage`, `lineage_edges` |
| 4 | Two hash tiers (`h1` token stream, `h3` alpha-renamed AST); forward pass, same-file bucketing; one cross-file rule | ⬜ | identity layer |

## Dependencies
- Task 2 **before week 6**, not week 8. If the oracle is not what §7 claims, the fallback is 500
  hand-labels, and discovering that in week 8 is exactly when ROADMAP:85 says decisions get expensive.
- Task 1 before any week-8 run.
- Blocked on [003_SYMBOLS_BLAME](003_SYMBOLS_BLAME.md) task 1 for a non-straw-man blame arm.

## Verification Steps
1. Two numbers at the week-8 gate: oracle accuracy vs a path-anchored baseline, and
   provenance-chain recall with lineage on vs the **volume-matched** control.
2. `docs/PREREGISTRATION.md` commit date precedes the first ablation run in `git log`.

## Notes
- **2026-10-06.** Do not build on stdlib `ast` alone — 5.5% of 2015-era Airflow files are
  unparseable under modern Python. Hash the tree-sitter s-expression and stamp the grammar version;
  `ast.dump` is not stable across Python minors.
- **2026-10-06.** The "40% of oracle methods contain an identity-breaking event" figure is 16/40,
  Wilson 95% [26.4%, 55.4%]. §7 holds every other author to exactly this standard; quote it with
  its n and interval, or re-measure it on Airflow and make it yours.

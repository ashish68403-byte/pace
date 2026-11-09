# Tracker: Function identity and the lineage DAG

**Task(s):** [docs/tasks/function-identity/](../tasks/function-identity/) — 001 (checkpoint C, slice C1, the pre-registration) written 2026-11-08; 002 (slice C3a, the lineage engine on Airflow) written 2026-11-09
**Decision records:** [ADR-0005](../ADR-0005-lineage-vs-blame.md) — the lineage-against-blame experiment; [PREREGISTRATION.md](../PREREGISTRATION.md)
**Priority:** P0
**Status:** 🚧 In progress — slice C1 done; C2, the blame arm, next
**Date:** 2026-10-06

---

## Summary
The typed lineage DAG with N:M extract/inline edges and per-edge confidence, built forward over
Python without rewriting the repository. ROADMAP §5 checkpoint C, §7. **This is the contribution.**

## Problem Statement
The novelty claim rests on one ablation table, and that table currently has no control, no
implemented dependent variable, and a ground truth that may not exist.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | [Control arms, dependent variable, pre-registration](../tasks/review-remediation/003_control_arms_and_preregistration.md) (finding 3) | 🚧 | `provenance/eval/metrics.py`, `preregistration.py`, `docs/PREREGISTRATION.md`: [function-identity/001](../tasks/function-identity/001_preregistration.md) done; the three-arm run is slice C4 |
| 2 | One-hour spike: confirm CodeShovel's Python oracle exists (file, method count, source projects, label scheme); record the no-oracle fallback in an ADR (finding 9) | ✅ | [ADR-0005](../ADR-0005-lineage-vs-blame.md), Context: the oracle exists; no fallback needed |
| 3 | Promote ROADMAP:195's own 100-stratified-function comparison to checkpoint C's oracle; re-measure the identity-breaking-event rate on Airflow at n≥300 using the `243fe86d4b` fixture | ⬜ | `symbol_lineage`, `lineage_edges` |
| 4 | Two hash tiers (`h1` token stream, `h3` alpha-renamed AST); forward pass, same-file bucketing; one cross-file rule | ⬜ | identity layer |

## Dependencies
- Task 2 **before checkpoint C starts**, not during it. If the oracle is not what §7 claims, the
  fallback is 500 hand-labels, and discovering that mid-checkpoint is exactly when ROADMAP §3 says
  decisions get expensive.
- Task 1 before any comparison run.
- Blocked on [003_SYMBOLS_BLAME](003_SYMBOLS_BLAME.md) task 1 for a non-straw-man blame arm.

## Verification Steps
1. Two numbers at checkpoint C's gate: oracle accuracy vs a path-anchored baseline, and
   provenance-chain recall with lineage on vs the **volume-matched** control.
2. `docs/PREREGISTRATION.md` commit date precedes the first ablation run in `git log`.

## Notes
- **2026-10-06.** Do not build on stdlib `ast` alone — 5.5% of 2015-era Airflow files are
  unparseable under modern Python. Hash the tree-sitter s-expression and stamp the grammar version;
  `ast.dump` is not stable across Python minors.
- **2026-10-06.** The "40% of oracle methods contain an identity-breaking event" figure is 16/40,
  Wilson 95% [26.4%, 55.4%]. §7 holds every other author to exactly this standard; quote it with
  its n and interval, or re-measure it on Airflow and make it yours.
- **2026-11-08 — slice C1 closed.** The experiment was fixed in ADR-0005 and PREREGISTRATION.md
  (`27205d76a8`) before any number. `metrics.py` computes a chain hit and the equal-size
  control's exact chance. `preregistration.py` holds the thresholds and the decision rules, which
  return the pre-registered words, and a test keeps them equal to the committed file. The tests
  run in `pace_test` and refuse `pace`. Next: slice C2, the blame arm.
- **2026-11-09 — slice C2 closed.** Arms A and L are stored for the 1,777 functions
  ([symbols-blame/001](../tasks/symbols-blame/001_blame_arms.md),
  [results/blame_arms.json](../../results/blame_arms.json)): per function, a median of 3
  commits by blame and 4 by `git log -L`, and neither tool stops at the 2025 move. Next: slice
  C3, the identity layer and its oracle check.

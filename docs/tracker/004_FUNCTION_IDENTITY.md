# Tracker: Function identity and the lineage DAG

**Task(s):** [docs/tasks/function-identity/](../tasks/function-identity/) — 001 (checkpoint C, slice C1, the pre-registration) written 2026-11-08; 002 (slice C3a, the lineage engine on Airflow) written 2026-11-09; 003 (slice C3b, the oracle run) written 2026-11-09
**Decision records:** [ADR-0005](../ADR-0005-lineage-vs-blame.md) — the lineage-against-blame experiment; [PREREGISTRATION.md](../PREREGISTRATION.md)
**Priority:** P0
**Status:** 🚧 In progress — slices C1, C2, C3a and C3b done; C4, the comparison, next
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
| 3 | Promote ROADMAP:195's own 100-stratified-function comparison to checkpoint C's oracle; re-measure the identity-breaking-event rate on Airflow at n≥300 using the `243fe86d4b` fixture | ✅ | `symbol_lineage`, `lineage_edges`: in [function-identity/002](../tasks/function-identity/002_lineage_engine.md), of the 2,808 functions alive at the pinned commit, 61.6% (Wilson 59.8–63.4%) hold a rename or move with the 2025 move counted and 21.3% (19.9–22.9%) without it; the 100-function agreement check with `git log -L` misses 0.90 at 0.771 [0.721, 0.817] ([results/lineage.json](../../results/lineage.json)) |
| 4 | Two hash tiers (`h1` token stream, `h3` alpha-renamed AST); forward pass, same-file bucketing; one cross-file rule | ✅ | `provenance/lineage/`: name, then name-blind h1, then h3, same file; the one cross-file rule linked 397 moves on Airflow; a rename made with a body edit is beyond it (ADR-0005, Addenda) |

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
- **2026-11-09 — slice C3a closed.** The lineage engine
  ([function-identity/002](../tasks/function-identity/002_lineage_engine.md),
  [results/lineage.json](../../results/lineage.json)) gives Airflow's history 5,023 identities
  and 28,327 versions, with 298 renames linked at h1, 52 at h3 and 397 moves between files. Two
  builds store the same content. Arm C has a median of 5 commits per question function. The
  agreement check misses 0.90 at 0.771, and ADR-0005's Addenda break the gap down. The engine is
  frozen as `lineage-mvp-1`. Next: slice C3b, one run on CodeShovel's oracle.
- **2026-11-09 — slice C3b closed.** The frozen engine ran once on CodeShovel's 40 Python oracle
  functions ([function-identity/003](../tasks/function-identity/003_oracle_run.md),
  [results/oracle.json](../../results/oracle.json)), on `2671da966b`; a repeat wrote the same
  bytes. Mean F1 is 0.8994 for lineage and 0.8363 for `git log -L`: a difference of 0.0631, 95%
  paired interval -0.0242 to 0.1487 (n = 40). Claim 3 failed, in the pre-registered words: "The
  lineage layer did not track Python functions measurably better than git log -L on
  CodeShovel's oracle." The 16 rename-or-move functions, reported apart: 0.8803 against 0.7434.
  On Airflow, lineage walks only the 303 in-scope files. Next: slice C4, the comparison.

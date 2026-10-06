# Tracker: Agent loop and refusal

**Task(s):** `docs/tasks/agent-refusal/` — created as tasks are written
**Priority:** P1
**Status:** ⬜ Not started
**Date:** 2026-10-06

---

## Summary
A hand-written agent loop with five tools and enforced budgets, grounding verification, and the
refusal path. ROADMAP §5 weeks 11-12 and 13 (ringed).

## Problem Statement
Refusal is currently scored only against its own `UNANSWERABLE` label. That establishes agreement
with your own labelling; it says nothing about whether abstaining raises accuracy on what is
answered. A one-line confidence threshold is the null hypothesis, and if it matches the verifier,
grounding verification is dead weight in the thesis rather than a contribution.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | **Hard deadline — week 11, first agent run.** Log per query, including refused ones: fused top-1 and top-10 scores, verifier score, citation-precision/correctness outcome (finding 15) | ⬜ | `provenance/eval/runner.py` `PER_QUERY_COLUMNS` |
| 2 | Risk-coverage curve over answerables, plus the same curve for two trivial abstainers (fused-score threshold; empty-evidence rule); quote the coverage level at which headline citation precision is stated | ⬜ | `provenance/eval/metrics.py` |
| 3 | Oversample unanswerables; report refusal recall and false-refusal rate (prevalence-free), derive precision analytically at the 20% base rate (finding 14) | ⬜ | golden-set generator |
| 4 | Publish counts per `UnanswerableStrategy` — with ~40 negatives across five strategies, no per-strategy claim is possible | ⬜ | `provenance/eval/runner.py` |
| 5 | Five tools, enforced budgets, replayable runs; `budget_exceeded` recorded as a trajectory outcome | ⬜ | agent loop |

## Dependencies
- **Task 1 cannot be done retrospectively.** If week 11's runs do not log it, finding 15 is
  unanswerable for the rest of the project. This is the single most time-sensitive item outside
  week 8.
- Task 3 feeds [006_EVAL_HARNESS](006_EVAL_HARNESS.md) task 7.

## Verification Steps
1. Multi-hop queries answered within budget, every run replayable from cassettes.
2. Refusal precision **and** recall reported with intervals (ROADMAP §5 week 13 gate).
3. Risk-coverage curve shows the verifier beating both trivial abstainers, or the thesis says it
   does not — which is also a result.

## Notes
- **2026-10-06.** Wire hard budget caps before the first live agent run: an in-process budget that
  aborts and records `budget_exceeded` as a trajectory outcome, so evaluation counts it as a failure
  rather than silently retrying.
- **2026-10-06.** The S1-S5 unanswerable taxonomy pre-empts the easy criticism by making negatives
  diverse — but diversity shows refusals are not trivially detectable, not that abstaining is useful.
  Those are different claims; task 2 is the one that tests the second.

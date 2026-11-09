# Tracker: Agent loop and refusal

**Task(s):** [docs/tasks/agent-refusal/](../tasks/agent-refusal/) — 001 (checkpoint D, slice D1, the unanswerable questions) written and done 2026-11-09
**Decision records:** [ADR-0006](../ADR-0006-refusal-first-checkpoint-d.md) — refusal-first D; [PREREGISTRATION.md](../PREREGISTRATION.md) claim 5
**Priority:** P1
**Status:** 🚧 In progress — D designed refusal first (ADR-0006); D1, the unanswerable questions, done; D2, the served pipeline, next
**Date:** 2026-10-06

---

## Summary
A hand-written agent loop with five tools and enforced budgets, grounding verification, and the
refusal path. ROADMAP §5 checkpoint D.

## Problem Statement
Refusal is currently scored only against its own `UNANSWERABLE` label. That establishes agreement
with your own labelling; it says nothing about whether abstaining raises accuracy on what is
answered. A one-line confidence threshold is the null hypothesis, and if it matches the verifier,
grounding verification is dead weight in the thesis rather than a contribution.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | **Hard deadline — D's first agent run.** Log per query, including refused ones: fused top-1 and top-10 scores, verifier score, citation-precision/correctness outcome (finding 15) | ⬜ | `provenance/eval/runner.py` `PER_QUERY_COLUMNS` |
| 2 | Risk-coverage curve over answerables, plus the same curve for two trivial abstainers (fused-score threshold; empty-evidence rule); quote the coverage level at which headline citation precision is stated | ⬜ | `provenance/eval/metrics.py` |
| 3 | Oversample unanswerables; report refusal recall and false-refusal rate (prevalence-free), derive precision analytically at the 20% base rate (finding 14) | 🚧 | the questions: [agent-refusal/001](../tasks/agent-refusal/001_unanswerable_questions.md) (slice D1), 300 dev and 214 test of each of S3, S4 and S5, built in `provenance/eval/golden/unanswerable.jsonl`; the rates are slice D3's |
| 4 | Publish counts per `UnanswerableStrategy` — with ~40 negatives across five strategies, no per-strategy claim is possible | ✅ | [results/unanswerable_census.json](../../results/unanswerable_census.json): 300 dev and 214 test per kind (slice D1); D3 reports every rate per kind |
| 5 | Five tools, enforced budgets, replayable runs; `budget_exceeded` recorded as a trajectory outcome | ➖ | the five-tool loop is future work ([ADR-0006](../ADR-0006-refusal-first-checkpoint-d.md) §1); D2's single-pass answerer keeps the budgets, the replays and `budget_exceeded` |

## Dependencies
- **Task 1 cannot be done retrospectively.** If D's first agent runs do not log it, finding 15 is
  unanswerable for the rest of the project. This is the single most time-sensitive item outside
  checkpoint C.
- Task 3 feeds [006_EVAL_HARNESS](006_EVAL_HARNESS.md) task 7.

## Verification Steps
1. Multi-hop queries answered within budget, every run replayable from cassettes.
2. Refusal precision **and** recall reported with intervals (ROADMAP §5 checkpoint D gate).
3. Risk-coverage curve shows the verifier beating both trivial abstainers, or the thesis says it
   does not — which is also a result.

## Notes
- **2026-10-06.** Wire hard budget caps before the first live agent run: an in-process budget that
  aborts and records `budget_exceeded` as a trajectory outcome, so evaluation counts it as a failure
  rather than silently retrying.
- **2026-10-06.** The S1-S5 unanswerable taxonomy pre-empts the easy criticism by making negatives
  diverse — but diversity shows refusals are not trivially detectable, not that abstaining is useful.
  Those are different claims; task 2 is the one that tests the second.
- **2026-11-09 — D designed refusal first** ([ADR-0006](../ADR-0006-refusal-first-checkpoint-d.md)).
  Three slices: D1, unanswerable questions (S3 held out, S4 after the pin, S5 nonexistent, 300 dev
  and 214 test each); D2, one warm process that retrieves, reranks, answers with citations or
  declines, verifies grounding and logs every query (task 1 lands there, before the first run);
  D3, claim 5's run. Claim 5's trivial abstainers are the pre-registration's, always answering and
  abstaining at random; task 2's fused-score threshold is reported beside them as finding 15's
  null. The five-tool loop is future work.
- **2026-11-09 — D1 done.** `pace golden unanswerable` built 1,542 questions in their own file,
  300 dev and 214 test of each kind, and `auto_full.jsonl` is unchanged
  ([results/unanswerable_census.json](../../results/unanswerable_census.json)). S5 tried 410
  splices on dev to keep 300: 56 were names lineage had seen, 15 an earlier question's and 39
  mentioned in the pool; on test it tried 433 to keep 214, rejecting 100, 68 and 51. Three rules
  were added to the spec with the code: an S5 name is used once; an S4 function and an S5 name
  serve one split, dev first, because 963 functions have questions in both splits; a name of
  underscores alone is not spliced (22 pairs on dev). S4's months came out 141, 165 and 208,
  October to December: the hash rule's chance (a split that uneven has p ≈ 0.001, and 20 control
  salts showed no bias), kept as specified, since every month is after the pin.

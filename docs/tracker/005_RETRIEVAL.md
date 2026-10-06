# Tracker: Retrieval

**Task(s):** `docs/tasks/retrieval/` — created as tasks are written
**Priority:** P1
**Status:** ⬜ Not started
**Date:** 2026-10-06

---

## Summary
Identifier-aware BM25 (`pg_search`) + dense (pgvector) + RRF + a small cross-encoder, and the
ablation table. ROADMAP §5 weeks 9-10.

## Problem Statement
`provenance/retrieve/__init__.py:13-26` states the dependency precisely: `rerank.py` is
"DELIBERATELY MISSING" because MiniLM "only fits at depth 10 AND only if the model is already
resident". Warm residency is made mandatory at ROADMAP:78 and baked into decision 8 at :96 — and
appears on no week and no cut list.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | One resident process serving both `pace query` and `provenance/eval/runner.py`, so evaluated and served are the same binary by construction (finding 17) | ⬜ | `provenance/api/main.py` lifespan, `provenance/retrieve/rerank.py` |
| 2 | Add the §6 cut-list row: if the worker slips, report RRF-only and say why | ⬜ | `docs/ROADMAP.md` §6 |
| 3 | Record reranker RSS beside the latency table — it was measured in isolation against 2,245 MB already hosting Postgres, pg_search, pgvector and Jaeger | ⬜ | `docs/ROADMAP.md` §2 |
| 4 | Ablation table with each stage attributed | ⬜ | `provenance/eval/runner.py` |

## Dependencies
- Task 1 is a variant of Trap 6 the existing mitigation does not cover: Trap 6 pins hardware and
  depth, but if eval loads the model in-process while serving uses a resident worker, the two
  headline numbers again come from two different systems — differing in process lifetime rather
  than depth.
- Task 4 depends on [006_EVAL_HARNESS](006_EVAL_HARNESS.md) tasks 1-2 (split, MDE).

## Verification Steps
1. Ablation beats the baseline against the pre-registered threshold, each stage attributed.
2. p95 latency within budget on the CPU-served configuration, with the resident worker.

## Notes
- **2026-10-06.** RRF discards scores for ranks, so "each stage attributed" needs care — decide
  whether attribution is by arm-ablation (drop one arm, re-measure) or by rank-contribution, and
  say which in the table caption.
- **2026-10-06.** `dense.py:38-80` already documents that HNSW post-filters rather than pre-filters,
  that `hnsw.iterative_scan` does not fix it, and names the partial-index workaround. Keep that.

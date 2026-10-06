# Tracker: Ingest

**Task(s):** `docs/tasks/ingest/` — created as tasks are written
**Priority:** P0
**Status:** 🚧 In progress
**Date:** 2026-10-06

---

## Summary
Scope freezing, the git walk, GitHub backfill, dead letters. ROADMAP §5 weeks 4-5. Currently
`pace ingest` persists only `repositories`, `ingest_runs` and `dead_letters` — until `commits` /
`files` / `chunks` / `chunk_evidence` are written, every evaluation number is structurally zero.

## Problem Statement
This subsystem owns the denominator of every recall figure in the project. Two review findings
block it, and one is a schedule risk large enough to eat weeks 6-7.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | [Freeze the corpus scope](../tasks/review-remediation/004_scope_freeze.md) (finding 4) | ⬜ | `provenance/ingest/scope.py`, `scope.txt` |
| 2 | Rate-limit budget: filter comment collectors to in-scope PRs; persist bodies during the manifest pass (finding 10) | ⬜ | `scripts/fetch_corpus.py` |
| 3 | `make db-dump` at each ringed gate; private full dump + body-stripped examiner dump (finding 12) | ⬜ | `Makefile`, `docs/RUNBOOK.md` |
| 4 | Commit `artifacts/corpus_facts.json` with the corpus SHA and the command behind each §4 number (finding 18) | ⬜ | `artifacts/corpus_facts.json` |
| 5 | Write `commits` / `files` / `chunks` / `chunk_evidence` — the graph write path | ⬜ | `provenance/ingest/` |

## Dependencies
- Task 1 blocks everything downstream: the scope file list is the recall denominator.
- Task 2 is on the critical path for weeks 4-5 — measured at ~29 h of rate-limited fetching as
  designed, ~25 min once bodies are persisted during the manifest pass.

## Verification Steps
1. `uv run pace scope --check` reproduces 303 files / 81,619 lines.
2. Second ingest run reports zero net writes and an identical chunk-ID set hash (ROADMAP §5 weeks 4-5 gate).
3. `dead_letters` is empty after a clean run.

## Notes
- **2026-10-06.** `0003_github.py:76` already records the stakes of getting the fetch wrong once:
  "re-fetching 40k PRs against a rate limit is not an option". The code knew; the plan had no line
  for it. See finding 10.

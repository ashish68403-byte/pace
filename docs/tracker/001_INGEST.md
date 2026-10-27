# Tracker: Ingest

**Task(s):** [docs/tasks/ingest/](../tasks/ingest/) — 001-003 written 2026-10-27 for checkpoint A
**Priority:** P0
**Status:** 🚧 In progress
**Date:** 2026-10-06

---

## Summary
Scope freezing, the git walk, GitHub backfill, dead letters. ROADMAP §5 checkpoints A (the git walk) and B (the backfill). Currently
`pace ingest` persists only `repositories`, `ingest_runs` and `dead_letters` — until `commits` /
`files` / `chunks` / `chunk_evidence` are written, every evaluation number is structurally zero.

## Problem Statement
This subsystem owns the denominator of every recall figure in the project. Two review findings
block it, and one is a schedule risk large enough to eat checkpoint B.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | [Freeze the corpus scope](../tasks/review-remediation/004_scope_freeze.md) (finding 4) | ✅ | `provenance/ingest/scope.py`, `scope.txt`, `scope_stats.json` |
| 2 | Rate-limit budget: filter comment collectors to in-scope PRs; persist bodies during the manifest pass (finding 10) | ⬜ | `scripts/fetch_corpus.py` |
| 3 | `make db-dump` at each checkpoint gate; private full dump + body-stripped examiner dump (finding 12) | ⬜ | `Makefile`, `docs/RUNBOOK.md` |
| 4 | Commit `artifacts/corpus_facts.json` with the corpus SHA and the command behind each §4 number (finding 18) | ⬜ | `artifacts/corpus_facts.json` |
| 5 | The graph write path. Checkpoint A: [commits and parent links](../tasks/ingest/001_commit_graph.md), then [files and revisions across renames](../tasks/ingest/002_file_history.md). `chunks` / `chunk_evidence` move to checkpoint B | ⬜ | `provenance/ingest/graph_writer.py` |
| 6 | [Measure how much history a current-path view loses](../tasks/ingest/003_history_truncation.md) — checkpoint A's headline number, and `pace history` as its demo | ⬜ | `provenance/analysis/`, `results/` |

## Dependencies
- Task 1 blocks everything downstream: the scope file list is the recall denominator.
- Task 2 is on the critical path for checkpoint B — measured at ~29 h of rate-limited fetching as
  designed, ~25 min once bodies are persisted during the manifest pass.

## Verification Steps
1. `uv run pace scope --check` reproduces 303 files / 81,619 lines.
2. Second ingest run reports zero net writes (checkpoint A's gate), and an identical chunk-ID set hash once
   chunks are written (checkpoint B).
3. `dead_letters` is empty after a clean run.

## Notes
- **2026-10-06.** `0003_github.py:76` already records the stakes of getting the fetch wrong once:
  "re-fetching 40k PRs against a rate limit is not an option". The code knew; the plan had no line
  for it. See finding 10.
- **2026-10-27.** The project is re-planned as checkpoints A → D, each one a complete submission.
  **Checkpoint A** is the history graph (tasks 001-002) plus the truncation measurement (task 003);
  ROADMAP §5 is being re-cut to match. Measured while scoping it: the full first-parent walk is
  **38,888** commits (10 s for git to stream them), with 1,031 merges whose second parents are never
  walked. The walker's docstring had "the full walk reports 17,015" — that is the legacy `airflow/`
  path counted over all parents (15,844 first-parent).

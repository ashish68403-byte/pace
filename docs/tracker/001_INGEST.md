# Tracker: Ingest

**Task(s):** [docs/tasks/ingest/](../tasks/ingest/) — 001-003 written 2026-10-27 for checkpoint A; 004 written 2026-10-27 (personal-data fix)
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
| 2 | Rate-limit budget: fetch only the pool's PRs, one request each plus comment pages (finding 10) — [eval-harness/004](../tasks/eval-harness/004_github_backfill.md) | ✅ | `provenance/ingest/github_backfill.py` |
| 3 | `make db-dump` at each checkpoint gate; private full dump + body-stripped examiner dump (finding 12) | ⬜ | `Makefile`, `docs/RUNBOOK.md` |
| 4 | Commit `artifacts/corpus_facts.json` with the corpus SHA and the command behind each §4 number (finding 18) | ⬜ | `artifacts/corpus_facts.json` |
| 5 | The graph write path. Checkpoint A: [commits and parent links](../tasks/ingest/001_commit_graph.md), then [files and revisions across renames](../tasks/ingest/002_file_history.md). `chunks` / `chunk_evidence` move to checkpoint B | ✅ | `provenance/ingest/graph_writer.py` |
| 6 | [Measure how much history a current-path view loses](../tasks/ingest/003_history_truncation.md) — checkpoint A's headline number, and `pace history` as its demo | ✅ | `provenance/analysis/`, `results/` |
| 7 | [Keep raw email addresses out of the database](../tasks/ingest/004_personal_data.md): checkpoint A's git ingest stored them, contrary to DATA-PROTECTION.md §2 | ✅ | `provenance/privacy.py`, `git_walk.py`, migration 0008 |

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
- **2026-10-27 — the first full ingest, measured** (corpus `1e2ad803f7`). Run 1 (`ingest_runs`
  87, 18 min 07 s): 38,888 commits with `topo_order` 1–38,888; 38,887 first-parent links; 1,031
  links to unwalked parents — exactly one per merge, its second parent; 303 files; 15,421 file
  revisions; 0 rejected commits, 0 dead letters; `rows_inserted` 93,499 = 38,888 + 38,887 + 303 +
  15,421. Run 2 (`ingest_runs` 88, 17 min 55 s): 0 inserted, 0 updated, **93,499 skipped — exactly
  run 1's inserts**, so its zero is a measurement, not an empty run. `insertions` is NULL on every
  commit, never 0. `models/dag.py` has 611 revisions — 516 as `airflow/models/dag.py`
  (2019-04-09 to 2025-03-19) and 95 under today's path — and the move `243fe86d4b` is stored as a
  `renamed` revision with `old_path = airflow/models/dag.py`. Commands: `uv run pace ingest --json`
  twice, then `SELECT count(*), min(topo_order), max(topo_order) FROM commits;`,
  `SELECT count(*) FROM commit_parents;`, `SELECT count(*) FROM files;`,
  `SELECT count(*) FROM dead_letters;`, and
  `SELECT run_id, status, rows_inserted, rows_updated, rows_skipped FROM ingest_runs;`.
- **2026-10-27 — checkpoint A closed.** `pace measure truncation` on the full corpus: a
  current-path view hides **75.6%** of all (file, commit) history (median per file 70.0%, IQR
  25.0–83.3%; 194/303 files hide more than half); 5,243 of 6,810 commits touching in-scope files
  are hidden from *every* current-path lookup; 82.4% of that hidden history references a PR.
  Agreement with `git --follow`: precision 0.9912, recall 0.8657, 191/303 files agree exactly.
  `models/dag.py`: 95 current-path vs 611 rename-aware (84.5% hidden), 611 via `--follow`
  (precision and recall both 1.0) — matches ROADMAP Trap 2 exactly, now with a committed
  derivation (finding 18). Both runs byte-identical (`sha256 b49198dc…`), 303/303 files pass the
  built-in check. **One real defect found and fixed along the way**, in the measurement's check,
  not the walker — see [003_history_truncation.md](../tasks/ingest/003_history_truncation.md)
  Notes for the root cause (3 files where a path was renamed away, then reused by an unrelated
  later file) and the fix (`_explain_path_reuse`, test-driven on a dedicated 4-commit fixture).
- **2026-10-27 — personal data fixed ([004](../tasks/ingest/004_personal_data.md)).** Checkpoint
  A's git walk had stored raw author and committer addresses on all 38,888 commits, and 4,767
  unscrubbed messages, contrary to DATA-PROTECTION.md §2. The parser now redacts first
  (`provenance/privacy.py`, shared with the GitHub fetch); migration 0008 renamed the columns to
  `*_email_sha256`, rewrote the rows in place and added CHECKs. On `pace`: 0 raw addresses
  afterwards; mechanical 2,223 (624 bot-author) unchanged; `results/history_truncation.json`
  regenerated byte-identical.

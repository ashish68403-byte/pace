# Tracker: Symbols and blame

**Task(s):** `docs/tasks/symbols-blame/` — created as tasks are written
**Priority:** P1
**Status:** ⬜ Not started
**Date:** 2026-10-06

---

## Summary
Symbol extraction (tree-sitter + Jedi behind an interface), blame with mechanical-commit filtering,
and the `symbol_versions` layer. ROADMAP §5 weeks 6-7, traps 3 and 4.

## Problem Statement
Trap 3 is measured and sound — 29% of blamed lines land on a mechanical commit while only 0.65% of
in-scope commits are mechanically titled, so a subject-regex filter is cheap and high-yield. The
review's addition is one of attribution, not correctness: the same correction is published prior
work, and currently reads as invented here.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | Blame with `--ignore-revs-file`, **including in the baseline**, or the comparison is a straw man | ⬜ | `provenance/ingest/`, blame layer |
| 2 | Add `commit.is_mechanical`; report the recall improvement as an ablation row | ⬜ | schema + `005_RETRIEVAL` ablation |
| 3 | Cite AG-SZZ (Kim et al. 2006) beside Trap 3's mitigation and Da Costa et al. (TSE 2017) as the model for evaluating a blame correction (finding 13) | ⬜ | `docs/ROADMAP.md` §4 trap 3, §7 |
| 4 | Reframe Cross-cutting as idiom detection over normalised AST hashes rather than `find_references` | ⬜ | ROADMAP §4 trap 4 |

## Dependencies
- Task 1 gates [004_FUNCTION_IDENTITY](004_FUNCTION_IDENTITY.md): the lineage-vs-blame comparison is
  meaningless if the blame arm is the unfiltered one.
- `symbol_versions` is a week-7 deliverable that ADR-0002 currently assumes exists in week 3 — see
  [000_REVIEW_REMEDIATION](000_REVIEW_REMEDIATION.md) finding 1.

## Verification Steps
1. For 100 stratified functions the precomputed chain matches `git log -L` at ≥0.9 commit-set F1
   (ROADMAP §5 weeks 6-7 gate).
2. Precompute completes under 2 h with checkpoint-restart.

## Notes
- **2026-10-06.** `blame -C -C -C` is already measured and rejected: plain 952 ms, `-M` 948 ms
  (free), `-M -C` 3,233 ms. `-M -C` alone recovers a function extracted into an existing file.

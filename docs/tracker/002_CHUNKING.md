# Tracker: Chunking

**Task(s):** `docs/tasks/chunking/` — created as tasks are written
**Priority:** P1
**Status:** ⬜ Not started
**Date:** 2026-10-06

---

## Summary
tree-sitter AST chunking of Python, the token budget, and chunk identity. ROADMAP §5 weeks 6-7.

## Problem Statement
Measured on the in-scope corpus (5,601 chunks): **338 chunks (6.0%) exceed bge-small's 512-token
window** against a conservative lower bound, and splitting a function silently drops comments. An
over-window chunk is truncated, not rejected — so the dense arm sees the first ~20% of the corpus's
most complex code while BM25 reads the full `content`. That degrades the ablation table
asymmetrically and presents as "dense retrieval is weak on long methods", which is plausible enough
to be believed.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | Calibrate `MAX_CHUNK_TOKENS` against the real bge-small tokenizer (p99 WordPiece-per-regex ratio; likely ≈300) or call the tokenizer at ingest (finding 16) | ⬜ | `provenance/parse/chunker.py:125-127` |
| 2 | Size-check the class card; split into attributes card + method-signature index | ⬜ | `chunker.py:345-391` |
| 3 | Make `_split_oversize` descend into an oversize statement's block children | ⬜ | `chunker.py:594-610` |
| 4 | Keep comment nodes in `statements`, attach leading runs to the following statement | ⬜ | `chunker.py:524-527, 612-620` |
| 5 | Two invariant tests: no emitted chunk over budget; a split function's fragments contain every `#` line of the original body | ⬜ | `tests/unit/test_chunker.py` |

## Dependencies
- Task 1 before any embedding run — recalibrating afterwards invalidates every stored vector.
- Interacts with [006_EVAL_HARNESS](006_EVAL_HARNESS.md) finding 7: the recall denominator currently
  moves with the chunker's grouping decision, so touching `MAX_CHUNK_TOKENS` shifts mean recall with
  zero change in retrieval quality. Fix the set-valued relevance there first, or change both together.

## Verification Steps
1. `uv run pytest tests/unit/test_chunker.py -q` — the two new invariants hold.
2. Re-run `chunk_source` over the in-scope corpus; zero chunks exceed the calibrated budget.

## Notes
- **2026-10-06.** Measured gaps: across the 266 functions the chunker splits, inter-fragment gaps
  hold 368 lines — 269 blank, **99 comment-only**, zero code; 38 functions lose at least one comment
  line. No code is lost, so no test or line count would ever have noticed. This violates the
  chunker's own opening rule at `chunker.py:5-8`, which justifies AST chunking on exactly this ground.

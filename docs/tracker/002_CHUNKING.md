# Tracker: Chunking

**Task(s):** `docs/tasks/chunking/` — created as tasks are written
**Priority:** P1
**Status:** ⬜ Not started
**Date:** 2026-10-06

---

## Summary
tree-sitter AST chunking of Python, the token budget, and chunk identity. ROADMAP §5 checkpoint C: [ADR-0003](../ADR-0003-rationale-pool-benchmark.md) moved code chunks out of B, whose benchmark searches rationale text ([eval-harness/001](../tasks/eval-harness/001_benchmark_definition.md)).

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
- **2026-10-27 — census at the frozen scope.** 303 files → 4,122 chunks in 2.1 s (the 5,601 above
  was measured on the pre-freeze 480-file scope). 252 chunks (6.1%) exceed the chunker's own
  400-token estimate, the largest at 4,809; 274 of 3,575 `(path, qualified_name)` keys map to more
  than one chunk. 21 files raise `ChunkParseError("empty after licence-header strip")` — all empty
  `__init__.py` files, which should yield zero chunks rather than a dead letter. All of it is now
  checkpoint C's work (ADR-0003).
- **2026-10-27 — the dependency on 006's set-valued relevance is met.** Recall now counts one unit per
  evidence key (slice B1), so the chunker's grouping decision no longer moves its denominator.

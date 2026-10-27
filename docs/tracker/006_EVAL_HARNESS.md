# Tracker: Evaluation harness

**Task(s):** [docs/tasks/eval-harness/](../tasks/eval-harness/) — 001 (checkpoint B, slice B1) written 2026-10-27
**Decision record:** [ADR-0003](../ADR-0003-rationale-pool-benchmark.md) — the pool is rationale text
**Priority:** P0
**Status:** 🚧 In progress
**Date:** 2026-10-06

---

## Summary
Golden set, leakage gate, metrics, the CI retrieval gate. ROADMAP §5 checkpoint B, and alive from then
on. This is the subsystem the "engineering-measurement project" claim rests on.

## Problem Statement
Seven of the twenty review findings land here. The harness's design is strong — the paired bootstrap
is correct in every detail that usually goes wrong, and "green because nothing was measured" is
treated as a bug class rather than an incident. The findings are about the eval layer not yet
honouring the identity contract its own reasoning establishes.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | [Dev/test split](../tasks/review-remediation/002_dev_test_split.md) (finding 2) — field and `assign_splits` done in [eval-harness/001](../tasks/eval-harness/001_benchmark_definition.md); the runner's `--split` filter and its refusal to update a baseline from test come in B5 | 🚧 | `provenance/eval/schema.py`, `runner.py` |
| 2 | Print `D` (discordant queries) and `1.96·√D/n` beside the paired delta; decide target `n` vs threshold (finding 7) — `mde()` and the sizes done in [eval-harness/001](../tasks/eval-harness/001_benchmark_definition.md); printing D in B5 | 🚧 | `provenance/eval/runner.py:89`, `metrics.py` |
| 3 | Set-valued relevance: `required_units: list[set[str]]`, recall as fraction of units with a member in top k; persist `n_units`, `max_parts_per_unit` (finding 7) — units are evidence keys, done in [eval-harness/001](../tasks/eval-harness/001_benchmark_definition.md) | ✅ | `anchors.py:113-159`, `metrics.py` |
| 4 | `_verdict` takes the **strongest** cheap baseline, not grep alone; gate `retention_rate` too (finding 8) — pre-registered in ADR-0003 §6, implemented in B3 | ⬜ | `scripts/leakage_probe.py:657-660` |
| 5 | Add `baseline_grep_r10`, `gold_symbol_df` to `PER_QUERY_COLUMNS`; second `per_class` breakdown on `gold_symbol_df == 0` vs `> 0` (finding 8) | ⬜ | `runner.py:266-285` |
| 6 | Gold-validity audit: 50-60 stratified records, three-way labels, Wilson interval, self-agreement kappa (finding 6) | ⬜ | new ADR + committed JSONL |
| 7 | Bootstrap the refusal pair; report `None` not 0.0 when `ci.n == 0` (finding 14) | ⬜ | `runner.py:396-399, 413-416` |
| 8 | Define the submission bundle: repo + corpus SHA + ID manifest + frozen `.npz` + body-stripped dump (finding 20) | ⬜ | `docs/ROADMAP.md` §5 Report and Buffer rows |

## Dependencies
- Tasks 1 and 2 **decide checkpoint B's generator target `n`** — do them before the generator runs at
  scale, or not at all.
- Task 3 interacts with [002_CHUNKING](002_CHUNKING.md): today the recall denominator is the
  chunker's grouping decision for the 7.5% of anchors resolving to multiple chunks.
- Task 6 before B's ablation rows are reported.

## Verification Steps
1. `uv run pace eval run --golden … --embeddings none` exits 0 and prints its own MDE.
2. Leakage probe returns a verdict that is not FAIL and not vacuous (`n_retained >= N`).
3. `uv run pytest tests/ -q` green, including the currently-absent `MIN_QUERIES_FOR_PAIRED_GATE` test.

## Notes
- **2026-10-06.** Measured: at n=200, a clean 4-down/0-up flip gives a paired CI of [-4.00, -0.50]
  and is significant — `DEFAULT_THRESHOLD = 0.02` sits almost exactly on the MDE for a monotone
  flip, which is a genuinely good choice. But 8-down/4-up, identical -2.0 net, gives [-5.50, +1.50]
  and is not. Real MDE ≈ 4.4 points at f=0.10, n=200. Detecting 2 points at f=0.10 needs n ≈ 960.
- **2026-10-06.** Do **not** swap the branches at `MIN_QUERIES_FOR_PAIRED_GATE` — failing on the
  point estimate below 100 shared queries is the fail-safe choice and the code documents it. Reword
  `eval/README.md:122-125` and add the missing test instead.
- **2026-10-27 — checkpoint B re-founded on a rationale pool.** Planning B found that ADR-0002's
  measured grep recall (0.175) was taken over a commit-message pool, while the harness searches code
  chunks, on which a symbol-naming question is answered by grep at rank 1. ADR-0003 makes the pool
  rationale text (commit messages first, PR text after the GitHub backfill), scores one relevance unit
  per evidence key, and pre-registers the gate (≥ 100 retained dev records; Wilson 95% upper bound on
  the strongest lexical baseline's recall@10 < 0.35; retention ≥ 25%) and the sizes (≥ 360 dev,
  ≥ 240 test, from MDE ≈ 1.96·√(f/n)). Slice plan B1–B5 is in
  [eval-harness/001](../tasks/eval-harness/001_benchmark_definition.md).
- **2026-10-27 — slice B1 closed.** `split` is required on every golden record and assigned per PR
  group by a seeded hash (`assign_splits`); the runner's `recall_at_k` column is unit recall, beside
  `n_units` and `max_parts_per_unit`; `mde` and `wilson_interval` pin ADR-0003's numbers in tests;
  migration 0007 allows `self` and `linked` evidence. Three items stay open, each with an owner:
  - **The CI golden subset is empty.** The week-1 seeds are retired (ADR-0003, Consequences), so
    CI's retrieval gate scores nothing until a generated dev subset and the fixture pool it needs
    are committed. Owner: B5.
  - **nDCG still counts documents.** Harmless while a change has one document (B2, B3); it must
    become unit-aware, or be reported with that caveat, before PR text joins the pool. Owner: B5.
  - **A PR that landed in more than one commit** lets a record score 0.5 (ADR-0003 §4). Owner: B2,
    which counts such PRs in the commit-message pool.

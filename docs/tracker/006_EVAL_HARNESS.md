# Tracker: Evaluation harness

**Task(s):** `docs/tasks/eval-harness/` — created as tasks are written
**Priority:** P0
**Status:** 🚧 In progress
**Date:** 2026-10-06

---

## Summary
Golden set, leakage gate, metrics, the CI retrieval gate. ROADMAP §5 weeks 2-3, alive all fourteen
weeks. This is the subsystem the "engineering-measurement project" claim rests on.

## Problem Statement
Seven of the twenty review findings land here. The harness's design is strong — the paired bootstrap
is correct in every detail that usually goes wrong, and "green because nothing was measured" is
treated as a bug class rather than an incident. The findings are about the eval layer not yet
honouring the identity contract its own reasoning establishes.

## Task Breakdown

| # | Task | Status | File |
|---|------|--------|------|
| 1 | [Dev/test split](../tasks/review-remediation/002_dev_test_split.md) (finding 2) | ⬜ | `provenance/eval/schema.py`, `runner.py` |
| 2 | Print `D` (discordant queries) and `1.96·√D/n` beside the paired delta; decide target `n` vs threshold (finding 7) | ⬜ | `provenance/eval/runner.py:89`, `metrics.py` |
| 3 | Set-valued relevance: `required_units: list[set[str]]`, recall as fraction of units with a member in top k; persist `n_units`, `max_parts_per_unit` (finding 7) | ⬜ | `anchors.py:113-159`, `metrics.py` |
| 4 | `_verdict` takes the **strongest** cheap baseline, not grep alone; gate `retention_rate` too (finding 8) | ⬜ | `scripts/leakage_probe.py:657-660` |
| 5 | Add `baseline_grep_r10`, `gold_symbol_df` to `PER_QUERY_COLUMNS`; second `per_class` breakdown on `gold_symbol_df == 0` vs `> 0` (finding 8) | ⬜ | `runner.py:266-285` |
| 6 | Gold-validity audit: 50-60 stratified records, three-way labels, Wilson interval, self-agreement kappa (finding 6) | ⬜ | new ADR + committed JSONL |
| 7 | Bootstrap the refusal pair; report `None` not 0.0 when `ci.n == 0` (finding 14) | ⬜ | `runner.py:396-399, 413-416` |
| 8 | Define the submission bundle: repo + corpus SHA + ID manifest + frozen `.npz` + body-stripped dump (finding 20) | ⬜ | `docs/ROADMAP.md` §5 weeks 15-16 |

## Dependencies
- Tasks 1 and 2 **decide the week-3 generator's target `n`** — do them before the generator runs at
  scale, or not at all.
- Task 3 interacts with [002_CHUNKING](002_CHUNKING.md): today the recall denominator is the
  chunker's grouping decision for the 7.5% of anchors resolving to multiple chunks.
- Task 6 before week 9 locks the ablation.

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

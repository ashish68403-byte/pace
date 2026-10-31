# Tracker: Evaluation harness

**Task(s):** [docs/tasks/eval-harness/](../tasks/eval-harness/) — 001 (checkpoint B, slice B1) written 2026-10-27; 002 (slice B2, the commit-message pool) written 2026-10-27; 003 (slice B3, generator and verdict) written 2026-10-29; 004 (slice B4, the GitHub backfill) written 2026-10-29; 005 (the gold-validity audit, finding 6) written 2026-10-31
**Decision records:** [ADR-0003](../ADR-0003-rationale-pool-benchmark.md) — the pool is rationale text; [ADR-0004](../ADR-0004-gold-validity-audit.md) — how much of the gold explains its function
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
| 4 | `_verdict` takes the **strongest** cheap baseline, not grep alone; gate `retention_rate` too (finding 8) — ADR-0003 §6's verdict, implemented in [eval-harness/003](../tasks/eval-harness/003_golden_set_generator.md): dev only, Wilson upper bound, retention, stale facts refuse to pass | ✅ | `provenance/eval/leakage.py` (`verdict`) |
| 5 | Add `baseline_grep_r10`, `gold_symbol_df` to `PER_QUERY_COLUMNS`; second `per_class` breakdown on `gold_symbol_df == 0` vs `> 0` (finding 8) | ⬜ | `runner.py:266-285` |
| 6 | Gold-validity audit: 50-60 stratified records, three-way labels, Wilson interval, self-agreement kappa (finding 6) — specified in [eval-harness/005](../tasks/eval-harness/005_gold_validity_audit.md): 56 dev records, a two-step label for PR-linked ones, and two companions that need no labeller | ⬜ | [ADR-0004](../ADR-0004-gold-validity-audit.md), `provenance/eval/gold_validity.py`, `provenance/eval/audit/` |
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
- **2026-10-28 — slice B2 closed.** `pace pool build` writes the message of each of the 6,810
  pool commits as a `kind='commit'` document, with 6,810 `self` and 5,963 `linked` evidence
  rows; a second run inserts nothing. A PR number is linked only from commit `1f63537c9b` on
  (apache/airflow's numbering) and only at ≥ 10% of the highest PR linked before it: 138
  earlier-repository references and 1 fork-internal reference are dropped, each listed in
  `results/pool_census.json`. The leakage probe now pools `RATIONALE_KINDS` only. B1's open
  item **"a PR that landed in more than one commit"** is closed: no PR has two documents, and
  the 31 apparent doubles were numbering collisions.
- **2026-10-29 — slice B3 closed.** `pace golden generate` turns the in-scope history's AST diff
  into 8,250 questions (2,378 "added", 5,872 "changed"; every rule's count is in
  `results/golden_census.json`), each carrying its leakage facts measured against the pool. 7,426
  (90.0%) pass ADR-0003 §5's filter, 4,324 of them dev. The verdict reads dev only and measures
  every fact again: **PASS**. The strongest lexical baseline, bm25_ident, has recall@10 0.110 and
  a Wilson 95% upper bound of 0.120 (`results/leakage_verdict.json`). 45% of retained dev
  questions name a symbol that no pool document contains. Found while planning and fixed before
  any number existed: `rank()` broke ties by partition order, and counted documents that matched
  nothing as retrieved.
- **2026-10-31 — slice B4 closed.** The dated self-assessment (DATA-PROTECTION.md §7, 2026-10-29)
  was committed before the first request. `pace github backfill` fetched 5,962 of the pool's 5,963
  linked PRs (1 number was not a PR on GitHub any more) in 13,435 requests (2.25 per PR), storing
  38,026 review comments and 18,972 conversation comments as documents, emails scrubbed and no raw
  API responses kept. The pool grew to 69,769 documents (5,961 descriptions + 38,026 review +
  18,972 conversation, beside B2's 6,810 commit messages); 3,226 bot-written texts were left out.
  B2's commit-subject links agree with GitHub's own merge commit for 5,686 PRs (275 outside the
  graph — first-parent-only history and squash/rebase merges account for the gap, a known limit,
  not a bug). Regenerated on the grown pool, the golden set retains 7,199 of 8,250 (87.3%), 4,206
  dev; the verdict is **PASS**: bm25_ident recall@10 0.140, Wilson upper bound 0.151 against the
  0.35 ceiling.

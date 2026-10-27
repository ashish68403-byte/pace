# Task 001: Define the benchmark — rationale pool, relevance units, split, sizes

**Tracker:** [006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) · **checkpoint B**, slice B1 of 5
**Priority:** P0 · **Status:** ⬜ Not started
**Decision record:** [ADR-0003](../../ADR-0003-rationale-pool-benchmark.md)
**Deadline:** ADR-0003 committed before slice B3's generator writes its first record

## User Story

- **Summary:** Fix what the benchmark searches, what counts as a right answer, how records are
  split, and how many are needed — in writing and in the schema — before any golden record is
  generated, so that checkpoint B's numbers cannot be shaped after they are seen.

### Use Case

- **As the** author defending checkpoint B's benchmark
- **I want** the pool, the scoring unit, the split and the gate pre-registered and enforced in code
- **so that** "did you decide that before or after you saw the number?" is answered by `git log`.

### Background (measured)

See ADR-0003's Context. In short: the harness searches code chunks while ADR-0002 measured a
commit-message pool; `recall_at_k` scores a fraction of documents, which collapses once one PR
owns many documents; and checkpoint A already holds the 6,810 in-scope commit messages the
benchmark can start from. Slices B2–B5 build on this one:

| Slice | Delivers | Depends on |
|---|---|---|
| **B1** (this task) | ADR-0003, `split`, unit recall, `mde()` / Wilson helpers, migration 0007, doc updates | — |
| B2 | Commit messages written as `kind = 'commit'` chunks with `self` / `linked` evidence | B1 |
| B3 | Generator v0, retention, the first leakage verdict on the commit-message pool | B1, B2 |
| B4 | Ethics statement, GitHub backfill under a request budget; PR text joins the pool | B1 (runs in parallel) |
| B5 | grep / BM25 / dense / RRF retrievers through the runner; first ablation rows on dev | B3 (B4 for the full pool) |

## Acceptance Criteria

- **Scenario:** the decision predates the data
  - **Given:** ADR-0003
  - **When:** `git log --diff-filter=A -- docs/ADR-0003-rationale-pool-benchmark.md` is compared
    with the first commit that adds a generated golden record
  - **Then:** the ADR's commit is earlier, and its Status reads Accepted.

- **Scenario:** every record carries its split
  - **Given:** `provenance/eval/schema.py`
  - **When:** a golden record without `split` is loaded
  - **Then:** loading fails (`extra="forbid"` stays); `split` accepts only `dev` or `test`.

- **Scenario:** a split is deterministic and grouped
  - **Given:** a list of records with query classes and group keys (PR number, else commit sha)
  - **When:** `assign_splits(records, seed)` runs twice, or over the records in another order
  - **Then:** every record gets the same split both times; no group appears on both sides; and a
    report gives each query class's dev share (expected 55–65% on real data; on a synthetic set of
    1,000 groups the overall share is within 0.55–0.65).

- **Scenario:** recall counts answers, not documents
  - **Given:** a record whose one required key resolves to 30 documents
  - **When:** one of the 30 is in the top 10
  - **Then:** `unit_recall_at_k` is 1.0, where `recall_at_k` would have been 1/30; and for a record
    with two units of which one is hit, it is 0.5.
  - **And:** the runner's per-query table records `n_units` and `max_parts_per_unit`, and its
    `recall_at_k` column is unit recall.

- **Scenario:** a record whose evidence resolves one document per key scores as before
  - **Given:** a record whose every required key resolves to exactly one chunk
  - **When:** it is scored
  - **Then:** unit recall equals the old fraction recall — the change is invisible where it should be.

- **Scenario:** the sizes are computed, not asserted
  - **Given:** `mde(n, f)` and `wilson_interval(successes, n)` in `provenance/eval/metrics.py`
  - **When:** called with the ADR's numbers
  - **Then:** `mde(200, 0.10) ≈ 0.044`, `mde(360, 0.10) ≈ 0.033`, `mde(960, 0.10) ≈ 0.020`, and
    the Wilson 95% interval for 18 successes in 100 has an upper bound ≈ 0.267 — each pinned by a
    unit test to three decimals.

- **Scenario:** the schema accepts the new evidence sources
  - **Given:** migration `0007_rationale_evidence`
  - **When:** `alembic upgrade head` runs, and the CI-style double round-trip runs in a throwaway
    database
  - **Then:** `chunk_evidence.source` accepts `self` and `linked` (and still rejects anything not
    listed); the downgrade removes them only when no row uses them, and fails loudly otherwise.

- **Scenario:** the retired seeds fail nothing silently
  - **Given:** `provenance/eval/golden/ci_subset.jsonl`
  - **When:** its three hand-written records are removed
  - **Then:** the file's header says why and names slice B5 as the owner of its replacement, and
    the tracker records the empty CI set as an open item with a date.

- **Scenario:** the plan matches the decision
  - **Given:** ROADMAP §5's checkpoint B row and §6
  - **When:** they are read
  - **Then:** they say the pool is rationale text, list B1–B5, and move code chunks to C-prep;
    trackers 002 and 006 point at this task and ADR-0003.

## Files to Modify

- `docs/ADR-0003-rationale-pool-benchmark.md` — new (this design)
- `provenance/eval/schema.py` — `split` on `GoldenRecord`; `assign_splits()`
- `provenance/eval/anchors.py` — `ResolvedAnchors.required_units` (per-key chunk sets)
- `provenance/eval/metrics.py` — `unit_recall_at_k`, `mde`, `wilson_interval`
- `provenance/eval/runner.py` — the `recall_at_k` column becomes unit recall; `n_units`,
  `max_parts_per_unit` columns
- `migrations/versions/0007_rationale_evidence.py` — new
- `provenance/eval/golden/ci_subset.jsonl` — seeds retired, header explains
- `tests/unit/test_metrics.py`, `tests/unit/test_golden_schema.py` (new),
  `tests/integration/test_schema.py` — the scenarios above
- `docs/ROADMAP.md` §5–§6, `docs/tracker/002_CHUNKING.md`, `docs/tracker/006_EVAL_HARNESS.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests -q
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    # migration round-trip, twice, in a throwaway database (never against `pace`)
    docker exec pace-db createdb -U postgres pace_roundtrip
    PACE_DATABASE_URL=postgresql://postgres:pace@localhost:5432/pace_roundtrip bash -c \
      'for i in 1 2; do uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head; done'
    docker exec pace-db dropdb -U postgres pace_roundtrip

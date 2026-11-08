# Task 006: Score grep and BM25 through the runner, and write the ablation table's first rows

**Tracker:** [006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) rows 1, 2 and 5, and the two
open notes B1 gave B5 · **checkpoint B**, slice B5a (the first half of B5)
**Priority:** P0 · **Status:** ✅ Done (2026-11-08)
**Decision records:** [ADR-0003](../../ADR-0003-rationale-pool-benchmark.md) §4–§7, and its third
addendum (2026-11-07, in the spec commit); [ADR-0004](../../ADR-0004-gold-validity-audit.md) §8
(every lift is quoted twice)
**Depends on:** [003](003_golden_set_generator.md) (the golden set and the probe),
[004](004_github_backfill.md) (the full pool) and [005](005_gold_validity_audit.md) stage 2 (the
validity rate)

## User Story

- **Summary:** Score three lexical retrievers through the runner's own scoring, on the 4,206
  retained dev records: grep, the leakage probe's identifier-aware BM25, and the production BM25
  index. Commit the ablation table's first rows, and fix the runner where tracker 006 says B5
  must. Dense retrieval and RRF follow in slice B5b.

### Use Case

- **As the** author defending checkpoint B
- **I want** each lexical retriever's recall, MRR and nDCG on the benchmark, with intervals, with
  paired differences against the baseline the verdict named, and with each difference quoted
  against the measured validity of the gold
- **so that** "how well do the obvious methods do?" has a committed, reproducible answer, and
  B5b's dense and RRF rows have something to be compared against.

### Background (measured 2026-11-07)

- **The runner.** `pace eval run` takes a retriever plug-in (`module:attr` with
  `.retrieve(question, qvec, k)`), resolves gold through `anchors.resolve_all`, scores one row per
  record and gates against a committed baseline. Four gaps remain from the review:
  - it has no split filter;
  - it scores every record it is given, including generated records the leakage filter dropped;
  - its nDCG counts documents, so a PR with thirty comments can pad a score;
  - its paired comparison prints neither D nor the MDE.
- **The verdict's baselines** (`results/leakage_verdict.json`), on the 4,206 retained dev records:
  - recall@10 for grep is 0.1025 (431 hits), for default BM25 0.0506 (213), and for
    identifier-aware BM25 0.1398 (588);
  - the verdict names `bm25_ident` as the baseline every lift is quoted against (ADR-0003 §6).
- **The production BM25 index.** `retrieve.lexical.lexical_search` runs BM25 over `chunks` in
  pg_search, on three fields: the content, the qualified name as a literal, and the lexical
  blob. It returns rationale documents today only because `chunks` holds nothing else.
  Checkpoint C adds code chunks.
- **A record's unit recall is 0 or 1.** Since slice B4, every document of a change satisfies both
  of a record's units, the commit and the PR. So unit recall@k equals the probe's hit rate, and
  the runner must reproduce the verdict's 431 and 588 exactly.
- **CI's database is empty,** `golden/ci_subset.jsonl` is empty, and DATA-PROTECTION.md §3
  commits no pool text.
- **The validity of the dev set's gold** is 95.5%, with a 95% interval of 84.9% to 98.8%
  (`results/gold_validity_audit.json`).
- **Decisions made with the user, 2026-11-07:**
  - B5 is two slices: B5a lexical (this task), B5b dense and RRF;
  - three rows: `grep`, `bm25_ident` and `bm25`;
  - CI keeps checking code paths only, recorded in an ADR-0003 addendum;
  - one scoring path: the ablation runs through the runner's own scoring.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/eval/runner.py` | DB, then pure | The `--split` option; scoring only retained records; new per-query columns and the df breakdown; D and the MDE in the paired comparison; the `pace eval ablation` command, a thin wrapper. |
| `provenance/eval/metrics.py` | pure | `unit_ndcg_at_k`: nDCG over relevance units. |
| `provenance/eval/lexical_baselines.py` | DB, then pure | `GrepRetriever` and `Bm25IdentRetriever`, over the probe's own `PoolIndex`. |
| `provenance/retrieve/lexical.py` | DB | A `kinds` filter on `lexical_search`; `Bm25Retriever`. |
| `provenance/eval/ablation.py` | DB, then pure | The rows, the comparisons, the validity division, the cross-check, and the two results files. |

### The runner

- **`--split dev|test|all`,** default `dev`. The runner scores only that split.
  `--update-baseline` refuses whenever test is included: test is opened at most twice in the
  project (ADR-0003 §7).
- **Only retained records are scored** (`passes_leakage_filter`), and the runner prints how many
  it skipped. ADR-0003 §5 filters before anything is scored.
- **Per-query rows** gain three columns:
  - `gold_symbol_df` and `baseline_grep_r10`, read from the record's leakage facts (tracker row 5);
  - `first_gold_rank`, the rank of the first required document in the top k, or empty.
- **A second breakdown,** beside the query classes, by the symbol's document frequency: df = 0
  against df > 0, each with its n and recall@k and interval. df = 0 is ADR-0002's thesis stratum.
  Records with no measured df are counted, not guessed.
- **The paired comparison prints D,** the number of queries where the two runs' per-query recall
  differs, **and the MDE**, `metrics.mde(n, D / n)`, which is 1.96·√D / n (tracker row 2).
- **nDCG counts relevance units** (`unit_ndcg_at_k`):
  - a required unit (gain 2) counts once, at the rank of its first retrieved document;
  - supporting evidence has no units, and keeps its per-document gain of 1;
  - the ideal ranking is built greedily, each rank holding the document that covers the most
    remaining gain. It is exact when a record has at most two units, which is every generated
    record. When a commit and its PR share their documents, one document satisfies both at
    rank 1.

### The retrievers

- **`grep` and `bm25_ident`** wrap the leakage probe's `PoolIndex` over `load_pool_from_db`, and
  rank exactly as the probe's `_rankings` does: only documents that matched, best first.
  - A retriever built alone loads the pool on its first query.
  - `pace eval ablation` builds one index and gives it to both.
- **`bm25`** is the production index. `lexical_search` gains
  `kinds: Sequence[str] | None = None`, and `Bm25Retriever` passes the pool's kinds
  (`RATIONALE_KINDS`), so the row keeps searching only the pool once checkpoint C adds code
  chunks.
- **All three** satisfy the runner's `Retriever` protocol, so `pace eval run --retriever`
  accepts each of them.

### The ablation

`pace eval ablation [--golden PATH] [--out PATH] [--per-query-out PATH] [--verdict PATH]`:

- **Records:** the retained dev records of `provenance/eval/golden/auto_full.jsonl`. k = 10.
  Gold is resolved once. Each row is scored with the runner's own `_score_records`, then
  `aggregate`.
- **`results/ablation.json`** has sorted keys and no run ids, timestamps or latencies, so it
  reproduces byte for byte. It holds:
  - the corpus commit, the pool version, the golden path, the split, k and the record count;
  - per row: hits@10; recall@10, MRR@10 and nDCG@10, each with its bootstrap interval; the
    same per query class and per df stratum;
  - the reference row, `bm25_ident`, and for every other row its paired difference in recall@10
    against it: the interval, D, the MDE, and whether it is significant;
  - per ADR-0004 §8, each difference quoted twice: raw, and divided by the validity rate at its
    point estimate and at both ends of its interval;
  - the validity's source and status: `current`, `missing`, `incomplete`, or `stale` when the
    audit was measured on another pool version. Without a current validity, the divided figures
    are absent and the status says why.
- **`results/ablation_per_query.jsonl`:** one line per record, sorted by qid, holding its query
  class, its df, and each row's `first_gold_rank`. Every number in `ablation.json` can be
  recomputed from it.
- **The cross-check.** Before anything is written, the hits@10 of `grep` and `bm25_ident` must
  equal the verdict file's `hits_at_10`. The verdict must also have scored the same number of
  records over a pool of the same size. Otherwise the command fails and writes nothing: the
  runner and the probe are two computations of one number, and they must agree.
- **A printed table** shows each row's recall@10 with its interval, MRR, nDCG, and its difference
  against `bm25_ident` with D and the MDE.

### CI, and ADR-0003's third addendum

- **The addendum (2026-11-07)** records that CI scores no retrieval, and why. Scoring needs the
  pool in CI's database, and DATA-PROTECTION §3 commits no pool text. The benchmark's numbers
  are produced locally and committed as results files that reproduce byte for byte, rebuildable
  from the committed golden set and manifest plus `pace github backfill`. It also records the
  two options not chosen: a scheduled CI job that clones apache/airflow and builds its own
  commit-message pool, and a committed commit-message fixture.
- **The CI workflow's comment and the runner's empty-set message** point at the addendum instead
  of "until B5".

### Not in this slice

- **Dense retrieval and RRF** (embedding the pool, frozen query vectors, the dense and fused
  rows): slice B5b.
- **The test split:** never opened.
- **Refusal and citation metrics:** tier 2, checkpoint D.
- **Retuning `bm25`'s field boosts** on these numbers: tuning on dev is B5b's work, with RRF's k.

## Acceptance Criteria

- **Scenario:** the runner scores one split, retained records only
  - **Given:** a golden file with dev and test records, and a dropped generated record
  - **Then:**
    - `--split dev` scores only dev records and prints how many it skipped as dropped;
    - `--update-baseline` refuses with `--split test` and with `--split all`.

- **Scenario:** nDCG counts answers
  - **Then:**
    - two units sharing their documents, first gold at rank 3, give 1/log2(4);
    - two units with separate documents found at ranks 1 and 2 give 1.0, while a ranking that
      finds only one of them gives less;
    - a PR's thirty documents in the top 10 score no higher than one of them at the same first
      rank;
    - no retrieved gold gives 0, and no units gives NaN.

- **Scenario:** the comparison says whether a difference was measurable
  - **Then:** the paired comparison's message names D and the MDE, and the MDE equals
    `metrics.mde(n, D / n)`.

- **Scenario:** the new columns and the df breakdown
  - **Then:** every per-query row carries `gold_symbol_df`, `baseline_grep_r10` and
    `first_gold_rank`; the aggregate has df = 0 and df > 0 strata, and counts records without a
    df.

- **Scenario:** the pool and only the pool
  - **Given:** `history_repo`'s pool, plus a planted chunk of another kind containing the query's
    words
  - **Then:** `lexical_search(..., kinds=RATIONALE_KINDS)` never returns the planted chunk, and
    each of the three retrievers returns only pool documents.

- **Scenario:** the ablation file
  - **Given:** fake retrievers over synthetic records
  - **Then:**
    - two runs write the same bytes;
    - a hits@10 that disagrees with the verdict file fails, and writes nothing;
    - with the audit file missing, incomplete or on another pool version, the validity status
      says so and the divided figures are absent.

- **Scenario:** the real run
  - **Given:** `pace`
  - **Then:**
    - `pace eval ablation` reproduces the verdict's 431 and 588;
    - it writes `results/ablation.json` and `results/ablation_per_query.jsonl`;
    - a second run writes the same bytes.

- **Scenario:** the plan documents agree
  - **Then:**
    - ADR-0003's third addendum is committed with this spec;
    - tracker 006 rows 1, 2 and 5 are ✅, and its two B5 notes are closed;
    - `results/README.md` lists both files;
    - the eval README and ROADMAP describe B5a.

## Files to Modify

- `provenance/eval/lexical_baselines.py`, `provenance/eval/ablation.py`: new
- `provenance/eval/runner.py`, `provenance/eval/metrics.py`, `provenance/retrieve/lexical.py`
- `results/ablation.json`, `results/ablation_per_query.jsonl`: new
- Tests: `tests/unit/test_ablation_rules.py`, `tests/integration/test_lexical_baselines.py`
  (new); `tests/unit/test_metrics.py`, `tests/unit/test_eval_runner.py`,
  `tests/unit/test_relevance_units.py`
- In the spec commit: `docs/ADR-0003-rationale-pool-benchmark.md` (third addendum),
  `docs/tracker/006_EVAL_HARNESS.md` (header)
- Later: `.github/workflows/ci.yml` (comment), `provenance/eval/README.md`, `docs/ROADMAP.md`,
  `docs/tracker/006_EVAL_HARNESS.md`, `results/README.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests -q
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace eval ablation          # twice: both files byte-identical
    uv run pace eval run --golden provenance/eval/golden/auto_full.jsonl \
        --retriever provenance.retrieve.lexical:Bm25Retriever --embeddings none --no-invariants

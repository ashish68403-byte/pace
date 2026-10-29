# Task 003: Generate the golden set from the code diff, and give the first leakage verdict

**Tracker:** [006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) · **checkpoint B**, slice B3 of 5
**Priority:** P0 · **Status:** ⬜ Not started
**Decision records:** [ADR-0002](../../ADR-0002-golden-set-construction.md) (the anchor comes from the
code); [ADR-0003](../../ADR-0003-rationale-pool-benchmark.md) §3–§8 and its 2026-10-29 addendum
**Depends on:** [001](001_benchmark_definition.md) (split, units, `wilson_interval`) and
[002](002_commit_message_pool.md) (the pool and its PR links)

## User Story

- **Summary:** Generate every question ADR-0003 §3 allows from the AST diff of the in-scope
  history, measure each one's leakage against the commit-message pool at generation time, commit
  the whole candidate set with its outcomes, and compute checkpoint B's leakage verdict on the dev
  split exactly as §6 pre-registered it.

### Use Case

- **As the** author defending checkpoint B's benchmark
- **I want** a generator whose every candidate, rule and drop is counted, and a verdict that cannot
  pass green by measuring nothing, by measuring the weakest baseline, or by filtering until only
  easy-to-miss questions remain
- **so that** "did you try grep?" and "what did your filter throw away?" both have committed answers.

### Background (measured 2026-10-29 at design time, with a throwaway script; the generator's census is the authority)

- **Supply.** 15,421 in-scope revisions; 13,246 belong to non-merge, non-mechanical commits and
  were diffed. 84 blobs do not parse (2015-era Python 2), so 93 revisions have an unknown side and
  are skipped.

  | | "Added" | "Changed" |
  |---|---|---|
  | Raw events from the AST diff | 5,366 | 16,328 |
  | Symbol still in the same file at HEAD | 2,803 | 8,967 |
  | Not a cross-file move (ADR §3) | 2,493 | — |
  | Added exactly once in the file's history (addendum) | 2,378 | — |
  | One qualifying commit per (symbol, month) (ADR §3) | — | 6,059 |
  | Name unique across HEAD files (addendum) | — | 5,872 |

- **Retention estimate** (ADR-0002 steps 4–5, run with the probe's own code, before the second
  addendum rule): "added" keeps 1,980 of 2,378 (135 answered by grep at rank 1, 263 share a rare
  token); "changed" keeps 5,622 of 6,059 (244 and 193). That is about 7,600 retained records and
  90% retention, against ADR-0003 §8's minimums of 360 dev and 240 test. The dev share is 58–59%
  per template.
- **The baselines' recall@10 was deliberately not measured at design time.** It is the gate, and
  designing after seeing it would undo the pre-registration.
- **Probe facts.** `_verdict` checks grep alone, ignores the split, has no interval and no retention
  condition (review finding 8). `passes_leakage_filter` still encodes the pre-ADR-0003 rule (grep
  recall ≤ 0.35 per record), and nothing calls it. `pace leakage` already tries
  `provenance.eval.leakage` before falling back to the script.
- **Golden set.** `provenance/eval/README.md` already names the generated set
  `provenance/eval/golden/auto_full.jsonl`.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/parse/symbols.py` | pure | `symbols(text, path)`: `{qualified_name: full_text}` from the chunker. Functions and methods (`Class.method`); an oversized function's fragments are rejoined in part order; class cards and module chunks are excluded; `None` when the chunker refuses the file. `diff_symbols(before, after)`: added, changed and disappeared names. *Changed* means the comment- and layout-blind token hash (`hash_ladder(...).h1`) differs, after a cheap equal-text short-circuit. |
| `provenance/eval/candidates.py` | DB + git, then pure | Walk each in-scope file's revisions in topo order, reading blobs through `CatFileBatch`. Every revision updates the file's symbol state (`{}` when the file is absent, `None` when unknown); only non-merge, non-mechanical commits emit events. Then the five rules below as pure functions, each counted. |
| `provenance/eval/leakage.py` | moved | `scripts/leakage_probe.py`, moved unchanged into the package; the script becomes a shim. It gains `measure(question, gold_documents)`, which returns one record's leakage facts, and the ADR-0003 §6 verdict. |
| `provenance/eval/generator.py` | mostly pure | Candidate → `GoldenRecord` → `auto_full.jsonl` and `results/golden_census.json`. `pace golden generate`. |

### The rules (ADR-0003 §3, plus the 2026-10-29 addendum)

1. **At HEAD:** the qualified name exists at HEAD in the same rename-aware file.
2. **Not a move** ("added" only): no symbol with the same short name disappears from another
   in-scope file in the same commit.
3. **Added once** ("added" only, addendum): the name was added exactly once in that file's
   history.
4. **One commit per month** ("changed" only): exactly one qualifying commit changed the
   (file, symbol) in that UTC month.
5. **Unique name** ("changed" only, addendum): the qualified name exists in exactly one file at
   HEAD, because the template does not name the file.

### Records

| Field | Value |
|---|---|
| `question` | ``Why was `X` added to `path`?`` (`provenance`) or ``Why did `X` change in <Month YYYY>?`` (`archaeology`). `X` is the qualified name, `path` is today's path, and the month is `committed_at` in UTC. |
| `qid` | `<added\|changed>-<sha[:12]>-<sha256(path::X)[:8]>`, so the same candidate always gets the same id. |
| `required_evidence` | the commit, plus its PR when B2 linked one. |
| `supporting_evidence` | the symbol anchor (today's path, X), kept for checkpoint C. |
| `split` | `assign_splits(group_key(sha, pr))`. |
| `generation_strategy` | `ast_diff_v0`. |
| `corpus_commit` | the corpus HEAD. |
| `leakage` | `baseline_grep_r10` and `baseline_bm25_r10` (this record's hit@10; BM25 is the identifier-aware variant); `gold_symbol_df` (pool documents containing X's short name); `grep_answers_at_rank_1` and `rare_tokens_shared_with_gold` (**new**); `probe_index_version` (`commit-message-v1:<pool size>`); `probe_corpus_commit`. `probed_at` stays empty. |

`auto_full.jsonl` holds **every candidate**, sorted by `qid`. `passes_leakage_filter` becomes
ADR-0003 §5's rule: a generated record is retained iff `grep_answers_at_rank_1` is false and
`rare_tokens_shared_with_gold` is empty, and missing facts fail closed. `results/golden_census.json`
holds counts and identifiers only:
- the corpus commit;
- revisions diffed and skipped, and parse failures;
- each rule's survivors per template;
- retention outcomes per template;
- each class's dev share;
- the share of retained records whose `gold_symbol_df` is 0;
- up to 20 dropped `qid`s per reason.

### The verdict (ADR-0003 §6; review finding 8)

The verdict reads the **dev** split only. It recomputes every record's two leakage facts against
the current pool.

- **INCONCLUSIVE** when any recomputed fact differs from the stored one (the pool changed since
  generation: regenerate), or fewer than 100 dev records are retained.
- **FAIL** when dev retention is below 25%, or the strongest lexical baseline's Wilson 95% upper
  bound on recall@10 is at least 0.35. The lexical baselines are grep, BM25-default and
  BM25-ident.
- **PASS** otherwise.

Dense is reported and never enters the verdict. The report adds ADR-0002's thesis statistic: the
share of retained records whose symbol name is in no pool document, with each baseline's recall@10
split by that stratum. Output is sorted, with no timestamps. Exit codes are unchanged (0 PASS,
1 FAIL, 2 INCONCLUSIVE; `--allow-inconclusive` maps 2 to 0).

### Not in this slice

- **The gold-validity audit** (review finding 6): 50–60 labelled records. It gates slice B5's
  ablation rows, not this verdict.
- **The runner's split filter and its per-stratum breakdown** (tracker 006 rows 1 and 5): slice B5.
- **Regenerating after the pool grows** (slice B4). The verdict's stored-fact check makes a stale
  set refuse to pass.

## Acceptance Criteria

- **Scenario:** a symbol's change is a change of tokens, not of layout
  - **Given:** a function whose only edit is a comment or a re-indent, and one whose return value
    changes
  - **Then:** `diff_symbols` reports only the second as changed. Methods are named `Class.method`,
    fragments are rejoined, class cards are excluded, and unparseable text gives `None`.

- **Scenario:** each rule drops what it should, and only that
  - **Given:** synthetic events
  - **Then:**
    - a symbol absent at HEAD is dropped;
    - an "added" symbol whose short name disappears from another file in the same commit is
      dropped, while a rename within one file is not;
    - a symbol added twice in its file's history is dropped;
    - two qualifying commits in one month drop that (file, symbol, month);
    - a "changed" name present in two HEAD files is dropped.

- **Scenario:** a candidate becomes the same record every time
  - **Given:** one candidate of each template, with and without a linked PR
  - **Then:**
    - the question follows the template ("March 2023" for the month);
    - the `qid` is deterministic and valid;
    - required evidence holds the commit, plus the PR when linked;
    - the supporting anchor is today's path and X;
    - the split comes from `assign_splits`.

- **Scenario:** leakage facts are measured with the verdict's own code
  - **Given:** a small pool
  - **Then:** `measure` reports grep's rank-1 hit, the rare tokens shared with the gold document,
    hit@10 for grep and BM25-ident, and the short name's document frequency; and
    `passes_leakage_filter` retains exactly the records with no rank-1 hit and no shared rare token.

- **Scenario:** the generator, end to end on `history_repo`
  - **Given:** the fixture ingested and its pool written (boundary at its root commit)
  - **When:** generation runs twice
  - **Then:** there are exactly three candidates:
    - `alpha` added at `c1`;
    - `helper` added at `c1`;
    - `helper` changed at `c7`, with gold `c7` and PR 14.

    `alpha`'s two January-2020 changes are dropped by the month rule, and `B.grow` is dropped
    because it is gone at HEAD. All three are retained, and both runs write byte-identical files.

- **Scenario:** the verdict cannot pass green by mistake
  - **Given:** the verdict function
  - **Then:**
    - 99 retained dev records give INCONCLUSIVE;
    - 24.9% retention gives FAIL;
    - an upper bound of exactly 0.35 gives FAIL;
    - a stored fact that disagrees with the pool gives INCONCLUSIVE;
    - only dev records are read.

- **Scenario:** the probe moved, and nothing broke
  - **Given:** `scripts/leakage_probe.py` and `pace leakage`
  - **Then:** both run, `pace leakage` no longer prints a not-found line first, and CI's command is
    unchanged.

- **Scenario:** the real benchmark, generated and judged
  - **Given:** `pace`, with the B2 pool built
  - **When:** `pace golden generate` runs twice, then the verdict runs on dev
  - **Then:**
    - `auto_full.jsonl` and `golden_census.json` are byte-identical across the two runs;
    - candidates equal retained plus dropped;
    - each class's dev share is between 55% and 65%;
    - `results/leakage_verdict.json` is committed with whatever verdict it gives.

- **Scenario:** the plan documents agree
  - **Then:**
    - ADR-0003 carries the 2026-10-29 addendum, committed before generation;
    - ROADMAP shows B3 and its verdict;
    - tracker 006 row 4 is ✅;
    - `results/README.md` lists the census and the verdict;
    - the eval README describes `auto_full.jsonl`.

## Files to Modify

- `provenance/parse/symbols.py`, `provenance/eval/candidates.py`, `provenance/eval/generator.py`:
  new
- `provenance/eval/leakage.py`: new, by moving `scripts/leakage_probe.py`; the script becomes a shim
- `provenance/eval/schema.py`: the two leakage facts, and the new `passes_leakage_filter`
- `provenance/cli.py`: the `golden` sub-app with `generate`
- `provenance/eval/golden/auto_full.jsonl`, `results/golden_census.json`,
  `results/leakage_verdict.json`: new
- Tests: `tests/unit/test_symbols.py`, `tests/unit/test_candidate_rules.py`,
  `tests/unit/test_generator_records.py`, `tests/unit/test_leakage_verdict.py`,
  `tests/integration/test_golden_generation.py` (new); `tests/unit/test_cli_surface.py`
- `docs/ADR-0003-rationale-pool-benchmark.md` (the addendum, in the spec commit), `docs/ROADMAP.md`,
  `docs/tracker/006_EVAL_HARNESS.md`, `results/README.md`, `provenance/eval/README.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests -q
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace golden generate           # twice: both outputs byte-identical
    uv run pace leakage --from-db --golden provenance/eval/golden/auto_full.jsonl \
        --json-out results/leakage_verdict.json
